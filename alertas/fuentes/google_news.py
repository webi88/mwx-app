"""Fuente Google News RSS para el motor de alertas.

Portado del motor viejo (`GestorTwitter/logica_alertas.py`) manteniendo el
esqueleto del proyecto nuevo:

- Split de keywords principales (nombres/handles del cliente) vs genericas
  (temas), usando `cliente.num_principales` (fallback 10 como el viejo).
- Variantes de acento por keyword ("Óscar Rébora" -> tambien "Oscar Rebora"),
  porque Google News indexa de las dos formas.
- Operador `when:` calculado por ventana (24h->1d, <=72h->3d, <=168h->7d).
- Plataformas por Google News (`site:x.com`, `site:facebook.com`,
  `site:instagram.com`, `site:tiktok.com`) como el viejo. Configurable con el
  parametro `plataformas` o la env `ALERTAS_PLATAFORMAS`
  (ej. "google,twitter,facebook,instagram").
- Fecha fail-open: si falta `published_parsed` NO se descarta la entrada; solo
  se descarta si la fecha existe y quedo fuera de la ventana, comparando en
  UTC (tz correcto).
- URL cruda de Google News: la resuelve `utils.url_utils.resolver_url_google_news`.

Requiere `feedparser` y `requests` (ambos en requirements.txt).
"""

import html
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from loguru import logger


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Plataformas por defecto (paridad con el viejo auto_alertas.py).
_PLATAFORMAS_DEFAULT = ("google", "twitter", "facebook", "instagram")


def _plataformas_desde_env() -> dict:
    """Lee `ALERTAS_PLATAFORMAS` (coma-separado) con default razonable."""
    crudo = (os.getenv("ALERTAS_PLATAFORMAS") or "").strip()
    if crudo:
        activas = {p.strip().lower() for p in crudo.split(",") if p.strip()}
    else:
        activas = set(_PLATAFORMAS_DEFAULT)
    return {
        "google": "google" in activas or "web" in activas,
        "twitter": "twitter" in activas or "x" in activas,
        "facebook": "facebook" in activas or "fb" in activas,
        "instagram": "instagram" in activas or "ig" in activas,
        "tiktok": "tiktok" in activas,
    }


def _variantes_kw(kw: str) -> list[str]:
    """Variantes de una keyword: con acentos + sin acentos (como el viejo)."""
    limpio = (kw or "").strip()
    if not limpio:
        return []
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFKD", limpio)
        if not unicodedata.combining(c)
    )
    variantes = [limpio]
    if sin_acentos != limpio:
        variantes.append(sin_acentos)
    return variantes


def _when_para_horas(horas: int) -> str:
    """Operador `when:` segun la ventana pedida."""
    try:
        horas = int(horas)
    except (TypeError, ValueError):
        horas = 24
    if horas <= 24:
        return "when:1d"
    if horas <= 72:
        return "when:3d"
    if horas <= 168:
        return "when:7d"
    return f"when:{max(1, horas // 24)}d"


class GoogleNewsSource:
    """Busca menciones en Google News RSS (web + site: de redes)."""

    BASE_URL = "https://news.google.com/rss/search"
    MAX_ENTRADAS = 50
    MAX_WORKERS = 20

    def __init__(self):
        self.base_url = self.BASE_URL
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _UA})

    # ------------------------------------------------------------------ #
    # API principal
    # ------------------------------------------------------------------ #
    def buscar(
        self,
        keywords: list[str],
        localidad: str = "",
        horas: int = 24,
        num_principales: int = None,
        plataformas: dict = None,
    ) -> list[dict]:
        """Devuelve menciones normalizadas (nunca lanza).

        `num_principales`: cuantas de las primeras keywords son nombres/handles
        del cliente (las demas son temas genericos). Si no se pasa, 10 (como el
        viejo). `plataformas`: dict {"google","twitter","facebook","instagram",
        "tiktok"}; default = env ALERTAS_PLATAFORMAS o google+twitter+fb+ig.
        """
        keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
        if not keywords:
            return []

        plataformas = dict(plataformas) if plataformas is not None else _plataformas_desde_env()
        queries = self._construir_queries(keywords, localidad, horas, num_principales, plataformas)
        if not queries:
            return []

        menciones: list[dict] = []
        with ThreadPoolExecutor(max_workers=min(self.MAX_WORKERS, max(1, len(queries)))) as executor:
            futuros = [
                executor.submit(self._buscar_query, query, horas, fuente_label)
                for query, fuente_label in queries
            ]
            for futuro in futuros:
                try:
                    menciones.extend(futuro.result())
                except Exception as e:  # nunca tumbar la corrida por una query
                    logger.error(f"Error en busqueda Google News: {e}")

        return self._deduplicar(menciones)

    # ------------------------------------------------------------------ #
    # Construccion de queries
    # ------------------------------------------------------------------ #
    def _construir_queries(
        self,
        keywords: list[str],
        localidad: str,
        horas: int,
        num_principales: int = None,
        plataformas: dict = None,
    ) -> list[tuple]:
        plataformas = dict(plataformas) if plataformas is not None else _plataformas_desde_env()
        try:
            split = int(num_principales) if num_principales else 10
        except (TypeError, ValueError):
            split = 10
        if split <= 0:
            split = 10
        kw_especificas = keywords[:split]
        kw_genericas = keywords[split:]
        loc = (localidad or "").strip()
        when = _when_para_horas(horas)

        def _lotes(lista, n):
            for i in range(0, len(lista), n):
                yield lista[i:i + n]

        def _grupo_or(lote):
            variantes = []
            for kw in lote:
                for v in _variantes_kw(kw):
                    variantes.append(f'"{v}"')
            return "(" + " OR ".join(variantes) + ")"

        queries: list[tuple] = []

        # 1) Google (web/noticias): principales directas + localidad; genericas
        #    SOLO con localidad (como el viejo).
        if plataformas.get("google"):
            for lote in _lotes(kw_especificas, 3):
                grupo = _grupo_or(lote)
                queries.append((f"{grupo} {when}", "Google News"))
                if loc:
                    queries.append((f'{grupo} "{loc}" {when}', "Google News"))
            if kw_genericas and loc:
                for lote in _lotes(kw_genericas, 3):
                    queries.append((f'{_grupo_or(lote)} "{loc}" {when}', "Google News"))

        # 2) X/Twitter via Google News (site:x.com), incluidas menciones @handle.
        if plataformas.get("twitter"):
            for lote in _lotes(kw_especificas, 3):
                grupo = _grupo_or(lote)
                queries.append((f"{grupo} site:x.com", "X (Twitter)"))
                if loc:
                    queries.append((f'{grupo} "{loc}" site:x.com', "X (Twitter)"))
            handles = [k for k in kw_especificas if k.startswith("@")]
            for handle in handles:
                queries.append((f'"{handle}" site:x.com', "X (Twitter Menciones)"))
            if kw_genericas and loc:
                for lote in _lotes(kw_genericas, 3):
                    queries.append((f'{_grupo_or(lote)} "{loc}" site:x.com {when}', "X (Twitter)"))

        # 3) Facebook.
        if plataformas.get("facebook"):
            for lote in _lotes(kw_especificas, 3):
                grupo = _grupo_or(lote)
                queries.append((f"{grupo} site:facebook.com {when}", "Facebook"))
                if loc:
                    queries.append((f'{grupo} "{loc}" site:facebook.com {when}', "Facebook"))

        # 4) Instagram.
        if plataformas.get("instagram"):
            for lote in _lotes(kw_especificas, 3):
                grupo = _grupo_or(lote)
                queries.append((f"{grupo} site:instagram.com {when}", "Instagram"))
                if loc:
                    queries.append((f'{grupo} "{loc}" site:instagram.com {when}', "Instagram"))

        # 5) TikTok (todas las keywords, lotes de 5 como el viejo).
        if plataformas.get("tiktok"):
            for lote in _lotes(keywords, 5):
                queries.append((f"{_grupo_or(lote)} site:tiktok.com {when}", "TikTok"))

        return queries

    # ------------------------------------------------------------------ #
    # Ejecucion de una query
    # ------------------------------------------------------------------ #
    def _buscar_query(self, query: str, horas: int = 24, fuente_label: str = "Google News") -> list[dict]:
        menciones: list[dict] = []
        try:
            params = {
                "q": query,
                "hl": "es-419",
                "gl": "MX",
                "ceid": "MX:es-419",
            }
            response = self.session.get(self.base_url, params=params, timeout=30)

            if response.status_code != 200:
                logger.warning(f"Google News ({fuente_label}) HTTP {response.status_code}: {query[:80]}")
                return []

            feed = feedparser.parse(response.text)
            limite = datetime.now(timezone.utc) - timedelta(hours=horas)

            for entry in feed.entries[:self.MAX_ENTRADAS]:
                fecha_iso, fecha_dt = self._fecha_entrada(entry)
                # Fecha fail-open: solo descartar si existe y quedo fuera.
                if fecha_dt is not None and fecha_dt < limite:
                    continue

                enlace = entry.get("link", "")
                if not enlace:
                    continue

                source = entry.get("source", {})
                source_title = source.get("title", "") if isinstance(source, dict) else ""
                source_href = source.get("href", "") if isinstance(source, dict) else ""

                menciones.append({
                    "titulo": self._limpiar_titulo(entry.get("title", ""), source_title),
                    "resumen": self._extraer_resumen(entry, source_title),
                    "enlace": enlace,
                    "fuente": fuente_label,
                    "fuente_medio": source_title,
                    "fuente_url": source_href,
                    "fecha": fecha_iso,
                    "tipo": "google_news",
                })

            time.sleep(0.3)

        except requests.exceptions.Timeout:
            logger.warning(f"Timeout en RSS ({fuente_label}): {query[:80]}")
        except Exception as e:
            logger.error(f"Error busqueda Google News ({fuente_label}): {e}")

        return menciones

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fecha_entrada(entry) -> tuple:
        """(fecha_iso, datetime_utc) tolerante a feeds sin fecha.

        Si no hay fecha devuelve (None, None): la mencion NO se descarta.
        """
        parsed = getattr(entry, "published_parsed", None)
        if parsed:
            try:
                dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                return dt.isoformat(), dt
            except Exception:
                pass

        crudo = getattr(entry, "published", "") or ""
        if crudo:
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(crudo)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat(), dt
            except Exception:
                return crudo, None
        return None, None

    @staticmethod
    def _limpiar_titulo(titulo: str, source_title: str = "") -> str:
        titulo = (titulo or "").strip()
        if source_title:
            for sufijo in (f" - {source_title}", f" | {source_title}", f" – {source_title}"):
                if titulo.endswith(sufijo):
                    titulo = titulo[:-len(sufijo)].strip()
                    break
        return titulo

    @staticmethod
    def _extraer_resumen(entry, source_title: str = "") -> str:
        """Resumen de texto plano (sin HTML) de la entrada RSS."""
        resumen = entry.get("summary", entry.get("description", ""))
        if isinstance(resumen, list) and resumen:
            resumen = resumen[0].get("value", "") if isinstance(resumen[0], dict) else ""
        if not resumen:
            return ""
        resumen = re.sub(r"<[^>]+>", " ", str(resumen))
        resumen = html.unescape(resumen)
        resumen = " ".join(resumen.split())
        if len(resumen) > 400:
            resumen = resumen[:397].rsplit(" ", 1)[0] + "..."
        return resumen

    @staticmethod
    def _deduplicar(menciones: list[dict]) -> list[dict]:
        vistas = set()
        unicas = []
        for mencion in menciones:
            enlace = mencion.get("enlace", "")
            if enlace and enlace in vistas:
                continue
            if enlace:
                vistas.add(enlace)
            unicas.append(mencion)
        return unicas
