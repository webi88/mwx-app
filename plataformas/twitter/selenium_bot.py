import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    InvalidElementStateException,
    InvalidSessionIdException,
    NoSuchDriverException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from urllib3.exceptions import MaxRetryError
import pickle
import time
import random
import os
import re
import hashlib
import threading
import unicodedata
from contextlib import contextmanager
from typing import Optional
from datetime import datetime, timedelta
from loguru import logger

from core.config import settings, resolver_ruta, detectar_chrome_version
from plataformas.chrome_driver import crear_chrome, _env_activo
from utils.proxies import ProxyManager
from utils.anti_detection import (
    aplicar_stealth,
    aplicar_user_agent,
    normalizar_cookies,
    resolver_ua_cuenta,
)


# --------------------------------------------------------------------------- #
# Cache de validacion de proxies contra x.com (por proceso)
# --------------------------------------------------------------------------- #
# `ProxyManager.x_accesible` hace un GET a x.com a traves del proxy (hasta 12s
# de timeout) y `_proxy_para_x` lo repetia en CADA accion: con la sesion sticky
# ya validada en la campana no hace falta volver a sondearla, pero si el proxy
# muere, el TTL corto hace que se vuelva a validar/rotar. Configurable con
# `PROXY_X_CACHE_SEG` (0 = desactivar la cache).
_PROXY_X_VALIDADOS: dict = {}
_PROXY_X_LOCK = threading.Lock()
_PROXY_X_CACHE_MAX = 2000


def _proxy_x_cache_segundos() -> float:
    """TTL en segundos de la cache de proxies validados (default 300s)."""
    try:
        return max(0.0, float(os.environ.get("PROXY_X_CACHE_SEG", "300")))
    except (TypeError, ValueError):
        return 300.0


def _env_float_seg(nombre: str, por_defecto: float) -> float:
    """Lee un float de entorno (segundos); usa el default si falta o es invalido.

    A diferencia de `max(0, ...)`, los negativos se devuelven tal cual: el
    llamador decide (p.ej. `<= 0` = desactivar la funcion). Nunca lanza.
    """
    try:
        valor = os.environ.get(nombre)
        if valor is None or not str(valor).strip():
            return float(por_defecto)
        return float(str(valor).strip())
    except Exception:
        return float(por_defecto)


@contextmanager
def _page_load_timeout_acotado(driver, timeout_max: float = 25.0):
    """Baja TEMPORALMENTE el `page_load_timeout` del driver y lo RESTAURA.

    `driver.get()`/`driver.refresh()` con la SPA de X atascada quemaban el
    timeout completo del bot (~60-70s; log real de Railway:
    `navegar_tolerante: ... ok en 70.7s tras refresh`). Aqui se guarda el valor
    previo y se pone `min(previo, timeout_max)`. La restauracion SIEMPRE ocurre
    en el `finally` (aunque el cuerpo lance). Nunca lanza por si mismo.

    Selenium expone el valor previo de dos formas segun la version:
    `driver.page_load_timeout` (propiedad vieja) o `driver.timeouts.page_load`
    (Selenium 4.48+). Si no es legible pero el driver SOPORTA
    `set_page_load_timeout`, se asume el default del bot (60s, fijado en
    `iniciar_driver`) para poder restaurarlo; si el driver no lo soporta
    (Fakes de tests), el helper queda como no-op.
    """
    previo = None
    for getter in (
        lambda d: d.page_load_timeout,
        lambda d: d.timeouts.page_load,
    ):
        try:
            valor = getter(driver)
        except Exception:
            continue
        try:
            valor = float(valor)
        except (TypeError, ValueError):
            continue
        if valor > 0:
            previo = valor
            break
    if previo is None:
        try:
            driver.set_page_load_timeout(60)
        except Exception:
            previo = None
        else:
            previo = 60.0
    if previo is not None:
        try:
            driver.set_page_load_timeout(min(previo, float(timeout_max)))
        except Exception:
            previo = None
    try:
        yield
    finally:
        if previo is not None:
            try:
                driver.set_page_load_timeout(previo)
            except Exception:
                pass


def _proxy_x_valido_reciente(proxy: str) -> bool:
    """True si `proxy` se valido hace menos de `PROXY_X_CACHE_SEG` segundos."""
    try:
        if _proxy_x_cache_segundos() <= 0:
            return False
        with _PROXY_X_LOCK:
            expira = _PROXY_X_VALIDADOS.get(proxy, 0.0)
        return expira > time.time()
    except Exception:
        return False


def _marcar_proxy_x_valido(proxy: str) -> None:
    """Guarda que `proxy` alcanzo x.com (limpieza simple del mapa)."""
    try:
        ttl = _proxy_x_cache_segundos()
        if ttl <= 0 or not proxy:
            return
        ahora = time.time()
        with _PROXY_X_LOCK:
            _PROXY_X_VALIDADOS[proxy] = ahora + ttl
            if len(_PROXY_X_VALIDADOS) > _PROXY_X_CACHE_MAX:
                for clave in [
                    k for k, v in _PROXY_X_VALIDADOS.items() if v <= ahora
                ]:
                    _PROXY_X_VALIDADOS.pop(clave, None)
    except Exception:
        pass


class TwitterBot:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.driver = None
        self.base_url = "https://x.com"
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.ua_config_path = resolver_ruta("data/perfiles_chrome/ua_config.txt")
        self._ua_persistente = None
        self.ultima_url_publicada = ""
        self.ultimo_error = ""
        self.cuenta_suspendida = False
        self._proxy_cache = None
        # Proxy local de auth (LocalForwardProxy) de ESTE navegador. Se
        # guarda aqui porque `aplicar_a_options` lo devuelve al llamador y
        # `cerrar()` debe cerrarlo siempre (antes quedaba vivo un aceptador +
        # un hilo por conexion de Chrome por CADA navegador lanzado).
        self._fwd_proxy = None
        # True cuando las cookies se inyectaron por CDP sin navegar
        # (`preparar_sesion_cdp`): los flujos van directo a la URL objetivo y,
        # si X pide login, hacen UN fallback a `login_con_cookies()`.
        self._sesion_cdp = False

    @staticmethod
    def _ahora() -> float:
        """Reloj local para medir fases (perf).

        Usa `time.monotonic()`; los tests parchean `selenium_bot.time` con un
        reloj falso sin `monotonic`, por lo que se cae a `time.time()` para no
        romperlos. Nunca lanza.
        """
        try:
            return time.monotonic()
        except Exception:
            pass
        try:
            return time.time()
        except Exception:
            return 0.0
    
    def _obtener_proxy(self) -> str:
        # Modo sin proxy: usa la IP del servidor (Railway) tal cual. Se
        # comprueba ANTES de tocar la BD/ProxyManager para no validar
        # (`x_accesible`) ni rotar sesiones sticky.
        if _env_activo("TWITTER_SIN_PROXY", False):
            logger.info(
                f"TWITTER_SIN_PROXY activo: {self.usuario} usara la IP del "
                f"servidor (sin proxy)"
            )
            return ""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                if cuenta and (cuenta.proxy or "").strip():
                    return cuenta.proxy
        except Exception:
            logger.warning(f"No se pudo leer el proxy de {self.usuario}, usando proxy MX sticky")
        # Fallback: proxy Smartproxy México sticky, sesión determinista por usuario.
        return settings.proxy_sticky_mx(
            session_id=hashlib.md5(self.usuario.encode()).hexdigest()[:8]
        )

    def _guardar_proxy(self, proxy: str) -> None:
        """Persiste el proxy que resultó funcional para reutilizarlo luego."""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                if cuenta and (cuenta.proxy or "") != proxy:
                    cuenta.proxy = proxy
        except Exception as e:
            logger.warning(f"No se pudo guardar el proxy de {self.usuario}: {e}")

    def _guardar_cookies_json(self, cookies: list) -> None:
        """Persiste las cookies derivadas en `cookies_json` de la cuenta (y el
        `auth_token` que traigan, para que futuras corridas no dependan del
        password/TOTP si el .pkl/cookies_json se pierden)."""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                if cuenta is not None:
                    cuenta.cookies_json = cookies
                    auth_token = next(
                        (c.get("value") for c in cookies
                         if isinstance(c, dict) and c.get("name") == "auth_token" and c.get("value")),
                        None,
                    )
                    if auth_token:
                        cuenta.auth_token = auth_token
        except Exception as e:
            logger.warning(f"No se pudieron guardar las cookies de {self.usuario}: {e}")

    def _brandear_cuenta(self, cookies: list) -> None:
        """Guarda la sesion resultante en .pkl y en la BD (`cookies_json` +
        `auth_token`) para que la cuenta quede lista sin repetir el login."""
        self._guardar_cookies_json(cookies)
        try:
            os.makedirs(os.path.dirname(self.cookies_path), exist_ok=True)
            with open(self.cookies_path, "wb") as f:
                pickle.dump(cookies, f)
        except Exception as e:
            logger.warning(f"No se pudo escribir el .pkl de {self.usuario}: {e}")

    def _cookies_auth_token(self) -> Optional[list]:
        """Cookie mínima (solo auth_token) para que el NAVEGADOR complete la sesión.

        X NO emite `ct0` en una petición sin sesión; lo emite al cargar x.com
        con una sesión válida. Por eso inyectamos solo el auth_token, cargamos
        x.com y luego guardamos todas las cookies que X emita (incluido ct0)."""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                auth_token = (cuenta.auth_token or "").strip() if cuenta else ""
        except Exception as e:
            logger.warning(f"No se pudo leer el auth_token de {self.usuario}: {e}")
            return None

        if not auth_token:
            self.ultimo_error = "la cuenta no tiene auth_token para iniciar sesión"
            return None

        logger.info(f"Inyectando auth_token de {self.usuario} para que X emita ct0")
        return [{
            "name": "auth_token", "value": auth_token, "domain": ".x.com",
            "path": "/", "secure": True, "httpOnly": True,
        }]

    def _proxy_para_x(self) -> str:
        """Devuelve un proxy que SI alcanza x.com.

        X bloquea algunas IPs residenciales del pool: el tunel CONNECT da 200
        pero x.com nunca responde (net::ERR_CONNECTION_CLOSED / timeout). Como
        cada sesion sticky sale por una IP distinta, se valida y, si falla, se
        rota la sesion hasta encontrar una operativa. El proxy resultante se
        guarda en la cuenta para dar estabilidad en las siguientes ejecuciones.
        """
        if self._proxy_cache:
            return self._proxy_cache

        proxy = self._obtener_proxy()
        if not proxy:
            return ""
        pm = ProxyManager()
        if "_session-" not in proxy:
            self._proxy_cache = proxy
            return proxy

        # Cache por proceso: dentro de una campana la sesion sticky no cambia y
        # re-sondear x.com en cada accion cuesta ~0.5-1.5s (hasta ~60s si el
        # proxy esta bloqueado y rota 5 veces). Con el TTL corto el proxy se
        # re-valida solo si pasó un rato (o si `PROXY_X_CACHE_SEG=0`).
        if _proxy_x_valido_reciente(proxy):
            logger.debug(
                f"Proxy de {self.usuario} ya validado hace poco; se omite el sondeo"
            )
            self._proxy_cache = proxy
            return proxy

        intentos = int(os.environ.get("PROXY_X_INTENTOS", "5"))
        for i in range(intentos):
            if pm.x_accesible(proxy):
                _marcar_proxy_x_valido(proxy)
                if i:
                    logger.info(f"Proxy de {self.usuario} OK tras {i} rotacion(es)")
                    self._guardar_proxy(proxy)
                self._proxy_cache = proxy
                return proxy
            logger.warning(
                f"Proxy de {self.usuario} no alcanza x.com; rotando sesion "
                f"({i + 1}/{intentos})"
            )
            proxy = pm.refrescar_sesion(proxy)

        logger.error(f"Ningun proxy alcanzo x.com para {self.usuario}; se usara el ultimo")
        self._proxy_cache = proxy
        return proxy

    def _obtener_ua_consistente(self) -> str:
        """Devuelve el User-Agent EXACTO que debe usar esta cuenta.

        Orden: `Cuenta.user_agent` (viene en el lote y se guarda en la BD) ->
        archivo global `data/perfiles_chrome/ua_config.txt` (override manual) ->
        "" (se deja el UA natural de Chrome). Nunca genera ni persiste un UA
        aleatorio, porque un UA distinto al de la cuenta delata la automatizacion.
        """
        if self._ua_persistente is not None:
            return self._ua_persistente

        ua = ""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                ua = resolver_ua_cuenta(cuenta)
        except Exception as e:
            logger.warning(f"No se pudo leer el user_agent de {self.usuario}: {e}")
            ua = resolver_ua_cuenta(None)

        self._ua_persistente = ua
        if ua:
            logger.info(f"UA de {self.usuario}: {ua}")
        else:
            logger.info(
                f"{self.usuario} sin user_agent configurado; se usara el UA natural de Chrome"
            )
        return ua
    
    def iniciar_driver(self, pantalla_externa: bool = False, proxy_dinamico: bool = False) -> bool:
        """Inicia Chrome para `self.usuario`.

        `proxy_dinamico=True` (pestaña persistente): Chrome SIEMPRE se enruta
        por el `LocalForwardProxy` local (aunque la cuenta no tenga proxy =>
        modo directo), de forma que `cambiar_cuenta()` puede cambiar la IP de
        salida en caliente con `cambiar_upstream()`. `proxy_dinamico=False`
        conserva el comportamiento anterior (solo se aplica proxy si la cuenta
        tiene uno).
        """
        self.ultimo_error = ""
        try:
            perfil = settings.profiles_dir / self.usuario
            os.makedirs(perfil, exist_ok=True)
            # Limpia bloqueos huerfanos de un Chrome cerrado a la fuerza (en el
            # volumen persistente de Railway quedan tras cada reinicio y
            # provocan "user data directory is already in use").
            for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    os.remove(perfil / lock)
                except OSError:
                    pass

            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--disable-quic")
            options.add_argument(f"--user-data-dir={perfil}")

            # UA EXACTO de la cuenta (del lote). Si no hay, se deja el natural
            # de Chrome (no se inventa/persiste uno aleatorio).
            ua = self._obtener_ua_consistente()
            if ua:
                options.add_argument(f"--user-agent={ua}")
            
            if settings.headless:
                options.add_argument("--headless=new")
            
            if pantalla_externa:
                options.add_argument("--window-size=1920,1080")
            else:
                options.add_argument("--window-size=1366,768")
            
            proxy = self._proxy_para_x()
            if proxy_dinamico:
                # Pestaña persistente: SIEMPRE forwarder local (modo directo si
                # la cuenta no tiene proxy) para poder cambiar el upstream en
                # caliente al cambiar de cuenta.
                # El proxy local es de ESTE bot: se guarda para cerrarlo en
                # `cerrar()`.
                self._fwd_proxy = ProxyManager().aplicar_a_options(
                    options, proxy, tag=self.usuario, dinamico=True
                )
            elif proxy:
                # El proxy local es de ESTE bot: se guarda para cerrarlo en
                # `cerrar()` (sin esto quedaba un hilo aceptador + workers por
                # cada navegador lanzado hasta agotar el contenedor).
                self._fwd_proxy = ProxyManager().aplicar_a_options(
                    options, proxy, tag=self.usuario
                )

            # X es una SPA pesada: "eager" devuelve el control al terminar el
            # HTML sin esperar todos los subrecursos (con "normal", en Railway
            # el renderer se saturaba y saltaba el page_load_timeout).
            options.page_load_strategy = "eager"

            self.driver = crear_chrome(options, version_main=detectar_chrome_version())
            self.driver.set_page_load_timeout(60)
            self.driver.set_script_timeout(60)

            # Stealth en cada documento nuevo + UA por CDP (incluye
            # userAgentMetadata y Accept-Language coherentes con el UA).
            aplicar_stealth(self.driver)
            if ua:
                aplicar_user_agent(self.driver, ua)
            
            logger.info(f"Driver iniciado para {self.usuario}")
            return True
        
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error iniciando driver para {self.usuario}: {e}")
            # Si el driver no llego a crearse, no dejar vivo el proxy local.
            self._cerrar_fwd_proxy()
            return False

    def esta_vivo(self) -> bool:
        """True si hay driver y RESPONDE (`current_url`). Nunca lanza.

        Lo usa `cambiar_cuenta` para no inyectar cookies en un Chrome muerto
        (`tab crashed`, driver cerrado, sesion cdp colgada...).
        """
        try:
            driver = self.driver
            if driver is None:
                return False
            _ = driver.current_url
            return True
        except Exception:
            return False

    def _limpiar_cache_no_bloqueante(self, usuario: str = "") -> None:
        """Lanza `Network.clearBrowserCache` en un hilo daemon con tope.

        `clearBrowserCache` NO borra cookies, por lo que puede solaparse con la
        inyeccion de las cookies nuevas sin riesgo. En Windows/Chrome se
        observo UN hipo de ~30s al limpiar la cache con la pestaña cargada: la
        llamada se ejecuta en un hilo daemon y `cambiar_cuenta` espera como
        maximo `CAMBIO_CUENTA_CACHE_TIMEOUT` segundos (env, default 3.0;
        `<=0` omite la llamada por completo); si el hilo sigue vivo, se informa
        a DEBUG y el flujo continua. Nunca lanza ni cambia el retorno.
        """
        try:
            tope = _env_float_seg("CAMBIO_CUENTA_CACHE_TIMEOUT", 3.0)
        except Exception:
            tope = 3.0
        if tope <= 0:
            logger.debug(
                "cambiar_cuenta: clearBrowserCache omitido "
                "(CAMBIO_CUENTA_CACHE_TIMEOUT<=0)"
            )
            return
        driver = self.driver
        etiqueta = usuario or self.usuario

        def _limpiar():
            try:
                driver.execute_cdp_cmd("Network.clearBrowserCache", {})
            except Exception as e:
                logger.debug(
                    f"clearBrowserCache (segundo plano) fallo para {etiqueta}: "
                    f"{type(e).__name__}: {e}"
                )

        try:
            hilo = threading.Thread(
                target=_limpiar, name="clear-cache-cuenta", daemon=True
            )
            hilo.start()
        except Exception as e:
            logger.debug(
                f"No se pudo lanzar el hilo de clearBrowserCache para "
                f"{etiqueta}: {type(e).__name__}: {e}"
            )
            return
        hilo.join(timeout=tope)
        if hilo.is_alive():
            logger.debug(
                f"clearBrowserCache sigue en segundo plano (> {tope:.1f}s); "
                "se continua con el cambio de cuenta"
            )

    def cambiar_cuenta(
        self,
        usuario: str,
        proxy: str = "",
        validar_proxy: bool = False,
    ) -> bool:
        """Cambia la sesion EN CALIENTE a `usuario` sobre el MISMO Chrome.

        "Pestaña persistente": el navegador se abre UNA vez (con
        `iniciar_driver(proxy_dinamico=True)`) y por cada cuenta solo se cambia
        cookies + user-agent + upstream del proxy. Devuelve True si quedo lista
        la sesion de la cuenta nueva; nunca lanza.

        - `usuario` == `self.usuario` y driver vivo => True (no-op).
        - Sin driver vivo => False con `self.ultimo_error` explicativo.
        - `proxy` explicito o `self._obtener_proxy()` (cuenta nueva; respeta
          `TWITTER_SIN_PROXY`). Con `validar_proxy=True` se usa la logica de
          `_proxy_para_x()` (puede tardar hasta ~60s rotando; default False).
        - Si no hay `_fwd_proxy` y hace falta proxy, se avisa por WARNING: a un
          Chrome ya abierto no se le puede inyectar `--proxy-server`; las
          cookies igual se cambian.
        - Limpieza: `clearBrowserCookies` (+ fallback `delete_all_cookies`) es
          SINCRONA (rapida y critica para no mezclar sesiones);
          `clearBrowserCache` corre en un hilo daemon con tope
          `CAMBIO_CUENTA_CACHE_TIMEOUT` (env, default 3.0s; <=0 la omite): un
          hipo de Chrome/Windows de ~30s ya no puede frenar el cambio de cuenta
          (no borra cookies, asi que se solapa con la inyeccion sin riesgo).
        - Estado por cuenta que se resetea: `cookies_path`, `_ua_persistente`,
          `_proxy_cache`, `_sesion_cdp`, `ultima_url_publicada`,
          `cuenta_suspendida` y `ultimo_error`. (Escaneado de atributos: son
          TODOS los caches por cuenta de la clase.)
        """
        usuario = (usuario or "").strip()
        if not usuario:
            self.ultimo_error = "cambiar_cuenta: usuario vacio"
            return False
        if usuario == self.usuario and self.esta_vivo():
            return True
        if not self.esta_vivo():
            self.ultimo_error = (
                "cambiar_cuenta: el navegador no esta vivo; hay que iniciar "
                "driver de nuevo"
            )
            logger.warning(self.ultimo_error)
            return False

        try:
            t_inicio = self._ahora()
            ua_anterior = self._ua_persistente or ""

            # Actualiza la identidad de la cuenta y resetea TODO su estado
            # cacheado antes de inyectar nada.
            self.usuario = usuario
            self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
            self._ua_persistente = None
            self._proxy_cache = None
            self._sesion_cdp = False
            self.ultimo_error = ""
            self.cuenta_suspendida = False
            self.ultima_url_publicada = ""

            # UA de la cuenta nueva (CDP lo cambia en caliente).
            ua = self._obtener_ua_consistente()
            if ua:
                if ua != ua_anterior:
                    aplicar_user_agent(self.driver, ua)
            elif ua_anterior:
                # La cuenta nueva no tiene UA propio y la anterior si tenia
                # override: intentar volver al UA NATURAL de Chrome (best
                # effort: si CDP no lo soporta, se queda el anterior).
                try:
                    self.driver.execute_cdp_cmd(
                        "Emulation.setUserAgentOverride", {"userAgent": ""}
                    )
                except Exception as e:
                    logger.debug(
                        f"No se pudo resetear el UA natural de {usuario}: "
                        f"{type(e).__name__}: {e}"
                    )
            t_ua = self._ahora()

            # Limpia cookies (SINCRONO: rapido y critico para no mezclar
            # sesiones) y cache (NO bloqueante con tope: en Windows/Chrome se
            # vio un hipo de ~30s; clearBrowserCache no toca cookies, asi que
            # puede seguir en segundo plano mientras se inyectan las nuevas).
            try:
                self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            except Exception as e:
                logger.debug(f"clearBrowserCookies fallo para {usuario}: {e}")
                try:
                    self.driver.delete_all_cookies()
                except Exception as e2:
                    logger.debug(f"delete_all_cookies fallo para {usuario}: {e2}")
            self._limpiar_cache_no_bloqueante(usuario)
            t_limpieza = self._ahora()

            # Proxy objetivo: explicito o el de la cuenta nueva.
            proxy_objetivo = (proxy or "").strip()
            if not proxy_objetivo:
                proxy_objetivo = self._obtener_proxy()
            if validar_proxy:
                self._proxy_cache = None
                proxy_objetivo = self._proxy_para_x()

            if self._fwd_proxy is not None:
                self._fwd_proxy.cambiar_upstream(proxy_objetivo)
                if proxy_objetivo:
                    logger.info(f"{usuario}: upstream cambiado")
                else:
                    logger.info(f"{usuario}: upstream en modo directo (sin proxy)")
            elif proxy_objetivo:
                logger.warning(
                    f"{usuario}: sin forwarder local; no se puede aplicar el "
                    f"proxy a un Chrome ya abierto (la sesion de cookies si se "
                    f"cambia)"
                )
            t_proxy = self._ahora()

            # Inyecta las cookies de la cuenta nueva sin navegar.
            ok = bool(self.preparar_sesion_cdp())
            t_cookies = self._ahora()
            if not ok:
                self.ultimo_error = (
                    self.ultimo_error
                    or "la cuenta no tiene cookies ni auth_token usable"
                )
                logger.warning(
                    f"cambiar_cuenta: sin sesion para {usuario} "
                    f"({self.ultimo_error})"
                )
            try:
                logger.debug(
                    f"perf @{usuario}: cambiar_cuenta total="
                    f"{t_cookies - t_inicio:.2f}s "
                    f"(ua={t_ua - t_inicio:.2f} "
                    f"limpieza={t_limpieza - t_ua:.2f} "
                    f"proxy={t_proxy - t_limpieza:.2f} "
                    f"cookies={t_cookies - t_proxy:.2f})"
                )
            except Exception:
                pass
            return ok
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.warning(
                f"No se pudo cambiar la cuenta a {usuario} en caliente: {e}"
            )
            return False

    def login_con_cookies(self) -> bool:
        if not os.path.exists(self.cookies_path):
            logger.warning(f"No hay cookies .pkl para {self.usuario}, intentando cookies_json")
            return self.login_con_cookies_json()
        
        if not self.driver and not self.iniciar_driver():
            return False
        
        try:
            self.driver.get(self.base_url)
            self._esperar_documento_listo()
            time.sleep(0.3)
            
            with open(self.cookies_path, "rb") as f:
                cookies = pickle.load(f)

            # Misma via CDP que `login_con_cookies_json` (helper compartido):
            # `add_cookie` puede fallar con "invalid cookie domain" en
            # contenedores/redirecciones (bug real Chrome 154 en Railway).
            cookies_selenium = normalizar_cookies(cookies)
            nombres_inyectados: set = set()
            inyectadas_cdp, _fallidas_cdp = self._inyectar_cookies_cdp(
                cookies_selenium, nombres_inyectados
            )
            if inyectadas_cdp:
                logger.info(
                    f"login: cookies inyectadas via CDP "
                    f"({inyectadas_cdp}/{len(cookies_selenium or [])})"
                )
            nombres_lista = {
                str(c.get("name")) for c in cookies_selenium if isinstance(c, dict)
            }
            falta_auth_token = (
                "auth_token" in nombres_lista
                and "auth_token" not in nombres_inyectados
            )
            if inyectadas_cdp == 0 or falta_auth_token:
                logger.warning(
                    f"login: CDP no inyecto las cookies del .pkl de "
                    f"{self.usuario}; fallback add_cookie"
                )
                for cookie in cookies_selenium:
                    try:
                        self.driver.add_cookie(cookie)
                    except Exception:
                        continue
            
            self.driver.refresh()
            self._esperar_documento_listo()
            time.sleep(0.3)

            if self._detectar_cuenta_propia_suspendida():
                self.cuenta_suspendida = True
                self.ultimo_error = "cuenta suspendida/bloqueada por X"
                logger.warning(f"Cuenta suspendida detectada para {self.usuario}")
                return False

            # Anti-bot (Cloudflare) TAMPOCO es "login exitoso": `/account/access`
            # no contiene "login" en la URL y antes se reportaba sesion
            # confirmada. Se devuelve False sin tocar `cuenta_suspendida`.
            if self.es_pagina_anti_bot():
                self.ultimo_error = (
                    "X pidió verificación anti-bot (Cloudflare); sesión no confirmada"
                )
                logger.warning(
                    f"Anti-bot (Cloudflare) en login .pkl de {self.usuario} "
                    f"({self._diagnostico_pagina()}); la cuenta NO se marca suspendida"
                )
                return False

            # FALSO "Login exitoso" arreglado (log real de Railway
            # 2026-09-21): un .pkl vencido puede servir la home como visitante
            # SIN "login" en la URL; X recien pide login al abrir
            # `/compose/post` y se perdian hasta 38s de compositor. Ahora se
            # usa `_hay_muro_login()`, que ademas de la URL detecta los
            # formularios VISIBLES de login (username, loginButton,
            # ocfEnterTextTextInput, password).
            if self._hay_muro_login():
                logger.warning(
                    f"Sesion .pkl invalida para {self.usuario}; "
                    "probando cookies completas de la BD (cookies_json)"
                )
                # El .pkl puede estar vencido o tener solo auth_token: antes de
                # rendirse usa TODAS las cookies de la BD (auth_token, ct0,
                # twid, ...) reutilizando el driver ya abierto.
                if self.login_con_cookies_json():
                    return True
                # Sin cookies validas: deja un error de sesion expirada. Si el
                # fallback ya explico otra causa (anti-bot, suspendida o el
                # propio auth_token) ese mensaje se conserva.
                error = (self.ultimo_error or "").lower()
                if not any(
                    frag in error
                    for frag in (
                        "expirad",
                        "invalid",
                        "inválid",
                        "anti-bot",
                        "suspend",
                    )
                ):
                    self.ultimo_error = "sesión expirada (.pkl inválido): " + (
                        self.ultimo_error or "sin cookies_json ni auth_token"
                    )
                return False

            logger.info(f"Login exitoso para {self.usuario}")
            return True
        
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error en login {self.usuario}: {e}")
            # Falla dura del .pkl: intenta con las cookies completas de la BD.
            return self.login_con_cookies_json()
    
    def login_con_cookies_json(self) -> bool:
        """Inicia sesión inyectando las cookies nativas de X guardadas en BD
        (`Cuenta.cookies_json`) en lugar del archivo .pkl.

        Si X NO emite `ct0` (auth_token muerto) o redirige a login, y la cuenta
        tiene `password` en la BD, intenta UN login con password/TOTP
        (`_revivir_sesion_con_password`) y sigue el flujo normal si revive;
        si falla, deja un mensaje que dice la verdad.
        """
        import json

        cookies_json = None
        password = ""
        totp_secret = ""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                cookies_json = cuenta.cookies_json if cuenta else None
                password = (cuenta.password or "").strip() if cuenta else ""
                totp_secret = (cuenta.totp_secret or "").strip() if cuenta else ""
        except Exception as e:
            logger.error(f"Error leyendo cookies_json de {self.usuario}: {e}")

        if not cookies_json:
            logger.info(f"{self.usuario} sin cookies: usando auth_token para que X emita ct0...")
            cookies_json = self._cookies_auth_token()

        if not cookies_json:
            if password:
                logger.info(
                    f"{self.usuario} sin cookies ni auth_token: intentando login con "
                    "password/TOTP..."
                )
                return self.login_con_password(password, totp_secret=totp_secret)
            self.ultimo_error = self.ultimo_error or "la cuenta no tiene cookies ni auth_token usable"
            logger.warning(f"No hay cookies_json para {self.usuario}")
            return False

        if isinstance(cookies_json, str):
            try:
                cookies_json = json.loads(cookies_json)
            except Exception:
                logger.warning(f"cookies_json de {self.usuario} no es JSON valido")
                return False

        if not isinstance(cookies_json, list):
            logger.warning(f"cookies_json de {self.usuario} no es una lista")
            return False

        if not self.driver:
            if not self.iniciar_driver():
                return False

        try:
            # Normaliza TODAS las cookies (auth_token, ct0, twid, etc.) desde
            # formatos de vendedor/EditThisCookie/Selenium al que acepta Chrome.
            cookies_selenium = normalizar_cookies(cookies_json)
            if not cookies_selenium:
                logger.warning(f"cookies_json de {self.usuario} sin cookies utilizables")
                self.ultimo_error = "cookies_json sin cookies utilizables"
                return False

            # (a) CDP PRIMERO: `Network.setCookie` NO depende del documento
            # actual. Bug real de Chrome 154 en Railway: `driver.add_cookie`
            # lanzaba "invalid cookie domain" para CADA cookie (el documento
            # quedaba en un 404/redireccion/interstitial) y las cuentas sin
            # `.pkl` nunca inyectaban la sesion (52/52 fallos de "Reportar").
            nombres_inyectados: set = set()
            inyectadas_cdp, fallidas_cdp = self._inyectar_cookies_cdp(
                cookies_selenium, nombres_inyectados
            )
            if inyectadas_cdp:
                logger.info(
                    f"login: cookies inyectadas via CDP "
                    f"({inyectadas_cdp}/{len(cookies_selenium)})"
                )

            nombres_lista = {
                str(c.get("name")) for c in cookies_selenium if isinstance(c, dict)
            }
            falta_auth_token = (
                "auth_token" in nombres_lista
                and "auth_token" not in nombres_inyectados
            )

            # (b) Fallback UNICO con add_cookie si el CDP no inyecto NADA o
            # dejo fuera la cookie critica: navegar al dominio base (para que
            # el documento sea valido) y reintentar.
            if inyectadas_cdp == 0 or falta_auth_token:
                logger.warning(
                    f"login: CDP inyecto {inyectadas_cdp}/{len(cookies_selenium)} "
                    f"cookies para {self.usuario}; fallback add_cookie"
                )
                try:
                    self.driver.get(self.base_url)
                    self._esperar_documento_listo()
                    time.sleep(0.3)
                except Exception as e:
                    logger.debug(f"No se pudo navegar a {self.base_url}: {e}")
                for cookie in cookies_selenium:
                    try:
                        self.driver.add_cookie(cookie)
                        inyectadas_cdp += 1
                        if isinstance(cookie, dict) and cookie.get("name"):
                            nombres_inyectados.add(str(cookie["name"]))
                    except Exception as e:
                        logger.debug(
                            f"Cookie {cookie.get('name') if isinstance(cookie, dict) else '?'} "
                            f"no inyectada por add_cookie: {e}"
                        )
                        continue

            # (c) La cookie critica DEBE haber entrado. Si no, es un fallo de
            # INYECCION (bug/entorno), NO de validez del auth_token: cortar
            # aqui sin llegar a afirmar "X no emitio ct0".
            if "auth_token" in nombres_lista and "auth_token" not in nombres_inyectados:
                self.ultimo_error = (
                    "no se pudieron inyectar las cookies en el navegador "
                    "(CDP/add_cookie)"
                )
                logger.error(
                    f"Login {self.usuario}: {self.ultimo_error} "
                    f"(CDP={inyectadas_cdp}, fallidas={fallidas_cdp})"
                )
                return False

            # Refrescar para que Chrome aplique las cookies recien inyectadas
            # (con CDP no hizo falta navegar antes de inyectar).
            try:
                self.driver.refresh()
                self._esperar_documento_listo()
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Refresh tras inyectar cookies fallo: {e}")

            # Entrar a /home para que X ejecute su JS autenticado y emita ct0.
            try:
                self.driver.get(f"{self.base_url}/home")
                self._esperar_documento_listo()
                time.sleep(0.3)
            except Exception:
                pass

            # Anti-bot ANTES de suspension (falso positivo real de Railway): X
            # redirige a `/account/access?__cf_chl_rt_tk=...` con el challenge
            # de Cloudflare ("Just a moment...") y antes se marcaba como
            # suspendida, desactivando cuentas buenas (`marcar_cuenta_suspendida`).
            # Aqui se devuelve False SIN tocar `cuenta_suspendida`.
            if self.es_pagina_anti_bot():
                self.ultimo_error = (
                    "X pidió verificación anti-bot (Cloudflare); sesión no confirmada"
                )
                logger.warning(
                    f"Anti-bot (Cloudflare) al confirmar sesion de {self.usuario} "
                    f"({self._diagnostico_pagina()}); la cuenta NO se marca suspendida"
                )
                return False

            if self._detectar_cuenta_propia_suspendida():
                self.cuenta_suspendida = True
                self.ultimo_error = "cuenta suspendida/bloqueada por X"
                logger.warning(f"Cuenta suspendida detectada para {self.usuario} (cookies_json)")
                return False

            # X suele lanzar un desafio de seguridad (login inusual) al inyectar
            # un auth_token "en frio". Si hay password/totp_secret en la BD, se
            # intenta resolver antes de declarar la sesion como expirada.
            if self._hay_challenge_seguridad():
                if not self._resolver_challenge_seguridad(password, totp_secret):
                    self.ultimo_error = self.ultimo_error or (
                        "X pidio verificar identidad y no se pudo resolver "
                        "con password/totp_secret"
                    )
                    logger.warning(
                        f"Challenge de seguridad sin resolver para {self.usuario}"
                    )
                    return False

            # Sesion pedida explicitamente por X (redirect a login): el
            # auth_token esta muerto. Si la cuenta tiene password en BD, UN
            # intento de revivir con password/TOTP antes de fallar.
            if "login" in self.driver.current_url.lower():
                if self._revivir_sesion_con_password(password, totp_secret):
                    self.ultimo_error = ""
                    return True
                self.ultimo_error = "sesión expirada (auth_token/cookies inválidos)"
                if (password or "").strip():
                    self.ultimo_error += "; también falló el login con contraseña"
                logger.warning(f"Sesion expirada para {self.usuario} (cookies_json)")
                return False

            # Guarda TODAS las cookies que X generó (incluye ct0) para reutilizar
            # tanto en `cookies_json` (BD) como en el .pkl local: "brandear" la
            # cuenta para no repetir este proceso en la siguiente ejecucion.
            try:
                cookies_navegador = self.driver.get_cookies()
            except Exception as e:
                logger.warning(
                    f"No se pudieron leer las cookies del navegador: {e}"
                )
                cookies_navegador = []

            if cookies_navegador and any(
                c.get("name") == "ct0" for c in cookies_navegador
            ):
                self._brandear_cuenta(cookies_navegador)
                logger.info(
                    f"Cookies (incl. ct0) guardadas para {self.usuario} "
                    f"({len(cookies_navegador)}), cuenta brandeada"
                )
                logger.info(f"Login exitoso para {self.usuario} via cookies_json")
                # Limpia un error previo (p.ej. del .pkl vencido) ya que la
                # sesion quedo confirmada.
                self.ultimo_error = ""
                return True

            # FIX A: X no emitio ct0 (auth_token muerto). Si la cuenta tiene
            # password/totp_secret en la BD, UN intento de login con password.
            logger.warning(
                f"X no emitio ct0 para {self.usuario}; sesion no confirmada"
            )
            if self._revivir_sesion_con_password(password, totp_secret):
                self.ultimo_error = ""
                return True
            self.ultimo_error = (
                "X no emitió ct0 (auth_token inválido o sesión bloqueada)"
            )
            if (password or "").strip():
                self.ultimo_error += "; también falló el login con contraseña"
            return False

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error en login con cookies_json {self.usuario}: {e}")
            return False

    def _revivir_sesion_con_password(self, password: str, totp_secret: str) -> bool:
        """UN login con password/TOTP cuando el auth_token no da sesion.

        Se usa SOLO desde `login_con_cookies_json` (auth_token muerto): el
        motor de activaciones conserva su gate `ACTIVACION_PERMITIR_PASSWORD`.
        Si `login_con_password` funciona, brandea la cuenta (cookies ct0) y
        deja INFO "revivida con password"; si falla, WARNING claro. Nunca
        lanza.
        """
        if not (password or "").strip():
            return False
        logger.info(
            f"{self.usuario}: auth_token sin sesion; intentando revivir con "
            "password/TOTP (UN intento)"
        )
        try:
            ok = bool(self.login_con_password(password, totp_secret=totp_secret))
        except Exception as e:
            logger.warning(
                f"login_con_password fallo para {self.usuario}: "
                f"{type(e).__name__}: {e}"
            )
            ok = False
        if not ok:
            logger.warning(
                f"{self.usuario}: login con password/TOTP tambien fallo"
            )
            return False
        try:
            cookies = self.driver.get_cookies()
            if cookies:
                self._brandear_cuenta(cookies)
        except Exception as e:
            logger.debug(
                f"No se pudieron guardar las cookies tras revivir {self.usuario}: {e}"
            )
        logger.info(f"Login exitoso para {self.usuario} (revivida con password)")
        return True

    def _esperar_documento_listo(self, timeout: int = 10) -> bool:
        """Espera a que `document.readyState` sea `interactive`/`complete`.

        Poll cada 0.25s via `execute_script`. Nunca lanza: devuelve False si el
        driver no responde o no se alcanza el estado dentro de `timeout`s.
        """
        try:
            fin = time.time() + max(0.0, float(timeout))
        except Exception:
            return False
        while True:
            try:
                estado = self.driver.execute_script("return document.readyState")
            except Exception:
                estado = None
            if estado in ("interactive", "complete"):
                return True
            try:
                if time.time() >= fin:
                    return False
                time.sleep(0.25)
            except Exception:
                return False

    def _cargar_cookies_normalizadas(self) -> list:
        """Cookies de la cuenta normalizadas para CDP/Selenium (nunca lanza).

        Orden: `.pkl` de la cuenta (`pickle.load`) -> `Cookie.cookies_json` de
        la BD -> `auth_token` minimo para que X emita `ct0`. Devuelve [] si no
        hay nada usable. Reutiliza `normalizar_cookies` sin duplicar la logica
        de `login_con_cookies`/`login_con_cookies_json`.
        """
        # 1) Archivo .pkl (ruta rapida, ya en formato Selenium/navegador).
        try:
            if os.path.exists(self.cookies_path):
                with open(self.cookies_path, "rb") as f:
                    cookies_pkl = pickle.load(f)
                normalizadas = normalizar_cookies(cookies_pkl)
                if normalizadas:
                    return normalizadas
        except Exception as e:
            logger.debug(f"No se pudo leer el .pkl de {self.usuario}: {e}")

        # 2) cookies_json de la BD (formato completo: auth_token, ct0, twid...).
        try:
            import json

            from core.database import get_db_session
            from core.models import Cuenta

            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                cookies_json = cuenta.cookies_json if cuenta else None
            if isinstance(cookies_json, str):
                try:
                    cookies_json = json.loads(cookies_json)
                except Exception:
                    cookies_json = None
            if cookies_json:
                normalizadas = normalizar_cookies(cookies_json)
                if normalizadas:
                    return normalizadas
        except Exception as e:
            logger.debug(f"No se pudo leer cookies_json de {self.usuario}: {e}")

        # 3) auth_token minimo (X emite ct0 al cargar x.com con sesion valida).
        try:
            minimas = self._cookies_auth_token()
            if minimas:
                return normalizar_cookies(minimas)
        except Exception as e:
            logger.debug(f"No se pudo preparar el auth_token de {self.usuario}: {e}")
        return []

    # ------------------------------------------------------------------ #
    # Inyeccion de cookies por CDP (comun a logins y motor de activaciones)
    # ------------------------------------------------------------------ #
    # Bug real de Chrome 154 en Railway: `driver.add_cookie` lanzaba
    # "invalid cookie domain" para TODAS las cookies cuando el documento
    # actual no era valido (get fallido/redireccion/interstitial en el
    # contenedor), asi que las cuentas sin `.pkl` nunca entraban y TODOS los
    # flujos de pagina fallaban. La via CDP `Network.setCookie` NO depende
    # del documento actual (es la que ya usaba el motor de activaciones).
    #
    # Contrato de error para el dashboard (NO confundir):
    #   (i)  "no se pudieron inyectar las cookies en el navegador
    #        (CDP/add_cookie)"  -> bug/entorno de inyeccion.
    #   (ii) "X no emitio ct0 (auth_token invalido o sesion bloqueada)"
    #        -> las cookies SI entraron; el auth_token no vale.

    def _set_cookie_cdp(self, payload: dict) -> bool:
        """Inyecta UNA cookie con `Network.setCookie`; True si Chrome la acepto.

        No depende del documento actual (a diferencia de `add_cookie`). Trata
        como fallo tanto las excepciones como la respuesta `{"success": False}`.
        Nunca lanza.
        """
        try:
            respuesta = self.driver.execute_cdp_cmd("Network.setCookie", payload)
        except Exception as e:
            logger.debug(
                f"Network.setCookie fallo ({payload.get('name')}): "
                f"{type(e).__name__}: {e}"
            )
            return False
        if isinstance(respuesta, dict) and respuesta.get("success") is False:
            logger.debug(
                f"Network.setCookie rechazo la cookie {payload.get('name')} "
                "(success=False)"
            )
            return False
        return True

    @staticmethod
    def _payload_cookie_cdp(cookie: dict) -> Optional[dict]:
        """Payload de `Network.setCookie` para una cookie normalizada (o None).

        Mapea `expiry`/`expirationDate` a `expirationDate`; usa `domain`/`path`
        (o la `url` de la cookie si viene) y descarta cookies sin name/value.
        """
        if not isinstance(cookie, dict):
            return None
        nombre = cookie.get("name")
        valor = cookie.get("value")
        if not nombre or valor is None or str(valor) == "":
            return None
        domain = str(cookie.get("domain") or ".x.com")
        path = str(cookie.get("path") or "/")
        payload = {
            "name": str(nombre),
            "value": valor if isinstance(valor, str) else str(valor),
            "domain": domain,
            "path": path,
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        url_cookie = cookie.get("url")
        if url_cookie:
            payload["url"] = str(url_cookie)
        expira = cookie.get("expiry", cookie.get("expirationDate"))
        if expira:
            try:
                payload["expirationDate"] = float(expira)
            except (TypeError, ValueError):
                pass
        return payload

    def _inyectar_cookies_cdp(
        self, cookies: list, inyectadas_nombres: Optional[set] = None
    ) -> tuple:
        """Inyecta TODAS las cookies por CDP `Network.setCookie` (sin navegar).

        Misma via que `preparar_sesion_cdp` (motor de activaciones): no depende
        del documento actual y acepta cookies de cualquier formato normalizado
        (`normalizar_cookies`). Si el payload por `domain`/`path` falla, se
        reintenta UNA vez con una `url` derivada (algunos renderers exigen
        `url`). Si se pasa un set en `inyectadas_nombres`, se agregan los
        NOMBRES realmente inyectados (para validar la cookie critica). Nunca
        lanza.

        Devuelve `(inyectadas, fallidas)`.
        """
        inyectadas = 0
        fallidas = 0
        for cookie in cookies or ():
            payload = self._payload_cookie_cdp(cookie)
            if payload is None:
                fallidas += 1
                continue
            entregada = self._set_cookie_cdp(payload)
            if not entregada:
                # Reintento con `url` derivada del dominio (sin domain/path).
                domain = str(payload.get("domain") or ".x.com")
                path = str(payload.get("path") or "/")
                if not path.startswith("/"):
                    path = "/" + path
                payload_url = {
                    k: v for k, v in payload.items() if k not in ("domain", "path")
                }
                payload_url["url"] = f"https://{domain.lstrip('.')}{path}"
                entregada = self._set_cookie_cdp(payload_url)
            if entregada:
                inyectadas += 1
                if isinstance(inyectadas_nombres, set):
                    inyectadas_nombres.add(str(payload["name"]))
            else:
                fallidas += 1
                logger.debug(
                    f"Cookie {payload.get('name')} no inyectada por CDP"
                )
        return inyectadas, fallidas

    def preparar_sesion_cdp(self, cookies: Optional[list] = None) -> bool:
        """Inyecta las cookies guardadas por CDP SIN navegar (sesion rapida).

        Es la ruta principal del motor de activaciones: inicia el driver (si no
        existe) y hace `Network.enable` + un `Network.setCookie` por cookie
        (mapea `expiry` -> `expirationDate`; omite cookies sin name/value;
        domain por defecto `.x.com`). No navega ni espera: los flujos
        (`publicar_tweet`/`solo_retwittear`/`responder_tweet`) van directo a la
        URL objetivo y, si X pide login, hacen UN fallback a
        `login_con_cookies()`.

        Devuelve True si inyecto >=1 cookie (deja `self._sesion_cdp = True`);
        False si no hay cookies o CDP falla (el motor cae a `login_con_cookies`).
        Nunca lanza.
        """
        self.ultimo_error = ""
        self._sesion_cdp = False
        try:
            if not self.driver:
                if not self.iniciar_driver():
                    return False

            cookies = cookies if cookies is not None else self._cargar_cookies_normalizadas()
            if not cookies:
                logger.debug(f"sesion CDP no disponible para {self.usuario}: sin cookies")
                return False

            try:
                self.driver.execute_cdp_cmd("Network.enable", {})
            except Exception as e:
                logger.debug(f"CDP no disponible para {self.usuario}: {e}")
                return False

            # Misma via CDP que `login_con_cookies_json` (helper compartido).
            inyectadas, fallidas = self._inyectar_cookies_cdp(cookies)
            if fallidas:
                logger.debug(
                    f"sesion CDP: {inyectadas} inyectadas, {fallidas} fallidas "
                    f"para {self.usuario}"
                )

            if inyectadas >= 1:
                self._sesion_cdp = True
                logger.debug(f"sesion CDP lista para {self.usuario} ({inyectadas} cookies)")
                return True
            return False
        except Exception as e:
            logger.debug(f"Preparacion de sesion CDP fallo para {self.usuario}: {e}")
            self._sesion_cdp = False
            return False

    # ------------------------------------------------------------------ #
    # Navegacion tolerante al interstitial de error inicial de X
    # ------------------------------------------------------------------ #
    # El PRIMER render de una pestaña recien creada (cookies inyectadas por
    # CDP, headless/Railway) a veces cae en la pagina "something went wrong"
    # de X, y el `driver.get` quema el page_load_timeout completo (~60s). Un
    # refresh inmediato la resuelve. Estas señales identifican cuando hay que
    # DESCARTAR la pestaña en vez de reintentar.
    _SENALES_ERROR_DRIVER_DURO = (
        "invalid session id",
        "session id is null",
        "no such driver",
        "connection refused",
        "err_connection_refused",
        "max retries",
        "maxretry",
        "newconnectionerror",
        "tab crashed",
        "chrome not reachable",
        "cannot connect to chrome",
        "disconnected",
        "target closed",
    )

    @classmethod
    def _es_error_driver_duro(cls, error) -> bool:
        """True si el error indica que hay que DESCARTAR la pestaña/driver.

        Cubre `InvalidSessionIdException`, `NoSuchDriverException`,
        `MaxRetryError` y sus mensajes tipicos ("connection refused", "tab
        crashed", "cannot connect to chrome"...). Nunca lanza.
        """
        try:
            if isinstance(
                error,
                (InvalidSessionIdException, NoSuchDriverException, MaxRetryError),
            ):
                return True
            texto = str(error or "").lower()
            return any(senal in texto for senal in cls._SENALES_ERROR_DRIVER_DURO)
        except Exception:
            return False

    def _es_pagina_error_x(self) -> bool:
        """True si la pagina actual es el interstitial de error de X (o blank).

        Combina `_frase_error_pagina()` ("something went wrong"/"algo salió
        mal"/"rate limit"/"try again", tambien en el title) con URL
        vacia/`chrome://`/`about:` y title vacio estando en x.com, que es como
        suele quedar el interstitial real. Nunca lanza.
        """
        try:
            url = (self.driver.current_url or "").strip()
        except Exception:
            return True
        if not url or url.startswith("chrome://") or url.startswith("about:"):
            return True
        if self._frase_error_pagina():
            return True
        try:
            titulo = (self.driver.title or "").strip().lower()
        except Exception:
            return False
        if any(frase in titulo for frase in self._FRASES_ERROR_PAGINA):
            return True
        return not titulo and ("x.com" in url.lower() or "twitter.com" in url.lower())

    # Señales de la pagina anti-bot (Cloudflare u otros): NO es una suspension
    # ni el desafio de identidad de X. Falso positivo real en Railway:
    # `https://x.com/account/access?__cf_chl_rt_tk=...` con title "Just a
    # moment..." se marcaba como cuenta suspendida y el motor DESACTIVABA
    # cuentas buenas (`marcar_cuenta_suspendida`). El page_source se compara en
    # minusculas.
    _FRAGMENTOS_ANTI_BOT_URL = (
        "/account/access",
        "challenges.cloudflare.com",
    )
    _FRASES_ANTI_BOT_TITULO = (
        "just a moment",
        "un momento",
        "attention required",
        "checking your browser",
    )
    _FRASES_ANTI_BOT_FUENTE = (
        "__cf_chl",
        "challenges.cloudflare.com",
        "verifying you are human",
        "verificando que eres humano",
        "checking your browser",
        "enable javascript and cookies",
        "cf-chl",
    )

    def es_pagina_anti_bot(self) -> bool:
        """True si la pagina actual es un challenge/interstitial ANTI-BOT.

        Un challenge de Cloudflare (`/account/access?__cf_chl_rt_tk=...`,
        title "Just a moment...", `__cf_chl` en el HTML) NO es una cuenta
        suspendida: el chequeo de suspension llama aqui PRIMERO para no
        desactivar cuentas buenas (`marcar_cuenta_suspendida`), y el login no
        gasta password/TOTP (no es el desafio de identidad de X). Detecta por
        URL, title y `page_source`. Nunca lanza: ante driver muerto devuelve
        False y los flujos deciden.
        """
        try:
            try:
                url = (self.driver.current_url or "").lower()
            except Exception:
                url = ""
            try:
                title = (self.driver.title or "").lower()
            except Exception:
                title = ""
            try:
                fuente = (self.driver.page_source or "").lower()
            except Exception:
                fuente = ""
        except Exception:
            return False
        if any(frag in url for frag in self._FRAGMENTOS_ANTI_BOT_URL):
            return True
        if any(frase in title for frase in self._FRASES_ANTI_BOT_TITULO):
            return True
        return any(frase in fuente for frase in self._FRASES_ANTI_BOT_FUENTE)

    def _estado_pagina(self) -> str:
        """Clasifica la pagina actual: "login", "error" u "ok".

        "login" manda sobre "error" (una pagina de login jamas es usable).
        Nunca lanza.
        """
        try:
            if self._hay_muro_login():
                return "login"
        except Exception:
            pass
        try:
            if self._es_pagina_error_x():
                return "error"
        except Exception:
            pass
        return "ok"

    def _error_sesion_navegacion(self, url: str) -> str:
        """Mensaje (ya existente en el proyecto) de sesion de X expirada."""
        return (
            "sesión de X expirada o inválida: se pidió login al navegar a "
            f"{url} ({self._diagnostico_pagina()})"
        )

    def navegar_tolerante(self, url: str, timeout_total: float = 45.0) -> str:
        """Navega a `url` tolerando el interstitial inicial de error de X.

        `driver.get(url)` puede lanzar `TimeoutException` con la pagina de
        error "something went wrong" montada (quema el page_load_timeout): aqui
        se detecta y se hacen hasta DOS `driver.refresh()` acotados (P1: el
        generico de X a veces necesita un segundo refresh para curarse) con
        detecciones intermedias, siempre dentro de un presupuesto de
        recuperacion de ~15s. NO navega a ninguna otra ruta. Nunca lanza.

        El segundo refresh NUNCA se hace para el challenge anti-bot de
        Cloudflare, ni para login, ni para driver roto: esos se clasifican de
        inmediato como siempre.

        El `get` y los `refresh` corren con un `page_load_timeout` TEMPORAL
        (`min(previo, 25s)`) que se RESTAURA siempre (context manager con
        `finally`): un get atascado cuesta <=~25s en vez de los ~60-70s del
        timeout del bot (log real de Railway: "ok en 70.7s tras refresh").

        Devuelve:
          - "ok":     pagina usable (al get o tras algun refresh).
          - "error":  sigue la pagina de error tras los refrescos (fallo
                      controlado; deja `ultimo_error`).
          - "login":  X pide login (sesion caida; deja `ultimo_error`).
          - "driver": driver roto (`InvalidSessionId`/`NoSuchDriver`/`MaxRetry`
                      /"connection refused"/"tab crashed"...): el motor debe
                      descartar la pestaña.
        """
        t_inicio = self._ahora()

        def _log(final: str, extra: str = ""):
            try:
                logger.info(
                    f"perf @{self.usuario}: navegar {url} -> {final} "
                    f"en {self._ahora() - t_inicio:.1f}s {extra}".rstrip()
                )
            except Exception:
                pass

        def _marcar_driver(error) -> str:
            self.ultimo_error = f"{type(error).__name__}: {error}"
            _log("driver", f"err={self.ultimo_error}")
            logger.warning(
                f"navegar_tolerante: driver roto al ir a {url}: "
                f"{self.ultimo_error}"
            )
            return "driver"

        try:
            tiempo_total = max(0.0, float(timeout_total))
        except (TypeError, ValueError):
            tiempo_total = 45.0

        # 1) get: TimeoutException y WebDriverException transitorias se toleran;
        #    los fallos DUROS de driver devuelven "driver" de inmediato. El
        #    get corre con page_load_timeout acotado a 25s (se restaura siempre).
        with _page_load_timeout_acotado(self.driver, 25.0):
            try:
                self.driver.get(url)
            except TimeoutException:
                logger.debug(
                    f"navegar_tolerante: get lento/timed out en {url} "
                    f"({self._diagnostico_pagina()})"
                )
            except WebDriverException as e:
                if self._es_error_driver_duro(e):
                    return _marcar_driver(e)
                logger.debug(
                    f"navegar_tolerante: WebDriverException transitoria en {url} "
                    f"({type(e).__name__}: {e})"
                )
            except Exception as e:
                if self._es_error_driver_duro(e):
                    return _marcar_driver(e)

        # 2) Primera deteccion.
        estado = self._estado_pagina()
        if estado == "login":
            self.ultimo_error = self._error_sesion_navegacion(url)
            _log("login", f"err={self.ultimo_error}")
            return "login"
        if estado == "ok":
            _log("ok")
            return "ok"

        # 3) Pagina de error: UN refresh con espera acotada. Desde aqui corre
        #    el presupuesto de recuperacion (P1: incluye un posible SEGUNDO
        #    refresh; nunca mas de ~15s de esperas deliberadas).
        t_recuperacion = self._ahora()
        presupuesto_recuperacion = 15.0
        transcurrido = t_recuperacion - t_inicio
        espera_max = max(
            0.0, min(10.0, tiempo_total - transcurrido, presupuesto_recuperacion)
        )
        logger.info(
            f"navegar_tolerante: pagina de error de X al ir a {url}; "
            f"UN refresh ({self._diagnostico_pagina()})"
        )
        with _page_load_timeout_acotado(self.driver, 25.0):
            try:
                self.driver.refresh()
            except TimeoutException:
                logger.debug("navegar_tolerante: refresh lento/timed out")
            except WebDriverException as e:
                if self._es_error_driver_duro(e):
                    return _marcar_driver(e)
                logger.debug(
                    f"navegar_tolerante: refresh WebDriverException transitoria "
                    f"({type(e).__name__}: {e})"
                )
            except Exception as e:
                if self._es_error_driver_duro(e):
                    return _marcar_driver(e)

        # 4) Deteccion tras el primer refresh (polling acotado).
        fin = self._ahora() + espera_max
        while True:
            estado = self._estado_pagina()
            if estado == "login":
                self.ultimo_error = self._error_sesion_navegacion(url)
                _log("login", f"err={self.ultimo_error}")
                return "login"
            if estado == "ok":
                _log("ok", "tras refresh")
                return "ok"
            if self._ahora() >= fin:
                break
            try:
                time.sleep(0.5)
            except Exception:
                break

        # 5) P1: la pagina de error generica de X ("something went wrong") a
        #    veces no se cura con UN refresh; UN refresh ADICIONAL acotado
        #    (2 en total) dentro del presupuesto de recuperacion. NUNCA para
        #    anti-bot Cloudflare (se detecta antes de refrescar), login ni
        #    driver: esos ya retornaron arriba.
        if self.es_pagina_anti_bot():
            logger.info(
                f"navegar_tolerante: anti-bot persiste en {url}; sin refresh "
                f"adicional ({self._diagnostico_pagina()})"
            )
        else:
            restante = presupuesto_recuperacion - (self._ahora() - t_recuperacion)
            if restante <= 1.0:
                logger.info(
                    f"navegar_tolerante: sin presupuesto para el refresh "
                    f"adicional de {url} ({restante:.1f}s restantes)"
                )
            else:
                logger.info(
                    f"navegar_tolerante: la pagina de error de X sigue tras el "
                    f"refresh; refresh ADICIONAL acotado "
                    f"({self._diagnostico_pagina()})"
                )
                with _page_load_timeout_acotado(self.driver, min(8.0, restante)):
                    try:
                        self.driver.refresh()
                    except TimeoutException:
                        logger.debug(
                            "navegar_tolerante: segundo refresh lento/timed out"
                        )
                    except WebDriverException as e:
                        if self._es_error_driver_duro(e):
                            return _marcar_driver(e)
                        logger.debug(
                            f"navegar_tolerante: segundo refresh "
                            f"WebDriverException transitoria "
                            f"({type(e).__name__}: {e})"
                        )
                    except Exception as e:
                        if self._es_error_driver_duro(e):
                            return _marcar_driver(e)

                espera_max = max(
                    0.0, min(5.0, presupuesto_recuperacion - (self._ahora() - t_recuperacion))
                )
                fin = self._ahora() + espera_max
                while True:
                    estado = self._estado_pagina()
                    if estado == "login":
                        self.ultimo_error = self._error_sesion_navegacion(url)
                        _log("login", f"err={self.ultimo_error}")
                        return "login"
                    if estado == "ok":
                        _log("ok", "tras segundo refresh")
                        return "ok"
                    if self._ahora() >= fin:
                        break
                    try:
                        time.sleep(0.5)
                    except Exception:
                        break

        if not self.ultimo_error:
            self.ultimo_error = (
                f"página de error de X tras refresh: {self._diagnostico_pagina()}"
            )
        _log("error", f"err={self.ultimo_error}")
        logger.warning(f"navegar_tolerante: {self.ultimo_error}")
        return "error"

    def calentar(self) -> bool:
        """Absorbe el interstitial inicial de X al CREAR una pestaña del pool.

        Navega UNA sola vez a `x.com/home` con `navegar_tolerante` (que ya
        incluye hasta DOS refrescos acotados si aparece la pagina de error, P1).
        Se usa al CREAR una pestaña persistente; NO se llama en cada cambio de
        cuenta.

        Devuelve True si la pagina quedo usable ("ok"); en "login"/"error"/
        "driver" deja `ultimo_error` claro y devuelve False. Nunca lanza.
        """
        try:
            resultado = self.navegar_tolerante(f"{self.base_url}/home")
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.warning(f"calentar: fallo inesperado para {self.usuario}: {e}")
            return False
        if resultado == "ok":
            logger.info(f"calentar: pestaña de {self.usuario} lista para usar")
            return True
        self.ultimo_error = self.ultimo_error or (
            f"calentar: navegacion a x.com/home no usable ({resultado})"
        )
        logger.warning(f"calentar: {self.ultimo_error}")
        return False

    def _revivir_sesion_cdp(self) -> bool:
        """Fallback UNICO tras detectar muro de login con `_sesion_cdp`.

        Apaga `_sesion_cdp` y hace el login lento (`login_con_cookies`). Devuelve
        True si la sesion quedo restablecida. Solo se llama cuando el flujo ya
        detecto que X pidio login (no navega por si mismo).
        """
        if not self._sesion_cdp:
            return False
        logger.warning(
            f"Sesion CDP invalida para {self.usuario}; login lento y UN reintento"
        )
        self._sesion_cdp = False
        try:
            return bool(self.login_con_cookies())
        except Exception as e:
            logger.warning(f"Login lento de recuperacion fallo para {self.usuario}: {e}")
            return False

    def _hay_challenge_seguridad(self, revisar_url: bool = True) -> bool:
        """True si X esta mostrando un desafio de verificacion de identidad
        (login inusual) en vez de haber cargado la sesion directamente.

        `revisar_url=False` omite la heuristica por URL: dentro del propio
        flujo de `login_con_password` la URL es `/i/flow/login` durante TODO
        el proceso (tambien en los pasos normales de usuario/password), asi
        que ahi solo el texto de la pagina es una senal confiable.

        Un challenge ANTI-BOT (Cloudflare, p. ej. `/account/access?__cf_chl_...`)
        devuelve False: no es el desafio de identidad de X y no se debe gastar
        password/TOTP. Ese caso lo maneja `es_pagina_anti_bot()`.
        """
        if self.es_pagina_anti_bot():
            return False
        if revisar_url:
            try:
                url = (self.driver.current_url or "").lower()
            except Exception:
                url = ""
            if "challenge" in url or "flow/login" in url or "account/access" in url:
                return True
        try:
            src = self.driver.page_source.lower()
        except Exception:
            return False
        senales = (
            "verify your identity", "verifica tu identidad", "confirm your identity",
            "confirma tu identidad", "unusual login activity", "actividad de inicio de "
            "sesion inusual",
        )
        return any(s in src for s in senales)

    def _resolver_challenge_seguridad(self, password: str, totp_secret: str, timeout: int = 20) -> bool:
        """Intenta pasar el desafio de seguridad de X usando `password` y el
        codigo TOTP derivado de `totp_secret` (ambos guardados en la BD junto
        al auth_token). Devuelve True si el desafio quedo resuelto."""
        logger.info(f"Resolviendo challenge de seguridad para {self.usuario}...")

        if password:
            password_input = None
            for sel in ("input[name='password']", "input[type='password']"):
                try:
                    password_input = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    break
                except Exception:
                    continue
            if password_input is not None:
                try:
                    password_input.click()
                    password_input.send_keys(password)
                    time.sleep(1)
                    self._clic_texto_visible(
                        "iniciar sesion", "log in", "entrar", "sign in",
                        "iniciar sesión", "siguiente", "next", "confirmar", "confirm",
                    )
                    time.sleep(3)
                except Exception as e:
                    logger.warning(f"No se pudo enviar password en challenge de {self.usuario}: {e}")

        if totp_secret:
            codigo_input = None
            for sel in (
                "input[data-testid='ocfEnterTextTextInput']",
                "input[name='text']",
                "input[name='challenge_response']",
                "input[type='text']",
            ):
                try:
                    codigo_input = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    break
                except Exception:
                    continue
            if codigo_input is not None:
                try:
                    import pyotp
                    codigo = pyotp.TOTP(totp_secret.replace(" ", "")).now()
                    codigo_input.click()
                    codigo_input.send_keys(codigo)
                    time.sleep(1)
                    self._clic_texto_visible(
                        "siguiente", "next", "confirmar", "confirm",
                        "verificar", "verify", "continuar", "continue",
                    )
                    time.sleep(3)
                except Exception as e:
                    logger.warning(f"No se pudo enviar TOTP en challenge de {self.usuario}: {e}")

        fin = time.time() + timeout
        while time.time() < fin:
            if not self._hay_challenge_seguridad():
                try:
                    url = (self.driver.current_url or "").lower()
                except Exception:
                    url = ""
                return "login" not in url
            time.sleep(1)

        logger.warning(f"El challenge de seguridad de {self.usuario} no se resolvio a tiempo")
        return False

    def esperar_login_manual(self, usuario: str) -> bool:
        try:
            if not self.iniciar_driver():
                return False
            
            self.driver.get(f"{self.base_url}/i/flow/login")
            
            logger.info(f"Esperando login manual para {usuario}...")
            logger.info("Haz login en el navegador y presiona Enter aqui cuando termines...")
            
            timeout = 300
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                current_url = self.driver.current_url
                
                if "home" in current_url or "twitter.com" not in current_url:
                    time.sleep(3)
                    
                    if "login" not in self.driver.current_url.lower():
                        self.guardar_cookies()
                        logger.info(f"Login manual exitoso para {usuario}")
                        return True
                
                time.sleep(2)
            
            logger.warning(f"Timeout en login manual para {usuario}")
            return False
        
        except Exception as e:
            logger.error(f"Error en login manual: {e}")
            return False

    def _limpiar_cookies_navegador(self) -> bool:
        """Borra TODAS las cookies del navegador (CDP + Selenium best-effort).

        Se usa antes de un login con password para no arrastrar el auth_token
        invalido de la sesion anterior. El CDP no depende del documento actual.
        Nunca lanza.
        """
        self._sesion_cdp = False
        limpio = False
        try:
            self.driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
            limpio = True
        except Exception as e:
            logger.debug(f"clearBrowserCookies CDP fallo: {e}")
        try:
            self.driver.delete_all_cookies()
            limpio = True
        except Exception as e:
            logger.debug(f"delete_all_cookies fallo: {e}")
        return limpio

    def login_con_password(self, password: str, timeout: int = 60, totp_secret: str = "") -> bool:
        """Login automatizado con usuario + contraseña y guarda las cookies.

        Abre x.com/i/flow/login, escribe el usuario, avanza, escribe la
        contraseña y confirma. Si termina en home (o sin 'login' en la URL),
        guarda las cookies y devuelve True. Devuelve False si algo falla o si
        tarda más de 'timeout' segundos.

        Si X responde con un desafio de verificacion de identidad (comun en
        logins automatizados) y hay `totp_secret`, intenta resolverlo antes
        de declarar el intento como fallido.
        """
        # Reuso seguro: si ya hay un driver (fallback desde
        # `login_con_cookies_json` con un auth_token muerto) NO se lanza otro
        # Chrome y se limpian las cookies de la sesion anterior para que X
        # muestre el flow de login limpio.
        if not self.driver and not self.iniciar_driver():
            return False
        if self.driver:
            self._limpiar_cookies_navegador()

        if not totp_secret:
            try:
                from core.database import get_db_session
                from core.models import Cuenta
                with get_db_session() as db:
                    cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                    totp_secret = (cuenta.totp_secret or "").strip() if cuenta else ""
            except Exception as e:
                logger.warning(f"No se pudo leer el totp_secret de {self.usuario}: {e}")

        try:
            self.driver.get(f"{self.base_url}/i/flow/login")
            time.sleep(3)

            # 1) Campo de usuario (primer paso del login de X).
            usuario_input = None
            for sel in (
                "input[name='text']",
                "input[autocomplete='username']",
                "input[type='text']",
            ):
                try:
                    usuario_input = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    break
                except Exception:
                    continue
            if usuario_input is None:
                logger.warning(f"No se encontro el campo de usuario para {self.usuario}")
                return False

            usuario_input.click()
            usuario_input.send_keys(self.usuario)

            # Boton "Siguiente"/"Next" tras el usuario.
            self._clic_texto_visible("siguiente", "next", "continuar", "continue")

            # 2) Campo de contraseña.
            password_input = None
            for sel in ("input[name='password']", "input[type='password']"):
                try:
                    password_input = WebDriverWait(self.driver, 15).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                    )
                    break
                except Exception:
                    continue
            if password_input is None:
                logger.warning(f"No se encontro el campo de contraseña para {self.usuario}")
                return False

            password_input.click()
            password_input.send_keys(password)
            time.sleep(1)

            # Boton "Iniciar sesión"/"Log in"/"Entrar".
            self._clic_texto_visible(
                "iniciar sesion", "log in", "entrar", "sign in", "iniciar sesión"
            )

            # 3) Esperar a entrar (home o fuera de login), resolviendo un
            # posible desafio de verificacion de identidad en el camino.
            challenge_intentado = False
            fin = time.time() + timeout
            while time.time() < fin:
                try:
                    url = self.driver.current_url.lower()
                except Exception:
                    url = ""

                if not challenge_intentado and self._hay_challenge_seguridad(revisar_url=False):
                    challenge_intentado = True
                    self._resolver_challenge_seguridad(password, totp_secret)
                    time.sleep(1)
                    continue

                if "login" not in url or "home" in url or f"/{self.usuario.lower()}" in url:
                    time.sleep(2)
                    if "login" not in url:
                        cookies = self.driver.get_cookies()
                        self._brandear_cuenta(cookies)
                        logger.info(f"Login con password exitoso para {self.usuario}, cuenta brandeada")
                        return True
                time.sleep(2)

            logger.warning(f"Timeout en login con password para {self.usuario}")
            return False

        except Exception as e:
            logger.error(f"Error en login con password para {self.usuario}: {e}")
            return False

    # XPath 1.0: minúsculas + sin acentos para comparar textos de botones.
    _XPATH_TXT_MAYUS = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚÑÜáéíóúñü"
    )
    _XPATH_TXT_MINUS = (
        "abcdefghijklmnopqrstuvwxyzaeiounuaeiounu"
    )

    def _clic_texto_visible(self, *textos) -> bool:
        """Clica el primer elemento clickeable cuyo texto contiene alguno de 'textos'.

        La UI de X usa `div[role='button']` ademas de `button` y acentos
        ("Iniciar sesión"): se comparan AMBOS, en minusculas y sin acentos
        (XPath `translate`). Sin esto, `login_con_password` no encontraba el
        boton "Siguiente"/"Iniciar sesion" y el flujo moria en el primer paso.
        """
        for texto in textos:
            t = str(texto or "").strip().lower()
            if not t:
                continue
            xpaths = (
                "//button[contains(translate(., "
                f"'{self._XPATH_TXT_MAYUS}', '{self._XPATH_TXT_MINUS}'), '{t}')]",
                "//*[@role='button'][contains(translate(., "
                f"'{self._XPATH_TXT_MAYUS}', '{self._XPATH_TXT_MINUS}'), '{t}')]",
            )
            for xpath in xpaths:
                try:
                    elem = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    elem.click()
                    return True
                except Exception:
                    continue
        return False

    def guardar_cookies(self) -> bool:
        try:
            cookies = self.driver.get_cookies()
            os.makedirs(os.path.dirname(self.cookies_path), exist_ok=True)
            
            with open(self.cookies_path, "wb") as f:
                pickle.dump(cookies, f)
            
            logger.info(f"Cookies guardadas para {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error guardando cookies: {e}")
            return False
    
    def comportamiento_humano_visualizacion(self):
        try:
            scroll_px = random.randint(200, 400)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_px})")
            time.sleep(random.uniform(0.5, 1.2))
            
            if random.random() < 0.15:
                scroll_up = random.randint(100, 300)
                self.driver.execute_script(f"window.scrollBy(0, -{scroll_up})")
                time.sleep(random.uniform(0.3, 0.8))
            
            time.sleep(random.uniform(0.5, 1.5))
        
        except Exception as e:
            logger.error(f"Error en comportamiento humano: {e}")
    
    def delay_entre_visualizaciones(self, base_delay: float = 3.0):
        delay = base_delay + random.uniform(0.5, 1.5)
        time.sleep(delay)
    
    def _es_tweet_fijado(self, tweet) -> bool:
        """True si el `article` es el tweet fijado (Pinned/Fijado) del perfil.

        Evita que `_obtener_ultimo_enlace` / `_obtener_url_respuesta` devuelvan
        el tweet fijado en lugar de la publicacion recien hecha.
        """
        try:
            for ctx in tweet.find_elements(By.CSS_SELECTOR, "[data-testid='socialContext']"):
                txt = (ctx.text or "").lower()
                if "pinned" in txt or "fijado" in txt:
                    return True
        except Exception:
            pass
        return False

    # Regex de una URL de PUBLICACION (host twitter.com/x.com + /status/<id>).
    _RE_URL_STATUS = re.compile(
        r"^https?://(?:[a-z0-9-]+\.)*(?:twitter\.com|x\.com)/"
        r"([^/?#]+)/status/\d+",
        re.IGNORECASE,
    )

    def _handles_propios(self) -> set:
        """Handles que cuentan como "la propia cuenta" (en minusculas).

        Usa `self.usuario` (clave interna) y, si el bot conoce el handle real
        (`handle_actual`/`nombre_usuario`...), tambien lo acepta: tras un cambio
        de @ la clave interna puede no coincidir con el handle de X. Nunca
        lanza; sin handles devuelve un set vacio.
        """
        handles: set = set()
        for attr in ("usuario", "handle_actual", "handle_propuesto",
                     "nombre_usuario", "screen_name"):
            try:
                valor = str(getattr(self, attr, "") or "").strip().lstrip("@").lower()
            except Exception:
                continue
            if valor:
                handles.add(valor)
        return handles

    def _obtener_enlace_reciente_timeline(
        self, texto_publicado: str = "", timeout: float = 3.0
    ) -> Optional[str]:
        """Ruta RAPIDA: link de la publicacion recien hecha en el timeline actual.

        Tras publicar, X deja la pestana en `/home` (o en el compositor) y los
        PRIMEROS articulos del timeline suelen incluir el post recien hecho. Se
        escanea ese timeline SIN navegar (barato, ~1-3s) buscando un
        `a[href*='/status/']` cuya URL sea de la PROPIA cuenta
        (`/<usuario>/status/`; tambien el handle real si el bot lo conoce) y, si
        `texto_publicado` viene, se exige que el articulo contenga sus primeros
        ~20 caracteres (evita confundir un post viejo con el nuevo).

        Devuelve la URL sin query string, o None si no aparece en `timeout`.
        Nunca lanza.
        """
        if not self.driver:
            return None
        propios = self._handles_propios()
        if not propios:
            return None
        try:
            referencia = " ".join(str(texto_publicado or "").split())[:20]
        except Exception:
            referencia = ""
        try:
            fin = self._ahora() + max(0.2, float(timeout))
        except Exception:
            fin = self._ahora() + 3.0
        while True:
            try:
                articulos = self.driver.find_elements(
                    By.CSS_SELECTOR, "article[data-testid='tweet']"
                )
            except Exception:
                articulos = []
            for articulo in (articulos or [])[:4]:
                try:
                    if self._es_tweet_fijado(articulo):
                        continue
                    if referencia:
                        contenido = " ".join((articulo.text or "").split())
                        if referencia.lower() not in contenido.lower():
                            continue
                    for enlace in articulo.find_elements(
                        By.CSS_SELECTOR, "a[href*='/status/']"
                    ):
                        href = str(enlace.get_attribute("href") or "").strip()
                        m = self._RE_URL_STATUS.match(href)
                        if not m:
                            continue
                        if m.group(1).lstrip("@").lower() not in propios:
                            continue
                        return href.split("?")[0].split("#")[0].rstrip("/")
                except Exception:
                    continue
            try:
                if self._ahora() >= fin:
                    return None
            except Exception:
                return None
            try:
                time.sleep(0.25)
            except Exception:
                return None

    def _obtener_ultimo_enlace(self, usuario: str) -> Optional[str]:
        try:
            try:
                self.driver.get(f"{self.base_url}/{usuario}")
            except TimeoutException:
                # Carga lenta (proxy intermitente): el HTML suele seguir
                # llegando; las esperas explicitas de elementos deciden.
                logger.warning(
                    f"Carga lenta de {self.base_url}/{usuario}; sigo con esperas explicitas"
                )
            # La espera fija de 4s se cambio por: esperar a que exista el
            # articulo (hasta 5s) + una pausa corta para que el timeline pinte
            # la publicacion recien hecha.
            self._esperar_article_tweet(timeout=5)
            time.sleep(random.uniform(0.3, 0.6))
            
            tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            
            # Salta el tweet fijado: no es la publicacion recien hecha.
            for tweet in tweets[:5]:
                try:
                    if self._es_tweet_fijado(tweet):
                        continue
                    enlace = tweet.find_element(By.CSS_SELECTOR, "a[href*='/status/']")
                    return enlace.get_attribute("href")
                except Exception:
                    continue
            
            return None
        
        except Exception as e:
            logger.error(f"Error obteniendo ultimo enlace: {e}")
            return None
    
    def obtener_ultimos_tweets(self, usuario: str, cantidad: int = 5) -> list[str]:
        try:
            self.driver.get(f"{self.base_url}/{usuario}")
            time.sleep(4)
            
            enlaces = []
            tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            
            for tweet in tweets[:cantidad]:
                try:
                    enlace = tweet.find_element(By.CSS_SELECTOR, "a[href*='/status/']")
                    enlaces.append(enlace.get_attribute("href"))
                except:
                    continue
            
            return enlaces
        
        except Exception as e:
            logger.error(f"Error obteniendo tweets: {e}")
            return []
    
    def contar_tweets_periodo(
        self,
        usuario: str,
        horas: int = 24,
        fecha_inicio: str = None,
        fecha_fin: str = None
    ) -> dict:
        resultado = {"tweets": 0, "retweets": 0, "total": 0}
        
        try:
            self.driver.get(f"{self.base_url}/{usuario}")
            time.sleep(4)
            
            fecha_limite = datetime.now() - timedelta(hours=horas)
            
            scroll_count = 0
            max_scrolls = 20
            
            while scroll_count < max_scrolls:
                tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
                
                for tweet in tweets:
                    try:
                        texto = tweet.text.lower()
                        
                        es_retweet = "retweeted" in texto or "reposteó" in texto
                        
                        if es_retweet:
                            resultado["retweets"] += 1
                        else:
                            resultado["tweets"] += 1
                    except:
                        continue
                
                self.driver.execute_script("window.scrollBy(0, 1000)")
                time.sleep(2)
                scroll_count += 1
            
            resultado["total"] = resultado["tweets"] + resultado["retweets"]
            
            logger.info(f"Conteo para @{usuario}: {resultado['tweets']} tweets, {resultado['retweets']} retweets")
        
        except Exception as e:
            logger.error(f"Error contando tweets: {e}")
        
        return resultado
    
    def _detectar_limite_cuenta(self) -> bool:
        try:
            page_source = self.driver.page_source.lower()
            
            limites = [
                "temporarily limited",
                "suspicious activity",
                "account is suspended",
                "are you a robot",
                "unusual activity",
                "verify your identity"
            ]
            
            for limite in limites:
                if limite in page_source:
                    logger.warning(f"Limite detectado: {limite}")
                    return True
            
            return False

        except:
            return False

    def _detectar_cuenta_propia_suspendida(self) -> bool:
        """Detecta si la CUENTA CON LA QUE SE INICIO SESION (no el tweet/
        cuenta objetivo) fue suspendida/bloqueada por X. A diferencia de
        `_detectar_limite_cuenta()` (limites blandos y temporales), esto solo
        reconoce frases de bloqueo permanente para no marcar como baneada una
        cuenta con un limite recuperable.

        FALSO POSITIVO ARREGLADO: `/account/access` NO es prueba de suspension
        por si solo; X/CDN lo usa tambien para el challenge anti-bot de
        Cloudflare. Antes, `https://x.com/account/access?__cf_chl_rt_tk=...`
        con title "Just a moment..." marcaba cuentas BUENAS como suspendidas y
        el motor las desactivaba (`marcar_cuenta_suspendida`). Ahora un
        challenge anti-bot devuelve False de inmediato y el title con
        "suspended" solo cuenta si NO es anti-bot.
        """
        try:
            if self.es_pagina_anti_bot():
                return False

            url_actual = (self.driver.current_url or "").lower()
            if "/suspended" in url_actual:
                return True

            page_source = self.driver.page_source.lower()
            frases = [
                "your account is suspended",
                "we suspended your account",
                "we've suspended your account",
                "your account has been locked",
                "tu cuenta ha sido suspendida",
                "tu cuenta fue suspendida",
                "hemos suspendido tu cuenta",
            ]
            if any(frase in page_source for frase in frases):
                return True

            # Titulo con "suspended" SOLO si la pagina no es anti-bot (arriba).
            try:
                titulo = (self.driver.title or "").lower()
            except Exception:
                titulo = ""
            return "suspended" in titulo
        except Exception:
            return False

    def _detectar_tweet_no_disponible(self) -> str:
        """Revisa si la pagina del tweet objetivo cargo pero no es
        retwitteable (borrado, protegido/privado o cuenta suspendida).
        Devuelve el motivo detectado o "" si no hay indicios."""
        try:
            page_source = self.driver.page_source.lower()

            motivos = {
                "this post is unavailable": "tweet no disponible/borrado",
                "this tweet is unavailable": "tweet no disponible/borrado",
                "hmm...this page doesn't exist": "tweet no disponible/borrado",
                "these posts are protected": "cuenta protegida/privada",
                "this account is protected": "cuenta protegida/privada",
                "this account doesn't exist": "cuenta no existe",
                "account suspended": "cuenta objetivo suspendida",
            }
            for frase, motivo in motivos.items():
                if frase in page_source:
                    return motivo
            return ""
        except Exception:
            return ""

    def publicar_tweet(
        self,
        contenido: str,
        imagen_path: Optional[str] = None,
        buscar_url: bool = True,
    ) -> Optional[str]:
        """Publica un tweet y devuelve la URL del post recien publicado.

        Devuelve la URL del tweet publicado (str) si se obtuvo, `True` como
        fallback truthy si se publico pero no se pudo extraer la URL, o `None`
        si la publicacion fallo.

        `buscar_url=False` omite buscar la URL (cuesta datos de proxy por cada
        post): para campanas masivas basta la verificacion real de la
        publicacion. En ese caso `ultima_url_publicada` queda vacia y se
        devuelve `True`. Con `buscar_url=True` se intenta PRIMERO la ruta rapida
        (`_obtener_enlace_reciente_timeline`, sin navegar) y, si no hay link, se
        visita el perfil (`_obtener_ultimo_enlace`).
        """
        if not self.driver:
            if not self.login_con_cookies():
                return None
        
        if self._detectar_limite_cuenta():
            self.ultimo_error = "cuenta limitada por X"
            logger.error("Cuenta limitada, saltando publicacion")
            return None
        
        t_inicio = self._ahora()
        tiempos = {"compose": t_inicio, "escribir": t_inicio}
        try:
            # Compositor de POST NUEVO: prueba /compose/post, /compose/tweet y
            # el boton "Nuevo post" de /home, y distingue la sesion caida o
            # una pagina de error de X del simple retraso de la SPA (ver
            # `_abrir_compositor`). El editor devuelto es SIEMPRE visible
            # (evita escribir en un composer oculto que deja el boton Post
            # deshabilitado).
            def _preparar_borrador() -> str:
                """Abre el compositor y deja el contenido escrito (sin pulsar Post)."""
                try:
                    editor = self._abrir_compositor()
                except Exception as e_compositor:
                    # Sesion CDP invalida: UN fallback a login lento y UN reintento.
                    if not (
                        self._sesion_cdp
                        and self._es_error_fatal_compositor(e_compositor)
                        and self._revivir_sesion_cdp()
                    ):
                        raise
                    editor = self._abrir_compositor()
                tiempos["compose"] = self._ahora()

                texto = self._recortar_para_x(self._reorganizar_hashtags(contenido))
                self._pegar_texto(editor, texto)

                if imagen_path and os.path.exists(imagen_path):
                    self._subir_imagen(imagen_path)

                time.sleep(random.uniform(0.15, 0.35))
                tiempos["escribir"] = self._ahora()
                return texto

            try:
                contenido = _preparar_borrador()
            except Exception as e_borrador:
                # Blindaje: fallo TRANSITORIO del compositor/escritura (pagina
                # de error o interstitial de X, editor bloqueado por la mascara
                # del modal). Nada se publico todavia, asi que UN CICLO LIMPIO
                # completo (refresh corto + recomponer + reescribir) es seguro.
                # Sesion caida, anti-bot y driver roto NO se reintentan (los
                # reconoce el motor). Mientras el reintento funcione todo queda
                # en info/debug: sin errores ruidosos en la consola de Railway.
                if (
                    not self._es_fallo_transitorio_compositor(e_borrador)
                    or not self.esta_vivo()
                ):
                    raise
                logger.info(
                    f"compositor/escritura con fallo transitorio @{self.usuario} "
                    f"({type(e_borrador).__name__}: {e_borrador}); ciclo limpio "
                    "(refresh corto + recomponer)"
                )
                self._refresh_corto(3)
                contenido = _preparar_borrador()
            t_compose = tiempos["compose"]
            t_escribir = tiempos["escribir"]

            # Pausa humana: el tiempo que una persona tarda en releer el tuit
            # antes de enviarlo. Ademas, da margen a que X procese los eventos
            # reales de teclado que escribio `_pegar_texto`.
            logger.debug(
                f"pausa humana antes de publicar @{self.usuario} (1.8-3.5s)"
            )
            time.sleep(random.uniform(1.8, 3.5))

            # Esperar a que el boton "Post" se HABILITE: X lo mantiene
            # deshabilitado hasta que el editor registra el texto. Si en 10s no
            # se habilita, el texto no quedo en el editor y hay que cortar aqui
            # (antes se clicaba un boton muerto y se reportaba un falso
            # "X no confirmo la publicacion").
            publicar_btn = self._esperar_boton_post_habilitado(10, texto=contenido)
            if publicar_btn is None:
                raise Exception(
                    "boton Post deshabilitado: el texto no quedo registrado en el editor "
                    f"({len(contenido)} chars)"
                )
            self.driver.execute_script("arguments[0].click();", publicar_btn)
            
            # Verificar que el tweet REALMENTE se publico (no basta con hacer clic)
            if not self._verificar_publicacion():
                # _verificar_publicacion deja el detalle real (p. ej. toast de
                # error de X) en ultimo_error; solo se rellena si quedo vacio.
                self.ultimo_error = self.ultimo_error or "X no confirmó la publicación"
                logger.error(f"No se confirmo la publicacion del tweet por {self.usuario}")
                try:
                    self.driver.save_screenshot(resolver_ruta("data/temp/twitter_no_publicado.png"))
                    logger.error("Captura guardada: data/temp/twitter_no_publicado.png")
                except:
                    pass
                return None
            t_confirmado = self._ahora()
            
            logger.info(f"Tweet publicado por {self.usuario}")
            logger.info(
                f"perf @{self.usuario}: compose={t_compose - t_inicio:.1f}s "
                f"escribir={t_escribir - t_compose:.1f}s "
                f"publicar={t_confirmado - t_escribir:.1f}s "
                f"total={t_confirmado - t_inicio:.1f}s"
            )
            
            # 5 segundos de vista a la pantalla para confirmacion visual.
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 5s la pantalla visible para confirmacion visual...")
                time.sleep(5)
            
            if not buscar_url:
                # Activaciones masivas: visitar el perfil solo para conocer la
                # URL del tweet cuesta ~10-15s y datos de proxy por cada post;
                # la verificacion real del toast ya confirma la publicacion.
                logger.debug(
                    f"Búsqueda de URL omitida (buscar_url=False) para {self.usuario}"
                )
                self.ultima_url_publicada = ""
                return True

            # Ruta RAPIDA (~1-3s): el post recien publicado suele estar en los
            # primeros articulos del timeline actual (X aterriza en /home o
            # /compose) y no hace falta navegar. Solo si falla se visita el
            # perfil (`_obtener_ultimo_enlace`, ~5-10s).
            url = self._obtener_enlace_reciente_timeline(contenido)
            if not url:
                url = self._obtener_ultimo_enlace(self.usuario)
            self.ultima_url_publicada = url or ""
            return url or True   # True como fallback truthy si no se pudo obtener la URL
        
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error publicando tweet para {self.usuario}: {e}")
            try:
                self.driver.save_screenshot(resolver_ruta("data/temp/twitter_error_publish.png"))
                logger.error(f"Captura de pantalla guardada: data/temp/twitter_error_publish.png")
            except:
                pass
            return None

    # Frases con las que X RECHAZA un post (toast o aviso en la pagina). Si
    # aparecen durante la verificacion, se corta de inmediato: el post NO se
    # publico y hay que reportar el motivo real, no un timeout.
    _TOASTS_ERROR = (
        "something went wrong",
        "algo salió mal",
        "no pudimos enviar",
        "try again",
        "inténtalo de nuevo",
        "you are over the daily limit",
        "rate limit",
        "unable to send",
    )

    def _texto_toast(self) -> str:
        """Devuelve el texto del toast/alert visible de X ('' si no hay)."""
        partes = []
        for sel in (
            "[data-testid='toast']",
            "[role='alert']",
            "[data-testid='toast'] span",
        ):
            try:
                for elem in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if elem.is_displayed():
                            texto = (elem.text or "").strip()
                            if texto:
                                partes.append(texto)
                    except Exception:
                        continue
            except Exception:
                continue
        return " ".join(partes)

    def _verificar_publicacion(self, tiempo_max: int = 15) -> bool:
        """Confirma que el tweet realmente se publico.

        Tras publicar, X muestra un toast ('Your post was sent' / 'Tu post fue
        enviado') y saca de /compose/post. Si X RECHAZA el post aparece un toast
        de error: se detecta, se corta antes de agotar `tiempo_max` y el motivo
        queda en `self.ultimo_error`. Los `TimeoutException` del renderer
        durante el polling se toleran (se cuentan y se avisa a los 3 seguidos),
        porque en Railway el renderer se satura y lanza timeouts transitorios.
        Al fallar de verdad se registra URL + fragmento de la pagina.
        """
        senales = [
            "your post was sent",
            "your reply was sent",
            "tu post fue enviado",
            "tu post se envió",
            "tu publicación fue enviada",
            "tu respuesta fue enviada",
            "tweet publicado",
        ]
        inicio = time.time()
        fallos_renderer = 0
        while time.time() - inicio < tiempo_max:
            try:
                url = self.driver.current_url.lower()
                src = self.driver.page_source.lower()
                fallos_renderer = 0

                if any(s in src for s in senales):
                    return True

                # Toasts de rechazo de X: cortar YA (no esperar el timeout).
                toast = self._texto_toast()
                fuente = f"{src} {(toast or '').lower()}"
                for frase in self._TOASTS_ERROR:
                    if frase in fuente:
                        fragmento = re.sub(r"\s+", " ", (toast or frase)).strip()[:120]
                        self.ultimo_error = f"X rechazó el post: {fragmento}"
                        logger.error(
                            f"X rechazo la publicacion de {self.usuario}: {fragmento}"
                        )
                        return False

                if "compose" not in url and ("home" in url or "status" in url):
                    try:
                        self.driver.find_element(By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']")
                    except Exception:
                        return True

            except Exception:
                # TimeoutException del renderer: transitorio, no cambia el
                # resultado; solo se avisa si se encadenan 3+ seguidos.
                fallos_renderer += 1
                if fallos_renderer >= 3 and fallos_renderer % 3 == 0:
                    logger.warning(
                        "renderer sin responder durante la verificacion de publicacion"
                    )
            time.sleep(1)

        # Diagnostico final antes de devolver False.
        try:
            url_actual = self.driver.current_url
        except Exception:
            url_actual = "(no disponible)"
        try:
            toast = (self._texto_toast() or "").strip()
        except Exception:
            toast = ""
        try:
            pagina = self.driver.page_source
        except Exception:
            pagina = ""
        fragmento = toast or re.sub(r"<[^>]+>", " ", pagina)
        fragmento = re.sub(r"\s+", " ", fragmento).strip()[:300]
        logger.error(
            f"No se confirmo la publicacion de {self.usuario}. "
            f"URL: {url_actual} | fragmento: {fragmento}"
        )
        return False
    
    def _buscar_boton_post(self):
        """Busqueda forzosa del boton 'Post'/'Publicar'. Prueba varios selectores
        y cae en una busqueda por texto visible como ultimo recurso."""
        selectores = [
            "[data-testid='tweetButtonInline']",
            "[data-testid='tweetButton']",
            "button[data-testid='tweetButtonInline']",
        ]
        for sel in selectores:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed() and btn.is_enabled():
                    logger.debug(f"Boton Post encontrado con selector: {sel}")
                    return btn
            except Exception:
                continue

        logger.debug("Buscando boton Post por texto visible...")
        xpaths = [
            "//span[text()='Post']/ancestor::*[self::div[@role='button']]",
            "//span[text()='Post']",
            "//span[text()='Publicar']",
            "//div[@role='button'][.//span[text()='Post']]",
            "//div[@role='button'][.//span[text()='Publicar']]",
        ]
        for xp in xpaths:
            try:
                btn = self.driver.find_element(By.XPATH, xp)
                if btn.is_displayed():
                    logger.debug(f"Boton Post encontrado por texto con XPath: {xp}")
                    return btn
            except Exception:
                continue

        raise Exception("No se encontro el boton 'Post'/'Publicar' en la pagina")

    def _post_habilitado(self, boton) -> bool:
        """True si el boton Post esta realmente habilitado.

        `is_enabled()` no ve `aria-disabled`: X usa `div[role='button']` con
        `aria-disabled='true'` cuando el editor esta vacio. Se revisa el propio
        boton y su ancestro `[role='button']/button` mas cercano.
        """
        try:
            if not boton.is_enabled():
                return False
        except Exception:
            return False
        try:
            if (boton.get_attribute("aria-disabled") or "").lower() == "true":
                return False
        except Exception:
            pass
        try:
            # Recorre el boton y sus ancestros `[role='button']/button`: X marca
            # `aria-disabled='true'` tanto en el propio div como en un wrapper.
            estado = self.driver.execute_script(
                "var el = arguments[0];"
                "while (el) {"
                "  if (el.getAttribute && el.getAttribute('aria-disabled') === 'true') return 'true';"
                "  if (el.disabled) return 'true';"
                "  var p = el.parentElement ? el.parentElement.closest('[role=\\\"button\\\"],button') : null;"
                "  if (!p || p === el) break;"
                "  el = p;"
                "}"
                "return '';",
                boton,
            )
            return (estado or "").lower() != "true"
        except Exception:
            return True

    def _esperar_boton_post_habilitado(self, timeout: int = 10, texto: str = ""):
        """Espera hasta `timeout`s a que el boton "Post" se habilite.

        X mantiene el boton deshabilitado mientras el editor no tenga texto.
        Devuelve el boton habilitado o None si no aparecio en el plazo. `texto`
        (opcional) es el texto que se intento escribir, para el diagnostico.
        """
        fin = time.time() + max(1, timeout)
        while time.time() < fin:
            try:
                posible = self._buscar_boton_post()
            except Exception:
                posible = None
            if posible is not None:
                if self._post_habilitado(posible):
                    logger.info("Boton Post habilitado, procediendo a publicar")
                    return posible
            time.sleep(0.5)

        # Diagnostico al fallar: longitud del texto que realmente quedo en el
        # editor VISIBLE (si se puede leer) y del que se intento escribir. Se
        # usa la lectura EXACTA: `_leer_texto_editor` concatena `.text` +
        # `textContent` + `innerText` y reportaba longitudes 2-3x infladas.
        largo_editor = None
        try:
            editor = self._primer_editor_visible()
            if editor is not None:
                largo_editor = self._largo_editor(editor)
        except Exception:
            largo_editor = None
        detalle_editor = (
            f"{largo_editor} chars en el editor visible"
            if largo_editor is not None
            else "no se pudo leer el editor visible"
        )
        logger.warning(
            f"El boton Post no se habilito en {timeout}s "
            f"(se intentaron escribir {len(texto or '')} chars; {detalle_editor})"
        )
        return None

    def _buscar_opcion_quote(self, timeout: float = 10.0):
        """Busca la opcion 'Quote' (Citar) del menu desplegable de retweet.
        En X el menu muestra 'Repost' y 'Quote'; el quote usa data-testid='quote'.

        El menu puede tardar en renderizar con carga lenta, por eso se
        reintenta con los MISMOS selectores hasta `timeout`s (0.5s entre
        intentos) antes de lanzar el error.
        """
        selectores = [
            "[data-testid='quote']",
            "a[href*='/intent/post']",
        ]
        xpaths = [
            "//span[text()='Quote']",
            "//span[text()='Citar']",
            "//div[@role='menuitem'][.//span[text()='Quote']]",
            "//div[@role='menuitem'][.//span[text()='Citar']]",
            "//a[@role='menuitem'][.//span[text()='Quote']]",
            "//a[@role='menuitem'][.//span[text()='Citar']]",
        ]
        fin = time.time() + max(0.5, timeout)
        while True:
            for sel in selectores:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if btn.is_displayed():
                        logger.info(f"Opcion Quote encontrada con selector: {sel}")
                        return btn
                except Exception:
                    continue

            for xp in xpaths:
                try:
                    btn = self.driver.find_element(By.XPATH, xp)
                    if btn.is_displayed():
                        logger.info(f"Opcion Quote encontrada por texto con XPath: {xp}")
                        return btn
                except Exception:
                    continue

            if time.time() >= fin:
                break
            time.sleep(0.5)

        raise Exception("No se encontro la opcion 'Quote'/'Citar' en el menu de retweet")
    
    def publicar_hilo(self, tweets: list[str], usuario: str, imagen_path: Optional[str] = None) -> Optional[str]:
        """Publica un hilo de tweets y devuelve la URL del primer tweet.

        Devuelve la URL del primer tweet del hilo (str) si se obtuvo, `True`
        como fallback truthy si se publico pero no se pudo extraer la URL, o
        `None` si la publicacion fallo.
        """
        if not self.driver:
            if not self.login_con_cookies():
                return None
        
        try:
            # Compositor de POST NUEVO (mismo helper que `publicar_tweet`):
            # /compose/post, /compose/tweet y el boton "Nuevo post" de /home.
            editor = self._abrir_compositor()
            
            for idx, tweet_texto in enumerate(tweets):
                # El primer editor lo devuelve `_abrir_compositor`; los
                # siguientes (tweets 2..n) los monta "agregar" y hay que
                # esperarlos de nuevo.
                if idx > 0:
                    editor = self._esperar_editor_visible()
                
                texto = self._reorganizar_hashtags(tweet_texto)
                texto = self._recortar_para_x(texto)
                self._pegar_texto(editor, texto)
                
                time.sleep(1)
                
                if idx < len(tweets) - 1:
                    agregar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='addButton']")
                    self.driver.execute_script("arguments[0].click();", agregar_btn)
                    time.sleep(1)
                
                if imagen_path and idx == 0:
                    self._subir_imagen(imagen_path)
            
            # Busqueda forzosa del boton "Post" (por texto y por testid)
            publicar_btn = self._buscar_boton_post()
            self.driver.execute_script("arguments[0].click();", publicar_btn)
            
            if not self._verificar_publicacion():
                logger.error(f"No se confirmo la publicacion del hilo por {self.usuario}")
                try:
                    self.driver.save_screenshot(resolver_ruta("data/temp/twitter_no_publicado.png"))
                except:
                    pass
                return None
            
            logger.info(f"Hilo publicado por {self.usuario}: {len(tweets)} tweets")
            
            # 5 segundos de vista a la pantalla para confirmacion visual.
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 5s la pantalla visible para confirmacion visual...")
                time.sleep(5)
            
            url = self._obtener_ultimo_enlace(self.usuario)
            self.ultima_url_publicada = url or ""
            return url or True   # True como fallback truthy si no se pudo obtener la URL
        
        except Exception as e:
            logger.error(f"Error publicando hilo: {e}")
            try:
                self.driver.save_screenshot(resolver_ruta("data/temp/twitter_error_hilo.png"))
                logger.error(f"Captura de pantalla guardada: data/temp/twitter_error_hilo.png")
            except:
                pass
            return None
    
    # ------------------------------------------------------------------
    # Respuestas / comentarios a tweets existentes
    # ------------------------------------------------------------------
    # NOTA: los selectores de la UI de X cambian con frecuencia. Estos metodos
    # usan varios selectores de respaldo (data-testid / rol textbox / texto
    # visible) y verifican la publicacion real antes de reportar exito.

    def _buscar_boton_responder(self, timeout: int = 12):
        """Devuelve el boton Responder del tweet (o un fallback).

        Selectores (con fallback): `[data-testid='reply']` (y variantes con
        `div[role='button']`), `button[...]`, aria-label "Responder"/"Reply"
        (incluida la coincidencia lateral '...eply'/'...espond' por si X cambia
        el prefijo del aria-label) y botones con ese texto visible.
        Devuelve None si no aparece un boton visible/habilitado en `timeout`s.
        """
        selectores = [
            "[data-testid='reply']",
            "div[role='button'][data-testid='reply']",
            "[data-testid='reply'] [role='button']",
            "button[data-testid='reply']",
        ]
        xpaths = [
            "//*[@data-testid='reply']",
            "//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'responder')]",
            "//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'reply')]",
            "//*[@role='button'][@aria-label='Reply' or @aria-label='Responder']",
            "//*[@role='button'][.//*[local-name()='path'] and "
            "(contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'eply') or "
            "contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ',"
            "'abcdefghijklmnopqrstuvwxyz'),'espond'))]",
            "//div[@role='button'][.//span[text()='Responder']]",
            "//div[@role='button'][.//span[text()='Reply']]",
        ]
        fin = time.time() + timeout
        while time.time() < fin:
            for sel in selectores:
                try:
                    for btn in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info(f"Boton Responder encontrado con selector: {sel}")
                            return btn
                except Exception:
                    continue
            for xp in xpaths:
                try:
                    for btn in self.driver.find_elements(By.XPATH, xp):
                        if btn.is_displayed() and btn.is_enabled():
                            logger.info(f"Boton Responder encontrado con XPath: {xp}")
                            return btn
                except Exception:
                    continue
            time.sleep(0.5)
        logger.warning(f"timeout esperando boton Responder ({timeout}s)")
        return None

    # Frases visibles de X cuando el tweet ancla NO acepta respuestas (el
    # boton Responder no esta disponible). Se comparan en minusculas y sin
    # acentos contra el texto visible de la pagina (ver
    # `_motivo_no_respondible`).
    _FRASES_RESPUESTAS_LIMITADAS = (
        "who can reply",
        "quien puede responder",
        "quienes pueden responder",
        "respuestas limitadas",
        "replies are limited",
        "only some people can reply",
        "solo algunas personas pueden responder",
    )

    def _motivo_no_respondible(self) -> str:
        """Motivo por el que el tweet ancla NO se puede responder ('' si si).

        Detecta, en el texto visible de la pagina (o su `page_source`):
        - respuestas limitadas ("Who can reply?", "Las respuestas estan
          limitadas", "Only some people can reply", ...);
        - tweet eliminado/no existe/cuenta suspendida
          (`_detectar_tweet_no_disponible`).

        Compara en minusculas y sin acentos. Nunca lanza.
        """
        texto = ""
        try:
            texto = self.driver.find_element(By.TAG_NAME, "body").text or ""
        except Exception:
            texto = ""
        if not texto:
            try:
                texto = self.driver.page_source or ""
            except Exception:
                texto = ""
        try:
            normalizado = unicodedata.normalize("NFKD", (texto or "").lower())
            normalizado = "".join(
                c for c in normalizado if not unicodedata.combining(c)
            )
        except Exception:
            normalizado = (texto or "").lower()
        for frase in self._FRASES_RESPUESTAS_LIMITADAS:
            if frase in normalizado:
                return "el tweet ancla tiene las respuestas limitadas"
        return self._detectar_tweet_no_disponible()

    def _esperar_article_tweet(self, timeout: int = 10):
        """Espera hasta `timeout`s a que exista `article[data-testid='tweet']`.

        La pagina del tweet ancla puede tardar con proxy lento; los timeouts
        transitorios del renderer se toleran. Devuelve el articulo o None si no
        aparecio en el plazo (el llamador sigue con el flujo normal, sin
        abortar). Nunca lanza.
        """
        fin = time.time() + max(0.2, float(timeout))
        while time.time() < fin:
            try:
                articulos = self.driver.find_elements(
                    By.CSS_SELECTOR, "article[data-testid='tweet']"
                )
                if articulos:
                    return articulos[0]
            except Exception:
                pass
            time.sleep(0.5)
        return None

    # Frases visibles de X cuando la pagina esta rota/limitada (no un simple
    # retraso de la SPA): permiten cortar rapido en vez de agotar el timeout
    # completo del compositor.
    _FRASES_ERROR_PAGINA = (
        "something went wrong",
        "algo salió mal",
        "algo salio mal",
        "rate limit",
        "try again",
    )

    # Errores de `_abrir_compositor` que NO deben seguir probando rutas: la
    # sesion esta caida (login). La pagina de error generica de X ("something
    # went wrong"/"algo salio mal") ya NO es fatal: es un fallo de RUTA, asi
    # que se hace un refresh corto + segundo intento y, si sigue, se prueba la
    # siguiente ruta con el presupuesto restante.
    _ERRORES_FATALES_COMPOSITOR = (
        "sesión de x expirada",
        "sesion de x expirada",
        "se pidió login",
        "se pidio login",
    )

    # Presupuesto TOTAL (segundos) para abrir el compositor. Los timeouts por
    # ruta (10+5 en /compose/post y 8+4 en cada alterna) pueden sumar mas que
    # esto en el peor caso; cuando se agota, las esperas de las rutas
    # siguientes se recortan para cortar hacia el error final (el motor
    # reintenta con otro navegador). Si una ruta cae en la PAGINA DE ERROR de X
    # ("something went wrong"), ya NO se aborta: se refresca la pestaña por
    # /home con `navegar_tolerante` (absorbe el interstitial) y se prueba la
    # SIGUIENTE ruta con este presupuesto (~35-40s; antes se quemaban ~44s de
    # proxy sin cambiar el resultado).
    _PRESUPUESTO_COMPOSITOR = 38.0

    def _hay_muro_login(self) -> bool:
        """True si X pidio login en la pagina actual (sesion caida).

        Detecta por URL (`login`, `/i/flow`, `account/access`) y por
        formularios VISIBLES de login (username, loginButton,
        ocfEnterTextTextInput, password). Nunca lanza: si el driver esta
        muerto devuelve False y las esperas del flujo deciden.
        """
        try:
            url = (self.driver.current_url or "").lower()
        except Exception:
            url = ""
        if any(frag in url for frag in ("login", "/i/flow", "account/access")):
            return True

        for sel in (
            "input[name='text'][autocomplete='username']",
            "[data-testid='loginButton']",
            "[data-testid='ocfEnterTextTextInput']",
            "input[name='password']",
        ):
            try:
                for elem in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if elem.is_displayed():
                        return True
            except Exception:
                continue
        return False

    def _frase_error_pagina(self) -> str:
        """Primera frase de error visible en la pagina ('' si no hay).

        Revisa "something went wrong"/"algo salió mal"/"rate limit"/"try
        again". Nunca lanza.
        """
        try:
            page_source = (self.driver.page_source or "").lower()
        except Exception:
            return ""
        for frase in self._FRASES_ERROR_PAGINA:
            if frase in page_source:
                return frase
        return ""

    def _diagnostico_pagina(self) -> str:
        """Diagnostico legible de la pagina actual para logs/errores.

        Formato: `url=<...> title=<...> login=<bool> error=<frase>`. Nunca
        lanza: si el driver esta muerto devuelve lo que pueda.
        """
        try:
            url = self.driver.current_url or ""
        except Exception:
            url = "<sin url>"
        try:
            title = self.driver.title or ""
        except Exception:
            title = "<sin title>"
        try:
            login = self._hay_muro_login()
        except Exception:
            login = False
        try:
            error = self._frase_error_pagina()
        except Exception:
            error = ""
        return f"url={url} title={title} login={bool(login)} error={error}"

    def _url_en_x(self) -> bool:
        """True si la ventana actual esta navegando en x.com/twitter.com.

        Un driver recien creado vive en `chrome://new-tab-page`/`about:blank`:
        mientras siga ahi NO tiene sentido esperar elementos de X. Nunca lanza.
        """
        try:
            actual = (self.driver.current_url or "").lower()
        except Exception:
            return False
        return ("x.com" in actual) or ("twitter.com" in actual)

    def _enfocar_ventana_x(self) -> bool:
        """Si hay varias ventanas, cambia a la primera que este en X.

        Devuelve True si la ventana activa quedo en X; False si ninguna lo
        esta (o no se pudo leer la lista). Nunca lanza.
        """
        try:
            handles = list(self.driver.window_handles or [])
        except Exception:
            return False
        if self._url_en_x():
            return True
        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                if self._url_en_x():
                    logger.info(f"x.com encontrado en otra ventana ({handle[:8]}...)")
                    return True
            except Exception:
                continue
        return False

    def _asegurar_pagina_tweet(self, url: str) -> None:
        """Garantiza que el driver este en la pagina del tweet o lanza RAPIDO.

        Bug real: con el driver en `chrome://new-tab-page`/`about:blank` (tab
        crashed o `get` que nunca navego) `responder_tweet` esperaba 12s+8s un
        boton que jamas iba a aparecer. Aqui:
        1. Si ya hay una ventana en X, se usa esa.
        2. Si no, se reintenta UNA navegacion a `url`.
        3. Si sigue sin estar en X, lanza "tab crashed/navegador sin navegar"
           (el motor lo trata como error reintentable) de inmediato.
        Nunca lanza otro tipo de error.
        """
        if self._url_en_x():
            return
        if self._enfocar_ventana_x():
            return
        try:
            self.driver.get(url)
        except TimeoutException:
            logger.warning(
                f"Carga lenta de {url} (reintento de navegacion); sigo con "
                f"esperas explicitas"
            )
        time.sleep(0.3)
        if not self._url_en_x():
            raise Exception(
                "tab crashed/navegador sin navegar: la ventana de X no cargo "
                f"({self._diagnostico_pagina()})"
            )

    def _recuperar_interstitial(self, url: str) -> str:
        """Clasifica la pagina objetivo tras el `get` y refresca si es interstitial.

        Se llama JUSTO despues del `driver.get(url)` de `solo_retwittear` y
        `responder_tweet`: un get atascado suele dejar la pagina de error de X
        ("something went wrong") o el challenge anti-bot de Cloudflare
        montados, y esperar 12s+8s el boton era tiro perdido (~20s por fallo).

        Para la PAGINA DE ERROR generica de X delega en `navegar_tolerante`,
        que hace hasta DOS refrescos acotados (P1: el interstitial generico a
        veces necesita un segundo) dentro de un presupuesto de ~15s; si
        persiste, el llamador falla rapido con el detalle correspondiente.
        Para el challenge anti-bot hace UN SOLO refresh corto (nunca el
        adicional): es lo que puede limpiar Cloudflare.

        Devuelve:
          - "ok":       pagina usable (no habia interstitial o el refresh la arreglo).
          - "error":    sigue la pagina de error de X.
          - "anti-bot": sigue el challenge anti-bot (Cloudflare).
          - "login":    X pide login (sesion caida).
          - "driver":   driver roto (el motor descarta la pestaña).

        Nunca lanza.
        """
        try:
            anti_bot = self.es_pagina_anti_bot()
            error = self._es_pagina_error_x()
        except Exception:
            return "ok"
        if not anti_bot and not error:
            return "ok"

        logger.warning(
            f"interstitial en {url} "
            f"({'anti-bot (1 refresh)' if anti_bot else 'pagina de error de X (hasta 2 refrescos)'}); "
            f"refresh tolerante antes de esperar el boton"
        )

        if anti_bot:
            # `navegar_tolerante` clasifica la pagina anti-bot como "ok"/"login"
            # y no refrescaria: el refresh directo es lo que puede limpiar el
            # challenge de Cloudflare.
            self._refresh_corto(8.0)
            try:
                time.sleep(0.5)
            except Exception:
                pass
            if self.es_pagina_anti_bot():
                return "anti-bot"
            return "error" if self._es_pagina_error_x() else "ok"

        # El "error" puede venir de un `chrome://`/`about:blank` donde el get
        # NUNCA navego (tab crashed): eso no es el interstitial de X. UN
        # reintento de get (como `_asegurar_pagina_tweet`) y, si sigue sin X,
        # "driver" para que el llamador reporte tab crashed y el motor
        # reintente con un navegador nuevo rapido (sin esperar 12s+8s).
        if not self._url_en_x():
            self._get_acotado(url)
            if self.es_pagina_anti_bot():
                return "anti-bot"
            if not self._url_en_x():
                self.ultimo_error = (
                    "tab crashed/navegador sin navegar: la ventana de X no cargo "
                    f"({self._diagnostico_pagina()})"
                )
                return "driver"
            if not self._es_pagina_error_x():
                return "ok"

        # P1: la pagina de error generica delega en `navegar_tolerante`, que ya
        # hace hasta DOS refrescos acotados (presupuesto ~15s). El anti-bot
        # retorno arriba con SU unico refresh corto: aqui nunca se le agrega
        # otro.
        resultado = self.navegar_tolerante(url, timeout_total=20.0)
        if self.es_pagina_anti_bot():
            return "anti-bot"
        return resultado

    @classmethod
    def _es_error_fatal_compositor(cls, error) -> bool:
        """True si el error indica sesion caida (no seguir probando rutas)."""
        try:
            texto = str(error or "").lower()
        except Exception:
            return False
        return any(senal in texto for senal in cls._ERRORES_FATALES_COMPOSITOR)

    @staticmethod
    def _es_fallo_pagina_error(error) -> bool:
        """True si el error es la pagina de error generica de X.

        A diferencia de la sesion caida, la pagina de error es un fallo de
        RUTA: `_abrir_compositor` hace un refresh corto y un segundo intento
        de la misma ruta antes de pasar a la siguiente.
        """
        try:
            texto = str(error or "").lower()
        except Exception:
            return False
        return "página de error" in texto or "pagina de error" in texto

    # Fallos TRANSITORIOS y SEGUROS del compositor/escritura (nada se publico
    # todavia) que merecen UN ciclo limpio en `publicar_tweet` (refresh corto +
    # recomponer + reescribir). Los rechazos de X y los problemas de
    # sesion/anti-bot/driver NO estan aqui: esos se reportan tal cual.
    _FALLOS_TRANSITORIOS_COMPOSITOR = (
        "no se pudo escribir el texto en el editor de x",
        "no se pudo limpiar",
    )

    @classmethod
    def _es_fallo_transitorio_compositor(cls, error) -> bool:
        """True si el fallo de compositor/escritura merece UN ciclo limpio.

        Cubre SOLO fallos transitorios y seguros (nada se envio a X): la
        pagina de error/interstitial generica de X ("compositor no disponible
        (pagina de error de X)", "compositor de X no cargo") y no haber podido
        escribir el texto por el bloqueo de la mascara del modal ("no se pudo
        escribir el texto en el editor de X", "editor de X con borrador que no
        se pudo limpiar"). NO cubre sesion caida, anti-bot ni driver roto:
        `publicar_tweet` los propaga (el motor los reconoce) porque un refresh
        no los arregla.
        """
        try:
            texto = str(error or "").lower()
        except Exception:
            return False
        if any(senal in texto for senal in cls._ERRORES_FATALES_COMPOSITOR):
            return False
        if "anti-bot" in texto or "cloudflare" in texto:
            return False
        if cls._es_fallo_pagina_error(error):
            return True
        if "compositor de x no cargo" in texto:
            return True
        return any(senal in texto for senal in cls._FALLOS_TRANSITORIOS_COMPOSITOR)

    # Selectores del compositor de X. El `data-testid` es el principal; los
    # contenteditables quedan como respaldo por si X cambia el testid.
    _EDITOR_SELECTORES = (
        "[data-testid='tweetTextarea_0']",
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
    )

    # P0-A/P0-B: JS que resuelve el editable REAL (el elemento o un descendiente
    # contenteditable/role=textbox) y lo enfoca. Con `arguments[1]` truthy
    # ADEMAS selecciona todo su contenido (Range) para que la insercion
    # posterior (CDP `Input.insertText` o Ctrl+V) REEMPLACE lo que hubiera
    # (misma semantica que Ctrl+A + pegar). Devuelve True/False.
    _JS_ENFOCAR_EDITABLE = """
const raiz = arguments[0];
if (!raiz) { return false; }
const esEditable = (n) => {
    if (!n || n.nodeType !== 1) { return false; }
    try { if (n.isContentEditable) { return true; } } catch (e) {}
    const tag = (n.tagName || '').toLowerCase();
    return tag === 'textarea' || tag === 'input';
};
let objetivo = esEditable(raiz) ? raiz : null;
if (!objetivo) {
    const cands = raiz.querySelectorAll("[contenteditable='true'], [role='textbox']");
    for (const c of cands) {
        if (esEditable(c)) { objetivo = c; break; }
    }
}
if (!objetivo) {
    const cands = raiz.querySelectorAll("[role='textbox']");
    for (const c of cands) {
        const internos = c.querySelectorAll("[contenteditable='true'], textarea, input");
        for (const i of internos) {
            if (esEditable(i)) { objetivo = i; break; }
        }
        if (objetivo) { break; }
    }
}
if (!objetivo) { return false; }
try { objetivo.focus(); } catch (e) { return false; }
if (arguments.length > 1 && arguments[1]) {
    try {
        const tag = (objetivo.tagName || '').toLowerCase();
        if (tag === 'textarea' || tag === 'input') {
            objetivo.select();
        } else {
            const rango = document.createRange();
            rango.selectNodeContents(objetivo);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(rango);
        }
    } catch (e) {}
}
return true;
"""

    # P0-C: JS que comprueba que el punto CENTRAL del editor no este tapado por
    # un overlay (`data-testid='mask'` del modal, etc.): `elementFromPoint`
    # devuelve el nodo topmost en ese punto y debe ser el editor o un
    # descendiente suyo. Devuelve true/false; null si el chequeo no se pudo
    # hacer (el llamador cae al comportamiento anterior).
    _JS_EDITOR_NO_OCLUIDO = """
const el = arguments[0];
if (!el) { return false; }
try {
    const r = el.getBoundingClientRect();
    if (!r || r.width <= 0 || r.height <= 0) { return false; }
    const x = r.left + (r.width / 2);
    const y = r.top + (r.height / 2);
    const top = document.elementFromPoint(x, y);
    if (!top) { return false; }
    return top === el || el.contains(top);
} catch (e) { return null; }
"""

    def _editor_no_ocluido(self, editor) -> Optional[bool]:
        """True/False si el centro del editor NO esta tapado por un overlay.

        `None` si el chequeo JS no se pudo hacer (driver raro/Fake): el
        llamador debe caer al comportamiento anterior. Nunca lanza.
        """
        try:
            resultado = self.driver.execute_script(self._JS_EDITOR_NO_OCLUIDO, editor)
        except Exception:
            return None
        if resultado is None:
            return None
        return bool(resultado)

    def _enfocar_editable(self, elemento, seleccionar: bool = True) -> bool:
        """Enfoca el editable REAL dentro de `elemento` (True si lo logro).

        En algunas variantes de X `tweetTextarea_0` es un WRAPPER sin
        `contenteditable` y el editor real es un descendiente: el CDP
        `Input.insertText` escribe en el elemento ENFOCADO, asi que aqui se
        resuelve y enfoca con JS (sin clic de Selenium: el `mask` del modal
        intercepta el clic). Con `seleccionar=True` selecciona todo el
        contenido con un Range para que la insercion lo REEMPLACE.

        Devuelve False si no hay editable enfocable. Nunca lanza por si mismo:
        las excepciones del driver se propagan al llamador (que las maneja).
        """
        try:
            ok = self.driver.execute_script(
                self._JS_ENFOCAR_EDITABLE, elemento, bool(seleccionar)
            )
        except Exception:
            return False
        return bool(ok)

    def _primer_editor_visible(self, selectores: Optional[list] = None, preferir_dialogo: bool = False):
        """Devuelve el PRIMER editor VISIBLE (y NO ocluido) de la pagina.

        X puede tener varios `[data-testid='tweetTextarea_0']` montados en el
        DOM (composers viejos/ocultos): buscar con `presence` agarra el primero
        aunque no se vea, el texto se escribe en el editor equivocado y el
        composer visible queda vacio (boton Post deshabilitado). Por eso aqui
        se exige `is_displayed()`. Con `preferir_dialogo=True` (modal de
        respuesta/cita) se busca primero dentro de un `div[role='dialog']`
        visible.

        P0-C: el primer editor VISIBLE tambien puede estar TAPADO por el
        `data-testid='mask'` del modal (bug real: `send_keys: mask sigue
        interceptando el clic`). Si hay VARIOS candidatos, se comprueba con
        `_editor_no_ocluido` (JS `elementFromPoint`) y se devuelve el primero
        cuyo centro no este tapado; si el chequeo JS falla o ninguno pasa, se
        devuelve el primer visible (comportamiento anterior). Sigue devolviendo
        None si no hay ningun editor visible.
        """
        selectores = list(selectores or self._EDITOR_SELECTORES)
        candidatos = []
        if preferir_dialogo:
            try:
                for dlg in self.driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']"):
                    try:
                        if not dlg.is_displayed():
                            continue
                    except Exception:
                        continue
                    for sel in selectores:
                        try:
                            for editor in dlg.find_elements(By.CSS_SELECTOR, sel):
                                if editor.is_displayed():
                                    candidatos.append(editor)
                        except Exception:
                            continue
            except Exception:
                pass
        for sel in selectores:
            try:
                for editor in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if editor.is_displayed():
                        candidatos.append(editor)
            except Exception:
                continue
        if not candidatos:
            return None
        if len(candidatos) == 1:
            # Sin alternativa: el comportamiento de siempre (aunque el mask lo
            # tape, no hay otro editor que elegir).
            return candidatos[0]
        for editor in candidatos:
            estado = self._editor_no_ocluido(editor)
            if estado is None:
                # Chequeo JS no disponible: comportamiento anterior.
                return candidatos[0]
            if estado:
                return editor
        # Todos visibles pero ocluidos: no hay alternativa mejor.
        return candidatos[0]

    def _buscar_editor_visible_actual(self, timeout: float = 5.0):
        """Devuelve el PRIMER editor VISIBLE actual (None si no aparece).

        X re-renderiza el DOM (React) y un `WebElement` guardado puede quedar
        viejo (`StaleElementReferenceException`): re-localizar es barato y
        evita perder la accion cuando el editor cambio de nodo. Sondea con
        `_primer_editor_visible` (sin refresh ni esperas de flujo) hasta
        `timeout` segundos. Nunca lanza: devuelve None si no hay editor.
        """
        fin = time.time() + max(0.0, float(timeout))
        while True:
            try:
                editor = self._primer_editor_visible()
                if editor is not None:
                    return editor
            except Exception as e:
                logger.debug(f"Re-localizando editor: {type(e).__name__}: {e}")
            if time.time() >= fin:
                return None
            time.sleep(0.2)

    def _esperar_editor_visible(
        self,
        timeout: int = 18,
        preferir_dialogo: bool = False,
        reintento_timeout: int = 10,
        retornar_none_en_fallo: bool = False,
    ):
        """Espera a que monte un editor VISIBLE y lo devuelve.

        Se usa en TODOS los flujos de escritura (tweet, hilo, quote y
        respuesta). Tolera los `WebDriverException` transitorios de la SPA
        saturada, pero re-lanza de inmediato:
        - los fallos duros de driver (`InvalidSessionIdException`,
          `NoSuchDriverException`, `MaxRetryError`, "connection refused"):
          el motor de activaciones los reconoce como transitorios y reintenta
          con un navegador nuevo;
        - la sesion caida (`_hay_muro_login`): no tiene sentido esperar mas,
          hay que renovar cookies.

        Con `retornar_none_en_fallo=True` (lo usa `_abrir_compositor`) una
        pagina de error generica de X ("something went wrong"/"algo salio
        mal") o el agotamiento del timeout devuelven None como fallo
        CONTROLADO, para que el compositor pruebe la siguiente ruta sin
        abortar. En los flujos de cita/respuesta (default) el contrato no
        cambia: pagina de error o timeout lanzan excepcion.

        Si en `timeout` segundos no aparece ningun editor visible, hace UN
        `driver.refresh()` (tolerando el TimeoutException de carga lenta del
        proxy) y espera `reintento_timeout` segundos mas. Si sigue sin
        aparecer lanza `Exception("compositor de X no cargo: el editor visible
        no aparecio (<diagnostico>)")` (mensaje que el motor reconoce) o
        devuelve None si `retornar_none_en_fallo=True`.

        Defaults agresivos (18/10) para que un fallo de compositor no bloquee
        la campana; el llamador puede pasar timeouts explicitos si necesita
        tolerar una carga mas lenta.
        """

        def buscar_editor():
            try:
                return self._primer_editor_visible(preferir_dialogo=preferir_dialogo)
            except (InvalidSessionIdException, NoSuchDriverException, MaxRetryError):
                raise
            except NoSuchWindowException as e:
                # Ventana cerrada o driver rotando: seguir esperando; si el
                # navegador murio de verdad, el timeout lanza "compositor de X
                # no cargo" (reintentable por el motor).
                logger.debug(
                    f"Editor visible: NoSuchWindowException transitoria "
                    f"({type(e).__name__}: {e})"
                )
                return None
            except WebDriverException as e:
                mensaje = str(e).lower()
                if (
                    "connection refused" in mensaje
                    or "err_connection_refused" in mensaje
                    or "max retries" in mensaje
                    or "maxretry" in mensaje
                    or "newconnectionerror" in mensaje
                ):
                    raise
                # Timeout/ventana cerrada transitoria de la SPA: seguir esperando.
                logger.debug(
                    f"Editor visible: WebDriverException transitorio ignorado "
                    f"({type(e).__name__}: {e})"
                )
                return None

        def verificar_pagina():
            """Devuelve "error" si X sirvio su pagina de error (fallo controlado).

            El muro de login SIEMPRE se propaga (sesion caida): no tiene
            sentido probar otra ruta con las mismas cookies.
            """
            if self._hay_muro_login():
                raise Exception(
                    "sesión de X expirada o inválida: se pidió login al abrir "
                    f"el compositor ({self._diagnostico_pagina()})"
                )
            error = self._frase_error_pagina()
            if error:
                if retornar_none_en_fallo:
                    return "error"
                raise Exception(
                    "X mostró una página de error al abrir el compositor "
                    f"({self._diagnostico_pagina()})"
                )
            return ""

        fin = time.time() + max(0.2, float(timeout))
        while time.time() < fin:
            editor = buscar_editor()
            if editor is not None:
                return editor
            if verificar_pagina() == "error":
                logger.warning(
                    "X mostró una página de error; fallo controlado de la ruta "
                    f"({self._diagnostico_pagina()})"
                )
                return None
            time.sleep(0.5)

        logger.warning(
            f"Editor visible de X no aparecio en {timeout}s; refrescando la pagina"
        )
        try:
            self.driver.refresh()
        except TimeoutException:
            logger.warning("Refresh lento del compositor; sigo con esperas explicitas")
        except Exception as e:
            logger.warning(f"Refresh del compositor fallo ({type(e).__name__}: {e})")

        fin = time.time() + max(0.2, float(reintento_timeout))
        while time.time() < fin:
            editor = buscar_editor()
            if editor is not None:
                return editor
            if verificar_pagina() == "error":
                logger.warning(
                    "X mostró una página de error; fallo controlado de la ruta "
                    f"({self._diagnostico_pagina()})"
                )
                return None
            time.sleep(0.5)

        logger.warning(
            f"timeout esperando editor visible "
            f"(timeout={timeout}s, reintento={reintento_timeout}s)"
        )
        if retornar_none_en_fallo:
            return None
        raise Exception(
            "compositor de X no cargo: el editor visible no aparecio "
            f"({self._diagnostico_pagina()})"
        )

    def _clic_nuevo_post(self) -> bool:
        """Clica el boton "Nuevo post"/New post de /home (True si lo logro).

        Selectores en orden: `SideNav_NewTweet_Button`, enlaces a
        `/compose/post` y `/compose/tweet`, y aria-label "Post"/"Publicar".
        El clic se hace por JS para que el sidebar no intercepte el evento.
        Nunca lanza.
        """
        selectores = (
            "[data-testid='SideNav_NewTweet_Button']",
            "a[href='/compose/post']",
            "a[href='/compose/tweet']",
            "[aria-label*='Post']",
            "[aria-label*='Publicar']",
        )
        for sel in selectores:
            try:
                for btn in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if not btn.is_displayed():
                        continue
                    try:
                        self.driver.execute_script("arguments[0].click();", btn)
                    except Exception:
                        btn.click()
                    return True
            except Exception:
                continue
        return False

    def _refresh_corto(self, timeout: float = 5.0) -> None:
        """Hace UN refresh tolerante con `page_load_timeout` corto (nunca lanza).

        X a veces sirve su pagina de error generica; refrescar suele montar la
        SPA. `driver.refresh()` puede bloquear hasta el page_load_timeout del
        bot, asi que se baja a `timeout` y se RESTAURA el valor previo con el
        context manager (antes restauraba 60 fijo).
        """
        with _page_load_timeout_acotado(self.driver, max(0.5, float(timeout))):
            try:
                self.driver.refresh()
            except Exception as e:
                logger.debug(
                    f"Refresh corto del compositor fallo ({type(e).__name__}: {e})"
                )

    def _get_acotado(self, url: str, timeout_max: float = 20.0) -> None:
        """`driver.get(url)` con `page_load_timeout` temporal (nunca lanza).

        Un get atascado con la SPA de X quemaba el timeout completo del bot
        (~60s): aqui se acota a `min(previo, timeout_max)` y se restaura
        SIEMPRE (context manager con finally). El `TimeoutException` de carga
        lenta se tolera: la pagina suele seguir cargando y las esperas
        explicitas de elementos deciden.
        """
        with _page_load_timeout_acotado(self.driver, timeout_max):
            try:
                self.driver.get(url)
            except TimeoutException:
                logger.warning(f"Carga lenta de {url}; sigo con esperas explicitas")

    def _verificar_anti_bot_compositor(self) -> None:
        """Lanza el fallo claro si la pagina actual es un challenge anti-bot.

        Un challenge de Cloudflare en el compositor NO es una cuenta
        suspendida: el motor lo trata como sesion caida de la campana
        ("renueva cookies/login") y NO debe marcar suspendida. Se comprueba en
        cada ruta ANTES del muro de login porque `/account/access` figura en
        ambas heuristicas y el anti-bot manda (falso positivo real arreglado).
        """
        if self.es_pagina_anti_bot():
            raise Exception(
                "X pidió verificación anti-bot (Cloudflare); no se pudo abrir "
                f"el compositor ({self._diagnostico_pagina()})"
            )

    def _navegar_home_tolerante(self) -> None:
        """Refresca la pestaña por `/home` tolerando el interstitial de X.

        Se usa cuando una ruta del compositor cae en la pagina de error de X:
        `navegar_tolerante` absorbe el interstitial con UN refresh y deja la
        pestaña lista para probar la SIGUIENTE ruta (antes se abortaba tras el
        refresh de la misma ruta). Si el anti-bot persiste lanza el fallo
        anti-bot; si X pide login, el fallo de sesion; si el driver esta roto,
        un `WebDriverException` (el motor reintenta con navegador nuevo).
        """
        resultado = self.navegar_tolerante(f"{self.base_url}/home")
        self._verificar_anti_bot_compositor()
        if resultado == "login":
            raise Exception(
                "sesión de X expirada o inválida: se pidió login al abrir "
                f"el compositor ({self._diagnostico_pagina()})"
            )
        if resultado == "driver":
            raise WebDriverException(
                self.ultimo_error or "driver roto al recuperar /home del compositor"
            )

    def _abrir_compositor(self):
        """Abre el compositor de un POST NUEVO y devuelve el editor visible.

        Prueba en orden, con UN refresh corto + UN segundo intento de la MISMA
        ruta cuando X sirve su pagina de error o el editor no aparece:
        1. `/compose/post` (ruta clasica, timeout 10/5).
        2. `/compose/tweet` (8/4).
        3. `/home` + boton "Nuevo post" (8/4).

        Si una ruta cae en la pagina de error de X y el refresh de la MISMA
        ruta no la arregla, NO se aborta: se refresca la pestaña por `/home`
        con `navegar_tolerante` (absorbe el interstitial con UN refresh) y se
        prueba la SIGUIENTE ruta con el presupuesto restante. El presupuesto
        total (`_PRESUPUESTO_COMPOSITOR` ~38s) recorta las esperas para no
        eternizarse y los gets van acotados con `_get_acotado` (~20s).

        Si las 3 rutas fallaron con la pagina de error/interstitial de X y aun
        queda presupuesto, hay una ULTIMA fase acotada: `_refresh_corto(3)` en
        la pestaña + UN reintento directo de `/compose/post` con esperas cortas
        (<=8s/4s). Es SOLO para ese fallo transitorio: login, sesion caida,
        anti-bot y driver roto se propagan antes (nunca se enmascaran).

        Un challenge anti-bot (Cloudflare en `/account/access`, "Just a
        moment...") lanza "X pidió verificación anti-bot (Cloudflare); no se
        pudo abrir el compositor": el motor lo trata como sesion caida de la
        campaña y NO marca suspendida (falso positivo real arreglado). La
        sesion caida ("sesión de X expirada o inválida") y los errores duros
        de driver (InvalidSessionId/NoSuchDriver/MaxRetry/connection refused)
        SI se propagan tal cual (el motor los reconoce). Si ninguna ruta da
        editor, lanza Exception con la frase "compositor de X no cargo" (el
        motor la usa) + el diagnostico de la pagina. NO se usa para
        citas/respuestas: esas abren un modal y llaman directamente a
        `_esperar_editor_visible`.
        """
        inicio = time.time()
        # True si alguna ruta fallo por la pagina de error/interstitial de X:
        # habilita la ULTIMA fase de recuperacion (refresh corto + reintento
        # directo de /compose/post) antes del fallo controlado.
        fallo_pagina_error = False

        def restante() -> float:
            """Segundos que quedan del presupuesto total (nunca negativo)."""
            return max(0.0, self._PRESUPUESTO_COMPOSITOR - (time.time() - inicio))

        def esperar_compositor(espera, reintento):
            """Llama `_esperar_editor_visible` en modo fallo controlado.

            Tolera parches/mocks con la firma vieja (sin
            `retornar_none_en_fallo`): en ese caso el fallo llega como
            excepcion y `abrir_con_espera` la trata igual (refresh + segundo
            intento). Nunca lanza por la sesion caida ni por driver muerto.
            """
            try:
                return self._esperar_editor_visible(
                    timeout=max(0.2, espera),
                    reintento_timeout=max(0.2, reintento),
                    retornar_none_en_fallo=True,
                )
            except TypeError as e:
                if "retornar_none_en_fallo" not in str(e):
                    raise
                return self._esperar_editor_visible(
                    timeout=max(0.2, espera),
                    reintento_timeout=max(0.2, reintento),
                )

        def abrir_con_espera(ruta, espera, reintento):
            """Abre `ruta`; devuelve el editor visible o lanza fallo controlado.

            Si la ruta cae en la pagina de error de X, hace UN `driver.refresh()`
            corto y UN segundo intento de la MISMA ruta; si la pagina de error
            PERSISTE, lanza "compositor no disponible (pagina de error de X)"
            y `_abrir_compositor` refresca por /home y prueba la SIGUIENTE ruta
            con el presupuesto acotado (antes se abortaba). Un challenge
            anti-bot se corta con su propio mensaje antes que el muro de login.
            """
            self._get_acotado(f"{self.base_url}{ruta}")
            # Sin sleep fijo: basta con que el documento este interactivo y,
            # sobre todo, con la espera por elemento VISIBLE (`_esperar_editor_visible`).
            self._esperar_documento_listo(timeout=min(3, max(0.2, restante())))

            # Anti-bot ANTES del muro de login: `/account/access` (Cloudflare)
            # figura en ambos chequeos y una verificacion anti-bot NO es sesion
            # caida ni suspension.
            self._verificar_anti_bot_compositor()

            # Si X ya redirigio al login no hay SPA que esperar: cortar ya.
            if self._hay_muro_login():
                raise Exception(
                    "sesión de X expirada o inválida: se pidió login al abrir "
                    f"el compositor ({self._diagnostico_pagina()})"
                )

            for intento in (1, 2):
                if intento == 2:
                    logger.warning(
                        f"ruta {ruta} con pagina de error; refresh corto y "
                        f"segundo intento"
                    )
                    self._refresh_corto()
                    # El refresh pudo caer en un challenge anti-bot.
                    self._verificar_anti_bot_compositor()
                # Deteccion temprana de la pagina de error de X: no tiene
                # sentido agotar el timeout del editor si X ya sirvio
                # "something went wrong" y no hay ningun editor visible.
                if (
                    self._frase_error_pagina()
                    and self._primer_editor_visible() is None
                ):
                    if intento == 1 and restante() > 1.0:
                        continue
                    raise Exception(
                        "compositor no disponible (pagina de error de X) "
                        f"({self._diagnostico_pagina()})"
                    )
                espera_efectiva = min(float(espera), restante())
                reintento_efectivo = min(
                    float(reintento), max(0.0, restante() - espera_efectiva)
                )
                if espera_efectiva < float(espera) or reintento_efectivo < float(reintento):
                    logger.warning(
                        f"timeout esperando editor visible en {ruta}: presupuesto "
                        f"ajustado a {espera_efectiva:.0f}s + {reintento_efectivo:.0f}s"
                    )
                try:
                    editor = esperar_compositor(espera_efectiva, reintento_efectivo)
                except (WebDriverException, MaxRetryError):
                    # Driver muerto (o reconexion rechazada): que el motor
                    # reintente con un navegador nuevo.
                    raise
                except Exception as e:
                    if self._es_error_fatal_compositor(e):
                        # Sesion caida: probar otra ruta no ayuda.
                        raise
                    logger.warning(f"Compositor no disponible en {ruta}: {e}")
                    if self._es_fallo_pagina_error(e):
                        if intento == 1 and restante() > 1.0:
                            continue
                        # El segundo intento (refresh) TAMPOCO arreglo la
                        # pagina de error: fallo rapido y reintentable.
                        raise
                    raise
                if editor is not None:
                    return editor
                # None = fallo controlado: pagina de error (reintentar la
                # misma ruta) o timeout sin editor (pasar a la siguiente).
                if (
                    intento == 1
                    and self._frase_error_pagina()
                    and restante() > 1.0
                ):
                    continue
                break

            if self._frase_error_pagina() and self._primer_editor_visible() is None:
                raise Exception(
                    "compositor no disponible (pagina de error de X) "
                    f"({self._diagnostico_pagina()})"
                )
            raise Exception(
                f"editor visible de X no apareció en {ruta} "
                f"({self._diagnostico_pagina()})"
            )

        for ruta, espera, reintento in (
            ("/compose/post", 10, 5),
            ("/compose/tweet", 8, 4),
        ):
            if restante() <= 0.5:
                # Presupuesto agotado: seguir con la siguiente ruta solo
                # alargaria el fallo. El motor reintenta con otro navegador.
                logger.warning(
                    f"timeout esperando editor visible: presupuesto de "
                    f"{self._PRESUPUESTO_COMPOSITOR:.0f}s agotado antes de {ruta}"
                )
                break
            try:
                editor = abrir_con_espera(ruta, espera, reintento)
            except (WebDriverException, MaxRetryError):
                # Driver muerto (o reconexion rechazada): que el motor
                # reintente con un navegador nuevo.
                raise
            except Exception as e:
                if self._es_error_fatal_compositor(e):
                    # Sesion caida: probar otra ruta no ayuda.
                    raise
                # Si la pagina quedo en anti-bot, ese es el fallo real (no es
                # sesion confirmada ni suspension).
                self._verificar_anti_bot_compositor()
                if self._es_fallo_pagina_error(e):
                    # NUEVO (antes se abortaba): la pagina de error de X se
                    # reintento UNA vez en la MISMA ruta sin exito. Se refresca
                    # la pestaña por /home con `navegar_tolerante` (absorbe el
                    # interstitial de X) y se prueba la SIGUIENTE ruta con el
                    # presupuesto restante en vez de fallar de inmediato.
                    fallo_pagina_error = True
                    logger.warning(
                        f"pagina de error de X persistente en {ruta}; "
                        "refresh tolerante por /home y siguiente ruta"
                    )
                    self._navegar_home_tolerante()
                    continue
                logger.warning(f"Compositor no disponible en {ruta}: {e}")
                continue
            logger.info(f"compositor abierto via {ruta}")
            return editor

        # 3) /home + boton "Nuevo post" (ultima ruta de la SPA)
        self._get_acotado(f"{self.base_url}/home")
        self._esperar_documento_listo(timeout=min(3, max(0.2, restante())))
        self._verificar_anti_bot_compositor()
        if self._hay_muro_login():
            raise Exception(
                "sesión de X expirada o inválida: se pidió login al abrir "
                f"el compositor ({self._diagnostico_pagina()})"
            )
        if self._clic_nuevo_post():
            logger.info("Boton 'Nuevo post' cliqueado en /home")
        else:
            logger.warning("No se encontro el boton 'Nuevo post' en /home")

        for intento in (1, 2):
            if intento == 2:
                logger.warning(
                    "timer de /home con pagina de error; refresh corto y "
                    "segundo intento"
                )
                self._refresh_corto()
            espera_efectiva = min(8.0, restante())
            reintento_efectivo = min(4.0, max(0.0, restante() - espera_efectiva))
            if espera_efectiva < 8.0 or reintento_efectivo < 4.0:
                logger.warning(
                    "timeout esperando editor visible en /home: presupuesto "
                    f"ajustado a {espera_efectiva:.0f}s + {reintento_efectivo:.0f}s"
                )
            try:
                editor = esperar_compositor(espera_efectiva, reintento_efectivo)
            except (WebDriverException, MaxRetryError):
                raise
            except Exception as e:
                if self._es_error_fatal_compositor(e):
                    raise
                if self._es_fallo_pagina_error(e):
                    fallo_pagina_error = True
                logger.warning(f"Compositor no disponible en /home: {e}")
                if (
                    intento == 1
                    and self._es_fallo_pagina_error(e)
                    and restante() > 1.0
                ):
                    continue
                break
            if editor is not None:
                logger.info("compositor abierto via /home + boton Nuevo post")
                return editor
            if intento == 1 and self._frase_error_pagina() and restante() > 1.0:
                continue
            break

        # 4) ULTIMA fase de recuperacion (fallos TRANSITORIOS): las 3 rutas
        # cayeron en la pagina de error/interstitial de X y aun queda
        # presupuesto. UN `_refresh_corto(3)` en la pestaña + UN reintento
        # directo de /compose/post con esperas cortas (<=8s/4s). Login, sesion
        # caida, anti-bot y driver roto NO llegan hasta aqui (se propagan
        # antes), asi que este reintento jamas los enmascara y el exito de las
        # rutas normales no cambia.
        if (fallo_pagina_error or self._frase_error_pagina()) and restante() > 2.0:
            logger.info(
                "compositor: pagina de error/interstitial de X persistente; "
                "refresh corto + reintento final de /compose/post"
            )
            self._refresh_corto(3)
            try:
                self._get_acotado(f"{self.base_url}/compose/post")
                self._esperar_documento_listo(timeout=min(3, max(0.2, restante())))
                self._verificar_anti_bot_compositor()
                if self._hay_muro_login():
                    raise Exception(
                        "sesión de X expirada o inválida: se pidió login al "
                        f"abrir el compositor ({self._diagnostico_pagina()})"
                    )
                if (
                    self._frase_error_pagina()
                    and self._primer_editor_visible() is None
                ):
                    logger.info(
                        "compositor: la pagina de error de X sigue tras el "
                        "reintento final"
                    )
                else:
                    editor = esperar_compositor(
                        min(8.0, restante()),
                        min(4.0, max(0.0, restante() - min(8.0, restante()))),
                    )
                    if editor is not None:
                        logger.info(
                            "compositor abierto via /compose/post "
                            "(reintento final)"
                        )
                        return editor
            except (WebDriverException, MaxRetryError):
                # Driver muerto: que el motor descarte la pestaña.
                raise
            except Exception as e:
                if self._es_error_fatal_compositor(e):
                    # Sesion caida: no hay nada mas que hacer.
                    raise
                # El anti-bot manda: no es sesion confirmada ni suspension.
                self._verificar_anti_bot_compositor()
                logger.info(
                    "compositor: reintento final sin editor "
                    f"({type(e).__name__}: {e})"
                )

        if self._frase_error_pagina() and self._primer_editor_visible() is None:
            raise Exception(
                "compositor no disponible (pagina de error de X) "
                f"({self._diagnostico_pagina()})"
            )
        raise Exception(
            "compositor de X no cargo: el editor visible no aparecio "
            f"({self._diagnostico_pagina()})"
        )

    @staticmethod
    def _status_id(url: str) -> str:
        """Extrae el id numerico de un enlace `/status/<id>` ('' si no hay)."""
        m = re.search(r"/status/(\d+)", url or "")
        return m.group(1) if m else ""

    def _obtener_url_respuesta(self, url_original: str = "") -> Optional[str]:
        """Mejor esfuerzo para obtener la URL de la respuesta recien publicada.

        La pestana principal del perfil NO muestra las respuestas, por eso se
        consulta `/<usuario>/with_replies` y se salta el tweet fijado y la URL
        original respondida (comparando por `status id`, no por dominio, para
        que twitter.com/x.com no confundan). Devuelve None si no se pudo.
        """
        original = (url_original or "").split("?")[0].rstrip("/")
        original_id = self._status_id(original)
        try:
            actual = (self.driver.current_url or "").split("?")[0].rstrip("/")
            if (
                "/status/" in actual
                and "/compose" not in actual
                and self._status_id(actual) != original_id
            ):
                return actual
        except Exception:
            pass

        try:
            self.driver.get(f"{self.base_url}/{self.usuario}/with_replies")
            self._esperar_article_tweet(timeout=2)
            time.sleep(0.5)
            tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            for tweet in tweets[:5]:
                try:
                    if self._es_tweet_fijado(tweet):
                        continue
                    enlace = tweet.find_element(By.CSS_SELECTOR, "a[href*='/status/']")
                    href = (enlace.get_attribute("href") or "").split("?")[0].rstrip("/")
                    if href and self._status_id(href) != original_id:
                        return href
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"No se pudo obtener la URL de la respuesta: {e}")
        return None

    def responder_tweet(
        self, url: str, texto: str, imagen_path: Optional[str] = None
    ) -> Optional[str]:
        """Responde (comentario) a un tweet existente y devuelve la URL.

        Asume que el llamador ya inicio sesion; si no hay driver, intenta
        `login_con_cookies()`. Navega a `url`, clica Responder
        (`_buscar_boton_responder`), escribe `texto` (`_pegar_texto`) y publica
        con `_buscar_boton_post()`. `imagen_path` es opcional y tolerante a
        fallos (`_subir_imagen` solo loguea si falla).

        Verifica la publicacion real con `_verificar_publicacion()` (toast
        "Your reply was sent" / cierre del compositor): NO devuelve exito solo
        por hacer clic. Si se publico, intenta obtener la URL de la respuesta
        (`_obtener_url_respuesta`).

        Devuelve la URL de la respuesta (str) si se obtuvo, `True` como fallback
        truthy si se publico pero no hubo URL, o `None` si fallo (el motivo
        queda en `self.ultimo_error`).
        """
        self.ultimo_error = ""
        self.ultima_url_publicada = ""

        if not (url or "").strip():
            self.ultimo_error = "falta la URL del tweet a responder"
            return None
        if not (texto or "").strip():
            self.ultimo_error = "falta el texto de la respuesta"
            return None

        if not self.driver:
            if not self.login_con_cookies():
                self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                return None

        t_inicio = self._ahora()
        try:
            try:
                self.driver.get(url)
            except TimeoutException:
                # Carga lenta (proxy intermitente): la pagina suele seguir
                # cargando; las esperas explicitas de elementos deciden.
                logger.warning(f"Carga lenta de {url}; sigo con esperas explicitas")
            time.sleep(0.3)

            # Interstitial/anti-bot tras el get: UN refresh tolerante y fallo
            # RAPIDO con detalle si persiste (evita quemar 12s+8s buscando el
            # boton Responder en una pagina que no es el tweet).
            estado_interstitial = self._recuperar_interstitial(url)
            if estado_interstitial == "anti-bot":
                self.ultimo_error = (
                    "X pidió verificación anti-bot (Cloudflare) en el tweet "
                    f"ancla (interstitial) ({self._diagnostico_pagina()})"
                )
                logger.error(self.ultimo_error)
                return None
            if estado_interstitial == "error":
                self.ultimo_error = (
                    "pagina de error de X (interstitial) en el tweet ancla "
                    f"({self._diagnostico_pagina()})"
                )
                logger.error(self.ultimo_error)
                return None
            if estado_interstitial == "driver":
                self.ultimo_error = (
                    self.ultimo_error
                    or "tab crashed/navegador sin navegar al abrir el tweet"
                )
                logger.error(self.ultimo_error)
                return None

            if self._hay_muro_login():
                # Sesion CDP invalida: UN fallback a login lento y UN reintento
                # de la navegacion (misma semantica que el resto de flujos).
                if self._sesion_cdp and self._revivir_sesion_cdp():
                    try:
                        self.driver.get(url)
                    except TimeoutException:
                        logger.warning(
                            f"Carga lenta de {url} tras login; sigo con esperas explicitas"
                        )
                    time.sleep(0.3)
                if self._hay_muro_login():
                    self.ultimo_error = (
                        "sesión de X expirada o inválida: se pidió login al abrir "
                        "el tweet ancla"
                    )
                    logger.error(self.ultimo_error)
                    return None

            # La ventana DEBE estar en la pagina del tweet: con el driver en
            # `chrome://new-tab-page`/`about:blank` (tab crashed o navegacion
            # que nunca ocurrio) se esperaban 12s+8s un boton inexistente.
            # Aqui se reintenta UNA navegacion y, si sigue sin X, se corta de
            # inmediato con un error reintentable por el motor.
            self._asegurar_pagina_tweet(url)

            if self._detectar_limite_cuenta():
                self.ultimo_error = "cuenta limitada por X"
                logger.error("Cuenta limitada, saltando respuesta")
                return None

            # La pagina del tweet puede tardar con proxy lento: esperar a que
            # exista el articulo (si no aparece, se sigue con el flujo actual).
            self._esperar_article_tweet(timeout=10)

            reply_btn = self._buscar_boton_responder(timeout=12)
            if reply_btn is None:
                # Distinguir un tweet NO respondible (respuestas limitadas,
                # eliminado...) de una simple carga lenta: con motivo no se
                # refresca ni se insiste (no cambiaria nada).
                motivo = self._motivo_no_respondible()
                if motivo:
                    self.ultimo_error = motivo
                    logger.warning(f"Tweet ancla no respondible: {motivo}")
                    return None

                # La SPA de X puede montar el boton tarde: UN refresh
                # (tolerando la carga lenta) y un segundo intento mas corto.
                logger.warning(
                    "timeout esperando boton Responder (12s); refrescando la "
                    "pagina del tweet"
                )
                try:
                    self.driver.refresh()
                except TimeoutException:
                    logger.warning("Refresh lento del tweet ancla; sigo con esperas")
                except Exception as e:
                    logger.warning(
                        f"Refresh del tweet ancla fallo ({type(e).__name__}: {e})"
                    )
                self._esperar_article_tweet(timeout=10)
                reply_btn = self._buscar_boton_responder(timeout=8)
                if reply_btn is None:
                    self.ultimo_error = (
                        "no se encontro el boton Responder del tweet "
                        f"({self._diagnostico_pagina()})"
                    )
                    logger.warning(
                        f"timeout esperando boton Responder tras refresh (8s): "
                        f"{self.ultimo_error}"
                    )
                    return None
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", reply_btn
                )
                time.sleep(0.5)
            except Exception:
                pass
            self.driver.execute_script("arguments[0].click();", reply_btn)
            time.sleep(random.uniform(0.5, 0.8))

            try:
                # Preferir el editor del modal de respuesta y, sobre todo, que
                # este VISIBLE (X monta varios composers: el texto podia caer
                # en uno oculto y el boton Responder quedar deshabilitado).
                editor = self._esperar_editor_visible(preferir_dialogo=True)
            except Exception as e:
                self.ultimo_error = str(e)
                logger.warning(self.ultimo_error)
                return None

            texto = self._reorganizar_hashtags(texto)
            texto = self._recortar_para_x(texto)
            self._pegar_texto(editor, texto)
            time.sleep(random.uniform(0.15, 0.35))

            if imagen_path and os.path.exists(imagen_path):
                self._subir_imagen(imagen_path)
                time.sleep(0.5)

            # Pausa humana: releer la respuesta antes de enviarla (mismo patron
            # que publicar_tweet).
            logger.debug(
                f"pausa humana antes de publicar la respuesta @{self.usuario} (1.8-3.5s)"
            )
            time.sleep(random.uniform(1.8, 3.5))

            # No clicar a ciegas: X mantiene el boton Responder deshabilitado
            # hasta que el editor registra el texto. Si no se habilita, el
            # reply NO se publico y hay que cortar aqui (el motor lo reintenta
            # sin riesgo de duplicar).
            publicar_btn = self._esperar_boton_post_habilitado(10, texto=texto)
            if publicar_btn is None:
                self.ultimo_error = (
                    "boton Responder deshabilitado: el texto no quedo en el "
                    "editor (el reply NO se publico)"
                )
                logger.warning(self.ultimo_error)
                return None
            self.driver.execute_script("arguments[0].click();", publicar_btn)

            # Verificacion real: no basta con hacer clic.
            if not self._verificar_publicacion():
                self.ultimo_error = "X no confirmo la publicación de la respuesta"
                logger.error(f"No se confirmo la respuesta de {self.usuario}")
                try:
                    self.driver.save_screenshot(
                        resolver_ruta("data/temp/twitter_no_respondido.png")
                    )
                    logger.error("Captura guardada: data/temp/twitter_no_respondido.png")
                except Exception:
                    pass
                return None

            logger.info(f"Respuesta publicada por {self.usuario}")

            # 3s de vista a la pantalla para confirmacion visual (patron del bot).
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 3s la pantalla visible para confirmacion visual...")
                time.sleep(3)

            url_respuesta = self._obtener_url_respuesta(url)
            # La URL de la RESPUESTA queda tambien en `ultima_url_publicada`
            # (respaldo del motor); si no se pudo capturar, se deja vacia para
            # no arrastrar la URL de una accion anterior.
            self.ultima_url_publicada = url_respuesta or ""
            logger.info(
                f"perf @{self.usuario}: total={self._ahora() - t_inicio:.1f}s (urls=1)"
            )
            return url_respuesta or True

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error respondiendo tweet para {self.usuario}: {e}")
            logger.info(
                f"perf @{self.usuario}: total={self._ahora() - t_inicio:.1f}s (urls=1)"
            )
            try:
                self.driver.save_screenshot(
                    resolver_ruta("data/temp/twitter_error_reply.png")
                )
            except Exception:
                pass
            return None

    def _limpiar_texto_x(self, texto: str) -> str:
        """Limpieza comun del texto a publicar en X (nunca lanza).

        - Colapsa espacios/tabs multiples.
        - Quita el espacio antes de puntuacion (` ,` -> `,`).
        - Colapsa puntuacion duplicada separada por espacio (`frase. ,` /
          `frase. .` -> `frase.`), artefacto de quitar un hashtag intercalado;
          no toca `...` (sin espacios intermedios).
        - Colapsa 3+ saltos de linea a 2 y quita espacios al inicio/fin de linea.
        """
        texto = texto or ""
        try:
            texto = re.sub(r"[ \t]+", " ", texto)
            texto = re.sub(r"([.!?,;:])\s+([.!?,;:])", r"\1", texto)
            texto = re.sub(r"\s+([,.;:!?])", r"\1", texto)
            texto = re.sub(r"\n{3,}", "\n\n", texto)
            texto = re.sub(r"[ \t]+\n", "\n", texto)
            texto = re.sub(r"\n[ \t]+", "\n", texto)
            texto = re.sub(r"[ \t]+", " ", texto)
        except Exception:
            pass
        return texto.strip()

    def _reorganizar_hashtags(self, texto: str) -> str:
        """Deja los hashtags en un punto natural del texto (nunca lanza).

        - Si la IA YA los integro (el texto no termina ni empieza con hashtag),
          se respetan donde estan: solo se limpian espacios y puntuacion.
          Esto evita el bug de "el desfile del , junto a" (hashtag borrado y
          espacio/coma huerfanos) y de partir frases por moverlos a la fuerza.
        - Si quedaron al final (o al inicio), se mueven EN GRUPO a la posicion
          justo despues de una puntuacion de frase/clausula (`.`, `!`, `?`,
          `,`, `;`, `:`, salto de linea) mas cercana a la mitad; si no hay
          puntuacion, al espacio mas cercano a la mitad; si no hay espacios,
          al final como ultimo recurso.
        - Nunca parte un hashtag y nunca deja ` ,`.
        """
        try:
            texto = texto or ""
            hashtags = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", texto)
            if not hashtags:
                return texto

            limpio = self._limpiar_texto_x(texto)

            # ¿Ya estan integrados en el cuerpo? Entonces NO se mueven.
            sin_cierre = limpio.rstrip(" \t\r\n.,;:!?…")
            termina_en_tag = bool(
                re.search(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+$", sin_cierre)
            )
            empieza_con_tag = bool(
                re.match(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", limpio.lstrip(" \t\r\n"))
            )
            if not termina_en_tag and not empieza_con_tag:
                return limpio

            base = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", limpio)
            base = self._limpiar_texto_x(base)
            tags = " ".join(hashtags)
            if not base:
                return tags

            mitad = len(base) // 2

            # Candidatos: justo despues de puntuacion + su espacio, o de un
            # salto de linea. Siempre con contenido despues (no al final).
            candidatos = []
            for m in re.finditer(r"[.!?,;:]\s+", base):
                if m.end() > 0 and base[m.end():].strip():
                    candidatos.append(m.end())
            for m in re.finditer(r"\n\s*", base):
                if m.end() > 0 and base[m.end():].strip():
                    candidatos.append(m.end())

            punto = None
            if candidatos:
                # Prefiere los que caen entre el 20% y el 80% del largo.
                minimo, maximo = len(base) * 0.2, len(base) * 0.8
                en_banda = [p for p in candidatos if minimo <= p <= maximo]
                punto = min(en_banda or candidatos, key=lambda p: abs(p - mitad))
            else:
                espacios = [m.start() for m in re.finditer(r"(?:\s|\n)", base)]
                if espacios:
                    punto = min(espacios, key=lambda p: abs(p - mitad))

            if punto is None:
                # Sin puntuacion ni espacios: ultimo recurso, al final.
                return self._limpiar_texto_x(f"{base} {tags}")

            izquierda = base[:punto].rstrip()
            derecha = base[punto:].lstrip()
            if not izquierda:
                resultado = f"{tags} {derecha}".strip()
            elif not derecha:
                resultado = f"{izquierda} {tags}".strip()
            else:
                resultado = f"{izquierda} {tags} {derecha}"
            return self._limpiar_texto_x(resultado)
        except Exception as e:
            logger.debug(f"_reorganizar_hashtags no pudo procesar el texto: {e}")
            return texto

    def _recortar_para_x(self, texto: str, limite: int = 280) -> str:
        """Recorta `texto` al limite de X sin partir palabras ni hashtags.

        X deshabilita el boton Post cuando el texto excede 280 caracteres.
        Si no cabe:
        1. Se corta en el ULTIMO cierre de frase (`.`, `!`, `?`) que quede
           dentro del limite y a partir del 55% del limite; queda una frase
           completa y NO se agrega `…`.
        2. Si no hay cierre de frase, se corta en el ultimo espacio anterior
           al limite y se agrega `…` (como antes).
        Si el corte cae dentro de un hashtag, se retrocede hasta antes del `#`.
        El resultado nunca excede `limite`. Nunca lanza.
        """
        texto = texto or ""
        try:
            limite = int(limite)
        except Exception:
            limite = 280
        if limite <= 0 or len(texto) <= limite:
            return texto

        def _corte_fuera_de_hashtag(pos: int) -> int:
            """Retrocede `pos` si cae dentro de un token `#...`."""
            for m in re.finditer(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", texto):
                if m.start() < pos < m.end():
                    return m.start()
            return pos

        # 1) Ultimo cierre de frase completo dentro del limite.
        minimo = int(limite * 0.55)
        mejor = None
        for m in re.finditer(r"[.!?](?=\s|$)", texto[:limite]):
            fin = m.end()
            if (
                fin == limite
                and len(texto) > limite
                and not texto[limite].isspace()
            ):
                # El `$` era el borde de la rebanada, no un cierre real.
                continue
            if fin >= minimo:
                mejor = fin
        if mejor is not None:
            corte_frase = _corte_fuera_de_hashtag(mejor)
            if corte_frase == mejor:
                recortado = texto[:mejor].rstrip()
            else:  # defensa: nunca dejar un hashtag partido
                recortado = self._limpiar_texto_x(texto[:corte_frase]) + "…"
            if recortado and len(recortado) <= limite:
                logger.warning(
                    f"Texto recortado para X (frase completa): "
                    f"{len(texto)} -> {len(recortado)} chars"
                )
                return recortado

        # 2) Ultimo espacio con indice < limite (reservando 1 char para `…`).
        corte = texto.rfind(" ", 0, limite)
        if corte <= 0:
            corte = max(0, limite - 1)
        corte = _corte_fuera_de_hashtag(corte)
        recortado = self._limpiar_texto_x(texto[:corte]) + "…"
        if len(recortado) > limite:  # salvaguarda: jamas exceder el limite
            recortado = recortado[:limite]
        logger.warning(
            f"Texto recortado para X: {len(texto)} -> {len(recortado)} chars"
        )
        return recortado

    def _normalizar_texto_editor(self, texto: str) -> str:
        """Normaliza para comparar (sin espacios ni signos, en minusculas)."""
        return re.sub(r"[\W_]+", "", texto or "", flags=re.UNICODE).lower()

    def _leer_texto_editor(self, elemento) -> str:
        """Junta el texto visible del editor contenteditable/input."""
        partes = []
        try:
            partes.append(elemento.text or "")
        except Exception:
            pass
        for atributo in ("textContent", "innerText", "value"):
            try:
                valor = elemento.get_attribute(atributo)
                if valor:
                    partes.append(valor)
            except Exception:
                continue
        return "\n".join(partes)

    def _leer_texto_editor_exacto(self, elemento) -> str:
        """Lectura UNICA del editor para medir su largo REAL (sin duplicados).

        `_leer_texto_editor` concatena `.text` + `textContent` + `innerText` +
        `value`; en un contenteditable eso es el MISMO contenido 2-3 veces y la
        longitud medida queda inflada (diagnostico real de Railway: "2632 chars
        en el editor visible" con ~3 borradores acumulados). Aqui se elige la
        fuente mas larga disponible: `value` para `input`/`textarea` y
        `.text`/`textContent`/`innerText` para contenteditable. Nunca lanza:
        devuelve `""` si no se pudo leer (p. ej. elemento stale).
        """
        candidatos = []
        for lector in (
            lambda: elemento.text or "",
            lambda: elemento.get_attribute("textContent") or "",
            lambda: elemento.get_attribute("innerText") or "",
            lambda: elemento.get_attribute("value") or "",
        ):
            try:
                valor = lector()
            except Exception:
                continue
            if valor:
                candidatos.append(valor)
        if not candidatos:
            return ""
        return max(candidatos, key=len)

    def _editor_vacio(self, elemento) -> bool:
        """True si el editor no tiene contenido (ignora espacios/zero-width).

        Mide sobre la lectura exacta (sin duplicados de `_leer_texto_editor`).
        Si el elemento esta stale (no se pudo leer) devuelve True: el flujo de
        pegado re-localiza el editor y la validacion posterior lo detecta.
        """
        try:
            texto = self._leer_texto_editor_exacto(elemento)
        except Exception:
            return True
        return not texto.replace("\u200b", "").strip()

    def _largo_editor(self, elemento) -> int:
        """Largo del texto REAL del editor (0 si no se pudo leer). Nunca lanza."""
        return len(self._leer_texto_editor_exacto(elemento))

    def _editor_con_restos(self, elemento, texto: str, margen: int = 30) -> int:
        """Largo TOTAL del editor si quedo sucio (0 = sin restos).

        Bug real de Railway: X restaura el borrador del composer entre
        intentos y el pegado lo ACUMULA (el texto nuevo se inserta junto al
        viejo). Si el editor supera `len(texto) + margen` chars quedaron
        restos; se devuelve su largo total para el mensaje de error. Nunca
        lanza (un fallo de lectura se trata como "sin restos": la verificacion
        `_verificar_texto_en_editor` ya valido que el texto esta).
        """
        try:
            largo = self._largo_editor(elemento)
        except Exception:
            return 0
        if largo > len(texto or "") + max(0, margen):
            return largo
        return 0

    def _verificar_texto_en_editor(
        self, elemento, texto: str, intentos: int = 3, espera: float = 0.3
    ) -> bool:
        """True si el editor contiene (aprox) el texto recien escrito.

        Compara por los primeros 20 caracteres alfanumericos normalizados: el
        editor de X puede cambiar saltos/espacios, pero nunca el inicio del
        texto. Sondea unas cuantas veces porque el portapapeles/React pueden
        tardar unos milisegundos en reflejarlo.
        """
        aguja = self._normalizar_texto_editor(texto)[:20]
        if not aguja:
            return True
        for _ in range(max(1, intentos)):
            try:
                contenido = self._normalizar_texto_editor(self._leer_texto_editor(elemento))
            except Exception:
                # StaleElementReferenceException (u otra): el elemento quedo
                # viejo; no se puede afirmar que el texto este.
                contenido = ""
            if aguja in contenido:
                return True
            time.sleep(espera)
        return False

    # Bit de modificador Ctrl para `Input.dispatchKeyEvent` de CDP
    # (Alt=1, Ctrl=2, Meta=4, Shift=8).
    _CDP_MOD_CTRL = 2

    def _cdp_tecla(self, tipo: str, key: str, code: str, vk: int, modifiers: int = 0):
        """Envia UNA tecla al elemento ENFOCADO via CDP (sin clic ni foco).

        `rawKeyDown` no genera evento `char` (los atajos de edicion se
        procesan en el `keydown`): es el patron que usa Playwright para
        Ctrl+A/Delete. El evento entra por el pipeline real de Chrome, asi que
        `selectAll`/borrado nativos se ejecutan de verdad (a diferencia del
        Range de JS, que React pierde al re-renderizar y hacia que
        `Input.insertText` insertara en el caret en vez de reemplazar).
        """
        self.driver.execute_cdp_cmd(
            "Input.dispatchKeyEvent",
            {
                "type": tipo,
                "modifiers": modifiers,
                "key": key,
                "code": code,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            },
        )

    def _limpiar_editor_cdp(self, elemento):
        """Vacia el editor con Ctrl+A + Delete por CDP (4 eventos).

        Sin clic (el `data-testid='mask'` del modal de X lo intercepta) y sin
        portapapeles (Railway no tiene xclip). `_enfocar_editable` deja el foco
        en el editable REAL: en algunas variantes `tweetTextarea_0` es un
        wrapper y el contenteditable es un descendiente.
        """
        self._enfocar_editable(elemento, seleccionar=False)
        ctrl = self._CDP_MOD_CTRL
        self._cdp_tecla("rawKeyDown", "a", "KeyA", 65, ctrl)
        self._cdp_tecla("keyUp", "a", "KeyA", 65, ctrl)
        self._cdp_tecla("rawKeyDown", "Delete", "Delete", 46)
        self._cdp_tecla("keyUp", "Delete", "Delete", 46)

    def _limpiar_editor_actionchains(self, elemento):
        """UN reintento de borrado con ActionChains (Ctrl+A + Delete).

        Sin clic: el foco ya lo dejo `_enfocar_editable` por JS y las teclas de
        W3C van al elemento activo, asi que el `mask` del modal no las
        intercepta. Se usa si la via CDP no dejo el editor vacio.
        """
        self._enfocar_editable(elemento, seleccionar=False)
        modifier = Keys.COMMAND if os.name == "posix" else Keys.CONTROL
        ActionChains(self.driver).key_down(modifier).send_keys("a").key_up(
            modifier
        ).send_keys(Keys.DELETE).perform()

    def _limpiar_editor_x(self, elemento):
        """Deja VACIO el editor antes de pegar (borrador restaurado por X).

        Bug real de Railway (pestaña persistente): X RESTAURA el borrador del
        composer entre intentos de la misma cuenta y el pegado se ACUMULABA
        (1800-2600 chars en el editor, > 280: boton Post deshabilitado para
        siempre, con un falso "el texto no quedo registrado"). La causa era el
        Range de `_enfocar_editable(seleccionar=True)`: React lo pierde al
        re-renderizar y `Input.insertText` inserta en el caret en vez de
        reemplazar. Aqui se limpia primero:
          1. CDP Ctrl+A + Delete sobre el editable enfocado.
          2. UN reintento con ActionChains (Ctrl+A + Delete), tambien sin clic.
        Lanza `"editor de X con borrador que no se pudo limpiar (N chars)"` si
        sigue sucio: nada se publico aun, el motor lo trata como fallo seguro.
        """
        if self._editor_vacio(elemento):
            return
        for intento, limpiador in enumerate(
            (self._limpiar_editor_cdp, self._limpiar_editor_actionchains), start=1
        ):
            try:
                limpiador(elemento)
            except Exception as e:
                logger.debug(
                    f"limpieza de borrador intento {intento} fallo "
                    f"({type(e).__name__}: {e}); se sigue al siguiente"
                )
            if self._editor_vacio(elemento):
                logger.info(
                    "Editor de X limpiado (borrador previo eliminado antes de escribir)"
                )
                return
        raise Exception(
            "editor de X con borrador que no se pudo limpiar "
            f"({self._largo_editor(elemento)} chars)"
        )

    def _destruir_mascara_modal(self) -> bool:
        """Destruye TODAS las capas `[data-testid="mask"]` del modal.

        Ghostban: la capa `mask` del modal interceptaba el clic del editor y
        obligaba a usar insercion silenciosa por CDP/JS (que X detecta). Se
        elimina ANTES de limpiar/escribir y tambien en el reintento limpio si
        React la vuelve a montar. Absorbe CUALQUIER excepcion del renderer
        (`TimeoutException`/`WebDriverException` transitorios incluidos) a
        DEBUG: nunca lanza, nunca usa `logger.error`; si el JS falla, el
        pegado continua con la red de seguridad de `_esperar_mask_desaparezca`.
        Devuelve True si el JS corrio sin excepcion.
        """
        try:
            self.driver.execute_script(
                "document.querySelectorAll('[data-testid=\"mask\"]')"
                ".forEach(e => e.remove());"
            )
            return True
        except Exception as e:
            logger.debug(
                f"No se pudo destruir la capa mask del modal "
                f"({type(e).__name__}: {e}); se continua con el pegado"
            )
            return False

    def _hay_mask_modal(self) -> bool:
        """True si hay alguna capa `[data-testid="mask"]` montada. Nunca lanza.

        Si el `is_displayed()` del elemento falla (nodo raro del renderer) se
        asume PRESENTE: es mejor un reintento limpio de mas que rendirse con el
        editor tapado.
        """
        try:
            elementos = self.driver.find_elements(
                By.CSS_SELECTOR, "[data-testid='mask']"
            )
        except Exception:
            return False
        for el in elementos:
            try:
                if el.is_displayed():
                    return True
            except Exception:
                return True
        return bool(elementos)

    def _pegar_texto(self, elemento, texto: str):
        """Escribe `texto` en el editor con EVENTOS REALES de teclado.

        X (Ghostban/Shadowban) oculta los posts de las cuentas que insertan
        texto de forma silenciosa: por eso YA NO se usa CDP `Input.insertText`,
        ni `document.execCommand('insertText')`, ni el portapapeles. El flujo es:
          0. Se DESTRUYE la capa `[data-testid="mask"]` del modal con JS: era la
             que interceptaba el clic y obligaba a los metodos silenciosos. Si
             el JS falla (driver raro) NO se aborta el pegado:
             `_esperar_mask_desaparezca` y el manejo de
             `ElementClickInterceptedException` quedan como red de seguridad.
          1. Se VACIA el editor (`_limpiar_editor_x`): X restaura el borrador del
             composer entre intentos de la misma cuenta y el pegado lo ACUMULABA
             (1800-2600 chars, > 280: boton Post deshabilitado para siempre).
          2. Metodo PRIMARIO: `editor.click()` envuelto en try/except (si el
             clic falla NO se aborta: `send_keys` enfoca por W3C) +
             `editor.send_keys(texto)` en UNA sola llamada: Chrome simula los
             eventos reales de teclado (nada de bucle char por char, que satura
             el renderer en Railway).
          3. FALLBACK: `ActionChains(driver).send_keys(texto)` (los mismos
             eventos de teclado a nivel W3C) sobre el editable REAL enfocado por
             JS (`_enfocar_editable`, solo foco, sin inyectar texto).

        Despues de escribir se valida el largo: si el editor quedo con restos
        (`_editor_con_restos`), se limpia y escribe UNA vez mas; si sigue sucio
        se lanza `"editor de X con borrador que no se pudo limpiar (N chars)"`
        (nada se publico: fallo seguro para el motor).

        Si la secuencia completa falla y hay mascara presente o el editor quedo
        bloqueado por un overlay, se hace UN reintento limpio (una sola
        pasada): re-destruir TODAS las capas `mask`, esperar <=1s, re-localizar
        el editor visible y reintentar la MISMA secuencia (send_keys ->
        ActionChains, eventos reales de teclado). El resto de fallos conserva
        el mensaje exacto `"no se pudo escribir el texto en el editor de X"`.

        El DOM de X (React) se re-renderiza y el `WebElement` guardado puede
        quedar viejo (`StaleElementReferenceException`), perdiendo el intento:
        cada metodo tiene hasta 2 pasadas y, si el elemento queda viejo, se
        RE-LOCALIZA el primer editor VISIBLE
        (`_buscar_editor_visible_actual`) y se reintenta con el fresco.

        Si ninguno deja el texto en el editor lanza una excepcion explicita
        (`"no se pudo escribir el texto en el editor de X"`) para que el fallo
        se reporte como tal, en vez de publicar en vacio y terminar en un falso
        "X no confirmo la publicacion".
        """
        texto = texto or ""
        if not texto:
            return

        # Ghostban: la capa `data-testid="mask"` del modal interceptaba el clic
        # de `send_keys` y obligaba a usar insercion silenciosa por CDP/JS, que
        # X SI detecta como automatizacion (oculta los posts). Se destruye
        # ANTES de limpiar/escribir; si el JS falla, el pegado continua con la
        # red de seguridad de `_intentar`/`_esperar_mask_desaparezca`. Absorbe
        # los TimeoutException/WebDriverException transitorios del renderer
        # (solo DEBUG, nunca ERROR).
        self._destruir_mascara_modal()

        estado = {"elemento": elemento}

        def _es_elemento_viejo(el) -> bool:
            """True si el elemento ya no esta en el DOM (quedo viejo)."""
            try:
                el.is_displayed()
                return False
            except StaleElementReferenceException:
                return True
            except Exception:
                return False

        def _relocalizar(timeout: float = 5.0) -> bool:
            """Re-localiza el editor visible actual; True si hay uno fresco."""
            try:
                fresco = self._buscar_editor_visible_actual(timeout=timeout)
            except Exception:
                fresco = None
            if fresco is None:
                return False
            estado["elemento"] = fresco
            return True

        def _esperar_mask_desaparezca(timeout: float = 2.0) -> bool:
            """Espera <=`timeout`s a que el overlay `data-testid='mask'` se vaya.

            El modal de X monta un `mask` que intercepta los clics
            (`ElementClickInterceptedException` en Railway); esperar a que
            desaparezca suele bastar para que el MISMO metodo funcione. True
            tambien si no hay mask. Nunca lanza.
            """
            try:
                WebDriverWait(self.driver, max(0.2, float(timeout))).until(
                    EC.invisibility_of_element_located(
                        (By.CSS_SELECTOR, "[data-testid='mask']")
                    )
                )
                return True
            except Exception:
                pass
            try:
                return not any(
                    el.is_displayed()
                    for el in self.driver.find_elements(
                        By.CSS_SELECTOR, "[data-testid='mask']"
                    )
                )
            except Exception:
                return False

        def _intentar(nombre, metodo) -> bool:
            """Ejecuta `metodo(elemento)` con hasta 2 pasadas. Nunca lanza.

            Si el elemento queda viejo (por la excepcion o porque el texto no
            quedo), re-localiza el editor visible y reintenta UNA vez con el
            elemento fresco. Si el clic lo intercepta el `mask` del modal
            (`ElementClickInterceptedException`), espera <=2s a que el mask
            desaparezca y reintenta UNA vez. Devuelve True SOLO si el texto
            quedo en el editor.
            """
            for intento in (1, 2):
                el = estado["elemento"]
                try:
                    metodo(el)
                except Exception as e:
                    if (
                        isinstance(e, ElementClickInterceptedException)
                        and intento == 1
                    ):
                        # Overlay `data-testid="mask"` del modal: espera corta
                        # (<=2s) a que desaparezca y reintenta UNA vez; si no,
                        # se sigue con el metodo siguiente como siempre.
                        if _esperar_mask_desaparezca(2.0):
                            logger.debug(
                                f"{nombre}: mask del modal intercepto el clic; "
                                f"desaparecio, reintentando"
                            )
                            continue
                        logger.debug(
                            f"{nombre}: mask sigue interceptando el clic; "
                            f"probando el siguiente metodo"
                        )
                        return False
                    if (
                        isinstance(
                            e,
                            (
                                StaleElementReferenceException,
                                InvalidElementStateException,
                            ),
                        )
                        and intento == 1
                        and _relocalizar()
                    ):
                        logger.debug(
                            f"{nombre}: editor viejo ({type(e).__name__}); "
                            f"re-localizado, reintentando"
                        )
                        continue
                    # PyperclipException = el contenedor no tiene mecanismo de
                    # portapapeles (sin xclip/xsel en Railway): es ESPERADO, no
                    # un problema del bot; se registra en debug.
                    if any(cls.__name__ == "PyperclipException" for cls in type(e).__mro__):
                        logger.debug(
                            f"Sin portapapeles disponible ({type(e).__name__}: {e}); "
                            f"probando el siguiente metodo"
                        )
                    else:
                        logger.warning(
                            f"{nombre} fallo ({type(e).__name__}: {e}); "
                            f"probando el siguiente metodo"
                        )
                    return False
                if self._verificar_texto_en_editor(estado["elemento"], texto):
                    return True
                # El metodo corrio pero el texto no quedo: si el elemento quedo
                # viejo (X re-renderizo el editor), re-localizar y reintentar.
                if intento == 1 and _es_elemento_viejo(el) and _relocalizar():
                    logger.warning(
                        f"{nombre} no dejo el texto y el editor quedo viejo; "
                        f"re-localizando y reintentando"
                    )
                    continue
                logger.warning(f"{nombre} no dejo el texto en el editor")
                return False
            return False

        # 1) PRIMARIO: `editor.click()` fuerza el foco (envuelto en try/except:
        #    si el clic falla NO se aborta, `send_keys` enfoca por W3C) y
        #    `send_keys(texto)` en UNA sola llamada genera los eventos REALES de
        #    teclado (keydown/keypress/input/keyup) que X exige para no marcar la
        #    cuenta como bot (Ghostban/Shadowban). Sin bucle char por char: miles
        #    de comandos al renderer son los que provocaban los `Timed out
        #    receiving message from renderer` en Railway.
        def _send_keys(el):
            try:
                el.click()
            except Exception as e:
                logger.debug(
                    f"click sobre el editor fallo ({type(e).__name__}: {e}); "
                    f"se continua con send_keys (enfoca por W3C)"
                )
            el.send_keys(texto)

        # 2) FALLBACK: los mismos eventos reales de teclado a nivel W3C, sobre
        #    el editable REAL enfocado por JS (`_enfocar_editable` solo enfoca,
        #    no inyecta texto): cubre los wrappers donde `tweetTextarea_0` no es
        #    el contenteditable.
        def _actionchains(el):
            self._enfocar_editable(el, seleccionar=False)
            ActionChains(self.driver).send_keys(texto).perform()

        def _escribir_con_metodos() -> bool:
            """Recorre los 2 metodos de escritura (mismo orden) hasta que el texto quede."""
            for nombre, metodo in (
                ("send_keys", _send_keys),
                ("actionchains", _actionchains),
            ):
                if _intentar(nombre, metodo):
                    return True
            return False

        def _bloqueo_de_overlay() -> bool:
            """True si una mascara/overlay explica el fallo de escritura.

            El mask del modal puede volver a montarse (React) o tapar el centro
            del editor (`elementFromPoint`) aunque el nodo no figure en el
            arbol: en ambos casos merece UN reintento limpio. Nunca lanza.
            """
            if self._hay_mask_modal():
                return True
            try:
                if self._editor_no_ocluido(estado["elemento"]) is False:
                    return True
            except Exception:
                pass
            return False

        def _reintento_limpio_tras_bloqueo() -> bool:
            """UN reintento limpio si la mascara/overlay bloqueo la escritura.

            Re-destruye TODAS las capas `[data-testid="mask"]` (absorbiendo
            cualquier excepcion del renderer), espera <=1s, re-localiza el
            editor VISIBLE y reintenta la MISMA secuencia de escritura (eventos
            reales de teclado). Devuelve True si el texto quedo; el flujo jamas
            hace mas de UNA pasada.
            """
            if not _bloqueo_de_overlay():
                return False
            logger.info(
                "editor de X bloqueado por la mascara del modal; reintento "
                "limpio (destruir mask + re-localizar + reescribir)"
            )
            self._destruir_mascara_modal()
            _esperar_mask_desaparezca(1.0)
            _relocalizar(timeout=1.0)
            return _escribir_con_metodos()

        # P0: ANTES de escribir, VACIAR el editor. X restaura el borrador del
        # composer entre intentos (misma cuenta/pestaña) y el pegado lo
        # ACUMULABA (1800-2600 chars > 280: el boton Post quedaba deshabilitado
        # para siempre). Si no se puede limpiar, se corta aqui con un error
        # explicito: nada se publico y el motor lo reintenta sin riesgo.
        self._limpiar_editor_x(estado["elemento"])

        if not _escribir_con_metodos():
            # Blindaje extra: si el mask volvio a montarse o el editor quedo
            # bloqueado por un overlay, UN reintento limpio antes de rendirse.
            if not _reintento_limpio_tras_bloqueo():
                raise Exception("no se pudo escribir el texto en el editor de X")

        # P0: el borrador puede restaurarse DESPUES de la limpieza (React) y
        # colarse junto al texto nuevo. Si el editor quedo con restos, se
        # limpia y se escribe UNA vez mas; si sigue sucio, error explicito.
        restante = self._editor_con_restos(estado["elemento"], texto)
        if restante <= 0:
            return
        logger.warning(
            f"El editor de X quedo con restos tras escribir ({restante} chars "
            f"para {len(texto)} intentados); limpiando y reescribiendo"
        )
        self._limpiar_editor_x(estado["elemento"])
        if _escribir_con_metodos():
            restante = self._editor_con_restos(estado["elemento"], texto)
            if restante <= 0:
                return
        else:
            raise Exception("no se pudo escribir el texto en el editor de X")

        raise Exception(
            "editor de X con borrador que no se pudo limpiar "
            f"({restante} chars)"
        )
    
    def _subir_imagen(self, imagen_path: str):
        try:
            input_file = self.driver.find_element(By.CSS_SELECTOR, "input[type='file'][accept*='image']")
            input_file.send_keys(os.path.abspath(imagen_path))
            # La espera del boton Post habilitado (o la verificacion real)
            # absorbe el tiempo de subida; 3s fijos eran de mas.
            time.sleep(1.5)
        except Exception as e:
            logger.error(f"Error subiendo imagen: {e}")
    
    def _esperar_rt_confirmado(self, timeout: int = 12) -> bool:
        """True si el tweet objetivo quedo retwitteado.

        Tras aplicar el RT, X cambia el boton `[data-testid='retweet']` por
        `[data-testid='unretweet']`. Se revisa SOLO el primer `article` (en una
        pagina de status es el tweet objetivo) para no confundirlo con
        respuestas de la conversacion ya retwitteadas.
        """
        fin = time.time() + timeout
        while time.time() < fin:
            try:
                articulos = self.driver.find_elements(
                    By.CSS_SELECTOR, "article[data-testid='tweet']"
                )
                if articulos:
                    if articulos[0].find_elements(By.CSS_SELECTOR, "[data-testid='unretweet']"):
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _unretweet_visible(self) -> bool:
        """True si el tweet objetivo YA quedo retwitteado (estado `unretweet`).

        Se revisa SOLO el primer `article` (en una pagina de status es el tweet
        objetivo) para no confundirlo con respuestas ya retwitteadas; si no hay
        ningun `article` (variante A/B del DOM) se cae a la pagina completa.
        Nunca lanza: ante cualquier error devuelve False.
        """
        try:
            articulos = self.driver.find_elements(
                By.CSS_SELECTOR, "article[data-testid='tweet']"
            )
            if articulos:
                return bool(
                    articulos[0].find_elements(
                        By.CSS_SELECTOR, "[data-testid='unretweet']"
                    )
                )
            return bool(
                self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-testid='unretweet']"
                )
            )
        except Exception as e:
            logger.debug(f"No se pudo leer el estado del retweet: {e}")
            return False

    # Selectores del boton de retweet que X A/B alterna. `retweet` es el
    # historico; algunas variantes usan `repost` o solo aria-label/texto.
    _RETWEET_SELECTORES = (
        (By.CSS_SELECTOR, "[data-testid='retweet']"),
        (By.CSS_SELECTOR, "[data-testid='repost']"),
        (
            By.XPATH,
            "//*[@role='button'][@aria-label='Repost' or @aria-label='Repostear' "
            "or @aria-label='Retweet']",
        ),
        (
            By.XPATH,
            "//div[@role='button'][.//span[text()='Repost' or text()='Repostear' "
            "or text()='Retweet']]",
        ),
    )

    def _buscar_boton_retweet(self):
        """Devuelve el boton de RT visible (o None) SIN esperar.

        Prueba, en orden: `[data-testid='retweet']`, `[data-testid='repost']`,
        el aria-label "Repost"/"Repostear"/"Retweet" y el texto visible
        equivalente. Nunca lanza.
        """
        for by, sel in self._RETWEET_SELECTORES:
            try:
                for btn in self.driver.find_elements(by, sel):
                    if btn.is_displayed() and btn.is_enabled():
                        logger.info(f"Boton Retweet encontrado con selector: {sel}")
                        return btn
            except Exception:
                continue
        return None

    def _tweet_ya_tiene_like(self) -> bool:
        """True si el tweet de la pagina actual ya tiene like (NO navega).

        En X el boton cambia de estado: sin like suele ser
        `[data-testid='like']` y con like `[data-testid='unlike']`; algunas
        versiones mantienen el testid `like` con `aria-pressed='true'`. Se
        soportan AMBOS casos. Nunca lanza: ante cualquier error devuelve False.
        """
        try:
            if self.driver is None:
                return False
            if self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='unlike']"):
                return True
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='like']"):
                try:
                    pressed = (btn.get_attribute("aria-pressed") or "").strip().lower()
                except Exception:
                    pressed = ""
                if pressed == "true":
                    return True
            return False
        except Exception as e:
            logger.debug(f"No se pudo leer el estado del like: {e}")
            return False

    def _dar_like_en_pagina_actual(self) -> bool:
        """Da like al tweet de la pagina actual de forma IDEMPOTENTE.

        - Si el tweet ya tiene like (`_tweet_ya_tiene_like`) no toca nada.
        - Si no, busca `[data-testid='like']` (WebDriverWait corto), clica con
          JS y verifica; si el primer clic no registro, hace UN solo reintento
          (re-buscando el boton, porque X re-renderiza el DOM).
        - NUNCA clica `[data-testid='unlike']`: eso QUITARIA el like.

        Devuelve True si el tweet quedo con like (ya fuera o nuevo); False si no
        se pudo. Nunca lanza.
        """
        try:
            if self.driver is None:
                return False

            if self._tweet_ya_tiene_like():
                logger.info("el tweet ya tenía like; no se toca")
                return True

            try:
                btn = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='like']"))
                )
            except TimeoutException:
                logger.warning(
                    "No aparecio el boton de like (posible 'unlike' con testid raro)"
                )
                return False

            # Defensa extra: jamas clicar un boton que quite el like.
            try:
                if (btn.get_attribute("data-testid") or "").strip().lower() == "unlike":
                    logger.info("el tweet ya tenía like; no se toca")
                    return True
            except Exception:
                pass

            for intento in range(2):
                try:
                    self.driver.execute_script("arguments[0].click();", btn)
                except Exception as e:
                    logger.warning(f"Fallo el clic de like (intento {intento + 1}): {e}")
                time.sleep(random.uniform(0.4, 0.8))
                if self._tweet_ya_tiene_like():
                    logger.info("Like registrado en el tweet")
                    return True
                if intento == 0:
                    logger.warning("El like no registro; reintentando UNA vez")
                    try:
                        btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='like']")
                    except Exception:
                        return False

            logger.warning("El like no se pudo confirmar tras 2 intentos")
            return False
        except Exception as e:
            logger.warning(f"No se pudo dar like: {e}")
            return False

    def solo_retwittear(
        self,
        targets: list[str],
        usuario: str,
        mensaje_cita: str = None,
        es_calentamiento: bool = False,
        dar_like: bool = False,
        imagen_path: Optional[str] = None
    ) -> dict:
        """Hace RT (simple o con cita) de las URLs recibidas.

        Para la distribucion horaria se usa con UNA sola URL: hace exactamente
        un RT y devuelve `{"exitos": 1, "urls": []}` (el RT simple NO crea una
        publicacion propia, asi que no genera link; la tabla lo etiqueta).
        En las CITAS si se intenta capturar la URL del post de la cita (ruta
        rapida del timeline y, si falla, el perfil); si no se puede, `urls`
        queda sin esa entrada. Con varias URLs mantiene el recorrido secuencial
        en el mismo navegador.

        Verifica cada publicacion REALMENTE (estado `unretweet` en RT simple y
        `_verificar_publicacion()` en citas) antes de contar el exito, por lo
        que `exitos`/`fallidos`/`urls` reflejan el resultado real por URL.
        """
        resultados = {"exitos": 0, "fallidos": 0, "urls": []}
        
        if not self.driver:
            if not self.login_con_cookies():
                return resultados

        t_inicio = self._ahora()
        for url in targets:
            try:
                try:
                    self.driver.get(url)
                except TimeoutException:
                    # Carga lenta (proxy intermitente): la pagina suele seguir
                    # cargando; las esperas explicitas de elementos deciden.
                    logger.warning(f"Carga lenta de {url}; sigo con esperas explicitas")
                time.sleep(0.3)

                # Interstitial real (logs de Railway): un get atascado deja la
                # pagina de error de X o el challenge anti-bot montados y
                # esperar 12s+8s el boton era tiro perdido. UN refresh
                # tolerante aqui y fallo RAPIDO con detalle si persiste.
                estado_interstitial = self._recuperar_interstitial(url)
                if estado_interstitial == "anti-bot":
                    raise Exception(
                        "X pidió verificación anti-bot (Cloudflare) "
                        f"(interstitial) ({self._diagnostico_pagina()})"
                    )
                if estado_interstitial == "error":
                    raise Exception(
                        "pagina de error de X (interstitial) "
                        f"({self._diagnostico_pagina()})"
                    )
                if estado_interstitial == "driver":
                    raise Exception(
                        self.ultimo_error or "tab crashed/navegador sin navegar"
                    )

                if self._sesion_cdp and self._hay_muro_login():
                    # Sesion CDP invalida: UN fallback a login lento y UN
                    # reintento de la navegacion (misma semantica que el resto).
                    if self._revivir_sesion_cdp():
                        try:
                            self.driver.get(url)
                        except TimeoutException:
                            logger.warning(
                                f"Carga lenta de {url} tras login; sigo con esperas explicitas"
                            )
                        time.sleep(0.3)
                    if self._hay_muro_login():
                        self.ultimo_error = (
                            "sesión de X expirada o inválida: se pidió login al "
                            "abrir el tweet"
                        )
                        logger.error(self.ultimo_error)
                        resultados["fallidos"] += 1
                        break
                
                if self._detectar_limite_cuenta():
                    self.ultimo_error = "cuenta limitada por X"
                    break

                # Antes de esperar el boton (12s): dar tiempo a que monte el
                # tweet objetivo (proxy lento) y cortar de inmediato si algo es
                # concluyente (muro de login o RT ya hecho).
                self._esperar_article_tweet(8)
                if self._hay_muro_login():
                    self.ultimo_error = (
                        "sesión de X expirada o inválida: se pidió login al "
                        "abrir el tweet"
                    )
                    logger.error(self.ultimo_error)
                    resultados["fallidos"] += 1
                    break
                if not mensaje_cita and self._unretweet_visible():
                    # Si el tweet objetivo YA esta retwitteado (p. ej. un timeout
                    # ambiguo anterior), X reemplaza 'retweet' por 'unretweet' y
                    # el boton no aparecera nunca: se cuenta como exito sin
                    # duplicar. Solo RT simple: en cita, un RT simple existente
                    # no es la cita.
                    logger.info(
                        f"El tweet ya estaba retwitteado, se cuenta como exito: {url}"
                    )
                    resultados["exitos"] += 1
                    # RT simple: no genera publicacion propia => SIN link (antes
                    # se agregaba el perfil de la cuenta, que no es una
                    # publicacion).
                    self.ultima_url_publicada = ""
                    time.sleep(random.uniform(0.2, 0.5))
                    continue

                # X A/B: variantes del boton (testid/aria-label/texto) que no
                # requieren esperar; si no esta, se usa la espera explicita.
                rt_btn = self._buscar_boton_retweet()
                if rt_btn is None:
                    try:
                        rt_btn = WebDriverWait(self.driver, 12).until(
                            EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='retweet']"))
                        )
                    except TimeoutException:
                        # Un timeout no siempre significa que no se pueda
                        # retwittear: con proxys lentos el primer render queda a
                        # medias. UN refresh + espera explicita extra suele bastar.
                        # Presupuesto agresivo: 12s + 8s = 20s (antes 30s + 20s),
                        # para que un fallo no bloquee la campana.
                        logger.warning(
                            f"timeout esperando boton retweet (12s) en {url}; "
                            "refrescando la pagina e intentando de nuevo"
                        )
                        try:
                            self.driver.refresh()
                        except TimeoutException:
                            logger.warning(
                                f"Refresh lento de {url}; sigo con esperas explicitas"
                            )
                        rt_btn = None
                        try:
                            rt_btn = WebDriverWait(self.driver, 8).until(
                                EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='retweet']"))
                            )
                        except TimeoutException:
                            # Ultimo recurso A/B: variantes por aria-label/texto.
                            rt_btn = self._buscar_boton_retweet()
                            if rt_btn is None:
                                if self._detectar_cuenta_propia_suspendida():
                                    self.cuenta_suspendida = True
                                    raise Exception("cuenta suspendida/bloqueada por X")
                                motivo = self._detectar_tweet_no_disponible()
                                logger.warning(
                                    f"timeout esperando boton retweet tras refresh (8s) en {url}"
                                )
                                raise Exception(
                                    motivo or "boton de retweet no encontrado (carga lenta o cambio de interfaz)"
                                )
                rt_btn.click()
                time.sleep(0.3)
                
                if mensaje_cita:
                    # Elegir la opcion "Quote" del menu (NO "Retweet") para citar
                    quote_btn = self._buscar_opcion_quote()
                    self.driver.execute_script("arguments[0].click();", quote_btn)
                    # `_buscar_opcion_quote` espera a que el modal monte su
                    # editor; la pausa fija de 2s era innecesaria.
                    time.sleep(random.uniform(0.5, 0.9))
                    
                    # P0-D: el modal de cita monta el editor dentro de un
                    # `div[role='dialog']`; sin `preferir_dialogo` se elegia el
                    # composer inline de la pagina (tapado por el mask del
                    # modal) y el pegado fallaba.
                    editor = self._esperar_editor_visible(preferir_dialogo=True)
                    mensaje = self._recortar_para_x(mensaje_cita)
                    self._pegar_texto(editor, mensaje)
                    time.sleep(0.3)
                    
                    if imagen_path and os.path.exists(imagen_path):
                        self._subir_imagen(imagen_path)
                        time.sleep(0.5)
                    
                    # Pausa humana: releer la cita antes de publicarla (solo la
                    # rama de CITA; el RT simple no escribe texto).
                    logger.debug(
                        f"pausa humana antes de publicar la cita @{self.usuario} (1.8-3.5s)"
                    )
                    time.sleep(random.uniform(1.8, 3.5))

                    publicar_btn = self._buscar_boton_post()
                    self.driver.execute_script("arguments[0].click();", publicar_btn)
                    
                    # Verificacion real: no contar exito solo por hacer clic.
                    if not self._verificar_publicacion():
                        raise Exception("X no confirmo la publicacion de la cita")
                else:
                    rt_option = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='retweetConfirm']"))
                    )
                    self.driver.execute_script("arguments[0].click();", rt_option)
                    
                    # Verificacion real: el boton del tweet objetivo debe pasar
                    # a estado "unretweet".
                    if not self._esperar_rt_confirmado():
                        raise Exception("X no confirmo el RT (sin estado 'unretweet')")
                
                # El resultado ya esta verificado (unretweet/toast): no hace
                # falta dormir 2s antes de cerrar la accion.
                time.sleep(0.3)
                
                if dar_like:
                    # Idempotente: si la cuenta ya tiene like en este tweet NO
                    # se toca (antes se clicaba a ciegas y podia QUITARLO). El
                    # fallo del like no tumba el RT/cita ya confirmado.
                    if not self._dar_like_en_pagina_actual():
                        logger.warning(
                            f"No se pudo confirmar el like (el RT sigue contando): {url}"
                        )
                
                resultados["exitos"] += 1
                logger.info(f"RT exitoso: {url}")
                
                if mensaje_cita:
                    # Cita: intentar la URL del POST DE LA CITA por la ruta
                    # rapida (timeline actual, sin navegar) y, solo si falla,
                    # por el perfil.
                    url_publicada = self._obtener_enlace_reciente_timeline(
                        mensaje
                    )
                    if not url_publicada:
                        url_publicada = self._obtener_ultimo_enlace(self.usuario)
                    if url_publicada:
                        resultados["urls"].append(url_publicada)
                        self.ultima_url_publicada = url_publicada
                else:
                    # El RT simple no genera un post propio: sin URL de
                    # publicacion (antes se enlazaba el perfil de la cuenta).
                    self.ultima_url_publicada = ""
                
                time.sleep(random.uniform(0.4, 1.2))
            
            except Exception as e:
                resultados["fallidos"] += 1
                self.ultimo_error = f"{type(e).__name__}: {e}"
                logger.error(f"Error en RT: {e}")
        
        logger.info(
            f"perf @{self.usuario}: total={self._ahora() - t_inicio:.1f}s "
            f"(urls={len(targets)})"
        )
        return resultados
    
    def retweet(self, url: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url)
            time.sleep(3)
            
            rt_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='retweet']")
            rt_btn.click()
            time.sleep(1)
            
            rt_option = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='retweetConfirm']")
            rt_option.click()
            
            time.sleep(2)
            logger.info(f"RT hecho por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en retweet: {e}")
            return False
    
    def asegurar_like(self, url: str) -> bool:
        """Asegura que el tweet `url` quede con like (idempotente).

        Inicia sesion si hace falta, navega a `url` (tolerando un
        `TimeoutException` de carga lenta, como el resto del bot), espera a que
        aparezca `article[data-testid='tweet']` y delega en
        `_dar_like_en_pagina_actual()`.

        Devuelve True = el tweet quedo con like (ya fuera de antes o recien
        dado). JAMAS quita un like existente ni da like dos veces: si la cuenta
        ya tenia like en ese tweet, no hace clic. False = no se pudo confirmar
        (el motivo queda en `self.ultimo_error`).
        """
        self.ultimo_error = ""

        if not (url or "").strip():
            self.ultimo_error = "falta la URL del tweet para el like"
            return False

        if not self.driver:
            if not self.login_con_cookies():
                self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                return False

        try:
            try:
                self.driver.get(url)
            except TimeoutException:
                # Carga lenta (proxy intermitente): la pagina suele seguir
                # cargando; las esperas explicitas de elementos deciden.
                logger.warning(f"Carga lenta de {url}; sigo con esperas explicitas")

            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "article[data-testid='tweet']")
                    )
                )
            except TimeoutException:
                self.ultimo_error = "el tweet no cargo (timeout esperando el articulo)"
                logger.warning(f"{self.ultimo_error}: {url}")
                return False

            if self._dar_like_en_pagina_actual():
                logger.info(f"Like asegurado por {self.usuario}: {url}")
                return True

            self.ultimo_error = self.ultimo_error or (
                "no se pudo confirmar el like en el tweet (ya tenia like o la UI no respondio)"
            )
            logger.warning(f"No se pudo asegurar el like: {url}")
            return False

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"Error asegurando like: {e}")
            return False

    def like(self, url: str) -> bool:
        """Da like al tweet `url` SOLO si aun no tiene like (idempotente).

        Delega en `asegurar_like(url)`: True = el tweet quedo con like (ya
        fuera o nuevo); JAMAS quita un like existente ni da like dos veces.
        """
        return self.asegurar_like(url)
    
    def _gestionar_pin_mensajes(self) -> bool:
        try:
            pin_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='text']")
            pin_input.send_keys("0000")
            
            enviar_btn = self.driver.find_element(By.XPATH, "//span[text()='Enviar']")
            enviar_btn.click()
            
            time.sleep(2)
            return True
        
        except:
            return False
    
    def _desbloquear_mensajes(self) -> bool:
        try:
            self.driver.get(f"{self.base_url}/messages")
            time.sleep(3)
            
            if "login" in self.driver.current_url.lower():
                return False
            
            try:
                self._gestionar_pin_mensajes()
            except:
                pass
            
            return True
        
        except Exception as e:
            logger.error(f"Error desbloqueando mensajes: {e}")
            return False
    
    def enviar_link_a_grupos(
        self,
        link: str,
        mensaje: str,
        usuario: str,
        nombres_grupos: list[str]
    ) -> dict:
        resultados = {"enviados": 0, "fallidos": 0}
        
        if not self.driver:
            if not self.login_con_cookies():
                return resultados
        
        try:
            self.driver.get(link)
            time.sleep(3)
            
            compartir_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='share']")
            compartir_btn.click()
            time.sleep(1)
            
            enviar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='sendDMFromTweet']")
            enviar_btn.click()
            time.sleep(2)
            
            for grupo in nombres_grupos:
                try:
                    busqueda = self.driver.find_element(By.CSS_SELECTOR, "input[name='searchBox']")
                    busqueda.send_keys(grupo)
                    time.sleep(2)
                    
                    resultado = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='userCell']")
                    resultado.click()
                    time.sleep(1)
                    
                    mensaje_input = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='dmTextInput']")
                    mensaje_input.send_keys(mensaje)
                    
                    enviar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='dmComposerSendButton']")
                    enviar_btn.click()
                    
                    time.sleep(2)
                    resultados["enviados"] += 1
                
                except Exception as e:
                    resultados["fallidos"] += 1
                    logger.error(f"Error enviando a grupo: {e}")
        
        except Exception as e:
            logger.error(f"Error en enviar_link_a_grupos: {e}")
        
        return resultados
    
    def recopilar_links_de_grupos(self, usuario: str) -> list[str]:
        links = []
        
        if not self.driver:
            if not self.login_con_cookies():
                return links
        
        try:
            self.driver.get(f"{self.base_url}/messages")
            time.sleep(3)
            
            chats = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='conversation']")
            
            for chat in chats[:10]:
                try:
                    chat.click()
                    time.sleep(2)
                    
                    mensajes = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='messageText']")
                    
                    for mensaje in mensajes:
                        texto = mensaje.text
                        url_match = re.findall(r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+', texto)
                        
                        for url in url_match:
                            if usuario.lower() not in url.lower():
                                links.append(url)
                    
                    time.sleep(1)
                
                except:
                    continue
        
        except Exception as e:
            logger.error(f"Error recopilando links: {e}")
        
        return list(set(links))
    
    def compartir_en_grupos_dm(
        self,
        link: str,
        mensaje: str,
        usuario: str
    ) -> bool:
        try:
            self._desbloquear_mensajes()
            
            self.driver.get(link)
            time.sleep(3)
            
            compartir_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='share']")
            compartir_btn.click()
            time.sleep(1)
            
            enviar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='sendDMFromTweet']")
            enviar_btn.click()
            time.sleep(2)
            
            grupos = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='userCell']")
            
            for grupo in grupos[:5]:
                try:
                    grupo.click()
                    time.sleep(1)
                except:
                    continue
            
            mensaje_input = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='dmTextInput']")
            mensaje_input.send_keys(mensaje)
            
            enviar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='dmComposerSendButton']")
            enviar_btn.click()
            
            time.sleep(3)
            return True
        
        except Exception as e:
            logger.error(f"Error compartiendo en grupos: {e}")
            return False
    
    def buscar_google_completo(self, cliente: str, dias: int = 7) -> list[dict]:
        resultados = []
        
        try:
            queries = [
                f'"{cliente}" noticias',
                f'"{cliente}" Mexico',
                f'"{cliente}" gobierno'
            ]
            
            for query in queries:
                self.driver.get(f"https://www.google.com/search?q={query}&tbs=qdr:d{dias}")
                time.sleep(3)
                
                enlaces = self.driver.find_elements(By.CSS_SELECTOR, "div.g a")
                
                for enlace in enlaces[:10]:
                    try:
                        url = enlace.get_attribute("href")
                        titulo = enlace.find_element(By.CSS_SELECTOR, "h3").text
                        
                        resultados.append({
                            "titulo": titulo,
                            "url": url,
                            "fuente": "Google",
                            "cliente": cliente
                        })
                    except:
                        continue
                
                time.sleep(2)
        
        except Exception as e:
            logger.error(f"Error en busqueda Google: {e}")
        
        return resultados
    
    def buscar_twitter_x(self, cliente: str) -> list[dict]:
        resultados = []
        
        try:
            self.driver.get(f"{self.base_url}/search?q={cliente}&src=typed_query&f=live")
            time.sleep(4)
            
            tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            
            for tweet in tweets[:20]:
                try:
                    texto = tweet.text
                    enlace = tweet.find_element(By.CSS_SELECTOR, "a[href*='/status/']").get_attribute("href")
                    
                    resultados.append({
                        "titulo": texto[:200],
                        "url": enlace,
                        "fuente": "Twitter/X",
                        "cliente": cliente
                    })
                except:
                    continue
        
        except Exception as e:
            logger.error(f"Error en busqueda Twitter: {e}")
        
        return resultados
    
    def buscar_instagram(self, cliente: str) -> list[dict]:
        resultados = []
        
        try:
            hashtag = cliente.replace(" ", "").lower()
            self.driver.get(f"https://www.instagram.com/explore/tags/{hashtag}/")
            time.sleep(4)
            
            posts = self.driver.find_elements(By.CSS_SELECTOR, "article a[href*='/p/']")
            
            for post in posts[:10]:
                try:
                    url = post.get_attribute("href")
                    resultados.append({
                        "titulo": f"Post de Instagram: {cliente}",
                        "url": url,
                        "fuente": "Instagram",
                        "cliente": cliente
                    })
                except:
                    continue
        
        except Exception as e:
            logger.error(f"Error en busqueda Instagram: {e}")
        
        return resultados
    
    def buscar_facebook(self, cliente: str) -> list[dict]:
        resultados = []
        
        try:
            self.driver.get(f"https://www.facebook.com/search/posts/?q={cliente}")
            time.sleep(4)
            
            posts = self.driver.find_elements(By.CSS_SELECTOR, "div[data-ad-rendering-role='story_message']")
            
            for post in posts[:10]:
                try:
                    texto = post.text
                    resultados.append({
                        "titulo": texto[:200],
                        "url": "https://facebook.com",
                        "fuente": "Facebook",
                        "cliente": cliente
                    })
                except:
                    continue
        
        except Exception as e:
            logger.error(f"Error en busqueda Facebook: {e}")
        
        return resultados
    
    def monitoreo_completo(
        self,
        cliente: str,
        buscar_google: bool = True,
        buscar_twitter: bool = True,
        buscar_instagram: bool = True,
        buscar_facebook: bool = True,
        dias: int = 7
    ) -> dict:
        resultados = {
            "cliente": cliente,
            "google": [],
            "twitter": [],
            "instagram": [],
            "facebook": [],
            "total": 0
        }
        
        if not self.driver:
            if not self.login_con_cookies():
                return resultados
        
        if buscar_google:
            resultados["google"] = self.buscar_google_completo(cliente, dias)
        
        if buscar_twitter:
            resultados["twitter"] = self.buscar_twitter_x(cliente)
        
        if buscar_instagram:
            resultados["instagram"] = self.buscar_instagram(cliente)
        
        if buscar_facebook:
            resultados["facebook"] = self.buscar_facebook(cliente)
        
        resultados["total"] = (
            len(resultados["google"]) +
            len(resultados["twitter"]) +
            len(resultados["instagram"]) +
            len(resultados["facebook"])
        )
        
        return resultados
    
    # ------------------------------------------------------------------
    # Reportar contenido en X (posts y cuentas)
    # ------------------------------------------------------------------
    # La UI de X alterna espanol/ingles y cambia textos/estructura con
    # frecuencia: este flujo se resuelve por TEXTO visible bilingue (con
    # selectores de respaldo) y verifica el resultado REAL. Nunca se declara
    # exito solo por hacer clic; si el motivo pedido no coincide con ninguna
    # opcion VISIBLE no se elige al azar: las opciones reales quedan en
    # `self.ultimo_error` y se falla.
    #
    # `dry_run=True` es SOLO para verificacion (smoke): recorre el flujo hasta
    # el boton de envio y cierra con Escape/boton cerrar SIN enviar el reporte.

    # Motivos EN/ES que la UI de X usa (posts). El orden importa: los textos
    # mas especificos van primero para no confundir opciones parecidas
    # ("It's abusive or harmful" vs "It's spam").
    # Textos REALES observados en el modal espanol de X (2026): "Spam; Odio,
    # abuso o acoso; Discurso violento; Contenido multimedia de caracter
    # grafico o violento; Suplantacion de identidad; Contenido sexual para
    # adultos; Suicidio o autolesiones; Integridad civica..." + boton
    # "Siguiente" (segunda pantalla de sub-opciones antes del envio final).
    _MOTIVOS_REPORTE_POST = {
        "spam": ("spam", "it's spam", "es spam", "esto es spam"),
        "hate": (
            "odio, abuso o acoso", "hateful", "discurso de odio",
            "incitacion al odio", "acoso o incitacion al odio", "odio",
            "acoso", "hate",
        ),
        "abuse": (
            "odio, abuso o acoso", "abuso o acoso", "it's abusive or harmful",
            "es abusivo o danino", "abusive", "abusivo", "abuso",
        ),
        "violence": (
            "discurso violento", "contenido multimedia de caracter grafico o violento",
            "violent", "violencia", "violento",
        ),
        "self_harm": (
            "suicidio o autolesiones", "self-harm", "self harm",
            "intentions of self-harm", "autolesion", "autolesiones",
            "suicide", "suicidio",
        ),
        "sensitive": (
            "contenido sexual para adultos",
            "contenido multimedia de caracter grafico o violento",
            "sensitive photo or video", "sensitive media",
            "muestra una foto o video sensible", "contenido sensible",
            "sensitive", "sensible",
        ),
        "impersonation": (
            "suplantacion de identidad", "pretending to be someone else",
            "se hacen pasar por", "impersonation", "suplantacion",
            "suplantar",
        ),
        "nudity": (
            "contenido sexual para adultos", "contenido sexual", "nudity",
            "desnudez", "sexual",
        ),
        "false_info": (
            "integridad civica", "false information", "informacion falsa",
            "misleading", "enganoso",
        ),
    }

    # Motivos EN/ES para reportar CUENTAS (los textos de X difieren de los de
    # posts: "Their account is spam", "They're being abusive or harmful"...).
    # La pantalla de motivos de CUENTA de X usa la MISMA lista de categorias
    # (sin "Integridad civica" en el smoke real) y tambien boton "Siguiente".
    _MOTIVOS_REPORTE_CUENTA = {
        "spam": (
            "spam", "their account is spam", "su cuenta es spam",
            "account is spam", "es spam",
        ),
        "hate": (
            "odio, abuso o acoso", "hateful", "discurso de odio",
            "incitacion al odio", "odio", "acoso", "hate",
        ),
        "abuse": (
            "odio, abuso o acoso", "abuso o acoso",
            "they're being abusive or harmful", "es abusivo o danino",
            "abusive or harmful", "abusive", "abusivo", "abuso",
        ),
        "violence": (
            "discurso violento", "contenido multimedia de caracter grafico o violento",
            "violent", "violencia", "violento",
        ),
        "self_harm": (
            "suicidio o autolesiones", "self-harm", "self harm",
            "intentions of self-harm", "autolesion", "autolesiones",
            "suicide", "suicidio",
        ),
        "sensitive": (
            "contenido sexual para adultos",
            "contenido multimedia de caracter grafico o violento",
            "contenido sensible", "sensitive", "sensible",
        ),
        "impersonation": (
            "suplantacion de identidad", "pretending to be someone else",
            "se hacen pasar por", "impersonation", "suplantacion",
            "suplantar",
        ),
        "nudity": (
            "contenido sexual para adultos", "contenido sexual", "nudity",
            "desnudez", "sexual",
        ),
        "false_info": (
            "integridad civica", "false information", "informacion falsa",
            "misleading", "enganoso",
        ),
    }

    # Alias tolerantes (la web puede mandar claves historicas como "nudity" o
    # "false_info"; el motivo se normaliza antes de buscar la tabla).
    _ALIAS_MOTIVO_REPORTE = {
        "selfharm": "self_harm",
        "autolesion": "self_harm",
        "autolesiones": "self_harm",
        "odio": "hate",
        "acoso": "hate",
        "harassment": "hate",
        "abuso": "abuse",
        "abusivo": "abuse",
        "violento": "violence",
        "sensible": "sensitive",
        "desnudez": "nudity",
        "sexual": "nudity",
        "suplantacion": "impersonation",
        "false_information": "false_info",
        "misleading": "false_info",
        "informacion_falsa": "false_info",
    }

    # Confirmacion de X (toast/alert): reporte enviado o YA existente
    # (idempotente -> True).
    _FRASES_CONFIRMACION_REPORTE = (
        "thanks for letting us know",
        "gracias por informarnos",
        "gracias por avisarnos",
        "tu reporte fue enviado",
        "report submitted",
        "we received your report",
        "already reported",
        "ya reportaste",
        "ya lo reportaste",
        "has reportado",
        "thanks",
        "gracias",
    )

    # Frases fuertes que se pueden buscar en `page_source` sin falsos positivos
    # por un simple "gracias" de la UI normal.
    _FRASES_FUERTES_CONFIRMACION = (
        "thanks for letting us know",
        "gracias por informarnos",
        "gracias por avisarnos",
        "tu reporte fue enviado",
        "report submitted",
        "we received your report",
        "already reported",
        "ya reportaste",
        "ya lo reportaste",
        "has reportado",
    )

    # Botones de avance/envio del flujo de reporte (EN/ES). "denunciar" es el
    # boton final de la variante espanola de X ("Denunciar" / "Denunciar post").
    _FRASES_ENVIO_REPORTE = (
        "submit", "next", "done", "enviar", "siguiente", "listo", "hecho",
        "denunciar",
    )

    # Rutas de x.com que NO son perfiles (para `reportar_objetivo`).
    _RUTAS_NO_PERFIL = frozenset({
        "home", "explore", "notifications", "messages", "i", "search",
        "settings", "compose", "tos", "privacy", "about", "login", "logout",
        "signup", "intent", "share",
    })

    @staticmethod
    def _normalizar_texto_ui(texto) -> str:
        """Normaliza texto de UI para comparar EN/ES sin depender de acentos.

        Minusculas, comillas tipograficas unificadas, espacios colapsados y
        sin marcas diacriticas. Nunca lanza (texto invalido -> str vacio).
        """
        try:
            t = str(texto or "").strip().lower()
            t = (
                t.replace("\u2019", "'")
                .replace("\u2018", "'")
                .replace("\u201c", '"')
                .replace("\u201d", '"')
                .replace("\u00a0", " ")
                .replace("\u200b", "")
            )
            t = unicodedata.normalize("NFKD", t)
            t = "".join(c for c in t if not unicodedata.combining(c))
            return re.sub(r"\s+", " ", t).strip()
        except Exception:
            return str(texto or "").strip().lower()

    def _opciones_visibles(self, contenedor) -> list:
        """Devuelve `[(elemento, texto)]` de opciones/razones VISIBLES.

        Cubre los `role` que X ha usado para las razones del reporte
        (`menuitem`/`radio`/`option`/`label`) y, como respaldo, botones
        (`role='button'`/`button`). Deduplica por texto normalizado para no
        procesar dos veces el mismo nodo envuelto. Nunca lanza.
        """
        opciones = []
        vistos = set()
        selectores = (
            "[role='menuitem']",
            "[role='radio']",
            "[role='option']",
            "label",
            "[role='button']",
            "button",
        )
        for sel in selectores:
            try:
                elementos = contenedor.find_elements(By.CSS_SELECTOR, sel)
            except Exception:
                continue
            for el in elementos:
                try:
                    if not el.is_displayed():
                        continue
                    texto = (el.text or "").strip()
                except Exception:
                    continue
                clave = self._normalizar_texto_ui(texto)
                if not clave or clave in vistos:
                    continue
                vistos.add(clave)
                opciones.append((el, texto))
        return opciones

    def _textos_opciones(self, opciones, excluir=()) -> str:
        """Junta los textos visibles de `opciones` (para el diagnostico)."""
        excluir_norm = tuple(self._normalizar_texto_ui(x) for x in (excluir or ()))
        textos = []
        for _, texto in opciones or ():
            limpio = (texto or "").strip()
            norm = self._normalizar_texto_ui(limpio)
            if not norm or any(x and x in norm for x in excluir_norm):
                continue
            if limpio not in textos:
                textos.append(limpio)
        return "; ".join(textos)[:400]

    def _elegir_opcion_visible(self, opciones, candidatos):
        """Elige la opcion VISIBLE que coincide con `candidatos` (o `(None, "")`).

        Estrategia por candidato en orden: coincidencia EXACTA, luego
        `startswith`, luego contenido. Asi "It's abusive or harmful" no se
        confunde con "It's spam" y el primer candidato especifico gana.
        """
        normalizadas = []
        for el, texto in opciones or ():
            norm = self._normalizar_texto_ui(texto)
            if norm:
                normalizadas.append((el, norm, (texto or "").strip()))
        for candidato in candidatos or ():
            cand = self._normalizar_texto_ui(candidato)
            if not cand:
                continue
            for el, norm, original in normalizadas:
                if norm == cand:
                    return el, original
            for el, norm, original in normalizadas:
                if norm.startswith(cand):
                    return el, original
            for el, norm, original in normalizadas:
                if cand in norm:
                    return el, original
        return None, ""

    def _elemento_habilitado(self, elemento) -> bool:
        """True si el elemento es visible y no esta deshabilitado (aria)."""
        try:
            if not elemento.is_displayed():
                return False
        except Exception:
            return False
        try:
            if not elemento.is_enabled():
                return False
        except Exception:
            return False
        try:
            if (elemento.get_attribute("aria-disabled") or "").lower() == "true":
                return False
        except Exception:
            pass
        return True

    def _contenedor_dialogo(self, timeout: float = 8.0):
        """Primer modal visible (`[role=dialog]` y respaldos) o None."""
        fin = time.time() + max(0.2, float(timeout))
        while True:
            for sel in (
                "[role='dialog']",
                "[data-testid='modal']",
                "div[aria-modal='true']",
            ):
                try:
                    for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed():
                            return el
                except Exception:
                    continue
            if time.time() >= fin:
                return None
            time.sleep(0.4)

    def _esperar_menu_opciones(self, timeout: float = 8.0):
        """Primer menu visible (`[role=menu]` y respaldos) o None."""
        fin = time.time() + max(0.2, float(timeout))
        while True:
            for sel in ("[role='menu']", "[data-testid='Dropdown']"):
                try:
                    for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed():
                            return el
                except Exception:
                    continue
            if time.time() >= fin:
                return None
            time.sleep(0.4)

    def _buscar_boton_mas_opciones(
        self, contenedor=None, timeout: float = 12.0, solo_contenedor: bool = False
    ):
        """Boton '...' (mas opciones) del tweet/perfil.

        Intenta DENTRO del articulo (`[data-testid='caret']` y aria-labels
        "More"/"Más"/"More options"/"Más opciones") y, si no aparece, a nivel
        documento. Devuelve None si no hay un boton visible en el plazo.

        `solo_contenedor=True`: si `contenedor` es None o no tiene caret, NO se
        busca a nivel documento. Evita abrir menus ajenos (barra lateral
        "About; Get App; Developers", tendencias "This trend is spam...") cuando
        el article del tweet SI existe pero su caret no aparecio.
        """
        css = (
            "[data-testid='caret']",
            "button[data-testid='caret']",
            "div[role='button'][data-testid='caret']",
        )
        xpaths_rel = (
            ".//*[@data-testid='caret']",
            ".//*[@aria-label='More']",
            ".//*[@aria-label='Más']",
            ".//*[@aria-label='Mas']",
            ".//*[@aria-label='More options']",
            ".//*[@aria-label='Más opciones']",
        )
        xpaths_abs = (
            "//*[@data-testid='caret']",
            "//*[@aria-label='More']",
            "//*[@aria-label='Más']",
            "//*[@aria-label='Mas']",
            "//*[@aria-label='More options']",
            "//*[@aria-label='Más opciones']",
        )
        fin = time.time() + max(0.5, float(timeout))
        while True:
            candidatos = []
            if contenedor is not None:
                for sel in css:
                    try:
                        candidatos.extend(contenedor.find_elements(By.CSS_SELECTOR, sel))
                    except Exception:
                        continue
                for xp in xpaths_rel:
                    try:
                        candidatos.extend(contenedor.find_elements(By.XPATH, xp))
                    except Exception:
                        continue
            if not candidatos and not solo_contenedor:
                for sel in css:
                    try:
                        candidatos.extend(self.driver.find_elements(By.CSS_SELECTOR, sel))
                    except Exception:
                        continue
                for xp in xpaths_abs:
                    try:
                        candidatos.extend(self.driver.find_elements(By.XPATH, xp))
                    except Exception:
                        continue
            for el in candidatos:
                if self._elemento_habilitado(el):
                    return el
            if time.time() >= fin:
                return None
            time.sleep(0.5)

    def _buscar_boton_por_texto(self, frases, contenedor=None, timeout: float = 6.0):
        """Boton visible/habilitado cuyo texto coincide con `frases`.

        Busca dentro del modal visible (o `contenedor`/documento) por texto
        exacto o `startswith` de la frase. Devuelve None en el plazo. No elige
        a ciegas: solo botones de avance/envio (`_FRASES_ENVIO_REPORTE`).
        """
        fin = time.time() + max(0.5, float(timeout))
        while True:
            if contenedor is not None:
                opciones = self._opciones_visibles(contenedor)
            else:
                dialogo = self._contenedor_dialogo(timeout=0.3)
                opciones = self._opciones_visibles(dialogo or self.driver)
            for frase in frases:
                objetivo = self._normalizar_texto_ui(frase)
                if not objetivo:
                    continue
                for el, texto in opciones:
                    norm = self._normalizar_texto_ui(texto)
                    if norm == objetivo or norm.startswith(objetivo + " "):
                        if self._elemento_habilitado(el):
                            return el
            if time.time() >= fin:
                return None
            time.sleep(0.4)

    def _candidatos_opcion_reporte(self, es_cuenta: bool, handle: str = "") -> tuple:
        """Textos EN/ES de la opcion de reporte en el menu '...'."""
        h = (handle or "").strip().lstrip("@").lower()
        if es_cuenta:
            candidatos = []
            if h:
                candidatos.extend(
                    (
                        f"report @{h}", f"reportar a @{h}", f"reportar @{h}",
                        f"denunciar a @{h}", f"denunciar @{h}",
                    )
                )
            candidatos.extend(
                (
                    "report account", "report profile", "reportar cuenta",
                    "reportar perfil", "denunciar cuenta", "denunciar perfil",
                    "report @", "reportar a @", "reportar @",
                    "denunciar a @", "denunciar @", "denunciar", "reportar",
                    "report",
                )
            )
            return tuple(candidatos)
        # UI espanola real: "Denunciar post"/"Denunciar publicacion"; la
        # inglesa: "Report post". Se aceptan ambas y las variantes con
        # "Reportar" por si X vuelve a esa cadena.
        return (
            "report post", "report tweet", "denunciar post",
            "denunciar publicacion", "denunciar tweet", "reportar publicacion",
            "reportar post", "reportar tweet", "denunciar", "reportar",
            "report",
        )

    def _candidatos_motivo_reporte(self, motivo: str, es_cuenta: bool) -> tuple:
        """Textos EN/ES de las razones del modal para `motivo`."""
        tabla = (
            self._MOTIVOS_REPORTE_CUENTA if es_cuenta else self._MOTIVOS_REPORTE_POST
        )
        clave = self._normalizar_texto_ui(motivo).replace(" ", "_").replace("-", "_")
        clave = self._ALIAS_MOTIVO_REPORTE.get(clave, clave)
        return tuple(tabla.get(clave) or tabla["spam"])

    def _reporte_confirmado(self) -> bool:
        """True si X confirmo el reporte (o dice que YA estaba reportado).

        Revisa el toast/`[role=alert]` y `page_source` con frases fuertes
        (evita falsos positivos de un "gracias" suelto de la UI). Nunca lanza.
        """
        try:
            toast = self._normalizar_texto_ui(self._texto_toast())
        except Exception:
            toast = ""
        if toast:
            for frase in self._FRASES_CONFIRMACION_REPORTE:
                if self._normalizar_texto_ui(frase) in toast:
                    return True
        try:
            fuente = self._normalizar_texto_ui(self.driver.page_source)
        except Exception:
            fuente = ""
        if fuente:
            for frase in self._FRASES_FUERTES_CONFIRMACION:
                if self._normalizar_texto_ui(frase) in fuente:
                    return True
        try:
            dialogo = self._contenedor_dialogo(timeout=0.2)
            if dialogo is not None:
                texto = self._normalizar_texto_ui(dialogo.text)
                for frase in ("gracias por", "thanks for"):
                    if frase in texto:
                        return True
        except Exception:
            pass
        return False

    def _dialogo_abierto(self) -> bool:
        """True si sigue montado un modal de reporte (post-avance)."""
        try:
            return self._contenedor_dialogo(timeout=0.2) is not None
        except Exception:
            return False

    def _cerrar_flujo_reporte(self) -> bool:
        """Cierra el menu/modal del reporte (boton cerrar o Escape). Nunca lanza."""
        for sel in (
            "[data-testid='app-bar-close']",
            "[aria-label='Close']",
            "[aria-label='Cerrar']",
        ):
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        self.driver.execute_script("arguments[0].click();", el)
                        return True
            except Exception:
                continue
        try:
            body = self.driver.find_element(By.TAG_NAME, "body")
            body.send_keys(Keys.ESCAPE)
            return True
        except Exception:
            pass
        try:
            activo = self.driver.switch_to.active_element
            if activo is not None:
                activo.send_keys(Keys.ESCAPE)
                return True
        except Exception:
            pass
        return False

    def _clic_js(self, elemento) -> bool:
        """Clic tolerante (JS y, si falla, clic nativo). Nunca lanza."""
        try:
            self.driver.execute_script("arguments[0].click();", elemento)
            return True
        except Exception as e:
            logger.debug(
                f"clic JS del reporte fallo ({type(e).__name__}: {e}); "
                "probando clic nativo"
            )
        try:
            elemento.click()
            return True
        except Exception as e:
            logger.warning(
                f"No se pudo clicar el elemento del reporte "
                f"({type(e).__name__}: {e})"
            )
            return False

    @staticmethod
    def _handle_desde_url(url: str) -> str:
        """Handle del perfil en una URL de x.com ('' si no es un perfil)."""
        try:
            m = re.match(
                r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/?#]+)",
                (url or "").strip(),
                re.IGNORECASE,
            )
            if not m:
                return ""
            handle = m.group(1)
            if handle.lower() in TwitterBot._RUTAS_NO_PERFIL:
                return ""
            return handle
        except Exception:
            return ""

    def _esperar_perfil(self, timeout: float = 10.0) -> bool:
        """Espera (sin abortar) a que monte la cabecera del perfil."""
        selectores = (
            "[data-testid='UserName']",
            "[data-testid='primaryColumn']",
            "[data-testid='UserProfileHeader_Items']",
        )
        fin = time.time() + max(0.2, float(timeout))
        while True:
            for sel in selectores:
                try:
                    for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed():
                            return True
                except Exception:
                    continue
            if time.time() >= fin:
                return False
            time.sleep(0.5)

    # Textos de menus que NO son del tweet (barra lateral, tendencias,
    # busqueda, cuenta): si el caret abre uno de estos, es el menu EQUIVOCADO.
    # Solo se usa como diagnostico: la validacion real es que exista una opcion
    # de reporte en el menu ANTES de elegir cualquier cosa.
    _TEXTOS_MENU_AJENO = (
        "about", "get app", "developers", "acerca de", "descargar la app",
        "desarrolladores", "this trend is spam", "esta tendencia es spam",
        "settings and privacy", "configuracion y privacidad", "help center",
        "centro de ayuda", "keyboard shortcuts", "atajos de teclado",
        "log out", "cerrar sesion", "add an existing account",
        "agregar una cuenta existente", "create new account",
        "crear cuenta nueva",
    )

    def _contenedor_perfil(self):
        """Contenedor del perfil (`primaryColumn`/`main`) o None.

        Scope del caret del perfil: evita que `_buscar_boton_mas_opciones`
        caiga en el boton '...' de la barra lateral/tendencias.
        """
        for sel in ("[data-testid='primaryColumn']", "main[role='main']"):
            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    if el.is_displayed():
                        return el
            except Exception:
                continue
        return None

    def _es_menu_ajeno(self, opciones) -> bool:
        """True si el menu visible es de la pagina/tendencias (no del tweet)."""
        for _, texto in opciones or ():
            norm = self._normalizar_texto_ui(texto)
            if not norm:
                continue
            if any(frase in norm for frase in self._TEXTOS_MENU_AJENO):
                return True
        return False

    def _opciones_son_categorias_reporte(self, opciones, es_cuenta: bool) -> bool:
        """True si las opciones VISIBLES parecen categorias de reporte de X.

        Se usa para decidir el respaldo a Spam: solo se aplica si la pantalla
        SI es el modal de categorias (no un modal cualquiera con un "Next").
        """
        tabla = (
            self._MOTIVOS_REPORTE_CUENTA if es_cuenta else self._MOTIVOS_REPORTE_POST
        )
        candidatos = []
        for lista in tabla.values():
            candidatos.extend(lista)
        elegido, _ = self._elegir_opcion_visible(opciones, tuple(candidatos))
        return elegido is not None

    def _ejecutar_reporte(
        self, url: str, es_cuenta: bool, motivo: str, dry_run: bool
    ) -> bool:
        """Flujo comun de reporte (post o cuenta) con verificacion real.

        Hasta 2 intentos completos: navega, espera el article/el perfil
        (15s con recuperacion de interstitial), localiza el caret '...' DENTRO
        del tweet (NUNCA el global si hay article), valida que el menu traiga
        una opcion de reporte y recien entonces abre el modal. Un menu ajeno
        (barra lateral/tendencias) o un article lento se cierra/reintenta; solo
        tras los 2 intentos falla con el mensaje claro.

        Asume que hay driver y que el llamador limpio `self.ultimo_error`.
        Devuelve True solo con confirmacion (toast/alert/"gracias"/ya
        reportado) o, en `dry_run`, cuando llego al boton de envio SIN
        pulsarlo. Nunca lanza hacia afuera (el llamador tambien envuelve).
        """
        etiqueta = "cuenta" if es_cuenta else "post"
        t_inicio = self._ahora()
        try:
            titulo = (self.driver.title or "")[:80]
        except Exception:
            titulo = ""
        logger.info(
            f"perf @{self.usuario}: reportar {etiqueta} {url} inicio "
            f"(title='{titulo}', motivo={motivo}, dry_run={bool(dry_run)})"
        )

        handle = self._handle_desde_url(url) if es_cuenta else ""
        max_intentos = 2
        opciones_menu: list = []
        opcion = None
        texto_opcion = ""
        contenedor = None

        for intento in range(1, max_intentos + 1):
            # 1) Navegacion con timeouts acotados (misma semantica que el resto).
            self._get_acotado(url, timeout_max=20.0)
            time.sleep(0.3)

            estado = self._recuperar_interstitial(url)
            if estado == "anti-bot":
                self.ultimo_error = (
                    "X pidió verificación anti-bot (Cloudflare) al abrir el "
                    f"{etiqueta} ({self._diagnostico_pagina()})"
                )
                logger.warning(self.ultimo_error)
                return False
            if estado == "driver":
                self.ultimo_error = self.ultimo_error or (
                    f"tab crashed/navegador sin navegar al abrir el {etiqueta}"
                )
                logger.warning(self.ultimo_error)
                return False
            if estado == "error":
                logger.warning(
                    f"intento {intento}/{max_intentos}: pagina de error de X "
                    f"({self._diagnostico_pagina()})"
                )
                if intento < max_intentos:
                    continue
                self.ultimo_error = (
                    f"pagina de error de X (interstitial) al abrir el {etiqueta} "
                    f"({self._diagnostico_pagina()})"
                )
                logger.warning(self.ultimo_error)
                return False

            # Sesion caida (muro de login): UN fallback CDP y reintento de la
            # navegacion; si sigue, se falla CLARO (antes se confundia con
            # "no se encontro el tweet a reportar").
            if estado == "login" or self._hay_muro_login():
                if self._sesion_cdp and self._revivir_sesion_cdp():
                    self._get_acotado(url, timeout_max=20.0)
                    time.sleep(0.3)
                if self._hay_muro_login():
                    self.ultimo_error = (
                        "sesión de X expirada o inválida: se pidió login al abrir "
                        f"el {etiqueta}"
                    )
                    logger.warning(self.ultimo_error)
                    return False

            try:
                self._asegurar_pagina_tweet(url)
            except Exception as e:
                self.ultimo_error = str(e)
                logger.warning(self.ultimo_error)
                return False

            if self._detectar_limite_cuenta():
                self.ultimo_error = "cuenta limitada por X"
                logger.warning(self.ultimo_error)
                return False

            # 2) Esperar el objetivo: article del tweet (15s, con recuperacion
            # de interstitial en medio) o cabecera del perfil.
            contenedor = None
            if es_cuenta:
                self._esperar_perfil(timeout=8.0)
                contenedor = self._contenedor_perfil()
            else:
                contenedor = self._esperar_article_tweet(timeout=8)
                if contenedor is None:
                    estado_art = self._recuperar_interstitial(url)
                    if estado_art == "anti-bot":
                        self.ultimo_error = (
                            "X pidió verificación anti-bot (Cloudflare) al abrir "
                            f"el {etiqueta} ({self._diagnostico_pagina()})"
                        )
                        logger.warning(self.ultimo_error)
                        return False
                    if estado_art == "login":
                        self.ultimo_error = (
                            "sesión de X expirada o inválida: se pidió login al "
                            f"abrir el {etiqueta}"
                        )
                        logger.warning(self.ultimo_error)
                        return False
                    if estado_art == "driver":
                        self.ultimo_error = self.ultimo_error or (
                            f"tab crashed/navegador sin navegar al abrir el {etiqueta}"
                        )
                        logger.warning(self.ultimo_error)
                        return False
                    contenedor = self._esperar_article_tweet(timeout=7)
                if contenedor is None:
                    logger.warning(
                        f"intento {intento}/{max_intentos}: article del tweet no "
                        f"aparecio (15s) ({self._diagnostico_pagina()}); "
                        "navegando de nuevo"
                    )
                    if intento < max_intentos:
                        continue
                    self.ultimo_error = (
                        "no se encontro el tweet a reportar "
                        f"({self._diagnostico_pagina()})"
                    )
                    logger.warning(self.ultimo_error)
                    return False

            # 3) Caret '...' DENTRO del article/perfil (sin fallback global si
            # hay contenedor): el fallback al documento abria menus ajenos
            # ("About; Get App; Developers", tendencias "This trend is spam...").
            solo_contenedor = contenedor is not None
            caret = self._buscar_boton_mas_opciones(
                contenedor, timeout=10.0, solo_contenedor=solo_contenedor
            )
            if caret is None:
                logger.warning(
                    f"intento {intento}/{max_intentos}: timeout esperando '...' "
                    f"del {etiqueta}; refrescando y reintentando"
                )
                self._refresh_corto(8.0)
                time.sleep(0.3)
                if es_cuenta:
                    self._esperar_perfil(timeout=8.0)
                    contenedor = self._contenedor_perfil() or contenedor
                else:
                    contenedor = self._esperar_article_tweet(timeout=10) or contenedor
                solo_contenedor = contenedor is not None
                caret = self._buscar_boton_mas_opciones(
                    contenedor, timeout=8.0, solo_contenedor=solo_contenedor
                )
            if caret is None:
                if intento < max_intentos:
                    continue
                self.ultimo_error = (
                    f"no se encontro el boton de mas opciones ('...') del "
                    f"{etiqueta} ({self._diagnostico_pagina()})"
                )
                logger.warning(self.ultimo_error)
                return False
            self._clic_js(caret)
            time.sleep(random.uniform(0.3, 0.6))

            # 4) VALIDAR el menu ANTES de elegir nada: debe traer una opcion de
            # reporte. Si es el menu de la pagina/tendencias, cerrar, refresh y
            # reintentar; jamas elegir opciones de esos menus.
            menu = self._esperar_menu_opciones(timeout=8.0)
            opciones_menu = self._opciones_visibles(menu or self.driver)
            opcion, texto_opcion = self._elegir_opcion_visible(
                opciones_menu, self._candidatos_opcion_reporte(es_cuenta, handle)
            )
            if opcion is None:
                ajeno = self._es_menu_ajeno(opciones_menu)
                logger.warning(
                    f"intento {intento}/{max_intentos}: menu "
                    f"{'ajeno (barra/tendencias)' if ajeno else 'sin opcion de reporte'}"
                    f"; opciones visibles: {self._textos_opciones(opciones_menu)}"
                )
                self._cerrar_flujo_reporte()
                if intento < max_intentos:
                    self._refresh_corto(6.0)
                    time.sleep(0.3)
                    continue
                self.ultimo_error = (
                    f"no se encontro la opcion de reporte del {etiqueta} en el "
                    f"menu (handle='{handle}'); opciones visibles: "
                    f"{self._textos_opciones(opciones_menu)}"
                )
                logger.warning(self.ultimo_error)
                return False

            logger.info(
                f"reporte {etiqueta}: opcion de menu elegida -> '{texto_opcion}'"
            )
            self._clic_js(opcion)
            time.sleep(random.uniform(0.4, 0.8))
            break

        # Idempotencia: "ya reportaste"/"already reported" puede aparecer AQUI.
        if self._reporte_confirmado():
            self.ultimo_error = ""
            logger.info(
                f"perf @{self.usuario}: reportar {etiqueta} {url} -> ya estaba "
                f"reportado en {self._ahora() - t_inicio:.1f}s"
            )
            return True

        # 4) Motivo en el modal (opciones VISIBLES; sin elegir al azar).
        dialogo = self._contenedor_dialogo(timeout=10.0)
        opciones_motivo = self._opciones_visibles(dialogo or self.driver)
        try:
            logger.info(
                f"reporte {etiqueta}: opciones VISIBLES del modal: "
                f"{self._textos_opciones(opciones_motivo)}"
            )
        except Exception:
            pass
        eleccion, texto_motivo = self._elegir_opcion_visible(
            opciones_motivo, self._candidatos_motivo_reporte(motivo, es_cuenta)
        )
        if eleccion is None and self._opciones_son_categorias_reporte(
            opciones_motivo, es_cuenta
        ):
            # FIX B: el motivo pedido no esta entre las opciones VISIBLES pero
            # la pantalla SI es el modal de categorias de X: se usa "Spam" (la
            # categoria mas amplia) como respaldo. Nunca se elige "Siguiente".
            tabla = (
                self._MOTIVOS_REPORTE_CUENTA if es_cuenta
                else self._MOTIVOS_REPORTE_POST
            )
            eleccion, texto_motivo = self._elegir_opcion_visible(
                opciones_motivo, tuple(tabla["spam"])
            )
            if eleccion is not None:
                logger.info(
                    f"reporte {etiqueta}: motivo '{motivo}' no visible; se usa "
                    f"la categoria Spam ('{texto_motivo}') como respaldo"
                )
        if eleccion is None:
            excluir = tuple(self._FRASES_ENVIO_REPORTE) + (
                "close", "cancel", "cerrar", "cancelar",
            )
            self.ultimo_error = (
                f"motivo '{motivo}' no encontrado entre las opciones VISIBLES "
                f"del reporte de {etiqueta}: "
                f"{self._textos_opciones(opciones_motivo, excluir=excluir)}"
            )
            logger.warning(self.ultimo_error)
            self._cerrar_flujo_reporte()
            return False
        logger.info(
            f"reporte {etiqueta}: motivo '{motivo}' -> '{texto_motivo}'"
        )
        self._clic_js(eleccion)
        time.sleep(random.uniform(0.4, 0.8))

        # 5) Avanzar por las pantallas intermedias y verificar el envio real.
        # X usa una segunda pantalla de SUB-opciones (p.ej. tras "Spam" +
        # "Siguiente") antes del envio final. Si el boton de avance no aparece,
        # se elige la sub-opcion VISIBLE que coincida con el motivo pedido
        # (nunca al azar) y se sigue; si ninguna coincide, se falla con las
        # opciones reales.
        clics = 0
        sub_pendiente = 0
        for _ in range(6):
            if self._reporte_confirmado():
                self.ultimo_error = ""
                logger.info(
                    f"perf @{self.usuario}: reportar {etiqueta} {url} -> ok en "
                    f"{self._ahora() - t_inicio:.1f}s (motivo={motivo}, "
                    f"opcion='{texto_motivo}')"
                )
                return True
            boton = self._buscar_boton_por_texto(
                self._FRASES_ENVIO_REPORTE, timeout=6.0
            )
            if boton is None:
                if self._reporte_confirmado():
                    self.ultimo_error = ""
                    logger.info(
                        f"perf @{self.usuario}: reportar {etiqueta} {url} -> ok "
                        f"en {self._ahora() - t_inicio:.1f}s "
                        f"(motivo={motivo}, opcion='{texto_motivo}')"
                    )
                    return True
                if clics and not self._dialogo_abierto():
                    # El modal desaparecio tras enviar (algunas variantes no
                    # muestran toast): el reporte se envio.
                    logger.info(
                        f"El modal de reporte se cerro tras {clics} clic(s); se "
                        f"asume reporte enviado de {url}"
                    )
                    self.ultimo_error = ""
                    return True
                if sub_pendiente < 2:
                    dialogo_actual = self._contenedor_dialogo(timeout=0.3)
                    opciones_sub = self._opciones_visibles(
                        dialogo_actual or self.driver
                    )
                    sub, texto_sub = self._elegir_opcion_visible(
                        opciones_sub,
                        self._candidatos_motivo_reporte(motivo, es_cuenta),
                    )
                    if sub is not None and self._elemento_habilitado(sub):
                        sub_pendiente += 1
                        logger.info(
                            f"reporte {etiqueta}: pantalla intermedia, sub-opcion "
                            f"elegida -> '{texto_sub}'"
                        )
                        self._clic_js(sub)
                        clics += 1
                        time.sleep(random.uniform(0.4, 0.8))
                        continue
                self.ultimo_error = (
                    f"no se encontro el boton de envio del reporte de {etiqueta} "
                    f"({self._diagnostico_pagina()})"
                )
                logger.warning(self.ultimo_error)
                self._cerrar_flujo_reporte()
                return False
            if dry_run:
                # Verificacion: NO se envia nada. Cerrar el modal y reportar
                # que el flujo llego hasta el boton de envio.
                try:
                    texto_boton = (boton.text or "").strip()
                except Exception:
                    texto_boton = ""
                self._cerrar_flujo_reporte()
                self.ultimo_error = ""
                logger.info(
                    f"perf @{self.usuario}: reportar {etiqueta} {url} -> "
                    f"dry-run ok en {self._ahora() - t_inicio:.1f}s "
                    f"(boton '{texto_boton}' sin pulsar; motivo={motivo}, "
                    f"opcion='{texto_motivo}')"
                )
                return True
            self._clic_js(boton)
            clics += 1
            time.sleep(random.uniform(0.6, 1.2))

        if self._reporte_confirmado():
            self.ultimo_error = ""
            logger.info(
                f"perf @{self.usuario}: reportar {etiqueta} {url} -> ok en "
                f"{self._ahora() - t_inicio:.1f}s (motivo={motivo})"
            )
            return True
        if clics and not self._dialogo_abierto():
            # El modal desaparecio tras enviar (algunas variantes no muestran
            # toast): el reporte se envio.
            logger.info(
                f"El modal de reporte se cerro tras {clics} clic(s); se asume "
                f"reporte enviado de {url}"
            )
            self.ultimo_error = ""
            return True
        self.ultimo_error = (
            f"X no confirmo el reporte del {etiqueta} ({self._diagnostico_pagina()})"
        )
        logger.error(self.ultimo_error)
        return False

    def reportar_post(
        self, url_tweet: str, motivo: str = "spam", dry_run: bool = False
    ) -> bool:
        """Reporta un tweet y devuelve True SOLO con confirmacion real de X.

        Flujo: navega al tweet con timeouts acotados, abre el menu '...'
        (`[data-testid='caret']` o aria-label "More"/"Más"), elige la opcion
        de reporte por texto bilingüe, elige el motivo entre las opciones
        VISIBLES del modal (EN/ES) y avanza con Enviar/Siguiente/Listo hasta
        que X confirma (toast/alert "Thanks for letting us know"/"Gracias") o
        dice que YA estaba reportado (idempotente -> True). Si el motivo no
        coincide con ninguna opcion no se elige al azar: queda en
        `self.ultimo_error` con las opciones reales. Nunca lanza.

        `dry_run=True` es SOLO para verificacion (smoke): llega hasta el boton
        de envio y cierra el modal sin pulsarlo; devuelve True si el flujo
        llego hasta ahi.
        """
        self.ultimo_error = ""
        if not (url_tweet or "").strip():
            self.ultimo_error = "falta la URL del tweet a reportar"
            return False
        if not self.driver:
            if not self.login_con_cookies():
                self.ultimo_error = (
                    self.ultimo_error or "no se pudo iniciar sesion"
                )
                return False
        try:
            return self._ejecutar_reporte(
                url_tweet, es_cuenta=False, motivo=motivo, dry_run=dry_run
            )
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"Error reportando post {url_tweet}: {e}")
            return False

    def reportar_cuenta(
        self, url_perfil: str, motivo: str = "spam", dry_run: bool = False
    ) -> bool:
        """Reporta una CUENTA/perfil y devuelve True SOLO con confirmacion.

        Mismo flujo que `reportar_post` pero sobre un perfil (sin `/status/`):
        menu '...' del perfil, opcion "Report @user"/"Reportar a @user"/
        "Reportar cuenta", modal con las razones de CUENTA ("They're being
        abusive or harmful"/"Es abusivo o dañino", "Their account is spam"/
        "Su cuenta es spam", "They're pretending to be someone else"/"Se hacen
        pasar por otra persona"...) y verificacion del envio. Si la URL trae
        `/status/`, se recorta al perfil. Nunca lanza.

        `dry_run=True`: llega hasta el boton de envio y cierra sin pulsarlo.
        """
        self.ultimo_error = ""
        objetivo = (url_perfil or "").strip()
        if not objetivo:
            self.ultimo_error = "falta la URL del perfil a reportar"
            return False
        # Aunque llegue una URL de tweet, aqui se reporta la CUENTA.
        objetivo = re.sub(r"/status/\d+.*$", "", objetivo).rstrip("/")
        if not self.driver:
            if not self.login_con_cookies():
                self.ultimo_error = (
                    self.ultimo_error or "no se pudo iniciar sesion"
                )
                return False
        try:
            return self._ejecutar_reporte(
                objetivo, es_cuenta=True, motivo=motivo, dry_run=dry_run
            )
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.error(f"Error reportando cuenta {url_perfil}: {e}")
            return False

    def reportar_objetivo(
        self, url: str, motivo: str = "spam", dry_run: bool = False
    ) -> bool:
        """Reporta una URL cualquiera: post si tiene `/status/`, si no cuenta.

        Si no se puede decidir (URL rara), intenta reportar como POST y cae a
        CUENTA. Misma semantica de verificacion y `dry_run` que los otros dos.
        """
        self.ultimo_error = ""
        objetivo = (url or "").strip()
        if not objetivo:
            self.ultimo_error = "falta la URL a reportar"
            return False
        if re.search(r"/status/\d+", objetivo):
            return self.reportar_post(objetivo, motivo=motivo, dry_run=dry_run)
        if re.match(
            r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/[^/?#]+/?$",
            objetivo,
            re.IGNORECASE,
        ) and self._handle_desde_url(objetivo):
            return self.reportar_cuenta(objetivo, motivo=motivo, dry_run=dry_run)
        # No decidible: post primero, cuenta despues.
        if self.reportar_post(objetivo, motivo=motivo, dry_run=dry_run):
            return True
        return self.reportar_cuenta(objetivo, motivo=motivo, dry_run=dry_run)
    
    # ------------------------------------------------------------------
    # Perfil: cambio de nombre visible y de @handle (Selenium)
    # ------------------------------------------------------------------
    # NOTA: la UI de configuracion de X (/settings/profile y
    # /settings/username) cambia con frecuencia. Estos metodos usan varios
    # selectores de respaldo (data-testid / name / texto visible) y verifican
    # el resultado recargando la pagina, pero pueden requerir ajustar los
    # selectores si X rediseña esas pantallas.

    def _esperar_input(self, selectores: list, timeout: int = 15):
        """Devuelve el primer input visible y habilitado de `selectores` (o None)."""
        fin = time.time() + timeout
        while time.time() < fin:
            for sel in selectores:
                try:
                    for elem in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if elem.is_displayed() and elem.is_enabled():
                            return elem
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    def _escribir_input(self, elemento, texto: str) -> bool:
        """Limpia el input (CTRL+A + DELETE) y escribe `texto`."""
        try:
            elemento.click()
            modifier = Keys.COMMAND if os.name == "posix" else Keys.CONTROL
            ActionChains(self.driver).key_down(modifier).send_keys("a").key_up(modifier).perform()
            ActionChains(self.driver).send_keys(Keys.DELETE).perform()
            time.sleep(0.2)
            self._pegar_texto(elemento, texto)
            return True
        except Exception as e:
            logger.warning(f"Pegado fallido, usando send_keys: {e}")
            try:
                for char in texto:
                    elemento.send_keys(char)
                    time.sleep(0.01)
                return True
            except Exception as e2:
                logger.error(f"Error escribiendo el input: {e2}")
                return False

    def _clic_guardar(
        self, testids: Optional[list] = None, textos: Optional[tuple] = None
    ) -> bool:
        """Clica Guardar/Save/Confirmar.

        Intenta por data-testid, por cualquier `*_Save_Button` visible, por
        texto en botones/`role=button` y, como ultimo recurso, con el patron
        `_clic_texto_visible` del bot.
        """
        textos = textos or ("guardar", "save", "confirmar", "confirm")

        for testid in (testids or []):
            for sel in (f"[data-testid='{testid}']", f"button[data-testid='{testid}']"):
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                    if btn.is_displayed() and btn.is_enabled():
                        self.driver.execute_script("arguments[0].click();", btn)
                        return True
                except Exception:
                    continue

        try:
            for btn in self.driver.find_elements(By.CSS_SELECTOR, "[data-testid$='_Save_Button']"):
                if btn.is_displayed() and btn.is_enabled():
                    self.driver.execute_script("arguments[0].click();", btn)
                    return True
        except Exception:
            pass

        try:
            candidatos = self.driver.find_elements(
                By.XPATH, "//button | //div[@role='button'] | //a[@role='button']"
            )
        except Exception:
            candidatos = []

        for texto in textos:
            for elem in candidatos:
                try:
                    if not (elem.is_displayed() and elem.is_enabled()):
                        continue
                    txt = (elem.text or "").strip().lower()
                    if txt and len(txt) <= 40 and texto in txt:
                        self.driver.execute_script("arguments[0].click();", elem)
                        return True
                except Exception:
                    continue

        return self._clic_texto_visible(*textos)

    def _confirmar_guardado(
        self,
        senales: list,
        url_recarga: str,
        selectores_input: list,
        valor_esperado: str,
    ) -> bool:
        """Confirma un guardado de perfil por toast o recargando la pagina.

        - Toast: elementos `[data-testid='toast']` / `div[role='alert']`.
        - Fuente: solo frases largas (>=13 chars) para no dar falsos positivos.
        - Verificacion fuerte: recarga `url_recarga` y compara el value del
          input con `valor_esperado` (ignora el '@' inicial).
        """
        fin = time.time() + 10
        while time.time() < fin:
            try:
                toasts = self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-testid='toast'], div[role='alert']"
                )
                for toast in toasts:
                    txt = (toast.text or "").lower()
                    if any(s in txt for s in senales):
                        return True
                src = self.driver.page_source.lower()
                for s in senales:
                    if len(s) >= 13 and s in src:
                        return True
            except Exception:
                pass
            time.sleep(1)

        try:
            self.driver.get(url_recarga)
            time.sleep(3)
            elem = self._esperar_input(selectores_input, timeout=10)
            if elem is not None:
                actual = (elem.get_attribute("value") or "").strip().lstrip("@")
                esperado = (valor_esperado or "").strip().lstrip("@")
                if actual.lower() == esperado.lower():
                    return True
        except Exception as e:
            logger.warning(f"No se pudo verificar recargando {url_recarga}: {e}")
        return False

    def cambiar_nombre(self, nuevo_nombre: str) -> bool:
        """Cambia el nombre visible (display name) de la cuenta.

        Valida no vacio y <=50 caracteres. Si no hay driver, llama a
        `login_con_cookies()` (que intenta `.pkl` y cae a `cookies_json`).

        Selectores: `input[name='displayName']`, `input[autocomplete='name']`,
        primer `input[type='text']` visible. Guardar:
        `[data-testid='Profile_Save_Button']`, `[data-testid$='_Save_Button']`,
        texto "Guardar"/"Save" (`_clic_texto_visible` como fallback).

        Verifica por toast ("Guardado"/"Saved"/"Your profile was updated"/
        "Se actualizo tu perfil") o recargando /settings/profile y comparando
        el valor. Deja 3s visible. Devuelve bool y rellena `self.ultimo_error`.

        La UI de X cambia: si falla, revisar estos selectores con Chrome real.
        """
        self.ultimo_error = ""
        nombre = (nuevo_nombre or "").strip()
        if not nombre:
            self.ultimo_error = "el nombre no puede estar vacio"
            return False
        if len(nombre) > 50:
            self.ultimo_error = "el nombre supera los 50 caracteres permitidos por X"
            return False

        try:
            if not self.driver:
                if not self.login_con_cookies():
                    self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                    return False

            url_perfil = f"{self.base_url}/settings/profile"
            self.driver.get(url_perfil)
            time.sleep(3)

            selectores = [
                "input[name='displayName']",
                "input[autocomplete='name']",
                "input[type='text']",
            ]
            campo = self._esperar_input(selectores, timeout=15)
            if campo is None:
                self.ultimo_error = "no se encontro el campo de nombre en /settings/profile"
                logger.warning(self.ultimo_error)
                return False

            if not self._escribir_input(campo, nombre):
                self.ultimo_error = "no se pudo escribir el nuevo nombre"
                return False
            time.sleep(1)

            if not self._clic_guardar(["Profile_Save_Button"]):
                self.ultimo_error = "no se encontro el boton Guardar en /settings/profile"
                logger.warning(self.ultimo_error)
                return False

            senales = [
                "your profile was updated",
                "profile was updated",
                "se actualizo tu perfil",
                "se actualizó tu perfil",
                "guardado",
                "saved",
            ]
            ok = self._confirmar_guardado(senales, url_perfil, selectores, nombre)
            if not ok:
                self.ultimo_error = "X no confirmo el cambio de nombre"
                logger.warning(self.ultimo_error)

            # 3s de vista a la pantalla para confirmacion visual.
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 3s la pantalla visible para confirmacion visual...")
                time.sleep(3)
            return ok

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error cambiando el nombre de {self.usuario}: {e}")
            return False

    def cambiar_handle(self, nuevo_handle: str, password: str = "") -> bool:
        """Cambia el @usuario (handle) de la cuenta.

        Normaliza quitando el '@' y espacios; valida `^[A-Za-z0-9_]{4,15}$`.
        X exige confirmar la contrasena: si `password` esta vacia devuelve
        False con `self.ultimo_error`.

        Selectores: `input[name='username']`,
        `input[autocomplete='username']`, `input[type='text']`. Guardar:
        `[data-testid='Profile_Save_Button']`,
        `[data-testid='UserName_Save_Button']`,
        `[data-testid$='_Save_Button']`, textos "Guardar"/"Save". Modal de
        contrasena: `input[type='password']`/`input[name='password']` y
        "Confirmar"/"Confirm"/"Next" (se prueba Enter tambien).

        Verifica recargando /settings/username (input value == nuevo_handle)
        o por toast. Deja 3s visible. Devuelve bool y rellena
        `self.ultimo_error` con el error real.

        La UI de X cambia: si falla, revisar estos selectores con Chrome real.
        """
        self.ultimo_error = ""
        handle = (nuevo_handle or "").strip().lstrip("@").strip()
        if not re.match(r"^[A-Za-z0-9_]{4,15}$", handle):
            self.ultimo_error = (
                "handle invalido: usa de 4 a 15 letras, numeros o _ (sin @)"
            )
            return False
        if not (password or "").strip():
            self.ultimo_error = "se requiere contrasena para cambiar el @"
            return False

        try:
            if not self.driver:
                if not self.login_con_cookies():
                    self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                    return False

            url_usuario = f"{self.base_url}/settings/username"
            self.driver.get(url_usuario)
            time.sleep(3)

            selectores = [
                "input[name='username']",
                "input[autocomplete='username']",
                "input[type='text']",
            ]
            campo = self._esperar_input(selectores, timeout=15)
            if campo is None:
                self.ultimo_error = "no se encontro el campo de @usuario en /settings/username"
                logger.warning(self.ultimo_error)
                return False

            if not self._escribir_input(campo, handle):
                self.ultimo_error = "no se pudo escribir el nuevo @"
                return False
            time.sleep(1)

            if not self._clic_guardar(["Profile_Save_Button", "UserName_Save_Button"]):
                self.ultimo_error = "no se encontro el boton Guardar en /settings/username"
                logger.warning(self.ultimo_error)
                return False

            # X suele pedir la contrasena en un modal para confirmar el cambio.
            time.sleep(1)
            modal = self._esperar_input(
                ["input[type='password']", "input[name='password']"], timeout=5
            )
            if modal is not None:
                try:
                    modal.click()
                    modal.send_keys(password)
                    time.sleep(0.5)
                    try:
                        modal.send_keys(Keys.ENTER)
                    except Exception:
                        pass
                    time.sleep(1)
                    if self._esperar_input(
                        ["input[type='password']", "input[name='password']"], timeout=2
                    ) is not None:
                        self._clic_guardar(
                            ["confirmationSheetConfirm"],
                            textos=(
                                "confirmar",
                                "confirm",
                                "guardar",
                                "save",
                                "next",
                                "siguiente",
                            ),
                        )
                except Exception as e:
                    logger.warning(f"No se pudo confirmar la contrasena: {e}")

            senales = [
                "your username was updated",
                "username was updated",
                "se actualizo tu nombre de usuario",
                "se actualizó tu nombre de usuario",
                "tu nombre de usuario se actualizo",
                "guardado",
                "saved",
            ]
            ok = self._confirmar_guardado(senales, url_usuario, selectores, handle)
            if not ok:
                self.ultimo_error = "X no confirmo el cambio de @"
                logger.warning(self.ultimo_error)

            # 3s de vista a la pantalla para confirmacion visual.
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 3s la pantalla visible para confirmacion visual...")
                time.sleep(3)
            return ok

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error cambiando el handle de {self.usuario}: {e}")
            return False

    def cambiar_perfil(
        self,
        nombre: str = None,
        handle: str = None,
        password: str = None,
        foto_perfil_path: Optional[str] = None,
        foto_portada_path: Optional[str] = None,
        verificar_fotos_manuales: bool = False,
    ) -> dict:
        """Cambia el nombre y/o el @ de la cuenta y lo persiste en BD.

        Apartado de fotos (brandeo) para hacerlo facil en el mismo flujo:
          - `foto_perfil_path` / `foto_portada_path`: ruta local -> subida
            automatica via `cambiar_foto_perfil/portada(path)`.
          - `verificar_fotos_manuales=True`: el operador ya subio foto+portada
            a mano (p. ej. con `abrir_para_brandeo_manual`); solo verifica
            (`cambiar_foto_perfil(None)` / `cambiar_foto_portada(None)`) y
            guarda cookies. Si ademas se paso `*_path`, la subida automatica
            tiene prioridad y no se verifica esa foto en modo manual.

        Si no hay driver, llama a `login_con_cookies()`. Si `password` es None
        la lee de la BD (`Cuenta.password`). Actualiza en BD
        `nombre_mostrado`/`handle_actual` de los cambios que tuvieron exito.

        Devuelve {"ok", "nombre", "handle", "foto_perfil", "foto_portada",
        "error"}. "ok" es True si al menos un cambio solicitado tuvo exito
        (o si no habia nada que cambiar). Nunca lanza excepcion. Requiere
        Chrome real; los selectores de X pueden cambiar.
        """
        resultado = {
            "ok": False,
            "nombre": False,
            "handle": False,
            "foto_perfil": False,
            "foto_portada": False,
            "error": "",
        }

        def _agregar_error(msg: str) -> None:
            msg = (msg or "").strip()
            if not msg:
                return
            if resultado["error"]:
                resultado["error"] = f"{resultado['error']} | {msg}"
            else:
                resultado["error"] = msg

        try:
            quiere_nombre = bool((nombre or "").strip())
            quiere_handle = bool((handle or "").strip())
            quiere_foto_perfil = bool((foto_perfil_path or "").strip())
            quiere_foto_portada = bool((foto_portada_path or "").strip())
            quiere_verificar_manual = bool(verificar_fotos_manuales)

            if (
                not quiere_nombre
                and not quiere_handle
                and not quiere_foto_perfil
                and not quiere_foto_portada
                and not quiere_verificar_manual
            ):
                resultado["ok"] = True
                return resultado

            if not self.driver:
                if not self.login_con_cookies():
                    resultado["error"] = self.ultimo_error or "no se pudo iniciar sesion"
                    return resultado

            if quiere_handle and password is None:
                try:
                    from core.database import get_db_session
                    from core.models import Cuenta

                    with get_db_session() as db:
                        reg = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                        password = (reg.password or "") if reg else ""
                except Exception as e:
                    logger.warning(f"No se pudo leer la contrasena de {self.usuario}: {e}")
                    password = ""

            if quiere_nombre:
                resultado["nombre"] = self.cambiar_nombre(nombre)
                if not resultado["nombre"]:
                    _agregar_error(self.ultimo_error or "no se pudo cambiar el nombre")

            if quiere_handle:
                resultado["handle"] = self.cambiar_handle(handle, password or "")
                if not resultado["handle"]:
                    _agregar_error(self.ultimo_error or "no se pudo cambiar el @")

            if quiere_foto_perfil:
                try:
                    resultado["foto_perfil"] = bool(self.cambiar_foto_perfil(foto_perfil_path))
                    if not resultado["foto_perfil"]:
                        _agregar_error(self.ultimo_error or "no se pudo cambiar la foto de perfil")
                except Exception as e:
                    _agregar_error(f"foto perfil: {type(e).__name__}: {e}")

            if quiere_foto_portada:
                try:
                    resultado["foto_portada"] = bool(self.cambiar_foto_portada(foto_portada_path))
                    if not resultado["foto_portada"]:
                        _agregar_error(self.ultimo_error or "no se pudo cambiar la foto de portada")
                except Exception as e:
                    _agregar_error(f"foto portada: {type(e).__name__}: {e}")

            if quiere_verificar_manual:
                if not quiere_foto_perfil:
                    try:
                        resultado["foto_perfil"] = bool(self.cambiar_foto_perfil(None))
                        if not resultado["foto_perfil"]:
                            _agregar_error(
                                self.ultimo_error or "no se verifico la foto de perfil manual"
                            )
                    except Exception as e:
                        _agregar_error(f"foto perfil manual: {type(e).__name__}: {e}")
                if not quiere_foto_portada:
                    try:
                        resultado["foto_portada"] = bool(self.cambiar_foto_portada(None))
                        if not resultado["foto_portada"]:
                            _agregar_error(
                                self.ultimo_error or "no se verifico la foto de portada manual"
                            )
                    except Exception as e:
                        _agregar_error(f"foto portada manual: {type(e).__name__}: {e}")

            resultado["ok"] = bool(
                resultado["nombre"]
                or resultado["handle"]
                or resultado["foto_perfil"]
                or resultado["foto_portada"]
            )

            campos = {}
            if resultado["nombre"]:
                campos["nombre_mostrado"] = (nombre or "").strip()
            if resultado["handle"]:
                campos["handle_actual"] = (handle or "").strip().lstrip("@")

            if campos:
                try:
                    from core.database import get_db_session
                    from core.models import Cuenta

                    with get_db_session() as db:
                        reg = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                        if reg is not None:
                            for campo, valor in campos.items():
                                if hasattr(Cuenta, campo):
                                    setattr(reg, campo, valor)
                except Exception as e:
                    logger.error(f"Error actualizando perfil en BD de {self.usuario}: {e}")
                    if not resultado["error"]:
                        resultado["error"] = f"bd: {str(e)[:150]}"

            return resultado

        except Exception as e:
            resultado["error"] = f"{type(e).__name__}: {e}"
            logger.exception(f"Error en cambiar_perfil de {self.usuario}: {e}")
            return resultado

    # ------------------------------------------------------------------
    # Perfil: foto de perfil (avatar) y foto de portada (banner)
    # ------------------------------------------------------------------
    # NOTA: la UI de /settings/profile de X cambia con frecuencia. Estos
    # metodos usan varios selectores de respaldo (data-testid / aria-label /
    # texto visible en espanol e ingles) y verifican el resultado por toast o
    # recargando la pagina. Si X rediseña esa pantalla, revisar los selectores
    # con Chrome real.

    def cambiar_foto_perfil(self, imagen_path: Optional[str] = None) -> bool:
        """Sube la foto de perfil (avatar) de la cuenta desde un archivo local.

        Dos modos:
          - Automatico: pasa `imagen_path` (.png/.jpg/.jpeg/.webp/.gif) y se
            sube via `input[type='file']` + modal de recorte + Guardar.
          - Manual (brandeo): pasa `imagen_path=None` cuando el operador ya
            subio la foto a mano (p. ej. con `abrir_para_brandeo_manual`); en
            ese caso NO se abre file-chooser, solo se verifica que haya avatar
            (`_src_foto`) y se guardan cookies (.pkl + BD).

        Requiere Chrome real con sesion valida (cookies); sin Chrome o sin
        sesion devuelve False con `self.ultimo_error` claro. Nunca lanza
        excepcion. Selectores sujetos a cambios de X: probar con Chrome real.
        """
        return self._cambiar_foto(imagen_path, "avatar")

    def cambiar_foto_portada(self, imagen_path: Optional[str] = None) -> bool:
        """Sube la foto de portada (banner/encabezado) desde un archivo local.

        Dos modos (igual que `cambiar_foto_perfil`):
          - Automatico: `imagen_path` con el archivo a subir.
          - Manual (brandeo): `imagen_path=None`; asume que el operador ya la
            subio a mano y solo verifica (`profile_banners`) + guarda cookies.

        Requiere Chrome real con sesion valida; sin Chrome devuelve False con
        `self.ultimo_error` claro. Nunca lanza excepcion. Selectores sujetos
        a cambios de X: probar con Chrome real.
        """
        return self._cambiar_foto(imagen_path, "portada")

    def _verificar_foto_manual(self, tipo: str) -> bool:
        """Verifica una foto que el operador ya subio a mano y guarda cookies.

        Asume que NO se pasa archivo: solo confirma que en
        https://x.com/settings/profile ya hay avatar (`profile_images`) o
        portada (`profile_banners`), guarda cookies (.pkl + BD) y devuelve
        True si la imagen esta presente. Requiere Chrome real con sesion
        valida; si no hay driver intenta `login_con_cookies()`. Nunca lanza
        excepcion: cualquier fallo queda en `self.ultimo_error`.
        """
        self.ultimo_error = ""
        etiqueta = "perfil" if tipo == "avatar" else "portada"
        try:
            if not self.driver:
                try:
                    if not self.login_con_cookies():
                        self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                        logger.warning(
                            f"[foto-manual] Sin sesion para {self.usuario}: "
                            "requiere Chrome real con cookies validas"
                        )
                        return False
                except Exception as e:
                    self.ultimo_error = f"{type(e).__name__}: {e}"
                    logger.warning(
                        f"[foto-manual] Requiere Chrome real; no se pudo abrir: {e}"
                    )
                    return False

            try:
                self.driver.get(f"{self.base_url}/settings/profile")
                time.sleep(3)
            except Exception as e:
                logger.warning(f"[foto-manual] No se pudo cargar /settings/profile: {e}")

            try:
                if "login" in (self.driver.current_url or "").lower():
                    self.ultimo_error = "sesion expirada: X pidio login en /settings/profile"
                    return False
            except Exception:
                pass

            src = ""
            try:
                src = self._src_foto(tipo)
            except Exception as e:
                logger.warning(f"[foto-manual] No se pudo leer el preview de {etiqueta}: {e}")

            guardado = False
            try:
                guardado = self.guardar_cookies()
            except Exception as e:
                logger.warning(f"[foto-manual] No se pudo guardar .pkl: {e}")
            try:
                cookies_nav = self.driver.get_cookies()
                if cookies_nav:
                    self._guardar_cookies_json(cookies_nav)
            except Exception as e:
                logger.warning(f"[foto-manual] No se pudo guardar cookies en BD: {e}")

            if src:
                logger.info(
                    f"[foto-manual] Foto de {etiqueta} de {self.usuario} verificada "
                    f"(manual) y cookies guardadas (pkl={guardado})"
                )
                return True

            self.ultimo_error = (
                f"no se detecto foto de {etiqueta} subida manualmente "
                "(requiere Chrome real; sube la imagen en /settings/profile y reintenta)"
            )
            logger.warning(self.ultimo_error)
            return False
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"[foto-manual] Error verificando foto de {etiqueta}: {e}")
            return False

    def _cambiar_foto(self, imagen_path: Optional[str], tipo: str) -> bool:
        """Implementacion compartida de avatar/portada (ver metodos publicos).

        Si `imagen_path` es None/vacio asume brandeo manual previo (el
        operador ya subio la foto a mano) y delega a `_verificar_foto_manual`
        (solo verifica + guarda cookies). Si trae ruta, hace la subida
        automatica con los selectores actuales (requieren Chrome real).
        """
        self.ultimo_error = ""

        # 0) Modo manual: sin archivo -> solo verificar + guardar cookies.
        #     (brandeo con `abrir_para_brandeo_manual`).
        if not (imagen_path or "").strip():
            logger.info(
                f"Sin imagen_path para {tipo}: asumiendo subida manual previa, "
                "solo se verifica y se guardan cookies"
            )
            return self._verificar_foto_manual(tipo)

        # 1) Validacion local ANTES de login/Chrome.
        ruta = os.path.abspath(imagen_path)
        if not os.path.isfile(ruta):
            self.ultimo_error = f"no existe el archivo de imagen: {imagen_path}"
            logger.warning(self.ultimo_error)
            return False
        if not ruta.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            self.ultimo_error = f"formato de imagen no soportado: {imagen_path}"
            return False

        etiquetas_avatar = (
            "añadir foto de perfil", "agregar foto de perfil",
            "cambiar foto de perfil", "editar foto de perfil",
            "add profile photo", "change profile photo", "update profile photo",
            "add avatar", "change avatar", "update avatar", "edit avatar",
            "añadir foto", "agregar foto", "cambiar foto", "editar foto",
            "add photo", "change photo", "edit photo",
        )
        etiquetas_portada = (
            "añadir foto de portada", "agregar foto de portada",
            "cambiar foto de portada", "editar foto de portada",
            "añadir foto de encabezado", "agregar foto de encabezado",
            "añadir encabezado", "agregar encabezado", "cambiar encabezado",
            "editar encabezado",
            "add header photo", "change header photo", "add header",
            "add a header", "add banner", "change banner", "edit banner",
            "change header", "edit header", "update header",
        )

        if tipo == "portada":
            etiquetas, excluir = etiquetas_portada, etiquetas_avatar
        else:
            etiquetas, excluir = etiquetas_avatar, etiquetas_portada

        try:
            # 2) Login si no hay driver.
            if not self.driver:
                if not self.login_con_cookies():
                    self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                    return False

            # 3) Abrir /settings/profile.
            url_perfil = f"{self.base_url}/settings/profile"
            try:
                self.driver.get(url_perfil)
                time.sleep(3)
            except Exception as e:
                logger.warning(f"No se pudo cargar {url_perfil}: {e}")

            if "login" in (self.driver.current_url or "").lower():
                self.ultimo_error = "sesion expirada: X pidio login en /settings/profile"
                return False

            src_antes = self._src_foto(tipo)

            # 4) Control de portada (para el avatar es opcional).
            timeout_control = 8 if tipo == "portada" else 3
            control = self._buscar_control_etiqueta(
                etiquetas, excluir=excluir, timeout=timeout_control
            )
            if tipo == "portada" and control is None:
                self.ultimo_error = (
                    "no se encontro el control de portada "
                    "(Añadir encabezado/Add header) en /settings/profile"
                )
                logger.warning(self.ultimo_error)
                return False

            # 5) Inputs de archivo candidatos.
            inputs = self._inputs_archivo(timeout=10)
            candidato = None
            if tipo == "portada":
                candidato = self._input_cercano_a_preview("profile_banners")
            else:
                candidato = self._input_cercano_a_preview("profile_images")
            if candidato is None:
                candidato = self._input_archivo_por_etiqueta(etiquetas, excluir=excluir)

            candidatos = []
            if candidato is not None:
                candidatos.append(candidato)
            for inp in inputs:
                if inp not in candidatos:
                    candidatos.append(inp)

            # Si no hay inputs, clicar el control (con el dialogo nativo de
            # archivos interceptado para no bloquear Chrome) y esperarlos.
            if not candidatos and control is not None:
                self._clic_evitando_dialogo(control)
                candidatos = list(self._inputs_archivo(timeout=8))

            if not candidatos:
                self.ultimo_error = (
                    "no se encontro el input de archivo para la foto de "
                    + ("perfil" if tipo == "avatar" else "portada")
                )
                logger.warning(self.ultimo_error)
                return False

            # 6) Enviar la imagen: preferido y, si no sale el modal, el siguiente.
            subido = False
            for indice, inp in enumerate(candidatos[:2]):
                if not self._enviar_archivo_input(inp, ruta):
                    continue
                subido = True
                if self._hay_modal_recorte(timeout=7):
                    break
                logger.info(
                    f"No aparecio el modal de recorte con el input #{indice + 1}; "
                    "probando otro"
                )

            if not subido:
                self.ultimo_error = "X no acepto el archivo (send_keys fallo en los inputs)"
                return False

            # 7) Aceptar el recorte (si no hay modal, continuar) y guardar.
            if not self._aceptar_modal_recorte(timeout=10):
                logger.info("Sin modal de recorte; continuando con el guardado del perfil")
            time.sleep(1)
            for _ in range(3):
                if self._clic_guardar(["Profile_Save_Button"]):
                    time.sleep(2)
                    break
                time.sleep(2)

            # 8) Verificar y dejar la pantalla visible.
            ok = self._verificar_foto_subida(tipo, src_antes, timeout=15)
            if not ok:
                self.ultimo_error = (
                    "X no confirmo el cambio de foto de "
                    + ("perfil" if tipo == "avatar" else "portada")
                )
                logger.warning(self.ultimo_error)

            # 3s de vista a la pantalla para confirmacion visual.
            # En headless (Railway) no hay pantalla que mirar: omitir la espera.
            if not settings.headless:
                logger.info("Dejando 3s la pantalla visible para confirmacion visual...")
                time.sleep(3)
            return ok

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error cambiando la foto ({tipo}) de {self.usuario}: {e}")
            return False

    def _inputs_archivo(self, timeout: int = 10) -> list:
        """Espera a que existan `input[type='file']` (aunque esten ocultos)."""
        fin = time.time() + timeout
        while time.time() < fin:
            try:
                inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
                if inputs:
                    return inputs
            except Exception:
                pass
            time.sleep(0.5)
        return []

    def _etiqueta_cercana(self, elemento, etiquetas: tuple) -> bool:
        """True si el propio elemento o sus ancestros (<=6) tienen un
        aria-label o un texto corto que contiene alguna de `etiquetas`."""
        nodos = [elemento]
        try:
            nodos += elemento.find_elements(By.XPATH, "./ancestor::*[position()<=6]")
        except Exception:
            pass
        for nodo in nodos:
            try:
                al = (nodo.get_attribute("aria-label") or "").lower()
            except Exception:
                al = ""
            try:
                txt = (nodo.text or "").strip().lower()
            except Exception:
                txt = ""
            for etq in etiquetas:
                if etq and (etq in al or (txt and len(txt) <= 60 and etq in txt)):
                    return True
        return False

    def _input_archivo_por_etiqueta(self, etiquetas: tuple, excluir: tuple = ()):
        """Input de archivo dentro del contenedor de un control con esas
        etiquetas; descarta los que esten junto a etiquetas de `excluir`."""
        try:
            inputs = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        except Exception:
            return None
        for inp in inputs:
            if excluir and self._etiqueta_cercana(inp, excluir):
                continue
            if self._etiqueta_cercana(inp, etiquetas):
                return inp
        return None

    def _input_cercano_a_preview(self, patron_src: str):
        """Devuelve el input[type=file] mas cercano en el DOM a un img cuyo src
        contiene `patron_src` ('profile_images' avatar, 'profile_banners' portada)."""
        script = """
        var patron = arguments[0];
        var inputs = document.querySelectorAll("input[type='file']");
        var mejor = null, mejorNivel = 99;
        for (var i = 0; i < inputs.length; i++) {
            var node = inputs[i].parentElement;
            var nivel = 0;
            while (node && node !== document.body && nivel < 10) {
                var imgs = node.querySelectorAll("img");
                var encontrado = false;
                for (var j = 0; j < imgs.length; j++) {
                    var src = imgs[j].getAttribute("src") || "";
                    if (src.indexOf(patron) !== -1) { encontrado = true; break; }
                }
                if (encontrado) {
                    if (nivel < mejorNivel) { mejorNivel = nivel; mejor = inputs[i]; }
                    break;
                }
                node = node.parentElement;
                nivel++;
            }
        }
        return mejor;
        """
        try:
            return self.driver.execute_script(script, patron_src)
        except Exception as e:
            logger.debug(f"No se pudo buscar input cercano a {patron_src}: {e}")
            return None

    def _buscar_control_etiqueta(self, etiquetas: tuple, excluir: tuple = (), timeout: int = 6):
        """Busca un control visible por aria-label o texto (botones y
        `role=button`) que contenga alguna de `etiquetas` (minusculas)."""
        fin = time.time() + timeout
        while time.time() < fin:
            candidatos = []
            for etiqueta in etiquetas:
                try:
                    candidatos += self.driver.find_elements(
                        By.XPATH,
                        "//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                        f"'abcdefghijklmnopqrstuvwxyz'), '{etiqueta}')]",
                    )
                except Exception:
                    pass
                try:
                    candidatos += self.driver.find_elements(
                        By.XPATH,
                        "//*[self::button or @role='button'][contains(translate("
                        "normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                        "'abcdefghijklmnopqrstuvwxyz'), "
                        f"'{etiqueta}') and string-length(normalize-space(.)) <= 60]",
                    )
                except Exception:
                    pass
            for elem in candidatos:
                try:
                    if not (elem.is_displayed() and elem.is_enabled()):
                        continue
                    if excluir and self._etiqueta_cercana(elem, excluir):
                        continue
                    return elem
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    def _clic_evitando_dialogo(self, elemento) -> bool:
        """Clica sin abrir el dialogo nativo de archivos (bloquearia Selenium):
        intercepta el file chooser por CDP antes del clic."""
        try:
            self.driver.execute_cdp_cmd(
                "Page.setInterceptFileChooserDialog", {"enabled": True}
            )
        except Exception as e:
            logger.debug(f"No se pudo interceptar el file chooser: {e}")
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", elemento
            )
            time.sleep(0.3)
            self.driver.execute_script("arguments[0].click();", elemento)
            time.sleep(1)
            return True
        except Exception as e:
            logger.warning(f"No se pudo clicar el control de imagen: {e}")
            return False
        finally:
            try:
                self.driver.execute_cdp_cmd(
                    "Page.setInterceptFileChooserDialog", {"enabled": False}
                )
            except Exception:
                pass

    def _enviar_archivo_input(self, input_elem, ruta: str) -> bool:
        """Hace send_keys de la ruta al input (funciona aunque este oculto)."""
        try:
            input_elem.send_keys(ruta)
            return True
        except Exception as e:
            logger.debug(f"send_keys al input de archivo fallo: {e}")
            return False

    def _hay_modal_recorte(self, timeout: int = 6) -> bool:
        """Detecta si aparecio el modal de recorte tras subir una imagen."""
        fin = time.time() + timeout
        while time.time() < fin:
            try:
                if self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "[data-testid='applyButton'], [data-testid='cropApplyButton']",
                ):
                    return True
            except Exception:
                pass
            try:
                dialogos = self.driver.find_elements(
                    By.CSS_SELECTOR, "div[role='dialog'], div[aria-modal='true']"
                )
                for dlg in dialogos:
                    txt = (dlg.text or "").lower()
                    if any(p in txt for p in ("recortar", "crop", "aplicar", "apply", "ajustar")):
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _aceptar_modal_recorte(self, timeout: int = 10) -> bool:
        """Espera y clica "Aplicar"/"Apply"/"Guardar"/"Save"/"Listo"/"Done"
        del modal de recorte. Devuelve False si no habia modal (no es fatal)."""
        textos = ("aplicar", "apply", "guardar", "save", "listo", "done")
        testids = ("applyButton", "cropApplyButton", "mediaApplyButton", "confirmationSheetConfirm")
        fin = time.time() + timeout
        while time.time() < fin:
            for testid in testids:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, f"[data-testid='{testid}']")
                    if btn.is_displayed() and btn.is_enabled():
                        self.driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.5)
                        return True
                except Exception:
                    continue

            try:
                dialogos = self.driver.find_elements(
                    By.CSS_SELECTOR, "div[role='dialog'], div[aria-modal='true']"
                )
            except Exception:
                dialogos = []
            for dlg in dialogos:
                try:
                    botones = dlg.find_elements(By.XPATH, ".//button | .//*[@role='button']")
                except Exception:
                    botones = []
                for btn in botones:
                    try:
                        if not (btn.is_displayed() and btn.is_enabled()):
                            continue
                        txt = (btn.text or "").strip().lower()
                        if txt and len(txt) <= 30 and any(t in txt for t in textos):
                            self.driver.execute_script("arguments[0].click();", btn)
                            time.sleep(1.5)
                            return True
                    except Exception:
                        continue
            time.sleep(0.5)
        return False

    def _src_foto(self, tipo: str) -> str:
        """Devuelve el src de la imagen actual de avatar/portada en la pagina."""
        if tipo == "portada":
            selectores = [
                "img[src*='profile_banners']",
                "img[alt*='encabezado']",
                "img[alt*='portada']",
                "img[alt*='header']",
                "img[alt*='banner']",
            ]
        else:
            selectores = [
                "img[src*='profile_images']",
                "img[alt*='foto de perfil']",
                "img[alt*='profile photo']",
                "img[alt*='avatar']",
            ]
        for sel in selectores:
            try:
                for img in self.driver.find_elements(By.CSS_SELECTOR, sel):
                    src = img.get_attribute("src") or ""
                    if src:
                        return src
            except Exception:
                continue
        return ""

    def _verificar_foto_subida(self, tipo: str, src_antes: str = "", timeout: int = 15) -> bool:
        """Confirma la subida de avatar/portada por toast o recargando y
        comparando el src de la imagen (`profile_images`/`profile_banners`)."""
        senales = (
            "profile was updated", "your profile was updated",
            "se actualizo tu perfil", "se actualizó tu perfil",
            "tu perfil se actualizo", "tu perfil se actualizó",
            "actualizado", "actualizada", "guardado", "saved", "updated",
        )
        inicio = time.time()
        while time.time() - inicio < timeout:
            try:
                toasts = self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-testid='toast'], div[role='alert']"
                )
                for toast in toasts:
                    txt = (toast.text or "").lower()
                    if txt and any(s in txt for s in senales):
                        return True
            except Exception:
                pass
            time.sleep(1)

        # Sin toast: recarga y compara el src (verificacion fuerte).
        try:
            self.driver.get(f"{self.base_url}/settings/profile")
            time.sleep(3)
            src_despues = self._src_foto(tipo)
            if src_despues and src_despues != src_antes:
                logger.info(f"Foto de {tipo} verificada por cambio de src")
                return True
        except Exception as e:
            logger.warning(f"No se pudo verificar la foto de {tipo} recargando: {e}")
        return False

    # ------------------------------------------------------------------
    # Flujo guiado completo de perfil
    # (foto perfil -> portada -> bio -> ubicacion -> nombre -> @handle)
    # ------------------------------------------------------------------
    # Orden pedido: 1) login con cookies, 2) foto de perfil, 3) foto de
    # portada, 4) biografia, 5) ubicacion, 6) nombre mostrado, 7) @handle
    # (More -> Settings and privacy -> Your account -> Account information ->
    # contrasena -> Username -> guardar).
    #
    # Camino principal: perfil propio + modal "Edit profile". Si el modal o
    # algun campo no aparece, cada paso cae a los metodos existentes de
    # /settings/profile (`_cambiar_foto`, `_escribir_bio`,
    # `_escribir_ubicacion`, `cambiar_nombre`, `cambiar_handle`).

    _SEL_NOMBRE_PERFIL = (
        "input[name='displayName']",
        "input[autocomplete='name']",
        "input[data-testid='displayName']",
    )
    _SEL_BIO_PERFIL = (
        "textarea[name='description']",
        "textarea[aria-label*='Bio' i]",
        "textarea[aria-label*='Biografía' i]",
        "textarea[aria-label*='biografia' i]",
        "textarea[placeholder*='Bio' i]",
        "textarea[placeholder*='descripción' i]",
        "textarea[placeholder*='Descripción' i]",
        "textarea[data-testid='bio']",
        "textarea[data-testid='ProfileBio']",
    )
    _SEL_UBICACION_PERFIL = (
        "input[name='location']",
        "input[aria-label*='Location' i]",
        "input[aria-label*='Ubicación' i]",
        "input[aria-label*='ubicacion' i]",
        "input[placeholder*='Location' i]",
        "input[placeholder*='Ubicación' i]",
        "input[data-testid='location']",
    )

    def _datos_cuenta_perfil(self) -> dict:
        """Lee de la BD el @ actual y la contrasena de la cuenta.

        La contrasena se devuelve para usarla internamente; NUNCA se loguea
        ni se incluye en resultados."""
        datos = {"handle_actual": "", "password": ""}
        try:
            from core.database import get_db_session
            from core.models import Cuenta

            with get_db_session() as db:
                reg = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                if reg is not None:
                    datos["handle_actual"] = (
                        getattr(reg, "handle_actual", "") or ""
                    ).strip().lstrip("@")
                    datos["password"] = (getattr(reg, "password", "") or "").strip()
        except Exception as e:
            logger.warning(f"No se pudieron leer los datos de perfil de {self.usuario}: {e}")
        return datos

    def _clic_elemento(self, elemento) -> bool:
        """Clica de forma robusta (scroll + JS click con fallback nativo)."""
        if elemento is None:
            return False
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", elemento
            )
            time.sleep(0.3)
        except Exception:
            pass
        try:
            self.driver.execute_script("arguments[0].click();", elemento)
            return True
        except Exception:
            pass
        try:
            elemento.click()
            return True
        except Exception as e:
            logger.debug(f"No se pudo clicar el elemento: {e}")
            return False

    def _elemento_por_testid_o_texto(
        self, testids: tuple = (), textos: tuple = (), timeout: int = 10,
        exacto_texto: bool = False,
    ):
        """Primer elemento visible por data-testid o por texto (ES/EN).

        Con `exacto_texto=True` el texto debe coincidir completo (util para
        "More"/"Your account", que aparecen como subcadenas en otras frases).
        """
        fin = time.time() + timeout
        while time.time() < fin:
            for testid in testids:
                try:
                    for elem in self.driver.find_elements(
                        By.CSS_SELECTOR, f"[data-testid='{testid}']"
                    ):
                        if elem.is_displayed() and elem.is_enabled():
                            return elem
                except Exception:
                    continue
            for texto in textos:
                try:
                    xpath = (
                        "//*[self::button or @role='button' or self::a or self::span]["
                        "contains(translate(normalize-space(.), "
                        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                        f"'{texto.lower()}') and string-length(normalize-space(.)) <= 80]"
                    )
                    for elem in self.driver.find_elements(By.XPATH, xpath):
                        try:
                            if not (elem.is_displayed() and elem.is_enabled()):
                                continue
                            txt = re.sub(r"\s+", " ", (elem.text or "").strip().lower())
                            if exacto_texto and txt != texto.lower():
                                continue
                            return elem
                        except Exception:
                            continue
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    def _clic_por_testid_o_texto(
        self, testids: tuple = (), textos: tuple = (), timeout: int = 10,
        exacto_texto: bool = False,
    ) -> bool:
        elem = self._elemento_por_testid_o_texto(
            testids, textos, timeout=timeout, exacto_texto=exacto_texto
        )
        if elem is None:
            return False
        return self._clic_elemento(elem)

    def _dialogo_editar_perfil(self, timeout: int = 10):
        """Devuelve el `div[role='dialog']` del modal "Edit profile" (o None)."""
        fin = time.time() + timeout
        while time.time() < fin:
            try:
                dialogos = self.driver.find_elements(
                    By.CSS_SELECTOR, "div[role='dialog'], div[aria-modal='true']"
                )
            except Exception:
                dialogos = []
            for dlg in dialogos:
                try:
                    if not dlg.is_displayed():
                        continue
                except Exception:
                    continue
                for sel in (
                    "textarea[name='description']",
                    "input[name='displayName']",
                    "input[name='location']",
                    "input[type='file']",
                ):
                    try:
                        if dlg.find_elements(By.CSS_SELECTOR, sel):
                            return dlg
                    except Exception:
                        continue
                try:
                    txt = (dlg.text or "").lower()
                    if "edit profile" in txt or "editar perfil" in txt:
                        return dlg
                except Exception:
                    pass
            time.sleep(0.5)
        return None

    def _cerrar_modal_perfil(self) -> bool:
        """Cierra el modal de edicion de perfil (close button o Escape)."""
        if self._dialogo_editar_perfil(timeout=1) is None:
            return True
        for sel in (
            "[data-testid='app-bar-close']",
            "div[role='dialog'] [aria-label='Close']",
            "div[role='dialog'] [aria-label='Cerrar']",
        ):
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    self._clic_elemento(btn)
                    time.sleep(1)
                    if self._dialogo_editar_perfil(timeout=2) is None:
                        return True
            except Exception:
                continue
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(1)
        except Exception:
            pass
        return self._dialogo_editar_perfil(timeout=2) is None

    def _abrir_modal_editar_perfil(self, handle_url: str = "", timeout: int = 15) -> bool:
        """Navega al perfil propio y abre el modal "Edit profile".

        Devuelve True si el modal quedo abierto. Rellena `self.ultimo_error`
        si algo falla (sesion expirada, suspension, sin boton, sin modal).
        """
        self.ultimo_error = ""
        try:
            if self._dialogo_editar_perfil(timeout=1) is not None:
                return True

            perfil = (handle_url or "").strip().lstrip("@") or self.usuario
            self.driver.get(f"{self.base_url}/{perfil}")
            time.sleep(3)

            try:
                url = (self.driver.current_url or "").lower()
            except Exception:
                url = ""
            if "login" in url:
                self.ultimo_error = "sesion expirada: X pidio login al abrir el perfil"
                return False
            if self._detectar_cuenta_propia_suspendida():
                self.cuenta_suspendida = True
                self.ultimo_error = "cuenta suspendida/bloqueada por X"
                return False

            boton = None
            for testid in ("editProfileButton", "EditProfileButton"):
                try:
                    for elem in self.driver.find_elements(
                        By.CSS_SELECTOR, f"[data-testid='{testid}']"
                    ):
                        if elem.is_displayed() and elem.is_enabled():
                            boton = elem
                            break
                except Exception:
                    continue
                if boton is not None:
                    break
            if boton is None:
                boton = self._buscar_control_etiqueta(
                    ("edit profile", "editar perfil"), timeout=8
                )
            if boton is None:
                self.ultimo_error = (
                    "no se encontro el boton Edit profile/Editar perfil en el perfil propio"
                )
                logger.warning(self.ultimo_error)
                return False

            self._clic_elemento(boton)
            if self._dialogo_editar_perfil(timeout=timeout) is None:
                self.ultimo_error = "no se abrio el modal Edit profile"
                logger.warning(self.ultimo_error)
                return False
            return True
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error abriendo el modal Edit profile de {self.usuario}: {e}")
            return False

    def _buscar_en_contenedor(self, contenedor, selectores):
        """Primer elemento visible/habilitado de `selectores` dentro de `contenedor`."""
        for sel in selectores:
            try:
                for elem in contenedor.find_elements(By.CSS_SELECTOR, sel):
                    try:
                        if elem.is_displayed() and elem.is_enabled():
                            return elem
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _inputs_archivo_en_dialogo(self, dialogo) -> list:
        """Todos los `input[type='file']` dentro del modal (hasta los ocultos)."""
        try:
            return list(dialogo.find_elements(By.CSS_SELECTOR, "input[type='file']"))
        except Exception:
            return []

    def _input_archivo_cercano_en_dialogo(self, dialogo, patron_src: str):
        """`input[type=file]` mas cercano en el DOM (dentro del modal) a un img
        cuyo src contiene `patron_src` ('profile_images'/'profile_banners')."""
        script = """
        var dlg = arguments[0], patron = arguments[1];
        var inputs = dlg.querySelectorAll("input[type='file']");
        var mejor = null, mejorNivel = 99;
        for (var i = 0; i < inputs.length; i++) {
            var node = inputs[i].parentElement;
            var nivel = 0;
            while (node && node !== dlg && nivel < 10) {
                var imgs = node.querySelectorAll("img");
                var encontrado = false;
                for (var j = 0; j < imgs.length; j++) {
                    var src = imgs[j].getAttribute("src") || "";
                    if (src.indexOf(patron) !== -1) { encontrado = true; break; }
                }
                if (encontrado) {
                    if (nivel < mejorNivel) { mejorNivel = nivel; mejor = inputs[i]; }
                    break;
                }
                node = node.parentElement;
                nivel++;
            }
        }
        return mejor;
        """
        try:
            return self.driver.execute_script(script, dialogo, patron_src)
        except Exception as e:
            logger.debug(f"No se pudo buscar input cercano a {patron_src} en el modal: {e}")
            return None

    def _candidatos_input_foto(self, dialogo, tipo: str) -> list:
        """Candidatos de `input[type=file]` para avatar/portada en el modal.

        Orden: cercano al preview en el modal -> cercano al preview global ->
        por etiqueta -> cualquier input del modal (para portada se descarta el
        del avatar) -> cualquier input de la pagina."""
        etiquetas_avatar = (
            "añadir foto de perfil", "agregar foto de perfil",
            "cambiar foto de perfil", "editar foto de perfil",
            "add profile photo", "change profile photo", "update profile photo",
            "add avatar", "change avatar", "update avatar", "edit avatar",
            "añadir foto", "agregar foto", "cambiar foto", "editar foto",
            "add photo", "change photo", "edit photo",
        )
        etiquetas_portada = (
            "añadir foto de portada", "agregar foto de portada",
            "cambiar foto de portada", "editar foto de portada",
            "añadir foto de encabezado", "agregar foto de encabezado",
            "añadir encabezado", "agregar encabezado", "cambiar encabezado",
            "editar encabezado",
            "add header photo", "change header photo", "add header",
            "add a header", "add banner", "change banner", "edit banner",
            "change header", "edit header", "update header",
        )
        if tipo == "portada":
            etiquetas, excluir, patron = etiquetas_portada, etiquetas_avatar, "profile_banners"
        else:
            etiquetas, excluir, patron = etiquetas_avatar, etiquetas_portada, "profile_images"

        candidatos = []

        def _agregar(elem):
            if elem is None:
                return
            try:
                if elem not in candidatos:
                    candidatos.append(elem)
            except Exception:
                pass

        try:
            if dialogo is not None:
                _agregar(self._input_archivo_cercano_en_dialogo(dialogo, patron))
            _agregar(self._input_cercano_a_preview(patron))
            _agregar(self._input_archivo_por_etiqueta(etiquetas, excluir=excluir))
        except Exception as e:
            logger.debug(f"No se pudieron calcular candidatos de foto ({tipo}): {e}")

        inputs = []
        try:
            if dialogo is not None:
                inputs = self._inputs_archivo_en_dialogo(dialogo)
        except Exception:
            inputs = []
        if not inputs:
            inputs = self._inputs_archivo(timeout=3)

        avatar_input = None
        if dialogo is not None:
            avatar_input = self._input_archivo_cercano_en_dialogo(dialogo, "profile_images")
        for inp in inputs:
            if tipo == "portada" and avatar_input is not None and inp == avatar_input:
                continue
            _agregar(inp)
        return candidatos

    def _escribir_campo_perfil(self, selectores: tuple, texto: str, dialogo=None) -> tuple:
        """Rellena un campo del perfil: modal "Edit profile" primero y, si el
        campo no aparece, `/settings/profile` con los MISMOS selectores.

        Devuelve `(ok, via)` con `via` = "modal" | "settings" | "".
        """
        self.ultimo_error = ""
        try:
            if dialogo is None:
                dialogo = self._dialogo_editar_perfil(timeout=1)

            campo = None
            via = "settings"
            if dialogo is not None:
                via = "modal"
                campo = self._buscar_en_contenedor(dialogo, selectores)
                if campo is None:
                    campo = self._esperar_input(list(selectores), timeout=4)
            else:
                campo = self._esperar_input(list(selectores), timeout=4)

            if campo is None:
                logger.info(
                    "Campo de perfil no visible; usando /settings/profile "
                    f"({', '.join(selectores[:2])}...)"
                )
                self.driver.get(f"{self.base_url}/settings/profile")
                time.sleep(3)
                campo = self._esperar_input(list(selectores), timeout=12)
                via = "settings"

            if campo is None:
                self.ultimo_error = (
                    "no se encontro el campo de perfil (modal ni /settings/profile)"
                )
                logger.warning(self.ultimo_error)
                return False, ""

            if not self._escribir_input(campo, texto):
                self.ultimo_error = "no se pudo escribir en el campo de perfil"
                return False, via
            return True, via
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.warning(f"Error escribiendo campo de perfil: {e}")
            return False, ""

    def _escribir_bio(self, texto: str, dialogo=None) -> tuple:
        """Escribe la biografia (modal primero, fallback /settings/profile)."""
        return self._escribir_campo_perfil(
            self._SEL_BIO_PERFIL, (texto or "").strip(), dialogo=dialogo
        )

    def _escribir_ubicacion(self, texto: str, dialogo=None) -> tuple:
        """Escribe la ubicacion (modal primero, fallback /settings/profile)."""
        return self._escribir_campo_perfil(
            self._SEL_UBICACION_PERFIL, (texto or "").strip(), dialogo=dialogo
        )

    def _verificar_campo_perfil(self, valor: str, selectores: tuple, timeout: int = 12) -> bool:
        """Verifica un campo recargando /settings/profile y comparando el value."""
        esperado = (valor or "").strip()
        try:
            self.driver.get(f"{self.base_url}/settings/profile")
            time.sleep(3)
        except Exception as e:
            logger.warning(f"No se pudo recargar /settings/profile: {e}")
        fin = time.time() + timeout
        while time.time() < fin:
            campo = self._esperar_input(list(selectores), timeout=2)
            if campo is not None:
                try:
                    actual = campo.get_attribute("value")
                except Exception:
                    actual = None
                if actual is None or actual == "":
                    try:
                        actual = campo.get_attribute("textContent") or campo.text or ""
                    except Exception:
                        actual = ""
                actual = (actual or "").strip()
                if actual == esperado:
                    return True
                if re.sub(r"\s+", " ", actual) == re.sub(r"\s+", " ", esperado):
                    return True
                self.ultimo_error = (
                    f"el campo no quedo con el valor esperado (actual={actual[:60]!r})"
                )
                return False
            time.sleep(0.5)
        self.ultimo_error = self.ultimo_error or "no se pudo leer el campo para verificar"
        return False

    def _guardar_modal_perfil(self, senales: tuple = (), timeout: int = 12) -> bool:
        """Clica Guardar del modal Edit profile y espera toast o cierre del modal."""
        senales = senales or (
            "profile was updated", "your profile was updated",
            "se actualizo tu perfil", "se actualizó tu perfil",
            "tu perfil se actualizo", "tu perfil se actualizó",
            "guardado", "saved",
        )
        if not self._clic_guardar(["Profile_Save_Button"]):
            self.ultimo_error = "no se encontro el boton Guardar del modal de perfil"
            logger.warning(self.ultimo_error)
            return False
        fin = time.time() + timeout
        while time.time() < fin:
            try:
                toasts = self.driver.find_elements(
                    By.CSS_SELECTOR, "[data-testid='toast'], div[role='alert']"
                )
                for toast in toasts:
                    txt = (toast.text or "").lower()
                    if txt and any(s in txt for s in senales):
                        return True
            except Exception:
                pass
            if self._dialogo_editar_perfil(timeout=1) is None:
                return True
            time.sleep(0.5)
        self.ultimo_error = "X no confirmo el guardado del perfil (modal sigue abierto)"
        logger.warning(self.ultimo_error)
        return False

    def _paso_foto(self, tipo: str, ruta: str, handle_url: str) -> tuple:
        """Sube avatar/portada via modal Edit profile; fallback /settings/profile."""
        etiqueta = "perfil" if tipo == "avatar" else "portada"
        try:
            ruta_txt = (ruta or "").strip()
            if not ruta_txt:
                return True, "no solicitado"
            ruta_abs = os.path.abspath(ruta_txt)
            if not os.path.isfile(ruta_abs):
                return False, f"no existe el archivo de imagen: {ruta_txt}"
            if not ruta_abs.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return False, f"formato de imagen no soportado: {ruta_txt}"
            self.ultimo_error = ""

            # Camino principal: perfil propio + modal "Edit profile".
            if self._abrir_modal_editar_perfil(handle_url):
                dialogo = self._dialogo_editar_perfil(timeout=6)
                if dialogo is not None:
                    src_antes = ""
                    try:
                        src_antes = self._src_foto(tipo)
                    except Exception:
                        pass
                    candidatos = self._candidatos_input_foto(dialogo, tipo)
                    subido = False
                    for indice, inp in enumerate(candidatos[:3]):
                        if not self._enviar_archivo_input(inp, ruta_abs):
                            continue
                        subido = True
                        if self._hay_modal_recorte(timeout=7):
                            break
                        logger.info(
                            f"No aparecio el modal de recorte con el input "
                            f"#{indice + 1} para {etiqueta}; probando otro"
                        )
                    if subido:
                        if not self._aceptar_modal_recorte(timeout=12):
                            logger.info(
                                f"Sin modal de recorte para {etiqueta}; continuando"
                            )
                        time.sleep(1)
                        if self._guardar_modal_perfil():
                            if self._verificar_foto_subida(tipo, src_antes, timeout=15):
                                return True, f"modal Edit profile ({etiqueta})"
                    logger.info(
                        f"El modal no completo la foto de {etiqueta}; "
                        "usando fallback /settings/profile"
                    )

            # Fallback: mismos metodos existentes sobre /settings/profile.
            ok = (
                self.cambiar_foto_perfil(ruta_txt)
                if tipo == "avatar"
                else self.cambiar_foto_portada(ruta_txt)
            )
            if ok:
                return True, "fallback URL directa /settings/profile"
            return False, self.ultimo_error or f"no se pudo cambiar la foto de {etiqueta}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _paso_campo_texto(self, tipo: str, texto: str, handle_url: str) -> tuple:
        """Aplica bio/ubicacion con modal primero y fallback /settings/profile."""
        etiqueta = "biografia" if tipo == "bio" else "ubicacion"
        selectores = self._SEL_BIO_PERFIL if tipo == "bio" else self._SEL_UBICACION_PERFIL
        escribidor = self._escribir_bio if tipo == "bio" else self._escribir_ubicacion
        try:
            valor = (texto or "").strip()
            if not valor:
                return True, "no solicitado"
            if tipo == "bio" and len(valor) > 160:
                return False, "la biografia supera los 160 caracteres permitidos por X"
            if tipo == "ubicacion" and len(valor) > 30:
                return False, "la ubicacion supera los 30 caracteres permitidos por X"
            self.ultimo_error = ""

            # Camino principal: modal "Edit profile".
            if self._abrir_modal_editar_perfil(handle_url):
                dialogo = self._dialogo_editar_perfil(timeout=6)
                ok, via = escribidor(valor, dialogo=dialogo)
                if ok and via == "modal":
                    if self._guardar_modal_perfil():
                        if self._verificar_campo_perfil(valor, selectores):
                            return True, f"modal Edit profile ({etiqueta})"
                logger.info(
                    f"El modal no completo la {etiqueta}; usando fallback /settings/profile"
                )

            # Fallback: /settings/profile con los mismos selectores.
            self._cerrar_modal_perfil()
            ok, via = escribidor(valor, dialogo=None)
            if ok:
                if self._clic_guardar(["Profile_Save_Button"], textos=("guardar", "save")):
                    senales = [
                        "your profile was updated", "profile was updated",
                        "se actualizo tu perfil", "se actualizó tu perfil",
                        "tu perfil se actualizo", "tu perfil se actualizó",
                        "guardado", "saved",
                    ]
                    url_settings = f"{self.base_url}/settings/profile"
                    if self._confirmar_guardado(senales, url_settings, list(selectores), valor):
                        return True, "fallback URL directa /settings/profile"
                self.ultimo_error = (
                    self.ultimo_error or f"X no confirmo el cambio de {etiqueta}"
                )
            return False, self.ultimo_error or f"no se pudo cambiar la {etiqueta}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _paso_nombre(self, nombre: str, handle_url: str) -> tuple:
        """Cambia el nombre mostrado: modal primero, fallback `cambiar_nombre`."""
        try:
            valor = (nombre or "").strip()
            if not valor:
                return True, "no solicitado"
            if len(valor) > 50:
                return False, "el nombre supera los 50 caracteres permitidos por X"
            self.ultimo_error = ""

            if self._abrir_modal_editar_perfil(handle_url):
                dialogo = self._dialogo_editar_perfil(timeout=6)
                ok, via = self._escribir_campo_perfil(
                    self._SEL_NOMBRE_PERFIL, valor, dialogo=dialogo
                )
                if ok and via == "modal":
                    if self._guardar_modal_perfil():
                        if self._verificar_campo_perfil(valor, self._SEL_NOMBRE_PERFIL):
                            return True, "modal Edit profile (nombre mostrado)"
                logger.info(
                    "El modal no completo el nombre; usando fallback cambiar_nombre"
                )

            if self.cambiar_nombre(valor):
                return True, "fallback URL directa /settings/profile"
            return False, self.ultimo_error or "no se pudo cambiar el nombre"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _hay_prompt_password(self) -> bool:
        return (
            self._esperar_input(
                ["input[type='password']", "input[name='password']"], timeout=1
            )
            is not None
        )

    def _enviar_prompt_password(self, password: str, timeout: int = 15) -> bool:
        """Escribe y envia la contrasena en el prompt de seguridad de X.

        La contrasena NUNCA se loguea ni se devuelve."""
        self.ultimo_error = ""
        campo = self._esperar_input(
            ["input[type='password']", "input[name='password']"], timeout=8
        )
        if campo is None:
            return True
        try:
            campo.click()
            modifier = Keys.COMMAND if os.name == "posix" else Keys.CONTROL
            ActionChains(self.driver).key_down(modifier).send_keys("a").key_up(modifier).perform()
            ActionChains(self.driver).send_keys(Keys.DELETE).perform()
            time.sleep(0.2)
            campo.send_keys(password)
            time.sleep(0.5)
        except Exception as e:
            self.ultimo_error = f"no se pudo escribir la contrasena ({type(e).__name__})"
            logger.warning(self.ultimo_error)
            return False

        enviado = False
        for testid in ("confirmationSheetConfirm",):
            try:
                for btn in self.driver.find_elements(
                    By.CSS_SELECTOR, f"[data-testid='{testid}']"
                ):
                    if btn.is_displayed() and btn.is_enabled():
                        self._clic_elemento(btn)
                        enviado = True
                        break
            except Exception:
                pass
            if enviado:
                break

        if not enviado:
            try:
                dialogos = self.driver.find_elements(
                    By.CSS_SELECTOR, "div[role='dialog'], div[aria-modal='true']"
                )
            except Exception:
                dialogos = []
            for dlg in dialogos:
                try:
                    botones = dlg.find_elements(By.XPATH, ".//button | .//*[@role='button']")
                except Exception:
                    botones = []
                for btn in botones:
                    try:
                        if not (btn.is_displayed() and btn.is_enabled()):
                            continue
                        txt = (btn.text or "").strip().lower()
                        if txt and len(txt) <= 30 and any(
                            t in txt
                            for t in (
                                "confirmar", "confirm", "next", "siguiente",
                                "continuar", "continue",
                            )
                        ):
                            self._clic_elemento(btn)
                            enviado = True
                            break
                    except Exception:
                        continue
                if enviado:
                    break

        if not enviado:
            try:
                campo.send_keys(Keys.ENTER)
                enviado = True
            except Exception:
                pass
        if not enviado:
            self.ultimo_error = "no se encontro el boton para confirmar la contrasena"
            return False

        fin = time.time() + timeout
        while time.time() < fin:
            if (
                self._esperar_input(
                    ["input[type='password']", "input[name='password']"], timeout=1
                )
                is None
            ):
                return True
            time.sleep(0.5)
        self.ultimo_error = "X no acepto la contrasena (el prompt sigue visible)"
        logger.warning(self.ultimo_error)
        return False

    def _navegar_account_information(self, timeout: int = 15) -> bool:
        """Navega More -> Settings and privacy -> Your account -> Account information.

        Devuelve True si llego (haya o no prompt de contrasena)."""
        self.ultimo_error = ""
        try:
            if not self.driver:
                self.ultimo_error = "no hay driver (requiere login previo)"
                return False
            try:
                url_actual = (self.driver.current_url or "").lower()
            except Exception:
                url_actual = ""
            if "x.com" not in url_actual:
                self.driver.get(f"{self.base_url}/home")
                time.sleep(3)

            # 1) More / Mas
            if not self._clic_por_testid_o_texto(
                ("AppTabBar_More_Menu",),
                ("more", "más", "mas"),
                timeout=10,
                exacto_texto=True,
            ):
                self.ultimo_error = "no se encontro el menu More/Mas (AppTabBar_More_Menu)"
                logger.warning(self.ultimo_error)
                return False
            time.sleep(1.5)

            # 2) Settings and privacy / Configuracion y privacidad
            if not self._clic_por_testid_o_texto(
                ("settingsAndPrivacy",),
                ("settings and privacy", "configuración y privacidad",
                 "configuracion y privacidad"),
                timeout=10,
                exacto_texto=True,
            ):
                self.ultimo_error = (
                    "no se encontro Settings and privacy/Configuracion y privacidad"
                )
                logger.warning(self.ultimo_error)
                return False
            time.sleep(2.5)

            # 3) Your account / Tu cuenta
            if not self._clic_por_testid_o_texto(
                ("yourAccount",), ("your account", "tu cuenta"),
                timeout=10, exacto_texto=True,
            ):
                if self._hay_prompt_password():
                    return True
                self.ultimo_error = "no se encontro Your account/Tu cuenta"
                logger.warning(self.ultimo_error)
                return False
            time.sleep(2)
            if self._hay_prompt_password():
                return True

            # 4) Account information / Informacion de la cuenta
            if not self._clic_por_testid_o_texto(
                ("accountInfo",),
                ("account information", "información de la cuenta",
                 "informacion de la cuenta"),
                timeout=10,
                exacto_texto=True,
            ):
                if self._hay_prompt_password():
                    return True
                self.ultimo_error = (
                    "no se encontro Account information/Informacion de la cuenta"
                )
                logger.warning(self.ultimo_error)
                return False
            time.sleep(2)

            # 5) Esperar prompt de contrasena o la fila Username.
            fin = time.time() + timeout
            while time.time() < fin:
                if self._hay_prompt_password():
                    return True
                if self._elemento_por_testid_o_texto(
                    ("Username",), ("username", "nombre de usuario"), timeout=1
                ) is not None:
                    return True
                time.sleep(0.5)
            self.ultimo_error = "no aparecio el prompt de contrasena ni la opcion Username"
            logger.warning(self.ultimo_error)
            return False
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.warning(f"No se pudo navegar a Account information: {e}")
            return False

    def _cambiar_handle_en_account_info(self, handle: str, password: str) -> bool:
        """Click en Username, escribe el nuevo @ y guarda (con contrasena si
        X la pide). Verifica con `_confirmar_guardado` (toast o
        /settings/username)."""
        self.ultimo_error = ""
        try:
            # La ruta pudo haberse detenido en el prompt de contrasena.
            if self._hay_prompt_password():
                if not self._enviar_prompt_password(password):
                    return False
                time.sleep(2)

            fila = self._elemento_por_testid_o_texto(
                ("Username",), ("username", "nombre de usuario"), timeout=8
            )
            if fila is None:
                # Puede faltar el click en Account information (el prompt de
                # contrasena corto la ruta antes de ese paso).
                if self._clic_por_testid_o_texto(
                    ("accountInfo",),
                    ("account information", "información de la cuenta",
                     "informacion de la cuenta"),
                    timeout=5,
                    exacto_texto=True,
                ):
                    time.sleep(2)
                    if self._hay_prompt_password():
                        if not self._enviar_prompt_password(password):
                            return False
                    fila = self._elemento_por_testid_o_texto(
                        ("Username",), ("username", "nombre de usuario"), timeout=8
                    )
            if fila is None:
                self.ultimo_error = "no se encontro la opcion Username en Account information"
                logger.warning(self.ultimo_error)
                return False
            self._clic_elemento(fila)
            time.sleep(2)

            # Esperar el campo del @ (X puede pedir contrasena otra vez).
            selectores = [
                "input[name='username']",
                "input[autocomplete='username']",
            ]
            campo = None
            fin = time.time() + 20
            while time.time() < fin:
                if self._hay_prompt_password():
                    if not self._enviar_prompt_password(password):
                        return False
                    time.sleep(1)
                    continue
                campo = self._esperar_input(selectores, timeout=2)
                if campo is not None:
                    break
                time.sleep(0.5)
            if campo is None:
                self.ultimo_error = "no se encontro el campo de @usuario tras Account information"
                logger.warning(self.ultimo_error)
                return False

            if not self._escribir_input(campo, handle):
                self.ultimo_error = "no se pudo escribir el nuevo @"
                return False
            time.sleep(1)

            if not self._clic_guardar(
                ["UserName_Save_Button", "Profile_Save_Button"],
                textos=("guardar", "save"),
            ):
                self.ultimo_error = "no se encontro el boton Guardar del @"
                logger.warning(self.ultimo_error)
                return False

            # X puede volver a pedir la contrasena al guardar.
            time.sleep(1)
            if self._hay_prompt_password():
                if not self._enviar_prompt_password(password):
                    return False

            senales = [
                "your username was updated",
                "username was updated",
                "se actualizo tu nombre de usuario",
                "se actualizó tu nombre de usuario",
                "tu nombre de usuario se actualizo",
                "guardado",
                "saved",
            ]
            url_usuario = f"{self.base_url}/settings/username"
            ok = self._confirmar_guardado(senales, url_usuario, selectores, handle)
            if not ok:
                self.ultimo_error = "X no confirmo el cambio de @"
                logger.warning(self.ultimo_error)
            return ok
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error cambiando el @ por Account information: {e}")
            return False

    def _paso_handle(self, handle: str, password_bd: str) -> tuple:
        """Paso 7: More -> Settings -> Account information -> Username -> @.

        Si toda la ruta falla, cae a `cambiar_handle(handle, password)` (URL
        directa) y lo indica en el detalle."""
        nuevo = (handle or "").strip().lstrip("@").strip()
        if not re.match(r"^[A-Za-z0-9_]{4,15}$", nuevo):
            return False, "handle invalido: usa de 4 a 15 letras, numeros o _ (sin @)"
        password = (password_bd or "").strip()
        try:
            self.ultimo_error = ""
            alcanzado = self._navegar_account_information()
            detalle_ruta = ""
            if alcanzado:
                if self._cambiar_handle_en_account_info(nuevo, password):
                    return True, (
                        "ruta More > Settings and privacy > Your account > "
                        "Account information > Username"
                    )
                detalle_ruta = self.ultimo_error or "la ruta de Ajustes no completo el cambio"
            else:
                detalle_ruta = self.ultimo_error or "no se alcanzo Account information"

            logger.info(
                "El cambio de @ por la ruta de Ajustes fallo; probando URL "
                f"directa /settings/username ({detalle_ruta})"
            )
            if self.cambiar_handle(nuevo, password):
                return True, f"fallback URL directa /settings/username ({detalle_ruta})"
            return False, self.ultimo_error or detalle_ruta or "no se pudo cambiar el @"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _chequear_controles_perfil(self, quiere: dict, handle_url: str) -> dict:
        """Dry-run: comprueba los controles SIN subir/escribir/enviar nada.

        Devuelve `{paso: (ok, detalle)}` solo de los pasos solicitados, en el
        orden pedido (foto_perfil, foto_portada, bio, ubicacion, nombre, handle).
        """
        resultados = {}
        modal = None
        avatar_input = None
        banner_input = None
        bio_campo = None
        ubicacion_campo = None
        nombre_campo = None
        modal_ok = False
        detalle_modal = ""

        try:
            modal_ok = self._abrir_modal_editar_perfil(handle_url)
            if modal_ok:
                modal = self._dialogo_editar_perfil(timeout=8)
            if modal is not None:
                file_inputs = self._inputs_archivo_en_dialogo(modal)
                avatar_input = self._input_archivo_cercano_en_dialogo(
                    modal, "profile_images"
                )
                if avatar_input is None and file_inputs:
                    avatar_input = file_inputs[0]
                banner_input = self._input_archivo_cercano_en_dialogo(
                    modal, "profile_banners"
                )
                if banner_input is None and len(file_inputs) > 1:
                    banner_input = file_inputs[1]
                bio_campo = self._buscar_en_contenedor(modal, self._SEL_BIO_PERFIL)
                ubicacion_campo = self._buscar_en_contenedor(
                    modal, self._SEL_UBICACION_PERFIL
                )
                nombre_campo = self._buscar_en_contenedor(modal, self._SEL_NOMBRE_PERFIL)
                detalle_modal = "modal Edit profile alcanzado"
            else:
                detalle_modal = self.ultimo_error or "no se pudo abrir el modal Edit profile"
        except Exception as e:
            detalle_modal = f"{type(e).__name__}: {e}"

        try:
            self._cerrar_modal_perfil()
        except Exception:
            pass

        if quiere.get("foto_perfil"):
            ok = bool(modal_ok and avatar_input is not None)
            resultados["foto_perfil"] = (
                ok,
                "input de archivo de avatar encontrado en el modal"
                if ok
                else f"no se encontro el input de avatar ({detalle_modal})",
            )
        if quiere.get("foto_portada"):
            ok = bool(modal_ok and banner_input is not None)
            resultados["foto_portada"] = (
                ok,
                "input de archivo de portada encontrado en el modal"
                if ok
                else f"no se encontro el input de portada ({detalle_modal})",
            )
        if quiere.get("bio"):
            ok = bool(modal_ok and bio_campo is not None)
            resultados["bio"] = (
                ok,
                "textarea de biografia encontrado en el modal"
                if ok
                else f"no se encontro el textarea de biografia ({detalle_modal})",
            )
        if quiere.get("ubicacion"):
            ok = bool(modal_ok and ubicacion_campo is not None)
            resultados["ubicacion"] = (
                ok,
                "input de ubicacion encontrado en el modal"
                if ok
                else f"no se encontro el input de ubicacion ({detalle_modal})",
            )
        if quiere.get("nombre"):
            ok = bool(modal_ok and nombre_campo is not None)
            resultados["nombre"] = (
                ok,
                "input de nombre (displayName) encontrado en el modal"
                if ok
                else f"no se encontro el input de nombre ({detalle_modal})",
            )
        if quiere.get("handle"):
            try:
                ruta_ok = self._navegar_account_information()
                prompt = self._hay_prompt_password() if ruta_ok else False
                fila_username = False
                if ruta_ok and not prompt:
                    fila_username = (
                        self._elemento_por_testid_o_texto(
                            ("Username",), ("username", "nombre de usuario"), timeout=3
                        )
                        is not None
                    )
                if prompt or fila_username:
                    if prompt:
                        detalle = (
                            "ruta More > Settings > Your account > Account information "
                            "alcanzada; prompt de contrasena visible (NO se envio)"
                        )
                    else:
                        detalle = (
                            "ruta More > Settings > Your account > Account information "
                            "alcanzada; opcion Username visible"
                        )
                    resultados["handle"] = (True, detalle)
                else:
                    resultados["handle"] = (
                        False,
                        self.ultimo_error
                        or "no aparecio el prompt de contrasena ni la opcion Username",
                    )
            except Exception as e:
                resultados["handle"] = (False, f"{type(e).__name__}: {e}")
        return resultados

    def actualizar_perfil_completo(
        self,
        foto_perfil_path: Optional[str] = None,
        foto_portada_path: Optional[str] = None,
        nombre: Optional[str] = None,
        bio: Optional[str] = None,
        ubicacion: Optional[str] = None,
        handle: Optional[str] = None,
        password: str = "",
        dry_run: bool = False,
        callback=None,
    ) -> dict:
        """Flujo guiado completo de perfil, en el orden pedido por el usuario.

        1) Login con cookies (`login_con_cookies`, que cae a cookies_json).
        2) Foto de perfil en el perfil propio + modal "Edit profile".
        3) Foto de portada (banner).
        4) Biografia (max 160).
        5) Ubicacion (max 30).
        6) Nombre mostrado (modal; fallback `cambiar_nombre`).
        7) @handle por la ruta More -> Settings and privacy -> Your account ->
           Account information (contrasena si X la pide) -> Username ->
           guardar; si toda la ruta falla, fallback `cambiar_handle`.

        `dry_run=True`: hace login + navegaciones y comprueba los controles
        (boton Edit profile, inputs de archivo, textarea bio, input ubicacion,
        ruta de Ajustes y prompt de contrasena) SIN subir archivos, SIN
        escribir campos, SIN enviar la contrasena y SIN guardar nada.

        Devuelve el dict con "ok", "login", un bool por paso, "pasos"
        ({"paso","ok","detalle"} en orden) y "error" (primer error legible).
        `callback(actual, total, paso, ok, detalle)` es opcional. Nunca lanza
        excepcion ni expone la contrasena.
        """
        resultado = {
            "ok": False,
            "login": False,
            "foto_perfil": False,
            "foto_portada": False,
            "nombre": False,
            "bio": False,
            "ubicacion": False,
            "handle": False,
            "pasos": [],
            "error": "",
        }
        pasos = resultado["pasos"]

        quiere = {
            "foto_perfil": bool((foto_perfil_path or "").strip()),
            "foto_portada": bool((foto_portada_path or "").strip()),
            "bio": bool((bio or "").strip()),
            "ubicacion": bool((ubicacion or "").strip()),
            "nombre": bool((nombre or "").strip()),
            "handle": bool((handle or "").strip()),
        }
        total_previsto = 1 + sum(1 for v in quiere.values() if v)

        def _reportar(paso: str, ok: bool, detalle: str) -> None:
            detalle = (detalle or "").strip()[:400]
            pasos.append({"paso": paso, "ok": bool(ok), "detalle": detalle})
            if not ok and not resultado["error"]:
                resultado["error"] = f"{paso}: {detalle}" if detalle else paso
            if callback is not None:
                try:
                    callback(len(pasos), total_previsto, paso, bool(ok), detalle)
                except Exception as e:
                    logger.debug(f"callback de progreso fallo: {e}")

        try:
            # 1) Login con cookies (.pkl y, si falla, cookies_json).
            if not self.driver:
                login_ok = bool(self.login_con_cookies())
            else:
                login_ok = True
            resultado["login"] = login_ok
            _reportar(
                "login",
                login_ok,
                "sesion iniciada con cookies"
                if login_ok
                else (self.ultimo_error or "no se pudo iniciar sesion"),
            )
            if not login_ok:
                return resultado

            datos = self._datos_cuenta_perfil()
            handle_url = datos.get("handle_actual") or self.usuario
            password_efectiva = (password or "").strip() or datos.get("password", "")

            if dry_run:
                chequeos = self._chequear_controles_perfil(quiere, handle_url)
                for paso, (ok, detalle) in chequeos.items():
                    _reportar(paso, ok, detalle)
                    if paso in resultado:
                        resultado[paso] = bool(ok)
            else:
                # 2) Foto de perfil (avatar).
                if quiere["foto_perfil"]:
                    ok, detalle = self._paso_foto("avatar", foto_perfil_path, handle_url)
                    resultado["foto_perfil"] = bool(ok)
                    _reportar("foto_perfil", ok, detalle)

                # 3) Foto de portada (banner).
                if quiere["foto_portada"]:
                    ok, detalle = self._paso_foto("portada", foto_portada_path, handle_url)
                    resultado["foto_portada"] = bool(ok)
                    _reportar("foto_portada", ok, detalle)

                # 4) Biografia.
                if quiere["bio"]:
                    ok, detalle = self._paso_campo_texto("bio", bio, handle_url)
                    resultado["bio"] = bool(ok)
                    _reportar("bio", ok, detalle)

                # 5) Ubicacion.
                if quiere["ubicacion"]:
                    ok, detalle = self._paso_campo_texto("ubicacion", ubicacion, handle_url)
                    resultado["ubicacion"] = bool(ok)
                    _reportar("ubicacion", ok, detalle)

                # 6) Nombre mostrado.
                if quiere["nombre"]:
                    ok, detalle = self._paso_nombre(nombre, handle_url)
                    resultado["nombre"] = bool(ok)
                    _reportar("nombre", ok, detalle)

                # 7) @handle.
                if quiere["handle"]:
                    ok, detalle = self._paso_handle(handle, password_efectiva)
                    resultado["handle"] = bool(ok)
                    _reportar("handle", ok, detalle)

            solicitados = [resultado[k] for k, v in quiere.items() if v]
            resultado["ok"] = bool(resultado["login"]) and all(solicitados)
            if resultado["ok"]:
                resultado["error"] = ""

            # Persistencia (solo cambios reales): nombre_mostrado/handle_actual
            # y cookies frescas (.pkl + BD).
            if not dry_run:
                hubo_cambios = any(
                    resultado[k]
                    for k in (
                        "foto_perfil", "foto_portada", "bio",
                        "ubicacion", "nombre", "handle",
                    )
                )
                if hubo_cambios:
                    campos = {}
                    if resultado["nombre"] and (nombre or "").strip():
                        campos["nombre_mostrado"] = nombre.strip()
                    if resultado["handle"] and (handle or "").strip():
                        campos["handle_actual"] = handle.strip().lstrip("@")
                    if campos:
                        try:
                            from core.database import get_db_session
                            from core.models import Cuenta

                            with get_db_session() as db:
                                reg = db.query(Cuenta).filter(
                                    Cuenta.usuario == self.usuario
                                ).first()
                                if reg is not None:
                                    for campo, valor in campos.items():
                                        if hasattr(Cuenta, campo):
                                            setattr(reg, campo, valor)
                        except Exception as e:
                            logger.error(
                                f"Error actualizando perfil en BD de {self.usuario}: {e}"
                            )
                    try:
                        self.guardar_cookies()
                    except Exception as e:
                        logger.warning(f"No se pudo guardar el .pkl tras el perfil: {e}")
                    try:
                        cookies_nav = self.driver.get_cookies()
                        if cookies_nav:
                            self._guardar_cookies_json(cookies_nav)
                    except Exception as e:
                        logger.warning(
                            f"No se pudieron guardar las cookies en BD: {e}"
                        )

            return resultado
        except Exception as e:
            resultado["error"] = f"{type(e).__name__}: {e}"
            logger.exception(
                f"Error en actualizar_perfil_completo de {self.usuario}: {e}"
            )
            return resultado

    def abrir_para_brandeo_manual(self, minutos: int = 5) -> bool:
        """Abre Chrome con la sesion de la cuenta para brandeo manual.

        Flujo para la seccion de "brandear" cuentas: abre el navegador con las
        cookies de `self.usuario`, navega a
        https://x.com/settings/profile y deja la ventana abierta `minutos`
        (default 5) para que el operador suba manualmente la foto de perfil +
        la portada (retrato). Al terminar guarda las cookies actualizadas
        (.pkl con `guardar_cookies()` + BD con `_guardar_cookies_json()`) y
        cierra el navegador.

        Requiere Chrome real con sesion valida (cookies .pkl o cookies_json /
        auth_token). Fuerza modo visible temporalmente (el brandeo manual no
        funciona en headless) y restaura `settings.headless` al salir.

        No rompe si el operador cierra la ventana antes: detecta el cierre
        (`current_url` lanza), lo loguea e intenta guardar lo que haya sin
        lanzar excepcion. Devuelve True si se guardaron cookies, False si no
        (motivo en `self.ultimo_error`). Nunca lanza excepcion.

        Uso:
            from plataformas.twitter.selenium_bot import TwitterBot
            bot = TwitterBot("mi_cuenta")
            bot.abrir_para_brandeo_manual(minutos=5)
            bot.cerrar()  # por seguridad (el metodo ya cierra solo)
        """
        self.ultimo_error = ""
        try:
            minutos_int = int(minutos or 5)
        except Exception:
            minutos_int = 5
        if minutos_int <= 0:
            minutos_int = 5
        segundos_totales = minutos_int * 60

        headless_previo = bool(getattr(settings, "headless", False))
        if headless_previo:
            try:
                settings.headless = False
                logger.info(
                    "[brandeo] HEADLESS desactivado temporalmente: "
                    "el brandeo manual requiere ventana visible"
                )
            except Exception:
                pass

        try:
            if not self.driver:
                try:
                    ok_login = self.login_con_cookies()
                except Exception as e:
                    self.ultimo_error = f"{type(e).__name__}: {e}"
                    logger.warning(
                        f"[brandeo] Requiere Chrome real; no se pudo abrir: {e}"
                    )
                    return False
                if not ok_login:
                    self.ultimo_error = self.ultimo_error or "no se pudo iniciar sesion"
                    logger.warning(
                        f"[brandeo] Sin sesion para {self.usuario}: "
                        "requiere Chrome real con cookies validas"
                    )
                    return False

            url_brandeo = f"{self.base_url}/settings/profile"
            try:
                self.driver.get(url_brandeo)
                time.sleep(3)
            except Exception as e:
                logger.warning(f"[brandeo] No se pudo cargar {url_brandeo}: {e}")

            try:
                if "login" in (self.driver.current_url or "").lower():
                    self.ultimo_error = "sesion expirada: X pidio login en /settings/profile"
                    logger.warning(f"[brandeo] {self.ultimo_error}")
                    return False
            except Exception:
                pass

            try:
                self.driver.set_window_size(1920, 1080)
            except Exception:
                try:
                    self.driver.maximize_window()
                except Exception:
                    pass

            logger.info(
                f"[brandeo] @{self.usuario}: tienes {minutos_int} minutos — "
                "sube la foto de perfil + la portada (retrato) manualmente en "
                "esta ventana. NO cierres la ventana; se guardara sola al terminar."
            )
            logger.info(
                f"[brandeo] Ve a {url_brandeo} si no estas ahi; "
                f"te quedan {minutos_int} minutos para el brandeo manual."
            )

            fin = time.time() + segundos_totales
            ultimo_aviso = -1
            while time.time() < fin:
                try:
                    _ = self.driver.current_url
                except Exception:
                    logger.warning(
                        f"[brandeo] El navegador de {self.usuario} se cerro antes "
                        "de tiempo; se intentara guardar lo que haya sin romper."
                    )
                    break
                restante = int(fin - time.time())
                minuto_restante = restante // 60
                if minuto_restante != ultimo_aviso and restante > 0:
                    ultimo_aviso = minuto_restante
                    if minuto_restante > 0:
                        logger.info(f"[brandeo] @{self.usuario}: quedan ~{minuto_restante} min...")
                time.sleep(5)

            guardado = False
            try:
                guardado = bool(self.guardar_cookies())
            except Exception as e:
                logger.warning(f"[brandeo] No se pudo guardar .pkl: {e}")
                guardado = False
            try:
                cookies_nav = self.driver.get_cookies()
                if cookies_nav:
                    self._guardar_cookies_json(cookies_nav)
                    logger.info(
                        f"[brandeo] Cookies actualizadas de {self.usuario} "
                        f"(.pkl={guardado}, BD={len(cookies_nav)} cookies)"
                    )
                elif not guardado:
                    self.ultimo_error = "no se pudieron guardar las cookies tras el brandeo"
            except Exception as e:
                # El operador pudo cerrar la ventana: no romper.
                logger.warning(f"[brandeo] No se pudo leer/guardar cookies en BD: {e}")
                if not guardado:
                    self.ultimo_error = (
                        "el navegador se cerro antes de guardar las cookies"
                    )

            if guardado:
                logger.info(
                    f"[brandeo] Brandeo manual de @{self.usuario} terminado; "
                    "cookies guardadas. Puedes verificar con "
                    "cambiar_foto_perfil(None) / cambiar_foto_portada(None)."
                )
                return True
            logger.warning(
                f"[brandeo] Brandeo de @{self.usuario} sin guardar cookies: "
                f"{self.ultimo_error or 'requiere Chrome real'}"
            )
            return False
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"[brandeo] Error en brandeo manual de {self.usuario}: {e}")
            return False
        finally:
            try:
                if bool(getattr(settings, "headless", False)) != bool(headless_previo):
                    settings.headless = headless_previo
            except Exception:
                pass
            try:
                self.cerrar()
            except Exception:
                pass
            finally:
                try:
                    self.driver = None
                except Exception:
                    pass

    def _cerrar_fwd_proxy(self):
        """Cierra (tolerante) el proxy local de auth de ESTE navegador."""
        fwd, self._fwd_proxy = self._fwd_proxy, None
        if fwd is not None:
            try:
                fwd.close()
                logger.debug(f"Proxy local cerrado para {self.usuario}")
            except Exception as e:
                logger.debug(
                    f"Error cerrando el proxy local de {self.usuario}: "
                    f"{type(e).__name__}: {e}"
                )

    def _matar_driver(self):
        """Cierra el driver y, si queda huerfano, mata su chromedriver.

        `driver.quit()` a veces lanza o deja el proceso del service vivo
        (especialmente con `tab crashed` o contenedor agotado): aqui se
        intenta `service.stop()` y, si sigue, `terminate()/kill()`. Nunca
        lanza.
        """
        driver = self.driver
        if driver is None:
            return
        try:
            driver.quit()
        except Exception as e:
            logger.error(f"Error cerrando driver: {e}")
            try:
                servicio = getattr(driver, "service", None)
                if servicio is not None and hasattr(servicio, "stop"):
                    servicio.stop()
                proceso = getattr(servicio, "process", None) if servicio else None
                if proceso is not None and proceso.poll() is None:
                    proceso.terminate()
                    try:
                        proceso.wait(timeout=5)
                    except Exception:
                        proceso.kill()
            except Exception as e2:
                logger.debug(
                    f"No se pudo detener el chromedriver de {self.usuario}: "
                    f"{type(e2).__name__}: {e2}"
                )
            return
        # quit() no lanzo: comprobar por si el proceso del service siguio vivo.
        try:
            proceso = getattr(getattr(driver, "service", None), "process", None)
            if proceso is not None and proceso.poll() is None:
                logger.warning(
                    f"chromedriver de {self.usuario} seguia vivo tras quit(); "
                    "matandolo"
                )
                proceso.terminate()
                try:
                    proceso.wait(timeout=3)
                except Exception:
                    proceso.kill()
        except Exception:
            pass

    def cerrar(self):
        """Cierra el navegador Y el proxy local de auth. Nunca lanza.

        Antes solo hacia `driver.quit()`: el LocalForwardProxy (aceptador +
        workers) quedaba vivo para siempre, uno por cada navegador lanzado,
        hasta agotar los hilos del contenedor (`can't start new thread`).
        """
        try:
            self._matar_driver()
        except Exception as e:
            logger.debug(f"Error cerrando driver de {self.usuario}: {e}")
        finally:
            self._cerrar_fwd_proxy()


def abrir_para_brandeo_manual(usuario: str, minutos: int = 5) -> bool:
    """Atajo de modulo para el brandeo manual de una cuenta.

    Abre Chrome con la sesion de `usuario`, navega a
    https://x.com/settings/profile y deja la ventana abierta `minutos`
    (default 5) para que el operador suba foto de perfil + portada
    manualmente; luego guarda cookies (.pkl + BD) y cierra. No rompe si el
    operador cierra antes. Requiere Chrome real.

    Uso:
        from plataformas.twitter.selenium_bot import abrir_para_brandeo_manual
        abrir_para_brandeo_manual("mi_cuenta", minutos=5)
    """
    bot = TwitterBot((usuario or "").strip())
    try:
        return bool(bot.abrir_para_brandeo_manual(minutos=minutos))
    finally:
        try:
            bot.cerrar()
        except Exception:
            pass
