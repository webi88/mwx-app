"""Cliente CapSolver para Cloudflare Turnstile / interstitial challenge.

Nunca lanza; todos los errores vuelven en el dict de resultado.

Contrato congelado:

    api_key() -> str
        env `CAPSOLVER_API_KEY` (strip); "" si no esta definida.

    disponible() -> bool
        bool(api_key()).

    estado() -> dict
        {"activo": bool, "proveedor": "capsolver", "motivo": str}

    resolver_turnstile(url, sitekey, *, proxy="", action="", cdata="",
                       chl_page_data="", timeout_seg=None) -> dict
        {"ok","token","error","segundos","task_id","usado_proxy"}
        - Con `proxy` normalizable (http/https/socks5 o `host:port:user:pass`)
          usa `AntiTurnstileTask`; sin proxy usa `AntiTurnstileTaskProxyLess`.
        - `metadata` solo incluye los campos no vacios (action/cdata/
          chlPageData).
        - Polling cada `CAPSOLVER_POLL_SEG` (default 3) hasta
          `CAPSOLVER_TIMEOUT_SEG` (default 120; `timeout_seg` lo pisa).
        - `status == "ready"` -> ok con `solution.token`.
        - `errorId != 0` / `errorCode` -> ok=False con mensaje legible (incluye
          el codigo). Timeouts/red/excepciones -> ok=False. NUNCA lanza.
        - `segundos` = duracion total real.

    resolver_challenge_cloudflare(url, *, proxy="", timeout_seg=None) -> dict
        {"ok","cookies","user_agent","error","segundos","task_id"}
        - `AntiCloudflareTask` EXIGE proxy: sin proxy usable devuelve ok=False
          con "requiere proxy" (sin llamar a la API).
        - La solucion trae `cookies` (p.ej. cf_clearance) y `userAgent`; se
          normalizan a dict/str.

API CapSolver: POST https://api.capsolver.com/createTask y /getTaskResult.
La API key NO se imprime jamas en los logs ni viaja en los resultados.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import unquote

from loguru import logger

__all__ = [
    "api_key",
    "disponible",
    "estado",
    "resolver_turnstile",
    "resolver_challenge_cloudflare",
]

_URL_API = "https://api.capsolver.com"
_TIMEOUT_SEG_DEFAULT = 120
_POLL_SEG_DEFAULT = 3
_HTTP_TIMEOUT_SEG = 20

# `.env` del proyecto: fallback para procesos que NO cargan dotenv en
# `os.environ` (p. ej. el dashboard de Streamlit local). Monkeypatcheable.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_FORMATOS_PROXY = ("http://", "https://", "socks5://", "socks4://", "socks://")


# --------------------------------------------------------------------------- #
# Configuracion (env, nunca lanza)
# --------------------------------------------------------------------------- #
def _leer_env_archivo(nombre: str) -> str:
    """Valor de `nombre` en el `.env` del proyecto ("" si falta).

    Parser minimo KEY=VALUE (comentarios `#`, `export` y comillas opcionales);
    se usa SOLO como fallback cuando `os.environ` no trae la variable. Nunca
    lanza.
    """
    try:
        ruta = _ENV_FILE
        if ruta is None or not Path(ruta).is_file():
            return ""
        for linea in Path(ruta).read_text(encoding="utf-8", errors="ignore").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            clave = clave.strip()
            if clave.startswith("export "):
                clave = clave[len("export "):].strip()
            if clave != nombre:
                continue
            return valor.strip().strip("'\"")
    except Exception:
        return ""
    return ""


def api_key() -> str:
    """API key de CapSolver: `CAPSOLVER_API_KEY` (env; fallback al `.env`)."""
    try:
        valor = str(os.environ.get("CAPSOLVER_API_KEY", "") or "").strip()
    except Exception:
        valor = ""
    if valor:
        return valor
    return _leer_env_archivo("CAPSOLVER_API_KEY").strip()


def disponible() -> bool:
    """True si hay API key configurada (el solucionador puede intentarse)."""
    return bool(api_key())


def estado() -> dict:
    """Estado legible del solucionador para la UI (sin exponer la key)."""
    if disponible():
        motivo = "API key configurada (CAPSOLVER_API_KEY)"
        activo = True
    else:
        motivo = (
            "sin CAPSOLVER_API_KEY: define la variable para resolver captchas "
            "automaticamente"
        )
        activo = False
    return {"activo": activo, "proveedor": "capsolver", "motivo": motivo}


def _env_int(nombre: str, default: int) -> int:
    """Entero positivo desde el entorno (default si falta o es invalido)."""
    try:
        valor = int(float(str(os.environ.get(nombre, "")).strip()))
    except (TypeError, ValueError):
        return default
    return max(1, valor)


def _timeout_seg(timeout_seg=None) -> int:
    """Timeout del polling: kwarg > `CAPSOLVER_TIMEOUT_SEG` > 120."""
    if timeout_seg is not None:
        try:
            valor = int(float(timeout_seg))
            if valor > 0:
                return valor
        except (TypeError, ValueError):
            pass
    return _env_int("CAPSOLVER_TIMEOUT_SEG", _TIMEOUT_SEG_DEFAULT)


def _poll_seg() -> int:
    """Intervalo entre sondeos: `CAPSOLVER_POLL_SEG` (default 3)."""
    return _env_int("CAPSOLVER_POLL_SEG", _POLL_SEG_DEFAULT)


# --------------------------------------------------------------------------- #
# HTTP (requests -> httpx -> urllib; nunca lanza)
# --------------------------------------------------------------------------- #
def _post_json(ruta: str, payload: dict, timeout: int = _HTTP_TIMEOUT_SEG):
    """POST JSON a CapSolver. Devuelve `(ok, datos, error)`; nunca lanza."""
    url = _URL_API + str(ruta or "")
    errores = []
    for transporte in ("requests", "httpx", "urllib"):
        try:
            if transporte == "requests":
                import requests

                respuesta = requests.post(url, json=payload, timeout=timeout)
                codigo = int(getattr(respuesta, "status_code", 0) or 0)
                datos = respuesta.json()
            elif transporte == "httpx":
                import httpx

                respuesta = httpx.post(url, json=payload, timeout=timeout)
                codigo = int(getattr(respuesta, "status_code", 0) or 0)
                datos = respuesta.json()
            else:
                import urllib.request

                peticion = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
                    codigo = int(getattr(respuesta, "status", 0) or 200)
                    datos = json.loads(respuesta.read().decode("utf-8") or "{}")
            if not isinstance(datos, dict):
                return False, {}, f"CapSolver respondio un JSON inesperado (HTTP {codigo})"
            if codigo >= 400 and int(datos.get("errorId") or 0) == 0:
                return False, {}, f"CapSolver HTTP {codigo}"
            return True, datos, ""
        except Exception as e:
            errores.append(f"{transporte}: {type(e).__name__}: {e}")
    return False, {}, "CapSolver no respondio (" + "; ".join(errores[:2]) + ")"


def _error_api(datos: dict) -> str:
    """Mensaje legible de error de CapSolver ("" si `errorId` es 0)."""
    try:
        if int(datos.get("errorId") or 0) == 0:
            return ""
    except (TypeError, ValueError):
        return ""
    codigo = str(datos.get("errorCode") or "").strip()
    descripcion = str(datos.get("errorDescription") or "").strip()
    if codigo and descripcion:
        return f"CapSolver error {codigo}: {descripcion}"
    if descripcion:
        return f"CapSolver: {descripcion}"
    if codigo:
        return f"CapSolver error {codigo}"
    return "CapSolver devolvio un error desconocido"


# --------------------------------------------------------------------------- #
# Proxy (normalizacion para las tareas con proxy)
# --------------------------------------------------------------------------- #
def _normalizar_proxy(proxy) -> dict:
    """Normaliza un proxy a `{scheme,host,port,user,password}` ({} si no se puede).

    Acepta `http://user:pass@host:port`, `socks5://...`, `user:pass@host:port`,
    `host:port:user:pass` (formato del repo) y `host:port` sin credenciales.
    Solo http/https (-> "http") y socks5 son utilizables por CapSolver.
    Nunca lanza.
    """
    texto = str(proxy or "").strip()
    if not texto:
        return {}
    try:
        scheme = "http"
        for prefijo in _FORMATOS_PROXY:
            if texto.lower().startswith(prefijo):
                scheme = prefijo[:-3].lower()
                texto = texto[len(prefijo):]
                break
        user = password = ""
        if "@" in texto:
            credenciales, host_puerto = texto.rsplit("@", 1)
            if ":" in credenciales:
                user, password = credenciales.split(":", 1)
            else:
                user = credenciales
        else:
            host_puerto = texto
            partes = host_puerto.split(":")
            if len(partes) == 4:
                if partes[1].isdigit():
                    # host:port:user:pass (formato del repo).
                    host_puerto = f"{partes[0]}:{partes[1]}"
                    user, password = partes[2], partes[3]
                elif partes[3].isdigit():
                    # user:pass:host:port (variante del vendedor).
                    host_puerto = f"{partes[2]}:{partes[3]}"
                    user, password = partes[0], partes[1]
                else:
                    return {}
            elif len(partes) != 2:
                return {}
        if ":" not in host_puerto:
            return {}
        host, puerto = host_puerto.rsplit(":", 1)
        host = host.strip("[]")
        if not host or not puerto.isdigit():
            return {}
        if scheme in ("socks5", "socks"):
            scheme = "socks5"
        elif scheme in ("http", "https"):
            scheme = "http"
        else:
            return {}
        return {
            "scheme": scheme,
            "host": host,
            "port": int(puerto),
            "user": unquote(user),
            "password": unquote(password),
        }
    except Exception:
        return {}


def _payload_proxy(info: dict) -> dict:
    """Campos `proxy*` que espera CapSolver en la tarea."""
    payload = {
        "proxyType": str(info.get("scheme") or "http"),
        "proxyAddress": str(info.get("host") or ""),
        "proxyPort": int(info.get("port") or 0),
    }
    if info.get("user"):
        payload["proxyLogin"] = str(info["user"])
    if info.get("password"):
        payload["proxyPassword"] = str(info["password"])
    return payload


# --------------------------------------------------------------------------- #
# Polling de resultados
# --------------------------------------------------------------------------- #
def _esperar_solucion(clave: str, task_id: str, timeout_seg: int):
    """Sondea `/getTaskResult` hasta `ready` o timeout. `(ok, solution, error)`."""
    limite = time.time() + max(1, int(timeout_seg))
    intervalo = _poll_seg()
    while True:
        restante = limite - time.time()
        if restante <= 0:
            return False, {}, f"CapSolver no termino en {int(timeout_seg)}s"
        ok, datos, error = _post_json(
            "/getTaskResult", {"clientKey": clave, "taskId": task_id}
        )
        if not ok:
            return False, {}, error
        error_api = _error_api(datos)
        if error_api:
            return False, {}, error_api
        estado_tarea = str(datos.get("status") or "").strip().lower()
        if estado_tarea == "ready":
            solucion = datos.get("solution")
            if isinstance(solucion, dict) and solucion:
                return True, solucion, ""
            return False, {}, "CapSolver respondio ready sin solution"
        # "idle"/"processing"/desconocido: seguir esperando.
        time.sleep(min(float(intervalo), max(0.05, restante)))


def _crear_tarea(clave: str, tarea: dict):
    """Crea la tarea y devuelve `(ok, task_id, error)`; nunca lanza."""
    ok, datos, error = _post_json(
        "/createTask", {"clientKey": clave, "task": tarea}
    )
    if not ok:
        return False, "", error
    error_api = _error_api(datos)
    if error_api:
        return False, "", error_api
    task_id = str(datos.get("taskId") or "").strip()
    if not task_id:
        return False, "", "CapSolver no devolvio taskId"
    return True, task_id, ""


# --------------------------------------------------------------------------- #
# API publica
# --------------------------------------------------------------------------- #
def resolver_turnstile(
    url,
    sitekey,
    *,
    proxy="",
    action="",
    cdata="",
    chl_page_data="",
    timeout_seg=None,
) -> dict:
    """Resuelve un Cloudflare Turnstile y devuelve el token.

    Contrato: ``{"ok","token","error","segundos","task_id","usado_proxy"}``.
    Con `proxy` normalizable usa `AntiTurnstileTask`; si no,
    `AntiTurnstileTaskProxyLess`. NUNCA lanza.
    """
    resultado = {
        "ok": False,
        "token": "",
        "error": "",
        "segundos": 0.0,
        "task_id": "",
        "usado_proxy": False,
    }
    inicio = time.time()
    try:
        clave = api_key()
        if not clave:
            resultado["error"] = (
                "sin CAPSOLVER_API_KEY: no se puede resolver el Turnstile"
            )
            return resultado
        url = str(url or "").strip()
        sitekey = str(sitekey or "").strip()
        if not url or not sitekey:
            resultado["error"] = "faltan la URL o el sitekey del Turnstile"
            return resultado

        info = _normalizar_proxy(proxy)
        tarea = {"websiteURL": url, "websiteKey": sitekey}
        if info:
            tarea["type"] = "AntiTurnstileTask"
            tarea.update(_payload_proxy(info))
            resultado["usado_proxy"] = True
        else:
            tarea["type"] = "AntiTurnstileTaskProxyLess"

        metadatos = {}
        action = str(action or "").strip()
        cdata = str(cdata or "").strip()
        chl_page_data = str(chl_page_data or "").strip()
        if action:
            metadatos["action"] = action
        if cdata:
            metadatos["cdata"] = cdata
        if chl_page_data:
            metadatos["chlPageData"] = chl_page_data
        if metadatos:
            tarea["metadata"] = metadatos

        ok, task_id, error = _crear_tarea(clave, tarea)
        resultado["task_id"] = task_id
        if not ok:
            resultado["error"] = error
            return resultado

        logger.debug(
            f"CapSolver: tarea Turnstile {task_id} creada "
            f"({'con proxy' if info else 'ProxyLess'})"
        )
        ok, solucion, error = _esperar_solucion(
            clave, task_id, _timeout_seg(timeout_seg)
        )
        if not ok:
            resultado["error"] = error
            return resultado
        token = str(solucion.get("token") or "").strip()
        if not token:
            resultado["error"] = (
                "CapSolver resolvio el Turnstile pero no devolvio token"
            )
            return resultado
        resultado["ok"] = True
        resultado["token"] = token
        logger.debug(
            f"CapSolver: Turnstile resuelto en {round(time.time() - inicio, 1)}s"
        )
    except Exception as e:
        resultado["error"] = f"error inesperado: {e}"
        logger.debug(f"CapSolver: {resultado['error']}")
    finally:
        resultado["segundos"] = round(max(0.0, time.time() - inicio), 3)
    return resultado


def _normalizar_cookies(solucion: dict) -> dict:
    """Cookies de la solucion -> dict `{nombre: valor}` ({} si no hay)."""
    cookies = solucion.get("cookies")
    if isinstance(cookies, dict):
        return {
            str(nombre): str(valor)
            for nombre, valor in cookies.items()
            if str(nombre)
        }
    if isinstance(cookies, list):
        salida = {}
        for cookie in cookies:
            if (
                isinstance(cookie, dict)
                and cookie.get("name") is not None
                and cookie.get("value") is not None
            ):
                salida[str(cookie["name"])] = str(cookie["value"])
        return salida
    if isinstance(cookies, str) and cookies.strip():
        return {"cf_clearance": cookies.strip()}
    directa = solucion.get("cf_clearance")
    if isinstance(directa, str) and directa.strip():
        return {"cf_clearance": directa.strip()}
    return {}


def resolver_challenge_cloudflare(url, *, proxy="", timeout_seg=None) -> dict:
    """Resuelve el interstitial Cloudflare (AntiCloudflareTask).

    Contrato: ``{"ok","cookies","user_agent","error","segundos","task_id"}``.
    EXIGE proxy: sin proxy usable devuelve ok=False con "requiere proxy" SIN
    llamar a la API. NUNCA lanza.
    """
    resultado = {
        "ok": False,
        "cookies": {},
        "user_agent": "",
        "error": "",
        "segundos": 0.0,
        "task_id": "",
    }
    inicio = time.time()
    try:
        clave = api_key()
        if not clave:
            resultado["error"] = (
                "sin CAPSOLVER_API_KEY: no se puede resolver el reto de Cloudflare"
            )
            return resultado
        url = str(url or "").strip()
        if not url:
            resultado["error"] = "falta la URL del interstitial de Cloudflare"
            return resultado
        info = _normalizar_proxy(proxy)
        if not info:
            resultado["error"] = (
                "AntiCloudflareTask requiere proxy: el interstitial no se puede "
                "resolver sin proxy"
            )
            return resultado

        tarea = {"type": "AntiCloudflareTask", "websiteURL": url}
        tarea.update(_payload_proxy(info))
        ok, task_id, error = _crear_tarea(clave, tarea)
        resultado["task_id"] = task_id
        if not ok:
            resultado["error"] = error
            return resultado

        logger.debug(f"CapSolver: tarea Cloudflare {task_id} creada")
        ok, solucion, error = _esperar_solucion(
            clave, task_id, _timeout_seg(timeout_seg)
        )
        if not ok:
            resultado["error"] = error
            return resultado
        cookies = _normalizar_cookies(solucion)
        user_agent = str(
            solucion.get("userAgent") or solucion.get("user_agent") or ""
        ).strip()
        if not cookies and not user_agent:
            resultado["error"] = (
                "CapSolver resolvio el interstitial pero no devolvio cookies "
                "ni userAgent"
            )
            return resultado
        resultado["ok"] = True
        resultado["cookies"] = cookies
        resultado["user_agent"] = user_agent
        logger.debug(
            f"CapSolver: interstitial resuelto en {round(time.time() - inicio, 1)}s "
            f"({len(cookies)} cookies)"
        )
    except Exception as e:
        resultado["error"] = f"error inesperado: {e}"
        logger.debug(f"CapSolver: {resultado['error']}")
    finally:
        resultado["segundos"] = round(max(0.0, time.time() - inicio), 3)
    return resultado
