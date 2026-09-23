# -*- coding: utf-8 -*-
"""Tests de la CURVA DE ACELERACION ("modo explosion de tendencia") del motor.

Regla: fase 1 = SOLO cuentas Tier 1; tras `curva_fase1_min` minutos se libera
la fase 2 (Tier 2, Tier 3 y sin tier). La ronda no avanza ni genera textos
mientras el gate no deje elegibles. En fase 2 los roles rt/like/cita/comentario
apuntan a las URLs publicadas por Tier 1 (cascada); las cuentas Tier 3 solo
pueden RT/likes y se cuentan en `tier3_liberadas`.

Sin red, sin Chrome y SIN tocar la base real (fakes + reloj falso + parches,
mismo patron que las suites del motor).

Cubre:
    (1) `_configurar_curva`: sin Tier 1 -> desactivada con WARNING; duracion <=
        fase1 -> fase1 = max(1, duracion // 3); configuracion normal.
    (2) `_elegibilidad_curva` con reloj falso: fase 1 solo Tier 1; fase 2
        todos; `progreso["fase_actual"]` visible en `snapshot_progreso()`.
    (3) `_bucle_rondas(elegibilidad=...)`: el gate se evalua cada ronda, las
        cuentas no elegibles no cuentan como fallo y sin elegibles no avanza.
    (4) Cascada: `_capturar_url_fase1` (cap 50, perfil/RT fuera) y
        `_urls_efectivas_rol` solo en fase 2 para rt/like/cita/comentario; el
        contador `cascada_urls` del resumen.
    (5) E2E `repetir=False` con curva: DOS etapas secuenciales (Tier 1 y luego
        el resto) en `ejecutar_por_roles` y en `ejecutar` (cita masiva); sin
        curva todo ejecuta en una pasada (comportamiento previo).
    (6) Tier 3 en la curva: fase 1 lo ignora por completo (ni textos ni
        navegador); fase 2 lo libera para rt en cascada y lo cuenta en
        `tier3_liberadas` (Tier 2 sigue igual).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_curva_motor.py
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _parches(*cambios):
    """Aplica `(objeto, nombre, valor)` y restaura SIEMPRE al salir."""
    originales = []
    try:
        for objeto, nombre, valor in cambios:
            originales.append((objeto, nombre, getattr(objeto, nombre)))
            setattr(objeto, nombre, valor)
        yield
    finally:
        for objeto, nombre, valor in reversed(originales):
            setattr(objeto, nombre, valor)


class _CuentaFake:
    """Cuenta minima con los atributos que lee el motor (sin BD)."""

    def __init__(self, usuario, rol_activacion="", tier_calidad="", **extra):
        self.usuario = usuario
        self.auth_token = "auth_token_fake"
        self.cookies_json = ""
        self.password = ""
        self.tipo_cuenta = "politica"
        self.perfil_personalidad = "formal"
        self.personalidad = ""
        self.seccion = ""
        self.nombre_mostrado = ""
        self.rol_activacion = rol_activacion
        self.tier_calidad = tier_calidad
        for clave, valor in extra.items():
            setattr(self, clave, valor)


class _RelojFalso:
    """`time` minimo de motor: `monotonic` avanza y `sleep` no duerme."""

    def __init__(self, paso=0.5):
        self._t = 1000.0
        self._paso = float(paso)
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            self._t += self._paso
            return self._t

    def sleep(self, segundos):
        with self._lock:
            try:
                self._t += max(0.0, float(segundos))
            except (TypeError, ValueError):
                self._t += self._paso


class _LoggerFalso:
    """Captura los mensajes de loguru sin escribirlos."""

    def __init__(self):
        self.mensajes: list = []

    def _apunta(self, nivel):
        def _registrar(mensaje, *args, **kwargs):
            self.mensajes.append((nivel, str(mensaje)))
        return _registrar

    def __getattr__(self, nombre):
        if nombre in ("info", "warning", "error", "debug", "success", "critical"):
            return self._apunta(nombre)
        raise AttributeError(nombre)


def _patches_e2e(registros):
    """Parches comunes de un E2E del motor (sin BD, sin IA, sin Chrome)."""
    return (
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    )


def _accion_ok(usuario, rol, urls, texto, dar_like, retardo=0):
    return (usuario, rol, True, "ok", "https://x.com/t1/status/1")


# --------------------------------------------------------------------------- #
# (1) _configurar_curva
# --------------------------------------------------------------------------- #
def test_configurar_curva(check):
    print("(1) _configurar_curva: activacion, ajuste de fase y sin Tier 1")
    motor = MotorActivacion(max_concurrente=1)
    resumen: dict = {}
    activa, fase1 = motor._configurar_curva(
        [_CuentaFake("solo_t2", tier_calidad="tier2")], True, 5, 30, resumen
    )
    check(
        "sin Tier 1: curva desactivada con WARNING",
        activa is False and resumen["curva_aceleracion"] is False,
        f"(activa={activa}, resumen={resumen})",
    )
    check(
        "sin Tier 1: resumen con fase 0 y motor sin curva",
        resumen["curva_fase1_min"] == 0 and motor._curva_activa is False,
    )
    check(
        "sin Tier 1: la campana NO queda vacia (se sigue normal)",
        bool([_CuentaFake("solo_t2", tier_calidad="tier2")]),
    )

    motor2 = MotorActivacion(max_concurrente=1)
    resumen2: dict = {}
    activa2, fase2 = motor2._configurar_curva(
        [_CuentaFake("t1", tier_calidad="tier1")], True, 20, 10, resumen2
    )
    check(
        "duracion <= fase1: se ajusta a max(1, duracion // 3)",
        activa2 is True and fase2 == 3 and resumen2["curva_fase1_min"] == 3,
        f"(fase1={fase2}, {resumen2})",
    )
    check(
        "la fase 2 arranca a los fase1_min minutos",
        motor2._curva_activa is True
        and motor2._curva_fase1_seg == 3 * 60.0,
    )

    motor3 = MotorActivacion(max_concurrente=1)
    resumen3: dict = {}
    activa3, fase3 = motor3._configurar_curva(
        [_CuentaFake("t1", tier_calidad="Líder")], True, 6, 30, resumen3
    )
    check(
        "etiqueta de tier normalizada ('Líder' = Tier 1)",
        activa3 is True and fase3 == 6,
        f"(activa={activa3}, fase1={fase3})",
    )
    check(
        "curva_fase1_min=None usa la env/default (15)",
        MotorActivacion(max_concurrente=1)._configurar_curva(
            [_CuentaFake("t1", tier_calidad="tier1")], True, None, 60, {}
        )[1]
        == 15,
    )

    motor4 = MotorActivacion(max_concurrente=1)
    activa4, _f4 = motor4._configurar_curva(
        [_CuentaFake("t1", tier_calidad="tier1")], False, 5, 30, {}
    )
    check("curva_aceleracion=False no activa nada", activa4 is False)


# --------------------------------------------------------------------------- #
# (2) _elegibilidad_curva con reloj falso
# --------------------------------------------------------------------------- #
def test_elegibilidad_curva(check):
    print("(2) _elegibilidad_curva: fase 1 solo Tier 1, fase 2 todos")
    motor = MotorActivacion(max_concurrente=1)
    check(
        "sin curva activa no hay callable de elegibilidad",
        motor._elegibilidad_curva(False) is None,
    )
    motor._tier_de = {"t1": "tier1", "t2": "tier2", "sin_tier": ""}
    motor._curva_activa = True
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 60.0
        motor._curva_fase2_t0 = (
            motor_mod.time.monotonic() + motor._curva_fase1_seg
        )
        elegible = motor._elegibilidad_curva(True)
        check(
            "fase 1: Tier 1 elegible; Tier 2 y sin tier NO",
            elegible("t1") is True
            and elegible("t2") is False
            and elegible("sin_tier") is False,
        )
        check(
            "snapshot en fase 1",
            motor.snapshot_progreso().get("fase_actual") == 1,
            f"({motor.snapshot_progreso().get('fase_actual')})",
        )
        check(
            "cuenta desconocida en fase 1 no es elegible",
            elegible("otra") is False,
        )
        motor_mod.time.sleep(61)
        check(
            "fase 2: todas las cuentas elegibles",
            elegible("t1") is True
            and elegible("t2") is True
            and elegible("sin_tier") is True,
        )
        check(
            "snapshot en fase 2",
            motor.snapshot_progreso().get("fase_actual") == 2,
            f"({motor.snapshot_progreso().get('fase_actual')})",
        )
    finally:
        motor_mod.time = original_time


# --------------------------------------------------------------------------- #
# (3) Gate en _bucle_rondas
# --------------------------------------------------------------------------- #
def test_bucle_gate(check):
    print("(3) _bucle_rondas: el gate se evalua al iniciar cada ronda")
    motor = MotorActivacion(max_concurrente=1)
    motor._n_workers = lambda: 2
    procesables = [_CuentaFake("t1_gate"), _CuentaFake("t2_gate")]
    llamadas: list = []
    llamadas_gate = {"n": 0}

    def _elegible(usuario):
        # Fase 1 durante las 2 primeras evaluaciones de ronda; luego fase 2.
        llamadas_gate["n"] += 1
        fase = 1 if llamadas_gate["n"] <= 2 else 2
        with motor._lock:
            motor.progreso["fase_actual"] = fase
        if fase == 1:
            return usuario == "t1_gate"
        return True

    def _ejecutar(cuenta, texto, rol=""):
        llamadas.append(cuenta.usuario)
        return (cuenta.usuario, rol, True, "ok", "")

    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        rondas = motor._bucle_rondas(
            procesables,
            1,
            lambda ronda, usuarios=None: {u: "texto" for u in (usuarios or [])},
            _ejecutar,
            lambda resultado, ronda: None,
            elegibilidad=_elegible,
        )
    finally:
        motor_mod.time = original_time

    check("el bucle arranca al menos 1 ronda", rondas >= 1, f"({rondas})")
    check(
        "fase 1: solo ejecuto la cuenta Tier 1",
        "t1_gate" in llamadas,
        f"({sorted(set(llamadas))})",
    )
    check(
        "fase 2: la cuenta antes no elegible tambien ejecuto",
        "t2_gate" in llamadas,
        f"({sorted(set(llamadas))})",
    )
    check(
        "el gate se evaluo en cada ronda",
        llamadas_gate["n"] >= rondas,
        f"(gate={llamadas_gate['n']}, rondas={rondas})",
    )

    # Gate siempre cerrado: la ronda no avanza ni genera textos.
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._n_workers = lambda: 2
    generadas: list = []
    ejecutadas: list = []
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=2.0)
    try:
        rondas2 = motor2._bucle_rondas(
            procesables,
            1,
            lambda ronda, usuarios=None: generadas.append(ronda) or {},
            lambda c, t, r="": ejecutadas.append(c.usuario),
            lambda resultado, ronda: None,
            elegibilidad=lambda usuario: False,
        )
    finally:
        motor_mod.time = original_time
    check(
        "gate cerrado: 0 rondas y 0 textos generados",
        rondas2 == 0 and generadas == [],
        f"(rondas={rondas2}, generadas={generadas})",
    )
    check("gate cerrado: 0 ejecuciones", ejecutadas == [], f"({ejecutadas})")


# --------------------------------------------------------------------------- #
# (4) Cascada de URLs de fase 1
# --------------------------------------------------------------------------- #
def test_cascada(check):
    print("(4) cascada: captura de URLs de Tier 1 y uso en fase 2")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1": "tier1", "t2": "tier2"}
    motor._curva_activa = True
    motor._curva_fase2_t0 = 10 ** 12  # fase 1 (muy futuro)
    url_campana = ["https://x.com/camp/status/9"]

    motor._capturar_url_fase1("t1", "hashtags", "https://x.com/t1/status/1")
    check(
        "en fase 1 se captura la URL del post de Tier 1",
        motor._urls_fase1 == ["https://x.com/t1/status/1"],
        f"({motor._urls_fase1})",
    )
    check(
        "en fase 1 rt/cita usan las URLs de la campana",
        motor._urls_efectivas_rol("rt", url_campana) == url_campana,
    )
    motor._capturar_url_fase1("t1", "hashtags", "https://twitter.com/t1")
    motor._capturar_url_fase1("t2", "hashtags", "https://x.com/t2/status/2")
    motor._capturar_url_fase1("t1", "rt", "https://x.com/otro/status/3")
    check(
        "perfil propio, cuentas no Tier 1 y RTs no se capturan",
        motor._urls_fase1 == ["https://x.com/t1/status/1"],
        f"({motor._urls_fase1})",
    )
    for i in range(80):
        motor._capturar_url_fase1(
            "t1", "hashtags", f"https://x.com/t1/status/{100 + i}"
        )
    check(
        "cap de 50 URLs de fase 1",
        len(motor._urls_fase1) == 50,
        f"({len(motor._urls_fase1)})",
    )

    motor._curva_fase2_t0 = 0.0  # fase 2 (pasado)
    check(
        "fase 2: rt/like/cita/comentario usan las URLs de Tier 1",
        motor._urls_efectivas_rol("rt", url_campana) == motor._urls_fase1
        and motor._urls_efectivas_rol("like", url_campana) == motor._urls_fase1
        and motor._urls_efectivas_rol("cita", url_campana) == motor._urls_fase1
        and motor._urls_efectivas_rol("comentario", url_campana)
        == motor._urls_fase1,
    )
    check(
        "fase 2: hashtags sigue usando las URLs de la campana",
        motor._urls_efectivas_rol("hashtags", url_campana) == url_campana,
    )
    motor._marcar_cascada(motor._urls_fase1[0])
    motor._marcar_cascada("https://x.com/otra/status/1")
    resumen = motor._con_claves_pestana({"exitosas": 1})
    check(
        "cascada_urls cuenta SOLO las URLs de T1 usadas",
        resumen.get("cascada_urls") == 1,
        f"({resumen.get('cascada_urls')})",
    )

    # Plumbing en la ejecucion real (sin cuotas ni Chrome).
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._curva_activa = True
    motor2._curva_fase2_t0 = 0.0
    motor2._urls_fase1 = ["https://x.com/t1/status/1"]
    motor2._cuotas = None
    capturado: dict = {}

    def _intentar(cuenta, rol, urls, texto, dar_like):
        capturado["urls"] = list(urls)
        return (cuenta.usuario, rol, True, "ok", "")

    motor2._intentar_accion_rol = _intentar
    motor2._ejecutar_accion_rol(
        _CuentaFake("t2_rt"), "rt", url_campana, "", False, 0
    )
    check(
        "_ejecutar_accion_rol usa la cascada para el rol rt",
        capturado.get("urls") == ["https://x.com/t1/status/1"],
        f"({capturado})",
    )
    capturado.clear()
    motor2._intentar_quote_rt = (
        lambda c, urls, texto, dar_like: (
            capturado.__setitem__("urls", list(urls)),
            (c.usuario, True, "ok", ""),
        )[1]
    )
    motor2._quote_rt_una_cuenta(_CuentaFake("t2_cita"), url_campana, "texto", False, 0)
    check(
        "_quote_rt_una_cuenta usa la cascada para la cita",
        capturado.get("urls") == ["https://x.com/t1/status/1"],
        f"({capturado})",
    )


# --------------------------------------------------------------------------- #
# (5) E2E: dos etapas con curva / una pasada sin curva
# --------------------------------------------------------------------------- #
def _preparar_motor_roles(motor, cuentas, llamadas):
    motor._n_workers = lambda: 2
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #x"
            for lista in grupos.values() for c in lista
        }
    )
    motor._ejecutar_accion_rol = (
        lambda c, r, u, t_, d, retardo=0: (
            llamadas.append((c.usuario, r)),
            _accion_ok(c.usuario, r, u, t_, d),
        )[1]
    )
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]


def test_e2e_roles_dos_etapas(check):
    print("(5) E2E por roles: curva = 2 etapas (Tier 1 y luego el resto)")
    cuentas = [
        _CuentaFake("t1_curva", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("t2_curva", rol_activacion="rt", tier_calidad="tier2"),
        _CuentaFake("sin_tier_curva", rol_activacion="cita", tier_calidad=""),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=3,
            cohortes=1,
            repetir=False,
            curva_aceleracion=True,
            curva_fase1_min=1,
        )
    usuarios = [u for u, _r in llamadas]
    check(
        "las 3 cuentas ejecutan (etapa 1 + etapa 2)",
        sorted(usuarios) == ["sin_tier_curva", "t1_curva", "t2_curva"],
        f"({usuarios})",
    )
    check(
        "etapa 1 primero: la primera accion es de Tier 1",
        usuarios and usuarios[0] == "t1_curva",
        f"({usuarios})",
    )
    check(
        "resumen: curva activa con fase 1 y fase final 2",
        resumen.get("curva_aceleracion") is True
        and resumen.get("curva_fase1_min") == 1
        and resumen.get("fase_actual") == 2,
        f"(curva={resumen.get('curva_aceleracion')}, "
        f"fase1={resumen.get('curva_fase1_min')}, "
        f"fase={resumen.get('fase_actual')})",
    )
    check("resumen: exitosas=3", resumen.get("exitosas") == 3)
    check(
        "tras la etapa 2 el motor quedo en fase 2 (cascada habilitada)",
        motor._en_fase2() is True,
    )
    check(
        "la URL publicada por Tier 1 se capturo para la cascada",
        motor._urls_fase1 == ["https://x.com/t1/status/1"],
        f"({motor._urls_fase1})",
    )


def test_e2e_sin_curva(check):
    print("(5b) E2E por roles sin curva: una sola pasada (comportamiento previo)")
    cuentas = [
        _CuentaFake("t1_plano", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("t2_plano", rol_activacion="rt", tier_calidad="tier2"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=3,
            cohortes=1,
            repetir=False,
        )
    check(
        "sin curva las 2 cuentas ejecutan igual",
        sorted(u for u, _r in llamadas) == ["t1_plano", "t2_plano"],
        f"({llamadas})",
    )
    check(
        "sin curva: resumen con curva off, fase 0 y fase_actual 1",
        resumen.get("curva_aceleracion") is False
        and resumen.get("curva_fase1_min") == 0
        and resumen.get("fase_actual") == 1
        and resumen.get("cascada_urls") == 0,
        f"(curva={resumen.get('curva_aceleracion')}, "
        f"fase={resumen.get('fase_actual')}, "
        f"cascada={resumen.get('cascada_urls')})",
    )


def test_e2e_cita_masiva(check):
    print("(5c) E2E cita masiva: la curva tambien aplica (T1 y luego T2)")
    cuentas = [
        _CuentaFake("c_t1", tier_calidad="tier1"),
        _CuentaFake("c_t2", tier_calidad="tier2"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    motor._obtener_cuentas = lambda *a, **k: list(cuentas)
    motor._asignar_variaciones_cita = (
        lambda cs, *a, **k: {c.usuario: "texto" for c in cs}
    )
    motor._quote_rt_una_cuenta = (
        lambda c, urls, texto, dar_like, retardo=0: (
            llamadas.append(c.usuario),
            (c.usuario, True, "ok", "https://x.com/t1/status/1"),
        )[1]
    )
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            duracion_min=3,
            cohortes=1,
            repetir=False,
            curva_aceleracion=True,
            curva_fase1_min=1,
        )
    check(
        "cita masiva: etapa 1 (Tier 1) antes que la etapa 2 (Tier 2)",
        llamadas == ["c_t1", "c_t2"],
        f"({llamadas})",
    )
    check(
        "cita masiva: resumen con curva y fase final 2",
        resumen.get("curva_aceleracion") is True
        and resumen.get("fase_actual") == 2,
        f"(curva={resumen.get('curva_aceleracion')}, "
        f"fase={resumen.get('fase_actual')})",
    )


def test_e2e_sin_tier1(check):
    print("(5d) E2E con curva y CERO Tier 1: desactiva y sigue normal")
    cuentas = [
        _CuentaFake("solo_t2_a", rol_activacion="rt", tier_calidad="tier2"),
        _CuentaFake("solo_t2_b", rol_activacion="cita", tier_calidad="tier2"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    logger_falso = _LoggerFalso()
    with _parches(
        (motor_mod, "logger", logger_falso),
        *_patches_e2e(registros),
    ):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=3,
            cohortes=1,
            repetir=False,
            curva_aceleracion=True,
            curva_fase1_min=1,
        )
    check(
        "sin Tier 1: curva desactivada en el resumen",
        resumen.get("curva_aceleracion") is False
        and resumen.get("curva_fase1_min") == 0,
        f"(curva={resumen.get('curva_aceleracion')})",
    )
    check(
        "sin Tier 1: la campana ejecuta a las 2 cuentas (no queda vacia)",
        sorted(u for u, _r in llamadas) == ["solo_t2_a", "solo_t2_b"],
        f"({llamadas})",
    )
    check(
        "sin Tier 1: WARNING claro en el log",
        any(
            nivel == "warning" and "no tiene cuentas Tier 1" in mensaje
            for nivel, mensaje in logger_falso.mensajes
        ),
        f"({logger_falso.mensajes[:3]})",
    )


def test_e2e_rondas_curva(check):
    print("(5e) E2E en rondas con curva: fase 1 solo T1 y fase 2 despues")
    cuentas = [
        _CuentaFake("r_t1", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("r_t2", rol_activacion="rt", tier_calidad="tier2"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(*_patches_e2e(registros)):
            resumen = motor.ejecutar_por_roles(
                urls=["https://x.com/ancla"],
                texto_base="texto",
                hashtags="#x",
                usuarios=[c.usuario for c in cuentas],
                duracion_min=3,
                cohortes=1,
                repetir=True,
                curva_aceleracion=True,
                curva_fase1_min=1,
            )
    finally:
        motor_mod.time = original_time
    usuarios = [u for u, _r in llamadas]
    check(
        "rondas: la primera accion es de Tier 1",
        usuarios and usuarios[0] == "r_t1",
        f"({usuarios[:4]})",
    )
    check(
        "rondas: Tier 2 tambien ejecuta al liberarse la fase 2",
        "r_t2" in usuarios,
        f"({sorted(set(usuarios))})",
    )
    check(
        "rondas: resumen con curva activa y fase final 2",
        resumen.get("curva_aceleracion") is True
        and resumen.get("curva_fase1_min") == 1
        and resumen.get("fase_actual") == 2,
        f"(curva={resumen.get('curva_aceleracion')}, "
        f"fase1={resumen.get('curva_fase1_min')}, "
        f"fase={resumen.get('fase_actual')})",
    )


# --------------------------------------------------------------------------- #
# (6) Tier 3 en la curva: fase 1 lo ignora; fase 2 lo libera (rt en cascada)
# --------------------------------------------------------------------------- #
def test_fase1_ignora_tier3(check):
    print("(6) curva fase 1: Tier 3 completamente ignorado (rt incluido)")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1_ig": "tier1", "t2_ig": "tier2", "t3_ig": "tier3"}
    motor._curva_activa = True
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible = motor._elegibilidad_curva(True)
        check(
            "fase 1: Tier 3 con rol rt NO es elegible",
            elegible("t3_ig") is False,
        )
        check(
            "fase 1: Tier 2 tampoco es elegible",
            elegible("t2_ig") is False,
        )
        check("fase 1: Tier 1 si es elegible", elegible("t1_ig") is True)
        check(
            "la fase queda en 1",
            motor.snapshot_progreso().get("fase_actual") == 1,
            f"({motor.snapshot_progreso().get('fase_actual')})",
        )

        motor._n_workers = lambda: 2
        generadas: list = []
        ejecutadas: list = []
        rondas = motor._bucle_rondas(
            [_CuentaFake("t3_ig", rol_activacion="rt", tier_calidad="tier3")],
            1,
            lambda ronda, usuarios=None: generadas.append(ronda) or {},
            lambda c, t, r="": ejecutadas.append(c.usuario),
            lambda resultado, ronda: None,
            elegibilidad=elegible,
        )
    finally:
        motor_mod.time = original_time
    check(
        "fase 1: 0 rondas con solo Tier 3",
        rondas == 0,
        f"({rondas})",
    )
    check(
        "fase 1: 0 textos generados para Tier 3",
        generadas == [],
        f"({generadas})",
    )
    check(
        "fase 1: 0 ejecuciones de Tier 3",
        ejecutadas == [],
        f"({ejecutadas})",
    )


def test_e2e_fase2_tier3_rt_cascada(check):
    print("(6b) curva fase 2: Tier 3 ejecuta rt en cascada y cuenta en tier3_liberadas")
    cuentas = [
        _CuentaFake("t1_cas", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("t2_cas", rol_activacion="cita", tier_calidad="tier2"),
        _CuentaFake("t3_cas", rol_activacion="rt", tier_calidad="tier3"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    motor._n_workers = lambda: 2
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #x"
            for lista in grupos.values() for c in lista
        }
    )
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]
    # Sin esperas reales de cohorte/anti-spam (los retardos se conservan).
    motor._dormir_cancelable = lambda *a, **k: True
    orden: list = []
    capturado: dict = {}
    urls_publicadas = {
        "t1_cas": "https://x.com/t1_cas/status/1",
        "t2_cas": "https://x.com/t2_cas/status/2",
        "t3_cas": "",
    }

    def _intentar(cuenta, rol, urls, texto, dar_like):
        orden.append(cuenta.usuario)
        capturado[cuenta.usuario] = list(urls)
        return (
            cuenta.usuario, rol, True, "ok",
            urls_publicadas.get(cuenta.usuario, ""),
        )

    motor._intentar_accion_rol = _intentar
    registros: list = []
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=3,
            cohortes=1,
            repetir=False,
            curva_aceleracion=True,
            curva_fase1_min=1,
        )
    check(
        "etapa 1 primero: solo Tier 1 ejecuto antes que el resto",
        orden and orden[0] == "t1_cas"
        and sorted(orden) == ["t1_cas", "t2_cas", "t3_cas"],
        f"({orden})",
    )
    check(
        "Tier 1 de fase 1 uso las URLs de la campana (sin cascada todavia)",
        capturado.get("t1_cas") == ["https://x.com/ancla"],
        f"({capturado.get('t1_cas')})",
    )
    check(
        "Tier 3 de fase 2 ejecuta rt apuntando a las URLs de Tier 1 (cascada)",
        capturado.get("t3_cas") == ["https://x.com/t1_cas/status/1"],
        f"({capturado.get('t3_cas')})",
    )
    check(
        "Tier 2 de fase 2 tambien usa la cascada (sigue igual)",
        capturado.get("t2_cas") == ["https://x.com/t1_cas/status/1"],
        f"({capturado.get('t2_cas')})",
    )
    check(
        "tier3_liberadas = 1 (solo el Tier 3 con exito en fase 2)",
        resumen.get("tier3_liberadas") == 1,
        f"({resumen.get('tier3_liberadas')})",
    )
    check(
        "resumen: 3 exitosas, fase final 2 y sin omitidas Tier 3",
        resumen.get("exitosas") == 3
        and resumen.get("fase_actual") == 2
        and resumen.get("tier3_omitidas") == 0,
        f"(exitosas={resumen.get('exitosas')}, "
        f"fase={resumen.get('fase_actual')}, "
        f"tier3_omitidas={resumen.get('tier3_omitidas')})",
    )
    check(
        "el motor quedo en fase 2 (cascada habilitada)",
        motor._en_fase2() is True,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_configurar_curva(check)
    test_elegibilidad_curva(check)
    test_bucle_gate(check)
    test_cascada(check)
    test_e2e_roles_dos_etapas(check)
    test_e2e_sin_curva(check)
    test_e2e_cita_masiva(check)
    test_e2e_sin_tier1(check)
    test_e2e_rondas_curva(check)
    test_fase1_ignora_tier3(check)
    test_e2e_fase2_tier3_rt_cascada(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_curva_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
