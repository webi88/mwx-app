"""Smoke REAL con Chrome (se corre a mano, NO lo ejecuta `run_tests.py`).

Comprueba contra el HTML local `tests/editor_mask.html` los dos arreglos que en
Railway dependian del navegador de verdad:

  1. `_primer_editor_visible` elige el editor NO tapado por el overlay
     `data-testid='mask'` (el compositor inline viejo).
  2. `_pegar_texto` escribe por CDP (`Input.insertText`) en un contenteditable
     DENTRO de un wrapper no editable y tapado por el mask, REEMPLAZANDO su
     contenido y sin tocar el otro editor.

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

        # P0-A real: CDP insertText en el wrapper (editor tapado por el mask).
        wrapper = driver.find_element("id", "wrapper")
        bot._pegar_texto(wrapper, "TEXTO NUEVO CDP")
        editor = driver.find_element("id", "editor")
        contenido = (editor.get_attribute("textContent") or "").strip()
        check(
            "Input.insertText reemplaza el contenido del editor",
            contenido == "TEXTO NUEVO CDP",
            repr(contenido),
        )
        check("wrapper.text confirma el texto", "TEXTO NUEVO CDP" in (wrapper.text or ""))

        # El editor #otro no se toco.
        otro = driver.find_element("id", "otro")
        contenido_otro = (otro.get_attribute("textContent") or "").strip()
        check(
            "no se escribio en otro editor",
            contenido_otro == "contenido",
            repr(contenido_otro),
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
