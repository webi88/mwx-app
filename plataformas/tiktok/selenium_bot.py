import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
import pickle
import time
import random
import os
from typing import Optional
from loguru import logger

from core.config import settings, resolver_ruta, detectar_chrome_version


class TikTokBot:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.driver = None
        self.base_url = "https://www.tiktok.com"
        self.cookies_path = resolver_ruta(f"data/cookies/tiktok/{usuario}.pkl")
    
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
            
            self.driver.get(f"{self.base_url}/login")
            
            logger.info(f"Esperando login manual para {usuario}...")
            logger.info("Haz login en el navegador y presiona Enter aqui cuando termines...")
            
            timeout = 300
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                current_url = self.driver.current_url
                
                if "/login" not in current_url:
                    time.sleep(3)
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
            
            if "/login" in self.driver.current_url:
                logger.warning(f"Sesion expirada para {self.usuario}")
                return False
            
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
    
    def publicar(self, contenido: str, video_path: Optional[str] = None) -> bool:
        if not video_path:
            logger.error("TikTok requiere video para publicar")
            return False
        
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(f"{self.base_url}/upload")
            time.sleep(5)
            
            input_file = self.driver.find_element(By.CSS_SELECTOR, "input[type='file'][accept*='video']")
            input_file.send_keys(os.path.abspath(video_path))
            
            tiempo_espera = 0
            while tiempo_espera < 60:
                try:
                    self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='upload_post']")
                    break
                except:
                    time.sleep(1)
                    tiempo_espera += 1
            
            caption = self.driver.find_element(By.CSS_SELECTOR, "[contenteditable='true']")
            caption.send_keys(contenido)
            
            publicar_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='upload_post']")
            publicar_btn.click()
            
            time.sleep(10)
            
            logger.info(f"Video publicado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error publicando: {e}")
            return False
    
    def like(self, url_video: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_video)
            time.sleep(5)
            
            like_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='like-icon']")
            like_btn.click()
            
            time.sleep(2)
            logger.info(f"Like dado por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error en like: {e}")
            return False
    
    def comentar(self, url_video: str, comentario: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_video)
            time.sleep(5)
            
            caja_comentario = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='comment-input']")
            caja_comentario.send_keys(comentario)
            caja_comentario.submit()
            
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
            time.sleep(random.uniform(5, 10))
            
            scroll_px = random.randint(200, 500)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_px})")
            time.sleep(random.uniform(2, 4))
            
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
            
            seguir_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='follow-button']")
            
            if "Following" in seguir_btn.text:
                logger.info("Ya sigues a este usuario")
                return True
            
            seguir_btn.click()
            
            time.sleep(2)
            logger.info(f"Usuario seguido por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error siguiendo usuario: {e}")
            return False
    
    def compartir_video(self, url_video: str) -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_video)
            time.sleep(5)
            
            compartir_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='share-icon']")
            compartir_btn.click()
            time.sleep(1)
            
            repost_btn = self.driver.find_element(By.XPATH, "//span[contains(text(),'Repost')]")
            repost_btn.click()
            
            time.sleep(3)
            logger.info(f"Video compartido por {self.usuario}")
            return True
        
        except Exception as e:
            logger.error(f"Error compartiendo: {e}")
            return False
    
    def reportar_post(self, url_video: str, motivo: str = "spam") -> bool:
        if not self.driver:
            if not self.login_con_cookies():
                return False
        
        try:
            self.driver.get(url_video)
            time.sleep(5)
            
            compartir_btn = self.driver.find_element(By.CSS_SELECTOR, "[data-e2e='share-icon']")
            compartir_btn.click()
            time.sleep(1)
            
            reportar_btn = self.driver.find_element(By.XPATH, "//span[contains(text(),'Report')]")
            reportar_btn.click()
            time.sleep(2)
            
            motivos = {
                "spam": "Spam",
                "hate": "Hate speech",
                "violence": "Violence",
                "nudity": "Nudity",
                "misinformation": "Misinformation"
            }
            
            motivo_texto = motivos.get(motivo, "Spam")
            
            opcion = self.driver.find_element(By.XPATH, f"//span[contains(text(),'{motivo_texto}')]")
            opcion.click()
            time.sleep(1)
            
            submit_btn = self.driver.find_element(By.XPATH, "//button[text()='Submit'], //button[text()='Next']")
            submit_btn.click()
            
            time.sleep(2)
            logger.info(f"Video reportado: {url_video}")
            return True
        
        except Exception as e:
            logger.error(f"Error reportando video: {e}")
            return False
    
    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
