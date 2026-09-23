# -*- coding: utf-8 -*-
"""Tests de la CUOTA DIARIA en la capa del motor (`CuotasHorarias` + `preparar`).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: se
parchean `activaciones.cuotas.contar_acciones_por_usuario` y
`activaciones.cuotas.contar_acciones_dia_por_usuario` (bases sinteticas).

Cubre:
    (1) `CuotasHorarias` con tope diario 12: reservar/liberar consumen AMBAS
        capas (horaria y diaria), `agotado_dia`, `rol_permitido`/`viables`
        bloqueados para TODOS los roles, `uso_dia` y `resumen()["diarias"]`.
    (2) Base diaria inyectada desde la BD (`preparar`): 12 -> agotada de
        inmediato, 11 -> 1 cupo; y `preparar` llama UNA sola vez por consulta.
    (3) Tope desactivado (`limite_dia=0`) o apagado (`activo_dia=False`) =
        ILIMITADO (comportamiento identico al de antes de la capa diaria).
    (4) CARRERA: 30 hilos reservan/liberan con limite diario 5; exactamente 5
        exitos, sin excepciones y sin reservas en vuelo.
    (5) Defaults: sin parametros, el tope diario sale de `settings`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_cuotas_diarias_motor.py
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
from activaciones.cuotas import CuotasHorarias  # noqa: E402
from core.config import settings  # noqa: E402


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


def _preparar(cuotas, usuarios, base=None, base_dia=None):
    """`preparar` con las dos consultas a BD parcheadas (bases sinteticas)."""
    with _parches(
        (
            cuotas_mod,
            "contar_acciones_por_usuario",
            lambda *a, **k: dict(base or {}),
        ),
        (
            cuotas_mod,
            "contar_acciones_dia_por_usuario",
            lambda *a, **k: dict(base_dia or {}),
        ),
    ):
        cuotas.preparar(usuarios)


# --------------------------------------------------------------------------- #
# (1) Cuota diaria basica
# --------------------------------------------------------------------------- #
def test_diaria_basica(check):
    print("(1) cuota diaria: reservar/liberar, agotado_dia, viables y uso_dia")
    cuotas = CuotasHorarias(
        limites={"hashtags": 50, "rt": 50},
        limite_dia=12,
        minutos_dia=1440,
        activo_dia=True,
    )
    _preparar(cuotas, ["u1"])

    check(
        "resumen diario: limite 12 y ventana 1440",
        cuotas.resumen()["diarias"] == {
            "limite": 12, "ventana_min": 1440, "usado_total": 0,
        },
        f"({cuotas.resumen()['diarias']})",
    )
    check("sin uso, agotado_dia False", not cuotas.agotado_dia("u1"))
    check(
        "rol_permitido True con cupo diario",
        cuotas.rol_permitido("u1", "rt") and cuotas.rol_permitido("u1", "cita"),
    )

    reservas_ok = [cuotas.reservar("u1", "hashtags") for _ in range(12)]
    check(
        "12 reservas diarias True (base+reservas < 12 al reservar)",
        all(reservas_ok),
        f"({sum(reservas_ok)}/12)",
    )
    check(
        "13a reserva False (tope diario alcanzado)",
        not cuotas.reservar("u1", "hashtags"),
    )
    check("agotado_dia True", cuotas.agotado_dia("u1"))
    check(
        "rol_permitido False para OTRO rol (el tope no distingue rol)",
        not cuotas.rol_permitido("u1", "rt")
        and not cuotas.rol_permitido("u1", "cita")
        and not cuotas.rol_permitido("u1", "comentario"),
    )
    check(
        "viables vacio con la cuenta agotada",
        cuotas.viables("u1", ["rt", "cita", "hashtags", "comentario"]) == [],
    )
    check(
        "uso_dia usado=12 limite=12",
        cuotas.uso_dia("u1") == {"usado": 12, "limite": 12},
        f"({cuotas.uso_dia('u1')})",
    )
    check(
        "resumen usado_total=12 (reservas en vuelo incluidas)",
        cuotas.resumen()["diarias"]["usado_total"] == 12,
        f"({cuotas.resumen()['diarias']})",
    )

    # Devolver las 12 reservas devuelve el cupo diario completo.
    for _ in range(12):
        cuotas.liberar("u1", "hashtags", exito=False)
    check(
        "liberar(exito=False) devuelve cupo (agotado_dia False, usado 0)",
        not cuotas.agotado_dia("u1") and cuotas.uso_dia("u1")["usado"] == 0,
        f"({cuotas.uso_dia('u1')})",
    )

    # Un exito consume cupo diario de verdad.
    check("re-reservar tras devolver True", cuotas.reservar("u1", "hashtags"))
    cuotas.liberar("u1", "hashtags", exito=True)
    check(
        "exito consume cupo diario (usado 1)",
        cuotas.uso_dia("u1")["usado"] == 1,
        f"({cuotas.uso_dia('u1')})",
    )

    # Base diaria inyectada de la BD.
    cuotas_base = CuotasHorarias(limites={"rt": 5}, limite_dia=12)
    _preparar(cuotas_base, ["u2"], base_dia={"u2": 12})
    check(
        "base diaria 12: agotada de inmediato y no reserva",
        cuotas_base.agotado_dia("u2")
        and not cuotas_base.reservar("u2", "rt"),
    )
    _preparar(cuotas_base, ["u2"], base_dia={"u2": 11})
    check(
        "base diaria 11: deja 1 cupo (y solo 1)",
        cuotas_base.reservar("u2", "rt")
        and not cuotas_base.reservar("u2", "rt")
        and cuotas_base.agotado_dia("u2"),
    )
    check(
        "cuenta sin base diaria no se agota",
        not cuotas_base.agotado_dia("nadie"),
    )


# --------------------------------------------------------------------------- #
# (2) preparar: una consulta por capa
# --------------------------------------------------------------------------- #
def test_preparar_una_consulta(check):
    print("(2) preparar: UNA consulta agrupada por capa (horaria y diaria)")
    llamadas = {"horaria": 0, "diaria": 0}
    cuotas = CuotasHorarias(limites={"rt": 1}, limite_dia=12)

    def _horaria(usuarios, minutos=None):
        llamadas["horaria"] += 1
        return {}

    def _diaria(usuarios, minutos=None):
        llamadas["diaria"] += 1
        return {"a": 3}

    with _parches(
        (cuotas_mod, "contar_acciones_por_usuario", _horaria),
        (cuotas_mod, "contar_acciones_dia_por_usuario", _diaria),
    ):
        cuotas.preparar(["a", "b"])
        cuotas.preparar(["a", "b"])

    check(
        "contar_acciones_por_usuario se llama 1 vez por preparar",
        llamadas["horaria"] == 2,
        f"({llamadas['horaria']})",
    )
    check(
        "contar_acciones_dia_por_usuario se llama 1 vez por preparar",
        llamadas["diaria"] == 2,
        f"({llamadas['diaria']})",
    )
    check(
        "la base diaria queda cargada (3 acumuladas)",
        cuotas.uso_dia("a")["usado"] == 3,
        f"({cuotas.uso_dia('a')})",
    )


# --------------------------------------------------------------------------- #
# (3) Tope desactivado / ilimitado
# --------------------------------------------------------------------------- #
def test_tope_desactivado(check):
    print("(3) limite_dia=0 y activo_dia=False = ILIMITADO")
    ilimitado = CuotasHorarias(limites={"rt": 100}, limite_dia=0)
    _preparar(ilimitado, ["x"])
    check(
        "limite_dia=0: reservar siempre True y agotado_dia False",
        all(ilimitado.reservar("x", "rt") for _ in range(30))
        and not ilimitado.agotado_dia("x"),
    )
    check(
        "limite_dia=0: resumen diario con limite 0",
        ilimitado.resumen()["diarias"]["limite"] == 0,
        f"({ilimitado.resumen()['diarias']})",
    )

    apagado = CuotasHorarias(
        limites={"rt": 100}, limite_dia=5, activo_dia=False
    )
    _preparar(apagado, ["y"])
    check(
        "activo_dia=False: ignora el limite diario (siempre reserva)",
        all(apagado.reservar("y", "rt") for _ in range(30))
        and not apagado.agotado_dia("y"),
    )
    check(
        "activo_dia=False: resumen diario con limite 0",
        apagado.resumen()["diarias"]["limite"] == 0,
    )

    negativo = CuotasHorarias(limites={"rt": 100}, limite_dia=-7)
    _preparar(negativo, ["z"])
    check(
        "limite_dia negativo tambien es ilimitado",
        negativo.reservar("z", "rt") and not negativo.agotado_dia("z"),
    )


# --------------------------------------------------------------------------- #
# (4) Carrera de reservas con tope diario
# --------------------------------------------------------------------------- #
def test_carrera_diaria(check):
    print("(4) carrera: 30 hilos con limite diario 5 -> exactamente 5 exitos")
    cuotas = CuotasHorarias(limites={"rt": 999}, limite_dia=5)
    _preparar(cuotas, ["u_carrera"])
    barrera = threading.Barrier(30)
    errores: list = []

    def _worker():
        try:
            barrera.wait(timeout=10)
            for _ in range(50):
                if cuotas.reservar("u_carrera", "rt"):
                    cuotas.liberar("u_carrera", "rt", exito=True)
        except Exception as e:  # noqa: BLE001
            errores.append(f"{type(e).__name__}: {e}")

    hilos = [threading.Thread(target=_worker) for _ in range(30)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    check("carrera: ninguna excepcion", not errores, f"({errores[:2]})")
    check("carrera: ningun hilo colgado", all(not h.is_alive() for h in hilos))
    check(
        "carrera: exactamente 5 exitos diarios",
        cuotas.uso_dia("u_carrera")["usado"] == 5,
        f"({cuotas.uso_dia('u_carrera')})",
    )
    check(
        "carrera: agotada al final",
        cuotas.agotado_dia("u_carrera")
        and not cuotas.reservar("u_carrera", "rt"),
    )
    check(
        "carrera: sin reservas en vuelo al final",
        cuotas.resumen()["reservas_activas"] == 0,
        f"({cuotas.resumen()})",
    )


# --------------------------------------------------------------------------- #
# (5) Defaults desde settings
# --------------------------------------------------------------------------- #
def test_defaults_settings(check):
    print("(5) defaults: el tope diario sale de settings (12 / 1440 / activo)")
    cuotas = CuotasHorarias(limites={"rt": 1})
    _preparar(cuotas, ["u_default"])
    check(
        "limite diario por defecto = settings.limite_acciones_dia",
        cuotas.resumen()["diarias"]["limite"] == settings.limite_acciones_dia,
        f"({cuotas.resumen()['diarias']})",
    )
    check(
        "ventana diaria por defecto = settings.limite_dia_ventana_min",
        cuotas.resumen()["diarias"]["ventana_min"]
        == settings.limite_dia_ventana_min,
    )
    check(
        "interruptor por defecto = settings.limite_diario_activo",
        cuotas.agotado_dia("nadie") is False,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_diaria_basica(check)
    test_preparar_una_consulta(check)
    test_tope_desactivado(check)
    test_carrera_diaria(check)
    test_defaults_settings(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_cuotas_diarias_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
