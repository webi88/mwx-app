from alertas.fuentes.google_news import GoogleNewsSource
from alertas.fuentes.twitter_api import TwitterSource
from alertas.filtros import FiltrosGeograficos, FiltrosTematicos
from alertas.deduplicacion import Deduplicador
from alertas.notificador import NotificadorTelegram
from ia.filtros_alertas import FiltrosAlertas
from ia.celulas import CelulasManager
from loguru import logger
from concurrent.futures import ThreadPoolExecutor
import json


class MotorAlertas:
    def __init__(self):
        self.google_news = GoogleNewsSource()
        self.twitter_source = TwitterSource()
        self.filtros_geo = FiltrosGeograficos()
        self.filtros_tematicos = FiltrosTematicos()
        self.deduplicador = Deduplicador()
        self.notificador = NotificadorTelegram()
        self.filtros_ai = FiltrosAlertas()
        self.celulas_manager = CelulasManager()
    
    def ejecutar_alertas(self, cliente_id: int = None, horas: int = 24) -> dict:
        clientes = self.celulas_manager.obtener_clientes()
        
        if cliente_id:
            clientes = [c for c in clientes if c.id == cliente_id]
        
        resultados = {"total": 0, "enviadas": 0, "filtradas": 0, "duplicadas": 0}
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for cliente in clientes:
                future = executor.submit(self._procesar_cliente, cliente, horas)
                futures.append(future)
            
            for future in futures:
                try:
                    resultado = future.result()
                    resultados["total"] += resultado["total"]
                    resultados["enviadas"] += resultado["enviadas"]
                    resultados["filtradas"] += resultado["filtradas"]
                    resultados["duplicadas"] += resultado["duplicadas"]
                except Exception as e:
                    logger.error(f"Error procesando cliente: {e}")
        
        return resultados
    
    def _procesar_cliente(self, cliente, horas: int) -> dict:
        resultado = {"total": 0, "enviadas": 0, "filtradas": 0, "duplicadas": 0}
        
        try:
            keywords = json.loads(cliente.keywords) if cliente.keywords else []
            exclude_terms = json.loads(cliente.exclude_terms) if cliente.exclude_terms else []
            
            menciones = []
            
            menciones.extend(self.google_news.buscar(keywords, cliente.localidad, horas))
            menciones.extend(self.twitter_source.buscar(keywords, horas))
            
            resultado["total"] = len(menciones)
            
            menciones_filtradas = []
            for mencion in menciones:
                if self.filtros_tematicos.excluir(mencion, exclude_terms):
                    continue
                
                if cliente.localidad:
                    if not self.filtros_geo.verificar_region(mencion, cliente.localidad):
                        continue
                
                menciones_filtradas.append(mencion)
            
            resultado["filtradas"] = resultado["total"] - len(menciones_filtradas)
            
            menciones_filtradas = self.deduplicador.filtrar_duplicados(
                menciones_filtradas, cliente.id
            )
            
            resultado["duplicadas"] = len(menciones_filtradas) - len(
                [m for m in menciones_filtradas if not m.get("duplicada")]
            )
            
            menciones_finales = [m for m in menciones_filtradas if not m.get("duplicada")]
            
            if menciones_finales:
                chat_ids = cliente.telegram_chat_ids.split(",") if cliente.telegram_chat_ids else []
                
                for mencion in menciones_finales:
                    self.notificador.enviar_alerta(mencion, chat_ids)
                    resultado["enviadas"] += 1
            
            self.deduplicador.guardar_historial(menciones_finales, cliente.id)
            
        except Exception as e:
            logger.error(f"Error en _procesar_cliente: {e}")
        
        return resultado
