# MEMORY — GestorRedes / MWX APP

## Estado (2026-11-23)
- Cajón de verificación humana: columnas en `Cuenta`, `core/verificacion.py`,
  pestaña `Cuentas → 🚦 Verificación`, integración en Local/motor (login,
  onboarding, anti-bot y bloqueo real de X: se marca, se salta y no se reintenta).
- Ejecutable: tabla `AppRelease`, `core/app_releases.py`, API pública
  `/v1/app/version` y `/v1/app/descargar`, pestaña superadmin
  `⬇️ Ejecutable / Actualizaciones`, autoactualizador `mwx_app/updater.py`
  (`__version__ = 1.1.2`) y `empaquetar_mwx.py`.
- Local: 1–3 Chrome visibles, reparto por cuenta, contadores con lock y orden
  de cuentas aleatorio en cada ciclo. Defaults de máxima velocidad intactos.
- Baja local aplicada: tinnietinka, jerielizmarie3, neriepalma, epyputra.
  Respaldo: `data/backups/eliminacion_cuentas_20261123_074650.json`.
- Suite: 5855/5855. Cambios pusheados a `origin/main`.
- `MWX APP` 1.1.2 compilado, selftest OK y release publicado en producción
  (obligatorio, 34,246,536 bytes, sha256 `392e6ed5...`). Incluye refresco de
  configuración cada 10 s, chequeo de versión cada 20 s con la app abierta y
  updater corregido: consola oculta, espera por PID y cierre inmediato.
- Railway: API y dashboard redeployados; `/v1/app/version` y
  `/v1/app/descargar` responden 200.
- Hotfix: migración PostgreSQL `DATETIME -> TIMESTAMP`; `verificacion_fecha`
  ya existe en Supabase y la consulta ORM de cuentas funciona.

## Próximo paso único
- Instalar 1.1.2 manualmente una vez (el updater viejo puede colgarse), luego
  probar el auto-update de la siguiente versión y Local con 2 navegadores.

## Pendientes
- Validar el cajón con un bloqueo real de X y reactivar la cuenta.
- Las 4 cuentas siguen en Supabase si existen: borrar allí con `DATABASE_URL`.
- Con máxima velocidad el riesgo de bloqueos de X sigue; no hay evasión ni
  captcha automático: la verificación es manual desde el celular.
