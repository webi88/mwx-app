# -*- coding: utf-8 -*-
"""Clasificacion de cuentas por seccion.

Tres secciones operativas:
    - CI: Centro-Izquierda
    - CD: Centro-Derecha
    - IP: Institucion Privada

Interfaz congelada: otros modulos (web, bot, cuentas, exportacion) importan
SECCIONES, normalizar_seccion(), etiqueta_seccion() y seccion_desde_sector().
Sin dependencias externas.
"""
import unicodedata

# Codigo canonico -> nombre legible de la seccion.
SECCIONES = {
    "CI": "Centro-Izquierda",
    "CD": "Centro-Derecha",
    "IP": "Institución Privada",
}


def _normalizar_texto(valor) -> str:
    """Pasa un valor a minusculas sin acentos y sin separadores.

    "Institución Privada" -> "institucionprivada"; "centro-izquierda" ->
    "centroizquierda". Acepta None sin romper (devuelve "")."""
    if valor is None:
        return ""
    texto = str(valor).strip().lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    for separador in (" ", "-", "_", ".", "/"):
        texto = texto.replace(separador, "")
    return texto


def normalizar_seccion(valor) -> str:
    """Devuelve el codigo canonico de seccion ("CI", "CD" o "IP").

    Acepta variantes como "ci", "CI", "CentroIzquierda", "centro-izquierda",
    "izquierda" (-> "CI"); "cd", "centroderecha", "derecha" (-> "CD"); "ip",
    "privada", "institucion privada", "privados" (-> "IP"). Cualquier otro
    valor, vacio o None devuelve "" (sin asignar)."""
    clave = _normalizar_texto(valor)
    if not clave:
        return ""
    if clave == "ci" or "izquierd" in clave:
        return "CI"
    if clave == "cd" or "derech" in clave:
        return "CD"
    if clave == "ip" or "privad" in clave or "institucion" in clave:
        return "IP"
    return ""


def etiqueta_seccion(valor) -> str:
    """Devuelve la etiqueta legible de una seccion.

    "CI" -> "CI — Centro-Izquierda"; "CD" -> "CD — Centro-Derecha";
    "IP" -> "IP — Institución Privada"; "" -> "Sin asignar"."""
    clave = normalizar_seccion(valor)
    if not clave:
        return "Sin asignar"
    return f"{clave} — {SECCIONES[clave]}"


def seccion_desde_sector(sector) -> str:
    """Mapea el campo libre 'sector' de la cuenta a una seccion canonica.

    centroderecha -> "CD"; centroizquierda -> "CI"; privados -> "IP";
    cualquier otro valor -> ""."""
    return normalizar_seccion(sector)
