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
import os
import random
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from core.database import get_db_session
from core.models import Cuenta
from core.config import settings, resolver_ruta
from core.roles import normalizar_rol_activacion
from activaciones.variaciones import generar_pool_variaciones_openai, variar_texto
from core.registro import registrar_accion


def _parsear_tokens(valor: str) -> list[str]:
    """Separa hashtags/menciones por comas, espacios o saltos de linea."""
    if not valor:
        return []
    return [t for t in re.split(r"[,\s]+", str(valor).strip()) if t]


def _normalizar_hashtags(valor: str) -> list[str]:
    """Devuelve hashtags con '#' garantizado, sin duplicados ni vacios."""
    tags, vistos = [], set()
    for token in _parsear_tokens(valor):
        token = token.strip()
        if not token:
            continue
        if not token.startswith("#"):
            token = "#" + token.lstrip("@")
        clave = token.lower()
        if clave not in vistos:
            vistos.add(clave)
            tags.append(token)
    return tags


def _normalizar_menciones(valor: str) -> list[str]:
    """Devuelve menciones con '@' garantizado, sin duplicados ni vacios."""
    menciones, vistos = [], set()
    for token in _parsear_tokens(valor):
        token = token.strip().lstrip("@")
        if not token:
            continue
        token = "@" + token
        clave = token.lower()
        if clave not in vistos:
            vistos.add(clave)
            menciones.append(token)
    return menciones


MENSAJE_SIN_SESION = (
    "sin sesión: sin .pkl ni cookies_json/auth_token; "
    "brandea o carga cookies antes de activar"
)


def _sugerencia_sesion(n: int) -> str:
    """Accion sugerida para las cuentas filtradas por falta de sesion."""
    return (
        f"{n} cuenta(s) sin sesión: no tienen .pkl en "
        "data/cookies/twitter/ ni cookies_json/auth_token en la BD. "
        "Brandéalas (login manual) o importa el lote con auth_token/cookies "
        "antes de activar; se saltaron sin abrir navegador."
    )


def _tiene_credencial_sesion(cuenta) -> bool:
    """True si la cuenta tiene alguna credencial de sesion usable.

    Revisa, sin abrir ningun navegador: archivo .pkl en disco,
    `cookies_json` con contenido y `auth_token` no vacio en la BD.
    """
    usuario = (getattr(cuenta, "usuario", "") or "").strip()
    if usuario:
        try:
            if os.path.exists(resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")):
                return True
        except Exception:
            pass
    cookies = getattr(cuenta, "cookies_json", None)
    if isinstance(cookies, list):
        if len(cookies) > 0:
            return True
    elif isinstance(cookies, str):
        if cookies.strip() and cookies.strip().lower() not in ("[]", "null", "none"):
            return True
    elif cookies:
        return True
    auth = getattr(cuenta, "auth_token", "")
    if isinstance(auth, str):
        return bool(auth.strip())
    return bool(auth)


def _partir_por_sesion(cuentas: list) -> tuple:
    """Separa cuentas con credencial de sesion de las que no tienen ninguna."""
    con, sin = [], []
    for cuenta in (cuentas or []):
        (con if _tiene_credencial_sesion(cuenta) else sin).append(cuenta)
    return con, sin


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

    def _obtener_cuentas_por_rol(self, usuarios: list | None = None,
                                 solo_roles: list | None = None) -> list[Cuenta]:
        """Cuentas twitter activas filtradas por usuarios y/o roles.

        Reutiliza `_obtener_cuentas` (sin modificarlo) y agrega los filtros:
        - usuarios: limita a esos nombres de usuario (ignora '@' y mayusculas).
        - solo_roles: limita a los roles normalizados indicados (core/roles.py).
        """
        cuentas = self._obtener_cuentas()

        if usuarios:
            deseados = {
                str(u).strip().lstrip("@").lower()
                for u in usuarios
                if str(u).strip()
            }
            cuentas = [
                c for c in cuentas
                if (c.usuario or "").strip().lstrip("@").lower() in deseados
            ]

        if solo_roles:
            permitidos = {normalizar_rol_activacion(r) for r in solo_roles}
            permitidos.discard("")
            if not permitidos:
                return []
            cuentas = [
                c for c in cuentas
                if normalizar_rol_activacion(c.rol_activacion) in permitidos
            ]

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

    def _pool_hashtags(self, texto_base: str, hashtags: str,
                       menciones: str, cantidad: int) -> list[str]:
        """Arma 'cantidad' textos combinando texto_base + hashtags + menciones.

        - Acepta hashtags separados por espacios o comas (con o sin '#').
        - Acepta menciones como "@a @b" o "a,b" (con o sin '@').
        - Varía el subconjunto y el orden de hashtags/menciones por cuenta y
          aplica variaciones locales al texto base para no repetir contenido.
        - Siempre devuelve exactamente 'cantidad' textos (rellena con sufijo
          numerado si no alcanza la variedad).
        """
        if cantidad <= 0:
            return []

        tags = _normalizar_hashtags(hashtags)
        menciones_norm = _normalizar_menciones(menciones)
        base = (texto_base or "").strip()

        if not base and not tags and not menciones_norm:
            return [""] * cantidad

        pool, vistos = [], set()
        intentos = 0
        max_intentos = max(cantidad * 25, 50)
        while len(pool) < cantidad and intentos < max_intentos:
            intentos += 1
            partes = []
            if base:
                # El primer texto conserva el original; el resto se varía.
                if not pool:
                    partes.append(base)
                else:
                    partes.append(variar_texto(base, n_hashtags=0))
            if tags:
                k = random.randint(1, len(tags))
                seleccion = random.sample(tags, k)
                random.shuffle(seleccion)
                partes.append(" ".join(seleccion))
            if menciones_norm:
                k = random.randint(1, len(menciones_norm))
                seleccion = random.sample(menciones_norm, k)
                random.shuffle(seleccion)
                partes.append(" ".join(seleccion))
            texto = "\n\n".join(p for p in partes if p).strip()
            if texto and texto not in vistos:
                vistos.add(texto)
                pool.append(texto)

        sufijo = 1
        while len(pool) < cantidad:
            base_txt = pool[-1] if pool else (
                base or " ".join(tags + menciones_norm)
            )
            pool.append(f"{base_txt} ({sufijo})")
            sufijo += 1
        return pool[:cantidad]

    def _quote_rt_una_cuenta(self, cuenta: Cuenta, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta el quote-RT para UNA cuenta con su propio navegador/proxy.

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada).
        """
        if not _tiene_credencial_sesion(cuenta):
            return (cuenta.usuario, False, MENSAJE_SIN_SESION[:120], "")
        if retardo > 0:
            time.sleep(retardo)
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            url = random.choice(urls)

            bot = TwitterBot(cuenta.usuario)
            if not bot.login_con_cookies():
                motivo = getattr(bot, "ultimo_error", "") or "login fallido"
                logger.warning(f"Login fallido para @{cuenta.usuario}: {motivo}")
                return (cuenta.usuario, False, motivo[:120], "")

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

    def _ejecutar_accion_rol(self, cuenta: Cuenta, rol: str, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta UNA accion segun el rol de activacion de la cuenta.

        - "cita": quote-RT con el texto asignado.
        - "hashtags": publica el texto con hashtags/menciones ("post" es alias).
        - "comentario": respuesta a un tweet (url + texto distintos por slot).
        - "rt": retweet simple (sin cita); puede dar like.

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url).
        Nunca lanza: cualquier error se reporta como fallo.
        """
        if not _tiene_credencial_sesion(cuenta):
            return (cuenta.usuario, rol, False, MENSAJE_SIN_SESION[:120], "")
        if retardo > 0:
            time.sleep(retardo)

        if rol == "post":
            rol = "hashtags"

        bot = None
        url_objetivo = ""
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            if rol in ("cita", "rt", "comentario"):
                url_objetivo = random.choice(urls) if urls else ""
                if not url_objetivo:
                    return (cuenta.usuario, rol, False, "sin URL objetivo", "")

            bot = TwitterBot(cuenta.usuario)
            if not bot.login_con_cookies():
                motivo = getattr(bot, "ultimo_error", "") or "login fallido"
                logger.warning(f"Login fallido para @{cuenta.usuario}: {motivo}")
                return (cuenta.usuario, rol, False, motivo[:120], url_objetivo)

            if rol == "cita":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    mensaje_cita=texto,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                urls_pub = res.get("urls") or []
                url_publicada = urls_pub[0] if urls_pub else url_objetivo
                detalle = "ok" if ok else (
                    getattr(bot, "ultimo_error", "") or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            if rol == "rt":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                detalle = "ok" if ok else (
                    getattr(bot, "ultimo_error", "") or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_objetivo)

            if rol == "comentario":
                if not (texto or "").strip():
                    return (cuenta.usuario, rol, False, "sin texto asignado", url_objetivo)
                responder = getattr(bot, "responder_tweet", None)
                if responder is None:
                    return (cuenta.usuario, rol, False, "sin soporte de respuesta", url_objetivo)
                ok = bool(responder(url_objetivo, texto))
                if ok:
                    detalle = "comentario publicado"
                else:
                    motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                    detalle = f"comentario: {motivo}"
                return (cuenta.usuario, rol, ok, detalle[:120], url_objetivo)

            # rol == "hashtags"
            res = bot.publicar_tweet(texto)
            ok = bool(res)
            if isinstance(res, str):
                url_publicada = res
            elif ok:
                url_publicada = getattr(bot, "ultima_url_publicada", "") or ""
            else:
                url_publicada = ""
            if ok:
                detalle = "hashtags publicados"
            else:
                motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                detalle = f"hashtags: {motivo}"
            return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario} (rol {rol}): {e}")
            return (
                cuenta.usuario, rol, False,
                f"{type(e).__name__}: {e}"[:120], url_objetivo,
            )
        finally:
            if bot is not None:
                try:
                    bot.cerrar()
                except Exception as e:
                    logger.warning(
                        f"No se pudo cerrar el navegador de @{cuenta.usuario}: {e}"
                    )

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
            return {
                "exitosas": 0, "fallidas": 0, "detalles": [], "total": 0,
                "sin_sesion": 0, "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
            }

        con_sesion, sin_sesion = _partir_por_sesion(cuentas)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia_sesion = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""

        resultados = [
            {"usuario": c.usuario, "ok": False,
             "detalle": MENSAJE_SIN_SESION[:120], "url": ""}
            for c in sin_sesion
        ]
        for cuenta in sin_sesion:
            with self._lock:
                self.progreso["hechas"] += 1
                self.progreso["fallidas"] += 1
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                MENSAJE_SIN_SESION[:120],
            )
            if callback:
                callback(
                    self.progreso["hechas"], len(cuentas),
                    cuenta.usuario, False,
                )
        if sin_sesion:
            logger.warning(
                f"Activacion: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). "
                f"{sugerencia_sesion}"
            )

        if not con_sesion:
            resumen_vacio = {
                "exitosas": self.progreso["exitosas"],
                "fallidas": self.progreso["fallidas"],
                "detalles": resultados,
                "total": len(cuentas),
                "sin_sesion": len(sin_sesion),
                "sin_sesion_usuarios": sin_sesion_usuarios,
                "sugerencia_sesion": sugerencia_sesion,
            }
            logger.info(
                f"Activacion finalizada: {resumen_vacio['exitosas']} exitosas, "
                f"{resumen_vacio['fallidas']} fallidas de {resumen_vacio['total']} "
                f"({resumen_vacio['sin_sesion']} sin sesión)"
            )
            return resumen_vacio

        pool = generar_pool_variaciones_openai(
            texto_base, cantidad=len(con_sesion), narrativa=narrativa, entrenamiento=entrenamiento
        )
        # Garantizar un texto unico por cuenta (ninguna cuenta comparte el mismo).
        random.shuffle(pool)
        asignaciones = {
            cuenta.usuario: (pool[i] if i < len(pool) else texto_base)
            for i, cuenta in enumerate(con_sesion)
        }
        bloques = self._distribuir_cohortes(con_sesion, duracion_min, cohortes)

        logger.info(
            f"Activacion masiva: {len(con_sesion)} cuentas con sesión "
            f"({len(sin_sesion)} sin sesión saltadas), {len(urls)} urls, "
            f"{cohortes} cohortes, {duracion_min} min, concurrencia {self.max_concurrente}"
        )

        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

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
            "sin_sesion": len(sin_sesion),
            "sin_sesion_usuarios": sin_sesion_usuarios,
            "sugerencia_sesion": sugerencia_sesion,
        }
        logger.info(
            f"Activacion finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_sesion']} sin sesión)"
        )
        return resumen

    def ejecutar_por_roles(
        self,
        urls: list[str],
        texto_base: str = "",
        hashtags: str = "",
        menciones: str = "",
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        usuarios: list | None = None,
        solo_roles: list | None = None,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
    ) -> dict:
        """Campaña masiva dividida en subcuentas por rol.

        - Carga cuentas twitter activas; si `usuarios` se pasa, limita a esos
          usuarios; si `solo_roles`, filtra a esos roles.
        - Agrupa por Cuenta.rol_activacion (normalizado con core/roles.py):
            * "cita": quote-RT con texto del pool (OpenAI + fallback local).
            * "hashtags": publica texto combinando base + hashtags + menciones.
            * "rt": retweet simple (con like opcional).
        - Cuentas SIN rol se saltan y se cuentan en `sin_rol`.
        - Cuentas SIN ninguna credencial de sesion (.pkl, cookies_json ni
          auth_token) se filtran antes de abrir navegadores y se cuentan en
          `sin_sesion` con la accion sugerida en `sugerencia_sesion`.
        - Cohortes temporales + delay aleatorio y concurrencia limitada,
          igual que `ejecutar()`.
        - Nunca lanza: cada cuenta fallida se reporta en `detalles`.
        """
        urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        cuentas = self._obtener_cuentas_por_rol(usuarios, solo_roles)

        grupos = {"cita": [], "hashtags": [], "rt": []}
        sin_rol_usuarios = []
        for cuenta in cuentas:
            rol = normalizar_rol_activacion(getattr(cuenta, "rol_activacion", ""))
            if rol in grupos:
                grupos[rol].append(cuenta)
            else:
                sin_rol_usuarios.append(cuenta.usuario)

        procesables = grupos["cita"] + grupos["hashtags"] + grupos["rt"]
        rol_de = {
            cuenta.usuario: normalizar_rol_activacion(
                getattr(cuenta, "rol_activacion", "")
            )
            for cuenta in procesables
        }
        ejecutables, sin_sesion = _partir_por_sesion(procesables)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia_sesion = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""
        grupos_ejec = {"cita": [], "hashtags": [], "rt": []}
        for cuenta in ejecutables:
            grupos_ejec[rol_de.get(cuenta.usuario, "")].append(cuenta)

        resumen = {
            "total": len(procesables),
            "exitosas": 0,
            "fallidas": 0,
            "sin_rol": len(sin_rol_usuarios),
            "sin_sesion": len(sin_sesion),
            "por_rol": {
                "cita": {
                    "total": len(grupos["cita"]), "exitosas": 0, "fallidas": 0,
                },
                "hashtags": {
                    "total": len(grupos["hashtags"]), "exitosas": 0, "fallidas": 0,
                },
                "rt": {
                    "total": len(grupos["rt"]), "exitosas": 0, "fallidas": 0,
                },
            },
            "detalles": [],
            "sin_rol_usuarios": sin_rol_usuarios,
            "sin_sesion_usuarios": sin_sesion_usuarios,
            "sugerencia_sesion": sugerencia_sesion,
        }
        for cuenta in sin_sesion:
            rol = rol_de.get(cuenta.usuario, "")
            resumen["fallidas"] += 1
            if rol in resumen["por_rol"]:
                resumen["por_rol"][rol]["fallidas"] += 1
            resumen["detalles"].append({
                "usuario": cuenta.usuario,
                "rol": rol,
                "ok": False,
                "detalle": MENSAJE_SIN_SESION[:120],
                "url": "",
            })

        if not procesables:
            logger.warning(
                "Activacion por roles: no hay cuentas con rol para ejecutar "
                f"({len(sin_rol_usuarios)} sin rol)"
            )
            return resumen

        if sin_sesion:
            logger.warning(
                f"Activacion por roles: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). "
                f"{sugerencia_sesion}"
            )

        if not ejecutables:
            logger.info(
                f"Activacion por roles finalizada: 0 exitosas, "
                f"{resumen['fallidas']} fallidas de {resumen['total']} "
                f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión)"
            )
            return resumen

        # --- Pool de textos por rol ---
        pool_cita = []
        if grupos_ejec["cita"]:
            pool_cita = generar_pool_variaciones_openai(
                texto_base,
                cantidad=len(grupos_ejec["cita"]),
                narrativa=narrativa,
                entrenamiento=entrenamiento,
            )
            random.shuffle(pool_cita)

        pool_hashtags = []
        if grupos_ejec["hashtags"]:
            pool_hashtags = self._pool_hashtags(
                texto_base, hashtags, menciones, len(grupos_ejec["hashtags"])
            )
            random.shuffle(pool_hashtags)

        asignaciones = {}
        for i, cuenta in enumerate(grupos_ejec["cita"]):
            asignaciones[cuenta.usuario] = (
                pool_cita[i] if i < len(pool_cita) else texto_base
            )
        for i, cuenta in enumerate(grupos_ejec["hashtags"]):
            asignaciones[cuenta.usuario] = (
                pool_hashtags[i] if i < len(pool_hashtags) else texto_base
            )
        for cuenta in grupos_ejec["rt"]:
            asignaciones[cuenta.usuario] = ""

        rol_por_usuario = {
            cuenta.usuario: rol_de.get(cuenta.usuario, "")
            for cuenta in ejecutables
        }

        bloques = self._distribuir_cohortes(ejecutables, duracion_min, cohortes)

        logger.info(
            f"Activacion por roles: {len(ejecutables)} cuentas con sesión "
            f"(cita={len(grupos_ejec['cita'])}, hashtags={len(grupos_ejec['hashtags'])}, "
            f"rt={len(grupos_ejec['rt'])}, sin_rol={len(sin_rol_usuarios)}, "
            f"sin_sesion={len(sin_sesion)}), "
            f"{len(urls)} urls, {cohortes} cohortes, {duracion_min} min, "
            f"concurrencia {self.max_concurrente}"
        )

        self.progreso = {
            "hechas": len(sin_sesion),
            "exitosas": 0,
            "fallidas": len(sin_sesion),
        }
        for cuenta in sin_sesion:
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                MENSAJE_SIN_SESION[:120],
            )
            if callback:
                callback(
                    self.progreso["hechas"], len(procesables),
                    cuenta.usuario, False,
                )
        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for idx, bloque in enumerate(bloques):
                for cuenta in bloque:
                    rol = rol_por_usuario.get(cuenta.usuario, "")
                    retardo = idx * intervalo_cohorte + random.uniform(0, 15)
                    futuros.append((retardo, pool_exec.submit(
                        self._ejecutar_accion_rol, cuenta, rol, urls,
                        asignaciones.get(cuenta.usuario, ""), dar_like, retardo,
                    ), cuenta.usuario))

            for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    usuario_res, rol_res, ok, detalle, url = futuro.result()
                except Exception as e:
                    usuario_res = usuario
                    rol_res = rol_por_usuario.get(usuario, "")
                    ok, detalle, url = False, str(e)[:80], ""

                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["exitosas" if ok else "fallidas"] += 1
                    resumen["exitosas" if ok else "fallidas"] += 1
                    if rol_res in resumen["por_rol"]:
                        resumen["por_rol"][rol_res][
                            "exitosas" if ok else "fallidas"
                        ] += 1
                    registrar_accion(
                        usuario_res,
                        "activacion",
                        "exito" if ok else "fallido",
                        url,
                        detalle,
                    )
                    resumen["detalles"].append({
                        "usuario": usuario_res,
                        "rol": rol_res,
                        "ok": ok,
                        "detalle": detalle,
                        "url": url,
                    })
                    if callback:
                        callback(
                            self.progreso["hechas"], len(procesables),
                            usuario_res, ok,
                        )

        logger.info(
            f"Activacion por roles finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión)"
        )
        return resumen

    def _ejecutar_campana_una_cuenta(self, cuenta: Cuenta, acciones: list,
                                     retardo: float = 0,
                                     dar_like: bool = False) -> list:
        """Ejecuta en secuencia las 9 acciones de campana de UNA cuenta.

        Cada elemento de ``acciones`` es ``(rol, urls, texto)`` con rol en
        ("hashtags", "comentario", "rt"). Delega cada slot en
        :meth:`_ejecutar_accion_rol` con una pausa corta entre acciones para
        no abrir navegadores en rafaga. Los slots sin texto (post/comentario)
        se reportan como fallo sin abrir navegador. Nunca lanza.
        """
        if retardo > 0:
            try:
                time.sleep(retardo)
            except Exception:
                pass
        resultados = []
        total = len(acciones or [])
        for indice, pieza in enumerate(acciones or []):
            try:
                if isinstance(pieza, dict):
                    rol = pieza.get("rol") or pieza.get("tipo") or ""
                    urls = pieza.get("urls") or pieza.get("url") or []
                    texto = pieza.get("texto") or ""
                else:
                    rol, urls, texto = pieza
                if isinstance(urls, str):
                    urls = [urls] if urls.strip() else []
                else:
                    try:
                        urls = [u for u in (urls or []) if str(u or "").strip()]
                    except TypeError:
                        urls = []
                if rol in ("hashtags", "comentario", "cita", "post"):
                    if not str(texto or "").strip():
                        url_ref = urls[0] if urls else ""
                        resultados.append(
                            (cuenta.usuario, rol, False, "sin texto asignado", url_ref)
                        )
                        continue
                resultados.append(
                    self._ejecutar_accion_rol(cuenta, rol, urls, texto or "", dar_like, 0)
                )
            except Exception as e:
                try:
                    rol_fallo = pieza[0] if not isinstance(pieza, dict) else "campana"
                except Exception:
                    rol_fallo = "campana"
                resultados.append(
                    (cuenta.usuario, rol_fallo, False,
                     f"{type(e).__name__}: {e}"[:120], "")
                )
            try:
                if indice < total - 1:
                    time.sleep(random.uniform(5.0, 15.0))
            except Exception:
                pass
        return resultados

    def ejecutar_campana_3_3_3(
        self,
        urls_rt: list[str],
        urls_comentarios: list[str] | None = None,
        textos_posts_por_cuenta: dict | None = None,
        textos_comentarios_por_cuenta: dict | None = None,
        usuarios: list | None = None,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        callback=None,
    ) -> dict:
        """Campana 3+3+3: 3 posts + 3 comentarios + 3 RTs por cuenta.

        Cada cuenta con sesion ejecuta 9 acciones con textos distintos por
        slot (los provee quien llama, normalmente
        ``ia.generar_pool_campana_por_cuenta``): los posts usan el rol
        "hashtags" de :meth:`_ejecutar_accion_rol`, los comentarios el rol
        "comentario" (respuesta con url + texto) y los RTs el rol "rt" sobre
        el tweet principal. Las cuentas sin ninguna credencial de sesion se
        filtran con el mismo criterio de :func:`_partir_por_sesion` sin abrir
        navegador y sus 9 slots se cuentan como fallidos con la sugerencia
        de sesion. El arranque se reparte en cohortes temporales con
        concurrencia limitada y cada cuenta corre sus 9 acciones en
        secuencia. No modifica :meth:`ejecutar` ni
        :meth:`ejecutar_por_roles`. Nunca lanza por cuenta: los fallos van
        en ``detalles``.
        """
        try:
            urls_rt_limpias = [
                str(u).strip() for u in (urls_rt or []) if str(u or "").strip()
            ]
        except Exception:
            urls_rt_limpias = []
        try:
            urls_com_limpias = [
                str(u).strip() for u in (urls_comentarios or []) if str(u or "").strip()
            ]
        except Exception:
            urls_com_limpias = []
        try:
            duracion = max(1, int(duracion_min or 60))
        except (TypeError, ValueError):
            duracion = 60
        try:
            n_cohortes = max(1, int(cohortes or 4))
        except (TypeError, ValueError):
            n_cohortes = 4
        pool_posts = textos_posts_por_cuenta if isinstance(textos_posts_por_cuenta, dict) else {}
        pool_coms = textos_comentarios_por_cuenta if isinstance(textos_comentarios_por_cuenta, dict) else {}

        cuentas = self._obtener_cuentas(cantidad_cuentas, tags, grupo)
        if usuarios:
            try:
                deseados = {
                    str(u).strip().lstrip("@").lower()
                    for u in usuarios if str(u or "").strip()
                }
            except Exception:
                deseados = set()
            cuentas = [
                c for c in cuentas
                if (c.usuario or "").strip().lstrip("@").lower() in deseados
            ]

        base_resumen = {
            "modo": "campana_3_3_3",
            "total": len(cuentas),
            "total_cuentas": len(cuentas),
            "total_acciones": len(cuentas) * 9,
            "exitosas": 0,
            "fallidas": 0,
            "por_tipo": {
                "post": {"total": 0, "exitosas": 0, "fallidas": 0},
                "comentario": {"total": 0, "exitosas": 0, "fallidas": 0},
                "retweet": {"total": 0, "exitosas": 0, "fallidas": 0},
            },
            "detalles": [],
            "sin_sesion": 0,
            "sin_sesion_usuarios": [],
            "sugerencia_sesion": "",
        }
        if not cuentas:
            logger.warning("Campana 3+3+3: no hay cuentas activas de twitter")
            return base_resumen

        con_sesion, sin_sesion = _partir_por_sesion(cuentas)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""
        base_resumen["sin_sesion"] = len(sin_sesion)
        base_resumen["sin_sesion_usuarios"] = sin_sesion_usuarios
        base_resumen["sugerencia_sesion"] = sugerencia

        mapa_tipo = {"hashtags": "post", "post": "post",
                     "comentario": "comentario", "rt": "retweet", "cita": "comentario"}
        self.progreso = {"hechas": 0, "exitosas": 0, "fallidas": 0}

        for cuenta in sin_sesion:
            for slot in range(9):
                if slot < 3:
                    rol_slot, tipo_slot = "hashtags", "post"
                elif slot < 6:
                    rol_slot, tipo_slot = "comentario", "comentario"
                else:
                    rol_slot, tipo_slot = "rt", "retweet"
                base_resumen["fallidas"] += 1
                base_resumen["por_tipo"][tipo_slot]["total"] += 1
                base_resumen["por_tipo"][tipo_slot]["fallidas"] += 1
                base_resumen["detalles"].append({
                    "usuario": cuenta.usuario,
                    "rol": rol_slot,
                    "tipo": tipo_slot,
                    "slot": slot % 3,
                    "ok": False,
                    "detalle": MENSAJE_SIN_SESION[:120],
                    "url": "",
                })
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["fallidas"] += 1
                registrar_accion(
                    cuenta.usuario, "campana_3_3_3", "fallido", "",
                    MENSAJE_SIN_SESION[:120],
                )
                if callback:
                    try:
                        callback(self.progreso["hechas"], base_resumen["total_acciones"],
                                 cuenta.usuario, False)
                    except Exception:
                        pass
        if sin_sesion:
            logger.warning(
                f"Campana 3+3+3: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). {sugerencia}"
            )
        if not con_sesion:
            logger.info(
                f"Campana 3+3+3 finalizada: 0 exitosas, "
                f"{base_resumen['fallidas']} fallidas de "
                f"{base_resumen['total_acciones']} ({base_resumen['sin_sesion']} sin sesión)"
            )
            return base_resumen

        acciones_por_cuenta: dict = {}
        for cuenta in con_sesion:
            try:
                brutos_post = pool_posts.get(cuenta.usuario, [])
                if isinstance(brutos_post, str):
                    brutos_post = [brutos_post]
                lista_post = [str(t or "").strip() for t in (brutos_post or [])]
                lista_post = [t for t in lista_post if t][:3]
            except Exception:
                lista_post = []
            try:
                brutos_com = pool_coms.get(cuenta.usuario, [])
                if isinstance(brutos_com, str):
                    brutos_com = [brutos_com]
                lista_com = [str(t or "").strip() for t in (brutos_com or [])]
                lista_com = [t for t in lista_com if t][:3]
            except Exception:
                lista_com = []
            slots: list = []
            for i in range(3):
                slots.append(("hashtags", [], lista_post[i] if i < len(lista_post) else ""))
            for i in range(3):
                url_com = urls_com_limpias[i % len(urls_com_limpias)] if urls_com_limpias else ""
                slots.append(("comentario", [url_com] if url_com else [],
                              lista_com[i] if i < len(lista_com) else ""))
            for i in range(3):
                url_rt = urls_rt_limpias[i % len(urls_rt_limpias)] if urls_rt_limpias else ""
                slots.append(("rt", [url_rt] if url_rt else [], ""))
            try:
                random.shuffle(slots)
            except Exception:
                pass
            acciones_por_cuenta[cuenta.usuario] = slots

        bloques = self._distribuir_cohortes(list(con_sesion), duracion, n_cohortes)
        intervalo = max(1, (duracion * 60) // max(n_cohortes, 1))
        retardos = {}
        for idx, bloque in enumerate(bloques):
            for cuenta in bloque:
                try:
                    retardos[cuenta.usuario] = idx * intervalo + random.uniform(0, 15)
                except Exception:
                    retardos[cuenta.usuario] = float(idx * intervalo)

        logger.info(
            f"Campana 3+3+3: {len(con_sesion)} cuentas con sesión "
            f"({len(sin_sesion)} sin sesión saltadas), "
            f"{len(urls_rt_limpias)} urls RT, {len(urls_com_limpias)} urls comentario, "
            f"{n_cohortes} cohortes, {duracion} min, concurrencia {self.max_concurrente}"
        )

        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for cuenta in con_sesion:
                futuros.append((
                    retardos.get(cuenta.usuario, 0.0),
                    pool_exec.submit(
                        self._ejecutar_campana_una_cuenta, cuenta,
                        acciones_por_cuenta.get(cuenta.usuario, []),
                        retardos.get(cuenta.usuario, 0.0), dar_like,
                    ),
                    cuenta.usuario,
                ))
            for _, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    resultados = futuro.result()
                except Exception as e:
                    resultados = [(usuario, "campana", False, str(e)[:120], "")]
                for usuario_res, rol_res, ok, detalle, url in resultados:
                    tipo = mapa_tipo.get(rol_res, rol_res)
                    if tipo not in base_resumen["por_tipo"]:
                        tipo = "post"
                    with self._lock:
                        self.progreso["hechas"] += 1
                        self.progreso["exitosas" if ok else "fallidas"] += 1
                        base_resumen["exitosas" if ok else "fallidas"] += 1
                        base_resumen["por_tipo"][tipo]["total"] += 1
                        base_resumen["por_tipo"][tipo]["exitosas" if ok else "fallidas"] += 1
                        registrar_accion(
                            usuario_res, "campana_3_3_3",
                            "exito" if ok else "fallido", url, detalle,
                        )
                        base_resumen["detalles"].append({
                            "usuario": usuario_res,
                            "rol": rol_res,
                            "tipo": tipo,
                            "ok": ok,
                            "detalle": detalle,
                            "url": url,
                        })
                        if callback:
                            try:
                                callback(self.progreso["hechas"],
                                         base_resumen["total_acciones"], usuario_res, ok)
                            except Exception:
                                pass

        logger.info(
            f"Campana 3+3+3 finalizada: {base_resumen['exitosas']} exitosas, "
            f"{base_resumen['fallidas']} fallidas de {base_resumen['total_acciones']} "
            f"({base_resumen['sin_sesion']} sin sesión)"
        )
        return base_resumen
