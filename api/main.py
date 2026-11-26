# -*- coding: utf-8 -*-
"""API HTTP del ejecutable MWX APP (FastAPI + Pydantic v2).

Contrato ``/v1`` consumido por el cliente desktop:

- ``GET  /health``               healthcheck público de Railway (sin auth)
- ``GET  /v1/app/version``       último release de la app (metadatos, sin contenido)
- ``GET  /v1/app/descargar``     ZIP del release (público, para autoactualizarse)

Releases por plataforma sin migración de esquema: la ``version`` lleva el
sufijo (Windows ``1.1.4``; macOS ``1.1.4-mac-arm64`` / ``1.1.4-mac-intel``).
Los clientes Windows NO envían ``plataforma`` (default ``windows``); los Mac
envían ``?plataforma=mac-arm64`` o ``mac-intel``.
- ``POST /v1/login``            credenciales -> token HMAC + campaña + cuentas
- ``GET  /v1/perfil``           mismo payload sin contraseñas (token Bearer)
- ``POST /v1/estado``           comprueba que el token/usuario siguen vigentes
- ``GET  /v1/imagenes/{nombre}`` imagen de campaña: BD primero, archivo como fallback
- ``POST /v1/chat/completions`` proxy OpenAI con la key real del servidor
- ``POST /v1/evento``           auditoría opcional del cliente

Despliegue independiente (Railway):
    uvicorn api.main:app --host 0.0.0.0 --port $PORT

Nunca se registran contraseñas, tokens, cookies completas, prompts ni la
`OPENAI_API_KEY` en logs.
"""
from __future__ import annotations

import json
import mimetypes
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

# `core.config.Settings` exige OPENAI_API_KEY al importarse. El proxy debe
# responder 503 (no tumbar el arranque) si falta la key, asi que cargamos el
# .env del proyecto y, si aun no hay key real, usamos un centinela interno.
_CENTINELA_SIN_OPENAI = "__mwx_sin_openai_key__"


def _asegurar_import_de_settings() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    except Exception:
        pass
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        os.environ["OPENAI_API_KEY"] = _CENTINELA_SIN_OPENAI


_asegurar_import_de_settings()

import httpx  # noqa: E402
from fastapi import Body, Depends, FastAPI, HTTPException, Request, status  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from loguru import logger  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from core.app_auth import crear_token_app, verify_password  # noqa: E402
from core.app_imagenes import leer_imagen, nombre_seguro  # noqa: E402
from core.config import resolver_ruta, settings  # noqa: E402
from core.models import (  # noqa: E402
    AppAsignacion,
    AppConfig,
    AppEvento,
    AppUsuario,
    Cuenta,
)
from core import app_releases  # noqa: E402

from api.deps import (  # noqa: E402
    get_db,
    limpiar_intentos,
    obtener_usuario_actual,
    registrar_intento_fallido,
    verificar_rate_limit,
)
from api.schemas import EventoRequest, LoginRequest  # noqa: E402

# ---------------------------------------------------------------------------
# Constantes del contrato
# ---------------------------------------------------------------------------
MODELOS_OPENAI_PERMITIDOS = frozenset(
    {"gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"}
)
MAX_TOKENS_OPENAI = 4096
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT_OPENAI_SEG = 60.0


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------
def _inicializar_bd() -> None:
    """Crea/actualiza las tablas del proyecto (`init_db`) al arrancar."""
    from core.database import init_db

    init_db()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _inicializar_bd()
    logger.info("MWX APP API iniciada")
    yield


app = FastAPI(
    title="MWX APP API",
    version="1.0.0",
    description="API del ejecutable MWX APP (login, campaña, imágenes, IA).",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Errores uniformes: {"ok": false, "error": "..."}
# ---------------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def _manejar_http_exception(_request: Request, exc: HTTPException) -> JSONResponse:
    detalle = exc.detail if isinstance(exc.detail, str) else "solicitud inválida"
    if exc.status_code == status.HTTP_404_NOT_FOUND and detalle == "Not Found":
        detalle = "recurso no encontrado"
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": detalle})


@app.exception_handler(RequestValidationError)
async def _manejar_validacion(_request: Request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "datos inválidos"},
    )


@app.exception_handler(Exception)
async def _manejar_error_generico(_request: Request, exc: Exception) -> JSONResponse:
    logger.error(f"Error interno MWX API: {type(exc).__name__}")
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "error interno"},
    )


# ---------------------------------------------------------------------------
# Utilidades de serializacion
# ---------------------------------------------------------------------------
def _texto(valor: Any) -> str:
    """Convierte cualquier valor de BD a string sin lanzar."""
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor
    if isinstance(valor, (dict, list, tuple)):
        try:
            return json.dumps(valor, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(valor)


def _lista_json(valor: Any) -> list:
    """Parsea un campo Text JSON a lista (tolera listas nativas y basura)."""
    if valor is None or valor == "":
        return []
    if isinstance(valor, (list, tuple)):
        return list(valor)
    if isinstance(valor, str):
        try:
            datos = json.loads(valor)
        except (TypeError, ValueError):
            return []
        return list(datos) if isinstance(datos, list) else []
    return []


def _entero(valor: Any, defecto: int) -> int:
    try:
        if valor is None or valor == "":
            return defecto
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _normalizar_plataforma(plataforma: Any) -> str:
    """Normaliza el query param ``plataforma`` (``mac_arm64`` -> ``mac-arm64``).

    Minúsculas, sin espacios y con ``-`` como separador. Vacío -> ``windows``
    (el .exe Windows nunca envía el parámetro). ``all`` se conserva para pedir
    el último release global. Nunca lanza.
    """
    texto = _texto(plataforma).strip().lower().replace("_", "-").replace(" ", "")
    return texto or "windows"


def _imagen_payload(item: Any) -> Optional[dict]:
    """Normaliza una imagen de `AppConfig.imagenes` al contrato de respuesta."""
    if isinstance(item, dict):
        nombre = str(item.get("nombre") or item.get("name") or "").strip()
    else:
        nombre = str(item or "").strip()
    if not nombre:
        return None
    return {"nombre": nombre, "url": f"/v1/imagenes/{quote(nombre, safe='')}"}


def _campana_payload(config: Optional[AppConfig]) -> dict:
    """Configuración de campaña del usuario (o defaults del modelo)."""
    if config is None:
        return {
            "hashtags": [],
            "contexto": "",
            "narrativa": "",
            "urls": [],
            "texto_base": "",
            "acciones_por_cuenta": 10,
            "pausa_seg": 2,
            "duracion_min": 60,
            "limite_dia": 49,
            "imagenes": [],
            "permitir_ajustes": False,
            "navegadores": 1,
            "sin_imagenes": True,
        }

    hashtags = [str(x).strip() for x in _lista_json(config.hashtags) if str(x).strip()]
    urls = [str(x).strip() for x in _lista_json(config.urls) if str(x).strip()]
    imagenes = []
    for item in _lista_json(config.imagenes):
        normalizada = _imagen_payload(item)
        if normalizada:
            imagenes.append(normalizada)

    sin_imagenes = getattr(config, "sin_imagenes", True)
    if sin_imagenes is None:
        sin_imagenes = True

    return {
        "hashtags": hashtags,
        "contexto": _texto(config.contexto),
        "narrativa": _texto(config.narrativa),
        "urls": urls,
        "texto_base": _texto(config.texto_base),
        "acciones_por_cuenta": _entero(config.acciones_por_cuenta, 10),
        "pausa_seg": _entero(config.pausa_seg, 2),
        "duracion_min": _entero(config.duracion_min, 60),
        "limite_dia": _entero(config.limite_dia, 49),
        "imagenes": imagenes,
        "permitir_ajustes": bool(config.permitir_ajustes),
        "navegadores": max(
            1, min(3, _entero(getattr(config, "navegadores", 1), 1))
        ),
        "sin_imagenes": bool(sin_imagenes),
    }


def _cuenta_payload(cuenta: Cuenta, incluir_password: bool) -> dict:
    """Cuenta de la flota en el formato del contrato (solo campos públicos)."""
    datos = {
        "usuario": _texto(cuenta.usuario),
        "nombre_mostrado": _texto(cuenta.nombre_mostrado),
        "handle_actual": _texto(cuenta.handle_actual),
        "seccion": _texto(cuenta.seccion),
        "tipo_cuenta": _texto(cuenta.tipo_cuenta),
        "perfil_personalidad": _texto(cuenta.perfil_personalidad),
        "personalidad": _texto(cuenta.personalidad),
        "tier_calidad": _texto(cuenta.tier_calidad),
        "totp_secret": _texto(cuenta.totp_secret),
        "auth_token": _texto(cuenta.auth_token),
        "cookies_json": _texto(cuenta.cookies_json),
        "user_agent": _texto(cuenta.user_agent),
    }
    if incluir_password:
        datos["password"] = _texto(cuenta.password)
    return datos


def _cuentas_del_usuario(db, usuario_id: int) -> list[Cuenta]:
    """Cuentas asignadas: solo twitter y activas (orden estable por usuario)."""
    filas = db.execute(
        select(Cuenta)
        .join(AppAsignacion, AppAsignacion.cuenta_id == Cuenta.id)
        .where(
            AppAsignacion.usuario_id == int(usuario_id),
            Cuenta.plataforma == "twitter",
            Cuenta.activa.is_(True),
        )
        .order_by(func.lower(Cuenta.usuario).asc())
    )
    return list(filas.scalars().all())


def _payload_sesion(db, usuario: AppUsuario, incluir_password: bool) -> dict:
    """Cuerpo completo de login/perfil (misma forma en ambos)."""
    config = (
        db.execute(select(AppConfig).where(AppConfig.usuario_id == int(usuario.id)))
        .scalars()
        .first()
    )
    return {
        "usuario": {
            "username": _texto(usuario.username),
            "nombre": _texto(usuario.nombre),
        },
        "campana": _campana_payload(config),
        "cuentas": [
            _cuenta_payload(cuenta, incluir_password)
            for cuenta in _cuentas_del_usuario(db, usuario.id)
        ],
    }


def _horas_token() -> int:
    """Duración del token en horas (env `MWX_TOKEN_HORAS`, default 12)."""
    try:
        horas = int(os.environ.get("MWX_TOKEN_HORAS", "12") or 12)
    except (TypeError, ValueError):
        horas = 12
    return min(max(horas, 1), 24 * 30)


def _expira_utc(expira: str) -> str:
    """Normaliza el ISO-8601 del token al formato UTC con `Z` (sin microsegundos)."""
    texto = str(expira or "")
    try:
        momento = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        momento = momento.astimezone(timezone.utc).replace(microsecond=0)
        return momento.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return texto


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    """Healthcheck público para Railway (sin auth y SIN tocar la BD).

    Responde siempre en menos de un segundo y NUNCA falla por una base de
    datos lenta o caída: solo confirma que el proceso de la API está vivo.
    """
    return {"ok": True, "servicio": "mwx-api", "version": "1.0"}


@app.get("/v1/app/version")
def app_version(plataforma: str = "windows", db=Depends(get_db)):
    """Último release publicado del ejecutable (público, sin Bearer).

    Query param ``plataforma`` (default ``windows``): los clientes Windows NO
    envían el parámetro; macOS envía ``mac-arm64`` o ``mac-intel`` (también se
    acepta ``mac`` y ``mac_arm64``/``mac_intel``). ``all`` devuelve el último
    global.

    Responde metadatos SIN cargar el ``contenido`` del ZIP (que puede pesar
    decenas de MB). Si no hay releases de esa plataforma devuelve 200
    ``{"ok": True, "disponible": False, "version": ""}``. El cliente compara
    ``version`` con la suya para decidir si debe actualizarse antes del login.
    """
    datos = app_releases.ultimo_release(_normalizar_plataforma(plataforma))
    if not datos.get("disponible"):
        return {"ok": True, "disponible": False, "version": ""}
    return {
        "ok": True,
        "disponible": True,
        "version": _texto(datos.get("version")),
        "obligatorio": bool(datos.get("obligatorio")),
        "notas": _texto(datos.get("notas")),
        "sha256": _texto(datos.get("sha256")),
        "tamano": _entero(datos.get("tamano"), 0),
        "filename": _texto(datos.get("filename")),
        "fecha": _texto(datos.get("fecha")),
    }


@app.get("/v1/app/descargar")
def app_descargar(
    version: str = "",
    plataforma: str = "windows",
    db=Depends(get_db),
):
    """Descarga el ZIP de un release (público, sin Bearer).

    ``?version=`` vacío devuelve el release más reciente DE ``plataforma``
    (default ``windows``: el .exe Windows no envía el parámetro; macOS envía
    ``mac-arm64``/``mac-intel``). Una ``version`` explícita manda y la
    plataforma se ignora (la versión ya es única e incluye el sufijo).

    Responde 200 con ``application/zip`` y ``Content-Disposition: attachment``;
    404 uniforme ``{"ok": false, "error": "actualización no disponible"}`` si no
    existe. Nunca registra los bytes del paquete.
    """
    version_txt = _texto(version).strip()
    if version_txt:
        datos = app_releases.leer_release(version_txt)
    else:
        datos = app_releases.leer_release(
            "", _normalizar_plataforma(plataforma)
        )
    contenido = datos.get("contenido") or b""
    if not datos.get("disponible") or not contenido:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="actualización no disponible",
        )
    nombre = (
        _texto(datos.get("filename"))
        or f"MWX-APP-{_texto(datos.get('version')) or 'release'}.zip"
    )
    nombre = nombre.replace('"', "").replace("\r", "").replace("\n", "")[:200]
    return Response(
        content=contenido,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre}"',
            "Content-Length": str(len(contenido)),
            "Cache-Control": "no-store",
        },
    )


@app.post("/v1/login")
def login(datos: LoginRequest, request: Request, db=Depends(get_db)):
    """Inicia sesión del ejecutable y devuelve token + campaña + cuentas.

    Body JSON: ``{"username", "password", "machine_id"}``.

    200: token HMAC, expiración, usuario, campaña y cuentas asignadas
    (``activa=True``, plataforma twitter). 401 credenciales incorrectas,
    403 usuario desactivado y 429 demasiados intentos (username + IP).
    Registra un ``AppEvento`` tipo ``login`` con el ``machine_id`` (sin
    credenciales).
    """
    ip = request.client.host if request.client else "desconocida"
    nombre = (datos.username or "").strip()
    verificar_rate_limit(nombre, ip)

    usuario = (
        db.execute(
            select(AppUsuario).where(
                func.lower(AppUsuario.username) == nombre.lower()
            )
        )
        .scalars()
        .first()
    )
    if usuario is None or not verify_password(datos.password, usuario.password_hash):
        registrar_intento_fallido(nombre, ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="usuario o contraseña incorrectos",
        )
    if not bool(usuario.activo):
        registrar_intento_fallido(nombre, ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="cuenta desactivada",
        )

    limpiar_intentos(nombre, ip)
    sesion = crear_token_app(usuario, horas=_horas_token())

    try:
        usuario.ultimo_acceso = datetime.utcnow()
        db.add(
            AppEvento(
                usuario_id=usuario.id,
                tipo="login",
                detalle=(
                    f"machine_id={str(datos.machine_id or '')[:200]} "
                    f"ip={str(ip)[:64]}"
                ),
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.error("No se pudo registrar el evento de login del MWX APP")

    cuerpo = _payload_sesion(db, usuario, incluir_password=True)
    return {
        "ok": True,
        "token": sesion["token"],
        "expira": _expira_utc(sesion["expira"]),
        "usuario": cuerpo["usuario"],
        "campana": cuerpo["campana"],
        "cuentas": cuerpo["cuentas"],
    }


@app.get("/v1/perfil")
def obtener_perfil(
    usuario: AppUsuario = Depends(obtener_usuario_actual),
    db=Depends(get_db),
):
    """Devuelve el MISMO payload que login pero SIN ``password`` en cuentas.

    Requiere ``Authorization: Bearer <token>``. Revalida usuario existente,
    activo y ``token_version`` (401/403 con mensajes claros si ya no vale).
    Incluye los secretos de operación necesarios: ``totp_secret``,
    ``auth_token``, ``cookies_json`` y ``user_agent``.
    """
    cuerpo = _payload_sesion(db, usuario, incluir_password=False)
    return {"ok": True, **cuerpo}


@app.post("/v1/estado")
def estado_sesion(usuario: AppUsuario = Depends(obtener_usuario_actual)):
    """Comprueba que el token y el usuario siguen vigentes antes de campaña.

    Requiere Bearer. 200 ``{"ok", "activo", "nombre"}``; 401 si el token está
    vencido/revocado y 403 si el usuario fue desactivado.
    """
    return {"ok": True, "activo": True, "nombre": _texto(usuario.nombre)}


@app.get("/v1/imagenes/{nombre:path}")
def obtener_imagen(
    nombre: str,
    usuario: AppUsuario = Depends(obtener_usuario_actual),
    db=Depends(get_db),
):
    """Sirve una imagen de campaña del usuario autenticado (requiere Bearer).

    La API corre en un contenedor SEPARADO del dashboard y no comparte disco,
    por eso el contenido se busca PRIMERO en la base de datos
    (`core.app_imagenes.leer_imagen`) y, solo si no está ahí, se usa el archivo
    local ``data/mwx_app/{username}/imagenes/{nombre}`` (compatibilidad).

    El nombre se valida con ``core.app_imagenes.nombre_seguro`` (rechaza
    traversal, espacios, acentos y cualquier cosa fuera de
    ``[A-Za-z0-9._-]{1,120}``). Devuelve 404 uniforme si el nombre es inválido
    o la imagen no existe en BD ni en disco. Nunca registra los bytes.
    """
    texto = nombre_seguro(nombre)
    if not texto:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="imagen no encontrada"
        )

    imagen = leer_imagen(db, usuario.id, texto)
    if imagen is not None:
        contenido = imagen.get("contenido") or b""
        tipo_bd = str(
            imagen.get("content_type") or "application/octet-stream"
        )
        return Response(
            content=contenido,
            media_type=tipo_bd,
            headers={
                "Content-Disposition": f'inline; filename="{texto}"',
                "Cache-Control": "private, max-age=300",
            },
        )

    base = Path(
        resolver_ruta(f"data/mwx_app/{_texto(usuario.username)}/imagenes")
    ).resolve()
    ruta = Path(
        resolver_ruta(
            f"data/mwx_app/{_texto(usuario.username)}/imagenes/{texto}"
        )
    ).resolve()
    try:
        ruta.relative_to(base)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="imagen no encontrada"
        )
    if not ruta.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="imagen no encontrada"
        )

    tipo = mimetypes.guess_type(str(ruta))[0] or "application/octet-stream"
    return FileResponse(str(ruta), media_type=tipo)


@app.post("/v1/chat/completions")
async def chat_completions(
    payload: dict[str, Any] = Body(...),
    _usuario: AppUsuario = Depends(obtener_usuario_actual),
):
    """Proxy OpenAI Chat Completions usando la key REAL del servidor.

    Requiere Bearer. Reenvía el body a
    ``https://api.openai.com/v1/chat/completions`` y devuelve status+JSON del
    upstream. Solo permite ``model`` en {gpt-4o-mini, gpt-4o, gpt-4.1-mini,
    gpt-4.1} y ``max_tokens`` <= 4096 (400 si no). 503 si falta
    ``OPENAI_API_KEY``; timeout de 60 s. El cliente usa
    ``OPENAI_API_BASE=https://<host>/v1`` sin conocer la key real.
    """
    modelo = payload.get("model")
    if not isinstance(modelo, str) or modelo not in MODELOS_OPENAI_PERMITIDOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="modelo no permitido",
        )

    if "max_tokens" in payload:
        max_tokens = payload.get("max_tokens")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
            or max_tokens > MAX_TOKENS_OPENAI
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="max_tokens debe ser un entero entre 1 y 4096",
            )

    if payload.get("stream") is True:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stream no soportado",
        )

    api_key = _texto(getattr(settings, "openai_api_key", "")).strip()
    if not api_key or api_key == _CENTINELA_SIN_OPENAI:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="servicio de IA no configurado",
        )

    cabeceras = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_OPENAI_SEG) as cliente:
            respuesta = await cliente.post(
                OPENAI_CHAT_URL, headers=cabeceras, json=payload
            )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="tiempo de espera agotado al contactar el servicio de IA",
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="error al contactar el servicio de IA",
        )

    try:
        contenido = respuesta.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="respuesta inválida del servicio de IA",
        )
    return JSONResponse(status_code=respuesta.status_code, content=contenido)


@app.post("/v1/evento")
def registrar_evento(
    datos: EventoRequest,
    usuario: AppUsuario = Depends(obtener_usuario_actual),
    db=Depends(get_db),
):
    """Guarda un ``AppEvento`` del cliente (requiere Bearer).

    Body ``{"tipo", "detalle"}``; el detalle se trunca a 2000 caracteres.
    Devuelve 200 ``{"ok": true}``.
    """
    evento = AppEvento(
        usuario_id=usuario.id,
        tipo=(datos.tipo or "").strip()[:50],
        detalle=(datos.detalle or "")[:2000],
    )
    try:
        db.add(evento)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="no se pudo guardar el evento",
        )
    return {"ok": True}
