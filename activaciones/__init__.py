# -*- coding: utf-8 -*-
"""Paquete de activaciones.

El motor masivo (`activaciones.motor`, ~343 KB, con cuotas/tiers/curva) NO se
importa al cargar el paquete: la campana LOCAL (`activaciones.local`) debe
poder usarse desde el ejecutable MWX APP (PyInstaller) sin arrastrarlo.

`from activaciones import MotorActivacion` sigue funcionando gracias a PEP 562
(`__getattr__` perezoso): el modulo del motor se carga solo cuando alguien pide
ese atributo.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # solo para type checkers; nunca se ejecuta en runtime
    from activaciones.motor import MotorActivacion

__all__ = ["MotorActivacion", "variar_texto", "generar_pool_variaciones"]


def __getattr__(nombre: str):
    """Import perezoso de los simbolos publicos del paquete (PEP 562)."""
    if nombre == "MotorActivacion":
        from activaciones.motor import MotorActivacion

        return MotorActivacion
    if nombre == "variar_texto":
        from activaciones.variaciones import variar_texto

        return variar_texto
    if nombre == "generar_pool_variaciones":
        from activaciones.variaciones import generar_pool_variaciones

        return generar_pool_variaciones
    raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
