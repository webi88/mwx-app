import json
import os
import sys
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from core.database import get_db_session
from core.models import Cliente, ReporteDiario
from alertas.fuentes.google_news import GoogleNewsSource
from alertas.fuentes.twitter_api import TwitterSource
from alertas.filtros import FiltrosGeograficos, FiltrosTematicos
from alertas.deduplicacion import Deduplicador
from alertas.notificador import NotificadorTelegram
from ia.filtros_alertas import FiltrosAlertas
from ia.celulas import CelulasManager
from utils.url_utils import resolver_url_google_news


class AutoAlertas:
    def __init__(self):
        self.google_news = GoogleNewsSource()
        self.twitter_source = TwitterSource()
        self.filtros_geo = FiltrosGeograficos()
        self.filtros_tematicos = FiltrosTematicos()
        self.deduplicador = Deduplicador()
        self.notificador = NotificadorTelegram()
        self.filtros_ai = FiltrosAlertas()
        self.celulas_manager = CelulasManager()
        self.historial_path = "reportes/alertas_historial.json"
        self.menciones_dia_path = "reportes/menciones_dia"
        os.makedirs("reportes", exist_ok=True)
    
    def _fue_despertada_por_pmset(self) -> bool:
        try:
            import subprocess
            result = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True)
            
            if "RTC" in result.stdout or "Maintenance" in result.stdout:
                lines = result.stdout.split("\n")
                for line in lines[-20:]:
                    if ("wake" in line.lower() or "RTC" in line) and "202" in line:
                        return True
            return False
        except:
            return False
    
    def _dormir_mac(self):
        try:
            import subprocess
            subprocess.run(["pmset", "sleepnow"])
            logger.info("Mac duerme...")
        except:
            pass
    
    def cargar_historial_urls(self) -> dict:
        if os.path.exists(self.historial_path):
            with open(self.historial_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"por_cliente": {}}
    
    def guardar_historial_urls(self, historial: dict):
        try:
            for cliente in historial.get("por_cliente", {}):
                if isinstance(historial["por_cliente"][cliente], set):
                    historial["por_cliente"][cliente] = list(historial["por_cliente"][cliente])
            
            with open(self.historial_path, "w", encoding="utf-8") as f:
                json.dump(historial, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando historial: {e}")
    
    def agrupar_por_temas(self, menciones: list[dict]) -> list[dict]:
        return self.filtros_ai.agrupar_temas([m["titulo"] for m in menciones])
    
    def _guardar_menciones_dia(self, menciones: list[dict], cliente_nombre: str):
        try:
            fecha = datetime.now().strftime("%Y-%m-%d")
            archivo = f"{self.menciones_dia_path}_{fecha}.json"
            
            if os.path.exists(archivo):
                with open(archivo, "r", encoding="utf-8") as f:
                    datos = json.load(f)
            else:
                datos = {"menciones": []}
            
            for mencion in menciones:
                existe = any(m.get("enlace") == mencion.get("enlace") for m in datos["menciones"])
                if not existe:
                    mencion["cliente"] = cliente_nombre
                    datos["menciones"].append(mencion)
            
            with open(archivo, "w", encoding="utf-8") as f:
                json.dump(datos, f, indent=2, ensure_ascii=False)
        
        except Exception as e:
            logger.error(f"Error guardando menciones del dia: {e}")
    
    def _cargar_menciones_dia(self) -> list[dict]:
        try:
            fecha = datetime.now().strftime("%Y-%m-%d")
            archivo = f"{self.menciones_dia_path}_{fecha}.json"
            
            if os.path.exists(archivo):
                with open(archivo, "r", encoding="utf-8") as f:
                    datos = json.load(f)
                return datos.get("menciones", [])
            return []
        except:
            return []
    
    def _enviar_resumen_diario(self, cliente: Cliente):
        try:
            menciones = self._cargar_menciones_dia()
            menciones_cliente = [m for m in menciones if m.get("cliente") == cliente.nombre]
            
            if not menciones_cliente:
                return
            
            por_fuente = {}
            for m in menciones_cliente:
                fuente = m.get("fuente", "Otra")
                por_fuente[fuente] = por_fuente.get(fuente, 0) + 1
            
            chat_ids = cliente.telegram_chat_ids.split(",") if cliente.telegram_chat_ids else []
            
            for chat_id in chat_ids:
                chat_id = chat_id.strip()
                if chat_id:
                    self.notificador.enviar_resumen(chat_id, cliente.nombre, menciones_cliente)
            
            try:
                with get_db_session() as db:
                    reporte = ReporteDiario(
                        fecha=datetime.now().date(),
                        cliente_id=cliente.id,
                        total_alertas=len(menciones_cliente),
                        por_fuente=por_fuente,
                        enviado=True
                    )
                    db.add(reporte)
                    db.commit()
            except:
                pass
        
        except Exception as e:
            logger.error(f"Error enviando resumen diario: {e}")
    
    def ejecutar_alertas(
        self,
        clientes_filtro: list[str] = None,
        horas: int = 24,
        resumen_diario: bool = False
    ) -> dict:
        clientes = self.celulas_manager.obtener_clientes()
        
        if clientes_filtro:
            clientes = [c for c in clientes if c.nombre in clientes_filtro]
        
        resultados = {
            "total": 0,
            "enviadas": 0,
            "filtradas": 0,
            "duplicadas": 0,
            "clientes procesados": len(clientes)
        }
        
        historial = self.cargar_historial_urls()
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for cliente in clientes:
                future = executor.submit(
                    self._procesar_cliente,
                    cliente,
                    horas,
                    historial
                )
                futures.append((future, cliente))
            
            for future, cliente in futures:
                try:
                    resultado = future.result()
                    resultados["total"] += resultado["total"]
                    resultados["enviadas"] += resultado["enviadas"]
                    resultados["filtradas"] += resultado["filtradas"]
                    resultados["duplicadas"] += resultado["duplicadas"]
                    
                    if resumen_diario:
                        self._enviar_resumen_diario(cliente)
                
                except Exception as e:
                    logger.error(f"Error procesando cliente {cliente.nombre}: {e}")
        
        self.guardar_historial_urls(historial)
        
        return resultados
    
    def _procesar_cliente(
        self,
        cliente: Cliente,
        horas: int,
        historial: dict
    ) -> dict:
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
            
            urls_existentes = set(historial.get("por_cliente", {}).get(str(cliente.id), []))
            
            menciones_finales = []
            for mencion in menciones_filtradas:
                url = mencion.get("enlace", "")
                
                if url in urls_existentes:
                    resultado["duplicadas"] += 1
                    continue
                
                menciones_finales.append(mencion)
            
            if menciones_finales:
                chat_ids = cliente.telegram_chat_ids.split(",") if cliente.telegram_chat_ids else []
                
                for mencion in menciones_finales:
                    enlace = mencion.get("enlace", "")
                    enlace_real = resolver_url_google_news(enlace) or enlace
                    mencion["enlace_real"] = enlace_real
                    
                    self.notificador.enviar_alerta(mencion, chat_ids)
                    resultado["enviadas"] += 1
                    
                    if str(cliente.id) not in historial.get("por_cliente", {}):
                        historial.setdefault("por_cliente", {})[str(cliente.id)] = []
                    historial["por_cliente"][str(cliente.id)].append(enlace)
            
            self._guardar_menciones_dia(menciones_finales, cliente.nombre)
            
        except Exception as e:
            logger.error(f"Error en _procesar_cliente: {e}")
        
        return resultado


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Sistema de alertas automaticas")
    parser.add_argument("--clientes", type=str, help="Filtrar por clientes (separados por coma)")
    parser.add_argument("--horas", type=int, default=24, help="Ventana de tiempo en horas")
    parser.add_argument("--resumen-diario", action="store_true", help="Enviar resumen diario")
    
    args = parser.parse_args()
    
    fue_despertada = False
    try:
        auto = AutoAlertas()
        
        if sys.platform == "darwin":
            fue_despertada = auto._fue_despertada_por_pmset()
        
        clientes_filtro = args.clientes.split(",") if args.clientes else None
        
        resultados = auto.ejecutar_alertas(
            clientes_filtro=clientes_filtro,
            horas=args.horas,
            resumen_diario=args.resumen_diario
        )
        
        logger.info(f"Alertas completadas: {resultados}")
        
        if fue_despertada:
            auto._dormir_mac()
    
    except Exception as e:
        logger.error(f"Error en main: {e}")
        
        if fue_despertada:
            try:
                import subprocess
                subprocess.run(["pmset", "sleepnow"])
            except:
                pass


if __name__ == "__main__":
    main()
