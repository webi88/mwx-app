from core.database import get_db_session
from core.models import AlertaHistorial
from datetime import datetime, timedelta
from loguru import logger


class Deduplicador:
    def __init__(self):
        self.dias_historial = 7
    
    def filtrar_duplicados(self, menciones: list[dict], cliente_id: int) -> list[dict]:
        urls_existentes = self._obtener_urls_existentes(cliente_id)
        
        resultado = []
        urls_vistas = set()
        
        for mencion in menciones:
            url = mencion.get("enlace", "")
            
            if not url:
                continue
            
            if url in urls_vistas:
                continue
            
            if url in urls_existentes:
                mencion["duplicada"] = True
            else:
                mencion["duplicada"] = False
            
            urls_vistas.add(url)
            resultado.append(mencion)
        
        return resultado
    
    def _obtener_urls_existentes(self, cliente_id: int) -> set:
        urls = set()
        
        try:
            fecha_limite = datetime.now() - timedelta(days=self.dias_historial)
            
            with get_db_session() as db:
                historial = db.query(AlertaHistorial).filter(
                    AlertaHistorial.cliente_id == cliente_id,
                    AlertaHistorial.fecha_envio >= fecha_limite
                ).all()
                
                for registro in historial:
                    urls.add(registro.url)
        except Exception as e:
            logger.error(f"Error obteniendo historial: {e}")
        
        return urls
    
    def guardar_historial(self, menciones: list[dict], cliente_id: int):
        try:
            with get_db_session() as db:
                for mencion in menciones:
                    url = mencion.get("enlace", "")
                    
                    if not url:
                        continue
                    
                    existente = db.query(AlertaHistorial).filter(
                        AlertaHistorial.cliente_id == cliente_id,
                        AlertaHistorial.url == url
                    ).first()
                    
                    if not existente:
                        registro = AlertaHistorial(
                            cliente_id=cliente_id,
                            url=url,
                            fuente=mencion.get("fuente", ""),
                            fecha_envio=datetime.now()
                        )
                        db.add(registro)
                
                db.commit()
                logger.info(f"Historial guardado: {len(menciones)} menciones")
        except Exception as e:
            logger.error(f"Error guardando historial: {e}")
    
    def limpiar_historial(self, dias: int = 30):
        try:
            fecha_limite = datetime.now() - timedelta(days=dias)
            
            with get_db_session() as db:
                eliminados = db.query(AlertaHistorial).filter(
                    AlertaHistorial.fecha_envio < fecha_limite
                ).delete()
                db.commit()
                
                logger.info(f"Historial limpiado: {eliminados} registros eliminados")
        except Exception as e:
            logger.error(f"Error limpiando historial: {e}")
