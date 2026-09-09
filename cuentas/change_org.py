import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from faker import Faker
import random
import time
import json
import os
from datetime import datetime
from loguru import logger
from core.config import resolver_ruta, detectar_chrome_version


class ChangeOrgBot:
    def __init__(self):
        self.faker = Faker('es_MX')
        self.driver = None
        self.identidades_usadas = []
        self.reportes_path = resolver_ruta("reportes/firmas_change_org.json")
        os.makedirs("reportes", exist_ok=True)
    
    def generar_identidad(self) -> dict:
        nombre = self.faker.first_name()
        apellido = self.faker.last_name()
        email = f"{nombre.lower()}.{apellido.lower()}{random.randint(10,99)}@{random.choice(['gmail.com', 'hotmail.com', 'outlook.com', 'yahoo.com'])}"
        codigo_postal = self.faker.postcode()
        
        return {
            "nombre": nombre,
            "apellido": apellido,
            "email": email,
            "codigo_postal": codigo_postal,
            "timestamp": datetime.now().isoformat()
        }
    
    def configurar_navegador(self) -> bool:
        try:
            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1920,1080")
            
            self.driver = uc.Chrome(options=options, version_main=detectar_chrome_version(), use_subprocess=False)
            self.driver.set_page_load_timeout(30)
            
            logger.info("Navegador configurado para Change.org")
            return True
        
        except Exception as e:
            logger.error(f"Error configurando navegador: {e}")
            return False
    
    def escribir_humano(self, elemento, texto: str):
        try:
            elemento.click()
            time.sleep(0.1)
            
            for char in texto:
                if random.random() < 0.05:
                    wrong_char = chr(random.randint(97, 122))
                    elemento.send_keys(wrong_char)
                    time.sleep(0.1)
                    elemento.send_keys("\b")
                    time.sleep(0.1)
                
                elemento.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))
        
        except Exception as e:
            logger.error(f"Error escribiendo: {e}")
    
    def mover_mouse_aleatorio(self):
        try:
            actions = ActionChains(self.driver)
            
            for _ in range(random.randint(2, 5)):
                x = random.randint(100, 800)
                y = random.randint(100, 600)
                actions.move_by_offset(x, y)
                actions.pause(random.uniform(0.1, 0.3))
            
            actions.perform()
        
        except Exception as e:
            logger.error(f"Error moviendo mouse: {e}")
    
    def scroll_aleatorio(self):
        try:
            scroll_px = random.randint(200, 600)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_px})")
            time.sleep(random.uniform(0.5, 1.5))
            
            if random.random() < 0.3:
                scroll_up = random.randint(50, 200)
                self.driver.execute_script(f"window.scrollBy(0, -{scroll_up})")
                time.sleep(random.uniform(0.3, 0.8))
        
        except Exception as e:
            logger.error(f"Error en scroll: {e}")
    
    def firmar_peticion(self, url_peticion: str) -> dict:
        identidad = self.generar_identidad()
        resultado = {
            "identidad": identidad,
            "url": url_peticion,
            "exito": False,
            "error": None,
            "timestamp": datetime.now().isoformat()
        }
        
        try:
            self.driver.get(url_peticion)
            time.sleep(random.uniform(3, 5))
            
            self.mover_mouse_aleatorio()
            self.scroll_aleatorio()
            
            try:
                firmar_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='sign-button'], .sign-button, button[type='submit']"))
                )
                firmar_btn.click()
                time.sleep(3)
            except:
                try:
                    firmar_btn = self.driver.find_element(By.XPATH, "//button[contains(text(),'Firmar')]")
                    firmar_btn.click()
                    time.sleep(3)
                except:
                    resultado["error"] = "No se encontro boton de firmar"
                    return resultado
            
            try:
                nombre_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='name'], input[placeholder*='nombre'], input[placeholder*='Nombre']"))
                )
                self.escribir_humano(nombre_input, identidad["nombre"])
                time.sleep(0.5)
            except Exception as e:
                resultado["error"] = f"Error en campo nombre: {e}"
                return resultado
            
            try:
                apellido_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='lastName'], input[placeholder*='apellido'], input[placeholder*='Apellido']")
                self.escribir_humano(apellido_input, identidad["apellido"])
                time.sleep(0.5)
            except:
                pass
            
            try:
                email_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='email'], input[type='email'], input[placeholder*='email']")
                self.escribir_humano(email_input, identidad["email"])
                time.sleep(0.5)
            except:
                pass
            
            try:
                cp_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='postalCode'], input[placeholder*='postal'], input[placeholder*='código']")
                self.escribir_humano(cp_input, identidad["codigo_postal"])
                time.sleep(0.5)
            except:
                pass
            
            self.scroll_aleatorio()
            time.sleep(1)
            
            try:
                submit_btn = self.driver.find_element(By.CSS_SELECTOR, "button[type='submit'], button[data-testid='sign-button']")
                submit_btn.click()
                time.sleep(5)
                
                if "gracias" in self.driver.current_url.lower() or "thank" in self.driver.current_url.lower():
                    resultado["exito"] = True
                    logger.info(f"Firma exitosa: {identidad['nombre']} {identidad['apellido']}")
                else:
                    resultado["exito"] = True
                    logger.info(f"Firma procesada: {identidad['nombre']}")
            
            except Exception as e:
                resultado["error"] = f"Error al enviar: {e}"
            
            self.guardar_firma(identidad, url_peticion, resultado["exito"], resultado["error"])
            
            return resultado
        
        except Exception as e:
            resultado["error"] = str(e)
            logger.error(f"Error firmando: {e}")
            return resultado
    
    def guardar_firma(self, identidad: dict, url: str, exito: bool, error: str):
        try:
            if os.path.exists(self.reportes_path):
                with open(self.reportes_path, "r", encoding="utf-8") as f:
                    reportes = json.load(f)
            else:
                reportes = {"firmas": []}
            
            reportes["firmas"].append({
                "identidad": f"{identidad['nombre']} {identidad['apellido']}",
                "email": identidad["email"],
                "url": url,
                "exito": exito,
                "error": error,
                "timestamp": datetime.now().isoformat()
            })
            
            with open(self.reportes_path, "w", encoding="utf-8") as f:
                json.dump(reportes, f, indent=2, ensure_ascii=False)
        
        except Exception as e:
            logger.error(f"Error guardando firma: {e}")
    
    def ejecutar_sesion(
        self,
        url_peticion: str,
        firmas_por_ip: int = 5,
        reconexiones: int = 3
    ) -> dict:
        resultados = {
            "total": 0,
            "exitosas": 0,
            "fallidas": 0,
            "firmas": []
        }
        
        if not self.configurar_navegador():
            return resultados
        
        try:
            for reconexion in range(reconexiones):
                logger.info(f"Sesion {reconexion + 1}/{reconexiones}")
                
                for i in range(firmas_por_ip):
                    resultado = self.firmar_peticion(url_peticion)
                    
                    resultados["total"] += 1
                    if resultado["exito"]:
                        resultados["exitosas"] += 1
                    else:
                        resultados["fallidas"] += 1
                    
                    resultados["firmas"].append(resultado)
                    
                    time.sleep(random.uniform(5, 15))
                    
                    if random.random() < 0.2:
                        self.scroll_aleatorio()
                        time.sleep(random.uniform(2, 5))
                
                if reconexion < reconexiones - 1:
                    logger.info(f"Pausa para cambio de IP ({reconexion + 1}/{reconexiones})")
                    logger.info("Cambia tu IP manualmente y presiona Enter para continuar...")
                    input()
        
        except Exception as e:
            logger.error(f"Error en sesion: {e}")
        
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
        
        return resultados
    
    def main(self):
        import argparse
        
        parser = argparse.ArgumentParser(description="Bot de firmas Change.org")
        parser.add_argument("url", help="URL de la peticion a firmar")
        parser.add_argument("--firmas", type=int, default=5, help="Firmas por IP")
        parser.add_argument("--reconexiones", type=int, default=3, help="Numero de reconexiones")
        
        args = parser.parse_args()
        
        logger.info(f"Iniciando campana de firmas: {args.url}")
        logger.info(f"Firmas por IP: {args.firmas}, Reconexiones: {args.reconexiones}")
        
        resultados = self.ejecutar_sesion(
            url_peticion=args.url,
            firmas_por_ip=args.firmas,
            reconexiones=args.reconexiones
        )
        
        logger.info(f"Resultados: {resultados['exitosas']}/{resultados['total']} exitosas")
        
        return resultados


if __name__ == "__main__":
    bot = ChangeOrgBot()
    bot.main()
