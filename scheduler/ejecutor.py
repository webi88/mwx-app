from core.models import Tarea, Cuenta
from core.database import get_db_session
from core.registro import marcar_cuenta_suspendida, registrar_accion
import json
import time
import random
from loguru import logger


#: Senales SEGURAS de reintento: implican que NADA se publico, asi que repetir
#: la tarea no puede duplicar contenido. Referencia (lista completa y politica
#: de exclusion): `activaciones/motor.py::_es_error_reintentable`. Se duplica
#: aqui a proposito para no importar el modulo pesado del motor en el scheduler.
_SENALES_REINTENTABLES = (
    "compositor de x no cargo",
    "compositor no disponible",
    "pagina de error de x",
    "something went wrong",
    "boton de retweet no encontrado",
    "boton responder no encontrado",
    "no se pudo escribir el texto en el editor",
    "boton post deshabilitado",
    "tab crashed",
    "err_proxy_connection_failed",
    "cannot connect to chrome",
    "can't start new thread",
    "connection refused",
)

#: Senales que NUNCA se reintentan (mismo criterio que `activaciones/motor.py`):
#: "X no confirmo" (el post pudo haberse publicado), rechazos de X, sesion
#: expirada, login fallido, anti-bot, respuestas limitadas y cuentas
#: suspendidas.
_SENALES_NO_REINTENTABLES = (
    "x no confirmo",
    "x no confirmó",
    "x rechazo",
    "x rechazó",
    "may not be allowed",
    "not allowed to perform",
    "limitada por x",
    "sesion de x expirada",
    "sesión de x expirada",
    "login fallido",
    "no se pudo iniciar sesion",
    "no se pudo iniciar sesión",
    "anti-bot",
    "anti bot",
    "cloudflare",
    "just a moment",
    "respuestas limitadas",
    "no permite respuestas",
    "suspendida",
    "suspendido",
)


def _es_error_reintentable(motivo) -> bool:
    """True si el fallo permite UN reintento sin riesgo de duplicar el post.

    Solo reintenta fallos donde NADA llego a publicarse: compositor/pagina de
    error de X, editor no escribible, boton Post deshabilitado, boton de
    RT/responder no encontrado y errores transitorios de driver. Excluye
    explicitamente lo que jamas debe repetirse: "X no confirmo", rechazos de
    X, sesion expirada, login fallido, anti-bot, respuestas limitadas y
    cuentas suspendidas. Nunca lanza.
    """
    try:
        texto = "" if motivo is None else str(motivo).lower()
    except Exception:  # noqa: BLE001
        return False
    if not texto.strip():
        return False
    if any(senal in texto for senal in _SENALES_NO_REINTENTABLES):
        return False
    return any(senal in texto for senal in _SENALES_REINTENTABLES)


class EjecutorTareas:
    def ejecutar_tarea(self, tarea: Tarea) -> dict:
        """Ejecuta la tarea en cada cuenta asignada.

        Devuelve ``{"exitos", "fallidos", "motivos"}``: `motivos` trae UN
        detalle por cuenta fallida (nunca vacio). Los campos historicos
        (`exitos`/`fallidos`) se conservan. Nunca lanza.
        """
        logger.info(f"Ejecutando tarea {tarea.id}: {tarea.tipo} en {tarea.plataforma}")
        
        cuenta_ids = json.loads(tarea.cuentas_ids) if tarea.cuentas_ids else []
        
        if not cuenta_ids:
            logger.warning(f"Tarea {tarea.id} sin cuentas asignadas")
            return {"exitos": 0, "fallidos": 0, "motivos": []}
        
        exitos = 0
        fallidos = 0
        motivos = []
        
        for cuenta_id in cuenta_ids:
            try:
                with get_db_session() as db:
                    cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
                    
                    if not cuenta or not cuenta.activa:
                        fallidos += 1
                        motivos.append(f"cuenta {cuenta_id} no existe o esta inactiva")
                        continue
                
                resultado = self._ejecutar_accion(tarea, cuenta)
                ok, motivo = self._normalizar_resultado(resultado)
                
                if ok:
                    exitos += 1
                else:
                    fallidos += 1
                    motivos.append(motivo or "fallo sin detalle")
                
                time.sleep(random.uniform(2.0, 4.0))
            
            except Exception as e:
                logger.error(f"Error en cuenta {cuenta_id}: {e}")
                fallidos += 1
                motivos.append(f"{type(e).__name__}: {e}")
        
        resultados = {"exitos": exitos, "fallidos": fallidos, "motivos": motivos}
        logger.info(f"Tarea {tarea.id} completada: {exitos} exitos, {fallidos} fallidos")
        
        return resultados

    @staticmethod
    def _normalizar_resultado(resultado) -> tuple:
        """Normaliza lo que devuelva `_ejecutar_accion` a `(ok, motivo)`.

        Acepta la tupla nueva `(ok, motivo)` y el bool legado (tests o
        subclases): ante un bool, `motivo` es "". Nunca lanza.
        """
        if isinstance(resultado, tuple):
            ok = bool(resultado[0]) if resultado else False
            motivo = str(resultado[1] or "") if len(resultado) > 1 else ""
            return ok, motivo
        return bool(resultado), ""

    
    #: Tipos sociales que llevan pausa de cierre. El scheduler puede juntar
    #: varias tareas de la misma cuenta a la misma hora y no queremos rafagas.
    TIPOS_CON_PAUSA_CIERRE = ("post", "comentario", "retweet")

    #: Acciones normalizadas que un Tier 3 (Métricas/Soporte) NO puede
    #: ejecutar: solo RT/likes. `core.registro.normalizar_rol_cuota` mapea
    #: "post"/"publicacion"/"mantenimiento"/"calentamiento"/"hilo" -> "hashtags".
    TIPOS_PROHIBIDOS_TIER3 = ("hashtags", "cita", "comentario")

    #: Tipos de tarea que son de ACTIVACION MASIVA: una cuenta PAUSADA
    #: (`Cuenta.pausada_activacion`) NO los ejecuta (se entrega a clientes).
    #: Los tipos de mantenimiento ("post", "mantenimiento", "calentamiento",
    #: "hilo", "visualizacion" y vacios/desconocidos) SI se permiten: la pausa
    #: solo aplica a la activacion.
    TIPOS_ACTIVACION_PAUSA = (
        "rt",
        "retweet",
        "repost",
        "cita",
        "quote",
        "comentario",
        "respuesta",
        "reply",
        "like",
    )

    @classmethod
    def _bloqueada_por_pausa(cls, cuenta, tipo) -> bool:
        """True si la cuenta esta pausada para activacion y la tarea es de activacion.

        Una cuenta PAUSADA (`pausada_activacion=True`) queda excluida de los
        tipos de `TIPOS_ACTIVACION_PAUSA` (rt/retweet/repost, cita/quote,
        comentario/respuesta/reply y like) SIN abrir navegador y SIN reintento;
        los tipos de MANTENIMIENTO (post/mantenimiento/calentamiento/hilo/
        visualizacion y vacios/desconocidos) siguen funcionando. Nunca lanza:
        ante cualquier fallo devuelve False (no bloquear de mas).
        """
        try:
            from core.pausas import esta_pausada

            if not esta_pausada(cuenta):
                return False
            return str(tipo or "").strip().lower() in cls.TIPOS_ACTIVACION_PAUSA
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _motivo_pausa(cuenta) -> str:
        """Motivo del bloqueo por pausa (jamas coincide con los reintentables)."""
        usuario = str(getattr(cuenta, "usuario", "") or "").strip()
        return f"⏸️ @{usuario} — pausada para activación (solo mantenimiento)"

    @classmethod
    def _bloqueada_por_tier(cls, tarea, cuenta) -> bool:
        """True si el tier de la cuenta prohibe el tipo de la tarea.

        Tier 3 (Métricas/Soporte) solo puede RT/likes: cualquier tarea que
        normalice a "hashtags" (post/publicacion/mantenimiento/calentamiento/
        hilo), "cita" o "comentario" queda bloqueada ANTES de crear el bot.
        Tier 2, Tier 1 y cuentas sin tier NO se bloquean aqui (el Tier 2 tiene
        su propio blindaje de posts en las campanas, no en el scheduler).
        Nunca lanza: ante cualquier fallo devuelve False (no bloquear de mas).
        """
        try:
            from core.registro import normalizar_rol_cuota
            from core.tiers import tier_de_cuenta

            if tier_de_cuenta(cuenta) != "tier3":
                return False
            return (
                normalizar_rol_cuota(getattr(tarea, "tipo", ""))
                in cls.TIPOS_PROHIBIDOS_TIER3
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _motivo_tier3(tarea) -> str:
        """Motivo del bloqueo por Tier 3 (jamas coincide con los reintentables)."""
        tipo = str(getattr(tarea, "tipo", "") or "")
        return (
            f"tier 3 (Métricas/Soporte): la accion '{tipo}' esta PROHIBIDA "
            f"(solo RT/likes); no se ejecuto"
        )

    def _ejecutar_accion(self, tarea: Tarea, cuenta: Cuenta) -> tuple:
        """Ejecuta UNA accion de la tarea en UNA cuenta.

        Devuelve `(ok, motivo)`: `motivo` queda vacio en exito y trae el
        detalle real del fallo en caso contrario (normalmente
        `bot.ultimo_error`). Registra SIEMPRE la accion en `core.registro`
        (exito/fallido) y garantiza `bot.cerrar()` incluso si algo lanza
        (antes una excepcion dejaba Chrome huerfano). Nunca lanza.

        TIER 3: post/hashtags, cita y comentario se cortan ANTES de crear el
        bot (sin abrir Chrome) y se registran como fallido con un motivo que
        NO es reintentable (`retweet`/`like`/`visualizacion` si pasan).

        PAUSA: las cuentas pausadas para activacion masiva se cortan ANTES de
        crear el bot y de la logica de tier SOLO en tareas de activacion
        (rt/retweet/repost, cita/quote, comentario/respuesta/reply y like); las
        de mantenimiento (post/mantenimiento/calentamiento/hilo/visualizacion y
        desconocidas) se ejecutan normal.
        """
        bot = None
        motivo = ""
        cerrado = False
        registrado = False
        try:
            if self._bloqueada_por_pausa(cuenta, getattr(tarea, "tipo", "")):
                motivo = self._motivo_pausa(cuenta)
                logger.warning(
                    f"Accion {tarea.tipo} de activacion OMITIDA para "
                    f"@{cuenta.usuario}: pausada para activacion (solo "
                    f"mantenimiento); no se abre navegador"
                )
                self._registrar(cuenta, tarea, False, None, motivo)
                registrado = True
                return False, motivo

            if self._bloqueada_por_tier(tarea, cuenta):
                motivo = self._motivo_tier3(tarea)
                logger.warning(
                    f"Accion {tarea.tipo} PROHIBIDA para @{cuenta.usuario} "
                    f"por su tier (Métricas/Soporte): no se abre navegador"
                )
                self._registrar(cuenta, tarea, False, None, motivo)
                registrado = True
                return False, motivo

            from plataformas.base import PlataformaFactory
            bot = PlataformaFactory.crear_bot(cuenta.plataforma, cuenta.usuario)
            
            if not bot.login_con_cookies():
                detalle = self._motivo_bot(bot)
                motivo = f"login fallido: {detalle}" if detalle else "login fallido"
                logger.warning(f"Login fallido para {cuenta.usuario}: {motivo}")
                if getattr(bot, "cuenta_suspendida", False):
                    marcar_cuenta_suspendida(cuenta.usuario)
                    logger.warning(f"@{cuenta.usuario} marcada como suspendida (desactivada)")
                self._registrar(cuenta, tarea, False, bot, motivo)
                registrado = True
                return False, motivo
            
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
                        motivo = "comentario sin url/texto validos"
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
                    motivo = "retweet sin URL valida"
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
            
            if not resultado and not motivo:
                motivo = self._motivo_bot(bot) or "sin detalle"
            if not resultado:
                logger.warning(
                    f"Accion {tarea.tipo} fallida para @{cuenta.usuario}: {motivo}"
                )
            
            self._registrar(cuenta, tarea, bool(resultado), bot, motivo)
            registrado = True
            
            try:
                bot.cerrar()
                cerrado = True
            except Exception as e:  # noqa: BLE001
                logger.debug(f"No se pudo cerrar el bot de @{cuenta.usuario}: {e}")

            # Cierre suave SOLO para acciones sociales: evita rafagas cuando el
            # scheduler junta varias tareas de la misma cuenta a la misma hora.
            if tarea.tipo in self.TIPOS_CON_PAUSA_CIERRE:
                time.sleep(random.uniform(2.0, 15.0))
            
            return bool(resultado), ("" if resultado else motivo)
        
        except Exception as e:
            motivo = f"{type(e).__name__}: {e}"
            logger.error(f"Error ejecutando accion: {motivo}")
            if not registrado:
                self._registrar(cuenta, tarea, False, bot, motivo)
            return False, motivo
        finally:
            if bot is not None and not cerrado:
                try:
                    bot.cerrar()
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"No se pudo cerrar el bot de @{cuenta.usuario}: {e}")

    @staticmethod
    def _motivo_bot(bot, defecto: str = "") -> str:
        """Detalle de `bot.ultimo_error`; `defecto` si viene vacio.

        El bot puede ser None o no exponer el atributo. Nunca lanza.
        """
        try:
            detalle = str(getattr(bot, "ultimo_error", "") or "").strip()
        except Exception:  # noqa: BLE001
            detalle = ""
        return detalle or defecto

    @staticmethod
    def _registrar(cuenta, tarea, ok: bool, bot, motivo: str) -> None:
        """Registra UNA accion en `core.registro` (un registro por cuenta).

        Exito -> estado "exito" con la URL publicada
        (`getattr(bot, "ultima_url_publicada", "")`). Fallo -> estado
        "fallido" con el motivo recortado a 400 chars. `registrar_accion`
        nunca lanza; por si acaso, tampoco este helper.
        """
        try:
            if ok:
                url = str(getattr(bot, "ultima_url_publicada", "") or "")
                registrar_accion(cuenta.usuario, tarea.tipo, "exito", url, "")
            else:
                registrar_accion(
                    cuenta.usuario, tarea.tipo, "fallido", "", str(motivo or "")[:400]
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"No se pudo registrar la accion de @{cuenta.usuario}: {e}")

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
