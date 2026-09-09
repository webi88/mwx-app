"""Migra los datos de la BD local (SQLite) a Supabase (PostgreSQL).

La BD local (data/gestor_redes.db) tiene tus cuentas, células y clientes.
Supabase es una BD NUEVA y vacía: este script copia todas las tablas desde
el SQLite local hacia PostgreSQL.

Uso (desde la raíz del proyecto):

    .venv/Scripts/python.exe migrar_a_supabase.py "postgresql://postgres.REF:CONTRASEÑA@db.REF.supabase.co:5432/postgres"

La URL de Supabase la sacas de: Supabase -> Project Settings -> Database ->
Connection string -> (URI, conexión directa o Session pooler).
"""
import sys

from sqlalchemy import create_engine, select, func
from loguru import logger

from core.config import resolver_ruta
from core.database import Base

# Orden seguro para las foreign keys (padres antes que hijos).
TABLAS = [
    "celulas",
    "clientes",
    "cuentas",
    "alertas_historial",
    "menciones_dia",
    "reportes_diarios",
    "tareas",
    "registro_acciones",
]


def main():
    destino = sys.argv[1] if len(sys.argv) > 1 else None
    if not destino or "sqlite" in destino:
        print('Uso: .venv/Scripts/python.exe migrar_a_supabase.py "postgresql://..."')
        sys.exit(1)

    # Registra todos los modelos en Base.metadata antes de create_all.
    import core.models  # noqa: F401

    origen_url = f"sqlite:///{resolver_ruta('data/gestor_redes.db')}"
    engine_origen = create_engine(origen_url)

    connect_args = {} if "sslmode" in destino else {"sslmode": "require"}
    engine_destino = create_engine(destino, connect_args=connect_args)

    # 1) Crear tablas en el destino si no existen.
    Base.metadata.create_all(engine_destino)
    print("Tablas aseguradas en Supabase.\n")

    # 2) Aviso si el destino ya tiene cuentas (para no duplicar llaves).
    with engine_destino.connect() as d:
        ya_cuentas = d.execute(
            select(func.count()).select_from(Base.metadata.tables["cuentas"])
        ).scalar()
    if ya_cuentas:
        print(
            f"⚠️  Supabase ya tiene {ya_cuentas} cuentas. Si ya migraste antes, "
            "volver a correr esto puede fallar por llaves duplicadas (id)."
        )

    # 3) Copiar fila por fila.
    total = 0
    with engine_origen.connect() as src, engine_destino.begin() as dst:
        for nombre in TABLAS:
            tabla = Base.metadata.tables[nombre]
            filas = src.execute(select(tabla)).fetchall()
            if not filas:
                print(f"  {nombre}: 0 filas")
                continue
            datos = [dict(r._mapping) for r in filas]
            dst.execute(tabla.insert(), datos)
            total += len(datos)
            print(f"  {nombre}: {len(datos)} filas copiadas")

    print(f"\n✅ Migración completada: {total} filas copiadas a Supabase.")


if __name__ == "__main__":
    main()
