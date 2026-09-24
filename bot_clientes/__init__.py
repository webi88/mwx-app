"""Bot de Telegram para CLIENTES (no tecnicos) — paquete independiente.

NO modifica ni depende de runtime del bot interno (`bot/`): es un bot nuevo
con su propio token (`TELEGRAM_CLIENTES_BOT_TOKEN`). Funciona SOLO en el grupo
de clientes (`TELEGRAM_CLIENTES_CHAT_ID`, por defecto -1005538610567) y las
cuentas son GLOBALES del grupo (`data/clientes_bot.json`).

Flujos 100% por botones: codigo de verificacion 2FA de X (SOLO TOTP, desde
la semilla en `Cuenta.totp_secret`), cambiar nombre EN X, foto de perfil y
portada. Solo `/start`, `/ayuda` y `/cancelar`; el comando `/nombre` (solo
admin) actualiza el nombre REGISTRADO en el bot sin abrir X.
"""
