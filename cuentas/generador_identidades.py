# -*- coding: utf-8 -*-
"""Generador de identidades (display name + @) para cuentas de X.

Tipos de identidad:

* ``"movimiento"`` — nombres de ciudadania organizada, genericos y variados
  (ej. "Ciudadania Feliz", "Ciudad Unida", "Movimientos Unidos", "Voces del
  Pueblo"). PROHIBIDO que contengan la frase literal "Movimiento Ciudadano",
  la sigla "MC" aislada o siglas/nombres de partidos.
* ``"persona"`` — nombres mexicanos reales y variados (Faker ``es_MX`` con
  listas de respaldo), para cuentas que simulan personas "reales".
* ``"partido"`` — organizaciones que EVOCAN a un partido SIN nombrarlo ni usar
  siglas: colores y simbolos cotidianos (ej. "Movimiento Naranja", "Fuerza
  Naranja", "Marea Naranja", "Amarillo de Luz", "Los Bolillos", "Bolillos de
  la Colonia", "Corazon Naranja", "Faro Azul", "Bandera Guinda", "Rosa en
  Movimiento"). PROHIBIDO "Morena"/"PAN"/"PRI"/siglas o "Movimiento
  Ciudadano" (ver ``_TOKENS_PARTIDO`` y ``_nombre_prohibido``).

``asignar_propuestas`` acepta ademas ``"mixto"`` (sinonimos "mezcla"/"mitad"):
reparte ~50% persona / ~50% partido, barajado cuenta por cuenta.

Interfaz congelada (consumida por dashboard y CLI):

    es_handle_generico(handle) -> bool
    ia_disponible() -> bool
    generar_identidad(tipo="persona", seccion="", contexto="", evitar=None) -> dict
    generar_identidades(cantidad, tipo="persona", seccion="", contexto="",
                        usuarios_existentes=None, registro="") -> list[dict]
    asignar_propuestas(usuarios, tipo="auto", seccion="", dry_run=False,
                       contexto="", proteger_brandeadas=False) -> dict
    aplicar_propuestas_en_lote(usuarios, max_workers=2, password="",
                               renombrar=False, callback=None,
                               cancelar=None) -> dict
    aplicar_propuesta(usuario, password="") -> dict
    descartar_propuesta(usuario) -> bool

Personalidades y perfiles de redaccion para el mantenimiento programado:

    generar_personalidad(registro="", seccion="", nombre="", tipo="",
                         perfil="") -> str
    asignar_personalidades(usuarios, forzar=False, dry_run=False) -> dict
    asignar_perfiles_personalidad(usuarios=None, forzar=False, dry_run=False,
                                  solo_sin_perfil=True) -> dict

Los 3 perfiles ("formal"/"ciudadano"/"popular", ver ``core/perfiles.py``) se
reparten equitativamente entre las cuentas y la personalidad se genera con el
tono del perfil asignado.

OpenAI es OPCIONAL (la key se lee de ``core.config.settings``): si falla, no
hay key o el modelo responde mal, la generacion continua 100% local (Faker +
listas propias). Las funciones de generacion nunca lanzan excepciones.
"""
import json
import random
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

from core.perfiles import (
    PERFILES_ORDEN,
    distribuir_perfiles,
    normalizar_perfil,
    perfil_coherente,
)

__all__ = [
    "HANDLE_RE",
    "es_handle_generico",
    "ia_disponible",
    "generar_identidad",
    "generar_identidades",
    "asignar_propuestas",
    "aplicar_propuestas_en_lote",
    "aplicar_propuesta",
    "descartar_propuesta",
    "generar_personalidad",
    "asignar_personalidades",
    "asignar_perfiles_personalidad",
    "rebalancear_perfiles",
]

# --------------------------------------------------------------------------- #
# Reglas de X
# --------------------------------------------------------------------------- #
# Limite real de X para el @usuario.
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{4,15}$")
_LARGO_MAX_NOMBRE = 50  # limite real de X para el display name
_LOTE_OPENAI_MAX = 30
_MODELO_OPENAI = "gpt-4o-mini"

# Siglas/nombres de partidos que NUNCA deben aparecer (como palabra/token).
_TOKENS_PARTIDO = {
    "mc",
    "morena",
    "pan",
    "pri",
    "prd",
    "pvem",
    "pt",
    "pes",
    "pmc",
}


# --------------------------------------------------------------------------- #
# Listas propias (fallback de Faker y vocabulario "movimiento")
# --------------------------------------------------------------------------- #
_CONCEPTOS = [
    # (palabra, genero, numero)
    ("Ciudadanía", "f", "s"),
    ("Ciudad", "f", "s"),
    ("Voces", "f", "p"),
    ("Gente", "f", "s"),
    ("Pueblo", "m", "s"),
    ("Corazón", "m", "s"),
    ("Fuerza", "f", "s"),
    ("Unión", "f", "s"),
    ("Movimiento", "m", "s"),
    ("Movimientos", "m", "p"),
    ("Barrio", "m", "s"),
    ("Colonia", "f", "s"),
    ("Paz", "f", "s"),
    ("Esperanza", "f", "s"),
    ("Progreso", "m", "s"),
    ("Comunidad", "f", "s"),
    ("Vecinos", "m", "p"),
    ("Colectivo", "m", "s"),
    ("Causa", "f", "s"),
    ("Camino", "m", "s"),
    ("Semilla", "f", "s"),
    ("Puente", "m", "s"),
    ("Motor", "m", "s"),
    ("Alianza", "f", "s"),
    ("Familias", "f", "p"),
    ("Juventud", "f", "s"),
    ("Red", "f", "s"),
    ("Círculo", "m", "s"),
    ("Mañana", "f", "s"),
    ("Horizonte", "m", "s"),
    ("Rumbo", "m", "s"),
    ("Latido", "m", "s"),
    ("Raíces", "f", "p"),
    ("Encuentro", "m", "s"),
]

_ADJETIVOS = {
    ("f", "s"): [
        "Feliz", "Unida", "Despierta", "Viva", "Imparable", "Conectada",
        "Activa", "Libre", "Valiente", "Honesta", "Trabajadora", "Cercana",
        "Auténtica", "Diversa", "Solidaria", "Incluyente", "Fuerte", "Naranja",
        "Presente", "Abierta", "Orgullosa", "Compartida", "Segura",
    ],
    ("f", "p"): [
        "Felices", "Unidas", "Despiertas", "Vivas", "Imparables",
        "Conectadas", "Activas", "Libres", "Valientes", "Honestas",
        "Trabajadoras", "Cercanas", "Auténticas", "Diversas", "Solidarias",
        "Incluyentes", "Fuertes", "Naranjas", "Presentes", "Abiertas",
        "Orgullosas", "Compartidas", "Seguras",
    ],
    ("m", "s"): [
        "Feliz", "Unido", "Despierto", "Vivo", "Imparable", "Conectado",
        "Activo", "Libre", "Valiente", "Honesto", "Trabajador", "Cercano",
        "Auténtico", "Diverso", "Solidario", "Incluyente", "Fuerte", "Naranja",
        "Presente", "Abierto", "Orgulloso", "Compartido", "Seguro",
    ],
    ("m", "p"): [
        "Felices", "Unidos", "Despiertos", "Vivos", "Imparables",
        "Conectados", "Activos", "Libres", "Valientes", "Honestos",
        "Trabajadores", "Cercanos", "Auténticos", "Diversos", "Solidarios",
        "Incluyentes", "Fuertes", "Naranjas", "Presentes", "Abiertos",
        "Orgullosos", "Compartidos", "Seguros",
    ],
}

_COLECTIVOS_M = ["Pueblo", "Barrio", "Campo", "País"]
_COLECTIVOS_F = ["Colonia", "Comunidad", "Ciudad", "Gente", "Ciudadanía"]
_PLURALES = ["Vecinos", "Familias", "Jóvenes", "Trabajadores", "Ciudadanos"]
_ACCIONES = [
    "Movimiento", "Marcha", "Acción", "Paz", "Esperanza", "Libertad",
    "Lucha", "Construcción",
]

# Nombres completos ya validados (variedad y calidad garantizadas).
_PRESETS_MOVIMIENTO = [
    "Ciudadanía Feliz",
    "Ciudad Unida",
    "Movimientos Unidos",
    "Voces del Pueblo",
    "Gente en Movimiento",
    "Corazón de la Colonia",
    "Fuerza Ciudadana",
    "Unión de Vecinos",
    "Esperanza Viva",
    "Progreso Compartido",
    "Barrio Despierto",
    "Pueblo en Marcha",
    "Comunidad Activa",
    "Causa Común",
    "Camino Ciudadano",
    "Semilla Ciudadana",
    "Puente de Gente",
    "Motor Ciudadano",
    "Alianza Vecinal",
    "Familias Unidas",
    "Juventud Presente",
    "Red Ciudadana",
    "Círculo Abierto",
    "Mañana en Paz",
    "Horizonte Naranja",
    "Rumbo Ciudadano",
    "Latido del Barrio",
    "Raíces Vivas",
    "Encuentro Ciudadano",
    "La Gente Manda",
    "Nuestra Ciudad Cuenta",
    "Voces de la Colonia",
    "Corazón Naranja",
    "Calles con Esperanza",
    "Vecinos en Movimiento",
    "Ciudadanía Activa",
    "Movimiento de Barrio",
    "Gente que Suma",
    "Pueblo que Avanza",
    "Fuerza de la Comunidad",
    "Voces que Unen",
    "Ciudad que Late",
    "Barrio con Voz",
    "Todos por la Colonia",
    "Unión Ciudadana",
]

# --- Vocabulario "partido" (similitud con partidos, SIN nombrarlos) --------- #
# Guiños = COLORES + SIMBOLOS cotidianos combinados en plantillas. El dueño
# pidio expresamente ejemplos como "naranja", "bolillos" o "amarilloluz"; se
# prohibe CUALQUIER sigla/nombre literal de partido (ver _TOKENS_PARTIDO).
_COLORES_PARTIDO = [
    "Naranja",
    "Guinda",
    "Azul",
    "Rojo",
    "Verde",
    "Amarillo",
    "Rosa",
    "Morado",
    "Dorado",
    "Celeste",
    "Turquesa",
]
_SIMBOLOS_PARTIDO = [
    "Luz",
    "Marea",
    "Corriente",
    "Ola",
    "Sol",
    "Faro",
    "Estrella",
    "Alba",
    "Aurora",
    "Viento",
    "Bandera",
    "Corazón",
    "Bolillos",
    "Antorcha",
    "Manantial",
    "Cauce",
    "Eco",
    "Amanecer",
    "Semilla",
    "Raíz",
]
# Plurales ya declinados para la plantilla "Los {plural}".
_PLURALES_PARTIDO = [
    "Bolillos",
    "Faros",
    "Corazones",
    "Soles",
    "Ecos",
    "Cauces",
    "Antorchas",
    "Estrellas",
    "Semillas",
    "Raíces",
    "Manantiales",
    "Amaneceres",
]
_COLECTIVOS_PARTIDO = [
    "Movimiento",
    "Fuerza",
    "Barrio",
    "Pueblo",
    "Frente",
    "Unión",
    "Causa",
    "Voz",
    "Alianza",
    "Comunidad",
    "Red",
    "Camino",
]
_COLECTIVOS_F_PARTIDO = ["Colonia", "Comunidad", "Ciudad", "Gente", "Vecindad"]
# Cierres de la plantilla "{color} con {sustantivo}" (ej. "Naranja con Causa").
_SUSTANTIVOS_CON = [
    "Causa",
    "Luz",
    "Corazón",
    "Rumbo",
    "Orgullo",
    "Raíz",
    "Fuerza",
    "Esperanza",
]

# Nombres partidistas ya validados (incluye los ejemplos exactos del dueño).
_PRESETS_PARTIDO = [
    "Movimiento Naranja",
    "Fuerza Naranja",
    "Marea Naranja",
    "Corriente Naranja",
    "Amarillo de Luz",
    "Los Bolillos",
    "Bolillos de la Colonia",
    "Corazón Naranja",
    "Sol Naranja",
    "Faro Azul",
    "Bandera Guinda",
    "Rosa en Movimiento",
    "Aurora Naranja",
    "Barrio Naranja",
    "Naranja con Causa",
    "Movimiento Guinda",
    "Fuerza Amarilla",
    "Marea Verde",
    "Corriente Rosa",
    "Luz de Barrio",
    "Antorcha Naranja",
    "Estrella Guinda",
    "Faro Naranja",
    "Ola Morada",
    "Aurora Rosa",
    "Viento Verde",
    "Bandera Naranja",
    "Sol de la Colonia",
    "Bolillos con Causa",
    "Corazón Guinda",
    "Celeste de Luz",
    "Dorado de Luz",
    "Los Faros",
    "Cauce Naranja",
    "Turquesa en Movimiento",
    "Gente Naranja",
]

# Respaldo si Faker es_MX no esta instalado o falla.
_NOMBRES_MX = [
    "Juan", "María", "José", "Luis", "Carlos", "Miguel", "Jorge", "Pedro",
    "Alejandro", "Fernando", "Ricardo", "Roberto", "Javier", "Sergio",
    "Andrés", "Raúl", "Arturo", "Enrique", "Guadalupe", "Lupita", "Rosa",
    "Verónica", "Patricia", "Adriana", "Gabriela", "Fernanda", "Daniela",
    "Alejandra", "Mónica", "Claudia", "Yolanda", "Silvia", "Elena", "Norma",
    "Rocío", "Marisol", "Esperanza", "Cecilia", "Leticia", "Alicia",
]

_APELLIDOS_MX = [
    "Pérez", "López", "Hernández", "García", "Martínez", "Rodríguez",
    "González", "Sánchez", "Ramírez", "Torres", "Flores", "Rivera", "Gómez",
    "Díaz", "Cruz", "Morales", "Reyes", "Gutiérrez", "Ortiz", "Chávez",
    "Mendoza", "Ruiz", "Castillo", "Jiménez", "Vázquez", "Ramos", "Herrera",
    "Medina", "Aguilar", "Vargas", "Castro", "Romero", "Álvarez", "Guerrero",
    "Salazar", "Núñez", "Domínguez", "Rojas", "Valencia", "Ibarra", "Cortés",
    "Padilla", "Zamora", "Rosales", "Fuentes", "Carrillo", "Solís",
]

_faker_es_mx = None
_faker_intentado = False

# --------------------------------------------------------------------------- #
# Utilidades de normalizacion
# --------------------------------------------------------------------------- #
def _quitar_acentos(texto: str) -> str:
    desc = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in desc if not unicodedata.combining(c))


def _normalizar_texto(valor) -> str:
    """Minusculas, sin acentos y con espacios colapsados (None -> "")."""
    if valor is None:
        return ""
    texto = _quitar_acentos(str(valor).strip().lower())
    return re.sub(r"\s+", " ", texto)


def _normalizar_handle_texto(valor) -> str:
    """Forma canonica de comparar handles: minusculas, sin @ ni separadores."""
    texto = _normalizar_texto(valor).replace(" ", "_")
    texto = re.sub(r"[^a-z0-9_]", "", texto)
    texto = re.sub(r"_+", "_", texto).strip("_")
    return texto


def _clave_handle(valor) -> str:
    return _normalizar_handle_texto(str(valor or "").lstrip("@"))


def _preparar_evitar(evitar) -> set:
    """Normaliza una coleccion de handles ocupados a un set de claves."""
    usados = set()
    try:
        for item in evitar or set():
            clave = _clave_handle(item)
            if clave:
                usados.add(clave)
    except Exception:
        pass
    return usados


# --------------------------------------------------------------------------- #
# Reglas de contenido prohibido
# --------------------------------------------------------------------------- #
def _nombre_prohibido(nombre: str) -> bool:
    """True si el nombre viola reglas (Movimiento Ciudadano / MC / partidos)."""
    plano = _normalizar_texto(nombre)
    if not plano:
        return True
    # "Movimiento Ciudadano", "Movimiento Ciudadanía", etc. (aunque cambien
    # espacios/mayusculas/acentos).
    compacto = plano.replace(" ", "")
    if "movimientociudadan" in compacto or "ciudadanomovimiento" in compacto:
        return True
    tokens = [t for t in re.split(r"[^a-z0-9]+", plano) if t]
    for token in tokens:
        if token in _TOKENS_PARTIDO:
            return True
    # "Movimiento (de/del/por la) Ciudadanía..." — demasiado parecido a MC.
    if tokens and tokens[0] in ("movimiento", "movimientos"):
        for token in tokens[1:4]:
            if token.startswith("ciudadan"):
                return True
    return False


def _handle_prohibido(handle: str) -> bool:
    """True si el handle contiene MC aislada o referencias a partidos."""
    h = _normalizar_handle_texto(handle)
    if not h:
        return True
    if "movimientociudadan" in h:
        return True
    for token in h.split("_"):
        if token in _TOKENS_PARTIDO:
            return True
    if "morena" in h:
        return True
    return False


# --------------------------------------------------------------------------- #
# Filtro anti-sobrescritura: handles genericos de proveedores
# --------------------------------------------------------------------------- #
# Heuristica determinista y CONSERVADORA (ante la duda devuelve False) usada
# por ``_cuenta_protegida``: los proveedores de cuentas "Aged" entregan
# usuarios basura tipo "Katiaforbx7m", "GoodWinsnvn", "khawajaGjdgi",
# "Sajtiagosal21" o "qwrtyps12x" y esas cuentas SI se pueden renombrar; las
# que ya tienen un handle humano/brandeado NO se deben tocar.

# Vocales "fuertes" (para el ratio) y letras que cortan corridas de
# consonantes: la "y" actua como semivocal en espanol ("proyecto", "yoga").
_VOCALES_FUERTES = frozenset("aeiou")
_VOCALES_CORRIDA = frozenset("aeiouy")


def _construir_pares_consonantes() -> set:
    """Pares de consonantes tolerables en espanol (tabla fija).

    Incluye los grupos clasicos con l/r (tr, br, cl, pl, gr, pr, cr, dr, fl,
    bl, gl, str, ntr, mbr, ...), los digrafos (ch, ll, rr, qu, gu), las
    nasales + consonante (nt, nd, mb, mp, ...), la s + consonante y la l/r +
    consonante. Un par FUERA de la tabla delata basura de proveedor ("jt",
    "gj", "qw", "bx", "vn", ...).
    """
    pares = set()
    consonantes = "bcdfghjklmnpqrstvwxyz"
    for c in consonantes:
        pares.add(c + "l")
        pares.add(c + "r")
    pares |= {"ch", "ll", "rr", "qu", "gu"}
    for c in consonantes:
        pares.add("n" + c)
        pares.add("m" + c)
        pares.add("s" + c)
        pares.add("l" + c)
        pares.add("r" + c)
    return pares


_PARES_CONSONANTES = _construir_pares_consonantes()

# Palabras legitimas (minusculas, sin acentos): nombres/apellidos mexicanos
# ya usados por el generador, terminos politicos/organizativos y vocabulario
# cotidiano. Si >= 70% de las letras de un handle se descomponen en estas
# palabras, el handle se considera humano (NUNCA generico).
_PALABRAS_LEGITIMAS = {_normalizar_texto(n) for n in _NOMBRES_MX + _APELLIDOS_MX} | {
    # Organizacion / politica / gobierno.
    "movimiento", "movimientos", "mov", "unidos", "unidas", "unido", "unida",
    "voz", "voces", "libertad", "justicia", "analisis", "consultoria",
    "datos", "ciudad", "ciudadania", "ciudadano", "ciudadana", "mexico",
    "mexicana", "mexicano", "patria", "pueblo", "gente", "fuerza",
    "esperanza", "futuro", "accion", "cambio", "progreso", "democracia",
    "reforma", "revolucion", "derechos", "humanos", "territorio",
    "colonia", "barrio", "comunidad", "vecinos", "familia", "familias",
    "juventud", "union", "colectivo", "causa", "camino", "semilla",
    "puente", "motor", "alianza", "manana", "horizonte", "rumbo",
    "latido", "raices", "encuentro", "gobierno", "senado", "diputado",
    "diputados", "legislatura", "institucion", "institucional", "privada",
    "privado", "publico", "publica", "politica", "politico", "social",
    "sociedad", "cultura", "deporte", "futbol", "ciencia", "tecnologia",
    "economia", "finanzas", "empresa", "empresas", "negocios", "mercado",
    "capital", "grupo", "fundacion", "asociacion", "organizacion",
    "asamblea", "campesino", "campesinos", "obrero", "obreros", "maestro",
    "maestros", "estudiante", "estudiantes", "universidad", "escuela",
    "hospital", "seguridad", "defensa", "salud", "educacion", "trabajo",
    "ambiente", "verde", "azul", "rojo", "guinda", "rosa", "morado",
    "dorado", "celeste", "turquesa", "amarillo", "naranja", "luz", "marea",
    "corriente", "ola", "sol", "faro", "estrella", "alba", "aurora",
    "viento", "bandera", "corazon", "bolillos", "antorcha", "manantial",
    "cauce", "eco", "amanecer", "raiz",
    # Nombres extra y vocabulario profesional/cotidiano.
    "katia", "santiago", "tiago", "diego", "sofia", "camila", "valeria",
    "regina", "renata", "emilio", "bruno", "dante", "alvaro", "gael",
    "zoe", "emma", "mia", "ana", "soledad", "concepcion", "refugio",
    "consultor", "consultores", "analista", "analistas", "estrategia",
    "estrategias", "proyecto", "proyectos", "soluciones", "servicios",
    "sistemas", "digital", "digitales", "media", "medios", "noticias",
    "periodismo", "comunicacion", "publicidad", "estudio", "estudios",
}

# Tokens cortos/con digitos que se aceptan tal cual al partir el handle
# ("4T_puntodos" es una cuenta 4T legitima, "CDMX"/"Hdz" son siglas comunes).
_TOKENS_CLAVE = {"4t", "cdmx", "mx", "hdz", "gzz", "mtz", "vgz", "rdz", "ip"}

# Bloques minimos de contexto por seccion/registro (fallback local cuando
# ``ia.prompts.get_prompt_identidades_contexto`` todavia no existe).
_REGLAS_CONTEXTO_FALLBACK = (
    "Reglas de CONTEXTO por seccion/registro (MANDAN sobre el detalle del tipo "
    "cuando apliquen):\n"
    "- IP + politica: identidad formal, corporativa o de analista "
    "(ej. AnalisisIP, ConsultoriaDatos).\n"
    "- LIB o JUS + activista: seudonimo combativo o de causas "
    "(ej. VozLibertad, JusticiaYa).\n"
    "- ciudadana: ignora la seccion; nombre mexicano real y coloquial "
    "(ej. Maria Lopez, Lupita Hernandez).\n"
)


def _segmentar_handle(handle: str) -> list:
    """Parte un handle en tokens por separadores y fronteras camelCase.

    "UnidosMovCDMX" -> ["Unidos", "Mov", "CDMX"]; "lupita_hdz" ->
    ["lupita", "hdz"]. Se usa para medir cuanto del handle es vocabulario
    legitimo. Nunca lanza (cualquier valor raro devuelve []).
    """
    segmentos = []
    try:
        for bruto in re.split(r"[^A-Za-z0-9]+", str(handle or "")):
            if not bruto:
                continue
            inicio = 0
            for i in range(1, len(bruto)):
                anterior, actual = bruto[i - 1], bruto[i]
                corte = False
                if anterior.islower() and actual.isupper():
                    corte = True
                elif (
                    anterior.isupper()
                    and actual.isupper()
                    and i + 1 < len(bruto)
                    and bruto[i + 1].islower()
                ):
                    corte = True
                if corte:
                    segmentos.append(bruto[inicio:i])
                    inicio = i
            segmentos.append(bruto[inicio:])
    except Exception:
        return []
    return [segmento for segmento in segmentos if segmento]


def _mejor_cobertura_palabra(token: str) -> int:
    """Letras del token cubiertas por palabras legitimas (DP sin solaparse)."""
    letras = re.sub(r"[^a-z]", "", str(token or "").lower())
    n = len(letras)
    if n < 3:
        return 0
    cubierto = [0] * (n + 1)
    for i in range(1, n + 1):
        cubierto[i] = cubierto[i - 1]
        for largo in range(3, min(30, i) + 1):
            if letras[i - largo:i] in _PALABRAS_LEGITIMAS:
                cubierto[i] = max(cubierto[i], cubierto[i - largo] + largo)
    return cubierto[n]


def _cobertura_palabras_legitimas(handle: str) -> float:
    """Fraccion (0-1) de letras del handle que forman palabras legitimas."""
    letras = sum(1 for c in str(handle or "").lower() if c.isalpha())
    if letras <= 0:
        return 0.0
    cubiertas = 0
    for token in _segmentar_handle(handle):
        cubiertas += _mejor_cobertura_palabra(token)
    return min(1.0, cubiertas / letras)


def _tiene_cluster_invalido(texto: str) -> bool:
    """True si hay una corrida de consonantes imposible en espanol.

    "jt", "gj", "qw", "bx", "vn" o "qwrtyps" delatan basura generada; "tr",
    "br", "nt", "mbr" o "naranja" no. Nunca lanza.
    """
    try:
        for corrida in _corridas_consonantes(texto):
            for i in range(len(corrida) - 1):
                if corrida[i:i + 2] not in _PARES_CONSONANTES:
                    return True
    except Exception:
        return False
    return False


def _corridas_consonantes(texto: str) -> list:
    """Corridas de consonantes de un texto (la "y" corta como semivocal)."""
    corridas = []
    actual = ""
    for c in re.sub(r"[^a-z]", "", str(texto or "").lower()):
        if c in _VOCALES_CORRIDA:
            if actual:
                corridas.append(actual)
                actual = ""
        else:
            actual += c
    if actual:
        corridas.append(actual)
    return corridas


def _digitos_sospechosos(texto: str) -> bool:
    """True si los digitos del handle tienen pinta de sufijo de proveedor.

    Digitos EMBEBIDOS entre letras ("...bx7m", "12x") o numeros en un handle
    largo ("Sajtiagosal21") son la firma tipica de los lotes Aged. Un handle
    humano corto con algun numero ("lupita2024") queda fuera.
    """
    limpio = str(texto or "")
    if not any(c.isdigit() for c in limpio):
        return False
    if re.search(r"[A-Za-z]\d[A-Za-z]", limpio):
        return True
    return len(limpio) >= 9


def _transiciones_mayusculas(texto: str) -> int:
    """Cambios minuscula->MAYUSCULA (y fin de acronimo) del handle."""
    cuenta = 0
    limpio = re.sub(r"[^A-Za-z]", "", str(texto or ""))
    for i in range(1, len(limpio)):
        if limpio[i - 1].islower() and limpio[i].isupper():
            cuenta += 1
        elif (
            limpio[i - 1].isupper()
            and limpio[i].isupper()
            and i + 1 < len(limpio)
            and limpio[i + 1].islower()
        ):
            cuenta += 1
    return cuenta


def es_handle_generico(handle) -> bool:
    """True si ``handle`` parece un usuario generado por un proveedor.

    Heuristica determinista y CONSERVADORA (ante la duda devuelve False) para
    el filtro anti-sobrescritura de ``asignar_propuestas``: las cuentas con
    handle humano/brandeado NO deben perder su identidad, mientras que los
    handles basura de lotes "Aged" (``Katiaforbx7m``, ``GoodWinsnvn``,
    ``khawajaGjdgi``, ``Sajtiagosal21``, ``qwrtyps12x``) SI se pueden renovar.

    Senales combinadas (score; hace falta >= 2 para marcarlo generico):

      * corridas de consonantes imposibles en espanol (se toleran tr, br, cl,
        pl, gr, pr, cr, dr, fl, bl, gl, str, ntr, mbr, nasales+liquidas, ...);
      * proporcion de vocales fuertes (a/e/i/o/u) menor al 34%;
      * digitos embebidos entre letras o sufijos numericos en handles largos;
      * cambios de mayuscula tipo camelCase aleatorio (>= 2 transiciones).

    Rescates (devuelven False de inmediato): tokens clave conocidos ("4t",
    "CDMX", "Hdz", ...) y una whitelist de nombres/apellidos mexicanos y
    vocabulario politico/cotidiano; si >= 70% de las letras del handle se
    descomponen en esas palabras, es humano. ``None``, vacio, numeros o un
    display name con espacios (no es un handle) devuelven False. Nunca lanza.
    """
    try:
        texto = str(handle or "").strip().lstrip("@")
        if not texto or any(c.isspace() for c in texto):
            return False
        limpio = re.sub(r"[^A-Za-z0-9_]", "", texto)
        if len(limpio) < 6:
            return False
        bajo = limpio.lower()

        # Rescates ANTES de puntuar: tokens clave ("4T", "CDMX", "Hdz", ...) y
        # whitelist de palabras. La segmentacion usa el camelCase ORIGINAL
        # ("UnidosMovCDMX" -> "Unidos"/"Mov"/"CDMX"), no la copia en minusculas.
        for token in _segmentar_handle(limpio):
            if token.lower() in _TOKENS_CLAVE:
                return False
        if _cobertura_palabras_legitimas(limpio) >= 0.7:
            return False

        score = 0
        if _tiene_cluster_invalido(bajo):
            score += 1
        letras = re.sub(r"[^a-z]", "", bajo)
        if letras:
            vocales = sum(1 for c in letras if c in _VOCALES_FUERTES)
            if (vocales / len(letras)) < 0.34:
                score += 1
        if _digitos_sospechosos(limpio):
            score += 1
        if _transiciones_mayusculas(texto) >= 2:
            score += 1
        return score >= 2
    except Exception:
        return False


def _cuenta_protegida(cuenta) -> tuple:
    """Devuelve ``(protegida, campo, valor)`` para el filtro anti-sobrescritura.

    Una cuenta se protege cuando ya tiene identidad humana o trabajo previo:

      * ``handle_actual`` no vacio y NO generico -> ``("handle_actual", valor)``;
      * ``nombre_mostrado`` no vacio y NO generico -> ``("nombre_mostrado", valor)``;
      * ya tiene ``handle_propuesto`` o ``nombre_propuesto`` (fue trabajada)
        -> ``("propuesta", valor)``.

    Si no hay nada que proteger devuelve ``(False, "", "")``. Nunca lanza.
    """
    try:
        handle = str(getattr(cuenta, "handle_actual", "") or "").strip()
        if handle and not es_handle_generico(handle):
            return True, "handle_actual", handle
        nombre = str(getattr(cuenta, "nombre_mostrado", "") or "").strip()
        if nombre and not es_handle_generico(nombre):
            return True, "nombre_mostrado", nombre
        handle_propuesto = str(getattr(cuenta, "handle_propuesto", "") or "").strip()
        nombre_propuesto = str(getattr(cuenta, "nombre_propuesto", "") or "").strip()
        if handle_propuesto or nombre_propuesto:
            return True, "propuesta", handle_propuesto or nombre_propuesto
        return False, "", ""
    except Exception:
        return False, "", ""


# --------------------------------------------------------------------------- #
# Faker / nombres de persona
# --------------------------------------------------------------------------- #
def _obtener_faker():
    """Instancia perezosa de Faker('es_MX'); devuelve None si no esta."""
    global _faker_es_mx, _faker_intentado
    if not _faker_intentado:
        _faker_intentado = True
        try:
            from faker import Faker

            _faker_es_mx = Faker("es_MX")
        except Exception as e:  # pragma: no cover - depende del entorno
            logger.warning(f"Faker es_MX no disponible, uso listas propias: {e}")
            _faker_es_mx = None
    return _faker_es_mx


def _validar_nombre(nombre) -> str:
    """Devuelve el nombre limpio si cumple las reglas, o None si no."""
    limpio = re.sub(r"\s+", " ", str(nombre or "").strip())
    if not limpio or len(limpio) > _LARGO_MAX_NOMBRE:
        return None
    if any(c in limpio for c in "#@/:\\|"):
        return None
    if not re.match(r"^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 .'-]+$", limpio):
        return None
    palabras = [p for p in limpio.split(" ") if p]
    if not (2 <= len(palabras) <= 4):
        return None
    if _nombre_prohibido(limpio):
        return None
    return limpio


def _nombre_movimiento(rng=random) -> str:
    """Nombre de ciudadania organizada (preset o combinacion aleatoria)."""
    if rng.random() < 0.5:
        return rng.choice(_PRESETS_MOVIMIENTO)
    concepto, genero, numero = rng.choice(_CONCEPTOS)
    adjetivo = rng.choice(_ADJETIVOS[(genero, numero)])
    opciones = [
        f"{concepto} {adjetivo}",
        f"{concepto} de {rng.choice(_PLURALES)}",
        f"{concepto} del {rng.choice(_COLECTIVOS_M)}",
        f"{concepto} de la {rng.choice(_COLECTIVOS_F)}",
        f"{concepto} en {rng.choice(_ACCIONES)}",
    ]
    nombre = rng.choice(opciones)
    palabras = nombre.split()
    if len(palabras) >= 2:
        primera = _normalizar_texto(palabras[0])
        ultima = _normalizar_texto(palabras[-1])
        if primera == ultima or primera[:6] == ultima[:6]:
            nombre = f"{concepto} {adjetivo}"
    return nombre


def _nombre_partido(rng=random) -> str:
    """Nombre que EVOCA a un partido sin nombrarlo (colores + simbolos).

    Usa presets ya validados (ej. "Movimiento Naranja", "Los Bolillos",
    "Amarillo de Luz") y 8 plantillas de combinacion ("Movimiento {color}",
    "{color} de {simbolo}", "Los {plurales}", "{simbolo} {color}",
    "{colectivo} {color}", "{color} con {sustantivo}", "{color} en
    Movimiento", "Bolillos de la {colonia}"). Nunca incluye siglas ni nombres
    de partidos: ``_validar_nombre``/``_nombre_prohibido`` lo bloquean despues.
    """
    if rng.random() < 0.4:
        return rng.choice(_PRESETS_PARTIDO)
    color = rng.choice(_COLORES_PARTIDO)
    simbolo = rng.choice(_SIMBOLOS_PARTIDO)
    colectivo = rng.choice(_COLECTIVOS_PARTIDO)
    opciones = [
        f"Movimiento {color}",
        f"{color} de {simbolo}",
        f"Los {rng.choice(_PLURALES_PARTIDO)}",
        f"{simbolo} {color}",
        f"{colectivo} {color}",
        f"{color} con {rng.choice(_SUSTANTIVOS_CON)}",
        f"{color} en Movimiento",
        f"Bolillos de la {rng.choice(_COLECTIVOS_F_PARTIDO)}",
    ]
    nombre = rng.choice(opciones)
    palabras = nombre.split()
    if len(palabras) >= 2:
        primera = _normalizar_texto(palabras[0])
        ultima = _normalizar_texto(palabras[-1])
        if primera == ultima or primera[:6] == ultima[:6]:
            nombre = f"Movimiento {color}"
    return nombre


def _nombre_por_tipo(tipo: str, rng=random) -> str:
    """Nombre local segun el tipo de identidad (persona/movimiento/partido)."""
    if tipo == "movimiento":
        return _nombre_movimiento(rng)
    if tipo == "partido":
        return _nombre_partido(rng)
    return _nombre_persona(rng)


def _nombre_persona(rng=random) -> str:
    """Nombre mexicano real (Faker es_MX si esta; si no, listas propias)."""
    fake = _obtener_faker()
    if fake is not None:
        try:
            nombre = fake.first_name().strip()
            apellidos = [fake.last_name().strip()]
            if rng.random() < 0.45:
                segundo = fake.last_name().strip()
                if segundo and segundo != apellidos[0]:
                    apellidos.append(segundo)
            completo = " ".join([nombre] + [a for a in apellidos if a])
            if _validar_nombre(completo):
                return completo
        except Exception as e:  # pragma: no cover - protege contra Faker raro
            logger.debug(f"Faker fallo generando persona, uso listas: {e}")
    for _ in range(30):
        nombre = rng.choice(_NOMBRES_MX)
        apellidos = [rng.choice(_APELLIDOS_MX)]
        if rng.random() < 0.45:
            apellidos.append(rng.choice(_APELLIDOS_MX))
        completo = " ".join([nombre] + apellidos)
        if _validar_nombre(completo):
            return completo
    return "Juan Pérez"


# --------------------------------------------------------------------------- #
# Handles
# --------------------------------------------------------------------------- #
def _limpiar_handle(valor) -> str:
    """Deja solo [a-z0-9_], colapsa guiones y quita bordes (sin recortar)."""
    return _normalizar_handle_texto(valor)


def _recortar_handle(texto: str, max_len: int = 15) -> str:
    """Recorta a `max_len` intentando cortar en frontera de palabra (_)."""
    texto = (texto or "").strip("_")
    if len(texto) <= max_len:
        return texto
    corte = texto.rfind("_", 0, max_len + 1)
    if corte >= 4:
        return texto[:corte]
    return texto[:max_len].strip("_")


def _construir_base(palabras, rng=random) -> str:
    """Elige un estilo de handle a partir de las palabras del nombre.

    Prioriza estilos compuestos (nombre_apellido, nombreapellido, ...) para
    reducir colisiones con handles genericos ya ocupados en X; solo
    ocasionalmente usa una sola palabra si el nombre es muy largo.
    """
    compuestos = []
    if len(palabras) >= 2:
        compuestos.append("_".join(palabras[:2]))
        compuestos.append("".join(palabras[:2]))
        compuestos.append(palabras[0] + "_" + palabras[-1])
    if len(palabras) >= 3:
        compuestos.append("_".join(palabras[:3]))
        compuestos.append("".join(palabras[:3]))
    simples = [palabras[0], "".join(palabras)]

    pool = [estilo for estilo in compuestos if len(estilo) <= 15]
    if not pool:
        pool = [estilo for estilo in simples if len(estilo) <= 15] or compuestos
    elif rng.random() < 0.12:
        # Poca variedad de nombre corto, pero evita que TODOS sean compuestos.
        pool = [estilo for estilo in simples if len(estilo) <= 15] or pool

    base = rng.choice(pool)
    if rng.random() < 0.22 and len(base) + 3 <= 15:
        base += "_mx"
    return base


def _handle_emergencia(usados, rng=random) -> str:
    """Handle valido garantizado (ultimo recurso)."""
    for _ in range(500):
        candidato = ("voz" + str(rng.randint(100000000, 999999999)))[:15]
        if (
            HANDLE_RE.match(candidato)
            and not _handle_prohibido(candidato)
            and _clave_handle(candidato) not in usados
        ):
            return candidato
    return "voz00000000001"


def _handle_desde_nombre(nombre: str, evitar=None, rng=random) -> str:
    """Deriva un handle valido y unico (respecto de ``evitar``) del nombre."""
    usados = _preparar_evitar(evitar)
    palabras = [_normalizar_handle_texto(p) for p in str(nombre or "").split()]
    palabras = [p for p in palabras if p]
    if not palabras:
        palabras = ["voz", "ciudadana"]

    base = _limpiar_handle(_construir_base(palabras, rng))
    if len(base) < 4:
        base = _limpiar_handle((base + "_mx") if base else "voz_mx")
    if base and base[0].isdigit():
        base = "v" + base

    handle = _recortar_handle(base)
    if len(handle) < 4:
        handle = ("voz" + handle)[:15]
    if _handle_prohibido(handle):
        handle = ""
    if handle and _clave_handle(handle) not in usados:
        return handle

    for _ in range(60):
        sufijo = str(rng.randint(1, 9999))
        max_base = max(4, 15 - len(sufijo))
        recorte = _recortar_handle(base, max_base) or "voz"
        candidato = (recorte + sufijo)[:15]
        if candidato and candidato[0].isdigit():
            candidato = ("v" + candidato)[:15]
        if (
            HANDLE_RE.match(candidato)
            and not _handle_prohibido(candidato)
            and _clave_handle(candidato) not in usados
        ):
            return candidato
    return _handle_emergencia(usados, rng)


# --------------------------------------------------------------------------- #
# Generacion local (nunca usa red)
# --------------------------------------------------------------------------- #
def _generar_identidad_local(tipo: str, usados: set, nombres_usados: set, rng=random) -> dict:
    """Genera una identidad valida evitando handles y nombres ya usados.

    Acepta ``"persona"``, ``"movimiento"`` y ``"partido"`` (con ``"mixto"``
    elige persona/partido al azar por identidad).
    """
    if tipo not in ("persona", "movimiento", "partido"):
        tipo = rng.choice(("persona", "partido"))
    for _ in range(80):
        nombre = _nombre_por_tipo(tipo, rng)
        limpio = _validar_nombre(nombre)
        if limpio is None:
            continue
        if limpio.lower() in nombres_usados:
            continue
        handle = _handle_desde_nombre(limpio, usados, rng)
        if not HANDLE_RE.match(handle) or _handle_prohibido(handle):
            continue
        if _clave_handle(handle) in usados:
            continue
        return {"nombre": limpio, "handle": handle, "tipo": tipo}

    # Reserva: espacio de nombres practicamente inagotable, pero por si acaso.
    for _ in range(1000):
        if tipo == "partido":
            nombre = _nombre_partido(rng)
        elif tipo == "movimiento":
            concepto = rng.choice(_CONCEPTOS)[0]
            adjetivo = rng.choice(_ADJETIVOS[("f", "s")] + _ADJETIVOS[("m", "s")])
            nombre = f"{concepto} {adjetivo}"
        else:
            nombre = f"{rng.choice(_NOMBRES_MX)} {rng.choice(_APELLIDOS_MX)}"
        if nombre.lower() in nombres_usados or _validar_nombre(nombre) is None:
            continue
        handle = _handle_desde_nombre(nombre, usados, rng)
        if HANDLE_RE.match(handle) and _clave_handle(handle) not in usados:
            return {"nombre": nombre, "handle": handle, "tipo": tipo}

    if tipo == "partido":
        nombre = f"Movimiento Naranja {len(nombres_usados) + 1}"
    elif tipo == "movimiento":
        nombre = f"Ciudadanía Activa {len(nombres_usados) + 1}"
    else:
        nombre = f"Juan Pérez {len(nombres_usados) + 1}"
    handle = _handle_emergencia(usados, rng)
    return {"nombre": nombre, "handle": handle, "tipo": tipo}


def _identidad_emergencia(tipo: str) -> dict:
    rng = random
    if tipo not in ("persona", "movimiento", "partido"):
        tipo = rng.choice(("persona", "partido"))
    if tipo == "partido":
        nombre = "Movimiento Naranja"
    elif tipo == "movimiento":
        nombre = "Ciudadanía Activa"
    else:
        nombre = "Juan Pérez"
    return {
        "nombre": nombre,
        "handle": _handle_emergencia(set(), rng),
        "tipo": tipo,
    }


# --------------------------------------------------------------------------- #
# OpenAI (opcional, con fallback local)
# --------------------------------------------------------------------------- #
def _openai_disponible() -> bool:
    """True solo si hay una key con pinta de real configurada."""
    try:
        from core.config import settings

        key = (getattr(settings, "openai_api_key", "") or "").strip()
        if not key:
            return False
        bajo = key.lower()
        if bajo.startswith(("test", "placeholder", "tu_", "your_")) or "placeholder" in bajo:
            return False
        return True
    except Exception:
        return False


def _bloque_contexto_identidades(seccion: str, registro: str, tipo: str, contexto: str) -> str:
    """Bloque de reglas por seccion/registro para el prompt de identidades.

    Llama (import perezoso) a ``ia.prompts.get_prompt_identidades_contexto``,
    que otro modulo mantiene; si aun no existe o falla, usa el bloque local
    minimo ``_REGLAS_CONTEXTO_FALLBACK``. Nunca lanza.
    """
    try:
        from ia.prompts import get_prompt_identidades_contexto

        bloque = get_prompt_identidades_contexto(
            seccion=seccion, registro=registro, tipo=tipo, contexto=contexto
        )
        texto = str(bloque or "").strip()
        if texto:
            return texto
    except Exception:
        pass
    return _REGLAS_CONTEXTO_FALLBACK


def _construir_prompt(
    cantidad: int,
    tipo: str,
    seccion: str,
    contexto: str,
    evitar: set,
    registro: str = "",
) -> str:
    """Prompt de un lote de identidades (nombre + handle).

    ``registro`` ("politica"/"activista"/"ciudadana") agrega, DESPUES del
    detalle del tipo, el bloque de contexto por seccion/registro (MANDAN sobre
    el detalle cuando apliquen). Regla dura: con registro "ciudadana" la
    seccion se IGNORA (no se menciona en el prompt).
    """
    registro_norm = _normalizar_registro(registro)
    if registro_norm == "ciudadana":
        seccion = ""
    comun = (
        f"Genera exactamente {cantidad} identidades DISTINTAS para cuentas de X "
        "(Twitter) en Mexico.\n"
        "Reglas del handle: solo letras, numeros y guion bajo; 4 a 15 caracteres; "
        "sin acentos, sin @, sin iniciar con numero; cada handle debe ser unico.\n"
        "Reglas del nombre: 2 a 4 palabras, en espanol, maximo 50 caracteres, "
        "sin hashtags ni emojis, sin siglas de partidos.\n"
    )
    if tipo == "movimiento":
        detalle = (
            "Tipo MOVIMIENTO: ciudadania organizada, generico y variado "
            "(ej. Ciudadania Feliz, Ciudad Unida, Movimientos Unidos, Voces del "
            "Pueblo).\n"
            "PROHIBIDO escribir 'Movimiento Ciudadano', la sigla 'MC' aislada, "
            "'Morena', 'PAN', 'PRI', 'PRD' o cualquier sigla/nombre de partido.\n"
            "Combina conceptos (Ciudadania, Ciudad, Voces, Gente, Pueblo, "
            "Corazon, Fuerza, Union, Movimiento, Barrio, Colonia, Paz, "
            "Esperanza, Progreso, Comunidad, Vecinos) con adjetivos variados "
            "(Feliz, Unida, Despierta, Viva, Imparable, Conectada, Activa, "
            "Naranja) o conectores (del Pueblo, de la Colonia, en Movimiento).\n"
        )
    elif tipo == "partido":
        detalle = (
            "Tipo SIMILITUD DE PARTIDO: organizaciones que EVOQUEN a un partido "
            "politico SIN nombrarlo ni usar siglas, mediante COLORES y SIMBOLOS "
            "cotidianos (ej. Movimiento Naranja, Fuerza Naranja, Marea Naranja, "
            "Corriente Naranja, Amarillo de Luz, Los Bolillos, Bolillos de la "
            "Colonia, Corazon Naranja, Sol Naranja, Faro Azul, Bandera Guinda, "
            "Rosa en Movimiento, Aurora Naranja, Barrio Naranja, Naranja con "
            "Causa).\n"
            "Colores utiles: Naranja, Guinda, Azul, Rojo, Verde, Amarillo, "
            "Rosa, Morado, Dorado, Celeste, Turquesa. Simbolos utiles: Luz, "
            "Marea, Corriente, Ola, Sol, Faro, Estrella, Alba, Aurora, Viento, "
            "Bandera, Corazon, Bolillos.\n"
            "PROHIBIDO escribir siglas o nombres de partidos (MC, Morena, PAN, "
            "PRI, PRD, PVEM, PT, PES) o frases parecidas a 'Movimiento "
            "Ciudadano'.\n"
        )
    else:
        detalle = (
            "Tipo PERSONA: nombre y apellido(s) mexicanos reales y variados "
            "(ej. Juan Perez, Maria Lopez, Lupita Hernandez), como una persona "
            "comun; evita nombres de politicos famosos.\n"
        )
    extra = ""
    if registro_norm == "ciudadana":
        # La regla dura viaja explicita en el prompt: ciudadana ignora seccion.
        extra += (
            "Registro ciudadana: IGNORA la seccion (cualquiera que sea); usa "
            "nombres de personas mexicanas reales y coloquiales.\n"
        )
    if seccion:
        extra += f"Contexto de seccion: {seccion}.\n"
    if contexto:
        extra += f"Contexto adicional: {str(contexto)[:300]}.\n"
    if evitar:
        muestra = ", ".join(sorted(evitar)[:80])
        extra += f"NO uses estos handles ya ocupados: {muestra}.\n"
    # El bloque de contexto va DESPUES del detalle del tipo y MANDA sobre el.
    bloque_contexto = _bloque_contexto_identidades(
        seccion, registro_norm, tipo, contexto
    )
    partes = [comun, detalle]
    if bloque_contexto:
        partes.append(str(bloque_contexto).rstrip() + "\n")
    if extra:
        partes.append(extra)
    partes.append(
        'Responde SOLO con un JSON array, sin markdown ni explicaciones: '
        '[{"nombre": "...", "handle": "..."}, ...]'
    )
    return "".join(partes)


def _parsear_lote(content: str) -> list:
    """Extrae [{nombre, handle}] de una respuesta de OpenAI (tolerante)."""
    contenido = (content or "").strip()
    if not contenido:
        return []
    contenido = re.sub(r"^```(?:json)?|```$", "", contenido, flags=re.MULTILINE).strip()

    datos = None
    try:
        datos = json.loads(contenido)
    except Exception:
        match = re.search(r"\[[\s\S]*\]", contenido)
        if match:
            try:
                datos = json.loads(match.group(0))
            except Exception:
                datos = None
        if datos is None:
            items = []
            for linea in contenido.splitlines():
                linea = linea.strip().strip(",").strip()
                if not linea or linea in ("[", "]"):
                    continue
                partes = re.split(r"\s*[|;\-–—:]\s*", linea, maxsplit=1)
                if len(partes) < 2:
                    continue
                nombre = partes[0].strip(" \"'")
                handle = partes[1].strip(" \"'").lstrip("@")
                if nombre and handle:
                    items.append({"nombre": nombre, "handle": handle})
            return items

    if isinstance(datos, dict):
        for clave in ("identidades", "cuentas", "resultados", "items"):
            if isinstance(datos.get(clave), list):
                datos = datos[clave]
                break
        else:
            return []
    if not isinstance(datos, list):
        return []

    items = []
    for item in datos:
        if isinstance(item, dict):
            nombre = item.get("nombre") or item.get("name") or item.get("display_name") or ""
            handle = item.get("handle") or item.get("usuario") or item.get("username") or ""
            if nombre and handle:
                items.append({"nombre": nombre, "handle": handle})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            items.append({"nombre": item[0], "handle": item[1]})
    return items


def _pedir_openai_lote(
    cantidad: int,
    tipo: str,
    seccion: str,
    contexto: str,
    evitar: set,
    rng=random,
    registro: str = "",
) -> list:
    """Pide un lote a OpenAI. Devuelve [] si falla o no hay key utilizable.

    ``registro`` se pasa al prompt (bloque de contexto por seccion/registro);
    el kwarg es opcional para no romper llamadas viejas.
    """
    if not _openai_disponible():
        return []
    try:
        import openai
        from core.config import settings

        prompt = _construir_prompt(cantidad, tipo, seccion, contexto, evitar, registro)
        api_key = settings.openai_api_key
        if hasattr(openai, "OpenAI"):  # openai >= 1.0
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=_MODELO_OPENAI,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
            )
            content = response.choices[0].message.content or ""
        else:  # openai 0.28.x
            openai.api_key = api_key
            response = openai.ChatCompletion.create(
                model=_MODELO_OPENAI,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
            )
            content = response.choices[0].message.content or ""
        return _parsear_lote(content)
    except Exception as e:
        logger.warning(f"OpenAI no disponible para identidades, uso generador local: {e}")
        return []


def _pedir_openai_lote_compatible(
    cantidad: int,
    tipo: str,
    seccion: str,
    contexto: str,
    evitar: set,
    rng=random,
    registro: str = "",
) -> list:
    """Llama a ``_pedir_openai_lote`` con ``registro`` y tolera firmas viejas.

    Los consumidores/tests pueden monkeypatchear ``_pedir_openai_lote`` con la
    firma antigua (sin el kwarg ``registro``): si el ``TypeError`` viene de la
    firma, se reintenta sin el kwarg. Cualquier otro error se propaga igual
    que antes (el llamador ya lo maneja).
    """
    try:
        return _pedir_openai_lote(
            cantidad, tipo, seccion, contexto, evitar, rng, registro=registro
        )
    except TypeError:
        return _pedir_openai_lote(cantidad, tipo, seccion, contexto, evitar, rng)


def _identidad_desde_item(item, tipo: str, usados: set, rng=random):
    """Valida un item de OpenAI y lo convierte en identidad; None si no sirve."""
    if not isinstance(item, dict):
        return None
    limpio = _validar_nombre(item.get("nombre"))
    if limpio is None:
        return None
    handle = _normalizar_handle_texto(item.get("handle"))
    if not HANDLE_RE.match(handle) or _handle_prohibido(handle) or _clave_handle(handle) in usados:
        handle = _handle_desde_nombre(limpio, usados, rng)
    if not HANDLE_RE.match(handle) or _handle_prohibido(handle) or _clave_handle(handle) in usados:
        return None
    return {"nombre": limpio, "handle": handle, "tipo": tipo}


# --------------------------------------------------------------------------- #
# Normalizacion de "tipo" de identidad
# --------------------------------------------------------------------------- #
def _normalizar_tipo(tipo) -> str:
    """Normaliza a "persona", "movimiento", "partido" o "mixto" (default "persona").

    Acepta variantes con acentos/mayusculas y sinonimos:
    "movimiento(s)", "politica/o", "institucional", "colectivo", "ciudadania",
    "apoyo" -> "movimiento"; "persona(s)", "ciudadana/o", "real", "individual"
    -> "persona"; "partido(s)", "similitud(es)", "guino"/"guiño", "espectro",
    "color(es)" -> "partido" (similitud con un partido SIN nombrarlo);
    "mixto"/"mixta(s)", "mezcla(s)", "mitad(es)" -> "mixto" (es un reparto
    ~50/50 persona/partido que interpreta ``asignar_propuestas``). Cualquier
    valor desconocido o vacio cae a "persona".
    """
    clave = _normalizar_handle_texto(tipo)  # sin acentos/espacios
    if not clave:
        return "persona"
    if clave in (
        "movimiento", "movimientos", "politica", "politico", "institucional",
        "formal", "organizacion", "colectivo", "coordinadora", "agrupacion",
        "ciudadania", "apoyo",
    ):
        return "movimiento"
    if clave in (
        "partido", "partidos", "similitud", "similitudes", "guino", "espectro",
        "color", "colores",
    ):
        return "partido"
    if clave in ("mixto", "mixta", "mixtos", "mixtas", "mezcla", "mezclas",
                 "mitad", "mitades"):
        return "mixto"
    if clave in (
        "persona", "personas", "ciudadana", "ciudadano", "individual", "real",
        "informal", "coloquial", "usuario",
    ):
        return "persona"
    if clave.startswith(("movimient", "politic", "institucional", "organiz",
                         "colectiv", "agrupac", "ciudadani")):
        return "movimiento"
    if clave.startswith(("partid", "similitud", "espectro", "color")):
        return "partido"
    if clave.startswith("mixt"):
        return "mixto"
    if clave.startswith(("person", "ciudadan")):
        return "persona"
    return "persona"


def _tipo_desde_cuenta(valor) -> str:
    """Mapea Cuenta.tipo_cuenta de BD a "movimiento"/"persona" ("" si vacio).

    "activista" se trata como "persona" (retrato humano, no escena de
    organizacion), igual que hace ``cuentas.fotos._estilo_visual``.
    """
    if not str(valor or "").strip():
        return ""
    texto = _normalizar_handle_texto(valor)
    if texto.startswith("activis"):
        return "persona"
    try:
        from core.registros import normalizar_tipo_cuenta

        clave = normalizar_tipo_cuenta(valor)
        if clave == "politica":
            return "movimiento"
        if clave == "ciudadana":
            return "persona"
    except Exception:
        pass
    texto = _normalizar_handle_texto(valor)
    if texto.startswith(("movimient", "politic", "institucional", "organiz",
                         "colectiv", "agrupac", "ciudadani")):
        return "movimiento"
    return ""


def _registro_desde_cuenta(cuenta) -> str:
    """Registro de la cuenta ("politica"/"activista"/"ciudadana"; "" si no hay).

    Normaliza ``Cuenta.tipo_cuenta`` con ``core.registros.normalizar_tipo_cuenta``
    (import perezoso, con respaldo local). Nunca lanza.
    """
    valor = getattr(cuenta, "tipo_cuenta", "")
    if not str(valor or "").strip():
        return ""
    try:
        from core.registros import normalizar_tipo_cuenta

        return normalizar_tipo_cuenta(valor) or ""
    except Exception:
        return _normalizar_registro(valor)


def _seccion_efectiva_cuenta(cuenta, seccion: str, registro: str) -> str:
    """Seccion efectiva (codigo CI/IP/LIB/JUS) para agrupar y promptear.

    El parametro ``seccion`` de ``asignar_propuestas`` sobreescribe el de la
    cuenta cuando viene; la REGLA DURA es que una cuenta de registro
    ``"ciudadana"`` IGNORA su seccion (devuelve ""). Normaliza con
    ``core.secciones.normalizar_seccion`` (import perezoso). Nunca lanza.
    """
    if registro == "ciudadana":
        return ""
    base = str(seccion or "").strip() or str(getattr(cuenta, "seccion", "") or "").strip()
    if not base:
        return ""
    try:
        from core.secciones import normalizar_seccion

        return normalizar_seccion(base)
    except Exception:
        return base


# --------------------------------------------------------------------------- #
# API publica
# --------------------------------------------------------------------------- #
def ia_disponible() -> bool:
    """True si hay OPENAI_API_KEY real (no placeholder). Publica.

    Envuelve ``_openai_disponible`` para que el dashboard/CLI puedan avisar
    si las propuestas se generaran con IA o 100% local. Nunca lanza.
    """
    return _openai_disponible()


def generar_identidad(
    tipo: str = "persona",
    seccion: str = "",
    contexto: str = "",
    evitar: set | None = None,
) -> dict:
    """Genera UNA identidad valida (100% local, sin red).

    Devuelve ``{"nombre": str, "handle": str, "tipo": tipo_normalizado}``.
    ``evitar`` es un conjunto de handles ya ocupados que no se pueden repetir.
    Nunca lanza excepcion.
    """
    tipo_norm = _normalizar_tipo(tipo)
    usados = _preparar_evitar(evitar)
    try:
        return _generar_identidad_local(tipo_norm, usados, set(), random)
    except Exception as e:
        logger.warning(f"generar_identidad fallo ({tipo_norm}): {e}")
        return _identidad_emergencia(tipo_norm)


def _generar_identidades_con_origen(
    cantidad: int,
    tipo: str = "persona",
    seccion: str = "",
    contexto: str = "",
    usuarios_existentes: set | None = None,
    registro: str = "",
) -> tuple:
    """Igual que ``generar_identidades`` pero devuelve ``(identidades, uso_ia)``.

    ``uso_ia`` es True solo si al menos una identidad del resultado vino de un
    lote validado de OpenAI (no del fallback local). Interno: lo usa
    ``asignar_propuestas`` para reportar ``origen_ia``. ``registro`` viaja al
    prompt/bloque de contexto (opcional, al final).
    """
    resultado: list[dict] = []
    uso_ia = False
    try:
        objetivo = int(cantidad)
    except (TypeError, ValueError):
        return resultado, uso_ia
    if objetivo <= 0:
        return resultado, uso_ia

    tipo_norm = _normalizar_tipo(tipo)
    usados = _preparar_evitar(usuarios_existentes)
    nombres_usados = set()
    usar_openai = _openai_disponible()

    try:
        while len(resultado) < objetivo:
            faltan = objetivo - len(resultado)
            if usar_openai:
                try:
                    lote = _pedir_openai_lote_compatible(
                        min(_LOTE_OPENAI_MAX, faltan), tipo_norm, seccion, contexto,
                        usados, random, registro,
                    )
                except Exception as e:
                    logger.warning(f"Fallo inesperado de OpenAI, sigo local: {e}")
                    lote = []
                if lote:
                    for item in lote:
                        if len(resultado) >= objetivo:
                            break
                        ident = _identidad_desde_item(item, tipo_norm, usados, random)
                        if ident is None:
                            continue
                        clave_nombre = ident["nombre"].lower()
                        if clave_nombre in nombres_usados:
                            continue
                        resultado.append(ident)
                        nombres_usados.add(clave_nombre)
                        usados.add(_clave_handle(ident["handle"]))
                        uso_ia = True
                else:
                    usar_openai = False

            if len(resultado) >= objetivo:
                break
            ident = _generar_identidad_local(tipo_norm, usados, nombres_usados, random)
            resultado.append(ident)
            nombres_usados.add(ident["nombre"].lower())
            usados.add(_clave_handle(ident["handle"]))
    except Exception as e:
        logger.warning(f"generar_identidades se corto en {len(resultado)}/{objetivo}: {e}")
    return resultado, uso_ia


def generar_identidades(
    cantidad: int,
    tipo: str = "persona",
    seccion: str = "",
    contexto: str = "",
    usuarios_existentes: set | None = None,
    registro: str = "",
) -> list[dict]:
    """Genera exactamente ``cantidad`` identidades unicas (nombre y handle).

    Usa OpenAI en lotes (max ~30 por llamada) si hay key real; completa con el
    generador local (Faker es_MX + listas). Si OpenAI falla o no hay key,
    devuelve todo local. Evita los handles de ``usuarios_existentes``.
    ``registro`` ("politica"/"activista"/"ciudadana", opcional al final) viaja
    al prompt para que la IA respete el estilo por seccion/registro.
    Nunca lanza excepcion; si algo revienta devuelve lo que tenga.
    """
    return _generar_identidades_con_origen(
        cantidad, tipo, seccion=seccion, contexto=contexto,
        usuarios_existentes=usuarios_existentes, registro=registro,
    )[0]


def descartar_propuesta(usuario: str) -> bool:
    """Limpia ``nombre_propuesto``/``handle_propuesto`` de la cuenta.

    Devuelve True si la cuenta existe y se limpio; False si no existe o hubo
    un error de BD.
    """
    usuario = str(usuario or "").strip()
    if not usuario:
        return False
    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is None:
                return False
            cuenta.nombre_propuesto = ""
            cuenta.handle_propuesto = ""
        return True
    except Exception as e:
        logger.warning(f"No se pudo descartar la propuesta de {usuario}: {e}")
        return False


def _tipos_mezcla(cantidad: int, rng=random) -> list:
    """Reparto ~50% persona / ~50% partido, barajado por cuenta.

    Con cantidad par queda exactamente mitad y mitad; con impar se sortea el
    cupo sobrante. Nunca lanza.
    """
    try:
        cantidad = int(cantidad)
    except (TypeError, ValueError):
        return []
    if cantidad <= 0:
        return []
    if cantidad == 1:
        return [rng.choice(("persona", "partido"))]
    n_persona = cantidad // 2
    if cantidad % 2:
        n_persona += rng.choice((0, 1))
    tipos = ["persona"] * n_persona + ["partido"] * (cantidad - n_persona)
    rng.shuffle(tipos)
    return tipos


def _identidad_es_unica(candidata, ocupados: set, nombres_ocupados: set) -> bool:
    """True si el handle y el nombre de la candidata no chocan con los ocupados."""
    if not isinstance(candidata, dict):
        return False
    handle = str(candidata.get("handle") or "")
    nombre = _normalizar_texto(candidata.get("nombre"))
    if not nombre or not HANDLE_RE.match(handle):
        return False
    clave_h = _clave_handle(handle)
    return bool(
        clave_h
        and clave_h not in ocupados
        and nombre not in nombres_ocupados
    )


def asignar_propuestas(
    usuarios: list[str],
    tipo: str = "auto",
    seccion: str = "",
    dry_run: bool = False,
    contexto: str = "",
    proteger_brandeadas: bool = False,
) -> dict:
    """Genera y guarda ``nombre_propuesto``/``handle_propuesto`` por cuenta.

    ``tipo`` (acepta sinonimos via ``_normalizar_tipo``):
      * ``"auto"`` (default): usa ``Cuenta.tipo_cuenta`` — "politica" ->
        "movimiento", "ciudadana" -> "persona"; sin tipo definido elige al azar
        (50/50 persona/movimiento).
      * ``"persona"``/``"movimiento"``/``"partido"``: fuerza ese tipo
        ("partido" = similitud con un partido SIN nombrarlo: colores/simbolos).
      * ``"mixto"`` ("mezcla"/"mitad"): reparte ~50% persona / ~50% partido,
        barajado cuenta por cuenta.

    Cada cuenta aporta tambien su REGISTRO (``Cuenta.tipo_cuenta`` normalizado:
    politica/activista/ciudadana) y su SECCION (``Cuenta.seccion`` normalizada:
    CI/IP/LIB/JUS). Los lotes se agrupan por ``(tipo, seccion, registro)`` y los
    tres viajan al prompt. El parametro ``seccion`` sobreescribe el de la cuenta
    cuando viene; REGLA DURA: con registro "ciudadana" la seccion se IGNORA.

    ``proteger_brandeadas=True`` (opcional, al final) OMITE antes de generar a
    las cuentas que ya tienen identidad humana o trabajo previo (ver
    ``_cuenta_protegida``/``es_handle_generico``): ni IA ni generador local, y
    TAMPOCO se tocan en la BD. Se reportan en ``omitidas_protegidas``,
    ``protegidas_usuarios`` y ``protegidas_detalle``. Con ``False`` (default)
    el comportamiento es identico al historico.

    Usa ``generar_identidades``/OpenAI en lotes (<=30 por llamada) y completa
    con el generador local; si no hay key real de OpenAI sigue 100% local.
    Valida que nombre y handle no choquen con NINGUN ocupado (``usuario``,
    ``handle_actual``, ``handle_propuesto`` y
    ``nombre_mostrado``/``nombre_propuesto`` de todas las Cuentas) ni con las
    identidades ya generadas en esta corrida; con ``dry_run=True`` genera y
    devuelve SIN escribir en la BD.

    Devuelve:
        {"total", "ok", "propuestas": [{"usuario","nombre","handle","tipo"}],
         "errores": [str], "dry_run": bool, "origen_ia": bool,
         "persona": n, "partido": n, "movimiento": n,
         "omitidas_protegidas": n, "protegidas_usuarios": [usuario, ...],
         "protegidas_detalle": [{"usuario","campo","valor"}, ...]}

    ``total`` es SIEMPRE el de la lista recibida (no el de las cuentas
    procesadas), para que el dashboard muestre el universo real del lote.
    """
    resultado = {
        "total": 0,
        "ok": 0,
        "propuestas": [],
        "errores": [],
        "dry_run": bool(dry_run),
        "origen_ia": False,
        "persona": 0,
        "partido": 0,
        "movimiento": 0,
        "omitidas_protegidas": 0,
        "protegidas_usuarios": [],
        "protegidas_detalle": [],
    }
    try:
        lista_usuarios = []
        vistos = set()
        for usuario in usuarios or []:
            usuario = str(usuario or "").strip()
            if usuario and usuario not in vistos:
                vistos.add(usuario)
                lista_usuarios.append(usuario)
    except Exception as e:
        resultado["errores"].append(f"Lista de usuarios invalida: {e}")
        return resultado
    resultado["total"] = len(lista_usuarios)
    if not lista_usuarios:
        return resultado

    solicitado = _normalizar_handle_texto(tipo)
    tipo_forzado = "auto" if not solicitado or solicitado == "auto" else _normalizar_tipo(tipo)

    try:
        from core.database import get_db_session
        from core.models import Cuenta
    except Exception as e:
        resultado["errores"].append(f"BD no disponible: {e}")
        return resultado

    # (1) Lee cuentas/ocupados y decide el tipo efectivo de CADA cuenta.
    asignacion = []
    ocupados = set()
    nombres_ocupados = set()
    try:
        with get_db_session() as db:
            filas = db.query(Cuenta).filter(Cuenta.usuario.in_(lista_usuarios)).all()
            por_usuario = {fila.usuario: fila for fila in filas}

            # Ocupados: TODAS las cuentas (usuario / handle_actual / propuesto)
            # y nombres ya visibles/propuestos.
            for fila in db.query(Cuenta).all():
                for valor in (fila.usuario, fila.handle_actual, fila.handle_propuesto):
                    clave = _clave_handle(valor)
                    if clave:
                        ocupados.add(clave)
                for valor in (fila.nombre_mostrado, fila.nombre_propuesto):
                    nombre = _normalizar_texto(valor)
                    if nombre:
                        nombres_ocupados.add(nombre)

            for usuario in lista_usuarios:
                cuenta = por_usuario.get(usuario)
                if cuenta is None:
                    resultado["errores"].append(f"usuario no encontrado: {usuario}")
                    continue

                # Filtro anti-sobrescritura: identidad humana ya existente o
                # propuesta previa -> no se genera ni se toca nada.
                if proteger_brandeadas:
                    protegida, campo, valor = _cuenta_protegida(cuenta)
                    if protegida:
                        resultado["omitidas_protegidas"] += 1
                        resultado["protegidas_usuarios"].append(usuario)
                        resultado["protegidas_detalle"].append(
                            {"usuario": usuario, "campo": campo, "valor": valor}
                        )
                        continue

                if tipo_forzado == "auto":
                    tipo_identidad = _tipo_desde_cuenta(getattr(cuenta, "tipo_cuenta", ""))
                    if not tipo_identidad:
                        tipo_identidad = "movimiento" if random.random() < 0.5 else "persona"
                else:
                    tipo_identidad = tipo_forzado  # se ajusta abajo si es "mixto"

                registro_cuenta = _registro_desde_cuenta(cuenta)
                seccion_ctx = _seccion_efectiva_cuenta(cuenta, seccion, registro_cuenta)
                asignacion.append(
                    {
                        "usuario": usuario,
                        "tipo": tipo_identidad,
                        "seccion": seccion_ctx,
                        "registro": registro_cuenta,
                    }
                )
    except Exception as e:
        logger.exception(f"Error leyendo cuentas en asignar_propuestas: {e}")
        resultado["errores"].append(f"error de BD: {e}")
        return resultado

    if not asignacion:
        return resultado

    # (1b) "mixto": baraja ~50% persona / ~50% partido entre las cuentas reales.
    if tipo_forzado == "mixto":
        for item, tipo_mezcla in zip(asignacion, _tipos_mezcla(len(asignacion))):
            item["tipo"] = tipo_mezcla

    # (2) Genera por lotes agrupando por (tipo, seccion, registro). Fuera de la
    # sesion de BD: la IA puede tardar y no hay que retener la conexion (SQLite).
    grupos = {}
    orden = []
    for indice, item in enumerate(asignacion):
        clave = (item["tipo"], item["seccion"], item["registro"])
        if clave not in grupos:
            grupos[clave] = []
            orden.append(clave)
        grupos[clave].append(indice)

    generadas = {}
    generados = set()  # handles de TODOS los lotes (evita choques entre grupos)
    for clave in orden:
        indices = grupos[clave]
        tipo_grupo, seccion_grupo, registro_grupo = clave
        try:
            lote, uso_ia = _generar_identidades_con_origen(
                len(indices),
                tipo_grupo,
                seccion=seccion_grupo,
                contexto=contexto,
                usuarios_existentes=set(ocupados) | generados,
                registro=registro_grupo,
            )
        except Exception as e:
            logger.warning(f"Lote de identidades fallo ({tipo_grupo}): {e}")
            lote, uso_ia = [], False
        if uso_ia:
            resultado["origen_ia"] = True
        for ident in lote:
            generados.add(_clave_handle(ident.get("handle")))
        for indice, ident in zip(indices, lote):
            generadas[indice] = ident

    # (3) Asigna una identidad valida por cuenta; completa con el generador
    # local (max 60 intentos por cuenta) si el lote no alcanzo o choco.
    asignados = set()
    nombres_asignados = set()
    for indice, item in enumerate(asignacion):
        usuario = item["usuario"]
        identidad = None
        candidata = generadas.get(indice)
        if candidata is not None and _identidad_es_unica(
            candidata, ocupados | asignados, nombres_ocupados | nombres_asignados
        ):
            identidad = candidata
        if identidad is None:
            evitar = set(ocupados) | generados | asignados
            for _ in range(60):
                candidata = generar_identidad(
                    item["tipo"], seccion=item["seccion"], evitar=evitar
                )
                if _identidad_es_unica(
                    candidata, ocupados | asignados, nombres_ocupados | nombres_asignados
                ):
                    identidad = candidata
                    break
        if identidad is None:
            resultado["errores"].append(
                f"no se pudo generar identidad unica para {usuario}"
            )
            continue

        asignados.add(_clave_handle(identidad["handle"]))
        nombres_asignados.add(_normalizar_texto(identidad["nombre"]))
        tipo_real = identidad.get("tipo") if identidad.get("tipo") in (
            "persona", "movimiento", "partido",
        ) else "persona"
        resultado["propuestas"].append(
            {
                "usuario": usuario,
                "nombre": identidad["nombre"],
                "handle": identidad["handle"],
                "tipo": tipo_real,
            }
        )
        resultado["ok"] += 1
        resultado[tipo_real] = resultado.get(tipo_real, 0) + 1

    # (4) Persiste (fuera de la sesion de generacion) salvo dry_run.
    if not dry_run and resultado["propuestas"]:
        try:
            with get_db_session() as db:
                filas = db.query(Cuenta).filter(
                    Cuenta.usuario.in_([p["usuario"] for p in resultado["propuestas"]])
                ).all()
                por_usuario = {fila.usuario: fila for fila in filas}
                for propuesta in resultado["propuestas"]:
                    cuenta = por_usuario.get(propuesta["usuario"])
                    if cuenta is not None:
                        cuenta.nombre_propuesto = propuesta["nombre"]
                        cuenta.handle_propuesto = propuesta["handle"]
        except Exception as e:
            logger.exception(f"Error guardando propuestas en la BD: {e}")
            resultado["errores"].append(f"error de BD al guardar: {e}")

    return resultado


def aplicar_propuesta(usuario: str, password: str = "") -> dict:
    """Aplica en X la propuesta pendiente de una cuenta (requiere Chrome).

    Lee ``Cuenta.nombre_propuesto``/``handle_propuesto`` y llama a
    ``TwitterBot(usuario).cambiar_perfil(...)``. Si ``password`` viene vacia se
    pasa ``None`` para que ``cambiar_perfil`` la lea de la BD (obligatoria solo
    cuando hay propuesta de handle). Si el cambio es exitoso actualiza
    ``nombre_mostrado``/``handle_actual`` y limpia la propuesta aplicada.
    Devuelve ``{"ok", "usuario", "nombre", "handle", "error"}``. Nunca lanza.
    """
    resultado = {
        "ok": False,
        "usuario": str(usuario or "").strip(),
        "nombre": False,
        "handle": False,
        "error": "",
    }
    usuario = resultado["usuario"]
    if not usuario:
        resultado["error"] = "usuario vacio"
        return resultado

    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is None:
                resultado["error"] = "usuario no encontrado en la BD"
                return resultado
            nombre = (cuenta.nombre_propuesto or "").strip()
            handle = (cuenta.handle_propuesto or "").strip().lstrip("@")

        if not nombre and not handle:
            resultado["error"] = "la cuenta no tiene propuesta pendiente"
            return resultado

        # Import perezoso: Selenium/Chrome solo se carga al aplicar de verdad.
        from plataformas.twitter.selenium_bot import TwitterBot

        bot = TwitterBot(usuario)
        try:
            salida = bot.cambiar_perfil(
                nombre=nombre or None,
                handle=handle or None,
                password=(password or None),
            )
        finally:
            try:
                bot.cerrar()
            except Exception:
                pass

        resultado["nombre"] = bool(salida.get("nombre"))
        resultado["handle"] = bool(salida.get("handle"))
        resultado["ok"] = bool(salida.get("ok"))
        resultado["error"] = str(salida.get("error") or "")

        # Solo limpia la propuesta que de verdad se aplico (si el handle fallo,
        # se conserva para reintentar sin volver a generar).
        if resultado["ok"]:
            with get_db_session() as db:
                reg = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
                if reg is not None:
                    if resultado["nombre"]:
                        reg.nombre_mostrado = nombre
                        reg.nombre_propuesto = ""
                    if resultado["handle"]:
                        reg.handle_actual = handle
                        reg.handle_propuesto = ""
        return resultado

    except Exception as e:
        resultado["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"Error aplicando propuesta de {usuario}: {e}")
        return resultado


def _renombrar_clave_interna(usuario: str) -> dict:
    """Renombra la clave interna al @ real tras aplicar (NUNCA lanza).

    Import perezoso de ``core.renombrar`` (solo se carga si ``renombrar=True``).
    Devuelve ``{"ok": bool, "error": str}``.
    """
    try:
        from core.renombrar import renombrar_al_handle_actual

        salida = renombrar_al_handle_actual(usuario)
        if isinstance(salida, dict) and salida.get("ok") and salida.get("renombrado"):
            return {"ok": True, "error": ""}
        error = ""
        if isinstance(salida, dict):
            error = str(salida.get("error") or "").strip()
        return {"ok": False, "error": error or "renombrado no confirmado"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def aplicar_propuestas_en_lote(
    usuarios: list[str],
    max_workers: int = 2,
    password: str = "",
    renombrar: bool = False,
    callback=None,
    cancelar=None,
) -> dict:
    """Aplica ``aplicar_propuesta`` en paralelo (ThreadPoolExecutor).

    Parametros:
      * ``usuarios``: lista de claves internas; se deduplica y se descartan
        vacios.
      * ``max_workers``: cuantas cuentas se procesan a la vez, acotado a
        ``1..4`` (default 2). Con SQLite conviene 2-3: cada worker abre Chrome
        y hace escrituras cortas; mas concurrencia puede bloquear la BD.
      * ``password``: se pasa TAL CUAL a ``aplicar_propuesta`` (vacio = usa la
        de la BD).
      * ``renombrar=True``: tras un ok con handle aplicado llama
        ``core.renombrar.renombrar_al_handle_actual`` (import perezoso; nunca
        lanza). Si el renombrado funciona marca ``"renombrado": True``; si
        falla guarda ``"error_renombrado"`` y ``ok`` sigue True. Se ejecuta en
        el hilo recolector (serializado) para no competir por SQLite.
      * ``callback(progreso)``: se invoca por cada cuenta TERMINADA, SIEMPRE
        desde el hilo recolector (nunca desde los workers) con
        ``{"total", "hechas", "usuario", "ok", "error"}``. Sus excepciones se
        ignoran.
      * ``cancelar``: ``threading.Event`` opcional; si esta set, no se lanzan
        mas cuentas (las que ya estan en curso terminan).

    Nunca lanza. Devuelve:
        {"total", "ok", "fallidos", "renombrados", "cancelado": bool,
         "resultados": [{"usuario", "ok", "nombre", "handle", "error",
                         "renombrado", "error_renombrado"}],
         "errores": [str, ...]}
    """
    resultado = {
        "total": 0,
        "ok": 0,
        "fallidos": 0,
        "renombrados": 0,
        "cancelado": False,
        "resultados": [],
        "errores": [],
    }

    try:
        lista_usuarios = []
        vistos = set()
        for usuario in usuarios or []:
            usuario = str(usuario or "").strip()
            if usuario and usuario not in vistos:
                vistos.add(usuario)
                lista_usuarios.append(usuario)
    except Exception as e:
        resultado["errores"].append(f"lista de usuarios invalida: {e}")
        return resultado
    resultado["total"] = len(lista_usuarios)
    if not lista_usuarios:
        return resultado

    try:
        n_workers = int(max_workers)
    except (TypeError, ValueError):
        n_workers = 2
    n_workers = max(1, min(4, n_workers))

    def _aplicar_una(usuario: str) -> dict:
        """Worker: aplica la propuesta y arma el dict de resultado base."""
        entrada = {
            "usuario": usuario,
            "ok": False,
            "nombre": False,
            "handle": False,
            "error": "",
            "renombrado": False,
            "error_renombrado": "",
        }
        try:
            salida = aplicar_propuesta(usuario, password)
            if isinstance(salida, dict):
                entrada["ok"] = bool(salida.get("ok"))
                entrada["nombre"] = bool(salida.get("nombre"))
                entrada["handle"] = bool(salida.get("handle"))
                entrada["error"] = str(salida.get("error") or "")
            else:
                entrada["error"] = "respuesta invalida de aplicar_propuesta"
        except Exception as e:
            entrada["error"] = f"{type(e).__name__}: {e}"
        return entrada

    hechas = 0
    cancelado = False
    try:
        with ThreadPoolExecutor(max_workers=n_workers) as ejecutor:
            pendientes = list(lista_usuarios)
            futuros = {}

            def _en_cancelacion() -> bool:
                return cancelar is not None and cancelar.is_set()

            def _lanzar_hasta_llenar():
                """Lanza cuentas hasta llenar los workers; False si cancelaron."""
                while pendientes and len(futuros) < n_workers:
                    if _en_cancelacion():
                        return False
                    usuario = pendientes.pop(0)
                    futuros[ejecutor.submit(_aplicar_una, usuario)] = usuario
                return True

            if not _lanzar_hasta_llenar():
                cancelado = True

            while futuros:
                for futuro in as_completed(list(futuros)):
                    usuario = futuros.pop(futuro)
                    try:
                        entrada = futuro.result()
                    except Exception as e:
                        entrada = {
                            "usuario": usuario,
                            "ok": False,
                            "nombre": False,
                            "handle": False,
                            "error": f"{type(e).__name__}: {e}",
                            "renombrado": False,
                            "error_renombrado": "",
                        }
                    # Renombrado (hilo recolector, serializado): solo si el
                    # handle se aplico de verdad en X.
                    if renombrar and entrada.get("ok") and entrada.get("handle"):
                        info = _renombrar_clave_interna(usuario)
                        if info.get("ok"):
                            entrada["renombrado"] = True
                            resultado["renombrados"] += 1
                        else:
                            entrada["error_renombrado"] = (
                                info.get("error") or "renombrado no confirmado"
                            )

                    if entrada.get("ok"):
                        resultado["ok"] += 1
                    else:
                        resultado["fallidos"] += 1
                        error = str(entrada.get("error") or "").strip()
                        resultado["errores"].append(
                            f"{usuario}: {error}" if error else usuario
                        )
                    hechas += 1
                    resultado["resultados"].append(entrada)

                    if callback is not None:
                        try:
                            callback(
                                {
                                    "total": resultado["total"],
                                    "hechas": hechas,
                                    "usuario": usuario,
                                    "ok": bool(entrada.get("ok")),
                                    "error": str(entrada.get("error") or ""),
                                }
                            )
                        except Exception:
                            pass

                    if pendientes:
                        if _en_cancelacion():
                            cancelado = True
                        else:
                            siguiente = pendientes.pop(0)
                            futuros[ejecutor.submit(_aplicar_una, siguiente)] = siguiente
    except Exception as e:
        logger.warning(f"Error en aplicar_propuestas_en_lote: {e}")
        resultado["errores"].append(f"error del lote: {type(e).__name__}: {e}")
        if cancelar is not None and cancelar.is_set():
            cancelado = True

    resultado["cancelado"] = bool(cancelado)
    return resultado


# --------------------------------------------------------------------------- #
# Personalidades (tono de voz + intereses) para el mantenimiento programado
# --------------------------------------------------------------------------- #
# Cada personalidad describe el tono, los temas y los gustos de una cuenta
# para que el mantenimiento programado genere tuits acordes a ella. Se guarda
# en ``Cuenta.personalidad``. OpenAI es opcional; el generador local usa
# plantillas + vocabulario mexicano para garantizar variedad y maxima
# disponibilidad (nunca lanza excepcion).
_PERSONALIDAD_MAX = 300
_LOTE_PERSONALIDAD_MAX = _LOTE_OPENAI_MAX  # 30 por llamada a OpenAI

# Mapeo perfil -> registro cuando la cuenta no trae registro explicito, y
# descripcion exacta de cada perfil (para prompts y documentacion). El contrato
# vive en core.perfiles (PERFILES_PERSONALIDAD, normalizar_perfil, ...).
_PERFIL_A_REGISTRO = {
    "formal": "politica",
    "ciudadano": "ciudadana",
    "popular": "ciudadana",
}
_PERFIL_DESCRIPCION = {
    "formal": "analitico y estructurado",
    "ciudadano": "par de renglones, analisis intermedio",
    "popular": "casual, un renglon, con faltas de ortografia intencionales",
}

# --- Vocabulario de "politica / institucional" (sobrio, sin slang) ---------- #
_TONOS_POLITICOS = [
    "formal y mesurado",
    "institucional y sereno",
    "analítico y propositivo",
    "sobrio y respetuoso",
    "técnico y prudente",
    "cercano pero formal",
    "reflexivo y documentado",
    "serio y ecuánime",
    "didáctico y ordenado",
    "firme sin estridencias",
]
_INTERESES_POLITICOS = [
    "la gestión pública",
    "las políticas públicas",
    "el presupuesto público",
    "el gasto público",
    "la transparencia",
    "el análisis de datos",
    "la economía",
    "la historia de México",
    "el marco legal",
    "la educación pública",
    "la salud pública",
    "la movilidad",
    "la seguridad ciudadana",
    "el desarrollo urbano",
    "la política exterior",
    "la rendición de cuentas",
    "el debate parlamentario",
    "la geopolítica",
    "la administración pública",
    "la agenda legislativa",
    "los indicadores económicos",
    "la estadística",
    "el periodismo serio",
    "los archivos históricos",
    "el servicio público",
]
_GUSTOS_POLITICOS = [
    "la lectura por las tardes",
    "el café sin prisas",
    "la historia nacional",
    "los documentales",
    "la economía explicada simple",
    "caminar la ciudad",
    "las mesas de análisis",
    "los datos duros",
    "la música clásica",
    "el ajedrez",
    "la fotografía documental",
    "la cocina tradicional",
    "las biografías",
    "los museos",
    "la conversación informada",
]
_CIERRES_POLITICOS = [
    "Evita el alarmismo y las descalificaciones.",
    "Prefiere argumentos con datos y fuentes.",
    "No entra en pleitos ni usa groserías.",
    "Cuida el lenguaje y la ortografía.",
    "Comparte propuestas más que consignas.",
    "Mantiene un tono respetuoso incluso al criticar.",
    "Cita cifras y contexto antes de opinar.",
]

# --- Vocabulario de "ciudadana" (coloquial mexicano, cotidiano) ------------- #
_TONOS_CIUDADANOS = [
    "relajado y bromista",
    "coloquial y directo",
    "cálido y platicador",
    "espontáneo y ocurrente",
    "sarcástico y relajado",
    "entusiasta y ruidoso",
    "tranquilo y observador",
    "fanfarrón y buena onda",
    "nostálgico y sentimental",
    "curioso y preguntón",
    "fiestero pero trabajador",
    "de barrio y sin pretensiones",
]
_INTERESES_CIUDADANOS = [
    "el futbol",
    "las Águilas del América",
    "los Tigres",
    "las Chivas",
    "la Selección Mexicana",
    "los corridos",
    "la música de banda",
    "el rock en español",
    "las cumbias",
    "el sonidero",
    "el reguetón",
    "los tacos al pastor",
    "el pozole",
    "los tamales",
    "los chilaquiles",
    "las quesadillas",
    "el pan de muerto",
    "las marquesitas",
    "las carnitas",
    "los esquites",
    "la vida de barrio",
    "su colonia",
    "la familia",
    "los domingos familiares",
    "los memes",
    "las telenovelas",
    "las series",
    "el streaming",
    "la tecnología",
    "los celulares",
    "los videojuegos",
    "viajar en carretera",
    "la playa",
    "los pueblos mágicos",
    "las tradiciones",
    "el Día de Muertos",
    "las posadas",
    "los tianguis",
    "la cultura azteca",
    "la historia prehispánica",
    "las pirámides",
    "Tenochtitlan",
    "el arte urbano",
    "los conciertos",
    "bailar",
    "el café de olla",
    "las luchas libres",
    "el box mexicano",
    "los perros callejeros",
    "los gatos",
    "cocinar en casa",
    "las redes sociales",
    "los paseos en bici",
    "el mercado de la esquina",
]
_GUSTOS_CIUDADANOS = [
    "los chistes locales",
    "las pláticas de banqueta",
    "los memes del momento",
    "las retas de futbol",
    "las tardes de carnaval",
    "la comida de la esquina",
    "escuchar música mientras trabaja",
    "los viajes improvisados",
    "los cumpleaños familiares",
    "las fiestas patronales",
    "el tianguis del domingo",
    "los chismes del barrio",
    "ver partidos con los compas",
    "las series de moda",
    "aprender cosas nuevas en internet",
    "los videos virales",
]
_CIERRES_CIUDADANOS = [
    "Escribe como platica en la banqueta.",
    "Usa groserías leves y mucho sentido del humor.",
    "Cuenta anécdotas con su familia y sus compas.",
    "Se ríe de todo, incluso de sí misma.",
    "Opina sin pretensiones, como cualquier vecino.",
    "Le entusiasman las noticias de su colonia.",
    "Pregunta y responde con confianza.",
]

# --- Vocabulario "activista" (intermedio: terminos politicos + calle) ------- #
# Mezcla causas/derechos/organizacion (registro politico) con palabras de
# barrio y cotidianidad (registro ciudadano), con licencias leves de escritura
# (se permite omitir tildes de vez en cuando), pero SIN groserias ni insultos.
_TONOS_ACTIVISTAS = [
    "cercano y movilizador",
    "territorial y directo",
    "crítico pero respetuoso",
    "popular y documentado",
    "callejero y organizado",
    "sencillo y político",
    "firme y comunitario",
    "apasionado sin groserías",
    "claridoso y con datos",
    "de asamblea y banqueta",
]
_INTERESES_ACTIVISTAS = [
    "los derechos humanos",
    "la defensa del territorio",
    "las causas sociales",
    "el agua de la colonia",
    "la educación pública",
    "la salud comunitaria",
    "el empleo digno",
    "la organización vecinal",
    "el presupuesto participativo",
    "las asambleas de barrio",
    "los pueblos originarios",
    "el medio ambiente",
    "la transparencia",
    "la movilidad",
    "la cultura comunitaria",
    "los jóvenes del barrio",
    "las madres que buscan",
    "los mercados locales",
    "la radio comunitaria",
    "el fútbol de la colonia",
]
_GUSTOS_ACTIVISTAS = [
    "las asambleas de colonia",
    "el tianguis del domingo",
    "la música de protesta",
    "los murales del barrio",
    "caminar el territorio",
    "el café de olla",
    "las pláticas de banqueta",
    "la comida corrida",
    "los conciertos comunitarios",
    "la lectura",
    "el fútbol de barrio",
    "los documentales",
    "la radio",
    "las ferias del pueblo",
    "el mezcal",
]
_CIERRES_ACTIVISTAS = [
    "Combina términos políticos con lenguaje popular, sin groserías.",
    "Escribe como en una asamblea de colonia: claro y sin tanta formalidad.",
    "De vez en cuando omite tildes, como al escribir rápido en el celular.",
    "Se permite una que otra palabra de barrio, pero documenta lo que dice.",
    "Habla de las causas con ejemplos de su colonia.",
    "Critica con argumentos y sin insultos.",
    "Prefiere consignas cortas y mucha organización.",
]

_PLANTILLAS_POLITICAS = [
    "{sujeto} de tono {tono}. Habla de {i1}, {i2} y {i3}, siempre con datos y contexto. {cierre}",
    "{sujeto} con tono {tono}: sigue de cerca {i1} y {i2}, y suele comentar {tema}. {cierre}",
    "Se expresa con tono {tono} sobre {tema}, {i1} y {i2}. Entre sus gustos están {g1} y {g2}. {cierre}",
    "{sujeto} con tono {tono} que sigue {tema} y disfruta {g1}; también le interesan {i1} y {i2}. {cierre}",
    "Tono {tono} para hablar de {i1}, {i2} y {tema}; prefiere {g1} y {g2} fuera de la política. {cierre}",
]
_PLANTILLAS_CIUDADANAS = [
    "{sujeto} de tono {tono}. Disfruta {i1}, {i2} y {i3}; habla de {tema} como en la banqueta. {cierre}",
    "{sujeto} con tono {tono}: le apasionan {i1} y {i2}, y también {g1}. Opina de {tema} sin complicarse. {cierre}",
    "Habla con tono {tono} de {tema}, {i1} y {i2}; entre sus gustos están {g1} y {g2}. {cierre}",
    "{sujeto} de tono {tono}: publica de {i1}, {i2} y {i3}, y presume {g1}. {cierre}",
    "Tono {tono}, bien mexicano: hoy habla de {i1} y mañana de {tema}; ama {g1} y {g2}. {cierre}",
]
_PLANTILLAS_ACTIVISTAS = [
    "{sujeto} de tono {tono}. Habla de {i1}, {i2} y {i3} mezclando términos políticos con lenguaje de calle. {cierre}",
    "{sujeto} con tono {tono}: le mueve {i1} y {i2}, y lo comenta con sus compas en la banqueta. {cierre}",
    "Habla con tono {tono} de {tema}, {i1} y {i2}; en su tiempo libre disfruta {g1} y {g2}. {cierre}",
    "{sujeto} de tono {tono}: sigue {tema} y las causas de {i1}; presume {g1} y {g2}. {cierre}",
    "Tono {tono}: mezcla {i1}, {i2} y {tema} con dichos y palabras de barrio. {cierre}",
]

# Jerga que jamas deberia aparecer en una personalidad "politica".
_JERGA_COLOQUIAL = {
    "wey", "güey", "guey", "compa", "compas", "carnal", "carnales",
    "chido", "chida", "chidos", "chidas", "neta", "jaja", "jajaja",
    "jajajaja", "morra", "morro", "morras", "morros", "pedo", "pedos",
    "peda", "crudo", "cruda", "chale", "órale", "orale", "chingón",
    "chingon", "chingona", "simón", "simon", "naco", "naca",
}

# Licencias leves permitidas al registro "activista" (lenguaje popular sin
# caer en groserías ni slang pesado). El resto de _JERGA_COLOQUIAL se prohibe.
_LICENCIAS_ACTIVISTAS = {
    "compa", "compas", "neta", "chido", "chida", "chidos", "chidas",
    "orale", "chale", "simon", "sale", "va",
}
_JERGA_FUERTE_ACTIVISTA = {
    token
    for token in (_normalizar_texto(t) for t in _JERGA_COLOQUIAL)
    if token and token not in _LICENCIAS_ACTIVISTAS
}

# --- Vocabulario "popular" (abreviado, de barrio, SIN groserias) ------------ #
# El perfil "popular" (ver core.perfiles) escribe muy casual, en un solo
# renglon y con faltas intencionales ("q", "pa", "tons", "xq", "k", tildes
# omitidas), pero jamas groserias ni insultos.
_TONOS_POPULARES = [
    "muy casual y de barrio",
    "relajado y sin formalidades",
    "callejero y buena onda",
    "de banqueta y sin filtros",
    "bromista y directo",
    "espontaneo y despreocupado",
    "alegre y fiestero",
    "sencillo y sin pretensiones",
    "ocurrente y platicador",
    "de barrio y sin rodeos",
]
_INTERESES_POPULARES = [
    "el futbol de la colonia",
    "las retas con los compas",
    "los tacos de la esquina",
    "las quesadillas",
    "el tianguis",
    "su colonia",
    "los memes",
    "la musica de barrio",
    "las cumbias",
    "los corridos",
    "la banda",
    "el regueton",
    "los perros callejeros",
    "los gatos",
    "las fiestas del barrio",
    "los dias de descanso",
    "el mercado de la esquina",
    "la comida corrida",
    "los videojuegos",
    "las redes sociales",
    "los videos virales",
    "bailar",
    "los conciertos",
    "el cafe de olla",
    "las carnes asadas",
    "los domingos familiares",
    "las platicas de banqueta",
    "los chismes del barrio",
]
_GUSTOS_POPULARES = [
    "las platicas de banqueta",
    "los chistes locales",
    "los memes del momento",
    "las retas de futbol",
    "la comida de la esquina",
    "escuchar musica mientras trabaja",
    "los viajes improvisados",
    "las fiestas patronales",
    "el tianguis del domingo",
    "ver partidos con los compas",
    "los videos virales",
    "aprender cosas en internet",
    "las tardes de carnaval",
    "los cumpleanos familiares",
]
_CIERRES_POPULARES = [
    "Escribe cortito, como mensaje de celular.",
    "Usa 'q', 'pa' y 'tons' de vez en cuando.",
    "Publica en un solo renglon y sin tanta coma.",
    "Se le van las tildes cuando escribe rapido, pero nada de groserias.",
    "Habla de su barrio con mucho sentido del humor.",
    "Escribe como en el chat del barrio, sin insultar a nadie.",
    "Le da igual la ortografia; le importa la onda.",
]
_PLANTILLAS_POPULARES = [
    "{sujeto} con tono {tono}. Publica de {i1}, {i2} y {tema} en un solo renglon. {cierre}",
    "{sujeto} de tono {tono}: le late {i1} y {i2}, y tambien {g1}. {cierre}",
    "Tono {tono}: habla de {i1}, {i2} y {tema} como en el chat. Entre sus gustos, {g1} y {g2}. {cierre}",
    "{sujeto} con tono {tono} que solo escribe de {tema}, {i1} y {g1}. {cierre}",
    "Tono {tono}, cero formalidad: hoy {i1}, manana {tema}, y siempre {g1}. {cierre}",
]

# Licencias de escritura del perfil "popular": slang leve y abreviaturas de
# celular. El resto del slang de _JERGA_COLOQUIAL (wey, pedo, ...) se prohibe.
_LICENCIAS_POPULARES = set(_LICENCIAS_ACTIVISTAS) | {
    "q", "pa", "tons", "xq", "k", "jaja", "jajaja", "jajajaja",
    "carnal", "carnales", "morro", "morra", "morros", "morras",
}
_JERGA_PROHIBIDA_POPULAR = {
    token
    for token in (_normalizar_texto(t) for t in _JERGA_COLOQUIAL)
    if token and token not in _LICENCIAS_POPULARES
}

# Groserias/insultos que NINGUN registro ni perfil debe usar (tokens ya
# normalizados: minusculas y sin acentos).
_GROSERIAS = {
    "chinga", "chingar", "chingado", "chingada", "chingados", "chingadas",
    "chingon", "chingona", "chingones", "chingonas",
    "pinche", "pinches",
    "pendejo", "pendeja", "pendejos", "pendejas", "pendejada", "pendejadas",
    "mierda", "mierdas",
    "cabron", "cabrona", "cabrones", "cabronas",
    "joder", "jodido", "jodida", "jodidos", "jodidas",
    "puto", "puta", "putos", "putas", "putazo",
    "culero", "culera", "culeros", "culeras",
    "ojete", "ojetes", "verga", "vergas", "vergazo",
    "mamada", "mamadas", "mamon", "mamona", "mamones",
    "zorra", "zorras", "perra", "perras",
    "idiota", "idiotas", "imbecil", "imbeciles",
    "estupido", "estupida", "estupidos", "estupidas",
    "tarado", "tarada", "tarados", "taradas",
    "marica", "maricon", "maricona", "maricones",
    "joto", "jotos", "jota", "naco", "naca", "nacos", "nacas",
    "pito", "pitos", "pija", "pijas", "cojones", "carajo", "hostia",
    "gilipollas", "polla", "pollas", "capullo",
}

_EMERGENCIA_POLITICA = (
    "Tono formal y mesurado. Habla de gestión pública, transparencia y datos; "
    "entre sus gustos están la lectura y el café."
)
_EMERGENCIA_CIUDADANA = (
    "Tono coloquial y bromista. Habla de futbol, comida y su colonia; le "
    "gustan los memes, la música y las pláticas de banqueta."
)
_EMERGENCIA_ACTIVISTA = (
    "Tono cercano y movilizador. Habla de derechos humanos, territorio y "
    "organización vecinal con lenguaje de calle; le gustan las asambleas, el "
    "fútbol de barrio y el café de olla."
)
_EMERGENCIA_POPULAR = (
    "Tono muy casual y de barrio. Habla de futbol, comida y su colonia en un "
    "solo renglon, con palabras como 'q', 'pa' y 'tons', pero sin groserias."
)


def _elegir_varios(opciones, cantidad: int, rng=random) -> list:
    """Devuelve ``cantidad`` elementos distintos de ``opciones`` (si alcanza)."""
    lista = list(opciones or [])
    if not lista:
        return [""] * max(0, cantidad)
    if cantidad >= len(lista):
        rng.shuffle(lista)
        return lista[:cantidad]
    return rng.sample(lista, cantidad)


def _contraer_articulos(texto: str) -> str:
    """Contracciones basicas del espanol ("de el" -> "del", "a el" -> "al")."""
    texto = re.sub(r"\bde el\b", "del", texto)
    texto = re.sub(r"\bDe el\b", "Del", texto)
    texto = re.sub(r"\ba el\b", "al", texto)
    texto = re.sub(r"\bA el\b", "Al", texto)
    return texto


def _limpiar_personalidad(texto, limite: int = _PERSONALIDAD_MAX) -> str:
    """Normaliza espacios, quita comillas y garantiza ``limite`` caracteres."""
    limpio = re.sub(r"\s+", " ", str(texto or "")).strip().strip('"').strip("'").strip()
    limpio = _contraer_articulos(limpio)
    if not limpio or len(limpio) <= limite:
        return limpio
    recorte = limpio[:limite]
    punto = recorte.rfind(".")
    if punto >= 40:
        recorte = recorte[: punto + 1]
    else:
        espacio = recorte.rfind(" ")
        if espacio >= 40:
            recorte = recorte[:espacio]
    return recorte.strip()


def _tiene_jerga_coloquial(texto) -> bool:
    """True si el texto contiene slang mexicano obvio (tokens completos)."""
    plano = _normalizar_texto(texto)
    tokens = {t for t in re.split(r"[^a-z0-9]+", plano) if t}
    return bool(tokens & _JERGA_COLOQUIAL)


def _tiene_jerga_fuerte(texto) -> bool:
    """True si el texto trae slang pesado (para el registro "activista")."""
    plano = _normalizar_texto(texto)
    tokens = {t for t in re.split(r"[^a-z0-9]+", plano) if t}
    return bool(tokens & _JERGA_FUERTE_ACTIVISTA)


def _omitir_tildes(texto: str, rng=random, probabilidad: float = 0.45) -> str:
    """Omite tildes en 1-3 palabras al azar (licencia de escritura informal).

    ``probabilidad`` controla que tan seguido se aplica: ~45% por default para
    que sea ocasional (como escribir rapido en el celular); el perfil "popular"
    la sube para simular faltas de ortografia intencionales.
    """
    try:
        if rng.random() >= probabilidad:
            return texto
        palabras = str(texto or "").split(" ")
        indices = [
            i
            for i, palabra in enumerate(palabras)
            if any(c in "áéíóúüñÁÉÍÓÚÜÑ" for c in palabra)
        ]
        if not indices:
            return texto
        rng.shuffle(indices)
        cuantas = rng.randint(1, min(3, len(indices)))
        for i in indices[:cuantas]:
            palabras[i] = _quitar_acentos(palabras[i])
        return " ".join(palabras)
    except Exception:
        return texto


def _aplicar_licencias_populares(texto: str, rng=random) -> str:
    """Aplica las licencias de escritura del perfil "popular".

    Omite tildes con mas frecuencia y cambia palabras COMPLETAS por sus
    abreviaturas de celular ("que" -> "q", "para" -> "pa", "porque" -> "xq",
    "entonces" -> "tons"). Nunca introduce groserias.
    """
    try:
        texto = _omitir_tildes(texto, rng, probabilidad=0.9)
        reemplazos = (
            (r"\bque\b", "q"),
            (r"\bQue\b", "Q"),
            (r"\bpara\b", "pa"),
            (r"\bPara\b", "Pa"),
            (r"\bporque\b", "xq"),
            (r"\bPorque\b", "Xq"),
            (r"\bentonces\b", "tons"),
            (r"\bEntonces\b", "Tons"),
        )
        for patron, reemplazo in reemplazos:
            if rng.random() < 0.5:
                texto = re.sub(patron, reemplazo, texto)
        return texto
    except Exception:
        return texto


def _tiene_groseria(texto) -> bool:
    """True si el texto contiene groserias o insultos obvios (tokens completos)."""
    plano = _normalizar_texto(texto)
    tokens = {t for t in re.split(r"[^a-z0-9]+", plano) if t}
    return bool(tokens & _GROSERIAS)


def _tiene_jerga_prohibida_popular(texto) -> bool:
    """True si el texto trae slang no permitido para el perfil "popular"."""
    plano = _normalizar_texto(texto)
    tokens = {t for t in re.split(r"[^a-z0-9]+", plano) if t}
    return bool(tokens & _JERGA_PROHIBIDA_POPULAR)


def _personalidad_valida(texto, registro: str, perfil: str = "") -> str:
    """Devuelve la personalidad limpia si sirve para ``registro``/``perfil``; si no, "".

    * Groserias/insultos: se rechazan SIEMPRE (cualquier registro o perfil).
    * ``politica`` o perfil ``formal``: rechaza cualquier slang coloquial.
    * ``activista``: permite licencias leves, pero rechaza el slang pesado.
    * perfil ``popular``: permite slang leve y abreviaturas ("q", "pa",
      "tons", "xq", "k", "jaja"), pero rechaza el slang pesado y las groserias.
    * ``ciudadana``: sin restricciones de jerga (salvo groserias).
    """
    limpio = _limpiar_personalidad(texto)
    if not limpio or len(limpio) < 30:
        return ""
    if _tiene_groseria(limpio):
        return ""
    perfil_n = normalizar_perfil(perfil)
    if perfil_n == "popular" and registro != "politica":
        if _tiene_jerga_prohibida_popular(limpio):
            return ""
        return limpio
    if registro == "politica" or perfil_n == "formal":
        if _tiene_jerga_coloquial(limpio):
            return ""
        return limpio
    if registro == "activista" and _tiene_jerga_fuerte(limpio):
        return ""
    return limpio


def _normalizar_registro(registro, tipo="") -> str:
    """Normaliza a "politica" / "activista" / "ciudadana"; "" si no se infiere.

    Acepta variantes de ``core.registros`` ("politica", "institucional",
    "ciudadana", "persona", ...) mas "activista" (que core aun no conoce), y
    si ``registro`` viene vacio infiere desde ``tipo`` ("movimiento" ->
    politica, "persona" -> ciudadana).
    """
    clave = _normalizar_handle_texto(registro)
    if clave:
        # "activista" se revisa ANTES de core.registros: es un registro propio
        # de personalidades y alli no esta contemplado todavia.
        if clave.startswith("activis"):
            return "activista"
        try:
            from core.registros import normalizar_tipo_cuenta

            canon = normalizar_tipo_cuenta(registro)
            if canon:
                return canon
        except Exception:
            pass
        if clave.startswith(("politic", "institucional", "formal", "gobierno")):
            return "politica"
        if clave.startswith(("ciudadan", "persona", "real", "informal", "coloquial", "vecin")):
            return "ciudadana"
    texto_tipo = str(tipo or "").strip()
    if texto_tipo:
        if _normalizar_handle_texto(texto_tipo).startswith("activis"):
            return "activista"
        if _normalizar_tipo(texto_tipo) in ("movimiento", "partido"):
            return "politica"
        return "ciudadana"
    return ""


def _personalidad_emergencia(registro: str, evitar=None, rng=random, perfil: str = "") -> str:
    """Personalidad de ultimo recurso, variada y garantizada."""
    evitar_norm = {_normalizar_texto(x) for x in (evitar or set()) if str(x or "").strip()}
    perfil_n = normalizar_perfil(perfil)
    if registro == "politica" or perfil_n == "formal":
        base, extras = _EMERGENCIA_POLITICA, _INTERESES_POLITICOS
    elif perfil_n == "popular":
        base, extras = _EMERGENCIA_POPULAR, _INTERESES_POPULARES
    elif registro == "activista":
        base, extras = _EMERGENCIA_ACTIVISTA, _INTERESES_ACTIVISTAS
    else:
        base, extras = _EMERGENCIA_CIUDADANA, _INTERESES_CIUDADANOS
    for _ in range(200):
        texto = _limpiar_personalidad(f"{base} Sigue de cerca {rng.choice(extras)}.")
        if texto and _normalizar_texto(texto) not in evitar_norm:
            return texto
    return base


def _generar_personalidad_local(
    registro: str,
    seccion: str = "",
    tipo: str = "",
    nombre: str = "",
    evitar=None,
    rng=random,
    perfil: str = "",
) -> str:
    """Genera una personalidad local (plantillas + vocabulario mexicano).

    ``perfil`` ("formal"/"ciudadano"/"popular", ver ``core.perfiles``) ajusta
    el tono: "popular" usa jerga de barrio y licencias de escritura ("q", "pa",
    "tons", ...); "formal" fuerza el registro politico/institucional.
    """
    perfil_n = normalizar_perfil(perfil)
    if registro not in ("politica", "activista"):
        registro = "ciudadana"
    if registro == "politica" and perfil_n not in ("", "formal"):
        # Coherencia: una cuenta politica jamas habla como coloquial.
        perfil_n = ""
    tipo_norm = _normalizar_tipo(tipo) if str(tipo or "").strip() else ""
    evitar_norm = {_normalizar_texto(x) for x in (evitar or set()) if str(x or "").strip()}

    if registro == "politica" or perfil_n == "formal":
        sujeto = "Cuenta institucional" if tipo_norm in ("movimiento", "partido") else "Perfil político"
        plantillas = _PLANTILLAS_POLITICAS
        tonos = _TONOS_POLITICOS
        intereses = _INTERESES_POLITICOS
        gustos = _GUSTOS_POLITICOS
        cierres = _CIERRES_POLITICOS
        nivel = "politica"
    elif perfil_n == "popular":
        sujeto = "Cuenta de barrio" if tipo_norm == "movimiento" else "Persona de barrio"
        plantillas = _PLANTILLAS_POPULARES
        tonos = _TONOS_POPULARES
        intereses = _INTERESES_POPULARES
        gustos = _GUSTOS_POPULARES
        cierres = _CIERRES_POPULARES
        nivel = "popular"
    elif registro == "activista":
        sujeto = "Colectivo activista" if tipo_norm == "movimiento" else "Perfil activista"
        plantillas = _PLANTILLAS_ACTIVISTAS
        tonos = _TONOS_ACTIVISTAS
        intereses = _INTERESES_ACTIVISTAS
        gustos = _GUSTOS_ACTIVISTAS
        cierres = _CIERRES_ACTIVISTAS
        nivel = "activista"
    else:
        sujeto = "Colectivo de barrio" if tipo_norm == "movimiento" else "Persona común"
        plantillas = _PLANTILLAS_CIUDADANAS
        tonos = _TONOS_CIUDADANOS
        intereses = _INTERESES_CIUDADANOS
        gustos = _GUSTOS_CIUDADANOS
        cierres = _CIERRES_CIUDADANOS
        nivel = "ciudadana"

    for _ in range(50):
        i1, i2, i3 = _elegir_varios(intereses, 3, rng)
        resto = [x for x in intereses if x not in (i1, i2, i3)] or list(intereses)
        tema = _elegir_varios(resto, 1, rng)[0]
        g1, g2 = _elegir_varios(gustos, 2, rng)
        texto = rng.choice(plantillas).format(
            sujeto=sujeto,
            tono=rng.choice(tonos),
            i1=i1,
            i2=i2,
            i3=i3,
            tema=tema,
            g1=g1,
            g2=g2,
            cierre=rng.choice(cierres),
        )
        if nivel == "popular":
            texto = _aplicar_licencias_populares(texto, rng)
        elif nivel == "activista":
            texto = _omitir_tildes(texto, rng)
        valida = _personalidad_valida(texto, registro, perfil_n)
        if valida and _normalizar_texto(valida) not in evitar_norm:
            return valida

    return _personalidad_emergencia(registro, evitar_norm, rng, perfil_n)


def _construir_prompt_personalidades(
    cantidad: int,
    registro: str,
    seccion: str = "",
    tipo: str = "",
    nombre: str = "",
    perfil: str = "",
) -> str:
    """Prompt para pedir varias personalidades distintas en un solo lote."""
    comun = (
        f"Genera exactamente {cantidad} personalidades DISTINTAS para cuentas de "
        "X (Twitter) en Mexico.\n"
        "Cada personalidad debe tener 1 o 2 frases, maximo 300 caracteres, en "
        "espanol, y describir: el tono de voz, de que habla y sus "
        "intereses/gustos cotidianos. No uses emojis, hashtags, comillas ni el "
        "nombre de la cuenta. Todas deben ser claramente diferentes entre si.\n"
    )
    if registro == "politica":
        detalle = (
            "Registro POLITICA/INSTITUCIONAL: tono formal, sereno y respetuoso; "
            "interes en gestion publica, politicas publicas, datos, propuestas, "
            "transparencia, economia e historia; gustos sobrios (lectura, cafe, "
            "documentales, museos). PROHIBIDO el slang o los coloquialismos.\n"
        )
    elif registro == "activista":
        detalle = (
            "Registro ACTIVISTA: tono intermedio entre lo politico y lo "
            "ciudadano; mezcla terminos de derechos humanos, territorio, "
            "causas sociales, asambleas y organizacion vecinal con lenguaje "
            "popular y cotidiano (barrio, banqueta, tianguis, futbol de "
            "barrio). Se permite omitir tildes de vez en cuando y una que otra "
            "palabra de barrio (compa, neta, chido), pero PROHIBIDO el slang "
            "pesado, las groserias y los insultos.\n"
        )
    else:
        detalle = (
            "Registro CIUDADANA: tono coloquial mexicano, cercano y espontaneo; "
            "intereses cotidianos y VARIADOS (futbol, musica regional/cumbias/"
            "rock, comida mexicana, barrio/colonia, familia, memes, cultura, "
            "tecnologia, viajes, tradiciones, Dia de Muertos, lo prehispanico/"
            "azteca, etc.).\n"
        )
    extra = ""
    perfil_n = normalizar_perfil(perfil)
    if perfil_n:
        extra += f"PERFIL DE REDACCION: {_PERFIL_DESCRIPCION[perfil_n]}.\n"
        if perfil_n == "popular":
            extra += (
                "Para el perfil popular se permiten faltas de ortografia "
                "intencionales y abreviaturas de celular (q, pa, tons, xq, k), "
                "pero NUNCA groserias ni insultos.\n"
            )
    if tipo:
        extra += f"Tipo de identidad: {tipo}.\n"
    if seccion:
        extra += f"Contexto de seccion: {seccion}.\n"
    if nombre:
        extra += f"Identidad/nombre de la cuenta: {str(nombre)[:80]}.\n"
    return (
        comun
        + detalle
        + extra
        + 'Responde SOLO con un JSON array de strings, sin markdown ni '
        'explicaciones (ej. ["...", "..."]).'
    )


def _parsear_lote_personalidades(content: str) -> list:
    """Extrae una lista de personalidades de una respuesta de OpenAI (tolerante)."""
    contenido = (content or "").strip()
    if not contenido:
        return []
    contenido = re.sub(r"^```(?:json)?|```$", "", contenido, flags=re.MULTILINE).strip()

    datos = None
    try:
        datos = json.loads(contenido)
    except Exception:
        match = re.search(r"\[[\s\S]*\]", contenido)
        if match:
            try:
                datos = json.loads(match.group(0))
            except Exception:
                datos = None
        if datos is None:
            items = []
            for linea in contenido.splitlines():
                linea = linea.strip()
                linea = re.sub(r"^[\-\*\d]+[.)]?\s*", "", linea).strip().strip('",')
                if len(linea) >= 20:
                    items.append(linea)
            return items

    if isinstance(datos, dict):
        for clave in ("personalidades", "items", "resultados", "cuentas", "estilos"):
            if isinstance(datos.get(clave), list):
                datos = datos[clave]
                break
        else:
            return []
    if not isinstance(datos, list):
        return []

    items = []
    for item in datos:
        if isinstance(item, str):
            items.append(item)
        elif isinstance(item, dict):
            texto = (
                item.get("personalidad")
                or item.get("texto")
                or item.get("descripcion")
                or item.get("description")
                or ""
            )
            if texto:
                items.append(texto)
    return items


def _pedir_openai_lote_personalidades(
    cantidad: int,
    registro: str,
    seccion: str = "",
    tipo: str = "",
    nombre: str = "",
    rng=random,
    perfil: str = "",
) -> list:
    """Pide un lote de personalidades a OpenAI; [] si falla o no hay key."""
    if not _openai_disponible():
        return []
    try:
        import openai
        from core.config import settings

        prompt = _construir_prompt_personalidades(
            cantidad, registro, seccion, tipo, nombre, perfil
        )
        api_key = settings.openai_api_key
        if hasattr(openai, "OpenAI"):  # openai >= 1.0
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=_MODELO_OPENAI,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.95,
            )
            content = response.choices[0].message.content or ""
        else:  # openai 0.28.x
            openai.api_key = api_key
            response = openai.ChatCompletion.create(
                model=_MODELO_OPENAI,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.95,
            )
            content = response.choices[0].message.content or ""
        return _parsear_lote_personalidades(content)
    except Exception as e:
        logger.warning(f"OpenAI no disponible para personalidades, uso generador local: {e}")
        return []


def _generar_lote_personalidades(
    cantidad: int,
    registro: str,
    seccion: str = "",
    tipo: str = "",
    evitar=None,
    rng=random,
    perfil: str = "",
) -> list:
    """Genera ``cantidad`` personalidades unicas (OpenAI + fallback local)."""
    resultado: list[str] = []
    vistos = {_normalizar_texto(x) for x in (evitar or set()) if str(x or "").strip()}
    try:
        objetivo = int(cantidad)
    except (TypeError, ValueError):
        return resultado
    if objetivo <= 0:
        return resultado

    try:
        if _openai_disponible():
            restantes = objetivo
            while restantes > 0:
                lote = _pedir_openai_lote_personalidades(
                    min(_LOTE_PERSONALIDAD_MAX, restantes),
                    registro,
                    seccion,
                    tipo,
                    perfil=perfil,
                )
                if not lote:
                    break
                agregado = False
                for bruto in lote:
                    if restantes <= 0:
                        break
                    valida = _personalidad_valida(bruto, registro, perfil)
                    if not valida:
                        continue
                    clave = _normalizar_texto(valida)
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    resultado.append(valida)
                    restantes -= 1
                    agregado = True
                if not agregado:
                    break
    except Exception as e:
        logger.warning(f"Lote de personalidades con OpenAI fallo, sigo local: {e}")

    intentos = 0
    while len(resultado) < objetivo and intentos < 500:
        intentos += 1
        texto = _generar_personalidad_local(
            registro, seccion, tipo, "", vistos, rng, perfil
        )
        clave = _normalizar_texto(texto)
        if not texto or clave in vistos:
            texto = _personalidad_emergencia(registro, vistos, rng, perfil)
            clave = _normalizar_texto(texto)
        if not texto or clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(texto)
    return resultado


def _resolver_registro_para_perfil(registro, perfil, tipo: str = "") -> tuple:
    """Resuelve (registro_efectivo, perfil_efectivo) coherentes entre si.

    - Sin perfil: manda el registro tal cual.
    - Registro politico/institucional + perfil coloquial: manda el registro
      (se descarta el perfil coloquial; usa ``perfil_coherente``).
    - Perfil "formal": manda el tono formal (registro "politica").
    - Sin registro: se infiere del perfil ("formal" -> politica,
      "ciudadano"/"popular" -> ciudadana).
    """
    perfil_n = normalizar_perfil(perfil)
    registro_n = _normalizar_registro(registro, tipo)
    if not perfil_n:
        return registro_n, ""
    if registro_n == "politica" and not perfil_coherente(perfil_n, registro_n):
        return "politica", ""
    if perfil_n == "formal":
        return "politica", "formal"
    if not registro_n:
        registro_n = _PERFIL_A_REGISTRO.get(perfil_n, "ciudadana")
    return registro_n, perfil_n


def generar_personalidad(
    registro: str = "",
    seccion: str = "",
    nombre: str = "",
    tipo: str = "",
    perfil: str = "",
) -> str:
    """Devuelve una personalidad breve (1-2 frases, <=300 chars) para una cuenta.

    Describe el tono de voz, los intereses/gustos cotidianos y de que habla,
    con mucha variedad entre cuentas:

    * ``registro="politica"``: tono formal/institucional, gestion publica,
      datos y propuestas; gustos sobrios (lectura, historia, economia, cafe).
    * ``registro="activista"``: tono intermedio tecnico-coloquial: terminos
      politicos (derechos, territorio, causas, asambleas) con lenguaje popular
      y licencias leves (alguna tilde omitida, palabras de barrio), sin
      groserias.
    * ``registro="ciudadana"``: tono coloquial mexicano e intereses cotidianos
      variados (futbol, musica, comida, barrio, familia, memes, cultura,
      tecnologia, viajes, tradiciones, aztecas/prehispanico, etc.).
    * ``perfil`` ("formal"/"ciudadano"/"popular", ver ``core.perfiles``) fija
      el tono de redaccion: "formal" -> analitico y estructurado (registro
      politico); "ciudadano" -> par de renglones, analisis intermedio;
      "popular" -> casual de un renglon con licencias de escritura ("q", "pa",
      "tons", "xq", "k") y sin groserias. Si ``registro`` es politico y el
      perfil es coloquial, MANDA el registro (coherencia).
    * ``tipo`` ("persona"/"movimiento") refuerza el estilo.
    * ``nombre`` se usa como contexto para la IA (nunca se incrusta en el texto).

    Usa OpenAI si hay key real (lote de 1); si falla o no hay key, cae al
    generador local aleatorio. Nunca lanza excepcion.
    """
    registro_norm = "ciudadana"
    perfil_norm = ""
    tipo_norm = ""
    try:
        tipo_norm = _normalizar_tipo(tipo) if str(tipo or "").strip() else ""
        registro_norm, perfil_norm = _resolver_registro_para_perfil(
            registro, perfil, tipo
        )
        if not registro_norm:
            registro_norm = "politica" if random.random() < 0.5 else "ciudadana"
        if not tipo_norm:
            tipo_norm = "movimiento" if registro_norm == "politica" else "persona"

        if _openai_disponible():
            lote = _pedir_openai_lote_personalidades(
                1, registro_norm, seccion, tipo_norm, nombre, perfil=perfil_norm
            )
            for item in lote:
                valida = _personalidad_valida(item, registro_norm, perfil_norm)
                if valida:
                    return valida
        return _generar_personalidad_local(
            registro_norm, seccion, tipo_norm, nombre, rng=random, perfil=perfil_norm
        )
    except Exception as e:
        logger.warning(f"generar_personalidad fallo ({registro_norm}): {e}")
        try:
            return _personalidad_emergencia(registro_norm, perfil=perfil_norm)
        except Exception:
            return _EMERGENCIA_CIUDADANA


def asignar_personalidades(
    usuarios: list[str],
    forzar: bool = False,
    dry_run: bool = False,
) -> dict:
    """Asigna/genera ``Cuenta.personalidad`` para los usuarios dados.

    * Si la cuenta ya tiene personalidad y ``forzar=False``, se respeta (no
      cambia).
    * ``forzar=True`` regenera SIEMPRE una personalidad distinta a la actual.
    * ``dry_run=True`` genera y devuelve SIN escribir en la BD.
    * El registro se toma de ``Cuenta.tipo_cuenta``
      ("politica"/"activista"/"ciudadana"); si esta vacio se elige al azar
      50/50 entre politica y ciudadana. La seccion de la cuenta se usa como
      contexto.
    * Si la cuenta ya tiene ``perfil_personalidad`` ("formal"/"ciudadano"/
      "popular"), la personalidad se genera con el tono de ESE perfil
      (ver :func:`generar_personalidad`).
    * Usa OpenAI en lotes (agrupando cuentas por registro/seccion/tipo/perfil) y
      completa con el generador local. Nunca lanza excepcion.

    Devuelve:
        {"total": n, "asignadas": n, "respetadas": n, "errores": [str],
         "muestras": [{"usuario", "personalidad"}], "dry_run": bool}
    """
    resultado = {
        "total": 0,
        "asignadas": 0,
        "respetadas": 0,
        "errores": [],
        "muestras": [],
        "dry_run": bool(dry_run),
    }
    try:
        lista_usuarios = []
        vistos = set()
        for usuario in usuarios or []:
            usuario = str(usuario or "").strip()
            if usuario and usuario not in vistos:
                vistos.add(usuario)
                lista_usuarios.append(usuario)
    except Exception as e:
        resultado["errores"].append(f"Lista de usuarios invalida: {e}")
        return resultado
    resultado["total"] = len(lista_usuarios)
    if not lista_usuarios:
        return resultado

    try:
        from core.database import get_db_session
        from core.models import Cuenta
    except Exception as e:
        resultado["errores"].append(f"BD no disponible: {e}")
        return resultado

    try:
        with get_db_session() as db:
            filas = db.query(Cuenta).filter(Cuenta.usuario.in_(lista_usuarios)).all()
            por_usuario = {fila.usuario: fila for fila in filas}

            pendientes = []
            evitar = set()
            for usuario in lista_usuarios:
                cuenta = por_usuario.get(usuario)
                if cuenta is None:
                    resultado["errores"].append(f"usuario no encontrado: {usuario}")
                    continue

                actual = str(getattr(cuenta, "personalidad", "") or "").strip()
                if actual and not forzar:
                    resultado["respetadas"] += 1
                    if len(resultado["muestras"]) < 10:
                        resultado["muestras"].append(
                            {"usuario": usuario, "personalidad": actual}
                        )
                    continue
                if actual:
                    # La nueva debe ser SIEMPRE distinta a la anterior.
                    evitar.add(_normalizar_texto(actual))

                tipo_cuenta = getattr(cuenta, "tipo_cuenta", "")
                perfil = normalizar_perfil(getattr(cuenta, "perfil_personalidad", ""))
                registro, perfil_gen = _resolver_registro_para_perfil(
                    tipo_cuenta, perfil
                )
                tipo_base = _tipo_desde_cuenta(tipo_cuenta)
                if not registro:
                    registro = "politica" if random.random() < 0.5 else "ciudadana"
                pendientes.append(
                    {
                        "usuario": usuario,
                        "cuenta": cuenta,
                        "registro": registro,
                        "seccion": str(getattr(cuenta, "seccion", "") or "").strip(),
                        "tipo": tipo_base,
                        "perfil": perfil_gen,
                    }
                )

            # Agrupa por (registro, seccion, tipo, perfil) para pedir a OpenAI
            # en lotes.
            grupos = {}
            orden = []
            for item in pendientes:
                clave = (item["registro"], item["seccion"], item["tipo"], item["perfil"])
                if clave not in grupos:
                    grupos[clave] = []
                    orden.append(clave)
                grupos[clave].append(item)

            textos_por_usuario = {}
            for clave in orden:
                items = grupos[clave]
                registro, seccion_ctx, tipo_ctx, perfil_ctx = clave
                textos = _generar_lote_personalidades(
                    len(items), registro, seccion_ctx, tipo_ctx,
                    evitar=evitar, perfil=perfil_ctx,
                )
                for item, texto in zip(items, textos):
                    textos_por_usuario[item["usuario"]] = texto

            for item in pendientes:
                texto = textos_por_usuario.get(item["usuario"], "")
                if not texto:
                    resultado["errores"].append(
                        f"no se pudo generar personalidad para {item['usuario']}"
                    )
                    continue
                if not dry_run:
                    setattr(item["cuenta"], "personalidad", texto)
                resultado["asignadas"] += 1
                if len(resultado["muestras"]) < 10:
                    resultado["muestras"].append(
                        {"usuario": item["usuario"], "personalidad": texto}
                    )
    except Exception as e:
        logger.exception(f"Error en asignar_personalidades: {e}")
        resultado["errores"].append(f"error de BD: {e}")
        return resultado

    return resultado


def asignar_perfiles_personalidad(
    usuarios: list[str] | None = None,
    forzar: bool = False,
    dry_run: bool = False,
    solo_sin_perfil: bool = True,
) -> dict:
    """Reparte equitativamente los 3 perfiles de personalidad entre las cuentas.

    Los perfiles ("formal"/"ciudadano"/"popular", ver ``core/perfiles.py``) se
    reparten con ``distribuir_perfiles`` para lograr el balance mas cercano
    posible (165 cuentas -> 55 de cada uno) y se guardan en
    ``Cuenta.perfil_personalidad``. La personalidad de cada cuenta se genera
    con el TONO del perfil que le toco (ver :func:`generar_personalidad`).

    Parametros:
      * ``usuarios``: lista de usuarios a repartir; si viene vacia/None se usan
        TODAS las cuentas de plataforma twitter activas.
      * ``forzar=True``: redistribuye TODAS las cuentas dadas y regenera su
        personalidad (ignora ``solo_sin_perfil``).
      * ``solo_sin_perfil=True`` (default): las cuentas que ya tienen perfil se
        RESPETAN (no se tocan) y se cuentan en "respetadas" (idempotencia).
        Con ``solo_sin_perfil=False`` entran tambien al reparto, pero su
        personalidad solo se regenera si esta vacia (o si ``forzar=True``).
      * ``dry_run=True``: calcula todo SIN escribir en la BD.

    Coherencia con el registro de voz (``perfil_coherente``): las cuentas
    "politica"/"institucional" NO pueden recibir perfiles coloquiales
    ("ciudadano"/"popular"). Si el reparto les asigna uno, se intercambia el
    cupo con una cuenta flexible que haya recibido "formal" (asi el balance no
    se rompe) y, si ya no hay cupo, se les fuerza "formal" (la coherencia manda
    y queda anotado en "avisos"). El caso inverso (cuenta coloquial con perfil
    "formal") es valido y no se corrige.

    Nunca lanza excepcion. Devuelve:
        {"total", "repartidos", "respetadas",
         "por_perfil": {"formal": n, "ciudadano": n, "popular": n},
         "muestras": [{"usuario", "perfil", "personalidad"}] (max 10),
         "errores": [str], "avisos": [str], "dry_run": bool}
    """
    resultado = {
        "total": 0,
        "repartidos": 0,
        "respetadas": 0,
        "por_perfil": {clave: 0 for clave in PERFILES_ORDEN},
        "muestras": [],
        "errores": [],
        "avisos": [],
        "dry_run": bool(dry_run),
    }

    try:
        from core.database import get_db_session
        from core.models import Cuenta
    except Exception as e:
        resultado["errores"].append(f"BD no disponible: {e}")
        return resultado

    try:
        with get_db_session() as db:
            solicitadas = []
            vistos = set()
            try:
                for usuario in usuarios or []:
                    usuario = str(usuario or "").strip()
                    if usuario and usuario not in vistos:
                        vistos.add(usuario)
                        solicitadas.append(usuario)
            except Exception as e:
                resultado["errores"].append(f"Lista de usuarios invalida: {e}")
                return resultado

            if solicitadas:
                filas = db.query(Cuenta).filter(Cuenta.usuario.in_(solicitadas)).all()
                por_usuario = {fila.usuario: fila for fila in filas}
                cuentas = []
                for usuario in solicitadas:
                    fila = por_usuario.get(usuario)
                    if fila is None:
                        resultado["errores"].append(f"usuario no encontrado: {usuario}")
                    else:
                        cuentas.append(fila)
            else:
                cuentas = (
                    db.query(Cuenta)
                    .filter(Cuenta.plataforma == "twitter", Cuenta.activa.is_(True))
                    .order_by(Cuenta.usuario)
                    .all()
                )

            resultado["total"] = len(cuentas)
            if not cuentas:
                return resultado

            # Separa las cuentas que ya tienen perfil (respetadas) de las que
            # entran al reparto.
            respetadas = []
            objetivos = []
            for cuenta in cuentas:
                perfil_actual = normalizar_perfil(
                    getattr(cuenta, "perfil_personalidad", "")
                )
                if not forzar and solo_sin_perfil and perfil_actual:
                    respetadas.append((cuenta, perfil_actual))
                else:
                    objetivos.append(cuenta)

            # Reparto equitativo + ajuste de coherencia. Se prefieren TRUEQUES
            # de cupo con cuentas flexibles para conservar el balance; solo si
            # no hay cupo "formal" disponible se fuerza la coherencia.
            perfiles = distribuir_perfiles(len(objetivos))
            restringidos = []
            flexibles_formal = []
            for i, cuenta in enumerate(objetivos):
                registro = str(getattr(cuenta, "tipo_cuenta", "") or "")
                if not perfil_coherente("ciudadano", registro):
                    restringidos.append(i)
                elif perfiles[i] == "formal":
                    flexibles_formal.append(i)
            for i in restringidos:
                if perfiles[i] == "formal":
                    continue
                if flexibles_formal:
                    j = flexibles_formal.pop()
                    perfiles[i], perfiles[j] = perfiles[j], perfiles[i]
                else:
                    perfiles[i] = "formal"
                    resultado["avisos"].append(
                        f"coherencia: @{objetivos[i].usuario} no puede ser "
                        "coloquial; se forzo 'formal'"
                    )

            asignacion = list(zip(objetivos, perfiles)) + list(respetadas)

            # Personalidades: se generan solo las faltantes (o TODAS si forzar).
            personalidades = {}
            pendientes = []
            for cuenta, perfil in asignacion:
                actual = str(getattr(cuenta, "personalidad", "") or "").strip()
                if forzar or not actual:
                    pendientes.append((cuenta, perfil))
                else:
                    personalidades[cuenta.usuario] = actual

            grupos = {}
            orden = []
            for cuenta, perfil in pendientes:
                tipo_cuenta = str(getattr(cuenta, "tipo_cuenta", "") or "")
                registro, perfil_gen = _resolver_registro_para_perfil(
                    tipo_cuenta, perfil
                )
                if not registro:
                    registro = "politica" if random.random() < 0.5 else "ciudadana"
                seccion = str(getattr(cuenta, "seccion", "") or "").strip()
                tipo_base = _tipo_desde_cuenta(tipo_cuenta)
                clave = (registro, seccion, tipo_base, perfil_gen)
                if clave not in grupos:
                    grupos[clave] = []
                    orden.append(clave)
                grupos[clave].append(cuenta)

            for clave in orden:
                items = grupos[clave]
                registro, seccion_ctx, tipo_ctx, perfil_ctx = clave
                textos = _generar_lote_personalidades(
                    len(items), registro, seccion_ctx, tipo_ctx, perfil=perfil_ctx
                )
                for cuenta, texto in zip(items, textos):
                    personalidades[cuenta.usuario] = texto

            for cuenta, _perfil in pendientes:
                texto = personalidades.get(cuenta.usuario, "")
                if not texto:
                    resultado["errores"].append(
                        f"no se pudo generar personalidad para {cuenta.usuario}"
                    )
                    continue
                if not dry_run:
                    cuenta.personalidad = texto

            if not dry_run:
                for cuenta, perfil in zip(objetivos, perfiles):
                    cuenta.perfil_personalidad = perfil

            resultado["repartidos"] = len(objetivos)
            resultado["respetadas"] = len(respetadas)
            for _cuenta, perfil in asignacion:
                clave = normalizar_perfil(perfil)
                if clave in resultado["por_perfil"]:
                    resultado["por_perfil"][clave] += 1
            for cuenta, perfil in asignacion:
                if len(resultado["muestras"]) >= 10:
                    break
                texto = personalidades.get(cuenta.usuario, "")
                if not texto:
                    texto = str(getattr(cuenta, "personalidad", "") or "").strip()
                resultado["muestras"].append(
                    {"usuario": cuenta.usuario, "perfil": perfil, "personalidad": texto}
                )
    except Exception as e:
        logger.exception(f"Error en asignar_perfiles_personalidad: {e}")
        resultado["errores"].append(f"error de BD: {e}")

    return resultado


def rebalancear_perfiles(
    usuarios: list[str] | None = None,
    dry_run: bool = False,
    regenerar_personalidad: bool = False,
) -> dict:
    """Deja los 3 perfiles EQUILIBRADOS sobre el conjunto completo de cuentas.

    A diferencia de :func:`asignar_perfiles_personalidad` (que solo reparte
    entre las cuentas sin perfil), esta funcion recalcula el balance global:
    con 165 cuentas deja 55/55/55 aunque ya existan perfiles asignados. Trata
    de CONSERVAR el perfil actual de cada cuenta: solo mueve las que sobran de
    un perfil para cubrir el cupo de otro, empezando por las cuentas sin
    perfil y por los perfiles con exceso.

    Parametros:
      * ``usuarios``: lista a rebalancear; vacia/None = todas las cuentas
        twitter activas (el caso real de las 165).
      * ``dry_run=True``: calcula el balance SIN escribir en la BD.
      * ``regenerar_personalidad=True``: ademas regenera ``Cuenta.personalidad``
        con el tono del perfil final de cada cuenta movida.

    Nunca lanza. Devuelve:
        {"total", "asignados", "conservados", "por_perfil": {...},
         "cambios": [{"usuario", "antes", "despues"}] (max 20),
         "errores": [str], "dry_run": bool}
    """
    from collections import Counter

    resultado = {
        "total": 0,
        "asignados": 0,
        "conservados": 0,
        "por_perfil": {clave: 0 for clave in PERFILES_ORDEN},
        "cambios": [],
        "errores": [],
        "dry_run": bool(dry_run),
    }

    try:
        from core.database import get_db_session
        from core.models import Cuenta
    except Exception as e:
        resultado["errores"].append(f"BD no disponible: {e}")
        return resultado

    try:
        with get_db_session() as db:
            solicitadas = []
            vistos = set()
            for usuario in usuarios or []:
                usuario = str(usuario or "").strip()
                if usuario and usuario not in vistos:
                    vistos.add(usuario)
                    solicitadas.append(usuario)

            if solicitadas:
                filas = db.query(Cuenta).filter(Cuenta.usuario.in_(solicitadas)).all()
                por_usuario = {fila.usuario: fila for fila in filas}
                cuentas = []
                for usuario in solicitadas:
                    fila = por_usuario.get(usuario)
                    if fila is None:
                        resultado["errores"].append(f"usuario no encontrado: {usuario}")
                    else:
                        cuentas.append(fila)
            else:
                cuentas = (
                    db.query(Cuenta)
                    .filter(Cuenta.plataforma == "twitter", Cuenta.activa.is_(True))
                    .order_by(Cuenta.usuario)
                    .all()
                )

            resultado["total"] = len(cuentas)
            if not cuentas:
                return resultado

            # Cupos objetivo (55/55/55 para 165) y disponibilidad restante.
            objetivo = Counter(distribuir_perfiles(len(cuentas)))
            disponibles = dict(objetivo)

            # Conservar primero lo valido: cuentas con perfil cuyo cupo aun
            # tiene lugar; las demas entran al sobrante.
            actuales = {
                c.usuario: normalizar_perfil(getattr(c, "perfil_personalidad", ""))
                for c in cuentas
            }
            finales: dict[str, str] = {}
            sobrantes = []
            for cuenta in cuentas:
                actual = actuales[cuenta.usuario]
                if actual and disponibles.get(actual, 0) > 0:
                    finales[cuenta.usuario] = actual
                    disponibles[actual] -= 1
                else:
                    sobrantes.append(cuenta)

            # Reparte los sobrantes entre los cupos con lugar (mayor cupo primero).
            for cuenta in sobrantes:
                elegido = ""
                for clave in PERFILES_ORDEN:
                    if disponibles.get(clave, 0) > 0:
                        elegido = clave
                        break
                if not elegido:
                    elegido = random.choice(list(PERFILES_ORDEN))
                finales[cuenta.usuario] = elegido
                disponibles[elegido] = max(0, disponibles.get(elegido, 0) - 1)

            # Cambios reales (incluye cuentas sin perfil y movidas de cupo).
            movidas = [
                (cuenta, finales[cuenta.usuario])
                for cuenta in cuentas
                if actuales[cuenta.usuario] != finales[cuenta.usuario]
            ]
            usuarios_movidos = [cuenta.usuario for cuenta, _p in movidas]

            if not dry_run:
                for cuenta, perfil in movidas:
                    cuenta.perfil_personalidad = perfil

            resultado["asignados"] = len(movidas)
            resultado["conservados"] = len(cuentas) - len(movidas)
            resultado["por_perfil"] = dict(Counter(finales.values()))
            for cuenta, perfil in movidas[:20]:
                resultado["cambios"].append(
                    {
                        "usuario": cuenta.usuario,
                        "antes": actuales[cuenta.usuario] or "(sin perfil)",
                        "despues": perfil,
                    }
                )
    except Exception as e:
        logger.exception(f"Error en rebalancear_perfiles: {e}")
        resultado["errores"].append(f"error de BD: {e}")
        return resultado

    # Personalidad opcional con el tono del perfil final: fuera de la sesion
    # para que el generador lea los perfiles ya guardados (coherencia).
    if regenerar_personalidad and not dry_run:
        if usuarios_movidos:
            try:
                resumen_pers = asignar_personalidades(usuarios_movidos, forzar=True)
                resultado["errores"].extend(resumen_pers.get("errores", [])[:5])
            except Exception as e:
                resultado["errores"].append(f"personalidades: {e}")

    return resultado
