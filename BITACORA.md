# Bitacora del Proyecto GestorRedes

Sistema de gestion y automatizacion de redes sociales controlado desde Telegram.
Plataformas: Twitter/X, Facebook, Instagram, TikTok. Contenido generado con Gemini.

---

## 1. Resumen de Estado Actual (19-Ago-2026)

| Componente | Estado | Detalle |
|---|---|---|
| Bot de Telegram | Funcional | Arranca y responde, 1 usuario admin registrado |
| Base de datos | Creada | `data/gestor_redes.db` (SQLite) |
| Cuentas migradas | 32 | Todas Twitter, grupo A, **todas INACTIVAS** |
| Cookies | Copiadas | 32 `.pkl`, la mayoria muy pequenas (388-493 B) = sesiones vacias |
| Celulas | 0 | Aun no se configuran clientes |
| Scheduler | Vacio | 0 tareas programadas |
| Alertas | Vacio | 0 historial |
| Creacion de cuentas | Bloqueado | Grizzly SMS sin saldo |
| Driver Selenium | Roto | "Binary Location Must be a String" |

---

## 2. Linea de Tiempo

### 18-Ago-2026 — Montaje inicial (~13:36)
- Scaffolding del proyecto: estructura de carpetas (`bot/`, `core/`, `plataformas/`,
  `alertas/`, `scheduler/`, `cuentas/`, `ia/`, `utils/`), `requirements.txt`,
  `Dockerfile`, `docker-compose.yml`, `.env.example`, `README.md`.
- Nucleo base: `core/config.py`, `core/models.py`, `core/auth.py`.

### 18-Ago-2026 — Desarrollo de modulos (~13:50–14:20)
- `bot/handlers/ayuda.py` (guia visual de comandos).
- Modulos de automatizacion: bots Selenium por plataforma, campanas Change.org
  (`cuentas/change_org.py`), registro en Twitter (`cuentas/twitter_signup.py`).

### 18-Ago-2026 — Sistema de alertas y refinamiento (~14:47–14:50)
- Alertas: deduplicacion, filtros geo/tematicos, notificador, fuentes
  (Google News RSS + Twitter API), motor.
- `core/database.py`, `core/monitor_actividad.py`, `cuentas/grizzly_api.py`.
- Handlers de celulas y reportes.

### 18-Ago-2026 — Primera prueba del bot (~15:00–15:10)
- Primer arranque registrado en `data/logs/bot_test.log`.
- Usuario admin registrado: **AxelOUYI** (telegram_id `5746247004`).
- **Fallo**: intento de crear cuenta con Grizzly -> "Sin saldo en Grizzly".
- **Bug detectado**: `/stats` -> `AlertaHistorial no tiene atributo 'fecha'`.
- Monitor: "No hay cuentas o lectoras configuradas" (aun sin cuentas).

### 18-Ago-2026 — Migracion desde GestorTwitter (~16:16–16:26)
- Se creo `migrar_cuentas.py`: lee el `config.json` original, inserta cuentas en
  SQLite como plataforma `twitter` y copia los `.pkl` a `data/cookies/twitter/`.
- Resultado: **32 cuentas migradas** con cookies copiadas (origen: cookies del
  11-Ago 11:51, sesiones del proyecto antiguo).
- Actualizacion de `keyboards.py`, `main.py`, `comandos.py`, `cuentas.py`.

### 18-Ago-2026 — Verificacion de cuentas (~16:29–16:30)
- Ejecucion de `/cuentas_verificar`: fallo masivo del driver Selenium
  **"Binary Location Must be a String"** (no encuentra el ejecutable de Chrome).
- Las 32 cuentas quedaron marcadas como **INACTIVAS** (posiblemente por fallo
  de verificacion, no por suspension real).

---

## 3. Estado de la Base de Datos

### Cuentas (32)
- Todas `plataforma=twitter`, `grupo=A`, `activa=0` (INACTIVAS).
- Cookies path: `data/cookies/twitter/<usuario>.pkl`.

### Usuarios (1)
- `id=1`, telegram_id `5746247004`, rol `admin` (AxelOUYI).

### Vacias
- `celulas`, `clientes`, `tareas`, `alertas_historial`, `menciones_dia`, `reportes_diarios`.

---

## 4. Problemas Pendientes

1. **Selenium: "Binary Location Must be a String"**
   - Ocurre en `plataformas/twitter/selenium_bot.py:iniciar_driver`.
   - La configuracion de la ruta del binario de Chrome esta vacia o mal
     tipada (espera string, recibe None/otro tipo).
   - Bloquea verificacion y publicacion. **Prioridad alta.**

2. **Grizzly SMS sin saldo**
   - `/crear_cuentas` no puede obtener numeros virtuales.

3. **Bug en `/stats`**
   - `type object 'AlertaHistorial' has no attribute 'fecha'` en
     `bot/handlers/comandos.py:737` — el modelo SQLAlchemy no tiene la columna
     `fecha` (se consulta en clase, no en instancia).

4. **Errores menores del bot**
   - "Query is too old" (callback expirado).
   - "Message is not modified" (mismo markup enviado 2 veces).
   - "'NoneType' object has no attribute 'reply_text'" (update sin mensaje efectivo).

5. **Cookies probablemente expiradas**
   - 20 de 32 `.pkl` pesan ~388-493 B (sesiones vacias/expiradas del proyecto
     original). Solo ~9 pesan >1 KB (posibles sesiones validas).
   - Las pequenas de ~493 B y ~388 B son sospechosas de no tener sesion.

---

## 5. Proximos Pasos Sugeridos

1. Arreglar la ruta del binario de Chrome (configurar correctamente en
   `core/config.py` o `plataformas/base.py`).
2. Re-verificar cuentas con el driver funcionando para separar reales vs
   suspendidas/expiradas.
3. Depurar cookies: revisar que `.pkl` contienen sesion valida vs vacias.
4. Corregir bug de `/stats` (columna `fecha` en `AlertaHistorial`).
5. Configurar celulas/clientes y tags para las cuentas activas.
6. Reponer saldo de Grizzly o agregar cuentas manualmente si se necesita crear mas.

---

## 6. Comandos probados / funcionales

- `/start` -> menu principal con botones. **OK**
- `/stats`, `/monitor` -> funcionan pero con datos vacios/error conocido.
- `/crear_cuentas` -> bloqueado (Grizzly sin saldo).
- `/cuentas_verificar` -> falla por driver Selenium.

---

*Bitacora generada automaticamente a partir de logs, fechas de archivos y estado
de la base de datos.*