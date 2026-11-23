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
- Suite: 5843/5843. Sin commit.

## Próximo paso único
- Recompilar `MWX APP` 1.1.0, publicar el ZIP en el panel como release
  obligatoria y probar Local con 2 navegadores y 1–2 cuentas.

## Pendientes
- Redeploy Railway (dashboard + API) para releases, updater y verificación.
- Las 4 cuentas siguen en Supabase si existen: borrar allí con `DATABASE_URL`.
- Con máxima velocidad el riesgo de bloqueos de X sigue; no hay evasión ni
  captcha automático: la verificación es manual desde el celular.
