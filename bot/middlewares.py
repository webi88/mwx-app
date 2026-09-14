"""Autenticacion del bot por Telegram ID (solo admin/operadores).

Lee TELEGRAM_ADMIN_IDS del .env (ej: TELEGRAM_ADMIN_IDS=[5746247004]
o "123,456"). Sin IDs configurados bloquea todo y avisa que hay que
configurarlos. No contiene tokens reales.
"""

import functools
import os
import re

from telegram import Update
from telegram.ext import ContextTypes


def obtener_admin_ids() -> set:
    """Devuelve el set de Telegram IDs autorizados desde el entorno."""
    raw = (
        os.getenv("TELEGRAM_ADMIN_IDS", "")
        or os.getenv("TELEGRAM_ADMIN_ID", "")
        or os.getenv("ADMIN_IDS", "")
    )
    if not raw:
        # No leer core.config: ese modulo no define admin IDs.
        return set()
    ids = set()
    for num in re.findall(r"\d+", str(raw)):
        try:
            ids.add(int(num))
        except ValueError:
            continue
    return ids


def es_autorizado(user_id: int) -> bool:
    """True si user_id esta en TELEGRAM_ADMIN_IDS."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    return uid in obtener_admin_ids()


def solo_autorizados(func):
    """Decorador para handlers PTB v21: bloquea a quien no sea admin/operador."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user if update else None
        user_id = user.id if user and user.id is not None else None
        if user_id is None or not es_autorizado(user_id):
            msg = (
                "⛔ No autorizado. Tu Telegram ID no esta en la lista.\n"
                "Pide al admin que agregue tu ID en TELEGRAM_ADMIN_IDS del .env."
            )
            try:
                if update and update.effective_message:
                    await update.effective_message.reply_text(msg)
                elif update and update.callback_query:
                    await update.callback_query.answer(msg, show_alert=True)
            except Exception:
                pass
            return
        return await func(update, context, *args, **kwargs)

    return wrapper
