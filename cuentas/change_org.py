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
   Detecta captcha y errores de la pagina.

2. **Registro/Login**: `ChangeOrgReportBot.registrar_o_entrar()` crea la cuenta
   en Change.org (o entra a una existente) con el email/email_password de una
   cuenta de la BD via `https://www.change.org/login_or_join?user_flow=nav`:
   "Iniciar sesión" -> correo -> "Continuar" -> contraseña -> "Continuar" ->
   (solo cuentas nuevas) "Nombres"/"Apellidos" -> "Continuar". Estado "nueva"
   si vio "Crea tu contraseña", "existente" si entro por login y "fallo" si no
   se pudo confirmar la sesion. `registrar_cuenta_change()` y
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

3. **Granja de identidades**: `generar_identidad_change()` produce
   Nombre/Apellido/Correo/CP creibles (Faker es_MX) y
   `guardar_identidad_change()` las persiste en la tabla `cuenta_change` para
   las futuras firmas masivas (la columna `usada_firma` queda reservada).

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

    guardar_identidad_change(identidad, url_peticion="", contexto="", queja="",
                             origen="reporte") -> dict
        {"guardada": bool, "id": int|None, "motivo": str}
        - email duplicado -> {"guardada": False, "motivo": "duplicada",
          "id": <id existente>} (NO es error)
        - fallo real -> {"guardada": False, "motivo": "error", "error": str}
        - si la tabla no existe (OperationalError) llama UNA vez a
          core.database.init_db() y reintenta. NUNCA lanza.

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
        .registrar_o_entrar() -> {"ok","estado","error","evidencia"}
            estado in "nueva"|"existente"|"fallo"; nunca lanza.
        .reportar(identidad=None, queja="") -> dict
            Con `cuenta`: ejecuta registrar_o_entrar() tras preparar_driver();
            si el registro/login falla devuelve ok=False con ese error; si
            entra bien, sigue con el flujo de reporte (estado_cuenta incluido).
        .cerrar() -> None   (driver.quit con fallbacks + cerrar SIEMPRE el fwd)

    registrar_cuenta_change(usuario="", email="", password="", nombre="",
                            apellido="", proxy="", headless=None,
                            cancelar=None, esperar_captcha_seg=None) -> dict
        Abre Chrome (crear_chrome + stealth + proxy), registra/entra y cierra
        SIEMPRE. NUNCA lanza. Resultado: {"ok","usuario","email","estado",
        "nombre","apellido","error","evidencia","url","cancelado"}.
        `esperar_captcha_seg` activa el MODO ASISTIDO dentro del bot.

    ejecutar_campana_registros(cuentas, max_workers=2, usar_proxies=True,
                               pais_proxy="", headless=None, cancelar=None,
                               callback=None, esperar_captcha_seg=None) -> dict
        `cuentas`: lista de dicts {"usuario","email","email_password" (o
        "password"),"nombre_mostrado" (opcional)}; lista cap 500 y workers
        acotados a [1,5]; proxy round-robin por cuenta. Callback:
        {"tipo":"inicio","total":N}, por cuenta terminada {"tipo":"registro",
        "hechas":i,"total":N,"ok":bool,"usuario":str,"email":str,
        "estado":str,"detalle":str} (las omitidas, sin email/contraseña,
        tambien reportan con estado "omitida") y, si el modo asistido esta
        activo y aparece el reto anti-bot, {"tipo":"espera_captcha","hechas":i,
        "total":N,"usuario":str,"email":str,"detalle":"esperando captcha (Ns)"}.
        Resumen: {"total","exitosos","fallidos","nuevas","existentes",
        "omitidas","cancelada","resultados","proxies_total","sin_proxy",
        "error"}; NUNCA lanza.

    ejecutar_un_reporte(url_peticion, contexto="", proxy="", headless=None,
                        evitar=None, cancelar=None, guardar_identidad=True,
                        cuenta=None, esperar_captcha_seg=None) -> dict
        Sin `cuenta`: genera identidad + queja IA, corre el bot y persiste la
        identidad en la granja si el reporte fue OK (comportamiento clasico).
        Con `cuenta`: NO llama a generar_identidad_change (usa los datos de la
        cuenta), NO guarda en la granja (identidad_guardada=False) y agrega
        "estado_cuenta" y "usuario" al resultado. `esperar_captcha_seg` activa
        el MODO ASISTIDO del bot. Cierra SIEMPRE (finally).

    ejecutar_campana_reportes(url_peticion, contexto="", cantidad=5,
                              max_workers=2, usar_proxies=True, pais_proxy="",
                              guardar_identidades=True, headless=None,
                              cancelar=None, callback=None, cuentas=None,
                              esperar_captcha_seg=None) -> dict
        Con `cuentas` no vacias: cada reporte usa la siguiente cuenta en
        round-robin (login/registro primero); el evento "reporte" agrega
        "usuario" y "con_cuenta": True (sin "identidad") y el resumen agrega
        "con_cuentas" y "cuentas_total". Sin cuentas: flujo anonimo clasico.
        Con el modo asistido activo tambien emite {"tipo":"espera_captcha",
        "hechas":i,"total":N,"usuario":str,"email":str,
        "detalle":"esperando captcha (Ns)"}.

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

import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

from faker import Faker
from loguru import logger
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from core.config import detectar_chrome_version, settings
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
    "guardar_identidad_change",
    "registrar_cuenta_change",
    "ejecutar_un_reporte",
    "ejecutar_campana_reportes",
    "ejecutar_campana_registros",
]


MENSAJE_CANCELADO = "⛔ Ataque de reportes detenido por el usuario"

_URL_LOGIN_CHANGE = "https://www.change.org/login_or_join?user_flow=nav"

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

    def _navegar(self, url: str) -> None:
        """Abre la URL tolerando TimeoutException (sigue con esperas explicitas)."""
        try:
            self.driver.get(url)
        except TimeoutException as e:
            logger.warning(
                f"Change.org: timeout cargando {url}; sigo con esperas explicitas ({e})"
            )
        except Exception as e:
            raise RuntimeError(f"no se pudo abrir {url}: {e}")

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

    def _buscar_enlace_reporte(self, intentos: int = 12):
        """Busca el enlace con scroll humano incremental hasta encontrarlo."""
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

    def _evidencia_sesion(self, email: str = "") -> str:
        """Evidencia POSITIVA de sesion iniciada; "" si aun no se confirma."""
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
        return ""

    def registrar_o_entrar(self) -> dict:
        """Registra (cuenta NUEVA) o inicia sesion en Change.org.

        Flujo real: `login_or_join?user_flow=nav` -> "Iniciar sesión" ->
        "Dirección de correo electrónico" -> "Continuar" -> contraseña (nueva o
        de login) -> "Continuar" -> (solo nuevas) "Nombres"/"Apellidos" ->
        "Continuar". La contraseña sale de `password_change_para`.

        Devuelve ``{"ok","estado","error","evidencia"}`` con estado "nueva" si
        vio "Crea tu contraseña" (o la pantalla de nombre), "existente" si entro
        por login y "fallo" si no se pudo confirmar la sesion. NUNCA lanza.
        """
        resultado = {"ok": False, "estado": "fallo", "error": "", "evidencia": ""}
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

            self._navegar(_URL_LOGIN_CHANGE)
            if self._cancelado():
                resultado["error"] = MENSAJE_CANCELADO
                return resultado
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
            error = ""
            confirmado = ""
            reto_iter = 0
            for _ in range(60):
                if self._cancelado():
                    error = MENSAJE_CANCELADO
                    break
                captcha = self._detectar_captcha()
                if captcha:
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
        """Devuelve el elemento del campo pedido (selectores -> etiquetas)."""
        for by, selector in _SELECTORES_CAMPOS.get(tipo, ()):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and self._interactuable(elemento):
                    return elemento
        for etiqueta in _ETIQUETAS_CAMPOS.get(tipo, ()):
            elemento = self._campo_por_etiqueta(etiqueta, tipo)
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
        """Radios visibles del modal de denuncia (motive del reporte)."""
        radios = []
        for by, selector in (
            (By.CSS_SELECTOR, "input[type='radio']"),
            (By.CSS_SELECTOR, "[role='radio']"),
        ):
            for elemento in self._buscar_elementos(by, selector):
                if self._visible(elemento) and elemento not in radios:
                    radios.append(elemento)
        return radios

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

    def _seleccionar_motivo_denuncia(self, radios=None):
        """Marca el TERCER motivo ("No me gusta esta petición..."). (ok, error)."""
        radios = list(radios if radios is not None else self._radios_denuncia())
        if not radios:
            return False, "no se encontro el grupo de motivos de denuncia en el modal"
        elegido = None
        for radio in radios:
            texto = self._texto_radio(radio)
            if any(frase in texto for frase in _FRASES_MOTIVO_TERCERO):
                elegido = radio
                break
        if elegido is None:
            if len(radios) >= 3:
                elegido = radios[2]
            else:
                return False, (
                    "no se encontro el tercer motivo de denuncia "
                    "(no me gusta / no estoy de acuerdo) en el modal"
                )
        if not self._clic_elemento(elegido):
            return False, (
                self.ultimo_error or "no se pudo seleccionar el motivo de la denuncia"
            )
        return True, ""

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
        if radios:
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
        texto = _normalizar(self._texto_pagina())
        for frase in _FRASES_CAPTCHA:
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
        return (
            bool(self._radios_denuncia())
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

    # ------------------------------------------------------------------ #
    # Flujo principal
    # ------------------------------------------------------------------ #
    def reportar(self, identidad: dict = None, queja: str = "") -> dict:
        """Corre el flujo completo de UN reporte de violacion de politicas.

        Si el bot trae `cuenta`, PRIMERO ejecuta `registrar_o_entrar()` (tras
        `preparar_driver()`): si el registro/login falla, devuelve ok=False con
        ese error y no toca la peticion; si entra bien, sigue con el reporte.

        Devuelve ``{"ok","email","nombre","apellido","queja","error","evidencia",
        "url"}`` (+ ``"estado_cuenta"`` en modo cuenta; + ``"cancelado": True``
        si se pidio el paro antes de enviar). Nunca lanza.
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

            self._navegar(self.url_peticion)
            resultado["url"] = self._url_actual() or resultado["url"]
            if _paro():
                return resultado

            self._cerrar_banner_cookies()
            if _paro():
                return resultado

            enlace = self._buscar_enlace_reporte()
            if enlace is None:
                if not self._cancelado():
                    resultado["error"] = (
                        "no se encontro el enlace de reporte de violacion de politicas"
                    )
                return resultado
            if not self._clic_elemento(enlace):
                resultado["error"] = (
                    self.ultimo_error
                    or "no se pudo abrir el formulario de reporte de politicas"
                )
                return resultado
            if _paro():
                return resultado

            if not self._esperar_formulario():
                if not self._cancelado():
                    resultado["error"] = "el formulario de reporte no aparecio"
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
    "evidencia","url","cancelado"}`` con ``estado in "nueva"|"existente"|"fallo"``.
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
            "proxy": 0,
            "sin_proxy": 0,
            "hechas": 0,
            "exitosos": 0,
            "fallidos": 0,
            "nuevas": 0,
            "existentes": 0,
            "omitidas": 0,
        }
        total = len(lista)

        def _siguiente_proxy() -> str:
            with lock:
                if not proxies:
                    contadores["sin_proxy"] += 1
                    return ""
                proxy = proxies[contadores["proxy"] % len(proxies)]
                contadores["proxy"] += 1
                return proxy

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
        resumen["sin_proxy"] = contadores["sin_proxy"]
        resumen["cancelada"] = bool(cancelada)
        logger.info(
            "Change.org: campana de registros terminada "
            f"({contadores['exitosos']} exitosos, {contadores['fallidos']} fallidos, "
            f"{contadores['omitidas']} omitidas"
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
      "identidades_guardadas","resultados","proxies_total","sin_proxy","error",
      "con_cuentas","cuentas_total"}``. NUNCA lanza.
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
            "proxy": 0,
            "sin_proxy": 0,
            "hechas": 0,
            "enviados": 0,
            "fallidos": 0,
            "guardadas": 0,
            "cuenta": 0,
        }

        def _siguiente_proxy() -> str:
            with lock:
                if not proxies:
                    contadores["sin_proxy"] += 1
                    return ""
                proxy = proxies[contadores["proxy"] % len(proxies)]
                contadores["proxy"] += 1
                return proxy

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
        resumen["sin_proxy"] = contadores["sin_proxy"]
        resumen["cancelada"] = bool(cancelada)
        logger.info(
            "Change.org: campana terminada "
            f"({contadores['enviados']} enviados, {contadores['fallidos']} fallidos, "
            f"{contadores['guardadas']} identidades guardadas"
            + (", cancelada" if cancelada else "")
            + ")"
        )
        return resumen
    except Exception as e:
        resumen["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"Change.org: la campana de reportes fallo: {e}")
        return resumen
