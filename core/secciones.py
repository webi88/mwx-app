# -*- coding: utf-8 -*-
"""Clasificacion de cuentas por seccion.

Cuatro secciones operativas (dashboard y motor las consumen por codigo):
    - CI:  Ciudadanía
    - IP:  Institución Privada
    - LIB: Libertad
    - JUS: Justicia

Las secciones antiguas Centro-Izquierda/Centro-Derecha (CD) ya NO existen:
`normalizar_seccion("CD")`, "centroderecha", "centroizquierda", "izquierda" y
"derecha" devuelven "" (sin asignar). El campo heredado 'sector' se mapea con
`seccion_desde_sector()`.

Interfaz congelada: otros modulos (web, bot, cuentas, exportacion) importan
SECCIONES, normalizar_seccion(), etiqueta_seccion() y seccion_desde_sector().
Sin dependencias externas.
"""
import unicodedata

# Codigo canonico -> nombre legible de la seccion.
SECCIONES = {
    "CI": "Ciudadanía",
    "IP": "Institución Privada",
    "LIB": "Libertad",
    "JUS": "Justicia",
}

# Alias aceptados por codigo (texto ya normalizado con _normalizar_texto).
_ALIAS_SECCION = {
    "CI": ("ci", "ciudadania", "ciudadano", "ciudadana"),
    "IP": ("ip", "privada", "privado", "privados", "institucionprivada", "institucion"),
    "LIB": ("lib", "libertad"),
    "JUS": ("jus", "justicia"),
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


def _claves_seccion(codigo: str) -> tuple:
    """Claves normalizadas que identifican una seccion.

    Incluye el codigo ("CI"), el nombre ("Ciudadanía") y la etiqueta completa
    que devuelve `etiqueta_seccion()` ("CI — Ciudadanía"), con y sin el guion
    largo, para que el dashboard pueda pasar indistintamente la etiqueta."""
    nombre = SECCIONES[codigo]
    return (
        _normalizar_texto(codigo),
        _normalizar_texto(nombre),
        _normalizar_texto(f"{codigo} — {nombre}"),
        _normalizar_texto(f"{codigo} {nombre}"),
    )


def normalizar_seccion(valor) -> str:
    """Devuelve el codigo canonico de seccion ("CI", "IP", "LIB" o "JUS").

    Acepta variantes como "ci", "CI", "ciudadania", "ciudadano", "ciudadana"
    (-> "CI"); "ip", "privada", "privado", "privados", "institucion privada",
    "institucion" (-> "IP"); "lib", "libertad" (-> "LIB"); "jus", "justicia"
    (-> "JUS"); y tambien la etiqueta completa ("CI — Ciudadanía", etc.).
    Cualquier otro valor, vacio o None devuelve "" (sin asignar): en
    particular "cd", "CD", "centroderecha", "centroizquierda", "izquierda" y
    "derecha" ya NO son validos (esas secciones dejaron de existir)."""
    clave = _normalizar_texto(valor)
    if not clave:
        return ""
    for codigo in SECCIONES:
        if clave in _ALIAS_SECCION.get(codigo, ()) or clave in _claves_seccion(codigo):
            return codigo
    return ""


def etiqueta_seccion(valor) -> str:
    """Devuelve la etiqueta legible de una seccion.

    "CI" -> "CI — Ciudadanía"; "IP" -> "IP — Institución Privada";
    "LIB" -> "LIB — Libertad"; "JUS" -> "JUS — Justicia"; "" -> "Sin asignar"."""
    clave = normalizar_seccion(valor)
    if not clave:
        return "Sin asignar"
    return f"{clave} — {SECCIONES[clave]}"


def seccion_desde_sector(sector) -> str:
    """Mapea el campo heredado 'sector' de la cuenta a una seccion canonica.

    El campo 'sector' viene de la etapa original de las cuentas. El mapeo a
    las 4 secciones actuales es:
        - "centroizquierda", "izquierda", "ciudadania" -> "CI"
        - "privados", "privado", "privada"            -> "IP"
        - "libertad"                                  -> "LIB"
        - "justicia"                                  -> "JUS"
        - "centroderecha", "derecha"                  -> "" (esa seccion ya no
          existe)
        - cualquier otro valor                        -> ""
    Tambien pasa por `normalizar_seccion()` para aceptar los codigos nuevos."""
    clave = _normalizar_texto(sector)
    if not clave:
        return ""
    # Mapeo heredado explicito: centroizquierda/izquierda -> CI y
    # centroderecha/derecha -> sin asignar (CD desaparecio).
    if "centroizquierd" in clave or clave == "izquierda":
        return "CI"
    if "centroderech" in clave or clave == "derecha":
        return ""
    return normalizar_seccion(sector)
