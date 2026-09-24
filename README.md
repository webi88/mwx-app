# GestorRedes Telegram Bot

Sistema de gestion y automatizacion de redes sociales con bot de Telegram.

## Instalacion

### 1. Clonar el repositorio
```bash
git clone <repositorio>
cd GestorRedes-Telegram
```

### 2. Crear entorno virtual
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

### 3. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno
```bash
cp .env.example .env
# Editar .env con tus credenciales
```

### 5. Ejecutar
```bash
python -m bot.main
```

## Docker

```bash
docker-compose up -d
```

## Bot para clientes (`bot_clientes/`)

Bot de Telegram **independiente** del bot interno, pensado para clientes no
técnicos que reciben cuentas de X. Todo se hace con **botones** (solo
`/start`, `/ayuda` y `/cancelar`):

- 🔑 **Código de verificación (TOTP)**: se genera al momento desde la semilla
  2FA guardada en `Cuenta.totp_secret` (`pyotp`). El bot **ya no revisa
  correos**: el correo lo tienen los propios clientes. Si una cuenta no tiene
  semilla, el bot pide ayuda a quien le entregó la cuenta.
- ✏️ **Cambiar el nombre** de la cuenta.
- 📸 **Foto de perfil** y 🖼️ **portada** (el cliente envía la imagen por
  Telegram y el bot la sube con Selenium).

```bash
python -m bot_clientes.main
```

Requisitos y registro:

1. `TELEGRAM_CLIENTES_BOT_TOKEN` en `.env` (crear un bot nuevo con @BotFather,
   distinto al bot interno).
2. Los clientes se registran en `data/clientes_bot.json` **con estos comandos
   de admin** (los IDs admin salen de `TELEGRAM_ADMIN_IDS`):
   - `/clientes` — lista los clientes y sus cuentas.
   - `/asignar <telegram_id> [nombre] <usuario1> <usuario2> ...` — agrega
     cuentas a un cliente (el archivo se crea solo si no existe).
   - `/quitar <telegram_id> <usuario1> ...` — quita cuentas a un cliente.
3. El archivo no guarda credenciales (solo nombre, Telegram ID y usuarios) y
   está en `.gitignore`.

### Uso en GRUPO (bot @vrf2fa_bot)

El bot está pensado para usarse dentro de un grupo con los clientes: cada
miembro pulsa SUS botones y responde a los mensajes del bot. El bot contesta en
el grupo anteponiendo `👤 @fulano, ...` para que se sepa a quién responde
(cada usuario tiene su propio estado: los flujos nunca se mezclan) y saluda una
sola vez al agregarlo al grupo (`new_chat_members`/`my_chat_member`).

⚠️ **Group Privacy en @BotFather**: para que los clientes puedan ESCRIBIR el
nombre nuevo o ENVIAR la foto directamente en el grupo (sin responder al
mensaje del bot) hay que desactivar el modo privacidad del bot:

1. Abre @BotFather → `/setprivacy` → elige **@vrf2fa_bot**.
2. Pulsa **Disable**.

Con la privacidad **ON** el bot igual recibe las **respuestas** a sus propios
mensajes (name/photo como *reply*), pero con **Disable** funciona mejor: acepta
el mensaje suelto y la respuesta. Los comandos `/start`, `/ayuda` y `/cancelar`
funcionan en el grupo en ambos casos.

## Comandos de Telegram

### Sistema
- `/start` - Menu principal
- `/help` - Lista de comandos
- `/status` - Estado del sistema

### Cuentas
- `/cuentas` - Listar cuentas
- `/cuentas_agregar [usuario] [plataforma] [tags] [grupo]` - Agregar cuenta
- `/cuentas_editar [usuario] [campo] [valor]` - Editar cuenta
- `/cuentas_eliminar [usuario]` - Eliminar cuenta

### Contenido
- `/contenido [tipo] [celula_id] [cliente_id]` - Generar contenido
- `/publicar [plataforma] [cuenta_id] [contenido]` - Publicar
- `/retweet [url] [cuenta_ids]` - Retweet masivo
- `/like [url] [cuenta_ids]` - Like masivo

### Alertas
- `/alertas` - Configurar alertas
- `/alertas_estado` - Ver estado
- `/alertas_historial` - Ver historial

### Scheduler
- `/programar [tipo] [plataforma] [fecha] [cuenta_ids]` - Programar tarea
- `/tareas` - Ver tareas
- `/cancelar [tarea_id]` - Cancelar tarea

### Celulas
- `/celula` - Ver celulas
- `/celula crear [nombre] [narrativa]` - Crear celula
- `/cliente` - Ver clientes
- `/cliente crear [nombre] [celula_id]` - Crear cliente
- `/entrenar [cliente_id] [instrucciones]` - Entrenar cliente

### Reportes
- `/stats` - Estadisticas
- `/reporte [cliente_id]` - Reporte por cliente
- `/resumen_diario` - Resumen del dia

## Estructura

```
gestor-redes-telegram/
├── bot/                    # Bot de Telegram
├── core/                   # Config, database, models
├── plataformas/            # Twitter, Facebook, Instagram, TikTok
├── ia/                     # Generador de contenido con Gemini
├── alertas/                # Motor de alertas
├── scheduler/              # Programador de tareas
├── cuentas/                # Grizzly SMS y creacion de cuentas
├── utils/                  # Utilidades
├── data/                   # Datos y cookies
└── config/                 # Configuracion
```

## Requisitos

- Python 3.10+
- Chrome/Chromium
- API Key de Google Gemini
- API Key de Grizzly SMS (opcional)
- Bot Token de Telegram
