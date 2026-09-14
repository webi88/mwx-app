# -*- coding: utf-8 -*-
"""Perfiles de personalidad de las cuentas (redaccion del contenido).

Tres perfiles canonicos que definen COMO redacta cada cuenta. El perfil se
guarda en ``Cuenta.perfil_personalidad`` y viaja por los pools de generacion
(ver ``ia.generador_contenido``) para que el LLM respete el formato exacto:

    - ``formal``    -> Formal/Estructurado: analitico en 3 partes
                       (Titulo / Descripcion / Conclusion).
    - ``ciudadano`` -> Ciudadano Promedio: par de renglones, analisis
                       intermedio (ni muy formal ni muy coloquial).
    - ``popular``   -> Popular/Organico: muy casual, de un solo renglon y con
                       faltas de ortografia intencionales.

Tambien centraliza la regla de hashtags (obligatorio y en MEDIO del texto,
nunca al final): ``elegir_hashtag()`` y ``colocar_hashtag_en_medio()``.

Interfaz congelada: otros modulos (ia, cuentas, web, cli) importan las
constantes y funciones publicas de aqui sin duplicarlas. Sin dependencias
externas (solo stdlib) para evitar imports circulares.
"""
import random
import re
import unicodedata

# Codigo canonico -> etiqueta legible.
PERFILES_PERSONALIDAD = {
    "formal": "Formal/Estructurado",
    "ciudadano": "Ciudadano Promedio",
    "popular": "Popular/Orgánico",
}

# Orden canonico (util para repartos e historicos).
PERFILES_ORDEN = ("formal", "ciudadano", "popular")


def _sin_acentos(texto) -> str:
    """Minusculas sin acentos (para comparar variantes de entrada)."""
    plano = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in plano if not unicodedata.combining(c)).lower().strip()


def normalizar_perfil(valor) -> str:
    """Devuelve el codigo canonico del perfil.

    Acepta variantes con/sin acentos y sinonimos:
        - "formal", "formals", "estructurado", "analitico" -> "formal".
        - "ciudadano", "promedio", "intermedio" -> "ciudadano".
        - "popular", "organico", "casual", "barrio" -> "popular".
    Cualquier otro valor, vacio o None devuelve "" (perfil sin definir).
    """
    clave = _sin_acentos(valor)
    if not clave:
        return ""
    # Normaliza separadores ("popular/organico" -> "popular organico").
    clave = re.sub(r"[^a-z0-9]+", " ", clave).strip()
    for token in clave.split():
        if token.startswith("formal") or token.startswith("estructurad"):
            return "formal"
        if token.startswith("analitic"):
            return "formal"
        if token.startswith("ciudadan"):
            return "ciudadano"
        if token.startswith("promedi"):
            return "ciudadano"
        if token.startswith("intermedi"):
            return "ciudadano"
        if token.startswith("popular"):
            return "popular"
        if token.startswith("organic"):
            return "popular"
        if token.startswith("casual") or token.startswith("barrio"):
            return "popular"
    return ""


def etiqueta_perfil(valor) -> str:
    """Etiqueta legible del perfil; "Sin perfil" si no se reconoce."""
    clave = normalizar_perfil(valor)
    if not clave:
        return "Sin perfil"
    return PERFILES_PERSONALIDAD[clave]


def perfil_coherente(perfil: str, registro: str = "") -> bool:
    """True si el perfil puede convivir con el registro de voz de la cuenta.

    Los perfiles "ciudadano" y "popular" exigen un registro coloquial, asi que
    no se asignan a cuentas con voz "politica"/institucional (a menos que el
    registro venga vacio).
    """
    perfil_n = normalizar_perfil(perfil)
    registro_n = _sin_acentos(registro)
    if not perfil_n:
        return False
    if perfil_n == "formal":
        return True
    if not registro_n:
        return True
    if registro_n.startswith(("politic", "institucional")):
        return False
    return True


def distribuir_perfiles(cantidad: int, rng=random) -> list[str]:
    """Reparte ``cantidad`` perfiles lo mas equitativo posible entre los 3.

    Con 165 cuentas devuelve 55 de cada perfil (en orden aleatorio); si no es
    divisible, los sobrantes caen en "popular" y luego "ciudadano" (el orden
    final se baraja). Nunca lanza: cantidad <= 0 devuelve [].
    """
    try:
        n = int(cantidad)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []

    base, resto = divmod(n, len(PERFILES_ORDEN))
    perfiles = []
    for i, clave in enumerate(PERFILES_ORDEN):
        extra = 1 if i > 0 and resto > 0 else 0
        perfiles.extend([clave] * (base + extra))
    rng.shuffle(perfiles)
    return perfiles


# --------------------------------------------------------------------------- #
# Regla de hashtags: obligatorio y SIEMPRE integrado en medio del texto.
# --------------------------------------------------------------------------- #
HASHTAGS_SUGERIDOS = (
    "#Mexico", "#Nacional", "#MexicoHoy", "#Pueblo", "#Ciudad",
    "#Comunidad", "#Barrio", "#Tradicion", "#Cultura", "#Actualidad",
    "#BuenosDias", "#Informacion", "#Opinion", "#Conversacion", "#Organizacion",
)


def _normalizar_hashtag(valor) -> str:
    """Convierte "Mexico hoy" -> "#MexicoHoy"; "" si no queda utilizable."""
    texto = str(valor or "").strip().lstrip("#").strip()
    if not texto:
        return ""
    partes = re.findall(r"[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ]+", texto)
    if not partes:
        return ""
    return "#" + "".join(p[:1].upper() + p[1:] for p in partes)


def elegir_hashtag(texto: str = "", extra=None, rng=random) -> str:
    """Elige un hashtag para el texto.

    Si ``texto`` ya trae hashtags, devuelve el primero (respetando el que
    puso el LLM); si no, elige uno de ``extra`` (o de ``HASHTAGS_SUGERIDOS``).
    """
    texto = str(texto or "")
    existentes = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", texto)
    if existentes:
        return existentes[0]
    candidatos = []
    if extra:
        if isinstance(extra, str):
            extra = re.split(r"[,\s]+", extra)
        for c in extra:
            tag = _normalizar_hashtag(c)
            if tag:
                candidatos.append(tag)
    if not candidatos:
        candidatos = list(HASHTAGS_SUGERIDOS)
    return rng.choice(candidatos)


def tiene_hashtag(texto) -> bool:
    """True si el texto incluye al menos un hashtag."""
    return bool(re.search(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", str(texto or "")))


def _quitar_hashtags(texto: str) -> str:
    """Quita los hashtags del texto y normaliza los espacios resultantes."""
    limpio = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", str(texto or ""))
    limpio = re.sub(r"[ \t]+", " ", limpio)
    limpio = re.sub(r"\n{3,}", "\n\n", limpio)
    return limpio.strip()


def colocar_hashtag_en_medio(texto, hashtag: str = "", rng=random) -> str:
    """Devuelve el texto con UN hashtag integrado en el MEDIO (nunca al final).

    - Si el texto ya tiene hashtag(s) al final, se mueven al medio.
    - El punto de insercion es el espacio/enter natural mas cercano a la mitad
      del texto (tras la primera frase si el texto es corto).
    - Garantiza al menos un hashtag: si no hay, usa ``hashtag`` o uno aleatorio.
    - Nunca lanza: devuelve el texto original si algo falla.
    """
    try:
        original = str(texto or "").strip()
        if not original:
            return original
        tag = _normalizar_hashtag(hashtag) or elegir_hashtag(original, rng=rng)
        base = _quitar_hashtags(original)
        if not base:
            return tag
        if not tag:
            return base

        mitad = len(base) // 2
        # Punto natural mas cercano a la mitad: espacio o salto de linea.
        espacios = [m.start() for m in re.finditer(r"(?:\s|\n)", base)]
        if espacios:
            punto = min(espacios, key=lambda p: abs(p - mitad))
            izquierda = base[:punto].rstrip()
            derecha = base[punto:].lstrip()
        else:
            izquierda, derecha = base, ""

        if not izquierda:
            return f"{tag} {derecha}".strip()
        if not derecha:
            # Texto de un solo bloque sin espacios: parte a la mitad exacta.
            corte = max(1, len(base) // 2)
            return f"{base[:corte].rstrip()} {tag} {base[corte:].lstrip()}".strip()
        return f"{izquierda} {tag} {derecha}".strip()
    except Exception:
        return str(texto or "").strip()
