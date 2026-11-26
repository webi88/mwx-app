# MEMORY — GestorRedes / MWX APP

## Estado (2026-11-26)
- Producción (Railway `mwx-app` / `mwx-api`) redeployada: migración
  `app_config.sin_imagenes` aplicada en Supabase; ambos servicios SUCCESS.
  `JAP_API_KEY` configurada en `mwx-app`; página `🛒 JAP` verificada en
  https://mwx.app con saldo real $3.51 y cotizador de los 18 links.
- Windows: **MWX APP 1.1.4** publicado como obligatorio en la BD
  (`sha256 ce8870a6...`, 34,253,279 bytes). `/v1/app/version` y
  `/v1/app/descargar` responden 200. Incluye `sin_imagenes`, diagnóstico de
  navegadores y rotación Local.
- macOS: build **1.1.4 arm64 + Intel** vía GitHub Actions
  (`.github/workflows/build-mwx-macos.yml`), release GitHub
  `https://github.com/webi88/mwx-app/releases/tag/macos-1.1.4`
  (arm64 31.7 MB, intel 33.0 MB). Auto-update **DESACTIVADO** en Mac
  (actualización manual); config/sesión en `~/.mwx_app`; Chrome detectado en
  `/Applications/Google Chrome.app/...`.
- Local: `_partir_plan_por_navegador` intercala cuentas por Chrome (no agota
  una cuenta completa); proxies prevalidados en caché;
  `sin_imagenes=True` funciona sin proxy (`CHROME_SIN_IMAGENES=1`).
- JAP: `integraciones/jap.py` + página admin `🛒 JAP`. Presets económico
  9013/8860/8413/6601/9011 y seguro 10186/8861/8555/6602/8855. Costo de 18
  tweets: 50 likes+10 RT+5 comentarios+500 vistas = **$3.071**; 100/20/5/1000
  = **$3.364**; saldo **$3.5098**. No hay servicios X-México; comentarios en
  español = 6602 Custom USA o cuentas propias.
- Suite **6278/6278**; `compileall` global OK. Rama GitHub
  `macos-build-1.1.4` (NO mergeada a `main`).

## Próximo paso único
- Probar el `.app` en una Mac real (clic derecho > Abrir, login, Chrome y una
  campaña corta) y una compra JAP pequeña real (likes/RT) verificando estado.

## Pendientes
- Probar el auto-update de Windows 1.1.3 → 1.1.4 en una laptop.
- Cambiar `admin/admin` de producción y rotar credenciales (repo público).
- Mac: si se quiere auto-update, implementar releases por plataforma (hoy es
  manual).
