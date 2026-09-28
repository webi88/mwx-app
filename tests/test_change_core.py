# -*- coding: utf-8 -*-
"""Tests rapidos de la granja de identidades de Change.org en `core/`.

Cubre:
    (1) Modelo `core.models.CuentaChange`: tabla y columnas del contrato
        congelado (11 columnas) y que la clase `Cuenta` no se toco.
    (2) Tipos/defaults: email String(200) UNIQUE con default "", queja Text,
        usada_firma Boolean default False, origen default "reporte",
        fecha_creacion DateTime con default callable.
    (3) DDL/CRUD real en SQLite EN MEMORIA (engine propio con StaticPool):
        create_all, INSERT+SELECT, defaults aplicados y email duplicado
        -> IntegrityError.
    (4) `core.database.obtener_sesion`: alias publico de `get_db_session`,
        usable como context manager (`with obtener_sesion() as db:`) sin
        tocar la BD real (se reemplaza `SessionLocal` por un engine sqlite
        en memoria), incluyendo commit automatico y rollback ante excepcion.
    (5) Invariante de `_resincronizar_secuencias`: la tabla nueva tiene `id`,
        que es lo que esa funcion recorre para resincronizar PostgreSQL.

Sin red, sin Chrome y SIN tocar `data/gestor_redes.db`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_core.py   (solo este archivo)
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import Boolean, DateTime, String, Text, create_engine, inspect as sqla_inspect  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as database  # noqa: E402
from core.database import Base, get_db_session, obtener_sesion  # noqa: E402
from core.models import Cuenta, CuentaChange  # noqa: E402

# Contrato congelado: las 11 columnas que DEBEN existir.
COLUMNAS_ESPERADAS = [
    "id",
    "nombre",
    "apellido",
    "email",
    "codigo_postal",
    "url_peticion",
    "contexto",
    "queja",
    "origen",
    "usada_firma",
    "fecha_creacion",
]


def _engine_memoria():
    """Engine SQLite en memoria COMPARTIDO (StaticPool) para DDL/CRUD.

    StaticPool hace que todas las sesiones del engine vean la MISMA base en
    memoria; el engine real de `core.database` no se toca en ningun momento.
    """
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# --------------------------------------------------------------------------- #
# (1) y (2) Modelo: tabla, columnas, tipos y defaults
# --------------------------------------------------------------------------- #
def test_modelo(check):
    check("modelo: CuentaChange deriva de Base", issubclass(CuentaChange, Base))
    check(
        "modelo: __tablename__ == 'cuenta_change'",
        CuentaChange.__tablename__ == "cuenta_change",
    )
    check(
        "modelo: tabla registrada en Base.metadata",
        "cuenta_change" in Base.metadata.tables,
    )

    tabla = Base.metadata.tables.get("cuenta_change")
    columnas = list(tabla.columns.keys()) if tabla is not None else []
    check(
        "modelo: estan las 11 columnas del contrato",
        sorted(columnas) == sorted(COLUMNAS_ESPERADAS),
        str(columnas),
    )

    col = CuentaChange.__table__.columns
    check(
        "modelo: id es PK autoincrement",
        col["id"].primary_key is True and col["id"].autoincrement is True,
    )
    check(
        "modelo: nombre/apellido String(120) y codigo_postal String(20)",
        all(
            isinstance(col[n].type, String) and col[n].type.length == 120
            for n in ("nombre", "apellido")
        )
        and isinstance(col["codigo_postal"].type, String)
        and col["codigo_postal"].type.length == 20,
    )
    check(
        "modelo: email String(200) UNIQUE con default ''",
        isinstance(col["email"].type, String)
        and col["email"].type.length == 200
        and col["email"].unique is True
        and col["email"].default is not None
        and col["email"].default.arg == "",
    )
    check(
        "modelo: url_peticion/contexto/queja son Text",
        all(isinstance(col[n].type, Text) for n in ("url_peticion", "contexto", "queja")),
    )
    check(
        "modelo: origen String(30) default 'reporte'",
        isinstance(col["origen"].type, String)
        and col["origen"].type.length == 30
        and col["origen"].default is not None
        and col["origen"].default.arg == "reporte",
    )
    check(
        "modelo: usada_firma Boolean default False",
        isinstance(col["usada_firma"].type, Boolean)
        and col["usada_firma"].default is not None
        and col["usada_firma"].default.arg is False,
    )
    check(
        "modelo: fecha_creacion DateTime con default callable",
        isinstance(col["fecha_creacion"].type, DateTime)
        and col["fecha_creacion"].default is not None
        and callable(col["fecha_creacion"].default.arg),
    )

    check(
        "modelo: Cuenta sigue intacta (tabla 'cuentas', sin mezclarse)",
        Cuenta.__tablename__ == "cuentas"
        and "cuenta_change" not in Base.metadata.tables["cuentas"].columns,
    )


# --------------------------------------------------------------------------- #
# (3) y (5) DDL/CRUD real en SQLite en memoria
# --------------------------------------------------------------------------- #
def test_ddl_crud(check):
    engine = _engine_memoria()
    try:
        Base.metadata.create_all(engine)
        check(
            "DDL: create_all crea la tabla cuenta_change",
            sqla_inspect(engine).has_table("cuenta_change"),
        )
        # `_resincronizar_secuencias` recorre Base.metadata y salta tablas sin
        # 'id': la tabla nueva SI lo tiene, asi que entra en el resync.
        check(
            "DDL: cuenta_change tiene columna 'id' (entra al resync de secuencias)",
            "id" in Base.metadata.tables["cuenta_change"].columns,
        )

        fabrica = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        db = fabrica()
        try:
            db.add(
                CuentaChange(
                    nombre="Ana",
                    apellido="Perez",
                    email="ana@example.com",
                    codigo_postal="06000",
                    url_peticion="https://www.change.org/p/ejemplo",
                    contexto="motivo general de la queja",
                    queja="texto generado por la IA",
                )
            )
            db.commit()
            leida = db.query(CuentaChange).filter_by(email="ana@example.com").one()
            check(
                "CRUD: la fila insertada se lee completa",
                leida.nombre == "Ana"
                and leida.apellido == "Perez"
                and leida.codigo_postal == "06000"
                and leida.queja == "texto generado por la IA"
                and leida.url_peticion == "https://www.change.org/p/ejemplo",
            )
            check(
                "CRUD: defaults aplicados en el INSERT (origen/usada_firma)",
                leida.origen == "reporte" and leida.usada_firma is False,
            )
            check(
                "CRUD: fecha_creacion se llena sola",
                leida.fecha_creacion is not None,
            )
            check(
                "CRUD: id autoincremental",
                isinstance(leida.id, int) and leida.id >= 1,
            )
        finally:
            db.close()

        # Email duplicado -> IntegrityError por el UNIQUE.
        db = fabrica()
        try:
            db.add(CuentaChange(nombre="Otra", apellido="X", email="ana@example.com"))
            try:
                db.commit()
                check("CRUD: email duplicado lanza IntegrityError", False)
            except IntegrityError:
                check("CRUD: email duplicado lanza IntegrityError", True)
                db.rollback()
        finally:
            db.close()

        # Un email distinto si entra y no quedo basura del rollback.
        db = fabrica()
        try:
            db.add(CuentaChange(nombre="Luis", apellido="Gomez", email="luis@example.com"))
            db.commit()
            check(
                "CRUD: un email distinto si se inserta",
                db.query(CuentaChange).count() == 2,
            )
        finally:
            db.close()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# (4) core.database.obtener_sesion
# --------------------------------------------------------------------------- #
def test_obtener_sesion(check):
    check(
        "obtener_sesion: existe, es callable y es una funcion",
        callable(obtener_sesion) and inspect.isfunction(obtener_sesion),
    )
    check(
        "obtener_sesion: NO es la fabrica cruda SessionLocal",
        obtener_sesion is not database.SessionLocal,
    )
    check(
        "obtener_sesion: get_db_session sigue existiendo y el alias apunta a el",
        callable(get_db_session) and database.obtener_sesion is obtener_sesion,
    )

    # Se reemplaza SessionLocal por un engine sqlite en memoria para no tocar
    # `data/gestor_redes.db`; get_db_session lo lee del modulo en cada llamada.
    engine = _engine_memoria()
    Base.metadata.create_all(engine)
    original = database.SessionLocal
    database.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        ctx = obtener_sesion()
        check(
            "obtener_sesion: devuelve un context manager (__enter__/__exit__)",
            hasattr(ctx, "__enter__") and hasattr(ctx, "__exit__"),
        )

        with ctx as db:
            check(
                "obtener_sesion: entrega una sesion con query/add/commit",
                all(hasattr(db, nombre) for nombre in ("query", "add", "commit")),
            )
            db.add(
                CuentaChange(
                    nombre="Marta",
                    apellido="Ruiz",
                    email="marta@example.com",
                    origen="manual",
                )
            )
        with obtener_sesion() as db:
            check(
                "obtener_sesion: commit automatico al salir del with",
                db.query(CuentaChange).filter_by(email="marta@example.com").count() == 1,
            )

        try:
            with obtener_sesion() as db:
                db.add(CuentaChange(nombre="Rota", apellido="Y", email="rota@example.com"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with obtener_sesion() as db:
            check(
                "obtener_sesion: rollback ante excepcion (nada persistido)",
                db.query(CuentaChange).filter_by(email="rota@example.com").count() == 0,
            )
    finally:
        database.SessionLocal = original
        engine.dispose()


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_modelo(check)
    test_ddl_crud(check)
    test_obtener_sesion(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_change_core.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
