from core.models import Tarea, Cuenta
from core.database import get_db_session
from core.registro import marcar_cuenta_suspendida
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
    
    #: Tipos sociales que llevan pausa de cierre. El scheduler puede juntar
    #: varias tareas de la misma cuenta a la misma hora y no queremos rafagas.
    TIPOS_CON_PAUSA_CIERRE = ("post", "comentario", "retweet")

    def _ejecutar_accion(self, tarea: Tarea, cuenta: Cuenta) -> bool:
        try:
            from plataformas.base import PlataformaFactory
            bot = PlataformaFactory.crear_bot(cuenta.plataforma, cuenta.usuario)
            
            if not bot.login_con_cookies():
                logger.warning(f"Login fallido para {cuenta.usuario}")
                if getattr(bot, "cuenta_suspendida", False):
                    marcar_cuenta_suspendida(cuenta.usuario)
                    logger.warning(f"@{cuenta.usuario} marcada como suspendida (desactivada)")
                return False
            
            resultado = False
            
            if tarea.tipo == "post":
                if cuenta.plataforma == "twitter":
                    resultado = bot.publicar_tweet(tarea.contenido, tarea.imagen_path)
                else:
                    resultado = bot.publicar(tarea.contenido, tarea.imagen_path)
            
            elif tarea.tipo == "comentario":
                # contenido = {"url": ..., "texto": ...} (tolerante a texto plano).
                if cuenta.plataforma == "twitter":
                    url, texto = self._parsear_comentario(tarea.contenido)
                    if not url or not texto:
                        logger.warning(
                            f"Tarea {tarea.id} de comentario sin url/texto validos: "
                            f"{tarea.contenido!r}"
                        )
                    else:
                        resultado = bool(bot.responder_tweet(url, texto))
                # Pausa despues de responder (patron humano del bot).
                time.sleep(random.uniform(3.0, 8.0))
            
            elif tarea.tipo == "retweet":
                # Cada tarea ejecuta UN retweet: la PRIMERA URL de la lista.
                # El planner reparte una URL por tarea; si el contenido trae
                # varias (formato viejo) se mantiene la compatibilidad haciendo
                # solo la primera.
                url = self._primera_url(tarea.contenido)
                if not url:
                    logger.warning(f"Tarea {tarea.id} de retweet sin URL valida")
                elif cuenta.plataforma == "twitter":
                    resultado = self._retwittear_una(bot, url, cuenta.usuario)
                elif hasattr(bot, "retweet"):
                    resultado = bool(bot.retweet(url))
            
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
            
            if getattr(bot, "cuenta_suspendida", False):
                marcar_cuenta_suspendida(cuenta.usuario)
                logger.warning(f"@{cuenta.usuario} marcada como suspendida (desactivada)")
            bot.cerrar()

            # Cierre suave SOLO para acciones sociales: evita rafagas cuando el
            # scheduler junta varias tareas de la misma cuenta a la misma hora.
            if tarea.tipo in self.TIPOS_CON_PAUSA_CIERRE:
                time.sleep(random.uniform(2.0, 15.0))
            
            return resultado
        
        except Exception as e:
            logger.error(f"Error ejecutando accion: {e}")
            return False

    @staticmethod
    def _parsear_comentario(contenido) -> tuple[str, str]:
        """Devuelve ``(url, texto)`` de ``tarea.contenido``.

        Acepta JSON ``{"url","texto"}``, el dict ya deserializado, una lista
        JSON (primera URL) o texto plano: si la primera linea empieza por
        http(s) se toma como URL y el resto como comentario; si no, todo es
        texto. Nunca lanza.
        """
        url = ""
        texto = ""
        try:
            if not contenido:
                return url, texto
            if isinstance(contenido, dict):
                return (
                    str(contenido.get("url") or "").strip(),
                    str(contenido.get("texto") or "").strip(),
                )
            crudo = str(contenido).strip()
            try:
                datos = json.loads(crudo)
            except (ValueError, TypeError):
                datos = None
            if isinstance(datos, dict):
                url = str(datos.get("url") or "").strip()
                texto = str(datos.get("texto") or "").strip()
                if url or texto:
                    return url, texto
            if isinstance(datos, list) and datos:
                return str(datos[0] or "").strip(), ""
            lineas = crudo.splitlines()
            primera = lineas[0].strip() if lineas else ""
            if primera.lower().startswith(("http://", "https://")):
                partes = primera.split(None, 1)
                url = partes[0]
                resto = partes[1] if len(partes) > 1 else ""
                texto = "\n".join(([resto] if resto else []) + lineas[1:]).strip()
            else:
                texto = crudo
        except Exception:
            pass
        return url, texto

    @staticmethod
    def _primera_url(contenido) -> str:
        """Extrae la primera URL de un contenido JSON/lista/plano. Nunca lanza."""
        try:
            if not contenido:
                return ""
            if isinstance(contenido, (list, tuple)):
                for valor in contenido:
                    valor = str(valor or "").strip()
                    if valor:
                        return valor
                return ""
            crudo = str(contenido).strip()
            try:
                datos = json.loads(crudo)
            except (ValueError, TypeError):
                datos = None
            if isinstance(datos, (list, tuple)):
                for valor in datos:
                    valor = str(valor or "").strip()
                    if valor:
                        return valor
                return ""
            if isinstance(datos, dict):
                return str(datos.get("url") or "").strip()
            if isinstance(datos, str):
                return datos.strip()
            return crudo.split(",")[0].strip()
        except Exception:
            return ""

    @staticmethod
    def _retwittear_una(bot, url: str, usuario: str) -> bool:
        """Hace UN retweet verificado con ``solo_retwittear``; fallback a
        ``retweet`` si el bot no expone el primero. Nunca lanza."""
        if hasattr(bot, "solo_retwittear"):
            resultado = bot.solo_retwittear([url], usuario)
            if isinstance(resultado, dict):
                return resultado.get("exitos", 0) > 0
            return bool(resultado)
        return bool(bot.retweet(url))
