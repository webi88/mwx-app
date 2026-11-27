# -*- coding: utf-8 -*-
"""Tests de `core/jap_historial.py` (historial de lotes + reportes al cliente).

SIN red, SIN Chrome y SIN tocar la BD real: todo corre contra un SQLite EN
MEMORIA (`StaticPool`) parcheando `core.database.SessionLocal`. La tabla
`jap_pedido` se crea con `Base.metadata.create_all` y se prueba tambien el
camino "tabla faltante -> init_db() + reintento" con un `init_db` falso.

Cubre:
  (1) Modelo `JAPPedido`: tabla/columnas/index y ausencia de datos del
      proveedor.
  (2) Folio `LOTE-YYYYmmdd-HHMMSS-xxxx` sin la palabra del proveedor.
  (3) `guardar_pedidos`: 2 exitos + 1 fallo, ordenes 0 cuando falla, tipos
      normalizados, cliente/campaña/notas, lista vacia/invalida y folio
      autogenerado.
  (4) `pedidos_de_lote`, `listar_lotes`, `ultimo_lote` y `resumen_lote` (solo
      exitos en los totales del reporte).
  (5) HTML: cliente/campaña/fecha/folio, links escapados y clicables, solo
      columnas con datos, seguidores UNA vez, "Acciones registradas",
      "Reporte generado por MW Group" y CERO menciones al proveedor/costos/
      ids de servicio.
  (6) CSV: BOM UTF-8, columnas correctas, seguidores una sola fila y las
      mismas garantias de contenido.
  (7) `generar_reporte_lote`: html/csv, nombre `reporte-<folio>`, lote
      inexistente/formato invalido y saneado del nombre.
  (8) Tolerancia: BD caida, tabla faltante, init_db roto, listas vacias y
      entradas raras -> `ok=False`/contenido vacio, nunca excepcion.

Uso:
    .venv/Scripts/python.exe tests/test_jap_historial.py
"""
from __future__ import annotations

import csv
import io
import re
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# La consola de Windows usa cp1252 y varios checks llevan acentos.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core import database  # noqa: E402
from core import jap_historial as historial  # noqa: E402
from core.models import Base, JAPPedido  # noqa: E402

LINK_A = "https://x.com/cliente/status/1001"
LINK_B = "https://x.com/cliente/status/1002"
LINK_PERFIL = "https://x.com/perfil_demo"
LOTE_1 = "LOTE-TEST-0001"
LOTE_2 = "LOTE-TEST-0002"
LOTE_3 = "LOTE-TEST-0003"

# Palabras/ids que NUNCA deben aparecer en un reporte para el cliente.
PROHIBIDOS = (
    "jap",
    "just another panel",
    "proveedor",
    "costo",
    "saldo",
    "api",
    "compra",
    "9013",
    "6601",
    "10186",
    "8860",
    "8413",
    "9011",
    "8855",
)

COLUMNAS_ESPERADAS = {
    "id",
    "lote_id",
    "order_id",
    "tipo",
    "link",
    "cantidad",
    "costo",
    "exito",
    "estado",
    "fecha",
    "cliente",
    "campana",
    "notas",
}


def _engine_memoria():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _fabrica(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _prohibidos(texto) -> list:
    bajo = str(texto or "").lower()
    return [palabra for palabra in PROHIBIDOS if palabra in bajo]


def _pedidos_principal() -> list:
    """2 exitos (likes/retweets en el MISMO link) + 1 fallo (comments)."""
    return [
        {
            "order": 101,
            "tipo": "likes",
            "link": LINK_A,
            "cantidad": 50,
            "costo": 0.05,
            "ok": True,
        },
        {
            "order_id": 102,
            "tipo": "retweet",  # alias que debe normalizarse a "retweets"
            "link": LINK_A,
            "cantidad": 10,
            "costo": 0.01,
            "ok": True,
        },
        {
            "order": None,
            "tipo": "comments",
            "link": LINK_B,
            "cantidad": 5,
            "costo": 0.2,
            "ok": False,
            "error": "fallo de prueba",
        },
    ]


def _pedidos_seguidores() -> list:
    """3 exitos: followers (sin link por publicacion), views y comments."""
    return [
        {
            "order": 201,
            "tipo": "followers",
            "link": LINK_PERFIL,
            "cantidad": 25,
            "costo": 0.01,
            "ok": True,
        },
        {
            "order": 202,
            "tipo": "views",
            "link": LINK_A,
            "cantidad": 500,
            "costo": 0.002,
            "ok": True,
        },
        {
            "order": 203,
            "tipo": "comments",
            "link": LINK_B,
            "cantidad": 3,
            "costo": 0.1,
            "ok": True,
        },
    ]


# --------------------------------------------------------------------------- #
# (1) Modelo
# --------------------------------------------------------------------------- #
def test_modelo(check):
    print("(1) modelo JAPPedido")
    columnas = set(JAPPedido.__table__.columns.keys())
    check("modelo: tabla jap_pedido", JAPPedido.__tablename__ == "jap_pedido")
    check(
        "modelo: columnas exactas del contrato",
        columnas == COLUMNAS_ESPERADAS,
        str(sorted(columnas)),
    )
    check(
        "modelo: lote_id indexado",
        bool(JAPPedido.__table__.columns["lote_id"].index),
    )
    check(
        "modelo: no guarda datos del proveedor",
        not ({"servicio", "servicio_nombre", "api_key", "comments"} & columnas),
    )
    check(
        "modelo: registrada en Base.metadata",
        "jap_pedido" in Base.metadata.tables,
    )


# --------------------------------------------------------------------------- #
# (2) Folio
# --------------------------------------------------------------------------- #
def test_lote_id(check):
    print("(2) nuevo_lote_id")
    folio = historial.nuevo_lote_id()
    check("folio: prefijo LOTE-", folio.startswith("LOTE-"), folio)
    check(
        "folio: formato LOTE-YYYYmmdd-HHMMSS-xxxx",
        bool(re.match(r"^LOTE-\d{8}-\d{6}-[a-z0-9]{4}$", folio)),
        folio,
    )
    check(
        "folio: sin nombre del proveedor",
        "jap" not in folio.lower() and "panel" not in folio.lower(),
    )
    check("folio: dos folios distintos", historial.nuevo_lote_id() != folio)


# --------------------------------------------------------------------------- #
# (3) guardar_pedidos
# --------------------------------------------------------------------------- #
def test_guardar(check, fabrica):
    print("(3) guardar_pedidos")
    resultado = historial.guardar_pedidos(
        _pedidos_principal(),
        lote_id=LOTE_1,
        cliente="Cliente Demo",
        campana="Campaña Demo",
        notas="Nota interna del lote",
    )
    check(
        "guardar: ok con 3 resultados (2 exitos + 1 fallo)",
        resultado.get("ok") is True
        and resultado.get("guardados") == 3
        and resultado.get("lote_id") == LOTE_1
        and resultado.get("error") == "",
        str(resultado),
    )

    db = fabrica()
    try:
        filas = (
            db.query(JAPPedido)
            .filter(JAPPedido.lote_id == LOTE_1)
            .order_by(JAPPedido.id.asc())
            .all()
        )
        check("guardar: 3 filas persistidas", len(filas) == 3, str(len(filas)))
        primera, segunda, tercera = filas
        check(
            "guardar: order_id del primer exito",
            primera.order_id == 101 and primera.exito is True,
        )
        check(
            "guardar: order_id alternativo (order_id) del segundo",
            segunda.order_id == 102 and segunda.exito is True,
        )
        check(
            "guardar: fallo con order_id 0 y exito False",
            tercera.order_id == 0 and tercera.exito is False,
        )
        check(
            "guardar: estado del fallo conserva el motivo",
            "fallo de prueba" in (tercera.estado or ""),
            str(tercera.estado),
        )
        check(
            "guardar: cliente/campaña/notas aplicados",
            primera.cliente == "Cliente Demo"
            and primera.campana == "Campaña Demo"
            and primera.notas == "Nota interna del lote",
        )
        check(
            "guardar: costo y cantidad persistidos",
            abs(float(primera.costo) - 0.05) < 1e-9
            and primera.cantidad == 50,
        )
        check("guardar: fecha asignada", primera.fecha is not None)
    finally:
        db.close()

    pedidos_1 = historial.pedidos_de_lote(LOTE_1)
    check(
        "guardar: pedidos_de_lote devuelve 3 dicts",
        len(pedidos_1) == 3 and all(isinstance(p, dict) for p in pedidos_1),
    )
    claves = {
        "order_id",
        "tipo",
        "link",
        "cantidad",
        "costo",
        "exito",
        "estado",
        "fecha",
        "cliente",
        "campana",
        "notas",
    }
    check(
        "guardar: contrato exacto de claves (sin ORM ni proveedor)",
        set(pedidos_1[0].keys()) == claves,
        str(sorted(pedidos_1[0].keys())),
    )
    check(
        "guardar: alias retweet -> retweets",
        pedidos_1[1]["tipo"] == "retweets",
        pedidos_1[1]["tipo"],
    )
    check(
        "guardar: fecha serializada ISO",
        bool(
            re.match(
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
                pedidos_1[0]["fecha"],
            )
        ),
        pedidos_1[0]["fecha"],
    )

    resultado_2 = historial.guardar_pedidos(
        _pedidos_seguidores(),
        lote_id=LOTE_2,
        cliente="Cliente Demo",
        campana="Campaña Demo",
    )
    check(
        "guardar: segundo lote con 3 exitos",
        resultado_2.get("ok") is True and resultado_2.get("guardados") == 3,
        str(resultado_2),
    )
    pedidos_2 = historial.pedidos_de_lote(LOTE_2)
    check("guardar: segundo lote legible", len(pedidos_2) == 3)

    auto = historial.guardar_pedidos(
        [
            {
                "tipo": "views",
                "link": LINK_A,
                "cantidad": 100,
                "ok": True,
            }
        ]
    )
    check(
        "guardar: folio autogenerado cuando falta",
        auto.get("ok") is True
        and auto.get("lote_id", "").startswith("LOTE-")
        and auto.get("guardados") == 1,
        str(auto),
    )

    vacio = historial.guardar_pedidos([])
    check(
        "guardar: lista vacia -> ok False sin excepcion",
        vacio.get("ok") is False and vacio.get("guardados") == 0 and vacio.get("error"),
        str(vacio),
    )
    invalido = historial.guardar_pedidos("no es lista")
    check(
        "guardar: entrada no-lista -> ok False",
        invalido.get("ok") is False and bool(invalido.get("error")),
        str(invalido),
    )
    return pedidos_1, pedidos_2


# --------------------------------------------------------------------------- #
# (4) Consultas y resumen
# --------------------------------------------------------------------------- #
def test_listar(check, fabrica):
    print("(4) pedidos_de_lote / listar_lotes / ultimo_lote / resumen_lote")
    ultimo = historial.guardar_pedidos(
        [
            {
                "order": 301,
                "tipo": "followers",
                "link": LINK_PERFIL,
                "cantidad": 10,
                "costo": 0.01,
                "ok": True,
            }
        ],
        lote_id=LOTE_3,
        cliente="Cliente Demo",
        campana="Campaña Demo",
    )
    check("listar: lote de prueba guardado", ultimo.get("ok") is True)

    lotes = historial.listar_lotes(10)
    check(
        "listar: devuelve una lista con los 4 lotes",
        isinstance(lotes, list) and len(lotes) >= 4,
        str(len(lotes)),
    )
    check(
        "listar: mas reciente primero",
        bool(lotes) and lotes[0].get("lote_id") == LOTE_3,
        str(lotes[0].get("lote_id") if lotes else None),
    )
    por_lote = {lote.get("lote_id"): lote for lote in lotes}
    primero = por_lote.get(LOTE_1) or {}
    segundo = por_lote.get(LOTE_2) or {}
    check(
        "listar: resumen lote 1 (3 pedidos, 2 exitos, 1 fallo)",
        primero.get("pedidos") == 3
        and primero.get("exitosos") == 2
        and primero.get("fallidos") == 1,
        str(primero),
    )
    check(
        "listar: tipos incluye el fallo, links solo exitos",
        primero.get("tipos") == ["comments", "likes", "retweets"]
        and primero.get("links") == 1
        and primero.get("links_lista") == [LINK_A],
        str(primero.get("tipos")),
    )
    check(
        "listar: cantidad_total solo exitos y costo_total",
        primero.get("cantidad_total") == 60
        and abs(float(primero.get("costo_total") or 0.0) - 0.06) < 1e-6,
        str(primero),
    )
    check(
        "listar: cliente/campaña/fecha del lote",
        primero.get("cliente") == "Cliente Demo"
        and primero.get("campana") == "Campaña Demo"
        and bool(primero.get("fecha")),
    )
    check(
        "listar: resumen lote 2 (3 exitos, followers fuera de links)",
        segundo.get("pedidos") == 3
        and segundo.get("exitosos") == 3
        and segundo.get("fallidos") == 0
        and segundo.get("links") == 2
        and segundo.get("cantidad_total") == 528,
        str(segundo),
    )
    check(
        "listar: limite=1 devuelve solo el ultimo",
        len(historial.listar_lotes(1)) == 1
        and historial.listar_lotes(1)[0].get("lote_id") == LOTE_3,
    )
    check(
        "listar: limite invalido no lanza",
        isinstance(historial.listar_lotes("basura"), list),
    )

    ultimo_lote = historial.ultimo_lote()
    check(
        "ultimo_lote: disponible con el folio mas reciente",
        ultimo_lote.get("disponible") is True
        and ultimo_lote.get("lote_id") == LOTE_3,
        str(ultimo_lote),
    )

    # Limpieza de la fabrica para evitar warnings al cerrar (no borra los lotes).
    check("listar: fabrica sigue operativa", fabrica is not None)


def test_resumen(check, pedidos_1, pedidos_2):
    print("(4b) resumen_lote")
    resumen_1 = historial.resumen_lote(pedidos_1)
    check(
        "resumen: totales/fallos del lote 1",
        resumen_1.get("pedidos_totales") == 3
        and resumen_1.get("pedidos_exitosos") == 2
        and resumen_1.get("pedidos_fallidos") == 1,
        str(resumen_1.get("pedidos_totales")),
    )
    check(
        "resumen: likes y retweets solo exitos",
        resumen_1.get("likes") == 50 and resumen_1.get("retweets") == 10,
        str(resumen_1),
    )
    check(
        "resumen: comentarios fallidos NO cuentan",
        resumen_1.get("comments") == 0
        and resumen_1.get("por_tipo", {}).get("comments", {}).get("cantidad") == 0,
    )
    check(
        "resumen: acciones/links/costo del lote 1",
        resumen_1.get("acciones_total") == 60
        and resumen_1.get("publicaciones") == 1
        and resumen_1.get("links") == [LINK_A]
        and abs(float(resumen_1.get("costo_total") or 0.0) - 0.06) < 1e-6,
        str(resumen_1),
    )
    check(
        "resumen: el link fallido no aparece",
        LINK_B not in resumen_1.get("links", []),
    )
    check(
        "resumen: hay_exitos True y tipos con acciones",
        resumen_1.get("hay_exitos") is True
        and resumen_1.get("tipos_con_acciones") == ["likes", "retweets"],
        str(resumen_1.get("tipos_con_acciones")),
    )

    resumen_2 = historial.resumen_lote(pedidos_2)
    check(
        "resumen: seguidores separados del engagement",
        resumen_2.get("seguidores") == 25
        and resumen_2.get("acciones_total") == 503
        and resumen_2.get("total_con_seguidores") == 528,
        str(resumen_2),
    )
    check(
        "resumen: 2 publicaciones (perfil de seguidores no cuenta)",
        resumen_2.get("publicaciones") == 2
        and sorted(resumen_2.get("links") or []) == sorted([LINK_A, LINK_B]),
    )
    check(
        "resumen: por_tipo trae cantidad/pedidos/links",
        resumen_2.get("por_tipo", {}).get("followers", {}).get("cantidad") == 25
        and resumen_2.get("por_tipo", {}).get("views", {}).get("links") == 1
        and resumen_2.get("por_tipo", {}).get("comments", {}).get("links") == 1,
    )

    vacio = historial.resumen_lote([])
    check(
        "resumen: lista vacia -> ceros sin excepcion",
        vacio.get("pedidos_totales") == 0
        and vacio.get("acciones_total") == 0
        and vacio.get("likes") == 0
        and vacio.get("hay_exitos") is False,
        str(vacio),
    )
    solo_fallos = historial.resumen_lote(
        [{"tipo": "likes", "cantidad": 9, "ok": False}]
    )
    check(
        "resumen: solo fallos -> 0 acciones",
        solo_fallos.get("pedidos_fallidos") == 1
        and solo_fallos.get("acciones_total") == 0
        and solo_fallos.get("hay_exitos") is False,
    )
    raro = historial.resumen_lote(None)
    check(
        "resumen: None -> ceros sin excepcion",
        raro.get("pedidos_totales") == 0 and raro.get("hay_exitos") is False,
    )


# --------------------------------------------------------------------------- #
# (5) Reporte HTML
# --------------------------------------------------------------------------- #
def test_html(check, pedidos_1, pedidos_2):
    print("(5) construir_reporte_html")
    html_1 = historial.construir_reporte_html(
        pedidos_1,
        fecha="01/02/2026",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio=LOTE_1,
        notas="Todo listo para publicar",
    )
    check(
        "html: documento valido y con estilos",
        "<html" in html_1
        and "</html>" in html_1
        and "<style>" in html_1
        and "@media print" in html_1,
    )
    check(
        "html: titulos del reporte",
        "Reporte de acciones realizadas" in html_1
        and "Reporte de engagement en redes" in html_1,
    )
    check(
        "html: encabezado cliente/campaña/fecha/folio",
        "Cliente Demo" in html_1
        and "Campaña Demo" in html_1
        and "01/02/2026" in html_1
        and LOTE_1 in html_1,
    )
    check(
        "html: link, likes y retweets visibles",
        LINK_A in html_1 and "Likes" in html_1 and "Retweets" in html_1,
    )
    check(
        "html: cantidades correctas",
        ">50<" in html_1 and ">10<" in html_1 and ">60<" in html_1,
    )
    check(
        "html: el fallo no genera columna ni link",
        "Comentarios" not in html_1 and LINK_B not in html_1,
    )
    check(
        "html: estado y pie de MW Group",
        "Acciones registradas" in html_1
        and html_1.count("Reporte generado por MW Group") == 1,
    )
    check(
        "html: notas opcionales incluidas",
        "Todo listo para publicar" in html_1,
    )
    check(
        "html: sin seguidores no muestra la seccion",
        "Seguidores nuevos" not in html_1,
    )
    check(
        "html: CERO menciones al proveedor/costos/ids",
        not _prohibidos(html_1),
        str(_prohibidos(html_1)),
    )

    html_2 = historial.construir_reporte_html(
        pedidos_2,
        fecha="02/02/2026",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio=LOTE_2,
    )
    check(
        "html: seguidores UNA sola vez",
        html_2.count("Seguidores nuevos: 25") == 1,
        str(html_2.count("Seguidores nuevos")),
    )
    check(
        "html: el perfil de seguidores no va en la tabla",
        LINK_PERFIL not in html_2,
    )
    check(
        "html: solo columnas con datos (comentarios/vistas)",
        "Comentarios" in html_2
        and "Vistas" in html_2
        and "Likes" not in html_2
        and "Retweets" not in html_2,
    )
    check(
        "html: cantidades del lote 2",
        ">500<" in html_2 and ">3<" in html_2,
    )
    check(
        "html: lote 2 sin menciones prohibidas",
        not _prohibidos(html_2),
        str(_prohibidos(html_2)),
    )

    html_3 = historial.construir_reporte_html(
        [
            {
                "tipo": "likes",
                "link": "https://x.com/a/status/3?x=1&y=2",
                "cantidad": 7,
                "ok": True,
            }
        ],
        cliente="<b>Cliente & Cía</b>",
    )
    check(
        "html: escapa el cliente",
        "&lt;b&gt;Cliente &amp; Cía&lt;/b&gt;" in html_3
        and "<b>Cliente" not in html_3,
    )
    check(
        "html: escapa el link en href y texto",
        'href="https://x.com/a/status/3?x=1&amp;y=2"' in html_3
        and "?x=1&y=2" not in html_3,
    )
    check(
        "html: link clicable",
        'target="_blank"' in html_3 and "noopener noreferrer" in html_3,
    )

    html_vacio = historial.construir_reporte_html([])
    check(
        "html: sin exitos -> valido con mensaje",
        "<html" in html_vacio
        and "Sin acciones registradas" in html_vacio
        and not _prohibidos(html_vacio),
    )
    check(
        "html: solo fallos -> sin acciones",
        "Sin acciones registradas"
        in historial.construir_reporte_html(
            [{"tipo": "likes", "link": LINK_A, "cantidad": 9, "ok": False}]
        ),
    )
    check(
        "html: None -> sin acciones sin excepcion",
        "Sin acciones registradas" in historial.construir_reporte_html(None),
    )

    html_followers = historial.construir_reporte_html(
        [
            {
                "tipo": "followers",
                "link": LINK_PERFIL,
                "cantidad": 40,
                "ok": True,
            }
        ],
        folio=LOTE_2,
    )
    check(
        "html: solo seguidores -> seccion unica y sin tabla",
        "Seguidores nuevos: 40" in html_followers
        and "<table" not in html_followers
        and LINK_PERFIL not in html_followers,
    )


# --------------------------------------------------------------------------- #
# (6) Reporte CSV
# --------------------------------------------------------------------------- #
def test_csv(check, pedidos_1, pedidos_2):
    print("(6) construir_reporte_csv")
    csv_1 = historial.construir_reporte_csv(
        pedidos_1,
        fecha="01/02/2026",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio=LOTE_1,
        notas="Todo listo",
    )
    check("csv: BOM UTF-8 para Excel", csv_1.startswith("\ufeff"))
    check(
        "csv: metadata cliente/campaña/fecha/folio",
        "Cliente,Cliente Demo" in csv_1
        and "Campaña,Campaña Demo" in csv_1
        and "Fecha,01/02/2026" in csv_1
        and f"Folio,{LOTE_1}" in csv_1,
    )
    check(
        "csv: columnas correctas y solo con datos",
        "Publicación,Likes,Retweets" in csv_1
        and "Comentarios" not in csv_1
        and "Vistas" not in csv_1,
    )
    check(
        "csv: link y cantidades",
        f"{LINK_A},50,10" in csv_1,
    )
    check(
        "csv: el fallo no aparece",
        LINK_B not in csv_1 and "fallo de prueba" not in csv_1,
    )
    check(
        "csv: notas y CERO menciones prohibidas",
        "Notas,Todo listo" in csv_1 and not _prohibidos(csv_1),
        str(_prohibidos(csv_1)),
    )
    check(
        "csv: sin seguidores no hay fila de seguidores",
        "Seguidores" not in csv_1,
    )

    lector = list(csv.reader(io.StringIO(csv_1.lstrip("\ufeff"))))
    indice = next(
        (i for i, fila in enumerate(lector) if fila and fila[0] == "Publicación"),
        -1,
    )
    check(
        "csv: cabecera parseable",
        indice >= 0 and lector[indice] == ["Publicación", "Likes", "Retweets"],
        str(lector[indice] if indice >= 0 else None),
    )
    check(
        "csv: fila del link parseable",
        indice >= 0
        and lector[indice + 1][0] == LINK_A
        and lector[indice + 1][1:] == ["50", "10"],
        str(lector[indice + 1] if indice >= 0 else None),
    )

    csv_2 = historial.construir_reporte_csv(
        pedidos_2,
        fecha="02/02/2026",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio=LOTE_2,
    )
    check(
        "csv: columnas del lote 2 (comentarios/vistas)",
        "Publicación,Comentarios,Vistas" in csv_2
        and "Likes" not in csv_2
        and "Retweets" not in csv_2,
    )
    check(
        "csv: seguidores UNA sola fila",
        csv_2.count("Seguidores") == 1 and "Seguidores,25" in csv_2,
    )
    check(
        "csv: perfil de seguidores no aparece",
        LINK_PERFIL not in csv_2,
    )
    lector_2 = list(csv.reader(io.StringIO(csv_2.lstrip("\ufeff"))))
    indice_2 = next(
        (i for i, fila in enumerate(lector_2) if fila and fila[0] == "Publicación"),
        -1,
    )
    filas_2 = {
        fila[0]: fila
        for fila in lector_2[indice_2 + 1:]
        if fila and fila[0].startswith("http")
    } if indice_2 >= 0 else {}
    check(
        "csv: filas por link del lote 2",
        filas_2.get(LINK_A) == [LINK_A, "0", "500"]
        and filas_2.get(LINK_B) == [LINK_B, "3", "0"],
        str(filas_2),
    )
    check(
        "csv: lote 2 sin menciones prohibidas",
        not _prohibidos(csv_2),
        str(_prohibidos(csv_2)),
    )

    csv_vacio = historial.construir_reporte_csv([])
    check(
        "csv: vacio -> BOM y mensaje sin cabecera de tabla",
        csv_vacio.startswith("\ufeff")
        and "Sin acciones registradas" in csv_vacio
        and "Publicación,Likes" not in csv_vacio,
    )
    check(
        "csv: None -> valido sin excepcion",
        historial.construir_reporte_csv(None).lstrip("\ufeff").startswith(
            "Reporte de acciones realizadas"
        ),
    )


# --------------------------------------------------------------------------- #
# (7) generar_reporte_lote
# --------------------------------------------------------------------------- #
def test_generar(check):
    print("(7) generar_reporte_lote")
    html = historial.generar_reporte_lote(LOTE_1, "html")
    check(
        "generar: html ok con nombre reporte-<folio>.html",
        html.get("ok") is True
        and html.get("filename") == f"reporte-{LOTE_1}.html"
        and "Cliente Demo" in (html.get("contenido") or "")
        and LOTE_1 in (html.get("contenido") or ""),
        str(html.get("filename")),
    )
    check(
        "generar: html sin menciones prohibidas",
        not _prohibidos(html.get("contenido") or ""),
        str(_prohibidos(html.get("contenido") or "")),
    )
    csv_reporte = historial.generar_reporte_lote(LOTE_2, "csv")
    check(
        "generar: csv ok con BOM y nombre .csv",
        csv_reporte.get("ok") is True
        and csv_reporte.get("filename") == f"reporte-{LOTE_2}.csv"
        and (csv_reporte.get("contenido") or "").startswith("\ufeff"),
        str(csv_reporte.get("filename")),
    )
    check(
        "generar: csv sin menciones prohibidas",
        not _prohibidos(csv_reporte.get("contenido") or ""),
    )
    inexistente = historial.generar_reporte_lote("LOTE-NO-EXISTE", "html")
    check(
        "generar: lote inexistente -> ok False y contenido vacio",
        inexistente.get("ok") is False
        and inexistente.get("contenido") == ""
        and inexistente.get("filename") == ""
        and bool(inexistente.get("error")),
        str(inexistente),
    )
    formato = historial.generar_reporte_lote(LOTE_1, "pdf")
    check(
        "generar: formato invalido -> ok False",
        formato.get("ok") is False and bool(formato.get("error")),
        str(formato),
    )
    vacio = historial.generar_reporte_lote("", "html")
    check("generar: lote vacio -> ok False", vacio.get("ok") is False)

    raro = historial.guardar_pedidos(
        [
            {
                "tipo": "likes",
                "link": LINK_A,
                "cantidad": 1,
                "ok": True,
            }
        ],
        lote_id="LOTE/SLASH/1",
    )
    check("generar: lote con barras guardado", raro.get("ok") is True)
    saneado = historial.generar_reporte_lote("LOTE/SLASH/1", "html")
    check(
        "generar: nombre de archivo saneado (sin rutas)",
        saneado.get("ok") is True
        and "/" not in (saneado.get("filename") or "")
        and "\\" not in (saneado.get("filename") or "")
        and (saneado.get("filename") or "").startswith("reporte-LOTE-SLASH-1"),
        str(saneado.get("filename")),
    )


# --------------------------------------------------------------------------- #
# (8) Tolerancia
# --------------------------------------------------------------------------- #
def test_tolerancia(check):
    print("(8) tolerancia (BD caida / tabla faltante / entradas raras)")

    class _SesionRota:
        def __call__(self):
            raise RuntimeError("bd caida")

    try:
        with mock.patch.object(database, "SessionLocal", _SesionRota()):
            r = historial.guardar_pedidos(
                [
                    {
                        "tipo": "likes",
                        "link": LINK_A,
                        "cantidad": 1,
                        "ok": True,
                    }
                ]
            )
            check(
                "tolerancia: BD caida al guardar -> ok False",
                r.get("ok") is False
                and r.get("guardados") == 0
                and bool(r.get("error")),
                str(r),
            )
            check(
                "tolerancia: BD caida -> pedidos_de_lote []",
                historial.pedidos_de_lote("LOTE-X") == [],
            )
            check(
                "tolerancia: BD caida -> listar_lotes []",
                historial.listar_lotes(5) == [],
            )
            check(
                "tolerancia: BD caida -> ultimo_lote no disponible",
                historial.ultimo_lote() == {"disponible": False},
            )
            reporte = historial.generar_reporte_lote("LOTE-X")
            check(
                "tolerancia: BD caida -> reporte ok False/vacio",
                reporte.get("ok") is False and reporte.get("contenido") == "",
                str(reporte),
            )
            check(
                "tolerancia: los generadores puros siguen funcionando",
                "Sin acciones registradas"
                in historial.construir_reporte_html(
                    [{"tipo": "likes", "cantidad": 1, "ok": True}]
                ),
            )
    finally:
        # Nada que restaurar: mock.patch revierte solo.
        pass

    # Tabla faltante -> init_db() UNA vez + reintento.
    engine = _engine_memoria()
    fabrica = _fabrica(engine)
    llamadas = []

    def _init_fake():
        llamadas.append(1)
        Base.metadata.create_all(engine)

    try:
        with mock.patch.object(database, "SessionLocal", fabrica), \
                mock.patch.object(database, "init_db", _init_fake):
            resultado = historial.guardar_pedidos(
                [
                    {
                        "tipo": "likes",
                        "link": LINK_A,
                        "cantidad": 2,
                        "ok": True,
                    }
                ],
                lote_id="LOTE-INIT-0001",
            )
            check(
                "tolerancia: tabla faltante -> init_db y reintenta",
                resultado.get("ok") is True
                and resultado.get("guardados") == 1
                and llamadas == [1],
                str(resultado),
            )
            check(
                "tolerancia: el reintento persistio la fila",
                len(historial.pedidos_de_lote("LOTE-INIT-0001")) == 1,
            )
    finally:
        engine.dispose()

    # init_db tambien falla -> error controlado, nunca excepcion.
    engine = _engine_memoria()
    fabrica = _fabrica(engine)
    llamadas = []

    def _init_roto():
        llamadas.append(1)
        raise RuntimeError("no se pudo crear el esquema")

    try:
        with mock.patch.object(database, "SessionLocal", fabrica), \
                mock.patch.object(database, "init_db", _init_roto):
            resultado = historial.guardar_pedidos(
                [
                    {
                        "tipo": "likes",
                        "link": LINK_A,
                        "cantidad": 2,
                        "ok": True,
                    }
                ],
                lote_id="LOTE-INIT-0002",
            )
            check(
                "tolerancia: init_db roto -> ok False sin excepcion",
                resultado.get("ok") is False
                and bool(resultado.get("error"))
                and llamadas == [1],
                str(resultado),
            )
    finally:
        engine.dispose()

    # Entradas raras en los generadores.
    check(
        "tolerancia: HTML con objeto raro -> valido",
        "Sin acciones registradas" in historial.construir_reporte_html(object()),
    )
    check(
        "tolerancia: CSV con objeto raro -> BOM y mensaje",
        historial.construir_reporte_csv(object())
        .lstrip("\ufeff")
        .startswith("Reporte de acciones realizadas"),
    )
    check(
        "tolerancia: reporte de lote con lista de no-dicts",
        "Sin acciones registradas"
        in historial.construir_reporte_html(["basura", None, 3]),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta todos los checks con el `check` del runner (o local)."""
    engine = _engine_memoria()
    Base.metadata.create_all(engine)
    fabrica = _fabrica(engine)
    original = database.SessionLocal
    database.SessionLocal = fabrica
    try:
        test_modelo(check)
        test_lote_id(check)
        pedidos_1, pedidos_2 = test_guardar(check, fabrica)
        test_listar(check, fabrica)
        test_resumen(check, pedidos_1, pedidos_2)
        test_html(check, pedidos_1, pedidos_2)
        test_csv(check, pedidos_1, pedidos_2)
        test_generar(check)
    finally:
        database.SessionLocal = original
        engine.dispose()
    test_tolerancia(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_jap_historial.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
