"""
Restauracion de cuentas desde un respaldo JSON a la base de datos.

El respaldo creado al limpiar la flota (`data/backups/limpieza_cuentas_*.json`)
contiene las cuentas borradas y una lista `keep` con las que NUNCA deben
restaurarse. Este script permite reponerlas si mas adelante se necesitan.

Por defecto corre en SIMULACION (dry-run) y no escribe nada; la restauracion
real requiere `--apply`.

La restauracion apunta a la base de datos de `DATABASE_URL` (por defecto la
local `data/gestor_redes.db`). Para restaurar en Supabase, exporta la
DATABASE_URL de Railway antes de correr (los valores reales no van en el repo):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python restaurar_cuentas.py --apply --inactivas

Uso:
    python restaurar_cuentas.py                         # dry-run del ultimo respaldo
    python restaurar_cuentas.py --archivo ruta.json     # dry-run de un respaldo concreto
    python restaurar_cuentas.py --apply                 # restauracion real
    python restaurar_cuentas.py --apply --sobrescribir  # actualiza las ya existentes
    python restaurar_cuentas.py --apply --inactivas     # repone con activa=False
    python restaurar_cuentas.py --json                  # resumen en JSON
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    # Ruta absoluta desde la raiz del proyecto (core/config.py).
    from core.config import resolver_ruta
except Exception:  # pragma: no cover - el script debe funcionar aunque falle la config
    def resolver_ruta(relativa: str) -> str:
        """Fallback sin dependencias: resuelve relativo a este archivo."""
        return str((Path(__file__).resolve().parent / relativa).resolve())


PROJECT_ROOT = Path(resolver_ruta("."))
CARPETA_BACKUPS = PROJECT_ROOT / "data" / "backups"
PATRON_BACKUP = "limpieza_cuentas_*.json"


def buscar_ultimo_backup() -> Optional[Path]:
    """Devuelve el `limpieza_cuentas_*.json` mas reciente (o None).

    El nombre incluye la marca de tiempo (`limpieza_cuentas_YYYYMMDD_HHMMSS`),
    por lo que el orden alfabetico coincide con el cronologico."""
    if not CARPETA_BACKUPS.is_dir():
        return None
    archivos = sorted(CARPETA_BACKUPS.glob(PATRON_BACKUP))
    if not archivos:
        return None
    return archivos[-1]


def _contar_keep(archivo) -> Tuple[int, int]:
    """Lee el respaldo y devuelve (en `keep`, de esos presentes en `cuentas`).

    Solo se usa para desglosar el resumen legible; cualquier fallo devuelve
    (0, 0) sin interrumpir la restauracion (que ya reporta sus propios errores)."""
    try:
        with Path(archivo).open("r", encoding="utf-8") as manejador:
            data = json.load(manejador)
    except Exception:
        return (0, 0)
    if not isinstance(data, dict):
        return (0, 0)

    crudo = data.get("keep")
    usuarios = (
        {str(u).strip() for u in crudo if str(u or "").strip()}
        if isinstance(crudo, (list, tuple, set))
        else set()
    )
    if not usuarios:
        return (0, 0)

    presentes = 0
    cuentas = data.get("cuentas")
    if isinstance(cuentas, list):
        for fila in cuentas:
            if isinstance(fila, dict) and str(fila.get("usuario") or "").strip() in usuarios:
                presentes += 1
    return (len(usuarios), presentes)


def _describir_destino_bd() -> str:
    """Describe la BD destino SIN exponer credenciales."""
    try:
        from urllib.parse import urlsplit

        from core.config import settings

        url = settings.database_url or ""
    except Exception:
        url = ""
    if not url:
        return "DATABASE_URL no definida; se usara la local por defecto"
    if url.startswith("sqlite"):
        relativa = url.split(":///", 1)[-1]
        return f"SQLite: {resolver_ruta(relativa)}"
    try:
        partes = urlsplit(url)
        host = partes.hostname or "?"
        base = (partes.path or "").lstrip("/") or "?"
        return f"{partes.scheme or 'remota'}: {host}/{base} (credenciales ocultas)"
    except Exception:
        return "remota (credenciales ocultas)"


def _imprimir_resumen(resultado: dict, archivo) -> None:
    """Resumen legible para el operador (nunca imprime credenciales)."""
    modo = "SIMULACION (dry-run)" if resultado.get("dry_run") else "REAL (escribe en la BD)"
    print("=" * 62)
    print("Restauracion de cuentas desde respaldo JSON")
    print("=" * 62)
    print(f"Archivo   : {archivo}")
    print(f"BD destino: {_describir_destino_bd()}")
    print(f"Modo      : {modo}")
    if not resultado.get("ok"):
        print("ESTADO    : con errores (la restauracion no se completo)")
    print("-" * 62)

    total = int(resultado.get("total") or 0)
    omitidas = int(resultado.get("omitidas") or 0)
    _, keep_presentes = _contar_keep(archivo)
    candidatas = max(total - keep_presentes, 0)
    existentes = max(omitidas - keep_presentes, 0)

    print(f"Total en el respaldo : {total}")
    if keep_presentes:
        print(f"  En la lista 'keep' : {keep_presentes} (nunca se tocan)")
    print(f"  Candidatas         : {candidatas}")
    print(f"Restauradas (nuevas) : {int(resultado.get('restauradas') or 0)}")
    print(f"Actualizadas         : {int(resultado.get('actualizadas') or 0)}")
    if keep_presentes or existentes:
        print(
            f"Omitidas             : {omitidas} "
            f"(ya existentes: {existentes} + keep: {keep_presentes})"
        )
    else:
        print(f"Omitidas             : {omitidas}")
    print(f"Errores              : {int(resultado.get('errores') or 0)}")

    detalle = resultado.get("detalle_errores") or []
    if detalle:
        print("Primeros errores:")
        for mensaje in detalle[:5]:
            print(f"  - {mensaje}")
        if len(detalle) > 5:
            print(f"  ... y {len(detalle) - 5} mas")

    print("-" * 62)
    if resultado.get("dry_run"):
        print("AVISO: simulacion sin cambios; la restauracion real requiere --apply.")
    else:
        print("Restauracion aplicada (transaccion confirmada).")
    print(
        "AVISO: la restauracion apunta a la BD de DATABASE_URL (por defecto la "
        "local data/gestor_redes.db). Para Supabase, exporta la DATABASE_URL de "
        "Railway antes de correr."
    )


def main(argv=None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = argparse.ArgumentParser(
        description=(
            "Restaura cuentas desde un respaldo JSON (limpieza_cuentas_*.json) "
            "a la base de datos de DATABASE_URL. Por defecto es un DRY-RUN: no "
            "escribe nada hasta que se pasa --apply."
        ),
        epilog=(
            "SEGURIDAD: no imprime credenciales. Para Supabase, exporta la "
            "DATABASE_URL de Railway antes de correr: la restauracion escribe "
            "en ESA base de datos. El respaldo no se modifica (solo lectura)."
        ),
    )
    parser.add_argument(
        "archivo",
        nargs="?",
        default=None,
        help=(
            "Respaldo JSON a usar. Si se omite, toma el limpieza_cuentas_*.json "
            "mas reciente de data/backups/."
        ),
    )
    parser.add_argument(
        "--archivo",
        dest="archivo_opcion",
        default=None,
        help="Igual que la posicion anterior (se acepta como bandera explicita).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Escribe de verdad en la BD. Sin esta bandera todo es dry-run.",
    )
    parser.add_argument(
        "--sobrescribir",
        action="store_true",
        help=(
            "Si la cuenta ya existe (mismo usuario), pisa sus columnas en vez "
            "de omitirla. Nunca toca 'usuario', 'id' ni 'fecha_creacion'."
        ),
    )
    parser.add_argument(
        "--inactivas",
        action="store_true",
        help="Las cuentas NUEVAS se reponen con activa=False (no entran en campanas).",
    )
    parser.add_argument(
        "--json",
        dest="como_json",
        action="store_true",
        help="Imprime solo el resumen en JSON (util para automatizar).",
    )
    args = parser.parse_args(argv)

    archivo = args.archivo_opcion or args.archivo
    if not archivo:
        ultimo = buscar_ultimo_backup()
        if ultimo is None:
            print(
                f"ERROR: no se encontro ningun '{PATRON_BACKUP}' en {CARPETA_BACKUPS}.\n"
                "       Pasa la ruta explicitamente: "
                "python restaurar_cuentas.py --archivo ruta.json"
            )
            return 1
        archivo = str(ultimo)

    dry_run = not args.apply

    # Import perezoso: el script no importa core.database (ni conecta) al cargarse.
    from cuentas.restaurador import restaurar_desde_backup

    resultado = restaurar_desde_backup(
        archivo,
        dry_run=dry_run,
        sobrescribir=args.sobrescribir,
        inactivas=args.inactivas,
    )

    if args.como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resultado, archivo)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
