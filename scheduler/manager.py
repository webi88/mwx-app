from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from core.database import get_db_session
from core.models import Tarea
from scheduler import calentamiento
from scheduler.ejecutor import EjecutorTareas, _es_error_reintentable
from loguru import logger


class SchedulerManager:
    def __init__(self, con_calentamiento: bool = False):
        # max_instances=1: evita que _verificar_tareas se encime consigo mismo
        # (una corrida lenta con ejecuciones Selenium no debe solaparse con la
        # siguiente pasada del IntervalTrigger de 30s).
        #
        # `con_calentamiento` (default False): SOLO el proceso standalone
        # (python -m scheduler.standalone) lo activa con True. Las instancias
        # del dashboard (SchedulerManager() sin argumentos) NO registran el
        # calentamiento continuo para que no corra en 2+ procesos a la vez.
        self.scheduler = BackgroundScheduler(max_instances=1)
        self.ejecutor = EjecutorTareas()
        self.con_calentamiento = bool(con_calentamiento)
        self._iniciar()
    
    def _iniciar(self):
        self.scheduler.add_job(
            self._verificar_tareas,
            trigger=IntervalTrigger(seconds=30),
            id="verificar_tareas",
            replace_existing=True
        )
        if self.con_calentamiento:
            self._registrar_calentamiento()
        
        self.scheduler.start()
        logger.info("Scheduler iniciado")
    
    def _registrar_calentamiento(self):
        """Registra el calentamiento continuo (revisa cada 60s).

        El job solo publica cuando toca (ventana aleatoria de
        CALENTAMIENTO_MIN_MIN..CALENTAMIENTO_MAX_MIN minutos) y se pausa solo
        si hay una campana de activacion en curso. Con CALENTAMIENTO_ACTIVO=0
        no se registra. Tolerante a fallos: un error nunca tumba el scheduler.
        """
        try:
            if not calentamiento.config_calentamiento()["activo"]:
                logger.info(
                    "Calentamiento continuo desactivado (CALENTAMIENTO_ACTIVO=0)"
                )
                return
            self.scheduler.add_job(
                self._calentamiento,
                trigger=IntervalTrigger(seconds=60),
                id="calentamiento_continuo",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("Calentamiento continuo registrado (revisando cada 60s)")
        except Exception as e:
            logger.warning(f"No se pudo registrar el calentamiento continuo: {e}")
    
    def _calentamiento(self):
        """Wrapper del job de calentamiento: jamas deja escapar una excepcion."""
        try:
            calentamiento.ejecutar_tanda_si_toca()
        except Exception as e:
            logger.debug(f"Error en el calentamiento continuo: {e}")
    
    def _verificar_tareas(self):
        try:
            # Campana de activacion en curso: las tareas programadas se
            # difieren (quedan "pendiente") para no abrir Chrome mientras la
            # campana usa los navegadores; se ejecutan solas al terminar (el
            # marcador de campana caduca a los 90 min). No se marca nada como
            # fallido y un error aqui jamas escapa.
            if calentamiento.campana_activa():
                logger.debug(
                    "Tareas diferidas: hay una campana de activacion en curso"
                )
                return

            from datetime import datetime, timedelta
            
            with get_db_session() as db:
                tareas_pendientes = db.query(Tarea).filter(
                    Tarea.estado == "pendiente",
                    Tarea.fecha_hora <= datetime.now()
                ).all()
                
                for tarea in tareas_pendientes:
                    try:
                        tarea.estado = "ejecutando"
                        db.commit()
                        
                        resultado = self.ejecutor.ejecutar_tarea(tarea)
                        exitos, fallidos, motivos = self._leer_resultado(resultado)
                        
                        opciones = getattr(tarea, "opciones", None)
                        if not isinstance(opciones, dict):
                            opciones = {}
                        try:
                            intentos = int(opciones.get("intentos", 0) or 0)
                        except (TypeError, ValueError):
                            intentos = 0
                        
                        # Fallo SEGURO (nada se publico): reprogramar la MISMA
                        # tarea, maximo 2 reintentos, con un motivo persistido
                        # en `resultado` para poder auditar por que se repitio.
                        if (
                            exitos == 0
                            and fallidos > 0
                            and motivos
                            and all(_es_error_reintentable(m) for m in motivos)
                            and intentos < 2
                        ):
                            tarea.estado = "pendiente"
                            tarea.fecha_hora = datetime.now() + timedelta(minutes=10)
                            tarea.opciones = {**opciones, "intentos": intentos + 1}
                            tarea.resultado = (
                                f"reintento {intentos + 1}/2: {motivos[0][:180]}"
                            )
                            db.commit()
                            logger.info(
                                f"Tarea {tarea.id}: fallo reintentable, se reprograma "
                                f"(intento {intentos + 1}/2, {tarea.fecha_hora})"
                            )
                            continue
                        
                        tarea.estado = "completada" if exitos > 0 else "fallida"
                        detalle = f"{exitos} exitos, {fallidos} fallidos"
                        if motivos:
                            detalle += " | " + " | ".join(motivos[:3])
                        tarea.resultado = detalle[:300]
                        db.commit()
                    
                    except Exception as e:
                        logger.error(f"Error ejecutando tarea {tarea.id}: {e}")
                        tarea.estado = "fallida"
                        tarea.resultado = str(e)
                        db.commit()
        
        except Exception as e:
            logger.error(f"Error en _verificar_tareas: {e}")

    @staticmethod
    def _leer_resultado(resultado) -> tuple:
        """Normaliza el dict del ejecutor a `(exitos, fallidos, motivos)`.

        Tolera ejecutores viejos (sin "motivos") y resultados raros/
        no-dict (tests con fakes): en esos casos devuelve 0/0/[] sin lanzar.
        """
        try:
            datos = resultado if isinstance(resultado, dict) else {}
            exitos = int(datos.get("exitos", 0) or 0)
            fallidos = int(datos.get("fallidos", 0) or 0)
            crudos = datos.get("motivos") or []
            if isinstance(crudos, (list, tuple)):
                motivos = [str(m).strip() for m in crudos if str(m or "").strip()]
            else:
                texto = str(crudos).strip()
                motivos = [texto] if texto else []
            return exitos, fallidos, motivos
        except Exception:  # noqa: BLE001
            return 0, 0, []
    
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
    
    def detener(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Scheduler detenido")
