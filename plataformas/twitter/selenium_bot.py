import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException
import pickle
import time
import random
import os
import re
import hashlib
from typing import Optional
from datetime import datetime, timedelta
from loguru import logger

from core.config import settings, resolver_ruta, detectar_chrome_version
from utils.proxies import ProxyManager
from utils.anti_detection import (
    aplicar_stealth,
    aplicar_user_agent,
    normalizar_cookies,
    resolver_ua_cuenta,
)


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
    
    def _obtener_proxy(self) -> str:
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

        intentos = int(os.environ.get("PROXY_X_INTENTOS", "5"))
        for i in range(intentos):
            if pm.x_accesible(proxy):
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
    
    def iniciar_driver(self, pantalla_externa: bool = False) -> bool:
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
            if proxy:
                ProxyManager().aplicar_a_options(options, proxy, tag=self.usuario)
            
            self.driver = uc.Chrome(options=options, version_main=detectar_chrome_version(), use_subprocess=False)
            self.driver.set_page_load_timeout(30)

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
            return False
    
    def login_con_cookies(self) -> bool:
        if not os.path.exists(self.cookies_path):
            logger.warning(f"No hay cookies .pkl para {self.usuario}, intentando cookies_json")
            return self.login_con_cookies_json()
        
        if not self.iniciar_driver():
            return False
        
        try:
            self.driver.get(self.base_url)
            time.sleep(2)
            
            with open(self.cookies_path, "rb") as f:
                cookies = pickle.load(f)
            
            for cookie in cookies:
                try:
                    self.driver.add_cookie(cookie)
                except:
                    continue
            
            self.driver.refresh()
            time.sleep(3)

            if self._detectar_cuenta_propia_suspendida():
                self.cuenta_suspendida = True
                self.ultimo_error = "cuenta suspendida/bloqueada por X"
                logger.warning(f"Cuenta suspendida detectada para {self.usuario}")
                return False

            if "login" in self.driver.current_url.lower():
                logger.warning(
                    f"Sesion .pkl expirada para {self.usuario}; "
                    "probando cookies completas de la BD (cookies_json)"
                )
                # El .pkl puede estar vencido o tener solo auth_token: antes de
                # rendirse usa TODAS las cookies de la BD (auth_token, ct0,
                # twid, ...) reutilizando el driver ya abierto.
                return self.login_con_cookies_json()

            logger.info(f"Login exitoso para {self.usuario}")
            return True
        
        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error en login {self.usuario}: {e}")
            # Falla dura del .pkl: intenta con las cookies completas de la BD.
            return self.login_con_cookies_json()
    
    def login_con_cookies_json(self) -> bool:
        """Inicia sesión inyectando las cookies nativas de X guardadas en BD
        (`Cuenta.cookies_json`) en lugar del archivo .pkl."""
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
            # /404 en vez de home: evita las redirecciones agresivas que X hace
            # desde "/" cuando aun no hay sesion (te manda a /i/flow/login).
            self.driver.get(f"{self.base_url}/404")
            time.sleep(2)

            # Normaliza TODAS las cookies (auth_token, ct0, twid, etc.) desde
            # formatos de vendedor/EditThisCookie/Selenium al que acepta Chrome.
            cookies_selenium = normalizar_cookies(cookies_json)
            if not cookies_selenium:
                logger.warning(f"cookies_json de {self.usuario} sin cookies utilizables")
                self.ultimo_error = "cookies_json sin cookies utilizables"
                return False

            for cookie in cookies_selenium:
                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    logger.debug(f"Cookie {cookie.get('name')} no inyectada: {e}")
                    continue

            self.driver.refresh()
            time.sleep(3)

            # Entrar a /home para que X ejecute su JS autenticado y emita ct0.
            try:
                self.driver.get(f"{self.base_url}/home")
                time.sleep(4)
            except Exception:
                pass

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

            if "login" in self.driver.current_url.lower():
                self.ultimo_error = "sesión expirada (auth_token/cookies inválidos)"
                logger.warning(f"Sesion expirada para {self.usuario} (cookies_json)")
                return False

            # Guarda TODAS las cookies que X generó (incluye ct0) para reutilizar
            # tanto en `cookies_json` (BD) como en el .pkl local: "brandear" la
            # cuenta para no repetir este proceso en la siguiente ejecucion.
            try:
                cookies_navegador = self.driver.get_cookies()
                if cookies_navegador and any(c.get("name") == "ct0" for c in cookies_navegador):
                    self._brandear_cuenta(cookies_navegador)
                    logger.info(
                        f"Cookies (incl. ct0) guardadas para {self.usuario} "
                        f"({len(cookies_navegador)}), cuenta brandeada"
                    )
                else:
                    logger.warning(f"X no emitio ct0 para {self.usuario}; sesion no confirmada")
                    self.ultimo_error = "X no emitió ct0 (auth_token inválido o sesión bloqueada)"
                    return False
            except Exception as e:
                logger.warning(f"No se pudieron guardar las cookies del navegador: {e}")

            logger.info(f"Login exitoso para {self.usuario} via cookies_json")
            # Limpia un error previo (p.ej. del .pkl vencido) ya que la sesion
            # quedo confirmada.
            self.ultimo_error = ""
            return True

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error en login con cookies_json {self.usuario}: {e}")
            return False

    def _hay_challenge_seguridad(self, revisar_url: bool = True) -> bool:
        """True si X esta mostrando un desafio de verificacion de identidad
        (login inusual) en vez de haber cargado la sesion directamente.

        `revisar_url=False` omite la heuristica por URL: dentro del propio
        flujo de `login_con_password` la URL es `/i/flow/login` durante TODO
        el proceso (tambien en los pasos normales de usuario/password), asi
        que ahi solo el texto de la pagina es una senal confiable.
        """
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
        if not self.iniciar_driver():
            return False

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

    def _clic_texto_visible(self, *textos) -> bool:
        """Clica el primer elemento clickeable cuyo texto contiene alguno de 'textos'."""
        for texto in textos:
            try:
                xpath = (
                    "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz'), '" + texto.lower() + "')]"
                )
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

    def _obtener_ultimo_enlace(self, usuario: str) -> Optional[str]:
        try:
            self.driver.get(f"{self.base_url}/{usuario}")
            time.sleep(4)
            
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
    
    def _parsear_fecha_mx(self, fecha_str: str, fin_de_dia: bool = False) -> datetime:
        try:
            fecha = datetime.strptime(fecha_str, "%a %b %d %H:%M:%S %z %Y")
            return fecha.replace(tzinfo=None)
        except:
            return datetime.now()
    
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
        cuenta con un limite recuperable."""
        try:
            url_actual = (self.driver.current_url or "").lower()
            if "/account/access" in url_actual or "/suspended" in url_actual:
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
            return any(frase in page_source for frase in frases)
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

    def publicar_tweet(self, contenido: str, imagen_path: Optional[str] = None) -> Optional[str]:
        """Publica un tweet y devuelve la URL del post recien publicado.

        Devuelve la URL del tweet publicado (str) si se obtuvo, `True` como
        fallback truthy si se publico pero no se pudo extraer la URL, o `None`
        si la publicacion fallo.
        """
        if not self.driver:
            if not self.login_con_cookies():
                return None
        
        if self._detectar_limite_cuenta():
            self.ultimo_error = "cuenta limitada por X"
            logger.error("Cuenta limitada, saltando publicacion")
            return None
        
        try:
            self.driver.get(f"{self.base_url}/compose/post")
            time.sleep(3)
            
            editor = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']"))
            )
            
            contenido = self._reorganizar_hashtags(contenido)
            self._pegar_texto(editor, contenido)
            
            if imagen_path and os.path.exists(imagen_path):
                self._subir_imagen(imagen_path)
            
            time.sleep(1)
            
            # Busqueda forzosa del boton "Post" (por texto y por testid)
            publicar_btn = self._buscar_boton_post()
            self.driver.execute_script("arguments[0].click();", publicar_btn)
            
            # Verificar que el tweet REALMENTE se publico (no basta con hacer clic)
            if not self._verificar_publicacion():
                self.ultimo_error = "X no confirmó la publicación"
                logger.error(f"No se confirmo la publicacion del tweet por {self.usuario}")
                try:
                    self.driver.save_screenshot(resolver_ruta("data/temp/twitter_no_publicado.png"))
                    logger.error("Captura guardada: data/temp/twitter_no_publicado.png")
                except:
                    pass
                return None
            
            logger.info(f"Tweet publicado por {self.usuario}")
            
            # 5 segundos de vista a la pantalla para confirmacion visual
            logger.info("Dejando 5s la pantalla visible para confirmacion visual...")
            time.sleep(5)
            
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

    def _verificar_publicacion(self, tiempo_max: int = 12) -> bool:
        """Confirma que el tweet realmente se publico. Tras publicar, X muestra un
        toast ('Your post was sent' / 'Tu post fue enviado') y saca de /compose/post."""
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
        while time.time() - inicio < tiempo_max:
            try:
                url = self.driver.current_url.lower()
                src = self.driver.page_source.lower()

                if any(s in src for s in senales):
                    return True

                if "compose" not in url and ("home" in url or "status" in url):
                    try:
                        self.driver.find_element(By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']")
                    except Exception:
                        return True

            except Exception:
                pass
            time.sleep(1)
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
                    logger.info(f"Boton Post encontrado con selector: {sel}")
                    return btn
            except Exception:
                continue

        logger.info("Buscando boton Post por texto visible...")
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
                    logger.info(f"Boton Post encontrado por texto con XPath: {xp}")
                    return btn
            except Exception:
                continue

        raise Exception("No se encontro el boton 'Post'/'Publicar' en la pagina")

    def _buscar_opcion_quote(self):
        """Busca la opcion 'Quote' (Citar) del menu desplegable de retweet.
        En X el menu muestra 'Repost' y 'Quote'; el quote usa data-testid='quote'."""
        selectores = [
            "[data-testid='quote']",
            "a[href*='/intent/post']",
        ]
        for sel in selectores:
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, sel)
                if btn.is_displayed():
                    logger.info(f"Opcion Quote encontrada con selector: {sel}")
                    return btn
            except Exception:
                continue

        xpaths = [
            "//span[text()='Quote']",
            "//span[text()='Citar']",
            "//div[@role='menuitem'][.//span[text()='Quote']]",
            "//div[@role='menuitem'][.//span[text()='Citar']]",
            "//a[@role='menuitem'][.//span[text()='Quote']]",
            "//a[@role='menuitem'][.//span[text()='Citar']]",
        ]
        for xp in xpaths:
            try:
                btn = self.driver.find_element(By.XPATH, xp)
                if btn.is_displayed():
                    logger.info(f"Opcion Quote encontrada por texto con XPath: {xp}")
                    return btn
            except Exception:
                continue

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
            self.driver.get(f"{self.base_url}/compose/post")
            time.sleep(3)
            
            for idx, tweet_texto in enumerate(tweets):
                editor = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']"))
                )
                
                texto = self._reorganizar_hashtags(tweet_texto)
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
            
            # 5 segundos de vista a la pantalla para confirmacion visual
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

        Selectores (con fallback): `[data-testid='reply']`, `button[...]`,
        aria-label "Responder"/"Reply" y botones con ese texto visible.
        Devuelve None si no aparece un boton visible/habilitado en `timeout`s.
        """
        selectores = [
            "[data-testid='reply']",
            "button[data-testid='reply']",
        ]
        xpaths = [
            "//*[@data-testid='reply']",
            "//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'responder')]",
            "//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
            "'abcdefghijklmnopqrstuvwxyz'), 'reply')]",
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
        return None

    def _buscar_editor_respuesta(self, timeout: int = 12):
        """Devuelve el textbox de composicion visible de la respuesta.

        Prefiere el editor dentro del modal (`div[role='dialog']`) y luego
        cualquier editor visible. Selectores (con fallback):
        `[data-testid='tweetTextarea_0']`, `div[role='textbox']` y
        `div[contenteditable='true']`. Devuelve None si no aparece.
        """
        selectores = [
            "[data-testid='tweetTextarea_0']",
            "div[role='textbox'][contenteditable='true']",
            "div[contenteditable='true']",
        ]
        fin = time.time() + timeout
        while time.time() < fin:
            # 1) Preferir el editor dentro de un dialogo visible (modal reply).
            try:
                for dlg in self.driver.find_elements(By.CSS_SELECTOR, "div[role='dialog']"):
                    try:
                        if not dlg.is_displayed():
                            continue
                    except Exception:
                        continue
                    for sel in selectores:
                        try:
                            editor = dlg.find_element(By.CSS_SELECTOR, sel)
                            if editor.is_displayed():
                                return editor
                        except Exception:
                            continue
            except Exception:
                pass
            # 2) Cualquier editor visible de la pagina.
            for sel in selectores:
                try:
                    for editor in self.driver.find_elements(By.CSS_SELECTOR, sel):
                        if editor.is_displayed():
                            return editor
                except Exception:
                    continue
            time.sleep(0.5)
        return None

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
            time.sleep(4)
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

        try:
            self.driver.get(url)
            time.sleep(random.uniform(2.5, 4.0))

            if self._detectar_limite_cuenta():
                self.ultimo_error = "cuenta limitada por X"
                logger.error("Cuenta limitada, saltando respuesta")
                return None

            reply_btn = self._buscar_boton_responder()
            if reply_btn is None:
                self.ultimo_error = "no se encontro el boton Responder del tweet"
                logger.warning(self.ultimo_error)
                return None
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});", reply_btn
                )
                time.sleep(0.5)
            except Exception:
                pass
            self.driver.execute_script("arguments[0].click();", reply_btn)
            time.sleep(random.uniform(1.0, 2.0))

            editor = self._buscar_editor_respuesta()
            if editor is None:
                self.ultimo_error = "no se encontro el cuadro de composicion de la respuesta"
                logger.warning(self.ultimo_error)
                return None

            self._pegar_texto(editor, self._reorganizar_hashtags(texto))
            time.sleep(random.uniform(0.8, 1.5))

            if imagen_path and os.path.exists(imagen_path):
                self._subir_imagen(imagen_path)
                time.sleep(1)

            publicar_btn = self._buscar_boton_post()
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
            logger.info("Dejando 3s la pantalla visible para confirmacion visual...")
            time.sleep(3)

            url_respuesta = self._obtener_url_respuesta(url)
            if url_respuesta:
                self.ultima_url_publicada = url_respuesta
            return url_respuesta or True

        except Exception as e:
            self.ultimo_error = f"{type(e).__name__}: {e}"
            logger.exception(f"Error respondiendo tweet para {self.usuario}: {e}")
            try:
                self.driver.save_screenshot(
                    resolver_ruta("data/temp/twitter_error_reply.png")
                )
            except Exception:
                pass
            return None

    def _reorganizar_hashtags(self, texto: str) -> str:
        """Reubica los hashtags sueltos del texto en un unico punto natural
        cercano a la mitad (mismo criterio que
        ``core.perfiles.colocar_hashtag_en_medio``: nunca al final, nunca
        partidos a la mitad de una palabra, sin espacios/saltos sueltos)."""
        hashtags = re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", texto)
        if not hashtags:
            return texto

        base = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", texto)
        base = re.sub(r"[ \t]+", " ", base)
        base = re.sub(r"\n{3,}", "\n\n", base).strip()
        tags = " ".join(hashtags)
        if not base:
            return tags

        mitad = len(base) // 2
        puntos = [m.start() for m in re.finditer(r"(?:\s|\n)", base)]
        if puntos:
            punto = min(puntos, key=lambda p: abs(p - mitad))
            izquierda = base[:punto].rstrip()
            derecha = base[punto:].lstrip()
        else:
            corte = max(1, len(base) // 2)
            izquierda, derecha = base[:corte].rstrip(), base[corte:].lstrip()

        if not izquierda:
            return f"{tags} {derecha}".strip()
        if not derecha:
            return f"{izquierda} {tags}".strip()
        return f"{izquierda} {tags} {derecha}".strip()
    
    def _pegar_texto(self, elemento, texto: str):
        import pyperclip
        
        try:
            pyperclip.copy(texto)
            
            modifier = Keys.COMMAND if os.name == "posix" else Keys.CONTROL
            elemento.click()
            ActionChains(self.driver).key_down(modifier).send_keys("a").key_up(modifier).perform()
            ActionChains(self.driver).key_down(modifier).send_keys("v").key_up(modifier).perform()
        except:
            for char in texto:
                elemento.send_keys(char)
                time.sleep(0.01)
    
    def _subir_imagen(self, imagen_path: str):
        try:
            input_file = self.driver.find_element(By.CSS_SELECTOR, "input[type='file'][accept*='image']")
            input_file.send_keys(os.path.abspath(imagen_path))
            time.sleep(3)
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
        un RT y devuelve `{"exitos": 1, "urls": [<url>]}` (el RT simple no crea
        una URL nueva; se registra la del perfil de la cuenta que retwittea).
        Con varias URLs mantiene el recorrido secuencial en el mismo navegador.

        Verifica cada publicacion REALMENTE (estado `unretweet` en RT simple y
        `_verificar_publicacion()` en citas) antes de contar el exito, por lo
        que `exitos`/`fallidos`/`urls` reflejan el resultado real por URL.
        """
        resultados = {"exitos": 0, "fallidos": 0, "urls": []}
        
        if not self.driver:
            if not self.login_con_cookies():
                return resultados
        
        for url in targets:
            try:
                self.driver.get(url)
                time.sleep(3)
                
                if self._detectar_limite_cuenta():
                    self.ultimo_error = "cuenta limitada por X"
                    break

                try:
                    rt_btn = WebDriverWait(self.driver, 12).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='retweet']"))
                    )
                except TimeoutException:
                    if self._detectar_cuenta_propia_suspendida():
                        self.cuenta_suspendida = True
                        raise Exception("cuenta suspendida/bloqueada por X")
                    motivo = self._detectar_tweet_no_disponible()
                    raise Exception(
                        motivo or "boton de retweet no encontrado (carga lenta o cambio de interfaz)"
                    )
                rt_btn.click()
                time.sleep(1)
                
                if mensaje_cita:
                    # Elegir la opcion "Quote" del menu (NO "Retweet") para citar
                    quote_btn = self._buscar_opcion_quote()
                    self.driver.execute_script("arguments[0].click();", quote_btn)
                    time.sleep(2)
                    
                    editor = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']"))
                    )
                    self._pegar_texto(editor, mensaje_cita)
                    time.sleep(1)
                    
                    if imagen_path and os.path.exists(imagen_path):
                        self._subir_imagen(imagen_path)
                        time.sleep(1)
                    
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
                
                time.sleep(2)
                
                if dar_like:
                    try:
                        like_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='like']")
                        like_btn.click()
                        time.sleep(1)
                    except:
                        pass
                
                resultados["exitos"] += 1
                logger.info(f"RT exitoso: {url}")
                
                if mensaje_cita:
                    url_publicada = self._obtener_ultimo_enlace(self.usuario)
                    if url_publicada:
                        resultados["urls"].append(url_publicada)
                        self.ultima_url_publicada = url_publicada
                else:
                    # El RT simple no genera un post propio: se enlaza el
                    # perfil de la cuenta que retwittea, no el tweet original.
                    url_perfil = f"https://twitter.com/{usuario}"
                    resultados["urls"].append(url_perfil)
                    self.ultima_url_publicada = url_perfil
                
                time.sleep(random.uniform(2.5, 6.0))
            
            except Exception as e:
                resultados["fallidos"] += 1
                self.ultimo_error = f"{type(e).__name__}: {e}"
                logger.error(f"Error en RT: {e}")
        
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
    
    def like(self, url: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url)
            time.sleep(3)
            
            like_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='like']")
            like_btn.click()
            
            time.sleep(2)
            logger.info(f"Like dado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en like: {e}")
            return False
    
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
    
    def _extraer_links_de_chat(self, mi_usuario: str) -> list[str]:
        links = []
        
        try:
            mensajes = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='messageText']")
            
            for mensaje in mensajes:
                texto = mensaje.text
                url_match = re.findall(r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+', texto)
                
                for url in url_match:
                    if mi_usuario.lower() not in url.lower():
                        links.append(url)
        
        except Exception as e:
            logger.error(f"Error extrayendo links: {e}")
        
        return links
    
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
    
    def reportar_post(self, url_tweet: str, motivo: str = "spam") -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_tweet)
            time.sleep(3)
            
            mas_opciones = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='caret']")
            mas_opciones.click()
            time.sleep(1)
            
            reportar_btn = self.driver.find_element(By.XPATH, "//span[text()='Reportar']")
            reportar_btn.click()
            time.sleep(2)
            
            motivos = {
                "spam": "Spam",
                "hate": "Odio o acoso",
                "abuse": "Abuso",
                "impersonation": "Suplantacion de identidad",
                "violence": "Violencia",
                "self_harm": "Autolesion",
                "sensitive": "Contenido sensible"
            }
            
            motivo_texto = motivos.get(motivo, "Spam")
            
            opcion = self.driver.find_element(By.XPATH, f"//span[contains(text(),'{motivo_texto}')]")
            opcion.click()
            time.sleep(1)
            
            siguiente_btn = self.driver.find_element(By.XPATH, "//span[text()='Siguiente']")
            siguiente_btn.click()
            time.sleep(1)
            
            listo_btn = self.driver.find_element(By.XPATH, "//span[text()='Listo']")
            listo_btn.click()
            
            time.sleep(2)
            logger.info(f"Post reportado: {url_tweet}")
            return True
        
        except Exception as e:
            logger.error(f"Error reportando post: {e}")
            return False
    
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

    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Error cerrando driver: {e}")


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
