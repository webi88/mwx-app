"""Checks de `core.config.detectar_chrome_version()` en macOS (sin Chrome real).

En macOS Chrome NO esta en el PATH: el binario vive dentro de
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. Estos checks
simulan `sys.platform == "darwin"` y `subprocess.run` para verificar que:

  (1) macOS prueba PRIMERO la ruta de la .app de Chrome y devuelve la version;
  (2) si Chrome no esta, cae a la .app de Chromium;
  (3) si ninguna .app existe, prueba los binarios del PATH (google-chrome...);
  (4) sin ningun binario devuelve None sin lanzar excepcion;
  (5) Linux conserva `google-chrome` como primer comando;
  (6) salidas invalidas no rompen la deteccion.

NUNCA ejecuta Chrome, `subprocess.run` siempre esta mockeado, y no toca la BD
ni `data/`.

Uso:
    .venv/Scripts/python.exe tests/test_macos_chrome.py
    .venv/Scripts/python.exe tests/run_tests.py
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.config import detectar_chrome_version  # noqa: E402

RUTA_CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RUTA_CHROMIUM_MAC = "/Applications/Chromium.app/Contents/MacOS/Chromium"
COMANDOS_PATH = ["google-chrome", "chromium", "chromium-browser"]


class _Salida:
    """Imita `subprocess.CompletedProcess` (solo `stdout`/`stderr`)."""

    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


@contextlib.contextmanager
def _simular(plataforma, responder, llamados):
    """Patchea plataforma + `subprocess.run`; registra cada comando probado.

    `responder(cmd)` devuelve el stdout o lanza la excepcion que debe ver la
    funcion (p. ej. `FileNotFoundError` cuando el binario no existe).
    """

    def falso_run(cmd, *args, **kwargs):
        comando = list(cmd)
        llamados.append(comando)
        resultado = responder(comando)
        if isinstance(resultado, BaseException):
            raise resultado
        return _Salida(stdout=resultado)

    with mock.patch.object(sys, "platform", plataforma), \
            mock.patch.object(os, "name", "posix"), \
            mock.patch.object(subprocess, "run", falso_run):
        yield


def _no_existe(comando):
    raise FileNotFoundError(2, "No such file or directory", comando[0])


def test_macos_chrome_app(check):
    """macOS: el PRIMER comando es la .app de Chrome y devuelve 153."""
    llamados = []

    with _simular("darwin", lambda cmd: "Google Chrome 153.0.0.0", llamados):
        version = detectar_chrome_version()

    check(
        "macOS|.app Chrome:devuelve la version mayor 153",
        version == 153,
        f"(version={version})",
    )
    check(
        "macOS|.app Chrome:primer comando es la ruta de Google Chrome.app",
        llamados[:1] == [[RUTA_CHROME_MAC, "--version"]],
        f"(comandos={llamados})",
    )
    check(
        "macOS|.app Chrome:se detiene al primer exito",
        len(llamados) == 1,
        f"(probados={len(llamados)})",
    )


def test_macos_fallback_chromium(check):
    """macOS: sin Chrome, cae a la .app de Chromium (salida de 2 tokens)."""
    llamados = []

    def responder(comando):
        if comando[0] == RUTA_CHROME_MAC:
            _no_existe(comando)
        return "Chromium 140.0.6742.0"

    with _simular("darwin", responder, llamados):
        version = detectar_chrome_version()

    check(
        "macOS|Chromium fallback:devuelve la version mayor 140",
        version == 140,
        f"(version={version})",
    )
    check(
        "macOS|Chromium fallback:prueba la .app de Chrome y luego Chromium",
        llamados[:2] == [
            [RUTA_CHROME_MAC, "--version"],
            [RUTA_CHROMIUM_MAC, "--version"],
        ],
        f"(comandos={llamados})",
    )


def test_macos_fallback_path(check):
    """macOS: sin .apps, cae a los binarios del PATH (google-chrome)."""
    llamados = []

    def responder(comando):
        if comando[0].startswith("/Applications/"):
            _no_existe(comando)
        return "Google Chrome 151.0.0.0"

    with _simular("darwin", responder, llamados):
        version = detectar_chrome_version()

    check(
        "macOS|PATH fallback:devuelve la version mayor 151",
        version == 151,
        f"(version={version})",
    )
    check(
        "macOS|PATH fallback:tercer comando es google-chrome",
        llamados[2:3] == [["google-chrome", "--version"]],
        f"(comandos={llamados})",
    )


def test_macos_sin_binarios(check):
    """macOS sin ningun binario: None sin lanzar y agota todos los comandos."""
    llamados = []

    with _simular("darwin", _no_existe, llamados):
        version = detectar_chrome_version()

    esperados = [
        [RUTA_CHROME_MAC, "--version"],
        [RUTA_CHROMIUM_MAC, "--version"],
        ["google-chrome", "--version"],
        ["chromium", "--version"],
        ["chromium-browser", "--version"],
    ]
    check(
        "macOS|sin binarios:devuelve None sin excepcion",
        version is None,
        f"(version={version})",
    )
    check(
        "macOS|sin binarios:prueba las .app y luego el PATH",
        llamados == esperados,
        f"(comandos={llamados})",
    )


def test_linux_conserva_google_chrome(check):
    """Linux: el primer comando sigue siendo `google-chrome`."""
    llamados = []

    with _simular("linux", lambda cmd: "Google Chrome 152.0.6478.126", llamados):
        version = detectar_chrome_version()

    check(
        "Linux:devuelve la version mayor 152",
        version == 152,
        f"(version={version})",
    )
    check(
        "Linux:primer comando es google-chrome",
        llamados[:1] == [["google-chrome", "--version"]],
        f"(comandos={llamados})",
    )
    check(
        "Linux:no prueba rutas .app de macOS",
        all(not comando[0].startswith("/Applications/") for comando in llamados),
        f"(comandos={llamados})",
    )


def test_salida_invalida(check):
    """Salida sin numero de version: sigue probando y termina en None."""
    llamados = []

    with _simular("darwin", lambda cmd: "sin version", llamados):
        version = detectar_chrome_version()

    check(
        "macOS|salida invalida:devuelve None sin lanzar",
        version is None,
        f"(version={version})",
    )
    check(
        "macOS|salida invalida:no corta la busqueda tras la primera salida",
        len(llamados) == 5,
        f"(probados={len(llamados)})",
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_macos_chrome_app(check)
    test_macos_fallback_chromium(check)
    test_macos_fallback_path(check)
    test_macos_sin_binarios(check)
    test_linux_conserva_google_chrome(check)
    test_salida_invalida(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_macos_chrome.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
