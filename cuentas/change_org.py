"""Reportes de violacion de politicas en Change.org + registro + granja.

Este modulo REEMPLAZA por completo al antiguo bot de FIRMAS (obsoleto). Tiene
tres misiones:

1. **Reportes**: `ChangeOrgReportBot` abre la peticion en Chrome, cierra el
   banner de cookies, localiza el enlace "Denunciar una violación de las
   políticas" (ES/EN, con scroll humano incremental), abre el modal "Denunciar
   abuso", marca el TERCER motivo ("No me gusta esta petición o no estoy de
   acuerdo con ella"), garantiza "México" en "¿Dónde vives?", escribe la queja
   generada por IA en el textarea que aparece y pulsa "Enviar". Marca exito SOLO
   con evidencia positiva ("Gracias por tomarte el tiempo de denunciar
   contenido...", URL de confirmacion o desaparicion del formulario sin error).
   Detecta captcha y errores de la pagina. Si al abrir la peticion Change.org
   responde con su pagina de ERROR DE APLICACION ("¡Oh no! Error del
   servidor..."), `_pagina_error_servidor()` la detecta por texto visible,
   hace UN `driver.refresh()` y, si persiste, falla con
   `_ERROR_SERVIDOR_PETICION` en vez del engañoso "no se encontro el enlace"
   (nunca busca el enlace ni el modal en esa pantalla). Timing/hidratacion
   (best-effort): antes de pulsar el enlace espera a que el panel de firma ya
   no tenga spinner y muestre texto/boton de firma (`_esperar_app_peticion`,
   `_JS_ESTADO_APP_PETICION`); si el modal no abre, `reportar()` reintenta con
   re-localizacion del enlace tras 3s, UNA recarga completa de la peticion y,
   como ULTIMO recurso, navega directo al `href` real del enlace (solo
   http(s) de change.org; nunca "#"/javascript). Si nada lo abre: "el
   formulario de reporte no aparecio (url=...)".

2. **Registro/Login**: `ChangeOrgReportBot.registrar_o_entrar()` crea la cuenta
   en Change.org (o entra a una existente) con el email/email_password de una
   cuenta de la BD via `https://www.change.org/login_or_join?user_flow=nav`:
   "Iniciar sesión" -> correo -> "Continuar" -> contraseña -> "Continuar" ->
   (solo cuentas nuevas) "Nombres"/"Apellidos" -> "Continuar". Si para una
   cuenta EXISTENTE Change muestra la pantalla que envio un codigo temporal al
   correo (sin campo de contraseña), se pulsa UNA vez la opcion "Ingresar con
   contraseña"/"Sign in with password" (`_pulsar_opcion_password`) y el bucle
   sigue con el campo de contraseña; si esa opcion no aparece en ~5 intentos,
   falla con `_ERROR_PANTALLA_CODIGO` (nunca con el timeout generico). Estado
   "nueva" si vio "Crea tu contraseña", "existente" si entro por login y "fallo"
   si no se pudo confirmar la sesion. `registrar_cuenta_change()` y
   `ejecutar_campana_registros()` orquestan el lote.
   Si Cloudflare interpone su reto anti-bot (widget Turnstile en sombra/iframe
   "protegida"), se detecta (`_detectar_reto_humano`/`_detectar_captcha`) y se
   falla con un error accionable (reintentar con otro proxy o con navegador
   visible) en vez de agotar el timeout. MODO ASISTIDO: con
   `esperar_captcha_seg > 0` y Chrome VISIBLE, el bot avisa al operador
   (`aviso({"tipo":"espera_captcha","usuario":...,"email":...,"segundos":N,
   "estado":"iniciando"})`), espera hasta N segundos a que un humano resuelva
   el reto y sigue solo cuando desaparece; en headless nunca espera (no hay
   quien lo resuelva) y el error lo aclara.

   **Falsos positivos de captcha**: `_detectar_captcha` mira SOLO el texto
   VISIBLE (nunca `page_source`/scripts, donde la palabra "recaptcha" aparece
   por config del sitio) y los terminos GENERICOS no cuentan como frase. Ante
   captcha/reto, el bucle de `registrar_o_entrar` comprueba PRIMERO
   `_evidencia_sesion(email)`: si la pagina ya muestra la sesion iniciada (home
   con avatar de cabecera y sin "Iniciar sesión"), confirma el login/registro y
   no bloquea con la espera asistida (caso real: cuenta YA REGISTRADA que se
   quedo 600s esperando por un `recaptcha` residual).

   **Errores de red/TLS (proxies rotos)**: tras navegar (y al inicio del
   registro) `_error_navegacion_pagina()` detecta las paginas de error de Chrome
   (`Privacy error`, `Your connection is not private`, `NET::ERR_CERT_*`,
   `ERR_TUNNEL_*`, `ERR_PROXY_*`, `This site can't be reached`, ...) por titulo
   y texto visible normalizados y falla con
   `"la conexion con change.org fallo (<codigo>): probable PROXY con TLS/red
   rota; prueba otro proxy"` en vez del engañoso "no se encontro el campo de
   direccion de correo electronico". Las campanas ademas validan cada proxy
   antes de usarlo (`proxy_change_valido`) y lo descartan si el nodo no da TLS.
   NADA de esto se confunde con captcha ni con campos faltantes.

3. **Granja de identidades**: `generar_identidad_change()` produce
   Nombre/Apellido/Correo/CP creibles (Faker es_MX) y
   `guardar_identidad_change()` las persiste en la tabla `cuenta_change` para
   las futuras firmas masivas (la columna `usada_firma` queda reservada).

4. **Sesiones persistentes (best-effort)**: una vez que un humano resolvio el
   captcha la primera vez, las siguientes corridas reutilizan la sesion y NO
   vuelven a pedir captcha. Sin columnas nuevas en la BD: las cookies viven en
   `data/cookies/change/{usuario}.json` (carpeta gitignored) via
   `guardar_sesion_change` / `cargar_sesion_change` / `borrar_sesion_change`.
   `ChangeOrgReportBot._restaurar_sesion_change()` inyecta las cookies por CDP
   (`Network.setCookie`), navega a la home y exige evidencia real de sesion
   (`_evidencia_sesion` / `_evidencia_home_logueada`, hasta ~8s). Con
   `CHANGE_REUTILIZAR_SESION=0` se desactiva guardar y restaurar (flujo
   clasico). Cloudflare ata la sesion a IP+UA: si la IP cambia puede volver a
   pedir el reto y el bot hara el login normal.

5. **Evidencia visual de los exitos**: cada reporte con `ok=True` guarda un
   screenshot de la pantalla de confirmacion en `_DIR_CAPTURAS_CHANGE`
   (`data/reportes/change/`, carpeta gitignored) como
   `change_{slug}_{YYYYmmdd_HHMMSS}.png` (slug saneado de `usuario` o `email`
   de la cuenta; con sufijo `_1`, `_2`... si el nombre se repite) y expone la
   ruta en la clave `captura` del resultado. En fallos, cancelaciones o
   early-returns `captura` queda `""`; `_capturar_evidencia()` nunca lanza y
   jamas loguea datos de la cuenta (solo la ruta del PNG).

Contrato congelado (el frontend y los tests dependen de el; no romper):

    MENSAJE_CANCELADO = "⛔ Ataque de reportes detenido por el usuario"

    password_change_para(email_password) -> str
        Determinista. "" -> "". Con len >= 10 se usa tal cual; si es mas corta
        se deriva ``email_password + "Change.org"`` (resultado SIEMPRE >= 10).

    generar_identidad_change(faker=None, usados=None) -> dict
        {"nombre","apellido","email","codigo_postal"}; email creible
        (nombre.apellido + 2-4 digitos @ gmail/hotmail/outlook/yahoo .com),
        sin acentos, unico contra `usados` y nunca vacio.

    proxies_disponibles(pais="") -> list[str]
        ProxyManager: cargar_por_pais(pais) si hay pais, si no cargar_proxies();
        nunca lanza -> [] si falla.

    proxy_change_valido(proxy, timeout=15) -> bool
        Comprueba que la salida del proxy abre
        "https://www.change.org/login_or_join?user_flow=nav" con `requests.get`
        (proxies http/https = la URL reconstruida desde
        `ProxyManager().analizar(proxy)`, `allow_redirects=True`). True si
        `status_code < 500`; False en SSLError/ConnectionError/timeout/errores.
        Cadena vacia -> True (sin proxy no se valida); `requests` ausente o
        proxy no analizable -> True (NO descartar por incertidumbre). NUNCA
        lanza. Cache por proceso {proxy: (bool, timestamp)} con TTL
        `PROXY_CHANGE_CACHE_SEG` (env, default 600; 0 = sin cache) y tope ~1000
        entradas (purga expirados y luego el mas viejo).

    guardar_identidad_change(identidad, url_peticion="", contexto="", queja="",
                             origen="reporte") -> dict
        {"guardada": bool, "id": int|None, "motivo": str}
        - email duplicado -> {"guardada": False, "motivo": "duplicada",
          "id": <id existente>} (NO es error)
        - fallo real -> {"guardada": False, "motivo": "error", "error": str}
        - si la tabla no existe (OperationalError) llama UNA vez a
          core.database.init_db() y reintenta. NUNCA lanza.

    guardar_sesion_change(usuario, driver, email="", proxy="") -> bool
        Escribe `data/cookies/change/{usuario}.json` con
        ``{"usuario","email","guardada"(ISO),"proxy","cookies":[...]}`` de
        forma atomica (temp + os.replace; 0600 en POSIX). NUNCA lanza y NO
        loguea valores de cookies; False sin cookies o con
        `CHANGE_REUTILIZAR_SESION` desactivado.

    cargar_sesion_change(usuario) -> dict | None
        Carga y valida el JSON (dict con "cookies" lista no vacia); None si no
        existe/es corrupto/invalido o si la reutilizacion esta desactivada.
        NUNCA lanza.

    borrar_sesion_change(usuario) -> bool
        Borra el archivo de sesion; True si lo elimino. NUNCA lanza.

    ChangeOrgReportBot(url_peticion="", contexto="", proxy="", headless=None,
                       timeout=45, cancelar=None, cuenta=None,
                       esperar_captcha_seg=None, aviso=None)
        `cuenta` = dict {"usuario","email","password" o "email_password",
                         "nombre","apellido","nombre_mostrado" (opcional)}.
        `esperar_captcha_seg`: segundos del MODO ASISTIDO; None -> env
            CHANGE_ESPERAR_CAPTCHA_SEG -> 0; acotado a [0, 900]. Con > 0 y
            Chrome visible, si aparece el reto anti-bot (Cloudflare/captcha) el
            bot emite `aviso({"tipo":"espera_captcha","usuario":...,"email":...,
            "segundos":N,"estado":"iniciando"})` UNA vez y hace polling cada 1s
            (cancelable) hasta que desaparezca; si se agota devuelve el error
            "verificacion anti-bot de Change.org (Cloudflare): el reto no se
            resolvio en Ns; usa el modo asistido con Chrome visible y
            resuelvelo a mano". Con 0 (default) se conserva el fallo clasico y
            en headless NUNCA espera (no hay humano).
        .preparar_driver() -> bool
        .registrar_o_entrar() -> {"ok","estado","error","evidencia",
                                  "sesion_restaurada"}
            estado in "nueva"|"existente"|"fallo"; nunca lanza. Con sesion
            persistida y evidencia real retorna de inmediato ok=True,
            estado="existente", evidencia="sesion restaurada (cookies)" y
            sesion_restaurada=True (sin escribir el correo ni pedir captcha).
        .reportar(identidad=None, queja="") -> dict
            Con `cuenta`: ejecuta registrar_o_entrar() tras preparar_driver();
            si el registro/login falla devuelve ok=False con ese error; si
            entra bien, sigue con el flujo de reporte (estado_cuenta incluido).
            Clave `captura`: ruta del PNG de evidencia cuando ok=True ("" en
            fallo, cancelado o early-returns); la clave SIEMPRE esta presente.
        ._error_navegacion_pagina() -> str
            Codigo (`ERR_CERT_AUTHORITY_INVALID`, ...) o etiqueta corta de la
            pagina de error de red de Chrome; "" si la pagina esta bien. Nunca
            lanza. `_navegar` y `registrar_o_entrar` lo usan: al detectarlo
            dejan en `ultimo_error` el mensaje accionable de proxy con TLS/red
            rota y fallan con el.
        ._pagina_error_servidor() -> str
            Frase detectada de la pagina de error de APLICACION de Change.org
            ("¡Oh no! Error del servidor...": "error del servidor", "algo salio
            mal", "oh no" con compania, ...); "" si la pagina esta bien. Mira
            SOLO el texto visible y NUNCA lanza.
        .cerrar() -> None   (driver.quit con fallbacks + cerrar SIEMPRE el fwd)

    registrar_cuenta_change(usuario="", email="", password="", nombre="",
                            apellido="", proxy="", headless=None,
                            cancelar=None, esperar_captcha_seg=None) -> dict
        Abre Chrome (crear_chrome + stealth + proxy), registra/entra y cierra
        SIEMPRE. NUNCA lanza. Resultado: {"ok","usuario","email","estado",
        "nombre","apellido","error","evidencia","url","cancelado",
        "sesion_restaurada"}.
        `esperar_captcha_seg` activa el MODO ASISTIDO dentro del bot.

    ejecutar_campana_registros(cuentas, max_workers=2, usar_proxies=True,
                               pais_proxy="", headless=None, cancelar=None,
                               callback=None, esperar_captcha_seg=None) -> dict
        `cuentas`: lista de dicts {"usuario","email","email_password" (o
        "password"),"nombre_mostrado" (opcional)}; lista cap 500 y workers
        acotados a [1,5]; proxy round-robin por cuenta: al elegir se valida con
        `proxy_change_valido` y los nodos con TLS/red rota se SALTAN (una sola
        validacion por proxy y corrida). Callback:
        {"tipo":"inicio","total":N}, por cuenta terminada {"tipo":"registro",
        "hechas":i,"total":N,"ok":bool,"usuario":str,"email":str,
        "estado":str,"detalle":str} (las omitidas, sin email/contraseña,
        tambien reportan con estado "omitida") y, si el modo asistido esta
        activo y aparece el reto anti-bot, {"tipo":"espera_captcha","hechas":i,
        "total":N,"usuario":str,"email":str,"detalle":"esperando captcha (Ns)"}.
        Resumen: {"total","exitosos","fallidos","nuevas","existentes",
        "omitidas","cancelada","resultados","proxies_total","sin_proxy",
        "proxies_descartados","error"}; `proxies_descartados` cuenta los
        proxies unicos descartados y `sin_proxy` las cuentas que corrieron sin
        proxy (porque ninguno valido o no habia). NUNCA lanza.

    ejecutar_un_reporte(url_peticion, contexto="", proxy="", headless=None,
                        evitar=None, cancelar=None, guardar_identidad=True,
                        cuenta=None, esperar_captcha_seg=None) -> dict
        Sin `cuenta`: genera identidad + queja IA, corre el bot y persiste la
        identidad en la granja si el reporte fue OK (comportamiento clasico).
        Con `cuenta`: NO llama a generar_identidad_change (usa los datos de la
        cuenta), NO guarda en la granja (identidad_guardada=False) y agrega
        "estado_cuenta", "usuario" y "sesion_restaurada" (True si el bot
        reutilizo las cookies persistidas) al resultado. La clave `captura`
        (ruta del PNG de evidencia o "") viaja tal cual desde `reportar()` en
        ambos modos. `esperar_captcha_seg`
        activa el MODO ASISTIDO del bot. Cierra SIEMPRE (finally).

    ejecutar_campana_reportes(url_peticion, contexto="", cantidad=5,
                              max_workers=2, usar_proxies=True, pais_proxy="",
                              guardar_identidades=True, headless=None,
                              cancelar=None, callback=None, cuentas=None,
                              esperar_captcha_seg=None) -> dict
        Con `cuentas` no vacias: cada reporte usa la siguiente cuenta en
        round-robin (login/registro primero); el evento "reporte" agrega
        "usuario" y "con_cuenta": True (sin "identidad") y el resumen agrega
        "con_cuentas" y "cuentas_total". Sin cuentas: flujo anonimo clasico.
        Los proxies se validan con `proxy_change_valido` al elegirlos (round-
        robin): los nodos con TLS/red rota se saltan y el resumen agrega
        `proxies_descartados` (unicos). Con el modo asistido activo tambien
        emite {"tipo":"espera_captcha","hechas":i,"total":N,"usuario":str,
        "email":str,"detalle":"esperando captcha (Ns)"}.

    _resolver_esperar_captcha_seg(valor=None) -> int
        `valor` None -> env CHANGE_ESPERAR_CAPTCHA_SEG -> 0; acota a [0, 900];
        valores invalidos -> 0.

Reglas de oro del flujo Selenium:
    - `driver.get` tolera `TimeoutException` (sigue con esperas explicitas).
    - Escritura humana REAL con `send_keys` caracter por caracter (los eventos
      reales de teclado son los que evitan el shadowban).
    - Exito jamas "por haber hecho clic": siempre con evidencia positiva.
    - `cancelar` (threading.Event) se revisa antes de abrir el navegador,
      durante el scroll y antes de enviar.
"""
from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote, urljoin, urlparse

from faker import Faker
from loguru import logger
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from core.config import detectar_chrome_version, resolver_ruta, settings
from core.database import obtener_sesion
from core.models import CuentaChange
from plataformas.chrome_driver import crear_chrome
from utils.proxies import ProxyManager

try:  # opcional: el bot funciona aunque el modulo no exista o falle el import
    from utils.anti_detection import aplicar_stealth
except ImportError:  # pragma: no cover - entorno sin anti_detection
    aplicar_stealth = None


__all__ = [
    "MENSAJE_CANCELADO",
    "password_change_para",
    "ChangeOrgReportBot",
    "generar_identidad_change",
    "proxies_disponibles",
    "proxy_change_valido",
    "guardar_identidad_change",
    "guardar_sesion_change",
    "cargar_sesion_change",
    "borrar_sesion_change",
    "registrar_cuenta_change",
    "ejecutar_un_reporte",
    "ejecutar_campana_reportes",
    "ejecutar_campana_registros",
]


MENSAJE_CANCELADO = "⛔ Ataque de reportes detenido por el usuario"

_URL_LOGIN_CHANGE = "https://www.change.org/login_or_join?user_flow=nav"

# Home de Change.org: destino de la restauracion de sesion por cookies.
_URL_HOME_CHANGE = "https://www.change.org/"

# Sesiones persistidas por cuenta (cookies con sesion iniciada). La carpeta ya
# esta en .gitignore; se guarda un archivo por cuenta y NO se agregan columnas
# a la BD. `_DIR_SESIONES_CHANGE` es modulo-global para poder monkeypatchearlo
# en los tests a un directorio temporal.
_DIR_SESIONES_CHANGE = resolver_ruta("data/cookies/change")

# Capturas (screenshots) de evidencia de los reportes EXITOSOS. La carpeta
# esta en .gitignore; `_DIR_CAPTURAS_CHANGE` es modulo-global para poder
# monkeypatchearlo en los tests a un directorio temporal.
_DIR_CAPTURAS_CHANGE = resolver_ruta("data/reportes/change")

_SUFIJO_PASSWORD_CORTA = "Change.org"

_DOMINIOS_EMAIL = ("gmail.com", "hotmail.com", "outlook.com", "yahoo.com")

_NOMBRES_FALLBACK = (
    "Maria", "Jose", "Guadalupe", "Fernanda", "Luis", "Ana", "Diego",
    "Sofia", "Rafael", "Carmen", "Alejandro", "Daniela",
)
_APELLIDOS_FALLBACK = (
    "Garcia", "Hernandez", "Lopez", "Martinez", "Rodriguez", "Perez",
    "Sanchez", "Ramirez", "Flores", "Gomez", "Torres", "Vazquez",
)

# Frases del enlace de reporte (ya normalizadas: minusculas y sin acentos).
_FRASES_ENLACE = (
    "denunciar una violacion de las politicas",
    "denunciar una violacion",
    "report a policy violation",
    "report policy violation",
    "report a violation",
)
# Señales (ya normalizadas) de que la app React de la peticion TERMINO de
# hidratar: el panel de firma muestra texto/boton de firma o el estado de
# usuario que YA firmo. Son best-effort para `_esperar_app_peticion` (el flujo
# NUNCA depende de ellas).
_FRASES_FIRMA = (
    "firma esta peticion",
    "firma la peticion",
    "firmar esta peticion",
    "firmar la peticion",
    "firmar ahora",
    "firmar",
    "sign this petition",
    "sign the petition",
    "sign now",
    "sign petition",
)
_FRASES_FIRMADO = (
    "has firmado",
    "ya firmaste",
    "firmaste esta peticion",
    "gracias por firmar",
    "has apoyado esta peticion",
    "you signed",
    "already signed",
    "thank you for signing",
)
# Poll JS del panel de firma de la peticion: devuelve
# {"panel": bool, "spinner": bool, "texto": str} o null. El marcador
# "change_org:app_peticion" permite a los tests (fakes) interceptarlo.
# La señal fuerte es un BOTON/ENLACE visible con "firm" en su texto (la app
# cliente-react ya monto el CTA); desde el se sube por ancestros hasta el panel
# (>=250x250) y ahi se busca un spinner/loading REAL. OJO: `[role=progressbar]`
# NO cuenta (la peticion tiene la barra "¡Alcancemos 200 firmas!" siempre
# visible y no es un spinner de carga).
_JS_ESTADO_APP_PETICION = """
try {
  /* change_org:app_peticion */
  var visible = function (el) {
    try {
      if (!el) { return false; }
      var estilo = window.getComputedStyle(el);
      if (!estilo || estilo.display === 'none' || estilo.visibility === 'hidden'
          || parseFloat(estilo.opacity || '1') === 0) { return false; }
      var rect = el.getBoundingClientRect();
      return rect.width > 2 && rect.height > 2;
    } catch (e) { return false; }
  };
  var firma = null;
  var candidatos = document.querySelectorAll("button, a, [role='button']");
  for (var i = 0; i < candidatos.length; i++) {
    var texto = String(candidatos[i].innerText || "").toLowerCase();
    if (texto.indexOf("firm") !== -1 && visible(candidatos[i])) {
      firma = candidatos[i];
      break;
    }
  }
  var panel = null;
  if (firma) {
    var nodo = firma;
    for (var sube = 0; sube < 8 && nodo; sube++) {
      nodo = nodo.parentElement;
      if (!nodo || nodo === document.body || nodo === document.documentElement) {
        break;
      }
      panel = nodo;
      var rect = nodo.getBoundingClientRect();
      if (rect.width >= 250 && rect.height >= 250) { break; }
    }
  }
  var alcance = panel || document.body || document.documentElement;
  var spinner = false;
  if (panel) {
    var posibles = panel.querySelectorAll(
      "[class*='spinner'], [class*='Spinner'], [class*='loading'], " +
      "[class*='Loading'], [class*='skeleton'], [class*='Skeleton'], " +
      "[class*='animate-spin'], svg[class*='spin']"
    );
    for (var k = 0; k < posibles.length; k++) {
      if (visible(posibles[k])) { spinner = true; break; }
    }
  }
  return {
    panel: !!panel,
    spinner: spinner,
    texto: String((alcance && alcance.innerText) || "")
  };
} catch (e) {
  return null;
}
"""
# Botones del banner de cookies.
_FRASES_BANNER = (
    "aceptar", "accept", "acepto", "got it", "entendido", "de acuerdo",
)
# Botones de envio del formulario.
_FRASES_ENVIAR = (
    "enviar denuncia", "enviar reporte", "enviar", "submit", "report", "send",
)
# Evidencia POSITIVA de exito (nada de "se hizo clic").
_FRASES_EXITO = (
    "gracias por tomarte el tiempo de denunciar contenido",
    "gracias por tomarte el tiempo",
    "denunciar contenido",
    "gracias por tu reporte",
    "gracias por tu denuncia",
    "gracias por denunciar",
    "hemos recibido tu denuncia",
    "hemos recibido tu reporte",
    "hemos recibido",
    "denuncia enviada",
    "denuncia recibida",
    "reporte enviado",
    "reporte recibido",
    "thank you for taking the time to report",
    "thank you for your report",
    "thank you for reporting",
    "we've received",
    "we have received",
    "report submitted",
    "report received",
)
_FRASES_URL_EXITO = (
    "thank", "gracias", "confirm", "success", "submitted", "recibido",
    "received",
)
# Errores explicitos de la pagina (normalizados).
_FRASES_ERROR = (
    "algo salio mal",
    "intenta de nuevo",
    "intenta nuevamente",
    "vuelve a intentarlo",
    "ocurrio un error",
    "ha ocurrido un error",
    "error al enviar",
    "no se pudo enviar",
    "try again",
    "something went wrong",
    "unable to submit",
    "please try again",
)
# Señales de captcha (normalizadas).
_FRASES_CAPTCHA = (
    "no soy un robot",
    "no soy robot",
    "no eres un bot",
    "pulsar y mantener",
    "press and hold",
    "demuestra que eres una persona",
    "verifica que eres humano",
    "verificar que eres humano",
    "verify you are human",
    "i'm not a robot",
    "i am not a robot",
    "recaptcha",
    "hcaptcha",
    "captcha",
)
# Terminos GENERICOS de captcha que NO cuentan como frase de TEXTO visible: los
# scripts/config/analytics del sitio mencionan "recaptcha"/"hcaptcha"/"captcha"
# aunque no haya captcha real (falso positivo reportado en la home logueada de
# Change.org, que dejaba el registro esperando 600s tras un login exitoso).
_FRASES_CAPTCHA_GENERICAS = ("captcha", "recaptcha", "hcaptcha")

# Paginas de error de red/TLS de Chrome (titulo y texto visible normalizados;
# los apostrofes se eliminan antes de comparar). NUNCA se confunden con
# captcha ni con "campo no encontrado".
_FRASES_ERROR_RED = (
    "privacy error",
    "your connection is not private",
    "this site cant be reached",
    "this site cannot be reached",
    "this site is not available",
    "the connection was reset",
    "la conexion no es privada",
    "este sitio no puede ser alcanzado",
    "no se puede acceder a este sitio",
    "err_cert",
    "err_tunnel",
    "err_proxy",
    "err_connection",
    "net::err_",
)
# Codigo especifico de Chrome/Chromium (p. ej. ERR_CERT_AUTHORITY_INVALID).
_PATRON_CODIGO_RED = re.compile(r"(?:net::)?(err_[a-z0-9_]+)")

# Pagina de error de APLICACION de Change.org ("¡Oh no! Error del servidor —
# Puede actualizar la pagina y si aun hay problemas inténtelo mas tarde...
# Volver a inicio"). Es una pantalla del sitio, NO la peticion: al buscar el
# enlace de reporte el bot daria el error generico engañoso "no se encontro el
# enlace". Se detecta SOLO por TEXTO VISIBLE (nunca `page_source` cuando hay
# body) y con combinaciones razonables para no marcar paginas normales:
# las frases FUERTES bastan solas; "oh no" y demas frases ambiguas exigen
# compania de otra señal de error en la MISMA pagina.
_FRASES_ERROR_SERVIDOR_FUERTES = (
    "error del servidor",
    "server error",
    "algo salio mal",
    "something went wrong",
)
_FRASES_ERROR_SERVIDOR_DEBILES = (
    "oh no",
    "actualiza la pagina",
    "actualizar la pagina",
    "intenta de nuevo mas tarde",
    "intentelo mas tarde",
    "try again later",
    "please try again later",
    "refresh the page",
)
# Companias que validan una frase DEBIL: "oh no" + "error"/"volver a inicio".
_FRASES_ERROR_SERVIDOR_CONTEXTO = (
    "error",
    "servidor",
    "volver a inicio",
    "volver al inicio",
    "pagina de inicio",
    "back to home",
    "home page",
    "no disponible",
)
# Mensaje claro cuando la app de Change.org falla al abrir la peticion (nunca
# el generico "no se encontro el enlace": la pagina jamas fue la peticion).
_ERROR_SERVIDOR_PETICION = (
    "Change.org devolvio un error de servidor al abrir la peticion "
    "(actualiza/reintenta mas tarde o usa otro proxy)"
)

# --------------------------------------------------------------------------- #
# Textos del flujo de registro/login (todos normalizados: sin acentos)
# --------------------------------------------------------------------------- #
# Botones para abrir el formulario de acceso.
_FRASES_LOGIN = (
    "iniciar sesion", "log in", "login", "sign in", "entrar",
)
# Botones "Continuar" (los sociales se descartan: Google/Facebook/etc).
_FRASES_CONTINUAR = ("continuar", "continue", "siguiente", "next")
_FRASES_CONTINUAR_EXCLUIR = (
    "google", "facebook", "apple", "twitter", "microsoft", "enlace",
    "magic link", "codigo", "code", "telefono", "phone",
)
# Pantallas detectadas por texto normalizado.
_FRASES_PANTALLA_NUEVA = (
    "crea tu contrasena",
    "crea una contrasena",
    "crear una contrasena",
    "create your password",
    "create a password",
    "choose a password",
    "elige una contrasena",
)
_FRASES_PANTALLA_NOMBRE = (
    "escribe tu nombre",
    "escribe tus nombres",
    "enter your name",
    "what's your name",
    "cual es tu nombre",
    "como te llamas",
)
# Pantalla de CUENTA EXISTENTE que pide un codigo temporal por correo (el
# formulario OCULTA la contraseña); abajo suele estar la opcion para volver a
# "Ingresar con contraseña". Todas normalizadas (sin acentos ni mayusculas).
_FRASES_PANTALLA_CODIGO = (
    "enviamos un codigo",
    "te enviamos un codigo",
    "hemos enviado un codigo",
    "enviamos un codigo temporal",
    "codigo temporal",
    "codigo de verificacion",
    "revisa tu correo",
    "revisa tu bandeja",
    "we sent a code",
    "sent you a code",
    "sent a temporary code",
    "temporary code",
    "verification code",
    "check your email",
)
# Opcion/enlace de esa pantalla para volver al login con contraseña
# (mas especifica primero: "contraseña" pelada queda al final).
_FRASES_OPCION_PASSWORD = (
    "ingresar con contrasena",
    "iniciar sesion con contrasena",
    "entrar con contrasena",
    "usar contrasena",
    "sign in with password",
    "log in with password",
    "use password",
    "enter password",
    "contrasena",
)
# Errores de login (normalizados) con mensaje claro para el UI.
_FRASES_ERROR_SESION = (
    "contrasena incorrecta",
    "clave incorrecta",
    "incorrect password",
    "wrong password",
    "invalid password",
    "contrasena invalida",
    "la contrasena es incorrecta",
    "no pudimos encontrarte",
    "no encontramos ninguna cuenta",
    "we couldn't find",
    "we could not find",
    "email no valido",
    "correo no valido",
    "invalid email",
)
# Señales que solo son error DESPUES de enviar la contraseña (en la pantalla
# "Crea tu contraseña" el texto de requisitos incluye "al menos 10 caracteres").
_FRASES_ERROR_SESION_TARDIAS = (
    "demasiados intentos",
    "too many attempts",
    "too many requests",
    "contrasena demasiado corta",
    "password is too short",
    "at least 10 characters",
    "al menos 10 caracteres",
)
# Motivo TERCERO del modal "Denunciar abuso" (radio 3).
_FRASES_MOTIVO_TERCERO = (
    "no me gusta esta peticion",
    "no estoy de acuerdo con ella",
    "no estoy de acuerdo",
    "no me gusta",
    "i don't like this petition",
    "i do not like this petition",
    "i disagree",
    "don't like",
)
# Frases que confirman que el modal "Denunciar abuso" esta ABIERTO segun el
# TEXTO de su dialogo. La deteccion por texto es OBLIGATORIA: en vivo
# `driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")` devolvio 0
# radios con el modal abierto (mientras `execute_script` +
# `document.querySelectorAll` si veia los 3), por lo que la presencia del
# formulario NO puede depender de `find_elements` / `is_displayed`.
_FRASES_MODAL_DENUNCIA = (
    "denunciar un abuso",
    "report abuse",
    "no me gusta esta peticion",
    "viola las normas de la comunidad",
    "contenido es ilegal",
)
# Valor real del tercer radio ("No me gusta esta petición...") en la UI de
# Change.org y su indice de respaldo cuando `value` no esta disponible.
_VALOR_MOTIVO_TERCERO = "dislike_content"
_INDICE_MOTIVO_TERCERO = 2
# Error claro cuando NINGUNA estrategia (Selenium -> etiqueta -> JS) logro
# marcar el motivo.
_ERROR_MOTIVO_NO_SELECCIONABLE = (
    "no se pudo seleccionar el motivo de denuncia "
    "(radios del modal no seleccionables)"
)
# JS puro (sin `find_elements`) para el modal: leer los radios del DOM,
# seleccionar el motivo 3 y devolver evidencia serializable. Se usa como
# respaldo cuando Selenium no "ve" los radios nativos del modal.
_JS_ESTADO_RADIOS = """
/* change_org:estado_radios */
var valor = %r;
var radios = Array.prototype.slice.call(
    document.querySelectorAll("input[type='radio']"));
var objetivo = null;
for (var i = 0; i < radios.length; i++) {
    if (String(radios[i].value || '') === valor) { objetivo = radios[i]; break; }
}
if (!objetivo && radios.length > %d) { objetivo = radios[%d]; }
function marcado(r) {
    if (!r) { return false; }
    if (r.checked) { return true; }
    return String(r.getAttribute('aria-checked') || '').toLowerCase() === 'true';
}
var alguno = false;
for (var j = 0; j < radios.length; j++) { if (marcado(radios[j])) { alguno = true; } }
return {
    total: radios.length,
    tercero: marcado(objetivo),
    alguno: alguno,
    textarea: !!document.querySelector('textarea, [contenteditable="true"]')
};
""" % (
    _VALOR_MOTIVO_TERCERO,
    _INDICE_MOTIVO_TERCERO,
    _INDICE_MOTIVO_TERCERO,
)
_JS_SELECCIONAR_MOTIVO = """
/* change_org:seleccionar_motivo */
var valor = %r;
var radios = Array.prototype.slice.call(
    document.querySelectorAll("input[type='radio']"));
var elegido = null;
for (var i = 0; i < radios.length; i++) {
    if (String(radios[i].value || '') === valor) { elegido = radios[i]; break; }
}
if (!elegido && radios.length > %d) { elegido = radios[%d]; }
if (!elegido) { return {ok: false, motivo: 'sin-radios'}; }
try {
    elegido.click();
    elegido.dispatchEvent(new MouseEvent('click', {
        bubbles: true, cancelable: true, view: window
    }));
} catch (e) {}
var marcado = !!elegido.checked
    || String(elegido.getAttribute('aria-checked') || '').toLowerCase() === 'true';
return {
    ok: true,
    checked: marcado,
    textarea: !!document.querySelector('textarea, [contenteditable="true"]')
};
""" % (
    _VALOR_MOTIVO_TERCERO,
    _INDICE_MOTIVO_TERCERO,
    _INDICE_MOTIVO_TERCERO,
)
# Etiqueta del campo "¿Dónde vives?" (select de ubicacion).
_FRASES_UBICACION = ("donde vives", "where do you live")
# Reto anti-bot de Cloudflare/Turnstile (normalizado). El widget vive en una
# sombra/iframe protegida: ademas del texto se detecta por JS y por selectores.
_FRASES_RETO_HUMANO = (
    "no eres un bot",
    "pulsar y mantener",
    "press and hold",
    "mantener pulsado",
    "comprobar que eres una persona",
    "demuestra que eres una persona",
    "verificar que eres humano",
    "verifica que eres humano",
    "verify you are human",
    "just a moment",
)
# Iteraciones (~10s) que se tolera el reto antes de fallar con error claro.
_ITERACIONES_ESPERA_RETO = 20
# Intentos (~5) que se tolera la pantalla de codigo sin la opcion de contraseña
# antes de fallar con el error claro del codigo de verificacion.
_INTENTOS_OPCION_PASSWORD = 5
# Error claro cuando Change.org pidio el codigo por correo y no aparece la
# opcion para volver a la contraseña (NUNCA el generico de timeout).
_ERROR_PANTALLA_CODIGO = (
    "Change.org pidio un codigo de verificacion por correo y no se encontro "
    "la opcion 'Ingresar con contrasena'"
)

# Etiquetas (normalizadas) que identifican cada campo del formulario.
_ETIQUETAS_CAMPOS = {
    "nombre": ("nombre", "first name", "given name", "name"),
    "apellido": ("apellido", "last name", "surname", "family name"),
    "email": ("correo", "email", "e-mail", "mail"),
    "motivo": (
        "motivo", "razon", "reason", "descripcion", "description",
        "mensaje", "message", "detalle", "detail", "explica", "cuentanos",
        "report", "denuncia",
    ),
}

# Selectores directos por campo (se prueban antes de las etiquetas).
_SELECTORES_CAMPOS = {
    "nombre": (
        (By.CSS_SELECTOR, "input[autocomplete='given-name']"),
        (By.CSS_SELECTOR, "input[name*='first']"),
        (By.CSS_SELECTOR, "input[id*='first']"),
        (By.CSS_SELECTOR, "input[name*='nombre']"),
        (By.CSS_SELECTOR, "input[id*='nombre']"),
        (By.CSS_SELECTOR, "input[placeholder*='nombre' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='name' i]"),
    ),
    "apellido": (
        (By.CSS_SELECTOR, "input[autocomplete='family-name']"),
        (By.CSS_SELECTOR, "input[name*='last']"),
        (By.CSS_SELECTOR, "input[id*='last']"),
        (By.CSS_SELECTOR, "input[name*='apellido']"),
        (By.CSS_SELECTOR, "input[id*='apellido']"),
        (By.CSS_SELECTOR, "input[placeholder*='apellido' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='last' i]"),
    ),
    "email": (
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.CSS_SELECTOR, "input[autocomplete='email']"),
        (By.CSS_SELECTOR, "input[name*='email']"),
        (By.CSS_SELECTOR, "input[id*='email']"),
        (By.CSS_SELECTOR, "input[name*='correo']"),
        (By.CSS_SELECTOR, "input[id*='correo']"),
        (By.CSS_SELECTOR, "input[placeholder*='correo' i]"),
        (By.CSS_SELECTOR, "input[placeholder*='mail' i]"),
    ),
    "motivo": (
        (By.CSS_SELECTOR, "textarea[name*='reason']"),
        (By.CSS_SELECTOR, "textarea[name*='description']"),
        (By.CSS_SELECTOR, "textarea[name*='message']"),
        (By.CSS_SELECTOR, "textarea[name*='detail']"),
        (By.CSS_SELECTOR, "textarea[id*='reason']"),
        (By.CSS_SELECTOR, "textarea"),
        (By.CSS_SELECTOR, "[contenteditable='true']"),
        # Modal real de Change.org: el campo opcional "Seleccionaste: ..." es
        # un input de React Aria (`name="question1.answer"`), NO un textarea.
        (By.CSS_SELECTOR, "[role='dialog'] input[name*='answer']"),
        (By.CSS_SELECTOR, "input[name*='question'][name*='answer']"),
        (By.CSS_SELECTOR, "input[name*='answer']"),
        (By.CSS_SELECTOR, "[role='dialog'] input[type='text']"),
    ),
}


# --------------------------------------------------------------------------- #
# Utilidades de texto / aleatoriedad
# --------------------------------------------------------------------------- #
def _normalizar(texto) -> str:
    """Minusculas, sin acentos y con espacios colapsados (nunca lanza)."""
    try:
        sin_acentos = unicodedata.normalize("NFKD", str(texto or ""))
        limpio = "".join(c for c in sin_acentos if not unicodedata.combining(c))
        return " ".join(limpio.lower().split())
    except Exception:
        return ""


def _slug_email(texto) -> str:
    """Fragmento valido para un email: solo [a-z0-9] (sin acentos ni espacios)."""
    return re.sub(r"[^a-z0-9]+", "", _normalizar(texto))


def _slug_captura(cuenta) -> str:
    """Fragmento saneado para el nombre del screenshot de un reporte.

    Prefiere "usuario" y cae a "email" del dict de la cuenta; deja solo
    ``[A-Za-z0-9_.-]`` (espacios, ``/`` y ``@`` pasan a ``_``), recorta
    separadores de los extremos y nunca queda vacio (respaldo "cuenta").
    Nunca lanza.
    """
    try:
        datos = dict(cuenta) if isinstance(cuenta, dict) else {}
        origen = str(datos.get("usuario") or datos.get("email") or "")
        limpio = re.sub(r"[^A-Za-z0-9_.-]+", "_", origen).strip("_.-")
    except Exception:
        limpio = ""
    return limpio or "cuenta"


def _dato_faker(fake, metodo: str, respaldo: tuple) -> str:
    """Lee un dato de Faker con respaldo local; nunca vacio."""
    if fake is not None:
        try:
            valor = str(getattr(fake, metodo)() or "").strip()
            if valor:
                return valor
        except Exception:
            pass
    return random.choice(respaldo)


def _normalizar_usados(usados) -> set:
    """Convierte `usados` (str, list, set o dicts con 'email') en set normalizado."""
    resultado = set()
    if not usados:
        return resultado
    if isinstance(usados, str):
        usados = [usados]
    try:
        for item in usados:
            valor = item.get("email", "") if isinstance(item, dict) else item
            valor = str(valor or "").strip().lower()
            if valor:
                resultado.add(valor)
    except TypeError:
        pass
    return resultado


def _notificar(callback, info: dict) -> None:
    """Llama al callback de progreso sin dejar que un error rompa la campana."""
    if callback is None:
        return
    try:
        callback(info)
    except Exception as e:
        logger.warning(f"Change.org: el callback de progreso fallo: {e}")


def _resolver_esperar_captcha_seg(valor=None) -> int:
    """Resuelve el MODO ASISTIDO: kwarg > env > 0, acotado a [0, 900].

    `None` lee `CHANGE_ESPERAR_CAPTCHA_SEG` (default 0). Valores invalidos o
    negativos -> 0; mayores a 900 -> 900. Determinista y nunca lanza.
    """
    if valor is None:
        valor = os.environ.get("CHANGE_ESPERAR_CAPTCHA_SEG", "0")
    try:
        segundos = int(float(str(valor).strip()))
    except (TypeError, ValueError):
        segundos = 0
    return max(0, min(900, segundos))


# --------------------------------------------------------------------------- #
# Sesiones persistentes de Change.org (cookies por cuenta, sin columnas en BD)
# --------------------------------------------------------------------------- #
def _reutilizar_sesion_activo() -> bool:
    """True si se guardan/restauran sesiones (env CHANGE_REUTILIZAR_SESION).

    Default "1" (activo). Con "0"/"false"/"no"/"off" se desactiva TODO:
    `guardar_sesion_change`, `cargar_sesion_change` y la restauracion en
    `ChangeOrgReportBot`. Nunca lanza.
    """
    try:
        valor = os.environ.get("CHANGE_REUTILIZAR_SESION")
        if valor is None:
            return True
        return str(valor).strip().lower() not in ("0", "false", "no", "off")
    except Exception:
        return True


def _ruta_sesion_change(usuario: str) -> str:
    """Ruta del JSON de sesion de UNA cuenta (nombre saneado, nunca vacio).

    El archivo vive en `_DIR_SESIONES_CHANGE` (`data/cookies/change/`), se
    lee el modulo-global en cada llamada (testeable con monkeypatch) y solo
    usa `[A-Za-z0-9_.-]` en el nombre: sin separadores de ruta, sin escapar
    del directorio y nunca vacio (respaldo "cuenta"). Nunca lanza.
    """
    try:
        limpio = re.sub(r"[^A-Za-z0-9_.-]", "_", str(usuario or ""))
    except Exception:
        limpio = ""
    if not limpio:
        limpio = "cuenta"
    try:
        carpeta = _DIR_SESIONES_CHANGE
    except Exception:  # pragma: no cover - defensa extrema
        carpeta = ""
    return os.path.join(str(carpeta or ""), f"{limpio}.json")


def _escribir_json_atomico(ruta: str, datos: dict) -> bool:
    """Escribe `datos` como JSON en `ruta` de forma ATÓMICA (temp + replace).

    Crea la carpeta si falta; en POSIX deja el archivo con permisos 0600.
    Nunca lanza ni deja archivos temporales huerfanos.
    """
    temporal = None
    try:
        carpeta = os.path.dirname(ruta) or "."
        os.makedirs(carpeta, exist_ok=True)
        fd, temporal = tempfile.mkstemp(
            prefix=".sesion-change-", suffix=".tmp", dir=carpeta
        )
        with os.fdopen(fd, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, ensure_ascii=False)
        if os.name == "posix":
            try:
                os.chmod(temporal, 0o600)
            except OSError:
                pass
        os.replace(temporal, ruta)
        temporal = None
        return True
    except Exception as e:
        logger.debug(
            f"Change.org: no se pudo escribir la sesion ({type(e).__name__}): {e}"
        )
        return False
    finally:
        if temporal:
            try:
                os.remove(temporal)
            except OSError:
                pass


def guardar_sesion_change(
    usuario: str, driver, email: str = "", proxy: str = ""
) -> bool:
    """Guarda las cookies de la sesion actual en `data/cookies/change/`.

    Lee `driver.get_cookies()` y escribe el JSON
    ``{"usuario","email","guardada" (ISO),"proxy","cookies":[...]}`` con
    escritura atomica (temp + `os.replace`; permisos 0600 en POSIX). NO
    loguea valores de cookies. Con `CHANGE_REUTILIZAR_SESION` desactivado no
    escribe nada. NUNCA lanza; devuelve True/False.
    """
    if not _reutilizar_sesion_activo():
        return False
    try:
        cookies = list(driver.get_cookies() or [])
    except Exception:
        return False
    if not cookies:
        return False
    try:
        datos = {
            "usuario": str(usuario or ""),
            "email": str(email or ""),
            "guardada": datetime.now().isoformat(timespec="seconds"),
            "proxy": str(proxy or ""),
            "cookies": cookies,
        }
        ruta = _ruta_sesion_change(usuario)
        ok = _escribir_json_atomico(ruta, datos)
    except Exception:  # pragma: no cover - defensa extrema
        return False
    if ok:
        # Solo el conteo: jamas los valores de las cookies.
        logger.debug(
            f"Change.org: sesion guardada para {usuario or '(sin usuario)'} "
            f"({len(cookies)} cookies)"
        )
    return ok


def cargar_sesion_change(usuario: str) -> dict | None:
    """Carga la sesion persistida de `usuario` (None si no hay o es invalida).

    Valida que el JSON sea un dict con ``"cookies"`` como lista no vacia. Con
    `CHANGE_REUTILIZAR_SESION` desactivado devuelve None aunque el archivo
    exista. NUNCA lanza.
    """
    if not _reutilizar_sesion_activo():
        return None
    try:
        with open(_ruta_sesion_change(usuario), "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
    except Exception:
        return None
    if not isinstance(datos, dict):
        return None
    cookies = datos.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    return datos


def borrar_sesion_change(usuario: str) -> bool:
    """Borra la sesion persistida de `usuario`; True si elimino el archivo.

    False si no existe o si no se pudo borrar. NUNCA lanza.
    """
    try:
        os.remove(_ruta_sesion_change(usuario))
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Registro/Login: contraseña determinista y nombres
# --------------------------------------------------------------------------- #
def password_change_para(email_password: str) -> str:
    """Deriva la contraseña de Change.org a partir de `email_password`.

    DETERMINISTA (misma entrada -> misma salida) para no guardar columnas
    nuevas en la BD:

      - vacia/None -> ``""`` (cuenta NO elegible para registrar/entrar).
      - con 10 o mas caracteres -> se usa tal cual.
      - con menos de 10 -> ``email_password + "Change.org"`` (el resultado
        SIEMPRE tiene >= 11 caracteres, minimo que exige Change.org).
    """
    base = str(email_password or "").strip()
    if not base:
        return ""
    if len(base) >= 10:
        return base
    return base + _SUFIJO_PASSWORD_CORTA


def _partir_nombre_mostrado(nombre_mostrado) -> tuple:
    """Parte "Nombre Apellido..." en (Nombres, Apellidos) si trae >=2 palabras."""
    partes = str(nombre_mostrado or "").split()
    if len(partes) >= 2:
        return partes[0], " ".join(partes[1:])
    return "", ""


def _resolver_nombres(nombre="", apellido="", nombre_mostrado="") -> tuple:
    """Resuelve (Nombres, Apellidos) NUNCA vacios.

    Precedencia: explicitos -> `nombre_mostrado` partido (>=2 palabras) ->
    Faker es_MX (via `generar_identidad_change`, reutilizado) -> respaldo fijo.
    """
    nombre = str(nombre or "").strip()
    apellido = str(apellido or "").strip()
    if not (nombre and apellido):
        parte_nombre, parte_apellido = _partir_nombre_mostrado(nombre_mostrado)
        if not nombre:
            nombre = parte_nombre
        if not apellido:
            apellido = parte_apellido
    if not nombre or not apellido:
        try:
            identidad = generar_identidad_change()
        except Exception:  # pragma: no cover - defensa extrema
            identidad = {}
        if not nombre:
            nombre = str(identidad.get("nombre") or "").strip()
        if not apellido:
            apellido = str(identidad.get("apellido") or "").strip()
    return nombre or "Usuario", apellido or "Change"


def _identidad_desde_cuenta(cuenta: dict) -> dict:
    """Identidad (solo metadata) de una cuenta de la BD, sin usar Faker/email.

    En modo cuenta NO se llama a `generar_identidad_change`: los nombres salen
    de `nombre`/`apellido` o de `nombre_mostrado` partido (pueden quedar vacios).
    """
    cuenta = dict(cuenta or {})
    nombre = str(cuenta.get("nombre") or "").strip()
    apellido = str(cuenta.get("apellido") or "").strip()
    if not (nombre and apellido):
        parte_nombre, parte_apellido = _partir_nombre_mostrado(
            cuenta.get("nombre_mostrado")
        )
        if not nombre:
            nombre = parte_nombre
        if not apellido:
            apellido = parte_apellido
    return {
        "nombre": nombre,
        "apellido": apellido,
        "email": str(cuenta.get("email") or "").strip(),
        "codigo_postal": "",
    }


# --------------------------------------------------------------------------- #
# Granja de identidades
# --------------------------------------------------------------------------- #
def generar_identidad_change(faker=None, usados=None) -> dict:
    """Genera una identidad sintetica para reportes/firmas de Change.org.

    Devuelve SIEMPRE ``{"nombre","apellido","email","codigo_postal"}`` con
    campos no vacios. El email es creible (nombre.apellido + 2-4 digitos en
    gmail/hotmail/outlook/yahoo), sin acentos, y no colisiona con `usados`
    (set/lista de emails ya generados).
    """
    fake = faker
    if fake is None:
        try:
            fake = Faker("es_MX")
        except Exception as e:
            logger.warning(f"Change.org: Faker es_MX no disponible ({e}); uso respaldo local")
            fake = None

    usados_set = _normalizar_usados(usados)
    nombre = ""
    apellido = ""
    email = ""
    dominio = _DOMINIOS_EMAIL[0]
    for _ in range(80):
        nombre = _dato_faker(fake, "first_name", _NOMBRES_FALLBACK)
        apellido = _dato_faker(fake, "last_name", _APELLIDOS_FALLBACK)
        slug_nombre = _slug_email(nombre)
        slug_apellido = _slug_email(apellido)
        if not slug_nombre or not slug_apellido:
            continue
        dominio = random.choice(_DOMINIOS_EMAIL)
        email = f"{slug_nombre}.{slug_apellido}{random.randint(10, 9999)}@{dominio}"
        if email.lower() not in usados_set:
            break
    else:
        # Colision persistente (practicamente imposible): refuerza el sufijo.
        slug_nombre = _slug_email(nombre) or random.choice(("usuario", "ciudadano"))
        slug_apellido = _slug_email(apellido) or random.choice(("mx", "mexico"))
        email = (
            f"{slug_nombre}.{slug_apellido}{random.randint(100000, 999999)}"
            f"@{random.choice(_DOMINIOS_EMAIL)}"
        )

    codigo_postal = ""
    if fake is not None:
        try:
            codigo_postal = str(fake.postcode() or "").strip()
        except Exception:
            codigo_postal = ""
    if not codigo_postal:
        codigo_postal = f"{random.randint(1000, 99999):05d}"

    return {
        "nombre": nombre or random.choice(_NOMBRES_FALLBACK),
        "apellido": apellido or random.choice(_APELLIDOS_FALLBACK),
        "email": email,
        "codigo_postal": codigo_postal,
    }


def proxies_disponibles(pais: str = "") -> list:
    """Proxies del pais pedido (o todos); nunca lanza -> [] si algo falla."""
    try:
        pm = ProxyManager()
        if pais:
            proxies = pm.cargar_por_pais(pais)
        else:
            proxies = pm.cargar_proxies()
        return [str(p) for p in (proxies or []) if p]
    except Exception as e:
        logger.warning(f"Change.org: no se pudieron cargar proxies ({pais!r}): {e}")
        return []


# --------------------------------------------------------------------------- #
# Validacion de proxies contra change.org (los nodos residenciales se rompen)
# --------------------------------------------------------------------------- #
_URL_VALIDACION_PROXY = _URL_LOGIN_CHANGE
_PROXY_CHANGE_CACHE: dict = {}          # proxy -> (bool, timestamp)
_PROXY_CHANGE_CACHE_LOCK = threading.Lock()
_PROXY_CHANGE_CACHE_MAX = 1000
_PROXY_CHANGE_CACHE_TTL_DEFAULT = 600   # PROXY_CHANGE_CACHE_SEG (10 min)


def _proxy_change_cache_seg() -> int:
    """TTL de la cache de validacion: env `PROXY_CHANGE_CACHE_SEG` -> 600.

    `0` (o negativo -> 0) desactiva la cache; valores no numericos o vacios
    devuelven el default (600). Nunca lanza.
    """
    try:
        valor = int(float(str(os.environ.get("PROXY_CHANGE_CACHE_SEG", "")).strip()))
    except (TypeError, ValueError):
        return _PROXY_CHANGE_CACHE_TTL_DEFAULT
    return max(0, valor)


def _url_proxy_para_requests(info: dict) -> str:
    """URL `scheme://user:pass@host:port` desde `ProxyManager().analizar()`."""
    scheme = str(info.get("scheme") or "http").strip() or "http"
    host = str(info.get("host") or "").strip()
    port = info.get("port")
    user = str(info.get("user") or "")
    password = str(info.get("password") or "")
    credenciales = ""
    if user:
        credenciales = quote(user, safe="")
        if password:
            credenciales += ":" + quote(password, safe="")
        credenciales += "@"
    return f"{scheme}://{credenciales}{host}:{port}"


def _guardar_cache_proxy(proxy: str, valido: bool) -> None:
    """Guarda el resultado en la cache del proceso (tope ~1000). Nunca lanza."""
    try:
        with _PROXY_CHANGE_CACHE_LOCK:
            if len(_PROXY_CHANGE_CACHE) >= _PROXY_CHANGE_CACHE_MAX:
                ahora = time.time()
                ttl = _proxy_change_cache_seg()
                expirados = [
                    clave
                    for clave, (_, marca) in list(_PROXY_CHANGE_CACHE.items())
                    if ttl <= 0 or (ahora - marca) >= ttl
                ]
                for clave in expirados:
                    _PROXY_CHANGE_CACHE.pop(clave, None)
                while len(_PROXY_CHANGE_CACHE) >= _PROXY_CHANGE_CACHE_MAX:
                    mas_viejo = min(
                        _PROXY_CHANGE_CACHE,
                        key=lambda clave: _PROXY_CHANGE_CACHE[clave][1],
                    )
                    _PROXY_CHANGE_CACHE.pop(mas_viejo, None)
            _PROXY_CHANGE_CACHE[proxy] = (bool(valido), time.time())
    except Exception:
        pass


def proxy_change_valido(proxy: str, timeout: int = 15) -> bool:
    """Comprueba si un proxy abre change.org sin errores de TLS/red.

    Hace ``requests.get("https://www.change.org/login_or_join?user_flow=nav",
    proxies={"http": url, "https": url}, timeout=timeout,
    allow_redirects=True)`` con la URL del proxy reconstruida desde
    ``ProxyManager().analizar(proxy)`` (soporta credenciales con caracteres
    especiales). Devuelve True si ``status_code < 500``; False en
    SSLError/ConnectionError/timeout/cualquier error.

    - Cadena vacia (o None) -> True: sin proxy no se valida.
    - `requests` no disponible o proxy no analizable -> True (NO descartar por
      incertidumbre).
    - Cache por proceso ``{proxy: (bool, timestamp)}`` con TTL
      `PROXY_CHANGE_CACHE_SEG` (env, default 600; 0 = sin cache) y tope ~1000
      entradas (purga primero los expirados y luego el mas viejo).
    NUNCA lanza.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return True
    ttl = _proxy_change_cache_seg()
    ahora = time.time()
    if ttl > 0:
        try:
            with _PROXY_CHANGE_CACHE_LOCK:
                entrada = _PROXY_CHANGE_CACHE.get(proxy)
            if entrada is not None and (ahora - entrada[1]) < ttl:
                return bool(entrada[0])
        except Exception:
            pass

    try:
        info = ProxyManager().analizar(proxy)
    except Exception:
        info = None
    if not info:
        # Formato desconocido: NO se descarta por incertidumbre (el forward
        # proxy local puede interpretarlo igual).
        return True
    try:
        import requests
    except Exception:
        return True

    url_proxy = _url_proxy_para_requests(info)
    try:
        respuesta = requests.get(
            _URL_VALIDACION_PROXY,
            proxies={"http": url_proxy, "https": url_proxy},
            timeout=timeout,
            allow_redirects=True,
        )
        valido = int(getattr(respuesta, "status_code", 0) or 0) < 500
    except Exception:
        valido = False
    if ttl > 0:
        _guardar_cache_proxy(proxy, valido)
    return bool(valido)


def guardar_identidad_change(
    identidad: dict,
    url_peticion: str = "",
    contexto: str = "",
    queja: str = "",
    origen: str = "reporte",
) -> dict:
    """Persiste una identidad de la granja en `cuenta_change`.

    - email duplicado -> ``{"guardada": False, "id": <existente>,
      "motivo": "duplicada"}`` (NO es error).
    - fallo real -> ``{"guardada": False, "id": None, "motivo": "error",
      "error": str}``.
    - tabla inexistente (OperationalError) -> llama UNA vez a
      ``core.database.init_db()`` y reintenta.
    NUNCA lanza.
    """
    identidad = dict(identidad or {})
    email = str(identidad.get("email") or "").strip().lower()
    if not email:
        return {
            "guardada": False,
            "id": None,
            "motivo": "error",
            "error": "la identidad no tiene email",
        }

    def _insertar() -> dict:
        with obtener_sesion() as db:
            existente = (
                db.query(CuentaChange)
                .filter(CuentaChange.email == email)
                .first()
            )
            if existente is not None:
                return {"guardada": False, "id": existente.id, "motivo": "duplicada"}
            registro = CuentaChange(
                nombre=str(identidad.get("nombre") or ""),
                apellido=str(identidad.get("apellido") or ""),
                email=email,
                codigo_postal=str(identidad.get("codigo_postal") or ""),
                url_peticion=str(url_peticion or ""),
                contexto=str(contexto or ""),
                queja=str(queja or ""),
                origen=str(origen or "reporte"),
            )
            db.add(registro)
            db.flush()
            return {"guardada": True, "id": registro.id, "motivo": ""}

    try:
        return _insertar()
    except Exception as e:
        # Importaciones aqui para poder clasificar el error real de SQLAlchemy.
        try:
            from sqlalchemy.exc import IntegrityError, OperationalError
        except Exception:  # pragma: no cover - sqlalchemy siempre esta
            return {"guardada": False, "id": None, "motivo": "error", "error": str(e)}

        if isinstance(e, IntegrityError):
            # Carrera: otro hilo/proceso inserto el mismo email entre el SELECT
            # y el commit. Se busca el id existente con una sesion nueva.
            try:
                with obtener_sesion() as db:
                    existente = (
                        db.query(CuentaChange)
                        .filter(CuentaChange.email == email)
                        .first()
                    )
                    if existente is not None:
                        return {
                            "guardada": False,
                            "id": existente.id,
                            "motivo": "duplicada",
                        }
            except Exception:
                pass
            return {"guardada": False, "id": None, "motivo": "duplicada"}

        if isinstance(e, OperationalError):
            # Puede ser "no such table: cuenta_change" (BD recien creada o
            # desplegada a medias): se crea el esquema UNA vez y se reintenta.
            try:
                from core.database import init_db

                init_db()
            except Exception as e_init:
                return {
                    "guardada": False,
                    "id": None,
                    "motivo": "error",
                    "error": f"init_db fallo: {e_init}",
                }
            try:
                return _insertar()
            except Exception as e_reintento:
                return {
                    "guardada": False,
                    "id": None,
                    "motivo": "error",
                    "error": str(e_reintento),
                }

        return {"guardada": False, "id": None, "motivo": "error", "error": str(e)}


# --------------------------------------------------------------------------- #
# Bot Selenium de reportes
# --------------------------------------------------------------------------- #
class ChangeOrgReportBot:
    """Automatiza el formulario de "Denunciar una violación de las políticas".

    Helpers internos (todos testeables con un FakeDriver):
      - `preparar_driver` / `cerrar`
      - `_navegar`, `_cerrar_banner_cookies`
      - `_buscar_enlace_reporte` (scroll humano) / `_clic_elemento`
      - `_resolver_campo` / `escribir_humano` / `_llenar_formulario`
      - `_buscar_boton_enviar` / `_esperar_confirmacion`
      - `_reporte_exitoso` / `_detectar_captcha` / `_detectar_error_envio`
      - `_capturar_evidencia` (screenshot de los reportes EXITOSOS)
      - `_capturar_fallo` (screenshot de DEPURACION de los fallidos)
    """

    def __init__(
        self,
        url_peticion: str = "",
        contexto: str = "",
        proxy: str = "",
        headless: bool = None,
        timeout: int = 45,
        cancelar=None,
        cuenta: dict = None,
        esperar_captcha_seg=None,
        aviso=None,
    ):
        self.url_peticion = str(url_peticion or "")
        self.contexto = str(contexto or "")
        self.proxy = str(proxy or "")
        self.headless = headless
        self.timeout = int(timeout or 45)
        self.cancelar = cancelar
        self.cuenta = dict(cuenta or {})
        self.esperar_captcha_seg = _resolver_esperar_captcha_seg(esperar_captcha_seg)
        self.aviso = aviso if callable(aviso) else None
        self.driver = None
        self._fwd_proxy = None
        self.ultimo_error = ""
        self.estado_cuenta = ""

    # ------------------------------------------------------------------ #
    # Ciclo de vida del navegador
    # ------------------------------------------------------------------ #
    def preparar_driver(self) -> bool:
        """Crea el Chrome (con proxy si hay) y el stealth. False si falla."""
        if self.driver is not None:
            return True
        try:
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1920,1080")
            headless = self.headless if self.headless is not None else settings.headless
            if headless:
                options.add_argument("--headless=new")

            if self.proxy:
                try:
                    pm = ProxyManager()
                    self._fwd_proxy = pm.aplicar_a_options(
                        options, self.proxy, tag="change"
                    )
                except Exception as e:
                    logger.warning(f"Change.org: no se pudo aplicar el proxy: {e}")
                    self._fwd_proxy = None

            self.driver = crear_chrome(
                options, version_main=detectar_chrome_version()
            )
            if aplicar_stealth is not None:
                try:
                    aplicar_stealth(self.driver)
                except Exception as e:
                    logger.debug(f"Change.org: stealth no aplicado: {e}")
            try:
                self.driver.set_page_load_timeout(max(10, self.timeout))
            except Exception:
                pass
            try:
                self.driver.set_script_timeout(max(10, self.timeout))
            except Exception:
                pass
            logger.info(
                "Change.org: navegador listo"
                + (f" (proxy {self.proxy})" if self.proxy else "")
            )
            return True
        except Exception as e:
            self.ultimo_error = f"no se pudo iniciar el navegador: {e}"
            logger.error(f"Change.org: {self.ultimo_error}")
            self._cerrar_fwd_proxy()
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
            return False

    def cerrar(self) -> None:
        """Cierra el driver con fallbacks y SIEMPRE el forward proxy. Nunca lanza."""
        driver = self.driver
        self.driver = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                # Fallback: la sesion quedo viva pero el servicio subyacente se
                # puede detener a mano (evita chromedriver huerfano).
                servicio = getattr(driver, "service", None)
                for metodo in ("stop", "terminate", "kill"):
                    if servicio is None or not hasattr(servicio, metodo):
                        continue
                    try:
                        getattr(servicio, metodo)()
                        break
                    except Exception:
                        continue
        self._cerrar_fwd_proxy()

    def _cerrar_fwd_proxy(self) -> None:
        fwd = getattr(self, "_fwd_proxy", None)
        if fwd is None:
            return
        self._fwd_proxy = None
        try:
            fwd.close()
        except Exception as e:
            logger.debug(f"Change.org: no se pudo cerrar el forward proxy: {e}")

    def _cancelado(self) -> bool:
        try:
            return bool(self.cancelar is not None and self.cancelar.is_set())
        except Exception:
            return False

    def _es_headless(self) -> bool:
        """True si el navegador corre sin ventana visible (nadie puede ayudar)."""
        try:
            return bool(
                self.headless if self.headless is not None else settings.headless
            )
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Helpers Selenium basicos (tolerantes a fallos)
    # ------------------------------------------------------------------ #
    def _buscar_elementos(self, by, selector) -> list:
        try:
            return list(self.driver.find_elements(by, selector) or [])
        except Exception:
            return []

    def _buscar_elementos_de(self, elemento, by, selector) -> list:
        try:
            return list(elemento.find_elements(by, selector) or [])
        except Exception:
            return []

    def _visible(self, elemento) -> bool:
        try:
            return bool(elemento.is_displayed())
        except Exception:
            return False

    def _interactuable(self, elemento) -> bool:
        try:
            return bool(elemento.is_enabled())
        except Exception:
            return True

    def _texto_elemento(self, elemento) -> str:
        try:
            return str(elemento.text or "")
        except Exception:
            return ""

    def _texto_visible(self) -> str:
        """Texto VISIBLE de la pagina (body); fallback a `_texto_pagina`.

        Para detectar pantallas/errores de registro se prefiere el texto real
        del DOM: el `page_source` incluye bundles JS con frases traducidas que
        podrian dar falsos positivos.
        """
        try:
            cuerpo = self.driver.find_element(By.TAG_NAME, "body")
            texto = self._texto_elemento(cuerpo)
            if texto:
                return texto
        except Exception:
            pass
        return self._texto_pagina()

    def _url_actual(self) -> str:
        try:
            return str(self.driver.current_url or "")
        except Exception:
            return ""

    def _ventanas(self) -> list:
        try:
            return list(self.driver.window_handles or [])
        except Exception:
            return []

    def _cambiar_a_ultima_ventana(self, ventanas_antes: list) -> bool:
        """Si al hacer clic se abrio una pestaña nueva, cambia a ella."""
        try:
            actuales = list(self.driver.window_handles or [])
            if len(actuales) > len(ventanas_antes):
                self.driver.switch_to.window(actuales[-1])
                return True
        except Exception:
            pass
        return False

    def _error_navegacion_pagina(self) -> str:
        """Detecta la pagina de error de red de Chrome ("" si esta bien).

        Revisa titulo y texto visible normalizados (`Privacy error`,
        `Your connection is not private`, `This site can't be reached`,
        `NET::ERR_...`, `ERR_CERT_*`, `ERR_TUNNEL_*`, `ERR_PROXY_*`,
        `la conexion no es privada`, ...) y devuelve el codigo especifico
        (`ERR_CERT_AUTHORITY_INVALID`, ...) o la etiqueta corta detectada.
        Sirve para no confundir un proxy con TLS/red rota con un captcha o
        con un campo faltante. NUNCA lanza.
        """
        try:
            titulo = ""
            try:
                titulo = str(self.driver.title or "")
            except Exception:
                titulo = ""
            bruto = f"{titulo}\n{self._texto_visible()}"
            normalizado = _normalizar(bruto)
            # "can't"/"can’t"/"`" se comparan sin apostrofes.
            compacto = (
                normalizado.replace("'", "").replace("\u2019", "").replace("`", "")
            )
            coincide = _PATRON_CODIGO_RED.search(compacto)
            if coincide:
                return coincide.group(1).upper()
            for frase in _FRASES_ERROR_RED:
                if frase in compacto:
                    return frase
            return ""
        except Exception:
            return ""

    def _pagina_error_servidor(self) -> str:
        """Detecta la pagina de error de APLICACION de Change.org ("" si no).

        La pantalla real ("¡Oh no! Error del servidor — Puede actualizar la
        pagina y si aun hay problemas inténtelo mas tarde...") es una respuesta
        del propio sitio, no la peticion: buscarla por el enlace de reporte
        daria el engañoso "no se encontro el enlace de violacion de politicas".

        Se detecta por TEXTO VISIBLE normalizado (`_texto_visible` +
        `_normalizar`), con reglas anti-falso-positivo: "error del servidor",
        "server error", "algo salio mal" y "something went wrong" bastan SOLAS;
        "oh no" y las frases ambiguas ("actualiza la pagina", "intenta de nuevo
        mas tarde", "intentelo mas tarde", ...) solo cuentan acompanadas de
        otra señal de error en la MISMA pagina ("error", "volver a inicio",
        ...) o de una segunda frase ambigua.

        Devuelve la frase detectada (ya normalizada) o "". NUNCA lanza.
        """
        try:
            texto = _normalizar(self._texto_visible())
            if not texto:
                return ""
            for frase in _FRASES_ERROR_SERVIDOR_FUERTES:
                if frase in texto:
                    return frase
            presentes = [
                frase
                for frase in _FRASES_ERROR_SERVIDOR_DEBILES
                if frase in texto
            ]
            if not presentes:
                return ""
            companias = (
                _FRASES_ERROR_SERVIDOR_FUERTES
                + _FRASES_ERROR_SERVIDOR_CONTEXTO
            )
            for frase in presentes:
                if any(compania in texto for compania in companias):
                    return frase
                if any(otra != frase for otra in presentes):
                    # Dos frases ambiguas juntas (p. ej. "actualiza la pagina"
                    # + "intentelo mas tarde") ya son señal suficiente.
                    return frase
            return ""
        except Exception:
            return ""

    def _navegar(self, url: str) -> str:
        """Abre la URL tolerando TimeoutException (sigue con esperas explicitas).

        Tras navegar revisa `_error_navegacion_pagina()`: si Chrome mostro una
        pagina de error de red/TLS (p. ej. un proxy con TLS roto), deja en
        `self.ultimo_error` el mensaje accionable y devuelve el codigo
        detectado; si la pagina cargo bien devuelve "".
        """
        try:
            self.driver.get(url)
        except TimeoutException as e:
            logger.warning(
                f"Change.org: timeout cargando {url}; sigo con esperas explicitas ({e})"
            )
        except Exception as e:
            raise RuntimeError(f"no se pudo abrir {url}: {e}")

        codigo = self._error_navegacion_pagina()
        if codigo:
            self.ultimo_error = (
                f"la conexion con change.org fallo ({codigo}): probable PROXY "
                "con TLS/red rota; prueba otro proxy"
            )
            logger.warning(f"Change.org: {self.ultimo_error} (url={url})")
        return codigo

    def _esperar_documento_listo(self, timeout: int = 30) -> bool:
        """Espera (polling de 1s) a que `document.readyState` sea 'complete'.

        Con un proxy lento la pagina puede quedar a medio cargar (imagenes
        rotas, paneles con spinner) cuando `driver.get()` ya retorno: sin el
        DOM completo el enlace de reporte todavia no existe y la busqueda
        fallaria en falso.

        Devuelve True si el documento quedo listo y False si se agoto `timeout`
        o se pidio el paro. Tolerante: si el JS falla (driver muerto, target
        cambiado) se cuenta como NO listo y se reintenta. NUNCA lanza.
        """
        intentos = max(1, int(timeout or 0))
        for intento in range(intentos):
            if self._cancelado():
                return False
            try:
                estado = self.driver.execute_script("return document.readyState;")
                if estado is True or str(estado or "").strip().lower() == "complete":
                    return True
            except Exception:
                pass
            if intento + 1 < intentos:
                time.sleep(1)
        return False

    def _app_peticion_lista(self) -> bool:
        """UN poll del estado de la app de la peticion (uso interno). NUNCA lanza.

        True si el JS `_JS_ESTADO_APP_PETICION` reporta en el panel de firma
        texto/boton de firma visible (o el estado de usuario firmado) y, cuando
        el panel se pudo aislar, NINGUN spinner visible dentro de el. Cualquier
        fallo del JS (driver muerto, respuesta inesperada) cuenta como False.
        """
        try:
            estado = self.driver.execute_script(_JS_ESTADO_APP_PETICION)
        except Exception:
            return False
        if not isinstance(estado, dict):
            return False
        texto = _normalizar(str(estado.get("texto") or ""))
        if not texto:
            return False
        tiene_firma = any(frase in texto for frase in _FRASES_FIRMA)
        firmado = any(frase in texto for frase in _FRASES_FIRMADO)
        if not (tiene_firma or firmado):
            return False
        if bool(estado.get("panel")) and bool(estado.get("spinner")):
            return False
        return True

    def _esperar_app_peticion(self, timeout: int = 30) -> bool:
        """Espera (BEST-EFFORT) a que la app React de la peticion este lista.

        Con proxy lento la peticion carga (imagen/hero OK) pero la app sigue
        hidratando: el panel "Firma esta petición" queda con spinner y los
        clics del enlace de reporte NO abren el modal. Este poll (1s) espera
        hasta `timeout` segundos a ver en el panel de firma el texto/boton de
        firma ("firma esta peticion"/"firma la peticion"/"firmar") o el estado
        de usuario firmado, y SIN spinner visible dentro del panel; la señal
        real la aporta el JS `_JS_ESTADO_APP_PETICION`.

        BEST-EFFORT: si se agota `timeout` devuelve False y el flujo CONTINUA
        igual (jamas bloquea el reporte; los reintentos de `reportar()` cubren
        el timing). Tolerante a JS roto/driver muerto (False) y cancelable
        (`_cancelado`). NUNCA lanza.
        """
        intentos = max(1, int(timeout or 0))
        for intento in range(intentos):
            if self._cancelado():
                return False
            if self._app_peticion_lista():
                return True
            if intento + 1 < intentos:
                time.sleep(1)
        if self._cancelado():
            return False
        return self._app_peticion_lista()

    def _scroll_humano(self) -> None:
        """Scroll incremental 250-550px con pausa aleatoria y retroceso ocasional."""
        try:
            pixeles = random.randint(250, 550)
            self.driver.execute_script(f"window.scrollBy(0, {pixeles});")
            time.sleep(random.uniform(0.4, 1.2))
            if random.random() < 0.25:
                atras = random.randint(100, 220)
                self.driver.execute_script(f"window.scrollBy(0, -{atras});")
                time.sleep(random.uniform(0.2, 0.6))
        except Exception as e:
            logger.debug(f"Change.org: el scroll fallo: {e}")

    def _clic_elemento(self, elemento) -> bool:
        """scrollIntoView + clic normal con fallback JS + cambio de pestaña."""
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", elemento
            )
        except Exception:
            pass
        ventanas_antes = self._ventanas()
        try:
            elemento.click()
        except Exception as primero:
            try:
                self.driver.execute_script("arguments[0].click();", elemento)
            except Exception as segundo:
                self.ultimo_error = (
                    f"no se pudo hacer clic (normal: {primero}; JS: {segundo})"
                )
                return False
        self._cambiar_a_ultima_ventana(ventanas_antes)
        return True

    # ------------------------------------------------------------------ #
    # Banner de cookies / enlace de reporte
    # ------------------------------------------------------------------ #
    def _buscar_boton_banner(self):
        for by, selector in (
            (By.TAG_NAME, "button"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.TAG_NAME, "a"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not self._visible(elemento):
                    continue
                texto = _normalizar(self._texto_elemento(elemento))
                if any(frase in texto for frase in _FRASES_BANNER):
                    return elemento
        return None

    def _cerrar_banner_cookies(self, intentos: int = 8) -> bool:
        """Cierra el banner de cookies (~2s maximo). Tolerante si no aparece."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return False
            boton = self._buscar_boton_banner()
            if boton is not None and self._clic_elemento(boton):
                logger.debug("Change.org: banner de cookies cerrado")
                return True
            time.sleep(0.25)
        return False

    def _coincide_texto_enlace(self, elemento) -> bool:
        texto = _normalizar(self._texto_elemento(elemento))
        if texto:
            if any(frase in texto for frase in _FRASES_ENLACE):
                return True
            if "denunciar" in texto and (
                "violacion" in texto or "politica" in texto or "contenido" in texto
            ):
                return True
            if "report" in texto and ("violation" in texto or "policy" in texto):
                return True
        try:
            href = _normalizar(elemento.get_attribute("href") or "")
        except Exception:
            href = ""
        return "policy_violation" in href or "policy-violation" in href

    def _localizar_enlace_reporte(self):
        """Busca el enlace de reporte: href directo -> texto ES/EN -> boton."""
        # 1) href directo (rapido y sin falsos positivos de texto).
        for xpath in (
            "//a[contains(@href,'policy_violation')]",
            "//a[contains(@href,'policy-violation')]",
        ):
            for elemento in self._buscar_elementos(By.XPATH, xpath):
                if self._visible(elemento):
                    return elemento
        # 2) enlaces/botones cuyo texto normalizado coincide (tolera acentos).
        for by, selector in (
            (By.TAG_NAME, "a"),
            (By.TAG_NAME, "button"),
            (By.TAG_NAME, "span"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.CSS_SELECTOR, "[role='link']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and self._coincide_texto_enlace(elemento):
                    return elemento
        # 3) XPath por texto parcial de "Denunciar".
        for elemento in self._buscar_elementos(
            By.XPATH, "//button[contains(.,'Denunciar')]"
        ):
            if self._visible(elemento):
                return elemento
        return None

    def _href_directo_enlace(self, elemento) -> str:
        """Href REAL (http(s) de change.org) del enlace, o "".

        Solo sirve como ULTIMO recurso de `reportar()`: se ignora todo lo que
        no sea una URL http(s) del mismo dominio (nunca "#", "javascript:",
        "mailto:", "tel:", href vacio ni dominios externos). Los hrefs
        relativos (`/p/.../policy_violation`) se resuelven contra la URL
        actual o la raiz de change.org. NUNCA lanza.
        """
        try:
            href = str(elemento.get_attribute("href") or "").strip()
        except Exception:
            return ""
        if not href:
            return ""
        bajo = href.lower()
        if bajo.startswith(("#", "javascript:", "mailto:", "tel:")):
            return ""
        try:
            completa = urljoin(
                self._url_actual() or "https://www.change.org/", href
            )
            partes = urlparse(completa)
        except Exception:
            return ""
        if (partes.scheme or "").lower() not in ("http", "https"):
            return ""
        host = (partes.netloc or "").lower().split("@")[-1].split(":")[0]
        if host != "change.org" and not host.endswith(".change.org"):
            return ""
        return completa

    def _buscar_enlace_reporte_una_pasada(self, intentos: int = 12):
        """Una pasada: sin scroll -> scroll humano incremental hasta encontrarlo."""
        if self._cancelado():
            return None
        elemento = self._localizar_enlace_reporte()
        if elemento is not None:
            return elemento
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return None
            self._scroll_humano()
            elemento = self._localizar_enlace_reporte()
            if elemento is not None:
                return elemento
        return None

    def _buscar_enlace_reporte(self, intentos: int = 12):
        """Busca el enlace con scroll humano y una segunda pasada tras refresh.

        Si la primera pasada no lo encuentra (pagina a medio cargar por un
        proxy lento), hace UN `driver.refresh()` (try/except) +
        `_esperar_documento_listo(25)` + otra pasada completa. Si aun asi no
        aparece, deja un WARNING con la URL y devuelve None. NUNCA lanza.
        """
        elemento = self._buscar_enlace_reporte_una_pasada(intentos)
        if elemento is not None or self._cancelado():
            return elemento
        logger.debug(
            "Change.org: enlace de reporte no encontrado en la primera pasada; "
            f"refresco y reintento (url={self._url_actual() or self.url_peticion})"
        )
        try:
            self.driver.refresh()
        except Exception as e:
            logger.debug(f"Change.org: el refresh de recuperacion fallo: {e}")
        self._esperar_documento_listo(25)
        elemento = self._buscar_enlace_reporte_una_pasada(intentos)
        if elemento is None:
            logger.warning(
                "Change.org: no se encontro el enlace de reporte de violacion "
                f"de politicas (url={self._url_actual() or self.url_peticion})"
            )
        return elemento

    # ------------------------------------------------------------------ #
    # Registro / Login en Change.org
    # ------------------------------------------------------------------ #
    def _texto_boton_candidato(self, elemento) -> str:
        """Texto normalizado de un boton (visible text -> value)."""
        texto = _normalizar(self._texto_elemento(elemento))
        if not texto:
            try:
                texto = _normalizar(elemento.get_attribute("value") or "")
            except Exception:
                texto = ""
        return texto

    def _buscar_boton_login(self):
        """Boton/link 'Iniciar sesión' | 'Log in' (ES/EN), visible y habilitado."""
        for by, selector in (
            (By.TAG_NAME, "button"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.TAG_NAME, "a"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not (self._visible(elemento) and self._interactuable(elemento)):
                    continue
                texto = self._texto_boton_candidato(elemento)
                if not texto:
                    continue
                if any(
                    texto == frase or texto.startswith(frase)
                    for frase in _FRASES_LOGIN
                ):
                    return elemento
        return None

    def _buscar_boton_continuar(self):
        """Boton 'Continuar'/'Continue' del formulario de acceso.

        Prioriza el texto EXACTO y los `type=submit`; descarta los botones
        sociales (Google/Facebook/...) y los de codigo por email/telefono.
        """
        candidatos = []
        for by, selector in (
            (By.TAG_NAME, "button"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not (self._visible(elemento) and self._interactuable(elemento)):
                    continue
                texto = self._texto_boton_candidato(elemento)
                if not texto:
                    continue
                if any(excluir in texto for excluir in _FRASES_CONTINUAR_EXCLUIR):
                    continue
                if texto in _FRASES_CONTINUAR:
                    return elemento
                if any(texto.startswith(frase) for frase in _FRASES_CONTINUAR):
                    candidatos.append(elemento)
        return candidatos[0] if candidatos else None

    def _campo_password(self):
        """Input de contraseña visible (pantalla nueva o de login)."""
        for by, selector in (
            (By.CSS_SELECTOR, "input[type='password']"),
            (By.CSS_SELECTOR, "input[name*='password']"),
            (By.CSS_SELECTOR, "input[id*='password']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and self._interactuable(elemento):
                    return elemento
        return None

    def _pantalla_codigo_temporal(self) -> bool:
        """True si el texto visible pide un codigo temporal/verificacion.

        Change.org la muestra al correo de una cuenta EXISTENTE en vez del
        formulario de contraseña; `_pulsar_opcion_password` es la salida.
        """
        try:
            texto = _normalizar(self._texto_visible())
        except Exception:
            return False
        if not texto:
            return False
        return any(frase in texto for frase in _FRASES_PANTALLA_CODIGO)

    def _pulsar_opcion_password(self) -> bool:
        """Pulsa la opcion 'Ingresar con contraseña' de la pantalla de codigo.

        Busca `a`/`button`/`[role='button']`/`[role='link']` visibles e
        interactuables cuyo texto (o `value`) coincida con
        `_FRASES_OPCION_PASSWORD` (las frases mas especificas primero) y los
        pulsa con `_clic_elemento`. Devuelve True si logro pulsar alguno.
        """
        for frase in _FRASES_OPCION_PASSWORD:
            for by, selector in (
                (By.TAG_NAME, "a"),
                (By.TAG_NAME, "button"),
                (By.CSS_SELECTOR, "[role='button']"),
                (By.CSS_SELECTOR, "[role='link']"),
            ):
                for elemento in self._buscar_elementos(by, selector):
                    if not (
                        self._visible(elemento) and self._interactuable(elemento)
                    ):
                        continue
                    texto = self._texto_boton_candidato(elemento)
                    if not texto or frase not in texto:
                        continue
                    if self._clic_elemento(elemento):
                        return True
        return False

    def _campo_email_login(self):
        """Input de correo del acceso: prioriza el modal (`role='dialog']`)."""
        for by, selector in (
            (By.CSS_SELECTOR, "[role='dialog'] input[type='email']"),
            (By.CSS_SELECTOR, "[role='dialog'] input[name*='email']"),
            (By.CSS_SELECTOR, "[role='dialog'] input[name*='correo']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and self._interactuable(elemento):
                    return elemento
        return self._resolver_campo("email")

    def _asegurar_campo_email(self, intentos: int = 8):
        """Abre el login (clic 'Iniciar sesión') y devuelve el campo de correo.

        En MODO ASISTIDO (`esperar_captcha_seg > 0`) el reto anti-bot puede
        aparecer ANTES del campo de correo: se espera a que un humano lo
        resuelva y se sigue buscando el campo (deja el motivo en
        `self.ultimo_error` si no se pudo).
        """
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return None
            if self.esperar_captcha_seg > 0 and not self._esperar_reto_humano():
                return None
            boton = self._buscar_boton_login()
            if boton is not None:
                self._clic_elemento(boton)
            elemento = self._campo_email_login()
            if elemento is not None and self._visible(elemento):
                return elemento
            time.sleep(0.5)
        elemento = self._campo_email_login()
        if elemento is not None and self._visible(elemento):
            return elemento
        return None

    def _pulsar_continuar(self, intentos: int = 10) -> bool:
        """Localiza y pulsa el boton Continuar (~5s). False si no aparece."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return False
            boton = self._buscar_boton_continuar()
            if boton is not None and self._clic_elemento(boton):
                return True
            time.sleep(0.5)
        return False

    def _detectar_error_sesion(self, tardias: bool = False) -> str:
        """Frase de error del login (normalizada), "" si no hay.

        `tardias=True` agrega las señales que solo son error DESPUES de enviar
        la contraseña (p. ej. "al menos 10 caracteres" tambien es un texto de
        ayuda en la pantalla "Crea tu contraseña").
        """
        texto = _normalizar(self._texto_visible())
        if not texto:
            return ""
        frases = _FRASES_ERROR_SESION + (
            _FRASES_ERROR_SESION_TARDIAS if tardias else ()
        )
        for frase in frases:
            if frase in texto:
                return frase
        return ""

    def _detectar_reto_humano(self) -> str:
        """Detecta el reto anti-bot de Cloudflare/Turnstile ("" si no hay).

        El widget vive en una sombra/iframe "protegida" (su
        `getBoundingClientRect` puede estar anulado para no poder medirlo), asi
        que se revisa primero por JS y luego por selectores/texto visible.
        Devuelve una etiqueta corta del reto o "".
        """
        try:
            resultado = self.driver.execute_script(
                "try {"
                "  if (window.turnstile) return 'turnstile';"
                "  const nodo = document.querySelector("
                "    '.cf-turnstile, [class*=turnstile], [id*=cf-chl],"
                "     [data-testid*=turnstile]');"
                "  if (nodo) return 'turnstile-dom';"
                "  const frames = document.querySelectorAll('iframe');"
                "  for (const f of frames) {"
                "    if (/challenges[.]cloudflare[.]com|turnstile/i.test(f.src || ''))"
                "      return 'turnstile-iframe';"
                "    try { if (!f.getBoundingClientRect()) return 'iframe-protegido'; }"
                "    catch (e) { return 'iframe-protegido'; }"
                "  }"
                "} catch (e) {}"
                "return '';"
            )
            if resultado:
                return str(resultado)
        except Exception:
            pass
        for by, selector in (
            (By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"),
            (By.CSS_SELECTOR, "iframe[src*='turnstile']"),
            (By.CSS_SELECTOR, ".cf-turnstile"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento):
                    return selector
        texto = _normalizar(self._texto_visible())
        for frase in _FRASES_RETO_HUMANO:
            if frase in texto:
                return frase
        return ""

    # ------------------------------------------------------------------ #
    # MODO ASISTIDO: espera humana al reto anti-bot (Cloudflare/captcha)
    # ------------------------------------------------------------------ #
    def _avisar_espera_captcha(self, segundos: int) -> None:
        """Emite UNA vez el aviso de espera asistida; jamas rompe el flujo."""
        aviso = getattr(self, "aviso", None)
        if not callable(aviso):
            return
        cuenta = dict(self.cuenta or {})
        try:
            aviso(
                {
                    "tipo": "espera_captcha",
                    "usuario": str(cuenta.get("usuario") or ""),
                    "email": str(cuenta.get("email") or ""),
                    "segundos": int(segundos),
                    "estado": "iniciando",
                }
            )
        except Exception as e:  # pragma: no cover - callback de terceros
            logger.debug(f"Change.org: el aviso de espera de captcha fallo: {e}")

    def _esperar_reto_humano(self, reto: str = "") -> bool:
        """MODO ASISTIDO: espera a que un humano resuelva el reto anti-bot.

        Devuelve True si ya NO hay reto (o si nunca lo hubo). `reto` puede ser
        la etiqueta ya detectada; vacio -> detecta aqui (Cloudflare o captcha).

        - `esperar_captcha_seg <= 0` (default) o navegador headless: devuelve
          False SIN esperar (fallo clasico) y deja el motivo en
          `self.ultimo_error` ("" con la espera desactivada, para que el
          llamador conserve su mensaje historico).
        - Chrome visible + espera > 0: emite `aviso` UNA vez, hace polling cada
          1s (cancelable) y, si el reto desaparece, devuelve True. Si se agota
          deja el error accionable; si se cancela, MENSAJE_CANCELADO.
        """
        reto = str(reto or "")
        if not reto:
            reto = self._detectar_reto_humano() or self._detectar_captcha()
        if not reto:
            return True
        segundos = int(getattr(self, "esperar_captcha_seg", 0) or 0)
        if segundos <= 0:
            # Comportamiento clasico EXACTO: el llamador decide el fallo.
            self.ultimo_error = ""
            return False
        if self._es_headless():
            self.ultimo_error = (
                "verificacion anti-bot de Change.org (Cloudflare): el navegador "
                "esta en headless y no hay un humano que resuelva el reto; usa "
                "el modo asistido con Chrome visible y resuelvelo a mano"
            )
            return False
        if self._cancelado():
            self.ultimo_error = MENSAJE_CANCELADO
            return False
        self._avisar_espera_captcha(segundos)
        self.ultimo_error = ""
        for _ in range(segundos):
            if self._cancelado():
                self.ultimo_error = MENSAJE_CANCELADO
                return False
            if not (self._detectar_reto_humano() or self._detectar_captcha()):
                return True
            # ~1s en tramos cortos para reaccionar rapido al paro.
            for _ in range(4):
                if self._cancelado():
                    self.ultimo_error = MENSAJE_CANCELADO
                    return False
                time.sleep(0.25)
        if self._cancelado():
            self.ultimo_error = MENSAJE_CANCELADO
            return False
        if not (self._detectar_reto_humano() or self._detectar_captcha()):
            return True
        self.ultimo_error = (
            "verificacion anti-bot de Change.org (Cloudflare): el reto no se "
            f"resolvio en {segundos}s; usa el modo asistido con Chrome visible "
            "y resuelvelo a mano"
        )
        return False

    def _mensaje_error_sesion(self, frase: str) -> str:
        """Mensaje accionable para el UI a partir de la señal detectada."""
        if frase in (
            "contrasena incorrecta",
            "clave incorrecta",
            "incorrect password",
            "wrong password",
            "invalid password",
            "contrasena invalida",
            "la contrasena es incorrecta",
        ):
            return f"contraseña incorrecta en Change.org (señal: '{frase}')"
        if (
            "no pudimos encontrarte" in frase
            or "no encontramos" in frase
            or "couldn't find" in frase
            or "could not find" in frase
        ):
            return f"Change.org no encontro una cuenta con ese correo (señal: '{frase}')"
        return f"error de Change.org: '{frase}'"

    def _hay_login_visible(self) -> bool:
        """True si hay un enlace/boton VISIBLE de 'Iniciar sesión'/'Log in'.

        Contra-señal de la home logueada: con sesion iniciada Change.org NO
        muestra ese boton. Acepta `aria-label` ademas de texto/`value`.
        """
        for by, selector in (
            (By.TAG_NAME, "button"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.TAG_NAME, "a"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not self._visible(elemento):
                    continue
                texto = self._texto_boton_candidato(elemento)
                if not texto:
                    try:
                        texto = _normalizar(
                            elemento.get_attribute("aria-label") or ""
                        )
                    except Exception:
                        texto = ""
                if not texto:
                    continue
                if any(
                    texto == frase or texto.startswith(frase)
                    for frase in _FRASES_LOGIN
                ):
                    return True
        return False

    def _evidencia_home_logueada(self) -> str:
        """Home logueada real: avatar/menu de cuenta y SIN 'Iniciar sesión'.

        Change.org sirve la home con sesion (`https://www.change.org/?met=esnv`)
        con el avatar/menu de cuenta en la cabecera y sin el enlace visible de
        "Iniciar sesión". Para no confundir el logo con un avatar se exige:
        (1) la URL NO es de login, (2) NO hay login visible y (3) al menos una
        señal de sesion: imagen de la cabecera (`header > img`, descartando
        logo/brand), avatar por `data-testid`/clase, boton con `aria-label` de
        cuenta, o un enlace de cerrar sesion presente en el DOM (puede estar
        oculto dentro de un dropdown). Nunca marca exito por un clic: solo
        señales reales del DOM.
        """
        url_norm = _normalizar(self._url_actual())
        if "login_or_join" in url_norm or "/login" in url_norm:
            return ""
        if self._hay_login_visible():
            return ""
        # (1) Imagen de la cabecera que no sea el logo/marca.
        for cabecera in self._buscar_elementos(By.TAG_NAME, "header"):
            if not self._visible(cabecera):
                continue
            for imagen in self._buscar_elementos_de(cabecera, By.TAG_NAME, "img"):
                if not self._visible(imagen):
                    continue
                pistas = _normalizar(
                    " ".join(
                        str(imagen.get_attribute(nombre) or "")
                        for nombre in ("alt", "src", "class", "data-testid")
                    )
                )
                if any(pista in pistas for pista in ("logo", "brand")):
                    continue
                return (
                    "home logueada (avatar de la cabecera y sin "
                    "'Iniciar sesión')"
                )
        # (2) Avatar/menu de cuenta por selector (testid, clase, aria-label).
        for by, selector in (
            (By.CSS_SELECTOR, "[data-testid*='avatar']"),
            (By.CSS_SELECTOR, "[class*='avatar']"),
            (By.CSS_SELECTOR, "[aria-label*='cuenta' i]"),
            (By.CSS_SELECTOR, "[aria-label*='account' i]"),
            (By.CSS_SELECTOR, "[aria-label*='usuario' i]"),
            (By.CSS_SELECTOR, "[aria-label*='user' i]"),
            (By.CSS_SELECTOR, "img[alt*='perfil' i]"),
            (By.CSS_SELECTOR, "img[alt*='profile' i]"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento):
                    return (
                        f"home logueada ({selector} visible y sin "
                        "'Iniciar sesión')"
                    )
        # (3) Menu de cuenta en el DOM (puede estar oculto en un dropdown).
        for by, selector in (
            (By.CSS_SELECTOR, "a[href*='/logout']"),
            (By.CSS_SELECTOR, "[href*='sign_out']"),
            (By.CSS_SELECTOR, "[href*='sign-out']"),
        ):
            if self._buscar_elementos(by, selector):
                return "home logueada (menu de cuenta/cerrar sesion en el DOM)"
        return ""

    def _evidencia_sesion(self, email: str = "") -> str:
        """Evidencia POSITIVA de sesion iniciada; "" si aun no se confirma.

        Prioridad: selectores fuertes (logout/perfil/ajustes/avatar) -> textos
        de cuenta en pantalla -> correo de la cuenta -> URL de cuenta -> home
        logueada real (avatar de cabecera o menu de cuenta sin el enlace
        "Iniciar sesión"). NUNCA marca exito por "hacer clic", solo con señales
        reales del DOM/pantalla.
        """
        selectores = (
            (By.CSS_SELECTOR, "a[href*='/logout']"),
            (By.CSS_SELECTOR, "a[href*='/profile']"),
            (By.CSS_SELECTOR, "a[href*='/settings']"),
            (By.CSS_SELECTOR, "[data-testid*='avatar']"),
            (By.CSS_SELECTOR, "img[alt*='perfil' i]"),
            (By.CSS_SELECTOR, "img[alt*='profile' i]"),
        )
        for by, selector in selectores:
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento):
                    return f"sesion iniciada ({selector})"
        texto = _normalizar(self._texto_visible())
        for frase in (
            "cerrar sesion",
            "sign out",
            "log out",
            "mi cuenta",
            "my account",
            "configuracion de la cuenta",
            "account settings",
        ):
            if frase in texto:
                return f"confirmacion en pantalla: '{frase}'"
        email_norm = _normalizar(email)
        url_norm = _normalizar(self._url_actual())
        en_login = "login_or_join" in url_norm or "/login" in url_norm
        if email_norm and email_norm in texto:
            if self._campo_password() is None and not en_login:
                return "el correo de la cuenta aparece en la pagina"
        if url_norm and not en_login:
            for fragmento in ("/profile", "/dashboard", "/account", "/settings", "/user"):
                if fragmento in url_norm:
                    return f"URL de cuenta: {self._url_actual()}"
        return self._evidencia_home_logueada()

    # ------------------------------------------------------------------ #
    # Sesion persistente por cookies (anti-captcha en corridas siguientes)
    # ------------------------------------------------------------------ #
    def _payload_cookie_cdp(self, cookie) -> dict:
        """Payload de `Network.setCookie` para una cookie guardada (o {}).

        Mapea `name,value,domain,path,secure,httpOnly` y `expires` ->
        `expirationDate` (acepta tambien `expiry`/`expirationDate`). Devuelve
        ``{}`` si la cookie no tiene name/value utilizables.
        """
        if not isinstance(cookie, dict):
            return {}
        nombre = cookie.get("name")
        valor = cookie.get("value")
        if not nombre or valor is None or str(valor) == "":
            return {}
        payload = {
            "name": str(nombre),
            "value": valor if isinstance(valor, str) else str(valor),
            "domain": str(cookie.get("domain") or ".change.org"),
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", True)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        expira = cookie.get("expires", cookie.get("expiry", cookie.get("expirationDate")))
        if expira:
            try:
                payload["expirationDate"] = float(expira)
            except (TypeError, ValueError):
                pass
        return payload

    def _set_cookie_cdp(self, payload: dict) -> bool:
        """Inyecta UNA cookie con `Network.setCookie`; False si falla.

        Trata como fallo las excepciones y la respuesta `{"success": False}`.
        Nunca lanza.
        """
        try:
            respuesta = self.driver.execute_cdp_cmd("Network.setCookie", payload)
        except Exception:
            return False
        if isinstance(respuesta, dict) and respuesta.get("success") is False:
            return False
        return True

    def _inyectar_cookies_cdp(self, cookies) -> int:
        """Inyecta TODAS las cookies guardadas por CDP sin navegar.

        Habilita `Network.enable` (best-effort) y hace un
        `Network.setCookie` por cookie; las cookies individuales que fallan
        se IGNORAN. Devuelve cuantas se inyectaron. Nunca lanza.
        """
        inyectadas = 0
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        for cookie in cookies or ():
            payload = self._payload_cookie_cdp(cookie)
            if not payload:
                continue
            if self._set_cookie_cdp(payload):
                inyectadas += 1
            else:
                logger.debug(
                    f"Change.org: cookie {payload.get('name')} no inyectada por CDP"
                )
        return inyectadas

    def _restaurar_sesion_change(self) -> bool:
        """Restaura la sesion persistida (cookies) de `self.cuenta["usuario"]`.

        Carga `data/cookies/change/{usuario}.json`, inyecta las cookies por
        CDP (`Network.setCookie`) y navega a la home con `self._navegar`
        (tolerante); reintenta la evidencia (`_evidencia_sesion` /
        `_evidencia_home_logueada`) hasta ~8s. Devuelve True SOLO con
        evidencia real de sesion. Si hay error de red o aparece un reto/captcha
        devuelve False sin bloquear (el flujo normal de login/registro decidi-
        ra). NUNCA lanza.
        """
        if not _reutilizar_sesion_activo():
            return False
        cuenta = dict(self.cuenta or {})
        usuario = str(cuenta.get("usuario") or "")
        email = str(cuenta.get("email") or "")
        if self.driver is None:
            return False
        datos = cargar_sesion_change(usuario)
        if not datos:
            return False
        error_previo = self.ultimo_error
        try:
            inyectadas = self._inyectar_cookies_cdp(datos.get("cookies") or [])
            if inyectadas <= 0:
                return False
            try:
                codigo_red = self._navegar(_URL_HOME_CHANGE)
            except Exception:
                return False
            if codigo_red:
                return False
            for _ in range(16):  # ~8s de reintentos
                if self._cancelado():
                    return False
                evidencia = (
                    self._evidencia_sesion(email) or self._evidencia_home_logueada()
                )
                if evidencia:
                    logger.debug(
                        f"Change.org: sesion restaurada de {usuario or '(sin usuario)'} "
                        f"({inyectadas} cookies): {evidencia}"
                    )
                    return True
                if self._detectar_reto_humano() or self._detectar_captcha():
                    return False
                time.sleep(0.5)
            return False
        except Exception as e:
            logger.debug(
                f"Change.org: no se pudo restaurar la sesion de "
                f"{usuario or '(sin usuario)'}: {type(e).__name__}: {e}"
            )
            return False
        finally:
            # La restauracion no debe dejar un error "pegado" del intento:
            # si falla, el flujo normal de login/registro decide y reporta.
            self.ultimo_error = error_previo

    def registrar_o_entrar(self) -> dict:
        """Registra (cuenta NUEVA) o inicia sesion en Change.org.

        Flujo real: `login_or_join?user_flow=nav` -> "Iniciar sesión" ->
        "Dirección de correo electrónico" -> "Continuar" -> contraseña (nueva o
        de login) -> "Continuar" -> (solo nuevas) "Nombres"/"Apellidos" ->
        "Continuar". La contraseña sale de `password_change_para`.

        Si Change responde con la pantalla de codigo temporal por correo para
        una cuenta existente (sin campo de contraseña), se pulsa UNA vez la
        opcion "Ingresar con contraseña"/"Sign in with password" y el flujo
        continua; si no aparece en ~5 intentos falla con
        `_ERROR_PANTALLA_CODIGO` (no con el timeout generico).

        SESION PERSISTENTE: si hay cookies guardadas de la cuenta
        (`data/cookies/change/{usuario}.json`) y `CHANGE_REUTILIZAR_SESION`
        esta activo, primero intenta `_restaurar_sesion_change()`: con
        evidencia de sesion retorna de inmediato
        ``{"ok": True, "estado": "existente", "evidencia": "sesion restaurada
        (cookies)", "error": "", "sesion_restaurada": True}`` sin escribir el
        correo ni pedir captcha. En el exito normal (login/registro) guarda la
        sesion nueva (las cookies pueden rotar).

        Devuelve ``{"ok","estado","error","evidencia","sesion_restaurada"}``
        con estado "nueva" si vio "Crea tu contraseña" (o la pantalla de
        nombre), "existente" si entro por login (o restauro la sesion) y
        "fallo" si no se pudo confirmar la sesion. NUNCA lanza.
        """
        resultado = {
            "ok": False,
            "estado": "fallo",
            "error": "",
            "evidencia": "",
            "sesion_restaurada": False,
        }
        cuenta = dict(self.cuenta or {})
        email = str(cuenta.get("email") or "").strip()
        password = password_change_para(
            cuenta.get("password") or cuenta.get("email_password") or ""
        )
        nombre = str(cuenta.get("nombre") or "").strip()
        apellido = str(cuenta.get("apellido") or "").strip()
        if not (nombre and apellido):
            nombre, apellido = _resolver_nombres(
                nombre, apellido, cuenta.get("nombre_mostrado") or ""
            )
        if not email or not password:
            resultado["error"] = "la cuenta no tiene email o contraseña para Change.org"
            return resultado
        try:
            if not self.preparar_driver():
                resultado["error"] = (
                    self.ultimo_error or "no se pudo iniciar el navegador"
                )
                return resultado

            # SESION PERSISTENTE: con cookies guardadas y evidencia real de
            # sesion NO se escribe el correo ni se pide captcha (se retorna ya).
            if _reutilizar_sesion_activo() and self._restaurar_sesion_change():
                resultado["ok"] = True
                resultado["estado"] = "existente"
                resultado["evidencia"] = "sesion restaurada (cookies)"
                resultado["sesion_restaurada"] = True
                self.estado_cuenta = "existente"
                return resultado

            codigo_red = self._navegar(_URL_LOGIN_CHANGE)
            if self._cancelado():
                resultado["error"] = MENSAJE_CANCELADO
                return resultado
            if codigo_red:
                # Proxy con TLS/red rota: NO es captcha ni "campo no encontrado".
                resultado["error"] = self.ultimo_error
                return resultado
            # El home de login tambien puede quedar a medio cargar con proxies
            # lentos: espera tolerante a que el DOM este listo (sin refrescos).
            self._esperar_documento_listo(20)
            self._cerrar_banner_cookies()

            campo = self._asegurar_campo_email()
            if campo is None:
                if self._cancelado():
                    resultado["error"] = MENSAJE_CANCELADO
                else:
                    resultado["error"] = (
                        self.ultimo_error
                        or "no se encontro el campo de direccion de correo electronico"
                    )
                return resultado
            if not self.escribir_humano(campo, email):
                resultado["error"] = "no se pudo escribir el correo electronico"
                return resultado
            time.sleep(random.uniform(0.15, 0.4))
            if not self._pulsar_continuar():
                resultado["error"] = "no se encontro el boton Continuar"
                return resultado

            vio_password_nueva = False
            password_enviada = False
            nombre_enviado = False
            opcion_password_pulsada = False
            intentos_opcion_password = 0
            error = ""
            confirmado = ""
            reto_iter = 0
            for _ in range(60):
                if self._cancelado():
                    error = MENSAJE_CANCELADO
                    break
                captcha = self._detectar_captcha()
                if captcha:
                    # La sesion manda: si la pagina ya muestra evidencia real
                    # de login (p. ej. avatar en la home), un resto de captcha
                    # no debe bloquear con la espera asistida.
                    evidencia = self._evidencia_sesion(email)
                    if evidencia:
                        confirmado = evidencia
                        break
                    if self.esperar_captcha_seg > 0:
                        # MODO ASISTIDO: un humano lo resuelve y seguimos.
                        if self._esperar_reto_humano(captcha):
                            reto_iter = 0
                            continue
                        error = self.ultimo_error or f"captcha detectado: {captcha}"
                        break
                    error = f"captcha detectado: {captcha}"
                    break
                reto = self._detectar_reto_humano()
                if reto:
                    # Igual que con el captcha: evidencia de sesion primero.
                    evidencia = self._evidencia_sesion(email)
                    if evidencia:
                        confirmado = evidencia
                        break
                    if self.esperar_captcha_seg > 0:
                        # MODO ASISTIDO: espera (visible) y continua al limpiarse.
                        if self._esperar_reto_humano(reto):
                            reto_iter = 0
                            continue
                        error = self.ultimo_error or (
                            "verificacion anti-bot de Change.org (Cloudflare): "
                            f"reto '{reto}' no superado; reintenta con otro "
                            "proxy o con el navegador visible"
                        )
                        break
                    reto_iter += 1
                    if reto_iter >= _ITERACIONES_ESPERA_RETO:
                        error = (
                            "verificacion anti-bot de Change.org (Cloudflare): "
                            f"reto '{reto}' no superado; reintenta con otro "
                            "proxy o con el navegador visible"
                        )
                        break
                else:
                    reto_iter = 0
                frase_error = self._detectar_error_sesion()
                if (
                    not frase_error
                    and password_enviada
                    and self._campo_password() is None
                ):
                    # Las señales "tardias" (p. ej. "al menos 10 caracteres")
                    # solo cuentan cuando la pantalla de contraseña ya no esta
                    # (en "Crea tu contraseña" son texto de ayuda).
                    frase_error = self._detectar_error_sesion(tardias=True)
                if frase_error:
                    error = self._mensaje_error_sesion(frase_error)
                    break
                evidencia = self._evidencia_sesion(email)
                if evidencia:
                    confirmado = evidencia
                    break

                if not password_enviada:
                    clave = self._campo_password()
                    if clave is None and self._pantalla_codigo_temporal():
                        # Cuenta existente: Change mando un codigo temporal al
                        # correo y escondio la contraseña tras la opcion
                        # "Ingresar con contraseña". Se pulsa UNA vez por
                        # pantalla y se sigue el flujo normal (la proxima vuelta
                        # encuentra el campo de contraseña y lo llena). Si la
                        # opcion no aparece en varios intentos, se falla con el
                        # error claro en vez del timeout generico.
                        if (
                            not opcion_password_pulsada
                            and self._pulsar_opcion_password()
                        ):
                            opcion_password_pulsada = True
                            continue
                        if not opcion_password_pulsada:
                            intentos_opcion_password += 1
                            if (
                                intentos_opcion_password
                                >= _INTENTOS_OPCION_PASSWORD
                            ):
                                error = _ERROR_PANTALLA_CODIGO
                                break
                        time.sleep(0.5)
                        continue
                    if clave is not None:
                        texto = _normalizar(self._texto_visible())
                        if any(frase in texto for frase in _FRASES_PANTALLA_NUEVA):
                            vio_password_nueva = True
                        if not self.escribir_humano(clave, password):
                            error = "no se pudo escribir la contrasena"
                            break
                        time.sleep(random.uniform(0.15, 0.4))
                        if not self._pulsar_continuar():
                            error = "no se encontro el boton Continuar de la contrasena"
                            break
                        password_enviada = True
                        continue

                if not nombre_enviado and (password_enviada or vio_password_nueva):
                    campo_nombre = self._resolver_campo("nombre")
                    campo_apellido = self._resolver_campo("apellido")
                    if campo_nombre is not None and campo_apellido is not None:
                        texto = _normalizar(self._texto_visible())
                        es_pantalla_nombre = any(
                            frase in texto for frase in _FRASES_PANTALLA_NOMBRE
                        )
                        if es_pantalla_nombre or vio_password_nueva:
                            if not self.escribir_humano(campo_nombre, nombre):
                                error = "no se pudo escribir el nombre"
                                break
                            if not self.escribir_humano(campo_apellido, apellido):
                                error = "no se pudo escribir el apellido"
                                break
                            time.sleep(random.uniform(0.15, 0.4))
                            if not self._pulsar_continuar():
                                error = (
                                    "no se encontro el boton Continuar del nombre"
                                )
                                break
                            nombre_enviado = True
                            continue
                time.sleep(0.5)
            else:
                error = "no se pudo confirmar la sesion de Change.org (timeout)"

            if not error and not confirmado:
                error = "no se pudo confirmar la sesion de Change.org"
            if error:
                resultado["error"] = (
                    MENSAJE_CANCELADO if self._cancelado() else error
                )
                return resultado

            estado = "nueva" if (vio_password_nueva or nombre_enviado) else "existente"
            resultado["ok"] = True
            resultado["estado"] = estado
            resultado["evidencia"] = confirmado
            self.estado_cuenta = estado
            # Las cookies pueden rotar: actualizar SIEMPRE la sesion guardada
            # (best-effort; `guardar_sesion_change` nunca lanza ni hace nada si
            # `CHANGE_REUTILIZAR_SESION` esta desactivado).
            try:
                guardar_sesion_change(
                    str(cuenta.get("usuario") or ""),
                    self.driver,
                    email,
                    self.proxy,
                )
            except Exception:  # pragma: no cover - defensa extrema
                pass
            return resultado
        except Exception as e:
            resultado["error"] = f"error inesperado en el registro: {e}"
            logger.error(
                f"Change.org: fallo el registro/login de {email or '(sin email)'}: {e}"
            )
            return resultado

    # ------------------------------------------------------------------ #
    # Formulario
    # ------------------------------------------------------------------ #
    def _campo_por_etiqueta(self, etiqueta: str, tipo: str):
        """Resuelve un campo por su <label>: for/id -> envolvente -> following."""
        for label in self._buscar_elementos(By.TAG_NAME, "label"):
            texto = _normalizar(self._texto_elemento(label))
            if not texto or etiqueta not in texto:
                continue
            try:
                for_id = str(label.get_attribute("for") or "").strip()
            except Exception:
                for_id = ""
            if for_id:
                for elemento in self._buscar_elementos(
                    By.CSS_SELECTOR, f"[id='{for_id}']"
                ):
                    if self._visible(elemento):
                        return elemento
            # label envolvente (<label>Nombre <input ...></label>)
            for selector in ("input", "textarea", "[contenteditable='true']"):
                for elemento in self._buscar_elementos_de(
                    label, By.CSS_SELECTOR, selector
                ):
                    if self._visible(elemento):
                        return elemento
            # label "suelta": input/textarea inmediatamente siguiente.
            tag = "textarea" if tipo == "motivo" else "input"
            for elemento in self._buscar_elementos_de(
                label, By.XPATH, f"./following::{tag}[1]"
            ):
                if self._visible(elemento):
                    return elemento
            if tipo != "motivo":
                for elemento in self._buscar_elementos_de(
                    label, By.XPATH, "./following::textarea[1]"
                ):
                    if self._visible(elemento):
                        return elemento
        return None

    def _resolver_campo(self, tipo: str):
        """Devuelve el elemento del campo pedido (selectores -> etiquetas).

        Para `motivo` agrega un respaldo JS (solo con el modal de denuncia
        abierto): si Selenium no "ve" los campos del modal (radios invisibles
        para `find_elements`, reportado en vivo) tampoco vera el textarea
        recien montado por React.
        """
        for by, selector in _SELECTORES_CAMPOS.get(tipo, ()):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and self._interactuable(elemento):
                    return elemento
        for etiqueta in _ETIQUETAS_CAMPOS.get(tipo, ()):
            elemento = self._campo_por_etiqueta(etiqueta, tipo)
            if elemento is not None:
                return elemento
        if tipo == "motivo" and self._modal_denuncia_presente():
            elemento = self._campo_motivo_js()
            if elemento is not None:
                return elemento
        return None

    def escribir_humano(self, elemento, texto: str, pausas_rapidas: bool = False) -> bool:
        """Escribe caracter por caracter con send_keys (eventos REALES de teclado).

        Clic tolerante + pausa 0.02-0.12s por caracter (0.02-0.04s con
        `pausas_rapidas`, para quejas largas del modal), con typo ocasional
        (~5%) corregido al instante con BACKSPACE. Devuelve False si no se pudo
        escribir; nunca lanza.
        """
        if elemento is None:
            return False
        texto = str(texto or "")
        if not texto:
            return False
        pausa_max = 0.04 if pausas_rapidas else 0.12
        try:
            try:
                elemento.click()
                time.sleep(random.uniform(0.05, 0.2))
            except Exception:
                try:
                    self.driver.execute_script("arguments[0].focus();", elemento)
                except Exception:
                    pass
            for char in texto:
                if random.random() < 0.05:
                    erroneo = random.choice("abcdefghijklmnopqrstuvwxyz")
                    elemento.send_keys(erroneo)
                    time.sleep(random.uniform(0.02, 0.06))
                    elemento.send_keys(Keys.BACKSPACE)
                    time.sleep(random.uniform(0.02, 0.06))
                elemento.send_keys(char)
                time.sleep(random.uniform(0.02, pausa_max))
            return True
        except Exception as e:
            self.ultimo_error = f"error escribiendo en el formulario: {e}"
            logger.warning(f"Change.org: {self.ultimo_error}")
            return False

    # ------------------------------------------------------------------ #
    # Modal real "Denunciar abuso": radio 3 + pais + textarea
    # ------------------------------------------------------------------ #
    def _radios_denuncia(self) -> list:
        """Radios visibles del modal de denuncia (motive del reporte).

        Si Selenium no los encuentra (reportado en vivo: `find_elements`
        devolvia 0 con el modal abierto), cae a un respaldo JS con
        `execute_script` que SI los ve; los radios del respaldo NO se filtran
        por `is_displayed` (la UI real los reporta con `display:inline-block`,
        pero un falso negativo de Selenium no debe ocultarlos).
        """
        radios = []
        for by, selector in (
            (By.CSS_SELECTOR, "input[type='radio']"),
            (By.CSS_SELECTOR, "[role='radio']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and elemento not in radios:
                    radios.append(elemento)
        if not radios:
            radios = self._radios_denuncia_js()
        return radios

    def _radios_denuncia_js(self) -> list:
        """Radios del DOM via JS puro (`execute_script`), sin `find_elements`."""
        try:
            encontrados = self.driver.execute_script(
                "return Array.prototype.slice.call("
                "document.querySelectorAll(\"input[type='radio'], [role='radio']\"));"
            )
        except Exception:
            return []
        if not encontrados:
            return []
        try:
            return list(encontrados)
        except Exception:
            return []

    def _texto_radio(self, radio) -> str:
        """Texto asociado a un radio: aria-label/value/texto + label[for]/padre."""
        partes = []
        partes.append(self._texto_elemento(radio))
        for attr in ("aria-label", "value", "title"):
            try:
                valor = radio.get_attribute(attr)
            except Exception:
                valor = ""
            if valor:
                partes.append(str(valor))
        try:
            radio_id = str(radio.get_attribute("id") or "").strip()
        except Exception:
            radio_id = ""
        if radio_id:
            for label in self._buscar_elementos(By.TAG_NAME, "label"):
                try:
                    if str(label.get_attribute("for") or "").strip() == radio_id:
                        partes.append(self._texto_elemento(label))
                except Exception:
                    continue
        try:
            padre = radio.find_element(By.XPATH, "..")
            partes.append(self._texto_elemento(padre))
        except Exception:
            pass
        return _normalizar(" ".join(str(p) for p in partes if p))

    def _texto_modal_denuncia(self) -> str:
        """Texto normalizado de los dialogos VISIBLES del modal ("" si no hay).

        Si `element.text` viene vacio se lee `innerText`/`textContent` por JS
        (tolerante): en vivo la evidencia del modal es el TEXTO del
        `[role=dialog]`, no la visibilidad de los radios.
        """
        partes = []
        for by, selector in (
            (By.CSS_SELECTOR, "[role='dialog']"),
            (By.CSS_SELECTOR, "[aria-modal='true']"),
            (By.TAG_NAME, "dialog"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not self._visible(elemento):
                    continue
                texto = self._texto_elemento(elemento)
                if not texto:
                    try:
                        texto = str(
                            self.driver.execute_script(
                                "return (arguments[0].innerText || "
                                "arguments[0].textContent || '');",
                                elemento,
                            )
                            or ""
                        )
                    except Exception:
                        texto = ""
                if texto:
                    partes.append(texto)
        return _normalizar(" ".join(partes))

    def _modal_denuncia_presente(self) -> bool:
        """True si un `[role=dialog]` VISIBLE trae el texto del modal.

        `find_elements` de los radios puede devolver 0 con el modal abierto
        (reportado en vivo); el texto del dialogo SI es fiable.
        """
        texto = self._texto_modal_denuncia()
        if not texto:
            return False
        return any(frase in texto for frase in _FRASES_MODAL_DENUNCIA)

    def _campo_motivo_js(self):
        """Campo del motivo via JS puro (o None).

        Respaldo para cuando Selenium no "ve" los campos del modal: busca
        `textarea`/contenteditable y, si no, el input de React Aria del modal
        real (`name="question1.answer"`, el texto opcional que aparece al
        marcar el tercer motivo). El input generico solo se acepta DENTRO del
        `[role=dialog]` para no tomar un campo ajeno de la pagina.
        """
        try:
            return self.driver.execute_script(
                "/* change_org:campo_motivo */"
                "var dialogo = document.querySelector(\"[role='dialog']\");"
                "var raiz = dialogo || document;"
                "var e = raiz.querySelector(\"textarea, [contenteditable='true']\");"
                "if (e) { return e; }"
                "if (!dialogo) { return null; }"
                "var campos = dialogo.querySelectorAll("
                "\"input[type='text'], input:not([type])\");"
                "var generico = null;"
                "for (var i = 0; i < campos.length; i++) {"
                "  var n = String(campos[i].name || '');"
                "  if (n.indexOf('question') === 0 || n.indexOf('answer') >= 0) {"
                "    return campos[i];"
                "  }"
                "  if (generico === null) { generico = campos[i]; }"
                "}"
                "return generico;"
            )
        except Exception:
            return None

    def _radio_marcado(self, radio) -> bool:
        """True si el radio esta marcado (`checked`/`aria-checked`)."""
        for atributo in ("checked", "aria-checked", "aria-selected"):
            try:
                valor = radio.get_attribute(atributo)
            except Exception:
                valor = None
            if valor in (True, 1, "1", "true", "True", "checked"):
                return True
        return False

    def _estado_radios_js(self) -> dict:
        """Estado de los radios leido por JS puro ({} si no se puede)."""
        try:
            estado = self.driver.execute_script(_JS_ESTADO_RADIOS)
        except Exception:
            return {}
        return estado if isinstance(estado, dict) else {}

    def _seleccionar_motivo_js(self) -> bool:
        """Marca el tercer motivo con JS puro (radios que Selenium no 've')."""
        try:
            estado = self.driver.execute_script(_JS_SELECCIONAR_MOTIVO)
        except Exception as e:
            logger.debug(f"Change.org: el JS para marcar el motivo fallo: {e}")
            return False
        return isinstance(estado, dict) and bool(
            estado.get("checked") or estado.get("textarea")
        )

    def _verificar_motivo_seleccionado(self, habia_textarea: bool = False) -> bool:
        """True si el motivo quedo marcado (evidencia de seleccion real).

        Evidencia: `checked`/`aria-checked="true"` en el tercer radio (o en
        cualquier radio: la UI real solo tiene 3), estado leido por JS puro
        (radios que Selenium no "ve") o que APAREZCA el textarea del motivo
        (`_esperar_textarea_motivo`). Si el textarea ya existia ANTES de la
        estrategia no cuenta como evidencia.
        """
        radios = self._radios_denuncia()
        if radios:
            tercero = (
                radios[_INDICE_MOTIVO_TERCERO]
                if len(radios) > _INDICE_MOTIVO_TERCERO
                else radios[-1]
            )
            if self._radio_marcado(tercero):
                return True
            if any(self._radio_marcado(radio) for radio in radios):
                return True
        estado = self._estado_radios_js()
        if estado and (estado.get("tercero") or estado.get("alguno")):
            return True
        if not habia_textarea:
            if self._esperar_textarea_motivo(intentos=3) is not None:
                return True
        return False

    def _etiqueta_motivo_tercero(self, radios=None):
        """Elemento clickable VISIBLE con el texto del tercer motivo (o None).

        Busca `label`, `div.cursor-pointer` y roles clickables; como respaldo
        sube desde el radio del tercer motivo a su `label`/padre.
        """
        for by, selector in (
            (By.CSS_SELECTOR, "label"),
            (By.CSS_SELECTOR, "div[class*='cursor-pointer']"),
            (By.CSS_SELECTOR, "[role='radio']"),
            (By.CSS_SELECTOR, "[role='button']"),
            (By.CSS_SELECTOR, "[role='option']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if not (self._visible(elemento) and self._interactuable(elemento)):
                    continue
                texto = _normalizar(self._texto_elemento(elemento))
                if texto and any(frase in texto for frase in _FRASES_MOTIVO_TERCERO):
                    return elemento
        for radio in list(radios) if radios else self._radios_denuncia():
            texto = self._texto_radio(radio)
            if not any(frase in texto for frase in _FRASES_MOTIVO_TERCERO):
                continue
            for xpath in ("./ancestor::label[1]", ".."):
                for padre in self._buscar_elementos_de(radio, By.XPATH, xpath):
                    if self._visible(padre):
                        return padre
        return None

    def _seleccionar_motivo_denuncia(self, radios=None):
        """Marca el TERCER motivo ("No me gusta esta petición..."). (ok, error).

        Estrategias EN CADENA, cada una VERIFICADA antes de pasar a la
        siguiente (los radios nativos del modal no siempre son visibles para
        `find_elements`; en vivo devolvia 0 con el modal abierto):
          1. radios de Selenium (`_radios_denuncia`, con respaldo JS) + clic
             normal/JS de `_clic_elemento`.
          2. etiqueta visible clickable con el texto del motivo
             (`_etiqueta_motivo_tercero`: label/div.cursor-pointer/padre).
          3. JS puro: `input[type=radio]` por `value` (`dislike_content`) o
             indice 2 con `.click()` + `dispatchEvent(MouseEvent)`.
        Verificacion: `checked`/`aria-checked="true"` en el tercero, estado
        por JS o que aparezca el textarea del motivo. Si NADA selecciona:
        `_ERROR_MOTIVO_NO_SELECCIONABLE`.
        """
        radios = list(radios) if radios else []
        if not radios:
            radios = self._radios_denuncia()
        habia_textarea = self._resolver_campo("motivo") is not None

        # (a) Radios de Selenium (o de su respaldo JS): clic normal.
        elegido = None
        for radio in radios:
            texto = self._texto_radio(radio)
            if any(frase in texto for frase in _FRASES_MOTIVO_TERCERO):
                elegido = radio
                break
        if elegido is None and len(radios) > _INDICE_MOTIVO_TERCERO:
            elegido = radios[_INDICE_MOTIVO_TERCERO]
        if elegido is not None and self._clic_elemento(elegido):
            if self._verificar_motivo_seleccionado(habia_textarea):
                return True, ""

        # (b) Etiqueta visible con el texto del tercer motivo (o el padre/label
        #     del radio), tambien con fallback JS dentro de `_clic_elemento`.
        etiqueta = self._etiqueta_motivo_tercero(radios)
        if etiqueta is not None and self._clic_elemento(etiqueta):
            if self._verificar_motivo_seleccionado(habia_textarea):
                return True, ""

        # (c) JS puro sobre los radios del DOM.
        self._seleccionar_motivo_js()
        if self._verificar_motivo_seleccionado(habia_textarea):
            return True, ""

        return False, _ERROR_MOTIVO_NO_SELECCIONABLE

    def _select_ubicacion(self):
        """Select de "¿Dónde vives?": por etiqueta -> el que tenga Mexico -> 1º."""
        for label in self._buscar_elementos(By.TAG_NAME, "label"):
            texto = _normalizar(self._texto_elemento(label))
            if not texto or not any(f in texto for f in _FRASES_UBICACION):
                continue
            for xpath in ("./select", "./following::select[1]"):
                for elemento in self._buscar_elementos_de(label, By.XPATH, xpath):
                    if self._visible(elemento):
                        return elemento
            try:
                for_id = str(label.get_attribute("for") or "").strip()
            except Exception:
                for_id = ""
            if for_id:
                for elemento in self._buscar_elementos(
                    By.CSS_SELECTOR, f"[id='{for_id}']"
                ):
                    if (
                        getattr(elemento, "tag", "") == "select"
                        and self._visible(elemento)
                    ):
                        return elemento
        for elemento in self._buscar_elementos(By.TAG_NAME, "select"):
            if self._visible(elemento) and self._opcion_mexico(elemento) is not None:
                return elemento
        for elemento in self._buscar_elementos(By.TAG_NAME, "select"):
            if self._visible(elemento):
                return elemento
        return None

    def _opcion_mexico(self, select):
        """<option> de Mexico dentro del select (None si no existe)."""
        for opcion in self._buscar_elementos_de(select, By.TAG_NAME, "option"):
            try:
                valor = _normalizar(opcion.get_attribute("value") or "")
            except Exception:
                valor = ""
            texto = _normalizar(self._texto_elemento(opcion))
            if (
                "mexico" in texto
                or "mexico" in valor
                or valor in ("mx", "mex")
                or texto in ("mx", "mex")
            ):
                return opcion
        return None

    def _opcion_seleccionada(self, opcion) -> bool:
        try:
            if opcion.get_attribute("selected") not in (None, "", False, "false"):
                return True
            if str(opcion.get_attribute("aria-selected") or "").lower() == "true":
                return True
        except Exception:
            pass
        return False

    def _seleccionar_pais_mexico(self) -> str:
        """Garantiza Mexico en "¿Dónde vives?" si es un <select>.

        Devuelve un detalle ("ya-mexico", "seleccionado", "sin-select", ...);
        si ya viene Mexico NO toca nada.
        """
        select = self._select_ubicacion()
        if select is None:
            return "sin-select"
        try:
            valor_actual = _normalizar(select.get_attribute("value") or "")
        except Exception:
            valor_actual = ""
        if valor_actual in ("mx", "mex", "mexico"):
            return "ya-mexico"
        opcion = self._opcion_mexico(select)
        if opcion is None:
            return "sin-opcion-mexico"
        if self._opcion_seleccionada(opcion):
            return "ya-mexico"
        try:
            opcion.click()
            return "seleccionado"
        except Exception:
            pass
        try:
            valor = opcion.get_attribute("value") or "MX"
            self.driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                select,
                valor,
            )
            return "seleccionado-js"
        except Exception as e:
            logger.debug(f"Change.org: no se pudo marcar Mexico en el select: {e}")
            return "no-seleccionado"

    def _esperar_textarea_motivo(self, intentos: int = 10):
        """Espera (~5s) el textarea que aparece tras marcar el tercer motivo."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return None
            elemento = self._resolver_campo("motivo")
            if elemento is not None:
                return elemento
            time.sleep(0.5)
        return None

    def _llenar_formulario(self, identidad: dict, queja: str):
        """Llena el modal real (radio 3 + Mexico + textarea). (ok, motivo_error).

        Si el modal no expone radios de motivo, cae al formulario clasico
        Nombre/Apellido/Correo/Motivo (compatibilidad).
        """
        if self._cancelado():
            return False, MENSAJE_CANCELADO
        texto_motivo = str(queja or self.contexto or "")
        if not texto_motivo:
            return False, "no hay texto de queja para el formulario"

        radios = self._radios_denuncia()
        if radios or self._modal_denuncia_presente():
            ok_radio, problema = self._seleccionar_motivo_denuncia(radios)
            if not ok_radio:
                return False, problema
            time.sleep(random.uniform(0.3, 0.8))
            self._seleccionar_pais_mexico()
            elemento = self._esperar_textarea_motivo()
            if elemento is None:
                return False, "no se encontro el campo de motivo en el formulario"
            if not self.escribir_humano(
                elemento, texto_motivo, pausas_rapidas=len(texto_motivo) > 300
            ):
                return False, "no se pudo escribir el motivo del reporte"
            time.sleep(random.uniform(0.15, 0.45))
            return True, ""

        # Formulario clasico (fallback): Nombre/Apellido/Correo + Motivo.
        identidad = dict(identidad or {})
        campos = (
            ("nombre", str(identidad.get("nombre") or "")),
            ("apellido", str(identidad.get("apellido") or "")),
            ("email", str(identidad.get("email") or "")),
        )
        for tipo, valor in campos:
            if not valor:
                continue
            elemento = self._resolver_campo(tipo)
            if elemento is None:
                return False, f"no se encontro el campo de {tipo} en el formulario"
            if not self.escribir_humano(elemento, valor):
                return False, f"no se pudo escribir el campo de {tipo}"
            time.sleep(random.uniform(0.15, 0.45))
        elemento = self._resolver_campo("motivo")
        if elemento is None:
            return False, "no se encontro el campo de motivo en el formulario"
        if not self.escribir_humano(elemento, texto_motivo):
            return False, "no se pudo escribir el motivo del reporte"
        time.sleep(random.uniform(0.15, 0.45))
        return True, ""

    def _es_submit(self, elemento) -> bool:
        try:
            return str(elemento.get_attribute("type") or "").lower() == "submit"
        except Exception:
            return False

    def _texto_boton_enviar(self, elemento) -> bool:
        texto = _normalizar(self._texto_elemento(elemento))
        if not texto:
            try:
                texto = _normalizar(elemento.get_attribute("value") or "")
            except Exception:
                texto = ""
        if not texto:
            return False
        return any(texto == frase or texto.startswith(frase) for frase in _FRASES_ENVIAR)

    def _buscar_boton_enviar(self, intentos: int = 20):
        """Busca el boton Enviar habilitado (~10s maximo)."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return None
            candidato_submit = None
            for by, selector in (
                (By.TAG_NAME, "button"),
                (By.CSS_SELECTOR, "[role='button']"),
                (By.CSS_SELECTOR, "input[type='submit']"),
            ):
                for elemento in self._buscar_elementos(by, selector):
                    if not (self._visible(elemento) and self._interactuable(elemento)):
                        continue
                    if self._texto_boton_enviar(elemento):
                        return elemento
                    if candidato_submit is None and self._es_submit(elemento):
                        candidato_submit = elemento
            if candidato_submit is not None:
                return candidato_submit
            time.sleep(0.5)
        return None

    # ------------------------------------------------------------------ #
    # Evaluacion del resultado (SOLO evidencia positiva)
    # ------------------------------------------------------------------ #
    def _texto_pagina(self) -> str:
        partes = []
        try:
            fuente = self.driver.page_source
            if fuente:
                partes.append(str(fuente))
        except Exception:
            pass
        try:
            cuerpo = self.driver.find_element(By.TAG_NAME, "body")
            texto = self._texto_elemento(cuerpo)
            if texto:
                partes.append(texto)
        except Exception:
            pass
        return "\n".join(partes)

    def _detectar_captcha(self) -> str:
        """Detecta un captcha VISIBLE ("" si no hay).

        Los SELECTORES exigen visibilidad real (`self._visible`). La
        comprobacion por TEXTO usa SOLO el texto visible (`_texto_visible`),
        NUNCA `page_source`: los scripts/config del sitio mencionan
        "recaptcha"/"hcaptcha"/"captcha" aunque no haya captcha (falso positivo
        reportado en la home logueada de Change.org). Por lo mismo, los
        terminos GENERICOS (`_FRASES_CAPTCHA_GENERICAS`) no cuentan como frase
        de texto: solo las señales inequivocas ("no soy un robot", "verifica
        que eres humano", ...).
        """
        selectores = (
            (By.CSS_SELECTOR, "iframe[src*='recaptcha']"),
            (By.CSS_SELECTOR, "iframe[src*='hcaptcha']"),
            (By.CSS_SELECTOR, ".g-recaptcha"),
            (By.CSS_SELECTOR, "[class*='hcaptcha']"),
            (By.CSS_SELECTOR, "iframe[title*='recaptcha' i]"),
        )
        for by, selector in selectores:
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento):
                    return selector
        texto = _normalizar(self._texto_visible())
        for frase in _FRASES_CAPTCHA:
            if frase in _FRASES_CAPTCHA_GENERICAS:
                continue
            if frase in texto:
                return frase
        return ""

    def _detectar_error_envio(self) -> str:
        texto = _normalizar(self._texto_pagina())
        if not texto:
            return ""
        for frase in _FRASES_ERROR:
            if frase in texto:
                return frase
        return ""

    def _formulario_presente(self) -> bool:
        """True si el formulario de denuncia esta montado.

        El modal de denuncia se detecta por el TEXTO de su `[role=dialog]`
        (en vivo `find_elements` de los radios devolvia 0 con el modal
        abierto); ademas se conservan los checks por campos/radios/clasico.
        """
        return (
            self._modal_denuncia_presente()
            or bool(self._radios_denuncia())
            or self._resolver_campo("motivo") is not None
            or self._resolver_campo("nombre") is not None
            or self._resolver_campo("email") is not None
        )

    def _reporte_exitoso(self, formulario_presente=None):
        """Devuelve (ok, evidencia/motivo). Exito SOLO con evidencia positiva.

        Prioridad: captcha -> error de pagina -> URL de confirmacion -> texto de
        gracias -> formulario desaparecido sin error. Nunca marca exito "por
        hacer clic".
        """
        captcha = self._detectar_captcha()
        if captcha:
            return False, f"captcha detectado: {captcha}"
        error = self._detectar_error_envio()
        if error:
            return False, f"error en la pagina: '{error}'"
        url = self._url_actual()
        url_norm = _normalizar(url)
        for fragmento in _FRASES_URL_EXITO:
            if fragmento and fragmento in url_norm:
                return True, f"URL de confirmacion: {url}"
        texto = _normalizar(self._texto_pagina())
        for frase in _FRASES_EXITO:
            if frase in texto:
                return True, f"confirmacion en pantalla: '{frase}'"
        if formulario_presente is None:
            try:
                formulario_presente = self._formulario_presente()
            except Exception:
                formulario_presente = True
        if not formulario_presente:
            return True, "el formulario de denuncia desaparecio sin mensaje de error"
        return False, "sin evidencia de exito en la pagina"

    def _esperar_confirmacion(self, intentos: int = 30):
        """Espera (~15s) la evidencia positiva del envio. Devuelve (ok, evidencia)."""
        ultima = ""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return False, MENSAJE_CANCELADO
            ok, evidencia = self._reporte_exitoso()
            if ok:
                return True, evidencia
            ultima = evidencia
            time.sleep(0.5)
        ok, evidencia = self._reporte_exitoso()
        if ok:
            return True, evidencia
        return False, evidencia or ultima

    def _capturar_evidencia(self) -> str:
        """Guarda un screenshot de la pantalla actual como evidencia del exito.

        Crea `_DIR_CAPTURAS_CHANGE` (`data/reportes/change/`) si falta y guarda
        ``change_{slug}_{YYYYmmdd_HHMMSS}.png``; el slug se sanea desde
        "usuario"/"email" de `self.cuenta` (`_slug_captura`) y, si el nombre se
        repite, agrega sufijo ``_1``, ``_2``... Devuelve la ruta del PNG o ""
        si falla; NUNCA lanza y solo loguea la ruta (nunca datos de la cuenta).
        """
        try:
            driver = getattr(self, "driver", None)
            if driver is None:
                return ""
            carpeta = _DIR_CAPTURAS_CHANGE
            os.makedirs(carpeta, exist_ok=True)
            marca = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"change_{_slug_captura(self.cuenta)}_{marca}"
            ruta = os.path.join(carpeta, f"{base}.png")
            sufijo = 1
            while os.path.exists(ruta) and sufijo <= 999:
                ruta = os.path.join(carpeta, f"{base}_{sufijo}.png")
                sufijo += 1
            if driver.save_screenshot(ruta) is False:
                logger.warning(
                    "Change.org: save_screenshot devolvio False; sin evidencia"
                )
                return ""
            logger.info(f"Change.org: evidencia del reporte guardada en {ruta}")
            return ruta
        except Exception as e:
            logger.warning(
                f"Change.org: no se pudo guardar la evidencia del reporte: {e}"
            )
            return ""

    def _capturar_fallo(self) -> str:
        """Captura de DEPURACION de un reporte fallido (solo diagnostico).

        Guarda ``debug_fallo_{slug}_{YYYYmmdd_HHMMSS}.png`` en
        `_DIR_CAPTURAS_CHANGE/debug/` (subcarpeta, para no mezclarla con las
        evidencias de exito). Nunca lanza, no altera el flujo y solo loguea la
        ruta; devuelve la ruta del PNG o "" si no se pudo.
        """
        try:
            driver = getattr(self, "driver", None)
            if driver is None:
                return ""
            carpeta = os.path.join(_DIR_CAPTURAS_CHANGE, "debug")
            os.makedirs(carpeta, exist_ok=True)
            marca = datetime.now().strftime("%Y%m%d_%H%M%S")
            base = f"debug_fallo_{_slug_captura(self.cuenta)}_{marca}"
            ruta = os.path.join(carpeta, f"{base}.png")
            sufijo = 1
            while os.path.exists(ruta) and sufijo <= 999:
                ruta = os.path.join(carpeta, f"{base}_{sufijo}.png")
                sufijo += 1
            if driver.save_screenshot(ruta) is False:
                logger.warning(
                    "Change.org: save_screenshot devolvio False; sin captura "
                    "de fallo"
                )
                return ""
            logger.info(f"Change.org: captura de fallo guardada en {ruta}")
            return ruta
        except Exception as e:
            logger.warning(
                f"Change.org: no se pudo guardar la captura de fallo: {e}"
            )
            return ""

    # ------------------------------------------------------------------ #
    # Flujo principal
    # ------------------------------------------------------------------ #
    def reportar(self, identidad: dict = None, queja: str = "") -> dict:
        """Corre el flujo completo de UN reporte de violacion de politicas.

        Si el bot trae `cuenta`, PRIMERO ejecuta `registrar_o_entrar()` (tras
        `preparar_driver()`): si el registro/login falla, devuelve ok=False con
        ese error y no toca la peticion; si entra bien, sigue con el reporte.

        Devuelve ``{"ok","email","nombre","apellido","queja","error","evidencia",
        "url","captura"}`` (+ ``"estado_cuenta"`` en modo cuenta; + ``"cancelado":
        True`` si se pidio el paro antes de enviar). `captura` es la ruta del
        screenshot de evidencia cuando ok=True y "" en cualquier otro caso
        (clave SIEMPRE presente); si el reporte FALLA con el navegador vivo se
        guarda ademas UNA captura de depuracion en
        `_DIR_CAPTURAS_CHANGE/debug/` (solo se loguea, no cambia el flujo).
        Si tras navegar `_pagina_error_servidor()` detecta la pagina de error de
        APLICACION de Change.org hace UN `driver.refresh()` + espera corta (~6s)
        y re-chequea; si persiste devuelve ``error = _ERROR_SERVIDOR_PETICION``
        SIN buscar el enlace (la captura de depuracion del finally se guarda
        igual). Con proxy lento, tras navegar (y tras el `refresh` del error de
        servidor) espera a que `document.readyState` sea 'complete' (~30s
        maximo, `_esperar_documento_listo`) antes de buscar el enlace; si el
        enlace no aparece, `_buscar_enlace_reporte` hace UNA segunda pasada con
        `refresh` + espera antes de rendirse.

        Timing/hidratacion: tras `_cerrar_banner_cookies()` espera BEST-EFFORT
        a la app (`_esperar_app_peticion(30)`) y luego busca el enlace. Si el
        modal de denuncia no aparece, hace una CADENA de intentos (log INFO por
        intento): (1) clic + `_esperar_formulario(30)`; (2) 3s + re-localizar el
        enlace + clic + `_esperar_formulario(25)`; (3) UNA recarga completa
        (`_navegar(url_peticion)` + `_esperar_documento_listo(30)` +
        `_esperar_app_peticion(30)` + re-localizar + clic + `_esperar_formulario(25)`);
        (4) ULTIMO recurso: si el enlace trae un `href` http(s) de change.org
        (nunca "#"/javascript), navega directo a el + `_esperar_formulario(25)`.
        Si aun asi no aparece: ``"el formulario de reporte no aparecio
        (url=...)"``. La cancelacion (`_cancelado`) y el error de red/servidor
        se siguen respetando en todos los pasos. Nunca lanza.
        """
        identidad = dict(identidad or {})
        if not identidad and self.cuenta:
            identidad = _identidad_desde_cuenta(self.cuenta)
        resultado = {
            "ok": False,
            "email": str(identidad.get("email") or ""),
            "nombre": str(identidad.get("nombre") or ""),
            "apellido": str(identidad.get("apellido") or ""),
            "queja": str(queja or ""),
            "error": "",
            "evidencia": "",
            "url": self.url_peticion,
            "captura": "",
        }
        if self._cancelado():
            resultado["cancelado"] = True
            resultado["error"] = MENSAJE_CANCELADO
            return resultado
        if not self.url_peticion:
            resultado["error"] = "falta la URL de la peticion a reportar"
            return resultado

        def _paro() -> bool:
            if self._cancelado():
                resultado["cancelado"] = True
                resultado["error"] = MENSAJE_CANCELADO
                return True
            return False

        try:
            if not self.preparar_driver():
                resultado["error"] = self.ultimo_error or "no se pudo iniciar el navegador"
                return resultado

            if self.cuenta:
                registro = self.registrar_o_entrar()
                resultado["estado_cuenta"] = str(
                    registro.get("estado") or "fallo"
                )
                resultado["sesion_restaurada"] = bool(
                    registro.get("sesion_restaurada")
                )
                if not registro.get("ok"):
                    resultado["error"] = str(
                        registro.get("error")
                        or "no se pudo registrar o iniciar sesion en Change.org"
                    )
                    if self._cancelado():
                        resultado["cancelado"] = True
                        resultado["error"] = MENSAJE_CANCELADO
                    return resultado
                if _paro():
                    return resultado

            codigo_red = self._navegar(self.url_peticion)
            resultado["url"] = self._url_actual() or resultado["url"]
            if _paro():
                return resultado
            if codigo_red:
                # Proxy con TLS/red rota: fallar YA (no buscar enlaces en una
                # pagina de error ni reportar "no se encontro el enlace").
                resultado["error"] = self.ultimo_error
                return resultado

            # Proxy lento: `get()` puede retornar con la pagina a medio cargar
            # (imagenes rotas, paneles con spinner). Se espera a que el DOM
            # quede listo ANTES de detectar errores y buscar el enlace.
            documento_listo = self._esperar_documento_listo(30)
            if documento_listo:
                logger.debug(
                    "Change.org: documento listo tras navegar a la peticion"
                )
            else:
                logger.info(
                    "Change.org: el documento no quedo listo en 30s; sigo con "
                    "la busqueda del enlace"
                )
            if _paro():
                return resultado

            # Error de APLICACION de Change.org ("¡Oh no! Error del servidor"):
            # la pantalla no es la peticion. UN refresh suele recuperarla; si
            # persiste, fallar con el mensaje claro y NO buscar el enlace.
            error_servidor = self._pagina_error_servidor()
            if error_servidor:
                logger.warning(
                    "Change.org: la peticion respondio con la pagina de error "
                    f"de servidor ('{error_servidor}'); refresco UNA vez"
                )
                try:
                    self.driver.refresh()
                except Exception as e:
                    logger.debug(f"Change.org: el refresh fallo: {e}")
                time.sleep(6)
                # El refresh tambien puede tardar con proxy lento: se espera de
                # nuevo a que el DOM quede listo antes de re-chequear.
                listo_refresh = self._esperar_documento_listo(30)
                logger.debug(
                    "Change.org: documento tras el refresh: "
                    + ("listo" if listo_refresh else "no listo")
                )
                if _paro():
                    return resultado
                if self._pagina_error_servidor():
                    resultado["error"] = _ERROR_SERVIDOR_PETICION
                    return resultado

            self._cerrar_banner_cookies()
            if _paro():
                return resultado

            # La app React puede tardar en hidratar (el panel "Firma esta
            # peticion" se queda con spinner y el clic NO abre el modal). La
            # espera es BEST-EFFORT: no bloquea el flujo si no confirma.
            self._esperar_app_peticion(30)
            if _paro():
                return resultado

            enlace = self._buscar_enlace_reporte()
            if enlace is None:
                if not self._cancelado():
                    if self._pagina_error_servidor():
                        # Red de seguridad: la app fallo despues del chequeo
                        # inicial y seguir buscando el enlace solo daria el
                        # error generico engañoso.
                        resultado["error"] = _ERROR_SERVIDOR_PETICION
                    else:
                        resultado["error"] = (
                            "no se encontro el enlace de reporte de violacion de politicas"
                        )
                return resultado
            # Href http(s) del mismo dominio: ULTIMO recurso por navegacion
            # directa si ningun clic logra abrir el modal (nunca "#").
            href_directo = self._href_directo_enlace(enlace)

            # Intento 1: clic normal + espera del modal (30s).
            logger.info(
                "Change.org: intento 1: clic en el enlace de reporte y espera "
                f"del modal (url={self._url_actual() or self.url_peticion})"
            )
            abierto = self._clic_elemento(enlace) and self._esperar_formulario(30)
            if not abierto and not self._cancelado():
                # Intento 2: React quiza hidrato despues del primer clic; se
                # espera 3s, se RE-LOCALIZA el enlace y se reintenta el clic
                # (nativo + fallback JS dentro de `_clic_elemento`).
                logger.info(
                    "Change.org: el modal no aparecio al primer clic; espero 3s, "
                    "re-localizo el enlace y reintento"
                )
                time.sleep(3)
                if _paro():
                    return resultado
                enlace = self._buscar_enlace_reporte(intentos=3)
                if enlace is not None:
                    abierto = self._clic_elemento(enlace) and self._esperar_formulario(25)
                if _paro():
                    return resultado
            if not abierto and not self._cancelado():
                # Intento 3: UNA recarga COMPLETA del flujo (navegar la
                # peticion de nuevo + esperas + re-localizar + clic).
                logger.info(
                    "Change.org: el modal sigue sin aparecer; recargo la peticion "
                    "UNA vez y repito el flujo"
                )
                codigo_red = self._navegar(self.url_peticion)
                if not codigo_red:
                    self._esperar_documento_listo(30)
                    self._esperar_app_peticion(30)
                if _paro():
                    return resultado
                enlace = self._buscar_enlace_reporte(intentos=3)
                if enlace is not None:
                    abierto = self._clic_elemento(enlace) and self._esperar_formulario(25)
                if _paro():
                    return resultado
            if not abierto and not self._cancelado() and href_directo:
                # ULTIMO recurso: el enlace trae un href real de change.org;
                # navegar directo a el y esperar el modal/formulario.
                logger.info(
                    "Change.org: el modal no aparece; ultimo recurso, navego "
                    f"directo al href del enlace ({href_directo})"
                )
                self._navegar(href_directo)
                abierto = self._esperar_formulario(25)
                if _paro():
                    return resultado
            if not abierto:
                if self._cancelado():
                    resultado["cancelado"] = True
                    resultado["error"] = MENSAJE_CANCELADO
                else:
                    resultado["error"] = (
                        "el formulario de reporte no aparecio "
                        f"(url={self._url_actual() or self.url_peticion})"
                    )
                return resultado

            ok_formulario, problema = self._llenar_formulario(identidad, queja)
            if not ok_formulario:
                resultado["error"] = problema
                return resultado
            if _paro():
                return resultado

            boton = self._buscar_boton_enviar()
            if boton is not None and _paro():
                return resultado
            if boton is None:
                if not self._cancelado():
                    resultado["error"] = (
                        "no se encontro o no se habilito el boton Enviar (~10s)"
                    )
                return resultado
            if not self._clic_elemento(boton):
                resultado["error"] = self.ultimo_error or "no se pudo pulsar Enviar"
                return resultado

            ok, evidencia = self._esperar_confirmacion()
            resultado["ok"] = bool(ok)
            resultado["evidencia"] = evidencia
            resultado["url"] = self._url_actual() or resultado["url"]
            if ok:
                # Evidencia visual SOLO del exito, con el driver aun vivo.
                resultado["captura"] = self._capturar_evidencia()
            if not ok and not self._cancelado():
                resultado["error"] = evidencia or "el envio no se pudo confirmar"
            if self._cancelado():
                resultado["cancelado"] = True
                resultado["error"] = MENSAJE_CANCELADO
            return resultado
        except Exception as e:
            resultado["error"] = f"error inesperado: {e}"
            logger.error(
                f"Change.org: error reportando {resultado['email'] or '(sin email)'}: {e}"
            )
            return resultado
        finally:
            # Diagnostico: UNA captura de depuracion si el reporte fallo y el
            # navegador sigue vivo (tolerante: nunca cambia el resultado).
            try:
                if not resultado["ok"] and not resultado.get("cancelado"):
                    self._capturar_fallo()
            except Exception:
                pass

    def _esperar_formulario(self, intentos: int = 20) -> bool:
        """Espera (~10s) a que monte el modal de denuncia (o el formulario nuevo)."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return False
            if self._formulario_presente():
                return True
            time.sleep(0.5)
        return self._formulario_presente()


# --------------------------------------------------------------------------- #
# Orquestacion: un reporte y campana
# --------------------------------------------------------------------------- #
def _queja_minima(contexto: str = "") -> str:
    """Queja local de respaldo si la IA devolviera algo vacio (defensa extra)."""
    base = (
        "Considero que esta peticion incumple las normas de la comunidad de "
        "Change.org porque difunde afirmaciones sin respaldo y no ofrece "
        "pruebas verificables."
    )
    contexto = str(contexto or "").strip()
    if contexto:
        base += f" Motivo del reporte: {contexto[:200]}."
    return base + " Pido que la plataforma la revise y actue conforme a sus reglas."


class _SelectorProxy:
    """Elige el siguiente proxy VALIDO en round-robin (thread-safe).

    - Antes de usar un candidato lo valida UNA vez con `proxy_change_valido`
      (una validacion por proxy y corrida; no se re-valida en la misma corrida).
    - Los candidatos que no validan (TLS/red rota) se SALTAN y se prueba el
      siguiente, hasta ``len(proxies)`` candidatos.
    - Si ninguno valida -> ``""`` (sin proxy) y suma ``sin_proxy``.
    - ``descartados``: proxies UNICOS descartados por la validacion.
    Nunca lanza.
    """

    def __init__(self, proxies):
        self._proxies = [str(p) for p in (proxies or []) if p]
        self._lock = threading.Lock()
        self._validando_lock = threading.Lock()
        self._estado: dict = {}   # proxy -> bool (validado en esta corrida)
        self._indice = 0
        self.sin_proxy = 0
        self.descartados = 0

    def siguiente(self) -> str:
        """Devuelve el proximo proxy valido o "" si no hay/no valida ninguno."""
        intentos = 0
        total = len(self._proxies)
        while intentos < total:
            with self._lock:
                indice = self._indice % total
                self._indice += 1
                proxy = self._proxies[indice]
                estado = self._estado.get(proxy)
            intentos += 1
            if estado is True:
                return proxy
            if estado is False:
                continue
            # Validacion serializada con re-chequeo: UNA por proxy y corrida,
            # aunque varios workers pidan proxy a la vez.
            try:
                with self._validando_lock:
                    with self._lock:
                        estado = self._estado.get(proxy)
                    if estado is None:
                        estado = bool(proxy_change_valido(proxy))
                        with self._lock:
                            self._estado[proxy] = estado
                            if not estado:
                                self.descartados += 1
            except Exception:
                estado = True  # nunca descartar por incertidumbre
            if estado:
                return proxy
        with self._lock:
            self.sin_proxy += 1
        return ""


def ejecutar_un_reporte(
    url_peticion: str,
    contexto: str = "",
    proxy: str = "",
    headless: bool = None,
    evitar=None,
    cancelar=None,
    guardar_identidad: bool = True,
    cuenta=None,
    esperar_captcha_seg=None,
    *,
    _aviso=None,
) -> dict:
    """Genera identidad + queja IA, corre el bot y guarda la identidad si fue OK.

    Modo clasico (sin `cuenta`): genera identidad + queja IA, corre el bot en
    anonimo y persiste la identidad en la granja si el reporte fue OK.
    Modo cuenta (`cuenta` = dict con "usuario","email","password"/"email_password",
    "nombre","apellido","nombre_mostrado"): NO llama a `generar_identidad_change`
    (usa los datos de la cuenta), NO guarda en la granja
    (``identidad_guardada=False``) y agrega ``"estado_cuenta"`` y ``"usuario"``
    al resultado. El bot hace login/registro en Change.org ANTES de reportar.
    `esperar_captcha_seg` activa el MODO ASISTIDO del bot (None -> env
    CHANGE_ESPERAR_CAPTCHA_SEG -> 0; ver `ChangeOrgReportBot`). `_aviso` es
    interno: el callback de aviso de espera que usan las campanas.

    Devuelve el dict de `reportar()` + ``{"identidad", "identidad_guardada",
    "guardado", "cancelado"}``. Con `cancelar` ya seteado devuelve
    ``{"ok": False, "cancelado": True, "error": MENSAJE_CANCELADO}`` sin abrir
    navegador. Cierra el navegador SIEMPRE (finally).
    """
    if cancelar is not None:
        try:
            if cancelar.is_set():
                return {
                    "ok": False,
                    "cancelado": True,
                    "error": MENSAJE_CANCELADO,
                }
        except Exception:
            pass

    esperar_captcha_seg = _resolver_esperar_captcha_seg(esperar_captcha_seg)
    cuenta = dict(cuenta or {}) if isinstance(cuenta, dict) else {}
    if cuenta:
        identidad = _identidad_desde_cuenta(cuenta)
    else:
        identidad = generar_identidad_change()
    queja = ""
    try:
        # Import PEREZOSO: ia.generador_contenido es pesado y el contrato pide
        # no importarlo a nivel modulo.
        from ia.generador_contenido import generar_queja_change

        datos = generar_queja_change(contexto, evitar=evitar)
        if isinstance(datos, dict):
            queja = str(datos.get("queja") or "")
    except Exception as e:
        logger.warning(f"Change.org: la IA de quejas fallo; uso respaldo: {e}")
    if not queja:
        queja = _queja_minima(contexto)

    bot = None
    try:
        bot = ChangeOrgReportBot(
            url_peticion=url_peticion,
            contexto=contexto,
            proxy=proxy,
            headless=headless,
            cancelar=cancelar,
            cuenta=cuenta or None,
            esperar_captcha_seg=esperar_captcha_seg,
            aviso=_aviso,
        )
        resultado = bot.reportar(identidad=identidad, queja=queja)
        if not isinstance(resultado, dict):
            resultado = {}
    except Exception as e:
        logger.error(f"Change.org: el bot de reportes fallo: {e}")
        resultado = {"ok": False, "error": f"error inesperado: {e}"}
    finally:
        if bot is not None:
            bot.cerrar()

    resultado = dict(resultado)
    resultado.setdefault("email", identidad.get("email", ""))
    resultado.setdefault("nombre", identidad.get("nombre", ""))
    resultado.setdefault("apellido", identidad.get("apellido", ""))
    resultado.setdefault("queja", queja)
    resultado.setdefault("error", "")
    resultado.setdefault("evidencia", "")
    resultado.setdefault("url", str(url_peticion or ""))
    # Evidencia visual del reporte ("" si el backend no la capturo).
    resultado.setdefault("captura", "")
    # Clave adicional: True si el bot restauro la sesion persistida (cookies)
    # en vez de hacer login/registro (aplica sobre todo al modo cuenta).
    resultado.setdefault("sesion_restaurada", False)
    resultado["identidad"] = identidad
    resultado["cancelado"] = bool(resultado.get("cancelado", False)) or None

    if cuenta:
        resultado["usuario"] = str(cuenta.get("usuario") or "")
        resultado.setdefault("estado_cuenta", "fallo")
        resultado["identidad_guardada"] = False
        resultado["guardado"] = None
        return resultado

    guardado = None
    identidad_guardada = False
    if resultado.get("ok") and guardar_identidad:
        guardado = guardar_identidad_change(
            identidad,
            url_peticion=url_peticion,
            contexto=contexto,
            queja=queja,
            origen="reporte",
        )
        identidad_guardada = bool((guardado or {}).get("guardada"))
    resultado["identidad_guardada"] = identidad_guardada
    resultado["guardado"] = guardado
    return resultado


def registrar_cuenta_change(
    usuario: str = "",
    email: str = "",
    password: str = "",
    nombre: str = "",
    apellido: str = "",
    proxy: str = "",
    headless: bool = None,
    cancelar=None,
    esperar_captcha_seg=None,
    *,
    _aviso=None,
) -> dict:
    """Registra (o inicia sesion en) UNA cuenta de Change.org.

    Abre Chrome (crear_chrome + stealth + proxy), ejecuta
    `ChangeOrgReportBot.registrar_o_entrar()` y cierra SIEMPRE. `password` se
    normaliza con `password_change_para` (si viene corta se deriva). Los nombres
    vacios se completan con Faker es_MX (`_resolver_nombres`), nunca quedan
    vacios. NUNCA lanza.

    `esperar_captcha_seg` activa el MODO ASISTIDO dentro del bot (None -> env
    CHANGE_ESPERAR_CAPTCHA_SEG -> 0; acotado a [0, 900]); `_aviso` es interno:
    el callback de aviso de espera que usan las campanas.

    Resultado: ``{"ok","usuario","email","estado","nombre","apellido","error",
    "evidencia","url","cancelado","sesion_restaurada"}`` con ``estado in
    "nueva"|"existente"|"fallo"``; ``sesion_restaurada=True`` cuando entro con
    las cookies persistidas (sin captcha).
    """
    resultado = {
        "ok": False,
        "usuario": str(usuario or ""),
        "email": str(email or ""),
        "estado": "fallo",
        "nombre": str(nombre or ""),
        "apellido": str(apellido or ""),
        "error": "",
        "evidencia": "",
        "url": "",
        "cancelado": False,
        "sesion_restaurada": False,
    }
    if cancelar is not None:
        try:
            if cancelar.is_set():
                resultado["cancelado"] = True
                resultado["error"] = MENSAJE_CANCELADO
                return resultado
        except Exception:
            pass

    email = str(email or "").strip()
    clave = password_change_para(password)
    if not email or not clave:
        resultado["error"] = "la cuenta no tiene email o contraseña"
        return resultado

    esperar_captcha_seg = _resolver_esperar_captcha_seg(esperar_captcha_seg)
    nombre, apellido = _resolver_nombres(nombre, apellido, "")
    resultado["nombre"] = nombre
    resultado["apellido"] = apellido

    bot = None
    try:
        bot = ChangeOrgReportBot(
            proxy=proxy,
            headless=headless,
            cancelar=cancelar,
            cuenta={
                "usuario": resultado["usuario"],
                "email": email,
                "password": clave,
                "nombre": nombre,
                "apellido": apellido,
            },
            esperar_captcha_seg=esperar_captcha_seg,
            aviso=_aviso,
        )
        if not bot.preparar_driver():
            resultado["error"] = bot.ultimo_error or "no se pudo iniciar el navegador"
            return resultado
        registro = bot.registrar_o_entrar()
        if not isinstance(registro, dict):
            registro = {}
        resultado["ok"] = bool(registro.get("ok"))
        resultado["estado"] = str(registro.get("estado") or "fallo")
        resultado["error"] = str(registro.get("error") or "")
        resultado["evidencia"] = str(registro.get("evidencia") or "")
        resultado["sesion_restaurada"] = bool(registro.get("sesion_restaurada"))
        resultado["url"] = bot._url_actual()
        if cancelar is not None:
            try:
                if cancelar.is_set():
                    resultado["cancelado"] = True
                    if not resultado["error"]:
                        resultado["error"] = MENSAJE_CANCELADO
            except Exception:
                pass
        return resultado
    except Exception as e:
        resultado["error"] = f"error inesperado: {e}"
        logger.error(
            f"Change.org: fallo el registro de {email or '(sin email)'}: {e}"
        )
        return resultado
    finally:
        if bot is not None:
            bot.cerrar()


def ejecutar_campana_registros(
    cuentas,
    max_workers: int = 2,
    usar_proxies: bool = True,
    pais_proxy: str = "",
    headless: bool = None,
    cancelar=None,
    callback=None,
    esperar_captcha_seg=None,
) -> dict:
    """Registra/entra en Change.org con un lote de cuentas de la BD.

    - `cuentas`: lista de dicts ``{"usuario","email","email_password" (o
      "password"),"nombre_mostrado" (opcional)}``; cap 500.
    - Omitidas: cuentas sin email o sin contraseña (no abren navegador); se
      reportan con ``estado="omitida"`` y se cuentan en ``omitidas``.
    - Nombres: `nombre_mostrado` partido (>=2 palabras) o Faker es_MX; nunca
      vacios (via `_resolver_nombres`).
    - `esperar_captcha_seg`: MODO ASISTIDO (None -> env
      CHANGE_ESPERAR_CAPTCHA_SEG -> 0; acotado a [0, 900]). Cuando el bot avisa
      que espera el reto anti-bot, este callback recibe ``{"tipo":
      "espera_captcha","hechas":i,"total":N,"usuario":str,"email":str,
      "detalle":"esperando captcha (Ns)"}`` (tambien si el reto aparece al
      inicio del registro).
    - `callback`: ``{"tipo":"inicio","total":N}`` y por cuenta terminada
      ``{"tipo":"registro","hechas":i,"total":N,"ok":bool,"usuario":str,
      "email":str,"estado":str,"detalle":str}``.
    - `cancelar` (threading.Event): para de encolar (`shutdown(cancel_futures=
      True)`) y marca ``cancelada=True``.
    - Resumen: ``{"total","exitosos","fallidos","nuevas","existentes",
      "omitidas","cancelada","resultados","proxies_total","sin_proxy","error"}``
      donde `resultados` trae dicts con al menos ``ok, usuario, email, estado,
      detalle``. Proxy round-robin por cuenta. NUNCA lanza.
    """
    resumen = {
        "total": 0,
        "exitosos": 0,
        "fallidos": 0,
        "nuevas": 0,
        "existentes": 0,
        "omitidas": 0,
        "cancelada": False,
        "resultados": [],
        "proxies_total": 0,
        "sin_proxy": 0,
        "proxies_descartados": 0,
        "error": "",
    }
    try:
        lista = []
        try:
            for item in cuentas or []:
                if isinstance(item, dict):
                    lista.append(dict(item))
        except TypeError:
            lista = []
        if len(lista) > 500:
            lista = lista[:500]
        resumen["total"] = len(lista)

        try:
            workers = max(1, min(5, int(max_workers)))
        except (TypeError, ValueError):
            workers = 2

        esperar_captcha_seg = _resolver_esperar_captcha_seg(esperar_captcha_seg)

        def _evento_cancelado() -> bool:
            if cancelar is None:
                return False
            try:
                return bool(cancelar.is_set())
            except Exception:
                return False

        _notificar(callback, {"tipo": "inicio", "total": len(lista)})
        if _evento_cancelado():
            resumen["cancelada"] = True
            return resumen

        proxies = []
        if usar_proxies:
            try:
                proxies = [p for p in (proxies_disponibles(pais_proxy) or []) if p]
            except Exception:
                proxies = []
        resumen["proxies_total"] = len(proxies)

        lock = threading.Lock()
        contadores = {
            "hechas": 0,
            "exitosos": 0,
            "fallidos": 0,
            "nuevas": 0,
            "existentes": 0,
            "omitidas": 0,
        }
        total = len(lista)
        # Proxy round-robin SOLO con nodos validos (los de TLS/red rota se
        # descartan y se prueba el siguiente; una validacion por corrida).
        selector_proxy = _SelectorProxy(proxies)

        def _siguiente_proxy() -> str:
            return selector_proxy.siguiente()

        def _procesar(item: dict) -> None:
            resumen["resultados"].append(item)
            with lock:
                contadores["hechas"] += 1
                if item.get("estado") == "omitida":
                    contadores["omitidas"] += 1
                elif item.get("ok"):
                    contadores["exitosos"] += 1
                    if item.get("estado") == "nueva":
                        contadores["nuevas"] += 1
                    elif item.get("estado") == "existente":
                        contadores["existentes"] += 1
                else:
                    contadores["fallidos"] += 1
                hechas = contadores["hechas"]
            _notificar(
                callback,
                {
                    "tipo": "registro",
                    "hechas": hechas,
                    "total": total,
                    "ok": bool(item.get("ok")),
                    "usuario": str(item.get("usuario") or ""),
                    "email": str(item.get("email") or ""),
                    "estado": str(item.get("estado") or ""),
                    "detalle": str(item.get("detalle") or ""),
                },
            )

        def _omitida(cuenta: dict, detalle: str) -> None:
            _procesar(
                {
                    "ok": False,
                    "usuario": str(cuenta.get("usuario") or ""),
                    "email": str(cuenta.get("email") or ""),
                    "estado": "omitida",
                    "detalle": detalle,
                }
            )

        def _trabajo(datos: dict) -> dict:
            proxy = _siguiente_proxy()

            def _aviso_captcha(info: dict) -> None:
                """Reenvia a la UI la espera asistida de ESTA cuenta."""
                info = dict(info or {})
                with lock:
                    hechas = contadores["hechas"]
                _notificar(
                    callback,
                    {
                        "tipo": "espera_captcha",
                        "hechas": hechas,
                        "total": total,
                        "usuario": str(datos.get("usuario") or ""),
                        "email": str(datos.get("email") or ""),
                        "detalle": (
                            f"esperando captcha ({info.get('segundos')}s)"
                        ),
                    },
                )

            try:
                resultado = registrar_cuenta_change(
                    usuario=datos["usuario"],
                    email=datos["email"],
                    password=datos["password"],
                    nombre=datos["nombre"],
                    apellido=datos["apellido"],
                    proxy=proxy,
                    headless=headless,
                    cancelar=cancelar,
                    esperar_captcha_seg=esperar_captcha_seg,
                    _aviso=_aviso_captcha,
                )
            except Exception as e:
                resultado = {
                    "ok": False,
                    "usuario": datos.get("usuario", ""),
                    "email": datos.get("email", ""),
                    "estado": "fallo",
                    "error": f"{type(e).__name__}: {e}",
                }
            if not isinstance(resultado, dict):
                resultado = {}
            ok = bool(resultado.get("ok"))
            estado = str(resultado.get("estado") or ("nueva" if ok else "fallo"))
            detalle = "éxito" if ok else str(
                resultado.get("error") or "fallo sin detalle"
            )
            return {
                "ok": ok,
                "usuario": str(resultado.get("usuario") or datos.get("usuario") or ""),
                "email": str(resultado.get("email") or datos.get("email") or ""),
                "estado": estado,
                "detalle": detalle,
                "error": str(resultado.get("error") or ""),
                "evidencia": str(resultado.get("evidencia") or ""),
            }

        # Omitidas primero (sin navegador), luego las validas en paralelo.
        validas = []
        for cuenta in lista:
            email = str(cuenta.get("email") or "").strip()
            clave = password_change_para(
                cuenta.get("email_password") or cuenta.get("password") or ""
            )
            if not email or not clave:
                _omitida(cuenta, "sin email o contraseña")
                continue
            nombre, apellido = _resolver_nombres(
                cuenta.get("nombre"), cuenta.get("apellido"),
                cuenta.get("nombre_mostrado"),
            )
            validas.append(
                {
                    "usuario": str(cuenta.get("usuario") or ""),
                    "email": email,
                    "password": clave,
                    "nombre": nombre,
                    "apellido": apellido,
                }
            )

        cancelada = _evento_cancelado()
        if validas and not cancelada:
            executor = ThreadPoolExecutor(max_workers=workers)
            futuros: dict = {}
            siguiente = 0
            try:
                while True:
                    while (
                        not _evento_cancelado()
                        and siguiente < len(validas)
                        and len(futuros) < max(1, workers * 2)
                    ):
                        datos = validas[siguiente]
                        futuros[executor.submit(_trabajo, datos)] = siguiente
                        siguiente += 1
                    if _evento_cancelado():
                        cancelada = True
                        break
                    if not futuros:
                        break
                    completado = None
                    for futuro in as_completed(list(futuros)):
                        completado = futuro
                        break
                    if completado is None:
                        break
                    futuros.pop(completado, None)
                    try:
                        resultado = completado.result()
                    except Exception as e:
                        resultado = {
                            "ok": False,
                            "usuario": "",
                            "email": "",
                            "estado": "fallo",
                            "detalle": f"{type(e).__name__}: {e}",
                        }
                    if not isinstance(resultado, dict):
                        resultado = {
                            "ok": False,
                            "usuario": "",
                            "email": "",
                            "estado": "fallo",
                            "detalle": "respuesta invalida del registro",
                        }
                    _procesar(resultado)
                    if _evento_cancelado():
                        cancelada = True
            finally:
                if _evento_cancelado():
                    cancelada = True
                try:
                    executor.shutdown(wait=not cancelada, cancel_futures=cancelada)
                except TypeError:  # pragma: no cover - Python < 3.9
                    executor.shutdown(wait=not cancelada)

        resumen["exitosos"] = contadores["exitosos"]
        resumen["fallidos"] = contadores["fallidos"]
        resumen["nuevas"] = contadores["nuevas"]
        resumen["existentes"] = contadores["existentes"]
        resumen["omitidas"] = contadores["omitidas"]
        resumen["sin_proxy"] = selector_proxy.sin_proxy
        resumen["proxies_descartados"] = selector_proxy.descartados
        resumen["cancelada"] = bool(cancelada)
        logger.info(
            "Change.org: campana de registros terminada "
            f"({contadores['exitosos']} exitosos, {contadores['fallidos']} fallidos, "
            f"{contadores['omitidas']} omitidas, "
            f"{selector_proxy.descartados} proxies descartados"
            + (", cancelada" if cancelada else "")
            + ")"
        )
        return resumen
    except Exception as e:
        resumen["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"Change.org: la campana de registros fallo: {e}")
        return resumen


def ejecutar_campana_reportes(
    url_peticion: str,
    contexto: str = "",
    cantidad: int = 5,
    max_workers: int = 2,
    usar_proxies: bool = True,
    pais_proxy: str = "",
    guardar_identidades: bool = True,
    headless: bool = None,
    cancelar=None,
    callback=None,
    cuentas=None,
    esperar_captcha_seg=None,
) -> dict:
    """Lanza N reportes en paralelo con proxy round-robin por reporte.

    - `cantidad` se acota a [1, 200] y `max_workers` a [1, 5].
    - `cuentas` (opcional): lista de dicts de cuentas de la BD; con una lista
      no vacia cada reporte usa la SIGUIENTE cuenta en round-robin
      (`ejecutar_un_reporte(..., cuenta=...)`: login/registro primero). El
      evento de esa cuenta agrega ``"usuario"`` y ``"con_cuenta": True`` (sin
      ``"identidad"``) y el resumen agrega ``"con_cuentas"`` y
      ``"cuentas_total"``. Sin cuentas: flujo anonimo clasico intacto.
    - `esperar_captcha_seg`: MODO ASISTIDO (None -> env
      CHANGE_ESPERAR_CAPTCHA_SEG -> 0). Cuando el bot avisa que espera el reto
      anti-bot, el callback recibe ``{"tipo":"espera_captcha","hechas":i,
      "total":N,"usuario":str,"email":str,"detalle":"esperando captcha (Ns)"}``.
    - `callback` recibe ``{"tipo": "inicio", "total": N}`` y luego, por CADA
      reporte terminado, ``{"tipo": "reporte", "hechas": i, "total": N,
      "ok": bool, "email": str, "identidad": {...}, "detalle": str}``.
    - `cancelar` (threading.Event): deja de enviar nuevos reportes
      (``shutdown(cancel_futures=True)``) y marca ``cancelada=True``.
    - Resumen: ``{"total","enviados","fallidos","cancelada",
      "identidades_guardadas","resultados","proxies_total","sin_proxy",
      "proxies_descartados","error","con_cuentas","cuentas_total"}``. NUNCA lanza.
    """
    resumen = {
        "total": 0,
        "enviados": 0,
        "fallidos": 0,
        "cancelada": False,
        "identidades_guardadas": 0,
        "resultados": [],
        "proxies_total": 0,
        "sin_proxy": 0,
        "proxies_descartados": 0,
        "error": "",
        "con_cuentas": False,
        "cuentas_total": 0,
    }
    try:
        try:
            total = max(1, min(200, int(cantidad)))
        except (TypeError, ValueError):
            total = 5
        try:
            workers = max(1, min(5, int(max_workers)))
        except (TypeError, ValueError):
            workers = 2
        resumen["total"] = total

        esperar_captcha_seg = _resolver_esperar_captcha_seg(esperar_captcha_seg)

        cuentas_lista = []
        try:
            for item in cuentas or []:
                if isinstance(item, dict):
                    cuentas_lista.append(dict(item))
        except TypeError:
            cuentas_lista = []
        resumen["con_cuentas"] = bool(cuentas_lista)
        resumen["cuentas_total"] = len(cuentas_lista)

        def _evento_cancelado() -> bool:
            if cancelar is None:
                return False
            try:
                return bool(cancelar.is_set())
            except Exception:
                return False

        cancelada = _evento_cancelado()
        resumen["cancelada"] = cancelada
        _notificar(callback, {"tipo": "inicio", "total": total})
        if cancelada:
            return resumen

        proxies = []
        if usar_proxies:
            try:
                proxies = [p for p in (proxies_disponibles(pais_proxy) or []) if p]
            except Exception:
                proxies = []
        resumen["proxies_total"] = len(proxies)

        lock = threading.Lock()
        evitar: list = []
        contadores = {
            "hechas": 0,
            "enviados": 0,
            "fallidos": 0,
            "guardadas": 0,
            "cuenta": 0,
        }
        # Proxy round-robin SOLO con nodos validos (los de TLS/red rota se
        # descartan y se prueba el siguiente; una validacion por corrida).
        selector_proxy = _SelectorProxy(proxies)

        def _siguiente_proxy() -> str:
            return selector_proxy.siguiente()

        def _siguiente_cuenta():
            """Cuenta de la BD para el siguiente reporte (round-robin)."""
            with lock:
                if not cuentas_lista:
                    return None
                cuenta = cuentas_lista[contadores["cuenta"] % len(cuentas_lista)]
                contadores["cuenta"] += 1
                return dict(cuenta)

        def _trabajo(cuenta_actual=None) -> dict:
            proxy = _siguiente_proxy()
            with lock:
                evitar_actual = list(evitar)

            def _aviso_captcha(info: dict) -> None:
                """Reenvia a la UI la espera asistida de ESTE reporte."""
                info = dict(info or {})
                with lock:
                    hechas = contadores["hechas"]
                cuenta_info = dict(cuenta_actual or {})
                _notificar(
                    callback,
                    {
                        "tipo": "espera_captcha",
                        "hechas": hechas,
                        "total": total,
                        "usuario": str(cuenta_info.get("usuario") or ""),
                        "email": str(cuenta_info.get("email") or ""),
                        "detalle": (
                            f"esperando captcha ({info.get('segundos')}s)"
                        ),
                    },
                )

            try:
                kwargs = dict(
                    url_peticion=url_peticion,
                    contexto=contexto,
                    proxy=proxy,
                    headless=headless,
                    evitar=evitar_actual,
                    cancelar=cancelar,
                    guardar_identidad=guardar_identidades,
                    esperar_captcha_seg=esperar_captcha_seg,
                    _aviso=_aviso_captcha,
                )
                if cuenta_actual is not None:
                    kwargs["cuenta"] = cuenta_actual
                resultado = ejecutar_un_reporte(**kwargs)
            except Exception as e:
                resultado = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "email": "",
                    "identidad": {},
                    "identidad_guardada": False,
                }
            if not isinstance(resultado, dict):
                resultado = {
                    "ok": False,
                    "error": "respuesta invalida del reporte",
                    "identidad": {},
                }
            queja = str(resultado.get("queja") or "")
            with lock:
                if queja:
                    evitar.append(queja)
                    if len(evitar) > 500:
                        del evitar[:-500]
            return resultado

        def _procesar(resultado: dict) -> None:
            resumen["resultados"].append(resultado)
            with lock:
                contadores["hechas"] += 1
                if resultado.get("ok"):
                    contadores["enviados"] += 1
                else:
                    contadores["fallidos"] += 1
                if resultado.get("identidad_guardada"):
                    contadores["guardadas"] += 1
                hechas = contadores["hechas"]
            ok = bool(resultado.get("ok"))
            info = {
                "tipo": "reporte",
                "hechas": hechas,
                "total": total,
                "ok": ok,
                "email": str(resultado.get("email") or ""),
                "detalle": (
                    "éxito"
                    if ok
                    else str(resultado.get("error") or "fallo sin detalle")
                ),
            }
            if cuentas_lista:
                # Modo cuenta: el evento identifica la cuenta usada y NO lleva
                # "identidad" (esa es de la granja anonima).
                info["usuario"] = str(resultado.get("usuario") or "")
                info["con_cuenta"] = True
            else:
                info["identidad"] = dict(resultado.get("identidad") or {})
            _notificar(callback, info)

        executor = ThreadPoolExecutor(max_workers=workers)
        futuros: dict = {}
        siguiente = 0
        try:
            while True:
                # Manda trabajo mientras haya ventana y el usuario no cancela.
                while (
                    not _evento_cancelado()
                    and siguiente < total
                    and len(futuros) < max(1, workers * 2)
                ):
                    cuenta_actual = _siguiente_cuenta()
                    futuros[executor.submit(_trabajo, cuenta_actual)] = siguiente
                    siguiente += 1
                if _evento_cancelado():
                    # Paro inmediato: no se esperan los futuros en vuelo; el
                    # finally hace shutdown(cancel_futures=True).
                    cancelada = True
                    break
                if not futuros:
                    break
                completado = None
                for futuro in as_completed(list(futuros)):
                    completado = futuro
                    break
                if completado is None:
                    break
                futuros.pop(completado, None)
                try:
                    resultado = completado.result()
                except Exception as e:
                    resultado = {
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}",
                        "email": "",
                        "identidad": {},
                        "identidad_guardada": False,
                    }
                if not isinstance(resultado, dict):
                    resultado = {
                        "ok": False,
                        "error": "respuesta invalida del reporte",
                        "identidad": {},
                    }
                _procesar(resultado)
                if _evento_cancelado():
                    cancelada = True
        finally:
            if _evento_cancelado():
                cancelada = True
            try:
                executor.shutdown(wait=not cancelada, cancel_futures=cancelada)
            except TypeError:  # pragma: no cover - Python < 3.9 sin cancel_futures
                executor.shutdown(wait=not cancelada)

        resumen["enviados"] = contadores["enviados"]
        resumen["fallidos"] = contadores["fallidos"]
        resumen["identidades_guardadas"] = contadores["guardadas"]
        resumen["sin_proxy"] = selector_proxy.sin_proxy
        resumen["proxies_descartados"] = selector_proxy.descartados
        resumen["cancelada"] = bool(cancelada)
        logger.info(
            "Change.org: campana terminada "
            f"({contadores['enviados']} enviados, {contadores['fallidos']} fallidos, "
            f"{contadores['guardadas']} identidades guardadas, "
            f"{selector_proxy.descartados} proxies descartados"
            + (", cancelada" if cancelada else "")
            + ")"
        )
        return resumen
    except Exception as e:
        resumen["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"Change.org: la campana de reportes fallo: {e}")
        return resumen
