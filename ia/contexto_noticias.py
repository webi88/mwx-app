"""Contexto de noticias (prensa) para la activacion masiva.

Este modulo resuelve tres cosas:

1. **Normalizar links**: acepta una lista o un texto pegado por el usuario
   (con numeracion, vinetas, comillas o espacios) y devuelve solo URLs
   ``http(s)`` limpias, deduplicadas por URL normalizada (minusculas en
   esquema/host, sin fragmento, sin parametros de tracking y sin slash final)
   conservando el orden original.
2. **Scrapear noticias**: descarga cada URL con ``curl_cffi`` usando huella de
   Chrome (fallback a ``httpx`` y ``requests``) y extrae titulo + cuerpo con un
   parser de la libreria estandar (``html.parser``). Sin dependencias nuevas.
3. **Construir un contexto/briefing con IA**: manda las noticias (y un texto
   extra opcional del usuario) a OpenAI ``gpt-4o-mini`` para sintetizar un
   contexto de 900-1500 caracteres con los hechos clave. Si la IA no esta
   disponible o devuelve vacio, arma un fallback local con los titulos y los
   primeros caracteres de cada noticia.

Interfaz publica CONGELADA (no cambiar nombres ni forma de retorno)::

    normalizar_links(links) -> list[str]
    extraer_noticia(url, timeout=20) -> dict
    extraer_noticias(links, timeout=20, max_workers=4) -> list[dict]
    generar_contexto_desde_links(links, texto_extra="", max_caracteres=1800, narrativa="") -> dict

Ninguna funcion lanza excepciones hacia afuera: ante cualquier fallo devuelven
listas vacias, un resultado con ``ok=False`` o el fallback local.
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import openai
from loguru import logger

from core.config import settings

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

# Tamano maximo del cuerpo descargado (bytes) para no reventar memoria con
# paginas gigantes ni gastar datos de mas.
MAX_BODY_BYTES = 1_500_000

# Tamano maximo del texto guardado por noticia (caracteres).
MAX_TEXTO_NOTICIA = 4000

# Tamano maximo del cuerpo de cada noticia dentro del prompt de la IA.
MAX_TEXTO_PROMPT = 1500

# Un parrafo de menos de estos caracteres se considera ruido (menus, creditos).
MIN_PARRAFO = 40

# Caracteres por noticia en el fallback local (el requisito pide 400-600).
CHARS_FALLBACK_NOTICIA = 500

# Patron para encontrar URLs dentro de lineas con numeracion, vinetas, etc.
_PATRON_URL = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)

# Cabeceras de navegador para los backends httpx/requests.
_HEADERS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.8",
}


# --------------------------------------------------------------------------- #
# Utilidades de texto
# --------------------------------------------------------------------------- #
def _limpiar_espacios(texto: str) -> str:
    """Colapsa espacios/saltos repetidos y recorta extremos."""
    return re.sub(r"\s+", " ", texto or "").strip()


def _error_corto(error) -> str:
    """Convierte una excepcion/mensaje en un texto corto de una sola linea."""
    return str(error).replace("\n", " ").strip()[:200]


# --------------------------------------------------------------------------- #
# 1) Normalizacion y deduplicacion de links
# --------------------------------------------------------------------------- #
def _limpiar_url(cruda: str) -> str:
    """Quita comillas, parentesis desbalanceados y puntuacion pegada al final."""
    url = (cruda or "").strip().strip("<>[]{}'\"`").strip()
    url = url.replace("&amp;", "&")
    if not url:
        return ""
    # Puntuacion final tipica de un texto pegado ("...", ",", ").", etc.).
    while url and url[-1] in ".,;:!?…":
        url = url[:-1]
    # Cierres desbalanceados: "https://x.com/nota)" o "nota]".
    for apertura, cierre in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(cierre) and url.count(apertura) < url.count(cierre):
            url = url[:-1]
    return url.strip()


def _es_param_tracking(nombre: str) -> bool:
    """True si el parametro de query es de tracking (utm_* o fbclid)."""
    clave = (nombre or "").lower()
    return clave.startswith("utm_") or clave == "fbclid"


def _normalizar_url(url: str) -> str:
    """Clave canonica de deduplicacion: minusculas en esquema/host, sin
    fragmento, sin tracking y sin slash final. Nunca lanza."""
    try:
        partes = urlsplit(url)
        esquema = (partes.scheme or "http").lower()
        host = (partes.netloc or "").lower()
        ruta = partes.path or ""
        if len(ruta) > 1 and ruta.endswith("/"):
            ruta = ruta.rstrip("/")
        elif ruta == "/":
            ruta = ""
        try:
            pares = parse_qsl(partes.query, keep_blank_values=True)
        except Exception:
            pares = []
        query = urlencode(
            [(k, v) for k, v in pares if not _es_param_tracking(k)],
            doseq=True,
        )
        return urlunsplit((esquema, host, ruta, query, ""))
    except Exception:
        return url or ""


def _extraer_urls(links) -> list[str]:
    """Extrae todas las URLs validas de un texto/lista SIN deduplicar.

    Acepta ``None``, strings (una o varias lineas, con numeracion o no) y
    cualquier iterable de strings. Nunca lanza.
    """
    if links is None:
        return []

    if isinstance(links, str):
        candidatos = [links]
    elif isinstance(links, (list, tuple, set, frozenset)):
        candidatos = [c for c in links if isinstance(c, str)]
    else:
        try:
            candidatos = [str(c) for c in links]
        except TypeError:
            return []

    urls = []
    for candidato in candidatos:
        for encontrada in _PATRON_URL.findall(candidato or ""):
            limpia = _limpiar_url(encontrada)
            if not limpia:
                continue
            try:
                if not urlsplit(limpia).netloc:
                    continue
            except Exception:
                continue
            urls.append(limpia)
    return urls


def normalizar_links(links) -> list[str]:
    """Normaliza y deduplica links conservando el orden de entrada.

    - Acepta lista o texto (lineas/espacios) con numeracion, vinetas, comillas.
    - Se queda solo con URLs ``http(s)://`` validas.
    - Deduplica por URL normalizada (minusculas en esquema/host, sin fragmento,
      sin ``utm_*``/``fbclid`` y sin slash final).
    - Nunca lanza: ante cualquier problema devuelve ``[]``.
    """
    try:
        unicos = []
        vistos = set()
        for url in _extraer_urls(links):
            clave = _normalizar_url(url)
            if clave in vistos:
                continue
            vistos.add(clave)
            unicos.append(url)
        return unicos
    except Exception as e:  # pragma: no cover - defensivo
        logger.debug(f"normalizar_links fallo: {_error_corto(e)}")
        return []


def _deduplicar_con_conteo(urls: list[str]) -> tuple[list[str], int]:
    """Deduplica URLs ya extraidas y cuenta cuantas entradas repetidas habia."""
    unicos = []
    vistos = set()
    for url in urls:
        clave = _normalizar_url(url)
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(url)
    return unicos, max(0, len(urls) - len(unicos))


# --------------------------------------------------------------------------- #
# 2) Descarga y parseo de noticias
# --------------------------------------------------------------------------- #
def _decodificar(contenido: bytes, encoding: str = "") -> str:
    """Decodifica bytes a texto probando el encoding declarado, utf-8 y latin-1."""
    datos = (contenido or b"")[:MAX_BODY_BYTES]
    if encoding:
        try:
            return datos.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            pass
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError as e:
        # Si el error cae justo en el borde del recorte, no es un problema de
        # encoding sino del limite de bytes: decodificamos el prefijo valido.
        if 0 < e.start <= len(datos) and e.start >= MAX_BODY_BYTES - 4:
            return datos[: e.start].decode("utf-8", errors="replace")
        return datos.decode("latin-1", errors="replace")


def _descargar_html(url: str, timeout: int = 20) -> tuple[str, str]:
    """Descarga el HTML probando curl_cffi -> httpx -> requests.

    Devuelve ``(html, error)``; ``error`` es "" cuando todo salio bien.
    Limita el cuerpo a ``MAX_BODY_BYTES``. Nunca lanza.
    """
    errores = []

    # 1) curl_cffi con huella de Chrome (mismo cliente que usa el proyecto).
    try:
        from curl_cffi import requests as curl_requests

        response = curl_requests.get(
            url, impersonate="chrome", timeout=timeout, allow_redirects=True
        )
        if response.status_code >= 400:
            errores.append(f"curl_cffi HTTP {response.status_code}")
        else:
            html = _decodificar(
                response.content, getattr(response, "encoding", "") or ""
            )
            if html.strip():
                return html, ""
            errores.append("curl_cffi respuesta vacia")
    except Exception as e:
        errores.append(f"curl_cffi: {_error_corto(e)}")

    # 2) httpx con un timeout de respaldo mas corto.
    respaldo_timeout = max(5, int(timeout) // 2)
    try:
        import httpx

        with httpx.Client(
            follow_redirects=True,
            timeout=respaldo_timeout,
            headers=_HEADERS_NAVEGADOR,
        ) as cliente:
            respuesta = cliente.get(url)
            if respuesta.status_code >= 400:
                errores.append(f"httpx HTTP {respuesta.status_code}")
            else:
                html = _decodificar(
                    respuesta.content, getattr(respuesta, "encoding", "") or ""
                )
                if html.strip():
                    return html, ""
                errores.append("httpx respuesta vacia")
    except Exception as e:
        errores.append(f"httpx: {_error_corto(e)}")

    # 3) requests como ultimo recurso.
    try:
        import requests

        respuesta = requests.get(
            url, timeout=respaldo_timeout, headers=_HEADERS_NAVEGADOR
        )
        if respuesta.status_code >= 400:
            errores.append(f"requests HTTP {respuesta.status_code}")
        else:
            html = _decodificar(
                respuesta.content, getattr(respuesta, "encoding", "") or ""
            )
            if html.strip():
                return html, ""
            errores.append("requests respuesta vacia")
    except Exception as e:
        errores.append(f"requests: {_error_corto(e)}")

    return "", ("fetch fallido: " + " | ".join(errores))[:300]


class _ExtractorHTML(HTMLParser):
    """Parser minimo (libreria estandar) que extrae titulo y parrafos.

    - Prioriza ``og:title``, luego ``<title>`` y luego el primer ``<h1>``.
    - Junta el contenido de los ``<p>``.
    - Ignora script/style/noscript/nav/header/footer/aside/svg/form/iframe.
    - Guarda ``og:description``/``meta description`` como respaldo del cuerpo.
    """

    _IGNORAR = {
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "aside",
        "svg",
        "form",
        "iframe",
        "template",
    }
    _BLOQUES = {"p", "h1"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.titulo_og = ""
        self.titulo_documento = ""
        self.titulo_h1 = ""
        self.descripcion = ""
        self.parrafos: list[str] = []
        self._ignorar = 0
        self._captura = ""
        self._buffer: list[str] = []

    # -- hooks de HTMLParser ------------------------------------------------ #
    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if tag in self._IGNORAR:
            self._ignorar += 1
            return
        if self._ignorar:
            return
        atributos = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self._procesar_meta(atributos)
            return
        if tag == "title":
            self._iniciar_captura("title")
            return
        if tag == "br":
            if self._captura:
                self._buffer.append(" ")
            return
        if tag in self._BLOQUES:
            self._iniciar_captura(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag in self._IGNORAR:
            if self._ignorar:
                self._ignorar -= 1
            return
        if self._ignorar:
            return
        if self._captura and tag == self._captura:
            self._cerrar_captura()

    def handle_data(self, data):
        if self._ignorar or not self._captura or not data:
            return
        self._buffer.append(data)

    def close(self):
        """Cierra el parser y captura un bloque que haya quedado abierto."""
        try:
            super().close()
        finally:
            if self._captura:
                self._cerrar_captura()

    # -- helpers internos --------------------------------------------------- #
    def _procesar_meta(self, atributos: dict):
        clave = (
            (atributos.get("property") or atributos.get("name") or "").strip().lower()
        )
        contenido = _limpiar_espacios(atributos.get("content") or "")
        if not contenido:
            return
        if clave == "og:title" and not self.titulo_og:
            self.titulo_og = contenido
        elif clave in ("description", "og:description") and not self.descripcion:
            self.descripcion = contenido

    def _iniciar_captura(self, tag: str):
        if self._captura:  # HTML mal formado: cerramos el bloque anterior
            self._cerrar_captura()
        self._captura = tag
        self._buffer = []

    def _cerrar_captura(self):
        tag = self._captura
        texto = _limpiar_espacios("".join(self._buffer))
        self._captura = ""
        self._buffer = []
        if not texto:
            return
        if tag == "title":
            if not self.titulo_documento:
                self.titulo_documento = texto
        elif tag == "h1":
            if not self.titulo_h1:
                self.titulo_h1 = texto
        elif tag == "p":
            self.parrafos.append(texto)


def extraer_noticia(url: str, timeout: int = 20) -> dict:
    """Descarga una noticia y devuelve ``{url, titulo, texto, ok, error}``.

    - Titulo: ``og:title`` -> ``<title>`` -> primer ``<h1>``.
    - Texto: parrafos ``<p>`` (>= 40 chars, sin repetidos); si no hay, usa
      ``og:description``/``meta description``.
    - Limita el texto a ~4000 caracteres.
    - Nunca lanza: ante fallo devuelve ``ok=False`` con un ``error`` corto.
    """
    resultado = {"url": url, "titulo": "", "texto": "", "ok": False, "error": ""}
    try:
        if not url or not str(url).lower().startswith(("http://", "https://")):
            resultado["error"] = "url invalida"
            return resultado

        html, error = _descargar_html(url, timeout=timeout)
        if error:
            resultado["error"] = error[:300]
            return resultado

        extractor = _ExtractorHTML()
        try:
            extractor.feed(html)
            extractor.close()
        except Exception as e:
            logger.debug(f"HTML mal formado en {url}: {_error_corto(e)}")

        titulo = (
            extractor.titulo_og
            or extractor.titulo_documento
            or extractor.titulo_h1
        )
        resultado["titulo"] = _limpiar_espacios(titulo)[:300]

        parrafos = []
        vistos = set()
        for bruto in extractor.parrafos:
            limpio = _limpiar_espacios(bruto)
            if len(limpio) < MIN_PARRAFO:
                continue
            clave = limpio.lower()
            if clave in vistos:
                continue
            vistos.add(clave)
            parrafos.append(limpio)

        texto = "\n".join(parrafos)
        if not texto:
            texto = _limpiar_espacios(extractor.descripcion)
        resultado["texto"] = texto[:MAX_TEXTO_NOTICIA].strip()

        if resultado["titulo"] or resultado["texto"]:
            resultado["ok"] = True
        else:
            resultado["error"] = "sin titulo ni texto"
    except Exception as e:
        resultado["error"] = _error_corto(e)
        logger.debug(f"extraer_noticia({url}) fallo: {_error_corto(e)}")
    return resultado


def extraer_noticias(
    links, timeout: int = 20, max_workers: int = 4
) -> list[dict]:
    """Extrae varias noticias en paralelo respetando el orden de entrada.

    Usa ``normalizar_links`` por defecto y un ``ThreadPoolExecutor``. Nunca
    lanza: los fallos se devuelven dentro del dict de cada noticia.
    """
    try:
        urls = normalizar_links(links)
    except Exception:
        return []
    if not urls:
        return []

    try:
        workers = max(1, int(max_workers))
    except (TypeError, ValueError):
        workers = 4

    resultados: list = [None] * len(urls)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futuros = {
                pool.submit(extraer_noticia, url, timeout): indice
                for indice, url in enumerate(urls)
            }
            for futuro in as_completed(futuros):
                indice = futuros[futuro]
                try:
                    resultados[indice] = futuro.result()
                except Exception as e:  # defensivo: extraer_noticia no lanza
                    resultados[indice] = {
                        "url": urls[indice],
                        "titulo": "",
                        "texto": "",
                        "ok": False,
                        "error": _error_corto(e),
                    }
    except Exception as e:  # pragma: no cover - defensivo
        logger.error(f"extraer_noticias fallo: {_error_corto(e)}")

    for indice, resultado in enumerate(resultados):
        if resultado is None:
            resultados[indice] = {
                "url": urls[indice],
                "titulo": "",
                "texto": "",
                "ok": False,
                "error": "sin resultado",
            }
    return resultados


# --------------------------------------------------------------------------- #
# 3) Contexto/briefing con IA
# --------------------------------------------------------------------------- #
def _llamar_ia(prompt: str, temperature: float = 0.4) -> str:
    """Llama a OpenAI (compatible con openai 0.28 y >= 1.0) y devuelve el texto.

    Replica el patron de ``ia/generador_contenido.py``. Propaga la excepcion
    para que el llamador aplique el fallback local.
    """
    if hasattr(openai, "OpenAI"):  # openai >= 1.0
        cliente = openai.OpenAI(api_key=settings.openai_api_key)
        respuesta = cliente.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return (respuesta.choices[0].message.content or "").strip()

    # openai 0.28.x
    openai.api_key = settings.openai_api_key
    respuesta = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
    )
    return (respuesta.choices[0].message.content or "").strip()


def _recortar(texto: str, limite: int) -> str:
    """Recorta un texto a ``limite`` caracteres buscando un cierre natural."""
    limpio = (texto or "").strip()
    if limite <= 0 or not limpio:
        return ""
    if len(limpio) <= limite:
        return limpio
    corte = limpio[:limite]
    for separador in (". ", ".\n", "! ", "? ", "\n"):
        posicion = corte.rfind(separador)
        if posicion >= int(limite * 0.6):
            return corte[: posicion + 1].strip()
    posicion = corte.rfind(" ")
    if posicion > 0:
        corte = corte[:posicion]
    return corte.strip()


def _construir_fallback(
    noticias: list[dict], texto_extra: str, max_caracteres: int
) -> str:
    """Fallback local: texto extra + 'Título: ...' + primeros chars de cada noticia."""
    partes = []
    extra = (texto_extra or "").strip()
    if extra:
        partes.append(extra)
    for noticia in noticias:
        titulo = (noticia.get("titulo") or "").strip() or "(sin titulo)"
        cuerpo = (noticia.get("texto") or "").strip()[:CHARS_FALLBACK_NOTICIA]
        partes.append(f"Título: {titulo}\n{cuerpo}".strip())
    return _recortar(
        "\n\n".join(p for p in partes if p), max_caracteres
    )


def _construir_prompt(
    noticias: list[dict], texto_extra: str, narrativa: str
) -> str:
    """Prompt en espanol que pide el briefing de 900-1500 caracteres."""
    lineas = [
        "Actúa como analista de actualidad para un equipo de redes sociales.",
        "",
        "A partir de las noticias y del contexto adicional que aparecen abajo, "
        "redacta UN solo texto de contexto en español, de entre 900 y 1500 "
        "caracteres, que sintetice los HECHOS clave (nombres propios, lugares, "
        "cifras y fechas) que después se usarán para generar publicaciones, "
        "comentarios y citas.",
        "",
        "Reglas:",
        "- No inventes datos: usa solo lo que aparece en las noticias o en el "
        "contexto adicional.",
        "- No uses markdown, viñetas, encabezados ni emojis; escribe prosa "
        "corrida.",
        "- No copies párrafos textuales: sintetiza con tus propias palabras.",
        "- Tono informativo, claro y neutral, apto para redes sociales.",
    ]
    narrativa_limpia = _limpiar_espacios(narrativa)
    if narrativa_limpia:
        lineas.append(
            f"- Respeta esta línea narrativa sin alterar los hechos: "
            f"{narrativa_limpia}"
        )

    extra = (texto_extra or "").strip()
    if extra:
        lineas += ["", "CONTEXTO ADICIONAL DEL USUARIO:", extra]

    if noticias:
        lineas += ["", "NOTICIAS:"]
        for indice, noticia in enumerate(noticias, 1):
            lineas.append(f"--- NOTICIA {indice} ---")
            lineas.append(f"Título: {noticia.get('titulo') or '(sin título)'}")
            cuerpo = (noticia.get("texto") or "").strip()
            if cuerpo:
                lineas.append(cuerpo[:MAX_TEXTO_PROMPT])

    lineas += [
        "",
        "Devuelve únicamente el texto del contexto, sin títulos, comillas ni "
        "comentarios extra.",
    ]
    return "\n".join(lineas)


def generar_contexto_desde_links(
    links,
    texto_extra: str = "",
    max_caracteres: int = 1800,
    narrativa: str = "",
) -> dict:
    """Scrapea los links y construye un contexto con IA (o fallback local).

    Retorno exacto::

        {"ok", "contexto", "fuentes", "errores", "links_usados",
         "links_duplicados", "resumen_ia", "ultimo_error"}

    - ``ok=True`` si hay contexto no vacío.
    - ``resumen_ia=True`` solo si el contexto vino de la IA.
    - Si no se pudo extraer ninguna noticia y no hay ``texto_extra``:
      ``ok=False``, ``contexto=""`` y el motivo en ``errores``.
    - Nunca lanza.
    """
    resultado = {
        "ok": False,
        "contexto": "",
        "fuentes": [],
        "errores": [],
        "links_usados": 0,
        "links_duplicados": 0,
        "resumen_ia": False,
        "ultimo_error": "",
    }
    try:
        extra = texto_extra if isinstance(texto_extra, str) else str(texto_extra or "")
        extra = extra.strip()

        try:
            limite = int(max_caracteres)
        except (TypeError, ValueError):
            limite = 1800
        if limite < 50:
            limite = 50

        crudos = _extraer_urls(links)
        unicos, duplicados = _deduplicar_con_conteo(crudos)
        resultado["links_usados"] = len(unicos)
        resultado["links_duplicados"] = duplicados

        noticias = extraer_noticias(unicos) if unicos else []
        ok_noticias = []
        for noticia in noticias:
            if noticia.get("ok"):
                ok_noticias.append(noticia)
                resultado["fuentes"].append(
                    {
                        "url": noticia.get("url", ""),
                        "titulo": noticia.get("titulo", ""),
                    }
                )
            else:
                resultado["errores"].append(
                    f"{noticia.get('url', '')}: "
                    f"{noticia.get('error') or 'sin detalle'}"
                )

        if not ok_noticias and not extra:
            resultado["errores"].append(
                "No se pudo extraer ninguna noticia y no se recibió texto de "
                "contexto."
            )
            resultado["ultimo_error"] = "Sin contenido para construir el contexto."
            return resultado

        respaldo = _construir_fallback(ok_noticias, extra, limite)

        contexto_ia = ""
        try:
            prompt = _construir_prompt(ok_noticias, extra, narrativa)
            contexto_ia = _llamar_ia(prompt, temperature=0.4)
        except Exception as e:
            resultado["ultimo_error"] = _error_corto(e)
            logger.warning(
                "generar_contexto_desde_links: IA no disponible "
                f"({_error_corto(e)}); se usa el fallback local"
            )

        if contexto_ia:
            resultado["contexto"] = _recortar(contexto_ia, limite)
            resultado["resumen_ia"] = True
        elif respaldo:
            resultado["contexto"] = respaldo
            resultado["resumen_ia"] = False
            if resultado["ultimo_error"]:
                resultado["ultimo_error"] = (
                    f"{resultado['ultimo_error']} (fallback local usado)"
                )[:300]
            else:
                resultado["ultimo_error"] = (
                    "La IA devolvió un contexto vacío; se usó el fallback local."
                )

        resultado["ok"] = bool(resultado["contexto"].strip())
        return resultado
    except Exception as e:  # pragma: no cover - defensivo
        resultado["ultimo_error"] = _error_corto(e)
        resultado["errores"].append(f"Error inesperado: {_error_corto(e)}")
        resultado["ok"] = bool(str(resultado.get("contexto", "")).strip())
        return resultado
