"""Gestion de cuentas + brandeo manual + fotos de perfil/portada (PTB v21).

Comandos:
  /cuentas                       Lista cuentas (resumen desde SQLite).
  /cuentas_verificar              Verifica sesiones (requiere Chrome; stub seguro).
  /brandear <usuario> [minutos]   Abre Chrome para subir foto+portada a mano.
  /cambiar_perfil <usuario> [| <nombre> [| <@handle> [password]]]
                                  Cambia nombre/@ y ofrece botones de fotos.
  /cambiar_nombre <usuario> | <nuevo nombre>
  /cambiar_handle <usuario> <@nuevo> <password>

Callbacks (ver bot/keyboards.py):
  cuentas_verificar, brandeo:<usuario>:<min>, perfil_foto/portada/brandeo/
  verificar/cancelar, foto_manual:<tipo>:<usuario>, foto_cancelar.
Fotos de Telegram: MessageHandler(filters.PHOTO) -> foto_recibida.

Solo llama a plataformas.twitter.selenium_bot (TwitterBot /
abrir_para_brandeo_manual / cambiar_foto_perfil / cambiar_foto_portada /
cambiar_perfil). No duplica logica Selenium.
"""

import asyncio
import os
import re

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from bot.keyboards import teclado_cambiar_perfil, teclado_cuentas, teclado_foto_modo
from bot.middlewares import solo_autorizados

BRANDEO_MIN_DEFAULT = 5
BRANDEO_MIN_MAX = 30
TIPOS_FOTO = ("perfil", "portada")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_brandear_args(args: list) -> tuple:
    """Devuelve (usuario, minutos). minutos se recorta a 1..BRANDEO_MIN_MAX."""
    if not args:
        return "", BRANDEO_MIN_DEFAULT
    usuario = (args[0] or "").strip().lstrip("@")
    minutos = BRANDEO_MIN_DEFAULT
    if len(args) >= 2:
        try:
            minutos = int(str(args[1]).strip())
        except (TypeError, ValueError):
            minutos = BRANDEO_MIN_DEFAULT
    if minutos <= 0:
        minutos = BRANDEO_MIN_DEFAULT
    if minutos > BRANDEO_MIN_MAX:
        minutos = BRANDEO_MIN_MAX
    return usuario, minutos


def _es_error_sesion(detalle: str) -> bool:
    """True si el error parece falta de sesion/Chrome (sin_sesion)."""
    texto = (detalle or "").lower()
    patrones = (
        "auth_token",
        "sin cookies",
        "sin sesion",
        "sin sesión",
        "no se pudo iniciar sesion",
        "no se pudo iniciar sesión",
        "sesion expirada",
        "sesión expirada",
        "sesion invalida",
        "requiere chrome",
        "chrome",
        "chromedriver",
        "webdriver",
        "session not created",
        "no such driver",
        "ct0",
    )
    return any(p in texto for p in patrones)


def _mensaje_sin_sesion(usuario: str, detalle: str) -> str:
    """Mensaje con el error real + instruccion para importar auth_token."""
    detalle = (detalle or "sin detalle").strip()
    return (
        f"⚠️ Sin sesión para @{usuario}.\n"
        f"Error real: {detalle[:400]}\n\n"
        "Para arreglarlo importa el auth_token de la cuenta:\n"
        "1) Guarda el auth_token en la BD (tabla cuentas, campo auth_token),\n"
        "   o deja las cookies en data/cookies/twitter/<usuario>.pkl\n"
        "2) Reintenta: /brandear <usuario> [minutos]\n"
        "Sin cookies/auth_token válido el navegador no puede abrirse."
    )


def _texto_uso_brandear() -> str:
    return (
        "🎨 *Brandear cuenta (foto + portada manual)*\n\n"
        "Uso: `/brandear <usuario> [minutos]`\n"
        "Ej: `/brandear mi_cuenta 5`\n\n"
        "Abre Chrome en el servidor/VPS en x.com/settings/profile y te da "
        "los minutos pedidos (default 5) para subir a mano la foto de perfil "
        "+ la portada. Al terminar guarda cookies (.pkl + BD) y cierra solo."
    )


def _texto_uso_cambiar_perfil() -> str:
    return (
        "✏️ *Cambiar nombre / @ / fotos*\n\n"
        "Uso:\n"
        "`/cambiar_perfil <usuario>` — muestra botones de fotos\n"
        "`/cambiar_perfil <usuario> | <Nuevo Nombre>` — cambia nombre\n"
        "`/cambiar_perfil <usuario> | <Nombre> | <@handle> [password]`\n"
        "`/cambiar_nombre <usuario> | <Nuevo Nombre>`\n"
        "`/cambiar_handle <usuario> <@nuevo> <password>`\n\n"
        "Después de nombre/@ el bot ofrece:\n"
        "📷 Cambiar foto perfil · 🖼️ Cambiar portada · 🎨 Brandeo manual."
    )


async def _ejecutar_brandeo(usuario: str, minutos: int) -> tuple:
    """Llama a abrir_para_brandeo_manual en un hilo. Devuelve (ok, error).

    Solo llamada a plataformas/ (no se edita plataformas/):
    equivale a plataformas.twitter.selenium_bot.abrir_para_brandeo_manual(
    usuario, minutos). Se usa la clase TwitterBot para conservar
    ultimo_error y mostrar el error real (sin_sesion) al operador.
    """
    # Import diferido: el bot debe importar aunque no haya Chrome.
    from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402

    tw = TwitterBot(usuario)
    try:
        ok = await asyncio.to_thread(tw.abrir_para_brandeo_manual, minutos)
    except Exception as e:  # nunca debe romper el bot
        logger.exception(f"[brandear] Error inesperado con @{usuario}: {e}")
        return False, f"{type(e).__name__}: {e}"
    error = (getattr(tw, "ultimo_error", "") or "").strip()
    try:
        tw.cerrar()
    except Exception:
        pass
    return bool(ok), error


async def _responder_brandeo_resultado(message, usuario: str, minutos: int, ok: bool, error: str) -> None:
    """Confirma cookies guardadas o muestra el error real de sesion."""
    if ok:
        await message.reply_text(
            f"✅ Brandeo de @{usuario} terminado.\n"
            f"Tuviste {minutos} min para subir foto+portada.\n"
            "Cookies guardadas (.pkl + BD).\n\n"
            "Verifica con:\n"
            f"`/cambiar_perfil {usuario}` → ✅ Verificar fotos manuales",
            parse_mode="Markdown",
        )
        return
    if _es_error_sesion(error):
        await message.reply_text(_mensaje_sin_sesion(usuario, error or "sin_sesion"))
        return
    await message.reply_text(
        f"❌ No se pudo brandear @{usuario}.\nError real: {(error or 'desconocido')[:500]}"
    )


def _cuenta_existe_en_bd(usuario: str) -> bool:
    """True si la cuenta esta en SQLite. Nunca lanza excepcion."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            reg = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            return reg is not None
    except Exception as e:
        logger.warning(f"No se pudo verificar @{usuario} en BD: {e}")
        return True  # no bloquear el flujo si la BD falla


# ---------------------------------------------------------------------------
# Comandos basicos de cuentas
# ---------------------------------------------------------------------------

@solo_autorizados
async def cuentas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista cuentas (resumen)."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            regs = db.query(Cuenta).order_by(Cuenta.usuario).limit(50).all()
            if not regs:
                await update.effective_message.reply_text(
                    "📂 No hay cuentas en la BD.\nImporta con migrar_cuentas.py o /cuentas_agregar."
                )
                return
            lineas = ["📂 *Cuentas (máx 50):*\n"]
            for r in regs:
                estado = getattr(r, "status", "?") or "?"
                lineas.append(f"• @{r.usuario} — {estado}")
            lineas.append("\nUsa /brandear <usuario> para foto+portada manual.")
            await update.effective_message.reply_text(
                "\n".join(lineas), parse_mode="Markdown", reply_markup=teclado_cuentas()
            )
    except Exception as e:
        logger.exception(f"Error listando cuentas: {e}")
        await update.effective_message.reply_text(
            f"❌ No se pudieron listar las cuentas: {type(e).__name__}: {e}"
        )


@solo_autorizados
async def cuentas_verificar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Comando /cuentas_verificar: requiere Chrome real con sesiones validas."""
    await update.effective_message.reply_text(
        "🔍 *Verificar suspendidas*\n\n"
        "Este chequeo abre Chrome cuenta por cuenta (lento, requiere VPS con "
        "Chrome + cookies válidas).\n"
        "De momento usa el dashboard (Cuentas → Sincronizar) o pide "
        "verificación de una cuenta con /brandear <usuario>.",
        parse_mode="Markdown",
        reply_markup=teclado_cuentas(),
    )


@solo_autorizados
async def cuentas_verificar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback del boton 'Verificar Suspendidas' (keyboards.teclado_cuentas)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔍 *Verificar suspendidas*\n\n"
        "Requiere Chrome real en el servidor. "
        "Para una cuenta puntual usa /brandear <usuario>.",
        parse_mode="Markdown",
        reply_markup=teclado_cuentas(),
    )


# ---------------------------------------------------------------------------
# /brandear
# ---------------------------------------------------------------------------

@solo_autorizados
async def brandear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brandear <usuario> [minutos]: navegador manual para foto+portada."""
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            _texto_uso_brandear(), parse_mode="Markdown"
        )
        return
    usuario, minutos = _parse_brandear_args(args)
    if not usuario:
        await update.effective_message.reply_text(
            _texto_uso_brandear(), parse_mode="Markdown"
        )
        return
    if not _cuenta_existe_en_bd(usuario):
        await update.effective_message.reply_text(
            f"⚠️ @{usuario} no está en la BD. Revisa el nombre con /cuentas."
        )
        return

    await update.effective_message.reply_text(
        f"🎨 Abriendo navegador para @{usuario}...\n"
        f"Tienes {minutos} minutos: sube la foto de perfil + la portada "
        f"en la ventana del servidor/VPS (x.com/settings/profile).\n"
        "NO cierres la ventana; se guardará sola al terminar."
    )
    ok, error = await _ejecutar_brandeo(usuario, minutos)
    await _responder_brandeo_resultado(update.effective_message, usuario, minutos, ok, error)


@solo_autorizados
async def brandear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback brandeo:<usuario>:<minutos> (keyboards.teclado_brandear)."""
    query = update.callback_query
    await query.answer("🎨 Abriendo navegador...")
    m = re.match(r"^brandeo:(.+):(\d+)$", query.data or "")
    if not m:
        await query.edit_message_text("❌ Callback de brandeo inválido.")
        return
    usuario = m.group(1).strip().lstrip("@")
    try:
        minutos = max(1, min(BRANDEO_MIN_MAX, int(m.group(2))))
    except ValueError:
        minutos = BRANDEO_MIN_DEFAULT
    await query.edit_message_text(
        f"🎨 Brandeo de @{usuario}: tienes {minutos} min en la ventana del "
        "servidor/VPS (sube foto+portada). Guardando al terminar..."
    )
    ok, error = await _ejecutar_brandeo(usuario, minutos)
    # query.message es el Message a responder
    await _responder_brandeo_resultado(query.message, usuario, minutos, ok, error)


# ---------------------------------------------------------------------------
# /cambiar_perfil + /cambiar_nombre + /cambiar_handle (flujo nombre/@ + fotos)
# ---------------------------------------------------------------------------

def _parse_cambiar_perfil_texto(usuario: str, resto: str) -> tuple:
    """Parsea 'Nombre | @handle password' -> (nombre, handle, password)."""
    nombre, handle, password = "", "", ""
    partes = [p.strip() for p in (resto or "").split("|")]
    if len(partes) >= 1 and partes[0]:
        nombre = partes[0]
    if len(partes) >= 2 and partes[1]:
        tokens = partes[1].split()
        if tokens:
            handle = tokens[0].lstrip("@").strip()
            if len(tokens) > 1:
                password = " ".join(tokens[1:]).strip()
    if len(partes) >= 3 and partes[2]:
        # tercer bloque: password explicita
        password = partes[2].strip()
    return nombre, handle, password


@solo_autorizados
async def cambiar_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cambiar_perfil: cambia nombre/@ y despues ofrece botones de fotos."""
    texto = (update.effective_message.text or "")
    # Quita "/cambiar_perfil" y posible "@BotName"
    cuerpo = re.sub(r"^/\w+(?:@\w+)?\s*", "", texto).strip()
    if not cuerpo:
        await update.effective_message.reply_text(
            _texto_uso_cambiar_perfil(), parse_mode="Markdown"
        )
        return
    # Formatos: "usuario" | "usuario | Nombre" | "usuario | Nombre | @handle pass"
    if "|" in cuerpo:
        primero, resto = cuerpo.split("|", 1)
        usuario = primero.strip().lstrip("@")
        nombre, handle, password = _parse_cambiar_perfil_texto(usuario, resto)
    else:
        tokens = cuerpo.split()
        usuario = (tokens[0] if tokens else "").lstrip("@")
        nombre, handle, password = "", "", ""
        if len(tokens) >= 2:
            # "/cambiar_perfil usuario Nuevo Nombre..." -> nombre = resto
            nombre = " ".join(tokens[1:]).strip()

    if not usuario:
        await update.effective_message.reply_text(
            _texto_uso_cambiar_perfil(), parse_mode="Markdown"
        )
        return

    if not nombre and not handle:
        # Sin cambios pedidos: muestra directamente el apartado de fotos.
        await update.effective_message.reply_text(
            f"✏️ Perfil de @{usuario}.\n"
            "Elige qué foto cambiar (puedes enviar la imagen aquí o abrir "
            "el navegador manual 5 min):",
            reply_markup=teclado_cambiar_perfil(usuario),
        )
        return

    await update.effective_message.reply_text(
        f"✏️ Cambiando perfil de @{usuario}... (requiere Chrome en el servidor)"
    )
    resultado = await _llamar_cambiar_perfil(
        usuario, nombre=nombre or None, handle=handle or None,
        password=password or None,
    )
    await _responder_cambio_perfil(update.effective_message, usuario, resultado)


@solo_autorizados
async def cambiar_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cambiar_nombre <usuario> | <nuevo nombre>: cambia nombre y ofrece fotos."""
    texto = (update.effective_message.text or "")
    cuerpo = re.sub(r"^/\w+(?:@\w+)?\s*", "", texto).strip()
    if "|" in cuerpo:
        izq, der = cuerpo.split("|", 1)
        usuario, nombre = izq.strip().lstrip("@"), der.strip()
    else:
        tokens = cuerpo.split()
        usuario = (tokens[0] if tokens else "").lstrip("@")
        nombre = " ".join(tokens[1:]).strip() if len(tokens) > 1 else ""
    if not usuario or not nombre:
        await update.effective_message.reply_text(
            "Uso: `/cambiar_nombre <usuario> | <Nuevo Nombre>`\n"
            "Ej: `/cambiar_nombre mi_cuenta | Ciudadanía Feliz`",
            parse_mode="Markdown",
        )
        return
    await update.effective_message.reply_text(f"✏️ Cambiando nombre de @{usuario}...")
    resultado = await _llamar_cambiar_perfil(usuario, nombre=nombre)
    await _responder_cambio_perfil(update.effective_message, usuario, resultado)


@solo_autorizados
async def cambiar_handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cambiar_handle <usuario> <@nuevo> <password>: cambia @ y ofrece fotos."""
    args = context.args or []
    if len(args) < 3:
        await update.effective_message.reply_text(
            "Uso: `/cambiar_handle <usuario> <@nuevo> <password>`\n"
            "Ej: `/cambiar_handle mi_cuenta @nuevo_handle MiPass123`",
            parse_mode="Markdown",
        )
        return
    usuario = (args[0] or "").lstrip("@").strip()
    handle = (args[1] or "").lstrip("@").strip()
    password = " ".join(args[2:]).strip()
    if not usuario or not handle or not password:
        await update.effective_message.reply_text(
            "Uso: `/cambiar_handle <usuario> <@nuevo> <password>`",
            parse_mode="Markdown",
        )
        return
    await update.effective_message.reply_text(f"✏️ Cambiando @ de @{usuario} a @{handle}...")
    resultado = await _llamar_cambiar_perfil(usuario, handle=handle, password=password)
    await _responder_cambio_perfil(update.effective_message, usuario, resultado)


async def _llamar_cambiar_perfil(usuario: str, **kwargs) -> dict:
    """Llama a TwitterBot.cambiar_perfil en un hilo. Nunca lanza excepcion."""
    from plataformas.twitter.selenium_bot import TwitterBot  # import diferido

    tw = TwitterBot(usuario)
    try:
        resultado = await asyncio.to_thread(tw.cambiar_perfil, **kwargs)
        if not isinstance(resultado, dict):
            return {"ok": False, "error": f"respuesta inesperada: {resultado!r}"}
        return resultado
    except Exception as e:
        logger.exception(f"[cambiar_perfil] Error con @{usuario}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            tw.cerrar()
        except Exception:
            pass


async def _responder_cambio_perfil(message, usuario: str, resultado: dict) -> None:
    """Informa nombre/@ y SIEMPRE ofrece el apartado de fotos."""
    ok = bool((resultado or {}).get("ok"))
    detalle = []
    for clave, etiqueta in (
        ("nombre", "Nombre"),
        ("handle", "@"),
        ("foto_perfil", "Foto perfil"),
        ("foto_portada", "Portada"),
    ):
        if (resultado or {}).get(clave):
            detalle.append(f"✅ {etiqueta}")
    error = ((resultado or {}).get("error") or "").strip()
    if ok:
        texto = (
            f"✅ Perfil de @{usuario} actualizado: {', '.join(detalle) or 'ok'}.\n\n"
            "Ahora las fotos (para hacerlo fácil):"
        )
    elif _es_error_sesion(error):
        texto = _mensaje_sin_sesion(usuario, error) + "\n\nCuando recuperes la sesión, usa los botones:"
    else:
        texto = (
            f"❌ No se pudo cambiar el perfil de @{usuario}.\n"
            f"Error real: {(error or 'desconocido')[:500]}\n\n"
            "Igual puedes intentar las fotos:"
        )
    await message.reply_text(texto, reply_markup=teclado_cambiar_perfil(usuario))


# ---------------------------------------------------------------------------
# Callbacks del apartado de fotos
# ---------------------------------------------------------------------------

@solo_autorizados
async def perfil_foto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton 📷 Cambiar foto perfil: pide foto de Telegram o modo manual."""
    query = update.callback_query
    m = re.match(r"^perfil_foto:(.+)$", query.data or "")
    if not m:
        await query.answer("Callback inválido", show_alert=True)
        return
    usuario = m.group(1).strip().lstrip("@")
    context.user_data["foto_espera"] = {"tipo": "perfil", "usuario": usuario}
    await query.answer()
    await query.edit_message_text(
        f"📷 *Foto de perfil para @{usuario}*\n\n"
        "Opción 1: envía la foto AQUÍ como imagen (la descargo a data/temp/ "
        "y la subo con cambiar_foto_perfil).\n"
        "Opción 2: abre el navegador manual 5 min y súbela tú.",
        parse_mode="Markdown",
        reply_markup=teclado_foto_modo(usuario, "perfil"),
    )


@solo_autorizados
async def perfil_portada_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton 🖼️ Cambiar portada: pide foto de Telegram o modo manual."""
    query = update.callback_query
    m = re.match(r"^perfil_portada:(.+)$", query.data or "")
    if not m:
        await query.answer("Callback inválido", show_alert=True)
        return
    usuario = m.group(1).strip().lstrip("@")
    context.user_data["foto_espera"] = {"tipo": "portada", "usuario": usuario}
    await query.answer()
    await query.edit_message_text(
        f"🖼️ *Portada para @{usuario}*\n\n"
        "Opción 1: envía la imagen AQUÍ (la descargo a data/temp/ "
        "y la subo con cambiar_foto_portada).\n"
        "Opción 2: abre el navegador manual 5 min y súbela tú.",
        parse_mode="Markdown",
        reply_markup=teclado_foto_modo(usuario, "portada"),
    )


@solo_autorizados
async def perfil_brandeo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton 🎨 Brandeo manual (5 min) desde el flujo cambiar_perfil."""
    query = update.callback_query
    m = re.match(r"^perfil_brandeo:(.+)$", query.data or "")
    if not m:
        await query.answer("Callback inválido", show_alert=True)
        return
    usuario = m.group(1).strip().lstrip("@")
    minutos = BRANDEO_MIN_DEFAULT
    await query.answer("🎨 Abriendo navegador...")
    await query.edit_message_text(
        f"🎨 Brandeo de @{usuario}: tienes {minutos} min en la ventana del "
        "servidor/VPS (sube foto+portada en x.com/settings/profile)."
    )
    ok, error = await _ejecutar_brandeo(usuario, minutos)
    await _responder_brandeo_resultado(query.message, usuario, minutos, ok, error)


@solo_autorizados
async def perfil_verificar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton ✅ Verificar fotos manuales: cambiar_perfil(verificar_fotos_manuales)."""
    query = update.callback_query
    m = re.match(r"^perfil_verificar:(.+)$", query.data or "")
    if not m:
        await query.answer("Callback inválido", show_alert=True)
        return
    usuario = m.group(1).strip().lstrip("@")
    await query.answer("🔍 Verificando fotos...")
    await query.edit_message_text(f"🔍 Verificando fotos manuales de @{usuario}...")
    # Reutiliza cambiar_perfil(foto_*, verificar_fotos_manuales) segun spec.
    resultado = await _llamar_cambiar_perfil(usuario, verificar_fotos_manuales=True)
    await _responder_cambio_perfil(query.message, usuario, resultado)


@solo_autorizados
async def perfil_cancelar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton ❌ Cancelar del apartado de fotos."""
    query = update.callback_query
    context.user_data.pop("foto_espera", None)
    await query.answer("Cancelado")
    await query.edit_message_text("❌ Operación de perfil cancelada.")


@solo_autorizados
async def foto_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton 🎨 modo manual por tipo: abrir_para_brandeo_manual 5 min + verificar."""
    query = update.callback_query
    m = re.match(r"^foto_manual:(perfil|portada):(.+)$", query.data or "")
    if not m:
        await query.answer("Callback inválido", show_alert=True)
        return
    tipo, usuario = m.group(1), m.group(2).strip().lstrip("@")
    minutos = BRANDEO_MIN_DEFAULT
    etiqueta = "perfil" if tipo == "perfil" else "portada"
    await query.answer("🎨 Abriendo navegador...")
    await query.edit_message_text(
        f"🎨 @{usuario}: tienes {minutos} min para subir la foto de {etiqueta} "
        "en la ventana del servidor/VPS. Al terminar verifico solo."
    )
    ok, error = await _ejecutar_brandeo(usuario, minutos)
    if not ok:
        await _responder_brandeo_resultado(query.message, usuario, minutos, ok, error)
        return
    # Brandeo ok: verifica esa foto (modo manual = imagen_path None).
    from plataformas.twitter.selenium_bot import TwitterBot  # import diferido

    tw = TwitterBot(usuario)
    try:
        if tipo == "perfil":
            verificado = await asyncio.to_thread(tw.cambiar_foto_perfil, None)
        else:
            verificado = await asyncio.to_thread(tw.cambiar_foto_portada, None)
        err = (getattr(tw, "ultimo_error", "") or "").strip()
    except Exception as e:
        verificado, err = False, f"{type(e).__name__}: {e}"
    finally:
        try:
            tw.cerrar()
        except Exception:
            pass
    if verificado:
        context.user_data.pop("foto_espera", None)
        await query.message.reply_text(
            f"✅ Foto de {etiqueta} de @{usuario} verificada y cookies guardadas.",
            reply_markup=teclado_cambiar_perfil(usuario),
        )
    elif _es_error_sesion(err):
        await query.message.reply_text(_mensaje_sin_sesion(usuario, err))
    else:
        await query.message.reply_text(
            f"⚠️ Terminaron los {minutos} min pero no se detectó foto de {etiqueta}.\n"
            f"Error: {(err or 'sin confirmación visual')[:400]}\n"
            "Súbela en x.com/settings/profile y pulsa ✅ Verificar fotos manuales.",
            reply_markup=teclado_cambiar_perfil(usuario),
        )


@solo_autorizados
async def foto_cancelar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Boton ❌ Cancelar del modo foto."""
    query = update.callback_query
    context.user_data.pop("foto_espera", None)
    await query.answer("Cancelado")
    await query.edit_message_text("❌ Subida de foto cancelada.")


# ---------------------------------------------------------------------------
# Foto recibida por Telegram -> data/temp/ -> cambiar_perfil(foto_*_path)
# ---------------------------------------------------------------------------

@solo_autorizados
async def foto_recibida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acepta la foto de Telegram y la sube como perfil/portada.

    Descarga el file_id a data/temp/ y reutiliza
    TwitterBot.cambiar_perfil(foto_perfil_path=... / foto_portada_path=...)
    (que internamente usa cambiar_foto_perfil/portada).
    """
    espera = context.user_data.get("foto_espera") or {}
    if not espera.get("usuario") or espera.get("tipo") not in TIPOS_FOTO:
        await update.effective_message.reply_text(
            "📷 No esperaba ninguna foto.\n"
            "Usa /cambiar_perfil <usuario> y pulsa 📷 o 🖼️ primero."
        )
        return
    usuario, tipo = espera["usuario"], espera["tipo"]
    etiqueta = "perfil" if tipo == "perfil" else "portada"
    fotos = update.effective_message.photo or []
    if not fotos:
        await update.effective_message.reply_text(
            "❌ Envíala como *foto/imagen*, no como archivo.", parse_mode="Markdown"
        )
        return

    # Descarga a data/temp/ (import diferido para no acoplar en import time).
    try:
        from core.config import resolver_ruta
        destino_dir = resolver_ruta("data/temp")
    except Exception:
        from pathlib import Path as _Path
        destino_dir = str(_Path(__file__).resolve().parent.parent.parent / "data" / "temp")
    os.makedirs(destino_dir, exist_ok=True)
    file_id = fotos[-1].file_id
    seguro = re.sub(r"[^A-Za-z0-9_-]+", "_", usuario)[:40]
    ruta = os.path.join(destino_dir, f"{seguro}_{tipo}_{file_id[:16]}.jpg")
    try:
        tg_file = await fotos[-1].get_file()
        await tg_file.download_to_drive(ruta)
    except Exception as e:
        logger.exception(f"No se pudo descargar la foto de Telegram: {e}")
        await update.effective_message.reply_text(
            f"❌ No se pudo descargar la foto: {type(e).__name__}: {e}"
        )
        return

    await update.effective_message.reply_text(
        f"📤 Foto recibida, subiendo como {etiqueta} de @{usuario}... "
        "(requiere Chrome en el servidor)"
    )
    # Reutiliza cambiar_perfil(foto_*_path, verificar...) segun spec.
    if tipo == "perfil":
        resultado = await _llamar_cambiar_perfil(usuario, foto_perfil_path=ruta)
    else:
        resultado = await _llamar_cambiar_perfil(usuario, foto_portada_path=ruta)
    context.user_data.pop("foto_espera", None)
    await _responder_cambio_perfil(update.effective_message, usuario, resultado)
