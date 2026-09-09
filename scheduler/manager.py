from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from core.database import get_db_session
from core.models import Tarea
from scheduler.ejecutor import EjecutorTareas
from loguru import logger


class SchedulerManager:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.ejecutor = EjecutorTareas()
        self._iniciar()
    
    def _iniciar(self):
        self.scheduler.add_job(
            self._verificar_tareas,
            trigger=IntervalTrigger(seconds=30),
            id="verificar_tareas",
            replace_existing=True
        )
        
        self.scheduler.start()
        logger.info("Scheduler iniciado")
    
    def _verificar_tareas(self):
        try:
            from datetime import datetime
            
            with get_db_session() as db:
                tareas_pendientes = db.query(Tarea).filter(
                    Tarea.estado == "pendiente",
                    Tarea.fecha_hora <= datetime.now()
                ).all()
                
                for tarea in tareas_pendientes:
                    try:
                        tarea.estado = "ejecutando"
                        db.commit()
                        
                        self.ejecutor.ejecutar_tarea(tarea)
                        
                        tarea.estado = "completada"
                        db.commit()
                    
                    except Exception as e:
                        logger.error(f"Error ejecutando tarea {tarea.id}: {e}")
                        tarea.estado = "fallida"
                        tarea.resultado = str(e)
                        db.commit()
        
        except Exception as e:
            logger.error(f"Error en _verificar_tareas: {e}")
    
    def programar_tarea(self, tarea: Tarea) -> bool:
        try:
            with get_db_session() as db:
                db.add(tarea)
                db.commit()
                logger.info(f"Tarea {tarea.id} programada para {tarea.fecha_hora}")
                return True
        except Exception as e:
            logger.error(f"Error programando tarea: {e}")
            return False
    
    def cancelar_tarea(self, tarea_id: int) -> bool:
        try:
            with get_db_session() as db:
                tarea = db.query(Tarea).filter(Tarea.id == tarea_id).first()
                if not tarea:
                    return False
                
                if tarea.estado in ["completada", "fallida"]:
                    return False
                
                tarea.estado = "cancelada"
                db.commit()
                logger.info(f"Tarea {tarea_id} cancelada")
                return True
        except Exception as e:
            logger.error(f"Error cancelando tarea: {e}")
            return False
    
    def obtener_tareas_pendientes(self) -> list[Tarea]:
        with get_db_session() as db:
            return db.query(Tarea).filter(
                Tarea.estado.in_(["pendiente", "ejecutando"])
            ).order_by(Tarea.fecha_hora).all()
    
    def detener(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler detenido")
