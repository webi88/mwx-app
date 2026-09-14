"""Menu principal y navegacion (PTB v21, async).

Comandos: /start, /help, /status.
Callbacks del menu (ver bot/keyboards.py): menu_inicio, menu_cuentas,
menu_brandear, menu_perfil, menu_status, menu_ayuda.
"""

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import menu_principal, teclado_cuentas
from bot.middlewares import solo_autorizados


@solo_autorizados
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el menu principal con botones."""
    texto = (
        "🤖 *GestorRedes — control remoto*\n\n"
        "Comandos rápidos:\n"
        "• `/brandear <usuario> [min]` — foto+portada manual (navegador 5 min)\n"
        "• `/cambiar_perfil <usuario>` — nombre/@ + botones 📷/🖼️\n"
        "• `/cuentas` — listar · `/cuentas_verificar` — sesiones\n"
        "• `/ayuda` — guía completa · `/status` — estado\n"
    )
    await update.effective_message.reply_text(
        texto, parse_mode="Markdown", reply_markup=menu_principal()
    )


@solo_autorizados
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ayuda rapida."""
    await update.effective_message.reply_text(
        "🆘 *Ayuda rápida*\n\n"
        "`/start` menú · `/status` estado · `/ayuda` guía completa\n"
        "`/brandear <usuario> [min]` abre navegador manual para foto+portada.\n"
        "`/cambiar_perfil <usuario>` cambia nombre/@ y ofrece 📷/🖼️.",
        parse_mode="Markdown",
        reply_markup=menu_principal(),
    )


@solo_autorizados
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Estado del sistema (sin secretos)."""
    try:
        from core.config import settings

        headless = bool(getattr(settings, "headless", False))
        max_browsers = getattr(settings, "max_browsers", "?")
    except Exception:
        headless, max_browsers = "?", "?"
    try:
        import telegram as _tg

        ptb = getattr(_tg, "__version__", "?")
    except Exception:
        ptb = "?"
    await update.effective_message.reply_text(
        "📊 *Estado*\n\n"
        f"• PTB: {ptb} (v21 async)\n"
        f"• HEADLESS: {headless} (el brandeo fuerza ventana visible)\n"
        f"• MAX_BROWSERS: {max_browsers}\n"
        "• Brandeo: /brandear · Perfil/fotos: /cambiar_perfil",
        parse_mode="Markdown",
    )


@solo_autorizados
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Navegacion de los botones menu_* (keyboards.menu_principal)."""
    query = update.callback_query
    data = query.data or ""
    await query.answer()
    if data == "menu_cuentas":
        await query.edit_message_text(
            "📂 *Cuentas*\n\n"
            "`/cuentas` listar · `/cuentas_verificar` sesiones\n"
            "`/brandear <usuario>` foto+portada manual\n"
            "`/cambiar_perfil <usuario>` nombre/@ + 📷/🖼️",
            parse_mode="Markdown",
            reply_markup=teclado_cuentas(),
        )
    elif data == "menu_brandear":
        await query.edit_message_text(
            "🎨 *Brandear cuenta*\n\n"
            "Uso: `/brandear <usuario> [minutos]`\n"
            "Ej: `/brandear mi_cuenta 5`\n\n"
            "O escribe el comando con el usuario. El navegador se abre en el "
            "servidor/VPS (x.com/settings/profile).",
            parse_mode="Markdown",
            reply_markup=teclado_cuentas(),
        )
    elif data == "menu_perfil":
        await query.edit_message_text(
            "✏️ *Cambiar perfil*\n\n"
            "Uso: `/cambiar_perfil <usuario>`\n"
            "Después de nombre/@ verás 📷 foto perfil y 🖼️ portada:\n"
            "envía la imagen aquí o abre el navegador manual 5 min.",
            parse_mode="Markdown",
            reply_markup=teclado_cuentas(),
        )
    elif data == "menu_status":
        try:
            from core.config import settings

            headless = bool(getattr(settings, "headless", False))
            max_browsers = getattr(settings, "max_browsers", "?")
        except Exception:
            headless, max_browsers = "?", "?"
        await query.edit_message_text(
            f"📊 *Estado*\n\nHEADLESS: {headless}\nMAX_BROWSERS: {max_browsers}\n"
            "Brandeo: /brandear · Fotos: /cambiar_perfil",
            parse_mode="Markdown",
            reply_markup=menu_principal(),
        )
    elif data == "menu_ayuda":
        await query.edit_message_text(
            "❓ *Ayuda*\n\nEscribe /ayuda para la guía completa de comandos.",
            parse_mode="Markdown",
            reply_markup=menu_principal(),
        )
    else:  # menu_inicio y cualquier otro
        await query.edit_message_text(
            "🤖 *GestorRedes — control remoto*\n\nElige una opción:",
            parse_mode="Markdown",
            reply_markup=menu_principal(),
        )
