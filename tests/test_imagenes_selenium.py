"""Suite rapida de `_subir_imagen` de `plataformas/twitter/selenium_bot.py`.

Version permanente de los checks del fix real de Railway: con una imagen de
verdad la subida/procesado puede tardar mas que el `time.sleep(1.5)` fijo que
usaba `_subir_imagen`; el flujo seguia con el boton "Post" todavia deshabilitado
y el post fallaba ("boton Post deshabilitado"). Verifica, sin Chrome:

  - el input file se busca PRIMERO dentro del `div[role='dialog']` VISIBLE y
    cae al selector global historico si no hay dialogo (o esta oculto);
  - `send_keys` recibe la ruta ABSOLUTA de la imagen;
  - la espera ACTIVA de la vista previa (poll ~0.3s) con timeout configurable
    al final (`timeout=15.0` por default conserva el flujo de los callers);
  - los selectores candidatos de vista previa: `[data-testid='attachments']`,
    `[data-testid='tweetPhoto']`, `img[src^='blob:']` (SOLO dentro del dialogo,
    para no contar la foto de perfil) y los `[aria-label*='Media'/'multimedia'/
    'Imagen'/'Image']`;
  - la espera sale de inmediato al detectar la vista previa (cortesia 0.2s,
    sin 1.5s fijos) y `_subir_imagen` NUNCA lanza: si el input no existe, si
    `send_keys` falla o si la vista previa no aparece en el plazo, deja
    constancia y CONTINUA.

Uso:
    .venv/Scripts/python.exe tests/test_imagenes_selenium.py   (solo este archivo)
    .venv/Scripts/python.exe tests/run_tests.py

Determinista y rapido: usa FakeDriver/FakeElemento (sin red ni Chrome) y
timeouts pequenos; el caso "nunca aparece" usa el timeout real de 0.6s.
"""
from __future__ import annotations

import inspect
import os
import sys
import time
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402

# Selector del input de imagen de X (contrato historico del bot).
SEL_INPUT = "input[type='file'][accept*='image']"
# Selector de vista previa mas fiable (el primero de la lista del bot).
SEL_PREVIEW = "[data-testid='attachments']"
# Selector de la foto de perfil (blob global que NO debe contar).
SEL_BLOB = "img[src^='blob:']"


class FakeElemento:
    """Elemento minimo con la API que usa `_subir_imagen`/`_buscar_input_imagen`."""

    def __init__(self, visible=True, send_keys_lanza=False):
        self.visible = visible
        self.send_keys_lanza = send_keys_lanza
        self.enviados = []
        self.por_selector = {}

    def is_displayed(self):
        return self.visible

    def send_keys(self, valor):
        if self.send_keys_lanza:
            raise RuntimeError("send_keys roto (test)")
        self.enviados.append(valor)

    def find_elements(self, by, sel):
        return list(self.por_selector.get(sel, []))

    def agregar(self, sel, elementos):
        self.por_selector.setdefault(sel, []).extend(elementos)
        return self


class PreviewDinamica:
    """Vista previa que aparece a partir de la llamada N (0 = ya presente)."""

    def __init__(self, tras=1, visible=True):
        self.tras = tras
        self.llamadas = 0
        self.elemento = FakeElemento(visible=visible)

    def obtener(self):
        self.llamadas += 1
        return [self.elemento] if self.llamadas >= self.tras else []


class FakeDriver:
    """Driver minimo con find_element(s) por selector (sin Chrome)."""

    def __init__(self):
        self.dialogos = []
        self.por_selector = {}
        self.busquedas = []
        self.find_element_calls = []

    def find_elements(self, by, sel):
        self.busquedas.append(sel)
        if sel == "div[role='dialog']":
            return list(self.dialogos)
        valor = self.por_selector.get(sel)
        if valor is None:
            return []
        if isinstance(valor, PreviewDinamica):
            return valor.obtener()
        return list(valor)

    def find_element(self, by, sel):
        self.find_element_calls.append(sel)
        elementos = self.find_elements(by, sel)
        if not elementos:
            raise NoSuchElementException(f"no existe: {sel}")
        return elementos[0]

    def agregar(self, sel, elementos):
        if isinstance(elementos, PreviewDinamica):
            self.por_selector[sel] = elementos
            return self
        self.por_selector.setdefault(sel, []).extend(elementos)
        return self


class FakeDriverRoto:
    """Driver cuyo `find_elements` siempre falla (renderer caido, etc.)."""

    def find_elements(self, by, sel):
        raise RuntimeError("find_elements roto (test)")

    def find_element(self, by, sel):
        raise RuntimeError("find_element roto (test)")


def bot_con_driver(driver):
    """TwitterBot sin __init__ (no toca BD ni Chrome): solo lo que usa el test."""
    bot = TwitterBot.__new__(TwitterBot)
    bot.driver = driver
    bot.usuario = "cuenta_test"
    bot.ultimo_error = ""
    return bot


def driver_con_input(global_o_dialogo: bool = False):
    """Driver con un input de imagen GLOBAL y, opcional, uno dentro del dialogo."""
    driver = FakeDriver()
    input_global = FakeElemento()
    driver.agregar(SEL_INPUT, [input_global])
    input_dialogo = None
    if global_o_dialogo:
        dlg = FakeElemento()
        input_dialogo = FakeElemento()
        dlg.agregar(SEL_INPUT, [input_dialogo])
        driver.dialogos.append(dlg)
    return driver, input_global, input_dialogo


def _espiar_sleep(registro, dormir_hasta=0.01):
    """`time.sleep` espia que registra el valor pedido y duerme muy poco.

    Permite verificar QUE duerme el bot (poll 0.3s, cortesia 0.2s, sin 1.5s
    fijos) sin pagar los segundos reales.
    """
    original = time.sleep

    def _sleep(segundos):
        registro.append(segundos)
        original(min(float(segundos), dormir_hasta))

    return _sleep


def run(check):
    # ------------------------------------------------------------------ #
    # 1) El input del DIALOGO visible tiene prioridad sobre el global.
    # ------------------------------------------------------------------ #
    driver, input_global, input_dialogo = driver_con_input(True)
    driver.agregar(SEL_PREVIEW, [FakeElemento()])
    bot = bot_con_driver(driver)
    with mock.patch("time.sleep"):
        bot._subir_imagen("foto.png", timeout=0.6)
    check(
        "input dentro del dialogo elegido",
        input_dialogo.enviados == [os.path.abspath("foto.png")],
        str(input_dialogo.enviados),
    )
    check(
        "input global NO se toca si hay dialogo",
        input_global.enviados == [],
        str(input_global.enviados),
    )
    check(
        "send_keys recibe la ruta ABSOLUTA",
        input_dialogo.enviados == [os.path.abspath("foto.png")],
    )

    # ------------------------------------------------------------------ #
    # 2) Dialogo OCULTO -> se ignora y usa el input global (fallback).
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    dlg_oculto = FakeElemento(visible=False)
    input_oculto = FakeElemento()
    dlg_oculto.agregar(SEL_INPUT, [input_oculto])
    driver.dialogos.append(dlg_oculto)
    input_global = FakeElemento()
    driver.agregar(SEL_INPUT, [input_global])
    driver.agregar(SEL_PREVIEW, [FakeElemento()])
    bot = bot_con_driver(driver)
    with mock.patch("time.sleep"):
        bot._subir_imagen("foto.png", timeout=0.6)
    check(
        "dialogo oculto ignorado; se usa el input global",
        input_global.enviados == [os.path.abspath("foto.png")]
        and input_oculto.enviados == [],
        f"global={input_global.enviados} oculto={input_oculto.enviados}",
    )

    # ------------------------------------------------------------------ #
    # 3) Sin dialogo -> selector global historico (compatibilidad).
    # ------------------------------------------------------------------ #
    driver, input_global, _ = driver_con_input(False)
    driver.agregar(SEL_PREVIEW, [FakeElemento()])
    bot = bot_con_driver(driver)
    with mock.patch("time.sleep"):
        bot._subir_imagen("foto.png", timeout=0.6)
    check(
        "sin dialogo cae al selector global",
        input_global.enviados == [os.path.abspath("foto.png")]
        and driver.find_element_calls == [SEL_INPUT],
        str(driver.find_element_calls),
    )

    # ------------------------------------------------------------------ #
    # 4) Input inexistente -> NO lanza y no lanza excepcion.
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    bot = bot_con_driver(driver)
    excepcion = None
    t0 = time.monotonic()
    try:
        bot._subir_imagen("foto.png", timeout=0.6)
    except Exception as e:  # pragma: no cover - solo si el fix se rompe
        excepcion = e
    check(
        "input inexistente no lanza",
        excepcion is None and (time.monotonic() - t0) < 1.0,
        f"excepcion={excepcion!r}",
    )
    check(
        "input inexistente: se intento el selector global",
        SEL_INPUT in driver.find_element_calls,
        str(driver.find_element_calls),
    )

    # ------------------------------------------------------------------ #
    # 5) Vista previa en el 2o poll -> sale antes del timeout y sin 1.5s.
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    driver.agregar(SEL_INPUT, [FakeElemento()])
    dinamica = PreviewDinamica(tras=2)
    driver.agregar(SEL_PREVIEW, dinamica)
    bot = bot_con_driver(driver)
    registro = []
    t0 = time.monotonic()
    with mock.patch("time.sleep", side_effect=_espiar_sleep(registro)):
        bot._subir_imagen("foto.png", timeout=2.0)
    transcurrido = time.monotonic() - t0
    check(
        "vista previa al 2o poll: termina antes del timeout",
        transcurrido < 2.0,
        f"{transcurrido:.3f}s",
    )
    check(
        "vista previa al 2o poll: detectada (2 sondeos)",
        dinamica.llamadas == 2,
        f"llamadas={dinamica.llamadas}",
    )
    check(
        "poll activo de ~0.3s",
        registro and all(0.25 <= s <= 0.35 for s in registro[:-1]),
        str(registro),
    )
    check(
        "cortesia de 0.2s al detectar (sin 1.5s fijos)",
        registro[-1] == 0.2 and 1.5 not in registro,
        str(registro),
    )

    # ------------------------------------------------------------------ #
    # 6) Vista previa que NUNCA aparece y timeout=0.6s -> termina y no lanza.
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    driver.agregar(SEL_INPUT, [FakeElemento()])
    bot = bot_con_driver(driver)
    excepcion = None
    t0 = time.monotonic()
    try:
        bot._subir_imagen("foto.png", timeout=0.6)
    except Exception as e:  # pragma: no cover - solo si el fix se rompe
        excepcion = e
    transcurrido = time.monotonic() - t0
    check(
        "sin vista previa y timeout=0.6 termina sin excepcion",
        excepcion is None and transcurrido < 3.0,
        f"{transcurrido:.2f}s excepcion={excepcion!r}",
    )
    check(
        "sin vista previa: sondeo activo (>=2 comprobaciones)",
        driver.busquedas.count(SEL_PREVIEW) >= 2,
        f"sondeos={driver.busquedas.count(SEL_PREVIEW)}",
    )

    # ------------------------------------------------------------------ #
    # 7) Selectores candidatos de vista previa.
    # ------------------------------------------------------------------ #
    for selector in ("[data-testid='attachments']", "[data-testid='tweetPhoto']"):
        driver = FakeDriver()
        driver.agregar(selector, [FakeElemento()])
        bot = bot_con_driver(driver)
        check(f"vista previa detectada por {selector}", bot._vista_previa_imagen() is True)

    for selector in (
        "[aria-label*='Media']",
        "[aria-label*='multimedia']",
        "[aria-label*='Imagen']",
        "[aria-label*='Image']",
    ):
        driver = FakeDriver()
        driver.agregar(selector, [FakeElemento()])
        bot = bot_con_driver(driver)
        check(f"vista previa detectada por {selector}", bot._vista_previa_imagen() is True)

    # `blob:` SOLO cuenta dentro del dialogo (anti-falso-positivo de perfil).
    driver = FakeDriver()
    driver.agregar(SEL_BLOB, [FakeElemento()])  # foto de perfil global
    bot = bot_con_driver(driver)
    check(
        "blob GLOBAL (foto de perfil) NO cuenta como vista previa",
        bot._vista_previa_imagen() is False,
    )
    dlg = FakeElemento()
    dlg.agregar(SEL_BLOB, [FakeElemento()])  # adjunto dentro del compositor
    driver.dialogos.append(dlg)
    check(
        "blob dentro del dialogo SI cuenta como vista previa",
        bot._vista_previa_imagen() is True,
    )

    # ------------------------------------------------------------------ #
    # 8) Vista previa OCULTA no cuenta; visible si.
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    oculto = FakeElemento(visible=False)
    driver.agregar(SEL_PREVIEW, [oculto])
    bot = bot_con_driver(driver)
    check("vista previa oculta no cuenta", bot._vista_previa_imagen() is False)
    oculto.visible = True
    check("vista previa visible si cuenta", bot._vista_previa_imagen() is True)

    # ------------------------------------------------------------------ #
    # 9) send_keys roto -> no propaga (try/except + log) y termina rapido.
    # ------------------------------------------------------------------ #
    driver = FakeDriver()
    driver.agregar(SEL_INPUT, [FakeElemento(send_keys_lanza=True)])
    bot = bot_con_driver(driver)
    excepcion = None
    t0 = time.monotonic()
    try:
        bot._subir_imagen("foto.png", timeout=0.6)
    except Exception as e:  # pragma: no cover - solo si el fix se rompe
        excepcion = e
    check(
        "send_keys roto no propaga excepcion",
        excepcion is None and (time.monotonic() - t0) < 1.0,
        f"excepcion={excepcion!r}",
    )

    # ------------------------------------------------------------------ #
    # 10) Driver roto -> los helpers devuelven False/None sin lanzar.
    # ------------------------------------------------------------------ #
    bot = bot_con_driver(FakeDriverRoto())
    check("driver roto: _vista_previa_imagen no lanza", bot._vista_previa_imagen() is False)
    check(
        "driver roto: _buscar_input_imagen no lanza",
        bot._buscar_input_imagen() is None,
    )
    excepcion = None
    try:
        bot._subir_imagen("foto.png", timeout=0.6)
    except Exception as e:  # pragma: no cover - solo si el fix se rompe
        excepcion = e
    check("driver roto: _subir_imagen no lanza", excepcion is None)

    # ------------------------------------------------------------------ #
    # 11) timeout: firma final y casos limite.
    # ------------------------------------------------------------------ #
    firma = inspect.signature(TwitterBot._subir_imagen)
    parametros = list(firma.parameters)
    check(
        "firma: (self, imagen_path, timeout=15.0)",
        parametros == ["self", "imagen_path", "timeout"]
        and firma.parameters["timeout"].default == 15.0
        and firma.parameters["imagen_path"].default is inspect.Parameter.empty,
        f"{parametros} default={firma.parameters['timeout'].default}",
    )

    driver, _, _ = driver_con_input(False)
    driver.agregar(SEL_PREVIEW, [FakeElemento()])
    bot = bot_con_driver(driver)
    excepcion = None
    try:
        with mock.patch("time.sleep"):
            bot._subir_imagen("foto.png", timeout=None)
    except Exception as e:  # pragma: no cover - solo si el fix se rompe
        excepcion = e
    check("timeout=None usa el default sin romper", excepcion is None)

    driver = FakeDriver()
    bot = bot_con_driver(driver)
    t0 = time.monotonic()
    resultado = bot._esperar_vista_previa_imagen(0)
    check(
        "timeout<=0: UNA comprobacion inmediata (sin espera)",
        resultado is False and (time.monotonic() - t0) < 0.5,
        f"{time.monotonic() - t0:.3f}s",
    )


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_tests import check as _check, resumen as _resumen  # noqa: E402

    print("=== test_imagenes_selenium.py ===")
    run(_check)
    sys.exit(_resumen())
