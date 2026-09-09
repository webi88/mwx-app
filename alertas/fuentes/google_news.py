import feedparser
import requests
from datetime import datetime, timedelta
from loguru import logger
from concurrent.futures import ThreadPoolExecutor
import time


class GoogleNewsSource:
    def __init__(self):
        self.base_url = "https://news.google.com/rss/search"
    
    def buscar(self, keywords: list[str], localidad: str = "", horas: int = 24) -> list[dict]:
        menciones = []
        queries = self._construir_queries(keywords, localidad)
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = []
            for query in queries:
                future = executor.submit(self._buscar_query, query, horas)
                futures.append(future)
            
            for future in futures:
                try:
                    resultados = future.result()
                    menciones.extend(resultados)
                except Exception as e:
                    logger.error(f"Error en busqueda: {e}")
        
        return menciones
    
    def _construir_queries(self, keywords: list[str], localidad: str) -> list[str]:
        queries = []
        
        lotes = [keywords[i:i+3] for i in range(0, len(keywords), 3)]
        
        for lote in lotes:
            query = " OR ".join([f'"{kw}"' for kw in lote])
            query += " when:7d"
            queries.append(query)
            
            if localidad:
                query_con_localidad = f'({query}) "{localidad}"'
                queries.append(query_con_localidad)
        
        return queries
    
    def _buscar_query(self, query: str, horas: int) -> list[dict]:
        menciones = []
        
        try:
            params = {
                "q": query,
                "hl": "es-419",
                "gl": "MX",
                "ceid": "MX:es-419"
            }
            
            response = requests.get(self.base_url, params=params, timeout=30)
            
            if response.status_code == 200:
                feed = feedparser.parse(response.text)
                
                fecha_limite = datetime.now() - timedelta(hours=horas)
                
                for entry in feed.entries[:50]:
                    try:
                        fecha_publicacion = datetime(*entry.published_parsed[:6])
                        
                        if fecha_publicacion < fecha_limite:
                            continue
                        
                        mencion = {
                            "titulo": entry.title,
                            "enlace": entry.link,
                            "fuente": entry.get("source", {}).get("title", "Google News"),
                            "fecha": fecha_publicacion.isoformat(),
                            "tipo": "google_news"
                        }
                        
                        menciones.append(mencion)
                    except:
                        continue
            
            time.sleep(0.5)
        
        except Exception as e:
            logger.error(f"Error busqueda Google News: {e}")
        
        return menciones
    
    def buscar_mencion_directa(self, handle: str, horas: int = 24) -> list[dict]:
        query = f'"{handle}" site:x.com when:7d'
        return self._buscar_query(query, horas)
