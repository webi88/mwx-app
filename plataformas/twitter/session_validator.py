"""Validador de sesiones de X (Twitter) sin navegador, usando httpx.

Permite comprobar si un `auth_token` + `ct0` guardados en BD siguen siendo
válidos (activos, expirados, suspendidos o limitados) sin abrir Chrome.
También inyecta el proxy Smartproxy México sticky de forma determinista por
cuenta para no levantar alarmas de anti-spam.
"""
import hashlib
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger

from core.config import settings
from core.database import get_db_session
from core.models import Cuenta

X_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
# Endpoint vivo de la web de X para validar sesión. El antiguo
# `/i/api/1.1/account/settings.json` fue retirado por X (devuelve 404 code 34
# "page does not exist"). `notifications/all.json` responde 200 si la sesión
# está activa y 401 ("Could not authenticate you", code 32) si el auth_token
# es inválido/expirado.
X_SETTINGS_URL = "https://x.com/i/api/2/notifications/all.json?count=1"


def _como_lista(cookies) -> list:
    """Normaliza `cookies_json` (None, str o list) a una lista de dicts."""
    if not cookies:
        return []
    if isinstance(cookies, str):
        import json

        try:
            cookies = json.loads(cookies)
        except Exception:
            return []
    if not isinstance(cookies, list):
        return []
    return [c for c in cookies if isinstance(c, dict)]


def extraer_ct0(cookies: list) -> str:
    """Devuelve el `value` de la cookie `ct0` (o "" si no está)."""
    for c in _como_lista(cookies):
        if c.get("name") == "ct0":
            return c.get("value") or ""
    return ""


def cookie_a_dict(cookies: list) -> dict:
    """Convierte una lista de cookies a `{name: value}` (para el header Cookie)."""
    resultado = {}
    for c in _como_lista(cookies):
        name = c.get("name")
        value = c.get("value")
        if name is not None and value is not None:
            resultado[name] = value
    return resultado


def construir_headers(auth_token: str, ct0: str) -> dict:
    """Construye los headers necesarios para consultar la API interna de X."""
    return {
        "authorization": f"Bearer {X_BEARER_TOKEN}",
        "x-csrf-token": ct0,
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "x-twitter-auth-type": "OAuth2Session",
    }


def _session_id_por_cuenta(usuario: str) -> str:
    """Sesión sticky determinista y distinta por cuenta (8 hex chars)."""
    return hashlib.md5(usuario.encode()).hexdigest()[:8]


def _mapear_estado(status_code: int) -> str:
    if status_code == 200:
        return "active"
    if status_code == 401:
        return "expired"
    if status_code == 403:
        return "suspended"
    if status_code == 429:
        return "limited"
    return "error"


def validar_token(
    auth_token: str,
    ct0: str,
    proxy: str = None,
    timeout: int = 15,
) -> dict:
    """Valida un token contra `account/settings.json` usando httpx.

    `proxy` es la URL completa con credenciales
    (ej. `http://user:pass@host:port`), que httpx acepta directamente.
    """
    headers = construir_headers(auth_token, ct0)
    try:
        resp = httpx.get(
            X_SETTINGS_URL,
            headers=headers,
            proxy=proxy,
            timeout=timeout,
            follow_redirects=False,
        )
        status_code = resp.status_code
        return {
            "status_code": status_code,
            "estado": _mapear_estado(status_code),
            "error": "",
        }
    except Exception as e:
        return {
            "status_code": None,
            "estado": "error",
            "error": str(e)[:200],
        }


def validar_cuenta(cuenta) -> str:
    """Valida una cuenta (objeto Cuenta) y persiste `status` + `last_checked`.

    Devuelve el estado resultante: `active`, `expired`, `suspended`,
    `limited` o `error`.
    """
    auth_token = (cuenta.auth_token or "").strip()
    ct0 = extraer_ct0(cuenta.cookies_json)

    def _actualizar(estado: str) -> str:
        try:
            with get_db_session() as db:
                reg = db.query(Cuenta).filter(Cuenta.usuario == cuenta.usuario).first()
                if reg is not None:
                    reg.status = estado
                    reg.last_checked = datetime.utcnow()
        except Exception as e:
            logger.error(f"Error actualizando cuenta {cuenta.usuario}: {e}")
        return estado

    if not auth_token or not ct0:
        logger.warning(f"Cuenta {cuenta.usuario} sin auth_token/ct0 -> expired")
        return _actualizar("expired")

    proxy = settings.proxy_sticky_mx(
        session_id=_session_id_por_cuenta(cuenta.usuario)
    )
    resultado = validar_token(auth_token, ct0, proxy=proxy)
    estado = resultado["estado"]
    logger.info(
        f"Validacion {cuenta.usuario}: {estado} "
        f"(status_code={resultado['status_code']})"
    )
    return _actualizar(estado)


def validar_todas(solo_status=None, callback=None) -> dict:
    """Valida todas las cuentas de twitter (opcionalmente filtrando por status).

    `callback(actual, total, usuario, estado)` se invoca por cuenta si se pasa.
    Devuelve un resumen con contadores por estado.
    """
    resumen = {
        "total": 0,
        "active": 0,
        "expired": 0,
        "suspended": 0,
        "limited": 0,
        "error": 0,
    }

    try:
        with get_db_session() as db:
            q = db.query(Cuenta).filter(Cuenta.plataforma == "twitter")
            if solo_status:
                q = q.filter(Cuenta.status == solo_status)
            cuentas = q.all()
    except Exception as e:
        logger.error(f"Error consultando cuentas twitter: {e}")
        return resumen

    total = len(cuentas)
    resumen["total"] = total

    for idx, cuenta in enumerate(cuentas, start=1):
        estado = validar_cuenta(cuenta)
        resumen[estado] = resumen.get(estado, 0) + 1
        if callback:
            try:
                callback(idx, total, cuenta.usuario, estado)
            except Exception as e:
                logger.error(f"Error en callback: {e}")

    return resumen
