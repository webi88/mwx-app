"""Teclados y botones inline del bot (PTB v21).

CONVENCION DE CALLBACKS (cada uno tiene su manejador en handlers/):
  Menu (handlers/comandos.py -> menu_callback):
    - menu_inicio | menu_cuentas | menu_brandear | menu_perfil
    - menu_status | menu_ayuda
  Cuentas (handlers/cuentas.py):
    - cuentas_verificar            -> cuentas_verificar_callback
    - brandeo:<usuario>:<minutos>  -> brandear_callback
    - perfil_foto:<usuario>        -> perfil_foto_callback
    - perfil_portada:<usuario>     -> perfil_portada_callback
    - perfil_brandeo:<usuario>     -> perfil_brandeo_callback
    - perfil_verificar:<usuario>   -> perfil_verificar_callback
    - perfil_cancelar              -> perfil_cancelar_callback
    - foto_manual:<tipo>:<usuario> -> foto_manual_callback (tipo=perfil|portada)
    - foto_cancelar                -> foto_cancelar_callback
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def menu_principal() -> InlineKeyboardMarkup:
    """Menu principal del /start."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📂 Cuentas", callback_data="menu_cuentas"),
                InlineKeyboardButton("🎨 Brandear", callback_data="menu_brandear"),
            ],
            [
                InlineKeyboardButton("✏️ Cambiar perfil", callback_data="menu_perfil"),
                InlineKeyboardButton("📊 Status", callback_data="menu_status"),
            ],
            [
                InlineKeyboardButton("❓ Ayuda", callback_data="menu_ayuda"),
            ],
        ]
    )


def teclado_cuentas() -> InlineKeyboardMarkup:
    """Atajos del apartado de cuentas."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔍 Verificar suspendidas", callback_data="cuentas_verificar"),
            ],
            [
                InlineKeyboardButton("🎨 Brandear cuenta", callback_data="menu_brandear"),
                InlineKeyboardButton("✏️ Cambiar perfil", callback_data="menu_perfil"),
            ],
            [
                InlineKeyboardButton("⬅️ Volver", callback_data="menu_inicio"),
            ],
        ]
    )


def teclado_brandear(usuario: str, minutos: int = 5) -> InlineKeyboardMarkup:
    """Confirmacion para abrir el navegador de brandeo manual."""
    usuario = (usuario or "").strip()
    minutos = int(minutos or 5)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🎨 Abrir navegador ({minutos} min)",
                    callback_data=f"brandeo:{usuario}:{minutos}",
                ),
            ],
            [
                InlineKeyboardButton("⬅️ Volver", callback_data="menu_cuentas"),
            ],
        ]
    )


def teclado_cambiar_perfil(usuario: str) -> InlineKeyboardMarkup:
    """Apartado de fotos dentro del flujo cambiar nombre/@.

    Se muestra despues de cambiar nombre/@ para hacerlo facil:
    foto de perfil, portada o brandeo manual.
    """
    usuario = (usuario or "").strip()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📷 Cambiar foto perfil",
                    callback_data=f"perfil_foto:{usuario}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼️ Cambiar portada",
                    callback_data=f"perfil_portada:{usuario}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎨 Brandeo manual (5 min)",
                    callback_data=f"perfil_brandeo:{usuario}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✅ Verificar fotos manuales",
                    callback_data=f"perfil_verificar:{usuario}",
                ),
            ],
            [
                InlineKeyboardButton("❌ Cancelar", callback_data="perfil_cancelar"),
            ],
        ]
    )


def teclado_foto_modo(usuario: str, tipo: str) -> InlineKeyboardMarkup:
    """Modos para subir foto: enviar por Telegram o abrir navegador manual.

    tipo: "perfil" | "portada".
    """
    usuario = (usuario or "").strip()
    tipo = (tipo or "perfil").strip().lower()
    if tipo not in ("perfil", "portada"):
        tipo = "perfil"
    etiqueta = "perfil" if tipo == "perfil" else "portada"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🎨 Abrir navegador manual 5 min ({etiqueta})",
                    callback_data=f"foto_manual:{tipo}:{usuario}",
                ),
            ],
            [
                InlineKeyboardButton("❌ Cancelar", callback_data="foto_cancelar"),
            ],
        ]
    )
