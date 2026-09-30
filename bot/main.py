"""Entry point del bot de Telegram (PTB v21, async).

Registra comandos y callbacks. Sin token real no construye la app
(verificacion de solo-imports: ast.parse).
Comandos registrados: /start /help /ayuda /status /cuentas
/cuentas_verificar /brandear /cambiar_perfil /cambiar_nombre /cambiar_handle
/codigo.
Callbacks: menu_*, cuentas_verificar, brandeo:*, perfil_*, foto_*.
Fotos: MessageHandler(filters.PHOTO) -> foto_recibida.
Handler de errores global (`_manejar_error`): el 409 Conflict del solape de
deploy se loguea en UNA linea (sin traceback); el resto de errores con su
tipo y mensaje.
"""

import logging
import os

from dotenv import load_dotenv
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.handlers.ayuda import ayuda
from bot.handlers.comandos import help_cmd, menu_callback, start, status
from bot.handlers.cuentas import (
    brandear,
    brandear_callback,
    cambiar_handle,
    cambiar_nombre,
    cambiar_perfil,
    cuentas,
    cuentas_verificar,
    cuentas_verificar_callback,
    foto_cancelar_callback,
    foto_manual_callback,
    foto_recibida,
    perfil_brandeo_callback,
    perfil_cancelar_callback,
    perfil_foto_callback,
    perfil_portada_callback,
    perfil_verificar_callback,
)
from bot.handlers.verificaciones import comando_codigo

load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot.main")


def obtener_token() -> str:
    """Token desde TELEGRAM_BOT_TOKEN (env/.env) o core.config.settings."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    if token:
        return token
    try:
        from core.config import settings

        return (getattr(settings, "telegram_bot_token", "") or "").strip()
    except Exception:
        return ""


async def _manejar_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Error handler global (PTB v21): el 409 Conflict no ensucia el log.

    Durante el solape del deploy viejo/nuevo, Telegram responde 409 a
    `getUpdates` (`Conflict: terminated by other getUpdates request`); es
    TRANSITORIO y PTB reintenta solo. Se registra en UNA linea (sin traceback)
    con la accion a tomar si persistiera. Cualquier otro error se registra con
    su tipo y mensaje (equivalente al default de PTB, sin el aviso "No error
    handlers are registered").
    """
    error = getattr(context, "error", None)
    if isinstance(error, Conflict):
        log.warning(
            "Conflicto de polling (409): hay otra instancia con este token "
            "(normal durante un deploy). PTB reintentará solo; si persiste más "
            "de ~5 min, busca un proceso/servicio duplicado. Detalle: %s",
            error,
        )
        return
    log.error(
        "Error no controlado en el bot: %s: %s",
        type(error).__name__,
        error,
        exc_info=error,
    )


def construir_app(token: str) -> Application:
    """Crea la Application y registra handlers (PTB v21)."""
    app = Application.builder().token(token).build()
    app.add_error_handler(_manejar_error)

    # Comandos base / menu
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("ayuda", ayuda))
    app.add_handler(CommandHandler("status", status))

    # Cuentas + brandeo + perfil/fotos
    app.add_handler(CommandHandler("cuentas", cuentas))
    app.add_handler(CommandHandler("cuentas_verificar", cuentas_verificar))
    app.add_handler(CommandHandler("brandear", brandear))
    app.add_handler(CommandHandler("cambiar_perfil", cambiar_perfil))
    app.add_handler(CommandHandler("cambiar_nombre", cambiar_nombre))
    app.add_handler(CommandHandler("cambiar_handle", cambiar_handle))
    app.add_handler(CommandHandler("codigo", comando_codigo))  # 2FA (TOTP)

    # Callbacks: menu (comandos.py) + cuentas/fotos (cuentas.py)
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu_"))
    app.add_handler(
        CallbackQueryHandler(cuentas_verificar_callback, pattern=r"^cuentas_verificar$")
    )
    app.add_handler(CallbackQueryHandler(brandear_callback, pattern=r"^brandeo:.+"))
    app.add_handler(CallbackQueryHandler(perfil_foto_callback, pattern=r"^perfil_foto:.+"))
    app.add_handler(
        CallbackQueryHandler(perfil_portada_callback, pattern=r"^perfil_portada:.+")
    )
    app.add_handler(
        CallbackQueryHandler(perfil_brandeo_callback, pattern=r"^perfil_brandeo:.+")
    )
    app.add_handler(
        CallbackQueryHandler(perfil_verificar_callback, pattern=r"^perfil_verificar:.+")
    )
    app.add_handler(
        CallbackQueryHandler(perfil_cancelar_callback, pattern=r"^perfil_cancelar$")
    )
    app.add_handler(CallbackQueryHandler(foto_manual_callback, pattern=r"^foto_manual:.+"))
    app.add_handler(
        CallbackQueryHandler(foto_cancelar_callback, pattern=r"^foto_cancelar$")
    )

    # Fotos enviadas como imagen de Telegram
    app.add_handler(MessageHandler(filters.PHOTO, foto_recibida))

    return app


def main() -> None:
    token = obtener_token()
    if not token or token in ("test_token_placeholder", "TU_TOKEN"):
        log.error("Falta TELEGRAM_BOT_TOKEN real en el .env; el bot no arranca.")
        raise SystemExit(1)
    app = construir_app(token)
    log.info(
        "Bot iniciado (PTB v21, polling). "
        "Comandos: /start /brandear /cambiar_perfil /codigo."
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
