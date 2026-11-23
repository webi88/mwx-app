# MWX APP (escritorio)

Aplicacion de escritorio para **Windows 10/11 de 64 bits** que permite a una
persona no tecnica ejecutar su campana local: iniciar sesion, pulsar un boton,
ver el avance y obtener los enlaces de las publicaciones.

- Un solo Chrome **visible** en la computadora (sin proxies).
- La configuracion tecnica (hashtags, textos, URLs, imagenes y limites) la
  entrega el **servidor**; el usuario final no la escribe.
- El paquete **NO incluye cuentas** ni contrasenas: el servidor decide que
  cuentas tiene asignadas cada usuario.

---

## 1. Requisitos

| Elemento | Detalle |
|---|---|
| Sistema | Windows 10 u 11, 64 bits |
| Navegador | **Google Chrome instalado** |
| Internet | Necesario para entrar, descargar imagenes y generar textos |
| Python (solo build) | Python 3.11 de 64 bits |

La primera vez que se ejecute una campana, la app descarga el `chromedriver`
compatible y lo guarda en:

```
%LOCALAPPDATA%\MWX APP\bin
```

Esto puede tardar unos segundos. Las siguientes ejecuciones reutilizan el
archivo.

---

## 2. Compilar el .exe

En la raiz del proyecto:

```bat
build_mwx.bat
```

El script:

1. Crea un entorno limpio `.venv-mwx`.
2. Instala `requirements-mwx.txt` + PyInstaller + Pillow.
3. Genera el icono y el logo (`assets\generar_icono.py`).
4. Empaqueta con `mwx_app.spec` en modo **onedir**.

Resultado:

```
dist\MWX APP\MWX APP.exe
dist\MWX APP\_internal\...
```

Si existe un `config.json` en la raiz, el script lo copia junto al ejecutable.

Para regenerar solo el icono:

```bat
.venv-mwx\Scripts\python.exe assets\generar_icono.py
```

---

## 3. Entregar al cliente

Entrega **la carpeta completa** `dist\MWX APP\` (no solo el .exe) y un archivo
`config.json` al lado de `MWX APP.exe`:

```json
{
  "api_url": "https://tu-servidor-api.up.railway.app"
}
```

Reglas:

- `api_url` es la URL publica del servicio de la API (sin `/v1`; si se incluye,
  la app lo quita automaticamente).
- No entregues `.env`, `data\gestor_redes.db`, cookies, contrasenas ni claves.
- Las cuentas y la configuracion de campana se administran **desde el dashboard
  web**; el ejecutable solo las descarga al iniciar sesion.

Tambien se puede definir la URL con la variable de entorno `MWX_API_URL`
(tiene prioridad sobre `config.json`). Si falta, la pantalla de login muestra
un enlace discreto **⚙️ Configurar servidor**.

---

## 4. Uso para el usuario final

1. Abrir `MWX APP.exe`.
2. Escribir **Usuario** y **Contrasena** (boton **Entrar** o tecla Enter).
3. Revisar la pantalla **Tu campana**: cuentas por seccion, hashtags, cantidad
   de publicaciones por cuenta y duracion.
4. Pulsar el boton verde **▶ Iniciar campana**. Se abre Chrome y la campana
   comienza.
5. Seguir el avance en vivo. Si hace falta, pulsar **⛔ Detener** y confirmar.
6. Al terminar se muestra el resumen con **Exitosas / Fallidas / Omitidas** y
   la tabla de enlaces. Botones: **📋 Copiar enlaces**, **▶ Volver a iniciar**
   y **Cerrar sesion**.

Notas:

- La sesion se recuerda hasta **12 horas**; al reabrir la app se reconecta sola
  (si el servidor la revoco, pedira entrar de nuevo).
- Si el administrador habilito **ajustes**, aparece el boton **⚙️ Ajustes**
  para cambiar publicaciones por cuenta, pausa, duracion, tope diario y agregar
  imagenes. Si no esta habilitado, la app usa los valores del servidor.
- Los enlaces de RT/likes no se muestran porque esas acciones no generan una
  publicacion nueva.

---

## 5. Donde se guardan los datos

En una instalacion normal (ejecutable):

```
%LOCALAPPDATA%\MWX APP\
├── data\gestor_redes.db        Base local (se sincroniza desde la API)
├── data\mwx_machine.json       Identificador estable del equipo
├── data\mwx_sesion.json        Token de sesion (sin contrasena, 12 h)
├── data\mwx_recordar.json      Ultimo usuario recordado
├── data\logs\mwx_app.log       Registro para soporte (nunca mostrar al cliente)
└── temp\mwx_img_*.jpg          Imagenes descargadas para la campana
```

Para borrar todo rastro local: cerrar la app y eliminar
`%LOCALAPPDATA%\MWX APP`. La sesion tambien se puede cerrar desde la app.

---

## 6. Problemas frecuentes

| Sintoma | Que hacer |
|---|---|
| "No hay conexion con el servidor" | Revisar internet y que `config.json` tenga la URL correcta. |
| "Usuario o contrasena incorrectos" | Verificar los datos con el administrador. |
| "Tu cuenta fue desactivada" | El administrador debe reactivarla en el dashboard. |
| "Tu sesion expiro, vuelve a entrar" | Volver a iniciar sesion (el token dura 12 h o fue revocado). |
| "Falta configurar el servidor" | Usar **⚙️ Configurar servidor** o entregar `config.json`. |
| No se abre Chrome / falla la publicacion | Confirmar que Google Chrome esta instalado y actualizado. |
| "Tus cuentas no estan listas para publicar" | El administrador debe asignar cuentas y configurar la campana. |

No se muestran tracebacks ni detalles tecnicos en pantalla. El archivo
`data\logs\mwx_app.log` sirve para que soporte diagnostique.

---

## 7. Seguridad

- El ejecutable nunca muestra contrasenas, tokens ni cookies.
- `mwx_sesion.json` guarda solo el token de sesion (no la contrasena).
- El token se usa como credencial del proxy de IA del servidor; la clave real
  de OpenAI vive **solo** en el servidor.
- No copiar a los equipos de los clientes: `.env`, base de datos del proyecto,
  cookies `.pkl`, proxies ni respaldos.

---

## 8. Estructura del modulo

```
mwx_app/
├── main.py          Punto de entrada (Tk + login)
├── config.py        URL de la API, machine_id y sesion local
├── api_client.py    Cliente HTTP con mensajes en espanol
├── store.py         Sincronizacion del perfil a la SQLite local
├── runner.py        Orquesta activaciones.local + proxy OpenAI + imagenes
├── ui.py            Pantallas Tkinter/ttk
└── theme.py         Tema oscuro MWX
```

La app reutiliza, sin modificarlos, `core/`, `ia/`, `plataformas/` y
`activaciones/local.py`.
