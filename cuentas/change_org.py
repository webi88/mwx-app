"""Reportes de violacion de politicas en Change.org + granja de identidades.

Este modulo REEMPLAZA por completo al antiguo bot de FIRMAS (obsoleto). Tiene
dos misiones:

1. **Reportes**: `ChangeOrgReportBot` abre la peticion en Chrome, cierra el
   banner de cookies, localiza el enlace "Denunciar una violación de las
   políticas" (ES/EN, con scroll humano incremental), llena el formulario con
   una identidad sintetica y una queja generada por IA, y marca exito SOLO con
   evidencia positiva (texto de gracias, URL de confirmacion o desaparicion del
   formulario sin mensaje de error). Detecta captcha y errores de la pagina.

2. **Granja de identidades**: `generar_identidad_change()` produce
   Nombre/Apellido/Correo/CP creibles (Faker es_MX) y
   `guardar_identidad_change()` las persiste en la tabla `cuenta_change` para
   las futuras firmas masivas (la columna `usada_firma` queda reservada).

Contrato congelado (el frontend y los tests dependen de el; no romper):

    MENSAJE_CANCELADO = "⛔ Ataque de reportes detenido por el usuario"

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
                       timeout=45, cancelar=None)
        .preparar_driver() -> bool
        .reportar(identidad=None, queja="") -> dict
        .cerrar() -> None   (driver.quit con fallbacks + cerrar SIEMPRE el fwd)

    ejecutar_un_reporte(url_peticion, contexto="", proxy="", headless=None,
                        evitar=None, cancelar=None, guardar_identidad=True) -> dict
        Genera identidad + queja IA, corre el bot y persiste la identidad si el
        reporte fue OK. Cierra el navegador SIEMPRE (finally).

    ejecutar_campana_reportes(url_peticion, contexto="", cantidad=5,
                              max_workers=2, usar_proxies=True, pais_proxy="",
                              guardar_identidades=True, headless=None,
                              cancelar=None, callback=None) -> dict
        ThreadPoolExecutor con proxy round-robin y callback de progreso.

Reglas de oro del flujo Selenium:
    - `driver.get` tolera `TimeoutException` (sigue con esperas explicitas).
    - Escritura humana REAL con `send_keys` caracter por caracter (los eventos
      reales de teclado son los que evitan el shadowban).
    - Exito jamas "por haber hecho clic": siempre con evidencia positiva.
    - `cancelar` (threading.Event) se revisa antes de abrir el navegador,
      durante el scroll y antes de enviar.
"""
from __future__ import annotations

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
    "ChangeOrgReportBot",
    "generar_identidad_change",
    "proxies_disponibles",
    "guardar_identidad_change",
    "ejecutar_un_reporte",
    "ejecutar_campana_reportes",
]


MENSAJE_CANCELADO = "⛔ Ataque de reportes detenido por el usuario"

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
    "i'm not a robot",
    "i am not a robot",
    "recaptcha",
    "hcaptcha",
    "captcha",
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
    ):
        self.url_peticion = str(url_peticion or "")
        self.contexto = str(contexto or "")
        self.proxy = str(proxy or "")
        self.headless = headless
        self.timeout = int(timeout or 45)
        self.cancelar = cancelar
        self.driver = None
        self._fwd_proxy = None
        self.ultimo_error = ""

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

    def escribir_humano(self, elemento, texto: str) -> bool:
        """Escribe caracter por caracter con send_keys (eventos REALES de teclado).

        Clic tolerante + pausa 0.02-0.12s por caracter, con typo ocasional
        (~5%) corregido al instante con BACKSPACE. Devuelve False si no se pudo
        escribir; nunca lanza.
        """
        if elemento is None:
            return False
        texto = str(texto or "")
        if not texto:
            return False
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
                time.sleep(random.uniform(0.02, 0.12))
            return True
        except Exception as e:
            self.ultimo_error = f"error escribiendo en el formulario: {e}"
            logger.warning(f"Change.org: {self.ultimo_error}")
            return False

    def _llenar_formulario(self, identidad: dict, queja: str):
        """Llena Nombre/Apellido/Correo/Motivo. Devuelve (ok, motivo_error)."""
        if self._cancelado():
            return False, MENSAJE_CANCELADO
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
        texto_motivo = str(queja or self.contexto or "")
        if not texto_motivo:
            return False, "no hay texto de queja para el formulario"
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
            self._resolver_campo("motivo") is not None
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

        Devuelve ``{"ok","email","nombre","apellido","queja","error","evidencia",
        "url"}`` (+ ``"cancelado": True`` si se pidio el paro antes de enviar).
        Nunca lanza.
        """
        identidad = dict(identidad or {})
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
        """Espera (~10s) a que monte el formulario (modal o pagina nueva)."""
        for _ in range(max(1, intentos)):
            if self._cancelado():
                return False
            if (
                self._resolver_campo("nombre") is not None
                or self._resolver_campo("motivo") is not None
            ):
                return True
            time.sleep(0.5)
        return self._resolver_campo("motivo") is not None


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
) -> dict:
    """Genera identidad + queja IA, corre el bot y guarda la identidad si fue OK.

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
) -> dict:
    """Lanza N reportes en paralelo con proxy round-robin por reporte.

    - `cantidad` se acota a [1, 200] y `max_workers` a [1, 5].
    - `callback` recibe ``{"tipo": "inicio", "total": N}`` y luego, por CADA
      reporte terminado, ``{"tipo": "reporte", "hechas": i, "total": N,
      "ok": bool, "email": str, "identidad": {...}, "detalle": str}``.
    - `cancelar` (threading.Event): deja de enviar nuevos reportes
      (``shutdown(cancel_futures=True)``) y marca ``cancelada=True``.
    - Resumen: ``{"total","enviados","fallidos","cancelada",
      "identidades_guardadas","resultados","proxies_total","sin_proxy","error"}``.
      NUNCA lanza.
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
        }

        def _siguiente_proxy() -> str:
            with lock:
                if not proxies:
                    contadores["sin_proxy"] += 1
                    return ""
                proxy = proxies[contadores["proxy"] % len(proxies)]
                contadores["proxy"] += 1
                return proxy

        def _trabajo() -> dict:
            proxy = _siguiente_proxy()
            with lock:
                evitar_actual = list(evitar)
            try:
                resultado = ejecutar_un_reporte(
                    url_peticion=url_peticion,
                    contexto=contexto,
                    proxy=proxy,
                    headless=headless,
                    evitar=evitar_actual,
                    cancelar=cancelar,
                    guardar_identidad=guardar_identidades,
                )
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
            _notificar(
                callback,
                {
                    "tipo": "reporte",
                    "hechas": hechas,
                    "total": total,
                    "ok": ok,
                    "email": str(resultado.get("email") or ""),
                    "identidad": dict(resultado.get("identidad") or {}),
                    "detalle": (
                        "éxito"
                        if ok
                        else str(resultado.get("error") or "fallo sin detalle")
                    ),
                },
            )

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
                    futuros[executor.submit(_trabajo)] = siguiente
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
