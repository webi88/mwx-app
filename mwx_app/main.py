# -*- coding: utf-8 -*-
"""Punto de entrada del ejecutable MWX APP.

PRIMERO fija los valores por defecto de entorno que `core.config` necesita;
recien despues importa la interfaz (que a su vez importa `core`/`ia` de forma
perezosa). Asi el token de sesion se puede inyectar antes de arrancar una
campana.

Tambien expone un modo `--selftest` sin Tkinter pensado para verificar el
ejecutable (y el codigo congelado) de punta a punta. Como el .exe es windowed
(`console=False`), el resultado se escribe SIEMPRE en un archivo JSON y en
`logs/selftest.log`; nunca se imprimen credenciales.
"""
from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import os
import re
import sys
import time
from pathlib import Path

MODULOS_SELFTEST = (
    "core.config",
    "core.database",
    "core.models",
    "core.app_auth",
    "core.registro",
    "ia.prompts",
    "ia.generador_contenido",
    "activaciones.local",
    "activaciones.variaciones",
    "plataformas.chrome_driver",
    "plataformas.twitter.selenium_bot",
    "utils.proxies",
    "utils.forward_proxy",
    "utils.anti_detection",
    "mwx_app.config",
    "mwx_app.updater",
    "mwx_app.api_client",
    "mwx_app.store",
    "mwx_app.runner",
)

_CLAVES_SECRETAS = frozenset(
    {
        "password",
        "passwd",
        "contrasena",
        "contraseña",
        "token",
        "cookies",
        "cookie",
        "cookies_json",
        "auth_token",
        "totp",
        "totp_secret",
        "secret",
        "secret_key",
        "api_key",
        "openai_api_key",
        "email_password",
        "authorization",
    }
)
_SUFIJOS_SECRETOS = ("_password", "_token", "_secret")
_RE_URL_CREDENCIALES = re.compile(r"(?i)\b(https?://)[^/\s:@]+(?::[^/\s@]*)?@")


def _preparar_entorno() -> None:
    """Valores seguros para que la app arranque sin un .env configurado."""
    os.environ.setdefault("OPENAI_API_KEY", "mwx-session")
    os.environ.setdefault("SECRET_KEY", "mwx-app-local")


def _version() -> str:
    """Version de la app (sin importar modulos pesados)."""
    try:
        from mwx_app import __version__

        return str(__version__ or "1.1.0")
    except Exception:
        return "1.1.0"


def _imprimir_seguro(texto: str) -> None:
    """Imprime solo si hay consola (el .exe puede correr sin stdout)."""
    try:
        if sys.stdout is not None:
            print(texto, flush=True)
    except Exception:
        pass


def _crear_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MWX APP",
        description="Aplicacion de escritorio MWX APP.",
        exit_on_error=False,
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Verifica imports, base de datos y (opcional) login sin abrir la interfaz.",
    )
    parser.add_argument(
        "--selftest-out",
        default="selftest_mwx.json",
        metavar="RUTA",
        help="Archivo JSON donde se escribe el resultado del selftest.",
    )
    parser.add_argument(
        "--api-url",
        default="",
        metavar="URL",
        help="URL base de la API para probar el login real.",
    )
    parser.add_argument(
        "--username",
        default="",
        metavar="USUARIO",
        help="Usuario para el login real del selftest.",
    )
    parser.add_argument(
        "--password",
        default="",
        metavar="PASSWORD",
        help="Contrasena para el login real del selftest (nunca se escribe en el JSON).",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        metavar="RUTA",
        help="Carpeta de datos aislada (BD SQLite, logs) para el selftest.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Muestra la version y termina.",
    )
    return parser


def _parsear_argumentos(argv=None):
    parser = _crear_parser()
    try:
        argumentos, _ = parser.parse_known_args(argv)
        return argumentos
    except SystemExit:
        raise
    except Exception:
        return None


def _describir_error(error: BaseException) -> str:
    try:
        texto = str(error)
    except Exception:
        texto = ""
    nombre = type(error).__name__
    return f"{nombre}: {texto[:300]}" if texto else nombre


def _redactar(texto: str, secretos=()) -> str:
    limpio = _RE_URL_CREDENCIALES.sub(r"\1***@", str(texto or ""))
    for secreto in secretos:
        valor = str(secreto or "")
        if len(valor) >= 3:
            limpio = limpio.replace(valor, "***")
    return limpio


def _normalizar_api(valor: str) -> str:
    try:
        from mwx_app.config import normalizar_url_api

        return normalizar_url_api(valor)
    except Exception:
        return str(valor or "").strip().rstrip("/")


def _ruta_salida(valor: str) -> Path:
    texto = str(valor or "selftest_mwx.json").strip() or "selftest_mwx.json"
    ruta = Path(texto).expanduser()
    if not ruta.is_absolute():
        ruta = Path.cwd() / ruta
    return ruta.resolve()


def _preparar_selftest(data_dir: str) -> dict:
    """Aisla la BD y los logs ANTES de que se importe `core`."""
    info = {"data_dir": "", "error": ""}
    if not data_dir:
        return info
    try:
        ruta = Path(data_dir).expanduser()
        if not ruta.is_absolute():
            ruta = Path.cwd() / ruta
        ruta = ruta.resolve()
        (ruta / "data").mkdir(parents=True, exist_ok=True)
        (ruta / "logs").mkdir(parents=True, exist_ok=True)
        os.environ["MWX_DATA_DIR"] = str(ruta)
        base_datos = ruta / "data" / "gestor_redes.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{base_datos.as_posix()}"
        info["data_dir"] = str(ruta)
    except Exception as error:
        info["error"] = _describir_error(error)
    return info


def _configurar_logs_selftest(data_dir: str) -> None:
    """Evita escribir en stderr (puede ser None en el .exe windowed)."""
    try:
        from loguru import logger

        logger.remove()
        if data_dir:
            destino = Path(data_dir) / "logs" / "mwx_selftest_internal.log"
            destino.parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(destino),
                level="INFO",
                encoding="utf-8",
                enqueue=False,
                backtrace=False,
                diagnose=False,
            )
    except Exception:
        pass


def _es_clave_secreta(clave: str) -> bool:
    nombre = str(clave or "").strip().lower()
    if nombre in _CLAVES_SECRETAS:
        return True
    return nombre.endswith(_SUFIJOS_SECRETOS)


def _sanear_json(valor, secretos=()):
    """Red de seguridad: jamas deja credenciales en el resultado."""
    if isinstance(valor, dict):
        limpio = {}
        for clave, item in valor.items():
            if _es_clave_secreta(clave):
                continue
            limpio[str(clave)] = _sanear_json(item, secretos)
        return limpio
    if isinstance(valor, (list, tuple)):
        return [_sanear_json(item, secretos) for item in valor]
    if isinstance(valor, str):
        return _redactar(valor, secretos)
    return valor


def _contar_secciones(usuarios) -> dict:
    from core.database import get_db_session
    from core.models import Cuenta

    claves = {
        str(usuario or "").strip().lower()
        for usuario in usuarios or []
        if str(usuario or "").strip()
    }
    if not claves:
        return {}
    conteo: dict = {}
    with get_db_session() as db:
        for cuenta in db.query(Cuenta).filter(Cuenta.plataforma == "twitter").all():
            if str(cuenta.usuario or "").strip().lower() not in claves:
                continue
            seccion = str(getattr(cuenta, "seccion", "") or "").strip()
            clave = seccion or "sin_seccion"
            conteo[clave] = conteo.get(clave, 0) + 1
    return dict(sorted(conteo.items()))


def _escribir_json(ruta: Path, datos: dict) -> bool:
    try:
        ruta = Path(ruta)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        texto = json.dumps(datos, ensure_ascii=False, indent=2)
        temporal = ruta.with_name(ruta.name + ".tmp")
        temporal.write_text(texto, encoding="utf-8")
        os.replace(temporal, ruta)
        return True
    except Exception:
        try:
            Path(ruta).write_text(
                json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except Exception:
            return False


def _registrar_linea(resultado: dict, ruta_out: Path) -> None:
    destinos = []
    data_dir = str(resultado.get("data_dir") or "")
    if data_dir:
        destinos.append(Path(data_dir) / "logs" / "selftest.log")
    try:
        from mwx_app import config as mwx_config

        destinos.append(mwx_config.ruta_datos("logs/selftest.log"))
    except Exception:
        pass
    if not destinos:
        destinos.append(Path.cwd() / "logs" / "selftest.log")

    importadas = resultado.get("imports") or {}
    importaciones_ok = sum(
        1
        for valor in importadas.values()
        if isinstance(valor, dict) and valor.get("ok")
    )
    try:
        cuentas = int(resultado.get("cuentas") or 0)
    except (TypeError, ValueError):
        cuentas = 0
    linea = (
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"ok={bool(resultado.get('ok'))} "
        f"imports={importaciones_ok}/{len(importadas)} "
        f"cuentas={cuentas} "
        f"motor_lazy={bool(resultado.get('motor_lazy'))} "
        f"out={ruta_out}\n"
    )
    for destino in destinos:
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            with open(destino, "a", encoding="utf-8") as archivo:
                archivo.write(linea)
            return
        except Exception:
            continue


def _ejecutar_selftest(argumentos) -> int:
    ruta_out = _ruta_salida(argumentos.selftest_out)
    preparacion = _preparar_selftest(argumentos.data_dir)
    _configurar_logs_selftest(preparacion.get("data_dir") or "")

    api_url = _normalizar_api(argumentos.api_url)
    if api_url:
        os.environ["MWX_API_URL"] = api_url

    resultado = {
        "ok": True,
        "version": _version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "data_dir": preparacion.get("data_dir") or "",
        "selftest_out": str(ruta_out),
        "imports": {},
        "imports_ok": True,
        "motor_cargado": False,
        "motor_lazy": True,
        "init_db": {"ok": False, "error": ""},
        "login": {"solicitado": False, "ok": True, "error": ""},
        "cuentas": 0,
        "secciones": {},
        "usuario": {},
        "api_url": api_url,
        "expira": "",
        "error": "",
    }

    secretos = [argumentos.password] if argumentos.password else []
    if preparacion.get("error"):
        resultado["ok"] = False
        resultado["error"] = (
            "No se pudo preparar --data-dir: " + preparacion["error"]
        )

    for nombre in MODULOS_SELFTEST:
        try:
            importlib.import_module(nombre)
            resultado["imports"][nombre] = {"ok": True}
        except Exception as error:
            resultado["imports"][nombre] = {
                "ok": False,
                "error": _redactar(_describir_error(error), secretos),
            }
            resultado["imports_ok"] = False
            resultado["ok"] = False

    motor_cargado = "activaciones.motor" in sys.modules
    resultado["motor_cargado"] = motor_cargado
    resultado["motor_lazy"] = not motor_cargado
    if motor_cargado:
        resultado["ok"] = False

    if argumentos.data_dir and preparacion.get("error"):
        resultado["init_db"] = {
            "ok": False,
            "error": "No se ejecuto: fallo la preparacion de --data-dir.",
        }
    else:
        try:
            modulo_bd = importlib.import_module("core.database")
            modulo_bd.init_db()
            resultado["init_db"] = {"ok": True, "error": ""}
        except Exception as error:
            resultado["init_db"] = {
                "ok": False,
                "error": _redactar(_describir_error(error), secretos),
            }
            resultado["ok"] = False

    tiene_alguno = bool(api_url or argumentos.username or argumentos.password)
    if not tiene_alguno:
        resultado["login"] = {"solicitado": False, "ok": True, "error": ""}
    else:
        resultado["login"] = {"solicitado": True, "ok": False, "error": ""}
        if not (api_url and argumentos.username and argumentos.password):
            resultado["login"]["error"] = (
                "Se requieren --api-url, --username y --password juntos."
            )
            resultado["ok"] = False
        elif not resultado["init_db"].get("ok"):
            resultado["login"]["error"] = (
                "La base de datos local no esta disponible."
            )
            resultado["ok"] = False
        else:
            try:
                cliente_mod = importlib.import_module("mwx_app.api_client")
                cliente = cliente_mod.MWXClient(api_url)
                datos = cliente.login(
                    argumentos.username,
                    argumentos.password,
                    machine_id="mwx-selftest",
                )
                token = str(datos.get("token") or "")
                if token:
                    secretos.append(token)

                usuario = datos.get("usuario")
                if not isinstance(usuario, dict):
                    usuario = {}
                resultado["usuario"] = {
                    "nombre": str(
                        usuario.get("nombre") or usuario.get("username") or ""
                    )
                }
                resultado["expira"] = str(datos.get("expira") or "")

                store_mod = importlib.import_module("mwx_app.store")
                asignados = store_mod.sincronizar_perfil(datos)
                resultado["cuentas"] = len(asignados)
                try:
                    resultado["secciones"] = _contar_secciones(asignados)
                except Exception as error:
                    resultado["secciones"] = {}
                    resultado.setdefault("avisos", []).append(
                        "No se pudieron contar las secciones: "
                        + _redactar(_describir_error(error), secretos)
                    )
                resultado["login"] = {
                    "solicitado": True,
                    "ok": True,
                    "error": "",
                }
            except Exception as error:
                resultado["login"] = {
                    "solicitado": True,
                    "ok": False,
                    "error": _redactar(_describir_error(error), secretos),
                }
                resultado["ok"] = False

    resultado = _sanear_json(resultado, secretos)
    if not _escribir_json(ruta_out, resultado):
        resultado["ok"] = False
        resultado["error"] = "No se pudo escribir el archivo de resultado."
    _registrar_linea(resultado, ruta_out)
    return 0 if resultado.get("ok") else 1


def _ruta_log() -> str:
    try:
        from mwx_app import config

        return str(config.ruta_datos("logs/mwx_app.log"))
    except Exception:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "MWX APP", "data", "logs", "mwx_app.log")


def _registrar_fatal(error: BaseException) -> None:
    try:
        import traceback
        import time

        ruta = _ruta_log()
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, "a", encoding="utf-8") as archivo:
            archivo.write(
                f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] FATAL\n"
                f"{''.join(traceback.format_exception(type(error), error, error.__traceback__))}\n"
            )
    except Exception:
        pass


def _mostrar_error_fatal(mensaje: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        raiz = tk.Tk()
        raiz.withdraw()
        messagebox.showerror("MWX APP", mensaje, parent=raiz)
        raiz.destroy()
        return
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, mensaje, "MWX APP", 0x10)
    except Exception:
        pass


def main(argv=None) -> int:
    _preparar_entorno()
    argumentos = _parsear_argumentos(argv)
    if argumentos is None:
        return 2
    if argumentos.version:
        _imprimir_seguro(f"MWX APP {_version()}")
        return 0
    if argumentos.selftest:
        try:
            return _ejecutar_selftest(argumentos)
        except Exception as error:
            fatal = {
                "ok": False,
                "version": _version(),
                "frozen": bool(getattr(sys, "frozen", False)),
                "data_dir": "",
                "imports": {},
                "imports_ok": False,
                "motor_cargado": "activaciones.motor" in sys.modules,
                "motor_lazy": "activaciones.motor" not in sys.modules,
                "init_db": {"ok": False, "error": ""},
                "login": {"solicitado": False, "ok": False, "error": ""},
                "cuentas": 0,
                "secciones": {},
                "usuario": {},
                "api_url": _normalizar_api(argumentos.api_url),
                "expira": "",
                "error": _redactar(
                    _describir_error(error),
                    [argumentos.password] if argumentos.password else [],
                ),
            }
            try:
                ruta = _ruta_salida(argumentos.selftest_out)
                _escribir_json(
                    ruta,
                    _sanear_json(
                        fatal,
                        [argumentos.password] if argumentos.password else [],
                    ),
                )
                _registrar_linea(fatal, ruta)
            except Exception:
                pass
            return 1

    try:
        from mwx_app.ui import ejecutar_app

        ejecutar_app()
        return 0
    except Exception as error:
        _registrar_fatal(error)
        _mostrar_error_fatal(
            "No se pudo abrir MWX APP.\n\n"
            "Cierra la aplicación, revisa que tengas conexión a internet "
            "e inténtalo de nuevo. Si el problema continúa, contacta a tu administrador."
        )
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
