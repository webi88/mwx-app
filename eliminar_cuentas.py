"""
Eliminacion masiva de cuentas por lista, con respaldo previo.

Lee una lista de cuentas (una por linea: `usuario`, `@usuario` o la linea
completa del vendedor) y elimina de la base de datos SOLO las que existan.
Antes de borrar escribe un respaldo JSON (`data/backups/eliminacion_cuentas_*`)
con todas las columnas de esas cuentas y sus tareas relacionadas, de modo que
`restaurar_cuentas.py` pueda reponerlas si hiciera falta.

Por defecto corre en SIMULACION (dry-run): solo consulta y muestra que cuentas
existen, sin escribir nada. La eliminacion real requiere `--apply`.

La operacion apunta a la base de datos de `DATABASE_URL` (por defecto la local
`data/gestor_redes.db`). Para operar en Supabase, exporta la DATABASE_URL de
Railway antes de correr (los valores reales no van en el repo):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python eliminar_cuentas.py lista.txt --apply

Uso:
    python eliminar_cuentas.py lista.txt                  # dry-run (no escribe)
    python eliminar_cuentas.py lista.txt --apply          # eliminacion real
    python eliminar_cuentas.py --usuarios "a,b" --apply   # sin archivo
    python eliminar_cuentas.py lista.txt --apply --sin-cookies
    python eliminar_cuentas.py lista.txt --apply --sin-tareas --sin-respaldo
    python eliminar_cuentas.py lista.txt --json           # resumen en JSON

AVISO: `--apply` BORRA cuentas de la BD de DATABASE_URL (con respaldo previo).
Este script nunca imprime credenciales ni el contenido de las lineas.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

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


def _descripcion_destino() -> str:
    """Descripcion de la BD destino sin exponer credenciales (reutilizada de
    `restaurar_cuentas.py`)."""
    try:
        from restaurar_cuentas import _describir_destino_bd

        return _describir_destino_bd()
    except Exception:
        try:
            from core.config import settings

            url = settings.database_url or ""
        except Exception:
            url = ""
        if not url:
            return "DATABASE_URL no definida; se usara la local por defecto"
        if url.startswith("sqlite"):
            return f"SQLite: {resolver_ruta(url.split(':///', 1)[-1])}"
        return "remota (credenciales ocultas)"


def _leer_lista(ruta) -> Optional[str]:
    """Lee el archivo de cuentas (texto crudo). None si no se pudo leer."""
    try:
        with Path(ruta).open("r", encoding="utf-8", errors="replace") as manejador:
            return manejador.read()
    except Exception:
        return None


def _imprimir_resumen(resultado: dict, origen: str) -> None:
    """Resumen legible para el operador (nunca imprime credenciales)."""
    if resultado.get("dry_run"):
        modo = "SIMULACION (dry-run, no escribe)"
    else:
        modo = "REAL (escribe en la BD)"
    print("=" * 62)
    print("Eliminacion masiva de cuentas")
    print("=" * 62)
    print(f"Lista     : {origen}")
    print(f"BD destino: {_descripcion_destino()}")
    print(f"Modo      : {modo}")
    if resultado.get("error"):
        print("ESTADO    : con errores (no se aplico nada)")
    print("-" * 62)

    print(f"Solicitadas  : {int(resultado.get('solicitadas') or 0)}")

    if resultado.get("dry_run"):
        encontradas = list(resultado.get("encontradas") or [])
        print(f"Encontradas  : {len(encontradas)}")
        for usuario in encontradas[:20]:
            print(f"  - {usuario}")
        if len(encontradas) > 20:
            print(f"  ... y {len(encontradas) - 20} mas")
        no_encontradas = list(resultado.get("no_encontradas") or [])
        print(f"No encontradas: {len(no_encontradas)}")
        for usuario in no_encontradas[:20]:
            print(f"  - {usuario}")
        if len(no_encontradas) > 20:
            print(f"  ... y {len(no_encontradas) - 20} mas")
    else:
        eliminadas = list(resultado.get("eliminadas") or [])
        no_encontradas = list(resultado.get("no_encontradas") or [])
        print(f"Eliminadas     : {len(eliminadas)}")
        for usuario in eliminadas[:20]:
            print(f"  - {usuario}")
        if len(eliminadas) > 20:
            print(f"  ... y {len(eliminadas) - 20} mas")
        print(f"No encontradas : {len(no_encontradas)}")
        print(f"Archivos borrados: {int(resultado.get('archivos_borrados') or 0)}")
        print(f"Tareas canceladas: {int(resultado.get('tareas_canceladas') or 0)}")
        print(f"Respaldo         : {resultado.get('respaldo') or '(sin respaldo)'}")

    if resultado.get("error"):
        print("-" * 62)
        print(f"ERROR: {resultado['error']}")

    print("-" * 62)
    if resultado.get("dry_run"):
        print("AVISO: simulacion sin cambios; la eliminacion real requiere --apply.")
    elif resultado.get("error"):
        print("Nada se elimino (rollback). Revisa el error de arriba.")
    else:
        print("Eliminacion aplicada (transaccion confirmada).")
    print(
        "AVISO: la operacion apunta a la BD de DATABASE_URL (por defecto la "
        "local data/gestor_redes.db). Para Supabase, exporta la DATABASE_URL "
        "de Railway antes de correr."
    )


def main(argv=None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = argparse.ArgumentParser(
        description=(
            "Elimina cuentas de la base de datos de DATABASE_URL a partir de "
            "una lista (usuario, @usuario o linea del vendedor). Por defecto "
            "es un DRY-RUN: solo muestra cuales existen; la eliminacion real "
            "requiere --apply. Siempre respalda antes (salvo --sin-respaldo)."
        ),
        epilog=(
            "SEGURIDAD: no imprime credenciales ni el contenido de las lineas. "
            "Para Supabase, exporta la DATABASE_URL de Railway antes de correr: "
            "la eliminacion opera en ESA base de datos. El respaldo se guarda "
            "en data/backups/eliminacion_cuentas_*.json."
        ),
    )
    parser.add_argument(
        "ruta_lista",
        nargs="?",
        default=None,
        help=(
            "Archivo con una cuenta por linea (usuario, @usuario o linea del "
            "vendedor). Se puede omitir si se usa --usuarios."
        ),
    )
    parser.add_argument(
        "--usuarios",
        default="",
        help="Usuarios separados por comas, adicionales al archivo (o solos).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Elimina de verdad (con respaldo previo). Sin esta bandera todo es dry-run.",
    )
    parser.add_argument(
        "--sin-cookies",
        action="store_true",
        help="No borra los archivos locales de cookies ni avatar/portada.",
    )
    parser.add_argument(
        "--sin-tareas",
        action="store_true",
        help="No cancela las tareas pendientes que dependian de esas cuentas.",
    )
    parser.add_argument(
        "--sin-respaldo",
        action="store_true",
        help="No escribe el respaldo JSON previo (NO recomendado).",
    )
    parser.add_argument(
        "--json",
        dest="como_json",
        action="store_true",
        help="Imprime solo el resumen en JSON (util para automatizar).",
    )
    args = parser.parse_args(argv)

    # Import perezoso: el script no importa core.database (ni conecta) al cargarse.
    from cuentas import eliminador

    texto = ""
    origen = "(sin lista)"
    if args.ruta_lista:
        crudo = _leer_lista(args.ruta_lista)
        if crudo is None:
            print(f"ERROR: no se pudo leer la lista '{args.ruta_lista}'.")
            return 1
        texto += crudo
        origen = args.ruta_lista
    if args.usuarios:
        texto += "\n" + args.usuarios.replace(",", "\n")
        if not args.ruta_lista:
            origen = "--usuarios"

    usuarios = eliminador.parsear_lista_usuarios(texto)
    if not usuarios:
        print(
            "ERROR: no se indico ninguna cuenta valida.\n"
            '       Pasa un archivo: python eliminar_cuentas.py lista.txt\n'
            '       o usa: python eliminar_cuentas.py --usuarios "usuario1,usuario2"'
        )
        return 1

    if not args.apply:
        # DRY-RUN: solo consulta, no escribe nada.
        consulta = eliminador.buscar_cuentas(usuarios)
        resultado = {
            "ok": not consulta.get("error"),
            "dry_run": True,
            "solicitadas": len(usuarios),
            "encontradas": [c.get("usuario", "") for c in consulta.get("encontradas") or []],
            "no_encontradas": list(consulta.get("no_encontradas") or []),
            "error": consulta.get("error") or "",
        }
    else:
        crudo = eliminador.eliminar_usuarios(
            usuarios,
            borrar_cookies=not args.sin_cookies,
            cancelar_tareas=not args.sin_tareas,
            respaldar=not args.sin_respaldo,
        )
        resultado = dict(crudo)
        resultado["ok"] = not crudo.get("error")
        resultado["dry_run"] = False
        resultado["solicitadas"] = len(usuarios)

    if args.como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resultado, origen)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
