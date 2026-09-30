# -*- coding: utf-8 -*-
"""Tests de las IMAGENES OPCIONALES en los posts de activaciones (motor).

Feature 100% aditiva: sin imagenes configuradas todo se comporta igual que
siempre; con imagenes, los posts del rol "hashtags" llevan UNA al azar con
probabilidad configurable (0-100).

Sin red, sin Chrome y sin tocar la base real (fakes + tempdir + monkeypatch,
mismo patron que `tests/test_actividad_motor.py`/`test_urls_exitos_motor.py`).

Cubre:
    (1) `_normalizar_imagenes_activacion`: str multilinea con ';'/'|', dedupe,
        tope 100, None -> [] y listas/tuplas/sets.
    (2) `_configurar_imagenes_activacion`: probabilidad None/150/-5/"30"/basura
        y conteo de rutas EXISTENTES en un tempdir; reset con `imagenes=None`.
    (3) `_elegir_imagen_post`: prob 0 -> "", prob 100 con archivos reales ->
        siempre una de la lista (200 iteraciones), sin existentes -> "" y
        frontera exacta con `random.random` parcheado.
    (4) `_intentar_accion_rol` rol "hashtags": con imagen NO llama a la API y
        el bot recibe `imagen_path`; sin imagen SI llama a la API y el bot va
        sin imagen. El alias "post" tambien lleva imagen. Otros roles (rt)
        jamas piden imagen.
    (5) Firmas de `ejecutar_por_roles`/`ejecutar_actividad` (kwargs AL FINAL) y
        mini-E2E: configurar/restaurar imagenes dentro de la campana.
    (6) Fakes viejos: bot con `publicar_tweet(texto, buscar_url=True)` sin
        `imagen_path` NO lanza con imagen (fallback TypeError) y publica igual.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_imagenes_motor.py
"""
from __future__ import annotations

import contextlib
import inspect
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
TESTS = RAIZ / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _parches(*cambios):
    """Aplica `(objeto, nombre, valor)` y restaura SIEMPRE al salir."""
    originales = []
    try:
        for objeto, nombre, valor in cambios:
            originales.append((objeto, nombre, getattr(objeto, nombre)))
            setattr(objeto, nombre, valor)
        yield
    finally:
        for objeto, nombre, valor in reversed(originales):
            setattr(objeto, nombre, valor)


def _archivo(tmp: str, nombre: str) -> str:
    """Crea un archivo real en `tmp` y devuelve su ruta."""
    ruta = os.path.join(tmp, nombre)
    with open(ruta, "wb") as fh:
        fh.write(b"png")
    return ruta


class _CuentaFake:
    """Cuenta minima con los atributos que leen los metodos del motor."""

    def __init__(self, usuario="cuenta_imagen", **extra):
        self.usuario = usuario
        self.auth_token = "auth_token_fake"
        self.cookies_json = ""
        self.password = ""
        self.plataforma = "twitter"
        self.activa = True
        for clave, valor in extra.items():
            setattr(self, clave, valor)


class _BotFake:
    """Bot fake con el CONTRATO NUEVO (`publicar_tweet` acepta imagen_path)."""

    def __init__(self):
        self.llamadas: list = []
        self.ultimo_error = ""
        self.ultima_url_publicada = ""
        self.cuenta_suspendida = False

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True):
        self.llamadas.append((texto, imagen_path, buscar_url))
        return True

    def solo_retwittear(self, urls, usuario, dar_like=False):
        self.llamadas.append(("rt", list(urls), usuario, dar_like))
        return {"exitos": 1, "urls": []}

    def esta_vivo(self):
        return True

    def cerrar(self):
        pass


class _BotViejo:
    """Bot fake con el CONTRATO VIEJO (sin `imagen_path`)."""

    def __init__(self):
        self.llamadas: list = []
        self.ultimo_error = ""
        self.ultima_url_publicada = ""

    def publicar_tweet(self, texto, buscar_url=True):
        self.llamadas.append((texto, buscar_url))
        return True


class _PestanaFake:
    """Pestaña minima del pool (solo `bot`)."""

    def __init__(self, bot):
        self.bot = bot


def _motor_con_pestana(bot):
    """Motor que ejecuta la accion en una pestaña fake (sin Chrome/BD)."""
    motor = MotorActivacion(max_concurrente=1)
    pestana = _PestanaFake(bot)
    motor._adquirir_pestana = lambda cuenta: pestana
    motor._cambiar_cuenta_pestana = lambda p, c: True
    motor._finalizar_pestana = lambda p, c, r: None
    motor._capturar_url_post = lambda: False
    return motor


# --------------------------------------------------------------------------- #
# (1) Normalizacion
# --------------------------------------------------------------------------- #
def test_normalizacion(check):
    print("(1) _normalizar_imagenes_activacion: separadores, dedupe y tope")
    motor = MotorActivacion(max_concurrente=1)
    partes = motor._normalizar_imagenes_activacion(
        "  a.png ; a.png ;b.png;B.png ; a.png "
    )
    check(
        "str multilinea con salto, ';' y '|' se separa",
        motor._normalizar_imagenes_activacion(
            "a.png\nb.png;c.png|d.png\r\ne.png"
        ) == ["a.png", "b.png", "c.png", "d.png", "e.png"],
        f"({motor._normalizar_imagenes_activacion('a.png;b.png')})",
    )
    check(
        "strip y dedupe preservando el orden",
        partes == ["a.png", "b.png", "B.png"],
        f"({partes})",
    )
    check(
        "None y textos vacios devuelven []",
        motor._normalizar_imagenes_activacion(None) == []
        and motor._normalizar_imagenes_activacion("") == []
        and motor._normalizar_imagenes_activacion("  ") == []
        and motor._normalizar_imagenes_activacion(";|;") == [],
        f"({motor._normalizar_imagenes_activacion(None)})",
    )
    check(
        "acepta lista, tupla y set de rutas",
        motor._normalizar_imagenes_activacion(["x.png", "", " y.png "])
        == ["x.png", "y.png"]
        and motor._normalizar_imagenes_activacion(("x.png", "y.png"))
        == ["x.png", "y.png"]
        and sorted(motor._normalizar_imagenes_activacion({"x.png", "y.png"}))
        == ["x.png", "y.png"],
        f"({motor._normalizar_imagenes_activacion(['x.png', '', ' y.png '])})",
    )
    larga = [f"i{n}.png" for n in range(150)]
    tope = motor._normalizar_imagenes_activacion("|".join(larga))
    check(
        "tope de 100 rutas",
        len(tope) == 100 and tope[0] == "i0.png" and tope[-1] == "i99.png",
        f"({len(tope)}, {tope[-1] if tope else None})",
    )


# --------------------------------------------------------------------------- #
# (2) Configuracion (probabilidad + existentes + reset)
# --------------------------------------------------------------------------- #
def test_configuracion(check):
    print("(2) _configurar_imagenes_activacion: clamp, existentes y reset")
    motor = MotorActivacion(max_concurrente=1)
    check(
        "init: lista vacia y probabilidad 0.0",
        motor._imagenes_activacion == []
        and motor._prob_imagen_activacion == 0.0,
        f"({motor._imagenes_activacion}, {motor._prob_imagen_activacion})",
    )
    with tempfile.TemporaryDirectory() as tmp:
        p1 = _archivo(tmp, "uno.png")
        p2 = _archivo(tmp, "dos.png")
        p3 = os.path.join(tmp, "tres.png")  # NO existe
        existentes = motor._configurar_imagenes_activacion(
            [p1, p2, p3], None
        )
        check(
            "None -> probabilidad 30 y cuenta solo rutas existentes (2/3)",
            existentes == 2
            and motor._prob_imagen_activacion == 30.0
            and motor._imagenes_activacion == [p1, p2, p3],
            f"(existentes={existentes}, prob={motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, 150)
        check(
            "150 se clampa a 100",
            motor._prob_imagen_activacion == 100.0,
            f"({motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, -5)
        check(
            "-5 se clampa a 0",
            motor._prob_imagen_activacion == 0.0,
            f"({motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, "30")
        check(
            '"30" (str) -> 30.0',
            motor._prob_imagen_activacion == 30.0,
            f"({motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, "basura")
        check(
            "basura -> default 30.0",
            motor._prob_imagen_activacion == 30.0,
            f"({motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, 33.5)
        check(
            "float valido se conserva (33.5)",
            motor._prob_imagen_activacion == 33.5,
            f"({motor._prob_imagen_activacion})",
        )
        motor._configurar_imagenes_activacion(p1, 100)
        existentes = motor._configurar_imagenes_activacion(None, 100)
        check(
            "imagenes=None resetea la lista (y devuelve 0 existentes)",
            existentes == 0
            and motor._imagenes_activacion == []
            and motor._prob_imagen_activacion == 100.0,
            f"({motor._imagenes_activacion}, {motor._prob_imagen_activacion})",
        )


# --------------------------------------------------------------------------- #
# (3) Eleccion aleatoria
# --------------------------------------------------------------------------- #
def test_eleccion(check):
    print("(3) _elegir_imagen_post: prob 0/100, sin existentes y frontera")
    motor = MotorActivacion(max_concurrente=1)
    with tempfile.TemporaryDirectory() as tmp:
        p1 = _archivo(tmp, "uno.png")
        p2 = _archivo(tmp, "dos.png")
        ausente = os.path.join(tmp, "no_existe.png")

        motor._configurar_imagenes_activacion([p1, p2], 0)
        check(
            "prob 0 -> '' SIEMPRE (200 iteraciones)",
            all(motor._elegir_imagen_post() == "" for _ in range(200)),
        )

        motor._configurar_imagenes_activacion([p1, p2, ausente], 100)
        elecciones = {motor._elegir_imagen_post() for _ in range(200)}
        check(
            "prob 100 con archivos reales -> siempre una de las existentes",
            elecciones == {p1, p2},
            f"({sorted(elecciones)})",
        )

        motor._configurar_imagenes_activacion([ausente], 100)
        check(
            "prob 100 sin NINGUNA ruta existente -> ''",
            motor._elegir_imagen_post() == "",
        )

        motor._configurar_imagenes_activacion([], 100)
        check(
            "sin imagenes -> ''",
            motor._elegir_imagen_post() == "",
        )

        motor._configurar_imagenes_activacion([p1], 30)
        with mock.patch.object(
            motor_mod.random, "random", return_value=0.2999999
        ):
            justo_debajo = motor._elegir_imagen_post()
        with mock.patch.object(
            motor_mod.random, "random", return_value=0.3
        ):
            justo_arriba = motor._elegir_imagen_post()
        check(
            "frontera del umbral: 29.99..% elige y 30% exacto NO",
            justo_debajo == p1 and justo_arriba == "",
            f"({justo_debajo!r}, {justo_arriba!r})",
        )


# --------------------------------------------------------------------------- #
# (4) `_intentar_accion_rol` con imagen (rol hashtags)
# --------------------------------------------------------------------------- #
def test_intentar_accion_rol(check):
    print("(4) rol hashtags: con imagen salta la API; sin imagen usa la API")
    cuenta = _CuentaFake()
    ruta_imagen = "C:/imagenes/una.png"

    # --- CON imagen: la API NO se llama y el bot recibe la ruta. ---
    bot = _BotFake()
    motor = _motor_con_pestana(bot)
    api_llamadas: list = []

    def _api_que_revienta(*args, **kwargs):
        api_llamadas.append((args, kwargs))
        raise AssertionError("la API no debia llamarse con imagen")

    motor._probar_api_rol = _api_que_revienta
    motor._elegir_imagen_post = lambda: ruta_imagen
    resultado = motor._intentar_accion_rol(
        cuenta, "hashtags", [], "texto con imagen", False
    )
    check(
        "con imagen: exito y detalle 'hashtags publicados (con imagen)'",
        resultado[2] is True
        and resultado[3] == "hashtags publicados (con imagen)",
        f"({resultado})",
    )
    check(
        "con imagen: NO se llama a `_probar_api_rol`",
        api_llamadas == [],
        f"({api_llamadas})",
    )
    check(
        "con imagen: el bot recibe imagen_path y buscar_url=False",
        bot.llamadas == [("texto con imagen", ruta_imagen, False)],
        f"({bot.llamadas})",
    )

    # --- El alias "post" tambien lleva imagen. ---
    bot_alias = _BotFake()
    motor_alias = _motor_con_pestana(bot_alias)
    motor_alias._probar_api_rol = _api_que_revienta
    motor_alias._elegir_imagen_post = lambda: ruta_imagen
    resultado_alias = motor_alias._intentar_accion_rol(
        cuenta, "post", [], "texto alias", False
    )
    check(
        "alias 'post' -> hashtags con imagen",
        resultado_alias[1] == "hashtags"
        and resultado_alias[2] is True
        and bot_alias.llamadas
        == [("texto alias", ruta_imagen, False)],
        f"({resultado_alias}, {bot_alias.llamadas})",
    )

    # --- SIN imagen: SI se llama a la API y el bot va sin imagen. ---
    bot2 = _BotFake()
    motor2 = _motor_con_pestana(bot2)
    api_calls: list = []

    def _api_recorder(c, rol, url, texto, dar_like):
        api_calls.append((c.usuario, rol, url, texto, dar_like))
        return None

    motor2._probar_api_rol = _api_recorder
    motor2._elegir_imagen_post = lambda: ""
    resultado2 = motor2._intentar_accion_rol(
        cuenta, "hashtags", [], "texto sin imagen", False
    )
    check(
        "sin imagen: exito y detalle 'hashtags publicados'",
        resultado2[2] is True
        and resultado2[3] == "hashtags publicados",
        f"({resultado2})",
    )
    check(
        "sin imagen: SI se llama a `_probar_api_rol` UNA vez",
        len(api_calls) == 1
        and api_calls[0][0] == cuenta.usuario
        and api_calls[0][1] == "hashtags",
        f"({api_calls})",
    )
    check(
        "sin imagen: el bot publica con imagen_path=None (default)",
        bot2.llamadas == [("texto sin imagen", None, False)],
        f"({bot2.llamadas})",
    )

    # --- Otros roles jamas piden imagen (aunque haya configurada). ---
    bot3 = _BotFake()
    motor3 = _motor_con_pestana(bot3)
    motor3._probar_api_rol = _api_recorder
    motor3._elegir_imagen_post = lambda: (_ for _ in ()).throw(
        AssertionError("rt no debe elegir imagen")
    )
    resultado3 = motor3._intentar_accion_rol(
        cuenta, "rt", ["https://x.com/ancla/status/1"], "", False
    )
    check(
        "rol rt: exito por Selenium SIN intentar elegir imagen",
        resultado3[2] is True
        and bot3.llamadas
        == [("rt", ["https://x.com/ancla/status/1"], "cuenta_imagen", False)],
        f"({resultado3}, {bot3.llamadas})",
    )


# --------------------------------------------------------------------------- #
# (5) Firmas + mini-E2E (configuracion/restauracion en campana)
# --------------------------------------------------------------------------- #
def test_firmas_y_e2e(check):
    print("(5) firmas con los kwargs AL FINAL y mini-E2E de configuracion")
    esperado = ["imagenes", "probabilidad_imagen"]
    for nombre, funcion in (
        ("ejecutar_por_roles", MotorActivacion.ejecutar_por_roles),
        ("_ejecutar_por_roles_campana",
         MotorActivacion._ejecutar_por_roles_campana),
        ("ejecutar_actividad", MotorActivacion.ejecutar_actividad),
        ("_ejecutar_actividad_impl",
         MotorActivacion._ejecutar_actividad_impl),
    ):
        params = inspect.signature(funcion).parameters
        nombres = list(params)
        check(
            f"{nombre}: termina en {esperado}",
            nombres[-2:] == esperado,
            f"({nombres[-4:]})",
        )
        check(
            f"{nombre}: defaults None/30",
            params["imagenes"].default is None
            and params["probabilidad_imagen"].default == 30,
            f"({params['imagenes'].default}, "
            f"{params['probabilidad_imagen'].default})",
        )

    # Mini-E2E de `ejecutar_actividad` con imagenes configuradas.
    cuenta = _CuentaFake("img_cuenta")
    motor = MotorActivacion(max_concurrente=1)
    motor._obtener_cuentas_por_rol = lambda *a, **k: [cuenta]
    motor._generar_textos_actividad = (
        lambda cs, objetivo, tags, *a, **k: {
            c.usuario: ["texto de la imagen #x"] for c in cs
        }
    )
    llamadas: list = []

    def _intentar(c, rol, urls, texto, dar_like):
        llamadas.append((c.usuario, rol, texto))
        return (c.usuario, rol, True, "ok", "https://x.com/img_cuenta/status/1")

    motor._intentar_accion_rol = _intentar
    motor._dormir_cancelable = lambda segundos, tramo=0.5: True
    with _parches(
        (motor_mod, "registrar_accion", lambda *a, **k: None),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    ):
        resumen = motor.ejecutar_actividad(
            usuarios=["img_cuenta"],
            hashtags=["#x"],
            posts_min=1,
            posts_max=1,
            imagenes=["C:/imagenes/a.png", "C:/imagenes/b.png"],
            probabilidad_imagen=55,
        )
    check(
        "E2E actividad: publica y configura las imagenes de la campana",
        resumen.get("exitosas") == 1
        and llamadas
        and motor._imagenes_activacion
        == ["C:/imagenes/a.png", "C:/imagenes/b.png"]
        and motor._prob_imagen_activacion == 55.0,
        f"(exitosas={resumen.get('exitosas')}, "
        f"imgs={motor._imagenes_activacion})",
    )
    with _parches(
        (motor_mod, "registrar_accion", lambda *a, **k: None),
        (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
        (cuotas_mod, "contar_acciones_dia_por_usuario", lambda *a, **k: {}),
    ):
        resumen_sin = motor.ejecutar_actividad(
            usuarios=["img_cuenta"],
            hashtags=["#x"],
            posts_min=1,
            posts_max=1,
        )
    check(
        "E2E actividad SIN kwargs: sigue publicando y resetea las imagenes",
        resumen_sin.get("exitosas") == 1
        and motor._imagenes_activacion == []
        and motor._prob_imagen_activacion == 30.0,
        f"(exitosas={resumen_sin.get('exitosas')}, "
        f"imgs={motor._imagenes_activacion})",
    )

    # `ejecutar_por_roles` reenvia los kwargs al flujo real (capturado).
    motor_roles = MotorActivacion(max_concurrente=1)
    capturado: dict = {}

    def _campana_fake(*args, **kwargs):
        capturado["args"] = args
        capturado["kwargs"] = kwargs
        return {"exitosas": 0, "fallidas": 0, "total": 0}

    motor_roles._ejecutar_por_roles_campana = _campana_fake
    motor_roles.ejecutar_por_roles(
        urls=["https://x.com/ancla/status/1"],
        imagenes=["C:/imagenes/a.png"],
        probabilidad_imagen=10,
    )
    check(
        "ejecutar_por_roles reenvia (cancelar, imagenes, probabilidad) "
        "al final",
        capturado.get("args", ())[-3:]
        == (None, ["C:/imagenes/a.png"], 10),
        f"({capturado.get('args', ())[-3:]})",
    )
    motor_roles.ejecutar_por_roles(urls=["https://x.com/ancla/status/1"])
    check(
        "ejecutar_por_roles sin kwargs reenvia (None, None, 30)",
        capturado.get("args", ())[-3:] == (None, None, 30),
        f"({capturado.get('args', ())[-3:]})",
    )


# --------------------------------------------------------------------------- #
# (6) Fakes viejos tolerados (fallback TypeError)
# --------------------------------------------------------------------------- #
def test_fake_viejo(check):
    print("(6) bot viejo sin `imagen_path`: fallback TypeError y publica igual")
    motor = MotorActivacion(max_concurrente=1)
    motor._capturar_url_post = lambda: False
    cuenta = _CuentaFake()
    bot = _BotViejo()
    try:
        resultado = motor._accion_en_bot(
            bot, cuenta, "hashtags", "texto con imagen", False, "",
            imagen_path="C:/imagenes/a.png",
        )
        lanzo = False
    except Exception as e:  # noqa: BLE001
        resultado, lanzo = None, f"{type(e).__name__}: {e}"
    check(
        "no lanza con el bot viejo (fallback TypeError)",
        lanzo is False,
        f"({lanzo})",
    )
    check(
        "publica igual y marca 'con imagen' en el detalle",
        isinstance(resultado, tuple)
        and resultado[2] is True
        and resultado[3] == "hashtags publicados (con imagen)"
        and bot.llamadas == [("texto con imagen", False)],
        f"({resultado}, {bot.llamadas})",
    )

    # Contrato nuevo: la imagen viaja como kwarg a `publicar_tweet`.
    bot2 = _BotFake()
    resultado2 = motor._accion_en_bot(
        bot2, cuenta, "hashtags", "texto con imagen", False, "",
        imagen_path="C:/imagenes/b.png",
    )
    check(
        "bot nuevo: recibe imagen_path y buscar_url",
        resultado2[2] is True
        and bot2.llamadas == [("texto con imagen", "C:/imagenes/b.png", False)],
        f"({bot2.llamadas})",
    )

    # Sin imagen: llamada EXACTA al contrato viejo (sin kwargs nuevos).
    bot3 = _BotViejo()
    resultado3 = motor._accion_en_bot(
        bot3, cuenta, "hashtags", "texto sin imagen", False, ""
    )
    check(
        "sin imagen: detalle normal y bot viejo sin kwargs nuevos",
        resultado3[2] is True
        and resultado3[3] == "hashtags publicados"
        and bot3.llamadas == [("texto sin imagen", False)],
        f"({resultado3}, {bot3.llamadas})",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_normalizacion(check)
    test_configuracion(check)
    test_eleccion(check)
    test_intentar_accion_rol(check)
    test_firmas_y_e2e(check)
    test_fake_viejo(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_imagenes_motor.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
