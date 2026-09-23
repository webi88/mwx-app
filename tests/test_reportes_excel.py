"""Tests rapidos de la tabla separada + links + descarga Excel de `Reportes`.

Objetivo del dueño: en la "tabla de exitos" del dashboard debe haber UNA fila
vacia entre cada exito y el siguiente, cada fila debe decir que fue (Resultado)
y traer el link del post cuando exista (Link), y el .xlsx descargado debe traer
el mismo acomodo (exito, vacia, exito, vacia, ...).

Cubre, sin red ni Chrome:
  - `_es_url_post`: solo http(s) con "/status/" (rechaza perfiles/vacios/basura).
  - `_resultado_accion`: etiquetas por tipo (cita/post/comentario/rt/like) y
    link solo para categorias que lo generan; rt/like SIEMPRE sin link.
  - `_filas_con_separacion`: 0/1/3 exitos, largo exacto n + n-1, posiciones
    pares = exitos en orden, impares = filas con TODAS sus claves en "",
    primera/ultima = primer/ultimo exito, idempotencia y no mutacion.
  - `_excel_exitos_bytes`: .xlsx valido en memoria con cabeceras estilizadas
    (negrita, fondo 1DA1F2, ancho 22), fila 2 = primer exito, fila 3 vacia,
    fila 4 = segundo exito, ...; Resultado/Link en sus celdas; filas vacias SIN
    celdas con valores; 0 filas sin lanzar; fallo de openpyxl -> b"".
  - `web/operaciones/reportes.py`: usa `_filas_con_separacion` para el
    dataframe, LinkColumn clicable y `st.download_button` con
    `key="dl_reportes_exitos"`; ya NO duplica la lista markdown de links.
  - Render completo con `AppTest` (BD silenciada con fakes): 0 excepciones,
    dataframe con las filas separadas, columnas Resultado/Link y boton de
    descarga; fallback TypeError de `obtener_acciones`; con
    `_excel_exitos_bytes -> b""` muestra warning y NO boton.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_reportes_excel.py   (solo este archivo)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import openpyxl  # noqa: E402

from web.operaciones import reportes  # noqa: E402

COLUMNAS = ["Fecha", "Usuario", "Tipo", "Resultado", "Link"]


def _exitos(n: int) -> list:
    """Exitos de prueba (dicts con las 5 columnas de la tabla).

    Alterna post (con link), rt (sin link, aunque la URL sea de perfil) y cita
    (con link); `Resultado`/`Link` siempre salen del helper real."""
    fecha = "2026-09-22 10:0{}"
    tipos = ("post", "rt", "cita")
    filas = []
    for i in range(n):
        tipo = tipos[i % 3]
        url = (
            f"https://twitter.com/cuenta_{i}"
            if tipo == "rt"
            else f"https://x.com/cuenta_{i}/status/{i}"
        )
        resultado, link = reportes._resultado_accion(tipo, url)
        filas.append({
            "Fecha": fecha.format(i),
            "Usuario": f"cuenta_{i}",
            "Tipo": tipo,
            "Resultado": resultado,
            "Link": link,
        })
    return filas


def _es_vacia(fila: dict) -> bool:
    """True si TODOS los valores de la fila son '' (fila separadora)."""
    return bool(fila) and all(valor == "" for valor in fila.values())


# --------------------------------------------------------------------------- #
# `_filas_con_separacion`
# --------------------------------------------------------------------------- #
def _test_filas_con_separacion(check):
    print("(a) _filas_con_separacion: 0/1/3 exitos")

    check("separacion: 0 exitos -> lista vacia", reportes._filas_con_separacion([]) == [])

    uno = _exitos(1)
    sep_uno = reportes._filas_con_separacion(uno)
    check(
        "separacion: 1 exito -> largo 1 y sin filas vacias",
        len(sep_uno) == 1 and sep_uno == uno and sep_uno[0]["Usuario"] == "cuenta_0",
    )

    tres = _exitos(3)
    sep = reportes._filas_con_separacion(tres)
    check(
        "separacion: 3 exitos -> largo exacto n + n-1 = 5",
        len(sep) == 5,
        f"(real={len(sep)})",
    )
    check(
        "separacion: posiciones pares = exitos EN ORDEN",
        [sep[0], sep[2], sep[4]] == tres,
    )
    claves = set(COLUMNAS)
    check(
        "separacion: posiciones impares = filas con TODOS los valores ''",
        _es_vacia(sep[1])
        and _es_vacia(sep[3])
        and set(sep[1]) == claves
        and set(sep[3]) == claves
        and sep[1] == {c: "" for c in COLUMNAS},
    )
    check(
        "separacion: primera fila = primer exito y ultima = ultimo exito",
        sep[0] == tres[0] and sep[-1] == tres[-1],
    )
    check(
        "separacion: no modifica la lista original (sigue con 3)",
        len(tres) == 3 and tres == _exitos(3),
    )

    sep_doble = reportes._filas_con_separacion(sep)
    check(
        "separacion: idempotente (lista ya separada no duplica vacias)",
        len(sep_doble) == 5
        and _es_vacia(sep_doble[1])
        and _es_vacia(sep_doble[3])
        and [sep_doble[0], sep_doble[2], sep_doble[4]] == tres,
    )

    check(
        "separacion: tolerante con None/entradas raras sin lanzar",
        reportes._filas_con_separacion(None) == []
        and reportes._filas_con_separacion([None, "", {"Usuario": "solo"}]) == [
            {"Usuario": "solo"}
        ],
    )


# --------------------------------------------------------------------------- #
# `_excel_exitos_bytes`
# --------------------------------------------------------------------------- #
def _leer(ws):
    return [
        [ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
        for r in range(1, ws.max_row + 1)
    ]


def _valor(celda):
    """openpyxl lee las celdas con '' como None: normaliza para comparar."""
    return "" if celda is None else str(celda)


def _fila_igual(fila, esperada) -> bool:
    return [_valor(v) for v in fila] == [_valor(v) for v in esperada]


def _test_excel(check):
    print("(b) _excel_exitos_bytes: layout exito/vacia/exito")

    tres = _exitos(3)
    datos = reportes._excel_exitos_bytes(tres)
    check("excel: devuelve bytes no vacios", isinstance(datos, bytes) and len(datos) > 0)

    wb = openpyxl.load_workbook(io.BytesIO(datos))
    ws = wb.active
    check(
        "excel: fila 1 = cabeceras exactas (Fecha/Usuario/Tipo/Resultado/Link)",
        _leer(ws)[0] == COLUMNAS,
        f"(real={_leer(ws)[0]!r})",
    )
    check(
        "excel: cabeceras con negrita, fondo 1DA1F2 y ancho 22",
        ws.cell(row=1, column=1).font.bold is True
        and str(ws.cell(row=1, column=1).fill.fgColor.rgb).endswith("1DA1F2")
        and all(ws.column_dimensions[c].width == 22 for c in "ABCDE"),
    )

    filas = _leer(ws)
    check(
        "excel: fila 2 = primer exito y fila 4 = segundo exito",
        _fila_igual(filas[1], [tres[0][c] for c in COLUMNAS])
        and _fila_igual(filas[3], [tres[1][c] for c in COLUMNAS]),
    )
    check(
        "excel: Resultado (col D) y Link (col E) en sus celdas reales",
        filas[1][3] == "Post"
        and filas[1][4] == "https://x.com/cuenta_0/status/0"
        and filas[3][3] == "RT simple (no genera link)"
        and _valor(filas[3][4]) == "",
        f"(real={filas[1][3:]!r} / {filas[3][3:]!r})",
    )
    check(
        "excel: filas 3 y 5 COMPLETAMENTE vacias (None/'')",
        all(v in (None, "") for v in filas[2]) and all(v in (None, "") for v in filas[4])
        and filas[2] != filas[3],
    )
    check(
        "excel: max_row = 6 y max_column = 5 (4 exitos/separadores + 2 vacias)",
        ws.max_row == 6 and ws.max_column == 5,
        f"(real={ws.max_row}x{ws.max_column})",
    )

    # Idempotencia: una lista ya separada produce el MISMO layout.
    datos_sep = reportes._excel_exitos_bytes(reportes._filas_con_separacion(tres))
    ws_sep = openpyxl.load_workbook(io.BytesIO(datos_sep)).active
    check(
        "excel: lista ya separada -> mismo layout (sin vacias dobles)",
        _leer(ws_sep) == filas,
    )

    # 0 filas: nunca lanza; si devuelve xlsx, trae solo la cabecera.
    datos_cero = reportes._excel_exitos_bytes([])
    if datos_cero == b"":
        check("excel: 0 filas -> devuelve b'' sin lanzar", True)
    else:
        ws_cero = openpyxl.load_workbook(io.BytesIO(datos_cero)).active
        check(
            "excel: 0 filas -> xlsx valido con solo la cabecera",
            isinstance(datos_cero, bytes)
            and ws_cero.max_row == 1
            and _leer(ws_cero)[0] == COLUMNAS,
        )

    # Fallo de openpyxl -> b"" (nunca lanza).
    with mock.patch.object(openpyxl, "Workbook", side_effect=RuntimeError("fallo simulado")):
        try:
            datos_fallo = reportes._excel_exitos_bytes(tres)
            error = None
        except Exception as e:  # noqa: BLE001
            datos_fallo, error = None, e
    check(
        "excel: si openpyxl falla devuelve b'' sin lanzar",
        error is None and datos_fallo == b"",
        repr(error),
    )


# --------------------------------------------------------------------------- #
# `_es_url_post`
# --------------------------------------------------------------------------- #
def _test_es_url_post(check):
    print("(c) _es_url_post: solo http(s) con /status/")

    check(
        "url_post: acepta https con /status/ (con query) y http",
        reportes._es_url_post("https://x.com/u/status/123?s=20") is True
        and reportes._es_url_post("http://twitter.com/u/status/456") is True,
    )
    check(
        "url_post: rechaza perfiles (sin /status/)",
        reportes._es_url_post("https://twitter.com/u") is False
        and reportes._es_url_post("https://x.com/u/") is False,
    )
    check(
        "url_post: rechaza vacio/None/no-str/basura",
        reportes._es_url_post("") is False
        and reportes._es_url_post(None) is False
        and reportes._es_url_post(123) is False
        and reportes._es_url_post("foo") is False
        and reportes._es_url_post("status/123") is False,
    )
    check(
        "url_post: tolerante con objetos raros (nunca lanza)",
        reportes._es_url_post(object()) is False
        and reportes._es_url_post(["https://x.com/u/status/1"]) is False
        and reportes._es_url_post("https://x.com/u/status/") is False,
    )


# --------------------------------------------------------------------------- #
# `_resultado_accion`
# --------------------------------------------------------------------------- #
def _test_resultado_accion(check):
    print("(d) _resultado_accion: etiquetas + link por tipo")

    url = "https://x.com/u/status/123?s=20"
    check(
        "resultado: cita con link -> Cita (RT con cita) + url",
        reportes._resultado_accion("cita", url) == ("Cita (RT con cita)", url),
    )
    check(
        "resultado: cita sin link -> link no capturado",
        reportes._resultado_accion("cita", "") == ("Cita (link no capturado)", ""),
    )
    check(
        "resultado: post con link -> Post + url",
        reportes._resultado_accion("post", url) == ("Post", url),
    )
    check(
        "resultado: post sin link -> link no capturado",
        reportes._resultado_accion("post", "") == ("Post (link no capturado)", ""),
    )
    check(
        "resultado: comentario con link -> Comentario + url",
        reportes._resultado_accion("comentario", url) == ("Comentario", url),
    )
    check(
        "resultado: rt con URL de PERFIL -> RT simple y link ''",
        reportes._resultado_accion("rt", "https://twitter.com/cuenta_1")
        == ("RT simple (no genera link)", ""),
    )
    check(
        "resultado: rt con URL de post -> igual SIN link (no genera link)",
        reportes._resultado_accion("rt", url) == ("RT simple (no genera link)", ""),
    )
    check(
        "resultado: like con URL del tweet -> Like y link ''",
        reportes._resultado_accion("like", "https://x.com/u/status/55")
        == ("Like (no genera link)", ""),
    )
    check(
        "resultado: tipo desconocido -> tal cual (con/sin link)",
        reportes._resultado_accion("desconocido", "")
        == ("desconocido (link no capturado)", "")
        and reportes._resultado_accion("desconocido", url) == ("desconocido", url),
    )
    check(
        "resultado: mayusculas/acentos ('Retweet con cita', 'PUBLICACIÓN')",
        reportes._resultado_accion("Retweet con cita", url) == ("Cita (RT con cita)", url)
        and reportes._resultado_accion("PUBLICACIÓN", url) == ("Post", url),
    )
    check(
        "resultado: alias (respuesta/reply -> Comentario, repost -> RT)",
        reportes._resultado_accion("respuesta", url) == ("Comentario", url)
        and reportes._resultado_accion("reply", "") == ("Comentario (link no capturado)", "")
        and reportes._resultado_accion("repost", url) == ("RT simple (no genera link)", ""),
    )
    check(
        "resultado: vacio/None -> Accion (link no capturado)",
        reportes._resultado_accion("", "") == ("Acción (link no capturado)", "")
        and reportes._resultado_accion(None, None) == ("Acción (link no capturado)", ""),
    )


# --------------------------------------------------------------------------- #
# Wiring de la pagina + AppTest
# --------------------------------------------------------------------------- #
def _test_pagina_fuente(check):
    print("(e) wiring de web/operaciones/reportes.py")
    fuente = (RAIZ / "web" / "operaciones" / "reportes.py").read_text(encoding="utf-8")
    check(
        "pagina: el dataframe usa la lista separada (filas_mostradas)",
        "filas_mostradas = _filas_con_separacion(filas)" in fuente
        and "st.dataframe(filas_mostradas" in fuente,
    )
    check(
        "pagina: download_button usa bytes de _excel_exitos_bytes + key/mime/nombre",
        "datos_excel = _excel_exitos_bytes(" in fuente
        and "data=datos_excel" in fuente
        and 'key="dl_reportes_exitos"' in fuente
        and 'mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"'
        in fuente
        and 'file_name=f"exitos_{datetime.now():%Y%m%d_%H%M%S}.xlsx"' in fuente,
    )
    check(
        "pagina: si el Excel falla (bytes vacios) muestra warning en vez del boton",
        "if datos_excel:" in fuente and "st.warning(" in fuente,
    )
    check(
        "pagina: COLUMNAS_EXITOS tiene las 5 columnas (Resultado y Link)",
        reportes.COLUMNAS_EXITOS == ("Fecha", "Usuario", "Tipo", "Resultado", "Link"),
        f"(real={reportes.COLUMNAS_EXITOS!r})",
    )
    check(
        "pagina: dataframe con LinkColumn clicable (display_text Abrir)",
        "column_config={" in fuente
        and 'st.column_config.LinkColumn("Link"' in fuente
        and 'display_text="🔗 Abrir"' in fuente,
    )
    check(
        "pagina: usa _resultado_accion para Resultado/Link y ya no duplica el markdown",
        "_resultado_accion(" in fuente
        and '"Resultado": resultado' in fuente
        and '"Link": link' in fuente
        and "🔗 @" not in fuente
        and "ver post]" not in fuente,
    )


def _app_reportes(simular_fallo_excel: bool = False, compat_fallback: bool = False):
    """Script de AppTest: render de Reportes con BD/acciones simuladas.

    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos lo rompen en silencio."""
    import types
    from datetime import datetime

    from web.operaciones import reportes

    class _QueryFake:
        def filter(self, *args, **kwargs):
            return self

        def count(self):
            return 0

        def all(self):
            return []

    class _DBFake:
        def query(self, *args, **kwargs):
            return _QueryFake()

    class _SesionFake:
        def __enter__(self):
            return _DBFake()

        def __exit__(self, *args):
            return False

    def _sesiones(*args, **kwargs):
        return _SesionFake()

    acciones = [
        types.SimpleNamespace(
            fecha=datetime(2026, 9, 22, 10, 0),
            usuario="cuenta_0",
            tipo="post",
            url_publicacion="https://x.com/cuenta_0/status/0",
            estado="exito",
        ),
        types.SimpleNamespace(
            fecha=datetime(2026, 9, 22, 10, 1),
            usuario="cuenta_1",
            tipo="rt",
            url_publicacion="https://twitter.com/cuenta_1",
            estado="exito",
        ),
        types.SimpleNamespace(
            fecha=datetime(2026, 9, 22, 10, 2),
            usuario="cuenta_2",
            tipo="cita",
            url_publicacion="https://x.com/cuenta_2/status/2",
            estado="exito",
        ),
    ]

    originales = (
        reportes.get_db_session,
        reportes.obtener_acciones,
        reportes._excel_exitos_bytes,
    )
    reportes.get_db_session = _sesiones
    if compat_fallback:
        # Backend viejo: `obtener_acciones` no acepta `solo_exitosas` (TypeError).
        reportes.obtener_acciones = lambda limit=200: list(acciones)
    else:
        reportes.obtener_acciones = lambda limit=50, solo_exitosas=False: list(acciones)
    if simular_fallo_excel:
        reportes._excel_exitos_bytes = lambda filas: b""
    try:
        reportes.render({"username": "tester", "rol": "admin"})
    finally:
        (
            reportes.get_db_session,
            reportes.obtener_acciones,
            reportes._excel_exitos_bytes,
        ) = originales


def _correr_app(simular_fallo_excel: bool = False, compat_fallback: bool = False):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(
        _app_reportes,
        default_timeout=60,
        kwargs={
            "simular_fallo_excel": simular_fallo_excel,
            "compat_fallback": compat_fallback,
        },
    )
    at.run()
    return at


def _test_app_test(check):
    print("(f) AppTest: pagina Reportes con 3 exitos")

    at = _correr_app()
    check(
        "AppTest: Reportes renderiza sin excepciones",
        not at.exception,
        str(at.exception[0].value)[:200] if at.exception else "",
    )

    botones = at.get("download_button")
    check(
        "AppTest: boton 'Descargar Excel (.xlsx)' presente con key dl_reportes_exitos",
        len(botones) == 1
        and "dl_reportes_exitos" in botones[0].id
        and "Descargar Excel" in botones[0].label,
        f"(botones={len(botones)})",
    )

    captions = [c.value for c in at.caption]
    check(
        "AppTest: caption de solo exitosas presente",
        any("acciones exitosas" in c for c in captions),
    )

    tablas = at.dataframe
    valor = tablas[0].value if tablas else None
    filas = valor.to_dict("records") if valor is not None else []
    check(
        "AppTest: dataframe con 5 filas = 3 exitos + 2 vacias",
        len(filas) == 5,
        f"(real={len(filas)})",
    )
    check(
        "AppTest: en pantalla fila 0 = cuenta_0, fila 2 = cuenta_1, fila 4 = cuenta_2",
        len(filas) == 5
        and filas[0]["Usuario"] == "cuenta_0"
        and filas[2]["Usuario"] == "cuenta_1"
        and filas[4]["Usuario"] == "cuenta_2",
    )
    check(
        "AppTest: filas 1 y 3 vacias en pantalla (CSV separado)",
        len(filas) == 5
        and all(str(filas[1].get(c) or "") == "" for c in COLUMNAS)
        and all(str(filas[3].get(c) or "") == "" for c in COLUMNAS),
    )
    check(
        "AppTest: dataframe con las columnas Resultado y Link",
        len(filas) == 5
        and list(valor.columns) == COLUMNAS
        and "Resultado" in valor.columns
        and "Link" in valor.columns,
        f"(columnas={list(valor.columns) if valor is not None else None})",
    )
    check(
        "AppTest: Resultado/Link por fila (post con link, rt sin link, cita con link)",
        len(filas) == 5
        and filas[0]["Resultado"] == "Post"
        and filas[0]["Link"] == "https://x.com/cuenta_0/status/0"
        and filas[2]["Resultado"] == "RT simple (no genera link)"
        and str(filas[2]["Link"] or "") == ""
        and filas[4]["Resultado"] == "Cita (RT con cita)"
        and filas[4]["Link"] == "https://x.com/cuenta_2/status/2",
        f"(real={[filas[i]['Resultado'] for i in (0, 2, 4)] if len(filas) == 5 else None})",
    )

    at_fallo = _correr_app(simular_fallo_excel=True)
    check(
        "AppTest: sin Excel (b'') NO hay boton y si un warning",
        not at_fallo.exception
        and len(at_fallo.get("download_button")) == 0
        and any("No se pudo generar el Excel" in w.value for w in at_fallo.warning),
    )

    at_compat = _correr_app(compat_fallback=True)
    valor_compat = at_compat.dataframe[0].value if at_compat.dataframe else None
    filas_compat = valor_compat.to_dict("records") if valor_compat is not None else []
    check(
        "AppTest: fallback TypeError de obtener_acciones sigue renderizando la tabla",
        not at_compat.exception
        and len(at_compat.get("download_button")) == 1
        and len(filas_compat) == 5
        and filas_compat[0]["Usuario"] == "cuenta_0",
        str(at_compat.exception[0].value)[:200] if at_compat.exception else "",
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    _test_filas_con_separacion(check)
    _test_excel(check)
    _test_es_url_post(check)
    _test_resultado_accion(check)
    _test_pagina_fuente(check)
    _test_app_test(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
