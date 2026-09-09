"""Importador de cuentas de X (Twitter) desde un lote de texto.

Formato de cada línea (7 campos separados por `:`):

    username:password:totp_secret:email:email_password:auth_token:cookies_base64

El último campo (`cookies_base64`) es una cadena en base64 que, al decodificarla,
contiene un JSON con una LISTA de cookies nativas de X (objetos con claves
`name`, `value`, `domain`, `path`, `secure`, `httpOnly`, etc.).

Este módulo reemplaza el flujo obsoleto de `cargar_cuenta.py` (login con
contraseña para extraer la cookie): ahora las credenciales y las cookies se
importan directamente a la base de datos.
"""
import base64
import binascii
import json
from datetime import datetime
from typing import Optional

from loguru import logger

from core.database import get_db_session
from core.models import Cuenta


def decodificar_cookies(cookies_base64: str) -> Optional[list]:
    """Decodifica una cadena base64 a una lista de cookies (JSON).

    Devuelve la lista de cookies o `None` si el base64/JSON es inválido.
    Tolerante con padding de base64 incompleto, `binascii.Error`,
    `json.JSONDecodeError` y `UnicodeDecodeError`.
    """
    if not cookies_base64:
        logger.warning("Cookies base64 vacías")
        return None

    raw = cookies_base64.strip()
    # Agregar padding `=` si hace falta (múltiplo de 4).
    rem = len(raw) % 4
    if rem:
        raw += "=" * (4 - rem)

    try:
        datos = base64.b64decode(raw)
    except (binascii.Error, ValueError) as e:
        logger.warning(f"Base64 inválido al decodificar cookies: {e}")
        return None

    try:
        texto = datos.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"Las cookies no son UTF-8 válido: {e}")
        return None

    try:
        cookies = json.loads(texto)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON inválido en cookies: {e}")
        return None

    if not isinstance(cookies, list):
        logger.warning(f"El JSON de cookies no es una lista (es {type(cookies).__name__})")
        return None

    return cookies


def parsear_linea(linea: str) -> Optional[dict]:
    """Convierte una línea del lote en un dict con las credenciales y cookies.

    Devuelve `None` si la línea está vacía, es un comentario (`#`) o está
    malformada. Registra con `logger.warning` el motivo del fallo (incluyendo
    los primeros 40 caracteres de la línea).
    """
    linea = (linea or "").strip()
    if not linea or linea.startswith("#"):
        return None

    partes = linea.split(":", 6)
    if len(partes) != 7:
        logger.warning(
            f"Línea malformada (se esperaban 7 campos, se obtuvieron {len(partes)}): "
            f"{linea[:40]}"
        )
        return None

    username, password, totp_secret, email, email_password, auth_token, cookies_b64 = partes

    cookies = decodificar_cookies(cookies_b64)
    if cookies is None:
        logger.warning(f"Línea malformada (cookies inválidas): {linea[:40]}")
        return None

    return {
        "username": username,
        "password": password,
        "totp_secret": totp_secret,
        "email": email,
        "email_password": email_password,
        "auth_token": auth_token,
        "cookies": cookies,
    }


def importar_una(fields: dict) -> str:
    """Inserta o actualiza una cuenta en la base de datos (upsert por usuario).

    Devuelve `"nueva"` si la cuenta no existía y `"actualizada"` si ya existía.
    """
    with get_db_session() as db:
        cuenta = db.query(Cuenta).filter(Cuenta.usuario == fields["username"]).first()

        if cuenta is None:
            cuenta = Cuenta(
                usuario=fields["username"],
                password=fields.get("password", ""),
                totp_secret=fields.get("totp_secret", ""),
                email=fields.get("email", ""),
                email_password=fields.get("email_password", ""),
                auth_token=fields.get("auth_token", ""),
                cookies_json=fields["cookies"],
                plataforma="twitter",
                status="imported",
                activa=True,
                grupo="A",
                sector="",
                fecha_creacion=datetime.utcnow(),
                last_checked=None,
            )
            db.add(cuenta)
            resultado = "nueva"
        else:
            cuenta.password = fields.get("password", "")
            cuenta.totp_secret = fields.get("totp_secret", "")
            cuenta.email = fields.get("email", "")
            cuenta.email_password = fields.get("email_password", "")
            cuenta.auth_token = fields.get("auth_token", "")
            cuenta.cookies_json = fields["cookies"]
            cuenta.status = "imported"
            cuenta.last_checked = None
            resultado = "actualizada"

    return resultado


def importar_lote(texto: str) -> dict:
    """Importa un lote completo de cuentas (una por línea).

    Devuelve un dict con el resumen:
        {"total", "importadas", "actualizadas", "errores", "detalle_errores"}
    Las líneas vacías y los comentarios (`#`) se omiten sin contar como error.
    """
    total = 0
    importadas = 0
    actualizadas = 0
    errores = 0
    detalle_errores = []

    for linea in (texto or "").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue  # no es una cuenta; no cuenta en el total ni como error

        total += 1
        try:
            fields = parsear_linea(linea)
            if fields is None:
                errores += 1
                detalle_errores.append(f"Línea malformada: {linea[:40]}")
                continue

            resultado = importar_una(fields)
            if resultado == "nueva":
                importadas += 1
            else:
                actualizadas += 1
        except Exception as e:
            errores += 1
            detalle_errores.append(f"Error importando '{linea[:40]}': {e}")
            logger.warning(f"Error importando línea: {e}")

    return {
        "total": total,
        "importadas": importadas,
        "actualizadas": actualizadas,
        "errores": errores,
        "detalle_errores": detalle_errores,
    }
