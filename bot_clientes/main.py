"""Entry point del bot de CLIENTES (PTB v21, async).

Bot INDEPENDIENTE del bot interno (`bot/`): token propio
(`TELEGRAM_CLIENTES_BOT_TOKEN`, creado con @BotFather). Matriz de acceso:
  - Grupo de clientes (`TELEGRAM_CLIENTES_CHAT_ID`, default -1005538610567):
    cualquier miembro, SOLO codigo 2FA (TOTP) + ayuda.
  - Privado del admin (`TELEGRAM_ADMIN_IDS`): TODAS las opciones y /nombre.
  - Privado ajeno: mensaje corto; otros grupos: silencio.
Las cuentas son GLOBALES del grupo (`data/clientes_bot.json`).

Comandos registrados:
  Cliente: /start /ayuda /help /cancelar
  Cualquier chat: /id (muestra el ID de este chat)
  Admin:   /nombre <usuario> <Nuevo Nombre>   (solo privado del admin)
Callbacks: cli_* (menu), cuenta_* (selector), codigo_*, nombre_*, foto_*.
Mensajes: texto (solo con flujo pendiente) y fotos (filters.PHOTO).
Grupos: bienvenida al agregar el bot (new_chat_members + my_chat_member);
privado del admin con todo; privado ajeno con aviso corto; otros grupos, silencio.

Arranque:
    python -m bot_clientes.main

Sin token real no construye la app: `import bot_clientes.main` es seguro (la
construccion solo ocurre dentro de `main()`).

Ademas, `main()` RECHAZA arrancar si el token resuelto es el MISMO que el del
bot interno (`TELEGRAM_BOT_TOKEN`): Telegram solo permite un proceso haciendo
polling por token (el segundo muere con `Conflict: terminated by other
getUpdates request`), asi que se exige un bot NUEVO de @BotFather.
"""

import logging
import os

from dotenv import load_dotenv
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
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
MENSAJE_TOKEN_INTERNO = (
    "El TELEGRAM_CLIENTES_BOT_TOKEN es el MISMO que el del bot interno "
    "(TELEGRAM_BOT_TOKEN). Telegram no permite dos bots con el mismo token "
    "(conflicto de polling): se romperían ambos. Crea un bot NUEVO con "
    "@BotFather (/newbot), copia SU token y ponlo en TELEGRAM_CLIENTES_BOT_TOKEN."
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


def obtener_token_interno() -> str:
    """Token del bot INTERNO (solo para detectar que no se repita el mismo)."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    if token:
        return token
    try:
        from core.config import settings

        return (getattr(settings, "telegram_bot_token", "") or "").strip()
    except Exception:
        return ""


def es_token_del_bot_interno(token: str, interno: str) -> bool:
    """True si `token` no esta vacio y es EXACTAMENTE el token del bot interno.

    Compara con `.strip()` para no fallar por espacios copiados de mas. Un
    token vacio NUNCA cuenta como duplicado (de eso se encarga el guard de
    "falta token").
    """
    propio = (token or "").strip()
    ajeno = (interno or "").strip()
    return bool(propio) and bool(ajeno) and propio == ajeno


def construir_app(token: str) -> Application:
    """Crea la Application y registra handlers (PTB v21)."""
    app = Application.builder().token(token).build()

    # Comandos del cliente.
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("ayuda", handlers.ayuda))
    app.add_handler(CommandHandler("help", handlers.ayuda))
    app.add_handler(CommandHandler("cancelar", handlers.cancelar))
    # /id: funciona en CUALQUIER chat (incluso no permitidos), para descubrir
    # el chat_id del grupo donde agregaron el bot.
    app.add_handler(CommandHandler("id", handlers.comando_id))

    # Comando de admin (nombre REGISTRADO en el bot, sin Chrome).
    app.add_handler(CommandHandler("nombre", handlers.comando_nombre))

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

    # Grupos: cuando agregan el bot, saludar con instrucciones cortas. Se
    # registran los dos avisos de Telegram (el mensaje `new_chat_members` y el
    # `my_chat_member`) y el handler anti-duplicado evita el saludo doble.
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS, handlers.bienvenida_grupo
        )
    )
    app.add_handler(
        ChatMemberHandler(
            handlers.bienvenida_miembro,
            chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    return app


def main() -> None:
    token = obtener_token()
    if not token or token in _PLACEHOLDERS:
        log.error(MENSAJE_SIN_TOKEN)
        raise SystemExit(1)
    # Blindaje: el MISMO token del bot interno rompe AMBOS bots (un solo
    # getUpdates por token). Se exige un bot NUEVO de @BotFather.
    if es_token_del_bot_interno(token, obtener_token_interno()):
        log.error(MENSAJE_TOKEN_INTERNO)
        raise SystemExit(1)
    app = construir_app(token)
    log.info(
        "Bot de clientes iniciado (PTB v21, polling). "
        "Cliente: /start /ayuda /cancelar | Cualquier chat: /id | Admin: /nombre"
    )
    app.run_polling(
        allowed_updates=["message", "callback_query", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
