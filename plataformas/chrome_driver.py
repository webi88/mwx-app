"""Creacion compartida de drivers Chrome/undetected-chromedriver.

Problema que resuelve (Railway/Docker con ``MAX_BROWSERS`` > 1):
``uc.Chrome(...)`` sin ``driver_executable_path`` crea un ``Patcher`` con la
ruta por defecto (``~/.local/share/undetected_chromedriver/undetected/...``) y
``Patcher.auto()`` desvincula/descarga/parchea ese MISMO archivo en cada
arranque. Con varios hilos/procesos (dashboard + scheduler + campanas con
varios navegadores) compiten por el archivo: ``Text file busy``,
``FileNotFoundError`` a media sesion y ``Connection refused`` cuando el
chromedriver muere.

Solucion: un unico chromedriver pre-parcheado en ``data/bin/`` que se prepara
UNA vez bajo lock de archivo entre procesos y luego se reutiliza en modo
lectura en todos los drivers. Con un ``driver_executable_path`` custom ya
parcheado, ``Patcher.auto()`` solo lee el binario y no lo toca (seguro para
uso concurrente).

Ademas aplica flags de ahorro de datos para que Chrome no gaste proxy
residencial en trafico de fondo (``update.googleapis.com``,
``clients2.google.com``, ``accounts.google.com``, ...).
"""
from __future__ import annotations

import errno
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

import undetected_chromedriver as uc
from loguru import logger
from selenium.common.exceptions import WebDriverException

from core.config import detectar_chrome_version, resolver_ruta

try:  # selenium >= 4.10
    from selenium.common.exceptions import NoSuchDriverException
except Exception:  # pragma: no cover - selenium viejo
    NoSuchDriverException = None

try:  # urllib3 siempre viene con selenium, pero no rompemos el import si falta
    from urllib3.exceptions import MaxRetryError
except Exception:  # pragma: no cover
    MaxRetryError = None


__all__ = ["crear_chrome", "FLAGS_AHORRO"]


# ---------------------------------------------------------------------------
# Flags de ahorro de datos / anti trafico de fondo
# ---------------------------------------------------------------------------

FLAGS_AHORRO = [
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-breakpad",
    "--disable-crash-reporter",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-client-side-phishing-detection",
    "--metrics-recording-only",
    "--disable-features=Translate,TranslateUI,OptimizationHints,OptimizationGuideModelDownloading,MediaRouter,AutofillServerCommunication,CalculateNativeWinOcclusion",
]

# Dominios que Chrome visita en segundo plano y que no hacen falta para operar
# en X/Facebook/Instagram/TikTok con login por cookies.
_DOMINIOS_GOOGLE = (
    "*.google.com",
    "*.googleapis.com",
    "*.gstatic.com",
    "*.doubleclick.net",
)

_MARCA_PARCHEADO = b"undetected chromedriver"

# Serializa el bootstrap dentro del MISMO proceso (ademas del lock de archivo
# entre procesos); algunos filesystems no soportan flock y los locks de
# Windows son por handle.
_LOCK_PROCESO = threading.Lock()


def _env_activo(nombre: str, por_defecto: bool) -> bool:
    """Lee un booleano de entorno: 0/false/no/off desactivan."""
    valor = os.environ.get(nombre)
    if valor is None or not valor.strip():
        return por_defecto
    return valor.strip().lower() not in ("0", "false", "no", "off")


def _flags_extra() -> list:
    """Flags opcionales segun entorno (Google bloqueado y sin imagenes)."""
    flags = []
    if _env_activo("CHROME_BLOQUEAR_GOOGLE", True):
        reglas = ", ".join(f"MAP {dominio} ~NOTFOUND" for dominio in _DOMINIOS_GOOGLE)
        flags.append(f"--host-resolver-rules={reglas}")
    if _env_activo("CHROME_SIN_IMAGENES", False):
        flags.append("--blink-settings=imagesEnabled=false")
    return flags


def _aplicar_flags_ahorro(options):
    """Aplica ``FLAGS_AHORRO`` (+ flags de datos) de forma IDEMPOTENTE.

    Se puede llamar varias veces sobre las mismas ``options``: los flags ya
    presentes no se duplican.
    """
    try:
        existentes = list(getattr(options, "arguments", []) or [])
    except Exception:
        existentes = []
    for flag in FLAGS_AHORRO + _flags_extra():
        if flag not in existentes:
            options.add_argument(flag)
            existentes.append(flag)
    return options


# ---------------------------------------------------------------------------
# Driver compartido pre-parcheado
# ---------------------------------------------------------------------------

def _ruta_driver_estable(version) -> str:
    """Ruta estable del chromedriver compartido dentro de ``data/bin``.

    En Windows se agrega ``.exe`` porque uc.Patcher se lo agrega a cualquier
    ruta custom que no lo traiga; mantener el mismo nombre evita desajustes.
    """
    sufijo = f"_{version}" if version else ""
    nombre = f"undetected_chromedriver{sufijo}"
    if os.name == "nt":
        nombre += ".exe"
    return resolver_ruta(f"data/bin/{nombre}")


def _esta_parcheado(ruta) -> bool:
    """True si el binario ya trae el parche de undetected-chromedriver.

    Usa la MISMA comprobacion de uc (``Patcher.is_binary_patched``); se llama
    de forma unbound con ``self=None`` porque al pasarle la ruta explicita la
    funcion no usa ``self`` para nada.
    """
    if not ruta or not os.path.isfile(ruta):
        return False
    try:
        return bool(uc.Patcher.is_binary_patched(None, ruta))
    except Exception:
        pass
    # Fallback: misma marca que usa uc internamente.
    try:
        with open(ruta, "rb") as fh:
            return fh.read().find(_MARCA_PARCHEADO) != -1
    except OSError:
        return False


def _timeout_lock() -> float:
    try:
        return max(10.0, float(os.environ.get("CHROME_DRIVER_LOCK_TIMEOUT", "180")))
    except Exception:
        return 180.0


def _adquirir_bloqueo(fh, timeout: float) -> None:
    """Adquiere el lock exclusivo del archivo (POSIX flock / Windows msvcrt).

    Si el filesystem no soporta el lock, avisa y continua (mejor esfuerzo):
    el ``_LOCK_PROCESO`` del proceso sigue serializando los hilos.
    """
    inicio = time.time()
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                if os.fstat(fh.fileno()).st_size == 0:
                    fh.write(b"\0")
                    fh.flush()
                    fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as e:
            no_soportado = {
                getattr(errno, "ENOTSUP", None),
                getattr(errno, "EOPNOTSUPP", None),
                getattr(errno, "ENOSYS", None),
            }
            if getattr(e, "errno", None) in no_soportado:
                logger.warning(
                    f"[chrome_driver] El filesystem no soporta lock de archivo "
                    f"({e}); continuo sin lock entre procesos"
                )
                return
            if time.time() - inicio >= timeout:
                raise TimeoutError(
                    "[chrome_driver] timeout esperando el lock del chromedriver "
                    "compartido; otro proceso no lo libero"
                )
            time.sleep(0.25)


def _liberar_bloqueo(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


@contextmanager
def _lock_archivo(ruta_lock: str, timeout: float = None):
    """Lock exclusivo entre procesos + hilos para el bootstrap del driver."""
    if timeout is None:
        timeout = _timeout_lock()
    carpeta = os.path.dirname(ruta_lock)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with _LOCK_PROCESO:
        fh = open(ruta_lock, "a+b")
        try:
            _adquirir_bloqueo(fh, timeout)
            yield
        finally:
            _liberar_bloqueo(fh)
            try:
                fh.close()
            except Exception:
                pass


def _bootstrap_driver(ruta_destino: str, version) -> str:
    """Descarga/parchea chromedriver UNA vez y lo copia a la ruta estable.

    Debe llamarse SIEMPRE con el lock de ``_lock_archivo`` tomado.
    """
    logger.info(
        f"[chrome_driver] Preparando chromedriver compartido por primera vez "
        f"(version_main={version})..."
    )
    patcher = uc.Patcher(version_main=version or 0)
    patcher.auto()
    origen = patcher.executable_path
    if not _esta_parcheado(origen):
        patcher.patch_exe()
    if not _esta_parcheado(origen):
        raise RuntimeError(
            f"[chrome_driver] undetected-chromedriver no pudo parchear el "
            f"binario: {origen}"
        )

    os.makedirs(os.path.dirname(ruta_destino), exist_ok=True)
    tmp = f"{ruta_destino}.tmp{os.getpid()}"
    shutil.copy2(origen, tmp)
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o755)
        except OSError:
            pass
    os.replace(tmp, ruta_destino)

    if not _esta_parcheado(ruta_destino):
        raise RuntimeError(
            f"[chrome_driver] El driver copiado no quedo parcheado: {ruta_destino}"
        )
    logger.info(f"[chrome_driver] chromedriver parcheado guardado en {ruta_destino}")
    return ruta_destino


_CACHE_EJECUTABLE: dict = {}


def _ruta_ejecutable(ruta: str) -> str:
    """Verifica que el driver pueda EJECUTARSE desde su ruta.

    El volumen persistente de Railway podria montarse como ``noexec``; en ese
    caso el binario de ``data/bin`` existe pero el sistema no lo deja correr
    (Selenium fallaria con "Permission denied"). Si el sondeo falla, se copia a
    un directorio temporal (normalmente ejecutable) y se usa esa copia.
    La verificacion se cachea por ruta para no lanzar subprocesos de mas.
    """
    if not ruta:
        return ruta
    cacheada = _CACHE_EJECUTABLE.get(ruta)
    if cacheada:
        return cacheada

    try:
        probe = subprocess.run(
            [ruta, "--version"], capture_output=True, timeout=15
        )
        if probe.returncode == 0:
            _CACHE_EJECUTABLE[ruta] = ruta
            return ruta
    except OSError as e:
        logger.warning(
            f"[chrome_driver] El driver de {ruta} no se puede ejecutar "
            f"({type(e).__name__}: {e}); usando copia temporal"
        )

    destino = os.path.join(
        tempfile.gettempdir(), "gestor_chromedriver", os.path.basename(ruta)
    )
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    try:
        shutil.copy2(ruta, destino)
        if os.name != "nt":
            os.chmod(destino, 0o755)
    except OSError as e:
        logger.error(
            f"[chrome_driver] No se pudo copiar el driver a {destino}: {e}; "
            f"se intentara usar el original"
        )
        return ruta

    # Comprueba la copia; si tampoco corre, devuelve la original.
    try:
        probe = subprocess.run(
            [destino, "--version"], capture_output=True, timeout=15
        )
        if probe.returncode == 0:
            logger.info(f"[chrome_driver] Driver ejecutable copiado a {destino}")
            _CACHE_EJECUTABLE[ruta] = destino
            return destino
    except OSError:
        pass
    return ruta


def _driver_compartido(version) -> str:
    """Devuelve la ruta del chromedriver compartido, preparandolo si falta."""
    ruta = _ruta_driver_estable(version)
    if _esta_parcheado(ruta):
        logger.debug(f"[chrome_driver] Reutilizando chromedriver compartido: {ruta}")
        return _ruta_ejecutable(ruta)

    with _lock_archivo(resolver_ruta("data/bin/.chromedriver.lock")):
        # Doble verificacion: otro proceso pudo prepararlo mientras esperabamos.
        if _esta_parcheado(ruta):
            logger.info(
                f"[chrome_driver] Otro proceso preparo el chromedriver; "
                f"reutilizando {ruta}"
            )
            return _ruta_ejecutable(ruta)
        return _ruta_ejecutable(_bootstrap_driver(ruta, version))


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------

def _excepciones_transitorias() -> tuple:
    """Errores tipicos cuando varios drivers compiten o Chrome tarda en arrancar.

    ``ConnectionError``/``TimeoutError``/``FileNotFoundError`` heredan de
    OSError, y ``NoSuchDriverException`` de WebDriverException: se listan
    explicitamente para documentar la intencion.
    """
    bases = [OSError, WebDriverException]
    if NoSuchDriverException is not None:
        bases.append(NoSuchDriverException)
    if MaxRetryError is not None:
        bases.append(MaxRetryError)
    return tuple(bases)


_EXCEPCIONES_TRANSITORIAS = _excepciones_transitorias()


def _resetear_options(options, argumentos_originales) -> None:
    """Deja las options listas para un nuevo intento de ``uc.Chrome``.

    uc marca ``options._session`` al construir y rechaza reutilizarlas; ademas
    le agrega argumentos propios (puerto de debug, etc.), asi que se restauran
    los originales para no acumular duplicados entre reintentos.
    """
    try:
        options._session = None
    except Exception:
        pass
    try:
        if argumentos_originales is not None:
            actuales = list(getattr(options, "arguments", []) or [])
            if actuales != list(argumentos_originales):
                try:
                    options.arguments[:] = list(argumentos_originales)
                except Exception:
                    options._arguments = list(argumentos_originales)
    except Exception:
        pass


def crear_chrome(options, version_main=None, intentos=2):
    """Devuelve un driver uc.Chrome listo (driver compartido pre-parcheado, sin carreras).

    - Aplica los flags de ahorro de datos (idempotente) antes de crear el driver.
    - Usa SIEMPRE el chromedriver pre-parcheado de ``data/bin/`` (se prepara una
      sola vez bajo lock de archivo entre procesos).
    - ``intentos`` (default 2): reintenta ante fallos transitorios tipicos de la
      contencion (Text file busy / NoSuchDriver / Connection refused / timeouts).
      Al agotarse, relanza la ultima excepcion para que el bot la reporte.
    """
    version = version_main
    if version is None:
        try:
            version = detectar_chrome_version()
        except Exception:
            version = None

    _aplicar_flags_ahorro(options)
    ruta_estable = _driver_compartido(version)

    try:
        argumentos_originales = list(getattr(options, "arguments", []) or [])
    except Exception:
        argumentos_originales = None

    total = max(1, int(intentos or 1))
    ultimo_error = None
    for intento in range(1, total + 1):
        _resetear_options(options, argumentos_originales)
        driver = None
        try:
            driver = uc.Chrome(
                options=options,
                version_main=version,
                use_subprocess=False,
                driver_executable_path=ruta_estable,
            )
            return driver
        except _EXCEPCIONES_TRANSITORIAS as e:
            ultimo_error = e
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            if intento < total:
                espera = 1.5 + random.random() * 0.5
                logger.warning(
                    f"[chrome_driver] Fallo transitorio creando Chrome "
                    f"({intento}/{total}): {type(e).__name__}: {e}. "
                    f"Reintentando en {espera:.1f}s"
                )
                time.sleep(espera)
        # Las excepciones NO transitorias se propagan tal cual al bot.

    if ultimo_error is None:  # no deberia ocurrir
        raise RuntimeError("[chrome_driver] no se pudo crear el driver de Chrome")
    raise ultimo_error
