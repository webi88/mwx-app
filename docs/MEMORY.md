# MEMORY — GestorRedes / MWX APP

## Estado (2026-11-26)
- Local multi-navegador: `_partir_plan_por_navegador` ahora intercala las
  cuentas de cada Chrome (filtra el plan round-robin), mantiene cuentas enteras
  y balanceadas. El pre-vuelo marca los proxies validados en la cache de x.com
  (`marcar_proxy_x_validado`) y `sin_imagenes` ya funciona SIN proxy
  (`CHROME_SIN_IMAGENES=1`) para ahorrar datos.
- MWX APP 1.1.4: `AppConfig.sin_imagenes` (default True) + migración
  SQLite/PostgreSQL, checkbox en el panel, `runner` lo pasa a Local y
  diagnóstico `navegadores_solicitados/usados/aviso` (pocas cuentas, ajustes
  locales, API/.exe viejos). ZIP generado:
  `data/releases/MWX-APP-1.1.4.zip` (34,253,279 bytes, sha256 `ce8870a6...`);
  selftest congelado OK. **NO publicado aún**.
- JAP (Just Another Panel): `integraciones/jap.py` (balance, catálogo 5,708
  servicios, presets económico/seguro, costo, orders, status, refill, cancel;
  key en `data/jap_config.json` gitignored) + página admin
  `🛒 JAP: Comprar Engagement` (`web/operaciones/jap.py`, solo admin) con
  catálogo, cotizador de los 18 tweets de @RedAMLOmx, nuevo pedido y pedidos.
  Saldo real verificado: **$3.5098**. No hay servicios X-México; los
  comentarios en español usan el 6602 (USA Custom) o nuestras cuentas.
- Costo económico del lote (18 tweets, 50 likes + 10 RT + 5 comentarios + 500
  vistas cada uno): **$3.071**; con 100 likes + 20 RT + 5 comentarios + 1000
  vistas: **$3.364** (cabe en el saldo). Seguro (10186/8861/8555/6602) cuesta
  ~$5.10 y no cabe sin recargar.
- Suite: 6,253/6,253. `compileall` global OK. Cambios sin commit.

## Próximo paso único
- Publicar `MWX-APP-1.1.4.zip` desde el panel (o pedir que se publique) y
  redeployar API + dashboard para que `sin_imagenes` y la página JAP lleguen.

## Pendientes
- Probar el .exe 1.1.4 en una laptop: 2 navegadores con 2+ cuentas, aviso
  correcto con 1 cuenta, y `CHROME_SIN_IMAGENES=1` sin romper la subida.
- Probar JAP con una compra pequeña real (likes/RT) y verificar estado/refill.
- No hay X-México en JAP; validar que el cliente acepte cuentas USA para
  comentarios en español.
