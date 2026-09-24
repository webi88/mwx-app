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
    # timeout: espera ante bloqueos de SQLite (dashboard y scheduler arrancan
    # a la vez y pueden pisarse en create_all). Evita "database is locked".
    _connect_args = {"check_same_thread": False, "timeout": 30}
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

    if not _ES_SQLITE:
        # Validación temprana: si la URL es PostgreSQL y falta el driver,
        # damos un error claro en vez de un ModuleNotFoundError opaco.
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            logger.error(
                "DATABASE_URL es PostgreSQL pero falta el driver. "
                "Instala: pip install psycopg2-binary"
            )
            raise

    Base.metadata.create_all(bind=engine)
    _migrar_columnas()
    _resincronizar_secuencias()


def _resincronizar_secuencias():
    """Pone al día las secuencias SERIAL/IDENTITY en PostgreSQL.

    Al migrar filas desde SQLite con `id` explícito, la secuencia de PostgreSQL
    se queda por detrás del `MAX(id)` y los siguientes INSERT fallan con
    'duplicate key value violates unique constraint'. Ajusta cada secuencia a
    max(id)+1 para que vuelva a autoincrementar correctamente."""
    if _ES_SQLITE:
        return
    from sqlalchemy import text

    try:
        with engine.begin() as conn:
            for tabla in Base.metadata.tables.values():
                if "id" not in tabla.columns:
                    continue
                try:
                    conn.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{tabla.name}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {tabla.name}), 0) + 1, false)"
                        )
                    )
                except Exception as e:
                    logger.warning(
                        f"No se pudo resincronizar la secuencia de {tabla.name}: {e}"
                    )
        logger.info("Secuencias de PostgreSQL resincronizadas")
    except Exception as e:
        logger.warning(f"No se pudieron resincronizar las secuencias: {e}")


# Columnas que se agregan a tablas 'cuentas' ya existentes.
# Formato: nombre -> tipo SQL (compatible con SQLite; en PostgreSQL se omiten
# los tipos DATETIME, que no existen en ese motor). Los booleanos usan
# "BOOLEAN DEFAULT FALSE": valido en SQLite >= 3.23 (literales TRUE/FALSE) y
# en PostgreSQL; "BOOLEAN DEFAULT 0" NO es valido en PostgreSQL.
NUEVAS_COLUMNAS_CUENTAS = {
    "proxy": "VARCHAR(300) DEFAULT ''",
    "pais": "VARCHAR(50) DEFAULT ''",
    "sector": "VARCHAR(30) DEFAULT ''",
    "avatar_path": "VARCHAR(200) DEFAULT ''",
    "banner_path": "VARCHAR(200) DEFAULT ''",
    "seccion": "VARCHAR(20) DEFAULT ''",
    "nombre_mostrado": "VARCHAR(120) DEFAULT ''",
    "handle_actual": "VARCHAR(100) DEFAULT ''",
    "tipo_cuenta": "VARCHAR(20) DEFAULT ''",
    "nombre_propuesto": "VARCHAR(120) DEFAULT ''",
    "handle_propuesto": "VARCHAR(100) DEFAULT ''",
    "personalidad": "TEXT",
    "rol_activacion": "VARCHAR(20) DEFAULT ''",
    "perfil_personalidad": "VARCHAR(20) DEFAULT ''",
    "tier_calidad": "VARCHAR(20) DEFAULT ''",
    "pausada_activacion": "BOOLEAN DEFAULT FALSE",
    "totp_secret": "VARCHAR(100) DEFAULT ''",
    "email_password": "VARCHAR(200) DEFAULT ''",
    "auth_token": "VARCHAR(200) DEFAULT ''",
    "user_agent": "VARCHAR(300) DEFAULT ''",
    "cookies_json": "TEXT",
    "status": "VARCHAR(20) DEFAULT 'imported'",
    "last_checked": "DATETIME",
}


def _backfill_seccion(conn) -> None:
    """Preclasifica en CI/IP/LIB/JUS las cuentas ya existentes segun su 'sector'.

    Se ejecuta una sola vez, justo despues de crear la columna 'seccion':
    valores con 'izquierd' o 'ciudadan' -> CI, 'privad' -> IP, 'libertad' ->
    LIB, 'justicia' -> JUS ('' el resto). 'derech' ya NO produce 'CD': esa
    seccion desaparecio. Solo toca filas con seccion vacia/NULL, asi que es
    idempotente."""
    from sqlalchemy import text

    conn.execute(
        text(
            "UPDATE cuentas SET seccion = CASE "
            "WHEN lower(sector) LIKE '%izquierd%' OR lower(sector) LIKE '%ciudadan%' THEN 'CI' "
            "WHEN lower(sector) LIKE '%privad%' THEN 'IP' "
            "WHEN lower(sector) LIKE '%libertad%' THEN 'LIB' "
            "WHEN lower(sector) LIKE '%justicia%' THEN 'JUS' "
            "ELSE '' END "
            "WHERE (seccion IS NULL OR seccion = '')"
        )
    )


def _limpiar_grupo_por_defecto(conn) -> None:
    """Limpia el antiguo default grupo='A': lo pasa a '' (sin grupo).

    Solo toca filas con grupo exactamente igual a 'A'; respeta NULL, '' y
    cualquier otro grupo real (B, C, ...). Es idempotente: una segunda
    ejecucion no cambia nada."""
    from sqlalchemy import text

    conn.execute(text("UPDATE cuentas SET grupo = '' WHERE grupo = 'A'"))


def _limpiar_secciones_invalidas(conn) -> None:
    """Corrige en 'cuentas.seccion' los valores que ya no son validos.

    Recorre los valores DISTINTOS no vacios guardados en la columna y aplica
    `normalizar_seccion()` de core.secciones: "CD"/"centroderecha" quedan en ''
    (sin asignar), "ci" pasa a "CI", etc. El UPDATE es parametrizado (text() +
    bind), por lo que funciona igual en SQLite y PostgreSQL, y es idempotente:
    una segunda pasada no encuentra nada que corregir. No lanza: si algo
    falla, solo advierte. Loguea el numero de filas corregidas."""
    from sqlalchemy import text

    try:
        # Import local para evitar cualquier ciclo con core.database.
        from core.secciones import normalizar_seccion
    except Exception as e:
        logger.warning(f"No se pudo importar normalizar_seccion: {e}")
        return

    try:
        filas = conn.execute(
            text(
                "SELECT DISTINCT seccion FROM cuentas "
                "WHERE seccion IS NOT NULL AND seccion <> ''"
            )
        ).fetchall()
    except Exception as e:
        logger.warning(f"No se pudieron leer las secciones de cuentas: {e}")
        return

    corregidas = 0
    for (valor,) in filas:
        try:
            normalizado = normalizar_seccion(valor)
        except Exception:
            continue
        if normalizado == valor:
            continue
        try:
            resultado = conn.execute(
                text("UPDATE cuentas SET seccion = :nuevo WHERE seccion = :viejo"),
                {"nuevo": normalizado, "viejo": valor},
            )
            corregidas += int(resultado.rowcount or 0)
        except Exception as e:
            logger.warning(f"No se pudo corregir la seccion {valor!r}: {e}")
    if corregidas:
        logger.info(
            f"Migracion: {corregidas} cuenta(s) con seccion invalida normalizada"
        )


def _migrar_columnas():
    """Migraciones ligeras: agrega columnas nuevas a tablas existentes.

    - SQLite: PRAGMA table_info + ALTER TABLE ... ADD COLUMN. Se registra en el
      set 'agregadas' que columnas se crearon de verdad; si 'seccion' es nueva
      se hace backfill desde 'sector' para preclasificar cuentas ya creadas.
      Ademas convierte grupo='A' historico a '' (sin grupo) sin tocar otros
      grupos reales (ver _limpiar_grupo_por_defecto).
    - PostgreSQL/Supabase: ALTER TABLE ... ADD COLUMN IF NOT EXISTS (idempotente
      y tolerante a fallos: solo advierte si algo no se puede aplicar). No se
      migran tipos DATETIME ('last_checked' ya existe en produccion). Tambien
      limpia grupo='A' -> '' y normaliza las secciones invalidas (p.ej. 'CD'
      -> '') de la misma forma idempotente."""
    from sqlalchemy import text

    nuevas_columnas = NUEVAS_COLUMNAS_CUENTAS

    if not _ES_SQLITE:
        # PostgreSQL (Supabase): ADD COLUMN IF NOT EXISTS es idempotente.
        try:
            with engine.begin() as conn:
                existentes = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'cuentas'"
                        )
                    )
                }
                for nombre, tipo in nuevas_columnas.items():
                    if "DATETIME" in tipo.upper():
                        # En PostgreSQL DATETIME no existe; 'last_checked' ya
                        # esta en produccion y no se toca.
                        continue
                    conn.execute(
                        text(
                            f"ALTER TABLE cuentas ADD COLUMN IF NOT EXISTS "
                            f"{nombre} {tipo}"
                        )
                    )
                if existentes and "seccion" not in existentes:
                    _backfill_seccion(conn)
                    logger.info(
                        "Migracion (Postgres): backfill de 'cuentas.seccion' desde 'sector'"
                    )
                try:
                    _limpiar_grupo_por_defecto(conn)
                    logger.info(
                        "Migracion (Postgres): grupo='A' historico convertido a '' (sin grupo)"
                    )
                except Exception as e_grupo:
                    logger.warning(f"Migracion de grupo (Postgres) no aplicada: {e_grupo}")
                try:
                    _limpiar_secciones_invalidas(conn)
                except Exception as e_seccion:
                    logger.warning(
                        f"Migracion de secciones (Postgres) no aplicada: {e_seccion}"
                    )
        except Exception as e:
            logger.warning(f"Migracion de columnas (Postgres) no aplicada: {e}")
        return

    try:
        agregadas = set()
        with engine.connect() as conn:
            cols = [row[1] for row in conn.execute(text("PRAGMA table_info(cuentas)"))]
            for nombre, tipo in nuevas_columnas.items():
                if cols and nombre not in cols:
                    conn.execute(text(f"ALTER TABLE cuentas ADD COLUMN {nombre} {tipo}"))
                    agregadas.add(nombre)
                    logger.info(f"Migracion: columna 'cuentas.{nombre}' agregada")
            if "seccion" in agregadas:
                _backfill_seccion(conn)
                logger.info("Migracion: backfill de 'cuentas.seccion' desde 'sector'")
            if cols:
                try:
                    _limpiar_grupo_por_defecto(conn)
                    logger.info(
                        "Migracion: grupo='A' historico convertido a '' (sin grupo)"
                    )
                except Exception as e_grupo:
                    logger.warning(f"Migracion de grupo no aplicada: {e_grupo}")
            if cols:
                try:
                    _limpiar_secciones_invalidas(conn)
                except Exception as e_seccion:
                    logger.warning(f"Migracion de secciones no aplicada: {e_seccion}")
            conn.commit()
    except Exception as e:
        logger.warning(f"Migracion de columnas no aplicada: {e}")
