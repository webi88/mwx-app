import re
from typing import Optional

import requests
from loguru import logger


def acortar_url(url: str) -> Optional[str]:
    """Acorta una URL con is.gd. Si algo falla, devuelve la URL original
    (nunca None) para no perder el enlace en la alerta."""
    if not url:
        return url
    if len(url) < 30:
        return url
    try:
        response = requests.get(
            "https://is.gd/create.php",
            params={"format": "json", "url": url},
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            corta = data.get("shorturl")
            if corta:
                return corta
        return url

    except Exception as e:
        logger.debug(f"Error acortando URL: {e}")
        return url


def resolver_url_google_news(url_google_news: str) -> Optional[str]:
    """Resuelve la URL real de un enlace de Google News.

    Orden de intentos (el viejo usaba solo googlenewsdecoder):
      1. ``googlenewsdecoder`` (paquete usado por el proyecto viejo) — soporta
         los links modernos ``/rss/articles/CBMi...``.
      2. Parametro legado ``?url=`` (feeds antiguos).
      3. HEAD siguiendo redirecciones, si sale de news.google.com.
      4. Fallback: la URL original (nunca None).
    """
    if not url_google_news:
        return url_google_news
    if "news.google.com" not in url_google_news:
        return url_google_news

    # 1) Decodificador real de Google News (tolerante a que no este instalado).
    try:
        from googlenewsdecoder import gnewsdecoder

        decoded = gnewsdecoder(url_google_news)
        if decoded and decoded.get("decoded_url"):
            return decoded["decoded_url"]
    except Exception as e:
        logger.debug(f"googlenewsdecoder no disponible o fallo: {e}")

    # 2) Formato legado.
    match = re.search(r"[?&]url=([^&]+)", url_google_news)
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1))

    # 3) HEAD con redirecciones.
    try:
        response = requests.head(url_google_news, allow_redirects=True, timeout=8)
        final = response.url
        if final and "news.google.com" not in final:
            return final
    except Exception as e:
        logger.debug(f"HEAD resolviendo URL de Google News fallo: {e}")

    return url_google_news
