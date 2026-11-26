# -*- coding: utf-8 -*-
"""Configuracion local del ejecutable MWX APP.

Resuelve la URL de la API y guarda los datos que pertenecen a ESTA maquina:

- ``config.json`` junto al .exe (o en la raiz del proyecto en desarrollo).
- ``DATA_DIR/data/mwx_machine.json`` con un UUID estable por equipo.
- ``DATA_DIR/data/mwx_sesion.json`` con token + usuario (NUNCA la contrasena).
- ``DATA_DIR/data/mwx_recordar.json`` con el ultimo usuario recordado.

Prioridad de la URL: ``MWX_API_URL`` > ``config.json`` > ``settings.mwx_api_url``.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

APP_NOMBRE = "MWX APP"
ARCHIVO_CONFIG = "config.json"
ARCHIVO_MAQUINA = "mwx_machine.json"
ARCHIVO_SESION = "mwx_sesion.json"
ARCHIVO_RECORDAR = "mwx_recordar.json"
SESION_HORAS = 12


def _congelado() -> bool:
    """True cuando el codigo corre dentro de un ejecutable PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def _dir_base() -> Path:
    """Carpeta escribible de configuracion (o la raiz del repo en desarrollo).

    En macOS congelado se usa ``~/.mwx_app`` (misma carpeta que ``DATA_DIR``)
    porque el ``.app`` va firmado y no se debe escribir dentro del bundle.
    """
    if _congelado():
        if sys.platform == "darwin":
            try:
                return Path.home() / ".mwx_app"
            except Exception:
                return Path("~").expanduser() / ".mwx_app"
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            return Path.cwd()
    return Path(__file__).resolve().parent.parent


def _configs_empaquetados() -> list:
    """Rutas alternativas de ``config.json`` dentro/junto al ``.app`` (macOS).

    En macOS el ejecutable real vive en ``Contents/MacOS``; el ZIP de
    distribucion puede traer ``config.json`` en ``Contents/MacOS``, en
    ``Contents/Resources``, en la raiz del ``.app`` o justo al lado del
    ``.app``. En desarrollo o en otras plataformas la lista queda vacia
    (comportamiento historico intacto).
    """
    if not _congelado() or sys.platform != "darwin":
        return []
    try:
        ejecutable = Path(sys.executable).resolve()
    except Exception:
        return []
    return [
        ejecutable.parent / ARCHIVO_CONFIG,
        ejecutable.parent.parent / "Resources" / ARCHIVO_CONFIG,
        ejecutable.parent.parent.parent / ARCHIVO_CONFIG,
        ejecutable.parent.parent.parent.parent / ARCHIVO_CONFIG,
    ]


def ruta_config() -> Path:
    """Ruta absoluta de ``config.json``."""
    return _dir_base() / ARCHIVO_CONFIG


def normalizar_url_api(valor: Any) -> str:
    """Normaliza la URL base: sin barra final y sin un ``/v1`` terminal."""
    texto = str(valor or "").strip()
    if not texto:
        return ""
    if not texto.lower().startswith(("http://", "https://")):
        texto = "https://" + texto.lstrip("/")
    texto = texto.rstrip("/")
    if texto.lower().endswith("/v1"):
        texto = texto[:-3].rstrip("/")
    return texto


def _leer_json(ruta: Path) -> dict:
    """Lee un JSON de disco; devuelve ``{}`` ante cualquier problema."""
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        return datos if isinstance(datos, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _escribir_json(ruta: Path, datos: dict) -> bool:
    """Escribe un JSON de forma atomica; nunca lanza."""
    ruta = Path(ruta)
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal = ruta.with_name(ruta.name + ".tmp")
        temporal.write_text(
            json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporal, ruta)
        return True
    except OSError:
        try:
            ruta.write_text(
                json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except OSError:
            return False


def api_url() -> str:
    """URL base de la API segun la prioridad documentada (nunca lanza)."""
    desde_entorno = normalizar_url_api(os.environ.get("MWX_API_URL", ""))
    if desde_entorno:
        return desde_entorno

    desde_archivo = normalizar_url_api(
        _leer_json(ruta_config()).get("api_url", "")
    )
    if desde_archivo:
        return desde_archivo

    for candidata in _configs_empaquetados():
        desde_archivo = normalizar_url_api(
            _leer_json(candidata).get("api_url", "")
        )
        if desde_archivo:
            return desde_archivo

    try:
        from core.config import settings

        return normalizar_url_api(getattr(settings, "mwx_api_url", "") or "")
    except Exception:
        return ""


def guardar_api_url(valor: Any) -> str:
    """Guarda la URL en ``config.json``. Devuelve la URL normalizada o ``""``."""
    normalizada = normalizar_url_api(valor)
    if not normalizada:
        return ""
    if _escribir_json(ruta_config(), {"api_url": normalizada}):
        return normalizada
    return ""


def _dir_datos() -> Path:
    """Carpeta de ESCRITURA de la app (DATA_DIR o ``%LOCALAPPDATA%/MWX APP``)."""
    try:
        from core.config import settings

        base = getattr(settings, "data_dir", "") or ""
        if base:
            return Path(base).expanduser()
    except Exception:
        pass

    desde_entorno = (os.environ.get("MWX_DATA_DIR") or "").strip()
    if desde_entorno:
        return Path(desde_entorno).expanduser()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NOMBRE
        return Path.home() / "AppData" / "Local" / APP_NOMBRE
    return Path.home() / ".mwx_app"


def dir_datos() -> Path:
    """Carpeta raiz de escritura de la app (``DATA_DIR``)."""
    return _dir_datos()


def ruta_datos(nombre: str) -> Path:
    """Ruta absoluta dentro de ``DATA_DIR/data`` (crea la carpeta padre)."""
    destino = _dir_datos() / "data" / str(nombre)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return destino


def _uuid_valido(valor: str) -> bool:
    try:
        uuid.UUID(str(valor))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def machine_id() -> str:
    """UUID estable de esta maquina (crea el archivo la primera vez)."""
    ruta = ruta_datos(ARCHIVO_MAQUINA)
    valor = str(_leer_json(ruta).get("machine_id") or "").strip()
    if _uuid_valido(valor):
        return valor

    nuevo = str(uuid.uuid4())
    _escribir_json(ruta, {"machine_id": nuevo})
    return nuevo


def cargar_sesion() -> dict:
    """Sesion persistida y vigente (< 12 h) o ``{}`` si no sirve."""
    datos = _leer_json(ruta_datos(ARCHIVO_SESION))
    token = str(datos.get("token") or "").strip()
    usuario = str(datos.get("usuario") or "").strip()
    if not token or not usuario:
        return {}
    try:
        guardado = float(datos.get("guardado") or 0)
    except (TypeError, ValueError):
        guardado = 0.0
    if guardado <= 0 or (time.time() - guardado) > (SESION_HORAS * 3600):
        return {}
    return {
        "token": token,
        "usuario": usuario,
        "nombre": str(datos.get("nombre") or usuario),
        "guardado": guardado,
    }


def guardar_sesion(token: str, usuario: str, nombre: str = "") -> bool:
    """Guarda token + usuario (sin contrasena) para reconectar dentro de 12 h."""
    token = str(token or "").strip()
    usuario = str(usuario or "").strip()
    if not token or not usuario:
        return False
    return _escribir_json(
        ruta_datos(ARCHIVO_SESION),
        {
            "token": token,
            "usuario": usuario,
            "nombre": str(nombre or usuario),
            "guardado": time.time(),
        },
    )


def borrar_sesion() -> None:
    """Elimina la sesion persistida (nunca lanza)."""
    try:
        ruta_datos(ARCHIVO_SESION).unlink(missing_ok=True)
    except OSError:
        pass


def cargar_usuario_recordado() -> str:
    """Usuario del ultimo login exitoso (solo el nombre, sin secretos)."""
    return str(_leer_json(ruta_datos(ARCHIVO_RECORDAR)).get("usuario") or "").strip()


def guardar_usuario_recordado(usuario: str) -> None:
    """Recuerda (o borra, con texto vacio) el usuario de la pantalla de login."""
    usuario = str(usuario or "").strip()
    if not usuario:
        try:
            ruta_datos(ARCHIVO_RECORDAR).unlink(missing_ok=True)
        except OSError:
            pass
        return
    _escribir_json(ruta_datos(ARCHIVO_RECORDAR), {"usuario": usuario})
