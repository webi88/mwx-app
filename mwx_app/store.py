# -*- coding: utf-8 -*-
"""Sincronizacion del perfil del servidor con la SQLite local.

La API es la fuente de verdad para la app: las cuentas asignadas al usuario se
guardan/actualizan en la base local que ya usa `core.database` y las demas
cuentas de Twitter del equipo quedan INACTIVAS (nunca se borran registros).
"""
from __future__ import annotations

import json
from typing import Any, Optional

_CAMPOS_TEXTO = (
    "totp_secret",
    "auth_token",
    "user_agent",
    "personalidad",
    "nombre_mostrado",
    "handle_actual",
)


def _texto(valor: Any) -> str:
    if valor is None:
        return ""
    if isinstance(valor, str):
        return valor.strip()
    return str(valor).strip()


def _normalizar_seccion(valor: Any) -> str:
    try:
        from core.secciones import normalizar_seccion

        return normalizar_seccion(valor) or ""
    except Exception:
        return _texto(valor)


def _normalizar_tipo_cuenta(valor: Any) -> str:
    try:
        from core.registros import normalizar_tipo_cuenta

        return normalizar_tipo_cuenta(valor) or ""
    except Exception:
        return _texto(valor)


def _normalizar_perfil(valor: Any) -> str:
    try:
        from core.perfiles import normalizar_perfil

        return normalizar_perfil(valor) or ""
    except Exception:
        return _texto(valor)


def _normalizar_tier(valor: Any) -> str:
    try:
        from core.tiers import normalizar_tier

        return normalizar_tier(valor) or ""
    except Exception:
        return _texto(valor)


def _parsear_cookies(valor: Any) -> Optional[Any]:
    """Convierte el string JSON de la API al objeto que guarda la columna JSON."""
    if valor is None:
        return None
    if isinstance(valor, (list, dict)):
        return valor
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        datos = json.loads(texto)
    except (ValueError, TypeError):
        return None
    if isinstance(datos, dict) and "cookies" in datos:
        return datos
    if isinstance(datos, (list, dict)):
        return datos
    return None


def extraer_cuentas(datos: Any) -> Optional[list]:
    """Lista de cuentas del payload login/perfil (``None`` si no viene)."""
    if isinstance(datos, list):
        return list(datos)
    if not isinstance(datos, dict):
        return None
    if isinstance(datos.get("perfil"), dict):
        datos = datos["perfil"]
    cuentas = datos.get("cuentas")
    if cuentas is None:
        return None
    if isinstance(cuentas, list):
        return list(cuentas)
    return None


def _actualizar_cuenta(cuenta: Any, item: dict) -> None:
    """Aplica los campos del contrato a una `Cuenta` (sin tocar pausas/status)."""
    for campo in _CAMPOS_TEXTO:
        if campo in item:
            setattr(cuenta, campo, _texto(item.get(campo)))

    if "password" in item:
        cuenta.password = _texto(item.get("password"))
    if "seccion" in item:
        cuenta.seccion = _normalizar_seccion(item.get("seccion"))
    if "tipo_cuenta" in item:
        cuenta.tipo_cuenta = _normalizar_tipo_cuenta(item.get("tipo_cuenta"))
    if "perfil_personalidad" in item:
        cuenta.perfil_personalidad = _normalizar_perfil(
            item.get("perfil_personalidad")
        )
    if "tier_calidad" in item:
        cuenta.tier_calidad = _normalizar_tier(item.get("tier_calidad"))
    if "cookies_json" in item:
        cookies = _parsear_cookies(item.get("cookies_json"))
        cuenta.cookies_json = cookies
        if cookies:
            cuenta.cookies_path = ""


def sincronizar_perfil(
    datos: Any, session_factory: Optional[Any] = None
) -> list:
    """Guarda las cuentas asignadas y devuelve sus usuarios en orden.

    ``session_factory`` permite inyectar una sesion (tests). Sin el se usa la
    base real del proyecto (`core.database`) y se garantiza que las tablas
    existan. Nunca borra cuentas ni registros de acciones.
    """
    cuentas_perfil = extraer_cuentas(datos)
    if cuentas_perfil is None:
        raise ValueError("El servidor no envió la lista de cuentas.")

    if session_factory is None:
        from core.database import get_db_session, init_db

        init_db()
        abrir_sesion = get_db_session
    else:
        abrir_sesion = session_factory

    try:
        from core.models import Cuenta
    except Exception as error:
        raise RuntimeError("No se pudo preparar la base de datos local.") from error

    asignados: list = []
    vistos: set = set()
    with abrir_sesion() as db:
        existentes = {
            str(cuenta.usuario or "").lower(): cuenta
            for cuenta in db.query(Cuenta).all()
        }

        for item in cuentas_perfil:
            if not isinstance(item, dict):
                continue
            usuario = _texto(item.get("usuario")).lstrip("@")
            if not usuario:
                continue
            clave = usuario.lower()
            cuenta = existentes.get(clave)
            if cuenta is None:
                cuenta = Cuenta(usuario=usuario, plataforma="twitter")
                db.add(cuenta)
                existentes[clave] = cuenta
            elif cuenta.plataforma != "twitter":
                cuenta.plataforma = "twitter"
            _actualizar_cuenta(cuenta, item)
            cuenta.activa = True
            if hasattr(cuenta, "pausada_activacion"):
                cuenta.pausada_activacion = False
            if clave not in vistos:
                vistos.add(clave)
                asignados.append(usuario)

        db.flush()

        for cuenta in (
            db.query(Cuenta).filter(Cuenta.plataforma == "twitter").all()
        ):
            if str(cuenta.usuario or "").lower() not in vistos:
                cuenta.activa = False

    return asignados
