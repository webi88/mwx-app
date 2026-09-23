# -*- coding: utf-8 -*-
"""Tests del PARO TOTAL (cancelacion) del motor de activaciones.

Regla: `ejecutar`/`ejecutar_por_roles` aceptan al final un `cancelar` tipo
`threading.Event` (basta `is_set()`); `MotorActivacion.solicitar_paro()` es el
fallback interno ya seteado. Con el evento puesto:
  - las acciones que no llegan a ejecutarse devuelven `ok=None` con
    `MENSAJE_CANCELADO` (OMITIDAS: no son exito ni fallo, no abren navegador,
    no reservan cuota y no registran en la BD);
  - los workers salen en segundos (jamas hasta agotar `duracion_min`);
  - `snapshot_progreso()["cancelada"]` y el resumen traen `cancelada`;
  - las pestañas persistentes se cierran en el `finally` existente.

Sin red, sin Chrome y SIN tocar la base real (fakes + reloj falso + parches,
mismo patron que `tests/test_curva_motor.py` y `test_cuotas_horarias.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_cancelacion_motor.py
"""
from __future__ import annotations

import contextlib
import os
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
from activaciones.motor import (  # noqa: E402
    MENSAJE_CANCELADO,
    MotorActivacion,
)


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


@contextlib.contextmanager
def _sin_api():
    """Desactiva `API_PRIMERO`/`RT_POR_API` (los E2E no tocan la red)."""
    claves = ("API_PRIMERO", "RT_POR_API")
    previos = {clave: os.environ.get(clave) for clave in claves}
    try:
        for clave in claves:
            os.environ[clave] = "0"
        yield
    finally:
        for clave, valor in previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor


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


class _PestanaBotFake:
    """Bot fake de una pestaña persistente: cuenta acciones, setea el paro y cierra."""

    def __init__(self, evento=None, limite=2):
        self.evento = evento
        self.limite = int(limite or 0)
        self.acciones = 0
        self.cerrado = False
        self.usuario = ""
        self.ultima_url_publicada = ""
        self.cuenta_suspendida = False
        self.ultimo_error = ""
        self.marca_paro = None

    def preparar_sesion_cdp(self):
        return True

    def login_con_cookies(self):
        return True

    def cambiar_cuenta(self, usuario, validar_proxy=False):
        self.usuario = str(usuario or "")
        return True

    def esta_vivo(self):
        return not self.cerrado

    def calentar(self):
        return True

    def publicar_tweet(self, texto, buscar_url=False):
        self.acciones += 1
        if self.evento is not None and self.acciones >= self.limite:
            try:
                self.marca_paro = motor_mod.time.monotonic()
            except Exception:
                self.marca_paro = None
            self.evento.set()
        return True

    def cerrar(self):
        self.cerrado = True


def _patches_e2e(registros):
    """Parches comunes de un E2E del motor (sin BD, sin IA, sin Chrome)."""
    return (
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    )


def _preparar_motor_roles(motor, cuentas, llamadas):
    """Patches del E2E por roles (sin navegador) usado por la no-regresion."""
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
# (1) Evento ya seteado antes de lanzar
# --------------------------------------------------------------------------- #
def test_evento_seteado_antes(check):
    print("(1) cancelar ya seteado: 0 consultas, 0 acciones y cancelada=True")
    check(
        "MENSAJE_CANCELADO exacto",
        MENSAJE_CANCELADO == "campaña cancelada por el usuario",
        f"({MENSAJE_CANCELADO!r})",
    )
    evento = threading.Event()
    evento.set()
    logger = _LoggerFalso()
    consultas = {"n": 0}
    pestanas = {"n": 0}

    motor = MotorActivacion(max_concurrente=1)
    motor._obtener_cuentas_por_rol = (
        lambda *a, **k: consultas.__setitem__("n", consultas["n"] + 1) or []
    )
    motor._adquirir_pestana = (
        lambda cuenta: pestanas.__setitem__("n", pestanas["n"] + 1) or None
    )
    with _parches((motor_mod, "logger", logger)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x/ancla"], usuarios=["u1"], duracion_min=1,
            cancelar=evento,
        )
    check(
        "roles: cancelada=True con total=0 y detalles vacios",
        resumen.get("cancelada") is True
        and resumen.get("total") == 0
        and resumen.get("detalles") == [],
        f"({resumen.get('cancelada')}, total={resumen.get('total')})",
    )
    check("roles: NO consulta cuentas (salida temprana)", consultas["n"] == 0)
    check("roles: 0 pestañas/navegadores creados", pestanas["n"] == 0)
    check(
        "roles: INFO del paro UNA vez y sin logger.error",
        sum(
            1 for nivel, mensaje in logger.mensajes
            if nivel == "info" and "paro total solicitado; cerrando" in mensaje
        ) == 1
        and not any(nivel == "error" for nivel, _m in logger.mensajes),
        f"({logger.mensajes[:4]})",
    )

    consultas2 = {"n": 0}
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._obtener_cuentas = (
        lambda *a, **k: consultas2.__setitem__("n", consultas2["n"] + 1) or []
    )
    with _parches((motor_mod, "logger", _LoggerFalso())):
        resumen2 = motor2.ejecutar(
            urls=["https://x/ancla"], texto_base="texto", duracion_min=1,
            cancelar=evento,
        )
    check(
        "cita masiva: cancelada=True con total=0 y rondas=0",
        resumen2.get("cancelada") is True
        and resumen2.get("total") == 0
        and resumen2.get("rondas") == 0,
        f"({resumen2.get('cancelada')}, total={resumen2.get('total')})",
    )
    check("cita masiva: NO consulta cuentas", consultas2["n"] == 0)


# --------------------------------------------------------------------------- #
# (2) Paro a mitad de rondas (pestaña fake + cuotas reales)
# --------------------------------------------------------------------------- #
def test_cancelacion_a_mitad(check):
    print("(2) paro a mitad de rondas: corta en <=2 acciones y limpia todo")
    evento = threading.Event()
    bot = _PestanaBotFake(evento=evento, limite=2)
    pestana = motor_mod._Pestana(bot=bot, usuario="", ocupada=True)
    motor = MotorActivacion(max_concurrente=1)
    motor._pestanas.append(pestana)
    motor._adquirir_pestana = lambda cuenta: pestana
    motor._n_workers = lambda: 1
    cuentas = [
        _CuentaFake("k1", rol_activacion="hashtags"),
        _CuentaFake("k2", rol_activacion="hashtags"),
        _CuentaFake("k3", rol_activacion="hashtags"),
    ]
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #k"
            for lista in grupos.values() for c in lista
        }
    )
    registros: list = []
    logger = _LoggerFalso()
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _sin_api(), _parches(
            (motor_mod, "logger", logger),
            *_patches_e2e(registros),
        ):
            resumen = motor.ejecutar_por_roles(
                urls=["https://x/ancla"], texto_base="tema", hashtags="#k",
                usuarios=[c.usuario for c in cuentas], duracion_min=5,
                cohortes=1, repetir=True, cancelar=evento,
            )
        t_fin = motor_mod.time.monotonic()
    finally:
        motor_mod.time = original_time

    check(
        "resumen cancelada=True y 2 exitosas (las de la cancelacion)",
        resumen.get("cancelada") is True and resumen.get("exitosas") == 2,
        f"(cancelada={resumen.get('cancelada')}, exitosas={resumen.get('exitosas')})",
    )
    check(
        "no hay acciones posteriores a la cancelacion",
        bot.acciones == 2,
        f"(acciones={bot.acciones})",
    )
    check(
        "el bucle termino en <=5s simulados (no agota duracion_min=5)",
        bot.marca_paro is not None and (t_fin - bot.marca_paro) <= 5.0,
        f"(delta={None if bot.marca_paro is None else t_fin - bot.marca_paro})",
    )
    check("la pestaña fake se cerro en el finally", bot.cerrado is True)
    check(
        "cuotas sin reservas en vuelo (sin fugas)",
        motor._cuotas is not None
        and motor._cuotas.resumen()["reservas_activas"] == 0,
        f"({None if motor._cuotas is None else motor._cuotas.resumen()})",
    )
    check(
        "cuotas: 2 cupos consumidos (uno por cada accion confirmada)",
        motor._cuotas.resumen()["acciones_exitosas"] == 2
        and sum(
            motor._cuotas.uso(u)["hashtags"]["usado"]
            for u in ("k1", "k2", "k3")
        ) == 2,
        f"({motor._cuotas.resumen()}, "
        f"{[motor._cuotas.uso(u)['hashtags']['usado'] for u in ('k1', 'k2', 'k3')]})",
    )
    check(
        "el registro no guarda el paro como error",
        not any(nivel == "error" for nivel, _m in logger.mensajes),
        f"({[m for n, m in logger.mensajes if n == 'error'][:2]})",
    )


# --------------------------------------------------------------------------- #
# (3) Retardo de cohorte interrumpido
# --------------------------------------------------------------------------- #
def test_retardo_cohorte(check):
    print("(3) retardo de cohorte: el paro lo interrumpe y la accion NO se ejecuta")

    # Entrada con paro ya puesto: ok=None sin llamar al intento ni tocar cupos.
    evento = threading.Event()
    evento.set()
    motor = MotorActivacion(max_concurrente=1)
    motor._cancelar = evento
    intentos = {"n": 0}
    motor._intentar_quote_rt = lambda *a, **k: (
        intentos.__setitem__("n", intentos["n"] + 1),
        ("r0", True, "ok", ""),
    )[1]
    cuota = CuotasHorarias(limites={"cita": 1})
    with _parches(
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    ):
        cuota.preparar(["r0"])
    motor._cuotas = cuota
    resultado0 = motor._quote_rt_una_cuenta(
        _CuentaFake("r0"), ["https://x/a"], "texto", False, 0
    )
    check(
        "entrada cancelada: ok=None + MENSAJE_CANCELADO",
        resultado0[1] is None and resultado0[2] == MENSAJE_CANCELADO,
        f"({resultado0})",
    )
    check("entrada cancelada: NO llama a _intentar_quote_rt", intentos["n"] == 0)
    check(
        "entrada cancelada: NO reserva cuota (sin fugas)",
        cuota.resumen()["reservas_activas"] == 0
        and cuota.uso("r0")["cita"]["usado"] == 0,
        f"({cuota.resumen()})",
    )

    # Retardo largo interrumpido a mitad: la accion jamas se ejecuta.
    evento2 = threading.Event()
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._cancelar = evento2
    ejecutadas = {"n": 0}
    motor2._intentar_quote_rt = lambda *a, **k: (
        ejecutadas.__setitem__("n", ejecutadas["n"] + 1),
        ("r1", True, "ok", ""),
    )[1]
    resultado: dict = {}

    def _lanzar():
        resultado["r"] = motor2._quote_rt_una_cuenta(
            _CuentaFake("r1"), ["https://x/a"], "texto", False, 600
        )

    hilo = threading.Thread(target=_lanzar)
    inicio = time.monotonic()
    hilo.start()
    time.sleep(0.3)
    evento2.set()
    hilo.join(timeout=10)
    transcurrido = time.monotonic() - inicio
    check(
        "retardo de 600s interrumpido por el paro (no ejecuta)",
        ejecutadas["n"] == 0
        and not hilo.is_alive()
        and resultado.get("r", (None, None, None, None))[1] is None,
        f"(ejecutadas={ejecutadas['n']}, r={resultado.get('r')})",
    )
    check(
        "el worker sale en segundos, no en 600s",
        transcurrido < 10,
        f"({transcurrido:.2f}s)",
    )
    check(
        "detalle exacto del retardo cancelado",
        resultado.get("r", (None, None, "", ""))[2] == MENSAJE_CANCELADO,
        f"({resultado.get('r')})",
    )


# --------------------------------------------------------------------------- #
# (4) solicitar_paro() sin kwarg
# --------------------------------------------------------------------------- #
def test_solicitar_paro(check):
    print("(4) solicitar_paro(): evento interno y campañas siguientes lo respetan")
    logger = _LoggerFalso()
    motor = MotorActivacion(max_concurrente=1)
    check(
        "sin paro: snapshot cancelada=False",
        motor.snapshot_progreso().get("cancelada") is False,
    )
    with _parches((motor_mod, "logger", logger)):
        motor.solicitar_paro()
    evento = motor._cancelar
    check(
        "crea un Event interno YA SETEADO",
        isinstance(evento, threading.Event) and evento.is_set()
        and motor._cancelado() is True,
    )
    with _parches((motor_mod, "logger", logger)):
        motor.solicitar_paro()
    check(
        "idempotente: reusa el MISMO evento y no repite el INFO",
        motor._cancelar is evento
        and sum(
            1 for nivel, mensaje in logger.mensajes
            if nivel == "info" and "paro total solicitado; cerrando" in mensaje
        ) == 1,
        f"({logger.mensajes})",
    )
    check(
        "snapshot cancelada=True tras el paro",
        motor.snapshot_progreso().get("cancelada") is True,
    )

    consultas = {"n": 0}
    motor._obtener_cuentas_por_rol = (
        lambda *a, **k: consultas.__setitem__("n", consultas["n"] + 1) or []
    )
    resumen = motor.ejecutar_por_roles(
        urls=["https://x/ancla"], usuarios=["u1"], duracion_min=1
    )
    check(
        "campaña SIN kwarg respeta el evento interno (cancelada, 0 consultas)",
        resumen.get("cancelada") is True and consultas["n"] == 0,
        f"(cancelada={resumen.get('cancelada')}, consultas={consultas['n']})",
    )

    motor2 = MotorActivacion(max_concurrente=1)
    motor2.solicitar_paro()
    resumen2 = motor2.ejecutar(
        urls=["https://x/ancla"], texto_base="texto", duracion_min=1
    )
    check(
        "ejecutar() tambien respeta el evento interno",
        resumen2.get("cancelada") is True and resumen2.get("total") == 0,
        f"(cancelada={resumen2.get('cancelada')})",
    )

    # Objeto raro en `_cancelar`: `solicitar_paro` no lanza y lo reemplaza.
    motor3 = MotorActivacion(max_concurrente=1)
    motor3._cancelar = "no-es-un-evento"
    motor3.solicitar_paro()
    check(
        "un `_cancelar` invalido se reemplaza sin lanzar",
        isinstance(motor3._cancelar, threading.Event)
        and motor3._cancelar.is_set()
        and motor3._cancelado() is True,
    )


# --------------------------------------------------------------------------- #
# (5) Resumen/snapshot siempre con `cancelada` + omitidas por cancelacion
# --------------------------------------------------------------------------- #
def test_claves_cancelada(check):
    print("(5) `cancelada` SIEMPRE presente y `omitidas_por_cancelacion`")
    normal = MotorActivacion(max_concurrente=1)
    resumen_normal = normal._con_claves_pestana({"exitosas": 1})
    check(
        "campaña normal: cancelada=False y omitidas_por_cancelacion=0",
        resumen_normal.get("cancelada") is False
        and resumen_normal.get("omitidas_por_cancelacion") == 0,
        f"(cancelada={resumen_normal.get('cancelada')})",
    )
    check(
        "campo modo_pestana sigue presente (no regresion)",
        "modo_pestana" in resumen_normal
        and "pestanas_creadas" in resumen_normal,
    )

    cancelado = MotorActivacion(max_concurrente=1)
    cancelado.solicitar_paro()
    logger = _LoggerFalso()
    with _parches((motor_mod, "logger", logger)):
        resumen_cancelado = cancelado._con_claves_pestana({
            "exitosas": 3, "fallidas": 1, "omitidas_por_cuota": 2,
        })
    check(
        "campaña cancelada: cancelada=True (aunque el resumen no la traiga)",
        resumen_cancelado.get("cancelada") is True,
    )
    check(
        "resumen final claro con exitosas/fallidas/omitidas",
        any(
            nivel == "info"
            and "campaña cancelada por el usuario" in mensaje
            and "3 exitosas" in mensaje and "1 fallidas" in mensaje
            and "2 omitidas" in mensaje
            for nivel, mensaje in logger.mensajes
        ),
        f"({logger.mensajes})",
    )
    check(
        "sin logger.error por la cancelacion",
        not any(nivel == "error" for nivel, _m in logger.mensajes),
    )

    motor = MotorActivacion(max_concurrente=1)
    motor._cancelar = threading.Event()
    check(
        "evento NO seteado: `_cancelado()` False y snapshot False",
        motor._cancelado() is False
        and motor.snapshot_progreso().get("cancelada") is False,
    )
    check(
        "`_dormir_cancelable` completo sin paro",
        motor._dormir_cancelable(0.01, tramo=0.01) is True,
    )
    motor._cancelar.set()
    check(
        "`_dormir_cancelable` corta con paro",
        motor._dormir_cancelable(5.0, tramo=0.01) is False,
    )

    # Gate de navegadores y pool de pestañas cortan con el paro (sin esperar).
    motor_gate = MotorActivacion(max_concurrente=1)
    motor_gate._navegadores_activos = 1  # cupo lleno: tocaria esperar
    motor_gate.solicitar_paro()
    antes = time.monotonic()
    liberado = motor_gate._adquirir_navegador()
    check(
        "gate de navegadores: False casi al instante con paro",
        liberado is False and (time.monotonic() - antes) < 2,
        f"(liberado={liberado})",
    )
    motor_pool = MotorActivacion(max_concurrente=1)
    motor_pool._pestana_capaz = lambda: True
    motor_pool.solicitar_paro()
    antes = time.monotonic()
    pestana_nula = motor_pool._adquirir_pestana(_CuentaFake("p1"))
    check(
        "pool de pestañas: None casi al instante con paro (no espera 180s)",
        pestana_nula is None and (time.monotonic() - antes) < 3,
        f"(pestana={pestana_nula})",
    )


# --------------------------------------------------------------------------- #
# (6) No regresion: sin `cancelar` (None) todo funciona igual
# --------------------------------------------------------------------------- #
def test_sin_cancelar_no_regresion(check):
    print("(6) sin cancelar (None): campaña normal con curva y reservas")
    cuentas = [
        _CuentaFake("n_t1", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("n_t2", rol_activacion="rt", tier_calidad="tier2"),
        _CuentaFake("n_sin", rol_activacion="cita", tier_calidad=""),
    ]
    reserva = _CuentaFake("n_reserva", rol_activacion="rt")
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_motor_roles(motor, cuentas, llamadas)
    motor._registrar_tiers(list(cuentas) + [reserva])
    with _sin_api(), _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=3,
            cohortes=1,
            repetir=False,
            curva_aceleracion=True,
            curva_fase1_min=1,
            reserva_usuarios=[reserva],
        )
    check(
        "sin cancelar: las 3 cuentas ejecutan y cancelada=False",
        sorted(u for u, _r in llamadas) == ["n_sin", "n_t1", "n_t2"]
        and resumen.get("cancelada") is False,
        f"({[u for u, _r in llamadas]}, cancelada={resumen.get('cancelada')})",
    )
    check(
        "sin cancelar: curva y reservas intactas",
        resumen.get("curva_aceleracion") is True
        and resumen.get("fase_actual") == 2
        and resumen.get("reserva_disponible") == 1,
        f"(curva={resumen.get('curva_aceleracion')}, "
        f"fase={resumen.get('fase_actual')}, "
        f"reserva={resumen.get('reserva_disponible')})",
    )
    check(
        "sin cancelar: omitidas_por_cancelacion=0 y exitosas=3",
        resumen.get("omitidas_por_cancelacion") == 0
        and resumen.get("exitosas") == 3,
        f"(omitidas={resumen.get('omitidas_por_cancelacion')}, "
        f"exitosas={resumen.get('exitosas')})",
    )

    # `ejecutar` (cita masiva) sin cancelar sigue funcionando igual.
    motor2 = MotorActivacion(max_concurrente=1)
    llamadas2: list = []
    registros2: list = []
    motor2._obtener_cuentas = lambda *a, **k: list(cuentas[:2])
    motor2._asignar_variaciones_cita = (
        lambda cs, *a, **k: {c.usuario: "texto" for c in cs}
    )
    motor2._quote_rt_una_cuenta = (
        lambda c, urls, texto, dar_like, retardo=0: (
            llamadas2.append(c.usuario),
            (c.usuario, True, "ok", "https://x.com/t1/status/1"),
        )[1]
    )
    motor2._distribuir_cohortes = lambda cs, duracion, cohortes: [list(cs)]
    with _sin_api(), _parches(*_patches_e2e(registros2)):
        resumen2 = motor2.ejecutar(
            urls=["https://x/ancla"], texto_base="texto", duracion_min=3,
            cohortes=1, repetir=False,
        )
    check(
        "sin cancelar: cita masiva normal",
        len(llamadas2) == 2 and resumen2.get("cancelada") is False
        and resumen2.get("exitosas") == 2,
        f"({llamadas2}, cancelada={resumen2.get('cancelada')})",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_evento_seteado_antes(check)
    test_cancelacion_a_mitad(check)
    test_retardo_cohorte(check)
    test_solicitar_paro(check)
    test_claves_cancelada(check)
    test_sin_cancelar_no_regresion(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_cancelacion_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
