import pickle
import os
from typing import Optional
from loguru import logger
from core.config import resolver_ruta


class CookiesManager:
    @staticmethod
    def guardar_cookies(usuario: str, plataforma: str, cookies: list) -> bool:
        cookies_dir = resolver_ruta(f"data/cookies/{plataforma}")
        os.makedirs(cookies_dir, exist_ok=True)
        
        cookies_path = f"{cookies_dir}/{usuario}.pkl"
        
        try:
            with open(cookies_path, "wb") as f:
                pickle.dump(cookies, f)
            logger.info(f"Cookies guardadas para {usuario} ({plataforma})")
            return True
        except Exception as e:
            logger.error(f"Error guardando cookies: {e}")
            return False
    
    @staticmethod
    def cargar_cookies(usuario: str, plataforma: str) -> Optional[list]:
        cookies_path = resolver_ruta(f"data/cookies/{plataforma}/{usuario}.pkl")
        
        if not os.path.exists(cookies_path):
            logger.warning(f"No hay cookies para {usuario} ({plataforma})")
            return None
        
        try:
            with open(cookies_path, "rb") as f:
                cookies = pickle.load(f)
            logger.info(f"Cookies cargadas para {usuario} ({plataforma})")
            return cookies
        except Exception as e:
            logger.error(f"Error cargando cookies: {e}")
            return None
    
    @staticmethod
    def eliminar_cookies(usuario: str, plataforma: str) -> bool:
        cookies_path = resolver_ruta(f"data/cookies/{plataforma}/{usuario}.pkl")
        
        if os.path.exists(cookies_path):
            os.remove(cookies_path)
            logger.info(f"Cookies eliminadas para {usuario} ({plataforma})")
            return True
        return False
    
    @staticmethod
    def listar_cookies(plataforma: Optional[str] = None) -> list[dict]:
        cookies_dir = resolver_ruta("data/cookies")
        resultados = []
        
        if plataforma:
            platforms = [plataforma]
        else:
            platforms = ["twitter", "facebook", "instagram", "tiktok"]
        
        for plat in platforms:
            plat_dir = f"{cookies_dir}/{plat}"
            if os.path.exists(plat_dir):
                for archivo in os.listdir(plat_dir):
                    if archivo.endswith(".pkl"):
                        resultados.append({
                            "usuario": archivo.replace(".pkl", ""),
                            "plataforma": plat,
                            "path": f"{plat_dir}/{archivo}"
                        })
        
        return resultados
    
    @staticmethod
    def verificar_cookies(usuario: str, plataforma: str) -> bool:
        cookies_path = resolver_ruta(f"data/cookies/{plataforma}/{usuario}.pkl")
        
        if not os.path.exists(cookies_path):
            return False
        
        try:
            with open(cookies_path, "rb") as f:
                cookies = pickle.load(f)
            
            required_fields = ["name", "value", "domain"]
            for cookie in cookies:
                if all(field in cookie for field in required_fields):
                    return True
            return False
        except:
            return False
