"""Motor de activacion masiva.

Orquesta quote-RTs aleatorizados sobre muchas cuentas a la vez, repartidas
en cohortes temporales para no disparar el anti-spam de X.

Caracteristicas:
- Concurrencia limitada de navegadores (configurable, default 15).
- Cada cuenta usa su propio proxy (guardado en la BD) y su cookie.
- Contenido variado por cuenta (pool de variaciones).
- Delay aleatorio entre acciones y arranque escalonado por cohortes.
- Headless para correr en Railway/VPS sin pantalla.
"""
import random
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from core.database import get_db_session
from core.models import Cuenta
from core.config import settings
from activaciones.variaciones import generar_pool_variaciones_openai
from core.registro import registrar_accion


class MotorActivacion:
    """Ejecuta una campaña de activacion (RT con cita) sobre N cuentas."""

    def __init__(self, max_concurrente: int = None):
        self.max_concurrente = max_concurrente or settings.max_browsers
        self._lock = threading.Lock()
        self.progreso = {"hechas": 0, "exitosas": 0, "fallidas": 0}

    def _obtener_cuentas(self, cantidad: int = None, tags: list[str] = None,
                         grupo: str = None) -> list[Cuenta]:
        with get_db_session() as db:
            query = db.query(Cuenta).filter(
                Cuenta.plataforma == "twitter",
                Cuenta.activa == True,
            )
            if grupo:
                query = query.filter(Cuenta.grupo == grupo)
            cuentas = query.all()

        if tags:
            cuentas = [c for c in cuentas if all(
                t.upper() in [x.strip().upper() for x in (c.tags or "").split(",") if x.strip()]
                for t in tags
            )]

        if cantidad:
            cuentas = cuentas[:cantidad]

        return cuentas

    def _distribuir_cohortes(self, cuentas: list[Cuenta], duracion_min: int,
                             cohortes: int) -> list[list[Cuenta]]:
        """Reparte las cuentas en 'cohortes' grupos con arranque escalonado
        a lo largo de la duracion total."""
        random.shuffle(cuentas)
        bloques = [[] for _ in range(cohortes)]
        for i, cuenta in enumerate(cuentas):
            bloques[i % cohortes].append(cuenta)
        return bloques

    def _quote_rt_una_cuenta(self, cuenta: Cuenta, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta el quote-RT para UNA cuenta con su propio navegador/proxy.

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada).
        """
        if retardo > 0:
            time.sleep(retardo)
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            url = random.choice(urls)

            bot = TwitterBot(cuenta.usuario)
            if not bot.login_con_cookies():
                logger.warning(f"Login fallido para @{cuenta.usuario}")
                return (cuenta.usuario, False, "login fallido", "")

            res = bot.solo_retwittear(
                [url],
                cuenta.usuario,
                mensaje_cita=texto,
                dar_like=dar_like,
            )
            bot.cerrar()

            ok = res.get("exitos", 0) > 0
            url_publicada = (res.get("urls") or [""])[0] if res.get("urls") else ""
            return (cuenta.usuario, ok, "ok" if ok else "sin exito", url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario}: {e}")
            return (cuenta.usuario, False, str(e)[:80], "")

    def ejecutar(
        self,
        urls: list[str],
        texto_base: str,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        n_hashtags: int = 2,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
    ) -> dict:
        """Lanza la campaña completa.

        - duracion_min: minutos que dura la activacion (default 60).
        - cohortes: en cuantos grupos temporales se reparten las cuentas.
        - narrativa: narrativa general de la celula (contexto para OpenAI).
        - entrenamiento: entrenamiento propio del cliente (contexto para OpenAI).
        """
        cuentas = self._obtener_cuentas(cantidad_cuentas, tags, grupo)
        if not cuentas:
            logger.warning("No hay cuentas activas de twitter para la activacion")
            return {"exitosas": 0, "fallidas": 0, "detalles": [], "total": 0}

        pool = generar_pool_variaciones_openai(
            texto_base, cantidad=len(cuentas), narrativa=narrativa, entrenamiento=entrenamiento
        )
        # Garantizar un texto unico por cuenta (ninguna cuenta comparte el mismo).
        random.shuffle(pool)
        asignaciones = {
            cuenta.usuario: (pool[i] if i < len(pool) else texto_base)
            for i, cuenta in enumerate(cuentas)
        }
        bloques = self._distribuir_cohortes(cuentas, duracion_min, cohortes)

        logger.info(
            f"Activacion masiva: {len(cuentas)} cuentas, {len(urls)} urls, "
            f"{cohortes} cohortes, {duracion_min} min, concurrencia {self.max_concurrente}"
        )

        resultados = []
        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

        # Programar tareas con retardo de arranque segun su cohorte.
        # El ThreadPoolExecutor ya limita la concurrencia; el retardo solo
        # distribuye los INICIOS a lo largo de la ventana para no disparar
        # todas las cuentas al mismo tiempo.
        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for idx, bloque in enumerate(bloques):
                for cuenta in bloque:
                    retardo = idx * intervalo_cohorte + random.uniform(0, 15)
                    futuros.append((retardo, pool_exec.submit(
                        self._quote_rt_una_cuenta, cuenta, urls,
                        asignaciones[cuenta.usuario], dar_like, retardo
                    ), cuenta.usuario))

            # Recoger resultados en orden de arranque (los retardos ya fueron
            # aplicados al momento de submit via scheduling natural del pool).
            for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    usuario_res, ok, detalle, url = futuro.result()
                except Exception as e:
                    usuario_res, ok, detalle, url = usuario, False, str(e)[:80], ""

                with self._lock:
                    self.progreso["hechas"] += 1
                    if ok:
                        self.progreso["exitosas"] += 1
                    else:
                        self.progreso["fallidas"] += 1
                    registrar_accion(
                        usuario_res,
                        "activacion",
                        "exito" if ok else "fallido",
                        url,
                        detalle,
                    )
                    resultados.append({"usuario": usuario_res, "ok": ok, "detalle": detalle, "url": url})
                    if callback:
                        callback(self.progreso["hechas"], len(cuentas), usuario_res, ok)

        resumen = {
            "exitosas": self.progreso["exitosas"],
            "fallidas": self.progreso["fallidas"],
            "detalles": resultados,
            "total": len(cuentas),
        }
        logger.info(
            f"Activacion finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']}"
        )
        return resumen
