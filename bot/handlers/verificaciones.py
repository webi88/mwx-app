"""Verificacion 2FA de cuentas de X: codigo TOTP en tiempo real (PTB v21).

Comando: /codigo <usuario>
Lee la semilla guardada en Cuenta.totp_secret (nunca se muestra en la
respuesta) y calcula el codigo de 6 digitos de la ventana actual con
pyotp. Pensado para usarlo desde el grupo de trabajo.
"""

import time

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from bot.middlewares import solo_autorizados

VENTANA_TOTP = 30  # segundos de la ventana estandar TOTP


def _escapar_md(texto: str) -> str:
    """Escapa caracteres de Markdown clasico (PTB usa Markdown, no V2)."""
    for ch in ("_", "*", "`", "["):
        texto = texto.replace(ch, "\\" + ch)
    return texto


def _segundos_restantes() -> int:
    """Segundos que le quedan a la ventana TOTP actual (1..VENTANA_TOTP)."""
    try:
        return VENTANA_TOTP - int(time.time()) % VENTANA_TOTP
    except Exception:
        return VENTANA_TOTP


def _etiqueta_cuenta(handle_actual: str, usuario: str) -> str:
    """Nombre para mostrar: handle_actual si existe; si no, el usuario interno."""
    handle = (handle_actual or "").strip().lstrip("@")
    if handle:
        return handle
    return (usuario or "").strip().lstrip("@")


def _texto_uso() -> str:
    """Mensaje de uso cuando falta el usuario."""
    return (
        "⚠️ Uso: `/codigo usuario`\n\n"
        "Ejemplo: `/codigo mi_cuenta`\n"
        "Devuelve el código 2FA (TOTP) vigente de esa cuenta de X."
    )


@solo_autorizados
async def comando_codigo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /codigo <usuario>: genera el codigo 2FA (TOTP) de la cuenta."""
    args = list(getattr(context, "args", None) or [])
    if not args or not str(args[0] or "").strip().lstrip("@"):
        await update.effective_message.reply_text(_texto_uso(), parse_mode="Markdown")
        return
    usuario = str(args[0]).strip().lstrip("@").strip()

    # --- Buscar la cuenta (coincidencia case-insensitive) ---
    try:
        from sqlalchemy import func

        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = (
                db.query(Cuenta)
                .filter(func.lower(Cuenta.usuario) == usuario.lower())
                .first()
            )
            if cuenta is None:
                datos = None
            else:
                datos = {
                    "usuario": (getattr(cuenta, "usuario", "") or "").strip(),
                    "handle_actual": (
                        getattr(cuenta, "handle_actual", "") or ""
                    ).strip(),
                    "totp_secret": getattr(cuenta, "totp_secret", "") or "",
                }
    except Exception as e:
        logger.exception(f"Error consultando la BD para /codigo {usuario}: {e}")
        await update.effective_message.reply_text(
            f"❌ No se pudo consultar la base de datos: {type(e).__name__}: {e}"
        )
        return

    if datos is None:
        await update.effective_message.reply_text(
            f"❌ No encontré la cuenta `{_escapar_md(usuario)}` en la base de datos.\n"
            "Revisa el usuario con /cuentas.",
            parse_mode="Markdown",
        )
        return

    etiqueta = _etiqueta_cuenta(
        datos.get("handle_actual") or "", datos.get("usuario") or ""
    ) or usuario

    # --- Semilla TOTP (sin mostrar nunca el secreto) ---
    secreto = "".join(str(datos.get("totp_secret") or "").split())
    if not secreto:
        await update.effective_message.reply_text(
            f"❌ La cuenta `@{_escapar_md(etiqueta)}` no tiene TOTP registrado "
            "(columna totp_secret vacía).\n"
            "Importa la semilla 2FA de la cuenta para poder generar el código.",
            parse_mode="Markdown",
        )
        return

    try:
        import pyotp

        codigo = pyotp.TOTP(secreto).now()
    except Exception as e:
        logger.warning(f"Secreto TOTP invalido para @{etiqueta}: {e}")
        await update.effective_message.reply_text(
            f"❌ El secreto TOTP de `@{_escapar_md(etiqueta)}` no es válido: "
            f"{type(e).__name__}: {e}",
            parse_mode="Markdown",
        )
        return

    if not codigo:
        await update.effective_message.reply_text(
            f"❌ No se pudo generar el código 2FA de `@{_escapar_md(etiqueta)}`.",
            parse_mode="Markdown",
        )
        return

    await update.effective_message.reply_text(
        f"🔐 Código 2FA para @{_escapar_md(etiqueta)}\n\n"
        f"{codigo}\n\n"
        f"_(Cambia en {_segundos_restantes()}s)_",
        parse_mode="Markdown",
    )
