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
import os
import random
import re
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
from core.registro import registrar_accion, marcar_cuenta_suspendida


def _parsear_tokens(valor: str) -> list[str]:
    """Separa hashtags/menciones por comas, espacios o saltos de linea."""
    if not valor:
        return []
    return [t for t in re.split(r"[,\s]+", str(valor).strip()) if t]


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


MENSAJE_SIN_SESION = (
    "sin sesión: sin .pkl, cookies_json/auth_token ni password; "
    "brandea o carga credenciales antes de activar"
)


def _sugerencia_sesion(n: int) -> str:
    """Accion sugerida para las cuentas filtradas por falta de sesion."""
    return (
        f"{n} cuenta(s) sin sesión: no tienen .pkl en "
        "data/cookies/twitter/ ni cookies_json/auth_token/password en la BD. "
        "Brandéalas (login manual) o importa el lote con auth_token/cookies/"
        "password antes de activar; se saltaron sin abrir navegador."
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
)


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


def _es_error_reintentable(detalle) -> bool:
    """True si el fallo amerita UN reintento sin riesgo de duplicar el post.

    Incluye los fallos transitorios de driver/navegador y los fallos de
    publicacion donde el texto no llego a enviarse (boton Post deshabilitado o
    editor que no registro el texto). Nunca lanza.
    """
    if _es_error_driver_transitorio(detalle):
        return True
    try:
        texto = "" if detalle is None else str(detalle).lower()
    except Exception:
        return False
    return any(senal in texto for senal in _SENALES_ERROR_PUBLICACION_SEGURA)


def _tiene_credencial_sesion(cuenta) -> bool:
    """True si la cuenta tiene alguna credencial de sesion usable.

    Revisa, sin abrir ningun navegador: archivo .pkl en disco,
    `cookies_json` con contenido y `auth_token` no vacio en la BD.
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
    password = getattr(cuenta, "password", "")
    if isinstance(password, str):
        return bool(password.strip())
    return bool(password)


def _partir_por_sesion(cuentas: list) -> tuple:
    """Separa cuentas con credencial de sesion de las que no tienen ninguna."""
    con, sin = [], []
    for cuenta in (cuentas or []):
        (con if _tiene_credencial_sesion(cuenta) else sin).append(cuenta)
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


def _roles_disponibles_aleatorios(urls, hashtags="", contexto="",
                                  texto_base="", solo_roles=None) -> list[str]:
    """Roles que se pueden sortear con los inputs dados (modo aleatorio).

    - "cita", "rt" y "comentario" requieren al menos una URL objetivo (el
      comentario responde al tweet ancla).
    - "hashtags" requiere hashtags, contexto o texto base.
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
        "(habilita cita/rt) o escribe hashtags, contexto o texto base "
        "(habilita hashtags); no se abrió ningún navegador."
    )


def _garantizar_hashtags_texto(texto: str, tags: list[str]) -> str:
    """Garantiza que el texto traiga al menos un hashtag de `tags`.

    Si no trae ninguno, agrega al final 1-2 tags elegidos al azar (los que
    quepan de la lista). Con `tags` vacio devuelve el texto intacto.
    Nunca lanza.
    """
    t = str(texto or "").strip()
    if not t or not tags:
        return t
    try:
        presentes = {
            h.lower()
            for h in re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", t)
        }
        if any(str(tag).lower() in presentes for tag in tags):
            return t
        cantidad = min(len(tags), random.randint(1, 2))
        elegidos = random.sample(list(tags), cantidad)
        return (t + " " + " ".join(elegidos)).strip()
    except Exception:
        return t


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


class MotorActivacion:
    """Ejecuta una campaña de activacion (RT con cita) sobre N cuentas."""

    def __init__(self, max_concurrente: int = None):
        self.max_concurrente = max_concurrente or settings.max_browsers
        self._lock = threading.Lock()
        self.progreso = {
            "hechas": 0,
            "exitosas": 0,
            "fallidas": 0,
            "ronda_actual": 1,
            "eventos": [],
        }

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

    def _registrar_evento(self, usuario, ok, detalle, ronda=1, rol="",
                          url="") -> None:
        """Version thread-safe de `_registrar_evento_locked`."""
        with self._lock:
            self._registrar_evento_locked(usuario, ok, detalle, ronda, rol, url)

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
                "ronda_actual": self.progreso.get("ronda_actual", 1),
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
            cuentas = [
                c for c in cuentas
                if normalizar_rol_activacion(c.rol_activacion) in permitidos
            ]

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
                                  roles_previos: dict | None = None) -> dict:
        """Sortea un rol de `disponibles` para cada cuenta de `cuentas`.

        Devuelve `{usuario: rol}`. Con `roles_previos` ({usuario: rol}) la
        cuenta recibe un rol DISTINTO al de su participacion anterior cuando
        hay mas de una opcion; si `disponibles` solo trae un rol (o ninguno),
        se usa la lista completa como fallback y puede repetirlo. Con
        `disponibles` vacio deja "" (defensivo, el llamador ya valida que haya
        al menos uno). Nunca lanza.
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
                previo = previos.get(usuario)
                candidatos = [r for r in opciones if r != previo] or opciones
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

    def _generar_textos_por_rol(self, grupos_ejec: dict, texto_base: str,
                                hashtags: str = "", menciones: str = "",
                                narrativa: str = "",
                                entrenamiento: str = "",
                                contexto: str = "",
                                ronda: int = 1) -> dict:
        """Arma {usuario: texto} para una ronda de la campana por roles.

        - "cita": variaciones OpenAI/fallback del texto base con los hashtags
          pedidos garantizados.
        - "hashtags": posts ORIGINALES por cuenta con IA (registro/perfil);
          si la IA falla o no devuelve texto, rellena con `_pool_hashtags`
          (comportamiento anterior) y agrega las menciones al final.
        - "comentario": respuestas ORIGINALES por cuenta con IA
          (`generar_textos_comentario`, registro/perfil) sobre el tweet ancla
          (contexto/texto base); si la IA falla, cae al pool de variaciones y,
          en ultimo caso, a un texto local con los hashtags pedidos.
        - "rt": sin texto ("").
        Nunca lanza por la IA: ante cualquier fallo usa el pool de respaldo.
        """
        asignaciones: dict = {}
        tags = _normalizar_hashtags(hashtags)
        narrativa = _narrativa_con_ronda(narrativa, ronda)

        citas = list(grupos_ejec.get("cita") or [])
        if citas:
            pool_cita = generar_pool_variaciones_openai(
                texto_base,
                cantidad=len(citas),
                narrativa=narrativa,
                entrenamiento=entrenamiento,
            )
            if tags:
                pool_cita = [
                    _garantizar_hashtags_texto(t, tags) for t in pool_cita
                ]
            random.shuffle(pool_cita)
            for i, cuenta in enumerate(citas):
                if i < len(pool_cita):
                    asignaciones[cuenta.usuario] = pool_cita[i]
                else:
                    asignaciones[cuenta.usuario] = _garantizar_hashtags_texto(
                        texto_base, tags
                    )

        cuentas_hashtags = list(grupos_ejec.get("hashtags") or [])
        if cuentas_hashtags:
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
                    texto_base, hashtags, menciones, len(faltantes)
                )
                random.shuffle(respaldo)
            for i, cuenta in enumerate(faltantes):
                asignaciones[cuenta.usuario] = (
                    respaldo[i] if i < len(respaldo) else texto_base
                )
            for cuenta in cuentas_hashtags:
                if cuenta.usuario in textos_ia:
                    asignaciones[cuenta.usuario] = _agregar_menciones(
                        textos_ia[cuenta.usuario], menciones_norm
                    )

        cuentas_comentario = list(grupos_ejec.get("comentario") or [])
        if cuentas_comentario:
            material = (
                str(contexto or "").strip() or str(texto_base or "").strip()
            )
            narrativa_com = narrativa
            if material:
                instruccion = f"Comenta el tweet ancla sobre: {material}"
                narrativa_com = (
                    f"{instruccion}\n{narrativa}" if narrativa else instruccion
                )
            textos_ia_com: dict = {}
            try:
                from ia.generador_contenido import generar_textos_comentario

                cuentas_info = []
                for cuenta in cuentas_comentario:
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
                resultado_ia = generar_textos_comentario(
                    cuentas_info,
                    n_por_cuenta=1,
                    narrativa=narrativa_com,
                    entrenamiento=entrenamiento,
                )
                for i, cuenta in enumerate(cuentas_comentario):
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
                base_respaldo = (
                    str(texto_base or "").strip()
                    or str(contexto or "").strip()
                )
                respaldo = []
                if base_respaldo:
                    try:
                        respaldo = generar_pool_variaciones_openai(
                            base_respaldo,
                            cantidad=len(faltantes),
                            narrativa=narrativa,
                            entrenamiento=entrenamiento,
                        )
                    except Exception:
                        respaldo = []
                if not isinstance(respaldo, list):
                    respaldo = []
                random.shuffle(respaldo)
                for i, cuenta in enumerate(faltantes):
                    texto = (
                        str(respaldo[i] or "").strip()
                        if i < len(respaldo) else ""
                    )
                    if not texto and tags:
                        semilla = base_respaldo or " ".join(tags)
                        texto = _garantizar_hashtags_texto(semilla, tags)
                    textos_ia_com[cuenta.usuario] = texto
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
                      porcentaje_max_ronda=90) -> int:
        """Ejecuta acciones en rondas hasta agotar `duracion_min`.

        Worker-pool con cola compartida: `self.max_concurrente` workers toman
        cuentas de un orden barajado; al agotarlo, regeneran los textos de la
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
        n_workers = max(1, int(self.max_concurrente or 1))
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

        def _iniciar_ronda_locked() -> None:
            estado["ronda"] += 1
            with self._lock:
                self.progreso["ronda_actual"] = estado["ronda"]
            k = _calcular_k_ronda(len(procesables), min_pct, max_pct)
            if k >= len(procesables):
                subset = list(procesables)
            else:
                try:
                    subset = random.sample(procesables, k)
                except Exception:
                    subset = list(procesables)
            random.shuffle(subset)
            estado["orden"] = subset
            estado["cursor"] = 0
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

        def _tomar_cuenta_locked():
            """Toma (cuenta, texto, rol) o None si todas descansan; con lock."""
            orden = estado["orden"]
            if estado["cursor"] >= len(orden):
                _iniciar_ronda_locked()
                orden = estado["orden"]
            revisados = 0
            while revisados < len(orden):
                if estado["cursor"] >= len(orden):
                    return None
                candidata = orden[estado["cursor"]]
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
            while time.monotonic() < fin:
                with lock:
                    elegido = _tomar_cuenta_locked()
                    ronda = estado["ronda"]
                if elegido is None:
                    # Todas las cuentas revisadas estan en descanso: soltar el
                    # lock y reintentar sin bloquear a los demas workers.
                    if time.monotonic() < fin:
                        time.sleep(random.uniform(2, 5))
                    continue
                cuenta, texto, rol = elegido
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
                    time.sleep(random.uniform(2, 6))

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

    def _intentar_quote_rt(self, cuenta: Cuenta, urls: list[str],
                           texto: str, dar_like: bool) -> tuple:
        """Un intento de quote-RT para UNA cuenta; cierra el bot siempre.

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada).
        """
        bot = None
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            url = random.choice(urls)

            bot = TwitterBot(cuenta.usuario)
            if not bot.login_con_cookies():
                motivo = getattr(bot, "ultimo_error", "") or "login fallido"
                logger.warning(f"Login fallido para @{cuenta.usuario}: {motivo}")
                return (cuenta.usuario, False, motivo[:120], "")

            res = bot.solo_retwittear(
                [url],
                cuenta.usuario,
                mensaje_cita=texto,
                dar_like=dar_like,
            )

            ok = res.get("exitos", 0) > 0
            url_publicada = (res.get("urls") or [""])[0] if res.get("urls") else ""
            return (cuenta.usuario, ok, "ok" if ok else "sin exito", url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario}: {e}")
            return (cuenta.usuario, False, str(e)[:80], "")
        finally:
            if bot is not None:
                if getattr(bot, "cuenta_suspendida", False):
                    marcar_cuenta_suspendida(cuenta.usuario)
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
        driver/navegador, espera 2-4s y reintenta UNA vez con un bot nuevo.

        Devuelve una tupla de 4 elementos:
        (usuario, exito, detalle, url_publicada).
        """
        if not _tiene_credencial_sesion(cuenta):
            return (cuenta.usuario, False, MENSAJE_SIN_SESION[:120], "")
        if retardo > 0:
            time.sleep(retardo)

        resultado = self._intentar_quote_rt(cuenta, urls, texto, dar_like)
        if not resultado[1] and _es_error_reintentable(resultado[2]):
            pausa = random.uniform(2, 4)
            logger.warning(
                f"Reintento de quote-RT para @{cuenta.usuario} por error de "
                f"driver/navegador ({resultado[2]}); espero {pausa:.1f}s"
            )
            time.sleep(pausa)
            resultado = self._intentar_quote_rt(cuenta, urls, texto, dar_like)
        return resultado

    def _intentar_accion_rol(self, cuenta: Cuenta, rol: str, urls: list[str],
                             texto: str, dar_like: bool) -> tuple:
        """Un intento de UNA accion segun el rol de activacion de la cuenta.

        - "cita": quote-RT con el texto asignado.
        - "hashtags": publica el texto con hashtags/menciones ("post" es alias).
        - "comentario": respuesta a un tweet (url + texto distintos por slot).
        - "rt": retweet simple (sin cita); puede dar like.

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url).
        Nunca lanza: cualquier error se reporta como fallo.
        """
        if rol == "post":
            rol = "hashtags"

        bot = None
        url_objetivo = ""
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            if rol in ("cita", "rt", "comentario"):
                url_objetivo = random.choice(urls) if urls else ""
                if not url_objetivo:
                    return (cuenta.usuario, rol, False, "sin URL objetivo", "")

            bot = TwitterBot(cuenta.usuario)
            if not bot.login_con_cookies():
                motivo = getattr(bot, "ultimo_error", "") or "login fallido"
                logger.warning(f"Login fallido para @{cuenta.usuario}: {motivo}")
                return (cuenta.usuario, rol, False, motivo[:120], url_objetivo)

            if rol == "cita":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    mensaje_cita=texto,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                urls_pub = res.get("urls") or []
                url_publicada = urls_pub[0] if urls_pub else url_objetivo
                detalle = "ok" if ok else (
                    getattr(bot, "ultimo_error", "") or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            if rol == "rt":
                res = bot.solo_retwittear(
                    [url_objetivo],
                    cuenta.usuario,
                    dar_like=dar_like,
                )
                ok = res.get("exitos", 0) > 0
                urls_pub = res.get("urls") or []
                # El RT simple no genera un post propio: `solo_retwittear`
                # ya devuelve el perfil de quien retwittea, no el tweet original.
                url_publicada = urls_pub[0] if urls_pub else f"https://twitter.com/{cuenta.usuario}"
                detalle = "ok" if ok else (
                    getattr(bot, "ultimo_error", "") or "sin exito"
                )
                return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

            if rol == "comentario":
                if not (texto or "").strip():
                    return (cuenta.usuario, rol, False, "sin texto asignado", url_objetivo)
                responder = getattr(bot, "responder_tweet", None)
                if responder is None:
                    return (cuenta.usuario, rol, False, "sin soporte de respuesta", url_objetivo)
                ok = bool(responder(url_objetivo, texto))
                if ok:
                    detalle = "comentario publicado"
                else:
                    motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                    detalle = f"comentario: {motivo}"
                return (cuenta.usuario, rol, ok, detalle[:120], url_objetivo)

            # rol == "hashtags"
            res = bot.publicar_tweet(texto, buscar_url=False)
            ok = bool(res)
            if isinstance(res, str):
                url_publicada = res
            elif ok:
                url_publicada = getattr(bot, "ultima_url_publicada", "") or ""
            else:
                url_publicada = ""
            if ok:
                detalle = "hashtags publicados"
            else:
                motivo = getattr(bot, "ultimo_error", "") or "sin exito"
                detalle = f"hashtags: {motivo}"
            return (cuenta.usuario, rol, ok, detalle[:120], url_publicada)

        except Exception as e:
            logger.error(f"Error en @{cuenta.usuario} (rol {rol}): {e}")
            return (
                cuenta.usuario, rol, False,
                f"{type(e).__name__}: {e}"[:120], url_objetivo,
            )
        finally:
            if bot is not None:
                if getattr(bot, "cuenta_suspendida", False):
                    marcar_cuenta_suspendida(cuenta.usuario)
                    logger.warning(
                        f"@{cuenta.usuario} marcada como suspendida (desactivada)"
                    )
                try:
                    bot.cerrar()
                except Exception as e:
                    logger.warning(
                        f"No se pudo cerrar el navegador de @{cuenta.usuario}: {e}"
                    )

    def _ejecutar_accion_rol(self, cuenta: Cuenta, rol: str, urls: list[str],
                             texto: str, dar_like: bool, retardo: float = 0) -> tuple:
        """Ejecuta UNA accion segun el rol de activacion de la cuenta.

        Si el primer intento falla por un error transitorio de
        driver/navegador, espera 2-4s y reintenta UNA vez con un bot nuevo.

        Devuelve una tupla de 5 elementos:
        (usuario, rol, exito, detalle, url).
        Nunca lanza: cualquier error se reporta como fallo.
        """
        if not _tiene_credencial_sesion(cuenta):
            return (cuenta.usuario, rol, False, MENSAJE_SIN_SESION[:120], "")
        if retardo > 0:
            time.sleep(retardo)

        resultado = self._intentar_accion_rol(cuenta, rol, urls, texto, dar_like)
        if not resultado[2] and _es_error_reintentable(resultado[3]):
            pausa = random.uniform(2, 4)
            logger.warning(
                f"Reintento de acción '{rol}' para @{cuenta.usuario} por error "
                f"de driver/navegador ({resultado[3]}); espero {pausa:.1f}s"
            )
            time.sleep(pausa)
            resultado = self._intentar_accion_rol(cuenta, rol, urls, texto, dar_like)
        return resultado

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
        """
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
            return {
                "exitosas": 0, "fallidas": 0, "detalles": [], "total": 0,
                "sin_sesion": 0, "sin_sesion_usuarios": [],
                "sugerencia_sesion": "",
                "sin_registro": len(sin_registro),
                "sin_registro_usuarios": sin_registro_usuarios,
                "sugerencia_registro": sugerencia_registro,
                "rondas": 0 if repetir else 1,
            }

        con_sesion, sin_sesion = _partir_por_sesion(cuentas)
        sin_sesion_usuarios = [c.usuario for c in sin_sesion]
        sugerencia_sesion = _sugerencia_sesion(len(sin_sesion)) if sin_sesion else ""

        resultados = [
            {"usuario": c.usuario, "ok": False,
             "detalle": MENSAJE_SIN_SESION[:120], "url": ""}
            for c in sin_sesion
        ]
        for cuenta in sin_sesion:
            with self._lock:
                self.progreso["hechas"] += 1
                self.progreso["fallidas"] += 1
                self._registrar_evento_locked(
                    cuenta.usuario, False, MENSAJE_SIN_SESION[:120], 1, "", ""
                )
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                MENSAJE_SIN_SESION[:120],
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
            return resumen_vacio

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
                nuevo = generar_pool_variaciones_openai(
                    texto_base,
                    cantidad=len(base),
                    narrativa=_narrativa_con_ronda(narrativa, _ronda),
                    entrenamiento=entrenamiento,
                )
                if tags_pedidos:
                    nuevo = [
                        _garantizar_hashtags_texto(t, tags_pedidos)
                        for t in nuevo
                    ]
                random.shuffle(nuevo)
                asignaciones_ronda = {
                    cuenta.usuario: (
                        nuevo[i] if i < len(nuevo)
                        else _garantizar_hashtags_texto(
                            texto_base, tags_pedidos
                        )
                    )
                    for i, cuenta in enumerate(base)
                }
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
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["exitosas" if ok else "fallidas"] += 1
                    resumen["exitosas" if ok else "fallidas"] += 1
                    self._registrar_evento_locked(
                        usuario_res, ok, detalle, ronda, "cita", url
                    )
                    registrar_accion(
                        usuario_res,
                        "activacion",
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
            )
            logger.info(
                f"Activacion finalizada (rondas): {resumen['exitosas']} exitosas, "
                f"{resumen['fallidas']} fallidas en {resumen['rondas']} ronda(s) "
                f"({resumen['sin_sesion']} sin sesión, "
                f"{resumen['sin_registro']} sin registro)"
            )
            return resumen

        pool = generar_pool_variaciones_openai(
            texto_base, cantidad=len(con_sesion), narrativa=narrativa, entrenamiento=entrenamiento
        )
        if tags_pedidos:
            pool = [_garantizar_hashtags_texto(t, tags_pedidos) for t in pool]
        # Garantizar un texto unico por cuenta (ninguna cuenta comparte el mismo).
        random.shuffle(pool)
        asignaciones = {
            cuenta.usuario: (
                pool[i] if i < len(pool)
                else _garantizar_hashtags_texto(texto_base, tags_pedidos)
            )
            for i, cuenta in enumerate(con_sesion)
        }
        bloques = self._distribuir_cohortes(con_sesion, duracion_min, cohortes)

        logger.info(
            f"Activacion masiva: {len(con_sesion)} cuentas con sesión "
            f"({len(sin_sesion)} sin sesión saltadas), {len(urls)} urls, "
            f"{cohortes} cohortes, {duracion_min} min, concurrencia {self.max_concurrente}"
        )

        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

        # El ThreadPoolExecutor ya limita la concurrencia; el retardo solo
        # distribuye los INICIOS a lo largo de la ventana para no disparar
        # todas las cuentas al mismo tiempo.
        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for idx, bloque in enumerate(bloques):
                for cuenta in bloque:
                    retardo = idx * intervalo_cohorte + random.uniform(0, 15)
                    futuros.append((retardo, pool_exec.submit(
                        self._quote_rt_una_cuenta, cuenta, urls,
                        asignaciones[cuenta.usuario], dar_like, retardo
                    ), cuenta.usuario))

            # Recoger resultados en orden de arranque (los retardos ya fueron
            # aplicados al momento de submit via scheduling natural del pool).
            for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    usuario_res, ok, detalle, url = futuro.result()
                except Exception as e:
                    usuario_res, ok, detalle, url = usuario, False, str(e)[:80], ""

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
                        "activacion",
                        "exito" if ok else "fallido",
                        url,
                        detalle,
                    )
                    resultados.append({"usuario": usuario_res, "ok": ok, "detalle": detalle, "url": url})
                    if callback:
                        callback(self.progreso["hechas"], len(cuentas), usuario_res, ok)

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
        }
        logger.info(
            f"Activacion finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_sesion']} sin sesión, "
            f"{resumen['sin_registro']} sin registro)"
        )
        return resumen

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
        - Cohortes temporales + delay aleatorio y concurrencia limitada,
          igual que `ejecutar()`.
        - Nunca lanza: cada cuenta fallida se reporta en `detalles`.
        """
        urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        try:
            cooldown_val = max(0.0, float(cooldown_min or 0))
        except (TypeError, ValueError):
            cooldown_val = 0.0
        roles_sortear = (
            _roles_disponibles_aleatorios(
                urls, hashtags, contexto, texto_base, solo_roles
            )
            if roles_aleatorios else []
        )
        if roles_aleatorios and not roles_sortear:
            sugerencia_roles = _sugerencia_roles_aleatorios()
            logger.warning(f"Activacion por roles: {sugerencia_roles}")
            return {
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
                "sugerencia_roles": sugerencia_roles,
            }
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
        if roles_aleatorios:
            # El rol guardado no filtra: cada cuenta recibe un rol sorteado en
            # cada ronda (los roles posibles ya se validaron arriba).
            procesables = list(cuentas)
            rol_de: dict = {}
        else:
            for cuenta in cuentas:
                rol = normalizar_rol_activacion(
                    getattr(cuenta, "rol_activacion", "")
                )
                if rol in grupos:
                    grupos[rol].append(cuenta)
                else:
                    sin_rol_usuarios.append(cuenta.usuario)

            procesables = (
                grupos["cita"] + grupos["hashtags"]
                + grupos["comentario"] + grupos["rt"]
            )
            rol_de = {
                cuenta.usuario: normalizar_rol_activacion(
                    getattr(cuenta, "rol_activacion", "")
                )
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
                "detalle": MENSAJE_SIN_SESION[:120],
                "url": "",
            })

        if not procesables:
            logger.warning(
                "Activacion por roles: no hay cuentas con rol para ejecutar "
                f"({len(sin_rol_usuarios)} sin rol)"
            )
            return resumen

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
            return resumen

        rol_por_usuario = {
            cuenta.usuario: rol_de.get(cuenta.usuario, "")
            for cuenta in ejecutables
        }

        logger.info(
            f"Activacion por roles: {len(ejecutables)} cuentas con sesión "
            f"(cita={len(grupos_ejec['cita'])}, hashtags={len(grupos_ejec['hashtags'])}, "
            f"rt={len(grupos_ejec['rt'])}, sin_rol={len(sin_rol_usuarios)}, "
            f"sin_sesion={len(sin_sesion)}), "
            f"{len(urls)} urls, {cohortes} cohortes, {duracion_min} min, "
            f"concurrencia {self.max_concurrente}"
        )

        with self._lock:
            self.progreso = {
                "hechas": len(sin_sesion),
                "exitosas": 0,
                "fallidas": len(sin_sesion),
                "ronda_actual": 1,
                "eventos": [],
            }
        for cuenta in sin_sesion:
            registrar_accion(
                cuenta.usuario, "activacion", "fallido", "",
                MENSAJE_SIN_SESION[:120],
            )
            with self._lock:
                self._registrar_evento_locked(
                    cuenta.usuario, False, MENSAJE_SIN_SESION[:120], 1,
                    rol_de.get(cuenta.usuario, ""), "",
                )
            if callback:
                callback(
                    self.progreso["hechas"], len(procesables),
                    cuenta.usuario, False,
                )
        intervalo_cohorte = max(1, (duracion_min * 60) // max(cohortes, 1))

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
                        base, roles_sortear, roles_previos=roles_ultimos
                    )
                    roles_ultimos.update(roles_ronda)
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
                    )
                    return (
                        _aplicar_anti_repeticion(textos, usados),
                        roles_ronda,
                    )
                if usuarios is None:
                    grupos_ronda = grupos_ejec
                else:
                    grupos_ronda = {
                        rol: [c for c in cuentas if c.usuario in deseados]
                        for rol, cuentas in grupos_ejec.items()
                    }
                textos = self._generar_textos_por_rol(
                    grupos_ronda,
                    texto_base,
                    hashtags=hashtags,
                    menciones=menciones,
                    narrativa=narrativa,
                    entrenamiento=entrenamiento,
                    contexto=contexto,
                    ronda=_ronda,
                )
                return _aplicar_anti_repeticion(textos, usados)

            def _ejecutar_una_rol(cuenta, texto, rol=""):
                rol_efectivo = rol or rol_por_usuario.get(cuenta.usuario, "")
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
                        "activacion",
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
            )
            logger.info(
                f"Activacion por roles finalizada (rondas): "
                f"{resumen['exitosas']} exitosas, {resumen['fallidas']} fallidas "
                f"en {resumen['rondas']} ronda(s) "
                f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión, "
                f"{resumen['sin_registro']} sin registro)"
            )
            return resumen

        # --- Pool de textos por rol (una sola ronda) ---
        if roles_aleatorios:
            roles_una = self._asignar_roles_aleatorios(ejecutables, roles_sortear)
            grupos_ejec_una = self._grupos_desde_roles(ejecutables, roles_una)
        else:
            roles_una = dict(rol_por_usuario)
            grupos_ejec_una = grupos_ejec
        asignaciones = self._generar_textos_por_rol(
            grupos_ejec_una,
            texto_base,
            hashtags=hashtags,
            menciones=menciones,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            contexto=contexto,
        )

        bloques = self._distribuir_cohortes(ejecutables, duracion_min, cohortes)

        with ThreadPoolExecutor(max_workers=self.max_concurrente) as pool_exec:
            futuros = []
            for idx, bloque in enumerate(bloques):
                for cuenta in bloque:
                    rol = roles_una.get(cuenta.usuario, "")
                    retardo = idx * intervalo_cohorte + random.uniform(0, 15)
                    futuros.append((retardo, pool_exec.submit(
                        self._ejecutar_accion_rol, cuenta, rol, urls,
                        asignaciones.get(cuenta.usuario, ""), dar_like, retardo,
                    ), cuenta.usuario))

            for retardo, futuro, usuario in sorted(futuros, key=lambda x: x[0]):
                try:
                    usuario_res, rol_res, ok, detalle, url = futuro.result()
                except Exception as e:
                    usuario_res = usuario
                    rol_res = roles_una.get(usuario, "")
                    ok, detalle, url = False, str(e)[:80], ""

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
                        "activacion",
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

        logger.info(
            f"Activacion por roles finalizada: {resumen['exitosas']} exitosas, "
            f"{resumen['fallidas']} fallidas de {resumen['total']} "
            f"({resumen['sin_rol']} sin rol, {resumen['sin_sesion']} sin sesión)"
        )
        return resumen

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
        }
        if not cuentas:
            logger.warning("Campana 3+3+3: no hay cuentas activas de twitter")
            return base_resumen

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
                "ronda_actual": 1,
                "eventos": [],
            }

        for cuenta in sin_sesion:
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
                    "detalle": MENSAJE_SIN_SESION[:120],
                    "url": "",
                })
                with self._lock:
                    self.progreso["hechas"] += 1
                    self.progreso["fallidas"] += 1
                    self._registrar_evento_locked(
                        cuenta.usuario, False, MENSAJE_SIN_SESION[:120], 1,
                        rol_slot, "",
                    )
                registrar_accion(
                    cuenta.usuario, "campana_3_3_3", "fallido", "",
                    MENSAJE_SIN_SESION[:120],
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
            return base_resumen

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
                            usuario_res, "campana_3_3_3",
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
        return base_resumen
