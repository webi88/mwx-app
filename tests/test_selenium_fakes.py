"""Suite rapida de regresion de `plataformas/twitter/selenium_bot.py` (sin Chrome).

Version permanente de los checks P0-A/B/C/D/E y P1 de la sesion de arreglos
del pegado de texto en X. Usa FakeDriver/FakeElement para simular, sin red y
sin Chrome, los casos reales de Railway:

  - `_pegar_texto`: CDP `Input.insertText` (wrapper no editable con descendiente
    contenteditable), portapapeles sin `el.click()` pese al `data-testid=mask`
    del modal, `execCommand` no-op, `send_keys` como ultimo recurso,
    verificacion del texto y re-localizacion de elementos stale (React), y el
    mensaje final exacto si TODOS los metodos fallan.
  - `_primer_editor_visible`: prefiere el editor NO ocluido por el mask y el
    editor del dialogo cuando `preferir_dialogo=True`.
  - `solo_retwittear` (cita): usa `preferir_dialogo=True` al pedir el editor.
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
    InvalidSessionIdException,
    StaleElementReferenceException,
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
    ):
        self.driver = driver
        self.tag = tag
        self.contenteditable = contenteditable
        self._texto = texto
        self.stale = stale
        self.occluded = occluded
        self.visible = visible
        self.hijos = []
        self.seleccionado = False
        self.click_count = 0

    # --- API Selenium usada por el bot ---
    def resolver_editable(self):
        if self.tag in ("textarea", "input") or self.contenteditable:
            return self
        for h in self.hijos:
            r = h.resolver_editable()
            if r is not None:
                return r
        return None

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
        if self.driver.mask_presente:
            raise ElementClickInterceptedException(
                "mask intercepta el clic (test)"
            )

    def send_keys(self, *args):
        if self.stale:
            raise StaleElementReferenceException("stale")
        if args and args[0] in (Keys.CONTROL, Keys.COMMAND):
            tecla = args[-1]
            if tecla == "a":
                self.seleccionado = True
            elif tecla == "v" and self.driver.send_keys_escribe:
                self.set_contenido(self.driver.clipboard, reemplazar=True)
            return
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
        self.cdp_escribe = True
        self.exec_command_escribe = False
        self.send_keys_escribe = True
        self.clipboard = ""
        self.focused = None
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
        if "execCommand" in script:
            el = args[0]
            if self.exec_command_escribe:
                el.set_contenido(str(args[1]), reemplazar=True)
            return None
        self.script_calls.append((script, args))
        return None

    def execute_cdp_cmd(self, cmd, params):
        self.cdp_calls.append((cmd, params))
        if cmd == "Input.insertText" and self.cdp_escribe and self.focused is not None:
            self.focused.set_contenido(params.get("text", ""), reemplazar=True)
        return {}

    def find_elements(self, by, sel):
        if sel == "[data-testid='mask']":
            return list(self.masks)
        if sel == "div[role='dialog']":
            return list(self.dialogos)
        return list(self.editores)


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
# P0-A/B: `_pegar_texto`
# --------------------------------------------------------------------------- #
def test_cdp_wrapper(check):
    print("P0-A: CDP Input.insertText con wrapper no editable")
    driver = FakeDriver()
    wrapper = FakeElement(driver, contenteditable=False)
    hijo = FakeElement(driver, contenteditable=True)
    wrapper.hijos = [hijo]
    driver.editores = [wrapper]
    bot = bot_con_driver(driver)
    bot._pegar_texto(wrapper, "Hola CDP desde wrapper")
    check(
        "CDP: el texto queda en el descendiente contenteditable",
        hijo._texto == "Hola CDP desde wrapper",
        repr(hijo._texto),
    )
    check(
        "CDP: se uso Input.insertText por execute_cdp_cmd",
        driver.cdp_calls and driver.cdp_calls[0][0] == "Input.insertText",
    )
    check(
        "CDP: se enfoco y selecciono el editable por JS",
        hijo.seleccionado is True,
    )
    check(
        "CDP: una sola pasada (sin portapapeles)",
        len(driver.cdp_calls) == 1,
    )


def test_pyperclip_lanza_y_fallbacks(check):
    print("P0-A/B: CDP no-op + pyperclip lanza + execCommand no-op -> send_keys")
    driver = FakeDriver()
    driver.cdp_escribe = False
    driver.exec_command_escribe = False
    editor = FakeElement(driver, contenteditable=True, texto="")
    driver.editores = [editor]
    copias = []
    with _pyperclip_falso(copias, lanzar=True):
        bot = bot_con_driver(driver)
        bot._pegar_texto(editor, "Texto por send_keys")
    check(
        "fallback: el texto final entra por send_keys",
        editor._texto == "Texto por send_keys",
        repr(editor._texto),
    )
    check("fallback: pyperclip no copio (sin xclip)", copias == [])
    check(
        "fallback: se intento execCommand antes de send_keys",
        any("execCommand" in s for s, _ in driver.js_calls),
    )
    check("fallback: el click de send_keys si corrio", editor.click_count >= 1)


def test_portapapeles_sin_click_con_mask(check):
    print("P0-B: portapapeles sin el.click() pese al mask")
    driver = FakeDriver()
    driver.cdp_escribe = False  # forzar la ruta del portapapeles
    driver.mask_presente = True
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    copias = []
    with _pyperclip_falso(copias, lanzar=False, driver=driver):
        bot = bot_con_driver(driver)
        bot._pegar_texto(editor, "Pegado sin click")
    check("mask: el texto queda pegado", editor._texto == "Pegado sin click", repr(editor._texto))
    check("mask: pyperclip copio el texto", copias == ["Pegado sin click"])
    check(
        "mask: ningun el.click() (mask intacto)",
        editor.click_count == 0,
        f"clicks={editor.click_count}",
    )
    check("mask: se enfoco el editor por JS", driver.focused is editor)


def test_stale_relocaliza(check):
    print("P0-A: stale element -> re-localizacion y reintento")
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
    check("stale: se re-localizo y se uso CDP con el fresco", len(driver.cdp_calls) >= 1)


def test_mensaje_final_exacto(check):
    print("P0-A: mensaje final exacto si TODO falla")
    driver = FakeDriver()
    driver.cdp_escribe = False
    driver.exec_command_escribe = False
    driver.send_keys_escribe = False
    editor = FakeElement(driver, contenteditable=True)
    driver.editores = [editor]
    with _pyperclip_falso([], lanzar=True):
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
# P0-D: `solo_retwittear` (rama cita) usa preferir_dialogo=True
# --------------------------------------------------------------------------- #
def test_cita_prefiere_dialogo(check):
    print("P0-D: la cita pide el editor del dialogo")
    driver = FakeDriver()
    bot = bot_con_driver(driver)

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
    pegados = []
    bot._pegar_texto = lambda editor, texto: pegados.append(texto)
    bot._buscar_boton_post = lambda: FakeBoton()
    bot._verificar_publicacion = lambda: True
    bot._obtener_ultimo_enlace = lambda usuario: ""

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
    check("cita: pega el mensaje de la cita", pegados == ["Comentario de cita"], str(pegados))


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
        test_cdp_wrapper(check)
        test_pyperclip_lanza_y_fallbacks(check)
        test_portapapeles_sin_click_con_mask(check)
        test_stale_relocaliza(check)
        test_mensaje_final_exacto(check)
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
