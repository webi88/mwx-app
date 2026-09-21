"""Runner de la suite rapida de regresion (solo stdlib, sin dependencias nuevas).

Descubre `tests/test_*.py`, importa cada modulo y ejecuta su funcion `run(check)`
(contrato: cada check llama `check(nombre, condicion, extra="")`). Imprime
PASS/FAIL por check y un resumen final tipo `RESULTADO: 38/38`. Sale con codigo
0 si todo paso o 1 si hubo cualquier fallo (o si un archivo no se pudo cargar).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py

NO ejecuta `tests/smoke_*.py`: esos requieren Chrome real y se corren a mano
(ver `tests/README.md`).
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent

# `tests/` a sys.path para que un test ejecutado suelto pueda importar `run_tests`.
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

_TOTAL = 0
_FALLOS: list = []
_ARCHIVO = ""


def check(nombre: str, condicion, extra="") -> bool:
    """Registra e imprime UN check con nombre claro en espanol.

    Devuelve el booleano normalizado para que los tests puedan encadenar.
    """
    global _TOTAL
    _TOTAL += 1
    ok = bool(condicion)
    sufijo = f" {extra}" if extra not in ("", None) else ""
    if ok:
        print(f"  PASS {nombre}{sufijo}", flush=True)
    else:
        print(f"  FAIL {nombre}{sufijo}", flush=True)
        _FALLOS.append(f"{_ARCHIVO}::{nombre}")
    return ok


def resumen() -> int:
    """Imprime el resumen final y devuelve el codigo de salida (0/1)."""
    pasados = _TOTAL - len(_FALLOS)
    print(flush=True)
    if _FALLOS:
        print(f"FALLOS ({len(_FALLOS)}):", flush=True)
        for nombre in _FALLOS:
            print(f"  - {nombre}", flush=True)
    print(f"RESULTADO: {pasados}/{_TOTAL}", flush=True)
    return 0 if not _FALLOS else 1


def _cargar_modulo(ruta: Path):
    """Importa un `tests/test_*.py` como modulo independiente."""
    nombre = f"tests_{ruta.stem}"
    especificacion = importlib.util.spec_from_file_location(nombre, ruta)
    if especificacion is None or especificacion.loader is None:
        raise ImportError(f"No se pudo cargar {ruta}")
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[nombre] = modulo
    especificacion.loader.exec_module(modulo)
    return modulo


def _ejecutar_archivo(ruta: Path) -> None:
    """Ejecuta un archivo de test y reporta su parcial."""
    global _ARCHIVO
    _ARCHIVO = ruta.name
    print(f"\n=== {ruta.name} ===", flush=True)
    total_antes = _TOTAL
    fallos_antes = len(_FALLOS)
    try:
        modulo = _cargar_modulo(ruta)
        ejecutar = getattr(modulo, "run", None)
        if not callable(ejecutar):
            check(f"{ruta.name}: expone una funcion run(check)", False, "(no existe run)")
            return
        ejecutar(check)
    except Exception:
        check(f"{ruta.name}: se ejecuto sin errores", False)
        traceback.print_exc()
    finally:
        hechos = _TOTAL - total_antes
        fallos = len(_FALLOS) - fallos_antes
        print(f"  -- {ruta.name}: {hechos - fallos}/{hechos}")


def main() -> int:
    archivos = sorted(TESTS.glob("test_*.py"))
    if not archivos:
        print("No se encontro ningun tests/test_*.py")
        return 1
    print(f"Suite rapida de regresion ({len(archivos)} archivo(s))")
    for ruta in archivos:
        _ejecutar_archivo(ruta)
    return resumen()


if __name__ == "__main__":
    sys.exit(main())
