# -*- coding: utf-8 -*-
"""Tests rapidos del backend de reportes de Change.org (SIN Chrome y SIN red).

Cubre el contrato congelado de `cuentas/change_org.py`:

  (1) `generar_identidad_change`: formato/dominios/email sin acentos, `usados`
      evita duplicados, campos no vacios y llamadas distintas.
  (2) `proxies_disponibles`: usa `cargar_por_pais` / `cargar_proxies` y nunca
      lanza ([] ante error).
  (3) Localizacion y clic del enlace de reporte con FakeDriver: texto ES exacto,
      variante EN, `href` policy_violation, scroll hasta encontrarlo y JS click
      de fallback.
  (4) Llenado del formulario: resuelve nombre/apellido/email/textarea por
      etiqueta y por fallback; `send_keys` se llama CARACTER por caracter y el
      texto tipeado es el de la identidad/queja.
  (5) `_reporte_exitoso`: exito por texto, por URL y por formulario desaparecido;
      captcha -> fallo con motivo; error de pagina -> fallo.
  (6) `reportar()` con FakeDriver completo (ok True) y `cerrar()` con cierre
      GARANTIZADO del forward proxy (fake con `.close()` registrado).
  (7) `preparar_driver`: monkeypatch de `crear_chrome` (options con
      `--headless=new` cuando headless=True) y de `ProxyManager`
      (`aplicar_a_options` + fwd guardado).
  (8) `guardar_identidad_change`: engine sqlite EN MEMORIA (StaticPool); inserta,
      email duplicado -> motivo "duplicada", tabla faltante -> `init_db` +
      reintento, fallo real -> motivo "error"; NUNCA lanza y NUNCA toca
      `data/gestor_redes.db`.
  (9) `ejecutar_un_reporte` con `ChangeOrgReportBot` FAKE + `generar_queja_change`
      FAKE: la queja se pasa al bot, se guarda la identidad solo si ok y
      `bot.cerrar` se llama SIEMPRE (finally).
  (10) `ejecutar_campana_reportes` con `ejecutar_un_reporte` FAKE: round-robin
       de proxies, callbacks `inicio` + N `reporte` con `hechas` 1..N, resumen
       consistente, `cancelar` pre-seteado, excepciones contadas como fallo y
       nunca lanza.
  (18) MODO ASISTIDO (`esperar_captcha_seg`): resolucion kwarg/env/acotado,
       espera visible hasta que el reto desaparece, timeout con mensaje
       accionable exacto, headless nunca espera (y lo aclara), cancelacion
       durante la espera, `esperar_captcha_seg=0` conserva el fallo clasico,
       integracion con `_asegurar_campo_email`/`registrar_o_entrar` y el evento
       `espera_captcha` reenviado por las campanas (bot -> registrar/reporte ->
       callback, tambien al inicio del registro).

Determinista: `time.sleep` se neutraliza y todo Chrome/BD/proxy/IA esta
monkeypatcheado. No se ejecutan campanas reales.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_backend.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import threading
import time
import types
from collections import Counter
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as database  # noqa: E402
import cuentas.change_org as change_org  # noqa: E402
import ia.generador_contenido as gc  # noqa: E402
from core.database import Base  # noqa: E402
from core.models import CuentaChange  # noqa: E402
from cuentas.change_org import (  # noqa: E402
    MENSAJE_CANCELADO,
    ChangeOrgReportBot,
    ejecutar_campana_registros,
    ejecutar_campana_reportes,
    ejecutar_un_reporte,
    generar_identidad_change,
    guardar_identidad_change,
    password_change_para,
    proxies_disponibles,
    registrar_cuenta_change,
)


@contextlib.contextmanager
def _sin_esperas():
    """Neutraliza `time.sleep` para que la suite sea rapida y determinista."""
    original = time.sleep
    time.sleep = lambda *args, **kwargs: None
    try:
        yield
    finally:
        time.sleep = original


# --------------------------------------------------------------------------- #
# Fakes: driver/elemento con la API de Selenium que usa el bot
# --------------------------------------------------------------------------- #
class FakeFwd:
    """Forward proxy local fake: registra si `cerrar()` lo cerro."""

    def __init__(self):
        self.cerrado = False
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.cerrado = True


class _ServicioFake:
    def __init__(self):
        self.llamados = []

    def stop(self):
        self.llamados.append("stop")

    def terminate(self):
        self.llamados.append("terminate")

    def kill(self):
        self.llamados.append("kill")


class _FakeSwitchTo:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.ventana_actual = handle


def _coincide_atributo(elemento, expresion) -> bool:
    """Mini-matcher de un atributo CSS: [name], [name='x'], [name*='x'], ..."""
    expresion = expresion.strip()
    if "=" not in expresion:
        return elemento.get_attribute(expresion) not in (None, "")
    attr, valor = expresion.split("=", 1)
    op = "="
    if attr.endswith(("*", "^", "$")):
        op, attr = attr[-1], attr[:-1]
    valor = valor.strip().strip("'\"")
    if valor.lower().endswith(" i"):  # flag case-insensitive de CSS
        valor = valor[:-2].rstrip().strip("'\"")
    actual = str(elemento.get_attribute(attr) or "")
    if op == "*":
        return valor.lower() in actual.lower()
    if op == "^":
        return actual.lower().startswith(valor.lower())
    if op == "$":
        return actual.lower().endswith(valor.lower())
    return actual.lower() == valor.lower()


def _coincide_selector(elemento, selector: str) -> bool:
    """Mini-matcher CSS suficiente para los selectores que usa el bot."""
    selector = selector.strip()
    if selector.startswith("[") and selector.endswith("]"):
        return _coincide_atributo(elemento, selector[1:-1])
    if selector.startswith("."):
        clases = str(elemento.get_attribute("class") or "").split()
        return selector[1:] in clases
    coincide = re.match(r"^([A-Za-z][A-Za-z0-9]*)((\[[^\]]+\])*)$", selector)
    if not coincide:
        return False
    if elemento.tag != coincide.group(1).lower():
        return False
    for attr_sel in re.findall(r"\[[^\]]+\]", coincide.group(2)):
        if not _coincide_atributo(elemento, attr_sel[1:-1]):
            return False
    return True


class FakeElement:
    """Elemento fake con la API de Selenium que consume `change_org`."""

    def __init__(
        self,
        driver,
        tag="div",
        texto="",
        href=None,
        attrs=None,
        visible=True,
        enabled=True,
        click_falla=False,
        js_click_falla=False,
        hijos=None,
    ):
        self.driver = driver
        self.tag = tag
        self._texto = texto
        self.href = href
        self.attrs = {str(k).lower(): v for k, v in (attrs or {}).items()}
        self.visible = visible
        self.enabled = enabled
        self.click_falla = click_falla
        self.js_click_falla = js_click_falla
        self.hijos = list(hijos or [])
        self.siguiente = None  # relacion "following::input[1]"
        self.click_count = 0
        self.js_click_count = 0
        self.focus_count = 0
        self.scroll_count = 0
        self.typed = []
        self.al_click = None

    # --- API Selenium ---
    @property
    def text(self):
        return self._texto

    def get_attribute(self, nombre):
        nombre = str(nombre or "").lower()
        if nombre == "href":
            return self.href
        if nombre == "id":
            return self.attrs.get("id")
        return self.attrs.get(nombre)

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self):
        if self.click_falla:
            raise RuntimeError("click interceptado")
        self.click_count += 1
        self.driver.clicks.append(self)
        if self.al_click:
            self.al_click()

    def send_keys(self, *valores):
        for valor in valores:
            if valor == Keys.BACKSPACE:
                if self.typed:
                    self.typed.pop()
                continue
            for char in str(valor):
                self.typed.append(char)

    def find_elements(self, by, selector):
        if by == By.XPATH:
            if "following::input" in selector:
                if self.siguiente is not None and self.siguiente.tag == "input":
                    return [self.siguiente]
                return []
            if "following::textarea" in selector:
                if self.siguiente is not None and self.siguiente.tag == "textarea":
                    return [self.siguiente]
                return []
            if "following::select" in selector:
                if self.siguiente is not None and self.siguiente.tag == "select":
                    return [self.siguiente]
                return []
            if selector in ("./select", ".//select"):
                return [h for h in self.hijos if h.tag == "select"]
            return []
        if by == By.CSS_SELECTOR:
            return [h for h in self.hijos if _coincide_selector(h, selector)]
        if by == By.TAG_NAME:
            return [h for h in self.hijos if h.tag == selector]
        return []

    def find_element(self, by, selector):
        encontrados = self.find_elements(by, selector)
        if not encontrados:
            raise NoSuchElementException(f"no encontrado: {selector}")
        return encontrados[0]


class FakeDriver:
    """Driver fake: elementos registrados + scripts + ventanas."""

    def __init__(self):
        self.url = ""
        self.current_url = "about:blank"
        self.title = ""
        self.page_source = ""
        self.body = None
        self.window_handles = ["w1"]
        self.ventana_actual = "w1"
        self.timeouts = []
        self.script_timeouts = []
        self.scripts = []
        self.clicks = []
        self.quit_count = 0
        self.quit_falla = False
        self.service = None
        self.al_navegar = None
        self.elementos = []
        self.switch_to = _FakeSwitchTo(self)

    def registrar(self, *elementos):
        for elemento in elementos:
            elemento.driver = self
            self.elementos.append(elemento)
        return elementos[0] if len(elementos) == 1 else elementos

    def get(self, url):
        self.url = url
        self.current_url = url
        if self.al_navegar:
            self.al_navegar(url)

    def set_page_load_timeout(self, valor):
        self.timeouts.append(valor)

    def set_script_timeout(self, valor):
        self.script_timeouts.append(valor)

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if args and isinstance(args[0], FakeElement):
            elemento = args[0]
            if "scrollIntoView" in script or "scrollBy" in script:
                elemento.scroll_count += 1
            if "click()" in script:
                if elemento.js_click_falla:
                    raise RuntimeError("js click fallo")
                elemento.js_click_count += 1
                self.clicks.append(elemento)
                if elemento.al_click:
                    elemento.al_click()
            if "focus()" in script:
                elemento.focus_count += 1
        return None

    def find_elements(self, by, selector):
        if by == By.TAG_NAME:
            return [el for el in self.elementos if el.tag == selector]
        if by == By.CSS_SELECTOR:
            return [el for el in self.elementos if _coincide_selector(el, selector)]
        if by == By.XPATH:
            return self._xpath(selector)
        return []

    def _xpath(self, expr):
        expr = str(expr)
        if "contains(@href" in expr:
            fragmento = re.search(r"contains\(@href,'([^']+)'\)", expr)
            if not fragmento:
                return []
            tag = "a" if expr.strip().startswith("//a") else None
            texto = fragmento.group(1).lower()
            return [
                el for el in self.elementos
                if (tag is None or el.tag == tag)
                and texto in str(el.href or "").lower()
            ]
        if "contains(.," in expr:
            fragmento = re.search(r"contains\(\.\s*,'([^']+)'\)", expr)
            if not fragmento:
                return []
            etiqueta = re.match(r"//([A-Za-z]+)\[", expr)
            tag = etiqueta.group(1).lower() if etiqueta else None
            texto = fragmento.group(1).lower()
            return [
                el for el in self.elementos
                if (tag is None or el.tag == tag) and texto in el.text.lower()
            ]
        return []

    def find_element(self, by, selector):
        if by == By.TAG_NAME and selector == "body" and self.body is not None:
            return self.body
        encontrados = self.find_elements(by, selector)
        if not encontrados:
            raise NoSuchElementException(f"no encontrado: {selector}")
        return encontrados[0]

    def quit(self):
        self.quit_count += 1
        if self.quit_falla:
            raise RuntimeError("quit fallo")


class _DriverConScroll(FakeDriver):
    """Driver que solo 've' los elementos despues del primer window.scrollBy."""

    def find_elements(self, by, selector):
        if not any("scrollBy" in s for s in self.scripts):
            return []
        return super().find_elements(by, selector)


def _bot(driver=None, **kwargs):
    bot = ChangeOrgReportBot(**kwargs)
    if driver is not None:
        bot.driver = driver
    return bot


# --------------------------------------------------------------------------- #
# (1) generar_identidad_change
# --------------------------------------------------------------------------- #
def test_generar_identidad(check):
    print("(1) generar_identidad_change")
    identidad = generar_identidad_change()
    check(
        "identidad: claves exactas del contrato",
        set(identidad) == {"nombre", "apellido", "email", "codigo_postal"},
        str(sorted(identidad)),
    )
    check(
        "identidad: campos no vacios",
        all(str(valor).strip() for valor in identidad.values()),
        str(identidad),
    )
    check(
        "identidad: email nombre.apellido + 2-4 digitos en dominio valido",
        re.fullmatch(
            r"[a-z0-9]+\.[a-z0-9]+[0-9]{2,4}"
            r"@(gmail|hotmail|outlook|yahoo)\.com",
            identidad["email"],
        )
        is not None,
        identidad["email"],
    )
    check(
        "identidad: email sin acentos ni simbolos raros",
        identidad["email"].isascii()
        and " " not in identidad["email"]
        and "ñ" not in identidad["email"],
    )
    check(
        "identidad: codigo postal no vacio",
        bool(str(identidad["codigo_postal"]).strip()),
        identidad["codigo_postal"],
    )

    primera = generar_identidad_change()
    emails = {primera["email"]}
    for _ in range(4):
        emails.add(generar_identidad_change(usados=emails)["email"])
    check("identidad: 5 llamadas con usados -> 5 emails unicos", len(emails) == 5)

    usados = {primera["email"]}
    segunda = generar_identidad_change(usados=usados)
    check(
        "identidad: evita un email ya usado (set)",
        segunda["email"] != primera["email"],
        f"{segunda['email']} vs {primera['email']}",
    )
    tercera = generar_identidad_change(usados=[{"email": primera["email"]}])
    check(
        "identidad: usados acepta dicts con email",
        tercera["email"] != primera["email"],
    )
    distintas = True
    for _ in range(3):
        a = generar_identidad_change()
        b = generar_identidad_change()
        distintas = distintas and a["email"] != b["email"]
    check(
        "identidad: dos llamadas seguidas dan identidades distintas",
        distintas,
    )

    class _FakerCustom:
        def first_name(self):
            return "José"

        def last_name(self):
            return "Núñez"

        def postcode(self):
            return "44100"

    con_faker = generar_identidad_change(faker=_FakerCustom())
    check(
        "identidad: usa el Faker inyectado",
        con_faker["nombre"] == "José"
        and con_faker["apellido"] == "Núñez"
        and con_faker["codigo_postal"] == "44100",
        str(con_faker),
    )
    check(
        "identidad: email del Faker sin acentos (jose.nunez...)",
        con_faker["email"].startswith("jose.nunez"),
        con_faker["email"],
    )

    class _FakerRoto:
        def first_name(self):
            raise RuntimeError("boom")

        def last_name(self):
            raise RuntimeError("boom")

        def postcode(self):
            raise RuntimeError("boom")

    con_roto = generar_identidad_change(faker=_FakerRoto())
    check(
        "identidad: Faker roto -> respaldo local no vacio",
        all(str(v).strip() for v in con_roto.values()),
        str(con_roto),
    )


# --------------------------------------------------------------------------- #
# (2) proxies_disponibles
# --------------------------------------------------------------------------- #
def test_proxies(check):
    print("(2) proxies_disponibles")
    capturado = {}

    class _ProxyManagerFake:
        def cargar_por_pais(self, pais):
            capturado["pais"] = pais
            return ["p1", "p2"]

        def cargar_proxies(self):
            capturado["todos"] = True
            return ["p3"]

    with mock.patch.object(change_org, "ProxyManager", _ProxyManagerFake):
        check(
            "proxies: con pais usa cargar_por_pais",
            proxies_disponibles("mexico") == ["p1", "p2"]
            and capturado.get("pais") == "mexico",
        )
        check(
            "proxies: sin pais usa cargar_proxies",
            proxies_disponibles() == ["p3"] and capturado.get("todos") is True,
        )

    class _ProxyManagerRoto:
        def cargar_proxies(self):
            raise RuntimeError("sin red")

        def cargar_por_pais(self, pais):
            raise RuntimeError("sin red")

    with mock.patch.object(change_org, "ProxyManager", _ProxyManagerRoto):
        check(
            "proxies: nunca lanza -> [] si falla",
            proxies_disponibles("usa") == [] and proxies_disponibles() == [],
        )


# --------------------------------------------------------------------------- #
# (3) Localizacion y clic del enlace
# --------------------------------------------------------------------------- #
def test_enlace(check):
    print("(3) localizacion y clic del enlace de reporte")
    with _sin_esperas():
        driver = FakeDriver()
        enlace = FakeElement(
            driver, tag="button", texto="Denunciar una violación de las políticas"
        )
        driver.registrar(enlace)
        bot = _bot(driver)
        check(
            "enlace: texto ES exacto (con acentos)",
            bot._buscar_enlace_reporte() is enlace,
        )

        driver = FakeDriver()
        enlace = FakeElement(driver, tag="a", texto="Report a policy violation")
        driver.registrar(enlace)
        check(
            "enlace: variante EN 'Report a policy violation'",
            _bot(driver)._buscar_enlace_reporte() is enlace,
        )

        driver = FakeDriver()
        enlace = FakeElement(
            driver, tag="span", texto="DENUNCIAR UNA VIOLACIÓN DE LAS POLÍTICAS"
        )
        driver.registrar(enlace)
        check(
            "enlace: tolera MAYUSCULAS y acentos (normaliza)",
            _bot(driver)._buscar_enlace_reporte() is enlace,
        )

        driver = FakeDriver()
        enlace = FakeElement(
            driver,
            tag="a",
            texto="",
            href="https://www.change.org/xyz/policy_violation/123",
        )
        driver.registrar(enlace)
        check(
            "enlace: href con policy_violation",
            _bot(driver)._buscar_enlace_reporte() is enlace,
        )

        driver = FakeDriver()
        enlace = FakeElement(
            driver,
            tag="a",
            texto="",
            href="https://www.change.org/policy-violation/report",
        )
        driver.registrar(enlace)
        check(
            "enlace: href con policy-violation (guion)",
            _bot(driver)._buscar_enlace_reporte() is enlace,
        )

        driver = _DriverConScroll()
        enlace = FakeElement(
            driver, tag="button", texto="Denunciar una violación de las políticas"
        )
        driver.registrar(enlace)
        encontrado = _bot(driver)._buscar_enlace_reporte(intentos=3)
        check(
            "enlace: scroll humano hasta encontrarlo",
            encontrado is enlace and any("scrollBy" in s for s in driver.scripts),
        )

        driver = FakeDriver()
        enlace = FakeElement(
            driver, tag="button", texto="Denunciar una violación de las políticas"
        )
        driver.registrar(enlace)
        bot = _bot(driver)
        check("clic: clic normal funciona", bot._clic_elemento(enlace) is True)
        check(
            "clic: scrollIntoView antes de clicar",
            enlace.scroll_count >= 1 and enlace.click_count == 1,
        )

        driver = FakeDriver()
        enlace = FakeElement(
            driver,
            tag="button",
            texto="Denunciar una violación de las políticas",
            click_falla=True,
        )
        driver.registrar(enlace)
        check(
            "clic: fallback por JS click",
            _bot(driver)._clic_elemento(enlace) is True
            and enlace.js_click_count == 1
            and enlace.click_count == 0,
        )

        driver = FakeDriver()
        enlace = FakeElement(driver, tag="a", texto="Report a policy violation")
        enlace.al_click = lambda: driver.window_handles.append("w2")
        driver.registrar(enlace)
        _bot(driver)._clic_elemento(enlace)
        check(
            "clic: si abre pestaña nueva cambia a la ultima ventana",
            driver.ventana_actual == "w2",
        )

        driver = FakeDriver()
        check(
            "enlace: None cuando no existe (tras agotar scroll)",
            _bot(driver)._buscar_enlace_reporte(intentos=2) is None,
        )


# --------------------------------------------------------------------------- #
# (4) Formulario y escritura humana
# --------------------------------------------------------------------------- #
def test_formulario(check):
    print("(4) llenado de formulario y escritura humana")
    identidad = {
        "nombre": "Ana",
        "apellido": "López",
        "email": "ana.lopez12@gmail.com",
        "codigo_postal": "44100",
    }
    queja = "Considero que esta peticion incumple las normas de la comunidad."
    with _sin_esperas():
        driver = FakeDriver()
        nombre = FakeElement(
            driver, tag="input", attrs={"autocomplete": "given-name", "type": "text"}
        )
        apellido = FakeElement(
            driver, tag="input", attrs={"autocomplete": "family-name", "type": "text"}
        )
        email = FakeElement(driver, tag="input", attrs={"type": "email"})
        motivo = FakeElement(driver, tag="textarea", attrs={"name": "reason"})
        driver.registrar(nombre, apellido, email, motivo)
        bot = _bot(driver)
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            ok, error = bot._llenar_formulario(identidad, queja)
        check("formulario: llena todo (ok, sin error)", ok is True and error == "")
        check(
            "formulario: nombre tipeado = identidad",
            "".join(nombre.typed) == identidad["nombre"],
            "".join(nombre.typed),
        )
        check(
            "formulario: apellido tipeado = identidad",
            "".join(apellido.typed) == identidad["apellido"],
        )
        check(
            "formulario: email tipeado = identidad",
            "".join(email.typed) == identidad["email"],
        )
        check(
            "formulario: motivo tipeado = queja",
            "".join(motivo.typed) == queja,
        )
        check(
            "formulario: send_keys caracter por caracter",
            len(nombre.typed) == len(identidad["nombre"])
            and len(email.typed) == len(identidad["email"]),
        )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="input")
        bot = _bot(driver)
        with mock.patch.object(change_org.random, "random", return_value=0.0):
            check(
                "escritura: typo ocasional corregido con BACKSPACE",
                bot.escribir_humano(campo, "hola") is True
                and "".join(campo.typed) == "hola",
                "".join(campo.typed),
            )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="textarea")
        bot = _bot(driver)
        largo = "Considero que esta peticion incumple las normas. " * 12
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            check(
                "escritura: variante rapida escribe una queja larga completa",
                bot.escribir_humano(campo, largo, pausas_rapidas=True) is True
                and "".join(campo.typed) == largo,
                str(len("".join(campo.typed))),
            )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="input", attrs={"id": "campo-uno"})
        label = FakeElement(
            driver, tag="label", texto="Nombre", attrs={"for": "campo-uno"}
        )
        driver.registrar(campo, label)
        check(
            "formulario: resuelve nombre por label[for]/id",
            _bot(driver)._resolver_campo("nombre") is campo,
        )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="input")
        label = FakeElement(driver, tag="label", texto="Nombre")
        label.siguiente = campo
        driver.registrar(campo, label)
        check(
            "formulario: resuelve nombre por following::input[1]",
            _bot(driver)._resolver_campo("nombre") is campo,
        )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="textarea")
        label = FakeElement(driver, tag="label", texto="Motivo del reporte", hijos=[campo])
        driver.registrar(campo, label)
        check(
            "formulario: resuelve motivo por label envolvente",
            _bot(driver)._resolver_campo("motivo") is campo,
        )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="input", attrs={"name": "userEmail"})
        driver.registrar(campo)
        check(
            "formulario: email por fallback input[name*='email']",
            _bot(driver)._resolver_campo("email") is campo,
        )

        driver = FakeDriver()
        campo = FakeElement(driver, tag="div", attrs={"contenteditable": "true"})
        driver.registrar(campo)
        check(
            "formulario: motivo por [contenteditable='true']",
            _bot(driver)._resolver_campo("motivo") is campo,
        )

        driver = FakeDriver()
        check(
            "formulario: sin campos -> error descriptivo",
            _bot(driver)._llenar_formulario(identidad, queja)[1]
            == "no se encontro el campo de nombre en el formulario",
        )


# --------------------------------------------------------------------------- #
# (5) _reporte_exitoso / captcha / errores
# --------------------------------------------------------------------------- #
def test_resultado(check):
    print("(5) evidencia de exito, captcha y errores")
    driver = FakeDriver()
    driver.page_source = "<html>Gracias, hemos recibido tu denuncia</html>"
    check(
        "resultado: exito por texto de gracias",
        _bot(driver)._reporte_exitoso(formulario_presente=True)
        == (True, "confirmacion en pantalla: 'hemos recibido tu denuncia'"),
    )

    driver = FakeDriver()
    driver.current_url = "https://www.change.org/p/x/thank-you"
    ok, evidencia = _bot(driver)._reporte_exitoso(formulario_presente=True)
    check("resultado: exito por URL de confirmacion", ok is True and "URL" in evidencia)

    driver = FakeDriver()
    ok, evidencia = _bot(driver)._reporte_exitoso()
    check(
        "resultado: exito por formulario desaparecido sin error",
        ok is True and "desaparecio" in evidencia,
    )

    driver = FakeDriver()
    driver.registrar(
        FakeElement(driver, tag="div", attrs={"class": "g-recaptcha"})
    )
    ok, evidencia = _bot(driver)._reporte_exitoso(formulario_presente=True)
    check(
        "resultado: captcha por selector -> fallo con motivo",
        ok is False and "captcha" in evidencia.lower(),
        evidencia,
    )

    driver = FakeDriver()
    driver.page_source = "<p>Confirma que no soy un robot para continuar</p>"
    ok, evidencia = _bot(driver)._reporte_exitoso(formulario_presente=True)
    check(
        "resultado: captcha por texto -> fallo con motivo",
        ok is False and "captcha" in evidencia.lower(),
        evidencia,
    )

    driver = FakeDriver()
    driver.page_source = "<p>Algo salió mal, intenta de nuevo</p>"
    ok, evidencia = _bot(driver)._reporte_exitoso(formulario_presente=True)
    check(
        "resultado: error de pagina -> fallo con motivo",
        ok is False and "algo salio mal" in evidencia,
        evidencia,
    )

    driver = FakeDriver()
    driver.registrar(
        FakeElement(driver, tag="input", attrs={"type": "email"}),
        FakeElement(driver, tag="textarea", attrs={"name": "reason"}),
    )
    ok, evidencia = _bot(driver)._reporte_exitoso()
    check(
        "resultado: sin evidencia y con formulario -> NO exito",
        ok is False and "sin evidencia" in evidencia,
        evidencia,
    )


# --------------------------------------------------------------------------- #
# (6) reportar() completo + cerrar()
# --------------------------------------------------------------------------- #
def _driver_formulario_completo():
    driver = FakeDriver()
    enlace = FakeElement(
        driver, tag="button", texto="Denunciar una violación de las políticas"
    )
    nombre = FakeElement(driver, tag="input", attrs={"autocomplete": "given-name"})
    apellido = FakeElement(driver, tag="input", attrs={"autocomplete": "family-name"})
    email = FakeElement(driver, tag="input", attrs={"type": "email"})
    motivo = FakeElement(driver, tag="textarea", attrs={"name": "reason"})
    enviar = FakeElement(
        driver, tag="button", attrs={"type": "submit"}, texto="Enviar denuncia"
    )
    driver.registrar(enlace, nombre, apellido, email, motivo, enviar)
    piezas = {
        "enlace": enlace,
        "nombre": nombre,
        "apellido": apellido,
        "email": email,
        "motivo": motivo,
        "enviar": enviar,
    }
    return driver, piezas


def test_reportar(check):
    print("(6) reportar() completo y cerrar()")
    identidad = {
        "nombre": "Ana",
        "apellido": "Lopez",
        "email": "ana.lopez12@gmail.com",
        "codigo_postal": "44100",
    }
    queja = "Queja de prueba para el reporte."
    with _sin_esperas():
        driver, piezas = _driver_formulario_completo()

        def _confirmar():
            driver.current_url = "https://www.change.org/p/x/thank-you"
            driver.page_source = "Gracias, hemos recibido tu reporte"

        piezas["enviar"].al_click = _confirmar
        bot = _bot(driver, url_peticion="https://www.change.org/p/x", timeout=33)
        bot.preparar_driver = lambda: True
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            resultado = bot.reportar(identidad=identidad, queja=queja)

        check("reportar: ok True", resultado["ok"] is True, str(resultado["error"]))
        check(
            "reportar: identidad en el resultado",
            resultado["email"] == identidad["email"]
            and resultado["nombre"] == identidad["nombre"]
            and resultado["apellido"] == identidad["apellido"],
        )
        check("reportar: queja en el resultado", resultado["queja"] == queja)
        check(
            "reportar: evidencia positiva (URL o pantalla)",
            "URL" in resultado["evidencia"] or "confirmacion" in resultado["evidencia"],
            resultado["evidencia"],
        )
        check(
            "reportar: texto tipeado en el formulario",
            "".join(piezas["nombre"].typed) == identidad["nombre"]
            and "".join(piezas["motivo"].typed) == queja,
        )
        check(
            "reportar: enlace y boton Enviar clicados",
            piezas["enlace"].click_count >= 1 and piezas["enviar"].click_count == 1,
        )
        check("reportar: url final del resultado", "thank" in resultado["url"])
        check("reportar: sin error", resultado["error"] == "", repr(resultado["error"]))

        # Captcha tras enviar -> fallo con motivo claro.
        driver, piezas = _driver_formulario_completo()
        piezas["enviar"].al_click = lambda: setattr(
            driver, "page_source", "Verifica que no soy un robot"
        )
        bot = _bot(driver, url_peticion="https://www.change.org/p/x")
        bot.preparar_driver = lambda: True
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "reportar: captcha -> ok False y error con captcha",
            resultado["ok"] is False and "captcha" in resultado["error"].lower(),
            resultado["error"],
        )

        # Enlace inexistente -> error descriptivo (sin exito falso).
        driver = FakeDriver()
        bot = _bot(driver, url_peticion="https://www.change.org/p/x")
        bot.preparar_driver = lambda: True
        resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "reportar: sin enlace -> ok False y error claro",
            resultado["ok"] is False
            and "no se encontro el enlace" in resultado["error"],
            resultado["error"],
        )

        # Cancelacion al inicio: NO se abre navegador.
        llamadas = []
        driver = FakeDriver()
        bot = _bot(driver, url_peticion="https://www.change.org/p/x")
        bot.preparar_driver = lambda: (llamadas.append(1), True)[1]
        evento = threading.Event()
        evento.set()
        bot.cancelar = evento
        resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "reportar: cancelado al inicio sin abrir navegador",
            resultado["ok"] is False
            and resultado.get("cancelado") is True
            and resultado["error"] == MENSAJE_CANCELADO
            and llamadas == [],
        )

        # Sin URL -> error, sin abrir navegador.
        bot = ChangeOrgReportBot(url_peticion="")
        check(
            "reportar: sin URL -> error",
            bot.reportar(identidad=identidad, queja=queja)["ok"] is False,
        )

    # cerrar(): driver.quit + fwd proxy cerrado SIEMPRE.
    fwd = FakeFwd()
    driver = FakeDriver()
    bot = ChangeOrgReportBot()
    bot.driver = driver
    bot._fwd_proxy = fwd
    bot.cerrar()
    check(
        "cerrar: driver.quit llamado y fwd proxy cerrado",
        driver.quit_count == 1 and fwd.cerrado is True and bot.driver is None,
    )
    bot.cerrar()  # idempotente: no lanza
    check("cerrar: idempotente (no lanza la segunda vez)", fwd.close_count == 1)

    servicio = _ServicioFake()
    driver = FakeDriver()
    driver.quit_falla = True
    driver.service = servicio
    bot = ChangeOrgReportBot()
    bot.driver = driver
    bot._fwd_proxy = FakeFwd()
    lanzado = False
    try:
        bot.cerrar()
    except Exception:
        lanzado = True
    check(
        "cerrar: fallback service.stop si quit falla y nunca lanza",
        servicio.llamados == ["stop"] and lanzado is False,
    )


# --------------------------------------------------------------------------- #
# (7) preparar_driver
# --------------------------------------------------------------------------- #
def test_preparar_driver(check):
    print("(7) preparar_driver (Chrome/proxy/stealth)")
    capturado = {}
    driver_falso = FakeDriver()
    fwd = FakeFwd()

    def _crear_chrome(options, version_main=None, intentos=3):
        capturado["options"] = options
        capturado["version"] = version_main
        return driver_falso

    class _ProxyManagerFake:
        def aplicar_a_options(self, options, proxy, tag="change", dinamico=False):
            capturado["proxy"] = proxy
            capturado["tag"] = tag
            return fwd

    with mock.patch.object(change_org, "crear_chrome", _crear_chrome), mock.patch.object(
        change_org, "detectar_chrome_version", lambda: 151
    ), mock.patch.object(
        change_org,
        "aplicar_stealth",
        lambda driver: capturado.setdefault("stealth", driver),
    ), mock.patch.object(
        change_org, "ProxyManager", _ProxyManagerFake
    ):
        bot = ChangeOrgReportBot(
            url_peticion="u",
            proxy="host:1:user:pass",
            headless=True,
            timeout=33,
        )
        check("preparar: True al crear el driver", bot.preparar_driver() is True)
        check(
            "preparar: headless=True agrega --headless=new",
            "--headless=new" in capturado["options"].arguments,
        )
        check(
            "preparar: aplica proxy con tag 'change'",
            capturado.get("tag") == "change"
            and capturado.get("proxy") == "host:1:user:pass",
        )
        check("preparar: guarda el forward proxy", bot._fwd_proxy is fwd)
        check(
            "preparar: page_load_timeout = timeout",
            driver_falso.timeouts == [33],
            str(driver_falso.timeouts),
        )
        check(
            "preparar: stealth aplicado al driver",
            capturado.get("stealth") is driver_falso,
        )
        check("preparar: version de Chrome detectada", capturado.get("version") == 151)
        check("preparar: True inmediato si ya hay driver", bot.preparar_driver() is True)

    capturado2 = {}
    driver_falso2 = FakeDriver()

    def _crear_chrome2(options, version_main=None, intentos=3):
        capturado2["options"] = options
        return driver_falso2

    with mock.patch.object(change_org, "crear_chrome", _crear_chrome2), mock.patch.object(
        change_org, "detectar_chrome_version", lambda: None
    ), mock.patch.object(change_org, "aplicar_stealth", lambda driver: True):
        bot = ChangeOrgReportBot(headless=False)
        bot.preparar_driver()
        check(
            "preparar: headless=False NO agrega --headless=new",
            "--headless=new" not in capturado2["options"].arguments,
        )

        capturado2.clear()
        with mock.patch.object(
            change_org, "settings", types.SimpleNamespace(headless=True)
        ):
            bot = ChangeOrgReportBot(headless=None)
            bot.preparar_driver()
            check(
                "preparar: headless=None usa settings.headless",
                "--headless=new" in capturado2["options"].arguments,
            )

        capturado2.clear()
        with mock.patch.object(change_org, "aplicar_stealth", None):
            bot = ChangeOrgReportBot(headless=True)
            check(
                "preparar: sin modulo anti_detection no falla",
                bot.preparar_driver() is True,
            )

    # Fallo de Chromedriver -> False y fwd SIEMPRE cerrado.
    fwd2 = FakeFwd()

    class _ProxyManagerFake2:
        def aplicar_a_options(self, options, proxy, tag="change", dinamico=False):
            return fwd2

    def _crear_chrome_roto(options, version_main=None, intentos=3):
        raise RuntimeError("no hay Chrome")

    with mock.patch.object(change_org, "crear_chrome", _crear_chrome_roto), mock.patch.object(
        change_org, "detectar_chrome_version", lambda: None
    ), mock.patch.object(change_org, "ProxyManager", _ProxyManagerFake2):
        bot = ChangeOrgReportBot(proxy="host:1:user:pass")
        check(
            "preparar: False si crear_chrome falla",
            bot.preparar_driver() is False and bot.driver is None,
        )
        check("preparar: cierra el fwd si falla al crear Chrome", fwd2.cerrado is True)
        check(
            "preparar: error guardado en ultimo_error",
            "no se pudo iniciar el navegador" in bot.ultimo_error,
        )


# --------------------------------------------------------------------------- #
# (8) guardar_identidad_change
# --------------------------------------------------------------------------- #
def _engine_memoria():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_guardar_identidad(check):
    print("(8) guardar_identidad_change (sqlite en memoria)")
    identidad = {
        "nombre": "Ana",
        "apellido": "Lopez",
        "email": "ana.lopez1@gmail.com",
        "codigo_postal": "44100",
    }
    engine = _engine_memoria()
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    original = database.SessionLocal
    database.SessionLocal = fabrica
    try:
        primera = guardar_identidad_change(
            identidad,
            url_peticion="https://www.change.org/p/x",
            contexto="contexto X",
            queja="queja X",
            origen="reporte",
        )
        check(
            "guardar: inserta -> guardada True con id",
            primera["guardada"] is True and isinstance(primera["id"], int),
            str(primera),
        )
        check("guardar: motivo vacio al insertar", primera["motivo"] == "")

        db = fabrica()
        try:
            fila = db.query(CuentaChange).filter_by(email=identidad["email"]).first()
            check(
                "guardar: fila persistida con todos los campos",
                fila is not None
                and fila.nombre == "Ana"
                and fila.apellido == "Lopez"
                and fila.codigo_postal == "44100"
                and fila.url_peticion == "https://www.change.org/p/x"
                and fila.contexto == "contexto X"
                and fila.queja == "queja X"
                and fila.origen == "reporte"
                and fila.usada_firma is False,
            )
            check(
                "guardar: fecha_creacion asignada",
                fila.fecha_creacion is not None,
            )
        finally:
            db.close()

        segunda = guardar_identidad_change(identidad, queja="otra")
        check(
            "guardar: email duplicado -> motivo duplicada con id existente",
            segunda["guardada"] is False
            and segunda["motivo"] == "duplicada"
            and segunda["id"] == primera["id"],
            str(segunda),
        )
        db = fabrica()
        try:
            check(
                "guardar: el duplicado no crea una segunda fila",
                db.query(CuentaChange).count() == 1,
            )
        finally:
            db.close()

        sin_email = guardar_identidad_change({"nombre": "X"})
        check(
            "guardar: identidad sin email -> motivo error",
            sin_email["guardada"] is False and sin_email["motivo"] == "error",
        )

        tercera = guardar_identidad_change(
            {**identidad, "email": identidad["email"].upper()}
        )
        check(
            "guardar: duplicado por mayusculas/minusculas",
            tercera["guardada"] is False and tercera["motivo"] == "duplicada",
        )
    finally:
        database.SessionLocal = original
        engine.dispose()

    # Tabla inexistente -> init_db UNA vez y reintento.
    engine = _engine_memoria()
    fabrica = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    original = database.SessionLocal
    original_init = database.init_db
    llamadas = []
    database.SessionLocal = fabrica

    def _init_fake():
        llamadas.append(1)
        Base.metadata.create_all(engine)

    database.init_db = _init_fake
    try:
        resultado = guardar_identidad_change(
            {"nombre": "Luis", "apellido": "Gomez", "email": "luis.gomez2@gmail.com"}
        )
        check(
            "guardar: tabla faltante -> init_db y reintenta",
            resultado["guardada"] is True and llamadas == [1],
            str(resultado),
        )
    finally:
        database.init_db = original_init
        database.SessionLocal = original
        engine.dispose()

    # Fallo real de BD -> motivo error, nunca lanza.
    original = database.SessionLocal

    def _sesion_rota():
        raise RuntimeError("bd caida")

    database.SessionLocal = _sesion_rota
    try:
        lanzado = False
        try:
            resultado = guardar_identidad_change(
                {"nombre": "Eva", "email": "eva.rota@gmail.com"}
            )
        except Exception:
            lanzado = True
            resultado = {}
        check(
            "guardar: fallo real -> motivo error y nunca lanza",
            lanzado is False
            and resultado.get("guardada") is False
            and resultado.get("motivo") == "error"
            and "bd caida" in resultado.get("error", ""),
            str(resultado),
        )
    finally:
        database.SessionLocal = original


# --------------------------------------------------------------------------- #
# (9) ejecutar_un_reporte
# --------------------------------------------------------------------------- #
def test_ejecutar_un_reporte(check):
    print("(9) ejecutar_un_reporte (bot e IA fake)")
    eventos = {"creados": [], "cerrados": 0, "queja": None}

    class _BotFake:
        retorno = {}

        def __init__(self, **kwargs):
            eventos["creados"].append(kwargs)

        def reportar(self, identidad=None, queja=""):
            eventos["queja"] = queja
            return dict(self.retorno)

        def cerrar(self):
            eventos["cerrados"] += 1

    capturado_ia = {}

    def _queja_fake(contexto, evitar=None, variante=None):
        capturado_ia["contexto"] = contexto
        capturado_ia["evitar"] = evitar
        return {"ok": True, "queja": "QUEJA-FAKE", "usada_ia": False, "error": ""}

    guardados = []

    def _guardar_fake(identidad, **kwargs):
        guardados.append((dict(identidad), kwargs))
        return {"guardada": True, "id": 7, "motivo": ""}

    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake), mock.patch.object(
        gc, "generar_queja_change", _queja_fake
    ), mock.patch.object(change_org, "guardar_identidad_change", _guardar_fake):
        _BotFake.retorno = {
            "ok": True,
            "email": "",
            "nombre": "",
            "apellido": "",
            "queja": "QUEJA-FAKE",
            "error": "",
            "evidencia": "ok",
            "url": "https://www.change.org/p/x",
        }
        resultado = ejecutar_un_reporte(
            "https://www.change.org/p/x",
            contexto="ctx",
            proxy="p1",
            evitar=["vieja"],
        )
        check("un_reporte: ok True", resultado["ok"] is True)
        check(
            "un_reporte: la queja generada se pasa al bot",
            eventos["queja"] == "QUEJA-FAKE",
            str(eventos["queja"]),
        )
        check(
            "un_reporte: contexto y evitar llegan a la IA",
            capturado_ia["contexto"] == "ctx"
            and capturado_ia["evitar"] == ["vieja"],
        )
        check(
            "un_reporte: proxy/url pasados al bot",
            eventos["creados"][-1]["proxy"] == "p1"
            and eventos["creados"][-1]["url_peticion"] == "https://www.change.org/p/x",
        )
        check(
            "un_reporte: identidad generada completa",
            set(resultado["identidad"])
            == {"nombre", "apellido", "email", "codigo_postal"},
        )
        check(
            "un_reporte: identidad_guardada True y guardado devuelto",
            resultado["identidad_guardada"] is True
            and resultado["guardado"] == {"guardada": True, "id": 7, "motivo": ""},
        )
        check(
            "un_reporte: guardar recibe la queja enviada",
            bool(guardados) and guardados[-1][1]["queja"] == "QUEJA-FAKE",
        )
        check("un_reporte: bot.cerrar llamado (finally)", eventos["cerrados"] == 1)
        check(
            "un_reporte: cancelado None si no se cancelo",
            resultado["cancelado"] is None,
        )
        check(
            "un_reporte: conserva claves de reportar()",
            all(k in resultado for k in ("ok", "email", "queja", "evidencia", "url")),
        )

        # ok False -> NO se guarda identidad.
        guardados.clear()
        _BotFake.retorno = {
            "ok": False,
            "email": "",
            "nombre": "",
            "apellido": "",
            "queja": "QUEJA-FAKE",
            "error": "sin exito",
            "evidencia": "sin evidencia",
            "url": "u",
        }
        resultado = ejecutar_un_reporte("u", contexto="")
        check(
            "un_reporte: ok False -> identidad_guardada False y sin guardar",
            resultado["identidad_guardada"] is False
            and resultado["guardado"] is None
            and guardados == [],
        )
        check("un_reporte: ok False -> bot.cerrar igual", eventos["cerrados"] == 2)

        # guardar_identidad=False -> aunque ok, no persiste.
        _BotFake.retorno = {
            "ok": True,
            "email": "",
            "nombre": "",
            "apellido": "",
            "queja": "QUEJA-FAKE",
            "error": "",
            "evidencia": "ok",
            "url": "u",
        }
        resultado = ejecutar_un_reporte("u", guardar_identidad=False)
        check(
            "un_reporte: guardar_identidad=False no persiste",
            resultado["ok"] is True
            and resultado["identidad_guardada"] is False
            and guardados == [],
        )

        # El bot revienta -> resultado fallido y cerrar SIEMPRE.
        class _BotExplota(_BotFake):
            def reportar(self, identidad=None, queja=""):
                raise RuntimeError("bot roto")

        cerrados_antes = eventos["cerrados"]
        with mock.patch.object(change_org, "ChangeOrgReportBot", _BotExplota):
            resultado = ejecutar_un_reporte("u")
        check(
            "un_reporte: excepcion del bot -> ok False y error",
            resultado["ok"] is False and "bot roto" in resultado["error"],
            str(resultado.get("error")),
        )
        check(
            "un_reporte: bot.cerrar se llama aunque reportar lance",
            eventos["cerrados"] == cerrados_antes + 1,
        )

    # Cancel pre-seteado -> dict exacto sin abrir navegador.
    creados_antes = len(eventos["creados"])
    evento = threading.Event()
    evento.set()
    resultado = ejecutar_un_reporte("u", cancelar=evento)
    check(
        "un_reporte: cancel pre-seteado -> dict exacto",
        resultado == {"ok": False, "cancelado": True, "error": MENSAJE_CANCELADO},
        f"ok={resultado.get('ok')} cancelado={resultado.get('cancelado')}",
    )
    check(
        "un_reporte: cancel pre-seteado NO crea bot",
        len(eventos["creados"]) == creados_antes,
    )

    # Si la IA falla, igual hay queja local no vacia.
    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake), mock.patch.object(
        gc, "generar_queja_change", side_effect=RuntimeError("ia caida")
    ), mock.patch.object(change_org, "guardar_identidad_change", _guardar_fake):
        _BotFake.retorno = {"ok": False}
        resultado = ejecutar_un_reporte("u", contexto="tema local")
        check(
            "un_reporte: IA caida -> queja de respaldo no vacia",
            bool(str(resultado["queja"]).strip())
            and "tema local" in resultado["queja"],
            str(resultado["queja"]),
        )


# --------------------------------------------------------------------------- #
# (10) ejecutar_campana_reportes
# --------------------------------------------------------------------------- #
def _fabricar_fake_reporte():
    """Fake de `ejecutar_un_reporte`: 1 de cada 4 falla; guarda 1 identidad."""
    llamadas = []

    def _fake(**kwargs):
        indice = len(llamadas)
        llamadas.append(kwargs)
        return {
            "ok": indice != 3,
            "email": f"e{indice}@gmail.com",
            "queja": f"queja {indice}",
            "identidad": {"email": f"e{indice}@gmail.com"},
            "identidad_guardada": indice == 0,
            "error": "" if indice != 3 else "fallo fake",
        }

    return _fake, llamadas


def test_campana(check):
    print("(10) ejecutar_campana_reportes (reporte fake)")
    fake_reporte, llamadas = _fabricar_fake_reporte()
    eventos = []
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte), mock.patch.object(
        change_org, "proxies_disponibles", lambda pais="": ["p1", "p2"]
    ):
        resumen = ejecutar_campana_reportes(
            "https://www.change.org/p/x",
            contexto="ctx",
            cantidad=4,
            max_workers=2,
            pais_proxy="mexico",
            callback=eventos.append,
        )
    check("campana: total 4", resumen["total"] == 4, str(resumen["total"]))
    check(
        "campana: enviados+fallidos == 4",
        resumen["enviados"] + resumen["fallidos"] == 4,
        str(resumen),
    )
    check(
        "campana: 3 exitos y 1 fallo",
        resumen["enviados"] == 3 and resumen["fallidos"] == 1,
    )
    check("campana: identidades_guardadas 1", resumen["identidades_guardadas"] == 1)
    check("campana: 4 resultados", len(resumen["resultados"]) == 4)
    check("campana: no cancelada", resumen["cancelada"] is False)
    check("campana: sin error", resumen["error"] == "", repr(resumen["error"]))
    check("campana: 4 reportes ejecutados", len(llamadas) == 4)
    check(
        "campana: round-robin de proxies (2+2)",
        Counter(l["proxy"] for l in llamadas) == Counter({"p1": 2, "p2": 2}),
        str([l["proxy"] for l in llamadas]),
    )
    check("campana: proxies_total 2", resumen["proxies_total"] == 2)
    check("campana: sin_proxy 0", resumen["sin_proxy"] == 0)
    check(
        "campana: evitar se va llenando entre reportes",
        llamadas[0]["evitar"] == [] and any(l["evitar"] for l in llamadas),
    )
    check(
        "campana: callback inicio con total",
        bool(eventos) and eventos[0] == {"tipo": "inicio", "total": 4},
        str(eventos[:1]),
    )
    reportes = [e for e in eventos if e["tipo"] == "reporte"]
    check("campana: 4 callbacks de reporte", len(reportes) == 4)
    check(
        "campana: hechas 1..4 en orden",
        [e["hechas"] for e in reportes] == [1, 2, 3, 4],
        str([e["hechas"] for e in reportes]),
    )
    esperado = {f"e{i}@gmail.com": i != 3 for i in range(4)}
    check(
        "campana: callback ok/email correctos por reporte",
        all(e["ok"] == esperado[e["email"]] for e in reportes),
    )
    fallidos_cb = [e for e in reportes if not e["ok"]]
    exitosos_cb = [e for e in reportes if e["ok"]]
    check(
        "campana: detalle del fallido = error",
        len(fallidos_cb) == 1 and "fallo fake" in fallidos_cb[0]["detalle"],
    )
    check("campana: detalle del exito = 'éxito'", exitosos_cb[0]["detalle"] == "éxito")
    check(
        "campana: identidad viaja en el callback",
        all("email" in e["identidad"] for e in reportes),
    )

    # Sin proxies -> sin_proxy cuenta cada reporte y el proxy es "".
    fake_reporte, llamadas = _fabricar_fake_reporte()
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte), mock.patch.object(
        change_org, "proxies_disponibles", lambda pais="": []
    ):
        resumen = ejecutar_campana_reportes("u", cantidad=3, usar_proxies=True)
    check(
        "campana: sin proxies -> sin_proxy 3 y proxy vacio",
        resumen["sin_proxy"] == 3
        and resumen["proxies_total"] == 0
        and all(l["proxy"] == "" for l in llamadas),
    )

    # usar_proxies=False no consulta proxies y tambien cuenta sin_proxy.
    consultas = []
    fake_reporte, llamadas = _fabricar_fake_reporte()
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte), mock.patch.object(
        change_org, "proxies_disponibles", lambda pais="": consultas.append(pais) or ["p1"]
    ):
        resumen = ejecutar_campana_reportes("u", cantidad=2, usar_proxies=False)
    check(
        "campana: usar_proxies=False no consulta y sin_proxy 2",
        consultas == [] and resumen["sin_proxy"] == 2,
    )

    # Excepcion del reporte -> se cuenta como fallido y nunca lanza.
    def _explota(**kwargs):
        raise RuntimeError("revienta fuerte")

    with mock.patch.object(change_org, "ejecutar_un_reporte", _explota):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=2, max_workers=1, usar_proxies=False
        )
    check(
        "campana: excepcion del reporte -> 2 fallidos",
        resumen["fallidos"] == 2
        and resumen["enviados"] == 0
        and len(resumen["resultados"]) == 2,
    )
    check("campana: nunca lanza con reportes que explotan", resumen["error"] == "")
    check(
        "campana: detalle de la excepcion en resultados",
        "revienta fuerte" in resumen["resultados"][0]["error"],
    )

    # Callback roto no tumba la campana.
    fake_reporte, _ = _fabricar_fake_reporte()

    def _callback_roto(info):
        raise RuntimeError("callback roto")

    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=2, usar_proxies=False, callback=_callback_roto
        )
    check(
        "campana: callback roto no rompe",
        resumen["enviados"] + resumen["fallidos"] == 2,
    )

    # Cancelar pre-seteado -> cancelada=True, sin reportes.
    fake_reporte, llamadas = _fabricar_fake_reporte()
    evento = threading.Event()
    evento.set()
    eventos_cancel = []
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=5, cancelar=evento, callback=eventos_cancel.append
        )
    check(
        "campana: cancel pre-seteado -> cancelada True sin reportes",
        resumen["cancelada"] is True
        and resumen["resultados"] == []
        and llamadas == [],
    )
    check(
        "campana: callback solo inicio si ya estaba cancelada",
        eventos_cancel == [{"tipo": "inicio", "total": 5}],
        str(eventos_cancel),
    )

    # Clamps de cantidad/max_workers.
    fake_reporte, llamadas = _fabricar_fake_reporte()
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=0, max_workers=99, usar_proxies=False
        )
    check(
        "campana: cantidad 0 -> total 1 (acotada)",
        resumen["total"] == 1 and len(llamadas) == 1,
    )
    fake_reporte, llamadas = _fabricar_fake_reporte()
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=999, max_workers=99, usar_proxies=False
        )
    check(
        "campana: cantidad 999 -> total 200 (acotada)",
        resumen["total"] == 200 and len(llamadas) == 200,
    )

    # Si proxies_disponibles revienta, la campana sigue sin proxy.
    fake_reporte, llamadas = _fabricar_fake_reporte()
    with mock.patch.object(change_org, "ejecutar_un_reporte", fake_reporte), mock.patch.object(
        change_org, "proxies_disponibles", side_effect=RuntimeError("proxies caidos")
    ):
        resumen = ejecutar_campana_reportes("u", cantidad=2, usar_proxies=True)
    check(
        "campana: proxies_disponibles que lanza -> sin proxy y sigue",
        resumen["proxies_total"] == 0
        and resumen["sin_proxy"] == 2
        and resumen["enviados"] + resumen["fallidos"] == 2,
    )


# --------------------------------------------------------------------------- #
# (11) password_change_para
# --------------------------------------------------------------------------- #
def test_password_change(check):
    print("(11) password_change_para")
    check("password: vacia -> ''", password_change_para("") == "")
    check("password: None -> ''", password_change_para(None) == "")
    check(
        "password: >=10 se usa tal cual",
        password_change_para("claveSegura123") == "claveSegura123",
    )
    check(
        "password: 10 exactos se usan tal cual",
        password_change_para("0123456789") == "0123456789",
    )
    corta = password_change_para("corta")
    check(
        "password: <10 se deriva con longitud >=10",
        corta != "corta" and len(corta) >= 10 and corta.startswith("corta"),
        repr(corta),
    )
    check(
        "password: 1 char tambien deriva >=10",
        len(password_change_para("a")) >= 10,
    )
    check(
        "password: determinista (dos llamadas iguales)",
        password_change_para("abc") == password_change_para("abc"),
    )
    check(
        "password: contrato en __all__",
        {"password_change_para", "registrar_cuenta_change",
         "ejecutar_campana_registros"} <= set(change_org.__all__),
    )


# --------------------------------------------------------------------------- #
# (12) registrar_o_entrar (flujo real de registro/login)
# --------------------------------------------------------------------------- #
def _driver_login(modo="nueva", error="", captcha=False, reto=False):
    """FakeDriver que simula login_or_join paso a paso (pantallas por click)."""
    driver = FakeDriver()
    btn_login = FakeElement(driver, tag="button", texto="Iniciar sesión")
    email = FakeElement(driver, tag="input", attrs={"type": "email"}, visible=False)
    btn_email = FakeElement(driver, tag="button", texto="Continuar", visible=False)
    clave = FakeElement(driver, tag="input", attrs={"type": "password"}, visible=False)
    btn_clave = FakeElement(driver, tag="button", texto="Continuar", visible=False)
    nombre = FakeElement(
        driver, tag="input", attrs={"autocomplete": "given-name"}, visible=False
    )
    apellido = FakeElement(
        driver, tag="input", attrs={"autocomplete": "family-name"}, visible=False
    )
    btn_nombre = FakeElement(driver, tag="button", texto="Continuar", visible=False)
    menu = FakeElement(
        driver, tag="a", href="https://www.change.org/profile/ana", visible=False
    )
    reto_frame = FakeElement(
        driver,
        tag="iframe",
        attrs={"src": "https://challenges.cloudflare.com/turnstile/v0/api.js"},
        visible=False,
    )
    driver.registrar(
        btn_login, email, btn_email, clave, btn_clave, nombre, apellido,
        btn_nombre, menu, reto_frame,
    )

    def _abrir_email():
        btn_login.visible = False
        email.visible = True
        btn_email.visible = True

    btn_login.al_click = _abrir_email

    def _mostrar_clave():
        email.visible = False
        btn_email.visible = False
        if reto:
            # Cloudflare intercepta: no hay pantalla de contraseña.
            reto_frame.visible = True
            driver.page_source = "<h1>Verificacion de seguridad</h1>"
            return
        clave.visible = True
        btn_clave.visible = True
        if modo == "nueva":
            driver.page_source = (
                "<h1>Crea tu contraseña</h1>"
                "<p>Debe tener al menos 10 caracteres</p>"
            )
        else:
            driver.page_source = "<h1>Ingresa tu contraseña</h1>"

    btn_email.al_click = _mostrar_clave

    def _tras_clave():
        clave.visible = False
        btn_clave.visible = False
        if error == "password":
            driver.page_source = "<p>Contraseña incorrecta, inténtalo de nuevo</p>"
            clave.visible = True
            btn_clave.visible = True
            return
        if captcha:
            driver.page_source = "Verifica que no soy un robot"
            return
        if modo == "nueva":
            nombre.visible = True
            apellido.visible = True
            btn_nombre.visible = True
            driver.page_source = "<h1>Escribe tu nombre</h1>"
        else:
            menu.visible = True
            driver.page_source = "<p>Mi cuenta</p>"

    btn_clave.al_click = _tras_clave

    def _tras_nombre():
        nombre.visible = False
        apellido.visible = False
        btn_nombre.visible = False
        menu.visible = True
        driver.page_source = "<p>Tu cuenta fue creada</p>"

    btn_nombre.al_click = _tras_nombre
    piezas = {
        "btn_login": btn_login,
        "email": email,
        "btn_email": btn_email,
        "clave": clave,
        "btn_clave": btn_clave,
        "nombre": nombre,
        "apellido": apellido,
        "btn_nombre": btn_nombre,
        "menu": menu,
    }
    return driver, piezas


def test_registro_flujo(check):
    print("(12) registrar_o_entrar (nueva / existente / fallo)")
    with _sin_esperas():
        # --- cuenta NUEVA: ve "Crea tu contraseña" y la pantalla de nombre ---
        cuenta = {
            "usuario": "u1",
            "email": "nueva@x.com",
            "password": "claveSegura123",
            "nombre": "Ana",
            "apellido": "Lopez",
        }
        driver, piezas = _driver_login("nueva")
        registro = _bot(driver, cuenta=cuenta).registrar_o_entrar()
        check(
            "registro nueva: ok True y estado 'nueva'",
            registro["ok"] is True and registro["estado"] == "nueva",
            str(registro),
        )
        check(
            "registro nueva: correo y contraseña tipeados",
            "".join(piezas["email"].typed) == "nueva@x.com"
            and "".join(piezas["clave"].typed) == "claveSegura123",
        )
        check(
            "registro nueva: nombre y apellido tipeados",
            "".join(piezas["nombre"].typed) == "Ana"
            and "".join(piezas["apellido"].typed) == "Lopez",
        )
        check(
            "registro nueva: 'Iniciar sesión' y 3 'Continuar' clicados",
            piezas["btn_login"].click_count == 1
            and piezas["btn_email"].click_count == 1
            and piezas["btn_clave"].click_count == 1
            and piezas["btn_nombre"].click_count == 1,
        )
        check(
            "registro nueva: evidencia positiva de sesion",
            bool(registro["evidencia"]),
            registro["evidencia"],
        )

        # --- cuenta EXISTENTE: pantalla de contraseña de login, sin nombre ---
        cuenta_existente = {
            "usuario": "u2",
            "email": "existe@x.com",
            "email_password": "claveSegura456",
        }
        driver, piezas = _driver_login("existente")
        registro = _bot(driver, cuenta=cuenta_existente).registrar_o_entrar()
        check(
            "registro existente: ok True y estado 'existente'",
            registro["ok"] is True and registro["estado"] == "existente",
            str(registro),
        )
        check(
            "registro existente: contraseña tipeada y sin pantalla de nombre",
            "".join(piezas["clave"].typed) == "claveSegura456"
            and piezas["nombre"].typed == []
            and piezas["btn_nombre"].click_count == 0,
        )
        check(
            "registro existente: acepta email_password sin 'password'",
            "".join(piezas["email"].typed) == "existe@x.com",
        )

        # --- contraseña incorrecta de una cuenta existente ---
        driver, piezas = _driver_login("existente", error="password")
        registro = _bot(driver, cuenta=cuenta_existente).registrar_o_entrar()
        check(
            "registro fallo: contraseña incorrecta -> ok False/estado fallo",
            registro["ok"] is False
            and registro["estado"] == "fallo"
            and "contraseña incorrecta" in registro["error"],
            str(registro),
        )

        # --- captcha ---
        driver, piezas = _driver_login("nueva", captcha=True)
        registro = _bot(driver, cuenta=cuenta).registrar_o_entrar()
        check(
            "registro fallo: captcha -> ok False con detalle",
            registro["ok"] is False
            and registro["estado"] == "fallo"
            and "captcha" in registro["error"].lower(),
            str(registro),
        )

        # --- reto anti-bot de Cloudflare (iframe protegido) ---
        driver, piezas = _driver_login("nueva", reto=True)
        bot = _bot(driver, cuenta=cuenta)
        registro = bot.registrar_o_entrar()
        check(
            "registro fallo: reto anti-bot -> error claro y accionable",
            registro["ok"] is False
            and registro["estado"] == "fallo"
            and "anti-bot" in registro["error"]
            and "Cloudflare" in registro["error"],
            str(registro["error"]),
        )
        check(
            "registro: _detectar_reto_humano detecta el iframe de Cloudflare",
            bool(bot._detectar_reto_humano()),
            bot._detectar_reto_humano(),
        )
        driver_limpio, _ = _driver_login("existente")
        check(
            "registro: sin reto, _detectar_reto_humano devuelve ''",
            _bot(driver_limpio)._detectar_reto_humano() == "",
        )

        # --- sin email/contraseña NO intenta navegar ---
        bot = ChangeOrgReportBot(cuenta={"usuario": "u3", "email": "", "password": "x"})
        registro = bot.registrar_o_entrar()
        check(
            "registro fallo: cuenta sin email -> error claro",
            registro["ok"] is False
            and registro["estado"] == "fallo"
            and "email" in registro["error"],
            str(registro),
        )
        bot = ChangeOrgReportBot(
            cuenta={"usuario": "u4", "email": "u4@x.com", "password": ""}
        )
        check(
            "registro fallo: cuenta sin contraseña -> error claro",
            bot.registrar_o_entrar()["ok"] is False,
        )


# --------------------------------------------------------------------------- #
# (13) registrar_cuenta_change
# --------------------------------------------------------------------------- #
def test_registrar_cuenta_change(check):
    print("(13) registrar_cuenta_change (bot fake)")
    eventos = []

    class _BotFake:
        def __init__(self, **kwargs):
            eventos.append(("creado", kwargs))
            self.ultimo_error = ""
            self._url = "https://www.change.org/"

        def preparar_driver(self):
            eventos.append(("preparar",))
            return True

        def registrar_o_entrar(self):
            eventos.append(("registrar",))
            return {"ok": True, "estado": "nueva", "error": "", "evidencia": "ok"}

        def _url_actual(self):
            return self._url

        def cerrar(self):
            eventos.append(("cerrar",))

    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake):
        resultado = registrar_cuenta_change(
            usuario="u1",
            email="u1@x.com",
            password="corta",
            proxy="p1",
            headless=True,
        )
    check(
        "registrar: claves exactas del resultado",
        set(resultado)
        == {
            "ok", "usuario", "email", "estado", "nombre", "apellido",
            "error", "evidencia", "url", "cancelado",
        },
        str(sorted(resultado)),
    )
    check(
        "registrar: ok/estado/url/evidencia",
        resultado["ok"] is True
        and resultado["estado"] == "nueva"
        and resultado["url"].startswith("https://")
        and resultado["evidencia"] == "ok",
    )
    capturado = eventos[0][1]
    check(
        "registrar: cuenta con email y password DERIVADA al bot",
        capturado["cuenta"]["email"] == "u1@x.com"
        and capturado["cuenta"]["password"] == password_change_para("corta")
        and len(capturado["cuenta"]["password"]) >= 10,
        str(capturado["cuenta"]["password"]),
    )
    check(
        "registrar: proxy/headless pasados al bot",
        capturado["proxy"] == "p1" and capturado["headless"] is True,
    )
    check(
        "registrar: nombres no vacios (Faker si no vienen)",
        bool(capturado["cuenta"]["nombre"].strip())
        and bool(capturado["cuenta"]["apellido"].strip()),
    )
    check(
        "registrar: preparar -> registrar -> cerrar (en orden)",
        [e[0] for e in eventos] == ["creado", "preparar", "registrar", "cerrar"],
        str([e[0] for e in eventos]),
    )

    # Cancelacion pre-seteada: no crea bot ni abre Chrome.
    creados_antes = len([e for e in eventos if e[0] == "creado"])
    evento = threading.Event()
    evento.set()
    resultado = registrar_cuenta_change(
        usuario="u2", email="u2@x.com", password="claveSegura123", cancelar=evento
    )
    check(
        "registrar: cancel pre-seteado -> cancelado sin abrir navegador",
        resultado["cancelado"] is True
        and resultado["error"] == MENSAJE_CANCELADO
        and len([e for e in eventos if e[0] == "creado"]) == creados_antes,
    )

    # Sin email/contraseña -> error, nunca lanza.
    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake):
        resultado = registrar_cuenta_change(usuario="u3", email="", password="x")
    check(
        "registrar: sin email -> error y sin bot",
        resultado["ok"] is False and "email" in (resultado["error"] or ""),
        str(resultado["error"]),
    )

    # Excepcion del bot -> fallo controlado y cierre SIEMPRE.
    class _BotExplota(_BotFake):
        def preparar_driver(self):
            raise RuntimeError("boom")

    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotExplota):
        lanzado = False
        try:
            resultado = registrar_cuenta_change(
                usuario="u4", email="u4@x.com", password="claveSegura123"
            )
        except Exception:
            lanzado = True
            resultado = {}
    check(
        "registrar: excepcion -> nunca lanza y error descriptivo",
        lanzado is False and resultado.get("ok") is False
        and "boom" in resultado.get("error", ""),
        str(resultado.get("error")),
    )


# --------------------------------------------------------------------------- #
# (14) ejecutar_campana_registros
# --------------------------------------------------------------------------- #
def test_campana_registros(check):
    print("(14) ejecutar_campana_registros")
    llamadas = []

    def _registro_fake(**kwargs):
        llamadas.append(kwargs)
        email = str(kwargs.get("email") or "")
        if email.startswith("fallo"):
            return {
                "ok": False,
                "usuario": kwargs.get("usuario", ""),
                "email": email,
                "estado": "fallo",
                "error": "clave mala",
            }
        return {
            "ok": True,
            "usuario": kwargs.get("usuario", ""),
            "email": email,
            "estado": "nueva",
            "error": "",
            "evidencia": "ok",
        }

    cuentas = [
        {
            "usuario": "u1",
            "email": "u1@x.com",
            "email_password": "claveSegura123",
            "nombre_mostrado": "Ana Maria Lopez",
        },
        {"usuario": "u2", "email": "u2@x.com", "email_password": "corta"},
        {"usuario": "u3", "email": "fallo3@x.com", "email_password": "claveSegura123"},
        {"usuario": "u4", "email": "", "email_password": "claveSegura123"},
        {"usuario": "u5", "email": "u5@x.com", "email_password": ""},
    ]
    eventos = []
    with mock.patch.object(
        change_org, "registrar_cuenta_change", _registro_fake
    ), mock.patch.object(change_org, "proxies_disponibles", lambda pais="": ["p1", "p2"]):
        resumen = ejecutar_campana_registros(
            cuentas, max_workers=2, callback=eventos.append
        )
    check(
        "campana registros: claves exactas del resumen",
        set(resumen)
        == {
            "total", "exitosos", "fallidos", "nuevas", "existentes",
            "omitidas", "cancelada", "resultados", "proxies_total",
            "sin_proxy", "error",
        },
        str(sorted(resumen)),
    )
    check("campana registros: total 5", resumen["total"] == 5)
    check(
        "campana registros: 2 exitosos + 1 fallido + 2 omitidas",
        resumen["exitosos"] == 2
        and resumen["fallidos"] == 1
        and resumen["omitidas"] == 2,
        str({k: resumen[k] for k in ("exitosos", "fallidos", "omitidas")}),
    )
    check(
        "campana registros: nuevas 2 / existentes 0",
        resumen["nuevas"] == 2 and resumen["existentes"] == 0,
    )
    check(
        "campana registros: resultados con claves minimas",
        len(resumen["resultados"]) == 5
        and all(
            {"ok", "usuario", "email", "estado", "detalle"} <= set(r)
            for r in resumen["resultados"]
        ),
    )
    check(
        "campana registros: proxies_total 2 y sin_proxy 0",
        resumen["proxies_total"] == 2 and resumen["sin_proxy"] == 0,
    )
    check(
        "campana registros: no cancelada y sin error",
        resumen["cancelada"] is False and resumen["error"] == "",
    )
    check(
        "campana registros: callback inicio",
        eventos[0] == {"tipo": "inicio", "total": 5},
        str(eventos[0]),
    )
    registro_ev = [e for e in eventos if e["tipo"] == "registro"]
    check(
        "campana registros: hechas 1..5",
        [e["hechas"] for e in registro_ev] == [1, 2, 3, 4, 5],
        str([e["hechas"] for e in registro_ev]),
    )
    check(
        "campana registros: eventos con las claves del contrato",
        all(
            {"tipo", "hechas", "total", "ok", "usuario", "email", "estado",
             "detalle"} <= set(e)
            for e in registro_ev
        ),
    )
    omitidas_ev = [e for e in registro_ev if e["estado"] == "omitida"]
    check(
        "campana registros: 2 eventos omitida (sin navegador)",
        len(omitidas_ev) == 2
        and all(e["ok"] is False and "contraseña" in e["detalle"] or "email" in e["detalle"] for e in omitidas_ev),
        str([e["detalle"] for e in omitidas_ev]),
    )
    check(
        "campana registros: solo 3 registros reales (2 omitidas no abren Chrome)",
        len(llamadas) == 3,
        str(len(llamadas)),
    )
    pass_por_usuario = {l["usuario"]: l["password"] for l in llamadas}
    check(
        "campana registros: password >=10 tal cual",
        pass_por_usuario["u1"] == "claveSegura123",
    )
    check(
        "campana registros: password corta derivada antes del bot",
        pass_por_usuario["u2"] == password_change_para("corta")
        and len(pass_por_usuario["u2"]) >= 10,
    )
    nombres = {l["usuario"]: (l["nombre"], l["apellido"]) for l in llamadas}
    check(
        "campana registros: nombre_mostrado partido (Nombres/Apellidos)",
        nombres["u1"] == ("Ana", "Maria Lopez"),
        str(nombres["u1"]),
    )
    check(
        "campana registros: nombres Faker no vacios sin nombre_mostrado",
        bool(nombres["u2"][0]) and bool(nombres["u2"][1])
        and bool(nombres["u3"][0]) and bool(nombres["u3"][1]),
        str((nombres["u2"], nombres["u3"])),
    )
    check(
        "campana registros: proxy round-robin (2+1)",
        Counter(l["proxy"] for l in llamadas) == Counter({"p1": 2, "p2": 1}),
        str([l["proxy"] for l in llamadas]),
    )

    # Cancelacion pre-seteada: sin registros ni eventos de cuenta.
    evento = threading.Event()
    evento.set()
    eventos_cancel = []
    cuentas_validas = [c for c in cuentas if c.get("email") and c.get("email_password")]
    with mock.patch.object(change_org, "registrar_cuenta_change", _registro_fake):
        resumen = ejecutar_campana_registros(
            cuentas_validas, cancelar=evento, callback=eventos_cancel.append
        )
    check(
        "campana registros: cancel pre-seteado -> cancelada sin resultados",
        resumen["cancelada"] is True and resumen["resultados"] == [],
        str(resumen["cancelada"]),
    )
    check(
        "campana registros: callback solo inicio si estaba cancelada",
        eventos_cancel == [{"tipo": "inicio", "total": len(cuentas_validas)}],
        str(eventos_cancel),
    )

    # Cap de 500 cuentas (todas sin contraseña -> omitidas, barato).
    muchas = [
        {"usuario": f"u{i}", "email": f"u{i}@x.com", "email_password": ""}
        for i in range(501)
    ]
    with mock.patch.object(change_org, "registrar_cuenta_change", _registro_fake):
        resumen = ejecutar_campana_registros(muchas, max_workers=99)
    check(
        "campana registros: lista cap 500",
        resumen["total"] == 500
        and resumen["omitidas"] == 500
        and len(resumen["resultados"]) == 500,
        str(resumen["total"]),
    )

    # Excepcion del registro -> fallido, nunca lanza.
    def _explota(**kwargs):
        raise RuntimeError("revienta")

    with mock.patch.object(
        change_org, "registrar_cuenta_change", _explota
    ), mock.patch.object(change_org, "proxies_disponibles", lambda pais="": []):
        resumen = ejecutar_campana_registros(
            [{"usuario": "x", "email": "x@x.com", "email_password": "claveSegura123"}]
        )
    check(
        "campana registros: excepcion -> 1 fallido y sin error global",
        resumen["fallidos"] == 1 and resumen["error"] == "",
        str(resumen["error"]),
    )


# --------------------------------------------------------------------------- #
# (15) modal real del reporte (radio 3 + México + textarea + Enviar)
# --------------------------------------------------------------------------- #
def _driver_modal(mexico_ya=False, gracias=True, radios_sin_texto=False):
    """FakeDriver del modal "Denunciar abuso" con radios/select/textarea."""
    driver = FakeDriver()
    enlace = FakeElement(
        driver, tag="button", texto="Denunciar una violación de las políticas"
    )
    if radios_sin_texto:
        radio1 = FakeElement(driver, tag="input", attrs={"type": "radio"})
        radio2 = FakeElement(driver, tag="input", attrs={"type": "radio"})
        radio3 = FakeElement(driver, tag="input", attrs={"type": "radio"})
    else:
        radio1 = FakeElement(
            driver, tag="input", attrs={"type": "radio", "value": "spam"},
            texto="Es spam o publicidad engañosa",
        )
        radio2 = FakeElement(
            driver, tag="input", attrs={"type": "radio", "value": "falsa"},
            texto="Contiene información falsa",
        )
        radio3 = FakeElement(
            driver, tag="input",
            attrs={"type": "radio", "value": "no-me-gusta"},
            texto="No me gusta esta petición o no estoy de acuerdo con ella",
        )
    label_ubi = FakeElement(driver, tag="label", texto="¿Dónde vives?")
    op_us = FakeElement(
        driver, tag="option", texto="Estados Unidos", attrs={"value": "US"}
    )
    attrs_mx = {"value": "MX"}
    if mexico_ya:
        attrs_mx["selected"] = "selected"
    op_mx = FakeElement(driver, tag="option", texto="México", attrs=attrs_mx)
    select = FakeElement(
        driver,
        tag="select",
        attrs={"value": "MX" if mexico_ya else "US"},
        hijos=[op_us, op_mx],
    )
    label_ubi.siguiente = select
    motivo = FakeElement(driver, tag="textarea", attrs={"name": "reason"}, visible=False)
    enviar = FakeElement(
        driver, tag="button", attrs={"type": "submit"}, texto="Enviar"
    )
    driver.registrar(
        enlace, radio1, radio2, radio3, label_ubi, select, op_us, op_mx,
        motivo, enviar,
    )

    def _mostrar_textarea():
        motivo.visible = True

    radio3.al_click = _mostrar_textarea

    def _confirmar():
        if gracias:
            driver.page_source = (
                "<p>Gracias por tomarte el tiempo de denunciar contenido. "
                "Revisaremos tu denuncia.</p>"
            )
        else:
            driver.page_source = "<p>La denuncia sigue en el formulario</p>"

    enviar.al_click = _confirmar
    return driver, {
        "enlace": enlace,
        "radio1": radio1,
        "radio2": radio2,
        "radio3": radio3,
        "select": select,
        "op_mx": op_mx,
        "motivo": motivo,
        "enviar": enviar,
    }


def test_reporte_modal(check):
    print("(15) modal real: radio 3, Mexico, textarea y Enviar")
    identidad = {
        "nombre": "Ana",
        "apellido": "Lopez",
        "email": "ana.lopez12@gmail.com",
        "codigo_postal": "44100",
    }
    queja = "Considero que esta peticion incumple las normas de la comunidad."
    check(
        "modal: frases de exito exactas en _FRASES_EXITO",
        "gracias por tomarte el tiempo" in change_org._FRASES_EXITO
        and "denunciar contenido" in change_org._FRASES_EXITO,
    )
    with _sin_esperas():
        # --- flujo completo OK ---
        driver, piezas = _driver_modal()
        bot = _bot(driver, url_peticion="https://www.change.org/p/x")
        bot.preparar_driver = lambda: True
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "modal: reporte ok con evidencia 'gracias por tomarte el tiempo'",
            resultado["ok"] is True
            and "gracias por tomarte el tiempo" in resultado["evidencia"],
            str(resultado["evidencia"]),
        )
        check(
            "modal: SOLO el tercer radio seleccionado",
            piezas["radio3"].click_count == 1
            and piezas["radio1"].click_count == 0
            and piezas["radio2"].click_count == 0,
            f"r1={piezas['radio1'].click_count} r3={piezas['radio3'].click_count}",
        )
        check(
            "modal: Mexico seleccionado en '¿Dónde vives?'",
            piezas["op_mx"].click_count == 1,
        )
        check(
            "modal: queja escrita en el textarea",
            "".join(piezas["motivo"].typed) == queja,
            "".join(piezas["motivo"].typed)[:60],
        )
        check("modal: boton Enviar clicado", piezas["enviar"].click_count == 1)

        # --- ya viene Mexico: NO se toca el select ---
        driver, piezas = _driver_modal(mexico_ya=True)
        bot = _bot(driver)
        detalle = bot._seleccionar_pais_mexico()
        check(
            "modal: si ya viene Mexico no se toca el select",
            detalle == "ya-mexico" and piezas["op_mx"].click_count == 0,
            detalle,
        )
        check(
            "modal: radio 3 por indice si los textos no coinciden",
            _bot(_driver_modal(radios_sin_texto=True)[0])._seleccionar_motivo_denuncia()[0]
            is True,
        )
        driver, piezas = _driver_modal(radios_sin_texto=True)
        bot = _bot(driver)
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            ok_radio, error = bot._llenar_formulario(identidad, queja)
        check(
            "modal: sin textos -> tercer radio por posicion",
            ok_radio is True
            and piezas["radio1"].click_count == 0
            and piezas["radio2"].click_count == 0
            and piezas["radio3"].click_count == 1,
            str(error),
        )

        # --- sin frase de gracias: NUNCA exito ---
        driver, piezas = _driver_modal(gracias=False)
        bot = _bot(driver, url_peticion="https://www.change.org/p/x")
        bot.preparar_driver = lambda: True
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "modal: sin evidencia positiva -> ok False (no exito por clic)",
            resultado["ok"] is False
            and piezas["enviar"].click_count == 1
            and "sin evidencia" in resultado["error"],
            resultado["error"],
        )

        # --- _reporte_exitoso detecta la frase nueva directamente ---
        driver = FakeDriver()
        driver.page_source = (
            "<p>¡Gracias por tomarte el tiempo de denunciar contenido!</p>"
        )
        ok, evidencia = _bot(driver)._reporte_exitoso(formulario_presente=True)
        check(
            "modal: _reporte_exitoso acepta la frase nueva",
            ok is True and "gracias por tomarte el tiempo" in evidencia,
            evidencia,
        )


# --------------------------------------------------------------------------- #
# (16) reportar con cuenta + ejecutar_un_reporte en modo cuenta
# --------------------------------------------------------------------------- #
def test_reporte_con_cuenta(check):
    print("(16) reportar con cuenta (login primero, sin granja)")
    identidad = {
        "nombre": "Ana",
        "apellido": "Lopez",
        "email": "ana.lopez12@gmail.com",
        "codigo_postal": "",
    }
    queja = "Queja de prueba para el reporte."
    cuenta = {
        "usuario": "u1",
        "email": "u1@x.com",
        "password": "claveSegura123",
        "nombre": "Ana",
        "apellido": "Lopez",
    }
    with _sin_esperas():
        # (a) registro falla -> NO se navega a la peticion
        driver = FakeDriver()
        bot = _bot(driver, url_peticion="https://www.change.org/p/x", cuenta=cuenta)
        bot.preparar_driver = lambda: True
        bot.registrar_o_entrar = lambda: {
            "ok": False,
            "estado": "fallo",
            "error": "contraseña incorrecta en Change.org",
            "evidencia": "",
        }
        resultado = bot.reportar(identidad=identidad, queja=queja)
        check(
            "cuenta: registro falla -> ok False con el error del registro",
            resultado["ok"] is False
            and "contraseña incorrecta" in resultado["error"]
            and resultado.get("estado_cuenta") == "fallo",
            str(resultado.get("error")),
        )
        check(
            "cuenta: registro falla -> no se navega a la peticion",
            driver.url == "",
            driver.url,
        )

        # (b) registro OK -> sigue con el flujo del modal y etiqueta estado_cuenta
        driver, piezas = _driver_modal()
        bot = _bot(driver, url_peticion="https://www.change.org/p/x", cuenta=cuenta)
        bot.preparar_driver = lambda: True
        bot.registrar_o_entrar = lambda: {
            "ok": True,
            "estado": "existente",
            "error": "",
            "evidencia": "sesion iniciada",
        }
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            resultado = bot.reportar(identidad=None, queja=queja)
        check(
            "cuenta: reporte ok con estado_cuenta 'existente'",
            resultado["ok"] is True and resultado.get("estado_cuenta") == "existente",
            str(resultado.get("error")),
        )
        check(
            "cuenta: identidad reconstruida desde la cuenta si falta",
            resultado["email"] == "u1@x.com"
            and resultado["nombre"] == "Ana"
            and resultado["apellido"] == "Lopez",
            str((resultado["email"], resultado["nombre"], resultado["apellido"])),
        )
        check(
            "cuenta: queja escrita en el modal tras el login",
            "".join(piezas["motivo"].typed) == queja,
        )


def test_un_reporte_con_cuenta(check):
    print("(16b) ejecutar_un_reporte(cuenta=...) sin granja")
    eventos = {"creados": [], "guardados": 0, "cerrados": 0, "identidad": {}}

    class _BotFake:
        def __init__(self, **kwargs):
            eventos["creados"].append(kwargs)

        def reportar(self, identidad=None, queja=""):
            eventos["identidad"] = dict(identidad or {})
            eventos["queja"] = queja
            return {
                "ok": True,
                "email": "u1@x.com",
                "nombre": "Ana",
                "apellido": "Lopez",
                "queja": queja,
                "error": "",
                "evidencia": "ok",
                "url": "https://www.change.org/p/x",
                "estado_cuenta": "existente",
            }

        def cerrar(self):
            eventos["cerrados"] += 1

    def _guardar_no(*args, **kwargs):
        eventos["guardados"] += 1
        return {"guardada": True, "id": 1, "motivo": ""}

    def _identidad_no(*args, **kwargs):
        raise AssertionError("generar_identidad_change NO debe llamarse en modo cuenta")

    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake), mock.patch.object(
        change_org, "generar_identidad_change", _identidad_no
    ), mock.patch.object(
        change_org, "guardar_identidad_change", _guardar_no
    ), mock.patch.object(
        gc,
        "generar_queja_change",
        lambda contexto, evitar=None, variante=None: {
            "ok": True,
            "queja": "QUEJA-CUENTA",
            "usada_ia": False,
            "error": "",
        },
    ):
        resultado = ejecutar_un_reporte(
            "https://www.change.org/p/x",
            contexto="ctx",
            cuenta={"usuario": "u1", "email": "u1@x.com",
                    "nombre_mostrado": "Ana Maria Lopez"},
        )
    check(
        "un_reporte cuenta: bot recibe la cuenta",
        eventos["creados"][-1].get("cuenta", {}).get("usuario") == "u1",
    )
    check(
        "un_reporte cuenta: ok/estado_cuenta/usuario en el resultado",
        resultado["ok"] is True
        and resultado["estado_cuenta"] == "existente"
        and resultado["usuario"] == "u1",
        str({k: resultado.get(k) for k in ("ok", "estado_cuenta", "usuario")}),
    )
    check(
        "un_reporte cuenta: identidad desde nombre_mostrado (sin Faker)",
        resultado["identidad"]["nombre"] == "Ana"
        and resultado["identidad"]["apellido"] == "Maria Lopez"
        and resultado["identidad"]["email"] == "u1@x.com",
        str(resultado["identidad"]),
    )
    check(
        "un_reporte cuenta: NO guarda en la granja",
        resultado["identidad_guardada"] is False
        and resultado["guardado"] is None
        and eventos["guardados"] == 0,
    )
    check(
        "un_reporte cuenta: la queja IA se pasa al bot",
        eventos["queja"] == "QUEJA-CUENTA",
    )
    check(
        "un_reporte cuenta: bot.cerrar (finally)",
        eventos["cerrados"] == 1,
    )
    check(
        "un_reporte cuenta: cancelado None si no se cancelo",
        resultado["cancelado"] is None,
    )


# --------------------------------------------------------------------------- #
# (17) ejecutar_campana_reportes con cuentas (round-robin)
# --------------------------------------------------------------------------- #
def test_campana_cuentas(check):
    print("(17) ejecutar_campana_reportes(cuentas=...) en round-robin")
    llamadas = []

    def _fake(**kwargs):
        indice = len(llamadas)
        llamadas.append(kwargs)
        cuenta = kwargs.get("cuenta") or {}
        return {
            "ok": True,
            "email": str(cuenta.get("email") or ""),
            "usuario": str(cuenta.get("usuario") or ""),
            "queja": f"queja {indice}",
            "identidad": {},
            "identidad_guardada": False,
            "estado_cuenta": "existente",
            "error": "",
        }

    cuentas = [
        {"usuario": "u1", "email": "u1@x.com"},
        {"usuario": "u2", "email": "u2@x.com"},
    ]
    eventos = []
    with mock.patch.object(change_org, "ejecutar_un_reporte", _fake):
        resumen = ejecutar_campana_reportes(
            "u", cantidad=4, max_workers=2, usar_proxies=False,
            cuentas=cuentas, callback=eventos.append,
        )
    check(
        "campana cuentas: resumen con_cuentas/cuentas_total",
        resumen["con_cuentas"] is True and resumen["cuentas_total"] == 2,
        str((resumen["con_cuentas"], resumen["cuentas_total"])),
    )
    check(
        "campana cuentas: round-robin u1,u2,u1,u2",
        [l.get("cuenta", {}).get("usuario") for l in llamadas]
        == ["u1", "u2", "u1", "u2"],
        str([l.get("cuenta", {}).get("usuario") for l in llamadas]),
    )
    reportes = [e for e in eventos if e["tipo"] == "reporte"]
    check(
        "campana cuentas: eventos con usuario y con_cuenta (sin identidad)",
        len(reportes) == 4
        and all(
            e.get("con_cuenta") is True
            and "identidad" not in e
            and bool(e.get("usuario"))
            for e in reportes
        ),
    )
    check(
        "campana cuentas: resultados traen usuario/estado_cuenta",
        all(r.get("usuario") and r.get("estado_cuenta") for r in resumen["resultados"]),
    )
    check(
        "campana cuentas: enviados 4 y sin error",
        resumen["enviados"] == 4 and resumen["error"] == "",
    )

    # Anonimo intacto: sin cuentas no hay con_cuenta ni usuario en el evento.
    llamadas_anon = []

    def _fake_anon(**kwargs):
        llamadas_anon.append(kwargs)
        return {
            "ok": True,
            "email": "anon@x.com",
            "queja": "q",
            "identidad": {"email": "anon@x.com"},
            "identidad_guardada": False,
            "error": "",
        }

    eventos_anon = []
    with mock.patch.object(change_org, "ejecutar_un_reporte", _fake_anon):
        resumen_anon = ejecutar_campana_reportes(
            "u", cantidad=1, usar_proxies=False, callback=eventos_anon.append
        )
    reportes_anon = [e for e in eventos_anon if e["tipo"] == "reporte"]
    check(
        "campana anonima: con_cuentas False y cuentas_total 0",
        resumen_anon["con_cuentas"] is False
        and resumen_anon["cuentas_total"] == 0,
    )
    check(
        "campana anonima: evento con identidad y sin con_cuenta",
        len(reportes_anon) == 1
        and "identidad" in reportes_anon[0]
        and "con_cuenta" not in reportes_anon[0],
    )
    check(
        "campana anonima: ejecutar_un_reporte SIN kwarg cuenta",
        "cuenta" not in llamadas_anon[0],
    )


# --------------------------------------------------------------------------- #
# (18) MODO ASISTIDO: espera humana al reto anti-bot de Change.org
# --------------------------------------------------------------------------- #
def test_modo_asistido(check):
    print("(18) modo asistido: esperar_captcha_seg (visible/headless/cancel)")
    with mock.patch.dict(os.environ):
        os.environ.pop("CHANGE_ESPERAR_CAPTCHA_SEG", None)

        # --- resolucion: None -> env -> 0 y acotado a [0, 900] ---
        check(
            "asistido: sin env ni kwarg -> 0 (default)",
            change_org._resolver_esperar_captcha_seg() == 0,
        )
        check(
            "asistido: kwarg manda y se acota a [0, 900]",
            change_org._resolver_esperar_captcha_seg(45) == 45
            and change_org._resolver_esperar_captcha_seg(-5) == 0
            and change_org._resolver_esperar_captcha_seg(5000) == 900
            and change_org._resolver_esperar_captcha_seg("120") == 120,
        )
        check(
            "asistido: env/kwarg invalidos -> 0",
            change_org._resolver_esperar_captcha_seg("abc") == 0
            and change_org._resolver_esperar_captcha_seg("") == 0
            and change_org._resolver_esperar_captcha_seg(None) == 0,
        )
        os.environ["CHANGE_ESPERAR_CAPTCHA_SEG"] = "120"
        check(
            "asistido: env CHANGE_ESPERAR_CAPTCHA_SEG leido",
            change_org._resolver_esperar_captcha_seg() == 120,
        )
        check(
            "asistido: kwarg vence al env",
            change_org._resolver_esperar_captcha_seg(30) == 30,
        )
        check(
            "asistido: el bot resuelve env/kwarg al construirse",
            ChangeOrgReportBot().esperar_captcha_seg == 120
            and ChangeOrgReportBot(esperar_captcha_seg=77).esperar_captcha_seg == 77
            and ChangeOrgReportBot(esperar_captcha_seg="5000").esperar_captcha_seg
            == 900,
        )
        check(
            "asistido: aviso no callable -> None (no revienta)",
            ChangeOrgReportBot(aviso="no").aviso is None,
        )

    # --- _esperar_reto_humano: espera VISIBLE y exito al limpiarse ---
    avisos = []
    bot = _bot(
        FakeDriver(),
        headless=False,
        esperar_captcha_seg=3,
        aviso=avisos.append,
        cuenta={"usuario": "u1", "email": "u1@x.com"},
    )
    detecciones = {"n": 0}

    def _reto_limpia():
        detecciones["n"] += 1
        return "" if detecciones["n"] > 2 else "turnstile"

    with _sin_esperas():
        bot._detectar_reto_humano = _reto_limpia
        bot._detectar_captcha = lambda: ""
        check(
            "asistido: espera visible -> True cuando el reto desaparece",
            bot._esperar_reto_humano("turnstile") is True,
        )
    check(
        "asistido: aviso unico con usuario/email/segundos/estado",
        avisos
        == [
            {
                "tipo": "espera_captcha",
                "usuario": "u1",
                "email": "u1@x.com",
                "segundos": 3,
                "estado": "iniciando",
            }
        ],
        str(avisos),
    )
    check(
        "asistido: polling 1s hasta que desaparece (3 detecciones)",
        detecciones["n"] == 3 and bot.ultimo_error == "",
        f"n={detecciones['n']} err={bot.ultimo_error!r}",
    )
    check(
        "asistido: sin reto -> True inmediato",
        bot._esperar_reto_humano("") is True,
    )

    # --- timeout: mensaje accionable EXACTO ---
    avisos.clear()
    bot = _bot(
        FakeDriver(), headless=False, esperar_captcha_seg=2, aviso=avisos.append
    )
    with _sin_esperas():
        bot._detectar_reto_humano = lambda: "turnstile"
        bot._detectar_captcha = lambda: ""
        check(
            "asistido: timeout -> False",
            bot._esperar_reto_humano("turnstile") is False,
        )
    esperado_timeout = (
        "verificacion anti-bot de Change.org (Cloudflare): el reto no se "
        "resolvio en 2s; usa el modo asistido con Chrome visible y resuelvelo "
        "a mano"
    )
    check(
        "asistido: timeout deja el mensaje accionable exacto",
        bot.ultimo_error == esperado_timeout,
        bot.ultimo_error,
    )
    check("asistido: el timeout emite el aviso igual", len(avisos) == 1)

    # --- espera desactivada (default): fallo inmediato, sin aviso ---
    avisos.clear()
    bot = _bot(
        FakeDriver(), headless=False, esperar_captcha_seg=0, aviso=avisos.append
    )
    with _sin_esperas():
        check(
            "asistido: espera 0 -> False inmediato sin aviso ni error propio",
            bot._esperar_reto_humano("turnstile") is False
            and avisos == []
            and bot.ultimo_error == "",
        )

    # --- headless + espera > 0: NUNCA espera (no hay humano) y lo aclara ---
    avisos.clear()
    bot = _bot(
        FakeDriver(), headless=True, esperar_captcha_seg=5, aviso=avisos.append
    )
    with _sin_esperas():
        check(
            "asistido: headless -> False sin aviso",
            bot._esperar_reto_humano("turnstile") is False and avisos == [],
        )
    check(
        "asistido: headless aclara que no hay humano y pide Chrome visible",
        "headless" in bot.ultimo_error and "Chrome visible" in bot.ultimo_error,
        bot.ultimo_error,
    )

    # --- cancelacion DURANTE la espera -> MENSAJE_CANCELADO ---
    evento = threading.Event()
    avisos.clear()
    bot = _bot(
        FakeDriver(),
        headless=False,
        esperar_captcha_seg=10,
        aviso=avisos.append,
        cancelar=evento,
    )

    def _reto_cancela():
        evento.set()
        return "turnstile"

    with _sin_esperas():
        bot._detectar_reto_humano = _reto_cancela
        bot._detectar_captcha = lambda: ""
        check(
            "asistido: cancelar durante la espera -> MENSAJE_CANCELADO",
            bot._esperar_reto_humano("turnstile") is False
            and bot.ultimo_error == MENSAJE_CANCELADO,
            bot.ultimo_error,
        )
    check("asistido: la espera cancelada alcanzo a emitir su aviso", len(avisos) == 1)

    # --- un aviso ROTO no tumba la espera ---
    bot = _bot(
        FakeDriver(),
        headless=False,
        esperar_captcha_seg=1,
        aviso=lambda info: (_ for _ in ()).throw(RuntimeError("aviso roto")),
    )
    with _sin_esperas():
        check(
            "asistido: aviso que lanza no rompe el flujo",
            bot._esperar_reto_humano("turnstile") is True
            and bot.ultimo_error == "",
        )

    # --- integracion: reto ANTES del campo de correo ---
    avisos.clear()
    driver = FakeDriver()
    email = FakeElement(driver, tag="input", attrs={"type": "email"})
    driver.registrar(email)
    bot = _bot(
        driver,
        headless=False,
        esperar_captcha_seg=5,
        aviso=avisos.append,
        cuenta={"usuario": "u1", "email": "u1@x.com"},
    )
    detecciones = {"n": 0}

    def _reto_inicial():
        detecciones["n"] += 1
        return "" if detecciones["n"] > 2 else "turnstile-iframe"

    with _sin_esperas():
        bot._detectar_reto_humano = _reto_inicial
        bot._detectar_captcha = lambda: ""
        campo = bot._asegurar_campo_email(intentos=2)
    check(
        "asistido: reto antes del correo -> espera y sigue al campo",
        campo is email and detecciones["n"] == 3 and len(avisos) == 1,
        f"campo={campo is email} n={detecciones['n']} avisos={len(avisos)}",
    )

    # --- integracion: reto DESPUES de Continuar (flujo real de registro) ---
    avisos.clear()
    cuenta = {
        "usuario": "u1",
        "email": "nueva@x.com",
        "password": "claveSegura123",
        "nombre": "Ana",
        "apellido": "Lopez",
    }
    driver, piezas = _driver_login("nueva", reto=True)
    reto_frame = next(el for el in driver.elementos if el.tag == "iframe")
    estado = {"polls": 0}

    def _reto_tras_continuar():
        if not reto_frame.visible:
            return ""
        estado["polls"] += 1
        if estado["polls"] >= 3:
            # El humano resolvio el reto: aparece la pantalla de contrasena.
            reto_frame.visible = False
            piezas["clave"].visible = True
            piezas["btn_clave"].visible = True
            driver.page_source = (
                "<h1>Crea tu contraseña</h1>"
                "<p>Debe tener al menos 10 caracteres</p>"
            )
            return ""
        return "turnstile-iframe"

    bot = _bot(
        driver,
        cuenta=cuenta,
        headless=False,
        esperar_captcha_seg=10,
        aviso=avisos.append,
    )
    with _sin_esperas():
        bot._detectar_reto_humano = _reto_tras_continuar
        bot._detectar_captcha = lambda: ""
        with mock.patch.object(change_org.random, "random", return_value=0.99):
            registro = bot.registrar_o_entrar()
    check(
        "asistido: el registro continua solo tras resolver el reto",
        registro["ok"] is True and registro["estado"] == "nueva",
        str(registro),
    )
    check(
        "asistido: la contrasena se tipeo tras la espera",
        "".join(piezas["clave"].typed) == "claveSegura123",
        "".join(piezas["clave"].typed),
    )
    check(
        "asistido: aviso espera_captcha tambien en el registro",
        len(avisos) == 1
        and avisos[0] == {
            "tipo": "espera_captcha",
            "usuario": "u1",
            "email": "nueva@x.com",
            "segundos": 10,
            "estado": "iniciando",
        },
        str(avisos),
    )

    # --- integracion: timeout del reto en el registro (mensaje exacto) ---
    driver, piezas = _driver_login("nueva", reto=True)
    reto_frame = next(el for el in driver.elementos if el.tag == "iframe")
    bot = _bot(
        driver,
        cuenta=cuenta,
        headless=False,
        esperar_captcha_seg=2,
        aviso=lambda info: None,
    )
    with _sin_esperas():
        bot._detectar_reto_humano = lambda: (
            "turnstile-iframe" if reto_frame.visible else ""
        )
        bot._detectar_captcha = lambda: ""
        registro = bot.registrar_o_entrar()
    check(
        "asistido: timeout en el registro -> error accionable exacto",
        registro["ok"] is False and registro["error"] == esperado_timeout,
        str(registro["error"]),
    )

    # --- integracion: headless con espera >0 aclara el motivo ---
    driver, piezas = _driver_login("nueva", reto=True)
    reto_frame = next(el for el in driver.elementos if el.tag == "iframe")
    bot = _bot(
        driver,
        cuenta=cuenta,
        headless=True,
        esperar_captcha_seg=5,
        aviso=lambda info: None,
    )
    with _sin_esperas():
        bot._detectar_reto_humano = lambda: (
            "turnstile-iframe" if reto_frame.visible else ""
        )
        bot._detectar_captcha = lambda: ""
        registro = bot.registrar_o_entrar()
    check(
        "asistido: headless en el registro aclara que no hay humano",
        registro["ok"] is False
        and "headless" in registro["error"]
        and "Chrome visible" in registro["error"],
        str(registro["error"]),
    )

    # --- esperar_captcha_seg=0 conserva EXACTO el fallo clasico ---
    avisos.clear()
    driver, piezas = _driver_login("nueva", reto=True)
    bot = _bot(
        driver, cuenta=cuenta, headless=False, esperar_captcha_seg=0,
        aviso=avisos.append,
    )
    with _sin_esperas():
        registro = bot.registrar_o_entrar()
    check(
        "asistido: espera 0 conserva el fallo clasico del reto",
        registro["ok"] is False
        and "reto '" in registro["error"]
        and "reintenta con otro" in registro["error"]
        and avisos == [],
        str(registro["error"]),
    )


def test_campana_espera_captcha(check):
    print("(18b) campanas reenvian el evento espera_captcha")
    capturado = {}

    class _BotConAviso:
        """Bot fake que AVISA al inicio del registro (como el reto inicial)."""

        def __init__(self, **kwargs):
            capturado.update(kwargs)
            self.ultimo_error = ""

        def preparar_driver(self):
            return True

        def registrar_o_entrar(self):
            aviso = capturado.get("aviso")
            if callable(aviso):
                aviso(
                    {
                        "tipo": "espera_captcha",
                        "segundos": 12,
                        "estado": "iniciando",
                    }
                )
            return {"ok": True, "estado": "nueva", "error": "", "evidencia": "ok"}

        def _url_actual(self):
            return "https://www.change.org/"

        def cerrar(self):
            pass

    eventos = []
    cuentas = [
        {
            "usuario": "u1",
            "email": "u1@x.com",
            "email_password": "claveSegura123",
        }
    ]
    with mock.patch.object(
        change_org, "ChangeOrgReportBot", _BotConAviso
    ), mock.patch.object(
        change_org, "proxies_disponibles", lambda pais="": []
    ):
        resumen = ejecutar_campana_registros(
            cuentas,
            usar_proxies=False,
            esperar_captcha_seg=45,
            callback=eventos.append,
        )
    check(
        "campana registros: el bot recibe esperar_captcha_seg y el aviso",
        capturado.get("esperar_captcha_seg") == 45
        and callable(capturado.get("aviso")),
        str(capturado.get("esperar_captcha_seg")),
    )
    espera_ev = [e for e in eventos if e.get("tipo") == "espera_captcha"]
    check(
        "campana registros: evento espera_captcha con cuenta y detalle",
        espera_ev
        == [
            {
                "tipo": "espera_captcha",
                "hechas": 0,
                "total": 1,
                "usuario": "u1",
                "email": "u1@x.com",
                "detalle": "esperando captcha (12s)",
            }
        ],
        str(espera_ev),
    )
    check(
        "campana registros: el registro termina ok tras el aviso",
        resumen["exitosos"] == 1 and resumen["error"] == "",
        str(resumen),
    )

    # --- ejecutar_un_reporte: forward del aviso a la campana de reportes ---
    llamadas = []

    def _reporte_fake(**kwargs):
        llamadas.append(kwargs)
        aviso = kwargs.get("_aviso")
        if callable(aviso):
            aviso({"tipo": "espera_captcha", "segundos": 30})
        cuenta = kwargs.get("cuenta") or {}
        return {
            "ok": True,
            "email": str(cuenta.get("email") or ""),
            "usuario": str(cuenta.get("usuario") or ""),
            "queja": "q",
            "identidad": {},
            "identidad_guardada": False,
            "estado_cuenta": "existente",
            "error": "",
        }

    eventos = []
    with mock.patch.object(change_org, "ejecutar_un_reporte", _reporte_fake):
        resumen = ejecutar_campana_reportes(
            "u",
            cantidad=1,
            usar_proxies=False,
            cuentas=[{"usuario": "u1", "email": "u1@x.com"}],
            esperar_captcha_seg=15,
            callback=eventos.append,
        )
    espera_ev = [e for e in eventos if e.get("tipo") == "espera_captcha"]
    check(
        "campana reportes: evento espera_captcha con cuenta y detalle",
        espera_ev
        == [
            {
                "tipo": "espera_captcha",
                "hechas": 0,
                "total": 1,
                "usuario": "u1",
                "email": "u1@x.com",
                "detalle": "esperando captcha (30s)",
            }
        ],
        str(espera_ev),
    )
    check(
        "campana reportes: forward de esperar_captcha_seg y _aviso al reporte",
        len(llamadas) == 1
        and llamadas[0].get("esperar_captcha_seg") == 15
        and callable(llamadas[0].get("_aviso")),
        str(llamadas[:1]),
    )
    check(
        "campana reportes: el reporte termina ok tras el aviso",
        resumen["enviados"] == 1 and resumen["error"] == "",
        str(resumen),
    )

    # --- registrar_cuenta_change reenvia los kwarg al bot ---
    capturado.clear()

    class _BotSimple:
        def __init__(self, **kwargs):
            capturado.update(kwargs)
            self.ultimo_error = ""

        def preparar_driver(self):
            return True

        def registrar_o_entrar(self):
            return {"ok": True, "estado": "nueva", "error": "", "evidencia": ""}

        def _url_actual(self):
            return "u"

        def cerrar(self):
            pass

    aviso_fn = lambda info: None
    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotSimple):
        registrar_cuenta_change(
            usuario="u9",
            email="u9@x.com",
            password="claveSegura123",
            esperar_captcha_seg=60,
            _aviso=aviso_fn,
        )
    check(
        "registrar: forward a bot de esperar_captcha_seg y aviso",
        capturado.get("esperar_captcha_seg") == 60
        and capturado.get("aviso") is aviso_fn,
        str(capturado.get("esperar_captcha_seg")),
    )

    # --- None -> env resuelto TAMBIEN en la capa publica ---
    capturado.clear()
    with mock.patch.dict(os.environ, {"CHANGE_ESPERAR_CAPTCHA_SEG": "240"}), \
            mock.patch.object(change_org, "ChangeOrgReportBot", _BotSimple):
        registrar_cuenta_change(
            usuario="u8", email="u8@x.com", password="claveSegura123"
        )
    check(
        "registrar: None -> env resuelto antes de crear el bot",
        capturado.get("esperar_captcha_seg") == 240,
        str(capturado.get("esperar_captcha_seg")),
    )
    capturado.clear()
    with mock.patch.dict(os.environ, {"CHANGE_ESPERAR_CAPTCHA_SEG": "240"}), \
            mock.patch.object(change_org, "ChangeOrgReportBot", _BotSimple):
        registrar_cuenta_change(
            usuario="u8", email="u8@x.com", password="claveSegura123",
            esperar_captcha_seg=15,
        )
    check(
        "registrar: kwarg vence al env antes de crear el bot",
        capturado.get("esperar_captcha_seg") == 15,
        str(capturado.get("esperar_captcha_seg")),
    )

    # --- ejecutar_un_reporte (modo cuenta) reenvia los kwarg al bot ---
    capturado.clear()

    class _BotReporte:
        def __init__(self, **kwargs):
            capturado.update(kwargs)

        def reportar(self, identidad=None, queja=""):
            return {
                "ok": True,
                "email": "",
                "usuario": "u9",
                "queja": queja,
                "error": "",
                "evidencia": "ok",
                "url": "u",
            }

        def cerrar(self):
            pass

    with mock.patch.object(
        change_org, "ChangeOrgReportBot", _BotReporte
    ), mock.patch.object(
        gc,
        "generar_queja_change",
        lambda *a, **k: {"ok": True, "queja": "Q", "usada_ia": False, "error": ""},
    ):
        ejecutar_un_reporte(
            "u",
            cuenta={"usuario": "u9", "email": "u9@x.com"},
            esperar_captcha_seg=90,
            _aviso=aviso_fn,
        )
    check(
        "un_reporte: forward a bot de esperar_captcha_seg y aviso",
        capturado.get("esperar_captcha_seg") == 90
        and capturado.get("aviso") is aviso_fn,
        str(capturado.get("esperar_captcha_seg")),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_generar_identidad(check)
    test_proxies(check)
    test_enlace(check)
    test_formulario(check)
    test_resultado(check)
    test_reportar(check)
    test_preparar_driver(check)
    test_guardar_identidad(check)
    test_ejecutar_un_reporte(check)
    test_campana(check)
    test_password_change(check)
    test_registro_flujo(check)
    test_registrar_cuenta_change(check)
    test_campana_registros(check)
    test_reporte_modal(check)
    test_reporte_con_cuenta(check)
    test_un_reporte_con_cuenta(check)
    test_campana_cuentas(check)
    test_modo_asistido(check)
    test_campana_espera_captcha(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_change_backend.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
