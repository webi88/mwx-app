# -*- coding: utf-8 -*-
"""Tests del autoactualizador `mwx_app/updater.py` (sin red ni ejecutar el .bat).

Cubre el contrato congelado:
  - `comparar_versiones` y `version_actual`;
  - `puede_actualizarse` segun `sys.frozen`;
  - `consultar` con `requests.get` simulado (sin release, release nuevo,
    obligatorio, sin red, JSON roto, HTTP 500 y error del servidor);
  - `descargar` con chunks falsos (sha256 bueno/malo, tamano incompleto,
    cancelacion, 404, destino por defecto y nombre saneado);
  - `preparar` con ZIPs temporales (exe anidado, ruta traversal, ZIP sin exe,
    ZIP invalido) verificando el contenido del `.bat`;
  - `lanzar` con `subprocess.Popen` simulado (nunca ejecuta nada);
  - `actualizar` de punta a punta con responses falsos.

Nunca se toca `data/` real, nunca se abre Chrome y nunca se lanza el .bat.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import threading
import zipfile
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


def _test_puede(check) -> None:
    with mock.patch.object(updater.sys, "frozen", False, create=True):
        check("updater: en desarrollo no se actualiza", updater.puede_actualizarse() is False)
    with mock.patch.object(updater.sys, "frozen", True, create=True):
        check("updater: congelado puede actualizarse", updater.puede_actualizarse() is True)
    with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(
        updater.sys, "platform", "darwin"
    ):
        check(
            "updater: en macOS congelado NO hay autoactualizacion (manual)",
            updater.puede_actualizarse() is False,
        )
    with mock.patch.object(updater.sys, "frozen", True, create=True), mock.patch.object(
        updater.sys, "platform", "win32"
    ):
        check(
            "updater: en Windows congelado SI hay autoactualizacion",
            updater.puede_actualizarse() is True,
        )
    with mock.patch.object(updater.sys, "frozen", False, create=True), mock.patch.object(
        updater.sys, "platform", "darwin"
    ):
        check(
            "updater: en macOS en desarrollo tampoco se actualiza",
            updater.puede_actualizarse() is False,
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
    _test_puede(check)
    _test_consultar(check)
    _test_descargar(check)
    _test_preparar(check)
    _test_lanzar(check)
    _test_actualizar(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
