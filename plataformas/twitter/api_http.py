import curl_cffi.requests as requests
import pickle
import time
import random
import os
from typing import Optional
from loguru import logger

from core.config import resolver_ruta


class TwitterAPI:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.session = None
        self.bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
    
    def _cargar_cookies(self) -> bool:
        if not os.path.exists(self.cookies_path):
            logger.warning(f"No hay cookies para {self.usuario}")
            return False
        
        try:
            with open(self.cookies_path, "rb") as f:
                cookies_list = pickle.load(f)
            
            self.session = requests.Session(impersonate="chrome120")
            
            cookie_dict = {}
            for cookie in cookies_list:
                cookie_dict[cookie["name"]] = cookie["value"]
            
            self.session.cookies.update(cookie_dict)
            
            ct0 = cookie_dict.get("ct0", "")
            auth_token = cookie_dict.get("auth_token", "")
            
            self.session.headers.update({
                "authorization": f"Bearer {self.bearer_token}",
                "x-csrf-token": ct0,
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "user-agent": random.choice(self.user_agents),
                "referer": "https://x.com/",
                "origin": "https://x.com",
            })
            
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
