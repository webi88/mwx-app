import requests
import time
from loguru import logger
from core.config import settings
from utils.url_utils import acortar_url, resolver_url_google_news


class NotificadorTelegram:
    def __init__(self):
        self.bot_token = settings.telegram_bot_token or ""
        self.activo = bool(self.bot_token)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.pausa_base = 3.5
        self.pausa_actual = self.pausa_base
    
    def enviar_alerta(self, mencion: dict, chat_ids: list[str]) -> bool:
        if not self.activo:
            logger.debug("Notificador Telegram desactivado (sin bot_token)")
            return True

        titulo = mencion.get("titulo", "")[:110]
        resumen = mencion.get("resumen_ai", "")
        enlace = mencion.get("enlace", "")
        fuente = mencion.get("fuente", "Desconocida")
        es_principal = mencion.get("es_principal", False)
        
        enlace_real = resolver_url_google_news(enlace) or enlace
        enlace_corto = acortar_url(enlace_real)
        
        emoji = "⭐" if es_principal else ""
        
        mensaje = (
            f"🚨{emoji} Alerta\n\n"
            f"📰 {titulo}\n\n"
        )
        
        if resumen:
            mensaje += f"{resumen}\n\n"
        
        mensaje += f"🔗 {enlace_corto}\n"
        mensaje += f"◆ {fuente}"
        
        exito = True
        for chat_id in chat_ids:
            chat_id = chat_id.strip()
            if not chat_id:
                continue
            
            if not self._enviar_mensaje(chat_id, mensaje, enlace_real):
                exito = False
            
            time.sleep(self.pausa_actual)
        
        return exito
    
    def _enviar_mensaje(self, chat_id: str, mensaje: str, enlace_preview: str = None) -> bool:
        if not self.activo:
            return False

        for intento in range(3):
            try:
                payload = {
                    "chat_id": chat_id,
                    "text": mensaje[:4096],
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False
                }
                
                if enlace_preview:
                    payload["reply_markup"] = {
                        "inline_keyboard": [[
                            {"text": "Ver noticia", "url": enlace_preview}
                        ]]
                    }
                
                response = requests.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                    timeout=30
                )
                
                if response.status_code == 200:
                    self.pausa_actual = max(self.pausa_base, self.pausa_actual - 0.5)
                    return True
                
                elif response.status_code == 429:
                    retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                    logger.warning(f"Rate limit Telegram, esperando {retry_after}s")
                    time.sleep(retry_after)
                    continue
                
                else:
                    logger.error(f"Error Telegram: {response.status_code} - {response.text}")
                    
                    if "parse" in response.text.lower():
                        payload["parse_mode"] = ""
                        response = requests.post(
                            f"{self.base_url}/sendMessage",
                            json=payload,
                            timeout=30
                        )
                        if response.status_code == 200:
                            return True
                    
                    self.pausa_actual = min(8.0, self.pausa_actual + 0.5)
                    time.sleep(5)
            
            except Exception as e:
                logger.error(f"Error enviando mensaje: {e}")
                time.sleep(5)
        
        return False
    
    def enviar_resumen(self, chat_id: str, cliente_nombre: str, menciones: list[dict]) -> bool:
        if not self.activo:
            logger.debug("Notificador Telegram desactivado (sin bot_token)")
            return True

        if not menciones:
            return True
        
        por_fuente = {}
        for m in menciones:
            fuente = m.get("fuente", "Otra")
            por_fuente[fuente] = por_fuente.get(fuente, 0) + 1
        
        mensaje = (
            f"📊 RESUMEN DIARIO - {cliente_nombre}\n\n"
            f"Total de menciones: {len(menciones)}\n\n"
            f"Por fuente:\n"
        )
        
        for fuente, cantidad in por_fuente.items():
            mensaje += f"  • {fuente}: {cantidad}\n"
        
        return self._enviar_mensaje(chat_id, mensaje)
