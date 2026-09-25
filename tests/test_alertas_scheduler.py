# -*- coding: utf-8 -*-
"""Tests rapidos del disparo 24/7 de alertas en el scheduler (sin red, sin Telegram).

Cubre:
  (a) `config_alertas()`: defaults de `core.config.settings`, invalidos,
      clamps (ventana >= 1, hora 0..23) e interruptores 0/1.
  (b) `core.config.Settings()` leyendo los env ALERTAS_* (aliases en MAYUSCULAS).
  (c) Gate `con_alertas` con un scheduler fake: el dashboard
      (`SchedulerManager()` sin flags) NUNCA registra jobs de alertas; con
      `con_alertas=True` se registran `alertas_periodicas` (IntervalTrigger de
      ALERTAS_INTERVALO_MIN) y `alertas_resumen_diario` (CronTrigger de
      ALERTAS_RESUMEN_DIARIO_HORA), con max_instances=1/coalesce/replace_existing.
  (d) Wrappers `_alertas` / `_resumen_diario_alertas`: llaman al motor con la
      ventana configurada, toleran motor sin metodo/firma vieja y NUNCA lanzan.
  (e) APScheduler REAL registrando los jobs (start sin hilos).
  (f) Contratos estaticos: `scheduler/standalone.py` pasa
      `con_calentamiento=True` y `con_alertas=True`; ninguna pagina web pasa
      `con_alertas` (evita envios duplicados).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_alertas_scheduler.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402
from apscheduler.triggers.cron import CronTrigger  # noqa: E402
from apscheduler.triggers.interval import IntervalTrigger  # noqa: E402

from core.config import Settings  # noqa: E402
from scheduler import manager as manager_mod  # noqa: E402

_VARS = (
    "ALERTAS_ACTIVO",
    "ALERTAS_INTERVALO_MIN",
    "ALERTAS_VENTANA_HORAS",
    "ALERTAS_RESUMEN_DIARIO",
    "ALERTAS_RESUMEN_DIARIO_HORA",
    "ALERTAS_ENVIAR_TELEGRAM",
)

_DEFAULT = {
    "activo": True,
    "intervalo_min": 60,
    "ventana_horas": 6,
    "resumen_diario": True,
    "resumen_diario_hora": 22,
}

_CAMPOS_SETTINGS = (
    "alertas_activo",
    "alertas_intervalo_min",
    "alertas_ventana_horas",
    "alertas_resumen_diario",
    "alertas_resumen_diario_hora",
    "alertas_enviar_telegram",
)


@contextlib.contextmanager
def _env(**valores):
    """Fija SOLO los env ALERTAS_* y restaura el entorno al salir."""
    originales = {clave: os.environ.pop(clave, None) for clave in _VARS}
    try:
        for clave, valor in valores.items():
            if valor is not None:
                os.environ[clave] = str(valor)
        yield
    finally:
        for clave in _VARS:
            os.environ.pop(clave, None)
        for clave, valor in originales.items():
            if valor is not None:
                os.environ[clave] = valor


@contextlib.contextmanager
def _settings_tocados(**valores):
    """Monkeypatch temporal de atributos de `core.config.settings`."""
    from core.config import settings

    originales = {campo: getattr(settings, campo) for campo in _CAMPOS_SETTINGS}
    try:
        for campo, valor in valores.items():
            setattr(settings, campo, valor)
        yield settings
    finally:
        for campo, valor in originales.items():
            setattr(settings, campo, valor)


class _FakeScheduler:
    """Fake de APScheduler: registra jobs sin arrancar hilos reales."""

    def __init__(self, *args, **kwargs):
        self.jobs = []
        self.started = False
        self.apagado = False

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        self.started = True

    @property
    def running(self):
        return self.started and not self.apagado

    def shutdown(self):
        self.apagado = True


class _APSchedulerRealSinArranque(BackgroundScheduler):
    """BackgroundScheduler REAL que no arranca hilos (solo registra los jobs)."""

    arrancado_en_prueba = False

    def start(self, *args, **kwargs):  # noqa: D401
        type(self).arrancado_en_prueba = True  # sin super(): no hay event loop


@contextlib.contextmanager
def _manager(config=None, con_calentamiento=False, con_alertas=False):
    """Crea un `SchedulerManager` con scheduler fake y `config_alertas` fijo.

    `config=None` -> se parchea `config_alertas` con los defaults. Se evita
    tocar el calentamiento real (se registra con su propio gate).
    """
    cfg = dict(_DEFAULT) if config is None else dict(config)
    creados = []

    def _crear_scheduler(*args, **kwargs):
        creado = _FakeScheduler(*args, **kwargs)
        creados.append(creado)
        return creado

    with mock.patch.object(
        manager_mod, "BackgroundScheduler", _crear_scheduler
    ), mock.patch.object(manager_mod, "config_alertas", lambda: dict(cfg)):
        obj = manager_mod.SchedulerManager(
            con_calentamiento=con_calentamiento, con_alertas=con_alertas
        )
        try:
            yield obj, creados[0]
        finally:
            try:
                obj.detener()
            except Exception:  # noqa: BLE001
                pass


@contextlib.contextmanager
def _motor_falso(clase):
    """Inyecta una clase `MotorAlertas` falsa en `alertas.motor`."""
    modulo = types.ModuleType("alertas.motor")
    modulo.MotorAlertas = clase
    anterior = sys.modules.get("alertas.motor")
    sys.modules["alertas.motor"] = modulo
    try:
        yield
    finally:
        if anterior is not None:
            sys.modules["alertas.motor"] = anterior
        else:
            sys.modules.pop("alertas.motor", None)


class _MotorFalso:
    """Motor fake con el contrato esperado (nuevos kwargs al final)."""

    llamadas: list = []
    resultado = {"total": 3, "enviadas": 2, "duplicadas": 1, "filtradas": 0}
    error = None

    def ejecutar_alertas(self, cliente_id=None, horas=24, resumen_diario=False):
        type(self).llamadas.append(
            {"cliente_id": cliente_id, "horas": horas, "resumen_diario": resumen_diario}
        )
        if type(self).error is not None:
            raise type(self).error
        return dict(type(self).resultado)

    def enviar_resumen_diario(self):
        type(self).llamadas.append({"resumen_diario": True})


class _MotorFirmaVieja:
    """Motor con la firma vieja SIN kwargs (la llamada debe caer a defaults)."""

    llamadas: list = []

    def ejecutar_alertas(self):
        type(self).llamadas.append({})
        return {"total": 0}


class _MotorSinMetodos:
    """Motor que aun no expone los metodos nuevos (no debe romper)."""


def _reset(monkeypatch_clase=None):
    _MotorFalso.llamadas = []
    _MotorFalso.error = None
    _MotorFirmaVieja.llamadas = []


# --------------------------------------------------------------------------- #
# (a) config_alertas
# --------------------------------------------------------------------------- #
def test_config_alertas(check):
    print("(a) config_alertas: defaults, invalidos y clamps")
    with _settings_tocados(
        alertas_activo=1,
        alertas_intervalo_min=60,
        alertas_ventana_horas=6,
        alertas_resumen_diario=1,
        alertas_resumen_diario_hora=22,
    ):
        cfg = manager_mod.config_alertas()
        check(
            "config_alertas: defaults de settings",
            cfg == _DEFAULT,
            repr(cfg),
        )

    with _settings_tocados(
        alertas_activo=0,
        alertas_intervalo_min=30,
        alertas_ventana_horas=12,
        alertas_resumen_diario=0,
        alertas_resumen_diario_hora=7,
    ):
        cfg = manager_mod.config_alertas()
        check(
            "config_alertas: 0/12/7 se respetan",
            (cfg["activo"], cfg["intervalo_min"], cfg["ventana_horas"],
             cfg["resumen_diario"], cfg["resumen_diario_hora"])
            == (False, 30, 12, False, 7),
            repr(cfg),
        )

    with _settings_tocados(
        alertas_activo="abc",
        alertas_intervalo_min="??",
        alertas_ventana_horas=0,
        alertas_resumen_diario=None,
        alertas_resumen_diario_hora=99,
    ):
        cfg = manager_mod.config_alertas()
        check(
            "config_alertas: invalidos -> fallback del manager (activo, intervalo 180)",
            (cfg["activo"], cfg["intervalo_min"]) == (True, 180),
            repr(cfg),
        )
        check(
            "config_alertas: ventana 0 -> 1",
            cfg["ventana_horas"] == 1,
            repr(cfg["ventana_horas"]),
        )
        check(
            "config_alertas: hora 99 -> 23",
            cfg["resumen_diario_hora"] == 23,
            repr(cfg["resumen_diario_hora"]),
        )

    with _settings_tocados(alertas_resumen_diario_hora=-5):
        cfg = manager_mod.config_alertas()
        check(
            "config_alertas: hora -5 -> 0",
            cfg["resumen_diario_hora"] == 0,
            repr(cfg["resumen_diario_hora"]),
        )

    with _settings_tocados(alertas_intervalo_min=-30):
        cfg = manager_mod.config_alertas()
        check(
            "config_alertas: intervalo negativo se conserva (el job lo apaga)",
            cfg["intervalo_min"] == -30,
            repr(cfg["intervalo_min"]),
        )


# --------------------------------------------------------------------------- #
# (b) Settings() con env ALERTAS_*
# --------------------------------------------------------------------------- #
def test_settings_env(check):
    print("(b) core.config.Settings() lee los env ALERTAS_*")
    with _env():
        nuevo = Settings()
        check(
            "Settings: sin env usa defaults (1/60/6/1/22 y pausa Telegram)",
            (
                nuevo.alertas_activo,
                nuevo.alertas_intervalo_min,
                nuevo.alertas_ventana_horas,
                nuevo.alertas_resumen_diario,
                nuevo.alertas_resumen_diario_hora,
            )
            == (1, 60, 6, 1, 22),
            repr(
                (
                    nuevo.alertas_activo,
                    nuevo.alertas_intervalo_min,
                    nuevo.alertas_ventana_horas,
                    nuevo.alertas_resumen_diario,
                    nuevo.alertas_resumen_diario_hora,
                )
            ),
        )
        check(
            "Settings: ALERTAS_ENVIAR_TELEGRAM default 0 (pausa)",
            nuevo.alertas_enviar_telegram == 0,
            repr(nuevo.alertas_enviar_telegram),
        )

    with _env(
        ALERTAS_ACTIVO="0",
        ALERTAS_INTERVALO_MIN="45",
        ALERTAS_VENTANA_HORAS="12",
        ALERTAS_RESUMEN_DIARIO="0",
        ALERTAS_RESUMEN_DIARIO_HORA="7",
        ALERTAS_ENVIAR_TELEGRAM="1",
    ):
        nuevo = Settings()
        check(
            "Settings: ALERTAS_* en MAYUSCULAS se leen",
            (
                nuevo.alertas_activo,
                nuevo.alertas_intervalo_min,
                nuevo.alertas_ventana_horas,
                nuevo.alertas_resumen_diario,
                nuevo.alertas_resumen_diario_hora,
                nuevo.alertas_enviar_telegram,
            )
            == (0, 45, 12, 0, 7, 1),
            repr(
                (
                    nuevo.alertas_activo,
                    nuevo.alertas_intervalo_min,
                    nuevo.alertas_ventana_horas,
                    nuevo.alertas_resumen_diario,
                    nuevo.alertas_resumen_diario_hora,
                    nuevo.alertas_enviar_telegram,
                )
            ),
        )


# --------------------------------------------------------------------------- #
# (c) gate con_alertas (scheduler fake)
# --------------------------------------------------------------------------- #
def test_gate_con_alertas(check):
    print("(c) SchedulerManager: gate con_alertas (standalone vs dashboard)")

    with _manager() as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: SchedulerManager() sin flags NO registra alertas",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    with _manager(con_alertas=False) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: con_alertas=False NO registra jobs de alertas",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    with _manager(con_alertas=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: con_alertas=True registra periodicas + resumen diario",
            ids == {"verificar_tareas", "alertas_periodicas", "alertas_resumen_diario"},
            repr(sorted(ids)),
        )
        job = next(j for j in sched.jobs if j["id"] == "alertas_periodicas")
        intervalo = getattr(job.get("trigger"), "interval", None)
        check(
            "gate: el job periodico corre cada ALERTAS_INTERVALO_MIN (60 min)",
            intervalo is not None and intervalo.total_seconds() == 60 * 60,
            repr(intervalo),
        )
        check(
            "gate: el job periodico usa max_instances=1, coalesce y replace_existing",
            callable(job.get("func"))
            and job.get("max_instances") == 1
            and job.get("coalesce") is True
            and job.get("replace_existing") is True,
            repr(
                {
                    k: job.get(k)
                    for k in ("max_instances", "coalesce", "replace_existing")
                }
            ),
        )
        cron = next(j for j in sched.jobs if j["id"] == "alertas_resumen_diario")
        check(
            "gate: el resumen diario usa CronTrigger",
            isinstance(cron.get("trigger"), CronTrigger),
            repr(cron.get("trigger")),
        )
        check(
            "gate: el resumen diario es a las 22:00",
            "hour='22'" in str(cron.get("trigger")),
            str(cron.get("trigger")),
        )
        check(
            "gate: el resumen diario tambien usa max_instances=1/coalesce/replace",
            cron.get("max_instances") == 1
            and cron.get("coalesce") is True
            and cron.get("replace_existing") is True,
            repr(
                {
                    k: cron.get(k)
                    for k in ("max_instances", "coalesce", "replace_existing")
                }
            ),
        )

    config_apagado = dict(_DEFAULT, activo=False)
    with _manager(config=config_apagado, con_alertas=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: ALERTAS_ACTIVO=0 ni con con_alertas=True registra",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    sin_intervalo = dict(_DEFAULT, intervalo_min=0)
    with _manager(config=sin_intervalo, con_alertas=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: ALERTAS_INTERVALO_MIN=0 apaga el job",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    sin_resumen = dict(_DEFAULT, resumen_diario=False)
    with _manager(config=sin_resumen, con_alertas=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: ALERTAS_RESUMEN_DIARIO=0 deja solo el periodico",
            ids == {"verificar_tareas", "alertas_periodicas"},
            repr(sorted(ids)),
        )

    resumen_7 = dict(_DEFAULT, resumen_diario_hora=7)
    with _manager(config=resumen_7, con_alertas=True) as (obj, sched):
        cron = next(
            j for j in sched.jobs if j["id"] == "alertas_resumen_diario"
        )
        check(
            "gate: ALERTAS_RESUMEN_DIARIO_HORA=7 se respeta",
            "hour='7'" in str(cron.get("trigger")),
            str(cron.get("trigger")),
        )

    with _manager(con_calentamiento=True, con_alertas=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "gate: calentamiento y alertas conviven con los dos flags",
            ids
            == {
                "verificar_tareas",
                "calentamiento_continuo",
                "alertas_periodicas",
                "alertas_resumen_diario",
            },
            repr(sorted(ids)),
        )


# --------------------------------------------------------------------------- #
# (d) wrappers
# --------------------------------------------------------------------------- #
def test_wrappers(check):
    print("(d) Wrappers _alertas / _resumen_diario_alertas")

    _reset()
    with _manager(config=dict(_DEFAULT, ventana_horas=6), con_alertas=True) as (obj, _):
        with _motor_falso(_MotorFalso):
            obj._alertas()
        check(
            "wrapper: _alertas llama al motor con horas=ALERTAS_VENTANA_HORAS",
            _MotorFalso.llamadas
            and _MotorFalso.llamadas[0].get("horas") == 6
            and _MotorFalso.llamadas[0].get("cliente_id") is None,
            repr(_MotorFalso.llamadas),
        )

    _reset()
    _MotorFalso.error = RuntimeError("boom")
    with _manager(con_alertas=True) as (obj, _):
        with _motor_falso(_MotorFalso):
            try:
                obj._alertas()
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
        check("wrapper: _alertas NO lanza si el motor falla", ok)
    _MotorFalso.error = None

    _reset()
    with _manager(con_alertas=True) as (obj, _):
        with _motor_falso(_MotorSinMetodos):
            try:
                obj._alertas()
                obj._resumen_diario_alertas()
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
        check("wrapper: motor sin metodos -> no lanza", ok)

    _reset()
    with _manager(con_alertas=True) as (obj, _):
        with _motor_falso(_MotorFirmaVieja):
            try:
                obj._alertas()
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
        check(
            "wrapper: firma vieja (sin kwargs) -> cae a la llamada sin args",
            ok and _MotorFirmaVieja.llamadas == [{}],
            repr(_MotorFirmaVieja.llamadas),
        )

    _reset()
    with _manager(con_alertas=True) as (obj, _):
        with _motor_falso(_MotorFalso):
            obj._resumen_diario_alertas()
        check(
            "wrapper: _resumen_diario_alertas llama enviar_resumen_diario()",
            _MotorFalso.llamadas == [{"resumen_diario": True}],
            repr(_MotorFalso.llamadas),
        )

    _reset()
    with _manager(con_alertas=True) as (obj, _):
        with _motor_falso(_MotorSinMetodos):
            try:
                obj._resumen_diario_alertas()
                ok = True
            except Exception:  # noqa: BLE001
                ok = False
        check("wrapper: resumen sin metodo en el motor -> no lanza", ok)


# --------------------------------------------------------------------------- #
# (e) APScheduler REAL registrando los jobs
# --------------------------------------------------------------------------- #
def test_apscheduler_real(check):
    print("(e) APScheduler real: jobs registrados sin arrancar hilos")

    with mock.patch.object(
        manager_mod, "BackgroundScheduler", _APSchedulerRealSinArranque
    ):
        obj = manager_mod.SchedulerManager(con_calentamiento=False, con_alertas=True)
    try:
        jobs = {job.id: job for job in obj.scheduler.get_jobs()}
        check(
            "apscheduler real: registra verificar_tareas + alertas",
            set(jobs) == {"verificar_tareas", "alertas_periodicas", "alertas_resumen_diario"},
            repr(sorted(jobs)),
        )
        periodico = jobs.get("alertas_periodicas")
        check(
            "apscheduler real: trigger IntervalTrigger cada 60 min",
            isinstance(getattr(periodico, "trigger", None), IntervalTrigger)
            and periodico.trigger.interval.total_seconds() == 60 * 60,
            repr(getattr(periodico, "trigger", None)),
        )
        resumen = jobs.get("alertas_resumen_diario")
        check(
            "apscheduler real: trigger CronTrigger hour=22",
            isinstance(getattr(resumen, "trigger", None), CronTrigger)
            and "hour='22'" in str(resumen.trigger),
            repr(getattr(resumen, "trigger", None)),
        )
        check(
            "apscheduler real: max_instances=1 y coalesce en ambos",
            periodico.max_instances == 1
            and periodico.coalesce is True
            and resumen.max_instances == 1
            and resumen.coalesce is True,
            repr(
                (
                    periodico.max_instances,
                    periodico.coalesce,
                    resumen.max_instances,
                    resumen.coalesce,
                )
            ),
        )
    finally:
        try:
            obj.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    with mock.patch.object(
        manager_mod, "BackgroundScheduler", _APSchedulerRealSinArranque
    ):
        obj = manager_mod.SchedulerManager()
    try:
        jobs = {job.id for job in obj.scheduler.get_jobs()}
        check(
            "apscheduler real: sin flags solo verificar_tareas",
            jobs == {"verificar_tareas"},
            repr(sorted(jobs)),
        )
    finally:
        try:
            obj.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# (f) contratos estaticos
# --------------------------------------------------------------------------- #
def test_contratos_estaticos(check):
    print("(f) Contratos: standalone y dashboard")

    texto = (RAIZ / "scheduler" / "standalone.py").read_text(encoding="utf-8")
    check(
        "standalone: pasa con_alertas=True",
        "con_alertas=True" in texto,
        "",
    )
    check(
        "standalone: conserva con_calentamiento=True",
        "con_calentamiento=True" in texto,
        "",
    )

    import inspect

    firma = inspect.signature(manager_mod.SchedulerManager.__init__)
    check(
        "manager: con_alertas default False (dashboard no lo activa)",
        firma.parameters.get("con_alertas") is not None
        and firma.parameters["con_alertas"].default is False,
        repr(firma),
    )

    archivos = []
    for ruta in sorted((RAIZ / "web").rglob("*.py")):
        texto = ruta.read_text(encoding="utf-8")
        if "SchedulerManager(" in texto:
            archivos.append(ruta.name)
            check(
                f"web: {ruta.name} NO pasa con_alertas (evita duplicados)",
                "con_alertas" not in texto,
                "",
            )
    check(
        "web: siguen existiendo los call sites del dashboard (>= 3)",
        len(archivos) >= 3,
        f"archivos={sorted(archivos)}",
    )

    texto_env = (RAIZ / ".env.example").read_text(encoding="utf-8")
    for env in _VARS:
        check(
            f".env.example: documenta {env}",
            env in texto_env,
            "",
        )


def run(check):
    test_config_alertas(check)
    test_settings_env(check)
    test_gate_con_alertas(check)
    test_wrappers(check)
    test_apscheduler_real(check)
    test_contratos_estaticos(check)


if __name__ == "__main__":
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "run_tests", str(RAIZ / "tests" / "run_tests.py")
    )
    _runner = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_runner)
    try:
        run(_runner.check)
    finally:
        sys.exit(_runner.resumen())
