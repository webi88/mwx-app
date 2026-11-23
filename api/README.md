# MWX APP API

API HTTP (FastAPI) que consume el ejecutable **MWX APP**. Corre como
**servicio separado** del bot/dashboard y solo lee/escribe en la misma base de
datos del proyecto (`DATABASE_URL`, PostgreSQL/Supabase o SQLite).

## Endpoints

Todos los errores usan `{"ok": false, "error": "..."}`.

| Método | Ruta | Auth | Descripción |
|--------|------|------|-------------|
| GET | `/health` | — | Healthcheck de Railway. Responde `{"ok":true,"servicio":"mwx-api","version":"1.0"}` sin tocar la BD (no falla si la BD está lenta/caída). |
| POST | `/v1/login` | — | Credenciales + `machine_id` → token HMAC, campaña y cuentas asignadas (activas, twitter). Rate limit 10 fallos / 5 min por username+IP. |
| GET | `/v1/perfil` | Bearer | Mismo payload que login **sin** `password` en cuentas (incluye `totp_secret`, `auth_token`, `cookies_json`, `user_agent`). Revalida usuario activo + `token_version`. |
| POST | `/v1/estado` | Bearer | `{"ok":true,"activo":true,"nombre":"..."}`; 401/403 si fue revocado/desactivado. |
| GET | `/v1/imagenes/{nombre}` | Bearer | Imagen de campaña: se busca **primero en la BD** (`app_imagen`) y, si no está, en el archivo local `data/mwx_app/{username}/imagenes/{nombre}` (compatibilidad). Nombre validado con `core.app_imagenes.nombre_seguro` (sin `/`, `\`, `..`, espacios ni acentos). |
| POST | `/v1/chat/completions` | Bearer | Proxy OpenAI: reenvía a `api.openai.com` con la key real del servidor. Modelos permitidos: `gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini`, `gpt-4.1`; `max_tokens <= 4096`. |
| POST | `/v1/evento` | Bearer | Guarda `AppEvento` (`detalle` truncado a 2000). |

- Token: HMAC-SHA256 (core/app_auth.py), duración `MWX_TOKEN_HORAS` (default 12).
- El login registra un `AppEvento` tipo `login` con `machine_id` (sin credenciales).
- El proxy nunca escribe prompts ni la key en logs.
- **Imágenes en la BD**: la API corre en un contenedor separado del dashboard y
  no comparte su disco, así que el binario vive en la tabla `app_imagen`
  (`core.app_imagenes` + `core.models.AppImagen`). El endpoint devuelve la
  respuesta de BD con `Content-Disposition: inline; filename="..."` y
  `Cache-Control: private, max-age=300`. El archivo local
  `data/mwx_app/{username}/imagenes/` queda solo como **fallback** para
  despliegues locales que aún no migraron las imágenes a la BD.
- **Healthcheck**: `/health` no requiere token ni consulta la BD; sirve para el
  chequeo de Railway y para monitoreo externo.

## Variables de entorno

| Variable | Obligatoria | Descripción |
|----------|-------------|-------------|
| `DATABASE_URL` | sí | PostgreSQL de Supabase (`postgresql://...?sslmode=require`) o SQLite. |
| `SECRET_KEY` | sí | Firma de los tokens (cambiar el default). |
| `OPENAI_API_KEY` | no | Key real usada por `/v1/chat/completions`. Si falta responde **503**. |
| `MWX_TOKEN_HORAS` | no | Horas de vigencia del token. Default `12`. |
| `MWX_RATE_LIMIT_INTENTOS` | no | Fallos de login permitidos por username+IP. Default `10`. |
| `MWX_RATE_LIMIT_MINUTOS` | no | Ventana del rate limit. Default `5`. |
| `PORT` | sí (Railway) | Puerto que asigna la plataforma; uvicorn lo usa tal cual. |

## Local

```bash
pip install -r requirements-api.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

Documentación interactiva: `http://localhost:8000/docs`.

## Railway (servicio nuevo)

1. Crear un **servicio nuevo** en el mismo proyecto de Railway (no reemplazar
   el del dashboard/bot).
2. Settings → Source: mismo repo, **Dockerfile path** `api/Dockerfile`
   (contexto = raíz del repo). Alternativa sin Docker:
   `pip install -r requirements-api.txt` + start command
   `uvicorn api.main:app --host 0.0.0.0 --port $PORT`.
3. Variables del servicio: `DATABASE_URL`, `SECRET_KEY`, `OPENAI_API_KEY`
   (opcional) y, si se desea, `MWX_TOKEN_HORAS`,
   `MWX_RATE_LIMIT_INTENTOS`, `MWX_RATE_LIMIT_MINUTOS`.
4. Settings → Networking → **Generate Domain** y asegurar **Target Port =
   `$PORT`** (8000 con la imagen incluida). La URL pública del servicio es la
   que el exe recibe en `MWX_API_URL`.
5. En el exe/config del cliente, para que el SDK de OpenAI use el proxy:

   ```text
   OPENAI_API_BASE=https://<host-api>/v1
   ```

   El SDK llamará a `{base}/chat/completions`; el token de la app viaja en
   `Authorization: Bearer <token>` y la API inyecta la key real.

### Build/run con Docker

```bash
docker build -f api/Dockerfile -t mwx-api .
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e DATABASE_URL="postgresql://..." \
  -e SECRET_KEY="..." \
  -e OPENAI_API_KEY="..." \
  mwx-api
```

## Notas de seguridad

- No hay endpoints administrativos: las cuentas/configuraciones se administran
  desde el dashboard (BD compartida).
- El login incluye `password` de cada cuenta según el contrato del exe; el
  perfil NUNCA la devuelve.
- Rotar `SECRET_KEY` invalida todos los tokens existentes.
- Las imágenes se guardan en la BD (compartida con el dashboard); el directorio
  local `data/mwx_app/` solo se consulta como fallback de compatibilidad.
- Los bytes de las imágenes nunca se escriben en los logs.
