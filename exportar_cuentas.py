"""Exporta las cuentas a un archivo Excel (usuario + contraseña + datos).

Uso:
    python exportar_cuentas.py              # todas las cuentas
    python exportar_cuentas.py --activas    # solo cuentas activas
"""
import sys

from core.exportar import exportar_cuentas_excel


def main():
    solo_activas = "--activas" in sys.argv
    resultado = exportar_cuentas_excel(solo_activas=solo_activas)
    if resultado.get("error"):
        print(f"ERROR: {resultado['error']}")
        sys.exit(1)
    print(f"Excel generado: {resultado['ruta']} ({resultado['total']} cuentas)")


if __name__ == "__main__":
    main()
