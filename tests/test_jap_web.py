"""Tests de la pagina "🛒 JAP: Comprar Engagement" (`web/operaciones/jap.py`).

Sin red real: todas las llamadas del backend `integraciones.jap` se parchean
con mocks (`unittest.mock`) dentro de los scripts de `AppTest`. Nunca se crea
una orden real ni se gasta saldo.

Cubre:
  (1) Registro en `web/app.py`: la operacion esta SOLO en `OPCIONES_ADMIN`
      (los operadores no deben gastar saldo), aparece en UNA categoria
      ("📊 Datos y cuentas"), hay branch de despacho `startswith("🛒")` y el
      modulo `web/operaciones/jap.py` existe. Las 17 operaciones base y las 4
      categorias quedan intactas.
  (2) Constantes y helpers puros: los 18 links exactos de @RedAMLOmx, los ids
      de comentarios 6601/6602, `_normalizar_links` (dedupe/numeracion/URLs
      invalidas), `_parsear_ids`, `_id_comentarios`, `_calcular_lote` y el
      plan de compra `_plan_pedidos_lote` (links/cantidades/seguidores/
      comentarios personalizados y errores de minimo/maximo).
  (3) AppTest con backend mockeado (key configurada, saldo $3.51): render sin
      excepciones, 3 pestanas, aviso visible de Mexico, metricas de saldo y
      servicios X, catalogo con la columna "Precio/1K USD", boton de los 18
      links que los carga en el textarea, tabla del cotizador con total > 0 y
      boton "Comprar lote" deshabilitado sin la confirmacion.
  (4) AppTest de compra del lote con `crear_orden` falso: sin confirmacion no
      llama a la API; con confirmacion crea los pedidos de los 2 links, deja
      el resumen persistente y limpia la confirmacion.
  (5) Persistencia del lote: `core.jap_historial` se importa de forma
      perezosa; `guardar_pedidos` recibe el folio, el cliente y la campana
      (mock), el resumen guarda `lote_id` y aparecen los botones de reporte.
  (6) Reportes: `generar_reporte_lote` se usa para el lote persistido,
      `construir_reporte_html/csv` para el reporte manual, y los archivos no
      mencionan al proveedor, costos, saldo, presets ni IDs de servicio.
  (7) AppTest del reporte manual con cantidades (sin llamar a `crear_orden`)
      y AppTest sin API key (balance/catalogo con error): render sin
      excepciones, instrucciones visibles y compra deshabilitada.

Uso:
    .venv/Scripts/python.exe tests/test_jap_web.py
"""
from __future__ import annotations

import ast
import contextlib
import re
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# La consola de Windows usa cp1252 y los nombres de check llevan acentos/emojis
# (igual que el resto de la suite). UTF-8 con `replace` evita el
# UnicodeEncodeError al correr el archivo suelto.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from integraciones import jap as jap_mod  # noqa: E402
from web.operaciones import jap as pagina  # noqa: E402

OP = "🛒 JAP: Comprar Engagement"

# Catalogo simulado con los ids verificados del cliente (X + otras redes).
FAKE_SERVICIOS = [
    {
        "service": 9013,
        "name": "Twitter Likes 7D",
        "category": "Twitter Likes",
        "type": "Default",
        "rate": "0.275",
        "min": "10",
        "max": "2500",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 10186,
        "name": "Twitter Likes Refill 15D",
        "category": "Twitter Likes",
        "type": "Default",
        "rate": "1.80",
        "min": "10",
        "max": "40000",
        "refill": True,
        "cancel": True,
    },
    {
        "service": 8860,
        "name": "Twitter Retweets",
        "category": "Twitter Retweets",
        "type": "Default",
        "rate": "0.125",
        "min": "10",
        "max": "5000",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 8861,
        "name": "Twitter Retweets Refill 30D",
        "category": "Twitter Retweets",
        "type": "Default",
        "rate": "0.50",
        "min": "10",
        "max": "5000",
        "refill": True,
        "cancel": True,
    },
    {
        "service": 8413,
        "name": "Twitter Tweet Views",
        "category": "Twitter Views",
        "type": "Default",
        "rate": "0.0025",
        "min": "5",
        "max": "1000000",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 8555,
        "name": "Twitter Tweet Views 365D",
        "category": "Twitter Views",
        "type": "Default",
        "rate": "0.0063",
        "min": "5",
        "max": "1000000",
        "refill": True,
        "cancel": True,
    },
    {
        "service": 6601,
        "name": "USA Random Comments",
        "category": "Twitter Comments",
        "type": "Custom Comments",
        "rate": "30.875",
        "min": "5",
        "max": "150",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 6602,
        "name": "USA Custom Comments",
        "category": "Twitter Comments",
        "type": "Custom Comments",
        "rate": "37.05",
        "min": "5",
        "max": "150",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 9011,
        "name": "Twitter Followers",
        "category": "Twitter Followers",
        "type": "Default",
        "rate": "0.375",
        "min": "10",
        "max": "100000",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 8855,
        "name": "USA Followers Refill 30D",
        "category": "Twitter Followers",
        "type": "Default",
        "rate": "0.85",
        "min": "10",
        "max": "100000",
        "refill": True,
        "cancel": True,
    },
    {
        "service": 3001,
        "name": "Instagram Likes",
        "category": "Instagram Likes",
        "type": "Default",
        "rate": "0.10",
        "min": "10",
        "max": "50000",
        "refill": False,
        "cancel": True,
    },
    {
        "service": 4001,
        "name": "YouTube Views",
        "category": "YouTube Views",
        "type": "Default",
        "rate": "0.05",
        "min": "100",
        "max": "100000",
        "refill": False,
        "cancel": True,
    },
]


# ---------------------------------------------------------------------------
# Backend falso (se inyecta a los scripts de AppTest por kwargs)
# ---------------------------------------------------------------------------
def _fake_listar(forzar=False):
    return {
        "ok": True,
        "servicios": [dict(s) for s in FAKE_SERVICIOS],
        "error": "",
        "desde_cache": False,
    }


def _fake_listar_sin_key(forzar=False):
    return {
        "ok": False,
        "servicios": [],
        "error": "Falta configurar la API key de JAP (JAP_API_KEY)",
    }


def _fake_balance():
    return {"ok": True, "balance": 3.51, "currency": "USD", "error": ""}


def _fake_balance_sin_key():
    return {
        "ok": False,
        "balance": 0.0,
        "currency": "",
        "error": "Falta configurar la API key de JAP (JAP_API_KEY)",
    }


def _fake_servicio(servicio_id):
    for servicio in FAKE_SERVICIOS:
        if str(servicio["service"]) == str(servicio_id):
            return servicio
    return None


def _fake_costo(servicio, cantidad):
    servicio_id = (
        servicio.get("service") if isinstance(servicio, dict) else servicio
    )
    info = _fake_servicio(servicio_id)
    if info is None:
        return {
            "ok": False,
            "servicio_id": servicio_id,
            "cantidad": 0,
            "rate": 0.0,
            "costo": 0.0,
            "min": 0,
            "max": 0,
            "error": f"No existe el servicio {servicio_id} en el catalogo de JAP",
        }
    try:
        unidades = int(cantidad)
    except (TypeError, ValueError):
        unidades = 0
    rate = float(info["rate"])
    return {
        "ok": unidades > 0,
        "servicio_id": int(info["service"]),
        "cantidad": unidades,
        "rate": rate,
        "costo": round(rate * max(unidades, 0) / 1000.0, 6),
        "moneda": "USD",
        "min": int(info["min"]),
        "max": int(info["max"]),
        "error": "" if unidades > 0 else "La cantidad debe ser mayor a 0",
    }


def _info_preset(servicio_id):
    info = _fake_servicio(servicio_id)
    return {
        "servicio": dict(info),
        "rate": float(info["rate"]),
        "min": int(info["min"]),
        "max": int(info["max"]),
        "nombre": info["name"],
    }


def _fake_recomendados():
    return {
        "ok": True,
        "error": "",
        "presets": {
            "economico": {
                "likes": _info_preset(9013),
                "retweets": _info_preset(8860),
                "views": _info_preset(8413),
                "comments": _info_preset(6601),
                "followers": _info_preset(9011),
            },
            "seguro": {
                "likes": _info_preset(10186),
                "retweets": _info_preset(8861),
                "views": _info_preset(8555),
                "comments": _info_preset(6602),
                "followers": _info_preset(8855),
            },
        },
    }


def _fake_recomendados_sin_key():
    return {"ok": False, "presets": {"economico": {}, "seguro": {}}, "error": ""}


def _sin_orden_real(*args, **kwargs):
    raise AssertionError("No se debe crear una orden real en los tests")


def _fake_guardar_key(key):
    return {"ok": True, "error": ""}


SERVICIOS_PLAN = {
    "likes": {"id": 9013, "nombre": "Twitter Likes 7D"},
    "retweets": {"id": 8860, "nombre": "Twitter Retweets"},
    "comments": {"id": 6602, "nombre": "USA Custom Comments"},
    "views": {"id": 8413, "nombre": "Twitter Tweet Views"},
    "followers": {"id": 9011, "nombre": "Twitter Followers"},
}

COMENTARIOS_5 = "\n".join(
    [f"Comentario numero {numero} con texto propio" for numero in range(1, 6)]
)


def _costo_con_minimos(servicio, cantidad):
    """`_fake_costo` + validacion real de min/max del catalogo simulado."""
    resultado = dict(_fake_costo(servicio, cantidad))
    info = _fake_servicio(
        servicio.get("service") if isinstance(servicio, dict) else servicio
    )
    try:
        unidades = int(cantidad)
    except (TypeError, ValueError):
        unidades = None
    if info is not None and unidades is not None:
        minimo = int(info["min"])
        maximo = int(info["max"])
        if 0 < unidades < minimo:
            resultado.update(
                ok=False,
                costo=0.0,
                error=f"La cantidad minima para este servicio es {minimo}",
            )
        elif maximo > 0 and unidades > maximo:
            resultado.update(
                ok=False,
                costo=0.0,
                error=f"La cantidad maxima para este servicio es {maximo}",
            )
    return resultado


# ---------------------------------------------------------------------------
# Historial falso (`core.jap_historial`): nunca toca la BD real.
# ---------------------------------------------------------------------------
class FakeHistorial:
    def __init__(self, lote_id="LOTE-TEST-001"):
        self.lote_id = lote_id
        self.lotes = {}
        self.guardados = []
        self.generados = []
        self.construidos = []

    def nuevo_lote_id(self):
        return self.lote_id

    def guardar_pedidos(
        self, pedidos, lote_id="", cliente="", campana="", notas=""
    ):
        copia = [dict(pedido) for pedido in (pedidos or [])]
        folio = str(lote_id or self.lote_id)
        self.guardados.append(
            {
                "pedidos": copia,
                "lote_id": folio,
                "cliente": cliente,
                "campana": campana,
                "notas": notas,
            }
        )
        self.lotes[folio] = copia
        return {
            "ok": True,
            "lote_id": folio,
            "guardados": len(copia),
            "error": "",
        }

    def pedidos_de_lote(self, lote_id):
        return [dict(pedido) for pedido in self.lotes.get(lote_id, [])]

    def listar_lotes(self, limite=10):
        filas = []
        for indice, (folio, pedidos) in enumerate(
            list(self.lotes.items())[: max(int(limite or 0), 0)]
        ):
            guardado = (
                self.guardados[indice] if indice < len(self.guardados) else {}
            )
            links = {
                pedido.get("link")
                for pedido in pedidos
                if pedido.get("link")
            }
            filas.append(
                {
                    "lote_id": folio,
                    "fecha": "2026-11-27T10:00:00",
                    "cliente": guardado.get("cliente") or "",
                    "campana": guardado.get("campana") or "",
                    "pedidos": len(pedidos),
                    "acciones": len(pedidos),
                    "links": len(links),
                }
            )
        return filas

    def generar_reporte_lote(self, lote_id, formato="html"):
        self.generados.append((lote_id, formato))
        if formato == "csv":
            contenido = f"Tipo,Publicación,Cantidad\nLote,{lote_id},1\n"
        else:
            contenido = (
                "<!DOCTYPE html><html><body><h1>MW Group</h1>"
                f"<p>Reporte {lote_id}</p></body></html>"
            )
        return {
            "ok": True,
            "contenido": contenido,
            "filename": f"reporte-{lote_id}.{formato}",
            "error": "",
        }

    def construir_reporte_html(
        self,
        pedidos,
        fecha="",
        cliente="",
        campana="",
        folio="",
        notas="",
    ):
        self.construidos.append(
            ("html", [dict(p) for p in pedidos], cliente, campana, folio)
        )
        return (
            "<!DOCTYPE html><html><body><h1>MW Group</h1>"
            f"<p>{folio} · {cliente} · {campana}</p>"
            f"<p>{len(pedidos)} acciones</p></body></html>"
        )

    def construir_reporte_csv(
        self,
        pedidos,
        fecha="",
        cliente="",
        campana="",
        folio="",
        notas="",
    ):
        self.construidos.append(
            ("csv", [dict(p) for p in pedidos], cliente, campana, folio)
        )
        return (
            "Tipo,Publicación,Cantidad\n"
            f"Lote,{folio},{len(pedidos)}\n"
        )


class FakeHistorialFallido(FakeHistorial):
    """Historial que falla al guardar: la compra debe continuar igual."""

    def guardar_pedidos(
        self, pedidos, lote_id="", cliente="", campana="", notas=""
    ):
        return {
            "ok": False,
            "lote_id": str(lote_id or ""),
            "guardados": 0,
            "error": "Base no disponible",
        }


@contextlib.contextmanager
def _historial_fake(fake):
    """Inyecta `core.jap_historial` sin importar el modulo real."""
    import core

    existia = hasattr(core, "jap_historial")
    anterior = getattr(core, "jap_historial", None)
    with mock.patch.dict(sys.modules, {"core.jap_historial": fake}):
        setattr(core, "jap_historial", fake)
        try:
            yield fake
        finally:
            if existia:
                setattr(core, "jap_historial", anterior)
            else:
                try:
                    delattr(core, "jap_historial")
                except AttributeError:
                    pass
                sys.modules.pop("core.jap_historial", None)


def _historial_real():
    """Modulo real si esta disponible (sin efectos secundarios)."""
    try:
        from core import jap_historial as real

        return real
    except Exception:
        return None


def _reporte_manual_real_o_local(pedidos):
    """(html, csv, origen) con el generador real si existe, si no el local."""
    real = _historial_real()
    if real is not None:
        html_fn = getattr(real, "construir_reporte_html", None)
        csv_fn = getattr(real, "construir_reporte_csv", None)
        if callable(html_fn) and callable(csv_fn):
            try:
                html = html_fn(
                    pedidos,
                    fecha="2026-11-27",
                    cliente="Cliente Demo",
                    campana="Campaña Demo",
                    folio="LOTE-DEMO",
                )
                csv_texto = csv_fn(
                    pedidos,
                    fecha="2026-11-27",
                    cliente="Cliente Demo",
                    campana="Campaña Demo",
                    folio="LOTE-DEMO",
                )
                if isinstance(html, bytes):
                    html = html.decode("utf-8", errors="replace")
                if isinstance(csv_texto, bytes):
                    csv_texto = csv_texto.decode("utf-8", errors="replace")
                if str(html or "").strip() and str(csv_texto or "").strip():
                    return str(html), str(csv_texto), "core.jap_historial"
            except Exception:
                pass
    html = pagina._reporte_html_local(
        pedidos,
        fecha="2026-11-27",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio="LOTE-DEMO",
    )
    csv_texto = pagina._reporte_csv_local(
        pedidos,
        fecha="2026-11-27",
        cliente="Cliente Demo",
        campana="Campaña Demo",
        folio="LOTE-DEMO",
    )
    return html, csv_texto, "local"


class FakeGoogleSheets:
    """Backend falso de `core.google_sheets` (sin red ni credenciales)."""

    def __init__(self, disponible=True, email="reportes@mw-test.iam.gserviceaccount.com"):
        self._disponible = bool(disponible)
        self.email = email if self._disponible else ""
        self.estado_llamadas = 0
        self.crear_llamadas = []
        self.guardados = []
        self.error_guardar = ""
        self.crear_ok = True
        self.url = "https://docs.google.com/spreadsheets/d/SHEET-TEST-123/edit"
        self.titulo = "Reporte de acciones realizadas"

    def estado(self):
        self.estado_llamadas += 1
        return {
            "disponible": self._disponible,
            "motivo": "" if self._disponible else "Sin credenciales configuradas",
            "fuente": "env" if self._disponible else "",
            "email": self.email if self._disponible else "",
        }

    def disponible(self):
        return self._disponible

    def guardar_credenciales_json(self, contenido):
        self.guardados.append(contenido)
        if self.error_guardar:
            return {"ok": False, "email": "", "error": self.error_guardar}
        self._disponible = True
        self.email = "reportes@mw-test.iam.gserviceaccount.com"
        return {"ok": True, "email": self.email, "error": ""}

    def crear_sheet_reporte(
        self,
        pedidos,
        cliente="",
        campana="",
        fecha="",
        folio="",
        notas="",
        compartir_publico=True,
        email="",
    ):
        self.crear_llamadas.append(
            {
                "pedidos": [dict(p) for p in (pedidos or [])],
                "cliente": cliente,
                "campana": campana,
                "fecha": fecha,
                "folio": folio,
                "compartir_publico": compartir_publico,
                "email": email,
            }
        )
        if not self.crear_ok:
            return {
                "ok": False,
                "spreadsheet_id": "",
                "url": "",
                "titulo": "",
                "compartido": False,
                "error": "Sin permisos de Google",
            }
        return {
            "ok": True,
            "spreadsheet_id": "SHEET-TEST-123",
            "url": self.url,
            "titulo": self.titulo,
            "compartido": True,
            "error": "",
        }


@contextlib.contextmanager
def _sheets_fake(fake):
    """Inyecta `core.google_sheets` en la pagina (None = modulo ausente)."""
    with mock.patch.object(pagina, "_sheets", lambda: fake):
        yield fake


JSON_CREDENCIALES = (
    '{"type": "service_account", "project_id": "mw-test", '
    '"private_key_id": "abc123secreto", "private_key": '
    '"-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkq\\n'
    '-----END PRIVATE KEY-----\\n", '
    '"client_email": "reportes@mw-test.iam.gserviceaccount.com"}'
)


def _textos_ui(at) -> str:
    """Todo el texto visible de la pagina (para buscar fugas del JSON)."""
    partes = []
    for tipo in (
        "markdown",
        "caption",
        "success",
        "info",
        "warning",
        "error",
        "code",
    ):
        try:
            elementos = at.get(tipo)
        except Exception:
            continue
        for elemento in elementos:
            try:
                valor = getattr(elemento, "value", "")
                if isinstance(valor, str):
                    partes.append(valor)
            except Exception:
                continue
    for entrada in at.text_input:
        try:
            partes.append(str(entrada.value))
        except Exception:
            continue
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# Scripts de AppTest (SOLO ASCII: Streamlit los escribe con la codificacion
# local de Windows). Reciben los fakes por kwargs.
# ---------------------------------------------------------------------------
def _app_jap_con_key(listar, balance, costo, recomendados, crear, guardar):
    from unittest import mock

    from integraciones import jap

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "calcular_costo", costo
    ), mock.patch.object(
        jap, "crear_orden", crear
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: True
    ), mock.patch.object(
        jap, "guardar_api_key", guardar
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_sin_key(listar, balance, recomendados):
    from unittest import mock

    from integraciones import jap

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "disponible", lambda: False
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_reporte_manual(listar, balance, recomendados, crear):
    from unittest import mock

    from integraciones import jap

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "crear_orden", crear
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: False
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_compra_lote(listar, balance, costo, recomendados, registro):
    from unittest import mock

    from integraciones import jap

    def _crear(
        servicio,
        link,
        cantidad,
        runs=0,
        interval=0,
        comments="",
        **kwargs,
    ):
        calculo = costo(servicio, cantidad)
        registro.append(
            {
                "servicio": servicio,
                "link": link,
                "cantidad": int(cantidad),
                "comments": str(comments or ""),
            }
        )
        return {
            "ok": True,
            "order": 9000 + len(registro),
            "costo": calculo.get("costo") or 0.0,
            "error": "",
        }

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "calcular_costo", costo
    ), mock.patch.object(
        jap, "crear_orden", _crear
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: True
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_sheets_ultimo(listar, balance, recomendados, ordenes):
    from unittest import mock

    from integraciones import jap

    def _crear(*args, **kwargs):
        ordenes.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "order": 9001, "costo": 0.0, "error": ""}

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "crear_orden", _crear
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: True
    ):
        import streamlit as st

        st.session_state["jap_lote_resultados"] = {
            "fecha": "2026-11-27T12:00:00",
            "total_gastado": 0.5,
            "exitosos": 2,
            "fallidos": 0,
            "pedidos": [
                {
                    "order": 9001,
                    "tipo": "likes",
                    "servicio": 9013,
                    "servicio_nombre": "Twitter Likes 7D",
                    "link": "https://x.com/a/status/1",
                    "cantidad": 50,
                    "costo": 0.25,
                    "ok": True,
                    "error": "",
                },
                {
                    "order": 9002,
                    "tipo": "retweets",
                    "servicio": 8860,
                    "servicio_nombre": "Twitter Retweets",
                    "link": "https://x.com/a/status/2",
                    "cantidad": 10,
                    "costo": 0.25,
                    "ok": True,
                    "error": "",
                },
            ],
            "lote_id": "LOTE-SHEET-001",
            "cliente": "Cliente Demo",
            "campana": "Campana Demo",
            "historial_ok": False,
            "historial_error": "",
        }
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_sheets_lote(listar, balance, recomendados, ordenes):
    from unittest import mock

    from integraciones import jap

    def _crear(*args, **kwargs):
        ordenes.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "order": 9001, "costo": 0.0, "error": ""}

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "crear_orden", _crear
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: False
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


def _app_jap_sheets_credenciales(listar, balance, recomendados):
    from unittest import mock

    from integraciones import jap

    with mock.patch.object(jap, "listar_servicios", listar), mock.patch.object(
        jap, "obtener_balance", balance
    ), mock.patch.object(
        jap, "servicios_recomendados", recomendados
    ), mock.patch.object(
        jap, "estado_ordenes", lambda ids: {"ok": True, "ordenes": {}, "error": ""}
    ), mock.patch.object(
        jap, "disponible", lambda: False
    ):
        from web.operaciones.jap import render

        render({"rol": "admin", "nombre": "Owner", "usuario": "admin"})


# ---------------------------------------------------------------------------
# Helpers de los checks
# ---------------------------------------------------------------------------
def _fuente_app() -> str:
    return (RAIZ / "web" / "app.py").read_text(encoding="utf-8")


def _constantes_app() -> dict:
    """Constantes literales de `web/app.py` con AST (sin ejecutar main())."""
    arbol = ast.parse(_fuente_app())
    constantes = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                if isinstance(objetivo, ast.Name):
                    try:
                        constantes[objetivo.id] = ast.literal_eval(nodo.value)
                    except Exception:
                        pass
    return constantes


def _prefijos_despacho() -> set:
    arbol = ast.parse(_fuente_app())
    prefijos = set()
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "startswith"
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            prefijos.add(nodo.args[0].value)
    return prefijos


def _modulos_importados() -> set:
    arbol = ast.parse(_fuente_app())
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            if nodo.module.startswith("web.operaciones."):
                modulos.add(nodo.module.rsplit(".", 1)[-1])
    return modulos


def _tabla_con_columnas(at, columnas):
    for tabla in at.dataframe:
        try:
            actuales = [str(c) for c in tabla.value.columns]
        except Exception:
            continue
        if all(columna in actuales for columna in columnas):
            return tabla.value
    return None


def _minimos_number_inputs(fuente: str) -> dict:
    """Mapa `key -> min_value` de los `st.number_input` de la pagina (AST)."""
    minimos = {}
    try:
        arbol = ast.parse(fuente)
    except SyntaxError:
        return minimos
    for nodo in ast.walk(arbol):
        if not (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "number_input"
        ):
            continue
        clave = None
        minimo = None
        for palabra in nodo.keywords:
            if palabra.arg == "key" and isinstance(palabra.value, ast.Constant):
                clave = str(palabra.value.value)
            elif (
                palabra.arg == "min_value"
                and isinstance(palabra.value, ast.Constant)
            ):
                minimo = palabra.value.value
        if clave:
            minimos[clave] = minimo
    return minimos


def _metricas(at) -> dict:
    return {str(m.label): str(m.value) for m in at.metric}


def _download_keys(at) -> list:
    """Keys de los `st.download_button` renderizados (via proto.id)."""
    claves = []
    for elemento in at.get("download_button"):
        try:
            identificador = str(elemento.proto.id)
            claves.append(identificador.rsplit("-", 1)[-1])
        except Exception:
            continue
    return claves


def _tiene_terminos_internos(texto) -> bool:
    """True si el reporte menciona al proveedor, costos o datos internos."""
    bajo = str(texto or "").lower()
    terminos = (
        "jap",
        "just another panel",
        "proveedor",
        "costo",
        "saldo",
        "preset",
        "compra",
        "ids de servicio",
        "id de servicio",
    )
    if any(termino in bajo for termino in terminos):
        return True
    return bool(re.search(r"\bapi\b", bajo))


def _app_test_con_key() -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "tabs": [],
        "aviso_mexico": False,
        "saldo": "",
        "servicios_x": "",
        "boton_18": False,
        "password": False,
        "catalogo": False,
        "catalogo_x": False,
        "links_cargados": 0,
        "tabla_cotizador": False,
        "total_lote": 0.0,
        "seccion_compra": False,
        "confirmo": False,
        "compra_deshabilitada": False,
        "estados": False,
        "cliente_input": False,
        "campana_input": False,
        "manual_expander": False,
    }
    try:
        at = AppTest.from_function(
            _app_jap_con_key,
            default_timeout=120,
            kwargs={
                "listar": _fake_listar,
                "balance": _fake_balance,
                "costo": _fake_costo,
                "recomendados": _fake_recomendados,
                "crear": _sin_orden_real,
                "guardar": _fake_guardar_key,
            },
        )
        at.run()
        if at.exception:
            return False, str(at.exception[0].value)[:300], resultado
        resultado["sin_excepciones"] = True
        resultado["tabs"] = [str(t.label) for t in at.tabs]
        resultado["aviso_mexico"] = any(
            "México" in str(w.value) for w in at.warning
        )
        metricas = _metricas(at)
        resultado["saldo"] = metricas.get("Saldo actual (USD)", "")
        resultado["servicios_x"] = metricas.get("Servicios de X", "")
        claves_botones = {b.key for b in at.button}
        resultado["boton_18"] = "jap_btn_cargar_18" in claves_botones
        try:
            from streamlit.proto.TextInput_pb2 import TextInput

            entrada_key = at.text_input(key="jap_api_key_input")
            resultado["password"] = (
                entrada_key.proto.type == TextInput.PASSWORD
            )
        except Exception:
            resultado["password"] = False

        tabla_catalogo = _tabla_con_columnas(at, ["Precio/1K USD", "Servicio"])
        if tabla_catalogo is not None:
            resultado["catalogo"] = True
            try:
                nombres = [str(v) for v in tabla_catalogo["Servicio"].tolist()]
                resultado["catalogo_x"] = any(
                    "Twitter Likes" in nombre for nombre in nombres
                )
            except Exception:
                resultado["catalogo_x"] = False

        # Cargar los 18 links (callback) y recalcular el cotizador.
        at.button(key="jap_btn_cargar_18").click().run()
        if at.exception:
            return False, str(at.exception[0].value)[:300], resultado
        lineas = [
            linea
            for linea in at.text_area(key="jap_quote_links").value.splitlines()
            if linea.strip()
        ]
        resultado["links_cargados"] = len(lineas)
        tabla_cotizador = _tabla_con_columnas(
            at, ["Likes $", "RT $", "Comentarios $", "Vistas $", "Total $"]
        )
        if tabla_cotizador is not None:
            resultado["tabla_cotizador"] = True
            try:
                resultado["total_lote"] = float(tabla_cotizador["Total $"].sum())
            except Exception:
                resultado["total_lote"] = 0.0
        resultado["seccion_compra"] = any(
            "pedido(s) · total estimado" in str(m.value) for m in at.markdown
        )
        try:
            resultado["confirmo"] = at.checkbox(
                key="jap_lote_confirmo"
            ) is not None
        except Exception:
            resultado["confirmo"] = False
        try:
            boton_lote = at.button(key="jap_btn_comprar_lote")
            resultado["compra_deshabilitada"] = bool(boton_lote.disabled)
        except Exception:
            resultado["compra_deshabilitada"] = False
        resultado["estados"] = any(
            t.key == "jap_btn_consultar" for t in at.button
        )
        claves_inputs = {t.key for t in at.text_input}
        resultado["cliente_input"] = "jap_lote_cliente" in claves_inputs
        resultado["campana_input"] = "jap_lote_campana" in claves_inputs
        resultado["manual_expander"] = any(
            "Reporte manual" in str(expander.label) for expander in at.expander
        )
        return True, "", resultado
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], resultado


def _app_test_sin_key() -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "instrucciones": False,
        "compra_deshabilitada": False,
        "key_no_configurada": False,
    }
    try:
        at = AppTest.from_function(
            _app_jap_sin_key,
            default_timeout=120,
            kwargs={
                "listar": _fake_listar_sin_key,
                "balance": _fake_balance_sin_key,
                "recomendados": _fake_recomendados_sin_key,
            },
        )
        at.run()
        if at.exception:
            return False, str(at.exception[0].value)[:300], resultado
        resultado["sin_excepciones"] = True
        texto_info = " ".join(str(info.value) for info in at.info)
        resultado["instrucciones"] = (
            "API key" in texto_info and "deshabilitadas" in texto_info
        )
        # Sin links/API key no se puede comprar: el botón debe estar
        # deshabilitado (o ausente en versiones viejas de la pagina).
        botones = {b.key: b for b in at.button}
        boton_compra = botones.get("jap_btn_comprar_lote")
        resultado["compra_deshabilitada"] = (
            boton_compra is None or bool(boton_compra.disabled)
        )
        metricas = _metricas(at)
        resultado["key_no_configurada"] = "No configurada" in metricas.get(
            "API key de JAP", ""
        )
        return True, "", resultado
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], resultado


def _app_test_compra_lote(fake=None) -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    registro = []
    fake = fake if fake is not None else FakeHistorial()
    resultado = {
        "sin_excepciones": False,
        "boton_existe": False,
        "deshabilitado_sin_confirmar": False,
        "llamadas_sin_confirmar": 0,
        "habilitado_confirmado": False,
        "llamadas": 0,
        "servicios": [],
        "links": [],
        "cantidades": [],
        "expander_resultados": False,
        "confirmacion_limpia": False,
        "pedidos_recientes": 0,
        "lote_id": "",
        "cliente": "",
        "campana": "",
        "historial_ok": False,
        "guardados": [],
        "reportes_lote": [],
        "download_keys": [],
        "persisted_table": False,
        "selector_lote": False,
        "warnings": [],
    }
    try:
        with _historial_fake(fake):
            at = AppTest.from_function(
                _app_jap_compra_lote,
                default_timeout=120,
                kwargs={
                    "listar": _fake_listar,
                    "balance": _fake_balance,
                    "costo": _fake_costo,
                    "recomendados": _fake_recomendados,
                    "registro": registro,
                },
            )
            at.run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], resultado

            at.text_area(key="jap_quote_links").set_value(
                "https://x.com/a/status/1\nhttps://x.com/a/status/2"
            ).run()
            at.number_input(key="jap_quote_likes").set_value(10).run()
            at.number_input(key="jap_quote_rts").set_value(0).run()
            at.number_input(key="jap_quote_comments").set_value(0).run()
            at.number_input(key="jap_quote_views").set_value(0).run()
            at.text_input(key="jap_lote_cliente").set_value(
                "Cliente Demo"
            ).run()
            at.text_input(key="jap_lote_campana").set_value(
                "Campaña Demo"
            ).run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], resultado

            try:
                boton = at.button(key="jap_btn_comprar_lote")
            except Exception:
                boton = None
            resultado["boton_existe"] = boton is not None
            resultado["deshabilitado_sin_confirmar"] = bool(
                boton is not None and boton.disabled
            )
            resultado["llamadas_sin_confirmar"] = len(registro)

            at.checkbox(key="jap_lote_confirmo").check().run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], resultado
            boton = at.button(key="jap_btn_comprar_lote")
            resultado["habilitado_confirmado"] = not bool(boton.disabled)
            boton.click().run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], resultado

            resultado["llamadas"] = len(registro)
            resultado["servicios"] = [
                llamada["servicio"] for llamada in registro
            ]
            resultado["links"] = [llamada["link"] for llamada in registro]
            resultado["cantidades"] = [
                llamada["cantidad"] for llamada in registro
            ]
            resultado["expander_resultados"] = any(
                "Último pedido del lote" in str(expander.label)
                for expander in at.expander
            )
            try:
                resultado["confirmacion_limpia"] = not bool(
                    at.checkbox(key="jap_lote_confirmo").value
                )
            except Exception:
                resultado["confirmacion_limpia"] = False
            try:
                recientes = at.session_state["jap_pedidos_recientes"]
                resultado["pedidos_recientes"] = len(recientes or [])
            except Exception:
                resultado["pedidos_recientes"] = 0
            try:
                lote = at.session_state["jap_lote_resultados"]
            except Exception:
                lote = {}
            if isinstance(lote, dict):
                resultado["lote_id"] = str(lote.get("lote_id") or "")
                resultado["cliente"] = str(lote.get("cliente") or "")
                resultado["campana"] = str(lote.get("campana") or "")
                resultado["historial_ok"] = bool(lote.get("historial_ok"))
            resultado["guardados"] = list(fake.guardados)
            resultado["reportes_lote"] = list(fake.generados)
            resultado["download_keys"] = _download_keys(at)
            resultado["persisted_table"] = (
                _tabla_con_columnas(
                    at,
                    [
                        "Folio",
                        "Fecha",
                        "Cliente",
                        "Campaña",
                        "Pedidos",
                        "Acciones",
                        "Links",
                    ],
                )
                is not None
            )
            try:
                at.selectbox(key="jap_reporte_lote_sel")
                resultado["selector_lote"] = True
            except Exception:
                resultado["selector_lote"] = False
            resultado["warnings"] = [str(w.value) for w in at.warning]
            resultado["sin_excepciones"] = True
            return True, "", resultado
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], resultado


def _app_test_reporte_manual() -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "download_keys": [],
        "html": False,
        "csv": False,
        "caption_sin_proveedor": False,
    }
    try:
        at = AppTest.from_function(
            _app_jap_reporte_manual,
            default_timeout=120,
            kwargs={
                "listar": _fake_listar_sin_key,
                "balance": _fake_balance_sin_key,
                "recomendados": _fake_recomendados_sin_key,
                "crear": _sin_orden_real,
            },
        )
        at.run()
        if at.exception:
            return False, str(at.exception[0].value)[:300], resultado
        at.text_area(key="jap_manual_links").set_value(
            "https://x.com/a/status/1\nhttps://x.com/a/status/2"
        ).run()
        at.number_input(key="jap_manual_likes").set_value(50).run()
        at.number_input(key="jap_manual_rts").set_value(0).run()
        at.number_input(key="jap_manual_comments").set_value(5).run()
        at.number_input(key="jap_manual_views").set_value(0).run()
        at.number_input(key="jap_manual_followers").set_value(0).run()
        at.text_input(key="jap_manual_cliente").set_value("Cliente Demo").run()
        at.text_input(key="jap_manual_campana").set_value("Campaña Demo").run()
        if at.exception:
            return False, str(at.exception[0].value)[:300], resultado
        resultado["download_keys"] = _download_keys(at)
        resultado["html"] = "jap_dl_manual_html" in resultado["download_keys"]
        resultado["csv"] = "jap_dl_manual_csv" in resultado["download_keys"]
        resultado["caption_sin_proveedor"] = any(
            "no incluye proveedor" in str(caption.value)
            for caption in at.caption
        )
        resultado["sin_excepciones"] = True
        return True, "", resultado
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], resultado


def _app_test_sheet_ultimo(fake, click=True) -> tuple[bool, str, dict]:
    """AppTest del boton de Google Sheet del ultimo lote (fake inyectado)."""
    from streamlit.testing.v1 import AppTest

    ordenes = []
    datos = {
        "sin_excepciones": False,
        "expander": False,
        "instrucciones": False,
        "boton_existe": False,
        "llamadas": [],
        "codes": [],
        "success": [],
        "captions": [],
        "ordenes": [],
        "bloqueo_sin_correo": False,
    }
    try:
        with _sheets_fake(fake):
            at = AppTest.from_function(
                _app_jap_sheets_ultimo,
                default_timeout=120,
                kwargs={
                    "listar": _fake_listar,
                    "balance": _fake_balance,
                    "recomendados": _fake_recomendados,
                    "ordenes": ordenes,
                },
            )
            at.run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            datos["expander"] = any(
                "Conectar Google Sheets" in str(expander.label)
                for expander in at.expander
            )
            textos = _textos_ui(at)
            datos["instrucciones"] = all(
                termino in textos
                for termino in (
                    "Google Sheets API",
                    "Google Drive API",
                    "GOOGLE_SERVICE_ACCOUNT_B64",
                    "data/google_service_account.json",
                )
            )
            clave_boton = pagina._clave_sheet("jap_lote_actual") + "_btn"
            datos["boton_existe"] = any(
                blabel.key == clave_boton for blabel in at.button
            )
            if click and datos["boton_existe"]:
                at.button(key=clave_boton).click().run()
                if at.exception:
                    return False, str(at.exception[0].value)[:300], datos
                clave_publico = (
                    pagina._clave_sheet("jap_lote_actual") + "_publico"
                )
                try:
                    at.checkbox(key=clave_publico).uncheck().run()
                    boton_privado = at.button(key=clave_boton)
                    datos["bloqueo_sin_correo"] = bool(
                        boton_privado.disabled
                    ) and any(
                        "Indica un correo" in str(elemento.value)
                        for elemento in at.error
                    )
                except Exception:
                    datos["bloqueo_sin_correo"] = False
            datos["llamadas"] = list(
                getattr(fake, "crear_llamadas", None) or []
            )
            datos["codes"] = [str(codigo.value) for codigo in at.code]
            datos["success"] = [str(s.value) for s in at.success]
            datos["captions"] = [str(c.value) for c in at.caption]
            datos["ordenes"] = list(ordenes)
            datos["sin_excepciones"] = True
            return True, "", datos
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], datos


def _app_test_sheet_lote(fake) -> tuple[bool, str, dict]:
    """AppTest del boton de Google Sheet de un lote persistido."""
    from streamlit.testing.v1 import AppTest

    ordenes = []
    datos = {
        "sin_excepciones": False,
        "boton_existe": False,
        "llamadas": [],
        "codes": [],
        "success": [],
        "ordenes": [],
    }
    pedidos = [
        {
            "order": 9101,
            "tipo": "likes",
            "servicio": 9013,
            "servicio_nombre": "Twitter Likes 7D",
            "link": "https://x.com/b/status/1",
            "cantidad": 25,
            "costo": 0.1,
            "ok": True,
            "error": "",
        },
        {
            "order": 9102,
            "tipo": "comments",
            "servicio": 6602,
            "servicio_nombre": "USA Custom Comments",
            "link": "https://x.com/b/status/2",
            "cantidad": 5,
            "costo": 0.2,
            "ok": True,
            "error": "",
        },
    ]
    historial = FakeHistorial("LOTE-SHEET-002")
    historial.lotes["LOTE-SHEET-002"] = pedidos
    historial.guardados = [
        {
            "pedidos": pedidos,
            "lote_id": "LOTE-SHEET-002",
            "cliente": "Cliente Lote",
            "campana": "Campana Lote",
            "notas": "",
        }
    ]
    try:
        with _sheets_fake(fake), _historial_fake(historial):
            at = AppTest.from_function(
                _app_jap_sheets_lote,
                default_timeout=120,
                kwargs={
                    "listar": _fake_listar_sin_key,
                    "balance": _fake_balance_sin_key,
                    "recomendados": _fake_recomendados_sin_key,
                    "ordenes": ordenes,
                },
            )
            at.run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            clave_boton = (
                pagina._clave_sheet("jap_lote_LOTE-SHEET-002") + "_btn"
            )
            datos["boton_existe"] = any(
                blabel.key == clave_boton for blabel in at.button
            )
            if datos["boton_existe"]:
                at.button(key=clave_boton).click().run()
                if at.exception:
                    return False, str(at.exception[0].value)[:300], datos
            datos["llamadas"] = list(fake.crear_llamadas)
            datos["codes"] = [str(codigo.value) for codigo in at.code]
            datos["success"] = [str(s.value) for s in at.success]
            datos["ordenes"] = list(ordenes)
            datos["sin_excepciones"] = True
            return True, "", datos
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], datos


def _app_test_sheet_manual(fake) -> tuple[bool, str, dict]:
    """AppTest del boton de Google Sheet del reporte manual."""
    from streamlit.testing.v1 import AppTest

    ordenes = []

    def _crear(*args, **kwargs):
        ordenes.append({"args": args, "kwargs": kwargs})
        return {"ok": False, "error": "no deberia crear ordenes"}

    datos = {
        "sin_excepciones": False,
        "boton_existe": False,
        "llamadas": [],
        "codes": [],
        "success": [],
        "ordenes": [],
    }
    try:
        with _sheets_fake(fake):
            at = AppTest.from_function(
                _app_jap_reporte_manual,
                default_timeout=120,
                kwargs={
                    "listar": _fake_listar_sin_key,
                    "balance": _fake_balance_sin_key,
                    "recomendados": _fake_recomendados_sin_key,
                    "crear": _crear,
                },
            )
            at.run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            at.text_area(key="jap_manual_links").set_value(
                "https://x.com/a/status/1\nhttps://x.com/a/status/2"
            ).run()
            at.number_input(key="jap_manual_likes").set_value(50).run()
            at.number_input(key="jap_manual_rts").set_value(0).run()
            at.number_input(key="jap_manual_comments").set_value(5).run()
            at.number_input(key="jap_manual_views").set_value(0).run()
            at.number_input(key="jap_manual_followers").set_value(0).run()
            at.text_input(key="jap_manual_cliente").set_value(
                "Cliente Manual"
            ).run()
            at.text_input(key="jap_manual_campana").set_value(
                "Campana Manual"
            ).run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            clave_boton = pagina._clave_sheet("jap_manual") + "_btn"
            datos["boton_existe"] = any(
                blabel.key == clave_boton for blabel in at.button
            )
            if datos["boton_existe"]:
                at.button(key=clave_boton).click().run()
                if at.exception:
                    return False, str(at.exception[0].value)[:300], datos
            datos["llamadas"] = list(fake.crear_llamadas)
            datos["codes"] = [str(codigo.value) for codigo in at.code]
            datos["success"] = [str(s.value) for s in at.success]
            datos["ordenes"] = list(ordenes)
            datos["sin_excepciones"] = True
            return True, "", datos
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], datos


def _app_test_sheets_credenciales(fake) -> tuple[bool, str, dict]:
    """AppTest del expander de conexion y del guardado de credenciales."""
    from streamlit.testing.v1 import AppTest

    datos = {
        "sin_excepciones": False,
        "expander": False,
        "password": False,
        "guardados": [],
        "valor_input": "",
        "fuga_json": False,
        "exito": False,
        "email_visible": False,
        "prueba_ok": False,
        "crear_llamadas": 0,
        "session_gs": {},
    }
    try:
        with _sheets_fake(fake):
            at = AppTest.from_function(
                _app_jap_sheets_credenciales,
                default_timeout=120,
                kwargs={
                    "listar": _fake_listar_sin_key,
                    "balance": _fake_balance_sin_key,
                    "recomendados": _fake_recomendados_sin_key,
                },
            )
            at.run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            datos["expander"] = any(
                "Conectar Google Sheets" in str(expander.label)
                for expander in at.expander
            )
            entrada = at.text_input(key="jap_gs_json_input")
            try:
                from streamlit.proto.TextInput_pb2 import TextInput

                datos["password"] = entrada.proto.type == TextInput.PASSWORD
            except Exception:
                datos["password"] = False
            at.text_input(key="jap_gs_json_input").set_value(
                JSON_CREDENCIALES
            ).run()
            at.button(key="jap_gs_btn_guardar").click().run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            datos["guardados"] = list(fake.guardados)
            datos["valor_input"] = str(
                at.text_input(key="jap_gs_json_input").value or ""
            )
            textos = _textos_ui(at)
            datos["fuga_json"] = (
                "private_key" in textos.lower()
                or "abc123secreto" in textos
            )
            datos["exito"] = any(
                "conectado" in str(s.value).lower()
                or "credenciales guardadas" in str(s.value).lower()
                for s in at.success
            )
            datos["email_visible"] = any(
                "reportes@mw-test.iam.gserviceaccount.com" in str(s.value)
                for s in at.success
            )
            try:
                datos["session_gs"] = {
                    str(clave_estado): str(
                        at.session_state[clave_estado]
                    )
                    for clave_estado in at.session_state.filtered_state
                    if "jap_gs" in str(clave_estado)
                }
            except Exception:
                datos["session_gs"] = {}
            at.button(key="jap_gs_btn_probar").click().run()
            if at.exception:
                return False, str(at.exception[0].value)[:300], datos
            datos["prueba_ok"] = any(
                "Conexión con Google Sheets OK" in str(s.value)
                for s in at.success
            )
            datos["crear_llamadas"] = len(fake.crear_llamadas)
            datos["sin_excepciones"] = True
            return True, "", datos
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300], datos


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def run(check):
    # ---------------- (1) Registro en web/app.py ----------------
    constantes = _constantes_app()
    opciones = constantes.get("OPCIONES") or []
    admin = constantes.get("OPCIONES_ADMIN") or []
    categorias = constantes.get("CATEGORIAS") or []
    en_categorias = [op for _, ops in categorias for op in ops]

    check(
        "jap: la operacion esta SOLO en OPCIONES_ADMIN (los operadores no "
        "gastan saldo)",
        OP in admin and OP not in opciones,
        f"(ops={len(opciones)}, admin={len(admin)})",
    )
    check(
        "jap: aparece en UNA sola categoria y es '📊 Datos y cuentas'",
        [nombre for nombre, ops in categorias if OP in ops]
        == ["📊 Datos y cuentas"],
    )
    check(
        "jap: las 17 operaciones base y las 4 categorias siguen intactas",
        len(opciones) == 17
        and len(categorias) == 4
        and set(en_categorias)
        == set(opciones) | set(admin) | set(constantes.get("OPCIONES_CREAR") or []),
        f"(real={len(opciones)} ops, {len(categorias)} categorias)",
    )
    check(
        "jap: existe el branch de despacho startswith('🛒')",
        "🛒" in _prefijos_despacho()
        and 'elif seleccion.startswith("🛒"):' in _fuente_app(),
    )
    check(
        "jap: el dispatch importa web.operaciones.jap y el archivo existe",
        "jap" in _modulos_importados()
        and (RAIZ / "web" / "operaciones" / "jap.py").is_file(),
    )

    # ---------------- (2) Constantes y helpers puros ----------------
    fuente_pagina = (RAIZ / "web" / "operaciones" / "jap.py").read_text(
        encoding="utf-8"
    )
    minimos = _minimos_number_inputs(fuente_pagina)
    claves_minimo_cero = (
        "jap_quote_likes",
        "jap_quote_rts",
        "jap_quote_comments",
        "jap_quote_views",
        "jap_quote_followers",
    )
    check(
        "jap: sin minimo de pedido (0 = no comprar en todos los campos)",
        all(minimos.get(clave) == 0 for clave in claves_minimo_cero),
        str({clave: minimos.get(clave) for clave in claves_minimo_cero}),
    )
    check(
        "jap: la pagina importa integraciones.jap de forma perezosa (dentro "
        "de _cliente)",
        "from integraciones import jap" in fuente_pagina
        and "def _cliente()" in fuente_pagina
        and "from integraciones import jap as cliente\n" in fuente_pagina,
    )
    check(
        "jap: hay 18 links de @RedAMLOmx (constante LINKS_REDAMLO)",
        len(pagina.LINKS_REDAMLO) == 18
        and all(
            link.startswith("https://x.com/RedAMLOmx/status/")
            for link in pagina.LINKS_REDAMLO
        )
        and pagina.LINKS_REDAMLO[0].endswith("2125500199543898152")
        and pagina.LINKS_REDAMLO[-1].endswith("2125682537393787196"),
        f"(real={len(pagina.LINKS_REDAMLO)})",
    )
    check(
        "jap: comentarios economicos=6601 y seguros/custom=6602",
        pagina.SERVICIO_COMENTARIOS_ECONOMICO == 6601
        and pagina.SERVICIO_COMENTARIOS_SEGURO == 6602
        and pagina._id_comentarios("Económico", False) == 6601
        and pagina._id_comentarios("Seguro", False) == 6602
        and pagina._id_comentarios("Económico", True) == 6602,
    )
    links_sucios = (
        " 1- https://x.com/a/status/1 \n"
        "https://x.com/a/status/1\n"
        "\n"
        "no-es-un-link\n"
        "2) https://x.com/a/status/2\n"
    )
    normalizados = pagina._normalizar_links(links_sucios)
    check(
        "jap: _normalizar_links dedupe, limpia numeracion e ignora basura",
        normalizados
        == ["https://x.com/a/status/1", "https://x.com/a/status/2"],
        str(normalizados),
    )
    check(
        "jap: _parsear_ids tolera comas/espacios y descarta invalidos",
        pagina._parsear_ids("12, 13; x, 12, #14") == [12, 13, 14],
        str(pagina._parsear_ids("12, 13; x, 12, #14")),
    )
    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        filas, avisos = pagina._calcular_lote(
            pagina.LINKS_REDAMLO[:2],
            {"likes": 50, "retweets": 10, "comments": 3, "views": 500},
            {
                "likes": {"id": 9013},
                "retweets": {"id": 8860},
                "comments": {"id": 6601},
                "views": {"id": 8413},
            },
        )
    total = sum(fila["Total $"] for fila in filas)
    check(
        "jap: _calcular_lote calcula un total > 0 por link",
        len(filas) == 2 and total > 0 and not avisos,
        f"(total={total:.6f}, avisos={avisos})",
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        filas_cero, avisos_cero = pagina._calcular_lote(
            pagina.LINKS_REDAMLO[:2],
            {"likes": 0, "retweets": 0, "comments": 0, "views": 0},
            {
                "likes": {"id": 9013},
                "retweets": {"id": 8860},
                "comments": {"id": 6601},
                "views": {"id": 8413},
            },
        )
    total_cero = sum(fila["Total $"] for fila in filas_cero)
    check(
        "jap: _calcular_lote con 0 en todo no cobra ni avisa (sin minimo)",
        len(filas_cero) == 2 and total_cero == 0 and not avisos_cero,
        f"(total={total_cero}, avisos={avisos_cero})",
    )

    # ---------------- (2b) Plan de compra del lote ----------------
    dos_links = ["https://x.com/a/status/1", "https://x.com/a/status/2"]
    cero = {
        "likes": 0,
        "retweets": 0,
        "comments": 0,
        "views": 0,
        "followers": 0,
    }
    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_likes, errores_likes = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "likes": 10},
            SERVICIOS_PLAN,
            "",
            "",
        )
    check(
        "jap: _plan_pedidos_lote crea 1 pedido de likes por link (2 links)",
        len(pedidos_likes) == 2
        and not errores_likes
        and all(
            p["tipo"] == "likes"
            and p["servicio"] == 9013
            and p["cantidad"] == 10
            for p in pedidos_likes
        )
        and {p["link"] for p in pedidos_likes} == set(dos_links),
        str(pedidos_likes),
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_vacio, errores_vacio = pagina._plan_pedidos_lote(
            dos_links, cero, SERVICIOS_PLAN, "", ""
        )
    check(
        "jap: _plan_pedidos_lote con 0 en todo no crea pedidos",
        pedidos_vacio == [] and errores_vacio == [],
        f"(pedidos={pedidos_vacio}, errores={errores_vacio})",
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_custom, errores_custom = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "comments": 5},
            SERVICIOS_PLAN,
            "",
            COMENTARIOS_5,
        )
    check(
        "jap: comentarios con texto propio usan 6602 y llevan `comments`",
        len(pedidos_custom) == 2
        and not errores_custom
        and all(
            p["tipo"] == "comments"
            and p["servicio"] == 6602
            and p["comments"] == COMENTARIOS_5
            for p in pedidos_custom
        ),
        str(pedidos_custom),
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_random, errores_random = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "comments": 5},
            SERVICIOS_PLAN,
            "",
            "",
        )
    check(
        "jap: comentarios sin texto usan 6601 y `comments` vacio",
        len(pedidos_random) == 2
        and not errores_random
        and all(
            p["tipo"] == "comments"
            and p["servicio"] == 6601
            and p["comments"] == ""
            for p in pedidos_random
        ),
        str(pedidos_random),
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_followers, errores_followers = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "followers": 10},
            SERVICIOS_PLAN,
            "https://x.com/perfil",
            "",
        )
    check(
        "jap: seguidores se piden UNA sola vez al perfil",
        len(pedidos_followers) == 1
        and not errores_followers
        and pedidos_followers[0]["tipo"] == "followers"
        and pedidos_followers[0]["link"] == "https://x.com/perfil"
        and pedidos_followers[0]["servicio"] == 9011,
        str(pedidos_followers),
    )
    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_sin_perfil, errores_sin_perfil = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "followers": 10},
            SERVICIOS_PLAN,
            "",
            "",
        )
    check(
        "jap: seguidores sin link de perfil devuelven error y no crean pedido",
        pedidos_sin_perfil == []
        and any("perfil" in error.lower() for error in errores_sin_perfil),
        str(errores_sin_perfil),
    )

    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_pocos, errores_pocos = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "comments": 5},
            SERVICIOS_PLAN,
            "",
            "Comentario 1\nComentario 2\nComentario 3",
        )
    check(
        "jap: comentarios personalizados insuficientes devuelven error",
        pedidos_pocos == []
        and any("Comentarios" in error for error in errores_pocos),
        str(errores_pocos),
    )

    with mock.patch.object(jap_mod, "calcular_costo", _costo_con_minimos):
        pedidos_min, errores_min = pagina._plan_pedidos_lote(
            dos_links,
            {**cero, "likes": 5},
            SERVICIOS_PLAN,
            "",
            "",
        )
    check(
        "jap: una cantidad debajo del minimo de JAP bloquea el plan",
        pedidos_min == []
        and any("minima" in error.lower() for error in errores_min),
        str(errores_min),
    )

    muchos_links = [
        f"https://x.com/a/status/{numero}" for numero in range(1, 35)
    ]
    with mock.patch.object(jap_mod, "calcular_costo", _fake_costo):
        pedidos_grandes, errores_grandes = pagina._plan_pedidos_lote(
            muchos_links,
            {**cero, "likes": 10, "retweets": 10, "views": 100},
            SERVICIOS_PLAN,
            "",
            "",
        )
    check(
        "jap: mas de 100 pedidos en un lote devuelve error",
        len(pedidos_grandes) == 102
        and any("100" in error for error in errores_grandes),
        f"(pedidos={len(pedidos_grandes)}, errores={errores_grandes})",
    )

    # ---------------- (3) AppTest con backend mockeado ----------------
    ok, detalle, datos = _app_test_con_key()
    check("jap: renderiza sin excepciones (AppTest con mocks)", ok, detalle)
    check(
        "jap: muestra las 3 pestanas esperadas (sin 'Nuevo pedido')",
        datos["tabs"]
        == [
            "📊 Catálogo",
            "🧮 Cotizador (lote de tweets)",
            "📦 Pedidos",
        ],
        str(datos["tabs"]),
    )
    check(
        "jap: muestra el aviso de que no hay servicios X de Mexico",
        datos["aviso_mexico"],
    )
    check(
        "jap: metricas de saldo ($3.51) y servicios de X",
        datos["saldo"] == "$3.51" and datos["servicios_x"] not in ("", "—"),
        f"(saldo={datos['saldo']}, x={datos['servicios_x']})",
    )
    check(
        "jap: el input de la API key es type=password",
        datos["password"],
    )
    check(
        "jap: el catalogo muestra la columna Precio/1K USD y servicios de X",
        datos["catalogo"] and datos["catalogo_x"],
    )
    check(
        "jap: existe el boton de cargar los 18 tweets de @RedAMLOmx",
        datos["boton_18"],
    )
    check(
        "jap: el boton carga exactamente los 18 links en el textarea",
        datos["links_cargados"] == 18,
        f"(links={datos['links_cargados']})",
    )
    check(
        "jap: el cotizador pinta la tabla por link con total > 0",
        datos["tabla_cotizador"] and datos["total_lote"] > 0,
        f"(total={datos['total_lote']:.6f})",
    )
    check(
        "jap: el cotizador muestra la seccion de compra del lote",
        datos["seccion_compra"] and datos["confirmo"],
        f"(seccion={datos['seccion_compra']}, confirmo={datos['confirmo']})",
    )
    check(
        "jap: el boton de comprar el lote esta deshabilitado sin confirmar",
        datos["compra_deshabilitada"],
    )
    check(
        "jap: la pestana Pedidos tiene el boton de consultar estado",
        datos["estados"],
    )
    check(
        "jap: el cotizador tiene Cliente y Campaña para el reporte "
        "(jap_lote_cliente / jap_lote_campana)",
        datos["cliente_input"] and datos["campana_input"],
        f"(cliente={datos['cliente_input']}, campana={datos['campana_input']})",
    )
    check(
        "jap: la pestana Pedidos tiene el expander del reporte manual",
        datos["manual_expander"],
    )

    # ---------------- (4) AppTest de compra del lote ----------------
    ok_lote, detalle_lote, datos_lote = _app_test_compra_lote()
    check(
        "jap: AppTest de compra del lote renderiza sin excepciones",
        ok_lote,
        detalle_lote,
    )
    check(
        "jap: sin confirmacion el boton del lote esta deshabilitado y no "
        "llama a la API",
        datos_lote["boton_existe"]
        and datos_lote["deshabilitado_sin_confirmar"]
        and datos_lote["llamadas_sin_confirmar"] == 0,
        str(datos_lote),
    )
    check(
        "jap: al confirmar se compra 1 pedido de likes por cada link",
        datos_lote["habilitado_confirmado"]
        and datos_lote["llamadas"] == 2
        and datos_lote["servicios"] == [9013, 9013]
        and datos_lote["links"]
        == ["https://x.com/a/status/1", "https://x.com/a/status/2"]
        and datos_lote["cantidades"] == [10, 10],
        str(datos_lote),
    )
    check(
        "jap: tras comprar aparece el resumen, se guardan los pedidos y la "
        "confirmacion se limpia",
        datos_lote["expander_resultados"]
        and datos_lote["pedidos_recientes"] == 2
        and datos_lote["confirmacion_limpia"],
        str(datos_lote),
    )

    # ---------------- (5) Persistencia del lote + reportes --------------
    check(
        "jap: guardar_pedidos recibe lote_id, cliente y campana al comprar",
        len(datos_lote["guardados"]) == 1
        and datos_lote["guardados"][0]["lote_id"] == "LOTE-TEST-001"
        and datos_lote["guardados"][0]["cliente"] == "Cliente Demo"
        and datos_lote["guardados"][0]["campana"] == "Campaña Demo"
        and len(datos_lote["guardados"][0]["pedidos"]) == 2,
        str(datos_lote["guardados"])[:400],
    )
    check(
        "jap: el resumen del lote guarda lote_id/cliente/campaña",
        datos_lote["lote_id"] == "LOTE-TEST-001"
        and datos_lote["cliente"] == "Cliente Demo"
        and datos_lote["campana"] == "Campaña Demo"
        and datos_lote["historial_ok"],
        str(
            {
                "lote_id": datos_lote["lote_id"],
                "cliente": datos_lote["cliente"],
                "campana": datos_lote["campana"],
                "ok": datos_lote["historial_ok"],
            }
        ),
    )
    check(
        "jap: tras la compra aparecen los reportes del lote (HTML y CSV)",
        {"jap_dl_lote_html", "jap_dl_lote_csv"}.issubset(
            set(datos_lote["download_keys"])
        ),
        str(datos_lote["download_keys"]),
    )
    check(
        "jap: el lote persistido usa generar_reporte_lote (HTML y CSV)",
        ("LOTE-TEST-001", "html") in datos_lote["reportes_lote"]
        and ("LOTE-TEST-001", "csv") in datos_lote["reportes_lote"],
        str(datos_lote["reportes_lote"]),
    )
    check(
        "jap: Pedidos lista los lotes persistidos y permite elegir uno",
        datos_lote["persisted_table"] and datos_lote["selector_lote"],
        f"(tabla={datos_lote['persisted_table']}, "
        f"selector={datos_lote['selector_lote']})",
    )

    fake_reporte = FakeHistorial()
    with _historial_fake(fake_reporte):
        html_lote, error_html_lote = pagina._reporte_lote_contenido(
            "LOTE-TEST-001", "html"
        )
        csv_lote, error_csv_lote = pagina._reporte_lote_contenido(
            "LOTE-TEST-001", "csv"
        )
    check(
        "jap: _reporte_lote_contenido delega en generar_reporte_lote",
        fake_reporte.generados
        == [("LOTE-TEST-001", "html"), ("LOTE-TEST-001", "csv")]
        and not error_html_lote
        and not error_csv_lote
        and "MW Group" in html_lote
        and "LOTE-TEST-001" in csv_lote,
        str(fake_reporte.generados),
    )

    pedidos_manual, errores_manual = pagina._plan_reporte_manual(
        ["https://x.com/a/status/1", "https://x.com/a/status/2"],
        {
            "likes": 50,
            "retweets": 0,
            "comments": 5,
            "views": 0,
            "followers": 10,
        },
        "https://x.com/perfil",
    )
    check(
        "jap: el reporte manual arma pedidos falsos (likes/comentarios/"
        "seguidores) sin crear ordenes",
        not errores_manual
        and len(pedidos_manual) == 5
        and all(pedido.get("ok") is True for pedido in pedidos_manual)
        and [p["tipo"] for p in pedidos_manual]
        == ["likes", "likes", "comments", "comments", "followers"],
        str(pedidos_manual),
    )

    fake_manual = FakeHistorial()
    with _historial_fake(fake_manual):
        pagina._contenido_reporte(
            pedidos_manual,
            "html",
            fecha="2026-11-27",
            cliente="Cliente Demo",
            campana="Campaña Demo",
            folio="LOTE-DEMO",
        )
        pagina._contenido_reporte(
            pedidos_manual,
            "csv",
            fecha="2026-11-27",
            cliente="Cliente Demo",
            campana="Campaña Demo",
            folio="LOTE-DEMO",
        )
    check(
        "jap: el reporte manual usa construir_reporte_html/csv cuando "
        "el historial esta disponible",
        [construido[0] for construido in fake_manual.construidos]
        == ["html", "csv"]
        and all(
            construido[1] == pedidos_manual
            for construido in fake_manual.construidos
        ),
        str([c[0] for c in fake_manual.construidos]),
    )

    with mock.patch.object(pagina, "_historial", lambda: None):
        folio_local = pagina._nuevo_lote_id()
        html_local = pagina._contenido_reporte(
            pedidos_manual,
            "html",
            fecha="2026-11-27",
            cliente="Cliente Demo",
            campana="Campaña Demo",
            folio="LOTE-DEMO",
        )
        csv_local = pagina._contenido_reporte(
            pedidos_manual,
            "csv",
            fecha="2026-11-27",
            cliente="Cliente Demo",
            campana="Campaña Demo",
            folio="LOTE-DEMO",
        )
    check(
        "jap: sin core.jap_historial hay folio local y reporte de respaldo "
        "(HTML/CSV) sin datos internos",
        folio_local.startswith("LOTE-")
        and "https://x.com/a/status/1" in html_local
        and "https://x.com/a/status/2" in csv_local
        and "mw group" in html_local.lower()
        and not _tiene_terminos_internos(html_local)
        and not _tiene_terminos_internos(csv_local),
        f"(folio={folio_local})",
    )

    html_manual, csv_manual, origen_manual = _reporte_manual_real_o_local(
        pedidos_manual
    )
    check(
        "jap: el reporte manual incluye links y cantidades",
        "https://x.com/a/status/1" in html_manual
        and "https://x.com/a/status/2" in html_manual
        and "50" in html_manual
        and "5" in html_manual
        and "https://x.com/a/status/1" in csv_manual
        and "https://x.com/a/status/2" in csv_manual
        and "50" in csv_manual
        and "5" in csv_manual,
        origen_manual,
    )
    check(
        "jap: el reporte no menciona proveedor, costos, saldo, presets, "
        "IDs de servicio ni 'compra'",
        not _tiene_terminos_internos(html_manual)
        and not _tiene_terminos_internos(csv_manual),
        origen_manual,
    )
    check(
        "jap: el reporte lleva el encabezado 'MW Group'",
        "mw group" in html_manual.lower(),
        origen_manual,
    )
    check(
        "jap: los archivos del lote se llaman reporte-<lote_id>.html/.csv",
        pagina._nombre_reporte("LOTE-TEST-001", "html")
        == "reporte-LOTE-TEST-001.html"
        and pagina._nombre_reporte("LOTE-TEST-001", "csv")
        == "reporte-LOTE-TEST-001.csv",
    )
    sin_links, errores_sin_links = pagina._plan_reporte_manual(
        [],
        {"likes": 50, "retweets": 0, "comments": 0, "views": 0, "followers": 0},
        "",
    )
    sin_cantidad, errores_sin_cantidad = pagina._plan_reporte_manual(
        ["https://x.com/a/status/1"],
        {"likes": 0, "retweets": 0, "comments": 0, "views": 0, "followers": 0},
        "",
    )
    check(
        "jap: el reporte manual exige links y al menos una cantidad > 0",
        sin_links == []
        and errores_sin_links
        and sin_cantidad == []
        and errores_sin_cantidad,
        f"({errores_sin_links}, {errores_sin_cantidad})",
    )

    ok_manual, detalle_manual, datos_manual = _app_test_reporte_manual()
    check(
        "jap: el reporte manual renderiza, descarga HTML/CSV y no llama a "
        "crear_orden (AppTest sin API key)",
        ok_manual
        and datos_manual["html"]
        and datos_manual["csv"],
        detalle_manual or str(datos_manual),
    )
    check(
        "jap: el reporte manual muestra el aviso de que no incluye proveedor",
        datos_manual["caption_sin_proveedor"],
        str(datos_manual),
    )

    ok_fallo, detalle_fallo, datos_fallo = _app_test_compra_lote(
        FakeHistorialFallido()
    )
    check(
        "jap: si el historial falla la compra continua (2 pedidos) y avisa",
        ok_fallo
        and datos_fallo["llamadas"] == 2
        and not datos_fallo["historial_ok"]
        and any(
            "Base no disponible" in aviso for aviso in datos_fallo["warnings"]
        ),
        detalle_fallo or str(datos_fallo["warnings"]),
    )
    check(
        "jap: sin historial persistido el lote igual ofrece reporte local",
        {"jap_dl_lote_html", "jap_dl_lote_csv"}.issubset(
            set(datos_fallo["download_keys"])
        ),
        str(datos_fallo["download_keys"]),
    )

    # ---------------- (6) AppTest sin API key ----------------
    ok_sin, detalle_sin, datos_sin = _app_test_sin_key()
    check(
        "jap: renderiza sin API key y sin catalogo (AppTest, sin excepciones)",
        ok_sin,
        detalle_sin,
    )
    check(
        "jap: sin key muestra instrucciones y desactiva la compra",
        datos_sin["instrucciones"] and datos_sin["compra_deshabilitada"],
        str(datos_sin),
    )
    check(
        "jap: sin key la metrica dice 'No configurada'",
        datos_sin["key_no_configurada"],
    )

    # ---------------- (7) Google Sheets (link para el jefe) ----------------
    sheets_ok = FakeGoogleSheets(disponible=True)
    ok_sheet, detalle_sheet, datos_sheet = _app_test_sheet_ultimo(sheets_ok)
    check(
        "jap: Google Sheets - la pagina tiene el expander 'Conectar Google "
        "Sheets' y renderiza sin excepciones",
        ok_sheet and datos_sheet["expander"],
        detalle_sheet or str(datos_sheet),
    )
    check(
        "jap: Google Sheets - instrucciones con Sheets API, Drive API, "
        "GOOGLE_SERVICE_ACCOUNT_B64 y el archivo local",
        datos_sheet["instrucciones"],
        str(datos_sheet["instrucciones"]),
    )
    llamada_ultimo = (
        datos_sheet["llamadas"][0] if datos_sheet["llamadas"] else {}
    )
    check(
        "jap: Google Sheets - el ultimo lote llama crear_sheet_reporte con "
        "los pedidos y metadatos correctos",
        datos_sheet["boton_existe"]
        and len(datos_sheet["llamadas"]) == 1
        and len(llamada_ultimo.get("pedidos") or []) == 2
        and llamada_ultimo.get("cliente") == "Cliente Demo"
        and llamada_ultimo.get("campana") == "Campana Demo"
        and llamada_ultimo.get("folio") == "LOTE-SHEET-001"
        and llamada_ultimo.get("fecha") == "2026-11-27T12:00:00"
        and llamada_ultimo.get("compartir_publico") is True
        and llamada_ultimo.get("email") == "",
        str(llamada_ultimo)[:400],
    )
    check(
        "jap: Google Sheets - muestra 'Google Sheet creado', el link y el "
        "codigo copiable",
        sheets_ok.url in datos_sheet["codes"]
        and any(
            "Google Sheet creado" in texto
            for texto in datos_sheet["success"]
        ),
        f"(codes={datos_sheet['codes']}, success={datos_sheet['success']})",
    )
    check(
        "jap: Google Sheets - crear el Sheet NO crea ordenes JAP",
        datos_sheet["ordenes"] == [],
        str(datos_sheet["ordenes"])[:300],
    )
    check(
        "jap: Google Sheets - sin correo y sin permiso publico bloquea el "
        "boton y pide un correo",
        datos_sheet["bloqueo_sin_correo"],
        str(datos_sheet["bloqueo_sin_correo"]),
    )

    sheets_off = FakeGoogleSheets(disponible=False)
    ok_off, detalle_off, datos_off = _app_test_sheet_ultimo(sheets_off)
    check(
        "jap: sin Google Sheets conectado no hay boton de crear Sheet, no "
        "llama a la API y muestra el aviso",
        ok_off
        and not datos_off["boton_existe"]
        and datos_off["llamadas"] == []
        and any(
            "Conecta Google Sheets en la pestaña Pedidos" in texto
            and "crear el link" in texto
            for texto in datos_off["captions"]
        ),
        detalle_off or str(datos_off["captions"]),
    )

    ok_null, detalle_null, datos_null = _app_test_sheet_ultimo(None)
    check(
        "jap: sin core.google_sheets la pagina avisa y no intenta crear el "
        "Sheet",
        ok_null
        and not datos_null["boton_existe"]
        and datos_null["llamadas"] == []
        and any(
            "Conecta Google Sheets" in texto
            for texto in datos_null["captions"]
        ),
        detalle_null or str(datos_null["captions"]),
    )

    sheets_lote = FakeGoogleSheets(disponible=True)
    ok_sheet_lote, detalle_sheet_lote, datos_sheet_lote = (
        _app_test_sheet_lote(sheets_lote)
    )
    llamada_lote = (
        datos_sheet_lote["llamadas"][0]
        if datos_sheet_lote["llamadas"]
        else {}
    )
    check(
        "jap: Google Sheets - el lote persistido llama con su cliente, "
        "campana, folio y fecha",
        ok_sheet_lote
        and datos_sheet_lote["boton_existe"]
        and len(datos_sheet_lote["llamadas"]) == 1
        and len(llamada_lote.get("pedidos") or []) == 2
        and llamada_lote.get("cliente") == "Cliente Lote"
        and llamada_lote.get("campana") == "Campana Lote"
        and llamada_lote.get("folio") == "LOTE-SHEET-002"
        and llamada_lote.get("fecha") == "2026-11-27T10:00:00"
        and sheets_lote.url in datos_sheet_lote["codes"]
        and datos_sheet_lote["ordenes"] == [],
        detalle_sheet_lote or str(llamada_lote)[:400],
    )

    sheets_manual = FakeGoogleSheets(disponible=True)
    ok_sheet_manual, detalle_sheet_manual, datos_sheet_manual = (
        _app_test_sheet_manual(sheets_manual)
    )
    llamada_manual = (
        datos_sheet_manual["llamadas"][0]
        if datos_sheet_manual["llamadas"]
        else {}
    )
    check(
        "jap: Google Sheets - el reporte manual usa los pedidos falsos, "
        "metadatos y no crea ordenes",
        ok_sheet_manual
        and datos_sheet_manual["boton_existe"]
        and len(datos_sheet_manual["llamadas"]) == 1
        and len(llamada_manual.get("pedidos") or []) == 4
        and all(
            pedido.get("ok") is True
            for pedido in llamada_manual.get("pedidos") or []
        )
        and llamada_manual.get("cliente") == "Cliente Manual"
        and llamada_manual.get("campana") == "Campana Manual"
        and str(llamada_manual.get("folio") or "").startswith("MANUAL-")
        and llamada_manual.get("fecha")
        == str(llamada_manual.get("folio") or "").replace("MANUAL-", "", 1)
        and sheets_manual.url in datos_sheet_manual["codes"]
        and datos_sheet_manual["ordenes"] == [],
        detalle_sheet_manual or str(llamada_manual)[:400],
    )

    sheets_cred = FakeGoogleSheets(disponible=False)
    ok_cred, detalle_cred, datos_cred = _app_test_sheets_credenciales(
        sheets_cred
    )
    check(
        "jap: Google Sheets - guardar credenciales usa "
        "guardar_credenciales_json y muestra el email",
        ok_cred
        and datos_cred["guardados"] == [JSON_CREDENCIALES]
        and datos_cred["exito"]
        and datos_cred["email_visible"],
        detalle_cred or str(datos_cred),
    )
    valores_sesion = " ".join(datos_cred["session_gs"].values()).lower()
    check(
        "jap: Google Sheets - el JSON de credenciales NO queda visible, en el "
        "input ni en session_state despues de guardar",
        datos_cred["password"]
        and datos_cred["valor_input"] == ""
        and not datos_cred["fuga_json"]
        and "abc123secreto" not in valores_sesion
        and "private_key" not in valores_sesion,
        f"(input={datos_cred['valor_input'][:20]!r}, "
        f"fuga={datos_cred['fuga_json']}, "
        f"sesion={datos_cred['session_gs']})",
    )
    check(
        "jap: Google Sheets - 'Probar conexion' solo consulta el estado (no "
        "guarda ni crea Sheets)",
        datos_cred["prueba_ok"]
        and len(sheets_cred.guardados) == 1
        and datos_cred["crear_llamadas"] == 0,
        str(datos_cred),
    )


if __name__ == "__main__":
    from run_tests import check, resumen

    run(check)
    sys.exit(resumen())
