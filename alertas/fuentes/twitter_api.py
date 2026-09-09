import curl_cffi.requests as requests
import pickle
import time
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from core.config import resolver_ruta
import os


class TwitterSource:
    def __init__(self):
        self.bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
    
    def buscar(self, keywords: list[str], horas: int = 24) -> list[dict]:
        menciones = []
        
        cookies_paths = self._obtener_cookies_disponibles()
        
        for cookies_path in cookies_paths[:3]:
            try:
                session = self._crear_sesion(cookies_path)
                if not session:
                    continue
                
                for keyword in keywords[:5]:
                    resultados = self._buscar_keyword(session, keyword, horas)
                    menciones.extend(resultados)
                    time.sleep(2)
                
                break
            except Exception as e:
                logger.error(f"Error con cookies {cookies_path}: {e}")
                continue
        
        return menciones
    
    def _obtener_cookies_disponibles(self) -> list[str]:
        cookies_dir = resolver_ruta("data/cookies/twitter")
        if not os.path.exists(cookies_dir):
            return []
        
        cookies_paths = []
        for archivo in os.listdir(cookies_dir):
            if archivo.endswith(".pkl"):
                cookies_paths.append(f"{cookies_dir}/{archivo}")
        
        return cookies_paths
    
    def _crear_sesion(self, cookies_path: str) -> Optional[requests.Session]:
        try:
            with open(cookies_path, "rb") as f:
                cookies_list = pickle.load(f)
            
            session = requests.Session(impersonate="chrome120")
            
            cookie_dict = {}
            for cookie in cookies_list:
                cookie_dict[cookie["name"]] = cookie["value"]
            
            session.cookies.update(cookie_dict)
            
            ct0 = cookie_dict.get("ct0", "")
            
            session.headers.update({
                "authorization": f"Bearer {self.bearer_token}",
                "x-csrf-token": ct0,
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "user-agent": self.user_agents[0],
                "referer": "https://x.com/",
            })
            
            return session
        except Exception as e:
            logger.error(f"Error creando sesion: {e}")
            return None
    
    def _buscar_keyword(self, session: requests.Session, keyword: str, horas: int) -> list[dict]:
        menciones = []
        
        try:
            query = f'"{keyword}"'
            
            variables = {
                "query": query,
                "count": 20,
                "querySource": "typed_query",
                "product": "Latest"
            }
            
            features = {
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }
            
            response = session.get(
                "https://x.com/i/api/graphql/gkjsKepM6gl_HmFWoWKfgg/SearchTimeline",
                params={"variables": str(variables), "features": str(features)}
            )
            
            if response.status_code == 200:
                data = response.json()
                
                try:
                    instructions = data["data"]["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"]
                    
                    for instruction in instructions:
                        if instruction.get("type") == "TimelineAddEntries":
                            entries = instruction.get("entries", [])
                            
                            for entry in entries:
                                try:
                                    tweet = entry["content"]["itemContent"]["tweet_results"]["result"]
                                    
                                    texto = tweet.get("legacy", {}).get("full_text", "")
                                    fecha_str = tweet.get("legacy", {}).get("created_at", "")
                                    
                                    if fecha_str:
                                        fecha = datetime.strptime(fecha_str, "%a %b %d %H:%M:%S %z %Y")
                                        fecha_limite = datetime.now().astimezone() - timedelta(hours=horas)
                                        
                                        if fecha < fecha_limite:
                                            continue
                                    
                                    mencion = {
                                        "titulo": texto[:200],
                                        "enlace": f"https://x.com/i/status/{tweet.get('rest_id', '')}",
                                        "fuente": "Twitter/X",
                                        "fecha": fecha_str,
                                        "tipo": "twitter",
                                        "kw_principal": keyword
                                    }
                                    
                                    menciones.append(mencion)
                                except:
                                    continue
                except:
                    pass
            
            time.sleep(2)
        
        except Exception as e:
            logger.error(f"Error busqueda Twitter: {e}")
        
        return menciones
    
    def buscar_mencion_directa(self, handle: str, horas: int = 24) -> list[dict]:
        cookies_paths = self._obtener_cookies_disponibles()
        
        if not cookies_paths:
            return []
        
        session = self._crear_sesion(cookies_paths[0])
        if not session:
            return []
        
        return self._buscar_keyword(session, handle, horas)
