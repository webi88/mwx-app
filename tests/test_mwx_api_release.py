# -*- coding: utf-8 -*-
"""Tests de los endpoints públicos de releases de la API (`api/main.py`).

Cubre, sin red y sin tocar la base real (las funciones de `core.app_releases`
se sustituyen por mocks):
  - `GET /v1/app/version` sin release y con release (metadatos, sin contenido);
  - `GET /v1/app/version?plataforma=`: default `windows` (compatibilidad con el
    .exe Windows), `mac-arm64`/`mac-intel` (normaliza `_` y mayusculas);
  - `GET /v1/app/descargar` con ZIP (200, headers) y sin release (404 uniforme);
  - `GET /v1/app/descargar?plataforma=mac-arm64` sin version devuelve SU ZIP;
    una version explicita manda e ignora la plataforma;
  - que no se exige Bearer (el .exe debe poder actualizarse antes del login);
  - que `/health` conserva su contrato.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_api_release.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("OPENAI_API_KEY", "sk-test-mwx-release")
os.environ.setdefault("SECRET_KEY", "mwx-release-test")

_CLAVES_VERSION = {
    "ok",
    "disponible",
    "version",
    "obligatorio",
    "notas",
    "sha256",
    "tamano",
    "filename",
    "fecha",
}


def _release_meta() -> dict:
    return {
        "disponible": True,
        "version": "1.2.0",
        "obligatorio": True,
        "notas": "Corrige el compositor y mejora la velocidad.",
        "sha256": "a" * 64,
        "tamano": 34567890,
        "filename": "MWX-APP-1.2.0.zip",
        "fecha": "2026-11-23T10:00:00",
    }


def _test_version(check, cliente, releases) -> None:
    with mock.patch.object(
        releases, "ultimo_release", return_value={"disponible": False}
    ):
        respuesta = cliente.get("/v1/app/version")
    check(
        "api release: version sin release -> 200",
        respuesta.status_code == 200,
        str(respuesta.status_code),
    )
    check(
        "api release: version sin release payload exacto",
        respuesta.json() == {"ok": True, "disponible": False, "version": ""},
        str(respuesta.json()),
    )

    with mock.patch.object(
        releases, "ultimo_release", return_value=_release_meta()
    ):
        respuesta = cliente.get("/v1/app/version")
    cuerpo = respuesta.json()
    check(
        "api release: version con release -> 200",
        respuesta.status_code == 200,
        str(respuesta.status_code),
    )
    check(
        "api release: version con release contrato completo",
        set(cuerpo) == _CLAVES_VERSION,
        str(set(cuerpo)),
    )
    check("api release: version remota", cuerpo.get("version") == "1.2.0")
    check("api release: obligatorio booleano", cuerpo.get("obligatorio") is True)
    check("api release: notas", cuerpo.get("notas") == _release_meta()["notas"])
    check("api release: sha256", cuerpo.get("sha256") == "a" * 64)
    check("api release: tamano entero", cuerpo.get("tamano") == 34567890)
    check("api release: filename", cuerpo.get("filename") == "MWX-APP-1.2.0.zip")
    check("api release: fecha ISO", cuerpo.get("fecha") == "2026-11-23T10:00:00")
    check(
        "api release: version NO carga el contenido",
        "contenido" not in cuerpo,
    )

    with mock.patch.object(releases, "ultimo_release", return_value={}):
        respuesta = cliente.get("/v1/app/version")
    check(
        "api release: metadatos vacios equivalen a sin release",
        respuesta.status_code == 200
        and respuesta.json() == {"ok": True, "disponible": False, "version": ""},
        str(respuesta.json()),
    )


def _test_descargar(check, cliente, releases) -> None:
    contenido = b"PK\x03\x04contenido-zip-falso"
    pedidos = []

    def _leer(version="", plataforma=""):
        pedidos.append((version, plataforma))
        return {
            "disponible": True,
            "version": "1.2.0",
            "filename": "MWX-APP-1.2.0.zip",
            "contenido": contenido,
        }

    with mock.patch.object(releases, "leer_release", _leer):
        respuesta = cliente.get("/v1/app/descargar?version=1.2.0")

    check(
        "api release: descargar -> 200",
        respuesta.status_code == 200,
        str(respuesta.status_code),
    )
    check(
        "api release: descargar devuelve los bytes",
        respuesta.content == contenido,
    )
    check(
        "api release: descargar content-type zip",
        respuesta.headers.get("content-type", "").startswith("application/zip"),
        respuesta.headers.get("content-type", ""),
    )
    check(
        "api release: descargar content-disposition attachment",
        respuesta.headers.get("content-disposition")
        == 'attachment; filename="MWX-APP-1.2.0.zip"',
        respuesta.headers.get("content-disposition", ""),
    )
    check(
        "api release: descargar content-length",
        respuesta.headers.get("content-length") == str(len(contenido)),
        respuesta.headers.get("content-length", ""),
    )
    check(
        "api release: descargar uso el parametro version sin plataforma",
        pedidos == [("1.2.0", "")],
        str(pedidos),
    )
    check(
        "api release: descargar no exige Bearer",
        "authorization" not in {clave.lower() for clave in respuesta.request.headers},
    )

    with mock.patch.object(releases, "leer_release", _leer):
        cliente.get("/v1/app/descargar")
    check(
        "api release: descargar sin version pide el ultimo de windows",
        pedidos[-1] == ("", "windows"),
        str(pedidos[-1]),
    )

    with mock.patch.object(
        releases, "leer_release", return_value={"disponible": False, "contenido": b""}
    ):
        respuesta = cliente.get("/v1/app/descargar?version=9.9.9")
    check(
        "api release: descargar sin release -> 404",
        respuesta.status_code == 404,
        str(respuesta.status_code),
    )
    check(
        "api release: descargar sin release error uniforme",
        respuesta.json() == {"ok": False, "error": "actualización no disponible"},
        str(respuesta.json()),
    )

    with mock.patch.object(
        releases,
        "leer_release",
        return_value={
            "disponible": True,
            "version": "1.2.0",
            "filename": "MWX-APP-1.2.0.zip",
            "contenido": b"",
        },
    ):
        respuesta = cliente.get("/v1/app/descargar")
    check(
        "api release: descargar con contenido vacio -> 404",
        respuesta.status_code == 404,
        str(respuesta.status_code),
    )


def _test_version_plataforma(check, cliente, releases) -> None:
    llamadas = []
    metas = {
        "windows": _release_meta(),
        "mac-arm64": {
            **_release_meta(),
            "version": "1.1.4-mac-arm64",
            "filename": "MWX-APP-mac-arm64-1.1.4.zip",
        },
        "mac-intel": {
            **_release_meta(),
            "version": "1.1.4-mac-intel",
            "filename": "MWX-APP-mac-intel-1.1.4.zip",
        },
    }

    def _ultimo(plataforma=""):
        llamadas.append(plataforma)
        return dict(metas.get(plataforma, {"disponible": False}))

    with mock.patch.object(releases, "ultimo_release", _ultimo):
        sin_parametro = cliente.get("/v1/app/version")
        arm = cliente.get("/v1/app/version?plataforma=mac-arm64")
        intel = cliente.get("/v1/app/version?plataforma=mac_intel")
        vacia = cliente.get("/v1/app/version?plataforma=")
        mayusculas = cliente.get("/v1/app/version?plataforma=MAC-ARM64")

    check(
        "api release: version sin parametro usa windows",
        llamadas[0] == "windows",
        str(llamadas[0]),
    )
    check(
        "api release: version default devuelve el release Windows",
        sin_parametro.status_code == 200
        and sin_parametro.json().get("version") == "1.2.0",
        str(sin_parametro.json()),
    )
    check(
        "api release: version mac-arm64 pasa la plataforma",
        llamadas[1] == "mac-arm64",
        str(llamadas[1]),
    )
    check(
        "api release: version mac-arm64 devuelve su release",
        arm.status_code == 200
        and arm.json().get("version") == "1.1.4-mac-arm64"
        and arm.json().get("filename") == "MWX-APP-mac-arm64-1.1.4.zip",
        str(arm.json()),
    )
    check(
        "api release: version normaliza guion bajo",
        llamadas[2] == "mac-intel",
        str(llamadas[2]),
    )
    check(
        "api release: version mac-intel devuelve su release",
        intel.json().get("version") == "1.1.4-mac-intel",
        str(intel.json()),
    )
    check(
        "api release: plataforma vacia equivale a windows",
        llamadas[3] == "windows",
        str(llamadas[3]),
    )
    check(
        "api release: plataforma en mayusculas se normaliza",
        llamadas[4] == "mac-arm64",
        str(llamadas[4]),
    )


def _test_descargar_plataforma(check, cliente, releases) -> None:
    contenido_win = b"PK\x03\x04windows"
    contenido_arm = b"PK\x03\x04mac-arm64"
    contenido_intel = b"PK\x03\x04mac-intel"
    llamadas = []

    def _leer(version="", plataforma=""):
        llamadas.append((version, plataforma))
        disponibles = {
            "1.1.4": ("MWX-APP-1.1.4.zip", contenido_win),
            "1.1.4-mac-arm64": (
                "MWX-APP-mac-arm64-1.1.4.zip",
                contenido_arm,
            ),
            "1.1.4-mac-intel": (
                "MWX-APP-mac-intel-1.1.4.zip",
                contenido_intel,
            ),
        }
        if not version:
            if plataforma == "mac-arm64":
                version = "1.1.4-mac-arm64"
            elif plataforma == "mac-intel":
                version = "1.1.4-mac-intel"
            else:
                version = "1.1.4"
        filename, datos = disponibles.get(version, ("", b""))
        if not datos:
            return {"disponible": False, "contenido": b""}
        return {
            "disponible": True,
            "version": version,
            "filename": filename,
            "contenido": datos,
        }

    with mock.patch.object(releases, "leer_release", _leer):
        sin_parametro = cliente.get("/v1/app/descargar")
        arm = cliente.get("/v1/app/descargar?plataforma=mac-arm64")
        intel = cliente.get("/v1/app/descargar?plataforma=mac_intel")
        explicita = cliente.get(
            "/v1/app/descargar?version=1.1.4-mac-intel&plataforma=mac-arm64"
        )

    check(
        "api release: descargar sin parametro usa windows",
        llamadas[0] == ("", "windows"),
        str(llamadas[0]),
    )
    check(
        "api release: descargar default devuelve el ZIP Windows",
        sin_parametro.status_code == 200
        and sin_parametro.content == contenido_win,
    )
    check(
        "api release: descargar mac-arm64 pasa la plataforma",
        llamadas[1] == ("", "mac-arm64"),
        str(llamadas[1]),
    )
    check(
        "api release: descargar mac-arm64 devuelve el ZIP Mac",
        arm.status_code == 200 and arm.content == contenido_arm,
        str(arm.status_code),
    )
    check(
        "api release: descargar mac-arm64 filename del ZIP",
        "mac-arm64" in arm.headers.get("content-disposition", ""),
        arm.headers.get("content-disposition", ""),
    )
    check(
        "api release: descargar normaliza guion bajo",
        llamadas[2] == ("", "mac-intel"),
        str(llamadas[2]),
    )
    check(
        "api release: descargar mac-intel devuelve su ZIP",
        intel.content == contenido_intel,
    )
    check(
        "api release: descargar version explicita ignora plataforma",
        llamadas[3] == ("1.1.4-mac-intel", ""),
        str(llamadas[3]),
    )
    check(
        "api release: descargar version explicita devuelve ese ZIP",
        explicita.status_code == 200 and explicita.content == contenido_intel,
    )

    with mock.patch.object(
        releases,
        "leer_release",
        return_value={"disponible": False, "contenido": b""},
    ):
        faltante = cliente.get("/v1/app/descargar?plataforma=mac-intel")
    check(
        "api release: descargar sin release Mac -> 404 uniforme",
        faltante.status_code == 404
        and faltante.json()
        == {"ok": False, "error": "actualización no disponible"},
        str(faltante.json()),
    )


def _test_health(check, cliente) -> None:
    respuesta = cliente.get("/health")
    check(
        "api release: /health intacto",
        respuesta.status_code == 200
        and respuesta.json()
        == {"ok": True, "servicio": "mwx-api", "version": "1.0"},
        str(respuesta.json()),
    )


def run(check) -> None:
    from fastapi.testclient import TestClient

    import api.main as api_main
    from core import app_releases as releases

    cliente = TestClient(api_main.app)
    _test_version(check, cliente, releases)
    _test_version_plataforma(check, cliente, releases)
    _test_descargar(check, cliente, releases)
    _test_descargar_plataforma(check, cliente, releases)
    _test_health(check, cliente)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
