import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
import pickle
import time
import random
import os
from typing import Optional
from loguru import logger

from core.config import settings, resolver_ruta, detectar_chrome_version


class InstagramBot:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.driver = None
        self.base_url = "https://www.instagram.com"
        self.cookies_path = resolver_ruta(f"data/cookies/instagram/{usuario}.pkl")
    
    def _limpiar_procesos_chrome(self):
        import subprocess
        try:
            if os.name == "posix":
                subprocess.run(["pkill", "-f", "chromedriver"], capture_output=True)
            else:
                subprocess.run(["taskkill", "/F", "/IM", "chromedriver.exe"], capture_output=True)
        except:
            pass
    
    def _detectar_pantalla_externa(self) -> bool:
        try:
            import subprocess
            result = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True)
            return "Número" in result.stdout and result.stdout.count("Tipo de pantalla") > 1
        except:
            return False
    
    def _iniciar_driver(self):
        self._limpiar_procesos_chrome()
        
        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--incognito")
        options.add_argument(f"--user-data-dir={settings.profiles_dir / self.usuario}")
        
        mobile_emulation = {
            "deviceMetrics": {"width": 414, "height": 896, "pixelRatio": 3.0},
            "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        }
        options.add_experimental_option("mobileEmulation", mobile_emulation)
        
        if settings.headless:
            options.add_argument("--headless=new")
        
        try:
            self.driver = uc.Chrome(options=options, version_main=detectar_chrome_version(), use_subprocess=False)
            self.driver.set_page_load_timeout(30)
            return True
        except Exception as e:
            logger.error(f"Error iniciando Chrome: {e}")
            return False
    
    def esperar_login_manual(self, usuario: str) -> bool:
        try:
            if not self._iniciar_driver():
                return False
            
            self.driver.get(f"{self.base_url}/accounts/login/")
            
            logger.info(f"Esperando login manual para {usuario}...")
            logger.info("Haz login en el navegador y presiona Enter aqui cuando termines...")
            
            timeout = 300
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                current_url = self.driver.current_url
                
                if "/accounts/login" not in current_url and "challenge" not in current_url:
                    time.sleep(3)
                    
                    try:
                        ahora_no = self.driver.find_element(By.XPATH, "//button[contains(text(),'Ahora no'), contains(text(),'Not Now')]")
                        ahora_no.click()
                    except:
                        pass
                    
                    self.guardar_cookies()
                    logger.info(f"Login manual exitoso para {usuario}")
                    return True
                
                time.sleep(2)
            
            logger.warning(f"Timeout en login manual para {usuario}")
            return False
        
        except Exception as e:
            logger.error(f"Error en login manual: {e}")
            return False
    
    def login_con_cookies(self) -> bool:
        if not os.path.exists(self.cookies_path):
            logger.warning(f"No hay cookies para {self.usuario}")
            return False
        
        if not self._iniciar_driver():
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
            
            if "/accounts/login" in self.driver.current_url:
                logger.warning(f"Sesion expirada para {self.usuario}")
                return False
            
            try:
                ahora_no = self.driver.find_element(By.XPATH, "//button[contains(text(),'Ahora no'), contains(text(),'Not Now')]")
                ahora_no.click()
            except:
                pass
            
            logger.info(f"Login exitoso para {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en login: {e}")
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
    
    def publicar(self, contenido: str, imagen_path: Optional[str] = None) -> bool:
        if not imagen_path:
            logger.error("Instagram requiere imagen para publicar")
            return False
        
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(f"{self.base_url}/")
            time.sleep(3)
            
            crear_btn = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Nueva publicación'], [aria-label='New post']")
            crear_btn.click()
            time.sleep(2)
            
            input_file = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
            input_file.send_keys(os.path.abspath(imagen_path))
            time.sleep(3)
            
            siguiente_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Siguiente'), contains(text(),'Next')]")
            siguiente_btn.click()
            time.sleep(2)
            
            siguiente_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Siguiente'), contains(text(),'Next')]")
            siguiente_btn.click()
            time.sleep(2)
            
            caption = self.driver.find_element(By.CSS_SELECTOR, "textarea")
            caption.send_keys(contenido)
            
            compartir_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Compartir'), contains(text(),'Share')]")
            compartir_btn.click()
            
            time.sleep(5)
            
            logger.info(f"Post publicado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error publicando: {e}")
            return False
    
    def like(self, url_post: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_post)
            time.sleep(3)
            
            like_btn = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Me gusta'], [aria-label='Like']")
            like_btn.click()
            
            time.sleep(2)
            logger.info(f"Like dado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en like: {e}")
            return False
    
    def comentar(self, url_post: str, comentario: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_post)
            time.sleep(3)
            
            caja_comentario = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Agrega un comentario...'], [aria-label='Add a comment...']")
            caja_comentario.send_keys(comentario)
            
            publicar_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Publicar'), contains(text(),'Post')]")
            publicar_btn.click()
            
            time.sleep(2)
            logger.info(f"Comentario enviado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error comentando: {e}")
            return False
    
    def visualizar(self, url: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url)
            time.sleep(random.uniform(3, 6))
            
            scroll_px = random.randint(200, 500)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_px})")
            time.sleep(random.uniform(1, 2))
            
            logger.info(f"Visualizacion generada por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en visualizacion: {e}")
            return False
    
    def seguir_usuario(self, perfil_url: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(perfil_url)
            time.sleep(3)
            
            seguir_btn = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Seguir'], [aria-label='Follow']")
            seguir_btn.click()
            
            time.sleep(2)
            logger.info(f"Usuario seguido por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error siguiendo usuario: {e}")
            return False
    
    def compartir_post(self, url_post: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_post)
            time.sleep(3)
            
            compartir_btn = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Compartir'], [aria-label='Share']")
            compartir_btn.click()
            time.sleep(1)
            
            historia_btn = self.driver.find_element(By.XPATH, "//span[contains(text(),'Add post to your story'), contains(text(),'Agregar a tu historia')]")
            historia_btn.click()
            time.sleep(2)
            
            compartir_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Share'), contains(text(),'Compartir')]")
            compartir_btn.click()
            
            time.sleep(3)
            logger.info(f"Post compartido en historia por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error compartiendo: {e}")
            return False
    
    def reportar_post(self, url_post: str, motivo: str = "spam") -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_post)
            time.sleep(3)
            
            mas_opciones = self.driver.find_element(By.CSS_SELECTOR, "[aria-label='Más opciones'], [aria-label='More']")
            mas_opciones.click()
            time.sleep(1)
            
            reportar_btn = self.driver.find_element(By.XPATH, "//span[contains(text(),'Reportar'), contains(text(),'Report')]")
            reportar_btn.click()
            time.sleep(2)
            
            motivos = {
                "spam": "Spam",
                "hate": "Odio o acoso",
                "violence": "Violencia",
                "nudity": "Nudidad",
                "false_info": "Información falsa",
                "scam": "Estafa"
            }
            
            motivo_texto = motivos.get(motivo, "Spam")
            
            opcion = self.driver.find_element(By.XPATH, f"//span[contains(text(),'{motivo_texto}')]")
            opcion.click()
            time.sleep(1)
            
            submit_btn = self.driver.find_element(By.XPATH, "//button[text()='Submit'], //button[text()='Enviar']")
            submit_btn.click()
            
            time.sleep(2)
            logger.info(f"Post reportado: {url_post}")
            return True
        
        except Exception as e:
            logger.error(f"Error reportando post: {e}")
            return False
    
    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
