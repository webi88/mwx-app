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
      invalidas), `_parsear_ids`, `_etiqueta_servicio`, `_id_comentarios` y
      `_calcular_lote` (total > 0 con precios simulados).
  (3) AppTest con backend mockeado (key configurada, saldo $3.51): render sin
      excepciones, 4 pestanas, aviso visible de Mexico, metricas de saldo y
      servicios X, catalogo con la columna "Precio/1K USD", boton de los 18
      links que los carga en el textarea, tabla del cotizador con total > 0 y
      boton de compra deshabilitado sin la confirmacion.
  (4) AppTest sin API key (balance/catalogo con error): render sin excepciones,
      instrucciones visibles y compra deshabilitada.

Uso:
    .venv/Scripts/python.exe tests/test_jap_web.py
"""
from __future__ import annotations

import ast
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
        "compra_deshabilitada": False,
        "estados": False,
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

        try:
            resultado["compra_deshabilitada"] = bool(
                at.button(key="jap_btn_comprar").disabled
            )
        except Exception:
            resultado["compra_deshabilitada"] = False

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
        resultado["estados"] = any(
            t.key == "jap_btn_consultar" for t in at.button
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
        # Sin catálogo no hay servicio que comprar: el botón puede no existir.
        # Cualquiera de las dos formas (ausente o deshabilitado) impide gastar.
        botones = {b.key: b for b in at.button}
        boton_compra = botones.get("jap_btn_comprar")
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
        "jap_buy_cantidad",
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
    check(
        "jap: _etiqueta_servicio muestra ID/categoria/nombre/precio/min-max",
        pagina._etiqueta_servicio(
            {
                "service": 9013,
                "category": "Twitter Likes",
                "name": "Twitter Likes 7D",
                "rate": "0.275",
                "min": "10",
                "max": "2500",
            }
        )
        == "9013 · Twitter Likes · Twitter Likes 7D · $0.2750/1K · 10-2500",
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

    # ---------------- (3) AppTest con backend mockeado ----------------
    ok, detalle, datos = _app_test_con_key()
    check("jap: renderiza sin excepciones (AppTest con mocks)", ok, detalle)
    check(
        "jap: muestra las 4 pestanas esperadas",
        datos["tabs"]
        == [
            "📊 Catálogo",
            "🧮 Cotizador (lote de tweets)",
            "🚀 Nuevo pedido",
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
        "jap: el boton de comprar esta deshabilitado sin la confirmacion",
        datos["compra_deshabilitada"],
    )
    check(
        "jap: la pestana Pedidos tiene el boton de consultar estado",
        datos["estados"],
    )

    # ---------------- (4) AppTest sin API key ----------------
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


if __name__ == "__main__":
    from run_tests import check, resumen

    run(check)
    sys.exit(resumen())
