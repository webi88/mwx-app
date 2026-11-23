"""Suite rapida del MODO LOCAL de `plataformas/twitter/selenium_bot.py` (sin Chrome).

Campaña LOCAL en la computadora del dueño: Chrome VISIBLE, SIN proxy y con las
pausas de confirmacion visual recortadas. Verifica, sin abrir Chrome ni tocar
`data/`:

  - `TwitterBot.__init__(usuario, modo_local=False)` guarda el flag; default
    False (comportamiento historico).
  - `iniciar_driver(..., modo_local=None)` acepta el kwarg AL FINAL y conserva
    los parametros previos (`pantalla_externa`, `proxy_dinamico`).
  - Con `settings.headless=True` y `modo_local=True`: las opciones NO llevan
    `--headless`; NO se llama `_proxy_para_x()` ni `ProxyManager
    .aplicar_a_options`; `page_load_timeout` y `script_timeout` quedan en 30
    (en vez de 60). La ventana local es 1366x768.
  - Sin modo local: `--headless=new` (si `settings.headless`), se consulta el
    proxy y los timeouts quedan en 60 (byte-identico al flujo previo).
  - `_pausa_visible`: en modo local duerme <=0.2s (5s -> 0.2s); sin modo local
    duerme EXACTAMENTE el valor pedido; en headless (sin modo local) no duerme.
  - `cambiar_cuenta` en modo local IGNORA proxies (no valida ni avisa), NO
    refresca la pagina y sigue inyectando cookies por CDP.

Los fakes/monkeypatches cubren `crear_chrome`, `detectar_chrome_version`,
`aplicar_stealth`, `aplicar_user_agent` y `ProxyManager.aplicar_a_options`.
`settings.profiles_dir` se apunta a un directorio TEMPORAL para no tocar
`data/perfiles_chrome/`.

Uso:
    .venv/Scripts/python.exe tests/test_selenium_local.py   (solo este archivo)
    .venv/Scripts/python.exe tests/run_tests.py
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import plataformas.twitter.selenium_bot as modulo  # noqa: E402
from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402


class FakeDriver:
    """Driver minimo: registra timeouts y llamadas CDP (sin red ni Chrome)."""

    def __init__(self):
        self.page_load_timeout = None
        self.script_timeout = None
        self.pagina_timeout = []
        self.script_timeout_llamadas = []
        self.cdp = []
        self.cookies = []
        self._url = "https://x.com/home"

    # API usada por `iniciar_driver`.
    def set_page_load_timeout(self, valor):
        self.page_load_timeout = valor
        self.pagina_timeout.append(valor)

    def set_script_timeout(self, valor):
        self.script_timeout = valor
        self.script_timeout_llamadas.append(valor)

    # API usada por `guardar_sesion_actual`.
    def get_cookies(self):
        return list(self.cookies)

    # API usada por `esta_vivo`/`cambiar_cuenta`.
    @property
    def current_url(self):
        return self._url

    def execute_cdp_cmd(self, comando, parametros):
        self.cdp.append((comando, parametros))
        return {}


class FakeForwardProxy:
    """Forwarder de mentira: solo registra los cambios de upstream."""

    def __init__(self):
        self.upstreams = []

    def cambiar_upstream(self, proxy):
        self.upstreams.append(proxy)


def _ejecutar_iniciar_driver(bot, **kwargs):
    """Corre `iniciar_driver` con TODO lo externo monkeypatcheado.

    Devuelve `(ok, opciones_capturadas, driver_falso, mocks)`.
    """
    capturado = {}
    driver = FakeDriver()
    proxy_mock = mock.Mock(return_value="")
    proxy_para_x = mock.Mock(return_value="http://proxy-de-prueba:8080")
    forwarder = FakeForwardProxy()
    aplicar_options = mock.Mock(return_value=forwarder)

    def _crear_chrome(options, version_main=None, **kw):
        capturado["options"] = options
        capturado["version_main"] = version_main
        return driver

    class _ProxyManagerFalso:
        def aplicar_a_options(self, options, proxy, tag="", dinamico=False):
            return aplicar_options(options, proxy, tag=tag, dinamico=dinamico)

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(modulo, "crear_chrome", _crear_chrome), mock.patch.object(
            modulo, "detectar_chrome_version", return_value=153
        ), mock.patch.object(modulo, "aplicar_stealth") as stealth, mock.patch.object(
            modulo, "aplicar_user_agent"
        ) as aplicar_ua, mock.patch.object(
            modulo, "ProxyManager", _ProxyManagerFalso
        ), mock.patch.object(
            modulo.settings, "profiles_dir", Path(tmp)
        ), mock.patch.object(
            TwitterBot, "_obtener_ua_consistente", return_value=""
        ), mock.patch.object(
            TwitterBot, "_proxy_para_x", proxy_para_x
        ):
            ok = bot.iniciar_driver(**kwargs)

    return ok, capturado, driver, {
        "proxy_para_x": proxy_para_x,
        "aplicar_options": aplicar_options,
        "stealth": stealth,
        "aplicar_ua": aplicar_ua,
    }


def test_constructor_y_firma(check):
    bot = TwitterBot("cuenta_local")
    check(
        "constructor: modo_local default False (retrocompatible)",
        bot.modo_local is False,
    )
    bot_local = TwitterBot("cuenta_local", modo_local=True)
    check("constructor: modo_local=True se guarda como bool", bot_local.modo_local is True)
    check(
        "constructor: acepta modo_local por keyword",
        TwitterBot("cuenta_local", modo_local=1).modo_local is True,
    )

    parametros = inspect.signature(TwitterBot.iniciar_driver).parameters
    nombres = list(parametros)
    check(
        "iniciar_driver: conserva parametros previos en orden",
        nombres[:3] == ["self", "pantalla_externa", "proxy_dinamico"],
        str(nombres),
    )
    check(
        "iniciar_driver: modo_local es el ULTIMO parametro (kwarg)",
        nombres[-1] == "modo_local" and len(nombres) == 4,
        str(nombres),
    )
    check(
        "iniciar_driver: defaults previos intactos (False/False) y modo_local None",
        parametros["pantalla_externa"].default is False
        and parametros["proxy_dinamico"].default is False
        and parametros["modo_local"].default is None,
    )


def test_iniciar_driver_modo_local(check):
    with mock.patch.object(modulo.settings, "headless", True):
        bot = TwitterBot("cuenta_local", modo_local=True)
        ok, capturado, driver, mocks = _ejecutar_iniciar_driver(bot)

    argumentos = list(capturado["options"].arguments)
    check("modo local: iniciar_driver devuelve True", ok is True)
    check(
        "modo local: NUNCA agrega --headless (aunque settings.headless=True)",
        "--headless=new" not in argumentos and "--headless" not in argumentos,
        str(argumentos),
    )
    check(
        "modo local: ventana visible 1366x768",
        "--window-size=1366,768" in argumentos,
        str(argumentos),
    )
    check(
        "modo local: NO llama _proxy_para_x (sin validacion de proxies)",
        mocks["proxy_para_x"].call_count == 0,
        f"llamadas={mocks['proxy_para_x'].call_count}",
    )
    check(
        "modo local: NO aplica proxy al navegador",
        mocks["aplicar_options"].call_count == 0,
    )
    check(
        "modo local: page_load_timeout = 30",
        driver.page_load_timeout == 30,
        str(driver.page_load_timeout),
    )
    check(
        "modo local: script_timeout = 30",
        driver.script_timeout == 30,
        str(driver.script_timeout),
    )
    check(
        "modo local: driver asignado al bot",
        bot.driver is driver,
    )
    check(
        "modo local: siguen aplicandose stealth y page_load_strategy eager",
        mocks["stealth"].call_count == 1
        and capturado["options"].page_load_strategy == "eager",
    )


def test_iniciar_driver_kwarg_explicito(check):
    # Bot "normal" + `iniciar_driver(modo_local=True)`: el kwarg manda y deja
    # la sesion en modo local (lo leen `cambiar_cuenta` y `_pausa_visible`).
    with mock.patch.object(modulo.settings, "headless", True):
        bot = TwitterBot("cuenta_normal")
        ok, capturado, driver, mocks = _ejecutar_iniciar_driver(
            bot, modo_local=True
        )

    argumentos = list(capturado["options"].arguments)
    check("iniciar_driver(modo_local=True): devuelve True", ok is True)
    check(
        "iniciar_driver(modo_local=True): sin --headless y visible",
        "--headless=new" not in argumentos
        and "--window-size=1366,768" in argumentos,
        str(argumentos),
    )
    check(
        "iniciar_driver(modo_local=True): no consulta proxy",
        mocks["proxy_para_x"].call_count == 0,
    )
    check(
        "iniciar_driver(modo_local=True): persiste el modo en el bot",
        bot.modo_local is True,
    )
    check(
        "iniciar_driver(modo_local=True): timeouts 30",
        driver.page_load_timeout == 30 and driver.script_timeout == 30,
    )


def test_iniciar_driver_sin_modo_local(check):
    # Regresion: sin modo local el flujo es el de siempre.
    with mock.patch.object(modulo.settings, "headless", True):
        bot = TwitterBot("cuenta_servidor")
        ok, capturado, driver, mocks = _ejecutar_iniciar_driver(bot)

    argumentos = list(capturado["options"].arguments)
    check("sin modo local: devuelve True", ok is True)
    check(
        "sin modo local: --headless=new presente con settings.headless=True",
        "--headless=new" in argumentos,
        str(argumentos),
    )
    check(
        "sin modo local: SI consulta/valida el proxy (comportamiento previo)",
        mocks["proxy_para_x"].call_count == 1,
        f"llamadas={mocks['proxy_para_x'].call_count}",
    )
    check(
        "sin modo local: aplica el proxy cuando corresponde",
        mocks["aplicar_options"].call_count == 1,
    )
    check(
        "sin modo local: timeouts de 60 (byte-identico al flujo previo)",
        driver.page_load_timeout == 60 and driver.script_timeout == 60,
        f"{driver.page_load_timeout}/{driver.script_timeout}",
    )


def test_iniciar_driver_local_con_proxy(check):
    # Modo local + proxy_dinamico=True: usa el proxy sticky de la cuenta y
    # crea SIEMPRE el forwarder (para cambiar el upstream al cambiar cuenta).
    with mock.patch.object(modulo.settings, "headless", True):
        bot = TwitterBot("cuenta_local_proxy", modo_local=True)
        ok, capturado, driver, mocks = _ejecutar_iniciar_driver(
            bot, proxy_dinamico=True
        )

    argumentos = list(capturado["options"].arguments)
    check(
        "modo local con proxy: iniciar_driver devuelve True y sigue visible",
        ok is True
        and "--headless=new" not in argumentos
        and "--window-size=1366,768" in argumentos,
        str(argumentos),
    )
    check(
        "modo local con proxy: SI consulta/valida _proxy_para_x",
        mocks["proxy_para_x"].call_count == 1,
        f"llamadas={mocks['proxy_para_x'].call_count}",
    )
    check(
        "modo local con proxy: forwarder dinamico aplicado y guardado",
        mocks["aplicar_options"].call_count == 1
        and mocks["aplicar_options"].call_args.kwargs.get("dinamico") is True
        and bot._fwd_proxy is not None
        and bot._local_con_proxy is True,
        str(mocks["aplicar_options"].call_args),
    )
    check(
        "modo local con proxy: timeouts 30",
        driver.page_load_timeout == 30 and driver.script_timeout == 30,
    )


def test_guardar_sesion_actual(check):
    bot = TwitterBot("cuenta_sesion")
    check(
        "guardar_sesion_actual: sin driver devuelve False",
        bot.guardar_sesion_actual() is False,
    )
    bot.driver = FakeDriver()
    check(
        "guardar_sesion_actual: driver sin cookies devuelve False",
        bot.guardar_sesion_actual() is False,
    )
    bot.driver.cookies = [
        {"name": "auth_token", "value": "abc"},
        {"name": "cf_clearance", "value": "xyz"},
    ]
    guardadas = []
    with mock.patch.object(
        TwitterBot, "_brandear_cuenta", lambda self, cookies: guardadas.append(cookies)
    ):
        ok = bot.guardar_sesion_actual()
    check(
        "guardar_sesion_actual: persiste TODAS las cookies (cf_clearance incluida)",
        ok is True
        and guardadas
        and {c["name"] for c in guardadas[0]} == {"auth_token", "cf_clearance"},
        str(guardadas),
    )

    class _DriverRoto(FakeDriver):
        def get_cookies(self):
            raise RuntimeError("driver roto")

    bot_roto = TwitterBot("rota")
    bot_roto.driver = _DriverRoto()
    check(
        "guardar_sesion_actual: nunca lanza con driver roto",
        bot_roto.guardar_sesion_actual() is False,
    )


def test_cambiar_cuenta_local_con_proxy(check):
    # Local CON forwarder: valida el proxy al cambiar de cuenta y mueve el
    # upstream en caliente (sin cache ni refresh, como el modo local).
    bot = TwitterBot("cuenta_a", modo_local=True)
    bot._local_con_proxy = True
    bot._fwd_proxy = FakeForwardProxy()
    bot.driver = FakeDriver()
    proxy_para_x = mock.Mock(return_value="http://proxy-validado:8080")
    refrescar = mock.Mock(return_value="ok")

    with mock.patch.object(TwitterBot, "_proxy_para_x", proxy_para_x), mock.patch.object(
        TwitterBot, "preparar_sesion_cdp", return_value=True
    ), mock.patch.object(
        TwitterBot, "_limpiar_cache_no_bloqueante"
    ) as cache, mock.patch.object(
        TwitterBot, "refrescar_pagina_rapido", refrescar
    ), mock.patch.object(
        TwitterBot, "_obtener_ua_consistente", return_value=""
    ):
        ok = bot.cambiar_cuenta(
            "cuenta_b", proxy="http://proxy-explicito:8080"
        )

    check("cambiar_cuenta local con proxy: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cambiar_cuenta local con proxy: SI valida contra x.com",
        proxy_para_x.call_count == 1,
        f"llamadas={proxy_para_x.call_count}",
    )
    check(
        "cambiar_cuenta local con proxy: mueve el upstream en caliente",
        bot._fwd_proxy.upstreams == ["http://proxy-validado:8080"],
        str(bot._fwd_proxy.upstreams),
    )
    check(
        "cambiar_cuenta local con proxy: sigue SIN cache ni refresh",
        cache.call_count == 0 and refrescar.call_count == 0,
    )


def test_pausa_visible(check):
    # Modo local: recorta a <=0.2s (5s -> 0.2s) y tambien recorta 0.9s.
    bot = TwitterBot("cuenta_local", modo_local=True)
    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        bot._pausa_visible(5.0)
    valores = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa local: 5s se recorta a <=0.2s",
        valores == [0.2],
        str(valores),
    )

    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        bot._pausa_visible(0.9)
    valores = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa local: 0.9s tambien se recorta a 0.2s",
        valores == [0.2],
        str(valores),
    )

    # Modo no local visible: EXACTAMENTE el valor pedido (5s / 3s).
    bot_servidor = TwitterBot("cuenta_servidor")
    with mock.patch.object(modulo.settings, "headless", False), mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_servidor._pausa_visible(5.0)
        bot_servidor._pausa_visible(3.0)
    valores = [c.args[0] for c in dormir.call_args_list]
    check(
        "pausa no local: duerme EXACTAMENTE 5s y 3s",
        valores == [5.0, 3.0],
        str(valores),
    )

    # Modo no local + headless: no hay pantalla que mirar => NO duerme.
    with mock.patch.object(modulo.settings, "headless", True), mock.patch(
        "plataformas.twitter.selenium_bot.time.sleep"
    ) as dormir:
        bot_servidor._pausa_visible(5.0)
        bot_servidor._pausa_visible(3.0)
    check(
        "pausa headless (sin modo local): NO duerme",
        dormir.call_count == 0,
        f"llamadas={dormir.call_count}",
    )

    # Borde: valores invalidos/0 no duermen (nunca lanza).
    with mock.patch("plataformas.twitter.selenium_bot.time.sleep") as dormir:
        bot._pausa_visible(0)
        bot._pausa_visible(-3)
        bot._pausa_visible("no-es-numero")
    check(
        "pausa: valores invalidos/<=0 no duermen",
        dormir.call_count == 0,
    )


def test_cambiar_cuenta_modo_local_ignora_proxy(check):
    bot = TwitterBot("cuenta_a", modo_local=True)
    bot.driver = FakeDriver()
    proxy_para_x = mock.Mock(return_value="http://proxy-de-prueba:8080")
    obtener_proxy = mock.Mock(return_value="http://proxy-cuenta:8080")
    refrescar = mock.Mock(return_value="ok")

    with mock.patch.object(TwitterBot, "_proxy_para_x", proxy_para_x), mock.patch.object(
        TwitterBot, "_obtener_proxy", obtener_proxy
    ), mock.patch.object(
        TwitterBot, "preparar_sesion_cdp", return_value=True
    ) as preparar_cdp, mock.patch.object(
        TwitterBot, "_limpiar_cache_no_bloqueante"
    ), mock.patch.object(
        TwitterBot, "refrescar_pagina_rapido", refrescar
    ), mock.patch.object(
        TwitterBot, "_obtener_ua_consistente", return_value=""
    ):
        ok = bot.cambiar_cuenta(
            "cuenta_b", proxy="http://proxy-explicito:8080", validar_proxy=True
        )

    check("cambiar_cuenta local: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cambiar_cuenta local: NO consulta el proxy de la cuenta",
        obtener_proxy.call_count == 0,
    )
    check(
        "cambiar_cuenta local: NO valida proxies contra x.com",
        proxy_para_x.call_count == 0,
    )
    check(
        "cambiar_cuenta local: NO refresca la pagina",
        refrescar.call_count == 0,
        f"llamadas={refrescar.call_count}",
    )
    check(
        "cambiar_cuenta local: ultimo_refresh queda en skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )
    check(
        "cambiar_cuenta local: conserva la inyeccion de cookies por CDP",
        preparar_cdp.call_count == 1,
    )
    check(
        "cambiar_cuenta local: cambia el usuario activo",
        bot.usuario == "cuenta_b",
    )


def run(check):
    test_constructor_y_firma(check)
    test_iniciar_driver_modo_local(check)
    test_iniciar_driver_kwarg_explicito(check)
    test_iniciar_driver_sin_modo_local(check)
    test_iniciar_driver_local_con_proxy(check)
    test_guardar_sesion_actual(check)
    test_cambiar_cuenta_local_con_proxy(check)
    test_pausa_visible(check)
    test_cambiar_cuenta_modo_local_ignora_proxy(check)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_tests import check as _check, resumen as _resumen  # noqa: E402

    print("=== test_selenium_local.py ===")
    run(_check)
    sys.exit(_resumen())
