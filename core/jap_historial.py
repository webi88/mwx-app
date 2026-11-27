# -*- coding: utf-8 -*-
"""Historial de acciones compradas + reportes para el CLIENTE.

Este modulo persiste en la tabla `jap_pedido` (ver `core.models.JAPPedido`) los
resultados de un lote de engagement y construye reportes HTML/CSV **para el
cliente final**: sin costos, sin ids de servicio, sin saldo, sin API key, sin
el nombre del proveedor y sin estados internos del panel. El folio del lote es
``LOTE-YYYYmmdd-HHMMSS-xxxx`` (nunca contiene la palabra del proveedor).

Contrato publico (defensivo: NINGUNA funcion lanza)
----------------------------------------------------
- ``nuevo_lote_id() -> str``
- ``guardar_pedidos(pedidos, lote_id="", cliente="", campana="", notas="") -> dict``
- ``pedidos_de_lote(lote_id) -> list``
- ``listar_lotes(limite=10) -> list``
- ``ultimo_lote() -> dict``
- ``resumen_lote(pedidos) -> dict``
- ``construir_reporte_html(pedidos, fecha="", cliente="", campana="", folio="", notas="") -> str``
- ``construir_reporte_csv(pedidos, fecha="", cliente="", campana="", folio="", notas="") -> str``
- ``generar_reporte_lote(lote_id, formato="html") -> dict``

Notas de diseno
---------------
- De cada pedido solo se guardan columnas del modelo. Los campos del proveedor
  (servicio, nombre del servicio, texto de comentarios, etc.) se descartan a
  proposito.
- ``exito`` es el unico criterio para los totales del reporte; los fallos se
  conservan en la BD para auditoria (`pedidos_totales` los incluye, pero no
  suman acciones).
- Un pedido de seguidores NO va por enlace: se agrega una sola vez en el
  reporte ("Seguidores nuevos: N").
- La tabla se crea con ``Base.metadata.create_all`` dentro de ``init_db``; si
  al guardar falta la tabla, se llama a ``init_db()`` y se reintenta UNA vez.
"""
from __future__ import annotations

import csv
import html
import io
import re
import secrets
import unicodedata
from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import func, select

from core.database import get_db_session
from core.models import JAPPedido

__all__ = [
    "nuevo_lote_id",
    "guardar_pedidos",
    "pedidos_de_lote",
    "listar_lotes",
    "ultimo_lote",
    "resumen_lote",
    "construir_reporte_html",
    "construir_reporte_csv",
    "generar_reporte_lote",
]

TIPOS = ("likes", "retweets", "comments", "views", "followers")

ETIQUETAS_TIPO = {
    "likes": "Likes",
    "retweets": "Retweets",
    "comments": "Comentarios",
    "views": "Vistas",
    "followers": "Seguidores",
}

# Tipos que se muestran por publicacion (los seguidores van aparte).
TIPOS_POR_ENLACE = ("likes", "retweets", "comments", "views")

_ALIAS_TIPO = {
    "like": "likes",
    "likes": "likes",
    "me gusta": "likes",
    "me_gusta": "likes",
    "retweet": "retweets",
    "retweets": "retweets",
    "rt": "retweets",
    "repost": "retweets",
    "reposts": "retweets",
    "retuiteo": "retweets",
    "retuiteos": "retweets",
    "comment": "comments",
    "comments": "comments",
    "comentario": "comments",
    "comentarios": "comments",
    "view": "views",
    "views": "views",
    "vista": "views",
    "vistas": "views",
    "visualizacion": "views",
    "visualizaciones": "views",
    "reproduccion": "views",
    "reproducciones": "views",
    "follower": "followers",
    "followers": "followers",
    "seguidor": "followers",
    "seguidores": "followers",
}

_FECHA_ISO = "%Y-%m-%dT%H:%M:%S"


# --------------------------------------------------------------------------- #
# Utilidades defensivas
# --------------------------------------------------------------------------- #
def _sin_acentos(texto: str) -> str:
    """Quita acentos/diacriticos sin depender de dependencias externas."""
    normalizado = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in normalizado if not unicodedata.combining(c))


def _texto(valor: Any, limite: int = 500) -> str:
    """Convierte a str limpio y recorta (nunca lanza)."""
    if valor is None:
        return ""
    try:
        texto = str(valor).strip()
    except Exception:
        return ""
    if limite and len(texto) > limite:
        return texto[:limite]
    return texto


def _entero(valor: Any) -> int:
    """Entero no negativo (acepta '50', 50.0, None, basura)."""
    if isinstance(valor, bool):
        return int(valor)
    try:
        if valor is None or valor == "":
            return 0
        numero = int(float(str(valor).strip()))
    except Exception:
        return 0
    return max(0, numero)


def _flotante(valor: Any) -> float:
    """Float no negativo (acepta '0.05', None, basura)."""
    try:
        if valor is None or valor == "":
            return 0.0
        numero = float(str(valor).strip())
    except Exception:
        return 0.0
    return max(0.0, numero)


def _booleano(valor: Any) -> bool:
    """Normaliza banderas booleanas de formularios/JSON."""
    if isinstance(valor, bool):
        return valor
    if valor is None:
        return False
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = _sin_acentos(_texto(valor, 40)).lower()
    return texto in ("1", "true", "yes", "y", "si", "ok", "exito", "correcto")


def _es_exito(pedido: Dict[str, Any]) -> bool:
    """Lee ``exito`` (BD) o ``ok`` (resultado del panel); default True."""
    if not isinstance(pedido, dict):
        return False
    if "exito" in pedido:
        return _booleano(pedido.get("exito"))
    if "ok" in pedido:
        return _booleano(pedido.get("ok"))
    return True


def _normalizar_tipo(valor: Any) -> str:
    """Mapea alias ES/EN a los 5 tipos canonicos; desconocidos se conservan."""
    texto = _sin_acentos(_texto(valor, 60)).lower().replace("-", " ")
    texto = re.sub(r"\s+", " ", texto).strip()
    if texto in _ALIAS_TIPO:
        return _ALIAS_TIPO[texto]
    return texto.replace(" ", "_")[:30]


def _mensaje_error(error: Any, limite: int = 300) -> str:
    """Error corto y sin credenciales embebidas (nunca lanza)."""
    texto = _texto(error, 1000)
    # Oculta la contrasena si el driver incluye la URL de conexion.
    texto = re.sub(r"(://[^:/@\s]+):([^@\s]+)@", r"\1:***@", texto)
    return texto[:limite]


def _es_error_tabla_faltante(error: Any) -> bool:
    """True si el error indica que la tabla `jap_pedido` no existe todavia."""
    texto = _sin_acentos(_texto(error, 1000)).lower()
    senales = (
        "no such table",
        "does not exist",
        "undefinedtable",
        "undefined table",
    )
    return any(senal in texto for senal in senales)


def _fecha_iso(valor: Any) -> str:
    """Serializa un datetime a ISO corto; '' si no se puede."""
    if isinstance(valor, datetime):
        try:
            return valor.isoformat(timespec="seconds")
        except Exception:
            return ""
    return _texto(valor, 40)


def _fecha_legible(valor: Any) -> str:
    """Fecha humana dd/mm/aaaa a partir de ISO/datetime; hoy si falta."""
    if isinstance(valor, datetime):
        return valor.strftime("%d/%m/%Y")
    texto = _texto(valor, 60)
    if not texto:
        return datetime.now().strftime("%d/%m/%Y")
    try:
        return datetime.fromisoformat(texto).strftime("%d/%m/%Y")
    except Exception:
        pass
    try:
        return datetime.strptime(texto[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return texto[:40]


# --------------------------------------------------------------------------- #
# Folio y persistencia
# --------------------------------------------------------------------------- #
def nuevo_lote_id() -> str:
    """Folio opaco para el cliente: ``LOTE-YYYYmmdd-HHMMSS-xxxx``.

    No usa ninguna palabra del proveedor. El sufijo aleatorio evita colisiones
    cuando se guardan dos lotes en el mismo segundo.
    """
    try:
        ahora = datetime.now()
        sufijo = secrets.token_hex(2)
        return f"LOTE-{ahora.strftime('%Y%m%d-%H%M%S')}-{sufijo}"
    except Exception:
        # Respaldo ultra-defensivo (no deberia ocurrir).
        return f"LOTE-{datetime.now().strftime('%Y%m%d-%H%M%S')}-0000"


def _normalizar_pedido(
    pedido: Any,
    lote_id: str,
    cliente: str,
    campana: str,
    notas: str,
) -> Dict[str, Any]:
    """Convierte un dict de entrada/BD a columnas de `JAPPedido`."""
    datos = pedido if isinstance(pedido, dict) else {}
    exito = _es_exito(datos)
    estado = _texto(
        datos.get("estado")
        or datos.get("error")
        or ("exito" if exito else "fallido"),
        30,
    )
    return {
        "lote_id": _texto(lote_id, 40),
        "order_id": _entero(datos.get("order_id", datos.get("order"))),
        "tipo": _normalizar_tipo(datos.get("tipo")),
        "link": _texto(datos.get("link"), 4000),
        "cantidad": _entero(datos.get("cantidad")),
        "costo": _flotante(datos.get("costo")),
        "exito": exito,
        "estado": estado,
        "cliente": _texto(cliente, 120),
        "campana": _texto(campana, 160),
        "notas": _texto(datos.get("notas") or notas, 4000),
    }


def guardar_pedidos(
    pedidos: Any,
    lote_id: str = "",
    cliente: str = "",
    campana: str = "",
    notas: str = "",
) -> dict:
    """Persiste TODOS los resultados (exitos y fallos) de un lote.

    ``pedidos`` es una lista de dicts con ``order``/``order_id``, ``tipo``,
    ``link``, ``cantidad``, ``costo``, ``ok``/``exito`` y ``error``/``estado``.
    Devuelve ``{"ok", "lote_id", "guardados", "error"}``. Si falta la tabla
    llama ``init_db()`` y reintenta UNA vez. NUNCA lanza.
    """
    resultado = {
        "ok": False,
        "lote_id": _texto(lote_id, 40),
        "guardados": 0,
        "error": "",
    }
    try:
        if not isinstance(pedidos, (list, tuple)):
            resultado["error"] = "pedidos debe ser una lista"
            return resultado
        filas = [
            _normalizar_pedido(pedido, lote_id, cliente, campana, notas)
            for pedido in pedidos
            if isinstance(pedido, dict)
        ]
        if not filas:
            resultado["error"] = "sin pedidos para guardar"
            return resultado
        if not resultado["lote_id"]:
            resultado["lote_id"] = nuevo_lote_id()
        lote = resultado["lote_id"]
        for fila in filas:
            fila["lote_id"] = lote

        def _insertar() -> int:
            with get_db_session() as db:
                for fila in filas:
                    db.add(JAPPedido(**fila))
            return len(filas)

        try:
            guardados = _insertar()
        except Exception as error:
            if not _es_error_tabla_faltante(error):
                resultado["error"] = _mensaje_error(error) or "no se pudo guardar"
                return resultado
            # Tabla recien desplegada: crear el esquema UNA vez y reintentar.
            try:
                from core.database import init_db

                init_db()
            except Exception as error_init:
                resultado["error"] = (
                    "no se pudo crear la tabla jap_pedido: "
                    + (_mensaje_error(error_init) or "error desconocido")
                )
                return resultado
            try:
                guardados = _insertar()
            except Exception as error_reintento:
                resultado["error"] = (
                    _mensaje_error(error_reintento) or "no se pudo guardar"
                )
                return resultado

        resultado["ok"] = True
        resultado["guardados"] = int(guardados)
        return resultado
    except Exception as error:
        resultado["ok"] = False
        resultado["guardados"] = 0
        resultado["error"] = _mensaje_error(error) or "error inesperado"
        return resultado


def _fila_a_dict(fila: JAPPedido) -> Dict[str, Any]:
    """Serializa una fila ORM a dict plano (sin objetos de SQLAlchemy)."""
    return {
        "order_id": _entero(getattr(fila, "order_id", 0)),
        "tipo": _texto(getattr(fila, "tipo", ""), 30),
        "link": _texto(getattr(fila, "link", ""), 4000),
        "cantidad": _entero(getattr(fila, "cantidad", 0)),
        "costo": _flotante(getattr(fila, "costo", 0.0)),
        "exito": bool(getattr(fila, "exito", False)),
        "estado": _texto(getattr(fila, "estado", ""), 30),
        "fecha": _fecha_iso(getattr(fila, "fecha", None)),
        "cliente": _texto(getattr(fila, "cliente", ""), 120),
        "campana": _texto(getattr(fila, "campana", ""), 160),
        "notas": _texto(getattr(fila, "notas", ""), 4000),
    }


def pedidos_de_lote(lote_id: Any) -> List[Dict[str, Any]]:
    """Filas del lote como dicts planos (orden de insercion). [] si no hay."""
    lote = _texto(lote_id, 40)
    if not lote:
        return []
    try:
        with get_db_session() as db:
            filas = (
                db.execute(
                    select(JAPPedido)
                    .where(JAPPedido.lote_id == lote)
                    .order_by(JAPPedido.id.asc())
                )
                .scalars()
                .all()
            )
            return [_fila_a_dict(fila) for fila in filas]
    except Exception:
        return []


def listar_lotes(limite: Any = 10) -> List[Dict[str, Any]]:
    """Resumen de los ultimos lotes (mas reciente primero).

    Cada elemento trae ``lote_id, fecha, cliente, campana, pedidos, exitosos,
    fallidos, tipos, links, cantidad_total, costo_total``. ``links`` es el
    numero de publicaciones unicas con acciones exitosas (los seguidores no
    cuentan como publicacion); ``links_lista`` trae los enlaces.
    ``cantidad_total`` suma las cantidades exitosas de TODOS los tipos
    (incluidos seguidores); ``costo_total`` es de control interno.
    """
    try:
        numero = int(limite)
    except Exception:
        numero = 10
    numero = max(1, numero)
    try:
        with get_db_session() as db:
            ultimos = db.execute(
                select(
                    JAPPedido.lote_id,
                    func.max(JAPPedido.fecha),
                    func.max(JAPPedido.id),
                )
                .where(JAPPedido.lote_id != "")
                .group_by(JAPPedido.lote_id)
                .order_by(
                    func.max(JAPPedido.fecha).desc(),
                    func.max(JAPPedido.id).desc(),
                )
                .limit(numero)
            ).all()
            lotes = [fila[0] for fila in ultimos if fila[0]]
            if not lotes:
                return []
            filas = (
                db.execute(
                    select(JAPPedido)
                    .where(JAPPedido.lote_id.in_(lotes))
                    .order_by(JAPPedido.id.asc())
                )
                .scalars()
                .all()
            )
            resumenes: Dict[str, Dict[str, Any]] = {}
            for fila in filas:
                lote = _texto(fila.lote_id, 40)
                if not lote:
                    continue
                item = resumenes.setdefault(
                    lote,
                    {
                        "lote_id": lote,
                        "fecha": "",
                        "cliente": "",
                        "campana": "",
                        "pedidos": 0,
                        "exitosos": 0,
                        "fallidos": 0,
                        "tipos": set(),
                        "links": set(),
                        "cantidad_total": 0,
                        "costo_total": 0.0,
                    },
                )
                fecha_fila = _fecha_iso(fila.fecha)
                if fecha_fila and fecha_fila > item["fecha"]:
                    item["fecha"] = fecha_fila
                if not item["cliente"]:
                    item["cliente"] = _texto(fila.cliente, 120)
                if not item["campana"]:
                    item["campana"] = _texto(fila.campana, 160)
                item["pedidos"] += 1
                tipo = _normalizar_tipo(fila.tipo)
                if tipo:
                    item["tipos"].add(tipo)
                if not bool(fila.exito):
                    item["fallidos"] += 1
                    continue
                item["exitosos"] += 1
                item["cantidad_total"] += _entero(fila.cantidad)
                item["costo_total"] += _flotante(fila.costo)
                enlace = _texto(fila.link, 4000)
                if enlace and tipo != "followers":
                    item["links"].add(enlace)

        salida: List[Dict[str, Any]] = []
        for lote in lotes:
            item = resumenes.get(lote)
            if not item:
                continue
            enlaces = sorted(item["links"])
            salida.append(
                {
                    "lote_id": item["lote_id"],
                    "fecha": item["fecha"],
                    "cliente": item["cliente"],
                    "campana": item["campana"],
                    "pedidos": item["pedidos"],
                    "exitosos": item["exitosos"],
                    "fallidos": item["fallidos"],
                    "tipos": sorted(item["tipos"]),
                    "links": len(enlaces),
                    "links_lista": enlaces,
                    "cantidad_total": item["cantidad_total"],
                    "costo_total": round(item["costo_total"], 6),
                }
            )
        return salida
    except Exception:
        return []


def ultimo_lote() -> Dict[str, Any]:
    """Resumen del ultimo lote guardado o ``{"disponible": False}``."""
    try:
        lotes = listar_lotes(1)
        if not lotes:
            return {"disponible": False}
        item = dict(lotes[0])
        item["disponible"] = True
        return item
    except Exception:
        return {"disponible": False}


# --------------------------------------------------------------------------- #
# Resumen
# --------------------------------------------------------------------------- #
def resumen_lote(pedidos: Any) -> Dict[str, Any]:
    """Agrega las acciones EXITOSAS de una lista de pedidos.

    - ``acciones_total``: likes + retweets + comentarios + vistas (engagement).
    - ``seguidores``: followers exitosos (van aparte, sin enlace).
    - ``links``: publicaciones unicas con engagement (no incluye el perfil de
      seguidores).
    - ``por_tipo``: detalle por tipo con cantidad, pedidos, enlaces y costo.
    - ``pedidos_totales`` incluye fallos; ``pedidos_exitosos``/``fallidos`` no.
    """
    base = {
        "pedidos_totales": 0,
        "pedidos_exitosos": 0,
        "pedidos_fallidos": 0,
        "acciones_total": 0,
        "total_con_seguidores": 0,
        "seguidores": 0,
        "publicaciones": 0,
        "links": [],
        "links_unicos": 0,
        "cantidad_total": 0,
        "costo_total": 0.0,
        "por_tipo": {},
        "tipos_con_acciones": [],
        "hay_exitos": False,
    }
    try:
        lista = list(pedidos) if isinstance(pedidos, (list, tuple)) else []
    except Exception:
        lista = []
    if not lista:
        base["por_tipo"] = {
            tipo: {
                "cantidad": 0,
                "pedidos": 0,
                "links": 0,
                "links_lista": [],
                "costo": 0.0,
            }
            for tipo in TIPOS
        }
        for tipo in TIPOS:
            base[tipo] = 0
        return base

    por_tipo = {
        tipo: {
            "cantidad": 0,
            "pedidos": 0,
            "links": set(),
            "costo": 0.0,
        }
        for tipo in TIPOS
    }
    enlaces = set()
    costo = 0.0
    acciones = 0
    seguidores = 0
    exitosos = 0
    fallidos = 0
    for pedido in lista:
        if not isinstance(pedido, dict):
            continue
        if not _es_exito(pedido):
            fallidos += 1
            continue
        exitosos += 1
        tipo = _normalizar_tipo(pedido.get("tipo"))
        if tipo not in por_tipo:
            continue
        cantidad = _entero(pedido.get("cantidad"))
        if cantidad <= 0:
            continue
        enlace = _texto(pedido.get("link"), 4000)
        costo_pedido = _flotante(pedido.get("costo"))
        por_tipo[tipo]["cantidad"] += cantidad
        por_tipo[tipo]["pedidos"] += 1
        por_tipo[tipo]["costo"] += costo_pedido
        if tipo == "followers":
            seguidores += cantidad
        else:
            acciones += cantidad
            if enlace:
                por_tipo[tipo]["links"].add(enlace)
                enlaces.add(enlace)
        costo += costo_pedido

    base.update(
        {
            "pedidos_totales": sum(1 for p in lista if isinstance(p, dict)),
            "pedidos_exitosos": exitosos,
            "pedidos_fallidos": fallidos,
            "acciones_total": acciones,
            "total_con_seguidores": acciones + seguidores,
            "seguidores": seguidores,
            "publicaciones": len(enlaces),
            "links": sorted(enlaces),
            "links_unicos": len(enlaces),
            "cantidad_total": acciones + seguidores,
            "costo_total": round(costo, 6),
            "tipos_con_acciones": [
                tipo for tipo in TIPOS if por_tipo[tipo]["cantidad"] > 0
            ],
            "hay_exitos": exitosos > 0 and (acciones + seguidores) > 0,
        }
    )
    base["por_tipo"] = {
        tipo: {
            "cantidad": datos["cantidad"],
            "pedidos": datos["pedidos"],
            "links": len(datos["links"]),
            "links_lista": sorted(datos["links"]),
            "costo": round(datos["costo"], 6),
        }
        for tipo, datos in por_tipo.items()
    }
    for tipo in TIPOS:
        base[tipo] = por_tipo[tipo]["cantidad"]
    return base


# --------------------------------------------------------------------------- #
# Reporte HTML (para el cliente)
# --------------------------------------------------------------------------- #
_CSS_REPORTE = """
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 18px; background: #eef2f7; color: #1f2933; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; font-size: 14px; line-height: 1.55; }
.reporte { max-width: 960px; margin: 0 auto; background: #ffffff; border: 1px solid #d9e0ea; border-radius: 14px; box-shadow: 0 14px 40px rgba(15, 23, 42, 0.08); overflow: hidden; }
.encabezado { padding: 30px 34px 22px; background: #0f172a; color: #ffffff; }
.encabezado h1 { margin: 0 0 6px; font-size: 26px; font-weight: 700; letter-spacing: 0.2px; }
.encabezado p { margin: 0; color: #cbd5e1; font-size: 14px; }
.meta { display: flex; flex-wrap: wrap; gap: 10px 26px; margin-top: 18px; padding-top: 16px; border-top: 1px solid rgba(255, 255, 255, 0.16); }
.meta span { color: #e2e8f0; font-size: 13px; }
.meta strong { color: #ffffff; font-weight: 600; }
.cuerpo { padding: 28px 34px 10px; }
.resumen h2, .notas h2 { margin: 0 0 14px; font-size: 17px; }
.tarjetas { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 26px; }
.tarjeta { min-width: 132px; padding: 14px 16px; border: 1px solid #e2e8f0; border-radius: 10px; background: #f8fafc; }
.tarjeta span { display: block; color: #64748b; font-size: 12px; letter-spacing: 0.4px; text-transform: uppercase; }
.tarjeta strong { display: block; margin-top: 4px; font-size: 22px; color: #0f172a; }
.seguidores { margin: 0 0 24px; padding: 14px 16px; border-left: 4px solid #2563eb; background: #eff6ff; color: #1e3a8a; font-size: 15px; font-weight: 600; }
table { width: 100%; border-collapse: collapse; margin: 0 0 24px; font-size: 13px; }
th { padding: 10px 12px; background: #f1f5f9; color: #334155; text-align: left; font-weight: 600; border-bottom: 2px solid #e2e8f0; }
td { padding: 10px 12px; border-bottom: 1px solid #eef2f7; vertical-align: top; }
td.numero, th.numero { text-align: right; white-space: nowrap; }
td.link { max-width: 420px; word-break: break-all; }
td.link a { color: #1d4ed8; text-decoration: none; }
td.link a:hover { text-decoration: underline; }
.estado { display: inline-block; margin: 0 0 22px; padding: 6px 14px; border-radius: 999px; background: #dcfce7; color: #166534; font-size: 12px; font-weight: 700; }
.notas { margin: 0 0 24px; padding: 16px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; }
.notas p { margin: 0; color: #334155; white-space: pre-wrap; }
.vacio { margin: 10px 0 26px; padding: 30px 20px; border: 1px dashed #cbd5e1; border-radius: 10px; color: #475569; text-align: center; font-size: 15px; }
.pie { padding: 18px 34px 24px; border-top: 1px solid #e2e8f0; color: #64748b; font-size: 12px; }
@media print {
  body { background: #ffffff; padding: 0; }
  .reporte { max-width: none; border: 0; border-radius: 0; box-shadow: none; }
  .encabezado { background: #0f172a !important; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .tarjeta, .estado, .seguidores { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
}
@page { margin: 12mm; }
"""


def _datos_reporte(pedidos: Any) -> Dict[str, Any]:
    """Extrae SOLO acciones exitosas atribuibles a un enlace + seguidores."""
    datos = {
        "links": [],
        "por_link": {},
        "totales": {tipo: 0 for tipo in TIPOS_POR_ENLACE},
        "seguidores": 0,
        "publicaciones": 0,
        "acciones": 0,
    }
    try:
        lista = list(pedidos) if isinstance(pedidos, (list, tuple)) else []
    except Exception:
        lista = []
    for pedido in lista:
        if not isinstance(pedido, dict) or not _es_exito(pedido):
            continue
        tipo = _normalizar_tipo(pedido.get("tipo"))
        cantidad = _entero(pedido.get("cantidad"))
        if cantidad <= 0:
            continue
        if tipo == "followers":
            datos["seguidores"] += cantidad
            continue
        if tipo not in TIPOS_POR_ENLACE:
            continue
        enlace = _texto(pedido.get("link"), 4000)
        if not enlace:
            # Sin enlace no se puede atribuir a una publicacion.
            continue
        fila = datos["por_link"].get(enlace)
        if fila is None:
            fila = {campo: 0 for campo in TIPOS_POR_ENLACE}
            datos["por_link"][enlace] = fila
            datos["links"].append(enlace)
        fila[tipo] += cantidad
        datos["totales"][tipo] += cantidad
        datos["acciones"] += cantidad
    datos["publicaciones"] = len(datos["links"])
    return datos


def _html_vacio(
    fecha: str = "",
    cliente: str = "",
    campana: str = "",
    folio: str = "",
    notas: str = "",
) -> str:
    """HTML valido para un lote sin acciones exitosas."""
    return _documento_html(
        datos={
            "links": [],
            "por_link": {},
            "totales": {},
            "seguidores": 0,
            "publicaciones": 0,
            "acciones": 0,
        },
        fecha=fecha,
        cliente=cliente,
        campana=campana,
        folio=folio,
        notas=notas,
    )


def _documento_html(
    datos: Dict[str, Any],
    fecha: str,
    cliente: str,
    campana: str,
    folio: str,
    notas: str,
) -> str:
    """Construye el documento HTML completo (todo dato va escapado)."""

    def esc(valor: Any) -> str:
        return html.escape(str(valor if valor is not None else ""), quote=True)

    fecha_txt = _fecha_legible(fecha)
    folio_txt = _texto(folio, 40)
    cliente_txt = _texto(cliente, 120)
    campana_txt = _texto(campana, 160)
    notas_txt = _texto(notas, 4000)

    metadatos = []
    if cliente_txt:
        metadatos.append(f"<span><strong>Cliente:</strong> {esc(cliente_txt)}</span>")
    if campana_txt:
        metadatos.append(f"<span><strong>Campaña:</strong> {esc(campana_txt)}</span>")
    metadatos.append(f"<span><strong>Fecha:</strong> {esc(fecha_txt)}</span>")
    if folio_txt:
        metadatos.append(f"<span><strong>Folio:</strong> {esc(folio_txt)}</span>")
    meta_html = "".join(metadatos)

    tarjetas = []
    if datos.get("publicaciones"):
        tarjetas.append(("Publicaciones", datos["publicaciones"]))
    if datos.get("acciones"):
        tarjetas.append(("Total de acciones", datos["acciones"]))
    for tipo in TIPOS_POR_ENLACE:
        cantidad = (datos.get("totales") or {}).get(tipo, 0)
        if cantidad:
            tarjetas.append((ETIQUETAS_TIPO[tipo], cantidad))
    tarjetas_html = "".join(
        f"<div class=\"tarjeta\"><span>{esc(etiqueta)}</span>"
        f"<strong>{int(cantidad):,}</strong></div>"
        for etiqueta, cantidad in tarjetas
    )
    resumen_html = (
        "<section class=\"resumen\"><h2>Resumen</h2>"
        f"<div class=\"tarjetas\">{tarjetas_html}</div></section>"
        if tarjetas
        else ""
    )

    seguidores = int(datos.get("seguidores") or 0)
    seguidores_html = (
        f"<p class=\"seguidores\">Seguidores nuevos: {seguidores:,}</p>"
        if seguidores
        else ""
    )

    columnas = [
        tipo
        for tipo in TIPOS_POR_ENLACE
        if int((datos.get("totales") or {}).get(tipo, 0)) > 0
    ]
    tabla_html = ""
    if datos.get("links") and columnas:
        encabezados = "<th>Publicación</th>" + "".join(
            f"<th class=\"numero\">{esc(ETIQUETAS_TIPO[tipo])}</th>"
            for tipo in columnas
        )
        filas = []
        for enlace in datos["links"]:
            fila = datos["por_link"].get(enlace) or {}
            celdas = []
            for tipo in columnas:
                valor = int(fila.get(tipo) or 0)
                celdas.append(f"<td class=\"numero\">{valor:,}</td>")
            filas.append(
                "<tr>"
                f"<td class=\"link\"><a href=\"{esc(enlace)}\" "
                f"target=\"_blank\" rel=\"noopener noreferrer\">{esc(enlace)}</a></td>"
                + "".join(celdas)
                + "</tr>"
            )
        tabla_html = (
            "<section class=\"detalle\"><h2>Detalle por publicación</h2>"
            "<table><thead><tr>"
            + encabezados
            + "</tr></thead><tbody>"
            + "".join(filas)
            + "</tbody></table></section>"
        )

    hay_acciones = bool(datos.get("acciones") or seguidores)
    if hay_acciones:
        estado_html = "<div class=\"estado\">Acciones registradas</div>"
        contenido = (
            (resumen_html or "")
            + seguidores_html
            + estado_html
            + tabla_html
        )
    else:
        contenido = (
            "<div class=\"vacio\">Sin acciones registradas</div>"
            + seguidores_html
            + tabla_html
        )

    notas_html = (
        "<section class=\"notas\"><h2>Notas</h2>"
        f"<p>{esc(notas_txt)}</p></section>"
        if notas_txt
        else ""
    )

    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"es\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Reporte de acciones realizadas</title>\n"
        f"<style>{_CSS_REPORTE}</style>\n"
        "</head>\n"
        "<body>\n"
        "<div class=\"reporte\">\n"
        "<header class=\"encabezado\">\n"
        "<h1>Reporte de acciones realizadas</h1>\n"
        "<p>Reporte de engagement en redes</p>\n"
        f"<div class=\"meta\">{meta_html}</div>\n"
        "</header>\n"
        f"<main class=\"cuerpo\">{contenido}</main>\n"
        f"{notas_html}\n"
        "<footer class=\"pie\">Reporte generado por MW Group"
        f" · Fecha de generación: {esc(fecha_txt)}</footer>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


def construir_reporte_html(
    pedidos: Any,
    fecha: str = "",
    cliente: str = "",
    campana: str = "",
    folio: str = "",
    notas: str = "",
) -> str:
    """Reporte HTML autocontenido y listo para imprimir, SOLO para el cliente.

    Cuenta unicamente pedidos exitosos. Los fallos no aparecen. No incluye
    costos, proveedor, ids de servicio, saldo ni estados internos. `notas` es
    opcional y el llamador decide si son aptas para el cliente. NUNCA lanza.
    """
    try:
        datos = _datos_reporte(pedidos)
        return _documento_html(
            datos=datos,
            fecha=fecha,
            cliente=cliente,
            campana=campana,
            folio=folio,
            notas=notas,
        )
    except Exception:
        return _html_vacio(
            fecha=fecha,
            cliente=cliente,
            campana=campana,
            folio=folio,
            notas=notas,
        )


# --------------------------------------------------------------------------- #
# Reporte CSV (para el cliente)
# --------------------------------------------------------------------------- #
def construir_reporte_csv(
    pedidos: Any,
    fecha: str = "",
    cliente: str = "",
    campana: str = "",
    folio: str = "",
    notas: str = "",
) -> str:
    """CSV UTF-8 con BOM (Excel), SOLO con acciones exitosas.

    Encabezado de metadatos, tabla por publicacion (solo columnas con datos) y
    una fila unica de seguidores. Sin costos, ids de servicio ni proveedor.
    NUNCA lanza.
    """
    try:
        datos = _datos_reporte(pedidos)
        fecha_txt = _fecha_legible(fecha)
        folio_txt = _texto(folio, 40)
        cliente_txt = _texto(cliente, 120)
        campana_txt = _texto(campana, 160)
        notas_txt = _texto(notas, 4000)

        salida = io.StringIO(newline="")
        writer = csv.writer(salida, lineterminator="\n")
        writer.writerow(["Reporte de acciones realizadas"])
        if cliente_txt:
            writer.writerow(["Cliente", cliente_txt])
        if campana_txt:
            writer.writerow(["Campaña", campana_txt])
        writer.writerow(["Fecha", fecha_txt])
        if folio_txt:
            writer.writerow(["Folio", folio_txt])
        writer.writerow([])

        columnas = [
            tipo
            for tipo in TIPOS_POR_ENLACE
            if int((datos.get("totales") or {}).get(tipo, 0)) > 0
        ]
        if datos.get("links") and columnas:
            writer.writerow(["Publicación"] + [ETIQUETAS_TIPO[t] for t in columnas])
            for enlace in datos["links"]:
                fila = datos["por_link"].get(enlace) or {}
                writer.writerow(
                    [enlace] + [int(fila.get(tipo) or 0) for tipo in columnas]
                )
            writer.writerow([])
        elif not datos.get("seguidores"):
            writer.writerow(["Sin acciones registradas"])
            writer.writerow([])

        seguidores = int(datos.get("seguidores") or 0)
        if seguidores:
            writer.writerow(["Seguidores", seguidores])
        if notas_txt:
            writer.writerow(["Notas", notas_txt])
        return "\ufeff" + salida.getvalue()
    except Exception:
        salida = io.StringIO(newline="")
        writer = csv.writer(salida, lineterminator="\n")
        writer.writerow(["Reporte de acciones realizadas"])
        writer.writerow(["Sin acciones registradas"])
        return "\ufeff" + salida.getvalue()


# --------------------------------------------------------------------------- #
# Reporte de un lote guardado
# --------------------------------------------------------------------------- #
def _nombre_archivo(folio: str, formato: str) -> str:
    """Nombre seguro ``reporte-<folio>.<ext>`` (sin rutas ni proveedor)."""
    seguro = re.sub(r"[^A-Za-z0-9_-]+", "-", _texto(folio, 40)).strip("-")
    if not seguro:
        seguro = "lote"
    return f"reporte-{seguro}.{formato}"


def generar_reporte_lote(lote_id: Any, formato: str = "html") -> dict:
    """Lee un lote de la BD y devuelve ``{"ok","contenido","filename","error"}``.

    ``formato`` es ``"html"`` o ``"csv"``. Nunca lanza.
    """
    resultado = {"ok": False, "contenido": "", "filename": "", "error": ""}
    try:
        lote = _texto(lote_id, 40)
        if not lote:
            resultado["error"] = "lote_id vacio"
            return resultado
        elegido = _texto(formato, 10).lower() or "html"
        if elegido not in ("html", "csv"):
            resultado["error"] = "formato no valido (usa html o csv)"
            return resultado
        pedidos = pedidos_de_lote(lote)
        if not pedidos:
            resultado["error"] = "lote no encontrado o sin pedidos"
            return resultado
        cliente = next((p.get("cliente") or "" for p in pedidos if p.get("cliente")), "")
        campana = next((p.get("campana") or "" for p in pedidos if p.get("campana")), "")
        fecha = _fecha_legible(pedidos[0].get("fecha"))
        if elegido == "html":
            contenido = construir_reporte_html(
                pedidos,
                fecha=fecha,
                cliente=cliente,
                campana=campana,
                folio=lote,
            )
        else:
            contenido = construir_reporte_csv(
                pedidos,
                fecha=fecha,
                cliente=cliente,
                campana=campana,
                folio=lote,
            )
        resultado.update(
            {
                "ok": True,
                "contenido": contenido,
                "filename": _nombre_archivo(lote, elegido),
            }
        )
        return resultado
    except Exception as error:
        resultado["error"] = _mensaje_error(error) or "error inesperado"
        return resultado
