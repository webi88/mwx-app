# -*- coding: utf-8 -*-
"""Tests rapidos del flujo de FIRMAR peticiones de Change.org (SIN Chrome).

Cubre las piezas nuevas de `cuentas/change_org.py` que implementan la firma
masiva (ademas de la denuncia existente):

  (1) `ChangeOrgReportBot.firmar()` con FakeDriver completo (flujo de 8 pasos):
      "Firmar la peticion" -> "¡Ya firmaste! Ahora aporta o comparte" ->
      "No, prefiero compartirla" -> "Copiar enlace" -> "Continuar" -> evidencia
      positiva y `usada_firma=True` + `captura`.
  (2) `firmar()` falla claro sin el boton "Firmar la peticion" (nunca el
      timeout generico) y sin URL.
  (3) `firmar()` en modo cuenta: `registrar_o_entrar` se ejecuta primero y, si
      restaura sesion, NO escribe el correo (anti-captcha) y propaga
      `sesion_restaurada`/`estado_cuenta`.
  (4) `marcar_identidad_firmada`: marca `CuentaChange.usada_firma=True` (sqlite
      en memoria) y tolera email inexistente (nunca lanza).
  (5) `ejecutar_un_firma` con `ChangeOrgReportBot` FAKE: el resultado de
      `firmar()` se propaga, `bot.cerrar()` se llama SIEMPRE (finally) y, en
      modo anonimo ok, se guarda la identidad y se marca `usada_firma`.
  (6) `ejecutar_campana_firmas` con `ejecutar_un_firma` FAKE: round-robin de
      proxies, callbacks `inicio` + N `firma` con `hechas` 1..N, resumen
      consistente (`firmadas`/`fallidos`), `cancelar` pre-seteado y nunca lanza.

Determinista: `time.sleep` neutralizado, Chrome/BD/proxy/IA monkeypatcheados y
`_DIR_CAPTURAS_CHANGE` apuntando a un temp dir. No se ejecutan campanas reales.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_firmar.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as database  # noqa: E402
import cuentas.change_org as change_org  # noqa: E402
from core.database import Base  # noqa: E402
from core.models import CuentaChange  # noqa: E402
from cuentas.change_org import (  # noqa: E402
    MENSAJE_CANCELADO,
    ChangeOrgReportBot,
    ejecutar_campana_firmas,
    ejecutar_un_firma,
    generar_identidad_change,
    guardar_identidad_change,
    marcar_identidad_firmada,
)


# --------------------------------------------------------------------------- #
# Fakes minimos (driver/elemento) para el flujo de firma
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _sin_esperas():
    original = time.sleep
    time.sleep = lambda *args, **kwargs: None
    try:
        yield
    finally:
        time.sleep = original


def _coincide_atributo(elemento, expresion) -> bool:
    expresion = expresion.strip()
    if "=" not in expresion:
        return elemento.get_attribute(expresion) not in (None, "")
    attr, valor = expresion.split("=", 1)
    op = "="
    if attr.endswith(("*", "^", "$")):
        op, attr = attr[-1], attr[:-1]
    valor = valor.strip().strip("'\"")
    if valor.lower().endswith(" i"):
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


class _FakeElement:
    def __init__(
        self,
        driver,
        tag="button",
        texto="",
        attrs=None,
        visible=True,
        enabled=True,
    ):
        self.driver = driver
        self.tag = tag
        self._texto = texto
        self.attrs = {str(k).lower(): v for k, v in (attrs or {}).items()}
        self.visible = visible
        self.enabled = enabled
        self.click_count = 0
        self.al_click = None

    @property
    def text(self):
        return self._texto

    def set_texto(self, texto):
        self._texto = texto

    def get_attribute(self, nombre):
        nombre = str(nombre or "").lower()
        if nombre == "href":
            return self.attrs.get("href")
        return self.attrs.get(nombre)

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def click(self):
        self.click_count += 1
        self.driver.clicks.append(self)
        if self.al_click:
            self.al_click()

    def send_keys(self, *valores):
        pass

    def find_elements(self, by, selector):
        return []


class _FakeDriver:
    def __init__(self):
        self.current_url = "about:blank"
        self.title = ""
        self.page_source = ""
        self.body = None
        self.window_handles = ["w1"]
        self.ventana_actual = "w1"
        self.elementos = []
        self.clicks = []
        self.scripts = []
        self.al_navegar = None
        self.screenshots = []
        self.refresh_count = 0
        self.cookies = []
        self.cdp_calls = []
        self.switch_to = self._SwitchTo(self)

    class _SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.ventana_actual = handle

    def registrar(self, *elementos):
        for elemento in elementos:
            elemento.driver = self
            self.elementos.append(elemento)
        return elementos[0] if len(elementos) == 1 else elementos

    def set_body_texto(self, texto):
        if self.body is None:
            self.body = _FakeElement(self, tag="body", texto=texto)
        else:
            self.body.set_texto(texto)

    def get(self, url):
        self.current_url = url
        if self.al_navegar:
            self.al_navegar(url)

    def refresh(self):
        self.refresh_count += 1

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "readyState" in str(script):
            return "complete"
        return None

    def execute_cdp_cmd(self, cmd, params):
        self.cdp_calls.append((cmd, params))
        return {"success": True}

    def get_cookies(self):
        return [dict(c) for c in self.cookies]

    def save_screenshot(self, ruta):
        self.screenshots.append(ruta)
        with open(ruta, "wb") as archivo:
            archivo.write(b"\x89PNG\r\n\x1a\nfake")
        return True

    def find_elements(self, by, selector):
        if by == By.TAG_NAME:
            return [el for el in self.elementos if el.tag == selector]
        if by == By.CSS_SELECTOR:
            return [el for el in self.elementos if _coincide_selector(el, selector)]
        return []

    def find_element(self, by, selector):
        if by == By.TAG_NAME and selector == "body" and self.body is not None:
            return self.body
        encontrados = self.find_elements(by, selector)
        if not encontrados:
            raise NoSuchElementException(f"no encontrado: {selector}")
        return encontrados[0]

    def quit(self):
        pass


def _bot(driver=None, **kwargs):
    bot = ChangeOrgReportBot(**kwargs)
    if driver is not None:
        bot.driver = driver
    bot.preparar_driver = lambda: True
    return bot


def _driver_firma():
    """FakeDriver que simula el flujo de 8 pasos de la firma."""
    driver = _FakeDriver()
    driver.set_body_texto("Firma esta petición")

    btn_firmar = _FakeElement(driver, tag="button", texto="Firmar la petición")
    btn_no = _FakeElement(
        driver, tag="button", texto="No, prefiero compartirla", visible=False
    )
    btn_copiar = _FakeElement(
        driver, tag="button", texto="Copiar enlace", visible=False
    )
    btn_continuar = _FakeElement(driver, tag="button", texto="Continuar", visible=False)
    driver.registrar(btn_firmar, btn_no, btn_copiar, btn_continuar)

    def _tras_firmar():
        # Paso 3: pantalla "¡Ya firmaste! Ahora aporta o comparte".
        driver.set_body_texto("¡Ya firmaste! Ahora aporta o comparte")
        btn_firmar.visible = False
        btn_no.visible = True

    btn_firmar.al_click = _tras_firmar

    def _tras_no_compartir():
        # Paso 5: OTRA pestana con el enlace de compartir.
        driver.window_handles = ["w1", "w2"]
        driver.set_body_texto("Comparte esta petición")
        btn_no.visible = False
        btn_copiar.visible = True

    btn_no.al_click = _tras_no_compartir

    def _tras_copiar():
        # Paso 7: abajo aparece "Continuar".
        btn_copiar.visible = False
        btn_continuar.visible = True

    btn_copiar.al_click = _tras_copiar

    def _tras_continuar():
        # Paso 8: la firma queda lista (evidencia positiva final).
        driver.set_body_texto("firmaste esta petición")
        btn_continuar.visible = False

    btn_continuar.al_click = _tras_continuar

    piezas = {
        "btn_firmar": btn_firmar,
        "btn_no": btn_no,
        "btn_copiar": btn_copiar,
        "btn_continuar": btn_continuar,
    }
    return driver, piezas


def _engine_memoria():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_firmar_flujo(check):
    print("(1) firmar() flujo de 8 pasos con FakeDriver")
    with tempfile.TemporaryDirectory(prefix="change_firma_") as capturas_tmp, \
            _sin_esperas(), \
            mock.patch.object(change_org, "_DIR_CAPTURAS_CHANGE", capturas_tmp), \
            mock.patch.dict(os.environ, {"CHANGE_CAPTCHA_SOLVER": "off"}):
        driver, piezas = _driver_firma()
        identidad = generar_identidad_change()
        bot = _bot(
            driver,
            url_peticion="https://www.change.org/p/victorcastrocos-salvar",
            resolver_captcha="off",
        )
        resultado = bot.firmar(identidad=identidad)
        check(
            "firmar: ok True y evidencia positiva",
            resultado["ok"] is True and "confirmacion" in str(resultado["evidencia"]),
            str(resultado),
        )
        check(
            "firmar: usada_firma True",
            resultado["usada_firma"] is True,
            str(resultado["usada_firma"]),
        )
        check(
            "firmar: captura no vacia (PNG de evidencia)",
            bool(resultado["captura"])
            and os.path.isfile(str(resultado["captura"])),
            str(resultado["captura"]),
        )
        check(
            "firmar: email/nombre/apellido de la identidad propagados",
            resultado["email"] == identidad["email"]
            and resultado["nombre"] == identidad["nombre"]
            and resultado["apellido"] == identidad["apellido"],
            str({k: resultado[k] for k in ("email", "nombre", "apellido")}),
        )
        check(
            "firmar: los 4 botones del flujo fueron clicados en orden",
            [el.text for el in driver.clicks]
            == [
                "Firmar la petición",
                "No, prefiero compartirla",
                "Copiar enlace",
                "Continuar",
            ],
            str([el.text for el in driver.clicks]),
        )
        check(
            "firmar: la pestana de compartir se abrio (cambio a w2)",
            driver.ventana_actual == "w2" and len(driver.window_handles) == 2,
            f"ventana={driver.ventana_actual} handles={driver.window_handles}",
        )


def test_firmar_sin_boton_y_sin_url(check):
    print("(2) firmar() fallos claros")
    with tempfile.TemporaryDirectory(prefix="change_firma_") as capturas_tmp, \
            _sin_esperas(), \
            mock.patch.object(change_org, "_DIR_CAPTURAS_CHANGE", capturas_tmp), \
            mock.patch.dict(os.environ, {"CHANGE_CAPTCHA_SOLVER": "off"}):
        # Sin boton "Firmar la peticion".
        driver = _FakeDriver()
        driver.set_body_texto("Peticion normal sin boton de firma")
        bot = _bot(
            driver,
            url_peticion="https://www.change.org/p/x",
            resolver_captcha="off",
        )
        resultado = bot.firmar()
        check(
            "firmar: sin boton -> error claro (no el timeout generico)",
            resultado["ok"] is False
            and "no se encontro el boton Firmar la peticion" in str(resultado["error"]),
            str(resultado["error"]),
        )
        check(
            "firmar: sin boton -> usada_firma False y captura vacia",
            resultado["usada_firma"] is False and resultado["captura"] == "",
            str(resultado),
        )

        # Sin URL.
        bot_sin_url = _bot(driver)
        resultado_sin_url = bot_sin_url.firmar()
        check(
            "firmar: sin URL -> error 'falta la URL de la peticion a firmar'",
            resultado_sin_url["ok"] is False
            and resultado_sin_url["error"] == "falta la URL de la peticion a firmar",
            str(resultado_sin_url),
        )

        # Cancelado antes de empezar.
        evento = threading.Event()
        evento.set()
        bot_cancelado = _bot(driver, url_peticion="https://www.change.org/p/x",
                             cancelar=evento)
        resultado_cancelado = bot_cancelado.firmar()
        check(
            "firmar: cancelar pre-seteado -> cancelado sin navegar",
            resultado_cancelado.get("cancelado") is True
            and resultado_cancelado["error"] == MENSAJE_CANCELADO,
            str(resultado_cancelado),
        )


def test_firmar_con_cuenta_sesion_restaurada(check):
    print("(3) firmar() en modo cuenta: sesion restaurada (anti-captcha)")
    with tempfile.TemporaryDirectory(prefix="change_firma_") as capturas_tmp, \
            _sin_esperas(), \
            mock.patch.object(change_org, "_DIR_CAPTURAS_CHANGE", capturas_tmp), \
            mock.patch.dict(os.environ, {"CHANGE_CAPTCHA_SOLVER": "off"}):
        driver, piezas = _driver_firma()
        cuenta = {
            "usuario": "u1",
            "email": "u1@x.com",
            "email_password": "claveSegura123",
        }
        bot = _bot(driver, url_peticion="https://www.change.org/p/x",
                   cuenta=cuenta, resolver_captcha="off")

        # registrar_o_entrar fake: restaura sesion sin escribir el correo.
        llamado = {"registrar": 0}

        def _registrar_fake():
            llamado["registrar"] += 1
            return {
                "ok": True,
                "estado": "existente",
                "error": "",
                "evidencia": "sesion restaurada (cookies)",
                "sesion_restaurada": True,
            }

        bot.registrar_o_entrar = _registrar_fake
        resultado = bot.firmar()
        check(
            "firmar cuenta: ok True con sesion restaurada",
            resultado["ok"] is True
            and resultado["sesion_restaurada"] is True,
            str(resultado),
        )
        check(
            "firmar cuenta: registrar_o_entrar llamado una vez",
            llamado["registrar"] == 1,
            str(llamado),
        )
        check(
            "firmar cuenta: usuario y estado_cuenta propagados",
            resultado["usuario"] == "u1"
            and resultado["estado_cuenta"] == "existente",
            str({k: resultado.get(k) for k in ("usuario", "estado_cuenta")}),
        )


def test_marcar_identidad_firmada(check):
    print("(4) marcar_identidad_firmada (sqlite en memoria)")
    identidad = generar_identidad_change()
    engine = _engine_memoria()
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    original = database.SessionLocal
    database.SessionLocal = fabrica
    try:
        guardada = guardar_identidad_change(
            identidad, url_peticion="https://www.change.org/p/x", origen="firma"
        )
        check("marcar: identidad previa guardada", guardada["guardada"] is True)

        marcada = marcar_identidad_firmada(identidad["email"])
        check(
            "marcar: marcada True con id",
            marcada["marcada"] is True and isinstance(marcada["id"], int),
            str(marcada),
        )
        db = fabrica()
        try:
            fila = db.query(CuentaChange).filter_by(email=identidad["email"]).first()
            check("marcar: usada_firma quedó True en la BD", bool(fila.usada_firma))
        finally:
            db.close()

        inexistente = marcar_identidad_firmada("no.existe999@x.com")
        check(
            "marcar: email inexistente -> marcada False con motivo 'no existe'",
            inexistente["marcada"] is False and inexistente["motivo"] == "no existe",
            str(inexistente),
        )
        vacio = marcar_identidad_firmada("")
        check(
            "marcar: email vacio -> motivo 'sin email'",
            vacio["marcada"] is False and vacio["motivo"] == "sin email",
            str(vacio),
        )
    finally:
        database.SessionLocal = original
        engine.dispose()


def test_ejecutar_un_firma(check):
    print("(5) ejecutar_un_firma (bot fake + guardado/marcado)")
    resultado_bot = {
        "ok": True,
        "email": "ana.lopez1@gmail.com",
        "nombre": "Ana",
        "apellido": "Lopez",
        "error": "",
        "evidencia": "confirmacion en pantalla: 'ya firmaste'",
        "url": "https://www.change.org/p/x",
        "captura": "",
        "usada_firma": True,
        "sesion_restaurada": False,
    }

    class _BotFake:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.cerrado = False

        def firmar(self, identidad=None):
            return dict(resultado_bot)

        def cerrar(self):
            self.cerrado = True

    fabricados = []
    original_bot = change_org.ChangeOrgReportBot

    def _fabrica(*args, **kwargs):
        bot = _BotFake(*args, **kwargs)
        fabricados.append(bot)
        return bot

    guardados = []
    marcados = []
    with mock.patch.object(change_org, "ChangeOrgReportBot", _fabrica), \
            mock.patch.object(
                change_org,
                "guardar_identidad_change",
                side_effect=lambda identidad, **kw: guardados.append(identidad)
                or {"guardada": True, "id": 1},
            ), \
            mock.patch.object(
                change_org,
                "marcar_identidad_firmada",
                side_effect=lambda email: marcados.append(email)
                or {"marcada": True, "id": 1},
            ):
        resultado = ejecutar_un_firma("https://www.change.org/p/x")
    finally_cerrado = bool(fabricados) and fabricados[0].cerrado
    check(
        "un firma: resultado ok con identidad propagada",
        resultado["ok"] is True
        and isinstance(resultado["identidad"], dict)
        and bool(resultado["identidad"].get("email")),
        str(resultado),
    )
    check(
        "un firma: bot.cerrar() SIEMPRE llamado (finally)",
        finally_cerrado,
        str(finally_cerrado),
    )
    check(
        "un firma: identidad guardada y marcada usada_firma",
        resultado["identidad_guardada"] is True
        and resultado["usada_firma"] is True
        and len(guardados) == 1
        and len(marcados) == 1,
        f"guardados={len(guardados)} marcados={len(marcados)}",
    )

    # cancelar pre-seteado -> cancelado sin abrir navegador.
    evento = threading.Event()
    evento.set()
    fabricados.clear()
    with mock.patch.object(change_org, "ChangeOrgReportBot", _fabrica):
        resultado_cancelado = ejecutar_un_firma(
            "https://www.change.org/p/x", cancelar=evento
        )
    check(
        "un firma: cancelar pre-seteado -> cancelado sin bot",
        resultado_cancelado.get("cancelado") is True
        and resultado_cancelado["error"] == MENSAJE_CANCELADO
        and not fabricados,
        str(resultado_cancelado),
    )

    # Modo cuenta: no guarda en la granja y agrega usuario.
    fabricados.clear()
    cuenta = {"usuario": "u1", "email": "u1@x.com", "email_password": "claveSegura123"}
    with mock.patch.object(change_org, "ChangeOrgReportBot", _fabrica):
        resultado_cuenta = ejecutar_un_firma(
            "https://www.change.org/p/x", cuenta=cuenta
        )
    check(
        "un firma: modo cuenta -> usuario + identidad_guardada False",
        resultado_cuenta.get("usuario") == "u1"
        and resultado_cuenta.get("identidad_guardada") is False,
        str(resultado_cuenta),
    )


def test_campana_firmas(check):
    print("(6) ejecutar_campana_firmas (ejecutar_un_firma fake)")
    llamadas = []
    original_un_firma = change_org.ejecutar_un_firma

    def _un_firma_fake(**kwargs):
        llamadas.append(kwargs)
        indice = len(llamadas)
        return {
            "ok": True,
            "email": f"firma{indice}@x.com",
            "identidad": {"email": f"firma{indice}@x.com"},
            "identidad_guardada": True,
            "usada_firma": True,
            "usuario": "",
        }

    proxies = ["p1", "p2", "p3"]
    with mock.patch.object(change_org, "ejecutar_un_firma", _un_firma_fake), \
            mock.patch.object(change_org, "proxies_disponibles", return_value=proxies), \
            mock.patch.object(change_org, "proxy_change_valido", return_value=True), \
            mock.patch.dict(os.environ, {"CHANGE_CAPTCHA_SOLVER": "off"}):
        eventos = []
        resumen = ejecutar_campana_firmas(
            "https://www.change.org/p/x",
            cantidad=5,
            max_workers=2,
            usar_proxies=True,
            callback=lambda e: eventos.append(e),
        )
    check(
        "campana firmas: total 5 y firmadas 5 (0 fallidos)",
        resumen["total"] == 5 and resumen["firmadas"] == 5 and resumen["fallidos"] == 0,
        str({k: resumen[k] for k in ("total", "firmadas", "fallidos")}),
    )
    check(
        "campana firmas: callback inicio + 5 firmas con hechas 1..5",
        eventos[0].get("tipo") == "inicio"
        and sum(1 for e in eventos if e.get("tipo") == "firma") == 5
        and [e.get("hechas") for e in eventos if e.get("tipo") == "firma"]
        == [1, 2, 3, 4, 5],
        str([e.get("tipo") for e in eventos]),
    )
    check(
        "campana firmas: proxies_total 3 y round-robin sobre los 3",
        resumen["proxies_total"] == 3
        and set(llamadas[i]["proxy"] for i in range(min(3, len(llamadas))))
        == set(proxies),
        str(resumen["proxies_total"]),
    )
    check(
        "campana firmas: identidades_guardadas 5",
        resumen["identidades_guardadas"] == 5,
        str(resumen["identidades_guardadas"]),
    )

    # cancelar pre-seteado -> cancelada sin encolar.
    evento = threading.Event()
    evento.set()
    with mock.patch.object(change_org, "ejecutar_un_firma", _un_firma_fake), \
            mock.patch.object(change_org, "proxies_disponibles", return_value=[]):
        resumen_cancelada = ejecutar_campana_firmas(
            "https://www.change.org/p/x", cantidad=5, cancelar=evento
        )
    check(
        "campana firmas: cancelar pre-seteado -> cancelada y 0 firmadas",
        resumen_cancelada["cancelada"] is True and resumen_cancelada["firmadas"] == 0,
        str(resumen_cancelada),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks (Chrome/BD/proxy/IA monkeypatcheados; sin campanas reales)."""
    test_firmar_flujo(check)
    test_firmar_sin_boton_y_sin_url(check)
    test_firmar_con_cuenta_sesion_restaurada(check)
    test_marcar_identidad_firmada(check)
    test_ejecutar_un_firma(check)
    test_campana_firmas(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
