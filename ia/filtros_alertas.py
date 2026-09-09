import openai
from core.config import settings
from ia.prompts import get_prompt_agrupar_temas
from loguru import logger
import json
import time


class FiltrosAlertas:
    def __init__(self):
        openai.api_key = settings.openai_api_key
    
    def filtrar_batch(self, items: list[dict]) -> list[dict]:
        resultados = []
        chunk_size = 25
        
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            
            texto = ""
            for idx, item in enumerate(chunk, 1):
                titulo = item.get("titulo", "")[:110]
                resumen = item.get("resumen", "")[:200]
                fuente = item.get("fuente", "Noticia")
                kw = item.get("kw_principal", "")
                
                texto += f"{idx}. [{fuente}] {titulo}\n"
                texto += f"   Contexto: {resumen}\n"
                texto += f"   Persona/tema clave: {kw}\n\n"
            
            prompt = f"{self.filtro_prompt}\n\nNOTICIAS A CLASIFICAR:\n{texto}"
            
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3
                )
                
                if response.choices[0].message.content:
                    lineas = response.choices[0].message.content.strip().split("\n")
                    
                    for linea in lineas:
                        linea = linea.strip()
                        if not linea:
                            continue
                        
                        try:
                            num = int(linea.split(".")[0])
                            contenido = ". ".join(linea.split(".")[1:]).strip()
                            
                            if num <= len(chunk):
                                item = chunk[num - 1]
                                
                                if "NO" in contenido.upper():
                                    item["filtrado"] = True
                                else:
                                    item["resumen_ai"] = contenido if contenido != "OK" else ""
                                    item["filtrado"] = False
                                
                                resultados.append(item)
                        except:
                            continue
                
                time.sleep(2)
            
            except Exception as e:
                logger.error(f"Error en filtro AI: {e}")
                for item in chunk:
                    item["filtrado"] = False
                    resultados.append(item)
        
        return resultados
    
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
