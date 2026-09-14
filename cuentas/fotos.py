# -*- coding: utf-8 -*-
"""Fotos de perfil (avatar) y de portada (banner) para cuentas de X.

Genera imagenes con IA (``ia.generador_imagenes.GeneradorImagenes``) y las
aplica en X con Selenium (``plataformas.twitter.selenium_bot.TwitterBot``,
import perezoso: Chrome solo se carga al aplicar de verdad).

Interfaz congelada:

    generar_foto_perfil(usuario, forzar=False) -> dict
    generar_portada(usuario, forzar=False) -> dict
    generar_fotos(usuarios, con_portada=False, forzar=False, dry_run=False,
                  callback=None) -> dict
    aplicar_foto_perfil(usuario) -> dict
    aplicar_portada(usuario) -> dict
    aplicar_fotos(usuarios, con_portada=False, callback=None) -> dict

Reglas de diseno:

* Rutas absolutas via ``core.config.resolver_ruta``:
  ``data/avatares/<usuario>.png`` (1024x1024) y
  ``data/portadas/<usuario>.png`` (1536x1024).
* El prompt varia por cuenta: usa tipo_cuenta, nombre, personalidad, seccion y
  azar para que ninguna salga igual. Siempre pide SIN texto, SIN letras, SIN
  logos, SIN marcas de agua y SIN simbolos de partidos (las banderas que
  aparezcan son mexicanas/ciudadanas, nunca de un partido ni con siglas).
* ``_estilo_visual`` decide entre retrato de "persona" y escena "movimiento":
  ``politica`` -> movimiento; ``activista``/``ciudadana`` -> persona; con
  tipo_cuenta vacio se infiere por nombre/personalidad (palabras como
  ciudadania, unidos, voces, pueblo, colectivo, coordinadora, etc.).
* Ninguna funcion publica lanza excepcion: los errores se reportan en el dict.
"""
import os
import random
import re
import shutil
import unicodedata

from loguru import logger

__all__ = [
    "generar_foto_perfil",
    "generar_portada",
    "generar_fotos",
    "aplicar_foto_perfil",
    "aplicar_portada",
    "aplicar_fotos",
    "registrar_fotos_manuales",
]

# Campos de Cuenta que se copian a un dict (acceso defensivo con getattr:
# funciona aunque core aun no tenga alguna columna).
_CAMPOS_CUENTA = (
    "usuario",
    "avatar_path",
    "banner_path",
    "tipo_cuenta",
    "personalidad",
    "nombre_mostrado",
    "nombre_propuesto",
    "handle_actual",
    "seccion",
    "sector",
    "pais",
)

# Palabras que sugieren que una cuenta es una organizacion/movimiento, no una
# persona (solo se usan cuando tipo_cuenta esta vacio).
_PALABRAS_ORGANIZACION = {
    "ciudadania", "ciudadanos", "ciudadanas", "ciudad", "ciudades",
    "unidos", "unidas", "voces", "pueblo", "pueblos", "colectivo",
    "colectiva", "colectivos", "movimiento", "movimientos", "frente",
    "alianza", "comunidad", "vecinos", "vecinal", "barrial", "organizacion",
    "coordinadora", "asamblea", "defensores", "agrupacion", "comite",
    "sindicato", "federacion", "observatorio", "plataforma", "institucional",
    "institucion", "nacion", "patria", "territorio", "gente", "causa",
    "causas", "trabajadores", "jovenes", "juventud",
}


# --------------------------------------------------------------------------- #
# Utilidades
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


def _campo(cuenta, nombre: str, default=""):
    """Lee un campo de una Cuenta o de un dict (compatible con tests)."""
    if isinstance(cuenta, dict):
        return cuenta.get(nombre, default)
    return getattr(cuenta, nombre, default)


def _archivo_seguro(nombre) -> str:
    """Nombre de archivo sin caracteres problematicos."""
    limpio = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(nombre or "").strip())
    return limpio.strip("._") or "cuenta"


def _ruta_avatar(usuario: str) -> str:
    from core.config import resolver_ruta

    return resolver_ruta(f"data/avatares/{_archivo_seguro(usuario)}.png")


def _ruta_portada(usuario: str) -> str:
    from core.config import resolver_ruta

    return resolver_ruta(f"data/portadas/{_archivo_seguro(usuario)}.png")


def _nombre_clave(datos: dict) -> str:
    return str(
        datos.get("nombre_mostrado")
        or datos.get("nombre_propuesto")
        or datos.get("usuario")
        or ""
    ).strip()


def _frase_personalidad(datos: dict, limite: int = 140) -> str:
    texto = re.sub(r"\s+", " ", str(datos.get("personalidad") or "")).strip()
    if len(texto) > limite:
        texto = texto[:limite].rstrip(" ,.;")
    return texto


def _notificar(callback, actual, total, usuario, tipo, ok) -> None:
    if not callable(callback):
        return
    try:
        callback(actual, total, usuario, tipo, bool(ok))
    except Exception as e:  # un callback roto no debe tumbar el lote
        logger.debug(f"callback de fotos fallo ({usuario}/{tipo}): {e}")


# --------------------------------------------------------------------------- #
# Heuristica visual
# --------------------------------------------------------------------------- #
def _estilo_visual(cuenta) -> str:
    """Devuelve "persona" (retrato) o "movimiento" (escena simbolica).

    Prioridad:
      1. ``tipo_cuenta``: "politica" -> movimiento; "activista"/"ciudadana"
         -> persona.
      2. Si esta vacio: nombre/personalidad/usuario con palabras de
         organizacion -> movimiento; cualquier otra cosa -> persona.
    """
    tipo = _normalizar_texto(_campo(cuenta, "tipo_cuenta", ""))
    if tipo:
        if tipo.startswith("activis"):
            return "persona"
        if tipo.startswith(("politic", "institucional", "formal", "gobierno")):
            return "movimiento"
        if tipo.startswith(("ciudadan", "persona", "real", "informal", "coloquial")):
            return "persona"

    texto = " ".join(
        str(_campo(cuenta, campo, "") or "")
        for campo in (
            "nombre_mostrado",
            "nombre_propuesto",
            "usuario",
            "personalidad",
        )
    )
    tokens = {t for t in re.split(r"[^a-z0-9]+", _normalizar_texto(texto)) if t}
    if tokens & _PALABRAS_ORGANIZACION:
        return "movimiento"
    return "persona"


# --------------------------------------------------------------------------- #
# Prompts (varian por cuenta y por azar)
# --------------------------------------------------------------------------- #
_GENEROS = ("hombre", "mujer")
_RANGOS_EDAD = ((20, 30), (28, 42), (38, 55))

_CONTEXTOS_PERSONA = [
    "empleado de una tienda de abarrotes",
    "comerciante del mercado",
    "chofer de transporte publico",
    "maestra de primaria",
    "enfermera de clinica publica",
    "mecanico de taller",
    "cocinera de fonda",
    "estudiante universitario",
    "ingeniero de obra civil",
    "vendedor de tianguis",
    "panadero de barrio",
    "contadora de oficina",
    "fotografo aficionado",
    "costurera de colonia",
    "mesero de cafeteria",
    "tecnico de computadoras",
]

_ROPA_PERSONA = [
    "playera sencilla y jeans",
    "camisa de cuadros arremangada",
    "sudadera con capucha",
    "blusa floreada",
    "chamarra de mezclilla",
    "camiseta deportiva",
    "camisa formal sin corbata",
    "sueter tejido",
]

_FONDOS_PERSONA = [
    "sala de una casa humilde con fotos familiares",
    "cocina mexicana con trastes de barro",
    "banqueta de su colonia con locales al fondo",
    "parque de barrio con arboles",
    "cafeteria de la esquina",
    "mercado local colorido",
    "patio con macetas y ropa tendida",
    "frente a un mural de la colonia",
]

_FONDOS_ACTIVISTA = [
    "calle de su colonia con puestos y gente caminando",
    "banqueta frente a un mural comunitario",
    "explanada de una plaza publica de barrio",
    "mercado local a media manana",
    "cancha de futbol de barrio con jovenes al fondo",
    "parada del camion con vecinos esperando",
]

_LUCES_HORA = [
    "luz natural de media manana",
    "luz calida de atardecer",
    "luz suave de tarde nublada",
    "luz de manana temprano",
]

_ESCENAS_MOVIMIENTO = [
    "gente diversa caminando por una plaza publica con kiosco",
    "manos de vecinos de distintas edades juntas sobre una mesa comunitaria",
    "fachadas coloridas de un barrio con gente platicando en la banqueta",
    "mercado tradicional con puestos de frutas, flores y artesanias",
    "mural de historia y cultura mexicana en una pared de la colonia",
    "zocalo con arboles, familias y vendedores",
    "campo mexicano al amanecer entre milpas y montanas",
    "papel picado y artesanias en un tianguis",
    "cancha de futbol de barrio con jovenes jugando",
    "cocina comunitaria con mujeres preparando comida",
]

_ESCENAS_COTIDIANAS = [
    "puesto de tacos al atardecer con gente platicando",
    "mercado local con montones de fruta y flores",
    "cancha de futbol de barrio con jovenes jugando",
    "cocina mexicana con olla de barro humeando",
    "tianguis colorido con puestos de tela y comida",
    "calle de colonia con fachadas de colores",
    "parque de barrio con familias y globos",
    "puesto de aguas frescas en un jardin",
    "carretera al campo entre milpas y montanas",
    "kiosco de plaza con vendedores de globos",
    "comida corrida servida en mesa de mantel",
    "terraza con macetas y ropa tendida de fondo",
]

_ESCENAS_CIVICAS = [
    "zocalo de una ciudad mexicana con kiosco, arboles y gente caminando",
    "plaza publica de barrio con murales y familias reunidas",
    "cancha de futbol de barrio con jovenes y vecinos",
    "mercado tradicional con puestos de frutas, flores y artesanias",
    "campo mexicano al amanecer entre milpas y montanas",
    "calle de colonia con papel picado y locales abiertos",
    "mural de cultura mexicana en una pared comunitaria",
    "andador con artesanos y textiles de colores",
]

# (claves en la personalidad, escena para portada)
_TEMAS_ESCENA = (
    (
        ("futbol", "america", "tigres", "chivas", "seleccion", "deport", "cancha"),
        "cancha de futbol de barrio con jovenes jugando",
    ),
    (
        ("taco", "pozole", "tamal", "chilaquil", "carnita", "esquite", "comida",
         "cocina", "mercado", "marquesita"),
        "mercado o puesto de comida mexicana lleno de color",
    ),
    (
        ("corrido", "banda", "cumbia", "sonidero", "regueton", "rock", "musica",
         "concierto", "bailar"),
        "plaza con musica en vivo y gente bailando",
    ),
    (
        ("azteca", "prehispan", "piramide", "tenochtitlan", "historia", "cultura"),
        "zona arqueologica mexicana al atardecer con visitantes locales",
    ),
    (
        ("playa", "pueblo", "viaj", "carretera"),
        "carretera entre montanas rumbo a un pueblo magico",
    ),
    (
        ("perro", "gato"),
        "parque de barrio con perros y sus duenos",
    ),
    (
        ("cafe",),
        "cafeteria de la esquina con mesas afuera",
    ),
    (
        ("bici", "bicicleta"),
        "ciclovia de la ciudad con ciclistas al atardecer",
    ),
    (
        ("derecho", "territorio", "agua", "causa", "marcha", "asamblea",
         "vecin", "organizacion", "educacion", "salud", "jovenes"),
        "encuentro comunitario de vecinos en una plaza publica",
    ),
)


def _escena_por_personalidad(personalidad) -> str:
    """Elige una escena de portada segun palabras clave de la personalidad."""
    plano = _normalizar_texto(personalidad)
    if not plano:
        return ""
    for claves, escena in _TEMAS_ESCENA:
        if any(clave in plano for clave in claves):
            return escena
    return ""


def _contexto_cuenta(datos: dict) -> str:
    """Frase corta con nombre/personalidad para variar el prompt."""
    partes = []
    nombre = _nombre_clave(datos)
    if nombre:
        partes.append(f"nombre de la cuenta: {nombre}")
    seccion = str(datos.get("seccion") or datos.get("sector") or "").strip()
    if seccion:
        partes.append(f"contexto: {seccion[:40]}")
    personalidad = _frase_personalidad(datos)
    if personalidad:
        partes.append(f"intereses y tono: {personalidad}")
    return "; ".join(partes)


def _prompt_avatar(datos: dict, estilo: str) -> str:
    """Prompt de foto de perfil para un estilo "persona" o "movimiento"."""
    edad = random.randint(*random.choice(_RANGOS_EDAD))
    genero = random.choice(_GENEROS)
    luz = random.choice(_LUCES_HORA)
    extras = ""
    contexto_cuenta = _contexto_cuenta(datos)
    if contexto_cuenta:
        extras = f" Referencia de la cuenta (no dibujar texto): {contexto_cuenta}."

    if estilo == "movimiento":
        escena = random.choice(_ESCENAS_MOVIMIENTO)
        return (
            "Imagen simbolica y ciudadana de Mexico: "
            f"{escena}, con personas mexicanas diversas de distintas edades y "
            "vestimenta cotidiana, ambiente civico y de comunidad, fotografia "
            f"documental calida, {luz}, composicion cuidada."
            " Sin texto, sin letras, sin numeros, sin logos, sin marcas de agua,"
            " sin simbolos de partidos politicos y sin rostros de politicos"
            " famosos." + extras
        )

    if _normalizar_texto(_campo(datos, "tipo_cuenta", "")).startswith("activis"):
        fondo = random.choice(_FONDOS_ACTIVISTA)
        contexto = "persona mexicana comun de ambiente de barrio o ciudad"
        tono = "tono ciudadano, cercano y autentico"
        ropa = random.choice(_ROPA_PERSONA)
    else:
        fondo = random.choice(_FONDOS_PERSONA)
        contexto = random.choice(_CONTEXTOS_PERSONA)
        tono = "aspecto cotidiano y autentico"
        ropa = random.choice(_ROPA_PERSONA)

    return (
        f"Retrato fotografico realista de una persona mexicana de {edad} anos "
        f"({genero}), {contexto}, vistiendo {ropa}, en {fondo}, {luz}. "
        "Retrato de medio cuerpo, mirando a la camara, gesto natural y "
        f"cercano, {tono}, sin filtros llamativos."
        " Sin texto, sin letras, sin numeros, sin logos, sin marcas de agua y "
        "sin simbolos politicos ni de partidos." + extras
    )


def _prompt_portada(datos: dict, estilo: str) -> str:
    """Prompt de foto de portada (horizontal, 1536x1024)."""
    luz = random.choice(_LUCES_HORA)
    escena = _escena_por_personalidad(datos.get("personalidad"))
    extras = ""
    contexto_cuenta = _contexto_cuenta(datos)
    if contexto_cuenta:
        extras = f" Referencia de la cuenta (no dibujar texto): {contexto_cuenta}."

    if estilo == "movimiento":
        if not escena:
            escena = random.choice(_ESCENAS_CIVICAS)
        return (
            "Fotografia panoramica horizontal (formato 3:2), escena civica y "
            f"ciudadana en Mexico: {escena}, gente comun diversa, ambiente "
            f"autentico y luminoso, {luz}, estilo documental costumbrista."
            " Sin texto, sin letras, sin numeros, sin logos, sin marcas de agua,"
            " sin simbolos de partidos politicos y sin rostros de politicos"
            " famosos." + extras
        )

    if not escena:
        escena = random.choice(_ESCENAS_COTIDIANAS)
    return (
        "Fotografia panoramica horizontal (formato 3:2) de una escena cotidiana "
        f"en Mexico: {escena}, {luz}, colores calidos, ambiente autentico y "
        "natural, estilo fotoperiodismo costumbrista."
        " Sin texto, sin letras, sin numeros, sin logos, sin marcas de agua y "
        "sin simbolos politicos ni de partidos." + extras
    )


# --------------------------------------------------------------------------- #
# BD
# --------------------------------------------------------------------------- #
def _cargar_datos(usuario: str):
    """Copia los campos de la cuenta a un dict; None si no existe."""
    from core.database import get_db_session
    from core.models import Cuenta

    with get_db_session() as db:
        cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
        if cuenta is None:
            return None
        return {campo: getattr(cuenta, campo, "") for campo in _CAMPOS_CUENTA}


def _guardar_campo(usuario: str, campo: str, valor: str) -> bool:
    """Guarda un campo (avatar_path/banner_path) de la cuenta."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is None or not hasattr(cuenta, campo):
                return False
            setattr(cuenta, campo, valor)
        return True
    except Exception as e:
        logger.warning(f"No se pudo guardar {campo} de {usuario}: {e}")
        return False


# --------------------------------------------------------------------------- #
# Generacion con IA
# --------------------------------------------------------------------------- #
def _generar_imagen(prompt: str, size: str, output_path: str):
    """Llama al generador de imagenes; devuelve ruta o None. Nunca lanza."""
    try:
        from ia.generador_imagenes import GeneradorImagenes
    except Exception as e:
        logger.warning(f"GeneradorImagenes no disponible: {e}")
        return None
    try:
        generador = GeneradorImagenes()
        return generador.generar_imagen(
            prompt, modelo="gpt-image-1", size=size, output_path=output_path
        )
    except TypeError:
        # Compatibilidad con versiones previas sin size/output_path.
        logger.warning(
            "generar_imagen no acepta size/output_path; usando la firma anterior"
        )
        try:
            return generador.generar_imagen(prompt, modelo="gpt-image-1")
        except Exception as e:
            logger.warning(f"generar_imagen fallo (firma anterior): {e}")
            return None
    except Exception as e:
        logger.warning(f"generar_imagen fallo: {e}")
        return None


def _mover_a_destino(ruta, destino: str) -> str:
    """Si el generador devolvio otra ruta, copia el archivo al destino."""
    if not ruta:
        return ""
    try:
        if os.path.abspath(ruta) == os.path.abspath(destino):
            return ruta
        if os.path.exists(ruta):
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copyfile(ruta, destino)
            return destino
    except Exception as e:
        logger.warning(f"No se pudo copiar la imagen a {destino}: {e}")
    return ruta


def _generar_imagen_cuenta(usuario: str, campo: str, tipo: str, forzar: bool) -> dict:
    """Motor comun de avatar/portada. Devuelve el dict congelado."""
    resultado = {
        "usuario": usuario,
        "ok": False,
        "generada": False,
        "ruta": "",
        "error": "",
    }
    if not usuario:
        resultado["error"] = "usuario vacio"
        return resultado

    try:
        datos = _cargar_datos(usuario)
    except Exception as e:
        resultado["error"] = f"error de BD al leer la cuenta: {e}"
        return resultado
    if datos is None:
        resultado["error"] = "usuario no encontrado en la BD"
        return resultado

    existente = str(datos.get(campo) or "").strip()
    if existente and not forzar:
        resultado.update({"ok": True, "ruta": existente})
        return resultado

    destino = _ruta_avatar(usuario) if tipo == "avatar" else _ruta_portada(usuario)
    estilo = _estilo_visual(datos)
    if tipo == "avatar":
        prompt = _prompt_avatar(datos, estilo)
        size = "1024x1024"
    else:
        prompt = _prompt_portada(datos, estilo)
        size = "1536x1024"

    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
    except Exception as e:
        resultado["error"] = f"no se pudo crear la carpeta {os.path.dirname(destino)}: {e}"
        return resultado

    ruta = _generar_imagen(prompt, size, destino)
    if not ruta or not os.path.exists(ruta):
        resultado["error"] = "la generacion con IA no devolvio un archivo valido"
        return resultado

    ruta = _mover_a_destino(ruta, destino)
    if not _guardar_campo(usuario, campo, ruta):
        resultado["ruta"] = ruta
        resultado["error"] = (
            f"la imagen se genero pero no se pudo guardar '{campo}' en la BD"
        )
        return resultado

    resultado.update({"ok": True, "generada": True, "ruta": ruta})
    logger.info(f"{tipo} generado para {usuario}: {ruta}")
    return resultado


def generar_foto_perfil(usuario: str, forzar: bool = False) -> dict:
    """Genera (con IA) la foto de perfil de una cuenta y guarda avatar_path.

    Si la cuenta ya tiene ``avatar_path`` y ``forzar=False``, no genera nada y
    devuelve ``{"ok": True, "generada": False, "ruta": <existente>}``.
    Con ``forzar=True`` regenera SIEMPRE. Nunca lanza excepcion.
    """
    try:
        return _generar_imagen_cuenta(str(usuario or "").strip(), "avatar_path", "avatar", bool(forzar))
    except Exception as e:
        logger.exception(f"Error inesperado generando avatar de {usuario}: {e}")
        return {
            "usuario": str(usuario or "").strip(),
            "ok": False,
            "generada": False,
            "ruta": "",
            "error": f"{type(e).__name__}: {e}",
        }


def generar_portada(usuario: str, forzar: bool = False) -> dict:
    """Genera (con IA) la foto de portada y guarda banner_path.

    Mismas reglas que ``generar_foto_perfil`` pero en formato horizontal
    (size ``1536x1024``) y carpeta ``data/portadas/``. Nunca lanza excepcion.
    """
    try:
        return _generar_imagen_cuenta(str(usuario or "").strip(), "banner_path", "portada", bool(forzar))
    except Exception as e:
        logger.exception(f"Error inesperado generando portada de {usuario}: {e}")
        return {
            "usuario": str(usuario or "").strip(),
            "ok": False,
            "generada": False,
            "ruta": "",
            "error": f"{type(e).__name__}: {e}",
        }


def generar_fotos(
    usuarios: list,
    con_portada: bool = False,
    forzar: bool = False,
    dry_run: bool = False,
    callback=None,
) -> dict:
    """Genera avatar (todos) y portada (si ``con_portada``) para un lote.

    ``dry_run=True`` NO genera ni escribe en la BD: solo reporta que haria
    (``accion`` "generaria"/"omitiria" en cada entrada de ``detalle``).
    ``callback(actual, total, usuario, tipo, ok)`` se invoca por operacion.

    Devuelve:
        {"total", "avatares_ok", "portadas_ok", "omitidas", "errores": [str],
         "fallidas": int, "detalle": [ {...} ]}
    """
    resultado = {
        "total": 0,
        "avatares_ok": 0,
        "portadas_ok": 0,
        "omitidas": 0,
        "errores": [],
        "fallidas": 0,
        "detalle": [],
    }

    try:
        lista = []
        vistos = set()
        for usuario in usuarios or []:
            usuario = str(usuario or "").strip()
            if usuario and usuario not in vistos:
                vistos.add(usuario)
                lista.append(usuario)
    except Exception as e:
        resultado["errores"].append(f"lista de usuarios invalida: {e}")
        return resultado

    resultado["total"] = len(lista)
    total = len(lista)

    def _procesar(indice, usuario, tipo):
        campo = "avatar_path" if tipo == "avatar" else "banner_path"
        if dry_run:
            try:
                datos = _cargar_datos(usuario)
            except Exception as e:
                datos = None
                error = f"error de BD al leer la cuenta: {e}"
            else:
                error = "" if datos is not None else "usuario no encontrado en la BD"
            if datos is None:
                resultado["errores"].append(f"{usuario} ({tipo}): {error}")
                resultado["fallidas"] += 1
                resultado["detalle"].append(
                    {"usuario": usuario, "tipo": tipo, "accion": "error", "error": error}
                )
                _notificar(callback, indice, total, usuario, tipo, False)
                return
            existente = str(datos.get(campo) or "").strip()
            if existente and not forzar:
                resultado["omitidas"] += 1
                resultado["detalle"].append(
                    {
                        "usuario": usuario,
                        "tipo": tipo,
                        "accion": "omitiria",
                        "ruta": existente,
                    }
                )
            else:
                destino = (
                    _ruta_avatar(usuario) if tipo == "avatar" else _ruta_portada(usuario)
                )
                resultado["detalle"].append(
                    {
                        "usuario": usuario,
                        "tipo": tipo,
                        "accion": "generaria",
                        "ruta": destino,
                    }
                )
            _notificar(callback, indice, total, usuario, tipo, True)
            return

        generador = generar_foto_perfil if tipo == "avatar" else generar_portada
        try:
            salida = generador(usuario, forzar)
        except Exception as e:  # blindaje extra (las funciones nunca lanzan)
            salida = {"ok": False, "generada": False, "ruta": "", "error": str(e)}
        detalle = {
            "usuario": usuario,
            "tipo": tipo,
            "ok": bool(salida.get("ok")),
            "generada": bool(salida.get("generada")),
            "ruta": str(salida.get("ruta") or ""),
            "error": str(salida.get("error") or ""),
        }
        resultado["detalle"].append(detalle)
        if detalle["ok"] and detalle["generada"]:
            if tipo == "avatar":
                resultado["avatares_ok"] += 1
            else:
                resultado["portadas_ok"] += 1
        elif detalle["ok"]:
            resultado["omitidas"] += 1
        else:
            resultado["fallidas"] += 1
            resultado["errores"].append(f"{usuario} ({tipo}): {detalle['error']}")
        _notificar(callback, indice, total, usuario, tipo, detalle["ok"])

    for indice, usuario in enumerate(lista, 1):
        _procesar(indice, usuario, "avatar")
        if con_portada:
            _procesar(indice, usuario, "portada")

    return resultado


# --------------------------------------------------------------------------- #
# Aplicacion en X (Selenium, import perezoso)
# --------------------------------------------------------------------------- #
def _interpretar_resultado(salida):
    """Normaliza el retorno de TwitterBot (bool o dict) a bool."""
    if isinstance(salida, dict):
        return bool(salida.get("ok", salida.get("exito", False)))
    return bool(salida)


def _aplicar_en_x(usuario: str, campo: str, tipo: str) -> dict:
    """Motor comun para aplicar avatar/portada con TwitterBot."""
    resultado = {"usuario": usuario, "ok": False, "error": ""}
    if not usuario:
        resultado["error"] = "usuario vacio"
        return resultado

    try:
        datos = _cargar_datos(usuario)
    except Exception as e:
        resultado["error"] = f"error de BD al leer la cuenta: {e}"
        return resultado
    if datos is None:
        resultado["error"] = "usuario no encontrado en la BD"
        return resultado

    ruta = str(datos.get(campo) or "").strip()
    if not ruta:
        resultado["error"] = (
            f"la cuenta no tiene {'avatar_path' if tipo == 'avatar' else 'banner_path'} "
            "(primero genera la foto)"
        )
        return resultado
    if not os.path.exists(ruta):
        resultado["error"] = f"el archivo de la imagen no existe: {ruta}"
        return resultado

    bot = None
    try:
        # Import perezoso: Chrome/selenium solo se carga al aplicar de verdad.
        from plataformas.twitter.selenium_bot import TwitterBot

        metodo_nombre = (
            "cambiar_foto_perfil" if tipo == "avatar" else "cambiar_foto_portada"
        )
        bot = TwitterBot(usuario)
        metodo = getattr(bot, metodo_nombre, None)
        if not callable(metodo):
            resultado["error"] = (
                f"TwitterBot.{metodo_nombre} no esta disponible todavía "
                "(modulo de plataformas pendiente)"
            )
            return resultado

        salida = metodo(ruta)
        if _interpretar_resultado(salida):
            resultado["ok"] = True
        else:
            error = str(getattr(bot, "ultimo_error", "") or "")
            if isinstance(salida, dict) and salida.get("error"):
                error = str(salida.get("error"))
            resultado["error"] = error or f"{metodo_nombre} devolvio False"
    except Exception as e:
        resultado["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"Error aplicando {tipo} de {usuario}: {e}")
    finally:
        if bot is not None:
            try:
                bot.cerrar()
            except Exception:
                pass
    return resultado


def aplicar_foto_perfil(usuario: str) -> dict:
    """Aplica en X el ``avatar_path`` guardado, con TwitterBot (Selenium).

    Devuelve ``{"usuario", "ok", "error"}``. Nunca lanza: si la cuenta no
    tiene avatar o el metodo de Selenium no existe, lo reporta en ``error``.
    """
    try:
        return _aplicar_en_x(str(usuario or "").strip(), "avatar_path", "avatar")
    except Exception as e:
        logger.exception(f"Error inesperado aplicando avatar de {usuario}: {e}")
        return {
            "usuario": str(usuario or "").strip(),
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }


def aplicar_portada(usuario: str) -> dict:
    """Igual que ``aplicar_foto_perfil`` pero con ``banner_path``."""
    try:
        return _aplicar_en_x(str(usuario or "").strip(), "banner_path", "portada")
    except Exception as e:
        logger.exception(f"Error inesperado aplicando portada de {usuario}: {e}")
        return {
            "usuario": str(usuario or "").strip(),
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }


def aplicar_fotos(usuarios: list, con_portada: bool = False, callback=None) -> dict:
    """Aplica avatar (y portada si ``con_portada``) en X para un lote.

    ``callback(actual, total, usuario, tipo, ok)`` se invoca por operacion.
    Devuelve ``{"total", "avatares_ok", "portadas_ok", "fallidas", "detalle"}``
    (``fallidas`` es el numero de operaciones fallidas; el motivo va en
    ``detalle``). Nunca lanza excepcion.
    """
    resultado = {
        "total": 0,
        "avatares_ok": 0,
        "portadas_ok": 0,
        "fallidas": 0,
        "detalle": [],
    }

    try:
        lista = []
        vistos = set()
        for usuario in usuarios or []:
            usuario = str(usuario or "").strip()
            if usuario and usuario not in vistos:
                vistos.add(usuario)
                lista.append(usuario)
    except Exception as e:
        resultado["detalle"].append({"usuario": "", "tipo": "", "ok": False, "error": str(e)})
        return resultado

    resultado["total"] = len(lista)
    total = len(lista)

    def _procesar(indice, usuario, tipo):
        aplicador = aplicar_foto_perfil if tipo == "avatar" else aplicar_portada
        salida = aplicador(usuario)
        detalle = {
            "usuario": usuario,
            "tipo": tipo,
            "ok": bool(salida.get("ok")),
            "error": str(salida.get("error") or ""),
        }
        resultado["detalle"].append(detalle)
        if detalle["ok"]:
            if tipo == "avatar":
                resultado["avatares_ok"] += 1
            else:
                resultado["portadas_ok"] += 1
        else:
            resultado["fallidas"] += 1
        _notificar(callback, indice, total, usuario, tipo, detalle["ok"])

    for indice, usuario in enumerate(lista, 1):
        _procesar(indice, usuario, "avatar")
        if con_portada:
            _procesar(indice, usuario, "portada")

    return resultado


# --------------------------------------------------------------------------- #
# Brandeo manual (archivos propios del operador)
# --------------------------------------------------------------------------- #
_EXT_MANUAL_PERMITIDAS = {".png", ".jpg", ".jpeg", ".webp"}


def _resolver_origen_manual(ruta) -> str:
    """Devuelve la ruta absoluta del archivo origen dado por el operador.

    Acepta rutas absolutas o relativas al proyecto (via
    ``core.config.resolver_ruta``). Tolera el alias ``data/avatars/`` (lo
    mapea a ``data/avatares/``) porque parte de la documentacion lo nombra
    asi; el canonico en este modulo es ``data/avatares/``.
    """
    texto = str(ruta or "").strip()
    if not texto:
        return ""
    # Alias documentado: data/avatars/ -> data/avatares/.
    normalizado = texto.replace("\\", "/")
    if "data/avatars/" in normalizado:
        texto = texto.replace("data/avatars/", "data/avatares/").replace(
            "data\\avatars\\", "data/avatares/"
        )
    if os.path.isabs(texto):
        return os.path.abspath(texto)
    try:
        from core.config import resolver_ruta

        return resolver_ruta(texto)
    except Exception:
        return os.path.abspath(texto)


def _validar_archivo_manual(origen_abs: str) -> str:
    """Valida el archivo origen. Devuelve "" si es valido o el motivo."""
    if not origen_abs:
        return "ruta vacia"
    if not os.path.isfile(origen_abs):
        return f"el archivo no existe: {origen_abs}"
    try:
        if os.path.getsize(origen_abs) <= 0:
            return f"el archivo esta vacio: {origen_abs}"
    except Exception as e:
        return f"no se pudo leer el archivo: {e}"
    _, ext = os.path.splitext(origen_abs)
    if ext.lower() not in _EXT_MANUAL_PERMITIDAS:
        return (
            f"extension no valida ({ext or 'sin extension'}): "
            "usa png/jpg/jpeg/webp"
        )
    return ""


def registrar_fotos_manuales(
    usuario: str,
    foto_perfil_path=None,
    foto_portada_path=None,
) -> dict:
    """Registra fotos manuales (brandeo) para una cuenta.

    Copia el archivo del operador a la ruta canonica
    (``data/avatares/<usuario>.png`` para avatar,
    ``data/portadas/<usuario>.png`` para portada) y actualiza
    ``Cuenta.avatar_path`` / ``Cuenta.banner_path`` en la BD.

    Args:
        usuario: clave interna de la cuenta (``Cuenta.usuario``).
        foto_perfil_path: ruta del avatar (absoluta o relativa al
            proyecto). ``None``/vacio = no tocar el avatar.
        foto_portada_path: ruta de la portada. ``None``/vacio = no tocarla.

    Devuelve (nunca lanza excepcion):
        {"usuario", "ok", "avatar": {"ok","ruta","error"},
         "portada": {"ok","ruta","error"}, "error": str}
    Solo aparecen las claves del tipo solicitado; ``ok`` global es True
    cuando todo lo solicitado se registro bien.
    """
    resultado = {
        "usuario": str(usuario or "").strip(),
        "ok": False,
        "avatar": None,
        "portada": None,
        "error": "",
    }
    usuario = resultado["usuario"]
    try:
        if not usuario:
            resultado["error"] = "usuario vacio"
            return resultado

        perfil_dado = str(foto_perfil_path or "").strip() if foto_perfil_path is not None else ""
        portada_dada = str(foto_portada_path or "").strip() if foto_portada_path is not None else ""
        if not perfil_dado and not portada_dada:
            resultado["error"] = "indica al menos una foto (perfil o portada)"
            return resultado

        try:
            datos = _cargar_datos(usuario)
        except Exception as e:
            resultado["error"] = f"error de BD al leer la cuenta: {e}"
            return resultado
        if datos is None:
            resultado["error"] = "usuario no encontrado en la BD"
            return resultado

        pendientes = []
        if perfil_dado:
            pendientes.append(("avatar", "avatar_path", perfil_dado, _ruta_avatar(usuario)))
        if portada_dada:
            pendientes.append(("portada", "banner_path", portada_dada, _ruta_portada(usuario)))

        errores = []
        todo_ok = True
        for tipo, campo, origen_dado, destino in pendientes:
            detalle = {"ok": False, "ruta": "", "error": ""}
            origen_abs = _resolver_origen_manual(origen_dado)
            motivo = _validar_archivo_manual(origen_abs)
            if motivo:
                detalle["error"] = motivo
                resultado[tipo] = detalle
                errores.append(f"{tipo}: {motivo}")
                todo_ok = False
                continue
            try:
                os.makedirs(os.path.dirname(destino), exist_ok=True)
                if os.path.abspath(origen_abs) != os.path.abspath(destino):
                    shutil.copyfile(origen_abs, destino)
                # Si el origen ya era el destino, no hay nada que copiar.
            except Exception as e:
                detalle["error"] = f"no se pudo copiar a {destino}: {e}"
                resultado[tipo] = detalle
                errores.append(f"{tipo}: {detalle['error']}")
                todo_ok = False
                continue
            if not _guardar_campo(usuario, campo, destino):
                detalle["ruta"] = destino
                detalle["error"] = (
                    f"el archivo se copio pero no se pudo guardar "
                    f"'{campo}' en la BD"
                )
                resultado[tipo] = detalle
                errores.append(f"{tipo}: {detalle['error']}")
                todo_ok = False
                continue
            detalle.update({"ok": True, "ruta": destino})
            resultado[tipo] = detalle
            logger.info(f"foto manual ({tipo}) registrada para {usuario}: {destino}")

        # Quita las claves no solicitadas para una respuesta limpia.
        if not perfil_dado:
            resultado.pop("avatar", None)
        if not portada_dada:
            resultado.pop("portada", None)
        resultado["ok"] = bool(todo_ok)
        if not todo_ok:
            resultado["error"] = "; ".join(errores)
        return resultado
    except Exception as e:
        logger.exception(f"Error inesperado registrando fotos manuales de {usuario}: {e}")
        resultado["error"] = f"{type(e).__name__}: {e}"
        return resultado
