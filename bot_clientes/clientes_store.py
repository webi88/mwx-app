"""Registro de clientes del bot externo (`data/clientes_bot.json`).

Estructura del archivo::

    {"clientes": {"<telegram_id>": {"nombre": "Cliente A",
                                    "cuentas": ["usuario1", "usuario2"]}}}

Reglas:
  - El archivo NUNCA guarda credenciales: solo el nombre del cliente, su
    Telegram ID y los usuarios internos de sus cuentas de X.
  - `guardar()` es atomico (archivo temporal + `os.replace`): un corte a
    mitad de escritura no corrompe el registro.
  - NINGUNA funcion lanza excepcion: los errores se devuelven como string
    ("" = todo bien). `cargar()` siempre devuelve un dict valido y crea el
    archivo vacio si no existe.
  - Las mutaciones (`asignar`, `quitar`) devuelven "" si todo salio bien o el
    mensaje de error listo para mostrar en Telegram.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

CLAVE_CLIENTES = "clientes"

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


def _normalizar_id(telegram_id) -> str:
    """Telegram ID como string de digitos, sin signos ni espacios ("" si malo)."""
    if telegram_id is None:
        return ""
    texto = str(telegram_id).strip()
    if not texto:
        return ""
    try:
        numero = int(texto)
    except (TypeError, ValueError):
        return ""
    if numero <= 0:
        return ""
    return str(numero)


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


def _estructura_vacia() -> dict:
    return {CLAVE_CLIENTES: {}}


def _normalizar_datos(datos) -> dict:
    """Deja `datos` con la estructura esperada y valores limpios."""
    if not isinstance(datos, dict):
        return _estructura_vacia()
    clientes = datos.get(CLAVE_CLIENTES)
    if not isinstance(clientes, dict):
        return _estructura_vacia()
    limpios = {}
    for telegram_id, info in clientes.items():
        tid = _normalizar_id(telegram_id)
        if not tid:
            continue
        if not isinstance(info, dict):
            info = {}
        nombre = re.sub(r"\s+", " ", str(info.get("nombre") or "")).strip()
        limpios[tid] = {
            "nombre": nombre or f"Cliente {tid}",
            "cuentas": limpiar_usuarios(info.get("cuentas") or []),
        }
    return {CLAVE_CLIENTES: limpios}


# --------------------------------------------------------------------------- #
# Lectura / escritura
# --------------------------------------------------------------------------- #

def cargar(ruta: str = "") -> dict:
    """Lee el registro; crea el archivo vacio si no existe. Nunca lanza."""
    destino = ruta or ruta_actual()
    try:
        if not os.path.exists(destino):
            datos = _estructura_vacia()
            guardar(datos, destino)
            return datos
        with open(destino, "r", encoding="utf-8") as fh:
            datos = json.load(fh)
        return _normalizar_datos(datos)
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

def cliente_de(telegram_id, ruta: str = "") -> dict | None:
    """Datos del cliente (`{"nombre", "cuentas"}`) o None si no esta."""
    tid = _normalizar_id(telegram_id)
    if not tid:
        return None
    info = cargar(ruta).get(CLAVE_CLIENTES, {}).get(tid)
    if not isinstance(info, dict):
        return None
    return {
        "nombre": str(info.get("nombre") or "").strip() or f"Cliente {tid}",
        "cuentas": limpiar_usuarios(info.get("cuentas") or []),
    }


def cuentas_de(telegram_id, ruta: str = "") -> list:
    """Usuarios asignados al cliente ([] si no esta registrado)."""
    info = cliente_de(telegram_id, ruta)
    return list(info.get("cuentas") or []) if info else []


def todos_los_clientes(ruta: str = "") -> dict:
    """Copia `{telegram_id: {"nombre", "cuentas"}}` de todo el registro."""
    return {tid: dict(info) for tid, info in cargar(ruta).get(CLAVE_CLIENTES, {}).items()}


def es_cliente(telegram_id, ruta: str = "") -> bool:
    """True si el Telegram ID esta registrado."""
    return cliente_de(telegram_id, ruta) is not None


def usuario_permitido(telegram_id, usuario, ruta: str = "") -> bool:
    """True si `usuario` pertenece a las cuentas del cliente (case-insensitive)."""
    buscado = limpiar_usuario(usuario).lower()
    if not buscado:
        return False
    return buscado in {u.lower() for u in cuentas_de(telegram_id, ruta)}


# --------------------------------------------------------------------------- #
# Mutaciones
# --------------------------------------------------------------------------- #

def asignar(
    telegram_id, nombre: str = "", usuarios=None, ruta: str = ""
) -> str:
    """Registra/actualiza un cliente y AGREGA cuentas (no borra las previas).

    Devuelve "" si todo salio bien o el mensaje de error. El nombre solo se
    actualiza si viene no vacio; si no, conserva el anterior (o "Cliente <id>").
    """
    tid = _normalizar_id(telegram_id)
    if not tid:
        return "Telegram ID inválido: usa solo números (el ID de la persona)."
    usuarios_limpios = limpiar_usuarios(usuarios)
    nombre_limpio = re.sub(r"\s+", " ", str(nombre or "")).strip()
    if not usuarios_limpios and not nombre_limpio:
        return "No hay usuarios válidos para asignar ni nombre para actualizar."

    datos = cargar(ruta)
    clientes = datos.setdefault(CLAVE_CLIENTES, {})
    info = clientes.get(tid)
    if not isinstance(info, dict):
        info = {}
    cuentas = limpiar_usuarios(list(info.get("cuentas") or []) + usuarios_limpios)
    nombre_final = (
        nombre_limpio
        or str(info.get("nombre") or "").strip()
        or f"Cliente {tid}"
    )
    clientes[tid] = {"nombre": nombre_final, "cuentas": cuentas}
    return guardar(datos, ruta)


def quitar(telegram_id, usuarios=None, ruta: str = "") -> str:
    """Quita usuarios de un cliente. Devuelve "" o el mensaje de error."""
    tid = _normalizar_id(telegram_id)
    if not tid:
        return "Telegram ID inválido: usa solo números (el ID de la persona)."
    a_quitar = {u.lower() for u in limpiar_usuarios(usuarios)}
    if not a_quitar:
        return "No hay usuarios válidos para quitar."

    datos = cargar(ruta)
    clientes = datos.get(CLAVE_CLIENTES, {})
    info = clientes.get(tid)
    if not isinstance(info, dict):
        return f"El cliente {tid} no está registrado."
    restantes = [
        usuario
        for usuario in (info.get("cuentas") or [])
        if str(usuario).lower() not in a_quitar
    ]
    clientes[tid] = {
        "nombre": str(info.get("nombre") or "").strip() or f"Cliente {tid}",
        "cuentas": restantes,
    }
    return guardar(datos, ruta)
