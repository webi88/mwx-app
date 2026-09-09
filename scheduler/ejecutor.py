from core.models import Tarea, Cuenta
from core.database import get_db_session
import json
import time
import random
from loguru import logger


class EjecutorTareas:
    def ejecutar_tarea(self, tarea: Tarea) -> dict:
        logger.info(f"Ejecutando tarea {tarea.id}: {tarea.tipo} en {tarea.plataforma}")
        
        cuenta_ids = json.loads(tarea.cuentas_ids) if tarea.cuentas_ids else []
        
        if not cuenta_ids:
            logger.warning(f"Tarea {tarea.id} sin cuentas asignadas")
            return {"exitos": 0, "fallidos": 0}
        
        exitos = 0
        fallidos = 0
        
        for cuenta_id in cuenta_ids:
            try:
                with get_db_session() as db:
                    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
                    
                    if not cuenta or not cuenta.activa:
                        fallidos += 1
                        continue
                
                resultado = self._ejecutar_accion(tarea, cuenta)
                
                if resultado:
                    exitos += 1
                else:
                    fallidos += 1
                
                time.sleep(random.uniform(2.0, 4.0))
            
            except Exception as e:
                logger.error(f"Error en cuenta {cuenta_id}: {e}")
                fallidos += 1
        
        resultados = {"exitos": exitos, "fallidos": fallidos}
        logger.info(f"Tarea {tarea.id} completada: {exitos} exitos, {fallidos} fallidos")
        
        return resultados
    
    def _ejecutar_accion(self, tarea: Tarea, cuenta: Cuenta) -> bool:
        try:
            from plataformas.base import PlataformaFactory
            bot = PlataformaFactory.crear_bot(cuenta.plataforma, cuenta.usuario)
            
            if not bot.login_con_cookies():
                logger.warning(f"Login fallido para {cuenta.usuario}")
                return False
            
            resultado = False
            
            if tarea.tipo == "post":
                if cuenta.plataforma == "twitter":
                    resultado = bot.publicar_tweet(tarea.contenido, tarea.imagen_path)
                else:
                    resultado = bot.publicar(tarea.contenido, tarea.imagen_path)
            
            elif tarea.tipo == "retweet":
                if cuenta.plataforma == "twitter":
                    urls = json.loads(tarea.contenido) if tarea.contenido else []
                    for url in urls:
                        resultado = bot.retweet(url)
                        time.sleep(random.uniform(2.5, 6.0))
            
            elif tarea.tipo == "like":
                if cuenta.plataforma == "twitter":
                    urls = json.loads(tarea.contenido) if tarea.contenido else []
                    for url in urls:
                        resultado = bot.like(url)
                        time.sleep(random.uniform(2, 5))
            
            elif tarea.tipo == "visualizacion":
                bot.driver.get(tarea.contenido)
                from utils.humanizer import comportamiento_humano_visualizacion
                comportamiento_humano_visualizacion(bot.driver)
                resultado = True
            
            bot.cerrar()
            
            return resultado
        
        except Exception as e:
            logger.error(f"Error ejecutando accion: {e}")
            return False
