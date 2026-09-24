"""Entry point del bot de CLIENTES (PTB v21, async).

Bot INDEPENDIENTE del bot interno (`bot/`): token propio
(`TELEGRAM_CLIENTES_BOT_TOKEN`, creado con @BotFather) y su propio registro de
clientes (`data/clientes_bot.json`).

Comandos registrados:
  Cliente: /start /ayuda /help /cancelar
  Admin:   /clientes /asignar /quitar   (solo TELEGRAM_ADMIN_IDS)
Callbacks: cli_* (menu), cuenta_* (selector), codigo_*, nombre_*, foto_*.
Mensajes: texto (solo con flujo pendiente) y fotos (filters.PHOTO).

Arranque:
    python -m bot_clientes.main

Sin token real no construye la app: `import bot_clientes.main` es seguro (la
construccion solo ocurre dentro de `main()`).
"""

import logging
import os

from dotenv import load_dotenv
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot_clientes import handlers

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot_clientes.main")

MENSAJE_SIN_TOKEN = (
    "falta TELEGRAM_CLIENTES_BOT_TOKEN (crea el bot con @BotFather y pega su "
    "token en el .env del servidor)"
)
_PLACEHOLDERS = {
    "test_token_placeholder",
    "tu_token",
    "your_token",
    "your_token_here",
    "TU_TOKEN",
}


def obtener_token() -> str:
    """Token desde el entorno o, como respaldo, `settings` (sin editar config)."""
    token = (os.getenv("TELEGRAM_CLIENTES_BOT_TOKEN", "") or "").strip()
    if token:
        return token
    try:
        from core.config import settings

        return (getattr(settings, "telegram_clientes_bot_token", "") or "").strip()
    except Exception:
        return ""


def construir_app(token: str) -> Application:
    """Crea la Application y registra handlers (PTB v21)."""
    app = Application.builder().token(token).build()

    # Comandos del cliente.
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("ayuda", handlers.ayuda))
    app.add_handler(CommandHandler("help", handlers.ayuda))
    app.add_handler(CommandHandler("cancelar", handlers.cancelar))

    # Comandos de admin.
    app.add_handler(CommandHandler("clientes", handlers.comando_clientes))
    app.add_handler(CommandHandler("asignar", handlers.comando_asignar))
    app.add_handler(CommandHandler("quitar", handlers.comando_quitar))

    # Callbacks con prefijos propios (ver docstring de bot_clientes/handlers.py).
    app.add_handler(CallbackQueryHandler(handlers.cli_callback, pattern=r"^cli_"))
    app.add_handler(
        CallbackQueryHandler(handlers.cuenta_callback, pattern=r"^cuenta_")
    )
    app.add_handler(
        CallbackQueryHandler(handlers.codigo_callback, pattern=r"^codigo_")
    )
    app.add_handler(
        CallbackQueryHandler(handlers.nombre_callback, pattern=r"^nombre_")
    )
    app.add_handler(CallbackQueryHandler(handlers.foto_callback, pattern=r"^foto_"))

    # Texto: el handler solo actua si hay un flujo pendiente en user_data.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.texto_recibido)
    )
    # Imagenes: flujo de foto de perfil/portada.
    app.add_handler(MessageHandler(filters.PHOTO, handlers.foto_recibida))

    return app


def main() -> None:
    token = obtener_token()
    if not token or token in _PLACEHOLDERS:
        log.error(MENSAJE_SIN_TOKEN)
        raise SystemExit(1)
    app = construir_app(token)
    log.info(
        "Bot de clientes iniciado (PTB v21, polling). "
        "Cliente: /start /ayuda /cancelar | Admin: /clientes /asignar /quitar"
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
