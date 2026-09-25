"""Filtros locales del motor de alertas.

Portado del motor viejo (`GestorTwitter/logica_alertas.py`) al esqueleto nuevo:

- `FiltrosGeograficos`: whitelist de regiones por localidad
  (`_REGIONES_VALIDAS`), normalizacion sin acentos, paises/ciudades extranjeras
  ampliadas (LatAm + Espana + EEUU) y la regla del viejo
  `_es_ubicacion_irrelevante`: las keywords principales (nombres propios) solo
  se descartan si hay mencion extranjera SIN mencion local; las genericas
  requieren contexto geografico local.
- `FiltrosTematicos`: `exclude_terms` del cliente y blacklist global
  (turismo/hoteles/deportes/farandula...) con match por PALABRA/LIMITE (no
  substring: "liga" ya no mata "obligacion"), sobre titulo+resumen; la
  blacklist global NO aplica a menciones principales.
- `verificar_cuerpo_batch`: verifica el cuerpo de articulos de Google News
  para rescatar menciones cuyo titulo/snippet no trae la keyword (portado de
  `_verificar_cuerpo_batch` / `_extraer_texto_articulo`). Se activa con
  `ALERTAS_VERIFICAR_CUERPO=1` (default ON) con tope `ALERTAS_CUERPO_MAX`
  (default 30) y workers `ALERTAS_CUERPO_WORKERS` (default 4). Nunca lanza.
"""

import html as _html
import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from loguru import logger

# --------------------------------------------------------------------------- #
# Regiones validas por localidad (whitelist del motor viejo, normalizada)
# --------------------------------------------------------------------------- #
_REGIONES_VALIDAS = {
    "cancun": {"cancun", "quintana roo", "q. roo", "q.roo", "qroo",
               "caribe mexicano", "riviera maya", "playa del carmen", "tulum",
               "cozumel", "isla mujeres", "holbox", "chetumal", "cabo catoche",
               "benito juarez quintana", "solidaridad", "puerto morelos",
               "lazaro cardenas quintana", "yum balam", "sian kaan",
               "bacalar", "mahahual", "felipe carrillo puerto"},
    "azcapotzalco": {"azcapotzalco", "cdmx", "ciudad de mexico", "c.d.m.x",
                     "alcaldia", "vallejo", "g.a.m."},
    "guadalajara": {"guadalajara", "jalisco", "zona metropolitana de guadalajara",
                    "zapopan", "tlaquepaque", "tonala jalisco"},
    "monterrey": {"monterrey", "nuevo leon", "n.l.", "zona metropolitana de monterrey",
                  "san pedro garza", "santa catarina nuevo", "apodaca"},
    "merida": {"merida", "yucatan", "merida yucatan"},
    "puebla": {"puebla", "puebla de zaragoza"},
    "tijuana": {"tijuana", "baja california", "bc"},
    "leon": {"leon", "guanajuato", "leon guanajuato"},
    "queretaro": {"queretaro", "queretaro de arteaga"},
    "toluca": {"toluca", "estado de mexico", "edomex"},
    "morelia": {"morelia", "michoacan"},
    "veracruz": {"veracruz", "boca del rio"},
    "oaxaca": {"oaxaca"},
    "chihuahua": {"chihuahua", "ciudad juarez"},
    "hermosillo": {"hermosillo", "sonora"},
    "aguascalientes": {"aguascalientes"},
    "culiacan": {"culiacan", "sinaloa"},
    "durango": {"durango"},
    "saltillo": {"saltillo", "coahuila"},
    "villahermosa": {"villahermosa", "tabasco"},
    "tuxtla": {"tuxtla gutierrez", "chiapas"},
    "acapulco": {"acapulco", "guerrero"},
    "cuernavaca": {"cuernavaca", "morelos"},
    "xalapa": {"xalapa"},
    "tampico": {"tampico", "tamaulipas"},
    "reynosa": {"reynosa", "tamaulipas"},
    "mazatlan": {"mazatlan", "sinaloa"},
    "pachuca": {"pachuca", "hidalgo"},
    "campeche": {"campeche"},
    "colima": {"colima"},
    "tepic": {"tepic", "nayarit"},
    "zacatecas": {"zacatecas"},
    "tlaxcala": {"tlaxcala"},
}

# Estados completos -> municipios/alias (para localidades que son estado).
_REGIONES_ESTADO = {
    "jalisco": {"jalisco", "guadalajara", "zapopan", "tlaquepaque", "tonala"},
    "nuevo leon": {"nuevo leon", "monterrey", "san pedro garza", "apodaca"},
    "quintana roo": {"quintana roo", "qroo", "cancun", "playa del carmen",
                     "tulum", "cozumel", "chetumal", "bacalar", "holbox",
                     "isla mujeres", "cabo catoche", "yum balam"},
    "yucatan": {"yucatan", "merida"},
    "guanajuato": {"guanajuato", "leon", "irapuato", "celaya"},
    "sinaloa": {"sinaloa", "culiacan", "mazatlan"},
    "tamaulipas": {"tamaulipas", "tampico", "reynosa", "matamoros"},
    "michoacan": {"michoacan", "morelia"},
    "veracruz": {"veracruz", "xalapa", "boca del rio"},
    "puebla": {"puebla"},
    "sonora": {"sonora", "hermosillo"},
    "chihuahua": {"chihuahua", "ciudad juarez"},
    "coahuila": {"coahuila", "saltillo", "torreon"},
    "oaxaca": {"oaxaca"},
    "guerrero": {"guerrero", "acapulco"},
    "morelos": {"morelos", "cuernavaca"},
    "tabasco": {"tabasco", "villahermosa"},
    "chiapas": {"chiapas", "tuxtla gutierrez", "san cristobal"},
    "hidalgo": {"hidalgo", "pachuca"},
    "nayarit": {"nayarit", "tepic"},
    "baja california": {"baja california", "tijuana", "mexicali", "ensenada"},
    "baja california sur": {"baja california sur", "la paz", "los cabos", "cabo san lucas"},
    "estado de mexico": {"estado de mexico", "edomex", "toluca"},
    "ciudad de mexico": {"ciudad de mexico", "cdmx", "c.d.m.x"},
    "cdmx": {"cdmx", "ciudad de mexico", "c.d.m.x"},
}

# Paises extranjeros (LatAm + Espana + EEUU), del viejo ampliados.
_PAISES_EXTRANJEROS = {
    "peru", "colombia", "argentina", "chile", "venezuela", "ecuador",
    "bolivia", "uruguay", "paraguay", "costa rica", "panama",
    "guatemala", "honduras", "el salvador", "nicaragua", "cuba",
    "republica dominicana", "puerto rico", "espana", "brasil",
    "estados unidos", "ee.uu.", "eeuu", "canada", "haiti", "belice",
    "kenia", "marruecos", "argelia", "egipto", "sudafrica", "nigeria",
    "china", "india", "japon", "australia", "alemania", "francia",
    "italia", "reino unido", "inglaterra", "portugal", "grecia",
    "turquia", "rusia", "ucrania",
}

_CIUDADES_EXTRANJERAS = {
    "lima", "bogota", "buenos aires", "santiago chile", "caracas",
    "quito", "la paz bolivia", "montevideo", "asuncion", "san jose costa rica",
    "madrid", "barcelona", "valencia espana", "sevilla", "bilbao", "galicia",
    "lugo", "washington", "new york", "nueva york", "miami", "los angeles",
    "houston", "chicago", "san antonio", "phoenix", "dallas", "orlando",
    "sao paulo", "rio de janeiro", "brasilia", "cucuta", "medellin",
    "cali colombia", "piura", "arequipa", "trujillo peru", "sechura",
    "engativa", "mendoza", "cordoba argentina", "rosario argentina",
    "guayaquil", "maracaibo", "barquisimeto", "valencia venezuela",
    "san salvador", "tegucigalpa", "managua", "panama city",
    "miami beach", "ny", "dc",
    # Argentina (provincias/ciudades que aparecen en notas de ANP)
    "piuquenes", "valle de uco", "tunuyan", "el calafate", "calafate",
    "perito moreno", "ushuaia", "tierra del fuego", "neuquen", "bariloche",
    "mar del plata", "la plata argentina", "tucuman", "salta argentina",
    "santa fe argentina", "entre rios", "misiones argentina", "chubut",
    "rio negro argentina", "la pampa", "san juan argentina",
    "san luis argentina", "catamarca", "jujuy", "formosa argentina",
    "chaco argentina", "santiago del estero", "hurlingham", "patagonia",
    "horco molle", "iguazu",
    # Peru / Colombia / Kenia / otros
    "paracas", "cusco", "sacsayhuaman", "iguaque", "iguique", "nairobi",
    # Espana
    "alicante", "malaga", "murcia", "zaragoza espana", "tenerife",
    "canarias", "baleares", "mallorca", "ibiza", "vigo", "gijon",
    "pamplona", "valladolid", "salamanca espana", "gran canaria",
}

# Temas globalmente excluidos para keywords GENERICAS (no para principales).
_TEMAS_EXCLUIDOS = (
    "deportes", "futbol", "liga", "champions", "super bowl", "nba", "mlb",
    "entretenimiento", "espectaculos", "farandula", "celebridad", "reality",
    "pelicula", "peliculas", "serie", "series", "netflix", "disney", "telenovela",
    "concierto", "conciertos", "festival musical",
    "turismo", "hotel", "hoteles", "resort", "resorts", "vuelo", "vuelos",
    "aeropuerto", "vacaciones", "all inclusive", "todo incluido",
    "gastronomia", "restaurante", "restaurantes", "receta", "recetas",
    "comida", "tacos", "pizza", "sushi", "mariscos",
    "moda", "belleza", "maquillaje", "ropa", "outfit",
    "horoscopo", "loteria", "melate",
)


def _normalizar_texto(texto: str) -> str:
    """Minusculas sin acentos (misma normalizacion que el motor viejo)."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _contiene_palabra_completa(texto_norm: str, termino_norm: str) -> bool:
    """Match por palabra/limite (evita 'liga' dentro de 'obligacion')."""
    if not texto_norm or not termino_norm:
        return False
    patron = r"(?:^|\W)" + re.escape(termino_norm) + r"(?:$|\W)"
    return bool(re.search(patron, texto_norm))


def texto_normalizado(mencion: dict) -> str:
    """Titulo + resumen + medio normalizados (para matching)."""
    if not isinstance(mencion, dict):
        return _normalizar_texto(str(mencion))
    return _normalizar_texto(
        f"{mencion.get('titulo', '')} {mencion.get('resumen', '')} "
        f"{mencion.get('fuente_medio', '')}"
    )


def _regiones_de(localidad_norm: str) -> set:
    regiones = set(_REGIONES_VALIDAS.get(localidad_norm, set()))
    regiones |= set(_REGIONES_ESTADO.get(localidad_norm, set()))
    regiones.add(localidad_norm)
    return {r for r in regiones if r}


def _texto_menciona_region(texto_norm: str, localidad_norm: str) -> bool:
    """True si el texto menciona alguna region valida para la localidad."""
    if not localidad_norm:
        return False
    return any(r in texto_norm for r in _regiones_de(localidad_norm))


def _texto_menciona_extranjero(texto_norm: str) -> bool:
    """True si el texto menciona un pais/ciudad extranjera (palabra completa)."""
    for pais in _PAISES_EXTRANJEROS:
        if pais in texto_norm and _contiene_palabra_completa(texto_norm, pais):
            return True
    for ciudad in _CIUDADES_EXTRANJERAS:
        if ciudad in texto_norm and _contiene_palabra_completa(texto_norm, ciudad):
            return True
    return False


def _es_ubicacion_irrelevante(texto_norm: str, localidad_norm: str,
                              tiene_kw_especifica: bool) -> bool:
    """Regla del viejo `_es_ubicacion_irrelevante`.

    - Keyword principal (nombre propio): solo descartar si menciona extranjero
      y NO menciona la region local.
    - Keyword generica: debe mencionar la region local; si ademas menciona
      extranjero, se descarta.
    """
    if not localidad_norm:
        return False

    menciona_region = _texto_menciona_region(texto_norm, localidad_norm)
    menciona_extranjero = _texto_menciona_extranjero(texto_norm)

    if tiene_kw_especifica:
        return menciona_extranjero and not menciona_region

    if not menciona_region:
        return True
    if menciona_extranjero:
        return True
    return False


class FiltrosGeograficos:
    """Filtro geografico por regiones (portado del viejo)."""

    def __init__(self):
        self.paises_extranjeros = sorted(_PAISES_EXTRANJEROS)
        self.sinonimos_regiones = {k: sorted(v) for k, v in _REGIONES_ESTADO.items()}

    def verificar_region(self, mencion: dict, localidad: str,
                         tiene_kw_especifica: bool = None) -> bool:
        """True si la mencion pasa el filtro geografico.

        `tiene_kw_especifica=None` (default) usa `mencion["es_principal"]`.
        """
        if not localidad:
            return True
        if tiene_kw_especifica is None:
            tiene_kw_especifica = bool(mencion.get("es_principal"))
        loc_norm = _normalizar_texto(localidad)
        return not _es_ubicacion_irrelevante(
            texto_normalizado(mencion), loc_norm, bool(tiene_kw_especifica)
        )


class FiltrosTematicos:
    """Exclusion de terminos del cliente + blacklist global para genericas."""

    def __init__(self):
        self.temas_excluidos = list(_TEMAS_EXCLUIDOS)

    def excluir(self, mencion: dict, exclude_terms: list = None,
                es_principal: bool = False) -> bool:
        """True si la mencion debe descartarse.

        `exclude_terms` del cliente se aplica SIEMPRE (principales incluidas).
        La blacklist global solo cuando `es_principal` es False.
        """
        texto = texto_normalizado(mencion)

        for termino in (exclude_terms or []):
            termino_norm = _normalizar_texto(termino)
            if termino_norm and _contiene_palabra_completa(texto, termino_norm):
                return True

        if es_principal:
            return False

        for tema in self.temas_excluidos:
            tema_norm = _normalizar_texto(tema)
            if tema_norm and _contiene_palabra_completa(texto, tema_norm):
                return True

        return False


# --------------------------------------------------------------------------- #
# Verificacion de cuerpo de articulo (rescate de menciones)
# --------------------------------------------------------------------------- #
def _env_bool(nombre: str, default: bool = True) -> bool:
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return default
    return valor in ("1", "true", "si", "sí", "yes", "on")


def verificacion_cuerpo_activa() -> bool:
    """Default ON (con tope bajo por `ALERTAS_CUERPO_MAX`)."""
    return _env_bool("ALERTAS_VERIFICAR_CUERPO", True)


def _extraer_texto_articulo(url_google_news: str, timeout: float = 4.0) -> str:
    """Texto plano de un articulo de Google News (nunca lanza).

    Decodifica el link con googlenewsdecoder (tolerante), descarga con tope de
    ~200KB y extrae og:title/og:description/meta + parrafos <p>. Corre en un
    hilo con timeout estricto para que una pagina colgada no frene la corrida.
    """
    resultado = [""]

    def _extraer():
        try:
            import re as _re

            url_real = url_google_news
            if "news.google.com" in url_google_news:
                try:
                    from googlenewsdecoder import gnewsdecoder

                    decoded = gnewsdecoder(url_google_news)
                    if decoded and decoded.get("decoded_url"):
                        url_real = decoded["decoded_url"]
                except Exception:
                    pass

            if any(d in url_real for d in ("instagram.com", "facebook.com", "tiktok.com")):
                return

            headers = {
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            }
            response = requests.get(url_real, headers=headers, timeout=(3, 5), stream=True)
            contenido = ""
            for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
                contenido += chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="ignore")
                if len(contenido) > 200000:
                    break
            response.close()

            og_title = _re.findall(r'property="og:title"\s+content="([^"]*)"', contenido)
            og_desc = _re.findall(r'property="og:description"\s+content="([^"]*)"', contenido)
            meta_desc = _re.findall(r'name="description"\s+content="([^"]*)"', contenido)
            parrafos = _re.findall(r"<p[^>]*>(.*?)</p>", contenido, _re.DOTALL)
            texto_parrafos = " ".join(_re.sub(r"<[^>]+>", " ", p) for p in parrafos[:50])

            crudo = _html.unescape(" ".join(og_title + og_desc + meta_desc) + " " + texto_parrafos)
            resultado[0] = _normalizar_texto(crudo)
        except Exception:
            pass

    hilo = threading.Thread(target=_extraer, daemon=True)
    hilo.start()
    hilo.join(timeout=timeout)
    return resultado[0]


def verificar_cuerpo_batch(menciones: list[dict], kw_principales: list[str],
                           max_workers: int = None, max_articulos: int = None) -> list[dict]:
    """Rescata menciones cuya keyword principal aparece SOLO en el cuerpo.

    Devuelve la lista de menciones rescatadas (con `es_principal=True`,
    `kw_principal` y `kw_cuerpo=True`). Nunca lanza.
    """
    if not menciones or not kw_principales:
        return []

    try:
        max_workers = int(max_workers or os.getenv("ALERTAS_CUERPO_WORKERS", "4"))
    except (TypeError, ValueError):
        max_workers = 4
    try:
        max_articulos = int(max_articulos or os.getenv("ALERTAS_CUERPO_MAX", "30"))
    except (TypeError, ValueError):
        max_articulos = 30
    max_workers = max(1, min(max_workers, 8))
    max_articulos = max(0, max_articulos)

    candidatas = [m for m in menciones[:max_articulos] if m.get("enlace")]
    if not candidatas:
        return []

    kw_norm = [(_normalizar_texto(k), k) for k in kw_principales if str(k).strip()]
    rescatadas: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futuros = {
            executor.submit(_extraer_texto_articulo, m.get("enlace", "")): m
            for m in candidatas
        }
        for futuro in as_completed(futuros):
            mencion = futuros[futuro]
            try:
                texto = futuro.result()
            except Exception:
                continue
            if not texto or len(texto) < 50:
                continue
            for kw_n, kw_original in kw_norm:
                if not kw_n or len(kw_n) < 3:
                    continue
                if " " in kw_n:
                    if all(p in texto for p in kw_n.split()):
                        mencion["es_principal"] = True
                        mencion["kw_principal"] = kw_original
                        mencion["kw_cuerpo"] = True
                        rescatadas.append(mencion)
                        break
                elif kw_n in texto:
                    mencion["es_principal"] = True
                    mencion["kw_principal"] = kw_original
                    mencion["kw_cuerpo"] = True
                    rescatadas.append(mencion)
                    break

    if rescatadas:
        logger.info(f"Cuerpo del articulo: {len(rescatadas)} menciones rescatadas")
    return rescatadas
