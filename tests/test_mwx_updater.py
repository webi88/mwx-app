# -*- coding: utf-8 -*-
"""Tests del autoactualizador `mwx_app/updater.py` (sin red ni ejecutar scripts).

Cubre el contrato congelado:
  - `comparar_versiones`, `version_actual` y `_plataforma_actual`;
  - `puede_actualizarse` segun `sys.frozen` y la plataforma (Windows/macOS);
  - `consultar` con `requests.get` simulado (sin release, release nuevo,
    obligatorio, sin red, JSON roto, HTTP 500, error del servidor y la
    plataforma windows/mac-arm64/mac-intel);
  - `descargar` con chunks falsos (sha256 bueno/malo, tamano incompleto,
    cancelacion, 404, destino por defecto, nombre saneado y plataforma);
  - `preparar` con ZIPs temporales (exe anidado, .app de macOS, ruta
    traversal, ZIP sin exe/app, ZIP invalido) verificando `.bat` y `.sh`;
  - `lanzar` con `subprocess.Popen` simulado (cmd.exe en Windows, /bin/sh en
    macOS; nunca ejecuta nada);
  - `actualizar` de punta a punta con responses falsos.

Nunca se toca `data/` real, nunca se abre Chrome y nunca se lanza un script.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import requests  # noqa: E402

from mwx_app import __version__, config, updater  # noqa: E402

_CLAVES_CONSULTA = {
    "ok",
    "disponible",
    "requerida",
    "obligatoria",
    "version",
    "notas",
    "sha256",
    "tamano",
    "url_descarga",
    "error",
}
_CLAVES_DESCARGA = {"ok", "ruta", "bytes", "error"}
_CLAVES_PREPARAR = {"ok", "staging", "source", "bat", "app_dir", "error"}
_CLAVES_ACTUALIZAR = {"ok", "version", "bat", "staging", "app_dir", "error"}


class _Respuesta:
    """Response falsa compatible con lo que usa `updater`."""

    def __init__(
        self,
        estado=200,
        datos=None,
        contenido=b"",
        headers=None,
        trozos=None,
        romper_json=False,
    ):
        self.status_code = estado
        self._datos = datos
        self._contenido = contenido
        self.headers = dict(headers or {})
        self._trozos = trozos
        self._romper_json = romper_json
        self.cerrada = False

    def json(self):
        if self._romper_json:
            raise ValueError("json roto")
        return self._datos

    def iter_content(self, chunk_size=256 * 1024):
        if self._trozos is not None:
            for trozo in self._trozos:
                yield trozo
            return
        for inicio in range(0, len(self._contenido), chunk_size):
            yield self._contenido[inicio : inicio + chunk_size]

    def close(self):
        self.cerrada = True


def _crear_zip(ruta: Path, miembros: dict, exe=True) -> Path:
    """Crea un ZIP temporal con los miembros indicados (sin tocar data/)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as zf:
        for nombre, contenido in miembros.items():
            zf.writestr(nombre, contenido)
        if exe and not any(
            nombre.lower().endswith("mwx app.exe") for nombre in miembros
        ):
            zf.writestr("app/MWX APP.exe", b"exe-falso")
            zf.writestr("app/sub/dll.txt", b"dll-falsa")
    return ruta


def _zip_bytes() -> bytes:
    """Bytes de un ZIP valido con `MWX APP.exe` (para `actualizar`)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MWX APP.exe", b"exe")
    return buffer.getvalue()


@contextmanager
def _entorno_mac(maquina="arm64", congelado=None, ejecutable=None):
    """Simula macOS (sys.platform/os.name/machine) sin una Mac real."""
    with ExitStack() as pila:
        pila.enter_context(mock.patch.object(updater.sys, "platform", "darwin"))
        pila.enter_context(mock.patch.object(updater.os, "name", "posix"))
        pila.enter_context(
            mock.patch.object(updater.platform, "machine", lambda: maquina)
        )
        if congelado is not None:
            pila.enter_context(
                mock.patch.object(updater.sys, "frozen", congelado, create=True)
            )
        if ejecutable is not None:
            pila.enter_context(
                mock.patch.object(updater.sys, "executable", str(ejecutable))
            )
        yield


# ---------------------------------------------------------------------------
# Version / comparacion / gate
# ---------------------------------------------------------------------------
def _test_version(check) -> None:
    check(
        "updater: version_actual coincide con mwx_app.__version__",
        updater.version_actual() == __version__,
        updater.version_actual(),
    )
    casos = [
        ("1.10.0", "1.9.0", 1),
        ("1.9.0", "1.10.0", -1),
        ("1.0.0", "1.0.0", 0),
        ("v1.2", "1.2.0", 0),
        ("1.2.3-beta", "1.2.3", 0),
        ("", "1.0", -1),
        ("2", "10", -1),
        ("10", "2", 1),
        ("1.0.0.1", "1.0.0", 1),
        (None, "", 0),
    ]
    for izquierda, derecha, esperado in casos:
        valor = updater.comparar_versiones(izquierda, derecha)
        check(
            f"updater: comparar_versiones({izquierda!r}, {derecha!r}) == {esperado}",
            valor == esperado,
            str(valor),
        )

    for valor, esperado in [
        ("1.1.4-mac-arm64", "1.1.4"),
        ("1.1.4-mac-intel", "1.1.4"),
        ("1.1.4-mac", "1.1.4"),
        ("1.1.4-MAC-ARM64", "1.1.4"),
        ("1.1.4-macarm64", "1.1.4"),
        ("1.1.4", "1.1.4"),
        ("1.1.4-macchiato", "1.1.4-macchiato"),
        ("", ""),
    ]:
        obtenido = updater._version_base_plataforma(valor)
        check(
            f"updater: _version_base_plataforma({valor!r}) == {esperado!r}",
            obtenido == esperado,
            repr(obtenido),
        )


def _test_plataforma(check) -> None:
    casos = [
        ("arm64", "mac-arm64"),
        ("aarch64", "mac-arm64"),
        ("x86_64", "mac-intel"),
        ("amd64", "mac-intel"),
    ]
    for maquina, esperada in casos:
        with _entorno_mac(maquina):
            valor = updater._plataforma_actual()
        check(
            f"updater._plataforma_actual: mac {maquina} -> {esperada}",
            valor == esperada,
            valor,
        )
    with mock.patch.object(updater.os, "name", "nt"), mock.patch.object(
        updater.sys, "platform", "win32"
    ):
        check(
            "updater._plataforma_actual: windows",
            updater._plataforma_actual() == "windows",
            updater._plataforma_actual(),
        )
    with _entorno_mac("ppc"):
        check(
            "updater._plataforma_actual: mac sin arquitectura soportada",
            updater._plataforma_actual() == "",
            updater._plataforma_actual(),
        )
    with mock.patch.object(updater.os, "name", "posix"), mock.patch.object(
        updater.sys, "platform", "linux"
    ):
        check(
            "updater._plataforma_actual: linux no soportado",
            updater._plataforma_actual() == "",
            updater._plataforma_actual(),
        )


def _test_puede(check) -> None:
    with mock.patch.object(updater.sys, "frozen", False, create=True):
        check("updater: en desarrollo no se actualiza", updater.puede_actualizarse() is False)
    with mock.patch.object(updater.sys, "frozen", True, create=True):
        check("updater: congelado puede actualizarse", updater.puede_actualizarse() is True)
    with _entorno_mac("arm64", congelado=True):
        check(
            "updater: en macOS arm64 congelado SI hay autoactualizacion",
            updater.puede_actualizarse() is True,
        )
    with _entorno_mac("x86_64", congelado=True):
        check(
            "updater: en macOS Intel congelado SI hay autoactualizacion",
            updater.puede_actualizarse() is True,
        )
    with _entorno_mac("arm64", congelado=False):
        check(
            "updater: en macOS en desarrollo no se actualiza",
            updater.puede_actualizarse() is False,
        )
    with _entorno_mac("ppc", congelado=True):
        check(
            "updater: en macOS con arquitectura desconocida no se actualiza",
            updater.puede_actualizarse() is False,
        )
    with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(
        updater.os, "name", "posix"
    ), mock.patch.object(updater.sys, "platform", "linux"):
        check(
            "updater: en una plataforma no soportada no se actualiza",
            updater.puede_actualizarse() is False,
        )
    with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(
        updater.os, "name", "nt"
    ), mock.patch.object(updater.sys, "platform", "win32"):
        check(
            "updater: en Windows congelado SI hay autoactualizacion",
            updater.puede_actualizarse() is True,
        )


# ---------------------------------------------------------------------------
# consultar
# ---------------------------------------------------------------------------
def _test_consultar(check) -> None:
    llamadas = []

    def _falso_get(url, **kwargs):
        llamadas.append({"url": url, **kwargs})
        return _Respuesta(
            datos={
                "ok": True,
                "disponible": True,
                "version": "1.2.0",
                "obligatorio": True,
                "notas": "Notas del release",
                "sha256": "abc",
                "tamano": 123,
                "filename": "MWX-APP-1.2.0.zip",
                "fecha": "2026-11-23T00:00:00",
            }
        )

    with mock.patch.object(updater.requests, "get", _falso_get):
        datos = updater.consultar("https://api.test/v1/", __version__)
    check(
        "updater.consultar: devuelve el contrato completo",
        set(datos) == _CLAVES_CONSULTA,
        str(set(datos)),
    )
    check("updater.consultar: ok True", datos.get("ok") is True)
    check("updater.consultar: disponible True", datos.get("disponible") is True)
    check("updater.consultar: requerida True", datos.get("requerida") is True)
    check("updater.consultar: obligatoria True", datos.get("obligatoria") is True)
    check("updater.consultar: version remota", datos.get("version") == "1.2.0")
    check("updater.consultar: notas/sha/tamano", datos.get("notas") == "Notas del release" and datos.get("sha256") == "abc" and datos.get("tamano") == 123)
    check(
        "updater.consultar: url_descarga del contrato",
        datos.get("url_descarga")
        == "https://api.test/v1/app/descargar?version=1.2.0",
        str(datos.get("url_descarga")),
    )
    check(
        "updater.consultar: normaliza la URL base",
        llamadas and llamadas[0]["url"] == "https://api.test/v1/app/version",
        str(llamadas[0]["url"]) if llamadas else "",
    )
    check(
        "updater.consultar: UA MWX-APP/<version> (Windows)",
        llamadas
        and llamadas[0]["headers"].get("User-Agent")
        == f"MWX-APP/{__version__} (Windows)",
        str(llamadas[0]["headers"]) if llamadas else "",
    )
    check(
        "updater.consultar: timeout respetado",
        llamadas and llamadas[0].get("timeout") == 6,
    )

    def _sin_release(url, **kwargs):
        return _Respuesta(datos={"ok": True, "disponible": False, "version": ""})

    with mock.patch.object(updater.requests, "get", _sin_release):
        datos = updater.consultar("https://api.test", "1.1.0")
    check("updater.consultar: sin release ok True", datos.get("ok") is True)
    check("updater.consultar: sin release disponible False", datos.get("disponible") is False)
    check("updater.consultar: sin release requerida False", datos.get("requerida") is False)
    check("updater.consultar: sin release version vacia", datos.get("version") == "")
    check("updater.consultar: sin release sin url", datos.get("url_descarga") == "")

    def _misma_version(url, **kwargs):
        return _Respuesta(
            datos={
                "ok": True,
                "disponible": True,
                "version": "1.1.0",
                "obligatorio": True,
            }
        )

    with mock.patch.object(updater.requests, "get", _misma_version):
        datos = updater.consultar("https://api.test", "1.1.0")
    check(
        "updater.consultar: misma version no es requerida",
        datos.get("requerida") is False and datos.get("obligatoria") is True,
    )

    def _sin_red(*args, **kwargs):
        raise requests.ConnectionError("sin red")

    with mock.patch.object(updater.requests, "get", _sin_red):
        datos = updater.consultar("https://api.test", "1.1.0")
    check("updater.consultar: sin red no lanza", isinstance(datos, dict))
    check("updater.consultar: sin red ok False", datos.get("ok") is False)
    check("updater.consultar: sin red requerida False", datos.get("requerida") is False)
    check("updater.consultar: sin red con error", bool(datos.get("error")))
    check("updater.consultar: sin red contrato completo", set(datos) == _CLAVES_CONSULTA)

    def _timeout(*args, **kwargs):
        raise requests.Timeout("timeout")

    with mock.patch.object(updater.requests, "get", _timeout):
        datos = updater.consultar("https://api.test", "1.1.0")
    check("updater.consultar: timeout ok False", datos.get("ok") is False)

    def _json_roto(url, **kwargs):
        return _Respuesta(romper_json=True)

    with mock.patch.object(updater.requests, "get", _json_roto):
        datos = updater.consultar("https://api.test", "1.1.0")
    check("updater.consultar: JSON roto ok False", datos.get("ok") is False)
    check("updater.consultar: JSON roto con error", bool(datos.get("error")))

    def _http_500(url, **kwargs):
        return _Respuesta(estado=500, datos={})

    with mock.patch.object(updater.requests, "get", _http_500):
        datos = updater.consultar("https://api.test", "1.1.0")
    check("updater.consultar: HTTP 500 ok False", datos.get("ok") is False)

    def _error_servidor(url, **kwargs):
        return _Respuesta(datos={"ok": False, "error": "fallo interno"})

    with mock.patch.object(updater.requests, "get", _error_servidor):
        datos = updater.consultar("https://api.test", "1.1.0")
    check(
        "updater.consultar: error del servidor propagado",
        datos.get("ok") is False and datos.get("error") == "fallo interno",
    )

    datos = updater.consultar("", "1.1.0")
    check(
        "updater.consultar: sin api_url devuelve error",
        datos.get("ok") is False and bool(datos.get("error")),
    )
    check(
        "updater.consultar: nunca lanza con api_url None",
        isinstance(updater.consultar(None), dict),
    )


def _test_consultar_plataformas(check) -> None:
    def _respuesta_version(url, **kwargs):
        return _Respuesta(
            datos={
                "ok": True,
                "disponible": True,
                "version": "1.2.0",
                "obligatorio": True,
            }
        )

    for maquina, token in (("arm64", "mac-arm64"), ("x86_64", "mac-intel")):
        llamadas = []

        def _get(url, **kwargs):
            llamadas.append({"url": url, **kwargs})
            return _respuesta_version(url, **kwargs)

        with _entorno_mac(maquina), mock.patch.object(updater.requests, "get", _get):
            datos = updater.consultar("https://api.test", "1.1.0")
        check(
            f"updater.consultar: URL con ?plataforma={token}",
            llamadas
            and llamadas[0]["url"]
            == f"https://api.test/v1/app/version?plataforma={token}",
            str(llamadas[0]["url"]) if llamadas else "",
        )
        check(
            f"updater.consultar: url_descarga incluye version+{token}",
            datos.get("url_descarga")
            == (
                "https://api.test/v1/app/descargar"
                f"?version=1.2.0&plataforma={token}"
            ),
            str(datos.get("url_descarga")),
        )
        check(
            f"updater.consultar: UA macOS ({token})",
            llamadas
            and llamadas[0]["headers"].get("User-Agent")
            == "MWX-APP/1.1.0 (macOS)",
            str(llamadas[0]["headers"]) if llamadas else "",
        )

    llamadas = []

    def _get_win(url, **kwargs):
        llamadas.append({"url": url, **kwargs})
        return _respuesta_version(url, **kwargs)

    with mock.patch.object(updater.os, "name", "nt"), mock.patch.object(
        updater.requests, "get", _get_win
    ):
        datos = updater.consultar("https://api.test", "1.1.0")
    check(
        "updater.consultar: Windows no agrega plataforma en la URL",
        llamadas and llamadas[0]["url"] == "https://api.test/v1/app/version",
        str(llamadas[0]["url"]) if llamadas else "",
    )
    check(
        "updater.consultar: Windows url_descarga sin plataforma",
        datos.get("url_descarga")
        == "https://api.test/v1/app/descargar?version=1.2.0",
        str(datos.get("url_descarga")),
    )

    llamadas = []

    def _get_explicita(url, **kwargs):
        llamadas.append({"url": url, **kwargs})
        return _respuesta_version(url, **kwargs)

    with mock.patch.object(updater.requests, "get", _get_explicita):
        datos = updater.consultar(
            "https://api.test", "1.1.0", plataforma="mac-arm64"
        )
    check(
        "updater.consultar: plataforma explicita en la URL",
        llamadas
        and llamadas[0]["url"]
        == "https://api.test/v1/app/version?plataforma=mac-arm64",
        str(llamadas[0]["url"]) if llamadas else "",
    )
    check(
        "updater.consultar: url_descarga con plataforma explicita",
        datos.get("url_descarga")
        == (
            "https://api.test/v1/app/descargar"
            "?version=1.2.0&plataforma=mac-arm64"
        ),
        str(datos.get("url_descarga")),
    )

    def _version(remota):
        def _get(url, **kwargs):
            return _Respuesta(
                datos={
                    "ok": True,
                    "disponible": True,
                    "version": remota,
                    "obligatorio": True,
                }
            )

        return _get

    with _entorno_mac("arm64"), mock.patch.object(
        updater.requests, "get", _version("1.1.4-mac-arm64")
    ):
        datos = updater.consultar("https://api.test", "1.1.4")
    check(
        "updater.consultar: misma version Mac (con sufijo) no es requerida",
        datos.get("disponible") is True
        and datos.get("requerida") is False
        and datos.get("version") == "1.1.4-mac-arm64",
        str(datos),
    )
    with _entorno_mac("x86_64"), mock.patch.object(
        updater.requests, "get", _version("1.1.5-mac-intel")
    ):
        datos = updater.consultar("https://api.test", "1.1.4")
    check(
        "updater.consultar: version Mac nueva si es requerida",
        datos.get("requerida") is True,
        str(datos),
    )


# ---------------------------------------------------------------------------
# descargar
# ---------------------------------------------------------------------------
def _test_descargar(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_descarga_"))
    try:
        contenido = b"PK\x03\x04" + (b"datos" * 2000)
        sha = hashlib.sha256(contenido).hexdigest()
        trozos = [contenido[:1000], contenido[1000:]]
        llamadas = []

        def _falso_get(url, **kwargs):
            llamadas.append({"url": url, **kwargs})
            return _Respuesta(
                contenido=contenido,
                headers={"Content-Length": str(len(contenido))},
                trozos=trozos,
            )

        destino = tmp / "paquete.zip"
        avances = []
        with mock.patch.object(updater.requests, "get", _falso_get):
            datos = updater.descargar(
                "https://api.test",
                "1.2.0",
                destino=destino,
                sha256=sha,
                progreso=lambda frac, texto: avances.append((frac, texto)),
            )
        check("updater.descargar: contrato completo", set(datos) == _CLAVES_DESCARGA)
        check("updater.descargar: ok True", datos.get("ok") is True, str(datos.get("error")))
        check("updater.descargar: ruta", datos.get("ruta") == str(destino))
        check("updater.descargar: bytes", datos.get("bytes") == len(contenido))
        check("updater.descargar: archivo escrito", destino.is_file() and destino.read_bytes() == contenido)
        check(
            "updater.descargar: URL/params/timeout/stream",
            llamadas
            and llamadas[0]["url"] == "https://api.test/v1/app/descargar"
            and llamadas[0]["params"] == {"version": "1.2.0"}
            and llamadas[0]["timeout"] == (10, 120)
            and llamadas[0]["stream"] is True,
            str(llamadas[0]) if llamadas else "",
        )
        check("updater.descargar: progreso termina en 1.0", bool(avances) and avances[-1][0] == 1.0, str(avances[-1]) if avances else "")
        check(
            "updater.descargar: progreso monotono",
            all(b[0] >= a[0] for a, b in zip(avances, avances[1:])),
        )
        check(
            "updater.descargar: texto final",
            avances and avances[-1][1] == "Descarga completa",
        )

        destino_malo = tmp / "malo.zip"
        with mock.patch.object(updater.requests, "get", _falso_get):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=destino_malo, sha256="0" * 64
            )
        check("updater.descargar: sha malo ok False", datos.get("ok") is False)
        check("updater.descargar: sha malo con error", "sha256" in str(datos.get("error")).lower())
        check("updater.descargar: sha malo borra el archivo", not destino_malo.exists())

        def _incompleta(url, **kwargs):
            return _Respuesta(
                contenido=contenido,
                headers={"Content-Length": str(len(contenido) + 50)},
                trozos=[contenido],
            )

        destino_inc = tmp / "incompleto.zip"
        with mock.patch.object(updater.requests, "get", _incompleta):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=destino_inc, sha256=sha
            )
        check("updater.descargar: tamano incompleto ok False", datos.get("ok") is False)
        check(
            "updater.descargar: tamano incompleto con error",
            "incompleta" in str(datos.get("error")).lower(),
        )
        check("updater.descargar: incompleto borra el archivo", not destino_inc.exists())

        evento = threading.Event()
        evento.set()
        destino_cancel = tmp / "cancel.zip"
        with mock.patch.object(updater.requests, "get", _falso_get):
            datos = updater.descargar(
                "https://api.test",
                "1.2.0",
                destino=destino_cancel,
                cancelar=evento,
            )
        check("updater.descargar: cancelada ok False", datos.get("ok") is False)
        check(
            "updater.descargar: cancelada con error",
            "cancel" in str(datos.get("error")).lower(),
        )
        check("updater.descargar: cancelada no deja archivo", not destino_cancel.exists())

        destino_404 = tmp / "404.zip"

        def _404(url, **kwargs):
            return _Respuesta(estado=404, datos={"ok": False})

        with mock.patch.object(updater.requests, "get", _404):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=destino_404
            )
        check("updater.descargar: 404 ok False", datos.get("ok") is False)
        check(
            "updater.descargar: 404 mensaje del contrato",
            datos.get("error") == "actualizacion no disponible",
        )

        def _vacia(url, **kwargs):
            return _Respuesta(contenido=b"", headers={"Content-Length": "0"})

        with mock.patch.object(updater.requests, "get", _vacia):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=tmp / "vacia.zip"
            )
        check("updater.descargar: vacia ok False", datos.get("ok") is False)

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            with mock.patch.object(updater.requests, "get", _falso_get):
                datos = updater.descargar(
                    "https://api.test", "1.2.0", sha256=sha
                )
        esperada = tmp / "updates" / "MWX-APP-1.2.0.zip"
        check("updater.descargar: destino por defecto", datos.get("ok") is True and Path(datos.get("ruta")) == esperada, str(datos.get("ruta")))
        check("updater.descargar: destino por defecto existe", esperada.is_file())

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            with mock.patch.object(updater.requests, "get", _falso_get):
                datos = updater.descargar(
                    "https://api.test", "../../evil", sha256=sha
                )
        ruta_evil = Path(datos.get("ruta") or "")
        check(
            "updater.descargar: version traversal se sanea",
            ruta_evil.parent == tmp / "updates"
            and ".." not in ruta_evil.name,
            str(ruta_evil),
        )

        datos = updater.descargar(None, None)
        check(
            "updater.descargar: sin api_url devuelve error",
            datos.get("ok") is False and set(datos) == _CLAVES_DESCARGA,
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# descargar por plataforma
# ---------------------------------------------------------------------------
def _test_descargar_plataforma(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_desc_plataforma_"))
    try:
        contenido = b"PK\x03\x04mac"
        llamadas = []

        def _get(url, **kwargs):
            llamadas.append({"url": url, **kwargs})
            return _Respuesta(
                contenido=contenido,
                headers={"Content-Length": str(len(contenido))},
                trozos=[contenido],
            )

        with mock.patch.object(updater.requests, "get", _get):
            datos = updater.descargar(
                "https://api.test",
                "1.2.0",
                destino=tmp / "mac.zip",
                plataforma="mac-arm64",
            )
        check(
            "updater.descargar: params con plataforma explicita",
            datos.get("ok") is True
            and llamadas
            and llamadas[0]["params"]
            == {"version": "1.2.0", "plataforma": "mac-arm64"},
            str(llamadas[0]) if llamadas else "",
        )
        check(
            "updater.descargar: UA macOS con plataforma",
            llamadas
            and llamadas[0]["headers"].get("User-Agent")
            == "MWX-APP/1.2.0 (macOS)",
            str(llamadas[0]["headers"]) if llamadas else "",
        )

        llamadas.clear()
        with mock.patch.object(
            updater, "_plataforma_actual", lambda: "mac-intel"
        ), mock.patch.object(updater.requests, "get", _get):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=tmp / "mac2.zip"
            )
        check(
            "updater.descargar: plataforma auto mac-intel en params",
            datos.get("ok") is True
            and llamadas
            and llamadas[0]["params"]
            == {"version": "1.2.0", "plataforma": "mac-intel"},
            str(llamadas[0]) if llamadas else "",
        )

        llamadas.clear()
        with mock.patch.object(
            updater, "_plataforma_actual", lambda: "windows"
        ), mock.patch.object(updater.requests, "get", _get):
            datos = updater.descargar(
                "https://api.test", "1.2.0", destino=tmp / "win.zip"
            )
        check(
            "updater.descargar: Windows mantiene solo version en params",
            datos.get("ok") is True
            and llamadas
            and llamadas[0]["params"] == {"version": "1.2.0"},
            str(llamadas[0]) if llamadas else "",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# preparar
# ---------------------------------------------------------------------------
def _test_preparar(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_preparar_"))
    try:
        origen = _crear_zip(tmp / "MWX-APP-1.2.0.zip", {})
        app_dir = tmp / "instalado"
        staging = tmp / "staging"
        datos = updater.preparar(
            str(origen), app_dir=str(app_dir), staging_dir=str(staging)
        )
        check("updater.preparar: contrato completo", set(datos) == _CLAVES_PREPARAR)
        check("updater.preparar: ok True", datos.get("ok") is True, str(datos.get("error")))
        check(
            "updater.preparar: staging",
            datos.get("staging") == str(staging.resolve()),
            str(datos.get("staging")),
        )
        check(
            "updater.preparar: source es la carpeta del exe",
            Path(datos.get("source") or "").name.lower() == "app",
            str(datos.get("source")),
        )
        check(
            "updater.preparar: app_dir",
            datos.get("app_dir") == str(app_dir.resolve()),
            str(datos.get("app_dir")),
        )
        bat = Path(datos.get("bat") or "")
        check("updater.preparar: .bat existe", bat.is_file(), str(bat))
        crudo = bat.read_bytes()
        check("updater.preparar: .bat UTF-8 sin BOM", not crudo.startswith(b"\xef\xbb\xbf"))
        texto = crudo.decode("utf-8")
        obligatorios = [
            "@echo off",
            "chcp 65001",
            "setlocal",
            'set "MWX_PID=',
            'tasklist /FI "PID eq %MWX_PID%"',
            "ping -n 3 127.0.0.1",
            "robocopy",
            "/E /IS /IT /R:5 /W:2",
            "/XF config.json",
            'start ""',
            "rmdir /S /Q",
            'del "%~f0"',
            "MWX APP.exe",
        ]
        for fragmento in obligatorios:
            check(
                f"updater.preparar: .bat contiene {fragmento!r}",
                fragmento in texto,
            )
        check(
            "updater.preparar: .bat con rutas absolutas",
            str(staging.resolve()) in texto
            and str(app_dir.resolve()) in texto
            and str(Path(datos.get("source") or "")) in texto,
        )
        check(
            "updater.preparar: no relanza si robocopy >= 8",
            "if %MWX_RC% GEQ 8 goto MWX_ERROR" in texto
            and ":MWX_ERROR" in texto,
        )

        sin_exe = _crear_zip(tmp / "MWX-APP-1.3.0.zip", {}, exe=False)
        datos = updater.preparar(
            str(sin_exe), app_dir=str(app_dir), staging_dir=str(tmp / "s2" / "stage")
        )
        check("updater.preparar: ZIP sin exe ok False", datos.get("ok") is False)
        check(
            "updater.preparar: ZIP sin exe con error",
            "MWX APP.exe" in str(datos.get("error")),
        )
        check(
            "updater.preparar: ZIP sin exe no escribe .bat",
            not (tmp / "s2" / "actualizar_mwx.bat").exists(),
        )

        traversal = _crear_zip(
            tmp / "MWX-APP-1.4.0.zip", {"../evil.txt": b"malo"}, exe=False
        )
        datos = updater.preparar(
            str(traversal), app_dir=str(app_dir), staging_dir=str(tmp / "s3")
        )
        check("updater.preparar: traversal ok False", datos.get("ok") is False)
        check(
            "updater.preparar: traversal con error",
            "ruta" in str(datos.get("error")).lower(),
            str(datos.get("error")),
        )
        check("updater.preparar: traversal no escribe fuera", not (tmp / "evil.txt").exists())

        absoluto = _crear_zip(
            tmp / "MWX-APP-1.5.0.zip", {"C:/evil.txt": b"malo"}, exe=False
        )
        datos = updater.preparar(
            str(absoluto), app_dir=str(app_dir), staging_dir=str(tmp / "s4")
        )
        check("updater.preparar: ruta absoluta ok False", datos.get("ok") is False)

        invalido = tmp / "MWX-APP-1.6.0.zip"
        invalido.write_bytes(b"no es un zip")
        datos = updater.preparar(
            str(invalido), app_dir=str(app_dir), staging_dir=str(tmp / "s5")
        )
        check("updater.preparar: ZIP invalido ok False", datos.get("ok") is False)

        datos = updater.preparar(str(tmp / "no_existe.zip"))
        check("updater.preparar: archivo inexistente ok False", datos.get("ok") is False)

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            datos = updater.preparar(
                str(_crear_zip(tmp / "MWX-APP-1.7.0.zip", {})),
                app_dir=str(app_dir),
            )
        check(
            "updater.preparar: staging por defecto usa la version",
            datos.get("ok") is True
            and Path(datos.get("staging") or "").name == "1.7.0",
            str(datos.get("staging")),
        )
        check(
            "updater.preparar: .bat por defecto junto a staging",
            Path(datos.get("bat") or "").name == "actualizar_mwx.bat",
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# preparar en macOS
# ---------------------------------------------------------------------------
def _test_preparar_mac(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_preparar_mac_"))
    try:
        origen = tmp / "MWX-APP-mac-arm64-1.2.0.zip"
        with zipfile.ZipFile(origen, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("MWX APP.app/Contents/Info.plist", b"<plist/>")
            zf.writestr("MWX APP.app/Contents/MacOS/MWX APP", b"bin")
            zf.writestr("config.json", b"{}")
            zf.writestr("LEEME_MAC.txt", b"leeme")
        app_dir = tmp / "Aplicaciones" / "MWX APP.app"
        app_dir.mkdir(parents=True)
        staging = tmp / "stage_mac"

        with mock.patch.object(updater.sys, "platform", "darwin"):
            datos = updater.preparar(
                str(origen), app_dir=str(app_dir), staging_dir=str(staging)
            )
        check(
            "updater.preparar mac: contrato completo",
            set(datos) == _CLAVES_PREPARAR,
            str(set(datos)),
        )
        check(
            "updater.preparar mac: ok True",
            datos.get("ok") is True,
            str(datos.get("error")),
        )
        source = Path(datos.get("source") or "")
        check(
            "updater.preparar mac: source es el .app",
            source.is_dir()
            and source.name.lower() == "mwx app.app"
            and (source / "Contents" / "Info.plist").is_file(),
            str(source),
        )
        check(
            "updater.preparar mac: app_dir",
            datos.get("app_dir") == str(app_dir.resolve()),
            str(datos.get("app_dir")),
        )
        guion = Path(datos.get("bat") or "")
        check(
            "updater.preparar mac: script .sh existe",
            guion.name == "actualizar_mwx.sh" and guion.is_file(),
            str(guion),
        )
        crudo = guion.read_bytes() if guion.is_file() else b""
        check(
            "updater.preparar mac: .sh UTF-8 sin BOM",
            not crudo.startswith(b"\xef\xbb\xbf"),
        )
        check(
            "updater.preparar mac: .sh con LF (sin CRLF)",
            bool(crudo) and b"\r\n" not in crudo,
        )
        texto = crudo.decode("utf-8", "ignore")
        for fragmento in (
            "ditto",
            "kill -0",
            "xattr -dr",
            "BACKUP",
            "Library/Logs/MWX APP",
            '$HOME/Applications/MWX APP.app',
            'open "$FINAL"',
            'open "$DESTINO"',
        ):
            check(
                f"updater.preparar mac: .sh contiene {fragmento!r}",
                fragmento in texto,
            )
        check(
            "updater.preparar mac: .sh usa PID/ORIGEN/DESTINO",
            '"$1"' in texto
            and '"$2"' in texto
            and '"$3"' in texto,
        )

        bash = shutil.which("bash")
        if bash:
            proceso = subprocess.run(
                [bash, "-n", str(guion).replace("\\", "/")],
                capture_output=True,
                text=True,
                timeout=30,
            )
            check(
                "updater.preparar mac: bash -n valida el script",
                proceso.returncode == 0
                and not (proceso.stderr or "").strip(),
                (proceso.stderr or "").strip(),
            )
        else:
            check(
                "updater.preparar mac: .sh no vacio (sin bash)",
                len(texto) > 300,
            )

        origen_min = tmp / "MWX-APP-mac-intel-1.2.0.zip"
        with zipfile.ZipFile(origen_min, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("mwx app.app/Contents/Info.plist", b"<plist/>")
        with mock.patch.object(updater.sys, "platform", "darwin"):
            datos = updater.preparar(
                str(origen_min),
                app_dir=str(app_dir),
                staging_dir=str(tmp / "stage_mac_min"),
            )
        check(
            "updater.preparar mac: busca MWX APP.app case-insensitive",
            datos.get("ok") is True
            and Path(datos.get("source") or "").name.lower() == "mwx app.app",
            str(datos.get("source")),
        )

        solo_exe = _crear_zip(tmp / "MWX-APP-mac-1.3.0.zip", {})
        staging_exe = tmp / "stage_mac_exe" / "stage"
        with mock.patch.object(updater.sys, "platform", "darwin"):
            datos = updater.preparar(
                str(solo_exe),
                app_dir=str(app_dir),
                staging_dir=str(staging_exe),
            )
        check(
            "updater.preparar mac: ZIP de Windows no se instala en macOS",
            datos.get("ok") is False
            and "MWX APP.app" in str(datos.get("error"))
            and "Windows" in str(datos.get("error")),
            str(datos.get("error")),
        )
        check(
            "updater.preparar mac: ZIP de Windows no escribe .sh",
            not (tmp / "stage_mac_exe" / "actualizar_mwx.sh").exists(),
        )

        ejecutable = (
            tmp
            / "Aplicaciones"
            / "MWX APP.app"
            / "Contents"
            / "MacOS"
            / "MWX APP"
        )
        with mock.patch.object(updater.sys, "platform", "darwin"), mock.patch.object(
            updater.sys, "executable", str(ejecutable)
        ):
            datos = updater.preparar(
                str(origen), staging_dir=str(tmp / "stage_mac_defecto")
            )
        check(
            "updater.preparar mac: app_dir por defecto es la raiz del .app",
            datos.get("ok") is True
            and datos.get("app_dir")
            == str((tmp / "Aplicaciones" / "MWX APP.app").resolve()),
            str(datos.get("app_dir")),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# lanzar
# ---------------------------------------------------------------------------
def _test_lanzar(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_lanzar_"))
    try:
        bat = tmp / "actualizar_mwx.bat"
        bat.write_text("@echo off\r\n", encoding="utf-8")
        llamadas = []

        def _popen(comando, **kwargs):
            llamadas.append({"comando": comando, **kwargs})
            return object()

        with mock.patch.object(updater.subprocess, "Popen", _popen):
            datos = updater.lanzar({"ok": True, "bat": str(bat)})
        check("updater.lanzar: contrato", set(datos) == {"ok", "error"})
        check("updater.lanzar: ok True", datos.get("ok") is True, str(datos.get("error")))
        check("updater.lanzar: Popen una vez", len(llamadas) == 1)
        check(
            "updater.lanzar: comando apunta al .bat",
            llamadas and str(bat) in llamadas[0]["comando"],
            str(llamadas[0]["comando"]) if llamadas else "",
        )
        if os.name == "nt":
            check(
                "updater.lanzar: en Windows usa cmd.exe /c",
                llamadas and llamadas[0]["comando"][0:2] == ["cmd.exe", "/c"],
                str(llamadas[0]["comando"]) if llamadas else "",
            )
        check(
            "updater.lanzar: sin consola visible (CREATE_NO_WINDOW)",
            llamadas
            and (
                os.name != "nt"
                or (
                    int(llamadas[0].get("creationflags") or 0)
                    & int(
                        getattr(
                            updater.subprocess,
                            "CREATE_NO_WINDOW",
                            0x08000000,
                        )
                    )
                )
            )
            and llamadas[0].get("close_fds") is True,
            str(llamadas[0].get("creationflags")) if llamadas else "",
        )
        check(
            "updater.lanzar: sin salida a consola",
            llamadas
            and llamadas[0].get("stdin") == updater.subprocess.DEVNULL
            and llamadas[0].get("stdout") == updater.subprocess.DEVNULL,
        )

        datos = updater.lanzar({"bat": str(tmp / "no_existe.bat")})
        check("updater.lanzar: .bat inexistente ok False", datos.get("ok") is False)
        check("updater.lanzar: datos invalidos ok False", updater.lanzar(None).get("ok") is False)

        def _popen_error(*args, **kwargs):
            raise OSError("no se pudo")

        with mock.patch.object(updater.subprocess, "Popen", _popen_error):
            datos = updater.lanzar({"bat": str(bat)})
        check("updater.lanzar: error de Popen ok False", datos.get("ok") is False)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# lanzar en macOS
# ---------------------------------------------------------------------------
def _test_lanzar_mac(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_lanzar_mac_"))
    try:
        guion = tmp / "actualizar_mwx.sh"
        guion.write_bytes(b"#!/bin/sh\n")
        origen = tmp / "MWX APP.app"
        (origen / "Contents").mkdir(parents=True)
        destino = tmp / "Aplicaciones" / "MWX APP.app"
        llamadas = []

        def _popen(comando, **kwargs):
            llamadas.append({"comando": comando, **kwargs})
            return object()

        with mock.patch.object(updater.sys, "platform", "darwin"), mock.patch.object(
            updater.subprocess, "Popen", _popen
        ):
            datos = updater.lanzar(
                {
                    "ok": True,
                    "bat": str(guion),
                    "source": str(origen),
                    "app_dir": str(destino),
                }
            )
        check("updater.lanzar mac: contrato", set(datos) == {"ok", "error"})
        check(
            "updater.lanzar mac: ok True",
            datos.get("ok") is True,
            str(datos.get("error")),
        )
        check(
            "updater.lanzar mac: /bin/sh con PID/source/app_dir",
            llamadas
            and llamadas[0]["comando"]
            == [
                "/bin/sh",
                str(guion),
                str(os.getpid()),
                str(origen),
                str(destino),
            ],
            str(llamadas[0]["comando"]) if llamadas else "",
        )
        check(
            "updater.lanzar mac: sesion nueva y desacoplada",
            llamadas
            and llamadas[0].get("start_new_session") is True
            and llamadas[0].get("close_fds") is True,
            str(llamadas[0]) if llamadas else "",
        )
        check(
            "updater.lanzar mac: sin salida a consola",
            llamadas
            and llamadas[0].get("stdin") == updater.subprocess.DEVNULL
            and llamadas[0].get("stdout") == updater.subprocess.DEVNULL
            and llamadas[0].get("stderr") == updater.subprocess.DEVNULL,
        )

        llamadas.clear()
        with mock.patch.object(updater.sys, "platform", "darwin"), mock.patch.object(
            updater.subprocess, "Popen", _popen
        ):
            datos = updater.lanzar(
                {
                    "ok": True,
                    "bat": str(guion),
                    "staging": str(tmp),
                    "app_dir": str(destino),
                }
            )
        check(
            "updater.lanzar mac: deriva source del staging (flujo actualizar)",
            datos.get("ok") is True
            and llamadas
            and llamadas[0]["comando"][3] == str(origen.resolve()),
            str(llamadas[0]["comando"]) if llamadas else "",
        )

        llamadas.clear()
        with mock.patch.object(updater.sys, "platform", "darwin"), mock.patch.object(
            updater.subprocess, "Popen", _popen
        ):
            datos = updater.lanzar(
                {
                    "ok": True,
                    "bat": str(guion),
                    "app_dir": str(destino),
                }
            )
        check(
            "updater.lanzar mac: sin source ni staging falla",
            datos.get("ok") is False
            and not llamadas
            and bool(datos.get("error")),
            str(datos.get("error")),
        )

        with mock.patch.object(updater.sys, "platform", "darwin"):
            datos = updater.lanzar(
                {
                    "ok": True,
                    "bat": str(tmp / "no_existe.sh"),
                    "source": str(origen),
                    "app_dir": str(destino),
                }
            )
        check(
            "updater.lanzar mac: script inexistente ok False",
            datos.get("ok") is False,
        )

        def _popen_error(*args, **kwargs):
            raise OSError("no se pudo")

        with mock.patch.object(updater.sys, "platform", "darwin"), mock.patch.object(
            updater.subprocess, "Popen", _popen_error
        ):
            datos = updater.lanzar(
                {
                    "ok": True,
                    "bat": str(guion),
                    "source": str(origen),
                    "app_dir": str(destino),
                }
            )
        check(
            "updater.lanzar mac: error de Popen ok False",
            datos.get("ok") is False,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# actualizar (flujo completo)
# ---------------------------------------------------------------------------
def _test_actualizar(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_upd_actualizar_"))
    try:
        zip_bytes = _zip_bytes()
        sha = hashlib.sha256(zip_bytes).hexdigest()
        version_info = {
            "ok": True,
            "disponible": True,
            "version": "1.2.0",
            "obligatorio": True,
            "notas": "Notas",
            "sha256": sha,
            "tamano": len(zip_bytes),
            "filename": "MWX-APP-1.2.0.zip",
            "fecha": "2026-11-23T00:00:00",
        }
        llamadas = []

        def _falso_get(url, **kwargs):
            llamadas.append(url)
            if url.endswith("/v1/app/version"):
                return _Respuesta(datos=dict(version_info))
            return _Respuesta(
                contenido=zip_bytes,
                headers={"Content-Length": str(len(zip_bytes))},
                trozos=[zip_bytes],
            )

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            with mock.patch.object(updater.requests, "get", _falso_get):
                datos = updater.actualizar("https://api.test")
        check("updater.actualizar: contrato completo", set(datos) == _CLAVES_ACTUALIZAR)
        check("updater.actualizar: ok True", datos.get("ok") is True, str(datos.get("error")))
        check("updater.actualizar: version", datos.get("version") == "1.2.0")
        check("updater.actualizar: bat existe", Path(datos.get("bat") or "").is_file())
        check("updater.actualizar: staging existe", Path(datos.get("staging") or "").is_dir())
        check("updater.actualizar: app_dir informado", bool(datos.get("app_dir")))
        check(
            "updater.actualizar: consulta y descarga",
            len(llamadas) == 2
            and llamadas[0].endswith("/v1/app/version")
            and llamadas[1].endswith("/v1/app/descargar"),
            str(llamadas),
        )

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            with mock.patch.object(updater.requests, "get", _falso_get):
                datos = updater.actualizar("https://api.test", version="1.2.0")
        check(
            "updater.actualizar: version explicita",
            datos.get("ok") is True and datos.get("version") == "1.2.0",
            str(datos.get("error")),
        )

        def _sin_release(url, **kwargs):
            return _Respuesta(datos={"ok": True, "disponible": False, "version": ""})

        with mock.patch.object(updater.requests, "get", _sin_release):
            datos = updater.actualizar("https://api.test")
        check("updater.actualizar: sin release ok False", datos.get("ok") is False)
        check("updater.actualizar: sin release con error", bool(datos.get("error")))

        def _misma(url, **kwargs):
            return _Respuesta(
                datos={
                    "ok": True,
                    "disponible": True,
                    "version": "1.1.0",
                    "obligatorio": False,
                }
            )

        with mock.patch.object(updater.requests, "get", _misma):
            datos = updater.actualizar("https://api.test")
        check("updater.actualizar: ya actualizado ok False", datos.get("ok") is False)
        check(
            "updater.actualizar: ya actualizado con error",
            "ultima" in str(datos.get("error")).lower(),
            str(datos.get("error")),
        )

        def _version_ok_descarga_mal(url, **kwargs):
            if url.endswith("/v1/app/version"):
                return _Respuesta(datos=dict(version_info))
            return _Respuesta(estado=500, datos={})

        with mock.patch.object(updater.config, "_dir_datos", lambda: tmp):
            with mock.patch.object(updater.requests, "get", _version_ok_descarga_mal):
                datos = updater.actualizar("https://api.test")
        check("updater.actualizar: descarga fallida ok False", datos.get("ok") is False)

        check(
            "updater.actualizar: nunca lanza con api_url None",
            isinstance(updater.actualizar(None), dict),
        )
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run(check) -> None:
    _test_version(check)
    _test_plataforma(check)
    _test_puede(check)
    _test_consultar(check)
    _test_consultar_plataformas(check)
    _test_descargar(check)
    _test_descargar_plataforma(check)
    _test_preparar(check)
    _test_preparar_mac(check)
    _test_lanzar(check)
    _test_lanzar_mac(check)
    _test_actualizar(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
