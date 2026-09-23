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
import re
import unicodedata

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
        "REGISTRO: POLÍTICO/INSTITUCIONAL. Escribe con lenguaje formal, cuidado "
        "y bien puntuado; usa correctamente los signos de apertura y cierre "
        "('¿...?', '¡...!'). Se permite COMO MÁXIMO UN error leve opcional por "
        "texto (por ejemplo, una tilde omitida); ningún error adicional. "
        "PROHIBIDAS las confusiones disléxicas (b/d, q/p, s/c/z, b/v, h muda "
        "omitida, y/ll), las abreviaturas de redes (q, pa, xq, tons, k, tmb, "
        "ntp) y las mayúsculas/minúsculas inconsistentes. Tono sereno, "
        "argumentativo y respetuoso; evita coloquialismos y exceso de emojis. "
        "Esta regla tiene PRIORIDAD sobre el perfil: aunque el perfil pida tono "
        "casual, MANTÉN la ortografía cuidada (máximo un error leve opcional) y "
        "solo relaja la cercanía, nunca la corrección."
    ),
    "activista": (
        "REGISTRO: ACTIVISTA/TÉCNICO-COLOQUIAL. Escribe como ciudadanía "
        "informada que apoya una causa política: usa términos políticos y de "
        "actualidad con naturalidad, pero con lenguaje popular. OBLIGATORIO "
        "incluir en CADA texto 2-3 errores ortográficos leves y variados "
        "(tildes omitidas, 'hay' por 'ay', 'haber' por 'aver', 'vez' por "
        "'ves', 'hacer' por 'aser', 'gracias' por 'grasias', 'también' por "
        "'tmb' o 'tmbn'; opcionalmente 'q' por 'que', 'pa' por 'para', 'xq' "
        "por 'porque', 'tons' y 'k' ocasionales), con MÁXIMO 1 error por "
        "palabra. PROHIBIDO ABRIR LOS SIGNOS DE INTERROGACIÓN Y EXCLAMACIÓN: "
        "NUNCA uses '¿' ni '¡' (escribe 'que paso?' en vez de '¿qué pasó?'; "
        "los cierres '?' y '!' sí se usan normales). PROHIBIDOS los errores "
        "disléxicos fuertes (confundir b/d o q/p) y cualquier falta que impida "
        "entender: el argumento debe quedar claro. Si el perfil es popular, usa "
        "el tono casual con estos mismos 2-3 errores, sin subir la intensidad."
    ),
    "ciudadana": (
        "REGISTRO: CIUDADANO/PERSONA REAL (HUMANO, NO IA PERFECTA). Escribe "
        "como una persona mexicana común 'sin estudios': lenguaje muy coloquial "
        "y emocional, cero tecnicismos y cero lenguaje de político. OBLIGATORIO "
        "en CADA texto: "
        "a) MALOS SIGNOS DE PUNTUACIÓN: casi sin comas ni puntos, frases "
        "encadenadas una tras otra, NUNCA '¿' ni '¡' (cierra normal con '?' o "
        "'!'), '...' al final a veces, mayúsculas inconsistentes y a veces "
        "inicio en minúscula; "
        "b) 4-7 errores ortográficos legibles y variados por texto, con máximo "
        "1 error por palabra: confusiones 'hay'/'ay' ('ay que ver'), "
        "'haber'/'a ver' por 'aver', 'haya' por 'haiga', b/v ('haver', "
        "'bolber'), s/c/z ('grasias', 'ves' por 'vez'), h muda omitida o de "
        "más ('ola', 'acer', 'hechar'), g/j ('jente'), y/ll ('yego', 'caye'), "
        "más abreviaturas de redes ('q', 'pa', 'xq', 'tons', 'k', 'tmb', "
        "'ntp'); "
        "c) VARIACIÓN TOTAL ENTRE TEXTOS: prohibido repetir aperturas, orden, "
        "muletillas o estructuras; alterna pregunta/afirmación/exclamación, con "
        "y sin emoji (máximo 1), longitud distinta, un texto empieza con "
        "anécdota, otro con opinión directa, otro con pregunta al aire; "
        "d) el hashtag va SIEMPRE bien escrito e intacto; "
        "e) aunque escribas con errores, el mensaje DEBE ENTENDERSE. "
        "Aplica estos rasgos SIN romper el FORMATO OBLIGATORIO del perfil (si "
        "el perfil es formal, conserva sus 3 bloques pero con este toque "
        "humano dentro)."
    ),
}


# Regla de oro del proyecto para POSTS y RETWEETS CON CITA generados por IA:
# texto final <= 100 caracteres (incluyendo hashtags y espacios). Vive aqui para
# inyectarse en TODOS los prompts de posts/citas y reutilizarse en el corte duro
# de ``ia/generador_contenido.py`` (ultima barrera, por si la IA se pasa).
REGLA_MAX_100 = (
    "REGLA DE ORO: EL TEXTO FINAL DEBE TENER UN MÁXIMO ESTRICTO DE 100 "
    "CARACTERES EN TOTAL, INCLUYENDO HASHTAGS Y ESPACIOS. SÉ MUY BREVE Y "
    "DIRECTO."
)

# Alias privado (compatibilidad con quien prefiera el guion bajo).
_REGLA_MAX_100 = REGLA_MAX_100


def _reglas_registro(registro: str = "", incluir_limite: bool = True) -> str:
    """Bloque de reglas de registro listo para concatenar (con saltos de linea).

    "politica" -> lenguaje institucional/formal.
    "activista" -> tecnico-coloquial (terminos politicos + lenguaje popular).
    "ciudadana" -> lenguaje muy coloquial de una persona mexicana real.
    Cualquier otro valor (o vacio) no agrega reglas de registro.

    ``incluir_limite`` (kwarg nuevo AL FINAL, default True): agrega SIEMPRE la
    ``REGLA_MAX_100`` (posts/citas con maximo estricto de 100 caracteres),
    incluso cuando el registro viene vacio o desconocido. Los llamadores de
    COMENTARIOS/BLOG pasan ``incluir_limite=False``: quedan FUERA del limite de
    100. Los llamadores actuales de 1 argumento siguen funcionando igual (el
    default incluye la regla).
    """
    clave = (registro or "").strip().lower()
    regla = _REGLAS_REGISTRO.get(clave)
    bloques = []
    if incluir_limite:
        bloques.append(REGLA_MAX_100)
    if regla:
        bloques.append(regla)
    if not bloques:
        return ""
    return "\n" + "\n".join(bloques) + "\n"


# --------------------------------------------------------------------------- #
# Narrativa/noticias como TRASFONDO INVISIBLE: el texto final opina con la voz
# de la cuenta, NUNCA menciona ni alude al material de referencia.
# --------------------------------------------------------------------------- #
_REGLAS_TRASFONDO = (
    "NARRATIVA/TRASFONDO = MATERIAL DE REFERENCIA INTERNO (puede venir de "
    "noticias). PROHIBIDO mencionarlo, citarlo, parafrasearlo o aludir a él: "
    "nada de medios, links, cifras, fechas, nombres propios, cargos ni frases "
    "del trasfondo. PROHIBIDO usar fórmulas como 'según la noticia', 'se "
    "informó', 'este hecho', 'ante esta situación' o 'en el contexto actual'. "
    "El texto debe leerse como una OPINIÓN/EXPERIENCIA ESPONTÁNEA de la "
    "cuenta; quien lo lea NO debe poder deducir de qué noticia salió. El "
    "CONTEXTO (si se indica) es un tema legítimo del que SÍ se opina, siempre "
    "con palabras propias y sin copiarlo literal."
)


def _reglas_trasfondo() -> str:
    """Bloque de reglas para tratar la narrativa/noticias como trasfondo invisible.

    Se agrega a los prompts que reciben ``narrativa`` (puede traer material de
    noticias) para que el texto final se lea como una opinion/experiencia
    espontanea de la cuenta y NUNCA mencione la noticia, el medio, links,
    cifras, fechas, nombres propios, cargos ni frases del trasfondo.
    Devuelve el bloque listo para concatenar (con salto de linea inicial).
    """
    return f"\n{_REGLAS_TRASFONDO}\n"


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
        "errores: con registro politica, COMO MÁXIMO UN error leve opcional "
        "(p. ej. una tilde omitida) y CERO abreviaturas; con registro "
        "activista, OBLIGATORIOS 2-3 errores ortográficos leves por texto SIN "
        "abrir '¿' ni '¡' (los cierres '?' y '!' se usan normales); con "
        "registro ciudadano, aplica el modo humano completo DENTRO de los 3 "
        "bloques (malos signos de puntuación, 4-7 errores legibles, variación "
        "entre textos, etc.) sin romper la estructura."
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
        "\n- El REGISTRO modula la intensidad humana: con registro politica, "
        "ortografía cuidada (MÁXIMO UN error leve opcional, CERO abreviaturas); "
        "con registro activista, OBLIGATORIOS 2-3 errores ortográficos leves "
        "por texto, sin abrir '¿' ni '¡' (los cierres '?' y '!' se usan "
        "normal); con registro ciudadano, aplica el modo HUMANO COMPLETO "
        "(malos signos de puntuación, 4-7 errores ortográficos legibles, "
        "abreviaturas, variación total entre textos)."
        "\n- El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
        "texto; NUNCA lo pongas al final (bien escrito, sin deformar)."
        "\nESTE FORMATO TIENE PRIORIDAD SOBRE CUALQUIER OTRA REGLA DE ESTRUCTURA."
    ),
    "popular": (
        "\nFORMATO OBLIGATORIO SEGUN EL PERFIL (Popular/Organico):"
        "\n- Texto MUY casual y CORTO: de un solo renglon."
        "\n- Con registro ciudadano: incluye FALTAS INTENSAS pero legibles: "
        "malos signos de puntuación (casi sin comas ni puntos, nunca '¿' ni "
        "'¡'), sin tildes, 'q' por 'que', 'pa' por 'para', 'xq' por 'porque', "
        "'tons', 'k' por 'que', más confusiones disléxicas (b/v, s/c/z, h "
        "muda, y/ll) y mayúsculas inconsistentes; que se note humano, no IA "
        "perfecta."
        "\n- Con registro politica: ortografía cuidada (MÁXIMO UN error leve "
        "opcional, CERO abreviaturas). Con registro activista: OBLIGATORIOS "
        "2-3 errores ortográficos leves por texto, sin abrir '¿' ni '¡'. El "
        "registro manda sobre el perfil en intensidad de errores."
        "\n- Aunque tenga faltas intencionales, el mensaje DEBE entenderse igual "
        "(máximo 1 error por palabra) y el hashtag va bien escrito."
        "\n- El texto DEBE incluir al menos un hashtag INTEGRADO EN MEDIO del "
        "texto; NUNCA lo pongas al final."
        "\nESTE FORMATO TIENE PRIORIDAD SOBRE CUALQUIER OTRA REGLA DE ESTRUCTURA."
    ),
}


def bloque_estilo_perfil(perfil: str, comentario: bool = False) -> str:
    """Bloque de instrucciones de formato segun el perfil de la cuenta.

    Acepta variantes via ``core.perfiles.normalizar_perfil`` ("Formal",
    "Popular/Orgánico", etc.). Devuelve el bloque listo para concatenar (con
    salto de linea inicial) o "" si el perfil no se reconoce.

    ``comentario`` (kwarg nuevo AL FINAL, opcional): para COMENTARIOS/
    RESPUESTAS el bloque cambia la regla de "hashtag obligatorio en medio" por
    su prohibicion (X marca como spam las respuestas con hashtags, links o
    @menciones); el resto del formato del perfil se conserva intacto. Default
    False = comportamiento anterior identico para posts/citas.
    """
    try:
        clave = normalizar_perfil(perfil)
    except Exception:
        return ""
    bloque = _BLOQUES_ESTILO_PERFIL.get(clave, "")
    if not bloque or not comentario:
        return bloque
    return re.sub(
        r"- El texto DEBE incluir al menos un hashtag[^.]*\.",
        "- NUNCA uses hashtags, links ni @menciones: es una respuesta breve.",
        bloque,
    )


# Instrucciones por tema para el mantenimiento organico de cuentas.
# "personalidad" es un texto libre generado por otro modulo (cuentas/).
def _instruccion_tema(titulo: str, guias) -> str:
    """Instruccion compacta y ESPECIFICA de un tema cotidiano (nunca generica).

    Formato: "TEMA: <titulo>." + 1-2 guias propias + la guia comun de tono
    (cotidiano y cercano, sin inventar datos verificables ni meterse en
    politica). Se usa para construir ``_INSTRUCCIONES_TEMA`` sin duplicar texto.
    """
    lineas = [f"TEMA: {titulo}."]
    lineas.extend(f"- {g}" for g in guias)
    lineas.append(
        "- Tono cotidiano, cercano y en primera persona; SIN inventar datos "
        "verificables ni meterse en política."
    )
    return "\n".join(lineas)


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

# Temas NUEVOS (mas de 30 cotidianos y seguros): cada uno con su instruccion
# propia. El orden del tuple alterna FAMILIAS de vocabulario para que los
# textos consecutivos de una misma cuenta (temas rotativos) nunca caigan en la
# misma familia (y por tanto no compartan palabras clave).
_INSTRUCCIONES_TEMA.update({
    "trafico": _instruccion_tema(
        "TRAFICO / TRASLADOS VIALES",
        (
            "Habla del trafico y los traslados cotidianos (horas pico, "
            "avenidas, esperas) con humor o resignacion, en primera persona.",
            "Puedes compartir un truco del trayecto o recomendar salir "
            "temprano; nada de cifras ni datos oficiales.",
        ),
    ),
    "clima": _instruccion_tema(
        "CLIMA / TIEMPO DEL DIA",
        (
            "Comenta el clima del dia (calor, frio, viento, cielo) y como te "
            "afecta en lo cotidiano.",
            "Evita pronosticos o datos exactos: habla de lo que sientes y de "
            "los planes que cambian.",
        ),
    ),
    "comida": _instruccion_tema(
        "COMIDA Y ANTOJOS",
        (
            "Habla de antojos, platillos de casa y comida cotidiana con gusto "
            "y cercania.",
            "Puedes preguntar por el platillo favorito de la gente o contar que "
            "se te antojo hoy.",
        ),
    ),
    "series": _instruccion_tema(
        "SERIES Y TELEVISION",
        (
            "Comenta series, capitulos o maratones recientes y pide "
            "recomendaciones sin destripar tramas.",
            "Tono de sobremesa: que estas viendo, que te engancho y que verias "
            "otra vez.",
        ),
    ),
    "lunes": _instruccion_tema(
        "LUNES / INICIO DE SEMANA",
        (
            "Habla del inicio de semana con humor: flojera, cafe, pendientes y "
            "ganas de arrancar.",
            "Mantente en la vida diaria: el lunes es tema de animo, no de "
            "politica.",
        ),
    ),
    "insomnio": _instruccion_tema(
        "INSOMNIO / NO PODER DORMIR",
        (
            "Comenta las noches sin dormir, el celular a deshoras o la mente "
            "dando vueltas.",
            "Tono honesto y ligero; puedes preguntar que hace la gente cuando "
            "no puede dormir.",
        ),
    ),
    "futbol": _instruccion_tema(
        "FUTBOL / PELOTA",
        (
            "Habla de futbol como pasion cotidiana: partidos, porras, jugadas y "
            "recuerdos.",
            "Sin datos ni resultados inventados: mejor la emocion, la porra y "
            "lo que se vive en casa.",
        ),
    ),
    "memes": _instruccion_tema(
        "MEMES / HUMOR DIGITAL",
        (
            "Conversa sobre memes y humor de internet: lo que te dio risa hoy o "
            "el meme que anda en el grupo.",
            "Tono ligero y respetuoso; nada de burlas a personas ni temas "
            "sensibles.",
        ),
    ),
    "motivacion": _instruccion_tema(
        "MOTIVACION / ANIMO",
        (
            "Comparte animo y frases motivacionales propias para empezar el dia "
            "o seguir adelante.",
            "Habla desde lo personal: un aprendizaje, un proposito o una meta "
            "pequena.",
        ),
    ),
    "mascotas": _instruccion_tema(
        "MASCOTAS",
        (
            "Habla de perros, gatos o mascotas de casa: travesuras, paseos y "
            "compania.",
            "Tono tierno y cotidiano; puedes preguntar por las mascotas de la "
            "gente.",
        ),
    ),
    "cafe": _instruccion_tema(
        "CAFE / RITUAL DE LA MANANA",
        (
            "Habla del cafe y su ritual diario: el de la manana, la platica, el "
            "descanso.",
            "Puedes preguntar como lo toman o contar el momento del dia en que "
            "mas sabe.",
        ),
    ),
    "musica": _instruccion_tema(
        "MUSICA / CANCIONES",
        (
            "Comenta musica y canciones que te acompanan: generos, recuerdos, "
            "playlists.",
            "Pide recomendaciones o cuenta que cancion no te cansas de "
            "escuchar.",
        ),
    ),
    "vecinos": _instruccion_tema(
        "VECINOS / CONVIVENCIA",
        (
            "Habla de la vida con los vecinos: convivencia, ruidos, favores y "
            "detalles.",
            "Tono amable, sin conflictos ni quejas fuertes.",
        ),
    ),
    "transporte": _instruccion_tema(
        "TRANSPORTE PUBLICO",
        (
            "Comenta el transporte publico del dia a dia: camiones, metro, bici "
            "o caminatas.",
            "Tono de experiencia propia; sin tarifas ni cifras.",
        ),
    ),
    "filas": _instruccion_tema(
        "FILAS Y ESPERAS",
        (
            "Habla de las filas y esperas cotidianas (banco, super, tramites) "
            "con humor.",
            "Puedes contar un truco para hacer la espera mas amena.",
        ),
    ),
    "calor": _instruccion_tema(
        "CALOR / TEMPORADA",
        (
            "Comenta el calor de la temporada y como lo llevas: sombra, agua, "
            "ventilador.",
            "Tono de queja amable y cotidiana; sin datos meteorologicos.",
        ),
    ),
    "lluvia": _instruccion_tema(
        "LLUVIA",
        (
            "Habla de la lluvia: lo que cambio el plan, el olor a tierra y los "
            "charcos.",
            "Tono sensorial y cercano; puedes preguntar si a la gente le gusta "
            "la lluvia.",
        ),
    ),
    "finde": _instruccion_tema(
        "FIN DE SEMANA",
        (
            "Celebra o planea el fin de semana: descanso, salida, familia o "
            "casa.",
            "Pregunta a la gente por sus planes sin prometer eventos.",
        ),
    ),
    "deportes": _instruccion_tema(
        "DEPORTES / EJERCICIO",
        (
            "Habla de deportes en general (correr, bici, basquet, gimnasio) "
            "como parte del dia.",
            "Comparte animo y habitos; sin marcas ni datos verificables.",
        ),
    ),
    "recuerdos": _instruccion_tema(
        "RECUERDOS / INFANCIA",
        (
            "Comparte recuerdos bonitos de la infancia, la escuela o el barrio.",
            "Tono nostalgico y calido; invita a la gente a contar los suyos.",
        ),
    ),
    "escuela": _instruccion_tema(
        "ESCUELA / ESTUDIO",
        (
            "Habla de la escuela y el estudio: clases, examenes, amigos y "
            "aprendizajes.",
            "Puedes recordar tu epoca escolar o animar a quien estudia.",
        ),
    ),
    "trabajo": _instruccion_tema(
        "TRABAJO / CHAMBA",
        (
            "Comenta la vida laboral cotidiana: pendientes, juntas y orgullo "
            "por el esfuerzo.",
            "Tono cercano y humano; sin mencionar empresas ni puestos "
            "concretos.",
        ),
    ),
    "home_office": _instruccion_tema(
        "HOME OFFICE / TRABAJO DESDE CASA",
        (
            "Habla del trabajo desde casa: pijama, cafe, pausas y convivencia.",
            "Tono relajado y honesto sobre sus ventajas y retos.",
        ),
    ),
    "super": _instruccion_tema(
        "SUPER / MERCADO",
        (
            "Comenta la ida al super o al mercado: la lista, los precios y lo "
            "que se antojo.",
            "Sin cifras ni marcas: habla de la experiencia y de los antojos.",
        ),
    ),
    "cocina": _instruccion_tema(
        "COCINA / RECETAS DE CASA",
        (
            "Habla de cocinar en casa: recetas sencillas, olores y lo que se te "
            "quemo.",
            "Puedes compartir un truco de cocina o preguntar que se cocina hoy.",
        ),
    ),
    "postres": _instruccion_tema(
        "POSTRES / PAN DULCE",
        (
            "Habla de postres y pan dulce: lo dulce de la tarde, el antojo y la "
            "sobremesa.",
            "Tono goloso y alegre; pregunta por el postre favorito.",
        ),
    ),
    "tacos": _instruccion_tema(
        "TACOS / ANTOJITOS",
        (
            "Habla de tacos y antojitos mexicanos como plan cotidiano.",
            "Tono sabroso y de barrio; sin resenar locales concretos.",
        ),
    ),
    "netflix": _instruccion_tema(
        "NETFLIX / PLATAFORMAS",
        (
            "Comenta lo que estas viendo en plataformas: estrenos, maratones y "
            "recomendaciones.",
            "Sin destripar tramas ni inventar titulos: habla de generos y "
            "sensaciones.",
        ),
    ),
    "videojuegos": _instruccion_tema(
        "VIDEOJUEGOS / PARTIDAS",
        (
            "Habla de videojuegos y partidas: retos, nostalgia y jugar con "
            "amigos.",
            "Puedes preguntar a que juega la gente o recordar un juego clasico.",
        ),
    ),
    "podcast": _instruccion_tema(
        "PODCAST / AUDIO",
        (
            "Comenta podcasts que escuchas: temas, voces y momentos para "
            "oirlos.",
            "Recomienda sin inventar nombres: habla del tipo de contenido.",
        ),
    ),
    "libros": _instruccion_tema(
        "LIBROS / LECTURA",
        (
            "Habla de libros y lectura: lo que estas leyendo, generos y frases "
            "que te marcaron.",
            "Sin citar textualmente; comparte que te hace sentir leer.",
        ),
    ),
    "viajes": _instruccion_tema(
        "VIAJES / VACACIONES",
        (
            "Habla de viajes y ganas de conocer: planes, maletas y lugares "
            "sonados.",
            "Sin datos turisticos exactos; mejor la ilusion y los preparativos.",
        ),
    ),
    "carretera": _instruccion_tema(
        "CARRETERA / CAMINO",
        (
            "Comenta los viajes por carretera: paisajes, paradas y musica a "
            "todo volumen.",
            "Tono aventurero y cotidiano; sin rutas ni cifras.",
        ),
    ),
    "playa": _instruccion_tema(
        "PLAYA / MAR",
        (
            "Habla de la playa: el mar, la arena, el descanso y la familia.",
            "Tono fresco y relajado; sin promociones turisticas.",
        ),
    ),
    "barrio": _instruccion_tema(
        "BARRIO / COLONIA",
        (
            "Habla del barrio o la colonia: calles, negocios, gente y "
            "tradiciones.",
            "Tono de pertenencia y carino; sin conflictos ni politica.",
        ),
    ),
    "familia": _instruccion_tema(
        "FAMILIA / CASA",
        (
            "Comenta la vida en familia: comidas, platicas, apoyo y rutinas.",
            "Tono calido; puedes compartir un momento familiar sencillo.",
        ),
    ),
    "abuelos": _instruccion_tema(
        "ABUELOS / MAYORES",
        (
            "Habla de los abuelos y los mayores: ensenanzas, historias y "
            "carino.",
            "Tono respetuoso y nostalgico; invita a valorar a los mayores.",
        ),
    ),
    "amistad": _instruccion_tema(
        "AMISTAD / AMIGOS",
        (
            "Habla de la amistad: amigas y amigos, platicas y apoyo en las "
            "buenas y en las malas.",
            "Tono cercano; puedes dedicar el texto a alguien sin usar "
            "@menciones.",
        ),
    ),
})

# Temas de mantenimiento (una sola fuente de verdad). Orden pensado para que
# los temas CONSECUTIVOS sean de familias de vocabulario DISTINTAS: con la
# rotacion por (cuenta, texto) los dos textos de una misma cuenta nunca caen
# en la misma familia. El orden NO debe aleatorizarse: `web/operaciones/posts.py`
# etiqueta el plan con `temas[(i * n + j) % len(temas)]` y debe coincidir.
TEMAS_MANTENIMIENTO = (
    # Legacy (retrocompatibilidad; sus instrucciones no cambian)
    "azteca",
    "trafico",
    "clima",
    "comida",
    "series",
    "lunes",
    "futbol",
    "memes",
    "motivacion",
    "mascotas",
    "dia",
    "transporte",
    "calor",
    "cafe",
    "netflix",
    "insomnio",
    "deportes",
    "gustos",
    "filas",
    "lluvia",
    "super",
    "videojuegos",
    "finde",
    "tendencias",
    "carretera",
    "cocina",
    "podcast",
    "escuela",
    "musica",
    "vecinos",
    "postres",
    "libros",
    "trabajo",
    "barrio",
    "tacos",
    "home_office",
    "familia",
    "amistad",
    "playa",
    "recuerdos",
    "viajes",
    "abuelos",
)


def _normalizar_tema(tema: str = "") -> str:
    """Normaliza el nombre del tema (minusculas, sin acentos y con alias).

    Robusto a variantes reales: quita acentos/diacriticos con ``unicodedata``,
    colapsa separadores y acepta alias, plurales y sinonimos cotidianos
    ("tráfico"->trafico, "clima local"->clima, "antojo"/"antojos"->comida,
    "series de tv"/"tv"->series, "no puedo dormir"->insomnio, "fútbol"->futbol,
    "frases"/"motivacionales"->motivacion, "perros"/"gatos"->mascotas,
    "home office"->home_office, etc.). Los temas personalizados desconocidos
    se devuelven normalizados; "" si no queda nada util.
    """
    raw = str(tema or "").strip().lower()
    if not raw:
        return ""
    plano = unicodedata.normalize("NFKD", raw)
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    plano = plano.replace("ñ", "n")
    plano = re.sub(r"[^a-z0-9]+", " ", plano).strip()
    if not plano:
        return ""
    alias = {
        # legacy
        "prehispanico": "azteca",
        "prehispanica": "azteca",
        "mexica": "azteca",
        "mexicas": "azteca",
        "aztecas": "azteca",
        "tenochtitlan": "azteca",
        "raices": "azteca",
        "actualidad": "dia",
        "diario": "dia",
        "efemerides": "dia",
        "tendencia": "tendencias",
        "trending": "tendencias",
        "viral": "tendencias",
        "gusto": "gustos",
        "cotidiano": "gustos",
        "intereses": "gustos",
        "hobbies": "gustos",
        "pasatiempos": "gustos",
        # trafico / traslados
        "traficos": "trafico",
        "trafico vehicular": "trafico",
        # clima
        "clima local": "clima",
        "tiempo": "clima",
        "pronostico": "clima",
        # comida / antojos
        "comidas": "comida",
        "antojo": "comida",
        "antojos": "comida",
        "comida de casa": "comida",
        # series / tv
        "serie": "series",
        "series de tv": "series",
        "serie de tv": "series",
        "tv": "series",
        "television": "series",
        # lunes / descanso
        "inicio de semana": "lunes",
        "comienzo de semana": "lunes",
        # insomnio
        "no puedo dormir": "insomnio",
        "desveladas": "insomnio",
        "desvelo": "insomnio",
        # futbol
        "fut": "futbol",
        "partido": "futbol",
        # memes
        "meme": "memes",
        "humor": "memes",
        # motivacion
        "motivacional": "motivacion",
        "motivacionales": "motivacion",
        "frases": "motivacion",
        "frases motivacionales": "motivacion",
        "inspiracion": "motivacion",
        "animo": "motivacion",
        # mascotas
        "mascota": "mascotas",
        "perros": "mascotas",
        "perro": "mascotas",
        "gatos": "mascotas",
        "gato": "mascotas",
        "michis": "mascotas",
        # cafe / musica / vecinos
        "cafecito": "cafe",
        "desayuno": "cafe",
        "canciones": "musica",
        "cancion": "musica",
        "vecino": "vecinos",
        "vecindad": "vecinos",
        # transporte / filas
        "transporte publico": "transporte",
        "camion": "transporte",
        "camiones": "transporte",
        "metro": "transporte",
        "pesero": "transporte",
        "fila": "filas",
        # calor / lluvia
        "calorones": "calor",
        "lluvias": "lluvia",
        "llover": "lluvia",
        "llueve": "lluvia",
        "temporal": "lluvia",
        # finde / deportes / recuerdos
        "fin de semana": "finde",
        "deporte": "deportes",
        "gimnasio": "deportes",
        "correr": "deportes",
        "recuerdo": "recuerdos",
        "memoria": "recuerdos",
        "infancia": "recuerdos",
        # escuela / trabajo / home office
        "clases": "escuela",
        "tarea": "escuela",
        "estudiar": "escuela",
        "universidad": "escuela",
        "chamba": "trabajo",
        "oficina": "trabajo",
        "home office": "home_office",
        "homeoffice": "home_office",
        "teletrabajo": "home_office",
        "trabajo desde casa": "home_office",
        # super / cocina / postres / tacos
        "mercado": "super",
        "mandado": "super",
        "despensa": "super",
        "abarrotes": "super",
        "cocinar": "cocina",
        "receta": "cocina",
        "recetas": "cocina",
        "postre": "postres",
        "pan dulce": "postres",
        "taco": "tacos",
        "taquitos": "tacos",
        "antojitos": "tacos",
        # pantallas / juegos
        "streaming": "netflix",
        "videojuego": "videojuegos",
        "juegos": "videojuegos",
        "gaming": "videojuegos",
        "podcasts": "podcast",
        "libro": "libros",
        "lectura": "libros",
        "leer": "libros",
        # viajes / carretera / playa
        "viaje": "viajes",
        "vacaciones": "viajes",
        "camino": "carretera",
        "mar": "playa",
        # barrio / familia / abuelos / amistad
        "colonia": "barrio",
        "familiar": "familia",
        "abuelo": "abuelos",
        "abuela": "abuelos",
        "amigos": "amistad",
        "amigas": "amistad",
    }
    return alias.get(plano, plano.replace(" ", "_"))


def instrucciones_tema(tema: str = "") -> str:
    """Devuelve las instrucciones del tema pedido ('' si no hay tema).

    Reutilizable por otros modulos para no duplicar los textos. Todos los
    temas de ``TEMAS_MANTENIMIENTO`` tienen instruccion PROPIA; el texto
    generico "TEMA A TRATAR: ..." queda SOLO para temas personalizados
    desconocidos.
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
    max_chars: int = 100,
    estructura: bool = True,
    hashtags: bool = True,
) -> str:
    """Reglas de formato comunes a todos los tipos de contenido.

    El default es 100 caracteres (regla de oro de posts/citas); los formatos
    fuera de ese alcance pasan su propio ``max_chars`` explicito (blog 600,
    comentario 200).

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
        + _reglas_registro(registro, incluir_limite=False)
        + f"\n{REGLA_MAX_100}\n"
        + "\nTIPO DE CONTENIDO: REPOSTEO CON CITA.\n"
        "TAREA: lograr que la audiencia comparta el contenido.\n"
        "- Invita a dar RT, citar el tuit o compartir en otras redes.\n"
        "- Textos cortos, directos y con llamado explicito a compartir.\n"
        "- Cada texto debe invitar a compartir de una forma distinta.\n"
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad, max_chars=100)
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
        + _reglas_registro(registro, incluir_limite=False)
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


# --------------------------------------------------------------------------- #
# Tweet ancla real: el TEMA PRINCIPAL de la respuesta. El motor pasa el texto
# del tweet al que responde cada comentario y se usa como tema (nunca como
# material copiable, y NUNCA via `narrativa`: la red anti-fuga lo reemplazaria).
# --------------------------------------------------------------------------- #
_MAX_TWEET_ANCLA = 400


def texto_tweet_ancla(
    tweet_ancla_texto: str = "", max_chars: int = _MAX_TWEET_ANCLA
) -> str:
    """Normaliza (espacios) y trunca el texto del tweet ancla; nunca lanza.

    Devuelve "" si no hay texto util. Reutilizable por otros modulos (p. ej.
    ``ia/generador_contenido.py``) para no duplicar la normalizacion.
    """
    try:
        t = " ".join(str(tweet_ancla_texto or "").split())
    except Exception:
        return ""
    if not t:
        return ""
    try:
        limite = max(1, int(max_chars))
    except (TypeError, ValueError):
        limite = _MAX_TWEET_ANCLA
    if len(t) > limite:
        t = t[:limite].rstrip()
    return t


def bloque_tweet_ancla(
    tweet_ancla_texto: str = "", max_chars: int = _MAX_TWEET_ANCLA
) -> str:
    """Bloque de prompt que fija el TEMA PRINCIPAL: el tweet real al que responde.

    Devuelve "" si no hay texto. El ancla es SOLO tema: prohibe repetirla
    literalmente, parafrasearla plano, mencionar "el tweet" y citar medios o
    fuentes; ademas declara que ese tema manda sobre la narrativa/contexto de
    campana. Se usa en ``get_prompt_comentario`` y en el prompt de lotes de
    comentarios de ``ia/generador_contenido.py``.
    """
    ancla = texto_tweet_ancla(tweet_ancla_texto, max_chars)
    if not ancla:
        return ""
    return (
        "\nEL TWEET AL QUE RESPONDES DICE: «{ancla}».\n"
        "TAREA: responde A ESE CONTENIDO (opina, reacciona, coincide o "
        "discrepa) usando ese tema como base; NO lo repitas literalmente, NO "
        "lo parafrasees plano, NO menciones \"el tweet\", NO cites medios ni "
        "fuentes.\n"
        "IMPORTANTE: este es el TEMA PRINCIPAL de la respuesta y MANDA sobre "
        "cualquier narrativa o contexto de campaña.\n"
    ).format(ancla=ancla)


def get_prompt_comentario(
    narrativa: str,
    entrenamiento: str = "",
    cantidad: int = 0,
    registro: str = "",
    perfil: str = "",
    tema: str = "",
    personalidad: str = "",
    tweet_ancla_texto: str = "",
) -> str:
    """Prompt para COMENTARIOS/RESPUESTAS breves y conversacionales.

    Mismos bloques que mantenimiento (narrativa, registro, tema, personalidad
    y formato por perfil) pero los textos son aptos para responder: mas
    breves, directos y conversacionales, SIN hashtags, links ni @menciones
    (X marca las respuestas con esos elementos como probable spam). El perfil
    formal conserva Titulo/Descripcion/Conclusion en version corta.

    ``tweet_ancla_texto`` (kwarg nuevo AL FINAL, retrocompatible): TEXTO REAL
    del tweet al que se responde. Si viene no vacio se agrega como TEMA
    PRINCIPAL (truncado a 400 chars) al que debe reaccionar el comentario,
    por encima de la narrativa/contexto de campana, sin copiarlo ni
    parafrasearlo y sin mencionar "el tweet" ni citar medios/fuentes.
    """
    return (
        _base_narrativa(narrativa, entrenamiento)
        + (_reglas_trasfondo() if narrativa else "")
        + _reglas_registro(registro, incluir_limite=False)
        + "\nTIPO DE CONTENIDO: COMENTARIO/RESPUESTA BREVE.\n"
        "TAREA: comentar o responder una publicacion de forma breve y natural.\n"
        "- Los textos son para responder: directos, conversacionales y con "
        "reaccion u opinion propia.\n"
        "- NO uses hashtags, ni links, ni @menciones (X marca las respuestas "
        "con esos elementos como probable spam).\n"
        "- Evita frases de relleno repetidas; aporta un angulo distinto y "
        "conversacional.\n"
        "- NO repitas la publicacion original ni empieces con 'En respuesta a'.\n"
        "- Cada texto debe aportar un angulo distinto y sonar humano.\n"
        "- Son mas breves que un post normal: 1-2 frases (el perfil formal "
        "mantiene Titulo/Descripcion/Conclusion pero MUY cortos).\n"
        + bloque_tweet_ancla(tweet_ancla_texto)
        + _bloque_tema_personalidad(tema, personalidad)
        + _reglas_formato(cantidad, max_chars=200, estructura=False, hashtags=False)
        + bloque_estilo_perfil(perfil, comentario=True)
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
        + (_reglas_trasfondo() if narrativa else "")
        + _reglas_registro(registro, incluir_limite=False)
        + f"\n{REGLA_MAX_100}\n"
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
        "- EXTENSION: MÁXIMO ESTRICTO de 100 caracteres por texto "
        "(incluyendo hashtags y espacios). El texto debe quedar COMPLETO y con "
        "sentido (incluida su frase de cierre), nunca cortado a la mitad.\n"
        + _bloque_tema_personalidad("", personalidad)
    )
    if hashtags:
        prompt += (
            "\nHASHTAGS OBLIGATORIOS:\n"
            f"- Usa SOLO hashtags de esta lista (bien escritos): {hashtags}\n"
            "- PROHIBIDO inventar o agregar cualquier otro hashtag distinto.\n"
            "- Puedes usar UNO, DOS o TODOS los de la lista (elige la cantidad "
            "segun el texto); lo importante es que todos los que uses salgan "
            "de esa lista.\n"
            "- Integrados EN MEDIO del texto, de forma NATURAL: al final de "
            "una frase o despues de una coma/punto cercano a la mitad; nunca "
            "entre un articulo y su sustantivo, nunca partiendo la frase y "
            "nunca al final del texto.\n"
        )
    if contexto:
        prompt += (
            "\nCONTEXTO (tema legítimo sobre el que SÍ debes opinar con tus "
            "propias palabras; es distinto del trasfondo de noticias y NO "
            "debes copiar su redacción):\n"
            f"{contexto}"
        )
    prompt += _reglas_formato(cantidad, max_chars=100)
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


def get_prompt_agrupar_temas() -> str:
    return (
        "Agrupa los siguientes titulares en temas similares.\n"
        "Responde con JSON: {\"temas\": [{\"titulo\": \"...\", \"cantidad\": N}]}\n"
        "Maximo 5 temas principales."
    )


def get_prompt_resumen_ejecutivo() -> str:
    """Prompt del resumen ejecutivo de titulares (5-10 lineas, sin inventar).

    Se usa en ``ia.filtros_alertas.FiltrosAlertas.resumir`` con la lista de
    titulares pegada al final. Pide texto plano en espanol, breve, que integre
    los titulares sin copiarlos uno por uno y sin agregar datos que no esten
    en ellos. No incluye reglas de hashtags/registro: es un resumen interno
    para el dashboard, no un post.
    """
    return (
        "Eres un analista que redacta resúmenes ejecutivos breves y objetivos.\n"
        "TAREA: resume los titulares que se listan abajo en un texto de 5 a 10 "
        "líneas.\n"
        "REGLAS:\n"
        "- Escribe en español, claro y directo.\n"
        "- NO inventes datos, cifras, fechas, nombres, lugares ni hechos que no "
        "aparezcan en los titulares.\n"
        "- NO copies los titulares uno por uno: intégralos en un resumen con "
        "sentido y destaca los temas o asuntos que más se repiten.\n"
        "- NO uses markdown raro (nada de tablas, bloques de código, encabezados "
        "con # ni listas numeradas): solo texto plano.\n"
        "- Devuelve ÚNICAMENTE el resumen, sin títulos, etiquetas ni comentarios "
        "extra."
    )


# --------------------------------------------------------------------------- #
# Identidades (nombres y @) por contexto: Seccion + Registro + tipo.
# `cuentas/generador_identidades.py` importa `get_prompt_identidades_contexto`
# para inyectar estas reglas cruzadas en su prompt de generacion masiva.
# --------------------------------------------------------------------------- #
_IDENTIDADES_HEADER = (
    "REGLAS DE IDENTIDAD POR CONTEXTO (MANDAN SOBRE EL TIPO BASE CUANDO "
    "APLIQUEN):\n"
)

_PROHIBICIONES_IDENTIDAD = (
    "PROHIBICIONES GLOBALES: nada de siglas ni nombres de partidos (MC, "
    "Morena, PAN, PRI, PRD, PVEM, PT, PES) ni de políticos famosos; los "
    "handles deben ser válidos para X (4-15 caracteres, solo letras, números y "
    "guion bajo, sin acentos, sin espacios y sin '@').\n"
)

_REGLA_IDENT_CIUDADANA = (
    "REGISTRO CIUDADANA (IGNORA LA SECCION, CUALQUIERA QUE SEA): los nombres "
    "deben ser de personas mexicanas 100% reales y coloquiales (estilo Faker "
    "es_MX; p. ej. \"Lupita Hernández\", \"Juan Pérez\", \"María López\"); los "
    "handles van con nombre + apellido o nombre + inicial (p. ej. "
    "\"LupitaHernandez\", \"JuanP\"). PROHIBIDO el tono institucional, "
    "corporativo o de causa social: se ignora la sección por completo y se "
    "escribe como una persona común.\n"
)

_REGLA_IDENT_IP_POLITICA = (
    "SECCIÓN IP (Institución Privada) + REGISTRO POLITICA: nombres y handles "
    "FORMALES, corporativos o de analista serio; usa display names tipo "
    "\"Análisis ...\", \"Consultoría ...\" o \"Centro de ...\"; handles sobrios "
    "y profesionales (ejemplos válidos: @AnalisisIP, @ConsultoriaDatos); tono "
    "institucional, técnico y mesurado. PROHIBIDO: siglas o nombres de "
    "partidos y políticos famosos.\n"
)

_REGLA_IDENT_CI_POLITICA = (
    "SECCIÓN CI (Ciudadanía) + REGISTRO POLITICA: tono institucional y cercano "
    "a lo ciudadano; display names sobrios de institución cívica o colectivo "
    "ciudadano (p. ej. \"Centro Cívico ...\", \"Ciudadanía Activa ...\", "
    "\"Observatorio Ciudadano ...\"), sin simbología ni siglas de partidos.\n"
)

_REGLA_IDENT_LIB_POLITICA = (
    "SECCIÓN LIB (Libertad) + REGISTRO POLITICA: institucional y formal con "
    "sensibilidad por las libertades y los derechos; display names elegantes y "
    "serios (p. ej. \"Centro por la Libertad ...\", \"Observatorio Libre "
    "...\"); sin consignas partidistas ni nombres de políticos.\n"
)

_REGLA_IDENT_JUS_POLITICA = (
    "SECCIÓN JUS (Justicia) + REGISTRO POLITICA: institucional y formal con "
    "sensibilidad por la justicia, la legalidad y los derechos; display names "
    "serios (p. ej. \"Centro de Justicia ...\", \"Observatorio Jurídico "
    "...\"); sin consignas partidistas ni nombres de políticos.\n"
)

_REGLA_IDENT_POLITICA_GENERAL = (
    "REGISTRO POLITICA (sin sección): nombres y handles formales e "
    "institucionales, serios y mesurados, sin coloquialismos; PROHIBIDO siglas "
    "o nombres de partidos y políticos famosos.\n"
)

_REGLA_IDENT_LIBJUS_ACTIVISTA = (
    "SECCIÓN LIB/JUS + REGISTRO ACTIVISTA: seudónimos combativos o de causas "
    "sociales, con fuerza; display names tipo \"Voz Libertad\", \"Justicia Ya\" "
    "o \"Fuerza ...\"; handles con causa (ejemplos válidos: @VozLibertad, "
    "@JusticiaYa); PROHIBIDO nombrar partidos o políticos.\n"
)

_REGLA_IDENT_IP_ACTIVISTA = (
    "SECCIÓN IP (Institución Privada) + REGISTRO ACTIVISTA: la causa se expresa "
    "de forma SERIA e institucional (analista o institución con causa), sin "
    "agresividad ni consignas; display names tipo \"Centro de Análisis ...\" o "
    "\"Consultoría con Causa ...\"; PROHIBIDO nombrar partidos o políticos.\n"
)

_REGLA_IDENT_CI_ACTIVISTA = (
    "SECCIÓN CI (Ciudadanía) + REGISTRO ACTIVISTA: activismo ciudadano cercano "
    "y movilizador; nombres de colectivo o de persona comprometida con la "
    "causa, sin tono corporativo; PROHIBIDO nombrar partidos o políticos.\n"
)

_REGLA_IDENT_ACTIVISTA_GENERAL = (
    "REGISTRO ACTIVISTA (sin sección): seudónimos combativos o de causa social, "
    "cercanos y movilizadores; PROHIBIDO nombrar partidos o políticos.\n"
)

# Default sensato por seccion cuando NO hay registro definido (neutro).
_REGLAS_IDENT_SECCION = {
    "IP": (
        "SECCIÓN IP (sin registro): identidades neutras corporativas o de "
        "analista serio (p. ej. \"Análisis ...\", \"Consultoría ...\", "
        "\"Centro de ...\"); nada de siglas o nombres de partidos ni políticos "
        "famosos.\n"
    ),
    "CI": (
        "SECCIÓN CI (sin registro): identidades neutras de ciudadanía "
        "(personas o colectivos ciudadanos), cercanas y sin tono "
        "partidista.\n"
    ),
    "LIB": (
        "SECCIÓN LIB (sin registro): identidades neutras con sensibilidad por "
        "la libertad y los derechos, sin agresividad ni tono partidista.\n"
    ),
    "JUS": (
        "SECCIÓN JUS (sin registro): identidades neutras con sensibilidad por "
        "la justicia y la legalidad, sin agresividad ni tono partidista.\n"
    ),
}

# Reglas del tipo base. Las reglas de contexto de arriba MANDAN sobre ellas.
_REGLAS_IDENT_TIPO = {
    "partido": (
        "TIPO SIMILITUD DE PARTIDO: conserva los COLORES y SÍMBOLOS cotidianos "
        "(p. ej. Naranja, Guinda, Azul, Rojo, Amarillo, Sol, Marea, Corriente, "
        "Faro, Bolillos) para EVOCAR una identidad política; NUNCA nombres ni "
        "siglas de partidos (MC, Morena, PAN, PRI, PRD, PVEM, PT, PES) ni "
        "políticos famosos; las reglas de contexto de arriba MANDAN sobre este "
        "tipo base.\n"
    ),
    "movimiento": (
        "TIPO MOVIMIENTO: vocabulario ciudadano y organizado (Ciudadanía, "
        "Ciudad, Voces, Gente, Pueblo, Comunidad, Vecinos, Unión, Corazón), "
        "variado y sin partidos; si las reglas de contexto de arriba mandan "
        "otra cosa (p. ej. ciudadana = personas reales), el contexto tiene "
        "prioridad.\n"
    ),
    "persona": (
        "TIPO PERSONA: nombres humanos y coloquiales de personas mexicanas "
        "reales, con handles de nombre + apellido o nombre + inicial; salvo que "
        "las reglas de contexto de arriba manden otra cosa (p. ej. IP + "
        "política o LIB/JUS + activista), en cuyo caso el contexto tiene "
        "prioridad.\n"
    ),
    "mixto": (
        "TIPO MIXTO: combina identidades de persona con similitudes de partido "
        "o movimientos; a cada identidad le aplican las reglas de contexto y el "
        "estilo del tipo que le toque.\n"
    ),
}

_REGLA_IDENT_NEUTRA = (
    "SIN CONTEXTO DE SECCIÓN/REGISTRO/TIPO: identidades neutras, variadas y "
    "creíbles, sin tono partidista.\n"
)


def _texto_clave_identidades(valor) -> str:
    """Pasa un valor a minúsculas sin acentos para comparar; nunca lanza."""
    try:
        texto = " ".join(str(valor or "").split()).lower()
        plano = unicodedata.normalize("NFKD", texto)
        return "".join(c for c in plano if not unicodedata.combining(c))
    except Exception:
        return ""


def _seccion_identidad(valor) -> str:
    """Código de sección (CI/IP/LIB/JUS) tolerante a fallos de normalización.

    Normaliza con ``core.secciones.normalizar_seccion`` (import local dentro de
    try/except); si core falla o no reconoce el valor, cae a palabras clave y,
    como última red, usa el texto tal cual (la decisión final la toma quien
    construye el bloque). Nunca lanza.
    """
    raw = str(valor or "").strip()
    if not raw:
        return ""
    try:
        from core.secciones import normalizar_seccion
    except Exception:
        normalizar_seccion = None
    if normalizar_seccion is not None:
        try:
            codigo = normalizar_seccion(raw)
            if codigo:
                return codigo
        except Exception:
            pass
    # Fallback tolerante: texto tal cual con ayuda de palabras clave
    # ("Institución Privada" -> IP, "Libertad" -> LIB, "Justicia" -> JUS...).
    clave = _texto_clave_identidades(raw)
    codigos = {"ci": "CI", "ip": "IP", "lib": "LIB", "jus": "JUS"}
    if clave in codigos:
        return codigos[clave]
    if "institucion" in clave or "privad" in clave:
        return "IP"
    if "libertad" in clave:
        return "LIB"
    if "justicia" in clave:
        return "JUS"
    if "ciudadan" in clave:
        return "CI"
    return raw


def _registro_identidad(valor) -> str:
    """Registro (politica/activista/ciudadana) tolerante a fallos de core.

    Normaliza con ``core.registros.normalizar_tipo_cuenta`` (import local
    dentro de try/except); si core falla, replica su orden de prioridad
    (exactos de activista antes de los prefijos "ciudadan"/"politic"). Nunca
    lanza.
    """
    raw = str(valor or "").strip()
    if not raw:
        return ""
    try:
        from core.registros import normalizar_tipo_cuenta
    except Exception:
        normalizar_tipo_cuenta = None
    if normalizar_tipo_cuenta is not None:
        try:
            codigo = normalizar_tipo_cuenta(raw)
            if codigo:
                return codigo
        except Exception:
            pass
    clave = _texto_clave_identidades(raw)
    if clave in (
        "ciudadania politica",
        "ciudadano politico",
        "activista",
        "militante",
        "simpatizante",
        "tecnico",
        "tecnico coloquial",
    ):
        return "activista"
    if clave in ("politica", "politico", "institucional", "formal", "oficial"):
        return "politica"
    if "activis" in clave:
        return "activista"
    if "politic" in clave:
        return "politica"
    if "ciudadan" in clave or clave in (
        "persona",
        "real",
        "informal",
        "coloquial",
    ):
        return "ciudadana"
    return raw


def _normalizar_tipo_identidad(valor) -> str:
    """Normaliza el tipo de identidad a partido/movimiento/persona/mixto.

    Acepta los sinónimos del generador de identidades ("similitud", "guiño",
    "color", "espectro"...). Devuelve "" si el valor no se reconoce; nunca
    lanza.
    """
    clave = " ".join(
        _texto_clave_identidades(valor).replace("_", " ").replace("-", " ").split()
    )
    if not clave:
        return ""
    alias = {
        "partido": "partido",
        "similitud": "partido",
        "similitud de partido": "partido",
        "similitud partido": "partido",
        "guino": "partido",
        "guino de partido": "partido",
        "color": "partido",
        "colores": "partido",
        "espectro": "partido",
        "movimiento": "movimiento",
        "organizacion": "movimiento",
        "colectivo": "movimiento",
        "colectiva": "movimiento",
        "persona": "persona",
        "personas": "persona",
        "humano": "persona",
        "humana": "persona",
        "nombre": "persona",
        "nombres": "persona",
        "mixto": "mixto",
        "mixta": "mixto",
    }
    if clave in alias:
        return alias[clave]
    if (
        "similitud" in clave
        or "guino" in clave
        or "espectro" in clave
        or "color" in clave
        or "partido" in clave
    ):
        return "partido"
    if "movimiento" in clave:
        return "movimiento"
    if "persona" in clave:
        return "persona"
    if "mixto" in clave or "mixta" in clave:
        return "mixto"
    return ""


def _recortar_contexto_identidad(contexto, limite: int = 300) -> str:
    """Normaliza (espacios) y recorta el contexto adicional a ``limite``."""
    try:
        texto = " ".join(str(contexto or "").split())
    except Exception:
        return ""
    if not texto:
        return ""
    if len(texto) > limite:
        texto = texto[:limite].rstrip()
    return texto


def _construir_prompt_identidades(
    seccion: str = "",
    registro: str = "",
    tipo: str = "",
    contexto: str = "",
) -> str:
    """Arma el bloque de identidades; devuelve '' si nada aplica. Nunca lanza."""
    sec = _seccion_identidad(seccion)
    reg = _registro_identidad(registro)
    tpo = _normalizar_tipo_identidad(tipo)
    ctx = _recortar_contexto_identidad(contexto)

    sec_valida = sec in ("CI", "IP", "LIB", "JUS")
    reg_valido = reg in ("politica", "activista", "ciudadana")
    tpo_valido = tpo in _REGLAS_IDENT_TIPO
    if not (sec_valida or reg_valido or tpo_valido or ctx):
        return ""

    bloques = [_IDENTIDADES_HEADER, _PROHIBICIONES_IDENTIDAD]

    regla = ""
    if reg == "ciudadana":
        # Regla obligatoria: el registro ciudadana IGNORA la seccion.
        regla = _REGLA_IDENT_CIUDADANA
    elif reg == "politica":
        if sec == "IP":
            regla = _REGLA_IDENT_IP_POLITICA
        elif sec == "CI":
            regla = _REGLA_IDENT_CI_POLITICA
        elif sec == "LIB":
            regla = _REGLA_IDENT_LIB_POLITICA
        elif sec == "JUS":
            regla = _REGLA_IDENT_JUS_POLITICA
        else:
            regla = _REGLA_IDENT_POLITICA_GENERAL
    elif reg == "activista":
        if sec in ("LIB", "JUS"):
            # Regla obligatoria: LIB o JUS + activista = seudonimo combativo.
            regla = _REGLA_IDENT_LIBJUS_ACTIVISTA
        elif sec == "IP":
            regla = _REGLA_IDENT_IP_ACTIVISTA
        elif sec == "CI":
            regla = _REGLA_IDENT_CI_ACTIVISTA
        else:
            regla = _REGLA_IDENT_ACTIVISTA_GENERAL
    elif sec_valida:
        # Default sensato por seccion cuando no hay registro (neutro).
        regla = _REGLAS_IDENT_SECCION.get(sec, "")

    if regla:
        bloques.append(regla)
    if tpo_valido:
        bloques.append(_REGLAS_IDENT_TIPO[tpo])
    if not regla and not tpo_valido and ctx:
        bloques.append(_REGLA_IDENT_NEUTRA)
    if ctx:
        bloques.append(f"Contexto adicional (respétalo): {ctx}\n")

    return "".join(bloques)


def get_prompt_identidades_contexto(
    seccion: str = "",
    registro: str = "",
    tipo: str = "",
    contexto: str = "",
) -> str:
    """Bloque de REGLAS DE IDENTIDAD por contexto (Seccion + Registro + tipo).

    Devuelve texto plano listo para concatenar a un prompt de identidades;
    '' si no hay nada que aplicar. Nunca lanza.

    Cruces obligatorios (los consume ``cuentas/generador_identidades.py``):
        - IP + politica: nombres/handles FORMALES, corporativos o de analista
          serio (``@AnalisisIP``, ``@ConsultoriaDatos``, display names
          "Análisis ...", "Consultoría ...", "Centro de ...").
        - LIB/JUS + activista: seudonimos combativos o de causa social
          (``@VozLibertad``, ``@JusticiaYa``, display names "Voz Libertad",
          "Justicia Ya", "Fuerza ...").
        - ciudadana (cualquier seccion): IGNORA LA SECCION; personas mexicanas
          100% reales y coloquiales (estilo Faker es_MX).

    ``seccion`` se normaliza con ``core.secciones.normalizar_seccion``
    (CI/IP/LIB/JUS) y ``registro`` con
    ``core.registros.normalizar_tipo_cuenta`` (politica/activista/ciudadana),
    con imports locales tolerantes a fallos (si fallan, se usa el texto tal
    cual). ``tipo`` acepta "partido" (o similitud/guiño/color/espectro),
    "movimiento", "persona" y "mixto". ``contexto`` no vacio se agrega como
    linea "Contexto adicional (respétalo): ..." recortada a 300 caracteres.
    """
    try:
        return _construir_prompt_identidades(seccion, registro, tipo, contexto)
    except Exception:
        return ""
