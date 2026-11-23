"""Tests del arranque de Chrome en MWX APP (PyInstaller, Windows 64-bit).

Objetivo: que `plataformas/chrome_driver.py` elija SIEMPRE una carpeta de
drivers escribible, reutilice un chromedriver ya parcheado (incluido el que
viaja dentro del `.exe`) y nunca falle solo porque la carpeta del ejecutable es
de solo lectura.

Cubre:
  (1) `_dir_drivers` sin env: settings.base_dir / resolver_ruta;
  (2) override `MWX_DRIVER_DIR` y prioridad sobre settings;
  (3) `settings.data_dir` (API frozen-safe que agrega core);
  (4) `sys.frozen` simulado;
  (5) fallback a `%LOCALAPPDATA%/MWX APP/bin` (o `~/.mwx_app/bin`) cuando la
      carpeta preferida lanza PermissionError (Path.mkdir monkeypatcheado);
  (6) driver empaquetado en `sys._MEIPASS/bin` con y sin version + copia;
  (7) copia empaquetada fallida -> ejecucion directa desde `_MEIPASS`;
  (8) reutilizacion sin bootstrap si el driver valido ya existe;
  (9) bootstrap con PermissionError -> reintento en la carpeta de respaldo;
  (10) el lock vive en la MISMA carpeta del driver (compatibilidad actual);
  (11) la decision de carpeta se cachea por proceso.

No usan red, Chrome ni `data/` real: todas las rutas viven en tempdirs.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_chromedriver.py   (solo este)
"""
from __future__ import annotations

import contextlib
import errno
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# La raiz del repo a sys.path (mismo patron que el resto de tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import plataformas.chrome_driver as cd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _sin_env_driver():
    """Quita del entorno las variables que influyen en la eleccion de carpeta."""
    claves = ("MWX_DRIVER_DIR", "LOCALAPPDATA", "APPDATA", "HOME")
    previos = {clave: os.environ.get(clave) for clave in claves}
    for clave in claves:
        os.environ.pop(clave, None)
    try:
        yield
    finally:
        for clave, valor in previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor


@contextlib.contextmanager
def _env_driver(carpeta):
    """Entorno minimo con `MWX_DRIVER_DIR` apuntando a un tempdir."""
    with _sin_env_driver(), mock.patch.dict(
        os.environ, {"MWX_DRIVER_DIR": str(carpeta)}
    ):
        yield


def _reset():
    """Olvida la carpeta cacheada y las rutas ejecutables ya sondeadas."""
    cd._limpiar_cache_dir_drivers()
    cd._CACHE_EJECUTABLE.clear()


def _driver_falso(ruta, parcheado=True) -> str:
    """Crea un archivo con la marca de undetected-chromedriver (sin ejecutarlo)."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    contenido = (cd._MARCA_PARCHEADO + b"\n") if parcheado else b"sin parche\n"
    ruta.write_bytes(contenido)
    return str(ruta)


def _nombre_driver(version) -> str:
    """Nombre versionado que usa el proyecto en la plataforma actual.

    Se calcula SIN tocar `_dir_drivers()` para que el helper no cree carpetas
    reales ni dependa de la cache cuando solo se necesita el nombre.
    """
    nombre = f"undetected_chromedriver_{version}"
    if os.name == "nt":
        nombre += ".exe"
    return nombre


def _sin_frozen():
    """Garantiza `sys.frozen = False` aunque el test corra empaquetado."""
    return mock.patch.object(sys, "frozen", False, create=True)


# ---------------------------------------------------------------------------
# (1)-(4) Eleccion de carpeta
# ---------------------------------------------------------------------------


def test_dir_sin_env(check):
    """Sin env usa settings.base_dir/data/bin (equivalente a resolver_ruta)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        proyecto = tmp / "proyecto"
        with mock.patch.object(cd, "settings", SimpleNamespace(base_dir=proyecto)):
            with _sin_env_driver():
                _reset()
                carpeta = cd._dir_drivers()
        check(
            "sin env: usa base_dir/data/bin",
            Path(carpeta) == proyecto / "data" / "bin",
            f"({carpeta})",
        )
        check(
            "sin env: crea la carpeta de drivers",
            (proyecto / "data" / "bin").is_dir(),
        )
        check(
            "sin env: guarda la decision en el cache de modulo",
            cd._CACHE_DIR_DRIVERS == carpeta,
        )

    # settings sin data_dir/app_dir/base_dir -> resolver_ruta historico.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with mock.patch.object(cd, "settings", SimpleNamespace()):
            with mock.patch.object(
                cd, "resolver_ruta", lambda rel: str(tmp / "resolver" / Path(rel))
            ):
                with _sin_env_driver():
                    _reset()
                    carpeta = cd._dir_drivers()
        check(
            "sin settings: cae a resolver_ruta('data/bin')",
            Path(carpeta) == tmp / "resolver" / "data" / "bin",
            f"({carpeta})",
        )


def test_env_y_settings(check):
    """`MWX_DRIVER_DIR` manda; sin el, se usa `settings.data_dir/data/bin`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env_dir = tmp / "env_drivers"
        datos = tmp / "datos"
        with mock.patch.object(cd, "settings", SimpleNamespace(data_dir=str(datos))):
            with _env_driver(env_dir):
                _reset()
                carpeta = cd._dir_drivers()
                check(
                    "MWX_DRIVER_DIR tiene prioridad sobre settings.data_dir",
                    Path(carpeta) == env_dir,
                    f"({carpeta})",
                )
                # La decision se cachea: cambiar el env no la altera.
                os.environ["MWX_DRIVER_DIR"] = str(tmp / "otro")
                check(
                    "cache: no re-evalua el env en la misma sesion",
                    cd._dir_drivers() == carpeta,
                )

            # Fuera del env: se usa settings.data_dir (siempre en tempdir).
            _reset()
            carpeta = cd._dir_drivers()
            check(
                "sin env: usa settings.data_dir/data/bin",
                Path(carpeta) == datos / "data" / "bin",
                f"({carpeta})",
            )

            with _env_driver(env_dir):
                _reset()
                check(
                    "limpiar cache: vuelve a leer el env",
                    Path(cd._dir_drivers()) == env_dir,
                )


def test_data_dir_semantica(check):
    """Si `settings.data_dir` ya es la carpeta `data`, no duplica `data/data`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data_dir = tmp / "MWX APP" / "data"
        with mock.patch.object(
            cd, "settings", SimpleNamespace(data_dir=str(data_dir))
        ):
            with _sin_env_driver():
                _reset()
                carpeta = cd._dir_drivers()
        check(
            "data_dir que ya termina en 'data' -> usa <data_dir>/bin",
            Path(carpeta) == data_dir / "bin",
            f"({carpeta})",
        )
        check(
            "data_dir que ya termina en 'data' -> no crea data/data/bin",
            not (data_dir / "data" / "bin").exists(),
        )


def test_frozen_simulado(check):
    """Con `sys.frozen=True` la carpeta elegida sigue siendo escribible."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        datos = tmp / "datos_frozen"
        with mock.patch.object(cd, "settings", SimpleNamespace(data_dir=str(datos))):
            with _sin_env_driver():
                with mock.patch.object(sys, "frozen", True, create=True):
                    _reset()
                    carpeta = cd._dir_drivers()
        check(
            "frozen simulado: usa settings.data_dir/data/bin",
            Path(carpeta) == datos / "data" / "bin",
            f"({carpeta})",
        )
        check(
            "frozen simulado: crea la carpeta fuera del _MEIPASS",
            (datos / "data" / "bin").is_dir(),
        )


def test_fallback_permisos(check):
    """Si la carpeta preferida es de solo lectura, se usa el respaldo."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        principal = tmp / "solo_lectura"
        respaldo = tmp / "respaldo_usuario"
        mkdir_original = Path.mkdir

        def mkdir_falso(self, *args, **kwargs):
            if str(self).lower().startswith(str(principal).lower()):
                raise PermissionError(13, "Acceso denegado (simulado)", str(self))
            return mkdir_original(self, *args, **kwargs)

        with _sin_env_driver():
            os.environ["MWX_DRIVER_DIR"] = str(principal)
            with mock.patch.object(
                cd, "settings", SimpleNamespace(data_dir=str(principal))
            ):
                with mock.patch.object(
                    cd, "_candidatos_fallback", return_value=[str(respaldo)]
                ):
                    with mock.patch.object(Path, "mkdir", mkdir_falso):
                        _reset()
                        carpeta = cd._dir_drivers()
        check(
            "permisos: abandona la carpeta de solo lectura",
            Path(carpeta) == respaldo,
            f"({carpeta})",
        )
        check(
            "permisos: crea y usa la carpeta de respaldo",
            respaldo.is_dir() and cd._puede_escribir(str(respaldo)),
        )


def test_dir_fallback(check):
    """El respaldo principal es LOCALAPPDATA\\MWX APP\\bin (o ~/.mwx_app/bin)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if os.name == "nt":
            with _sin_env_driver():
                os.environ["LOCALAPPDATA"] = str(tmp / "local")
                esperado = tmp / "local" / "MWX APP" / "bin"
                obtenido = Path(cd._dir_drivers_fallback())
            check(
                "fallback Windows: LOCALAPPDATA/MWX APP/bin",
                obtenido == esperado,
                f"({obtenido})",
            )
            check(
                "fallback Windows: nunca apunta a la carpeta del exe",
                "AppData" in str(obtenido),
            )
        else:
            with _sin_env_driver():
                os.environ["HOME"] = str(tmp / "home")
                esperado = tmp / "home" / ".mwx_app" / "bin"
                obtenido = Path(cd._dir_drivers_fallback())
            check(
                "fallback POSIX: ~/.mwx_app/bin",
                obtenido == esperado,
                f"({obtenido})",
            )


# ---------------------------------------------------------------------------
# (5)-(7) Driver empaquetado en _MEIPASS
# ---------------------------------------------------------------------------


def test_empaquetado_no_frozen(check):
    """`_driver_empaquetado` solo busca dentro del bundle si `sys.frozen`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        meipass = tmp / "meipass"
        _driver_falso(meipass / "bin" / _nombre_driver(157))
        with _sin_frozen():
            with mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
                check(
                    "no frozen: no usa drivers de sys._MEIPASS",
                    cd._driver_empaquetado(157) == "",
                )


def test_frozen_copia_empaquetado(check):
    """En congelado copia el driver versionado de `_MEIPASS/bin` y lo reutiliza."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        principal = tmp / "drivers"
        meipass = tmp / "meipass"
        nombre = _nombre_driver(157)
        empaquetado = _driver_falso(meipass / "bin" / nombre)

        with _env_driver(principal):
            _reset()
            with mock.patch.object(sys, "frozen", True, create=True):
                with mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
                    check(
                        "frozen: encuentra el driver empaquetado de la version",
                        os.path.normcase(cd._driver_empaquetado(157))
                        == os.path.normcase(empaquetado),
                    )
                    with mock.patch.object(
                        cd,
                        "_bootstrap_driver",
                        side_effect=AssertionError("no debe bootstrapear"),
                    ):
                        with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                            resultado = cd._driver_compartido(157)
            destino = principal / nombre
            check(
                "frozen: copia el driver empaquetado a la carpeta elegida",
                Path(resultado) == destino and destino.is_file(),
                f"({resultado})",
            )
            check(
                "frozen: la copia conserva la marca de parcheado",
                cd._esta_parcheado(str(destino)),
            )

            # Segunda entrada: ya existe en la carpeta -> NO se vuelve a copiar.
            with mock.patch.object(
                cd,
                "_copiar_driver_atomico",
                side_effect=AssertionError("no debe copiar de nuevo"),
            ):
                with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                    reutilizado = cd._driver_compartido(157)
            check(
                "frozen: reutiliza el driver existente sin escribir",
                Path(reutilizado) == destino,
            )


def test_frozen_empaquetado_sin_version(check):
    """Acepta `undetected_chromedriver.exe` (sin version) como comodin."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        principal = tmp / "drivers"
        meipass = tmp / "meipass"
        nombre_generico = "undetected_chromedriver.exe" if os.name == "nt" else (
            "undetected_chromedriver"
        )
        _driver_falso(meipass / "bin" / nombre_generico)

        with _env_driver(principal):
            _reset()
            with mock.patch.object(sys, "frozen", True, create=True):
                with mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
                    with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                        resultado = cd._driver_compartido(157)
            esperado = principal / _nombre_driver(157)
            check(
                "frozen: acepta el driver empaquetado sin version",
                Path(resultado) == esperado and esperado.is_file(),
                f"({resultado})",
            )


def test_frozen_copia_fallida_ejecuta_directo(check):
    """Si no se puede copiar (solo lectura), ejecuta desde `_MEIPASS`."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        principal = tmp / "drivers"
        meipass = tmp / "meipass"
        empaquetado = _driver_falso(meipass / "bin" / _nombre_driver(157))

        def replace_denegado(origen, destino):
            raise PermissionError(13, "Acceso denegado (simulado)", str(destino))

        with _env_driver(principal):
            _reset()
            with mock.patch.object(sys, "frozen", True, create=True):
                with mock.patch.object(sys, "_MEIPASS", str(meipass), create=True):
                    # Falla REAL de os.replace (carpeta de solo lectura).
                    with mock.patch.object(cd.os, "replace", replace_denegado):
                        with mock.patch.object(
                            cd,
                            "_bootstrap_driver",
                            side_effect=AssertionError("no debe bootstrapear"),
                        ):
                            with mock.patch.object(
                                cd, "_ruta_ejecutable", lambda p: p
                            ):
                                resultado = cd._driver_compartido(157)
            check(
                "frozen: copia fallida (os.replace) -> usa _MEIPASS",
                resultado == empaquetado,
                f"({resultado})",
            )
            check(
                "frozen: no deja una copia ni .tmp a medias",
                not (principal / _nombre_driver(157)).exists()
                and not list(principal.glob("*.tmp*")),
            )


# ---------------------------------------------------------------------------
# (8)-(10) Reutilizacion, fallback de bootstrap y lock
# ---------------------------------------------------------------------------


def test_reutiliza_sin_bootstrap(check):
    """Un driver valido existente se reutiliza sin bootstrap ni escritura."""
    with tempfile.TemporaryDirectory() as tmp:
        principal = Path(tmp) / "drivers"
        with _env_driver(principal):
            _reset()
            existente = _driver_falso(cd._ruta_driver_estable(157))
            bootstrap = mock.Mock(
                side_effect=lambda destino, version: _driver_falso(destino)
            )
            copiar = mock.Mock(side_effect=AssertionError("no debe copiar"))
            with mock.patch.object(cd, "_bootstrap_driver", bootstrap):
                with mock.patch.object(cd, "_copiar_driver_atomico", copiar):
                    with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                        resultado = cd._driver_compartido(157)
            check(
                "reutiliza el driver valido existente",
                resultado == existente,
                f"({resultado})",
            )
            check(
                "no bootstrapea si el driver valido ya existe",
                bootstrap.call_count == 0,
                f"(llamadas={bootstrap.call_count})",
            )
            check(
                "no copia si el driver valido ya existe",
                copiar.call_count == 0,
                f"(llamadas={copiar.call_count})",
            )


def test_bootstrap_permisos_fallback(check):
    """Bootstrap con PermissionError -> reintenta en la carpeta de respaldo."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        principal = tmp / "drivers"
        respaldo = tmp / "respaldo_usuario"
        with _env_driver(principal):
            _reset()
            llamadas = []

            def bootstrap_falso(destino, version):
                llamadas.append(destino)
                if Path(destino).parent == principal:
                    raise PermissionError(
                        13, "Acceso denegado (simulado)", str(destino)
                    )
                return _driver_falso(destino)

            with mock.patch.object(
                cd, "_candidatos_fallback", return_value=[str(respaldo)]
            ):
                with mock.patch.object(
                    cd, "_bootstrap_driver", side_effect=bootstrap_falso
                ):
                    with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                        resultado = cd._driver_compartido(157)

            check(
                "bootstrap: primer intento en la carpeta elegida",
                len(llamadas) == 2
                and Path(llamadas[0]).parent == principal,
                f"({llamadas})",
            )
            check(
                "bootstrap: reintento en la carpeta de respaldo",
                len(llamadas) == 2
                and Path(llamadas[1]).parent == respaldo,
                f"({llamadas})",
            )
            check(
                "bootstrap: devuelve el driver del respaldo",
                Path(resultado) == respaldo / _nombre_driver(157)
                and Path(resultado).is_file(),
                f"({resultado})",
            )


def test_lock_en_carpeta_del_driver(check):
    """El lock entre procesos queda en la MISMA carpeta del driver."""
    with tempfile.TemporaryDirectory() as tmp:
        principal = Path(tmp) / "drivers"
        with _env_driver(principal):
            _reset()
            locks = []

            @contextlib.contextmanager
            def lock_falso(ruta):
                locks.append(ruta)
                yield

            with mock.patch.object(cd, "_lock_archivo", lock_falso):
                with mock.patch.object(
                    cd,
                    "_bootstrap_driver",
                    side_effect=lambda destino, version: _driver_falso(destino),
                ):
                    with mock.patch.object(cd, "_ruta_ejecutable", lambda p: p):
                        resultado = cd._driver_compartido(157)
            check(
                "lock: se crea en la carpeta del driver",
                bool(locks) and Path(locks[0]).parent == principal,
                f"({locks})",
            )
            check(
                "lock: conserva el nombre .chromedriver.lock",
                bool(locks) and Path(locks[0]).name == ".chromedriver.lock",
            )
            check(
                "lock: el bootstrap devuelve un driver valido",
                Path(resultado).is_file() and cd._esta_parcheado(resultado),
            )


# ---------------------------------------------------------------------------
# (11) Utilidades y errores de permisos
# ---------------------------------------------------------------------------


def test_errores_permiso(check):
    """`_es_error_permiso` reconoce solo errores de permisos/solo lectura."""
    check(
        "permiso: PermissionError",
        cd._es_error_permiso(PermissionError(13, "denegado")),
    )
    check(
        "permiso: OSError EACCES",
        cd._es_error_permiso(OSError(errno.EACCES, "denegado")),
    )
    check(
        "permiso: OSError EROFS (solo lectura)",
        cd._es_error_permiso(OSError(getattr(errno, "EROFS", 30), "read-only")),
    )
    check(
        "permiso: mensaje de Windows 'Access is denied'",
        cd._es_error_permiso(OSError("Access is denied")),
    )
    check(
        "permiso: un error de red NO se confunde con permisos",
        not cd._es_error_permiso(OSError("connection refused")),
    )


def run(check):
    """Ejecuta todos los checks con el `check` del runner (o del marco local)."""
    test_dir_sin_env(check)
    test_env_y_settings(check)
    test_data_dir_semantica(check)
    test_frozen_simulado(check)
    test_fallback_permisos(check)
    test_dir_fallback(check)
    test_empaquetado_no_frozen(check)
    test_frozen_copia_empaquetado(check)
    test_frozen_empaquetado_sin_version(check)
    test_frozen_copia_fallida_ejecuta_directo(check)
    test_reutiliza_sin_bootstrap(check)
    test_bootstrap_permisos_fallback(check)
    test_lock_en_carpeta_del_driver(check)
    test_errores_permiso(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_mwx_chromedriver.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
