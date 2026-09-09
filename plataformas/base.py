from abc import ABC, abstractmethod
from typing import Optional
from loguru import logger
from core.config import resolver_ruta


class PlataformaBase(ABC):
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.driver = None
        self.cookies_path = resolver_ruta(f"data/cookies/{usuario}.pkl")
    
    @abstractmethod
    def login_con_cookies(self) -> bool:
        pass
    
    @abstractmethod
    def guardar_cookies(self) -> bool:
        pass
    
    @abstractmethod
    def publicar(self, contenido: str, imagen_path: Optional[str] = None) -> bool:
        pass
    
    def cerrar(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.error(f"Error cerrando driver: {e}")


class PlataformaFactory:
    @staticmethod
    def crear_bot(plataforma: str, usuario: str) -> PlataformaBase:
        if plataforma == "twitter":
            from plataformas.twitter.selenium_bot import TwitterBot
            return TwitterBot(usuario)
        elif plataforma == "facebook":
            from plataformas.facebook.selenium_bot import FacebookBot
            return FacebookBot(usuario)
        elif plataforma == "instagram":
            from plataformas.instagram.selenium_bot import InstagramBot
            return InstagramBot(usuario)
        elif plataforma == "tiktok":
            from plataformas.tiktok.selenium_bot import TikTokBot
            return TikTokBot(usuario)
        else:
            raise ValueError(f"Plataforma no soportada: {plataforma}")
