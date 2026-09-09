from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from contextlib import contextmanager
from typing import Generator

from loguru import logger
from core.config import settings, resolver_ruta

# DATABASE_URL acepta:
#   - SQLite local:   sqlite:///data/gestor_redes.db
#   - PostgreSQL:     postgresql://user:pass@host:5432/dbname
#                     (Supabase; añade "sslmode=require" opcional en la URL)
_database_url = settings.database_url
if _database_url.startswith("sqlite:///") and not _database_url.startswith("sqlite:////"):
    _relative = _database_url.replace("sqlite:///", "", 1)
    _database_url = f"sqlite:///{resolver_ruta(_relative)}"

_ES_SQLITE = "sqlite" in _database_url

if _ES_SQLITE:
    _connect_args = {"check_same_thread": False}
else:
    _connect_args = {}
    if "sslmode" not in _database_url:
        _connect_args["sslmode"] = "require"  # Supabase exige SSL

engine = create_engine(
    _database_url,
    echo=False,
    connect_args=_connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if not _ES_SQLITE:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_session() -> Generator:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    # Importa los modelos para que queden registrados en Base.metadata antes
    # de create_all, sin importar el orden en que se llame desde fuera.
    import core.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrar_columnas()


def _migrar_columnas():
    """Migraciones ligeras: agrega columnas nuevas a tablas existentes.

    Solo aplica a SQLite (PRAGMA table_info + ALTER TABLE). En PostgreSQL las
    columnas se crean directamente con Base.metadata.create_all()."""
    if not _ES_SQLITE:
        return
    from sqlalchemy import text

    nuevas_columnas = {
        "proxy": "VARCHAR(300) DEFAULT ''",
        "pais": "VARCHAR(50) DEFAULT ''",
        "sector": "VARCHAR(30) DEFAULT ''",
        "avatar_path": "VARCHAR(200) DEFAULT ''",
        "totp_secret": "VARCHAR(100) DEFAULT ''",
        "email_password": "VARCHAR(200) DEFAULT ''",
        "auth_token": "VARCHAR(200) DEFAULT ''",
        "cookies_json": "TEXT",
        "status": "VARCHAR(20) DEFAULT 'imported'",
        "last_checked": "DATETIME",
    }

    try:
        with engine.connect() as conn:
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(cuentas)"))]
            for nombre, tipo in nuevas_columnas.items():
                if cols and nombre not in cols:
                    conn.execute(text(f"ALTER TABLE cuentas ADD COLUMN {nombre} {tipo}"))
                    logger.info(f"Migracion: columna 'cuentas.{nombre}' agregada")
            conn.commit()
    except Exception as e:
        logger.warning(f"Migracion de columnas no aplicada: {e}")
