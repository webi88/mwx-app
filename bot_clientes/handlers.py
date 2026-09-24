"""Flujos del bot de clientes de X (PTB v21, async).

Bot INDEPENDIENTE del bot interno (`bot/`): aqui el cliente no tecnico
obtiene el codigo de verificacion 2FA (TOTP) generado al momento desde la
semilla guardada en `Cuenta.totp_secret`, cambia el nombre, la foto de perfil y
la portada de sus cuentas de X. Todo con BOTONES; el cliente solo necesita
escribir `/start`.

Reglas de diseno:
  - Mensajes cortos y sencillos ("como si fueran tontos"): sin terminos
    tecnicos, sin comandos, con ejemplos.
  - El bot funciona SOLO en el grupo de clientes (`clientes_store.chat_id()`,
    por defecto -1005538610567): en privado responde un aviso corto y en
    OTROS grupos se queda en silencio absoluto. Ya NO hay registro por
    Telegram ID ni `/asignar`/`/quitar`/`/clientes`.
  - Las cuentas son GLOBALES del grupo (`clientes_store.cuentas()`): cualquier
    miembro puede elegirlas y usar sus funciones.
  - Selenium NUNCA en el hilo del bot: `asyncio.to_thread(...)` con import
    perezoso de `plataformas.twitter.selenium_bot` y `bot.cerrar()` SIEMPRE en
    `finally`.
  - El codigo 2FA es SOLO TOTP (`pyotp.TOTP(semilla).now()`): el bot ya NO
    revisa correos ni usa `utils.lector_correo` (el correo lo tienen los
    propios clientes). Si no hay semilla, se pide ayuda al que dio la cuenta.
  - El secreto TOTP NUNCA se muestra.
  - Nada de credenciales en mensajes ni en `data/clientes_bot.json`.
  - Admin: `/nombre <usuario> <Nuevo Nombre>` actualiza SOLO el nombre
    registrado en la BD (sin Chrome); el nombre EN X lo cambia el cliente con
    el boton ✏️.

Callbacks (ver bot_clientes/keyboards.py):
  cli_*    menu/acciones            -> cli_callback
  cuenta_* selector de cuenta       -> cuenta_callback
  codigo_* codigo / otro codigo     -> codigo_callback
  nombre_* elegir / confirmar       -> nombre_callback
  foto_*   elegir / confirmar foto  -> foto_callback
Mensajes: texto -> texto_recibido (solo si ESE usuario tiene flujo pendiente);
fotos -> foto_recibida.

GRUPOS (el bot vive en un grupo con los clientes):
  - `context.user_data` de PTB v21 es POR USUARIO: los flujos de un miembro
    nunca se mezclan con los de otro (los callbacks validan la cuenta contra
    la lista del que pulsa el boton).
  - En grupo, cada respuesta antepone "👤 @fulano, " para que se sepa a quien
    le habla el bot (`mencion_grupo`/`con_mencion`).
  - Los mensajes de otros miembros sin flujo pendiente se ignoran en silencio
    (sin spam al grupo); el mensaje suelto y la RESPUESTA a un mensaje del bot
    se aceptan igual.
  - `bienvenida_grupo` (new_chat_members) y `bienvenida_miembro`
    (my_chat_member) saludan UNA sola vez al agregar el bot (anti-duplicado).
"""

from __future__ import annotations

import asyncio
import functools
import html
import os
import re
import time

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from bot.middlewares import obtener_admin_ids
from bot_clientes import clientes_store
from bot_clientes.keyboards import (
    menu_principal,
    teclado_acciones_cuenta,
    teclado_cancelar,
    teclado_codigo,
    teclado_codigo_fallo,
    teclado_confirmar_foto,
    teclado_confirmar_nombre,
    teclado_cuentas,
    teclado_lista_cuentas,
    teclado_reintentar_foto,
    teclado_reintentar_nombre,
)

# --------------------------------------------------------------------------- #
# Textos (sencillos, en segunda persona, con emojis)
# --------------------------------------------------------------------------- #

# Mensaje en privado: el bot es exclusivo del grupo de clientes.
TEXTO_SOLO_GRUPO = "ℹ️ Este bot funciona únicamente en el grupo de clientes."

TEXTO_SIN_CUENTAS = (
    "😕 Todavía no hay cuentas configuradas en el bot.\n\n"
    "Pide ayuda a la persona que te dio la cuenta."
)

TEXTO_FALLO_NOMBRE = (
    "❌ No se pudo cambiar. Inténtalo otra vez o pide ayuda a quien te dio la cuenta."
)

TEXTO_FALLO_FOTO = (
    "❌ No se pudo subir la foto. Inténtalo otra vez o pide ayuda a quien te dio la cuenta."
)

TEXTO_AYUDA = (
    "❓ Cómo usar este bot\n\n"
    "Este bot funciona únicamente en este grupo y todas las cuentas son del "
    "grupo: cualquier miembro puede elegir la cuenta y usar sus funciones.\n\n"
    "1️⃣ Toca un botón de abajo para decirme qué quieres hacer.\n"
    "2️⃣ Elige la cuenta (si hay varias, te muestro la lista).\n"
    "3️⃣ Sigue lo que dice el mensaje (escribir un nombre o enviar una foto).\n\n"
    "🔑 Quiero mi código: te doy el código de 6 números (2FA) que X te pide al entrar.\n"
    "✏️ Cambiar el nombre: escribes el nombre nuevo y yo lo cambio EN X.\n"
    "📸 Foto de perfil y 🖼️ Portada: me envías la imagen y yo la pongo.\n"
    "📋 Cuentas: te muestro las cuentas y su nombre registrado en el bot.\n\n"
    "Si algo falla:\n"
    "• Espera un momento y toca otra vez el botón (o 🔄 Intentar de nuevo).\n"
    "• Si sigue fallando, pide ayuda a la persona que te dio la cuenta.\n\n"
    "Escribe /cancelar para detener lo que estés haciendo.\n\n"
    "👥 En grupo: cada quien pulsa SUS botones y responde a MIS mensajes; "
    "el bot contesta con el nombre de quien preguntó.\n"
    "🔧 Equipo: /nombre <usuario> <Nuevo Nombre> actualiza el nombre REGISTRADO "
    "en el bot (sin abrir X); para cambiarlo EN X usa el botón ✏️."
)

TEXTO_BIENVENIDA = (
    "👋 ¡Hola! Soy el bot que te ayuda con las cuentas de X del grupo.\n\n"
    "Escribe /start y te muestro los botones: 🔑 código de verificación, "
    "✏️ cambiar el nombre, 📸 foto de perfil, 🖼️ portada y 📋 cuentas."
)

# Cuenta sin semilla 2FA en la BD: mensaje claro y amable, sin correos ni
# tecnicismos (el correo lo tienen los propios clientes).
TEXTO_SIN_TOTP = (
    "⚠️ Esta cuenta todavía no tiene configurado el código 2FA en el bot.\n\n"
    "Pide ayuda a la persona que te dio la cuenta."
)

# Flujos y sus botones del menu.
TIPO_POR_BOTON = {
    "cli_codigo": "codigo",
    "cli_nombre": "nombre",
    "cli_foto_perfil": "foto_perfil",
    "cli_foto_portada": "foto_portada",
}

# Codigo 2FA: SOLO TOTP desde Cuenta.totp_secret (ya no se usa el correo).
VENTANA_TOTP = 30

# Validacion del nombre visible.
NOMBRE_MIN = 2
NOMBRE_MAX = 50
_PATRON_NOMBRE = re.compile(r"^[\w .,'\-&()]+$", re.UNICODE)
_PATRON_LETRA = re.compile(r"[^\W\d_]", re.UNICODE)
_FRAGMENTOS_URL = ("http://", "https://", "www.", "t.me/", ".com/", ".net/")


# --------------------------------------------------------------------------- #
# Helpers puros
# --------------------------------------------------------------------------- #

def es_admin(telegram_id) -> bool:
    """True si el Telegram ID esta en TELEGRAM_ADMIN_IDS (helper del bot interno)."""
    try:
        uid = int(telegram_id)
    except (TypeError, ValueError):
        return False
    try:
        return uid in obtener_admin_ids()
    except Exception:
        return False


def validar_nombre(texto: str) -> tuple:
    """Valida el nombre nuevo. Devuelve (ok, nombre_limpio, mensaje_error)."""
    nombre = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not nombre:
        return False, "", "Escribe el nombre nuevo, por favor."
    if len(nombre) < NOMBRE_MIN:
        return False, "", "El nombre es muy corto: escribe al menos 2 letras."
    if len(nombre) > NOMBRE_MAX:
        return False, "", "El nombre es muy largo: máximo 50 letras."
    minusculas = nombre.lower()
    if any(fragmento in minusculas for fragmento in _FRAGMENTOS_URL):
        return False, "", "No uses links: solo el nombre, por ejemplo: María López."
    if "@" in nombre:
        return False, "", "No uses @: solo el nombre, por ejemplo: María López."
    if not _PATRON_NOMBRE.match(nombre):
        return False, "", "Usa solo letras y números, sin emojis ni símbolos raros."
    if not _PATRON_LETRA.search(nombre):
        return False, "", "El nombre debe tener al menos una letra."
    return True, nombre, ""


def generar_codigo_totp(secreto: str) -> tuple:
    """Codigo TOTP de 6 digitos desde la semilla. Devuelve (codigo, error)."""
    limpio = "".join(str(secreto or "").split())
    if not limpio:
        return "", "sin secreto TOTP"
    try:
        import pyotp

        return str(pyotp.TOTP(limpio).now()), ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def segundos_restantes_ventana(ventana: int = VENTANA_TOTP) -> int:
    """Segundos que le quedan a la ventana TOTP actual (1..ventana)."""
    try:
        return int(ventana) - int(time.time()) % int(ventana)
    except Exception:
        return int(ventana)


def _esc(texto) -> str:
    """Escapa HTML (solo se usa en mensajes con parse_mode HTML)."""
    return html.escape(str(texto or ""))


def texto_codigo(codigo: str, segundos=None) -> str:
    """Mensaje (HTML) con el codigo TOTP, pensado para clientes no tecnicos."""
    codigo = str(codigo or "").strip()
    lineas = [f"✅ Este es tu código: <b>{_esc(codigo)}</b>", ""]
    lineas.append(
        "Cópialo y pégalo en X. X te lo pide después de escribir tu usuario "
        "y tu contraseña."
    )
    try:
        segundos = int(segundos) if segundos else 0
    except (TypeError, ValueError):
        segundos = 0
    if segundos > 0:
        lineas.append(
            f"⏳ Vence en ~{segundos} segundos: si se vence, pide otro código."
        )
    else:
        lineas.append("⏳ Úsalo cuanto antes: si se vence, pide otro código.")
    lineas.append("🤫 No lo compartas con nadie.")
    return "\n".join(lineas)


def texto_menu(admin: bool, cuentas) -> str:
    """Saludo + menu, MUY simple (cuentas GLOBALES del grupo)."""
    cuentas = list(cuentas or [])
    lineas = ["👋 ¡Hola! Soy el bot de las cuentas de X del grupo.", ""]
    if not cuentas:
        lineas.append("Todavía no hay cuentas configuradas. Pide ayuda al equipo.")
    else:
        lineas.append(
            f"Hay {len(cuentas)} cuentas disponibles; elige la que quieras."
        )
    if admin:
        lineas.append("🔧 Equipo: /nombre <usuario> <Nuevo Nombre> actualiza el nombre registrado.")
    lineas += ["", "¿Qué quieres hacer? Toca un botón 👇"]
    return "\n".join(lineas)


def texto_pedir_nombre(usuario: str) -> str:
    return (
        f"✏️ Escríbeme el nombre nuevo para @{usuario}.\n\n"
        "Ejemplo: María López\n\n"
        "Solo el nombre: sin @ y sin links."
    )


def texto_pedir_foto(foto_tipo: str, usuario: str) -> str:
    etiqueta = "foto de perfil" if foto_tipo == "perfil" else "portada"
    return (
        f"📸 Envíame la {etiqueta} de @{usuario} como imagen.\n\n"
        "Mándala aquí como FOTO (no como archivo 📎).\n"
        "Después te pregunto si la uso."
    )


def _etiqueta_cuenta(datos, usuario: str) -> str:
    handle = str((datos or {}).get("handle_actual") or "").strip().lstrip("@")
    return handle or str(usuario or "").strip().lstrip("@")


def _resolver_cuenta(usuario, cuentas) -> str:
    """Devuelve el usuario EXACTO de la lista (case-insensitive) o ""."""
    buscado = str(usuario or "").strip().lstrip("@").strip().lower()
    if not buscado:
        return ""
    for cuenta in cuentas or []:
        if str(cuenta).strip().lstrip("@").strip().lower() == buscado:
            return str(cuenta).strip()
    return ""


def _limpiar(context) -> None:
    """Borra el estado de cualquier flujo en curso."""
    user_data = getattr(context, "user_data", None)
    if user_data is None:
        return
    for clave in ("cli_flujo", "cli_espera", "cli_nombre_pend", "cli_foto_pend"):
        try:
            user_data.pop(clave, None)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Grupos: menciones y aislamiento por miembro
# --------------------------------------------------------------------------- #

def _nombre_usuario(update) -> str:
    """'@usuario' si lo tiene; si no, su nombre visible; si no, 'amig@'."""
    user = getattr(update, "effective_user", None) if update else None
    username = str(getattr(user, "username", "") or "").strip().lstrip("@")
    if username:
        return f"@{username}"
    nombre = str(
        getattr(user, "full_name", "") or getattr(user, "first_name", "") or ""
    ).strip()
    return nombre or "amig@"


def es_grupo(update) -> bool:
    """True si el update viene de un grupo o supergrupo."""
    chat = getattr(update, "effective_chat", None) if update else None
    return str(getattr(chat, "type", "") or "").lower() in ("group", "supergroup")


def mencion_grupo(update) -> str:
    """'@fulano' en grupos; '' en privado (para anteponer '👤 @fulano, ')."""
    return _nombre_usuario(update) if es_grupo(update) else ""


def con_mencion(texto: str, mencion: str) -> str:
    """Antepone '👤 @fulano, ' para que en el grupo se sepa a quien responde."""
    if not mencion or not texto:
        return texto
    return f"👤 {mencion}, {texto}"


# --------------------------------------------------------------------------- #
# Acceso: guard por CHAT (solo el grupo de clientes)
# --------------------------------------------------------------------------- #

def _telegram_id(update) -> int | None:
    user = getattr(update, "effective_user", None) if update else None
    uid = getattr(user, "id", None)
    if uid is None:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


async def _avisar(update, texto: str) -> None:
    """Responde al update (alerta en callbacks, mensaje en texto). Nunca lanza."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer(str(texto)[:190], show_alert=True)
        except Exception:
            pass
        return
    message = getattr(update, "effective_message", None)
    if message is not None:
        try:
            await message.reply_text(texto)
        except Exception:
            pass


async def _guard_chat(update) -> bool:
    """True SOLO si el update viene del grupo de clientes permitido.

    - Privado: responde UNA vez "ℹ️ Este bot funciona únicamente en el grupo
      de clientes." (alerta si es callback).
    - Otro grupo/canal: silencio absoluto (en callbacks solo se quita el
      spinner, sin texto).
    - Grupo permitido: True.

    El modo deja de depender del Telegram ID: las cuentas son globales del
    grupo (`clientes_store.cuentas()`).
    """
    chat = getattr(update, "effective_chat", None) if update else None
    chat_id = getattr(chat, "id", None)
    if clientes_store.es_chat_permitido(chat_id):
        return True
    es_privado = str(getattr(chat, "type", "") or "").lower() == "private"
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            if es_privado:
                await query.answer(TEXTO_SOLO_GRUPO, show_alert=True)
            else:
                await query.answer()  # quita el spinner sin responder al grupo
        except Exception:
            pass
        return False
    if es_privado:
        await _avisar(update, TEXTO_SOLO_GRUPO)
    return False


def _cuentas() -> list:
    """Cuentas GLOBALES del grupo (lista limpia del registro del bot)."""
    return clientes_store.cuentas()


def _es_admin_update(update) -> bool:
    """True si quien interactua esta en TELEGRAM_ADMIN_IDS."""
    uid = _telegram_id(update)
    return uid is not None and es_admin(uid)


def _datos_cuenta(usuario: str) -> dict | None:
    """Datos de la cuenta en SQLite (sin credenciales sensibles) o None."""
    try:
        from sqlalchemy import func

        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = (
                db.query(Cuenta)
                .filter(func.lower(Cuenta.usuario) == str(usuario).lower())
                .first()
            )
            if cuenta is None:
                return None
            return {
                "usuario": str(getattr(cuenta, "usuario", "") or "").strip(),
                "handle_actual": str(getattr(cuenta, "handle_actual", "") or "").strip(),
                "nombre_mostrado": str(
                    getattr(cuenta, "nombre_mostrado", "") or ""
                ).strip(),
                "totp_secret": str(getattr(cuenta, "totp_secret", "") or ""),
                "activa": bool(getattr(cuenta, "activa", True)),
            }
    except Exception as e:
        logger.warning(f"No se pudo leer la cuenta {usuario} en la BD: {e}")
        return None


# --------------------------------------------------------------------------- #
# Codigo de verificacion 2FA (SOLO TOTP; el bot ya no revisa correos)
# --------------------------------------------------------------------------- #

async def _flujo_codigo(target, context, usuario: str, mencion: str = "") -> None:
    """Entrega el codigo TOTP de `usuario` desde `Cuenta.totp_secret`.

    El codigo se genera al momento con pyotp y el SECRETO NUNCA se muestra.
    Sin semilla: mensaje amable y boton para reintentar (por si el admin acaba
    de importarla). Ya NO existe la via de correo (`utils.lector_correo`).
    """
    datos = await asyncio.to_thread(_datos_cuenta, usuario)
    etiqueta = _etiqueta_cuenta(datos, usuario)
    if datos is None:
        await _responder(
            target,
            f"😕 No encontré la cuenta @{etiqueta}.\n\n"
            "Pide ayuda a quien te dio la cuenta.",
            mencion=mencion,
        )
        return

    secreto = str(datos.get("totp_secret") or "")
    codigo, error = generar_codigo_totp(secreto)
    if not codigo:
        if error and error != "sin secreto TOTP":
            logger.warning(f"Semilla TOTP inválida para @{usuario}: {error}")
        await _responder(
            target,
            TEXTO_SIN_TOTP,
            teclado_codigo_fallo(usuario),
            mencion=mencion,
        )
        return

    await _responder(
        target,
        texto_codigo(codigo, segundos_restantes_ventana()),
        teclado_codigo(usuario),
        html=True,
        mencion=mencion,
    )


# --------------------------------------------------------------------------- #
# Ejecucion en X (Selenium; hilo aparte + cierre garantizado)
# --------------------------------------------------------------------------- #

def _guardar_nombre_mostrado(usuario: str, nombre: str) -> None:
    """Persiste el nombre en la BD (para 📋 Mis cuentas). Nunca lanza."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is not None:
                cuenta.nombre_mostrado = nombre
    except Exception as e:
        logger.debug(f"No se pudo guardar nombre_mostrado de {usuario}: {e}")


async def ejecutar_cambiar_nombre(usuario: str, nombre: str) -> tuple:
    """Cambia el nombre en X con TwitterBot. Devuelve (ok, error_real)."""
    try:
        from plataformas.twitter.selenium_bot import TwitterBot
    except Exception as e:
        return False, f"modulo no disponible: {type(e).__name__}: {e}"
    bot = TwitterBot(usuario)
    try:
        ok = await asyncio.to_thread(bot.cambiar_nombre, nombre)
        error = (getattr(bot, "ultimo_error", "") or "").strip()
        if ok:
            _guardar_nombre_mostrado(usuario, nombre)
        return bool(ok), error
    except Exception as e:
        logger.exception(f"Error cambiando el nombre de @{usuario}: {e}")
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            bot.cerrar()
        except Exception:
            pass


async def ejecutar_cambiar_foto(usuario: str, foto_tipo: str, ruta: str) -> tuple:
    """Sube la foto de perfil/portada en X. Devuelve (ok, error_real)."""
    try:
        from plataformas.twitter.selenium_bot import TwitterBot
    except Exception as e:
        return False, f"modulo no disponible: {type(e).__name__}: {e}"
    bot = TwitterBot(usuario)
    try:
        if str(foto_tipo).strip().lower() == "portada":
            ok = await asyncio.to_thread(bot.cambiar_foto_portada, ruta)
        else:
            ok = await asyncio.to_thread(bot.cambiar_foto_perfil, ruta)
        error = (getattr(bot, "ultimo_error", "") or "").strip()
        return bool(ok), error
    except Exception as e:
        logger.exception(f"Error subiendo {foto_tipo} de @{usuario}: {e}")
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            bot.cerrar()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Utilidades de mensajes
# --------------------------------------------------------------------------- #

async def _responder(target, texto: str, markup=None, html: bool = False, mencion: str = ""):
    """Edita el mensaje objetivo si se puede; si no, responde uno nuevo.

    `mencion` (solo grupos) antepone "👤 @fulano, " para que se sepa a quien
    responde el bot dentro del grupo.
    """
    if mencion:
        texto = con_mencion(texto, mencion)
    kwargs = {}
    if markup is not None:
        kwargs["reply_markup"] = markup
    if html:
        kwargs["parse_mode"] = "HTML"
    for metodo in ("edit_text", "reply_text"):
        funcion = getattr(target, metodo, None)
        if funcion is None:
            continue
        try:
            return await funcion(texto, **kwargs)
        except Exception:
            continue
    return None


def _data_temp() -> str:
    """Carpeta `data/temp/` (para las imagenes que manda el cliente)."""
    try:
        from core.config import resolver_ruta

        destino = resolver_ruta("data/temp")
    except Exception:
        raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        destino = os.path.join(raiz, "data", "temp")
    os.makedirs(destino, exist_ok=True)
    return destino


async def _descargar_foto(message, foto, usuario: str, foto_tipo: str) -> tuple:
    """Descarga la imagen de Telegram a data/temp/. Devuelve (ruta, error)."""
    try:
        destino_dir = _data_temp()
    except Exception as e:
        return "", f"no hay carpeta temporal: {type(e).__name__}: {e}"
    seguro = re.sub(r"[^A-Za-z0-9_-]+", "_", str(usuario))[:40] or "cuenta"
    ruta = os.path.join(
        destino_dir, f"cli_{seguro}_{foto_tipo}_{int(time.time() * 1000)}.jpg"
    )
    try:
        archivo = await foto.get_file()
        await archivo.download_to_drive(ruta)
        return ruta, ""
    except Exception as e:
        logger.warning(f"No se pudo descargar la foto de Telegram: {e}")
        return "", f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Seleccion de cuenta / inicio de flujos
# --------------------------------------------------------------------------- #

async def _iniciar_flujo(
    target, context, tipo: str, usuario: str, editar: bool = True, mencion: str = ""
) -> None:
    """Arranca el flujo pedido con la cuenta ya elegida."""
    if tipo == "codigo":
        await _flujo_codigo(target, context, usuario, mencion=mencion)
        return
    if tipo == "nombre":
        context.user_data["cli_espera"] = {"tipo": "nombre", "usuario": usuario}
        texto = texto_pedir_nombre(usuario)
        markup = teclado_cancelar()
    else:
        foto_tipo = "perfil" if tipo == "foto_perfil" else "portada"
        context.user_data["cli_espera"] = {
            "tipo": "foto",
            "foto_tipo": foto_tipo,
            "usuario": usuario,
        }
        texto = texto_pedir_foto(foto_tipo, usuario)
        markup = teclado_cancelar()
    if editar:
        await _responder(target, texto, markup, mencion=mencion)
    else:
        try:
            await target.reply_text(con_mencion(texto, mencion), reply_markup=markup)
        except Exception:
            await _responder(target, texto, markup, mencion=mencion)


async def _elegir_cuenta_o_iniciar(query, context, tipo: str, mencion: str = "") -> None:
    """Con 1 cuenta va directo; con varias muestra un boton por cuenta."""
    cuentas = _cuentas()
    if not cuentas:
        await _responder(query.message, TEXTO_SIN_CUENTAS, mencion=mencion)
        return
    if len(cuentas) == 1:
        await _iniciar_flujo(
            query.message, context, tipo, cuentas[0], editar=True, mencion=mencion
        )
        return
    context.user_data["cli_flujo"] = tipo
    context.user_data.pop("cli_espera", None)
    await _responder(
        query.message,
        "¿Con cuál cuenta quieres hacerlo?",
        teclado_cuentas(cuentas),
        mencion=mencion,
    )


async def _mostrar_cuentas(query, mencion: str = "") -> None:
    """Lista las cuentas del grupo con su @ real y su nombre REGISTRADO en la BD."""
    cuentas = _cuentas()
    if not cuentas:
        await _responder(query.message, TEXTO_SIN_CUENTAS, menu_principal(), mencion=mencion)
        return
    lineas = ["📋 Cuentas del grupo:", ""]
    for indice, usuario in enumerate(cuentas, 1):
        datos = await asyncio.to_thread(_datos_cuenta, usuario)
        etiqueta = _etiqueta_cuenta(datos, usuario)
        nombre = str((datos or {}).get("nombre_mostrado") or "").strip()
        linea = f"{indice}. @{etiqueta}"
        if nombre:
            linea += f" — «{nombre}»"
        lineas.append(linea)
    lineas += ["", "Toca una cuenta para hacer algo con ella 👇"]
    await _responder(
        query.message,
        "\n".join(lineas),
        teclado_lista_cuentas(cuentas[:10]),
        mencion=mencion,
    )


async def _pedir_nombre(target, context, usuario: str, mencion: str = "") -> None:
    """Pide el nombre nuevo por texto."""
    context.user_data["cli_espera"] = {"tipo": "nombre", "usuario": usuario}
    context.user_data.pop("cli_nombre_pend", None)
    await _responder(
        target, texto_pedir_nombre(usuario), teclado_cancelar(), mencion=mencion
    )


async def _pedir_foto(
    target, context, usuario: str, foto_tipo: str, mencion: str = ""
) -> None:
    """Pide la imagen por Telegram."""
    context.user_data["cli_espera"] = {
        "tipo": "foto",
        "foto_tipo": foto_tipo,
        "usuario": usuario,
    }
    context.user_data.pop("cli_foto_pend", None)
    await _responder(
        target, texto_pedir_foto(foto_tipo, usuario), teclado_cancelar(), mencion=mencion
    )


# --------------------------------------------------------------------------- #
# Comandos publicos
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start: saludo + menu de botones (lo unico que el cliente necesita)."""
    if not await _guard_chat(update):
        return
    _limpiar(context)
    await update.effective_message.reply_text(
        con_mencion(texto_menu(_es_admin_update(update), _cuentas()), mencion_grupo(update)),
        reply_markup=menu_principal(),
    )


async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guia sencilla de cada boton y que hacer si algo falla."""
    if not await _guard_chat(update):
        return
    _limpiar(context)
    await update.effective_message.reply_text(
        con_mencion(TEXTO_AYUDA, mencion_grupo(update)), reply_markup=menu_principal()
    )


async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancelar: detiene cualquier flujo en curso y vuelve al menu."""
    if not await _guard_chat(update):
        return
    _limpiar(context)
    await update.effective_message.reply_text(
        con_mencion(
            "Listo, cancelado. ✅\n\n¿Qué quieres hacer ahora? Toca un botón 👇",
            mencion_grupo(update),
        ),
        reply_markup=menu_principal(),
    )


# --------------------------------------------------------------------------- #
# Callbacks del menu (`cli_*`)
# --------------------------------------------------------------------------- #

async def cli_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu principal y acciones: cli_menu, cli_codigo, cli_nombre, ..."""
    query = update.callback_query
    if not await _guard_chat(update):
        return
    data = (query.data or "").strip()
    mencion = mencion_grupo(update)
    await query.answer()

    if data.startswith("cli_cuenta_"):
        usuario = _resolver_cuenta(data[len("cli_cuenta_"):], _cuentas())
        if not usuario:
            try:
                await query.answer("Esa cuenta ya no está disponible.", show_alert=True)
            except Exception:
                pass
            return
        _limpiar(context)
        await _responder(
            query.message,
            f"¿Qué quieres hacer con @{usuario}?",
            teclado_acciones_cuenta(usuario),
            mencion=mencion,
        )
        return

    if data == "cli_menu":
        _limpiar(context)
        await _responder(
            query.message,
            texto_menu(_es_admin_update(update), _cuentas()),
            menu_principal(),
            mencion=mencion,
        )
        return
    if data == "cli_ayuda":
        _limpiar(context)
        await _responder(query.message, TEXTO_AYUDA, menu_principal(), mencion=mencion)
        return
    if data == "cli_cancelar":
        _limpiar(context)
        await _responder(
            query.message,
            "Listo, cancelado. ✅\n\n¿Qué quieres hacer ahora? Toca un botón 👇",
            menu_principal(),
            mencion=mencion,
        )
        return
    if data == "cli_cuentas":
        _limpiar(context)
        await _mostrar_cuentas(query, mencion=mencion)
        return

    tipo = TIPO_POR_BOTON.get(data)
    if tipo:
        _limpiar(context)
        await _elegir_cuenta_o_iniciar(query, context, tipo, mencion=mencion)


async def cuenta_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Selector de cuenta (`cuenta_<usuario>`) segun el flujo pendiente."""
    query = update.callback_query
    if not await _guard_chat(update):
        return
    mencion = mencion_grupo(update)
    usuario = _resolver_cuenta((query.data or "")[len("cuenta_"):], _cuentas())
    if not usuario:
        await query.answer("Esa cuenta ya no está disponible.", show_alert=True)
        return
    tipo = str(context.user_data.get("cli_flujo") or "").strip()
    if tipo not in ("codigo", "nombre", "foto_perfil", "foto_portada"):
        await query.answer("Primero elige qué quieres hacer 🙂", show_alert=True)
        await _responder(
            query.message,
            "¿Qué quieres hacer? Toca un botón 👇",
            menu_principal(),
            mencion=mencion,
        )
        return
    await query.answer()
    context.user_data.pop("cli_flujo", None)
    await _iniciar_flujo(
        query.message, context, tipo, usuario, editar=True, mencion=mencion
    )


async def codigo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Codigo de verificacion y reintentos (`codigo_<usuario>`)."""
    query = update.callback_query
    if not await _guard_chat(update):
        return
    mencion = mencion_grupo(update)
    usuario = _resolver_cuenta((query.data or "")[len("codigo_"):], _cuentas())
    if not usuario:
        await query.answer("Esa cuenta ya no está disponible.", show_alert=True)
        return
    await query.answer()
    _limpiar(context)
    await _flujo_codigo(query.message, context, usuario, mencion=mencion)


async def nombre_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Nombre: elegir cuenta (`nombre_<u>`) o confirmar (`nombre_si/no_<u>`)."""
    query = update.callback_query
    if not await _guard_chat(update):
        return
    data = (query.data or "").strip()
    cuentas = _cuentas()
    mencion = mencion_grupo(update)

    for prefijo in ("nombre_si_", "nombre_no_"):
        if not data.startswith(prefijo):
            continue
        usuario = _resolver_cuenta(data[len(prefijo):], cuentas)
        pendiente = context.user_data.get("cli_nombre_pend") or {}
        if (
            not usuario
            or pendiente.get("usuario") != usuario
            or not pendiente.get("nombre")
        ):
            await query.answer(
                "Este botón ya venció. Toca otra vez el menú 🙂", show_alert=True
            )
            return
        await query.answer()
        if prefijo == "nombre_no_":
            context.user_data.pop("cli_nombre_pend", None)
            await _responder(
                query.message,
                "Está bien, no cambié nada. 👍",
                menu_principal(),
                mencion=mencion,
            )
            return
        nombre = str(pendiente.get("nombre"))
        await _responder(
            query.message,
            f"⏳ Cambiando el nombre de @{usuario} a «{nombre}».\n\n"
            "Esto tarda 1-2 minutos. No cierres el chat, por favor...",
            mencion=mencion,
        )
        ok, error = await ejecutar_cambiar_nombre(usuario, nombre)
        context.user_data.pop("cli_nombre_pend", None)
        if ok:
            await query.message.reply_text(
                con_mencion(
                    f"✅ ¡Listo! Ahora el nombre de @{usuario} es «{nombre}».",
                    mencion,
                ),
                reply_markup=menu_principal(),
            )
        else:
            logger.warning(f"No se pudo cambiar el nombre de @{usuario}: {error}")
            await query.message.reply_text(
                con_mencion(TEXTO_FALLO_NOMBRE, mencion),
                reply_markup=teclado_reintentar_nombre(usuario),
            )
        return

    if data.startswith("nombre_"):
        usuario = _resolver_cuenta(data[len("nombre_"):], cuentas)
        if not usuario:
            await query.answer("Esa cuenta ya no está disponible.", show_alert=True)
            return
        await query.answer()
        await _pedir_nombre(query.message, context, usuario, mencion=mencion)
        return

    await query.answer()


async def foto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fotos: elegir cuenta/confirmar (`foto_perfil_`, `foto_si_`, ...)."""
    query = update.callback_query
    if not await _guard_chat(update):
        return
    data = (query.data or "").strip()
    cuentas = _cuentas()
    mencion = mencion_grupo(update)

    for accion in ("foto_si_", "foto_no_", "foto_otra_"):
        if not data.startswith(accion):
            continue
        resto = data[len(accion):]
        foto_tipo, _, usuario_raw = resto.partition("_")
        foto_tipo = "portada" if foto_tipo.strip().lower() == "portada" else "perfil"
        usuario = _resolver_cuenta(usuario_raw, cuentas)
        pendiente = context.user_data.get("cli_foto_pend") or {}
        if (
            not usuario
            or pendiente.get("usuario") != usuario
            or pendiente.get("tipo") != foto_tipo
        ):
            await query.answer(
                "Este botón ya venció. Toca otra vez el menú 🙂", show_alert=True
            )
            return
        await query.answer()
        etiqueta = "foto de perfil" if foto_tipo == "perfil" else "portada"
        if accion == "foto_no_":
            context.user_data.pop("cli_foto_pend", None)
            _limpiar(context)
            await _responder(
                query.message,
                "Está bien, no cambié nada. 👍",
                menu_principal(),
                mencion=mencion,
            )
            return
        if accion == "foto_otra_":
            context.user_data.pop("cli_foto_pend", None)
            await _pedir_foto(query.message, context, usuario, foto_tipo, mencion=mencion)
            return
        ruta = str(pendiente.get("ruta") or "")
        context.user_data.pop("cli_espera", None)
        await _responder(
            query.message,
            f"⏳ Subiendo tu nueva {etiqueta} de @{usuario}.\n\n"
            "Esto tarda 1-2 minutos. No cierres el chat, por favor...",
            mencion=mencion,
        )
        ok, error = await ejecutar_cambiar_foto(usuario, foto_tipo, ruta)
        context.user_data.pop("cli_foto_pend", None)
        if ok:
            try:
                if ruta and os.path.isfile(ruta):
                    os.remove(ruta)
            except OSError:
                pass
            await query.message.reply_text(
                con_mencion(f"✅ ¡Listo! Ya cambié tu {etiqueta}.", mencion),
                reply_markup=menu_principal(),
            )
        else:
            logger.warning(f"No se pudo subir {foto_tipo} de @{usuario}: {error}")
            await query.message.reply_text(
                con_mencion(TEXTO_FALLO_FOTO, mencion),
                reply_markup=teclado_reintentar_foto(foto_tipo, usuario),
            )
        return

    if data.startswith("foto_perfil_") or data.startswith("foto_portada_"):
        foto_tipo = "perfil" if data.startswith("foto_perfil_") else "portada"
        prefijo = "foto_perfil_" if foto_tipo == "perfil" else "foto_portada_"
        usuario = _resolver_cuenta(data[len(prefijo):], cuentas)
        if not usuario:
            await query.answer("Esa cuenta ya no está disponible.", show_alert=True)
            return
        await query.answer()
        await _pedir_foto(query.message, context, usuario, foto_tipo, mencion=mencion)
        return

    await query.answer()


# --------------------------------------------------------------------------- #
# Mensajes de texto (solo con flujo pendiente) y fotos
# --------------------------------------------------------------------------- #

async def texto_recibido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texto libre: SOLO se procesa si ESE usuario tiene un flujo pendiente.

    En grupos (con privacy ON alcanza con responder a un mensaje del bot; con
    privacy OFF se reciben todos): los mensajes de otros miembros sin flujo se
    IGNORAN en silencio (sin spam al grupo) porque `context.user_data` es por
    usuario en PTB v21.
    """
    espera = context.user_data.get("cli_espera") or {}
    if espera.get("tipo") != "nombre":
        return
    if not await _guard_chat(update):
        return
    mencion = mencion_grupo(update)
    usuario = _resolver_cuenta(espera.get("usuario") or "", _cuentas())
    if not usuario:
        _limpiar(context)
        await update.effective_message.reply_text(
            con_mencion(
                "😕 La cuenta ya no está disponible. Vuelve a empezar con /start.",
                mencion,
            )
        )
        return
    texto = str(getattr(update.effective_message, "text", "") or "")
    ok, nombre, error = validar_nombre(texto)
    if not ok:
        await update.effective_message.reply_text(
            con_mencion(f"🤔 {error}\n\nEscríbelo otra vez, por favor.", mencion)
        )
        return
    context.user_data.pop("cli_espera", None)
    context.user_data["cli_nombre_pend"] = {"usuario": usuario, "nombre": nombre}
    await update.effective_message.reply_text(
        con_mencion(
            f"Voy a cambiar el nombre de @{usuario} a:\n\n«{nombre}»\n\n¿Lo hago?",
            mencion,
        ),
        reply_markup=teclado_confirmar_nombre(usuario),
    )


async def foto_recibida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Foto de Telegram: la guarda en data/temp y pide confirmacion."""
    espera = context.user_data.get("cli_espera") or {}
    mencion = mencion_grupo(update)
    if espera.get("tipo") != "foto":
        if not await _guard_chat(update):
            return
        await update.effective_message.reply_text(
            con_mencion(
                "📸 No esperaba ninguna foto.\n\n"
                "Primero toca el botón 📸 o 🖼️ y después mándame la imagen. 🙂",
                mencion,
            )
        )
        return
    if not await _guard_chat(update):
        return
    usuario = _resolver_cuenta(espera.get("usuario") or "", _cuentas())
    foto_tipo = "portada" if espera.get("foto_tipo") == "portada" else "perfil"
    if not usuario:
        _limpiar(context)
        await update.effective_message.reply_text(
            con_mencion(
                "😕 La cuenta ya no está disponible. Vuelve a empezar con /start.",
                mencion,
            )
        )
        return
    fotos = list(getattr(update.effective_message, "photo", []) or [])
    if not fotos:
        await update.effective_message.reply_text(
            con_mencion("📸 Mándala como FOTO (no como archivo 📎), por favor.", mencion)
        )
        return
    ruta, error = await _descargar_foto(
        update.effective_message, fotos[-1], usuario, foto_tipo
    )
    if not ruta:
        logger.warning(f"No se pudo bajar la foto de @{usuario}: {error}")
        await update.effective_message.reply_text(
            con_mencion(
                "❌ No se pudo recibir la foto. Inténtalo otra vez, por favor.",
                mencion,
            )
        )
        return
    context.user_data.pop("cli_espera", None)
    context.user_data["cli_foto_pend"] = {
        "usuario": usuario,
        "tipo": foto_tipo,
        "ruta": ruta,
    }
    etiqueta = "foto de perfil" if foto_tipo == "perfil" else "portada"
    pregunta = con_mencion(
        f"¿Uso esta foto como tu nueva {etiqueta} de @{usuario}?", mencion
    )
    try:
        await update.effective_message.reply_photo(
            photo=fotos[-1].file_id,
            caption=pregunta,
            reply_markup=teclado_confirmar_foto(foto_tipo, usuario),
        )
    except Exception as e:
        logger.warning(f"No se pudo reenviar la vista previa a @{usuario}: {e}")
        await update.effective_message.reply_text(
            pregunta,
            reply_markup=teclado_confirmar_foto(foto_tipo, usuario),
        )


# --------------------------------------------------------------------------- #
# Grupos: bienvenida al agregar el bot
# --------------------------------------------------------------------------- #

# Anti-duplicado: al agregar el bot, Telegram manda DOS updates (el mensaje de
# `new_chat_members` y el `my_chat_member`); solo se saluda UNA vez por chat.
_BIENVENIDAS: dict = {}
BIENVENIDA_VENTANA_SEG = 60.0


def _bienvenida_reciente(chat_id, ventana: float = BIENVENIDA_VENTANA_SEG) -> bool:
    """True si ya se saludo a ese chat hace menos de `ventana` segundos."""
    if chat_id is None:
        return True
    try:
        ahora = time.monotonic()
    except Exception:
        ahora = time.time()
    previo = _BIENVENIDAS.get(chat_id)
    if previo is not None and (ahora - previo) < float(ventana):
        return True
    _BIENVENIDAS[chat_id] = ahora
    return False


async def _enviar_bienvenida(update, context, chat_id) -> None:
    """Manda el saludo por el mensaje del update o, si no, directo al chat."""
    mensaje = getattr(update, "effective_message", None)
    if mensaje is not None:
        try:
            await mensaje.reply_text(TEXTO_BIENVENIDA)
            return
        except Exception:
            pass
    try:
        await context.bot.send_message(chat_id=chat_id, text=TEXTO_BIENVENIDA)
    except Exception as e:
        logger.debug(f"No se pudo saludar al chat {chat_id}: {e}")


def _bot_entre_los_nuevos(update, context) -> bool:
    """True si el bot aparece en `new_chat_members` (o no hay bot que comparar)."""
    mensaje = getattr(update, "effective_message", None)
    nuevos = list(getattr(mensaje, "new_chat_members", []) or []) if mensaje else []
    if not nuevos:
        return True
    bot_id = getattr(getattr(context, "bot", None), "id", None)
    if bot_id is None:
        return True
    return any(getattr(usuario, "id", None) == bot_id for usuario in nuevos)


async def bienvenida_grupo(update, context) -> None:
    """Al AGREGAR el bot a un grupo: instrucciones cortas (una sola vez).

    Se registra con `MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS)`.
    Si agregan a otra persona (no al bot), no dice nada. En un grupo que NO
    sea el de clientes se queda en silencio.
    """
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or not clientes_store.es_chat_permitido(chat_id):
        return
    if not _bot_entre_los_nuevos(update, context):
        return
    if _bienvenida_reciente(chat_id):
        return
    await _enviar_bienvenida(update, context, chat_id)


_ESTADOS_FUERA = {"left", "kicked"}
_ESTADOS_DENTRO = {"member", "administrator", "creator", "restricted"}


async def bienvenida_miembro(update, context) -> None:
    """`ChatMemberHandler(MY_CHAT_MEMBER)`: saluda cuando el bot RECIEN entra.

    Complementa a `bienvenida_grupo` (Telegram manda ambos updates al agregar
    el bot); `_bienvenida_reciente` evita el saludo doble. En otros grupos,
    silencio.
    """
    miembro = getattr(update, "my_chat_member", None)
    if miembro is None:
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or not clientes_store.es_chat_permitido(chat_id):
        return
    viejo = str(getattr(getattr(miembro, "old_chat_member", None), "status", "") or "")
    nuevo = str(getattr(getattr(miembro, "new_chat_member", None), "status", "") or "")
    if viejo not in _ESTADOS_FUERA or nuevo not in _ESTADOS_DENTRO:
        return
    if _bienvenida_reciente(chat_id):
        return
    await _enviar_bienvenida(update, context, chat_id)


# --------------------------------------------------------------------------- #
# Comandos de admin (/nombre)
# --------------------------------------------------------------------------- #

async def _responder_admin(update, texto: str) -> None:
    """Respuesta de un comando de admin (con mencion si llega de un grupo)."""
    message = getattr(update, "effective_message", None) if update else None
    if message is None:
        return
    try:
        await message.reply_text(con_mencion(texto, mencion_grupo(update)))
    except Exception:
        pass


def solo_admins(func):
    """Decorador: SOLO el grupo de clientes y TELEGRAM_ADMIN_IDS."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not await _guard_chat(update):
            return
        if not _es_admin_update(update):
            await _responder_admin(update, "⛔ Este comando es solo para el equipo.")
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def _texto_uso_nombre() -> str:
    return (
        "Uso: /nombre <usuario> <Nuevo Nombre>\n\n"
        "Ejemplo: /nombre MiCuenta María López\n\n"
        "Actualiza el nombre REGISTRADO en el bot (sin abrir X). Para cambiarlo "
        "EN X usa el botón ✏️ Cambiar nombre."
    )


def _actualizar_nombre_registrado(usuario: str, nombre: str) -> tuple:
    """Actualiza `Cuenta.nombre_mostrado` en la BD. Devuelve (ok, error)."""
    usuario = clientes_store.limpiar_usuario(usuario)
    nombre = re.sub(r"\s+", " ", str(nombre or "")).strip()
    if not usuario:
        return False, "Falta el usuario de la cuenta (ej. /nombre MiCuenta María López)."
    if not nombre:
        return False, "Falta el nombre nuevo (ej. /nombre MiCuenta María López)."
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
                return (
                    False,
                    f"No encontré la cuenta @{usuario} en la base de datos. "
                    "Revisa el usuario.",
                )
            cuenta.nombre_mostrado = nombre
        return True, ""
    except Exception as e:
        logger.warning(f"No se pudo actualizar nombre_mostrado de {usuario}: {e}")
        return False, f"No se pudo actualizar el registro: {type(e).__name__}: {e}"


@solo_admins
async def comando_nombre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/nombre <usuario> <Nuevo Nombre> (admin): actualiza el nombre REGISTRADO.

    Escribe `Cuenta.nombre_mostrado` en la BD (sin Chrome). El nombre EN X lo
    cambia el cliente con el botón ✏️.
    """
    args = [str(a) for a in (context.args or [])]
    if len(args) < 2:
        await _responder_admin(update, _texto_uso_nombre())
        return
    usuario = clientes_store.limpiar_usuario(args[0])
    nombre = re.sub(r"\s+", " ", " ".join(args[1:])).strip()
    ok, error = await asyncio.to_thread(_actualizar_nombre_registrado, usuario, nombre)
    if not ok:
        await _responder_admin(update, f"❌ {error}")
        return
    await _responder_admin(
        update,
        f"✅ Listo. El nombre registrado de @{usuario} ahora es «{nombre}».\n\n"
        "(Esto actualiza el registro del bot; para cambiarlo EN X usa el boton ✏️.)",
    )
