"""Bot de Telegram para CLIENTES (no tecnicos) — paquete independiente.

NO modifica ni depende de runtime del bot interno (`bot/`): es un bot nuevo
con su propio token (`TELEGRAM_CLIENTES_BOT_TOKEN`) y su propio registro de
clientes (`data/clientes_bot.json`).

Flujos 100% por botones: codigo de verificacion de X (2FA TOTP o codigo del
correo), cambiar nombre, foto de perfil y portada. Solo `/start`, `/ayuda` y
`/cancelar`; los comandos de admin (/clientes, /asignar, /quitar) son para el
equipo.
"""
