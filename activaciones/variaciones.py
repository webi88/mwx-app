"""Generacion de variaciones de contenido para activaciones masivas.

Permite que las 500 cuentas publiquen quote-RTs con textos ligeramente
distintos entre si para no disparar el anti-spam de X (evita el patron
de contenido identico en masa).
"""
import random
import re
from loguru import logger


SINONIMOS = {
    "muy": ["muy", "bastante", "sumamente", "realmente"],
    "importante": ["importante", "clave", "fundamental", "relevante"],
    "gran": ["gran", "enorme", "tremendo", "increible"],
    "buen": ["buen", "excelente", "gran"],
    "día": ["día", "dia", "día de hoy"],
    "hoy": ["hoy", "este día", "ahora"],
    "mexico": ["México", "Mexico", "nuestro país"],
    "gobierno": ["gobierno", "Gobierno", "administración"],
    "pueblo": ["pueblo", "gente", "ciudadanía", "la gente"],
}

HASHTAGS = [
    "#México", "#Mexico", "#Transformación", "#4T", "#PrimeroElPueblo",
    "#NadaNosDetiene", "#LaEsperanzaDeMéxico", "#JusticiaSocial",
    "#MéxicoAvanza", "#ConElPuebloTodo", "#CuartaTransformación",
    "#Bienestar", "#OrgullosamenteMexicano", "#Soberanía",
]

APERTURAS = [
    "Esto es lo que está pasando:",
    "Totalmente de acuerdo.",
    "No podía ser más claro:",
    "Así es, sin más:",
    "Punto importante:",
    "Hay que difundir esto:",
    "Lo que muchos no están viendo:",
    "Para no olvidar:",
]

CIERRES = [
    "",
    "¿Qué opinas?",
    "RT para difundir.",
    "Comparte.",
    "No dejemos de hablar de esto.",
    "La información es poder.",
    "",
    "Dale RT.",
]


_RE_HASHTAG = r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+"


def _quitar_hashtags(texto: str) -> str:
    """Quita los hashtags del texto y normaliza los espacios resultantes."""
    limpio = re.sub(_RE_HASHTAG, " ", str(texto or ""))
    limpio = re.sub(r"[ \t]{2,}", " ", limpio)
    limpio = re.sub(r" +([,.;:!?])", r"\1", limpio)
    return re.sub(r"\n{3,}", "\n\n", limpio).strip()


def _normalizar_hashtags(hashtags) -> list[str]:
    """Normaliza hashtags (lista o string con comas/espacios, con o sin '#').

    Devuelve la grafia tal como la pidio el usuario (solo le antepone '#'),
    sin duplicados case-insensitive ni vacios. Nunca lanza.
    """
    if hashtags is None:
        return []
    if isinstance(hashtags, str):
        crudos = [hashtags]
    else:
        try:
            crudos = list(hashtags)
        except TypeError:
            return []
    tags, vistos = [], set()
    for crudo in crudos:
        for token in str(crudo or "").replace(",", " ").split():
            token = token.strip()
            if not token:
                continue
            if not token.startswith("#"):
                token = "#" + token.lstrip("@")
            clave = token.lower()
            if clave not in vistos:
                vistos.add(clave)
                tags.append(token)
    return tags


def variar_texto(base: str, n_hashtags: int = 2, hashtags=None) -> str:
    """Aplica variaciones leves a un texto base: apertura, sinonimos,
    hashtags aleatorios y cierre. Devuelve un texto distinto cada vez.

    `hashtags` (lista o string con comas/espacios, con o sin '#'): si NO es
    None se usan ESTRICTAMENTE esos hashtags y se agrega al final un
    subconjunto aleatorio de tamano 1..len (con lista vacia no agrega nada).
    Si es None se conserva el comportamiento clasico (global `HASHTAGS` con
    `n_hashtags`).
    """
    texto = base.strip()

    for original, opciones in SINONIMOS.items():
        if original in texto and random.random() < 0.5:
            reemplazo = random.choice(opciones)
            if reemplazo != original:
                texto = texto.replace(original, reemplazo, 1)

    if random.random() < 0.35 and texto:
        apertura = random.choice(APERTURAS)
        texto = f"{apertura} {texto[0].lower() + texto[1:] if len(texto) > 1 else texto}"

    if random.random() < 0.3:
        cierre = random.choice(CIERRES)
        if cierre:
            texto = f"{texto}\n\n{cierre}"

    if hashtags is not None:
        pedidos = _normalizar_hashtags(hashtags)
        if pedidos:
            # Quita cualquier hashtag previo (p.ej. #Ajeno del base) para que
            # SOLO queden los pedidos, en un subconjunto aleatorio.
            texto = _quitar_hashtags(texto)
            k = random.randint(1, len(pedidos))
            seleccion = random.sample(pedidos, k)
            texto = f"{texto}\n\n{' '.join(seleccion)}"
    elif n_hashtags > 0:
        tags = random.sample(HASHTAGS, k=min(n_hashtags, len(HASHTAGS)))
        texto = f"{texto}\n\n{' '.join(tags)}"

    return texto


def generar_pool_variaciones(base: str, cantidad: int, n_hashtags: int = 2,
                             hashtags=None) -> list[str]:
    """Genera 'cantidad' variaciones distintas de un texto base.

    `hashtags` se propaga a `variar_texto`: si no es None, cada variacion usa
    SOLO un subconjunto aleatorio de esos hashtags (nunca los globales); con
    lista vacia no agrega hashtags.
    """
    pool = []
    vistos = set()
    intentos = 0
    while len(pool) < cantidad and intentos < cantidad * 5:
        v = variar_texto(base, n_hashtags, hashtags=hashtags)
        intentos += 1
        if v not in vistos:
            vistos.add(v)
            pool.append(v)
    logger.info(f"Pool de variaciones generado: {len(pool)} textos unicos")
    return pool


def generar_pool_variaciones_openai(
    base: str,
    cantidad: int,
    narrativa: str = "",
    entrenamiento: str = "",
    registro: str = "",
    perfil: str = "",
    hashtags=None,
) -> list[str]:
    """Genera 'cantidad' variaciones unicas de una cita usando OpenAI.

    Las variaciones respetan el REGISTRO (politica/activista/ciudadana) y el
    PERFIL (formal/ciudadano/popular) de la cuenta para la que se generan, de
    modo que cada grupo de cuentas recibe textos con su propio estilo.

    `narrativa` es SOLO TRASFONDO: viaja al prompt como referencia interna
    (el prompt de `ia.generador_contenido` ya lo refuerza) y NUNCA se usa como
    texto publicable ni como base del fallback local (que solo varia `base`).

    `hashtags` (lista o string, con o sin '#') solo afecta al FALLBACK local:
    si no es None, el fallback usa ESTRICTAMENTE esos hashtags (subconjunto
    aleatorio) en vez de los globales; con [] no agrega ninguno. Los textos
    que devuelve OpenAI no se modifican aqui.

    Si OpenAI no devuelve suficiente variedad, rellena el faltante con el
    fallback basado en sinonimos. Si aun asi falta, agrega variantes con
    sufijo numerado. Devuelve exactamente 'cantidad' strings (o tantos como
    sea humanamente posible). Retrocompatible: con registro/perfil vacios se
    comporta como antes."""
    from ia.generador_contenido import GeneradorContenido

    pool = []
    vistos = set()

    textos_openai = GeneradorContenido().generar_variaciones_masivas(
        base, cantidad, narrativa, entrenamiento, registro, perfil
    )
    for t in textos_openai:
        if t and t not in vistos:
            vistos.add(t)
            pool.append(t)

    if len(pool) < cantidad:
        faltante = cantidad - len(pool)
        fallback = generar_pool_variaciones(
            base, cantidad=faltante, n_hashtags=2, hashtags=hashtags
        )
        for t in fallback:
            if t and t not in vistos:
                vistos.add(t)
                pool.append(t)
            if len(pool) >= cantidad:
                break

    sufijo = 1
    while len(pool) < cantidad:
        v = f"{base} ({sufijo})"
        sufijo += 1
        if v not in vistos:
            vistos.add(v)
            pool.append(v)

    logger.info(f"Pool de variaciones OpenAI generado: {len(pool)} textos unicos")
    return pool[:cantidad]
