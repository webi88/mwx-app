# -*- coding: utf-8 -*-
"""Clasificacion de cuentas por tipo de voz/registro de lenguaje.

Tres tipos operativos:
    - politica:  lenguaje formal impecable (cuenta oficial o vocero).
    - activista: lenguaje tecnico-coloquial; ciudadania que apoya a la
      politica, usa terminos politicos con lenguaje popular y pequenas
      faltas de ortografia ocasionales.
    - ciudadana: persona real, lenguaje coloquial con mas faltas.

Interfaz congelada: otros modulos (web, bot, cuentas, ia) importan
TIPOS_CUENTA, normalizar_tipo_cuenta() y etiqueta_tipo_cuenta().
Sin dependencias externas. La normalizacion de acentos reutiliza el mismo
enfoque de core/secciones.py para que ambas clasificaciones sean coherentes.
"""
from core.secciones import _normalizar_texto

# Codigo canonico -> nombre legible del tipo de cuenta.
TIPOS_CUENTA = {
    "politica": "Política / Institucional",
    "activista": "Activista / Técnico-coloquial",
    "ciudadana": "Ciudadana / Persona real",
}

# Sinonimos EXACTOS (ya normalizados: minusculas, sin acentos ni separadores).
_POLITICA_EXACTOS = ("politica", "politico", "institucional", "formal", "oficial")
_ACTIVISTA_EXACTOS = (
    "activista",
    "activistatecnicocoloquial",  # round-trip de la etiqueta de TIPOS_CUENTA
    "militante",
    "simpatizante",
    "tecnico",
    "tecnicocoloquial",
    "ciudadaniapolitica",
    "ciudadanopolitico",
    "conjunto",
    "deconjunto",
)
_CIUDADANA_EXACTOS = (
    "ciudadana",
    "ciudadano",
    "ciudadania",
    "ciudadanas",
    "ciudadanos",
    "real",
    "persona",
    "informal",
    "coloquial",
)


def normalizar_tipo_cuenta(valor) -> str:
    """Devuelve el codigo canonico del tipo de cuenta.

    Valores posibles: "politica", "activista", "ciudadana" o "" (sin definir).

    Orden de prioridad (importante): primero los sinonimos EXACTOS de
    "activista" y "politica", y solo despues los prefijos. Asi
    "ciudadania politica" y "ciudadano politico" caen en "activista" y NO en
    "ciudadana" (empiezan con "ciudadan") ni en "politica" (contienen
    "politic").     Reglas:
        - "politica", "politico", "institucional", "formal", "oficial"
          (exactos) -> "politica".
        - "activista", "militante", "simpatizante", "tecnico",
          "tecnico-coloquial", "ciudadania politica", "ciudadano politico",
          "conjunto", "de conjunto" (exactos) -> "activista".
        - cualquier otra que empiece con "politic" -> "politica".
        - cualquier otra que empiece con "ciudadan" ("ciudadano",
          "ciudadana", "ciudadania", ...) -> "ciudadana".
        - "real", "persona", "informal", "coloquial" -> "ciudadana".
    Cualquier otro valor, vacio o None devuelve "" (sin definir)."""
    clave = _normalizar_texto(valor)
    if not clave:
        return ""

    # 1) Exactos de politica (formal/institucional).
    if clave in _POLITICA_EXACTOS:
        return "politica"

    # 2) Exactos de activista ANTES de evaluar prefijos: "ciudadania politica"
    #    empieza con "ciudadan" y acabaria mal como "ciudadana".
    if clave in _ACTIVISTA_EXACTOS:
        return "activista"

    # 3) Prefijos generales.
    if clave.startswith("activis"):
        return "activista"
    if clave.startswith("politic"):
        return "politica"
    if clave.startswith("ciudadan"):
        return "ciudadana"

    # 4) Sinonimos exactos de ciudadana.
    if clave in _CIUDADANA_EXACTOS:
        return "ciudadana"

    return ""


def etiqueta_tipo_cuenta(valor) -> str:
    """Devuelve la etiqueta legible de un tipo de cuenta.

    "politica" -> "Política / Institucional";
    "activista" -> "Activista / Técnico-coloquial";
    "ciudadana" -> "Ciudadana / Persona real"; "" -> "Sin definir"."""
    clave = normalizar_tipo_cuenta(valor)
    if not clave:
        return "Sin definir"
    return TIPOS_CUENTA[clave]
