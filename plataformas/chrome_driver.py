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

Empaquetado (MWX APP / PyInstaller en Windows): la carpeta del ``.exe`` suele
ser de SOLO LECTURA, asi que el driver ya no se asume en ``data/bin``. La
carpeta se elige con ``_dir_drivers()`` (env ``MWX_DRIVER_DIR`` ->
``settings.data_dir/data/bin`` -> ``resolver_ruta("data/bin")`` -> respaldo de
usuario), se prueba escritura y, en congelado, se reutiliza primero un
chromedriver YA PARCHEADO empaquetado en ``sys._MEIPASS/bin``. Si nada se
puede escribir, el bootstrap cae a ``%LOCALAPPDATA%/MWX APP/bin``. El modo
normal (sin congelar, Chrome instalado en la maquina) conserva exactamente los
mismos nombres de archivo, locks y concurrencia entre procesos.
"""
from __future__ import annotations

import errno
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import undetected_chromedriver as uc
from loguru import logger
from selenium.common.exceptions import WebDriverException

from core.config import detectar_chrome_version, resolver_ruta, settings

try:  # selenium >= 4.10
    from selenium.common.exceptions import NoSuchDriverException
except Exception:  # pragma: no cover - selenium viejo
    NoSuchDriverException = None

try:  # urllib3 siempre viene con selenium, pero no rompemos el import si falta
    from urllib3.exceptions import MaxRetryError
except Exception:  # pragma: no cover
    MaxRetryError = None


__all__ = ["crear_chrome", "FLAGS_AHORRO", "FLAGS_ESTABILIDAD", "FLAGS_CONTENEDOR"]


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
    # Evita el autoplay de videos en los tweets ancla: ahorra CPU/RAM y
    # acelera las cargas (los videos de X son el mayor consumidor de red/CPU
    # en las paginas de status de las campanas).
    "--autoplay-policy=user-gesture-required",
    # Cache en disco moderada: acelera las recargas de la SPA de X/FB/IG sin
    # llenar el disco (256MB de cache normal + 32MB de media).
    "--disk-cache-size=268435456",
    "--media-cache-size=33554432",
    # `PreloadMediaEngagementData`/`MediaEngagementBypassAutoplayPolicies`
    # evitan precargas de media en segundo plano (datos del proxy).
    "--disable-features=Translate,TranslateUI,OptimizationHints,OptimizationGuideModelDownloading,MediaRouter,AutofillServerCommunication,CalculateNativeWinOcclusion,PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
]

# Flags de ESTABILIDAD del renderer: en Railway (CPU compartida, 3 Chrome a la
# vez, headless) Chrome congela pestanas ocultas y limita los timers, lo que
# provoca `Timed out receiving message from renderer` en medio de la
# publicacion. Estos flags evitan ese throttling y los cuelgues del renderer.
FLAGS_ESTABILIDAD = [
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
]

# Flags de CONTENEDOR: cada renderer de Chrome es un proceso pesado (fork/exec)
# y con 6 navegadores concurrentes el contenedor agotaba PIDs/hilos
# (`BlockingIOError: [Errno 11] Resource temporarily unavailable`). Limitar a 2
# renderers, capar el heap V8 y desactivar site-per-process (1 proceso por
# sitio) reduce los forks sin afectar a X/Facebook/Instagram/TikTok. El
# `--disable-features` extra se fusiona con el de FLAGS_AHORRO en UNA sola
# bandera (ver `_fusionar_disable_features`).
#
# `--disable-dev-shm-usage`: Railway/Docker montan /dev/shm pequeno (64MB por
# defecto en Docker) y Chrome lo revienta con `tab crashed`; este flag hace que
# use /tmp en su lugar (motivo principal de los crashes de renderer).
# `--disable-extensions` / `--disable-component-extensions-with-background-pages`
# evitan procesos de extensiones que no se usan en automatizacion, y
# `--mute-audio` quita trabajo de audio innecesario en el contenedor.
FLAGS_CONTENEDOR = [
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--mute-audio",
    "--renderer-process-limit=2",
    "--js-flags=--max-old-space-size=256",
]

# Features que se agregan a la unica bandera --disable-features (site-per-process
# y IsolateOrigins crean un proceso por sitio en X: con 6 navegadores el
# contenedor agotaba PIDs).
_FEATURES_CONTENEDOR = ("site-per-process", "IsolateOrigins")

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


def _env_int(nombre: str, por_defecto: int, minimo: int = 1) -> int:
    """Lee un entero de entorno con valor minimo; nunca lanza."""
    try:
        valor = int(str(os.environ.get(nombre, "")).strip())
        return max(minimo, valor)
    except Exception:
        return por_defecto


def _flags_extra() -> list:
    """Flags opcionales segun entorno (Google bloqueado y sin imagenes)."""
    flags = []
    if _env_activo("CHROME_BLOQUEAR_GOOGLE", True):
        reglas = ", ".join(f"MAP {dominio} ~NOTFOUND" for dominio in _DOMINIOS_GOOGLE)
        flags.append(f"--host-resolver-rules={reglas}")
    if _env_activo("CHROME_SIN_IMAGENES", False):
        flags.append("--blink-settings=imagesEnabled=false")
    return flags


def _flags_contenedor() -> list:
    """Flags de contenedor + `--no-zygote` opcional (default activo)."""
    flags = list(FLAGS_CONTENEDOR)
    if _env_activo("CHROME_NO_ZYGOTE", True):
        flags.append("--no-zygote")
    return flags


def _aplicar_flags(options, flags):
    """Aplica `flags` de forma IDEMPOTENTE sobre `options`.

    Se puede llamar varias veces sobre las mismas ``options``: los flags ya
    presentes no se duplican.
    """
    try:
        existentes = list(getattr(options, "arguments", []) or [])
    except Exception:
        existentes = []
    for flag in flags:
        if flag not in existentes:
            options.add_argument(flag)
            existentes.append(flag)
    return options


def _aplicar_flags_ahorro(options):
    """Aplica ``FLAGS_AHORRO`` (+ flags de datos) de forma IDEMPOTENTE."""
    return _aplicar_flags(options, FLAGS_AHORRO + _flags_extra())


def _aplicar_flags_estabilidad(options):
    """Aplica ``FLAGS_ESTABILIDAD`` (renderer) de forma IDEMPOTENTE."""
    return _aplicar_flags(options, FLAGS_ESTABILIDAD)


def _fusionar_disable_features(options, extras=_FEATURES_CONTENEDOR):
    """Deja UNA SOLA bandera ``--disable-features`` con la union de features.

    ``FLAGS_AHORRO`` ya trae un ``--disable-features=...``; si se agregara otro
    argumento con el mismo prefijo, Chrome usaria el ULTIMO y se perderian las
    features del primero. Aqui se recolectan TODAS, se eliminan y se agrega una
    unica bandera fusionada. Idempotente: llamarla de nuevo no duplica nada.
    """
    extras = tuple(extras or ())
    try:
        argumentos = list(getattr(options, "arguments", []) or [])
    except Exception:
        argumentos = []
    features = []
    restantes = []
    for arg in argumentos:
        texto = str(arg)
        if texto.startswith("--disable-features="):
            for feature in texto.split("=", 1)[1].split(","):
                feature = feature.strip()
                if feature and feature not in features:
                    features.append(feature)
            continue
        restantes.append(arg)
    for feature in extras:
        if feature and feature not in features:
            features.append(feature)
    if features:
        fusionada = "--disable-features=" + ",".join(features)
        try:
            options.arguments[:] = restantes + [fusionada]
        except Exception:
            try:
                options.add_argument(fusionada)
            except Exception:
                pass
    return features


def _aplicar_flags_contenedor(options):
    """Aplica ``FLAGS_CONTENEDOR`` y fusiona ``--disable-features`` en UNA."""
    _aplicar_flags(options, _flags_contenedor())
    return _fusionar_disable_features(options, _FEATURES_CONTENEDOR)


def _es_fallo_recursos(e) -> bool:
    """True si el fallo apunta a agotamiento de hilos/procesos/PIDs.

    En Railway con varios Chrome a la vez el fork/exec del chromedriver falla
    con ``BlockingIOError``/``Errno 11`` o al crear hilos; esos casos merecen
    un backoff mas largo antes de reintentar.
    """
    try:
        texto = f"{type(e).__name__}: {e}".lower()
    except Exception:
        return False
    senales = (
        "blockingioerror",
        "resource temporarily unavailable",
        "can't start new thread",
        "can not start new thread",
        "errno 11",
        "cannot allocate memory",
        "cannot fork",
        "too many open files",
    )
    return any(senal in texto for senal in senales)


# ---------------------------------------------------------------------------
# Throttle de lanzamiento (fork/exec sin tormenta)
# ---------------------------------------------------------------------------

# Limita cuantos Chrome se lanzan a la vez en este proceso: con 6 workers
# concurrentes el fork/exec simultaneo del chromedriver agotaba PIDs
# (`BlockingIOError: [Errno 11] Resource temporarily unavailable`) y tumbaba la
# campana. Configurable con CHROME_LAUNCH_MAX (default 2).
_LANZAMIENTOS_MAX = _env_int("CHROME_LAUNCH_MAX", 2)
_SEMAFORO_LANZAMIENTO = threading.Semaphore(_LANZAMIENTOS_MAX)


# ---------------------------------------------------------------------------
# Driver compartido pre-parcheado
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Carpeta del driver (frozen-safe para MWX APP / PyInstaller)
# ---------------------------------------------------------------------------

# La decision se toma UNA vez por proceso (probar permisos cuesta y el entorno
# no cambia durante la sesion). Los tests la reinician con
# `_limpiar_cache_dir_drivers()`.
_CACHE_DIR_DRIVERS = ""
_CACHE_MOTIVO_DIR_DRIVERS = ""
_LOCK_DIR_DRIVERS = threading.Lock()


def _es_congelado() -> bool:
    """True si corremos dentro de un ejecutable empaquetado (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def _entorno_path(nombre: str) -> str:
    """Lee una ruta de entorno expandiendo ``~``/``%VAR%``; "" si no existe."""
    valor = (os.environ.get(nombre) or "").strip()
    if not valor:
        return ""
    try:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(valor)))
    except Exception:
        return valor


def _candidatos_fallback() -> list:
    """Carpetas ESCRIBIBLES de respaldo, en orden de preferencia.

    Windows: ``%LOCALAPPDATA%\\MWX APP\\bin`` -> ``%APPDATA%\\MWX APP\\bin``.
    Otros: ``~/.mwx_app/bin``. Siempre se agrega una carpeta bajo TEMP como
    ultimo recurso (el ejecutable puede correr en un usuario sin perfil).
    """
    candidatos = []
    if os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
            if base:
                candidatos.append(os.path.join(base, "MWX APP", "bin"))
        try:
            candidatos.append(
                os.path.join(
                    os.path.expanduser("~"), "AppData", "Local", "MWX APP", "bin"
                )
            )
        except Exception:
            pass
    else:
        try:
            candidatos.append(
                os.path.join(os.path.expanduser("~"), ".mwx_app", "bin")
            )
        except Exception:
            pass
    candidatos.append(os.path.join(tempfile.gettempdir(), "MWX APP", "bin"))

    # Dedupe case-insensitive (Windows) conservando el orden.
    unicos = []
    vistos = set()
    for carpeta in candidatos:
        if not carpeta:
            continue
        clave = os.path.normcase(os.path.abspath(carpeta))
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(carpeta)
    return unicos


def _dir_drivers_fallback() -> str:
    """Carpeta de respaldo principal (``%LOCALAPPDATA%/MWX APP/bin`` o similar)."""
    candidatos = _candidatos_fallback()
    return candidatos[0] if candidatos else os.path.join(
        tempfile.gettempdir(), "MWX APP", "bin"
    )


def _puede_escribir(carpeta: str) -> bool:
    """Crea la carpeta y comprueba con un archivo sonda que se puede escribir.

    No lanza: devuelve False ante cualquier error de permisos/disco. El archivo
    sonda se borra de inmediato y NO se toca ningun driver existente.
    """
    if not carpeta:
        return False
    try:
        Path(carpeta).mkdir(parents=True, exist_ok=True)
        sonda = Path(carpeta) / (
            f".mwx_write_{os.getpid()}_{int(time.time() * 1000)}"
        )
        sonda.write_bytes(b"ok")
        sonda.unlink()
        return True
    except OSError as e:
        logger.debug(
            f"[chrome_driver] Sin permiso de escritura en {carpeta}: "
            f"{type(e).__name__}: {e}"
        )
        return False
    except Exception as e:  # pragma: no cover - defensivo
        logger.debug(
            f"[chrome_driver] No se pudo probar escritura en {carpeta}: "
            f"{type(e).__name__}: {e}"
        )
        return False


def _carpeta_datos_configurada() -> str:
    """``data/bin`` segun ``core.config.settings`` o ``resolver_ruta``.

    En ``core.config`` actual ``settings.data_dir`` es la RAIZ DE ESCRITURA
    (en desarrollo la raiz del proyecto; en el .exe ``%LOCALAPPDATA%/MWX APP``)
    que CONTIENE la carpeta ``data``; por eso el driver va a
    ``<data_dir>/data/bin``. ``resolver_ruta("data/bin")`` produce esa misma
    ruta en ambos modos y queda como ultimo recurso si otra rama aun no expone
    los atributos nuevos.
    """
    # `data_dir` es la raiz de escritura que CONTIENE `data/` en core.config,
    # pero si alguna variante lo define ya como la carpeta `data`, no se
    # duplica (`data/data/bin`).
    data_dir = getattr(settings, "data_dir", "") or ""
    if data_dir:
        try:
            ruta = Path(str(data_dir))
            if ruta.name.lower() == "data":
                return str(ruta / "bin")
            return str(ruta / "data" / "bin")
        except Exception:
            pass
    for atributo in ("app_dir", "base_dir"):
        valor = getattr(settings, atributo, "") or ""
        if not valor:
            continue
        try:
            return os.path.join(str(valor), "data", "bin")
        except Exception:
            continue
    try:
        return resolver_ruta("data/bin")
    except Exception:
        return ""


def _carpeta_fallback_escribible() -> str:
    """Primera carpeta de respaldo donde de verdad se puede escribir."""
    for carpeta in _candidatos_fallback():
        if _puede_escribir(carpeta):
            return carpeta
    return _dir_drivers_fallback()


def _dir_drivers() -> str:
    """Devuelve la carpeta donde vive el chromedriver compartido (cacheada).

    Prioridad:
      1. ``MWX_DRIVER_DIR`` (override explicito, util en el .exe/MWX APP).
      2. ``settings.data_dir/data/bin`` (o ``app_dir``/``base_dir``/data/bin).
      3. ``resolver_ruta("data/bin")`` (comportamiento historico).
      4. ``%LOCALAPPDATA%/MWX APP/bin`` (Windows) o ``~/.mwx_app/bin``.

    Se elige la PRIMERA donde se pueda escribir; en un .exe instalado en una
    carpeta de solo lectura cae sola al respaldo de usuario. Nunca asume que
    ``data/`` esta en la carpeta del ejecutable.
    """
    global _CACHE_DIR_DRIVERS, _CACHE_MOTIVO_DIR_DRIVERS
    if _CACHE_DIR_DRIVERS:
        return _CACHE_DIR_DRIVERS

    with _LOCK_DIR_DRIVERS:
        if _CACHE_DIR_DRIVERS:  # otro hilo la eligio mientras esperabamos
            return _CACHE_DIR_DRIVERS

        candidatos = []
        env_dir = _entorno_path("MWX_DRIVER_DIR")
        if env_dir:
            candidatos.append((env_dir, "env MWX_DRIVER_DIR"))
        configurada = _carpeta_datos_configurada()
        if configurada:
            candidatos.append(
                (configurada, "settings/resolver_ruta (data/bin)")
            )
        for carpeta in _candidatos_fallback():
            candidatos.append((carpeta, "respaldo de usuario (LOCALAPPDATA/~)"))

        elegida = ""
        motivo = ""
        for carpeta, razon in candidatos:
            if _puede_escribir(carpeta):
                elegida, motivo = carpeta, razon
                break

        if not elegida:
            # Ni el override ni el perfil del usuario son escribibles: TEMP
            # suele serlo aunque el resto del sistema este restringido. Si
            # tampoco, se deja la ruta de respaldo y el error de permisos se
            # reporta al preparar el driver (con su ruta exacta).
            elegida = _dir_drivers_fallback()
            motivo = "respaldo de usuario (sin permiso verificado)"
            logger.error(
                f"[chrome_driver] No se pudo escribir en ninguna carpeta de "
                f"drivers; se intentara {elegida}"
            )

        _CACHE_DIR_DRIVERS = elegida
        _CACHE_MOTIVO_DIR_DRIVERS = motivo
        logger.info(
            f"[chrome_driver] Carpeta de drivers: {elegida} ({motivo})"
            f"{' [frozen]' if _es_congelado() else ''}"
        )
        return elegida


def _limpiar_cache_dir_drivers() -> None:
    """Olvida la carpeta elegida (tests o cambio de entorno en caliente)."""
    global _CACHE_DIR_DRIVERS, _CACHE_MOTIVO_DIR_DRIVERS
    with _LOCK_DIR_DRIVERS:
        _CACHE_DIR_DRIVERS = ""
        _CACHE_MOTIVO_DIR_DRIVERS = ""


def _es_error_permiso(e) -> bool:
    """True si el error es de permisos/solo-lectura (candidato a fallback)."""
    if isinstance(e, PermissionError):
        return True
    codigos = {
        errno.EACCES,
        errno.EPERM,
        getattr(errno, "EROFS", None),
        getattr(errno, "ENOTDIR", None),
    }
    if getattr(e, "errno", None) in codigos:
        return True
    if os.name == "nt" and getattr(e, "winerror", None) in (5, 19, 82):
        # 5=ACCESS_DENIED, 19=WRITE_PROTECT, 82=CANNOT_MAKE
        return True
    texto = str(e).lower()
    return (
        "permission denied" in texto
        or "access is denied" in texto
        or "read-only file system" in texto
        or "acceso denegado" in texto
    )


def _driver_empaquetado(version) -> str:
    """Busca un chromedriver YA PARCHEADO dentro del bundle de PyInstaller.

    Solo aplica en congelado (``sys.frozen``). Nombres esperados dentro de
    ``sys._MEIPASS[\\bin]``: ``undetected_chromedriver_<version>.exe`` (exacto)
    y, como comodin, ``undetected_chromedriver.exe`` / ``chromedriver.exe``.
    Devuelve "" si no hay ninguno parcheado.
    """
    if not _es_congelado():
        return ""
    base = getattr(sys, "_MEIPASS", "") or ""
    if not base or not os.path.isdir(base):
        return ""

    ext = ".exe" if os.name == "nt" else ""
    sufijo = f"_{version}" if version else ""
    nombres = []
    if sufijo:
        nombres.append(f"undetected_chromedriver{sufijo}{ext}")
    nombres.append(f"undetected_chromedriver{ext}")
    nombres.append(f"chromedriver{ext}")

    for subcarpeta in ("bin", os.path.join("data", "bin"), ""):
        for nombre in nombres:
            ruta = (
                os.path.join(base, subcarpeta, nombre)
                if subcarpeta
                else os.path.join(base, nombre)
            )
            if os.path.isfile(ruta) and _esta_parcheado(ruta):
                logger.debug(
                    f"[chrome_driver] Driver empaquetado encontrado: {ruta}"
                )
                return ruta
    return ""


def _ruta_driver_estable(version) -> str:
    """Ruta estable del chromedriver compartido dentro de ``_dir_drivers()``.

    En Windows se agrega ``.exe`` porque uc.Patcher se lo agrega a cualquier
    ruta custom que no lo traiga; mantener el mismo nombre evita desajustes.
    """
    sufijo = f"_{version}" if version else ""
    nombre = f"undetected_chromedriver{sufijo}"
    if os.name == "nt":
        nombre += ".exe"
    return os.path.join(_dir_drivers(), nombre)


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


def _lock_driver(carpeta: str):
    """Lock de archivo del driver, SIEMPRE dentro de la carpeta del driver."""
    return _lock_archivo(os.path.join(carpeta, ".chromedriver.lock"))


def _copiar_driver_atomico(origen: str, destino: str) -> str:
    """Copia el driver con temporal + ``os.replace`` (sin dejar binarios a medias)."""
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    tmp = f"{destino}.tmp{os.getpid()}"
    shutil.copy2(origen, tmp)
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o755)
        except OSError:
            pass
    try:
        os.replace(tmp, destino)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return destino


def _preparar_driver_empaquetado(origen: str, destino: str) -> str:
    """Copia el driver empaquetado a la carpeta elegida o lo ejecuta in-place.

    Si la carpeta destino es de solo lectura (o el binario esta en uso) NO se
    falla: se ejecuta directo desde ``sys._MEIPASS`` (``_ruta_ejecutable`` lo
    copia a TEMP si esa ubicacion tampoco permite ejecutar).
    """
    try:
        _copiar_driver_atomico(origen, destino)
        if _esta_parcheado(destino):
            logger.info(
                f"[chrome_driver] chromedriver empaquetado copiado a {destino}"
            )
            return destino
        logger.warning(
            f"[chrome_driver] El driver copiado desde {origen} no quedo "
            f"parcheado; se usara el binario empaquetado"
        )
    except OSError as e:
        logger.warning(
            f"[chrome_driver] No se pudo copiar el driver empaquetado a "
            f"{destino} ({type(e).__name__}: {e}); se ejecutara desde {origen}"
        )
    return _ruta_ejecutable(origen)


def _preparar_en_carpeta(
    ruta_destino: str, version, origen_empaquetado: str = ""
) -> str:
    """Prepara el driver en ``ruta_destino`` bajo lock entre procesos.

    Debe llamarse con una carpeta ESCRIBIBLE (``_dir_drivers`` ya la probo);
    si aun asi falla por permisos, ``_driver_compartido`` reintenta en el
    respaldo de usuario. Reutiliza sin escribir si otro proceso ya lo dejo.
    """
    carpeta = os.path.dirname(ruta_destino)
    with _lock_driver(carpeta):
        if _esta_parcheado(ruta_destino):
            logger.info(
                f"[chrome_driver] Otro proceso preparo el chromedriver; "
                f"reutilizando {ruta_destino}"
            )
            return ruta_destino
        if origen_empaquetado:
            return _preparar_driver_empaquetado(origen_empaquetado, ruta_destino)
        return _bootstrap_driver(ruta_destino, version)


def _bootstrap_driver(ruta_destino: str, version) -> str:
    """Descarga/parchea chromedriver UNA vez y lo copia a la ruta estable.

    Debe llamarse SIEMPRE con el lock de ``_lock_archivo`` tomado.
    """
    logger.info(
        f"[chrome_driver] Preparando chromedriver compartido por primera vez "
        f"(version_main={version}) en {ruta_destino}..."
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
    """Devuelve la ruta del chromedriver compartido, preparandolo si falta.

    Orden:
      1. Reutilizar el driver ya presente en ``_dir_drivers()`` (sin escribir).
      2. En congelado, usar/copiar el driver pre-parcheado de ``sys._MEIPASS``.
      3. Bootstrapear (descargar/parchear) bajo lock en ``_dir_drivers()``.
      4. Si el punto 3 falla por permisos, bootstrapear en el respaldo de
         usuario (``%LOCALAPPDATA%/MWX APP/bin`` o ``~/.mwx_app/bin``).
    """
    ruta = _ruta_driver_estable(version)
    if _esta_parcheado(ruta):
        logger.debug(f"[chrome_driver] Reutilizando chromedriver compartido: {ruta}")
        return _ruta_ejecutable(ruta)

    empaquetado = _driver_empaquetado(version)
    if empaquetado:
        logger.info(
            f"[chrome_driver] Usando chromedriver empaquetado: {empaquetado}"
        )

    try:
        preparado = _preparar_en_carpeta(ruta, version, empaquetado)
    except OSError as e:
        if not _es_error_permiso(e):
            raise
        carpeta_alt = _carpeta_fallback_escribible()
        ruta_alt = os.path.join(carpeta_alt, os.path.basename(ruta))
        logger.warning(
            f"[chrome_driver] No se pudo escribir el driver en "
            f"{os.path.dirname(ruta)} ({type(e).__name__}: {e}); "
            f"se prepara en la carpeta de respaldo {ruta_alt}"
        )
        preparado = _preparar_en_carpeta(ruta_alt, version, empaquetado)
    return _ruta_ejecutable(preparado)


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


def crear_chrome(options, version_main=None, intentos=3):
    """Devuelve un driver uc.Chrome listo (driver compartido pre-parcheado, sin carreras).

    - Aplica los flags de ahorro de datos, de estabilidad del renderer y de
      contenedor (``--disable-dev-shm-usage`` porque /dev/shm es pequeno en
      Docker/Railway y provoca ``tab crashed``, ``--disable-extensions``,
      ``--disable-component-extensions-with-background-pages``,
      ``--mute-audio``, renderer-process-limit, heap capado, site-per-process
      off y ``--disable-features`` fusionado en UNA sola bandera).
    - Usa SIEMPRE el chromedriver pre-parcheado de ``_dir_drivers()`` (por
      defecto ``data/bin/``; en el .exe cae a una carpeta escribible) y se
      prepara una sola vez bajo lock de archivo entre procesos.
    - Limita los lanzamientos concurrentes (``CHROME_LAUNCH_MAX``, default 2)
      con un jitter de 0.2-0.5s para que N workers no hagan fork/exec al mismo
      tiempo (causa de ``BlockingIOError: [Errno 11]``).
    - ``intentos`` (default 3 = hasta 2 reintentos): ante fallos transitorios
      tipicos de la contencion (Text file busy / NoSuchDriver / Connection
      refused / timeouts / agotamiento de PIDs) espera y reintenta. Si el fallo
      es de recursos (Errno 11, can't start new thread) el backoff es 2-4s.
      Al agotarse, relanza la ultima excepcion para que el bot la reporte.
    """
    version = version_main
    if version is None:
        try:
            version = detectar_chrome_version()
        except Exception:
            version = None

    _aplicar_flags_ahorro(options)
    _aplicar_flags_estabilidad(options)
    _aplicar_flags_contenedor(options)
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
            # Throttle: como maximo `_LANZAMIENTOS_MAX` forks a la vez, con un
            # jitter corto para que no coincidan exactamente.
            with _SEMAFORO_LANZAMIENTO:
                time.sleep(0.2 + random.random() * 0.3)
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
                if _es_fallo_recursos(e):
                    espera = 2.0 + random.random() * 2.0
                else:
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
