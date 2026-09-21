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
* Los lotes "Aged" traen el JSON de cookies y el User-Agent en la MISMA línea
  y en cualquier orden (incluso con `:` dentro de los valores del JSON). Por eso
  `parsear_linea` EXTRAE ambos bloques de la línea ANTES de hacer `split(":")`:
  el JSON se localiza con un escáner balanceado y se valida con `json.loads`, y
  el UA (`Mozilla/...`) termina en `:` o fin de línea.

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


def _lista_cookies_de_datos(datos) -> Optional[list]:
    """Extrae la lista de cookies de un JSON ya parseado, SIN warnings.

    Acepta directamente una lista o un objeto `{"cookies": [...]}` (algunos
    exportadores envuelven la lista). Devuelve `None` si la forma no es
    reconocida. Se usa en el escaneo de candidatos de `parsear_linea` (donde
    muchos bloques se descartan antes de dar con el JSON real) y
    `_extraer_lista_cookies` la envuelve con el warning para el resto de los
    flujos. NO normaliza las cookies: se guardan tal cual vienen.
    """
    if isinstance(datos, list):
        return datos
    if isinstance(datos, dict) and isinstance(datos.get("cookies"), list):
        return datos["cookies"]
    return None


def _extraer_lista_cookies(datos) -> Optional[list]:
    """Extrae la lista de cookies de un JSON ya parseado (con warning).

    Acepta directamente una lista o un objeto `{"cookies": [...]}` (algunos
    exportadores envuelven la lista). Devuelve `None` con warning si la forma
    no es reconocida. NO normaliza las cookies: se guardan tal cual vienen.
    """
    cookies = _lista_cookies_de_datos(datos)
    if cookies is None:
        logger.warning(
            f"El JSON de cookies no es una lista ni {{'cookies': [...]}} "
            f"(es {type(datos).__name__})"
        )
    return cookies


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


def _escanear_bloque_balanceado(texto: str, inicio: int) -> Optional[str]:
    """Devuelve el bloque `[...]` / `{...}` balanceado que empieza en `inicio`.

    Respeta las cadenas (`"..."`, con `\\"` escapado) para no contar los
    `[`/`{`/`]`/`}` que viven dentro de un valor (p. ej. `"value":"a[b:c"`) y
    anida `[]`/`{}` con una pila. Devuelve `None` si no encuentra el cierre o si
    los delimitadores se cruzan (`[}`). Pensado para localizar bloques JSON
    dentro de una línea con más campos.
    """
    if inicio < 0 or inicio >= len(texto) or texto[inicio] not in "[{":
        return None

    pila = []
    en_cadena = False
    escape = False
    for i in range(inicio, len(texto)):
        caracter = texto[i]
        if en_cadena:
            if escape:
                escape = False
            elif caracter == "\\":
                escape = True
            elif caracter == '"':
                en_cadena = False
            continue
        if caracter == '"':
            en_cadena = True
        elif caracter in "[{":
            pila.append(caracter)
        elif caracter in "]}":
            if not pila:
                return None
            apertura = pila.pop()
            if (apertura, caracter) not in (("[", "]"), ("{", "}")):
                return None
            if not pila:
                return texto[inicio : i + 1]
    return None


def _buscar_bloque_json_cookies(linea: str):
    """Busca el primer bloque JSON de cookies válido dentro de `linea`.

    Recorre la línea desde cada `[`/`{` con `_escanear_bloque_balanceado` y
    valida cada candidato con `json.loads` + `_lista_cookies_de_datos` (una
    lista o un objeto `{"cookies": [...]}`). El bloque puede estar en CUALQUIER
    posición de la línea (no solo al final).

    Devuelve `(inicio, largo, cookies)` —con `cookies` ya como lista— o `None`
    si ningún candidato es un JSON de cookies válido. Nunca lanza.
    """
    posicion = 0
    while posicion < len(linea):
        candidatos = [
            p
            for p in (linea.find("[", posicion), linea.find("{", posicion))
            if p != -1
        ]
        if not candidatos:
            return None
        inicio = min(candidatos)
        bloque = _escanear_bloque_balanceado(linea, inicio)
        if bloque is not None:
            try:
                datos = json.loads(bloque)
            except (ValueError, TypeError, RecursionError):
                datos = None
            if datos is not None:
                cookies = _lista_cookies_de_datos(datos)
                if cookies is not None:
                    return inicio, len(bloque), cookies
        # El candidato no sirvió (no era JSON o no era de cookies): sigue
        # buscando desde el carácter siguiente.
        posicion = inicio + 1
    return None


def _eliminar_bloque(linea: str, inicio: int, largo: int) -> str:
    """Quita `linea[inicio:inicio+largo]` y UN separador `:` adyacente.

    Los campos base van separados por `:`, así que al quitar un bloque (JSON o
    UA) también se quita el `:` que lo separaba, para no dejar `::` ni
    desalinear los campos. Prefiere el separador ANTERIOR al bloque (ignorando
    espacios); si no existe, el POSTERIOR; si tampoco, solo el bloque.
    """
    fin = inicio + largo

    anterior = inicio - 1
    while anterior >= 0 and linea[anterior] in " \t":
        anterior -= 1
    if anterior >= 0 and linea[anterior] == ":":
        return linea[:anterior] + linea[fin:]

    posterior = fin
    while posterior < len(linea) and linea[posterior] in " \t":
        posterior += 1
    if posterior < len(linea) and linea[posterior] == ":":
        return linea[:inicio] + linea[posterior + 1 :]

    return linea[:inicio] + linea[fin:]


def _es_inicio_campo(linea: str, posicion: int) -> bool:
    """True si `posicion` empieza un campo (inicio de línea o tras un `:`)."""
    anterior = posicion - 1
    while anterior >= 0 and linea[anterior] in " \t":
        anterior -= 1
    return anterior < 0 or linea[anterior] == ":"


def _parece_user_agent(valor: str) -> bool:
    """True si el campo (sin espacios) parece un User-Agent de navegador."""
    return valor.strip().lower().startswith("mozilla/")


def _extraer_user_agent(linea: str, user_agent: str = ""):
    """Extrae el User-Agent de `linea` (etiquetado o posicional) y lo elimina.

    * Etiquetado: los segmentos `user_agent=`, `useragent=` o `ua=` en
      cualquier posición se eliminan SIEMPRE (aunque ya haya un UA) y se guarda
      el primer valor no vacío.
    * Posicional: un `Mozilla/...` que empieza un campo y termina en `:` o fin
      de línea (el UA no contiene `:` en la práctica); se elimina junto con UN
      separador adyacente.

    Devuelve `(linea_restante, user_agent)`.
    """
    restantes = []
    for parte in linea.split(":"):
        clave, separador, valor = parte.partition("=")
        if separador and clave.strip().lower() in _CLAVES_USER_AGENT:
            valor = valor.strip()
            if valor and not user_agent:
                user_agent = valor
        else:
            restantes.append(parte)
    linea = ":".join(restantes)

    if user_agent:
        return linea, user_agent

    posicion = linea.lower().find("mozilla/")
    while posicion != -1:
        if _es_inicio_campo(linea, posicion):
            fin = linea.find(":", posicion)
            if fin == -1:
                fin = len(linea)
            user_agent = linea[posicion:fin].strip()
            return _eliminar_bloque(linea, posicion, fin - posicion), user_agent
        posicion = linea.lower().find("mozilla/", posicion + 1)
    return linea, user_agent


def _parsear_linea_impl(linea: str) -> Optional[dict]:
    """Implementación de `parsear_linea` (el wrapper solo agrega el try/except)."""
    original = linea
    linea = (linea or "").strip()
    if not linea or linea.startswith("#"):
        return None

    cookies = None
    user_agent = ""

    # 1) JSON de cookies: se extrae ANTES de cualquier `split(":")` porque el
    #    JSON trae `:` en sus valores y desalinearía los campos base.
    bloque = _buscar_bloque_json_cookies(linea)
    if bloque is not None:
        inicio, largo, cookies = bloque
        linea = _eliminar_bloque(linea, inicio, largo)

    # 2) User-Agent (etiquetado o posicional), también antes del split.
    linea, user_agent = _extraer_user_agent(linea, user_agent)

    # 3) Campos base: usuario, password, totp, email, email_password, auth_token.
    partes = [parte.strip() for parte in linea.split(":")]
    if not any(partes):
        logger.warning(f"Línea malformada (sin campos): {original[:40]}")
        return None

    # Los campos faltantes se rellenan con "" (no se descarta la línea).
    if len(partes) < 6:
        partes += [""] * (6 - len(partes))

    # 4) Campos extra: 7º = cookies (base64/JSON crudo), 8º = User-Agent. Los
    #    campos 7º/8º vacíos se ignoran; cualquier valor no vacío DESPUÉS del
    #    8º campo es línea malformada.
    if len(partes) > 8 and any(partes[8:]):
        logger.warning(
            f"Línea malformada (se esperaban 6, 7 u 8 campos, se obtuvieron "
            f"{len(partes)}): {original[:40]}"
        )
        return None

    if len(partes) > 6 and partes[6]:
        septimo = partes[6]
        if _parece_user_agent(septimo):
            # 7º campo = User-Agent posicional (las cookies vienen vacías).
            if not user_agent:
                user_agent = septimo
        else:
            if cookies is not None:
                logger.warning(
                    f"Línea malformada (dos campos de cookies): {original[:40]}"
                )
                return None
            cookies = decodificar_cookies(septimo)
            if cookies is None:
                logger.warning(
                    f"Línea malformada (cookies inválidas): {original[:40]}"
                )
                return None

    if len(partes) > 7 and partes[7] and not user_agent:
        user_agent = partes[7]

    username, password, totp_secret, email, email_password, auth_token = partes[:6]

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


def parsear_linea(linea: str) -> Optional[dict]:
    """Convierte una línea del lote en un dict con credenciales, cookies y UA.

    Formatos aceptados (campos separados por `:`):

        user:pass:totp:email:email_pass:auth_token[:cookies][:user_agent]

    * 6 campos: sin cookies ni User-Agent.
    * 7 campos: el último son las cookies (base64 o JSON crudo). Si el último
      campo empieza con `Mozilla/` es el User-Agent y las cookies vienen vacías.
    * 8 campos: 7º cookies, 8º User-Agent.
    * El User-Agent también se acepta con clave `user_agent=`, `useragent=` o
      `ua=` en cualquier posición; ese campo se extrae (solo el texto posterior
      al primer `=`) y se elimina del listado.
    * Los lotes "Aged" traen el JSON de cookies (`[...]` o `{"cookies": [...]}`)
      y el User-Agent en la MISMA línea, en cualquier orden y con `:` dentro de
      los valores del JSON: ambos se extraen (escáner balanceado + `json.loads`)
      ANTES de dividir por `:`, quitando también el separador adyacente. Si
      faltan campos base, se rellenan con `""`.
    * Los campos 7º/8º vacíos (`...:tok::`) se ignoran sin error; un valor no
      vacío después del 8º campo es línea malformada.

    Devuelve `None` si la línea está vacía, es un comentario (`#`) o está
    malformada. Registra con `logger.warning` el motivo del fallo (incluyendo
    los primeros 40 caracteres de la línea). Nunca lanza excepciones.
    """
    try:
        return _parsear_linea_impl(linea)
    except Exception as e:  # noqa: BLE001 - el importador nunca debe tumbar el lote
        logger.warning(f"Línea malformada (error de parseo: {e}): {str(linea)[:40]}")
        return None


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
