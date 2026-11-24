"""Tests del ajuste "navegadores simultaneos" en la config de campana MWX.

Cubre el modelo (`AppConfig.navegadores`, Integer, default 1) y la migracion
idempotente `core.database._migrar_columnas_app_config()` que agrega la columna
a la tabla `app_config` que YA existe en produccion (create_all no lo hace).

Reglas:
  - NUNCA toca `data/gestor_redes.db`: todo corre en SQLite temporales.
  - El `engine` global de `core.database` solo se parchea en memoria y SIEMPRE
    se restaura (try/finally) para no afectar al resto de la suite.
  - No usa red, Chrome ni Supabase.

Cubre:
  (1) `AppConfig.navegadores` existe, es Integer, default 1 y esta entre
      `limite_dia` e `imagenes`.
  (2) El ORM guarda 1 por defecto en una fila nueva (BD temporal).
  (3) Helper SQLite: tabla vieja SIN la columna -> la agrega, las filas
      existentes quedan en 1 y una segunda ejecucion es idempotente.
  (4) Helper SQLite con tabla inexistente -> False, sin excepcion.
  (5) `_migrar_columnas_app_config()` con el engine apuntado a un temporal ->
      agrega la columna y hace commit; dos llamadas seguidas no fallan.
  (6) La migracion nunca lanza: engine roto -> solo warning (se captura).
  (7) AST de `init_db`: llama a `_migrar_columnas_app_config` despues de
      `_migrar_columnas`; la funcion contiene el ALTER de SQLite y el
      `ADD COLUMN IF NOT EXISTS` de PostgreSQL.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_app_config_navegadores.py
"""
from __future__ import annotations

import ast
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import sessionmaker

# La raiz del repo a sys.path (mismo patron que el resto de tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import core.database as db_mod  # noqa: E402
from core.models import AppConfig, Base  # noqa: E402

SQL_VIEJO = """
CREATE TABLE app_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL,
    hashtags TEXT DEFAULT '[]',
    limite_dia INTEGER DEFAULT 49
)
"""


def _sqlite_temporal(tmp: Path, nombre: str = "prueba.db"):
    """Engine SQLite temporal (archivo dentro del temp dir del test)."""
    return create_engine(
        f"sqlite:///{tmp / nombre}", connect_args={"check_same_thread": False}
    )


def _columnas(engine) -> list:
    with engine.connect() as conn:
        return [fila[1] for fila in conn.execute(text("PRAGMA table_info(app_config)"))]


def _crear_tabla_vieja(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(SQL_VIEJO))
        conn.execute(
            text("INSERT INTO app_config (usuario_id, limite_dia) VALUES (7, 49)")
        )


def _ast_init_db():
    """Devuelve (nombres de llamadas en orden, arbol AST de init_db)."""
    arbol = ast.parse(inspect.getsource(db_mod.init_db))
    nodos = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
    ]
    nodos.sort(key=lambda n: (n.lineno, n.col_offset))  # orden real del codigo
    return [nodo.func.id for nodo in nodos], arbol


def _check_modelo(check) -> None:
    columna = AppConfig.__table__.c.get("navegadores")
    check("modelo: AppConfig.navegadores existe", columna is not None)
    if columna is None:
        return
    check(
        "modelo: navegadores es Integer",
        isinstance(columna.type, Integer),
        f"tipo={columna.type!r}",
    )
    check(
        "modelo: navegadores default 1",
        columna.default is not None and columna.default.arg == 1,
        f"default={getattr(columna.default, 'arg', None)!r}",
    )
    nombres = [c.name for c in AppConfig.__table__.columns]
    check(
        "modelo: navegadores va despues de limite_dia y antes de imagenes",
        nombres.index("limite_dia") < nombres.index("navegadores") < nombres.index("imagenes"),
        f"orden={nombres}",
    )
    check(
        "modelo: navegadores es un atributo mapeado del ORM",
        hasattr(AppConfig, "navegadores"),
    )


def _check_orm_default(tmp: Path, check) -> None:
    engine = _sqlite_temporal(tmp, "orm.db")
    try:
        Base.metadata.tables["app_config"].create(engine)
        Sesion = sessionmaker(bind=engine, expire_on_commit=False)
        db = Sesion()
        try:
            fila = AppConfig(usuario_id=1)
            db.add(fila)
            db.commit()
            db.refresh(fila)
            check(
                "ORM: fila nueva guarda navegadores=1 por defecto",
                fila.navegadores == 1,
                f"valor={fila.navegadores!r}",
            )
            fila.navegadores = 3
            db.commit()
            check(
                "ORM: navegadores acepta 1-3",
                db.get(AppConfig, fila.id).navegadores == 3,
            )
        finally:
            db.close()
    finally:
        engine.dispose()


def _check_helper_sqlite(tmp: Path, check) -> None:
    engine = _sqlite_temporal(tmp, "helper.db")
    try:
        _crear_tabla_vieja(engine)
        check(
            "helper: tabla vieja NO trae navegadores",
            "navegadores" not in _columnas(engine),
        )
        with engine.begin() as conn:
            agregada = db_mod._migrar_app_config_sqlite(conn)
        check("helper: primera pasada agrega la columna", agregada is True)
        check(
            "helper: PRAGMA ya muestra navegadores",
            "navegadores" in _columnas(engine),
        )
        with engine.connect() as conn:
            valor = conn.execute(
                text("SELECT navegadores FROM app_config WHERE usuario_id = 7")
            ).scalar()
        check("helper: la fila existente queda en 1", valor == 1, f"valor={valor!r}")
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO app_config (usuario_id) VALUES (8)"))
            conn.commit()
            valor_nuevo = conn.execute(
                text("SELECT navegadores FROM app_config WHERE usuario_id = 8")
            ).scalar()
        check(
            "helper: una fila nueva sin la columna toma el default 1",
            valor_nuevo == 1,
            f"valor={valor_nuevo!r}",
        )
        with engine.begin() as conn:
            segunda = db_mod._migrar_app_config_sqlite(conn)
        check("helper: segunda pasada es idempotente", segunda is False)
        check(
            "helper: no duplica la columna",
            _columnas(engine).count("navegadores") == 1,
        )
    finally:
        engine.dispose()

    # Tabla inexistente: PRAGMA vacio -> no intenta ALTER y no lanza.
    vacio = _sqlite_temporal(tmp, "sin_tabla.db")
    try:
        with vacio.begin() as conn:
            resultado = db_mod._migrar_app_config_sqlite(conn)
        check("helper: sin tabla app_config devuelve False", resultado is False)
    finally:
        vacio.dispose()


def _check_migracion_engine(tmp: Path, check) -> None:
    engine = _sqlite_temporal(tmp, "engine.db")
    original = db_mod.engine
    original_sqlite = db_mod._ES_SQLITE
    try:
        _crear_tabla_vieja(engine)
        db_mod.engine = engine
        db_mod._ES_SQLITE = True  # fuerza la rama SQLite aunque la suite corra en Postgres
        db_mod._migrar_columnas_app_config()  # debe hacer commit propio
        check(
            "migracion: usa el engine global (SQLite) y agrega la columna",
            "navegadores" in _columnas(engine),
        )
        with engine.connect() as conn:
            valor = conn.execute(
                text("SELECT navegadores FROM app_config WHERE usuario_id = 7")
            ).scalar()
        check("migracion: commit aplicado a la fila existente", valor == 1)
        db_mod._migrar_columnas_app_config()  # segunda vez: no lanza
        check(
            "migracion: llamarla dos veces no rompe",
            _columnas(engine).count("navegadores") == 1,
        )
    finally:
        db_mod.engine = original
        db_mod._ES_SQLITE = original_sqlite
        engine.dispose()
    check(
        "migracion: el engine real de core.database queda restaurado",
        db_mod.engine is original and db_mod._ES_SQLITE is original_sqlite,
    )


def _check_nunca_lanza(check) -> None:
    class EngineRoto:
        def connect(self):
            raise RuntimeError("bd caida (simulada)")

        def begin(self):
            raise RuntimeError("bd caida (simulada)")

    original = db_mod.engine
    try:
        db_mod.engine = EngineRoto()
        try:
            db_mod._migrar_columnas_app_config()
            lanzo = False
        except Exception:
            lanzo = True
        check("defensivo: engine roto NO lanza excepcion", lanzo is False)
    finally:
        db_mod.engine = original


def _check_ast(check) -> None:
    llamadas, arbol = _ast_init_db()
    check(
        "AST: init_db llama a _migrar_columnas_app_config",
        "_migrar_columnas_app_config" in llamadas,
        f"llamadas={llamadas}",
    )
    check(
        "AST: corre despues de _migrar_columnas y antes de _resincronizar_secuencias",
        llamadas.index("_migrar_columnas")
        < llamadas.index("_migrar_columnas_app_config")
        < llamadas.index("_resincronizar_secuencias"),
        f"llamadas={llamadas}",
    )
    check(
        "AST: init_db mantiene create_all",
        any(
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "create_all"
            for nodo in ast.walk(arbol)
        ),
    )
    codigo = inspect.getsource(db_mod._migrar_columnas_app_config)
    check(
        "AST: la migracion usa el helper aislado de SQLite",
        "_migrar_app_config_sqlite(" in codigo,
        "",
    )
    check(
        "AST: migracion SQLite usa PRAGMA table_info(app_config)",
        "PRAGMA table_info(app_config)" in inspect.getsource(
            db_mod._migrar_app_config_sqlite
        ),
    )
    check(
        "AST: ALTER de SQLite con navegadores INTEGER DEFAULT 1",
        db_mod.APP_CONFIG_NAVEGADORES_SQL == "navegadores INTEGER DEFAULT 1"
        and "ALTER TABLE app_config ADD COLUMN" in inspect.getsource(
            db_mod._migrar_app_config_sqlite
        ),
    )
    check(
        "AST: PostgreSQL usa ADD COLUMN IF NOT EXISTS",
        "ALTER TABLE app_config ADD COLUMN IF NOT EXISTS" in codigo
        and "APP_CONFIG_NAVEGADORES_SQL" in codigo,
    )
    check(
        "AST: la migracion esta protegida con try/except y warning",
        "except Exception" in codigo and "logger.warning" in codigo,
    )


def run(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="app_config_navegadores_"))
    try:
        _check_modelo(check)
        _check_orm_default(tmp, check)
        _check_helper_sqlite(tmp, check)
        _check_migracion_engine(tmp, check)
        _check_nunca_lanza(check)
        _check_ast(check)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    from run_tests import check, resumen

    run(check)
    sys.exit(resumen())
