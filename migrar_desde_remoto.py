# -*- coding: utf-8 -*-
"""Sincroniza TODAS las cuentas de mwx.app (PostgreSQL/Supabase) a la BD local.

Uso principal (SIEMPRE desde la raiz del proyecto):

    python migrar_desde_remoto.py                 # exporta JSON + dry-run
    python migrar_desde_remoto.py --apply         # respaldo + renombres + restore

Reglas:
    * Solo se copia la tabla ``cuentas``; ``tareas`` y demas tablas no se tocan.
    * El dry-run NUNCA modifica la BD local: solo lee y escribe el JSON de
      exportacion en ``data/backups/``.
    * ``--apply`` SIEMPRE respalda primero (SQLite backup API + JSON + sesiones
      de Change.org) y aborta sin restaurar si un renombre falla.
    * Nunca se imprime la URL remota, contrasenas, tokens, cookies ni emails.

La URL remota se resuelve en este orden:
    1. ``--url URL``
    2. variable de entorno ``DATABASE_URL_REMOTE`` (o ``.env``)
    3. Railway CLI (``railway variables --json`` -> ``DATABASE_URL``)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - consolas sin soporte
        pass

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

try:  # configuracion central (rutas absolutas y .env de PROJECT_ROOT)
    from core.config import PROJECT_ROOT, resolver_ruta
except Exception:  # pragma: no cover - fallback para ejecuciones aisladas
    PROJECT_ROOT = Path(__file__).resolve().parent

    def resolver_ruta(relativa: str) -> str:
        return str((PROJECT_ROOT / relativa).resolve())


from core.database import engine as _engine_local  # noqa: E402
from core.database import get_db_session  # noqa: E402
from core.models import Cuenta  # noqa: E402
from core.renombrar import renombrar_usuario  # noqa: E402
from cuentas.restaurador import _parsear_fecha, restaurar_desde_backup  # noqa: E402

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
COLUMNAS_EXCLUIDAS_VERIFICACION = ("id", "fecha_creacion")
COLUMNAS_BOOLEANAS = ("activa", "pausada_activacion")
COLUMNAS_FECHA = ("fecha_creacion", "last_checked")
COLUMNAS_SENSIBLES = (
    "password",
    "email",
    "email_password",
    "auth_token",
    "totp_secret",
    "cookies_json",
    "proxy",
    "phone",
    "user_agent",
    "secret",
    "token",
)

_PATRON_URL = re.compile(r"(?:postgres(?:ql)?|mysql|mariadb|sqlite)://[^\s'\"<>]+", re.I)
_PATRON_CREDENCIAL = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;'\"]+"
)

_CARPETA_BACKUPS = Path(resolver_ruta("data/backups"))
_CARPETA_COOKIES_CHANGE = Path(resolver_ruta("data/cookies/change"))


# --------------------------------------------------------------------------- #
# Utilidades de seguridad / serializacion
# --------------------------------------------------------------------------- #
def enmascarar_url(url: Any) -> str:
    """Describe una URL SIN usuario, contrasena ni query string."""
    texto = str(url or "").strip()
    if not texto:
        return "(vacia)"
    if texto.startswith("sqlite"):
        return texto
    try:
        partes = urlsplit(texto)
        host = partes.hostname or "?"
        puerto = f":{partes.port}" if partes.port else ""
        base = (partes.path or "").lstrip("/") or "?"
        credencial = "***@" if (partes.username or partes.password) else ""
        return f"{partes.scheme or 'remota'}://{credencial}{host}{puerto}/{base}"
    except Exception:
        return "(url oculta)"


def sanear_texto(texto: Any) -> str:
    """Quita URLs/credenciales de un mensaje de error antes de mostrarlo."""
    limpio = _PATRON_URL.sub(lambda m: enmascarar_url(m.group(0)), str(texto or ""))
    return _PATRON_CREDENCIAL.sub(r"\1=***", limpio)


def normalizar_url_remota(url: Any) -> str:
    """Normaliza ``postgres://`` a ``postgresql://`` (SQLAlchemy 2.x)."""
    texto = str(url or "").strip()
    if texto.startswith("postgres://"):
        texto = "postgresql://" + texto[len("postgres://") :]
    return texto


def _serializar_valor(valor: Any) -> Any:
    """Convierte valores de SQLAlchemy/PostgreSQL a JSON (nunca lanza)."""
    try:
        if valor is None:
            return None
        if isinstance(valor, bool):
            return bool(valor)
        if isinstance(valor, (int, float, str)):
            return valor
        if isinstance(valor, datetime):
            return valor.isoformat()
        if isinstance(valor, date):
            return valor.isoformat()
        if isinstance(valor, Decimal):
            return str(valor)
        if isinstance(valor, uuid.UUID):
            return str(valor)
        if isinstance(valor, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(valor)).decode("ascii")
        if isinstance(valor, dict):
            return {str(clave): _serializar_valor(contenido) for clave, contenido in valor.items()}
        if isinstance(valor, (list, tuple)):
            return [_serializar_valor(contenido) for contenido in valor]
        if isinstance(valor, (set, frozenset)):
            return [_serializar_valor(contenido) for contenido in sorted(valor, key=repr)]
        try:
            json.dumps(valor)
            return valor
        except Exception:
            return str(valor)
    except Exception:
        try:
            return str(valor)
        except Exception:
            return None


def _ruta_unica(ruta: Path) -> Path:
    """Devuelve una ruta libre; si existe le agrega ``_1``, ``_2``, ..."""
    destino = Path(ruta)
    if not destino.exists():
        return destino
    for indice in range(1, 1000):
        candidato = destino.with_name(f"{destino.name}_{indice}")
        if not candidato.exists():
            return candidato
    marca = datetime.now().strftime("%H%M%S%f")
    return destino.with_name(f"{destino.name}_{marca}")


def escribir_json_atomico(destino: Path, payload: Any) -> Path:
    """Escribe un JSON completo en ``destino`` (temp + fsync + os.replace)."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporal = tempfile.mkstemp(
        prefix=f".{destino.stem}_", suffix=".tmp", dir=str(destino.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as manejador:
            json.dump(payload, manejador, ensure_ascii=False, indent=2, default=str)
            manejador.write("\n")
            manejador.flush()
            os.fsync(manejador.fileno())
        os.replace(temporal, destino)
        return destino
    except Exception:
        try:
            os.unlink(temporal)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# URL remota
# --------------------------------------------------------------------------- #
def _leer_env_remota() -> str:
    """Lee ``DATABASE_URL_REMOTE`` del entorno y, si falta, del ``.env``."""
    valor = (os.environ.get("DATABASE_URL_REMOTE") or "").strip()
    if valor:
        return valor
    try:
        from dotenv import dotenv_values

        datos = dotenv_values(str(Path(PROJECT_ROOT) / ".env")) or {}
        return str(datos.get("DATABASE_URL_REMOTE") or "").strip()
    except Exception:
        return ""


def _url_desde_railway_cli() -> str:
    """Obtiene ``DATABASE_URL`` con Railway CLI. Nunca imprime la URL."""
    ejecutable = (
        shutil.which("railway")
        or shutil.which("railway.cmd")
        or shutil.which("railway.exe")
    )
    if not ejecutable:
        raise RuntimeError(
            "Railway CLI no encontrado. Instalalo (npm i -g @railway/cli) o "
            "pasa --url / define DATABASE_URL_REMOTE."
        )
    try:
        proceso = subprocess.run(
            [ejecutable, "variables", "--json"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(PROJECT_ROOT),
        )
    except Exception as exc:
        raise RuntimeError(
            f"no se pudo ejecutar Railway CLI ({type(exc).__name__})"
        ) from None
    if proceso.returncode != 0:
        raise RuntimeError(
            f"railway variables --json fallo (codigo {proceso.returncode}); "
            "revisa que la CLI siga logueada"
        )
    try:
        datos = json.loads(proceso.stdout or "{}")
    except Exception:
        raise RuntimeError("la salida de Railway CLI no es JSON valido") from None
    if not isinstance(datos, dict):
        raise RuntimeError("la salida de Railway CLI no es un objeto JSON")
    url = str(datos.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("Railway CLI no devolvio la variable DATABASE_URL")
    return url


def obtener_url_remota(url: Optional[str] = None) -> str:
    """Resuelve la URL remota: argumento > env/.env > Railway CLI.

    Lanza ``RuntimeError`` con un mensaje saneado si no se puede resolver.
    NUNCA devuelve/imprime credenciales en los mensajes de error.
    """
    candidata = str(url or "").strip()
    if candidata:
        return normalizar_url_remota(candidata)
    candidata = _leer_env_remota()
    if candidata:
        return normalizar_url_remota(candidata)
    return normalizar_url_remota(_url_desde_railway_cli())


# --------------------------------------------------------------------------- #
# Exportacion remota
# --------------------------------------------------------------------------- #
def _motor_remoto(url: str):
    """Crea el engine remoto con SSL obligatorio salvo SQLite (tests)."""
    texto = normalizar_url_remota(url)
    try:
        backend = make_url(texto).get_backend_name()
    except Exception:
        backend = ""
    if backend == "sqlite":
        return create_engine(
            texto,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
    connect_args = {}
    if "sslmode=" not in texto:
        connect_args["sslmode"] = "require"
    return create_engine(texto, connect_args=connect_args, pool_pre_ping=True)


def _resolver_destino_export(destino: Optional[Any]) -> Path:
    """Resuelve el JSON de exportacion (default ``data/backups/mwx_cuentas_<ts>.json``)."""
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    if destino:
        ruta = Path(str(destino))
        if not ruta.is_absolute():
            ruta = Path(resolver_ruta(str(destino)))
        if ruta.is_dir() or str(destino).endswith(("/", "\\")):
            ruta = ruta / f"mwx_cuentas_{marca}.json"
    else:
        ruta = Path(_CARPETA_BACKUPS) / f"mwx_cuentas_{marca}.json"
    return _ruta_unica(ruta)


def exportar_cuentas_remotas(url: str, destino: Optional[Any] = None) -> Dict[str, Any]:
    """Exporta TODAS las filas de ``cuentas`` a un JSON compatible con el restaurador.

    Refleja la tabla con SQLAlchemy para incluir automaticamente todas las
    columnas (36 hoy). Devuelve un resumen sin secretos.
    """
    resultado: Dict[str, Any] = {
        "ok": False,
        "exportadas": 0,
        "total": 0,
        "destino": "",
        "origen": "mwx.app",
        "error": "",
        "url": enmascarar_url(url),
    }
    motor = None
    try:
        motor = _motor_remoto(url)
        metadatos = MetaData()
        metadatos.reflect(motor, only=["cuentas"])
        tabla = metadatos.tables.get("cuentas")
        if tabla is None:
            raise RuntimeError("la base remota no tiene la tabla 'cuentas'")
        with motor.connect() as conexion:
            filas = conexion.execute(select(tabla)).mappings().all()
        cuentas: List[Dict[str, Any]] = []
        for fila in filas:
            cuentas.append(
                {clave: _serializar_valor(valor) for clave, valor in dict(fila).items()}
            )
        ruta_destino = _resolver_destino_export(destino)
        payload = {
            "fecha": datetime.now().isoformat(),
            "origen": "mwx.app",
            "keep": [],
            "cuentas": cuentas,
        }
        escribir_json_atomico(ruta_destino, payload)
        resultado.update(
            {
                "ok": True,
                "exportadas": len(cuentas),
                "total": len(cuentas),
                "destino": str(ruta_destino),
            }
        )
    except Exception as exc:
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    finally:
        if motor is not None:
            try:
                motor.dispose()
            except Exception:
                pass
    return resultado


# --------------------------------------------------------------------------- #
# Lectura / comparacion local
# --------------------------------------------------------------------------- #
def _fila_a_dict(fila: Any) -> Dict[str, Any]:
    """Convierte una fila (modelo SQLAlchemy, Row o dict) a dict simple."""
    if isinstance(fila, dict):
        return dict(fila)
    for atributo in ("_mapping", "mapping"):
        try:
            mapeo = getattr(fila, atributo, None)
            if mapeo is not None:
                return dict(mapeo)
        except Exception:
            continue
    try:
        return dict(fila)
    except Exception:
        return {}


def leer_cuentas_locales(session_factory=None) -> List[Dict[str, Any]]:
    """Lee TODAS las cuentas locales como lista de dicts (sin secretos en logs)."""
    fabrica = session_factory or get_db_session
    with fabrica() as db:
        filas = db.query(Cuenta).all()
        return [
            {columna.name: getattr(cuenta, columna.name, None) for columna in Cuenta.__table__.columns}
            for cuenta in filas
        ]


def _indice_por_campo(filas: Sequence[Dict[str, Any]], campo: str, transformar=None):
    indice: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for fila in filas:
        valor = str(fila.get(campo) or "").strip()
        if not valor:
            continue
        if transformar is not None:
            valor = transformar(valor)
        indice[valor].append(fila)
    return indice


def detectar_duplicados_locales(
    cuentas_remotas: Sequence[Dict[str, Any]],
    cuentas_locales: Optional[Sequence[Dict[str, Any]]] = None,
    session_factory=None,
) -> Dict[str, Any]:
    """Detecta cuentas locales que en realidad son la MISMA identidad remota.

    Regla:
        * Primero por ``auth_token`` exacto no vacio.
        * Si no hay match, por ``email`` case-insensitive.
        * Solo se propone renombrar si hay UN unico match remoto y la clave
          remota NO existe ya en local.
        * Si hay 2+ matches -> ``ambiguos``. Si el destino ya existe en local
          -> ``omitidos``. Todo lo demas se conserva como ``local_only_reales``.

    Nunca lanza: los fallos se devuelven en ``ok``/``error``.
    """
    resultado: Dict[str, Any] = {
        "ok": True,
        "renombres": [],
        "ambiguos": [],
        "omitidos": [],
        "local_only_reales": [],
        "error": "",
    }
    try:
        remotas = [_fila_a_dict(fila) for fila in (cuentas_remotas or [])]
        if cuentas_locales is None:
            locales = leer_cuentas_locales(session_factory)
        else:
            locales = [_fila_a_dict(fila) for fila in cuentas_locales]

        remotas_por_usuario: Dict[str, Dict[str, Any]] = {}
        for fila in remotas:
            usuario = str(fila.get("usuario") or "").strip()
            if usuario:
                remotas_por_usuario[usuario] = fila

        locales_por_usuario: Dict[str, Dict[str, Any]] = {}
        for fila in locales:
            usuario = str(fila.get("usuario") or "").strip()
            if usuario:
                locales_por_usuario[usuario] = fila

        por_token = _indice_por_campo(remotas, "auth_token")
        por_email = _indice_por_campo(remotas, "email", lambda v: v.lower())

        destinos_reclamados = set()
        claves_locales_ci = {usuario.lower() for usuario in locales_por_usuario}

        for local_usuario in sorted(locales_por_usuario):
            if local_usuario in remotas_por_usuario:
                continue  # ya existe por clave exacta: el restore la actualiza
            local = locales_por_usuario[local_usuario]

            motivo = ""
            candidatos: List[Dict[str, Any]] = []
            token = str(local.get("auth_token") or "").strip()
            if token:
                candidatos = list(por_token.get(token, []))
                motivo = "token"
            if not candidatos:
                email = str(local.get("email") or "").strip().lower()
                if email:
                    candidatos = list(por_email.get(email, []))
                    motivo = "email"

            if not candidatos:
                resultado["local_only_reales"].append(local_usuario)
                continue

            usuarios_candidatos = [
                str(candidato.get("usuario") or "").strip()
                for candidato in candidatos
                if str(candidato.get("usuario") or "").strip()
            ]
            if len(candidatos) != 1 or len(usuarios_candidatos) != 1:
                resultado["ambiguos"].append(
                    {
                        "usuario": local_usuario,
                        "motivo": motivo,
                        "candidatos": sorted(set(usuarios_candidatos)),
                    }
                )
                continue

            nuevo = usuarios_candidatos[0]
            # El destino no puede existir ya en local (otra fila distinta).
            otras_claves = claves_locales_ci - {local_usuario.lower()}
            if nuevo.lower() in otras_claves:
                resultado["omitidos"].append(
                    {
                        "usuario": local_usuario,
                        "motivo": motivo,
                        "candidatos": [nuevo],
                        "razon": "la clave remota ya existe en local",
                    }
                )
                continue
            if nuevo.lower() in destinos_reclamados:
                resultado["ambiguos"].append(
                    {
                        "usuario": local_usuario,
                        "motivo": motivo,
                        "candidatos": [nuevo],
                        "razon": "destino ya reclamado por otro renombre",
                    }
                )
                continue

            destinos_reclamados.add(nuevo.lower())
            resultado["renombres"].append(
                {"anterior": local_usuario, "nuevo": nuevo, "motivo": motivo}
            )

        resultado["local_only_reales"].sort()
    except Exception as exc:
        resultado["ok"] = False
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    return resultado


def _normalizar_para_comparar(columna: str, valor: Any) -> Any:
    """Normaliza datetime/JSON/bool para comparar remoto vs local."""
    if valor is None:
        return None
    if columna in COLUMNAS_BOOLEANAS:
        try:
            return bool(valor)
        except Exception:
            return None
    if columna in COLUMNAS_FECHA:
        fecha = valor if isinstance(valor, datetime) else _parsear_fecha(valor)
        if fecha is None:
            return None
        if fecha.tzinfo is not None:
            fecha = fecha.astimezone(timezone.utc).replace(tzinfo=None)
        return fecha.isoformat(timespec="microseconds")
    if columna == "cookies_json":
        if isinstance(valor, str):
            try:
                valor = json.loads(valor)
            except Exception:
                return valor
        try:
            return json.dumps(valor, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return str(valor)
    if isinstance(valor, bool):
        return bool(valor)
    if isinstance(valor, (int, float, str)):
        return valor
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(valor)).decode("ascii")
    try:
        return json.dumps(valor, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(valor)


def _es_sensible(columna: str) -> bool:
    nombre = str(columna or "").lower()
    return any(marca in nombre for marca in COLUMNAS_SENSIBLES)


def _valor_seguro(columna: str, valor: Any) -> str:
    """Valor para un reporte de mismatch: oculta columnas sensibles."""
    if valor is None:
        return "None"
    if _es_sensible(columna):
        return "<oculto>"
    texto = str(valor)
    return texto[:80] + ("..." if len(texto) > 80 else "")


def _estadisticas(filas: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Conteos por tier/seccion/tipo/rol/perfil + pausadas/activas/sesiones."""

    def _contar(campo: str) -> Dict[str, int]:
        contador = Counter()
        for fila in filas:
            valor = str(fila.get(campo) or "").strip() or "(vacio)"
            contador[valor] += 1
        return dict(sorted(contador.items()))

    return {
        "total": len(filas),
        "por_tier": _contar("tier_calidad"),
        "por_seccion": _contar("seccion"),
        "por_tipo_cuenta": _contar("tipo_cuenta"),
        "por_rol_activacion": _contar("rol_activacion"),
        "por_perfil_personalidad": _contar("perfil_personalidad"),
        "pausadas": sum(1 for fila in filas if bool(fila.get("pausada_activacion"))),
        "activas": sum(1 for fila in filas if bool(fila.get("activa"))),
        "cookies_json_no_nulos": sum(
            1 for fila in filas if fila.get("cookies_json") is not None
        ),
        "auth_token_no_vacio": sum(
            1 for fila in filas if str(fila.get("auth_token") or "").strip()
        ),
    }


def _columnas_verificacion(remotas: Sequence[Dict[str, Any]]) -> List[str]:
    if not remotas:
        return []
    presentes = set()
    for fila in remotas:
        presentes.update(fila.keys())
    return sorted(
        columna
        for columna in presentes
        if columna not in COLUMNAS_EXCLUIDAS_VERIFICACION
    )


def verificar(
    cuentas_remotas: Sequence[Dict[str, Any]],
    session_factory=None,
) -> Dict[str, Any]:
    """Verifica que la BD local quedo identica al remoto en las 263 cuentas.

    Compara columna por columna (excluye ``id`` y ``fecha_creacion``)
    normalizando datetime/JSON/bool. Los valores sensibles jamas se muestran.
    """
    resultado: Dict[str, Any] = {
        "ok": False,
        "remotas": 0,
        "locales": 0,
        "faltantes": [],
        "local_only": [],
        "mismatches": [],
        "stats_remoto": {},
        "stats_local": {},
        "error": "",
    }
    try:
        remotas = [_fila_a_dict(fila) for fila in (cuentas_remotas or [])]
        locales = leer_cuentas_locales(session_factory)
        rem_por_usuario = {
            str(fila.get("usuario") or "").strip(): fila
            for fila in remotas
            if str(fila.get("usuario") or "").strip()
        }
        loc_por_usuario = {
            str(fila.get("usuario") or "").strip(): fila
            for fila in locales
            if str(fila.get("usuario") or "").strip()
        }
        usuarios_remotos = set(rem_por_usuario)
        usuarios_locales = set(loc_por_usuario)

        columnas = _columnas_verificacion(remotas)
        mismatches: List[Dict[str, str]] = []
        for usuario in sorted(usuarios_remotos & usuarios_locales):
            remoto = rem_por_usuario[usuario]
            local = loc_por_usuario[usuario]
            for columna in columnas:
                if columna not in local:
                    mismatches.append(
                        {
                            "usuario": usuario,
                            "columna": columna,
                            "remoto": _valor_seguro(columna, remoto.get(columna)),
                            "local": "<no existe en local>",
                        }
                    )
                    continue
                a = _normalizar_para_comparar(columna, remoto.get(columna))
                b = _normalizar_para_comparar(columna, local.get(columna))
                if a != b:
                    mismatches.append(
                        {
                            "usuario": usuario,
                            "columna": columna,
                            "remoto": _valor_seguro(columna, remoto.get(columna)),
                            "local": _valor_seguro(columna, local.get(columna)),
                        }
                    )

        resultado.update(
            {
                "ok": not (usuarios_remotos - usuarios_locales) and not mismatches,
                "remotas": len(usuarios_remotos),
                "locales": len(locales),
                "faltantes": sorted(usuarios_remotos - usuarios_locales),
                "local_only": sorted(usuarios_locales - usuarios_remotos),
                "mismatches": mismatches,
                "stats_remoto": _estadisticas(remotas),
                "stats_local": _estadisticas(locales),
            }
        )
    except Exception as exc:
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    return resultado


# --------------------------------------------------------------------------- #
# Respaldo local
# --------------------------------------------------------------------------- #
def ruta_sqlite_local() -> Optional[Path]:
    """Ruta absoluta del SQLite local (o None si la BD no es SQLite)."""
    try:
        url = _engine_local.url
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            return Path(url.database)
    except Exception:
        pass
    return None


def _respaldar_sqlite(origen: Path, destino: Path) -> None:
    """Copia binaria consistente con la API sqlite3 ``backup`` (no copy)."""
    conexion_origen = sqlite3.connect(str(origen))
    conexion_destino = sqlite3.connect(str(destino))
    try:
        with conexion_destino:
            conexion_origen.backup(conexion_destino)
    finally:
        try:
            conexion_origen.close()
        except Exception:
            pass
        try:
            conexion_destino.close()
        except Exception:
            pass


def respaldar_local(
    session_factory=None,
    sqlite_path: Optional[Any] = None,
    destino_dir: Optional[Any] = None,
    change_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Respalda SQLite + JSON de cuentas + sesiones de Change.org.

    El SQLite se copia con la API ``sqlite3.backup`` (consistente incluso con
    WAL), nunca con una copia de archivo simple.
    """
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    carpeta = Path(destino_dir) if destino_dir else Path(_CARPETA_BACKUPS)
    resultado: Dict[str, Any] = {
        "ok": False,
        "carpeta": str(carpeta),
        "db": "",
        "cuentas_json": "",
        "change": "",
        "cuentas": 0,
        "change_archivos": 0,
        "error": "",
    }
    try:
        carpeta.mkdir(parents=True, exist_ok=True)
        filas = leer_cuentas_locales(session_factory)
        resultado["cuentas"] = len(filas)

        ruta_origen = Path(sqlite_path) if sqlite_path else ruta_sqlite_local()
        if ruta_origen and ruta_origen.exists():
            destino_db = _ruta_unica(carpeta / f"gestor_redes_{marca}.db")
            _respaldar_sqlite(ruta_origen, destino_db)
            resultado["db"] = str(destino_db)

        destino_json = _ruta_unica(carpeta / f"cuentas_locales_{marca}.json")
        escribir_json_atomico(
            destino_json,
            {
                "fecha": datetime.now().isoformat(),
                "origen": "local",
                "keep": [],
                "cuentas": [_serializar_valor(fila) for fila in filas],
            },
        )
        resultado["cuentas_json"] = str(destino_json)

        origen_change = Path(change_dir) if change_dir else Path(_CARPETA_COOKIES_CHANGE)
        if origen_change.is_dir():
            destino_change = _ruta_unica(carpeta / f"change_sessions_{marca}")
            shutil.copytree(origen_change, destino_change, dirs_exist_ok=True)
            resultado["change"] = str(destino_change)
            resultado["change_archivos"] = len(list(destino_change.glob("*.json")))

        resultado["ok"] = True
    except Exception as exc:
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    return resultado


def renombrar_sesion_change(
    anterior: str,
    nuevo: str,
    change_dir: Optional[Any] = None,
    backup_dir: Optional[Any] = None,
) -> Dict[str, Any]:
    """Mueve ``data/cookies/change/<old>.json`` a ``<new>.json`` si aplica.

    Si el destino ya existe NO lo pisa (se conserva tal cual). Antes copia el
    archivo al respaldo de sesiones de Change si se indica ``backup_dir``.
    """
    resultado: Dict[str, Any] = {
        "ok": True,
        "movido": False,
        "de": "",
        "a": "",
        "motivo": "",
        "error": "",
    }
    try:
        directorio = Path(change_dir) if change_dir else Path(_CARPETA_COOKIES_CHANGE)
        origen = directorio / f"{anterior}.json"
        destino = directorio / f"{nuevo}.json"
        if not origen.is_file():
            resultado["motivo"] = "no existe sesion de Change para la cuenta"
            return resultado
        resultado["de"] = str(origen)
        resultado["a"] = str(destino)
        if backup_dir:
            try:
                carpeta_respaldo = Path(backup_dir)
                carpeta_respaldo.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origen, carpeta_respaldo / origen.name)
            except Exception:
                pass
        if destino.exists():
            resultado["motivo"] = "el destino ya existe; se conserva el actual"
            return resultado
        shutil.move(str(origen), str(destino))
        resultado["movido"] = True
    except Exception as exc:
        resultado["ok"] = False
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    return resultado


# --------------------------------------------------------------------------- #
# Aplicacion real
# --------------------------------------------------------------------------- #
def _cargar_remotas_de_json(ruta: Any) -> List[Dict[str, Any]]:
    with Path(ruta).open("r", encoding="utf-8") as manejador:
        datos = json.load(manejador)
    if isinstance(datos, dict):
        filas = datos.get("cuentas")
    elif isinstance(datos, list):
        filas = datos
    else:
        filas = None
    if not isinstance(filas, list):
        raise ValueError("el JSON no contiene la lista 'cuentas'")
    return [fila for fila in filas if isinstance(fila, dict)]


def aplicar(
    export_json: Any,
    renombres: Optional[Sequence[Dict[str, Any]]] = None,
    session_factory=None,
    sqlite_path: Optional[Any] = None,
    destino_dir: Optional[Any] = None,
    change_dir: Optional[Any] = None,
    verificar_al_final: bool = True,
) -> Dict[str, Any]:
    """Respalda, renombra duplicados y restaura TODAS las cuentas remotas.

    Si un renombre devuelve ``ok=False`` se ABORTA sin restaurar (se reporta el
    error). Nunca lanza.
    """
    resultado: Dict[str, Any] = {
        "ok": False,
        "respaldo": None,
        "renombres": [],
        "restauracion": None,
        "verificacion": None,
        "error": "",
    }
    try:
        respaldo = respaldar_local(
            session_factory=session_factory,
            sqlite_path=sqlite_path,
            destino_dir=destino_dir,
            change_dir=change_dir,
        )
        resultado["respaldo"] = respaldo
        if not respaldo.get("ok"):
            resultado["error"] = (
                "no se pudo crear el respaldo local; no se toco nada: "
                f"{respaldo.get('error') or 'error desconocido'}"
            )
            return resultado

        remotas = _cargar_remotas_de_json(export_json)
        if renombres is None:
            detectados = detectar_duplicados_locales(
                remotas, session_factory=session_factory
            )
            renombres = detectados.get("renombres", [])

        for item in list(renombres or []):
            anterior = str(item.get("anterior") or "").strip()
            nuevo = str(item.get("nuevo") or "").strip()
            if not anterior or not nuevo:
                resultado["error"] = "renombre con usuario vacio; no se restauro nada"
                return resultado
            detalle_renombre = renombrar_usuario(anterior, nuevo, dry_run=False)
            sesion_change = renombrar_sesion_change(
                anterior,
                nuevo,
                change_dir=change_dir,
                backup_dir=respaldo.get("change") or None,
            )
            resultado["renombres"].append(
                {
                    "anterior": anterior,
                    "nuevo": nuevo,
                    "motivo": item.get("motivo", ""),
                    "ok": bool(detalle_renombre.get("ok")),
                    "renombrado": bool(detalle_renombre.get("renombrado")),
                    "error": sanear_texto(detalle_renombre.get("error", "")),
                    "archivos": detalle_renombre.get("archivos", []),
                    "sesion_change": sesion_change,
                }
            )
            if not detalle_renombre.get("ok"):
                resultado["error"] = (
                    f"el renombre {anterior!r} -> {nuevo!r} fallo "
                    f"({sanear_texto(detalle_renombre.get('error') or 'error desconocido')}); "
                    "se aborto SIN restaurar"
                )
                return resultado

        restauracion = restaurar_desde_backup(
            export_json,
            dry_run=False,
            sobrescribir=True,
            inactivas=False,
        )
        resultado["restauracion"] = restauracion
        if not restauracion.get("ok"):
            detalle = "; ".join((restauracion.get("detalle_errores") or [])[:3])
            resultado["error"] = sanear_texto(f"la restauracion fallo: {detalle}")
            return resultado

        if verificar_al_final:
            verificacion = verificar(remotas, session_factory=session_factory)
            resultado["verificacion"] = verificacion
            resultado["ok"] = bool(verificacion.get("ok"))
            if not resultado["ok"]:
                resultado["error"] = (
                    "la verificacion encontro diferencias: "
                    f"{len(verificacion.get('mismatches') or [])} mismatches, "
                    f"{len(verificacion.get('faltantes') or [])} faltantes"
                )
                return resultado
        else:
            resultado["ok"] = True
    except Exception as exc:
        resultado["error"] = f"{type(exc).__name__}: {sanear_texto(exc)}"
    return resultado


# --------------------------------------------------------------------------- #
# Resumen para el operador
# --------------------------------------------------------------------------- #
def construir_resumen(
    exportacion: Dict[str, Any],
    locales: Sequence[Dict[str, Any]],
    duplicados: Optional[Dict[str, Any]],
    dry_run: bool,
) -> Dict[str, Any]:
    """Resumen del dry-run sin secretos (263/71/192/21/0 + stats)."""
    remotas = [
        fila for fila in (exportacion.get("cuentas") or []) if isinstance(fila, dict)
    ]
    usuarios_remotos = {
        str(fila.get("usuario") or "").strip()
        for fila in remotas
        if str(fila.get("usuario") or "").strip()
    }
    usuarios_locales = {
        str(fila.get("usuario") or "").strip()
        for fila in locales
        if str(fila.get("usuario") or "").strip()
    }
    duplicados = duplicados or {}
    renombres = list(duplicados.get("renombres") or [])
    local_only_reales = sorted(duplicados.get("local_only_reales") or [])

    # Proyeccion tras renombrar (solo informativa; el dry-run no lo aplica).
    claves_proyectadas = set(usuarios_locales)
    for item in renombres:
        anterior = str(item.get("anterior") or "").strip()
        nuevo = str(item.get("nuevo") or "").strip()
        if anterior:
            claves_proyectadas.discard(anterior)
        if nuevo:
            claves_proyectadas.add(nuevo)

    return {
        "ok": bool(exportacion.get("ok")),
        "dry_run": bool(dry_run),
        "origen": exportacion.get("origen") or "mwx.app",
        "destino": exportacion.get("destino") or "",
        "exportadas": len(remotas),
        "locales_actuales": len(usuarios_locales),
        "existentes": len(usuarios_remotos & usuarios_locales),
        "nuevas": len(usuarios_remotos - usuarios_locales),
        "renombres": renombres,
        "total_renombres": len(renombres),
        "local_only_reales": local_only_reales,
        "total_local_only_reales": len(local_only_reales),
        "ambiguos": list(duplicados.get("ambiguos") or []),
        "omitidos": list(duplicados.get("omitidos") or []),
        "tras_renombres": {
            "existentes": len(usuarios_remotos & claves_proyectadas),
            "nuevas": len(usuarios_remotos - claves_proyectadas),
        },
        "stats": _estadisticas(remotas),
        "error": exportacion.get("error") or duplicados.get("error") or "",
    }


def _imprimir_resumen(resumen: Dict[str, Any], aplicacion: Optional[Dict[str, Any]] = None) -> None:
    """Salida legible. NUNCA imprime secretos (solo conteos y claves)."""
    modo = "REAL (escribe en la BD local)" if not resumen.get("dry_run") else "SIMULACION (dry-run)"
    print("=" * 64)
    print("Sincronizacion mwx.app -> SQLite local (solo tabla 'cuentas')")
    print("=" * 64)
    print(f"Origen    : {resumen.get('origen')}")
    print(f"Modo      : {modo}")
    print(f"Export JSON: {resumen.get('destino')}")
    print("-" * 64)
    print(f"Exportadas            : {resumen.get('exportadas')}")
    print(f"Locales actuales      : {resumen.get('locales_actuales')}")
    print(f"Existentes (update)   : {resumen.get('existentes')}")
    print(f"Nuevas (create)       : {resumen.get('nuevas')}")
    print(f"Renombres             : {resumen.get('total_renombres')}")
    print(f"Local-only reales     : {resumen.get('total_local_only_reales')} (se conservan)")
    print(f"Ambiguos              : {len(resumen.get('ambiguos') or [])}")
    print(f"Omitidos              : {len(resumen.get('omitidos') or [])}")

    stats = resumen.get("stats") or {}
    print("-" * 64)
    print(f"Tiers                 : {stats.get('por_tier')}")
    print(f"Secciones             : {stats.get('por_seccion')}")
    print(f"Tipos de cuenta       : {stats.get('por_tipo_cuenta')}")
    print(f"Roles de activacion   : {stats.get('por_rol_activacion')}")
    print(f"Pausadas / activas    : {stats.get('pausadas')} / {stats.get('activas')}")
    print(
        "cookies_json / auth   : "
        f"{stats.get('cookies_json_no_nulos')} / {stats.get('auth_token_no_vacio')}"
    )

    renombres = resumen.get("renombres") or []
    if renombres:
        print("-" * 64)
        print(f"Renombres propuestos ({len(renombres)}):")
        for item in renombres:
            print(
                f"  {item.get('anterior')} -> {item.get('nuevo')} "
                f"({item.get('motivo')})"
            )
    local_only = resumen.get("local_only_reales") or []
    if local_only:
        print("-" * 64)
        print(f"Local-only reales conservadas ({len(local_only)}):")
        for usuario in local_only[:30]:
            print(f"  {usuario}")
        if len(local_only) > 30:
            print(f"  ... y {len(local_only) - 30} mas")

    if aplicacion is not None:
        print("-" * 64)
        print("Aplicacion real")
        respaldo = aplicacion.get("respaldo") or {}
        print(f"  Respaldo SQLite : {respaldo.get('db') or '(omitido)'}")
        print(f"  Respaldo JSON   : {respaldo.get('cuentas_json') or '(omitido)'}")
        print(
            f"  Respaldo Change : {respaldo.get('change') or '(sin sesiones)'}"
        )
        print(f"  Renombres ok    : {sum(1 for r in aplicacion.get('renombres') or [] if r.get('ok'))}")
        restauracion = aplicacion.get("restauracion") or {}
        print(
            "  Restauracion    : "
            f"{restauracion.get('restauradas', 0)} creadas / "
            f"{restauracion.get('actualizadas', 0)} actualizadas"
        )
        verificacion = aplicacion.get("verificacion") or {}
        if verificacion:
            print(
                "  Verificacion    : "
                f"{'OK' if verificacion.get('ok') else 'CON DIFERENCIAS'} "
                f"({len(verificacion.get('mismatches') or [])} mismatches, "
                f"{len(verificacion.get('faltantes') or [])} faltantes, "
                f"{len(verificacion.get('local_only') or [])} local-only)"
            )
        if aplicacion.get("error"):
            print(f"  ERROR           : {aplicacion.get('error')}")
        else:
            print("  Estado          : aplicado correctamente")
    print("-" * 64)
    if resumen.get("dry_run"):
        print("AVISO: simulacion. No se modifico la BD local; usa --apply para aplicar.")
    print("SEGURIDAD: no se muestran URLs, passwords, tokens, cookies ni emails.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sincroniza TODAS las cuentas de mwx.app (PostgreSQL/Supabase) a la "
            "BD local SQLite. Por defecto es un DRY-RUN: exporta el JSON y "
            "calcula los cambios sin tocar la BD local."
        ),
        epilog=(
            "URL remota: --url > DATABASE_URL_REMOTE > Railway CLI. "
            "SEGURIDAD: no imprime credenciales. Solo se copia la tabla 'cuentas'."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Respalda, renombra duplicados y restaura de verdad en la BD local.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="URL PostgreSQL remota (si se omite: DATABASE_URL_REMOTE o Railway CLI).",
    )
    parser.add_argument(
        "--destino",
        default=None,
        help="Ruta del JSON de exportacion (default data/backups/mwx_cuentas_<ts>.json).",
    )
    parser.add_argument(
        "--json",
        dest="como_json",
        action="store_true",
        help="Imprime solo el resumen JSON (sin secretos).",
    )
    parser.add_argument(
        "--sin-renombrar",
        action="store_true",
        help="No propone ni aplica renombres de cuentas locales duplicadas.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fuerza simulacion aunque se pase --apply (no escribe en la BD).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = _construir_parser()
    args = parser.parse_args(argv)
    dry_run = bool(args.dry_run or not args.apply)

    try:
        url = obtener_url_remota(args.url)
    except Exception as exc:
        print(f"ERROR: no se pudo obtener la URL remota: {sanear_texto(exc)}")
        return 1

    exportacion = exportar_cuentas_remotas(url, args.destino)
    if not exportacion.get("ok"):
        mensaje = {
            "ok": False,
            "dry_run": dry_run,
            "error": exportacion.get("error") or "no se pudo exportar",
        }
        if args.como_json:
            print(json.dumps(mensaje, ensure_ascii=False, indent=2, default=str))
        else:
            print(
                "ERROR: no se pudo exportar las cuentas remotas: "
                f"{exportacion.get('error') or 'error desconocido'}"
            )
        return 1

    try:
        # El JSON recien escrito es la fuente de verdad (mismo formato que el
        # restaurador); asi el resumen y el restore usan exactamente lo exportado.
        remotas = _cargar_remotas_de_json(exportacion.get("destino"))
    except Exception as exc:
        mensaje = {
            "ok": False,
            "dry_run": dry_run,
            "error": f"no se pudo releer el JSON exportado: {sanear_texto(exc)}",
        }
        if args.como_json:
            print(json.dumps(mensaje, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"ERROR: no se pudo releer el JSON exportado: {sanear_texto(exc)}")
        return 1
    exportacion["cuentas"] = remotas

    try:
        locales = leer_cuentas_locales()
    except Exception as exc:
        mensaje = {
            "ok": False,
            "dry_run": dry_run,
            "error": f"no se pudo leer la BD local: {sanear_texto(exc)}",
        }
        if args.como_json:
            print(json.dumps(mensaje, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"ERROR: no se pudo leer la BD local: {sanear_texto(exc)}")
        return 1

    if args.sin_renombrar:
        usuarios_remotos = {
            str(fila.get("usuario") or "").strip() for fila in remotas
        }
        usuarios_locales = {
            str(fila.get("usuario") or "").strip() for fila in locales
        }
        duplicados = {
            "ok": True,
            "renombres": [],
            "ambiguos": [],
            "omitidos": [],
            "local_only_reales": sorted(usuarios_locales - usuarios_remotos),
            "error": "",
        }
    else:
        duplicados = detectar_duplicados_locales(
            remotas, cuentas_locales=locales
        )

    if not duplicados.get("ok"):
        resumen_error = construir_resumen(exportacion, locales, duplicados, dry_run)
        resumen_error["ok"] = False
        resumen_error["error"] = (
            duplicados.get("error") or "no se pudieron detectar los duplicados locales"
        )
        if args.como_json:
            print(json.dumps(resumen_error, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"ERROR: {resumen_error['error']}")
        return 1

    resumen = construir_resumen(exportacion, locales, duplicados, dry_run)
    aplicacion = None
    if not dry_run:
        aplicacion = aplicar(
            exportacion.get("destino"),
            renombres=duplicados.get("renombres") or [],
        )
        resumen["ok"] = bool(resumen.get("ok") and aplicacion.get("ok"))
        resumen["aplicacion"] = aplicacion
        if aplicacion.get("verificacion"):
            verificacion = aplicacion["verificacion"]
            resumen["verificacion"] = verificacion
            resumen["local_only_reales"] = verificacion.get("local_only") or []
            resumen["total_local_only_reales"] = len(resumen["local_only_reales"])
    elif duplicados.get("error"):
        resumen["ok"] = False
        resumen["error"] = duplicados["error"]

    if args.como_json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resumen, aplicacion)

    return 0 if resumen.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
