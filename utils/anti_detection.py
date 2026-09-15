"""Helpers de anti-deteccion compartidos por todos los bots Selenium.

Centraliza:
- El script JS de stealth (webdriver, plugins, languages, chrome, WebGL...).
- La aplicacion del UA EXACTO de cada cuenta por CDP
  (`Emulation.setUserAgentOverride`) + `userAgentMetadata` coherente con la
  version de Chrome del UA.
- La normalizacion de cookies de formatos de vendedor/EditThisCookie al
  formato que acepta Selenium (`name/value/domain/path/secure/httpOnly` y,
  cuando aplica, `expiry`/`sameSite`).

Regla de oro: NINGUNA funcion lanza excepciones. Si algo falla se loguea y se
devuelve un valor neutro para no tumbar el flujo del bot.
"""

import json
import os
import re
from typing import Optional

from loguru import logger

from core.config import resolver_ruta


ACCEPT_LANGUAGE = "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7"
RUTA_UA_GLOBAL = "data/perfiles_chrome/ua_config.txt"

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['es-MX', 'es', 'en-US', 'en']});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return _getParameter.call(this, parameter);
};
"""


def stealth_script() -> str:
    """Devuelve el JS de stealth (el mismo que antes estaba embebido en TwitterBot)."""
    return _STEALTH_JS


def aplicar_stealth(driver) -> bool:
    """Inyecta el JS de stealth en cada documento nuevo via CDP.

    Devuelve True si el CDP lo acepto; False (sin lanzar) si el driver no
    soporta `execute_cdp_cmd` o el comando fallo."""
    if driver is None:
        return False
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": stealth_script()},
        )
        return True
    except Exception as e:
        logger.warning(f"aplicar_stealth fallo: {e}")
        return False


def plataforma_desde_ua(ua: str) -> str:
    """Deriva la plataforma del UA: Windows, macOS, Linux, Android, iOS o ''."""
    try:
        ua = ua or ""
        if not ua:
            return ""
        # El orden importa: Android contiene "Linux" y iPhone contiene "Mac OS X".
        if "Android" in ua:
            return "Android"
        if "iPhone" in ua or "iPad" in ua or "iPod" in ua:
            return "iOS"
        if "Macintosh" in ua or "Mac OS X" in ua:
            return "macOS"
        if "Windows" in ua:
            return "Windows"
        if "Linux" in ua or "X11" in ua or "CrOS" in ua:
            return "Linux"
        return ""
    except Exception:
        return ""


def chrome_version_desde_ua(ua: str) -> Optional[int]:
    """Version mayor de Chrome del UA (`Chrome/152.0.0.0` -> 152) o None."""
    try:
        m = re.search(r"(?:Chrome|Chromium|CriOS)/(\d+)", ua or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _chrome_version_completa(ua: str) -> str:
    try:
        m = re.search(r"(?:Chrome|Chromium|CriOS)/([\d.]+)", ua or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def _platform_navegador(plataforma: str) -> str:
    return {
        "Windows": "Win32",
        "macOS": "MacIntel",
        "Linux": "Linux x86_64",
        "Android": "Linux armv81",
        "iOS": "iPhone",
    }.get(plataforma, "")


def _platform_version(ua: str, plataforma: str) -> str:
    try:
        if plataforma == "Windows":
            return "10.0.0"
        if plataforma == "macOS":
            m = re.search(r"Mac OS X (\d+)[_.](\d+)(?:[_.](\d+))?", ua or "")
            if m:
                partes = [m.group(1), m.group(2)]
                if m.group(3):
                    partes.append(m.group(3))
                return ".".join(partes)
            return "10.15.7"
        if plataforma == "Android":
            m = re.search(r"Android (\d+(?:\.\d+)*)", ua or "")
            return m.group(1) if m else ""
        if plataforma == "iOS":
            m = re.search(r"(?:CPU iPhone OS|CPU OS) (\d+)[_.](\d+)", ua or "")
            return f"{m.group(1)}.{m.group(2)}" if m else ""
        return ""
    except Exception:
        return ""


def _architecture(plataforma: str) -> str:
    if plataforma in ("Windows", "macOS", "Linux"):
        return "x86"
    if plataforma == "iOS":
        return "arm"
    return ""


def _model(ua: str, plataforma: str) -> str:
    try:
        if plataforma != "Android":
            return ""
        m = re.search(r"Android [\d.]+;\s*([^;)]+)\)", ua or "")
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _metadata_ua(ua: str, mobile: bool = False) -> dict:
    """Construye `userAgentMetadata` coherente con la version de Chrome del UA."""
    version = chrome_version_desde_ua(ua)
    completa = _chrome_version_completa(ua)
    plataforma = plataforma_desde_ua(ua)
    brands = [
        {"brand": "Not_A Brand", "version": "8"},
        {"brand": "Chromium", "version": str(version) if version else ""},
        {"brand": "Google Chrome", "version": str(version) if version else ""},
    ]
    metadata = {
        "brands": brands,
        "platform": plataforma or "Windows",
        "platformVersion": _platform_version(ua, plataforma),
        "architecture": _architecture(plataforma),
        "model": _model(ua, plataforma),
        "mobile": bool(mobile),
    }
    if completa:
        metadata["fullVersion"] = completa
        metadata["fullVersionList"] = [
            {"brand": "Not_A Brand", "version": "8"},
            {"brand": "Chromium", "version": completa},
            {"brand": "Google Chrome", "version": completa},
        ]
    return metadata


def aplicar_user_agent(driver, ua: str, mobile: bool = False) -> bool:
    """Fija el UA EXACTO de la cuenta (y su metadata) sobre el driver abierto.

    Usa `Emulation.setUserAgentOverride` con `userAgent`, `platform`,
    `acceptLanguage` y `userAgentMetadata` (brands coherentes con la version de
    Chrome del UA, plataforma derivada, `mobile=False`). Si el CDP falla,
    devuelve False sin romper (queda el UA lanzado por `--user-agent=`)."""
    if driver is None or not (ua or "").strip():
        return False
    ua = ua.strip()
    try:
        plataforma = plataforma_desde_ua(ua)
        driver.execute_cdp_cmd(
            "Emulation.setUserAgentOverride",
            {
                "userAgent": ua,
                "platform": _platform_navegador(plataforma),
                "acceptLanguage": ACCEPT_LANGUAGE,
                "userAgentMetadata": _metadata_ua(ua, mobile=mobile),
            },
        )
        return True
    except Exception as e:
        logger.warning(f"aplicar_user_agent fallo: {e}")
        return False


_SAMESITE_MAP = {
    "no_restriction": "None",
    "no-restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": None,
}


def _a_bool(valor, default: bool = False) -> bool:
    if valor is None:
        return default
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    if texto in ("true", "1", "yes", "si", "sí"):
        return True
    if texto in ("false", "0", "no", ""):
        return False
    return default


def normalizar_cookie(cookie) -> Optional[dict]:
    """Normaliza una cookie de formato vendedor/EditThisCookie/Selenium.

    Acepta `Name`/`name`, `Value`/`value`, `expirationDate` (float -> `expiry`
    int), `sameSite` ("no_restriction"->"None", "lax"->"Lax",
    "strict"->"Strict", "unspecified" se omite). Ignora `hostOnly`, `session`,
    `storeId` e `id`. Si `session=True` o el expiry es <= 0, se omite `expiry`.
    Devuelve None si falta `name` o `value` (o estan vacios)."""
    try:
        if not isinstance(cookie, dict):
            return None

        name = cookie.get("name", cookie.get("Name"))
        value = cookie.get("value", cookie.get("Value"))
        if name is None or value is None:
            return None

        name = str(name).strip()
        if not name:
            return None

        value = value if isinstance(value, str) else str(value)
        if value == "":
            return None

        salida = {
            "name": name,
            "value": value,
            "domain": str(
                cookie.get("domain") or cookie.get("Domain") or ".x.com"
            ).strip() or ".x.com",
            "path": str(
                cookie.get("path") or cookie.get("Path") or "/"
            ).strip() or "/",
            "secure": _a_bool(cookie.get("secure", cookie.get("Secure")), True),
            "httpOnly": _a_bool(
                cookie.get("httpOnly", cookie.get("HttpOnly")), False
            ),
        }

        # Expiry: `expirationDate` (formato vendedor) o `expiry` (Selenium).
        session = _a_bool(cookie.get("session"), False)
        expira = cookie.get(
            "expiry",
            cookie.get("expirationDate", cookie.get("ExpirationDate")),
        )
        if not session and expira is not None:
            try:
                expira_int = int(float(expira))
            except (TypeError, ValueError):
                expira_int = None
            if expira_int and expira_int > 0:
                salida["expiry"] = expira_int

        same_site = cookie.get("sameSite", cookie.get("SameSite"))
        if same_site is not None:
            normalizado = _SAMESITE_MAP.get(str(same_site).strip().lower())
            if normalizado:
                salida["sameSite"] = normalizado

        return salida
    except Exception as e:
        logger.debug(f"Cookie no normalizable: {e}")
        return None


def normalizar_cookies(lista) -> list:
    """Normaliza una lista de cookies e ignora las invalidas.

    Acepta tambien un string JSON (por comodidad) y nunca lanza."""
    try:
        if isinstance(lista, str):
            try:
                lista = json.loads(lista)
            except Exception:
                return []
        if not isinstance(lista, (list, tuple)):
            return []
        salida = []
        for cookie in lista:
            normalizada = normalizar_cookie(cookie)
            if normalizada:
                salida.append(normalizada)
        return salida
    except Exception as e:
        logger.warning(f"normalizar_cookies fallo: {e}")
        return []


def resolver_ua_cuenta(cuenta) -> str:
    """UA que debe usar una cuenta.

    Orden: `Cuenta.user_agent` (viene en el lote) -> archivo global
    `data/perfiles_chrome/ua_config.txt` (override manual) -> "" (dejar el UA
    natural del navegador). Nunca genera ni persiste UAs aleatorios."""
    try:
        ua = ""
        if isinstance(cuenta, dict):
            ua = cuenta.get("user_agent") or ""
        else:
            ua = getattr(cuenta, "user_agent", "") or ""
        ua = str(ua).strip()
        if ua:
            return ua

        ruta = resolver_ruta(RUTA_UA_GLOBAL)
        if os.path.exists(ruta):
            with open(ruta, "r", encoding="utf-8") as f:
                ua = f.read().strip()
            if ua:
                return ua
    except Exception as e:
        logger.warning(f"No se pudo resolver el user_agent de la cuenta: {e}")
    return ""
