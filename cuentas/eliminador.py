"""Eliminacion masiva de cuentas por lista, con respaldo previo.

Este modulo es la version reutilizable (CLI + dashboard) de
`web/operaciones/cuentas.py::_eliminar_cuentas`: en vez de borrar a ciegas,
primero busca las cuentas (case-insensitive), escribe un respaldo JSON con
TODAS sus columnas + las tareas relacionadas y recien entonces hace el DELETE
en UNA transaccion. Ademas puede borrar las cookies locales (`.pkl`) y las
imagenes (avatar/portada) SOLO si viven dentro de `data/avatars`/`data/portadas`,
y cancelar las tareas programadas que dependian exclusivamente de esas cuentas.

Flujo recomendado (dry-run -> apply):

    from cuentas.eliminador import parsear_lista_usuarios, buscar_cuentas, eliminar_usuarios

    usuarios = parsear_lista_usuarios(texto_pegado)
    previa = buscar_cuentas(usuarios)          # no escribe nada
    print(previa["encontradas"], previa["no_encontradas"])

    resultado = eliminar_usuarios(usuarios)    # respalda y elimina
    print(resultado["eliminadas"], resultado["respaldo"])

El respaldo (mismo formato que `cuentas/restaurador.py`, que puede reponerlo)
queda en `data/backups/eliminacion_cuentas_YYYYMMDD_HHMMSS.json`:

    {
      "fecha": "2026-09-22T12:00:00.123456",
      "host": "...",
      "keep": [],
      "cuentas": [ {..todas las columnas de la tabla `cuentas`..}, ... ],
      "tareas":  [ {..tareas que referenciaban esas cuentas..}, ... ]
    }

Reglas:
    - NUNCA lanza: los fallos se devuelven en `error` (string) y el resto de
      claves queda vacio/0.
    - Si el respaldo falla, NO se elimina nada.
    - El DELETE va en UNA transaccion (`get_db_session`): si algo revienta se
      hace rollback y no queda nada a medias.
    - `buscar_cuentas` y `eliminar_usuarios` buscan por `Cuenta.usuario` con
      `func.lower` (X no distingue mayusculas al cambiar de @, la clave interna
      pudo escribirse con otra caja).
    - `borrar_cookies=True` tambien borra avatar/portada, pero JAMAS archivos
      fuera de `data/cookies/<plataforma>/`, `data/avatares/` y `data/portadas/`
      (las rutas se validan resueltas para evitar `..`).

Las carpetas son constantes a nivel de modulo para que los tests apunten a un
tmp: `CARPETA_COOKIES`, `CARPETA_AVATARES`, `CARPETA_PORTADAS`, `CARPETA_BACKUPS`.
"""
from __future__ import annotations

import json
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from core.config import resolver_ruta
from core.database import get_db_session
from core.models import Cuenta, Tarea

# Carpetas de datos locales (constantes para poder monkeypatchearlas en tests).
CARPETA_COOKIES = resolver_ruta("data/cookies")
CARPETA_AVATARES = resolver_ruta("data/avatares")
CARPETA_PORTADAS = resolver_ruta("data/portadas")
CARPETA_BACKUPS = resolver_ruta("data/backups")

# Motivo que se agrega al `resultado` de las tareas canceladas.
MOTIVO_TAREA_CANCELADA = "cuentas eliminadas"

_SEPARADORES_RUTA = ("/", "\\")


# --------------------------------------------------------------------------- #
# Parseo y normalizacion de listas
# --------------------------------------------------------------------------- #
def parsear_lista_usuarios(texto: str) -> List[str]:
    """Parsea el texto pegado por el usuario y devuelve la lista de usuarios.

    Acepta una cuenta por linea en cualquiera de estas formas:
        usuario
        @usuario
        usuario:password:totp:...        (linea completa del vendedor: solo
                                          se toma el primer campo antes de ':')

    Ignora lineas vacias y comentarios que empiecen con `#`. Quita espacios y
    el `@` inicial. Deduplica SIN distinguir mayusculas (conserva la primera
    forma escrita). NUNCA lanza: cualquier entrada rara se omite.
    """
    usuarios: List[str] = []
    try:
        if not texto or not isinstance(texto, str):
            return usuarios
        vistos = set()
        for linea in texto.splitlines():
            candidato = linea.strip()
            if not candidato or candidato.startswith("#"):
                continue
            # Linea del vendedor: el primer campo es el usuario.
            candidato = candidato.split(":", 1)[0].strip()
            if candidato.startswith("@"):
                candidato = candidato[1:].strip()
            if not candidato:
                continue
            clave = candidato.lower()
            if clave in vistos:
                continue
            vistos.add(clave)
            usuarios.append(candidato)
    except Exception:
        # Por contrato: jamas lanza. Lo parseado hasta el fallo se conserva.
        return usuarios
    return usuarios


def _normalizar_usuarios(usuarios) -> List[str]:
    """Normaliza una lista ya parseada (strip, `@` inicial, dedupe por caja).

    Acepta cualquier iterable de strings; None o valores no iterables dan [].
    Conserva la primera forma escrita de cada usuario."""
    if usuarios is None:
        return []
    if isinstance(usuarios, str):
        usuarios = [usuarios]
    try:
        iterable = list(usuarios)
    except TypeError:
        return []
    salida: List[str] = []
    vistos = set()
    for item in iterable:
        if item is None:
            continue
        candidato = str(item).strip()
        if candidato.startswith("@"):
            candidato = candidato[1:].strip()
        if not candidato:
            continue
        clave = candidato.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(candidato)
    return salida


def _ids_tarea(tarea) -> List[int]:
    """Extrae los ids de `Tarea.cuentas_ids` (JSON) de forma tolerante.

    Acepta lista JSON, string JSON vacio/invalido, None o una lista ya
    deserializada. Ignora ids que no sean numeros. NUNCA lanza."""
    crudo = getattr(tarea, "cuentas_ids", "") or ""
    if isinstance(crudo, (list, tuple)):
        valores = list(crudo)
    else:
        try:
            valores = json.loads(crudo)
        except (ValueError, TypeError):
            return []
    if not isinstance(valores, (list, tuple)):
        return []
    ids: List[int] = []
    for valor in valores:
        try:
            ids.append(int(valor))
        except (TypeError, ValueError):
            continue
    return ids


# --------------------------------------------------------------------------- #
# Consulta (dry-run)
# --------------------------------------------------------------------------- #
def buscar_cuentas(usuarios, plataforma: str = "twitter") -> Dict[str, Any]:
    """Busca cuentas por `usuario` (case-insensitive) SIN escribir nada.

    Devuelve siempre:
        {
          "encontradas": [
              {"usuario", "activa", "status", "nombre_mostrado", "handle_actual"},
              ...
          ],
          "no_encontradas": [str, ...],
          "error": "",
        }

    El orden de `encontradas`/`no_encontradas` sigue el de la lista pedida.
    Ante cualquier error de BD devuelve `encontradas=[]`,
    `no_encontradas=usuarios` y `error` con la causa. NUNCA lanza.
    """
    resultado: Dict[str, Any] = {"encontradas": [], "no_encontradas": [], "error": ""}
    try:
        pedidos = _normalizar_usuarios(usuarios)
        if not pedidos:
            return resultado
        plataforma = str(plataforma or "twitter")

        with get_db_session() as db:
            filas = (
                db.query(Cuenta)
                .filter(
                    Cuenta.plataforma == plataforma,
                    func.lower(Cuenta.usuario).in_([u.lower() for u in pedidos]),
                )
                .all()
            )

        por_clave: Dict[str, Any] = {}
        for fila in filas:
            clave = str(getattr(fila, "usuario", "") or "").lower()
            if clave and clave not in por_clave:
                por_clave[clave] = fila

        for usuario in pedidos:
            fila = por_clave.get(usuario.lower())
            if fila is None:
                resultado["no_encontradas"].append(usuario)
                continue
            resultado["encontradas"].append(
                {
                    "usuario": str(getattr(fila, "usuario", "") or ""),
                    "activa": bool(getattr(fila, "activa", False)),
                    "status": str(getattr(fila, "status", "") or ""),
                    "nombre_mostrado": str(getattr(fila, "nombre_mostrado", "") or ""),
                    "handle_actual": str(getattr(fila, "handle_actual", "") or ""),
                }
            )
    except Exception as exc:
        resultado["encontradas"] = []
        resultado["no_encontradas"] = _normalizar_usuarios(usuarios)
        resultado["error"] = f"{type(exc).__name__}: {exc}"
    return resultado


# --------------------------------------------------------------------------- #
# Respaldo JSON
# --------------------------------------------------------------------------- #
def _serializar_fila(objeto, columnas) -> Dict[str, Any]:
    """Dict con TODAS las columnas del objeto (fechas en ISO, JSON tal cual)."""
    fila: Dict[str, Any] = {}
    for columna in columnas:
        valor = getattr(objeto, columna.name, None)
        if isinstance(valor, datetime):
            valor = valor.isoformat()
        fila[columna.name] = valor
    return fila


def _ruta_respaldo(ruta_respaldo: str = "") -> Path:
    """Destino del respaldo: `ruta_respaldo` o el nombre con fecha en backups."""
    if ruta_respaldo:
        camino = Path(str(ruta_respaldo))
        if not camino.is_absolute():
            camino = Path(resolver_ruta(str(ruta_respaldo)))
        return camino
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(CARPETA_BACKUPS) / f"eliminacion_cuentas_{marca}.json"


def _escribir_respaldo(cuentas, tareas, ruta_respaldo: str = "") -> str:
    """Escribe el respaldo JSON y devuelve su ruta absoluta.

    Mismo serializado que `cuentas/restaurador.py` (fechas ISO, JSON tal
    cual) para que `restaurar_desde_backup` pueda reponerlo. Si falla (carpeta
    sin permisos, disco lleno, ruta invalida...) lanza la excepcion: el
    llamador (`eliminar_usuarios`) aborta la eliminacion."""
    destino = _ruta_respaldo(ruta_respaldo)
    destino.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fecha": datetime.now().isoformat(),
        "host": platform.node() or "desconocido",
        "keep": [],
        "cuentas": [_serializar_fila(c, list(Cuenta.__table__.columns)) for c in cuentas],
        "tareas": [_serializar_fila(t, list(Tarea.__table__.columns)) for t in tareas],
    }
    with destino.open("w", encoding="utf-8") as manejador:
        json.dump(payload, manejador, ensure_ascii=False, indent=2, default=str)
    return str(destino)


def _tareas_relacionadas(db, ids) -> list:
    """Tareas cuyo `cuentas_ids` referencia alguna de las cuentas (ids)."""
    ids_set = set(ids or ())
    if not ids_set:
        return []
    try:
        tareas = db.query(Tarea).all()
    except Exception:
        return []
    return [t for t in tareas if ids_set.intersection(_ids_tarea(t))]


# --------------------------------------------------------------------------- #
# Tareas y archivos
# --------------------------------------------------------------------------- #
def _cancelar_tareas(db, ids) -> int:
    """Cancela tareas pendientes que dependian SOLO de las cuentas eliminadas.

    Una tarea se cancela si esta `pendiente`, tiene `cuentas_ids` no vacio y
    TODOS esos ids pertenecen al conjunto eliminado. Las tareas que aun
    referencian cuentas vivas quedan intactas. Devuelve cuantas cancelo."""
    ids_set = set(ids or ())
    if not ids_set:
        return 0
    try:
        pendientes = db.query(Tarea).filter(Tarea.estado == "pendiente").all()
    except Exception:
        return 0
    canceladas = 0
    for tarea in pendientes:
        ids_tarea = _ids_tarea(tarea)
        if not ids_tarea or not set(ids_tarea).issubset(ids_set):
            continue
        tarea.estado = "cancelada"
        anterior = str(getattr(tarea, "resultado", "") or "").strip()
        if not anterior:
            tarea.resultado = MOTIVO_TAREA_CANCELADA
        elif MOTIVO_TAREA_CANCELADA not in anterior:
            tarea.resultado = f"{anterior} | {MOTIVO_TAREA_CANCELADA}"
        canceladas += 1
    return canceladas


def _resolver_archivo(ruta) -> Optional[Path]:
    """Resuelve una ruta del respaldo (relativa al proyecto o absoluta)."""
    if not ruta:
        return None
    try:
        texto = str(ruta).strip()
        if not texto:
            return None
        camino = Path(texto)
        if not camino.is_absolute():
            camino = Path(resolver_ruta(texto))
        return camino.resolve()
    except Exception:
        return None


def _dentro_de(ruta: Path, carpeta) -> bool:
    """True si `ruta` (resuelta) esta dentro de `carpeta` (resuelta).

    Compara con `os.path.commonpath` + `normcase` para que funcione en Windows
    (que ignora mayusculas) y rechace escapes con `..`."""
    try:
        objetivo = os.path.normcase(str(Path(ruta).resolve()))
        base = os.path.normcase(str(Path(carpeta).resolve()))
        if objetivo == base:
            return False
        return os.path.commonpath([objetivo, base]) == base
    except (ValueError, OSError):
        return False


def _archivos_de_cuentas(filas, plataforma) -> List[Path]:
    """Candidatos a borrar: pkl de cookies + avatar/portada dentro de data/.

    La cookie es siempre `CARPETA_COOKIES/<plataforma>/<usuario>.pkl`; avatar
    y portada SOLO se agregan si su ruta resuelta cae dentro de
    `CARPETA_AVATARES`/`CARPETA_PORTADAS` (nada fuera de esas carpetas)."""
    candidatos: List[Path] = []
    carpeta_cookies = Path(CARPETA_COOKIES)
    carpeta_avatares = Path(CARPETA_AVATARES)
    carpeta_portadas = Path(CARPETA_PORTADAS)
    for fila in filas:
        usuario = str(getattr(fila, "usuario", "") or "").strip()
        if usuario and not any(sep in usuario for sep in _SEPARADORES_RUTA):
            candidatos.append(carpeta_cookies / str(plataforma or "") / f"{usuario}.pkl")
        ruta_avatar = _resolver_archivo(getattr(fila, "avatar_path", ""))
        if ruta_avatar is not None and _dentro_de(ruta_avatar, carpeta_avatares):
            candidatos.append(ruta_avatar)
        ruta_banner = _resolver_archivo(getattr(fila, "banner_path", ""))
        if ruta_banner is not None and _dentro_de(ruta_banner, carpeta_portadas):
            candidatos.append(ruta_banner)
    return candidatos


def _borrar_archivos(candidatos) -> int:
    """Borra los archivos existentes (tolerante a inexistentes/errores).

    Devuelve cuantos archivos se borraron de verdad, sin contar duplicados.
    NUNCA lanza."""
    borrados = 0
    vistos = set()
    for candidato in candidatos or ():
        try:
            ruta = Path(candidato)
            clave = os.path.normcase(str(ruta))
            if clave in vistos:
                continue
            vistos.add(clave)
            if ruta.is_file():
                ruta.unlink()
                borrados += 1
        except Exception:
            continue
    return borrados


# --------------------------------------------------------------------------- #
# Eliminacion
# --------------------------------------------------------------------------- #
def eliminar_usuarios(
    usuarios,
    plataforma: str = "twitter",
    borrar_cookies: bool = True,
    cancelar_tareas: bool = True,
    respaldar: bool = True,
    ruta_respaldo: str = "",
) -> Dict[str, Any]:
    """Elimina cuentas por `usuario` con respaldo previo. NUNCA lanza.

    Parametros:
        usuarios: lista de usuarios (o `@usuario`/lineas completas; se
              normalizan y deduplican sin distinguir mayusculas).
        plataforma: plataforma de las cuentas (default "twitter").
        borrar_cookies: borra `data/cookies/<plataforma>/<usuario>.pkl` y las
              imagenes de avatar/portada que esten DENTRO de data/.
        cancelar_tareas: cancela las tareas pendientes que dependian SOLO de
              las cuentas eliminadas.
        respaldar: escribe el JSON de respaldo ANTES del DELETE; si falla, no
              se elimina nada.
        ruta_respaldo: ruta alternativa del respaldo (si se omite se usa
              `data/backups/eliminacion_cuentas_YYYYMMDD_HHMMSS.json`).

    Devuelve siempre:
        {
          "eliminadas": [str],        # usuarios realmente borrados (forma de la BD)
          "no_encontradas": [str],    # pedidos que no existen
          "archivos_borrados": int,
          "tareas_canceladas": int,
          "respaldo": str,            # ruta del JSON ("" si respaldar=False)
          "error": "",                # causa si algo fallo (y no se aplico nada)
        }

    Todo el DELETE + cancelacion de tareas va en UNA transaccion
    (`get_db_session`); los archivos se borran despues del commit. Si el
    respaldo o la transaccion fallan, `error` trae la causa y no se elimino
    nada de la BD."""
    resultado: Dict[str, Any] = {
        "eliminadas": [],
        "no_encontradas": [],
        "archivos_borrados": 0,
        "tareas_canceladas": 0,
        "respaldo": "",
        "error": "",
    }
    try:
        pedidos = _normalizar_usuarios(usuarios)
        if not pedidos:
            resultado["error"] = "no se indicaron usuarios"
            return resultado
        plataforma = str(plataforma or "twitter")

        with get_db_session() as db:
            filas = (
                db.query(Cuenta)
                .filter(
                    Cuenta.plataforma == plataforma,
                    func.lower(Cuenta.usuario).in_([u.lower() for u in pedidos]),
                )
                .all()
            )

            por_clave: Dict[str, Any] = {}
            for fila in filas:
                clave = str(getattr(fila, "usuario", "") or "").lower()
                if clave and clave not in por_clave:
                    por_clave[clave] = fila

            encontradas = []
            no_encontradas: List[str] = []
            for usuario in pedidos:
                fila = por_clave.get(usuario.lower())
                if fila is None:
                    no_encontradas.append(usuario)
                else:
                    encontradas.append(fila)

            if not encontradas:
                resultado["no_encontradas"] = no_encontradas
                return resultado

            reales = [str(getattr(fila, "usuario", "") or "") for fila in encontradas]
            ids = {int(getattr(fila, "id", 0) or 0) for fila in encontradas}
            ids.discard(0)

            # 1) Respaldo ANTES del DELETE: si falla, la excepcion sale de la
            #    transaccion (rollback) y nada se elimina.
            respaldo_ruta = ""
            if respaldar:
                respaldo_ruta = _escribir_respaldo(
                    encontradas, _tareas_relacionadas(db, ids), ruta_respaldo
                )

            # 2) DELETE en la misma transaccion (patron de _eliminar_cuentas).
            db.query(Cuenta).filter(
                Cuenta.plataforma == plataforma,
                Cuenta.usuario.in_(reales),
            ).delete(synchronize_session=False)

            # 3) Tareas pendientes que quedaron huerfanas.
            tareas_canceladas = _cancelar_tareas(db, ids) if cancelar_tareas else 0

            # 4) Candidatos a borrar del disco (se borran tras el commit).
            candidatos = _archivos_de_cuentas(encontradas, plataforma)

        resultado["eliminadas"] = reales
        resultado["no_encontradas"] = no_encontradas
        resultado["tareas_canceladas"] = int(tareas_canceladas or 0)
        resultado["respaldo"] = respaldo_ruta
        if borrar_cookies:
            resultado["archivos_borrados"] = _borrar_archivos(candidatos)
    except Exception as exc:
        resultado["eliminadas"] = []
        resultado["archivos_borrados"] = 0
        resultado["tareas_canceladas"] = 0
        resultado["respaldo"] = ""
        resultado["error"] = f"{type(exc).__name__}: {exc}"
    return resultado
