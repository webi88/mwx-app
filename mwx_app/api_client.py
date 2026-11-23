# -*- coding: utf-8 -*-
"""Cliente HTTP del ejecutable MWX APP.

Consume la API de `api/` (login, perfil, estado, imagenes, eventos) y traduce
TODO error a un mensaje en espanol listo para mostrar en la interfaz. Nunca
propaga tracebacks ni registra tokens/contrasenas/cookies.
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote

import requests

from mwx_app import __version__ as APP_VERSION
from mwx_app.config import normalizar_url_api

TIMEOUT_LOGIN = 20
TIMEOUT_IMAGEN = 60
TIMEOUT_EVENTO = 8

MENSAJE_SIN_CONEXION = (
    "No hay conexión con el servidor. Revisa tu internet e inténtalo de nuevo."
)
MENSAJE_CREDENCIALES = "Usuario o contraseña incorrectos."
MENSAJE_DESACTIVADA = "Tu cuenta fue desactivada. Contacta a tu administrador."
MENSAJE_SESION = "Tu sesión expiró, vuelve a entrar."
MENSAJE_INTENTOS = "Demasiados intentos. Espera unos minutos e inténtalo de nuevo."
MENSAJE_SERVIDOR = (
    "El servidor no está disponible en este momento. Inténtalo de nuevo en unos minutos."
)
MENSAJE_RESPUESTA = (
    "El servidor devolvió una respuesta inesperada. Inténtalo de nuevo en unos minutos."
)
MENSAJE_SERVIDOR_SIN_CONFIGURAR = (
    "Falta configurar el servidor. Pídele la dirección a tu administrador."
)


class MWXAPIError(Exception):
    """Error de la API con un mensaje ya listo para la interfaz."""

    def __init__(self, mensaje: str, codigo: int = 0):
        super().__init__(mensaje)
        self.mensaje = str(mensaje or MENSAJE_SERVIDOR)
        self.codigo = int(codigo or 0)

    def __str__(self) -> str:
        return self.mensaje


class SinConexionError(MWXAPIError):
    """No se pudo contactar al servidor (sin red, DNS, timeout, etc.)."""


class CredencialesError(MWXAPIError):
    """Usuario o contrasena incorrectos."""

    def __init__(self):
        super().__init__(MENSAJE_CREDENCIALES, 401)


class CuentaDesactivadaError(MWXAPIError):
    """La cuenta del usuario fue desactivada por el administrador."""

    def __init__(self):
        super().__init__(MENSAJE_DESACTIVADA, 403)


class SesionExpiradaError(MWXAPIError):
    """El token ya no es valido (vencido o revocado)."""

    def __init__(self):
        super().__init__(MENSAJE_SESION, 401)


class DemasiadosIntentosError(MWXAPIError):
    """Rate limit de login."""

    def __init__(self):
        super().__init__(MENSAJE_INTENTOS, 429)


class ServidorError(MWXAPIError):
    """Respuesta invalida o fallo del servidor."""

    def __init__(self, mensaje: str = MENSAJE_SERVIDOR, codigo: int = 500):
        super().__init__(mensaje or MENSAJE_SERVIDOR, codigo)


def mensaje_amigable(error: Any) -> str:
    """Convierte cualquier excepcion en un texto seguro para el usuario."""
    if isinstance(error, MWXAPIError):
        return error.mensaje
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return MENSAJE_SIN_CONEXION
    return "Ocurrió un problema inesperado. Inténtalo de nuevo."


def _detalle_error(respuesta: Any) -> str:
    """Texto corto del error del servidor (``detail``/``error``)."""
    try:
        datos = respuesta.json()
    except Exception:
        return ""
    if isinstance(datos, dict):
        for clave in ("detail", "error", "message"):
            valor = datos.get(clave)
            if isinstance(valor, str) and valor.strip():
                return valor.strip()
            if isinstance(valor, dict):
                interno = valor.get("message") or valor.get("detail")
                if isinstance(interno, str) and interno.strip():
                    return interno.strip()
    return ""


def _error_http(respuesta: Any, contexto: str) -> MWXAPIError:
    """Traduce un status HTTP a un error tipado con mensaje claro."""
    codigo = int(getattr(respuesta, "status_code", 0) or 0)
    detalle = _detalle_error(respuesta).lower()

    if "desactivada" in detalle:
        return CuentaDesactivadaError()
    if codigo == 429 or "demasiados intentos" in detalle:
        return DemasiadosIntentosError()
    if codigo == 403:
        return CuentaDesactivadaError()
    if codigo == 401:
        if contexto == "login":
            return CredencialesError()
        return SesionExpiradaError()
    if codigo == 404 and contexto == "imagen":
        return MWXAPIError("La imagen no está disponible.", 404)
    if codigo >= 500:
        return ServidorError(MENSAJE_SERVIDOR, codigo)
    if codigo == 400:
        return ServidorError(
            "La configuración de la campaña no es válida. Contacta a tu administrador.",
            400,
        )
    return ServidorError(MENSAJE_SERVIDOR, codigo or 500)


class MWXClient:
    """Cliente minimo de la API MWX APP."""

    def __init__(
        self,
        api_url: str,
        timeout: int = TIMEOUT_LOGIN,
        session: Optional[Any] = None,
    ):
        self.api_url = normalizar_url_api(api_url)
        try:
            self.timeout = max(1, int(timeout))
        except (TypeError, ValueError):
            self.timeout = TIMEOUT_LOGIN
        self._session = session if session is not None else requests.Session()

    def _url(self, ruta: str) -> str:
        return f"{self.api_url}{ruta}"

    def _headers(self, token: str = "") -> dict:
        cabeceras = {
            "Accept": "application/json",
            "User-Agent": f"MWX-APP/{APP_VERSION} (Windows)",
        }
        token = str(token or "").strip()
        if token:
            cabeceras["Authorization"] = f"Bearer {token}"
        return cabeceras

    def _pedir(
        self,
        metodo: str,
        ruta: str,
        token: str = "",
        cuerpo: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> Any:
        if not self.api_url:
            raise SinConexionError(MENSAJE_SERVIDOR_SIN_CONFIGURAR, 0)
        try:
            respuesta = self._session.request(
                metodo,
                self._url(ruta),
                headers=self._headers(token),
                json=cuerpo,
                timeout=timeout or self.timeout,
            )
        except requests.Timeout:
            raise SinConexionError(MENSAJE_SIN_CONEXION, 0)
        except requests.RequestException:
            raise SinConexionError(MENSAJE_SIN_CONEXION, 0)
        except (OSError, ValueError):
            raise SinConexionError(MENSAJE_SIN_CONEXION, 0)
        return respuesta

    def _json(self, respuesta: Any, contexto: str) -> dict:
        codigo = int(getattr(respuesta, "status_code", 0) or 0)
        if codigo < 200 or codigo >= 300:
            raise _error_http(respuesta, contexto)
        try:
            datos = respuesta.json()
        except (ValueError, json.JSONDecodeError):
            raise ServidorError(MENSAJE_RESPUESTA, codigo)
        if not isinstance(datos, dict):
            raise ServidorError(MENSAJE_RESPUESTA, codigo)
        if datos.get("ok") is False:
            error = str(datos.get("error") or datos.get("detail") or "")
            if "intentos" in error.lower():
                raise DemasiadosIntentosError()
            if "desactivada" in error.lower():
                raise CuentaDesactivadaError()
            if "contraseña" in error.lower() or "credenciales" in error.lower():
                raise CredencialesError()
            if contexto != "login" and any(
                clave in error.lower() for clave in ("sesión", "sesion", "token")
            ):
                raise SesionExpiradaError()
            raise ServidorError(error or MENSAJE_SERVIDOR, codigo)
        return datos

    def login(
        self,
        username: str,
        password: str,
        machine_id: str = "",
        timeout: Optional[int] = None,
    ) -> dict:
        """``POST /v1/login`` con usuario, contrasena e identificador de equipo."""
        cuerpo = {
            "username": str(username or "").strip(),
            "password": str(password or ""),
            "machine_id": str(machine_id or ""),
        }
        respuesta = self._pedir(
            "POST", "/v1/login", cuerpo=cuerpo, timeout=timeout or TIMEOUT_LOGIN
        )
        datos = self._json(respuesta, "login")
        if not str(datos.get("token") or "").strip():
            raise ServidorError(MENSAJE_RESPUESTA, 200)
        return datos

    def perfil(self, token: str) -> dict:
        """``GET /v1/perfil`` (campaña + cuentas asignadas, sin contrasenas)."""
        respuesta = self._pedir("GET", "/v1/perfil", token=token)
        datos = self._json(respuesta, "perfil")
        if isinstance(datos.get("perfil"), dict) and "cuentas" not in datos:
            return datos["perfil"]
        return datos

    def estado(self, token: str) -> dict:
        """``POST /v1/estado``; tolera un backend que solo exponga GET."""
        respuesta = self._pedir("POST", "/v1/estado", token=token)
        if int(getattr(respuesta, "status_code", 0) or 0) == 405:
            respuesta = self._pedir("GET", "/v1/estado", token=token)
        datos = self._json(respuesta, "estado")
        if datos.get("activo") is False:
            raise CuentaDesactivadaError()
        return datos

    def descargar_imagen(self, token: str, nombre: str) -> bytes:
        """``GET /v1/imagenes/{nombre}`` devuelve el contenido binario."""
        nombre = str(nombre or "").strip()
        if not nombre:
            raise MWXAPIError("La imagen no está disponible.", 404)
        respuesta = self._pedir(
            "GET",
            f"/v1/imagenes/{quote(nombre, safe='')}",
            token=token,
            timeout=TIMEOUT_IMAGEN,
        )
        codigo = int(getattr(respuesta, "status_code", 0) or 0)
        if codigo < 200 or codigo >= 300:
            raise _error_http(respuesta, "imagen")
        contenido = getattr(respuesta, "content", b"") or b""
        if not contenido:
            raise ServidorError("La imagen llegó vacía. Inténtalo de nuevo.", codigo)
        return bytes(contenido)

    def evento(
        self,
        token: str,
        tipo: str,
        detalle: str = "",
        timeout: Optional[int] = None,
    ) -> bool:
        """``POST /v1/evento`` (auditoria best-effort)."""
        cuerpo = {
            "tipo": str(tipo or "")[:200],
            "detalle": str(detalle or "")[:10000],
        }
        respuesta = self._pedir(
            "POST",
            "/v1/evento",
            token=token,
            cuerpo=cuerpo,
            timeout=timeout or TIMEOUT_EVENTO,
        )
        datos = self._json(respuesta, "evento")
        return bool(datos.get("ok", True))
