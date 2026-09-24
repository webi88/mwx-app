"""Registro GLOBAL del bot de clientes (`data/clientes_bot.json`).

Estructura NUEVA (cuentas globales del grupo de clientes)::

    {"chat_id": -1005538610567,
     "cuentas": ["usuario1", "usuario2", ...]}

El bot funciona SOLO en ese grupo: cualquier miembro puede elegir cualquiera de
las cuentas. El archivo YA NO guarda Telegram IDs ni nombres de clientes.

Migracion automatica: si encuentra el formato viejo
`{"clientes": {"<telegram_id>": {"nombre": ..., "cuentas": [...]}}}` une las
cuentas UNICAS de todos los clientes, descarta los IDs/nombres y reescribe el
archivo en el formato nuevo.

Reglas:
  - NUNCA guarda credenciales: solo el chat permitido y los usuarios de X.
  - `guardar()` es atomico (archivo temporal + `os.replace`).
  - Ninguna funcion lanza excepcion: los errores se devuelven como string
    ("" = todo bien). `cargar()` siempre devuelve un dict valido y crea el
    archivo con las cuentas por defecto si no existe.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Chat de Telegram del grupo de clientes (fuente: TELEGRAM_CLIENTES_CHAT_ID).
CHAT_CLIENTES_DEFAULT = -1005538610567

# Cuentas por defecto (fuente historica: data/clientes/a_importar.txt). Se usan
# para sembrar el archivo cuando no existe (p. ej. deploy nuevo en Railway) y
# no contienen credenciales.
CUENTAS_DEFAULT = [
    "3ranuii97",
    "SoLikeShady",
    "Samia_lovesyou",
    "LuxuryMachine",
    "PinkLipStick16",
    "BAMsugar96",
    "HardisonRichard",
    "yungbillyz",
    "sir_portugal",
    "kristy63411720",
    "gadams_gene",
    "estybaby8",
    "_Campos_7",
    "BuchholzLacey",
    "Kaitlyn26014743",
]

CLAVE_CHAT = "chat_id"
CLAVE_CUENTAS = "cuentas"
CLAVE_VIEJA = "clientes"  # formato anterior (registro por Telegram ID)
VARIABLE_CHAT = "TELEGRAM_CLIENTES_CHAT_ID"

# Ruta alternativa (pruebas / embedding). Cadena vacia = ruta real del proyecto.
_RUTA_OVERRIDE = ""


# --------------------------------------------------------------------------- #
# Rutas y utilidades internas
# --------------------------------------------------------------------------- #

def _ruta_default() -> str:
    """Ruta real `data/clientes_bot.json` (absoluta, sin depender del CWD)."""
    try:
        from core.config import resolver_ruta

        return resolver_ruta("data/clientes_bot.json")
    except Exception:
        raiz = Path(__file__).resolve().parent.parent
        return str((raiz / "data" / "clientes_bot.json").resolve())


def ruta_actual() -> str:
    """Ruta que usan las funciones cuando no se les pasa una explicita."""
    return _RUTA_OVERRIDE or _ruta_default()


def usar_ruta(ruta: str) -> None:
    """Fija una ruta alternativa (pruebas). Cadena vacia = volver a la real."""
    global _RUTA_OVERRIDE
    _RUTA_OVERRIDE = str(ruta or "").strip()


def limpiar_usuario(usuario) -> str:
    """Quita espacios y '@' de un usuario de X."""
    return str(usuario or "").strip().lstrip("@").strip()


def limpiar_usuarios(usuarios) -> list:
    """Limpia '@'/espacios, descarta vacios y deduplica (case-insensitive).

    Acepta lista/tupla/set (o un string suelto, que se trata como una sola
    cuenta para no iterar sus letras).
    """
    if isinstance(usuarios, str):
        usuarios = [usuarios]
    vistos = set()
    limpios = []
    for usuario in usuarios or []:
        nombre = limpiar_usuario(usuario)
        if not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        limpios.append(nombre)
    return limpios


def _normalizar_chat_id(valor):
    """Chat ID como int (acepta negativos). None si no es valido."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


def _chat_de_entorno():
    """Chat ID configurado por env `TELEGRAM_CLIENTES_CHAT_ID` (o None)."""
    return _normalizar_chat_id(os.getenv(VARIABLE_CHAT, ""))


# --------------------------------------------------------------------------- #
# Estructura / migracion
# --------------------------------------------------------------------------- #

def _estructura_vacia() -> dict:
    return {
        CLAVE_CHAT: CHAT_CLIENTES_DEFAULT,
        CLAVE_CUENTAS: list(CUENTAS_DEFAULT),
    }


def _normalizar_datos(datos) -> dict:
    """Deja `datos` en el formato nuevo `{"chat_id", "cuentas"}`."""
    if not isinstance(datos, dict):
        return _estructura_vacia()
    chat = _normalizar_chat_id(datos.get(CLAVE_CHAT))
    if chat is None:
        chat = CHAT_CLIENTES_DEFAULT
    cuentas = limpiar_usuarios(datos.get(CLAVE_CUENTAS) or [])
    return {CLAVE_CHAT: chat, CLAVE_CUENTAS: cuentas}


def _migrar_formato_viejo(datos) -> tuple:
    """Convierte `{"clientes": {...}}` al formato nuevo.

    Devuelve `(datos_nuevos, hubo_migracion)`: une las cuentas UNICAS de todos
    los clientes (case-insensitive, conservando el primer nombre) y descarta
    los Telegram IDs/nombres de cliente.
    """
    if not isinstance(datos, dict) or CLAVE_VIEJA not in datos:
        return _normalizar_datos(datos), False
    clientes = datos.get(CLAVE_VIEJA)
    unidas = []
    if isinstance(clientes, dict):
        for info in clientes.values():
            if isinstance(info, dict):
                unidas.extend(info.get(CLAVE_CUENTAS) or [])
            elif isinstance(info, list):
                unidas.extend(info)
    chat = _normalizar_chat_id(datos.get(CLAVE_CHAT))
    if chat is None:
        chat = CHAT_CLIENTES_DEFAULT
    return {
        CLAVE_CHAT: chat,
        CLAVE_CUENTAS: limpiar_usuarios(unidas) or list(CUENTAS_DEFAULT),
    }, True


# --------------------------------------------------------------------------- #
# Lectura / escritura
# --------------------------------------------------------------------------- #

def cargar(ruta: str = "") -> dict:
    """Lee el registro (migrando el formato viejo). Nunca lanza.

    Si el archivo no existe lo crea con el chat permitido y las cuentas por
    defecto. Si detecta el formato viejo (`{"clientes": ...}`) lo migra y lo
    reescribe en el formato nuevo.
    """
    destino = ruta or ruta_actual()
    try:
        if not os.path.exists(destino):
            datos = _estructura_vacia()
            guardar(datos, destino)
            return datos
        with open(destino, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
        nuevos, migrado = _migrar_formato_viejo(datos)
        nuevos = _normalizar_datos(nuevos)
        if migrado:
            guardar(nuevos, destino)
        return nuevos
    except Exception:
        return _estructura_vacia()


def guardar(datos: dict, ruta: str = "") -> str:
    """Escribe el registro de forma atomica. Devuelve "" o el error."""
    destino = ruta or ruta_actual()
    try:
        datos = _normalizar_datos(datos)
        carpeta = os.path.dirname(destino) or "."
        os.makedirs(carpeta, exist_ok=True)
        contenido = json.dumps(datos, ensure_ascii=False, indent=2, sort_keys=True)
        fd, temporal = tempfile.mkstemp(
            prefix=".clientes_bot_", suffix=".tmp", dir=carpeta
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(contenido)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporal, destino)
        except Exception:
            try:
                os.unlink(temporal)
            except OSError:
                pass
            raise
        return ""
    except Exception as e:
        return f"No se pudo guardar {destino}: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Consultas
# --------------------------------------------------------------------------- #

def chat_id(ruta: str = "") -> int:
    """Chat permitido: env -> JSON -> default (-1005538610567)."""
    de_entorno = _chat_de_entorno()
    if de_entorno is not None:
        return de_entorno
    return int(cargar(ruta).get(CLAVE_CHAT, CHAT_CLIENTES_DEFAULT))


def cuentas(ruta: str = "") -> list:
    """Cuentas GLOBALES del grupo (lista limpia y sin duplicados)."""
    return limpiar_usuarios(cargar(ruta).get(CLAVE_CUENTAS) or [])


def es_chat_permitido(chat, ruta: str = "") -> bool:
    """True si `chat` es el grupo de clientes configurado."""
    valor = _normalizar_chat_id(chat)
    if valor is None:
        return False
    return valor == chat_id(ruta)
