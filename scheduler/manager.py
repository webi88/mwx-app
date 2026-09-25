from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from core.database import get_db_session
from core.models import Tarea
from scheduler import calentamiento
from scheduler.ejecutor import EjecutorTareas, _es_error_reintentable
from loguru import logger


#: Defaults de las alertas 24/7 (se leen de core.config.settings.alertas_*).
_DEFECTOS_ALERTAS = {
    "activo": 1,
    "intervalo_min": 180,
    "ventana_horas": 6,
    "resumen_diario": 1,
    "resumen_diario_hora": 22,
}


def config_alertas() -> dict:
    """Ajustes efectivos de las alertas 24/7 (nunca lanza; invalidos -> default).

    Lee `core.config.settings` (campos `alertas_*`, env `ALERTAS_*`). Devuelve
    ``{"activo","intervalo_min","ventana_horas","resumen_diario",
    "resumen_diario_hora"}`` con:
      - `activo`/`resumen_diario`: bool (0/None/"abc" -> default 1);
      - `intervalo_min`: int >= 0 (0 = apagar el job);
      - `ventana_horas`: int >= 1 (horas hacia atras por busqueda);
      - `resumen_diario_hora`: int acotado a 0..23.
    El registro real del job depende ademas de `con_alertas=True`, que SOLO
    pasa `scheduler.standalone` (el dashboard nunca lo activa).
    """
    try:
        from core.config import settings

        def _int(nombre: str, defecto: int) -> int:
            try:
                return int(getattr(settings, nombre, defecto) or 0)
            except (TypeError, ValueError):
                return int(defecto)

        return {
            "activo": _int("alertas_activo", _DEFECTOS_ALERTAS["activo"]) != 0,
            "intervalo_min": _int(
                "alertas_intervalo_min", _DEFECTOS_ALERTAS["intervalo_min"]
            ),
            "ventana_horas": max(
                1, _int("alertas_ventana_horas", _DEFECTOS_ALERTAS["ventana_horas"])
            ),
            "resumen_diario": _int(
                "alertas_resumen_diario", _DEFECTOS_ALERTAS["resumen_diario"]
            )
            != 0,
            "resumen_diario_hora": min(
                23,
                max(
                    0,
                    _int(
                        "alertas_resumen_diario_hora",
                        _DEFECTOS_ALERTAS["resumen_diario_hora"],
                    ),
                ),
            ),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Alertas: config invalida, uso defaults: {e}")
        return {
            "activo": bool(_DEFECTOS_ALERTAS["activo"]),
            "intervalo_min": int(_DEFECTOS_ALERTAS["intervalo_min"]),
            "ventana_horas": int(_DEFECTOS_ALERTAS["ventana_horas"]),
            "resumen_diario": bool(_DEFECTOS_ALERTAS["resumen_diario"]),
            "resumen_diario_hora": int(_DEFECTOS_ALERTAS["resumen_diario_hora"]),
        }


class SchedulerManager:
    def __init__(
        self, con_calentamiento: bool = False, con_alertas: bool = False
    ):
        # max_instances=1: evita que _verificar_tareas se encime consigo mismo
        # (una corrida lenta con ejecuciones Selenium no debe solaparse con la
        # siguiente pasada del IntervalTrigger de 30s).
        #
        # `con_calentamiento` (default False): SOLO el proceso standalone
        # (python -m scheduler.standalone) lo activa con True. Las instancias
        # del dashboard (SchedulerManager() sin argumentos) NO registran el
        # calentamiento continuo para que no corra en 2+ procesos a la vez.
        #
        # `con_alertas` (default False): mismo gate para las alertas 24/7
        # (`alertas_periodicas` + `alertas_resumen_diario`). Si el dashboard lo
        # activara, cada pagina que instancia SchedulerManager() buscaria y
        # enviaria las MISMAS alertas a Telegram (envios duplicados).
        self.scheduler = BackgroundScheduler(max_instances=1)
        self.ejecutor = EjecutorTareas()
        self.con_calentamiento = bool(con_calentamiento)
        self.con_alertas = bool(con_alertas)
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
        if self.con_alertas:
            self._registrar_alertas()
        
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
    
    def _registrar_alertas(self):
        """Registra las alertas 24/7 (job periodico + resumen diario opcional).

        Solo se llama con `con_alertas=True` (lo pasa UNICAMENTE
        `scheduler/standalone.py`). Con `ALERTAS_ACTIVO=0` o
        `ALERTAS_INTERVALO_MIN=0` no se registra NADA. El resumen diario se
        registra solo si `ALERTAS_RESUMEN_DIARIO=1`, a la hora local
        `ALERTAS_RESUMEN_DIARIO_HORA` (0-23). Tolerante a fallos: un error aqui
        nunca tumba el scheduler.
        """
        try:
            cfg = config_alertas()
            if not cfg["activo"]:
                logger.info("Alertas periodicas desactivadas (ALERTAS_ACTIVO=0)")
                return
            intervalo = int(cfg["intervalo_min"])
            if intervalo <= 0:
                logger.info(
                    "Alertas periodicas desactivadas (ALERTAS_INTERVALO_MIN=0)"
                )
                return

            self.scheduler.add_job(
                self._alertas,
                trigger=IntervalTrigger(minutes=intervalo),
                id="alertas_periodicas",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                f"Alertas periodicas registradas (cada {intervalo} min; "
                f"ventana {int(cfg['ventana_horas'])} h)"
            )

            if cfg["resumen_diario"]:
                hora = int(cfg["resumen_diario_hora"])
                self.scheduler.add_job(
                    self._resumen_diario_alertas,
                    trigger=CronTrigger(hour=hora),
                    id="alertas_resumen_diario",
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                logger.info(
                    f"Resumen diario de alertas registrado (todos los dias {hora:02d}:00)"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"No se pudieron registrar las alertas 24/7: {e}")
    
    def _alertas(self):
        """Job periodico de alertas: jamas deja escapar una excepcion.

        Llama al motor con la ventana de `ALERTAS_VENTANA_HORAS`. Tolerante a
        motores viejos: si `enviar`/`horas` aun no existen o la firma cambia,
        cae a la llamada sin argumentos o se omite con log DEBUG.
        """
        try:
            from alertas.motor import MotorAlertas

            cfg = config_alertas()
            motor = MotorAlertas()
            ejecutar = getattr(motor, "ejecutar_alertas", None)
            if not callable(ejecutar):
                logger.debug("MotorAlertas.ejecutar_alertas no existe todavia; se omite")
                return
            try:
                resultado = ejecutar(horas=int(cfg["ventana_horas"]))
            except TypeError:
                # Firma vieja/sin kwargs: ejecutar con los defaults del motor.
                resultado = ejecutar()
            if isinstance(resultado, dict):
                logger.info(
                    "Alertas: "
                    f"total={resultado.get('total', 0)} "
                    f"enviadas={resultado.get('enviadas', 0)} "
                    f"duplicadas={resultado.get('duplicadas', 0)}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Error en las alertas periodicas: {e}")
    
    def _resumen_diario_alertas(self):
        """Job del resumen diario: tolerante si el motor aun no expone el metodo."""
        try:
            from alertas.motor import MotorAlertas

            motor = MotorAlertas()
            enviar = getattr(motor, "enviar_resumen_diario", None)
            if not callable(enviar):
                logger.debug(
                    "MotorAlertas.enviar_resumen_diario aun no existe; se omite"
                )
                return
            enviar()
            logger.info("Resumen diario de alertas enviado")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Error en el resumen diario de alertas: {e}")
    
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
