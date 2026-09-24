"""Teclados inline del bot de clientes (PTB v21) — grandes, con emojis.

CONVENCION DE CALLBACKS (cada prefijo tiene su manejador en handlers.py):
  cli_*          Menu y acciones -> cli_callback
    - cli_menu | cli_codigo | cli_nombre | cli_foto_perfil | cli_foto_portada
    - cli_mis_cuentas | cli_ayuda | cli_cancelar | cli_cuenta_<usuario>
  cuenta_*       Selector de cuenta -> cuenta_callback
  codigo_*       Pedir otro codigo -> codigo_callback
  nombre_*       Elegir cuenta / confirmar -> nombre_callback
    - nombre_<usuario> | nombre_si_<usuario> | nombre_no_<usuario>
  foto_*         Elegir cuenta / confirmar -> foto_callback
    - foto_perfil_<usuario> | foto_portada_<usuario>
    - foto_si_<tipo>_<usuario> | foto_no_<tipo>_<usuario> |
      foto_otra_<tipo>_<usuario>
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def menu_principal() -> InlineKeyboardMarkup:
    """Menu principal del /start: UN boton por cosa que el cliente quiere."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔑 Quiero mi código de X", callback_data="cli_codigo")],
            [InlineKeyboardButton("✏️ Cambiar el nombre", callback_data="cli_nombre")],
            [
                InlineKeyboardButton(
                    "📸 Cambiar la foto de perfil", callback_data="cli_foto_perfil"
                )
            ],
            [
                InlineKeyboardButton(
                    "🖼️ Cambiar la portada", callback_data="cli_foto_portada"
                )
            ],
            [InlineKeyboardButton("📋 Mis cuentas", callback_data="cli_mis_cuentas")],
            [InlineKeyboardButton("❓ Ayuda", callback_data="cli_ayuda")],
        ]
    )


def teclado_cuentas(usuarios) -> InlineKeyboardMarkup:
    """Un boton por cuenta (para elegir con cual hacer la operacion)."""
    filas = [
        [
            InlineKeyboardButton(
                f"👤 @{str(usuario).strip().lstrip('@')}",
                callback_data=f"cuenta_{str(usuario).strip()}",
            )
        ]
        for usuario in (usuarios or [])
    ]
    filas.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")])
    return InlineKeyboardMarkup(filas)


def teclado_acciones_cuenta(usuario: str) -> InlineKeyboardMarkup:
    """Acciones disponibles para UNA cuenta (desde 📋 Mis cuentas)."""
    usuario = str(usuario or "").strip()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔑 Quiero mi código", callback_data=f"codigo_{usuario}"
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Cambiar el nombre", callback_data=f"nombre_{usuario}"
                )
            ],
            [
                InlineKeyboardButton(
                    "📸 Cambiar foto de perfil", callback_data=f"foto_perfil_{usuario}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🖼️ Cambiar portada", callback_data=f"foto_portada_{usuario}"
                )
            ],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")],
        ]
    )


def teclado_mis_cuentas(usuarios) -> InlineKeyboardMarkup:
    """Lista de cuentas tocables (desde 📋 Mis cuentas)."""
    filas = [
        [
            InlineKeyboardButton(
                f"👤 @{str(usuario).strip().lstrip('@')}",
                callback_data=f"cli_cuenta_{str(usuario).strip()}",
            )
        ]
        for usuario in (usuarios or [])
    ]
    filas.append([InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")])
    return InlineKeyboardMarkup(filas)


def teclado_codigo(usuario: str) -> InlineKeyboardMarkup:
    """Boton 'otro codigo' despues de entregar el codigo."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Quiero otro código", callback_data=f"codigo_{usuario}"
                )
            ],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")],
        ]
    )


def teclado_codigo_fallo(usuario: str) -> InlineKeyboardMarkup:
    """Boton de reintento cuando el codigo del correo no llego."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Intentar de nuevo", callback_data=f"codigo_{usuario}"
                )
            ],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")],
        ]
    )


def teclado_confirmar_nombre(usuario: str) -> InlineKeyboardMarkup:
    """Confirmacion (✅/❌) antes de cambiar el nombre."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Sí, cámbialo", callback_data=f"nombre_si_{usuario}"
                )
            ],
            [InlineKeyboardButton("❌ No", callback_data=f"nombre_no_{usuario}")],
        ]
    )


def teclado_reintentar_nombre(usuario: str) -> InlineKeyboardMarkup:
    """Reintento cuando el cambio de nombre fallo."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔁 Intentar otra vez", callback_data=f"nombre_{usuario}"
                )
            ],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")],
        ]
    )


def teclado_confirmar_foto(tipo: str, usuario: str) -> InlineKeyboardMarkup:
    """Confirmacion (✅/🔁/❌) despues de recibir la imagen."""
    tipo = "portada" if (tipo or "").strip().lower() == "portada" else "perfil"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Sí, usa esta foto", callback_data=f"foto_si_{tipo}_{usuario}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔁 Enviar otra foto", callback_data=f"foto_otra_{tipo}_{usuario}"
                )
            ],
            [InlineKeyboardButton("❌ No", callback_data=f"foto_no_{tipo}_{usuario}")],
        ]
    )


def teclado_reintentar_foto(tipo: str, usuario: str) -> InlineKeyboardMarkup:
    """Reintento cuando subir la foto fallo."""
    tipo = "portada" if (tipo or "").strip().lower() == "portada" else "perfil"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔁 Intentar otra vez", callback_data=f"foto_{tipo}_{usuario}"
                )
            ],
            [InlineKeyboardButton("⬅️ Volver al menú", callback_data="cli_menu")],
        ]
    )


def teclado_cancelar() -> InlineKeyboardMarkup:
    """Boton unico de cancelar (mientras se espera texto o foto)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancelar", callback_data="cli_cancelar")]]
    )
