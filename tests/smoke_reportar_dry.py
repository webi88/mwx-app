"""Smoke REAL con Chrome de `reportar_post`/`reportar_cuenta` en modo dry-run.

Manual (NO lo ejecuta `run_tests.py`: abre Chrome de verdad). Verifica con UNA
cuenta real de la flota (`data/cookies/twitter/*.pkl`) que el flujo de reporte
LLEGA hasta el modal de X y hasta el boton de envio, SIN ENVIAR NADA
(`dry_run=True` jamas pulsa Submit: cierra con Escape/boton cerrar).

Imprime los TEXTOS REALES de las opciones del modal (EN/ES) para calibrar el
mapping de motivos, y los de la opcion del menu "Report..." del tweet/perfil.

Uso:
    .venv/Scripts/python.exe tests/smoke_reportar_dry.py
    SMOKE_USUARIO=OtraCuenta .venv/Scripts/python.exe tests/smoke_reportar_dry.py

Requiere Chrome instalado. `TWITTER_SIN_PROXY=1` por defecto (los smokes
previos del repo corren sin proxy); para usar la sesion sticky de la cuenta,
exportar `TWITTER_SIN_PROXY=0`. Cierra Chrome (`bot.cerrar()`) SIEMPRE, incluso
si un check falla. PROHIBIDO ENVIAR: el script solo usa `dry_run=True`.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TWITTER_SIN_PROXY", "1")

from loguru import logger  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402


def _mensajes_modal(capturas: list) -> list:
    """Frases 'opciones VISIBLES del modal: ...' capturadas del log."""
    frases = []
    for mensaje in capturas:
        texto = mensaje.record["message"]
        if "opciones VISIBLES del modal" in texto:
            frases.append(texto)
    return frases


def _urls_de_tweets(driver) -> list:
    """URLs de tweets publicos visibles en la pagina actual (sin duplicados)."""
    urls: list = []
    try:
        for articulo in driver.find_elements(
            By.CSS_SELECTOR, "article[data-testid='tweet']"
        ):
            for enlace in articulo.find_elements(By.CSS_SELECTOR, "a[href*='/status/']"):
                href = (enlace.get_attribute("href") or "").split("?")[0]
                if re.search(r"/status/\d+$", href) and href not in urls:
                    urls.append(href)
    except Exception:
        pass
    return urls


def _buscar_tweets(bot) -> list:
    """Tweets publicos del timeline /home (con polling) o, si falla, de un
    perfil publico de respaldo (`SMOKE_PERFIL_FALLBACK`)."""
    urls: list = []
    for intento in range(4):
        try:
            bot.driver.get("https://x.com/home")
        except Exception:
            pass
        if bot._esperar_article_tweet(timeout=8):
            urls = _urls_de_tweets(bot.driver)
        if urls:
            return urls
        time.sleep(2)
    perfil = (os.environ.get("SMOKE_PERFIL_FALLBACK") or "ClaraBrugadaM").strip()
    try:
        bot.driver.get(f"https://x.com/{perfil}")
        bot._esperar_article_tweet(timeout=15)
        urls = _urls_de_tweets(bot.driver)
    except Exception:
        pass
    return urls


def main() -> int:
    usuario = (os.environ.get("SMOKE_USUARIO") or "4T_puntodos").strip()
    print(f"SMOKE reportar dry-run con @{usuario} (headless={os.environ.get('HEADLESS')}, "
          f"sin_proxy={os.environ.get('TWITTER_SIN_PROXY')})")

    capturas: list = []
    sink = logger.add(lambda m: capturas.append(m), level="INFO")
    bot = TwitterBot(usuario)
    total = 0
    pasados = 0

    def check(nombre, cond, extra=""):
        nonlocal total, pasados
        total += 1
        pasados += bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'} {nombre} {extra}".rstrip(), flush=True)

    try:
        check("login con cookies (.pkl)", bot.login_con_cookies(), bot.ultimo_error or "")

        # Tweets publicos y ESTABLES del timeline (nunca un post propio: X no deja
        # reportar los propios). Se intentan varios hasta que uno funcione.
        urls = _buscar_tweets(bot)
        check("tweets publicos encontrados", bool(urls), f"({len(urls)}) {urls[:3]}")

        tweet_ok = ""
        inicio_capturas = len(capturas)
        for url in urls[:5]:
            if bot.reportar_post(url, "spam", dry_run=True):
                tweet_ok = url
                break
        check(
            "reportar_post(dry_run=True) llego al envio",
            bool(tweet_ok),
            tweet_ok or f"ultimo_error={bot.ultimo_error}",
        )

        # Opciones REALES del modal del tweet (para el informe de calibracion).
        for frase in _mensajes_modal(capturas[inicio_capturas:]):
            print(f"  [tweet] {frase}")

        perfil_ok = False
        inicio_capturas = len(capturas)
        if tweet_ok:
            m = re.search(r"x\.com/([^/]+)/status/", tweet_ok)
            handle = m.group(1) if m else ""
            if handle:
                perfil_ok = bot.reportar_cuenta(
                    f"https://x.com/{handle}", "spam", dry_run=True
                )
        check(
            "reportar_cuenta(dry_run=True) llego al envio",
            bool(perfil_ok),
            "" if perfil_ok else f"ultimo_error={bot.ultimo_error}",
        )
        for frase in _mensajes_modal(capturas[inicio_capturas:]):
            print(f"  [cuenta] {frase}")

        # El script deja claro (por si alguien lo corre sin leer el docstring)
        # que NO se envio ningun reporte.
        print("  (dry-run: NINGUN reporte fue enviado; solo se abrieron menus/modales)")
    except Exception as e:
        print(f"  FAIL smoke con excepcion: {type(e).__name__}: {e}")
        total += 1
    finally:
        try:
            bot.cerrar()
        except Exception:
            pass
        logger.remove(sink)

    print(f"\nRESULTADO SMOKE: {pasados}/{total}")
    return 0 if pasados == total else 1


if __name__ == "__main__":
    sys.exit(main())
