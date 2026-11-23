# -*- coding: utf-8 -*-
"""Esquemas Pydantic v2 de la API del MWX APP.

Solo se modelan los cuerpos de entrada. Las respuestas siguen el contrato
exacto documentado en `api/main.py` (dicts JSON) para no inventar campos.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """Credenciales del ejecutable + identificador de la máquina."""

    model_config = ConfigDict(extra="ignore")

    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)
    machine_id: str = Field(default="", max_length=300)


class EventoRequest(BaseModel):
    """Evento de auditoría enviado por el cliente (sin credenciales)."""

    model_config = ConfigDict(extra="ignore")

    tipo: str = Field(..., min_length=1, max_length=200)
    detalle: str = Field(default="", max_length=10000)
