# -*- coding: utf-8 -*-
"""Releases del ejecutable MWX APP (descarga + actualizacion remota).

El ejecutable vive en la maquina del cliente, pero el paquete publicado se
guarda en la BASE DE DATOS (tabla `app_releases`) para que la API lo sirva sin
depender de un disco compartido: `contenido` viaja como `LargeBinary`
(BLOB/BYTEA).

Reglas de esta capa (mismo estilo defensivo que `core/app_imagenes.py`):

- **Ninguna funcion lanza**: ante cualquier dato raro devuelve un dict con
  `ok=False` (o `disponible=False`) y un `error` claro.
- **Metadatos sin contenido**: `ultimo_release`, `listar_releases` y
  `empaquetar_dist` usan SELECT sin la columna `contenido` (evita cargar el ZIP
  completo de ~34 MB); solo `leer_release` lo carga.
- **UPSERT por version**: publicar una version existente la REEMPLAZA.
- **Plataformas sin migracion de esquema**: la columna `version` es UNIQUE y
  lleva el sufijo de la plataforma (`1.1.4` = Windows, `1.1.4-mac-arm64`,
  `1.1.4-mac-intel`). `plataforma_de_version` deduce la plataforma del texto.
- **Retencion**: al publicar se conservan solo los 2 releases mas recientes por
  `id` DE CADA PLATAFORMA (windows, mac-arm64, mac-intel, mac); publicar un Mac
  nunca borra el release vigente de Windows (nunca se borra el recien
  publicado).
- Solo SQLAlchemy + stdlib (sin FastAPI, sin Streamlit). `AppRelease` y
  `get_db_session` se importan DENTRO de las funciones (sin ciclos).

API PUBLICA (nombres y firmas CONGELADOS):

    version_actual() -> str
    empaquetar_dist(origen="dist/MWX APP", destino="data/releases", version="") -> dict
    publicar_release(contenido, version, obligatorio=True, notas="", filename="") -> dict
    publicar_release_desde_archivo(ruta, version="", obligatorio=True, notas="") -> dict
    plataforma_de_version(version) -> str
    ultimo_release(plataforma="") -> dict
    leer_release(version="", plataforma="") -> dict
    listar_releases(limite=5, plataforma="") -> list[dict]
    borrar_release(version) -> dict
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from core.config import resolver_ruta

__all__ = [
    "version_actual",
    "empaquetar_dist",
    "publicar_release",
    "publicar_release_desde_archivo",
    "plataforma_de_version",
    "ultimo_release",
    "leer_release",
    "listar_releases",
    "borrar_release",
]

# Limites de tamaño de un release (bytes).
_MIN_BYTES = 1024  # 1 KB
_MAX_BYTES = 150 * 1024 * 1024  # 150 MB

# Nombre del paquete que se agrega al ZIP si la carpeta de origen no lo trae.
_NOMBRE_EXE = "mwx app.exe"

# Texto que se agrega al ZIP cuando la carpeta de origen no trae LEEME.txt.
_LEEME = (
    "MWX APP\r\n"
    "=======\r\n"
    "\r\n"
    "1. Extrae TODO el contenido de este ZIP en una carpeta "
    "(no lo ejecutes desde dentro del ZIP).\r\n"
    '2. Abre "MWX APP.exe".\r\n'
    "3. Necesitas Google Chrome instalado e Internet para iniciar sesion, "
    "descargar tus cuentas y generar contenido.\r\n"
    "4. Si Windows SmartScreen muestra una advertencia, elige "
    '"Mas informacion" > "Ejecutar de todas formas".\r\n'
)

_PATRON_VERSION_ZIP = re.compile(r"^MWX-APP-(.+)\.zip$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Utilidades internas tolerantes
# ---------------------------------------------------------------------------
def _texto(valor) -> str:
    """Convierte a texto recortado; "" para None o errores."""
    try:
        if valor is None:
            return ""
        if isinstance(valor, (bytes, bytearray)):
            valor = valor.decode("utf-8", "ignore")
        return str(valor).strip()
    except Exception:
        return ""


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


def _ruta(valor) -> str:
    """Ruta ABSOLUTA con `resolver_ruta` (relativas al project root); "" si no
    hay valor valido. Nunca lanza."""
    texto = _texto(valor)
    if not texto:
        return ""
    try:
        return resolver_ruta(texto)
    except Exception:
        return texto


def _sha256(datos: bytes) -> str:
    """SHA-256 hexadecimal de los bytes indicados ("" si falla)."""
    try:
        return hashlib.sha256(datos).hexdigest()
    except Exception:
        return ""


def _fecha_iso(valor) -> str:
    """Fecha como ISO-8601 o "" (nunca lanza)."""
    try:
        if valor is None:
            return ""
        if isinstance(valor, datetime):
            return valor.isoformat()
        return str(valor)
    except Exception:
        return ""


def _fila_meta(fila) -> dict:
    """Dict de METADATOS (sin contenido) desde una fila de `app_releases`."""
    return {
        "disponible": True,
        "version": _texto(fila[0]),
        "obligatorio": bool(fila[1]),
        "notas": _texto(fila[2]),
        "sha256": _texto(fila[3]),
        "tamano": int(fila[4] or 0),
        "filename": _texto(fila[5]),
        "fecha": _fecha_iso(fila[6]),
    }


def _columnas_meta(AppRelease):
    """Columnas de metadatos en el MISMO orden que `_fila_meta` (sin contenido)."""
    return (
        AppRelease.version,
        AppRelease.obligatorio,
        AppRelease.notas,
        AppRelease.sha256,
        AppRelease.tamano,
        AppRelease.filename,
        AppRelease.fecha,
    )


def _version_desde_nombre(filename: str) -> str:
    """Extrae la version de `MWX-APP-<version>.zip`; "" si no coincide."""
    try:
        coincidencia = _PATRON_VERSION_ZIP.match(_texto(filename))
        return _texto(coincidencia.group(1)) if coincidencia else ""
    except Exception:
        return ""


def _es_zip(datos: bytes) -> bool:
    """True si los bytes son un ZIP valido (nunca lanza)."""
    try:
        return zipfile.is_zipfile(io.BytesIO(datos))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Plataformas (sin migracion de esquema: el sufijo de `version` la define)
# ---------------------------------------------------------------------------
def plataforma_de_version(version) -> str:
    """Plataforma de un release segun su `version` (Windows por defecto).

    - Contiene `mac-arm64` / `mac_arm64` (o `macarm64`) -> `"mac-arm64"`.
    - Contiene `mac-intel` / `mac_intel` / `x86_64` -> `"mac-intel"`.
    - Cualquier otro texto con `mac` -> `"mac"`.
    - Resto (incluido None, "" o no-str) -> `"windows"`.

    Nunca lanza."""
    try:
        texto = _texto(version).lower().replace(" ", "")
    except Exception:
        return "windows"
    if not texto:
        return "windows"
    compacto = texto.replace("_", "-")
    if "mac-arm64" in compacto or "macarm64" in compacto:
        return "mac-arm64"
    if (
        "mac-intel" in compacto
        or "macintel" in compacto
        or "x86_64" in texto
        or "x86-64" in compacto
    ):
        return "mac-intel"
    if "mac" in compacto:
        return "mac"
    return "windows"


def _normalizar_plataforma(plataforma) -> str:
    """Normaliza el filtro: "" (todas) o windows/mac-arm64/mac-intel/mac.

    Acepta `_` como separador (`mac_arm64` -> `mac-arm64`) y aliases
    `all`/`todas`/`todos` que equivalen a "" (sin filtro). Un valor no
    reconocido cae a "windows" (comportamiento historico: cualquier version sin
    `mac` es Windows). Nunca lanza."""
    texto = _texto(plataforma).lower().replace(" ", "")
    if texto in ("", "all", "todas", "todos"):
        return ""
    return plataforma_de_version(texto)


def _condicion_plataforma(AppRelease, plataforma):
    """Condicion SQLAlchemy para filtrar releases por plataforma.

    Devuelve None cuando no hay filtro (""/"all"). Nunca lanza."""
    clave = _normalizar_plataforma(plataforma)
    if clave == "":
        return None

    from sqlalchemy import not_, or_

    if clave == "windows":
        return not_(AppRelease.version.ilike("%mac%"))
    if clave == "mac-arm64":
        return or_(
            AppRelease.version.ilike("%mac-arm64%"),
            AppRelease.version.ilike("%mac_arm64%"),
        )
    if clave == "mac-intel":
        return or_(
            AppRelease.version.ilike("%mac-intel%"),
            AppRelease.version.ilike("%mac_intel%"),
            AppRelease.version.ilike("%x86%"),
        )
    return AppRelease.version.ilike("%mac%")


# ---------------------------------------------------------------------------
# Version y empaquetado local
# ---------------------------------------------------------------------------
def version_actual() -> str:
    """Version del ejecutable MWX APP (`mwx_app.__version__`); "" si no existe."""
    try:
        from mwx_app import __version__

        return _texto(__version__)
    except Exception:
        return ""


def empaquetar_dist(
    origen="dist/MWX APP",
    destino="data/releases",
    version="",
) -> dict:
    """Empaqueta el CONTENIDO de `origen` en `destino/MWX-APP-<version>.zip`.

    - El ZIP contiene lo que hay DENTRO de la carpeta de origen (sin la carpeta
      contenedora), con compresion `ZIP_DEFLATED`.
    - Si `config.json` no esta dentro del origen pero existe en la raiz del
      proyecto, se agrega al ZIP.
    - Si no hay `LEEME.txt`, se genera uno corto dentro del ZIP (extraer TODO,
      Chrome + Internet).
    - Verifica que el ZIP contenga `MWX APP.exe` (recursivo) antes de publicar
      localmente; si falta, borra el ZIP y devuelve `ok=False`.
    - Calcula sha256/tamano y escribe `destino/release.json` (metadatos sin
      contenido).

    Devuelve `{"ok","zip","filename","version","sha256","tamano","error"}`.
    Usa `core.config.resolver_ruta` (rutas relativas -> project root). Nunca
    lanza."""
    resultado = {
        "ok": False,
        "zip": "",
        "filename": "",
        "version": "",
        "sha256": "",
        "tamano": 0,
        "error": "",
    }
    try:
        origen_path = Path(_ruta(origen))
        if not origen_path.is_dir():
            resultado["error"] = (
                f"no existe la carpeta de origen: {origen_path}"
            )
            return resultado

        version_txt = (
            _texto(version)
            or version_actual()
            or datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        resultado["version"] = version_txt
        filename = f"MWX-APP-{version_txt}.zip"
        resultado["filename"] = filename

        destino_path = Path(_ruta(destino))
        destino_path.mkdir(parents=True, exist_ok=True)
        zip_path = destino_path / filename
        resultado["zip"] = str(zip_path)

        nombres = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Contenido de la carpeta (rutas relativas a `origen`).
            for base, carpetas, archivos in os.walk(origen_path):
                carpetas.sort()
                for nombre_archivo in sorted(archivos):
                    completo = Path(base) / nombre_archivo
                    try:
                        arcname = completo.relative_to(origen_path).as_posix()
                    except Exception:
                        continue
                    try:
                        zf.write(completo, arcname)
                        nombres.append(arcname)
                    except Exception:
                        continue

            # config.json de la raiz del proyecto si el dist no trae uno
            # (en cualquier subcarpeta: cuenta como "dentro del origen").
            if not any(
                n.lower() == "config.json" or n.lower().endswith("/config.json")
                for n in nombres
            ):
                config_raiz = Path(_ruta("config.json"))
                if config_raiz.is_file():
                    try:
                        zf.write(config_raiz, "config.json")
                        nombres.append("config.json")
                    except Exception:
                        pass

            # LEEME.txt generado si el dist no trae uno.
            if not any(
                n.lower() == "leeme.txt" or n.lower().endswith("/leeme.txt")
                for n in nombres
            ):
                try:
                    zf.writestr("LEEME.txt", _LEEME)
                    nombres.append("LEEME.txt")
                except Exception:
                    pass

        # Verificar el ejecutable ANTES de publicar localmente.
        tiene_exe = any(
            nombre.lower() == _NOMBRE_EXE
            or nombre.lower().endswith("/" + _NOMBRE_EXE)
            for nombre in nombres
        )
        if not tiene_exe:
            try:
                zip_path.unlink()
            except Exception:
                pass
            resultado["zip"] = ""
            resultado["filename"] = ""
            resultado["error"] = (
                "el zip no contiene 'MWX APP.exe' (revisa que el ejecutable "
                f"exista dentro de {origen_path})"
            )
            return resultado

        datos = zip_path.read_bytes()
        sha = _sha256(datos)
        tamano = len(datos)
        resultado["sha256"] = sha
        resultado["tamano"] = tamano

        # Metadatos para publicacion local (sin contenido).
        meta = {
            "version": version_txt,
            "filename": filename,
            "sha256": sha,
            "tamano": tamano,
            "fecha": datetime.utcnow().isoformat(),
        }
        try:
            (destino_path / "release.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e_meta:
            resultado["error"] = f"no se pudo escribir release.json: {e_meta}"
            return resultado

        resultado["ok"] = True
        return resultado
    except Exception as e:
        resultado["error"] = f"no se pudo empaquetar el dist: {e}"
        return resultado


# ---------------------------------------------------------------------------
# Publicacion en la BD
# ---------------------------------------------------------------------------
def publicar_release(
    contenido,
    version,
    obligatorio=True,
    notas="",
    filename="",
) -> dict:
    """Inserta o ACTUALIZA (UPSERT por version) un release en `app_releases`.

    - Valida que `contenido` sea bytes de un ZIP entre 1 KB y 150 MB.
    - Calcula sha256/tamano, recorta `notas` a 4000 y `filename` a 200
      (`MWX-APP-<version>.zip` si viene vacio).
    - Si la version ya existe la reemplaza (mismo `id`); al final conserva solo
      los 2 releases mas recientes por `id` DE CADA PLATAFORMA (windows,
      mac-arm64, mac-intel, mac) y nunca borra el recien publicado. Publicar un
      Mac no afecta al release vigente de Windows.

    Devuelve `{"ok","version","sha256","tamano","error"}`. Nunca lanza."""
    version_txt = _texto(version)[:50]
    resultado = {
        "ok": False,
        "version": version_txt,
        "sha256": "",
        "tamano": 0,
        "error": "",
    }
    try:
        if not version_txt:
            resultado["error"] = "la version es obligatoria"
            return resultado

        datos = _bytes_de(contenido)
        if datos is None:
            resultado["error"] = "el contenido debe ser bytes (ZIP)"
            return resultado

        tamano = len(datos)
        resultado["tamano"] = tamano
        if tamano < _MIN_BYTES:
            resultado["error"] = "el ZIP es demasiado pequeno (minimo 1 KB)"
            return resultado
        if tamano > _MAX_BYTES:
            resultado["error"] = "el ZIP supera el limite de 150 MB"
            return resultado
        if not _es_zip(datos):
            resultado["error"] = "el contenido no es un ZIP valido"
            return resultado

        sha = _sha256(datos)
        resultado["sha256"] = sha

        name = _texto(filename)[:200] or f"MWX-APP-{version_txt}.zip"
        notas_txt = _texto(notas)[:4000]

        from sqlalchemy import delete, select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import AppRelease

        with get_db_session() as db:
            existente = (
                db.execute(
                    select(AppRelease).where(
                        AppRelease.version == version_txt
                    )
                )
                .scalars()
                .first()
            )
            if existente is None:
                fila = AppRelease(
                    version=version_txt,
                    obligatorio=bool(obligatorio),
                    notas=notas_txt,
                    filename=name,
                    sha256=sha,
                    tamano=tamano,
                    contenido=datos,
                    fecha=datetime.utcnow(),
                )
                db.add(fila)
            else:
                fila = existente
                fila.obligatorio = bool(obligatorio)
                fila.notas = notas_txt
                fila.filename = name
                fila.sha256 = sha
                fila.tamano = tamano
                fila.contenido = datos
                fila.fecha = datetime.utcnow()
                db.add(fila)

            db.flush()  # asigna/confirma el id antes de podar
            nuevo_id = getattr(fila, "id", None)

            # Retencion: 2 releases mas recientes por id DE CADA PLATAFORMA,
            # jamas el nuevo. Se agrupa por el sufijo de `version`
            # (`plataforma_de_version`); publicar Mac no borra Windows.
            filas = db.execute(
                select(AppRelease.id, AppRelease.version).order_by(
                    AppRelease.id.desc()
                )
            ).all()
            vistos = {}
            ids = []
            conservar = set()
            for valor in filas:
                if not valor or valor[0] is None:
                    continue
                identificador = int(valor[0])
                ids.append(identificador)
                clave = plataforma_de_version(valor[1])
                vistos[clave] = vistos.get(clave, 0) + 1
                if vistos[clave] <= 2:
                    conservar.add(identificador)
            if nuevo_id is not None:
                conservar.add(int(nuevo_id))
            borrar = [identificador for identificador in ids if identificador not in conservar]
            if borrar:
                db.execute(
                    delete(AppRelease).where(AppRelease.id.in_(borrar))
                )

        resultado["ok"] = True
        return resultado
    except Exception as e:
        resultado["error"] = f"no se pudo publicar el release: {e}"
        return resultado


def publicar_release_desde_archivo(
    ruta,
    version="",
    obligatorio=True,
    notas="",
) -> dict:
    """Publica en la BD el ZIP indicado por `ruta` (ver `publicar_release`).

    Si `version` viene vacia se intenta deducir del nombre
    `MWX-APP-<version>.zip`; si tampoco, se usa `version_actual()`.
    Devuelve el mismo dict que `publicar_release`. Nunca lanza."""
    version_txt = _texto(version)
    try:
        ruta_path = Path(_ruta(ruta))
        if not ruta_path.is_file():
            return {
                "ok": False,
                "version": version_txt,
                "sha256": "",
                "tamano": 0,
                "error": f"no existe el archivo: {ruta_path}",
            }
        datos = ruta_path.read_bytes()
        version_txt = (
            version_txt
            or _version_desde_nombre(ruta_path.name)
            or version_actual()
        )
        return publicar_release(
            datos,
            version_txt,
            obligatorio=obligatorio,
            notas=notas,
            filename=ruta_path.name,
        )
    except Exception as e:
        return {
            "ok": False,
            "version": version_txt,
            "sha256": "",
            "tamano": 0,
            "error": f"no se pudo leer el archivo: {e}",
        }


# ---------------------------------------------------------------------------
# Consulta y borrado (metadatos SIN contenido salvo `leer_release`)
# ---------------------------------------------------------------------------
def ultimo_release(plataforma="") -> dict:
    """Metadatos del release mas reciente de `plataforma` (SIN `contenido`).

    - `plataforma` vacio o "all" -> el mas reciente global (comportamiento
      historico).
    - "windows" -> ultimo `version` SIN "mac" (case-insensitive).
    - "mac-arm64" -> ultimo `version` con `%mac-arm64%` (o `%mac_arm64%`).
    - "mac-intel" -> ultimo `version` con `%mac-intel%` (o `%mac_intel%`).
    - "mac" -> ultimo `version` con `%mac%`.

    Claves: `disponible, version, obligatorio, notas, sha256, tamano, filename,
    fecha` (fecha ISO o ""). Sin releases de esa plataforma o ante error ->
    `{"disponible": False}`. Nunca lanza."""
    try:
        from sqlalchemy import select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import AppRelease

        stmt = select(*_columnas_meta(AppRelease))
        condicion = _condicion_plataforma(AppRelease, plataforma)
        if condicion is not None:
            stmt = stmt.where(condicion)
        stmt = stmt.order_by(AppRelease.id.desc()).limit(1)
        with get_db_session() as db:
            fila = db.execute(stmt).first()
        if fila is None:
            return {"disponible": False}
        return _fila_meta(fila)
    except Exception:
        return {"disponible": False}


def leer_release(version="", plataforma="") -> dict:
    """Metadatos + `contenido` (bytes) del release pedido o del mas reciente.

    - `version` vacia -> resuelve primero `ultimo_release(plataforma)` y lee esa
      version (asi Mac y Windows bajan SU propio ZIP).
    - `version` explicita -> se ignora `plataforma` y se lee esa version (ya es
      unica y contiene el sufijo).

    Si no existe devuelve `{"disponible": False, "contenido": b""}`. Nunca
    lanza."""
    try:
        from sqlalchemy import select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import AppRelease

        version_txt = _texto(version)
        if not version_txt:
            ultimo = ultimo_release(plataforma)
            if not ultimo.get("disponible"):
                return {"disponible": False, "contenido": b""}
            version_txt = _texto(ultimo.get("version"))
        if not version_txt:
            return {"disponible": False, "contenido": b""}

        with get_db_session() as db:
            stmt = select(AppRelease).where(
                AppRelease.version == version_txt
            )
            fila = (
                db.execute(stmt.order_by(AppRelease.id.desc()).limit(1))
                .scalars()
                .first()
            )
        if fila is None:
            return {"disponible": False, "contenido": b""}
        datos = {
            "disponible": True,
            "version": _texto(fila.version),
            "obligatorio": bool(fila.obligatorio),
            "notas": _texto(fila.notas),
            "sha256": _texto(fila.sha256),
            "tamano": int(fila.tamano or 0),
            "filename": _texto(fila.filename),
            "fecha": _fecha_iso(fila.fecha),
            "contenido": _bytes_de(fila.contenido) or b"",
        }
        return datos
    except Exception:
        return {"disponible": False, "contenido": b""}


def listar_releases(limite=5, plataforma="") -> list:
    """Ultimos releases (metadatos SIN contenido), del mas nuevo al mas viejo.

    `plataforma` vacio = todos (comportamiento historico); con un valor usa la
    misma regla de `ultimo_release` (windows / mac-arm64 / mac-intel / mac).
    `limite` default 5 (si viene invalido o <= 0 se usa 5). Ante cualquier
    error devuelve []. Nunca lanza."""
    try:
        from sqlalchemy import select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import AppRelease

        try:
            tope = int(limite)
        except (TypeError, ValueError, OverflowError):
            tope = 5
        if tope <= 0:
            tope = 5

        stmt = select(*_columnas_meta(AppRelease))
        condicion = _condicion_plataforma(AppRelease, plataforma)
        if condicion is not None:
            stmt = stmt.where(condicion)
        stmt = stmt.order_by(AppRelease.id.desc()).limit(tope)
        with get_db_session() as db:
            filas = db.execute(stmt).all()
        return [_fila_meta(fila) for fila in filas]
    except Exception:
        return []


def borrar_release(version) -> dict:
    """Borra el release de la version indicada.

    Devuelve `{"ok","version","error"}`; `ok=False` si la version viene vacia
    o no existe. Nunca lanza."""
    version_txt = _texto(version)
    if not version_txt:
        return {"ok": False, "version": "", "error": "la version es obligatoria"}
    try:
        from sqlalchemy import select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import AppRelease

        with get_db_session() as db:
            fila = (
                db.execute(
                    select(AppRelease).where(
                        AppRelease.version == version_txt
                    )
                )
                .scalars()
                .first()
            )
            if fila is None:
                return {
                    "ok": False,
                    "version": version_txt,
                    "error": "release no encontrado",
                }
            db.delete(fila)
        return {"ok": True, "version": version_txt, "error": ""}
    except Exception as e:
        return {
            "ok": False,
            "version": version_txt,
            "error": f"no se pudo borrar el release: {e}",
        }
