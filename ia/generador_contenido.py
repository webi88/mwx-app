import json
import re

import openai
from core.config import settings, resolver_ruta
from ia.prompts import (
    get_prompt_generico,
    get_prompt_verificado_ambiental,
    get_prompt_harfuch
)
from loguru import logger


class GeneradorContenido:
    def __init__(self):
        openai.api_key = settings.openai_api_key
    
    def generar_contenido(
        self,
        tipo: str,
        narrativa: str,
        entrenamiento: str = "",
        contexto: str = "",
        cantidad: int = 10
    ) -> list[str]:
        try:
            if tipo == "verificado":
                prompt = get_prompt_verificado_ambiental(contexto)
            elif tipo == "harfuch":
                prompt = get_prompt_harfuch(contexto)
            else:
                prompt = get_prompt_generico(narrativa, entrenamiento)
            
            if contexto:
                prompt += f"\n\nCONTEXTO ADICIONAL:\n{contexto}"
            
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7
            )
            
            if response.choices[0].message.content:
                text = response.choices[0].message.content
                posts = [p.strip() for p in text.split("---") if p.strip()]
                return posts[:cantidad]
            
            return []
        
        except Exception as e:
            logger.error(f"Error generando contenido: {e}")
            return []
    
    def generar_variaciones_masivas(
        self,
        base: str,
        cantidad: int,
        narrativa: str = "",
        entrenamiento: str = "",
    ) -> list[str]:
        """Genera EXACTAMENTE 'cantidad' versiones de cita distintas y unicas
        a partir de 'base' (mismo sentido/tono). Devuelve una lista de strings
        (max. 240 caracteres por texto, pedido en el prompt)."""
        try:
            prompt = (
                f"Genera EXACTAMENTE {cantidad} versiones distintas y únicas de la "
                "siguiente cita. Mantén el mismo sentido y tono, pero varía la "
                "redacción, el orden de las ideas y las palabras. Cada texto debe "
                "tener un MÁXIMO de 240 caracteres."
            )
            if narrativa:
                prompt += f"\n\nNARRATIVA GENERAL:\n{narrativa}"
            if entrenamiento:
                prompt += f"\n\nENTRENAMIENTO DEL CLIENTE:\n{entrenamiento}"
            prompt += (
                f"\n\nCITA BASE:\n{base}\n\n"
                "Responde ÚNICAMENTE con un array JSON de strings, por ejemplo: "
                '["texto1", "texto2", ...]. No incluyas numeración, prefijos ni '
                "texto adicional fuera del JSON."
            )

            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.9,
            )

            content = (response.choices[0].message.content or "").strip()

            # 1) Intentar parsear como JSON array de strings.
            textos = []
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    textos = [str(t).strip() for t in parsed if str(t).strip()]
            except Exception:
                # 2) Fallback: dividir por saltos de linea.
                textos = [line.strip() for line in content.splitlines() if line.strip()]

            # Filtrar vacios y quitar prefijos tipo "1.", "2.", "3)".
            limpios = []
            for t in textos:
                t = re.sub(r"^\s*\d+[.)\-]\s*", "", t).strip()
                if t:
                    limpios.append(t)

            return limpios[:cantidad]

        except Exception as e:
            logger.error(f"Error generando variaciones masivas: {e}")
            return []

    def generar_imagen(self, prompt: str) -> str:
        try:
            response = openai.Image.create(
                model="gpt-image-1",
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            
            data = response.data[0]
            output_path = resolver_ruta(f"data/temp/ai_generated_{abs(hash(prompt))}.png")
            
            import base64
            import os
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            if getattr(data, "b64_json", None):
                image_data = base64.b64decode(data.b64_json)
                with open(output_path, "wb") as f:
                    f.write(image_data)
            elif getattr(data, "url", None):
                import requests
                resp = requests.get(data.url)
                with open(output_path, "wb") as f:
                    f.write(resp.content)
            else:
                logger.error("La respuesta no trae imagen (ni b64_json ni url)")
                return None
            
            return output_path
        
        except Exception as e:
            logger.error(f"Error generando imagen: {e}")
            return None
