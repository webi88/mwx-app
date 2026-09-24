"""Lector de códigos de verificación por CORREO (IMAP SSL).

Lo usa el bot de clientes (`bot_clientes/handlers.py`) cuando una cuenta NO
tiene 2FA TOTP: X manda el código de confirmación al correo de la cuenta y este
módulo lo busca en la bandeja (INBOX y, si no aparece, en Spam/Bulk/Junk).

CONTRATO CONGELADO (el bot ya lo consume así; NO cambiar):

    obtener_codigo_verificacion(email, password, timeout=25) -> dict

    dict con claves EXACTAS:
        {"ok": bool, "codigo": str, "remitente": str, "asunto": str,
         "fecha": str, "error": str}

    * `ok=True` + `codigo` no vacío cuando encontró un código reciente.
    * `ok=False` + `error` en español (SIN la contraseña) cuando no lo encontró
      o hubo un problema.
    * NUNCA lanza excepciones y NUNCA registra la contraseña.

Detección del código (`extraer_codigo_de_texto`): busca secuencias de 6 dígitos
en asuntos/cuerpos (limpia HTML y entidades), descarta números incrustados en
secuencias más largas (teléfonos, ids, colores hex) y PRIORIZA los que están
cerca de palabras como "código/code/verificación/confirmation".

Conexión: IMAP SSL a `imap.mail.yahoo.com:993` por defecto; el host se puede
cambiar con la variable de entorno `LECTOR_CORREO_HOST` (y el puerto con
`LECTOR_CORREO_PORT`). Se leen los ~25 correos más recientes por UID
descendente y se prefiere a los remitentes/asuntos de X/Twitter/verificación.

Solo stdlib: `imaplib`, `email`, `socket`, `re` (más `os`, `time`, `logging`).
"""

from __future__ import annotations

import imaplib
import logging
import os
import re
import socket
import time
from email import message_from_bytes
from email.header import decode_header, make_header

__all__ = [
    "extraer_codigo_de_texto",
    "obtener_codigo_verificacion",
    "diagnosticar_conexion",
]

logger = logging.getLogger(__name__)

# Servidor IMAP por defecto (Yahoo) y puerto SSL estándar.
HOST_POR_DEFECTO = "imap.mail.yahoo.com"
PUERTO_POR_DEFECTO = 993

# Cuántos correos recientes se revisan como máximo por carpeta.
LIMITE_CORREOS = 25

# Carpetas de spam/bulk que Yahoo y otros proveedores suelen usar. Se buscan
# por nombre (exacto o que contenga spam/junk/bulk) y, si el listado no las
# muestra, se intentan seleccionar directamente.
_CARPETAS_SPAM = (
    "Bulk",
    "Spam",
    "Junk",
    "Bulk Mail",
    "Junk E-mail",
    "Junk Email",
    "Correo no deseado",
)

# Raíces de palabras que indican que el número cercano es un código de
# verificación (se comparan sobre texto sin acentos y en minúsculas). Se usan
# raíces para cubrir variantes ("verif" -> verificación/verification/verify).
_CLAVES_CODIGO = (
    "codigo",
    "code",
    "verif",
    "confirm",
    "autentic",
    "otp",
    "one-time",
    "one time",
    "un solo uso",
    "seguridad",
    "security",
    "sesion",
    "login",
    "sign in",
    "clave",
    "acceso",
)

# Preferencia del CORREO completo: remitentes/asuntos de X/Twitter/verificación.
_CLAVES_REMITENTE = ("twitter", "x.com", "verify", "verif")
_CLAVES_ASUNTO = ("codigo", "code", "verif", "confirm", "login", "sesion", "acceso")

# Mapa simple de acentos para normalizar la cercanía sin depender de más módulos.
_MAPA_ACENTOS = str.maketrans(
    {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u", "ü": "u", "ñ": "n",
        "Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ü": "U", "Ñ": "N",
    }
)

# 6 dígitos contiguos que NO formen parte de un número/token más largo ni de
# un color hex o una entidad HTML (`#123456`, `&#123456;`, `abc123456`).
_RE_CODIGO_CONTIGUO = re.compile(r"(?<![\w#&$])(\d{6})(?![\w#&$])")
# Variante "123 456" / "123-456" (solo si hay contexto de verificación cerca).
_RE_CODIGO_AGRUPADO = re.compile(r"(?<![\w#&$])(\d{3})[ \u00a0-](\d{3})(?![\w-])")

_RE_SCRIPT_ESTILO = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_RE_ETIQUETA = re.compile(r"<[^>]+>")
_RE_ENTIDAD_NUMERICA = re.compile(r"&#(\d{1,7});")
_RE_ENTIDAD_HEX = re.compile(r"&#x([0-9a-fA-F]{1,6});")
_RE_ESPACIOS_HORIZONTALES = re.compile(r"[ \t\r\f\v]+")
_RE_SALTOS_REPETIDOS = re.compile(r"\n{3,}")

# Entidades HTML más comunes (se desescapan antes de buscar códigos).
_ENTIDADES_SIMPLES = (
    ("&nbsp;", " "),
    ("&amp;", "&"),
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&apos;", "'"),
    ("&#39;", "'"),
)


# --------------------------------------------------------------------------- #
# Respuesta del contrato
# --------------------------------------------------------------------------- #
def _respuesta(
    ok: bool = False,
    codigo: str = "",
    remitente: str = "",
    asunto: str = "",
    fecha: str = "",
    error: str = "",
) -> dict:
    """Arma el dict del contrato con las 6 claves EXACTAS y valores de texto."""
    return {
        "ok": bool(ok),
        "codigo": str(codigo or ""),
        "remitente": str(remitente or ""),
        "asunto": str(asunto or ""),
        "fecha": str(fecha or ""),
        "error": str(error or ""),
    }


def _sanear_texto(error, password: str = "") -> str:
    """Texto de un error SIN la contraseña y acotado (para mensajes públicos)."""
    try:
        texto = str(error)
    except Exception:
        return ""
    if password:
        try:
            texto = texto.replace(str(password), "***")
        except Exception:
            pass
    texto = " ".join(texto.split())
    return texto[:180]


# --------------------------------------------------------------------------- #
# Extracción pura del código (sin red)
# --------------------------------------------------------------------------- #
def _desescapar_entidades(texto: str) -> str:
    """Desescapa entidades HTML comunes y numéricas/hexadecimales.

    Se hace ANTES de buscar códigos porque algunos correos escriben dígitos u
    otros caracteres con entidades (`&#54;`); también evita que `&#123456;`
    se lea como código. Nunca lanza."""
    for clave, valor in _ENTIDADES_SIMPLES:
        texto = texto.replace(clave, valor)

    def _caracter(codigo: int) -> str:
        try:
            if codigo < 32 or codigo > 0x10FFFF:
                return " "
            return chr(codigo)
        except Exception:
            return " "

    try:
        texto = _RE_ENTIDAD_NUMERICA.sub(lambda m: _caracter(int(m.group(1))), texto)
        texto = _RE_ENTIDAD_HEX.sub(lambda m: _caracter(int(m.group(1), 16)), texto)
    except Exception:
        pass
    return texto


def _limpiar_html(texto: str) -> str:
    """Quita scripts/estilos/etiquetas, desescapa entidades y normaliza espacios."""
    texto = _RE_SCRIPT_ESTILO.sub(" ", texto)
    texto = _RE_ETIQUETA.sub(" ", texto)
    texto = _desescapar_entidades(texto)
    texto = _RE_ESPACIOS_HORIZONTALES.sub(" ", texto)
    texto = _RE_SALTOS_REPETIDOS.sub("\n\n", texto)
    return texto.strip()


def _sin_acentos(texto) -> str:
    """Texto sin acentos (para comparar palabras clave con tolerancia)."""
    try:
        return str(texto).translate(_MAPA_ACENTOS)
    except Exception:
        return str(texto)


# Radio (en caracteres) donde una palabra clave aún cuenta como "cerca"; el
# peso decae linealmente con la distancia, así el código que sigue a "tu código
# de verificación es ..." gana aunque otro número esté en el mismo párrafo.
_RADIO_CERCANIA = 60


def _puntaje_cercania(texto: str, inicio: int, fin: int) -> float:
    """Cercanía ponderada por distancia a palabras clave de verificación.

    Suma, por cada aparición de cada raíz en `_CLAVES_CODIGO`, un peso
    `max(0, 1 - distancia/radio)`: las palabras pegadas al candidato valen ~1 y
    las que están al otro extremo del párrafo casi 0. Así "código: 123456"
    gana a un número suelto lejano aunque ambos compartan párrafo.
    """
    normalizado = _sin_acentos(texto).lower()
    centro = (inicio + fin) / 2.0
    total = 0.0
    for clave in _CLAVES_CODIGO:
        posicion = normalizado.find(clave)
        while posicion != -1:
            medio = posicion + len(clave) / 2.0
            distancia = abs(medio - centro)
            if distancia < _RADIO_CERCANIA:
                total += 1.0 - (distancia / _RADIO_CERCANIA)
            posicion = normalizado.find(clave, posicion + 1)
    return total


def _candidatos_codigo(texto: str) -> list:
    """Candidatos `(inicio, codigo, base, cercania)` del texto ya limpio.

    * 6 dígitos contiguos: siempre candidatos (base 1.0).
    * `123 456` / `123-456`: solo si hay una palabra clave de verificación
      cerca (base 0.25), para no confundir teléfonos o referencias sueltas.
    """
    candidatos = []
    for coincidencia in _RE_CODIGO_CONTIGUO.finditer(texto):
        cercania = _puntaje_cercania(texto, coincidencia.start(), coincidencia.end())
        candidatos.append((coincidencia.start(), coincidencia.group(1), 1.0, cercania))
    for coincidencia in _RE_CODIGO_AGRUPADO.finditer(texto):
        cercania = _puntaje_cercania(texto, coincidencia.start(), coincidencia.end())
        if cercania:
            codigo = coincidencia.group(1) + coincidencia.group(2)
            candidatos.append((coincidencia.start(), codigo, 0.25, cercania))
    return candidatos


def extraer_codigo_de_texto(texto) -> str:
    """Primer/mejor código de 6 dígitos del texto (asunto o cuerpo). Nunca lanza.

    * Limpia HTML/entidades y acepta `bytes` (decodifica utf-8 con reemplazo).
    * Descarta números incrustados en secuencias más largas (ids, teléfonos),
      colores hex (`#123456`) y entidades.
    * Prioriza los códigos cercanos a palabras como "código/code/verificación/
      confirmation"; ante empate, el primero (el más arriba en el correo).
    * Devuelve `""` si no hay ningún código válido.
    """
    try:
        if texto is None:
            return ""
        if isinstance(texto, (bytes, bytearray)):
            texto = bytes(texto).decode("utf-8", "replace")
        elif not isinstance(texto, str):
            texto = str(texto)
        if not texto.strip():
            return ""

        limpio = _limpiar_html(texto)
        candidatos = _candidatos_codigo(limpio)
        if not candidatos:
            return ""

        def _puntos(candidato):
            _, _, base, cercania = candidato
            return base + 2.0 * cercania

        mejor = max(candidatos, key=lambda c: (_puntos(c), -c[0]))
        return mejor[1]
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Acceso IMAP (conexión, carpetas, mensajes)
# --------------------------------------------------------------------------- #
def _host_correo() -> str:
    """Host IMAP: `LECTOR_CORREO_HOST` o `imap.mail.yahoo.com`."""
    try:
        host = os.environ.get("LECTOR_CORREO_HOST", "").strip()
    except Exception:
        host = ""
    return host or HOST_POR_DEFECTO


def _puerto_correo() -> int:
    """Puerto IMAP SSL: `LECTOR_CORREO_PORT` o 993."""
    try:
        puerto = int(os.environ.get("LECTOR_CORREO_PORT", "") or PUERTO_POR_DEFECTO)
        return puerto if 0 < puerto < 65536 else PUERTO_POR_DEFECTO
    except (TypeError, ValueError):
        return PUERTO_POR_DEFECTO


def _cerrar_silencioso(cliente) -> None:
    """Cierra la conexión sin propagar errores (logout y shutdown tolerantes)."""
    if cliente is None:
        return
    try:
        cliente.logout()
        return
    except Exception:
        pass
    try:
        cliente.shutdown()
    except Exception:
        pass


def _conectar_y_entrar(host: str, usuario: str, password: str, timeout: int):
    """Abre IMAP4 SSL y hace login. Propaga la excepción original si falla."""
    cliente = imaplib.IMAP4_SSL(host, _puerto_correo(), timeout=timeout)
    try:
        cliente.login(usuario, password)
    except Exception:
        _cerrar_silencioso(cliente)
        raise
    return cliente


def _parece_error_credenciales(error) -> bool:
    """True si el error IMAP huele a usuario/contraseña rechazados."""
    texto = _sanear_texto(error).lower()
    return any(
        senal in texto
        for senal in (
            "authenticationfailed",
            "authentication failed",
            "invalid credentials",
            "invalid user",
            "invalid password",
            "login failed",
            "unauthorized",
            "auth",
        )
    )


def _a_texto(valor) -> str:
    """Convierte bytes/str a texto tolerante (utf-8 con reemplazo)."""
    if isinstance(valor, (bytes, bytearray)):
        return bytes(valor).decode("utf-8", "replace")
    return str(valor or "")


def _desescapar_carpeta(nombre: str) -> str:
    """Quita escapes IMAP del nombre de una carpeta."""
    return str(nombre or "").replace('\\"', '"').replace("\\\\", "\\").strip()


def _nombre_carpeta(entrada) -> str:
    """Extrae el nombre de una línea de `LIST` de IMAP.

    Formatos típicos:
        (\\HasNoChildren) "/" "INBOX"
        (\\HasNoChildren) "/" "Bulk Mail"
        (\\HasNoChildren) "/" INBOX
    """
    texto = _a_texto(entrada).strip()
    if not texto:
        return ""
    if texto.endswith('"'):
        coincidencias = re.findall(r'"((?:[^"\\]|\\.)*)"', texto)
        if coincidencias:
            return _desescapar_carpeta(coincidencias[-1])
        return ""
    return _desescapar_carpeta(texto.rsplit(" ", 1)[-1].strip().strip('"'))


def _es_carpeta_spam(nombre: str) -> bool:
    """True si el nombre de carpeta es (o suena a) spam/bulk/junk."""
    clave = _sin_acentos(nombre).lower().strip()
    if not clave:
        return False
    if any(clave == alias.lower() for alias in _CARPETAS_SPAM):
        return True
    return any(senal in clave for senal in ("spam", "junk", "bulk"))


def _carpetas_a_revisar(cliente) -> list:
    """INBOX primero y luego las carpetas de spam/bulk que existan.

    Si `LIST` no muestra ninguna carpeta de spam, se agregan los alias más
    comunes (Bulk/Spam/Junk/Bulk Mail...) para intentar seleccionarlos; los que
    no existan simplemente se omiten al seleccionar.
    """
    carpetas = ["INBOX"]
    vistas = {"inbox"}
    try:
        _, datos = cliente.list()
    except Exception:
        datos = []

    listadas = []
    for entrada in datos or []:
        nombre = _nombre_carpeta(entrada)
        if not nombre:
            continue
        listadas.append(nombre)
        clave = _sin_acentos(nombre).lower()
        if clave in vistas:
            continue
        if _es_carpeta_spam(nombre):
            carpetas.append(nombre)
            vistas.add(clave)

    if not any(_es_carpeta_spam(nombre) for nombre in listadas):
        for alias in _CARPETAS_SPAM:
            clave = alias.lower()
            if clave in vistas:
                continue
            carpetas.append(alias)
            vistas.add(clave)
    return carpetas


def _uids_recientes(cliente, limite: int = LIMITE_CORREOS) -> list:
    """UIDs de los últimos `limite` correos, en orden ASCENDENTE. Nunca lanza."""
    try:
        tipado, datos = cliente.uid("search", None, "ALL")
    except Exception:
        return []
    if tipado != "OK":
        return []
    texto = b" ".join(
        bytes(dato) for dato in (datos or []) if isinstance(dato, (bytes, bytearray))
    )
    uids = [int(token) for token in texto.split() if token.isdigit()]
    if limite > 0:
        return uids[-limite:]
    return uids


def _fetch_crudo(cliente, uid: int) -> bytes:
    """Mensaje completo del UID (BODY.PEEK[] para no marcarlo como leído)."""
    try:
        tipado, datos = cliente.uid("fetch", str(uid), "(BODY.PEEK[])")
    except Exception:
        return b""
    if tipado != "OK" or not datos:
        return b""

    trozos = []
    for item in datos:
        if isinstance(item, tuple):
            for parte in item[1:]:
                if isinstance(parte, (bytes, bytearray)):
                    trozos.append(bytes(parte))
        elif isinstance(item, (bytes, bytearray)):
            trozos.append(bytes(item))
    if not trozos:
        return b""
    return max(trozos, key=len)


def _decodificar_cabecera(valor) -> str:
    """Decodifica una cabecera RFC2047 (`=?utf-8?...?=`) a texto plano."""
    if valor is None:
        return ""
    if isinstance(valor, (bytes, bytearray)):
        valor = _a_texto(valor)
    try:
        return str(make_header(decode_header(str(valor)))).strip()
    except Exception:
        return _a_texto(valor).strip()


def _decodificar_parte(parte) -> str:
    """Texto de una parte MIME (text/plain o text/html), tolerante a charset."""
    try:
        carga = parte.get_payload(decode=True)
    except Exception:
        carga = None
    if carga is None:
        try:
            crudo = parte.get_payload()
        except Exception:
            return ""
        return crudo if isinstance(crudo, str) else ""

    charsets = []
    try:
        charsets.append(parte.get_content_charset())
    except Exception:
        pass
    charsets.extend(["utf-8", "latin-1"])
    for charset in charsets:
        if not charset:
            continue
        try:
            return carga.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return carga.decode("utf-8", errors="replace")


def _cuerpo_mensaje(msg) -> str:
    """Concatena las partes de texto (plain+html) de un mensaje. Nunca lanza."""
    partes = []
    try:
        if msg.is_multipart():
            for parte in msg.walk():
                try:
                    if parte.is_multipart():
                        continue
                    if parte.get_content_type() in ("text/plain", "text/html"):
                        texto = _decodificar_parte(parte)
                        if texto:
                            partes.append(texto)
                except Exception:
                    continue
        else:
            texto = _decodificar_parte(msg)
            if texto:
                partes.append(texto)
    except Exception:
        pass
    return "\n".join(partes)


def _datos_mensaje(crudo: bytes) -> tuple:
    """(remitente, asunto, cuerpo, fecha) de un RFC822 crudo. Nunca lanza."""
    try:
        msg = message_from_bytes(crudo)
    except Exception:
        return "", "", "", ""
    remitente = _decodificar_cabecera(msg.get("From", ""))
    asunto = _decodificar_cabecera(msg.get("Subject", ""))
    fecha = _decodificar_cabecera(msg.get("Date", ""))
    cuerpo = _cuerpo_mensaje(msg)
    return remitente, asunto, cuerpo, fecha


def _puntos_preferencia(remitente: str, asunto: str) -> int:
    """Puntaje del correo: remitentes X/Twitter valen 3, asuntos de código 1."""
    remitente_norm = _sin_acentos(remitente).lower()
    asunto_norm = _sin_acentos(asunto).lower()
    puntos = 3 * sum(1 for clave in _CLAVES_REMITENTE if clave in remitente_norm)
    puntos += sum(1 for clave in _CLAVES_ASUNTO if clave in asunto_norm)
    return puntos


def _restante(inicio: float, timeout: int) -> float:
    """Segundos que quedan del presupuesto total (nunca negativo)."""
    return max(0.0, float(timeout) - (time.monotonic() - inicio))


def _buscar_en_carpetas(cliente, inicio: float, timeout: int) -> tuple:
    """Recorre INBOX y spam buscando el mejor correo con código.

    Devuelve `(encontrado, agotado)`:
    * `encontrado` = dict `{codigo, remitente, asunto, fecha, puntos}` o None.
    * `agotado` = True si se terminó el presupuesto de tiempo.
    """
    for carpeta in _carpetas_a_revisar(cliente):
        if _restante(inicio, timeout) <= 0:
            return None, True
        try:
            tipado, _ = cliente.select(carpeta, readonly=True)
        except Exception:
            continue
        if tipado != "OK":
            continue

        mejor = None
        for uid in reversed(_uids_recientes(cliente, LIMITE_CORREOS)):
            if _restante(inicio, timeout) <= 0:
                return (mejor, False) if mejor is not None else (None, True)
            crudo = _fetch_crudo(cliente, uid)
            if not crudo:
                continue
            remitente, asunto, cuerpo, fecha = _datos_mensaje(crudo)
            codigo = extraer_codigo_de_texto(asunto + "\n" + cuerpo)
            if not codigo:
                continue
            puntos = _puntos_preferencia(remitente, asunto)
            if mejor is None or puntos > mejor["puntos"]:
                mejor = {
                    "codigo": codigo,
                    "remitente": remitente,
                    "asunto": asunto,
                    "fecha": fecha,
                    "puntos": puntos,
                }
        if mejor is not None:
            return mejor, False
    return None, False


# --------------------------------------------------------------------------- #
# API pública
# --------------------------------------------------------------------------- #
def obtener_codigo_verificacion(email: str, password: str, timeout: int = 25) -> dict:
    """Busca el último código de 6 dígitos en el correo IMAP de la cuenta.

    Devuelve SIEMPRE el dict del contrato
    (`ok/codigo/remitente/asunto/fecha/error`) y NUNCA lanza. Los errores son
    descriptivos en español y NUNCA incluyen la contraseña:

    * login rechazado: "usuario o contraseña incorrectos... puede requerir
      contraseña de aplicación de Yahoo";
    * timeout / conexión: mensajes explícitos con el host;
    * sin códigos: "no encontré ningún código en los últimos correos (...)".

    `timeout` es el presupuesto total (segundos) y también el timeout de socket
    de cada operación IMAP. El host sale de `LECTOR_CORREO_HOST` (default
    `imap.mail.yahoo.com`).
    """
    correo = str(email or "").strip()
    clave = str(password or "")
    if not correo or not clave:
        return _respuesta(error="Falta el correo o la contraseña de la cuenta.")

    try:
        limite = max(1, int(timeout or 25))
    except (TypeError, ValueError):
        limite = 25

    host = _host_correo()
    inicio = time.monotonic()
    cliente = None
    logger.debug("lector_correo: revisando %s en %s", correo, host)
    try:
        try:
            cliente = _conectar_y_entrar(host, correo, clave, limite)
        except socket.timeout:
            return _respuesta(
                error=f"se agotó el tiempo de espera conectando al correo ({host}, {limite}s)."
            )
        except imaplib.IMAP4.error as error:
            if _parece_error_credenciales(error):
                return _respuesta(
                    error=(
                        "usuario o contraseña incorrectos en el correo (el servidor "
                        "rechazó el login); en Yahoo puede requerir una contraseña de "
                        "aplicación."
                    )
                )
            return _respuesta(
                error=(
                    f"el servidor de correo rechazó la conexión "
                    f"({_sanear_texto(error, clave) or type(error).__name__})."
                )
            )
        except (socket.gaierror, ConnectionError, OSError) as error:
            detalle = _sanear_texto(error, clave) or type(error).__name__
            return _respuesta(
                error=f"no pude conectar al servidor de correo {host} ({detalle})."
            )
        except Exception as error:
            detalle = _sanear_texto(error, clave) or type(error).__name__
            return _respuesta(
                error=f"error inesperado conectando al correo ({type(error).__name__}: {detalle})."
            )

        encontrado, agotado = _buscar_en_carpetas(cliente, inicio, limite)
        if encontrado is not None:
            return _respuesta(
                ok=True,
                codigo=encontrado["codigo"],
                remitente=encontrado["remitente"],
                asunto=encontrado["asunto"],
                fecha=encontrado["fecha"],
            )
        if agotado:
            return _respuesta(
                error=f"se agotó el tiempo de espera revisando el correo ({host}, {limite}s)."
            )
        return _respuesta(
            error=(
                "no encontré ningún código en los últimos correos "
                f"(revisé hasta {LIMITE_CORREOS} mensajes de INBOX/Spam en {host})."
            )
        )
    except Exception as error:
        detalle = _sanear_texto(error, clave) or type(error).__name__
        return _respuesta(
            error=f"error inesperado revisando el correo ({type(error).__name__}: {detalle})."
        )
    finally:
        _cerrar_silencioso(cliente)


def diagnosticar_conexion(email: str, password: str, timeout: int = 15) -> dict:
    """Prueba login + listado/lectura de correos (soporte). NUNCA lanza.

    Devuelve `{"ok", "host", "carpetas", "mensajes", "tiempo_seg", "error"}`:
    * `carpetas`: nombres de buzones que expone el servidor (los primeros 50).
    * `mensajes`: cuántos correos hay en INBOX (lectura de solo conteo).
    * `error`: mensaje en español sin la contraseña si algo falla.
    """
    host = _host_correo()
    inicio = time.monotonic()

    def _salida(ok: bool, carpetas=None, mensajes: int = 0, error: str = "") -> dict:
        return {
            "ok": bool(ok),
            "host": host,
            "carpetas": list(carpetas or []),
            "mensajes": int(mensajes or 0),
            "tiempo_seg": round(time.monotonic() - inicio, 2),
            "error": str(error or ""),
        }

    correo = str(email or "").strip()
    clave = str(password or "")
    if not correo or not clave:
        return _salida(False, error="Falta el correo o la contraseña de la cuenta.")

    try:
        limite = max(1, int(timeout or 15))
    except (TypeError, ValueError):
        limite = 15

    cliente = None
    try:
        try:
            cliente = _conectar_y_entrar(host, correo, clave, limite)
        except socket.timeout:
            return _salida(False, error=f"se agotó el tiempo de espera conectando al correo ({host}, {limite}s).")
        except imaplib.IMAP4.error as error:
            if _parece_error_credenciales(error):
                return _salida(
                    False,
                    error=(
                        "usuario o contraseña incorrectos en el correo (el servidor "
                        "rechazó el login); en Yahoo puede requerir una contraseña de "
                        "aplicación."
                    ),
                )
            return _salida(
                False,
                error=f"el servidor de correo rechazó la conexión ({_sanear_texto(error, clave) or type(error).__name__}).",
            )
        except (socket.gaierror, ConnectionError, OSError) as error:
            detalle = _sanear_texto(error, clave) or type(error).__name__
            return _salida(False, error=f"no pude conectar al servidor de correo {host} ({detalle}).")
        except Exception as error:
            detalle = _sanear_texto(error, clave) or type(error).__name__
            return _salida(False, error=f"error inesperado conectando al correo ({type(error).__name__}: {detalle}).")

        carpetas = []
        try:
            _, datos = cliente.list()
            for entrada in datos or []:
                nombre = _nombre_carpeta(entrada)
                if nombre and nombre not in carpetas:
                    carpetas.append(nombre)
        except Exception:
            pass

        mensajes = 0
        try:
            tipado, _ = cliente.select("INBOX", readonly=True)
            if tipado == "OK":
                tipado, datos = cliente.uid("search", None, "ALL")
                if tipado == "OK":
                    mensajes = len(_uids_recientes(cliente, 0))
        except Exception:
            pass

        return _salida(True, carpetas=carpetas[:50], mensajes=mensajes)
    except Exception as error:
        detalle = _sanear_texto(error, clave) or type(error).__name__
        return _salida(False, error=f"error inesperado revisando el correo ({type(error).__name__}: {detalle}).")
    finally:
        _cerrar_silencioso(cliente)
