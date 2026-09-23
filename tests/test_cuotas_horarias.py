# -*- coding: utf-8 -*-
"""Tests rapidos del Sistema de Cuotas Inteligente por Hora (motor).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: se
parchean `activaciones.cuotas.contar_acciones_por_usuario` (base sintetica) y
`activaciones.motor.registrar_accion` (captura en memoria). Ningun test abre
navegador ni escribe en la BD.

Cubre:
    (1) `CuotasHorarias`: base inyectada, reservar/liberar, viables/agotado,
        limite 0 = ilimitado, uso/resumen y throttle de `avisar_agotada`.
    (2) CARRERA: 40 hilos reservan/liberan con exito en paralelo; el total de
        exitos jamas supera el limite y no hay excepciones.
    (3) `_asignar_roles_aleatorios` con cuotas: el rol agotado desaparece de
        las opciones y la cuenta sin cupo recibe "".
    (4) Gate atomico de `_ejecutar_accion_rol` y `_quote_rt_una_cuenta`:
        exito consume cupo, fallo no, cuota agotada devuelve `ok=None` sin
        ejecutar la accion.
    (5) `_bucle_rondas` con reloj falso y resultados omitidos (termina rapido).
    (6) E2E corto de `ejecutar_por_roles`: `omitidas_por_cuota` en el resumen
        y registro con `tipo_registro_rol` ("post"/"cita"/"rt"/"comentario").

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_cuotas_horarias.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import sys
import threading
import time
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
from activaciones.cuotas import CuotasHorarias  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402
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
    """`preparar` con las consultas a BD parcheadas (bases sinteticas).

    Parchea la base horaria Y la diaria (capa nueva) para no tocar la BD real.
    """
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


class _CuentaFake:
    """Cuenta minima con los atributos que lee el motor (sin BD)."""

    def __init__(self, usuario, rol_activacion="", **extra):
        self.usuario = usuario
        self.auth_token = "auth_token_fake"
        self.cookies_json = ""
        self.password = ""
        self.tipo_cuenta = ""
        self.perfil_personalidad = ""
        self.personalidad = ""
        self.seccion = ""
        self.nombre_mostrado = ""
        self.rol_activacion = rol_activacion
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


def _motor_con_cuotas(limites, base=None, usuarios=None):
    """Motor con `_cuotas` ya preparado (sin BD) para los tests de gate."""
    motor = MotorActivacion(max_concurrente=1)
    cuotas = CuotasHorarias(limites=limites)
    _preparar(cuotas, usuarios or [], base)
    motor._cuotas = cuotas
    return motor, cuotas


# --------------------------------------------------------------------------- #
# (1) CuotasHorarias basico
# --------------------------------------------------------------------------- #
def test_cuotas_basicas(check):
    print("(1) CuotasHorarias: base, reservar/liberar, viables, agotado, aviso")
    cuotas = CuotasHorarias(limites={"hashtags": 2, "cita": 0})
    _preparar(cuotas, ["u1"])
    check("sin base: rol_permitido True", cuotas.rol_permitido("u1", "hashtags"))
    check("reservar 1 True", cuotas.reservar("u1", "hashtags"))
    check("reservar 2 True", cuotas.reservar("u1", "hashtags"))
    check("reservar 3 False (limite 2)", not cuotas.reservar("u1", "hashtags"))
    check(
        "rol_permitido False con base+reservas = limite",
        not cuotas.rol_permitido("u1", "hashtags"),
    )
    check(
        "uso: usado=2 limite=2",
        cuotas.uso("u1").get("hashtags") == {"usado": 2, "limite": 2},
        f"({cuotas.uso('u1').get('hashtags')})",
    )

    cuotas.liberar("u1", "hashtags", exito=True)
    check(
        "liberar(exito=True) consume cupo (usado sigue en 2)",
        not cuotas.rol_permitido("u1", "hashtags")
        and cuotas.uso("u1")["hashtags"]["usado"] == 2,
    )
    cuotas.liberar("u1", "hashtags", exito=False)
    check(
        "liberar(exito=False) devuelve el cupo (usado=1)",
        cuotas.rol_permitido("u1", "hashtags")
        and cuotas.uso("u1")["hashtags"]["usado"] == 1,
    )
    cuotas.liberar("u1", "hashtags", exito=False)
    check(
        "liberar de mas nunca deja reservas negativas",
        cuotas.resumen()["reservas_activas"] == 0,
    )

    cuotas_limite = CuotasHorarias(limites={"hashtags": 2})
    _preparar(cuotas_limite, ["u2"], base={"u2": {"hashtags": 2}})
    check(
        "base inyectada de la BD consume cupo de inmediato",
        not cuotas_limite.rol_permitido("u2", "hashtags"),
    )
    check(
        "alias 'post' normaliza al mismo cupo",
        not cuotas_limite.rol_permitido("u2", "post"),
    )
    _preparar(cuotas_limite, ["u2"], base={"u2": {"hashtags": 1}})
    check(
        "preparar es re-llamable y reemplaza la base",
        cuotas_limite.rol_permitido("u2", "hashtags")
        and cuotas_limite.uso("u2")["hashtags"]["usado"] == 1,
    )

    check(
        "limite 0 = ilimitado (siempre permitido)",
        cuotas.rol_permitido("u1", "cita"),
    )
    check(
        "limite 0 = ilimitado (reservar siempre True)",
        cuotas.reservar("u1", "cita") and cuotas.reservar("u1", "cita"),
    )

    cuotas_orden = CuotasHorarias(limites={"hashtags": 3, "cita": 1, "rt": 3})
    _preparar(cuotas_orden, ["u3"], base={"u3": {"cita": 1}})
    check(
        "viables conserva orden, normaliza y deduplica",
        cuotas_orden.viables("u3", ["post", "cita", "hashtags", "rt", "quote"])
        == ["hashtags", "rt"],
        f"({cuotas_orden.viables('u3', ['post', 'cita', 'hashtags', 'rt', 'quote'])})",
    )
    check(
        "agotado con candidatos vacios -> False",
        not cuotas_orden.agotado("u3", []),
    )
    check(
        "agotado con rol sin cupo -> False (quedan viables)",
        not cuotas_orden.agotado("u3", ["cita", "rt"]),
    )
    check(
        "agotado True cuando todos los candidatos estan al limite",
        cuotas_orden.agotado("u3", ["cita", "cita"]),
    )

    cuotas_aviso = CuotasHorarias(limites={"rt": 1}, max_aviso_seg=0)
    check("avisar_agotada con max=0 avisa siempre (1)",
          cuotas_aviso.avisar_agotada("u1"))
    check("avisar_agotada con max=0 avisa siempre (2)",
          cuotas_aviso.avisar_agotada("u1"))
    cuotas_throttle = CuotasHorarias(limites={"rt": 1}, max_aviso_seg=60)
    check("throttle: primer aviso True", cuotas_throttle.avisar_agotada("u1"))
    check("throttle: segundo aviso inmediato False",
          not cuotas_throttle.avisar_agotada("u1"))
    check("throttle es por cuenta", cuotas_throttle.avisar_agotada("u2"))

    resumen = cuotas.resumen()
    check(
        "resumen trae limites/ventana/acciones/reservas y la capa diaria",
        set(resumen) == {
            "limites", "ventana_min", "acciones_exitosas", "reservas_activas",
            "diarias",
        },
        f"({sorted(resumen)})",
    )
    check(
        "resumen: capa diaria con limite y ventana",
        resumen["diarias"].get("limite") == settings.limite_acciones_dia
        and resumen["diarias"].get("ventana_min") == settings.limite_dia_ventana_min,
        f"({resumen['diarias']})",
    )
    check("resumen: ventana 60 y exitos acumulados",
          resumen["ventana_min"] == 60 and resumen["acciones_exitosas"] == 1,
          f"({resumen})")


# --------------------------------------------------------------------------- #
# (2) Carrera de reservas
# --------------------------------------------------------------------------- #
def test_carrera(check):
    print("(2) carrera: 40 hilos reservan/liberan con limite 5")
    cuotas = CuotasHorarias(limites={"hashtags": 5})
    _preparar(cuotas, ["u_carrera"])
    barrera = threading.Barrier(40)
    errores: list = []

    def _worker():
        try:
            barrera.wait(timeout=10)
            for _ in range(50):
                if cuotas.reservar("u_carrera", "hashtags"):
                    cuotas.liberar("u_carrera", "hashtags", True)
        except Exception as e:  # noqa: BLE001
            errores.append(f"{type(e).__name__}: {e}")

    hilos = [threading.Thread(target=_worker) for _ in range(40)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join(timeout=30)

    resumen = cuotas.resumen()
    check("carrera: ninguna excepcion", not errores, f"({errores[:2]})")
    check("carrera: ningun hilo colgado", all(not h.is_alive() for h in hilos))
    check(
        "carrera: exitos <= limite (5)",
        resumen["acciones_exitosas"] <= 5,
        f"({resumen['acciones_exitosas']})",
    )
    check("carrera: hay exitos (>=1)", resumen["acciones_exitosas"] >= 1)
    check(
        "carrera: sin reservas en vuelo al final",
        resumen["reservas_activas"] == 0,
        f"({resumen})",
    )


# --------------------------------------------------------------------------- #
# (3) _asignar_roles_aleatorios con cuotas
# --------------------------------------------------------------------------- #
def test_asignar_roles(check):
    print("(3) _asignar_roles_aleatorios: filtra roles sin cupo")
    opciones = ["cita", "rt", "hashtags", "comentario"]
    cuentas = [_CuentaFake("agotada"), _CuentaFake("libre")]
    limites = {rol: 1 for rol in opciones}
    base = {"agotada": {rol: 1 for rol in opciones}}
    cuotas = CuotasHorarias(limites=limites)
    _preparar(cuotas, ["agotada", "libre"], base=base)

    roles = MotorActivacion._asignar_roles_aleatorios(
        cuentas, opciones, cuotas=cuotas
    )
    check("cuenta con todos los roles agotados recibe ''", roles["agotada"] == "")
    check(
        "cuenta libre recibe un rol valido",
        roles["libre"] in opciones,
        f"({roles['libre']})",
    )

    cuotas_parcial = CuotasHorarias(limites=limites)
    _preparar(
        cuotas_parcial,
        ["solo_rt", "libre"],
        base={"solo_rt": {"rt": 1}},
    )
    roles_parcial = MotorActivacion._asignar_roles_aleatorios(
        [_CuentaFake("solo_rt")], ["rt"], cuotas=cuotas_parcial
    )
    check(
        "rol unico sin cupo desaparece de las opciones",
        roles_parcial["solo_rt"] == "",
    )

    roles_sin = MotorActivacion._asignar_roles_aleatorios(cuentas, opciones)
    check(
        "sin cuotas el resultado NO queda vacio (comportamiento actual)",
        roles_sin["agotada"] in opciones and roles_sin["libre"] in opciones,
    )
    roles_cuotas = MotorActivacion._asignar_roles_aleatorios(
        cuentas, opciones, cuotas=cuotas
    )
    check(
        "con cuotas solo se omiten las agotadas",
        roles_cuotas["libre"] in opciones and roles_cuotas["agotada"] == "",
    )


# --------------------------------------------------------------------------- #
# (4) Gate de _ejecutar_accion_rol y _quote_rt_una_cuenta
# --------------------------------------------------------------------------- #
def test_gate_accion_rol(check):
    print("(4) gate atomico de _ejecutar_accion_rol")
    motor, cuotas = _motor_con_cuotas({"hashtags": 1, "cita": 1})
    cuenta = _CuentaFake("u_accion")
    intentos = {"n": 0}
    registros: list = []

    def _intentar(cuenta_arg, rol, urls, texto, dar_like):
        intentos["n"] += 1
        return (cuenta_arg.usuario, rol, True, "ok", "https://x/1")

    motor._intentar_accion_rol = _intentar
    with _parches(
        (motor_mod, "registrar_accion",
         lambda *a, **k: registros.append(a)),
    ):
        resultado = motor._ejecutar_accion_rol(
            cuenta, "hashtags", ["https://x/1"], "texto de prueba", False, 0
        )
        check(
            "exito: ok True y detalle 'ok'",
            resultado[2] is True and resultado[3] == "ok",
            f"({resultado})",
        )
        check(
            "exito consume cupo de hashtags",
            cuotas.uso("u_accion")["hashtags"]["usado"] == 1
            and not cuotas.rol_permitido("u_accion", "hashtags"),
        )
        resultado_agotado = motor._ejecutar_accion_rol(
            cuenta, "hashtags", ["https://x/1"], "otro texto", False, 0
        )
        check(
            "cuota agotada: ok is None con MENSAJE_CUOTA_AGOTADA",
            resultado_agotado[2] is None
            and resultado_agotado[3] == motor_mod.MENSAJE_CUOTA_AGOTADA,
            f"({resultado_agotado})",
        )
        check("cuota agotada: NO llama a _intentar_accion_rol",
              intentos["n"] == 1)
        check("cuota agotada: NO registra en la BD", registros == [])

    cuota_fallo = _CuentaFake("u_fallo")
    motor._intentar_accion_rol = lambda c, r, u, t, d: (
        c.usuario, r, False, "sin exito", "",
    )
    with _parches((motor_mod, "registrar_accion", lambda *a, **k: None)):
        resultado_fallo = motor._ejecutar_accion_rol(
            cuota_fallo, "cita", ["https://x/2"], "texto", False, 0
        )
    check("fallo: ok False", resultado_fallo[2] is False)
    check(
        "fallo no consume cupo (reserva devuelta)",
        cuotas.uso("u_fallo")["cita"]["usado"] == 0
        and cuotas.rol_permitido("u_fallo", "cita"),
    )

    motor_sin, _ = _motor_con_cuotas({"hashtags": 1})
    motor_sin._cuotas = None
    motor_sin._intentar_accion_rol = lambda c, r, u, t, d: (
        c.usuario, r, True, "ok", "",
    )
    with _parches((motor_mod, "registrar_accion", lambda *a, **k: None)):
        resultado_sin = motor_sin._ejecutar_accion_rol(
            _CuentaFake("u_sin"), "hashtags", [], "texto", False, 0
        )
    check("sin cuotas el resultado pasa igual (retrocompatible)",
          resultado_sin[2] is True)


def test_gate_quote_rt(check):
    print("(4b) gate atomico de _quote_rt_una_cuenta (rol cita)")
    motor, cuotas = _motor_con_cuotas({"cita": 1})
    cuenta = _CuentaFake("u_cita")
    llamadas = {"n": 0}

    def _intentar(cuenta_arg, urls, texto, dar_like):
        llamadas["n"] += 1
        return (cuenta_arg.usuario, True, "ok", "https://x/cita")

    motor._intentar_quote_rt = _intentar
    resultado = motor._quote_rt_una_cuenta(
        cuenta, ["https://x/1"], "texto de cita", False, 0
    )
    check("cita: exito consume cupo", resultado[1] is True
          and cuotas.uso("u_cita")["cita"]["usado"] == 1)
    resultado_agotado = motor._quote_rt_una_cuenta(
        cuenta, ["https://x/1"], "otra cita", False, 0
    )
    check(
        "cita agotada: tupla (usuario, None, ...) sin ejecutar",
        resultado_agotado[0] == "u_cita"
        and resultado_agotado[1] is None
        and resultado_agotado[2] == motor_mod.MENSAJE_CUOTA_AGOTADA,
        f"({resultado_agotado})",
    )
    check("cita agotada: NO llama a _intentar_quote_rt", llamadas["n"] == 1)

    motor_fallo, cuotas_fallo = _motor_con_cuotas({"cita": 1})
    motor_fallo._intentar_quote_rt = lambda c, u, t, d: (
        c.usuario, False, "sin exito", "",
    )
    motor_fallo._quote_rt_una_cuenta(
        _CuentaFake("u_cita_fallo"), ["https://x/1"], "x", False, 0
    )
    check(
        "cita fallida devuelve el cupo",
        cuotas_fallo.uso("u_cita_fallo")["cita"]["usado"] == 0,
    )


# --------------------------------------------------------------------------- #
# (5) _bucle_rondas con reloj falso y resultados omitidos
# --------------------------------------------------------------------------- #
def test_bucle_omitidas(check):
    print("(5) _bucle_rondas: resultados ok=None con reloj falso")
    motor = MotorActivacion(max_concurrente=1)
    motor._n_workers = lambda: 2
    procesables = [_CuentaFake("r1"), _CuentaFake("r2"), _CuentaFake("r3")]
    reportados: list = []

    def _generar(ronda, usuarios=None):
        lista = list(usuarios) if usuarios else [c.usuario for c in procesables]
        return ({u: "texto" for u in lista}, {u: "rt" for u in lista})

    def _ejecutar_uno(cuenta, texto, rol=""):
        return (cuenta.usuario, rol, None, motor_mod.MENSAJE_CUOTA_AGOTADA, "")

    def _reportar(resultado, ronda):
        reportados.append((resultado, ronda))

    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        inicio = time.monotonic()
        rondas = motor._bucle_rondas(
            procesables, 1, _generar, _ejecutar_uno, _reportar,
            porcentaje_min_ronda=40, porcentaje_max_ronda=90,
        )
        transcurrido = time.monotonic() - inicio
    finally:
        motor_mod.time = original_time

    check(
        "reloj falso: el bucle termina rapido (<10s)",
        transcurrido < 10,
        f"({transcurrido:.2f}s)",
    )
    check("reloj falso: hubo resultados reportados", bool(reportados))
    check(
        "todos los resultados reportados son omitidas (ok None)",
        all(r[0][2] is None for r in reportados),
        f"({len(reportados)} reportes)",
    )
    check("devuelve rondas >= 1", rondas >= 1, f"({rondas})")
    check(
        "sin cuotas el bucle no se rompe (retrocompatible)",
        motor_mod.time is original_time and rondas >= 1,
    )


# --------------------------------------------------------------------------- #
# (6) E2E de ejecutar_por_roles (resumen + tipo de registro)
# --------------------------------------------------------------------------- #
def _run_e2e(motor, cuentas, accion_fake, registros):
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    # Sin red: el texto real del tweet ancla no hace falta para este E2E.
    motor._obtener_anclas = lambda urls: {}
    motor._ejecutar_accion_rol = accion_fake
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #e2e"
            for lista in grupos.values() for c in lista
        }
    )
    motor._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]
    with _parches(
        (motor_mod, "_partir_por_sesion",
         lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion",
         lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario",
         lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario",
         lambda *a, **k: {}),
    ):
        return motor.ejecutar_por_roles(
            urls=["https://x/ancla"],
            texto_base="texto base",
            hashtags="#e2e",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )


def test_e2e_por_roles(check):
    print("(6) E2E ejecutar_por_roles: omitidas_por_cuota y tipo_registro_rol")
    cuentas = [
        _CuentaFake("e_post", rol_activacion="hashtags"),
        _CuentaFake("e_cita", rol_activacion="cita"),
        _CuentaFake("e_rt", rol_activacion="rt"),
        _CuentaFake("e_coment", rol_activacion="comentario"),
    ]
    registros: list = []

    def _accion_ok(cuenta, rol, urls, texto, dar_like, retardo=0):
        return (cuenta.usuario, rol, True, "ok", "https://x/e2e")

    motor = MotorActivacion(max_concurrente=1)
    resumen = _run_e2e(motor, cuentas, _accion_ok, registros)
    tipos = {r[1] for r in registros}
    check(
        "E2E: exito registra con tipo_registro_rol de cada rol",
        tipos == {"post", "cita", "rt", "comentario"},
        f"({sorted(tipos)})",
    )
    check(
        "E2E: cada exito se registra como 'exito'",
        all(r[2] == "exito" for r in registros) and len(registros) == 4,
        f"({len(registros)})",
    )
    check("E2E: resumen trae omitidas_por_cuota=0",
          resumen.get("omitidas_por_cuota") == 0)
    check(
        "E2E: resumen trae el objeto cuotas",
        isinstance(resumen.get("cuotas"), dict)
        and resumen["cuotas"].get("ventana_min") == 60
        and "hashtags" in resumen["cuotas"].get("limites", {}),
        f"({resumen.get('cuotas')})",
    )
    check("E2E: exitosas=4", resumen.get("exitosas") == 4)

    # Segunda pasada: una cuenta sin cupo devuelve ok=None.
    registros_2: list = []

    def _accion_omitida(cuenta, rol, urls, texto, dar_like, retardo=0):
        if rol == "comentario":
            return (cuenta.usuario, rol, None,
                    motor_mod.MENSAJE_CUOTA_AGOTADA, "")
        return (cuenta.usuario, rol, True, "ok", "https://x/e2e")

    motor_2 = MotorActivacion(max_concurrente=1)
    resumen_2 = _run_e2e(motor_2, cuentas, _accion_omitida, registros_2)
    check(
        "E2E: omitidas_por_cuota cuenta la ok=None",
        resumen_2.get("omitidas_por_cuota") == 1,
        f"({resumen_2.get('omitidas_por_cuota')})",
    )
    check(
        "E2E: la omitida NO se registra ni cuenta como fallo",
        len(registros_2) == 3
        and resumen_2.get("fallidas") == 0
        and resumen_2.get("exitosas") == 3,
    )
    check(
        "E2E: la omitida no aparece en detalles",
        all(d.get("ok") is not False for d in resumen_2.get("detalles", []))
        or len(resumen_2.get("detalles", [])) == 3,
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_cuotas_basicas(check)
    test_carrera(check)
    test_asignar_roles(check)
    test_gate_accion_rol(check)
    test_gate_quote_rt(check)
    test_bucle_omitidas(check)
    test_e2e_por_roles(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_cuotas_horarias.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
