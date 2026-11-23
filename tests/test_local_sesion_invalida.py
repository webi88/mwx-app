# -*- coding: utf-8 -*-
"""Tests de sesion invalida en la campana LOCAL (`activaciones/local.py`).

Casos reales: una cuenta con login/onboarding/anti-bot debe marcarse como
caida y OMITIR todas sus acciones restantes de la misma campana, sin volver a
abrir su pagina ni registrar sus acciones omitidas como hechas/fallidas.

Solo stdlib + fakes: sin Chrome, sin red, sin BD real.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.local as local_mod  # noqa: E402
import activaciones.variaciones as variaciones_mod  # noqa: E402
import core.registro as registro_mod  # noqa: E402
import ia.generador_contenido as generador_mod  # noqa: E402
from core.config import settings  # noqa: E402


@contextlib.contextmanager
def _parches(*cambios):
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
    def __init__(self, inicio=1000.0):
        self.ahora = float(inicio)

    def monotonic(self):
        return self.ahora

    def sleep(self, segundos):
        self.ahora += float(segundos)


def _cuenta(usuario):
    return {
        "usuario": usuario,
        "tier_calidad": "tier1",
        "tipo_cuenta": "ciudadana",
        "perfil_personalidad": "popular",
        "personalidad": "le gusta el futbol",
        "seccion": "CI",
        "nombre_mostrado": usuario,
        "handle_actual": "",
        "pausada_activacion": False,
        "auth_token": "token",
        "cookies_json": None,
        "cookies_path": "",
    }


class _BotLocal:
    creados = []
    refresh_estados = {}
    errores_entrada = {}
    errores_cambio = {}
    fallos_post = {}
    fallos_cita = {}
    fallan_cambio = set()

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
        self.refrescar_llamadas = 0
        self.cerrado = False
        type(self).creados.append(self)

    def iniciar_driver(self, pantalla_externa=False, proxy_dinamico=False,
                       modo_local=False):
        return True

    def preparar_sesion_cdp(self):
        return True

    def login_con_cookies(self):
        return False

    def refrescar_pagina_rapido(self):
        self.refrescar_llamadas += 1
        estado = type(self).refresh_estados.get(self.usuario, "ok")
        self.ultimo_refresh = estado
        self.ultimo_error = type(self).errores_entrada.get(self.usuario, "")
        return estado

    def cambiar_cuenta(self, usuario):
        self.cambio_llamadas.append(usuario)
        if usuario in type(self).fallan_cambio:
            self.ultimo_error = "cambio de sesion fallido"
            return False
        self.usuario = usuario
        self.ultimo_error = type(self).errores_cambio.get(usuario, "")
        self.ultimo_refresh = type(self).refresh_estados.get(usuario, "ok")
        return True

    def esta_vivo(self):
        return True

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True):
        self.posts.append({"usuario": self.usuario, "texto": texto})
        detalle = type(self).fallos_post.get(self.usuario, "")
        if detalle:
            self.ultimo_error = detalle
            return None
        url = f"https://x.com/{self.usuario}/status/{len(self.posts)}"
        self.ultima_url_publicada = url
        return url

    def solo_retwittear(self, targets, usuario, mensaje_cita=None,
                        dar_like=False, imagen_path=None, buscar_url=True):
        self.citas.append({"usuario": usuario, "mensaje_cita": mensaje_cita})
        detalle = type(self).fallos_cita.get(usuario, "")
        if detalle:
            self.ultimo_error = detalle
            return {"exitos": 0, "fallidos": 1, "urls": []}
        return {"exitos": 1, "fallidos": 0, "urls": []}

    def cerrar(self):
        self.cerrado = True


def _clase_bot(**atributos):
    clase = type("FakeBotLocal", (_BotLocal,), {})
    clase.creados = []
    for nombre, valor in atributos.items():
        setattr(clase, nombre, valor)
    return clase


def _sin_barajar(_lista):
    return None


@contextlib.contextmanager
def _campana(cuentas, bot_cls, registros=None):
    reloj = _Reloj()
    registros = registros if registros is not None else []

    def _leer(usuarios=None):
        return list(cuentas), [], ""

    def _contar(usuarios):
        return {}

    def _registrar(usuario, tipo, estado, url="", detalle=""):
        registros.append(
            {
                "usuario": usuario,
                "tipo": tipo,
                "estado": estado,
                "url": url,
                "detalle": detalle,
            }
        )

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
            yield {"registros": registros, "reloj": reloj}


def _ejecutar(clase, cuentas, **kwargs):
    with _settings(local_max_cuentas=5, limite_local_dia=49, headless=False):
        with _campana(cuentas, clase) as entorno:
            res = local_mod.ejecutar_campana_local(
                usuarios=[cuenta["usuario"] for cuenta in cuentas],
                **kwargs,
            )
    bot = clase.creados[0] if clase.creados else None
    return res, entorno, bot


def run(check):
    local_mod.cerrar_navegadores_locales()

    check(
        "sesion: 'sesion de X expirada' con login se detecta",
        local_mod._error_sesion_invalida(
            "sesión de X expirada o inválida: se pidió login "
            "(url=https://x.com/i/jf/onboarding/web)"
        ),
    )
    check(
        "sesion: 'sesion expirada (.pkl invalido)' tambien se detecta",
        local_mod._error_sesion_invalida(
            "sesión expirada (.pkl inválido): cookies vencidas"
        ),
    )
    check(
        "sesion: compositor con login se detecta",
        local_mod._error_sesion_invalida(
            "compositor de X no cargo: title=Home / X login=..."
        ),
    )
    check(
        "sesion: compositor sin login es transitorio",
        not local_mod._error_sesion_invalida(
            "compositor de X no cargo: el editor visible no aparecio"
        ),
    )
    check(
        "sesion: anti-bot/cloudflare se detecta",
        local_mod._error_sesion_invalida(
            "X pidió verificación anti-bot (Cloudflare); sesión no confirmada"
        ),
    )
    check(
        "sesion: onboarding y telefono se detectan",
        local_mod._error_sesion_invalida(
            "https://x.com/i/jf/onboarding/web introduce tu número de teléfono"
        ),
    )
    check(
        "sesion: muro de login y crea tu cuenta se detectan",
        local_mod._error_sesion_invalida("muro de login: crea tu cuenta")
        and local_mod._error_sesion_invalida("add phone number")
        and local_mod._error_sesion_invalida("sign up"),
    )
    check(
        "sesion: timeout/renderer/red NO se detectan",
        not local_mod._error_sesion_invalida(
            "TimeoutException: Timed out receiving message from renderer"
        )
        and not local_mod._error_sesion_invalida("net::ERR_CONNECTION_RESET"),
    )
    check(
        "sesion: detalle vacio/None no se detecta",
        not local_mod._error_sesion_invalida("")
        and not local_mod._error_sesion_invalida(None),
    )

    clase = _clase_bot(
        refresh_estados={"c_login": "login"},
        fallos_post={
            "b_expirada": (
                "sesión de X expirada o inválida: se pidió login "
                "(url=https://x.com/i/jf/onboarding/web)"
            )
        },
    )
    res, entorno, bot = _ejecutar(
        clase,
        [_cuenta("a_valida"), _cuenta("b_expirada"), _cuenta("c_login")],
        urls=["https://x.com/ancla/status/1"],
        hashtags="#mexico",
        texto_base="Texto base",
        acciones_por_cuenta=2,
        duracion_min=1,
    )
    check(
        "caso real: 3 acciones ejecutadas (2 de A + 1 fallida de B)",
        res["hechas"] == 3 and res["exitosas"] == 2 and res["fallidas"] == 1,
        f"(hechas={res['hechas']}, exitosas={res['exitosas']}, fallidas={res['fallidas']})",
    )
    check(
        "caso real: planificadas 6 y omitidas 3 sin contarlas como hechas",
        res["planificadas"] == 6
        and res["omitidas"] == 3
        and res["omitidas"] == res["planificadas"] - res["hechas"],
    )
    check(
        "caso real: B y C se marcan una sola vez como sin sesion",
        res["omitidas_sin_sesion"] == 2
        and res["omitidas_usuarios"]["sin_sesion"] == ["b_expirada", "c_login"],
        f"({res['omitidas_usuarios']['sin_sesion']})",
    )
    check(
        "caso real: la cita de B NO se ejecuta",
        all(cita["usuario"] != "b_expirada" for cita in bot.citas)
        and sum(1 for post in bot.posts if post["usuario"] == "b_expirada") == 1,
    )
    check(
        "caso real: C se detecta al cambiar de cuenta y no publica nada",
        "c_login" in bot.cambio_llamadas
        and all(post["usuario"] != "c_login" for post in bot.posts)
        and all(cita["usuario"] != "c_login" for cita in bot.citas),
    )
    check(
        "caso real: por_cuenta refleja lo ejecutado (C en cero)",
        res["por_cuenta"]["a_valida"]["exitosas"] == 2
        and res["por_cuenta"]["b_expirada"]["fallidas"] == 1
        and res["por_cuenta"]["b_expirada"]["hechas"] == 1
        and res["por_cuenta"]["c_login"]["hechas"] == 0
        and res["por_cuenta"]["c_login"]["planificadas"] == 2,
    )
    check(
        "caso real: solo se registran las 3 acciones ejecutadas",
        len(entorno["registros"]) == 3
        and [
            (fila["usuario"], fila["tipo"], fila["estado"])
            for fila in entorno["registros"]
        ]
        == [
            ("a_valida", "post", "exito"),
            ("b_expirada", "post", "fallido"),
            ("a_valida", "cita", "exito"),
        ],
    )
    check(
        "caso real: eventos, hechas y cuentas_usadas cuadran",
        len(res["eventos"]) == res["hechas"] == 3
        and res["cuentas_usadas"] == 2
        and res["hechas"] == res["exitosas"] + res["fallidas"],
    )

    # La campana LOCAL ya NO refresca al entrar: aunque el fake devolviera un
    # estado invalido, `refrescar_pagina_rapido` no debe llamarse y la cuenta
    # ejecuta sus acciones (la sesion invalida se detectara en la accion).
    resultados_entrada = []
    for estado in ("login", "anti_bot", "driver", "ok", "error", "skip"):
        clase = _clase_bot(refresh_estados={"c_entrada": estado})
        res, entorno, bot = _ejecutar(
            clase,
            [_cuenta("c_entrada")],
            urls=["https://x.com/ancla/status/2"],
            hashtags="#mexico",
            texto_base="Texto base",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        resultados_entrada.append(
            (
                estado,
                res["hechas"] == 2
                and res["omitidas_sin_sesion"] == 0
                and bot.refrescar_llamadas == 0
                and len(bot.posts) == 1
                and len(bot.citas) == 1,
            )
        )
    check(
        "entrada: la campana local NO refresca (ningun estado se consulta)",
        all(ok for _estado, ok in resultados_entrada),
        f"({resultados_entrada})",
    )

    clase = _clase_bot(errores_cambio={"c_error": "muro de login"})
    res, entorno, bot = _ejecutar(
        clase,
        [_cuenta("a_valida"), _cuenta("c_error")],
        urls=["https://x.com/ancla/status/6"],
        hashtags="#mexico",
        texto_base="Texto base",
        acciones_por_cuenta=2,
        duracion_min=1,
    )
    check(
        "cambio: ultimo_error de login omite las acciones de esa cuenta",
        res["hechas"] == 2
        and res["omitidas_sin_sesion"] == 1
        and "c_error" in bot.cambio_llamadas
        and all(post["usuario"] != "c_error" for post in bot.posts)
        and all(cita["usuario"] != "c_error" for cita in bot.citas),
    )

    clase = _clase_bot(fallan_cambio={"b_caida"})
    res, entorno, bot = _ejecutar(
        clase,
        [_cuenta("a_valida"), _cuenta("b_caida")],
        urls=["https://x.com/ancla/status/7"],
        hashtags="#mexico",
        texto_base="Texto base",
        acciones_por_cuenta=2,
        duracion_min=1,
    )
    check(
        "cambio: cambiar_cuenta False se marca UNA vez (sin doble conteo)",
        res["hechas"] == 2
        and res["omitidas_sin_sesion"] == 1
        and res["omitidas_usuarios"]["sin_sesion"] == ["b_caida"]
        and bot.cambio_llamadas.count("b_caida") == 1,
        f"({res['omitidas_usuarios']['sin_sesion']})",
    )

    for detalle in (
        "TimeoutException: Timed out receiving message from renderer",
        "net::ERR_CONNECTION_RESET",
        "compositor de X no cargo: el editor visible no aparecio",
    ):
        clase = _clase_bot(fallos_post={"a_transitoria": detalle})
        res, entorno, bot = _ejecutar(
            clase,
            [_cuenta("a_transitoria")],
            urls=["https://x.com/ancla/status/8"],
            hashtags="#mexico",
            texto_base="Texto base",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        check(
            f"transitorio: '{detalle[:28]}...' NO marca y la cita se ejecuta",
            res["hechas"] == 2
            and res["fallidas"] == 1
            and res["exitosas"] == 1
            and res["omitidas_sin_sesion"] == 0
            and len(bot.citas) == 1,
        )

    clase = _clase_bot(
        fallos_post={
            "a_expirada": (
                "compositor de X no cargo: title=Home / X login=..."
            )
        }
    )
    res, entorno, bot = _ejecutar(
        clase,
        [_cuenta("a_expirada")],
        urls=["https://x.com/ancla/status/9"],
        hashtags="#mexico",
        texto_base="Texto base",
        acciones_por_cuenta=2,
        duracion_min=1,
    )
    check(
        "accion: compositor con login marca y omite la cita siguiente",
        res["hechas"] == 1
        and res["fallidas"] == 1
        and res["omitidas_sin_sesion"] == 1
        and not bot.citas,
    )
