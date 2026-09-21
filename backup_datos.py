"""
Respaldo de los datos criticos de GestorRedes-Telegram-Final.

Genera un ZIP con timestamp en `data/backups/` que incluye:
    - data/gestor_redes.db          (base de datos SQLite)
    - data/cookies/**               (sesiones de las cuentas)
    - data/web_users.json           (usuarios del dashboard)
    - config/config.json            (configuracion del monitor)
    - data/avatars/**               (fotos de perfil)
    - data/portadas/**              (fotos de portada)
    - data/reportes/**              (solo con --con-reportes)

SEGURIDAD: el archivo `.env` (tokens, API keys y passwords) NUNCA se incluye
en el respaldo. No existe ninguna bandera para empaquetarlo.

Si el volumen de Railway se pierde, este ZIP permite recuperar la base de
datos y las cookies de las cuentas sin volver a iniciar sesion una por una.

Uso:
    python backup_datos.py
    python backup_datos.py --destino D:/respaldos
    python backup_datos.py --con-reportes
"""

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    # Ruta absoluta desde la raiz del proyecto (core/config.py).
    from core.config import resolver_ruta
except Exception:  # pragma: no cover - el respaldo debe funcionar aunque falle la config
    def resolver_ruta(relativa: str) -> str:
        """Fallback sin dependencias: resuelve relativo a este archivo."""
        return str((Path(__file__).resolve().parent / relativa).resolve())


PROJECT_ROOT = Path(resolver_ruta("."))

# (etiqueta, ruta relativa a la raiz, tipo: "archivo" o "carpeta")
CATEGORIAS = [
    ("Base de datos", "data/gestor_redes.db", "archivo"),
    ("Cookies", "data/cookies", "carpeta"),
    ("Usuarios web", "data/web_users.json", "archivo"),
    ("Config del monitor", "config/config.json", "archivo"),
    ("Avatares", "data/avatars", "carpeta"),
    ("Portadas", "data/portadas", "carpeta"),
]
CATEGORIA_REPORTES = ("Reportes", "data/reportes", "carpeta")

# Archivos de sistema que nunca aportan al respaldo.
_IGNORAR = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}


def _mb(bytes_: int) -> float:
    """Convierte bytes a megabytes (para el resumen legible)."""
    return bytes_ / (1024 * 1024)


def _es_basura(ruta: Path) -> bool:
    """True si el archivo es de sistema/temporal y no debe respaldarse."""
    return ruta.name in _IGNORAR or ruta.name.startswith("~$")


def _recolectar(tipo: str, ruta: Path) -> list:
    """Devuelve los archivos de una categoria (lista vacia si no existe)."""
    if tipo == "archivo":
        return [ruta] if ruta.is_file() else []
    if not ruta.is_dir():
        return []
    return [
        item
        for item in sorted(ruta.rglob("*"))
        if item.is_file() and not _es_basura(item)
    ]


def crear_backup(destino: str, con_reportes: bool = False) -> int:
    """Crea el ZIP de respaldo y muestra el resumen por categoria.

    Devuelve el codigo de salida (0 = OK, 1 = error amigable).
    """
    destino_dir = Path(destino)
    if not destino_dir.is_absolute():
        destino_dir = Path(resolver_ruta(destino))
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_zip = destino_dir / f"gestor_backup_{marca}.zip"

    categorias = list(CATEGORIAS)
    if con_reportes:
        categorias.append(CATEGORIA_REPORTES)

    print("Respaldo de datos de GestorRedes")
    print(f"Origen : {PROJECT_ROOT}")
    print(f"Destino: {ruta_zip}")
    print("-" * 60)

    try:
        destino_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ERROR: no se pudo crear la carpeta de destino '{destino_dir}': {exc}")
        return 1

    recolectado = []
    faltantes = []
    for etiqueta, relativa, tipo in categorias:
        archivos = _recolectar(tipo, PROJECT_ROOT / relativa)
        if not archivos:
            faltantes.append(f"{etiqueta} ({relativa})")
            print(f"  AVISO: no se encontro nada en {relativa}")
            continue
        recolectado.append((etiqueta, archivos))

    if not recolectado:
        print("ERROR: no hay nada que respaldar; revisa que el proyecto tenga datos.")
        return 1

    total_archivos = 0
    total_bytes = 0
    try:
        with zipfile.ZipFile(ruta_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for etiqueta, archivos in recolectado:
                bytes_cat = 0
                for archivo in archivos:
                    zf.write(archivo, arcname=archivo.relative_to(PROJECT_ROOT).as_posix())
                    bytes_cat += archivo.stat().st_size
                total_archivos += len(archivos)
                total_bytes += bytes_cat
                print(f"  [OK] {etiqueta}: {len(archivos)} archivo(s), {_mb(bytes_cat):.2f} MB")
    except OSError as exc:
        print(f"ERROR: no se pudo escribir el ZIP '{ruta_zip}': {exc}")
        print("       Verifica permisos y espacio libre en el destino.")
        return 1

    print("-" * 60)
    print(f"Respaldo creado: {ruta_zip}")
    try:
        tam_zip = ruta_zip.stat().st_size
    except OSError:
        tam_zip = total_bytes
    print(
        f"Total: {total_archivos} archivo(s), {_mb(total_bytes):.2f} MB sin comprimir, "
        f"{_mb(tam_zip):.2f} MB en disco"
    )
    if faltantes:
        print("Categorias no encontradas (no se incluyeron): " + ", ".join(faltantes))
    print("Recuerda: el archivo .env NUNCA se incluye en el respaldo.")
    return 0


def main() -> int:
    """Punto de entrada CLI con argparse."""
    parser = argparse.ArgumentParser(
        description=(
            "Crea un ZIP con los datos criticos del proyecto: base de datos, "
            "cookies, usuarios web, config, avatares y portadas."
        ),
        epilog=(
            "SEGURIDAD: el archivo .env (tokens, API keys y passwords) NUNCA "
            "se incluye en el respaldo; no existe ninguna bandera para "
            "empaquetarlo. En Railway puedes programar este script para "
            "respaldar el volumen a un destino externo."
        ),
    )
    parser.add_argument(
        "--destino",
        default="data/backups",
        help=(
            "Carpeta donde guardar el ZIP (por defecto: data/backups). "
            "Las rutas relativas se resuelven desde la raiz del proyecto."
        ),
    )
    parser.add_argument(
        "--con-reportes",
        action="store_true",
        help="Incluye tambien data/reportes/** (historiales y logs).",
    )
    args = parser.parse_args()

    try:
        return crear_backup(args.destino, con_reportes=args.con_reportes)
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        return 130
    except Exception as exc:  # nunca mostrar stacktrace crudo al operador
        print(f"ERROR inesperado: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
