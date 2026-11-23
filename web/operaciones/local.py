"""Operacion LOCAL: campana con Chrome visible en la computadora del dueño.

El dueño abre el dashboard en SU equipo, escribe el contexto/datos de la
campana, pulsa "▶️ Ejecutar en local" y ve abrirse un Chrome que trabaja con
las cuentas Tier 1 seleccionadas (SIN limite de cuentas: `MAX_CUENTAS = 0`
delega el tope en `LOCAL_MAX_CUENTAS`; 0 = toda la flota Tier 1 elegible),
con proxies residenciales opcionales (un proxy sticky por cuenta), sin cargar
imagenes de X para ahorrar datos y con espera opcional del reto Cloudflare,
alternando posts con imagen y retweets con cita del tweet ancla, repartidos en
la ventana elegida (60 minutos por defecto) y con un tope diario por cuenta
(49 por defecto, el limite aproximado de X para cuentas no premium).

El trabajo real lo hace `activaciones/local.py` (backend en paralelo):

    ejecutar_campana_local(
        usuarios=None, urls=None, hashtags="", texto_base="", contexto="",
        narrativa="", menciones="", imagenes=None, probabilidad_imagen=100,
        acciones_por_cuenta=6, duracion_min=60, limite_dia=None, dar_like=False,
        permitir_pausadas=False, dejar_abierto=False, cancelar=None, callback=None,
        pausa_entre_acciones_seg=10, capturar_url=False,
        usar_proxy=True, sin_imagenes=True, esperar_captcha_seg=60,
    ) -> dict
    cerrar_navegadores_locales() -> int

Los kwargs `pausa_entre_acciones_seg`/`capturar_url`/`usar_proxy`/
`sin_imagenes`/`esperar_captcha_seg` se pasan SOLO si el backend los acepta
(`_soporta_kwarg`): una version vieja sigue funcionando igual (sin proxies,
con el comportamiento anterior).

IMPORTANTE: el backend se importa SIEMPRE de forma PEREZOSA dentro del handler
del boton (nunca a nivel de modulo): asi la pagina carga aunque el backend
todavia no exista en la version desplegada y el test puede inyectar un fake.

Registro de procesos: vive a nivel MODULO (`_CAMPANAS` + `_CAMPANAS_LOCK`,
patron de `web/operaciones/change.py`), no en `st.session_state`, por lo que el
panel en vivo sobrevive a recargas del navegador y a la navegacion entre
operaciones del dashboard. El hilo del backend SOLO muta el registro bajo lock
(JAMAS toca `st.*`: Streamlit no es thread-safe); el hilo principal pinta el
panel con `st.fragment(run_every=1s)` si esta disponible y, si no, con el bucle
`time.sleep(1); st.rerun()` mientras la campana siga viva.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

import streamlit as st

from web.ui import cabecera
from web.operaciones._helpers import aviso_verificacion

# ============================ CONSTANTES ============================

# Prefijo de las claves de widget/estado de la pagina (no colisiona con
# "act", "act_roles", "actividad" ni "change").
PREFIX = "local"

# Tope de cuentas del modo local. 0 = sin limite (lo define LOCAL_MAX_CUENTAS;
# 0 = todas las Tier 1 elegibles). El backend tambien lo valida.
MAX_CUENTAS = 0

# Estados que cuentan como "campana en curso" (bloquean lanzar otra).
_ESTADOS_ACTIVOS = ("en_curso", "deteniendo")

# Maximo de eventos guardados en memoria (anti-fuga en campanas largas). El
# feed en vivo muestra solo los ultimos 8; el expander final, todos.
LOG_MAX = 200

# Texto fijo del panel informativo de la pagina.
PANEL_INFO = (
    "🖥️ Chrome visible · proxies opcionales · solo Tier 1 · "
    "reparto en la ventana"
)

# Caption de ahorro bajo los controles de red/datos.
CAPTION_AHORRO = (
    "Las imágenes se comprimen automáticamente; el resumen muestra los MB "
    "usados por el proxy"
)

# Aviso cuando no se subieron imagenes.
AVISO_SIN_IMAGENES = "No hay imágenes: los posts saldrán solo con texto."

# ============================ REGISTRO DE CAMPANAS ============================
# Entradas de ESTE proceso (en curso y terminadas). Cada entrada:
#   {"id", "hilo", "evento" (threading.Event de paro), "estado"
#    ("en_curso"|"deteniendo"|"terminada"|"cancelada"|"error"), "hechas",
#    "total", "exitosas", "fallidas", "omitidas", "ronda", "eventos" (lista de
#    dicts), "resumen", "error", "motivo", "inicio", "fin", "parametros"}
# El callback del backend corre en hilos worker: solo escribe aqui bajo lock.
_CAMPANAS: dict = {}
_CAMPANAS_LOCK = threading.RLock()


def _entero(valor, default: int = 0) -> int:
    """int() tolerante a None/str/basura (nunca lanza)."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _en_contexto_streamlit() -> bool:
    """True solo dentro de un script de Streamlit (AppTest incluido).

    En modo bare (tests que llaman helpers sin runtime) es False, de modo que
    el lanzamiento no intenta `st.rerun()`."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def _campana_actual():
    """Entrada de la campana mas reciente (en curso o terminada); None si no hay."""
    with _CAMPANAS_LOCK:
        if not _CAMPANAS:
            return None
        return next(reversed(_CAMPANAS.values()))


def _campana_en_curso():
    """Entrada activa (en_curso/deteniendo) o None. Guard de campana unica."""
    with _CAMPANAS_LOCK:
        for entrada in reversed(list(_CAMPANAS.values())):
            if str(entrada.get("estado") or "") in _ESTADOS_ACTIVOS:
                return entrada
    return None


def _registrar_campana(hilo=None, evento=None, parametros=None) -> str:
    """Registra una campana nueva en `en_curso` y devuelve su id.

    Devuelve "" si YA hay una campana local en curso (guard de campana unica a
    nivel proceso, tambien entre sesiones del dashboard)."""
    id_campana = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    with _CAMPANAS_LOCK:
        for entrada in _CAMPANAS.values():
            if str(entrada.get("estado") or "") in _ESTADOS_ACTIVOS:
                return ""
        _CAMPANAS[id_campana] = {
            "id": id_campana,
            "hilo": hilo,
            "evento": evento if evento is not None else threading.Event(),
            "estado": "en_curso",
            "hechas": 0,
            "total": 0,
            "exitosas": 0,
            "fallidas": 0,
            "omitidas": 0,
            "ronda": 0,
            "eventos": [],
            "resumen": {},
            "error": "",
            "motivo": "",
            "inicio": time.time(),
            "fin": None,
            "parametros": dict(parametros or {}),
        }
    return id_campana


def _anotar_evento(id_campana, evento) -> None:
    """Procesa UN evento del callback del backend (puro: jamas toca `st.*`).

    Contrato del evento: `{"hechas", "total", "usuario", "rol", "ok",
    "detalle", "url"}`. Si trae `ok` se contabiliza como exitosa (`True`),
    fallida (`False`) u omitida (`None`); si no trae `ok` solo actualiza el
    avance (eventos de progreso). Guarda el evento para el feed y lo recorta a
    `LOG_MAX`."""
    if not isinstance(evento, dict) or not id_campana:
        return
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        if evento.get("hechas") is not None:
            entrada["hechas"] = max(
                _entero(entrada.get("hechas")),
                _entero(evento.get("hechas")),
            )
        elif "ok" in evento or (
            evento.get("usuario") and evento.get("tipo") != "espera_captcha"
        ):
            entrada["hechas"] = _entero(entrada.get("hechas")) + 1
        total = _entero(evento.get("total"))
        if total:
            entrada["total"] = total
        ronda = _entero(evento.get("ronda"))
        if ronda:
            entrada["ronda"] = ronda
        if "ok" in evento:
            ok = evento.get("ok")
            if ok is True:
                entrada["exitosas"] = _entero(entrada.get("exitosas")) + 1
            elif ok is False:
                entrada["fallidas"] = _entero(entrada.get("fallidas")) + 1
            else:
                entrada["omitidas"] = _entero(entrada.get("omitidas")) + 1
        eventos = entrada.setdefault("eventos", [])
        eventos.append(dict(evento))
        if len(eventos) > LOG_MAX:
            del eventos[:-LOG_MAX]


def _finalizar_campana(id_campana, resumen=None, error=None) -> None:
    """Cierra la campana: estado final, resumen/error, totales y fin.

    Nunca lanza. Si el callback no entrego eventos, se usan los del resumen
    (`resumen["eventos"]`) para que el expander final muestre el detalle."""
    if not id_campana:
        return
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        entrada["fin"] = time.time()
        resumen_dict = resumen if isinstance(resumen, dict) else {}
        entrada["resumen"] = resumen_dict
        entrada["error"] = (
            str(error)
            if error is not None
            else str(resumen_dict.get("error") or "")
        )
        entrada["motivo"] = str(resumen_dict.get("motivo") or "")
        if error is not None:
            entrada["estado"] = "error"
        elif resumen_dict.get("cancelada"):
            entrada["estado"] = "cancelada"
        else:
            entrada["estado"] = "terminada"
        entrada["hechas"] = _entero(
            resumen_dict.get("hechas"), _entero(entrada.get("hechas"))
        )
        entrada["total"] = _entero(
            resumen_dict.get("planificadas"),
            _entero(resumen_dict.get("total"), _entero(entrada.get("total"))),
        )
        entrada["exitosas"] = _entero(
            resumen_dict.get("exitosas"), _entero(entrada.get("exitosas"))
        )
        entrada["fallidas"] = _entero(
            resumen_dict.get("fallidas"), _entero(entrada.get("fallidas"))
        )
        entrada["omitidas"] = _entero(
            resumen_dict.get("omitidas"), _entero(entrada.get("omitidas"))
        )
        eventos = resumen_dict.get("eventos")
        if isinstance(eventos, list) and eventos:
            actuales = entrada.setdefault("eventos", [])
            if len(eventos) >= len(actuales):
                entrada["eventos"] = [
                    dict(evento)
                    for evento in eventos[-LOG_MAX:]
                    if isinstance(evento, dict)
                ]


def _solicitar_paro() -> bool:
    """Setea el `threading.Event` de la campana local activa.

    El backend deja de abrir cuentas nuevas y cierra el navegador. Devuelve
    False si no habia ninguna campana en curso."""
    with _CAMPANAS_LOCK:
        entrada = None
        for candidata in reversed(list(_CAMPANAS.values())):
            if str(candidata.get("estado") or "") in _ESTADOS_ACTIVOS:
                entrada = candidata
                break
        if entrada is None:
            return False
        if entrada.get("estado") == "en_curso":
            entrada["estado"] = "deteniendo"
        try:
            evento = entrada.get("evento")
            if evento is not None:
                evento.set()
        except Exception:
            pass
    return True


def _limpiar_registro(id_campana=None) -> None:
    """Borra una entrada del registro (o TODAS). Uso en tests/limpieza."""
    with _CAMPANAS_LOCK:
        if id_campana is None:
            _CAMPANAS.clear()
        else:
            _CAMPANAS.pop(id_campana, None)


def _snapshot():
    """Copia inmutable de la entrada mas reciente para pintar el panel.

    Se copia bajo lock para no retener el lock mientras se pinta y para que el
    callback del backend pueda seguir mutando el registro. Devuelve None si no
    hay campanas."""
    with _CAMPANAS_LOCK:
        if not _CAMPANAS:
            return None
        entrada = next(reversed(_CAMPANAS.values()))
        estado = str(entrada.get("estado") or "")
        resumen = entrada.get("resumen")
        resumen_dict = dict(resumen) if isinstance(resumen, dict) else {}
        hilo = entrada.get("hilo")
        try:
            hilo_vivo = bool(hilo is not None and hilo.is_alive())
        except Exception:
            hilo_vivo = False
        return {
            "id": str(entrada.get("id") or ""),
            "estado": estado,
            "en_curso": estado in _ESTADOS_ACTIVOS,
            "deteniendo": estado == "deteniendo",
            "hechas": _entero(entrada.get("hechas")),
            "total": _entero(entrada.get("total")),
            "exitosas": _entero(entrada.get("exitosas")),
            "fallidas": _entero(entrada.get("fallidas")),
            "omitidas": _entero(entrada.get("omitidas")),
            "ronda": _entero(entrada.get("ronda")),
            "eventos": [
                dict(evento)
                for evento in (entrada.get("eventos") or [])
                if isinstance(evento, dict)
            ],
            "resumen": resumen_dict,
            "error": str(entrada.get("error") or ""),
            "motivo": str(entrada.get("motivo") or ""),
            "parametros": dict(entrada.get("parametros") or {}),
            "hilo_vivo": hilo_vivo,
        }


# ============================ HELPERS PUROS ============================

def _cookies_json_con_contenido(cookies_json) -> bool:
    """True si `cookies_json` trae cookies reales (no None/""/[]/{}).

    Mismo criterio que `scheduler/calentamiento._cookies_json_con_contenido`:
    acepta lista/dict (columna JSON) o cadena JSON cruda. Nunca lanza."""
    try:
        if cookies_json is None:
            return False
        if isinstance(cookies_json, str):
            crudo = cookies_json.strip()
            if not crudo or crudo.lower() in ("[]", "{}", "null", "none"):
                return False
            try:
                return bool(json.loads(crudo))
            except (TypeError, ValueError):
                # Cadena no-JSON con contenido: se asume que algo trae.
                return True
        return bool(cookies_json)
    except Exception:
        return False


def _cuenta_tiene_sesion(cuenta) -> bool:
    """True si la cuenta tiene una sesion REALMENTE utilizable.

    Mismos criterios que `scheduler/calentamiento._tiene_sesion`:
      1. `auth_token` no vacio;
      2. `cookies_json` con contenido;
      3. `cookies_path` no vacio Y el archivo existe en disco.
    Nunca lanza: cualquier error -> False."""
    try:
        if str(getattr(cuenta, "auth_token", "") or "").strip():
            return True
        if _cookies_json_con_contenido(getattr(cuenta, "cookies_json", None)):
            return True
        cookies_path = str(getattr(cuenta, "cookies_path", "") or "").strip()
        if not cookies_path:
            return False
        try:
            from core.config import resolver_ruta

            ruta = resolver_ruta(cookies_path)
        except Exception:
            ruta = cookies_path
        return os.path.isfile(ruta)
    except Exception:
        return False


def _normalizar_hashtags(texto) -> list:
    """Hashtags normalizados de un texto/lista: con '#' y sin duplicados.

    Acepta saltos de linea, comas, punto y coma y espacios. Ignora vacios,
    '#' sueltos y tokens sin letras/digitos. Nunca lanza."""
    if texto is None:
        return []
    if isinstance(texto, (list, tuple, set)):
        piezas = []
        for elemento in texto:
            piezas.extend(str(elemento or "").replace(",", " ").split())
    else:
        piezas = str(texto).replace(",", " ").replace(";", " ").split()
    tags: list = []
    vistos: set = set()
    for pieza in piezas:
        token = str(pieza or "").strip()
        if not token:
            continue
        token = "#" + token.lstrip("#").strip()
        if len(token) <= 1:
            continue
        clave = token.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        tags.append(token)
    return tags


def _hashtags_detectados(*textos) -> list:
    """Hashtags escritos dentro del contexto/texto base (preview de la UI).

    El backend hace la misma deteccion cuando el campo de hashtags esta vacio;
    aqui se muestra ANTES de lanzar para que el usuario sepa que SI se usara el
    '#'. Nunca lanza."""
    encontrados = []
    for texto in textos:
        try:
            encontrados.extend(
                re.findall(r"#[0-9A-Za-z_ÁÉÍÓÚÜÑáéíóúüñ]+", str(texto or ""))
            )
        except Exception:
            continue
    return _normalizar_hashtags(encontrados)


def _hashtags_texto(tags) -> str:
    """Une una lista de hashtags en el texto que espera el backend."""
    if isinstance(tags, str):
        return tags.strip()
    try:
        return " ".join(str(tag) for tag in (tags or []) if str(tag).strip())
    except Exception:
        return ""


def _soporta_kwarg(func, nombre: str) -> bool:
    """True si `func` acepta el kwarg `nombre` (backend viejo -> False).

    Se inspecciona la firma ANTES de llamar para no romper una version del
    backend local que todavia no tenga `pausa_entre_acciones_seg`/`capturar_url`
    (y para no capturar un `TypeError` interno del backend). Tambien acepta
    funciones que reciben `**kwargs`. Nunca lanza."""
    import inspect

    try:
        parametros = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if nombre in parametros:
        return True
    return any(
        parametro.kind == inspect.Parameter.VAR_KEYWORD
        for parametro in parametros.values()
    )


def _lista_urls(texto) -> list:
    """URLs http(s) del textarea (una por linea), sin vacios ni comentarios.

    Acepta el texto del textarea o una lista/tupla ya separada (la usa
    `_validar_formulario` cuando recibe las URLs ya parseadas). Las lineas que
    no empiezan por http(s) se ignoran (mismo criterio que el backend)."""
    if isinstance(texto, (list, tuple, set)):
        candidatos = [str(valor) for valor in texto]
    else:
        candidatos = str(texto or "").splitlines()
    urls: list = []
    for linea in candidatos:
        valor = linea.strip()
        if not valor or valor.startswith("#"):
            continue
        if not valor.lower().startswith(("http://", "https://")):
            continue
        urls.append(valor)
    return urls


def _validar_formulario(
    usuarios=None,
    urls="",
    contexto="",
    hashtags="",
    narrativa="",
    imagenes=None,
    maximo: int = MAX_CUENTAS,
) -> tuple[bool, str, str]:
    """Valida el formulario ANTES de lanzar. Devuelve `(ok, error, aviso)`.

    - `ok=False` + `error`: bloquea el lanzamiento (sin cuentas, mas de
      `maximo` cuentas cuando `maximo > 0` o sin ningun material para
      generar/citar).
    - `ok=True` + `aviso`: se puede lanzar, pero conviene avisar (p. ej. no hay
      imagenes y los posts saldran solo con texto).
    `maximo <= 0` = SIN limite por cantidad (`MAX_CUENTAS = 0`).
    Funcion PURA (sin Streamlit): la usan la pagina y los tests."""
    try:
        total = len(list(usuarios or []))
    except Exception:
        total = 0
    if total <= 0:
        return (
            False,
            "Selecciona al menos una cuenta Tier 1 para ejecutar en local.",
            "",
        )
    try:
        tope = int(maximo)
    except (TypeError, ValueError):
        tope = MAX_CUENTAS
    if tope > 0 and total > tope:
        return (
            False,
            f"El modo local acepta como máximo {tope} cuentas "
            f"(seleccionaste {total}).",
            "",
        )
    tiene_urls = bool(_lista_urls(urls))
    tiene_contexto = bool(str(contexto or "").strip())
    tiene_hashtags = bool(_normalizar_hashtags(hashtags))
    tiene_narrativa = bool(str(narrativa or "").strip())
    if not (tiene_urls or tiene_contexto or tiene_hashtags or tiene_narrativa):
        return (
            False,
            "Escribe al menos una URL del tweet ancla o un contexto/tema, "
            "hashtags o trasfondo para que el backend sepa de qué hablar.",
            "",
        )
    try:
        n_imagenes = len(list(imagenes or []))
    except Exception:
        n_imagenes = 0
    aviso = "" if n_imagenes > 0 else AVISO_SIN_IMAGENES
    return True, "", aviso


def _formatear_evento(evento) -> str:
    """Linea legible de UN evento del feed (funcion PURA, sin Streamlit).

    Formato: `✅ @usuario · rol — detalle · [link](url)` con `✅`/`❌`/`➖`
    segun `ok` (`True`/`False`/`None`).

    Los eventos `{"tipo": "espera_captcha", "usuario": ..., "segundos": N}`
    (reto Cloudflare en Chrome) se pintan como
    `🧠 @usuario — resuelve la verificación en Chrome (Ns)`, sin `ok`.
    Devuelve "" si el evento no es dict."""
    if not isinstance(evento, dict):
        return ""
    usuario = str(evento.get("usuario") or "").strip().lstrip("@")
    if str(evento.get("tipo") or "").strip() == "espera_captcha":
        segundos = _entero(evento.get("segundos"), 0)
        if usuario:
            return (
                f"🧠 @{usuario} — resuelve la verificación en Chrome "
                f"({segundos}s)"
            )
        return f"🧠 Resuelve la verificación en Chrome ({segundos}s)"
    rol = str(evento.get("rol") or "").strip()
    detalle = " ".join(str(evento.get("detalle") or "").split())
    url = str(evento.get("url") or "").strip()
    ok = evento.get("ok")
    if ok is True:
        icono = "✅"
    elif ok is False:
        icono = "❌"
    else:
        icono = "➖"
    partes = [f"{icono} @{usuario}" if usuario else icono]
    if rol:
        partes.append(f"· {rol}")
    if detalle:
        partes.append(f"— {detalle}")
    if url.lower().startswith(("http://", "https://")):
        partes.append(f"· [link]({url})")
    return " ".join(partes)


def _resumen_metricas(resumen) -> dict:
    """Metricas (etiqueta -> valor) del resumen final. Funcion PURA.

    Devuelve SIEMPRE las mismas 8 claves (0 si faltan) para que la pagina y
    los tests no dependan de que el backend traiga todas."""
    datos = resumen if isinstance(resumen, dict) else {}
    return {
        "✅ Exitosas": _entero(datos.get("exitosas")),
        "❌ Fallidas": _entero(datos.get("fallidas")),
        "➖ Omitidas": _entero(datos.get("omitidas")),
        "🧱 Tier fuera": _entero(datos.get("omitidas_tier")),
        "🔑 Sin sesión": _entero(datos.get("omitidas_sin_sesion")),
        "⏸️ Pausadas": _entero(datos.get("omitidas_pausadas")),
        "🛑 Por cuota": _entero(datos.get("omitidas_por_cuota")),
        "⏰ Por tiempo": _entero(datos.get("omitidas_por_tiempo")),
    }


def _captions_resumen(resumen, parametros=None) -> list:
    """Captions extra del resumen final (hashtags, links y ritmo). Pura.

    - `hashtags_auto` (lista detectada por el backend en contexto/texto base).
    - `hashtags` usados en la campaña (texto o lista).
    - `capturar_url` y `pausa_entre_acciones_seg`: del resumen si vienen o, si
      no, de los parametros con los que se lanzo la campaña.
    Devuelve SOLO las lineas aplicables (sin vacios). Nunca lanza."""
    datos = resumen if isinstance(resumen, dict) else {}
    params = parametros if isinstance(parametros, dict) else {}
    lineas: list = []

    auto = _normalizar_hashtags(datos.get("hashtags_auto"))
    if auto:
        lineas.append(
            "🏷️ Hashtags detectados del contexto: " + _hashtags_texto(auto)
        )

    usados = _normalizar_hashtags(datos.get("hashtags"))
    if usados:
        lineas.append("🏷️ Hashtags usados: " + _hashtags_texto(usados))

    capturar = datos.get("capturar_url")
    if capturar is None:
        capturar = params.get("capturar_url")
    if capturar is not None:
        if bool(capturar):
            lineas.append(
                "🔗 Enlaces de cada publicación: capturados para Reportes."
            )
        else:
            lineas.append(
                "🔗 Enlaces de cada publicación: no capturados (desactivado); "
                "Reportes mostrará «link no capturado»."
            )

    navegadores = datos.get("navegadores")
    if navegadores is None:
        navegadores = params.get("navegadores")
    try:
        navegadores_n = _entero(navegadores)
    except Exception:
        navegadores_n = 0
    if navegadores_n > 1:
        lineas.append(
            f"🪟 Navegadores simultáneos: {navegadores_n} "
            "(cada cuenta completa en un solo navegador)."
        )

    pausa = datos.get("pausa_entre_acciones_seg")
    if pausa is None:
        pausa = params.get("pausa_entre_acciones_seg")
    if pausa is not None:
        lineas.append(
            f"⏱️ Pausa mínima entre acciones: {_entero(pausa, 2)} s."
        )
    return lineas


# El backend reporta bytes (mismo criterio que `LocalForwardProxy.estadisticas`).
_CLAVES_PROXY_SUBIDA = (
    "datos_proxy_subida",
    "datos_proxy_up",
    "proxy_bytes_subida",
)
_CLAVES_PROXY_BAJADA = (
    "datos_proxy_bajada",
    "datos_proxy_down",
    "proxy_bytes_bajada",
)


def _mb_desde_bytes(valor) -> float | None:
    """Convierte bytes a MB (float); None si el valor no es numerico/positivo."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if numero < 0:
        return None
    return numero / (1024 * 1024)


def _primer_dato(datos: dict, claves) -> object:
    """Primer valor no-None del resumen entre varios alias de la misma clave."""
    for clave in claves:
        if isinstance(datos, dict) and datos.get(clave) is not None:
            return datos.get(clave)
    return None


def _resumen_datos_extra(resumen) -> dict:
    """Metricas extra del resumen (datos del proxy y captchas). Funcion PURA.

    Solo incluye las claves que el backend trae; si no vienen, devuelve `{}`
    (la pagina no inventa metricas). `datos_proxy_total` se interpreta en
    BYTES (como `LocalForwardProxy.estadisticas`) y se muestra en MB."""
    datos = resumen if isinstance(resumen, dict) else {}
    extra: dict = {}
    if datos.get("navegadores") is not None:
        cantidad = _entero(datos.get("navegadores"))
        if cantidad > 0:
            extra["🪟 Navegadores"] = str(cantidad)
    total_mb = _mb_desde_bytes(datos.get("datos_proxy_total"))
    if total_mb is not None:
        extra["📡 Datos del proxy"] = f"{total_mb:.1f} MB"
    for clave, etiqueta in (
        ("captchas_resueltos", "🧠 Captchas resueltos"),
        ("captchas_agotados", "⏳ Captchas agotados"),
    ):
        if datos.get(clave) is not None:
            extra[etiqueta] = str(_entero(datos.get(clave)))
    return extra


def _caption_datos_proxy(resumen) -> str:
    """Caption con subida/bajada del proxy en MB (PURA; "" si no aplica)."""
    datos = resumen if isinstance(resumen, dict) else {}
    if _mb_desde_bytes(datos.get("datos_proxy_total")) is None:
        return ""
    partes = []
    subida = _mb_desde_bytes(_primer_dato(datos, _CLAVES_PROXY_SUBIDA))
    if subida is not None:
        partes.append(f"subida {subida:.1f} MB")
    bajada = _mb_desde_bytes(_primer_dato(datos, _CLAVES_PROXY_BAJADA))
    if bajada is not None:
        partes.append(f"bajada {bajada:.1f} MB")
    if not partes:
        return ""
    return "📡 Tráfico del proxy — " + " · ".join(partes) + "."


def _filas_por_cuenta(resumen) -> list:
    """Filas de la tabla por cuenta del resumen (`st.dataframe`). Pura.

    Acepta `por_cuenta` como dict `{usuario: {planificadas, hechas, exitosas,
    fallidas, urls}}` o como lista de dicts con `usuario`. Los links se cuentan
    de `urls` (si el backend la trae) o de los eventos con URL de esa cuenta.
    Nunca lanza."""
    datos = resumen if isinstance(resumen, dict) else {}
    por_cuenta = datos.get("por_cuenta") or {}
    links_eventos: dict = {}
    for evento in datos.get("eventos") or []:
        if not isinstance(evento, dict):
            continue
        usuario_evento = str(evento.get("usuario") or "").strip().lstrip("@")
        url_evento = str(evento.get("url") or "").strip()
        if usuario_evento and url_evento:
            links_eventos[usuario_evento] = (
                links_eventos.get(usuario_evento, 0) + 1
            )
    items: list = []
    if isinstance(por_cuenta, dict):
        items = list(por_cuenta.items())
    elif isinstance(por_cuenta, (list, tuple)):
        for info in por_cuenta:
            if isinstance(info, dict) and info.get("usuario"):
                items.append((info.get("usuario"), info))
    filas = []
    for usuario, info in items[:300]:
        detalle = info if isinstance(info, dict) else {}
        usuario_limpio = str(usuario).strip().lstrip("@")
        urls = detalle.get("urls") or []
        links = len(urls) if isinstance(urls, (list, tuple)) else 0
        if not links:
            links = links_eventos.get(usuario_limpio, 0)
        filas.append(
            {
                "Cuenta": f"@{usuario_limpio}",
                "Planificadas": _entero(detalle.get("planificadas")),
                "Hechas": _entero(detalle.get("hechas")),
                "✅ Exitosas": _entero(detalle.get("exitosas")),
                "❌ Fallidas": _entero(detalle.get("fallidas")),
                "🔗 Links": links,
            }
        )
    return filas


def _eventos_para_panel(snap: dict) -> list:
    """Eventos a mostrar (los del resumen si el callback no entrego ninguno)."""
    eventos = list(snap.get("eventos") or [])
    if eventos:
        return eventos
    resumen = snap.get("resumen") or {}
    if isinstance(resumen, dict) and isinstance(resumen.get("eventos"), list):
        return [
            evento
            for evento in resumen["eventos"]
            if isinstance(evento, dict)
        ]
    return []


# ============================ CUENTAS ============================

def _etiqueta_cuenta(usuario: str, nombre: str = "") -> str:
    """Etiqueta del multiselect: `@usuario — nombre` (o solo `@usuario`)."""
    nombre = " ".join(str(nombre or "").split())
    if nombre:
        return f"@{usuario} — {nombre}"
    return f"@{usuario}"


def _cargar_cuentas_tier1() -> list:
    """Cuentas Twitter activas de Tier 1 (con sesion/pausa para la UI).

    Comparacion PEREZOSA (dentro del render) para que la pagina no dependa de
    la BD al importarse. Devuelve dicts:
    `{"usuario", "nombre", "etiqueta", "tiene_sesion", "pausada"}`.
    Nunca lanza: ante error muestra el detalle y devuelve [] (la pagina sigue).
    """
    try:
        from core.database import get_db_session
        from core.models import Cuenta
        from core.tiers import normalizar_tier
        from web.operaciones._helpers import es_pausada_activacion
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudieron importar las dependencias de cuentas: {e}")
        return []

    try:
        with get_db_session() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.activa == True)
                .order_by(Cuenta.usuario)
                .all()
            )
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudieron cargar las cuentas Tier 1: {e}")
        return []

    filas: list = []
    for cuenta in cuentas:
        usuario = str(getattr(cuenta, "usuario", "") or "").strip()
        if not usuario:
            continue
        try:
            tier = normalizar_tier(getattr(cuenta, "tier_calidad", ""))
        except Exception:
            tier = ""
        if tier != "tier1":
            continue
        nombre = str(
            getattr(cuenta, "nombre_mostrado", "")
            or getattr(cuenta, "handle_actual", "")
            or ""
        ).strip()
        try:
            pausada = bool(es_pausada_activacion(cuenta))
        except Exception:
            pausada = False
        filas.append(
            {
                "usuario": usuario,
                "nombre": nombre,
                "etiqueta": _etiqueta_cuenta(usuario, nombre),
                "tiene_sesion": _cuenta_tiene_sesion(cuenta),
                "pausada": pausada,
            }
        )
    return filas


def _defaults_cuentas(filas: list, maximo: int = MAX_CUENTAS) -> list:
    """Etiquetas por defecto del multiselect: las candidatas con sesion.

    Prioriza las que tienen sesion y NO estan pausadas; si no hay suficientes,
    las primeras con sesion; y si ninguna tiene sesion, las primeras a secas.
    Con `maximo <= 0` (sin limite) devuelve TODAS las candidatas; con un
    `maximo > 0` recorta a esa cantidad. Nunca lanza."""
    filas = [fila for fila in (filas or []) if isinstance(fila, dict)]
    candidatas = [
        fila for fila in filas if fila.get("tiene_sesion") and not fila.get("pausada")
    ]
    if not candidatas:
        candidatas = [fila for fila in filas if fila.get("tiene_sesion")]
    if not candidatas:
        candidatas = filas
    etiquetas = [
        str(fila.get("etiqueta") or "")
        for fila in candidatas
        if str(fila.get("etiqueta") or "")
    ]
    try:
        tope = int(maximo)
    except (TypeError, ValueError):
        tope = MAX_CUENTAS
    if tope > 0:
        etiquetas = etiquetas[:tope]
    return etiquetas


# ============================ LANZAMIENTO ============================

def _limite_hora_por_cuenta() -> int:
    """Acciones/accion por hora segun las cuotas (posts + citas).

    Con los defaults del proyecto (5 posts + 5 citas) devuelve 10. La UI lo
    usa como default y como referencia del "limite por hora"; nunca lanza.
    """
    try:
        from core.config import settings

        total = int(getattr(settings, "limite_posts_hora", 5)) + int(
            getattr(settings, "limite_citas_hora", 5)
        )
        return min(49, max(1, total))
    except Exception:
        return 10


def _parametros_default() -> dict:
    """Defaults del formulario desde `settings` (con fallbacks seguros).

    Default = el **limite por hora** (10 = 5 posts + 5 citas). El usuario
    puede subirlo hasta 49 (tope diario de X); el backend recorta al cupo que
    quede realmente libre hoy.
    """
    acciones = 10
    duracion = 60
    limite = 49
    try:
        from core.config import settings

        limite = _entero(getattr(settings, "limite_local_dia", 49), 49)
        hora = _limite_hora_por_cuenta()
        acciones = max(
            hora, _entero(getattr(settings, "local_acciones_cuenta", 10), 10)
        )
        duracion = _entero(getattr(settings, "local_duracion_min", 60), 60)
    except Exception:
        pass
    if limite > 0:
        acciones = min(acciones, limite)
    acciones = min(49, max(1, acciones))
    duracion = min(120, max(5, duracion))
    limite = min(49, max(0, limite))
    return {"acciones": acciones, "duracion": duracion, "limite": limite}


def _lanzar(
    usuarios,
    urls_texto,
    contexto,
    hashtags,
    narrativa,
    texto_base,
    imagenes,
    probabilidad_imagen,
    acciones,
    duracion,
    limite_dia,
    dar_like,
    permitir_pausadas,
    dejar_abierto,
    pausa_entre_acciones_seg: int = 2,
    capturar_url: bool = True,
    usar_proxy: bool = True,
    sin_imagenes: bool = True,
    esperar_captcha_seg: int = 0,
    navegadores: int = 1,
) -> None:
    """Valida y lanza `ejecutar_campana_local` en un hilo (nunca lanza).

    La importacion del backend es PEREZOSA (aqui dentro): si `activaciones.
    local` todavia no existe, la pagina lo dice y no rompe. El resumen final y
    el progreso quedan en el registro de modulo, asi que el panel sobrevive a
    recargas/navegacion."""
    usuarios = [str(usuario) for usuario in (usuarios or []) if usuario]
    urls = _lista_urls(urls_texto)
    tags = _normalizar_hashtags(hashtags)
    pausa_acciones = min(
        120, max(1, _entero(pausa_entre_acciones_seg, 2))
    )
    capturar_enlaces = bool(capturar_url)
    usa_proxy = bool(usar_proxy)
    ahorrar_imagenes = bool(sin_imagenes)
    espera_captcha = min(300, max(0, _entero(esperar_captcha_seg, 60)))
    navegadores_n = min(3, max(1, _entero(navegadores, 1)))

    ok, error, aviso = _validar_formulario(
        usuarios=usuarios,
        urls=urls,
        contexto=contexto,
        hashtags=tags,
        narrativa=narrativa,
        imagenes=imagenes,
    )
    if not ok:
        st.error(f"⛔ {error}")
        return
    if aviso:
        st.warning(f"⚠️ {aviso}")
    if _campana_en_curso() is not None:
        st.warning(
            "⛔ Ya hay una campaña local en curso: usa «⛔ Detener» del panel "
            "de arriba o espera a que termine."
        )
        return

    try:
        from activaciones.local import ejecutar_campana_local
    except Exception as e:  # noqa: BLE001
        st.error(
            "El backend local todavía no está disponible "
            "(`activaciones/local.py`): actualiza el proyecto y vuelve a "
            f"intentar. Detalle: {e}"
        )
        return
    if not callable(ejecutar_campana_local):
        st.error("El backend local no expone `ejecutar_campana_local`.")
        return

    evento = threading.Event()
    holder = {"id": ""}

    def _callback(evento_motor):
        """Callback del backend: solo muta el registro (otro hilo)."""
        _anotar_evento(holder["id"], evento_motor)

    parametros = {
        "usuarios": list(usuarios),
        "urls": list(urls),
        "hashtags": _hashtags_texto(tags),
        "contexto": str(contexto or "").strip(),
        "narrativa": str(narrativa or "").strip(),
        "texto_base": str(texto_base or "").strip(),
        "imagenes": list(imagenes or []),
        "probabilidad_imagen": _entero(probabilidad_imagen, 100),
        "acciones_por_cuenta": _entero(acciones, 6),
        "duracion_min": _entero(duracion, 60),
        "limite_dia": _entero(limite_dia, 49),
        "dar_like": bool(dar_like),
        "permitir_pausadas": bool(permitir_pausadas),
        "dejar_abierto": bool(dejar_abierto),
        "pausa_entre_acciones_seg": pausa_acciones,
        "capturar_url": capturar_enlaces,
        "usar_proxy": usa_proxy,
        "sin_imagenes": ahorrar_imagenes,
        "esperar_captcha_seg": espera_captcha,
        "navegadores": navegadores_n,
    }
    soporta_pausa = _soporta_kwarg(
        ejecutar_campana_local, "pausa_entre_acciones_seg"
    )
    soporta_capturar = _soporta_kwarg(ejecutar_campana_local, "capturar_url")
    soporta_proxy = _soporta_kwarg(ejecutar_campana_local, "usar_proxy")
    soporta_sin_imagenes = _soporta_kwarg(
        ejecutar_campana_local, "sin_imagenes"
    )
    soporta_espera = _soporta_kwarg(
        ejecutar_campana_local, "esperar_captcha_seg"
    )
    soporta_navegadores = _soporta_kwarg(
        ejecutar_campana_local, "navegadores"
    )
    if navegadores_n > 1 and not soporta_navegadores:
        st.warning(
            "⚠️ Backend sin soporte de varios navegadores; esta corrida usará "
            "1 navegador. Actualiza el proyecto para usar 1-3."
        )
        # El snapshot/captions deben reflejar lo que REALMENTE se ejecutara.
        parametros["navegadores"] = 1
    if usa_proxy and not soporta_proxy:
        st.warning(
            "⚠️ Backend sin soporte de proxies; actualiza el proyecto. "
            "La campaña se ejecutará con el comportamiento anterior (sin "
            "proxy)."
        )

    def _runner():
        resumen = {}
        error_lanzamiento = None
        try:
            kwargs_backend = {
                "usuarios": list(parametros["usuarios"]),
                "urls": list(parametros["urls"]),
                "hashtags": parametros["hashtags"],
                "texto_base": parametros["texto_base"],
                "contexto": parametros["contexto"],
                "narrativa": parametros["narrativa"],
                "menciones": "",
                "imagenes": list(parametros["imagenes"]) or None,
                "probabilidad_imagen": parametros["probabilidad_imagen"],
                "acciones_por_cuenta": parametros["acciones_por_cuenta"],
                "duracion_min": parametros["duracion_min"],
                "limite_dia": parametros["limite_dia"],
                "dar_like": parametros["dar_like"],
                "permitir_pausadas": parametros["permitir_pausadas"],
                "dejar_abierto": parametros["dejar_abierto"],
                "cancelar": evento,
                "callback": _callback,
            }
            # Backends viejos (sin los kwargs nuevos) siguen funcionando.
            if soporta_pausa:
                kwargs_backend["pausa_entre_acciones_seg"] = parametros[
                    "pausa_entre_acciones_seg"
                ]
            if soporta_capturar:
                kwargs_backend["capturar_url"] = parametros["capturar_url"]
            if soporta_proxy:
                kwargs_backend["usar_proxy"] = parametros["usar_proxy"]
            if soporta_sin_imagenes:
                kwargs_backend["sin_imagenes"] = parametros["sin_imagenes"]
            if soporta_espera:
                kwargs_backend["esperar_captcha_seg"] = parametros[
                    "esperar_captcha_seg"
                ]
            if soporta_navegadores:
                kwargs_backend["navegadores"] = parametros["navegadores"]
            resumen = ejecutar_campana_local(**kwargs_backend)
        except BaseException as e:  # noqa: BLE001
            error_lanzamiento = e
        finally:
            _finalizar_campana(
                holder["id"],
                resumen=resumen if isinstance(resumen, dict) else {},
                error=error_lanzamiento,
            )

    hilo = threading.Thread(target=_runner, daemon=True, name="campana-local")
    id_campana = _registrar_campana(
        hilo=hilo, evento=evento, parametros=parametros
    )
    if not id_campana:
        st.warning(
            "⛔ Ya hay una campaña local en curso: espera a que termine antes "
            "de lanzar otra."
        )
        return
    holder["id"] = id_campana
    try:
        hilo.start()
    except BaseException as e:  # noqa: BLE001
        _finalizar_campana(id_campana, resumen={}, error=e)
        st.error(f"No se pudo iniciar la campaña local: {e}")
        return
    if _en_contexto_streamlit():
        st.rerun()


# ============================ PANEL PERSISTENTE ============================

def _pintar_en_vivo(snap: dict) -> None:
    """Barra + contador + feed + boton "⛔ Detener" de la campana local viva."""
    st.markdown("#### 🖥️ Campaña local en vivo")
    total = max(1, _entero(snap.get("total")))
    hechas = min(_entero(snap.get("hechas")), total) if snap.get("total") else 0
    st.progress(
        min(1.0, hechas / total),
        text=f"✅ {hechas} / {snap.get('total') or '?'} acciones",
    )
    contador = (
        f"✅ **{hechas} / {snap.get('total') or '?'}**"
        f" · ✅ {snap.get('exitosas')} · ❌ {snap.get('fallidas')}"
        f" · ➖ {snap.get('omitidas')}"
    )
    if _entero(snap.get("ronda")):
        contador += f" · 🔄 Ronda {snap.get('ronda')}"
    st.markdown(contador)
    eventos = _eventos_para_panel(snap)[-8:]
    if eventos:
        st.markdown(
            "  \n".join(
                linea
                for linea in (_formatear_evento(evento) for evento in eventos)
                if linea
            )
        )
    else:
        st.markdown("⏳ Esperando las primeras acciones en Chrome…")
    if snap.get("deteniendo"):
        st.warning(
            "⛔ Detención solicitada: el navegador local se cerrará al "
            "terminar la acción en curso."
        )
    if st.button(
        "⛔ Detener",
        type="primary",
        key=f"{PREFIX}_btn_detener",
        disabled=bool(snap.get("deteniendo")),
        help=(
            "Pide el paro total: no se abren cuentas nuevas y el navegador "
            "local se cierra al terminar la acción en curso."
        ),
    ):
        if _solicitar_paro():
            st.rerun()
    st.caption(
        "⛔ Detener cierra el navegador local en cuanto termina la acción en "
        "curso; el resto de acciones queda omitido."
    )


def _pintar_terminada(snap: dict) -> None:
    """Resumen final de la ultima campana local (metricas, tabla y eventos)."""
    resumen = snap.get("resumen") or {}
    st.markdown("#### 🧾 Última campaña local")
    error_resumen = str(resumen.get("error") or "").strip()
    motivo = str(resumen.get("motivo") or "").strip()
    if snap.get("estado") == "error" or snap.get("error"):
        st.error(
            "❌ La campaña local se interrumpió: "
            + (snap.get("error") or "error inesperado del backend")
        )
    elif resumen.get("cancelada"):
        st.warning(
            f"⛔ Campaña local detenida: {snap.get('exitosas')} exitosas / "
            f"{snap.get('fallidas')} fallidas."
        )
    elif resumen.get("ok") is False or error_resumen:
        st.error(
            "❌ La campaña local terminó con error: "
            + (error_resumen or motivo or "motivo desconocido")
        )
    else:
        linea = (
            f"✅ Campaña local finalizada: {snap.get('exitosas')} exitosas / "
            f"{snap.get('fallidas')} fallidas"
        )
        if _entero(resumen.get("planificadas")):
            linea += f" · {_entero(resumen.get('planificadas'))} planificadas"
        st.success(linea)

    metricas = _resumen_metricas(resumen)
    etiquetas = list(metricas)
    columnas = st.columns(4)
    for indice, etiqueta in enumerate(etiquetas):
        columnas[indice % 4].metric(etiqueta, metricas[etiqueta])

    extras = _resumen_datos_extra(resumen)
    if extras:
        columnas_extra = st.columns(len(extras))
        for columna, (etiqueta, valor) in zip(columnas_extra, extras.items()):
            columna.metric(etiqueta, valor)
        caption_proxy = _caption_datos_proxy(resumen)
        if caption_proxy:
            st.caption(caption_proxy)

    for linea in _captions_resumen(resumen, snap.get("parametros")):
        st.caption(linea)

    filas = _filas_por_cuenta(resumen)
    if filas:
        st.dataframe(filas, use_container_width=True, hide_index=True)

    eventos = _eventos_para_panel(snap)
    if eventos:
        with st.expander(
            f"🔍 Eventos de la campaña ({len(eventos)})", expanded=False
        ):
            for evento in eventos[-LOG_MAX:]:
                linea = _formatear_evento(evento)
                if linea:
                    st.markdown(linea)
    else:
        st.caption("Sin eventos registrados.")

    dejar_abierto = bool((snap.get("parametros") or {}).get("dejar_abierto"))
    if resumen.get("navegador_abierto") or dejar_abierto:
        if st.button(
            "🪟 Cerrar navegador local",
            key=f"{PREFIX}_btn_cerrar",
            help=(
                "Cierra el Chrome que quedó abierto al terminar la campaña "
                "(se ejecuta en este equipo)."
            ),
        ):
            try:
                from activaciones.local import cerrar_navegadores_locales

                cerrados = cerrar_navegadores_locales()
            except Exception as e:  # noqa: BLE001
                st.error(f"No se pudo cerrar el navegador local: {e}")
            else:
                st.success(
                    "🪟 Navegador local cerrado "
                    f"({_entero(cerrados)} cerrado(s))."
                )


def _pintar_proceso() -> None:
    """Pinta UNA pasada del panel: en vivo o el resumen final (`st.*` aqui)."""
    snap = _snapshot()
    if not snap:
        return
    if snap.get("en_curso"):
        _pintar_en_vivo(snap)
    else:
        _pintar_terminada(snap)


def _refrescar_con_fragmento() -> bool:
    """`st.fragment(run_every=1s)` que repinta la campana local en vivo.

    Devuelve False si esta version de Streamlit no soporta fragmentos (el
    llamador cae al bucle `time.sleep(1); st.rerun()`). Al terminar la campana,
    el fragmento dispara UN rerun completo y deja de refrescar."""
    fabrica = getattr(st, "fragment", None)
    if not callable(fabrica):
        return False

    def _pasada():
        if _campana_en_curso() is None:
            st.rerun(scope="app")
            return
        _pintar_proceso()

    try:
        decorada = fabrica(run_every=1.0)(_pasada)
    except Exception:
        return False
    decorada()
    return True


def _render_proceso_activo(max_pasos=None) -> None:
    """Panel de proceso persistente (auto-refresco SOLO mientras corre).

    - Con campana en curso: `st.fragment(run_every=1s)` si esta disponible; si
      no, bucle `time.sleep(1); st.rerun()`.
    - Sin campana en curso: UNA pasada (ultimo resumen en memoria o nada).

    `max_pasos` es un flag interno para tests/llamadas acotadas: con un valor
    (p. ej. 1) se pinta UNA sola pasada, sin fragmentos ni bucles."""
    if max_pasos is not None:
        _pintar_proceso()
        return
    if _campana_en_curso() is None:
        _pintar_proceso()
        return
    if _refrescar_con_fragmento():
        return
    _pintar_proceso()
    time.sleep(1)
    st.rerun()


# ============================ FORMULARIO / RENDER ============================

def _formulario(filas: list) -> None:
    """Formulario completo del modo local (sin campana en curso)."""
    defaults = _parametros_default()
    tope_cuentas = MAX_CUENTAS  # 0 = sin limite: toda la flota Tier 1

    contexto = st.text_area(
        "📝 Contexto / tema de los posts",
        height=100,
        key=f"{PREFIX}_contexto",
        placeholder=(
            "Ej. la importancia de la participación ciudadana en las "
            "próximas elecciones"
        ),
        help=(
            "Obligatorio si no hay URLs para citas. La IA lo usa como tema y "
            "NO lo copia literalmente. Los posts se generan con el registro y "
            "el perfil de cada cuenta."
        ),
    )
    hashtags = st.text_area(
        "🏷️ Hashtags (separados por espacio o coma)",
        height=70,
        key=f"{PREFIX}_hashtags",
        placeholder="#Mexico, #Comunidad",
        help=(
            "Se garantiza el '#' y se eliminan duplicados. Si lo dejas vacío, "
            "se buscan hashtags en el contexto y en el texto base."
        ),
    )
    narrativa = st.text_area(
        "📰 Trasfondo / noticias (NO se publica)",
        height=80,
        key=f"{PREFIX}_narrativa",
        placeholder=(
            "Pega aquí las notas o datos de contexto: se usan SOLO como "
            "referencia interna; nunca se citan ni se publican."
        ),
        help=(
            "La IA lo usa como trasfondo invisible: prohibido mencionar "
            "medios, enlaces, cifras o frases de las noticias."
        ),
    )
    urls_texto = st.text_area(
        "🔗 URLs del tweet ancla (una por línea)",
        height=90,
        key=f"{PREFIX}_urls",
        placeholder="https://x.com/cuenta/status/123...",
        help=(
            "Para los retweets con cita. Si lo dejas vacío, la campaña se "
            "centra en los posts originales."
        ),
    )
    texto_base = st.text_area(
        "💬 Texto base de la cita",
        height=70,
        key=f"{PREFIX}_texto_base",
        placeholder="Ej. Esto es importante para México",
        help=(
            "Punto de partida de la cita; la IA lo varia por cuenta según su "
            "registro/perfil (máximo 280 caracteres)."
        ),
    )
    st.caption(
        "Los textos se piden completos y pueden ocupar hasta 280 caracteres "
        "(límite de X), con el hashtag integrado en medio."
    )
    if not _normalizar_hashtags(hashtags):
        auto_ui = _hashtags_detectados(contexto, texto_base)
        if auto_ui:
            st.info(
                "🏷️ Se detectaron hashtags en el contexto/texto base: "
                f"{_hashtags_texto(auto_ui)}. Se usarán en el texto."
            )
        else:
            st.warning(
                "⚠️ Sin hashtags escritos: el texto podría salir sin `#`. "
                "Escríbelos aquí (ej. #Muuki2026); si los pones en el contexto "
                "o en el texto base, el backend intentará detectarlos."
            )

    st.markdown("#### 🖼️ Imágenes para los posts")
    archivos_imagenes = st.file_uploader(
        "Imágenes para los posts (puedes subir varias)",
        type=["png", "jpg", "jpeg", "gif", "webp"],
        accept_multiple_files=True,
        key=f"{PREFIX}_imagenes",
        help=(
            "Se elige UNA al azar por post. En el modo local los posts van "
            "con imagen por defecto; si no subes ninguna, saldrán solo con "
            "texto."
        ),
    )
    probabilidad_imagen = st.slider(
        "Probabilidad de publicar con imagen (%)",
        min_value=0,
        max_value=100,
        value=100,
        step=5,
        key=f"{PREFIX}_prob_imagen",
        help=(
            "Porcentaje de posts que llevan una imagen al azar de las "
            "subidas (100 = todos; 0 = ninguno)."
        ),
    )
    rutas_imagenes: list = []
    if archivos_imagenes:
        try:
            from web.operaciones._helpers import guardar_imagen_subida
        except Exception as e:  # noqa: BLE001
            st.warning(f"No se pudo preparar el guardado de imágenes: {e}")
        else:
            for indice, archivo in enumerate(archivos_imagenes):
                ruta = guardar_imagen_subida(
                    archivo, prefijo=f"local_img_{indice}"
                )
                if ruta:
                    rutas_imagenes.append(ruta)
            if rutas_imagenes:
                st.caption(
                    f"🖼️ {len(rutas_imagenes)} imagen(es) lista(s): cada post "
                    f"llevará una al azar el {_entero(probabilidad_imagen)}% "
                    "de las veces."
                )
            fallidas = len(archivos_imagenes) - len(rutas_imagenes)
            if fallidas > 0:
                st.warning(
                    f"⚠️ {fallidas} imagen(es) no se pudieron guardar y se "
                    "ignorarán."
                )

    st.markdown("#### ⚙️ Ritmo y tope")
    col_acciones, col_ventana, col_tope = st.columns(3)
    with col_acciones:
        acciones = st.number_input(
            "🔁 Acciones por cuenta en la ventana",
            min_value=1,
            max_value=49,
            value=defaults["acciones"],
            step=1,
            key=f"{PREFIX}_acciones",
            help=(
                "10 = 5 posts con imagen + 5 citas (límite por hora). "
                "49 = tope diario de X (25 posts + 24 citas; más agresivo). "
                "El backend descuenta lo que la cuenta ya publicó hoy."
            ),
        )
    with col_ventana:
        duracion = st.number_input(
            "⏱️ Ventana de ejecución (minutos)",
            min_value=5,
            max_value=120,
            value=defaults["duracion"],
            step=5,
            key=f"{PREFIX}_duracion",
            help=(
                "Es el MÁXIMO de la campaña: las acciones se reparten con la "
                "pausa mínima elegida, así que puede terminar antes (con 2 s "
                "de pausa, mucho antes de la hora)."
            ),
        )
    with col_tope:
        limite_dia = st.number_input(
            "📅 Tope diario por cuenta",
            min_value=0,
            max_value=49,
            value=defaults["limite"],
            step=1,
            key=f"{PREFIX}_limite_dia",
            help=(
                "0 = sin tope. X permite ~49-50 posts/día en cuentas no "
                "premium; el valor por defecto es 49."
            ),
        )
    col_pausa, col_capturar = st.columns([1, 2])
    with col_pausa:
        pausa_acciones = st.number_input(
            "⏱️ Pausa entre acciones (segundos)",
            min_value=1,
            max_value=120,
            value=2,
            step=1,
            key=f"{PREFIX}_pausa_acciones",
            help=(
                "2 s = máxima velocidad; súbela solo si X marca las cuentas "
                "por ráfagas."
            ),
        )
    with col_capturar:
        capturar_url = st.checkbox(
            "🔗 Capturar enlace de cada publicación (instantáneo)",
            value=True,
            key=f"{PREFIX}_capturar_url",
            help=(
                "El link se toma del aviso de X y del timeline sin abrir el "
                "perfil: no cuesta 5-10s por acción. Si no aparece, la fila de "
                "Reportes dirá «link no capturado»."
            ),
        )
    navegadores = st.number_input(
        "🪟 Navegadores simultáneos (Chrome visibles)",
        min_value=1,
        max_value=3,
        value=2,
        step=1,
        key=f"{PREFIX}_navegadores",
        help=(
            "Más navegadores = más velocidad entre cuentas, pero más RAM/CPU. "
            "Sin proxies salen por la misma IP y X puede limitar. No promete "
            "indetectabilidad; usa 1 si tu equipo va justo de memoria."
        ),
    )
    st.caption(
        "⚡ El plan se reparte en la ventana con la pausa elegida (2 s = "
        "máxima velocidad); el enlace de cada publicación se captura al "
        "instante sin abrir el perfil. 🎲 Orden de cuentas aleatorio en cada ciclo."
    )

    dar_like = st.checkbox(
        "❤️ Dar like al tweet ancla en las citas",
        value=False,
        key=f"{PREFIX}_dar_like",
    )
    permitir_pausadas = st.checkbox(
        "🔕 Incluir cuentas pausadas (no recomendado)",
        value=False,
        key=f"{PREFIX}_pausadas",
        help=(
            "Por defecto las cuentas pausadas para activación quedan FUERA. "
            "Márcalo solo si sabes lo que haces."
        ),
    )
    dejar_abierto = st.checkbox(
        "🪟 Dejar el navegador abierto al terminar",
        value=False,
        key=f"{PREFIX}_dejar_abierto",
        help=(
            "Al terminar NO se cierra Chrome: podrás revisar el timeline y "
            "cerrarlo con el botón «🪟 Cerrar navegador local»."
        ),
    )

    st.markdown("### 👥 Cuentas Tier 1")
    total = len(filas)
    con_sesion = len([fila for fila in filas if fila.get("tiene_sesion")])
    pausadas = len([fila for fila in filas if fila.get("pausada")])
    columnas = st.columns(3)
    columnas[0].metric("Tier 1 total", total)
    columnas[1].metric("Tier 1 con sesión", con_sesion)
    columnas[2].metric("Pausadas", pausadas)

    mapa = {
        fila["etiqueta"]: fila["usuario"]
        for fila in filas
        if fila.get("etiqueta") and fila.get("usuario")
    }
    etiquetas = list(mapa)
    seleccion = st.multiselect(
        "Cuentas Tier 1 (sin límite)",
        etiquetas,
        default=_defaults_cuentas(filas, maximo=tope_cuentas),
        key=f"{PREFIX}_cuentas",
        help=(
            "Solo aparecen cuentas Tier 1 activas. Puedes seleccionar toda la "
            "flota: de 1 a 3 navegadores cambian de sesión con las cookies de "
            "cada cuenta."
        ),
    )
    usuarios_sel = [mapa[etiqueta] for etiqueta in seleccion if etiqueta in mapa]
    st.caption(f"Seleccionadas: **{len(usuarios_sel)}**")
    # Aviso informativo: la activacion ya omite las cuentas en verificacion
    # (core.verificacion); no cambia nada de esta pagina.
    aviso_verificacion()
    if tope_cuentas > 0 and len(usuarios_sel) > tope_cuentas:
        st.error(
            f"⛔ El modo local acepta como máximo {tope_cuentas} cuentas "
            f"(seleccionaste {len(usuarios_sel)}): quita algunas y vuelve a "
            "intentar."
        )
    st.caption(
        f"⚡ Plan: **{acciones} por cuenta** · **{len(usuarios_sel)}** "
        "seleccionadas. 10 = 5 posts + 5 citas (límite por hora); 49 = tope "
        "diario (25 posts + 24 citas, más agresivo). El plan se reparte en la "
        "ventana; con la pausa de 2s se ejecuta lo antes posible y el enlace "
        "de cada publicación se captura al instante."
    )
    if usuarios_sel and not any(
        fila.get("tiene_sesion")
        for fila in filas
        if fila.get("usuario") in set(usuarios_sel)
    ):
        st.warning(
            "⚠️ Ninguna de las cuentas seleccionadas tiene sesión guardada "
            "(auth_token/cookies): el backend las omitirá sin abrir Chrome."
        )

    st.markdown("#### 🌐 Red y datos")
    col_proxy, col_sin_imagenes = st.columns(2)
    with col_proxy:
        usar_proxy = st.checkbox(
            "🌐 Usar proxies residenciales (MX)",
            value=True,
            key=f"{PREFIX}_usar_proxy",
            help=(
                "Un proxy sticky por cuenta; reparte la salida y evita la "
                "verificación de Cloudflare"
            ),
        )
    with col_sin_imagenes:
        sin_imagenes = st.checkbox(
            "🖼️ No cargar imágenes de X (ahorra datos)",
            value=True,
            key=f"{PREFIX}_sin_imagenes",
            help=(
                "No descarga imágenes/video del timeline; las imágenes que "
                "publicas sí se suben (comprimidas)"
            ),
        )
    esperar_captcha = st.number_input(
        "🧠 Esperar a que resuelvas el reto Cloudflare (seg)",
        min_value=0,
        max_value=300,
        value=0,
        step=10,
        key=f"{PREFIX}_esperar_captcha",
        help=(
            "0 = saltar rápido y guardar la cuenta en el cajón de verificación "
            "humana (no se vuelve a usar hasta revisarla). Con >0, el bot espera "
            "a que resuelvas la verificación en el Chrome visible y reintenta"
        ),
    )
    st.caption(CAPTION_AHORRO)

    st.info(PANEL_INFO)
    st.caption(
        "🛡️ Solo Tier 1, proxies opcionales, Chrome visible y de 1 a 3 "
        "navegadores que cambian de cuenta con cookies. El tope diario cuenta "
        "las acciones exitosas de las últimas 24 h por cuenta."
    )

    if st.button(
        "▶️ Ejecutar en local",
        type="primary",
        key=f"{PREFIX}_btn_ejecutar",
        help=(
            "Abre Chrome en ESTE equipo y reparte posts con imagen + citas "
            "del tweet ancla en la ventana elegida."
        ),
    ):
        _lanzar(
            usuarios=usuarios_sel,
            urls_texto=urls_texto,
            contexto=contexto,
            hashtags=hashtags,
            narrativa=narrativa,
            texto_base=texto_base,
            imagenes=rutas_imagenes,
            probabilidad_imagen=probabilidad_imagen,
            acciones=acciones,
            duracion=duracion,
            limite_dia=limite_dia,
            dar_like=dar_like,
            permitir_pausadas=permitir_pausadas,
            dejar_abierto=dejar_abierto,
            pausa_entre_acciones_seg=pausa_acciones,
            capturar_url=capturar_url,
            usar_proxy=usar_proxy,
            sin_imagenes=sin_imagenes,
            esperar_captcha_seg=esperar_captcha,
            navegadores=navegadores,
        )


def _en_contenedor() -> bool:
    """True si el dashboard corre en Railway/Docker/Kubernetes.

    La campaña local abre Chrome VISIBLE en la maquina donde corre Streamlit.
    En un contenedor no hay pantalla para el operador: es mejor avisar y no
    gastar acciones en una ventana invisible. Nunca lanza."""
    try:
        return bool(
            os.environ.get("RAILWAY_ENVIRONMENT")
            or os.environ.get("KUBERNETES_SERVICE_HOST")
            or os.path.exists("/.dockerenv")
        )
    except Exception:
        return False


def render(usuario: dict):
    """Pagina "🖥️ LOCAL (FLOTA)": campaña con Chrome visible."""
    cabecera(
        "🖥️ LOCAL (FLOTA)",
        "Chrome visible en tu computadora: posts con imagen + citas",
    )
    st.info(PANEL_INFO)

    if _en_contenedor():
        st.warning(
            "🖥️ Esta operación abre Chrome **visible** en el equipo donde corre "
            "el dashboard. En Railway/Docker no hay una pantalla para ti: no "
            "la uses ahí. Corre el dashboard en tu computadora "
            "(`python iniciar_dashboard.py`) o usa «🎯 Activación Masiva»."
        )
        return

    _render_proceso_activo()
    if _campana_en_curso() is not None:
        st.caption(
            "🚦 Hay una campaña local en curso: usa el panel de arriba para "
            "verla o detenerla; al terminar podrás lanzar otra."
        )
        return

    filas = _cargar_cuentas_tier1()
    if not filas:
        st.warning(
            "No hay cuentas Twitter activas de **Tier 1**. Asigna el Tier en "
            "«🗂️ Cuentas: Perfiles, Secciones & Nombres» → «🏅 Tiers»."
        )
        return
    _formulario(filas)
