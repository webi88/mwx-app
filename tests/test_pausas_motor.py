# -*- coding: utf-8 -*-
"""Pausas para activacion en el MOTOR y el SCHEDULER (sin red ni Chrome).

REGLA: `Cuenta.pausada_activacion=True` = cuentas entregadas a CLIENTES que
quedan FUERA de toda activacion masiva, pero SIGUEN vivas en mantenimiento.

Cubre:
    (1) E2E `ejecutar_por_roles`: 5 cuentas (3 pausadas, una Tier 3) -> solo 2
        ejecutan; `pausadas_omitidas`/`pausadas_usuarios`, sin fallos, sin
        `sin_sesion`/`sin_rol`, sin omisiones de cuota/tier por las pausadas y
        log INFO una vez por campana.
    (2) Seleccion manual (usuarios explicitos) que incluye pausadas -> omitidas
        igual; si TODAS estan pausadas, el resumen lo reporta sin ejecutar.
    (3) E2E cita masiva (`ejecutar`) con reservas pausadas: excluidas y
        contadas.
    (4) E2E 3+3+3: las pausadas no reciben slots.
    (5) `_bloqueada_por_pausa`: tabla activacion vs mantenimiento + tolerancia.
    (6) `scheduler.ejecutor`: tarea `rt` en pausada -> bloqueada sin Chrome y
        sin reintento; tarea `post` en pausada -> se ejecuta.
    (7) Calentamiento continuo: las pausadas SIGUEN siendo elegibles.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_pausas_motor.py
"""
from __future__ import annotations

import contextlib
import json
import sys
import types
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
import scheduler.calentamiento as calentamiento  # noqa: E402
import scheduler.ejecutor as ejecutor_mod  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402
from core.models import Cuenta as CuentaModel  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers / fakes (sin BD, sin Chrome)
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
    """Cuenta minima con los atributos que leen motor/scheduler."""

    def __init__(self, usuario, rol_activacion="", tier_calidad="",
                 pausada=False, **extra):
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
        self.pausada_activacion = pausada
        self.plataforma = "twitter"
        for clave, valor in extra.items():
            setattr(self, clave, valor)


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


def _preparar_roles(motor, cuentas, llamadas):
    """Deja `ejecutar_por_roles` listo para correr con fakes."""
    motor._n_workers = lambda: 2
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = lambda grupos, *a, **k: {
        c.usuario: "texto #x"
        for lista in grupos.values() for c in lista
    }
    motor._distribuir_cohortes = lambda cs, d, c: [list(cs)]
    motor._dormir_cancelable = lambda *a, **k: True

    def _accion(cuenta, rol, urls, texto, dar_like, retardo=0):
        llamadas.append((cuenta.usuario, rol))
        return (cuenta.usuario, rol, True, "ok", "https://x.com/post")

    motor._ejecutar_accion_rol = _accion


# --------------------------------------------------------------------------- #
# (1) E2E por roles: 5 cuentas, 3 pausadas -> solo 2 ejecutan
# --------------------------------------------------------------------------- #
def test_e2e_roles_pausadas(check):
    print("(1) ejecutar_por_roles: las pausadas quedan fuera (sin fallo)")
    cuentas = [
        _CuentaFake("act_post", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("act_rt", rol_activacion="rt", tier_calidad="tier2"),
        _CuentaFake("paus_post", rol_activacion="hashtags", tier_calidad="tier1",
                    pausada=True),
        _CuentaFake("paus_cita", rol_activacion="cita", tier_calidad="",
                    pausada=True),
        # Pausada ADEMAS Tier 3 con hashtags: la pausa manda y NO debe salir
        # en `tier3_omitidas` (no interfiere el blindaje de tier).
        _CuentaFake("paus_t3", rol_activacion="hashtags", tier_calidad="tier3",
                    pausada=True),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    logger_falso = _LoggerFalso()
    _preparar_roles(motor, cuentas, llamadas)
    with _parches(
        (motor_mod, "logger", logger_falso),
        *_patches_e2e(registros),
    ):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto base",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    ejecutados = sorted({u for u, _r in llamadas})
    check(
        "solo las 2 NO pausadas ejecutan",
        ejecutados == ["act_post", "act_rt"],
        f"({ejecutados})",
    )
    check(
        "pausadas_omitidas = 3",
        resumen.get("pausadas_omitidas") == 3,
        f"({resumen.get('pausadas_omitidas')})",
    )
    check(
        "pausadas_usuarios completo",
        sorted(resumen.get("pausadas_usuarios") or [])
        == ["paus_cita", "paus_post", "paus_t3"],
        f"({resumen.get('pausadas_usuarios')})",
    )
    check(
        "las pausadas NO cuentan como fallidas",
        resumen.get("fallidas") == 0 and resumen.get("exitosas") == 2,
        f"(fallidas={resumen.get('fallidas')}, exitosas={resumen.get('exitosas')})",
    )
    check(
        "las pausadas NO aparecen en sin_sesion ni sin_rol",
        resumen.get("sin_sesion") == 0
        and resumen.get("sin_rol") == 0
        and not any("paus_" in str(u) for u in resumen.get("sin_sesion_usuarios") or [])
        and not any("paus_" in str(u) for u in resumen.get("sin_rol_usuarios") or []),
        f"(sin_sesion={resumen.get('sin_sesion')}, sin_rol={resumen.get('sin_rol')})",
    )
    check(
        "las pausadas NO entran a detalles ni a omitidas por cuota",
        not any("paus_" in str(d.get("usuario")) for d in resumen.get("detalles") or [])
        and resumen.get("omitidas_por_cuota") == 0,
        f"(omitidas_cuota={resumen.get('omitidas_por_cuota')})",
    )
    check(
        "la pausada Tier 3 no se reporta como tier3_omitida (pausa primero)",
        resumen.get("tier3_omitidas") == 0
        and "paus_t3" not in (resumen.get("tier3_omitidas_usuarios") or []),
        f"(tier3={resumen.get('tier3_omitidas')}, "
        f"usuarios={resumen.get('tier3_omitidas_usuarios')})",
    )
    check(
        "log INFO una vez con el mensaje del contrato",
        any(
            nivel == "info"
            and "3 cuenta(s) pausada(s) para activación quedaron fuera "
                "(siguen en mantenimiento)" in mensaje
            for nivel, mensaje in logger_falso.mensajes
        ),
        f"({[m for _n, m in logger_falso.mensajes if 'pausada' in m][:1]})",
    )
    check(
        "no se registro ninguna accion de las pausadas",
        not any(str(a[0]).startswith("paus_") for a in registros if a),
        f"({registros})",
    )


# --------------------------------------------------------------------------- #
# (2) Seleccion manual / todas pausadas
# --------------------------------------------------------------------------- #
def test_seleccion_manual_y_todas_pausadas(check):
    print("(2) seleccion manual con pausadas y caso 'todas pausadas'")
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    pausada = _CuentaFake("manual_pausada", rol_activacion="hashtags",
                          pausada=True)
    activa = _CuentaFake("manual_activa", rol_activacion="hashtags")
    _preparar_roles(motor, [pausada, activa], llamadas)
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=["manual_pausada", "manual_activa"],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "manual: la pausada se omite aunque venga en `usuarios`",
        [u for u, _r in llamadas] == ["manual_activa"],
        f"({llamadas})",
    )
    check(
        "manual: contador y lista reportan la pausada",
        resumen.get("pausadas_omitidas") == 1
        and resumen.get("pausadas_usuarios") == ["manual_pausada"],
        f"({resumen.get('pausadas_usuarios')})",
    )

    # TODAS pausadas: la campana no ejecuta nada y aun asi lo reporta.
    motor2 = MotorActivacion(max_concurrente=1)
    llamadas2: list = []
    registros2: list = []
    solo_pausadas = [
        _CuentaFake("p1", rol_activacion="hashtags", pausada=True),
        _CuentaFake("p2", rol_activacion="rt", pausada=True),
    ]
    _preparar_roles(motor2, solo_pausadas, llamadas2)
    with _parches(*_patches_e2e(registros2)):
        resumen2 = motor2.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            hashtags="#x",
            usuarios=["p1", "p2"],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "todas pausadas: 0 ejecuciones y 0 fallos",
        llamadas2 == []
        and resumen2.get("exitosas") == 0
        and resumen2.get("fallidas") == 0,
        f"(llamadas={llamadas2}, resumen={resumen2.get('fallidas')})",
    )
    check(
        "todas pausadas: contadores presentes (salida temprana)",
        resumen2.get("pausadas_omitidas") == 2
        and sorted(resumen2.get("pausadas_usuarios") or []) == ["p1", "p2"],
        f"(omitidas={resumen2.get('pausadas_omitidas')}, "
        f"usuarios={resumen2.get('pausadas_usuarios')})",
    )


# --------------------------------------------------------------------------- #
# (3) E2E cita masiva (ejecutar) + reserva pausada
# --------------------------------------------------------------------------- #
def test_e2e_cita_masiva_y_reserva(check):
    print("(3) cita masiva: pausadas y reservas pausadas fuera")
    cuentas = [
        _CuentaFake("c_act1", tier_calidad="tier1"),
        _CuentaFake("c_act2", tier_calidad=""),
        _CuentaFake("c_paus1", tier_calidad="tier1", pausada=True),
        _CuentaFake("c_paus2", tier_calidad="", pausada=True),
    ]
    reserva_pausada = _CuentaFake("r_paus", tier_calidad="", pausada=True)
    reserva_activa = _CuentaFake("r_act", tier_calidad="")
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    motor._obtener_cuentas = lambda *a, **k: list(cuentas)
    motor._asignar_variaciones_cita = (
        lambda cs, *a, **k: {c.usuario: "texto" for c in cs}
    )
    motor._distribuir_cohortes = lambda cs, d, c: [list(cs)]

    def _quote(cuenta, urls, texto, dar_like, retardo=0):
        llamadas.append(cuenta.usuario)
        return (cuenta.usuario, True, "ok", "https://x.com/c/status/1")

    motor._quote_rt_una_cuenta = _quote
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            duracion_min=1,
            cohortes=1,
            repetir=False,
            reserva_usuarios=[reserva_pausada, reserva_activa],
        )
    check(
        "cita masiva: solo las no pausadas ejecutan",
        sorted(llamadas) == ["c_act1", "c_act2"],
        f"({llamadas})",
    )
    check(
        "cita masiva: pausadas de la campana + reserva pausada contadas",
        resumen.get("pausadas_omitidas") == 3
        and sorted(resumen.get("pausadas_usuarios") or [])
        == ["c_paus1", "c_paus2", "r_paus"],
        f"(omitidas={resumen.get('pausadas_omitidas')}, "
        f"usuarios={resumen.get('pausadas_usuarios')})",
    )
    check(
        "cita masiva: la reserva pausada no queda disponible",
        resumen.get("reserva_disponible") == 1
        and resumen.get("reserva_usada") == 0,
        f"(disponible={resumen.get('reserva_disponible')})",
    )
    check(
        "cita masiva: sin fallos por las pausadas",
        resumen.get("fallidas") == 0 and resumen.get("exitosas") == 2,
        f"(fallidas={resumen.get('fallidas')})",
    )


# --------------------------------------------------------------------------- #
# (4) E2E 3+3+3
# --------------------------------------------------------------------------- #
def test_e2e_3_3_3(check):
    print("(4) campana 3+3+3: las pausadas no reciben slots")
    cuentas = [
        _CuentaFake("t_act", tier_calidad="tier1"),
        _CuentaFake("t_paus", tier_calidad="tier1", pausada=True),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    motor._obtener_cuentas = lambda *a, **k: list(cuentas)
    motor._distribuir_cohortes = lambda cs, d, c: [list(cs)]

    def _una_cuenta(cuenta, acciones, retardo=0, dar_like=False):
        llamadas.append(cuenta.usuario)
        return [
            (cuenta.usuario, rol, True, "ok", "")
            for rol, _urls, _texto in (acciones or [])
        ]

    motor._ejecutar_campana_una_cuenta = _una_cuenta
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_campana_3_3_3(
            urls_rt=["https://x.com/ancla"],
            textos_posts_por_cuenta={
                "t_act": ["post 1", "post 2", "post 3"],
                "t_paus": ["post 1", "post 2", "post 3"],
            },
            usuarios=["t_act", "t_paus"],
            duracion_min=1,
            cohortes=1,
        )
    check(
        "3+3+3: la pausada no ejecuta slots",
        llamadas == ["t_act"],
        f"({llamadas})",
    )
    check(
        "3+3+3: totales calculados sin la pausada",
        resumen.get("total_cuentas") == 1
        and resumen.get("total_acciones") == 9,
        f"(cuentas={resumen.get('total_cuentas')}, "
        f"acciones={resumen.get('total_acciones')})",
    )
    check(
        "3+3+3: contadores de pausa en el resumen",
        resumen.get("pausadas_omitidas") == 1
        and resumen.get("pausadas_usuarios") == ["t_paus"],
        f"({resumen.get('pausadas_usuarios')})",
    )
    check(
        "3+3+3: sin fallos por la pausada",
        resumen.get("fallidas") == 0 and resumen.get("exitosas") == 9,
        f"(fallidas={resumen.get('fallidas')}, exitosas={resumen.get('exitosas')})",
    )


# --------------------------------------------------------------------------- #
# (5) _reserva pausada en `_preparar_reservas` (directo)
# --------------------------------------------------------------------------- #
def test_preparar_reservas_pausadas(check):
    print("(5) _preparar_reservas: filtra pausadas y las reporta")
    motor = MotorActivacion(max_concurrente=1)
    activa = _CuentaFake("r_ok")
    pausada = _CuentaFake("r_paus", pausada=True)
    salida: list = []
    reservas = motor._preparar_reservas(
        [activa, pausada], [], pausadas_out=salida
    )
    check(
        "reserva pausada fuera de la lista",
        [c.usuario for c in reservas] == ["r_ok"],
        f"({[c.usuario for c in reservas]})",
    )
    check(
        "reserva pausada reportada por `pausadas_out`",
        salida == ["r_paus"],
        f"({salida})",
    )
    check(
        "sin `pausadas_out` la firma vieja sigue funcionando",
        [c.usuario for c in motor._preparar_reservas([activa], [])] == ["r_ok"],
    )
    check(
        "cuentas sin atributo de pausa siguen siendo reservas (no-regresion)",
        [c.usuario for c in motor._preparar_reservas(
            [_CuentaFake("vieja")], []
        )] == ["vieja"],
    )


# --------------------------------------------------------------------------- #
# (6) Scheduler: _bloqueada_por_pausa + _ejecutar_accion
# --------------------------------------------------------------------------- #
_TIPOS_ACTIVACION = (
    "rt", "retweet", "repost", "cita", "quote",
    "comentario", "respuesta", "reply", "like",
)
_TIPOS_MANTENIMIENTO = (
    "post", "mantenimiento", "calentamiento", "hilo", "visualizacion",
    "", "desconocido", None,
)


def test_guard_scheduler(check):
    print("(6) scheduler: _bloqueada_por_pausa (activacion vs mantenimiento)")
    ejecutor = ejecutor_mod.EjecutorTareas()
    pausada = _CuentaFake("sched_paus", pausada=True)
    activa = _CuentaFake("sched_act")
    check(
        "todos los tipos de activacion bloqueados con pausada",
        all(
            ejecutor._bloqueada_por_pausa(pausada, tipo) is True
            for tipo in _TIPOS_ACTIVACION
        ),
        f"({[(t, ejecutor._bloqueada_por_pausa(pausada, t)) for t in _TIPOS_ACTIVACION]})",
    )
    check(
        "los tipos de mantenimiento NO se bloquean",
        all(
            ejecutor._bloqueada_por_pausa(pausada, tipo) is False
            for tipo in _TIPOS_MANTENIMIENTO
        ),
        f"({[(t, ejecutor._bloqueada_por_pausa(pausada, t)) for t in _TIPOS_MANTENIMIENTO]})",
    )
    check(
        "cuenta NO pausada nunca se bloquea",
        all(
            ejecutor._bloqueada_por_pausa(activa, tipo) is False
            for tipo in _TIPOS_ACTIVACION
        ),
    )
    check(
        "mayusculas/espacios se normalizan (' RT ' -> bloqueada)",
        ejecutor._bloqueada_por_pausa(pausada, " RT ") is True,
    )
    tolerantes = (
        _CuentaFake("t_none", pausada=None),
        _CuentaFake("t_cero", pausada=0),
        _CuentaFake("t_str0", pausada="0"),
        _CuentaFake("t_vacio", pausada=""),
        _CuentaFake("t_false", pausada=False),
    )
    check(
        "valores falsos de pausa NO bloquean",
        all(
            ejecutor._bloqueada_por_pausa(c, "rt") is False for c in tolerantes
        ),
    )
    truthy = (
        _CuentaFake("t_true", pausada=True),
        _CuentaFake("t_uno", pausada=1),
        _CuentaFake("t_si", pausada="si"),
        _CuentaFake("t_strtrue", pausada="true"),
    )
    check(
        "valores verdaderos de pausa SI bloquean",
        all(
            ejecutor._bloqueada_por_pausa(c, "rt") is True for c in truthy
        ),
    )
    sin_attr = types.SimpleNamespace(usuario="t_sin_attr")
    check(
        "objeto sin atributo de pausa no se bloquea",
        ejecutor._bloqueada_por_pausa(sin_attr, "rt") is False,
    )
    motivo = ejecutor._motivo_pausa(pausada)
    check(
        "motivo exacto del contrato",
        motivo == "⏸️ @sched_paus — pausada para activación (solo mantenimiento)",
        f"({_ascii(motivo)!r})",
    )
    check(
        "el motivo NO es reintentable (como el guard de tier)",
        ejecutor_mod._es_error_reintentable(motivo) is False,
    )


class _TareaFake:
    """Tarea minima para `_ejecutar_accion`."""

    def __init__(self, tipo, contenido="", id_=1):
        self.id = id_
        self.tipo = tipo
        self.contenido = contenido
        self.imagen_path = ""
        self.plataforma = "twitter"
        self.cuentas_ids = "[]"


class _FakeBot:
    """Bot minimo sin Chrome."""

    def __init__(self, ok=True):
        self.ok = ok
        self.ultimo_error = ""
        self.cuenta_suspendida = False
        self.ultima_url_publicada = "https://x.com/u/status/1"
        self.publicados = []
        self.cerrados = 0

    def login_con_cookies(self):
        return True

    def publicar_tweet(self, contenido, imagen_path=None):
        self.publicados.append(contenido)
        return self.ok

    def cerrar(self):
        self.cerrados += 1


def _ascii(texto) -> str:
    """Version ASCII de un texto (Windows cp1252 no imprime emojis)."""
    try:
        return str(texto).encode("ascii", "ignore").decode("ascii")
    except Exception:
        return repr(texto)


@contextlib.contextmanager
def _sin_sleep():
    with mock.patch("time.sleep", lambda *a, **k: None):
        yield


def test_ejecutor_accion(check):
    print("(6b) _ejecutar_accion: rt bloqueado sin Chrome; post ejecuta")
    pausada = _CuentaFake("ej_paus", pausada=True)
    ejecutor = ejecutor_mod.EjecutorTareas()

    creados = []
    registros = []

    def _crear_bot(*a, **k):
        creados.append((a, k))
        return _FakeBot()

    with mock.patch(
        "plataformas.base.PlataformaFactory.crear_bot", _crear_bot
    ), mock.patch.object(
        ejecutor_mod, "registrar_accion",
        lambda *a, **k: registros.append(a),
    ), _sin_sleep():
        ok, motivo = ejecutor._ejecutar_accion(
            _TareaFake("rt", json.dumps(["https://x.com/ancla"])), pausada
        )
    check(
        "rt en pausada: bloqueada con el motivo exacto",
        ok is False
        and motivo == "⏸️ @ej_paus — pausada para activación (solo mantenimiento)",
        f"(ok={ok!r}, motivo={_ascii(motivo)!r})",
    )
    check(
        "rt en pausada: NO se creo el bot (sin Chrome)",
        creados == [],
        f"({creados})",
    )
    check(
        "rt en pausada: se registra como fallido con el motivo",
        registros
        and registros[0][0] == "ej_paus"
        and registros[0][2] == "fallido"
        and registros[0][4] == motivo,
        f"({_ascii(registros)})",
    )
    check(
        "rt en pausada: segundo intento tampoco crea bot",
        ejecutor._bloqueada_por_pausa(pausada, "retweet") is True,
    )

    # Mantenimiento: `post` SI se ejecuta en una cuenta pausada.
    bot_ok = _FakeBot()
    registros2 = []
    with mock.patch(
        "plataformas.base.PlataformaFactory.crear_bot",
        lambda *a, **k: bot_ok,
    ), mock.patch.object(
        ejecutor_mod, "registrar_accion",
        lambda *a, **k: registros2.append(a),
    ), _sin_sleep():
        ok_post, motivo_post = ejecutor._ejecutar_accion(
            _TareaFake("post", "texto de mantenimiento #x"), pausada
        )
    check(
        "post en pausada: SE ejecuta (mantenimiento)",
        ok_post is True and motivo_post == "" and bot_ok.publicados,
        f"(ok={ok_post!r}, motivo={motivo_post!r})",
    )
    check(
        "post en pausada: cierra el bot",
        bot_ok.cerrados == 1,
        f"(cerrados={bot_ok.cerrados})",
    )

    # Cita/comentario/like tambien bloqueados; mantenimiento variado permitido.
    for tipo in ("cita", "comentario", "like", "reply"):
        with mock.patch(
            "plataformas.base.PlataformaFactory.crear_bot", _crear_bot
        ), _sin_sleep():
            ok_x, _motivo_x = ejecutor._ejecutar_accion(
                _TareaFake(tipo, ""), pausada
            )
        check(
            f"tipo '{tipo}' en pausada: bloqueado sin Chrome",
            ok_x is False and not creados,
            f"(ok={ok_x!r})",
        )


# --------------------------------------------------------------------------- #
# (7) Calentamiento: las pausadas siguen siendo elegibles
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, filas):
        self._filas = list(filas)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._filas)

    def first(self):
        return self._filas[0] if self._filas else None


class _FakeDB:
    """Sesion minima para `elegir_cuenta`/`programar_publicacion`."""

    def __init__(self, cuentas=(), registros=(), tareas=()):
        self.cuentas = list(cuentas)
        self.registros = list(registros)
        self.tareas = list(tareas)
        self.agregadas = []
        self._siguiente_id = len(self.tareas)

    def query(self, entidad, *resto):
        if entidad is CuentaModel:
            return _FakeQuery(self.cuentas)
        from core.models import RegistroAccion, Tarea

        if entidad is Tarea:
            return _FakeQuery(self.tareas)
        if entidad is RegistroAccion:
            return _FakeQuery(self.registros)
        return _FakeQuery([])

    def add(self, obj):
        self._siguiente_id += 1
        if getattr(obj, "id", None) is None:
            obj.id = self._siguiente_id
        self.agregadas.append(obj)
        self.tareas.append(obj)

    def commit(self):
        pass

    def close(self):
        pass


@contextlib.contextmanager
def _sesion_falsa(db):
    yield db


def _cuenta_calentamiento(id_, usuario, pausada=False):
    return types.SimpleNamespace(
        id=id_,
        usuario=usuario,
        plataforma="twitter",
        activa=True,
        status="imported",
        cookies_path="",
        cookies_json=None,
        auth_token="tok_test",
        tipo_cuenta="",
        personalidad="",
        seccion="",
        nombre_mostrado="",
        perfil_personalidad="",
        pausada_activacion=pausada,
    )


def test_calentamiento_pausadas_elegibles(check):
    print("(7) calentamiento: las pausadas siguen siendo elegibles")
    pausada = _cuenta_calentamiento(7, "cal_paus", pausada=True)
    activa = _cuenta_calentamiento(8, "cal_act", pausada=False)
    check(
        "_es_elegible: pausada SI (mantenimiento)",
        calentamiento._es_elegible(pausada, set(), set()) is True,
    )
    check(
        "_es_elegible: activa SI (no-regresion)",
        calentamiento._es_elegible(activa, set(), set()) is True,
    )

    db = _FakeDB(cuentas=[pausada])
    with mock.patch.object(
        calentamiento, "_usuarios_con_registro_reciente",
        lambda db_, gap: set(),
    ):
        elegida = calentamiento.elegir_cuenta(db, gap_horas=12)
    check(
        "elegir_cuenta: devuelve la pausada cuando es la unica candidata",
        elegida is pausada,
        f"({getattr(elegida, 'usuario', None)})",
    )

    vistas: dict = {}

    def _choice(lista):
        vistas["candidatas"] = list(lista)
        return lista[0]

    db2 = _FakeDB(cuentas=[activa, pausada])
    with mock.patch.object(
        calentamiento, "_usuarios_con_registro_reciente",
        lambda db_, gap: set(),
    ), mock.patch.object(calentamiento.random, "choice", _choice):
        calentamiento.elegir_cuenta(db2, gap_horas=12)
    candidatas = [getattr(c, "usuario", "") for c in vistas.get("candidatas", [])]
    check(
        "elegir_cuenta: la pausada esta entre las candidatas",
        "cal_paus" in candidatas,
        f"({candidatas})",
    )

    # programar_publicacion crea la Tarea de mantenimiento para la pausada.
    db3 = _FakeDB(cuentas=[pausada])
    with mock.patch.object(
        calentamiento, "get_db_session",
        lambda: _sesion_falsa(db3),
    ), mock.patch.object(
        calentamiento, "_usuarios_con_registro_reciente",
        lambda db_, gap: set(),
    ), mock.patch.object(
        calentamiento, "generar_texto", lambda cuenta: "texto #Comunidad"
    ), mock.patch.object(calentamiento.random, "choice", _choice), mock.patch(
        "time.sleep", lambda *a, **k: None
    ):
        item = calentamiento.programar_publicacion()
    check(
        "programar_publicacion: programa la tarea de post de la pausada",
        isinstance(item, dict) and item.get("usuario") == "cal_paus",
        f"({item})",
    )
    check(
        "la Tarea creada es de mantenimiento (tipo post)",
        bool(db3.agregadas)
        and getattr(db3.agregadas[0], "tipo", "") == "post"
        and getattr(db3.agregadas[0], "estado", "") == "pendiente",
        f"({[(getattr(t, 'tipo', ''), getattr(t, 'estado', '')) for t in db3.agregadas]})",
    )

    # Y el guard del ejecutor NO la bloquea al ejecutarla (post).
    check(
        "el scheduler no bloquea la tarea de mantenimiento de la pausada",
        ejecutor_mod.EjecutorTareas._bloqueada_por_pausa(pausada, "post") is False,
    )


# --------------------------------------------------------------------------- #
# (8) No-regresion: campana sin pausadas = comportamiento previo
# --------------------------------------------------------------------------- #
def test_sin_pausadas_no_regresion(check):
    print("(8) no-regresion: sin pausadas el resumen trae 0 y todo ejecuta")
    cuentas = [
        _CuentaFake("n1", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("n2", rol_activacion="rt", tier_calidad="tier2"),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_roles(motor, cuentas, llamadas)
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
        "sin pausadas: ambas ejecutan",
        sorted({u for u, _r in llamadas}) == ["n1", "n2"],
        f"({llamadas})",
    )
    check(
        "sin pausadas: claves presentes con 0/[]",
        resumen.get("pausadas_omitidas") == 0
        and resumen.get("pausadas_usuarios") == [],
        f"(omitidas={resumen.get('pausadas_omitidas')})",
    )
    check(
        "sin pausadas: exitosas=2 y fallidas=0",
        resumen.get("exitosas") == 2 and resumen.get("fallidas") == 0,
        f"(exitosas={resumen.get('exitosas')})",
    )

    # Flujo vacio (sin cuentas): las claves tambien estan presentes.
    motor2 = MotorActivacion(max_concurrente=1)
    motor2._obtener_cuentas = lambda *a, **k: []
    resumen2 = motor2.ejecutar(urls=[], texto_base="", duracion_min=1)
    check(
        "campana vacia: claves de pausa presentes",
        resumen2.get("pausadas_omitidas") == 0
        and resumen2.get("pausadas_usuarios") == [],
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_e2e_roles_pausadas(check)
    test_seleccion_manual_y_todas_pausadas(check)
    test_e2e_cita_masiva_y_reserva(check)
    test_e2e_3_3_3(check)
    test_preparar_reservas_pausadas(check)
    test_guard_scheduler(check)
    test_ejecutor_accion(check)
    test_calentamiento_pausadas_elegibles(check)
    test_sin_pausadas_no_regresion(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
