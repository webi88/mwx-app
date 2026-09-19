import curl_cffi.requests as requests
import hashlib
import pickle
import time
import random
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


class TwitterAPI:
    def __init__(self, usuario: str):
        self.usuario = usuario
        self.cookies_path = resolver_ruta(f"data/cookies/twitter/{usuario}.pkl")
        self.session = None
        self.bearer_token = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

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
            headers = {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "accept-language": "es-MX,es;q=0.9,en;q=0.8",
                "referer": "https://x.com/",
                "x-twitter-active-user": "yes",
            }
            try:
                resp = self.session.get("https://x.com/", headers=headers, timeout=20)
            except TypeError:
                # Sesiones fake/stubs que no aceptan kwargs.
                resp = self.session.get("https://x.com/")

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
            import json

            try:
                cookies = json.loads(cookies)
            except Exception:
                return []
        if isinstance(cookies, dict):
            cookies = cookies.get("cookies") or []
        if not isinstance(cookies, list):
            return []
        return [c for c in cookies if isinstance(c, dict)]
    
    def retweet(self, tweet_url: str, dar_like: bool = False) -> bool:
        """Retweetea por API HTTP (firma compatible con `retweet(url)`).

        Con `dar_like=True` da like al mismo tweet UNA vez (se ignora su
        resultado): el motor usa esto para reemplazar el flujo Selenium de
        RT+like por ~1-2s en vez de ~10-35s.
        """
        if not self.session and not self._cargar_cookies():
            return False

        resultado = False
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False

            variables = {"source_tweet_id": tweet_id}
            features = {
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }

            response = self.session.post(
                "https://x.com/i/api/graphql/ojPdsZsimiJrUGLR1sjUtA/CreateRetweet",
                json={"variables": variables, "features": features}
            )

            if response.status_code == 200:
                data = response.json()
                if "errors" not in data:
                    logger.info(f"RT por API para {self.usuario}")
                    resultado = True

            if not resultado:
                logger.warning(
                    f"RT API fallo ({response.status_code}) para {self.usuario}"
                )

        except Exception as e:
            logger.error(f"Error en retweet: {e}")
            resultado = False

        if dar_like:
            try:
                self.like(tweet_url)
            except Exception as e:
                logger.debug(f"Like del RT fallo para {self.usuario}: {e}")

        return resultado
    
    def like(self, tweet_url: str) -> bool:
        if not self.session and not self._cargar_cookies():
            return False
        
        try:
            tweet_id = self._extraer_tweet_id(tweet_url)
            if not tweet_id:
                return False
            
            variables = {"source_tweet_id": tweet_id}
            features = {
                "rweb_tipjar_consumption_enabled": True,
                "responsive_web_graphql_exclude_directive_enabled": True,
                "verified_phone_label_enabled": False,
            }
            
            response = self.session.post(
                "https://x.com/i/api/graphql/lZ0GCEojmtQfiUQa5oJSEw/FavoriteTweet",
                json={"variables": variables, "features": features}
            )
            
            if response.status_code == 200:
                data = response.json()
                if "errors" not in data:
                    logger.info(f"Like exitoso para {self.usuario}")
                    return True
            
            logger.warning(f"Like fallido para {self.usuario}: {response.status_code}")
            return False
        
        except Exception as e:
            logger.error(f"Error en like: {e}")
            return False

    def accion_rapida(self, rol: str, url: str, dar_like: bool = False) -> bool:
        """Ejecuta por HTTP una accion soportada (punto de entrada del motor).

        - `rt`: retweet (+ like si `dar_like=True`).
        - `like`: like.
        - Cualquier otro rol devuelve False (sin soporte HTTP: el motor debe
          usar Selenium).
        """
        rol = (rol or "").strip().lower()
        if rol == "rt":
            return self.retweet(url, dar_like=dar_like)
        if rol == "like":
            return self.like(url)
        return False

    def _extraer_tweet_id(self, url: str) -> Optional[str]:
        import re
        match = re.search(r"/status/(\d+)", url)
        return match.group(1) if match else None
    
    def verificar_sesion(self) -> bool:
        if not self.session and not self._cargar_cookies():
            return False
        
        try:
            response = self.session.get("https://x.com/i/api/1.1/account/verify_credentials.json")
            return response.status_code == 200
        except:
            return False
    
    def rt_rapido(self, urls: list[str], usuario_ids: list[str]) -> dict:
        resultados = {"exitos": 0, "fallidos": 0}
        
        for url in urls:
            for usuario_id in usuario_ids:
                api = TwitterAPI(usuario_id)
                if api.retweet(url):
                    resultados["exitos"] += 1
                else:
                    resultados["fallidos"] += 1
                
                time.sleep(random.uniform(2.5, 6.0))
        
        return resultados
