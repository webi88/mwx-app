"""Prompts organizados para la generacion de contenido con IA.

Incluye:
- Prompt generico (compatibilidad con llamadas antiguas).
- Prompts especificos por tipo: mantenimiento, activacion, narrativa,
  reposteo y blog.
- Prompts especiales: verificado ambiental y Harfuch.
- Prompts de apoyo para filtros y agrupacion de temas.

Los perfiles de redaccion (formal/ciudadano/popular) viven en
``core.perfiles``; ``bloque_estilo_perfil()`` traduce cada perfil al bloque de
formato obligatorio que se agrega al prompt.
"""
from core.perfiles import normalizar_perfil


def _base_narrativa(narrativa_info: str, entrenamiento: str = "") -> str:
    """Construye la persona/base comun con la narrativa y el entrenamiento."""
    if "4T" in narrativa_info or "morena" in narrativa_info.lower():
        base = (
            "ERES UN ESTRATEGA DIGITAL DE MORENA Y LA 4T.\n"
            "TU OBJETIVO ES APOYAR A CLAUDIA SHEINBAUM Y AL GOBIERNO.\n"
        )
    else:
        base = (
            f"ERES UN ESTRATEGA DIGITAL ESPECIALIZADO EN {narrativa_info}.\n"
            "TU OBJETIVO ES CREAR CONTENIDO RELEVANTE Y ATRACTIVO PARA LA AUDIENCIA.\n"
        )

    if entrenamiento:
        base += f"\nINSTRUCCIONES ESPECIFICAS:\n{entrenamiento}\n"

    return base


_REGLAS_REGISTRO = {
    "politica": (
        "REGISTRO: POLÍTICO/INSTITUCIONAL. Escribe con lenguaje formal, cuidado y "
        "bien puntuado; ortografía IMPECABLE (todas las tildes, puntuación "
        "correcta); tono sereno, argumentativo y respetuoso; evita "
        "coloquialismos, abreviaturas de redes y exceso de emojis. "
        "PROHIBIDO: faltas intencionales, confusiones disléxicas (b/d, q/p, "
        "s/c/z, b/v, h muda omitida, y/ll), mayúsculas/minúsculas inconsistentes "
        "y abreviaturas (q, pa, xq, tons, k, tmb, ntp). Esta regla tiene "
        "PRIORIDAD sobre el perfil: aunque el perfil pida tono casual, MANTÉN "
        "la ortografía perfecta y solo relaja la cercanía, nunca la corrección."
    ),
    "activista": (
        "REGISTRO: ACTIVISTA/TÉCNICO-COLOQUIAL. Escribe como ciudadania informada "
        "que apoya una causa politica: usa terminos politicos/actualidad con "
        "naturalidad pero con lenguaje popular; puedes omitir alguna tilde y "
        "usar 'q' por 'que', 'pa' por 'para', 'xq' por 'porque' y 'tons' de "
        "vez en cuando (MÁXIMO 2-3 licencias leves por texto); nada de errores "
        "graves; el mensaje debe seguir claro y con argumento. "
        "PROHIBIDO: errores disléxicos fuertes (confundir b/d, q/p, s/c/z, "
        "quitar la h muda, cambiar y/ll), mayúsculas/minúsculas inconsistentes "
        "y faltas que dificulten la lectura. Si el perfil es popular, usa solo "
        "el tono casual SIN subir la intensidad de los errores."
    ),
    "ciudadana": (
        "REGISTRO: CIUDADANO/PERSONA REAL (HUMANO, NO IA PERFECTA). Escribe como "
        "una persona mexicana común en redes: lenguaje muy coloquial y emocional, "
        "frases cortas, muletillas ('la neta', 'no manches', 'tons', 'pues', "
        "'mijo', 'ntp', 'tmb'). OBLIGATORIO que se note humano: incluye en CADA "
        "texto de 3 a 6 rasgos humanos legibles (nunca tantos que impidan "
        "entender, máximo 1 error por palabra): "
        "a) FALTAS FRECUENTES pero legibles: casi sin tildes ('mas', 'que', "
        "'tambien', 'corazon'); "
        "b) CONFUSIONES DISLÉXICAS variadas: b/d ('buro' por 'duro'), q/p, "
        "s/c/z ('aser' por 'hacer', 'cosas'/'kozaz', 'ves' por 'vez'), b/v "
        "('haver', 'bolber'), g/j ('jente'), h muda omitida o de más ('ola', "
        "'acer', 'hechar', 'halla' por 'haya'); y/ll ('yego', 'caye', 'apoyar' "
        "con 'y'); "
        "c) MAYÚSCULAS/MINÚSCULAS INCONSISTENTES: empieza a veces en minúscula, "
        "nombres propios sin mayúscula, alguna palabra en MAYÚSCULA suelta para "
        "énfasis ('NETA', 'YA'); "
        "d) ABREVIATURAS de redes: 'q', 'pa', 'xq', 'tons', 'k', 'tmb', 'ntp' "
        "(2-4 por texto); "
        "e) VARIACIÓN TOTAL ENTRE TEXTOS: prohibido repetir aperturas, orden, "
        "muletillas o estructuras; alterna pregunta/afirmación/exclamación, con "
        "y sin emoji (máximo 1), longitud distinta, un texto empieza con "
        "anécdota, otro con opinión directa, otro con pregunta al aire. "
        "Que se sienta espontáneo, sin lenguaje de político ni tecnicismos; aun "
        "así debe ENTENDERSE. NO deformes el hashtag (el hashtag va bien "
        "escrito). Aplica estos rasgos SIN romper el FORMATO OBLIGATORIO del "
        "perfil (si el perfil es formal, conserva sus 3 bloques pero con este "
        "toque humano dentro)."
    ),
}


def _reglas_registro(registro: str = "") -> str:
    """Bloque de reglas de registro listo para concatenar (con saltos de linea).

    "politica" -> lenguaje institucional/formal.
    "activista" -> tecnico-coloquial (terminos politicos + lenguaje popular).
    "ciudadana" -> lenguaje muy coloquial de una persona mexicana real.
    Cualquier otro valor (o vacio) no agrega reglas extra.
    """
    clave = (registro or "").strip().lower()
    regla = _REGLAS_REGISTRO.get(clave)
    if not regla:
        return ""
    return f"\n{regla}\n"


# --------------------------------------------------------------------------- #
# Perfiles de redaccion (core.perfiles) -> formato OBLIGATORIO por cuenta.
# --------------------------------------------------------------------------- #
_BLOQUES_ESTILO_PERFIL = {
    "formal": (
        "\nFORMATO OBLIGATORIO SEGUN EL PERFIL (Formal/Estructurado):"
        "\n- Redaccion analitica dividida en TRES partes claras: Titulo, "
        "Descripcion y Conclusion."
        "\n- Manten la estructura con DOBLE ENTER (una linea vacia) entre cada "
        "bloque."
        "\n- Ortografía cuidada y estructura intacta. El REGISTRO modula los "
        "errores: con registro politico/activista, CERO faltas; con registro "
        "ciudadano, permite SOLO un toque humano leve dentro de los bloques "
        "(alguna tilde omitida o 1 abreviatura) SIN romper los 3 bloques ni "
        "usar confusiones disléxicas fuertes."
        "\n- El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
        "texto; NUNCA lo pongas al final (colocalo dentro de la Descripcion, "
        "bien escrito, sin deformar)."
        "\nESTE FORMATO TIENE PRIORIDAD SOBRE CUALQUIER OTRA REGLA DE ESTRUCTURA."
    ),
    "ciudadano": (
        "\nFORMATO OBLIGATORIO SEGUN EL PERFIL (Ciudadano Promedio):"
        "\n- Un par de renglones (1-2 frases), con nivel de analisis intermedio."
        "\n- Ni muy formal ni muy coloquial en ESTRUCTURA: lenguaje claro, natural "
        "y cercano."
        "\n- El REGISTRO modula la intensidad humana: con registro politico, "
        "ortografía impecable; con registro activista, máximo 2-3 licencias "
        "leves (q/pa/xq/tons); con registro ciudadano, aplica el modo HUMANO "
        "COMPLETO (faltas frecuentes legibles, dislexias b/d-q/p-s/c/z-h muda-"
        "y/ll, mayúsculas inconsistentes, abreviaturas, variación total entre "
        "textos)."
        "\n- El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
        "texto; NUNCA lo pongas al final (bien escrito, sin deformar)."
        "\nESTE FORMATO TIENE PRIORIDAD SOBRE CUALQUIER OTRA REGLA DE ESTRUCTURA."
    ),
    "popular": (
        "\nFORMATO OBLIGATORIO SEGUN EL PERFIL (Popular/Organico):"
        "\n- Texto MUY casual y CORTO: de un solo renglon."
        "\n- Con registro ciudadano: incluye FALTAS INTENSAS pero legibles: sin "
        "tildes, 'q' por 'que', 'pa' por 'para', 'xq' por 'porque', 'tons', 'k' "
        "por 'que', más confusiones disléxicas (b/v, s/c/z, h muda, y/ll) y "
        "mayúsculas inconsistentes; que se note humano, no IA perfecta."
        "\n- Con registro politico/activista: SUAVIZA al mínimo (solo tono casual, "
        "ortografía cuidada, máximo 1-2 abreviaturas leves en activista y CERO "
        "faltas en politico; PROHIBIDAS las dislexias fuertes). El registro "
        "manda sobre el perfil en intensidad de errores."
        "\n- Aunque tenga faltas intencionales, el mensaje DEBE entenderse igual "
        "(máximo 1 error por palabra) y el hashtag va bien escrito."
        "\n- El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
        "texto; NUNCA lo pongas al final."
        "\nESTE FORMATO TIENE PRIORIDAD SOBRE CUALQUIER OTRA REGLA DE ESTRUCTURA."
    ),
}


def bloque_estilo_perfil(perfil: str) -> str:
    """Bloque de instrucciones de formato segun el perfil de la cuenta.

    Acepta variantes via ``core.perfiles.normalizar_perfil`` ("Formal",
    "Popular/Orgánico", etc.). Devuelve el bloque listo para concatenar (con
    salto de linea inicial) o "" si el perfil no se reconoce.
    """
    try:
        clave = normalizar_perfil(perfil)
    except Exception:
        return ""
    return _BLOQUES_ESTILO_PERFIL.get(clave, "")


# Instrucciones por tema para el mantenimiento organico de cuentas.
# "personalidad" es un texto libre generado por otro modulo (cuentas/).
_INSTRUCCIONES_TEMA = {
    "azteca": (
        "TEMA: AZTECA / CULTURA PREHISPANICA.\n"
        "- Habla de la cultura prehispanica/mexica: raices, historia, orgullo y "
        "tradiciones ancestrales (Mexico-Tenochtitlan, Templo Mayor, calendario, "
        "arte, mercados, herbolaria, lengua, comida).\n"
        "- Hazlo con RESPETO y SIN inventar datos historicos falsos: si no estas "
        "seguro de un dato, expresalo de forma general o simbolica."
    ),
    "dia": (
        "TEMA: DIA / ACTUALIDAD / EFEMERIDES.\n"
        "- Comenta un tema de actualidad del dia, la agenda publica o una efemeride.\n"
        "- SIN inventar noticias: si no hay un hecho verificado, comparte una "
        "reflexion general ligada al momento presente."
    ),
    "tendencias": (
        "TEMA: TENDENCIAS / CONVERSACION DIGITAL.\n"
        "- Sumate a los temas del momento y a la conversacion digital.\n"
        "- Puedes incluir hashtags relevantes e invitar a la gente a participar."
    ),
    "gustos": (
        "TEMA: GUSTOS / INTERESES COTIDIANOS.\n"
        "- Habla de intereses cotidianos (futbol, musica, comida, barrio, familia, "
        "tecnologia, viajes, etc.) con un tono personal, cercano y autentico."
    ),
}

TEMAS_MANTENIMIENTO = ("azteca", "dia", "tendencias", "gustos")


def _normalizar_tema(tema: str = "") -> str:
    """Normaliza el nombre del tema (minusculas, sin acentos y con alias)."""
    t = (tema or "").strip().lower()
    for acentuada, plana in (
        ("í", "i"), ("á", "a"), ("é", "e"), ("ó", "o"), ("ú", "u"),
    ):
        t = t.replace(acentuada, plana)
    alias = {
        "prehispanico": "azteca",
        "prehispanica": "azteca",
        "mexica": "azteca",
        "aztecas": "azteca",
        "actualidad": "dia",
        "diario": "dia",
        "efemerides": "dia",
        "tendencia": "tendencias",
        "trending": "tendencias",
        "gusto": "gustos",
        "cotidiano": "gustos",
        "intereses": "gustos",
    }
    return alias.get(t, t)


def instrucciones_tema(tema: str = "") -> str:
    """Devuelve las instrucciones del tema pedido ('' si no hay tema).

    Reutilizable por otros modulos para no duplicar los textos.
    """
    clave = _normalizar_tema(tema)
    if not clave:
        return ""
    return _INSTRUCCIONES_TEMA.get(clave, f"TEMA A TRATAR: {str(tema).strip()}")


def _bloque_tema_personalidad(tema: str = "", personalidad: str = "") -> str:
    """Bloque de contexto con la personalidad de la cuenta y/o el tema."""
    bloques = []
    if personalidad:
        bloques.append(
            "PERSONALIDAD DE LA CUENTA (respeta su tono e intereses): "
            f"{personalidad}"
        )
    instructivo = instrucciones_tema(tema)
    if instructivo:
        bloques.append(instructivo)
    if not bloques:
        return ""
    return "\n\n" + "\n\n".join(bloques)


def _reglas_formato(
    cantidad: int = 0,
    max_chars: int = 240,
    estructura: bool = True,
    hashtags: bool = True,
) -> str:
    """Reglas de formato comunes a todos los tipos de contenido.

    ``hashtags=False`` omite la regla de hashtag obligatorio en medio (util
    para formatos donde no aplica, p. ej. blog).
    """
    reglas = (
        "\nREGLAS DE FORMATO:\n"
        "1. SOLO entrega el texto del post. NADA MAS.\n"
        "2. NO uses etiquetas como 'Post 1:', 'Opcion 1'.\n"
        "3. NO enumeres los posts.\n"
        "4. NO uses corchetes [ ] en el texto.\n"
        "5. Usa '---' para separar cada post.\n"
        f"6. Longitud maxima: {max_chars} caracteres.\n"
        "7. No repitas textos, frases, ideas, aperturas, muletillas ni "
        "estructuras entre si: cada texto con apertura distinta (anécdota / "
        "opinión directa / pregunta al aire / dato), orden distinto, longitud "
        "distinta, y alternando pregunta-afirmación-exclamación, con y sin "
        "emoji (máximo 1 por texto).\n"
    )
    if hashtags:
        reglas += (
            "8. El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
            "texto; NUNCA lo pongas al final.\n"
        )
    if cantidad and cantidad > 0:
        numero = 9 if hashtags else 8
        reglas += (
            f"{numero}. Genera EXACTAMENTE {cantidad} textos DISTINTOS entre si. "
            "Cada uno debe tener un enfoque, redaccion y estructura diferentes.\n"
        )
    if estructura:
        reglas += (
            "\nESTRUCTURA DE CADA POST:\n"
            "[Titulo o frase gancho]\n"
            "[Doble Enter - Renglon Vacio]\n"
            "[Desarrollo del mensaje (aqui va el hashtag, integrado en medio)]\n"
            "[Doble Enter - Renglon Vacio]\n"
            "[Cierre breve]\n"
            "[Recuerda: el hashtag va EN MEDIO del texto, nunca al final]"
        )
    return reglas


def _prompt_mantenimiento(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: MANTENIMIENTO DE CUENTA.\n"
        "TAREA: mantener la cuenta con presencia y conversacion diaria.\n"
        "- Publica comentarios cercanos, naturales y humanos sobre el tema del dia.\n"
        "- Conversa con la audiencia e invita a opinar sin sonar repetitivo.\n"
        "- No repitas temas, frases ni estructuras entre publicaciones.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad)
        + bloque_estilo_perfil(perfil)
    )


def _prompt_activacion(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: ACTIVACION DIGITAL.\n"
        "TAREA: movilizar a la audiencia y detonar accion digital.\n"
        "- Llama a compartir, comentar, dar RT, etiquetar y difundir el mensaje.\n"
        "- Usa un tono energico, directo y movilizador.\n"
        "- Cada texto debe proponer una accion concreta distinta.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad)
        + bloque_estilo_perfil(perfil)
    )


def _prompt_narrativa(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: NARRATIVA.\n"
        "TAREA: construir y reforzar el relato.\n"
        "- Explica los logros, avances y la vision del movimiento.\n"
        "- Conecta los hechos con los valores de la narrativa.\n"
        "- Cada texto debe aportar un angulo distinto del relato.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad)
        + bloque_estilo_perfil(perfil)
    )


def _prompt_reposteo(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: REPOSTEO CON CITA.\n"
        "TAREA: lograr que la audiencia comparta el contenido.\n"
        "- Invita a dar RT, citar el tuit o compartir en otras redes.\n"
        "- Textos cortos, directos y con llamado explicito a compartir.\n"
        "- Cada texto debe invitar a compartir de una forma distinta.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad)
        + bloque_estilo_perfil(perfil)
    )


def _prompt_blog(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: BLOG.\n"
        "TAREA: redactar articulos breves para blog.\n"
        "- Desarrolla la idea con inicio, desarrollo y cierre.\n"
        "- Puedes usar varios parrafos dentro del mismo texto.\n"
        "- Tono informativo y persuasivo, sin perder la linea de la narrativa.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad, max_chars=600, estructura=False, hashtags=False)
        + bloque_estilo_perfil(perfil)
    )


_CONSTRUCTORES = {
    "mantenimiento": _prompt_mantenimiento,
    "activacion": _prompt_activacion,
    "activación": _prompt_activacion,
    "narrativa": _prompt_narrativa,
    "reposteo": _prompt_reposteo,
    "repost": _prompt_reposteo,
    "repostear": _prompt_reposteo,
    "blog": _prompt_blog,
}


def get_prompt_generico(
    narrativa_info: str,
    entrenamiento: str = "",
    tipo: str = "",
    cantidad: int = 0,
    registro: str = "",
) -> str:
    """Prompt generico (compatible con la firma posicional original).

    Si se recibe un `tipo` conocido se delega en `get_prompt_por_tipo`;
    en caso contrario se arma el prompt generico clasico, ahora con la
    cantidad exacta de textos cuando `cantidad > 0`.

    `registro` ("politica"/"ciudadana"/"") agrega reglas de estilo.
    """
    tipo_norm = (tipo or "").strip().lower()
    if tipo_norm and tipo_norm in _CONSTRUCTORES:
        return get_prompt_por_tipo(
            tipo_norm,
            narrativa_info,
            entrenamiento,
            cantidad=cantidad,
            registro=registro,
        )

    return (
        _base_narrativa(narrativa_info, entrenamiento)
        + _reglas_registro(registro)
        + _reglas_formato(cantidad)
    )


def get_prompt_por_tipo(
    tipo: str,
    narrativa: str,
    entrenamiento: str = "",
    contexto: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    """Devuelve el prompt correspondiente al tipo de contenido.

    Tipos soportados: mantenimiento, activacion, narrativa, reposteo y blog.
    Si el tipo es desconocido cae al prompt generico (sin lanzar error).

    `registro` ("politica"/"ciudadana"/"") se incluye en el prompt final
    junto a narrativa/entrenamiento/contexto/formato.

    `tema` (azteca/dia/tendencias/gustos/...) y `personalidad` (texto libre)
    se agregan como contexto; en mantenimiento el tema tiene instrucciones
    especificas y la personalidad se respeta de forma explicita.

    `perfil` ("formal"/"ciudadano"/"popular") agrega el FORMATO OBLIGATORIO
    por perfil de ``core.perfiles``; vacio/desconocido no agrega nada.
    """
    tipo_norm = (tipo or "").strip().lower()
    constructor = _CONSTRUCTORES.get(tipo_norm)

    if constructor is None:
        prompt = get_prompt_generico(
            narrativa, entrenamiento, cantidad=cantidad, registro=registro
        )
        prompt += _bloque_tema_personalidad(tema, personalidad)
        prompt += bloque_estilo_perfil(perfil)
    else:
        prompt = constructor(
            narrativa, entrenamiento, cantidad, registro, tema, personalidad,
            perfil,
        )

    if contexto:
        prompt += f"\n\nCONTEXTO ADICIONAL:\n{contexto}"

    return prompt


def get_prompt_mantenimiento(
    narrativa: str,
    entrenamiento: str = "",
    contexto: str = "",
    cantidad: int = 0,
    registro: str = "",
    tema: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    """Prompt de mantenimiento ya armado (helper publico).

    Equivale a `get_prompt_por_tipo("mantenimiento", ...)` con los mismos
    argumentos. `tema` acepta azteca/dia/tendencias/gustos (con alias);
    `personalidad` es el texto libre de la cuenta; `perfil`
    ("formal"/"ciudadano"/"popular") agrega su formato obligatorio.
    """
    return get_prompt_por_tipo(
        "mantenimiento",
        narrativa,
        entrenamiento,
        contexto,
        cantidad,
        registro,
        tema,
        personalidad,
        perfil,
    )


def get_prompt_comentario(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    perfil: str = "",
    tema: str = "",
    personalidad: str = "",
) -> str:
    """Prompt para COMENTARIOS/RESPUESTAS breves y conversacionales.

    Mismos bloques que mantenimiento (narrativa, registro, tema, personalidad
    y formato por perfil) pero los textos son aptos para responder: mas
    breves, directos y conversacionales. Todos llevan hashtag EN MEDIO y el
    perfil formal conserva Titulo/Descripcion/Conclusion en version corta.
    """
    return (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: COMENTARIO/RESPUESTA BREVE.\n"
        "TAREA: comentar o responder una publicacion de forma breve y natural.\n"
        "- Los textos son para responder: directos, conversacionales y con "
        "reaccion u opinion propia.\n"
        "- NO repitas la publicacion original ni empieces con 'En respuesta a'.\n"
        "- Cada texto debe aportar un angulo distinto y sonar humano.\n"
        "- Son mas breves que un post normal: 1-2 frases (el perfil formal "
        "mantiene Titulo/Descripcion/Conclusion pero MUY cortos).\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad, max_chars=200, estructura=False)
        + bloque_estilo_perfil(perfil)
    )


def get_prompt_hashtags(
    hashtags: str = "",
    contexto: str = "",
    narrativa: str = "",
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    personalidad: str = "",
    perfil: str = "",
) -> str:
    """Prompt de POST ORIGINAL con hashtag(s) obligatorios.

    A diferencia de los tipos clasicos, aqui el texto SIEMPRE debe ser una
    publicacion ORIGINAL sobre ``contexto``: se opina, comenta y aporta algo
    propio (prohibido copiar el contexto tal cual o parafrasearlo plano). Los
    ``hashtags`` pedidos se integran EN MEDIO del texto (nunca al final) y
    ``registro``/``perfil``/``personalidad`` modulan el estilo igual que en el
    resto de los prompts. Reutiliza los bloques existentes de este modulo.
    """
    prompt = (
        _base_narrativa(narrativa, entrenamiento)
        + _reglas_registro(registro)
        + "\nTIPO DE CONTENIDO: POST ORIGINAL CON HASHTAG.\n"
        "TAREA: redactar publicaciones ORIGINALES con opinion propia sobre el "
        "CONTEXTO indicado.\n"
        "- PROHIBIDO copiar el contexto tal cual o parafrasearlo de forma plana: "
        "cada texto debe OPINAR, comentar, reaccionar y aportar algo propio "
        "(una emocion, un ejemplo, una comparacion, una anecdota breve) con la "
        "voz de la cuenta.\n"
        "- Cada texto debe tener un ENFOQUE, una apertura y una estructura "
        "DISTINTOS: no repitas frases, ideas, muletillas ni el mismo angulo.\n"
        "- El resultado NO debe ser el contexto ni el contexto con el hashtag "
        "pegado: debe sonar como una publicacion espontanea y humana.\n"
        "- HASHTAG NATURAL: el hashtag (o grupo de hashtags) debe integrarse "
        "de forma NATURAL, preferentemente al final de una frase o despues de "
        "una coma/punto cercano a la MITAD del texto.\n"
        "- El hashtag no debe romper la frase: no partir ninguna oración con "
        "el (PROHIBIDO insertarlo entre un artículo o demostrativo y su "
        "sustantivo, ejemplo PROHIBIDO: \"el orgullo #Mexico nacional\"); el "
        "texto debe leerse con sentido completo aunque el hashtag no "
        "estuviera.\n"
        "- PROHIBIDO dejar el hashtag al final del texto: nunca al final ni "
        "como cierre ni en una lista aparte.\n"
        "- EXTENSION: maximo 240 caracteres por texto. El texto debe quedar "
        "COMPLETO y con sentido (incluida su frase de cierre), nunca cortado "
        "a la mitad.\n"
        + _bloque_tema_personalidad("", personalidad)
    )
    if hashtags:
        prompt += (
            "\nHASHTAGS OBLIGATORIOS:\n"
            f"- Usa EXACTAMENTE estos hashtags (bien escritos): {hashtags}\n"
            "- Integrados EN MEDIO del texto, de forma NATURAL: al final de "
            "una frase o despues de una coma/punto cercano a la mitad; nunca "
            "entre un articulo y su sustantivo, nunca partiendo la frase y "
            "nunca al final del texto.\n"
        )
    if contexto:
        prompt += (
            "\nCONTEXTO (tema sobre el que debes opinar; NO copies su redacción):\n"
            f"{contexto}"
        )
    prompt += _reglas_formato(cantidad)
    prompt += bloque_estilo_perfil(perfil)
    return prompt


def get_prompt_verificado_ambiental(contexto_links: str) -> str:
    return (
        "ERES UN VERIFICADOR DE HECHOS OBJETIVO PERO CON SESGO SUTIL PRO-GOBIERNO DE QUINTANA ROO.\n\n"
        "TAREA: Crear 3 copys (uno por link) donde:\n"
        "1. Resumas el punto principal del articulo\n"
        "2. Verifiques o desmientas la informacion dada\n"
        "3. Si el tema es de Quintana Roo y se puede verificar/desmentir positivamente\n"
        "   a favor del gobierno del estado, hazlo de forma SUTIL\n"
        "4. Con todas las demas regiones, se objetivo y realista\n\n"
        "REGLAS:\n"
        "- Maximo 240 caracteres por post\n"
        "- Usa emojis segun corresponda\n"
        "- Se objetivo pero favorece sutilmente a QRoo cuando aplique\n"
        "NO uses hashtags\n"
        "- Separa cada post con '---'\n\n"
        f"LINKS A VERIFICAR:\n{contexto_links}"
    )


def get_prompt_harfuch(contexto_links: str) -> str:
    return (
        "ERES UN ESTRATEGA DIGITAL DE APOYO EXTREMO A OMAR GARCIA HARFUCH.\n\n"
        "TAREA: Crear 2 copys POR CADA LINK que:\n"
        "1. Resalten los logros y cualidades de Harfuch\n"
        "2. Generen apoyo y admiracion\n"
        "3. Destaquen su liderazgo y capacidad\n"
        "4. Sean persuasivos y emocionales\n\n"
        "REGLAS:\n"
        "- Maximo 240 caracteres por post\n"
        "- Tono de apoyo extremo pero creible\n"
        "- Usa emojis estrategicamente\n"
        "- Separa cada post con '---'\n\n"
        f"LINKS:\n{contexto_links}"
    )


def get_prompt_filtro_alerta() -> str:
    return (
        "Eres un clasificador de relevancia politica en Mexico.\n"
        "Clasifica cada noticia como RELEVANTE o NO RELEVANTE.\n\n"
        "RELEVANTE: Gobierno, funcionarios, partidos, elecciones, seguridad publica,\n"
        "medio ambiente, infraestructura publica, MIA, emergencias atendidas por autoridades.\n\n"
        "NO RELEVANTE: Contenido comercial, entretenimiento sin autoridades,\n"
        "deportes sin corrupcion, turismo/gastronomia, farandula.\n\n"
        "Responde con el formato:\n"
        "N. resumen (si es relevante)\n"
        "N. NO (si no es relevante)\n"
        "N. OK (si es relevante pero sin info para resumir)"
    )


def get_prompt_agrupar_temas() -> str:
    return (
        "Agrupa los siguientes titulares en temas similares.\n"
        "Responde con JSON: {\"temas\": [{\"titulo\": \"...\", \"cantidad\": N}]}\n"
        "Maximo 5 temas principales."
    )
