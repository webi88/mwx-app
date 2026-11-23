# -*- coding: utf-8 -*-
"""Tests del nucleo compartido con el ejecutable MWX APP (Windows/PyInstaller).

Sin dependencias nuevas, sin red y SIN tocar `data/` real: todo se ejecuta en
`tempfile.mkdtemp()` y con copias aisladas de `core/config.py` para simular
`sys.frozen` / `sys.executable` sin recargar el modulo global del proyecto.

Cubre:
    (1) Modo NO congelado: PROJECT_ROOT/DATA_DIR, `resolver_ruta` y `.env`
        conservan el comportamiento historico.
    (2) Modo CONGELADO (PyInstaller): APP_DIR/RESOURCE_DIR/DATA_DIR, `MWX_DATA_DIR`,
        resolucion de `data/`, `config/`, `logs/` contra DATA_DIR y recursos
        contra `_MEIPASS`/carpeta del .exe; `.env` de DATA_DIR y APP_DIR.
    (3) `core.database`: SQLite relativa apunta a DATA_DIR en congelado y a la
        ruta de siempre sin congelar; PostgreSQL intacto; URL vacia -> default
        bajo DATA_DIR.
    (4) Import perezoso de `activaciones`: `import activaciones.local` NO carga
        `activaciones.motor` (PEP 562), pero `from activaciones import
        MotorActivacion` sigue funcionando.
    (5) PBKDF2 (`hash_password` / `verify_password`).
    (6) Tokens HMAC (`crear_token_app` / `verificar_token_app`): expiracion,
        firma alterada, secreto incorrecto y `token_version`.
    (7) Tablas nuevas `App*` + `autenticar_app` / `revocar_sesiones` en una
        SQLite temporal.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_core.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

_ENV_KEYS = (
    "MWX_DATA_DIR",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "MWX_API_URL",
    "DATABASE_URL",
    "SECRET_KEY",
    "LOCALAPPDATA",
    "APPDATA",
)

_CONTADOR = [0]


# ---------------------------------------------------------------------------
# Utilidades de aislamiento
# ---------------------------------------------------------------------------
@contextmanager
def _entorno_aislado(env=None, frozen=None, exe=None, meipass=None):
    """Fija/limpia variables y atributos de `sys` y los restaura SIEMPRE."""
    estado_env = {clave: os.environ.get(clave) for clave in _ENV_KEYS}
    tenia_frozen = hasattr(sys, "frozen")
    frozen_original = getattr(sys, "frozen", None)
    exe_original = sys.executable
    tenia_meipass = hasattr(sys, "_MEIPASS")
    meipass_original = getattr(sys, "_MEIPASS", None)
    try:
        for clave in _ENV_KEYS:
            os.environ.pop(clave, None)
        for clave, valor in (env or {}).items():
            os.environ[str(clave)] = str(valor)
        if frozen is None:
            if hasattr(sys, "frozen"):
                del sys.frozen
        else:
            sys.frozen = bool(frozen)
        if exe is not None:
            sys.executable = str(exe)
        if meipass is not None:
            sys._MEIPASS = str(meipass)
        elif tenia_meipass:
            del sys._MEIPASS
        yield
    finally:
        for clave, valor in estado_env.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        if tenia_frozen:
            sys.frozen = frozen_original
        elif hasattr(sys, "frozen"):
            del sys.frozen
        sys.executable = exe_original
        if tenia_meipass:
            sys._MEIPASS = meipass_original
        elif hasattr(sys, "_MEIPASS"):
            del sys._MEIPASS


def _cargar_modulo(ruta: Path, etiqueta: str):
    """Importa un archivo `.py` como modulo aislado y unico."""
    _CONTADOR[0] += 1
    nombre = f"mwx_test_{etiqueta}_{_CONTADOR[0]}"
    especificacion = importlib.util.spec_from_file_location(nombre, ruta)
    if especificacion is None or especificacion.loader is None:
        raise ImportError(f"No se pudo cargar {ruta}")
    modulo = importlib.util.module_from_spec(especificacion)
    sys.modules[nombre] = modulo
    try:
        especificacion.loader.exec_module(modulo)
    except Exception:
        sys.modules.pop(nombre, None)
        raise
    return modulo


def _copiar_config(destino: Path) -> Path:
    """Copia `core/config.py` a un proyecto temporal (para no tocar data/)."""
    ruta = destino / "core" / "config.py"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RAIZ / "core" / "config.py", ruta)
    return ruta


# ---------------------------------------------------------------------------
# (1) Modo NO congelado: comportamiento historico
# ---------------------------------------------------------------------------
def _probar_no_congelado(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_core_no_frozen_"))
    try:
        config_path = _copiar_config(tmp)
        (tmp / ".env").write_text(
            "OPENAI_API_KEY=clave-desde-env-no-frozen\n", encoding="utf-8"
        )
        with _entorno_aislado(env={"LOCALAPPDATA": str(tmp / "localappdata")}):
            cfg = _cargar_modulo(config_path, "cfg_no_frozen")
            raiz = tmp.resolve()
            check(
                "no-congelado: PROJECT_ROOT es la raiz del proyecto",
                cfg.PROJECT_ROOT == raiz,
                str(cfg.PROJECT_ROOT),
            )
            check(
                "no-congelado: DATA_DIR = PROJECT_ROOT",
                cfg.DATA_DIR == raiz,
                str(cfg.DATA_DIR),
            )
            check(
                "no-congelado: resolver_ruta('data/...') historica",
                cfg.resolver_ruta("data/gestor_redes.db")
                == str((raiz / "data" / "gestor_redes.db").resolve()),
                cfg.resolver_ruta("data/gestor_redes.db"),
            )
            check(
                "no-congelado: resolver_ruta('core/...') contra PROJECT_ROOT",
                cfg.resolver_ruta("core/config.py")
                == str((raiz / "core" / "config.py").resolve()),
                cfg.resolver_ruta("core/config.py"),
            )
            check(
                "no-congelado: settings.base_dir = raiz",
                cfg.settings.base_dir == raiz,
            )
            check(
                "no-congelado: cookies_dir bajo data/",
                cfg.settings.cookies_dir == raiz / "data" / "cookies",
            )
            check(
                "no-congelado: profiles/reports/temp bajo data/",
                cfg.settings.profiles_dir == raiz / "data" / "perfiles_chrome"
                and cfg.settings.reports_dir == raiz / "data" / "reportes"
                and cfg.settings.temp_dir == raiz / "data" / "temp",
            )
            check(
                "no-congelado: data_dir/app_dir son str de la raiz",
                cfg.settings.data_dir == str(raiz)
                and cfg.settings.app_dir == str(raiz),
            )
            check(
                "no-congelado: .env desde PROJECT_ROOT",
                cfg.settings.openai_api_key == "clave-desde-env-no-frozen",
                cfg.settings.openai_api_key,
            )
            check(
                "no-congelado: env_file unico (PROJECT_ROOT/.env)",
                Path(cfg.Settings.model_config["env_file"]) == raiz / ".env",
            )
            check(
                "no-congelado: OPENAI_API_BASE/MWX_API_URL vacios",
                cfg.settings.openai_api_base == "" and cfg.settings.mwx_api_url == "",
            )
            check(
                "no-congelado: crea las carpetas de datos",
                (raiz / "data" / "cookies").is_dir()
                and (raiz / "data" / "logs").is_dir(),
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# (2) Modo CONGELADO (PyInstaller)
# ---------------------------------------------------------------------------
def _preparar_frozen(tmp: Path):
    exe_dir = tmp / "Instalado"
    exe_dir.mkdir()
    exe = exe_dir / "MWX APP.exe"
    exe.write_bytes(b"MZ")
    recursos = tmp / "recursos"
    (recursos / "core").mkdir(parents=True)
    (recursos / "core" / "config.py").write_text("# recurso\n", encoding="utf-8")
    (recursos / "assets").mkdir()
    (recursos / "assets" / "logo.png").write_bytes(b"png")
    (exe_dir / "assets").mkdir()
    (exe_dir / "assets" / "solo_app.txt").write_text("app", encoding="utf-8")
    return exe, exe_dir, recursos


def _probar_congelado(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_core_frozen_"))
    try:
        exe, exe_dir, recursos = _preparar_frozen(tmp)
        data_root = tmp / "datos_app"
        with _entorno_aislado(
            env={
                "OPENAI_API_KEY": "clave-frozen",
                "MWX_DATA_DIR": str(data_root),
                "OPENAI_BASE_URL": "https://api.ejemplo/v1",
                "MWX_API_URL": "http://127.0.0.1:8765",
            },
            frozen=True,
            exe=exe,
            meipass=recursos,
        ):
            cfg = _cargar_modulo(RAIZ / "core" / "config.py", "cfg_frozen")
            data_res = data_root.resolve()
            check(
                "congelado: APP_DIR = carpeta del ejecutable",
                cfg.APP_DIR == exe_dir.resolve(),
                str(cfg.APP_DIR),
            )
            check(
                "congelado: RESOURCE_DIR = _MEIPASS",
                cfg.RESOURCE_DIR == recursos.resolve(),
                str(cfg.RESOURCE_DIR),
            )
            check(
                "congelado: DATA_DIR = MWX_DATA_DIR",
                cfg.DATA_DIR == data_res,
                str(cfg.DATA_DIR),
            )
            check(
                "congelado: crea DATA_DIR, data/, config/ y logs/",
                all(
                    (data_res / sub).is_dir()
                    for sub in ("", "data", "config", "logs")
                ),
            )
            check(
                "congelado: resolver_ruta('data/...') -> DATA_DIR",
                cfg.resolver_ruta("data/gestor_redes.db")
                == str((data_res / "data" / "gestor_redes.db").resolve()),
                cfg.resolver_ruta("data/gestor_redes.db"),
            )
            check(
                "congelado: resolver_ruta('config/...') -> DATA_DIR",
                cfg.resolver_ruta("config/monitor_actividad_config.json")
                == str((data_res / "config" / "monitor_actividad_config.json").resolve()),
            )
            check(
                "congelado: resolver_ruta('logs/...') -> DATA_DIR",
                cfg.resolver_ruta("logs/app.log")
                == str((data_res / "logs" / "app.log").resolve()),
            )
            check(
                "congelado: recursos existentes -> RESOURCE_DIR",
                cfg.resolver_ruta("core/config.py")
                == str((recursos / "core" / "config.py").resolve()),
            )
            check(
                "congelado: recurso solo en APP_DIR -> carpeta del .exe",
                cfg.resolver_ruta("assets/solo_app.txt")
                == str((exe_dir / "assets" / "solo_app.txt").resolve()),
            )
            check(
                "congelado: ruta absoluta intacta",
                cfg.resolver_ruta(str(tmp / "otro.txt"))
                == str((tmp / "otro.txt").resolve()),
            )
            check(
                "congelado: base_dir = DATA_DIR",
                cfg.settings.base_dir == data_res,
            )
            check(
                "congelado: cookies/profiles/reports/temp bajo DATA_DIR/data",
                cfg.settings.cookies_dir == data_res / "data" / "cookies"
                and cfg.settings.profiles_dir == data_res / "data" / "perfiles_chrome"
                and cfg.settings.reports_dir == data_res / "data" / "reportes"
                and cfg.settings.temp_dir == data_res / "data" / "temp",
            )
            check(
                "congelado: settings.data_dir/app_dir expuestos como str",
                cfg.settings.data_dir == str(data_res)
                and cfg.settings.app_dir == str(exe_dir.resolve()),
            )
            check(
                "congelado: .env = (DATA_DIR, APP_DIR)",
                tuple(cfg.Settings.model_config["env_file"])
                == (data_res / ".env", exe_dir.resolve() / ".env"),
            )
            check(
                "congelado: OPENAI_API_KEY/OPENAI_BASE_URL/MWX_API_URL",
                cfg.settings.openai_api_key == "clave-frozen"
                and cfg.settings.openai_api_base == "https://api.ejemplo/v1"
                and cfg.settings.mwx_api_url == "http://127.0.0.1:8765",
            )
            check(
                "congelado: log_file se resuelve bajo DATA_DIR",
                cfg.resolver_ruta(cfg.settings.log_file).startswith(str(data_res)),
            )
            check(
                "congelado: PROJECT_ROOT sigue siendo una raiz valida",
                cfg.PROJECT_ROOT == exe_dir.resolve(),
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _probar_env_congelado(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_core_env_"))
    try:
        exe, exe_dir, recursos = _preparar_frozen(tmp)
        data_root = tmp / "datos_app"
        data_root.mkdir()
        data_env = data_root / ".env"
        app_env = exe_dir / ".env"

        # (a) Solo DATA_DIR/.env
        data_env.write_text("OPENAI_API_KEY=desde-data-dir\n", encoding="utf-8")
        with _entorno_aislado(
            env={"MWX_DATA_DIR": str(data_root)},
            frozen=True,
            exe=exe,
            meipass=recursos,
        ):
            cfg = _cargar_modulo(RAIZ / "core" / "config.py", "cfg_env_data")
            check(
                "congelado: .env de DATA_DIR se carga",
                cfg.settings.openai_api_key == "desde-data-dir",
                cfg.settings.openai_api_key,
            )

        # (b) Solo APP_DIR/.env (carpeta del ejecutable)
        data_env.unlink()
        app_env.write_text("OPENAI_API_KEY=desde-app-dir\n", encoding="utf-8")
        with _entorno_aislado(
            env={"MWX_DATA_DIR": str(data_root)},
            frozen=True,
            exe=exe,
            meipass=recursos,
        ):
            cfg2 = _cargar_modulo(RAIZ / "core" / "config.py", "cfg_env_app")
            check(
                "congelado: .env junto al .exe se carga",
                cfg2.settings.openai_api_key == "desde-app-dir",
                cfg2.settings.openai_api_key,
            )

        # (c) Ambos: el orden de model_config es DATA_DIR y luego APP_DIR.
        data_env.write_text("OPENAI_API_KEY=desde-data-dir\n", encoding="utf-8")
        with _entorno_aislado(
            env={"MWX_DATA_DIR": str(data_root)},
            frozen=True,
            exe=exe,
            meipass=recursos,
        ):
            cfg3 = _cargar_modulo(RAIZ / "core" / "config.py", "cfg_env_ambos")
            archivos = tuple(cfg3.Settings.model_config["env_file"])
            check(
                "congelado: .env con orden DATA_DIR -> APP_DIR",
                archivos[0].name == ".env"
                and str(archivos[0]).startswith(str(data_root.resolve()))
                and str(archivos[1]).startswith(str(exe_dir.resolve())),
                str(archivos),
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# (3) Base de datos
# ---------------------------------------------------------------------------
def _probar_database(check) -> None:
    import core.config as cfg_real
    import core.database as db_real

    check(
        "DB no-congelada: URL resuelta identica a la historica",
        db_real.resolver_url_base_datos(cfg_real.settings.database_url)
        == db_real._database_url,
    )
    check(
        "DB no-congelada: SQLite relativa usa resolver_ruta",
        db_real.resolver_url_base_datos("sqlite:///data/gestor_redes.db")
        == f"sqlite:///{cfg_real.resolver_ruta('data/gestor_redes.db')}",
    )
    check(
        "DB no-congelada: PostgreSQL intacto",
        db_real.resolver_url_base_datos("postgresql://u:p@host:5432/db")
        == "postgresql://u:p@host:5432/db",
    )
    check(
        "DB no-congelada: DATABASE_URL vacia conserva cadena vacia",
        db_real.resolver_url_base_datos("") == "",
    )

    tmp = Path(tempfile.mkdtemp(prefix="mwx_core_db_"))
    try:
        exe, _exe_dir, recursos = _preparar_frozen(tmp)
        data_root = tmp / "datos_db"
        with _entorno_aislado(
            env={
                "OPENAI_API_KEY": "clave-db",
                "MWX_DATA_DIR": str(data_root),
            },
            frozen=True,
            exe=exe,
            meipass=recursos,
        ):
            cfg = _cargar_modulo(RAIZ / "core" / "config.py", "cfg_db_frozen")
            config_original = sys.modules.get("core.config")
            sys.modules["core.config"] = cfg
            modulo_db = None
            try:
                modulo_db = _cargar_modulo(
                    RAIZ / "core" / "database.py", "db_frozen"
                )
            finally:
                if config_original is not None:
                    sys.modules["core.config"] = config_original
                else:
                    sys.modules.pop("core.config", None)
            try:
                esperada = (data_root.resolve() / "data" / "gestor_redes.db")
                check(
                    "DB congelada: URL por defecto bajo DATA_DIR",
                    modulo_db._database_url == f"sqlite:///{esperada}",
                    modulo_db._database_url,
                )
                check(
                    "DB congelada: engine SQLite apunta a DATA_DIR",
                    Path(modulo_db.engine.url.database).resolve()
                    == esperada.resolve(),
                    str(modulo_db.engine.url.database),
                )
                check(
                    "DB congelada: URL vacia -> default bajo DATA_DIR",
                    modulo_db.resolver_url_base_datos("")
                    == f"sqlite:///{esperada}",
                )
                check(
                    "DB congelada: SQLite absoluta no se re-resuelve",
                    modulo_db.resolver_url_base_datos(
                        "sqlite:////ruta/absoluta/test.db"
                    )
                    == "sqlite:////ruta/absoluta/test.db",
                )
                check(
                    "DB congelada: PostgreSQL intacto",
                    modulo_db.resolver_url_base_datos(
                        "postgresql://u:p@host:5432/db"
                    )
                    == "postgresql://u:p@host:5432/db",
                )
                check(
                    "DB congelada: WAL configurado en SQLite",
                    modulo_db._ES_SQLITE is True,
                )
            finally:
                try:
                    modulo_db.engine.dispose()
                except Exception:
                    pass
                sys.modules.pop(modulo_db.__name__, None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# (4) Import perezoso de `activaciones`
# ---------------------------------------------------------------------------
def _probar_lazy_activaciones(check) -> None:
    claves = [
        clave
        for clave in list(sys.modules)
        if clave == "activaciones" or clave.startswith("activaciones.")
    ]
    respaldo = {clave: sys.modules.pop(clave) for clave in claves}
    try:
        import activaciones.local  # noqa: F401

        check(
            "lazy: import activaciones.local NO carga activaciones.motor",
            "activaciones.motor" not in sys.modules,
        )
        check(
            "lazy: import activaciones.local no carga variaciones",
            "activaciones.variaciones" not in sys.modules,
        )
        from activaciones import MotorActivacion

        check(
            "lazy: from activaciones import MotorActivacion funciona",
            callable(MotorActivacion),
        )
        check(
            "lazy: motor se carga solo al pedirlo",
            "activaciones.motor" in sys.modules,
        )
    finally:
        for clave in list(sys.modules):
            if clave == "activaciones" or clave.startswith("activaciones."):
                sys.modules.pop(clave, None)
        sys.modules.update(respaldo)


# ---------------------------------------------------------------------------
# (5) Contraseñas PBKDF2
# ---------------------------------------------------------------------------
def _probar_password(check) -> None:
    from core.app_auth import (
        BYTES_SALT,
        ITERACIONES_PASSWORD,
        hash_password,
        verify_password,
    )

    h1 = hash_password("Clave Segura 123")
    h2 = hash_password("Clave Segura 123")
    partes = h1.split("$")
    check(
        "password: formato pbkdf2_sha256$iter$salt$hash",
        len(partes) == 4 and partes[0] == "pbkdf2_sha256",
        h1[:24] + "...",
    )
    check(
        "password: 200_000 iteraciones",
        partes[1] == "200000" and ITERACIONES_PASSWORD == 200_000,
    )
    check(
        "password: salt aleatorio de 16 bytes",
        len(bytes.fromhex(partes[2])) == BYTES_SALT == 16,
    )
    check("password: salt distinto por hash", h1 != h2)
    check(
        "password: verifica la correcta",
        verify_password("Clave Segura 123", h1) is True,
    )
    check(
        "password: verifica el segundo hash",
        verify_password("Clave Segura 123", h2) is True,
    )
    check(
        "password: rechaza incorrecta",
        verify_password("clave equivocada", h1) is False,
    )
    check(
        "password: hash corrupto no lanza",
        verify_password("x", "basura") is False
        and verify_password("x", "") is False
        and verify_password("x", None) is False,
    )
    check(
        "password: algoritmo distinto no lanza",
        verify_password("x", "md5$1$00$00") is False,
    )


# ---------------------------------------------------------------------------
# (6) Tokens HMAC
# ---------------------------------------------------------------------------
class _UsuarioToken:
    id = 42
    username = "AdminMWX"
    token_version = 7


def _probar_tokens(check) -> None:
    from core.app_auth import crear_token_app, verificar_token_app

    datos = crear_token_app(_UsuarioToken(), horas=12, secreto="secreto-prueba")
    check(
        "token: devuelve token/expira",
        isinstance(datos, dict)
        and isinstance(datos.get("token"), str)
        and isinstance(datos.get("expira"), str),
    )
    payload = verificar_token_app(datos["token"], secreto="secreto-prueba")
    check(
        "token: payload uid/username/tv",
        isinstance(payload, dict)
        and payload.get("uid") == 42
        and payload.get("username") == "AdminMWX"
        and payload.get("tv") == 7,
        str(payload),
    )
    if payload:
        check(
            "token: exp ~ iat + 12h",
            43000 <= (payload["exp"] - payload["iat"]) <= 43300,
            str(payload["exp"] - payload["iat"]),
        )
    expira = datetime.fromisoformat(datos["expira"])
    check(
        "token: expira es ISO UTC",
        expira.tzinfo is not None
        and expira.utcoffset().total_seconds() == 0,
        datos["expira"],
    )
    check(
        "token: firma con secreto incorrecto se rechaza",
        verificar_token_app(datos["token"], secreto="otro-secreto") is None,
    )
    payload_b64, firma = datos["token"].split(".")
    alterado = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
    check(
        "token: payload alterado se rechaza",
        verificar_token_app(f"{alterado}.{firma}", secreto="secreto-prueba") is None,
    )
    check(
        "token: firma alterada se rechaza",
        verificar_token_app(
            f"{payload_b64}.{firma[:-2]}xx", secreto="secreto-prueba"
        )
        is None,
    )
    check(
        "token: basura/cadena vacia no lanza",
        verificar_token_app("basura") is None
        and verificar_token_app("a.b.c") is None
        and verificar_token_app("") is None
        and verificar_token_app(None) is None,
    )
    vencido = crear_token_app(_UsuarioToken(), horas=-1, secreto="secreto-prueba")
    check(
        "token: vencido se rechaza",
        verificar_token_app(vencido["token"], secreto="secreto-prueba") is None,
    )
    token_default = crear_token_app(_UsuarioToken())
    check(
        "token: usa settings.secret_key por defecto",
        verificar_token_app(token_default["token"]) is not None,
    )
    check(
        "token: no consulta la BD (tv forma parte del payload)",
        verificar_token_app(
            crear_token_app(
                type(
                    "Otro",
                    (),
                    {"id": 1, "username": "u", "token_version": 99},
                )(),
                secreto="s",
            )["token"],
            secreto="s",
        ).get("tv")
        == 99,
    )


# ---------------------------------------------------------------------------
# (7) Modelos App + autenticacion en SQLite temporal
# ---------------------------------------------------------------------------
def _probar_modelos_auth(check) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from core.app_auth import autenticar_app, hash_password, revocar_sesiones
    from core.database import Base
    from core.models import (
        AppAsignacion,
        AppConfig,
        AppEvento,
        AppUsuario,
        Cuenta,
    )

    check(
        "modelos: tablas app_* registradas en Base.metadata",
        {"app_usuario", "app_asignacion", "app_config", "app_evento"}
        <= set(Base.metadata.tables),
    )

    tmp = Path(tempfile.mkdtemp(prefix="mwx_core_models_"))
    db = None
    engine = None
    try:
        ruta_db = tmp / "app_test.db"
        engine = create_engine(
            f"sqlite:///{ruta_db}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        db = Session()

        usuario = AppUsuario(
            username="admin", password_hash=hash_password("clave"), nombre="Admin"
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
        check(
            "modelos: AppUsuario se crea con defaults",
            usuario.id is not None
            and usuario.activo is True
            and usuario.token_version == 0
            and usuario.creado is not None
            and usuario.ultimo_acceso is None,
        )

        cuenta = Cuenta(usuario="cuenta_demo", plataforma="twitter", activa=True)
        db.add(cuenta)
        db.commit()
        db.refresh(cuenta)

        asignacion = AppAsignacion(
            usuario_id=usuario.id, cuenta_id=cuenta.id
        )
        config = AppConfig(
            usuario_id=usuario.id,
            hashtags='["#uno", "#dos"]',
            contexto="tema",
            narrativa="trasfondo",
            urls='["https://x.com/a/status/1"]',
            texto_base="base",
            imagenes='["foto.png"]',
        )
        evento = AppEvento(
            usuario_id=usuario.id, tipo="login", detalle="ok"
        )
        db.add_all([asignacion, config, evento])
        db.commit()
        db.refresh(config)
        db.refresh(evento)
        check(
            "modelos: AppAsignacion se crea",
            asignacion.id is not None
            and asignacion.usuario_id == usuario.id
            and asignacion.cuenta_id == cuenta.id,
        )
        check(
            "modelos: AppConfig defaults 10/2/60/49/False",
            config.acciones_por_cuenta == 10
            and config.pausa_seg == 2
            and config.duracion_min == 60
            and config.limite_dia == 49
            and config.permitir_ajustes is False,
        )
        check(
            "modelos: AppConfig guarda JSON como Text",
            config.hashtags == '["#uno", "#dos"]'
            and config.urls == '["https://x.com/a/status/1"]'
            and config.imagenes == '["foto.png"]'
            and config.contexto == "tema"
            and config.narrativa == "trasfondo"
            and config.texto_base == "base",
        )
        check(
            "modelos: AppEvento fecha con default",
            evento.fecha is not None and evento.tipo == "login",
        )

        db.add(AppAsignacion(usuario_id=usuario.id, cuenta_id=cuenta.id))
        try:
            db.commit()
            check("modelos: AppAsignacion unique(usuario, cuenta)", False)
        except IntegrityError:
            db.rollback()
            check("modelos: AppAsignacion unique(usuario, cuenta)", True)

        autenticado = autenticar_app(db, "ADMIN", "clave")
        check(
            "auth: usuario/password correctos (case-insensitive)",
            autenticado is not None and autenticado.id == usuario.id,
        )
        check(
            "auth: actualiza ultimo_acceso",
            autenticado is not None and autenticado.ultimo_acceso is not None,
        )
        check(
            "auth: password incorrecta -> None",
            autenticar_app(db, "admin", "clave-mala") is None,
        )
        inactivo = AppUsuario(
            username="inactivo",
            password_hash=hash_password("clave2"),
            activo=False,
        )
        db.add(inactivo)
        db.commit()
        check(
            "auth: usuario inactivo -> None",
            autenticar_app(db, "inactivo", "clave2") is None,
        )

        version_antes = usuario.token_version
        nueva_version = revocar_sesiones(db, usuario.id)
        db.refresh(usuario)
        check(
            "revocar: incrementa token_version y hace commit",
            nueva_version == version_antes + 1
            and usuario.token_version == version_antes + 1,
        )
        check(
            "revocar: usuario inexistente -> None",
            revocar_sesiones(db, 999999) is None,
        )
    finally:
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run(check) -> None:
    _probar_no_congelado(check)
    _probar_congelado(check)
    _probar_env_congelado(check)
    _probar_database(check)
    _probar_lazy_activaciones(check)
    _probar_password(check)
    _probar_tokens(check)
    _probar_modelos_auth(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_mwx_core.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
