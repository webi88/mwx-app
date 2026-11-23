# -*- coding: utf-8 -*-
"""Autenticacion del ejecutable MWX APP (usuarios locales + tokens firmados).

- Contraseñas: PBKDF2-HMAC-SHA256 con salt aleatorio de 16 bytes y 200_000
  iteraciones. Formato autodescriptivo:
  ``pbkdf2_sha256$iteraciones$salt_hex$hash_hex``.
- Tokens: HMAC-SHA256 sobre un payload base64url JSON
  ``{uid, username, tv, exp, iat}`` firmado con `settings.secret_key` (o el
  secreto explicito). Incluyen `tv` (`token_version`) para poder revocar todas
  las sesiones de un usuario incrementando ese contador en la BD.

IMPORTANTE: este modulo NUNCA registra contraseñas ni hashes en logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select

from core.config import settings
from core.models import AppUsuario

ALGORITMO_PASSWORD = "pbkdf2_sha256"
ITERACIONES_PASSWORD = 200_000
BYTES_SALT = 16


# ---------------------------------------------------------------------------
# Contraseñas
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Devuelve el hash PBKDF2 de `password` (salt nuevo en cada llamada)."""
    texto = "" if password is None else str(password)
    sal = os.urandom(BYTES_SALT)
    derivado = hashlib.pbkdf2_hmac(
        "sha256", texto.encode("utf-8"), sal, ITERACIONES_PASSWORD
    )
    return (
        f"{ALGORITMO_PASSWORD}${ITERACIONES_PASSWORD}"
        f"${sal.hex()}${derivado.hex()}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Verifica una contraseña contra un hash PBKDF2. Nunca lanza.

    Acepta cualquier iteracion/salt guardado en el formato, de modo que subir
    `ITERACIONES_PASSWORD` en el futuro no invalide los hashes existentes.
    """
    try:
        if not password_hash:
            return False
        partes = str(password_hash).split("$")
        if len(partes) != 4:
            return False
        algoritmo, iteraciones_txt, sal_hex, esperado_hex = partes
        if algoritmo != ALGORITMO_PASSWORD:
            return False
        iteraciones = int(iteraciones_txt)
        if iteraciones <= 0:
            return False
        sal = bytes.fromhex(sal_hex)
        esperado = bytes.fromhex(esperado_hex)
        if not sal or not esperado:
            return False
        texto = "" if password is None else str(password)
        calculado = hashlib.pbkdf2_hmac(
            "sha256", texto.encode("utf-8"), sal, iteraciones
        )
        return hmac.compare_digest(calculado, esperado)
    except Exception:
        # Formato corrupto / tipos raros: se considera invalido, nunca lanza.
        return False


# ---------------------------------------------------------------------------
# Tokens de sesion
# ---------------------------------------------------------------------------
def _b64url_encode(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode("ascii")


def _b64url_decode(texto: str) -> bytes:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


def _secreto(secreto: str = "") -> bytes:
    return (secreto or getattr(settings, "secret_key", "") or "").encode("utf-8")


def _firmar(payload_b64: str, secreto: bytes) -> str:
    firma = hmac.new(secreto, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(firma)


def crear_token_app(
    usuario: AppUsuario, horas: int = 12, secreto: str = ""
) -> dict:
    """Crea un token firmado para `usuario`.

    Devuelve ``{"token": str, "expira": ISO-8601 UTC}``. El payload incluye
    `uid`, `username`, `tv` (token_version), `exp` e `iat`.
    """
    ahora = datetime.now(timezone.utc)
    expira = ahora + timedelta(hours=int(horas))
    payload = {
        "uid": int(getattr(usuario, "id", 0) or 0),
        "username": str(getattr(usuario, "username", "") or ""),
        "tv": int(getattr(usuario, "token_version", 0) or 0),
        "exp": int(expira.timestamp()),
        "iat": int(ahora.timestamp()),
    }
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )
    token = f"{payload_b64}.{_firmar(payload_b64, _secreto(secreto))}"
    return {"token": token, "expira": expira.isoformat()}


def verificar_token_app(token: str, secreto: str = "") -> Optional[dict]:
    """Valida firma y expiracion y devuelve el payload. NO consulta la BD.

    La capa API debe comprobar ademas que el usuario siga `activo` y que
    `payload["tv"] == usuario.token_version`. Devuelve None si el token esta
    vencido, mal firmado o corrupto (nunca lanza).
    """
    try:
        if not token:
            return None
        partes = str(token).split(".")
        if len(partes) != 2:
            return None
        payload_b64, firma = partes
        esperada = _firmar(payload_b64, _secreto(secreto))
        if not hmac.compare_digest(firma, esperada):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        expira = int(payload.get("exp") or 0)
        ahora = int(datetime.now(timezone.utc).timestamp())
        if expira <= ahora:
            return None
        payload["uid"] = int(payload.get("uid") or 0)
        payload["tv"] = int(payload.get("tv") or 0)
        payload["exp"] = expira
        payload["iat"] = int(payload.get("iat") or 0)
        payload["username"] = str(payload.get("username") or "")
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def revocar_sesiones(db, usuario_id: int) -> Optional[int]:
    """Invalida todos los tokens del usuario incrementando `token_version`.

    Devuelve el nuevo `token_version`, o None si el usuario no existe.
    """
    usuario = db.get(AppUsuario, int(usuario_id))
    if usuario is None:
        return None
    usuario.token_version = int(usuario.token_version or 0) + 1
    db.commit()
    return usuario.token_version


def autenticar_app(
    db, username: str, password: str
) -> Optional[AppUsuario]:
    """Login case-insensitive; solo usuarios activos.

    Actualiza `ultimo_acceso` y hace commit. Devuelve None si las credenciales
    son incorrectas o el usuario esta inactivo.
    """
    nombre = (username or "").strip().lower()
    if not nombre or password is None or password == "":
        return None
    usuario = (
        db.execute(
            select(AppUsuario).where(
                func.lower(AppUsuario.username) == nombre,
                AppUsuario.activo.is_(True),
            )
        )
        .scalars()
        .first()
    )
    if usuario is None or not verify_password(password, usuario.password_hash):
        return None
    usuario.ultimo_acceso = datetime.utcnow()
    db.commit()
    return usuario
