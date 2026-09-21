import requests
import re
from typing import Optional
from loguru import logger


def acortar_url(url: str) -> Optional[str]:
    try:
        response = requests.get(
            f"https://is.gd/create.php",
            params={"format": "json", "url": url},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            return data.get("shorturl")
        
        return url
    
    except Exception as e:
        logger.error(f"Error acortando URL: {e}")
        return url


def resolver_url_google_news(url_google_news: str) -> Optional[str]:
    try:
        match = re.search(r"url=([^&]+)", url_google_news)
        if match:
            from urllib.parse import unquote
            return unquote(match.group(1))
        
        response = requests.head(url_google_news, allow_redirects=True, timeout=10)
        return response.url
    
    except Exception as e:
        logger.error(f"Error resolviendo URL: {e}")
        return None
