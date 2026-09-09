import openai
from core.config import settings, resolver_ruta
from loguru import logger
import base64
import os
import requests


class GeneradorImagenes:
    def __init__(self):
        openai.api_key = settings.openai_api_key
    
    def generar_imagen(self, prompt: str, modelo: str = "gpt-image-1") -> str:
        try:
            response = openai.Image.create(
                model=modelo,
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            
            data = response.data[0]
            output_path = resolver_ruta(f"data/temp/ai_image_{abs(hash(prompt)) % 10000}.png")
            
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            
            if getattr(data, "b64_json", None):
                image_data = base64.b64decode(data.b64_json)
                with open(output_path, "wb") as f:
                    f.write(image_data)
            elif getattr(data, "url", None):
                resp = requests.get(data.url)
                with open(output_path, "wb") as f:
                    f.write(resp.content)
            else:
                logger.error("La respuesta no trae imagen (ni b64_json ni url)")
                return None
            
            logger.info(f"Imagen generada: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"Error generando imagen: {e}")
            return None
    
    def texto_sobre_imagen(
        self,
        imagen_path: str,
        texto: str,
        output_path: str,
        posicion: str = "centro",
        color: str = "white"
    ) -> str:
        from PIL import Image, ImageDraw, ImageFont
        
        try:
            imagen = Image.open(imagen_path)
            draw = ImageDraw.Draw(imagen)
            
            try:
                fuente = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
            except:
                try:
                    fuente = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 36)
                except:
                    fuente = ImageFont.load_default()
            
            bbox = draw.textbbox((0, 0), texto, font=fuente)
            ancho_texto = bbox[2] - bbox[0]
            alto_texto = bbox[3] - bbox[1]
            
            ancho_imagen, alto_imagen = imagen.size
            
            if posicion == "centro":
                x = (ancho_imagen - ancho_texto) // 2
                y = (alto_imagen - alto_texto) // 2
            elif posicion == "arriba":
                x = (ancho_imagen - ancho_texto) // 2
                y = 50
            else:
                x = (ancho_imagen - ancho_texto) // 2
                y = alto_imagen - alto_texto - 50
            
            draw.text((x + 2, y + 2), texto, font=fuente, fill="black")
            draw.text((x, y), texto, font=fuente, fill=color)
            
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            imagen.save(output_path)
            
            return output_path
        
        except Exception as e:
            logger.error(f"Error superponiendo texto: {e}")
            return None
