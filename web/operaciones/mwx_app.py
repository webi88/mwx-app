# -*- coding: utf-8 -*-
"""📲 MWX APP: administracion de usuarios, cuentas y campana del ejecutable.

Esta operacion del DASHBOARD (Streamlit) es el panel del dueno/admin para el
ejecutable `MWX APP` (login local + campana en la maquina del usuario). NO usa
`data/web_users.json` ni `web/auth.py`: los usuarios del ejecutable viven en las
tablas `app_usuario` / `app_asignacion` / `app_config` / `app_evento` y las
contrasenas se guardan con el PBKDF2 de `core.app_auth`.

Pestanas:
  1. 👥 Usuarios: crear/editar/activar/desactivar, resetear contrasena,
     revocar sesiones y eliminar (con confirmacion).
  2. 🔗 Asignar cuentas: multiselect de la flota de X con filtros + resumen.
  3. ⚙️ Campana: hashtags, contexto, narrativa, URLs, imagenes y topes.
  4. 📊 Actividad / Seguridad: ultimos eventos, URL del API y acciones masivas.
  5. ⬇️ Ejecutable / Actualizaciones: descarga del ZIP publicado y publicacion
     de una actualizacion remota (obligatoria u opcional) en `app_releases`.

Reglas de seguridad:
  - NUNCA se muestran hashes ni contrasenas (la tabla solo expone metadatos).
  - Toda accion destructiva exige una confirmacion explicita (checkbox).
  - Los errores se muestran con `st.error` claro; jamas un traceback/secreto.
  - Solo el rol `admin` del dashboard puede entrar a la pagina.

Las funciones de BD son deliberadamente independientes de Streamlit (reciben y
devuelven datos planos) para poder probarlas con una SQLite temporal
monkeypatcheando el `obtener_sesion` de este modulo.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from io import BytesIO
from pathlib import Path

import streamlit as st
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.app_auth import hash_password, revocar_sesiones
from core.app_imagenes import (
    content_type_de,
    eliminar_imagen as eliminar_imagen_bd,
    guardar_imagen as guardar_imagen_bd,
    leer_imagen as leer_imagen_bd,
    listar_imagenes as listar_imagenes_bd,
)
from core.config import resolver_ruta, settings
from core.database import obtener_sesion
from core.models import AppAsignacion, AppConfig, AppEvento, AppUsuario, Cuenta
from core.secciones import SECCIONES, normalizar_seccion
from core.tiers import tier_de_cuenta
from web.ui import cabecera

# ============================ CONSTANTES ============================

PREFIX = "mwx"

# Usuario del ejecutable: minusculas, 3-20, solo letras/numeros/punto/guion.
REGEX_USERNAME = re.compile(r"^[a-z0-9._-]{3,20}$")
MIN_PASSWORD = 8
MAX_HASHTAGS = 6
MAX_IMAGENES = 20
MAX_EVENTOS = 50
EXT_IMAGENES = (".png", ".jpg", ".jpeg", ".webp")

# Valores por defecto de AppConfig (los mismos del modelo).
CONFIG_DEFECTO = {
    "hashtags": [],
    "contexto": "",
    "narrativa": "",
    "urls": [],
    "texto_base": "",
    "acciones_por_cuenta": 10,
    "pausa_seg": 2,
    "duracion_min": 60,
    "limite_dia": 49,
    "navegadores": 1,
    "imagenes": [],
    "permitir_ajustes": False,
}

TIER_OPCIONES = {
    "Todos": "",
    "Tier 1": "tier1",
    "Tier 2": "tier2",
    "Tier 3": "tier3",
    "Sin tier": "sin",
}

TIER_CORTO = {"tier1": "Tier 1", "tier2": "Tier 2", "tier3": "Tier 3"}

# Ultimo error de BD de una lectura, para que la UI lo muestre claro en vez de
# una lista vacia silenciosa. Se consume con `_avisar_error_bd()`.
_ERROR_BD = ""


# ============================ UTILIDADES ============================


def _texto(valor) -> str:
    """str limpio y tolerante a None (nunca lanza)."""
    return "" if valor is None else str(valor).strip()


def _entero(valor, defecto: int, minimo: int, maximo: int) -> int:
    """int() acotado a [minimo, maximo]; ante basura devuelve `defecto`."""
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        numero = int(defecto)
    return max(int(minimo), min(int(maximo), numero))


def _bool(valor) -> bool:
    """True/False robusto (acepta bool, "1"/"0", "true"/"false", "si"/"no")."""
    if isinstance(valor, bool):
        return valor
    if valor is None:
        return False
    texto = str(valor).strip().lower()
    return texto in ("1", "true", "si", "sí", "yes", "y", "on", "activo")


def _fallo_bd(mensaje: str) -> None:
    global _ERROR_BD
    _ERROR_BD = mensaje


def _avisar_error_bd() -> None:
    """Muestra y limpia el ultimo error de BD (st.error claro, sin traceback)."""
    global _ERROR_BD
    if _ERROR_BD:
        st.error(_ERROR_BD)
        _ERROR_BD = ""


def _lista_json(valor) -> list:
    """Decodifica un campo Text con JSON (lista) o devuelve [] (nunca lanza)."""
    if isinstance(valor, (list, tuple)):
        return [str(x) for x in valor if _texto(x)]
    if isinstance(valor, str) and valor.strip():
        try:
            datos = json.loads(valor)
        except Exception:
            return []
        if isinstance(datos, list):
            return [str(x) for x in datos if _texto(x)]
    return []


def _fmt_fecha(valor) -> str:
    """Fecha legible para tablas ("2026-11-10 14:05"); '—' si no hay."""
    if not valor:
        return "—"
    try:
        if isinstance(valor, str):
            return valor.replace("T", " ")[:16]
        return valor.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _normalizar_username(valor) -> str:
    """Usuario en minusculas y sin espacios (la BD guarda la forma canonica)."""
    return _texto(valor).lower()


def validar_username(valor) -> str:
    """Devuelve "" si el usuario es valido, o el mensaje de error."""
    nombre = _normalizar_username(valor)
    if not nombre:
        return "El usuario es obligatorio."
    if len(nombre) < 3 or len(nombre) > 20:
        return "El usuario debe tener entre 3 y 20 caracteres."
    if not REGEX_USERNAME.match(nombre):
        return (
            "Usuario invalido: usa solo minusculas, numeros, punto, guion y "
            "guion bajo (sin espacios ni acentos)."
        )
    return ""


def normalizar_hashtags(valor) -> list:
    """Normaliza hashtags desde texto o lista: antepone #, quita duplicados y
    devuelve como maximo 6. Nunca lanza."""
    if isinstance(valor, (list, tuple)):
        piezas = [str(x) for x in valor]
    else:
        piezas = re.split(r"[\s,;]+", _texto(valor))

    resultado: list = []
    vistos = set()
    for pieza in piezas:
        limpio = re.sub(r"[^\w]", "", str(pieza), flags=re.UNICODE)
        if not limpio:
            continue
        etiqueta = "#" + limpio
        clave = etiqueta.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(etiqueta)
        if len(resultado) >= MAX_HASHTAGS:
            break
    return resultado


def normalizar_urls(valor) -> list:
    """URLs ancla: una por linea/coma, solo http(s), sin duplicados."""
    if isinstance(valor, (list, tuple)):
        piezas = [str(x) for x in valor]
    else:
        piezas = re.split(r"[\r\n,]+", _texto(valor))
    resultado: list = []
    vistos = set()
    for pieza in piezas:
        url = _texto(pieza)
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in vistos:
            continue
        vistos.add(url)
        resultado.append(url)
    return resultado


def _cuenta_tiene_sesion(cuenta) -> bool:
    """True si la cuenta tiene `auth_token` o cookies (lista/dict/JSON)."""
    try:
        if _texto(getattr(cuenta, "auth_token", "")):
            return True
        cookies = getattr(cuenta, "cookies_json", None)
        if isinstance(cookies, str):
            texto = cookies.strip()
            if not texto or texto in ("[]", "{}", "null", "None"):
                return False
            try:
                cookies = json.loads(texto)
            except Exception:
                return bool(texto)
        if isinstance(cookies, dict):
            return bool(cookies)
        if isinstance(cookies, (list, tuple, set)):
            return len(cookies) > 0
        return bool(cookies)
    except Exception:
        return False


def _etiqueta_cuenta(fila: dict) -> str:
    """`@usuario · nombre · Sección · Tier` (etiqueta del multiselect)."""
    usuario = _texto(fila.get("usuario")) or "?"
    nombre = _texto(fila.get("nombre")) or _texto(fila.get("handle")) or "—"
    seccion = _texto(fila.get("seccion")) or "Sin sección"
    tier = TIER_CORTO.get(fila.get("tier"), "Sin tier")
    return f"@{usuario} · {nombre} · {seccion} · {tier}"


# ============================ USUARIOS ============================


def crear_usuario_app(
    username, password, nombre: str = "", activo: bool = True
) -> dict:
    """Crea un usuario del MWX APP con PBKDF2 y valida duplicado liso.

    Devuelve `{"ok", "error", "usuario_id"}`. El username se guarda en
    minusculas; el duplicado se comprueba case-insensitive."""
    nombre_usuario = _normalizar_username(username)
    error = validar_username(nombre_usuario)
    if error:
        return {"ok": False, "error": error, "usuario_id": None}
    if password is None or len(str(password)) < MIN_PASSWORD:
        return {
            "ok": False,
            "error": f"La contrasena debe tener al menos {MIN_PASSWORD} caracteres.",
            "usuario_id": None,
        }

    try:
        with obtener_sesion() as db:
            existe = (
                db.query(AppUsuario.id)
                .filter(func.lower(AppUsuario.username) == nombre_usuario)
                .first()
            )
            if existe is not None:
                return {
                    "ok": False,
                    "error": "Ese usuario ya existe (la comparacion ignora mayusculas).",
                    "usuario_id": None,
                }
            usuario = AppUsuario(
                username=nombre_usuario,
                password_hash=hash_password(str(password)),
                nombre=_texto(nombre)[:150],
                activo=bool(activo),
            )
            db.add(usuario)
            db.flush()
            usuario_id = int(usuario.id)
    except IntegrityError:
        return {
            "ok": False,
            "error": "Ese usuario ya existe (la comparacion ignora mayusculas).",
            "usuario_id": None,
        }
    except SQLAlchemyError:
        _fallo_bd("No se pudo crear el usuario (error de base de datos).")
        return {
            "ok": False,
            "error": "No se pudo crear el usuario (error de base de datos).",
            "usuario_id": None,
        }

    registrar_evento_app(usuario_id, "usuario_creado", f"@{nombre_usuario}")
    return {"ok": True, "error": "", "usuario_id": usuario_id}


def listar_usuarios_app() -> list:
    """Usuarios del APP con el conteo de cuentas asignadas.

    NUNCA incluye `password_hash` ni ningun secreto."""
    try:
        with obtener_sesion() as db:
            filas = (
                db.query(AppUsuario, func.count(AppAsignacion.id))
                .outerjoin(
                    AppAsignacion, AppAsignacion.usuario_id == AppUsuario.id
                )
                .group_by(AppUsuario.id)
                .order_by(AppUsuario.username)
                .all()
            )
    except SQLAlchemyError:
        _fallo_bd("No se pudieron leer los usuarios (error de base de datos).")
        return []

    return [
        {
            "id": int(usuario.id),
            "username": _texto(usuario.username),
            "nombre": _texto(usuario.nombre),
            "activo": bool(usuario.activo),
            "cuentas": int(cuentas or 0),
            "ultimo_acceso": usuario.ultimo_acceso,
            "creado": usuario.creado,
        }
        for usuario, cuentas in filas
    ]


def _obtener_usuario(db, usuario_id):
    """Devuelve el AppUsuario o None (tolerante a id basura)."""
    try:
        return db.get(AppUsuario, int(usuario_id))
    except (TypeError, ValueError):
        return None


def cambiar_estado_usuario(usuario_id, activo: bool) -> dict:
    """Activa/desactiva un usuario. Al DESACTIVAR tambien revoca sus sesiones."""
    try:
        with obtener_sesion() as db:
            usuario = _obtener_usuario(db, usuario_id)
            if usuario is None:
                return {"ok": False, "error": "El usuario no existe."}
            usuario.activo = bool(activo)
            if not activo:
                usuario.token_version = int(usuario.token_version or 0) + 1
    except SQLAlchemyError:
        return {"ok": False, "error": "No se pudo cambiar el estado del usuario."}

    registrar_evento_app(
        usuario_id,
        "usuario_activado" if activo else "usuario_desactivado",
        "Estado cambiado desde el dashboard",
    )
    return {"ok": True, "error": ""}


def resetear_password_usuario(usuario_id, password) -> dict:
    """Cambia el hash PBKDF2 del usuario (nunca guarda la contrasena)."""
    if password is None or len(str(password)) < MIN_PASSWORD:
        return {
            "ok": False,
            "error": f"La contrasena debe tener al menos {MIN_PASSWORD} caracteres.",
        }
    try:
        with obtener_sesion() as db:
            usuario = _obtener_usuario(db, usuario_id)
            if usuario is None:
                return {"ok": False, "error": "El usuario no existe."}
            usuario.password_hash = hash_password(str(password))
    except SQLAlchemyError:
        return {"ok": False, "error": "No se pudo cambiar la contrasena."}

    registrar_evento_app(
        usuario_id, "password_reset", "Contrasena restablecida por el admin"
    )
    return {"ok": True, "error": ""}


def revocar_sesiones_usuario(usuario_id) -> dict:
    """Invalida los tokens del usuario incrementando `token_version`."""
    try:
        with obtener_sesion() as db:
            nueva = revocar_sesiones(db, int(usuario_id))
    except (SQLAlchemyError, TypeError, ValueError):
        return {"ok": False, "error": "No se pudieron revocar las sesiones.", "version": None}
    if nueva is None:
        return {"ok": False, "error": "El usuario no existe.", "version": None}

    registrar_evento_app(
        usuario_id, "sesiones_revocadas", f"token_version={nueva}"
    )
    return {"ok": True, "error": "", "version": int(nueva)}


def revocar_todas_las_sesiones() -> dict:
    """Incrementa `token_version` de TODOS los usuarios del APP."""
    try:
        with obtener_sesion() as db:
            total = (
                db.query(AppUsuario)
                .update(
                    {
                        AppUsuario.token_version: AppUsuario.token_version + 1
                    },
                    synchronize_session=False,
                )
            )
    except SQLAlchemyError:
        return {
            "ok": False,
            "error": "No se pudieron revocar las sesiones (error de base de datos).",
            "total": 0,
        }

    registrar_evento_app(
        None, "sesiones_todas_revocadas", f"{int(total or 0)} usuarios"
    )
    return {"ok": True, "error": "", "total": int(total or 0)}


def eliminar_usuario_app(usuario_id) -> dict:
    """Elimina el usuario y sus asignaciones/config/eventos (una transaccion).

    Devuelve los conteos eliminados. No toca ninguna cuenta de la flota."""
    resultado = {
        "ok": False,
        "error": "",
        "asignaciones": 0,
        "config": 0,
        "eventos": 0,
        "username": "",
    }
    try:
        with obtener_sesion() as db:
            usuario = _obtener_usuario(db, usuario_id)
            if usuario is None:
                resultado["error"] = "El usuario no existe."
                return resultado
            uid = int(usuario.id)
            resultado["username"] = _texto(usuario.username)
            resultado["asignaciones"] = (
                db.query(AppAsignacion)
                .filter(AppAsignacion.usuario_id == uid)
                .delete(synchronize_session=False)
            )
            resultado["config"] = (
                db.query(AppConfig)
                .filter(AppConfig.usuario_id == uid)
                .delete(synchronize_session=False)
            )
            resultado["eventos"] = (
                db.query(AppEvento)
                .filter(AppEvento.usuario_id == uid)
                .delete(synchronize_session=False)
            )
            db.delete(usuario)
    except SQLAlchemyError:
        resultado["error"] = "No se pudo eliminar el usuario (error de base de datos)."
        return resultado

    resultado["ok"] = True
    registrar_evento_app(
        None, "usuario_eliminado", f"@{resultado['username']} y sus dependencias"
    )
    return resultado


# ============================ CUENTAS / ASIGNACIONES ============================


@st.cache_data(ttl=12, show_spinner=False)
def listar_cuentas_app(
    busqueda: str = "",
    seccion: str = "",
    tier: str = "",
    solo_con_sesion: bool = False,
    mostrar_pausadas: bool = False,
    incluir_ids: tuple = (),
) -> list:
    """Flota de X disponible para asignar, como dicts planos (cache 12s).

    `incluir_ids` fuerza a incluir esas cuentas aunque no pasen los filtros:
    asi una asignacion existente nunca desaparece del multiselect al filtrar.
    """
    try:
        with obtener_sesion() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter")
                .order_by(Cuenta.usuario)
                .all()
            )
    except SQLAlchemyError:
        _fallo_bd("No se pudieron leer las cuentas de X (error de base de datos).")
        return []

    try:
        forzadas = {int(x) for x in (incluir_ids or ()) if x is not None}
    except (TypeError, ValueError):
        forzadas = set()

    texto = _texto(busqueda).lower()
    seccion = normalizar_seccion(seccion) if _texto(seccion) else ""
    tier = _texto(tier).lower()
    filas = []

    for cuenta in cuentas:
        cuenta_id = int(cuenta.id)
        forzada = cuenta_id in forzadas
        sec = normalizar_seccion(getattr(cuenta, "seccion", ""))
        tier_cuenta = tier_de_cuenta(cuenta)
        pausada = bool(getattr(cuenta, "pausada_activacion", False))

        if not forzada:
            if not mostrar_pausadas and pausada:
                continue
            if seccion and sec != seccion:
                continue
            if tier == "sin":
                if tier_cuenta:
                    continue
            elif tier and tier_cuenta != tier:
                continue
            if solo_con_sesion and not _cuenta_tiene_sesion(cuenta):
                continue
            if texto:
                pajar = " ".join(
                    [
                        _texto(getattr(cuenta, "usuario", "")),
                        _texto(getattr(cuenta, "handle_actual", "")),
                        _texto(getattr(cuenta, "nombre_mostrado", "")),
                    ]
                ).lower()
                if texto not in pajar:
                    continue

        fila = {
            "id": cuenta_id,
            "usuario": _texto(getattr(cuenta, "usuario", "")),
            "handle": _texto(getattr(cuenta, "handle_actual", "")),
            "nombre": _texto(getattr(cuenta, "nombre_mostrado", "")),
            "seccion": sec,
            "tier": tier_cuenta,
            "pausada": pausada,
            "activa": bool(getattr(cuenta, "activa", True)),
            "con_sesion": _cuenta_tiene_sesion(cuenta),
        }
        fila["label"] = _etiqueta_cuenta(fila)
        filas.append(fila)

    filas.sort(key=lambda f: f["label"].lower())
    return filas


def limpiar_cache_app() -> None:
    """Limpia SOLO la cache de cuentas de esta pagina (tras guardar)."""
    try:
        listar_cuentas_app.clear()
    except Exception:
        pass


def listar_asignaciones(usuario_id) -> list:
    """IDs de las cuentas asignadas al usuario (ordenados)."""
    try:
        with obtener_sesion() as db:
            filas = (
                db.query(AppAsignacion.cuenta_id)
                .filter(AppAsignacion.usuario_id == int(usuario_id))
                .order_by(AppAsignacion.cuenta_id)
                .all()
            )
    except (SQLAlchemyError, TypeError, ValueError):
        return []
    return [int(fila[0]) for fila in filas]


def guardar_asignaciones(usuario_id, cuenta_ids) -> dict:
    """REEMPLAZA las asignaciones del usuario por `cuenta_ids` (delete+insert).

    Una sola transaccion, unique(usuario, cuenta) respetado y solo IDs que
    existen en la flota. Devuelve `{"ok", "error", "total"}`."""
    try:
        uid = int(usuario_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Usuario invalido.", "total": 0}

    ids: list = []
    for valor in cuenta_ids or []:
        try:
            numero = int(valor)
        except (TypeError, ValueError):
            continue
        if numero not in ids:
            ids.append(numero)

    try:
        with obtener_sesion() as db:
            usuario = db.get(AppUsuario, uid)
            if usuario is None:
                return {"ok": False, "error": "El usuario no existe.", "total": 0}

            validos = []
            if ids:
                validos = [
                    int(fila[0])
                    for fila in db.query(Cuenta.id).filter(Cuenta.id.in_(ids)).all()
                ]

            db.query(AppAsignacion).filter(
                AppAsignacion.usuario_id == uid
            ).delete(synchronize_session=False)

            for cuenta_id in validos:
                db.add(AppAsignacion(usuario_id=uid, cuenta_id=cuenta_id))
    except IntegrityError:
        return {
            "ok": False,
            "error": "No se pudieron guardar las asignaciones (conflicto de datos).",
            "total": 0,
        }
    except SQLAlchemyError:
        return {
            "ok": False,
            "error": "No se pudieron guardar las asignaciones (error de base de datos).",
            "total": 0,
        }

    registrar_evento_app(
        uid, "asignacion_guardada", f"{len(validos)} cuenta(s) asignada(s)"
    )
    limpiar_cache_app()
    return {"ok": True, "error": "", "total": len(validos)}


def asignar_todas_seccion(usuario_id, seccion) -> dict:
    """AGREGA todas las cuentas ACTIVAS de una seccion a las asignaciones.

    No elimina las asignaciones previas. Devuelve
    `{"ok", "error", "agregadas", "total", "seccion"}`."""
    codigo = normalizar_seccion(seccion)
    if not codigo:
        return {
            "ok": False,
            "error": "Seccion invalida (usa CI, IP, LIB o JUS).",
            "agregadas": 0,
            "total": 0,
            "seccion": "",
        }
    try:
        uid = int(usuario_id)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "Usuario invalido.",
            "agregadas": 0,
            "total": 0,
            "seccion": codigo,
        }

    try:
        with obtener_sesion() as db:
            usuario = db.get(AppUsuario, uid)
            if usuario is None:
                return {
                    "ok": False,
                    "error": "El usuario no existe.",
                    "agregadas": 0,
                    "total": 0,
                    "seccion": codigo,
                }

            existentes = {
                int(fila[0])
                for fila in db.query(AppAsignacion.cuenta_id)
                .filter(AppAsignacion.usuario_id == uid)
                .all()
            }
            candidatas = [
                int(cuenta.id)
                for cuenta in db.query(Cuenta)
                .filter(
                    Cuenta.plataforma == "twitter",
                    Cuenta.activa.is_(True),
                )
                .order_by(Cuenta.usuario)
                .all()
                if normalizar_seccion(getattr(cuenta, "seccion", "")) == codigo
            ]
            nuevas = [cid for cid in candidatas if cid not in existentes]
            for cuenta_id in nuevas:
                db.add(AppAsignacion(usuario_id=uid, cuenta_id=cuenta_id))
    except SQLAlchemyError:
        return {
            "ok": False,
            "error": "No se pudieron asignar las cuentas (error de base de datos).",
            "agregadas": 0,
            "total": 0,
            "seccion": codigo,
        }

    total = len(existentes) + len(nuevas)
    registrar_evento_app(
        uid,
        "asignacion_seccion",
        f"Seccion {codigo}: {len(nuevas)} agregada(s)",
    )
    limpiar_cache_app()
    return {
        "ok": True,
        "error": "",
        "agregadas": len(nuevas),
        "total": total,
        "seccion": codigo,
    }


# ============================ CONFIGURACION ============================


def obtener_config_app(usuario_id) -> dict:
    """Config de campana del usuario (dict con listas ya decodificadas)."""
    config = dict(CONFIG_DEFECTO)
    try:
        with obtener_sesion() as db:
            fila = (
                db.query(AppConfig)
                .filter(AppConfig.usuario_id == int(usuario_id))
                .first()
            )
    except (SQLAlchemyError, TypeError, ValueError):
        _fallo_bd("No se pudo leer la configuracion (error de base de datos).")
        return config

    if fila is None:
        return config

    config.update(
        {
            "hashtags": _lista_json(fila.hashtags),
            "contexto": _texto(fila.contexto),
            "narrativa": _texto(fila.narrativa),
            "urls": _lista_json(fila.urls),
            "texto_base": _texto(fila.texto_base),
            "acciones_por_cuenta": int(fila.acciones_por_cuenta or 10),
            "pausa_seg": int(fila.pausa_seg or 2),
            "duracion_min": int(fila.duracion_min or 60),
            "limite_dia": int(fila.limite_dia or 49),
            "navegadores": _entero(
                getattr(fila, "navegadores", 1), 1, 1, 3
            ),
            "imagenes": [
                nombre
                for nombre in _lista_json(fila.imagenes)
                if _nombre_imagen_permitido(nombre)
            ],
            "permitir_ajustes": bool(fila.permitir_ajustes),
        }
    )
    return config


def guardar_config_app(usuario_id, datos: dict) -> dict:
    """Upsert de AppConfig. Normaliza listas y acota los numeros."""
    datos = datos or {}
    try:
        uid = int(usuario_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Usuario invalido.", "config": {}}

    hashtags = normalizar_hashtags(datos.get("hashtags"))
    urls = normalizar_urls(datos.get("urls"))

    imagenes: list = []
    for nombre in datos.get("imagenes") or []:
        limpio = _texto(nombre)
        if _nombre_imagen_permitido(limpio) and limpio not in imagenes:
            imagenes.append(limpio)
    imagenes = imagenes[:MAX_IMAGENES]

    valores = {
        "hashtags": hashtags,
        "contexto": _texto(datos.get("contexto")),
        "narrativa": _texto(datos.get("narrativa")),
        "urls": urls,
        "texto_base": _texto(datos.get("texto_base")),
        "acciones_por_cuenta": _entero(
            datos.get("acciones_por_cuenta"), 10, 1, 49
        ),
        "pausa_seg": _entero(datos.get("pausa_seg"), 2, 1, 120),
        "duracion_min": _entero(datos.get("duracion_min"), 60, 10, 240),
        "limite_dia": _entero(datos.get("limite_dia"), 49, 1, 49),
        "navegadores": _entero(datos.get("navegadores"), 1, 1, 3),
        "imagenes": imagenes,
        "permitir_ajustes": bool(datos.get("permitir_ajustes", False)),
    }

    try:
        with obtener_sesion() as db:
            if db.get(AppUsuario, uid) is None:
                return {"ok": False, "error": "El usuario no existe.", "config": {}}
            fila = (
                db.query(AppConfig).filter(AppConfig.usuario_id == uid).first()
            )
            if fila is None:
                fila = AppConfig(usuario_id=uid)
                db.add(fila)
            fila.hashtags = json.dumps(valores["hashtags"], ensure_ascii=False)
            fila.contexto = valores["contexto"]
            fila.narrativa = valores["narrativa"]
            fila.urls = json.dumps(valores["urls"], ensure_ascii=False)
            fila.texto_base = valores["texto_base"]
            fila.acciones_por_cuenta = valores["acciones_por_cuenta"]
            fila.pausa_seg = valores["pausa_seg"]
            fila.duracion_min = valores["duracion_min"]
            fila.limite_dia = valores["limite_dia"]
            fila.navegadores = valores["navegadores"]
            fila.imagenes = json.dumps(valores["imagenes"], ensure_ascii=False)
            fila.permitir_ajustes = valores["permitir_ajustes"]
    except SQLAlchemyError:
        return {
            "ok": False,
            "error": "No se pudo guardar la configuracion (error de base de datos).",
            "config": {},
        }

    registrar_evento_app(
        uid,
        "config_guardada",
        f"{len(hashtags)} hashtag(s), {len(urls)} URL(s), "
        f"{len(imagenes)} imagen(es)",
    )
    return {"ok": True, "error": "", "config": valores}


# ============================ IMAGENES ============================


def _nombre_imagen_permitido(nombre) -> bool:
    """True si `nombre` es un nombre simple de imagen (sin rutas)."""
    base = _texto(nombre)
    if not base or base in (".", ".."):
        return False
    if base != os.path.basename(base) or "/" in base or "\\" in base:
        return False
    raiz, extension = os.path.splitext(base)
    return bool(raiz) and extension.lower() in EXT_IMAGENES


def _nombre_imagen_seguro(nombre) -> str:
    """Sanea el nombre subido (sin acentos, sin rutas) y fuerza extension."""
    base = os.path.basename(_texto(nombre) or "imagen.png")
    base = unicodedata.normalize("NFKD", base)
    base = "".join(c for c in base if not unicodedata.combining(c))
    raiz, extension = os.path.splitext(base)
    extension = extension.lower()
    if extension not in EXT_IMAGENES:
        extension = ".png"
    raiz = re.sub(r"[^A-Za-z0-9._-]+", "_", raiz).strip("._-")
    if not raiz:
        raiz = f"imagen_{int(time.time())}"
    return f"{raiz[:60]}{extension}"


def _ruta_imagenes_app(username: str) -> str:
    """Carpeta de imagenes del usuario: data/mwx_app/<usuario>/imagenes."""
    usuario = _normalizar_username(username)
    # Defensa extra contra valores legacy con rutas: solo [a-z0-9._-].
    usuario = re.sub(r"[^a-z0-9._-]", "_", usuario).strip("._-") or "usuario"
    return resolver_ruta(f"data/mwx_app/{usuario}/imagenes")


def ruta_imagen_app(username: str, nombre: str) -> str:
    """Ruta absoluta de una imagen guardada (solo nombre simple permitido)."""
    if not _nombre_imagen_permitido(nombre):
        return ""
    return os.path.join(_ruta_imagenes_app(username), os.path.basename(nombre))


def guardar_imagenes_mwx(
    username: str, archivos, existentes=None, usuario_id=None
) -> tuple:
    """Guarda las imagenes subidas y devuelve `(nombres, errores)`.

    - Conserva la lista `existentes` (si no se sube nada, no borra nada).
    - Nombres seguros/unicos; las imagenes se guardan en la BD (`app_imagen`)
      y, como respaldo para la vista previa/uso local, en
      `data/mwx_app/<usuario>/imagenes/`.
    - Cuando se pasa `usuario_id` (panel del dashboard) la BD es OBLIGATORIA:
      la API corre en otro contenedor y necesita leer la imagen de la BD. Una
      imagen que no se pueda persistir en el servidor NO se agrega a la
      configuracion.
    """
    nombres = [
        os.path.basename(str(n))
        for n in (existentes or [])
        if _nombre_imagen_permitido(n)
    ]
    # Sin duplicados conservando el orden.
    nombres = list(dict.fromkeys(nombres))
    errores: list = []
    if not archivos:
        return nombres[:MAX_IMAGENES], errores

    directorio = _ruta_imagenes_app(username)
    try:
        os.makedirs(directorio, exist_ok=True)
    except OSError:
        if usuario_id is None:
            return nombres[:MAX_IMAGENES], ["No se pudo crear la carpeta de imagenes."]

    nombres_db: set = set()
    if usuario_id is not None:
        try:
            with obtener_sesion() as db:
                nombres_db = {
                    str(item.get("nombre"))
                    for item in listar_imagenes_bd(db, int(usuario_id))
                    if item.get("nombre")
                }
        except Exception:
            nombres_db = set()

    for archivo in archivos:
        if len(nombres) >= MAX_IMAGENES:
            errores.append(f"Maximo {MAX_IMAGENES} imagenes; las demas se ignoraron.")
            break
        try:
            contenido = (
                archivo.getbuffer()
                if hasattr(archivo, "getbuffer")
                else archivo
            )
            datos = bytes(contenido) if not isinstance(contenido, bytes) else contenido
            nombre = _nombre_imagen_seguro(getattr(archivo, "name", "imagen.png"))
            raiz, extension = os.path.splitext(nombre)
            candidato = nombre
            contador = 1
            while (
                os.path.exists(os.path.join(directorio, candidato))
                or candidato in nombres
                or candidato in nombres_db
            ):
                candidato = f"{raiz}_{contador}{extension}"
                contador += 1

            # Copia local para vista previa (best-effort).
            try:
                with open(os.path.join(directorio, candidato), "wb") as manejador:
                    manejador.write(datos)
            except OSError:
                if usuario_id is None:
                    raise

            # Fuente de verdad en produccion: la base de datos.
            if usuario_id is not None:
                try:
                    with obtener_sesion() as db:
                        fila = guardar_imagen_bd(
                            db,
                            int(usuario_id),
                            candidato,
                            datos,
                            content_type_de(candidato),
                        )
                        if fila is None:
                            raise ValueError("imagen rechazada")
                        db.commit()
                    nombres_db.add(candidato)
                except Exception:
                    errores.append(
                        f"No se pudo guardar "
                        f"'{_texto(getattr(archivo, 'name', 'imagen'))}' "
                        "en el servidor (base de datos)."
                    )
                    continue

            nombres.append(candidato)
        except Exception:
            # Nunca se muestran rutas internas ni el error crudo.
            errores.append(
                f"No se pudo guardar '{_texto(getattr(archivo, 'name', 'imagen'))}'."
            )
    return nombres[:MAX_IMAGENES], errores


def eliminar_imagen_mwx(usuario_id, username: str, nombre: str) -> dict:
    """Quita la imagen de la config, de la BD y del disco (si existe)."""
    if not _nombre_imagen_permitido(nombre):
        return {"ok": False, "error": "Nombre de imagen invalido."}

    try:
        with obtener_sesion() as db:
            eliminar_imagen_bd(db, int(usuario_id), nombre)
            db.commit()
    except Exception:
        return {
            "ok": False,
            "error": (
                "No se pudo eliminar la imagen del servidor "
                "(base de datos)."
            ),
        }

    config = obtener_config_app(usuario_id)
    if not config:
        return {"ok": False, "error": "No se pudo leer la configuracion."}
    nombres = [n for n in config.get("imagenes", []) if n != nombre]
    resultado = guardar_config_app(
        usuario_id,
        {
            **config,
            "imagenes": nombres,
        },
    )
    if not resultado.get("ok"):
        return resultado

    ruta = ruta_imagen_app(username, nombre)
    if ruta and os.path.isfile(ruta):
        try:
            os.remove(ruta)
        except OSError:
            pass
    return {"ok": True, "error": ""}


# ============================ EVENTOS ============================


def registrar_evento_app(usuario_id, tipo: str, detalle: str = "") -> bool:
    """Guarda un evento de auditoria (nunca lanza, nunca guarda secretos)."""
    try:
        with obtener_sesion() as db:
            db.add(
                AppEvento(
                    usuario_id=(
                        int(usuario_id) if usuario_id is not None else None
                    ),
                    tipo=_texto(tipo)[:50],
                    detalle=_texto(detalle)[:2000],
                )
            )
    except (SQLAlchemyError, TypeError, ValueError):
        return False
    return True


def listar_eventos_app(usuario_id=None, tipo: str = "", limite: int = MAX_EVENTOS) -> list:
    """Ultimos eventos del APP (join con el username)."""
    limite = _entero(limite, MAX_EVENTOS, 1, 500)
    try:
        with obtener_sesion() as db:
            consulta = db.query(AppEvento, AppUsuario.username).outerjoin(
                AppUsuario, AppEvento.usuario_id == AppUsuario.id
            )
            if usuario_id not in (None, "", 0, "0"):
                consulta = consulta.filter(
                    AppEvento.usuario_id == int(usuario_id)
                )
            if _texto(tipo):
                consulta = consulta.filter(AppEvento.tipo == _texto(tipo))
            filas = (
                consulta.order_by(
                    AppEvento.fecha.desc(), AppEvento.id.desc()
                )
                .limit(limite)
                .all()
            )
    except (SQLAlchemyError, TypeError, ValueError):
        _fallo_bd("No se pudieron leer los eventos (error de base de datos).")
        return []

    return [
        {
            "fecha": _fmt_fecha(evento.fecha),
            "usuario": _texto(username) or "—",
            "tipo": _texto(evento.tipo) or "—",
            "detalle": _texto(evento.detalle),
        }
        for evento, username in filas
    ]


def listar_tipos_evento() -> list:
    """Tipos de evento distintos (para el filtro); [] si no hay/falla."""
    try:
        with obtener_sesion() as db:
            filas = (
                db.query(AppEvento.tipo)
                .filter(AppEvento.tipo.isnot(None))
                .distinct()
                .order_by(AppEvento.tipo)
                .all()
            )
    except SQLAlchemyError:
        return []
    return [tipo for (tipo,) in filas if _texto(tipo)]


# ============================ UI: HELPERS ============================


def _es_admin(usuario) -> bool:
    """True solo para el rol admin del dashboard."""
    return _texto((usuario or {}).get("rol")).lower() == "admin"


def _flash(clave: str, mensaje: str, nivel: str = "success") -> None:
    """Deja un mensaje para el siguiente rerun (sobrevive a `st.rerun`)."""
    st.session_state[clave] = {"mensaje": mensaje, "nivel": nivel}


def _mostrar_flash(clave: str) -> None:
    """Muestra (y consume) el mensaje pendiente de `clave`."""
    datos = st.session_state.pop(clave, None)
    if not datos:
        return
    mensaje = datos.get("mensaje") if isinstance(datos, dict) else str(datos)
    nivel = datos.get("nivel", "success") if isinstance(datos, dict) else "success"
    if not mensaje:
        return
    if nivel == "error":
        st.error(mensaje)
    elif nivel == "warning":
        st.warning(mensaje)
    elif nivel == "info":
        st.info(mensaje)
    else:
        st.success(mensaje)


def _programar_limpieza(*claves: str) -> None:
    """Marca claves de widgets para borrar en el SIGUIENTE rerun.

    No se tocan aqui (podrian estar ya instanciadas en este run): se guarda
    una clave NO-widget y `_aplicar_limpieza()` la consume antes de que los
    widgets se vuelvan a crear."""
    st.session_state["mwx_limpiar_widgets"] = [c for c in claves if c]


def _aplicar_limpieza() -> None:
    """Borra (antes de instanciar widgets) las claves pendientes de limpieza."""
    pendientes = st.session_state.pop("mwx_limpiar_widgets", None) or []
    for clave in pendientes:
        try:
            st.session_state.pop(clave, None)
        except Exception:
            pass


def _selector_usuario(usuarios: list, key: str, etiqueta: str):
    """Selectbox de usuarios del APP (id como valor, texto amigable)."""
    opciones = [int(u["id"]) for u in usuarios]
    etiquetas = {
        int(u["id"]): f"@{u['username']}"
        + (f" · {u['nombre']}" if u["nombre"] else "")
        + (" · 🔴 inactivo" if not u["activo"] else "")
        for u in usuarios
    }
    try:
        actual = st.session_state.get(key)
        if actual not in opciones:
            st.session_state.pop(key, None)
    except Exception:
        pass
    return st.selectbox(
        etiqueta,
        opciones,
        format_func=lambda uid: etiquetas.get(int(uid), f"#{uid}"),
        key=key,
    )


def _resumen_secciones(filas_por_id: dict, seleccion: list) -> str:
    """'N cuentas · secciones: LIB 3, CI 2' para la seleccion actual."""
    conteo: dict = {}
    for cuenta_id in seleccion:
        fila = filas_por_id.get(int(cuenta_id)) or {}
        codigo = _texto(fila.get("seccion")) or "Sin sección"
        conteo[codigo] = conteo.get(codigo, 0) + 1
    try:
        detalle = ", ".join(
            f"{codigo} {cantidad}"
            for codigo, cantidad in sorted(
                conteo.items(), key=lambda par: (-par[1], par[0])
            )
        )
    except Exception:
        detalle = ""
    total = len(seleccion)
    texto = f"**{total} cuenta{'s' if total != 1 else ''}**"
    if detalle:
        texto += f" · secciones: {detalle}"
    return texto


def _cuentas_sin_sesion(filas_por_id: dict, seleccion: list) -> list:
    """Usernames de la seleccion sin auth_token/cookies."""
    return [
        _texto((filas_por_id.get(int(cid)) or {}).get("usuario"))
        for cid in seleccion
        if not (filas_por_id.get(int(cid)) or {}).get("con_sesion", False)
    ]


# ============================ UI: TAB 1 - USUARIOS ============================


def _tab_usuarios() -> None:
    st.markdown("### 👥 Usuarios de MWX APP")
    st.caption(
        "Credenciales del ejecutable (no son las del dashboard). Solo se "
        "muestra metadata operativa: nunca contrasenas ni hashes."
    )
    _mostrar_flash("mwx_user_flash")
    usuarios = listar_usuarios_app()
    _avisar_error_bd()

    with st.expander("➕ Crear usuario", expanded=not usuarios):
        with st.form("mwx_form_crear_usuario"):
            col1, col2 = st.columns(2)
            with col1:
                username = st.text_input(
                    "Usuario (minúsculas, 3-20)",
                    key="mwx_new_username",
                    max_chars=20,
                    help="Solo letras minúsculas, números, punto, guion y guion bajo.",
                )
                nombre = st.text_input(
                    "Nombre visible",
                    key="mwx_new_nombre",
                    max_chars=150,
                )
            with col2:
                password = st.text_input(
                    "Contraseña (mínimo 8)",
                    type="password",
                    key="mwx_new_password",
                )
                confirmacion = st.text_input(
                    "Confirmar contraseña",
                    type="password",
                    key="mwx_new_password2",
                )
            activo = st.checkbox(
                "Activo", value=True, key="mwx_new_activo"
            )
            enviado = st.form_submit_button(
                "Crear usuario", type="primary"
            )

        if enviado:
            if password != confirmacion:
                st.error("Las contraseñas no coinciden.")
            elif not username or not password:
                st.error("Usuario y contraseña son obligatorios.")
            else:
                resultado = crear_usuario_app(
                    username, password, nombre, activo
                )
                if resultado.get("ok"):
                    _flash(
                        "mwx_user_flash",
                        f"Usuario @{_normalizar_username(username)} creado.",
                    )
                    st.rerun()
                else:
                    st.error(
                        resultado.get("error")
                        or "No se pudo crear el usuario."
                    )

    if not usuarios:
        st.info(
            "No hay usuarios del APP todavia. Crea el primero con el formulario."
        )
        return

    st.markdown("#### 📋 Usuarios registrados")
    tabla = [
        {
            "Usuario": f"@{u['username']}",
            "Nombre": u["nombre"] or "—",
            "Activo": "🟢 Sí" if u["activo"] else "🔴 No",
            "Cuentas asignadas": int(u["cuentas"]),
            "Último acceso": _fmt_fecha(u["ultimo_acceso"]),
            "Creado": _fmt_fecha(u["creado"]),
        }
        for u in usuarios
    ]
    st.dataframe(tabla, use_container_width=True, hide_index=True)

    st.markdown("#### 🎯 Gestionar usuario")
    usuario_id = _selector_usuario(usuarios, "mwx_user_sel", "Selecciona usuario")
    seleccionado = next(
        (u for u in usuarios if int(u["id"]) == int(usuario_id)), None
    )
    if seleccionado is None:
        return

    st.caption(
        f"@{seleccionado['username']} · {seleccionado['nombre'] or 'sin nombre'} · "
        f"{seleccionado['cuentas']} cuenta(s) asignada(s) · "
        f"último acceso: {_fmt_fecha(seleccionado['ultimo_acceso'])}"
    )

    col_estado, col_revocar = st.columns(2)
    with col_estado:
        if seleccionado["activo"]:
            if st.button(
                "🔴 Desactivar usuario",
                key="mwx_user_btn_desactivar",
                use_container_width=True,
            ):
                resultado = cambiar_estado_usuario(usuario_id, False)
                if resultado.get("ok"):
                    _flash(
                        "mwx_user_flash",
                        f"@{seleccionado['username']} desactivado "
                        "(sus sesiones quedaron revocadas).",
                    )
                    st.rerun()
                else:
                    st.error(resultado.get("error") or "No se pudo desactivar.")
        else:
            if st.button(
                "🟢 Activar usuario",
                key="mwx_user_btn_activar",
                use_container_width=True,
            ):
                resultado = cambiar_estado_usuario(usuario_id, True)
                if resultado.get("ok"):
                    _flash(
                        "mwx_user_flash",
                        f"@{seleccionado['username']} activado.",
                    )
                    st.rerun()
                else:
                    st.error(resultado.get("error") or "No se pudo activar.")

    with col_revocar:
        confirmar_revocar = st.checkbox(
            "Confirmo revocar las sesiones",
            key=f"mwx_user_conf_revocar_{usuario_id}",
        )
        if st.button(
            "🚫 Revocar sesiones",
            key="mwx_user_btn_revocar",
            use_container_width=True,
            disabled=not confirmar_revocar,
        ):
            resultado = revocar_sesiones_usuario(usuario_id)
            if resultado.get("ok"):
                _flash(
                    "mwx_user_flash",
                    f"Sesiones revocadas (token_version={resultado.get('version')}).",
                )
                _programar_limpieza(f"mwx_user_conf_revocar_{usuario_id}")
                st.rerun()
            else:
                st.error(resultado.get("error") or "No se pudieron revocar.")

    with st.expander("🔑 Resetear contraseña"):
        nueva = st.text_input(
            "Nueva contraseña (mínimo 8)",
            type="password",
            key=f"mwx_user_new_password_{usuario_id}",
        )
        nueva2 = st.text_input(
            "Confirmar nueva contraseña",
            type="password",
            key=f"mwx_user_new_password2_{usuario_id}",
        )
        if st.button(
            "🔑 Guardar nueva contraseña",
            key="mwx_user_btn_reset",
        ):
            if nueva != nueva2:
                st.error("Las contraseñas no coinciden.")
            elif not nueva:
                st.error("Escribe la nueva contraseña.")
            else:
                resultado = resetear_password_usuario(usuario_id, nueva)
                if resultado.get("ok"):
                    _flash(
                        "mwx_user_flash",
                        "Contraseña actualizada. Recuerda revocar las sesiones "
                        "si la cuenta estaba comprometida.",
                    )
                    st.rerun()
                else:
                    st.error(
                        resultado.get("error")
                        or "No se pudo cambiar la contraseña."
                    )

    with st.expander("🗑️ Eliminar usuario"):
        st.warning(
            "Se borrarán el usuario, sus asignaciones, su configuración y su "
            "historial de eventos. Las cuentas de X NO se eliminan."
        )
        confirmar_eliminar = st.checkbox(
            f"Confirmo eliminar definitivamente a @{seleccionado['username']}",
            key=f"mwx_user_conf_eliminar_{usuario_id}",
        )
        if st.button(
            "🗑️ Eliminar usuario",
            key="mwx_user_btn_eliminar",
            disabled=not confirmar_eliminar,
        ):
            resultado = eliminar_usuario_app(usuario_id)
            if resultado.get("ok"):
                _programar_limpieza(
                    "mwx_user_sel",
                    "mwx_asig_usuario",
                    "mwx_cfg_usuario",
                    "mwx_seg_usuario",
                    "mwx_seg_usuario_estado",
                    f"mwx_user_conf_revocar_{usuario_id}",
                    f"mwx_user_conf_eliminar_{usuario_id}",
                    f"mwx_user_new_password_{usuario_id}",
                    f"mwx_user_new_password2_{usuario_id}",
                )
                limpiar_cache_app()
                _flash(
                    "mwx_user_flash",
                    f"@{resultado.get('username') or 'usuario'} eliminado "
                    f"({resultado.get('asignaciones', 0)} asignación(es), "
                    f"{resultado.get('config', 0)} config, "
                    f"{resultado.get('eventos', 0)} evento(s)).",
                )
                st.rerun()
            else:
                st.error(resultado.get("error") or "No se pudo eliminar.")


# ============================ UI: TAB 2 - ASIGNAR ============================


def _tab_asignar() -> None:
    st.markdown("### 🔗 Asignar cuentas de X a un usuario")
    st.caption(
        "El ejecutable solo recibe las cuentas asignadas a su usuario. "
        "Guardar REEMPLAZA la selección actual."
    )
    _mostrar_flash("mwx_asig_flash")
    usuarios = listar_usuarios_app()
    _avisar_error_bd()
    if not usuarios:
        st.info("Crea primero un usuario en la pestaña 👥 Usuarios.")
        return

    usuario_id = _selector_usuario(
        usuarios, "mwx_asig_usuario", "Usuario de MWX APP"
    )
    usuario = next(u for u in usuarios if int(u["id"]) == int(usuario_id))

    col1, col2, col3 = st.columns(3)
    with col1:
        busqueda = st.text_input(
            "Buscar por usuario o @",
            key="mwx_asig_buscar",
            placeholder="ej. ana o @ana_x",
        )
    with col2:
        seccion_sel = st.selectbox(
            "Sección",
            ["Todas"] + list(SECCIONES),
            format_func=lambda codigo: (
                "Todas"
                if codigo == "Todas"
                else f"{codigo} — {SECCIONES[codigo]}"
            ),
            key="mwx_asig_seccion",
        )
    with col3:
        tier_sel = st.selectbox(
            "Tier",
            list(TIER_OPCIONES),
            key="mwx_asig_tier",
        )

    col4, col5 = st.columns(2)
    with col4:
        solo_sesion = st.checkbox(
            "Solo con sesión válida (auth_token o cookies)",
            key="mwx_asig_sesion",
        )
    with col5:
        mostrar_pausadas = st.checkbox(
            "Mostrar pausadas", value=False, key="mwx_asig_pausadas"
        )

    asignadas = listar_asignaciones(usuario_id)
    filas = listar_cuentas_app(
        busqueda=busqueda,
        seccion="" if seccion_sel == "Todas" else seccion_sel,
        tier=TIER_OPCIONES.get(tier_sel, ""),
        solo_con_sesion=solo_sesion,
        mostrar_pausadas=mostrar_pausadas,
        incluir_ids=tuple(asignadas),
    )
    _avisar_error_bd()

    filas_por_id = {int(f["id"]): f for f in filas}
    opciones = list(filas_por_id)
    opciones.sort(key=lambda cid: filas_por_id[cid]["label"].lower())
    por_defecto = [cid for cid in asignadas if cid in filas_por_id]

    seleccion = st.multiselect(
        "Cuentas asignadas al usuario",
        opciones,
        default=por_defecto,
        format_func=lambda cid: filas_por_id.get(int(cid), {}).get(
            "label", f"#{cid}"
        ),
        key="mwx_asig_cuentas",
    )
    seleccion = [int(cid) for cid in seleccion]

    st.markdown(_resumen_secciones(filas_por_id, seleccion))
    sin_sesion = _cuentas_sin_sesion(filas_por_id, seleccion)
    if sin_sesion:
        st.warning(
            "⚠️ Sin sesión válida (no podrán publicar hasta renovar cookies/"
            "auth_token): "
            + ", ".join(f"@{u}" for u in sin_sesion[:10])
            + (" …" if len(sin_sesion) > 10 else "")
        )
    pausadas = [
        filas_por_id[cid]["usuario"]
        for cid in seleccion
        if filas_por_id.get(cid, {}).get("pausada")
    ]
    if pausadas:
        st.info(
            "⏸️ Incluye cuentas pausadas para activación: "
            + ", ".join(f"@{u}" for u in pausadas[:10])
            + (" …" if len(pausadas) > 10 else "")
        )

    col_guardar, col_atajo = st.columns(2)
    with col_guardar:
        if st.button(
            "💾 Guardar asignaciones",
            type="primary",
            key="mwx_asig_btn_guardar",
            use_container_width=True,
        ):
            resultado = guardar_asignaciones(usuario_id, seleccion)
            if resultado.get("ok"):
                _flash(
                    "mwx_asig_flash",
                    f"Asignaciones guardadas: {resultado.get('total', 0)} "
                    f"cuenta(s) para @{usuario['username']}.",
                )
                st.rerun()
            else:
                st.error(
                    resultado.get("error")
                    or "No se pudieron guardar las asignaciones."
                )

    with col_atajo:
        seccion_atajo = st.selectbox(
            "Sección para el atajo",
            list(SECCIONES),
            format_func=lambda codigo: f"{codigo} — {SECCIONES[codigo]}",
            key="mwx_asig_atajo_seccion",
        )
        if st.button(
            f"✅ Asignar TODAS las de la sección {seccion_atajo} al usuario",
            key="mwx_asig_btn_todas",
            use_container_width=True,
        ):
            resultado = asignar_todas_seccion(usuario_id, seccion_atajo)
            if resultado.get("ok"):
                _flash(
                    "mwx_asig_flash",
                    f"Se agregaron {resultado.get('agregadas', 0)} cuenta(s) "
                    f"de la sección {resultado.get('seccion')}. "
                    f"Total asignado: {resultado.get('total', 0)}.",
                )
                st.rerun()
            else:
                st.error(
                    resultado.get("error")
                    or "No se pudieron asignar las cuentas."
                )


# ============================ UI: TAB 3 - CAMPANA ============================


def _tab_campana() -> None:
    st.markdown("### ⚙️ Campaña del usuario")
    st.caption(
        "Esta configuración es la que verá el ejecutable al iniciar sesión. "
        "Los topes se aplican por cuenta."
    )
    _mostrar_flash("mwx_cfg_flash")
    usuarios = listar_usuarios_app()
    _avisar_error_bd()
    if not usuarios:
        st.info("Crea primero un usuario en la pestaña 👥 Usuarios.")
        return

    usuario_id = _selector_usuario(
        usuarios, "mwx_cfg_usuario", "Usuario de MWX APP"
    )
    usuario = next(u for u in usuarios if int(u["id"]) == int(usuario_id))
    uid = int(usuario_id)
    username = usuario["username"]
    config = obtener_config_app(uid)
    _avisar_error_bd()

    hashtags_texto = st.text_input(
        "Hashtags (separados por espacios o comas; 1-6)",
        value=" ".join(config.get("hashtags", [])),
        key=f"mwx_cfg_{uid}_hashtags",
        help="Se normalizan con # y se guardan como máximo 6 (sin duplicados).",
    )
    contexto = st.text_area(
        "Contexto / tema manual",
        value=config.get("contexto", ""),
        key=f"mwx_cfg_{uid}_contexto",
        height=90,
        help="Tema que el usuario puede opinar. Se usa para generar los textos.",
    )
    narrativa = st.text_area(
        "Narrativa / trasfondo de noticias (invisible)",
        value=config.get("narrativa", ""),
        key=f"mwx_cfg_{uid}_narrativa",
        height=90,
        help=(
            "Material de referencia INTERNO: el generador NO debe mencionarlo "
            "ni citarlo en las publicaciones."
        ),
    )
    urls_texto = st.text_area(
        "URLs ancla (una por línea)",
        value="\n".join(config.get("urls", [])),
        key=f"mwx_cfg_{uid}_urls",
        height=100,
        help="Tweets que se usarán para citas/RT. Solo se aceptan http(s).",
    )
    texto_base = st.text_area(
        "Texto base de la cita",
        value=config.get("texto_base", ""),
        key=f"mwx_cfg_{uid}_texto_base",
        height=90,
        help="Idea base que se parafrasea de forma distinta en cada cita.",
    )

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        acciones = st.number_input(
            "Acciones por cuenta",
            min_value=1,
            max_value=49,
            value=_entero(config.get("acciones_por_cuenta"), 10, 1, 49),
            step=1,
            key=f"mwx_cfg_{uid}_acciones",
        )
    with col2:
        pausa = st.number_input(
            "Pausa entre acciones (s)",
            min_value=1,
            max_value=120,
            value=_entero(config.get("pausa_seg"), 2, 1, 120),
            step=1,
            key=f"mwx_cfg_{uid}_pausa",
        )
    with col3:
        duracion = st.number_input(
            "Duración (min)",
            min_value=10,
            max_value=240,
            value=_entero(config.get("duracion_min"), 60, 10, 240),
            step=5,
            key=f"mwx_cfg_{uid}_duracion",
        )
    with col4:
        limite = st.number_input(
            "Límite diario por cuenta",
            min_value=1,
            max_value=49,
            value=_entero(config.get("limite_dia"), 49, 1, 49),
            step=1,
            key=f"mwx_cfg_{uid}_limite",
        )
    with col5:
        navegadores = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=3,
            value=_entero(config.get("navegadores"), 1, 1, 3),
            step=1,
            key=f"mwx_cfg_{uid}_navegadores",
            help=(
                "1 = un Chrome; 2-3 = más velocidad entre cuentas, pero más "
                "RAM/CPU. Sin proxies salen por la misma IP."
            ),
        )

    permitir_ajustes = st.checkbox(
        "Permitir que el usuario ajuste estos valores en el ejecutable",
        value=bool(config.get("permitir_ajustes", False)),
        key=f"mwx_cfg_{uid}_ajustes",
    )

    st.markdown("#### 🖼️ Imágenes para los posts")
    archivos = st.file_uploader(
        "Imágenes (png/jpg/jpeg/webp; puedes subir varias)",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key=f"mwx_cfg_{uid}_imagenes",
        help=(
            "Se guardan en la base de datos del servidor (y una copia local "
            "para la vista previa). Si no subes nada, se conservan las que "
            "ya tenía el usuario."
        ),
    )
    imagenes_guardadas = list(config.get("imagenes", []))
    imagenes_bd: dict = {}
    if imagenes_guardadas:
        try:
            with obtener_sesion() as db:
                for nombre in imagenes_guardadas[:8]:
                    datos_imagen = leer_imagen_bd(db, uid, nombre)
                    if datos_imagen and datos_imagen.get("contenido"):
                        imagenes_bd[nombre] = datos_imagen
        except Exception:
            imagenes_bd = {}
    if imagenes_guardadas:
        st.caption(
            f"{len(imagenes_guardadas)} imagen(es) guardada(s). "
            "Máximo 20."
        )
        for indice, nombre in enumerate(imagenes_guardadas[:8]):
            ruta = ruta_imagen_app(username, nombre)
            col_img, col_accion = st.columns([1, 3])
            with col_img:
                datos_bd = imagenes_bd.get(nombre)
                if datos_bd:
                    try:
                        st.image(BytesIO(datos_bd["contenido"]), width=120)
                    except Exception:
                        st.caption("(no se pudo previsualizar)")
                elif ruta and os.path.isfile(ruta):
                    try:
                        st.image(ruta, width=120)
                    except Exception:
                        st.caption("(no se pudo previsualizar)")
                else:
                    st.caption("(imagen no disponible)")
            with col_accion:
                st.caption(nombre)
                if st.button(
                    "🗑️ Eliminar imagen",
                    key=f"mwx_cfg_{uid}_img_del_{indice}",
                ):
                    resultado = eliminar_imagen_mwx(uid, username, nombre)
                    if resultado.get("ok"):
                        _flash(
                            "mwx_cfg_flash",
                            f"Imagen '{nombre}' eliminada.",
                        )
                        st.rerun()
                    else:
                        st.error(
                            resultado.get("error")
                            or "No se pudo eliminar la imagen."
                        )
        if len(imagenes_guardadas) > 8:
            st.caption(
                f"… y {len(imagenes_guardadas) - 8} imagen(es) más (se "
                "muestran las primeras 8)."
            )

    if st.button(
        "💾 Guardar configuración",
        type="primary",
        key=f"mwx_cfg_{uid}_guardar",
    ):
        nombres, errores = guardar_imagenes_mwx(
            username, archivos, imagenes_guardadas, usuario_id=uid
        )
        for aviso in errores:
            st.warning(aviso)
        resultado = guardar_config_app(
            uid,
            {
                "hashtags": hashtags_texto,
                "contexto": contexto,
                "narrativa": narrativa,
                "urls": urls_texto,
                "texto_base": texto_base,
                "acciones_por_cuenta": acciones,
                "pausa_seg": pausa,
                "duracion_min": duracion,
                "limite_dia": limite,
                "navegadores": navegadores,
                "imagenes": nombres,
                "permitir_ajustes": permitir_ajustes,
            },
        )
        if resultado.get("ok"):
            _flash(
                "mwx_cfg_flash",
                f"Configuración guardada para @{username}.",
            )
            st.rerun()
        else:
            st.error(
                resultado.get("error")
                or "No se pudo guardar la configuración."
            )

    # ---- Vista previa "Lo que verá el usuario" ----
    st.markdown("#### 👁️ Lo que verá el usuario")
    hashtags_previos = normalizar_hashtags(hashtags_texto)
    asignadas = listar_asignaciones(uid)
    filas = listar_cuentas_app(
        mostrar_pausadas=True,
        incluir_ids=tuple(asignadas),
    )
    _avisar_error_bd()
    filas_por_id = {int(f["id"]): f for f in filas}
    secciones = _resumen_secciones(filas_por_id, asignadas)
    st.markdown(
        f"""
- **Cuentas para publicar:** {len(asignadas)} ({secciones.split('secciones: ')[-1] if 'secciones: ' in secciones else 'sin sección'})
- **Hashtags:** {' '.join(hashtags_previos) if hashtags_previos else '(sin hashtags)'}
- **Publicaciones por cuenta:** {int(acciones)}
- **Ventana:** {int(duracion)} minutos con pausas de {int(pausa)} s
- **Límite diario:** {int(limite)} acciones por cuenta
- **Navegadores simultáneos:** {int(navegadores)}
- **Imágenes:** {len(imagenes_guardadas)}
- **Puede ajustar valores:** {'sí' if permitir_ajustes else 'no'}
""".strip()
    )
    if not normalizar_urls(urls_texto):
        st.caption(
            "Sin URLs ancla: el ejecutable solo podrá publicar posts (no "
            "citas ni RT)."
        )
    if not hashtags_previos:
        st.warning(
            "Sin hashtags: el usuario no podrá publicar posts con hashtag "
            "hasta que agregues al menos uno."
        )


# ============================ UI: TAB 4 - ACTIVIDAD / SEGURIDAD ============================


def _tab_seguridad() -> None:
    st.markdown("### 📊 Actividad y seguridad")
    _mostrar_flash("mwx_seg_flash")
    usuarios = listar_usuarios_app()
    _avisar_error_bd()

    st.markdown("#### 📜 Últimos eventos")
    if not usuarios:
        st.info("Todavía no hay usuarios del APP ni eventos.")
    else:
        filtros = st.columns(2)
        with filtros[0]:
            opciones_usuario = [0] + [int(u["id"]) for u in usuarios]
            etiquetas = {
                int(u["id"]): f"@{u['username']}" for u in usuarios
            }
            try:
                actual = st.session_state.get("mwx_seg_usuario")
                if actual not in opciones_usuario:
                    st.session_state.pop("mwx_seg_usuario", None)
            except Exception:
                pass
            usuario_filtro = st.selectbox(
                "Filtrar por usuario",
                opciones_usuario,
                format_func=lambda uid: (
                    "Todos" if int(uid) == 0 else etiquetas.get(int(uid), f"#{uid}")
                ),
                key="mwx_seg_usuario",
            )
        with filtros[1]:
            tipos = listar_tipos_evento()
            tipo_filtro = st.selectbox(
                "Filtrar por tipo",
                ["Todos"] + tipos,
                key="mwx_seg_tipo",
            )

        eventos = listar_eventos_app(
            None if int(usuario_filtro) == 0 else int(usuario_filtro),
            "" if tipo_filtro == "Todos" else tipo_filtro,
            MAX_EVENTOS,
        )
        _avisar_error_bd()
        if eventos:
            st.dataframe(
                eventos, use_container_width=True, hide_index=True
            )
            st.caption(f"Mostrando los últimos {len(eventos)} eventos.")
        else:
            st.info("No hay eventos con esos filtros.")

    st.markdown("---")
    st.markdown("#### 🔌 Estado del API para el ejecutable")
    api_url = (
        _texto(getattr(settings, "mwx_api_url", ""))
        or _texto(os.environ.get("MWX_API_URL", ""))
    )
    st.text_input(
        "URL del API (MWX_API_URL) — solo lectura",
        value=api_url or "(sin configurar)",
        disabled=True,
        key="mwx_seg_api_url",
    )
    if api_url:
        st.caption(
            "En el .exe configura la MISMA URL pública (por ejemplo "
            f"`{api_url}`) en la variable de entorno/ajustes de conexión."
        )
    else:
        st.caption(
            "Define `MWX_API_URL` en el servidor (o en el .env) con la URL "
            "pública del API, por ejemplo `https://tu-dominio.com` o "
            "`http://192.168.1.10:8765`. Después configura esa misma URL en "
            "el ejecutable. No se muestran tokens ni claves."
        )

    st.markdown("---")
    st.markdown("#### 🚨 Acciones globales")

    with st.expander("🚫 Revocar TODAS las sesiones", expanded=False):
        st.warning(
            "Invalida los tokens de todos los usuarios del APP (deberán "
            "iniciar sesión de nuevo). No cambia contraseñas."
        )
        confirmar_todas = st.checkbox(
            "Confirmo revocar las sesiones de todos los usuarios",
            key="mwx_seg_conf_revocar",
        )
        if st.button(
            "🚫 Revocar todas las sesiones",
            key="mwx_seg_btn_revocar",
            disabled=not confirmar_todas,
        ):
            resultado = revocar_todas_las_sesiones()
            if resultado.get("ok"):
                _flash(
                    "mwx_seg_flash",
                    f"Sesiones revocadas en {resultado.get('total', 0)} usuario(s).",
                )
                _programar_limpieza("mwx_seg_conf_revocar")
                st.rerun()
            else:
                st.error(
                    resultado.get("error")
                    or "No se pudieron revocar las sesiones."
                )

    with st.expander("⏸️ Desactivar un usuario", expanded=False):
        if usuarios:
            usuario_id = _selector_usuario(
                usuarios, "mwx_seg_usuario_estado", "Usuario a desactivar"
            )
            seleccionado = next(
                u for u in usuarios if int(u["id"]) == int(usuario_id)
            )
            if not seleccionado["activo"]:
                st.info(f"@{seleccionado['username']} ya está desactivado.")
            else:
                confirmar_baja = st.checkbox(
                    f"Confirmo desactivar a @{seleccionado['username']}",
                    key=f"mwx_seg_conf_estado_{usuario_id}",
                )
                if st.button(
                    "⏸️ Desactivar usuario",
                    key="mwx_seg_btn_desactivar",
                    disabled=not confirmar_baja,
                ):
                    resultado = cambiar_estado_usuario(usuario_id, False)
                    if resultado.get("ok"):
                        _flash(
                            "mwx_seg_flash",
                            f"@{seleccionado['username']} desactivado y sus "
                            "sesiones revocadas.",
                        )
                        _programar_limpieza(f"mwx_seg_conf_estado_{usuario_id}")
                        st.rerun()
                    else:
                        st.error(
                            resultado.get("error")
                            or "No se pudo desactivar el usuario."
                        )
        else:
            st.info("No hay usuarios para desactivar.")

    with st.expander("🛡️ Checklist de seguridad", expanded=False):
        st.markdown(
            """
- ✅ **El exe solo recibe las cuentas asignadas** a su usuario (nunca la flota completa).
- ✅ **Nunca entregues la carpeta `data/` completa** ni la base de datos; comparte únicamente el ejecutable y la URL del API.
- ✅ **Cambia la contraseña al dar de baja a alguien** y revoca sus sesiones.
- ✅ Usa contraseñas de al menos 8 caracteres y no las reutilices con las del dashboard.
- ✅ Revisa esta pestaña de eventos ante inicios de sesión o campañas inesperadas.
- ✅ Las cuentas pausadas para activación no deberían usarse en campañas del APP.
""".strip()
        )


# ============================ EJECUTABLE / ACTUALIZACIONES ============================


@st.cache_data(ttl=15, show_spinner=False)
def _cache_ultimo_release() -> dict:
    """Metadatos del ultimo release (cache 15s). Nunca lanza."""
    try:
        from core.app_releases import ultimo_release

        datos = ultimo_release()
        return datos if isinstance(datos, dict) else {"disponible": False}
    except Exception:
        return {"disponible": False}


@st.cache_data(ttl=15, show_spinner=False)
def _cache_listar_releases(limite: int = 5) -> list:
    """Ultimos releases SIN contenido (cache 15s). Nunca lanza."""
    try:
        from core.app_releases import listar_releases

        datos = listar_releases(limite)
        return list(datos) if isinstance(datos, list) else []
    except Exception:
        return []


@st.cache_data(ttl=15, show_spinner=False, max_entries=1)
def _cache_contenido_release(version: str, sha256: str = "") -> bytes:
    """Contenido del release pedido (cache 15s; evita releer 34 MB por rerun)."""
    try:
        from core.app_releases import leer_release

        datos = leer_release(version)
        contenido = datos.get("contenido") if isinstance(datos, dict) else b""
        return contenido if isinstance(contenido, bytes) else b""
    except Exception:
        return b""


def _limpiar_cache_releases() -> None:
    """Invalida los metadatos/contenido cacheados tras publicar/empaquetar."""
    for funcion in (
        _cache_ultimo_release,
        _cache_listar_releases,
        _cache_contenido_release,
    ):
        try:
            funcion.clear()
        except Exception:
            pass


def _mb(tamano) -> str:
    """Bytes como 'X.X MB' ('' si el valor no es numerico)."""
    try:
        return f"{int(tamano) / (1024 * 1024):.1f} MB"
    except Exception:
        return ""


def _tab_ejecutable() -> None:
    """Descarga del ejecutable MWX APP + publicacion de actualizaciones.

    La descarga usa el ultimo release de la BD (`app_releases`) y, si no hay
    ninguno, permite empaquetar `dist/MWX APP` en local (en Railway esa carpeta
    no existe: alli hay que subir el ZIP). Ninguna accion de empaquetado o
    publicacion se ejecuta al renderizar: solo al pulsar los botones."""
    st.markdown("### ⬇️ Ejecutable / Actualizaciones")
    st.caption(
        "El ZIP se guarda en la base de datos (`app_releases`) para que la API "
        "lo sirva y los .exe se actualicen solos al abrirlos."
    )
    try:
        from core.app_releases import (
            empaquetar_dist,
            publicar_release,
            version_actual,
        )
    except Exception as e:
        st.error(f"La capa de releases no está disponible: {e}")
        return

    _mostrar_flash("mwx_release_flash")

    # ---------------- Descargar el ejecutable ----------------
    st.markdown("#### 📦 Descargar el ejecutable")
    ultimo = _cache_ultimo_release()
    disponible = bool(isinstance(ultimo, dict) and ultimo.get("disponible"))
    if disponible:
        version = _texto(ultimo.get("version")) or "(sin versión)"
        col_ver, col_fecha, col_tam, col_oblig = st.columns(4)
        col_ver.metric("Versión", version)
        col_fecha.metric("Publicada", _texto(ultimo.get("fecha"))[:16] or "—")
        col_tam.metric("Tamaño", _mb(ultimo.get("tamano")) or "—")
        col_oblig.metric(
            "Obligatoria",
            "Sí" if ultimo.get("obligatorio") else "No",
        )
        notas = _texto(ultimo.get("notas"))
        if notas:
            st.caption(f"Notas: {notas}")
        filename = (
            _texto(ultimo.get("filename"))
            or f"MWX-APP-{version}.zip"
        )
        contenido = _cache_contenido_release(
            _texto(ultimo.get("version")),
            _texto(ultimo.get("sha256")),
        )
        if contenido:
            st.download_button(
                f"⬇️ Descargar MWX APP {version}",
                data=contenido,
                file_name=filename,
                mime="application/zip",
                key="dl_mwx_release",
            )
        else:
            st.warning(
                "El release está registrado pero su contenido no se pudo leer."
            )
    else:
        try:
            ruta_dist = _texto(resolver_ruta("dist/MWX APP"))
        except Exception:
            ruta_dist = ""
        existe_dist = bool(ruta_dist) and os.path.isdir(ruta_dist)
        if existe_dist:
            st.info(
                "No hay release publicado en la BD, pero existe la carpeta "
                f"`{ruta_dist}`. Puedes empaquetarla y descargar el ZIP."
            )
            if st.button(
                "📦 Empaquetar dist/MWX APP",
                key="mwx_btn_empaquetar",
            ):
                with st.spinner("Empaquetando el ejecutable…"):
                    resultado = empaquetar_dist()
                if resultado.get("ok") and resultado.get("zip"):
                    st.session_state["mwx_zip_local"] = resultado["zip"]
                    st.success(
                        "ZIP generado: "
                        f"{resultado.get('filename') or resultado['zip']} "
                        f"({_mb(resultado.get('tamano')) or '—'})."
                    )
                else:
                    st.error(
                        resultado.get("error")
                        or "No se pudo empaquetar dist/MWX APP."
                    )
            ruta_zip = _texto(st.session_state.get("mwx_zip_local"))
            if ruta_zip and os.path.isfile(ruta_zip):
                try:
                    datos_zip = Path(ruta_zip).read_bytes()
                except Exception as e:
                    datos_zip = b""
                    st.error(f"No se pudo leer el ZIP local: {e}")
                if datos_zip:
                    st.download_button(
                        "⬇️ Descargar ZIP local",
                        data=datos_zip,
                        file_name=os.path.basename(ruta_zip),
                        mime="application/zip",
                        key="dl_mwx_release_local",
                    )
            st.caption(
                "En Railway no existe `dist/`: allí hay que compilar en local "
                "y subir el ZIP en «Publicar actualización remota»."
            )
        else:
            st.info(
                "No hay ningún release publicado ni la carpeta `dist/MWX APP`. "
                "Compila el ejecutable con `build_mwx.bat` y publica el ZIP en "
                "la sección de abajo."
            )

    st.markdown("---")

    # ---------------- Publicar actualización remota ----------------
    st.markdown("#### 🚀 Publicar actualización remota (autoactualización forzada)")
    st.caption(
        "Se conservan solo los 2 releases más recientes. Si marcas la "
        "actualización como obligatoria, el .exe se bloquea hasta actualizar."
    )
    subido = st.file_uploader(
        "Paquete ZIP del ejecutable",
        type=["zip"],
        key="mwx_release_zip",
        help="Máximo 150 MB. Compílalo con build_mwx.bat.",
    )
    if subido is not None:
        try:
            tamano_subido = len(subido.getvalue())
        except Exception:
            tamano_subido = 0
        if tamano_subido > 150 * 1024 * 1024:
            st.warning(
                f"El archivo pesa {_mb(tamano_subido)}: el máximo es 150 MB."
            )
        else:
            st.caption(f"Archivo cargado: {_mb(tamano_subido)}.")
    version_nueva = st.text_input(
        "Versión del release",
        value=version_actual(),
        key="mwx_release_version",
        help="Ej. 1.0.1. Si ya existe, se reemplaza.",
    )
    obligatorio = st.checkbox(
        "Actualización obligatoria (bloquea la app hasta actualizar)",
        value=True,
        key="mwx_release_obligatorio",
    )
    notas = st.text_area(
        "Notas de la versión (opcional)",
        key="mwx_release_notas",
        height=80,
    )

    if st.button(
        "🚀 Publicar actualización",
        type="primary",
        key="mwx_release_publicar",
    ):
        if subido is None:
            st.warning("Adjunta un ZIP del ejecutable.")
        elif not str(version_nueva or "").strip():
            st.warning("Escribe una versión para el release.")
        else:
            try:
                contenido = subido.getvalue()
            except Exception as e:
                contenido = b""
                st.error(f"No se pudo leer el archivo subido: {e}")
            if contenido:
                resultado = publicar_release(
                    contenido,
                    str(version_nueva).strip(),
                    bool(obligatorio),
                    str(notas or ""),
                )
                if resultado.get("ok"):
                    st.success(
                        "Actualización publicada: "
                        f"v{resultado.get('version')} · "
                        f"{_mb(resultado.get('tamano')) or '—'} · "
                        f"sha256 {str(resultado.get('sha256'))[:12]}…"
                    )
                    st.warning(
                        "Los .exe se actualizarán solos al abrirlos"
                        + (
                            " (obligatoria: la app queda bloqueada hasta "
                            "actualizar)."
                            if obligatorio
                            else "."
                        )
                    )
                    _limpiar_cache_releases()
                else:
                    st.error(
                        resultado.get("error")
                        or "No se pudo publicar la actualización."
                    )

    releases = _cache_listar_releases(5)
    if releases:
        st.markdown("##### 🗂️ Releases publicados (máx. 2 en retención)")
        st.dataframe(
            [
                {
                    "versión": _texto(r.get("version")),
                    "obligatoria": "Sí" if r.get("obligatorio") else "No",
                    "tamaño": _mb(r.get("tamano")) or "—",
                    "fecha": _texto(r.get("fecha"))[:16],
                    "sha256": _texto(r.get("sha256"))[:12],
                }
                for r in releases
            ],
            use_container_width=True,
        )
    else:
        st.caption("Todavía no hay releases publicados.")


# ============================ RENDER ============================


def render(usuario: dict) -> None:
    """Punto de entrada de la operación (registrada en `web/app.py`)."""
    if not _es_admin(usuario):
        st.error("Acceso restringido. Solo administradores del dashboard.")
        return

    # Limpia claves de widgets que una accion anterior dejo marcadas (se
    # hace ANTES de instanciar cualquier widget de las pestanas).
    _aplicar_limpieza()

    cabecera(
        "📲 MWX APP",
        "Usuarios, cuentas asignadas y campaña del ejecutable",
    )
    st.caption(
        "Panel de administración del ejecutable MWX APP. Los cambios se "
        "aplican al instante para el usuario; el exe solo recibe sus cuentas."
    )

    tabs = st.tabs(
        [
            "👥 Usuarios",
            "🔗 Asignar cuentas",
            "⚙️ Campaña",
            "📊 Actividad / Seguridad",
            "⬇️ Ejecutable / Actualizaciones",
        ]
    )
    with tabs[0]:
        _tab_usuarios()
    with tabs[1]:
        _tab_asignar()
    with tabs[2]:
        _tab_campana()
    with tabs[3]:
        _tab_seguridad()
    with tabs[4]:
        _tab_ejecutable()
