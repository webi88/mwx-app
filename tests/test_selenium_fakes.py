"""Suite rapida de regresion de `plataformas/twitter/selenium_bot.py` (sin Chrome).

Version permanente de los checks anti-Ghostban del pegado de texto en X y de los
arreglos de navegacion/login. Usa FakeDriver/FakeElement para simular, sin red y
sin Chrome, los casos reales de Railway:

  - `_pegar_texto`: DESTRUYE la capa `[data-testid="mask"]` antes de limpiar/
    escribir; el metodo primario es `editor.click()` (best effort) +
    `editor.send_keys(texto)` en UNA sola llamada (eventos REALES de teclado) y
    el fallback es `ActionChains(driver).send_keys(texto)` sobre el editable real
    enfocado por JS. NO usa CDP `Input.insertText` ni `execCommand` ni el
    portapapeles (X oculta los posts de la insercion silenciosa). Verifica el
    texto, re-localiza elementos stale (React) y conserva el mensaje final
    exacto si TODOS los metodos fallan.
  - Limpieza de borrador: `_limpiar_editor_x` / `_editor_con_restos` (X restaura
    el borrador del composer y el pegado lo acumulaba).
  - Pausa humana de 1.8-3.5s entre escribir y publicar en `publicar_tweet`,
    `responder_tweet` y la rama de CITA de `solo_retwittear`.
  - `_primer_editor_visible`: prefiere el editor NO ocluido por el mask y el
    editor del dialogo cuando `preferir_dialogo=True`.
  - `navegar_tolerante` / `_recuperar_interstitial`: hasta DOS refrescos para
    la pagina de error generica de X; NUNCA el refresh adicional para el
    challenge anti-bot (Cloudflare), login o driver roto.
  - `login_con_cookies` (.pkl): un muro de login (URL `login`/`/i/flow`/
    `account/access` o formulario VISIBLE) ya NO se declara "Login exitoso":
    cae a `login_con_cookies_json()` y, si tampoco hay cookies validas,
    devuelve False con error de sesion expirada y SIN marcar `cuenta_suspendida`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_selenium_fakes.py   (solo este archivo)

Determinista: no depende del reloj real (las esperas se neutralizan y
`navegar_tolerante` usa un reloj falso).
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
import types
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.common.exceptions import (  # noqa: E402
    ElementClickInterceptedException,
    ElementNotInteractableException,
    InvalidSessionIdException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402

from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402


@contextlib.contextmanager
def _sin_esperas():
    """Neutraliza `time.sleep` para que la suite sea rapida y determinista.

    Ningun check depende del tiempo real: cuando hace falta simular el reloj
    (`navegar_tolerante`), el propio test instala su reloj falso.
    """
    original = time.sleep
    time.sleep = lambda *args, **kwargs: None
    try:
        yield
    finally:
        time.sleep = original


# --------------------------------------------------------------------------- #
# Fakes: driver y elemento con la API de Selenium que usa el bot.
# --------------------------------------------------------------------------- #
class FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    @property
    def active_element(self):
        return self.driver.focused


class FakeElement:
    def __init__(
        self,
        driver,
        tag="div",
        contenteditable=True,
        texto="",
        stale=False,
        occluded=False,
        visible=True,
        click_falla=False,
    ):
        self.driver = driver
        self.tag = tag
        self.contenteditable = contenteditable
        self._texto = texto
        self.stale = stale
        self.occluded = occluded
        self.visible = visible
        self.click_falla = click_falla
        self.hijos = []
        self.seleccionado = False
        self.click_count = 0

    # --- API Selenium usada por el bot ---
    def resolver_editable(self):
        if self.es_editable():
            return self
        for h in self.hijos:
            r = h.resolver_editable()
            if r is not None:
                return r
        return None

    def es_editable(self):
        """True si el elemento acepta teclado (input/textarea/contenteditable)."""
        return self.tag in ("textarea", "input") or self.contenteditable

    def is_displayed(self):
        if self.stale:
            raise StaleElementReferenceException("stale")
        return self.visible

    @property
    def text(self):
        if self.stale:
            raise StaleElementReferenceException("stale")
        partes = [self._texto] + [h.text for h in self.hijos]
        return "\n".join(p for p in partes if p)

    def get_attribute(self, nombre):
        if self.stale:
            raise StaleElementReferenceException("stale")
        if nombre == "textContent":
            return self.text
        if nombre == "innerText":
            return self.text
        if nombre == "value":
            return self._texto
        if nombre == "id":
            return getattr(self, "id", None)
        return None

    def find_elements(self, by, sel):
        if self.stale:
            raise StaleElementReferenceException("stale")
        return list(self.hijos)

    def click(self):
        if self.stale:
            raise StaleElementReferenceException("stale")
        self.click_count += 1
        self.driver.eventos.append(("click", self))
        if self.click_falla:
            raise WebDriverException("click roto (test)")
        if self.driver.mask_presente:
            raise ElementClickInterceptedException(
                "mask intercepta el clic (test)"
            )

    def send_keys(self, *args):
        if self.stale:
            raise StaleElementReferenceException("stale")
        if args and args[0] in (Keys.CONTROL, Keys.COMMAND):
            tecla = args[-1]
            self.driver.eventos.append(("atajo", tecla))
            if tecla == "a":
                self.seleccionado = True
            return
        self.driver.eventos.append(("send_keys", tuple(args)))
        if self.driver.send_keys_lanza:
            raise WebDriverException("send_keys roto (test)")
        if not self.es_editable():
            # Realista: `element.send_keys` en un wrapper no interactuable
            # falla; el bot debe caer al fallback ActionChains enfocando el
            # editable real por JS.
            raise ElementNotInteractableException("no interactuable (test)")
        if args and self.driver.send_keys_escribe:
            self.set_contenido(str(args[0]), reemplazar=True)

    def set_contenido(self, valor, reemplazar=True):
        if reemplazar:
            self._texto = valor
        else:
            self._texto = f"{self._texto}{valor}"


class FakeDriver:
    def __init__(self):
        self.editores = []
        self.dialogos = []
        self.masks = []
        self.mask_presente = False
        self.js_falla = False
        self.send_keys_escribe = True
        self.send_keys_lanza = False
        self.actionchains_escribe = True
        self.focused = None
        self._ctrl_a = False
        # Orden cronologico de lo que pasa durante el pegado: permite verificar
        # que el mask se destruye ANTES de cualquier escritura y que la pausa
        # humana cae entre escribir y publicar.
        self.eventos = []
        self.cdp_calls = []
        self.js_calls = []
        self.script_calls = []
        self.get_calls = []
        self.refresh_calls = 0
        self.cookies_added = []
        self.page_load_timeout = 60
        self.current_url = "https://x.com/home"
        self.title = "X"
        self.page_source = ""
        self.switch_to = FakeSwitchTo(self)

    def set_page_load_timeout(self, valor):
        self.page_load_timeout = valor

    def get(self, url):
        self.get_calls.append(url)

    def refresh(self):
        self.refresh_calls += 1

    def add_cookie(self, cookie):
        self.cookies_added.append(dict(cookie))

    def get_cookies(self):
        return [dict(cookie) for cookie in self.cookies_added]

    def execute_script(self, script, *args):
        self.js_calls.append((script, args))
        if any(isinstance(a, FakeElement) and a.stale for a in args):
            raise StaleElementReferenceException("stale en JS")
        if self.js_falla:
            raise RuntimeError("JS no disponible (test)")
        if "document.readyState" in script:
            return "complete"
        if "data-testid" in script and ".remove()" in script:
            # JS exacto de `_pegar_texto`: destruye la capa `data-testid="mask"`.
            self.eventos.append(("destruir_mask",))
            self.masks = []
            self.mask_presente = False
            return None
        if "elementFromPoint" in script:
            el = args[0]
            return not el.occluded
        if "isContentEditable" in script:
            raiz = args[0]
            objetivo = raiz.resolver_editable()
            if objetivo is None:
                return False
            self.focused = objetivo
            if len(args) > 1 and args[1]:
                objetivo.seleccionado = True
            return True
        self.script_calls.append((script, args))
        return None

    def execute_cdp_cmd(self, cmd, params):
        self.cdp_calls.append((cmd, params))
        if cmd == "Input.dispatchKeyEvent":
            key = params.get("key")
            self.eventos.append(("cdp_tecla", key))
            if (
                params.get("type") == "rawKeyDown"
                and key == "a"
                and params.get("modifiers") == 2
            ):
                self._ctrl_a = True
            elif (
                params.get("type") == "rawKeyDown"
                and key == "Delete"
                and self._ctrl_a
                and self.focused is not None
            ):
                # Simula Ctrl+A + Delete: vacia el editable enfocado real.
                self.focused.set_contenido("", reemplazar=True)
                self._ctrl_a = False
        return {}

    def find_elements(self, by, sel):
        if sel == "[data-testid='mask']":
            return list(self.masks)
        if sel == "div[role='dialog']":
            return list(self.dialogos)
        return list(self.editores)


class FakeActionChains:
    """Reemplazo de `ActionChains` que registra la llamada (fallback W3C).

    Se parchea `plataformas.twitter.selenium_bot.ActionChains` SOLO en los tests
    de fallback: asi se verifica que el bot cae a
    `ActionChains(driver).send_keys(texto)` (eventos reales de teclado a nivel
    W3C) sin abrir Chrome, y que el texto llega al editable enfocado por JS.
    """

    def __init__(self, driver):
        self.driver = driver
        self._teclas = []

    def send_keys(self, *teclas):
        self._teclas.extend(teclas)
        return self

    def perform(self):
        texto = "".join(str(t) for t in self._teclas)
        self.driver.eventos.append(("actionchains", texto))
        if self.driver.actionchains_escribe and self.driver.focused is not None:
            self.driver.focused.set_contenido(texto, reemplazar=True)


@contextlib.contextmanager
def _actionchains_falso(driver, escribir=True):
    """Parchea `ActionChains` del bot por `FakeActionChains` durante el bloque."""
    driver.actionchains_escribe = escribir
    with mock.patch(
        "plataformas.twitter.selenium_bot.ActionChains", FakeActionChains
    ):
        yield


class FakeBoton:
    """Boton minimo para los puntos donde el bot solo hace `.click()`."""

    def __init__(self):
        self.click_count = 0

    def click(self):
        self.click_count += 1


class NavFake:
    """Driver minimo para `navegar_tolerante` (solo get/refresh/find_elements)."""

    def __init__(self):
        self.refresh_calls = 0
        self.get_calls = []
        self.page_load_timeout = 60
        self.current_url = "https://x.com/home"
        self.title = "X"
        self.page_source = ""

    def set_page_load_timeout(self, valor):
        self.page_load_timeout = valor

    def get(self, url):
        self.get_calls.append(url)

    def refresh(self):
        self.refresh_calls += 1

    def find_elements(self, by, sel):
        return []


class Reloj:
    """Reloj falso: `sleep` avanza el tiempo en vez de esperar de verdad."""

    def __init__(self, t=1000.0):
        self.t = t

    def ahora(self):
        return self.t

    def dormir(self, segundos):
        self.t += max(0.0, float(segundos))


def bot_con_driver(driver):
    """TwitterBot sin __init__ (no toca BD ni Chrome): solo lo que usa el test."""
    bot = TwitterBot.__new__(TwitterBot)
    bot.driver = driver
    bot.usuario = "cuenta_test"
    bot.ultimo_error = ""
    bot.ultima_url_publicada = ""
    bot._sesion_cdp = False
    bot.cuenta_suspendida = False
    return bot


class CuentaFake:
    """Cuenta minima para `login_con_cookies_json` (sin SQLAlchemy)."""

    def __init__(self, cookies_json=None, auth_token="", password="", totp_secret=""):
        self.cookies_json = cookies_json
        self.auth_token = auth_token
        self.password = password
        self.totp_secret = totp_secret


class SesionFake:
    """Context manager minimo que imita `with get_db_session() as db`."""

    def __init__(self, cuenta):
        self.cuenta = cuenta

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.cuenta


@contextlib.contextmanager
def _db_falsa(cuenta):
    """Parchea `core.database.get_db_session` (el bot lo importa en caliente)."""
    import core.database as core_database

    original = core_database.get_db_session
    core_database.get_db_session = lambda: SesionFake(cuenta)
    try:
        yield
    finally:
        core_database.get_db_session = original


@contextlib.contextmanager
def _pkl_falso(cookies):
    """Simula un `.pkl` existente con `pickle.load` mockeado (sin disco)."""
    with mock.patch("os.path.exists", return_value=True), mock.patch(
        "builtins.open", mock.mock_open(read_data=b"pkl")
    ), mock.patch(
        "plataformas.twitter.selenium_bot.pickle.load", return_value=list(cookies)
    ):
        yield


def preparar_bot_login(driver):
    """Bot con la ruta del `.pkl` y los detectores de pagina mockeados.

    Anti-bot/suspension se mockean a False para aislar el chequeo del muro de
    login (su precedencia se cubre en el check del anti-bot).
    """
    bot = bot_con_driver(driver)
    bot.base_url = "https://x.com"
    bot.cookies_path = "data/cookies/twitter/cuenta_test.pkl"
    bot.es_pagina_anti_bot = lambda: False
    bot._detectar_cuenta_propia_suspendida = lambda: False
    bot._hay_challenge_seguridad = lambda: False
    return bot


COOKIES_PKL = [
    {"name": "auth_token", "value": "pkl_viejo", "domain": ".x.com", "path": "/"}
]

COOKIES_JSON_VALIDAS = [
    {"name": "auth_token", "value": "bd_token", "domain": ".x.com", "path": "/"},
    {"name": "ct0", "value": "bd_ct0", "domain": ".x.com", "path": "/"},
    {"name": "twid", "value": "u=123", "domain": ".x.com", "path": "/"},
]


def fake_pyperclip(copias, lanzar=False, driver=None):
    """Modulo `pyperclip` falso: copia a `copias` o lanza PyperclipException."""
    modulo = types.ModuleType("pyperclip")

    class PyperclipException(Exception):
        pass

    def copy(texto):
        if lanzar:
            raise PyperclipException("no hay xclip (test)")
        copias.append(texto)
        if driver is not None:
            driver.clipboard = texto

    modulo.copy = copy
    modulo.PyperclipException = PyperclipException
    return modulo


@contextlib.contextmanager
def _pyperclip_falso(copias, lanzar=False, driver=None):
    """Instala el fake de pyperclip y restaura el modulo real al salir."""
    original = sys.modules.get("pyperclip")
    sys.modules["pyperclip"] = fake_pyperclip(copias, lanzar=lanzar, driver=driver)
    try:
        yield
    finally:
        if original is not None:
            sys.modules["pyperclip"] = original
        else:
            sys.modules.pop("pyperclip", None)


# --------------------------------------------------------------------------- #
# Ghostban: `_pegar_texto` con eventos REALES de teclado
# --------------------------------------------------------------------------- #
class GrabadorSleep:
    """`time.sleep` falso que registra los valores en la lista `eventos`."""

    def __init__(self, eventos):
        self.eventos = eventos

    def __call__(self, segundos):
        self.eventos.append(("sleep", round(float(segundos), 2)))


def _uniform_stub(registro):
    """`random.uniform` determinista: la pausa humana siempre vale 2.5s."""

    def stub(a, b):
        registro.append((float(a), float(b)))
        if (float(a), float(b)) == (1.8, 3.5):
            return 2.5
        return (float(a) + float(b)) / 2

    return stub


def _comprobar_pausa_humana(check, nombre, eventos, uniformes):
    """Verifica que la pausa 1.8-3.5s cae entre 'pegar' y 'boton'."""
    check(
        f"{nombre}: se pidio random.uniform(1.8, 3.5)",
        (1.8, 3.5) in uniformes,
        str(uniformes),
    )
    marcas = [e[0] for e in eventos]
    idx_pausa = next(
        (i for i, e in enumerate(eventos) if e == ("sleep", 2.5)), None
    )
    check(
        f"{nombre}: la pausa humana ocurre entre escribir y publicar",
        idx_pausa is not None
        and marcas.index("pegar") < idx_pausa < marcas.index("boton"),
        str(eventos),
    )


def test_mask_primero_y_send_keys(check):
    print("Ghostban: mask destruido antes de escribir + click/send_keys")
    driver = FakeDriver()
    driver.mask_presente = True
    driver.masks = [object()]
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    bot = bot_con_driver(driver)
    bot._pegar_texto(editor, "Texto con teclado real")
    check(
        "send_keys: el texto quedo en el editor",
        editor._texto == "Texto con teclado real",
        repr(editor._texto),
    )
    check(
        "mask: se elimino la capa [data-testid='mask']",
        driver.mask_presente is False and driver.masks == [],
    )
    js_exacto = (
        "document.querySelectorAll('[data-testid=\"mask\"]')"
        ".forEach(e => e.remove());"
    )
    check(
        "mask: se ejecuto el JS exacto de destruccion",
        any(script == js_exacto for script, _ in driver.js_calls),
        str([s for s, _ in driver.js_calls]),
    )
    marcas = [e[0] for e in driver.eventos]
    check(
        "mask: destruido ANTES del click y del send_keys",
        marcas.index("destruir_mask") < marcas.index("click")
        and marcas.index("destruir_mask") < marcas.index("send_keys"),
        str(driver.eventos),
    )
    check(
        "send_keys: hubo click previo en el editor",
        editor.click_count >= 1,
        f"clicks={editor.click_count}",
    )
    check(
        "send_keys: el texto se envio en UNA sola llamada",
        ("send_keys", ("Texto con teclado real",)) in driver.eventos,
        str(driver.eventos),
    )


def test_sin_metodos_silenciosos(check):
    print("Ghostban: ni CDP insertText, ni execCommand, ni pyperclip")
    driver = FakeDriver()
    driver.mask_presente = True
    driver.masks = [object()]
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    copias = []
    with _pyperclip_falso(copias, lanzar=False, driver=driver):
        bot = bot_con_driver(driver)
        bot._pegar_texto(editor, "Primario sin silenciosos")
    check(
        "silenciosos: sin Input.insertText en el camino primario",
        all(cmd != "Input.insertText" for cmd, _ in driver.cdp_calls),
        str(driver.cdp_calls),
    )
    check(
        "silenciosos: sin document.execCommand en el camino primario",
        all("execCommand" not in script for script, _ in driver.js_calls),
    )
    check("silenciosos: pyperclip nunca se uso (primario)", copias == [])

    # Camino de FALLBACK (send_keys roto): tampoco usa metodos silenciosos.
    driver2 = FakeDriver()
    driver2.send_keys_lanza = True
    editor2 = FakeElement(driver2, contenteditable=True)
    driver2.editores = [editor2]
    copias2 = []
    with _pyperclip_falso(copias2, lanzar=False, driver=driver2), _actionchains_falso(
        driver2
    ):
        bot2 = bot_con_driver(driver2)
        bot2._pegar_texto(editor2, "Fallback sin silenciosos")
    check(
        "silenciosos: sin Input.insertText en el fallback",
        all(cmd != "Input.insertText" for cmd, _ in driver2.cdp_calls),
    )
    check(
        "silenciosos: sin document.execCommand en el fallback",
        all("execCommand" not in script for script, _ in driver2.js_calls),
    )
    check("silenciosos: pyperclip nunca se uso (fallback)", copias2 == [])
    check(
        "silenciosos: el fallback escribio el texto",
        editor2._texto == "Fallback sin silenciosos",
        repr(editor2._texto),
    )


def test_click_falla_sigue_con_send_keys(check):
    print("Ghostban: si el click falla NO se aborta (send_keys enfoca por W3C)")
    driver = FakeDriver()
    editor = FakeElement(driver, contenteditable=True, click_falla=True)
    driver.editores = [editor]
    bot = bot_con_driver(driver)
    bot._pegar_texto(editor, "Texto pese al click roto")
    check(
        "click roto: el texto quedo en el editor",
        editor._texto == "Texto pese al click roto",
        repr(editor._texto),
    )
    marcas = [e[0] for e in driver.eventos]
    check(
        "click roto: se intento el click antes del send_keys",
        marcas.index("click") < marcas.index("send_keys"),
        str(driver.eventos),
    )
    check(
        "click roto: NO hizo falta el fallback ActionChains",
        "actionchains" not in marcas,
        str(driver.eventos),
    )


def test_fallback_actionchains(check):
    print("Ghostban: fallback ActionChains (send_keys roto y wrapper no editable)")
    # (a) send_keys roto en un contenteditable directo.
    driver = FakeDriver()
    driver.send_keys_lanza = True
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    with _actionchains_falso(driver):
        bot = bot_con_driver(driver)
        bot._pegar_texto(editor, "Texto por ActionChains")
    check(
        "actionchains: el texto quedo",
        editor._texto == "Texto por ActionChains",
        repr(editor._texto),
    )
    marcas = [e[0] for e in driver.eventos]
    check(
        "actionchains: se uso ActionChains(driver).send_keys",
        ("actionchains", "Texto por ActionChains") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "actionchains: fue el ULTIMO metodo (tras el send_keys primario)",
        "send_keys" in marcas and marcas.index("send_keys") < marcas.index("actionchains"),
        str(driver.eventos),
    )

    # (b) wrapper no editable con descendiente contenteditable (variante de X).
    driver2 = FakeDriver()
    wrapper = FakeElement(driver2, contenteditable=False)
    hijo = FakeElement(driver2, contenteditable=True)
    wrapper.hijos = [hijo]
    driver2.editores = [wrapper]
    with _actionchains_falso(driver2):
        bot2 = bot_con_driver(driver2)
        bot2._pegar_texto(wrapper, "Wrapper por ActionChains")
    check(
        "actionchains/wrapper: el texto queda en el descendiente contenteditable",
        hijo._texto == "Wrapper por ActionChains",
        repr(hijo._texto),
    )
    check(
        "actionchains/wrapper: se enfoco el editable real por JS",
        driver2.focused is hijo,
    )


def test_mask_js_falla_no_aborta(check):
    print("Ghostban: si el JS del mask falla el pegado continua")
    driver = FakeDriver()
    driver.js_falla = True
    driver.mask_presente = True
    driver.masks = [object()]
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    bot = bot_con_driver(driver)
    bot._pegar_texto(editor, "Texto sin JS de mask")
    check(
        "mask JS roto: el texto igual quedo (send_keys)",
        editor._texto == "Texto sin JS de mask",
        repr(editor._texto),
    )
    check(
        "mask JS roto: el fallo no tumbo el pegado (mask sigue montado)",
        driver.mask_presente is True,
    )


def test_limpieza_borrador(check):
    print("Ghostban: borrador restaurado por X se limpia antes de escribir")
    driver = FakeDriver()
    borrador = (
        "BORRADOR VIEJO QUE X RESTAURO EN EL COMPOSITOR Y QUE SE QUEDO PEGADO "
        "ENTRE INTENTOS DE LA MISMA CUENTA"
    )
    editor = FakeElement(driver, contenteditable=True, texto=borrador)
    driver.editores = [editor]
    bot = bot_con_driver(driver)
    check(
        "borrador: _editor_con_restos detecta el exceso",
        bot._editor_con_restos(editor, "Texto nuevo") > 0,
    )
    bot._pegar_texto(editor, "Texto nuevo")
    check(
        "borrador: el editor queda SOLO con el texto nuevo",
        editor._texto == "Texto nuevo",
        repr(editor._texto),
    )
    marcas = [e[0] for e in driver.eventos]
    check(
        "borrador: se uso la limpieza CDP (Ctrl+A/Delete) antes de escribir",
        ("cdp_tecla", "Delete") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "borrador: la limpieza ocurre despues de destruir el mask",
        marcas.index("destruir_mask") < marcas.index("cdp_tecla"),
        str(driver.eventos),
    )
    check(
        "borrador: _editor_con_restos queda en 0 tras escribir",
        bot._editor_con_restos(editor, "Texto nuevo") == 0,
    )


def test_stale_relocaliza(check):
    print("Ghostban: stale element -> re-localizacion y reintento")
    driver = FakeDriver()
    viejo = FakeElement(driver, contenteditable=True, stale=True)
    fresco = FakeElement(driver, contenteditable=True)
    driver.editores = [fresco]
    bot = bot_con_driver(driver)
    bot._pegar_texto(viejo, "Texto tras relocalizar")
    check(
        "stale: el texto queda en el elemento fresco",
        fresco._texto == "Texto tras relocalizar",
        repr(fresco._texto),
    )
    check(
        "stale: se re-localizo y se escribio en el fresco",
        ("send_keys", ("Texto tras relocalizar",)) in driver.eventos,
        str(driver.eventos),
    )


def test_mensaje_final_exacto(check):
    print("Ghostban: mensaje final exacto si TODO falla")
    driver = FakeDriver()
    driver.send_keys_escribe = False
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    with _actionchains_falso(driver, escribir=False), _pyperclip_falso([], lanzar=True):
        bot = bot_con_driver(driver)
        try:
            bot._pegar_texto(editor, "nada")
            error = None
        except Exception as e:  # noqa: BLE001
            error = str(e)
    check(
        "todo falla: mensaje final exacto",
        error == "no se pudo escribir el texto en el editor de X",
        repr(error),
    )


# --------------------------------------------------------------------------- #
# Pausa humana: relevo del tuit antes de publicar (1.8-3.5s)
# --------------------------------------------------------------------------- #
def test_pausa_humana_publicar_tweet(check):
    print("Pausa humana: publicar_tweet relee antes de buscar el boton")
    driver = FakeDriver()
    bot = bot_con_driver(driver)
    eventos = []
    uniformes = []
    bot._detectar_limite_cuenta = lambda: False
    bot._abrir_compositor = lambda: FakeElement(driver)
    bot._pegar_texto = lambda editor, texto: eventos.append(("pegar", texto))
    bot._verificar_publicacion = lambda: True
    bot._obtener_ultimo_enlace = lambda usuario: ""

    def spy_boton(timeout=10, texto=""):
        eventos.append(("boton",))
        return FakeBoton()

    bot._esperar_boton_post_habilitado = spy_boton
    with mock.patch("time.sleep", GrabadorSleep(eventos)), mock.patch(
        "plataformas.twitter.selenium_bot.random.uniform",
        side_effect=_uniform_stub(uniformes),
    ):
        resultado = bot.publicar_tweet("Texto de prueba")
    check(
        "pausa publicar_tweet: publicacion simulada OK",
        resultado is True,
        repr(resultado),
    )
    _comprobar_pausa_humana(check, "pausa publicar_tweet", eventos, uniformes)


def test_pausa_humana_responder_tweet(check):
    print("Pausa humana: responder_tweet relee antes de buscar el boton")
    driver = FakeDriver()
    bot = bot_con_driver(driver)
    eventos = []
    uniformes = []
    bot._recuperar_interstitial = lambda url: "ok"
    bot._hay_muro_login = lambda: False
    bot._asegurar_pagina_tweet = lambda url: None
    bot._detectar_limite_cuenta = lambda: False
    bot._esperar_article_tweet = lambda timeout=10: None
    bot._buscar_boton_responder = lambda timeout=12: FakeBoton()
    bot._esperar_editor_visible = lambda preferir_dialogo=False, **kw: FakeElement(driver)
    bot._pegar_texto = lambda editor, texto: eventos.append(("pegar", texto))
    bot._verificar_publicacion = lambda: True
    bot._obtener_url_respuesta = lambda url: ""

    def spy_boton(timeout=10, texto=""):
        eventos.append(("boton",))
        return FakeBoton()

    bot._esperar_boton_post_habilitado = spy_boton
    with mock.patch("time.sleep", GrabadorSleep(eventos)), mock.patch(
        "plataformas.twitter.selenium_bot.random.uniform",
        side_effect=_uniform_stub(uniformes),
    ):
        resultado = bot.responder_tweet(
            "https://x.com/a/status/1", "Comentario de prueba"
        )
    check(
        "pausa responder_tweet: respuesta simulada OK",
        resultado is True,
        repr(resultado),
    )
    _comprobar_pausa_humana(check, "pausa responder_tweet", eventos, uniformes)


# --------------------------------------------------------------------------- #
# P0-C: `_primer_editor_visible`
# --------------------------------------------------------------------------- #
def test_primer_editor_ocluido(check):
    print("P0-C: prefiere el editor NO ocluido")
    driver = FakeDriver()
    tapado = FakeElement(driver, occluded=True)
    limpio = FakeElement(driver, occluded=False)
    driver.editores = [tapado, limpio]
    bot = bot_con_driver(driver)
    elegido = bot._primer_editor_visible()
    check("editor visible: devuelve el NO ocluido", elegido is limpio)

    # con preferir_dialogo el editor del modal gana aunque el fondo no este ocluido
    driver2 = FakeDriver()
    fondo = FakeElement(driver2, occluded=False)
    dlg = FakeElement(driver2)
    en_dialogo = FakeElement(driver2, occluded=False)
    dlg.hijos = [en_dialogo]
    driver2.editores = [fondo]
    driver2.dialogos = [dlg]
    bot2 = bot_con_driver(driver2)
    elegido2 = bot2._primer_editor_visible(preferir_dialogo=True)
    check("editor visible: preferir_dialogo prioriza el modal", elegido2 is en_dialogo)

    # si NINGUNO pasa el chequeo, devuelve el primero visible
    driver3 = FakeDriver()
    a = FakeElement(driver3, occluded=True)
    b = FakeElement(driver3, occluded=True)
    driver3.editores = [a, b]
    bot3 = bot_con_driver(driver3)
    check(
        "editor visible: todos ocluidos -> el primero visible",
        bot3._primer_editor_visible() is a,
    )

    # si el chequeo JS falla, comportamiento anterior
    driver4 = FakeDriver()
    driver4.js_falla = True
    c = FakeElement(driver4)
    d = FakeElement(driver4)
    driver4.editores = [c, d]
    bot4 = bot_con_driver(driver4)
    check("editor visible: JS roto -> el primero visible", bot4._primer_editor_visible() is c)

    # sin editores visibles -> None
    driver5 = FakeDriver()
    oculto = FakeElement(driver5, visible=False)
    driver5.editores = [oculto]
    bot5 = bot_con_driver(driver5)
    check("editor visible: sin editores visibles -> None", bot5._primer_editor_visible() is None)

    # un solo visible: no se consulta el JS (sin alternativa)
    driver6 = FakeDriver()
    driver6.js_falla = True
    solo = FakeElement(driver6)
    driver6.editores = [solo]
    bot6 = bot_con_driver(driver6)
    check("editor visible: un solo candidato -> ese", bot6._primer_editor_visible() is solo)


# --------------------------------------------------------------------------- #
# P0-D: `solo_retwittear` (rama cita) usa preferir_dialogo=True + pausa humana
# --------------------------------------------------------------------------- #
def test_cita_prefiere_dialogo(check):
    print("P0-D: la cita pide el editor del dialogo y relee antes de publicar")
    driver = FakeDriver()
    bot = bot_con_driver(driver)

    eventos = []
    uniformes = []
    llamadas = {}

    def spy_editor(*args, **kwargs):
        llamadas.update(kwargs)
        return FakeElement(driver)

    bot._recuperar_interstitial = lambda url: "ok"
    bot._detectar_limite_cuenta = lambda: False
    bot._esperar_article_tweet = lambda timeout=8: None
    bot._hay_muro_login = lambda: False
    bot._buscar_boton_retweet = lambda: FakeBoton()
    bot._buscar_opcion_quote = lambda: FakeBoton()
    bot._esperar_editor_visible = spy_editor
    bot._pegar_texto = lambda editor, texto: eventos.append(("pegar", texto))
    bot._verificar_publicacion = lambda: True
    bot._obtener_ultimo_enlace = lambda usuario: ""

    def spy_boton_post():
        eventos.append(("boton",))
        return FakeBoton()

    bot._buscar_boton_post = spy_boton_post

    with mock.patch("time.sleep", GrabadorSleep(eventos)), mock.patch(
        "plataformas.twitter.selenium_bot.random.uniform",
        side_effect=_uniform_stub(uniformes),
    ):
        resultado = bot.solo_retwittear(
            ["https://x.com/alguien/status/1"],
            "cuenta_test",
            mensaje_cita="Comentario de cita",
        )
    check("cita: RT con cita reportado como exitoso", resultado.get("exitos") == 1, str(resultado))
    check(
        "cita: pide el editor del dialogo (preferir_dialogo=True)",
        llamadas.get("preferir_dialogo") is True,
        str(llamadas),
    )
    check(
        "cita: pega el mensaje de la cita",
        ("pegar", "Comentario de cita") in eventos,
        str(eventos),
    )
    _comprobar_pausa_humana(check, "pausa cita", eventos, uniformes)


# --------------------------------------------------------------------------- #
# P1: `navegar_tolerante` / `_recuperar_interstitial`
# --------------------------------------------------------------------------- #
def test_navegar_tolerante_p1(check):
    print("P1: navegar_tolerante hace 2 refrescos si el error persiste")
    original_sleep = time.sleep

    try:
        # Caso 1: error -> error -> ok con el SEGUNDO refresh
        reloj = Reloj()
        time.sleep = reloj.dormir
        driver = NavFake()
        bot = bot_con_driver(driver)
        bot._ahora = reloj.ahora
        estados = {0: "error", 1: "error", 2: "ok"}

        def estado():
            return estados.get(driver.refresh_calls, "ok")

        bot._estado_pagina = estado
        bot.es_pagina_anti_bot = lambda: False
        resultado = bot.navegar_tolerante("https://x.com/home")
        check("navegar: error tras 1er refresh -> ok con el 2do", resultado == "ok", resultado)
        check("navegar: se hicieron 2 refrescos", driver.refresh_calls == 2, f"{driver.refresh_calls}")

        # Caso 2: anti-bot -> NO hace el segundo refresh
        reloj = Reloj()
        time.sleep = reloj.dormir
        driver = NavFake()
        bot = bot_con_driver(driver)
        bot._ahora = reloj.ahora
        bot._estado_pagina = lambda: "error"
        bot.es_pagina_anti_bot = lambda: True
        resultado = bot.navegar_tolerante("https://x.com/home")
        check("navegar: anti-bot -> error", resultado == "error", resultado)
        check(
            "navegar: anti-bot nunca hace el 2do refresh",
            driver.refresh_calls == 1,
            f"{driver.refresh_calls}",
        )

        # Caso 3a: login en la PRIMERA deteccion -> no refresca
        reloj = Reloj()
        time.sleep = reloj.dormir
        driver = NavFake()
        bot = bot_con_driver(driver)
        bot._ahora = reloj.ahora
        bot._estado_pagina = lambda: "login"
        bot.es_pagina_anti_bot = lambda: False
        resultado = bot.navegar_tolerante("https://x.com/home")
        check("navegar: login directo -> login", resultado == "login", resultado)
        check(
            "navegar: login directo no refresca",
            driver.refresh_calls == 0,
            f"{driver.refresh_calls}",
        )

        # Caso 3b: error y luego login -> solo el primer refresh (jamas el adicional)
        reloj = Reloj()
        time.sleep = reloj.dormir
        driver = NavFake()
        bot = bot_con_driver(driver)
        bot._ahora = reloj.ahora
        secuencia = ["error", "login"]

        def estado_login():
            return secuencia.pop(0) if secuencia else "login"

        bot._estado_pagina = estado_login
        bot.es_pagina_anti_bot = lambda: False
        resultado = bot.navegar_tolerante("https://x.com/home")
        check("navegar: error y luego login -> login", resultado == "login", resultado)
        check(
            "navegar: error->login solo refresca una vez",
            driver.refresh_calls == 1,
            f"{driver.refresh_calls}",
        )

        # Caso 4: driver roto en el get -> "driver" sin refrescos
        reloj = Reloj()
        time.sleep = reloj.dormir
        driver = NavFake()

        def get_roto(url):
            raise InvalidSessionIdException("sesion muerta")

        driver.get = get_roto
        bot = bot_con_driver(driver)
        bot._ahora = reloj.ahora
        bot._estado_pagina = lambda: "error"
        bot.es_pagina_anti_bot = lambda: False
        resultado = bot.navegar_tolerante("https://x.com/home")
        check("navegar: driver roto -> driver", resultado == "driver", resultado)
        check(
            "navegar: driver roto no refresca",
            driver.refresh_calls == 0,
            f"{driver.refresh_calls}",
        )
    finally:
        time.sleep = original_sleep


def test_recuperar_interstitial(check):
    print("P1: _recuperar_interstitial (anti-bot 1 refresh; error delega)")
    driver = FakeDriver()
    bot = bot_con_driver(driver)
    refrescos = []
    bot._refresh_corto = lambda timeout=5.0: refrescos.append(timeout)
    bot.es_pagina_anti_bot = lambda: True
    bot._es_pagina_error_x = lambda: False
    check(
        "interstitial: anti-bot devuelve anti-bot",
        bot._recuperar_interstitial("u") == "anti-bot",
    )
    check(
        "interstitial: anti-bot -> 1 refresh corto (8s)",
        refrescos == [8.0],
        str(refrescos),
    )

    driver2 = FakeDriver()
    bot2 = bot_con_driver(driver2)
    delegaciones = []

    def spy_navegar(url, timeout_total=45.0):
        delegaciones.append((url, timeout_total))
        return "error"

    bot2.es_pagina_anti_bot = lambda: False
    bot2._es_pagina_error_x = lambda: True
    bot2._url_en_x = lambda: True
    bot2.navegar_tolerante = spy_navegar
    check(
        "interstitial: pagina de error generica -> error",
        bot2._recuperar_interstitial("u") == "error",
    )
    check(
        "interstitial: delega en navegar_tolerante(url, 20s)",
        delegaciones == [("u", 20.0)],
        str(delegaciones),
    )


# --------------------------------------------------------------------------- #
# P0-E: `login_con_cookies` (.pkl) NO declara exito si X pide login
# --------------------------------------------------------------------------- #
def test_login_pkl_muro_de_login(check):
    print("P0-E: login_con_cookies (.pkl) detecta el muro de login y delega")

    def _spy_delega(bot):
        llamadas = []
        bot.login_con_cookies_json = lambda: (llamadas.append(1), True)[1]
        return llamadas

    # (a1) .pkl presente y X redirige a /i/flow/login -> delega en cookies_json
    driver = FakeDriver()
    driver.current_url = "https://x.com/i/flow/login"
    bot = preparar_bot_login(driver)
    delegaciones = _spy_delega(bot)
    with _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    check(
        "pkl+login URL: no declara exito con el .pkl (delega)",
        delegaciones == [1],
        str(delegaciones),
    )
    check(
        "pkl+login URL: devuelve el resultado del fallback",
        resultado is True,
        repr(resultado),
    )

    # (a2) URL sin "login" pero con formulario de login VISIBLE -> delega
    driver = FakeDriver()
    driver.current_url = "https://x.com/home"
    driver.editores = [FakeElement(driver, contenteditable=False, visible=True)]
    bot = preparar_bot_login(driver)
    delegaciones = _spy_delega(bot)
    with _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    check(
        "pkl+form visible: no declara exito sin 'login' en la URL",
        delegaciones == [1],
        str(delegaciones),
    )
    check(
        "pkl+form visible: devuelve el resultado del fallback",
        resultado is True,
        repr(resultado),
    )

    # (a3) integracion: cookies_json validas -> el fallback recupera la sesion
    driver = FakeDriver()
    driver.current_url = "https://x.com/i/flow/login"

    def get_simulado(url):
        driver.get_calls.append(url)
        # X sirve la home como visitante (redirige a /i/flow/login) mientras el
        # .pkl esta vencido; las rutas del fallback ya cargan autenticadas.
        if url == "https://x.com":
            driver.current_url = "https://x.com/i/flow/login"
        else:
            driver.current_url = url

    driver.get = get_simulado
    bot = preparar_bot_login(driver)
    bot._brandear_cuenta = lambda cookies: None
    cuenta = CuentaFake(cookies_json=json.dumps(COOKIES_JSON_VALIDAS))
    with _db_falsa(cuenta), _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    check(
        "pkl vencido + cookies_json validas: recupera la sesion (True)",
        resultado is True,
        repr(resultado),
    )
    check(
        "pkl vencido + cookies_json validas: inyecta las cookies de la BD",
        any(c.get("name") == "ct0" for c in driver.cookies_added),
        str(driver.cookies_added),
    )

    # (b) .pkl presente y sin muro -> True sin tocar cookies_json
    driver = FakeDriver()
    driver.current_url = "https://x.com/home"
    driver.editores = []
    bot = preparar_bot_login(driver)
    sin_llamadas = _spy_delega(bot)
    with _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    check("pkl sin muro: devuelve True", resultado is True, repr(resultado))
    check(
        "pkl sin muro: NO llama a login_con_cookies_json",
        sin_llamadas == [],
        str(sin_llamadas),
    )

    # (c) .pkl vencido, sin cookies_json ni auth_token -> False y sin suspender
    driver = FakeDriver()
    driver.current_url = "https://x.com/i/flow/login"
    bot = preparar_bot_login(driver)
    bot._brandear_cuenta = lambda cookies: None
    cuenta = CuentaFake(cookies_json=None, auth_token="", password="", totp_secret="")
    with _db_falsa(cuenta), _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    error = (bot.ultimo_error or "").lower()
    check("pkl vencido: devuelve False", resultado is False, repr(resultado))
    check(
        "pkl vencido: ultimo_error menciona sesion expirada/invalida",
        any(frag in error for frag in ("expirad", "invalid", "inválid")),
        bot.ultimo_error,
    )
    check(
        "pkl vencido: NO marca la cuenta suspendida",
        bot.cuenta_suspendida is False,
    )

    # (d) anti-bot tiene prioridad: NO delega y NO marca suspendida
    driver = FakeDriver()
    driver.current_url = "https://x.com/account/access?__cf_chl_rt_tk=x"
    bot = preparar_bot_login(driver)
    bot.es_pagina_anti_bot = lambda: True
    delegaciones = _spy_delega(bot)
    with _pkl_falso(COOKIES_PKL):
        resultado = bot.login_con_cookies()
    check(
        "anti-bot primero: devuelve False sin delegar",
        resultado is False and delegaciones == [],
        str(delegaciones),
    )
    check(
        "anti-bot primero: NO marca la cuenta suspendida",
        bot.cuenta_suspendida is False,
    )


def run(check):
    """Ejecuta los checks de este archivo con el `check` del runner."""
    with _sin_esperas():
        test_mask_primero_y_send_keys(check)
        test_sin_metodos_silenciosos(check)
        test_click_falla_sigue_con_send_keys(check)
        test_fallback_actionchains(check)
        test_mask_js_falla_no_aborta(check)
        test_limpieza_borrador(check)
        test_stale_relocaliza(check)
        test_mensaje_final_exacto(check)
        test_pausa_humana_publicar_tweet(check)
        test_pausa_humana_responder_tweet(check)
        test_primer_editor_ocluido(check)
        test_cita_prefiere_dialogo(check)
        test_navegar_tolerante_p1(check)
        test_recuperar_interstitial(check)
        test_login_pkl_muro_de_login(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_selenium_fakes.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
