"""Tests del cliente backend de Just Another Panel (``integraciones.jap``).

Sin red real y sin tocar ``data/``: cada prueba usa un directorio temporal y
parchea ``requests.post`` con respuestas falsas. Las claves usadas son
SINTETICAS (no son secretos reales).

Cubre: credenciales (settings -> env -> JSON), guardado atomico, balance,
catalogo con cache memoria/disco y fallback, clasificacion X/Twitter, filtros,
resumen, costos min/max, presets verificados con fallback, creacion de ordenes
(URL, cantidad, custom comments, saldo), estado/refill/cancel y la garantia de
que la API key NUNCA aparece en errores, logs ni stdout/stderr.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_jap_api.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import requests  # noqa: E402

from integraciones import jap  # noqa: E402

# Clave SINTETICA de prueba (no es una key real).
CLAVE_FALSA = "jap_test_key_no_real_0123456789"


# ---------------------------------------------------------------------------
# Infraestructura falsa (sin red)
# ---------------------------------------------------------------------------
class _Respuesta:
    """Respuesta HTTP falsa compatible con lo que usa ``jap._peticion``."""

    def __init__(self, cuerpo, status_code=200, texto=None):
        self._cuerpo = cuerpo
        self.status_code = status_code
        if texto is not None:
            self.text = texto
        else:
            try:
                self.text = json.dumps(cuerpo)
            except (TypeError, ValueError):
                self.text = str(cuerpo)

    def json(self):
        if isinstance(self._cuerpo, BaseException):
            raise self._cuerpo
        return self._cuerpo


class _PostFalso:
    """Sustituto de ``requests.post``: consume respuestas y registra llamadas."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def __call__(self, url, data=None, timeout=None, **kwargs):
        self.llamadas.append(
            {"url": url, "data": dict(data or {}), "timeout": timeout}
        )
        if not self.respuestas:
            raise AssertionError("llamada HTTP no esperada en el test")
        elemento = self.respuestas.pop(0)
        if isinstance(elemento, BaseException):
            raise elemento
        if isinstance(elemento, _Respuesta):
            return elemento
        return _Respuesta(elemento)


def _servicio(
    servicio_id,
    categoria,
    nombre,
    rate,
    minimo=10,
    maximo=10000,
    tipo="Default",
    refill=False,
    cancel=True,
):
    return {
        "service": servicio_id,
        "name": nombre,
        "type": tipo,
        "category": categoria,
        "rate": str(rate),
        "min": str(minimo),
        "max": str(maximo),
        "refill": refill,
        "cancel": cancel,
    }


def _catalogo():
    """Catalogo simulado con los 10 ids de los presets + extras por tipo."""
    return [
        _servicio(9013, "Twitter Likes", "Likes 7D", 0.275, 10, 2500),
        _servicio(10186, "Twitter Likes", "Likes Refill 15D", 1.80, 10, 40000),
        _servicio(8860, "Twitter Retweets", "Retweets", 0.125, 10, 10000),
        _servicio(8861, "Twitter Retweets", "Retweets Refill 30D", 0.50),
        _servicio(8413, "Twitter Views", "Tweet Views", 0.0025, 10, 1000000),
        _servicio(8555, "Twitter Views", "Tweet Views 365D", 0.0063),
        _servicio(
            6601,
            "Twitter Comments",
            "USA Random Comments",
            30.875,
            5,
            150,
        ),
        _servicio(
            6602,
            "Twitter Comments",
            "USA Custom Comments",
            37.05,
            5,
            150,
            tipo="Custom Comments",
        ),
        _servicio(9011, "Twitter Followers", "Followers", 0.375),
        _servicio(8855, "Twitter Followers", "USA Followers Refill 30D", 0.85),
        _servicio(1000, "Twitter Impressions", "Impressions", 0.01),
        _servicio(1001, "Twitter Poll Votes", "Poll Votes", 5.0, 10, 1000),
        _servicio(1002, "Twitter Live", "Spaces (Live)", 8.0, 1, 100),
        _servicio(1003, "Twitter Packages", "Package 1K", 2.0, 1, 1000),
        _servicio(1004, "Twitter Mentions", "Mentions", 1.5, 10, 5000),
        _servicio(2000, "Instagram Likes", "Likes", 0.10),
        _servicio(2001, "Facebook Followers", "Followers", 0.20),
        _servicio(2002, "TikTok Views", "Views", 0.05),
    ]


def _cachear(servicios):
    """Coloca un catalogo en la cache en memoria (sin red ni disco)."""
    with jap._CACHE_BLOQUEO:
        jap._CACHE_MEMORIA["servicios"] = list(servicios)
        jap._CACHE_MEMORIA["timestamp"] = time.time()


@contextlib.contextmanager
def _entorno():
    """Temp dir aislado + entorno de JAP controlado (nunca toca data/)."""
    with tempfile.TemporaryDirectory() as temporal:
        base = Path(temporal)
        jap._limpiar_cache()
        with mock.patch.object(jap, "_api_key_settings", return_value=""), \
                mock.patch.object(
                    jap, "_ruta_config", return_value=str(base / "jap_config.json")
                ), \
                mock.patch.object(
                    jap,
                    "_ruta_cache",
                    return_value=str(base / "jap_services_cache.json"),
                ), \
                mock.patch.dict(
                    os.environ,
                    {
                        "JAP_API_KEY": CLAVE_FALSA,
                        "JAP_TIMEOUT_SEG": "30",
                        "JAP_CACHE_SEG": "3600",
                    },
                ):
            yield base


# ---------------------------------------------------------------------------
# Credenciales
# ---------------------------------------------------------------------------
def test_api_key(check):
    with _entorno() as base:
        ruta = base / "jap_config.json"
        ruta.write_text(json.dumps({"api_key": "clave-json"}), encoding="utf-8")

        with mock.patch.object(
            jap, "_api_key_settings", return_value="clave-settings"
        ):
            with mock.patch.dict(os.environ, {"JAP_API_KEY": "clave-env"}):
                check(
                    "api_key: settings gana sobre env y JSON",
                    jap.api_key() == "clave-settings",
                )

        with mock.patch.object(jap, "_api_key_settings", return_value=""):
            with mock.patch.dict(os.environ, {"JAP_API_KEY": "clave-env"}):
                check(
                    "api_key: env gana sobre el JSON",
                    jap.api_key() == "clave-env",
                )
            with mock.patch.dict(os.environ, {"JAP_API_KEY": ""}):
                check(
                    "api_key: usa el JSON si no hay settings ni env",
                    jap.api_key() == "clave-json",
                )

        ruta.unlink()
        with mock.patch.object(jap, "_api_key_settings", return_value=""):
            with mock.patch.dict(os.environ, {"JAP_API_KEY": ""}):
                check(
                    "api_key: sin ninguna fuente devuelve cadena vacia",
                    jap.api_key() == "",
                )


def test_guardar_api_key(check):
    with _entorno() as base:
        ruta = base / "jap_config.json"
        with mock.patch.object(jap, "_api_key_settings", return_value=""):
            with mock.patch.dict(os.environ, {"JAP_API_KEY": ""}):
                resultado = jap.guardar_api_key(CLAVE_FALSA)
                check(
                    "guardar_api_key: devuelve ok True",
                    resultado == {"ok": True, "error": ""},
                )
                check(
                    "guardar_api_key: escribe el JSON con la key",
                    json.loads(ruta.read_text(encoding="utf-8"))
                    == {"api_key": CLAVE_FALSA},
                )
                check(
                    "guardar_api_key: api_key() lee el JSON guardado",
                    jap.api_key() == CLAVE_FALSA,
                )

        check(
            "guardar_api_key: no deja archivos temporales",
            not list(base.glob(".jap_config_*")),
        )
        if os.name != "nt":
            check(
                "guardar_api_key: permisos 0600 en POSIX",
                stat.S_IMODE(ruta.stat().st_mode) == 0o600,
            )

        check(
            "guardar_api_key: rechaza una key vacia",
            jap.guardar_api_key("   ")["ok"] is False,
        )

        with mock.patch.object(
            jap.os, "replace", side_effect=OSError("sin permiso")
        ):
            fallo = jap.guardar_api_key("otra-clave-sintetica")
        check(
            "guardar_api_key: error de escritura devuelve ok False",
            fallo["ok"] is False,
        )
        check(
            "guardar_api_key: el error no incluye la key",
            "otra-clave-sintetica" not in fallo["error"],
        )
        check(
            "guardar_api_key: limpia temporales tras el error",
            not list(base.glob(".jap_config_*")),
        )


def test_disponible(check):
    with _entorno():
        with mock.patch.object(jap, "_api_key_settings", return_value=""):
            with mock.patch.dict(os.environ, {"JAP_API_KEY": ""}):
                check("disponible: False sin key", jap.disponible() is False)
            with mock.patch.dict(os.environ, {"JAP_API_KEY": CLAVE_FALSA}):
                check("disponible: True con key", jap.disponible() is True)


# ---------------------------------------------------------------------------
# Endpoints de lectura
# ---------------------------------------------------------------------------
def test_balance(check):
    with _entorno():
        post = _PostFalso({"balance": "3.5098125", "currency": "USD"})
        with mock.patch.object(jap.requests, "post", post):
            resultado = jap.obtener_balance()
        check("balance: ok True", resultado["ok"] is True)
        check(
            "balance: convierte el saldo a float",
            resultado["balance"] == 3.5098125,
        )
        check("balance: devuelve la moneda", resultado["currency"] == "USD")
        check(
            "balance: envia action=balance",
            post.llamadas[0]["data"].get("action") == "balance",
        )
        check(
            "balance: incluye la key en el POST",
            "key" in post.llamadas[0]["data"],
        )

        post = _PostFalso({"error": "Invalid API key"})
        with mock.patch.object(jap.requests, "post", post):
            fallo = jap.obtener_balance()
        check("balance: error de JAP -> ok False", fallo["ok"] is False)
        check(
            "balance: propaga el mensaje de JAP",
            "Invalid API key" in fallo["error"],
        )

        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            red = jap.obtener_balance()
        check("balance: fallo de red -> ok False", red["ok"] is False)
        check(
            "balance: el error de red esta en espanol",
            "Error de red" in red["error"],
        )

        post = _PostFalso(_Respuesta(ValueError("sin json"), texto="<html>"))
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.obtener_balance()
        check("balance: JSON invalido -> ok False", invalido["ok"] is False)
        check(
            "balance: detecta respuesta no JSON",
            "no es JSON" in invalido["error"],
        )

        post = _PostFalso({"balance": None, "currency": "USD"})
        with mock.patch.object(jap.requests, "post", post):
            sin_saldo = jap.obtener_balance()
        check("balance: saldo ausente -> ok False", sin_saldo["ok"] is False)


def test_cache_servicios(check):
    with _entorno() as base:
        catalogo = _catalogo()
        post = _PostFalso(catalogo, catalogo, catalogo)
        with mock.patch.object(jap.requests, "post", post):
            primera = jap.listar_servicios()
            check("catalogo: primera llamada ok", primera["ok"] is True)
            check(
                "catalogo: primera llamada viene de la red",
                primera["desde_cache"] is False,
            )
            check(
                "catalogo: devuelve todos los servicios",
                len(primera["servicios"]) == len(catalogo),
            )
            check(
                "catalogo: crea la cache en disco",
                (base / "jap_services_cache.json").is_file(),
            )
            check(
                "catalogo: envia action=services",
                post.llamadas[0]["data"].get("action") == "services",
            )

            segunda = jap.listar_servicios()
            check(
                "catalogo: segunda llamada usa la cache en memoria",
                segunda["ok"] is True and segunda["desde_cache"] is True,
            )
            check(
                "catalogo: la cache evita la segunda peticion",
                len(post.llamadas) == 1,
            )

            forzada = jap.listar_servicios(forzar=True)
            check(
                "catalogo: forzar=True vuelve a la red",
                forzada["desde_cache"] is False and len(post.llamadas) == 2,
            )

        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            respaldo = jap.listar_servicios(forzar=True)
        check(
            "catalogo: fallo de red usa la cache y devuelve ok True",
            respaldo["ok"] is True and respaldo["desde_cache"] is True,
        )
        check(
            "catalogo: fallo de red informa que uso la cache",
            "cache" in respaldo["error"].lower(),
        )

        jap._limpiar_cache()
        (base / "jap_services_cache.json").write_text(
            json.dumps({"timestamp": time.time(), "servicios": catalogo[:2]}),
            encoding="utf-8",
        )
        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            desde_disco = jap.listar_servicios()
        check(
            "catalogo: TTL vigente lee la cache de disco sin red",
            desde_disco["ok"] is True
            and desde_disco["desde_cache"] is True
            and len(post.llamadas) == 0,
        )
        check(
            "catalogo: la cache de disco trae los servicios guardados",
            len(desde_disco["servicios"]) == 2,
        )

        jap._limpiar_cache()
        (base / "jap_services_cache.json").unlink()
        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            sin_cache = jap.listar_servicios()
        check(
            "catalogo: sin cache y sin red devuelve ok False",
            sin_cache["ok"] is False,
        )
        check(
            "catalogo: sin cache devuelve lista vacia",
            sin_cache["servicios"] == [],
        )
        check(
            "catalogo: sin cache explica el error",
            bool(sin_cache["error"]),
        )

        jap._limpiar_cache()
        post = _PostFalso(catalogo, catalogo)
        with mock.patch.dict(os.environ, {"JAP_CACHE_SEG": "0"}):
            with mock.patch.object(jap.requests, "post", post):
                primera = jap.listar_servicios()
                segunda = jap.listar_servicios()
        check(
            "catalogo: JAP_CACHE_SEG=0 no usa cache fresca",
            primera["desde_cache"] is False
            and segunda["desde_cache"] is False
            and len(post.llamadas) == 2,
        )

        jap._limpiar_cache()
        (base / "jap_services_cache.json").unlink()
        post = _PostFalso(catalogo)
        with mock.patch.dict(os.environ, {"JAP_CACHE_SEG": "no-numero"}):
            with mock.patch.object(jap.requests, "post", post):
                jap.listar_servicios()
                jap.listar_servicios()
        check(
            "catalogo: JAP_CACHE_SEG invalido cae al default 3600",
            len(post.llamadas) == 1,
        )


def test_clasificar_servicio(check):
    casos = [
        ("Twitter Likes", "Likes 7D", "likes"),
        ("Twitter Likes", "Likes Refill 15D", "likes"),
        ("Twitter Retweets", "Retweets", "retweets"),
        ("Twitter Retweets", "Retweets Refill 30D", "retweets"),
        ("Twitter Retweets", "Quote Retweets", "retweets"),
        ("Twitter Views", "Tweet Views 365D", "views"),
        ("Twitter Impressions", "Impressions", "impressions"),
        ("Twitter Comments", "USA Random Comments", "comments"),
        ("Twitter Comments", "USA Custom Comments", "comments"),
        ("Twitter Followers", "Followers", "followers"),
        ("Twitter Followers", "USA Followers Refill 30D", "followers"),
        ("Twitter Mentions", "Mentions", "mentions"),
        ("Twitter Poll Votes", "Poll Votes", "poll"),
        ("Twitter Live", "Spaces (Live)", "live"),
        ("Twitter Packages", "Package 1K", "packages"),
        ("Instagram Likes", "Likes", "likes"),
        ("Facebook Followers", "Followers", "followers"),
    ]
    for categoria, nombre, esperado in casos:
        resultado = jap.clasificar_servicio(
            {"category": categoria, "name": nombre}
        )
        check(
            f"clasificar: {categoria} / {nombre} -> {esperado}",
            resultado == esperado,
            f"(obtenido: {resultado})",
        )

    check("clasificar: dict vacio -> other", jap.clasificar_servicio({}) == "other")
    check(
        "clasificar: valor no dict -> other",
        jap.clasificar_servicio(None) == "other",
    )
    check(
        "clasificar: categoria desconocida -> other",
        jap.clasificar_servicio({"category": "Otros", "name": "Servicio"})
        == "other",
    )


def test_es_servicio_x(check):
    check(
        "es_servicio_x: categoria Twitter -> True",
        jap.es_servicio_x({"category": "Twitter Likes", "name": "Likes"})
        is True,
    )
    check(
        "es_servicio_x: nombre con Twitter -> True",
        jap.es_servicio_x({"category": "Otros", "name": "Twitter Likes"})
        is True,
    )
    check(
        "es_servicio_x: categoria X -> True",
        jap.es_servicio_x({"category": "X Likes", "name": "Likes"}) is True,
    )
    check(
        "es_servicio_x: Instagram -> False",
        jap.es_servicio_x({"category": "Instagram Likes", "name": "Likes"})
        is False,
    )
    check(
        "es_servicio_x: 'Extra' no cuenta como X",
        jap.es_servicio_x({"category": "Extra Likes", "name": "Likes"}) is False,
    )
    check("es_servicio_x: None -> False", jap.es_servicio_x(None) is False)


def test_filtrar_servicios(check):
    with _entorno():
        catalogo = _catalogo()
        _cachear(catalogo)

        todos_x = jap.filtrar_servicios()
        check("filtrar: ok True", todos_x["ok"] is True)
        check(
            "filtrar: por defecto solo devuelve servicios X",
            all(jap.es_servicio_x(s) for s in todos_x["servicios"]),
        )
        check(
            "filtrar: cuenta correcta de servicios X",
            len(todos_x["servicios"])
            == len([s for s in catalogo if jap.es_servicio_x(s)]),
        )

        todas = jap.filtrar_servicios(plataforma="")
        check(
            "filtrar: plataforma vacia devuelve todo el catalogo",
            len(todas["servicios"]) == len(catalogo),
        )

        twitter = jap.filtrar_servicios(plataforma="twitter")
        check(
            "filtrar: 'twitter' se comporta como 'x'",
            len(twitter["servicios"]) == len(todos_x["servicios"]),
        )

        likes_x = jap.filtrar_servicios(tipo="likes")
        ids = {s["service"] for s in likes_x["servicios"]}
        check(
            "filtrar: tipo likes en X devuelve solo 9013 y 10186",
            ids == {9013, 10186},
            f"({sorted(ids)})",
        )

        refill = jap.filtrar_servicios(texto="refill")
        ids = {s["service"] for s in refill["servicios"]}
        check(
            "filtrar: texto 'refill' encuentra los servicios con refill",
            {8861, 10186, 8855}.issubset(ids),
            f"({sorted(ids)})",
        )

        por_id = jap.filtrar_servicios(plataforma="", texto="2000")
        check(
            "filtrar: el texto tambien busca por id",
            [s["service"] for s in por_id["servicios"]] == [2000],
        )

        instagram = jap.filtrar_servicios(plataforma="instagram")
        check(
            "filtrar: plataforma instagram solo devuelve Instagram",
            [s["service"] for s in instagram["servicios"]] == [2000],
        )

        inexistente = jap.filtrar_servicios(tipo="inexistente")
        check(
            "filtrar: tipo inexistente devuelve lista vacia",
            inexistente["ok"] is True and inexistente["servicios"] == [],
        )

    with _entorno():
        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            sin_catalogo = jap.filtrar_servicios()
        check(
            "filtrar: sin catalogo -> ok False",
            sin_catalogo["ok"] is False,
        )
        check(
            "filtrar: sin catalogo devuelve lista vacia",
            sin_catalogo["servicios"] == [],
        )


def test_resumen_catalogo(check):
    with _entorno():
        catalogo = _catalogo()
        _cachear(catalogo)
        resumen = jap.resumen_catalogo()
        check("resumen: ok True", resumen["ok"] is True)
        check("resumen: total correcto", resumen["total"] == len(catalogo))
        check(
            "resumen: cuenta Twitter en plataformas",
            resumen["plataformas"].get("twitter")
            == len([s for s in catalogo if jap.es_servicio_x(s)]),
        )
        check(
            "resumen: cuenta Instagram",
            resumen["plataformas"].get("instagram") == 1,
        )
        check(
            "resumen: cuenta Facebook",
            resumen["plataformas"].get("facebook") == 1,
        )
        check(
            "resumen: cuenta TikTok",
            resumen["plataformas"].get("tiktok") == 1,
        )
        check(
            "resumen: tipos cuenta todos los likes (X + Instagram)",
            resumen["tipos"].get("likes") == 3,
        )
        check(
            "resumen: tipos_x solo cuenta likes de X",
            resumen["tipos_x"].get("likes") == 2,
        )
        check(
            "resumen: expone alias por_plataforma/por_tipo",
            resumen["por_plataforma"] == resumen["plataformas"]
            and resumen["por_tipo"] == resumen["tipos"],
        )
        check(
            "resumen: total_x coincide con la suma de tipos_x",
            resumen["total_x"] == sum(resumen["tipos_x"].values()),
        )

    with _entorno():
        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            fallo = jap.resumen_catalogo()
        check("resumen: sin catalogo -> ok False", fallo["ok"] is False)
        check("resumen: sin catalogo total 0", fallo["total"] == 0)


# ---------------------------------------------------------------------------
# Costos y presets
# ---------------------------------------------------------------------------
def test_calcular_costo(check):
    servicio = _servicio(9013, "Twitter Likes", "Likes 7D", 0.275, 10, 2500)

    resultado = jap.calcular_costo(servicio, 1000)
    check("costo: ok True con dict", resultado["ok"] is True)
    check(
        "costo: rate * cantidad / 1000",
        resultado["costo"] == 0.275,
        f"({resultado['costo']})",
    )
    check("costo: servicio_id", resultado["servicio_id"] == 9013)
    check("costo: cantidad", resultado["cantidad"] == 1000)
    check("costo: moneda USD", resultado["moneda"] == "USD")
    check("costo: min", resultado["min"] == 10)
    check("costo: max", resultado["max"] == 2500)

    en_minimo = jap.calcular_costo(servicio, 10)
    check(
        "costo: cantidad en el minimo es valida",
        en_minimo["ok"] is True,
    )

    debajo = jap.calcular_costo(servicio, 9)
    check("costo: cantidad bajo el minimo -> ok False", debajo["ok"] is False)
    check(
        "costo: error de minimo en espanol",
        "minima" in debajo["error"],
    )

    encima = jap.calcular_costo(servicio, 2501)
    check("costo: cantidad sobre el maximo -> ok False", encima["ok"] is False)
    check("costo: error de maximo en espanol", "maxima" in encima["error"])

    check(
        "costo: cantidad 0 -> ok False",
        jap.calcular_costo(servicio, 0)["ok"] is False,
    )
    check(
        "costo: cantidad decimal -> ok False",
        jap.calcular_costo(servicio, 1000.5)["ok"] is False,
    )
    check(
        "costo: cantidad como string entero -> ok True",
        jap.calcular_costo(servicio, "1000")["ok"] is True,
    )
    check(
        "costo: servicio invalido -> ok False",
        jap.calcular_costo("no-es-id", 1000)["ok"] is False,
    )
    check(
        "costo: servicio sin tarifa -> ok False",
        jap.calcular_costo({"service": 1, "min": 1, "max": 10}, 5)["ok"]
        is False,
    )

    comentarios = _servicio(
        6601, "Twitter Comments", "USA Random Comments", 30.875, 5, 150
    )
    costo_comentarios = jap.calcular_costo(comentarios, 5)
    check(
        "costo: redondea a 6 decimales",
        costo_comentarios["costo"] == 0.154375,
        f"({costo_comentarios['costo']})",
    )

    with _entorno():
        catalogo = _catalogo()
        _cachear(catalogo)
        por_id = jap.calcular_costo(8860, 1000)
        check(
            "costo: acepta el id y busca en el catalogo",
            por_id["ok"] is True
            and por_id["servicio_id"] == 8860
            and por_id["costo"] == 0.125,
        )
        desconocido = jap.calcular_costo(999999, 1000)
        check(
            "costo: id inexistente -> ok False",
            desconocido["ok"] is False,
        )
        check(
            "costo: id inexistente explica el motivo",
            "No existe el servicio" in desconocido["error"],
        )


def test_servicios_recomendados(check):
    with _entorno():
        _cachear(_catalogo())
        resultado = jap.servicios_recomendados()
        check("presets: ok True", resultado["ok"] is True)
        check(
            "presets: incluye economico y seguro",
            set(resultado["presets"].keys()) == {"economico", "seguro"},
        )

        esperados = {
            "economico": {
                "likes": 9013,
                "retweets": 8860,
                "views": 8413,
                "comments": 6601,
                "followers": 9011,
            },
            "seguro": {
                "likes": 10186,
                "retweets": 8861,
                "views": 8555,
                "comments": 6602,
                "followers": 8855,
            },
        }
        for preset, roles in esperados.items():
            for rol, servicio_id in roles.items():
                item = resultado["presets"][preset].get(rol)
                check(
                    f"presets: {preset}/{rol} conserva el id {servicio_id}",
                    bool(item)
                    and item["servicio"]["service"] == servicio_id,
                )
        item = resultado["presets"]["economico"]["likes"]
        check(
            "presets: cada rol trae servicio/rate/min/max/nombre",
            set(item.keys())
            == {"servicio", "rate", "min", "max", "nombre"},
        )
        check(
            "presets: rate es float",
            isinstance(item["rate"], float) and item["rate"] == 0.275,
        )
        check("presets: min/max son enteros", item["min"] == 10 and item["max"] == 2500)
        check(
            "presets: nombre del servicio",
            item["nombre"] == "Likes 7D",
        )

        # Fallback: ningun id preset existe, se elige por categoria/nombre.
        jap._limpiar_cache()
        fallback = [
            _servicio(500, "Twitter Likes", "Likes 7D", 0.30),
            _servicio(501, "Twitter Likes", "Likes Refill 15D", 2.00),
            _servicio(510, "Twitter Retweets", "Retweets", 0.20),
            _servicio(511, "Twitter Retweets", "Retweets Refill 30D", 0.60),
            _servicio(520, "Twitter Views", "Tweet Views", 0.01),
            _servicio(521, "Twitter Views", "Tweet Views 365D", 0.02),
            _servicio(
                530, "Twitter Comments", "USA Random Comments", 20.0, 5, 150
            ),
            _servicio(
                531,
                "Twitter Comments",
                "USA Custom Comments",
                40.0,
                5,
                150,
                tipo="Custom Comments",
            ),
            _servicio(540, "Twitter Followers", "Followers", 0.40),
            _servicio(541, "Twitter Followers", "USA Followers Refill 30D", 0.90),
        ]
        _cachear(fallback)
        resultado = jap.servicios_recomendados()
        esperados_fallback = {
            "economico": {
                "likes": 500,
                "retweets": 510,
                "views": 520,
                "comments": 530,
                "followers": 540,
            },
            "seguro": {
                "likes": 501,
                "retweets": 511,
                "views": 521,
                "comments": 531,
                "followers": 541,
            },
        }
        for preset, roles in esperados_fallback.items():
            for rol, servicio_id in roles.items():
                item = resultado["presets"][preset].get(rol)
                check(
                    f"presets fallback: {preset}/{rol} elige {servicio_id}",
                    bool(item)
                    and item["servicio"]["service"] == servicio_id,
                    f"({item['servicio']['service'] if item else None})",
                )

    with _entorno():
        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            sin_catalogo = jap.servicios_recomendados()
        check("presets: sin catalogo -> ok False", sin_catalogo["ok"] is False)
        check(
            "presets: sin catalogo devuelve presets vacios",
            sin_catalogo["presets"] == {"economico": {}, "seguro": {}},
        )


# ---------------------------------------------------------------------------
# Ordenes
# ---------------------------------------------------------------------------
def test_crear_orden(check):
    servicio = _servicio(9013, "Twitter Likes", "Likes 7D", 0.275, 10, 2500)
    url = "https://x.com/usuario/status/123456789"

    with _entorno():
        post = _PostFalso(
            {"balance": "3.5098125", "currency": "USD"}, {"order": 555}
        )
        with mock.patch.object(jap.requests, "post", post):
            resultado = jap.crear_orden(servicio, url, 1000)
        check("orden: ok True", resultado["ok"] is True)
        check("orden: devuelve el numero de orden", resultado["order"] == 555)
        check("orden: devuelve el costo", resultado["costo"] == 0.275)
        check("orden: consulta el saldo antes", len(post.llamadas) == 2)
        check(
            "orden: la primera peticion es balance",
            post.llamadas[0]["data"].get("action") == "balance",
        )
        datos = post.llamadas[1]["data"]
        check("orden: envia action=add", datos.get("action") == "add")
        check("orden: envia el servicio", datos.get("service") == "9013")
        check("orden: envia el link", datos.get("link") == url)
        check("orden: envia la cantidad", datos.get("quantity") == "1000")
        check("orden: incluye la key en el POST", "key" in datos)
        check(
            "orden: no envia comments en un servicio normal",
            "comments" not in datos,
        )
        check(
            "orden: el resultado no contiene la key",
            CLAVE_FALSA not in json.dumps(resultado),
        )

    with _entorno():
        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalida = jap.crear_orden(servicio, "x.com/sin-esquema", 1000)
        check("orden: URL sin http(s) -> ok False", invalida["ok"] is False)
        check(
            "orden: URL invalida no hace peticiones",
            len(post.llamadas) == 0,
        )

        with mock.patch.object(jap.requests, "post", post):
            ftp = jap.crear_orden(servicio, "ftp://x.com/a", 1000)
        check("orden: esquema ftp -> ok False", ftp["ok"] is False)

        with mock.patch.object(jap.requests, "post", post):
            pocos = jap.crear_orden(servicio, url, 9)
        check("orden: cantidad debajo del minimo -> ok False", pocos["ok"] is False)
        check(
            "orden: cantidad invalida no consulta saldo",
            len(post.llamadas) == 0,
        )

        with mock.patch.object(jap.requests, "post", post):
            muchos = jap.crear_orden(servicio, url, 2501)
        check("orden: cantidad sobre el maximo -> ok False", muchos["ok"] is False)

        with mock.patch.object(jap.requests, "post", post):
            decimal = jap.crear_orden(servicio, url, 1000.5)
        check("orden: cantidad decimal -> ok False", decimal["ok"] is False)

        sin_id = {
            "name": "Likes sin id",
            "category": "Twitter Likes",
            "rate": "0.1",
            "min": "1",
            "max": "10",
        }
        with mock.patch.object(jap.requests, "post", post):
            sin_servicio_id = jap.crear_orden(sin_id, url, 5)
        check(
            "orden: servicio dict sin id -> ok False",
            sin_servicio_id["ok"] is False,
        )
        check(
            "orden: servicio dict sin id explica el motivo",
            "id valido" in sin_servicio_id["error"],
        )
        check(
            "orden: servicio dict sin id no hace peticiones",
            len(post.llamadas) == 0,
        )

    custom = _servicio(
        6602,
        "Twitter Comments",
        "USA Custom Comments",
        37.05,
        5,
        150,
        tipo="Custom Comments",
    )
    with _entorno():
        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            sin_comentarios = jap.crear_orden(custom, url, 5)
        check(
            "orden custom: comments vacio -> ok False",
            sin_comentarios["ok"] is False,
        )
        check(
            "orden custom: el error menciona comentarios personalizados",
            "personalizados" in sin_comentarios["error"],
        )
        check(
            "orden custom: no consulta saldo si faltan comentarios",
            len(post.llamadas) == 0,
        )

        with mock.patch.object(jap.requests, "post", post):
            cortos = jap.crear_orden(custom, url, 5, comments="uno\ndos")
        check(
            "orden custom: menos lineas que cantidad -> ok False",
            cortos["ok"] is False,
        )
        check(
            "orden custom: explica cuantas lineas faltan",
            "Faltan comentarios" in cortos["error"],
        )

        post = _PostFalso(
            {"balance": "100", "currency": "USD"}, {"order": 777}
        )
        with mock.patch.object(jap.requests, "post", post):
            correcta = jap.crear_orden(
                custom,
                url,
                5,
                comments="uno\ndos\ntres\ncuatro\ncinco\nseis",
            )
        check("orden custom: ok True", correcta["ok"] is True)
        check("orden custom: order 777", correcta["order"] == 777)
        check(
            "orden custom: costo de 5 comentarios",
            correcta["costo"] == 0.18525,
            f"({correcta['costo']})",
        )
        datos = post.llamadas[1]["data"]
        check(
            "orden custom: envia solo las lineas necesarias",
            datos.get("comments") == "uno\ndos\ntres\ncuatro\ncinco",
        )
        check(
            "orden custom: la cantidad va en quantity",
            datos.get("quantity") == "5",
        )

    with _entorno():
        post = _PostFalso({"balance": "0.10", "currency": "USD"})
        with mock.patch.object(jap.requests, "post", post):
            sin_saldo = jap.crear_orden(servicio, url, 1000)
        check(
            "orden: saldo insuficiente -> ok False",
            sin_saldo["ok"] is False,
        )
        check(
            "orden: el error menciona saldo insuficiente",
            "Saldo insuficiente" in sin_saldo["error"],
        )
        check(
            "orden: con saldo insuficiente no envia el add",
            len(post.llamadas) == 1,
        )

        post = _PostFalso(
            {"balance": "3.5", "currency": "USD"},
            {"error": "Not enough funds"},
        )
        with mock.patch.object(jap.requests, "post", post):
            rechazada = jap.crear_orden(servicio, url, 1000)
        check("orden: error de JAP -> ok False", rechazada["ok"] is False)
        check(
            "orden: propaga el error de JAP",
            "Not enough funds" in rechazada["error"],
        )
        check("orden: error de JAP deja order None", rechazada["order"] is None)

        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            sin_balance = jap.crear_orden(servicio, url, 1000)
        check(
            "orden: fallo al consultar saldo -> ok False",
            sin_balance["ok"] is False,
        )
        check(
            "orden: el error de saldo es claro",
            "No se pudo consultar el saldo" in sin_balance["error"],
        )

        post = _PostFalso(
            {"balance": "10", "currency": "USD"}, {"order": 888}
        )
        with mock.patch.object(jap.requests, "post", post):
            con_opciones = jap.crear_orden(
                servicio, url, 1000, runs=2, interval=60
            )
        check("orden: runs/interval -> ok True", con_opciones["ok"] is True)
        datos = post.llamadas[1]["data"]
        check("orden: envia runs", datos.get("runs") == "2")
        check("orden: envia interval", datos.get("interval") == "60")

    with _entorno():
        _cachear(_catalogo())
        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            inexistente = jap.crear_orden(999999, url, 1000)
        check(
            "orden: servicio id inexistente -> ok False",
            inexistente["ok"] is False,
        )
        check(
            "orden: servicio inexistente no hace peticiones",
            len(post.llamadas) == 0,
        )


def test_estado_orden(check):
    with _entorno():
        post = _PostFalso(
            {
                "123": {
                    "charge": "0.68750000",
                    "start_count": "100",
                    "status": "Completed",
                    "remains": "0",
                    "currency": "USD",
                }
            }
        )
        with mock.patch.object(jap.requests, "post", post):
            resultado = jap.estado_orden(123)
        check("estado: ok True", resultado["ok"] is True)
        check("estado: order 123", resultado["order"] == 123)
        check(
            "estado: charge float",
            resultado["charge"] == 0.6875,
            f"({resultado['charge']})",
        )
        check("estado: start_count int", resultado["start_count"] == 100)
        check("estado: status", resultado["status"] == "Completed")
        check("estado: remains int", resultado["remains"] == 0)
        check("estado: currency", resultado["currency"] == "USD")
        check(
            "estado: envia action=status y orders",
            post.llamadas[0]["data"].get("action") == "status"
            and post.llamadas[0]["data"].get("orders") == "123",
        )

        post = _PostFalso(
            {
                "charge": "0.10",
                "start_count": 5,
                "status": "Pending",
                "remains": 10,
                "currency": "USD",
            }
        )
        with mock.patch.object(jap.requests, "post", post):
            directo = jap.estado_orden(124)
        check(
            "estado: acepta campos directos del panel",
            directo["ok"] is True and directo["status"] == "Pending",
        )

        post = _PostFalso({"error": "Incorrect order ID"})
        with mock.patch.object(jap.requests, "post", post):
            error = jap.estado_orden(123)
        check("estado: error de JAP -> ok False", error["ok"] is False)

        post = _PostFalso({"123": {"error": "Incorrect order ID"}})
        with mock.patch.object(jap.requests, "post", post):
            error_interno = jap.estado_orden(123)
        check(
            "estado: error dentro del id -> ok False",
            error_interno["ok"] is False,
        )
        check(
            "estado: propaga el error interno",
            "Incorrect order ID" in error_interno["error"],
        )

        post = _PostFalso({"123": {}})
        with mock.patch.object(jap.requests, "post", post):
            vacio = jap.estado_orden(123)
        check("estado: respuesta vacia -> ok False", vacio["ok"] is False)

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.estado_orden("abc")
        check("estado: id invalido -> ok False", invalido["ok"] is False)
        check(
            "estado: id invalido no hace peticiones",
            len(post.llamadas) == 0,
        )

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            cero = jap.estado_orden(0)
        check("estado: id 0 -> ok False", cero["ok"] is False)


def test_estado_ordenes(check):
    respuesta = {
        "1": {
            "charge": "0.1",
            "start_count": 10,
            "status": "Pending",
            "remains": 90,
            "currency": "USD",
        },
        "2": {
            "charge": "0.2",
            "start_count": 0,
            "status": "Completed",
            "remains": 0,
            "currency": "USD",
        },
    }
    with _entorno():
        post = _PostFalso(respuesta)
        with mock.patch.object(jap.requests, "post", post):
            resultado = jap.estado_ordenes([1, 2])
        check("estado_ordenes: ok True", resultado["ok"] is True)
        check(
            "estado_ordenes: devuelve el dict de ordenes",
            set(resultado["ordenes"].keys()) == {"1", "2"},
        )
        check(
            "estado_ordenes: envia orders=1,2",
            post.llamadas[0]["data"].get("orders") == "1,2",
        )

        post = _PostFalso(respuesta)
        with mock.patch.object(jap.requests, "post", post):
            desde_string = jap.estado_ordenes("1,2")
        check(
            "estado_ordenes: acepta ids como string",
            desde_string["ok"] is True
            and post.llamadas[0]["data"].get("orders") == "1,2",
        )

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.estado_ordenes("1,abc")
        check(
            "estado_ordenes: string invalido -> ok False",
            invalido["ok"] is False,
        )
        check(
            "estado_ordenes: string invalido no hace peticiones",
            len(post.llamadas) == 0,
        )

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            vacio = jap.estado_ordenes([])
        check("estado_ordenes: lista vacia -> ok False", vacio["ok"] is False)

        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            red = jap.estado_ordenes([1, 2])
        check("estado_ordenes: fallo de red -> ok False", red["ok"] is False)


def test_refill_y_cancelar(check):
    with _entorno():
        post = _PostFalso({"refill": 123})
        with mock.patch.object(jap.requests, "post", post):
            resultado = jap.refill_orden(123)
        check("refill: ok True", resultado["ok"] is True)
        check("refill: devuelve el id", resultado["refill"] == 123)
        check(
            "refill: envia action=refill y order",
            post.llamadas[0]["data"].get("action") == "refill"
            and post.llamadas[0]["data"].get("order") == "123",
        )

        post = _PostFalso({"error": "Refill no disponible"})
        with mock.patch.object(jap.requests, "post", post):
            error = jap.refill_orden(123)
        check("refill: error de JAP -> ok False", error["ok"] is False)

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.refill_orden("abc")
        check("refill: id invalido -> ok False", invalido["ok"] is False)

        post = _PostFalso({"cancel": {"1": "success", "2": "success"}})
        with mock.patch.object(jap.requests, "post", post):
            cancelado = jap.cancelar_orden([1, 2])
        check("cancelar: ok True", cancelado["ok"] is True)
        check(
            "cancelar: envia action=cancel y orders=1,2",
            post.llamadas[0]["data"].get("action") == "cancel"
            and post.llamadas[0]["data"].get("orders") == "1,2",
        )
        check(
            "cancelar: devuelve el resultado",
            isinstance(cancelado["cancel"], dict),
        )

        post = _PostFalso({"cancel": 1})
        with mock.patch.object(jap.requests, "post", post):
            unico = jap.cancelar_orden(1)
        check(
            "cancelar: acepta un unico id",
            unico["ok"] is True
            and post.llamadas[0]["data"].get("orders") == "1",
        )

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.cancelar_orden([1, "x"])
        check("cancelar: lista invalida -> ok False", invalido["ok"] is False)

        post = _PostFalso({"error": "Order cannot be cancelled"})
        with mock.patch.object(jap.requests, "post", post):
            rechazado = jap.cancelar_orden([1])
        check(
            "cancelar: error de JAP -> ok False",
            rechazado["ok"] is False,
        )


def test_saldo_suficiente(check):
    with _entorno():
        post = _PostFalso({"balance": "3.5098125", "currency": "USD"})
        with mock.patch.object(jap.requests, "post", post):
            alcanza = jap.saldo_suficiente(0.5)
        check("saldo_suficiente: ok True", alcanza["ok"] is True)
        check("saldo_suficiente: balance", alcanza["balance"] == 3.5098125)
        check("saldo_suficiente: costo", alcanza["costo"] == 0.5)
        check("saldo_suficiente: suficiente True", alcanza["suficiente"] is True)

        post = _PostFalso({"balance": "0.10", "currency": "USD"})
        with mock.patch.object(jap.requests, "post", post):
            no_alcanza = jap.saldo_suficiente(5)
        check(
            "saldo_suficiente: suficiente False",
            no_alcanza["ok"] is True and no_alcanza["suficiente"] is False,
        )

        post = _PostFalso()
        with mock.patch.object(jap.requests, "post", post):
            invalido = jap.saldo_suficiente(-1)
        check("saldo_suficiente: costo negativo -> ok False", invalido["ok"] is False)

        post = _PostFalso(requests.exceptions.ConnectionError("sin red"))
        with mock.patch.object(jap.requests, "post", post):
            sin_red = jap.saldo_suficiente(1)
        check("saldo_suficiente: sin red -> ok False", sin_red["ok"] is False)


# ---------------------------------------------------------------------------
# Seguridad y configuracion
# ---------------------------------------------------------------------------
def test_sin_fugas_de_key(check):
    with _entorno():
        salida = io.StringIO()
        registros = []

        class _Capturador(logging.Handler):
            def emit(self, record):
                registros.append(record.getMessage())

        capturador = _Capturador()
        logger = logging.getLogger("integraciones.jap")
        logger.addHandler(capturador)
        try:
            post = _PostFalso(
                requests.exceptions.ConnectionError(f"fallo con {CLAVE_FALSA}")
            )
            with mock.patch.object(jap.requests, "post", post):
                with contextlib.redirect_stdout(salida), \
                        contextlib.redirect_stderr(salida):
                    resultado = jap.obtener_balance()
                    guardado = jap.guardar_api_key(CLAVE_FALSA)
        finally:
            logger.removeHandler(capturador)

        check(
            "seguridad: el error devuelto no contiene la key",
            CLAVE_FALSA not in json.dumps(resultado),
        )
        check(
            "seguridad: el error de red se sanea con ***",
            "***" in resultado["error"],
        )
        check(
            "seguridad: guardar_api_key no devuelve la key",
            CLAVE_FALSA not in json.dumps(guardado),
        )
        check(
            "seguridad: stdout/stderr no contiene la key",
            CLAVE_FALSA not in salida.getvalue(),
        )
        check(
            "seguridad: los logs no contienen la key",
            all(CLAVE_FALSA not in mensaje for mensaje in registros),
            f"({len(registros)} registro(s))",
        )

        check(
            "seguridad: _limpiar_error reemplaza la key",
            jap._limpiar_error(f"boom {CLAVE_FALSA}", CLAVE_FALSA)
            == "boom ***",
        )


def test_timeout(check):
    with _entorno():
        post = _PostFalso(
            {"balance": "1", "currency": "USD"},
            {"balance": "1", "currency": "USD"},
            {"balance": "1", "currency": "USD"},
        )
        with mock.patch.object(jap.requests, "post", post):
            jap.obtener_balance()
            with mock.patch.dict(os.environ, {"JAP_TIMEOUT_SEG": "7"}):
                jap.obtener_balance()
            with mock.patch.dict(os.environ, {"JAP_TIMEOUT_SEG": "no-numero"}):
                jap.obtener_balance()
        check(
            "timeout: default 30 segundos",
            post.llamadas[0]["timeout"] == 30.0,
        )
        check(
            "timeout: JAP_TIMEOUT_SEG=7 se respeta",
            post.llamadas[1]["timeout"] == 7.0,
        )
        check(
            "timeout: valor invalido cae al default 30",
            post.llamadas[2]["timeout"] == 30.0,
        )


def run(check):
    """Ejecuta todos los checks con el ``check`` del runner del proyecto."""
    test_api_key(check)
    test_guardar_api_key(check)
    test_disponible(check)
    test_balance(check)
    test_cache_servicios(check)
    test_clasificar_servicio(check)
    test_es_servicio_x(check)
    test_filtrar_servicios(check)
    test_resumen_catalogo(check)
    test_calcular_costo(check)
    test_servicios_recomendados(check)
    test_crear_orden(check)
    test_estado_orden(check)
    test_estado_ordenes(check)
    test_refill_y_cancelar(check)
    test_saldo_suficiente(check)
    test_sin_fugas_de_key(check)
    test_timeout(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
