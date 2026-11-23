# -*- coding: utf-8 -*-
"""Compatibilidad de importacion: `core.auth`.

El codigo historico/documentacion referenciaba ``core.auth`` para la
autenticacion multi-usuario, pero en este repositorio esa logica vivia en
``web/auth.py`` (dashboard) y en ``bot/middlewares.py`` (Telegram). La
autenticacion del ejecutable MWX APP se implementa en ``core.app_auth``.

Este modulo solo REEXPORTA la API publica para que ``import core.auth`` (y
``from core.auth import ...``) siga funcionando sin duplicar codigo. No se
importa desde ningun flujo critico.
"""
from __future__ import annotations

from core.app_auth import (
    autenticar_app,
    crear_token_app,
    hash_password,
    revocar_sesiones,
    verificar_token_app,
    verify_password,
)

__all__ = [
    "autenticar_app",
    "crear_token_app",
    "hash_password",
    "revocar_sesiones",
    "verificar_token_app",
    "verify_password",
]
