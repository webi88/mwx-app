# -*- coding: utf-8 -*-
"""Tests de las URLs REALES en la tabla de Exitos (Reportes) del motor.

Reglas congeladas por rol (`RegistroAccion.url_publicacion`):
  - `cita`: URL del POST DE LA CITA (la publicacion nueva de la cuenta); "" si
    no se pudo capturar. NUNCA el tweet ancla.
  - `hashtags`/`post`: URL del post publicado; "" si no se capturo. NUNCA el
    ancla.
  - `comentario`: URL de la RESPUESTA publicada (lo que devuelve
    `responder_tweet`); "" si no. NUNCA el ancla.
  - `rt` simple y `like`: "" SIEMPRE (no generan link; se etiquetan).

Se cubren:
  - `_url_publicada_valida`: acepta `/status/`, limpia query, rechaza perfiles
    (twitter.com y x.com) y basura.
  - `_accion_en_bot` (ruta clasica y de pestañas): rt/cita/comentario/hashtags.
  - `_probar_api_rol`: rt/like -> ""; cita/comentario/hashtags -> la URL que
    expone la API (`ultima_url_publicada`), nunca el ancla.
  - `_capturar_url_post`: default ON; `CAPTURAR_URL_POST=0` pasa
    `buscar_url=False` a `publicar_tweet`.

Sin red, sin Chrome y sin BD: fakes + `mock.patch` (mismo patron que
`test_cancelacion_motor.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_urls_exitos_motor.py
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from activaciones.motor import (  # noqa: E402
    MotorActivacion,
    _url_publicada_valida,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _env(nombre, valor):
    """Setea/borra una env y la restaura SIEMPRE al salir."""
    previo = os.environ.get(nombre)
    try:
        if valor is None:
            os.environ.pop(nombre, None)
        else:
            os.environ[nombre] = str(valor)
        yield
    finally:
        if previo is None:
            os.environ.pop(nombre, None)
        else:
            os.environ[nombre] = previo


class _CuentaFake:
    """Cuenta minima con los atributos que lee `_accion_en_bot`."""

    def __init__(self, usuario="cuenta_test"):
        self.usuario = usuario


class _BotFake:
    """Bot minimo: registra las llamadas y devuelve lo que el test configure."""

    def __init__(self, cita=None, rt=None, respuesta=None, post=None):
        self.cita = cita
        self.rt = rt
        self.respuesta = respuesta
        self.post = post
        self.ultimo_error = ""
        self.ultima_url_publicada = ""
        self.cuenta_suspendida = False
        self.llamadas = []

    def solo_retwittear(self, urls, usuario, mensaje_cita=None, dar_like=False):
        self.llamadas.append(("solo_retwittear", list(urls), mensaje_cita))
        return self.cita if mensaje_cita is not None else self.rt

    def responder_tweet(self, url, texto):
        self.llamadas.append(("responder_tweet", url, texto))
        return self.respuesta

    def publicar_tweet(self, texto, buscar_url=True):
        self.llamadas.append(("publicar_tweet", texto, buscar_url))
        return self.post


class _APIFake:
    """`TwitterAPI` fake: `accion_rapida` OK y una URL de publicacion."""

    instancias: list = []

    def __init__(self, usuario, url=""):
        self.usuario = usuario
        self.ultima_url_publicada = url
        type(self).instancias.append(self)
        self.llamadas = []

    def accion_rapida(self, rol, url="", dar_like=False, texto=""):
        self.llamadas.append((rol, url, dar_like, texto))
        return True


class _APISinUrl(_APIFake):
    """API fake SIN atributo `ultima_url_publicada` (retrocompatible)."""

    def __init__(self, usuario):
        super().__init__(usuario)
        del self.ultima_url_publicada


def _accion(motor, bot, rol, texto="texto de prueba",
            url_objetivo="https://x.com/ancla/status/1", dar_like=False):
    return motor._accion_en_bot(
        bot, _CuentaFake(), rol, texto, dar_like, url_objetivo
    )


# --------------------------------------------------------------------------- #
# `_url_publicada_valida`
# --------------------------------------------------------------------------- #
def test_url_valida(check):
    print("(1) _url_publicada_valida: /status/ si, perfil y basura no")
    check(
        "acepta https://x.com/<usuario>/status/<id>",
        _url_publicada_valida("https://x.com/ana/status/123")
        == "https://x.com/ana/status/123",
    )
    check(
        "acepta twitter.com",
        _url_publicada_valida("https://twitter.com/ana/status/123")
        == "https://twitter.com/ana/status/123",
    )
    check(
        "limpia el query string",
        _url_publicada_valida("https://x.com/ana/status/123?s=20&t=abc")
        == "https://x.com/ana/status/123",
    )
    check(
        "limpia el fragmento y la barra final",
        _url_publicada_valida("https://x.com/ana/status/123/#x")
        == "https://x.com/ana/status/123",
    )
    check(
        "acepta subdominios (mobile)",
        _url_publicada_valida("https://mobile.twitter.com/ana/status/9")
        == "https://mobile.twitter.com/ana/status/9",
    )
    check(
        "rechaza el PERFIL en x.com",
        _url_publicada_valida("https://x.com/ana") == "",
    )
    check(
        "rechaza el PERFIL en twitter.com",
        _url_publicada_valida("https://twitter.com/ana") == "",
    )
    check(
        "rechaza un status sin id numerico",
        _url_publicada_valida("https://x.com/ana/status/abc") == "",
    )
    check(
        "rechaza basura/None/vacio",
        _url_publicada_valida("") == ""
        and _url_publicada_valida(None) == ""
        and _url_publicada_valida("no es una url") == ""
        and _url_publicada_valida("ftp://x.com/a/status/1") == "",
    )


# --------------------------------------------------------------------------- #
# `_accion_en_bot` (ruta clasica y de pestañas persistentes)
# --------------------------------------------------------------------------- #
def test_accion_en_bot(check):
    print("(2) _accion_en_bot: URL por rol (rt/cita/comentario/hashtags)")
    motor = MotorActivacion(max_concurrente=1)
    ancla = "https://x.com/ancla/status/1"

    # --- cita: URL del POST DE LA CITA ---
    bot = _BotFake(
        cita={"exitos": 1, "urls": ["https://x.com/cuenta_test/status/501"]}
    )
    res = _accion(motor, bot, "cita", url_objetivo=ancla)
    check(
        "cita: registra la URL del post de la cita",
        res[2] is True and res[4] == "https://x.com/cuenta_test/status/501",
        str(res),
    )

    # --- cita sin URL capturada: "" (NUNCA el ancla) ---
    bot = _BotFake(cita={"exitos": 1, "urls": []})
    res = _accion(motor, bot, "cita", url_objetivo=ancla)
    check(
        "cita: sin link queda '' (nunca el ancla)",
        res[2] is True and res[4] == "",
        str(res),
    )

    bot = _BotFake(cita={"exitos": 1, "urls": ["https://x.com/cuenta_test"]})
    res = _accion(motor, bot, "cita", url_objetivo=ancla)
    check(
        "cita: un perfil del bot NO es link",
        res[4] == "",
        str(res),
    )

    # --- rt simple: "" siempre, aunque el bot devuelva el perfil ---
    bot = _BotFake(rt={"exitos": 1, "urls": ["https://twitter.com/cuenta_test"]})
    res = _accion(motor, bot, "rt", url_objetivo=ancla)
    check(
        "rt simple: exito pero SIN link (no el perfil)",
        res[2] is True and res[4] == "",
        str(res),
    )

    bot = _BotFake(rt={"exitos": 1, "urls": []})
    res = _accion(motor, bot, "rt", url_objetivo=ancla)
    check("rt simple: sin urls sigue en ''", res[2] is True and res[4] == "", str(res))

    # --- comentario: URL de la RESPUESTA (str devuelto por responder_tweet) ---
    bot = _BotFake(respuesta="https://x.com/cuenta_test/status/601")
    res = _accion(motor, bot, "comentario", url_objetivo=ancla)
    check(
        "comentario: registra la URL de la RESPUESTA devuelta",
        res[2] is True and res[4] == "https://x.com/cuenta_test/status/601",
        str(res),
    )

    # comentario True (publico sin URL): respaldo `ultima_url_publicada`.
    bot = _BotFake(respuesta=True)
    bot.ultima_url_publicada = "https://x.com/cuenta_test/status/602"
    res = _accion(motor, bot, "comentario", url_objetivo=ancla)
    check(
        "comentario: True + ultima_url_publicada -> esa URL",
        res[2] is True and res[4] == "https://x.com/cuenta_test/status/602",
        str(res),
    )

    bot = _BotFake(respuesta=True)
    res = _accion(motor, bot, "comentario", url_objetivo=ancla)
    check(
        "comentario: sin URL capturada -> '' (nunca el ancla)",
        res[2] is True and res[4] == "",
        str(res),
    )

    # --- hashtags: URL del post publicado (str o ultima_url_publicada) ---
    with _env("CAPTURAR_URL_POST", "1"):
        bot = _BotFake(post="https://x.com/cuenta_test/status/701")
        res = _accion(motor, bot, "hashtags")
        check(
            "hashtags: registra la URL del post devuelto",
            res[2] is True and res[4] == "https://x.com/cuenta_test/status/701",
            str(res),
        )
        check(
            "hashtags: pide la URL a publicar_tweet (buscar_url=True default)",
            ("publicar_tweet", "texto de prueba", True) in bot.llamadas,
            str(bot.llamadas),
        )

        bot = _BotFake(post=True)
        bot.ultima_url_publicada = "https://x.com/cuenta_test/status/702"
        res = _accion(motor, bot, "hashtags")
        check(
            "hashtags: True + ultima_url_publicada -> esa URL",
            res[2] is True and res[4] == "https://x.com/cuenta_test/status/702",
            str(res),
        )

        bot = _BotFake(post=True)
        bot.ultima_url_publicada = "https://x.com/cuenta_test"  # perfil: invalido
        res = _accion(motor, bot, "hashtags")
        check(
            "hashtags: un perfil como ultima_url_publicada se descarta",
            res[2] is True and res[4] == "",
            str(res),
        )

    # CAPTURAR_URL_POST=0: maxima velocidad, sin links.
    with _env("CAPTURAR_URL_POST", "0"):
        bot = _BotFake(post=True)
        res = _accion(motor, bot, "hashtags")
        check(
            "hashtags: CAPTURAR_URL_POST=0 pide buscar_url=False",
            ("publicar_tweet", "texto de prueba", False) in bot.llamadas,
            str(bot.llamadas),
        )
        check(
            "hashtags: con la captura off queda ''",
            res[2] is True and res[4] == "",
            str(res),
        )

    # --- like sin ruta Selenium: "" (nunca el ancla) ---
    res = _accion(motor, _BotFake(), "like", url_objetivo=ancla)
    check(
        "like: sin link (nunca el ancla)",
        res[2] is False and res[4] == "",
        str(res),
    )


def test_capturar_url_post_default(check):
    print("(2b) _capturar_url_post: default ON (CAPTURAR_URL_POST=1)")
    motor = MotorActivacion(max_concurrente=1)
    with _env("CAPTURAR_URL_POST", None):
        check("default (sin env): activado", motor._capturar_url_post() is True)
    with _env("CAPTURAR_URL_POST", "0"):
        check("CAPTURAR_URL_POST=0: desactivado", motor._capturar_url_post() is False)
    with _env("CAPTURAR_URL_POST", "false"):
        check("CAPTURAR_URL_POST=false: desactivado", motor._capturar_url_post() is False)
    with _env("CAPTURAR_URL_POST", "1"):
        check("CAPTURAR_URL_POST=1: activado", motor._capturar_url_post() is True)


# --------------------------------------------------------------------------- #
# `_probar_api_rol`
# --------------------------------------------------------------------------- #
def test_probar_api_rol(check):
    print("(3) _probar_api_rol: URL real de la API por rol")
    motor = MotorActivacion(max_concurrente=1)
    cuenta = _CuentaFake()
    ancla = "https://x.com/ancla/status/1"

    with _env("API_PRIMERO", "1"):
        # rt: sin link.
        with mock.patch(
            "plataformas.twitter.api_http.TwitterAPI",
            lambda usuario: _APIFake(usuario, "https://x.com/cuenta_test/status/1"),
        ):
            res = motor._probar_api_rol(cuenta, "rt", ancla, "", False)
        check(
            "API rt: exito pero SIN link",
            res is not None and res[2] is True and res[4] == "",
            str(res),
        )

        # like: sin link.
        with mock.patch(
            "plataformas.twitter.api_http.TwitterAPI",
            lambda usuario: _APIFake(usuario, "https://x.com/cuenta_test/status/2"),
        ):
            res = motor._probar_api_rol(cuenta, "like", ancla, "", False)
        check(
            "API like: exito pero SIN link",
            res is not None and res[2] is True and res[4] == "",
            str(res),
        )

        # hashtags/cita/comentario: la URL que expone la API.
        for rol in ("hashtags", "cita", "comentario"):
            url_api = f"https://x.com/cuenta_test/status/10{len(rol)}"
            with mock.patch(
                "plataformas.twitter.api_http.TwitterAPI",
                lambda usuario, _u=url_api: _APIFake(usuario, _u),
            ):
                res = motor._probar_api_rol(cuenta, rol, ancla, "texto", False)
            check(
                f"API {rol}: usa ultima_url_publicada (no el ancla)",
                res is not None and res[2] is True and res[4] == url_api,
                str(res),
            )

        # API sin `ultima_url_publicada` (retrocompatible): "" y no el ancla.
        with mock.patch(
            "plataformas.twitter.api_http.TwitterAPI", _APISinUrl
        ):
            res = motor._probar_api_rol(cuenta, "hashtags", ancla, "texto", False)
        check(
            "API sin ultima_url_publicada: '' (nunca el ancla)",
            res is not None and res[2] is True and res[4] == "",
            str(res),
        )

        # La URL de la API invalida (perfil) tambien se descarta.
        with mock.patch(
            "plataformas.twitter.api_http.TwitterAPI",
            lambda usuario: _APIFake(usuario, "https://x.com/cuenta_test"),
        ):
            res = motor._probar_api_rol(cuenta, "cita", ancla, "texto", False)
        check(
            "API cita: un perfil devuelto por la API se descarta",
            res is not None and res[4] == "",
            str(res),
        )


def test_api_http_ultima_url(check):
    print("(3b) TwitterAPI: ultima_url_publicada desde el id de CreateTweet")
    from plataformas.twitter import api_http

    data_ok = {
        "data": {
            "create_tweet": {
                "tweet_results": {
                    "result": {"rest_id": "987654321"}
                }
            }
        }
    }
    api = api_http.TwitterAPI.__new__(api_http.TwitterAPI)
    api.usuario = "cuenta_test"
    api.ultima_url_publicada = ""
    api.session = object()  # evita `_cargar_cookies` (sin BD/red)
    with mock.patch.object(api, "_graphql", return_value=(200, data_ok)):
        ok = api.crear_tweet("Texto por API")
    check(
        "crear_tweet OK: ultima_url_publicada = link del tweet creado",
        ok is True
        and api.ultima_url_publicada == "https://x.com/cuenta_test/status/987654321",
        f"{ok} {api.ultima_url_publicada!r}",
    )

    api2 = api_http.TwitterAPI.__new__(api_http.TwitterAPI)
    api2.usuario = "cuenta_test"
    api2.ultima_url_publicada = "link_viejo"
    api2.session = object()
    sin_id = {
        "data": {"create_tweet": {"tweet_results": {"result": {}}}}
    }
    with mock.patch.object(api2, "_graphql", return_value=(200, sin_id)):
        ok2 = api2.crear_tweet("Texto sin id")
    check(
        "crear_tweet sin id: sin link (y sin exito: cae a Selenium)",
        ok2 is False and api2.ultima_url_publicada == "",
        f"{ok2} {api2.ultima_url_publicada!r}",
    )

    check(
        "respuesta con errors -> ''",
        api._id_tweet_creado({"errors": [{"code": 226}]}) == "",
    )
    check(
        "id anidado tweet.rest_id -> se acepta",
        api._id_tweet_creado(
            {"data": {"create_tweet": {"tweet_results": {"result": {"tweet": {"rest_id": "42"}}}}}}
        )
        == "42",
    )


def run(check):
    """Ejecuta los checks de este archivo con el `check` del runner."""
    test_url_valida(check)
    test_accion_en_bot(check)
    test_capturar_url_post_default(check)
    test_probar_api_rol(check)
    test_api_http_ultima_url(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_urls_exitos_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
