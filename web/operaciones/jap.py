"""Operacion JAP: COMPRAR ENGAGEMENT (likes, vistas, seguidores y comentarios).

La pagina consume el cliente backend ya existente ``integraciones.jap``
(Just Another Panel API v2). NO habla HTTP por si misma: todas las llamadas
pasan por el contrato publico del cliente, que NUNCA lanza y devuelve dicts
con ``ok``/``error``.

Reglas de la operacion
----------------------
- Es SOLO para admin (`OPCIONES_ADMIN` en `web/app.py`): los pedidos gastan
  saldo real del panel JAP. El dashboard NO cobra tarjeta: el saldo se recarga
  en el panel de JAP.
- La API key se guarda con ``jap.guardar_api_key`` (data/jap_config.json) y
  NUNCA se muestra, ni siquiera en errores o logs. La UI solo indica
  "configurada"/"no configurada".
- El catalogo (~5,700 servicios) se cachea en ``st.session_state`` con TTL de
  1 hora para no pedirlo en cada rerun. El boton "🔄 Actualizar catálogo" usa
  ``jap.listar_servicios(forzar=True)``.
- JAP NO tiene servicios de X especificos de Mexico (aviso visible). Para
  comentarios en espanol solo existen los servicios USA: 6601 (Random) y
  6602 (Custom Comments), que aceptan texto libre pero cuyas cuentas son
  estadounidenses.

Pestanas
--------
1. "📊 Catálogo": filtros por plataforma/tipo/texto y tabla ordenada por
   precio ascendente (maximo 300 filas visibles).
2. "🧮 Cotizador (lote de tweets)": carga los 18 tweets de @RedAMLOmx,
   calcula el costo por link con presets Economico/Seguro y avisa si el saldo
   de JAP no alcanza. NO coloca pedidos.
3. "🚀 Nuevo pedido": crea una orden real (checkbox de confirmacion
   obligatorio) y la guarda en ``jap_pedidos_recientes``.
4. "📦 Pedidos": consulta estados, refill y cancelacion con confirmacion.

Pruebas: ``tests/test_jap_web.py`` (AppTest + mocks, sin red real).
"""
from __future__ import annotations

import re
import time
import unicodedata
from datetime import datetime

import streamlit as st

from web.ui import cabecera

# ---------------------------------------------------------------------------
# Constantes publicas (las usan la UI y los tests)
# ---------------------------------------------------------------------------
LINKS_REDAMLO = [
    "https://x.com/RedAMLOmx/status/2125500199543898152",
    "https://x.com/RedAMLOmx/status/2125500339696640417",
    "https://x.com/RedAMLOmx/status/2125500461528539401",
    "https://x.com/RedAMLOmx/status/2125500091960094754",
    "https://x.com/RedAMLOmx/status/2125499942428905728",
    "https://x.com/RedAMLOmx/status/2125499768696660191",
    "https://x.com/RedAMLOmx/status/2125501345058697341",
    "https://x.com/RedAMLOmx/status/2125501157942477153",
    "https://x.com/RedAMLOmx/status/2125501622289580155",
    "https://x.com/RedAMLOmx/status/2125502290547134673",
    "https://x.com/RedAMLOmx/status/2125502501663183059",
    "https://x.com/RedAMLOmx/status/2125502676150423878",
    "https://x.com/RedAMLOmx/status/2125503141340663884",
    "https://x.com/RedAMLOmx/status/2125675678301319500",
    "https://x.com/RedAMLOmx/status/2125676625668772181",
    "https://x.com/RedAMLOmx/status/2125678630961660063",
    "https://x.com/RedAMLOmx/status/2125679250619654423",
    "https://x.com/RedAMLOmx/status/2125682537393787196",
]

# Servicios de comentarios verificados en el catalogo real de JAP.
SERVICIO_COMENTARIOS_ECONOMICO = 6601  # USA Random Comments
SERVICIO_COMENTARIOS_SEGURO = 6602  # USA Custom Comments

CLAVE_PEDIDOS = "jap_pedidos_recientes"
CLAVE_CATALOGO = "jap_catalogo_cache"
CLAVE_BALANCE = "jap_balance_cache"
CLAVE_ULTIMO_PEDIDO = "jap_ultimo_pedido"
CLAVE_ESTADOS = "jap_estados_consulta"

TTL_CATALOGO_SEG = 3600
TTL_BALANCE_SEG = 120
MAX_FILAS_CATALOGO = 300
MAX_SERVICIOS_PEDIDO = 100
MAX_PEDIDOS_SESION = 50

PLATAFORMAS = [
    ("X / Twitter", "x"),
    ("Instagram", "instagram"),
    ("TikTok", "tiktok"),
    ("Facebook", "facebook"),
    ("YouTube", "youtube"),
    ("Todas", ""),
]

TIPOS_SERVICIO = [
    "likes",
    "retweets",
    "views",
    "comments",
    "followers",
    "impressions",
    "mentions",
    "other",
]

PRESETS = ["Económico", "Seguro"]

TIPOS_COTIZADOR = ("likes", "retweets", "comments", "views", "followers")

# Respaldo de ids ya verificados (se usan solo si `servicios_recomendados`
# no devuelve un preset completo; p. ej. catalogo cacheado incompleto).
IDS_RESPALDO = {
    "economico": {
        "likes": 9013,
        "retweets": 8860,
        "comments": SERVICIO_COMENTARIOS_ECONOMICO,
        "views": 8413,
        "followers": 9011,
    },
    "seguro": {
        "likes": 10186,
        "retweets": 8861,
        "comments": SERVICIO_COMENTARIOS_SEGURO,
        "views": 8555,
        "followers": 8855,
    },
}

AVISO_MEXICO = (
    "JAP no tiene servicios X específicos de México. Para comentarios en "
    "español usa el servicio Custom Comments (cuentas USA) o nuestras cuentas "
    "propias; los servicios 'México' solo existen para Facebook/YouTube/"
    "tráfico web."
)

_ESTADOS_AYUDA = (
    "**Estados típicos de JAP:**\n"
    "- **Pending**: la orden se recibió y está en cola.\n"
    "- **In progress**: el proveedor está entregando.\n"
    "- **Processing**: entrega pausada/revisión del proveedor.\n"
    "- **Completed**: entregada por completo.\n"
    "- **Partial**: entregó una parte (JAP suele reembolsar el resto).\n"
    "- **Canceled**: cancelada (revisa el reembolso).\n"
    "- **Refunded**: saldo devuelto a tu cuenta."
)


# ---------------------------------------------------------------------------
# Utilidades (sin red)
# ---------------------------------------------------------------------------
def _cliente():
    """Import perezoso del backend. None si el modulo no esta instalado."""
    try:
        from integraciones import jap as cliente

        return cliente
    except Exception:
        return None


def _norm(texto) -> str:
    """Minusculas sin acentos para comparar textos del catalogo."""
    cadena = str(texto or "").lower()
    descompuesto = unicodedata.normalize("NFKD", cadena)
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _a_float(valor):
    try:
        if valor is None or isinstance(valor, bool):
            return None
        return float(str(valor).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _a_int(valor):
    try:
        if valor is None or isinstance(valor, bool):
            return None
        numero = float(str(valor).strip())
        if not numero.is_integer():
            return None
        return int(numero)
    except (TypeError, ValueError):
        return None


def _cache_leer(clave: str, ttl_seg: int):
    """Entrada vigente de `st.session_state` o None (nunca lanza)."""
    try:
        entrada = st.session_state.get(clave)
        if not isinstance(entrada, dict):
            return None
        momento = _a_float(entrada.get("_ts")) or 0.0
        if (time.time() - momento) >= ttl_seg:
            return None
        return entrada.get("datos")
    except Exception:
        return None


def _cache_guardar(clave: str, datos):
    try:
        st.session_state[clave] = {"_ts": time.time(), "datos": datos}
    except Exception:
        pass


def _cache_limpiar(clave: str):
    try:
        st.session_state.pop(clave, None)
    except Exception:
        pass


def _normalizar_links(texto) -> list:
    """Links http(s) validos, sin numeracion, vacios ni duplicados."""
    vistos = set()
    links = []
    for linea in str(texto or "").splitlines():
        limpio = linea.strip().rstrip(",;")
        if not limpio:
            continue
        limpio = re.sub(r"^\s*\d+\s*[\-\.\)]\s*", "", limpio)
        if not re.match(r"(?i)^https?://\S+$", limpio):
            continue
        if limpio in vistos:
            continue
        vistos.add(limpio)
        links.append(limpio)
    return links


def _parsear_ids(texto) -> list:
    """IDs de pedido enteros > 0, unicos, de un texto separado por comas."""
    ids = []
    vistos = set()
    for parte in re.split(r"[,\s;]+", str(texto or "")):
        limpio = parte.strip().lstrip("#")
        if not limpio:
            continue
        numero = _a_int(limpio)
        if numero is None or numero <= 0 or numero in vistos:
            continue
        vistos.add(numero)
        ids.append(numero)
    return ids


def _clave_plataforma(etiqueta: str) -> str:
    for nombre, valor in PLATAFORMAS:
        if nombre == etiqueta:
            return valor
    return ""


def _plataforma_de(servicio: dict) -> str:
    """Plataforma legible de un servicio (tolerante a catalogos raros)."""
    jap = _cliente()
    try:
        if jap is not None and jap.es_servicio_x(servicio):
            return "X / Twitter"
    except Exception:
        pass
    texto = _norm(
        f"{servicio.get('category') or ''} {servicio.get('name') or ''}"
    )
    for clave, etiqueta in (
        ("facebook", "Facebook"),
        ("instagram", "Instagram"),
        ("tiktok", "TikTok"),
        ("youtube", "YouTube"),
        ("twitter", "X / Twitter"),
        ("telegram", "Telegram"),
        ("spotify", "Spotify"),
        ("discord", "Discord"),
        ("threads", "Threads"),
        ("linkedin", "LinkedIn"),
    ):
        if clave in texto:
            return etiqueta
    if re.search(r"\bx\b", texto):
        return "X / Twitter"
    return "Otra"


def _etiqueta_servicio(servicio: dict) -> str:
    """'ID · categoría · nombre · $rate/1K · min-max' para selectboxes."""
    servicio_id = _a_int(servicio.get("service"))
    categoria = str(servicio.get("category") or "Sin categoría")
    nombre = str(servicio.get("name") or "Sin nombre")
    rate = _a_float(servicio.get("rate"))
    minimo = _a_int(servicio.get("min")) or 0
    maximo = _a_int(servicio.get("max")) or 0
    return (
        f"{servicio_id if servicio_id is not None else '—'} · {categoria} · "
        f"{nombre} · ${rate if rate is not None else 0:.4f}/1K · "
        f"{minimo}-{maximo}"
    )


def _es_custom(servicio: dict) -> bool:
    texto = _norm(
        f"{servicio.get('name') or ''} {servicio.get('type') or ''}"
    )
    return "custom" in texto


# ---------------------------------------------------------------------------
# Caches de red (catalogo / saldo)
# ---------------------------------------------------------------------------
def _disponible() -> bool:
    jap = _cliente()
    if jap is None:
        return False
    try:
        return bool(jap.disponible())
    except Exception:
        return False


def _catalogo(forzar: bool = False) -> dict:
    """Catalogo de JAP cacheado en session_state (TTL 1 hora)."""
    if not forzar:
        cache = _cache_leer(CLAVE_CATALOGO, TTL_CATALOGO_SEG)
        if isinstance(cache, dict):
            return cache
    jap = _cliente()
    if jap is None:
        datos = {
            "ok": False,
            "servicios": [],
            "error": "El cliente de JAP (integraciones.jap) no está disponible",
        }
        _cache_guardar(CLAVE_CATALOGO, datos)
        return datos
    try:
        try:
            datos = jap.listar_servicios(forzar=bool(forzar))
        except TypeError:
            datos = jap.listar_servicios()
    except Exception as exc:
        datos = {
            "ok": False,
            "servicios": [],
            "error": f"No se pudo consultar el catálogo de JAP: {exc}",
        }
    if not isinstance(datos, dict):
        datos = {
            "ok": False,
            "servicios": [],
            "error": "JAP devolvió un catálogo con formato inesperado",
        }
    if not isinstance(datos.get("servicios"), list):
        datos = dict(datos)
        datos["servicios"] = []
    _cache_guardar(CLAVE_CATALOGO, datos)
    return datos


def _balance(forzar: bool = False) -> dict:
    """Saldo actual de JAP cacheado en session_state (TTL corto)."""
    if not forzar:
        cache = _cache_leer(CLAVE_BALANCE, TTL_BALANCE_SEG)
        if isinstance(cache, dict):
            return cache
    jap = _cliente()
    if jap is None:
        datos = {"ok": False, "balance": 0.0, "currency": "", "error": "Sin cliente JAP"}
        _cache_guardar(CLAVE_BALANCE, datos)
        return datos
    try:
        datos = jap.obtener_balance()
    except Exception as exc:
        datos = {
            "ok": False,
            "balance": 0.0,
            "currency": "",
            "error": f"No se pudo consultar el saldo: {exc}",
        }
    if not isinstance(datos, dict):
        datos = {
            "ok": False,
            "balance": 0.0,
            "currency": "",
            "error": "JAP devolvió un saldo con formato inesperado",
        }
    _cache_guardar(CLAVE_BALANCE, datos)
    return datos


def _servicios_x(servicios) -> int:
    jap = _cliente()
    total = 0
    for servicio in servicios or []:
        if not isinstance(servicio, dict):
            continue
        try:
            if jap is not None and jap.es_servicio_x(servicio):
                total += 1
                continue
        except Exception:
            pass
        if _plataforma_de(servicio) == "X / Twitter":
            total += 1
    return total


# ---------------------------------------------------------------------------
# Filtros del catalogo
# ---------------------------------------------------------------------------
def _servicios_filtrados(plataforma: str, tipo: str = "", texto: str = "") -> dict:
    """Servicios por plataforma/tipo/texto usando el catalogo YA cacheado.

    Para X delega en ``jap.filtrar_servicios`` (que usa su propia cache); para
    el resto filtra localmente por ``category``/``name`` sin red adicional.
    """
    jap = _cliente()
    if jap is None:
        return {"ok": False, "servicios": [], "error": "Cliente JAP no disponible"}

    clave = _norm(plataforma)
    if clave in ("", "todas", "all"):
        datos = _catalogo()
        servicios = [
            s for s in (datos.get("servicios") or []) if isinstance(s, dict)
        ]
        filtrados = []
        for servicio in servicios:
            if tipo:
                try:
                    if jap.clasificar_servicio(servicio) != tipo:
                        continue
                except Exception:
                    continue
            if texto:
                campos = _norm(
                    f"{servicio.get('name') or ''} "
                    f"{servicio.get('category') or ''} "
                    f"{servicio.get('service') or ''}"
                )
                if _norm(texto) not in campos:
                    continue
            filtrados.append(servicio)
        return {
            "ok": bool(datos.get("ok")),
            "servicios": filtrados,
            "error": str(datos.get("error") or ""),
        }

    if clave in ("x", "twitter"):
        try:
            datos = jap.filtrar_servicios(
                plataforma="x", tipo=tipo or "", texto=texto or ""
            )
        except Exception as exc:
            return {
                "ok": False,
                "servicios": [],
                "error": f"No se pudo filtrar el catálogo: {exc}",
            }
        if not isinstance(datos, dict):
            return {
                "ok": False,
                "servicios": [],
                "error": "JAP devolvió un filtro con formato inesperado",
            }
        return {
            "ok": bool(datos.get("ok")),
            "servicios": [
                s for s in (datos.get("servicios") or []) if isinstance(s, dict)
            ],
            "error": str(datos.get("error") or ""),
        }

    datos = _catalogo()
    filtrados = []
    for servicio in datos.get("servicios") or []:
        if not isinstance(servicio, dict):
            continue
        campos = _norm(
            f"{servicio.get('category') or ''} {servicio.get('name') or ''}"
        )
        if clave not in campos:
            continue
        if tipo:
            try:
                if jap.clasificar_servicio(servicio) != tipo:
                    continue
            except Exception:
                continue
        if texto and _norm(texto) not in campos:
            continue
        filtrados.append(servicio)
    return {
        "ok": bool(datos.get("ok")),
        "servicios": filtrados,
        "error": str(datos.get("error") or ""),
    }


def _por_precio(servicios) -> list:
    """Servicios ordenados por precio ascendente (sin precio al final)."""
    def clave(servicio):
        rate = _a_float(servicio.get("rate"))
        return (rate is None or rate <= 0, rate if rate is not None else 0.0)

    return sorted(
        [s for s in (servicios or []) if isinstance(s, dict)], key=clave
    )


# ---------------------------------------------------------------------------
# Cotizador: presets y costos
# ---------------------------------------------------------------------------
def _servicio_por_id(servicio_id) -> dict:
    numero = _a_int(servicio_id)
    if numero is None:
        return {}
    for servicio in _catalogo().get("servicios") or []:
        if isinstance(servicio, dict) and _a_int(servicio.get("service")) == numero:
            return servicio
    return {}


def _presets_resueltos() -> dict:
    """{'economico': {tipo: {id,nombre,rate,min,max}}, 'seguro': {...}}.

    Usa ``jap.servicios_recomendados()`` (que resuelve ambos presets contra el
    catalogo real) y cae a los ids verificados del cliente si faltan.
    """
    respuesta = {"economico": {}, "seguro": {}}
    jap = _cliente()
    datos = {}
    if jap is not None:
        try:
            datos = jap.servicios_recomendados()
        except Exception:
            datos = {}
    presets = (datos or {}).get("presets") or {}

    for nombre in ("economico", "seguro"):
        bloque = presets.get(nombre) or {}
        if not isinstance(bloque, dict):
            continue
        for tipo, info in bloque.items():
            if not isinstance(info, dict):
                continue
            servicio = info.get("servicio")
            if not isinstance(servicio, dict):
                servicio = {}
            rate = _a_float(info.get("rate"))
            if rate is None:
                rate = _a_float(servicio.get("rate"))
            respuesta[nombre][tipo] = {
                "id": (
                    _a_int(servicio.get("service"))
                    or _a_int(info.get("servicio_id"))
                ),
                "nombre": str(
                    info.get("nombre") or servicio.get("name") or ""
                ),
                "rate": rate,
                "min": _a_int(info.get("min")) or _a_int(servicio.get("min")),
                "max": _a_int(info.get("max")) or _a_int(servicio.get("max")),
            }

    respaldo = IDS_RESPALDO
    if jap is not None and isinstance(getattr(jap, "PRESETS", None), dict):
        respaldo = getattr(jap, "PRESETS") or IDS_RESPALDO
    for nombre in ("economico", "seguro"):
        for tipo, servicio_id in (respaldo.get(nombre) or {}).items():
            actual = respuesta[nombre].get(tipo) or {}
            if actual.get("id"):
                continue
            info = _servicio_por_id(servicio_id)
            respuesta[nombre][tipo] = {
                "id": _a_int(servicio_id),
                "nombre": str(info.get("name") or ""),
                "rate": _a_float(info.get("rate")),
                "min": _a_int(info.get("min")),
                "max": _a_int(info.get("max")),
            }
    return respuesta


def _cotizar(servicio_id, cantidad):
    """(costo, error) con ``jap.calcular_costo``. Nunca lanza."""
    valor = _a_int(cantidad)
    if valor is None or valor <= 0:
        return 0.0, ""
    jap = _cliente()
    if jap is None or servicio_id is None:
        return 0.0, "Servicio no disponible en el catálogo"
    try:
        resultado = jap.calcular_costo(servicio_id, valor)
    except Exception as exc:
        return 0.0, f"No se pudo calcular el costo: {exc}"
    if not isinstance(resultado, dict):
        return 0.0, "JAP devolvió un costo con formato inesperado"
    if not resultado.get("ok"):
        return 0.0, str(resultado.get("error") or "Costo no disponible")
    return _a_float(resultado.get("costo")) or 0.0, ""


def _id_comentarios(preset: str, personalizados: bool) -> int:
    """Comentarios: 6602 si hay texto propio o preset Seguro; si no, 6601."""
    if personalizados or preset == "Seguro":
        return SERVICIO_COMENTARIOS_SEGURO
    return SERVICIO_COMENTARIOS_ECONOMICO


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render(usuario: dict):
    rol = str((usuario or {}).get("rol") or "").strip().lower()
    if rol and rol != "admin":
        st.error(
            "Esta operación es solo para administradores: los pedidos gastan "
            "saldo real de JAP."
        )
        return
    cabecera(
        "🛒 JAP: COMPRAR ENGAGEMENT",
        "Panel externo de likes, vistas, seguidores y comentarios",
    )
    st.markdown(
        "**JAP** (Just Another Panel) es un panel externo de engagement. Los "
        "pedidos se pagan con el **saldo de JAP**; aquí no se cobra ninguna "
        "tarjeta: el saldo se recarga directamente en el panel de JAP. Desde "
        "esta página puedes consultar el catálogo, cotizar un lote y comprar."
    )

    disponible = _disponible()

    boton_refrescar, _ = st.columns([1, 3])
    with boton_refrescar:
        refrescar = st.button(
            "🔄 Actualizar catálogo",
            key="jap_btn_refresh",
            help="Vuelve a pedir el catálogo y el saldo a JAP (ignora la caché).",
        )
    if refrescar:
        _catalogo(forzar=True)
        _balance(forzar=True)
        _cache_limpiar(CLAVE_ESTADOS)
        st.rerun()

    catalogo = _catalogo()
    balance = _balance()
    servicios = [
        s for s in (catalogo.get("servicios") or []) if isinstance(s, dict)
    ]
    total_x = _servicios_x(servicios)

    col1, col2, col3 = st.columns(3)
    with col1:
        if balance.get("ok"):
            moneda = str(balance.get("currency") or "USD")
            st.metric(
                "Saldo actual (USD)",
                f"${_a_float(balance.get('balance')) or 0.0:,.2f}",
                moneda,
            )
        else:
            st.metric("Saldo actual (USD)", "—")
    with col2:
        st.metric(
            "API key de JAP",
            "✅ Configurada" if disponible else "⚠️ No configurada",
        )
    with col3:
        st.metric(
            "Servicios de X",
            f"{total_x:,}" if servicios else "—",
            "en el catálogo" if servicios else "",
        )

    with st.expander("🔑 API key de JAP", expanded=not disponible):
        st.caption(
            "La key se guarda localmente (`data/jap_config.json`, permisos "
            "restringidos en Linux) o en la variable de entorno `JAP_API_KEY`. "
            "Nunca se muestra ni se registra en logs."
        )
        st.caption(
            "Estado: **configurada**"
            if disponible
            else "Estado: **no configurada**"
        )
        # Limpiar el campo ANTES de instanciarlo (tras guardar) para no dejar
        # la key en el session_state de la sesión.
        if st.session_state.pop("jap_limpiar_key", False):
            try:
                st.session_state["jap_api_key_input"] = ""
            except Exception:
                pass
        nueva_key = st.text_input(
            "Pega tu API key de JAP",
            type="password",
            key="jap_api_key_input",
            placeholder="API key de justanotherpanel.com",
        )
        if st.button("Guardar", key="jap_btn_guardar_key"):
            jap = _cliente()
            if jap is None:
                st.error("El cliente de JAP no está disponible.")
            elif not str(nueva_key or "").strip():
                st.warning("La API key no puede estar vacía.")
            else:
                try:
                    resultado = jap.guardar_api_key(str(nueva_key).strip())
                except Exception as exc:
                    resultado = {"ok": False, "error": str(exc)}
                if isinstance(resultado, dict) and resultado.get("ok"):
                    _cache_limpiar(CLAVE_CATALOGO)
                    _cache_limpiar(CLAVE_BALANCE)
                    st.session_state["jap_limpiar_key"] = True
                    st.success("API key guardada. Catálogo y saldo actualizados.")
                    st.rerun()
                else:
                    error = (
                        resultado.get("error")
                        if isinstance(resultado, dict)
                        else ""
                    ) or "No se pudo guardar la API key"
                    st.error(str(error))

    if not disponible:
        st.info(
            "Para comprar necesitas configurar la API key de JAP en el panel "
            "de arriba (o en la variable `JAP_API_KEY`). Sin key la página "
            "solo muestra el aviso: las compras permanecen deshabilitadas."
        )
    if catalogo.get("error"):
        st.warning(str(catalogo.get("error")))
    elif balance.get("error"):
        st.warning(str(balance.get("error")))

    st.warning(AVISO_MEXICO)

    tabs = st.tabs(
        [
            "📊 Catálogo",
            "🧮 Cotizador (lote de tweets)",
            "🚀 Nuevo pedido",
            "📦 Pedidos",
        ]
    )
    with tabs[0]:
        _tab_catalogo()
    with tabs[1]:
        _tab_cotizador(disponible)
    with tabs[2]:
        _tab_nuevo_pedido(disponible)
    with tabs[3]:
        _tab_pedidos(disponible)


# ---------------------------------------------------------------------------
# Pestaña 1: Catálogo
# ---------------------------------------------------------------------------
def _tab_catalogo():
    st.markdown("### 📊 Catálogo de servicios JAP")
    col1, col2, col3 = st.columns([2, 2, 3])
    with col1:
        plataforma_etiqueta = st.selectbox(
            "Plataforma",
            [etiqueta for etiqueta, _ in PLATAFORMAS],
            key="jap_cat_plataforma",
        )
    with col2:
        tipo_etiqueta = st.selectbox(
            "Tipo",
            ["Todas"] + TIPOS_SERVICIO,
            key="jap_cat_tipo",
        )
    with col3:
        busqueda = st.text_input(
            "Buscar en el catálogo",
            key="jap_cat_texto",
            placeholder="likes, views, followers...",
        )

    plataforma = _clave_plataforma(plataforma_etiqueta)
    tipo = "" if tipo_etiqueta == "Todas" else tipo_etiqueta
    datos = _servicios_filtrados(plataforma, tipo, busqueda)
    if not datos.get("ok") and not datos.get("servicios"):
        st.error(
            str(datos.get("error") or "No se pudo consultar el catálogo de JAP")
        )
        return
    if datos.get("error"):
        st.caption(f"⚠️ {datos.get('error')}")

    servicios = _por_precio(datos.get("servicios") or [])
    if not servicios:
        st.info("No hay servicios que coincidan con los filtros.")
        return

    filas = []
    for servicio in servicios:
        filas.append(
            {
                "ID": _a_int(servicio.get("service")),
                "Plataforma": _plataforma_de(servicio),
                "Tipo": _clasificar(servicio),
                "Servicio": str(servicio.get("name") or ""),
                "Precio/1K USD": _a_float(servicio.get("rate")) or 0.0,
                "Mín": _a_int(servicio.get("min")) or 0,
                "Máx": _a_int(servicio.get("max")) or 0,
                "Refill": "✅" if servicio.get("refill") else "—",
                "Cancelar": "✅" if servicio.get("cancel") else "—",
                "Categoría": str(servicio.get("category") or ""),
            }
        )

    st.dataframe(
        filas[:MAX_FILAS_CATALOGO],
        hide_index=True,
        use_container_width=True,
        column_config={
            "Precio/1K USD": st.column_config.NumberColumn(
                "Precio/1K USD", format="$%.4f"
            ),
        },
    )
    st.caption(
        f"Mostrando {min(len(filas), MAX_FILAS_CATALOGO)} de {len(filas)} "
        "servicios (ordenados por precio ascendente). JAP cobra por cada "
        "1,000 unidades."
    )


def _clasificar(servicio: dict) -> str:
    jap = _cliente()
    try:
        if jap is not None:
            return str(jap.clasificar_servicio(servicio))
    except Exception:
        pass
    return "other"


# ---------------------------------------------------------------------------
# Pestaña 2: Cotizador
# ---------------------------------------------------------------------------
def _cargar_links_redamlo():
    """on_click: rellena el textarea con los 18 tweets (sin tocar la UI)."""
    try:
        st.session_state["jap_quote_links"] = "\n".join(LINKS_REDAMLO)
    except Exception:
        pass


def _tab_cotizador(disponible: bool):
    st.markdown("### 🧮 Cotizador (lote de tweets)")
    st.caption(
        "Calcula cuánto costaría comprar engagement para varios tweets. "
        "Este cotizador NO coloca pedidos: solo estima el costo con JAP."
    )

    st.button(
        "📥 Cargar los 18 tweets de @RedAMLOmx",
        key="jap_btn_cargar_18",
        on_click=_cargar_links_redamlo,
    )
    texto_links = st.text_area(
        "Links (uno por línea)",
        key="jap_quote_links",
        height=180,
        placeholder="https://x.com/usuario/status/123...",
    )
    links = _normalizar_links(texto_links)
    if links:
        st.caption(f"✅ {len(links)} link(s) válido(s) (sin duplicados).")
    else:
        st.info("Pega los links de los tweets o carga los 18 de @RedAMLOmx.")

    preset = st.radio(
        "Preset de servicios",
        PRESETS,
        horizontal=True,
        key="jap_quote_preset",
    )
    presets = _presets_resueltos()
    clave_preset = "seguro" if preset == "Seguro" else "economico"

    with st.expander(
        "💬 Comentarios personalizados (español, uno por línea)", expanded=False
    ):
        texto_comentarios = st.text_area(
            "Comentarios a publicar",
            key="jap_quote_comments_texto",
            height=120,
            placeholder="Uno por línea. Cuentas USA, texto libre en español.",
        )
        st.caption(
            "⚠️ Las cuentas de estos servicios son de Estados Unidos aunque el "
            "texto sea en español. Si escribes comentarios aquí se usa el "
            "servicio **6602 (USA Custom)**; si no, el **6601 (USA Random)**."
        )
    personalizados = bool(str(texto_comentarios or "").strip())

    st.markdown("**Cantidades por tweet** (JAP cobra por 1,000 unidades):")
    st.caption(
        "Pon **0** en lo que NO quieras comprar (likes, RT, comentarios, "
        "vistas o seguidores). Si pones un valor mayor a 0, JAP exige el "
        "mínimo de ese servicio."
    )
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        likes = st.number_input(
            "👍 Likes",
            min_value=0,
            value=50,
            step=10,
            key="jap_quote_likes",
            help="0 = no pedir likes.",
        )
    with c2:
        retweets = st.number_input(
            "🔁 Retweets",
            min_value=0,
            value=10,
            step=10,
            key="jap_quote_rts",
            help="0 = no pedir retweets.",
        )
    with c3:
        comentarios = st.number_input(
            "💬 Comentarios",
            min_value=0,
            value=5,
            step=5,
            key="jap_quote_comments",
            help=(
                "0 = no pedir comentarios. Si compras, JAP exige mínimo 5 "
                "por pedido."
            ),
        )
    with c4:
        vistas = st.number_input(
            "👁️ Vistas",
            min_value=0,
            value=500,
            step=100,
            key="jap_quote_views",
            help="0 = no pedir vistas.",
        )
    with c5:
        seguidores = st.number_input(
            "👥 Seguidores totales",
            min_value=0,
            value=0,
            step=10,
            key="jap_quote_followers",
            help=(
                "0 = no pedir seguidores. Se piden una sola vez al perfil "
                "(mínimo 10 si compras)."
            ),
        )

    perfil = st.text_input(
        "Link del perfil (solo si vas a comprar seguidores)",
        key="jap_quote_perfil",
        placeholder="https://x.com/usuario",
    )

    sid_comentarios = _id_comentarios(preset, personalizados)
    info_comentarios = _servicio_por_id(sid_comentarios)
    if not info_comentarios:
        info_comentarios = dict(presets.get(clave_preset, {}).get("comments") or {})

    st.markdown("**Servicios del preset seleccionado:**")
    resumen_preset = {
        "likes": presets.get(clave_preset, {}).get("likes") or {},
        "retweets": presets.get(clave_preset, {}).get("retweets") or {},
        "comments": {
            "id": sid_comentarios,
            "nombre": str(info_comentarios.get("nombre") or ""),
            "rate": _a_float(info_comentarios.get("rate")),
        },
        "views": presets.get(clave_preset, {}).get("views") or {},
        "followers": presets.get(clave_preset, {}).get("followers") or {},
    }
    for tipo in TIPOS_COTIZADOR:
        info = resumen_preset.get(tipo) or {}
        rate = _a_float(info.get("rate"))
        st.markdown(
            f"- **{tipo.capitalize()}**: `{info.get('id') or '—'}` · "
            f"{info.get('nombre') or '—'} · "
            f"{'$%.4f/1K' % rate if rate is not None else 'precio no disponible'}"
        )

    if personalizados and int(comentarios) > 0:
        st.caption(
            f"Se usarán {len([l for l in str(texto_comentarios).splitlines() if l.strip()])} "
            f"comentario(s) personalizado(s) con el servicio {sid_comentarios}. "
            f"Necesitas al menos {int(comentarios)} por link si compras."
        )

    if not links:
        return

    filas, avisos = _calcular_lote(
        links,
        {
            "likes": int(likes),
            "retweets": int(retweets),
            "comments": int(comentarios),
            "views": int(vistas),
        },
        resumen_preset,
    )
    total_lote = sum(fila["Total $"] for fila in filas)

    costo_seguidores, error_seguidores = 0.0, ""
    if int(seguidores) > 0:
        if int(seguidores) < 10:
            avisos.append("Seguidores: el mínimo es 10; no se cotizó el total.")
        else:
            costo_seguidores, error_seguidores = _cotizar(
                (resumen_preset.get("followers") or {}).get("id"),
                int(seguidores),
            )
            if error_seguidores:
                avisos.append(f"Seguidores: {error_seguidores}")
            else:
                total_lote += costo_seguidores
                if not str(perfil or "").strip():
                    avisos.append(
                        "Seguidores: indica el link del perfil para poder "
                        "comprarlos (en la tabla solo se muestra el costo)."
                    )

    st.dataframe(
        filas,
        hide_index=True,
        use_container_width=True,
        column_config={
            "Likes $": st.column_config.NumberColumn(format="$%.4f"),
            "RT $": st.column_config.NumberColumn(format="$%.4f"),
            "Comentarios $": st.column_config.NumberColumn(format="$%.4f"),
            "Vistas $": st.column_config.NumberColumn(format="$%.4f"),
            "Total $": st.column_config.NumberColumn(format="$%.4f"),
        },
    )
    st.caption(
        f"{len(filas)} link(s) · {len(avisos)} aviso(s). Precios basados en "
        "el catálogo cacheado de JAP."
    )

    balance = _balance()
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Total estimado del lote", f"${total_lote:,.4f}")
    with m2:
        if balance.get("ok"):
            st.metric(
                "Saldo actual JAP",
                f"${_a_float(balance.get('balance')) or 0.0:,.4f}",
            )
        else:
            st.metric("Saldo actual JAP", "—")
    with m3:
        if balance.get("ok"):
            saldo = _a_float(balance.get("balance")) or 0.0
            if total_lote > saldo:
                st.metric(
                    "Falta saldo",
                    f"${total_lote - saldo:,.4f}",
                    "el lote supera tu saldo",
                )
            else:
                st.metric(
                    "Saldo restante",
                    f"${saldo - total_lote:,.4f}",
                    "alcanza para el lote",
                )
        else:
            st.metric("Falta saldo", "—")

    if int(seguidores) >= 10:
        st.caption(
            f"Seguidores: {int(seguidores)} × "
            f"${_a_float((resumen_preset.get('followers') or {}).get('rate')) or 0:.4f}/1K "
            f"= ${costo_seguidores:,.4f} (una sola vez, al perfil)."
        )
    if avisos:
        with st.expander(f"⚠️ Avisos del cálculo ({len(avisos)})", expanded=False):
            for aviso in avisos:
                st.markdown(f"- {aviso}")
    if not disponible:
        st.caption(
            "Sin API key configurada este cálculo es una estimación: no se "
            "pueden colocar pedidos."
        )


def _calcular_lote(links, cantidades, servicios) -> tuple:
    """Filas (link, likes/RT/comentarios/vistas/total) y avisos únicos."""
    filas = []
    avisos = []
    cache_costos = {}

    def costo(tipo: str, cantidad: int) -> float:
        info = servicios.get(tipo) or {}
        servicio_id = info.get("id")
        clave = (servicio_id, cantidad)
        if clave in cache_costos:
            valor, error = cache_costos[clave]
        else:
            valor, error = _cotizar(servicio_id, cantidad)
            cache_costos[clave] = (valor, error)
        if error:
            aviso = f"{tipo.capitalize()} (servicio {servicio_id}): {error}"
            if aviso not in avisos:
                avisos.append(aviso)
        return valor

    for link in links:
        costo_likes = costo("likes", cantidades["likes"])
        costo_rts = costo("retweets", cantidades["retweets"])
        costo_comentarios = costo("comments", cantidades["comments"])
        costo_vistas = costo("views", cantidades["views"])
        filas.append(
            {
                "Link": link,
                "Likes $": costo_likes,
                "RT $": costo_rts,
                "Comentarios $": costo_comentarios,
                "Vistas $": costo_vistas,
                "Total $": (
                    costo_likes
                    + costo_rts
                    + costo_comentarios
                    + costo_vistas
                ),
            }
        )
    return filas, avisos


# ---------------------------------------------------------------------------
# Pestaña 3: Nuevo pedido
# ---------------------------------------------------------------------------
def _tab_nuevo_pedido(disponible: bool):
    st.markdown("### 🚀 Nuevo pedido")
    st.caption(
        "Crea un pedido real. El costo se descuenta del saldo de JAP cuando "
        "marques la confirmación."
    )

    ultimo = st.session_state.get(CLAVE_ULTIMO_PEDIDO)
    if isinstance(ultimo, dict) and ultimo.get("order"):
        st.success(
            f"✅ Pedido #{ultimo.get('order')} creado por "
            f"${_a_float(ultimo.get('costo')) or 0.0:,.4f} "
            f"({ultimo.get('servicio_nombre') or ultimo.get('servicio')})."
        )

    col1, col2 = st.columns(2)
    with col1:
        plataforma_etiqueta = st.selectbox(
            "Plataforma",
            [etiqueta for etiqueta, _ in PLATAFORMAS],
            key="jap_buy_plataforma",
        )
    with col2:
        tipo_etiqueta = st.selectbox(
            "Tipo",
            ["Todas"] + TIPOS_SERVICIO,
            key="jap_buy_tipo",
        )

    plataforma = _clave_plataforma(plataforma_etiqueta)
    tipo = "" if tipo_etiqueta == "Todas" else tipo_etiqueta
    datos = _servicios_filtrados(plataforma, tipo, "")
    servicios = _por_precio(datos.get("servicios") or [])
    if not servicios:
        st.warning(
            str(datos.get("error") or "No hay servicios para ese filtro.")
        )
        return
    if datos.get("error"):
        st.caption(f"⚠️ {datos.get('error')}")

    opciones = servicios[:MAX_SERVICIOS_PEDIDO]
    etiquetas = [_etiqueta_servicio(servicio) for servicio in opciones]
    seleccion = st.selectbox(
        f"Servicio ({len(opciones)} de {len(servicios)} · más baratos primero)",
        etiquetas,
    )
    servicio = opciones[etiquetas.index(seleccion)]
    servicio_id = _a_int(servicio.get("service"))
    es_custom = _es_custom(servicio)

    link = st.text_input(
        "Link del post/perfil",
        key="jap_buy_link",
        placeholder="https://x.com/usuario/status/...",
    )
    cantidad = st.number_input(
        "Cantidad",
        min_value=0,
        value=50,
        step=10,
        key="jap_buy_cantidad",
        help=(
            f"0 = no comprar. Si compras: mín "
            f"{_a_int(servicio.get('min')) or 0} · máx "
            f"{_a_int(servicio.get('max')) or 0}"
        ),
    )

    comentarios = ""
    if es_custom:
        comentarios = st.text_area(
            "Comentarios (uno por línea)",
            key="jap_buy_comments",
            height=120,
            placeholder="Comentario 1\nComentario 2\n...",
        )
        lineas = len([l for l in str(comentarios).splitlines() if l.strip()])
        if int(cantidad) > 0:
            st.caption(
                f"Necesitas al menos {int(cantidad)} línea(s) para este "
                f"servicio Custom Comments (llevas {lineas}). Las cuentas son "
                "USA aunque el texto sea en español."
            )
        else:
            st.caption(
                "Cantidad 0: no se usará este servicio de comentarios. Las "
                "cuentas son USA aunque el texto sea en español."
            )

    costo, error_costo = _cotizar(servicio_id, int(cantidad))
    balance = _balance()
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Costo estimado", f"${costo:,.4f}" if servicio_id else "—")
    with m2:
        if balance.get("ok"):
            st.metric(
                "Saldo JAP",
                f"${_a_float(balance.get('balance')) or 0.0:,.4f}",
            )
        else:
            st.metric("Saldo JAP", "—")
    with m3:
        if error_costo:
            st.metric("Validación", "⚠️ Revisa")
        elif balance.get("ok") and costo > (
            _a_float(balance.get("balance")) or 0.0
        ):
            st.metric("Validación", "❌ Saldo insuficiente")
        else:
            st.metric("Validación", "✅ Listo")

    if error_costo:
        st.warning(str(error_costo))
    elif balance.get("ok"):
        saldo = _a_float(balance.get("balance")) or 0.0
        if costo > saldo:
            st.error(
                f"Saldo insuficiente: el pedido cuesta ${costo:,.4f} y tienes "
                f"${saldo:,.4f}. Recarga saldo en el panel de JAP."
            )

    confirmo = st.checkbox(
        "Confirmo que este pedido gasta saldo real de JAP",
        key="jap_buy_confirmo",
    )
    if int(cantidad) <= 0:
        st.info(
            "Cantidad 0: no se creará ningún pedido. Elige una cantidad "
            "mayor a 0 si quieres comprar este servicio."
        )
    puede_comprar = bool(
        disponible
        and servicio_id
        and int(cantidad) > 0
        and str(link or "").strip()
        and not error_costo
        and confirmo
    )
    if st.button(
        "💳 Comprar en JAP",
        key="jap_btn_comprar",
        type="primary",
        disabled=not puede_comprar,
    ):
        _comprar(servicio, int(cantidad), str(link or "").strip(), comentarios)


def _comprar(servicio: dict, cantidad: int, link: str, comentarios: str):
    """Crea la orden UNA vez y guarda el pedido en session_state."""
    if st.session_state.get("jap_comprando"):
        return
    st.session_state["jap_comprando"] = True
    servicio_id = _a_int(servicio.get("service"))
    resultado = {"ok": False, "error": "No se pudo enviar el pedido"}
    try:
        jap = _cliente()
        if jap is None:
            raise RuntimeError("El cliente de JAP no está disponible")
        try:
            resultado = jap.crear_orden(
                servicio_id,
                link,
                cantidad,
                comments=str(comentarios or ""),
            )
        except TypeError:
            # Backend viejo sin el kwarg `comments`.
            resultado = jap.crear_orden(servicio_id, link, cantidad)
    except Exception as exc:
        resultado = {"ok": False, "error": f"Error al crear el pedido: {exc}"}
    finally:
        st.session_state["jap_comprando"] = False

    if not isinstance(resultado, dict) or not resultado.get("ok"):
        error = (
            resultado.get("error")
            if isinstance(resultado, dict)
            else "respuesta inesperada"
        ) or "No se pudo crear el pedido"
        st.error(f"No se pudo completar el pedido: {error}")
        return

    pedido = {
        "order": _a_int(resultado.get("order")),
        "servicio": servicio_id,
        "servicio_nombre": str(servicio.get("name") or ""),
        "link": link,
        "cantidad": cantidad,
        "costo": _a_float(resultado.get("costo")) or 0.0,
        "comentarios": str(comentarios or ""),
        "fecha": datetime.now().isoformat(timespec="seconds"),
    }
    recientes = st.session_state.get(CLAVE_PEDIDOS)
    if not isinstance(recientes, list):
        recientes = []
    recientes.insert(0, pedido)
    st.session_state[CLAVE_PEDIDOS] = recientes[:MAX_PEDIDOS_SESION]
    st.session_state[CLAVE_ULTIMO_PEDIDO] = pedido
    _cache_limpiar(CLAVE_BALANCE)
    st.rerun()


# ---------------------------------------------------------------------------
# Pestaña 4: Pedidos
# ---------------------------------------------------------------------------
def _fila_estado(order_id, info: dict) -> dict:
    return {
        "ID": _a_int(order_id) or order_id,
        "Estado": str(info.get("status") or ""),
        "Charge": _a_float(info.get("charge")) or 0.0,
        "Start count": _a_int(info.get("start_count")) or 0,
        "Remains": _a_int(info.get("remains")) or 0,
        "Currency": str(info.get("currency") or ""),
    }


def _tab_pedidos(disponible: bool):
    st.markdown("### 📦 Pedidos")
    st.caption("Consulta el estado de tus pedidos, pide refill o cancela.")

    ids_texto = st.text_input(
        "IDs de pedido (separados por coma)",
        key="jap_orders_ids",
        placeholder="123456, 123457",
    )
    if st.button("Consultar estado", key="jap_btn_consultar"):
        ids = _parsear_ids(ids_texto)
        if not ids:
            st.warning("Escribe al menos un ID de pedido válido.")
        else:
            jap = _cliente()
            try:
                resultado = (
                    jap.estado_ordenes(ids) if jap is not None else {}
                )
            except Exception as exc:
                resultado = {"ok": False, "error": str(exc)}
            if isinstance(resultado, dict) and resultado.get("ok"):
                st.session_state[CLAVE_ESTADOS] = {
                    "ids": ids,
                    "ordenes": resultado.get("ordenes") or {},
                }
            else:
                error = (
                    resultado.get("error")
                    if isinstance(resultado, dict)
                    else ""
                ) or "No se pudo consultar el estado"
                st.error(str(error))

    consulta = st.session_state.get(CLAVE_ESTADOS)
    if isinstance(consulta, dict) and consulta.get("ordenes"):
        ordenes = consulta.get("ordenes") or {}
        filas = []
        if isinstance(ordenes, dict):
            for order_id, info in ordenes.items():
                if isinstance(info, dict):
                    filas.append(_fila_estado(order_id, info))
        elif isinstance(ordenes, list):
            for info in ordenes:
                if isinstance(info, dict):
                    filas.append(_fila_estado(info.get("order"), info))
        if filas:
            st.dataframe(
                filas,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Charge": st.column_config.NumberColumn(format="$%.4f"),
                },
            )
        else:
            st.info("JAP no devolvió datos para esos IDs.")

    recientes = st.session_state.get(CLAVE_PEDIDOS)
    if not isinstance(recientes, list):
        recientes = []
    if not recientes:
        st.info(
            "Todavía no hay pedidos en esta sesión. Crea uno en "
            "«🚀 Nuevo pedido»."
        )
    else:
        st.markdown("**Pedidos recientes de esta sesión**")
        st.dataframe(
            [
                {
                    "Pedido": pedido.get("order"),
                    "Servicio": pedido.get("servicio_nombre")
                    or pedido.get("servicio"),
                    "Link": pedido.get("link"),
                    "Cantidad": pedido.get("cantidad"),
                    "Costo": _a_float(pedido.get("costo")) or 0.0,
                    "Fecha": pedido.get("fecha"),
                }
                for pedido in recientes
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Costo": st.column_config.NumberColumn(format="$%.4f"),
            },
        )

        etiquetas = [
            f"#{pedido.get('order')} · "
            f"{pedido.get('servicio_nombre') or pedido.get('servicio')} · "
            f"${_a_float(pedido.get('costo')) or 0.0:,.4f}"
            for pedido in recientes
        ]
        seleccion = st.selectbox("Pedido para refill/cancelar", etiquetas)
        pedido = recientes[etiquetas.index(seleccion)]
        order_id = _a_int(pedido.get("order"))
        confirmo = st.checkbox(
            f"Confirmo la acción sobre el pedido #{order_id}",
            key=f"jap_orders_confirmo_{order_id}",
        )
        accion1, accion2 = st.columns(2)
        with accion1:
            if st.button(
                "♻️ Refill",
                key=f"jap_btn_refill_{order_id}",
                disabled=not (disponible and confirmo and order_id),
            ):
                _accion_orden(order_id, "refill")
        with accion2:
            if st.button(
                "🛑 Cancelar",
                key=f"jap_btn_cancelar_{order_id}",
                disabled=not (disponible and confirmo and order_id),
            ):
                _accion_orden(order_id, "cancelar")

    with st.expander("ℹ️ ¿Qué significan los estados?"):
        st.markdown(_ESTADOS_AYUDA)


def _accion_orden(order_id: int, accion: str):
    """Refill/cancelar una orden mostrando el resultado en español."""
    jap = _cliente()
    if jap is None:
        st.error("El cliente de JAP no está disponible.")
        return
    try:
        if accion == "refill":
            resultado = jap.refill_orden(order_id)
        else:
            resultado = jap.cancelar_orden([order_id])
    except Exception as exc:
        resultado = {"ok": False, "error": str(exc)}
    if isinstance(resultado, dict) and resultado.get("ok"):
        if accion == "refill":
            st.success(f"♻️ Refill solicitado para el pedido #{order_id}.")
        else:
            st.success(f"🛑 Pedido #{order_id} cancelado.")
    else:
        error = (
            resultado.get("error") if isinstance(resultado, dict) else ""
        ) or "No se pudo completar la acción"
        st.error(str(error))


__all__ = [
    "LINKS_REDAMLO",
    "SERVICIO_COMENTARIOS_ECONOMICO",
    "SERVICIO_COMENTARIOS_SEGURO",
    "render",
]
