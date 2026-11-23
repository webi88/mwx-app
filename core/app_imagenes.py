# -*- coding: utf-8 -*-
"""Imagenes de campaña del MWX APP almacenadas en la base de datos.

La API del ejecutable corre en un contenedor distinto al del dashboard y NO
comparte su disco, por lo que las imagenes no pueden depender de
`data/mwx_app/<usuario>/imagenes/`: el binario se guarda en la tabla
`app_imagen` (`LargeBinary`) junto al usuario dueño.

Reglas de esta capa:

- **No hace commit**: el llamador decide cuando confirmar (`db.commit()`); aqui
  solo se hace `flush()` para detectar errores y asignar el `id`.
- **Upsert por (usuario_id, nombre)**: subir otra vez el mismo nombre ACTUALIZA
  contenido/tipo/tamaño/fecha (no duplica filas).
- **Tolerante**: nombres invalidos, usuarios raros, bytes gigantes, sesiones
  rotas, etc. devuelven `None`/`False`/`[]` en vez de lanzar.
- Solo SQLAlchemy + stdlib (sin FastAPI, sin Streamlit, sin loguru).

El limite de tamaño se lee de `MWX_IMAGEN_MAX_MB` (en MB, admite decimales),
con default 8 MB.
"""
from __future__ import annotations

import os
import re
from datetime import datetime

from sqlalchemy import select

from core.models import AppImagen

# Nombre de archivo permitido: 1-120 caracteres ASCII, sin rutas. Es el mismo
# patron que valida la API (`/v1/imagenes/{nombre}`) y el dashboard.
NOMBRE_SEGURO_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

# Tipo por defecto para extensiones desconocidas o nombres invalidos.
CONTENT_TYPE_POR_DEFECTO = "application/octet-stream"

# Limite por defecto (MB). Se puede sobreescribir con la variable de entorno
# `MWX_IMAGEN_MAX_MB` (o monkeypatcheando este modulo en pruebas).
MWX_IMAGEN_MAX_MB = 8

_TIPOS_POR_EXTENSION = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


# ---------------------------------------------------------------------------
# Nombres, extensiones y tipos MIME
# ---------------------------------------------------------------------------
def nombre_seguro(nombre) -> str:
    """Devuelve el nombre saneado o "" si no es un nombre de archivo valido.

    Rechaza cualquier valor que no sea `str`, los vacios, los que contienen
    `/`, `\\` o `..` (traversal) y los que no cumplen `NOMBRE_SEGURO_RE`.
    Nunca lanza.
    """
    try:
        if not isinstance(nombre, str):
            return ""
        if not nombre or nombre in (".", ".."):
            return ""
        if "/" in nombre or "\\" in nombre or ".." in nombre:
            return ""
        if not NOMBRE_SEGURO_RE.fullmatch(nombre):
            return ""
        return nombre
    except Exception:
        return ""


def extension_de(nombre) -> str:
    """Extension en minusculas SIN punto (`foto.JPG` -> `"jpg"`); "" si no hay.

    Un nombre invalido (ruta/traversal) devuelve "".
    """
    try:
        texto = nombre_seguro(nombre)
        if not texto or "." not in texto:
            return ""
        return texto.rsplit(".", 1)[-1].lower()
    except Exception:
        return ""


def content_type_de(nombre) -> str:
    """MIME de la imagen segun su extension; default `application/octet-stream`.

    Solo reconoce png/jpg/jpeg/webp/gif (lo mismo que acepta la campana).
    """
    try:
        return _TIPOS_POR_EXTENSION.get(
            extension_de(nombre), CONTENT_TYPE_POR_DEFECTO
        )
    except Exception:
        return CONTENT_TYPE_POR_DEFECTO


# ---------------------------------------------------------------------------
# Limite de tamaño
# ---------------------------------------------------------------------------
def limite_max_bytes() -> int:
    """Limite de tamaño en bytes (`MWX_IMAGEN_MAX_MB`, default 8 MB).

    La variable de entorno manda sobre el default del modulo; valores vacios,
    no numericos o <= 0 se ignoran. Admite decimales para poder probar con
    limites pequenos (p. ej. `MWX_IMAGEN_MAX_MB=0.001`).
    """
    megas = MWX_IMAGEN_MAX_MB
    crudo = os.environ.get("MWX_IMAGEN_MAX_MB")
    if crudo is not None and str(crudo).strip() != "":
        try:
            valor_env = float(str(crudo).strip())
            if valor_env > 0:
                megas = valor_env
        except (TypeError, ValueError):
            pass
    try:
        valor = float(megas)
    except (TypeError, ValueError):
        valor = 8.0
    if valor <= 0:
        valor = 8.0
    try:
        return int(valor * 1024 * 1024)
    except (OverflowError, ValueError):
        return 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Utilidades internas tolerantes
# ---------------------------------------------------------------------------
def _entero_positivo(valor) -> int:
    """Convierte a int > 0 o devuelve 0 (para usuarios/ids invalidos)."""
    try:
        numero = int(valor)
    except (TypeError, ValueError, OverflowError):
        return 0
    return numero if numero > 0 else 0


def _bytes_de(contenido) -> bytes | None:
    """Normaliza bytes/bytearray/memoryview; None para cualquier otro tipo."""
    try:
        if isinstance(contenido, bytes):
            return contenido
        if isinstance(contenido, (bytearray, memoryview)):
            return bytes(contenido)
    except Exception:
        return None
    return None


def _tipo_normalizado(content_type, nombre: str) -> str:
    """Usa el content_type recibido (recortado a 80) o lo deduce por extension."""
    try:
        if isinstance(content_type, str):
            tipo = content_type.strip()
            if tipo:
                return tipo[:80]
    except Exception:
        pass
    return content_type_de(nombre)


def _rollback_seguro(db) -> None:
    """Hace rollback sin propagar errores (sesion rota o inexistente)."""
    try:
        db.rollback()
    except Exception:
        pass


def _buscar(db, usuario_id: int, nombre: str):
    """Busca la fila exacta (usuario_id, nombre) o None."""
    return (
        db.execute(
            select(AppImagen).where(
                AppImagen.usuario_id == usuario_id,
                AppImagen.nombre == nombre,
            )
        )
        .scalars()
        .first()
    )


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------
def guardar_imagen(
    db,
    usuario_id,
    nombre,
    contenido: bytes,
    content_type: str = "",
) -> AppImagen | None:
    """Guarda (o reemplaza) una imagen del usuario en `app_imagen`.

    - Valida el nombre (`nombre_seguro`) y que el contenido sea bytes-like.
    - Rechaza contenido mayor a `limite_max_bytes()`.
    - Si ya existe (usuario_id, nombre) actualiza contenido, content_type,
      tamano y `creado`; si no, inserta una fila nueva.
    - `content_type` vacio se deduce por la extension (png/jpg/jpeg/webp/gif).
    - Hace `db.flush()` pero **NO commit**: el llamador confirma.

    Devuelve el `AppImagen` guardado o None si los datos no son validos.
    """
    try:
        texto = nombre_seguro(nombre)
        uid = _entero_positivo(usuario_id)
        if db is None or not texto or not uid:
            return None

        datos = _bytes_de(contenido)
        if datos is None:
            return None
        if len(datos) > limite_max_bytes():
            return None

        tipo = _tipo_normalizado(content_type, texto)
        imagen = _buscar(db, uid, texto)
        if imagen is None:
            imagen = AppImagen(
                usuario_id=uid,
                nombre=texto,
                contenido=datos,
                content_type=tipo,
                tamano=len(datos),
                creado=datetime.utcnow(),
            )
            db.add(imagen)
        else:
            imagen.contenido = datos
            imagen.content_type = tipo
            imagen.tamano = len(datos)
            imagen.creado = datetime.utcnow()

        db.flush()
        return imagen
    except Exception:
        # Un flush fallido deja la sesion en estado inconsistente: se limpia
        # para que el llamador pueda seguir (no se hace commit de nada).
        _rollback_seguro(db)
        return None


def leer_imagen(db, usuario_id, nombre) -> dict | None:
    """Devuelve `{"nombre","contenido","content_type","tamano"}` o None.

    No lanza: nombre/usuario invalidos, fila inexistente o error de BD -> None.
    """
    try:
        texto = nombre_seguro(nombre)
        uid = _entero_positivo(usuario_id)
        if db is None or not texto or not uid:
            return None

        imagen = _buscar(db, uid, texto)
        if imagen is None:
            return None

        contenido = _bytes_de(imagen.contenido)
        if contenido is None:
            contenido = b""
        return {
            "nombre": str(imagen.nombre or ""),
            "contenido": contenido,
            "content_type": str(
                imagen.content_type or CONTENT_TYPE_POR_DEFECTO
            ),
            "tamano": int(imagen.tamano or 0),
        }
    except Exception:
        return None


def listar_imagenes(db, usuario_id) -> list[dict]:
    """Lista las imagenes del usuario SIN contenido (metadatos para la UI).

    Cada item: `{"nombre","content_type","tamano","creado"}`. Orden de
    insercion (id ascendente). Ante cualquier error devuelve `[]`.
    """
    try:
        uid = _entero_positivo(usuario_id)
        if db is None or not uid:
            return []

        filas = (
            db.execute(
                select(AppImagen)
                .where(AppImagen.usuario_id == uid)
                .order_by(AppImagen.id.asc())
            )
            .scalars()
            .all()
        )
        return [
            {
                "nombre": str(imagen.nombre or ""),
                "content_type": str(
                    imagen.content_type or CONTENT_TYPE_POR_DEFECTO
                ),
                "tamano": int(imagen.tamano or 0),
                "creado": imagen.creado,
            }
            for imagen in filas
        ]
    except Exception:
        return []


def eliminar_imagen(db, usuario_id, nombre) -> bool:
    """Borra la imagen (usuario_id, nombre). True si existia, False si no.

    Hace `db.flush()` pero NO commit. Nunca lanza.
    """
    try:
        texto = nombre_seguro(nombre)
        uid = _entero_positivo(usuario_id)
        if db is None or not texto or not uid:
            return False

        imagen = _buscar(db, uid, texto)
        if imagen is None:
            return False

        db.delete(imagen)
        db.flush()
        return True
    except Exception:
        _rollback_seguro(db)
        return False


__all__ = [
    "NOMBRE_SEGURO_RE",
    "CONTENT_TYPE_POR_DEFECTO",
    "MWX_IMAGEN_MAX_MB",
    "nombre_seguro",
    "extension_de",
    "content_type_de",
    "limite_max_bytes",
    "guardar_imagen",
    "leer_imagen",
    "listar_imagenes",
    "eliminar_imagen",
]
