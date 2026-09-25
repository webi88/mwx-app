"""Filtros de alertas con IA (compatibles con openai 0.28 y >= 1.0).

Expone las piezas que usa el resto del proyecto:

- ``resumir(titulares)`` -> ``str``: resumen ejecutivo para el dashboard.
- ``agrupar_temas(titulares)`` -> ``list[dict]``: agrupacion tematica para
  reportes y para ``alertas/motor.py``.
- ``clasificar_menciones(menciones, keywords_principales, localidad)``:
  clasificador del motor de alertas (lotes <= 20 y **fail-open obligatorio**).

Todas las llamadas a OpenAI pasan por ``_chat`` (import perezoso del cliente:
``OpenAI(api_key=...)`` en openai >= 1.0 y ``openai.ChatCompletion.create`` en
openai 0.28.x). La API key se lee de ``OPENAI_API_KEY`` (.env/settings), nunca
esta hardcodeada, y en las pruebas ``_chat``/``_api_key`` pueden
monkeypatchearse para no gastar llamadas reales.
"""
import json
import os
import re
import unicodedata

import openai
from core.config import settings
from ia.prompts import get_prompt_agrupar_temas, get_prompt_resumen_ejecutivo
from loguru import logger


# --------------------------------------------------------------------------- #
# Clasificacion de menciones (contrato con `alertas/motor.py`)
# --------------------------------------------------------------------------- #

MAX_MENCIONES_POR_LOTE = 20
"""Maximo de menciones que se mandan a la IA por lote (contrato del motor)."""

_DESCARTAR = object()
"""Marcador interno: la IA respondio "N. NO" para esa mencion."""

_RE_LINEA_NUMERADA = re.compile(r"^\s*(\d+)\s*[.)\-:]\s*(.*)$")

_NO_RELEVANTE = {
    "no",
    "no_relevante",
    "no_es_relevante",
    "no_relevantes",
    "irrelevante",
    "descartar",
    "descartada",
    "descartado",
}

_SOLO_OK = {"ok", "si", "relevante", "sin_resumen"}

_PROMPT_CLASIFICADOR = (
    "Eres un filtro de alertas para monitoreo político, social y ambiental en "
    "México.\n"
    "Recibes una lista numerada de menciones (noticias y publicaciones de "
    "redes). Para CADA mención decide si es RELEVANTE o NO RELEVANTE.\n\n"
    "RELEVANTE = hay interés público o político en México: gobierno, "
    "funcionarios públicos, partidos, elecciones, políticas públicas, "
    "seguridad, crimen, justicia, medio ambiente, permisos, obra pública, "
    "servicios, economía pública, presupuesto, impuestos, emergencias, "
    "manifestaciones, derechos humanos y temas locales de la zona de interés.\n\n"
    "NO RELEVANTE (descártala):\n"
    "- Espectáculos, farándula, celebridades, influencers, chismes, vida "
    "privada o relaciones de famosos (bodas, romances, rupturas).\n"
    "- Noticias de otros países (Argentina, España, Estados Unidos, etc.) sin "
    "impacto directo en México.\n"
    "- Nombres de personas que NO correspondan a las palabras clave del "
    "monitoreo: por ejemplo, si la clave es \"Mia\", descarta a la cantante "
    "Mia o a cualquier homónimo sin relación con el tema.\n"
    "- Publicidad, promociones, ventas, contenido comercial, SEO o spam.\n"
    "- Deportes o entretenimiento SIN relación con autoridades, gobierno o "
    "interés público.\n\n"
    "FORMATO DE RESPUESTA (una sola línea por mención, sin markdown):\n"
    "- Relevante: \"N. <resumen de UNA frase, máximo 200 caracteres, basado "
    "SOLO en la información dada; no inventes datos>\"\n"
    "- No relevante: \"N. NO\"\n"
    "Responde TODAS las menciones, en orden, sin texto extra."
)


def _normalizar_texto(valor) -> str:
    """Minusculas y sin acentos, para comparar keywords de forma tolerante."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def _contiene_keyword(texto_normalizado: str, keyword) -> bool:
    """True si ``keyword`` aparece en el texto (sin acentos ni mayusculas).

    Las keywords de una sola palabra exigen límite de palabra para evitar
    falsos positivos (p. ej. "Mia" NO matchea "Miami"); las que traen espacios
    o símbolos (@, #) se buscan como subcadena.
    """
    kw = _normalizar_texto(keyword).strip()
    if not kw:
        return False
    variantes = [kw]
    if kw.startswith("@"):
        variantes.append(kw[1:])
    for variante in variantes:
        if not variante:
            continue
        if " " in variante or not variante.isalnum():
            if variante in texto_normalizado:
                return True
        else:
            patron = r"(?<![a-z0-9_])" + re.escape(variante) + r"(?![a-z0-9_])"
            if re.search(patron, texto_normalizado):
                return True
    return False


def _limpiar_resumen_ai(texto) -> str:
    """Limpia markdown/espacios del resumen de la IA y lo acota a 400 chars."""
    t = str(texto or "")
    t = t.replace("**", "").replace("*", "").replace("`", "")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 400:
        t = t[:397].rstrip() + "..."
    return t


def _es_no_relevante(contenido: str) -> bool:
    """True si el contenido equivale a un "NO" (variantes en espanol)."""
    t = _normalizar_texto(contenido).strip(" .;:!?¿¡-")
    return t.replace(" ", "_") in _NO_RELEVANTE


def _es_solo_ok(contenido: str) -> bool:
    """True si la IA marco la mencion como relevante pero sin resumen."""
    t = _normalizar_texto(contenido).strip(" .;:!?¿¡-")
    return t in _SOLO_OK


class FiltrosAlertas:
    def __init__(self):
        try:
            openai.api_key = self._api_key()
        except Exception:
            openai.api_key = ""

    # ------------------------------------------------------------------ #
    # Clave y llamada a OpenAI (compatible con openai 0.28 y >= 1.0)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _api_key() -> str:
        """Clave de OpenAI del entorno o de ``settings`` (nunca hardcodeada).

        Prioriza ``OPENAI_API_KEY`` del entorno EN EL MOMENTO de la llamada
        (facilita pruebas y despliegues) y, si no existe, cae a
        ``settings.openai_api_key`` (cargado del ``.env``). Nunca lanza.
        """
        try:
            clave = os.environ.get("OPENAI_API_KEY") or ""
        except Exception:
            clave = ""
        if not clave:
            try:
                clave = getattr(settings, "openai_api_key", "") or ""
            except Exception:
                clave = ""
        return str(clave).strip()

    def _chat(self, prompt: str, temperature: float = 0.3, timeout: float = 25.0) -> str:
        """Hace una llamada de chat y devuelve el texto (sin manejar errores).

        Compatible con openai 0.28 (``ChatCompletion.create``) y >= 1.0
        (``OpenAI(...).chat.completions.create``). ``timeout`` es el límite en
        segundos de la peticion (0/None lo desactiva). Propaga las excepciones
        para que cada metodo decida su fallback/fail-open.
        """
        api_key = self._api_key()

        if hasattr(openai, "OpenAI"):  # openai >= 1.0
            opciones_cliente = {"api_key": api_key}
            opciones_creacion = {}
            if timeout:
                opciones_cliente["timeout"] = timeout
                opciones_creacion["timeout"] = timeout
            client = openai.OpenAI(**opciones_cliente)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                **opciones_creacion,
            )
            return (response.choices[0].message.content or "").strip()

        # openai 0.28.x
        openai.api_key = api_key
        extra = {"request_timeout": timeout} if timeout else {}
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            **extra,
        )
        return (response.choices[0].message.content or "").strip()

    # ------------------------------------------------------------------ #
    # Agrupacion de temas
    # ------------------------------------------------------------------ #
    def agrupar_temas(self, titulares: list[str]) -> list[dict]:
        """Agrupa titulares en temas ``[{"titulo", "cantidad"}]`` (nunca lanza).

        Usa ``get_prompt_agrupar_temas()`` con ``_chat`` y cae al
        ``_agrupar_fallback`` local ante lista vacia, error de red o respuesta
        no parseable. Firma y tipo de retorno conservados para el dashboard y
        ``alertas/motor.py``.
        """
        limpios = self._normalizar_titulares(titulares)
        if not limpios:
            return []

        prompt = f"{get_prompt_agrupar_temas()}\n\nTITULARES:\n"
        for idx, titulo in enumerate(limpios, 1):
            prompt += f"{idx}. {titulo}\n"

        try:
            texto = self._limpiar_fences(self._chat(prompt, temperature=0.3))
            if texto:
                data = json.loads(texto)
                temas = data if isinstance(data, list) else data.get("temas", [])
                if isinstance(temas, list):
                    return temas
        except Exception as e:
            logger.error(f"Error agrupando temas: {e}")

        return self._agrupar_fallback(limpios)

    # ------------------------------------------------------------------ #
    # Resumen ejecutivo (usado por `web/operaciones/resumenes.py`)
    # ------------------------------------------------------------------ #
    def resumir(self, titulares: list[str]) -> str:
        """Devuelve un resumen ejecutivo breve de los titulares (nunca lanza).

        Firma exacta que espera la pagina "Resumenes Ejecutivos":
        ``FiltrosAlertas().resumir(textos)`` -> ``str``.

        Con OpenAI disponible (compatible con openai 0.28 y >= 1.0) pide el
        resumen con ``get_prompt_resumen_ejecutivo()`` a ``gpt-4o-mini`` con
        ``temperature=0.3`` y limpia los fences ``` de la respuesta. Ante lista
        vacia, respuesta vacia o cualquier error (API key ausente, red caida,
        etc.) cae al ``_resumen_fallback`` local, que no usa IA ni red y
        SIEMPRE devuelve un string util.
        """
        limpios = self._normalizar_titulares(titulares)
        if not limpios:
            return "No hay titulares disponibles para generar el resumen."

        prompt = f"{get_prompt_resumen_ejecutivo()}\n\nTITULARES:\n"
        for idx, titulo in enumerate(limpios, 1):
            prompt += f"{idx}. {titulo}\n"

        try:
            texto = self._limpiar_fences(self._chat(prompt, temperature=0.3))
            if texto:
                return texto
            logger.warning(
                "Resumen ejecutivo: OpenAI devolvio texto vacio; uso fallback local"
            )
        except Exception as e:
            logger.error(f"Error generando resumen ejecutivo: {e}")

        return self._resumen_fallback(limpios)

    # ------------------------------------------------------------------ #
    # Clasificacion de menciones (contrato con `alertas/motor.py`)
    # ------------------------------------------------------------------ #
    def clasificar_menciones(
        self,
        menciones: list[dict],
        keywords_principales: list[str] | None = None,
        localidad: str = "",
    ) -> list[dict]:
        """Clasifica menciones con IA y devuelve SOLO las relevantes.

        Contrato congelado con el motor de alertas:

        - Procesa en lotes de a lo sumo ``MAX_MENCIONES_POR_LOTE`` (20).
        - Devuelve las relevantes ENRIQUECIDAS (copias nuevas) con:
          * ``es_principal``: ``True`` si el texto matchea una keyword
            principal (solo se agrega si hay keywords).
          * ``kw_principal``: la keyword principal encontrada.
          * ``resumen_ai``: resumen de UNA frase cuando la IA lo proporciono.
        - **Fail-open obligatorio**: sin API key, timeout, error de red o lote
          cuya respuesta no se puede parsear, se devuelven las menciones de ese
          lote (o todas) tal cual, sin marcar y SIN lanzar jamás. La corrida de
          alertas nunca se queda sin menciones por culpa de la IA.
        - Si ``keywords_principales`` viene vacio, filtra igual pero no marca
          principales.
        - Tolera menciones sin ``titulo``/``resumen`` (incluso valores None).

        No muta las menciones recibidas.
        """
        lista = self._normalizar_menciones(menciones)
        if not lista:
            return []

        keywords = self._normalizar_keywords(keywords_principales)
        localidad = str(localidad or "").strip()

        if not self._api_key():
            logger.warning(
                "Clasificacion de alertas: sin OPENAI_API_KEY; fail-open "
                f"({len(lista)} mencion(es) sin marcar)"
            )
            return [self._clonar(m) for m in lista]

        resultados: list = []
        for inicio in range(0, len(lista), MAX_MENCIONES_POR_LOTE):
            lote = lista[inicio:inicio + MAX_MENCIONES_POR_LOTE]
            try:
                clasificadas = self._clasificar_lote(lote, keywords, localidad)
            except Exception as e:
                logger.warning(
                    "Clasificacion de alertas: lote fallo "
                    f"({str(e)[:200]}); fail-open con {len(lote)} mencion(es)"
                )
                clasificadas = None
            if clasificadas is None:
                resultados.extend(self._clonar(m) for m in lote)
            else:
                resultados.extend(clasificadas)
        return resultados

    def _clasificar_lote(self, lote: list, keywords: list[str], localidad: str):
        """Clasifica UN lote con IA.

        Devuelve la lista filtrada/ marcada, o ``None`` si el lote fallo (la
        respuesta no traia NINGUNA linea numerada valida), para que el llamador
        aplique el fail-open.
        """
        items = [self._formatear_mencion(i + 1, m) for i, m in enumerate(lote)]
        prompt = self._construir_prompt_clasificacion(items, keywords, localidad)
        respuesta = self._chat(prompt, temperature=0.2)
        vistos = self._parsear_respuesta_clasificacion(respuesta, len(lote))
        if not vistos:
            return None

        salida = []
        for indice, mencion in enumerate(lote, 1):
            if indice in vistos:
                resumen = vistos[indice]
                if resumen is _DESCARTAR:
                    continue
                salida.append(self._marcar(mencion, keywords, resumen))
            else:
                # La IA no se pronuncio por esta mencion: fail-open (se conserva).
                salida.append(self._clonar(mencion))
        return salida

    @staticmethod
    def _normalizar_menciones(menciones) -> list:
        """Acepta lista/tupla/generador, un dict suelto o un str (sin lanzar)."""
        if menciones is None:
            return []
        if isinstance(menciones, (dict, str)):
            return [menciones]
        try:
            return list(menciones)
        except TypeError:
            return []

    @staticmethod
    def _normalizar_keywords(keywords) -> list[str]:
        """Normaliza keywords a una lista de strings no vacios y unicos."""
        if keywords is None:
            return []
        if isinstance(keywords, str):
            keywords = [keywords]
        try:
            candidatas = list(keywords)
        except TypeError:
            return []
        limpias: list[str] = []
        for kw in candidatas:
            if kw is None:
                continue
            s = str(kw).strip()
            if s and s not in limpias:
                limpias.append(s)
        return limpias

    @staticmethod
    def _formatear_mencion(indice: int, mencion) -> str:
        """Arma la linea numerada que ve la IA (tolerante a campos ausentes)."""
        if not isinstance(mencion, dict):
            texto = str(mencion or "").strip() or "Sin contenido"
            return f"{indice}. {texto[:200]}"

        titulo = str(mencion.get("titulo") or mencion.get("texto") or "Sin titulo").strip()
        fuente = str(mencion.get("fuente") or "").strip()
        resumen = str(mencion.get("resumen") or mencion.get("descripcion") or "").strip()

        linea = f"{indice}. [{fuente}] {titulo[:200]}" if fuente else f"{indice}. {titulo[:200]}"
        if resumen:
            linea += f"\n   Contexto: {resumen[:200]}"
        return linea

    @staticmethod
    def _construir_prompt_clasificacion(items: list[str], keywords: list[str], localidad: str) -> str:
        """Prompt del clasificador + contexto (keywords, localidad) + items."""
        partes = [_PROMPT_CLASIFICADOR]
        if keywords:
            partes.append(
                "PALABRAS CLAVE PRINCIPALES DEL MONITOREO: " + ", ".join(keywords)
            )
        if localidad:
            partes.append(f"LOCALIDAD DE INTERES: {localidad}")
        partes.append("MENCIONES:\n" + "\n".join(items))
        return "\n\n".join(partes)

    @classmethod
    def _parsear_respuesta_clasificacion(cls, texto: str, total: int) -> dict:
        """Mapea ``{indice(1-based): resumen | "" | _DESCARTAR}`` de la IA.

        Solo acepta lineas numeradas dentro del rango del lote; ignora texto
        extra. Un dict vacio significa "respuesta inutilizable" (fail-open).
        """
        vistos: dict = {}
        for linea in str(texto or "").splitlines():
            match = _RE_LINEA_NUMERADA.match(linea.strip())
            if not match:
                continue
            try:
                indice = int(match.group(1))
            except (TypeError, ValueError):
                continue
            if indice < 1 or indice > total:
                continue
            contenido = _limpiar_resumen_ai(match.group(2))
            if _es_no_relevante(contenido):
                vistos[indice] = _DESCARTAR
            elif _es_solo_ok(contenido):
                vistos[indice] = ""
            elif contenido:
                vistos[indice] = contenido
        return vistos

    @classmethod
    def _marcar(cls, mencion, keywords: list[str], resumen_ai: str = ""):
        """Copia la mencion y agrega ``resumen_ai``/``es_principal``/``kw_principal``."""
        salida = cls._clonar(mencion)
        if not isinstance(salida, dict):
            return salida
        if resumen_ai:
            salida["resumen_ai"] = resumen_ai
        if keywords:
            encontrada = cls._keyword_principal(salida, keywords)
            salida["es_principal"] = bool(encontrada)
            if encontrada:
                salida["kw_principal"] = encontrada
        return salida

    @classmethod
    def _keyword_principal(cls, mencion: dict, keywords: list[str]) -> str:
        """Devuelve la primera keyword principal presente en la mencion (o "")."""
        textos = []
        for campo in (
            "titulo",
            "resumen",
            "texto",
            "contenido",
            "descripcion",
            "kw_principal",
        ):
            valor = mencion.get(campo)
            if valor:
                textos.append(str(valor))
        if not textos:
            return ""
        texto = _normalizar_texto(" \n ".join(textos))
        for kw in keywords:
            if _contiene_keyword(texto, kw):
                return str(kw).strip()
        return ""

    @staticmethod
    def _clonar(mencion):
        """Copia superficial de una mencion dict (para no mutar la original)."""
        if isinstance(mencion, dict):
            return dict(mencion)
        return mencion

    # ------------------------------------------------------------------ #
    # Utilidades internas
    # ------------------------------------------------------------------ #
    @staticmethod
    def _limpiar_fences(texto: str) -> str:
        """Quita los fences ``` (y la etiqueta de lenguaje) de la respuesta."""
        t = (texto or "").strip()
        if not t.startswith("```"):
            return t
        t = t[3:]
        salto = t.find("\n")
        if salto != -1:
            idioma = t[:salto].strip().lower()
            if idioma in ("", "markdown", "md", "texto", "text", "resumen", "plain", "json"):
                t = t[salto + 1:]
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
        return t.strip()

    @staticmethod
    def _normalizar_titulares(titulares) -> list[str]:
        """Convierte la entrada en una lista de titulares no vacios (sin lanzar).

        Acepta listas, tuplas, generadores, un unico string o incluso None;
        descarta elementos vacios y normaliza a ``str``.
        """
        if titulares is None or isinstance(titulares, str):
            titulares = [] if titulares is None else [titulares]
        try:
            iterable = list(titulares)
        except TypeError:
            return []
        limpios = []
        for t in iterable:
            if t is None:
                continue
            try:
                s = str(t).strip()
            except Exception:
                continue
            if s:
                limpios.append(s)
        return limpios

    def _resumen_fallback(self, titulares: list[str]) -> str:
        """Resumen local SIN IA y SIN red: cuenta, primeros titulares y temas.

        Nunca lanza: si algo falla (incluso el conteo de palabras) devuelve al
        menos un texto valido no vacio.
        """
        limpios = self._normalizar_titulares(titulares)
        total = len(limpios)
        if total == 0:
            return "No hay titulares disponibles para generar el resumen."

        plural = "es" if total != 1 else ""
        lineas = [f"Resumen ejecutivo ({total} titular{plural}):"]
        for idx, titulo in enumerate(limpios[:5], 1):
            lineas.append(f"{idx}. {titulo}")

        try:
            temas = self._agrupar_fallback(limpios)
        except Exception:
            temas = []
        if temas:
            principales = ", ".join(
                f"{t.get('titulo', '')} ({t.get('cantidad', 0)})"
                for t in temas[:5]
                if isinstance(t, dict) and t.get("titulo")
            )
            if principales:
                lineas.append(f"Temas con más presencia: {principales}.")

        restantes = total - 5
        if restantes > 0:
            lineas.append(
                f"... y {restantes} titular{'es' if restantes != 1 else ''} más."
            )
        return "\n".join(lineas)

    def _agrupar_fallback(self, titulares: list[str]) -> list[dict]:
        from collections import Counter

        palabras_clave = []
        for titulo in titulares:
            palabras = titulo.lower().split()
            palabras_clave.extend([p for p in palabras if len(p) > 4])

        conteo = Counter(palabras_clave).most_common(5)

        temas = []
        for palabra, cantidad in conteo:
            temas.append({"titulo": palabra, "cantidad": cantidad})

        return temas
