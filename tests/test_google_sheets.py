# -*- coding: utf-8 -*-
"""Tests de `core/google_sheets.py` (reporte del cliente con link).

SIN red real y SIN credenciales verdaderas: `google.oauth2.service_account.
Credentials` y `googleapiclient.discovery.build` se simulan con fakes. El
archivo local se escribe en un directorio TEMPORAL (se parchea `resolver_ruta`)
para NO tocar `data/` del proyecto.

Cubre:
  (1) `estado()`/`disponible()` sin credenciales y con las 5 fuentes (b64,
      JSON crudo, archivo por env, archivo local y GOOGLE_APPLICATION_CREDENTIALS),
      prioridad y que la private key NUNCA aparezca.
  (2) `guardar_credenciales_json` rechaza basura/campos faltantes y guarda el
      JSON valido de forma atomica en el temp dir.
  (3) `resumen_pedidos`/`construir_datos_sheet`: solo exitosos, seguidores UNA
      vez y aparte, columnas dinamicas y CERO palabras internas del proveedor.
  (4) `crear_sheet_reporte` con fakes: titulo + hojas Resumen/Detalle, values,
      batchUpdate de formato, permisos anyone/reader y por correo, URL de
      Sheets y errores de Google tolerados (`ok=False`, nunca excepcion).
  (5) Casos borde: sin email y sin enlace publico, correo invalido, sin
      exitos, pedidos raros y datos vacios.

Uso:
    .venv/Scripts/python.exe tests/test_google_sheets.py
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
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

from core import google_sheets  # noqa: E402

SA_EMAIL = "reportes@mw-test.iam.gserviceaccount.com"
SA_EMAIL_2 = "otra@mw-test.iam.gserviceaccount.com"
MARCA_SECRETA = "MWXSECRETO123"
SA_JSON = {
    "type": "service_account",
    "project_id": "mw-test",
    "private_key_id": "abc123",
    "private_key": (
        "-----BEGIN PRIVATE KEY-----\n"
        f"{MARCA_SECRETA}\n"
        "-----END PRIVATE KEY-----\n"
    ),
    "client_email": SA_EMAIL,
    "client_id": "123456789",
    "token_uri": "https://oauth2.googleapis.com/token",
}
SA_B64 = base64.b64encode(
    json.dumps(SA_JSON).encode("utf-8")
).decode("ascii")
SA_JSON_2 = dict(SA_JSON, client_email=SA_EMAIL_2)

LINK_A = "https://x.com/cliente/status/1001"
LINK_B = "https://x.com/cliente/status/1002"
LINK_PERFIL = "https://x.com/perfil_demo"
SHEET_ID = "SHEET-TEST-ABC"

PEDIDOS = [
    {"tipo": "likes", "link": LINK_A, "cantidad": 50, "ok": True},
    {"tipo": "retweets", "link": LINK_A, "cantidad": 10, "exito": True},
    {"tipo": "comments", "link": LINK_B, "cantidad": 5, "ok": True},
    {"tipo": "views", "link": LINK_B, "cantidad": 500, "ok": True},
    {"tipo": "followers", "link": LINK_PERFIL, "cantidad": 250, "ok": True},
    {"tipo": "likes", "link": LINK_B, "cantidad": 999, "ok": False},
]

# Subcadenas que NUNCA deben aparecer en el contenido del Sheet.
PROHIBIDOS = (
    "jap",
    "just another panel",
    "proveedor",
    "costo",
    "saldo",
    "api",
    "compra",
    "pedido",
    "9013",
    "8860",
    "8413",
    "6601",
    "6602",
    "10186",
    "8855",
    "9011",
)

_VARS_GOOGLE = (
    "GOOGLE_SERVICE_ACCOUNT_B64",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_SHEETS_SHARE_EMAIL",
    "GOOGLE_SHEETS_PUBLICO",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


# --------------------------------------------------------------------------- #
# Entorno aislado (temp dir + sin variables reales)
# --------------------------------------------------------------------------- #
def _resolver_temporal(base: str):
    """`resolver_ruta` falso: todo lo relativo vive dentro del temp dir."""

    def resolver(relativa):
        ruta = Path(str(relativa))
        if ruta.is_absolute():
            return str(ruta)
        return str((Path(base) / ruta).resolve())

    return resolver


def _settings_vacio() -> SimpleNamespace:
    return SimpleNamespace(
        google_service_account_b64="",
        google_service_account_json="",
        google_service_account_file="",
        google_sheets_share_email="",
        google_sheets_publico=True,
    )


@contextlib.contextmanager
def _entorno(**extra):
    """Temp dir + env limpio de Google; JAMAS toca data/ real."""
    with tempfile.TemporaryDirectory() as tmp:
        limpio = {
            clave: valor
            for clave, valor in os.environ.items()
            if clave not in _VARS_GOOGLE
        }
        limpio.update({clave: str(valor) for clave, valor in extra.items()})
        with (
            mock.patch.dict(os.environ, limpio, clear=True),
            mock.patch.object(google_sheets, "settings", _settings_vacio()),
            mock.patch.object(
                google_sheets, "resolver_ruta", _resolver_temporal(tmp)
            ),
        ):
            yield tmp


def _escribir_json(ruta: Path, datos=None) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(datos if datos is not None else SA_JSON),
        encoding="utf-8",
    )
    return ruta


# --------------------------------------------------------------------------- #
# Fakes de Google API (sin red)
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, respuesta=None, error=None):
        self._respuesta = respuesta
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._respuesta


class _FakeValues:
    def __init__(self, llamadas):
        self._llamadas = llamadas

    def batchUpdate(self, **kwargs):
        self._llamadas.append(
            {"llamada": "values.batchUpdate", "kwargs": kwargs}
        )
        return _FakeRequest({"spreadsheetId": kwargs.get("spreadsheetId")})

    def update(self, **kwargs):
        self._llamadas.append({"llamada": "values.update", "kwargs": kwargs})
        return _FakeRequest({})


class _FakeSpreadsheets:
    def __init__(self, llamadas, create_error=None):
        self._llamadas = llamadas
        self._create_error = create_error

    def create(self, **kwargs):
        self._llamadas.append(
            {"llamada": "spreadsheets.create", "kwargs": kwargs}
        )
        if self._create_error is not None:
            return _FakeRequest(error=self._create_error)
        return _FakeRequest(
            {
                "spreadsheetId": SHEET_ID,
                "spreadsheetUrl": (
                    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
                ),
                "sheets": [
                    {"properties": {"sheetId": 11, "title": "Resumen"}},
                    {"properties": {"sheetId": 22, "title": "Detalle"}},
                ],
            }
        )

    def values(self):
        return _FakeValues(self._llamadas)

    def batchUpdate(self, **kwargs):
        self._llamadas.append(
            {"llamada": "spreadsheets.batchUpdate", "kwargs": kwargs}
        )
        return _FakeRequest({"replies": []})


class _FakeSheetsService:
    def __init__(self, llamadas, create_error=None):
        self._spreadsheets = _FakeSpreadsheets(llamadas, create_error)

    def spreadsheets(self):
        return self._spreadsheets


class _FakePermissions:
    def __init__(self, llamadas):
        self._llamadas = llamadas

    def create(self, **kwargs):
        self._llamadas.append(
            {"llamada": "permissions.create", "kwargs": kwargs}
        )
        return _FakeRequest({"id": "permiso-1"})


class _FakeDriveService:
    def __init__(self, llamadas):
        self._llamadas = llamadas

    def permissions(self):
        return _FakePermissions(self._llamadas)


@contextlib.contextmanager
def _google_falso(llamadas, create_error=None):
    """Parchea credentials + build; devuelve el mock de Credentials."""
    sheets = _FakeSheetsService(llamadas, create_error)
    drive = _FakeDriveService(llamadas)

    def fake_build(servicio, version, **kwargs):
        llamadas.append(
            {
                "llamada": "build",
                "servicio": servicio,
                "version": version,
                "kwargs": kwargs,
            }
        )
        return sheets if servicio == "sheets" else drive

    with (
        mock.patch(
            "google.oauth2.service_account.Credentials."
            "from_service_account_info",
            return_value=object(),
        ) as credenciales,
        mock.patch(
            "googleapiclient.discovery.build", side_effect=fake_build
        ),
    ):
        yield credenciales


def _llamadas_de(llamadas, nombre):
    return [
        item
        for item in llamadas
        if isinstance(item, dict) and item.get("llamada") == nombre
    ]


# --------------------------------------------------------------------------- #
# Helpers de las filas
# --------------------------------------------------------------------------- #
def _mapa_resumen(filas):
    mapa = {}
    for fila in filas:
        if isinstance(fila, list) and len(fila) >= 2 and str(fila[0]):
            mapa[str(fila[0])] = fila[1]
    return mapa


def _texto_sheet(datos):
    partes = [str(datos.get("titulo_documento") or "")]
    for hoja in ("resumen", "detalle"):
        for fila in (datos.get(hoja) or {}).get("filas") or []:
            for celda in fila or []:
                partes.append(str(celda))
    return "\n".join(partes)


def _fuga_prohibida(datos):
    texto = _texto_sheet(datos).lower()
    return [palabra for palabra in PROHIBIDOS if palabra in texto]


# --------------------------------------------------------------------------- #
# (1) Estado de las credenciales
# --------------------------------------------------------------------------- #
def test_estado_sin_credenciales(check):
    with _entorno():
        estado = google_sheets.estado()
        disponible = google_sheets.disponible()
    check(
        "estado sin credenciales: no disponible",
        estado.get("disponible") is False,
        str(estado),
    )
    check(
        "estado sin credenciales: motivo claro y sin fuente",
        bool(str(estado.get("motivo") or "").strip())
        and estado.get("fuente") == ""
        and estado.get("email") == "",
        str(estado),
    )
    check(
        "disponible() sin credenciales es False",
        disponible is False,
        str(disponible),
    )


def test_estado_con_b64(check):
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        estado = google_sheets.estado()
        disponible = google_sheets.disponible()
    check(
        "estado con base64: disponible",
        estado.get("disponible") is True,
        str(estado),
    )
    check(
        "estado con base64: fuente y email correctos",
        estado.get("fuente") == "env_b64"
        and estado.get("email") == SA_EMAIL,
        str(estado),
    )
    check(
        "estado con base64: no expone private_key ni la marca secreta",
        "private_key" not in estado
        and MARCA_SECRETA not in json.dumps(estado, ensure_ascii=False),
        str(estado),
    )
    check("disponible() con base64 es True", disponible is True, str(disponible))


def test_estado_json_y_prioridad(check):
    with _entorno(GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps(SA_JSON)):
        estado = google_sheets.estado()
    check(
        "estado con JSON crudo: disponible y fuente env_json",
        estado.get("disponible") is True
        and estado.get("fuente") == "env_json"
        and estado.get("email") == SA_EMAIL,
        str(estado),
    )
    # base64 gana sobre el JSON crudo.
    with _entorno(
        GOOGLE_SERVICE_ACCOUNT_B64=SA_B64,
        GOOGLE_SERVICE_ACCOUNT_JSON=json.dumps(SA_JSON_2),
    ):
        estado = google_sheets.estado()
    check(
        "estado: GOOGLE_SERVICE_ACCOUNT_B64 tiene prioridad",
        estado.get("fuente") == "env_b64"
        and estado.get("email") == SA_EMAIL,
        str(estado),
    )
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64="esto-no-es-base64"):
        estado = google_sheets.estado()
    check(
        "estado con base64 invalido: no disponible y avisa",
        estado.get("disponible") is False
        and estado.get("fuente") == "env_b64"
        and "no es valida" in str(estado.get("motivo") or "").lower(),
        str(estado),
    )


def test_estado_archivos(check):
    with _entorno() as tmp:
        ruta = _escribir_json(Path(tmp) / "cuenta.json")
        with mock.patch.dict(
            os.environ, {"GOOGLE_SERVICE_ACCOUNT_FILE": str(ruta)}
        ):
            estado = google_sheets.estado()
        check(
            "estado con GOOGLE_SERVICE_ACCOUNT_FILE: fuente env_file",
            estado.get("disponible") is True
            and estado.get("fuente") == "env_file"
            and estado.get("email") == SA_EMAIL,
            str(estado),
        )
    with _entorno() as tmp:
        _escribir_json(
            Path(tmp) / "data" / "google_service_account.json"
        )
        estado = google_sheets.estado()
        check(
            "estado con data/google_service_account.json: fuente archivo",
            estado.get("disponible") is True
            and estado.get("fuente") == "archivo"
            and estado.get("email") == SA_EMAIL,
            str(estado),
        )
    with _entorno() as tmp:
        ruta = _escribir_json(Path(tmp) / "gac.json")
        with mock.patch.dict(
            os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(ruta)}
        ):
            estado = google_sheets.estado()
        check(
            "estado con GOOGLE_APPLICATION_CREDENTIALS: ultimo fallback",
            estado.get("disponible") is True
            and estado.get("fuente") == "application_credentials"
            and estado.get("email") == SA_EMAIL,
            str(estado),
        )
    with _entorno():
        estado = google_sheets.estado()
    check(
        "fuente permitida por el contrato en todos los casos",
        estado.get("fuente")
        in (
            "",
            "env_b64",
            "env_json",
            "env_file",
            "archivo",
            "application_credentials",
        ),
        str(estado),
    )


# --------------------------------------------------------------------------- #
# (2) Guardar credenciales
# --------------------------------------------------------------------------- #
def test_guardar_credenciales(check):
    with _entorno() as tmp:
        ruta = Path(tmp) / "data" / "google_service_account.json"

        invalido = google_sheets.guardar_credenciales_json("no es json")
        check(
            "guardar: rechaza un contenido que no es JSON",
            invalido.get("ok") is False and bool(invalido.get("error")),
            str(invalido),
        )
        check(
            "guardar invalido: NO crea el archivo",
            not ruta.exists(),
            str(ruta),
        )

        sin_clave = dict(SA_JSON)
        sin_clave.pop("private_key", None)
        resultado = google_sheets.guardar_credenciales_json(
            json.dumps(sin_clave)
        )
        check(
            "guardar: rechaza JSON sin private_key",
            resultado.get("ok") is False
            and "private_key" in str(resultado.get("error") or "").lower(),
            str(resultado),
        )

        sin_token = dict(SA_JSON)
        sin_token.pop("token_uri", None)
        resultado = google_sheets.guardar_credenciales_json(
            json.dumps(sin_token)
        )
        check(
            "guardar: rechaza JSON sin token_uri",
            resultado.get("ok") is False
            and "token_uri" in str(resultado.get("error") or "").lower(),
            str(resultado),
        )

        resultado = google_sheets.guardar_credenciales_json(
            json.dumps(SA_JSON)
        )
        check(
            "guardar: acepta un JSON valido y devuelve el email",
            resultado.get("ok") is True
            and resultado.get("email") == SA_EMAIL
            and resultado.get("error") == "",
            str(resultado),
        )
        check("guardar: escribe el archivo temporal", ruta.is_file(), str(ruta))
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        check(
            "guardar: el archivo conserva client_email/token_uri",
            datos.get("client_email") == SA_EMAIL
            and datos.get("token_uri") == SA_JSON["token_uri"],
            str(list(datos.keys())),
        )
        check(
            "guardar: el resultado no filtra la private key",
            set(resultado.keys()) == {"ok", "email", "error"}
            and MARCA_SECRETA not in json.dumps(resultado),
            str(resultado),
        )
        if os.name != "nt":
            permisos = os.stat(ruta).st_mode & 0o777
            check(
                "guardar: permisos 0600 en POSIX",
                permisos == 0o600,
                oct(permisos),
            )

        estado = google_sheets.estado()
        check(
            "guardar: estado() lo detecta como archivo local",
            estado.get("disponible") is True
            and estado.get("fuente") == "archivo"
            and estado.get("email") == SA_EMAIL,
            str(estado),
        )

        resultado_b64 = google_sheets.guardar_credenciales_json(SA_B64)
        check(
            "guardar: tambien acepta el JSON en base64",
            resultado_b64.get("ok") is True
            and resultado_b64.get("email") == SA_EMAIL,
            str(resultado_b64),
        )


# --------------------------------------------------------------------------- #
# (3) Datos del Sheet (puro)
# --------------------------------------------------------------------------- #
def test_resumen_y_datos(check):
    resumen = google_sheets.resumen_pedidos(PEDIDOS)
    check(
        "resumen: cuenta pedidos totales/exitosos/fallidos",
        resumen["pedidos_totales"] == 6
        and resumen["pedidos_exitosos"] == 5
        and resumen["pedidos_fallidos"] == 1,
        str(resumen)[:300],
    )
    check(
        "resumen: totales por tipo y engagement",
        resumen["por_tipo"]["likes"] == 50
        and resumen["por_tipo"]["retweets"] == 10
        and resumen["por_tipo"]["comments"] == 5
        and resumen["por_tipo"]["views"] == 500
        and resumen["por_tipo"]["followers"] == 250
        and resumen["acciones_total"] == 565,
        str(resumen["por_tipo"]),
    )
    check(
        "resumen: seguidores aparte y 2 publicaciones",
        resumen["seguidores"] == 250
        and resumen["total_con_seguidores"] == 815
        and resumen["publicaciones"] == 2,
        str(resumen)[:300],
    )
    check(
        "resumen: el pedido fallido (999) no entra",
        resumen["links"] == [LINK_A, LINK_B]
        and resumen["por_link"][LINK_B]["likes"] == 0,
        str(resumen["por_link"]),
    )

    datos = google_sheets.construir_datos_sheet(
        PEDIDOS,
        cliente="Cliente Demo",
        campana="Campaña Demo",
        fecha="2026-11-27T12:00:00",
        folio="LOTE-DEMO",
        notas="Reporte listo",
    )
    filas_resumen = datos["resumen"]["filas"]
    mapa = _mapa_resumen(filas_resumen)
    check(
        "sheet Resumen: titulo y metadatos",
        filas_resumen[0] == ["Reporte de acciones realizadas"]
        and mapa.get("Cliente") == "Cliente Demo"
        and mapa.get("Campaña") == "Campaña Demo"
        and mapa.get("Fecha") == "27/11/2026"
        and mapa.get("Folio") == "LOTE-DEMO",
        str(filas_resumen[:7]),
    )
    check(
        "sheet Resumen: tipos con cantidad > 0",
        mapa.get("Likes") == 50
        and mapa.get("Retweets") == 10
        and mapa.get("Comentarios") == 5
        and mapa.get("Vistas") == 500
        and mapa.get("Seguidores") == 250,
        str(mapa),
    )
    check(
        "sheet Resumen: total de acciones y publicaciones",
        ["Total de acciones", 565] in filas_resumen
        and ["Publicaciones", 2] in filas_resumen,
        str(filas_resumen),
    )
    check(
        "sheet Resumen: notas presentes",
        any(fila == ["Notas"] for fila in filas_resumen)
        and any(
            isinstance(fila, list) and fila and fila[0] == "Reporte listo"
            for fila in filas_resumen
        ),
        str(filas_resumen),
    )
    check(
        "sheet Resumen: encabezado 'Acción'/'Cantidad' y totales enteros",
        any(
            fila == ["Acción", "Cantidad"] for fila in filas_resumen
        )
        and all(
            isinstance(fila[1], int)
            for fila in filas_resumen
            if isinstance(fila, list)
            and len(fila) == 2
            and fila[0]
            in (
                "Likes",
                "Retweets",
                "Comentarios",
                "Vistas",
                "Seguidores",
                "Total de acciones",
                "Publicaciones",
            )
        ),
        str(filas_resumen),
    )
    check(
        "sheet Resumen: el 999 fallido no aparece",
        all(999 not in fila for fila in filas_resumen),
        str(filas_resumen),
    )

    detalle = datos["detalle"]["filas"]
    check(
        "sheet Detalle: encabezado con columnas dinamicas",
        detalle[0]
        == ["Publicación", "Likes", "Retweets", "Comentarios", "Vistas"],
        str(detalle[0]),
    )
    fila_a = next((f for f in detalle if f and f[0] == LINK_A), [])
    fila_b = next((f for f in detalle if f and f[0] == LINK_B), [])
    check(
        "sheet Detalle: cantidades por publicacion",
        fila_a == [LINK_A, 50, 10, 0, 0]
        and fila_b == [LINK_B, 0, 0, 5, 500],
        f"(A={fila_a}, B={fila_b})",
    )
    filas_seguidores = [
        fila for fila in detalle if fila and fila[0] == "Seguidores"
    ]
    check(
        "sheet Detalle: seguidores UNA sola vez y aparte",
        len(filas_seguidores) == 1
        and filas_seguidores[0][1] == 250,
        str(filas_seguidores),
    )
    check(
        "sheet Detalle: los seguidores no cuentan como publicacion",
        datos["publicaciones"] == 2
        and all(
            not any(LINK_PERFIL == str(celda) for celda in (fila or []))
            for fila in detalle
            if not (fila and fila[0] == "Seguidores")
        ),
        str(detalle),
    )
    check(
        "sheet: sin palabras internas del proveedor/API/IDs",
        _fuga_prohibida(datos) == [],
        str(_fuga_prohibida(datos)),
    )
    check(
        "sheet: titulo del documento contiene el titulo del reporte",
        datos["titulo_documento"].startswith(
            "Reporte de acciones realizadas"
        ),
        datos["titulo_documento"],
    )


def test_datos_dinamicos_y_bordes(check):
    solo_likes = google_sheets.construir_datos_sheet(
        [{"tipo": "likes", "link": LINK_A, "cantidad": 10, "ok": True}]
    )
    check(
        "detalle dinamico: sin Vistas/Comentarios no aparecen columnas",
        solo_likes["detalle"]["filas"][0]
        == ["Publicación", "Likes"]
        and solo_likes["columnas_detalle"] == ["likes"],
        str(solo_likes["detalle"]["filas"]),
    )

    solo_seguidores = google_sheets.construir_datos_sheet(
        [
            {"tipo": "followers", "link": LINK_PERFIL, "cantidad": 100, "ok": True},
            {"tipo": "seguidores", "link": LINK_PERFIL, "cantidad": 50, "ok": True},
        ]
    )
    check(
        "solo seguidores: se suman y aparecen una vez",
        solo_seguidores["seguidores"] == 150
        and solo_seguidores["publicaciones"] == 0
        and [
            fila
            for fila in solo_seguidores["detalle"]["filas"]
            if fila and fila[0] == "Seguidores"
        ]
        == [["Seguidores", 150, LINK_PERFIL]],
        str(solo_seguidores["detalle"]["filas"]),
    )
    check(
        "solo seguidores: el Resumen no inventa publicaciones",
        ["Publicaciones", 0] in solo_seguidores["resumen"]["filas"],
        str(solo_seguidores["resumen"]["filas"]),
    )

    vacio = google_sheets.construir_datos_sheet([])
    basura = google_sheets.construir_datos_sheet(["basura", None, 3])
    check(
        "sin datos: 'Sin acciones registradas' y hay_exitos False",
        vacio["hay_exitos"] is False
        and basura["hay_exitos"] is False
        and any(
            fila == ["Sin acciones registradas", ""]
            for fila in vacio["resumen"]["filas"]
        ),
        str(vacio["resumen"]["filas"]),
    )
    check(
        "sin datos: resumen_pedidos no lanza con entradas raras",
        google_sheets.resumen_pedidos(None)["hay_exitos"] is False
        and google_sheets.resumen_pedidos("basura")["pedidos_totales"] == 0,
    )

    formula = google_sheets.construir_datos_sheet(
        PEDIDOS, cliente="=SUM(A1:A2)"
    )
    check(
        "seguridad: un cliente que empieza con '=' se escribe como texto",
        ["Cliente", "'=SUM(A1:A2)"] in formula["resumen"]["filas"],
        str(formula["resumen"]["filas"][:5]),
    )

    alias = google_sheets.construir_datos_sheet(
        [
            {"tipo": "Me Gusta", "link": LINK_A, "cantidad": "7", "exito": "si"},
            {"tipo": "RT", "link": LINK_A, "cantidad": "3", "exito": "1"},
        ]
    )
    check(
        "tipos: alias ES/EN ('Me Gusta', 'RT') y cantidades string",
        alias["por_tipo"]["likes"] == 7
        and alias["por_tipo"]["retweets"] == 3
        and alias["acciones_total"] == 10,
        str(alias["por_tipo"]),
    )


# --------------------------------------------------------------------------- #
# (4) Crear el Sheet con Google simulado
# --------------------------------------------------------------------------- #
def test_crear_sheet_publico(check):
    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas) as credenciales:
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS,
                cliente="Cliente Demo",
                campana="Campaña Demo",
                fecha="2026-11-27T12:00:00",
                folio="LOTE-DEMO",
            )
    check(
        "crear sheet: ok, id y URL de Google Sheets",
        resultado.get("ok") is True
        and resultado.get("spreadsheet_id") == SHEET_ID
        and resultado.get("url")
        == f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
        str(resultado)[:400],
    )
    check(
        "crear sheet: titulo del documento devuelto",
        resultado.get("titulo", "").startswith(
            "Reporte de acciones realizadas"
        ),
        str(resultado.get("titulo")),
    )
    check(
        "crear sheet: usa las credenciales de la cuenta de servicio",
        credenciales.call_count == 1,
        str(credenciales.call_count),
    )

    builds = _llamadas_de(llamadas, "build")
    check(
        "crear sheet: construye clientes de Sheets v4 y Drive v3",
        [(b["servicio"], b["version"]) for b in builds]
        == [("sheets", "v4"), ("drive", "v3")],
        str(builds),
    )
    creaciones = _llamadas_de(llamadas, "spreadsheets.create")
    check(
        "crear sheet: spreadsheets.create con titulo y 2 hojas",
        len(creaciones) == 1
        and creaciones[0]["kwargs"]["body"]["properties"]["title"]
        == resultado["titulo"]
        and [
            hoja["properties"]["title"]
            for hoja in creaciones[0]["kwargs"]["body"]["sheets"]
        ]
        == ["Resumen", "Detalle"],
        str(creaciones[:1])[:500],
    )
    valores = _llamadas_de(llamadas, "values.batchUpdate")
    check(
        "crear sheet: values.batchUpdate recibe Resumen y Detalle",
        len(valores) == 1
        and {
            dato["range"] for dato in valores[0]["kwargs"]["body"]["data"]
        }
        == {"'Resumen'!A1", "'Detalle'!A1"},
        str(valores[:1])[:500],
    )
    filas_resumen = next(
        dato["values"]
        for dato in valores[0]["kwargs"]["body"]["data"]
        if dato["range"] == "'Resumen'!A1"
    )
    filas_detalle = next(
        dato["values"]
        for dato in valores[0]["kwargs"]["body"]["data"]
        if dato["range"] == "'Detalle'!A1"
    )
    check(
        "crear sheet: los valores incluyen totales y detalle",
        ["Likes", 50] in filas_resumen
        and ["Total de acciones", 565] in filas_resumen
        and any(fila and fila[0] == LINK_A for fila in filas_detalle),
        str(filas_resumen),
    )
    formato = _llamadas_de(llamadas, "spreadsheets.batchUpdate")
    check(
        "crear sheet: batchUpdate de formato con titulo/encabezados/congelado",
        len(formato) == 1
        and len(formato[0]["kwargs"]["body"]["requests"]) >= 5
        and any(
            "repeatCell" in request
            for request in formato[0]["kwargs"]["body"]["requests"]
        )
        and any(
            "updateSheetProperties" in request
            for request in formato[0]["kwargs"]["body"]["requests"]
        )
        and any(
            "autoResizeDimensions" in request
            for request in formato[0]["kwargs"]["body"]["requests"]
        ),
        str(formato[:1])[:500],
    )
    permisos = _llamadas_de(llamadas, "permissions.create")
    check(
        "crear sheet: permissions.create anyone/reader por defecto",
        len(permisos) == 1
        and permisos[0]["kwargs"]["body"]
        == {"type": "anyone", "role": "reader"}
        and permisos[0]["kwargs"]["fileId"] == SHEET_ID,
        str(permisos[:1]),
    )
    check(
        "crear sheet: 'compartido' reporta el enlace publico",
        isinstance(resultado.get("compartido"), dict)
        and resultado["compartido"].get("publico") is True
        and resultado["compartido"].get("ok") is True,
        str(resultado.get("compartido")),
    )


def test_crear_sheet_email(check):
    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS,
                cliente="Cliente Demo",
                compartir_publico=True,
                email="jefe@empresa.com",
            )
    permisos = _llamadas_de(llamadas, "permissions.create")
    check(
        "crear sheet con correo: publico + correo lector",
        resultado.get("ok") is True
        and len(permisos) == 2
        and permisos[0]["kwargs"]["body"]["type"] == "anyone"
        and permisos[1]["kwargs"]["body"]
        == {
            "type": "user",
            "role": "reader",
            "emailAddress": "jefe@empresa.com",
        },
        str(permisos),
    )
    check(
        "crear sheet con correo: 'compartido' lo registra",
        resultado["compartido"].get("email") == "jefe@empresa.com"
        and len(resultado["compartido"].get("permisos") or []) == 2,
        str(resultado.get("compartido")),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS,
                compartir_publico=False,
                email="jefe@empresa.com",
            )
    permisos = _llamadas_de(llamadas, "permissions.create")
    check(
        "crear sheet privado: solo comparte con el correo",
        resultado.get("ok") is True
        and len(permisos) == 1
        and permisos[0]["kwargs"]["body"].get("type") == "user"
        and resultado["compartido"].get("publico") is False,
        str(resultado)[:400],
    )

    llamadas = []
    with _entorno(
        GOOGLE_SERVICE_ACCOUNT_B64=SA_B64,
        GOOGLE_SHEETS_SHARE_EMAIL="configurado@empresa.com",
    ):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS, compartir_publico=True
            )
    permisos = _llamadas_de(llamadas, "permissions.create")
    check(
        "crear sheet: usa GOOGLE_SHEETS_SHARE_EMAIL si no se pasa correo",
        resultado.get("ok") is True
        and len(permisos) == 2
        and permisos[1]["kwargs"]["body"].get("emailAddress")
        == "configurado@empresa.com",
        str(permisos),
    )

    llamadas = []
    with _entorno(
        GOOGLE_SERVICE_ACCOUNT_B64=SA_B64,
        GOOGLE_SHEETS_PUBLICO="0",
        GOOGLE_SHEETS_SHARE_EMAIL="jefe@empresa.com",
    ):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS, compartir_publico=None
            )
    permisos = _llamadas_de(llamadas, "permissions.create")
    check(
        "crear sheet: GOOGLE_SHEETS_PUBLICO=0 desactiva el enlace publico",
        resultado.get("ok") is True
        and len(permisos) == 1
        and permisos[0]["kwargs"]["body"].get("type") == "user",
        str(permisos),
    )


def test_crear_sheet_sin_permiso(check):
    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS, compartir_publico=False, email=""
            )
    check(
        "crear sheet sin email ni enlace publico: error claro",
        resultado.get("ok") is False
        and "correo" in str(resultado.get("error") or "").lower()
        and resultado.get("spreadsheet_id") == "",
        str(resultado),
    )
    check(
        "crear sheet sin permisos: NO crea Sheet ni pide permisos",
        _llamadas_de(llamadas, "spreadsheets.create") == []
        and _llamadas_de(llamadas, "permissions.create") == []
        and _llamadas_de(llamadas, "build") == [],
        str(llamadas),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                PEDIDOS, email="esto-no-es-un-correo"
            )
    check(
        "crear sheet: correo invalido se rechaza antes de llamar a Google",
        resultado.get("ok") is False
        and _llamadas_de(llamadas, "spreadsheets.create") == [],
        str(resultado),
    )

    llamadas = []
    with _entorno():
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(PEDIDOS)
    check(
        "crear sheet sin credenciales: error y sin llamadas a Google",
        resultado.get("ok") is False
        and "credencial" in str(resultado.get("error") or "").lower()
        and _llamadas_de(llamadas, "spreadsheets.create") == [],
        str(resultado),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas):
            resultado = google_sheets.crear_sheet_reporte(
                [{"tipo": "likes", "link": LINK_A, "cantidad": 0, "ok": True}]
            )
    check(
        "crear sheet sin acciones exitosas: error y sin llamadas",
        resultado.get("ok") is False
        and "acciones exitosas" in str(resultado.get("error") or "").lower()
        and _llamadas_de(llamadas, "spreadsheets.create") == [],
        str(resultado),
    )


# --------------------------------------------------------------------------- #
# (5) Tolerancia a errores de Google
# --------------------------------------------------------------------------- #
def test_crear_sheet_errores_google(check):
    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(llamadas, create_error=RuntimeError("boom")):
            resultado = google_sheets.crear_sheet_reporte(PEDIDOS)
    check(
        "error de Google: ok=False con mensaje y sin excepcion",
        resultado.get("ok") is False
        and bool(resultado.get("error"))
        and resultado.get("spreadsheet_id") == "",
        str(resultado),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(
            llamadas,
            create_error=RuntimeError(
                "The caller does not have permission (403)"
            ),
        ):
            resultado = google_sheets.crear_sheet_reporte(PEDIDOS)
    check(
        "error 403: se traduce a un mensaje de permisos",
        resultado.get("ok") is False
        and "permiso" in str(resultado.get("error") or "").lower(),
        str(resultado),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(
            llamadas,
            create_error=RuntimeError("Quota exceeded for quota metric 429"),
        ):
            resultado = google_sheets.crear_sheet_reporte(PEDIDOS)
    check(
        "error 429: se traduce a cuota",
        resultado.get("ok") is False
        and "cuota" in str(resultado.get("error") or "").lower(),
        str(resultado),
    )

    llamadas = []
    with _entorno(GOOGLE_SERVICE_ACCOUNT_B64=SA_B64):
        with _google_falso(
            llamadas,
            create_error=RuntimeError(
                "Google Sheets API has not been used in project 1"
            ),
        ):
            resultado = google_sheets.crear_sheet_reporte(PEDIDOS)
    check(
        "error de API deshabilitada: se explica en espanol",
        resultado.get("ok") is False
        and "no esta habilitada" in str(resultado.get("error") or "").lower(),
        str(resultado),
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta todos los checks con el `check` del runner (o local)."""
    test_estado_sin_credenciales(check)
    test_estado_con_b64(check)
    test_estado_json_y_prioridad(check)
    test_estado_archivos(check)
    test_guardar_credenciales(check)
    test_resumen_y_datos(check)
    test_datos_dinamicos_y_bordes(check)
    test_crear_sheet_publico(check)
    test_crear_sheet_email(check)
    test_crear_sheet_sin_permiso(check)
    test_crear_sheet_errores_google(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_google_sheets.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
