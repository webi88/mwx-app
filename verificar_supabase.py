"""Verifica la conexión y cuenta filas en una base de datos (Supabase).

Sirve para confirmar:
  1) Que la URL de Supabase funciona.
  2) Cuántas filas hay en esa base (si migraste ahí o no).

Uso:
    .venv/Scripts/python.exe verificar_supabase.py "postgresql://postgres.REF:PASS@db.REF.supabase.co:5432/postgres"
"""
import sys

from sqlalchemy import create_engine, select, func

import core.models  # noqa: F401  (registra modelos en Base.metadata)
from core.database import Base


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    if not url or "sqlite" in url:
        print('Uso: .venv/Scripts/python.exe verificar_supabase.py "postgresql://..."')
        sys.exit(1)

    connect_args = {} if "sslmode" in url else {"sslmode": "require"}
    engine = create_engine(url, connect_args=connect_args)

    try:
        with engine.connect() as c:
            print("Conexión OK a la base de datos.\n")
            for nombre in ["celulas", "clientes", "cuentas"]:
                tabla = Base.metadata.tables[nombre]
                n = c.execute(select(func.count()).select_from(tabla)).scalar()
                print(f"  {nombre}: {n} filas")
    except Exception as e:
        print(f"❌ Error conectando: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
