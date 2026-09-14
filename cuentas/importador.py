"""Importador de cuentas de X (Twitter) desde un lote de texto.

Formato de cada línea (campos separados por `:`):

    username:password:totp_secret:email:email_password:auth_token[:cookies_base64]

* 6 campos: sin cookies (lo normal en lotes recién creados; el `auth_token`
  basta para validar la sesión y derivar el `ct0` después).
* 7 campos: el último (`cookies_base64`) es una cadena en base64 que, al
  decodificarla, contiene un JSON con una LISTA de cookies nativas de X
  (objetos con claves `name`, `value`, `domain`, `path`, `secure`, etc.).

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

try:
    from core.secciones import normalizar_seccion
except Exception:  # pragma: no cover - defensivo si falta core/secciones.py
    def normalizar_seccion(valor):
        return ""

try:
    from core.registros import normalizar_tipo_cuenta
except Exception:  # pragma: no cover - defensivo si falta core/registros.py
    def normalizar_tipo_cuenta(valor):
        return ""


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

    partes = linea.split(":")
    # Acepta 6 campos (sin cookies) o 7 (con cookies_base64). El base64 no
    # contiene ':' por lo que split simple es seguro.
    if len(partes) == 6:
        partes = partes + [""]
    if len(partes) != 7:
        logger.warning(
            f"Línea malformada (se esperaban 6 o 7 campos, se obtuvieron "
            f"{len(partes)}): {linea[:40]}"
        )
        return None

    username, password, totp_secret, email, email_password, auth_token, cookies_b64 = partes

    # Las cookies son OPCIONALES: si no vienen, la cuenta se importa igual y el
    # validador obtiene el ct0 a partir del auth_token.
    cookies = None
    if cookies_b64.strip():
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


def _valor_informado(valor) -> bool:
    """True si el lote trae un valor no vacío (tras `strip`).

    Se usa para NO pisar la BD con vacíos al re-importar: una línea de
    6 campos llega con `cookies=None` y puede traer `auth_token=""` u
    otras credenciales vacías; en esos casos se conserva lo guardado.
    """
    return isinstance(valor, str) and bool(valor.strip())


def importar_una(fields: dict, seccion: str = "", tipo_cuenta: str = "") -> str:
    """Inserta o actualiza una cuenta en la base de datos (upsert por usuario).

    Devuelve `"nueva"` si la cuenta no existía y `"actualizada"` si ya existía.

    `seccion` (CI/CD/IP) y `tipo_cuenta` (politica/ciudadana) son OPCIONALES:
    - Al crear una cuenta nueva se guardan normalizados (vacíos si no vienen).
    - Al actualizar una existente solo se pisan si llegan con valor; si vienen
      vacíos se respeta lo que ya tiene la cuenta.

    Sesión existente: al re-importar una línea de 6 campos (sin cookies) con
    `auth_token`/credenciales vacías NO se pisan `auth_token` ni
    `cookies_json` (ni el resto de credenciales); solo se actualizan cuando
    el lote trae valores no vacíos (`cookies` solo cuando no es `None`).
    """
    seccion_norm = normalizar_seccion(seccion)
    tipo_norm = normalizar_tipo_cuenta(tipo_cuenta)

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
                cookies_json=fields.get("cookies"),
                plataforma="twitter",
                status="imported",
                activa=True,
                grupo="",
                sector="",
                seccion=seccion_norm,
                tipo_cuenta=tipo_norm,
                fecha_creacion=datetime.utcnow(),
                last_checked=None,
            )
            db.add(cuenta)
            resultado = "nueva"
        else:
            # NO pisar la sesión existente: si la línea viene sin
            # auth_token/cookies (6 campos -> cookies=None) u otras
            # credenciales vacías, se conserva lo ya guardado en BD.
            # Solo se actualiza cuando el lote trae valores no vacíos.
            for _campo in (
                "password",
                "totp_secret",
                "email",
                "email_password",
                "auth_token",
            ):
                _nuevo = fields.get(_campo, "")
                if _valor_informado(_nuevo):
                    setattr(cuenta, _campo, _nuevo)
            if fields.get("cookies") is not None:
                cuenta.cookies_json = fields["cookies"]
            cuenta.status = "imported"
            cuenta.last_checked = None
            if seccion_norm:
                cuenta.seccion = seccion_norm
            if tipo_norm:
                cuenta.tipo_cuenta = tipo_norm
            resultado = "actualizada"

    return resultado


def importar_lote(texto: str, seccion: str = "", tipo_cuenta: str = "") -> dict:
    """Importa un lote completo de cuentas (una por línea).

    `seccion` y `tipo_cuenta` son opcionales y se aplican a TODAS las líneas
    (ver `importar_una` para la semántica de actualización).

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

            resultado = importar_una(fields, seccion=seccion, tipo_cuenta=tipo_cuenta)
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
