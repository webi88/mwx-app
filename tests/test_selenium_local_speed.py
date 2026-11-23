"""Suite rapida de VELOCIDAD del MODO LOCAL de `selenium_bot.py` (sin Chrome).

Campaña LOCAL (Chrome visible, 5 cuentas Tier 1): recortes de tiempo que NO
deben tocar el flujo normal (Railway/motor masivo). Verifica, con fakes y sin
abrir navegador ni tocar `data/`:

  - `solo_retwittear(..., buscar_url=True)` agrega el kwarg AL FINAL (despues de
    `imagen_path`), default True; los parametros/defaults previos no cambian.
  - En la rama de CITA con `buscar_url=False`: NO llama
    `_obtener_enlace_reciente_timeline` ni `_obtener_ultimo_enlace`, `urls`
    queda sin esa entrada y `ultima_url_publicada` vacia; el exito se decide
    igual (verificacion real). Con `True` las llama (ruta rapida y fallback al
    perfil) y captura la URL.
  - `cambiar_cuenta` en `modo_local=True` NO llama
    `_limpiar_cache_no_bloqueante`, pero SI limpia cookies por CDP
    (`Network.clearBrowserCookies`); en modo no local la llama como siempre.
  - `cambiar_cuenta` en `modo_local=True` NUNCA refresca la pagina (ni con
    `REFRESH_AL_ENTRAR=1` y `refrescar=True`) y deja `ultimo_refresh="skip"`
    (sin arrastrar "login"/"anti_bot" de la cuenta anterior).
  - `_pausa_humana`: local duerme uniform(0.3, 0.8); no local uniform(1.8, 3.5).
  - `_pausa_visible`: local recorta a <=0.2s; no local visible duerme el valor
    pedido; headless no local no duerme.
  - `TwitterBot(usuario, modo_local=False)` conserva el default y el flag.

Uso:
    .venv/Scripts/python.exe tests/test_selenium_local_speed.py
    .venv/Scripts/python.exe tests/run_tests.py
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import plataformas.twitter.selenium_bot as modulo  # noqa: E402
from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402

URL_TWEET = "https://x.com/alguien/status/100"
URL_TIMELINE = "https://x.com/cuenta_local/status/201"
URL_PERFIL = "https://x.com/cuenta_local/status/202"
URL_TOAST = "https://x.com/cuenta_local/status/203"


class FakeElemento:
    """Boton/clic de mentira: solo cuenta los clics."""

    def __init__(self):
        self.clics = 0

    def click(self):
        self.clics += 1


class FakeDriver:
    """Driver minimo para `solo_retwittear` y `cambiar_cuenta` (sin red)."""

    def __init__(self):
        self._url = "https://x.com/home"
        self.gets = []
        self.scripts = []
        self.cdp = []

    @property
    def current_url(self):
        return self._url

    def get(self, url):
        self.gets.append(url)

    def execute_script(self, script, *argumentos):
        self.scripts.append((script, argumentos))
        return None

    def execute_cdp_cmd(self, comando, parametros):
        self.cdp.append((comando, parametros))
        return {}


class FakeForwardProxy:
    """Forwarder de mentira: registra los cambios de upstream."""

    def __init__(self):
        self.upstreams = []

    def cambiar_upstream(self, proxy):
        self.upstreams.append(proxy)


def _correr_cita(
    buscar_url,
    timeline_result="",
    perfil_result="",
    buscar_url_perfil=True,
    toast_result="",
):
    """Ejecuta UNA cita con todo lo de Selenium monkeypatcheado.

    Devuelve `(bot, driver, resultado, registro, verificar, pegar)`.
    """
    bot = TwitterBot("cuenta_local", modo_local=True)
    driver = FakeDriver()
    bot.driver = driver
    registro = {"timeline": [], "perfil": [], "toast": []}
    boton_rt = FakeElemento()
    boton_quote = FakeElemento()
    boton_post = FakeElemento()
    editor = object()
    pegar = mock.Mock()
    verificar = mock.Mock(return_value=True)

    def _toast():
        registro["toast"].append(1)
        return toast_result

    def _timeline(mensaje, timeout=3.0):
        registro["timeline"].append(mensaje)
        return timeline_result

    def _perfil(usuario):
        registro["perfil"].append(usuario)
        return perfil_result

    with mock.patch("plataformas.twitter.selenium_bot.time.sleep"), mock.patch.object(
        bot, "_recuperar_interstitial", return_value="ok"
    ), mock.patch.object(
        bot, "_hay_muro_login", return_value=False
    ), mock.patch.object(
        bot, "_detectar_limite_cuenta", return_value=False
    ), mock.patch.object(
        bot, "_esperar_article_tweet"
    ), mock.patch.object(
        bot, "_buscar_boton_retweet", return_value=boton_rt
    ), mock.patch.object(
        bot, "_buscar_opcion_quote", return_value=boton_quote
    ), mock.patch.object(
        bot, "_esperar_editor_visible", return_value=editor
    ), mock.patch.object(
        bot, "_pegar_texto", pegar
    ), mock.patch.object(
        bot, "_buscar_boton_post", return_value=boton_post
    ), mock.patch.object(
        bot, "_verificar_publicacion", verificar
    ), mock.patch.object(
        bot, "_obtener_enlace_toast", side_effect=_toast
    ), mock.patch.object(
        bot, "_obtener_enlace_reciente_timeline", side_effect=_timeline
    ), mock.patch.object(
        bot, "_obtener_ultimo_enlace", side_effect=_perfil
    ):
        resultado = bot.solo_retwittear(
            [URL_TWEET],
            "cuenta_local",
            mensaje_cita="Gran partido el de ayer #mexico",
            buscar_url=buscar_url,
            buscar_url_perfil=buscar_url_perfil,
        )

    return bot, driver, resultado, registro, verificar, pegar


def test_firma_buscar_url(check):
    parametros = inspect.signature(TwitterBot.solo_retwittear).parameters
    nombres = list(parametros)
    check(
        "solo_retwittear: conserva los parametros previos en orden",
        nombres[:7]
        == [
            "self",
            "targets",
            "usuario",
            "mensaje_cita",
            "es_calentamiento",
            "dar_like",
            "imagen_path",
        ],
        str(nombres),
    )
    check(
        "solo_retwittear: buscar_url es el penultimo parametro (kwarg)",
        nombres[-2:] == ["buscar_url", "buscar_url_perfil"] and len(nombres) == 9,
        str(nombres),
    )
    check(
        "solo_retwittear: buscar_url default True (retrocompatible)",
        parametros["buscar_url"].default is True,
    )
    check(
        "solo_retwittear: buscar_url_perfil default True (historico)",
        parametros["buscar_url_perfil"].default is True,
    )
    check(
        "solo_retwittear: defaults previos intactos",
        parametros["mensaje_cita"].default is None
        and parametros["es_calentamiento"].default is False
        and parametros["dar_like"].default is False
        and parametros["imagen_path"].default is None,
    )


def test_cita_sin_buscar_url(check):
    bot, driver, resultado, registro, verificar, pegar = _correr_cita(
        buscar_url=False
    )

    check(
        "cita buscar_url=False: exitos=1 (verificacion real)",
        resultado["exitos"] == 1 and resultado["fallidos"] == 0,
        str(resultado),
    )
    check(
        "cita buscar_url=False: urls queda sin la entrada de la cita",
        resultado["urls"] == [],
        str(resultado["urls"]),
    )
    check(
        "cita buscar_url=False: NO llama _obtener_enlace_reciente_timeline",
        registro["timeline"] == [],
        str(registro["timeline"]),
    )
    check(
        "cita buscar_url=False: NO llama _obtener_ultimo_enlace",
        registro["perfil"] == [],
        str(registro["perfil"]),
    )
    check(
        "cita buscar_url=False: ultima_url_publicada vacia",
        bot.ultima_url_publicada == "",
        repr(bot.ultima_url_publicada),
    )
    check(
        "cita buscar_url=False: igual se verifico la publicacion",
        verificar.call_count == 1,
        f"llamadas={verificar.call_count}",
    )
    check(
        "cita buscar_url=False: igual se escribio el texto en el editor",
        pegar.call_count == 1,
        f"llamadas={pegar.call_count}",
    )
    check(
        "cita buscar_url=False: navego al tweet objetivo",
        driver.gets == [URL_TWEET],
        str(driver.gets),
    )


def test_cita_con_buscar_url(check):
    bot, driver, resultado, registro, verificar, pegar = _correr_cita(
        buscar_url=True, timeline_result=URL_TIMELINE
    )

    check(
        "cita buscar_url=True: exitos=1",
        resultado["exitos"] == 1 and resultado["fallidos"] == 0,
        str(resultado),
    )
    check(
        "cita buscar_url=True: llama la ruta rapida del timeline",
        len(registro["timeline"]) == 1,
        str(registro["timeline"]),
    )
    check(
        "cita buscar_url=True: NO visita el perfil si el timeline dio la URL",
        registro["perfil"] == [],
        str(registro["perfil"]),
    )
    check(
        "cita buscar_url=True: captura la URL del timeline",
        resultado["urls"] == [URL_TIMELINE]
        and bot.ultima_url_publicada == URL_TIMELINE,
        str(resultado["urls"]),
    )
    check(
        "cita buscar_url=True: verifico la publicacion real",
        verificar.call_count == 1,
    )
    check(
        "cita buscar_url=True: el flujo de cita no cambio (editor + Post)",
        pegar.call_count == 1 and driver.gets == [URL_TWEET],
    )


def test_cita_fallback_perfil(check):
    bot, driver, resultado, registro, verificar, pegar = _correr_cita(
        buscar_url=True, timeline_result="", perfil_result=URL_PERFIL
    )

    check(
        "cita buscar_url=True: timeline vacio -> fallback al perfil",
        len(registro["timeline"]) == 1 and registro["perfil"] == ["cuenta_local"],
        str(registro),
    )
    check(
        "cita buscar_url=True: captura la URL del perfil",
        resultado["urls"] == [URL_PERFIL]
        and bot.ultima_url_publicada == URL_PERFIL,
        str(resultado["urls"]),
    )
    check(
        "cita buscar_url=True: el exito sigue siendo 1",
        resultado["exitos"] == 1 and resultado["fallidos"] == 0,
    )


def test_cita_sin_buscar_url_perfil(check):
    bot, driver, resultado, registro, verificar, pegar = _correr_cita(
        buscar_url=True,
        timeline_result="",
        perfil_result=URL_PERFIL,
        buscar_url_perfil=False,
    )

    check(
        "cita buscar_url_perfil=False: exitos=1 (verificacion real)",
        resultado["exitos"] == 1 and resultado["fallidos"] == 0,
        str(resultado),
    )
    check(
        "cita buscar_url_perfil=False: NO llama _obtener_ultimo_enlace",
        registro["perfil"] == [],
        str(registro["perfil"]),
    )
    check(
        "cita buscar_url_perfil=False: urls queda sin la entrada de la cita",
        resultado["urls"] == [] and bot.ultima_url_publicada == "",
        f"{resultado['urls']} {bot.ultima_url_publicada!r}",
    )
    check(
        "cita buscar_url_perfil=False: SI intenta la ruta rapida del timeline",
        len(registro["timeline"]) == 1,
        str(registro["timeline"]),
    )


def test_cita_prefiere_toast(check):
    bot, driver, resultado, registro, verificar, pegar = _correr_cita(
        buscar_url=True,
        toast_result=URL_TOAST,
        timeline_result=URL_TIMELINE,
        perfil_result=URL_PERFIL,
    )

    check(
        "cita toast: captura el link del toast (0 navegaciones)",
        resultado["urls"] == [URL_TOAST]
        and bot.ultima_url_publicada == URL_TOAST,
        str(resultado["urls"]),
    )
    check(
        "cita toast: NO cae al timeline cuando el toast trae link",
        registro["timeline"] == [],
        str(registro["timeline"]),
    )
    check(
        "cita toast: NO visita el perfil cuando el toast trae link",
        registro["perfil"] == [],
        str(registro["perfil"]),
    )
    check(
        "cita toast: el exito sigue siendo 1",
        resultado["exitos"] == 1 and resultado["fallidos"] == 0,
        str(resultado),
    )


def test_cambiar_cuenta_cache(check):
    # Modo local: sin clearBrowserCache (hasta 3s por cambio) pero SI cookies.
    bot_local = TwitterBot("cuenta_a", modo_local=True)
    driver_local = FakeDriver()
    bot_local.driver = driver_local
    cache_local = mock.Mock()

    with mock.patch.object(
        bot_local, "_limpiar_cache_no_bloqueante", cache_local
    ), mock.patch.object(
        bot_local, "preparar_sesion_cdp", return_value=True
    ) as preparar_local, mock.patch.object(
        bot_local, "_obtener_ua_consistente", return_value=""
    ), mock.patch.object(
        bot_local, "_obtener_proxy", return_value="http://proxy-cuenta:8080"
    ), mock.patch.object(
        bot_local, "_proxy_para_x", return_value="http://proxy-cuenta:8080"
    ):
        ok_local = bot_local.cambiar_cuenta(
            "cuenta_b", proxy="http://proxy:8080", validar_proxy=True
        )

    check("cambiar_cuenta local: devuelve True", ok_local is True, bot_local.ultimo_error)
    check(
        "cambiar_cuenta local: NO llama _limpiar_cache_no_bloqueante",
        cache_local.call_count == 0,
        f"llamadas={cache_local.call_count}",
    )
    check(
        "cambiar_cuenta local: SI limpia cookies por CDP",
        ("Network.clearBrowserCookies", {}) in driver_local.cdp,
        str(driver_local.cdp),
    )
    check(
        "cambiar_cuenta local: sigue inyectando cookies por CDP",
        preparar_local.call_count == 1,
    )
    check(
        "cambiar_cuenta local: cambia el usuario activo",
        bot_local.usuario == "cuenta_b",
    )

    # Modo NO local: comportamiento historico (si llama el limpiador).
    bot_servidor = TwitterBot("cuenta_a")
    driver_servidor = FakeDriver()
    bot_servidor.driver = driver_servidor
    bot_servidor._fwd_proxy = FakeForwardProxy()
    cache_servidor = mock.Mock()

    with mock.patch.object(
        bot_servidor, "_limpiar_cache_no_bloqueante", cache_servidor
    ), mock.patch.object(
        bot_servidor, "preparar_sesion_cdp", return_value=True
    ) as preparar_servidor, mock.patch.object(
        bot_servidor, "_obtener_ua_consistente", return_value=""
    ):
        ok_servidor = bot_servidor.cambiar_cuenta(
            "cuenta_b", proxy="http://proxy:8080"
        )

    check(
        "cambiar_cuenta no local: devuelve True",
        ok_servidor is True,
        bot_servidor.ultimo_error,
    )
    check(
        "cambiar_cuenta no local: SI llama _limpiar_cache_no_bloqueante",
        cache_servidor.call_count == 1,
        f"llamadas={cache_servidor.call_count}",
    )
    check(
        "cambiar_cuenta no local: limpia cookies por CDP",
        ("Network.clearBrowserCookies", {}) in driver_servidor.cdp,
    )
    check(
        "cambiar_cuenta no local: inyecta cookies por CDP",
        preparar_servidor.call_count == 1,
    )


def test_cambiar_cuenta_local_sin_refresh(check):
    # Modo local: NUNCA refresca la pagina (el plan local rota de cuenta en
    # CADA accion y el refresh de entrada costaba ~6-8s por publicacion).
    bot = TwitterBot("cuenta_a", modo_local=True)
    bot.driver = FakeDriver()
    bot.ultimo_refresh = "login"  # estado viejo de la cuenta anterior
    refrescar = mock.Mock(return_value="ok")
    with mock.patch.dict(
        os.environ, {"REFRESH_AL_ENTRAR": "1"}, clear=False
    ), mock.patch.object(
        bot, "preparar_sesion_cdp", return_value=True
    ) as preparar, mock.patch.object(
        bot, "_obtener_ua_consistente", return_value=""
    ), mock.patch.object(
        bot, "refrescar_pagina_rapido", refrescar
    ):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
    check(
        "cambiar_cuenta local: NO llama refrescar_pagina_rapido "
        "(ni con REFRESH_AL_ENTRAR=1 y refrescar=True)",
        ok is True and refrescar.call_count == 0,
        f"ok={ok} llamadas={refrescar.call_count}",
    )
    check(
        "cambiar_cuenta local: deja ultimo_refresh=skip (sin estado viejo)",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )
    check(
        "cambiar_cuenta local: NO navega el driver",
        not getattr(bot.driver, "gets", []),
        str(getattr(bot.driver, "gets", [])),
    )
    check(
        "cambiar_cuenta local: sigue inyectando cookies por CDP",
        preparar.call_count == 1,
        f"llamadas={preparar.call_count}",
    )


def test_pausa_humana(check):
    bot_local = TwitterBot("cuenta_local", modo_local=True)
    with mock.patch(
        "plataformas.twitter.selenium_bot.random.uniform", return_value=0.55
    ) as uniforme, mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_local._pausa_humana()
    check(
        "pausa humana local: uniform(0.3, 0.8)",
        uniforme.call_args.args == (0.3, 0.8),
        str(uniforme.call_args),
    )
    check(
        "pausa humana local: duerme el valor sorteado",
        [c.args[0] for c in dormir.call_args_list] == [0.55],
        str(dormir.call_args_list),
    )

    bot_servidor = TwitterBot("cuenta_servidor")
    with mock.patch(
        "plataformas.twitter.selenium_bot.random.uniform", return_value=2.65
    ) as uniforme, mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_servidor._pausa_humana()
    check(
        "pausa humana no local: uniform(1.8, 3.5)",
        uniforme.call_args.args == (1.8, 3.5),
        str(uniforme.call_args),
    )
    check(
        "pausa humana no local: duerme el valor sorteado",
        [c.args[0] for c in dormir.call_args_list] == [2.65],
        str(dormir.call_args_list),
    )

    # Varias tiradas con el random REAL: siempre dentro del rango.
    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        for _ in range(30):
            bot_local._pausa_humana()
    valores_local = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa humana local: 30 tiradas dentro de [0.3, 0.8]",
        len(valores_local) == 30
        and all(0.3 <= v <= 0.8 for v in valores_local),
        f"min={min(valores_local):.2f} max={max(valores_local):.2f}",
    )

    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        for _ in range(30):
            bot_servidor._pausa_humana()
    valores_servidor = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa humana no local: 30 tiradas dentro de [1.8, 3.5]",
        len(valores_servidor) == 30
        and all(1.8 <= v <= 3.5 for v in valores_servidor),
        f"min={min(valores_servidor):.2f} max={max(valores_servidor):.2f}",
    )


def test_pausa_visible_tope_local(check):
    bot_local = TwitterBot("cuenta_local", modo_local=True)
    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        bot_local._pausa_visible(5.0)
        bot_local._pausa_visible(1.5)
        bot_local._pausa_visible(0.3)
    valores = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa visible local: tope 0.2s (5s -> 0.2s, 1.5s -> 0.2s, 0.3s -> 0.2s)",
        valores == [0.2, 0.2, 0.2],
        str(valores),
    )

    # En local el recorte manda incluso si headless esta activo (campaña local
    # nunca deberia estarlo, pero el tope no puede colarse por headless).
    with mock.patch.object(modulo.settings, "headless", True), mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_local._pausa_visible(5.0)
    check(
        "pausa visible local: con settings.headless=True igual duerme <=0.2s",
        [c.args[0] for c in dormir.call_args_list] == [0.2],
        str(dormir.call_args_list),
    )

    # No local visible: EXACTAMENTE el valor pedido (sin cambios).
    bot_servidor = TwitterBot("cuenta_servidor")
    with mock.patch.object(modulo.settings, "headless", False), mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_servidor._pausa_visible(5.0)
        bot_servidor._pausa_visible(3.0)
    check(
        "pausa visible no local: duerme EXACTAMENTE 5s y 3s",
        [c.args[0] for c in dormir.call_args_list] == [5.0, 3.0],
        str(dormir.call_args_list),
    )

    # No local + headless: no hay pantalla que mirar => NO duerme.
    with mock.patch.object(modulo.settings, "headless", True), mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_servidor._pausa_visible(5.0)
        bot_servidor._pausa_visible(3.0)
    check(
        "pausa visible headless (no local): NO duerme",
        dormir.call_count == 0,
        f"llamadas={dormir.call_count}",
    )

    # Borde: valores invalidos/0 no duermen (nunca lanza).
    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        bot_local._pausa_visible(0)
        bot_local._pausa_visible(-3)
        bot_local._pausa_visible("no-es-numero")
    check(
        "pausa visible: valores invalidos/<=0 no duermen",
        dormir.call_count == 0,
    )


def test_constructor_default(check):
    check(
        "constructor: modo_local default False (sin cambios)",
        TwitterBot("cuenta").modo_local is False,
    )
    check(
        "constructor: modo_local=True por keyword",
        TwitterBot("cuenta", modo_local=True).modo_local is True,
    )
    check(
        "constructor: modo_local=True posicional",
        TwitterBot("cuenta", True).modo_local is True,
    )
    check(
        "constructor: modo_local normaliza a bool",
        TwitterBot("cuenta", modo_local=1).modo_local is True,
    )


def run(check):
    test_firma_buscar_url(check)
    test_cita_sin_buscar_url(check)
    test_cita_con_buscar_url(check)
    test_cita_fallback_perfil(check)
    test_cita_sin_buscar_url_perfil(check)
    test_cita_prefiere_toast(check)
    test_cambiar_cuenta_cache(check)
    test_cambiar_cuenta_local_sin_refresh(check)
    test_pausa_humana(check)
    test_pausa_visible_tope_local(check)
    test_constructor_default(check)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_tests import check as _check, resumen as _resumen  # noqa: E402

    print("=== test_selenium_local_speed.py ===")
    run(_check)
    sys.exit(_resumen())
