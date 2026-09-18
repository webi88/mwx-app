"""Importador de cuentas de X (Twitter) desde un lote de texto.

Formato de cada línea (campos separados por `:`):

    username:password:totp_secret:email:email_password:auth_token[:cookies][:user_agent]

* 6 campos: sin cookies ni User-Agent (lo normal en lotes recién creados; el
  `auth_token` basta para validar la sesión y derivar el `ct0` después).
* 7 campos: el último (`cookies`) es la sesión de la cuenta, ya sea en base64
  (que al decodificar da un JSON) o JSON CRUDO (`[...]` / `{"cookies": [...]}`),
  con la LISTA de cookies nativas de X (objetos con claves `name`, `value`,
  `domain`, `path`, `secure`, etc.). Si el 7º campo empieza con `Mozilla/` es
  en realidad el User-Agent con las cookies vacías.
* 8 campos: el 7º son las `cookies` y el 8º (último) el `user_agent`, que se
  guarda tal cual para que Chrome use el mismo agente que cuando se creó.
* El User-Agent también puede venir con clave `user_agent=`, `useragent=` o
  `ua=` en CUALQUIER posición: se extrae su valor (solo el texto posterior al
  `=`) y el campo se elimina del listado.

Este módulo reemplaza el flujo obsoleto de `cargar_cuenta.py` (login con
contraseña para extraer la cookie): ahora las credenciales, las cookies
COMPLETAS (auth_token, ct0, twid, etc.) y el User-Agent se importan
directamente a la base de datos.
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


def _extraer_lista_cookies(datos) -> Optional[list]:
    """Extrae la lista de cookies de un JSON ya parseado.

    Acepta directamente una lista o un objeto `{"cookies": [...]}` (algunos
    exportadores envuelven la lista). Devuelve `None` con warning si la forma
    no es reconocida. NO normaliza las cookies: se guardan tal cual vienen.
    """
    if isinstance(datos, list):
        return datos
    if isinstance(datos, dict) and isinstance(datos.get("cookies"), list):
        return datos["cookies"]
    logger.warning(
        f"El JSON de cookies no es una lista ni {{'cookies': [...]}} "
        f"(es {type(datos).__name__})"
    )
    return None


def decodificar_cookies(valor: str) -> Optional[list]:
    """Convierte el campo de cookies a una lista de cookies (JSON).

    Acepta:
    * JSON CRUDO: el texto (tras `strip`) empieza con `[` o `{`; los lotes de
      vendedores suelen pegar el JSON directamente. Si es un objeto con la
      forma `{"cookies": [...]}` se extrae esa lista.
    * Base64 (comportamiento original): se decodifica y el JSON resultante
      debe ser una lista (o un objeto `{"cookies": [...]}`).

    Devuelve la lista de cookies tal como viene (SIN normalizar; la
    normalización para Selenium vive en `utils/anti_detection.py`) o `None`
    si el JSON/base64 es inválido. Nunca lanza excepciones: es tolerante con
    padding de base64 incompleto, `binascii.Error`, `json.JSONDecodeError` y
    `UnicodeDecodeError`.
    """
    if not valor:
        logger.warning("Cookies vacías")
        return None

    raw = valor.strip()
    if not raw:
        logger.warning("Cookies vacías")
        return None

    # JSON crudo (lotes de vendedores que pegan el JSON directamente).
    if raw[0] in "[{":
        try:
            datos = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON inválido en cookies: {e}")
            return None
        return _extraer_lista_cookies(datos)

    # Base64: agregar padding `=` si hace falta (múltiplo de 4).
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

    return _extraer_lista_cookies(cookies)


# Claves aceptadas para el User-Agent cuando viene etiquetado (`clave=valor`)
# en cualquier posición de la línea.
_CLAVES_USER_AGENT = ("user_agent", "useragent", "ua")


def parsear_linea(linea: str) -> Optional[dict]:
    """Convierte una línea del lote en un dict con credenciales, cookies y UA.

    Formatos aceptados (campos separados por `:`):

        user:pass:totp:email:email_pass:auth_token[:cookies][:user_agent]

    * 6 campos: sin cookies ni User-Agent.
    * 7 campos: el último son las cookies (base64 o JSON crudo). Si el último
      campo empieza con `Mozilla/` es el User-Agent y las cookies vienen
      vacías.
    * 8 campos: 7º cookies, 8º User-Agent.
    * El User-Agent también se acepta con clave `user_agent=`, `useragent=` o
      `ua=` en cualquier posición; ese campo se extrae (solo el texto posterior
      al primer `=`) y se elimina del listado.
    * Las cookies pueden ser JSON crudo; como el JSON contiene `:`, si el 7º
      campo empieza con `[` o `{` se re-une el resto de la línea como un solo
      campo de cookies (si al final viene un User-Agent posicional que empieza
      con `Mozilla/`, se separa antes de re-unir).

    Devuelve `None` si la línea está vacía, es un comentario (`#`) o está
    malformada. Registra con `logger.warning` el motivo del fallo (incluyendo
    los primeros 40 caracteres de la línea).
    """
    linea = (linea or "").strip()
    if not linea or linea.startswith("#"):
        return None

    partes = linea.split(":")

    # 1) Extraer el User-Agent etiquetado (`ua=Mozilla/...`) de cualquier
    #    posición. El UA no contiene `:`, así que basta con el fragmento.
    user_agent = ""
    restantes = []
    for parte in partes:
        clave, sep, valor = parte.partition("=")
        if sep and clave.strip().lower() in _CLAVES_USER_AGENT:
            if not user_agent:
                user_agent = valor.strip()
        else:
            restantes.append(parte)
    partes = restantes

    # 2) Cookies en JSON crudo: el JSON trae `:` propios, por lo que el split
    #    simple lo fragmenta. Si el 7º campo empieza con `[` o `{`, se re-une
    #    todo el resto de la línea como el campo de cookies. Un User-Agent
    #    posicional (sin clave, empieza con `Mozilla/`) al final se separa
    #    antes de re-unir, porque si no quedaría dentro del JSON.
    if len(partes) > 7 and partes[6].strip().startswith(("[", "{")):
        if not user_agent and partes[-1].strip().startswith("Mozilla/"):
            user_agent = partes[-1].strip()
            partes = partes[:6] + [":".join(partes[6:-1])]
        else:
            partes = partes[:6] + [":".join(partes[6:])]

    # Acepta 6 campos (sin cookies), 7 (con cookies o con UA) u 8 (cookies+UA).
    if len(partes) == 6:
        cookies_b64 = ""
    elif len(partes) == 7:
        ultimo = partes[6].strip()
        if ultimo.startswith("Mozilla/"):
            # 7º campo = User-Agent (las cookies vienen vacías).
            cookies_b64 = ""
            if not user_agent:
                user_agent = ultimo
        else:
            cookies_b64 = partes[6]
    elif len(partes) == 8:
        cookies_b64 = partes[6]
        ua_posicional = partes[7].strip()
        if ua_posicional and not user_agent:
            user_agent = ua_posicional
    else:
        logger.warning(
            f"Línea malformada (se esperaban 6, 7 u 8 campos, se obtuvieron "
            f"{len(partes)}): {linea[:40]}"
        )
        return None

    username, password, totp_secret, email, email_password, auth_token = partes[:6]

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
        "user_agent": user_agent,
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

    `seccion` (CI/IP/LIB/JUS) y `tipo_cuenta` (politica/ciudadana) son OPCIONALES:
    - Al crear una cuenta nueva se guardan normalizados (vacíos si no vienen).
    - Al actualizar una existente solo se pisan si llegan con valor; si vienen
      vacíos se respeta lo que ya tiene la cuenta.

    Sesión existente: al re-importar una línea de 6 campos (sin cookies) con
    `auth_token`/credenciales vacías NO se pisan `auth_token` ni
    `cookies_json` (ni el resto de credenciales); solo se actualizan cuando
    el lote trae valores no vacíos (`cookies` solo cuando no es `None`).
    El `user_agent` se pisa igual que el resto: solo si el lote trae uno no
    vacío, así una re-importación sin UA conserva el guardado.
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
                user_agent=fields.get("user_agent", ""),
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
                "user_agent",
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
