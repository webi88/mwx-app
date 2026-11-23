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


def generar_pool_variaciones_diversas(
    base: str,
    cantidad: int,
    registro: str = "",
    perfil: str = "",
    hashtags=None,
    evitar=None,
    limite: int = 100,
    semilla=None,
) -> list[str]:
    """Wrapper publico del generador local diverso de ``ia`` (import perezoso).

    Misma firma/defaults que
    ``ia.generador_contenido.generar_variaciones_locales_diversas``: reordena
    ideas, cambia conectores/aperturas/cierres y respeta registro/perfil y
    hashtags. Se expone aqui para no acoplar los llamadores de activaciones al
    modulo de IA. Nunca lanza: si el import falla devuelve [].
    """
    try:
        from ia.generador_contenido import generar_variaciones_locales_diversas
        return generar_variaciones_locales_diversas(
            base,
            cantidad,
            registro=registro,
            perfil=perfil,
            hashtags=hashtags,
            evitar=evitar,
            limite=limite,
            semilla=semilla,
        )
    except Exception as e:
        logger.error(f"Error en generador local diverso: {e}")
        return []


def generar_pool_variaciones_openai(
    base: str,
    cantidad: int,
    narrativa: str = "",
    entrenamiento: str = "",
    registro: str = "",
    perfil: str = "",
    hashtags=None,
    evitar=None,
    limite: int = 100,
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
    aleatorio); con [] no agrega ninguno. Los textos que devuelve OpenAI no se
    modifican aqui.

    `evitar` (antes de `limite`): textos de la campana que NO deben
    reutilizarse; se pasan a la IA como "NO REPITAS" y se filtran por
    similitud. Si OpenAI no devuelve suficiente variedad, rellena el faltante
    con el generador LOCAL DIVERSO (estructuras/angulos distintos, no el
    fallback plano de sinonimos). Ultimo recurso: sufijo numerado. Devuelve
    exactamente 'cantidad' strings (o tantos como sea humanamente posible).
    """
    from ia.generador_contenido import (
        DIVERSIDAD_UMBRAL_CITA,
        GeneradorContenido,
        generar_variaciones_locales_diversas,
        normalizar_para_comparar,
        texto_es_suficientemente_distinto,
    )

    try:
        cantidad = int(cantidad)
    except (TypeError, ValueError):
        cantidad = 0
    if cantidad <= 0:
        return []

    evitar_lista = []
    if evitar is not None:
        if isinstance(evitar, str):
            evitar_lista = [evitar.strip()] if evitar.strip() else []
        else:
            try:
                evitar_lista = [
                    str(x).strip() for x in evitar if str(x or "").strip()
                ]
            except TypeError:
                evitar_lista = [str(evitar).strip()] if str(evitar).strip() else []

    pool: list[str] = []
    vistos: set[str] = set()
    claves: set[str] = set()

    def _agregar(texto) -> bool:
        t = str(texto or "").strip()
        if not t:
            return False
        clave = normalizar_para_comparar(t)
        if t in vistos or (clave and clave in claves):
            return False
        if not texto_es_suficientemente_distinto(
            t, evitar_lista + pool, DIVERSIDAD_UMBRAL_CITA
        ):
            return False
        vistos.add(t)
        if clave:
            claves.add(clave)
        pool.append(t)
        return True

    try:
        textos_openai = GeneradorContenido().generar_variaciones_masivas(
            base,
            cantidad,
            narrativa,
            entrenamiento,
            registro,
            perfil,
            evitar=evitar_lista,
            limite=limite,
        )
    except Exception as e:
        logger.error(f"Error en variaciones OpenAI: {e}")
        textos_openai = []

    for t in textos_openai or []:
        _agregar(t)
        if len(pool) >= cantidad:
            break

    # Relleno con el generador LOCAL DIVERSO (nunca el fallback de sinonimos).
    intentos = 0
    while len(pool) < cantidad and intentos < 12:
        intentos += 1
        faltante = cantidad - len(pool)
        try:
            locales = generar_variaciones_locales_diversas(
                base,
                faltante,
                registro=registro,
                perfil=perfil,
                hashtags=hashtags,
                evitar=evitar_lista + pool,
                limite=limite,
                semilla=random.randint(1, 10 ** 9),
            )
        except Exception as e:
            logger.error(f"Error en relleno local diverso: {e}")
            locales = []
        if not locales:
            break
        antes = len(pool)
        for t in locales:
            _agregar(t)
            if len(pool) >= cantidad:
                break
        if len(pool) == antes:
            break

    # Ultimo recurso: sufijo numerado (garantiza el contrato de cantidad).
    sufijo = 1
    while len(pool) < cantidad and sufijo < cantidad + 50:
        v = f"{base} ({sufijo})"
        sufijo += 1
        if v not in vistos:
            vistos.add(v)
            pool.append(v)

    logger.info(f"Pool de variaciones OpenAI generado: {len(pool)} textos unicos")
    return pool[:cantidad]
