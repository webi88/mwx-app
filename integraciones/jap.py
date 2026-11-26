"""Cliente backend de Just Another Panel (JAP) API v2.

Solo backend: NO contiene UI ni ejecuta pedidos por si solo. La pagina
Streamlit consumira las funciones publicas de este modulo.

Contrato de credenciales
------------------------
La API key se resuelve en este orden (la primera NO vacia gana):

1. ``settings.jap_api_key`` (``core/config.py``, leida de ``JAP_API_KEY``).
2. Variable de entorno ``JAP_API_KEY``.
3. ``data/jap_config.json`` con ``{"api_key": "..."}`` (la escribe
   :func:`guardar_api_key` de forma atomica y con permisos 0600 en POSIX).

La key NUNCA se imprime, se registra en logs ni se devuelve en los errores.

Contrato publico (lo consume la UI)
-----------------------------------
``api_key``, ``guardar_api_key``, ``disponible``, ``obtener_balance``,
``listar_servicios``, ``clasificar_servicio``, ``es_servicio_x``,
``filtrar_servicios``, ``resumen_catalogo``, ``calcular_costo``,
``servicios_recomendados``, ``crear_orden``, ``estado_orden``,
``estado_ordenes``, ``refill_orden``, ``cancelar_orden``,
``saldo_suficiente``.

Todas las funciones publicas NUNCA lanzan: devuelven diccionarios con
``ok`` y, cuando aplica, un ``error`` en espanol.

Variables de entorno
--------------------
- ``JAP_API_KEY``: key de la API (opcional; tambien ``settings``/JSON).
- ``JAP_TIMEOUT_SEG``: timeout HTTP por peticion (default 30).
- ``JAP_CACHE_SEG``: TTL del catalogo de servicios (default 3600; 0 = sin
  cache). Aunque el TTL sea 0, ante un fallo de red se usa la ultima cache
  disponible como respaldo.

Notas del catalogo real (verificadas en vivo)
---------------------------------------------
- ``services`` devuelve ~5708 dicts con ``service``, ``name``, ``type``,
  ``category``, ``rate``, ``min``, ``max``, ``refill`` y ``cancel``.
- JAP NO tiene servicios de X especificos de Mexico. Para comentarios en
  espanol se usa un servicio del tipo ``Custom Comments`` (p. ej. 6602) y se
  envian las lineas en el parametro ``comments``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

API_URL = "https://justanotherpanel.com/api/v2"

TIPOS_SERVICIO = (
    "likes",
    "retweets",
    "views",
    "comments",
    "followers",
    "impressions",
    "mentions",
    "poll",
    "live",
    "packages",
    "other",
)

# Presets verificados contra el catalogo real (ids y precios de referencia):
#   9013  Twitter Likes 7D                 $0.275/1K   max 2,500
#   10186 Likes Refill 15D                 $1.80/1K    max 40,000
#   8860  Retweets                         $0.125/1K
#   8861  Retweets Refill 30D              $0.50/1K
#   8413  Tweet Views                      $0.0025/1K
#   8555  Tweet Views 365D                 $0.0063/1K
#   6601  USA Random Comments              $30.875/1K  min 5 max 150
#   6602  USA Custom Comments              $37.05/1K   min 5 max 150
#   9011  Followers                        $0.375/1K
#   8855  USA Followers Refill 30D         $0.85/1K
PRESETS = {
    "economico": {
        "likes": 9013,
        "retweets": 8860,
        "views": 8413,
        "comments": 6601,
        "followers": 9011,
    },
    "seguro": {
        "likes": 10186,
        "retweets": 8861,
        "views": 8555,
        "comments": 6602,
        "followers": 8855,
    },
}

# Marcas de nombre que distinguen el preset seguro/economico cuando el id
# esperado ya no existe en el catalogo (fallback por categoria/nombre).
_MARCAS_SEGURO = {
    "likes": ("refill",),
    "retweets": ("refill",),
    "views": ("365",),
    "comments": ("custom",),
    "followers": ("refill",),
}
_MARCAS_ECONOMICO = {
    "likes": (),
    "retweets": (),
    "views": (),
    "comments": ("random",),
    "followers": (),
}

_CACHE_BLOQUEO = threading.RLock()
_CACHE_MEMORIA: Dict[str, Any] = {"timestamp": 0.0, "servicios": []}


# ---------------------------------------------------------------------------
# Utilidades internas (sin red)
# ---------------------------------------------------------------------------
def _norm(texto: Any) -> str:
    """Minusculas sin acentos para comparar textos del catalogo."""
    cadena = str(texto or "").lower()
    descompuesto = unicodedata.normalize("NFKD", cadena)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _a_float(valor: Any) -> Optional[float]:
    try:
        if valor is None or isinstance(valor, bool):
            return None
        return float(str(valor).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _a_entero(valor: Any) -> Optional[int]:
    try:
        if valor is None or isinstance(valor, bool):
            return None
        if isinstance(valor, int):
            return valor
        if isinstance(valor, float):
            return int(valor) if valor.is_integer() else None
        texto = str(valor).strip()
        if not texto:
            return None
        numero = float(texto)
        if not numero.is_integer():
            return None
        return int(numero)
    except (TypeError, ValueError):
        return None


def _limpiar_error(texto: Any, clave: str = "") -> str:
    """Quita la API key de cualquier mensaje antes de devolverlo/loguearlo."""
    limpio = str(texto or "")
    if clave:
        limpio = limpio.replace(clave, "***")
    # Por si la key viajo como query string (`key=...`) en algun mensaje.
    limpio = re.sub(r"(?i)(key=)[^&\s]+", r"\1***", limpio)
    return limpio


def _api_key_settings() -> str:
    """Lee ``settings.jap_api_key`` con import perezoso (tests sin pydantic)."""
    try:
        from core.config import settings

        return str(getattr(settings, "jap_api_key", "") or "").strip()
    except Exception:
        return ""


def _ruta_config() -> str:
    """Ruta absoluta de ``data/jap_config.json`` (patcheable en tests)."""
    try:
        from core.config import resolver_ruta

        return resolver_ruta("data/jap_config.json")
    except Exception:
        return str(
            Path(__file__).resolve().parent.parent / "data" / "jap_config.json"
        )


def _ruta_cache() -> str:
    """Ruta absoluta de ``data/jap_services_cache.json`` (patcheable en tests)."""
    try:
        from core.config import resolver_ruta

        return resolver_ruta("data/jap_services_cache.json")
    except Exception:
        return str(
            Path(__file__).resolve().parent.parent
            / "data"
            / "jap_services_cache.json"
        )


def _timeout() -> float:
    try:
        valor = float(str(os.environ.get("JAP_TIMEOUT_SEG", "30")).strip())
    except (TypeError, ValueError):
        valor = 30.0
    return max(1.0, min(valor, 300.0))


def _cache_seg() -> int:
    try:
        valor = int(float(str(os.environ.get("JAP_CACHE_SEG", "3600")).strip()))
    except (TypeError, ValueError):
        valor = 3600
    return max(0, valor)


def _leer_config() -> str:
    try:
        ruta = Path(_ruta_config())
        if not ruta.is_file():
            return ""
        with ruta.open("r", encoding="utf-8") as manejador:
            contenido = json.load(manejador)
        if isinstance(contenido, dict):
            return str(contenido.get("api_key") or "").strip()
    except Exception:
        return ""
    return ""


def _leer_cache_disco() -> Tuple[List[dict], float]:
    """Devuelve (servicios, timestamp). Nunca lanza."""
    try:
        ruta = Path(_ruta_cache())
        if not ruta.is_file():
            return [], 0.0
        with ruta.open("r", encoding="utf-8") as manejador:
            contenido = json.load(manejador)
        if isinstance(contenido, dict):
            servicios = contenido.get("servicios") or []
            timestamp = _a_float(contenido.get("timestamp")) or 0.0
        elif isinstance(contenido, list):
            servicios = contenido
            timestamp = ruta.stat().st_mtime
        else:
            return [], 0.0
        if not isinstance(servicios, list):
            return [], 0.0
        limpios = [s for s in servicios if isinstance(s, dict)]
        if not limpios:
            return [], 0.0
        return limpios, timestamp
    except Exception:
        return [], 0.0


def _guardar_cache_disco(servicios: List[dict]) -> None:
    """Escritura atomica de la cache del catalogo. Nunca lanza."""
    temporal = ""
    try:
        ruta = Path(_ruta_cache())
        ruta.parent.mkdir(parents=True, exist_ok=True)
        contenido = json.dumps(
            {"timestamp": time.time(), "servicios": servicios},
            ensure_ascii=False,
        )
        descriptor, temporal = tempfile.mkstemp(
            prefix=".jap_services_", dir=str(ruta.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as manejador:
            manejador.write(contenido)
            manejador.flush()
            os.fsync(manejador.fileno())
        os.replace(temporal, ruta)
        temporal = ""
    except Exception:
        logger.debug("JAP: no se pudo guardar la cache del catalogo")
    finally:
        if temporal:
            try:
                os.unlink(temporal)
            except OSError:
                pass


def _limpiar_cache() -> None:
    """Limpia la cache en memoria (util para pruebas)."""
    with _CACHE_BLOQUEO:
        _CACHE_MEMORIA["timestamp"] = 0.0
        _CACHE_MEMORIA["servicios"] = []


def _peticion(accion: str, **parametros: Any) -> Tuple[Any, str]:
    """POST form a JAP. Devuelve ``(datos, error)``; nunca lanza.

    ``datos`` puede ser dict o list (accion ``services``). El error ya viene
    saneado: nunca contiene la API key.
    """
    clave = api_key()
    if not clave:
        return None, "Falta configurar la API key de JAP (JAP_API_KEY)"

    datos: Dict[str, Any] = {"key": clave, "action": accion}
    for nombre, valor in parametros.items():
        if valor is None:
            continue
        datos[str(nombre)] = valor

    try:
        respuesta = requests.post(API_URL, data=datos, timeout=_timeout())
    except requests.RequestException as exc:
        return None, _limpiar_error(f"Error de red al conectar con JAP: {exc}", clave)
    except Exception as exc:  # nunca dejar escapar una excepcion
        return None, _limpiar_error(
            f"Error inesperado al conectar con JAP: {exc}", clave
        )

    estado_http = getattr(respuesta, "status_code", 200)
    if isinstance(estado_http, int) and estado_http >= 500:
        return None, f"JAP respondio HTTP {estado_http}"

    try:
        cuerpo = respuesta.json()
    except Exception:
        texto = str(getattr(respuesta, "text", "") or "")[:200]
        detalle = f": {texto}" if texto else ""
        return None, _limpiar_error(
            f"JAP devolvio una respuesta que no es JSON valido{detalle}", clave
        )

    if isinstance(cuerpo, dict) and cuerpo.get("error"):
        return None, _limpiar_error(f"JAP: {cuerpo.get('error')}", clave)
    return cuerpo, ""


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------
def api_key() -> str:
    """API key efectiva: settings -> env ``JAP_API_KEY`` -> JSON local."""
    clave = _api_key_settings()
    if clave:
        return clave
    clave = str(os.environ.get("JAP_API_KEY") or "").strip()
    if clave:
        return clave
    return _leer_config()


def guardar_api_key(key: str) -> dict:
    """Guarda la key en ``data/jap_config.json`` de forma atomica.

    Devuelve ``{"ok": bool, "error": str}``. En POSIX el archivo queda con
    permisos 0600. Nunca imprime ni devuelve la key.
    """
    clave = str(key or "").strip()
    if not clave:
        return {"ok": False, "error": "La API key no puede estar vacia"}

    temporal = ""
    try:
        ruta = Path(_ruta_config())
        ruta.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(
            prefix=".jap_config_", dir=str(ruta.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as manejador:
            manejador.write(json.dumps({"api_key": clave}, ensure_ascii=False))
            manejador.flush()
            os.fsync(manejador.fileno())
        if os.name != "nt":
            os.chmod(temporal, 0o600)
        os.replace(temporal, ruta)
        temporal = ""
        logger.info("JAP: API key guardada en la configuracion local")
        return {"ok": True, "error": ""}
    except Exception as exc:
        return {
            "ok": False,
            "error": _limpiar_error(f"No se pudo guardar la API key: {exc}", clave),
        }
    finally:
        if temporal:
            try:
                os.unlink(temporal)
            except OSError:
                pass


def disponible() -> bool:
    """True si hay una API key configurada."""
    return bool(api_key())


# ---------------------------------------------------------------------------
# Endpoints de lectura
# ---------------------------------------------------------------------------
def obtener_balance() -> dict:
    """``action=balance``. Devuelve ``{"ok","balance","currency","error"}``."""
    resultado = {
        "ok": False,
        "balance": 0.0,
        "currency": "",
        "error": "",
    }
    try:
        datos, error = _peticion("balance")
        if error:
            resultado["error"] = error
            return resultado
        if not isinstance(datos, dict):
            resultado["error"] = "JAP devolvio un saldo con formato inesperado"
            return resultado
        saldo = _a_float(datos.get("balance"))
        if saldo is None:
            resultado["error"] = "JAP no devolvio un saldo valido"
            return resultado
        resultado.update(
            {
                "ok": True,
                "balance": saldo,
                "currency": str(datos.get("currency") or ""),
            }
        )
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo consultar el saldo: {exc}", api_key()
        )
        return resultado


def listar_servicios(forzar: bool = False) -> dict:
    """Catalogo completo de servicios (``action=services``) con cache.

    Cache en memoria y en disco (``data/jap_services_cache.json``) con TTL
    ``JAP_CACHE_SEG`` (0 = sin cache fresca). Si la red falla y hay cache, se
    devuelve ``ok=True`` con el ``error`` informativo. Nunca lanza.
    """
    resultado = {
        "ok": False,
        "servicios": [],
        "error": "",
        "desde_cache": False,
    }
    ttl = _cache_seg()
    ahora = time.time()

    if not forzar:
        if ttl > 0:
            with _CACHE_BLOQUEO:
                memoria = list(_CACHE_MEMORIA.get("servicios") or [])
                timestamp_memoria = _a_float(_CACHE_MEMORIA.get("timestamp")) or 0.0
            if memoria and (ahora - timestamp_memoria) < ttl:
                resultado.update(
                    {"ok": True, "servicios": memoria, "desde_cache": True}
                )
                return resultado

            servicios_disco, timestamp_disco = _leer_cache_disco()
            if servicios_disco and (ahora - timestamp_disco) < ttl:
                with _CACHE_BLOQUEO:
                    _CACHE_MEMORIA["servicios"] = servicios_disco
                    _CACHE_MEMORIA["timestamp"] = timestamp_disco
                resultado.update(
                    {"ok": True, "servicios": servicios_disco, "desde_cache": True}
                )
                return resultado

    datos, error = _peticion("services")
    if isinstance(datos, list):
        servicios = [s for s in datos if isinstance(s, dict)]
        if servicios:
            with _CACHE_BLOQUEO:
                _CACHE_MEMORIA["servicios"] = servicios
                _CACHE_MEMORIA["timestamp"] = time.time()
            _guardar_cache_disco(servicios)
            resultado.update({"ok": True, "servicios": servicios})
            return resultado
        error = error or "JAP devolvio un catalogo vacio"
    elif datos is not None:
        error = error or "JAP devolvio un catalogo con formato inesperado"

    error = error or "No se pudo obtener el catalogo de servicios de JAP"

    # Respaldo 1: cache en memoria (aunque haya vencido el TTL).
    with _CACHE_BLOQUEO:
        memoria = list(_CACHE_MEMORIA.get("servicios") or [])
    if memoria:
        resultado.update(
            {
                "ok": True,
                "servicios": memoria,
                "desde_cache": True,
                "error": f"No se pudo actualizar el catalogo: {error}. Se usa la cache en memoria.",
            }
        )
        return resultado

    # Respaldo 2: cache en disco (aunque haya vencido el TTL).
    servicios_disco, timestamp_disco = _leer_cache_disco()
    if servicios_disco:
        with _CACHE_BLOQUEO:
            _CACHE_MEMORIA["servicios"] = servicios_disco
            _CACHE_MEMORIA["timestamp"] = timestamp_disco
        resultado.update(
            {
                "ok": True,
                "servicios": servicios_disco,
                "desde_cache": True,
                "error": f"No se pudo actualizar el catalogo: {error}. Se usa la cache en disco.",
            }
        )
        return resultado

    resultado["error"] = error
    return resultado


# ---------------------------------------------------------------------------
# Clasificacion / filtros del catalogo
# ---------------------------------------------------------------------------
def clasificar_servicio(servicio: dict) -> str:
    """Clasifica un servicio en uno de ``TIPOS_SERVICIO``.

    Se basa SOLO en ``category`` y ``name`` (no en ``type``) y es tolerante a
    acentos/mayusculas. Nunca lanza.
    """
    try:
        if not isinstance(servicio, dict):
            return "other"
        texto = _norm(
            f"{servicio.get('category') or ''} {servicio.get('name') or ''}"
        )
        if not texto.strip():
            return "other"

        if any(
            marca in texto
            for marca in ("package", "bundle", "combo", "paquete", "megapack")
        ):
            return "packages"
        if "poll" in texto or re.search(r"\bvotos?\b|\bvotes?\b", texto):
            return "poll"
        if re.search(r"\bspaces?\b|espacios?\b", texto):
            return "live"
        if "live" in texto and "view" not in texto:
            return "live"
        if "impression" in texto:
            return "impressions"
        if (
            "retweet" in texto
            or "repost" in texto
            or "quote tweet" in texto
            or re.search(r"\bquotes?\b", texto)
        ):
            return "retweets"
        if "like" in texto or "favorite" in texto:
            return "likes"
        if "view" in texto or "reproduccion" in texto or re.search(r"\bplays?\b", texto):
            return "views"
        if (
            "comment" in texto
            or "reply" in texto
            or "replies" in texto
            or "comentario" in texto
        ):
            return "comments"
        if (
            "follower" in texto
            or "subscriber" in texto
            or "seguidor" in texto
            or "members" in texto
        ):
            return "followers"
        if "mention" in texto or "mencion" in texto:
            return "mentions"
        return "other"
    except Exception:
        return "other"


def es_servicio_x(servicio: dict) -> bool:
    """True si el servicio pertenece a X/Twitter (por ``category``/``name``)."""
    try:
        if not isinstance(servicio, dict):
            return False
        categoria = _norm(servicio.get("category"))
        nombre = _norm(servicio.get("name"))
        texto = f"{categoria} {nombre}"
        if "twitter" in texto:
            return True
        return bool(re.search(r"\bx\b", categoria) or re.search(r"\bx\b", nombre))
    except Exception:
        return False


def _plataforma_servicio(servicio: dict) -> str:
    """Plataforma deducida del catalogo (tolerante; ``otra`` si no se sabe)."""
    texto = _norm(
        f"{servicio.get('category') or ''} {servicio.get('name') or ''}"
    )
    for clave in (
        "twitter",
        "facebook",
        "instagram",
        "tiktok",
        "youtube",
        "telegram",
        "spotify",
        "discord",
        "threads",
        "linkedin",
    ):
        if clave in texto:
            return "twitter" if clave == "twitter" else clave
    if re.search(r"\bx\b", texto):
        return "twitter"
    return "otra"


def filtrar_servicios(plataforma: str = "x", tipo: str = "", texto: str = "") -> dict:
    """Filtra el catalogo por plataforma, tipo y texto.

    ``plataforma``: ``"x"``/``"twitter"`` (solo X), ``""``/``"todas"`` (todo) o
    el nombre de otra plataforma. ``tipo`` usa :func:`clasificar_servicio`.
    ``texto`` busca en ``name``, ``category`` e ``id``.
    """
    resultado = {"ok": False, "servicios": [], "error": ""}
    try:
        datos = listar_servicios()
        if not datos.get("ok"):
            resultado["error"] = datos.get("error") or (
                "No se pudo obtener el catalogo de JAP"
            )
            return resultado

        plataforma_norm = _norm(plataforma).strip()
        tipo_norm = _norm(tipo).strip()
        busqueda = _norm(texto).strip()

        filtrados = []
        for servicio in datos.get("servicios") or []:
            if plataforma_norm in ("", "todas", "all", "cualquiera"):
                pasa_plataforma = True
            elif plataforma_norm in ("x", "twitter"):
                pasa_plataforma = es_servicio_x(servicio)
            else:
                pasa_plataforma = plataforma_norm in _norm(
                    f"{servicio.get('category') or ''} "
                    f"{servicio.get('name') or ''}"
                )
            if not pasa_plataforma:
                continue

            if tipo_norm and clasificar_servicio(servicio) != tipo_norm:
                continue

            if busqueda:
                campos = _norm(
                    f"{servicio.get('name') or ''} "
                    f"{servicio.get('category') or ''} "
                    f"{servicio.get('service') or ''}"
                )
                if busqueda not in campos:
                    continue

            filtrados.append(servicio)

        resultado.update({"ok": True, "servicios": filtrados})
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo filtrar el catalogo: {exc}", api_key()
        )
        return resultado


def resumen_catalogo() -> dict:
    """Conteos del catalogo por plataforma y por tipo (tolerante a fallos)."""
    resultado = {
        "ok": False,
        "total": 0,
        "plataformas": {},
        "tipos": {},
        "por_plataforma": {},
        "por_tipo": {},
        "tipos_x": {},
        "total_x": 0,
        "error": "",
    }
    try:
        datos = listar_servicios()
        if not datos.get("ok"):
            resultado["error"] = datos.get("error") or (
                "No se pudo obtener el catalogo de JAP"
            )
            return resultado

        servicios = datos.get("servicios") or []
        plataformas: Dict[str, int] = {}
        tipos: Dict[str, int] = {}
        tipos_x: Dict[str, int] = {tipo: 0 for tipo in TIPOS_SERVICIO}

        for servicio in servicios:
            plataforma = _plataforma_servicio(servicio)
            plataformas[plataforma] = plataformas.get(plataforma, 0) + 1
            tipo = clasificar_servicio(servicio)
            tipos[tipo] = tipos.get(tipo, 0) + 1
            if es_servicio_x(servicio):
                tipos_x[tipo] = tipos_x.get(tipo, 0) + 1

        resultado.update(
            {
                "ok": True,
                "total": len(servicios),
                "plataformas": plataformas,
                "tipos": tipos,
                "por_plataforma": dict(plataformas),
                "por_tipo": dict(tipos),
                "tipos_x": tipos_x,
                "total_x": sum(tipos_x.values()),
            }
        )
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo resumir el catalogo: {exc}", api_key()
        )
        return resultado


# ---------------------------------------------------------------------------
# Costos y recomendaciones
# ---------------------------------------------------------------------------
def _resolver_servicio(servicio: Any) -> Tuple[Optional[dict], str]:
    """Acepta el dict del servicio o su id y devuelve el dict del catalogo."""
    if isinstance(servicio, dict):
        return dict(servicio), ""
    identificador = _a_entero(servicio)
    if identificador is None:
        return None, "El servicio debe ser un id numerico o un diccionario"
    datos = listar_servicios()
    if not datos.get("ok"):
        return None, datos.get("error") or "No se pudo consultar el catalogo de JAP"
    for candidato in datos.get("servicios") or []:
        if _a_entero(candidato.get("service")) == identificador:
            return candidato, ""
    return None, f"No existe el servicio {identificador} en el catalogo de JAP"


def calcular_costo(servicio: Any, cantidad: Any) -> dict:
    """Costo estimado: ``rate * cantidad / 1000`` (redondeado a 6 decimales).

    Acepta el dict del servicio o su id. Valida ``min``/``max``. Nunca lanza.
    """
    resultado = {
        "ok": False,
        "servicio_id": None,
        "cantidad": 0,
        "rate": 0.0,
        "costo": 0.0,
        "moneda": "USD",
        "min": 0,
        "max": 0,
        "error": "",
    }
    try:
        info, error = _resolver_servicio(servicio)
        if not info:
            resultado["error"] = error
            return resultado

        servicio_id = _a_entero(info.get("service"))
        rate = _a_float(info.get("rate"))
        minimo = _a_entero(info.get("min"))
        maximo = _a_entero(info.get("max"))
        cantidad_int = _a_entero(cantidad)

        resultado.update(
            {
                "servicio_id": servicio_id,
                "rate": round(rate, 8) if rate is not None else 0.0,
                "min": minimo if minimo is not None else 0,
                "max": maximo if maximo is not None else 0,
            }
        )

        if cantidad_int is None or cantidad_int <= 0:
            resultado["error"] = "La cantidad debe ser un numero entero mayor a 0"
            return resultado
        resultado["cantidad"] = cantidad_int

        if minimo is not None and cantidad_int < minimo:
            resultado["error"] = (
                f"La cantidad minima para este servicio es {minimo}"
            )
            return resultado
        if maximo is not None and maximo > 0 and cantidad_int > maximo:
            resultado["error"] = (
                f"La cantidad maxima para este servicio es {maximo}"
            )
            return resultado
        if rate is None or rate <= 0:
            resultado["error"] = "El servicio no tiene una tarifa valida"
            return resultado

        resultado["costo"] = round(rate * cantidad_int / 1000.0, 6)
        resultado["ok"] = True
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo calcular el costo: {exc}", api_key()
        )
        return resultado


def _elegir_por_rol(
    servicios: List[dict], rol: str, seguro: bool
) -> Optional[dict]:
    """Mejor servicio X disponible para un rol cuando el id preset no existe."""
    candidatos = [
        s
        for s in servicios
        if es_servicio_x(s) and clasificar_servicio(s) == rol
    ]
    if not candidatos:
        return None

    marcas = _MARCAS_SEGURO.get(rol, ()) if seguro else _MARCAS_ECONOMICO.get(rol, ())

    def puntaje(servicio: dict):
        nombre = _norm(servicio.get("name"))
        tarifa = _a_float(servicio.get("rate"))
        maximo = _a_entero(servicio.get("max"))
        servicio_id = _a_entero(servicio.get("service"))
        preferencia = 0
        if marcas:
            if any(marca in nombre for marca in marcas):
                preferencia -= 10
            else:
                preferencia += 10 if seguro else 0
        if seguro:
            if "refill" in nombre and "refill" not in marcas:
                preferencia += 5
        else:
            if "refill" in nombre:
                preferencia += 5
            if rol == "comments" and "custom" in nombre:
                preferencia += 5
        return (
            preferencia,
            tarifa if tarifa is not None else 999999.0,
            -(maximo if maximo is not None else 0),
            servicio_id if servicio_id is not None else 0,
        )

    return sorted(candidatos, key=puntaje)[0]


def servicios_recomendados() -> dict:
    """Presets ``economico`` y ``seguro`` resueltos contra el catalogo real.

    Si un id verificado ya no existe, se busca por categoria/nombre el mejor
    servicio X disponible (sin refill para economico; con refill/365D/custom
    para seguro). Nunca lanza.
    """
    presets: Dict[str, Dict[str, dict]] = {"economico": {}, "seguro": {}}
    try:
        datos = listar_servicios()
        if not datos.get("ok"):
            return {
                "ok": False,
                "presets": presets,
                "error": datos.get("error") or (
                    "No se pudo obtener el catalogo de JAP"
                ),
            }

        servicios = datos.get("servicios") or []
        indice: Dict[int, dict] = {}
        for servicio in servicios:
            servicio_id = _a_entero(servicio.get("service"))
            if servicio_id is not None:
                indice[servicio_id] = servicio

        faltantes: List[str] = []
        for nombre_preset, roles in PRESETS.items():
            seguro = nombre_preset == "seguro"
            for rol, servicio_esperado in roles.items():
                servicio = indice.get(servicio_esperado)
                if servicio is None:
                    servicio = _elegir_por_rol(servicios, rol, seguro=seguro)
                if servicio is None:
                    faltantes.append(f"{nombre_preset}/{rol}")
                    continue
                presets[nombre_preset][rol] = {
                    "servicio": servicio,
                    "rate": _a_float(servicio.get("rate")) or 0.0,
                    "min": _a_entero(servicio.get("min")) or 0,
                    "max": _a_entero(servicio.get("max")) or 0,
                    "nombre": str(servicio.get("name") or ""),
                }

        if not any(presets.values()):
            return {
                "ok": False,
                "presets": presets,
                "error": "No hay servicios X disponibles para los presets",
            }

        error = ""
        if faltantes:
            error = "No se encontraron servicios para: " + ", ".join(faltantes)
        return {"ok": True, "presets": presets, "error": error}
    except Exception as exc:
        return {
            "ok": False,
            "presets": presets,
            "error": _limpiar_error(
                f"No se pudieron calcular los presets: {exc}", api_key()
            ),
        }


# ---------------------------------------------------------------------------
# Ordenes
# ---------------------------------------------------------------------------
def _normalizar_ids(ids: Any) -> Optional[List[str]]:
    """Lista/string de ids -> lista de strings validos (o ``None`` si invalido)."""
    if ids is None:
        return None
    if isinstance(ids, (str, int)):
        partes: List[Any] = [ids]
    else:
        try:
            partes = list(ids)
        except TypeError:
            return None

    limpios: List[str] = []
    for parte in partes:
        if isinstance(parte, str) and "," in parte:
            subpartes: List[Any] = parte.split(",")
        else:
            subpartes = [parte]
        for subparte in subpartes:
            numero = _a_entero(subparte)
            if numero is None or numero <= 0:
                return None
            texto = str(numero)
            if texto not in limpios:
                limpios.append(texto)
    return limpios


def crear_orden(
    servicio: Any,
    link: str,
    cantidad: Any,
    runs: int = 0,
    interval: int = 0,
    comments: str = "",
) -> dict:
    """Crea una orden real en JAP (``action=add``).

    Valida URL http(s), cantidad entera dentro de ``min``/``max``, servicio
    existente y saldo suficiente (consulta el balance ANTES de enviar). Para
    servicios ``Custom Comments`` exige ``comments`` y que haya al menos una
    linea por unidad pedida. Nunca lanza y nunca registra la API key.
    """
    resultado = {"ok": False, "order": None, "costo": 0.0, "error": ""}
    try:
        info, error = _resolver_servicio(servicio)
        if not info:
            resultado["error"] = error
            return resultado

        enlace = str(link or "").strip()
        partes = urlparse(enlace)
        if partes.scheme not in ("http", "https") or not partes.netloc:
            resultado["error"] = "El link debe ser una URL http(s) valida"
            return resultado

        calculo = calcular_costo(info, cantidad)
        resultado["costo"] = calculo.get("costo") or 0.0
        if not calculo.get("ok"):
            resultado["error"] = calculo.get("error") or "Cantidad invalida"
            return resultado

        servicio_id = calculo.get("servicio_id")
        cantidad_int = calculo.get("cantidad") or 0
        if servicio_id is None or servicio_id <= 0:
            resultado["error"] = "El servicio no tiene un id valido para ordenar"
            return resultado
        nombre = _norm(info.get("name"))
        tipo_servicio = _norm(info.get("type"))
        es_custom = "custom" in nombre or "custom comments" in tipo_servicio

        lineas: List[str] = []
        if es_custom:
            lineas = [
                linea.strip()
                for linea in str(comments or "").splitlines()
                if linea.strip()
            ]
            if not lineas:
                resultado["error"] = (
                    "Este servicio es de comentarios personalizados: "
                    "escribe al menos un comentario (uno por linea)"
                )
                return resultado
            if len(lineas) < cantidad_int:
                resultado["error"] = (
                    f"Faltan comentarios: pediste {cantidad_int} y solo hay "
                    f"{len(lineas)} linea(s)"
                )
                return resultado

        saldo = obtener_balance()
        if not saldo.get("ok"):
            resultado["error"] = (
                "No se pudo consultar el saldo: "
                + (saldo.get("error") or "error desconocido")
            )
            return resultado

        balance = _a_float(saldo.get("balance")) or 0.0
        costo = _a_float(calculo.get("costo")) or 0.0
        if balance < costo:
            resultado["error"] = (
                f"Saldo insuficiente: necesitas ${costo:.6f} y tienes "
                f"${balance:.6f}"
            )
            return resultado

        parametros: Dict[str, Any] = {
            "service": str(servicio_id),
            "link": enlace,
            "quantity": str(cantidad_int),
        }
        repeticiones = _a_entero(runs)
        if repeticiones is not None and repeticiones > 0:
            parametros["runs"] = str(repeticiones)
        intervalo = _a_entero(interval)
        if intervalo is not None and intervalo > 0:
            parametros["interval"] = str(intervalo)
        if es_custom:
            parametros["comments"] = "\n".join(lineas[:cantidad_int])

        datos, error = _peticion("add", **parametros)
        if error:
            resultado["error"] = error
            return resultado
        if not isinstance(datos, dict):
            resultado["error"] = "JAP devolvio una respuesta inesperada al crear la orden"
            return resultado

        numero = _a_entero(datos.get("order"))
        if numero is None:
            resultado["error"] = "JAP no devolvio el numero de orden"
            return resultado

        resultado.update({"ok": True, "order": numero})
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo crear la orden: {exc}", api_key()
        )
        return resultado


def estado_orden(order_id: Any) -> dict:
    """Estado de UNA orden (``action=status&orders=<id>``). Nunca lanza."""
    resultado = {
        "ok": False,
        "order": None,
        "charge": 0.0,
        "start_count": 0,
        "status": "",
        "remains": 0,
        "currency": "",
        "error": "",
    }
    try:
        numero = _a_entero(order_id)
        if numero is None or numero <= 0:
            resultado["order"] = order_id if order_id is not None else None
            resultado["error"] = "El numero de orden debe ser un entero mayor a 0"
            return resultado
        resultado["order"] = numero

        datos, error = _peticion("status", orders=str(numero))
        if error:
            resultado["error"] = error
            return resultado
        if not isinstance(datos, dict):
            resultado["error"] = (
                "JAP devolvio una respuesta inesperada al consultar la orden"
            )
            return resultado

        info = datos.get(str(numero))
        if info is None:
            info = datos.get(numero)
        if not isinstance(info, dict):
            # Algunos paneles devuelven los campos directos para una sola orden.
            if any(
                clave in datos
                for clave in ("charge", "status", "remains", "start_count")
            ):
                info = datos
            else:
                resultado["error"] = (
                    f"JAP no devolvio datos para la orden {numero}"
                )
                return resultado

        if info.get("error"):
            resultado["error"] = _limpiar_error(
                f"JAP: {info.get('error')}", api_key()
            )
            return resultado
        if not info:
            resultado["error"] = (
                f"JAP devolvio una respuesta vacia para la orden {numero}"
            )
            return resultado

        resultado.update(
            {
                "charge": _a_float(info.get("charge")) or 0.0,
                "start_count": _a_entero(info.get("start_count")) or 0,
                "status": str(info.get("status") or ""),
                "remains": _a_entero(info.get("remains")) or 0,
                "currency": str(info.get("currency") or ""),
            }
        )
        resultado["ok"] = True
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo consultar la orden: {exc}", api_key()
        )
        return resultado


def estado_ordenes(ids: Any) -> dict:
    """Estado de VARIAS ordenes (``action=status&orders=1,2,3``). Nunca lanza."""
    resultado = {"ok": False, "ordenes": {}, "error": ""}
    try:
        limpios = _normalizar_ids(ids)
        if limpios is None:
            resultado["error"] = "Los ids deben ser enteros mayores a 0"
            return resultado
        if not limpios:
            resultado["error"] = "Debes indicar al menos un id de orden"
            return resultado

        datos, error = _peticion("status", orders=",".join(limpios))
        if error:
            resultado["error"] = error
            return resultado
        if not isinstance(datos, dict):
            resultado["error"] = (
                "JAP devolvio una respuesta inesperada al consultar las ordenes"
            )
            return resultado

        resultado.update({"ok": True, "ordenes": datos})
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudieron consultar las ordenes: {exc}", api_key()
        )
        return resultado


def refill_orden(order_id: Any) -> dict:
    """Solicita refill de una orden (``action=refill``). Nunca lanza."""
    resultado = {"ok": False, "refill": None, "resultado": {}, "error": ""}
    try:
        numero = _a_entero(order_id)
        if numero is None or numero <= 0:
            resultado["error"] = "El numero de orden debe ser un entero mayor a 0"
            return resultado

        datos, error = _peticion("refill", order=str(numero))
        if error:
            resultado["error"] = error
            return resultado

        if isinstance(datos, dict):
            resultado["resultado"] = datos
            resultado["refill"] = _a_entero(datos.get("refill")) or numero
            resultado["ok"] = True
            return resultado
        if isinstance(datos, list):
            resultado["resultado"] = {"refill": datos}
            resultado["refill"] = datos
            resultado["ok"] = True
            return resultado

        resultado["error"] = "JAP devolvio una respuesta inesperada al pedir refill"
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo pedir el refill: {exc}", api_key()
        )
        return resultado


def cancelar_orden(ids: Any) -> dict:
    """Cancela una o varias ordenes (``action=cancel&orders=...``). Nunca lanza."""
    resultado = {"ok": False, "cancel": None, "resultado": {}, "error": ""}
    try:
        limpios = _normalizar_ids(ids)
        if limpios is None:
            resultado["error"] = "Los ids deben ser enteros mayores a 0"
            return resultado
        if not limpios:
            resultado["error"] = "Debes indicar al menos un id de orden"
            return resultado

        datos, error = _peticion("cancel", orders=",".join(limpios))
        if error:
            resultado["error"] = error
            return resultado

        if isinstance(datos, dict):
            resultado["resultado"] = datos
            resultado["cancel"] = datos.get("cancel", datos)
            resultado["ok"] = True
            return resultado
        if isinstance(datos, list):
            resultado["resultado"] = {"cancel": datos}
            resultado["cancel"] = datos
            resultado["ok"] = True
            return resultado

        resultado["error"] = "JAP devolvio una respuesta inesperada al cancelar"
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudieron cancelar las ordenes: {exc}", api_key()
        )
        return resultado


def saldo_suficiente(costo: Any) -> dict:
    """Compara el saldo actual con ``costo``. Nunca lanza."""
    resultado = {
        "ok": False,
        "balance": 0.0,
        "costo": 0.0,
        "suficiente": False,
        "error": "",
    }
    try:
        valor = _a_float(costo)
        if valor is None or valor < 0:
            resultado["error"] = "El costo debe ser un numero mayor o igual a 0"
            return resultado
        resultado["costo"] = valor

        saldo = obtener_balance()
        if not saldo.get("ok"):
            resultado["error"] = saldo.get("error") or (
                "No se pudo consultar el saldo"
            )
            return resultado

        balance = _a_float(saldo.get("balance")) or 0.0
        resultado.update(
            {
                "ok": True,
                "balance": balance,
                "suficiente": balance >= valor,
            }
        )
        return resultado
    except Exception as exc:
        resultado["error"] = _limpiar_error(
            f"No se pudo comparar el saldo: {exc}", api_key()
        )
        return resultado


__all__ = [
    "API_URL",
    "TIPOS_SERVICIO",
    "PRESETS",
    "api_key",
    "guardar_api_key",
    "disponible",
    "obtener_balance",
    "listar_servicios",
    "clasificar_servicio",
    "es_servicio_x",
    "filtrar_servicios",
    "resumen_catalogo",
    "calcular_costo",
    "servicios_recomendados",
    "crear_orden",
    "estado_orden",
    "estado_ordenes",
    "refill_orden",
    "cancelar_orden",
    "saldo_suficiente",
]
