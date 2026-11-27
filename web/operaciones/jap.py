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
   calcula el costo por link con presets Economico/Seguro, avisa si el saldo
   de JAP no alcanza y permite comprar el lote desde el propio cotizador
   (checkbox de confirmacion obligatorio; 0 = no comprar ese tipo).
3. "📦 Pedidos": consulta estados, refill y cancelacion con confirmacion,
   lista los lotes guardados y ofrece el reporte para el cliente (HTML/CSV o
   un Google Sheet con link) mas un reporte manual para reconstruir una
   compra anterior. La conexion de Google Sheets (`core.google_sheets`,
   opcional) se configura desde el expander "🔗 Conectar Google Sheets"; si
   no hay credenciales la pagina avisa y no llama a la API.

Los pedidos del lote se crean SECUENCIALMENTE desde el boton de compra: si uno
falla, los demas continuan. Nada se envia solo por renderizar la pagina.

Cada lote se persiste en ``core.jap_historial`` (import perezoso; si el modulo
no esta disponible la compra funciona igual y solo se pierde el historial).
Los reportes descargables NO incluyen proveedor, costos, saldo, API key, IDs
de servicio ni presets: son documentos para el cliente final.

Pruebas: ``tests/test_jap_web.py`` (AppTest + mocks, sin red real).
"""
from __future__ import annotations

import csv
import html as html_lib
import io
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
CLAVE_ESTADOS = "jap_estados_consulta"
CLAVE_LOTE_RESULTADOS = "jap_lote_resultados"
CLAVE_LOTE_AVISO = "jap_lote_aviso"
CLAVE_SHEET_URL = "jap_sheet_url_"
CLAVE_SHEET_GUARDADO = "jap_gs_guardado_email"

TTL_CATALOGO_SEG = 3600
TTL_BALANCE_SEG = 120
MAX_FILAS_CATALOGO = 300
MAX_PEDIDOS_LOTE = 100
MAX_PEDIDOS_SESION = 50
MAX_LOTES_REPORTE = 10

_ETIQUETAS_REPORTE = {
    "likes": "Me gusta",
    "retweets": "Retweets",
    "comments": "Comentarios",
    "views": "Vistas",
    "followers": "Seguidores",
    "impressions": "Impresiones",
    "mentions": "Menciones",
    "follow": "Seguidores",
}

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


# ---------------------------------------------------------------------------
# Historial de lotes y reportes para el cliente
# ---------------------------------------------------------------------------
# El modulo `core.jap_historial` es opcional. Se importa de forma perezosa y
# la pagina funciona igual si no esta disponible (solo no persiste el lote).
def _historial():
    try:
        from core import jap_historial as historial

        return historial
    except Exception:
        return None


def _a_texto(valor) -> str:
    if isinstance(valor, bytes):
        try:
            return valor.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return str(valor or "")


def _nuevo_lote_id_local() -> str:
    return datetime.now().strftime("LOTE-%Y%m%d-%H%M%S")


def _nuevo_lote_id() -> str:
    """Folio del lote: `core.jap_historial.nuevo_lote_id` o fallback local."""
    historial = _historial()
    if historial is not None:
        funcion = getattr(historial, "nuevo_lote_id", None)
        if callable(funcion):
            try:
                folio = str(funcion() or "").strip()
                if folio:
                    return folio
            except Exception:
                pass
    return _nuevo_lote_id_local()


def _nombre_reporte(folio: str, formato: str) -> str:
    """Nombre de descarga `reporte-<folio>.html`/`.csv` (sin rutas raras)."""
    extension = "csv" if str(formato).lower() == "csv" else "html"
    limpio = re.sub(r"[^A-Za-z0-9._-]+", "-", str(folio or "")).strip(".-")
    return f"reporte-{limpio or 'lote'}.{extension}"


def _etiqueta_tipo_reporte(tipo) -> str:
    clave = str(tipo or "").strip().lower()
    if clave in _ETIQUETAS_REPORTE:
        return _ETIQUETAS_REPORTE[clave]
    return clave.replace("_", " ").capitalize() or "Acción"


def _es_pedido_exitoso(pedido: dict) -> bool:
    if "ok" in pedido:
        return bool(pedido.get("ok"))
    if "exito" in pedido:
        return bool(pedido.get("exito"))
    estado = str(pedido.get("estado") or "").strip().lower()
    if estado:
        return estado in ("exito", "exitoso", "ok", "completed", "completado")
    return True


def _pedidos_para_reporte(pedidos) -> list:
    """Solo acciones exitosas (un pedido manual sin `ok` cuenta como exitoso)."""
    return [
        pedido
        for pedido in (pedidos or [])
        if isinstance(pedido, dict) and _es_pedido_exitoso(pedido)
    ]


_CSS_REPORTE = """
body { margin: 0; padding: 32px; background: #f7f9fc; color: #1f2937;
       font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif; }
.hoja { max-width: 880px; margin: 0 auto; background: #ffffff; border-radius: 14px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, .08); padding: 40px 44px; }
.marca { border-bottom: 4px solid #1DA1F2; padding-bottom: 18px; margin-bottom: 24px; }
.marca h1 { margin: 0; font-size: 28px; letter-spacing: .5px; }
.marca p { margin: 6px 0 0; color: #64748b; font-size: 15px; }
.datos { margin: 0 0 24px; padding: 0; list-style: none; font-size: 14px; }
.datos li { margin: 6px 0; }
.datos strong { color: #0f172a; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; background: #eff6ff; color: #0f172a; padding: 12px; }
td { padding: 11px 12px; border-top: 1px solid #e2e8f0; word-break: break-word; }
tr:nth-child(even) td { background: #f8fafc; }
.pie { margin-top: 28px; padding-top: 16px; border-top: 1px solid #e2e8f0;
       color: #64748b; font-size: 12px; }
.vacio { padding: 18px; color: #64748b; font-size: 14px; }
"""


def _reporte_html_local(
    pedidos,
    fecha="",
    cliente="",
    campana="",
    folio="",
    notas="",
) -> str:
    """HTML de respaldo si `core.jap_historial` no esta disponible.

    Solo usa tipo/link/cantidad de cada pedido: no incluye costos, IDs ni
    datos internos.
    """
    filas = []
    for pedido in _pedidos_para_reporte(pedidos):
        tipo = html_lib.escape(_etiqueta_tipo_reporte(pedido.get("tipo")))
        link = html_lib.escape(str(pedido.get("link") or ""))
        cantidad = html_lib.escape(str(pedido.get("cantidad") or ""))
        filas.append(
            f"<tr><td>{tipo}</td><td>{link or '—'}</td>"
            f"<td>{cantidad or '—'}</td></tr>"
        )
    cuerpo = "\n".join(filas) or (
        '<tr><td colspan="3" class="vacio">Sin acciones registradas.</td></tr>'
    )
    datos = []
    if folio:
        datos.append(f"<li><strong>Folio:</strong> {html_lib.escape(str(folio))}</li>")
    if fecha:
        datos.append(f"<li><strong>Fecha:</strong> {html_lib.escape(str(fecha))}</li>")
    if cliente:
        datos.append(
            f"<li><strong>Cliente:</strong> {html_lib.escape(str(cliente))}</li>"
        )
    if campana:
        datos.append(
            f"<li><strong>Campaña:</strong> {html_lib.escape(str(campana))}</li>"
        )
    if notas:
        datos.append(
            f"<li><strong>Notas:</strong> {html_lib.escape(str(notas))}</li>"
        )
    bloque_datos = (
        f'<ul class="datos">{"".join(datos)}</ul>' if datos else ""
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        "<title>Reporte de acciones realizadas</title>\n"
        f"<style>{_CSS_REPORTE}</style>\n"
        "</head>\n<body>\n"
        '<div class="hoja">\n'
        '<div class="marca">\n'
        "<h1>MW Group</h1>\n"
        "<p>Reporte de acciones realizadas</p>\n"
        "</div>\n"
        f"{bloque_datos}\n"
        "<table>\n<thead><tr><th>Tipo de acción</th>"
        "<th>Publicación</th><th>Cantidad</th></tr></thead>\n"
        f"<tbody>\n{cuerpo}\n</tbody>\n</table>\n"
        '<div class="pie">Reporte generado por MW Group.</div>\n'
        "</div>\n</body>\n</html>\n"
    )


def _reporte_csv_local(
    pedidos,
    fecha="",
    cliente="",
    campana="",
    folio="",
    notas="",
) -> str:
    """CSV de respaldo (solo acciones exitosas, sin datos internos)."""
    buffer = io.StringIO()
    escritor = csv.writer(buffer, lineterminator="\n")
    if cliente:
        escritor.writerow(["Cliente", cliente])
    if campana:
        escritor.writerow(["Campaña", campana])
    if folio:
        escritor.writerow(["Folio", folio])
    if fecha:
        escritor.writerow(["Fecha", fecha])
    if notas:
        escritor.writerow(["Notas", notas])
    if cliente or campana or folio or fecha or notas:
        escritor.writerow([])
    escritor.writerow(["Tipo", "Publicación", "Cantidad"])
    for pedido in _pedidos_para_reporte(pedidos):
        escritor.writerow(
            [
                _etiqueta_tipo_reporte(pedido.get("tipo")),
                str(pedido.get("link") or ""),
                pedido.get("cantidad") or "",
            ]
        )
    return "\ufeff" + buffer.getvalue()


def _contenido_reporte(
    pedidos,
    formato="html",
    *,
    fecha="",
    cliente="",
    campana="",
    folio="",
    notas="",
) -> str:
    """Genera el reporte con `core.jap_historial` (si existe) o el respaldo."""
    formato = "csv" if str(formato).lower() == "csv" else "html"
    pedidos = [p for p in (pedidos or []) if isinstance(p, dict)]
    historial = _historial()
    if historial is not None:
        nombre = (
            "construir_reporte_csv"
            if formato == "csv"
            else "construir_reporte_html"
        )
        funcion = getattr(historial, nombre, None)
        if callable(funcion):
            try:
                contenido = funcion(
                    pedidos,
                    fecha=fecha,
                    cliente=cliente,
                    campana=campana,
                    folio=folio,
                    notas=notas,
                )
                if isinstance(contenido, (str, bytes)):
                    texto = _a_texto(contenido)
                    if texto.strip():
                        return texto
            except Exception:
                pass
    if formato == "csv":
        return _reporte_csv_local(
            pedidos,
            fecha=fecha,
            cliente=cliente,
            campana=campana,
            folio=folio,
            notas=notas,
        )
    return _reporte_html_local(
        pedidos,
        fecha=fecha,
        cliente=cliente,
        campana=campana,
        folio=folio,
        notas=notas,
    )


def _reporte_lote_contenido(
    lote_id,
    formato,
    pedidos=None,
    *,
    fecha="",
    cliente="",
    campana="",
    notas="",
    usar_persistido=True,
) -> tuple:
    """(contenido, error). Prefiere `generar_reporte_lote` para lotes guardados."""
    formato = "csv" if str(formato).lower() == "csv" else "html"
    historial = _historial()
    error = ""
    folio = str(lote_id or "").strip()
    if folio and usar_persistido and historial is not None:
        funcion = getattr(historial, "generar_reporte_lote", None)
        if callable(funcion):
            try:
                respuesta = funcion(folio, formato)
                if isinstance(respuesta, dict) and respuesta.get("ok"):
                    texto = _a_texto(respuesta.get("contenido"))
                    if texto.strip():
                        return texto, ""
                if isinstance(respuesta, dict):
                    error = str(respuesta.get("error") or "")
            except Exception as exc:
                error = str(exc)
    if pedidos is None and folio and historial is not None:
        funcion = getattr(historial, "pedidos_de_lote", None)
        if callable(funcion):
            try:
                pedidos = [
                    p for p in (funcion(folio) or []) if isinstance(p, dict)
                ]
            except Exception:
                pedidos = None
    if pedidos:
        return (
            _contenido_reporte(
                pedidos,
                formato,
                fecha=fecha,
                cliente=cliente,
                campana=campana,
                folio=folio,
                notas=notas,
            ),
            error,
        )
    return "", error or "No hay datos del lote para generar el reporte."


def _cantidad_resumen(valor) -> int:
    if isinstance(valor, (list, tuple, set, dict)):
        return len(valor)
    numero = _a_int(valor)
    return numero if numero is not None and numero >= 0 else 0


def _fila_resumen_lote(lote: dict) -> dict:
    """Normaliza un resumen de `listar_lotes` a columnas de la tabla."""
    folio = (
        lote.get("lote_id")
        or lote.get("folio")
        or lote.get("id")
        or ""
    )
    pedidos = 0
    for clave in ("pedidos", "total_pedidos", "pedidos_total", "total"):
        if lote.get(clave) is not None:
            pedidos = _cantidad_resumen(lote.get(clave))
            break
    acciones = 0
    for clave in ("acciones", "total_acciones", "cantidad", "exitosos"):
        if lote.get(clave) is not None:
            acciones = _cantidad_resumen(lote.get(clave))
            break
    links = 0
    for clave in ("links", "total_links", "enlaces"):
        if lote.get(clave) is not None:
            links = _cantidad_resumen(lote.get(clave))
            break
    return {
        "Folio": str(folio),
        "Fecha": str(lote.get("fecha") or lote.get("creado") or ""),
        "Cliente": str(lote.get("cliente") or ""),
        "Campaña": str(lote.get("campana") or lote.get("campaña") or ""),
        "Pedidos": pedidos,
        "Acciones": acciones,
        "Links": links,
    }


def _plan_reporte_manual(links, cantidades, perfil="") -> tuple:
    """Pedidos falsos para reconstruir un reporte (NO crea ordenes).

    Devuelve ``(pedidos, errores)``. Cada pedido es
    ``{"tipo","link","cantidad","ok": True}``. Las cantidades en 0 se omiten.
    """
    enlaces = [str(link).strip() for link in (links or []) if str(link).strip()]
    datos = cantidades if isinstance(cantidades, dict) else {}
    cantidades_lote = {
        tipo: _a_int(datos.get(tipo)) or 0
        for tipo in ("likes", "retweets", "comments", "views", "followers")
    }
    errores = []
    if not enlaces:
        errores.append("Pega al menos un link de publicación.")
    if not any(cantidad > 0 for cantidad in cantidades_lote.values()):
        errores.append("Indica al menos una cantidad mayor a 0.")
    if errores:
        return [], errores

    pedidos = []
    for tipo in ("likes", "retweets", "comments", "views"):
        cantidad = max(cantidades_lote[tipo], 0)
        if cantidad <= 0:
            continue
        for enlace in enlaces:
            pedidos.append(
                {
                    "tipo": tipo,
                    "link": enlace,
                    "cantidad": cantidad,
                    "ok": True,
                }
            )
    if cantidades_lote["followers"] > 0:
        pedidos.append(
            {
                "tipo": "followers",
                "link": str(perfil or "").strip(),
                "cantidad": cantidades_lote["followers"],
                "ok": True,
            }
        )
    return pedidos, []


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
            "📦 Pedidos",
        ]
    )
    with tabs[0]:
        _tab_catalogo()
    with tabs[1]:
        _tab_cotizador(disponible)
    with tabs[2]:
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
    # La confirmacion se limpia DESPUES de comprar (antes de instanciar el
    # checkbox) para evitar una recompra accidental con el mismo clic.
    if st.session_state.pop("jap_lote_limpiar_confirmacion", False):
        st.session_state["jap_lote_confirmo"] = False

    st.markdown("### 🧮 Cotizador (lote de tweets)")
    st.caption(
        "Calcula cuánto costaría comprar engagement para varios tweets y "
        "desde aquí mismo puedes comprar el lote. Pon 0 en lo que no quieras "
        "comprar; los pedidos solo se envían al pulsar el botón y confirmar."
    )
    _mostrar_ultimo_lote()

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

    if personalizados:
        sid_comentarios = _id_comentarios(preset, True)
    else:
        # Sin texto propio JAP no puede publicar Custom Comments: se usa el
        # servicio Random USA (6601) y no se manda `comments`.
        sid_comentarios = SERVICIO_COMENTARIOS_ECONOMICO
    info_comentarios = _servicio_por_id(sid_comentarios)
    if not info_comentarios:
        info_comentarios = dict(presets.get(clave_preset, {}).get("comments") or {})
        if _a_int(info_comentarios.get("id")) != sid_comentarios:
            info_comentarios = {}

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

    cantidades = {
        "likes": int(likes),
        "retweets": int(retweets),
        "comments": int(comentarios),
        "views": int(vistas),
        "followers": int(seguidores),
    }
    filas, avisos = _calcular_lote(links, cantidades, resumen_preset)
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

    _seccion_comprar_lote(
        links,
        cantidades,
        resumen_preset,
        str(perfil or "").strip(),
        texto_comentarios,
        balance,
        disponible,
        avisos,
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
# Compra del lote desde el cotizador
# ---------------------------------------------------------------------------
def _plan_pedidos_lote(links, cantidades, servicios, perfil, comentarios_texto):
    """Plan de pedidos del lote (sin Streamlit ni red directa).

    Devuelve ``(pedidos, errores)``. Cada pedido es un dict con ``tipo``,
    ``servicio``, ``servicio_nombre``, ``link``, ``cantidad``, ``costo`` y
    ``comments``. Con cantidad 0 NO se agrega pedido. Los seguidores se piden
    UNA sola vez al perfil (no por link) y los comentarios personalizados se
    repiten (el mismo texto) en cada link.
    """
    pedidos = []
    errores = []
    vistos = set()

    def agregar_error(mensaje):
        texto = str(mensaje or "").strip()
        if texto and texto not in vistos:
            vistos.add(texto)
            errores.append(texto)

    enlaces = [
        str(link).strip() for link in (links or []) if str(link).strip()
    ]
    if not enlaces:
        agregar_error(
            "No hay links de tweets: pega al menos uno para comprar el lote."
        )
        return pedidos, errores

    datos = cantidades if isinstance(cantidades, dict) else {}
    cantidades_lote = {
        tipo: _a_int(datos.get(tipo)) or 0
        for tipo in ("likes", "retweets", "comments", "views", "followers")
    }
    texto = str(comentarios_texto or "")
    lineas = [linea.strip() for linea in texto.splitlines() if linea.strip()]
    personalizados = bool(lineas)

    if (
        cantidades_lote["comments"] > 0
        and personalizados
        and len(lineas) < cantidades_lote["comments"]
    ):
        agregar_error(
            f"Comentarios: pediste {cantidades_lote['comments']} por link y "
            f"solo hay {len(lineas)} comentario(s) personalizado(s); agrega "
            "más líneas o baja la cantidad."
        )

    for tipo in ("likes", "retweets", "comments", "views"):
        cantidad = cantidades_lote[tipo]
        if cantidad <= 0:
            continue
        info = (
            servicios if isinstance(servicios, dict) else {}
        ).get(tipo) or {}
        servicio_id = _a_int(info.get("id"))
        nombre = str(info.get("nombre") or "")
        comments = ""
        if tipo == "comments":
            if personalizados:
                # Con texto propio se usa el servicio del preset (Custom
                # Comments 6602); el texto es el mismo para todos los links.
                comments = texto
            else:
                # Sin texto no se puede publicar Custom Comments: Random USA.
                if servicio_id != SERVICIO_COMENTARIOS_ECONOMICO:
                    nombre = ""
                servicio_id = SERVICIO_COMENTARIOS_ECONOMICO
        if servicio_id is None:
            agregar_error(
                f"{tipo.capitalize()}: el servicio del preset no tiene id."
            )
            continue
        if tipo == "comments" and personalizados and len(lineas) < cantidad:
            # El error de lineas insuficientes ya esta registrado.
            continue
        costo, error = _cotizar(servicio_id, cantidad)
        if error:
            agregar_error(
                f"{tipo.capitalize()} (servicio {servicio_id}): {error}"
            )
            continue
        for link in enlaces:
            pedidos.append(
                {
                    "tipo": tipo,
                    "servicio": servicio_id,
                    "servicio_nombre": nombre,
                    "link": link,
                    "cantidad": cantidad,
                    "costo": costo,
                    "comments": comments,
                }
            )

    if cantidades_lote["followers"] > 0:
        info = (
            servicios if isinstance(servicios, dict) else {}
        ).get("followers") or {}
        servicio_id = _a_int(info.get("id"))
        nombre = str(info.get("nombre") or "")
        enlace_perfil = str(perfil or "").strip()
        if not enlace_perfil:
            agregar_error(
                "Seguidores: indica el link del perfil para poder comprarlos."
            )
        elif servicio_id is None:
            agregar_error("Seguidores: el servicio del preset no tiene id.")
        else:
            costo, error = _cotizar(
                servicio_id, cantidades_lote["followers"]
            )
            if error:
                agregar_error(
                    f"Seguidores (servicio {servicio_id}): {error}"
                )
            else:
                pedidos.append(
                    {
                        "tipo": "followers",
                        "servicio": servicio_id,
                        "servicio_nombre": nombre,
                        "link": enlace_perfil,
                        "cantidad": cantidades_lote["followers"],
                        "costo": costo,
                        "comments": "",
                    }
                )

    if len(pedidos) > MAX_PEDIDOS_LOTE:
        agregar_error(
            f"El lote tiene {len(pedidos)} pedidos y el máximo es "
            f"{MAX_PEDIDOS_LOTE}; reduce las cantidades o los links."
        )
    return pedidos, errores


def _seccion_comprar_lote(
    links,
    cantidades,
    servicios,
    perfil,
    comentarios_texto,
    balance,
    disponible,
    avisos,
):
    """Seccion '🧾 Comprar este lote' del cotizador (solo compra al pulsar)."""
    st.markdown("---")
    st.markdown("### 🧾 Comprar este lote")

    pedidos, errores = _plan_pedidos_lote(
        links, cantidades, servicios, perfil, comentarios_texto
    )
    bloqueos = []
    for mensaje in list(errores) + list(avisos or []):
        texto = str(mensaje or "").strip()
        if texto and texto not in bloqueos:
            bloqueos.append(texto)

    total = sum(_a_float(pedido.get("costo")) or 0.0 for pedido in pedidos)
    saldo_ok = bool(balance.get("ok"))
    saldo = _a_float(balance.get("balance")) or 0.0
    saldo_texto = f"${saldo:,.4f}" if saldo_ok else "—"
    st.markdown(
        f"**{len(pedidos)} pedido(s) · total estimado ${total:,.4f} · "
        f"saldo {saldo_texto}**"
    )

    for mensaje in bloqueos:
        st.error(mensaje)
    if not pedidos and not bloqueos:
        st.info(
            "No hay nada que comprar: pon una cantidad mayor a 0 en likes, "
            "retweets, comentarios, vistas o seguidores (0 = no comprar)."
        )
    if saldo_ok and total > saldo:
        st.error(
            f"Saldo insuficiente: el lote cuesta ${total:,.4f} y tienes "
            f"${saldo:,.4f}. Recarga saldo en el panel de JAP."
        )
    if pedidos:
        st.caption(
            "Los pedidos se crean uno por uno en el orden anterior. Si uno "
            "falla, los demás continúan."
        )

    st.markdown("**Datos para el reporte del cliente (opcional):**")
    col_cliente, col_campana = st.columns(2)
    with col_cliente:
        cliente = st.text_input(
            "Cliente (para el reporte)",
            key="jap_lote_cliente",
            placeholder="Nombre del cliente",
        )
    with col_campana:
        campana = st.text_input(
            "Campaña / tema (para el reporte)",
            key="jap_lote_campana",
            placeholder="Ej. Lanzamiento de septiembre",
        )

    confirmo = st.checkbox(
        "Confirmo que estos pedidos gastan saldo real de JAP",
        key="jap_lote_confirmo",
    )
    puede_comprar = bool(
        disponible
        and links
        and pedidos
        and not bloqueos
        and (not saldo_ok or total <= saldo)
        and confirmo
    )
    if st.button(
        f"💳 Comprar lote ({len(pedidos)} pedidos)",
        key="jap_btn_comprar_lote",
        type="primary",
        disabled=not puede_comprar,
    ):
        _comprar_lote(
            pedidos,
            cliente=str(cliente or "").strip(),
            campana=str(campana or "").strip(),
        )


def _crear_orden_pedido(jap, pedido: dict) -> dict:
    """Crea UNA orden del plan. Nunca lanza (fallback sin `comments`)."""
    servicio_id = _a_int(pedido.get("servicio"))
    comments = str(pedido.get("comments") or "")
    try:
        try:
            return jap.crear_orden(
                servicio_id,
                pedido.get("link"),
                pedido.get("cantidad"),
                comments=comments,
            )
        except TypeError:
            # Backend viejo sin el kwarg `comments`.
            return jap.crear_orden(
                servicio_id, pedido.get("link"), pedido.get("cantidad")
            )
    except Exception as exc:
        return {"ok": False, "error": f"Error al crear el pedido: {exc}"}


def _comprar_lote(pedidos, cliente="", campana=""):
    """Compra el lote SECUENCIALMENTE y guarda el resumen en session_state."""
    if st.session_state.get("jap_lote_comprando"):
        return
    if not st.session_state.get("jap_lote_confirmo"):
        # Defensa extra: jamas se compra sin la confirmacion explicita.
        return
    pedidos = [p for p in (pedidos or []) if isinstance(p, dict)]
    if not pedidos:
        return
    jap = _cliente()
    if jap is None:
        st.error("El cliente de JAP no está disponible.")
        return

    st.session_state["jap_lote_comprando"] = True
    resultados = []
    total_gastado = 0.0
    recientes = st.session_state.get(CLAVE_PEDIDOS)
    if not isinstance(recientes, list):
        recientes = []
    fecha = datetime.now().isoformat(timespec="seconds")
    lote_id = _nuevo_lote_id()
    cliente = str(cliente or "").strip()
    campana = str(campana or "").strip()
    try:
        barra = st.progress(0.0)
        estado = st.empty()
        total = len(pedidos)
        for indice, pedido in enumerate(pedidos, start=1):
            estado.caption(f"Pedido {indice}/{total}…")
            resultado = _crear_orden_pedido(jap, pedido)
            ok = isinstance(resultado, dict) and bool(resultado.get("ok"))
            costo = (
                _a_float(resultado.get("costo")) or 0.0 if ok else 0.0
            )
            error = ""
            if not ok:
                error = (
                    resultado.get("error")
                    if isinstance(resultado, dict)
                    else "respuesta inesperada"
                ) or "No se pudo crear el pedido"
            order = _a_int(resultado.get("order")) if ok else None
            registro = {
                "order": order,
                "tipo": pedido.get("tipo"),
                "servicio": _a_int(pedido.get("servicio")),
                "servicio_nombre": str(
                    pedido.get("servicio_nombre") or ""
                ),
                "link": pedido.get("link"),
                "cantidad": _a_int(pedido.get("cantidad")),
                "costo": costo,
                "comments": str(pedido.get("comments") or ""),
                "ok": ok,
                "error": error,
            }
            resultados.append(registro)
            if ok:
                total_gastado += costo
                recientes.insert(
                    0,
                    {
                        "order": order,
                        "servicio": registro["servicio"],
                        "servicio_nombre": registro["servicio_nombre"],
                        "link": registro["link"],
                        "cantidad": registro["cantidad"],
                        "costo": costo,
                        "comentarios": registro["comments"],
                        "fecha": fecha,
                    },
                )
            barra.progress(indice / total)
        estado.caption(f"Pedidos procesados: {total}/{total}")
    finally:
        st.session_state["jap_lote_comprando"] = False

    exitosos = sum(1 for r in resultados if r.get("ok"))

    # Persistir el lote para el reporte del cliente. Si el backend de
    # historial no esta disponible la compra NO se pierde: solo se avisa.
    historial_ok = False
    historial_error = ""
    historial = _historial()
    guardar = getattr(historial, "guardar_pedidos", None) if historial else None
    if callable(guardar):
        try:
            respuesta = guardar(
                resultados,
                lote_id=lote_id,
                cliente=cliente,
                campana=campana,
            )
            if isinstance(respuesta, dict) and respuesta.get("ok"):
                historial_ok = True
                lote_id = str(respuesta.get("lote_id") or lote_id).strip() or lote_id
            elif isinstance(respuesta, dict):
                historial_error = str(
                    respuesta.get("error") or "No se pudo guardar el lote."
                )
            else:
                historial_error = "El historial devolvió una respuesta inesperada."
        except Exception as exc:
            historial_error = f"No se pudo guardar el lote: {exc}"
    else:
        historial_error = (
            "El historial de lotes no está disponible en esta versión: la "
            "compra se realizó, pero el reporte no quedó guardado."
        )

    st.session_state[CLAVE_PEDIDOS] = recientes[:MAX_PEDIDOS_SESION]
    st.session_state[CLAVE_LOTE_RESULTADOS] = {
        "fecha": fecha,
        "total_gastado": total_gastado,
        "exitosos": exitosos,
        "fallidos": len(resultados) - exitosos,
        "pedidos": resultados,
        "lote_id": lote_id,
        "cliente": cliente,
        "campana": campana,
        "historial_ok": historial_ok,
        "historial_error": historial_error,
    }
    if historial_error:
        st.session_state[CLAVE_LOTE_AVISO] = historial_error
    else:
        st.session_state.pop(CLAVE_LOTE_AVISO, None)
    _cache_limpiar(CLAVE_BALANCE)
    st.session_state["jap_lote_limpiar_confirmacion"] = True
    st.rerun()


def _mostrar_ultimo_lote():
    """Resumen persistente del ultimo lote comprado (con reporte descargable)."""
    resultados = st.session_state.get(CLAVE_LOTE_RESULTADOS)
    if not isinstance(resultados, dict):
        return
    aviso = st.session_state.pop(CLAVE_LOTE_AVISO, "")
    if aviso:
        st.warning(str(aviso))
    pedidos = [
        p for p in (resultados.get("pedidos") or []) if isinstance(p, dict)
    ]
    exitosos = _a_int(resultados.get("exitosos")) or 0
    fallidos = _a_int(resultados.get("fallidos")) or 0
    total = _a_float(resultados.get("total_gastado")) or 0.0
    lote_id = str(resultados.get("lote_id") or "").strip()
    cliente = str(resultados.get("cliente") or "").strip()
    campana = str(resultados.get("campana") or "").strip()
    fecha = str(resultados.get("fecha") or "").strip()
    with st.expander("Último pedido del lote", expanded=False):
        st.markdown(
            f"**{exitosos} exitoso(s) · {fallidos} fallido(s) · "
            f"${total:,.4f} gastados**"
        )
        st.caption(f"Fecha: {resultados.get('fecha') or '—'}")
        if lote_id:
            st.caption(f"Folio del reporte: **{lote_id}**")
        if pedidos:
            st.dataframe(
                [
                    {
                        "Pedido": p.get("order"),
                        "Tipo": p.get("tipo"),
                        "Servicio": p.get("servicio_nombre")
                        or p.get("servicio"),
                        "Link": p.get("link"),
                        "Cantidad": p.get("cantidad"),
                        "Costo": _a_float(p.get("costo")) or 0.0,
                        "Estado": "✅" if p.get("ok") else "❌",
                        "Error": str(p.get("error") or ""),
                    }
                    for p in pedidos
                ],
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Costo": st.column_config.NumberColumn(format="$%.4f"),
                },
            )

        pedidos_reporte = _pedidos_para_reporte(pedidos)
        if pedidos_reporte:
            html, error_html = _reporte_lote_contenido(
                lote_id,
                "html",
                pedidos_reporte,
                fecha=fecha,
                cliente=cliente,
                campana=campana,
                usar_persistido=bool(resultados.get("historial_ok")),
            )
            csv_texto, error_csv = _reporte_lote_contenido(
                lote_id,
                "csv",
                pedidos_reporte,
                fecha=fecha,
                cliente=cliente,
                campana=campana,
                usar_persistido=bool(resultados.get("historial_ok")),
            )
            if html:
                st.download_button(
                    "📄 Reporte para el cliente (HTML)",
                    data=html,
                    file_name=_nombre_reporte(lote_id or "lote", "html"),
                    mime="text/html",
                    key="jap_dl_lote_html",
                )
            if csv_texto:
                st.download_button(
                    "📊 Reporte (CSV)",
                    data=csv_texto,
                    file_name=_nombre_reporte(lote_id or "lote", "csv"),
                    mime="text/csv",
                    key="jap_dl_lote_csv",
                )
            if error_html or error_csv:
                st.caption(f"⚠️ {error_html or error_csv}")
            st.caption(
                "El reporte para el cliente no incluye proveedor, costos ni "
                "IDs de servicio."
            )
            st.markdown("**Reporte para el cliente (sin proveedor)**")
            _crear_sheet_cliente(
                pedidos_reporte,
                cliente=cliente,
                campana=campana,
                folio=lote_id,
                fecha=fecha,
                key_prefix="jap_lote_actual",
            )
        else:
            st.caption(
                "Este lote no tiene pedidos exitosos, así que no hay reporte "
                "para el cliente."
            )

        if st.button(
            "🧹 Limpiar resumen",
            key="jap_btn_limpiar_lote_resultados",
        ):
            st.session_state.pop(CLAVE_LOTE_RESULTADOS, None)
            st.rerun()


# ---------------------------------------------------------------------------
# Pestaña 3: Pedidos
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


def _cargar_links_manual():
    """on_click: rellena el textarea del reporte manual con los 18 tweets."""
    try:
        st.session_state["jap_manual_links"] = "\n".join(LINKS_REDAMLO)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Google Sheets: reporte con link para el cliente
# ---------------------------------------------------------------------------
# `core.google_sheets` es opcional: se importa de forma perezosa y la pagina
# funciona igual si no esta disponible (solo se muestra el aviso de conectarlo).
# Las credenciales NUNCA se muestran, registran ni se quedan en
# `st.session_state` despues de guardarlas.
def _sheets():
    """Import perezoso de `core.google_sheets`. None si no esta disponible."""
    try:
        from core import google_sheets

        return google_sheets
    except Exception:
        return None


def _sheets_estado() -> dict:
    """Estado normalizado de la conexion con Google Sheets (nunca lanza)."""
    vacio = {"disponible": False, "motivo": "", "fuente": "", "email": ""}
    modulo = _sheets()
    if modulo is None:
        return {
            **vacio,
            "motivo": "El módulo core.google_sheets no está disponible en esta "
            "versión.",
        }
    try:
        estado = modulo.estado()
    except Exception as exc:
        return {**vacio, "motivo": f"No se pudo consultar el estado: {exc}"}
    if not isinstance(estado, dict):
        return {**vacio, "motivo": "Google Sheets devolvió un estado inesperado."}
    return {
        "disponible": bool(estado.get("disponible")),
        "motivo": str(estado.get("motivo") or ""),
        "fuente": str(estado.get("fuente") or ""),
        "email": str(estado.get("email") or ""),
    }


def _sheets_disponible() -> bool:
    """True solo si el backend confirma que hay credenciales conectadas."""
    modulo = _sheets()
    if modulo is None:
        return False
    funcion = getattr(modulo, "disponible", None)
    if callable(funcion):
        try:
            return bool(funcion())
        except Exception:
            return False
    return bool(_sheets_estado().get("disponible"))


def _seccion_google_sheets():
    """Expander para conectar la cuenta de servicio de Google Sheets."""
    estado = _sheets_estado()
    conectado = bool(estado.get("disponible"))
    titulo = "🔗 Conectar Google Sheets" + (" ✅" if conectado else "")
    with st.expander(titulo, expanded=not conectado):
        guardado_email = st.session_state.pop(CLAVE_SHEET_GUARDADO, None)
        if guardado_email is not None:
            texto_guardado = "✅ Credenciales guardadas. Google Sheets quedó conectado"
            if str(guardado_email or "").strip():
                texto_guardado += f" · {str(guardado_email).strip()}"
            st.success(texto_guardado + ".")
        if conectado:
            correo = str(estado.get("email") or "").strip()
            texto = "✅ Google Sheets conectado"
            if correo:
                texto += f" · {correo}"
            st.success(texto)
            if estado.get("fuente"):
                st.caption(f"Credenciales: {estado.get('fuente')}")
        else:
            motivo = str(estado.get("motivo") or "").strip()
            st.warning(
                "Google Sheets no está conectado todavía."
                + (f" {motivo}" if motivo else "")
            )
        st.markdown(
            "**Cómo conectarlo (una sola vez):**\n"
            "1. Crea una **cuenta de servicio** en Google Cloud.\n"
            "2. Habilita la **Google Sheets API** y la **Google Drive API**.\n"
            "3. Descarga el **JSON** de la cuenta de servicio.\n"
            "4. En Railway define `GOOGLE_SERVICE_ACCOUNT_B64` con el "
            "**base64** del JSON, o pega/suelta el JSON aquí para guardarlo "
            "localmente en `data/google_service_account.json`."
        )
        # El JSON se limpia del session_state en el rerun posterior a guardar
        # (antes de instanciar los widgets). NUNCA se muestra ni se registra.
        if st.session_state.pop("jap_gs_limpiar_json", False):
            version_anterior = int(
                st.session_state.get("jap_gs_file_version") or 0
            )
            try:
                st.session_state["jap_gs_json_input"] = ""
            except Exception:
                pass
            try:
                # El uploader no permite asignar su valor, pero si borrarlo
                # antes de instanciar el widget nuevo: el JSON no queda en
                # session_state despues de guardarlo.
                st.session_state.pop(
                    f"jap_gs_json_file_{version_anterior}", None
                )
            except Exception:
                pass
            try:
                st.session_state["jap_gs_file_version"] = version_anterior + 1
            except Exception:
                pass
        version = int(st.session_state.get("jap_gs_file_version") or 0)
        archivo = st.file_uploader(
            "Suelta aquí el JSON de la cuenta de servicio",
            type=["json"],
            key=f"jap_gs_json_file_{version}",
        )
        pegado = st.text_input(
            "…o pega el JSON completo",
            type="password",
            key="jap_gs_json_input",
            placeholder='{"type": "service_account", "project_id": "..."}',
        )
        contenido = ""
        if archivo is not None:
            try:
                contenido = archivo.getvalue().decode("utf-8", errors="replace")
            except Exception:
                contenido = ""
        elif str(pegado or "").strip():
            contenido = str(pegado)
        col_guardar, col_probar = st.columns(2)
        with col_guardar:
            guardar = st.button(
                "Guardar credenciales", key="jap_gs_btn_guardar"
            )
        with col_probar:
            probar = st.button("Probar conexión", key="jap_gs_btn_probar")
        if guardar:
            modulo = _sheets()
            if modulo is None:
                st.error(
                    "El módulo de Google Sheets no está disponible en esta "
                    "versión."
                )
            elif not contenido.strip():
                st.warning("Pega o arrastra el JSON de la cuenta de servicio.")
            else:
                try:
                    resultado = modulo.guardar_credenciales_json(contenido)
                except Exception as exc:
                    resultado = {"ok": False, "error": str(exc)}
                if isinstance(resultado, dict) and resultado.get("ok"):
                    st.session_state["jap_gs_limpiar_json"] = True
                    st.session_state[CLAVE_SHEET_GUARDADO] = str(
                        resultado.get("email") or ""
                    )
                    st.rerun()
                else:
                    error = (
                        resultado.get("error")
                        if isinstance(resultado, dict)
                        else ""
                    ) or "No se pudieron guardar las credenciales."
                    st.error(str(error))
        if probar:
            prueba = _sheets_estado()
            if prueba.get("disponible"):
                correo = str(prueba.get("email") or "").strip()
                texto = "✅ Conexión con Google Sheets OK"
                if correo:
                    texto += f" · {correo}"
                st.success(texto)
            else:
                motivo = str(prueba.get("motivo") or "").strip()
                st.warning(
                    "Sin conexión con Google Sheets."
                    + (f" {motivo}" if motivo else "")
                )


def _clave_sheet(key_prefix: str) -> str:
    """Clave estable de session_state/botones para un reporte concreto."""
    limpio = re.sub(
        r"[^A-Za-z0-9_-]+", "-", str(key_prefix or "reporte")
    ).strip("-")
    return f"{CLAVE_SHEET_URL}{limpio or 'reporte'}"


def _pedidos_lote(folio) -> list:
    """Pedidos persistidos de un lote (vacio si el historial no responde)."""
    historial = _historial()
    funcion = getattr(historial, "pedidos_de_lote", None) if historial else None
    if not callable(funcion):
        return []
    try:
        return [p for p in (funcion(folio) or []) if isinstance(p, dict)]
    except Exception:
        return []


def _crear_sheet_cliente(
    pedidos,
    cliente="",
    campana="",
    folio="",
    fecha="",
    key_prefix="jap_sheet",
):
    """Boton para crear el Google Sheet del cliente y mostrar el link.

    Solo usa los pedidos ya existentes: NO crea ordenes en JAP ni llama al
    proveedor. Si Google Sheets no esta conectado muestra el aviso y no
    llama a la API.
    """
    pedidos_ok = _pedidos_para_reporte(
        [p for p in (pedidos or []) if isinstance(p, dict)]
    )
    if not pedidos_ok:
        st.caption("No hay acciones exitosas para incluir en el Google Sheet.")
        return
    if not _sheets_disponible():
        st.caption(
            "Conecta Google Sheets en la pestaña Pedidos para crear el link."
        )
        return

    clave_url = _clave_sheet(key_prefix)
    clave_boton = f"{clave_url}_btn"
    st.caption("Reporte para el cliente (sin proveedor).")
    correo = st.text_input(
        "Correo para compartir (opcional)",
        key=f"{clave_url}_email",
        placeholder="jefe@empresa.com",
        help="Si lo indicas, el Sheet también se comparte con ese correo.",
    )
    publico = st.checkbox(
        "Cualquiera con el enlace puede ver",
        value=True,
        key=f"{clave_url}_publico",
    )
    sin_permiso = not publico and not str(correo or "").strip()
    if sin_permiso:
        st.error(
            "Indica un correo o deja marcado «Cualquiera con el enlace puede "
            "ver» para poder compartir el Sheet."
        )
    if st.button(
        "📗 Crear Google Sheet y copiar link",
        key=clave_boton,
        disabled=sin_permiso,
    ):
        modulo = _sheets()
        respuesta = {"ok": False, "error": "Google Sheets no está disponible."}
        if modulo is not None:
            try:
                with st.spinner("Creando el Google Sheet…"):
                    respuesta = modulo.crear_sheet_reporte(
                        pedidos_ok,
                        cliente=str(cliente or "").strip(),
                        campana=str(campana or "").strip(),
                        fecha=str(fecha or "").strip(),
                        folio=str(folio or "").strip(),
                        compartir_publico=bool(publico),
                        email=str(correo or "").strip(),
                    )
            except Exception as exc:
                respuesta = {"ok": False, "error": str(exc)}
        url = ""
        if isinstance(respuesta, dict) and respuesta.get("ok"):
            url = str(respuesta.get("url") or "").strip()
        if url:
            st.session_state[clave_url] = url
        else:
            error = (
                respuesta.get("error") if isinstance(respuesta, dict) else ""
            ) or "No se pudo crear el Google Sheet."
            st.error(str(error))
    url = str(st.session_state.get(clave_url) or "").strip()
    if url:
        st.success("Google Sheet creado")
        try:
            st.link_button("🔗 Abrir Google Sheet", url)
        except Exception:
            pass
        st.code(url)
        st.caption("Envía este link a tu jefe; ya tiene permisos de lectura.")


def _seccion_reportes_cliente():
    """Lotes persistidos y descarga del reporte para el cliente."""
    st.markdown("### 📄 Reportes para el cliente")
    st.caption(
        "Descarga el reporte de un lote guardado o crea un Google Sheet con "
        "link. El reporte (archivo o Sheet) no incluye proveedor, costos ni "
        "IDs de servicio: está pensado para el cliente final."
    )
    _seccion_google_sheets()
    historial = _historial()
    funcion = getattr(historial, "listar_lotes", None) if historial else None
    if not callable(funcion):
        st.info(
            "El historial de lotes no está disponible en esta versión. "
            "Puedes reconstruir una compra anterior con el reporte manual "
            "de abajo."
        )
        return
    try:
        lotes = funcion(limite=MAX_LOTES_REPORTE)
    except TypeError:
        try:
            lotes = funcion(MAX_LOTES_REPORTE)
        except Exception:
            lotes = []
    except Exception:
        lotes = []
    lotes = [lote for lote in (lotes or []) if isinstance(lote, dict)]
    if not lotes:
        st.info(
            "Todavía no hay lotes guardados. Compra un lote en «🧮 Cotizador» "
            "o reconstruye una compra anterior con el reporte manual de abajo."
        )
        return

    filas = [_fila_resumen_lote(lote) for lote in lotes]
    st.dataframe(filas, hide_index=True, use_container_width=True)

    opciones = []
    mapa = {}
    mapa_filas = {}
    for indice, lote in enumerate(lotes):
        fila = _fila_resumen_lote(lote)
        folio = str(fila.get("Folio") or "").strip()
        if not folio:
            continue
        etiqueta = f"{folio} · {fila.get('Fecha') or 'sin fecha'}"
        cliente = str(fila.get("Cliente") or "").strip()
        if cliente:
            etiqueta += f" · {cliente}"
        if etiqueta in mapa:
            etiqueta = f"{etiqueta} ({indice + 1})"
        mapa[etiqueta] = folio
        mapa_filas[etiqueta] = fila
        opciones.append(etiqueta)
    if not opciones:
        st.warning("Los lotes guardados no tienen folio: no se puede descargar.")
        return

    seleccion = st.selectbox(
        "Lote para el reporte",
        opciones,
        key="jap_reporte_lote_sel",
    )
    folio = mapa.get(seleccion) or ""
    html, error_html = _reporte_lote_contenido(folio, "html")
    csv_texto, error_csv = _reporte_lote_contenido(folio, "csv")
    if html:
        st.download_button(
            "📄 Reporte para el cliente (HTML)",
            data=html,
            file_name=_nombre_reporte(folio, "html"),
            mime="text/html",
            key="jap_dl_lote_guardado_html",
        )
    if csv_texto:
        st.download_button(
            "📊 Reporte (CSV)",
            data=csv_texto,
            file_name=_nombre_reporte(folio, "csv"),
            mime="text/csv",
            key="jap_dl_lote_guardado_csv",
        )
    if not html and not csv_texto:
        st.warning(
            error_html
            or error_csv
            or "No se pudo generar el reporte de este lote."
        )
    elif error_html or error_csv:
        st.caption(f"⚠️ {error_html or error_csv}")
    st.caption(
        "El reporte para el cliente no incluye proveedor, costos ni IDs de "
        "servicio."
    )
    fila_lote = mapa_filas.get(seleccion) or {}
    pedidos_lote = _pedidos_lote(folio)
    if pedidos_lote:
        st.markdown("**Reporte para el cliente (sin proveedor)**")
        _crear_sheet_cliente(
            pedidos_lote,
            cliente=str(fila_lote.get("Cliente") or ""),
            campana=str(fila_lote.get("Campaña") or ""),
            folio=folio,
            fecha=str(fila_lote.get("Fecha") or ""),
            key_prefix=f"jap_lote_{folio}",
        )


def _seccion_reporte_manual():
    """Expander para reconstruir el reporte de una compra anterior."""
    with st.expander(
        "🧾 Reporte manual (para una compra anterior)", expanded=False
    ):
        st.caption(
            "Reconstruye el reporte de una compra ya realizada. No crea "
            "pedidos, no gasta saldo y no requiere confirmación."
        )
        fecha = st.date_input(
            "Fecha del reporte",
            key="jap_manual_fecha",
        )
        col_cliente, col_campana = st.columns(2)
        with col_cliente:
            cliente = st.text_input(
                "Cliente",
                key="jap_manual_cliente",
                placeholder="Nombre del cliente",
            )
        with col_campana:
            campana = st.text_input(
                "Campaña / tema",
                key="jap_manual_campana",
                placeholder="Ej. Lanzamiento de septiembre",
            )

        st.button(
            "📥 Cargar los 18 tweets de @RedAMLOmx",
            key="jap_manual_btn_18",
            on_click=_cargar_links_manual,
        )
        texto_links = st.text_area(
            "Links (uno por línea)",
            key="jap_manual_links",
            height=150,
            placeholder="https://x.com/usuario/status/123...",
        )
        links = _normalizar_links(texto_links)
        if links:
            st.caption(f"✅ {len(links)} link(s) válido(s).")
        else:
            st.info(
                "Pega los links de las publicaciones o carga los 18 de "
                "@RedAMLOmx."
            )

        st.markdown("**Cantidades por publicación** (0 = no incluir):")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            likes = st.number_input(
                "👍 Likes",
                min_value=0,
                value=50,
                step=10,
                key="jap_manual_likes",
            )
        with c2:
            retweets = st.number_input(
                "🔁 Retweets",
                min_value=0,
                value=10,
                step=10,
                key="jap_manual_rts",
            )
        with c3:
            comentarios = st.number_input(
                "💬 Comentarios",
                min_value=0,
                value=5,
                step=5,
                key="jap_manual_comments",
            )
        with c4:
            vistas = st.number_input(
                "👁️ Vistas",
                min_value=0,
                value=500,
                step=100,
                key="jap_manual_views",
            )
        col_seguidores, col_perfil = st.columns([1, 3])
        with col_seguidores:
            seguidores = st.number_input(
                "👥 Seguidores totales",
                min_value=0,
                value=0,
                step=10,
                key="jap_manual_followers",
                help="0 = no incluir seguidores en el reporte.",
            )
        with col_perfil:
            perfil = st.text_input(
                "Link del perfil (opcional, para seguidores)",
                key="jap_manual_perfil",
                placeholder="https://x.com/usuario",
            )

        cantidades = {
            "likes": int(likes),
            "retweets": int(retweets),
            "comments": int(comentarios),
            "views": int(vistas),
            "followers": int(seguidores),
        }
        pedidos, errores = _plan_reporte_manual(links, cantidades, perfil)
        if errores:
            for error in errores:
                st.warning(error)
            return
        if not pedidos:
            st.info("No hay datos suficientes para generar el reporte.")
            return

        fecha_texto = (
            fecha.isoformat()
            if hasattr(fecha, "isoformat")
            else str(fecha or "")
        )
        folio = f"MANUAL-{fecha_texto}" if fecha_texto else "MANUAL"
        html = _contenido_reporte(
            pedidos,
            "html",
            fecha=fecha_texto,
            cliente=str(cliente or "").strip(),
            campana=str(campana or "").strip(),
            folio=folio,
        )
        csv_texto = _contenido_reporte(
            pedidos,
            "csv",
            fecha=fecha_texto,
            cliente=str(cliente or "").strip(),
            campana=str(campana or "").strip(),
            folio=folio,
        )
        st.download_button(
            "📄 Reporte para el cliente (HTML)",
            data=html,
            file_name=_nombre_reporte(folio, "html"),
            mime="text/html",
            key="jap_dl_manual_html",
        )
        st.download_button(
            "📊 Reporte (CSV)",
            data=csv_texto,
            file_name=_nombre_reporte(folio, "csv"),
            mime="text/csv",
            key="jap_dl_manual_csv",
        )
        st.caption(
            "El reporte para el cliente no incluye proveedor, costos ni IDs "
            "de servicio."
        )
        st.markdown("**Reporte para el cliente (sin proveedor)**")
        _crear_sheet_cliente(
            pedidos,
            cliente=str(cliente or "").strip(),
            campana=str(campana or "").strip(),
            folio=folio,
            fecha=fecha_texto,
            key_prefix="jap_manual",
        )


def _tab_pedidos(disponible: bool):
    st.markdown("### 📦 Pedidos")
    st.caption(
        "Consulta el estado de tus pedidos, pide refill o cancela. Más abajo "
        "puedes descargar el reporte para el cliente."
    )

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
            "Todavía no hay pedidos en esta sesión. Compra un lote en "
            "«🧮 Cotizador» para verlo aquí y descargar su reporte."
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

    _seccion_reportes_cliente()
    _seccion_reporte_manual()

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
