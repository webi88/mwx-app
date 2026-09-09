import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
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


class TwitterBot:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.driver = None
        self.base_url = "https://x.com"
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.ua_config_path = resolver_ruta("data/perfiles_chrome/ua_config.txt")
        self._ua_persistente = None
        self.ultima_url_publicada = ""
    
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
    
    def _obtener_ua_consistente(self) -> str:
        if self._ua_persistente:
            return self._ua_persistente
        
        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        
        if os.path.exists(self.ua_config_path):
            try:
                with open(self.ua_config_path, "r") as f:
                    ua = f.read().strip()
                    if ua:
                        self._ua_persistente = ua
                        return ua
            except:
                pass
        
        ua = random.choice(uas)
        os.makedirs(os.path.dirname(self.ua_config_path), exist_ok=True)
        with open(self.ua_config_path, "w") as f:
            f.write(ua)
        
        self._ua_persistente = ua
        return ua
    
    def iniciar_driver(self, pantalla_externa: bool = False) -> bool:
        try:
            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(f"--user-data-dir={settings.profiles_dir / self.usuario}")
            options.add_argument(f"--user-agent={self._obtener_ua_consistente()}")
            
            if settings.headless:
                options.add_argument("--headless=new")
            
            if pantalla_externa:
                options.add_argument("--window-size=1920,1080")
            else:
                options.add_argument("--window-size=1366,768")
            
            proxy = self._obtener_proxy()
            if proxy:
                ProxyManager().aplicar_a_options(options, proxy, tag=self.usuario)
            
            stealth_script = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-MX', 'es', 'en-US', 'en']});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
            Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
            Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
            const _getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return _getParameter.call(this, parameter);
            };
            """
            
            self.driver = uc.Chrome(options=options, version_main=detectar_chrome_version(), use_subprocess=False)
            self.driver.set_page_load_timeout(30)
            
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": stealth_script
            })
            
            logger.info(f"Driver iniciado para {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error iniciando driver: {e}")
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
            
            if "login" in self.driver.current_url.lower():
                logger.warning(f"Sesion expirada para {self.usuario}")
                return False
            
            logger.info(f"Login exitoso para {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en login: {e}")
            return False
    
    def login_con_cookies_json(self) -> bool:
        """Inicia sesión inyectando las cookies nativas de X guardadas en BD
        (`Cuenta.cookies_json`) en lugar del archivo .pkl."""
        import json

        cookies_json = None
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                cookies_json = cuenta.cookies_json if cuenta else None
        except Exception as e:
            logger.error(f"Error leyendo cookies_json de {self.usuario}: {e}")

        if not cookies_json:
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
            self.driver.get(self.base_url)
            time.sleep(2)

            for cookie in cookies_json:
                if not isinstance(cookie, dict):
                    continue

                cookie_selenium = {}
                for key in ("name", "value", "domain", "path", "secure", "httpOnly", "expiry"):
                    if key in cookie:
                        cookie_selenium[key] = cookie[key]

                if "name" not in cookie_selenium or "value" not in cookie_selenium:
                    continue

                try:
                    self.driver.add_cookie(cookie_selenium)
                except Exception as e:
                    logger.debug(f"Cookie {cookie_selenium.get('name')} no inyectada: {e}")
                    continue

            self.driver.refresh()
            time.sleep(3)

            if "login" in self.driver.current_url.lower():
                logger.warning(f"Sesion expirada para {self.usuario} (cookies_json)")
                return False

            logger.info(f"Login exitoso para {self.usuario} via cookies_json")
            return True

        except Exception as e:
            logger.error(f"Error en login con cookies_json: {e}")
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
    
    def login_con_password(self, password: str, timeout: int = 60) -> bool:
        """Login automatizado con usuario + contraseña y guarda las cookies.

        Abre x.com/i/flow/login, escribe el usuario, avanza, escribe la
        contraseña y confirma. Si termina en home (o sin 'login' en la URL),
        guarda las cookies y devuelve True. Devuelve False si algo falla o si
        tarda más de 'timeout' segundos.
        """
        if not self.iniciar_driver():
            return False

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

            # 3) Esperar a entrar (home o fuera de login).
            fin = time.time() + timeout
            while time.time() < fin:
                try:
                    url = self.driver.current_url.lower()
                except Exception:
                    url = ""
                if "login" not in url or "home" in url or f"/{self.usuario.lower()}" in url:
                    time.sleep(2)
                    if "login" not in url:
                        self.guardar_cookies()
                        logger.info(f"Login con password exitoso para {self.usuario}")
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
    
    def _obtener_ultimo_enlace(self, usuario: str) -> Optional[str]:
        try:
            self.driver.get(f"{self.base_url}/{usuario}")
            time.sleep(4)
            
            tweets = self.driver.find_elements(By.CSS_SELECTOR, "article[data-testid='tweet']")
            
            if tweets:
                enlace = tweets[0].find_element(By.CSS_SELECTOR, "a[href*='/status/']")
                return enlace.get_attribute("href")
            
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
            logger.error(f"Error publicando tweet: {e}")
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
            "tu post fue enviado",
            "tu post se envió",
            "tu publicación fue enviada",
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
    
    def _reorganizar_hashtags(self, texto: str) -> str:
        palabras = texto.split()
        hashtags = [p for p in palabras if p.startswith("#")]
        no_hashtags = [p for p in palabras if not p.startswith("#")]
        
        if not hashtags:
            return texto
        
        mid = len(no_hashtags) // 2
        resultado = no_hashtags[:mid] + ["\n"] + hashtags + ["\n"] + no_hashtags[mid:]
        return " ".join(resultado)
    
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
    
    def solo_retwittear(
        self,
        targets: list[str],
        usuario: str,
        mensaje_cita: str = None,
        es_calentamiento: bool = False,
        dar_like: bool = False,
        imagen_path: Optional[str] = None
    ) -> dict:
        resultados = {"exitos": 0, "fallidos": 0, "urls": []}
        
        if not self.driver:
            if not self.login_con_cookies():
                return resultados
        
        for url in targets:
            try:
                self.driver.get(url)
                time.sleep(3)
                
                if self._detectar_limite_cuenta():
                    break
                
                rt_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='retweet']")
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
                else:
                    rt_option = self.driver.find_element(By.CSS_SELECTOR, "[data-testid='retweetConfirm']")
                    self.driver.execute_script("arguments[0].click();", rt_option)
                
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
                
                time.sleep(random.uniform(2.5, 6.0))
            
            except Exception as e:
                resultados["fallidos"] += 1
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
    
    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Error cerrando driver: {e}")
