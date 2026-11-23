# -*- coding: utf-8 -*-
"""Tests del ejecutable MWX APP (desktop) sin red y sin tocar `data/` real.

Cubre:
  - config: prioridad env > config.json > settings, normalizacion, machine_id,
    sesion persistida (<12 h, sin contrasena) y usuario recordado;
  - api_client: login OK, 401, 403, 429, timeout, JSON roto, perfil/estado,
    imagenes y eventos (sin red, con una sesion falsa);
  - store: upsert en SQLite temporal, desactivacion de no asignadas, sin borrar
    cuentas ni registros de acciones;
  - runner: kwargs correctos, imagenes descargadas, token inyectado en
    settings/env, ajustes permitidos/ignorados y cancelacion;
  - UI: smoke minimo del login (si hay display; si no, se omite).

Sin dependencias nuevas. Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_app.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

if "core.config" not in sys.modules:
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-mwx-app")
    os.environ.setdefault("SECRET_KEY", "mwx-test")

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.config import settings as _settings_global  # noqa: E402,F401
from mwx_app import api_client, config, runner, store, ui  # noqa: E402
from mwx_app.api_client import (  # noqa: E402
    CuentaDesactivadaError,
    CredencialesError,
    DemasiadosIntentosError,
    MENSAJE_CREDENCIALES,
    MENSAJE_DESACTIVADA,
    MENSAJE_RESPUESTA,
    MENSAJE_SESION,
    MENSAJE_SIN_CONEXION,
    MWXClient,
    ServidorError,
    SesionExpiradaError,
    SinConexionError,
)

_CLAVES_ENV = ("MWX_API_URL", "OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL")


@contextmanager
def _entorno(env=None, base=None, datos=None):
    """Aisla variables de entorno y las funciones de rutas de `config`."""
    previos = {clave: os.environ.get(clave) for clave in _CLAVES_ENV}
    try:
        for clave, valor in (env or {}).items():
            os.environ[clave] = str(valor)
        for clave in _CLAVES_ENV:
            if clave not in (env or {}):
                os.environ.pop(clave, None)
        parches = []
        if base is not None:
            parches.append(mock.patch.object(config, "_dir_base", lambda: Path(base)))
        if datos is not None:
            parches.append(mock.patch.object(config, "_dir_datos", lambda: Path(datos)))
        for parche in parches:
            parche.start()
        try:
            yield
        finally:
            for parche in reversed(parches):
                parche.stop()
    finally:
        for clave, valor in previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor


# ---------------------------------------------------------------------------
# API client falso
# ---------------------------------------------------------------------------
class _RespuestaFalsa:
    def __init__(self, estado=200, datos=None, contenido=b"", romper_json=False):
        self.status_code = estado
        self._datos = datos
        self.content = contenido
        self.romper_json = romper_json

    def json(self):
        if self.romper_json:
            raise ValueError("json roto")
        return self._datos


class _SesionFalsa:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def request(self, metodo, url, headers=None, json=None, timeout=None):
        self.llamadas.append(
            {
                "metodo": metodo,
                "url": url,
                "headers": dict(headers or {}),
                "json": json,
                "timeout": timeout,
            }
        )
        if not self.respuestas:
            return _RespuestaFalsa(200, {"ok": True})
        siguiente = self.respuestas.pop(0)
        if isinstance(siguiente, BaseException):
            raise siguiente
        return siguiente


def _login_ok():
    return _RespuestaFalsa(
        200,
        {
            "ok": True,
            "token": "token.firma",
            "expira": "2030-01-01T00:00:00Z",
            "usuario": {"username": "operador", "nombre": "Operador"},
            "campana": {"hashtags": []},
            "cuentas": [],
        },
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _test_config(check, tmp: Path) -> None:
    base = tmp / "base"
    datos = tmp / "datos"
    base.mkdir(parents=True)
    datos.mkdir(parents=True)

    with _entorno(base=base, datos=datos):
        check(
            "config: normaliza URL sin /v1 ni barra final",
            config.normalizar_url_api("https://api.test/v1/") == "https://api.test"
            and config.normalizar_url_api("api.test/") == "https://api.test",
        )
        guardada = config.guardar_api_url("https://archivo.test/v1/")
        archivo = base / "config.json"
        check(
            "config: guardar_api_url escribe config.json normalizado",
            guardada == "https://archivo.test"
            and archivo.is_file()
            and json.loads(archivo.read_text(encoding="utf-8"))["api_url"]
            == "https://archivo.test",
        )
        check(
            "config: prioridad archivo sobre settings",
            config.api_url() == "https://archivo.test",
        )

        os.environ["MWX_API_URL"] = "https://entorno.test/"
        check(
            "config: prioridad env sobre archivo",
            config.api_url() == "https://entorno.test",
        )
        os.environ.pop("MWX_API_URL", None)

        archivo.unlink()
        from core.config import settings

        original = getattr(settings, "mwx_api_url", "")
        try:
            settings.mwx_api_url = "https://settings.test"
            check(
                "config: cae a settings.mwx_api_url",
                config.api_url() == "https://settings.test",
            )
            settings.mwx_api_url = ""
            check(
                "config: sin ninguna URL devuelve vacio",
                config.api_url() == "",
            )
        finally:
            settings.mwx_api_url = original

        primero = config.machine_id()
        segundo = config.machine_id()
        archivo_maquina = datos / "data" / "mwx_machine.json"
        try:
            uuid.UUID(primero)
            uuid_valido = True
        except ValueError:
            uuid_valido = False
        check(
            "config: machine_id estable y UUID valido",
            primero == segundo and uuid_valido and archivo_maquina.is_file(),
        )

        check("config: sin sesion devuelve {}", config.cargar_sesion() == {})
        check(
            "config: guardar_sesion persiste token y usuario",
            config.guardar_sesion("token-secreto", "operador", "Operador"),
        )
        sesion = config.cargar_sesion()
        texto_sesion = (datos / "data" / "mwx_sesion.json").read_text(
            encoding="utf-8"
        )
        check(
            "config: cargar_sesion devuelve token/usuario",
            sesion.get("token") == "token-secreto"
            and sesion.get("usuario") == "operador"
            and sesion.get("nombre") == "Operador",
        )
        check(
            "config: la sesion NO guarda contrasena",
            "password" not in texto_sesion.lower()
            and "contrasena" not in texto_sesion.lower(),
        )
        ruta_sesion = datos / "data" / "mwx_sesion.json"
        viejos = json.loads(ruta_sesion.read_text(encoding="utf-8"))
        viejos["guardado"] = time.time() - (13 * 3600)
        ruta_sesion.write_text(json.dumps(viejos), encoding="utf-8")
        check(
            "config: sesion de mas de 12 h se descarta",
            config.cargar_sesion() == {},
        )
        config.borrar_sesion()
        check("config: borrar_sesion limpia", config.cargar_sesion() == {})

        config.guardar_usuario_recordado("operador")
        check(
            "config: usuario recordado",
            config.cargar_usuario_recordado() == "operador",
        )
        config.guardar_usuario_recordado("")
        check(
            "config: borrar usuario recordado",
            config.cargar_usuario_recordado() == "",
        )


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
def _test_api_client(check) -> None:
    sesion = _SesionFalsa([_login_ok()])
    cliente = MWXClient("https://api.test/v1/", session=sesion)
    datos = cliente.login("operador", "clave", "maquina-1")
    llamada = sesion.llamadas[0]
    check(
        "api: login OK devuelve token",
        datos.get("token") == "token.firma",
    )
    check(
        "api: login usa /v1/login y cuerpo correcto",
        llamada["metodo"] == "POST"
        and llamada["url"] == "https://api.test/v1/login"
        and llamada["json"]
        == {
            "username": "operador",
            "password": "clave",
            "machine_id": "maquina-1",
        }
        and "Authorization" not in llamada["headers"],
    )

    with mock.patch.object(
        requests.Session, "request", return_value=_login_ok()
    ) as parcheada:
        datos_parche = MWXClient("https://api.test").login("operador", "clave")
    check(
        "api: requests monkeypatcheado para login OK",
        datos_parche.get("token") == "token.firma"
        and parcheada.call_count == 1,
    )

    sesion = _SesionFalsa(
        [
            _RespuestaFalsa(
                401, {"ok": False, "error": "usuario o contraseña incorrectos"}
            )
        ]
    )
    try:
        MWXClient("https://api.test", session=sesion).login("x", "y")
        credenciales = None
    except CredencialesError as error:
        credenciales = error
    check(
        "api: 401 de login -> CredencialesError",
        credenciales is not None
        and credenciales.mensaje == MENSAJE_CREDENCIALES,
    )

    sesion = _SesionFalsa(
        [_RespuestaFalsa(403, {"ok": False, "error": "cuenta desactivada"})]
    )
    try:
        MWXClient("https://api.test", session=sesion).login("x", "y")
        desactivada = None
    except CuentaDesactivadaError as error:
        desactivada = error
    check(
        "api: 403 -> CuentaDesactivadaError",
        desactivada is not None and desactivada.mensaje == MENSAJE_DESACTIVADA,
    )

    sesion = _SesionFalsa(
        [_RespuestaFalsa(429, {"ok": False, "error": "demasiados intentos"})]
    )
    try:
        MWXClient("https://api.test", session=sesion).login("x", "y")
        intentos = None
    except DemasiadosIntentosError as error:
        intentos = error
    check("api: 429 -> DemasiadosIntentosError", intentos is not None)

    sesion = _SesionFalsa([requests.Timeout("agotado")])
    try:
        MWXClient("https://api.test", session=sesion).login("x", "y")
        timeout = None
    except SinConexionError as error:
        timeout = error
    check(
        "api: timeout -> mensaje de conexion",
        timeout is not None and timeout.mensaje == MENSAJE_SIN_CONEXION,
    )

    sesion = _SesionFalsa([_RespuestaFalsa(200, romper_json=True)])
    try:
        MWXClient("https://api.test", session=sesion).login("x", "y")
        roto = None
    except ServidorError as error:
        roto = error
    check(
        "api: JSON roto -> ServidorError amigable",
        roto is not None and roto.mensaje == MENSAJE_RESPUESTA,
    )

    sesion = _SesionFalsa(
        [_RespuestaFalsa(401, {"ok": False, "error": "token inválido o expirado"})]
    )
    try:
        MWXClient("https://api.test", session=sesion).perfil("viejo")
        expirada = None
    except SesionExpiradaError as error:
        expirada = error
    check(
        "api: 401 de perfil -> SesionExpiradaError",
        expirada is not None and expirada.mensaje == MENSAJE_SESION,
    )

    sesion = _SesionFalsa(
        [
            _RespuestaFalsa(
                200,
                {
                    "ok": True,
                    "usuario": {"username": "operador", "nombre": "Operador"},
                    "campana": {"hashtags": ["#Uno"]},
                    "cuentas": [{"usuario": "uno"}],
                },
            )
        ]
    )
    perfil = MWXClient("https://api.test", session=sesion).perfil("token")
    check(
        "api: perfil devuelve cuentas/campana",
        perfil.get("campana", {}).get("hashtags") == ["#Uno"]
        and perfil.get("cuentas") == [{"usuario": "uno"}]
        and sesion.llamadas[0]["headers"].get("Authorization") == "Bearer token",
    )

    sesion = _SesionFalsa(
        [_RespuestaFalsa(200, {"ok": True, "activo": True, "nombre": "Operador"})]
    )
    estado = MWXClient("https://api.test", session=sesion).estado("token")
    check(
        "api: estado usa POST /v1/estado",
        estado.get("activo") is True
        and sesion.llamadas[0]["metodo"] == "POST"
        and sesion.llamadas[0]["url"].endswith("/v1/estado"),
    )

    sesion = _SesionFalsa([_RespuestaFalsa(200, contenido=b"imagen")])
    contenido = MWXClient("https://api.test", session=sesion).descargar_imagen(
        "token", "foto.jpg"
    )
    check(
        "api: descarga de imagen devuelve bytes",
        contenido == b"imagen"
        and sesion.llamadas[0]["url"].endswith("/v1/imagenes/foto.jpg"),
    )

    sesion = _SesionFalsa([_RespuestaFalsa(200, {"ok": True})])
    check(
        "api: evento responde ok",
        MWXClient("https://api.test", session=sesion).evento(
            "token", "campana_inicio", "detalle"
        )
        is True,
    )

    cliente_sin_url = MWXClient("", session=_SesionFalsa([]))
    try:
        cliente_sin_url.login("x", "y")
        sin_url = None
    except SinConexionError as error:
        sin_url = error
    check(
        "api: sin URL configurada -> mensaje claro",
        sin_url is not None and "configurar el servidor" in sin_url.mensaje.lower(),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
@contextmanager
def _sesion_temporal(tmp: Path):
    ruta = tmp / "mwx_store.db"
    engine = create_engine(
        f"sqlite:///{ruta.as_posix()}", connect_args={"check_same_thread": False}
    )
    import core.models  # noqa: F401
    from core.database import Base

    Base.metadata.create_all(engine)
    Fabrica = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def abrir():
        db = Fabrica()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    try:
        yield abrir, Fabrica
    finally:
        engine.dispose()


def _test_store(check, tmp: Path) -> None:
    from core.models import Cuenta, RegistroAccion

    with _sesion_temporal(tmp) as (abrir, Fabrica):
        db = Fabrica()
        try:
            uno = Cuenta(
                usuario="uno",
                plataforma="twitter",
                activa=False,
                password="vieja",
                seccion="CD",
                tier_calidad="",
                pausada_activacion=True,
            )
            dos = Cuenta(usuario="dos", plataforma="twitter", activa=True)
            facebook = Cuenta(usuario="fb", plataforma="facebook", activa=True)
            db.add_all([uno, dos, facebook])
            db.add(
                RegistroAccion(
                    usuario="dos", tipo="post", estado="exito", detalle="historico"
                )
            )
            db.commit()
        finally:
            db.close()

        perfil = {
            "usuario": {"username": "operador", "nombre": "Operador"},
            "campana": {},
            "cuentas": [
                {
                    "usuario": "uno",
                    "nombre_mostrado": "Uno Nuevo",
                    "handle_actual": "uno_x",
                    "seccion": "LIB",
                    "tipo_cuenta": "activista",
                    "perfil_personalidad": "popular",
                    "personalidad": "Directa",
                    "tier_calidad": "tier1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "auth_token": "auth-nuevo",
                    "cookies_json": '[{"name":"auth_token","value":"uno"}]',
                    "user_agent": "UA-uno",
                },
                {"usuario": "tres", "seccion": "JUS", "tier_calidad": "tier1"},
            ],
        }
        asignados = store.sincronizar_perfil(perfil, session_factory=abrir)
        check(
            "store: devuelve usuarios asignados en orden",
            asignados == ["uno", "tres"],
        )

        db = Fabrica()
        try:
            uno = db.query(Cuenta).filter(Cuenta.usuario == "uno").one()
            dos = db.query(Cuenta).filter(Cuenta.usuario == "dos").one()
            tres = db.query(Cuenta).filter(Cuenta.usuario == "tres").one()
            facebook = db.query(Cuenta).filter(Cuenta.usuario == "fb").one()
            registros = db.query(RegistroAccion).count()
            check(
                "store: upsert actualiza campos del contrato",
                uno.activa is True
                and uno.pausada_activacion is False
                and uno.tier_calidad == "tier1"
                and uno.seccion == "LIB"
                and uno.tipo_cuenta == "activista"
                and uno.perfil_personalidad == "popular"
                and uno.auth_token == "auth-nuevo"
                and uno.totp_secret == "JBSWY3DPEHPK3PXP"
                and uno.nombre_mostrado == "Uno Nuevo"
                and uno.handle_actual == "uno_x"
                and uno.user_agent == "UA-uno",
            )
            check(
                "store: cookies_json se guarda como JSON",
                isinstance(uno.cookies_json, list)
                and uno.cookies_json[0].get("name") == "auth_token",
            )
            check(
                "store: perfil sin password conserva la anterior",
                uno.password == "vieja",
            )
            check(
                "store: cuenta no asignada queda inactiva",
                dos.activa is False,
            )
            check(
                "store: cuenta nueva se crea activa",
                tres.activa is True and tres.plataforma == "twitter",
            )
            check(
                "store: NO toca otras plataformas",
                facebook.activa is True,
            )
            check(
                "store: NO borra registros de acciones",
                registros == 1,
            )
            check(
                "store: no borra cuentas (4 filas)",
                db.query(Cuenta).count() == 4,
            )
        finally:
            db.close()

        perfil_password = {
            "cuentas": [{"usuario": "uno", "password": "nueva-clave"}]
        }
        store.sincronizar_perfil(perfil_password, session_factory=abrir)
        db = Fabrica()
        try:
            uno = db.query(Cuenta).filter(Cuenta.usuario == "uno").one()
            dos = db.query(Cuenta).filter(Cuenta.usuario == "dos").one()
            check(
                "store: password solo se actualiza si viene en el perfil",
                uno.password == "nueva-clave",
            )
            check(
                "store: un perfil que no incluye la cuenta la desactiva",
                dos.activa is False,
            )
        finally:
            db.close()

        check(
            "store: payload sin lista de cuentas falla claro",
            _capturar_error(
                lambda: store.sincronizar_perfil({"campana": {}}, session_factory=abrir)
            )
            == "El servidor no envió la lista de cuentas.",
        )


def _capturar_error(funcion) -> str:
    try:
        funcion()
        return ""
    except Exception as error:
        return str(error)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _test_runner(check, tmp: Path) -> None:
    from core.config import settings

    env_previos = {clave: os.environ.get(clave) for clave in _CLAVES_ENV}
    settings_previos = (
        getattr(settings, "openai_api_key", ""),
        getattr(settings, "openai_api_base", ""),
    )
    ejecutor_previo = runner.ejecutar_campana_local
    carpeta_previo = runner._carpeta_imagenes
    evento_previo = runner._evento_async
    capturado: dict = {}
    eventos: list = []

    class _ClienteFalso:
        def __init__(self):
            self.imagenes = []

        def descargar_imagen(self, token, nombre):
            self.imagenes.append((token, nombre))
            return b"contenido:" + nombre.encode("utf-8")

        def evento(self, token, tipo, detalle=""):
            eventos.append((tipo, detalle))
            return True

    def ejecutor_falso(**kwargs):
        from core.config import settings as _settings

        capturado.update(kwargs)
        capturado["_env_key"] = os.environ.get("OPENAI_API_KEY")
        capturado["_env_base"] = os.environ.get("OPENAI_API_BASE")
        capturado["_settings_key"] = getattr(_settings, "openai_api_key", "")
        capturado["_settings_base"] = getattr(_settings, "openai_api_base", "")
        return {
            "ok": True,
            "exitosas": 2,
            "fallidas": 1,
            "omitidas": 0,
            "planificadas": 3,
            "hechas": 3,
            "cancelada": False,
            "eventos": [
                {
                    "usuario": "uno",
                    "rol": "hashtags",
                    "ok": True,
                    "url": "https://x.com/uno/status/1",
                    "detalle": "ok",
                }
            ],
        }

    extra = tmp / "extra.png"
    extra.write_bytes(b"extra")
    carpeta = tmp / "temp"
    cliente = _ClienteFalso()

    perfil = {
        "usuario": {"username": "operador", "nombre": "Operador"},
        "campana": {
            "hashtags": ["#Uno", "#Dos"],
            "contexto": "tema",
            "narrativa": "trasfondo",
            "urls": ["https://x.com/ancla/status/9"],
            "texto_base": "base",
            "acciones_por_cuenta": 10,
            "pausa_seg": 2,
            "duracion_min": 60,
            "limite_dia": 49,
            "imagenes": [
                {"nombre": "foto.jpg", "url": "/v1/imagenes/foto.jpg"},
                {"nombre": "otra.png", "url": "/v1/imagenes/otra.png"},
            ],
            "permitir_ajustes": True,
        },
        "cuentas": [],
    }
    cancelar = __import__("threading").Event()

    runner.ejecutar_campana_local = ejecutor_falso
    runner._carpeta_imagenes = lambda: carpeta
    runner._evento_async = lambda cliente_, token, tipo, detalle="": eventos.append(
        (tipo, detalle)
    )
    try:
        resumen = runner.ejecutar_campana(
            perfil,
            "token-sesion",
            "https://api.test/v1",
            ajustes={
                "acciones_por_cuenta": 3,
                "pausa_seg": 5,
                "duracion_min": 10,
                "limite_dia": 20,
            },
            imagenes_extra=[str(extra)],
            cancelar=cancelar,
            callback=lambda evento: None,
            cliente=cliente,
            usuarios=["uno", "dos"],
        )
    finally:
        runner.ejecutar_campana_local = ejecutor_previo
        runner._carpeta_imagenes = carpeta_previo
        runner._evento_async = evento_previo
        for clave, valor in env_previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        settings.openai_api_key, settings.openai_api_base = settings_previos

    check("runner: devuelve el resumen del motor", resumen.get("exitosas") == 2)
    check(
        "runner: pasa usuarios de la asignacion",
        capturado.get("usuarios") == ["uno", "dos"],
    )
    check(
        "runner: pasa contenido y hashtags del servidor",
        capturado.get("hashtags") == "#Uno #Dos"
        and capturado.get("contexto") == "tema"
        and capturado.get("narrativa") == "trasfondo"
        and capturado.get("texto_base") == "base"
        and capturado.get("urls") == ["https://x.com/ancla/status/9"],
    )
    check(
        "runner: aplica ajustes permitidos",
        capturado.get("acciones_por_cuenta") == 3
        and capturado.get("pausa_entre_acciones_seg") == 5
        and capturado.get("duracion_min") == 10
        and capturado.get("limite_dia") == 20,
    )
    check(
        "runner: probabilidad 100, captura URL y evento de cancelacion",
        capturado.get("probabilidad_imagen") == 100
        and capturado.get("capturar_url") is True
        and capturado.get("cancelar") is cancelar,
    )
    rutas = capturado.get("imagenes") or []
    check(
        "runner: descarga imagenes a DATA_DIR/temp",
        len(rutas) == 3
        and (carpeta / "mwx_img_1.jpg").is_file()
        and (carpeta / "mwx_img_2.png").is_file()
        and (carpeta / "mwx_img_1.jpg").read_bytes()
        == b"contenido:foto.jpg"
        and str(extra) in rutas,
    )
    check(
        "runner: usa el cliente para descargar cada imagen",
        cliente.imagenes
        == [("token-sesion", "foto.jpg"), ("token-sesion", "otra.png")],
    )
    check(
        "runner: inyecta token en env y settings",
        capturado.get("_env_key") == "token-sesion"
        and capturado.get("_env_base") == "https://api.test/v1"
        and capturado.get("_settings_key") == "token-sesion"
        and capturado.get("_settings_base") == "https://api.test/v1",
    )
    check(
        "runner: envia evento de inicio y fin",
        any(tipo == "campana_inicio" for tipo, _ in eventos)
        and any(tipo == "campana_fin" for tipo, _ in eventos),
    )

    settings.openai_api_key, settings.openai_api_base = settings_previos
    for clave, valor in env_previos.items():
        if valor is None:
            os.environ.pop(clave, None)
        else:
            os.environ[clave] = valor

    capturado.clear()
    perfil["campana"]["permitir_ajustes"] = False
    cliente2 = _ClienteFalso()
    evento_cancelado = __import__("threading").Event()
    evento_cancelado.set()

    runner.ejecutar_campana_local = ejecutor_falso
    runner._carpeta_imagenes = lambda: carpeta
    runner._evento_async = lambda cliente_, token, tipo, detalle="": eventos.append(
        (tipo, detalle)
    )
    try:
        runner.ejecutar_campana(
            perfil,
            "token-sesion",
            "https://api.test",
            ajustes={
                "acciones_por_cuenta": 1,
                "pausa_seg": 99,
                "duracion_min": 2,
                "limite_dia": 3,
            },
            imagenes_extra=[str(extra)],
            cancelar=evento_cancelado,
            cliente=cliente2,
            usuarios=["uno"],
        )
    finally:
        runner.ejecutar_campana_local = ejecutor_previo
        runner._carpeta_imagenes = carpeta_previo
        runner._evento_async = evento_previo
        for clave, valor in env_previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        settings.openai_api_key, settings.openai_api_base = settings_previos

    check(
        "runner: sin permitir_ajustes usa los valores del servidor",
        capturado.get("acciones_por_cuenta") == 10
        and capturado.get("pausa_entre_acciones_seg") == 2
        and capturado.get("duracion_min") == 60
        and capturado.get("limite_dia") == 49,
    )
    check(
        "runner: sin permitir_ajustes ignora imagenes locales",
        len(capturado.get("imagenes") or []) == 2,
    )
    check(
        "runner: propaga el evento de cancelacion",
        capturado.get("cancelar") is evento_cancelado
        and capturado["cancelar"].is_set(),
    )


# ---------------------------------------------------------------------------
# UI y entregables
# ---------------------------------------------------------------------------
def _test_ui(check) -> None:
    perfil_firma = {
        "campana": {"hashtags": ["#Uno"], "contexto": "tema"},
        "cuentas": [{"usuario": "u1", "auth_token": "secreto-a"}],
    }
    perfil_firma_cambio = json.loads(json.dumps(perfil_firma))
    perfil_firma_cambio["campana"]["hashtags"] = ["#Dos"]
    perfil_firma_secreto = json.loads(json.dumps(perfil_firma))
    perfil_firma_secreto["cuentas"][0]["auth_token"] = "secreto-b"
    check(
        "sync: la firma detecta cambios de campana",
        ui._firma_perfil(perfil_firma)
        != ui._firma_perfil(perfil_firma_cambio),
    )
    check(
        "sync: la firma ignora tokens/cookies de las cuentas",
        ui._firma_perfil(perfil_firma)
        == ui._firma_perfil(perfil_firma_secreto),
    )

    tmp = Path(tempfile.mkdtemp(prefix="mwx_ui_"))
    base = tmp / "base"
    datos = tmp / "datos"
    base.mkdir()
    datos.mkdir()
    try:
        with _entorno(
            env={"MWX_API_URL": "https://api.test"},
            base=base,
            datos=datos,
        ):
            try:
                raiz = tk.Tk()
                raiz.withdraw()
            except tk.TclError as error:
                print(f"  SKIP ui: no hay display ({error})", flush=True)
                check("ui: smoke del login (omitido sin display)", True)
                return
            app = None
            try:
                app = ui.MWXApp(raiz, auto_reconectar=False)
                raiz.update_idletasks()
                check(
                    "ui: arranca en la pantalla de login",
                    app.pantalla_actual == "login"
                    and app._boton_login.cget("text") == "Entrar",
                )
                app._hacer_login()
                raiz.update_idletasks()
                check(
                    "ui: valida usuario/contrasena vacios sin lanzar",
                    "usuario" in app._lbl_error.cget("text").lower(),
                )

                class _ClientePerfil:
                    def __init__(self, perfil):
                        self.perfil_datos = perfil
                        self.llamadas = 0

                    def perfil(self, token):
                        self.llamadas += 1
                        return self.perfil_datos

                perfil_ui = {
                    "usuario": {"username": "operador", "nombre": "Operador"},
                    "campana": {
                        "hashtags": ["#Uno"],
                        "contexto": "tema",
                        "narrativa": "",
                        "urls": ["https://x.com/ancla/status/1"],
                        "texto_base": "",
                        "acciones_por_cuenta": 5,
                        "pausa_seg": 2,
                        "duracion_min": 60,
                        "limite_dia": 49,
                        "imagenes": [],
                        "permitir_ajustes": True,
                    },
                    "cuentas": [{"usuario": "u1", "seccion": "CI"}],
                }
                app.token = "token-prueba"
                app.usuario = "operador"
                app.nombre = "Operador"
                app.perfil = perfil_ui
                app._sync_firma = ui._firma_perfil(perfil_ui)
                app._sync_hora = "12:00:00"
                app.cliente = _ClientePerfil(perfil_ui)
                app.mostrar_campana()
                raiz.update_idletasks()
                check(
                    "ui: muestra estado de sincronizacion y boton actualizar",
                    app._lbl_sync is not None
                    and "sincronizada" in app._lbl_sync.cget("text").lower(),
                )
                check(
                    "ui: programa el refresco automatico de la campana",
                    app._after_sync is not None,
                )

                perfil_nuevo = json.loads(json.dumps(perfil_ui))
                perfil_nuevo["campana"]["hashtags"] = ["#Nuevo"]
                app._despachar(
                    "perfil_sincronizado",
                    {
                        "perfil": perfil_nuevo,
                        "manual": False,
                        "iniciar": False,
                    },
                )
                raiz.update_idletasks()
                check(
                    "ui: el refresco aplica el perfil nuevo y avisa",
                    app.perfil == perfil_nuevo
                    and "actualizada" in app._lbl_sync.cget("text").lower(),
                )

                llamadas = {"iniciada": 0}
                cliente = _ClientePerfil(perfil_nuevo)
                app.cliente = cliente
                app.token = "token-prueba"
                app.pantalla_actual = "campana"
                app._puede_iniciar = True
                app._campana_activa = False
                app._sincronizando = False
                app._inicio_pendiente = False
                app._verificar_actualizacion_obligatoria = lambda: True
                app._iniciar_campana_ahora = lambda: llamadas.__setitem__(
                    "iniciada", llamadas["iniciada"] + 1
                )
                app._hilo = lambda funcion, *args: funcion(*args)
                with mock.patch.object(
                    store,
                    "sincronizar_perfil",
                    lambda *args, **kwargs: ["u1"],
                ):
                    app._iniciar_campana()
                    app._procesar_cola()
                check(
                    "ui: antes de iniciar pide el perfil al panel",
                    cliente.llamadas == 1,
                )
                check(
                    "ui: inicia con la configuracion recien bajada",
                    llamadas["iniciada"] == 1,
                )

                app._campana_activa = True
                app._programar_sincronizacion()
                check(
                    "ui: no refresca mientras la campana esta en curso",
                    app._after_sync is None,
                )
                app._campana_activa = False
            finally:
                if app is not None:
                    app.detener()
                raiz.destroy()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_entregables(check) -> None:
    main_py = RAIZ / "mwx_app" / "main.py"
    arbol = ast.parse(main_py.read_text(encoding="utf-8"))
    modulos_prohibidos = ("core", "ia", "activaciones")
    importaciones = []
    for nodo in arbol.body:
        if isinstance(nodo, ast.Import):
            importaciones.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            importaciones.append(nodo.module or "")
    check(
        "main: no importa core/ia/activaciones a nivel de modulo",
        not any(
            modulo == prohibido or modulo.startswith(prohibido + ".")
            for modulo in importaciones
            for prohibido in modulos_prohibidos
        ),
    )
    llamada_freeze = any(
        isinstance(nodo, ast.If)
        and isinstance(nodo.test, ast.Compare)
        and isinstance(nodo.test.left, ast.Name)
        and nodo.test.left.id == "__name__"
        and any(
            isinstance(hijo, ast.Expr)
            and isinstance(hijo.value, ast.Call)
            and isinstance(hijo.value.func, ast.Attribute)
            and hijo.value.func.attr == "freeze_support"
            for hijo in nodo.body
        )
        for nodo in arbol.body
    )
    check("main: freeze_support dentro del bloque __main__", llamada_freeze)
    check(
        "entregables: icono y logo generados",
        (RAIZ / "assets" / "mwx_app.ico").is_file()
        and (RAIZ / "assets" / "mwx_logo.png").is_file(),
    )
    especificacion = (RAIZ / "mwx_app.spec").read_text(encoding="utf-8")
    requisitos = (RAIZ / "requirements-mwx.txt").read_text(encoding="utf-8")
    excluidos = (
        "streamlit",
        "pandas",
        "faker",
        "apscheduler",
        "telegram",
        "web",
        "activaciones.motor",
        "plataformas.twitter.api_http",
    )
    check(
        "entregables: spec con onedir, icono y exclusiones",
        "COLLECT(" in especificacion
        and "console=False" in especificacion
        and "mwx_app.ico" in especificacion
        and all(nombre in especificacion for nombre in excluidos),
    )
    check(
        "entregables: requirements-mwx con pines del contrato",
        "selenium==4.48.0" in requisitos
        and "undetected-chromedriver==3.5.5" in requisitos
        and "sqlalchemy==2.0.35" in requisitos
        and "openai==0.28.0" in requisitos
        and "pyotp==2.9.0" in requisitos,
    )
    check(
        "entregables: build_mwx.bat existe",
        (RAIZ / "build_mwx.bat").is_file(),
    )


# ---------------------------------------------------------------------------
def run(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_app_test_"))
    try:
        _test_config(check, tmp)
        _test_api_client(check)
        _test_store(check, tmp)
        _test_runner(check, tmp)
        _test_ui(check)
        _test_entregables(check)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
