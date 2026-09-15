import curl_cffi.requests as requests
import pickle
import time
import random
import os
from typing import Optional
from loguru import logger

from core.config import resolver_ruta
from utils.anti_detection import normalizar_cookies, resolver_ua_cuenta


class TwitterAPI:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.session = None
        self.bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

    def _leer_cuenta(self):
        """Devuelve la fila `Cuenta` de la BD (o None) para leer su UA/cookies."""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                return db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
        except Exception as e:
            logger.warning(f"No se pudo leer la cuenta {self.usuario}: {e}")
            return None

    def _cargar_cookies(self) -> bool:
        cuenta = self._leer_cuenta()

        cookies_list = None
        if os.path.exists(self.cookies_path):
            try:
                with open(self.cookies_path, "rb") as f:
                    cookies_list = pickle.load(f)
            except Exception as e:
                logger.error(f"Error leyendo {self.cookies_path}: {e}")

        # Sin .pkl (o ilegible): usa TODAS las cookies guardadas en la BD.
        if not cookies_list and cuenta is not None:
            cookies_json = getattr(cuenta, "cookies_json", None)
            if cookies_json:
                logger.info(f"Sin .pkl para {self.usuario}; usando cookies_json de la BD")
                cookies_list = cookies_json

        cookies_norm = normalizar_cookies(cookies_list)
        if not cookies_norm:
            logger.warning(f"No hay cookies para {self.usuario}")
            return False

        try:
            self.session = requests.Session(impersonate="chrome120")

            cookie_dict = {c["name"]: c["value"] for c in cookies_norm}
            self.session.cookies.update(cookie_dict)

            ct0 = cookie_dict.get("ct0", "")
            auth_token = cookie_dict.get("auth_token", "")

            headers = {
                "authorization": f"Bearer {self.bearer_token}",
                "x-csrf-token": ct0,
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "referer": "https://x.com/",
                "origin": "https://x.com",
            }

            # UA EXACTO de la cuenta (del lote). Si la cuenta no lo tiene, se
            # omite el header y curl_cffi usa el UA natural de su impersonacion.
            ua_cuenta = resolver_ua_cuenta(cuenta)
            if ua_cuenta:
                headers["user-agent"] = ua_cuenta
            else:
                logger.info(f"{self.usuario} sin user_agent; curl_cffi usara su UA natural")

            self.session.headers.update(headers)

            return True

        except Exception as e:
            logger.error(f"Error cargando cookies: {e}")
            return False
    
    def retweet(self, tweet_url: str) -> bool:
        if not self.session and not self._cargar_cookies():
            return False
        
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False
            
            variables = {"source_tweet_id": tweet_id}
            features = {
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }
            
            response = self.session.post(
                "https://x.com/i/api/graphql/ojPdsZsimiJrUGLR1sjUtA/CreateRetweet",
                json={"variables": variables, "features": features}
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" not in data:
                    logger.info(f"RT exitoso para {self.usuario}")
                    return True
            
            logger.warning(f"RT fallido para {self.usuario}: {response.status_code}")
            return False
        
        except Exception as e:
            logger.error(f"Error en retweet: {e}")
            return False
    
    def like(self, tweet_url: str) -> bool:
        if not self.session and not self._cargar_cookies():
            return False
        
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False
            
            variables = {"source_tweet_id": tweet_id}
            features = {
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }
            
            response = self.session.post(
                "https://x.com/i/api/graphql/lZ0GCEojmtQfiUQa5oJSEw/FavoriteTweet",
                json={"variables": variables, "features": features}
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" not in data:
                    logger.info(f"Like exitoso para {self.usuario}")
                    return True
            
            logger.warning(f"Like fallido para {self.usuario}: {response.status_code}")
            return False
        
        except Exception as e:
            logger.error(f"Error en like: {e}")
            return False
    
    def _extraer_tweet_id(self, url: str) -> Optional[str]:
        import re
        match = re.search(r"/status/(\d+)", url)
        return match.group(1) if match else None
    
    def verificar_sesion(self) -> bool:
        if not self.session and not self._cargar_cookies():
            return False
        
        try:
            response = self.session.get("https://x.com/i/api/1.1/account/verify_credentials.json")
            return response.status_code == 200
        except:
            return False
    
    def rt_rapido(self, urls: list[str], usuario_ids: list[str]) -> dict:
        resultados = {"exitos": 0, "fallidos": 0}
        
        for url in urls:
            for usuario_id in usuario_ids:
                api = TwitterAPI(usuario_id)
                if api.retweet(url):
                    resultados["exitos"] += 1
                else:
                    resultados["fallidos"] += 1
                
                time.sleep(random.uniform(2.5, 6.0))
        
        return resultados
