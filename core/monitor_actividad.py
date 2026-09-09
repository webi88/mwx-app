import json
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from loguru import logger

from core.config import resolver_ruta
from plataformas.twitter.selenium_bot import TwitterBot


class MonitorActividad:
    def __init__(self):
        self.config_path = resolver_ruta("config/monitor_actividad_config.json")
        self.resultados_path = resolver_ruta("reportes/monitor_actividad.json")
        os.makedirs(resolver_ruta("reportes"), exist_ok=True)
    
    def cargar_config(self) -> dict:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {
            "cuentas_monitor": [],
            "cuentas_lectoras": [],
            "horas": 24
        }
    
    def guardar_resultados(self, resultados: dict):
        historial = []
        
        if os.path.exists(self.resultados_path):
            with open(self.resultados_path, "r", encoding="utf-8") as f:
                historial = json.load(f)
        
        resultados["timestamp"] = datetime.now().isoformat()
        historial.append(resultados)
        
        historial = historial[-100:]
        
        with open(self.resultados_path, "w", encoding="utf-8") as f:
            json.dump(historial, f, indent=2, ensure_ascii=False)
    
    def _iniciar_y_loguear(self, usuario: str) -> Optional[TwitterBot]:
        try:
            bot = TwitterBot(usuario)
            if bot.login_con_cookies():
                return bot
            return None
        except Exception as e:
            logger.error(f"Error iniciando {usuario}: {e}")
            return None
    
    def _revisar_con_lectora(
        self,
        cuenta_lectora: str,
        cuentas_asignadas: list[str],
        horas: int = 24
    ) -> dict:
        resultados = {
            "lectora": cuenta_lectora,
            "cuentas_revisadas": [],
            "errores": []
        }
        
        bot = self._iniciar_y_loguear(cuenta_lectora)
        
        if not bot:
            resultados["errores"].append(f"No se pudo loguear {cuenta_lectora}")
            return resultados
        
        try:
            for cuenta in cuentas_asignadas:
                try:
                    datos = bot.contar_tweets_periodo(cuenta, horas=horas)
                    
                    resultados["cuentas_revisadas"].append({
                        "usuario": cuenta,
                        "tweets": datos.get("tweets", 0),
                        "retweets": datos.get("retweets", 0),
                        "total": datos.get("total", 0)
                    })
                    
                    time.sleep(3)
                
                except Exception as e:
                    resultados["errores"].append(f"Error en {cuenta}: {e}")
        
        finally:
            if bot:
                bot.cerrar()
        
        return resultados
    
    def monitorizar(
        self,
        horas: int = 24,
        cuentas_filtro: list[str] = None,
        lectoras_filtro: list[str] = None
    ) -> dict:
        config = self.cargar_config()
        
        cuentas_monitor = cuentas_filtro or config.get("cuentas_monitor", [])
        lectoras = lectoras_filtro or config.get("cuentas_lectoras", [])
        
        if not cuentas_monitor or not lectoras:
            logger.warning("No hay cuentas o lectoras configuradas")
            return {"error": "Sin configuracion"}
        
        distribucion = {}
        for i, cuenta in enumerate(cuentas_monitor):
            lectora = lectoras[i % len(lectoras)]
            if lectora not in distribucion:
                distribucion[lectora] = []
            distribucion[lectora].append(cuenta)
        
        resultados = {
            "cuentas_monitor": len(cuentas_monitor),
            "lectoras_usadas": len(distribucion),
            "horas": horas,
            "resultados_por_lectora": [],
            "resumen": []
        }
        
        with ThreadPoolExecutor(max_workers=len(distribucion)) as executor:
            futures = {}
            for lectora, cuentas_asignadas in distribucion.items():
                future = executor.submit(
                    self._revisar_con_lectora,
                    lectora,
                    cuentas_asignadas,
                    horas
                )
                futures[future] = lectora
            
            for future in as_completed(futures):
                try:
                    resultado = future.result()
                    resultados["resultados_por_lectora"].append(resultado)
                    
                    for cuenta in resultado.get("cuentas_revisadas", []):
                        resultados["resumen"].append(cuenta)
                
                except Exception as e:
                    logger.error(f"Error en lectora: {e}")
        
        self.guardar_resultados(resultados)
        
        return resultados
    
    def obtener_estadisticas(self, horas: int = 24) -> dict:
        if not os.path.exists(self.resultados_path):
            return {"error": "Sin datos"}
        
        with open(self.resultados_path, "r", encoding="utf-8") as f:
            historial = json.load(f)
        
        fecha_limite = datetime.now() - timedelta(hours=horas)
        
        recientes = [
            r for r in historial
            if datetime.fromisoformat(r.get("timestamp", "")) >= fecha_limite
        ]
        
        if not recientes:
            return {"error": "Sin datos recientes"}
        
        ultimo = recientes[-1]
        
        total_tweets = sum(c.get("tweets", 0) for c in ultimo.get("resumen", []))
        total_retweets = sum(c.get("retweets", 0) for c in ultimo.get("resumen", []))
        
        return {
            "ultima_ejecucion": ultimo.get("timestamp"),
            "cuentas_monitor": ultimo.get("cuentas_monitor", 0),
            "total_tweets": total_tweets,
            "total_retweets": total_retweets,
            "total_actividad": total_tweets + total_retweets
        }


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Monitor de actividad Twitter")
    parser.add_argument("--horas", type=int, default=24, help="Ventana de tiempo en horas")
    parser.add_argument("--fecha-inicio", type=str, help="Fecha inicio (YYYY-MM-DD HH:MM)")
    parser.add_argument("--fecha-fin", type=str, help="Fecha fin (YYYY-MM-DD HH:MM)")
    parser.add_argument("--cuentas", type=str, help="Cuentas a revisar (separadas por coma)")
    parser.add_argument("--lectoras", type=str, help="Cuentas lectoras (separadas por coma)")
    
    args = parser.parse_args()
    
    monitor = MonitorActividad()
    
    cuentas_filtro = args.cuentas.split(",") if args.cuentas else None
    lectoras_filtro = args.lectoras.split(",") if args.lectoras else None
    
    horas = args.horas
    if args.fecha_inicio and args.fecha_fin:
        inicio = datetime.strptime(args.fecha_inicio, "%Y-%m-%d %H:%M")
        fin = datetime.strptime(args.fecha_fin, "%Y-%m-%d %H:%M")
        horas = int((fin - inicio).total_seconds() / 3600)
    
    resultados = monitor.monitorizar(
        horas=horas,
        cuentas_filtro=cuentas_filtro,
        lectoras_filtro=lectoras_filtro
    )
    
    print("\n=== RESULTADOS DEL MONITOR ===")
    print(f"Cuentas monitorizadas: {resultados.get('cuentas_monitor', 0)}")
    print(f"Lectoras usadas: {resultados.get('lectoras_usadas', 0)}")
    print(f"Horas: {resultados.get('horas', 0)}")
    
    for cuenta in resultados.get("resumen", []):
        print(f"\n@{cuenta['usuario']}:")
        print(f"  Tweets propios: {cuenta.get('tweets', 0)}")
        print(f"  Retweets: {cuenta.get('retweets', 0)}")
        print(f"  Total: {cuenta.get('total', 0)}")


if __name__ == "__main__":
    main()
