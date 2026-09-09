# GestorRedes Telegram Bot + Dashboard Web

## Descripcion General

Sistema de gestion y automatizacion de redes sociales con **dos interfaces**:
1. **Dashboard Web** (Streamlit) para el jefe/operadores: todas las operaciones en una pagina web accesible desde cualquier dispositivo.
2. **Bot de Telegram** como control remoto movil.

Permite gestionar multiples cuentas en 4 plataformas (Twitter/X, Facebook, Instagram, TikTok).

## Arquitectura del Proyecto

```
GestorRedes-Telegram-Final/
├── bot/                    # Bot de Telegram (interfaz de usuario)
│   ├── main.py             # Entry point del bot
│   ├── keyboards.py        # Teclados y botones inline
│   ├── middlewares.py       # Autenticacion por Telegram ID
│   └── handlers/           # Manejadores de comandos
│       ├── comandos.py     # Menu principal y navegacion
│       ├── publicar.py     # Publicar contenido en redes
│       ├── cuentas.py      # Gestion de cuentas
│       ├── alertas.py      # Sistema de alertas
│       ├── scheduler.py    # Programador de tareas
│       ├── celulas.py      # Gestion de celulas/clientes
│       ├── reportes.py     # Estadisticas y reportes
│       └── ayuda.py        # Guia de comandos
│
├── core/                   # Nucleo del sistema
│   ├── config.py           # Configuracion central
│   ├── database.py         # Conexion SQLite
│   ├── models.py           # Modelos de datos
│   ├── auth.py             # Autenticacion multi-usuario
│   └── monitor_actividad.py # Monitoreo de actividad
│
├── plataformas/            # Automatizacion de redes sociales
│   ├── base.py             # Clase base y factory
│   ├── twitter/
│   │   ├── selenium_bot.py # TwitterBot (36 funciones)
│   │   ├── api_http.py     # TwitterAPI (HTTP directo)
│   │   └── cookies.py      # Gestion de cookies
│   ├── facebook/
│   │   └── selenium_bot.py # FacebookBot (13 funciones)
│   ├── instagram/
│   │   └── selenium_bot.py # InstagramBot (14 funciones)
│   └── tiktok/
│       └── selenium_bot.py # TikTokBot (12 funciones)
│
├── ia/                     # Inteligencia Artificial
│   ├── generador_contenido.py # Genera posts con Gemini
│   ├── generador_imagenes.py  # Genera imagenes con IA
│   ├── filtros_alertas.py     # Clasifica alertas
│   ├── celulas.py             # Gestion de celulas
│   └── prompts.py             # Prompts organizados
│
├── alertas/                # Motor de alertas
│   ├── motor.py            # Coordinador de alertas
│   ├── fuentes/
│   │   ├── google_news.py  # RSS de Google News
│   │   └── twitter_api.py  # Busqueda en Twitter
│   ├── filtros.py          # Filtros geo y tematicos
│   ├── deduplicacion.py    # No repetir alertas
│   └── notificador.py      # Envio a Telegram
│
├── scheduler/              # Programador de tareas
│   ├── manager.py          # APScheduler
│   ├── ejecutor.py         # Ejecuta tareas
│   └── models.py           # Modelos de tareas
│
├── cuentas/                # Creacion de cuentas
│   ├── grizzly_api.py      # API de Grizzly SMS
│   ├── twitter_signup.py   # Registro en Twitter
│   ├── email_generator.py  # Emails temporales
│   ├── creador.py          # Coordinador de creacion
│   ├── perfilador.py       # Configurar perfiles
│   └── change_org.py       # Bot de Change.org
│
├── utils/                  # Utilidades
│   ├── anti_detection.py   # Scripts stealth
│   ├── humanizer.py        # Comportamiento humano
│   ├── image_utils.py      # Manipulacion de imagenes
│   └── url_utils.py        # URLs y acortamiento
│
├── data/                   # Datos y cookies
│   ├── cookies/            # Sesiones por cuenta
│   ├── perfiles_chrome/    # Perfiles aislados
│   ├── reportes/           # Historial y logs
│   └── temp/               # Archivos temporales
│
├── config/                 # Configuracion
│   └── config.json         # Config del monitor
│
├── web/                   # DASHBOARD WEB (Streamlit)
│   ├── app.py             # Entry point: login + navegacion de 17 operaciones
│   ├── auth.py            # Autenticacion web (data/web_users.json, SHA256)
│   ├── ui.py              # Componentes visuales (cabecera, tarjetas, stats)
│   ├── sidebar.py         # Sidebar: clientes, asistente IA, multimedia, inventario
│   └── operaciones/       # ★ Paginas de operaciones (NO llamarla "pages" -> Streamlit la tomaria como multipagina automatica y rompe)
│       ├── _helpers.py    # Utilidades compartidas de las paginas
│       ├── alertas.py     # ★ DASHBOARD DE ALERTAS (por cliente, fuente, menciones, resumenes) - landing por defecto
│       ├── posts.py       # Publicar texto, hilos, contenido IA
│       ├── rts.py         # RT masivo, RT con cita, likes, calentamiento
│       ├── resumenes.py   # Resumenes ejecutivos / mananeras
│       ├── grupos.py      # Enviar a grupos, reciprocidad
│       ├── crisis.py      # Respuestas a tweets
│       ├── visualizaciones.py
│       ├── change.py      # Change.org
│       ├── blogs.py       # Blogs WordPress
│       ├── likes.py       # Likes masivos multi-plataforma
│       ├── follows.py     # Follows masivos
│       ├── reportar.py    # Reportar posts
│       ├── calendario.py  # Programacion de tareas
│       ├── reportes.py    # Estadisticas generales
│       ├── monitor.py     # Monitor de actividad
│       ├── multimedia.py  # Generacion de imagenes IA + texto sobre imagen
│       └── admin.py       # Gestion de usuarios web
│
├── .streamlit/
│   └── config.toml        # Tema oscuro, puerto 8501
├── migrar_cuentas.py      # Importa cuentas+cookies del proyecto original (GestorTwitter)
├── iniciar_dashboard.py   # Lanzador del dashboard web (mensaje amigable si falta streamlit)
├── auto_alertas.py        # Script de alertas autonomo
├── requirements.txt       # Dependencias
├── Dockerfile             # Contenedor Docker (Python 3.11 + Chrome)
├── docker-compose.yml     # Orquestacion Docker (bot + dashboard-web)
├── .env.example           # Variables de entorno
├── .gitignore             # Archivos ignorados
└── README.md              # Esta documentacion
```

## Funcionalidades Principales

### 1. Gestion de Cuentas
- Soporta 4 plataformas: Twitter/X, Facebook, Instagram, TikTok
- Sistema multi-cuenta con tags y grupos (A/B/C)
- Cookies persistentes para evitar logins repetidos
- Creacion automatica de cuentas con Grizzly SMS
- Login manual con anti-deteccion

### 2. Generacion de Contenido con IA
- Google Gemini para generar posts politicos
- Sistema de Celulas y Clientes con narrativas propias
- 7 formatos: Mantenimiento, Activacion, Narrativa, Reposteo, Blog, Verificado, Harfuch
- Generacion de imagenes con Nano Banana Pro/Flash
- Texto sobre imagen con PIL/Pillow

### 3. Automatizacion en Redes Sociales
- **Twitter**: Tweet con imagen, hilo, RT, RT con cita, likes, obtener links
- **Facebook**: Post, compartir, comentarios, likes, seguir, reportar
- **Instagram**: Post (requiere imagen), likes, comentarios, seguir, compartir en Stories
- **TikTok**: Publicar video, likes, comentarios, seguir, repost
- Comportamiento humano simulado (scrolls, delays aleatorios)
- Deteccion de limites y suspensiones

### 4. Sistema de Alertas y Monitoreo
- Busqueda multi-fuente: Google News RSS, Twitter API, Facebook
- Filtros en capas: geografico, tematico, por keywords
- Filtro AI con Gemini para clasificar relevancia politica
- Deduplicacion por cliente (no repetir alertas)
- Resumenes diarios por fuente y por temas
- Rate limiting adaptativo

### 5. Notificaciones a Telegram
- Alertas individuales con formato rico
- Resumenes por fuente y por temas
- Multiples chat_ids por cliente
- URLs acortadas automaticamente
- Division de mensajes largos

### 6. Scheduler de Tareas
- Persistencia en SQLite
- Thread background que ejecuta cada 30 segundos
- Tipos: post, retweet, respuesta, like, follow, visualizacion
- Soporte multi-plataforma

### 7. Sistema Multi-usuario
- Roles: admin (ve todo) y operador (solo sus datos)
- Autenticacion por Telegram ID
- Panel admin para gestionar usuarios

### 8. Monitoreo de Actividad
- Conteo de tweets y retweets por periodo
- Multi-lectora para ejecucion en paralelo
- Reportes guardados en JSON

### 9. Campanas Change.org
- Firmas automatizadas con identidades generadas (Faker)
- Comportamiento humano: escritura letra por letra
- Rotacion de IP con pausas manuales
- Anti-deteccion con undetected_chromedriver

## Tecnologias Utilizadas

| Tecnologia | Uso |
|------------|-----|
| Python 3.10+ | Lenguaje principal |
| python-telegram-bot | Bot de Telegram |
| SQLAlchemy | ORM para SQLite |
| Selenium + undetected_chromedriver | Automatizacion de navegadores |
| curl_cffi | HTTP con fingerprint de Chrome |
| Google Generative AI (Gemini) | Generacion de contenido |
| APScheduler | Programacion de tareas |
| Pillow (PIL) | Manipulacion de imagenes |
| feedparser | Parseo de RSS |
| Faker | Generacion de identidades |
| Loguru | Logging estructurado |

## Configuracion

### Variables de Entorno (.env)

```
TELEGRAM_BOT_TOKEN=token_de_tu_bot
TELEGRAM_ADMIN_IDS=tu_id_de_telegram
GEMINI_API_KEY=tu_api_key_de_gemini
GRIZZLY_API_KEY=tu_api_key_de_grizzly
DATABASE_URL=sqlite:///data/gestor_redes.db
```

> Estado actual: el `.env` del proyecto contiene valores placeholder de prueba (dashboard y bot arrancan, pero las funciones reales de IA/Telegram requieren claves reales).

### Estructura de Celulas/Clientes

```
CELULA (narrativa comun)
├── CLIENTE 1 (entrenamiento propio)
│   ├── Cuentas asignadas
│   └── Keywords de alertas
├── CLIENTE 2
│   └── ...
```

## Comandos de Telegram

### Menu Principal con Botones
- `/start` - Muestra el menu principal con botones
- `/help` - Muestra ayuda rapida
- `/ayuda` - Guia completa de comandos
- `/status` - Estado del sistema

### Gestion de Cuentas
- `/cuentas` - Listar cuentas
- `/cuentas_agregar` - Agregar cuenta
- `/cuentas_editar` - Editar cuenta
- `/cuentas_eliminar` - Eliminar cuenta
- `/cuentas_verificar` - Verificar sesiones, detectar y marcar suspendidas
- `/crear_cuentas` - Crear con Grizzly SMS

## Migracion de Cuentas desde el Proyecto Original

Las cuentas del proyecto original (GestorTwitter) se importan con el script:

```bash
python migrar_cuentas.py "ruta/al/config.json_original"
```

Que hace:
1. Lee las cuentas del `config.json` original (user, tags, grupo)
2. Las inserta en la base de datos SQLite como plataforma `twitter`
3. Copia los archivos `.pkl` de cookies desde `cookies/` a `data/cookies/twitter/`
4. Importa tambien cookies que existen pero no estaban en el config
5. Reporta resumen: importadas, con cookies, sin cookies, duplicadas

Despues de migrar, usa `/cuentas_verificar` en Telegram para:
- Revisar cada cuenta contra X.com con sus cookies
- Detectar suspendidas, limitadas o expiradas
- Marcar automaticamente las problematicas como INACTIVAS
- Luego `/cuentas_eliminar [usuario]` para borrarlas definitivamente

### Contenido
- `/contenido` - Generar con IA
- `/publicar` - Publicar en redes
- `/publicar_masivo` - Publicar mismo contenido en varias cuentas
- `/hilo` - Publicar hilo de tweets
- `/retweet` - Retweet masivo
- `/like` - Like masivo

### Alertas
- `/alertas` - Configurar alertas
- `/alertas_estado` - Ver estado del sistema
- `/alertas_historial` - Ver historial
- `/alertas_resumen` - Resumen ultimas 24h
- `/ejecutar_alertas` - Ejecutar ahora

### Scheduler
- `/programar` - Programar tarea
- `/tareas` - Ver tareas pendientes
- `/cancelar` - Cancelar tarea

### Reportes
- `/stats` - Estadisticas generales
- `/reporte` - Reporte por cliente
- `/resumen_diario` - Resumen del dia
- `/monitor` - Monitoreo de actividad

### Campanas
- `/change` - Campaña Change.org
- `/monitoreo` - Monitoreo completo
- `/grupos` - Enviar a grupos
- `/recopilar_links` - Recopilar links
- `/reportar` - Reportar posts

## Despliegue

### Con Docker (bot + dashboard web)
```bash
docker-compose up -d
```

### Dashboard Web (Streamlit)
```bash
pip install -r requirements.txt
python iniciar_dashboard.py
```
O directamente:
```bash
streamlit run web/app.py
```
- URL: `http://localhost:8501`
- Usuario por defecto: `admin` / `admin` (cambiar en `data/web_users.json`)
- Las contraseñas se guardan con hash SHA256
- Login: `web/auth.py`, navegación entre 17 operaciones vía selectbox en `web/app.py`

## Dashboard Web - Estructura

```
web/
├── app.py           # Entry point: login + navegacion de 17 operaciones
├── auth.py          # Autenticacion web (usuarios en data/web_users.json)
├── ui.py            # Componentes visuales (cabecera, tarjetas, stats)
├── sidebar.py       # Sidebar: clientes, asistente IA, multimedia, inventario
└── operaciones/     # ★ Paginas de operaciones
    ├── _helpers.py  # Utilidades compartidas
    ├── alertas.py   # ★ DASHBOARD DE ALERTAS (por cliente, fuente, menciones, resumenes) - es la pagina por defecto
    ├── posts.py     # Publicar texto, hilos, contenido IA
    ├── rts.py       # RT masivo, RT con cita, likes, calentamiento
    ├── resumenes.py # Resumenes ejecutivos / mananeras
    ├── grupos.py    # Enviar a grupos, reciprocidad
    ├── crisis.py    # Respuestas a tweets
    ├── visualizaciones.py
    ├── change.py    # Change.org
    ├── blogs.py     # Blogs WordPress
    ├── likes.py     # Likes masivos multi-plataforma
    ├── follows.py   # Follows masivos
    ├── reportar.py  # Reportar posts
    ├── calendario.py# Programacion de tareas
    ├── reportes.py  # Estadisticas generales
    ├── monitor.py   # Monitor de actividad
    ├── multimedia.py# Generacion de imagenes IA + texto sobre imagen
    └── admin.py     # Gestion de usuarios web
```

Las paginas web reutilizan la MISMA logica que el bot:
`plataformas/` (Selenium), `ia/` (Gemini), `alertas/` (motor), `scheduler/`, `cuentas/`, `core/` (SQLite).

> ⚠️ **IMPORTANTE**: la carpeta de paginas se llama `operaciones/`, NO `pages/`. Si se llama `pages/`, Streamlit la detecta como sistema de multipagina automatico, genera URLs falsas (`/admin`, `/alertas`, `/blogs`) que dan 404 y deja pantallas en blanco. Ya se renombro de `web/pages/` a `web/operaciones/`; no volver a llamarla `pages/`.

### Bot de Telegram
```bash
pip install -r requirements.txt
python -m bot.main
```

## Notas Importantes

1. **Requiere Chrome/Chromium** instalado en el sistema
2. **API Key de Gemini** necesaria para generacion de contenido
3. **API Key de Grizzly SMS** necesaria solo para crear cuentas
4. **Bot Token de Telegram** se obtiene de @BotFather
5. Las cookies se guardan en `data/cookies/` por plataforma
6. Los reportes se guardan en `data/reportes/`
7. El proyecto usa SQLite (sin necesidad de servidor de base de datos)
8. Funciona en Windows, Linux y macOS
9. Se puede desplegar en VPS, Railway, o cualquier servidor Docker
10. **Python 3.11 requerido** para `undetected_chromedriver` (3.14 rompe por distutils). Dockerfile ya usa `python:3.11-slim` + Chrome.
11. **Rutas siempre absolutas**: el proyecto usa `resolver_ruta()` (definida en `core/config.py`) para que rutas como la base de datos SQLite, cookies, `.env` y reportes funcionen sin importar desde qué carpeta se lance el script. `core/database.py` convierte `DATABASE_URL` relativa a ruta absoluta del project root.
12. **Ejecutar el dashboard desde la raiz del proyecto**: `streamlit run web/app.py` o `python iniciar_dashboard.py`. `web/app.py` agrega la raiz del proyecto a `sys.path` automaticamente.
13. `.env` debe estar en la raiz del proyecto (se carga con ruta absoluta). Actualmente contiene TOKENS DE PRUEBA placeholder (test_token_placeholder, test_gemini_key, etc.) - reemplazar con claves reales.
14. **Verificar el dashboard localmente**: `AppTest` de streamlit recorre las 17 operaciones. Para probar la web sin Chrome: el dashboard funciona, pero las acciones de Selenium (publicar, RT, likes) necesitan Chrome instalado.

## Bitacora de Cambios

### Bot de Telegram
- `bot/handlers/ayuda.py`: comando `/ayuda` con guia visual de comandos
- `bot/handlers/publicar.py`: `/publicar_masivo`, `/retweet_masivo`, `/like_masivo`, `/hilo`, `/monitoreo`, `/reportar`, `/grupos`, `/recopilar_links`
- `bot/handlers/comandos.py`: implementados los 5 TODOs pendientes (`alertas_estado`, `alertas_resumen`, `scheduler_ver`, `reportes_stats`, `reportes_resumen`) + callback `cuentas_verificar`
- `bot/handlers/cuentas.py`: `/cuentas_verificar` detecta suspendidas/limitadas con cookies y las marca INACTIVAS
- `bot/keyboards.py`: boton "Verificar Suspendidas"
- `bot/main.py`: registrados `/ayuda` y `/cuentas_verificar`

### Dashboard Web (Streamlit)
- Creadas 17 operaciones en `web/operaciones/`: alertas (7 pestañas, landing), posts, rts, resumenes, grupos, crisis, visualizaciones, change, blogs, likes, follows, reportar, calendario, reportes, monitor, multimedia, admin
- Login web en `web/auth.py` (usuarios en `data/web_users.json`, hash SHA256, default admin/admin)
- Tema oscuro MW en `.streamlit/config.toml` + `web/ui.py`
- `iniciar_dashboard.py` lanzador con mensaje amigable
- `docker-compose.yml`: servicio `dashboard-web` en puerto 8501
- **Bug arreglado**: `web/pages/` -> `web/operaciones/` (Streamlit tomaba `pages/` como multipagina automatica -> 404 y pantallas en blanco)
- **Bug arreglado**: `db.func` -> `sqlalchemy.func` en alertas
- **Bug arreglado**: `web/app.py` agrega raiz del proyecto a `sys.path` (fallaba `No module named 'core'` al correr desde `web/`)

### Publicacion con verificacion real + imagenes pegadas
- `plataformas/twitter/selenium_bot.py`:
  - `publicar_tweet` / `publicar_hilo` ya NO devuelven exito solo por hacer clic: verifican con `_verificar_publicacion()` (toast "Your post was sent" / salir de `/compose/post`) y dejan 5s la pantalla visible para confirmacion visual.
  - `_buscar_boton_post()`: busqueda forzosa del boton "Post"/"Publicar" (por testid y por texto visible).
  - `_buscar_opcion_quote()`: en RT con cita elige la opcion "Quote"/"Citar" del menu (antes usaba `retweetConfirm` y hacia repost directo + comentario suelto).
  - `solo_retwittear()`: acepta `imagen_path` para subir imagen en la cita.
- `ia/generador_contenido.py` y `ia/generador_imagenes.py`: modelo de imagen corregido a `gpt-image-1` (no existe `dall-e-3`) y manejo de respuesta `b64_json` (el modelo ya no devuelve `url`).
- **Imagenes pegadas en el dashboard**: nuevo helper `guardar_imagen_subida()` en `web/operaciones/_helpers.py`.
  - `web/operaciones/posts.py` (Publicar Texto): ahora se pega/arrastra la imagen con `st.file_uploader` en vez de escribir una ruta/URL.
  - `web/operaciones/rts.py` (RT con cita): campo opcional para subir imagen de la cita.

### Motor de activacion masiva (Railway / 500 cuentas)
- Nuevo modulo `activaciones/`:
  - `motor.py`: `MotorActivacion` orquesta quote-RTs aleatorizados sobre N cuentas a la vez con concurrencia limitada (`MAX_BROWSERS`, default 15), cohortes temporales para no disparar anti-spam, y cada cuenta usa su propio proxy + cookie.
  - `variaciones.py`: genera variaciones leves del texto de cita (sinonimos, aperturas, hashtags, cierres) para que las cuentas no publiquen contenido identico.
- `web/operaciones/activacion_masiva.py`: nueva pagina "🎯 Activación Masiva" registrada en `web/app.py`.
- `core/config.py`: `headless` y `max_browsers` ahora configurables por env (`HEADLESS`, `MAX_BROWSERS`).
- `cuentas/twitter_signup.py`: el registro respeta `settings.headless` (antes fijo en `headless=False`).
- `.env.example`: documentadas `HEADLESS`, `MAX_BROWSERS`, `OPENAI_API_KEY`.

### Secciones de cuentas (sector politico/comercial) + proxy por pais
- `utils/proxies.py`: `detectar_pais()` lee el codigo `_area-XX_` de Smartproxy; `cargar_por_pais()` filtra proxies por pais; `pais_normalizado()` mapea alias.
- `core/models.py`: `Cuenta` ahora tiene `pais`, `sector` y `avatar_path`. `core/database.py` migra esas columnas automaticamente.
- `cuentas/email_generator.py`: nombres de usuario y nombre desplegado por sector:
  - `centroderecha`: apoyando a Morena (4T).
  - `centroizquierda`: oposicion.
  - `privados`: corporativos (BBVA).
- `cuentas/creador.py`: `crear_cuentas()` acepta `sector`, `proxy_mismo_pais` (asigna proxy del MISMO pais que el numero) y `generar_avatar` (genera foto de perfil con IA y la guarda en `data/avatars/`).
- `cuentas/perfilador.py`: `aplicar_avatar()` / `aplicar_avatares_pendientes()` suben las fotos de perfil generadas a las cuentas.
- `web/operaciones/cuentas.py`: selector de sector, resumen de proxies por pais, opcion de avatar IA y validacion de proxy por pais.

### Proxies por pais (carpeta data/proxies/)
- `utils/proxies.py`: `ProxyManager` ahora lee la carpeta `data/proxies/` (un archivo `.txt` por pais, ej. `mexico.txt`, `usa.txt`, `espana.txt`, `brasil.txt`, `colombia.txt`, `argentina.txt`, `uk.txt`). Si no existe esa carpeta, cae al `data/proxies.txt` original.
- `cargar_por_pais(pais)`: busca primero el archivo dedicado del pais y, si no, filtra por deteccion `_area-XX_`.
- Cargados 350 proxies Smartproxy (50 por pais: MX, US, ES, BR, CO, AR, GB). El formato usado es `host:port:user:pass` con codigo `_area-XX_` en el usuario.
- `canada` agregado como pais valido (Grizzly code 36, lada +1, `_area-CA_` en proxies).
- **Bug arreglado**: USA y Canada comparten el prefijo `+1`, por lo que el selector de pais de X elegia "United States" al pedir Canada. `twitter_signup.py` ahora acepta `pais_nombre` y selecciona la opcion por nombre ("Canada") en vez de solo por lada.
- **Bug arreglado**: el clic en "Continuar" del formulario de telefono se congelaba porque en la nueva UI de X hay varios botones `type=submit` ("Continuar" del telefono y "Continuar" del email). `buscar_submit_telefono()` ahora sube por ancestros desde `input[name='phone']` para aislar el submit correcto, con fallback por texto exacto "Continuar". `esperar_otp()` ahora detecta mas selectores del campo de codigo.
- `cuentas/grizzly_api.py`: `cancelar_activacion()` ahora respeta la ventana de ~2 min de Grizzly (antes respondia `EARLY_CANCEL_DENIED` y dejaba el numero pagado). Reintenta hasta lograrlo.
- `cuentas/twitter_signup.py`: detecta el error generico de X ("Algo salió mal" / "Something went wrong"), que indica IP/proxy bloqueada para registro, y lo reporta claramente en vez de seguir intentando "Continuar". Lanza `ProxyBloqueadoError` y devuelve `"proxy_bloqueado"`.
- **Proxies quemados (bloqueados por X)**: `utils/proxies.py` agrega `marcar_quemado()`, `cargar_quemados()` y `_clave()`. Cuando un proxy falla con "Algo salió mal", `cuentas/creador.py` lo mueve a `data/proxies/quemados.txt`, lo elimina de su archivo de país y reintenta la MISMA cuenta con otro proxy del mismo país. Los proxies quemados se excluyen automáticamente al cargar.

### Rutas absolutas (resolver_ruta en core/config.py)
- `core/config.py`: `.env` se carga desde PROJECT_ROOT; nuevo helper `resolver_ruta()`
- `core/database.py`: `DATABASE_URL` relativa convertida a ruta absoluta (funciona desde cualquier CWD)
- Rutas relativas corregidas en: `plataformas/base.py`, `twitter/selenium_bot.py`, `twitter/cookies.py`, `twitter/api_http.py`, selenium_bots de facebook/instagram/tiktok, `core/monitor_actividad.py`, `cuentas/change_org.py`, `ia/generador_contenido.py`, `ia/generador_imagenes.py`, `alertas/fuentes/twitter_api.py`, `cuentas/creador.py`, `cuentas/perfilador.py`, `web/operaciones/monitor.py`

### Migracion de cuentas
- `migrar_cuentas.py`: importa cuentas + cookies del proyecto original (GestorTwitter). Se migraron 32 cuentas Twitter (28 del config + 4 cookies extra) a SQLite con cookies en `data/cookies/twitter/`

### Pendiente
- Reemplazar tokens placeholder en `.env` por claves reales (Telegram, Gemini, Grizzly)
- Probar acciones Selenium en VPS (requiere Chrome; local sin Chrome)
- `web_users.json`: cambiar usuario/clave admin por defecto antes de exponer el dashboard
