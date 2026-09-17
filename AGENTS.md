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

### Perfiles X: secciones CI/CD/IP y sincronizacion nombre/@ (2026-09-11)
- `core/models.py`: `Cuenta` agrega `seccion` (CI/CD/IP), `nombre_mostrado` y `handle_actual`. `usuario` sigue siendo la clave interna de login y NO se renombra (cookies, tareas y perfiles Chrome dependen de ella).
- `core/secciones.py` (nuevo): `SECCIONES` (CI=Centro-Izquierda, CD=Centro-Derecha, IP=Institución Privada), `normalizar_seccion()`, `etiqueta_seccion()`, `seccion_desde_sector()`.
- `core/database.py`: migracion de las 3 columnas en SQLite y PostgreSQL (`ADD COLUMN IF NOT EXISTS`); al crear `seccion` hace backfill desde `sector` (centroderecha→CD, centroizquierda→CI, privados→IP).
- `core/exportar.py`: Excel/CSV incluyen `Handle_Actual`, `Nombre_Mostrado`, `Seccion_CI_CD_IP`.
- `plataformas/twitter/perfil.py` (nuevo): lectura del perfil real via httpx con las cookies guardadas (`auth_token`+`ct0`) y proxy sticky por cuenta, sin Chrome: `extraer_datos_perfil()`, `obtener_perfil_http()`, `sincronizar_cuenta()`, `sincronizar_todas()` (persiste `nombre_mostrado`/`handle_actual`).
- `plataformas/twitter/selenium_bot.py`: `cambiar_nombre()`, `cambiar_handle()` (valida `^[A-Za-z0-9_]{4,15}$` y pide contrasena) y `cambiar_perfil()` (orquesta y persiste). ⚠️ Selectores nuevos pendientes de probar con Chrome real.
- `web/operaciones/cuentas.py`: reorganizada en 7 pestanas; nuevas "🗂️ Secciones CI/CD/IP" (asignacion masiva/individual + preclasificar por sector), "🔄 Sincronizar desde X" (httpx, muestra diffs de nombre/@) y "✏️ Cambiar nombre/@" (Selenium, con contrasena). Inventario con las columnas nuevas.
- `web/app.py`: la operacion se llama ahora "🗂️ Cuentas: Perfiles & Secciones".

### Generacion IA: fix de publicacion + un texto distinto por cuenta (2026-09-11)
- **Bug arreglado**: `web/operaciones/posts.py` llamaba `_mostrar_posts_generados()` a nivel de modulo; Streamlit solo ejecuta ese bloque en el primer import, asi que tras `st.rerun()` los posts generados nunca se mostraban ni se publicaban ("le pongo que genere y no lo manda"). Ahora se llama al final de `_generar_ia()`.
- `ia/prompts.py`: prompts especificos por tipo (`mantenimiento`, `activacion`, `narrativa`, `reposteo`, `blog`) via `get_prompt_por_tipo()`; pide la cantidad exacta de textos distintos separados por `---`.
- `ia/generador_contenido.py`: `ultimo_error` con la causa real (el dashboard la muestra con `st.error`); compatibilidad con openai 0.28 y >=1.0; `generar_contenido` garantiza la cantidad pedida (rellena con variaciones/fallback local); nuevo `generar_pool_por_cuenta(base, n_cuentas, ...)` (firma congelada).
- `web/operaciones/_helpers.py`: `generar_pool_por_cuenta_seguro()` y `ejecutar_en_cuentas()` acepta lista de acciones (una por cuenta).
- `web/operaciones/posts.py`: pestana IA con multiselect de cuentas, editor de textos (`st.data_editor`) y publicacion "un texto distinto por cuenta" (+ boton "mismo texto"); checkbox de variaciones en Publicar Texto.
- `web/sidebar.py`: "Generar Contenido" ahora ejecuta la generacion ahi mismo (antes solo programaba y mandaba a otra pagina).
- `web/operaciones/calendario.py`: checkbox "variar texto por cuenta" que crea una tarea por cuenta con su propio texto.

### CLI de cuentas (`cli_cuentas.py`)
- `python cli_cuentas.py listar|seccion|preclasificar|sincronizar|cambiar-perfil` con filtros `--usuarios/--todas/--status/--seccion` y `--dry-run` donde aplica. Reutiliza `core.secciones` + `plataformas.twitter.perfil` + `TwitterBot`.

### Registro de voz (politica/ciudadana) + identidades MC y nombres propuestos (2026-09-11)
- `core/models.py`: `Cuenta` agrega `tipo_cuenta` (`politica`/`ciudadana`), `nombre_propuesto` y `handle_propuesto`. `core/registros.py` (nuevo): `TIPOS_CUENTA`, `normalizar_tipo_cuenta()`, `etiqueta_tipo_cuenta()`.
- `core/exportar.py`: Excel/CSV incluyen `Tipo_Cuenta`, `Nombre_Propuesto`, `Handle_Propuesto`.
- `ia/prompts.py` + `ia/generador_contenido.py`: prompts aceptan `registro` (politico/institucional vs ciudadano/coloquial mexicano). `generar_pool_por_cuenta(..., registros=None)` genera un texto por cuenta respetando el registro de cada indice (lotes de 25 + fallback local).
- `cuentas/generador_identidades.py` (nuevo): identidades tipo `movimiento` ("Ciudadanía Feliz", "Ciudad Unida", "Movimientos Unidos"... sin "Movimiento Ciudadano" literal) y tipo `persona` (nombres mexicanos Faker es_MX); handles validos 4-15 chars. Funciones `generar_identidad(es)`, `asignar_propuestas()`, `aplicar_propuesta()` (Selenium), `descartar_propuesta()`.
- `cuentas/importador.py`: `importar_lote(texto, seccion="", tipo_cuenta="")` permite ingresar lotes directo a CI/CD/IP y con registro (el lote de las 200 de MC se puede importar como CI).
- `web/operaciones/cuentas.py`: nuevas pestanas "🎭 Registro" y "🏷️ Nombres" (generar/editar propuestas, aplicar en X individual o en lote, descartar); importar con seccion/tipo del lote; inventario con tipo y propuestas.
- `web/operaciones/posts.py` y `calendario.py`: al variar textos, cada cuenta usa su registro.
- `cli_cuentas.py`: nuevos subcomandos `tipo`, `generar-nombres` (`--identidad auto|persona|movimiento`, `--dry-run`, `--json`) y `aplicar-nombres`; `listar` muestra tipo y propuesta.

### Seleccion masiva de cuentas por rango/cantidad (2026-09-11)
- `web/operaciones/cuentas.py`: helper `_selector_masivo()` con 4 modos (Filtro de todas las que cumplan / Rango de cuenta a cuenta por usuario o por numero / Cantidad primeras-ultimas N / Manual), integrado en las pestanas Secciones CI/CD/IP, Registro, Nombres y Sincronizar. Los flujos individuales se conservan.
- `cli_cuentas.py`: opciones `--desde USUARIO --hasta USUARIO --limite N` (inclusive, orden alfabetico case-insensitive) en `seccion`, `tipo`, `generar-nombres` y `sincronizar`.

### Mantenimiento programado + activacion por roles + fix de sesion/carga (2026-09-11)
- **Mantenimiento organico**: `core/models.py` Cuenta agrega `personalidad` y `rol_activacion` (+ migracion y export). `cuentas/generador_identidades.py`: `generar_personalidad()` y `asignar_personalidades()`. `ia/prompts.py`/`ia/generador_contenido.py`: temas (`azteca`/`dia`/`tendencias`/`gustos`) y `generar_textos_mantenimiento()` (textos por cuenta respetando personalidad/registro, lotes <=15 + fallback local).
- `web/operaciones/posts.py`: pestana "🗓️ Mantenimiento Programado" — ventana 7:00-2:00 (configurable), slots aleatorios por cuenta con separacion minima, previsualizacion del plan y creacion de una `Tarea` por tuit en el scheduler.
- **Activacion por roles (subcuentas)**: `core/roles.py` (`cita`/`hashtags`/`rt`). `activaciones/motor.py`: `ejecutar_por_roles()` (quote-RT, hashtags+menciones o RT simple por subcuenta, cohortes y concurrencia). `web/operaciones/activacion_masiva.py`: pestana "🗂️ Por roles" con asignacion masiva de rol, reparto automatico en tercios, metricas por rol y campana; `rts.py` filtra cuentas por rol.
- **Fix sesion/lentitud**: `web/auth.py` token HMAC persistente (`crear_token_sesion`/`verificar_token_sesion`), `web/app.py` init cacheada con `st.cache_resource` y restauracion de sesion via `?s=` (recargar ya no bota al login), `web/sidebar.py` logout limpia el token, `web/operaciones/cuentas.py` renderiza solo la pestana activa (radio) y cachea `_listar_cuentas` (15s).

### Desactivar cuentas + fotos de perfil/portada + registro activista (2026-09-11)
- `core/models.py`: Cuenta agrega `banner_path` (+ migracion y export `Banner_Portada`). `core/registros.py` ahora tiene 3 registros: `politica`, `activista` (tecnico-coloquial, faltas leves) y `ciudadana` (coloquial, mas faltas); "ciudadania politica"->activista y round-trip de etiquetas.
- `ia/prompts.py`: reglas de registro para los 3 (`activista` con licencias q/pa/xq/tons ocasionales; `ciudadana` con faltas mas frecuentes). `ia/generador_imagenes.py`: `generar_imagen(..., size=, output_path=)` compatible openai 0.28/>=1.0.
- `cuentas/fotos.py` (nuevo): `generar_foto_perfil/portada`, `generar_fotos`, `aplicar_foto_perfil/portada`, `aplicar_fotos` (avatares 1024x1024 en `data/avatares/`, portadas 1536x1024 en `data/portadas/`; prompts por personalidad/tipo). `cuentas/generador_identidades.py`: personalidades para `activista`.
- `plataformas/twitter/selenium_bot.py`: `cambiar_foto_perfil()` y `cambiar_foto_portada()` (⚠ selectores pendientes de probar con Chrome real).
- `web/operaciones/cuentas.py`: pestanas nuevas "⏸️ Estado" (activar/desactivar masivo) y "📷 Fotos" (generar/aplicar/subir); registro con 3 tipos; inventario con activa/avatar/portada.
- `cli_cuentas.py`: subcomandos `desactivar`, `activar`, `generar-fotos`, `aplicar-fotos`; `listar --estado` y columnas estado/fotos.

### Reparto por hora + 3 perfiles de redaccion (2026-09-14)
- **Requerimiento**: por cuenta y por hora 7 acciones de texto (posts + comentarios aleatorios: 4+3, 5+2 o 6+1) y 15-20 retweets, repartidos a lo largo de 60 min con pausas aleatorias; 3 perfiles de personalidad repartidos equitativamente (55/55/55 con 165 cuentas); hashtag obligatorio EN MEDIO del texto (nunca al final).
- `core/perfiles.py` (nuevo): `PERFILES_PERSONALIDAD` (formal/ciudadano/popular), `normalizar_perfil`, `etiqueta_perfil`, `perfil_coherente`, `distribuir_perfiles(n)`, `elegir_hashtag`, `tiene_hashtag`, `colocar_hashtag_en_medio`.
- `core/models.py` + `core/database.py`: columna nueva `Cuenta.perfil_personalidad` (migracion automatica). `core/exportar.py` la incluye como `Perfil_Personalidad`.
- `ia/prompts.py`: `bloque_estilo_perfil(perfil)` con el formato obligatorio (formal = Titulo/Descripcion/Conclusion; ciudadano = par de renglones intermedios; popular = un renglon casual con faltas intencionales q/pa/xq/tons/k) + regla universal de hashtag en medio + `get_prompt_comentario(...)`. `get_prompt_por_tipo`/`get_prompt_mantenimiento` aceptan `perfil` al final.
- `ia/generador_contenido.py`: plantillas locales por tema->perfil, `generar_textos_mantenimiento` respeta `perfil` y `tipo_accion` (post/comentario), `generar_textos_comentario(...)` nueva, `generar_pool_por_cuenta(..., perfiles=...)` agrupa por registro+perfil y `generar_variaciones_masivas(..., perfil=...)`; TODO texto final pasa por `colocar_hashtag_en_medio`.
- `scheduler/distribucion_horaria.py` (nuevo, funciones puras): `n_comentarios_hora`, `calcular_delays`, `generar_orden_horario`, `plan_hora_cuenta`, `plan_hora_lote`, `resumen_plan`, `construir_plan_completo`.
- `scheduler/models.py`: `crear_tarea_comentario`, `crear_tareas_desde_plan`. `scheduler/ejecutor.py`: tipo `comentario` (JSON url+texto -> `responder_tweet`), retweet de UNA url por tarea, pausas de cierre 2-15s. `scheduler/manager.py`: `max_instances=1`.
- `plataformas/twitter/selenium_bot.py`: `responder_tweet(url, texto, imagen_path=None)` con verificacion real (⚠ selectores pendientes de probar con Chrome); `solo_retwittear` verifica RT con `unretweet` y reporta `urls` bien.
- `cuentas/generador_identidades.py`: `asignar_perfiles_personalidad(...)` (reparte y respeta existentes), `rebalancear_perfiles(...)` (equilibra el conjunto completo 55/55/55 conservando perfiles validos), personalidades con vocabulario "popular" y `generar_personalidad(..., perfil=...)`.
- `web/operaciones/reparto_hora.py` (nuevo) + `web/app.py`: operacion "⏰ Reparto por Hora" (cuentas, ritmo 5 posts + 2 comentarios = 7/hora, 15-20 RTs, URLs objetivo de RTs y comentarios, horario, preview por cuenta y programacion de las Tareas).
- `web/operaciones/cuentas.py`: pestana "🎨 Perfiles" (histograma, reparto equitativo/rebalanceo y cambio rapido por cuenta). `web/operaciones/posts.py`, `calendario.py`, `_helpers.py`: los flujos existentes ya pasan `perfil` y garantizan hashtag en medio.
- ⚠ Rendimiento: cada accion abre/cierra Chrome (1 navegador por accion). 24 acciones/hora x 165 cuentas = ~4000 acciones/hora; escalar `MAX_BROWSERS` en VPS/Railway con cuidado y monitorear limites de X.

### Renombrar usuario interno + fix de sincronizacion del perfil real (2026-09-15)
- **Bug arreglado (causa raiz)**: `plataformas/twitter/perfil.py` enviaba los headers de API (`authorization: Bearer`, `x-twitter-auth-type: OAuth2Session` via `construir_headers`) a la pagina HTML `x.com/{usuario}`, por lo que X respondia **HTTP 401** y NINGUNA cuenta con `auth_token` sincronizaba ("no me extrae el cambio del usuario"). Ahora la lectura HTML usa headers de navegador + `cookie: auth_token=...; ct0=...` (ct0 se obtiene con `obtener_ct0` si falta) y funciona incluso cuando la cuenta ya cambio de @ (X sirve el perfil real desde la URL del handle viejo).
- `plataformas/twitter/perfil.py`: `obtener_perfil_http(..., handle_actual="", handle_propuesto="")` acepta candidatos y prueba `[usuario, handle_actual, handle_propuesto]`; el resultado incluye `consultado`. `sincronizar_cuenta` pasa los candidatos de la cuenta. Las claves existentes del dict se conservan.
- `core/renombrar.py` (nuevo): `renombrar_usuario(usuario_actual, nuevo_usuario, dry_run=False)` renombra la clave interna `Cuenta.usuario` migrando cookies `data/cookies/{plataforma}/{usuario}.pkl`, perfil Chrome, avatar/portada (`data/avatares/`, `data/portadas/`), `cookies_path`/`avatar_path`/`banner_path`, y `RegistroAccion.usuario`. Valida colision case-insensitive, soporta `dry_run`, nunca lanza. `renombrar_al_handle_actual(usuario, dry_run=False)` usa el `handle_actual` de la BD. Las `Tarea` referencian por id: no se tocan.
- `web/operaciones/cuentas.py`: nueva pestana **"🏷️ Renombrar usuario"** (selector de cuenta, sincronizar perfil, dry-run, renombrado real con confirmacion, y lote "Renombrar al @ real" para cuentas donde `handle_actual != usuario`). En "🔄 Sincronizar desde X" aparece aviso + boton "Ir a Renombrar usuario" cuando hay cambios de @. En "✏️ Cambiar nombre/@" hay checkbox opcional para renombrar tambien la clave interna al nuevo @.
- `cli_cuentas.py`: subcomando `renombrar --usuario X (--nuevo Y | --actual) [--dry-run]`.
- Verificado: compileall global OK; sync real de 3 cuentas (todas devolvieron nombre/@ nuevos, p.ej. NeraFarner -> @UnidosMovCDMX "Unidos en Movimiento"); E2E de renombrado con cuenta temporal (archivos y BD migrados, limpieza, colision bloqueada); AppTest de las 13 pestanas de Cuentas con 0 excepciones.

### Anti-deteccion: UA por cuenta + cookies completas + stealth compartido (2026-09-15)
- `utils/anti_detection.py` (nuevo): `stealth_script()`/`aplicar_stealth(driver)` (webdriver=undefined, plugins, languages, window.chrome, WebGL), `plataforma_desde_ua`, `chrome_version_desde_ua`, `aplicar_user_agent(driver, ua)` (CDP `Emulation.setUserAgentOverride` + `userAgentMetadata`/brands), `normalizar_cookie(s)` (formatos de vendedor: `expirationDate`->`expiry`, `sameSite`, `hostOnly`/`session`; defaults `.x.com`/`/`) y `resolver_ua_cuenta` (BD -> `ua_config.txt` -> "").
- Los 4 bots siguen con `undetected_chromedriver` (uc 3.5.5) y ahora aplican `aplicar_stealth` tras crear el driver (FB/IG/TikTok tambien). Verificado con Chrome 152 real headless: `navigator.webdriver = null`, `navigator.userAgent` exacto al configurado, `platform=Win32`.
- **UA por cuenta**: `Cuenta.user_agent` (columna nueva + migracion + export `User_Agent`). `TwitterBot._obtener_ua_consistente()` lee el UA de ESA cuenta; fallback a `data/perfiles_chrome/ua_config.txt` (override manual) y, si no hay, NO fuerza UA (natural de Chrome); ya NO genera ni persiste un UA aleatorio global. `iniciar_driver` solo agrega `--user-agent=` si hay UA y llama `aplicar_user_agent`.
- `cuentas/importador.py`: acepta 8 campos (`...:cookies:user_agent`), campo `ua=`/`user_agent=` en cualquier posicion, 7º campo `Mozilla/...` como UA, y cookies en **JSON crudo** (`[...]` o `{"cookies":[...]}`) ademas de base64; `importar_una` guarda el UA sin pisarlo con vacios.
- **Cookies completas**: `login_con_cookies_json` inyecta TODAS las cookies normalizadas (auth_token, ct0, twid, ...); si el `.pkl` falla, `login_con_cookies()` cae a `login_con_cookies_json()` (antes devolvia False). `api_http.py` usa el UA de la cuenta (sin random) y cae a `cookies_json` si no hay `.pkl`.
- `web/operaciones/cuentas.py`: nueva pestana **"🛡️ Anti-deteccion"** (estado de cookies/UA por cuenta, guardar/limpiar User-Agent, pegar cookies completas en JSON/base64 con preview, pasos de verificacion manual), columna `user_agent` en el inventario y caption de importacion actualizado (8 campos).
- Verificado: compileall global OK; E2E de importacion con UA + cookies completas (auth_token/ct0/twid) en BD y limpieza; AppTest de las 14 pestanas de Cuentas con 0 excepciones; prueba Chrome real de stealth/UA.

### Flujo guiado completo de perfil X (2026-09-15)
- `plataformas/twitter/selenium_bot.py`: nuevo `actualizar_perfil_completo(foto_perfil_path, foto_portada_path, nombre, bio, ubicacion, handle, password="", dry_run=False, callback=None)` que ejecuta EN ORDEN: login con cookies -> perfil propio -> foto de perfil -> foto de portada -> bio -> ubicacion -> nombre mostrado (modal "Edit profile") -> More -> Settings and privacy -> Your account -> Account information (pide contrasena) -> Username -> cambio de @ con verificacion. Fallbacks a los metodos directos (`_cambiar_foto`, `cambiar_nombre`, `cambiar_handle`) si el modal o la ruta no aparecen; selectores bilingues (es/en) con `data-testid` + aria-label + texto. `dry_run=True` comprueba los controles sin subir/escribir/enviar nada. Persiste `nombre_mostrado`/`handle_actual` y guarda cookies solo con cambios reales. Devuelve `{"ok", "login", "foto_perfil", "foto_portada", "nombre", "bio", "ubicacion", "handle", "pasos": [{"paso","ok","detalle"}], "error"}`.
- `web/operaciones/cuentas.py`: nueva pestana **"🧾 Perfil completo"** (uploaders de foto de perfil y portada con preview, nombre, bio max 160, ubicacion, nuevo @, contrasena precargada de BD), botones "Verificar selectores (dry-run)" y "Ejecutar flujo completo en X" con progreso por pasos (callback), resumen persistente y opcion de renombrar la clave interna al nuevo @.
- Verificado: compileall global OK; dry-run real con Chrome 152 headless sobre una cuenta real: los 7 pasos ok (login con cookies, avatar, portada, bio, ubicacion, nombre y ruta Ajustes hasta el prompt de contrasena, SIN enviarla y sin modificar la cuenta); AppTest de las 15 pestanas de Cuentas con 0 excepciones + smoke de la pestana nueva.

### Activaciones: posts de hashtag con IA por registro + hashtags en citas + rondas continuas (2026-09-17)
- **Bug arreglado**: los posts del rol "hashtags" copiaban/pegaban el texto base (`_pool_hashtags` solo concatenaba base+tags+menciones). Ahora `activaciones/motor.py::_generar_textos_por_rol` llama a `ia.generador_contenido.generar_textos_hashtags_por_cuenta(cuentas_info, hashtags, contexto, n_por_cuenta=1, ...)` y genera un texto ORIGINAL por cuenta según su registro (politica/activista/ciudadana), perfil (formal/ciudadano/popular) y personalidad, a partir del "contexto" (ej. "gran deporte que tenemos como el futbol") y del hashtag pedido (ej. #mexico), siempre bien escrito e integrado EN MEDIO. Fallback: si la IA falla, rellena con `_pool_hashtags` (que se conserva intacto) y agrega las menciones al final.
- `ia/prompts.py` (nuevo `get_prompt_hashtags()`): prompt "POST ORIGINAL CON HASHTAG" que prohíbe copiar el contexto y exige los hashtags exactos en medio, con reglas de registro y bloque de perfil. `ia/generador_contenido.py` (nuevo `generar_textos_hashtags_por_cuenta()` + `_PLANTILLAS_HASHTAGS`): lotes de ≤15 cuentas, unicidad, garantía del hashtag (`colocar_hashtag_en_medio`) y fallback local bueno por registro (politica impecable, activista técnico-coloquial, ciudadana con errores humanos legibles).
- **Bug arreglado**: a los retweets con cita les faltaban los `#`. `_garantizar_hashtags_texto()` agrega 1-2 hashtags al final del texto SOLO si no trae ninguno de los pedidos (tanto en `ejecutar` —nuevo parámetro `hashtags`— como en `ejecutar_por_roles`).
- **Modo continuo por rondas** (`repetir=True` en `ejecutar` y `ejecutar_por_roles`): worker-pool con `self.max_concurrente` navegadores y cola compartida; al terminar una ronda, se REGENERAN los textos de la siguiente (nuevas variaciones/IA) y se vuelve a repartir hasta agotar `duracion_min`; las cuentas que no alcanzan el deadline no abren navegador. El resumen incluye `rondas` y los detalles pueden traer `ronda`. `repetir=False` conserva el comportamiento anterior (cohortes + retardos).
- **Filtro "todas las cuentas publican"** (`solo_con_registro=True`): las cuentas sin registro definido (politica/activista/ciudadana) NO hacen nada (no abren navegador) y se reportan en `sin_registro`, `sin_registro_usuarios` y `sugerencia_registro`. Disponible en ambas pestañas.
- `web/operaciones/activacion_masiva.py`: pestaña Cita masiva con hashtags para citas, "Navegadores simultáneos" (default `MAX_BROWSERS`, máx 30; Railway=3), "🔁 Repetir hasta agotar el tiempo" (default ON) y "📢 Todas las cuentas publican"; pestaña Por roles con "Contexto de los posts con hashtag", navegadores, repetir y "todas las cuentas"; métricas de rondas y sin registro en resultados.
- Verificado: compileall global OK; 19/19 checks de integración sin Chrome (reloj falso, monkeypatch): rondas ≥2 con textos distintos por ronda, citas con #mexico, posts hashtag con #mexico distintos del contexto, cuentas sin registro nunca ejecutadas, callback `total >= hechas`, y modos clásicos intactos; llamada real OpenAI de `generar_textos_hashtags_por_cuenta` (texto ciudadano con #mexico); AppTest de la operación Activación Masiva con 0 excepciones y los controles nuevos presentes.

### Pendiente
- Reemplazar tokens placeholder en `.env` por claves reales (Telegram, Gemini, Grizzly)
- Probar acciones Selenium en VPS (requiere Chrome; local sin Chrome)
- `web_users.json`: cambiar usuario/clave admin por defecto antes de exponer el dashboard
