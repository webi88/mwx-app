"""Smoke REAL con Chrome (se corre a mano, NO lo ejecuta `run_tests.py`).

Comprueba contra el HTML local `tests/editor_mask.html` el nuevo contrato
anti-Ghostban del pegado de texto:

  1. `_primer_editor_visible` elige el editor NO tapado por el overlay
     `data-testid='mask'` (el compositor inline viejo).
  2. `_pegar_texto` DESTRUYE el mask y escribe con eventos REALES de teclado:
     `editor.click()` (best effort) + `editor.send_keys(texto)` en UNA llamada,
     reemplazando el contenido previo ("VIEJO") y sin tocar el otro editor.
  3. Los listeners de `keydown`/`input` del editor confirman que Chrome genero
     eventos de teclado reales (lo que X exige para no ocultar los posts).
  4. La variante wrapper no editable (comun en X) tambien escribe el texto
     (send_keys o fallback ActionChains sobre el editable real enfocado por JS).

Uso:
    .venv/Scripts/python.exe tests/smoke_chrome_cdp.py

Requiere Chrome instalado (Chrome 153 probado). Cierra Chrome (`driver.quit()`)
siempre, incluso si un check falla. Nunca se registra en la suite rapida porque
arranca un navegador real.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("HEADLESS", "true")

from selenium.webdriver.chrome.options import Options  # noqa: E402

from plataformas.chrome_driver import crear_chrome  # noqa: E402
from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402

HTML = Path(__file__).resolve().parent / "editor_mask.html"


def main() -> int:
    if not HTML.is_file():
        print(f"  FAIL falta el HTML del smoke: {HTML}")
        print("\nRESULTADO SMOKE: 0/1")
        return 1

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1200,800")
    options.add_argument("--no-first-run")

    driver = None
    total = 0
    ok = 0

    def check(nombre, cond, extra=""):
        nonlocal total, ok
        total += 1
        if cond:
            ok += 1
            print(f"  PASS {nombre} {extra}".rstrip())
        else:
            print(f"  FAIL {nombre} {extra}".rstrip())

    try:
        driver = crear_chrome(options)
        print(
            f"Chrome {driver.capabilities.get('browserVersion', '?')} "
            f"(headless) listo"
        )
        driver.get(HTML.as_uri())
        bot = TwitterBot.__new__(TwitterBot)
        bot.driver = driver
        bot.ultimo_error = ""

        # P0-C real: con el mask encima del primer editor, elige el NO ocluido.
        elegido = bot._primer_editor_visible()
        idd = elegido.get_attribute("id") if elegido is not None else None
        check("primer editor visible = #otro (no ocluido)", idd == "otro", f"id={idd}")

        # Escenario real: mover el wrapper/editor DEBAJO del mask (en el HTML el
        # mask no lo cubre; en X el modal si tapa el compositor).
        wrapper = driver.find_element("id", "wrapper")
        driver.execute_script(
            "document.getElementById('wrapper').style.top='0px';"
            "document.getElementById('wrapper').style.left='0px';"
        )
        encima = driver.execute_script(
            "const r = document.getElementById('editor').getBoundingClientRect();"
            "const el = document.elementFromPoint("
            "r.left + r.width / 2, r.top + r.height / 2);"
            "return el ? (el.getAttribute('data-testid') || el.id || el.tagName) : '';"
        )
        check(
            "escenario: el mask cubre el editor (clic interceptado)",
            encima == "mask",
            f"encima={encima}",
        )
        check(
            "escenario: el mask existe antes del pegado",
            len(driver.find_elements("css selector", "[data-testid='mask']")) == 1,
        )

        # Listeners de teclado REALES sobre el editor (prueba del Ghostban).
        driver.execute_script(
            "window.__teclado = {keydown: 0, input: 0};"
            "const ed = document.getElementById('editor');"
            "ed.addEventListener('keydown', () => window.__teclado.keydown++);"
            "ed.addEventListener('input', () => window.__teclado.input++);"
        )

        # P0 real: `_pegar_texto` destruye el mask y escribe el contenteditable
        # (#editor) con click + send_keys reales, reemplazando "VIEJO".
        editor = driver.find_element("id", "editor")
        texto = "TEXTO CON TECLADO REAL"
        bot._pegar_texto(editor, texto)
        contenido = (editor.get_attribute("textContent") or "").strip()
        check(
            "mask: ya no esta en el DOM tras el pegado",
            len(driver.find_elements("css selector", "[data-testid='mask']")) == 0,
        )
        check(
            "send_keys: reemplaza el contenido previo ('VIEJO')",
            contenido == texto,
            repr(contenido),
        )
        teclado = driver.execute_script("return window.__teclado;") or {}
        check(
            "eventos reales: keydown del editor",
            int(teclado.get("keydown", 0)) >= len(texto),
            str(teclado),
        )
        check(
            "eventos reales: input del editor",
            int(teclado.get("input", 0)) >= 1,
            str(teclado),
        )

        # El editor #otro no se toco.
        otro = driver.find_element("id", "otro")
        contenido_otro = (otro.get_attribute("textContent") or "").strip()
        check(
            "no se escribio en otro editor",
            contenido_otro == "contenido",
            repr(contenido_otro),
        )

        # Variante wrapper no editable (comun en X) con mask RE-MONTADO: se
        # eliminan los otros editores para que la re-localizacion no tenga
        # candidatos y entre el FALLBACK ActionChains sobre el editable real.
        driver.execute_script(
            "for (const id of ['tapado', 'otro']) {"
            "  const e = document.getElementById(id); if (e) { e.remove(); }"
            "}"
            "const m = document.createElement('div');"
            "m.setAttribute('data-testid', 'mask');"
            "m.style.cssText = 'position:fixed;left:0;top:0;width:300px;"
            "height:300px;z-index:9999;';"
            "document.body.appendChild(m);"
            "document.getElementById('editor').textContent = 'VIEJO';"
        )
        bot._pegar_texto(wrapper, "TEXTO FALLBACK WRAPPER")
        contenido2 = (editor.get_attribute("textContent") or "").strip()
        check(
            "wrapper: el mask se destruyo de nuevo",
            len(driver.find_elements("css selector", "[data-testid='mask']")) == 0,
        )
        check(
            "wrapper: el texto nuevo queda en el editor (send_keys/fallback)",
            contenido2 == "TEXTO FALLBACK WRAPPER",
            repr(contenido2),
        )
    except Exception as e:  # noqa: BLE001
        check("Chrome real ejecuto el smoke sin errores", False, f"{type(e).__name__}: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    print(f"\nRESULTADO SMOKE: {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
