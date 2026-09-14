"""Guia de comandos del bot (PTB v21, async). Comando: /ayuda."""

from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import menu_principal
from bot.middlewares import solo_autorizados


@solo_autorizados
async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guia completa de comandos."""
    texto = (
        "📖 *Guía de comandos*\n\n"
        "*Menú*\n"
        "• `/start` menú principal · `/help` ayuda rápida\n"
        "• `/status` estado del sistema\n\n"
        "*Cuentas*\n"
        "• `/cuentas` listar\n"
        "• `/cuentas_verificar` sesiones (requiere Chrome)\n\n"
        "*🎨 Brandear (foto + portada manual)*\n"
        "• `/brandear <usuario> [minutos]` (default 5, máx 30)\n"
        "  Ej: `/brandear mi_cuenta 5`\n"
        "  Abre el navegador en el servidor/VPS: sube foto+portada a mano.\n"
        "  Al terminar guarda cookies. Sin sesión muestra el error real\n"
        "  (sin_sesion) y cómo importar el auth_token.\n\n"
        "*✏️ Cambiar nombre / @ / fotos*\n"
        "• `/cambiar_perfil <usuario>` — muestra 📷/🖼️/🎨\n"
        "• `/cambiar_nombre <usuario> | <Nuevo Nombre>`\n"
        "• `/cambiar_handle <usuario> <@nuevo> <password>`\n"
        "  Después de nombre/@ ofrece: 📷 foto perfil, 🖼️ portada\n"
        "  (envía la imagen aquí → se guarda en data/temp/ y se sube)\n"
        "  o 🎨 navegador manual 5 min.\n"
    )
    await update.effective_message.reply_text(
        texto, parse_mode="Markdown", reply_markup=menu_principal()
    )
