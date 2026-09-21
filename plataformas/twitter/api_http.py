import curl_cffi.requests as requests
import hashlib
import html as _html
import json
import pickle
import re
import threading
import time
import os
from typing import Optional
from loguru import logger

from core.config import resolver_ruta, settings
from utils.anti_detection import normalizar_cookies, resolver_ua_cuenta


def _env_activo(nombre: str, por_defecto: bool = False) -> bool:
    """Lee un booleano de entorno (0/false/no/off desactivan).

    Mismo criterio que ``plataformas.chrome_driver._env_activo``; se replica
    aqui para que este modulo HTTP no importe Selenium/undetected_chromedriver.
    """
    valor = os.environ.get(nombre)
    if valor is None or not valor.strip():
        return por_defecto
    return valor.strip().lower() not in ("0", "false", "no", "off")


def _env_float(nombre: str, por_defecto: float) -> float:
    """Lee un float de entorno (default ante ausencia/valor raro). Nunca lanza."""
    try:
        valor = os.environ.get(nombre)
        if valor is None or not str(valor).strip():
            return float(por_defecto)
        return float(str(valor).strip())
    except Exception:
        return float(por_defecto)


def _env_int(nombre: str, por_defecto: int) -> int:
    """Lee un int de entorno (default ante ausencia/valor raro). Nunca lanza."""
    try:
        return int(_env_float(nombre, por_defecto))
    except Exception:
        return int(por_defecto)


# --------------------------------------------------------------------------- #
# Descubrimiento/cache de queryIds GraphQL
# --------------------------------------------------------------------------- #
# Los queryId de la API interna de X cambian sin aviso (el viejo
# `ojPdsZsimiJrUGLR1sjUtA` de CreateRetweet ya devuelve 422). En vez de
# hardcodear uno vencido, `TwitterAPI._queryid` los DESCUBRE de los bundles JS
# de x.com, los cachea por proceso + archivo TTL 6h y los redescubre cuando X
# responde 400/422. El fallback conocido solo se usa si el descubrimiento
# falla (RT/like) o devuelve "" (CreateTweet/TweetResultByRestId: sin
# candidato fiable => el llamador debe usar descubrimiento).
_QUERYIDS_FALLBACK = {
    "CreateRetweet": "ojPdsZsimiJrUGLR1sjUtA",
    "FavoriteTweet": "lZ0GCEojmtQfiUQa5oJSEw",
}
_QUERYIDS_TTL_SEG = 6 * 3600
_QUERYIDS_PRESUPUESTO_SEG = 8.0
_QUERYIDS_MAX_BUNDLES = 3
_QUERYIDS_MEM: dict = {}
_QUERYIDS_LOCK = threading.Lock()
_QUERYIDS_DESCUBRIMIENTO_LOCK = threading.Lock()
_QUERYIDS_ARCHIVO = "data/twitter_queryids.json"

# Features del cliente web ACTUAL para los endpoints de ESCRITURA. X A/B
# rechaza con 422/validation los sets incompletos: antes CreateRetweet y
# FavoriteTweet mandaban solo `_FEATURES_BASICAS` (3 flags) y X los tumbaba
# ("must be defined graphql_validation_failed"). Se usa el mismo set completo
# en CreateRetweet/FavoriteTweet/CreateTweet (X tolera campos de mas).
_FEATURES_ESCRITURA = {
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    # Extras que ya mandaba CreateTweet (se conservan).
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "articles_preview_enabled": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
}
# Alias retrocompatibles: el codigo/tests viejos que importan las constantes
# anteriores siguen funcionando (ahora apuntan al set completo).
_FEATURES_CREATE_TWEET = _FEATURES_ESCRITURA
_FEATURES_BASICAS = _FEATURES_ESCRITURA


# --------------------------------------------------------------------------- #
# Disyuntor (circuit breaker) por grupo de rol + bloqueo por cuenta
# --------------------------------------------------------------------------- #
# Cuando X rechaza TODAS las acciones de un tipo (anti-bot 226, limite diario
# 344, 403/429/404, 422 persistente), seguir intentando en CADA cuenta cuesta
# segundos de proxy y NO arregla nada: en el log real cada cuenta pagaba el
# queryId + redescubrimiento + 422 antes de caer a Selenium. El disyuntor corta
# el grupo completo (`rt`, `like`, `tweet` = hashtags/post/comentario/cita)
# tras `API_BREAKER_FALLOS` fallos DUROS consecutivos durante
# `API_BREAKER_SEG` (default 600s). Las cuentas con 344/226 se bloquean ademas
# por `API_CUENTA_BLOQUEO_SEG` (default 600s).
_BREAKER_LOCK = threading.Lock()
_BREAKER_ESTADO: dict = {}
_API_CUENTAS_BLOQUEADAS: dict = {}


def _grupo_rol(rol: str) -> str:
    """Grupo de disyuntor del rol (`rt`/`like`/`tweet`; '' si no aplica)."""
    rol = str(rol or "").strip().lower()
    if rol in ("rt", "retweet"):
        return "rt"
    if rol in ("like",):
        return "like"
    if rol in ("hashtags", "post", "comentario", "cita"):
        return "tweet"
    return ""


def _grupo_operacion(operacion: str) -> str:
    """Grupo de disyuntor de una operacion GraphQL ('' si no aplica)."""
    operacion = str(operacion or "").strip()
    if operacion == "CreateRetweet":
        return "rt"
    if operacion == "FavoriteTweet":
        return "like"
    if operacion in ("CreateTweet", "TweetResultByRestId"):
        return "tweet"
    return ""


def _breaker_abierto(grupo: str) -> tuple:
    """`(abierto, motivo)` del disyuntor del grupo.

    Si la ventana ya vencio, lo cierra, limpia el contador y loguea el cierre.
    Nunca lanza: ante cualquier valor raro devuelve `(False, "")`.
    """
    try:
        grupo = str(grupo or "").strip().lower()
        if not grupo:
            return False, ""
        ahora = time.monotonic()
        with _BREAKER_LOCK:
            estado = _BREAKER_ESTADO.get(grupo)
            if not estado:
                return False, ""
            if float(estado.get("abierto_hasta", 0.0)) > ahora:
                return True, str(estado.get("motivo", ""))
            # Ventana vencida: se cierra (NO se borran los contadores antes de
            # tiempo; si el disyuntor nunca se abrio, los fallos consecutivos
            # deben seguir sumando).
            if estado.get("abierto_hasta"):
                logger.info(
                    f"disyuntor API '{grupo}' cerrado tras "
                    f"{_env_float('API_BREAKER_SEG', 600.0):.0f}s"
                )
                _BREAKER_ESTADO.pop(grupo, None)
        return False, ""
    except Exception:
        return False, ""


def _breaker_registrar_fallo(grupo: str, motivo: str = "") -> None:
    """Suma un fallo DURO; abre el disyuntor tras `API_BREAKER_FALLOS` seguidos.

    Los fallos "blandos" (red/curl/timeouts) NO deben llamar aqui. Log INFO al
    abrir. Nunca lanza.
    """
    try:
        grupo = str(grupo or "").strip().lower()
        if not grupo:
            return
        limite = max(1, _env_int("API_BREAKER_FALLOS", 3))
        duracion = max(1.0, _env_float("API_BREAKER_SEG", 600.0))
        motivo = str(motivo or "fallo duro")
        ahora = time.monotonic()
        abierto = False
        with _BREAKER_LOCK:
            estado = _BREAKER_ESTADO.get(grupo) or {}
            fallos = int(estado.get("fallos", 0)) + 1
            abierto_hasta = float(estado.get("abierto_hasta", 0.0))
            if abierto_hasta and abierto_hasta <= ahora and fallos > 1:
                # La ventana anterior ya vencio (p. ej. una llamada directa sin
                # consultar el disyuntor): se empieza de cero para exigir otra
                # vez `API_BREAKER_FALLOS` fallos consecutivos.
                fallos = 1
                estado["motivo"] = ""
            if fallos >= limite and abierto_hasta <= ahora:
                abierto_hasta = ahora + duracion
                estado["motivo"] = motivo
                abierto = True
            estado["fallos"] = fallos
            estado["abierto_hasta"] = abierto_hasta
            _BREAKER_ESTADO[grupo] = estado
        if abierto:
            logger.info(
                f"disyuntor API '{grupo}' ABIERTO {duracion:.0f}s tras "
                f"{limite} fallos duros: {motivo}"
            )
    except Exception:
        pass


def _breaker_registrar_exito(grupo: str) -> None:
    """Cierra el disyuntor del grupo y limpia los fallos consecutivos."""
    try:
        grupo = str(grupo or "").strip().lower()
        if not grupo:
            return
        with _BREAKER_LOCK:
            estado = _BREAKER_ESTADO.pop(grupo, None)
        if estado and estado.get("abierto_hasta"):
            logger.info(f"disyuntor API '{grupo}' cerrado por una accion exitosa")
    except Exception:
        pass


def _bloquear_cuenta(usuario: str, motivo: str = "") -> None:
    """Bloquea la cuenta `API_CUENTA_BLOQUEO_SEG` segundos (344/226)."""
    try:
        usuario = str(usuario or "").strip()
        if not usuario:
            return
        duracion = max(1.0, _env_float("API_CUENTA_BLOQUEO_SEG", 600.0))
        with _BREAKER_LOCK:
            _API_CUENTAS_BLOQUEADAS[usuario] = {
                "hasta": time.monotonic() + duracion,
                "motivo": str(motivo or ""),
            }
        logger.info(
            f"API: cuenta @{usuario} bloqueada {duracion:.0f}s por {motivo}"
        )
    except Exception:
        pass


def _cuenta_bloqueada(usuario: str) -> tuple:
    """`(bloqueada, motivo)` de la cuenta; limpia la entrada vencida."""
    try:
        usuario = str(usuario or "").strip()
        if not usuario:
            return False, ""
        ahora = time.monotonic()
        with _BREAKER_LOCK:
            estado = _API_CUENTAS_BLOQUEADAS.get(usuario)
            if not estado:
                return False, ""
            if float(estado.get("hasta", 0.0)) > ahora:
                return True, str(estado.get("motivo", ""))
            _API_CUENTAS_BLOQUEADAS.pop(usuario, None)
        return False, ""
    except Exception:
        return False, ""


def _texto_errores(data) -> str:
    """Texto normalizado de `errors` de GraphQL ('' si no hay). Nunca lanza."""
    try:
        errores = data.get("errors") if isinstance(data, dict) else None
        if not errores:
            return ""
        partes = []
        for err in errores:
            if not isinstance(err, dict):
                continue
            partes.append(str(err.get("message") or ""))
            codigo = err.get("code")
            if codigo is None:
                extensiones = err.get("extensions")
                if isinstance(extensiones, dict):
                    codigo = extensiones.get("code")
            if codigo is not None:
                partes.append(str(codigo))
        return " ".join(partes).lower()
    except Exception:
        return ""


def _clasificar_fallo(status, data) -> tuple:
    """`(es_duro, motivo_corto, bloquear_cuenta)` de un fallo de la API de X.

    Fallos DUROS: 226/anti-bot ("looks like it might be automated"),
    344/limite diario, 403, 429, 404 y 422 persistente. Los de red (status 0,
    timeouts de curl) NO son duros: son transitorios y no abren el disyuntor.
    Nunca lanza.
    """
    try:
        texto = _texto_errores(data)
    except Exception:
        texto = ""
    try:
        if status == 226 or "automated" in texto or "might be automated" in texto:
            return True, "226/anti-bot", True
        if "344" in texto or "daily limit" in texto or "limite diario" in texto:
            return True, "344/limite diario", True
        if status == 403:
            return True, "403", False
        if status == 429:
            return True, "429", False
        if status == 404:
            return True, "404", False
        if status == 422:
            return True, "422", False
    except Exception:
        return False, "", False
    return False, "", False


# --------------------------------------------------------------------------- #
# Texto REAL de un tweet (sin cuenta) para el contexto de los comentarios
# --------------------------------------------------------------------------- #
_TEXTO_TWEET_CACHE: dict = {}
_TEXTO_TWEET_LOCK = threading.Lock()
_TEXTO_TWEET_MAX = 500
_TEXTO_TWEET_CACHE_MAX = 2000


def _tweet_id_de_url(url: str) -> str:
    """Id numerico de un enlace `/status/<id>` ('' si no hay). Nunca lanza."""
    try:
        match = re.search(r"/status(?:es)?/(\d+)", str(url or ""))
        return match.group(1) if match else ""
    except Exception:
        return ""


def _texto_plano(valor, limite: int = _TEXTO_TWEET_MAX) -> str:
    """HTML/JSON de un tweet -> texto plano (sin links ni etiquetas)."""
    try:
        texto = _html.unescape(str(valor or ""))
        texto = re.sub(r"<[^>]+>", " ", texto)
        texto = re.sub(r"https?://\S+", "", texto)
        texto = re.sub(r"\s+", " ", texto).strip()
        return texto[:limite]
    except Exception:
        return ""


def obtener_texto_tweet(url: str) -> str:
    """Texto REAL del tweet ancla (sin cuenta) para dar contexto a los comentarios.

    1. CDN de sindicacion de X:
       `cdn.syndication.twimg.com/tweet-result?id=<id>&lang=es` (JSON con `text`).
    2. Fallback oEmbed de `publish.twitter.com` (HTML embebido del tweet).

    Cache en memoria por id (una sola descarga por tweet en todo el proceso),
    timeout de 8s y NUNCA lanza: devuelve "" si no se pudo obtener.
    """
    tweet_id = _tweet_id_de_url(url)
    if not tweet_id:
        return ""
    with _TEXTO_TWEET_LOCK:
        cacheado = _TEXTO_TWEET_CACHE.get(tweet_id)
    if cacheado is not None:
        return cacheado

    texto = ""
    try:
        resp = requests.get(
            "https://cdn.syndication.twimg.com/tweet-result"
            f"?id={tweet_id}&lang=es",
            timeout=8,
        )
        if getattr(resp, "status_code", 0) == 200:
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict):
                texto = _texto_plano(
                    data.get("text") or data.get("full_text") or ""
                )
    except Exception as e:
        logger.debug(
            f"obtener_texto_tweet: syndication fallo para {tweet_id} "
            f"({type(e).__name__}: {e})"
        )

    if not texto:
        try:
            resp = requests.get(
                "https://publish.twitter.com/oembed",
                params={"url": url, "omit_script": 1},
                timeout=8,
            )
            if getattr(resp, "status_code", 0) == 200:
                data = resp.json()
                if isinstance(data, dict):
                    texto = _texto_plano(data.get("html") or "")
        except Exception as e:
            logger.debug(
                f"obtener_texto_tweet: oembed fallo para {tweet_id} "
                f"({type(e).__name__}: {e})"
            )

    with _TEXTO_TWEET_LOCK:
        _TEXTO_TWEET_CACHE[tweet_id] = texto
        if len(_TEXTO_TWEET_CACHE) > _TEXTO_TWEET_CACHE_MAX:
            for clave in list(_TEXTO_TWEET_CACHE)[: _TEXTO_TWEET_CACHE_MAX // 2]:
                _TEXTO_TWEET_CACHE.pop(clave, None)
    return texto


class TwitterAPI:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.session = None
        self.bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        # Presupuesto total de red de la instancia (API_TIMEOUT_SEG, default
        # 10s): `_post_json` lo respeta por peticion para que una accion no
        # siga esperando indefinidamente. `accion_rapida` lo reinicia por accion.
        self._deadline = time.monotonic() + max(
            1.0, _env_float("API_TIMEOUT_SEG", 10.0)
        )

    def _leer_cuenta(self):
        """Devuelve la fila `Cuenta` de la BD (o None) para leer su UA/cookies."""
        try:
            from core.database import get_db_session
            from core.models import Cuenta
            with get_db_session() as db:
                return db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
        except Exception as e:
            logger.warning(f"No se pudo leer la cuenta {self.usuario}: {e}")
            return None

    def _cargar_cookies(self) -> bool:
        cuenta = self._leer_cuenta()

        cookies_list = None
        if os.path.exists(self.cookies_path):
            try:
                with open(self.cookies_path, "rb") as f:
                    cookies_list = pickle.load(f)
            except Exception as e:
                logger.error(f"Error leyendo {self.cookies_path}: {e}")

        # Sin .pkl (o ilegible): usa TODAS las cookies guardadas en la BD.
        if not cookies_list and cuenta is not None:
            cookies_json = getattr(cuenta, "cookies_json", None)
            if cookies_json:
                logger.info(f"Sin .pkl para {self.usuario}; usando cookies_json de la BD")
                cookies_list = cookies_json

        cookies_norm = normalizar_cookies(cookies_list)
        if not cookies_norm:
            # Cuentas importadas SOLO con `Cuenta.auth_token` (sin .pkl ni
            # cookies_json): la cookie minima de auth_token basta para que X
            # emita ct0 al cargar x.com (`asegurar_ct0`). Antes se descartaban
            # con "No hay cookies" y caian directo a Selenium.
            cookies_norm = self._cookies_auth_token(cuenta)
        if not cookies_norm:
            logger.warning(f"No hay cookies para {self.usuario}")
            return False

        try:
            self.session = requests.Session(impersonate="chrome120")

            # Proxy de la cuenta (o sticky MX determinista) salvo que
            # TWITTER_SIN_PROXY este activo: sin proxy se usa la IP del
            # servidor. Cualquier fallo al configurarlo NO rompe la carga.
            proxy = self._proxy_para_api(cuenta)
            if proxy:
                try:
                    self.session.proxies = {"http": proxy, "https": proxy}
                    logger.info(f"API HTTP de {self.usuario} via proxy")
                except Exception as e:
                    logger.warning(
                        f"No se pudo configurar el proxy de {self.usuario}: {e}"
                    )

            cookie_dict = {c["name"]: c["value"] for c in cookies_norm}
            self.session.cookies.update(cookie_dict)

            ct0 = cookie_dict.get("ct0", "")
            auth_token = cookie_dict.get("auth_token", "")

            headers = {
                "authorization": f"Bearer {self.bearer_token}",
                "x-twitter-auth-type": "OAuth2Session",
                "x-twitter-active-user": "yes",
                "referer": "https://x.com/",
                "origin": "https://x.com",
            }
            if ct0:
                headers["x-csrf-token"] = ct0

            # UA EXACTO de la cuenta (del lote). Si la cuenta no lo tiene, se
            # omite el header y curl_cffi usa el UA natural de su impersonacion.
            ua_cuenta = resolver_ua_cuenta(cuenta)
            if ua_cuenta:
                headers["user-agent"] = ua_cuenta
            else:
                logger.info(f"{self.usuario} sin user_agent; curl_cffi usara su UA natural")

            self.session.headers.update(headers)

            # Las cuentas importadas solo con auth_token no traen ct0: X lo
            # emite al cargar x.com. Sin ct0 la API interna rechaza RT/like,
            # asi que se obtiene y se persiste aqui mismo.
            if not ct0:
                self.asegurar_ct0()

            return True

        except Exception as e:
            logger.error(f"Error cargando cookies: {e}")
            return False

    def _cookies_auth_token(self, cuenta) -> list:
        """Cookie minima de `auth_token` normalizada ([] si no hay token).

        X emite `ct0` al cargar x.com con una sesion valida, asi que con el
        `auth_token` de la BD la API HTTP puede operar aunque la cuenta no
        tenga `.pkl` ni `cookies_json`. Acepta `Cuenta` o dict. Nunca lanza.
        """
        try:
            if isinstance(cuenta, dict):
                token = cuenta.get("auth_token")
            else:
                token = getattr(cuenta, "auth_token", "")
            if not isinstance(token, str):
                token = str(token or "")
            token = token.strip()
            if not token:
                return []
            logger.info(
                f"{self.usuario} solo con auth_token; se usa la cookie minima "
                f"(asegurar_ct0 obtendra el ct0)"
            )
            return normalizar_cookies([{
                "name": "auth_token",
                "value": token,
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }])
        except Exception as e:
            logger.debug(f"No se pudo armar la cookie auth_token: {e}")
            return []

    def _proxy_para_api(self, cuenta=None) -> str:
        """Proxy para la sesion HTTP ('' = sin proxy).

        `TWITTER_SIN_PROXY` activo => IP del servidor (sin proxy ni
        validacion). Si no: `Cuenta.proxy`; si esta vacio, el sticky MX
        deterministico por cuenta (mismo criterio que
        `TwitterBot._obtener_proxy`). Nunca lanza.
        """
        if _env_activo("TWITTER_SIN_PROXY", False):
            logger.info(f"TWITTER_SIN_PROXY activo: API de {self.usuario} sin proxy")
            return ""
        proxy = (getattr(cuenta, "proxy", "") or "").strip()
        if proxy:
            return proxy
        try:
            return settings.proxy_sticky_mx(
                session_id=hashlib.md5(self.usuario.encode()).hexdigest()[:8]
            )
        except Exception:
            return ""

    def asegurar_ct0(self) -> bool:
        """Asegura que la sesion HTTP tenga `ct0` (token CSRF) para la API de X.

        1. Si ya hay `ct0` en las cookies de la sesion, actualiza el header
           `x-csrf-token` y devuelve True.
        2. Si no, con la sesion ya configurada (cookies + UA + proxy) hace
           `GET https://x.com/` con headers de navegador y busca `ct0` en las
           cookies de la respuesta (o en su `Set-Cookie`).
        3. Si aparece, actualiza el header y lo PERSISTE en
           `Cuenta.cookies_json` (con `auth_token` si falta) para que las
           siguientes corridas no vuelvan a pedirlo.

        Nunca lanza: cualquier fallo devuelve False.
        """
        try:
            if self.session is None:
                return False

            ct0 = self._ct0_de_sesion()
            if ct0:
                self._actualizar_header_ct0(ct0)
                return True

            logger.info(f"Sin ct0 para {self.usuario}; obteniendo de x.com")
            # Con los headers de API (Bearer) x.com responde 401 al HTML y no
            # emite ct0: `_get_web` los quita solo para este GET.
            resp = self._get_web(
                "https://x.com/", 20, extra_headers={"x-twitter-active-user": "yes"}
            )

            ct0 = self._ct0_de_respuesta(resp)
            if not ct0:
                logger.warning(f"No se pudo obtener ct0 de x.com para {self.usuario}")
                return False

            self._actualizar_header_ct0(ct0)
            self._persistir_ct0(ct0)
            logger.info(f"ct0 obtenido y guardado para {self.usuario}")
            return True
        except Exception as e:
            logger.warning(
                f"No se pudo asegurar ct0 para {self.usuario}: "
                f"{type(e).__name__}: {e}"
            )
            return False

    def _ct0_de_sesion(self) -> str:
        """`ct0` de las cookies de la sesion ('' si no hay). Nunca lanza."""
        try:
            valor = self.session.cookies.get("ct0")
            return str(valor).strip() if valor else ""
        except Exception:
            return ""

    @staticmethod
    def _ct0_de_respuesta(resp) -> str:
        """`ct0` de las cookies/Set-Cookie de una respuesta ('' si no hay)."""
        try:
            cookies = getattr(resp, "cookies", None)
            if cookies is not None:
                valor = cookies.get("ct0")
                if valor:
                    return str(valor).strip()
        except Exception:
            pass
        try:
            set_cookie = str(resp.headers.get("set-cookie", "") or "")
        except Exception:
            set_cookie = ""
        import re

        match = re.search(r"(?:^|[,\s])ct0=([^;,\s]+)", set_cookie)
        return match.group(1).strip() if match else ""

    def _actualizar_header_ct0(self, ct0: str) -> None:
        """Actualiza el header `x-csrf-token` de la sesion (nunca lanza)."""
        try:
            self.session.headers["x-csrf-token"] = ct0
        except Exception as e:
            logger.debug(f"No se pudo actualizar x-csrf-token: {e}")

    def _persistir_ct0(self, ct0: str) -> None:
        """Persiste `ct0` (y `auth_token` si falta) en `Cuenta.cookies_json`.

        Añade a la lista existente el ct0 fresco y, si la sesion tiene
        `auth_token` y la cuenta no lo guardaba, tambien lo escribe. El formato
        replica el de `TwitterBot._guardar_cookies_json` (lista de dicts de
        cookie) para no acoplar el flujo HTTP a Selenium. Nunca lanza.
        """
        try:
            from core.database import get_db_session
            from core.models import Cuenta

            auth_token = ""
            try:
                auth_token = self.session.cookies.get("auth_token") or ""
            except Exception:
                auth_token = ""

            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == self.usuario).first()
                if cuenta is None:
                    return

                existentes = self._cookies_a_lista(getattr(cuenta, "cookies_json", None))
                por_nombre = {
                    c.get("name"): c for c in existentes if c.get("name")
                }
                lista = [c for c in existentes if c.get("name") != "ct0"]
                if auth_token and not (por_nombre.get("auth_token") or {}).get("value"):
                    lista.insert(0, {
                        "name": "auth_token",
                        "value": auth_token,
                        "domain": ".x.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    })
                lista.append({
                    "name": "ct0",
                    "value": ct0,
                    "domain": ".x.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                })
                cuenta.cookies_json = lista
                if auth_token:
                    cuenta.auth_token = auth_token
        except Exception as e:
            logger.warning(f"No se pudo guardar el ct0 de {self.usuario}: {e}")

    @staticmethod
    def _cookies_a_lista(cookies) -> list:
        """Normaliza `cookies_json` (None/str/dict/list) a lista de dicts."""
        if not cookies:
            return []
        if isinstance(cookies, str):
            try:
                cookies = json.loads(cookies)
            except Exception:
                return []
        if isinstance(cookies, dict):
            cookies = cookies.get("cookies") or []
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if isinstance(c, dict)]

    # ------------------------------------------------------------------ #
    # queryIds GraphQL: cache por proceso/archivo + descubrimiento
    # ------------------------------------------------------------------ #

    @staticmethod
    def _queryid_memoria(operacion: str) -> str:
        """queryId cacheado por proceso y vigente ('' si no hay). Nunca lanza."""
        try:
            with _QUERYIDS_LOCK:
                entrada = _QUERYIDS_MEM.get(operacion)
            if not entrada:
                return ""
            queryid, expira = entrada
            if expira > time.time() and queryid:
                return str(queryid)
            return ""
        except Exception:
            return ""

    @staticmethod
    def _guardar_queryid_memoria(operacion: str, queryid: str) -> None:
        try:
            with _QUERYIDS_LOCK:
                _QUERYIDS_MEM[operacion] = (str(queryid), time.time() + _QUERYIDS_TTL_SEG)
        except Exception:
            pass

    @staticmethod
    def _leer_queryids_archivo() -> dict:
        """Contenido del archivo de cache ({} si no existe/ilegible)."""
        try:
            ruta = resolver_ruta(_QUERYIDS_ARCHIVO)
            if not os.path.exists(ruta):
                return {}
            with open(ruta, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _queryid_archivo(self, operacion: str) -> str:
        """queryId del archivo compartido si sigue vigente (TTL 6h)."""
        try:
            entrada = self._leer_queryids_archivo().get(operacion)
            if isinstance(entrada, dict):
                queryid = str(entrada.get("id") or "")
                timestamp = float(entrada.get("ts") or 0)
                if queryid and (time.time() - timestamp) < _QUERYIDS_TTL_SEG:
                    return queryid
            elif isinstance(entrada, str) and entrada:
                # Formato viejo/comodo: {operacion: "queryId"} (sin TTL).
                return entrada
        except Exception:
            pass
        return ""

    @staticmethod
    def _escribir_queryids_archivo(data: dict) -> None:
        """Escritura ATOMICA del cache compartido (temp file + `os.replace`).

        Antes `json.dump` escribia directo sobre el archivo y dos workers
        concurrentes lo dejaban a medias ("Extra data: line 6 column 2"): el
        queryId malo persistia y `_invalidar_queryid` no podia leerlo. Ahora el
        lector siempre ve un JSON completo. Nunca lanza: best-effort.
        """
        try:
            if not isinstance(data, dict):
                data = {}
            ruta = resolver_ruta(_QUERYIDS_ARCHIVO)
            carpeta = os.path.dirname(ruta)
            if carpeta:
                os.makedirs(carpeta, exist_ok=True)
            tmp = f"{ruta}.tmp{os.getpid()}_{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ruta)
        except Exception as e:
            logger.debug(f"No se pudo escribir el cache de queryIds: {e}")

    @staticmethod
    def _guardar_queryid_archivo(operacion: str, queryid: str) -> None:
        """Escribe el cache compartido bajo lock y de forma atomica."""
        try:
            with _QUERYIDS_LOCK:
                data = TwitterAPI._leer_queryids_archivo()
                if not isinstance(data, dict):
                    data = {}
                data[operacion] = {"id": str(queryid), "ts": time.time()}
                TwitterAPI._escribir_queryids_archivo(data)
        except Exception as e:
            logger.debug(f"No se pudo guardar el cache de queryIds: {e}")

    def _guardar_queryid(self, operacion: str, queryid: str) -> None:
        """Guarda el queryId en memoria y en el archivo compartido."""
        self._guardar_queryid_memoria(operacion, queryid)
        self._guardar_queryid_archivo(operacion, queryid)

    def _invalidar_queryid(self, operacion: str) -> None:
        """Invalida el queryId (memoria + archivo) tras un 400/422 de X.

        Bajo `_QUERYIDS_LOCK` y con escritura atomica. Tolera el archivo
        corrupto (`json.load` falla => `{}`): lo REESCRIBE sano y sin la
        operacion, para que el queryId malo no persista. Nunca lanza.
        """
        try:
            with _QUERYIDS_LOCK:
                _QUERYIDS_MEM.pop(operacion, None)
        except Exception:
            pass
        try:
            with _QUERYIDS_LOCK:
                data = self._leer_queryids_archivo()
                if not isinstance(data, dict):
                    data = {}
                data.pop(operacion, None)
                self._escribir_queryids_archivo(data)
        except Exception as e:
            logger.debug(f"No se pudo invalidar el queryId de {operacion}: {e}")

    @staticmethod
    def _bundles_de_html(html: str) -> list:
        """URLs de bundles JS del cliente web de X (deduplicadas, en orden)."""
        urls, vistos = [], set()
        try:
            for match in re.findall(
                r"https://abs\.twimg\.com/responsive-web/client-web[^\"'\\\s>]+\.js",
                str(html or ""),
            ):
                if match not in vistos:
                    vistos.add(match)
                    urls.append(match)
        except Exception:
            pass
        return urls

    @staticmethod
    def _buscar_queryid_en_js(js: str, operacion: str) -> str:
        """queryId de `operacion` dentro de un bundle JS ('' si no aparece)."""
        if not js:
            return ""
        op = re.escape(str(operacion))
        patrones = (
            re.compile(r'queryId:"([A-Za-z0-9_-]+)",operationName:"' + op + r'"'),
            re.compile(
                r'operationName:"' + op + r'"[^{}]{0,200}?queryId:"([A-Za-z0-9_-]+)"'
            ),
            re.compile(
                r'queryId:"([A-Za-z0-9_-]+)"[^{}]{0,200}?operationName:"' + op + r'"'
            ),
        )
        for patron in patrones:
            try:
                match = patron.search(js)
                if match:
                    return match.group(1)
            except Exception:
                continue
        return ""

    def _get_web(self, url: str, timeout: float, extra_headers: dict = None):
        """GET de una pagina/bundle HTML con headers de NAVEGADOR.

        El `authorization: Bearer` + `x-twitter-auth-type` de la API hacen que
        x.com responda 401 al HTML (comprobado con una sesion real: con esos
        headers 401, con headers de navegador 200 y la SPA completa). Se quitan
        SOLO para este GET y se restauran SIEMPRE. Tolera sesiones fake sin
        kwargs. Nunca lanza por los headers.
        """
        web_headers = {
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "accept-language": "es-MX,es;q=0.9,en;q=0.8",
            "referer": "https://x.com/",
        }
        if extra_headers:
            web_headers.update(extra_headers)
        quitados = {}
        for header in ("authorization", "x-twitter-auth-type", "x-csrf-token"):
            try:
                if header in self.session.headers:
                    quitados[header] = self.session.headers.pop(header)
            except Exception:
                pass
        try:
            try:
                return self.session.get(
                    url, headers=web_headers, timeout=max(0.5, float(timeout))
                )
            except TypeError:
                return self.session.get(url, headers=web_headers)
        finally:
            for header, valor in quitados.items():
                try:
                    self.session.headers[header] = valor
                except Exception:
                    pass

    def _descubrir_queryid(self, operacion: str) -> str:
        """Busca el queryId en los bundles JS de x.com (presupuesto ~8s).

        Un lock de proceso evita que N workers descarguen los bundles a la vez:
        el primero descubre y los demas leen el cache. NO descarga nada si el
        disyuntor del grupo esta abierto. Nunca lanza.
        """
        grupo = _grupo_operacion(operacion)
        if grupo:
            abierto, motivo = _breaker_abierto(grupo)
            if abierto:
                logger.debug(
                    f"descubrimiento de {operacion} omitido: disyuntor "
                    f"'{grupo}' abierto ({motivo})"
                )
                return ""
        with _QUERYIDS_DESCUBRIMIENTO_LOCK:
            # Doble verificacion: otro worker pudo descubrirlo mientras esperaba.
            queryid = self._queryid_memoria(operacion)
            if queryid:
                return queryid
            if self.session is None:
                return ""
            fin = time.time() + _QUERYIDS_PRESUPUESTO_SEG
            try:
                resp = self._get_web(
                    "https://x.com/", min(6.0, max(1.0, fin - time.time()))
                )
                html = getattr(resp, "text", "") or ""
            except Exception as e:
                logger.debug(f"Descubrimiento de queryId: x.com fallo ({e})")
                return ""
            for bundle in self._bundles_de_html(html)[:_QUERYIDS_MAX_BUNDLES]:
                restante = fin - time.time()
                if restante <= 0.5:
                    break
                try:
                    resp_js = self._get_web(bundle, min(4.0, restante))
                    texto_js = getattr(resp_js, "text", "") or ""
                except Exception:
                    continue
                queryid = self._buscar_queryid_en_js(texto_js, operacion)
                if queryid:
                    logger.info(f"queryId descubierto para {operacion}: {queryid}")
                    return queryid
            return ""

    def _queryid(self, operacion: str) -> str:
        """queryId vigente de una operacion GraphQL (cache -> descubrimiento).

        Orden: cache por proceso -> archivo `data/twitter_queryids.json`
        (TTL 6h) -> descubrimiento en los bundles de x.com (~8s) -> fallback
        conocido (solo CreateRetweet/FavoriteTweet; CreateTweet y
        TweetResultByRestId devuelven "" si no hay candidato fiable). Nunca
        lanza.
        """
        operacion = str(operacion or "").strip()
        if not operacion:
            return ""
        try:
            queryid = self._queryid_memoria(operacion)
            if queryid:
                return queryid
            queryid = self._queryid_archivo(operacion)
            if queryid:
                self._guardar_queryid_memoria(operacion, queryid)
                return queryid
            # Con el disyuntor abierto NO se redescubre (ahorra ~8s por cuenta);
            # el fallback conocido sigue disponible sin tocar la red.
            grupo = _grupo_operacion(operacion)
            if grupo and _breaker_abierto(grupo)[0]:
                queryid = ""
            else:
                queryid = self._descubrir_queryid(operacion)
            if queryid:
                self._guardar_queryid(operacion, queryid)
                return queryid
            queryid = _QUERYIDS_FALLBACK.get(operacion, "")
            if queryid:
                logger.info(
                    f"queryId de {operacion} no descubierto; usando fallback "
                    f"conocido para {self.usuario}"
                )
                self._guardar_queryid_memoria(operacion, queryid)
            return queryid
        except Exception as e:
            logger.debug(f"_queryid({operacion}) fallo: {type(e).__name__}: {e}")
            return _QUERYIDS_FALLBACK.get(operacion, "")

    # ------------------------------------------------------------------ #
    # POST GraphQL con reintento por queryId vencido
    # ------------------------------------------------------------------ #

    def _timeout_restante(self) -> float:
        """Timeout del siguiente POST acotado al presupuesto de la accion.

        `API_HTTP_TIMEOUT` (default 8s) es el tope por peticion;
        `API_TIMEOUT_SEG` (default 10s) el presupuesto total de `accion_rapida`.
        Devuelve 0.0 si el presupuesto ya se agoto (el POST se omite). Nunca
        lanza.
        """
        try:
            tope = max(1.0, _env_float("API_HTTP_TIMEOUT", 8.0))
        except Exception:
            tope = 8.0
        try:
            restante = float(self._deadline) - time.monotonic()
        except Exception:
            return tope
        if restante <= 0:
            return 0.0
        return min(tope, restante)

    def _post_json(self, url: str, payload: dict):
        """POST JSON con timeout acotado (API_HTTP_TIMEOUT/presupuesto).

        Tolerante a sesiones fake/stub que no aceptan kwargs o que no exponen
        `_deadline`. Nunca lanza: devuelve None si no se pudo enviar.
        """
        timeout = self._timeout_restante()
        if timeout <= 0:
            logger.debug(
                f"POST {url} omitido para {self.usuario}: sin presupuesto de red"
            )
            return None
        try:
            return self.session.post(url, json=payload, timeout=timeout)
        except TypeError:
            # Sesion fake/stub que no acepta kwargs.
            try:
                return self.session.post(url, json=payload)
            except Exception:
                return None
        except Exception as e:
            logger.debug(
                f"POST {url} fallo para {self.usuario}: {type(e).__name__}: {e}"
            )
            return None

    @classmethod
    def _error_queryid_vencido(cls, status, data) -> bool:
        """True si un 400/422 sugiere queryId viejo (vale redescubrir UNA vez).

        Los errores de VALIDACION del payload (`graphql_validation_failed`,
        "must be defined") NO son queryId viejo: redescubrir y reintentar
        repite el mismo 422 y quema ~8s de proxy por cuenta (caso real del log
        con CreateRetweet). Los mensajes de persistedquery/not found y los 400
        o 422 sin cuerpo si son queryId vencido.
        """
        try:
            texto = cls._mensajes_error(data)
        except Exception:
            texto = ""
        if (
            "graphql_validation_failed" in texto
            or "must be defined" in texto
            or "validation_failed" in texto
        ):
            return False
        return True

    def _graphql(self, operacion: str, variables: dict, features: dict = None):
        """POST GraphQL con `_queryid` y UN reintento si X lo rechaza (400/422).

        Redescubre el queryId UNA vez SOLO si el error sugiere id vencido
        (persistedquery/not found, 400, 422 sin cuerpo o generico). Si el 422
        es de validacion del payload (`graphql_validation_failed`) falla rapido
        sin redescubrir. Devuelve `(status, data)`; `(0, None)` si no se pudo
        enviar o si el presupuesto se agoto. Nunca lanza.
        """
        status, data = 0, None
        for intento in (1, 2):
            queryid = self._queryid(operacion)
            if not queryid:
                return 0, None
            payload = {"variables": variables}
            if features:
                payload["features"] = features
            url = f"https://x.com/i/api/graphql/{queryid}/{operacion}"
            resp = self._post_json(url, payload)
            if resp is None:
                return 0, None
            status = getattr(resp, "status_code", 0)
            try:
                data = resp.json()
            except Exception:
                data = None
            if status in (400, 422) and intento == 1:
                if not self._error_queryid_vencido(status, data):
                    logger.warning(
                        f"{operacion} rechazado por X ({status}) por validacion "
                        f"(no es queryId viejo); sin redescubrimiento para "
                        f"{self.usuario}"
                    )
                    return status, data
                if self._timeout_restante() <= 0:
                    logger.debug(
                        f"{operacion}: sin presupuesto para redescubrir el queryId"
                    )
                    return status, data
                logger.info(
                    f"queryId de {operacion} rechazado por X ({status}); "
                    f"redescubriendo y reintentando para {self.usuario}"
                )
                self._invalidar_queryid(operacion)
                continue
            return status, data
        return status, data

    @staticmethod
    def _mensajes_error(data) -> str:
        """Texto normalizado de `errors` de GraphQL ('' si no hay)."""
        return _texto_errores(data)

    @staticmethod
    def _respuesta_ok(data) -> bool:
        """True si la respuesta 200 no trae errores de GraphQL."""
        try:
            return isinstance(data, dict) and not data.get("errors")
        except Exception:
            return False

    @classmethod
    def _retweet_ya_hecho(cls, data) -> bool:
        """True si X reporta que el tweet YA estaba retwitteado (code 327)."""
        texto = cls._mensajes_error(data)
        return (
            "327" in texto
            or "already retweeted" in texto
            or "already retweet" in texto
            or "ya retwitteaste" in texto
            or "reposteaste" in texto
        )

    @classmethod
    def _like_ya_hecho(cls, data) -> bool:
        """True si X reporta que el like YA estaba dado (code 139)."""
        texto = cls._mensajes_error(data)
        return (
            "139" in texto
            or "already favorited" in texto
            or "already liked" in texto
            or "ya te gusta" in texto
            or "ya lo marcaste" in texto
        )

    @staticmethod
    def _crear_tweet_ok(data) -> bool:
        """True si CreateTweet devolvio el id del tweet creado."""
        try:
            if not isinstance(data, dict) or data.get("errors"):
                return False
            resultado = (
                data.get("data", {})
                .get("create_tweet", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            return bool(resultado.get("rest_id") or resultado.get("tweet"))
        except Exception:
            return False

    def _registrar_fallo_api(self, grupo: str, status, data) -> None:
        """Alimenta disyuntor/bloqueo por cuenta con un fallo DURO.

        Clasifica `(status, data)`; si es de red (status 0) NO cuenta. Nunca
        lanza.
        """
        try:
            es_duro, motivo, bloquear = _clasificar_fallo(status, data)
            if not es_duro:
                return
            if bloquear:
                _bloquear_cuenta(self.usuario, motivo)
            _breaker_registrar_fallo(grupo, motivo)
        except Exception:
            pass

    def retweet(self, tweet_url: str, dar_like: bool = False) -> bool:
        """Retweetea por API HTTP (firma compatible con `retweet(url)`).

        Usa el `queryId` de CreateRetweet (descubierto/cacheado; UN reintento
        con redescubrimiento si X responde 400/422 y el error sugiere id
        vencido). Si X contesta "already retweeted" (code 327) se cuenta como
        EXITO: la campana es idempotente y no duplica nada. Con `dar_like=True`
        da like al mismo tweet UNA vez (se ignora su resultado); el like se
        omite si su disyuntor esta abierto o si ya no queda presupuesto.
        Alimenta el disyuntor del grupo "rt" con los fallos duros. Nunca lanza.
        """
        if not self.session and not self._cargar_cookies():
            return False

        t0 = time.time()
        resultado = False
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False

            status, data = self._graphql(
                "CreateRetweet",
                {"source_tweet_id": tweet_id},
                features=_FEATURES_ESCRITURA,
            )
            if self._respuesta_ok(data):
                logger.info(f"RT por API para {self.usuario}")
                _breaker_registrar_exito("rt")
                resultado = True
            elif self._retweet_ya_hecho(data):
                logger.info(
                    f"{self.usuario} ya habia retwitteado; se cuenta como exito"
                )
                _breaker_registrar_exito("rt")
                resultado = True
            else:
                motivo = self._mensajes_error(data)[:120]
                self._registrar_fallo_api("rt", status, data)
                logger.warning(
                    f"RT API fallo ({status}) [rt] para {self.usuario}: {motivo}"
                )

        except Exception as e:
            logger.error(f"Error en retweet: {e}")
            resultado = False

        if dar_like:
            try:
                abierto_like, motivo_like = _breaker_abierto("like")
                if abierto_like:
                    logger.debug(
                        f"like del RT omitido para {self.usuario}: disyuntor "
                        f"'like' abierto ({motivo_like})"
                    )
                elif self._timeout_restante() <= 0:
                    logger.debug(
                        f"like del RT omitido para {self.usuario}: sin presupuesto"
                    )
                else:
                    self.like(tweet_url)
            except Exception as e:
                logger.debug(f"Like del RT fallo para {self.usuario}: {e}")

        logger.debug(
            f"perf API @{self.usuario}: RT {time.time() - t0:.2f}s ok={resultado}"
        )
        return resultado

    def like(self, tweet_url: str) -> bool:
        """Da like por API HTTP (queryId de FavoriteTweet).

        Si X contesta "already favorited" (code 139) se cuenta como EXITO
        (idempotente). Alimenta el disyuntor del grupo "like" con los fallos
        duros. Nunca lanza.
        """
        if not self.session and not self._cargar_cookies():
            return False

        t0 = time.time()
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False

            status, data = self._graphql(
                "FavoriteTweet",
                {"source_tweet_id": tweet_id},
                features=_FEATURES_ESCRITURA,
            )
            if self._respuesta_ok(data):
                logger.info(f"Like exitoso para {self.usuario}")
                _breaker_registrar_exito("like")
                resultado = True
            elif self._like_ya_hecho(data):
                logger.info(f"{self.usuario} ya tenia like; se cuenta como exito")
                _breaker_registrar_exito("like")
                resultado = True
            else:
                motivo = self._mensajes_error(data)[:120]
                self._registrar_fallo_api("like", status, data)
                logger.warning(
                    f"Like API fallo ({status}) [like] para {self.usuario}: "
                    f"{motivo}"
                )
                resultado = False
        except Exception as e:
            logger.error(f"Error en like: {e}")
            resultado = False

        logger.debug(
            f"perf API @{self.usuario}: like {time.time() - t0:.2f}s ok={resultado}"
        )
        return resultado

    def crear_tweet(self, texto: str, reply_to_url: str = "",
                    quote_url: str = "") -> bool:
        """Publica un tweet nuevo (o respuesta/cita) por GraphQL CreateTweet.

        - `reply_to_url`: crea una RESPUESTA a ese tweet (variables `reply.
          in_reply_to_tweet_id`).
        - `quote_url`: crea un POST QUE CITA ese tweet (variable
          `attachment_url`).
        - Si vienen ambos, gana la respuesta.

        Devuelve True/False y NUNCA lanza (403/429/422 y cualquier error de X
        => False: el motor cae a Selenium).
        """
        texto = (texto or "").strip()
        if not texto:
            return False
        if not self.session and not self._cargar_cookies():
            return False

        t0 = time.time()
        try:
            variables = {
                "tweet_text": texto,
                "dark_request": False,
                "media": {"media_entities": [], "possibly_sensitive": False},
                "semantic_annotation_ids": [],
            }
            if reply_to_url:
                reply_id = self._extraer_tweet_id(reply_to_url)
                if not reply_id:
                    return False
                variables["reply"] = {
                    "in_reply_to_tweet_id": reply_id,
                    "exclude_reply_user_ids": [],
                }
            elif quote_url:
                if not self._extraer_tweet_id(quote_url):
                    return False
                variables["attachment_url"] = quote_url

            status, data = self._graphql(
                "CreateTweet", variables, features=_FEATURES_ESCRITURA
            )
            ok = self._crear_tweet_ok(data)
            if not ok:
                motivo = self._mensajes_error(data)[:140]
                self._registrar_fallo_api("tweet", status, data)
                logger.warning(
                    f"CreateTweet fallo ({status}) [tweet] para {self.usuario}: "
                    f"{motivo}"
                )
            else:
                logger.info(f"Tweet por API para {self.usuario}")
                _breaker_registrar_exito("tweet")
            logger.debug(
                f"perf API @{self.usuario}: crear_tweet {time.time() - t0:.2f}s "
                f"ok={ok}"
            )
            return ok
        except Exception as e:
            logger.error(f"Error en crear_tweet: {e}")
            return False

    def accion_rapida(self, rol: str, url: str = "", dar_like: bool = False,
                      texto: str = "") -> bool:
        """Ejecuta por HTTP una accion segun el rol (punto de entrada del motor).

        - `rt`: retweet (+ like si `dar_like=True`).
        - `like`: like.
        - `comentario`: respuesta a `url` con `texto` (CreateTweet reply).
        - `hashtags` / `post`: post nuevo con `texto` (CreateTweet).
        - `cita`: post que cita `url` con `texto` (CreateTweet quote).
        - Cualquier otro rol => False (sin soporte HTTP: el motor usa Selenium).

        LO PRIMERO es consultar el disyuntor del grupo (`rt`/`like`/`tweet`) y
        el bloqueo de la cuenta: si estan abiertos devuelve False SIN cargar
        cookies ni tocar la red. Si el fallo dura `API_TIMEOUT_SEG` (default
        10s) no sigue esperando. Firma retrocompatible: `url` ahora tiene
        default y `texto` se agrego al final. Loguea la duracion de cada accion
        (para medir en campana). Nunca lanza.
        """
        rol = (rol or "").strip().lower()
        t0 = time.time()
        grupo = _grupo_rol(rol)
        if grupo:
            abierto, motivo = _breaker_abierto(grupo)
            if abierto:
                logger.debug(
                    f"accion_rapida({rol}) omitida para {self.usuario}: "
                    f"disyuntor '{grupo}' abierto ({motivo})"
                )
                return False
            bloqueada, motivo_cuenta = _cuenta_bloqueada(self.usuario)
            if bloqueada:
                logger.debug(
                    f"accion_rapida({rol}) omitida para {self.usuario}: cuenta "
                    f"bloqueada ({motivo_cuenta})"
                )
                return False
        # Presupuesto de red de ESTA accion: `_post_json` lo respeta.
        limite = max(1.0, _env_float("API_TIMEOUT_SEG", 10.0))
        self._deadline = time.monotonic() + limite
        try:
            if rol == "rt":
                ok = self.retweet(url, dar_like=dar_like)
            elif rol == "like":
                ok = self.like(url)
            elif rol == "comentario":
                ok = self.crear_tweet(texto, reply_to_url=url)
            elif rol in ("hashtags", "post"):
                ok = self.crear_tweet(texto)
            elif rol == "cita":
                ok = self.crear_tweet(texto, quote_url=url)
            else:
                ok = False
        except Exception as e:
            logger.debug(
                f"accion_rapida({rol}) fallo para {self.usuario}: "
                f"{type(e).__name__}: {e}"
            )
            ok = False
        excedido = (time.time() - t0) > limite
        logger.debug(
            f"perf API @{self.usuario}: accion_rapida({rol or '?'}) "
            f"{time.time() - t0:.2f}s ok={ok}"
            + (" (presupuesto excedido)" if excedido else "")
        )
        return bool(ok)

    def _extraer_tweet_id(self, url: str) -> Optional[str]:
        """Id numerico de `/status/<id>` (None si no hay)."""
        match = re.search(r"/status(?:es)?/(\d+)", url or "")
        return match.group(1) if match else None
