# MEMORY — GestorRedes / MWX APP

## Estado (2026-11-27)
- Producción (Railway `mwx-app` / `mwx-api`) redeployada con releases por
  plataforma. Migración `app_config.sin_imagenes` aplicada; `JAP_API_KEY` en
  `mwx-app`; página `🛒 JAP` verificada en https://mwx.app (saldo $3.51).
- **Releases por plataforma** (sin migración de esquema): `version` Windows
  `1.1.4`; macOS `1.1.4-mac-arm64` / `1.1.4-mac-intel`. La API filtra con
  `?plataforma=` (Windows default; Mac envía su arch). Retención: 2 por
  plataforma, Windows no se borra al publicar Mac.
- Windows: 1.1.4 **obligatorio** en la BD (`sha256 ce8870a6...`, 34,253,279
  bytes); `/v1/app/version` y `/v1/app/descargar` 200.
- macOS: 1.1.4 arm64/Intel en la BD (31,759,474 / 33,057,623 bytes) y en
  GitHub `https://github.com/webi88/mwx-app/releases/tag/macos-1.1.4`.
  **Autoactualización macOS habilitada** (`puede_actualizarse` True;
  `consultar`/`descargar` usan plataforma; `preparar`/`lanzar` generan un
  `.sh` con backup/rollback, `ditto`, `xattr` y fallback a `~/Applications`).
  No se probó el reemplazo real en una Mac.
- Local: rotación de cuentas por Chrome (intercala), proxies prevalidados en
  caché y `sin_imagenes=True` sin proxy.
- JAP: `integraciones/jap.py` + página admin; presets económico
  9013/8860/8413/6601/9011 y seguro 10186/8861/8555/6602/8855. 18 tweets:
  50 likes+10 RT+5 comentarios+500 vistas = **$3.071**; 100/20/5/1000 =
  **$3.364**; saldo **$3.5098**. No hay X-México.
- Suite **6428/6428**; `compileall` OK. Rama `macos-build-1.1.4` fusionada a
  `main` y publicada; release Mac construido desde esa rama.

## Próximo paso único
- Probar en una Mac real: abrir la `.app`, login, Chrome y una campaña corta;
  luego publicar una versión Mac superior y verificar la autoactualización.

## Pendientes
- Probar el auto-update Windows 1.1.3 → 1.1.4 en una laptop.
- Probar una compra JAP pequeña real (likes/RT) y verificar estado/refill.
- Cambiar credenciales por defecto del dashboard y rotar secretos (repo
  público).
