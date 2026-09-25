"""Notificador de alertas a Telegram.

Mejoras sobre la version anterior (paridad con el viejo):
- Formato con nombre de cliente y fecha.
- Division por LINEAS si el mensaje excede el limite de Telegram (nunca corta
  a media frase).
- Escapado basico de Markdown con reintento en texto plano si Telegram lo
  rechaza (`can't parse entities`).
- Devuelve False si no hay token (para que el motor no cuente "enviadas"
  falsas) y loguea el error por chat.
- URL resuelta (googlenewsdecoder) + acortada (is.gd) y boton "Ver noticia".
"""

import re
import time
from datetime import date, datetime

import requests
from loguru import logger

from core.config import settings
from utils.url_utils import acortar_url, resolver_url_google_news

_ESPECIALES_MD = re.compile(r"([_*`\[\]])")
_LIMITE_TELEGRAM = 4096
_MAX_PARTE = 4000


def _escapar_markdown(texto) -> str:
    """Escapa los caracteres basicos de Markdown (los que rompen el parse)."""
    return _ESPECIALES_MD.sub(r"\\\1", str(texto or ""))


def _quitar_escape(texto: str) -> str:
    """Revierte `_escapar_markdown` para el reintento en texto plano."""
    return re.sub(r"\\([_*`\[\]])", r"\1", texto or "")


def _dividir_mensaje(mensaje: str, max_len: int = _MAX_PARTE) -> list:
    """Divide por lineas sin cortar a media frase."""
    partes: list = []
    actual = ""
    for linea in (mensaje or "").split("\n"):
        if actual and len(actual) + len(linea) + 1 > max_len:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    return partes or [""]


def _formatear_fecha(fecha) -> str:
    if not fecha:
        return ""
    if isinstance(fecha, datetime):
        return fecha.strftime("%Y-%m-%d %H:%M")
    if isinstance(fecha, date):
        return fecha.strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(str(fecha).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(fecha)[:40]


def _envio_telegram_habilitado() -> bool:
    """ALERTAS_ENVIAR_TELEGRAM: 0 (default) = pausa; 1 = enviar a Telegram."""
    try:
        return int(getattr(settings, "alertas_enviar_telegram", 0) or 0) != 0
    except (TypeError, ValueError):
        return False  # ante duda, pausa (no mandar nada a los grupos)


class NotificadorTelegram:
    def __init__(self):
        self.bot_token = settings.telegram_bot_token or ""
        self.activo = bool(self.bot_token)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self.pausa_base = 3.5
        self.pausa_actual = self.pausa_base
        # Pausa de envio (ALERTAS_ENVIAR_TELEGRAM=0): el motor sigue detectando
        # y registrando, pero aqui no se toca la API de Telegram.
        self.envio_habilitado = _envio_telegram_habilitado()
        self._pausa_avisada = False
        self._pausa_detectadas = 0
        self._pausa_total_fijado = False

    # ------------------------------------------------------------------ #
    # Pausa de envio
    # ------------------------------------------------------------------ #
    def preparar_pausa(self, total: int = 0) -> None:
        """Fija el total de alertas de la corrida y avisa UNA vez (si hay pausa).

        El motor lo llama antes de intentar enviar; si nadie lo llama, el
        primer `enviar_alerta`/`enviar_resumen` avisa igual con lo detectado.
        """
        if self.envio_habilitado:
            return
        try:
            total = int(total or 0)
        except (TypeError, ValueError):
            total = 0
        self._pausa_detectadas = max(self._pausa_detectadas, total)
        self._pausa_total_fijado = True
        self._avisar_pausa()

    def _avisar_pausa(self) -> None:
        if self._pausa_avisada:
            return
        self._pausa_avisada = True
        logger.info(
            "Envío a Telegram en pausa (ALERTAS_ENVIAR_TELEGRAM=0): "
            f"{self._pausa_detectadas} alertas detectadas y registradas en el dashboard"
        )

    def _pausado(self) -> bool:
        """True si el envio esta pausado (avisa una vez y NO toca Telegram)."""
        if self.envio_habilitado:
            return False
        if not self._pausa_total_fijado:
            self._pausa_detectadas += 1
        self._avisar_pausa()
        return True

    # ------------------------------------------------------------------ #
    # Alertas individuales
    # ------------------------------------------------------------------ #
    def enviar_alerta(self, mencion: dict, chat_ids: list) -> bool:
        """Envia una alerta a TODOS los chat_ids.

        Devuelve True si al menos un chat la recibio; los fallos se loguean
        por chat (False si no habia token o ningun envio fue aceptado)."""
        if self._pausado():
            return False
        if not self.activo:
            logger.warning("Notificador Telegram desactivado (sin TELEGRAM_BOT_TOKEN); alerta NO enviada")
            return False
        if not mencion or not chat_ids:
            logger.warning("Notificador Telegram: alerta sin chat_ids configurados")
            return False

        titulo = _escapar_markdown((mencion.get("titulo") or "")[:110])
        resumen = _escapar_markdown(mencion.get("resumen_ai") or "")
        enlace = mencion.get("enlace", "")
        enlace_real = resolver_url_google_news(enlace) or enlace
        enlace_corto = acortar_url(enlace_real)
        fuente = _escapar_markdown(mencion.get("fuente") or mencion.get("fuente_medio") or "Desconocida")
        cliente = _escapar_markdown(mencion.get("cliente") or "")
        fecha = _formatear_fecha(mencion.get("fecha"))
        fuente_url = mencion.get("fuente_url", "")

        emoji = " ⭐" if mencion.get("es_principal") else ""
        cabecera = f"🚨{emoji} Alerta"
        if cliente:
            cabecera += f" — {cliente}"

        mensaje = f"{cabecera}\n\n📰 {titulo}\n\n"
        if resumen:
            mensaje += f"{resumen}\n\n"
        mensaje += f"🔗 {enlace_corto}\n"

        meta = []
        if fuente:
            meta.append(f"◆ {fuente}")
        if fecha:
            meta.append(f"📅 {fecha}")
        if meta:
            mensaje += " · ".join(meta) + "\n"
        if fuente_url:
            mensaje += f"🔍 {_escapar_markdown(fuente_url)}\n"

        exito = False
        for chat_id in chat_ids:
            chat_id = str(chat_id).strip()
            if not chat_id:
                continue

            if self._enviar_mensaje(chat_id, mensaje, enlace_real):
                exito = True
                self.pausa_actual = max(self.pausa_base, self.pausa_actual - 0.5)
            else:
                logger.error(f"Telegram: fallo el envio de la alerta al chat {chat_id}")
                self.pausa_actual = min(8.0, self.pausa_actual + 0.5)

            time.sleep(self.pausa_actual)

        return exito

    # ------------------------------------------------------------------ #
    # Resumen diario (por cliente)
    # ------------------------------------------------------------------ #
    def enviar_resumen(self, chat_id: str, cliente_nombre: str, menciones: list) -> bool:
        if self._pausado():
            return False
        if not self.activo:
            logger.warning("Notificador Telegram desactivado (sin TELEGRAM_BOT_TOKEN); resumen NO enviado")
            return False

        if not menciones:
            return True

        por_fuente: dict = {}
        for mencion in menciones:
            fuente = mencion.get("fuente", "Otra") or "Otra"
            por_fuente[fuente] = por_fuente.get(fuente, 0) + 1

        mensaje = (
            f"📊 RESUMEN DIARIO — {_escapar_markdown(cliente_nombre)}\n\n"
            f"Total de menciones: {len(menciones)}\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Por fuente:\n"
        )
        for fuente, cantidad in por_fuente.items():
            mensaje += f"  • {_escapar_markdown(fuente)}: {cantidad}\n"

        return self._enviar_mensaje(chat_id, mensaje)

    # ------------------------------------------------------------------ #
    # Envio de bajo nivel
    # ------------------------------------------------------------------ #
    def _enviar_mensaje(self, chat_id: str, mensaje: str, enlace_preview: str = None,
                        parse_mode: str = "Markdown") -> bool:
        if not self.activo:
            return False

        partes = _dividir_mensaje(mensaje)

        for indice, parte in enumerate(partes):
            parte_ok = False

            for intento in range(3):
                try:
                    payload = {"chat_id": chat_id, "text": parte}
                    if parse_mode:
                        payload["parse_mode"] = parse_mode
                    if enlace_preview and indice == 0:
                        payload["link_preview_options"] = {
                            "url": enlace_preview,
                            "prefer_large_media": True,
                        }
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
                        parte_ok = True
                        break

                    if response.status_code == 429:
                        try:
                            retry_after = response.json().get("parameters", {}).get("retry_after", 30)
                        except Exception:
                            retry_after = 30
                        logger.warning(f"Rate limit Telegram, esperando {retry_after}s")
                        time.sleep(retry_after)
                        continue

                    texto_error = (response.text or "").lower()
                    if parse_mode and ("parse" in texto_error or "entit" in texto_error):
                        # Reintento en texto plano (sin escapes) para no perder la alerta.
                        payload["text"] = _quitar_escape(parte)
                        payload.pop("parse_mode", None)
                        response = requests.post(
                            f"{self.base_url}/sendMessage",
                            json=payload,
                            timeout=30
                        )
                        if response.status_code == 200:
                            parte_ok = True
                            break

                    logger.error(
                        f"Error Telegram chat {chat_id}: "
                        f"{response.status_code} - {(response.text or '')[:200]}"
                    )
                    time.sleep(5)

                except Exception as e:
                    logger.error(f"Error enviando mensaje a Telegram ({chat_id}): {e}")
                    time.sleep(5)

            if not parte_ok:
                return False

        return True
