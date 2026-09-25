# -*- coding: utf-8 -*-
"""Checks de la PAUSA de envio a Telegram (ALERTAS_ENVIAR_TELEGRAM=0).

Cubre:
  (a) `NotificadorTelegram` en pausa NO hace ninguna llamada de red, devuelve
      False y avisa UNA vez por corrida con el conteo detectado.
  (b) `_envio_telegram_habilitado()` lee el setting (0 default / 1 envia).
  (c) `MotorAlertas` con pausa: completa el pipeline, llena `MencionDia` y el
      historial de dedup (BD temporal), `enviadas==0`, `envio_pausado is True`,
      la segunda corrida deduplica y el `ReporteDiario` queda `enviado=False`.

Sin red, sin Telegram y sin IA (clasificador ausente = fail-open). La BD de
produccion no se toca: se usa un SQLite temporal con `get_db_session`
monkeypatcheado en `alertas.motor` y `alertas.deduplicacion`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_alertas_pausa_envio.py   (solo este)
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from loguru import logger  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import alertas.deduplicacion as dedup_mod  # noqa: E402
import alertas.motor as motor_mod  # noqa: E402
import alertas.notificador as notif_mod  # noqa: E402
from core.models import AlertaHistorial, Base, MencionDia, ReporteDiario  # noqa: E402

_MENCIONES = [
    {
        "titulo": "Cliente Prueba anuncia obras públicas en la ciudad",
        "resumen": "El cliente detalló el plan de trabajo para este año.",
        "enlace": "https://ejemplo.test/nota-1",
        "fuente": "Google News",
        "fecha": datetime.now(timezone.utc).isoformat(),
    },
    {
        "titulo": "Cliente Prueba responde a señalamientos de la oposición",
        "resumen": "Mediante un comunicado, el cliente fijó postura.",
        "enlace": "https://ejemplo.test/nota-2",
        "fuente": "X (Twitter)",
        "fecha": datetime.now(timezone.utc).isoformat(),
    },
]


def _boom(*args, **kwargs):
    raise AssertionError("la pausa NO debe hacer llamadas de red")


@contextlib.contextmanager
def _db_temporal():
    """SQLite temporal con el mismo contrato de `get_db_session`."""
    carpeta = tempfile.TemporaryDirectory()
    ruta = Path(carpeta.name) / "alertas_pausa_test.db"
    engine = create_engine(
        f"sqlite:///{ruta.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(
        autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
    )

    @contextlib.contextmanager
    def sesion():
        db = fabrica()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    try:
        yield sesion, engine
    finally:
        engine.dispose()
        carpeta.cleanup()


class _SinIA:
    """Clasificador ausente: el motor debe ser fail-open (sin red de IA)."""


class _ClienteFalso:
    id = 1
    nombre = "Cliente Prueba"
    keywords = json.dumps(["Cliente Prueba", "Tema Generico"], ensure_ascii=False)
    localidad = ""
    exclude_terms = "[]"
    num_principales = 1
    telegram_chat_ids = "-100TEST"


class _GestorFalso:
    def obtener_clientes(self):
        return [_ClienteFalso()]


# --------------------------------------------------------------------------- #
# (a) Notificador en pausa
# --------------------------------------------------------------------------- #
def test_notificador_pausado(check):
    print("(a) NotificadorTelegram en pausa: sin red, False y un solo aviso")
    mensajes: list = []
    sink = logger.add(lambda m: mensajes.append(m.record["message"]), level="INFO", format="{message}")
    try:
        n = notif_mod.NotificadorTelegram()
        n.activo = True
        n.envio_habilitado = False
        n.pausa_actual = 0

        with mock.patch.object(notif_mod.requests, "post", _boom), \
             mock.patch.object(notif_mod, "resolver_url_google_news", _boom), \
             mock.patch.object(notif_mod, "acortar_url", _boom):
            r1 = n.enviar_alerta({"titulo": "Aviso", "enlace": "https://x.test/a"}, ["-1"])
            r2 = n.enviar_alerta({"titulo": "Aviso 2", "enlace": "https://x.test/b"}, ["-1"])
            r3 = n.enviar_resumen("-1", "Cliente Prueba", [{"fuente": "Google News"}])

        check(
            "pausa: enviar_alerta devuelve False y no toca red",
            r1 is False and r2 is False,
            repr((r1, r2)),
        )
        check("pausa: enviar_resumen devuelve False y no toca red", r3 is False, repr(r3))

        avisos = [m for m in mensajes if "Envío a Telegram en pausa" in m]
        check(
            "pausa: avisa UNA sola vez por corrida",
            len(avisos) == 1,
            repr(avisos),
        )
        check(
            "pausa: el aviso (sin total pre-fijado) trae el conteo disponible (1)",
            bool(avisos) and "1 alertas detectadas" in avisos[0],
            repr(avisos[:1]),
        )
        check(
            "pausa: el aviso menciona ALERTAS_ENVIAR_TELEGRAM=0 y el dashboard",
            bool(avisos)
            and "ALERTAS_ENVIAR_TELEGRAM=0" in avisos[0]
            and "dashboard" in avisos[0],
            repr(avisos[:1]),
        )

        # preparar_pausa(total): el motor fija el total exacto antes de enviar.
        antes = len(mensajes)
        n2 = notif_mod.NotificadorTelegram()
        n2.activo = True
        n2.envio_habilitado = False
        n2.preparar_pausa(7)
        n2.enviar_alerta({"titulo": "Aviso 3"}, ["-1"])
        nuevos = [m for m in mensajes[antes:] if "Envío a Telegram en pausa" in m]
        check(
            "pausa: preparar_pausa(7) avisa con el total exacto una vez",
            len(nuevos) == 1 and "7 alertas detectadas" in nuevos[0],
            repr(nuevos),
        )
    finally:
        logger.remove(sink)


# --------------------------------------------------------------------------- #
# (b) Flag del settings
# --------------------------------------------------------------------------- #
def test_flag_settings(check):
    print("(b) _envio_telegram_habilitado lee ALERTAS_ENVIAR_TELEGRAM")
    from core.config import settings

    original = settings.alertas_enviar_telegram
    try:
        settings.alertas_enviar_telegram = 0
        check(
            "flag: 0 -> pausa (False)",
            notif_mod._envio_telegram_habilitado() is False,
            repr(notif_mod._envio_telegram_habilitado()),
        )
        settings.alertas_enviar_telegram = 1
        check(
            "flag: 1 -> envío habilitado (True)",
            notif_mod._envio_telegram_habilitado() is True,
            repr(notif_mod._envio_telegram_habilitado()),
        )
        settings.alertas_enviar_telegram = "??"
        check(
            "flag: valor inválido -> pausa (no manda nada por error)",
            notif_mod._envio_telegram_habilitado() is False,
            repr(notif_mod._envio_telegram_habilitado()),
        )
    finally:
        settings.alertas_enviar_telegram = original


# --------------------------------------------------------------------------- #
# (c) Motor con pausa: registra aunque no envie
# --------------------------------------------------------------------------- #
def test_motor_pausado_registra(check):
    print("(c) MotorAlertas con pausa: MencionDia + dedup, enviadas=0")

    def _buscar_falso(*args, **kwargs):
        return [dict(m) for m in _MENCIONES]

    mensajes: list = []
    sink = logger.add(lambda m: mensajes.append(m.record["message"]), level="INFO", format="{message}")
    try:
        with _db_temporal() as (sesion, _engine):
            with mock.patch.object(motor_mod, "get_db_session", sesion), \
                 mock.patch.object(dedup_mod, "get_db_session", sesion):
                motor = motor_mod.MotorAlertas()
                motor.celulas_manager = _GestorFalso()
                motor.google_news.buscar = _buscar_falso
                motor.twitter_source.buscar = lambda *a, **k: []
                motor.filtros_ai = _SinIA()

                notificador = notif_mod.NotificadorTelegram()
                notificador.activo = True
                notificador.envio_habilitado = False
                notificador.pausa_actual = 0
                motor.notificador = notificador

                with mock.patch.object(notif_mod.requests, "post", _boom):
                    res1 = motor.ejecutar_alertas(horas=24)
                    res2 = motor.ejecutar_alertas(horas=24)
                    resumen = motor.enviar_resumen_diario()

            with sesion() as db:
                n_menciones = db.query(MencionDia).count()
                n_historial = db.query(AlertaHistorial).count()
                reportes = db.query(ReporteDiario).all()
    finally:
        logger.remove(sink)

    avisos_motor = [m for m in mensajes if "Envío a Telegram en pausa" in m]
    check(
        "motor pausa: avisa una vez con el total exacto (2)",
        len(avisos_motor) == 1 and "2 alertas detectadas" in avisos_motor[0],
        repr(avisos_motor),
    )

    check(
        "motor pausa: resultado trae envio_pausado=True",
        res1.get("envio_pausado") is True,
        repr(res1),
    )
    check(
        "motor pausa: enviadas==0 en ambas corridas",
        res1["enviadas"] == 0 and res2["enviadas"] == 0,
        repr((res1["enviadas"], res2["enviadas"])),
    )
    check(
        "motor pausa: MencionDia se llena con las 2 menciones finales",
        n_menciones == 2,
        repr(n_menciones),
    )
    check(
        "motor pausa: historial de dedup se llena (2 URLs)",
        n_historial == 2,
        repr(n_historial),
    )
    check(
        "motor pausa: segunda corrida deduplica sin duplicar registros",
        res2["duplicadas"] == 2 and n_menciones == 2 and n_historial == 2,
        repr((res2["duplicadas"], n_menciones, n_historial)),
    )
    check(
        "motor pausa: ReporteDiario registrado y sin marcar enviado",
        len(reportes) == 1 and reportes[0].enviado is False,
        repr([(r.total_alertas, r.enviado) for r in reportes]),
    )
    check(
        "motor pausa: resumen diario pausado (no enviado)",
        resumen.get("envio_pausado") is True
        and resumen.get("enviados") == 0
        and resumen.get("pausados") == 1,
        repr(resumen),
    )


def run(check):
    test_notificador_pausado(check)
    test_flag_settings(check)
    test_motor_pausado_registra(check)


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
