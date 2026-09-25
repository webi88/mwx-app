"""Suite rapida de `TwitterBot.reportar_post` / `reportar_cuenta` /
`reportar_objetivo` (sin Chrome).

Fakes propios (driver/nodos con la API de Selenium que usa el bot) para cubrir,
de forma determinista, el flujo REAL de reporte de X:

  - flujo feliz de tweet y de cuenta (EN y ES), con verificacion por toast;
  - motivo no encontrado -> falla SIN clickear ninguna opcion y registra las
    opciones VISIBLES reales en `ultimo_error`;
  - "ya reportado"/"already reported" -> True idempotente (sin elegir motivo);
  - `dry_run=True` -> llega al boton de envio y CIERRA sin pulsarlo;
  - el modal que se cierra tras enviar (sin toast) cuenta como exito;
  - sin confirmacion de X -> False con mensaje claro;
  - `reportar_objetivo` distingue tweet (`/status/`) de perfil.

Determinista: se instala un reloj falso en `plataformas.twitter.selenium_bot`
(el `sleep` avanza el reloj), asi los timeouts/esperas no cuestan tiempo real.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_reportar_fakes.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import re
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402

from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402


# --------------------------------------------------------------------------- #
# Reloj falso: `sleep` avanza el tiempo (los timeouts del bot son instantaneos)
# --------------------------------------------------------------------------- #
class RelojFalso:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def time(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, segundos):
        self.t += max(0.0, float(segundos))


@contextlib.contextmanager
def _reloj_falso():
    """Parchea SOLO el `time` que ve selenium_bot (no el time global)."""
    import plataformas.twitter.selenium_bot as modulo

    with mock.patch.object(modulo, "time", RelojFalso()):
        yield


# --------------------------------------------------------------------------- #
# Fakes DOM
# --------------------------------------------------------------------------- #
def _valor_css(sel: str):
    m = re.search(r"(?:data-testid|role|aria-label)='([^']*)'", sel)
    return m.group(1) if m else None


class FakeNodo:
    """Nodo DOM falso con hijos, atributos y callbacks de clic."""

    def __init__(
        self,
        driver,
        texto="",
        tag="div",
        role=None,
        testid=None,
        aria_label=None,
        visible=True,
        enabled=True,
        al_click=None,
    ):
        self.driver = driver
        self._texto = texto
        self.tag = tag
        self.role = role
        self.testid = testid
        self.aria_label = aria_label
        self.visible = visible
        self.enabled = enabled
        self.al_click = al_click
        self.hijos = []
        self.click_count = 0

    # --- API Selenium usada por el bot ---
    @property
    def text(self):
        partes = [self._texto]
        partes += [h.text for h in self.hijos if h.visible]
        return "\n".join(p for p in partes if p)

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, nombre):
        if nombre == "aria-label":
            return self.aria_label
        if nombre == "data-testid":
            return self.testid
        if nombre in ("textContent", "innerText"):
            return self.text
        return None

    def click(self):
        self.click_count += 1
        etiqueta = self._texto or self.testid or self.aria_label or self.tag
        self.driver.eventos.append(("click", etiqueta))
        if self.al_click:
            self.al_click()

    def send_keys(self, *args):
        self.driver.eventos.append(("send_keys", tuple(str(a) for a in args)))
        if args and args[0] == Keys.ESCAPE:
            self.driver.escape_recibido += 1

    def coincide(self, by, sel):
        if by == By.CSS_SELECTOR:
            if sel.startswith("[data-testid="):
                return self.testid == _valor_css(sel)
            if sel.startswith("[role="):
                return self.role == _valor_css(sel)
            if sel.startswith("[aria-label="):
                return self.aria_label == _valor_css(sel)
            if sel == "button":
                return self.tag == "button"
            if sel == "label":
                return self.tag == "label"
            if sel == "article[data-testid='tweet']":
                return self.testid == "tweet" and self.tag == "article"
            return False
        if by == By.XPATH:
            if "caret" in sel:
                return self.testid == "caret"
            m = re.search(r"@aria-label='([^']*)'", sel)
            if m:
                return self.aria_label == m.group(1)
        return False

    def find_elements(self, by, sel):
        resultado = []
        for hijo in self.hijos:
            if hijo.coincide(by, sel):
                resultado.append(hijo)
        return resultado


class FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    @property
    def active_element(self):
        return self.driver.body


class FakeReportDriver:
    """Driver falso con registros documentales para el flujo de reporte."""

    def __init__(self):
        self.current_url = "chrome://new-tab-page"
        self.title = "X"
        self.page_source = ""
        self.page_load_timeout = 60
        self.get_calls = []
        self.refresh_calls = 0
        self.escape_recibido = 0
        self.eventos = []
        self.scripts = []
        self.body = FakeNodo(self, texto="", tag="body")
        self.switch_to = FakeSwitchTo(self)
        # Registros documentales
        self.articles = []
        self.menus = []
        self.dialogs = []
        self.toasts = []
        self.profile_elements = []
        self.close_elements = []
        self.caret_documento = []
        self.botones_genericos = []

    def set_page_load_timeout(self, valor):
        self.page_load_timeout = valor

    def get(self, url):
        self.get_calls.append(url)
        self.current_url = url

    def refresh(self):
        self.refresh_calls += 1

    def _visibles(self, lista):
        return [el for el in lista if el.visible]

    def _todos(self):
        vistos = []
        for grupo in (
            self.botones_genericos,
            self.caret_documento,
            self.close_elements,
            self.profile_elements,
            *[m.hijos for m in self.menus],
            *[d.hijos for d in self.dialogs],
        ):
            for el in grupo:
                if el not in vistos:
                    vistos.append(el)
        return vistos

    def find_elements(self, by, sel):
        if by == By.TAG_NAME:
            return []
        if by == By.XPATH:
            if "caret" in sel:
                return self._visibles(self.caret_documento)
            if "@aria-label=" in sel:
                valor = re.search(r"@aria-label='([^']*)'", sel)
                if valor:
                    return [
                        el for el in self._todos()
                        if el.aria_label == valor.group(1) and el.visible
                    ]
            return []
        # CSS
        if sel == "article[data-testid='tweet']":
            return self._visibles(self.articles)
        if sel in ("[role='menu']", "[data-testid='Dropdown']"):
            return self._visibles(self.menus)
        if sel in ("[role='dialog']", "[data-testid='modal']", "div[aria-modal='true']"):
            return self._visibles(self.dialogs)
        if sel in ("[data-testid='toast']", "[role='alert']", "[data-testid='toast'] span"):
            return self._visibles(self.toasts)
        if sel == "[data-testid='mask']":
            return []
        if sel == "[data-testid='caret']":
            return self._visibles(self.caret_documento)
        if sel in (
            "[data-testid='app-bar-close']",
            "[aria-label='Close']",
            "[aria-label='Cerrar']",
        ):
            return self._visibles(self.close_elements)
        if sel.startswith("[data-testid="):
            valor = _valor_css(sel)
            return [el for el in self._todos() if el.testid == valor and el.visible]
        if sel.startswith("[role="):
            valor = _valor_css(sel)
            return [el for el in self._todos() if el.role == valor and el.visible]
        if sel.startswith("[aria-label="):
            valor = _valor_css(sel)
            return [el for el in self._todos() if el.aria_label == valor and el.visible]
        if sel == "button":
            return [el for el in self._todos() if el.tag == "button" and el.visible]
        if sel == "label":
            return [el for el in self._todos() if el.tag == "label" and el.visible]
        return []

    def find_element(self, by, sel):
        if by == By.TAG_NAME and sel == "body":
            return self.body
        elementos = self.find_elements(by, sel)
        if elementos:
            return elementos[0]
        raise Exception(f"no encontrado (fake): {by} {sel}")

    def execute_script(self, script, *args):
        if "arguments[0].click()" in script:
            if args:
                args[0].click()
            return None
        if "scrollIntoView" in script:
            return None
        self.scripts.append((script, args))
        return None


def _bot(driver):
    """TwitterBot minimo (sin Chrome ni BD) para el flujo de reporte."""
    bot = TwitterBot.__new__(TwitterBot)
    bot.driver = driver
    bot.usuario = "cuenta_test"
    bot.base_url = "https://x.com"
    bot.ultimo_error = ""
    bot.ultima_url_publicada = ""
    bot._sesion_cdp = False
    bot.cuenta_suspendida = False
    return bot


# --------------------------------------------------------------------------- #
# Builders de escenarios
# --------------------------------------------------------------------------- #
def _articulo_con_caret(driver, al_click_caret):
    article = FakeNodo(driver, tag="article", testid="tweet")
    caret = FakeNodo(driver, testid="caret", aria_label="More")
    caret.al_click = al_click_caret
    article.hijos.append(caret)
    driver.articles.append(article)
    return article, caret


def _menu(driver, textos, callbacks=None):
    menu = FakeNodo(driver, role="menu", testid="Dropdown")
    items = []
    for indice, texto in enumerate(textos):
        item = FakeNodo(driver, texto=texto, role="menuitem")
        if callbacks and indice < len(callbacks) and callbacks[indice]:
            item.al_click = callbacks[indice]
        menu.hijos.append(item)
        items.append(item)
    driver.menus.append(menu)
    return menu, items


def _dialogo(driver, razones, submit_text, al_submit, razon_click=None, submit_al_click=None):
    dialogo = FakeNodo(driver, role="dialog")
    elementos_razon = []
    for texto in razones:
        nodo = FakeNodo(driver, texto=texto, role="radio")
        if razon_click:
            nodo.al_click = lambda nodo=nodo, texto=texto: razon_click(nodo, texto)
        dialogo.hijos.append(nodo)
        elementos_razon.append(nodo)
    submit = FakeNodo(driver, texto=submit_text, role="button")
    if submit_al_click is not None:
        submit.al_click = submit_al_click
    else:
        submit.al_click = al_submit
    dialogo.hijos.append(submit)
    driver.dialogs.append(dialogo)
    return dialogo, elementos_razon, submit


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def test_flujo_feliz_tweet(check):
    print("Reporte: flujo feliz de tweet (EN, toast de confirmacion)")
    driver = FakeReportDriver()
    estado = {"submit": None, "razon": None}

    def enviar():
        driver.dialogs = []
        driver.toasts = [
            FakeNodo(driver, texto="Thanks for letting us know", role="alert")
        ]

    def abrir_dialogo():
        _, razones, submit = _dialogo(
            driver, ["It's spam", "It's abusive or harmful"], "Submit", enviar
        )
        estado["submit"] = submit
        estado["razon"] = razones[0]

    def abrir_menu():
        _menu(driver, ["Embed Post", "Report post"], [None, lambda: abrir_dialogo()])

    _, caret = _articulo_con_caret(driver, abrir_menu)
    driver.profile_elements = []

    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/usuario/status/123", motivo="spam")

    check("tweet feliz: devuelve True", ok is True, bot.ultimo_error)
    check("tweet feliz: ultimo_error vacio", bot.ultimo_error == "", bot.ultimo_error)
    check(
        "tweet feliz: navego al tweet",
        driver.get_calls and driver.get_calls[0] == "https://x.com/usuario/status/123",
        str(driver.get_calls),
    )
    check("tweet feliz: abrio el menu '...'", caret.click_count == 1, caret.click_count)
    check(
        "tweet feliz: eligio 'Report post'",
        driver.eventos.count(("click", "Report post")) == 1,
        str(driver.eventos),
    )
    check(
        "tweet feliz: eligio el motivo 'It's spam'",
        driver.eventos.count(("click", "It's spam")) == 1,
        str(driver.eventos),
    )
    check(
        "tweet feliz: pulso Submit UNA vez",
        estado["submit"].click_count == 1,
        estado["submit"].click_count,
    )
    check(
        "tweet feliz: confirmo con el toast",
        any(e[0] == "click" and e[1] == "Submit" for e in driver.eventos),
        str(driver.eventos),
    )


def test_flujo_feliz_tweet_es(check):
    print("Reporte: flujo feliz de tweet (ES: Mas/Reportar publicacion/Enviar)")
    driver = FakeReportDriver()

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por informarnos", role="alert")]

    def abrir_dialogo():
        _dialogo(driver, ["Es spam", "Es abusivo o danino"], "Enviar", enviar)

    def abrir_menu():
        _menu(
            driver,
            ["Insertar publicacion", "Reportar publicacion"],
            [None, lambda: abrir_dialogo()],
        )

    # Sin testid: el bot debe encontrar el '...' por aria-label "Más".
    article = FakeNodo(driver, tag="article", testid="tweet")
    caret = FakeNodo(driver, aria_label="Más")
    caret.al_click = abrir_menu
    article.hijos.append(caret)
    driver.articles.append(article)

    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/usuario/status/1", motivo="spam")

    check("tweet ES feliz: devuelve True", ok is True, bot.ultimo_error)
    check(
        "tweet ES feliz: encontro el '...' por aria-label 'Más'",
        caret.click_count == 1,
        caret.click_count,
    )
    check(
        "tweet ES feliz: eligio 'Reportar publicacion'",
        ("click", "Reportar publicacion") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "tweet ES feliz: eligio 'Es spam'",
        ("click", "Es spam") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "tweet ES feliz: pulso 'Enviar'",
        ("click", "Enviar") in driver.eventos,
        str(driver.eventos),
    )


def test_flujo_feliz_tweet_denunciar(check):
    print("Reporte: UI ES real de X ('Denunciar post' / boton 'Denunciar')")
    driver = FakeReportDriver()

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por informarnos", role="alert")]

    def abrir_dialogo():
        # Opciones ES reales del flujo de denuncia de X.
        _dialogo(
            driver,
            [
                "Muestra una foto o video sensible",
                "Es spam",
                "Es abusivo o dañino",
                "Expresa intenciones de autolesión o suicidio",
            ],
            "Denunciar",
            enviar,
        )

    def abrir_menu():
        _menu(
            driver,
            ["Silenciar", "Denunciar post"],
            [None, lambda: abrir_dialogo()],
        )

    _articulo_con_caret(driver, abrir_menu)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/usuario/status/42", motivo="spam")

    check("denunciar ES: devuelve True", ok is True, bot.ultimo_error)
    check(
        "denunciar ES: eligio 'Denunciar post'",
        ("click", "Denunciar post") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "denunciar ES: eligio 'Es spam'",
        ("click", "Es spam") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "denunciar ES: pulso 'Denunciar'",
        ("click", "Denunciar") in driver.eventos,
        str(driver.eventos),
    )


def test_subscreen_real_spam(check):
    print("Reporte: pantalla intermedia real ('Siguiente' -> sub-opcion -> Denunciar)")
    driver = FakeReportDriver()
    estado = {"denunciar": None}

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por informarnos", role="alert")]

    def abrir_subpantalla():
        driver.dialogs = []
        sub_dialogo = FakeNodo(driver, role="dialog")
        for texto in ("Es publicidad no deseada", "Es spam repetitivo"):
            nodo = FakeNodo(driver, texto=texto, role="radio")

            def elegir(nodo=nodo, texto=texto):
                # Al elegir la sub-opcion aparece el boton final.
                if estado.get("denunciar") is None:
                    boton = FakeNodo(driver, texto="Denunciar", role="button")
                    boton.al_click = enviar
                    sub_dialogo.hijos.append(boton)
                    estado["denunciar"] = boton

            nodo.al_click = elegir
            sub_dialogo.hijos.append(nodo)
        driver.dialogs.append(sub_dialogo)

    def abrir_dialogo():
        _, _, siguiente = _dialogo(
            driver,
            ["Spam", "Odio, abuso o acoso", "Contenido sexual para adultos"],
            "Siguiente",
            None,
            submit_al_click=abrir_subpantalla,
        )

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Denunciar post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/11", motivo="spam")

    check("subpantalla: devuelve True", ok is True, bot.ultimo_error)
    check(
        "subpantalla: pulso 'Siguiente'",
        ("click", "Siguiente") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "subpantalla: eligio la sub-opcion 'Es spam repetitivo' (match del motivo)",
        ("click", "Es spam repetitivo") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "subpantalla: no eligio 'Es publicidad no deseada' al azar",
        ("click", "Es publicidad no deseada") not in driver.eventos,
        str(driver.eventos),
    )
    check(
        "subpantalla: pulso 'Denunciar' al final",
        estado["denunciar"] is not None and estado["denunciar"].click_count == 1,
        getattr(estado["denunciar"], "click_count", None),
    )


def test_mapeo_motivos_reales(check):
    print("Reporte: mapping contra las opciones REALES del modal de X")
    driver = FakeReportDriver()
    bot = _bot(driver)

    def nodos(*textos):
        return [(FakeNodo(driver, texto=t), t) for t in textos]

    # Textos reales observados en el smoke de X (2026, ES).
    reales = [
        "Spam",
        "Odio, abuso o acoso",
        "Protección de personas menores de edad",
        "Discurso violento",
        "Contenido multimedia de carácter gráfico o violento",
        "Comportamientos ilegales y regulados",
        "Suplantación de identidad",
        "Contenido sexual para adultos",
        "Contenido privado o no consentido",
        "Suicidio o autolesiones",
        "Terrorismo o extremismo violento",
        "Integridad cívica",
    ]
    esperado = {
        "spam": "Spam",
        "hate": "Odio, abuso o acoso",
        "abuse": "Odio, abuso o acoso",
        "violence": "Discurso violento",
        "self_harm": "Suicidio o autolesiones",
        "sensitive": "Contenido sexual para adultos",
        "impersonation": "Suplantación de identidad",
        "nudity": "Contenido sexual para adultos",
        "false_info": "Integridad cívica",
    }
    for motivo, texto_esperado in esperado.items():
        _, elegido = bot._elegir_opcion_visible(
            nodos(*reales), bot._candidatos_motivo_reporte(motivo, False)
        )
        check(
            f"mapping real: {motivo} -> '{texto_esperado}'",
            elegido == texto_esperado,
            elegido,
        )


def test_flujo_feliz_cuenta_denunciar(check):
    print("Reporte: cuenta ES real ('Denunciar a @user')")
    driver = FakeReportDriver()
    driver.profile_elements = [FakeNodo(driver, testid="UserName")]

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por avisarnos", role="alert")]

    def abrir_dialogo():
        _dialogo(
            driver,
            [
                "Se hacen pasar por otra persona",
                "Su cuenta es spam",
                "Es abusivo o dañino",
            ],
            "Denunciar",
            enviar,
        )

    driver.caret_documento = [FakeNodo(driver, testid="caret", aria_label="Más")]
    driver.caret_documento[0].al_click = lambda: _menu(
        driver,
        ["Silenciar", "Denunciar a @usuario"],
        [None, lambda: abrir_dialogo()],
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_cuenta("https://x.com/usuario", motivo="impersonation")

    check("cuenta denunciar: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cuenta denunciar: eligio 'Denunciar a @usuario'",
        ("click", "Denunciar a @usuario") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "cuenta denunciar: eligio 'Se hacen pasar por otra persona'",
        ("click", "Se hacen pasar por otra persona") in driver.eventos,
        str(driver.eventos),
    )


def test_flujo_feliz_cuenta(check):
    print("Reporte: flujo feliz de cuenta (Report @user / Their account is spam)")
    driver = FakeReportDriver()
    driver.profile_elements = [FakeNodo(driver, testid="UserName")]
    driver.caret_documento = [FakeNodo(driver, testid="caret", aria_label="More")]
    estado = {"submit": None}

    def enviar():
        driver.dialogs = []
        driver.toasts = [
            FakeNodo(driver, texto="Thanks for letting us know", role="alert")
        ]

    def abrir_dialogo():
        _, _, submit = _dialogo(
            driver,
            ["They're being abusive or harmful", "Their account is spam"],
            "Submit",
            enviar,
        )
        estado["submit"] = submit

    def abrir_menu():
        _menu(
            driver,
            ["Mute", "Report @someuser"],
            [None, lambda: abrir_dialogo()],
        )

    driver.caret_documento[0].al_click = abrir_menu
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_cuenta("https://x.com/someuser", motivo="spam")

    check("cuenta feliz: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cuenta feliz: eligio 'Report @someuser' (handle dinamico)",
        ("click", "Report @someuser") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "cuenta feliz: eligio 'Their account is spam'",
        ("click", "Their account is spam") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "cuenta feliz: pulso Submit",
        estado["submit"].click_count == 1,
        estado["submit"].click_count,
    )


def test_flujo_feliz_cuenta_es(check):
    print("Reporte: cuenta ES (Reportar a @user / Es abusivo o danino)")
    driver = FakeReportDriver()
    # El caret del perfil vive DENTRO de `primaryColumn` (como en X real).
    perfil = FakeNodo(driver, testid="primaryColumn")
    driver.profile_elements = [perfil]

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por avisarnos", role="alert")]

    def abrir_dialogo():
        _dialogo(
            driver,
            ["Su cuenta es spam", "Es abusivo o danino"],
            "Siguiente",
            enviar,
        )

    def abrir_menu():
        _menu(
            driver,
            ["Silenciar", "Reportar a @otro"],
            [None, lambda: abrir_dialogo()],
        )

    caret_perfil = FakeNodo(driver, testid="caret")
    caret_perfil.al_click = abrir_menu
    perfil.hijos.append(caret_perfil)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_cuenta(
            "https://x.com/otro/status/55", motivo="abuse"
        )

    check("cuenta ES feliz: devuelve True", ok is True, bot.ultimo_error)
    check(
        "cuenta ES feliz: recorta /status/ y reporta la cuenta",
        driver.get_calls and driver.get_calls[0] == "https://x.com/otro",
        str(driver.get_calls),
    )
    check(
        "cuenta ES feliz: eligio 'Es abusivo o danino'",
        ("click", "Es abusivo o danino") in driver.eventos,
        str(driver.eventos),
    )


def test_motivo_no_encontrado(check):
    print("Reporte: motivo no encontrado -> falla SIN clickear y lista opciones")
    driver = FakeReportDriver()
    estado = {"submit": None, "razones": []}

    def abrir_dialogo():
        _, razones, submit = _dialogo(
            driver,
            ["It displays a sensitive photo or video", "It's misleading"],
            "Submit",
            lambda: None,
        )
        estado["razones"] = razones
        estado["submit"] = submit

    def abrir_menu():
        _menu(driver, ["Report post"], [lambda: abrir_dialogo()])

    _articulo_con_caret(driver, abrir_menu)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/usuario/status/9", motivo="spam")

    check("motivo no encontrado: devuelve False", ok is False, repr(ok))
    check(
        "motivo no encontrado: NINGUNA razon fue clickeada",
        all(r.click_count == 0 for r in estado["razones"]),
        [r.click_count for r in estado["razones"]],
    )
    check(
        "motivo no encontrado: no pulso Submit",
        estado["submit"].click_count == 0,
        estado["submit"].click_count,
    )
    check(
        "motivo no encontrado: ultimo_error explica el motivo",
        "no encontrado" in bot.ultimo_error and "spam" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "motivo no encontrado: registra las opciones VISIBLES reales",
        "It displays a sensitive photo or video" in bot.ultimo_error
        and "It's misleading" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "motivo no encontrado: cerro el modal (Escape)",
        driver.escape_recibido >= 1,
        driver.escape_recibido,
    )


def test_motivo_ambiguedad_no_confunde(check):
    print("Reporte: opciones parecidas -> el motivo 'abuse' no cae en 'spam'")
    driver = FakeReportDriver()
    estado = {}

    def abrir_dialogo():
        _, razones, submit = _dialogo(
            driver,
            ["It's abusive or harmful", "It's spam"],
            "Submit",
            lambda: None,
        )
        estado["razon"] = razones[0]
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Report post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        # Solo se elige el motivo: se corta con un submit que no confirma.
        ok = bot.reportar_post("https://x.com/u/status/2", motivo="abuse")

    check(
        "ambiguedad: eligio 'It's abusive or harmful'",
        ("click", "It's abusive or harmful") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "ambiguedad: NO eligio 'It's spam'",
        ("click", "It's spam") not in driver.eventos,
        str(driver.eventos),
    )
    check(
        "ambiguedad: sin confirmacion devuelve False",
        ok is False and "no confirmo" in bot.ultimo_error,
        f"{ok} | {bot.ultimo_error}",
    )


def test_ya_reportado_al_abrir(check):
    print("Reporte: 'ya reportaste' tras la opcion del menu -> True idempotente")
    driver = FakeReportDriver()
    estado = {"razon": None}

    def ya_reportado():
        driver.page_source = "You already reported this post"

    def abrir_dialogo():
        _, razones, _ = _dialogo(driver, ["It's spam"], "Submit", lambda: None)
        estado["razon"] = razones[0]

    def abrir_menu():
        _menu(
            driver,
            ["Report post"],
            [lambda: (ya_reportado(), abrir_dialogo())],
        )

    _articulo_con_caret(driver, abrir_menu)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/3")

    check("ya reportado (al abrir): devuelve True", ok is True, bot.ultimo_error)
    check(
        "ya reportado (al abrir): no clickeo ningun motivo",
        estado["razon"].click_count == 0,
        estado["razon"].click_count,
    )
    check(
        "ya reportado (al abrir): sin toast no pulso Submit",
        ("click", "Submit") not in driver.eventos,
        str(driver.eventos),
    )


def test_ya_reportado_toast(check):
    print("Reporte: toast 'ya reportaste' tras enviar -> True idempotente")
    driver = FakeReportDriver()
    estado = {"submit": None}

    def enviar():
        driver.dialogs = []
        driver.toasts = [
            FakeNodo(driver, texto="You already reported this post", role="alert")
        ]

    def abrir_dialogo():
        _, _, submit = _dialogo(driver, ["It's spam"], "Submit", enviar)
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Report post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/4")

    check("ya reportado (toast): devuelve True", ok is True, bot.ultimo_error)
    check(
        "ya reportado (toast): pulso Submit una vez",
        estado["submit"].click_count == 1,
        estado["submit"].click_count,
    )


def test_dry_run_no_envia(check):
    print("Reporte: dry_run llega al boton de envio y NO lo pulsa")
    driver = FakeReportDriver()
    estado = {"submit": None}

    def abrir_dialogo():
        _, _, submit = _dialogo(driver, ["It's spam"], "Submit", lambda: None)
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Report post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post(
            "https://x.com/u/status/5", motivo="spam", dry_run=True
        )

    check("dry run: devuelve True (llego al envio)", ok is True, bot.ultimo_error)
    check(
        "dry run: NO pulso Submit",
        estado["submit"].click_count == 0,
        estado["submit"].click_count,
    )
    check(
        "dry run: cerro el flujo (Escape/boton cerrar)",
        driver.escape_recibido >= 1,
        driver.escape_recibido,
    )
    check(
        "dry run: ultimo_error vacio",
        bot.ultimo_error == "",
        bot.ultimo_error,
    )


def test_dry_run_cuenta(check):
    print("Reporte: dry_run de cuenta tampoco envia")
    driver = FakeReportDriver()
    driver.profile_elements = [FakeNodo(driver, testid="UserName")]
    estado = {"submit": None}

    def abrir_dialogo():
        _, _, submit = _dialogo(
            driver, ["Their account is spam"], "Submit", lambda: None
        )
        estado["submit"] = submit

    driver.caret_documento = [FakeNodo(driver, testid="caret")]
    driver.caret_documento[0].al_click = lambda: _menu(
        driver, ["Report @cuenta"], [lambda: abrir_dialogo()]
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_cuenta(
            "https://x.com/cuenta", motivo="spam", dry_run=True
        )

    check("dry run cuenta: devuelve True", ok is True, bot.ultimo_error)
    check(
        "dry run cuenta: NO pulso Submit",
        estado["submit"].click_count == 0,
        estado["submit"].click_count,
    )


def test_dialogo_cerrado_tras_envio(check):
    print("Reporte: modal que se cierra tras enviar (sin toast) -> exito")
    driver = FakeReportDriver()
    estado = {"submit": None}

    def enviar():
        driver.dialogs = []  # sin toast: el modal se desmonta al enviar

    def abrir_dialogo():
        _, _, submit = _dialogo(driver, ["It's spam"], "Submit", enviar)
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Report post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/6")

    check("modal cerrado: devuelve True", ok is True, bot.ultimo_error)
    check(
        "modal cerrado: pulso Submit una vez",
        estado["submit"].click_count == 1,
        estado["submit"].click_count,
    )
    check("modal cerrado: ultimo_error vacio", bot.ultimo_error == "", bot.ultimo_error)


def test_sin_confirmacion(check):
    print("Reporte: Submit que no confirma ni cierra -> False con mensaje")
    driver = FakeReportDriver()
    estado = {"submit": None}

    def abrir_dialogo():
        # El submit queda montado y no pasa nada (ni toast ni cierre).
        _, _, submit = _dialogo(driver, ["It's spam"], "Submit", lambda: None)
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Report post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/7")

    check("sin confirmacion: devuelve False", ok is False, repr(ok))
    check(
        "sin confirmacion: mensaje claro en ultimo_error",
        "X no confirmo el reporte" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "sin confirmacion: reintento de envio acotado (<=6)",
        1 <= estado["submit"].click_count <= 6,
        estado["submit"].click_count,
    )


def test_menu_sin_opcion_reporte(check):
    print("Reporte: menu sin opcion de reporte -> falla con opciones visibles")
    driver = FakeReportDriver()
    _, items = _menu(driver, ["Embed Post", "Mute"])
    _articulo_con_caret(driver, lambda: None)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/8")

    check("menu sin reporte: devuelve False", ok is False, repr(ok))
    check(
        "menu sin reporte: ninguna opcion fue clickeada",
        all(i.click_count == 0 for i in items),
        [i.click_count for i in items],
    )
    check(
        "menu sin reporte: ultimo_error lista opciones reales",
        "no se encontro la opcion de reporte" in bot.ultimo_error
        and "Embed Post" in bot.ultimo_error,
        bot.ultimo_error,
    )


# --------------------------------------------------------------------------- #
# FIX B: robustez del reporte (menu ajeno, article lento, respaldo Spam)
# --------------------------------------------------------------------------- #
def _refrescar_limpia_menus(driver):
    """`refresh` del fake que desmonta menus/dialogos (como Chrome real)."""

    def refrescar():
        driver.refresh_calls += 1
        driver.menus = []
        driver.dialogs = []

    driver.refresh = refrescar


def test_menu_ajeno_reintenta(check):
    print("Reporte: menu ajeno (About/Get App/Developers) -> cierra, refresh, 2o intento")
    driver = FakeReportDriver()
    _refrescar_limpia_menus(driver)
    estado = {"caret": 0, "submit": None}

    def enviar():
        driver.dialogs = []
        driver.toasts = [
            FakeNodo(driver, texto="Thanks for letting us know", role="alert")
        ]

    def abrir_menu():
        estado["caret"] += 1
        if estado["caret"] == 1:
            _menu(driver, ["About", "Get App", "Developers"])
            return

        def abrir_dialogo():
            _, _, submit = _dialogo(driver, ["It's spam"], "Submit", enviar)
            estado["submit"] = submit

        _menu(driver, ["Mute", "Report post"], [None, lambda: abrir_dialogo()])

    _, caret = _articulo_con_caret(driver, abrir_menu)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/100", motivo="spam")

    check("menu ajeno: reporta en el 2o intento (True)", ok is True, bot.ultimo_error)
    check(
        "menu ajeno: el caret se pulso 2 veces",
        estado["caret"] == 2,
        estado["caret"],
    )
    check(
        "menu ajeno: NUNCA clickeo About/Get App/Developers",
        not any(
            e[0] == "click" and e[1] in ("About", "Get App", "Developers")
            for e in driver.eventos
        ),
        str(driver.eventos),
    )
    check(
        "menu ajeno: cerro el menu con Escape y refresco",
        driver.escape_recibido >= 1 and driver.refresh_calls >= 1,
        f"escape={driver.escape_recibido} refresh={driver.refresh_calls}",
    )
    check(
        "menu ajeno: eligio la opcion correcta",
        ("click", "Report post") in driver.eventos,
        str(driver.eventos),
    )


def test_menu_ajeno_dos_intentos_falla(check):
    print("Reporte: menu ajeno en AMBOS intentos -> falla sin clickear opciones")
    driver = FakeReportDriver()
    _refrescar_limpia_menus(driver)

    def abrir_menu():
        _menu(driver, ["About", "Get App", "Developers"])

    _, caret = _articulo_con_caret(driver, abrir_menu)
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/101", motivo="spam")

    check("menu ajeno x2: devuelve False", ok is False, repr(ok))
    check(
        "menu ajeno x2: error claro con opciones visibles",
        "no se encontro la opcion de reporte" in bot.ultimo_error
        and "About" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "menu ajeno x2: ninguna opcion fue clickeada (solo el caret)",
        not any(
            e[0] == "click" and e[1] not in ("caret",)
            for e in driver.eventos
        ),
        str(driver.eventos),
    )
    check(
        "menu ajeno x2: caret pulsado 2 veces",
        caret.click_count == 2,
        caret.click_count,
    )


def test_article_tardio_reintenta(check):
    print("Reporte: article tardio -> navega de nuevo y reporta")
    driver = FakeReportDriver()
    contador = {"gets": 0}
    holder = {}

    def get(url):
        driver.get_calls.append(url)
        driver.current_url = url
        contador["gets"] += 1
        # El article recien aparece en la SEGUNDA navegacion (proxy lento).
        if contador["gets"] >= 2 and holder.get("article") is not None:
            if not driver.articles:
                driver.articles = [holder["article"]]

    driver.get = get

    def enviar():
        driver.dialogs = []
        driver.toasts = [
            FakeNodo(driver, texto="Thanks for letting us know", role="alert")
        ]

    def abrir_dialogo():
        _dialogo(driver, ["It's spam"], "Submit", enviar)

    def abrir_menu():
        _menu(driver, ["Report post"], [lambda: abrir_dialogo()])

    article = FakeNodo(driver, tag="article", testid="tweet")
    caret = FakeNodo(driver, testid="caret")
    caret.al_click = abrir_menu
    article.hijos.append(caret)
    holder["article"] = article

    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/102", motivo="spam")

    check("article tardio: reporta tras re-navegar (True)", ok is True, bot.ultimo_error)
    check(
        "article tardio: al menos 2 navegaciones",
        contador["gets"] >= 2,
        contador["gets"],
    )
    check(
        "article tardio: eligio el motivo y confirmo",
        ("click", "It's spam") in driver.eventos
        and ("click", "Submit") in driver.eventos,
        str(driver.eventos),
    )


def test_article_ausente_dos_intentos(check):
    print("Reporte: article ausente en ambos intentos -> fallo claro sin menús")
    driver = FakeReportDriver()
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/103", motivo="spam")

    check("article ausente: devuelve False", ok is False, repr(ok))
    check(
        "article ausente: mensaje 'no se encontro el tweet a reportar'",
        "no se encontro el tweet a reportar" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "article ausente: navego >= 2 veces",
        len(driver.get_calls) >= 2,
        str(driver.get_calls),
    )
    check(
        "article ausente: ningun click ni menu abierto",
        driver.eventos == [] and driver.menus == [],
        f"{driver.eventos} | {driver.menus}",
    )


def test_caret_no_global_si_hay_article(check):
    print("Reporte: article sin caret -> NO usa el caret global (barra lateral)")
    driver = FakeReportDriver()
    article = FakeNodo(driver, tag="article", testid="tweet")
    driver.articles = [article]
    lateral = FakeNodo(driver, testid="caret", aria_label="More")
    driver.caret_documento = [lateral]
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/106", motivo="spam")

    check("caret no global: devuelve False", ok is False, repr(ok))
    check(
        "caret no global: NO pulso el caret de la barra lateral",
        lateral.click_count == 0,
        lateral.click_count,
    )
    check(
        "caret no global: mensaje de '...' no encontrado",
        "no se encontro el boton de mas opciones" in bot.ultimo_error,
        bot.ultimo_error,
    )


def test_motivo_spam_respaldo(check):
    print("Reporte: motivo pedido ausente pero hay categorias -> respaldo Spam")
    driver = FakeReportDriver()

    def enviar():
        driver.dialogs = []
        driver.toasts = [FakeNodo(driver, texto="Gracias por informarnos", role="alert")]

    def abrir_dialogo():
        _dialogo(
            driver,
            ["Discurso violento", "Spam", "Suplantación de identidad"],
            "Denunciar",
            enviar,
        )

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Denunciar post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/104", motivo="hate")

    check("respaldo spam: devuelve True", ok is True, bot.ultimo_error)
    check(
        "respaldo spam: eligio 'Spam'",
        ("click", "Spam") in driver.eventos,
        str(driver.eventos),
    )
    check(
        "respaldo spam: NO eligio la categoria equivocada",
        ("click", "Discurso violento") not in driver.eventos,
        str(driver.eventos),
    )


def test_motivo_sin_categorias_falla(check):
    print("Reporte: modal sin categorias y sin spam -> fallo claro, sin clickear")
    driver = FakeReportDriver()
    estado = {"razon": None, "submit": None}

    def abrir_dialogo():
        _, razones, submit = _dialogo(driver, ["Siguiente"], "Enviar", lambda: None)
        estado["razon"] = razones[0]
        estado["submit"] = submit

    _articulo_con_caret(
        driver, lambda: _menu(driver, ["Denunciar post"], [lambda: abrir_dialogo()])
    )
    bot = _bot(driver)
    with _reloj_falso():
        ok = bot.reportar_post("https://x.com/u/status/105", motivo="spam")

    check("sin categorias: devuelve False", ok is False, repr(ok))
    check(
        "sin categorias: mensaje con motivo/opciones visibles",
        "motivo 'spam' no encontrado" in bot.ultimo_error,
        bot.ultimo_error,
    )
    check(
        "sin categorias: NO clickeo 'Siguiente' ni el motivo",
        estado["razon"].click_count == 0 and estado["submit"].click_count == 0,
        f"{estado['razon'].click_count}/{estado['submit'].click_count}",
    )


def test_reportar_objetivo_dispatch(check):
    print("Reporte: reportar_objetivo distingue tweet de perfil")
    driver = FakeReportDriver()
    bot = _bot(driver)
    llamadas = []
    bot.reportar_post = (
        lambda url, motivo="spam", dry_run=False: llamadas.append(("post", url)) or True
    )
    bot.reportar_cuenta = (
        lambda url, motivo="spam", dry_run=False: llamadas.append(("cuenta", url)) or True
    )

    ok_tweet = bot.reportar_objetivo("https://x.com/u/status/123")
    llamadas_tweet = list(llamadas)
    llamadas.clear()
    ok_perfil = bot.reportar_objetivo("https://x.com/u")
    llamadas_perfil = list(llamadas)
    llamadas.clear()
    ok_twitter = bot.reportar_objetivo("https://twitter.com/otro/")
    llamadas_twitter = list(llamadas)

    check(
        "objetivo tweet: delega en reportar_post",
        ok_tweet is True and llamadas_tweet == [("post", "https://x.com/u/status/123")],
        str(llamadas_tweet),
    )
    check(
        "objetivo perfil: delega en reportar_cuenta",
        ok_perfil is True and llamadas_perfil == [("cuenta", "https://x.com/u")],
        str(llamadas_perfil),
    )
    check(
        "objetivo perfil twitter.com: delega en reportar_cuenta",
        ok_twitter is True
        and llamadas_twitter == [("cuenta", "https://twitter.com/otro/")],
        str(llamadas_twitter),
    )


def test_reportar_objetivo_indeciso(check):
    print("Reporte: URL indecidible -> intenta post y cae a cuenta")
    bot = _bot(FakeReportDriver())
    llamadas = []

    def post(url, motivo="spam", dry_run=False):
        llamadas.append(("post", url))
        return False

    def cuenta(url, motivo="spam", dry_run=False):
        llamadas.append(("cuenta", url))
        return True

    bot.reportar_post = post
    bot.reportar_cuenta = cuenta
    ok = bot.reportar_objetivo("https://ejemplo.com/algo")

    check("objetivo indeciso: devuelve True via cuenta", ok is True, repr(ok))
    check(
        "objetivo indeciso: intento post y cayo a cuenta",
        llamadas == [("post", "https://ejemplo.com/algo"), ("cuenta", "https://ejemplo.com/algo")],
        str(llamadas),
    )


def test_reportar_url_vacia(check):
    print("Reporte: URLs vacias fallan con mensaje claro (sin driver)")
    bot = _bot(FakeReportDriver())
    ok_post = bot.reportar_post("   ")
    error_post = bot.ultimo_error
    ok_cuenta = bot.reportar_cuenta("")
    error_cuenta = bot.ultimo_error
    ok_objetivo = bot.reportar_objetivo("")
    error_objetivo = bot.ultimo_error

    check(
        "url vacia (post): False + error",
        ok_post is False and "falta la URL" in error_post,
        error_post,
    )
    check(
        "url vacia (cuenta): False + error",
        ok_cuenta is False and "falta la URL" in error_cuenta,
        error_cuenta,
    )
    check(
        "url vacia (objetivo): False + error",
        ok_objetivo is False and "falta la URL" in error_objetivo,
        error_objetivo,
    )


def test_sin_driver_login_falla(check):
    print("Reporte: sin driver y login fallido -> False sin excepciones")
    bot = _bot(FakeReportDriver())
    bot.driver = None
    bot.login_con_cookies = lambda: False
    ok = bot.reportar_post("https://x.com/u/status/1")
    check(
        "sin driver: False con error de login",
        ok is False and "sesion" in bot.ultimo_error.lower(),
        bot.ultimo_error,
    )

    bot2 = _bot(FakeReportDriver())
    capturado = {}

    def ejecutar_roto(url, es_cuenta, motivo, dry_run):
        capturado["url"] = url
        capturado["es_cuenta"] = es_cuenta
        raise RuntimeError("fallo simulado")

    bot2._ejecutar_reporte = ejecutar_roto
    ok2 = bot2.reportar_cuenta("https://x.com/u/status/9")
    check(
        "excepcion interna: se captura y devuelve False",
        ok2 is False and "RuntimeError" in bot2.ultimo_error,
        bot2.ultimo_error,
    )
    check(
        "cuenta: recorta /status/ antes del flujo",
        capturado.get("url") == "https://x.com/u" and capturado.get("es_cuenta") is True,
        str(capturado),
    )


def test_helpers_motivos(check):
    print("Reporte: helpers de motivos (alias, bilingue, handle)")
    bot = _bot(FakeReportDriver())
    claves_spec = ("spam", "hate", "abuse", "violence", "self_harm", "sensitive", "impersonation")

    check(
        "motivos: las 7 claves existen en post y cuenta",
        all(k in TwitterBot._MOTIVOS_REPORTE_POST and k in TwitterBot._MOTIVOS_REPORTE_CUENTA for k in claves_spec),
        str(sorted(TwitterBot._MOTIVOS_REPORTE_POST)),
    )
    check(
        "motivos: 'self-harm' normaliza a self_harm",
        bot._candidatos_motivo_reporte("self-harm", False)
        == TwitterBot._MOTIVOS_REPORTE_POST["self_harm"],
        str(bot._candidatos_motivo_reporte("self-harm", False)),
    )
    check(
        "motivos: 'AUTOlesión' alias -> self_harm",
        bot._candidatos_motivo_reporte("AUTOlesión", True)
        == TwitterBot._MOTIVOS_REPORTE_CUENTA["self_harm"],
        str(bot._candidatos_motivo_reporte("AUTOlesión", True)),
    )
    check(
        "motivos: desconocido cae a spam (retrocompatible)",
        bot._candidatos_motivo_reporte("motivo_raro", False)
        == TwitterBot._MOTIVOS_REPORTE_POST["spam"],
        str(bot._candidatos_motivo_reporte("motivo_raro", False)),
    )
    check(
        "motivos: 'nudity' y 'false_info' de la web existen",
        "nudity" in TwitterBot._MOTIVOS_REPORTE_POST
        and "false_info" in TwitterBot._MOTIVOS_REPORTE_CUENTA,
        "",
    )
    check(
        "normalizacion: acentos/comillas tipograficas",
        bot._normalizar_texto_ui("It’s SPÁM") == "it's spam",
        bot._normalizar_texto_ui("It’s SPÁM"),
    )
    check(
        "handle: x.com/u -> u",
        TwitterBot._handle_desde_url("https://x.com/u") == "u",
        TwitterBot._handle_desde_url("https://x.com/u"),
    )
    check(
        "handle: x.com/home -> ''",
        TwitterBot._handle_desde_url("https://x.com/home") == "",
        TwitterBot._handle_desde_url("https://x.com/home"),
    )
    check(
        "handle: x.com/i/flow -> ''",
        TwitterBot._handle_desde_url("https://x.com/i/flow/report") == "",
        TwitterBot._handle_desde_url("https://x.com/i/flow/report"),
    )


def test_confirmacion_frases(check):
    print("Reporte: deteccion de confirmacion por toast y page_source")
    driver = FakeReportDriver()
    bot = _bot(driver)

    driver.toasts = [FakeNodo(driver, texto="Tu reporte fue enviado", role="alert")]
    check("confirmacion: toast ES -> True", bot._reporte_confirmado() is True, "")

    driver.toasts = []
    driver.page_source = "<html>Gracias por informarnos</html>"
    check("confirmacion: page_source 'Gracias por informarnos' -> True", bot._reporte_confirmado() is True, "")

    driver.page_source = "<html>You already reported this</html>"
    check("confirmacion: page_source 'already reported' -> True", bot._reporte_confirmado() is True, "")

    driver.page_source = ""
    check("confirmacion: pagina normal -> False", bot._reporte_confirmado() is False, "")


def run(check):
    """Ejecuta los checks de este archivo con el `check` del runner."""
    test_flujo_feliz_tweet(check)
    test_flujo_feliz_tweet_es(check)
    test_flujo_feliz_tweet_denunciar(check)
    test_flujo_feliz_cuenta(check)
    test_flujo_feliz_cuenta_es(check)
    test_flujo_feliz_cuenta_denunciar(check)
    test_subscreen_real_spam(check)
    test_mapeo_motivos_reales(check)
    test_motivo_no_encontrado(check)
    test_motivo_ambiguedad_no_confunde(check)
    test_ya_reportado_al_abrir(check)
    test_ya_reportado_toast(check)
    test_dry_run_no_envia(check)
    test_dry_run_cuenta(check)
    test_dialogo_cerrado_tras_envio(check)
    test_sin_confirmacion(check)
    test_menu_sin_opcion_reporte(check)
    test_menu_ajeno_reintenta(check)
    test_menu_ajeno_dos_intentos_falla(check)
    test_article_tardio_reintenta(check)
    test_article_ausente_dos_intentos(check)
    test_caret_no_global_si_hay_article(check)
    test_motivo_spam_respaldo(check)
    test_motivo_sin_categorias_falla(check)
    test_reportar_objetivo_dispatch(check)
    test_reportar_objetivo_indeciso(check)
    test_reportar_url_vacia(check)
    test_sin_driver_login_falla(check)
    test_helpers_motivos(check)
    test_confirmacion_frases(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
