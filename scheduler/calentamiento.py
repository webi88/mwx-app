# -*- coding: utf-8 -*-
"""Calentamiento continuo de la flota (publicaciones organicas aleatorias).

El scheduler 24/7 (``python -m scheduler.standalone``) mantiene la flota "viva"
publicando, cada cierto intervalo ALEATORIO, un texto de mantenimiento en UNA
cuenta aleatoria (respetando registro/perfil/personalidad de esa cuenta). La
misma cuenta no se repite hasta pasar ``CALENTAMIENTO_GAP_HORAS`` horas sin
registro de acciones, y nunca se publica una segunda tarea pendiente para la
misma cuenta.

Se pausa SOLO si hay una campana de activacion en curso: el dashboard crea el
marcador ``data/.campana_activa`` al lanzar la campana y lo borra al terminar;
si el archivo no existe o es viejo (mas de 90 minutos) se considera libre.

Variables de entorno (todas opcionales; valor invalido -> default):
    CALENTAMIENTO_ACTIVO    "1"  (0 = apagado; el job ni siquiera se registra)
    CALENTAMIENTO_MIN_MIN   "15" (minutos minimos entre tandas)
    CALENTAMIENTO_MAX_MIN   "45" (minutos maximos entre tandas)
    CALENTAMIENTO_GAP_HORAS "12" (horas sin repetir la misma cuenta)
    CALENTAMIENTO_POSTS     "1"  (publicaciones por tanda; 1..10)
    CALENTAMIENTO_USAR_IA   "1"  (0 = forzar el fallback local, sin OpenAI)

Nunca lanza: cualquier error se registra y se devuelve un resultado vacio para
que el job de APScheduler siga vivo.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import or_

from core.config import resolver_ruta
from core.database import get_db_session
from core.models import Cuenta, RegistroAccion, Tarea
from core.perfiles import colocar_hashtag_en_medio, tiene_hashtag

#: Marcador que crea el dashboard mientras hay una campana de activacion.
ARCHIVO_CAMPANA_REL = "data/.campana_activa"
#: Un marcador mas viejo que esto se considera abandonado (campana muerta).
CAMPANA_VIGENCIA_SEG = 90 * 60

_DEFECTOS = {
    "activo": True,
    "min_min": 15,
    "max_min": 45,
    "gap_horas": 12,
    "posts": 1,
    "usar_ia": True,
}

#: Proxima tanda (time.monotonic). None = primera pasada: solo programa la hora.
_proxima_tanda = None

#: Ultimo recurso si no hay IA ni plantilla local (nunca deberia llegar aqui).
_PLANTILLAS_PROPIAS = (
    "Buen dia, seguimos por aqui compartiendo lo que pasa en la comunidad.",
    "Seguimos atentos a lo que ocurre en el pais y en la ciudad.",
    "Un gusto saludar, seguimos pendientes de los temas de siempre.",
    "Buenas, aqui seguimos comentando lo que pasa dia a dia.",
)


def _env_texto(nombre: str) -> str:
    try:
        valor = os.environ.get(nombre)
    except Exception:
        return ""
    return str(valor).strip() if valor is not None else ""


def _env_bool(nombre: str, defecto: bool) -> bool:
    """Lee un booleano de entorno; vacio/invalido -> `defecto`."""
    crudo = _env_texto(nombre).lower()
    if not crudo:
        return bool(defecto)
    return crudo in ("1", "true", "yes", "si", "sí", "on", "verdadero", "activo")


def _env_int(nombre: str, defecto: int) -> int:
    """Lee un entero de entorno; vacio/invalido -> `defecto`."""
    crudo = _env_texto(nombre)
    if not crudo:
        return int(defecto)
    try:
        return int(float(crudo))
    except (TypeError, ValueError):
        return int(defecto)


def config_calentamiento() -> dict:
    """Config efectiva del calentamiento (nunca lanza, invalidos -> defaults).

    Devuelve ``{"activo","min_min","max_min","gap_horas","posts","usar_ia"}``.
    Corrige rangos: ``min_min <= max_min``, ``gap_horas >= 0`` y
    ``1 <= posts <= 10``.
    """
    try:
        min_min = _env_int("CALENTAMIENTO_MIN_MIN", _DEFECTOS["min_min"])
        max_min = _env_int("CALENTAMIENTO_MAX_MIN", _DEFECTOS["max_min"])
        if min_min < 1:
            min_min = _DEFECTOS["min_min"]
        if max_min < 1:
            max_min = _DEFECTOS["max_min"]
        if min_min > max_min:
            min_min, max_min = max_min, min_min
        return {
            "activo": _env_bool("CALENTAMIENTO_ACTIVO", _DEFECTOS["activo"]),
            "min_min": min_min,
            "max_min": max_min,
            "gap_horas": max(0, _env_int("CALENTAMIENTO_GAP_HORAS", _DEFECTOS["gap_horas"])),
            "posts": max(1, min(10, _env_int("CALENTAMIENTO_POSTS", _DEFECTOS["posts"]))),
            "usar_ia": _env_bool("CALENTAMIENTO_USAR_IA", _DEFECTOS["usar_ia"]),
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"Calentamiento: config invalida, uso defaults: {e}")
        return dict(_DEFECTOS)


def campana_activa() -> bool:
    """True si hay una campana de activacion en curso (marcador fresco).

    El marcador ``data/.campana_activa`` lo crea el dashboard al lanzar una
    campana y lo borra al terminar. Si el archivo no existe o su mtime tiene
    mas de 90 minutos, se considera libre (campana terminada o proceso caido).
    Nunca lanza: ante cualquier error devuelve False.
    """
    try:
        ruta = Path(resolver_ruta(ARCHIVO_CAMPANA_REL))
        if not ruta.is_file():
            return False
        edad = time.time() - ruta.stat().st_mtime
        if edad < 0:
            edad = 0.0
        return edad < CAMPANA_VIGENCIA_SEG
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Calentamiento: no se pudo leer el marcador de campana: {e}")
        return False


def _usuarios_con_registro_reciente(db, gap_horas) -> set:
    """Usuarios con alguna `RegistroAccion` en las ultimas `gap_horas`.

    Nunca lanza: si la consulta falla devuelve un set vacio (equivale a "nadie
    tiene registro reciente").
    """
    try:
        corte = datetime.utcnow() - timedelta(hours=max(0, int(gap_horas)))
        filas = (
            db.query(RegistroAccion.usuario)
            .filter(RegistroAccion.fecha >= corte)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Calentamiento: sin registro de acciones ({e})")
        return set()

    usuarios = set()
    for fila in filas or []:
        try:
            if isinstance(fila, (tuple, list)):
                usuario = fila[0] if fila else ""
            else:
                usuario = getattr(fila, "usuario", "")
            usuario = str(usuario or "").strip()
            if usuario:
                usuarios.add(usuario)
        except Exception:  # noqa: BLE001
            continue
    return usuarios


def _cookies_json_con_contenido(cookies_json) -> bool:
    """True si `cookies_json` trae cookies reales (no None/""/[]/{}).

    `cookies_json` puede llegar como lista/dict (columna JSON de SQLAlchemy) o
    como cadena JSON cruda. Nunca lanza.
    """
    try:
        if cookies_json is None:
            return False
        if isinstance(cookies_json, str):
            crudo = cookies_json.strip()
            if not crudo or crudo.lower() in ("[]", "{}", "null", "none"):
                return False
            try:
                return bool(json.loads(crudo))
            except (TypeError, ValueError):
                # Cadena no-JSON con contenido: se asume que algo trae.
                return True
        return bool(cookies_json)
    except Exception:  # noqa: BLE001
        return False


def _tiene_sesion(cuenta) -> bool:
    """True si la cuenta tiene una sesion REALMENTE utilizable.

    Criterios (en orden):
      1. `auth_token` no vacio;
      2. `cookies_json` con contenido (distinto de None/""/[]/{});
      3. `cookies_path` no vacio Y el archivo existe de verdad en disco
         (`os.path.isfile(resolver_ruta(cookies_path))`).

    Antes bastaba con que `cookies_path` no estuviera vacio: se elegian
    cuentas con el `.pkl` borrado (p.ej. RedDelAvanza173) y la ventana se
    gastaba en `login_con_password`/TOTP hasta terminar en "Login fallido".
    Nunca lanza: cualquier error -> False.
    """
    try:
        auth_token = str(getattr(cuenta, "auth_token", "") or "").strip()
        if auth_token:
            return True

        if _cookies_json_con_contenido(getattr(cuenta, "cookies_json", None)):
            return True

        cookies_path = str(getattr(cuenta, "cookies_path", "") or "").strip()
        if not cookies_path:
            return False
        return os.path.isfile(resolver_ruta(cookies_path))
    except Exception:  # noqa: BLE001
        return False


def _es_elegible(cuenta, recientes: set, excluidos: set) -> bool:
    """Filtro final en Python (complementa la consulta SQL). Nunca lanza.

    TIER: las cuentas Tier 3 (Métricas/Soporte) quedan FUERA porque el
    calentamiento publica un post (rol hashtags) y el Tier 3 solo puede
    RT/likes. Tier 2 NO se excluye (mantiene el comportamiento actual).

    PAUSA: las cuentas pausadas para activacion masiva SI son elegibles aqui
    (el calentamiento es mantenimiento): la pausa solo excluye activacion.
    """
    try:
        if getattr(cuenta, "id", None) is None:
            return False
        if not getattr(cuenta, "activa", False):
            return False
        if str(getattr(cuenta, "plataforma", "") or "").strip().lower() != "twitter":
            return False
        try:
            from core.tiers import tier_de_cuenta

            if tier_de_cuenta(cuenta) == "tier3":
                return False
        except Exception:  # noqa: BLE001
            # Sin `core.tiers` disponible se conserva el comportamiento previo.
            pass
        if str(getattr(cuenta, "status", "") or "").strip().lower() == "suspended":
            return False
        if not _tiene_sesion(cuenta):
            return False
        usuario = str(getattr(cuenta, "usuario", "") or "").strip()
        if not usuario or usuario in recientes:
            return False
        if getattr(cuenta, "id", None) in excluidos:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def elegir_cuenta(db, gap_horas=None, excluir_ids=None):
    """Devuelve una cuenta al azar apta para calentar (o None).

    Aptas: `activa=True`, `plataforma="twitter"`, con sesion real
    (`auth_token`, `cookies_json` con contenido o `cookies_path` cuyo archivo
    existe), `status != "suspended"` y SIN `RegistroAccion` en las ultimas
    `gap_horas` (default: config). Las cuentas de `excluir_ids` (p.ej. con una
    tarea de post pendiente) se omiten. Nunca lanza.
    """
    try:
        if gap_horas is None:
            gap_horas = config_calentamiento()["gap_horas"]
        recientes = _usuarios_con_registro_reciente(db, gap_horas)
        try:
            excluidos = {int(x) for x in (excluir_ids or [])}
        except Exception:  # noqa: BLE001
            excluidos = set()

        candidatas = [
            c
            for c in (
                db.query(Cuenta)
                .filter(
                    Cuenta.activa.is_(True),
                    Cuenta.plataforma == "twitter",
                    or_(
                        Cuenta.cookies_path.like("_%"),
                        Cuenta.auth_token.like("_%"),
                        Cuenta.cookies_json.isnot(None),
                    ),
                )
                .all()
                or []
            )
            if _es_elegible(c, recientes, excluidos)
        ]
        if not candidatas:
            return None
        return random.choice(candidatas)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Calentamiento: error eligiendo cuenta: {e}")
        return None


def _info_cuenta(cuenta) -> dict:
    """Dict con el perfil/contenido de la cuenta para `generar_textos_mantenimiento`."""
    usuario = str(getattr(cuenta, "usuario", "") or "").strip()
    nombre = str(getattr(cuenta, "nombre_mostrado", "") or "").strip() or usuario
    return {
        "usuario": usuario,
        "registro": str(getattr(cuenta, "tipo_cuenta", "") or "").strip(),
        "personalidad": str(getattr(cuenta, "personalidad", "") or "").strip(),
        "seccion": str(getattr(cuenta, "seccion", "") or "").strip(),
        "nombre": nombre,
        "perfil": str(getattr(cuenta, "perfil_personalidad", "") or "").strip(),
        "tipo_accion": "post",
    }


def _primer_texto(filas) -> str:
    """Primer texto no vacio de la estructura ``[[texto, ...], ...]``; "" si no hay."""
    try:
        for fila in filas or []:
            for texto in fila or []:
                limpio = str(texto or "").strip()
                if limpio:
                    return limpio
    except Exception:  # noqa: BLE001
        pass
    return ""


def _texto_local(info: dict) -> str:
    """Texto por plantillas locales de IA (respeta registro/perfil); "" si falla."""
    try:
        from ia.generador_contenido import _fallback_estructura_mantenimiento

        return _primer_texto(_fallback_estructura_mantenimiento([info], 1))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Calentamiento: fallback local no disponible: {e}")
        return ""


def _texto_propio() -> str:
    """Ultimo recurso: plantilla simple con hashtag garantizado. Nunca lanza."""
    try:
        return colocar_hashtag_en_medio(random.choice(_PLANTILLAS_PROPIAS))
    except Exception:  # noqa: BLE001
        return "Seguimos atentos a lo que pasa en la comunidad. #Comunidad"


def generar_texto(cuenta) -> str:
    """Genera UN texto de mantenimiento para esa cuenta (nunca vacio, nunca lanza).

    Con ``CALENTAMIENTO_USAR_IA=0`` NO se llama a OpenAI: se usa directo la
    plantilla local (que ya respeta registro/perfil/personalidad). Con IA
    activa, si la llamada falla o devuelve vacio se cae al fallback local y,
    como ultimo recurso, a una plantilla propia. Garantiza hashtag presente.
    """
    try:
        cfg = config_calentamiento()
        info = _info_cuenta(cuenta)
        texto = ""

        if cfg["usar_ia"]:
            try:
                from ia.generador_contenido import generar_textos_mantenimiento

                texto = _primer_texto(
                    generar_textos_mantenimiento([info], n_por_cuenta=1)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Calentamiento: la IA fallo, uso fallback local: {e}")

        if not texto:
            texto = _texto_local(info)
        if not texto:
            texto = _texto_propio()

        texto = str(texto or "").strip()
        if not texto:
            texto = _texto_propio()
        if not tiene_hashtag(texto):
            try:
                texto = colocar_hashtag_en_medio(texto)
            except Exception:  # noqa: BLE001
                pass
        if not tiene_hashtag(texto):
            texto = _texto_propio()
        return texto
    except Exception as e:  # noqa: BLE001
        logger.error(f"Calentamiento: error generando texto: {e}")
        return _texto_propio()


def _ids_con_post_pendiente(db) -> set:
    """IDs de cuentas con una `Tarea` de post pendiente (sin duplicar)."""
    try:
        tareas = (
            db.query(Tarea)
            .filter(Tarea.estado == "pendiente", Tarea.tipo == "post")
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Calentamiento: no se pudieron leer tareas pendientes: {e}")
        return set()

    ids = set()
    for tarea in tareas or []:
        try:
            if str(getattr(tarea, "estado", "") or "").strip().lower() != "pendiente":
                continue
            if str(getattr(tarea, "tipo", "") or "").strip().lower() != "post":
                continue
            crudo = getattr(tarea, "cuentas_ids", "") or "[]"
            datos = json.loads(crudo) if isinstance(crudo, str) else crudo
            for valor in datos or []:
                try:
                    ids.add(int(valor))
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001
            continue
    return ids


def programar_publicacion() -> dict | None:
    """Elige cuenta, genera texto y crea UNA `Tarea` de post (o None).

    - Salta cuentas con una `Tarea` de post "pendiente" (no duplica).
    - `fecha_hora` = ahora + 0..5 min aleatorios (la recoge el scheduler).
    - Devuelve ``{"id","usuario"}`` de la tarea creada; None si no hay cuenta
      elegible o si algo falla. Nunca lanza.
    """
    try:
        cfg = config_calentamiento()
        with get_db_session() as db:
            pendientes = _ids_con_post_pendiente(db)
            cuenta = elegir_cuenta(db, cfg["gap_horas"], excluir_ids=pendientes)
            if cuenta is None:
                logger.debug("Calentamiento: no hay cuentas elegibles")
                return None

            usuario = str(getattr(cuenta, "usuario", "") or "").strip()
            texto = generar_texto(cuenta)
            if not texto:
                logger.warning(f"Calentamiento: sin texto para @{usuario}")
                return None

            tarea = Tarea(
                tipo="post",
                plataforma="twitter",
                contenido=texto,
                imagen_path="",
                video_path="",
                cuentas_ids=json.dumps([cuenta.id]),
                fecha_hora=datetime.now() + timedelta(minutes=random.uniform(0, 5)),
                estado="pendiente",
                creada_por=None,
            )
            db.add(tarea)
            db.commit()
            tarea_id = getattr(tarea, "id", None)

        logger.info(
            f"Calentamiento: publicacion programada (tarea {tarea_id}, @{usuario})"
        )
        return {"id": tarea_id, "usuario": usuario}
    except Exception as e:  # noqa: BLE001
        logger.error(f"Calentamiento: error programando publicacion: {e}")
        return None


def _programar_proxima_tanda(cfg: dict) -> None:
    """Fija la proxima tanda a `random.uniform(min_min, max_min)` minutos."""
    global _proxima_tanda
    try:
        minimo = float(cfg.get("min_min", _DEFECTOS["min_min"]))
        maximo = float(cfg.get("max_min", _DEFECTOS["max_min"]))
        _proxima_tanda = time.monotonic() + random.uniform(minimo, maximo) * 60.0
    except Exception:  # noqa: BLE001
        _proxima_tanda = time.monotonic() + 30 * 60.0


def ejecutar_tanda_si_toca() -> dict:
    """Job del scheduler: publica una tanda SOLO si toca y no hay campana.

    Devuelve un resumen ``{"programadas": [{"id","usuario"}, ...], "motivo"}``
    donde `motivo` es "desactivado", "campana_activa", "primera_espera",
    "esperando", "ok" o "sin_cuentas". Nunca lanza.
    """
    global _proxima_tanda
    resultado = {"programadas": [], "motivo": ""}
    try:
        cfg = config_calentamiento()
        if not cfg["activo"]:
            resultado["motivo"] = "desactivado"
            return resultado

        if campana_activa():
            logger.debug(
                "Calentamiento: pausado (hay una campana de activacion en curso)"
            )
            resultado["motivo"] = "campana_activa"
            return resultado

        if _proxima_tanda is None:
            # Primera pasada tras arrancar: no publica; solo agenda la ventana.
            _programar_proxima_tanda(cfg)
            resultado["motivo"] = "primera_espera"
            return resultado

        if time.monotonic() < _proxima_tanda:
            resultado["motivo"] = "esperando"
            return resultado

        try:
            posts = max(1, int(cfg["posts"]))
            for indice in range(posts):
                item = programar_publicacion()
                if item:
                    resultado["programadas"].append(item)
                if indice + 1 < posts:
                    # Pequena pausa entre publicaciones de la misma tanda.
                    time.sleep(random.uniform(3.0, 10.0))
        finally:
            # Pase lo que pase, la proxima tanda es una ventana nueva (no
            # reintenta cada 60s si no habia cuentas elegibles o fallo algo).
            _programar_proxima_tanda(cfg)

        resultado["motivo"] = "ok" if resultado["programadas"] else "sin_cuentas"
        if resultado["programadas"]:
            usuarios = ", ".join(
                f"@{p.get('usuario', '')}" for p in resultado["programadas"]
            )
            logger.info(f"Calentamiento: tanda programada ({usuarios})")
        return resultado
    except Exception as e:  # noqa: BLE001
        logger.error(f"Calentamiento: error en la tanda: {e}")
        return resultado
