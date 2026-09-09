# Despliegue en Railway (24/7)

El proyecto corre en **un único contenedor** en Railway que levanta a la vez:
- **Dashboard web (Streamlit)** — la interfaz principal.
- **Scheduler** de tareas programadas.

**Ya no hay bot de Telegram**: la interfaz es el dashboard web.

> ⚠️ **Netlify NO sirve para esto.** Streamlit es un servidor Python con estado
> (no un sitio estático), así que no puede correr en Netlify (que solo aloja
> estáticos + serverless). El dashboard debe vivir en **Railway**. Netlify solo
> tendría sentido para una landing page aparte, no para la app.
>
> El "nombre/dominio" lo da Railway: te genera un dominio `*.up.railway.app`
> o puedes conectar tu propio dominio (Dominios → Custom Domain).

## Arquitectura

| Componente | Dónde corre |
|---|---|
| Dashboard web (Streamlit) | Railway (contenedor, puerto `$PORT`) |
| Scheduler (tareas programadas) | Railway (mismo contenedor, vía supervisord) |
| Base de datos | **Supabase (PostgreSQL)** |
| Cookies / perfiles Chrome / reportes | Volumen de Railway (`/app/data`) |
| Automatización Chrome (headless) | Railway (sin pantalla, con proxy) |

## Paso 1 — Variables de entorno (en Railway: Service → Variables)

| Variable | Valor |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:TU_PASSWORD@TU_HOST.supabase.co:5432/postgres` |
| `OPENAI_API_KEY` | tu clave de OpenAI |
| `HEADLESS` | `true` (en Railway no hay pantalla) |
| `MAX_BROWSERS` | `3` (2–4 recomendado; sube solo si el plan tiene más RAM) |
| `SECRET_KEY` | cadena aleatoria larga |
| `TELEGRAM_BOT_TOKEN` | *(opcional)* solo si quieres notificaciones de alertas por Telegram |
| `LOG_LEVEL` | `INFO` |
| `LOG_FILE` | `data/logs/gestor_redes.log` |
| `SMARTPROXY_*` | tus credenciales Smartproxy (o el proxy residencial que uses) |

> En Supabase: **Project Settings → Database → Connection string** → usa la
> cadena de **puerto 5432**. El código ya añade `sslmode=require` automáticamente
> si no viene en la URL. No pongas `.env` en el repo.
>
> ⚠️ **IPv6 en Railway**: Railway no tiene IPv6, y la conexión directa de
> Supabase (`db.xxx.supabase.co`) puede resolver a IPv6 y dar
> `Network is unreachable`. Dos soluciones (el Dockerfile ya incluye la #1):
> 1. *(ya aplicada)* El Dockerfile fuerza IPv4 vía `/etc/gai.conf`.
> 2. *(alternativa)* Usa la cadena de **Session pooler** de Supabase
>    (`aws-0-<region>.pooler.supabase.com:5432`), que es solo IPv4.
>    **No uses el Transaction pooler (6543)** con SQLAlchemy (rompe
>    `CREATE TABLE`/migraciones).

## Paso 2 — Crear el servicio

1. Sube el repo a GitHub.
2. Railway: **New Project → Deploy from GitHub repo**.
3. Railway detecta `railway.json` y construye con el **Dockerfile** (instala Chrome + dependencias Python).

## Paso 3 — Base de datos (Supabase)

1. Crea un proyecto en [supabase.com](https://supabase.com).
2. Copia la **connection string** (directa, puerto 5432).
3. Pégala en la variable `DATABASE_URL` del servicio de Railway.
4. La primera vez, `init_db()` crea las tablas automáticamente (`create_all`).

> Ya no necesitas SQLite en el volumen. El volumen queda solo para cookies,
> perfiles de Chrome y reportes.

## Paso 4 — Volumen persistente (cookies / perfiles / reportes)

Sin esto, las cookies y perfiles de Chrome se pierden en cada deploy:

1. Service → **Volumes → Add Volume**.
2. **Mount path**: `/app/data`.
3. Nombre sugerido: `gestor-data`.

`entrypoint.sh` siembra `/app/data` (proxies, `web_users.json`) la primera vez.

## Paso 5 — Exponer el dashboard

1. Service → **Networking → Generate Domain** (o Custom Domain).
2. Railway usa `$PORT` automáticamente (el `supervisord.conf` ya lo respeta con `%(ENV_PORT)s`).
3. Healthcheck: `/_stcore/health`.
4. Abre la URL → login con `admin` / `admin` (cámbialo en el dashboard → Admin).

## Paso 6 — Verificar

- **Dashboard**: abre el dominio → debe mostrar el login.
- **Scheduler**: revisa los logs → `Scheduler standalone en ejecución`.

## Notas importantes

- **RAM y Chrome**: cada Chrome headless consume bastante memoria. Con
  `MAX_BROWSERS=3` y un plan básico debería bastar para lotes pequeños; para
  activaciones masivas grandes, sube el plan (Pro) y ajusta `MAX_BROWSERS`.
- **IP de datacenter**: Railway usa IP de datacenter. El proxy residencial
  (Smartproxy/Bright Data) enmascara la IP, pero la automatización headless
  contra X.com puede tener menor tasa de éxito que tu Chrome local con
  pantalla. Para pruebas con pantalla visible, corre local con `HEADLESS=false`.
- **Sin bot de Telegram**: si quieres notificaciones de alertas por Telegram,
  deja `TELEGRAM_BOT_TOKEN` y configura los `chat_ids` de cada cliente.
- **Reinicio seguro**: supervisord + `restartPolicyType: ON_FAILURE` reinician
  el contenedor si algo falla.
