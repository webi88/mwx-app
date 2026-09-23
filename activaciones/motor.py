"""Motor de activacion masiva.

Orquesta quote-RTs aleatorizados sobre muchas cuentas a la vez, repartidas
en cohortes temporales para no disparar el anti-spam de X.

Caracteristicas:
- Concurrencia limitada de navegadores (configurable, default 15).
- Cada cuenta usa su propio proxy (guardado en la BD) y su cookie.
- Contenido variado por cuenta (pool de variaciones).
- Delay aleatorio entre acciones y arranque escalonado por cohortes.
- Headless para correr en Railway/VPS sin pantalla.
"""
import inspect
import os
import random
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from core.database import get_db_session
from core.models import Cuenta
from core.config import settings, resolver_ruta
from core.roles import normalizar_rol_activacion
from core.secciones import normalizar_seccion
from core.registros import normalizar_tipo_cuenta
from core.perfiles import normalizar_perfil, colocar_hashtag_en_medio
from activaciones.variaciones import generar_pool_variaciones_openai, variar_texto
from core.registro import (
    registrar_accion,
    marcar_cuenta_suspendida,
    tipo_registro_rol,
)


def _env_activo(nombre: str, default: bool = True) -> bool:
    """True si la env `nombre` no esta desactivada.

    Valores que desactivan: "0", "false", "no", "off" (sin distinguir
    mayusculas ni espacios). Vacia o ausente devuelve `default`. Nunca lanza.
    """
    try:
        valor = os.environ.get(nombre)
    except Exception:
        return bool(default)
    if valor is None or not str(valor).strip():
        return bool(default)
    return str(valor).strip().lower() not in ("0", "false", "no", "off")


def _env_int(nombre: str, default: int, minimo: int = None,
             maximo: int = None) -> int:
    """Entero de la env `nombre`, acotado a [minimo, maximo].

    Si la variable no existe o no parsea se usa `default`; luego se aplican
    los limites. Nunca lanza.
    """
    try:
        valor = os.environ.get(nombre)
        if valor is not None and str(valor).strip():
            numero = int(float(str(valor).strip()))
        else:
            numero = int(default)
    except Exception:
        try:
            numero = int(default)
        except Exception:
            numero = 0
    if minimo is not None and numero < int(minimo):
        numero = int(minimo)
    if maximo is not None and numero > int(maximo):
        numero = int(maximo)
    return numero


def _parsear_tokens(valor: str) -> list[str]:
    """Separa hashtags/menciones por comas, espacios o saltos de linea."""
    if not valor:
        return []
    return [t for t in re.split(r"[,\s]+", str(valor).strip()) if t]


def _ram_max_mb() -> int:
    """RAM maxima del proceso en MB, o -1 si `resource` no esta disponible.

    En Linux (`resource.getrusage`) `ru_maxrss` viene en KB; en macOS en bytes
    (se normaliza igual). Nunca lanza: en Windows el modulo `resource` no
    existe y se devuelve -1.
    """
    try:
        import resource

        valor = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform == "darwin":
            valor = valor / (1024.0 * 1024.0)
        else:
            valor = valor / 1024.0
        return int(valor)
    except Exception:
        return -1


def _normalizar_hashtags(valor: str) -> list[str]:
    """Devuelve hashtags con '#' garantizado, sin duplicados ni vacios."""
    tags, vistos = [], set()
    for token in _parsear_tokens(valor):
        token = token.strip()
        if not token:
            continue
        if not token.startswith("#"):
            token = "#" + token.lstrip("@")
        clave = token.lower()
        if clave not in vistos:
            vistos.add(clave)
            tags.append(token)
    return tags


def _normalizar_menciones(valor: str) -> list[str]:
    """Devuelve menciones con '@' garantizado, sin duplicados ni vacios."""
    menciones, vistos = [], set()
    for token in _parsear_tokens(valor):
        token = token.strip().lstrip("@")
        if not token:
            continue
        token = "@" + token
        clave = token.lower()
        if clave not in vistos:
            vistos.add(clave)
            menciones.append(token)
    return menciones


# URL de PUBLICACION real: host twitter.com/x.com + `/status/<id>`. El perfil
# de la cuenta (`https://twitter.com/<usuario>`, sin `/status/`) NO es una
# publicacion y se descarta (antes se registraba como link del RT simple o de
# la cita sin URL capturada).
_RE_URL_PUBLICACION = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*(?:twitter\.com|x\.com)/[^/?#]+/status/\d+",
    re.IGNORECASE,
)


def _url_publicada_valida(url, usuario: str = "") -> str:
    """URL de publicacion limpia, o "" si no es valida (nunca el perfil).

    Solo acepta http(s) de `twitter.com`/`x.com` con `/status/<id>`; limpia
    query y fragmento. El perfil de `usuario`
    (`https://twitter.com/<usuario>` / `https://x.com/<usuario>`), el ancla, una
    basura cualquiera o un `None` devuelven `""`: `RegistroAccion.url_publicacion`
    JAMAS debe llevar el ancla ni un perfil. `usuario` se acepta por contrato
    (compatibilidad de firma; el descarte de perfiles lo hace el regex al exigir
    `/status/<id>`). Nunca lanza.
    """
    try:
        texto = str(url or "").strip()
        if not texto or not _RE_URL_PUBLICACION.match(texto):
            return ""
        return texto.split("?")[0].split("#")[0].rstrip("/")
    except Exception:
        return ""


MENSAJE_SIN_SESION = (
    "sin sesión: sin .pkl, cookies_json/auth_token ni password; "
    "brandea o carga credenciales antes de activar"
)

MENSAJE_SOLO_PASSWORD = (
    "sin sesión: solo password; activa cookies/auth_token para "
    "campañas rápidas"
)

# Resultado de una accion NO ejecutada por cuota horaria/DIARIA agotada
# (`ok=None`): no es exito ni fallo, no registra nada en la BD y no dispara
# callback.
MENSAJE_CUOTA_AGOTADA = "cuota agotada: la cuenta alcanzo sus limites por hora"

# Resultado de una accion NO ejecutada por PARO TOTAL del usuario (`ok=None`):
# mismo tratamiento que la cuota agotada (no es exito ni fallo, no abre
# navegador, no reserva cuota, no registra nada en la BD y no dispara callback).
MENSAJE_CANCELADO = "campaña cancelada por el usuario"

# Resultado de una accion NO ejecutada porque el TIER de la cuenta prohibe el
# rol (Tier 2 jamas publica hashtags/posts originales): tampoco es exito ni
# fallo y jamas toca la BD.
MENSAJE_TIER2_SIN_ROL = (
    "tier 2 sin rol permitido: solo RT/Cita/Comentario (no publica hashtags)"
)


def _avisar_cuota_agotada_log(cuotas, usuario) -> bool:
    """Avisa (una vez por minuto y cuenta) que la cuenta agoto sus cuotas.

    Usa `cuotas.avisar_agotada` (throttle por cuenta) y emite el mensaje
    EXACTO con `logger.debug`. Es un helper de modulo para que el metodo
    estatico `_asignar_roles_aleatorios` (sin `self`) pueda usarlo igual que
    `MotorActivacion._avisar_cuota_agotada`. Nunca lanza.
    """
    try:
        if cuotas is not None and cuotas.avisar_agotada(usuario):
            logger.debug(
                f"Cuenta {usuario} alcanzó todos sus límites por hora, "
                f"descansando"
            )
            return True
    except Exception:
        pass
    return False


def _mensaje_sin_sesion(cuenta) -> str:
    """Mensaje de `sin_sesion` segun la credencial que falte.

    Con `ACTIVACION_PERMITIR_PASSWORD=0` (default) una cuenta solo-password
    recibe `MENSAJE_SOLO_PASSWORD` (accion clara); el resto conserva
    `MENSAJE_SIN_SESION`. Nunca lanza.
    """
    try:
        if _solo_password(cuenta):
            return MENSAJE_SOLO_PASSWORD
    except Exception:
        pass
    return MENSAJE_SIN_SESION


def _sugerencia_sesion(n: int) -> str:
    """Accion sugerida para las cuentas filtradas por falta de sesion."""
    return (
        f"{n} cuenta(s) sin sesión: no tienen .pkl en "
        "data/cookies/twitter/ ni cookies_json/auth_token en la BD (las que "
        "solo tienen password se omiten por lentas: usa "
        "ACTIVACION_PERMITIR_PASSWORD=1 para permitirlas). Brandéalas (login "
        "manual) o importa el lote con auth_token/cookies antes de activar; "
        "se saltaron sin abrir navegador."
    )


_SENALES_ERROR_DRIVER_TRANSITORIO = (
    "connection refused",
    "failed to establish a new connection",
    "max retries",
    "maxretry",
    "newconnectionerror",
    "nosuchdriver",
    "unable to obtain driver",
    "text file busy",
    "connection aborted",
    "remotedisconnected",
    "errno 111",
    "errno 26",
    "errno 11",
    "can't start new thread",
    "can not start new thread",
    "resource temporarily unavailable",
    "tab crashed",
    "err_proxy_connection_failed",
    "proxy connection failed",
    "chrome not reachable",
    "invalid session id",
    "disconnected",
    "no such file or directory",
)


# Fallos de publicacion en los que X NUNCA recibio el contenido: nada se
# publico, asi que reintentar no puede duplicar el post. (Los fallos ambiguos,
# como "X no confirmó la publicación", NO se reintentan a proposito: el tweet
# pudo haberse enviado antes de perder la confirmacion.)
_SENALES_ERROR_PUBLICACION_SEGURA = (
    "post deshabilitado",
    "no se pudo escribir el texto en el editor",
    "boton de retweet no encontrado",
    "botón de retweet no encontrado",
    "compositor de x no cargo",
    "compositor no disponible",
    "no se encontro el boton responder",
    "no se encontró el botón responder",
    "boton responder deshabilitado",
    "botón responder deshabilitado",
)


# Sesion de X caida (cookies vencidas o login pedido al abrir el compositor):
# reintentar con las MISMAS cookies solo repetiria el fallo, asi que NO es
# reintentable ni significa que la cuenta este suspendida. El detalle se
# normaliza para indicar la accion real (renovar cookies/login).
_SENALES_SESION_INVALIDA = (
    "sesión de x expirada",
    "sesion de x expirada",
    "se pidió login",
    "se pidio login",
)

MENSAJE_SESION_INVALIDA = "sesión de X expirada: renueva cookies/login"


# Senales de ANTI-BOT / PAGINA INTERMEDIA de X (Cloudflare "Just a moment",
# interstitial "something went wrong", etc.): el bot puede confundirlas con
# una cuenta suspendida. El motor NUNCA desactiva una cuenta por estas
# senales. El subconjunto DURO se omite en lo que resta de la campana; las
# paginas de error transitorias llevan contador por cuenta
# (`_SENALES_NAVEGACION_TRANSITORIA`) y siguen participando hasta agotarlo.
_SENALES_ANTI_BOT = (
    "verificación anti-bot",
    "verificacion anti-bot",
    "cloudflare",
    "just a moment",
    "un momento",
    "challenges.cloudflare",
    "something went wrong",
    "algo salió mal",
    "algo salio mal",
    "pagina de error de x",
    "página de error de x",
    "interstitial",
)

# Subconjunto DURO: el challenge anti-bot que impide confirmar la sesion. La
# cuenta se omite en lo que resta de la campana (no se desactiva) y NO se
# reintenta. Las paginas de error transitorias ("something went wrong",
# "pagina de error de X", "interstitial") quedan fuera a proposito: esas se
# reintentan UNA vez porque un refresh suele resolverlas.
_SENALES_ANTI_BOT_BLOQUEO = (
    "verificación anti-bot",
    "verificacion anti-bot",
    "cloudflare",
    "just a moment",
    "un momento",
    "challenges.cloudflare",
)

# Fallos TRANSITORIOS de NAVEGACION de X (pagina de error generica /
# interstitial): NO significan que la sesion de la cuenta este caida. Cada
# cuenta lleva un contador por campana y SIGUE participando en las rondas;
# solo tras `_MAX_FALLOS_TRANSITORIOS` se omite (sin desactivarla en la BD).
_SENALES_NAVEGACION_TRANSITORIA = (
    "something went wrong",
    "algo salió mal",
    "algo salio mal",
    "pagina de error de x",
    "página de error de x",
    "interstitial",
)

_MAX_FALLOS_TRANSITORIOS = 2

# Rechazos DEFINITIVOS de X a la accion de la cuenta ("may not be allowed",
# "cuenta limitada por X"): se omite el resto de la campana (mismo mecanismo
# en memoria que la sesion caida, SIN tocar la BD) y NO se reintentan: gastar
# otro navegador no cambia el rechazo.
_SENALES_RECHAZO_X = (
    "your account may not be allowed to perform this action",
    "not allowed to perform",
    "x rechazó el post",
    "x rechazo el post",
    "cuenta limitada por x",
    "limitada por x",
)

# Detalles que indican que la SESION de la cuenta ya no sirve dentro de la
# campana (cookies vencidas, sin credenciales, login fallido, challenge
# anti-bot): esas cuentas se sacan del orden de las rondas siguientes para no
# quemar intentos ni abrir navegadores inutiles. Incluye las variantes de
# password/TOTP que antes no entraban ("X pidio verificar identidad...",
# challenge sin resolver, etc.) y TODAS las senales anti-bot (sin desactivar
# la cuenta). Las paginas de error transitorias entran aqui como senal, pero
# la politica real (contador por cuenta vs omision) la decide
# `_registrar_sesion_caida`.
_SENALES_SESION_CAIDA_POOL = (
    "sesión de x expirada",
    "sesion de x expirada",
    "login fallido",
    "no se pudo iniciar sesion",
    "no se pudo iniciar sesión",
    "no se pudo iniciar sesion con password",
    "auth_token/cookies inválidos",
    "auth_token/cookies invalidos",
    "auth_token inválido",
    "auth_token invalido",
    "verificar identidad",
    "password/totp",
    "no se pudo resolver",
    "sin cookies",
    "sin sesion",
    "sin sesión",
) + _SENALES_ANTI_BOT

# Senales de AGOTAMIENTO DE RECURSOS del contenedor (hilos/RAM): si aparecen,
# abrir MAS Chrome empeora el problema: el motor baja el limite de navegadores
# y NO reintenta la accion.
_SENALES_ERROR_RECURSOS = (
    "can't start new thread",
    "can not start new thread",
    "resource temporarily unavailable",
    "blockingioerror",
    "cannot connect to chrome",
    "chrome not reachable",
    "session not created",
    "tab crashed",
    "errno 11",
)


def _es_sesion_caida_detalle(detalle) -> bool:
    """True si el detalle indica que la sesion de la cuenta ya no sirve.

    Tolera None y tipos raros; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_SESION_CAIDA_POOL)


def _es_error_sesion_invalida(detalle) -> bool:
    """True si el detalle indica que la sesion de X ya no es valida.

    Tolera None y tipos raros; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_SESION_INVALIDA)


def _detalle_con_sesion(motivo) -> str:
    """Devuelve el detalle listo para reportar, normalizando la sesion caida.

    Si `motivo` corresponde a sesion expirada/invalida se reemplaza por
    `MENSAJE_SESION_INVALIDA`; si no, se devuelve el texto original ('' si
    viene vacio o no es convertible).
    """
    if _es_error_sesion_invalida(motivo):
        return MENSAJE_SESION_INVALIDA
    try:
        return "" if motivo is None else str(motivo)
    except Exception:
        return ""


def _detalle_login_fallido(motivo) -> str:
    """Detalle de un login fallido con el marcador 'login fallido' garantizado.

    Antes `getattr(bot, "ultimo_error", "") or "login fallido"` dejaba pasar
    motivos de password/TOTP ("X pidio verificar identidad...") que NO estaban
    en `_SENALES_SESION_CAIDA_POOL`: la cuenta no se sacaba del pool y gastaba
    login lento en cada ronda. Aqui el detalle siempre lleva "login fallido"
    (salvo sesion expirada, que se normaliza). Nunca lanza.
    """
    detalle = _detalle_con_sesion(motivo) or "login fallido"
    if _es_error_sesion_invalida(detalle):
        return detalle
    if "login fallido" in detalle.lower():
        return detalle
    return f"login fallido: {detalle}"


def _detalle_comentario(motivo) -> str:
    """Detalle legible del fallo de un comentario/respuesta.

    Normaliza la sesion caida (`_detalle_con_sesion`) y deja claro cuando el
    tweet ancla no acepta respuestas (respuestas limitadas): no es un fallo de
    la cuenta y no se reintenta. Nunca lanza.
    """
    try:
        texto = "" if motivo is None else str(motivo)
    except Exception:
        texto = ""
    texto_lower = texto.lower()
    if (
        "respuestas limitadas" in texto_lower
        or "no permite respuestas" in texto_lower
    ):
        return "el tweet ancla no permite respuestas"
    return _detalle_con_sesion(f"comentario: {texto}")


def _es_error_driver_transitorio(detalle) -> bool:
    """True si el detalle parece un fallo transitorio de driver/navegador.

    Revisa las senales tipicas de chromedriver/Selenium caido o reiniciandose
    (conexion rechazada, driver no obtenible, archivo en uso, etc.). Tolera
    None y tipos raros; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle)
        texto = texto.lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ERROR_DRIVER_TRANSITORIO)


def _es_error_recursos(detalle) -> bool:
    """True si el detalle es agotamiento de recursos del contenedor.

    Hilos ("can't start new thread", "resource temporarily unavailable"),
    Chrome muerto ("cannot connect to chrome", "chrome not reachable",
    "session not created", "tab crashed"). Con estos fallos NO se reintenta:
    lanzar mas Chrome empeora el agotamiento. Tolera None; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ERROR_RECURSOS)


def _es_anti_bot_detalle(detalle) -> bool:
    """True si el detalle menciona anti-bot/Cloudflare/interstitial de X.

    Estas senales NO son una suspension real de la cuenta: el motor jamas
    debe desactivarla por ellas (solo omitirla en la campana). Tolera None y
    tipos raros; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ANTI_BOT)


def _es_anti_bot_bloqueo(detalle) -> bool:
    """True si el detalle es el challenge anti-bot DURO (Cloudflare).

    A diferencia de `_es_anti_bot_detalle` incluye solo las senales que
    impiden confirmar la sesion: la cuenta se omite en la campana y NO se
    reintenta. Las paginas de error transitorias de X ("something went wrong",
    "pagina de error de X", "interstitial") no entran aqui a proposito (esas
    se reintentan una vez). Tolera None; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ANTI_BOT_BLOQUEO)


def _es_fallo_navegacion_transitorio(detalle) -> bool:
    """True si el detalle es una pagina de error/interstitial TRANSITORIO de X.

    Estas senales NO son sesion caida: la cuenta puede curarse con un intento
    posterior, asi que el motor la deja seguir en las rondas con un contador
    (`_registrar_fallo_transitorio`). Tolera None; nunca lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_NAVEGACION_TRANSITORIA)


def _es_rechazo_x(detalle) -> bool:
    """True si X RECHAZO definitivamente la accion de la cuenta.

    Senales como "Your account may not be allowed to perform this action" o
    "cuenta limitada por X": la cuenta se omite el resto de la campana (sin
    desactivarla en la BD) y el fallo NO se reintenta. Tolera None; nunca
    lanza.
    """
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_RECHAZO_X)


def _es_error_reintentable(detalle) -> bool:
    """True si el fallo amerita UN reintento sin riesgo de duplicar el post.

    Incluye los fallos transitorios de driver/navegador y los fallos de
    publicacion donde el texto no llego a enviarse (boton Post deshabilitado o
    editor que no registro el texto). EXCLUYE la sesion expirada/invalida:
    reintentar con las mismas cookies no arregla nada y hay que renovarlas.
    EXCLUYE el challenge anti-bot DURO (Cloudflare/"just a moment"): la cuenta
    se omite en la campana y reintentar pagaria otro challenge. EXCLUYE los
    rechazos DEFINITIVOS de X ("may not be allowed", "cuenta limitada por X"):
    otro navegador recibe el mismo rechazo. Las paginas de error transitorias
    ("compositor no disponible (pagina de error de X)", "compositor de X no
    cargo") SI se reintentan: un refresh las resuelve. Nunca lanza.
    """
    if _es_error_sesion_invalida(detalle):
        return False
    if _es_anti_bot_bloqueo(detalle):
        return False
    if _es_rechazo_x(detalle):
        return False
    if _es_error_driver_transitorio(detalle):
        return True
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ERROR_PUBLICACION_SEGURA)


def _rt_por_api_activo() -> bool:
    """True si el rol 'rt' debe intentar primero la API HTTP (env RT_POR_API).

    Default activo; `0`, `false`, `no` u `off` lo desactivan (sin distinguir
    mayusculas ni espacios). Nunca lanza.
    """
    try:
        valor = str(os.environ.get("RT_POR_API", "") or "").strip().lower()
    except Exception:
        return True
    return valor not in ("0", "false", "no", "off")


def _api_primero_activo() -> bool:
    """True si TODOS los roles deben intentar la API HTTP antes de Selenium.

    Lee `API_PRIMERO` (la UI escribe `API_PRIMERO` y `RT_POR_API` juntas); si
    la variable no existe o esta vacia se hereda el comportamiento historico
    de `RT_POR_API` (alias/compatibilidad). Default True. Nunca lanza.
    """
    try:
        valor = os.environ.get("API_PRIMERO")
    except Exception:
        return True
    if valor is None or not str(valor).strip():
        return _rt_por_api_activo()
    return str(valor).strip().lower() not in ("0", "false", "no", "off")


def _permitir_password() -> bool:
    """True si `ACTIVACION_PERMITIR_PASSWORD` permite cuentas solo-password.

    Default 0 (desactivado): una cuenta con SOLO password (sin .pkl,
    cookies_json ni auth_token) NO entra a campanas rapidas porque el login
    con password/TOTP es lento y suele fallar; se reporta en `sin_sesion` sin
    abrir navegador. Con la env en 1 se conserva el comportamiento anterior.
    Nunca lanza.
    """
    try:
        valor = os.environ.get("ACTIVACION_PERMITIR_PASSWORD")
    except Exception:
        return False
    if valor is None or not str(valor).strip():
        return False
    return str(valor).strip().lower() not in ("0", "false", "no", "off")


def _tiene_credencial_sesion(cuenta, permitir_password: bool = True) -> bool:
    """True si la cuenta tiene alguna credencial de sesion usable.

    Revisa, sin abrir ningun navegador: archivo .pkl en disco,
    `cookies_json` con contenido y `auth_token` no vacio en la BD. La
    `password` solo cuenta con `permitir_password=True` (ver
    `_permitir_password`).
    """
    usuario = (getattr(cuenta, "usuario", "") or "").strip()
    if usuario:
        try:
            if os.path.exists(resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")):
                return True
        except Exception:
            pass
    cookies = getattr(cuenta, "cookies_json", None)
    if isinstance(cookies, list):
        if len(cookies) > 0:
            return True
    elif isinstance(cookies, str):
        if cookies.strip() and cookies.strip().lower() not in ("[]", "null", "none"):
            return True
    elif cookies:
        return True
    auth = getattr(cuenta, "auth_token", "")
    if isinstance(auth, str) and auth.strip():
        return True
    elif auth:
        return True
    if not permitir_password:
        return False
    password = getattr(cuenta, "password", "")
    if isinstance(password, str):
        return bool(password.strip())
    return bool(password)


def _solo_password(cuenta) -> bool:
    """True si la cuenta SOLO tiene password (sin cookies/auth_token/.pkl).

    Se usa para reportarle un mensaje claro en `sin_sesion`. Nunca lanza.
    """
    try:
        if _tiene_credencial_sesion(cuenta, permitir_password=False):
            return False
        return bool(_tiene_credencial_sesion(cuenta, permitir_password=True))
    except Exception:
        return False


def _partir_por_sesion(cuentas: list) -> tuple:
    """Separa cuentas con credencial de sesion de las que no tienen ninguna.

    Con `ACTIVACION_PERMITIR_PASSWORD=0` (default) las cuentas con SOLO
    password NO cuentan como credencial: van a `sin_sesion` (login con
    password/TOTP lento y propenso a fallar). Con la env en 1 se comporta como
    antes. Nunca lanza.
    """
    permitir = _permitir_password()
    con, sin = [], []
    for cuenta in (cuentas or []):
        (con if _tiene_credencial_sesion(cuenta, permitir) else sin).append(cuenta)
    return con, sin


def _partir_por_registro(cuentas: list) -> tuple:
    """Separa cuentas con registro definido de las que no lo tienen.

    El registro se normaliza con `core.registros.normalizar_tipo_cuenta`
    ("politica"/"activista"/"ciudadana"); "" = sin definir.
    """
    con, sin = [], []
    for cuenta in (cuentas or []):
        try:
            registro = normalizar_tipo_cuenta(getattr(cuenta, "tipo_cuenta", ""))
        except Exception:
            registro = ""
        (con if registro else sin).append(cuenta)
    return con, sin


def _sugerencia_registro(n: int) -> str:
    """Accion sugerida para las cuentas filtradas por falta de registro."""
    return (
        f"{n} cuenta(s) sin registro definido (política/activista/ciudadanía): "
        "asígnales registro en 🗂️ Cuentas antes de activar; se omitieron."
    )


def _tier_cuenta(cuenta) -> str:
    """Tier normalizado de una cuenta ("tier1"/"tier2"/""); import perezoso.

    Tolerante: si `core.tiers` no esta disponible o la cuenta es rara,
    devuelve "" (sin tier, se comporta como volumen sin restriccion).
    """
    try:
        from core.tiers import tier_de_cuenta

        return tier_de_cuenta(cuenta)
    except Exception:
        return ""


def _es_tier2(cuenta) -> bool:
    """True si la cuenta es Tier 2 (Volumen/Aged). Nunca lanza."""
    try:
        from core.tiers import es_tier2

        return bool(es_tier2(cuenta))
    except Exception:
        return False


def _tier_permitido(cuenta, rol) -> bool:
    """True si el tier de la cuenta permite ese rol (import perezoso).

    False SOLO si es Tier 2 y el rol normalizado es "hashtags" (incluye
    post/publicacion/mantenimiento/calentamiento/hilo). Ante cualquier fallo
    devuelve True (no bloquear por un import roto)."""
    try:
        from core.tiers import rol_permitido_tier

        return bool(rol_permitido_tier(cuenta, rol))
    except Exception:
        return True


def _error_tier(cuenta, rol) -> str:
    """Mensaje bloqueante del tier ("" si el rol esta permitido); nunca lanza."""
    try:
        from core.tiers import error_rol_tier

        return str(error_rol_tier(cuenta, rol) or "")
    except Exception:
        return ""


def _filtrar_por_seccion(cuentas: list, secciones) -> list:
    """Filtra cuentas por seccion canonica (CI/IP/LIB/JUS...).

    Normaliza las secciones pedidas con `core.secciones.normalizar_seccion` y
    descarta las que no son validas (""). Si `secciones` viene vacio o None NO
    hay filtro: devuelve todas las cuentas. Si se pidieron secciones pero
    ninguna normaliza a un codigo valido, devuelve [] (nada coincide). Filtra
    objetos cuyo `getattr(c, "seccion", "")` normalizado este en el set.
    Nunca lanza.
    """
    try:
        lista = list(cuentas or [])
    except TypeError:
        return []
    if not secciones:
        return lista
    if isinstance(secciones, str):
        valores = [secciones]
    else:
        try:
            valores = list(secciones)
        except TypeError:
            valores = [secciones]
    pedidas = set()
    for valor in valores:
        try:
            clave = normalizar_seccion(valor)
        except Exception:
            clave = ""
        if clave:
            pedidas.add(clave)
    if not pedidas:
        return []
    resultado = []
    for cuenta in lista:
        try:
            propia = normalizar_seccion(getattr(cuenta, "seccion", ""))
        except Exception:
            propia = ""
        if propia in pedidas:
            resultado.append(cuenta)
    return resultado


def _clamp_porcentaje(valor, default: float) -> float:
    """Acota un porcentaje a [1, 100]; con None/raros usa `default`."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return float(default)
    if numero != numero:  # NaN
        return float(default)
    return min(100.0, max(1.0, numero))


def _calcular_k_ronda(n: int, min_pct: float, max_pct: float) -> int:
    """Cantidad de cuentas de una ronda con subconjunto aleatorio.

    Devuelve estrictamente mas del minimo y estrictamente menos que todas
    (nunca todas), salvo que con `n` tan pequeno (<=2) o un rango imposible no
    se pueda: ahi devuelve n. Nunca lanza.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    if n <= 2:
        return n
    try:
        k_min = max(2, int(n * float(min_pct) / 100.0) + 1)
        k_max = min(n - 1, max(1, int(n * float(max_pct) / 100.0)))
        if float(max_pct) >= 100.0:
            k_max = n - 1
    except (TypeError, ValueError):
        return n
    if k_min > k_max:
        return n
    k = random.randint(k_min, k_max)
    return max(1, min(n, k))


def _normalizar_rol_sorteo(valor) -> str:
    """Normaliza un rol para el sorteo; acepta el alias "post" -> hashtags.

    Incluye el rol nuevo "comentario" (y sus variantes) por si core/roles.py
    aun no lo conoce: el sorteo nunca debe quedarse sin ese rol.
    """
    rol = normalizar_rol_activacion(valor)
    if not rol:
        texto = str(valor or "").strip().lower()
        if texto in ("post", "posts"):
            rol = "hashtags"
        elif texto in (
            "comentario", "comentarios", "comentar", "comenta",
            "respuesta", "respuestas", "reply", "replies",
        ):
            rol = "comentario"
    return rol


def _rol_efectivo_cuenta(cuenta) -> str:
    """Rol canonico de una cuenta para AGRUPAR, con alias "post"->hashtags.

    `core.roles.normalizar_rol_activacion` solo conoce los 4 roles canonicos;
    mucha data historica trae "post", "mantenimiento", "hilo", "respuesta" o
    "reply". El alias se resuelve con `_normalizar_rol_sorteo`, de modo que
    Tier 2 con "post" cae en el grupo hashtags y el blindaje por tier tambien
    lo cubre. Rol desconocido -> "" (sin rol). Nunca lanza.
    """
    try:
        rol = normalizar_rol_activacion(getattr(cuenta, "rol_activacion", ""))
    except Exception:
        rol = ""
    if not rol:
        try:
            rol = _normalizar_rol_sorteo(
                getattr(cuenta, "rol_activacion", "")
            )
        except Exception:
            rol = ""
    return rol


def _roles_disponibles_aleatorios(urls, hashtags="", contexto="",
                                  texto_base="", solo_roles=None,
                                  narrativa="") -> list[str]:
    """Roles que se pueden sortear con los inputs dados (modo aleatorio).

    - "cita", "rt" y "comentario" requieren al menos una URL objetivo (el
      comentario responde al tweet ancla).
    - "hashtags" requiere hashtags, contexto, texto base o narrativa
      (el trasfondo de noticias habilita el rol como material narrativo,
      pero NUNCA se usa como texto publicable).
    - `solo_roles`, si viene, limita el sorteo a su interseccion con los
      disponibles; si la interseccion queda vacia se usan todos los
      disponibles. Nunca lanza: ante cualquier valor raro devuelve lo que
      se pudo calcular.
    """
    try:
        disponibles = []
        try:
            hay_urls = any(str(u or "").strip() for u in (urls or []))
        except TypeError:
            hay_urls = False
        if hay_urls:
            disponibles.extend(["cita", "rt", "comentario"])
        if (
            str(hashtags or "").strip()
            or str(contexto or "").strip()
            or str(texto_base or "").strip()
            or str(narrativa or "").strip()
        ):
            disponibles.append("hashtags")
        if solo_roles:
            try:
                permitidos = {_normalizar_rol_sorteo(r) for r in solo_roles}
            except TypeError:
                permitidos = set()
            permitidos.discard("")
            if permitidos:
                filtrados = [r for r in disponibles if r in permitidos]
                if filtrados:
                    disponibles = filtrados
        return disponibles
    except Exception:
        return []


def _sugerencia_roles_aleatorios() -> str:
    """Accion sugerida cuando el modo aleatorio no tiene ningun rol posible."""
    return (
        "Rol aleatorio sin roles disponibles: pega al menos una URL objetivo "
        "(habilita cita/rt) o escribe hashtags, contexto, texto base O EL "
        "TRASFONDO DE NOTICIAS (habilita hashtags); no se abrió ningún "
        "navegador."
    )


def _garantizar_hashtags_texto_local(texto: str, tags: list) -> str:
    """Fallback local de `_garantizar_hashtags_texto` (sin IA).

    Normaliza los tags pedidos, elimina los hashtags que no esten entre ellos
    y reinserta en el MEDIO un subconjunto aleatorio (los que ya estuvieran
    presentes o, si no hay ninguno, 1..len(tags) elegidos al azar) respetando
    la grafia exacta pedida. Nunca lanza.
    """
    try:
        pedidos, claves = [], set()
        for tag in (tags or []):
            limpio = str(tag or "").strip()
            if not limpio:
                continue
            if not limpio.startswith("#"):
                limpio = "#" + limpio.lstrip("@")
            clave = limpio.lower()
            if clave not in claves:
                claves.add(clave)
                pedidos.append(limpio)
        if not pedidos:
            return texto

        presentes = {}
        for hallado in re.findall(_RE_HASHTAG, texto):
            clave = hallado.lower()
            if clave in claves:
                presentes.setdefault(clave, hallado)
        sin_tags = re.sub(_RE_HASHTAG, " ", texto)
        sin_tags = re.sub(r"\s+([,.;:!?])", r"\1", sin_tags)
        sin_tags = re.sub(r"[ \t]{2,}", " ", sin_tags)
        sin_tags = re.sub(r"\n{3,}", "\n\n", sin_tags).strip()
        if not sin_tags:
            return " ".join(pedidos)
        if presentes:
            elegidos = [
                presentes[p.lower()] for p in pedidos
                if p.lower() in presentes
            ]
        else:
            k = random.randint(1, len(pedidos))
            elegidos = random.sample(pedidos, k)
        if not elegidos:
            return sin_tags
        colocado = colocar_hashtag_en_medio(sin_tags, hashtag=elegidos[0])
        hallado = re.search(_RE_HASHTAG, colocado)
        if hallado and hallado.group(0).lower() == elegidos[0].lower():
            colocado = (
                colocado[:hallado.start()]
                + " ".join(elegidos)
                + colocado[hallado.end():]
            )
        return colocado
    except Exception:
        return texto


def _garantizar_hashtags_texto(texto: str, tags: list[str]) -> str:
    """Deja en el texto SOLO hashtags de `tags` (subconjunto aleatorio).

    Con `tags` no vacio delega en `ia.generador_contenido
    .solo_hashtags_pedidos` (import perezoso), que conserva un subconjunto
    aleatorio de 1..len(tags), elimina cualquier otro hashtag y lo integra en
    MEDIO (nunca al final). Si ese import no esta disponible, usa el fallback
    local `_garantizar_hashtags_texto_local` (mismo criterio, con
    `core.perfiles.colocar_hashtag_en_medio`). Con `tags` vacio devuelve el
    texto tal cual. Nunca lanza.
    """
    t = str(texto or "").strip()
    if not t or not tags:
        return t
    try:
        from ia.generador_contenido import solo_hashtags_pedidos
    except Exception:
        solo_hashtags_pedidos = None
    if callable(solo_hashtags_pedidos):
        try:
            # Semilla aleatoria por texto: el subconjunto 1..len(tags) varia
            # entre textos (con la semilla 0 seria siempre el mismo).
            resultado = solo_hashtags_pedidos(
                t, list(tags), semilla=random.randint(0, 10**9)
            )
            if resultado is not None:
                return str(resultado)
        except Exception:
            pass
    return _garantizar_hashtags_texto_local(t, tags)


def _agregar_menciones(texto: str, menciones_norm: list[str]) -> str:
    """Agrega al final un subconjunto aleatorio de menciones (si hay)."""
    t = str(texto or "").strip()
    if not t or not menciones_norm:
        return t
    try:
        k = random.randint(1, len(menciones_norm))
        seleccion = random.sample(list(menciones_norm), k)
        random.shuffle(seleccion)
        return f"{t}\n\n{' '.join(seleccion)}".strip()
    except Exception:
        return t


def _limpiar_comentario_spam(texto) -> str:
    """Quita hashtags, URLs y @menciones de un comentario (anti-spam de X).

    Usa `ia.generador_contenido.limpiar_comentario_spam` (import perezoso);
    si no esta disponible aplica un fallback local equivalente. Nunca lanza:
    ante cualquier fallo devuelve el texto original sin espacios sobrantes.
    """
    try:
        from ia.generador_contenido import limpiar_comentario_spam
    except Exception:
        limpiar_comentario_spam = None
    if callable(limpiar_comentario_spam):
        try:
            limpio = limpiar_comentario_spam(texto)
            if limpio is not None:
                return str(limpio)
        except Exception:
            pass
    try:
        t = str(texto or "")
        t = re.sub(r"(?:https?://|www\.)\S+", " ", t, flags=re.IGNORECASE)
        t = re.sub(_RE_HASHTAG, " ", t)
        t = re.sub(r"@[A-Za-z0-9_]+", " ", t)
        t = re.sub(r"\s+([,.;:!?])", r"\1", t)
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()
    except Exception:
        return str(texto or "").strip()


_RE_HASHTAG = r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+"


def _narrativa_con_ronda(narrativa: str, ronda: int) -> str:
    """Aumenta la narrativa con la instruccion anti-repeticion de la ronda.

    A partir de la ronda 2 pide a la IA un texto completamente distinto al de
    las rondas anteriores. Con `ronda` 0/1 (modo clasico) devuelve la
    narrativa intacta.
    """
    base = str(narrativa or "").strip()
    try:
        numero = int(ronda or 0)
    except (TypeError, ValueError):
        numero = 0
    if numero <= 1:
        return base
    bloque = (
        f"RONDA {numero}: escribe un texto COMPLETAMENTE distinto a las "
        "rondas anteriores: otro enfoque, otras palabras, otra apertura y "
        "otro cierre; prohibido repetir frases o el mismo mensaje."
    )
    return f"{base}\n{bloque}" if base else bloque


def _clave_texto(texto) -> str:
    """Normaliza un texto para comparar repeticiones entre rondas."""
    try:
        return re.sub(r"\s+", " ", str(texto or "")).strip().lower()
    except Exception:
        return str(texto or "")


def _variar_protegiendo_hashtags(texto: str) -> str:
    """Varía el cuerpo de `texto` sin destruir los hashtags.

    Extrae los hashtags ANTES de variar (los sinonimos de `variar_texto`
    podrian convertir "mexico" en "nuestro país"), varia el cuerpo con
    `variar_texto(n_hashtags=0)`, reinserta el PRIMER hashtag EN EL MEDIO con
    `colocar_hashtag_en_medio` restaurando su grafia original (ese helper
    normaliza mayusculas, p.ej. #mexico -> #Mexico) y re-agrega los demas al
    final. Nunca lanza: devuelve el texto original si algo falla.
    """
    try:
        original = str(texto or "").strip()
        if not original:
            return original
        tags = re.findall(_RE_HASHTAG, original)
        cuerpo = re.sub(_RE_HASHTAG, " ", original)
        cuerpo = re.sub(r"[ \t]+", " ", cuerpo)
        cuerpo = re.sub(r"\n{3,}", "\n\n", cuerpo).strip()
        if not cuerpo:
            return original
        variado = str(variar_texto(cuerpo, n_hashtags=0) or "").strip()
        if not variado:
            return original
        if not tags:
            return variado
        variado = colocar_hashtag_en_medio(variado, hashtag=tags[0])
        variado = re.sub(
            _RE_HASHTAG,
            lambda m: (
                tags[0] if m.group(0).lower() == tags[0].lower()
                else m.group(0)
            ),
            variado,
        )
        if len(tags) > 1:
            variado = f"{variado} {' '.join(tags[1:])}".strip()
        return variado
    except Exception:
        return str(texto or "").strip()


def _aplicar_anti_repeticion(asignaciones: dict, usados: dict) -> dict:
    """Evita que una MISMA cuenta repita texto entre rondas de la campana.

    `usados` vive toda la campana y mapea {usuario: set(claves)}. Si el texto
    asignado ya se uso, aplica hasta 3 variaciones protegiendo los hashtags y
    vuelve a comprobar; si sigue repitiendose, lo deja variado. Nunca lanza.
    """
    if not isinstance(asignaciones, dict):
        return {}
    if not isinstance(usados, dict):
        usados = {}
    finales = {}
    for usuario, texto in asignaciones.items():
        t = str(texto or "")
        clave = _clave_texto(t)
        vistos = usados.get(usuario)
        if not isinstance(vistos, set):
            vistos = set()
            usados[usuario] = vistos
        intentos = 0
        while clave in vistos and intentos < 3:
            t = _variar_protegiendo_hashtags(t)
            clave = _clave_texto(t)
            intentos += 1
        vistos.add(clave)
        finales[usuario] = t
    return finales


class _Pestana:
    """Entrada del pool de pestañas persistentes del motor.

    Guarda el `TwitterBot` (Chrome) abierto, la cuenta que atiende ahora
    (`usuario`), cuantas acciones lleva desde el ultimo cambio de cuenta
    (`acciones`) y si algun worker la tiene tomada (`ocupada`). `creada` es el
    `time.monotonic()` de su creacion real (reciclado por EDAD ademas de por
    acciones: `PESTANA_MAX_MINUTOS`).
    """

    __slots__ = ("bot", "usuario", "acciones", "ocupada", "creada")

    def __init__(self, bot=None, usuario: str = "", acciones: int = 0,
                 ocupada: bool = False, creada=None):
        self.bot = bot
        self.usuario = str(usuario or "")
        self.acciones = int(acciones or 0)
        self.ocupada = bool(ocupada)
        self.creada = time.monotonic() if creada is None else float(creada)


class MotorActivacion:
    """Ejecuta una campaña de activacion (RT con cita) sobre N cuentas."""

    def __init__(self, max_concurrente: int = None):
        self.max_concurrente = max_concurrente or settings.max_browsers
        self._lock = threading.Lock()
        self.progreso = {
            "hechas": 0,
            "exitosas": 0,
            "fallidas": 0,
            "omitidas": 0,
            "ronda_actual": 1,
            "fase_actual": 1,
            "eventos": [],
        }
        # Sistema de Cuotas Inteligente por Hora: lo prepara cada campana
        # (`_preparar_cuotas`) con las cuentas ejecutables. Con None no hay
        # cuotas (comportamiento clasico, sin limites).
        self._cuotas = None
        self._ultimo_comentario_url: dict = {}
        self.pausa_comentario_url_seg: float = 0.0
        # GATE ADAPTATIVO de NAVEGADORES: las acciones HTTP (API primero) no lo
        # usan; solo los fallbacks a Selenium, limitados a `_limite_navegadores`
        # a la vez aunque haya muchos mas trabajadores en paralelo. Si el
        # contenedor se queda sin hilos/RAM, `_reducir_navegadores` baja el
        # limite a la mitad y no se reintenta en cascada.
        try:
            self._limite_navegadores = max(1, int(self.max_concurrente or 1))
        except Exception:
            self._limite_navegadores = 1
        self._navegadores_activos = 0
        self._navegadores_cond = threading.Condition()
        # Marca del ultimo agotamiento de recursos (None = nunca): durante
        # `_VENTANA_RECURSOS_SEG` no se reintentan acciones.
        self._recursos_agotados = None
        # Cuentas con la sesion caida DENTRO de esta campana: se descartan del
        # orden de las rondas siguientes (no cuentan como fallo por ronda).
        self._sesiones_caidas: set = set()
        # Cuentas omitidas por RECHAZO DEFINITIVO de X en esta campana (mismo
        # mecanismo en memoria que `_sesiones_caidas`, SIN tocar la BD).
        self._rechazos_x: set = set()
        # Fallos TRANSITORIOS de navegacion por cuenta (pagina de error /
        # interstitial): la cuenta sigue en las rondas; al agotar
        # `_MAX_FALLOS_TRANSITORIOS` pasa a `_transitorios_omitidos`.
        self._fallos_transitorios: dict = {}
        self._transitorios_omitidos: set = set()
        # Ancla asignada a cada cuenta de comentario al generar su texto: la
        # ejecucion usa ESA misma URL para que el comentario corresponda al
        # tweet que describe.
        self._ancla_por_cuenta: dict = {}
        # POOL DE PESTAÑAS PERSISTENTES: en vez de abrir/cerrar un Chrome por
        # accion, se mantienen hasta `_limite_navegadores` Chrome abiertos y
        # cada worker cambia de cuenta en caliente (`bot.cambiar_cuenta`).
        # Si el contrato no existe en selenium_bot, todo cae al camino clasico.
        self._modo_pestana = _env_activo("MODO_PESTANA", True)
        # Default 40 acciones por pestaña. El minimo duro es 1: los valores
        # explicitos por env se respetan (p.ej. 3 para reciclar rapido en
        # pruebas); 5 es el minimo RECOMENDADO para produccion.
        self._pestana_max_acciones = _env_int(
            "PESTANA_MAX_ACCIONES", 40, minimo=1
        )
        # Reciclado por EDAD de la pestaña persistente: ademas de
        # `PESTANA_MAX_ACCIONES`, una pestaña que cumple `PESTANA_MAX_MINUTOS`
        # minutos se cierra y se recrea (default 20; 0 = sin limite de edad).
        self._pestana_max_minutos = _env_int(
            "PESTANA_MAX_MINUTOS", 20, minimo=0
        )
        self._pestana_espera_seg = _env_int(
            "PESTANA_ESPERA_SEG", 180, minimo=0
        )
        self._pestanas: list = []
        self._pestanas_lock = threading.Lock()
        self._pestanas_cond = threading.Condition(self._pestanas_lock)
        self._pestanas_creadas = 0
        self._pestanas_recicladas = 0
        # CURVA DE ACELERACION ("modo explosion"): fase 1 solo Tier 1, fase 2
        # el resto tras `curva_fase1_min` minutos; las URLs publicadas por
        # Tier 1 en fase 1 cascada a los RT/citas/comentarios de fase 2.
        self._curva_activa = False
        self._curva_fase1_seg = 0.0
        self._curva_fase2_t0 = None
        self._urls_fase1: list = []
        self._cascada_usadas: set = set()
        # Tier de cada cuenta de la campana (usuario -> "tier1"/"tier2"/"")
        self._tier_de: dict = {}
        # Cuentas Tier 2 que `_obtener_cuentas_por_rol` excluyo por defensa.
        self._tier2_filtradas_rol: list = []
        # PARO TOTAL: evento de cancelacion de la campana en curso. Lo asigna
        # cada campana con el kwarg `cancelar` (solo si parece un Event real)
        # o `solicitar_paro()` (fallback interno ya seteado). `None` = sin
        # cancelacion (comportamiento actual exacto).
        self._cancelar = None
        # Evita repetir el INFO del paro mas de una vez por evento.
        self._paro_logueado = False

    _VENTANA_RECURSOS_SEG = 30.0

    # ------------------------------------------------------------------ #
    # PARO TOTAL (cancelacion)
    # ------------------------------------------------------------------ #
    def _log_paro(self) -> None:
        """Loguea UNA sola vez el paro total (idempotente; nunca lanza)."""
        try:
            if getattr(self, "_paro_logueado", False):
                return
            self._paro_logueado = True
            logger.info("Activacion: paro total solicitado; cerrando...")
        except Exception:
            pass

    def solicitar_paro(self) -> None:
        """Pide el PARO TOTAL de la campana en curso (idempotente; nunca lanza).

        Crea (si hace falta) un `threading.Event` interno YA SETEADO, lo deja
        en `self._cancelar` y despierta a los workers que esperan cupo de
        navegador/pestaña para que salgan de inmediato. Sirve de fallback
        cuando la UI no pasa `cancelar` a `ejecutar`/`ejecutar_por_roles`: las
        campanas siguientes lo respetan (terminan de inmediato).
        """
        try:
            evento = getattr(self, "_cancelar", None)
            if not callable(getattr(evento, "is_set", None)):
                evento = threading.Event()
                self._cancelar = evento
                self._paro_logueado = False
            evento.set()
            self._log_paro()
            for cond in (
                getattr(self, "_navegadores_cond", None),
                getattr(self, "_pestanas_cond", None),
            ):
                try:
                    if cond is not None:
                        with cond:
                            cond.notify_all()
                except Exception:
                    pass
        except Exception:
            pass

    def _guardar_cancelar(self, cancelar) -> None:
        """Guarda el evento de cancelacion externo (si parece un Event real).

        Con `None` o un objeto sin `is_set()` NO se toca `self._cancelar`: se
        conserva el evento interno de `solicitar_paro()` (si existe) para que
        la campana siguiente tambien lo respete. Nunca lanza.
        """
        try:
            if not callable(getattr(cancelar, "is_set", None)):
                return
            if cancelar is not getattr(self, "_cancelar", None):
                # Evento nuevo: el INFO del paro se emite una vez por evento.
                self._paro_logueado = False
            self._cancelar = cancelar
        except Exception:
            pass

    def _cancelado(self) -> bool:
        """True si hay paro total solicitado (kwarg `cancelar` o `solicitar_paro`).

        Nunca lanza: sin evento o con un objeto raro devuelve False
        (comportamiento identico al actual). Emite el INFO del paro UNA vez.
        """
        try:
            evento = getattr(self, "_cancelar", None)
            is_set = getattr(evento, "is_set", None)
            marcado = bool(is_set()) if callable(is_set) else False
        except Exception:
            return False
        if marcado:
            self._log_paro()
        return marcado

    def _dormir_cancelable(self, segundos, tramo: float = 0.5) -> bool:
        """Duerme `segundos` en tramos <= `tramo`, cortando si hay paro total.

        Devuelve True si completo la espera y False si se cancelo. Nunca lanza
        (un valor raro se trata como 0).
        """
        try:
            total = max(0.0, float(segundos or 0))
        except (TypeError, ValueError):
            total = 0.0
        try:
            tramo = max(0.05, float(tramo))
        except (TypeError, ValueError):
            tramo = 0.5
        fin = time.monotonic() + total
        while True:
            if self._cancelado():
                return False
            restante = fin - time.monotonic()
            if restante <= 0:
                return True
            time.sleep(min(tramo, restante))

    def _adquirir_navegador(self) -> bool:
        """Reserva un cupo de navegador (espera si el limite esta lleno).

        Devuelve False si se solicito el paro total mientras esperaba cupo (el
        llamador NO debe crear el navegador). Nunca lanza.
        """
        with self._navegadores_cond:
            while self._navegadores_activos >= self._limite_navegadores:
                if self._cancelado():
                    return False
                self._navegadores_cond.wait(timeout=0.5)
            self._navegadores_activos += 1
        return True

    def _liberar_navegador(self) -> None:
        """Devuelve un cupo de navegador (sin negativos; despierta waiters)."""
        with self._navegadores_cond:
            if self._navegadores_activos > 0:
                self._navegadores_activos -= 1
            self._navegadores_cond.notify_all()

    def _reducir_navegadores(self, motivo: str = "") -> int:
        """Reduce el limite de navegadores a la mitad (minimo 1).

        Se llama con fallos de RECURSOS ("can't start new thread", "cannot
        connect to chrome", tab crashed...): marcar `_recursos_agotados` evita
        reintentos en cascada. Loguea WARNING. Devuelve el limite nuevo. Nunca
        lanza.
        """
        try:
            with self._navegadores_cond:
                previo = self._limite_navegadores
                nuevo = max(1, int(previo) // 2)
                self._recursos_agotados = time.monotonic()
                if nuevo < previo:
                    self._limite_navegadores = nuevo
                self._navegadores_cond.notify_all()
        except Exception:
            return 1
        if nuevo < previo:
            # El pool de pestañas tambien respeta el limite nuevo: cerrar las
            # pestañas LIBRES sobrantes (las ocupadas se descartan al liberar).
            self._cerrar_pestanas_libres()
        texto = str(motivo or "")[:120]
        if nuevo < previo:
            logger.warning(
                f"Activacion: recursos agotados ({texto}); navegadores "
                f"{previo} -> {nuevo} (sin reintento en cascada)"
            )
        else:
            logger.warning(
                f"Activacion: recursos agotados ({texto}); navegadores ya en "
                f"el minimo ({previo})"
            )
        return self._limite_navegadores

    def _recursos_recientes(self, ventana: float = None) -> bool:
        """True si hubo agotamiento de recursos hace menos de `ventana` seg.

        Mientras sea True no se reintentan acciones (mas Chrome empeoraria).
        Nunca lanza.
        """
        try:
            marca = self._recursos_agotados
            if not marca:
                return False
            limite = self._VENTANA_RECURSOS_SEG if ventana is None else float(ventana)
            return (time.monotonic() - float(marca)) < limite
        except Exception:
            return False

    def _pestana_capaz(self) -> bool:
        """True si el pool de pestañas persistentes puede usarse.

        Requiere `MODO_PESTANA` activo y que `plataformas.twitter
        .selenium_bot.TwitterBot` implemente el contrato del pool
        (`cambiar_cuenta`, `esta_vivo` e `iniciar_driver(proxy_dinamico=...)`).
        Si el contrato no existe se devuelve False y el motor cae
        automaticamente al camino clasico (un Chrome por accion). Nunca lanza.
        """
        if not self._modo_pestana:
            return False
        try:
            from plataformas.twitter.selenium_bot import TwitterBot
        except Exception:
            return False
        try:
            if getattr(TwitterBot, "cambiar_cuenta", None) is None:
                return False
            if getattr(TwitterBot, "esta_vivo", None) is None:
                return False
            iniciar = getattr(TwitterBot, "iniciar_driver", None)
            if iniciar is None:
                return False
            parametros = inspect.signature(iniciar).parameters
            if "proxy_dinamico" in parametros:
                return True
            return any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in parametros.values()
            )
        except Exception:
            return False

    def _adquirir_pestana(self, cuenta):
        """Toma una pestaña libre del pool (o crea una si hay cupo).

        Devuelve la `_Pestana` marcada como ocupada o None: modo pestaña
        inactivo/contrato ausente, fallo al crear la pestaña o espera agotada
        (`PESTANA_ESPERA_SEG`) sin cupo. Con None el llamador usa el camino
        clasico (navegador por accion). Nunca lanza.
        """
        if not self._pestana_capaz():
            return None
        try:
            from plataformas.twitter.selenium_bot import TwitterBot
        except Exception:
            return None
        try:
            espera_total = max(0.0, float(self._pestana_espera_seg))
        except Exception:
            espera_total = 180.0
        deadline = time.monotonic() + espera_total
        while True:
            # Paro total: no dejar workers esperando cupo hasta 3 minutos.
            if self._cancelado():
                return None
            reserva = None
            with self._pestanas_cond:
                for pestana in self._pestanas:
                    if not getattr(pestana, "ocupada", True):
                        pestana.ocupada = True
                        return pestana
                try:
                    hay_cupo = (
                        len(self._pestanas) < int(self._limite_navegadores)
                    )
                except Exception:
                    hay_cupo = False
                if hay_cupo:
                    # La reserva consume el cupo desde ya: la creacion (lenta)
                    # se hace FUERA del lock para no congelar a los demas.
                    reserva = _Pestana(bot=None, usuario="", ocupada=True)
                    self._pestanas.append(reserva)
                else:
                    restante = deadline - time.monotonic()
                    if restante <= 0:
                        return None
                    self._pestanas_cond.wait(
                        timeout=min(1.0, max(0.05, restante))
                    )
                    continue

            if self._cancelado():
                # Se cancelo entre la reserva y la creacion: devolver el cupo
                # SIN abrir Chrome.
                with self._pestanas_cond:
                    try:
                        self._pestanas.remove(reserva)
                    except ValueError:
                        pass
                    self._pestanas_cond.notify_all()
                return None

            bot = None
            try:
                bot = TwitterBot(cuenta.usuario)
                iniciar = getattr(bot, "iniciar_driver", None)
                ok_driver = (
                    bool(iniciar(proxy_dinamico=True))
                    if callable(iniciar) else False
                )
                if not ok_driver:
                    raise RuntimeError(
                        "iniciar_driver(proxy_dinamico=True) fallo"
                    )
                ok_sesion = False
                try:
                    preparar = getattr(bot, "preparar_sesion_cdp", None)
                    ok_sesion = bool(preparar()) if callable(preparar) else False
                except Exception:
                    ok_sesion = False
                if not ok_sesion:
                    login = getattr(bot, "login_con_cookies", None)
                    ok_sesion = bool(login()) if callable(login) else False
                if not ok_sesion:
                    raise RuntimeError(
                        str(getattr(bot, "ultimo_error", "") or "sin sesion")
                    )
                # Calentamiento UNICO por pestaña recien creada: la PRIMERA
                # navegacion de X puede caer en el interstitial "something went
                # wrong"; `calentar()` lo tolera (refresh unico). NO es
                # requisito para usar la pestaña: si falla o no existe, se usa
                # igual y la accion maneja el interstitial. Jamas se llama al
                # cambiar de cuenta ni al liberar.
                try:
                    calentar = getattr(bot, "calentar", None)
                    if callable(calentar):
                        try:
                            calentar()
                        except Exception as e:
                            logger.debug(
                                f"pestana: calentamiento fallo para "
                                f"@{cuenta.usuario} ({type(e).__name__}: {e})"
                            )
                        else:
                            logger.debug(
                                f"pestana: calentada @{cuenta.usuario}"
                            )
                    else:
                        logger.debug(
                            f"pestana: calentamiento no disponible "
                            f"@{cuenta.usuario}"
                        )
                except Exception:
                    pass
            except Exception as e:
                logger.warning(
                    f"pestana: no se pudo crear para "
                    f"@{getattr(cuenta, 'usuario', '')} "
                    f"({type(e).__name__}: {e}); se usa el camino clasico"
                )
                try:
                    if bot is not None:
                        bot.cerrar()
                except Exception:
                    pass
                with self._pestanas_cond:
                    try:
                        self._pestanas.remove(reserva)
                    except ValueError:
                        pass
                    self._pestanas_cond.notify_all()
                return None
            reserva.bot = bot
            reserva.usuario = str(getattr(cuenta, "usuario", "") or "")
            reserva.acciones = 0
            reserva.ocupada = True
            reserva.creada = time.monotonic()
            with self._pestanas_cond:
                if reserva not in self._pestanas:
                    # El pool se cerro mientras se creaba (campaña terminada o
                    # cancelada): no dejar el Chrome huerfano.
                    self._pestanas_cond.notify_all()
                    crear_ok = False
                else:
                    self._pestanas_creadas += 1
                    self._pestanas_cond.notify_all()
                    crear_ok = True
            if not crear_ok:
                try:
                    bot.cerrar()
                except Exception:
                    pass
                return None
            if self._cancelado():
                # La campana se cancelo mientras se creaba el Chrome: cerrarlo
                # (nunca dejarlo huerfano) y no entregarlo a ningun worker.
                self._descartar_pestana(reserva)
                return None
            return reserva

    def _cambiar_cuenta_pestana(self, pestana, cuenta) -> bool:
        """Cambia la pestaña a `cuenta`; False = driver roto/descartado.

        Si la pestaña ya atiende a esa cuenta devuelve True sin tocar nada. Si
        `bot.cambiar_cuenta` falla, la pestaña se descarta (nunca deja un
        Chrome huerfano) y el llamador cae al camino clasico. Nunca lanza.
        """
        try:
            usuario = str(getattr(cuenta, "usuario", "") or "")
            if str(getattr(pestana, "usuario", "") or "") == usuario:
                return True
            bot = getattr(pestana, "bot", None)
            cambiar = getattr(bot, "cambiar_cuenta", None)
            if not callable(cambiar):
                self._descartar_pestana(pestana)
                return False
            ok = False
            try:
                ok = bool(cambiar(usuario, validar_proxy=False))
            except TypeError:
                try:
                    ok = bool(cambiar(usuario))
                except Exception:
                    ok = False
            except Exception:
                ok = False
            if not ok:
                self._descartar_pestana(pestana)
                return False
            pestana.usuario = usuario
            pestana.acciones = 0
            return True
        except Exception:
            try:
                self._descartar_pestana(pestana)
            except Exception:
                pass
            return False

    def _liberar_pestana(self, pestana, exito: bool) -> None:
        """Devuelve la pestaña al pool (o la recicla si toca).

        Suma una accion; si alcanzo `PESTANA_MAX_ACCIONES`, supero
        `PESTANA_MAX_MINUTOS` de EDAD o el driver murio, descarta la pestaña (y
        la cuenta como reciclada); si no, la marca libre. SIEMPRE despierta a
        los workers que esperan cupo. Nunca lanza.
        """
        reciclar = False
        try:
            with self._pestanas_cond:
                try:
                    pestana.acciones = int(pestana.acciones or 0) + 1
                except Exception:
                    pestana.acciones = 1
                try:
                    if pestana.acciones >= int(self._pestana_max_acciones):
                        reciclar = True
                except Exception:
                    pass
                try:
                    if int(self._pestana_max_minutos) > 0:
                        edad = time.monotonic() - float(
                            getattr(pestana, "creada", time.monotonic())
                        )
                        if edad >= int(self._pestana_max_minutos) * 60:
                            reciclar = True
                except Exception:
                    pass
                try:
                    if not bool(pestana.bot.esta_vivo()):
                        reciclar = True
                except Exception:
                    reciclar = True
                if reciclar:
                    self._pestanas_recicladas += 1
                else:
                    pestana.ocupada = False
                self._pestanas_cond.notify_all()
        except Exception:
            reciclar = True
        if reciclar:
            self._descartar_pestana(pestana)

    def _descartar_pestana(self, pestana) -> None:
        """Saca la pestaña del pool y cierra su Chrome (nunca lanza).

        Es segura de llamar mas de una vez y desde cualquier hilo: si ya no
        esta en el pool solo cierra el bot (idempotente en el contrato).
        """
        bot = None
        try:
            with self._pestanas_cond:
                try:
                    self._pestanas.remove(pestana)
                except ValueError:
                    pass
                pestana.ocupada = True
                self._pestanas_cond.notify_all()
            bot = getattr(pestana, "bot", None)
        except Exception:
            bot = getattr(pestana, "bot", None)
        try:
            if bot is not None:
                bot.cerrar()
        except Exception:
            pass
        try:
            with self._pestanas_cond:
                self._pestanas_cond.notify_all()
        except Exception:
            pass

    def _cerrar_pestanas_libres(self) -> int:
        """Cierra pestañas LIBRES sobrantes para respetar el limite actual.

        Se llama al reducir navegadores por agotamiento de recursos: las
        pestañas ocupadas terminan su accion y se liberan/descartan solas.
        Devuelve cuantas cerro; nunca lanza.
        """
        sobrantes = []
        try:
            with self._pestanas_cond:
                try:
                    limite = int(self._limite_navegadores)
                except Exception:
                    limite = 1
                libres = [p for p in self._pestanas if not p.ocupada]
                while len(self._pestanas) > limite and libres:
                    pestana = libres.pop()
                    try:
                        self._pestanas.remove(pestana)
                    except ValueError:
                        continue
                    sobrantes.append(pestana)
                if sobrantes:
                    self._pestanas_cond.notify_all()
        except Exception:
            sobrantes = []
        for pestana in sobrantes:
            try:
                bot = getattr(pestana, "bot", None)
                if bot is not None:
                    bot.cerrar()
            except Exception:
                pass
        return len(sobrantes)

    def _cerrar_pestanas(self) -> None:
        """Cierra TODAS las pestañas del pool y lo vacia (nunca lanza).

        Se llama en el `finally` de las campañas (`ejecutar`,
        `ejecutar_por_roles` y 3+3+3): ninguna pestaña debe quedar viva al
        terminar, pase lo que pase.
        """
        pestanas = []
        try:
            with self._pestanas_cond:
                pestanas = list(self._pestanas)
                self._pestanas.clear()
                self._pestanas_cond.notify_all()
        except Exception:
            pestanas = []
        for pestana in pestanas:
            try:
                bot = getattr(pestana, "bot", None)
                if bot is not None:
                    bot.cerrar()
            except Exception:
                pass

    def _claves_cuotas(self, resumen) -> dict:
        """Agrega al resumen las claves de cuotas horarias (nunca lanza).

        `omitidas_por_cuota` queda SIEMPRE presente (0 si no hubo omisiones) y
        `cuotas` es el resumen del contador o `{}` sin cuotas/campana.
        """
        if not isinstance(resumen, dict):
            resumen = {"resultado": resumen}
        resumen.setdefault("omitidas_por_cuota", 0)
        try:
            resumen["cuotas"] = (
                self._cuotas.resumen() if self._cuotas is not None else {}
            )
        except Exception:
            resumen["cuotas"] = {}
        return resumen

    def _con_claves_pestana(self, resumen) -> dict:
        """Agrega al resumen las claves del pool de pestañas (nunca lanza)."""
        if not isinstance(resumen, dict):
            resumen = {"resultado": resumen}
        try:
            with self._pestanas_lock:
                creadas = int(self._pestanas_creadas)
                recicladas = int(self._pestanas_recicladas)
        except Exception:
            creadas, recicladas = 0, 0
        try:
            resumen["modo_pestana"] = bool(self._pestana_capaz())
        except Exception:
            resumen["modo_pestana"] = False
        resumen["pestanas_creadas"] = creadas
        resumen["pestanas_recicladas"] = recicladas
        rechazos_x, transitorios = self._contadores_omitidas()
        resumen["rechazos_x"] = rechazos_x
        resumen["transitorios_omitidos"] = transitorios
        try:
            with self._lock:
                resumen["rechazos_x_usuarios"] = sorted(self._rechazos_x)
                resumen["transitorios_omitidos_usuarios"] = sorted(
                    self._transitorios_omitidos
                )
        except Exception:
            resumen["rechazos_x_usuarios"] = []
            resumen["transitorios_omitidos_usuarios"] = []
        if rechazos_x or transitorios:
            logger.info(
                f"Activacion: {rechazos_x} cuenta(s) omitidas por rechazo de "
                f"X y {transitorios} por fallos transitorios de navegacion "
                f"(NINGUNA desactivada en la BD)"
            )
        # Claves SIEMPRE presentes (aunque la campana no haya usado la funcion):
        # blindaje por tier, rotacion de reservas, curva y cascada de URLs.
        resumen.setdefault("tier2_hashtags_omitidas", 0)
        resumen.setdefault("tier2_hashtags_usuarios", [])
        resumen.setdefault("tier2_sin_rol", 0)
        resumen.setdefault("tier2_sin_rol_usuarios", [])
        resumen.setdefault("rotadas_por_cuota_dia", 0)
        resumen.setdefault("agotadas_dia", 0)
        resumen.setdefault("reserva_usada", 0)
        resumen.setdefault("reserva_disponible", 0)
        resumen.setdefault("curva_aceleracion", False)
        resumen.setdefault("curva_fase1_min", 0)
        resumen.setdefault("omitidas_por_cancelacion", 0)
        resumen.setdefault("fase_actual", int(self.progreso.get("fase_actual", 1) or 1))
        try:
            resumen["cascada_urls"] = len(self._cascada_usadas)
        except Exception:
            resumen["cascada_urls"] = 0
        # PARO TOTAL: `cancelada` SIEMPRE presente. El resumen interno puede
        # traerla (salida temprana); si no, manda el estado real del motor.
        try:
            cancelada = bool(self._cancelado())
        except Exception:
            cancelada = False
        resumen["cancelada"] = bool(resumen.get("cancelada", False)) or cancelada
        if resumen["cancelada"]:
            try:
                exitosas = int(resumen.get("exitosas", 0) or 0)
                fallidas = int(resumen.get("fallidas", 0) or 0)
                omitidas = int(resumen.get("omitidas_por_cancelacion", 0) or 0)
                omitidas += int(resumen.get("omitidas_por_cuota", 0) or 0)
            except Exception:
                exitosas, fallidas, omitidas = 0, 0, 0
            logger.info(
                f"Activacion: campaña cancelada por el usuario; {exitosas} "
                f"exitosas, {fallidas} fallidas, {omitidas} omitidas"
            )
        return resumen

    def _n_workers(self) -> int:
        """Trabajadores del pool de rondas: env `MAX_WORKERS` acotada a [1, 32].

        Default `max(12, max_browsers)`: las acciones API corren en paralelo
        sin Chrome (~1-2s) y los fallbacks quedan limitados por el gate de
        navegadores. Un valor raro en la env se ignora (nunca lanza) y el
        valor de env jamas pasa de 32 para no explotar hilos.
        """
        try:
            valor = os.environ.get("MAX_WORKERS")
            if valor is not None and str(valor).strip():
                return max(1, min(32, int(float(str(valor).strip()))))
        except Exception:
            pass
        try:
            return max(12, int(self.max_concurrente or 1))
        except Exception:
            return 12

    def _preparar_cuotas(self, usuarios) -> None:
        """Prepara el contador de cuotas horarias de la campana (nunca lanza).

        Import perezoso de `activaciones.cuotas.CuotasHorarias` (capa nueva):
        crea el contador, carga el conteo base desde la BD con
        `preparar(usuarios)` (una sola consulta agrupada) y lo asigna a
        `self._cuotas`. Ante cualquier excepcion registra un WARNING y deja
        `self._cuotas = None`: la campana sigue sin limites, como antes.
        """
        try:
            from activaciones.cuotas import CuotasHorarias

            cuotas = CuotasHorarias()
            cuotas.preparar(
                [str(u) for u in (usuarios or []) if str(u or "").strip()]
            )
            self._cuotas = cuotas
        except Exception as e:
            logger.warning(
                f"Activacion: no se pudieron preparar las cuotas horarias "
                f"({type(e).__name__}: {e}); se sigue sin limites"
            )
            self._cuotas = None

    # ------------------------------------------------------------------ #
    # Tiers + cuota diaria + curva + reservas (capa nueva)
    # ------------------------------------------------------------------ #
    def _reset_curva_campana(self) -> None:
        """Reinicia el estado de curva/cascada/tiers de una campana nueva.

        Se llama al inicio de cada campana (antes de usar cualquier dato de la
        anterior): `_urls_fase1`, `_cascada_usadas`, `_tier_de` y la fase.
        Nunca lanza.
        """
        try:
            self._curva_activa = False
            self._curva_fase1_seg = 0.0
            self._curva_fase2_t0 = None
            with self._lock:
                self._urls_fase1 = []
                self._cascada_usadas = set()
                self._tier_de = {}
                self._tier2_filtradas_rol = []
                self.progreso["fase_actual"] = 1
        except Exception:
            pass

    def _registrar_tiers(self, cuentas) -> None:
        """Guarda el tier de cada cuenta de la campana (`_tier_de`)."""
        for cuenta in (cuentas or []):
            try:
                usuario = str(getattr(cuenta, "usuario", "") or "").strip()
                if usuario:
                    self._tier_de[usuario] = _tier_cuenta(cuenta)
            except Exception:
                pass

    def _configurar_curva(self, cuentas, curva_aceleracion, curva_fase1_min,
                          duracion_min, resumen) -> tuple:
        """Activa la curva de aceleracion si la campana tiene Tier 1.

        Reglas:
        - Sin `curva_aceleracion` la curva queda desactivada (comportamiento
          normal). `curva_fase1_min` None -> env `CURVA_FASE1_MIN` (default 15).
        - Sin NINGUNA cuenta Tier 1 en la campana: se desactiva con WARNING y
          la campana sigue normal (no se queda vacia).
        - Si `duracion_min <= fase1_min`: fase1 = max(1, duracion_min // 3)
          con WARNING.

        Devuelve `(activa, fase1_min)` y escribe `curva_aceleracion` /
        `curva_fase1_min` en `resumen`. Nunca lanza.
        """
        try:
            fase1 = curva_fase1_min
            if fase1 is None:
                fase1 = _env_int("CURVA_FASE1_MIN", 15, minimo=1)
            fase1 = max(1, int(fase1))
        except Exception:
            fase1 = 15
        try:
            duracion = max(1, int(duracion_min or 0))
        except Exception:
            duracion = 1
        try:
            resumen["curva_aceleracion"] = bool(curva_aceleracion)
            resumen["curva_fase1_min"] = fase1 if curva_aceleracion else 0
        except Exception:
            pass
        if not curva_aceleracion:
            return False, fase1
        try:
            tier1 = [c for c in (cuentas or []) if _tier_cuenta(c) == "tier1"]
        except Exception:
            tier1 = []
        if not tier1:
            logger.warning(
                "Activacion: curva de aceleracion solicitada pero la campana "
                "no tiene cuentas Tier 1; se DESACTIVA y la campana sigue en "
                "modo normal (ninguna cuenta Tier 2 se queda fuera)"
            )
            try:
                resumen["curva_aceleracion"] = False
                resumen["curva_fase1_min"] = 0
            except Exception:
                pass
            return False, fase1
        if duracion <= fase1:
            nuevo = max(1, duracion // 3)
            logger.warning(
                f"Activacion: duracion ({duracion} min) <= fase 1 "
                f"({fase1} min); se ajusta la fase 1 a {nuevo} min"
            )
            fase1 = nuevo
        try:
            self._curva_activa = True
            self._curva_fase1_seg = float(fase1) * 60.0
            self._curva_fase2_t0 = time.monotonic() + self._curva_fase1_seg
            resumen["curva_aceleracion"] = True
            resumen["curva_fase1_min"] = fase1
        except Exception:
            self._curva_activa = False
            return False, fase1
        logger.info(
            f"Activacion: curva de aceleracion activa (fase 1 solo Tier 1 "
            f"durante {fase1} min; fase 2 despues)"
        )
        return True, fase1

    def _elegibilidad_curva(self, activa: bool):
        """Callable `usuario -> bool` del GATE de fase (None si no hay curva).

        Se evalua al INICIAR CADA RONDA: en fase 1 solo cuentas Tier 1; al
        pasar `curva_fase1_min` minutos todas son elegibles (fase 2). Actualiza
        `progreso["fase_actual"]` en cada evaluacion. Nunca lanza.
        """
        if not activa:
            return None

        def _elegible(usuario) -> bool:
            try:
                fase = (
                    2
                    if time.monotonic() >= float(self._curva_fase2_t0)
                    else 1
                )
            except Exception:
                fase = 2
            try:
                with self._lock:
                    self.progreso["fase_actual"] = fase
            except Exception:
                pass
            if fase == 1:
                return self._tier_de.get(str(usuario)) == "tier1"
            return True

        return _elegible

    def _en_fase2(self) -> bool:
        """True si la curva esta activa y ya empezo la fase 2. Nunca lanza."""
        try:
            if not self._curva_activa or self._curva_fase2_t0 is None:
                return False
            return time.monotonic() >= float(self._curva_fase2_t0)
        except Exception:
            return False

    def _urls_efectivas_rol(self, rol, urls):
        """URLs objetivo de un rol: cascada de fase 1 si esta disponible.

        En fase 2, para `rt`/`cita`/`comentario`, si hay URLs publicadas por
        Tier 1 en fase 1 (`self._urls_fase1`) se usan como objetivo; si no, se
        cae a las URLs de la campana. Nunca lanza.
        """
        try:
            if rol not in ("rt", "cita", "comentario"):
                return urls
            if not self._en_fase2():
                return urls
            with self._lock:
                fase1 = list(self._urls_fase1)
            return fase1 if fase1 else urls
        except Exception:
            return urls

    def _capturar_url_fase1(self, usuario, rol, url) -> None:
        """Guarda una URL publicada EXITOSAMENTE por Tier 1 en fase 1.

        Solo roles `hashtags`/`post` (posts originales), URLs no vacias y que
        no sean el PERFIL del propio usuario (los RT devuelven
        `https://twitter.com/<usuario>`, sin `/status/`); cap de 50. Nunca
        lanza.
        """
        try:
            rol_norm = "hashtags" if str(rol or "") == "post" else str(rol or "")
            if rol_norm != "hashtags":
                return
            if self._tier_de.get(str(usuario or "")) != "tier1":
                return
            url = str(url or "").strip()
            if not url:
                return
            usuario_txt = str(usuario or "").strip().lower()
            url_limpia = url.rstrip("/").lower()
            if usuario_txt and url_limpia in (
                f"https://twitter.com/{usuario_txt}",
                f"https://x.com/{usuario_txt}",
            ):
                return
            with self._lock:
                if url not in self._urls_fase1 and len(self._urls_fase1) < 50:
                    self._urls_fase1.append(url)
        except Exception:
            pass

    def _marcar_cascada(self, url) -> None:
        """Anota que una URL de fase 1 se uso como objetivo (fase 2)."""
        try:
            url = str(url or "").strip()
            if not url or not self._en_fase2():
                return
            with self._lock:
                if url in self._urls_fase1:
                    self._cascada_usadas.add(url)
        except Exception:
            pass

    def _preparar_reservas(self, reserva_usuarios, principales, secciones=None,
                           solo_con_registro: bool = False) -> list:
        """Carga las cuentas de RESPALDO de la campana (sin abrir navegador).

        Acepta usuarios (str, con o sin '@') o Cuentas ya construidas. Filtra
        activas y con sesion (helpers existentes), quita duplicadas y las que
        ya estan en la campana, respeta `solo_con_registro` y el tope
        `RESERVA_MAX_CUENTAS` (env, default 50). `None`/vacio = comportamiento
        actual exacto (sin reservas). Nunca lanza.
        """
        try:
            if not reserva_usuarios:
                return []
            if isinstance(reserva_usuarios, str):
                lista = [reserva_usuarios]
            else:
                lista = list(reserva_usuarios)
        except Exception:
            return []
        directas, nombres = [], []
        for entrada in lista:
            try:
                if isinstance(entrada, str):
                    texto = entrada.strip().lstrip("@")
                    if texto:
                        nombres.append(texto)
                elif entrada is not None:
                    try:
                        if getattr(entrada, "activa", True) is False:
                            continue
                    except Exception:
                        pass
                    directas.append(entrada)
            except Exception:
                continue
        cargadas: list = []
        if nombres:
            try:
                cargadas = self._obtener_cuentas_por_rol(
                    usuarios=nombres, secciones=secciones
                )
            except Exception:
                cargadas = []
        ya_en_campana = set()
        for cuenta in (principales or []):
            try:
                ya_en_campana.add(
                    str(getattr(cuenta, "usuario", "") or "").strip().lower()
                )
            except Exception:
                pass
        reservas, vistas = [], set()
        for cuenta in list(directas) + list(cargadas or []):
            try:
                usuario = str(getattr(cuenta, "usuario", "") or "").strip()
            except Exception:
                continue
            clave = usuario.lower()
            if not usuario or clave in ya_en_campana or clave in vistas:
                continue
            vistas.add(clave)
            reservas.append(cuenta)
        con_sesion, _sin = _partir_por_sesion(reservas)
        if solo_con_registro:
            con_sesion, _sin_reg = _partir_por_registro(con_sesion)
        tope = _env_int("RESERVA_MAX_CUENTAS", 50, minimo=0)
        return con_sesion[:tope]

    def _rotar_agotadas_dia(self, activos, reservas, rol_de,
                            roles_aleatorios: bool, resumen,
                            grupos_ejec=None) -> int:
        """Retira cuentas "Agotadas por hoy" y las sustituye 1:1 por reservas.

        - `activos`: lista MUTABLE en la que participan las cuentas (se retiran
          y se agregan las sustitutas).
        - `reservas`: lista MUTABLE de Cuentas de respaldo aun sin usar.
        - `rol_de`: {usuario: rol} de modo fijo (la reserva toma ESE rol); en
          modo aleatorio se ignora (la reserva entra libre al sorteo).
        - Una reserva Tier 2 JAMAS sustituye un rol hashtags: se busca otra
          reserva y, si no hay, la cuenta agotada queda retirada.
        - `grupos_ejec`: grupos por rol de modo fijo (se actualizan con la
          sustituta y se quita la cuenta retirada).

        Suma `agotadas_dia` (cuentas retiradas) y `rotadas_por_cuota_dia`
        (sustituciones 1:1 completadas) y refresca `reserva_usada` /
        `reserva_disponible`. Devuelve cuantas sustituciones hizo; nunca lanza.
        """
        sustituciones = 0
        try:
            cuotas = self._cuotas
            if cuotas is None or not activos:
                return 0
            for cuenta in list(activos):
                try:
                    usuario = str(getattr(cuenta, "usuario", "") or "")
                    if not usuario or not cuotas.agotado_dia(usuario):
                        continue
                    rol_requerido = (
                        "" if roles_aleatorios else str(rol_de.get(usuario, "") or "")
                    )
                    sustituta = None
                    for i, reserva in enumerate(list(reservas)):
                        try:
                            permitida = roles_aleatorios or _tier_permitido(
                                reserva, rol_requerido or ""
                            )
                        except Exception:
                            permitida = roles_aleatorios
                        if not permitida:
                            continue
                        try:
                            reservas.remove(reserva)
                        except ValueError:
                            continue
                        sustituta = reserva
                        break
                    try:
                        activos.remove(cuenta)
                    except ValueError:
                        pass
                    if not roles_aleatorios and grupos_ejec is not None:
                        try:
                            grupos_ejec.get(rol_requerido, []).remove(cuenta)
                        except Exception:
                            pass
                        rol_de.pop(usuario, None)
                    self._tier_de.pop(usuario, None)
                    resumen["agotadas_dia"] = int(
                        resumen.get("agotadas_dia", 0) or 0
                    ) + 1
                    if sustituta is None:
                        logger.info(
                            f"Activacion: @{usuario} agotada por hoy; se retira "
                            f"sin sustituta (no hay reserva con rol compatible)"
                        )
                        continue
                    activos.append(sustituta)
                    nuevo_usuario = str(
                        getattr(sustituta, "usuario", "") or ""
                    )
                    if not roles_aleatorios:
                        if grupos_ejec is not None and rol_requerido in grupos_ejec:
                            grupos_ejec[rol_requerido].append(sustituta)
                        if rol_requerido:
                            rol_de[nuevo_usuario] = rol_requerido
                    self._tier_de[nuevo_usuario] = _tier_cuenta(sustituta)
                    sustituciones += 1
                    resumen["rotadas_por_cuota_dia"] = int(
                        resumen.get("rotadas_por_cuota_dia", 0) or 0
                    ) + 1
                    logger.info(
                        f"Activacion: @{usuario} agotada por hoy; entra la "
                        f"reserva @{nuevo_usuario}"
                        + (f" (rol {rol_requerido})" if rol_requerido else "")
                    )
                except Exception as e:
                    logger.warning(
                        f"Activacion: no se pudo rotar la cuenta agotada: "
                        f"{type(e).__name__}: {e}"
                    )
        except Exception as e:
            logger.warning(
                f"Activacion: rotacion de reservas fallo "
                f"({type(e).__name__}: {e})"
            )
        finally:
            try:
                resumen["reserva_disponible"] = len(reservas or [])
                resumen["reserva_usada"] = int(
                    resumen.get("reserva_usada", 0) or 0
                ) + sustituciones
            except Exception:
                pass
        return sustituciones

    def _contar_tier2_sin_rol(self, usuario, resumen) -> None:
        """Suma (una sola vez) una cuenta Tier 2 sin rol permitido. Nunca lanza."""
        try:
            usuario = str(usuario or "").strip()
            if not usuario:
                return
            with self._lock:
                usuarios = resumen.setdefault("tier2_sin_rol_usuarios", [])
                if not isinstance(usuarios, list):
                    usuarios = []
                    resumen["tier2_sin_rol_usuarios"] = usuarios
                if usuario in usuarios:
                    return
                usuarios.append(usuario)
                resumen["tier2_sin_rol"] = int(
                    resumen.get("tier2_sin_rol", 0) or 0
                ) + 1
        except Exception:
            pass

    def _loguear_rendimiento(self, inicio, resumen) -> None:
        """INFO de `acciones/min` al terminar y RAM max del proceso si aplica.

        `acciones` = exitosas + fallidas (las omitidas no son trabajo real).
        La RAM solo se reporta si `resource` esta disponible (Railway/Linux).
        Nunca lanza.
        """
        try:
            segundos = max(0.001, float(time.monotonic()) - float(inicio))
            hechas = (
                int((resumen or {}).get("exitosas", 0) or 0)
                + int((resumen or {}).get("fallidas", 0) or 0)
            )
            logger.info(
                f"Activacion: {hechas} acciones en {segundos / 60.0:.1f} min "
                f"({hechas / segundos * 60.0:.1f} acciones/min)"
            )
            ram = _ram_max_mb()
            if ram >= 0:
                logger.info(f"Activacion: RAM max del proceso {ram} MB")
        except Exception:
            pass

    def _avisar_cuota_agotada(self, usuario) -> bool:
        """Avisa (throttle de 1/min por cuenta) que la cuenta agoto sus cuotas.

        Usa el helper de modulo `_avisar_cuota_agotada_log`, compartido con el
        metodo estatico `_asignar_roles_aleatorios` (que no tiene `self`).
        Nunca lanza.
        """
        return _avisar_cuota_agotada_log(self._cuotas, usuario)

    def _marcar_sesion_caida(self, usuario) -> None:
        """Anota la cuenta como 'sesion caida' para las rondas siguientes."""
        try:
            with self._lock:
                self._sesiones_caidas.add(str(usuario))
        except Exception:
            pass

    def _sesion_caida(self, usuario) -> bool:
        """True si la cuenta ya NO debe participar en esta campana.

        Incluye la sesion caida real, el rechazo definitivo de X y las cuentas
        que agotaron el maximo de fallos transitorios de navegacion.
        """
        try:
            clave = str(usuario)
            with self._lock:
                return (
                    clave in self._sesiones_caidas
                    or clave in self._rechazos_x
                    or clave in self._transitorios_omitidos
                )
        except Exception:
            return False

    def _registrar_sesion_caida(self, usuario, detalle) -> None:
        """Aplica la politica de fallos al cerrar una accion (nunca lanza).

        - Rechazo DEFINITIVO de X ("may not be allowed", "cuenta limitada"):
          `_registrar_rechazo_x` omite la cuenta el resto de la campana SIN
          tocar la BD.
        - Pagina de error/interstitial TRANSITORIO de X: `+1` en el contador de
          la cuenta; SIGUE participando en las rondas hasta superar
          `_MAX_FALLOS_TRANSITORIOS`.
        - Sesion caida real (cookies vencidas, login fallido, muro de login) y
          anti-bot DURO/Cloudflare: la cuenta se omite el resto de la campana
          (log claro; NUNCA se desactiva en la BD).
        - Cualquier otro detalle no se registra.
        """
        if _es_rechazo_x(detalle):
            self._registrar_rechazo_x(usuario, detalle)
            return
        if not _es_sesion_caida_detalle(detalle):
            return
        if _es_fallo_navegacion_transitorio(detalle) and not _es_anti_bot_bloqueo(
            detalle
        ):
            self._registrar_fallo_transitorio(usuario, detalle)
            return
        self._marcar_sesion_caida(usuario)
        if _es_anti_bot_bloqueo(detalle):
            logger.warning(
                f"X pidió verificación anti-bot; se omite @{usuario} en esta "
                f"campaña (NO se desactiva)"
            )
        else:
            logger.debug(
                f"sesion caida registrada para @{usuario}; se omite en las "
                f"siguientes rondas"
            )

    def _registrar_fallo_transitorio(self, usuario, detalle="") -> None:
        """Suma un fallo TRANSITORIO de navegacion a la cuenta (nunca lanza).

        La cuenta SIGUE participando en las rondas; al alcanzar
        `_MAX_FALLOS_TRANSITORIOS` se agrega a `_transitorios_omitidos` (misma
        omision en memoria que la sesion caida) con un log claro. No toca la
        BD.
        """
        try:
            clave = str(usuario)
            with self._lock:
                fallos = int(self._fallos_transitorios.get(clave, 0)) + 1
                self._fallos_transitorios[clave] = fallos
                if fallos >= _MAX_FALLOS_TRANSITORIOS:
                    self._transitorios_omitidos.add(clave)
                    omitida = True
                else:
                    omitida = False
            if omitida:
                logger.warning(
                    f"X devolvió {fallos} páginas de error/interstitial a "
                    f"@{clave}; se omite el resto de la campaña (NO se "
                    f"desactiva)"
                )
            else:
                logger.info(
                    f"Página de error/interstitial transitoria en @{clave} "
                    f"({fallos}/{_MAX_FALLOS_TRANSITORIOS}); sigue en las "
                    f"rondas siguientes"
                )
        except Exception:
            pass

    def _registrar_rechazo_x(self, usuario, detalle="") -> None:
        """Omite la cuenta por RECHAZO definitivo de X (nunca lanza).

        Mismo mecanismo en memoria que `_marcar_sesion_caida`; JAMAS toca la
        BD (`marcar_cuenta_suspendida` no se llama para estas senales).
        """
        try:
            clave = str(usuario)
            with self._lock:
                ya_marcada = clave in self._rechazos_x
                self._rechazos_x.add(clave)
            if not ya_marcada:
                logger.warning(
                    f"X bloqueó la acción para @{clave}; se omite el resto de "
                    f"la campaña (NO se desactiva)"
                )
        except Exception:
            pass

    def _contadores_omitidas(self) -> tuple:
        """(rechazos de X, transitorios omitidos) de esta campana (nunca lanza)."""
        try:
            with self._lock:
                return len(self._rechazos_x), len(self._transitorios_omitidos)
        except Exception:
            return 0, 0

    def _registrar_evento_locked(self, usuario, ok, detalle, ronda=1, rol="",
                                 url="") -> None:
        """Agrega un evento al progreso; REQUIERE `self._lock` ya tomado."""
        try:
            eventos = self.progreso.get("eventos")
            if not isinstance(eventos, list):
                eventos = []
                self.progreso["eventos"] = eventos
            try:
                numero = int(ronda or 1)
            except (TypeError, ValueError):
                numero = 1
            eventos.append({
                "usuario": str(usuario or ""),
                "ok": bool(ok),
                "detalle": str(detalle or ""),
                "ronda": numero,
                "rol": str(rol or ""),
                "url": str(url or ""),
            })
            if len(eventos) > 100:
                del eventos[:-100]
        except Exception:
            pass

    def snapshot_progreso(self) -> dict:
        """Copia thread-safe del progreso en vivo para la UI.

        Devuelve un dict con hechas/exitosas/fallidas, la ronda actual y los
        ultimos eventos (el mas reciente al final). La lista de eventos es una
        copia: mutarla no altera el progreso real.
        """
        with self._lock:
            eventos = self.progreso.get("eventos")
            return {
                "hechas": self.progreso.get("hechas", 0),
                "exitosas": self.progreso.get("exitosas", 0),
                "fallidas": self.progreso.get("fallidas", 0),
                "omitidas": self.progreso.get("omitidas", 0),
                "ronda_actual": self.progreso.get("ronda_actual", 1),
                "fase_actual": self.progreso.get("fase_actual", 1),
                "cancelada": bool(self._cancelado()),
                "eventos": list(eventos) if isinstance(eventos, list) else [],
            }

    def _obtener_cuentas(self, cantidad: int = None, tags: list[str] = None,
                         grupo: str = None, secciones=None) -> list[Cuenta]:
        with get_db_session() as db:
            query = db.query(Cuenta).filter(
                Cuenta.plataforma == "twitter",
                Cuenta.activa == True,
            )
            if grupo:
                query = query.filter(Cuenta.grupo == grupo)
            cuentas = query.all()

        if tags:
            cuentas = [c for c in cuentas if all(
                t.upper() in [x.strip().upper() for x in (c.tags or "").split(",") if x.strip()]
                for t in tags
            )]

        if cantidad:
            cuentas = cuentas[:cantidad]

        cuentas = _filtrar_por_seccion(cuentas, secciones)

        return cuentas

    def _obtener_cuentas_por_rol(self, usuarios: list | None = None,
                                 solo_roles: list | None = None,
                                 secciones=None) -> list[Cuenta]:
        """Cuentas twitter activas filtradas por usuarios y/o roles.

        Reutiliza `_obtener_cuentas` (sin modificarlo) y agrega los filtros:
        - usuarios: limita a esos nombres de usuario (ignora '@' y mayusculas).
        - solo_roles: limita a los roles normalizados indicados (core/roles.py).
        - secciones: pasa el filtro de seccion a `_obtener_cuentas`.
        """
        cuentas = self._obtener_cuentas(secciones=secciones)

        if usuarios:
            deseados = {
                str(u).strip().lstrip("@").lower()
                for u in usuarios
                if str(u).strip()
            }
            cuentas = [
                c for c in cuentas
                if (c.usuario or "").strip().lstrip("@").lower() in deseados
            ]

        if solo_roles:
            permitidos = {normalizar_rol_activacion(r) for r in solo_roles}
            permitidos.discard("")
            if not permitidos:
                return []
            finales, filtradas = [], []
            for c in cuentas:
                try:
                    # Alias ("post" -> hashtags, "respuesta"/"reply" ->
                    # comentario) para que el filtro tambien cubra data vieja.
                    rol_c = _rol_efectivo_cuenta(c)
                except Exception:
                    rol_c = ""
                if rol_c not in permitidos:
                    continue
                # Defensa extra: Tier 2 jamas entra en un filtro de hashtags
                # (aunque su rol guardado lo diga).
                if _tier_permitido(c, rol_c):
                    finales.append(c)
                else:
                    filtradas.append(str(c.usuario))
            cuentas = finales
            if filtradas:
                try:
                    actuales = list(getattr(self, "_tier2_filtradas_rol", []) or [])
                    self._tier2_filtradas_rol = sorted(
                        set(actuales) | set(filtradas)
                    )
                except Exception:
                    pass
                logger.warning(
                    f"Activacion: {len(filtradas)} cuenta(s) Tier 2 con rol "
                    f"prohibido ({'/'.join(filtradas[:5])}...) quedan fuera "
                    f"del filtro por rol (NO se desactivan en la BD)"
                )

        return cuentas

    def _distribuir_cohortes(self, cuentas: list[Cuenta], duracion_min: int,
                             cohortes: int) -> list[list[Cuenta]]:
        """Reparte las cuentas en 'cohortes' grupos con arranque escalonado
        a lo largo de la duracion total."""
        random.shuffle(cuentas)
        bloques = [[] for _ in range(cohortes)]
        for i, cuenta in enumerate(cuentas):
            bloques[i % cohortes].append(cuenta)
        return bloques

    def _pool_hashtags(self, texto_base: str, hashtags: str,
                       menciones: str, cantidad: int) -> list[str]:
        """Arma 'cantidad' textos combinando texto_base + hashtags + menciones.

        - Acepta hashtags separados por espacios o comas (con o sin '#').
        - Acepta menciones como "@a @b" o "a,b" (con o sin '@').
        - Varía el subconjunto y el orden de hashtags/menciones por cuenta y
          aplica variaciones locales al texto base para no repetir contenido.
        - Siempre devuelve exactamente 'cantidad' textos (rellena con sufijo
          numerado si no alcanza la variedad).
        """
        if cantidad <= 0:
            return []

        tags = _normalizar_hashtags(hashtags)
        menciones_norm = _normalizar_menciones(menciones)
        base = (texto_base or "").strip()

        if not base and not tags and not menciones_norm:
            return [""] * cantidad

        pool, vistos = [], set()
        intentos = 0
        max_intentos = max(cantidad * 25, 50)
        while len(pool) < cantidad and intentos < max_intentos:
            intentos += 1
            partes = []
            if base:
                # El primer texto conserva el original; el resto se varía.
                if not pool:
                    partes.append(base)
                else:
                    partes.append(variar_texto(base, n_hashtags=0))
            if tags:
                k = random.randint(1, len(tags))
                seleccion = random.sample(tags, k)
                random.shuffle(seleccion)
                partes.append(" ".join(seleccion))
            if menciones_norm:
                k = random.randint(1, len(menciones_norm))
                seleccion = random.sample(menciones_norm, k)
                random.shuffle(seleccion)
                partes.append(" ".join(seleccion))
            texto = "\n\n".join(p for p in partes if p).strip()
            if texto:
                # Deja SOLO un subconjunto aleatorio de los hashtags pedidos
                # (quita los ajenos que vengan en el texto base).
                texto = _garantizar_hashtags_texto(texto, tags)
            if texto and texto not in vistos:
                vistos.add(texto)
                pool.append(texto)

        sufijo = 1
        while len(pool) < cantidad:
            base_txt = pool[-1] if pool else (
                base or " ".join(tags + menciones_norm)
            )
            pool.append(f"{base_txt} ({sufijo})")
            sufijo += 1
        return pool[:cantidad]

    @staticmethod
    def _asignar_roles_aleatorios(cuentas: list, disponibles: list,
                                  roles_previos: dict | None = None,
                                  cuotas=None) -> dict:
        """Sortea un rol de `disponibles` para cada cuenta de `cuentas`.

        Devuelve `{usuario: rol}`. Con `roles_previos` ({usuario: rol}) la
        cuenta recibe un rol DISTINTO al de su participacion anterior cuando
        hay mas de una opcion; si `disponibles` solo trae un rol (o ninguno),
        se usa la lista completa como fallback y puede repetirlo. Con
        `disponibles` vacio deja "" (defensivo, el llamador ya valida que haya
        al menos uno).

        `cuotas` (opcional, `CuotasHorarias`): las opciones de cada cuenta se
        reducen a las que aun tienen cupo (`cuotas.viables`); si ninguna
        queda, la cuenta recibe "" (sin accion) y se avisa con
        `cuotas.avisar_agotada` (helper de modulo, este metodo es estatico).
        Con `cuotas=None` el comportamiento es EXACTO al de antes.

        TIER: a una cuenta Tier 2 se le quitan SIEMPRE las opciones prohibidas
        (`hashtags`/`post`/...); si se queda sin opciones, recibe "" SIN aviso
        de cuota (el llamador la cuenta en `tier2_sin_rol`). Nunca lanza.
        """
        roles = {}
        previos = roles_previos if isinstance(roles_previos, dict) else {}
        try:
            opciones = list(disponibles or [])
        except TypeError:
            opciones = []
        for cuenta in (cuentas or []):
            try:
                usuario = cuenta.usuario
                # Tier 2 jamas recibe un rol prohibido (hashtags/post/...).
                opciones_cuenta = [
                    r for r in opciones if _tier_permitido(cuenta, r)
                ]
                if cuotas is not None:
                    viables = cuotas.viables(usuario, opciones_cuenta)
                    if opciones_cuenta and not viables:
                        # La opcion existe a nivel tier pero no hay cupo.
                        _avisar_cuota_agotada_log(cuotas, usuario)
                    opciones_cuenta = viables
                if not opciones_cuenta:
                    roles[usuario] = ""
                    continue
                previo = previos.get(usuario)
                candidatos = [
                    r for r in opciones_cuenta if r != previo
                ] or opciones_cuenta
                roles[usuario] = random.choice(candidatos) if candidatos else ""
            except Exception:
                try:
                    roles[cuenta.usuario] = ""
                except Exception:
                    pass
        return roles

    @staticmethod
    def _grupos_desde_roles(cuentas: list, roles_por_usuario: dict) -> dict:
        """Agrupa cuentas por el rol REAL sorteado (ignora el rol guardado)."""
        grupos = {"cita": [], "hashtags": [], "comentario": [], "rt": []}
        for cuenta in (cuentas or []):
            rol = roles_por_usuario.get(cuenta.usuario, "")
            if rol in grupos:
                grupos[rol].append(cuenta)
        return grupos

    def _repartir_anclas(self, cuentas: list, ancla_textos) -> dict:
        """Asigna a cada cuenta de comentario el texto de UN tweet ancla.

        Reparte las URLs CON texto de `ancla_textos` ({url: texto}) entre las
        cuentas (round-robin tras barajar) y devuelve `{usuario: texto del
        ancla}`. Tambien guarda en `self._ancla_por_cuenta` la URL elegida para
        que la ejecucion use ESA misma ancla (el comentario corresponde al
        tweet que su texto describe). Sin anclas con texto devuelve {}.
        Nunca lanza.
        """
        asignadas: dict = {}
        try:
            items = [
                (str(url), str(texto))
                for url, texto in (ancla_textos or {}).items()
                if str(texto or "").strip()
            ]
        except Exception:
            items = []
        if not items:
            return asignadas
        random.shuffle(items)
        try:
            with self._lock:
                for i, cuenta in enumerate(cuentas or []):
                    url, texto = items[i % len(items)]
                    asignadas[cuenta.usuario] = texto
                    self._ancla_por_cuenta[cuenta.usuario] = url
        except Exception:
            pass
        return asignadas

    def _obtener_anclas(self, urls) -> dict:
        """Texto REAL de cada tweet ancla (una descarga por URL, con cache).

        Usa `plataformas.twitter.api_http.obtener_texto_tweet` (sin cuenta; su
        cache en memoria evita repetir la descarga del mismo tweet). Nunca
        lanza: las URLs sin texto quedan como "" y los comentarios caen al
        comportamiento previo (contexto/texto_base).
        """
        anclas: dict = {}
        try:
            from plataformas.twitter.api_http import obtener_texto_tweet
        except Exception as e:
            logger.debug(f"ancla: no se pudo importar obtener_texto_tweet: {e}")
            return anclas
        unicas = []
        for url in urls or []:
            url = str(url or "").strip()
            if url and url not in anclas and url not in unicas:
                unicas.append(url)
        for url in unicas:
            try:
                anclas[url] = str(obtener_texto_tweet(url) or "")
            except Exception:
                anclas[url] = ""
        con_texto = sum(1 for texto in anclas.values() if texto.strip())
        if anclas:
            logger.info(
                f"ancla: {con_texto}/{len(anclas)} tweet(s) ancla con texto "
                f"real para los comentarios"
            )
        return anclas

    def _asignar_variaciones_cita(self, cuentas: list, texto_base: str,
                                  narrativa: str = "",
                                  entrenamiento: str = "",
                                  tags: list = None) -> dict:
        """Asigna a cada cuenta una variacion de cita segun su registro/perfil.

        Agrupa `cuentas` por (registro, perfil) -- "politica"/"activista"/
        "ciudadana" + "formal"/"ciudadano"/"popular", normalizados -- y pide un
        pool a `generar_pool_variaciones_openai` POR GRUPO, para que el texto
        de la cita hable como la cuenta que lo publica.

        `narrativa` es SOLO TRASFONDO: viaja al prompt como referencia interna
        (el propio generador ya lo refuerza) y NUNCA se usa como texto
        publicable ni como base del fallback local. La transformacion por ronda
        (`_narrativa_con_ronda`) la hace el llamador, para no duplicarla.

        Aplica `_garantizar_hashtags_texto` con `tags` y reparte cada pool
        barajado entre las cuentas de su grupo; las cuentas que no alcancen
        texto reciben el fallback `_garantizar_hashtags_texto(texto_base,
        tags)` (comportamiento previo). Nunca lanza: si el pool de un grupo
        falla, sus cuentas usan el fallback. Devuelve {usuario: texto}.
        """
        asignaciones: dict = {}
        tags = list(tags or [])
        grupos: dict = {}
        for cuenta in (cuentas or []):
            try:
                clave = (
                    normalizar_tipo_cuenta(getattr(cuenta, "tipo_cuenta", "")),
                    normalizar_perfil(
                        getattr(cuenta, "perfil_personalidad", "")
                    ),
                )
            except Exception:
                clave = ("", "")
            grupos.setdefault(clave, []).append(cuenta)

        for (registro, perfil), grupo in grupos.items():
            try:
                pool = generar_pool_variaciones_openai(
                    texto_base,
                    cantidad=len(grupo),
                    narrativa=narrativa,
                    entrenamiento=entrenamiento,
                    registro=registro,
                    perfil=perfil,
                    hashtags=tags,
                )
            except Exception as e:
                logger.error(
                    f"Activacion: pool de citas fallo para "
                    f"({registro or 'libre'}/{perfil or 'libre'}) "
                    f"({type(e).__name__}: {e}); se usa el texto base"
                )
                pool = []
            if not isinstance(pool, list):
                pool = []
            if tags:
                pool = [_garantizar_hashtags_texto(t, tags) for t in pool]
            random.shuffle(pool)
            for i, cuenta in enumerate(grupo):
                if i < len(pool):
                    asignaciones[cuenta.usuario] = pool[i]
                else:
                    asignaciones[cuenta.usuario] = (
                        _garantizar_hashtags_texto(texto_base, tags)
                    )
        return asignaciones

    def _generar_textos_por_rol(self, grupos_ejec: dict, texto_base: str,
                                hashtags: str = "", menciones: str = "",
                                narrativa: str = "",
                                entrenamiento: str = "",
                                contexto: str = "",
                                ronda: int = 1,
                                ancla_textos: dict = None) -> dict:
        """Arma {usuario: texto} para una ronda de la campana por roles.

        - "cita": variaciones OpenAI/fallback del texto base con los hashtags
          pedidos garantizados.
        - "hashtags": posts ORIGINALES por cuenta con IA (registro/perfil);
          si la IA falla o no devuelve texto, rellena con `_pool_hashtags`
          (comportamiento anterior) y agrega las menciones al final.
        - "comentario": respuestas ORIGINALES por cuenta con IA
          (`generar_textos_comentario`, registro/perfil) sobre el tweet ancla.
          Con `ancla_textos` (URL -> texto real del tweet) el comentario se
          genera SOLO con el contenido del ancla al que responde, sin
          narrativa/contexto de campana; sin ancla se mantiene el
          comportamiento previo (contexto/texto_base). Si la IA falla, cae al
          pool de variaciones y, en ultimo caso, a un texto local.
        - "rt": sin texto ("").
        Nunca lanza por la IA: ante cualquier fallo usa el pool de respaldo.
        """
        asignaciones: dict = {}
        tags = _normalizar_hashtags(hashtags)
        narrativa = _narrativa_con_ronda(narrativa, ronda)

        citas = list(grupos_ejec.get("cita") or [])
        if citas:
            # Cada grupo (registro, perfil) recibe SU propio pool de variaciones
            # para que el texto de la cita hable como la cuenta que lo publica.
            asignaciones.update(self._asignar_variaciones_cita(
                citas,
                texto_base,
                narrativa=narrativa,
                entrenamiento=entrenamiento,
                tags=tags,
            ))

        cuentas_hashtags = [
            c
            for c in (grupos_ejec.get("hashtags") or [])
            if _tier_permitido(c, "hashtags")
        ]
        if cuentas_hashtags:
            # Campanas sin tweet ancla: el contexto de noticias raspadas por
            # la IA hace de "texto base" para el fallback local (posts reales
            # en vez de textos vacios cuando no hay texto_base).
            base_hashtags = (texto_base or "").strip() or str(contexto or "").strip()
            textos_ia: dict = {}
            try:
                from ia.generador_contenido import (
                    generar_textos_hashtags_por_cuenta,
                )

                cuentas_info = []
                for cuenta in cuentas_hashtags:
                    cuentas_info.append({
                        "usuario": cuenta.usuario,
                        "registro": normalizar_tipo_cuenta(
                            getattr(cuenta, "tipo_cuenta", "")
                        ),
                        "personalidad": (
                            getattr(cuenta, "personalidad", "") or ""
                        ),
                        "seccion": getattr(cuenta, "seccion", "") or "",
                        "nombre": (
                            getattr(cuenta, "nombre_mostrado", "")
                            or cuenta.usuario
                        ),
                        "perfil": normalizar_perfil(
                            getattr(cuenta, "perfil_personalidad", "")
                        ),
                    })
                resultado_ia = generar_textos_hashtags_por_cuenta(
                    cuentas_info,
                    hashtags=hashtags,
                    contexto=(str(contexto or "").strip() or texto_base),
                    n_por_cuenta=1,
                    narrativa=narrativa,
                    entrenamiento=entrenamiento,
                )
                if not isinstance(resultado_ia, dict):
                    resultado_ia = {}
                for cuenta in cuentas_hashtags:
                    lista = resultado_ia.get(cuenta.usuario) or []
                    if lista:
                        texto_ia = str(lista[0] or "").strip()
                        if texto_ia:
                            textos_ia[cuenta.usuario] = texto_ia
            except Exception as e:
                logger.error(
                    f"Activacion por roles: IA de hashtags fallo "
                    f"({type(e).__name__}: {e}); se usa el pool de respaldo"
                )

            menciones_norm = _normalizar_menciones(menciones)
            faltantes = [
                c for c in cuentas_hashtags if c.usuario not in textos_ia
            ]
            respaldo = []
            if faltantes:
                respaldo = self._pool_hashtags(
                    base_hashtags, hashtags, menciones, len(faltantes)
                )
                random.shuffle(respaldo)
            for i, cuenta in enumerate(faltantes):
                asignaciones[cuenta.usuario] = (
                    respaldo[i] if i < len(respaldo)
                    else (base_hashtags or texto_base)
                )
            for cuenta in cuentas_hashtags:
                if cuenta.usuario in textos_ia:
                    asignaciones[cuenta.usuario] = _agregar_menciones(
                        textos_ia[cuenta.usuario], menciones_norm
                    )

        cuentas_comentario = list(grupos_ejec.get("comentario") or [])
        if cuentas_comentario:
            # Material general de la campana (contexto manual o texto base).
            material_general = (
                str(contexto or "").strip() or str(texto_base or "").strip()
            )
            # Ancla REAL por cuenta (`ancla_textos`: texto del tweet ancla
            # descargado UNA vez por URL): con texto de ancla el comentario
            # habla SOLO de ese tweet, sin narrativa/contexto de campana.
            anclas = self._repartir_anclas(cuentas_comentario, ancla_textos)
            # Se agrupa por material para no multiplicar las llamadas a la IA.
            grupos_material: dict = {}
            for cuenta in cuentas_comentario:
                texto_ancla = anclas.get(cuenta.usuario, "")
                clave = (bool(texto_ancla), texto_ancla or material_general)
                grupos_material.setdefault(clave, []).append(cuenta)

            textos_ia_com: dict = {}
            try:
                from ia.generador_contenido import generar_textos_comentario
            except Exception as e:
                generar_textos_comentario = None
                logger.error(
                    f"Activacion por roles: no se pudo importar la IA de "
                    f"comentarios ({type(e).__name__}: {e})"
                )

            if generar_textos_comentario is not None:
                for (es_ancla, material), grupo in grupos_material.items():
                    cuentas_info = []
                    for cuenta in grupo:
                        cuentas_info.append({
                            "usuario": cuenta.usuario,
                            "registro": normalizar_tipo_cuenta(
                                getattr(cuenta, "tipo_cuenta", "")
                            ),
                            "personalidad": (
                                getattr(cuenta, "personalidad", "") or ""
                            ),
                            "seccion": getattr(cuenta, "seccion", "") or "",
                            "nombre": (
                                getattr(cuenta, "nombre_mostrado", "")
                                or cuenta.usuario
                            ),
                            "perfil": normalizar_perfil(
                                getattr(cuenta, "perfil_personalidad", "")
                            ),
                        })
                    try:
                        if es_ancla:
                            # SOLO el tweet ancla: se pasa su texto real y NO la
                            # narrativa/contexto de campana. `tweet_ancla_texto`
                            # es kwarg de la version nueva de `ia`; con la vieja
                            # (TypeError) el ancla viaja como narrativa.
                            try:
                                resultado_ia = generar_textos_comentario(
                                    cuentas_info,
                                    n_por_cuenta=1,
                                    tweet_ancla_texto=material,
                                    entrenamiento=entrenamiento,
                                )
                            except TypeError:
                                # Version vieja de `ia` sin el kwarg: el texto
                                # del ancla viaja como instruccion (mismo
                                # contrato que usaba el motor antes), sin
                                # narrativa de campana.
                                resultado_ia = generar_textos_comentario(
                                    cuentas_info,
                                    n_por_cuenta=1,
                                    narrativa=(
                                        f"Comenta el tweet ancla sobre: {material}"
                                    ),
                                    entrenamiento=entrenamiento,
                                )
                        else:
                            instruccion = (
                                f"Comenta el tweet ancla sobre: {material}"
                                if material else ""
                            )
                            trasfondo = (
                                "TRASFONDO (solo referencia interna; PROHIBIDO "
                                f"mencionarlo o copiarlo): {narrativa}"
                            ) if narrativa else ""
                            narrativa_com = "\n".join(
                                x for x in [instruccion, trasfondo] if x
                            )
                            resultado_ia = generar_textos_comentario(
                                cuentas_info,
                                n_por_cuenta=1,
                                narrativa=narrativa_com,
                                entrenamiento=entrenamiento,
                            )
                        for i, cuenta in enumerate(grupo):
                            lista = None
                            if isinstance(resultado_ia, dict):
                                lista = resultado_ia.get(cuenta.usuario)
                            elif (
                                isinstance(resultado_ia, (list, tuple))
                                and i < len(resultado_ia)
                            ):
                                lista = resultado_ia[i]
                            if isinstance(lista, str):
                                lista = [lista]
                            if lista:
                                texto_ia = str(lista[0] or "").strip()
                                if texto_ia:
                                    textos_ia_com[cuenta.usuario] = texto_ia
                    except Exception as e:
                        logger.error(
                            f"Activacion por roles: IA de comentarios fallo "
                            f"({type(e).__name__}: {e}); se usa el pool de respaldo"
                        )

            faltantes = [
                c for c in cuentas_comentario
                if c.usuario not in textos_ia_com
            ]
            if faltantes:
                # Pool de respaldo POR MATERIAL: con ancla real se basa en el
                # tweet ancla (sin narrativa de campana); sin ancla se mantiene
                # el comportamiento previo (texto_base/contexto + narrativa).
                grupos_respaldo: dict = {}
                for cuenta in faltantes:
                    texto_ancla = anclas.get(cuenta.usuario, "")
                    clave = (bool(texto_ancla), texto_ancla or material_general)
                    grupos_respaldo.setdefault(clave, []).append(cuenta)
                for (es_ancla, material), grupo in grupos_respaldo.items():
                    respaldo = []
                    if material:
                        try:
                            respaldo = generar_pool_variaciones_openai(
                                material,
                                cantidad=len(grupo),
                                narrativa="" if es_ancla else narrativa,
                                entrenamiento=entrenamiento,
                                hashtags=[],
                            )
                        except Exception:
                            respaldo = []
                    if not isinstance(respaldo, list):
                        respaldo = []
                    random.shuffle(respaldo)
                    for i, cuenta in enumerate(grupo):
                        texto = (
                            str(respaldo[i] or "").strip()
                            if i < len(respaldo) else ""
                        )
                        if not texto:
                            texto = material or " ".join(tags)
                        # Los comentarios/respuestas NUNCA llevan hashtags,
                        # links ni @menciones (senales de "Probable spam").
                        textos_ia_com[cuenta.usuario] = _limpiar_comentario_spam(
                            texto
                        )
            for cuenta in cuentas_comentario:
                asignaciones[cuenta.usuario] = textos_ia_com.get(
                    cuenta.usuario, ""
                )

        for cuenta in (grupos_ejec.get("rt") or []):
            asignaciones[cuenta.usuario] = ""

        return asignaciones

    def _bucle_rondas(self, procesables: list, duracion_min: int,
                      generar_textos, ejecutar_uno, reportar,
                      cooldown_min: float = 0, porcentaje_min_ronda=40,
                      porcentaje_max_ronda=90, elegibilidad=None,
                      sustituir=None) -> int:
        """Ejecuta acciones en rondas hasta agotar `duracion_min`.

        Worker-pool con cola compartida: `self._n_workers()` trabajadores
        (`MAX_WORKERS`, default `max(12, max_browsers)`) toman cuentas de un
        orden barajado; al agotarlo, regeneran los textos de la
        siguiente ronda, vuelven a barajar y reinician el cursor. Las cuentas
        que no alcanzan a ejecutar antes del deadline se omiten sin abrir
        navegador. Devuelve el numero de rondas iniciadas. Nunca lanza.

        Cada ronda (incluida la primera) trabaja sobre un SUBCONJUNTO
        ALEATORIO de `procesables`: entre `porcentaje_min_ronda` (estricto) y
        `porcentaje_max_ronda` de las cuentas, nunca todas (salvo con <=2
        cuentas o un rango imposible). El subconjunto se pasa a
        `generar_textos(ronda, [usuarios])`; si el callback solo acepta un
        argumento (TypeError) se reintenta como `generar_textos(ronda)`.

        `generar_textos(ronda, usuarios=None)` puede devolver `{usuario:
        texto}` o la tupla `({usuario: texto}, {usuario: rol})`; ambos mapas se
        guardan JUNTOS en el mismo lock, de modo que el rol viaja con su texto
        y ninguna ronda pisa el mapa de otra. `ejecutar_uno(cuenta, texto,
        rol)` recibe ese rol ("" cuando no aplica).

        `cooldown_min` > 0: la MISMA cuenta no repite accion antes de ese
        numero de minutos (medidos desde su ultimo despacho). Si la cuenta en
        turno esta en descanso se mueve al final del orden de la ronda y se
        toma la siguiente; si todas las revisadas descansan, el worker suelta
        el lock, duerme 2-5s y reintenta mientras quede tiempo.

        `elegibilidad(usuario) -> bool` (opcional, curva de aceleracion): se
        evalua al INICIAR CADA RONDA (incluida la 1); el subconjunto aleatorio
        se calcula SOLO entre las cuentas elegibles. Si no queda ninguna, la
        ronda NO avanza ni genera textos y los workers duermen 1-2s esperando
        el cambio de fase (eso no cuenta como fallo ni omision).

        `sustituir()` (opcional): se invoca al iniciar cada ronda para que el
        llamador rote cuentas agotadas por reservas (puede mutar `procesables`
        en sitio). Nunca lanza (sus errores se loguean).
        """
        try:
            minutos = max(0, int(duracion_min or 0))
        except (TypeError, ValueError):
            minutos = 0
        try:
            cooldown_seg = max(0.0, float(cooldown_min or 0)) * 60.0
        except (TypeError, ValueError):
            cooldown_seg = 0.0
        min_pct = _clamp_porcentaje(porcentaje_min_ronda, 40)
        max_pct = _clamp_porcentaje(porcentaje_max_ronda, 90)
        if min_pct > max_pct:
            min_pct, max_pct = max_pct, min_pct
        fin = time.monotonic() + minutos * 60
        n_workers = self._n_workers()
        estado = {
            "cursor": 0, "ronda": 0, "orden": [],
            "textos": {}, "roles": {},
        }
        ultima_accion: dict = {}
        lock = threading.Lock()

        def _en_descanso(usuario) -> bool:
            if cooldown_seg <= 0:
                return False
            ultima = ultima_accion.get(usuario)
            if ultima is None:
                return False
            return (time.monotonic() - ultima) < cooldown_seg

        def _iniciar_ronda_locked() -> bool:
            """Arranca una ronda; False si el GATE no deja elegibles.

            REQUIERE el `lock` local tomado. Con `sustituir` rota cuentas y
            con `elegibilidad` filtra el subconjunto (fase de la curva). Si no
            hay elegibles, no incrementa la ronda ni genera textos: los
            workers duermen y reintentan.
            """
            if self._cancelado():
                return False
            if sustituir is not None:
                try:
                    sustituir()
                except Exception as e:
                    logger.error(
                        f"Activacion (rondas): sustitucion de reservas fallo: "
                        f"{type(e).__name__}: {e}"
                    )
            elegibles = [
                c for c in procesables if not self._sesion_caida(c.usuario)
            ]
            if elegibilidad is not None:
                filtrados = []
                for candidata in elegibles:
                    try:
                        permitida = bool(elegibilidad(candidata.usuario))
                    except Exception:
                        permitida = True
                    if permitida:
                        filtrados.append(candidata)
                elegibles = filtrados
            if not elegibles:
                return False
            # Paro total: cortar ANTES de incrementar la ronda y de generar
            # textos (la IA puede tardar); nada de trabajo nuevo.
            if self._cancelado():
                return False
            estado["ronda"] += 1
            with self._lock:
                self.progreso["ronda_actual"] = estado["ronda"]
            k = _calcular_k_ronda(len(elegibles), min_pct, max_pct)
            if k >= len(elegibles):
                subset = list(elegibles)
            else:
                try:
                    subset = random.sample(elegibles, k)
                except Exception:
                    subset = list(elegibles)
            random.shuffle(subset)
            estado["orden"] = subset
            estado["cursor"] = 0
            if self._cancelado():
                return False
            usuarios = [c.usuario for c in subset]
            try:
                try:
                    generado = generar_textos(estado["ronda"], usuarios)
                except TypeError:
                    generado = generar_textos(estado["ronda"])
            except Exception as e:
                logger.error(
                    f"Activacion (rondas): no se pudieron generar los textos "
                    f"de la ronda {estado['ronda']}: {e}"
                )
                generado = {}
            textos, roles = {}, {}
            if isinstance(generado, tuple) and len(generado) == 2:
                textos, roles = generado
            else:
                textos = generado
            estado["textos"] = textos if isinstance(textos, dict) else {}
            estado["roles"] = roles if isinstance(roles, dict) else {}
            return True

        def _tomar_cuenta_locked():
            """Toma (cuenta, texto, rol) o None si todas descansan/espera gate."""
            orden = estado["orden"]
            if not orden or estado["cursor"] >= len(orden):
                if not _iniciar_ronda_locked():
                    return None
                orden = estado["orden"]
                if not orden:
                    return None
            revisados = 0
            while revisados < len(orden):
                if estado["cursor"] >= len(orden):
                    return None
                candidata = orden[estado["cursor"]]
                if self._sesion_caida(candidata.usuario):
                    # Cuenta omitida en esta campana (sesion caida, rechazo de
                    # X o transitorios agotados): se saca del orden sin contar
                    # un fallo nuevo ni abrir navegador por ella.
                    orden.pop(estado["cursor"])
                    revisados += 1
                    continue
                if _en_descanso(candidata.usuario):
                    orden.pop(estado["cursor"])
                    orden.append(candidata)
                    revisados += 1
                    continue
                estado["cursor"] += 1
                ultima_accion[candidata.usuario] = time.monotonic()
                return (
                    candidata,
                    estado["textos"].get(candidata.usuario, ""),
                    estado["roles"].get(candidata.usuario, ""),
                )
            return None

        def _worker() -> None:
            while time.monotonic() < fin and not self._cancelado():
                with lock:
                    if self._cancelado():
                        return
                    elegido = _tomar_cuenta_locked()
                    ronda = estado["ronda"]
                if elegido is None:
                    # Todas las cuentas revisadas estan en descanso: soltar el
                    # lock y reintentar sin bloquear a los demas workers.
                    if time.monotonic() < fin:
                        time.sleep(random.uniform(1, 2))
                    continue
                cuenta, texto, rol = elegido
                # Paro total: no ejecutar la cuenta recien tomada.
                if self._cancelado():
                    return
                if time.monotonic() >= fin:
                    return
                try:
                    resultado = ejecutar_uno(cuenta, texto, rol)
                except Exception as e:
                    logger.error(
                        f"Error en la ronda {ronda} para @{cuenta.usuario}: {e}"
                    )
                    continue
                with lock:
                    try:
                        reportar(resultado, ronda)
                    except Exception as e:
                        logger.error(
                            f"Error reportando el resultado de activacion: {e}"
                        )
                if time.monotonic() < fin:
                    # Pausa corta entre acciones: el anti-spam real es el
                    # cooldown por cuenta (`cooldown_min`), no este sleep.
                    time.sleep(random.uniform(0.1, 0.4))

        if not procesables:
            return 0

        with ThreadPoolExecutor(max_workers=n_workers) as pool_exec:
            futuros = [pool_exec.submit(_worker) for _ in range(n_workers)]
            for futuro in futuros:
                try:
                    futuro.result()
                except Exception as e:
                    logger.error(f"Worker de activacion termino con error: {e}")

        return estado["ronda"]

    def _asegurar_sesion(self, bot, cuenta) -> tuple:
        """Deja lista la sesion del bot (CDP rapido o login lento).

        Devuelve `(ok, detalle)`: `(True, "")` si la sesion quedo lista;
        `(False, detalle)` con el detalle normalizado de login para que el
        llamador lo reporte. Nunca lanza.
        """
        usuario = getattr(cuenta, "usuario", "")
        motivo = ""
        try:
            preparar_cdp = getattr(bot, "preparar_sesion_cdp", None)
            sesion_cdp = bool(preparar_cdp()) if callable(preparar_cdp) else False
        except Exception as e:
            logger.debug(f"preparar_sesion_cdp fallo para @{usuario}: {e}")
            sesion_cdp = False
        if sesion_cdp:
            logger.debug(f"sesion: CDP para @{usuario}")
            return True, ""
        logger.debug(f"sesion: login lento para @{usuario}")
        try:
            login = getattr(bot, "login_con_cookies", None)
            ok = bool(login()) if callable(login) else False
        except Exception as e:
            ok = False
            motivo = f"{type(e).__name__}: {e}"
        if ok:
            return True, ""
        motivo = getattr(bot, "ultimo_error", "") or motivo or "login fallido"
        logger.warning(f"Login fallido para @{usuario}: {motivo}")
        return False, _detalle_login_fallido(motivo)

    def _capturar_url_post(self) -> bool:
        """True si se debe capturar la URL real de posts/citas (`CAPTURAR_URL_POST`).

        Default 1 (activado): `publicar_tweet`/la cita buscan el link del
        timeline actual (~1-3s) y solo si falla visitan el perfil (~5-10s).
        `CAPTURAR_URL_POST=0` fuerza `buscar_url=False` (maxima velocidad, sin
        links: la tabla de Reportes mostrara "link no capturado"). Nunca lanza.
        """
        try:
            return _env_activo("CAPTURAR_URL_POST", True)
        except Exception:
            return True

    def _accion_en_bot(self, bot, cuenta, rol: str, texto: str,
                       dar_like: bool, url_objetivo: str) -> tuple:
        """Ejecuta la accion del rol en un bot con la sesion ya lista.

        Replica EXACTAMENTE los metodos y detalles del flujo clasico
        (cita/rt/comentario/hashtags) de `_intentar_accion_rol`, para que el
        pool de pestañas y el camino clasico publiquen igual. Nunca lanza:
        cualquier error se reporta como fallo.

        La URL registrada por rol cumple las reglas de Reportes:
        - cita: link del POST DE LA CITA (nunca el ancla; "" si no se capturo);
        - hashtags/post: link del post publicado ("" si no);
        - comentario: link de la RESPUESTA (nunca el ancla; "" si no);
        - rt simple y like: "" SIEMPRE (no generan link).

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url).
        """
        try:
            if rol == "like":
                return (
                    cuenta.usuario, rol, False,
                    "like sin soporte por API en este momento", "",
                )

            if rol == "cita":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    mensaje_cita=texto,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                urls_pub = res.get("urls") or []
                # NUNCA el ancla: si la cita no capturo su URL, queda "".
                url_publicada = _url_publicada_valida(
                    urls_pub[0] if urls_pub else "", cuenta.usuario
                )
                detalle = "ok" if ok else (
                    _detalle_con_sesion(getattr(bot, "ultimo_error", ""))
                    or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            if rol == "rt":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                # El RT simple no genera un post propio: SIN link (la tabla lo
                # etiqueta). Antes se guardaba el perfil o el ancla.
                url_publicada = ""
                detalle = "ok" if ok else (
                    _detalle_con_sesion(getattr(bot, "ultimo_error", ""))
                    or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            if rol == "comentario":
                if not (texto or "").strip():
                    return (
                        cuenta.usuario, rol, False, "sin texto asignado", "",
                    )
                responder = getattr(bot, "responder_tweet", None)
                if responder is None:
                    return (
                        cuenta.usuario, rol, False, "sin soporte de respuesta",
                        "",
                    )
                res = responder(url_objetivo, texto)
                ok = bool(res)
                if isinstance(res, str):
                    # `responder_tweet` devuelve la URL de la RESPUESTA como
                    # str; True (truthy no-str) = publico pero sin URL, y se
                    # usa `ultima_url_publicada` como respaldo.
                    url_publicada = _url_publicada_valida(res, cuenta.usuario)
                elif ok:
                    url_publicada = _url_publicada_valida(
                        getattr(bot, "ultima_url_publicada", "") or "",
                        cuenta.usuario,
                    )
                else:
                    url_publicada = ""
                if ok:
                    detalle = "comentario publicado"
                else:
                    motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                    # La sesion caida se reporta sin prefijo, con la accion
                    # concreta (renovar cookies/login); no es suspension. Las
                    # respuestas limitadas del tweet ancla se reportan claras.
                    detalle = _detalle_comentario(motivo)
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            # rol == "hashtags"
            res = bot.publicar_tweet(
                texto, buscar_url=self._capturar_url_post()
            )
            ok = bool(res)
            if isinstance(res, str):
                url_publicada = _url_publicada_valida(res, cuenta.usuario)
            elif ok:
                url_publicada = _url_publicada_valida(
                    getattr(bot, "ultima_url_publicada", "") or "",
                    cuenta.usuario,
                )
            else:
                url_publicada = ""
            if ok:
                detalle = "hashtags publicados"
            else:
                motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                detalle = _detalle_con_sesion(f"hashtags: {motivo}")
            return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario} (rol {rol}): {e}")
            detalle = _detalle_con_sesion(f"{type(e).__name__}: {e}")
            return (cuenta.usuario, rol, False, detalle[:120], "")

    def _procesar_suspension_cuenta(self, cuenta, bot, detalle="") -> bool:
        """Desactiva la cuenta SOLO si la suspension es real (nunca lanza).

        El bot puede marcar `cuenta_suspendida=True` ante un challenge
        anti-bot de Cloudflare o un interstitial de X: eso NO es una
        suspension real, asi que la cuenta NO se desactiva. Las senales
        anti-bot DURAS se registran (`_registrar_sesion_caida`) para no volver
        a pagar challenges; los rechazos definitivos de X se omiten sin tocar
        la BD y los interstitials transitorios quedan solo con su contador.
        Devuelve True solo cuando desactivo la cuenta de verdad.

        La senal del detalle manda sobre la bandera del bot: `bot.ultimo_error`
        se suma al detalle para no perder la causa anti-bot si el resultado
        venia sin ella (p.ej. el detalle es una URL).
        """
        try:
            if not getattr(bot, "cuenta_suspendida", False):
                return False
        except Exception:
            return False
        usuario = str(getattr(cuenta, "usuario", "") or "")
        try:
            texto = " ".join(
                x for x in (
                    str(detalle or ""),
                    str(getattr(bot, "ultimo_error", "") or ""),
                ) if x
            )
        except Exception:
            texto = ""
        if _es_rechazo_x(texto):
            # Rechazo DEFINITIVO de X: omitir el resto de la campana SIN
            # desactivar la cuenta en la BD (aunque el bot haya puesto
            # `cuenta_suspendida=True`).
            self._registrar_rechazo_x(usuario, texto)
            return False
        if _es_anti_bot_detalle(texto):
            if _es_fallo_navegacion_transitorio(texto) and not _es_anti_bot_bloqueo(
                texto
            ):
                # Interstitial transitorio: NO es suspension ni sesion caida;
                # el contador por cuenta lo registra la accion al reportar su
                # fallo (aqui solo se evita desactivar la cuenta).
                return False
            # Anti-bot/Cloudflare: NO desactivar; omitir en la campana (el log
            # claro lo emite `_registrar_sesion_caida`).
            self._registrar_sesion_caida(usuario, texto or "anti-bot")
            return False
        try:
            marcar_cuenta_suspendida(usuario)
            logger.warning(
                f"@{usuario} marcada como suspendida (desactivada)"
            )
        except Exception:
            pass
        return True

    def _finalizar_pestana(self, pestana, cuenta, resultado) -> None:
        """Devuelve o descarta la pestaña segun el resultado (nunca lanza).

        Aplica la politica de suspension (`_procesar_suspension_cuenta`: las
        senales anti-bot NO desactivan la cuenta), descarta la pestaña si el
        driver murio o el detalle es un error transitorio de navegador y, en
        cualquier otro caso, la libera para que otro worker la reutilice (el
        `finally` del llamador no necesita hacer nada mas).
        """
        exito, detalle = False, ""
        try:
            if isinstance(resultado, tuple):
                if len(resultado) > 2:
                    exito = bool(resultado[2])
                if len(resultado) > 3:
                    detalle = str(resultado[3] or "")
        except Exception:
            exito, detalle = False, ""
        bot = getattr(pestana, "bot", None)
        self._procesar_suspension_cuenta(cuenta, bot, detalle)
        descartar = bool(_es_error_driver_transitorio(detalle))
        if not descartar:
            try:
                descartar = not bool(bot.esta_vivo())
            except Exception:
                descartar = True
        if descartar:
            self._descartar_pestana(pestana)
        else:
            self._liberar_pestana(pestana, exito)

    def _ejecutar_accion_en_pestana(self, pestana, cuenta, rol: str,
                                    texto: str, dar_like: bool,
                                    url_objetivo: str) -> tuple:
        """Ejecuta la accion del rol en una pestaña prestada del pool.

        Usa `_accion_en_bot` (mismo dispatch del camino clasico) y en el
        `finally` libera o descarta la pestaña con `_finalizar_pestana`.
        Nunca lanza: los errores se reportan como fallo.
        """
        bot = pestana.bot
        resultado = (cuenta.usuario, rol, False, "sin exito", url_objetivo)
        try:
            resultado = self._accion_en_bot(
                bot, cuenta, rol, texto, dar_like, url_objetivo
            )
        except Exception as e:
            logger.error(
                f"Error en @{cuenta.usuario} (rol {rol}, pestaña): {e}"
            )
            detalle = _detalle_con_sesion(f"{type(e).__name__}: {e}")
            resultado = (cuenta.usuario, rol, False, detalle[:120], url_objetivo)
        finally:
            self._finalizar_pestana(pestana, cuenta, resultado)
        return resultado

    def _quote_rt_en_pestana(self, pestana, cuenta, url: str, texto: str,
                             dar_like: bool) -> tuple:
        """Quote-RT en una pestaña prestada del pool (tupla de 4).

        Replica exactamente el parseo del camino clasico de
        `_intentar_quote_rt` y libera/descarta la pestaña en el `finally`.
        Nunca lanza.
        """
        bot = pestana.bot
        resultado = (cuenta.usuario, False, "sin exito", "")
        try:
            res = bot.solo_retwittear(
                [url],
                cuenta.usuario,
                mensaje_cita=texto,
                dar_like=dar_like,
            )
            ok = res.get("exitos", 0) > 0
            urls_pub = res.get("urls") or []
            # Solo una publicacion real con `/status/`; nunca el ancla ni el
            # perfil (cuando la cita no capturo su URL queda "").
            url_publicada = _url_publicada_valida(
                urls_pub[0] if urls_pub else "", cuenta.usuario
            )
            if ok:
                resultado = (cuenta.usuario, True, "ok", url_publicada)
            else:
                motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                detalle = _detalle_con_sesion(motivo) or "sin exito"
                resultado = (
                    cuenta.usuario, False, detalle[:120], url_publicada
                )
        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario} (cita, pestaña): {e}")
            detalle = _detalle_con_sesion(f"{type(e).__name__}: {e}") or str(e)
            resultado = (cuenta.usuario, False, detalle[:120], "")
        finally:
            self._finalizar_pestana(pestana, cuenta, resultado)
        return resultado

    def _intentar_quote_rt(self, cuenta: Cuenta, urls: list[str],
                           texto: str, dar_like: bool) -> tuple:
        """Un intento de quote-RT para UNA cuenta; cierra el bot siempre.

        API PRIMERO (mismo criterio que `_intentar_accion_rol`): la cita se
        intenta por HTTP (`accion_rapida("cita", ...)`) antes de abrir Chrome;
        si la API no puede/falla, se usa Selenium. Con el modo pestaña activo
        se reutiliza un Chrome del pool (cambio de cuenta en caliente); si no
        hay pestaña disponible se mantiene el camino clasico (un Chrome por
        accion que se cierra en el `finally`).

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada).
        """
        # Paro total: ni API ni navegador (ok=None = omitida).
        if self._cancelado():
            return (cuenta.usuario, None, MENSAJE_CANCELADO, "")
        bot = None
        detalle_final = ""
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            url = random.choice(urls)
            self._marcar_cascada(url)

            # --- API PRIMERO: sin navegador. ---
            resultado_api = self._probar_api_rol(cuenta, "cita", url, texto, dar_like)
            if resultado_api is not None:
                return (resultado_api[0], resultado_api[2],
                        resultado_api[3], resultado_api[4])

            # --- Selenium: pestaña persistente del pool si esta disponible. ---
            pestana = self._adquirir_pestana(cuenta)
            if pestana is not None and self._cambiar_cuenta_pestana(
                pestana, cuenta
            ):
                return self._quote_rt_en_pestana(
                    pestana, cuenta, url, texto, dar_like
                )

            # Paro total: no crear Chrome para el camino clasico.
            if self._cancelado():
                return (cuenta.usuario, None, MENSAJE_CANCELADO, "")
            bot = TwitterBot(cuenta.usuario)
            ok_sesion, detalle_sesion = self._asegurar_sesion(bot, cuenta)
            if not ok_sesion:
                detalle_final = str(detalle_sesion or "")
                return (cuenta.usuario, False, detalle_sesion[:120], "")

            res = bot.solo_retwittear(
                [url],
                cuenta.usuario,
                mensaje_cita=texto,
                dar_like=dar_like,
            )

            ok = res.get("exitos", 0) > 0
            urls_pub = res.get("urls") or []
            # Cita: solo el link del POST DE LA CITA; si no se capturo, "".
            url_publicada = _url_publicada_valida(
                urls_pub[0] if urls_pub else "", cuenta.usuario
            )
            if ok:
                detalle_final = "ok"
                return (cuenta.usuario, True, "ok", url_publicada)
            motivo = getattr(bot, "ultimo_error", "") or "sin exito"
            detalle = _detalle_con_sesion(motivo) or "sin exito"
            detalle_final = detalle
            return (cuenta.usuario, False, detalle[:120], url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario}: {e}")
            detalle = _detalle_con_sesion(f"{type(e).__name__}: {e}") or str(e)
            detalle_final = detalle
            return (cuenta.usuario, False, detalle[:120], "")
        finally:
            if bot is not None:
                self._procesar_suspension_cuenta(cuenta, bot, detalle_final)
                try:
                    bot.cerrar()
                except Exception as e:
                    logger.warning(
                        f"No se pudo cerrar el navegador de @{cuenta.usuario}: {e}"
                    )

    def _quote_rt_una_cuenta(self, cuenta: Cuenta, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta el quote-RT para UNA cuenta con su propio navegador/proxy.

        Si el primer intento falla por un error transitorio de
        driver/navegador, espera 0.5-1.5s y reintenta UNA vez con un bot nuevo.
        Si el fallo es de RECURSOS (sin hilos/RAM) reduce el limite de
        navegadores y NO reintenta (mas Chrome empeoraria).

        Con cuotas horarias (`self._cuotas`) reserva ATOMICAMENTE un cupo del
        rol "cita" antes de cualquier trabajo; sin cupo devuelve `exito=None`
        con `MENSAJE_CUOTA_AGOTADA` (no es exito ni fallo) y la reserva se
        cierra en el `finally`.

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada). `exito=None` = omitida por
        cuota agotada.
        """
        cuotas = self._cuotas
        reservado = False
        # Paro total: devolver ok=None SIN reservar cuota ni abrir navegador.
        if self._cancelado():
            return (cuenta.usuario, None, MENSAJE_CANCELADO, "")
        if cuotas is not None:
            reservado = bool(cuotas.reservar(cuenta.usuario, "cita"))
            if not reservado:
                self._avisar_cuota_agotada(cuenta.usuario)
                return (
                    cuenta.usuario, None, MENSAJE_CUOTA_AGOTADA, "",
                )
        resultado = (cuenta.usuario, False, "", "")
        try:
            if not _tiene_credencial_sesion(cuenta, _permitir_password()):
                resultado = (
                    cuenta.usuario, False,
                    _mensaje_sin_sesion(cuenta)[:120], "",
                )
                return resultado
            if retardo > 0:
                # Retardo de cohortes (puede ser de minutos) en tramos
                # cancelables: con paro total NO se ejecuta la accion.
                if not self._dormir_cancelable(retardo):
                    resultado = (cuenta.usuario, None, MENSAJE_CANCELADO, "")
                    return resultado

            # Cascada de fase 1 (curva): la cita apunta a las URLs publicadas
            # por Tier 1 si ya empezo la fase 2.
            urls_efectivas = self._urls_efectivas_rol("cita", urls)
            resultado = self._intentar_quote_rt(
                cuenta, urls_efectivas, texto, dar_like
            )
            if resultado[1] is None:
                # Cancelada a mitad: no es sesion caida ni se reintenta.
                return resultado
            if not resultado[1]:
                detalle = resultado[2]
                if _es_error_recursos(detalle):
                    # Recursos agotados: bajar el limite y NO reintentar.
                    self._reducir_navegadores(detalle)
                elif (
                    _es_error_reintentable(detalle)
                    and not self._recursos_recientes()
                ):
                    pausa = random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Reintento de quote-RT para @{cuenta.usuario} por error "
                        f"de driver/navegador ({detalle}); espero {pausa:.1f}s"
                    )
                    time.sleep(pausa)
                    resultado = self._intentar_quote_rt(
                        cuenta, urls_efectivas, texto, dar_like
                    )
                    if resultado[1] is None:
                        return resultado
            if not resultado[1]:
                self._registrar_sesion_caida(resultado[0], resultado[2])
            return resultado
        finally:
            if reservado and cuotas is not None:
                try:
                    cuotas.liberar(
                        cuenta.usuario, "cita", bool(resultado[1])
                    )
                except Exception:
                    pass

    def _esperar_turno_comentario(self, url: str) -> float:
        """Espera el turno de ESTE worker para comentar `url` (anti-spam).

        Reserva un hueco temporal por URL ancla: dos comentarios dirigidos al
        MISMO tweet no salen a la vez, aunque haya varios workers en paralelo.
        No frena otros roles ni comentarios a URLs distintas. Devuelve los
        segundos esperados (0.0 si no hubo espera) y nunca lanza; el tope por
        worker es de 60s para evitar bloqueos eternos si la cola se acumula.
        """
        try:
            try:
                pausa = float(self.pausa_comentario_url_seg or 0)
            except (TypeError, ValueError):
                pausa = 0.0
            if pausa <= 0 or not str(url or "").strip():
                return 0.0
            url = str(url)
            ahora = time.monotonic()
            with self._lock:
                siguiente = self._ultimo_comentario_url.get(url, 0.0)
                espera = max(0.0, siguiente - ahora)
                base = max(ahora, siguiente)
                self._ultimo_comentario_url[url] = base + pausa
            if espera > 0:
                dormido = min(espera, 60.0)
                # Espera anti-spam en tramos <=0.5s: con paro total se corta y
                # la accion ya no se ejecuta (el llamador vuelve a chequear).
                if not self._dormir_cancelable(dormido):
                    return 0.0
                return dormido
            return 0.0
        except Exception:
            return 0.0

    # Roles que la API HTTP puede ejecutar (el resto solo Selenium).
    _ROLES_API = ("rt", "like", "hashtags", "comentario", "cita")

    def _url_objetivo_rol(self, cuenta, rol: str, urls) -> str:
        """URL objetivo del rol ('' si no hay ninguna).

        Los comentarios usan SU ancla asignada al generar el texto
        (`_generar_textos_por_rol`), para que el comentario viaje al tweet que
        su texto describe; el resto de roles sortea entre las URLs.
        """
        opciones = [u for u in (urls or []) if str(u or "").strip()]
        if not opciones:
            return ""
        if rol == "comentario":
            try:
                with self._lock:
                    asignada = self._ancla_por_cuenta.get(cuenta.usuario, "")
            except Exception:
                asignada = ""
            if asignada in opciones:
                return asignada
        return random.choice(opciones)

    def _probar_api_rol(self, cuenta, rol: str, url_objetivo: str,
                        texto: str, dar_like: bool):
        """Intenta LA accion por HTTP; devuelve la tupla de resultado o None.

        - None = rol no soportado / API desactivada (`API_PRIMERO`,
          `RT_POR_API`) / fallo: el llamador usa Selenium.
        - Tupla = exito por API, con detalle "... via API".

        Tolerante a firmas viejas (getattr/callable): si `accion_rapida` no
        acepta `texto` (versiones anteriores) el TypeError cae al except y se
        usa Selenium. Nunca lanza.
        """
        if rol not in self._ROLES_API:
            return None
        if not _api_primero_activo():
            return None
        try:
            from plataformas.twitter.api_http import TwitterAPI

            api = TwitterAPI(cuenta.usuario)
            accion_rapida = getattr(api, "accion_rapida", None)
            if not callable(accion_rapida):
                return None
            try:
                ok = accion_rapida(
                    rol, url=url_objetivo, dar_like=dar_like, texto=texto
                )
            except TypeError:
                # Firma vieja de `accion_rapida` (sin `texto`): rt/like no
                # necesitan texto y siguen por API; los roles que SI publican
                # texto (hashtags/comentario/cita) caen a Selenium para no
                # publicar un texto vacio.
                if rol not in ("rt", "like"):
                    raise
                ok = accion_rapida(rol, url=url_objetivo, dar_like=dar_like)
            if not ok:
                return None
            logger.debug(f"{rol}: API para @{cuenta.usuario}")
            # URL por rol (reglas de Reportes): rt/like NO generan link; los
            # roles que publican texto usan la URL expuesta por la API
            # (`ultima_url_publicada`, id del tweet creado) validada; si la API
            # no la expone, "" (JAMAS el ancla `url_objetivo`).
            if rol in ("rt", "like"):
                url_publicada = ""
            else:
                url_publicada = _url_publicada_valida(
                    getattr(api, "ultima_url_publicada", "") or "",
                    cuenta.usuario,
                )
            return (cuenta.usuario, rol, True, f"{rol} via API", url_publicada)
        except Exception as e:
            logger.debug(
                f"{rol}: API fallo para @{cuenta.usuario} "
                f"({type(e).__name__}: {e}); se usa Selenium"
            )
            return None

    def _cerrar_bot(self, cuenta, bot, detalle="") -> None:
        """Cierra el navegador del bot y aplica la politica de suspension.

        Solo desactiva la cuenta si la suspension es real
        (`_procesar_suspension_cuenta`: las senales anti-bot NO desactivan).
        Nunca lanza.
        """
        if bot is None:
            return
        self._procesar_suspension_cuenta(cuenta, bot, detalle)
        try:
            bot.cerrar()
        except Exception as e:
            logger.warning(
                f"No se pudo cerrar el navegador de @{cuenta.usuario}: {e}"
            )

    def _intentar_accion_rol(self, cuenta: Cuenta, rol: str, urls: list[str],
                             texto: str, dar_like: bool) -> tuple:
        """Un intento de UNA accion segun el rol de activacion de la cuenta.

        - "cita": quote-RT con el texto asignado.
        - "hashtags": publica el texto con hashtags/menciones ("post" es alias).
        - "comentario": respuesta a un tweet (url + texto distintos por slot).
        - "rt": retweet simple (sin cita); puede dar like.

        API PRIMERO: rt/like/hashtags/comentario/cita se intentan por HTTP
        (`TwitterAPI.accion_rapida`) SIN abrir navegador ni consumir el gate;
        solo si la API no puede/falla se usa Selenium, limitado por
        `_limite_navegadores` a la vez (adaptativo: baja si hay agotamiento de
        recursos).

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url).
        Nunca lanza: cualquier error se reporta como fallo.
        """
        if rol == "post":
            rol = "hashtags"

        # Paro total: ok=None (omitida) sin API, cuota ni navegador.
        if self._cancelado():
            return (cuenta.usuario, rol, None, MENSAJE_CANCELADO, "")

        bot = None
        url_objetivo = ""
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            if rol in ("cita", "rt", "comentario", "like"):
                url_objetivo = self._url_objetivo_rol(cuenta, rol, urls)
                if not url_objetivo:
                    return (cuenta.usuario, rol, False, "sin URL objetivo", "")
                # Cascada: si el objetivo vino de las URLs de fase 1, se anota
                # para el contador `cascada_urls` del resumen.
                self._marcar_cascada(url_objetivo)

            if rol == "comentario":
                espero = self._esperar_turno_comentario(url_objetivo)
                if espero > 0:
                    logger.debug(
                        f"comentario: pausa anti-spam {espero:.1f}s para "
                        f"{url_objetivo}"
                    )
                # La espera puede haberse cortado por paro total.
                if self._cancelado():
                    return (
                        cuenta.usuario, rol, None, MENSAJE_CANCELADO,
                        url_objetivo,
                    )

            # --- API PRIMERO (sin navegador ni gate). ---
            resultado_api = self._probar_api_rol(
                cuenta, rol, url_objetivo, texto, dar_like
            )
            if resultado_api is not None:
                return resultado_api
            if rol == "like":
                # "like" solo tiene ruta HTTP: sin soporte Selenium aqui, jamas
                # debe caer al flujo de hashtags (publicaria un post). Sin link
                # (el like no genera publicacion).
                return (
                    cuenta.usuario, rol, False,
                    "like sin soporte por API en este momento", "",
                )

            # --- Selenium: pestaña persistente del pool si esta disponible. ---
            # El pool ya limita a `_limite_navegadores` (no se usa el gate de
            # navegadores aqui para no doble-gatear). Si no hay pestaña, se cae
            # al camino clasico de abajo.
            pestana = self._adquirir_pestana(cuenta)
            if pestana is not None and self._cambiar_cuenta_pestana(
                pestana, cuenta
            ):
                return self._ejecutar_accion_en_pestana(
                    pestana, cuenta, rol, texto, dar_like, url_objetivo
                )

            # --- Selenium (fallback clasico): gate de navegadores. ---
            if not self._adquirir_navegador():
                return (
                    cuenta.usuario, rol, None, MENSAJE_CANCELADO, url_objetivo,
                )
            detalle_accion = ""
            try:
                if self._cancelado():
                    # El paro llego esperando cupo: no crear Chrome.
                    return (
                        cuenta.usuario, rol, None, MENSAJE_CANCELADO,
                        url_objetivo,
                    )
                bot = TwitterBot(cuenta.usuario)
                ok_sesion, detalle_sesion = self._asegurar_sesion(bot, cuenta)
                if not ok_sesion:
                    detalle_accion = str(detalle_sesion or "")
                    return (
                        cuenta.usuario, rol, False,
                        detalle_sesion[:120], "",
                    )
                resultado_accion = self._accion_en_bot(
                    bot, cuenta, rol, texto, dar_like, url_objetivo
                )
                try:
                    if (
                        isinstance(resultado_accion, tuple)
                        and len(resultado_accion) > 3
                    ):
                        detalle_accion = str(resultado_accion[3] or "")
                except Exception:
                    detalle_accion = ""
                return resultado_accion
            finally:
                self._cerrar_bot(cuenta, bot, detalle_accion)
                self._liberar_navegador()

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario} (rol {rol}): {e}")
            detalle = _detalle_con_sesion(f"{type(e).__name__}: {e}")
            return (
                cuenta.usuario, rol, False,
                detalle[:120], "",
            )

    def _ejecutar_accion_rol(self, cuenta: Cuenta, rol: str, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta UNA accion segun el rol de activacion de la cuenta.

        Si el primer intento falla por un error transitorio de
        driver/navegador, espera 0.5-1.5s y reintenta UNA vez con un bot nuevo.
        Si el fallo es de RECURSOS (sin hilos/RAM) o ya se redujo el limite
        hace <30s (`_recursos_agotados`), NO se reintenta: mas Chrome empeora
        el agotamiento.

        Con cuotas horarias (`self._cuotas`): ANTES de cualquier trabajo
        (credenciales, retardo, navegador) reserva un cupo del rol de forma
        ATOMICA; si no hay cupo, o el rol efectivo quedo vacio (cuenta sin
        cupo en modo aleatorio), devuelve `ok=None` con
        `MENSAJE_CUOTA_AGOTADA` y NO ejecuta nada. La reserva se cierra en el
        `finally` (`liberar`): con exito consume cupo, sin exito lo devuelve.

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url). `exito=None` significa omitida
        por cuota agotada (no es exito ni fallo). Nunca lanza: cualquier
        error se reporta como fallo.
        """
        cuotas = self._cuotas
        rol_norm = "hashtags" if rol == "post" else (rol or "")
        # Paro total: ok=None (omitida) SIN reservar cuota ni abrir navegador.
        if self._cancelado():
            return (cuenta.usuario, rol, None, MENSAJE_CANCELADO, "")
        # Cascada de fase 1 (curva): rt/cita/comentario apuntan a las URLs
        # publicadas por Tier 1 si ya empezo la fase 2; si no, a las de siempre.
        urls_efectivas = self._urls_efectivas_rol(rol_norm, urls)
        reservado = False
        if cuotas is not None:
            if not rol_norm:
                return (
                    cuenta.usuario, rol, None, MENSAJE_CUOTA_AGOTADA, "",
                )
            reservado = bool(cuotas.reservar(cuenta.usuario, rol_norm))
            if not reservado:
                self._avisar_cuota_agotada(cuenta.usuario)
                return (
                    cuenta.usuario, rol, None, MENSAJE_CUOTA_AGOTADA, "",
                )
        resultado = (cuenta.usuario, rol, False, "", "")
        try:
            if not _tiene_credencial_sesion(cuenta, _permitir_password()):
                resultado = (
                    cuenta.usuario, rol, False,
                    _mensaje_sin_sesion(cuenta)[:120], "",
                )
                return resultado
            if retardo > 0:
                # Retardo de cohortes (puede ser de minutos) en tramos
                # cancelables: con paro total NO se ejecuta la accion.
                if not self._dormir_cancelable(retardo):
                    resultado = (
                        cuenta.usuario, rol, None, MENSAJE_CANCELADO, "",
                    )
                    return resultado

            resultado = self._intentar_accion_rol(
                cuenta, rol, urls_efectivas, texto, dar_like
            )
            if resultado[2] is None:
                # Cancelada a mitad: no es sesion caida ni se reintenta.
                return resultado
            if not resultado[2]:
                detalle = resultado[3]
                if _es_error_recursos(detalle):
                    # Recursos del contenedor agotados: bajar el limite y NO
                    # reintentar; el worker sigue con otra cuenta.
                    self._reducir_navegadores(detalle)
                elif (
                    _es_error_reintentable(detalle)
                    and not self._recursos_recientes()
                ):
                    pausa = random.uniform(0.5, 1.5)
                    logger.warning(
                        f"Reintento de acción '{rol}' para @{cuenta.usuario} por "
                        f"error de driver/navegador ({detalle}); espero {pausa:.1f}s"
                    )
                    time.sleep(pausa)
                    resultado = self._intentar_accion_rol(
                        cuenta, rol, urls_efectivas, texto, dar_like
                    )
                    if resultado[2] is None:
                        return resultado
            if not resultado[2]:
                # Sesion caida (cookies vencidas/sin credenciales): se omite en
                # las rondas siguientes de la campana (conteo unico, sin abrir
                # Chrome).
                self._registrar_sesion_caida(resultado[0], resultado[3])
            return resultado
        finally:
            if reservado and cuotas is not None:
                try:
                    cuotas.liberar(
                        cuenta.usuario, rol_norm, bool(resultado[2])
                    )
                except Exception:
                    pass

    def ejecutar(
        self,
        urls: list[str],
        texto_base: str,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        n_hashtags: int = 2,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
        hashtags: str = "",
        solo_con_registro: bool = False,
        repetir: bool = False,
        secciones=None,
        porcentaje_min_ronda=40,
        porcentaje_max_ronda=90,
        reserva_usuarios=None,
        curva_aceleracion: bool = False,
        curva_fase1_min=None,
        cancelar=None,
    ) -> dict:
        """Lanza la campaña completa (wrapper del flujo real).

        Garantiza que TODAS las pestañas persistentes queden cerradas al
        terminar (exito, salida temprana o excepcion) y agrega al resumen las
        claves `modo_pestana`, `pestanas_creadas` y `pestanas_recicladas`.
        La logica vive en `_ejecutar_campana` (misma firma).

        `reserva_usuarios`: cuentas de RESPALDO (usuarios o Cuentas) que
        sustituyen 1:1 a las "Agotadas por hoy". `curva_aceleracion`/
        `curva_fase1_min`: modo explosion (fase 1 solo Tier 1). `cancelar`:
        `threading.Event` (basta `is_set()`) para el PARO TOTAL; `None` =
        comportamiento actual exacto.
        """
        inicio = time.monotonic()
        try:
            resumen = self._ejecutar_campana(
                urls, texto_base, cantidad_cuentas, tags, grupo, dar_like,
                duracion_min, cohortes, n_hashtags, narrativa, entrenamiento,
                callback, hashtags, solo_con_registro, repetir, secciones,
                porcentaje_min_ronda, porcentaje_max_ronda,
                reserva_usuarios, curva_aceleracion, curva_fase1_min,
                cancelar,
            )
        finally:
            self._cerrar_pestanas()
        self._loguear_rendimiento(inicio, resumen)
        return self._con_claves_pestana(resumen)

    def _ejecutar_campana(
        self,
        urls: list[str],
        texto_base: str,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        n_hashtags: int = 2,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
        hashtags: str = "",
        solo_con_registro: bool = False,
        repetir: bool = False,
        secciones=None,
        porcentaje_min_ronda=40,
        porcentaje_max_ronda=90,
        reserva_usuarios=None,
        curva_aceleracion: bool = False,
        curva_fase1_min=None,
        cancelar=None,
    ) -> dict:
        """Lanza la campaña completa.

        - duracion_min: minutos que dura la activacion (default 60).
        - cohortes: en cuantos grupos temporales se reparten las cuentas.
        - narrativa: narrativa general de la celula (contexto para OpenAI).
        - entrenamiento: entrenamiento propio del cliente (contexto para OpenAI).
        - hashtags: se garantizan en CADA cita (si faltan, se agregan al final).
        - solo_con_registro: salta las cuentas sin registro (politica/
          activista/ciudadana) sin abrir navegador y las cuenta aparte.
        - repetir: con True las cuentas trabajan en rondas hasta agotar
          `duracion_min`, regenerando textos nuevos en cada ronda y con un
          subconjunto aleatorio de cuentas por ronda (ver porcentajes).
        - secciones: limita a las cuentas de esas secciones (CI/IP/LIB/JUS...);
          vacio o None = todas.
        - porcentaje_min_ronda/porcentaje_max_ronda: con `repetir=True`, rango
          de cuentas por ronda (estricto: mas del minimo, menos que todas).
        - reserva_usuarios: cuentas de RESPALDO (str o Cuentas) que sustituyen
          1:1 a las "Agotadas por hoy" (tope diario).
        - curva_aceleracion/curva_fase1_min: "modo explosion": fase 1 solo
          Tier 1; fase 2 (Tier 2 y sin tier) tras `curva_fase1_min` minutos.
        - cancelar: `threading.Event` con `is_set()` para el PARO TOTAL.
          Ya seteado = la campana termina de inmediato (sin abrir navegador);
          a mitad = los workers salen en segundos y lo pendiente queda
          OMITIDO (`ok=None` + `MENSAJE_CANCELADO`). `None` = comportamiento
          actual exacto (o el evento interno de `solicitar_paro()`).
        """
        # Cuotas horarias: cada campana arranca limpia y las prepara con las
        # cuentas que realmente van a ejecutar (mas abajo).
        self._cuotas = None
        self._reset_curva_campana()
        # PARO TOTAL: guardar el evento externo (no pisa el interno de
        # `solicitar_paro()` cuando `cancelar` viene None).
        self._guardar_cancelar(cancelar)
        if self._cancelado():
            logger.info(
                "Activacion: paro total solicitado antes de arrancar; "
                "no se ejecuta ninguna accion"
            )
            return self._claves_cuotas({
                "exitosas": 0, "fallidas": 0, "detalles": [], "total": 0,
                "sin_sesion": 0, "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
                "sin_registro": 0, "sin_registro_usuarios": [],
                "sugerencia_registro": "",
                "rondas": 0, "omitidas_por_cuota": 0,
                "omitidas_por_cancelacion": 0, "cancelada": True,
            })
        with self._lock:
            self.progreso["ronda_actual"] = 1
        cuentas = self._obtener_cuentas(
            cantidad_cuentas, tags, grupo, secciones
        )

        sin_registro = []
        if solo_con_registro:
            cuentas, sin_registro = _partir_por_registro(cuentas)
        sin_registro_usuarios = [c.usuario for c in sin_registro]
        sugerencia_registro = (
            _sugerencia_registro(len(sin_registro)) if sin_registro else ""
        )
        if sin_registro:
            logger.warning(
                f"Activacion: {len(sin_registro)} cuenta(s) sin registro "
                f"definido, se omiten. {sugerencia_registro}"
            )

        if not cuentas:
            logger.warning("No hay cuentas activas de twitter para la activacion")
            return self._claves_cuotas({
                "exitosas": 0, "fallidas": 0, "detalles": [], "total": 0,
                "sin_sesion": 0, "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
                "sin_registro": len(sin_registro),
                "sin_registro_usuarios": sin_registro_usuarios,
                "sugerencia_registro": sugerencia_registro,
                "rondas": 0 if repetir else 1,
                "omitidas_por_cuota": 0,
            })

        con_sesion, sin_sesion = _partir_por_sesion(cuentas)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia_sesion = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""

        resultados = [
            {"usuario": c.usuario, "ok": False,
             "detalle": _mensaje_sin_sesion(c)[:120], "url": ""}
            for c in sin_sesion
        ]
        for cuenta in sin_sesion:
            mensaje_sin_sesion = _mensaje_sin_sesion(cuenta)[:120]
            with self._lock:
                self.progreso["hechas"] += 1
                self.progreso["fallidas"] += 1
                self._registrar_evento_locked(
                    cuenta.usuario, False, mensaje_sin_sesion, 1, "", ""
                )
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                mensaje_sin_sesion,
            )
            if callback:
                callback(
                    self.progreso["hechas"], len(cuentas),
                    cuenta.usuario, False,
                )
        if sin_sesion:
            logger.warning(
                f"Activacion: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). "
                f"{sugerencia_sesion}"
            )

        if not con_sesion:
            resumen_vacio = {
                "exitosas": self.progreso["exitosas"],
                "fallidas": self.progreso["fallidas"],
                "detalles": resultados,
                "total": len(cuentas),
                "sin_sesion": len(sin_sesion),
                "sin_sesion_usuarios": sin_sesion_usuarios,
                "sugerencia_sesion": sugerencia_sesion,
                "sin_registro": len(sin_registro),
                "sin_registro_usuarios": sin_registro_usuarios,
                "sugerencia_registro": sugerencia_registro,
                "rondas": 0 if repetir else 1,
            }
            logger.info(
                f"Activacion finalizada: {resumen_vacio['exitosas']} exitosas, "
                f"{resumen_vacio['fallidas']} fallidas de {resumen_vacio['total']} "
                f"({resumen_vacio['sin_sesion']} sin sesión)"
            )
            return self._claves_cuotas(resumen_vacio)

        # Con cuentas ejecutables: reservas de respaldo, tiers, cuotas
        # horarias+diarias (base desde la BD) y curva de aceleracion.
        reservas = self._preparar_reservas(
            reserva_usuarios, con_sesion, secciones=secciones,
            solo_con_registro=solo_con_registro,
        )
        self._registrar_tiers(list(con_sesion) + list(reservas))
        self._preparar_cuotas(
            [c.usuario for c in con_sesion] + [c.usuario for c in reservas]
        )
        claves_dinamicas = {
            "reserva_disponible": len(reservas),
            "reserva_usada": 0,
            "agotadas_dia": 0,
            "rotadas_por_cuota_dia": 0,
        }
        curva_activa, fase1_min = self._configurar_curva(
            con_sesion, curva_aceleracion, curva_fase1_min, duracion_min,
            claves_dinamicas,
        )
        if not repetir and reservas:
            # Una sola pasada: la sustitucion de las ya agotadas se hace ANTES
            # de encolar (1:1; en cita masiva todas las cuentas van a "cita").
            self._rotar_agotadas_dia(
                con_sesion, reservas, {}, True, claves_dinamicas
            )

        tags_pedidos = _normalizar_hashtags(hashtags)

        if repetir:
            resumen = {
                "exitosas": self.progreso["exitosas"],
                "fallidas": self.progreso["fallidas"],
                "detalles": list(resultados),
                "total": len(cuentas),
                "sin_sesion": len(sin_sesion),
                "sin_sesion_usuarios": sin_sesion_usuarios,
                "sugerencia_sesion": sugerencia_sesion,
                "sin_registro": len(sin_registro),
                "sin_registro_usuarios": sin_registro_usuarios,
                "sugerencia_registro": sugerencia_registro,
                "rondas": 0,
                "omitidas_por_cuota": 0,
                **claves_dinamicas,
            }

            usados: dict = {}

            def _generar_textos_ronda(_ronda, usuarios=None):
                if usuarios is None:
                    base = list(con_sesion)
                else:
                    try:
                        deseados = {str(u) for u in usuarios}
                    except TypeError:
                        deseados = set()
                    base = [c for c in con_sesion if c.usuario in deseados]
                asignaciones_ronda = self._asignar_variaciones_cita(
                    base,
                    texto_base,
                    narrativa=_narrativa_con_ronda(narrativa, _ronda),
                    entrenamiento=entrenamiento,
                    tags=tags_pedidos,
                )
                return _aplicar_anti_repeticion(asignaciones_ronda, usados)

            def _ejecutar_una_cuenta(cuenta, texto, rol=""):
                # `ejecutar` siempre hace quote-RT (rol "cita"); se acepta el
                # rol del nuevo contrato de `_bucle_rondas` y se ignora aqui.
                try:
                    return self._quote_rt_una_cuenta(
                        cuenta, urls, texto, dar_like, 0
                    )
                except Exception as e:
                    return (
                        cuenta.usuario, False,
                        f"{type(e).__name__}: {e}"[:120], "",
                    )

            def _reportar_ronda(resultado, ronda):
                usuario_res, ok, detalle, url = resultado
                if ok:
                    # Cascada: URL publicada por Tier 1 en fase 1 (cita).
                    self._capturar_url_fase1(usuario_res, "cita", url)
                if ok is None:
                    # Omitida (cuota agotada o PARO TOTAL): no es exito ni
                    # fallo, no se registra en la BD y no dispara callback.
                    with self._lock:
                        self.progreso["omitidas"] = (
                            self.progreso.get("omitidas", 0) + 1
                        )
                        if str(detalle) == MENSAJE_CANCELADO:
                            resumen["omitidas_por_cancelacion"] = (
                                resumen.get("omitidas_por_cancelacion", 0) + 1
                            )
                        else:
                            resumen["omitidas_por_cuota"] = (
                                resumen.get("omitidas_por_cuota", 0) + 1
                            )
                    return
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["exitosas" if ok else "fallidas"] += 1
                    resumen["exitosas" if ok else "fallidas"] += 1
                    self._registrar_evento_locked(
                        usuario_res, ok, detalle, ronda, "cita", url
                    )
                    registrar_accion(
                        usuario_res,
                        tipo_registro_rol("cita"),
                        "exito" if ok else "fallido",
                        url,
                        detalle,
                    )
                    resumen["detalles"].append({
                        "usuario": usuario_res,
                        "ok": ok,
                        "detalle": detalle,
                        "url": url,
                        "ronda": ronda,
                    })
                    if callback:
                        callback(
                            self.progreso["hechas"],
                            max(len(con_sesion), self.progreso["hechas"]),
                            usuario_res,
                            ok,
                        )

            logger.info(
                f"Activacion masiva (rondas): {len(con_sesion)} cuentas con "
                f"sesión, {len(urls)} urls, {duracion_min} min, concurrencia "
                f"{self.max_concurrente}"
            )
            resumen["rondas"] = self._bucle_rondas(
                con_sesion,
                duracion_min,
                _generar_textos_ronda,
                _ejecutar_una_cuenta,
                _reportar_ronda,
                porcentaje_min_ronda=porcentaje_min_ronda,
                porcentaje_max_ronda=porcentaje_max_ronda,
                elegibilidad=self._elegibilidad_curva(curva_activa),
                sustituir=(
                    lambda: self._rotar_agotadas_dia(
                        con_sesion, reservas, {}, True, resumen
                    )
                ),
            )
            try:
                fase = 2 if self._en_fase2() else 1
                with self._lock:
                    self.progreso["fase_actual"] = fase
                resumen["fase_actual"] = fase
            except Exception:
                pass
            logger.info(
                f"Activacion finalizada (rondas): {resumen['exitosas']} exitosas, "
                f"{resumen['fallidas']} fallidas en {resumen['rondas']} ronda(s) "
                f"({resumen['sin_sesion']} sin sesión, "
                f"{resumen['sin_registro']} sin registro)"
            )
            return self._claves_cuotas(resumen)

        # --- Una sola pasada (con curva: DOS etapas secuenciales) ---------- #
        # Omitidas por cuota horaria/diaria ANTES de encolar (la garantia
        # atomica la da la reserva interna de `_quote_rt_una_cuenta`).
        omitidas_por_cuota = 0
        omitidas_por_cancelacion = 0

        def _pasada_simple(cuentas_pasada, duracion_pasada):
            """Ejecuta una pasada de quote-RTs sobre `cuentas_pasada`."""
            nonlocal omitidas_por_cuota, omitidas_por_cancelacion
            if not cuentas_pasada or self._cancelado():
                return
            # Un pool por grupo (registro, perfil): cada cuenta publica una cita
            # con su propio estilo. La narrativa viaja solo como trasfondo.
            asignaciones = self._asignar_variaciones_cita(
                cuentas_pasada,
                texto_base,
                narrativa=narrativa,
                entrenamiento=entrenamiento,
                tags=tags_pedidos,
            )
            try:
                duracion_efectiva = max(1, int(duracion_pasada or 1))
            except Exception:
                duracion_efectiva = 1
            bloques = self._distribuir_cohortes(
                list(cuentas_pasada), duracion_efectiva, cohortes
            )
            intervalo_cohorte = max(
                1, (duracion_efectiva * 60) // max(cohortes, 1)
            )
            # El ThreadPoolExecutor ya limita la concurrencia; el retardo solo
            # distribuye los INICIOS a lo largo de la ventana para no disparar
            # todas las cuentas al mismo tiempo.
            with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
                futuros = []
                for idx, bloque in enumerate(bloques):
                    # Paro total: no encolar mas tareas.
                    if self._cancelado():
                        break
                    for cuenta in bloque:
                        if self._cancelado():
                            break
                        if (
                            self._cuotas is not None
                            and not self._cuotas.rol_permitido(cuenta.usuario, "cita")
                        ):
                            self._avisar_cuota_agotada(cuenta.usuario)
                            with self._lock:
                                self.progreso["omitidas"] = (
                                    self.progreso.get("omitidas", 0) + 1
                                )
                            omitidas_por_cuota += 1
                            continue
                        retardo = idx * intervalo_cohorte + random.uniform(0, 15)
                        futuros.append((retardo, pool_exec.submit(
                            self._quote_rt_una_cuenta, cuenta, urls,
                            asignaciones.get(cuenta.usuario, ""), dar_like,
                            retardo
                        ), cuenta.usuario))

                # Recoger resultados en orden de arranque (los retardos ya
                # fueron aplicados al submit via scheduling del pool). Con el
                # paro total se corta la recoleccion: las tareas ya encoladas
                # terminan/salen solas (ok=None) al ver el evento.
                for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                    if self._cancelado():
                        break
                    try:
                        usuario_res, ok, detalle, url = futuro.result()
                    except Exception as e:
                        usuario_res, ok, detalle, url = usuario, False, str(e)[:80], ""

                    if ok:
                        self._capturar_url_fase1(usuario_res, "cita", url)
                    if ok is None:
                        # Omitida (cuota agotada o PARO TOTAL; carrera con otro
                        # worker): no es exito ni fallo ni toca la BD.
                        with self._lock:
                            self.progreso["omitidas"] = (
                                self.progreso.get("omitidas", 0) + 1
                            )
                        if str(detalle) == MENSAJE_CANCELADO:
                            omitidas_por_cancelacion += 1
                        else:
                            omitidas_por_cuota += 1
                        continue

                    with self._lock:
                        self.progreso["hechas"] += 1
                        if ok:
                            self.progreso["exitosas"] += 1
                        else:
                            self.progreso["fallidas"] += 1
                        self._registrar_evento_locked(
                            usuario_res, ok, detalle, 1, "cita", url
                        )
                        registrar_accion(
                            usuario_res,
                            tipo_registro_rol("cita"),
                            "exito" if ok else "fallido",
                            url,
                            detalle,
                        )
                        resultados.append({"usuario": usuario_res, "ok": ok, "detalle": detalle, "url": url})
                        if callback:
                            callback(self.progreso["hechas"], len(cuentas), usuario_res, ok)

        logger.info(
            f"Activacion masiva: {len(con_sesion)} cuentas con sesión "
            f"({len(sin_sesion)} sin sesión saltadas), {len(urls)} urls, "
            f"{cohortes} cohortes, {duracion_min} min, concurrencia {self.max_concurrente}"
        )

        fase_final = 2 if self._en_fase2() else 1
        if curva_activa:
            # DOS ETAPAS secuenciales: primero Tier 1 (fase 1, acotada por
            # `fase1_min`) y despues Tier 2 + sin tier (fase 2). Misma
            # concurrencia y mismos reportes que el flujo normal.
            tier1_pasada = [
                c for c in con_sesion
                if self._tier_de.get(str(c.usuario)) == "tier1"
            ]
            resto_pasada = [
                c for c in con_sesion
                if self._tier_de.get(str(c.usuario)) != "tier1"
            ]
            if tier1_pasada and resto_pasada:
                try:
                    with self._lock:
                        self.progreso["fase_actual"] = 1
                    _pasada_simple(
                        tier1_pasada,
                        max(1, min(int(duracion_min or 1), int(fase1_min))),
                    )
                    with self._lock:
                        self.progreso["fase_actual"] = 2
                    # La fase 2 REAL empieza aqui (aunque la etapa 1 haya
                    # terminado antes del reloj): la cascada de URLs de Tier 1
                    # ya aplica a los rt/cita de la etapa 2.
                    self._curva_fase2_t0 = time.monotonic() - 1.0
                    _pasada_simple(
                        resto_pasada,
                        max(1, int(duracion_min or 1) - int(fase1_min)),
                    )
                    fase_final = 2
                except Exception as e:
                    logger.error(
                        f"Activacion: error en las etapas de la curva "
                        f"({type(e).__name__}: {e}); se continua"
                    )
            else:
                _pasada_simple(con_sesion, duracion_min)
        else:
            _pasada_simple(con_sesion, duracion_min)

        resumen = {
            "exitosas": self.progreso["exitosas"],
            "fallidas": self.progreso["fallidas"],
            "detalles": resultados,
            "total": len(cuentas),
            "sin_sesion": len(sin_sesion),
            "sin_sesion_usuarios": sin_sesion_usuarios,
            "sugerencia_sesion": sugerencia_sesion,
            "sin_registro": len(sin_registro),
            "sin_registro_usuarios": sin_registro_usuarios,
            "sugerencia_registro": sugerencia_registro,
            "rondas": 1,
            "omitidas_por_cuota": omitidas_por_cuota,
            "omitidas_por_cancelacion": omitidas_por_cancelacion,
            "fase_actual": fase_final,
            **claves_dinamicas,
        }
        logger.info(
            f"Activacion finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_sesion']} sin sesión, "
            f"{resumen['sin_registro']} sin registro)"
        )
        return self._claves_cuotas(resumen)

    def ejecutar_por_roles(
        self,
        urls: list[str],
        texto_base: str = "",
        hashtags: str = "",
        menciones: str = "",
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        usuarios: list | None = None,
        solo_roles: list | None = None,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
        contexto: str = "",
        solo_con_registro: bool = False,
        repetir: bool = False,
        roles_aleatorios: bool = False,
        cooldown_min: float = 0,
        secciones=None,
        porcentaje_min_ronda=40,
        porcentaje_max_ronda=90,
        pausa_comentario_url_seg: float = 15.0,
        reserva_usuarios=None,
        curva_aceleracion: bool = False,
        curva_fase1_min=None,
        cancelar=None,
    ) -> dict:
        """Campaña masiva dividida en subcuentas por rol (wrapper del flujo).

        Garantiza que TODAS las pestañas persistentes queden cerradas al
        terminar (exito, salida temprana o excepcion) y agrega al resumen las
        claves `modo_pestana`, `pestanas_creadas` y `pestanas_recicladas`.
        La logica vive en `_ejecutar_por_roles_campana` (misma firma).

        `reserva_usuarios`: cuentas de RESPALDO (usuarios o Cuentas) que
        sustituyen 1:1 a las "Agotadas por hoy" (Tier 2 jamas sustituye un rol
        hashtags). `curva_aceleracion`/`curva_fase1_min`: modo explosion
        (fase 1 solo Tier 1). `cancelar`: `threading.Event` (basta `is_set()`)
        para el PARO TOTAL; `None` = comportamiento actual exacto.
        """
        inicio = time.monotonic()
        try:
            resumen = self._ejecutar_por_roles_campana(
                urls, texto_base, hashtags, menciones, dar_like, duracion_min,
                cohortes, usuarios, solo_roles, narrativa, entrenamiento,
                callback, contexto, solo_con_registro, repetir, roles_aleatorios,
                cooldown_min, secciones, porcentaje_min_ronda,
                porcentaje_max_ronda, pausa_comentario_url_seg,
                reserva_usuarios, curva_aceleracion, curva_fase1_min,
                cancelar,
            )
        finally:
            self._cerrar_pestanas()
        self._loguear_rendimiento(inicio, resumen)
        return self._con_claves_pestana(resumen)

    def _ejecutar_por_roles_campana(
        self,
        urls: list[str],
        texto_base: str = "",
        hashtags: str = "",
        menciones: str = "",
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        usuarios: list | None = None,
        solo_roles: list | None = None,
        narrativa: str = "",
        entrenamiento: str = "",
        callback=None,
        contexto: str = "",
        solo_con_registro: bool = False,
        repetir: bool = False,
        roles_aleatorios: bool = False,
        cooldown_min: float = 0,
        secciones=None,
        porcentaje_min_ronda=40,
        porcentaje_max_ronda=90,
        pausa_comentario_url_seg: float = 15.0,
        reserva_usuarios=None,
        curva_aceleracion: bool = False,
        curva_fase1_min=None,
        cancelar=None,
    ) -> dict:
        """Campaña masiva dividida en subcuentas por rol.

        - Carga cuentas twitter activas; si `usuarios` se pasa, limita a esos
          usuarios; si `solo_roles`, filtra a esos roles (salvo en modo
          aleatorio, donde `solo_roles` es el subconjunto a sortear); si
          `secciones`, limita a esas secciones (CI/IP/LIB/JUS...).
        - Agrupa por Cuenta.rol_activacion (normalizado con core/roles.py):
            * "cita": quote-RT con texto del pool (OpenAI + fallback local)
              con los hashtags pedidos garantizados.
            * "hashtags": posts ORIGINALES por cuenta con IA (registro/perfil)
              sobre `contexto`; si la IA falla, cae al pool de respaldo
              base + hashtags + menciones.
            * "comentario": respuestas ORIGINALES por cuenta con IA sobre el
              tweet ancla (`contexto`/`texto_base`); si la IA falla, cae al
              pool de variaciones.
            * "rt": retweet simple (con like opcional).
        - Cuentas SIN rol se saltan y se cuentan en `sin_rol`.
        - Cuentas SIN ninguna credencial de sesion (.pkl, cookies_json ni
          auth_token) se filtran antes de abrir navegadores y se cuentan en
          `sin_sesion` con la accion sugerida en `sugerencia_sesion`.
        - `solo_con_registro`: salta las cuentas sin registro (politica/
          activista/ciudadana) sin abrir navegador y las cuenta aparte.
        - `repetir`: con True las cuentas trabajan en rondas hasta agotar
          `duracion_min`, regenerando textos nuevos en cada ronda y con un
          subconjunto aleatorio de cuentas por ronda (ver porcentajes).
        - `roles_aleatorios`: en vez del rol guardado, a CADA cuenta le toca
          un rol sorteado (cita/comentario/rt con URLs, hashtags con hashtags/
          contexto/texto base) en cada ronda; una cuenta que participa en
          rondas seguidas NUNCA repite su rol anterior mientras haya 2+ roles
          posibles. `solo_roles` limita el sorteo a esa interseccion; sin
          roles posibles devuelve el resumen vacio con `sugerencia_roles` sin
          abrir ningun navegador.
        - `cooldown_min`: minutos minimos entre dos acciones de la MISMA
          cuenta (0 = sin descanso). Las cuentas en descanso se mueven al
          final de la ronda y no se ejecutan antes de tiempo.
        - `porcentaje_min_ronda`/`porcentaje_max_ronda`: con `repetir=True`,
          rango de cuentas por ronda (estricto: mas del minimo, menos que
          todas).
        - `pausa_comentario_url_seg`: segundos minimos entre dos comentarios
          dirigidos a la MISMA URL ancla (0 = sin pausa); el hueco se reserva
          antes de abrir el navegador y solo afecta al rol "comentario" (no
          frena citas/RTs/hashtags ni comentarios a otras URLs).
        - TIER: las cuentas Tier 2 NO pueden publicar hashtags/posts (modo fijo
          se excluyen y se cuentan en `tier2_hashtags_omitidas`; en modo
          aleatorio nunca les toca ese rol). Jamas se desactivan en la BD.
        - `reserva_usuarios`: cuentas de RESPALDO (usuarios o Cuentas) que
          sustituyen 1:1 a las "Agotadas por hoy" (Tier 2 jamas sustituye un
          rol hashtags).
        - `curva_aceleracion`/`curva_fase1_min`: "modo explosion": fase 1 solo
          Tier 1; fase 2 (Tier 2 y sin tier) tras `curva_fase1_min` minutos.
        - Cohortes temporales + delay aleatorio y concurrencia limitada,
          igual que `ejecutar()`.
        - `cancelar`: `threading.Event` con `is_set()` para el PARO TOTAL.
          Ya seteado = la campana termina de inmediato (sin abrir navegador);
          a mitad = los workers salen en segundos y lo pendiente queda
          OMITIDO (`ok=None` + `MENSAJE_CANCELADO`). `None` = comportamiento
          actual exacto (o el evento interno de `solicitar_paro()`).
        - Nunca lanza: cada cuenta fallida se reporta en `detalles`.
        """
        # Cuotas horarias: cada campana arranca limpia y las prepara con las
        # cuentas ejecutables (mas abajo).
        self._cuotas = None
        self._reset_curva_campana()
        urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        try:
            cooldown_val = max(0.0, float(cooldown_min or 0))
        except (TypeError, ValueError):
            cooldown_val = 0.0
        try:
            pausa_comentario_val = max(
                0.0, float(pausa_comentario_url_seg or 0)
            )
        except (TypeError, ValueError):
            pausa_comentario_val = 0.0
        self.pausa_comentario_url_seg = pausa_comentario_val
        # PARO TOTAL: guardar el evento externo (no pisa el interno de
        # `solicitar_paro()` cuando `cancelar` viene None).
        self._guardar_cancelar(cancelar)
        if self._cancelado():
            logger.info(
                "Activacion por roles: paro total solicitado antes de "
                "arrancar; no se ejecuta ninguna accion"
            )
            return self._claves_cuotas({
                "total": 0,
                "exitosas": 0,
                "fallidas": 0,
                "sin_rol": 0,
                "sin_sesion": 0,
                "por_rol": {
                    "cita": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "hashtags": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "comentario": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "rt": {"total": 0, "exitosas": 0, "fallidas": 0},
                },
                "detalles": [],
                "sin_rol_usuarios": [],
                "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
                "sin_registro": 0,
                "sin_registro_usuarios": [],
                "sugerencia_registro": "",
                "rondas": 0,
                "roles_aleatorios": bool(roles_aleatorios),
                "cooldown_min": cooldown_val,
                "pausa_comentario_url_seg": pausa_comentario_val,
                "omitidas_por_cuota": 0,
                "omitidas_por_cancelacion": 0,
                "cancelada": True,
            })
        roles_sortear = (
            _roles_disponibles_aleatorios(
                urls, hashtags, contexto, texto_base, solo_roles,
                narrativa=narrativa,
            )
            if roles_aleatorios else []
        )
        if roles_aleatorios and not roles_sortear:
            sugerencia_roles = _sugerencia_roles_aleatorios()
            logger.warning(f"Activacion por roles: {sugerencia_roles}")
            return self._claves_cuotas({
                "total": 0,
                "exitosas": 0,
                "fallidas": 0,
                "sin_rol": 0,
                "sin_sesion": 0,
                "por_rol": {
                    "cita": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "hashtags": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "comentario": {"total": 0, "exitosas": 0, "fallidas": 0},
                    "rt": {"total": 0, "exitosas": 0, "fallidas": 0},
                },
                "detalles": [],
                "sin_rol_usuarios": [],
                "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
                "sin_registro": 0,
                "sin_registro_usuarios": [],
                "sugerencia_registro": "",
                "rondas": 0 if repetir else 1,
                "roles_aleatorios": True,
                "cooldown_min": cooldown_val,
                "pausa_comentario_url_seg": pausa_comentario_val,
                "sugerencia_roles": sugerencia_roles,
                "omitidas_por_cuota": 0,
            })
        cuentas = self._obtener_cuentas_por_rol(
            usuarios, None if roles_aleatorios else solo_roles, secciones
        )

        sin_registro = []
        if solo_con_registro:
            cuentas, sin_registro = _partir_por_registro(cuentas)
        sin_registro_usuarios = [c.usuario for c in sin_registro]
        sugerencia_registro = (
            _sugerencia_registro(len(sin_registro)) if sin_registro else ""
        )
        if sin_registro:
            logger.warning(
                f"Activacion por roles: {len(sin_registro)} cuenta(s) sin "
                f"registro definido, se omiten. {sugerencia_registro}"
            )

        grupos = {"cita": [], "hashtags": [], "comentario": [], "rt": []}
        sin_rol_usuarios = []
        tier2_hashtags_usuarios: list = []
        if roles_aleatorios:
            # El rol guardado no filtra: cada cuenta recibe un rol sorteado en
            # cada ronda (los roles posibles ya se validaron arriba).
            procesables = list(cuentas)
            rol_de: dict = {}
        else:
            for cuenta in cuentas:
                # Rol canonico con alias ("post"/"mantenimiento"/"hilo" ->
                # "hashtags", "respuesta"/"reply" -> "comentario"): asi Tier 2
                # con "post" tambien cae en el blindaje de hashtags.
                rol = _rol_efectivo_cuenta(cuenta)
                if rol in grupos:
                    grupos[rol].append(cuenta)
                else:
                    sin_rol_usuarios.append(cuenta.usuario)

            # TIER: las cuentas Tier 2 con rol efectivo "hashtags" (incluye
            # "post"/"mantenimiento"/"hilo", que normalizan a hashtags) se
            # EXCLUYEN antes de abrir navegador. NO se desactivan en la BD:
            # solo se omiten de la campaña y se reportan en `tier2_hashtags_*`.
            permitidas_tier = []
            for cuenta in grupos["hashtags"]:
                if _tier_permitido(cuenta, "hashtags"):
                    permitidas_tier.append(cuenta)
                else:
                    usuario_omitido = str(cuenta.usuario)
                    tier2_hashtags_usuarios.append(usuario_omitido)
                    logger.warning(
                        f"Activacion por roles: @{usuario_omitido} es Tier 2 y "
                        f"el rol 'hashtags' esta PROHIBIDO para su tier; se "
                        f"omite sin abrir navegador (NO se desactiva en la "
                        f"BD). {_error_tier(cuenta, 'hashtags')}"
                    )
            grupos["hashtags"] = permitidas_tier
            # Defensa extra: cuentas que `_obtener_cuentas_por_rol` ya filtro
            # por el mismo motivo (solo_roles=["hashtags"]).
            for usuario_filtrado in list(
                getattr(self, "_tier2_filtradas_rol", []) or []
            ):
                if usuario_filtrado not in tier2_hashtags_usuarios:
                    tier2_hashtags_usuarios.append(usuario_filtrado)

            procesables = (
                grupos["cita"] + grupos["hashtags"]
                + grupos["comentario"] + grupos["rt"]
            )
            rol_de = {
                cuenta.usuario: _rol_efectivo_cuenta(cuenta)
                for cuenta in procesables
            }
        ejecutables, sin_sesion = _partir_por_sesion(procesables)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia_sesion = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""
        grupos_ejec = {"cita": [], "hashtags": [], "comentario": [], "rt": []}
        if not roles_aleatorios:
            for cuenta in ejecutables:
                grupos_ejec[rol_de.get(cuenta.usuario, "")].append(cuenta)

        resumen = {
            "total": len(procesables),
            "exitosas": 0,
            "fallidas": 0,
            "sin_rol": len(sin_rol_usuarios),
            "sin_sesion": len(sin_sesion),
            "por_rol": {
                "cita": {
                    "total": len(grupos["cita"]), "exitosas": 0, "fallidas": 0,
                },
                "hashtags": {
                    "total": len(grupos["hashtags"]), "exitosas": 0, "fallidas": 0,
                },
                "comentario": {
                    "total": len(grupos["comentario"]), "exitosas": 0,
                    "fallidas": 0,
                },
                "rt": {
                    "total": len(grupos["rt"]), "exitosas": 0, "fallidas": 0,
                },
            },
            "detalles": [],
            "sin_rol_usuarios": sin_rol_usuarios,
            "sin_sesion_usuarios": sin_sesion_usuarios,
            "sugerencia_sesion": sugerencia_sesion,
            "sin_registro": len(sin_registro),
            "sin_registro_usuarios": sin_registro_usuarios,
            "sugerencia_registro": sugerencia_registro,
            "rondas": 0 if repetir else 1,
            "roles_aleatorios": bool(roles_aleatorios),
            "cooldown_min": cooldown_val,
            "pausa_comentario_url_seg": pausa_comentario_val,
            "omitidas_por_cuota": 0,
            "tier2_hashtags_omitidas": len(tier2_hashtags_usuarios),
            "tier2_hashtags_usuarios": list(tier2_hashtags_usuarios),
            "tier2_sin_rol": 0,
            "tier2_sin_rol_usuarios": [],
        }
        for cuenta in sin_sesion:
            rol = rol_de.get(cuenta.usuario, "")
            resumen["fallidas"] += 1
            if rol in resumen["por_rol"]:
                resumen["por_rol"][rol]["fallidas"] += 1
            resumen["detalles"].append({
                "usuario": cuenta.usuario,
                "rol": rol,
                "ok": False,
                "detalle": _mensaje_sin_sesion(cuenta)[:120],
                "url": "",
            })

        if not procesables:
            logger.warning(
                "Activacion por roles: no hay cuentas con rol para ejecutar "
                f"({len(sin_rol_usuarios)} sin rol)"
            )
            return self._claves_cuotas(resumen)

        if sin_sesion:
            logger.warning(
                f"Activacion por roles: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). "
                f"{sugerencia_sesion}"
            )

        if not ejecutables:
            logger.info(
                f"Activacion por roles finalizada: 0 exitosas, "
                f"{resumen['fallidas']} fallidas de {resumen['total']} "
                f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión)"
            )
            return self._claves_cuotas(resumen)

        rol_por_usuario = {
            cuenta.usuario: rol_de.get(cuenta.usuario, "")
            for cuenta in ejecutables
        }

        # Con cuentas ejecutables: reservas de respaldo, tiers, cuotas
        # horarias+diarias (base desde la BD) y curva de aceleracion.
        reservas = self._preparar_reservas(
            reserva_usuarios, ejecutables, secciones=secciones,
            solo_con_registro=solo_con_registro,
        )
        self._registrar_tiers(list(ejecutables) + list(reservas))
        self._preparar_cuotas(
            [c.usuario for c in ejecutables] + [c.usuario for c in reservas]
        )
        claves_dinamicas = {
            "reserva_disponible": len(reservas),
            "reserva_usada": 0,
            "agotadas_dia": 0,
            "rotadas_por_cuota_dia": 0,
        }
        curva_activa, fase1_min = self._configurar_curva(
            ejecutables, curva_aceleracion, curva_fase1_min, duracion_min,
            claves_dinamicas,
        )
        resumen.update(claves_dinamicas)

        logger.info(
            f"Activacion por roles: {len(ejecutables)} cuentas con sesión "
            f"(cita={len(grupos_ejec['cita'])}, hashtags={len(grupos_ejec['hashtags'])}, "
            f"rt={len(grupos_ejec['rt'])}, sin_rol={len(sin_rol_usuarios)}, "
            f"sin_sesion={len(sin_sesion)}), "
            f"{len(urls)} urls, {cohortes} cohortes, {duracion_min} min, "
            f"navegadores {self.max_concurrente}, trabajadores {self._n_workers()}"
            + (f", reservas {len(reservas)}" if reservas else "")
        )

        with self._lock:
            self.progreso = {
                "hechas": len(sin_sesion),
                "exitosas": 0,
                "fallidas": len(sin_sesion),
                "omitidas": 0,
                "ronda_actual": 1,
                "fase_actual": 1,
                "eventos": [],
            }
        for cuenta in sin_sesion:
            mensaje_sin_sesion = _mensaje_sin_sesion(cuenta)[:120]
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                mensaje_sin_sesion,
            )
            with self._lock:
                self._registrar_evento_locked(
                    cuenta.usuario, False, mensaje_sin_sesion, 1,
                    rol_de.get(cuenta.usuario, ""), "",
                )
            if callback:
                callback(
                    self.progreso["hechas"], len(procesables),
                    cuenta.usuario, False,
                )
        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

        # Contexto REAL del comentario: se descarga el texto de cada tweet
        # ancla UNA vez por URL (con cache) ANTES de las rondas, para que los
        # comentarios hablen del tweet al que responden y no de la campana.
        hay_comentarios = (
            ("comentario" in roles_sortear)
            if roles_aleatorios
            else bool(grupos_ejec.get("comentario"))
        )
        ancla_textos: dict = {}
        if urls and hay_comentarios:
            ancla_textos = self._obtener_anclas(urls)

        if repetir:
            usados: dict = {}
            roles_ultimos: dict = {}

            def _generar_textos_ronda(_ronda, usuarios=None):
                if usuarios is None:
                    base = list(ejecutables)
                else:
                    try:
                        deseados = {str(u) for u in usuarios}
                    except TypeError:
                        deseados = set()
                    base = [c for c in ejecutables if c.usuario in deseados]
                if roles_aleatorios:
                    roles_ronda = self._asignar_roles_aleatorios(
                        base, roles_sortear, roles_previos=roles_ultimos,
                        cuotas=self._cuotas,
                    )
                    roles_ultimos.update(roles_ronda)
                    for candidata in base:
                        if roles_ronda.get(candidata.usuario, ""):
                            continue
                        if not _es_tier2(candidata):
                            continue
                        if any(
                            _tier_permitido(candidata, r)
                            for r in roles_sortear
                        ):
                            continue
                        self._contar_tier2_sin_rol(
                            candidata.usuario, resumen
                        )
                    grupos_ronda = self._grupos_desde_roles(
                        base, roles_ronda
                    )
                    textos = self._generar_textos_por_rol(
                        grupos_ronda,
                        texto_base,
                        hashtags=hashtags,
                        menciones=menciones,
                        narrativa=narrativa,
                        entrenamiento=entrenamiento,
                        contexto=contexto,
                        ronda=_ronda,
                        ancla_textos=ancla_textos,
                    )
                    return (
                        _aplicar_anti_repeticion(textos, usados),
                        roles_ronda,
                    )
                if usuarios is None:
                    grupos_ronda = grupos_ejec
                else:
                    # Se parte de `grupos_ejec` (no de `cuentas`) para que las
                    # reservas que sustituyeron cuentas agotadas ya esten
                    # incluidas en la ronda.
                    grupos_ronda = {
                        rol: [c for c in lista if c.usuario in deseados]
                        for rol, lista in grupos_ejec.items()
                    }
                if self._cuotas is not None:
                    # Filtra (SIN mutar `grupos_ejec`) las cuentas sin cupo del
                    # rol: no se les genera texto ni se les despacha accion.
                    grupos_filtrados: dict = {}
                    for rol, lista in grupos_ronda.items():
                        permitidas = []
                        for candidata in lista:
                            try:
                                hay_cupo = self._cuotas.rol_permitido(
                                    candidata.usuario, rol
                                )
                            except Exception:
                                hay_cupo = True
                            if hay_cupo:
                                permitidas.append(candidata)
                            else:
                                self._avisar_cuota_agotada(candidata.usuario)
                        grupos_filtrados[rol] = permitidas
                    grupos_ronda = grupos_filtrados
                textos = self._generar_textos_por_rol(
                    grupos_ronda,
                    texto_base,
                    hashtags=hashtags,
                    menciones=menciones,
                    narrativa=narrativa,
                    entrenamiento=entrenamiento,
                    contexto=contexto,
                    ronda=_ronda,
                    ancla_textos=ancla_textos,
                )
                return _aplicar_anti_repeticion(textos, usados)

            def _ejecutar_una_rol(cuenta, texto, rol=""):
                rol_efectivo = rol or rol_por_usuario.get(cuenta.usuario, "")
                if not rol_efectivo:
                    # Sin rol efectivo: la cuenta no tiene cupo (modo aleatorio
                    # con cuotas) o es Tier 2 sin ningun rol permitido. JAMAS
                    # abre navegador; sin este guard caeria al flujo hashtags.
                    sin_rol_tier = _es_tier2(cuenta) and not any(
                        _tier_permitido(cuenta, r) for r in roles_sortear
                    )
                    detalle = (
                        MENSAJE_TIER2_SIN_ROL if sin_rol_tier
                        else MENSAJE_CUOTA_AGOTADA
                    )
                    return (cuenta.usuario, rol_efectivo, None, detalle, "")
                try:
                    return self._ejecutar_accion_rol(
                        cuenta, rol_efectivo, urls, texto, dar_like, 0
                    )
                except Exception as e:
                    return (
                        cuenta.usuario, rol_efectivo, False,
                        f"{type(e).__name__}: {e}"[:120], "",
                    )

            def _reportar_rol(resultado, ronda):
                usuario_res, rol_res, ok, detalle, url = resultado
                if ok:
                    # Cascada: URL publicada por Tier 1 en fase 1.
                    self._capturar_url_fase1(usuario_res, rol_res, url)
                if ok is None:
                    # Omitida (cuota agotada, Tier 2 sin rol permitido o PARO
                    # TOTAL): no es exito ni fallo, no se registra en la BD, no
                    # genera evento ni callback.
                    with self._lock:
                        self.progreso["omitidas"] = (
                            self.progreso.get("omitidas", 0) + 1
                        )
                    if str(detalle) == MENSAJE_TIER2_SIN_ROL:
                        self._contar_tier2_sin_rol(usuario_res, resumen)
                    elif str(detalle) == MENSAJE_CANCELADO:
                        with self._lock:
                            resumen["omitidas_por_cancelacion"] = (
                                resumen.get("omitidas_por_cancelacion", 0) + 1
                            )
                    else:
                        with self._lock:
                            resumen["omitidas_por_cuota"] = (
                                resumen.get("omitidas_por_cuota", 0) + 1
                            )
                    return
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["exitosas" if ok else "fallidas"] += 1
                    resumen["exitosas" if ok else "fallidas"] += 1
                    if rol_res in resumen["por_rol"]:
                        if roles_aleatorios:
                            resumen["por_rol"][rol_res]["total"] += 1
                        resumen["por_rol"][rol_res][
                            "exitosas" if ok else "fallidas"
                        ] += 1
                    self._registrar_evento_locked(
                        usuario_res, ok, detalle, ronda, rol_res, url
                    )
                    registrar_accion(
                        usuario_res,
                        tipo_registro_rol(rol_res),
                        "exito" if ok else "fallido",
                        url,
                        detalle,
                    )
                    resumen["detalles"].append({
                        "usuario": usuario_res,
                        "rol": rol_res,
                        "ok": ok,
                        "detalle": detalle,
                        "url": url,
                        "ronda": ronda,
                    })
                    if callback:
                        callback(
                            self.progreso["hechas"],
                            max(len(ejecutables), self.progreso["hechas"]),
                            usuario_res,
                            ok,
                        )

            resumen["rondas"] = self._bucle_rondas(
                ejecutables,
                duracion_min,
                _generar_textos_ronda,
                _ejecutar_una_rol,
                _reportar_rol,
                cooldown_min=cooldown_val,
                porcentaje_min_ronda=porcentaje_min_ronda,
                porcentaje_max_ronda=porcentaje_max_ronda,
                elegibilidad=self._elegibilidad_curva(curva_activa),
                sustituir=(
                    lambda: self._rotar_agotadas_dia(
                        ejecutables, reservas, rol_por_usuario,
                        bool(roles_aleatorios), resumen,
                        None if roles_aleatorios else grupos_ejec,
                    )
                ),
            )
            try:
                fase = 2 if self._en_fase2() else 1
                with self._lock:
                    self.progreso["fase_actual"] = fase
                resumen["fase_actual"] = fase
            except Exception:
                pass
            logger.info(
                f"Activacion por roles finalizada (rondas): "
                f"{resumen['exitosas']} exitosas, {resumen['fallidas']} fallidas "
                f"en {resumen['rondas']} ronda(s) "
                f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión, "
                f"{resumen['sin_registro']} sin registro)"
            )
            return self._claves_cuotas(resumen)

        # --- Una sola pasada (con curva: DOS etapas secuenciales) ---------- #
        # Rotacion previa de las "Agotadas por hoy" (1:1, mismo rol en modo
        # fijo; una reserva Tier 2 jamas sustituye hashtags).
        self._rotar_agotadas_dia(
            ejecutables, reservas, rol_por_usuario,
            bool(roles_aleatorios), resumen,
            None if roles_aleatorios else grupos_ejec,
        )

        omitidas_por_cuota = 0
        omitidas_por_cancelacion = 0

        def _pasada_rol_simple(cuentas_pasada, duracion_pasada):
            """Ejecuta UNA pasada por roles sobre `cuentas_pasada`."""
            nonlocal omitidas_por_cuota, omitidas_por_cancelacion
            if not cuentas_pasada or self._cancelado():
                return
            if roles_aleatorios:
                roles_pasada = self._asignar_roles_aleatorios(
                    cuentas_pasada, roles_sortear, cuotas=self._cuotas
                )
                for candidata in cuentas_pasada:
                    if roles_pasada.get(candidata.usuario, ""):
                        continue
                    if not _es_tier2(candidata):
                        continue
                    if any(
                        _tier_permitido(candidata, r) for r in roles_sortear
                    ):
                        continue
                    self._contar_tier2_sin_rol(candidata.usuario, resumen)
                grupos_pasada = self._grupos_desde_roles(
                    cuentas_pasada, roles_pasada
                )
            else:
                deseados_pasada = {c.usuario for c in cuentas_pasada}
                roles_pasada = {
                    c.usuario: rol_por_usuario.get(c.usuario, "")
                    for c in cuentas_pasada
                }
                grupos_pasada = {
                    rol: [c for c in lista if c.usuario in deseados_pasada]
                    for rol, lista in grupos_ejec.items()
                }
            asignaciones = self._generar_textos_por_rol(
                grupos_pasada,
                texto_base,
                hashtags=hashtags,
                menciones=menciones,
                narrativa=narrativa,
                entrenamiento=entrenamiento,
                contexto=contexto,
                ancla_textos=ancla_textos,
            )
            try:
                duracion_efectiva = max(1, int(duracion_pasada or 1))
            except Exception:
                duracion_efectiva = 1
            bloques = self._distribuir_cohortes(
                list(cuentas_pasada), duracion_efectiva, cohortes
            )
            intervalo_pasada = max(
                1, (duracion_efectiva * 60) // max(cohortes, 1)
            )
            with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
                futuros = []
                for idx, bloque in enumerate(bloques):
                    # Paro total: no encolar mas tareas.
                    if self._cancelado():
                        break
                    for cuenta in bloque:
                        if self._cancelado():
                            break
                        rol = roles_pasada.get(cuenta.usuario, "")
                        if not rol:
                            # Sin rol efectivo: cuota agotada (modo aleatorio)
                            # o Tier 2 sin ningun rol permitido. No abre nada.
                            with self._lock:
                                self.progreso["omitidas"] = (
                                    self.progreso.get("omitidas", 0) + 1
                                )
                            if _es_tier2(cuenta) and not any(
                                _tier_permitido(cuenta, r)
                                for r in roles_sortear
                            ):
                                self._contar_tier2_sin_rol(
                                    cuenta.usuario, resumen
                                )
                            else:
                                omitidas_por_cuota += 1
                            continue
                        if (
                            self._cuotas is not None
                            and not self._cuotas.rol_permitido(
                                cuenta.usuario, rol or ""
                            )
                        ):
                            # Omitida ANTES de encolar (la garantia atomica la
                            # da la reserva interna de `_ejecutar_accion_rol`).
                            self._avisar_cuota_agotada(cuenta.usuario)
                            with self._lock:
                                self.progreso["omitidas"] = (
                                    self.progreso.get("omitidas", 0) + 1
                                )
                            omitidas_por_cuota += 1
                            continue
                        retardo = idx * intervalo_pasada + random.uniform(0, 15)
                        futuros.append((retardo, pool_exec.submit(
                            self._ejecutar_accion_rol, cuenta, rol, urls,
                            asignaciones.get(cuenta.usuario, ""), dar_like,
                            retardo,
                        ), cuenta.usuario))

                for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                    if self._cancelado():
                        break
                    try:
                        usuario_res, rol_res, ok, detalle, url = futuro.result()
                    except Exception as e:
                        usuario_res = usuario
                        rol_res = roles_pasada.get(usuario, "")
                        ok, detalle, url = False, str(e)[:80], ""

                    if ok:
                        self._capturar_url_fase1(usuario_res, rol_res, url)
                    if ok is None:
                        # Omitida (cuota, Tier 2 sin rol permitido o PARO
                        # TOTAL): no es exito ni fallo ni toca la BD.
                        with self._lock:
                            self.progreso["omitidas"] = (
                                self.progreso.get("omitidas", 0) + 1
                            )
                        if str(detalle) == MENSAJE_TIER2_SIN_ROL:
                            self._contar_tier2_sin_rol(usuario_res, resumen)
                        elif str(detalle) == MENSAJE_CANCELADO:
                            omitidas_por_cancelacion += 1
                        else:
                            omitidas_por_cuota += 1
                        continue

                    with self._lock:
                        self.progreso["hechas"] += 1
                        self.progreso["exitosas" if ok else "fallidas"] += 1
                        resumen["exitosas" if ok else "fallidas"] += 1
                        if rol_res in resumen["por_rol"]:
                            if roles_aleatorios:
                                resumen["por_rol"][rol_res]["total"] += 1
                            resumen["por_rol"][rol_res][
                                "exitosas" if ok else "fallidas"
                            ] += 1
                        self._registrar_evento_locked(
                            usuario_res, ok, detalle, 1, rol_res, url
                        )
                        registrar_accion(
                            usuario_res,
                            tipo_registro_rol(rol_res),
                            "exito" if ok else "fallido",
                            url,
                            detalle,
                        )
                        resumen["detalles"].append({
                            "usuario": usuario_res,
                            "rol": rol_res,
                            "ok": ok,
                            "detalle": detalle,
                            "url": url,
                        })
                        if callback:
                            callback(
                                self.progreso["hechas"], len(procesables),
                                usuario_res, ok,
                            )

        fase_final = 2 if self._en_fase2() else 1
        if curva_activa:
            # DOS ETAPAS secuenciales: primero Tier 1 (fase 1, acotada por
            # `fase1_min`) y despues Tier 2 + sin tier (fase 2). Misma
            # concurrencia y mismos reportes que el flujo normal.
            tier1_pasada = [
                c for c in ejecutables
                if self._tier_de.get(str(c.usuario)) == "tier1"
            ]
            resto_pasada = [
                c for c in ejecutables
                if self._tier_de.get(str(c.usuario)) != "tier1"
            ]
            if tier1_pasada and resto_pasada:
                with self._lock:
                    self.progreso["fase_actual"] = 1
                _pasada_rol_simple(
                    tier1_pasada,
                    max(1, min(int(duracion_min or 1), int(fase1_min))),
                )
                with self._lock:
                    self.progreso["fase_actual"] = 2
                # La fase 2 REAL empieza aqui (aunque la etapa 1 haya terminado
                # antes del reloj): la cascada de URLs de Tier 1 ya aplica.
                self._curva_fase2_t0 = time.monotonic() - 1.0
                _pasada_rol_simple(
                    resto_pasada,
                    max(1, int(duracion_min or 1) - int(fase1_min)),
                )
                fase_final = 2
            else:
                _pasada_rol_simple(list(ejecutables), duracion_min)
        else:
            _pasada_rol_simple(list(ejecutables), duracion_min)

        resumen["omitidas_por_cuota"] = omitidas_por_cuota
        resumen["omitidas_por_cancelacion"] = omitidas_por_cancelacion
        resumen["fase_actual"] = fase_final
        logger.info(
            f"Activacion por roles finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión)"
        )
        return self._claves_cuotas(resumen)

    def _ejecutar_campana_una_cuenta(self, cuenta: Cuenta, acciones: list,
                                     retardo: float = 0,
                                     dar_like: bool = False) -> list:
        """Ejecuta en secuencia las 9 acciones de campana de UNA cuenta.

        Cada elemento de ``acciones`` es ``(rol, urls, texto)`` con rol en
        ("hashtags", "comentario", "rt"). Delega cada slot en
        :meth:`_ejecutar_accion_rol` con una pausa corta entre acciones para
        no abrir navegadores en rafaga. Los slots sin texto (post/comentario)
        se reportan como fallo sin abrir navegador. Nunca lanza.
        """
        if retardo > 0:
            try:
                time.sleep(retardo)
            except Exception:
                pass
        resultados = []
        total = len(acciones or [])
        for indice, pieza in enumerate(acciones or []):
            try:
                if isinstance(pieza, dict):
                    rol = pieza.get("rol") or pieza.get("tipo") or ""
                    urls = pieza.get("urls") or pieza.get("url") or []
                    texto = pieza.get("texto") or ""
                else:
                    rol, urls, texto = pieza
                if isinstance(urls, str):
                    urls = [urls] if urls.strip() else []
                else:
                    try:
                        urls = [u for u in (urls or []) if str(u or "").strip()]
                    except TypeError:
                        urls = []
                if rol in ("hashtags", "comentario", "cita", "post"):
                    if not str(texto or "").strip():
                        url_ref = urls[0] if urls else ""
                        resultados.append(
                            (cuenta.usuario, rol, False, "sin texto asignado", url_ref)
                        )
                        continue
                resultados.append(
                    self._ejecutar_accion_rol(cuenta, rol, urls, texto or "", dar_like, 0)
                )
            except Exception as e:
                try:
                    rol_fallo = pieza[0] if not isinstance(pieza, dict) else "campana"
                except Exception:
                    rol_fallo = "campana"
                resultados.append(
                    (cuenta.usuario, rol_fallo, False,
                     f"{type(e).__name__}: {e}"[:120], "")
                )
            try:
                if indice < total - 1:
                    time.sleep(random.uniform(5.0, 15.0))
            except Exception:
                pass
        return resultados

    def ejecutar_campana_3_3_3(
        self,
        urls_rt: list[str],
        urls_comentarios: list[str] | None = None,
        textos_posts_por_cuenta: dict | None = None,
        textos_comentarios_por_cuenta: dict | None = None,
        usuarios: list | None = None,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        callback=None,
    ) -> dict:
        """Campana 3+3+3 (wrapper): cierra pestañas y agrega claves al resumen.

        La logica vive en `_ejecutar_campana_3_3_3_impl` (misma firma).
        """
        inicio = time.monotonic()
        try:
            resumen = self._ejecutar_campana_3_3_3_impl(
                urls_rt, urls_comentarios, textos_posts_por_cuenta,
                textos_comentarios_por_cuenta, usuarios, cantidad_cuentas,
                tags, grupo, dar_like, duracion_min, cohortes, callback,
            )
        finally:
            self._cerrar_pestanas()
        self._loguear_rendimiento(inicio, resumen)
        return self._con_claves_pestana(resumen)

    def _ejecutar_campana_3_3_3_impl(
        self,
        urls_rt: list[str],
        urls_comentarios: list[str] | None = None,
        textos_posts_por_cuenta: dict | None = None,
        textos_comentarios_por_cuenta: dict | None = None,
        usuarios: list | None = None,
        cantidad_cuentas: int = None,
        tags: list[str] = None,
        grupo: str = None,
        dar_like: bool = False,
        duracion_min: int = 60,
        cohortes: int = 4,
        callback=None,
    ) -> dict:
        """Campana 3+3+3: 3 posts + 3 comentarios + 3 RTs por cuenta.

        Cada cuenta con sesion ejecuta 9 acciones con textos distintos por
        slot (los provee quien llama, normalmente
        ``ia.generar_pool_campana_por_cuenta``): los posts usan el rol
        "hashtags" de :meth:`_ejecutar_accion_rol`, los comentarios el rol
        "comentario" (respuesta con url + texto) y los RTs el rol "rt" sobre
        el tweet principal. Las cuentas sin ninguna credencial de sesion se
        filtran con el mismo criterio de :func:`_partir_por_sesion` sin abrir
        navegador y sus 9 slots se cuentan como fallidos con la sugerencia
        de sesion. El arranque se reparte en cohortes temporales con
        concurrencia limitada y cada cuenta corre sus 9 acciones en
        secuencia. No modifica :meth:`ejecutar` ni
        :meth:`ejecutar_por_roles`. Nunca lanza por cuenta: los fallos van
        en ``detalles``.
        """
        # Cuotas horarias: cada campana arranca limpia y las prepara con las
        # cuentas ejecutables (mas abajo).
        self._cuotas = None
        self._reset_curva_campana()
        try:
            urls_rt_limpias = [
                str(u).strip() for u in (urls_rt or []) if str(u or "").strip()
            ]
        except Exception:
            urls_rt_limpias = []
        try:
            urls_com_limpias = [
                str(u).strip() for u in (urls_comentarios or []) if str(u or "").strip()
            ]
        except Exception:
            urls_com_limpias = []
        try:
            duracion = max(1, int(duracion_min or 60))
        except (TypeError, ValueError):
            duracion = 60
        try:
            n_cohortes = max(1, int(cohortes or 4))
        except (TypeError, ValueError):
            n_cohortes = 4
        pool_posts = textos_posts_por_cuenta if isinstance(textos_posts_por_cuenta, dict) else {}
        pool_coms = textos_comentarios_por_cuenta if isinstance(textos_comentarios_por_cuenta, dict) else {}

        cuentas = self._obtener_cuentas(cantidad_cuentas, tags, grupo)
        if usuarios:
            try:
                deseados = {
                    str(u).strip().lstrip("@").lower()
                    for u in usuarios if str(u or "").strip()
                }
            except Exception:
                deseados = set()
            cuentas = [
                c for c in cuentas
                if (c.usuario or "").strip().lstrip("@").lower() in deseados
            ]

        base_resumen = {
            "modo": "campana_3_3_3",
            "total": len(cuentas),
            "total_cuentas": len(cuentas),
            "total_acciones": len(cuentas) * 9,
            "exitosas": 0,
            "fallidas": 0,
            "por_tipo": {
                "post": {"total": 0, "exitosas": 0, "fallidas": 0},
                "comentario": {"total": 0, "exitosas": 0, "fallidas": 0},
                "retweet": {"total": 0, "exitosas": 0, "fallidas": 0},
            },
            "detalles": [],
            "sin_sesion": 0,
            "sin_sesion_usuarios": [],
            "sugerencia_sesion": "",
            "omitidas_por_cuota": 0,
        }
        if not cuentas:
            logger.warning("Campana 3+3+3: no hay cuentas activas de twitter")
            return self._claves_cuotas(base_resumen)

        con_sesion, sin_sesion = _partir_por_sesion(cuentas)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""
        base_resumen["sin_sesion"] = len(sin_sesion)
        base_resumen["sin_sesion_usuarios"] = sin_sesion_usuarios
        base_resumen["sugerencia_sesion"] = sugerencia

        mapa_tipo = {"hashtags": "post", "post": "post",
                     "comentario": "comentario", "rt": "retweet", "cita": "comentario"}
        with self._lock:
            self.progreso = {
                "hechas": 0,
                "exitosas": 0,
                "fallidas": 0,
                "omitidas": 0,
                "ronda_actual": 1,
                "fase_actual": 1,
                "eventos": [],
            }

        for cuenta in sin_sesion:
            mensaje_sin_sesion = _mensaje_sin_sesion(cuenta)[:120]
            for slot in range(9):
                if slot < 3:
                    rol_slot, tipo_slot = "hashtags", "post"
                elif slot < 6:
                    rol_slot, tipo_slot = "comentario", "comentario"
                else:
                    rol_slot, tipo_slot = "rt", "retweet"
                base_resumen["fallidas"] += 1
                base_resumen["por_tipo"][tipo_slot]["total"] += 1
                base_resumen["por_tipo"][tipo_slot]["fallidas"] += 1
                base_resumen["detalles"].append({
                    "usuario": cuenta.usuario,
                    "rol": rol_slot,
                    "tipo": tipo_slot,
                    "slot": slot % 3,
                    "ok": False,
                    "detalle": mensaje_sin_sesion,
                    "url": "",
                })
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["fallidas"] += 1
                    self._registrar_evento_locked(
                        cuenta.usuario, False, mensaje_sin_sesion, 1,
                        rol_slot, "",
                    )
                registrar_accion(
                    cuenta.usuario, "campana_3_3_3", "fallido", "",
                    mensaje_sin_sesion,
                )
                if callback:
                    try:
                        callback(self.progreso["hechas"], base_resumen["total_acciones"],
                                 cuenta.usuario, False)
                    except Exception:
                        pass
        if sin_sesion:
            logger.warning(
                f"Campana 3+3+3: {len(sin_sesion)} cuenta(s) sin sesión "
                f"({', '.join('@' + u for u in sin_sesion_usuarios)}). {sugerencia}"
            )
        if not con_sesion:
            logger.info(
                f"Campana 3+3+3 finalizada: 0 exitosas, "
                f"{base_resumen['fallidas']} fallidas de "
                f"{base_resumen['total_acciones']} ({base_resumen['sin_sesion']} sin sesión)"
            )
            return self._claves_cuotas(base_resumen)

        # Con cuentas ejecutables: preparar el contador de cuotas horarias
        # (base desde la BD + reservas de esta campana).
        self._preparar_cuotas([c.usuario for c in con_sesion])

        acciones_por_cuenta: dict = {}
        for cuenta in con_sesion:
            try:
                brutos_post = pool_posts.get(cuenta.usuario, [])
                if isinstance(brutos_post, str):
                    brutos_post = [brutos_post]
                lista_post = [str(t or "").strip() for t in (brutos_post or [])]
                lista_post = [t for t in lista_post if t][:3]
            except Exception:
                lista_post = []
            try:
                brutos_com = pool_coms.get(cuenta.usuario, [])
                if isinstance(brutos_com, str):
                    brutos_com = [brutos_com]
                lista_com = [str(t or "").strip() for t in (brutos_com or [])]
                lista_com = [t for t in lista_com if t][:3]
            except Exception:
                lista_com = []
            slots: list = []
            for i in range(3):
                slots.append(("hashtags", [], lista_post[i] if i < len(lista_post) else ""))
            for i in range(3):
                url_com = urls_com_limpias[i % len(urls_com_limpias)] if urls_com_limpias else ""
                slots.append(("comentario", [url_com] if url_com else [],
                              lista_com[i] if i < len(lista_com) else ""))
            for i in range(3):
                url_rt = urls_rt_limpias[i % len(urls_rt_limpias)] if urls_rt_limpias else ""
                slots.append(("rt", [url_rt] if url_rt else [], ""))
            try:
                random.shuffle(slots)
            except Exception:
                pass
            acciones_por_cuenta[cuenta.usuario] = slots

        bloques = self._distribuir_cohortes(list(con_sesion), duracion, n_cohortes)
        intervalo = max(1, (duracion * 60) // max(n_cohortes, 1))
        retardos = {}
        for idx, bloque in enumerate(bloques):
            for cuenta in bloque:
                try:
                    retardos[cuenta.usuario] = idx * intervalo + random.uniform(0, 15)
                except Exception:
                    retardos[cuenta.usuario] = float(idx * intervalo)

        logger.info(
            f"Campana 3+3+3: {len(con_sesion)} cuentas con sesión "
            f"({len(sin_sesion)} sin sesión saltadas), "
            f"{len(urls_rt_limpias)} urls RT, {len(urls_com_limpias)} urls comentario, "
            f"{n_cohortes} cohortes, {duracion} min, concurrencia {self.max_concurrente}"
        )

        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for cuenta in con_sesion:
                futuros.append((
                    retardos.get(cuenta.usuario, 0.0),
                    pool_exec.submit(
                        self._ejecutar_campana_una_cuenta, cuenta,
                        acciones_por_cuenta.get(cuenta.usuario, []),
                        retardos.get(cuenta.usuario, 0.0), dar_like,
                    ),
                    cuenta.usuario,
                ))
            for _, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    resultados = futuro.result()
                except Exception as e:
                    resultados = [(usuario, "campana", False, str(e)[:120], "")]
                for usuario_res, rol_res, ok, detalle, url in resultados:
                    if ok is None:
                        # Omitida por cuota agotada: no es exito ni fallo, no
                        # se registra en la BD ni dispara callback.
                        with self._lock:
                            base_resumen["omitidas_por_cuota"] = (
                                base_resumen.get("omitidas_por_cuota", 0) + 1
                            )
                            self.progreso["omitidas"] = (
                                self.progreso.get("omitidas", 0) + 1
                            )
                        continue
                    tipo = mapa_tipo.get(rol_res, rol_res)
                    if tipo not in base_resumen["por_tipo"]:
                        tipo = "post"
                    with self._lock:
                        self.progreso["hechas"] += 1
                        self.progreso["exitosas" if ok else "fallidas"] += 1
                        base_resumen["exitosas" if ok else "fallidas"] += 1
                        base_resumen["por_tipo"][tipo]["total"] += 1
                        base_resumen["por_tipo"][tipo]["exitosas" if ok else "fallidas"] += 1
                        self._registrar_evento_locked(
                            usuario_res, ok, detalle, 1, rol_res, url
                        )
                        registrar_accion(
                            usuario_res, tipo_registro_rol(rol_res),
                            "exito" if ok else "fallido", url, detalle,
                        )
                        base_resumen["detalles"].append({
                            "usuario": usuario_res,
                            "rol": rol_res,
                            "tipo": tipo,
                            "ok": ok,
                            "detalle": detalle,
                            "url": url,
                        })
                        if callback:
                            try:
                                callback(self.progreso["hechas"],
                                         base_resumen["total_acciones"], usuario_res, ok)
                            except Exception:
                                pass

        logger.info(
            f"Campana 3+3+3 finalizada: {base_resumen['exitosas']} exitosas, "
            f"{base_resumen['fallidas']} fallidas de {base_resumen['total_acciones']} "
            f"({base_resumen['sin_sesion']} sin sesión)"
        )
        return self._claves_cuotas(base_resumen)
