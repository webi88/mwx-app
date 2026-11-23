# -*- coding: utf-8 -*-
"""Dependencias compartidas de la API MWX APP.

- `get_db`: sesión SQLAlchemy (SQLite en tests, PostgreSQL/Supabase en prod).
- `obtener_usuario_actual`: valida ``Authorization: Bearer`` y REVALIDA contra
  la BD (usuario existente, activo y `token_version` vigente), devolviendo el
  `AppUsuario` ORM.
- Rate limit simple en memoria para `/v1/login` (username + IP).

Nunca se registran contraseñas, tokens ni cookies en logs.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Generator, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.app_auth import verificar_token_app
from core.models import AppUsuario

_ESQUEMA_BEARER = HTTPBearer(auto_error=False)

_RATE_LOCK = threading.Lock()
_RATE_INTENTOS: dict[str, Deque[float]] = defaultdict(deque)


def get_db() -> Generator:
    """Entrega una sesión de BD y la cierra SIEMPRE al terminar la petición."""
    from core.database import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _error(status_code: int, mensaje: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=mensaje)


def obtener_usuario_actual(
    credenciales: Optional[HTTPAuthorizationCredentials] = Depends(_ESQUEMA_BEARER),
    db=Depends(get_db),
) -> AppUsuario:
    """Valida el Bearer y revalida la sesión contra la base de datos.

    Códigos: 401 (sin token, token inválido/expirado, usuario inexistente o
    sesión revocada) y 403 (usuario desactivado).
    """
    if credenciales is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, "token de autorización requerido")
    if (credenciales.scheme or "").lower() != "bearer":
        raise _error(status.HTTP_401_UNAUTHORIZED, "esquema de autorización inválido")

    payload = verificar_token_app(credenciales.credentials or "")
    if payload is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, "token inválido o expirado")

    usuario = db.get(AppUsuario, int(payload.get("uid") or 0))
    if usuario is None:
        raise _error(status.HTTP_401_UNAUTHORIZED, "usuario no encontrado")
    if not bool(usuario.activo):
        raise _error(status.HTTP_403_FORBIDDEN, "cuenta desactivada")
    if int(usuario.token_version or 0) != int(payload.get("tv") or 0):
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesión revocada")
    return usuario


# ---------------------------------------------------------------------------
# Rate limit de login (memoria del proceso, suficiente para el ejecutable)
# ---------------------------------------------------------------------------
def _entero_env(nombre: str, defecto: int) -> int:
    try:
        return max(1, int(os.environ.get(nombre, str(defecto)) or defecto))
    except (TypeError, ValueError):
        return defecto


def limite_intentos_login() -> int:
    """Máximo de fallos permitidos por username+IP (env `MWX_RATE_LIMIT_INTENTOS`)."""
    return _entero_env("MWX_RATE_LIMIT_INTENTOS", 10)


def minutos_ventana_login() -> int:
    """Ventana del rate limit en minutos (env `MWX_RATE_LIMIT_MINUTOS`)."""
    return _entero_env("MWX_RATE_LIMIT_MINUTOS", 5)


def _clave_rate(username: str, ip: str) -> str:
    return f"{(username or '').strip().lower()}|{(ip or '').strip()}"


def _podar(cola: Deque[float], ahora: float, ventana_seg: float) -> None:
    while cola and ahora - cola[0] > ventana_seg:
        cola.popleft()


def verificar_rate_limit(username: str, ip: str) -> None:
    """Lanza 429 si esa combinación agotó sus intentos en la ventana."""
    clave = _clave_rate(username, ip)
    ahora = time.monotonic()
    with _RATE_LOCK:
        cola = _RATE_INTENTOS[clave]
        _podar(cola, ahora, minutos_ventana_login() * 60.0)
        if len(cola) >= limite_intentos_login():
            raise _error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "demasiados intentos de inicio de sesión, intenta más tarde",
            )


def registrar_intento_fallido(username: str, ip: str) -> None:
    """Anota UN fallo de login para el rate limit (nunca lanza)."""
    clave = _clave_rate(username, ip)
    ahora = time.monotonic()
    with _RATE_LOCK:
        cola = _RATE_INTENTOS[clave]
        _podar(cola, ahora, minutos_ventana_login() * 60.0)
        cola.append(ahora)


def limpiar_intentos(username: str, ip: str) -> None:
    """Borra el historial de fallos de una combinación (login exitoso)."""
    with _RATE_LOCK:
        _RATE_INTENTOS.pop(_clave_rate(username, ip), None)


def limpiar_rate_limit() -> None:
    """Vacía TODO el rate limit en memoria (usado por pruebas y reinicios)."""
    with _RATE_LOCK:
        _RATE_INTENTOS.clear()
