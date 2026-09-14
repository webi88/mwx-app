# -*- coding: utf-8 -*-
"""Roles de activacion de cuentas en campana.

Tres roles operativos:
    - cita:     publica retweets con cita (quote tweets).
    - hashtags: publica hashtags y menciones.
    - rt:       publica retweets simples.

Interfaz congelada: otros modulos (web, bot, activaciones, ia) importan
ROLES_ACTIVACION, normalizar_rol_activacion() y etiqueta_rol_activacion().
Sin dependencias externas. La normalizacion de acentos reutiliza el mismo
enfoque de core/secciones.py para que las clasificaciones sean coherentes.
"""
from core.secciones import _normalizar_texto

# Codigo canonico -> nombre legible del rol de activacion.
ROLES_ACTIVACION = {
    "cita": "Retweet con cita",
    "hashtags": "Hashtags y menciones",
    "rt": "Retweet simple",
}


def _tokens(valor) -> list:
    """Parte un valor en palabras ya normalizadas (minusculas, sin acentos).

    "Retweet con cita" -> ["retweet", "con", "cita"];
    "menciones_hashtags" -> ["menciones", "hashtags"]; None -> []. """
    if valor is None:
        return []
    texto = str(valor).strip().lower()
    for separador in ("_", "-", ".", "/", ",", ";", "(", ")", "[", "]"):
        texto = texto.replace(separador, " ")
    return [_normalizar_texto(palabra) for palabra in texto.split() if palabra]


def normalizar_rol_activacion(valor) -> str:
    """Devuelve el codigo canonico del rol de activacion.

    Acepta variantes con o sin acentos y sinonimos:
        - "cita", "citado", "citar", "quote", "cita con comentario" -> "cita".
        - "hashtag", "hashtags", "hashtags y menciones", "mencion",
          "menciones", "menciones_hashtags" -> "hashtags".
        - "rt", "retweet", "repost", "repostear" -> "rt".
    Cualquier otro valor, vacio o None devuelve "" (sin rol). Las frases con
    "cita" ganan sobre "retweet" (ej. "Retweet con cita" -> "cita")."""
    tokens = _tokens(valor)
    if not tokens:
        return ""
    # La cita tiene prioridad: "Retweet con cita" no es un RT simple.
    for token in tokens:
        if token.startswith("cita") or token.startswith("quote"):
            return "cita"
    for token in tokens:
        if token.startswith("hashtag") or token.startswith("mencion"):
            return "hashtags"
    for token in tokens:
        if token == "rt" or token.startswith("retweet") or token.startswith("repost"):
            return "rt"
    return ""


def etiqueta_rol_activacion(valor) -> str:
    """Devuelve la etiqueta legible de un rol de activacion.

    "cita" -> "Retweet con cita"; "hashtags" -> "Hashtags y menciones";
    "rt" -> "Retweet simple"; "" -> "Sin rol"."""
    clave = normalizar_rol_activacion(valor)
    if not clave:
        return "Sin rol"
    return ROLES_ACTIVACION[clave]
