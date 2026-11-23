# -*- coding: utf-8 -*-
"""Tests del modo `--selftest` del ejecutable MWX APP (sin GUI).

Se ejecuta `python -m mwx_app.main --selftest ...` en un subproceso con el
Python del venv, SIEMPRE con `--data-dir` temporal para no tocar `data/` real.
Cubre:
  - imports de los 19 modulos criticos y `core.database.init_db()`;
  - que `activaciones.motor` NO se cargue (debe ser perezoso);
  - login real contra un `http.server` local (login/perfil) y sincronizacion
    de 2 cuentas en la SQLite del `--data-dir`;
  - que el JSON no contenga token, password, cookies, auth_token ni TOTP;
  - `--version` sin abrir Tkinter.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_selftest.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

_PASSWORD = "clave-selftest-no-real"
_TOKEN = "token-selftest-no-real"
_TOTP = "JBSWY3DPEHPK3PXP"
_AUTH_TOKEN = "a" * 40
_COOKIE_VALOR = "cookie-selftest-no-real"
_PASSWORD_CUENTA = "pass-cuenta-no-real"
_EXPIRA = "2030-01-02T03:04:05Z"
_USUARIO = "demo_selftest"


def _payload_login() -> dict:
    """Payload del contrato de `api/main.py` con secretos falsos incluidos."""
    return {
        "ok": True,
        "token": _TOKEN,
        "expira": _EXPIRA,
        "usuario": {"username": _USUARIO, "nombre": "Demo Selftest"},
        "campana": {
            "hashtags": ["#Demo"],
            "contexto": "",
            "narrativa": "",
            "urls": [],
            "texto_base": "",
            "acciones_por_cuenta": 10,
            "pausa_seg": 2,
            "duracion_min": 60,
            "limite_dia": 49,
            "imagenes": [],
            "permitir_ajustes": False,
        },
        "cuentas": [
            {
                "usuario": "cuenta_uno",
                "nombre_mostrado": "Cuenta Uno",
                "seccion": "CI",
                "tipo_cuenta": "politica",
                "perfil_personalidad": "formal",
                "tier_calidad": "tier1",
                "totp_secret": _TOTP,
                "auth_token": _AUTH_TOKEN,
                "cookies_json": json.dumps(
                    [{"name": "auth_token", "value": _COOKIE_VALOR}]
                ),
                "user_agent": "UA/1.0",
                "password": _PASSWORD_CUENTA,
            },
            {
                "usuario": "cuenta_dos",
                "nombre_mostrado": "Cuenta Dos",
                "seccion": "IP",
                "tipo_cuenta": "ciudadana",
                "perfil_personalidad": "popular",
                "tier_calidad": "tier2",
                "totp_secret": "",
                "auth_token": "",
                "cookies_json": "",
                "user_agent": "UA/2.0",
                "password": _PASSWORD_CUENTA,
            },
        ],
    }


class _ServidorFake:
    """Servidor HTTP minimo con los endpoints login/perfil del contrato."""

    def __init__(self):
        self.llamadas: list = []
        servidor = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def _responder(self, codigo: int, datos: dict) -> None:
                cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)

            def do_POST(self):
                largo = int(self.headers.get("Content-Length") or 0)
                crudo = self.rfile.read(largo) if largo else b"{}"
                try:
                    datos = json.loads(crudo.decode("utf-8"))
                except Exception:
                    datos = {}
                servidor.llamadas.append((self.path, datos))
                if self.path == "/v1/login":
                    self._responder(200, _payload_login())
                elif self.path == "/v1/estado":
                    self._responder(
                        200, {"ok": True, "activo": True, "nombre": "Demo Selftest"}
                    )
                else:
                    self._responder(404, {"ok": False, "error": "no encontrado"})

            def do_GET(self):
                if self.path == "/v1/perfil":
                    payload = _payload_login()
                    payload.pop("token", None)
                    for cuenta in payload.get("cuentas", []):
                        cuenta.pop("password", None)
                    self._responder(200, payload)
                else:
                    self._responder(404, {"ok": False, "error": "no encontrado"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._hilo = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def iniciar(self):
        self._hilo.start()
        return self

    @property
    def url(self) -> str:
        host, puerto = self._httpd.server_address[:2]
        return f"http://{host}:{puerto}"

    def cerrar(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
            self._hilo.join(5)


def _entorno_limpio() -> dict:
    """Entorno sin variables que puedan apuntar a la BD/URL reales."""
    entorno = dict(os.environ)
    for clave in ("MWX_DATA_DIR", "MWX_API_URL", "DATABASE_URL"):
        entorno.pop(clave, None)
    entorno["OPENAI_API_KEY"] = "sk-test-mwx-selftest"
    entorno["SECRET_KEY"] = "test-mwx-selftest-secret"
    entorno["PYTHONIOENCODING"] = "utf-8"
    entorno["PYTHONUTF8"] = "1"
    return entorno


def _comando(data_dir: Path, salida: Path, api_url="", username="", password=""):
    comando = [
        sys.executable,
        "-m",
        "mwx_app.main",
        "--selftest",
        "--data-dir",
        str(data_dir),
        "--selftest-out",
        str(salida),
    ]
    if api_url:
        comando += ["--api-url", api_url]
    if username:
        comando += ["--username", username]
    if password:
        comando += ["--password", password]
    return comando


def _ejecutar(comando, timeout=180):
    return subprocess.run(
        comando,
        cwd=str(RAIZ),
        env=_entorno_limpio(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _leer_json(ruta: Path) -> dict:
    return json.loads(Path(ruta).read_text(encoding="utf-8"))


def _sin_secretos(texto: str) -> bool:
    """True si el JSON no filtra credenciales ni claves sensibles."""
    prohibidos = (
        _TOKEN,
        _PASSWORD,
        _TOTP,
        _AUTH_TOKEN,
        _COOKIE_VALOR,
        _PASSWORD_CUENTA,
        '"password"',
        '"passwd"',
        '"token"',
        '"cookies"',
        '"cookies_json"',
        '"auth_token"',
        '"totp_secret"',
        '"secret_key"',
        '"api_key"',
    )
    return not any(prohibido in texto for prohibido in prohibidos)


def run(check) -> None:
    directorios = []
    db_real = RAIZ / "data" / "gestor_redes.db"
    estado_real = (
        (db_real.stat().st_mtime_ns, db_real.stat().st_size)
        if db_real.is_file()
        else None
    )

    try:
        # ------------------------------------------------------------------
        # 1) Selftest basico: imports + init_db aislado en un data-dir temp
        # ------------------------------------------------------------------
        base = Path(tempfile.mkdtemp(prefix="mwx_selftest_basico_"))
        directorios.append(base)
        data_dir = base / "datos"
        salida = base / "r.json"

        proceso = _ejecutar(_comando(data_dir, salida))
        check(
            "selftest basico: termina con exit 0",
            proceso.returncode == 0,
            f"exit={proceso.returncode} stderr={proceso.stderr[-200:]}",
        )
        check("selftest basico: escribe el JSON de resultado", salida.is_file())

        datos = _leer_json(salida) if salida.is_file() else {}
        check("selftest basico: ok=true", datos.get("ok") is True)
        check(
            "selftest basico: reporta los 19 imports",
            len(datos.get("imports") or {}) == 19,
            f"imports={len(datos.get('imports') or {})}",
        )
        check(
            "selftest basico: todos los imports OK",
            bool(datos.get("imports_ok"))
            and all(
                (valor or {}).get("ok")
                for valor in (datos.get("imports") or {}).values()
            ),
        )
        check(
            "selftest basico: init_db OK",
            bool((datos.get("init_db") or {}).get("ok")),
            str(datos.get("init_db")),
        )
        check(
            "selftest basico: activaciones.motor NO se carga (lazy)",
            datos.get("motor_lazy") is True and datos.get("motor_cargado") is False,
        )
        check(
            "selftest basico: data_dir aislado es el solicitado",
            datos.get("data_dir") == str(data_dir.resolve()),
        )
        check(
            "selftest basico: la SQLite temporal se creo",
            (data_dir / "data" / "gestor_redes.db").is_file(),
        )
        check(
            "selftest basico: sin credenciales no hay cuentas",
            datos.get("cuentas") == 0 and datos.get("login", {}).get("solicitado") is False,
        )
        log_selftest = data_dir / "logs" / "selftest.log"
        check(
            "selftest basico: escribe logs/selftest.log en el data-dir",
            log_selftest.is_file()
            and "ok=True" in log_selftest.read_text(encoding="utf-8"),
        )
        check(
            "selftest basico: JSON sin claves secretas",
            _sin_secretos(salida.read_text(encoding="utf-8")),
        )

        # ------------------------------------------------------------------
        # 2) --version sin Tkinter
        # ------------------------------------------------------------------
        version = _ejecutar([sys.executable, "-m", "mwx_app.main", "--version"], timeout=60)
        check("--version: exit 0", version.returncode == 0)
        check(
            "--version: imprime MWX APP 1.1.0",
            "MWX APP" in (version.stdout or "") and "1.1.0" in (version.stdout or ""),
            (version.stdout or "").strip(),
        )

        # ------------------------------------------------------------------
        # 3) Login real contra HTTP local + sync de 2 cuentas
        # ------------------------------------------------------------------
        base_api = Path(tempfile.mkdtemp(prefix="mwx_selftest_api_"))
        directorios.append(base_api)
        data_api = base_api / "datos"
        salida_api = base_api / "api.json"
        servidor = _ServidorFake().iniciar()
        try:
            proceso_api = _ejecutar(
                _comando(
                    data_api,
                    salida_api,
                    api_url=servidor.url,
                    username=_USUARIO,
                    password=_PASSWORD,
                )
            )
        finally:
            servidor.cerrar()

        check(
            "selftest API: termina con exit 0",
            proceso_api.returncode == 0,
            f"exit={proceso_api.returncode} stderr={proceso_api.stderr[-200:]}",
        )
        datos_api = _leer_json(salida_api) if salida_api.is_file() else {}
        check("selftest API: ok=true", datos_api.get("ok") is True)
        check(
            "selftest API: guarda exactamente 2 cuentas",
            datos_api.get("cuentas") == 2,
            f"cuentas={datos_api.get('cuentas')}",
        )
        check(
            "selftest API: usuario.nombre del servidor",
            (datos_api.get("usuario") or {}).get("nombre") == "Demo Selftest",
        )
        check(
            "selftest API: api_url normalizada",
            datos_api.get("api_url") == servidor.url,
            f"api_url={datos_api.get('api_url')} url={servidor.url}",
        )
        check(
            "selftest API: expira del servidor",
            datos_api.get("expira") == _EXPIRA,
        )
        secciones = datos_api.get("secciones") or {}
        check(
            "selftest API: conteo de secciones (CI + IP)",
            sum(secciones.values()) == 2
            and secciones.get("CI") == 1
            and secciones.get("IP") == 1,
            str(secciones),
        )
        check(
            "selftest API: login.ok=true",
            (datos_api.get("login") or {}).get("ok") is True,
        )
        check(
            "selftest API: el servidor recibio POST /v1/login",
            any(ruta == "/v1/login" for ruta, _ in servidor.llamadas),
        )

        texto_api = salida_api.read_text(encoding="utf-8") if salida_api.is_file() else ""
        check("selftest API: JSON sin token/password/cookies/TOTP", _sin_secretos(texto_api))
        check(
            "selftest API: no aparece el token de sesion",
            _TOKEN not in texto_api and _PASSWORD not in texto_api,
        )

        db_api = data_api / "data" / "gestor_redes.db"
        check("selftest API: existe la SQLite temporal", db_api.is_file())
        filas = []
        if db_api.is_file():
            conexion = sqlite3.connect(str(db_api))
            try:
                filas = conexion.execute(
                    "SELECT usuario, seccion, activa FROM cuentas ORDER BY usuario"
                ).fetchall()
            finally:
                conexion.close()
        check(
            "selftest API: la SQLite temporal tiene 2 cuentas activas",
            len(filas) == 2 and all(int(fila[2] or 0) == 1 for fila in filas),
            str(filas),
        )
        check(
            "selftest API: secciones guardadas en la BD",
            filas == [("cuenta_dos", "IP", 1), ("cuenta_uno", "CI", 1)],
            str(filas),
        )
        log_api = data_api / "logs" / "selftest.log"
        check(
            "selftest API: log con cuentas=2",
            log_api.is_file()
            and "cuentas=2" in log_api.read_text(encoding="utf-8"),
        )

        # ------------------------------------------------------------------
        # 4) Credenciales incompletas: falla con exit 1 sin tocar Tkinter
        # ------------------------------------------------------------------
        base_parcial = Path(tempfile.mkdtemp(prefix="mwx_selftest_parcial_"))
        directorios.append(base_parcial)
        salida_parcial = base_parcial / "parcial.json"
        proceso_parcial = _ejecutar(
            _comando(
                base_parcial / "datos",
                salida_parcial,
                api_url="http://127.0.0.1:1",
            ),
            timeout=60,
        )
        check(
            "selftest parcial: exit 1 si faltan usuario/password",
            proceso_parcial.returncode == 1,
            f"exit={proceso_parcial.returncode}",
        )
        datos_parcial = _leer_json(salida_parcial) if salida_parcial.is_file() else {}
        check(
            "selftest parcial: ok=false y login.ok=false",
            datos_parcial.get("ok") is False
            and (datos_parcial.get("login") or {}).get("ok") is False,
        )
        check(
            "selftest parcial: explica que faltan las 3 opciones",
            "juntos" in str((datos_parcial.get("login") or {}).get("error") or ""),
        )

        estado_actual = (
            (db_real.stat().st_mtime_ns, db_real.stat().st_size)
            if db_real.is_file()
            else None
        )
        check(
            "selftest: NO toca la SQLite real de data/",
            estado_actual == estado_real,
        )
    finally:
        for directorio in directorios:
            shutil.rmtree(directorio, ignore_errors=True)


if __name__ == "__main__":
    fallos = []

    def check(nombre, condicion, extra=""):
        estado = "PASS" if condicion else "FAIL"
        print(f"  {estado} {nombre}{(' ' + str(extra)) if extra else ''}")
        if not condicion:
            fallos.append(nombre)
        return bool(condicion)

    run(check)
    print(f"\nRESULTADO: {'OK' if not fallos else 'FALLOS: ' + ', '.join(fallos)}")
    sys.exit(0 if not fallos else 1)
