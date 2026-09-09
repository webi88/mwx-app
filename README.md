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
