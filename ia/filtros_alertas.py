import openai
from core.config import settings
from ia.prompts import get_prompt_agrupar_temas, get_prompt_resumen_ejecutivo
from loguru import logger
import json


class FiltrosAlertas:
    def __init__(self):
        openai.api_key = settings.openai_api_key
    
    def agrupar_temas(self, titulares: list[str]) -> list[dict]:
        prompt = f"{get_prompt_agrupar_temas()}\n\nTITULARES:\n"
        for idx, titulo in enumerate(titulares, 1):
            prompt += f"{idx}. {titulo}\n"
        
        try:
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3
            )
            
            if response.choices[0].message.content:
                texto = response.choices[0].message.content.strip()
                if texto.startswith("```"):
                    texto = texto.split("```")[1]
                if texto.startswith("json"):
                    texto = texto[4:]
                
                data = json.loads(texto)
                return data.get("temas", [])
        
        except Exception as e:
            logger.error(f"Error agrupando temas: {e}")
        
        return self._agrupar_fallback(titulares)

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

    def _chat(self, prompt: str, temperature: float = 0.3) -> str:
        """Llamada de chat compatible con openai 0.28 y >= 1.0 (sin manejar
        errores: ``resumir`` decide el fallback)."""
        if hasattr(openai, "OpenAI"):  # openai >= 1.0
            client = openai.OpenAI(api_key=settings.openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip()

        # openai 0.28.x
        openai.api_key = settings.openai_api_key
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        return (response.choices[0].message.content or "").strip()

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
            if idioma in ("", "markdown", "md", "texto", "text", "resumen", "plain"):
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
