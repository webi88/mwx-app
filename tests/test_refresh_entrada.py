"""Suite del REFRESH DE ENTRADA de `plataformas/twitter/selenium_bot.py`.

Cuando se ENTRA a una cuenta de X (cambio de cuenta en la pestaña persistente
del motor y primera entrada de la campaña LOCAL) las cookies se inyectan por
CDP SIN navegar: la pagina puede quedar desactualizada o a medio cargar. El
`TwitterBot.refrescar_pagina_rapido()` hace COMO MAXIMO `REFRESH_ENTRADA_MAX`
refrescos reales (default 2) y `cambiar_cuenta` lo llama automaticamente
(`refrescar=True`, default).

Verifica, SIN Chrome ni red y sin tocar `data/`:

  - Contrato/envs: `REFRESH_AL_ENTRAR` (0/false/no/off apaga, sin tocar el
    navegador), `REFRESH_ENTRADA_MAX` (default 2, clamp 0..4, 0 = sin
    refrescos) y `REFRESH_ENTRADA_TIMEOUT` (default 3.0, clamp 1.5..10).
  - Camino SANO en x.com: EXACTAMENTE 2 `refresh()`, `page_load_timeout`
    acotado/restaurado, espera de `document.readyState` activa y corta
    (poll 0.1s, tope ~1.0s) y CERO recuperaciones/navegaciones.
  - Cortes rapidos: si un refresh deja `login`/onboarding o `anti_bot` se
    detiene ahi (no hace el 2º ni recuperacion). Interstitial que persiste:
    `error` tras los N refrescos, SIN `navegar_tolerante`.
  - Login/onboarding por URL (`/i/flow/*`, `/i/jf/onboarding/*`,
    `account/access`, `login`) y por TEXTO visible con formulario de
    onboarding (telefono/fecha de nacimiento), sin falsos positivos en tweets.
  - `about:blank`/`chrome://` y pagina de error de Chrome: UNA navegacion
    tolerante a `/home` (get/recuperacion acotada, sin bucles).
  - Estados: "ok"/"login"/"anti_bot"/"error"/"driver"/"skip", `ultimo_refresh`
    persistido y NUNCA lanza (fakes sin `refresh`/`execute_script`/
    `current_url`, errores duros, TimeoutException, etc.).
  - `cambiar_cuenta(..., refrescar=True)` llama al refresh UNA vez DESPUES de
    inyectar cookies (fuera de modo local); con `refrescar=False` no lo llama;
    `login`/`anti_bot`/`error` no cambian el retorno y solo `driver` devuelve
    False. En `modo_local=True` NUNCA se refresca (aunque `refrescar=True` y
    `REFRESH_AL_ENTRAR=1`) y `ultimo_refresh` queda en "skip".
  - Sin proxies en `modo_local=True` (ni desde el refresh).

Uso:
    .venv/Scripts/python.exe tests/test_refresh_entrada.py
    .venv/Scripts/python.exe tests/run_tests.py
"""
from __future__ import annotations

import inspect
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import plataformas.twitter.selenium_bot as modulo  # noqa: E402
from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    TimeoutException,
    WebDriverException,
)

HOME = "https://x.com/home"
LOGIN = "https://x.com/i/flow/login"
ENVS_REFRESH = (
    "REFRESH_AL_ENTRAR",
    "REFRESH_ENTRADA_TIMEOUT",
    "REFRESH_ENTRADA_MAX",
)


@contextmanager
def _entorno(**valores):
    """Entorno limpio SOLO para las envs del refresh (restaura al salir)."""
    limpio = {k: v for k, v in os.environ.items() if k not in ENVS_REFRESH}
    limpio.update({k: str(v) for k, v in valores.items()})
    with mock.patch.dict(os.environ, limpio, clear=True):
        yield


class FakeVisible:
    """Elemento visible minimo (`is_displayed`) para el formulario onboarding."""

    def is_displayed(self):
        return True


class FakeDriver:
    """Driver minimo: registra refresh/get/scripts/timeouts (sin Chrome ni red).

    `execute_script` responde a la sonda DOM de `es_pagina_error_chrome`, al
    `document.readyState` del refresh y al texto visible (`innerText`). Con
    `ready_states` se simula "loading" antes de "complete". `refresh_effects`
    permite cambiar URL/DOM SOLO en la llamada N (1-based) para simular que un
    refresh deja login/onboarding o cura un interstitial. Todos los errores son
    inyectables por atributo.
    """

    def __init__(
        self,
        url=HOME,
        title="Home / X",
        page_source="<html>home</html>",
        ready_state="complete",
        ready_states=None,
        visible_text="",
        error_chrome_dom=False,
        refresh_effects=None,
    ):
        self._url = url
        self.title = title
        self.page_source = page_source
        self.ready_state = ready_state
        self.ready_states = list(ready_states or [])
        self.visible_text = visible_text
        self.error_chrome_dom = error_chrome_dom
        self.page_load_timeout = 60
        self.timeouts = []
        self.refresh_calls = 0
        self.gets = []
        self.scripts = []
        self.cdp = []
        self.refresh_exc = None
        self.get_exc = None
        self.script_exc = None
        self.refresh_after = None  # dict aplicado en CADA refresh
        self.refresh_effects = dict(refresh_effects or {})  # {n_llamada: dict}
        self.get_after = None  # dict(url=..., page_source=..., title=...)
        # Selectores -> elementos visibles (formulario de onboarding). Por
        # defecto NINGUN elemento (como una pagina normal de X).
        self.elementos_por_selector = {}
        # Metodo por INSTANCIA para poder borrarlo en subclases (fakes sin
        # `refresh`), sin tocar la clase real de Selenium.
        self.refresh = self._hacer_refresh

    @property
    def current_url(self):
        return self._url

    def set_page_load_timeout(self, valor):
        self.page_load_timeout = valor
        self.timeouts.append(valor)

    def execute_script(self, script, *argumentos):
        self.scripts.append((script, argumentos))
        if self.script_exc is not None:
            raise self.script_exc
        if "readyState" in script:
            if self.ready_states:
                return self.ready_states.pop(0)
            return self.ready_state
        if "main-frame-error" in script or "neterror" in script:
            return self.error_chrome_dom
        if "innerText" in script:
            return self.visible_text
        return None

    def execute_cdp_cmd(self, comando, parametros):
        self.cdp.append((comando, parametros))
        return {}

    def find_elements(self, by, sel):
        return list(self.elementos_por_selector.get(sel, []))

    def get(self, url):
        if self.get_exc is not None:
            raise self.get_exc
        self.gets.append(url)
        self._url = url
        if isinstance(self.get_after, dict):
            self._url = self.get_after.get("url", self._url)
            self.page_source = self.get_after.get(
                "page_source", self.page_source
            )
            self.title = self.get_after.get("title", self.title)
        return None

    def _aplicar_efecto(self, efecto):
        if not isinstance(efecto, dict):
            return
        self._url = efecto.get("url", self._url)
        self.page_source = efecto.get("page_source", self.page_source)
        self.title = efecto.get("title", self.title)
        if "visible_text" in efecto:
            self.visible_text = efecto["visible_text"]

    def _hacer_refresh(self):
        self.refresh_calls += 1
        if self.refresh_exc is not None:
            raise self.refresh_exc
        efecto = self.refresh_effects.get(self.refresh_calls)
        if isinstance(efecto, dict):
            self._aplicar_efecto(efecto)
        elif isinstance(self.refresh_after, dict):
            self._aplicar_efecto(self.refresh_after)


class FakeSinRefresh(FakeDriver):
    """Fake SIN `refresh`: el refresh de entrada debe devolver "skip"."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        del self.refresh


class FakeSinScript(FakeDriver):
    """Fake SIN `execute_script`: no rompe la espera de readyState."""

    execute_script = None


class FakeSinUrl(FakeDriver):
    """Fake sin `current_url`: refresh "skip" y `cambiar_cuenta` sigue True."""

    @property
    def current_url(self):
        raise AttributeError("sin current_url")


class FakeUrlRota(FakeDriver):
    """`current_url` lanza (driver muerto): refresh "driver"."""

    @property
    def current_url(self):
        raise WebDriverException("invalid session id")


class RelojFalso:
    """Reloj simulado: `sleep` avanza el tiempo sin esperar de verdad."""

    def __init__(self):
        self.t = 0.0

    def ahora(self):
        return self.t

    def dormir(self, segundos):
        self.t += max(0.0, float(segundos))


def _bot(driver=None, usuario="cuenta_a", modo_local=False):
    bot = TwitterBot(usuario, modo_local=modo_local)
    bot.driver = driver if driver is not None else FakeDriver()
    return bot


def _parchear_cambiar(bot, preparar=True, refrescar=None):
    """Parches comunes de `cambiar_cuenta` (sin UA/proxy/cache reales)."""
    from contextlib import ExitStack

    pila = ExitStack()
    pila.enter_context(
        mock.patch.object(bot, "_limpiar_cache_no_bloqueante")
    )
    pila.enter_context(
        mock.patch.object(bot, "preparar_sesion_cdp", return_value=preparar)
    )
    pila.enter_context(
        mock.patch.object(bot, "_obtener_ua_consistente", return_value="")
    )
    pila.enter_context(mock.patch.object(bot, "_obtener_proxy", return_value=""))
    pila.enter_context(mock.patch.object(bot, "_proxy_para_x", return_value=""))
    if refrescar is not None:
        pila.enter_context(
            mock.patch.object(bot, "refrescar_pagina_rapido", refrescar)
        )
    return pila


def _con_form_onboarding(driver, texto):
    """Marca un formulario de onboarding visible y el texto visible dado."""
    driver.visible_text = texto
    driver.elementos_por_selector = {
        TwitterBot._SELECTOR_FORM_ONBOARDING: [FakeVisible()]
    }
    return driver


def test_firmas(check):
    parametros = list(inspect.signature(TwitterBot.cambiar_cuenta).parameters)
    check(
        "firma: cambiar_cuenta conserva sus parametros previos en orden",
        parametros[:4] == ["self", "usuario", "proxy", "validar_proxy"],
        str(parametros),
    )
    firma = inspect.signature(TwitterBot.cambiar_cuenta).parameters
    check(
        "firma: `refrescar` es el ULTIMO parametro de cambiar_cuenta",
        parametros[-1] == "refrescar",
        str(parametros),
    )
    check(
        "firma: `refrescar` default True (entra refrescando por defecto)",
        firma["refrescar"].default is True,
    )
    check(
        "firma: cambiar_cuenta no agrega parametros despues de refrescar",
        len(parametros) == 5,
        str(parametros),
    )
    firma_refresh = inspect.signature(
        TwitterBot.refrescar_pagina_rapido
    ).parameters
    check(
        "firma: refrescar_pagina_rapido(self, timeout=3.0)",
        list(firma_refresh) == ["self", "timeout"]
        and firma_refresh["timeout"].default == 3.0,
        str(firma_refresh),
    )
    check(
        "espera readyState: tope <= 1.0s",
        TwitterBot._REFRESH_READY_TIMEOUT <= 1.0,
        str(TwitterBot._REFRESH_READY_TIMEOUT),
    )
    check(
        "espera readyState: poll <= 0.2s",
        0 < TwitterBot._REFRESH_READY_POLL <= 0.2,
        str(TwitterBot._REFRESH_READY_POLL),
    )


def test_camino_sano_dos_refrescos(check):
    driver = FakeDriver(url=HOME)
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        resultado = bot.refrescar_pagina_rapido()
    check("x.com/home: devuelve ok", resultado == "ok", resultado)
    check(
        "x.com/home: EXACTAMENTE 2 refrescos (default REFRESH_ENTRADA_MAX=2)",
        driver.refresh_calls == 2,
        f"refresh={driver.refresh_calls}",
    )
    check(
        "x.com/home: NO navega (sin get)",
        driver.gets == [],
        str(driver.gets),
    )
    check(
        "x.com/home: NUNCA llama a navegar_tolerante",
        navegar.call_count == 0,
        f"llamadas={navegar.call_count}",
    )
    check(
        "x.com/home: ultimo_refresh queda en ok",
        bot.ultimo_refresh == "ok",
        bot.ultimo_refresh,
    )
    check(
        "x.com/home: no toca ultimo_error en un caso sano",
        bot.ultimo_error == "",
        bot.ultimo_error,
    )
    check(
        "x.com/home: espera readyState con execute_script",
        any("readyState" in script for script, _ in driver.scripts),
        str(driver.scripts),
    )

    # /compose y /status tambien son X: 2 refrescos, sin navegar.
    for ruta in ("https://x.com/compose/post", "https://x.com/alguien/status/1"):
        driver_ruta = FakeDriver(url=ruta)
        with _entorno():
            estado = _bot(driver_ruta).refrescar_pagina_rapido()
        check(
            f"{ruta}: 2 refrescos y ok",
            estado == "ok" and driver_ruta.refresh_calls == 2
            and driver_ruta.gets == [],
            f"estado={estado} refresh={driver_ruta.refresh_calls} "
            f"gets={driver_ruta.gets}",
        )


def test_limite_refrescos_env(check):
    for valor, esperado in (
        ("0", 0),
        ("1", 1),
        ("2", 2),
        ("4", 4),
        ("99", 4),  # clamp superior
        ("-3", 0),  # clamp inferior
        ("x", 2),  # invalido -> default
        ("", 2),  # vacio -> default
    ):
        driver = FakeDriver()
        bot = _bot(driver)
        with _entorno(REFRESH_ENTRADA_MAX=valor):
            estado = bot.refrescar_pagina_rapido()
        check(
            f"REFRESH_ENTRADA_MAX={valor!r}: {esperado} refresh",
            driver.refresh_calls == esperado,
            f"refresh={driver.refresh_calls}",
        )
        check(
            f"REFRESH_ENTRADA_MAX={valor!r}: sigue devolviendo ok",
            estado == "ok" and bot.ultimo_refresh == "ok",
            f"estado={estado} ultimo_refresh={bot.ultimo_refresh}",
        )

    # Con 0 refrescos, una pagina de LOGIN se detecta sin tocar el navegador.
    driver = FakeDriver(url=LOGIN, title="Log in / X")
    bot = _bot(driver)
    with _entorno(REFRESH_ENTRADA_MAX="0"):
        estado = bot.refrescar_pagina_rapido()
    check(
        "REFRESH_ENTRADA_MAX=0 en login: 0 refresh y login",
        estado == "login" and driver.refresh_calls == 0,
        f"estado={estado} refresh={driver.refresh_calls}",
    )
    check(
        "REFRESH_ENTRADA_MAX=0 en login: deja error de sesion expirada",
        "sesión de X expirada" in (bot.ultimo_error or ""),
        bot.ultimo_error,
    )


def test_env_apagado(check):
    for valor in ("0", "false", "no", "off", "FALSE", "Off"):
        driver = FakeDriver()
        bot = _bot(driver)
        with _entorno(REFRESH_AL_ENTRAR=valor):
            resultado = bot.refrescar_pagina_rapido()
        check(
            f"REFRESH_AL_ENTRAR={valor!r}: devuelve skip",
            resultado == "skip",
            resultado,
        )
        check(
            f"REFRESH_AL_ENTRAR={valor!r}: NO refresh ni get ni scripts",
            driver.refresh_calls == 0
            and driver.gets == []
            and driver.scripts == [],
            f"refresh={driver.refresh_calls} gets={driver.gets} "
            f"scripts={len(driver.scripts)}",
        )
        check(
            f"REFRESH_AL_ENTRAR={valor!r}: ultimo_refresh=skip",
            bot.ultimo_refresh == "skip",
            bot.ultimo_refresh,
        )

    # Vacio/ausente = activo por defecto (2 refrescos).
    driver = FakeDriver()
    with _entorno(REFRESH_AL_ENTRAR=""):
        estado = _bot(driver).refrescar_pagina_rapido()
    check(
        "REFRESH_AL_ENTRAR vacio: activo (2 refrescos)",
        estado == "ok" and driver.refresh_calls == 2,
        f"estado={estado} refresh={driver.refresh_calls}",
    )


def test_navegacion_si_no_esta_en_x(check):
    # about:blank -> UNA navegacion a /home con navegar_tolerante.
    driver = FakeDriver(url="about:blank", title="", page_source="")
    driver.get_after = {
        "url": HOME,
        "title": "Home / X",
        "page_source": "<html>home</html>",
    }
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check(
        "about:blank: navega a x.com/home y devuelve ok",
        estado == "ok" and driver.gets == [HOME],
        f"estado={estado} gets={driver.gets}",
    )
    check(
        "about:blank: NO hace refresh (solo get)",
        driver.refresh_calls == 0,
        f"refresh={driver.refresh_calls}",
    )

    # chrome://newtab: mismo camino.
    driver = FakeDriver(url="chrome://newtab/", title="New Tab", page_source="")
    driver.get_after = {
        "url": HOME,
        "title": "Home / X",
        "page_source": "<html>home</html>",
    }
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check(
        "chrome://newtab: navega a x.com/home y devuelve ok",
        estado == "ok" and driver.gets == [HOME],
        f"estado={estado} gets={driver.gets}",
    )

    # Pagina de error de Chrome: UNA navegacion (navegar_tolerante) y nada mas.
    driver = FakeDriver(
        url="chrome-error://chromewebdata/",
        title="x.com",
        page_source="<html>ERR_CONNECTION_RESET</html>",
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "pagina de error de Chrome: UNA navegacion de recuperacion",
        navegar.call_count == 1,
        f"llamadas={navegar.call_count}",
    )
    check(
        "pagina de error de Chrome: navega a x.com/home",
        navegar.call_args.args == (HOME,),
        str(navegar.call_args),
    )
    check(
        "pagina de error de Chrome: timeout_total acotado (min(3+7,20)=10)",
        navegar.call_args.kwargs.get("timeout_total") == 10.0,
        str(navegar.call_args),
    )
    check(
        "pagina de error de Chrome: devuelve ok (mapeado)",
        estado == "ok",
        estado,
    )
    check(
        "pagina de error de Chrome: ultimo_refresh=ok",
        bot.ultimo_refresh == "ok",
        bot.ultimo_refresh,
    )

    # La pagina de error de Chrome tambien se recupera si la URL dice x.com
    # (la sonda DOM la detecta bajo el mismo host).
    driver = FakeDriver(url=HOME, title="x.com", error_chrome_dom=True)
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "error de Chrome bajo x.com: recupera con navegacion",
        estado == "ok" and navegar.call_count == 1,
        f"estado={estado} llamadas={navegar.call_count}",
    )


def test_interstitial_sin_recuperacion_extra(check):
    # Interstitial persistente: 2 refrescos y "error", sin navegacion extra.
    driver = FakeDriver(
        url=HOME,
        title="X",
        page_source="<html>Something went wrong. Try again.</html>",
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "interstitial persistente: hace los 2 refrescos",
        driver.refresh_calls == 2,
        f"refresh={driver.refresh_calls}",
    )
    check(
        "interstitial persistente: devuelve error",
        estado == "error",
        estado,
    )
    check(
        "interstitial persistente: NUNCA recupera navegando",
        navegar.call_count == 0,
        f"llamadas={navegar.call_count}",
    )
    check(
        "interstitial persistente: ultimo_refresh=error",
        bot.ultimo_refresh == "error",
        bot.ultimo_refresh,
    )
    check(
        "interstitial persistente: deja ultimo_error claro",
        "sin recuperacion" in (bot.ultimo_error or "").lower(),
        bot.ultimo_error,
    )

    # El 2º refresh lo cura: ok, con 2 refrescos y sin navegacion.
    driver = FakeDriver(
        url=HOME,
        title="X",
        page_source="<html>Something went wrong.</html>",
        refresh_effects={
            1: {
                "url": HOME,
                "title": "X",
                "page_source": "<html>Something went wrong.</html>",
            },
            2: {
                "url": HOME,
                "title": "Home / X",
                "page_source": "<html>home</html>",
            },
        },
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "interstitial que se cura en el 2º refresh: ok",
        estado == "ok" and driver.refresh_calls == 2,
        f"estado={estado} refresh={driver.refresh_calls}",
    )
    check(
        "interstitial curado: sin recuperacion de navegacion",
        navegar.call_count == 0,
        f"llamadas={navegar.call_count}",
    )


def test_login_onboarding_corta_el_primer_refresh(check):
    # Login ya visible: 1 refresco y corta (no gasta el 2º).
    driver = FakeDriver(url=LOGIN, title="Log in / X")
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check("login: devuelve login", estado == "login", estado)
    check(
        "login: deja ultimo_error de sesion expirada",
        "sesión de X expirada" in (bot.ultimo_error or ""),
        bot.ultimo_error,
    )
    check("login: ultimo_refresh=login", bot.ultimo_refresh == "login")
    check(
        "login: 1 refresh y corta (sin 2º ni recuperacion)",
        driver.refresh_calls == 1 and navegar.call_count == 0,
        f"refresh={driver.refresh_calls} recuperaciones={navegar.call_count}",
    )

    # El PRIMER refresh deja login: corta ahi, no hace el 2º.
    driver = FakeDriver(
        url=HOME,
        refresh_effects={
            1: {"url": LOGIN, "title": "Log in / X"},
        },
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "primer refresh deja login: devuelve login",
        estado == "login",
        estado,
    )
    check(
        "primer refresh deja login: SOLO 1 refresh (sin 2º)",
        driver.refresh_calls == 1,
        f"refresh={driver.refresh_calls}",
    )
    check(
        "primer refresh deja login: sin recuperacion",
        navegar.call_count == 0,
        f"recuperaciones={navegar.call_count}",
    )

    # Onboarding por URL: la pantalla del telefono corta al instante.
    for url in (
        "https://x.com/i/jf/onboarding/web",
        "https://x.com/i/flow/signup",
        "https://x.com/i/flow/consent",
    ):
        driver = FakeDriver(url=url, title="X")
        bot = _bot(driver)
        with _entorno():
            estado = bot.refrescar_pagina_rapido()
        check(
            f"{url}: devuelve login/onboarding",
            estado == "login",
            estado,
        )
        check(
            f"{url}: 1 refresh y corta",
            driver.refresh_calls == 1,
            f"refresh={driver.refresh_calls}",
        )
        check(
            f"{url}: ultimo_error de sesion invalida",
            "sesión de X expirada" in (bot.ultimo_error or ""),
            bot.ultimo_error,
        )


def test_onboarding_por_texto_y_sin_falsos_positivos(check):
    for texto in (
        "Introduce tu número de teléfono",
        "Agrega un número de teléfono",
        "Add phone number",
        "Introduce tu número",
        "Crea tu cuenta",
        "Create account",
        "Regístrate",
        "Sign up",
        "Fecha de nacimiento",
        "Date of birth",
    ):
        driver = _con_form_onboarding(FakeDriver(url=HOME), texto)
        bot = _bot(driver)
        with _entorno():
            estado = bot.refrescar_pagina_rapido()
        check(
            f"onboarding por texto {texto!r}: login",
            estado == "login" and bot.ultimo_refresh == "login",
            f"estado={estado} ultimo_refresh={bot.ultimo_refresh}",
        )
        check(
            f"onboarding por texto {texto!r}: 1 refresh y corta",
            driver.refresh_calls == 1,
            f"refresh={driver.refresh_calls}",
        )

    # Un TWEET que menciona esas frases NO debe marcar la sesion como caida:
    # no hay formulario de onboarding visible.
    driver = FakeDriver(
        url="https://x.com/alguien/status/1",
        title="Alguien en X",
        page_source="<html>Create account and sign up, add phone number</html>",
        visible_text="Create account and sign up, add phone number",
    )
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check(
        "tweet con 'Create account'/'Sign up': NO es login (ok)",
        estado == "ok" and driver.refresh_calls == 2,
        f"estado={estado} refresh={driver.refresh_calls}",
    )
    check(
        "tweet con frases de onboarding: no deja error",
        bot.ultimo_error == "",
        bot.ultimo_error,
    )


def test_estados_anti_bot(check):
    driver = FakeDriver(
        url="https://x.com/account/access?__cf_chl_rt_tk=abc",
        title="Just a moment...",
        page_source="<html>__cf_chl verifying you are human</html>",
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check("anti-bot: devuelve anti_bot", estado == "anti_bot", estado)
    check(
        "anti-bot: ultimo_refresh=anti_bot",
        bot.ultimo_refresh == "anti_bot",
        bot.ultimo_refresh,
    )
    check(
        "anti-bot: 1 refresh y corta (sin 2º ni recuperacion)",
        driver.refresh_calls == 1 and navegar.call_count == 0,
        f"refresh={driver.refresh_calls} recuperaciones={navegar.call_count}",
    )
    check(
        "anti-bot: NO marca cuenta suspendida ni error de login",
        bot.cuenta_suspendida is False
        and "suspendida" not in (bot.ultimo_error or "").lower(),
        bot.ultimo_error,
    )
    check(
        "anti-bot: deja ultimo_error claro (sin pedir password)",
        "anti-bot" in (bot.ultimo_error or "").lower()
        or "cloudflare" in (bot.ultimo_error or "").lower(),
        bot.ultimo_error,
    )

    # Interstitial anti-bot en x.com/home (sin URL especial): corta tras el 1º.
    driver = FakeDriver(
        url=HOME,
        title="X",
        page_source="<html>verifying you are human</html>",
    )
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "anti-bot en x.com/home: anti_bot tras 1 refresh",
        estado == "anti_bot" and driver.refresh_calls == 1,
        f"estado={estado} refresh={driver.refresh_calls}",
    )
    check(
        "anti-bot en x.com/home: sin recuperacion",
        navegar.call_count == 0,
        f"recuperaciones={navegar.call_count}",
    )


def test_driver_roto_y_timeouts(check):
    # Sin driver -> "driver".
    bot = TwitterBot("cuenta_a")
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("sin driver: devuelve driver", estado == "driver", estado)
    check("sin driver: ultimo_refresh=driver", bot.ultimo_refresh == "driver")

    # current_url lanza (driver muerto) -> "driver".
    bot = _bot(FakeUrlRota())
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("current_url roto: devuelve driver", estado == "driver", estado)
    check(
        "current_url roto: guarda ultimo_error",
        "invalid session id" in (bot.ultimo_error or "").lower(),
        bot.ultimo_error,
    )

    # refresh con error DURO -> "driver" en el primer intento.
    driver = FakeDriver()
    driver.refresh_exc = WebDriverException("invalid session id")
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("refresh con error duro: devuelve driver", estado == "driver", estado)
    check(
        "refresh con error duro: corta en el primer refresh",
        driver.refresh_calls == 1,
        f"refresh={driver.refresh_calls}",
    )
    check(
        "refresh con error duro: ultimo_refresh=driver",
        bot.ultimo_refresh == "driver",
        bot.ultimo_refresh,
    )

    # refresh lento (TimeoutException): se tolera y se evalua el estado.
    driver = FakeDriver()
    driver.refresh_exc = TimeoutException("timed out")
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("refresh con TimeoutException: se tolera y devuelve ok", estado == "ok", estado)
    check(
        "refresh con TimeoutException: completa los 2 refrescos",
        driver.refresh_calls == 2,
        f"refresh={driver.refresh_calls}",
    )
    check(
        "refresh con TimeoutException: ultimo_refresh=ok",
        bot.ultimo_refresh == "ok",
        bot.ultimo_refresh,
    )

    # WebDriverException transitoria de red: se tolera.
    driver = FakeDriver()
    driver.refresh_exc = WebDriverException("net::ERR_CONNECTION_RESET")
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check("refresh con error de red transitorio: devuelve ok", estado == "ok", estado)

    # Excepcion generica: NUNCA lanza; se evalua el estado.
    driver = FakeDriver()
    driver.refresh_exc = RuntimeError("hipo del driver")
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check("refresh con excepcion generica: no lanza y devuelve ok", estado == "ok", estado)


def test_fakes_sin_api(check):
    # Sin refresh: "skip" y cambiar_cuenta sigue True.
    driver = FakeSinRefresh()
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("fake sin refresh: devuelve skip", estado == "skip", estado)
    check("fake sin refresh: ultimo_refresh=skip", bot.ultimo_refresh == "skip")
    check(
        "fake sin refresh: no navega ni lanza",
        driver.gets == [] and driver.refresh_calls == 0,
        f"gets={driver.gets} refresh={driver.refresh_calls}",
    )
    with _entorno(), _parchear_cambiar(bot):
        ok = bot.cambiar_cuenta("cuenta_b")
    check(
        "fake sin refresh: cambiar_cuenta sigue devolviendo True",
        ok is True,
        bot.ultimo_error,
    )
    check(
        "fake sin refresh: cambiar_cuenta deja ultimo_refresh=skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )

    # Sin execute_script: refresh OK (2) y no rompe la espera.
    driver = FakeSinScript()
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("fake sin execute_script: devuelve ok", estado == "ok", estado)
    check(
        "fake sin execute_script: SI hizo los 2 refrescos",
        driver.refresh_calls == 2,
        f"refresh={driver.refresh_calls}",
    )

    # Sin current_url (AttributeError): "skip" en el refresh.
    driver = FakeSinUrl()
    bot = _bot(driver)
    with _entorno():
        estado = bot.refrescar_pagina_rapido()
    check("fake sin current_url: devuelve skip", estado == "skip", estado)
    check("fake sin current_url: ultimo_refresh=skip", bot.ultimo_refresh == "skip")

    # ...y cambiar_cuenta sigue True (esta_vivo se fuerza: el fake no expone
    # current_url y el flujo real de cambiar_cuenta exige un driver vivo).
    driver = FakeSinUrl()
    bot = _bot(driver)
    with _entorno(), mock.patch.object(
        bot, "esta_vivo", return_value=True
    ), _parchear_cambiar(bot):
        ok = bot.cambiar_cuenta("cuenta_b")
    check(
        "fake sin current_url: cambiar_cuenta sigue True",
        ok is True,
        bot.ultimo_error,
    )
    check(
        "fake sin current_url: refresh de entrada fue skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )

    # execute_script que lanza en readyState: se tolera.
    driver = FakeDriver()
    driver.script_exc = WebDriverException("renderer ocupado")
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check("execute_script que lanza: no rompe y devuelve ok", estado == "ok", estado)

    # page_source que lanza (fake sin esa API): tampoco rompe.
    class FakeSinSource(FakeDriver):
        @property
        def page_source(self):
            raise AttributeError("sin page_source")

        @page_source.setter
        def page_source(self, valor):
            # El constructor base asigna page_source; se ignora a proposito.
            pass

    driver = FakeSinSource()
    with _entorno():
        estado = _bot(driver).refrescar_pagina_rapido()
    check("fake sin page_source: devuelve ok", estado == "ok", estado)
    check(
        "fake sin page_source: SI hizo los 2 refrescos",
        driver.refresh_calls == 2,
        f"refresh={driver.refresh_calls}",
    )


def test_timeout_acotado_y_ready_state(check):
    # Clamp inferior: env 1 -> 1.5.
    driver = FakeDriver()
    with _entorno(REFRESH_ENTRADA_TIMEOUT="1"):
        _bot(driver).refrescar_pagina_rapido()
    check(
        "REFRESH_ENTRADA_TIMEOUT=1: se acota a 1.5s",
        driver.timeouts and driver.timeouts[0] == 1.5,
        str(driver.timeouts),
    )

    # Clamp superior: env 100 -> 10.
    driver = FakeDriver()
    with _entorno(REFRESH_ENTRADA_TIMEOUT="100"):
        _bot(driver).refrescar_pagina_rapido()
    check(
        "REFRESH_ENTRADA_TIMEOUT=100: se acota a 10s",
        driver.timeouts and driver.timeouts[0] == 10.0,
        str(driver.timeouts),
    )

    # Invalido -> default 3.
    driver = FakeDriver()
    with _entorno(REFRESH_ENTRADA_TIMEOUT="no-es-numero"):
        _bot(driver).refrescar_pagina_rapido()
    check(
        "REFRESH_ENTRADA_TIMEOUT invalido: default 3s",
        driver.timeouts and driver.timeouts[0] == 3.0,
        str(driver.timeouts),
    )

    # Parametro `timeout` se usa cuando no hay env.
    driver = FakeDriver()
    with _entorno():
        _bot(driver).refrescar_pagina_rapido(timeout=5)
    check(
        "timeout=5 sin env: page_load_timeout 5s",
        driver.timeouts and driver.timeouts[0] == 5.0,
        str(driver.timeouts),
    )

    # Parametro invalido -> 3.
    driver = FakeDriver()
    with _entorno():
        _bot(driver).refrescar_pagina_rapido(timeout="no-es-numero")
    check(
        "timeout invalido: default 3s",
        driver.timeouts and driver.timeouts[0] == 3.0,
        str(driver.timeouts),
    )

    # Restaura el timeout PREVIO despues del refresh.
    driver = FakeDriver()
    driver.page_load_timeout = 45
    with _entorno(REFRESH_ENTRADA_TIMEOUT="8"):
        _bot(driver).refrescar_pagina_rapido()
    check(
        "page_load_timeout: se acota a 8s durante el refresh",
        driver.timeouts and driver.timeouts[0] == 8.0,
        str(driver.timeouts),
    )
    check(
        "page_load_timeout: se RESTAURA al previo (45s)",
        driver.page_load_timeout == 45
        and driver.timeouts[-1] == 45.0,
        f"final={driver.page_load_timeout} {driver.timeouts}",
    )

    # ReadyState "loading" -> poll 0.1s hasta "complete".
    driver = FakeDriver(ready_states=["loading", "loading", "complete"])
    with _entorno(REFRESH_ENTRADA_TIMEOUT="8"), mock.patch.object(
        modulo.time, "sleep"
    ) as dormir:
        estado = _bot(driver).refrescar_pagina_rapido()
    check(
        "readyState: espera activa hasta complete",
        estado == "ok",
        estado,
    )
    check(
        "readyState: poll de 0.1s (2 esperas simuladas)",
        [c.args[0] for c in dormir.call_args_list] == [0.1, 0.1],
        str(dormir.call_args_list),
    )

    # ReadyState que NUNCA queda listo: presupuesto acotado (sin bucle).
    driver = FakeDriver(ready_state="loading")
    with _entorno(REFRESH_ENTRADA_TIMEOUT="8"), mock.patch.object(
        TwitterBot, "_REFRESH_READY_TIMEOUT", 0.0
    ), mock.patch.object(modulo.time, "sleep") as dormir:
        estado = _bot(driver).refrescar_pagina_rapido()
    check(
        "readyState: tope acotado (no espera si ya vencio)",
        estado == "ok" and dormir.call_count == 0,
        f"estado={estado} sleeps={dormir.call_count}",
    )


def test_rendimiento_camino_sano(check):
    # Camino sano: 2 refrescos, cero navegaciones y esperas simuladas 0.
    driver = FakeDriver()
    bot = _bot(driver)
    navegar = mock.Mock(return_value="ok")
    with _entorno(), mock.patch.object(bot, "navegar_tolerante", navegar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "rendimiento: 2 refresh exactos y 0 recuperaciones",
        estado == "ok"
        and driver.refresh_calls == 2
        and navegar.call_count == 0
        and driver.gets == [],
        f"estado={estado} refresh={driver.refresh_calls} "
        f"recuperaciones={navegar.call_count}",
    )

    # Renderer que NUNCA queda listo: cada refresh espera ~1.0s como maximo
    # (antes ~2.5s), asi que 2 refrescos ~2.0s simulados en total.
    driver = FakeDriver(ready_state="loading")
    bot = _bot(driver)
    reloj = RelojFalso()
    with _entorno(), mock.patch.object(
        bot, "_ahora", reloj.ahora
    ), mock.patch.object(modulo.time, "sleep", reloj.dormir):
        estado = bot.refrescar_pagina_rapido()
    check(
        "rendimiento: camino sano devuelve ok",
        estado == "ok",
        estado,
    )
    check(
        "rendimiento: 2 refrescos en ~2.0s simulados (tope 1s por refresh)",
        1.5 <= reloj.t <= 2.3,
        f"tiempo_simulado={reloj.t:.2f}s refresh={driver.refresh_calls}",
    )


def test_cambiar_cuenta_hook(check):
    # refrescar=True: el refresh corre UNA vez DESPUES de inyectar cookies.
    orden = []
    bot = _bot(FakeDriver())
    preparar = mock.Mock(side_effect=lambda: orden.append("cookies") or True)
    refrescar = mock.Mock(side_effect=lambda: orden.append("refresh") or "ok")
    with _entorno(), mock.patch.object(
        bot, "_limpiar_cache_no_bloqueante"
    ), mock.patch.object(
        bot, "preparar_sesion_cdp", preparar
    ), mock.patch.object(
        bot, "_obtener_ua_consistente", return_value=""
    ), mock.patch.object(
        bot, "_obtener_proxy", return_value=""
    ), mock.patch.object(
        bot, "_proxy_para_x", return_value=""
    ), mock.patch.object(
        bot, "refrescar_pagina_rapido", refrescar
    ):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
    check("cambiar_cuenta refrescar=True: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cambiar_cuenta refrescar=True: refresh UNA vez",
        refrescar.call_count == 1,
        f"llamadas={refrescar.call_count}",
    )
    check(
        "cambiar_cuenta: inyecta cookies ANTES del refresh",
        orden == ["cookies", "refresh"],
        str(orden),
    )

    # refrescar=False: NUNCA llama al refresh (comportamiento historico).
    bot = _bot(FakeDriver())
    refrescar = mock.Mock(return_value="ok")
    with _entorno(), _parchear_cambiar(bot, refrescar=refrescar):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=False)
    check(
        "cambiar_cuenta refrescar=False: devuelve True",
        ok is True,
        bot.ultimo_error,
    )
    check(
        "cambiar_cuenta refrescar=False: NO llama al refresh",
        refrescar.call_count == 0,
        f"llamadas={refrescar.call_count}",
    )

    # Sin sesion (preparar=False): no se refresca y devuelve False.
    bot = _bot(FakeDriver())
    refrescar = mock.Mock(return_value="ok")
    with _entorno(), _parchear_cambiar(
        bot, preparar=False, refrescar=refrescar
    ):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
    check(
        "cambiar_cuenta sin cookies: devuelve False",
        ok is False,
    )
    check(
        "cambiar_cuenta sin cookies: NO llama al refresh",
        refrescar.call_count == 0,
        f"llamadas={refrescar.call_count}",
    )

    # Estados best-effort: login/anti_bot/error NO cambian el retorno.
    for estado in ("login", "anti_bot", "error", "skip"):
        bot = _bot(FakeDriver())
        with _entorno(), _parchear_cambiar(
            bot, refrescar=mock.Mock(return_value=estado)
        ):
            ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
        check(
            f"cambiar_cuenta refresh={estado}: sigue devolviendo True",
            ok is True,
            bot.ultimo_error,
        )

    # Solo "driver" invalida la cuenta.
    bot = _bot(FakeDriver())
    with _entorno(), _parchear_cambiar(
        bot, refrescar=mock.Mock(return_value="driver")
    ):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
    check(
        "cambiar_cuenta refresh=driver: devuelve False",
        ok is False,
    )
    check(
        "cambiar_cuenta refresh=driver: deja ultimo_error claro",
        "driver inservible" in (bot.ultimo_error or "").lower(),
        bot.ultimo_error,
    )


def test_sin_proxies(check):
    # El refresh NUNCA consulta proxies.
    driver = FakeDriver()
    bot = _bot(driver, modo_local=True)
    obtener = mock.Mock(return_value="http://proxy-cuenta:8080")
    validar = mock.Mock(return_value="http://proxy-cuenta:8080")
    with _entorno(), mock.patch.object(
        TwitterBot, "_obtener_proxy", obtener
    ), mock.patch.object(TwitterBot, "_proxy_para_x", validar):
        estado = bot.refrescar_pagina_rapido()
    check(
        "modo local: el refresh no consulta proxies",
        estado == "ok" and obtener.call_count == 0 and validar.call_count == 0,
        f"estado={estado} obtener={obtener.call_count} "
        f"validar={validar.call_count}",
    )

    # cambiar_cuenta en modo local + refrescar=True: sigue ignorando proxies
    # y NUNCA refresca la pagina (maxima velocidad del plan local).
    bot = _bot(FakeDriver(), modo_local=True)
    obtener = mock.Mock(return_value="http://proxy-cuenta:8080")
    validar = mock.Mock(return_value="http://proxy-cuenta:8080")
    refrescar = mock.Mock(return_value="ok")
    with _entorno(REFRESH_AL_ENTRAR="1"), mock.patch.object(
        bot, "_obtener_proxy", obtener
    ), mock.patch.object(
        bot, "_proxy_para_x", validar
    ), _parchear_cambiar(bot, refrescar=refrescar):
        ok = bot.cambiar_cuenta(
            "cuenta_b", proxy="http://proxy-explicito:8080", validar_proxy=True
        )
    check(
        "modo local: cambiar_cuenta con refresh no consulta proxies",
        ok is True and obtener.call_count == 0 and validar.call_count == 0,
        f"ok={ok} obtener={obtener.call_count} validar={validar.call_count}",
    )
    check(
        "modo local: cambiar_cuenta NUNCA llama refrescar_pagina_rapido",
        refrescar.call_count == 0,
        f"llamadas={refrescar.call_count}",
    )
    check(
        "modo local: cambiar_cuenta deja ultimo_refresh=skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )

    # Sin modo local y sin proxy en la cuenta: tampoco se toca `_obtener_proxy`
    # (el refresh va por la sesion sticky ya aplicada).
    bot = _bot(FakeDriver())
    obtener = mock.Mock(return_value="")
    with _entorno(), mock.patch.object(TwitterBot, "_obtener_proxy", obtener):
        estado = bot.refrescar_pagina_rapido()
    check(
        "refresh sin proxy: no resuelve proxys por cuenta",
        estado == "ok" and obtener.call_count == 0,
        f"estado={estado} llamadas={obtener.call_count}",
    )


def test_env_apagado_en_cambiar_cuenta(check):
    driver = FakeDriver()
    bot = _bot(driver)
    with _entorno(REFRESH_AL_ENTRAR="0"), _parchear_cambiar(bot):
        ok = bot.cambiar_cuenta("cuenta_b", refrescar=True)
    check(
        "REFRESH_AL_ENTRAR=0: cambiar_cuenta devuelve True",
        ok is True,
        bot.ultimo_error,
    )
    check(
        "REFRESH_AL_ENTRAR=0: ni refresh ni get (comportamiento historico)",
        driver.refresh_calls == 0 and driver.gets == [],
        f"refresh={driver.refresh_calls} gets={driver.gets}",
    )
    check(
        "REFRESH_AL_ENTRAR=0: ultimo_refresh=skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )


def test_ultimo_refresh_se_actualiza(check):
    # Cada ejecucion sobreescribe el estado para diagnostico.
    driver = FakeDriver(url="about:blank", title="", page_source="")
    driver.get_after = {
        "url": HOME,
        "title": "Home / X",
        "page_source": "<html>home</html>",
    }
    bot = _bot(driver)
    with _entorno():
        bot.refrescar_pagina_rapido()
    check(
        "ultimo_refresh: navegacion ok queda en ok",
        bot.ultimo_refresh == "ok",
        bot.ultimo_refresh,
    )
    bot.ultimo_refresh = "anterior"
    with _entorno(REFRESH_AL_ENTRAR="0"):
        bot.refrescar_pagina_rapido()
    check(
        "ultimo_refresh: se sobreescribe con skip",
        bot.ultimo_refresh == "skip",
        bot.ultimo_refresh,
    )
    check(
        "ultimo_refresh: se inicializa vacio en el constructor",
        TwitterBot("cuenta_x").ultimo_refresh == "",
    )


def run(check):
    test_firmas(check)
    test_camino_sano_dos_refrescos(check)
    test_limite_refrescos_env(check)
    test_env_apagado(check)
    test_navegacion_si_no_esta_en_x(check)
    test_interstitial_sin_recuperacion_extra(check)
    test_login_onboarding_corta_el_primer_refresh(check)
    test_onboarding_por_texto_y_sin_falsos_positivos(check)
    test_estados_anti_bot(check)
    test_driver_roto_y_timeouts(check)
    test_fakes_sin_api(check)
    test_timeout_acotado_y_ready_state(check)
    test_rendimiento_camino_sano(check)
    test_cambiar_cuenta_hook(check)
    test_sin_proxies(check)
    test_env_apagado_en_cambiar_cuenta(check)
    test_ultimo_refresh_se_actualiza(check)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_tests import check as _check, resumen as _resumen  # noqa: E402

    print("=== test_refresh_entrada.py ===")
    run(_check)
    sys.exit(_resumen())
