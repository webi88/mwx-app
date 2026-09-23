# -*- coding: utf-8 -*-
"""Tests del BLINDAJE DE ROLES POR TIER en el motor de activaciones.

Regla de negocio: las cuentas Tier 2 (Volumen/Aged) tienen PROHIBIDO publicar
posts originales ("hashtags", incluyendo "post"/"mantenimiento"/"hilo", que
normalizan a hashtags). Nunca se desactivan en la BD: solo se omiten de la
campana y se cuentan.

Sin red, sin Chrome y SIN tocar la base real: se parchean la carga de cuentas,
`_partir_por_sesion`, `registrar_accion` y los contadores de cuotas.

Cubre:
    (1) Modo FIJO (`roles_aleatorios=False`): las cuentas Tier 2 con rol
        efectivo hashtags (incluye "post") se excluyen ANTES de abrir
        navegador; contadores `tier2_hashtags_omitidas`/`..._usuarios`.
    (2) Defensa extra de `_obtener_cuentas_por_rol(solo_roles=["hashtags"])`.
    (3) `_generar_textos_por_rol`: el grupo hashtags se genera solo para
        cuentas permitidas.
    (4) Modo ALEATORIO: 200 sorteos jamas dan hashtags a un Tier 2; `post`
        tambien bloqueado; Tier 1 si puede. Sin opciones permitidas la cuenta
        recibe "" y no abre navegador (`tier2_sin_rol`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_tiers_motor.py
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


def _patches_e2e(registros):
    """Parches comunes de un E2E del motor (sin BD, sin IA, sin Chrome)."""
    return (
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    )


def _preparar_motor(motor, cuentas, accion_fake):
    """Deja el motor listo para un E2E por roles sin red ni Chrome."""
    motor._n_workers = lambda: 2
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #tier"
            for lista in grupos.values() for c in lista
        }
    )
    motor._ejecutar_accion_rol = accion_fake
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]


# --------------------------------------------------------------------------- #
# (1) Modo fijo: Tier 2 con hashtags/post se excluye
# --------------------------------------------------------------------------- #
def test_fijo_excluye_tier2(check):
    print("(1) modo fijo: Tier 2 con hashtags/post NO entra a la campana")
    cuentas = [
        _CuentaFake("lider_post", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("aged_post", rol_activacion="hashtags", tier_calidad="tier2"),
        _CuentaFake("aged_mantenimiento", rol_activacion="post", tier_calidad="tier2"),
        _CuentaFake("aged_rt", rol_activacion="rt", tier_calidad="tier2"),
        _CuentaFake("sin_tier_cita", rol_activacion="cita", tier_calidad=""),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []

    def _accion(cuenta, rol, urls, texto, dar_like, retardo=0):
        llamadas.append((cuenta.usuario, rol))
        return (cuenta.usuario, rol, True, "ok", "https://x/post")

    _preparar_motor(motor, cuentas, _accion)
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x/ancla"],
            texto_base="texto base",
            hashtags="#tier",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )

    usuarios_llamados = {u for u, _rol in llamadas}
    check(
        "Tier 2 con hashtags NO abre navegador",
        "aged_post" not in usuarios_llamados,
        f"({sorted(usuarios_llamados)})",
    )
    check(
        "Tier 2 con rol 'post' (normaliza a hashtags) tampoco",
        "aged_mantenimiento" not in usuarios_llamados,
    )
    check(
        "Tier 1 con hashtags SI ejecuta",
        ("lider_post", "hashtags") in llamadas,
    )
    check(
        "Tier 2 con rt SI ejecuta (solo hashtags esta prohibido)",
        ("aged_rt", "rt") in llamadas,
    )
    check(
        "contador tier2_hashtags_omitidas = 2",
        resumen.get("tier2_hashtags_omitidas") == 2,
        f"({resumen.get('tier2_hashtags_omitidas')})",
    )
    check(
        "usuarios omitidos listados",
        sorted(resumen.get("tier2_hashtags_usuarios") or [])
        == ["aged_mantenimiento", "aged_post"],
        f"({resumen.get('tier2_hashtags_usuarios')})",
    )
    check(
        "por_rol.hashtags.total solo cuenta las permitidas",
        resumen.get("por_rol", {}).get("hashtags", {}).get("total") == 1,
        f"({resumen.get('por_rol', {}).get('hashtags')})",
    )
    check(
        "no se registra ningun fallo por las omitidas",
        resumen.get("fallidas") == 0,
        f"({resumen.get('fallidas')})",
    )


# --------------------------------------------------------------------------- #
# (2) Defensa extra en _obtener_cuentas_por_rol
# --------------------------------------------------------------------------- #
def test_defensa_filtro_rol(check):
    print("(2) _obtener_cuentas_por_rol(solo_roles=['hashtags']) excluye Tier 2")
    motor = MotorActivacion(max_concurrente=1)
    t1 = _CuentaFake("t1_def", rol_activacion="hashtags", tier_calidad="tier1")
    t2 = _CuentaFake("t2_def", rol_activacion="hashtags", tier_calidad="tier2")
    motor._tier2_filtradas_rol = []
    motor._obtener_cuentas = lambda **k: [t1, t2]

    resultado = motor._obtener_cuentas_por_rol(solo_roles=["hashtags"])
    check(
        "solo queda la cuenta Tier 1",
        [c.usuario for c in resultado] == ["t1_def"],
        f"({[c.usuario for c in resultado]})",
    )
    check(
        "la Tier 2 queda reportada en _tier2_filtradas_rol",
        motor._tier2_filtradas_rol == ["t2_def"],
        f"({motor._tier2_filtradas_rol})",
    )
    check(
        "la cuenta Tier 2 no se altera (sigue activa)",
        getattr(t2, "activa", True) is not False,
    )


# --------------------------------------------------------------------------- #
# (3) _generar_textos_por_rol solo para permitidas
# --------------------------------------------------------------------------- #
def test_generar_textos_filtra(check):
    print("(3) _generar_textos_por_rol: grupo hashtags solo para permitidas")
    motor = MotorActivacion(max_concurrente=1)
    t2 = _CuentaFake("t2_gen", rol_activacion="hashtags", tier_calidad="tier2")
    asignaciones = motor._generar_textos_por_rol(
        {"cita": [], "hashtags": [t2], "comentario": [], "rt": []},
        "texto base",
        hashtags="#tier",
    )
    check(
        "Tier 2 no recibe texto de hashtags",
        "t2_gen" not in asignaciones,
        f"({asignaciones})",
    )


# --------------------------------------------------------------------------- #
# (4) Modo aleatorio: 200 sorteos + sin opciones
# --------------------------------------------------------------------------- #
def test_aleatorio_200(check):
    print("(4) modo aleatorio: 200 sorteos sin hashtags para Tier 2")
    opciones = ["hashtags", "cita", "rt", "comentario"]
    tier2 = _CuentaFake("t2_sorteo", tier_calidad="tier2")
    recibidos_t2 = set()
    for _ in range(200):
        recibidos_t2.add(
            MotorActivacion._asignar_roles_aleatorios([tier2], opciones)["t2_sorteo"]
        )
    check(
        "Tier 2 NUNCA recibe hashtags en 200 sorteos",
        "hashtags" not in recibidos_t2,
        f"({sorted(recibidos_t2)})",
    )
    check(
        "Tier 2 si recibe los roles permitidos",
        recibidos_t2 <= {"cita", "rt", "comentario"} and len(recibidos_t2) >= 2,
        f"({sorted(recibidos_t2)})",
    )

    tier1 = _CuentaFake("t1_sorteo", tier_calidad="tier1")
    vistos_t1 = set()
    for _ in range(200):
        vistos_t1.add(
            MotorActivacion._asignar_roles_aleatorios([tier1], opciones)["t1_sorteo"]
        )
    check(
        "Tier 1 SI puede recibir hashtags",
        "hashtags" in vistos_t1,
        f"({sorted(vistos_t1)})",
    )

    solo_hashtags_t2 = MotorActivacion._asignar_roles_aleatorios(
        [tier2, tier1], ["hashtags"]
    )
    check(
        "con la unica opcion prohibida, Tier 2 recibe ''",
        solo_hashtags_t2["t2_sorteo"] == "",
    )
    check(
        "con la unica opcion prohibida, Tier 1 recibe hashtags",
        solo_hashtags_t2["t1_sorteo"] == "hashtags",
    )
    solo_post_t2 = MotorActivacion._asignar_roles_aleatorios(
        [tier2], ["post", "mantenimiento"]
    )
    check(
        "'post'/'mantenimiento' tambien quedan bloqueados para Tier 2",
        solo_post_t2["t2_sorteo"] == "",
    )
    check(
        "sin tier (volumen sin clasificar) SI puede hashtags",
        MotorActivacion._asignar_roles_aleatorios(
            [_CuentaFake("sin_tier")], ["hashtags"]
        )["sin_tier"] == "hashtags",
    )


def test_aleatorio_e2e_sin_rol(check):
    print("(4b) modo aleatorio E2E: Tier 2 sin rol permitido no abre nada")
    tier2 = _CuentaFake("t2_solo_hash", tier_calidad="tier2")
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor(
        motor,
        [tier2],
        lambda c, r, u, t_, d, retardo=0: (
            llamadas.append((c.usuario, r)),
            (c.usuario, r, True, "ok", "https://x/post"),
        )[1],
    )
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=[],
            hashtags="#tier",
            usuarios=["t2_solo_hash"],
            duracion_min=1,
            cohortes=1,
            repetir=False,
            roles_aleatorios=True,
        )
    check(
        "tier2_sin_rol = 1",
        resumen.get("tier2_sin_rol") == 1,
        f"({resumen.get('tier2_sin_rol')})",
    )
    check(
        "usuario listado en tier2_sin_rol_usuarios",
        resumen.get("tier2_sin_rol_usuarios") == ["t2_solo_hash"],
        f"({resumen.get('tier2_sin_rol_usuarios')})",
    )
    check(
        "no se ejecuto ninguna accion (ni navegador ni publicacion)",
        llamadas == [],
        f"({llamadas})",
    )
    check(
        "tampoco se cuenta como omitida por cuota",
        resumen.get("omitidas_por_cuota") == 0,
        f"({resumen.get('omitidas_por_cuota')})",
    )


def test_aleatorio_rondas(check):
    print("(4c) modo aleatorio en RONDAS: Tier 2 sin rol no ejecuta ni rompe")
    tier2 = _CuentaFake("t2_rondas", tier_calidad="tier2")
    motor = MotorActivacion(max_concurrente=1)
    motor._n_workers = lambda: 2
    motor._obtener_cuentas_por_rol = lambda *a, **k: [tier2]
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto"
            for lista in grupos.values() for c in lista
        }
    )
    llamadas: list = []
    motor._ejecutar_accion_rol = lambda c, r, u, t_, d, retardo=0: (
        llamadas.append((c.usuario, r)),
        (c.usuario, r, True, "ok", "https://x/post"),
    )[1]
    registros: list = []
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(*_patches_e2e(registros)):
            resumen = motor.ejecutar_por_roles(
                urls=[],
                hashtags="#tier",
                usuarios=["t2_rondas"],
                duracion_min=1,
                cohortes=1,
                repetir=True,
                roles_aleatorios=True,
            )
    finally:
        motor_mod.time = original_time

    check("rondas: Tier 2 no ejecuta nada", llamadas == [], f"({llamadas})")
    check(
        "rondas: tier2_sin_rol contado (sin duplicados)",
        resumen.get("tier2_sin_rol") == 1,
        f"({resumen.get('tier2_sin_rol')})",
    )
    check(
        "rondas: exitosas/fallidas en 0",
        resumen.get("exitosas") == 0 and resumen.get("fallidas") == 0,
        f"(exitosas={resumen.get('exitosas')}, fallidas={resumen.get('fallidas')})",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_fijo_excluye_tier2(check)
    test_defensa_filtro_rol(check)
    test_generar_textos_filtra(check)
    test_aleatorio_200(check)
    test_aleatorio_e2e_sin_rol(check)
    test_aleatorio_rondas(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_tiers_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
