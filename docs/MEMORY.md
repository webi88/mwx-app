# MEMORY — GestorRedes / MWX APP

## Estado (2026-11-23)
- Cajón de verificación humana: columnas en `Cuenta`, `core/verificacion.py`,
  pestaña `Cuentas → 🚦 Verificación`, integración en Local/motor (login,
  onboarding, anti-bot y bloqueo real de X: se marca, se salta y no se reintenta).
- Ejecutable: tabla `AppRelease`, `core/app_releases.py`, API pública
  `/v1/app/version` y `/v1/app/descargar`, pestaña superadmin
  `⬇️ Ejecutable / Actualizaciones`, autoactualizador `mwx_app/updater.py`
  (`__version__ = 1.1.0`) y `empaquetar_mwx.py`.
- Local: 1–3 Chrome visibles, reparto por cuenta, contadores con lock y orden
  de cuentas aleatorio en cada ciclo. Defaults de máxima velocidad intactos.
- Baja local aplicada: tinnietinka, jerielizmarie3, neriepalma, epyputra.
  Respaldo: `data/backups/eliminacion_cuentas_20261123_074650.json`.
- Suite: 5843/5843. Commits `bdbc252` + merge `bb0d327` pusheados a `origin/main`.
- `MWX APP` 1.1.0 compilado, selftest OK y release publicado en producción
  (obligatorio, 34,238,785 bytes, sha256 `5d13f3d1...`).
- Railway: API y dashboard redeployados; `/v1/app/version` y
  `/v1/app/descargar` responden 200.
- Hotfix: migración PostgreSQL `DATETIME -> TIMESTAMP`; `verificacion_fecha`
  ya existe en Supabase y la consulta ORM de cuentas funciona.

## Próximo paso único
- Probar el auto-update 1.1.0 en un equipo real y Local con 2 navegadores.

## Pendientes
- Validar el cajón con un bloqueo real de X y reactivar la cuenta.
- Las 4 cuentas siguen en Supabase si existen: borrar allí con `DATABASE_URL`.
- Con máxima velocidad el riesgo de bloqueos de X sigue; no hay evasión ni
  captcha automático: la verificación es manual desde el celular.
