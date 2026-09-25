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

Bot de Telegram **independiente** del bot interno. Tiene dos modos de uso:

| Contexto | Quién | Opciones |
|----------|-------|----------|
| **Grupo de clientes** (`-1005538610567`) | Cualquier miembro | **Solo 🔑 código de verificación (TOTP)** + ❓ ayuda (`/start /ayuda /cancelar`) |
| **Privado del administrador** (`TELEGRAM_ADMIN_IDS`) | Dueño/equipo | **Todo**: 🔑 código, ✏️ cambiar nombre EN X, 📸 foto, 🖼️ portada, 📋 cuentas, ❓ ayuda y `/nombre` |
| **Privado de alguien que no es admin** | — | Mensaje corto ("solo funciona para el administrador; en el grupo pide tu código 2FA"), sin menú |
| **Otros grupos** | — | Silencio total |

- 🔑 **Código de verificación (TOTP)**: se genera al momento desde la semilla
  2FA guardada en `Cuenta.totp_secret` (`pyotp`). El bot **ya no revisa
  correos**. Si una cuenta no tiene semilla, el bot pide ayuda al equipo.
- ✏️📸🖼️ **Nombre/fotos/cuentas**: solo en el **privado del administrador**
  (los clientes entran a X por su cuenta con el código).
- 📋 **Cuentas**: lista las cuentas globales y su nombre **registrado** en el
  bot (desde la BD; sin credenciales).

```bash
python -m bot_clientes.main
```

Requisitos y configuración:

1. `TELEGRAM_CLIENTES_BOT_TOKEN` en `.env` (crear un bot nuevo con @BotFather,
   distinto al bot interno).
2. `TELEGRAM_CLIENTES_CHAT_ID` con el chat_id del grupo (por defecto
   `-1005538610567`; también se puede fijar en el `chat_id` de
   `data/clientes_bot.json`).
3. `data/clientes_bot.json` guarda las cuentas globales del grupo:
   `{"chat_id": -1005538610567, "cuentas": ["usuario1", ...]}`. Se siembra solo
   con las 15 cuentas si no existe y migra automáticamente el formato viejo
   (`{"clientes": {...}}`). No guarda credenciales y está en `.gitignore`.
4. **Admin** (`TELEGRAM_ADMIN_IDS`), en el privado:
   - `/nombre <usuario> <Nuevo Nombre>` actualiza el **nombre registrado en el
     bot** (escribe `Cuenta.nombre_mostrado` en la BD, sin Chrome).
   - El botón ✏️ Cambiar nombre actualiza el nombre **EN X** (Selenium).

### Uso en GRUPO (bot @vrf2fa_bot)

En el grupo los clientes solo ven **🔑 Quiero mi código de X** y **❓ Ayuda**:
eligen la cuenta (las 15 son globales del grupo) y el bot responde con el TOTP
y la mención `👤 @fulano, ...`. El bot saluda una sola vez al agregarlo
(`new_chat_members`/`my_chat_member`).

✅ **Group Privacy NO hace falta**: en el grupo solo se usan **botones y
comandos** (`/start`, `/ayuda`, `/cancelar`), no texto libre ni fotos, así que
el modo privacidad de @BotFather puede quedarse como está (con *Disable*
tampoco pasa nada).

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
