# -*- coding: utf-8 -*-
"""Tests rapidos de CapSolver + integracion del captcha automatico (SIN red/Chrome).

Cubre el contrato de `utils/captcha_solver.py` y su integracion en
`cuentas/change_org.py` (secciones 22 y 23 del docstring del modulo):

  (22) `utils.captcha_solver`:
       - `api_key`/`disponible`/`estado` con y sin env, y fallback al `.env`
         del proyecto (monkeypatcheado a un temp file; NUNCA lee el real).
       - `resolver_turnstile`: createTask -> processing -> ready con token;
         `AntiTurnstileTaskProxyLess` sin proxy y `AntiTurnstileTask` con proxy
         crudo (`host:port:user:pass`, URL con credenciales, socks5); metadata
         (`action`/`cdata`/`chlPageData`) solo con campos no vacios; errores
         (`errorId != 0` con codigo/descripcion, HTTP, taskId ausente, ready sin
         token/solution); timeout por reloj falso; excepciones capturadas;
         `segundos` reales; NUNCA lanza y NUNCA imprime la API key.
       - `resolver_challenge_cloudflare`: `AntiCloudflareTask` EXIGE proxy
         (sin proxy -> ok=False con "requiere proxy" SIN llamar a la API);
         solucion con `cookies`/`userAgent` normalizados.

  (23) Integracion en `ChangeOrgReportBot`:
       - `resolver_captcha` (kwarg AL FINAL del bot y de las 4 funciones
         publicas; None -> env CHANGE_CAPTCHA_SOLVER -> "auto"); "off" nunca
         intenta; modulo ausente -> inactivo sin lanzar.
       - `_extraer_config_turnstile` / `_inyectar_token_turnstile` con
         FakeDriver (scripts mockeados) y tolerancia a JS roto.
       - `_avisar_captcha_api` con el contrato exacto y callback que no rompe.
       - `_resolver_reto_captcha_api`: exito ProxyLess (token inyectado y reto
         desaparecido), reintento UNICO con proxy crudo, fallo que restaura
         `ultimo_error`, interstitial con cookies por CDP + `aplicar_user_agent`
         + recarga, y cancelacion con MENSAJE_CANCELADO.
       - `registrar_o_entrar`: con solucionador activo el bucle supera el reto
         e inyecta el token; si el fake falla, cae al MODO ASISTIDO existente
         (`esperar_captcha_seg > 0`); `CHANGE_CAPTCHA_SOLVER=off` no llama al
         solucionador.
       - PerimeterX/HUMAN (`_detectar_perimeterx` por JS/selector/texto) y el
         FAST-SKIP: sin sitekey Turnstile NO se llama a CapSolver (ni
         `resolver_turnstile` ni `resolver_challenge_cloudflare` ni HTTP), se
         emite UN aviso `captcha_api` "fallo" con `_ERROR_PERIMETERX_API` y el
         flujo cae al MODO ASISTIDO; con sitekey Turnstile sigue resolviendo
         igual (no regresion).
       - Propagacion del kwarg a traves de `ejecutar_un_reporte`,
         `registrar_cuenta_change` y las 2 campanas (bots/funciones fake), con
         compatibilidad posicional de las firmas viejas.

Determinista: sin red, sin Chrome, sin BD y sin tocar `data/`: `time.sleep` se
neutraliza y el reloj de `utils.captcha_solver` es falso en los tests de
polling. `_ENV_FILE` apunta a un temporal inexistente salvo en el check
especifico del fallback.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_captcha_api.py   (solo este)
"""
from __future__ import annotations

import contextlib
import inspect
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from selenium.common.exceptions import NoSuchElementException  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402

import cuentas.change_org as change_org  # noqa: E402
import ia.generador_contenido as gc  # noqa: E402
import utils.captcha_solver as cs  # noqa: E402
from cuentas.change_org import (  # noqa: E402
    MENSAJE_CANCELADO,
    ChangeOrgReportBot,
    ejecutar_campana_registros,
    ejecutar_campana_reportes,
    ejecutar_un_reporte,
    registrar_cuenta_change,
)


@contextlib.contextmanager
def _sin_esperas():
    """Neutraliza `time.sleep` para que la suite sea rapida y determinista."""
    original = time.sleep
    time.sleep = lambda *args, **kwargs: None
    try:
        yield
    finally:
        time.sleep = original


class _FakeTime:
    """Reloj falso: `time.time()` avanza con cada `sleep()` (sin dormir)."""

    def __init__(self, inicio: float = 1000.0):
        self.actual = float(inicio)
        self.dormidos = []

    def time(self):
        return self.actual

    def sleep(self, segundos):
        self.dormidos.append(float(segundos))
        self.actual += max(0.0, float(segundos))


class _LoggerFake:
    """Logger que acumula mensajes para verificar que la key nunca se imprime."""

    def __init__(self):
        self.mensajes = []

    def _apunta(self, mensaje, *args, **kwargs):
        self.mensajes.append(str(mensaje))

    debug = _apunta
    info = _apunta
    warning = _apunta
    error = _apunta


# --------------------------------------------------------------------------- #
# Fakes Selenium minimos para la integracion en change_org
# --------------------------------------------------------------------------- #
class _FakeElement:
    """Elemento fake con la API que consume `change_org`."""

    def __init__(self, tag="div", texto="", attrs=None, visible=True):
        self.tag = tag
        self._texto = texto
        self.attrs = {str(k).lower(): v for k, v in (attrs or {}).items()}
        self.visible = visible
        self.typed = []

    @property
    def text(self):
        return self._texto

    def get_attribute(self, nombre):
        return self.attrs.get(str(nombre or "").lower())

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return True

    def click(self):
        return None

    def send_keys(self, *valores):
        for valor in valores:
            self.typed.append(str(valor))

    def find_elements(self, by, selector):
        return []

    def find_element(self, by, selector):
        raise NoSuchElementException(str(selector))


class FakeDriver:
    """Driver fake: deteccion Turnstile/PerimeterX, config, inyeccion y CDP."""

    def __init__(self, reto: str = "", config=None, px: str = ""):
        self.current_url = "https://www.change.org/login_or_join?user_flow=nav"
        self.title = ""
        self.page_source = ""
        self.reto = str(reto or "")
        self.config = dict(config or {})
        self.px = str(px or "")
        self.inyeccion_ok = True
        self.token_inyectado = ""
        self.reto_sigue_tras_inyectar = False
        self.cdp_calls = []
        self.gets = []
        self.scripts = []
        self.body = _FakeElement(tag="body", texto="")

    def get(self, url):
        self.gets.append(url)
        self.current_url = url

    def refresh(self):
        return None

    def set_page_load_timeout(self, valor):
        return None

    def set_script_timeout(self, valor):
        return None

    def execute_cdp_cmd(self, cmd, params):
        self.cdp_calls.append((cmd, params or {}))
        if (params or {}).get("name") == "cf_clearance":
            self.reto = ""
        return {"success": True}

    def execute_script(self, script, *args):
        self.scripts.append(script)
        texto = str(script)
        if "cf-turnstile-response" in texto:
            self.token_inyectado = str(args[0]) if args else ""
            if not self.reto_sigue_tras_inyectar:
                self.reto = ""
            return self.inyeccion_ok
        if "_pxAppId" in texto:
            return self.px
        if "window.turnstile" in texto:
            return self.reto
        if "data-sitekey" in texto and "salida" in texto:
            return dict(self.config)
        if "readyState" in texto:
            return "complete"
        return None

    def find_elements(self, by, selector):
        return []

    def find_element(self, by, selector):
        if by == By.TAG_NAME and selector == "body":
            return self.body
        raise NoSuchElementException(str(selector))


# --------------------------------------------------------------------------- #
# (22) utils.captcha_solver
# --------------------------------------------------------------------------- #
def test_solver_config(check):
    print("(22a) captcha_solver: config (env/estado/fallback .env)")
    with mock.patch.dict(os.environ):
        os.environ.pop("CAPSOLVER_API_KEY", None)
        check(
            "solver: sin env -> api_key '' / disponible False",
            cs.api_key() == "" and cs.disponible() is False,
        )
        estado = cs.estado()
        check(
            "solver: estado inactivo con contrato exacto",
            estado["activo"] is False
            and estado["proveedor"] == "capsolver"
            and "CAPSOLVER_API_KEY" in estado["motivo"],
            str(estado),
        )
        os.environ["CAPSOLVER_API_KEY"] = "  clave-secreta  "
        check(
            "solver: env con espacios -> strip",
            cs.api_key() == "clave-secreta" and cs.disponible() is True,
        )
        estado = cs.estado()
        check(
            "solver: estado activo",
            estado["activo"] is True and estado["proveedor"] == "capsolver",
            str(estado),
        )

    # Fallback al `.env` del proyecto (temp file; nunca el real).
    with tempfile.TemporaryDirectory(prefix="captcha_env_") as tmp:
        ruta = Path(tmp) / ".env"
        ruta.write_text(
            "# comentario\nOTRA=1\nCAPSOLVER_API_KEY=\"clave_en_archivo\"\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ), mock.patch.object(cs, "_ENV_FILE", ruta):
            os.environ.pop("CAPSOLVER_API_KEY", None)
            check(
                "solver: fallback al .env cuando os.environ no la trae",
                cs.api_key() == "clave_en_archivo" and cs.disponible() is True,
            )
            os.environ["CAPSOLVER_API_KEY"] = "env-manda"
            check(
                "solver: env vence al archivo",
                cs.api_key() == "env-manda",
            )
        ruta_inexistente = Path(tmp) / "no_existe.env"
        with mock.patch.dict(os.environ), mock.patch.object(
            cs, "_ENV_FILE", ruta_inexistente
        ):
            os.environ.pop("CAPSOLVER_API_KEY", None)
            check(
                "solver: sin env ni archivo -> ''",
                cs.api_key() == "" and cs.disponible() is False,
            )


def _fake_post(respuestas, llamadas):
    """`_post_json` fake que consume `respuestas` en orden y registra llamadas."""

    def fake(ruta, payload, timeout=20):
        llamadas.append((ruta, payload))
        if not respuestas:
            return False, {}, "sin respuestas"
        return respuestas.pop(0)

    return fake


def test_solver_turnstile_ok(check):
    print("(22b) captcha_solver: Turnstile OK + metadata + proxy")
    reloj = _FakeTime()
    llamadas = []
    respuestas = [
        (True, {"errorId": 0, "taskId": "t-1", "status": "idle"}, ""),
        (True, {"errorId": 0, "status": "processing"}, ""),
        (True, {"errorId": 0, "status": "ready", "solution": {"token": "TOK-OK"}}, ""),
    ]
    with mock.patch.object(cs, "api_key", return_value="CLAVE_TEST"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
        resultado = cs.resolver_turnstile(
            "https://www.change.org/login", "0xSITE", timeout_seg=10
        )
    check(
        "turnstile: createTask->processing->ready -> ok con token",
        resultado["ok"] is True
        and resultado["token"] == "TOK-OK"
        and resultado["task_id"] == "t-1",
        str(resultado),
    )
    check(
        "turnstile: contrato completo del resultado",
        set(resultado) == {"ok", "token", "error", "segundos", "task_id", "usado_proxy"}
        and resultado["error"] == ""
        and resultado["usado_proxy"] is False
        and isinstance(resultado["segundos"], float)
        and resultado["segundos"] >= 0,
        str(resultado),
    )
    tarea = llamadas[0][1]["task"]
    check(
        "turnstile: task ProxyLess sin metadata vacia",
        llamadas[0][0] == "/createTask"
        and llamadas[0][1]["clientKey"] == "CLAVE_TEST"
        and tarea["type"] == "AntiTurnstileTaskProxyLess"
        and tarea["websiteURL"] == "https://www.change.org/login"
        and tarea["websiteKey"] == "0xSITE"
        and "metadata" not in tarea,
        str(tarea),
    )
    check(
        "turnstile: getTaskResult con clientKey + taskId",
        llamadas[1][0] == "/getTaskResult"
        and llamadas[1][1] == {"clientKey": "CLAVE_TEST", "taskId": "t-1"},
        str(llamadas[1]),
    )
    check(
        "turnstile: polling durmio CAPSOLVER_POLL_SEG (3s)",
        reloj.dormidos == [3.0],
        str(reloj.dormidos),
    )

    # Metadata + proxy crudo `host:port:user:pass`.
    reloj = _FakeTime()
    llamadas = []
    respuestas = [
        (True, {"errorId": 0, "taskId": "t-2", "status": "idle"}, ""),
        (True, {"errorId": 0, "status": "ready", "solution": {"token": "TOK-P"}}, ""),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
        resultado = cs.resolver_turnstile(
            "https://x.test",
            "SK",
            proxy="proxy.example:1000:user_area-MX:secreto",
            action="login",
            cdata="c-data",
            chl_page_data="p-data",
            timeout_seg=5,
        )
    tarea = llamadas[0][1]["task"]
    check(
        "turnstile: proxy crudo -> AntiTurnstileTask + campos proxy",
        resultado["ok"] is True
        and resultado["usado_proxy"] is True
        and tarea["type"] == "AntiTurnstileTask"
        and tarea["proxyType"] == "http"
        and tarea["proxyAddress"] == "proxy.example"
        and tarea["proxyPort"] == 1000
        and tarea["proxyLogin"] == "user_area-MX"
        and tarea["proxyPassword"] == "secreto",
        str(tarea),
    )
    check(
        "turnstile: metadata con los campos no vacios",
        tarea.get("metadata")
        == {"action": "login", "cdata": "c-data", "chlPageData": "p-data"},
        str(tarea.get("metadata")),
    )

    for proxy in ("http://u:p@1.2.3.4:8080", "socks5://u:p@1.2.3.4:1080"):
        reloj = _FakeTime()
        llamadas = []
        respuestas = [
            (True, {"errorId": 0, "taskId": "t-3", "status": "idle"}, ""),
            (
                True,
                {"errorId": 0, "status": "ready", "solution": {"token": "T"}},
                "",
            ),
        ]
        with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
            cs, "time", reloj
        ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
            resultado = cs.resolver_turnstile("https://x", "SK", proxy=proxy)
        tarea = llamadas[0][1]["task"]
        esperado = "socks5" if proxy.startswith("socks5") else "http"
        check(
            f"turnstile: proxy URL {esperado} con credenciales",
            resultado["ok"] is True
            and resultado["usado_proxy"] is True
            and tarea["proxyType"] == esperado
            and tarea["proxyAddress"] == "1.2.3.4"
            and tarea["proxyPort"] == (1080 if esperado == "socks5" else 8080)
            and tarea["proxyLogin"] == "u"
            and tarea["proxyPassword"] == "p",
            str(tarea),
        )

    # Proxy no normalizable -> ProxyLess (no descarta la resolucion).
    reloj = _FakeTime()
    llamadas = []
    respuestas = [
        (True, {"errorId": 0, "taskId": "t-4", "status": "idle"}, ""),
        (True, {"errorId": 0, "status": "ready", "solution": {"token": "T"}}, ""),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
        resultado = cs.resolver_turnstile("https://x", "SK", proxy="solo-texto")
    check(
        "turnstile: proxy raro -> ProxyLess",
        resultado["ok"] is True
        and resultado["usado_proxy"] is False
        and llamadas[0][1]["task"]["type"] == "AntiTurnstileTaskProxyLess",
    )


def test_solver_turnstile_errores(check):
    print("(22c) captcha_solver: errores y timeout (nunca lanza)")
    reloj = _FakeTime()
    llamadas = []
    respuestas = [
        (
            True,
            {
                "errorId": 1,
                "errorCode": "ERROR_ZERO_BALANCE",
                "errorDescription": "No balance",
            },
            "",
        ),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: errorId != 0 -> ok False con codigo y descripcion",
        resultado["ok"] is False
        and "ERROR_ZERO_BALANCE" in resultado["error"]
        and "No balance" in resultado["error"],
        resultado["error"],
    )
    check(
        "solver: el error no trae token ni taskId",
        resultado["token"] == "" and resultado["task_id"] == "",
    )

    # HTTP caido (transporte no disponible).
    reloj = _FakeTime()
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(
        cs, "_post_json", return_value=(False, {}, "CapSolver no respondio (boom)")
    ):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: fallo HTTP -> ok False con el error propagado",
        resultado["ok"] is False and "no respondio" in resultado["error"],
        resultado["error"],
    )

    # createTask sin taskId.
    reloj = _FakeTime()
    respuestas = [(True, {"errorId": 0, "status": "idle"}, "")]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: createTask sin taskId -> ok False",
        resultado["ok"] is False and "taskId" in resultado["error"],
        resultado["error"],
    )

    # getTaskResult con errorId.
    reloj = _FakeTime()
    respuestas = [
        (True, {"errorId": 0, "taskId": "t", "status": "idle"}, ""),
        (True, {"errorId": 12, "errorCode": "E12", "errorDescription": "cap"}, ""),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: error en getTaskResult -> ok False",
        resultado["ok"] is False and "E12" in resultado["error"],
        resultado["error"],
    )

    # ready sin token / sin solution.
    for solucion, fragmento in (
        ({"type": "turnstile"}, "token"),
        ({}, "solution"),
    ):
        reloj = _FakeTime()
        respuestas = [
            (True, {"errorId": 0, "taskId": "t", "status": "idle"}, ""),
            (True, {"errorId": 0, "status": "ready", "solution": solucion}, ""),
        ]
        with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
            cs, "time", reloj
        ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
            resultado = cs.resolver_turnstile("https://x", "SK")
        check(
            f"solver: ready invalido ({fragmento}) -> ok False",
            resultado["ok"] is False and fragmento in resultado["error"],
            resultado["error"],
        )

    # Timeout: processing para siempre; el reloj falso avanza con los sleeps.
    reloj = _FakeTime()
    llamadas = []

    def siempre_processing(ruta, payload, timeout=20):
        llamadas.append(ruta)
        if ruta == "/createTask":
            return True, {"errorId": 0, "taskId": "t-timeout", "status": "idle"}, ""
        return True, {"errorId": 0, "status": "processing"}, ""

    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", siempre_processing):
        resultado = cs.resolver_turnstile("https://x", "SK", timeout_seg=5)
    check(
        "solver: timeout -> ok False con segundos",
        resultado["ok"] is False
        and "no termino en 5s" in resultado["error"]
        and resultado["segundos"] >= 5,
        f"{resultado['error']} segundos={resultado['segundos']}",
    )
    check(
        "solver: el timeout dejo de sondear al alcanzar el limite",
        len(llamadas) <= 4,
        str(len(llamadas)),
    )

    # `_post_json` que lanza -> el cliente NUNCA propaga.
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "_post_json", side_effect=RuntimeError("boom")
    ):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: excepcion de transporte capturada",
        resultado["ok"] is False and "error inesperado" in resultado["error"],
        resultado["error"],
    )
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "_post_json", side_effect=RuntimeError("boom")
    ):
        resultado = cs.resolver_challenge_cloudflare("https://x", proxy="1.2.3.4:80:u:p")
    check(
        "solver: excepcion en el interstitial tambien capturada",
        resultado["ok"] is False and "error inesperado" in resultado["error"],
        resultado["error"],
    )

    # Sin key / sin url o sitekey: fallan claro y sin red.
    llamadas = []
    with mock.patch.object(cs, "api_key", return_value=""), mock.patch.object(
        cs, "_post_json", _fake_post([], llamadas)
    ):
        resultado = cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: sin key -> error accionable y sin red",
        resultado["ok"] is False
        and "CAPSOLVER_API_KEY" in resultado["error"]
        and llamadas == [],
        resultado["error"],
    )
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "_post_json", _fake_post([], llamadas)
    ):
        sin_url = cs.resolver_turnstile("", "SK")
        sin_sitekey = cs.resolver_turnstile("https://x", "")
    check(
        "solver: sin url o sitekey -> error accionable",
        sin_url["ok"] is False
        and "URL o el sitekey" in sin_url["error"]
        and sin_sitekey["ok"] is False,
        f"{sin_url['error']} | {sin_sitekey['error']}",
    )


def test_solver_challenge_cloudflare(check):
    print("(22d) captcha_solver: interstitial Cloudflare (requiere proxy)")
    reloj = _FakeTime()
    llamadas = []
    respuestas = [
        (True, {"errorId": 0, "taskId": "cf-1", "status": "idle"}, ""),
        (
            True,
            {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "cookies": {"cf_clearance": "CF-ABC"},
                    "userAgent": "Mozilla/5.0 Test",
                },
            },
            "",
        ),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, llamadas)):
        resultado = cs.resolver_challenge_cloudflare(
            "https://www.change.org/x", proxy="1.2.3.4:8080:u:p"
        )
    check(
        "cloudflare: ok con cookies y userAgent normalizados",
        resultado["ok"] is True
        and resultado["cookies"] == {"cf_clearance": "CF-ABC"}
        and resultado["user_agent"] == "Mozilla/5.0 Test"
        and resultado["task_id"] == "cf-1",
        str(resultado),
    )
    tarea = llamadas[0][1]["task"]
    check(
        "cloudflare: task AntiCloudflareTask con proxy",
        tarea["type"] == "AntiCloudflareTask"
        and tarea["proxyType"] == "http"
        and tarea["proxyAddress"] == "1.2.3.4"
        and tarea["proxyPort"] == 8080,
        str(tarea),
    )

    # Cookies en lista (variante de la API) -> dict.
    reloj = _FakeTime()
    respuestas = [
        (True, {"errorId": 0, "taskId": "cf-2", "status": "idle"}, ""),
        (
            True,
            {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "cookies": [{"name": "cf_clearance", "value": "CF-L"}],
                    "userAgent": "UA",
                },
            },
            "",
        ),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
        resultado = cs.resolver_challenge_cloudflare(
            "https://x", proxy="1.2.3.4:80:u:p"
        )
    check(
        "cloudflare: cookies en lista -> dict",
        resultado["ok"] is True and resultado["cookies"] == {"cf_clearance": "CF-L"},
        str(resultado["cookies"]),
    )

    # Sin proxy usable: falla SIN llamar a la API.
    llamadas = []
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "_post_json", _fake_post([], llamadas)
    ):
        resultado = cs.resolver_challenge_cloudflare("https://x")
    check(
        "cloudflare: sin proxy -> ok False 'requiere proxy' y sin red",
        resultado["ok"] is False
        and "requiere proxy" in resultado["error"]
        and llamadas == [],
        resultado["error"],
    )

    # ready sin cookies ni userAgent (solution valida pero sin datos utiles).
    reloj = _FakeTime()
    respuestas = [
        (True, {"errorId": 0, "taskId": "cf-3", "status": "idle"}, ""),
        (
            True,
            {
                "errorId": 0,
                "status": "ready",
                "solution": {"type": "cloudflare"},
            },
            "",
        ),
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
        resultado = cs.resolver_challenge_cloudflare(
            "https://x", proxy="1.2.3.4:80:u:p"
        )
    check(
        "cloudflare: sin cookies ni UA -> ok False",
        resultado["ok"] is False and "cookies" in resultado["error"],
        resultado["error"],
    )

    # Error de la API.
    reloj = _FakeTime()
    respuestas = [
        (
            True,
            {
                "errorId": 1,
                "errorCode": "ERROR_PROXY",
                "errorDescription": "proxy muerto",
            },
            "",
        )
    ]
    with mock.patch.object(cs, "api_key", return_value="K"), mock.patch.object(
        cs, "time", reloj
    ), mock.patch.object(cs, "_post_json", _fake_post(respuestas, [])):
        resultado = cs.resolver_challenge_cloudflare(
            "https://x", proxy="1.2.3.4:80:u:p"
        )
    check(
        "cloudflare: errorId != 0 -> ok False con codigo",
        resultado["ok"] is False and "ERROR_PROXY" in resultado["error"],
        resultado["error"],
    )


def test_solver_no_loguea_key(check):
    print("(22e) captcha_solver: la API key jamas aparece en logs")
    logger_fake = _LoggerFake()
    llamadas = []
    respuestas = [
        (True, {"errorId": 0, "taskId": "t", "status": "idle"}, ""),
        (True, {"errorId": 0, "status": "ready", "solution": {"token": "X"}}, ""),
    ]
    reloj = _FakeTime()
    with mock.patch.object(cs, "logger", logger_fake), mock.patch.object(
        cs, "api_key", return_value="SUPER_SECRET_KEY_123"
    ), mock.patch.object(cs, "time", reloj), mock.patch.object(
        cs, "_post_json", _fake_post(respuestas, llamadas)
    ):
        cs.resolver_turnstile("https://x", "SK")
        cs.resolver_challenge_cloudflare("https://x", proxy="1.2.3.4:80:u:p")
    check(
        "solver: NUNCA se imprime la key (ni en exito)",
        logger_fake.mensajes
        and all("SUPER_SECRET_KEY_123" not in m for m in logger_fake.mensajes),
        str(logger_fake.mensajes[:3]),
    )

    logger_fake = _LoggerFake()
    reloj = _FakeTime()
    with mock.patch.object(cs, "logger", logger_fake), mock.patch.object(
        cs, "api_key", return_value="SUPER_SECRET_KEY_123"
    ), mock.patch.object(cs, "time", reloj), mock.patch.object(
        cs, "_post_json", side_effect=RuntimeError("boom")
    ):
        cs.resolver_turnstile("https://x", "SK")
    check(
        "solver: tampoco en el camino de excepcion",
        all("SUPER_SECRET_KEY_123" not in m for m in logger_fake.mensajes),
        str(logger_fake.mensajes),
    )


# --------------------------------------------------------------------------- #
# (23) Integracion en ChangeOrgReportBot
# --------------------------------------------------------------------------- #
def test_modo_captcha(check):
    print("(23a) change_org: modo resolver_captcha")
    with mock.patch.dict(os.environ):
        os.environ.pop("CAPSOLVER_API_KEY", None)
        os.environ.pop("CHANGE_CAPTCHA_SOLVER", None)
        check(
            "modo: default 'auto'",
            change_org._resolver_captcha_modo() == "auto",
        )
        check(
            "modo: off/0/false/no/disabled -> 'off'",
            change_org._resolver_captcha_modo("off") == "off"
            and change_org._resolver_captcha_modo("OFF") == "off"
            and change_org._resolver_captcha_modo("0") == "off"
            and change_org._resolver_captcha_modo(False) == "off"
            and change_org._resolver_captcha_modo("disabled") == "off",
        )
        check(
            "modo: auto/garbage -> 'auto'",
            change_org._resolver_captcha_modo("auto") == "auto"
            and change_org._resolver_captcha_modo("xyz") == "auto"
            and change_org._resolver_captcha_modo(True) == "auto",
        )
        os.environ["CHANGE_CAPTCHA_SOLVER"] = "off"
        check(
            "modo: env CHANGE_CAPTCHA_SOLVER leido",
            change_org._resolver_captcha_modo() == "off",
        )
        check(
            "modo: kwarg vence al env",
            change_org._resolver_captcha_modo("auto") == "auto",
        )
        check(
            "bot: default auto y kwarg off",
            ChangeOrgReportBot().resolver_captcha == "off"
            and ChangeOrgReportBot(resolver_captcha="auto").resolver_captcha
            == "auto"
            and ChangeOrgReportBot(resolver_captcha="off").resolver_captcha == "off",
        )
    bot = ChangeOrgReportBot()
    check(
        "bot: sin env queda 'auto'",
        bot.resolver_captcha == "auto",
    )
    with mock.patch.object(cs, "disponible", return_value=True):
        check(
            "activo: auto + key -> True",
            bot._resolver_captcha_activo() is True,
        )
        bot_off = ChangeOrgReportBot(resolver_captcha="off")
        check(
            "activo: 'off' jamas intenta aunque haya key",
            bot_off._resolver_captcha_activo() is False,
        )
    with mock.patch.object(cs, "disponible", return_value=False):
        check(
            "activo: auto sin key -> False",
            bot._resolver_captcha_activo() is False,
        )
    with mock.patch.dict(sys.modules, {"utils.captcha_solver": None}):
        check(
            "activo: modulo ausente -> False sin lanzar",
            bot._resolver_captcha_activo() is False,
        )


def test_extraer_e_inyectar(check):
    print("(23b) change_org: _extraer_config_turnstile / _inyectar_token_turnstile")
    driver = FakeDriver(
        config={
            "sitekey": "0xABC",
            "callback": "onTurnstileOk",
            "action": "login",
            "cdata": "cd-1",
            "chl_page_data": "pd-1",
        }
    )
    bot = ChangeOrgReportBot()
    bot.driver = driver
    config = bot._extraer_config_turnstile()
    check(
        "turnstile: extrae sitekey/callback/action/cdata/chl_page_data",
        config
        == {
            "sitekey": "0xABC",
            "callback": "onTurnstileOk",
            "action": "login",
            "cdata": "cd-1",
            "chl_page_data": "pd-1",
        },
        str(config),
    )

    bot.driver = object()  # sin execute_script
    config = bot._extraer_config_turnstile()
    check(
        "turnstile: sin execute_script -> claves vacias (no lanza)",
        set(config) == {"sitekey", "callback", "action", "cdata", "chl_page_data"}
        and not any(config.values()),
        str(config),
    )

    class _DriverConfigRara(FakeDriver):
        def execute_script(self, script, *args):
            return "no-dict"

    bot.driver = _DriverConfigRara()
    config = bot._extraer_config_turnstile()
    check(
        "turnstile: JS con respuesta rara -> claves vacias",
        all(clave in config for clave in ("sitekey", "callback", "action"))
        and not any(config.values()),
        str(config),
    )

    driver = FakeDriver()
    bot.driver = driver
    check(
        "token: se inyecta en el formulario",
        bot._inyectar_token_turnstile("TOK-INY") is True
        and driver.token_inyectado == "TOK-INY",
    )
    driver.inyeccion_ok = False
    check(
        "token: JS devuelve False -> False",
        bot._inyectar_token_turnstile("TOK-2") is False,
    )
    driver.token_inyectado = ""
    check(
        "token: vacio -> False sin tocar el driver",
        bot._inyectar_token_turnstile("") is False
        and driver.token_inyectado == "",
    )

    class _DriverRoto(FakeDriver):
        def execute_script(self, script, *args):
            raise RuntimeError("js roto")

    bot.driver = _DriverRoto()
    check(
        "token: JS roto -> False sin lanzar",
        bot._inyectar_token_turnstile("TOK") is False,
    )
    bot.driver = None
    check(
        "token: sin driver -> False",
        bot._inyectar_token_turnstile("TOK") is False,
    )


def test_aviso_captcha_api(check):
    print("(23c) change_org: aviso captcha_api con contrato exacto")
    avisos = []
    bot = ChangeOrgReportBot(
        cuenta={"usuario": "u1", "email": "u1@x.com"}, aviso=avisos.append
    )
    bot._avisar_captcha_api("iniciando", detalle="reto turnstile")
    check(
        "aviso: contrato exacto iniciando",
        avisos
        == [
            {
                "tipo": "captcha_api",
                "estado": "iniciando",
                "metodo": "capsolver",
                "usuario": "u1",
                "email": "u1@x.com",
                "segundos": 0,
                "detalle": "reto turnstile",
            }
        ],
        str(avisos),
    )
    bot._avisar_captcha_api("resuelto", segundos=7, detalle="token ok")
    check(
        "aviso: resuelto con segundos y detalle",
        avisos[1] == {
            "tipo": "captcha_api",
            "estado": "resuelto",
            "metodo": "capsolver",
            "usuario": "u1",
            "email": "u1@x.com",
            "segundos": 7,
            "detalle": "token ok",
        },
        str(avisos[1]),
    )

    def _explota(info):
        raise RuntimeError("callback roto")

    bot = ChangeOrgReportBot(aviso=_explota)
    bot._avisar_captcha_api("fallo", detalle="x")  # no debe lanzar
    check("aviso: callback que lanza no rompe", True)
    ChangeOrgReportBot()._avisar_captcha_api("fallo")
    check("aviso: sin callback no rompe", True)


def _fake_resolver_turnstile(registro, resultado=None):
    """Fake de `resolver_turnstile` que registra los kwargs recibidos."""

    def fake(url, sitekey, *, proxy="", action="", cdata="", chl_page_data="", timeout_seg=None):
        registro.append(
            {
                "url": url,
                "sitekey": sitekey,
                "proxy": proxy,
                "action": action,
                "cdata": cdata,
                "chl_page_data": chl_page_data,
                "timeout_seg": timeout_seg,
            }
        )
        if callable(resultado):
            return resultado(proxy)
        return resultado

    return fake


def _resultado_token(token="TOK", proxy=""):
    return {
        "ok": True,
        "token": token,
        "error": "",
        "segundos": 1.0,
        "task_id": "t",
        "usado_proxy": bool(proxy),
    }


def _resultado_fallo(error="sin saldo"):
    return {
        "ok": False,
        "token": "",
        "error": error,
        "segundos": 0.5,
        "task_id": "t",
        "usado_proxy": False,
    }


def test_resolver_api_turnstile(check):
    print("(23d) change_org: _resolver_reto_captcha_api (Turnstile)")
    driver = FakeDriver(
        reto="turnstile",
        config={"sitekey": "0xSITE", "action": "login", "cdata": "", "chl_page_data": ""},
    )
    avisos = []
    bot = ChangeOrgReportBot(
        cuenta={"usuario": "u1", "email": "u1@x.com"}, aviso=avisos.append
    )
    bot.driver = driver
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        _fake_resolver_turnstile(registro, _resultado_token("TOK-1")),
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: resuelto + token inyectado + reto desaparecido",
        resuelto is True
        and driver.token_inyectado == "TOK-1"
        and driver.reto == "",
        f"resuelto={resuelto} token={driver.token_inyectado!r} reto={driver.reto!r}",
    )
    check(
        "api turnstile: primer intento SIN proxy (ProxyLess) con la config del DOM",
        registro
        and registro[0]["proxy"] == ""
        and registro[0]["sitekey"] == "0xSITE"
        and registro[0]["url"] == driver.current_url
        and registro[0]["action"] == "login"
        and registro[0]["cdata"] == ""
        and registro[0]["chl_page_data"] == "",
        str(registro),
    )
    check(
        "api turnstile: avisos iniciando -> resuelto con metodo capsolver",
        [a["estado"] for a in avisos] == ["iniciando", "resuelto"]
        and all(a["tipo"] == "captcha_api" and a["metodo"] == "capsolver" for a in avisos)
        and isinstance(avisos[-1]["segundos"], int)
        and avisos[-1]["detalle"].startswith("resuelto"),
        str(avisos),
    )
    check(
        "api turnstile: ultimo_error intacto en exito",
        bot.ultimo_error == "",
    )

    # Primer intento falla -> UN reintento con el proxy crudo de la cuenta.
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-2"})
    avisos = []
    bot = ChangeOrgReportBot(
        proxy="proxy.example:1000:user_area-MX:secreto",
        cuenta={"usuario": "u2", "email": "u2@x.com"},
        aviso=avisos.append,
    )
    bot.driver = driver
    registro = []

    def resultado_con_proxy(proxy):
        return _resultado_token("TOK-PROXY", proxy) if proxy else _resultado_fallo("CAPTCHA_FAIL")

    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", _fake_resolver_turnstile(registro, resultado_con_proxy)
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: falla ProxyLess -> reintento UNICO con proxy crudo",
        resuelto is True
        and len(registro) == 2
        and registro[0]["proxy"] == ""
        and registro[1]["proxy"] == "http://user_area-MX:secreto@proxy.example:1000"
        and driver.token_inyectado == "TOK-PROXY",
        str(registro),
    )
    check(
        "api turnstile: el aviso final es resuelto",
        [a["estado"] for a in avisos] == ["iniciando", "resuelto"],
        str(avisos),
    )

    # Todo falla: False, detalle con ambos errores y ultimo_error restaurado.
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-3"})
    avisos = []
    bot = ChangeOrgReportBot(
        proxy="proxy.example:1000:user:p",
        cuenta={"usuario": "u3"},
        aviso=avisos.append,
    )
    bot.driver = driver
    bot.ultimo_error = "error previo"
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", _fake_resolver_turnstile(registro, _resultado_fallo("CAPTCHA_ZERO"))
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: fallo total -> False",
        resuelto is False and len(registro) == 2,
        str(resuelto),
    )
    check(
        "api turnstile: fallo no envenena ultimo_error",
        bot.ultimo_error == "error previo",
        bot.ultimo_error,
    )
    check(
        "api turnstile: aviso fallo con detalle de los 2 intentos",
        [a["estado"] for a in avisos] == ["iniciando", "fallo"]
        and "CAPTCHA_ZERO" in avisos[-1]["detalle"]
        and "con proxy" in avisos[-1]["detalle"],
        str(avisos[-1]),
    )

    # Cancelacion durante el intento.
    cancelar = threading.Event()
    cancelar.set()
    avisos = []
    bot = ChangeOrgReportBot(cancelar=cancelar, aviso=avisos.append)
    bot.driver = FakeDriver(reto="turnstile", config={"sitekey": "SK"})
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", _fake_resolver_turnstile(registro, _resultado_token())
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: cancelado -> False con MENSAJE_CANCELADO",
        resuelto is False
        and bot.ultimo_error == MENSAJE_CANCELADO
        and registro == [],
        f"resuelto={resuelto} error={bot.ultimo_error!r}",
    )
    check(
        "api turnstile: aviso fallo con el detalle de cancelacion",
        [a["estado"] for a in avisos] == ["iniciando", "fallo"]
        and avisos[-1]["detalle"] == MENSAJE_CANCELADO,
        str(avisos),
    )

    # Sin sitekey no se llama a resolver_turnstile (va al interstitial).
    driver = FakeDriver(reto="turnstile-dom", config={})
    avisos = []
    bot = ChangeOrgReportBot(aviso=avisos.append)
    bot.driver = driver
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", _fake_resolver_turnstile(registro, _resultado_token())
    ), mock.patch.object(
        cs,
        "resolver_challenge_cloudflare",
        lambda url, *, proxy="", timeout_seg=None: _resultado_fallo("requiere proxy"),
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile-dom")
    check(
        "api turnstile: sin sitekey -> interstitial y sin turnstile",
        resuelto is False and registro == [],
        str(registro),
    )

    # 'off' / sin disponible: no se llama a nada.
    avisos = []
    bot = ChangeOrgReportBot(resolver_captcha="off", aviso=avisos.append)
    bot.driver = FakeDriver(reto="turnstile", config={"sitekey": "SK"})
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", _fake_resolver_turnstile(registro, _resultado_token())
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: 'off' no intenta ni avisa",
        resuelto is False and registro == [] and avisos == [],
        f"resuelto={resuelto} avisos={avisos}",
    )

    # Un reto que ya fallo no se reintenta en la MISMA sesion (coste/tiempo).
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-4"})
    bot = ChangeOrgReportBot()
    bot.driver = driver
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        _fake_resolver_turnstile(registro, _resultado_fallo("CAPTCHA_ZERO")),
    ):
        primero = bot._resolver_reto_captcha_api("turnstile")
        segundo = bot._resolver_reto_captcha_api("turnstile")
    check(
        "api turnstile: un reto que fallo no se reintenta en la sesion",
        primero is False and segundo is False and len(registro) == 1,
        f"intentos={len(registro)} primero={primero} segundo={segundo}",
    )


def test_resolver_api_interstitial(check):
    print("(23e) change_org: _resolver_reto_captcha_api (interstitial)")
    driver = FakeDriver(reto="cloudflare-iframe", config={})
    avisos = []
    bot = ChangeOrgReportBot(
        proxy="proxy.example:1000:user_area-MX:secreto",
        cuenta={"usuario": "u1"},
        aviso=avisos.append,
    )
    bot.driver = driver
    capturado = {}
    ua_llamados = []

    def fake_cf(url, *, proxy="", timeout_seg=None):
        capturado["url"] = url
        capturado["proxy"] = proxy
        return {
            "ok": True,
            "cookies": {"cf_clearance": "CF-123"},
            "user_agent": "Mozilla/5.0 Test",
            "error": "",
            "segundos": 2.0,
            "task_id": "cf-1",
        }

    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_challenge_cloudflare", side_effect=fake_cf
    ), mock.patch.object(
        change_org,
        "aplicar_user_agent",
        lambda driver, ua: ua_llamados.append(ua) or True,
    ):
        resuelto = bot._resolver_reto_captcha_api("cloudflare-iframe")
    check(
        "api interstitial: resuelto con cookies + recarga + reto caido",
        resuelto is True and driver.reto == "",
        f"resuelto={resuelto} reto={driver.reto!r}",
    )
    check(
        "api interstitial: proxy crudo de la cuenta al solver",
        capturado.get("proxy") == "http://user_area-MX:secreto@proxy.example:1000"
        and capturado.get("url") == driver.current_url,
        str(capturado),
    )
    cookies_set = [
        params for cmd, params in driver.cdp_calls if cmd == "Network.setCookie"
    ]
    check(
        "api interstitial: cf_clearance inyectada por CDP",
        any(
            c.get("name") == "cf_clearance" and c.get("value") == "CF-123"
            for c in cookies_set
        ),
        str(cookies_set),
    )
    check(
        "api interstitial: userAgent de CapSolver aplicado",
        ua_llamados == ["Mozilla/5.0 Test"],
        str(ua_llamados),
    )
    check(
        "api interstitial: recarga la URL tras las cookies",
        driver.gets and driver.gets[-1] == capturado["url"],
        str(driver.gets),
    )
    check(
        "api interstitial: aviso resuelto con detalle del interstitial",
        [a["estado"] for a in avisos] == ["iniciando", "resuelto"]
        and "interstitial" in avisos[-1]["detalle"],
        str(avisos),
    )

    # Sin proxy: CapSolver avisa "requiere proxy" y el flujo falla sin lanzar.
    avisos = []
    bot = ChangeOrgReportBot(aviso=avisos.append)
    bot.driver = FakeDriver(reto="cloudflare-iframe")
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_challenge_cloudflare",
        lambda url, *, proxy="", timeout_seg=None: _resultado_fallo(
            "AntiCloudflareTask requiere proxy"
        ),
    ):
        resuelto = bot._resolver_reto_captcha_api("cloudflare-iframe")
    check(
        "api interstitial: sin proxy -> False con aviso fallo",
        resuelto is False
        and [a["estado"] for a in avisos] == ["iniciando", "fallo"]
        and "requiere proxy" in avisos[-1]["detalle"],
        str(avisos),
    )


def test_detectar_perimeterx(check):
    print("(23h) change_org: _detectar_perimeterx (JS/selector/texto)")
    bot = ChangeOrgReportBot()

    # JS: window._pxAppId.
    bot.driver = FakeDriver(px="perimeterx")
    check(
        "px: JS window._pxAppId -> etiqueta",
        bot._detectar_perimeterx() == "perimeterx",
    )

    # Selector: #px-captcha visible.
    class _DriverPXSelector(FakeDriver):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.elemento_px = _FakeElement(
                tag="div", attrs={"id": "px-captcha"}, visible=True
            )

        def find_elements(self, by, selector):
            if any(s in str(selector) for s in ("px-captcha", "px-cloud", "perimeterx")):
                return [self.elemento_px]
            return []

    bot.driver = _DriverPXSelector()
    check(
        "px: selector #px-captcha visible -> etiqueta",
        bot._detectar_perimeterx() == "#px-captcha",
    )

    # Texto visible (variante real de Change.org).
    class _DriverPXTexto(FakeDriver):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.body = _FakeElement(
                tag="body",
                texto="Solo para comprobar, no eres un bot de internet, verdad?",
            )

    bot.driver = _DriverPXTexto()
    check(
        "px: texto 'no eres un bot' -> etiqueta",
        bot._detectar_perimeterx() == "no eres un bot",
    )

    # Sin falsos positivos en una pagina normal y tolerancia a JS roto.
    bot.driver = FakeDriver()
    check("px: pagina normal -> ''", bot._detectar_perimeterx() == "")

    class _DriverRoto(FakeDriver):
        def execute_script(self, script, *args):
            raise RuntimeError("js roto")

    bot.driver = _DriverRoto()
    check("px: JS roto -> '' sin lanzar", bot._detectar_perimeterx() == "")

    bot.driver = object()  # sin API de Selenium
    check("px: driver raro -> '' sin lanzar", bot._detectar_perimeterx() == "")

    bot.driver = None
    check("px: sin driver -> ''", bot._detectar_perimeterx() == "")


def test_px_fast_skip(check):
    print("(23i) change_org: PerimeterX se SALTA CapSolver (sin gastar)")
    driver = FakeDriver(px="perimeterx", config={})
    avisos = []
    bot = ChangeOrgReportBot(cuenta={"usuario": "u1"}, aviso=avisos.append)
    bot.driver = driver
    bot.ultimo_error = "error previo"
    llamadas = {"turnstile": 0, "interstitial": 0, "http": 0}

    def _explota_turnstile(*a, **k):
        llamadas["turnstile"] += 1
        raise AssertionError("resolver_turnstile NO debe llamarse con PX")

    def _explota_interstitial(*a, **k):
        llamadas["interstitial"] += 1
        raise AssertionError("resolver_challenge_cloudflare NO debe llamarse con PX")

    def _explota_post(*a, **k):
        llamadas["http"] += 1
        raise AssertionError("no debe haber HTTP a CapSolver con PX")

    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", side_effect=_explota_turnstile
    ), mock.patch.object(
        cs, "resolver_challenge_cloudflare", side_effect=_explota_interstitial
    ), mock.patch.object(cs, "_post_json", side_effect=_explota_post):
        resuelto = bot._resolver_reto_captcha_api("no eres un bot")
    check(
        "px skip: devuelve False sin tocar CapSolver (0 llamadas)",
        resuelto is False
        and llamadas == {"turnstile": 0, "interstitial": 0, "http": 0},
        str(llamadas),
    )
    check(
        "px skip: UN solo aviso captcha_api 'fallo' con detalle accionable",
        len(avisos) == 1
        and avisos[0] == {
            "tipo": "captcha_api",
            "estado": "fallo",
            "metodo": "capsolver",
            "usuario": "u1",
            "email": "",
            "segundos": avisos[0].get("segundos"),
            "detalle": change_org._ERROR_PERIMETERX_API,
        }
        and isinstance(avisos[0]["segundos"], int)
        and "PerimeterX" in avisos[0]["detalle"]
        and "MODO ASISTIDO" in avisos[0]["detalle"],
        str(avisos),
    )
    check(
        "px skip: ultimo_error intacto y marca de sesion puesta",
        bot.ultimo_error == "error previo"
        and "no eres un bot" in bot._captcha_api_fallidos,
        f"error={bot.ultimo_error!r} fallidos={bot._captcha_api_fallidos}",
    )
    avisos.clear()
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs, "resolver_turnstile", side_effect=_explota_turnstile
    ), mock.patch.object(
        cs, "resolver_challenge_cloudflare", side_effect=_explota_interstitial
    ):
        resuelto2 = bot._resolver_reto_captcha_api("no eres un bot")
    check(
        "px skip: el segundo intento ni avisa (guard de sesion)",
        resuelto2 is False and avisos == [] and llamadas["interstitial"] == 0,
        f"avisos={avisos} llamadas={llamadas}",
    )

    # NO regresion: con sitekey Turnstile el fast-skip NO aplica aunque la
    # pagina tambien tenga senales PX.
    driver = FakeDriver(
        reto="turnstile",
        config={"sitekey": "0xSK-PX", "action": "login"},
        px="perimeterx",
    )
    avisos = []
    bot = ChangeOrgReportBot(aviso=avisos.append)
    bot.driver = driver
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        _fake_resolver_turnstile(registro, _resultado_token("TOK-PX")),
    ), mock.patch.object(
        cs,
        "resolver_challenge_cloudflare",
        side_effect=AssertionError("no debe ir al interstitial"),
    ):
        resuelto = bot._resolver_reto_captcha_api("turnstile")
    check(
        "px skip: con sitekey Turnstile sigue resolviendo (no regresion)",
        resuelto is True
        and len(registro) == 1
        and registro[0]["sitekey"] == "0xSK-PX"
        and driver.token_inyectado == "TOK-PX",
        f"resuelto={resuelto} registro={registro}",
    )
    check(
        "px skip: avisos iniciando -> resuelto en el camino Turnstile",
        [a["estado"] for a in avisos] == ["iniciando", "resuelto"],
        str(avisos),
    )


def _montar_bot_loop(driver, **kwargs):
    """Bot con el registro/login SIMULADO pero el solucionador REAL.

    Parchea los helpers de UI (navegacion, campos, escritura, evidencia) para
    que el bucle de `registrar_o_entrar` corra deterministicamente con el
    FakeDriver; `_detectar_reto_humano` lee directo `driver.reto` y la evidencia
    aparece al escribir la contrasena (marca `_evidencia_ok`).
    """
    cuenta = {
        "usuario": "u1",
        "email": "u1@x.com",
        "password": "claveSegura123",
        "nombre": "Ana",
        "apellido": "Lopez",
    }
    bot = ChangeOrgReportBot(cuenta=cuenta, **kwargs)
    bot.driver = driver
    bot.preparar_driver = lambda: True
    bot._restaurar_sesion_change = lambda: False
    bot._navegar = lambda url: ""
    bot._esperar_documento_listo = lambda *a, **k: True
    bot._cerrar_banner_cookies = lambda *a, **k: True
    bot._asegurar_campo_email = lambda *a, **k: _FakeElement()
    bot._pulsar_continuar = lambda intentos=10: True

    def _escribir(elemento, texto, **k):
        if str(texto) == "claveSegura123":
            bot._evidencia_ok = True
        return True

    bot.escribir_humano = _escribir
    bot._evidencia_sesion = (
        lambda email: "sesion ok" if getattr(bot, "_evidencia_ok", False) else ""
    )
    bot._detectar_captcha = lambda: ""
    bot._detectar_reto_humano = lambda: driver.reto
    bot._campo_password = lambda: _FakeElement()
    bot._detectar_error_sesion = lambda tardias=False: ""
    bot._pantalla_codigo_temporal = lambda: False
    bot._resolver_campo = lambda tipo: _FakeElement()
    return bot


def test_registrar_loop_captcha_api(check):
    print("(23f) change_org: registrar_o_entrar con CapSolver")
    # Con solucionador activo: el bucle supera el reto e inyecta el token.
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-LOOP"})
    avisos = []
    bot = _montar_bot_loop(driver, aviso=avisos.append)
    registro = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        _fake_resolver_turnstile(registro, _resultado_token("TOK-LOOP")),
    ):
        resultado = bot.registrar_o_entrar()
    check(
        "registro api: el reto se supera y el login termina ok",
        resultado["ok"] is True and resultado["estado"] == "existente",
        str(resultado),
    )
    check(
        "registro api: token inyectado en el formulario",
        driver.token_inyectado == "TOK-LOOP" and len(registro) == 1,
        f"token={driver.token_inyectado!r} intentos={len(registro)}",
    )
    check(
        "registro api: aviso captcha_api resuelto en el flujo",
        any(
            a["tipo"] == "captcha_api" and a["estado"] == "resuelto"
            for a in avisos
        ),
        str(avisos),
    )

    # Si el solucionador falla, cae al MODO ASISTIDO existente.
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-LOOP"})
    avisos = []
    bot = _montar_bot_loop(driver, aviso=avisos.append, esperar_captcha_seg=5)
    llamados_asistido = []

    def _asistido(reto=""):
        llamados_asistido.append(str(reto))
        driver.reto = ""
        return True

    bot._esperar_reto_humano = _asistido
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        _fake_resolver_turnstile([], _resultado_fallo("sin saldo")),
    ):
        resultado = bot.registrar_o_entrar()
    check(
        "registro api: fake que falla -> cae al modo asistido y sigue",
        resultado["ok"] is True and llamados_asistido == ["turnstile"],
        f"ok={resultado['ok']} asistido={llamados_asistido}",
    )
    check(
        "registro api: el reto se limpio en el modo asistido",
        driver.reto == "" and driver.token_inyectado == "",
    )

    # CHANGE_CAPTCHA_SOLVER=off (kwarg): NO se llama al solucionador.
    driver = FakeDriver(reto="turnstile", config={"sitekey": "SK-LOOP"})
    bot = _montar_bot_loop(driver, resolver_captcha="off")
    contador = {"n": 0}

    def _reto_off():
        contador["n"] += 1
        return "turnstile" if contador["n"] <= 1 else ""

    bot._detectar_reto_humano = _reto_off
    llamadas_resolver = []
    with mock.patch.object(cs, "disponible", return_value=True), mock.patch.object(
        cs,
        "resolver_turnstile",
        side_effect=lambda *a, **k: llamadas_resolver.append(1) or _resultado_token(),
    ):
        resultado = bot.registrar_o_entrar()
    check(
        "registro api: 'off' conserva el flujo clasico SIN llamar al solucionador",
        resultado["ok"] is True
        and llamadas_resolver == []
        and contador["n"] == 2,
        f"ok={resultado['ok']} llamadas={llamadas_resolver} detecciones={contador['n']}",
    )


class _BotFake:
    """Bot fake que captura TODOS los kwargs de su constructor."""

    creados: list = []

    def __init__(self, **kwargs):
        _BotFake.creados.append(dict(kwargs))
        self.ultimo_error = ""
        self.driver = None

    def preparar_driver(self):
        return True

    def reportar(self, identidad=None, queja=""):
        return {
            "ok": True,
            "email": str((identidad or {}).get("email") or ""),
            "nombre": "",
            "apellido": "",
            "queja": queja,
            "error": "",
            "evidencia": "ok",
            "url": "",
            "captura": "",
        }

    def registrar_o_entrar(self):
        return {
            "ok": True,
            "estado": "nueva",
            "error": "",
            "evidencia": "ok",
            "sesion_restaurada": False,
        }

    def _url_actual(self):
        return "https://www.change.org/"

    def cerrar(self):
        return None


def test_kwarg_funciones_publicas(check):
    print("(23g) change_org: kwarg resolver_captcha en las firmas publicas")
    funciones = (
        (ChangeOrgReportBot.__init__, "ChangeOrgReportBot.__init__"),
        (registrar_cuenta_change, "registrar_cuenta_change"),
        (ejecutar_campana_registros, "ejecutar_campana_registros"),
        (ejecutar_un_reporte, "ejecutar_un_reporte"),
        (ejecutar_campana_reportes, "ejecutar_campana_reportes"),
    )
    for funcion, nombre in funciones:
        parametros = list(inspect.signature(funcion).parameters)
        check(
            f"firma: {nombre} termina en resolver_captcha",
            parametros[-1] == "resolver_captcha",
            str(parametros[-3:]),
        )

    _BotFake.creados = []
    with mock.patch.object(change_org, "ChangeOrgReportBot", _BotFake), mock.patch.object(
        gc,
        "generar_queja_change",
        lambda contexto, evitar=None, variante=None: {
            "ok": True,
            "queja": "queja de prueba",
            "usada_ia": False,
            "error": "",
        },
    ):
        resultado = ejecutar_un_reporte(
            "https://www.change.org/p/test", guardar_identidad=False, resolver_captcha="off"
        )
        check(
            "propagacion: ejecutar_un_reporte pasa resolver_captcha al bot",
            resultado.get("ok") is True
            and _BotFake.creados[-1].get("resolver_captcha") == "off",
            str(_BotFake.creados[-1].get("resolver_captcha")),
        )
        resultado = ejecutar_un_reporte(
            "https://www.change.org/p/test", guardar_identidad=False
        )
        check(
            "propagacion: ejecutar_un_reporte sin kwarg -> None (env decide)",
            _BotFake.creados[-1].get("resolver_captcha") is None,
            str(_BotFake.creados[-1].get("resolver_captcha")),
        )

        resultado = registrar_cuenta_change(
            "u1", "u1@x.com", "clave12345", resolver_captcha="off"
        )
        check(
            "propagacion: registrar_cuenta_change pasa resolver_captcha al bot",
            resultado.get("ok") is True
            and _BotFake.creados[-1].get("resolver_captcha") == "off",
            str(resultado),
        )

    capturados = []

    def _registrar_fake(**kwargs):
        capturados.append(dict(kwargs))
        return {
            "ok": True,
            "usuario": kwargs.get("usuario", ""),
            "email": kwargs.get("email", ""),
            "estado": "nueva",
            "error": "",
        }

    with mock.patch.object(
        change_org, "registrar_cuenta_change", side_effect=_registrar_fake
    ):
        resumen = ejecutar_campana_registros(
            [{"usuario": "u1", "email": "u1@x.com", "password": "clave12345"}],
            max_workers=1,
            usar_proxies=False,
            resolver_captcha="off",
        )
    check(
        "propagacion: ejecutar_campana_registros reenvia el kwarg",
        resumen["exitosos"] == 1
        and capturados
        and capturados[0].get("resolver_captcha") == "off",
        str(capturados[:1]),
    )

    capturados = []

    def _reporte_fake(**kwargs):
        capturados.append(dict(kwargs))
        return {
            "ok": True,
            "email": "e@x.com",
            "identidad": {},
            "identidad_guardada": False,
            "queja": "",
        }

    with mock.patch.object(
        change_org, "ejecutar_un_reporte", side_effect=_reporte_fake
    ):
        resumen = ejecutar_campana_reportes(
            "https://www.change.org/p/test",
            cantidad=1,
            max_workers=1,
            usar_proxies=False,
            resolver_captcha="off",
        )
        check(
            "propagacion: ejecutar_campana_reportes reenvia el kwarg",
            resumen["enviados"] == 1
            and capturados
            and capturados[0].get("resolver_captcha") == "off",
            str(capturados[:1]),
        )
        # Firma vieja POSICIONAL (11 args) sigue funcionando.
        capturados = []
        resumen = ejecutar_campana_reportes(
            "https://www.change.org/p/test", "", 1, 1, False, "", False, None, None, None, None
        )
        check(
            "compatibilidad: firma vieja posicional intacta",
            resumen["total"] == 1
            and capturados
            and capturados[0].get("resolver_captcha") is None,
            str(resumen.get("total")),
        )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local).

    Aisla el entorno: `CAPSOLVER_API_KEY`/`CHANGE_CAPTCHA_SOLVER` se restauran,
    el `.env` real NUNCA se lee (`_ENV_FILE` -> temporal inexistente) y
    `time.sleep` se neutraliza.
    """
    with tempfile.TemporaryDirectory(prefix="captcha_api_") as tmp, \
            mock.patch.dict(os.environ), \
            mock.patch.object(cs, "_ENV_FILE", Path(tmp) / "no_existe.env"), \
            _sin_esperas():
        os.environ.pop("CAPSOLVER_API_KEY", None)
        os.environ.pop("CHANGE_CAPTCHA_SOLVER", None)
        test_solver_config(check)
        test_solver_turnstile_ok(check)
        test_solver_turnstile_errores(check)
        test_solver_challenge_cloudflare(check)
        test_solver_no_loguea_key(check)
        test_modo_captcha(check)
        test_extraer_e_inyectar(check)
        test_aviso_captcha_api(check)
        test_resolver_api_turnstile(check)
        test_resolver_api_interstitial(check)
        test_detectar_perimeterx(check)
        test_px_fast_skip(check)
        test_registrar_loop_captcha_api(check)
        test_kwarg_funciones_publicas(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_change_captcha_api.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
