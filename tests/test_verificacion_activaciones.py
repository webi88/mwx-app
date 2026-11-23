# -*- coding: utf-8 -*-
"""Tests del CAJON DE VERIFICACION HUMANA en las activaciones.

Regla: una cuenta con `verificacion_pendiente=True` (login, captcha, bloqueo o
verificacion de identidad REAL de X) queda FUERA de todos los flujos de
activacion y, cuando una campana detecta ese estado, la manda al cajon con
`core.verificacion.marcar_pendiente` (una sola vez por cuenta).

Sin red, sin Chrome y sin tocar la base real: se parchean la lectura de
cuentas, `TwitterBot`, `core.verificacion.marcar_pendiente`, el registro, la IA
y las cuotas (mismo patron que `tests/test_local_sesion_invalida.py` y
`tests/test_pausas_motor.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_verificacion_activaciones.py
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.local as local_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
import activaciones.variaciones as variaciones_mod  # noqa: E402
import core.registro as registro_mod  # noqa: E402
import core.verificacion as verificacion_mod  # noqa: E402
import ia.generador_contenido as generador_mod  # noqa: E402
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


@contextlib.contextmanager
def _settings(**cambios):
    originales = {}
    try:
        for nombre, valor in cambios.items():
            originales[nombre] = getattr(settings, nombre, None)
            setattr(settings, nombre, valor)
        yield
    finally:
        for nombre, valor in originales.items():
            setattr(settings, nombre, valor)


class _Reloj:
    """`time` minimo: monotonic avanza y sleep no duerme."""

    def __init__(self, inicio=1000.0, paso=0.5):
        self.ahora = float(inicio)
        self.paso = float(paso)

    def monotonic(self):
        self.ahora += self.paso
        return self.ahora

    def sleep(self, segundos):
        try:
            self.ahora += max(0.0, float(segundos))
        except (TypeError, ValueError):
            self.ahora += self.paso


def _cuenta_local(usuario, pendiente=False, pausada=False, motivo=""):
    return {
        "usuario": usuario,
        "tier_calidad": "tier1",
        "tipo_cuenta": "ciudadana",
        "perfil_personalidad": "popular",
        "personalidad": "le gusta el futbol",
        "seccion": "CI",
        "nombre_mostrado": usuario,
        "handle_actual": "",
        "pausada_activacion": pausada,
        "verificacion_pendiente": pendiente,
        "verificacion_motivo": motivo,
        "auth_token": "token",
        "cookies_json": None,
        "cookies_path": "",
    }


class _CuentaFake:
    """Cuenta minima del motor (sin BD)."""

    def __init__(self, usuario, rol_activacion="", tier_calidad="",
                 pausada=False, verificacion_pendiente=False, **extra):
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
        self.verificacion_pendiente = verificacion_pendiente
        self.verificacion_motivo = ""
        self.plataforma = "twitter"
        self.activa = True
        for clave, valor in extra.items():
            setattr(self, clave, valor)


class _BotLocal:
    """Fake de TwitterBot para la campana LOCAL (sin Chrome)."""

    creados = []
    fallan_sesion = set()
    errores_entrada = {}
    fallos_post = {}

    def __init__(self, usuario, modo_local=False):
        self.usuario = usuario
        self.modo_local = modo_local
        self.driver = True
        self.ultima_url_publicada = ""
        self.ultimo_error = ""
        self.ultimo_refresh = ""
        self.posts = []
        self.citas = []
        self.cambio_llamadas = []
        self.cerrado = False
        type(self).creados.append(self)

    def iniciar_driver(self, pantalla_externa=False, proxy_dinamico=False,
                       modo_local=False):
        return True

    def preparar_sesion_cdp(self):
        if self.usuario in type(self).fallan_sesion:
            self.ultimo_error = type(self).errores_entrada.get(
                self.usuario, "muro de login: se pidió login"
            )
            return False
        return True

    def login_con_cookies(self):
        if self.usuario in type(self).fallan_sesion:
            self.ultimo_error = type(self).errores_entrada.get(
                self.usuario, "muro de login: se pidió login"
            )
            return False
        return True

    def cambiar_cuenta(self, usuario, **kwargs):
        self.cambio_llamadas.append(usuario)
        self.usuario = usuario
        self.ultima_url_publicada = ""
        if usuario in type(self).fallan_sesion:
            self.ultimo_error = type(self).errores_entrada.get(
                usuario, "muro de login: se pidió login"
            )
            return False
        return True

    def esta_vivo(self):
        return True

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True,
                       buscar_url_perfil=None):
        self.posts.append({"usuario": self.usuario, "texto": texto})
        detalle = type(self).fallos_post.get(self.usuario, "")
        if detalle:
            self.ultimo_error = detalle
            return None
        url = f"https://x.com/{self.usuario}/status/{len(self.posts)}"
        self.ultima_url_publicada = url
        return url

    def solo_retwittear(self, targets, usuario, mensaje_cita=None,
                        dar_like=False, imagen_path=None, buscar_url=True,
                        buscar_url_perfil=None):
        self.citas.append({"usuario": usuario, "mensaje_cita": mensaje_cita})
        detalle = type(self).fallos_post.get(usuario, "")
        if detalle:
            self.ultimo_error = detalle
            return {"exitos": 0, "fallidos": 1, "urls": []}
        return {"exitos": 1, "fallidos": 0, "urls": []}

    def cerrar(self):
        self.cerrado = True


def _clase_bot(**atributos):
    clase = type("FakeBotVerificacion", (_BotLocal,), {})
    clase.creados = []
    for nombre, valor in atributos.items():
        setattr(clase, nombre, valor)
    return clase


def _sin_barajar(_lista):
    return None


@contextlib.contextmanager
def _campana_local(cuentas, bot_cls):
    """Parches de una campana LOCAL (sin Chrome, sin BD, sin IA real)."""
    reloj = _Reloj()

    def _leer(usuarios=None):
        return list(cuentas), [], ""

    def _contar(usuarios):
        return {}

    def _registrar(*args, **kwargs):
        return None

    def _posts(info, **kwargs):
        n = int(kwargs.get("n_por_cuenta", 1) or 1)
        return {
            cuenta["usuario"]: [
                f"Post {indice} de {cuenta['usuario']}" for indice in range(n)
            ]
            for cuenta in info
        }

    def _variaciones(base, cantidad, **kwargs):
        return [f"{base} cita {indice}" for indice in range(int(cantidad))]

    cambios = [
        (local_mod, "_leer_cuentas", _leer),
        (local_mod, "time", reloj),
        (local_mod, "TwitterBot", bot_cls),
        (registro_mod, "contar_acciones_dia_por_usuario", _contar),
        (registro_mod, "registrar_accion", _registrar),
        (generador_mod, "generar_textos_hashtags_por_cuenta", _posts),
        (variaciones_mod, "generar_pool_variaciones_openai", _variaciones),
    ]
    with _parches(*cambios):
        with _parches((local_mod.random, "shuffle", _sin_barajar)):
            yield reloj


def _patches_e2e(registros):
    """Parches comunes de un E2E del motor (sin BD, IA ni Chrome)."""
    return (
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    )


def _preparar_roles(motor, cuentas, llamadas):
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
# (1) Senales de bloqueo/verificacion de X
# --------------------------------------------------------------------------- #
def test_senales_bloqueo(check):
    print("(1) senales REALES de bloqueo/verificacion de X")
    textos = (
        "Your account has locked. Confirm your identity to continue.",
        "We noticed unusual activity on your account",
        "Please confirm your identity to unlock your account",
        "You must pass a challenge before continuing",
        "Your account unlocked",
        "Tu cuenta bloqueada temporalmente",
        "Detectamos actividad inusual",
        "Confirma tu identidad para continuar",
        "Verifica tu identidad para usar X",
    )
    for texto in textos:
        check(
            f"local: reconoce '{texto[:32]}...'",
            local_mod._error_bloqueo_cuenta(texto),
        )
        check(
            f"motor: reconoce '{texto[:32]}...'",
            motor_mod._es_bloqueo_cuenta(texto),
        )
    check(
        "local: bloqueo tambien entra en _error_sesion_invalida",
        local_mod._error_sesion_invalida(
            "Your account has locked. Confirm your identity"
        )
        and local_mod._error_sesion_invalida("actividad inusual"),
    )
    check(
        "motor: bloqueo tambien cuenta como sesion caida",
        motor_mod._es_sesion_caida_detalle("cuenta bloqueada"),
    )
    check(
        "sin falsos positivos con 'compositor de X no cargo'",
        not local_mod._error_bloqueo_cuenta(
            "compositor de X no cargó: el editor visible no apareció"
        )
        and not motor_mod._es_bloqueo_cuenta(
            "compositor de X no cargó: el editor visible no apareció"
        ),
    )
    check(
        "sin falsos positivos con errores de driver/red",
        not local_mod._error_bloqueo_cuenta(
            "TimeoutException: Timed out receiving message from renderer"
        )
        and not local_mod._error_bloqueo_cuenta("net::ERR_CONNECTION_RESET")
        and not motor_mod._es_bloqueo_cuenta("TimeoutException: renderer"),
    )
    check(
        "detalle vacio/None no es bloqueo",
        not local_mod._error_bloqueo_cuenta("")
        and not local_mod._error_bloqueo_cuenta(None)
        and not motor_mod._es_bloqueo_cuenta("")
        and not motor_mod._es_bloqueo_cuenta(None),
    )


# --------------------------------------------------------------------------- #
# (2) Local: dict, seleccion y resumen
# --------------------------------------------------------------------------- #
def test_local_seleccion(check):
    print("(2) LOCAL: dict, seleccion y resumen con el cajon")
    cuenta = SimpleNamespace(
        usuario="dict_user",
        tier_calidad="tier1",
        tipo_cuenta="ciudadana",
        perfil_personalidad="popular",
        personalidad="",
        seccion="CI",
        nombre_mostrado="Dict User",
        handle_actual="",
        pausada_activacion=False,
        verificacion_pendiente=True,
        verificacion_motivo="muro de login",
        auth_token="token",
        cookies_json=None,
        cookies_path="",
    )
    datos = local_mod._cuenta_a_dict(cuenta)
    check(
        "_cuenta_a_dict incluye verificacion_pendiente/motivo",
        datos.get("verificacion_pendiente") is True
        and datos.get("verificacion_motivo") == "muro de login",
        f"({datos.get('verificacion_pendiente')}, "
        f"{datos.get('verificacion_motivo')!r})",
    )

    cuentas = [
        _cuenta_local("normal"),
        _cuenta_local("pendiente", pendiente=True, motivo="captcha"),
        # Pendiente Y pausada: debe reportarse SOLO en verificacion.
        _cuenta_local("pendiente_pausada", pendiente=True, pausada=True),
        _cuenta_local("pausada", pausada=True),
    ]
    seleccion = local_mod._seleccionar_cuentas(cuentas, None, False, 10)
    nombres = [c["usuario"] for c in seleccion["selected"]]
    check(
        "seleccion: la pendiente NO entra a la campana",
        nombres == ["normal"],
        f"({nombres})",
    )
    check(
        "seleccion: omitidas_verificacion reporta ambas pendientes",
        seleccion["omitidas_verificacion"] == ["pendiente", "pendiente_pausada"],
        f"({seleccion['omitidas_verificacion']})",
    )
    check(
        "seleccion: no se mezcla con pausadas",
        seleccion["omitidas_pausadas"] == ["pausada"],
        f"({seleccion['omitidas_pausadas']})",
    )

    resumen = local_mod._resumen_inicial(49, 6, 60, 0, 100)
    check(
        "resumen inicial: claves de verificacion en cero",
        resumen["omitidas_verificacion"] == 0
        and resumen["verificaciones_marcadas"] == 0
        and resumen["verificaciones_usuarios"] == []
        and resumen["omitidas_usuarios"]["verificacion"] == [],
    )
    check(
        "firma: ejecutar_campana_local esperar_captcha_seg default 0",
        __import__("inspect").signature(
            local_mod.ejecutar_campana_local
        ).parameters["esperar_captcha_seg"].default == 0,
    )


# --------------------------------------------------------------------------- #
# (3) Local: marcar pendiente en campana (login/bloqueo) y NO por driver
# --------------------------------------------------------------------------- #
def test_local_marcar_pendiente(check):
    print("(3) LOCAL: marca pendiente por login/bloqueo, no por driver")
    local_mod.cerrar_navegadores_locales()

    # --- Login wall en la entrada: se marca UNA vez y se omite ---
    llamadas: list = []
    eventos: list = []

    def _marcar(usuario, motivo="", origen="", plataforma="twitter"):
        llamadas.append(
            {"usuario": usuario, "motivo": motivo, "origen": origen}
        )
        return True

    clase = _clase_bot(
        fallan_sesion={"loginwall"},
        errores_entrada={
            "loginwall": "muro de login: se pidió login (url=x.com/i/flow/login)"
        },
    )
    with _parches((verificacion_mod, "marcar_pendiente", _marcar)):
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ):
            with _campana_local(
                [
                    _cuenta_local("ya_pendiente", pendiente=True),
                    _cuenta_local("loginwall"),
                ],
                clase,
            ):
                res = local_mod.ejecutar_campana_local(
                    usuarios=["ya_pendiente", "loginwall"],
                    hashtags="#mexico",
                    acciones_por_cuenta=1,
                    duracion_min=1,
                    callback=eventos.append,
                )
    check(
        "campana: la cuenta pendiente previa queda fuera",
        res["cuentas"] == ["loginwall"]
        and res["omitidas_verificacion"] == 1
        and res["omitidas_usuarios"]["verificacion"] == ["ya_pendiente"],
        f"({res['cuentas']}, {res['omitidas_usuarios']['verificacion']})",
    )
    check(
        "campana: el login wall marca pendiente con motivo/origen",
        res["verificaciones_marcadas"] == 1
        and res["verificaciones_usuarios"] == ["loginwall"]
        and len(llamadas) == 1
        and llamadas[0]["usuario"] == "loginwall"
        and llamadas[0]["origen"] == "local"
        and "login" in llamadas[0]["motivo"].lower(),
        f"({llamadas})",
    )
    check(
        "campana: la cuenta se reporta sin sesion UNA vez",
        res["omitidas_sin_sesion"] == 1
        and res["omitidas_usuarios"]["sin_sesion"] == ["loginwall"],
        f"({res['omitidas_usuarios']['sin_sesion']})",
    )
    verificaciones = [
        evento for evento in eventos
        if evento.get("tipo") == "verificacion_requerida"
    ]
    check(
        "campana: callback recibe verificacion_requerida",
        len(verificaciones) == 1
        and verificaciones[0]["usuario"] == "loginwall"
        and isinstance(verificaciones[0].get("hechas"), int)
        and verificaciones[0].get("total") == 1,
        f"({verificaciones})",
    )
    check(
        "campana: no se abre Chrome real (solo el fake)",
        len(clase.creados) == 1 and clase.creados[0].usuario == "loginwall",
        f"({[b.usuario for b in clase.creados]})",
    )

    # --- Error SOLO de driver/renderer: NO se manda al cajon ---
    llamadas_driver: list = []

    def _marcar_driver(usuario, motivo="", origen="", plataforma="twitter"):
        llamadas_driver.append(usuario)
        return True

    clase_driver = _clase_bot(
        fallos_post={
            "solo_driver": (
                "TimeoutException: Timed out receiving message from renderer"
            )
        }
    )
    with _parches((verificacion_mod, "marcar_pendiente", _marcar_driver)):
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ):
            with _campana_local([_cuenta_local("solo_driver")], clase_driver):
                res_driver = local_mod.ejecutar_campana_local(
                    usuarios=["solo_driver"],
                    hashtags="#mexico",
                    acciones_por_cuenta=1,
                    duracion_min=1,
                )
    check(
        "campana: un fallo SOLO de driver NO marca pendiente",
        llamadas_driver == []
        and res_driver["verificaciones_marcadas"] == 0
        and res_driver["omitidas_sin_sesion"] == 0,
        f"(llamadas={llamadas_driver}, "
        f"marcadas={res_driver['verificaciones_marcadas']})",
    )

    # --- Bloqueo real en la accion: SI se manda al cajon ---
    llamadas_lock: list = []

    def _marcar_lock(usuario, motivo="", origen="", plataforma="twitter"):
        llamadas_lock.append({"usuario": usuario, "motivo": motivo})
        return True

    clase_lock = _clase_bot(
        fallos_post={
            "bloqueada": (
                "Your account has locked. Confirm your identity to continue"
            )
        }
    )
    with _parches((verificacion_mod, "marcar_pendiente", _marcar_lock)):
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ):
            with _campana_local([_cuenta_local("bloqueada")], clase_lock):
                res_lock = local_mod.ejecutar_campana_local(
                    usuarios=["bloqueada"],
                    hashtags="#mexico",
                    acciones_por_cuenta=1,
                    duracion_min=1,
                )
    check(
        "campana: un bloqueo REAL de X marca pendiente",
        res_lock["verificaciones_marcadas"] == 1
        and res_lock["omitidas_sin_sesion"] == 1
        and llamadas_lock
        and llamadas_lock[0]["usuario"] == "bloqueada"
        and "locked" in llamadas_lock[0]["motivo"].lower(),
        f"({llamadas_lock})",
    )


# --------------------------------------------------------------------------- #
# (4) Motor: _registrar_sesion_caida / _procesar_suspension_cuenta
# --------------------------------------------------------------------------- #
def test_motor_registrar_sesion(check):
    print("(4) MOTOR: marca pendiente solo por sesion/bloqueo REAL")
    llamadas: list = []

    def _marcar(usuario, motivo="", origen="", plataforma="twitter"):
        llamadas.append(
            {"usuario": usuario, "motivo": motivo, "origen": origen}
        )
        return True

    motor = MotorActivacion(max_concurrente=1)
    with _parches((verificacion_mod, "marcar_pendiente", _marcar)):
        motor._registrar_sesion_caida(
            "lock1", "Your account has locked. Confirm your identity"
        )
        check(
            "lock: sesion caida + marcar_pendiente(origen=activacion)",
            motor._sesion_caida("lock1")
            and len(llamadas) == 1
            and llamadas[0]["usuario"] == "lock1"
            and llamadas[0]["origen"] == "activacion"
            and "locked" in llamadas[0]["motivo"].lower(),
            f"({llamadas})",
        )
        motor._registrar_sesion_caida(
            "anti", "X pidió verificación anti-bot (Cloudflare); no confirmada"
        )
        check(
            "anti-bot duro: tambien va al cajon",
            len(llamadas) == 2 and llamadas[1]["usuario"] == "anti",
            f"({llamadas})",
        )
        motor._registrar_sesion_caida(
            "rechazo", "Your account may not be allowed to perform this action"
        )
        check(
            "rechazo definitivo de X: NO va al cajon",
            len(llamadas) == 2 and motor._sesion_caida("rechazo"),
            f"({llamadas})",
        )
        motor._registrar_sesion_caida("inter1", "something went wrong")
        check(
            "interstitial transitorio: NO va al cajon (1er fallo)",
            len(llamadas) == 2 and not motor._sesion_caida("inter1"),
            f"({llamadas})",
        )
        motor._registrar_sesion_caida("inter1", "something went wrong")
        check(
            "interstitial transitorio: se omite al 2do fallo SIN marcar",
            len(llamadas) == 2 and motor._sesion_caida("inter1"),
            f"({llamadas})",
        )
        motor._registrar_sesion_caida(
            "driver", "TimeoutException: Timed out receiving message from renderer"
        )
        check(
            "driver/renderer: NO va al cajon",
            len(llamadas) == 2 and not motor._sesion_caida("driver"),
            f"({llamadas})",
        )

        # _procesar_suspension_cuenta con bandera del bot y bloqueo: NO
        # desactiva en la BD y SI manda la cuenta al cajon.
        desactivadas: list = []
        bot = SimpleNamespace(
            cuenta_suspendida=True,
            ultimo_error="actividad inusual: verifica tu identidad",
        )
        with _parches(
            (motor_mod, "marcar_cuenta_suspendida",
             lambda usuario: desactivadas.append(usuario))
        ):
            resultado = motor._procesar_suspension_cuenta(
                _CuentaFake("sosp"), bot, ""
            )
        check(
            "bloqueo con cuenta_suspendida: NO desactiva y SI marca pendiente",
            resultado is False
            and desactivadas == []
            and len(llamadas) == 3
            and llamadas[-1]["usuario"] == "sosp"
            and motor._sesion_caida("sosp"),
            f"(resultado={resultado}, desactivadas={desactivadas}, "
            f"llamadas={llamadas[-1:]})",
        )


# --------------------------------------------------------------------------- #
# (5) Motor: separacion/anotacion y flujos
# --------------------------------------------------------------------------- #
def test_motor_separacion(check):
    print("(5) MOTOR: _separar_verificacion/_anotar_verificacion")
    cuentas = [
        SimpleNamespace(usuario="a_pend", verificacion_pendiente=True),
        SimpleNamespace(usuario="b_ok", verificacion_pendiente=False),
        {"usuario": "c_pend", "verificacion_pendiente": "si"},
    ]
    elegibles, pendientes = MotorActivacion._separar_verificacion(cuentas)
    check(
        "separar: deja fuera las pendientes (ORM y dict)",
        [c.usuario if hasattr(c, "usuario") else c["usuario"]
         for c in elegibles] == ["b_ok"],
        f"({elegibles})",
    )
    check(
        "separar: reporta los usuarios pendientes",
        pendientes == ["a_pend", "c_pend"],
        f"({pendientes})",
    )

    original = verificacion_mod.filtrar_para_activacion

    def _boom(*args, **kwargs):
        raise RuntimeError("BD caida")

    with _parches((verificacion_mod, "filtrar_para_activacion", _boom)):
        elegibles2, pendientes2 = MotorActivacion._separar_verificacion(cuentas)
    check(
        "separar: tolerante (si core.verificacion falla, devuelve todo)",
        len(elegibles2) == 3 and pendientes2 == [],
        f"({len(elegibles2)}, {pendientes2})",
    )
    check(
        "separar: el fallo no deja el monkeypatch roto",
        verificacion_mod.filtrar_para_activacion is original,
    )

    resumen: dict = {}
    usuarios = [f"u{i}" for i in range(25)] + ["u0"]
    MotorActivacion._anotar_verificacion(resumen, usuarios)
    check(
        "anotar: total real y lista recortada a 20 (sin duplicados)",
        resumen["verificacion_omitidas"] == 25
        and len(resumen["verificacion_usuarios"]) == 20
        and resumen["verificacion_usuarios"][0] == "u0",
        f"({resumen['verificacion_omitidas']}, "
        f"{len(resumen['verificacion_usuarios'])})",
    )
    motor = MotorActivacion(max_concurrente=1)
    check(
        "helpers de resumen: claves de verificacion SIEMPRE presentes",
        motor._claves_cuotas({}).get("verificacion_omitidas") == 0
        and motor._claves_cuotas({}).get("verificacion_usuarios") == []
        and motor._con_claves_pestana({}).get("verificacion_omitidas") == 0
        and motor._con_claves_pestana({}).get("verificacion_usuarios") == [],
    )


def test_motor_flujos(check):
    print("(6) MOTOR: los flujos excluyen pendientes y lo reportan")

    # --- ejecutar_por_roles ---
    cuentas = [
        _CuentaFake("ok_post", rol_activacion="hashtags", tier_calidad="tier1"),
        _CuentaFake("pend_post", rol_activacion="hashtags", tier_calidad="tier1",
                    verificacion_pendiente=True),
    ]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    registros: list = []
    _preparar_roles(motor, cuentas, llamadas)
    with _parches(*_patches_e2e(registros)):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla"],
            texto_base="texto base",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "por roles: la pendiente NO ejecuta",
        [u for u, _r in llamadas] == ["ok_post"],
        f"({llamadas})",
    )
    check(
        "por roles: verificacion_omitidas/usuarios en el resumen",
        resumen.get("verificacion_omitidas") == 1
        and resumen.get("verificacion_usuarios") == ["pend_post"],
        f"({resumen.get('verificacion_omitidas')}, "
        f"{resumen.get('verificacion_usuarios')})",
    )
    check(
        "por roles: sin fallos por la pendiente",
        resumen.get("exitosas") == 1 and resumen.get("fallidas") == 0,
        f"(exitosas={resumen.get('exitosas')}, "
        f"fallidas={resumen.get('fallidas')})",
    )

    # --- ejecutar (cita masiva) ---
    cuentas_cita = [
        _CuentaFake("c_ok", tier_calidad="tier1"),
        _CuentaFake("c_pend", tier_calidad="tier1",
                    verificacion_pendiente=True),
    ]
    motor2 = MotorActivacion(max_concurrente=1)
    citas: list = []
    registros2: list = []
    motor2._obtener_cuentas = lambda *a, **k: list(cuentas_cita)
    motor2._asignar_variaciones_cita = (
        lambda cs, *a, **k: {c.usuario: "texto" for c in cs}
    )
    motor2._distribuir_cohortes = lambda cs, d, c: [list(cs)]

    def _quote(cuenta, urls, texto, dar_like, retardo=0):
        citas.append(cuenta.usuario)
        return (cuenta.usuario, True, "ok", "https://x.com/c/status/1")

    motor2._quote_rt_una_cuenta = _quote
    with _parches(*_patches_e2e(registros2)):
        resumen_cita = motor2.ejecutar(
            urls=["https://x.com/ancla"],
            texto_base="texto",
            duracion_min=1,
            cohortes=1,
            repetir=False,
        )
    check(
        "cita masiva: la pendiente NO ejecuta",
        citas == ["c_ok"],
        f"({citas})",
    )
    check(
        "cita masiva: reporta verificacion_omitidas",
        resumen_cita.get("verificacion_omitidas") == 1
        and resumen_cita.get("verificacion_usuarios") == ["c_pend"],
        f"({resumen_cita.get('verificacion_usuarios')})",
    )

    # --- reservas: una pendiente NUNCA es respaldo ---
    motor3 = MotorActivacion(max_concurrente=1)
    salida_verificacion: list = []
    reservas = motor3._preparar_reservas(
        [
            _CuentaFake("r_ok"),
            _CuentaFake("r_pend", verificacion_pendiente=True),
        ],
        [],
        verificacion_out=salida_verificacion,
    )
    check(
        "reservas: la pendiente queda fuera y se reporta",
        [r.usuario for r in reservas] == ["r_ok"]
        and salida_verificacion == ["r_pend"],
        f"({[r.usuario for r in reservas]}, {salida_verificacion})",
    )

    # --- ejecutar_actividad ---
    cuentas_act = [
        _CuentaFake("act_ok", rol_activacion="hashtags",
                    tier_calidad="tier1"),
        _CuentaFake("act_pend", rol_activacion="hashtags",
                    tier_calidad="tier1", verificacion_pendiente=True),
    ]
    motor4 = MotorActivacion(max_concurrente=1)
    ejecutadas: list = []
    registros4: list = []
    motor4._cargar_cuentas_actividad = lambda usuarios: list(cuentas_act)
    motor4._objetivos_actividad = (
        lambda cs, n_min, n_max: {c.usuario: 1 for c in cs}
    )
    motor4._generar_textos_actividad = (
        lambda cs, objetivo, tags, *a, **k: {
            c.usuario: ["texto #x"] for c in cs
        }
    )
    motor4._ejecutar_accion_rol = (
        lambda cuenta, rol, urls, texto, like, retardo=0: (
            cuenta.usuario, rol, True, "ok", "https://x.com/a/status/1"
        )
    )
    motor4._dormir_cancelable = lambda *a, **k: True
    with _parches(*_patches_e2e(registros4)):
        resumen_act = motor4.ejecutar_actividad(
            usuarios=[c.usuario for c in cuentas_act],
            hashtags=["#x"],
            posts_min=1,
            posts_max=1,
            pausa_entre_posts_seg=(0, 0),
            max_browsers=1,
            duracion_max_min=1,
        )
    check(
        "actividad: la pendiente NO publica",
        resumen_act.get("verificacion_omitidas") == 1
        and resumen_act.get("verificacion_usuarios") == ["act_pend"]
        and resumen_act.get("exitosas") == 1
        and resumen_act.get("fallidas") == 0,
        f"(verificacion={resumen_act.get('verificacion_usuarios')}, "
        f"exitosas={resumen_act.get('exitosas')})",
    )


# --------------------------------------------------------------------------- #
# (7) UI: default del reto en la pagina LOCAL
# --------------------------------------------------------------------------- #
def test_ui_default(check):
    print("(7) UI LOCAL: el reto viene en 0 (saltar y guardar en el cajon)")
    fuente = (RAIZ / "web" / "operaciones" / "local.py").read_text(
        encoding="utf-8"
    )
    patron = re.search(
        r'"🧠 Esperar a que resuelvas el reto Cloudflare \(seg\)"'
        r"[\s\S]{0,800}?value=0",
        fuente,
    )
    check(
        "number_input del reto: value=0",
        patron is not None,
    )
    bloque = fuente
    if patron is not None:
        inicio = patron.start()
        bloque = fuente[inicio:inicio + 900]
    check(
        "help del reto menciona el cajon de verificacion",
        "cajón" in bloque.lower() or "cajon" in bloque.lower(),
        f"({bloque[:120]!r})",
    )


def run(check):
    test_senales_bloqueo(check)
    test_local_seleccion(check)
    test_local_marcar_pendiente(check)
    test_motor_registrar_sesion(check)
    test_motor_separacion(check)
    test_motor_flujos(check)
    test_ui_default(check)


if __name__ == "__main__":
    sys.path.insert(0, str(RAIZ / "tests"))
    from run_tests import check as _check

    run(_check)
    sys.exit(0 if not __import__("run_tests")._FALLOS else 1)
