"""Generacion de variaciones de contenido para activaciones masivas.

Permite que las 500 cuentas publiquen quote-RTs con textos ligeramente
distintos entre si para no disparar el anti-spam de X (evita el patron
de contenido identico en masa).
"""
import random
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


def variar_texto(base: str, n_hashtags: int = 2) -> str:
    """Aplica variaciones leves a un texto base: apertura, sinonimos,
    hashtags aleatorios y cierre. Devuelve un texto distinto cada vez."""
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

    if n_hashtags > 0:
        tags = random.sample(HASHTAGS, k=min(n_hashtags, len(HASHTAGS)))
        texto = f"{texto}\n\n{' '.join(tags)}"

    return texto


def generar_pool_variaciones(base: str, cantidad: int, n_hashtags: int = 2) -> list[str]:
    """Genera 'cantidad' variaciones distintas de un texto base."""
    pool = []
    vistos = set()
    intentos = 0
    while len(pool) < cantidad and intentos < cantidad * 5:
        v = variar_texto(base, n_hashtags)
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
) -> list[str]:
    """Genera 'cantidad' variaciones unicas de una cita usando OpenAI.

    Si OpenAI no devuelve suficiente variedad, rellena el faltante con el
    fallback basado en sinonimos. Si aun asi falta, agrega variantes con
    sufijo numerado. Devuelve exactamente 'cantidad' strings (o tantos como
    sea humanamente posible)."""
    from ia.generador_contenido import GeneradorContenido

    pool = []
    vistos = set()

    textos_openai = GeneradorContenido().generar_variaciones_masivas(
        base, cantidad, narrativa, entrenamiento
    )
    for t in textos_openai:
        if t and t not in vistos:
            vistos.add(t)
            pool.append(t)

    if len(pool) < cantidad:
        faltante = cantidad - len(pool)
        fallback = generar_pool_variaciones(base, cantidad=faltante, n_hashtags=2)
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


def elegir_variacion(pool: list[str]) -> str:
    """Elige una variacion aleatoria del pool (o la unica disponible)."""
    if not pool:
        return ""
    return random.choice(pool)
