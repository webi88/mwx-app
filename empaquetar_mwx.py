# -*- coding: utf-8 -*-
"""Empaqueta ``dist/MWX APP`` en un ZIP de release para el autoactualizador.

Uso:
    python empaquetar_mwx.py
    python empaquetar_mwx.py --version 1.1.0
    python empaquetar_mwx.py --origen "dist/MWX APP" --destino data/releases --json

El script SOLO genera el ZIP local (y ``release.json``); NO publica nada en la
base de datos. Para publicarlo se usa ``core.app_releases.publicar_release``
desde el dashboard o un script administrativo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def _preparar_entorno() -> None:
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        os.environ["OPENAI_API_KEY"] = "mwx-empaquetar"


def _crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="empaquetar_mwx.py",
        description="Genera el ZIP de release del ejecutable MWX APP.",
    )
    parser.add_argument(
        "--version",
        default="",
        help="Version del release (default: mwx_app.__version__).",
    )
    parser.add_argument(
        "--origen",
        default="dist/MWX APP",
        help='Carpeta del build (default: "dist/MWX APP").',
    )
    parser.add_argument(
        "--destino",
        default="data/releases",
        help="Carpeta donde se escribe el ZIP (default: data/releases).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime el resultado completo en JSON.",
    )
    return parser


def _imprimir(resultado: dict, como_json: bool) -> None:
    if como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2), flush=True)
        return
    if resultado.get("ok"):
        print(f"ZIP: {resultado.get('zip', '')}", flush=True)
        print(f"Version: {resultado.get('version', '')}", flush=True)
        print(f"SHA256: {resultado.get('sha256', '')}", flush=True)
        print(f"Tamano: {resultado.get('tamano', 0)} bytes", flush=True)
    else:
        print(
            f"ERROR: {resultado.get('error') or 'no se pudo empaquetar'}",
            flush=True,
        )


def main(argv=None) -> int:
    argumentos = _crear_parser().parse_args(argv)
    _preparar_entorno()
    try:
        from core.app_releases import empaquetar_dist

        resultado = empaquetar_dist(
            origen=argumentos.origen,
            destino=argumentos.destino,
            version=argumentos.version,
        )
    except Exception as error:
        resultado = {
            "ok": False,
            "zip": "",
            "filename": "",
            "version": "",
            "sha256": "",
            "tamano": 0,
            "error": f"no se pudo empaquetar el dist: {error}",
        }
    _imprimir(resultado, argumentos.json)
    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
