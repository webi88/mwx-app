# -*- coding: utf-8 -*-
"""Tests de RESERVAS DE RESPALDO + rotacion por cuota diaria en el motor.

Regla: las cuentas "Agotadas por hoy" (tope diario) se retiran de la campana y
se sustituyen 1:1 por cuentas de respaldo (`reserva_usuarios`). En modo fijo la
reserva toma el MISMO rol de la cuenta retirada y una reserva Tier 2 JAMAS
sustituye un rol hashtags. Sin `reserva_usuarios` nada cambia.

Sin red, sin Chrome y SIN tocar la base real (fakes + contadores parcheados).

Cubre:
    (1) `_preparar_reservas`: None/vacio, deduplicacion, ya-en-campana, sin
        sesion, `solo_con_registro` y tope `RESERVA_MAX_CUENTAS`.
    (2) `_rotar_agotadas_dia`: sustitucion 1:1 (mismo rol), bloqueo de Tier 2
        para hashtags, contadores y listas mutadas.
    (3) E2E single-pass: la agotada se sustituye ANTES de encolar; contadores
        `agotadas_dia`/`rotadas_por_cuota_dia`/`reserva_*`.
    (4) E2E en rondas: la rotacion corre al iniciar cada ronda.
    (5) Sin `reserva_usuarios` el comportamiento y los contadores no cambian.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_reserva_diaria_motor.py
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
from activaciones.cuotas import CuotasHorarias  # noqa: E402
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


def _preparar_cuotas_fake(cuotas, usuarios, base_dia=None, base_horaria=None):
    """`preparar` con las dos consultas a BD parcheadas."""
    with _parches(
        (
            cuotas_mod,
            "contar_acciones_por_usuario",
            lambda *a, **k: dict(base_horaria or {}),
        ),
        (
            cuotas_mod,
            "contar_acciones_dia_por_usuario",
            lambda *a, **k: dict(base_dia or {}),
        ),
    ):
        cuotas.preparar(usuarios)


def _patches_e2e(registros, base_dia=None):
    """Parches comunes de un E2E del motor (sin BD, sin IA, sin Chrome)."""
    return (
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (
            cuotas_mod,
            "contar_acciones_dia_por_usuario",
            lambda *a, **k: dict(base_dia or {}),
        ),
    )


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
            (c.usuario, r, True, "ok", "https://x.com/t1/status/1"),
        )[1]
    )
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]


# --------------------------------------------------------------------------- #
# (1) _preparar_reservas
# --------------------------------------------------------------------------- #
def test_preparar_reservas(check):
    print("(1) _preparar_reservas: filtros, dedupe y tope de respaldos")
    motor = MotorActivacion(max_concurrente=1)
    check("None -> sin reservas", motor._preparar_reservas(None, []) == [])
    check("lista vacia -> sin reservas", motor._preparar_reservas([], []) == [])

    principal = _CuentaFake("principal")
    r1 = _CuentaFake("reserva_a")
    r2 = _CuentaFake("reserva_b")
    duplicada = _CuentaFake("Reserva_A")  # duplicada sin distinguir mayusculas
    sin_sesion = _CuentaFake("sin_sesion", auth_token="", password="")
    reservas = motor._preparar_reservas(
        [r1, duplicada, r2, principal, sin_sesion],
        [principal],
    )
    check(
        "quita duplicadas y las que ya estan en la campana",
        [c.usuario for c in reservas] == ["reserva_a", "reserva_b"],
        f"({[c.usuario for c in reservas]})",
    )
    check(
        "sin credencial de sesion no es reserva",
        all(c.usuario != "sin_sesion" for c in reservas),
    )

    con_registro = _CuentaFake("con_registro", tipo_cuenta="politica")
    sin_registro = _CuentaFake("sin_registro", tipo_cuenta="")
    solo_reg = motor._preparar_reservas(
        [con_registro, sin_registro], [], solo_con_registro=True
    )
    check(
        "solo_con_registro=True descarta las cuentas sin registro",
        [c.usuario for c in solo_reg] == ["con_registro"],
        f"({[c.usuario for c in solo_reg]})",
    )
    todos = motor._preparar_reservas(
        [con_registro, sin_registro], [], solo_con_registro=False
    )
    check(
        "sin solo_con_registro se aceptan ambas",
        len(todos) == 2,
        f"({[c.usuario for c in todos]})",
    )

    # Strings -> carga desde la BD (parcheada) con/sin '@'.
    motor._obtener_cuentas_por_rol = lambda usuarios=None, **k: [
        _CuentaFake(str(u).lstrip("@"))
        for u in (usuarios or [])
    ]
    por_nombre = motor._preparar_reservas(
        ["@res_uno", "res_dos", "res_uno"], []
    )
    check(
        "nombres con/sin '@' y deduplicados",
        [c.usuario for c in por_nombre] == ["res_uno", "res_dos"],
        f"({[c.usuario for c in por_nombre]})",
    )

    env_previo = os.environ.get("RESERVA_MAX_CUENTAS")
    try:
        os.environ["RESERVA_MAX_CUENTAS"] = "2"
        tope = motor._preparar_reservas(
            [_CuentaFake(f"r{i}") for i in range(5)], []
        )
        check(
            "tope RESERVA_MAX_CUENTAS respetado (2)",
            [c.usuario for c in tope] == ["r0", "r1"],
            f"({[c.usuario for c in tope]})",
        )
    finally:
        if env_previo is None:
            os.environ.pop("RESERVA_MAX_CUENTAS", None)
        else:
            os.environ["RESERVA_MAX_CUENTAS"] = env_previo


# --------------------------------------------------------------------------- #
# (2) _rotar_agotadas_dia
# --------------------------------------------------------------------------- #
def test_rotar_agotadas(check):
    print("(2) _rotar_agotadas_dia: sustitucion 1:1 y bloqueo de Tier 2")
    motor = MotorActivacion(max_concurrente=1)
    motor._cuotas = CuotasHorarias(
        limites={"hashtags": 5, "cita": 5, "rt": 7, "comentario": 3},
        limite_dia=1,
    )
    _preparar_cuotas_fake(motor._cuotas, ["agotada", "r_t1", "r_t2"], base_dia={"agotada": 1})

    agotada = _CuentaFake("agotada", rol_activacion="hashtags", tier_calidad="tier1")
    activa = _CuentaFake("activa", rol_activacion="rt", tier_calidad="tier2")
    r_t1 = _CuentaFake("r_t1", rol_activacion="hashtags", tier_calidad="tier1")
    r_t2 = _CuentaFake("r_t2", rol_activacion="hashtags", tier_calidad="tier2")

    activos = [agotada, activa]
    reservas = [r_t2, r_t1]
    rol_de = {"agotada": "hashtags", "activa": "rt"}
    grupos = {"cita": [], "hashtags": [agotada], "comentario": [], "rt": [activa]}
    resumen = {
        "agotadas_dia": 0, "rotadas_por_cuota_dia": 0,
        "reserva_usada": 0, "reserva_disponible": 2,
    }
    sustituciones = motor._rotar_agotadas_dia(
        activos, reservas, rol_de, False, resumen, grupos
    )
    check(
        "una sustitucion (la reserva Tier 1, no la Tier 2)",
        sustituciones == 1,
        f"({sustituciones})",
    )
    check(
        "la agotada sale y entra la reserva Tier 1",
        [c.usuario for c in activos] == ["activa", "r_t1"],
        f"({[c.usuario for c in activos]})",
    )
    check(
        "la reserva Tier 2 sigue disponible (no sustituye hashtags)",
        [c.usuario for c in reservas] == ["r_t2"],
        f"({[c.usuario for c in reservas]})",
    )
    check(
        "rol_de actualizado con la sustituta",
        rol_de == {"activa": "rt", "r_t1": "hashtags"},
        f"({rol_de})",
    )
    check(
        "grupo hashtags con la sustituta",
        [c.usuario for c in grupos["hashtags"]] == ["r_t1"],
        f"({[c.usuario for c in grupos['hashtags']]})",
    )
    check(
        "contadores: agotadas_dia=1 y rotadas=1",
        resumen["agotadas_dia"] == 1 and resumen["rotadas_por_cuota_dia"] == 1,
        f"({resumen})",
    )
    check(
        "reserva_usada=1 y reserva_disponible=1",
        resumen["reserva_usada"] == 1 and resumen["reserva_disponible"] == 1,
        f"({resumen})",
    )

    # Sin reserva compatible: la cuenta se retira y se cuenta igual.
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._cuotas = CuotasHorarias(limites={"hashtags": 5}, limite_dia=1)
    _preparar_cuotas_fake(motor2._cuotas, ["agotada2"], base_dia={"agotada2": 1})
    agotada2 = _CuentaFake(
        "agotada2", rol_activacion="hashtags", tier_calidad="tier1"
    )
    activos2 = [agotada2]
    reservas2 = [_CuentaFake("r_t2b", tier_calidad="tier2")]
    resumen2 = {
        "agotadas_dia": 0, "rotadas_por_cuota_dia": 0,
        "reserva_usada": 0, "reserva_disponible": 1,
    }
    motor2._rotar_agotadas_dia(
        activos2, reservas2, {"agotada2": "hashtags"}, False, resumen2
    )
    check(
        "sin reserva compatible: retirada y contada, sin rotacion",
        activos2 == [] and resumen2["agotadas_dia"] == 1
        and resumen2["rotadas_por_cuota_dia"] == 0,
        f"(activos={activos2}, {resumen2})",
    )

    # Sin cuotas (o sin agotadas) no cambia nada.
    motor3 = MotorActivacion(max_concurrente=1)
    activos3 = [_CuentaFake("normal")]
    resumen3 = {"reserva_disponible": 0}
    check(
        "sin cuotas no rota nada",
        motor3._rotar_agotadas_dia(activos3, [], {}, True, resumen3) == 0
        and [c.usuario for c in activos3] == ["normal"],
    )


# --------------------------------------------------------------------------- #
# (3) E2E single-pass con reserva
# --------------------------------------------------------------------------- #
def test_e2e_single_pass(check):
    print("(3) E2E repetir=False: sustitucion ANTES de encolar (mismo rol)")
    cuentas = [
        _CuentaFake("a_dia", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("b_rt", rol_activacion="rt", tier_calidad="tier2"),
    ]
    reserva = _CuentaFake(
        "reserva_hash", rol_activacion="hashtags", tier_calidad="tier1"
    )
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    with _parches(*_patches_e2e(registros, base_dia={"a_dia": 12})):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
            reserva_usuarios=[reserva],
        )
    check(
        "la agotada no ejecuta; la reserva toma su rol",
        sorted(llamadas) == [("b_rt", "rt"), ("reserva_hash", "hashtags")],
        f"({llamadas})",
    )
    check(
        "contadores de rotacion",
        resumen.get("agotadas_dia") == 1
        and resumen.get("rotadas_por_cuota_dia") == 1
        and resumen.get("reserva_usada") == 1
        and resumen.get("reserva_disponible") == 0,
        f"(agotadas={resumen.get('agotadas_dia')}, "
        f"rotadas={resumen.get('rotadas_por_cuota_dia')}, "
        f"usada={resumen.get('reserva_usada')}, "
        f"disponible={resumen.get('reserva_disponible')})",
    )
    check(
        "por_rol.hashtags.total cuenta a la sustituta",
        resumen.get("por_rol", {}).get("hashtags", {}).get("total") == 1,
        f"({resumen.get('por_rol', {}).get('hashtags')})",
    )
    check(
        "exitosas=2 y total=2 (sin contar a la agotada como fallo)",
        resumen.get("exitosas") == 2 and resumen.get("fallidas") == 0,
        f"(exitosas={resumen.get('exitosas')}, fallidas={resumen.get('fallidas')})",
    )


# --------------------------------------------------------------------------- #
# (4) E2E en rondas: la rotacion corre al iniciar cada ronda
# --------------------------------------------------------------------------- #
def test_e2e_rondas(check):
    print("(4) E2E repetir=True: la rotacion corre al iniciar cada ronda")
    cuentas = [
        _CuentaFake("a_dia_r", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("b_rt_r", rol_activacion="rt", tier_calidad="tier2"),
    ]
    reserva = _CuentaFake(
        "reserva_r", rol_activacion="hashtags", tier_calidad="tier1"
    )
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(*_patches_e2e(registros, base_dia={"a_dia_r": 12})):
            resumen = motor.ejecutar_por_roles(
                urls=["https://x.com/ancla"],
                texto_base="texto",
                hashtags="#x",
                usuarios=[c.usuario for c in cuentas],
                duracion_min=1,
                cohortes=1,
                repetir=True,
                reserva_usuarios=[reserva],
            )
    finally:
        motor_mod.time = original_time
    usuarios = {u for u, _r in llamadas}
    check(
        "en rondas la agotada tampoco ejecuta y la reserva SI",
        "a_dia_r" not in usuarios and "reserva_r" in usuarios,
        f"({sorted(usuarios)})",
    )
    check(
        "la sustitucion se cuenta UNA sola vez",
        resumen.get("agotadas_dia") == 1
        and resumen.get("rotadas_por_cuota_dia") == 1
        and resumen.get("reserva_usada") == 1,
        f"(agotadas={resumen.get('agotadas_dia')}, "
        f"rotadas={resumen.get('rotadas_por_cuota_dia')}, "
        f"usada={resumen.get('reserva_usada')})",
    )
    check(
        "la sustituta ejecuta en el grupo hashtags",
        ("reserva_r", "hashtags") in llamadas,
        f"({llamadas[:4]})",
    )


# --------------------------------------------------------------------------- #
# (5) Sin reservas nada cambia
# --------------------------------------------------------------------------- #
def test_sin_reservas(check):
    print("(5) sin reserva_usuarios: contadores en 0 y todo ejecuta igual")
    cuentas = [
        _CuentaFake("s_a", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("s_b", rol_activacion="rt", tier_calidad="tier2"),
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
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "sin tope diario agotado, las 2 cuentas ejecutan",
        sorted(u for u, _r in llamadas) == ["s_a", "s_b"],
        f"({llamadas})",
    )
    check(
        "contadores de reserva/rotacion en 0",
        resumen.get("agotadas_dia") == 0
        and resumen.get("rotadas_por_cuota_dia") == 0
        and resumen.get("reserva_usada") == 0
        and resumen.get("reserva_disponible") == 0,
        f"(agotadas={resumen.get('agotadas_dia')}, "
        f"rotadas={resumen.get('rotadas_por_cuota_dia')}, "
        f"usada={resumen.get('reserva_usada')}, "
        f"disponible={resumen.get('reserva_disponible')})",
    )
    check(
        "claves nuevas SIEMPRE presentes en el resumen",
        all(
            clave in resumen
            for clave in (
                "tier2_hashtags_omitidas",
                "rotadas_por_cuota_dia",
                "agotadas_dia",
                "reserva_usada",
                "reserva_disponible",
                "curva_aceleracion",
                "curva_fase1_min",
                "fase_actual",
                "cascada_urls",
            )
        ),
        f"({sorted(resumen)})",
    )

    # Con una agotada y SIN reservas: se retira y se cuenta, sin romper.
    motor2 = MotorActivacion(max_concurrente=1)
    llamadas2: list = []
    registros2: list = []
    _preparar_motor_roles(motor2, cuentas, llamadas2)
    with _parches(*_patches_e2e(registros2, base_dia={"s_a": 12})):
        resumen2 = motor2.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "sin reservas: la agotada se retira y se cuenta",
        resumen2.get("agotadas_dia") == 1
        and resumen2.get("rotadas_por_cuota_dia") == 0
        and all(u != "s_a" for u, _r in llamadas2),
        f"({resumen2.get('agotadas_dia')}, {llamadas2})",
    )


# --------------------------------------------------------------------------- #
# (6) Hilos/RAM: MAX_WORKERS acotado, pestaña por edad y log de rendimiento
# --------------------------------------------------------------------------- #
def test_limites_hilos_ram(check):
    print("(6) hilos/RAM: MAX_WORKERS acotado, reciclado por edad y log")
    motor = MotorActivacion(max_concurrente=1)
    env_previo = os.environ.get("MAX_WORKERS")
    try:
        os.environ["MAX_WORKERS"] = "999"
        check("MAX_WORKERS=999 se acota a 32", motor._n_workers() == 32)
        os.environ["MAX_WORKERS"] = "0"
        check("MAX_WORKERS=0 se acota a 1", motor._n_workers() == 1)
        os.environ["MAX_WORKERS"] = "7"
        check("MAX_WORKERS=7 se respeta", motor._n_workers() == 7)
        os.environ.pop("MAX_WORKERS", None)
        check(
            "sin env: default max(12, max_browsers)",
            motor._n_workers() == max(12, motor.max_concurrente),
        )
    finally:
        if env_previo is None:
            os.environ.pop("MAX_WORKERS", None)
        else:
            os.environ["MAX_WORKERS"] = env_previo
    check(
        "_ram_max_mb devuelve un int (o -1 sin resource) sin lanzar",
        isinstance(motor_mod._ram_max_mb(), int),
    )

    from activaciones.motor import _Pestana

    check("_Pestana tiene el slot 'creada'", "creada" in _Pestana.__slots__)

    class _BotFake:
        def __init__(self):
            self.cerrado = 0

        def esta_vivo(self):
            return True

        def cerrar(self):
            self.cerrado += 1

    motor2 = MotorActivacion(max_concurrente=1)
    motor2._pestana_max_minutos = 1
    bot_viejo = _BotFake()
    vieja = _Pestana(
        bot=bot_viejo,
        usuario="u_vieja",
        creada=motor_mod.time.monotonic() - 61,
    )
    motor2._pestanas.append(vieja)
    motor2._liberar_pestana(vieja, True)
    check(
        "pestaña mayor a PESTANA_MAX_MINUTOS se recicla (Chrome cerrado)",
        bot_viejo.cerrado == 1 and motor2._pestanas_recicladas == 1,
        f"(cerrado={bot_viejo.cerrado}, recicladas={motor2._pestanas_recicladas})",
    )
    check("la pestaña vieja sale del pool", motor2._pestanas == [])

    bot_joven = _BotFake()
    joven = _Pestana(
        bot=bot_joven, usuario="u_joven", creada=motor_mod.time.monotonic()
    )
    motor2._pestanas.append(joven)
    motor2._liberar_pestana(joven, True)
    check(
        "pestaña joven no se recicla (queda libre en el pool)",
        bot_joven.cerrado == 0
        and not joven.ocupada
        and joven in motor2._pestanas,
    )

    cuentas = [_CuentaFake("log_a", rol_activacion="rt", tier_calidad="tier2")]
    motor3 = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor3, cuentas, llamadas)
    logger_falso = _LoggerFalso()
    with _parches(
        (motor_mod, "logger", logger_falso),
        *_patches_e2e(registros),
    ):
        motor3.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=["log_a"],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "la campana loguea INFO de acciones/min",
        any("acciones/min" in mensaje for _n, mensaje in logger_falso.mensajes),
        f"({[m for _n, m in logger_falso.mensajes if 'acciones' in m][:2]})",
    )


# --------------------------------------------------------------------------- #
# (7) La base diaria incluye principal + reservas y claves siempre presentes
# --------------------------------------------------------------------------- #
def test_base_diaria_incluye_reservas(check):
    print("(7) la base diaria de cuotas incluye principal + reservas")
    vistos: dict = {}

    def _diaria(usuarios, minutos=None):
        vistos["usuarios"] = sorted(str(u) for u in (usuarios or []))
        return {}

    cuentas = [_CuentaFake("p1", rol_activacion="rt", tier_calidad="tier2")]
    reserva = _CuentaFake("res1", rol_activacion="rt", tier_calidad="tier2")
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    with _parches(
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", _diaria),
    ):
        motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=["p1"],
            duracion_min=1,
            cohortes=1,
            repetir=False,
            reserva_usuarios=[reserva],
        )
    check(
        "UNA consulta diaria con principal + reserva",
        vistos.get("usuarios") == ["p1", "res1"],
        f"({vistos})",
    )


def test_claves_siempre_presentes(check):
    print("(7b) resumen de una campana vacia con TODAS las claves nuevas")
    motor = MotorActivacion(max_concurrente=1)
    motor._obtener_cuentas = lambda *a, **k: []
    resumen = motor.ejecutar(urls=[], texto_base="", duracion_min=1)
    claves = (
        "tier2_hashtags_omitidas",
        "rotadas_por_cuota_dia",
        "agotadas_dia",
        "reserva_usada",
        "reserva_disponible",
        "curva_aceleracion",
        "curva_fase1_min",
        "fase_actual",
        "cascada_urls",
    )
    check(
        "campana vacia: claves nuevas SIEMPRE presentes",
        all(clave in resumen for clave in claves),
        f"(faltan {[c for c in claves if c not in resumen]})",
    )
    check(
        "campana vacia: contadores en 0/False/1 por defecto",
        resumen.get("tier2_hashtags_omitidas") == 0
        and resumen.get("rotadas_por_cuota_dia") == 0
        and resumen.get("agotadas_dia") == 0
        and resumen.get("reserva_usada") == 0
        and resumen.get("reserva_disponible") == 0
        and resumen.get("curva_aceleracion") is False
        and resumen.get("curva_fase1_min") == 0
        and resumen.get("fase_actual") == 1
        and resumen.get("cascada_urls") == 0,
        f"({ {c: resumen.get(c) for c in claves} })",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_preparar_reservas(check)
    test_rotar_agotadas(check)
    test_e2e_single_pass(check)
    test_e2e_rondas(check)
    test_sin_reservas(check)
    test_limites_hilos_ram(check)
    test_base_diaria_incluye_reservas(check)
    test_claves_siempre_presentes(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_reserva_diaria_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
