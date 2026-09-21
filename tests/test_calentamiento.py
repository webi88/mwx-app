# -*- coding: utf-8 -*-
"""Tests rapidos de `scheduler/calentamiento.py` (sin red, sin IA real, sin Chrome).

Cubre el contrato completo del calentamiento continuo:

  (a) `config_calentamiento()`: defaults, valores invalidos y rangos.
  (b) `campana_activa()`: marcador fresco / ausente / viejo, sin lanzar.
  (c) `elegir_cuenta()`: excluye sin sesion, inactivas, suspended, facebook y
      cuentas con `RegistroAccion` reciente; incluye sin registro; respeta
      `excluir_ids`; aleatoriedad determinista con semilla fija.
  (d) `generar_texto()`: IA que lanza -> fallback local no vacio con hashtag;
      fallback local caido -> plantilla propia; `CALENTAMIENTO_USAR_IA=0` NO
      llama a la IA; texto de IA sin hashtag -> se lo agrega.
  (e) `programar_publicacion()`: crea UNA Tarea con los campos correctos y sin
      duplicar pendientes de la misma cuenta (con DB fake).
  (f) `ejecutar_tanda_si_toca()`: apagado, pausado por campana, primera espera,
      tanda completa con fakes, intervalo dentro de rango y tolerancia a fallos.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_calentamiento.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.models import Cuenta, RegistroAccion, Tarea  # noqa: E402
from core.perfiles import tiene_hashtag  # noqa: E402
from scheduler import calentamiento  # noqa: E402
from scheduler import manager as manager_mod  # noqa: E402

_VARS = (
    "CALENTAMIENTO_ACTIVO",
    "CALENTAMIENTO_MIN_MIN",
    "CALENTAMIENTO_MAX_MIN",
    "CALENTAMIENTO_GAP_HORAS",
    "CALENTAMIENTO_POSTS",
    "CALENTAMIENTO_USAR_IA",
)

_DEFAULT = {
    "activo": True,
    "min_min": 15,
    "max_min": 45,
    "gap_horas": 12,
    "posts": 1,
    "usar_ia": True,
}


@contextlib.contextmanager
def _env(**valores):
    """Fija SOLO las variables del calentamiento y restaura el entorno al salir."""
    originales = {clave: os.environ.pop(clave, None) for clave in _VARS}
    try:
        for clave, valor in valores.items():
            if valor is not None:
                os.environ[clave] = str(valor)
        yield
    finally:
        for clave in _VARS:
            os.environ.pop(clave, None)
        for clave, valor in originales.items():
            if valor is not None:
                os.environ[clave] = valor


@contextlib.contextmanager
def _sin_sleep():
    """Neutraliza `time.sleep` (el test del intervalo no espera de verdad)."""
    with mock.patch.object(time, "sleep", lambda *args, **kwargs: None):
        yield


@contextlib.contextmanager
def _estado_limpio():
    """Guarda/restaura el estado de modulo `_proxima_tanda` entre tests."""
    original = calentamiento._proxima_tanda
    try:
        yield
    finally:
        calentamiento._proxima_tanda = original


# --------------------------------------------------------------------------- #
# Fakes de DB/cuentas (la suite no toca la base real ni la red).
# --------------------------------------------------------------------------- #
class _FakeQuery:
    def __init__(self, filas):
        self._filas = list(filas)

    def filter(self, *args, **kwargs):  # los filtros se re-aplican en Python
        return self

    def all(self):
        return list(self._filas)


class _FakeDB:
    """Sesion minima: `query(modelo).filter(...).all()`, `add` y `commit`."""

    def __init__(self, cuentas=(), registros=(), tareas=()):
        self.cuentas = list(cuentas)
        self.registros = list(registros)
        self.tareas = list(tareas)
        self.agregadas = []
        self.consultas = 0
        self._siguiente_id = len(self.tareas)

    def query(self, entidad, *resto):
        self.consultas += 1
        if entidad is Cuenta:
            return _FakeQuery(self.cuentas)
        if entidad is Tarea:
            return _FakeQuery(self.tareas)
        if entidad is RegistroAccion or getattr(entidad, "class_", None) is RegistroAccion:
            return _FakeQuery(self.registros)
        return _FakeQuery([])

    def add(self, obj):
        self._siguiente_id += 1
        if getattr(obj, "id", None) is None:
            obj.id = self._siguiente_id
        self.agregadas.append(obj)
        self.tareas.append(obj)

    def commit(self):
        pass

    def close(self):
        pass


class _FakeScheduler:
    """Fake de APScheduler: registra jobs sin arrancar hilos reales."""

    def __init__(self, *args, **kwargs):
        self.jobs = []
        self.started = False
        self.apagado = False

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self):
        self.started = True

    @property
    def running(self):
        return self.started and not self.apagado

    def shutdown(self):
        self.apagado = True


@contextlib.contextmanager
def _sesion_falsa(db):
    yield db


def _sesion_de(db):
    """Context manager `get_db_session()` que devuelve el fake `db`."""
    return mock.patch.object(
        calentamiento, "get_db_session", lambda: _sesion_falsa(db)
    )


def _sesion_manager(db):
    """Igual que `_sesion_de` pero para el `get_db_session` de scheduler.manager."""
    return mock.patch.object(
        manager_mod, "get_db_session", lambda: _sesion_falsa(db)
    )


@contextlib.contextmanager
def _manager(con_calentamiento=None, **valores_env):
    """Crea un `SchedulerManager` con el APScheduler REAL reemplazado por un fake.

    - `con_calentamiento=None` -> `SchedulerManager()` (contrato del dashboard).
    - `con_calentamiento=False/True` -> se pasa el kwarg (contrato standalone).
    No arranca hilos reales; al salir llama `detener()` por limpieza.
    """
    creados = []

    def _crear_scheduler(*args, **kwargs):
        creado = _FakeScheduler(*args, **kwargs)
        creados.append(creado)
        return creado

    with _env(**valores_env), mock.patch.object(
        manager_mod, "BackgroundScheduler", _crear_scheduler
    ):
        if con_calentamiento is None:
            obj = manager_mod.SchedulerManager()
        else:
            obj = manager_mod.SchedulerManager(con_calentamiento=con_calentamiento)
        try:
            yield obj, creados[0]
        finally:
            try:
                obj.detener()
            except Exception:  # noqa: BLE001
                pass


def _cuenta(id_, usuario, **cambios):
    datos = dict(
        id=id_,
        usuario=usuario,
        plataforma="twitter",
        activa=True,
        status="imported",
        cookies_path=f"data/cookies/twitter/{usuario}.pkl",
        cookies_json=None,
        auth_token="",
        tipo_cuenta="",
        personalidad="",
        seccion="",
        nombre_mostrado="",
        perfil_personalidad="",
    )
    datos.update(cambios)
    return SimpleNamespace(**datos)


def _texto_de_tarea(tarea) -> str:
    return str(getattr(tarea, "contenido", "") or "")


# --------------------------------------------------------------------------- #
# (a) config_calentamiento
# --------------------------------------------------------------------------- #
def test_config(check):
    print("(a) config_calentamiento: defaults, invalidos y rangos")
    with _env():
        cfg = calentamiento.config_calentamiento()
        check("config: sin env usa los defaults", cfg == _DEFAULT, repr(cfg))

    with _env(
        CALENTAMIENTO_ACTIVO="",
        CALENTAMIENTO_MIN_MIN="abc",
        CALENTAMIENTO_MAX_MIN="-4",
        CALENTAMIENTO_GAP_HORAS="??",
        CALENTAMIENTO_POSTS="muchos",
        CALENTAMIENTO_USAR_IA="",
    ):
        cfg = calentamiento.config_calentamiento()
        check(
            "config: valores invalidos -> defaults",
            cfg == _DEFAULT,
            repr(cfg),
        )

    with _env(CALENTAMIENTO_ACTIVO="0", CALENTAMIENTO_USAR_IA="no"):
        cfg = calentamiento.config_calentamiento()
        check("config: ACTIVO=0 -> apagado", cfg["activo"] is False, repr(cfg["activo"]))
        check("config: USAR_IA=no -> False", cfg["usar_ia"] is False, repr(cfg["usar_ia"]))

    with _env(
        CALENTAMIENTO_ACTIVO="1",
        CALENTAMIENTO_MIN_MIN="5",
        CALENTAMIENTO_MAX_MIN="10",
        CALENTAMIENTO_GAP_HORAS="3",
        CALENTAMIENTO_POSTS="2",
        CALENTAMIENTO_USAR_IA="0",
    ):
        cfg = calentamiento.config_calentamiento()
        check(
            "config: valores validos se respetan",
            (cfg["activo"], cfg["min_min"], cfg["max_min"], cfg["gap_horas"],
             cfg["posts"], cfg["usar_ia"]) == (True, 5, 10, 3, 2, False),
            repr(cfg),
        )

    with _env(CALENTAMIENTO_MIN_MIN="90", CALENTAMIENTO_MAX_MIN="10"):
        cfg = calentamiento.config_calentamiento()
        check(
            "config: min>max se reordena (10..90)",
            cfg["min_min"] == 10 and cfg["max_min"] == 90,
            repr(cfg),
        )

    with _env(CALENTAMIENTO_POSTS="999"):
        cfg = calentamiento.config_calentamiento()
        check("config: POSTS se acota a 10", cfg["posts"] == 10, repr(cfg["posts"]))


# --------------------------------------------------------------------------- #
# (b) campana_activa
# --------------------------------------------------------------------------- #
def test_campana_activa(check):
    print("(b) campana_activa: marcador fresco/ausente/viejo sin lanzar")
    check(
        "campana: el marcador vive en data/.campana_activa",
        calentamiento.ARCHIVO_CAMPANA_REL == "data/.campana_activa",
        repr(calentamiento.ARCHIVO_CAMPANA_REL),
    )

    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / ".campana_activa"
        with mock.patch.object(calentamiento, "resolver_ruta", lambda rel: str(ruta)):
            check("campana: sin archivo -> False", calentamiento.campana_activa() is False)

            ruta.write_text("1", encoding="utf-8")
            check("campana: archivo fresco -> True", calentamiento.campana_activa() is True)

            viejo = time.time() - 2 * 60 * 60
            os.utime(ruta, (viejo, viejo))
            check(
                "campana: archivo viejo (2h) -> False",
                calentamiento.campana_activa() is False,
            )

            ruta.write_text("1", encoding="utf-8")
            with mock.patch.object(
                calentamiento, "resolver_ruta", side_effect=RuntimeError("boom")
            ):
                check(
                    "campana: error leyendo -> False sin lanzar",
                    calentamiento.campana_activa() is False,
                )


# --------------------------------------------------------------------------- #
# (c) elegir_cuenta
# --------------------------------------------------------------------------- #
def test_elegir_cuenta(check):
    print("(c) elegir_cuenta: exclusiones, elegibilidad y aleatoriedad")
    with _env():
        # Sin sesion / inactiva / suspended / facebook / reciente.
        db = _FakeDB(
            cuentas=[
                _cuenta(1, "sin_sesion", cookies_path="", cookies_json=None, auth_token=""),
                _cuenta(2, "inactiva", activa=False),
                _cuenta(3, "suspendida", status="suspended"),
                _cuenta(4, "suspendida_mayus", status="SUSPENDED"),
                _cuenta(5, "facebook", plataforma="facebook"),
                _cuenta(6, "buena_uno"),
            ],
            registros=[("buena_uno",)],
        )
        check(
            "elegir: sin sesion/inactiva/suspended/facebook/reciente -> None",
            calentamiento.elegir_cuenta(db) is None,
        )

        # Una sola elegible: la devuelve aunque sea "reciente" para otras.
        db = _FakeDB(cuentas=[_cuenta(7, "unica"), _cuenta(8, "reciente")],
                     registros=[("reciente",)])
        elegida = calentamiento.elegir_cuenta(db)
        check(
            "elegir: sin registro reciente entra (ignora a la reciente)",
            elegida is not None and elegida.usuario == "unica",
            getattr(elegida, "usuario", None),
        )

        # cookies_json con contenido cuenta como sesion; vacio no.
        db = _FakeDB(cuentas=[
            _cuenta(9, "json_vacio", cookies_path="", cookies_json=[], auth_token=""),
            _cuenta(10, "json_lleno", cookies_path="", cookies_json=[{"name": "auth_token"}]),
        ])
        elegida = calentamiento.elegir_cuenta(db)
        check(
            "elegir: cookies_json vacio NO es sesion y con cookies SI",
            elegida is not None and elegida.usuario == "json_lleno",
            getattr(elegida, "usuario", None),
        )

        # auth_token solo tambien sirve.
        db = _FakeDB(cuentas=[
            _cuenta(11, "solo_token", cookies_path="", cookies_json=None, auth_token="tok"),
        ])
        elegida = calentamiento.elegir_cuenta(db)
        check(
            "elegir: auth_token solo es sesion valida",
            elegida is not None and elegida.usuario == "solo_token",
            getattr(elegida, "usuario", None),
        )

        # excluir_ids (cuentas con tarea pendiente).
        db = _FakeDB(cuentas=[_cuenta(12, "ocupada"), _cuenta(13, "libre")])
        elegida = calentamiento.elegir_cuenta(db, excluir_ids={12})
        check(
            "elegir: respeta excluir_ids",
            elegida is not None and elegida.usuario == "libre",
            getattr(elegida, "usuario", None),
        )
        check(
            "elegir: todo excluido -> None",
            calentamiento.elegir_cuenta(db, excluir_ids={12, 13}) is None,
        )

        # Aleatoriedad con semilla fija sobre 6 elegibles.
        candidatas = [_cuenta(100 + i, f"u{i}") for i in range(6)]
        db = _FakeDB(cuentas=candidatas)
        elegidas = set()
        for semilla in range(12):
            random.seed(semilla)
            elegida = calentamiento.elegir_cuenta(db)
            if elegida is not None:
                elegidas.add(elegida.usuario)
        check(
            "elegir: elige al azar entre las elegibles (semilla fija)",
            elegidas and elegidas <= {f"u{i}" for i in range(6)},
            repr(sorted(elegidas)),
        )
        random.seed(3)
        primera = calentamiento.elegir_cuenta(db)
        random.seed(3)
        segunda = calentamiento.elegir_cuenta(db)
        check(
            "elegir: con la misma semilla elige la misma cuenta",
            primera is not None and segunda is not None and primera.id == segunda.id,
            f"{getattr(primera, 'usuario', None)} / {getattr(segunda, 'usuario', None)}",
        )

        # Nunca lanza aunque la DB falle.
        class _DBRota:
            def query(self, *args, **kwargs):
                raise RuntimeError("db caida")

        check(
            "elegir: DB rota -> None sin lanzar",
            calentamiento.elegir_cuenta(_DBRota()) is None,
        )


# --------------------------------------------------------------------------- #
# (d) generar_texto
# --------------------------------------------------------------------------- #
def test_generar_texto(check):
    print("(d) generar_texto: IA caida, fallback local y hashtag garantizado")
    cuenta = _cuenta(
        20,
        "u_estilo",
        tipo_cuenta="ciudadana",
        perfil_personalidad="popular",
        personalidad="le gusta el futbol",
        nombre_mostrado="Una Ciudadana",
    )

    # IA que lanza -> fallback local no vacio con hashtag.
    with _env():
        with mock.patch(
            "ia.generador_contenido.generar_textos_mantenimiento",
            side_effect=RuntimeError("OPENAI_API_KEY invalida (test)"),
        ):
            texto = calentamiento.generar_texto(cuenta)
        check(
            "texto: IA que lanza -> NO lanza y devuelve string",
            isinstance(texto, str) and bool(texto.strip()),
            repr(texto[:120]),
        )
        check("texto: el fallback lleva hashtag", tiene_hashtag(texto), repr(texto[:120]))

    # IA y fallback local caidos -> plantilla propia con hashtag.
    with _env():
        with mock.patch(
            "ia.generador_contenido.generar_textos_mantenimiento",
            side_effect=RuntimeError("IA caida"),
        ), mock.patch(
            "ia.generador_contenido._fallback_estructura_mantenimiento",
            side_effect=RuntimeError("local caido"),
        ):
            texto = calentamiento.generar_texto(cuenta)
        check(
            "texto: IA y local caidos -> plantilla propia no vacia",
            isinstance(texto, str) and len(texto.strip()) > 10,
            repr(texto[:120]),
        )
        check(
            "texto: la plantilla propia lleva hashtag",
            tiene_hashtag(texto),
            repr(texto[:120]),
        )

    # CALENTAMIENTO_USAR_IA=0: NO se llama a OpenAI.
    with _env(CALENTAMIENTO_USAR_IA="0"):
        falso = mock.MagicMock(side_effect=AssertionError("no debe llamarse"))
        with mock.patch("ia.generador_contenido.generar_textos_mantenimiento", falso):
            texto = calentamiento.generar_texto(cuenta)
        check(
            "texto: USAR_IA=0 no llama a la IA",
            falso.call_count == 0,
            f"llamadas={falso.call_count}",
        )
        check(
            "texto: USAR_IA=0 genera texto local con hashtag",
            bool(texto.strip()) and tiene_hashtag(texto),
            repr(texto[:120]),
        )

    # IA devuelve vacio -> se usa el fallback local.
    with _env():
        with mock.patch(
            "ia.generador_contenido.generar_textos_mantenimiento",
            return_value=[["   "]],
        ), mock.patch(
            "ia.generador_contenido._fallback_estructura_mantenimiento",
            return_value=[["Texto local de respaldo para la comunidad"]],
        ):
            texto = calentamiento.generar_texto(cuenta)
        check(
            "texto: IA vacia -> usa el fallback local",
            "Texto local de respaldo" in texto and tiene_hashtag(texto),
            repr(texto[:120]),
        )

    # IA devuelve texto sin hashtag -> se lo agrega.
    with _env():
        with mock.patch(
            "ia.generador_contenido.generar_textos_mantenimiento",
            return_value=[["Hoy compartimos una reflexion sencilla"]],
        ):
            texto = calentamiento.generar_texto(cuenta)
        check(
            "texto: a la IA sin hashtag se le agrega uno",
            "Hoy compartimos una" in texto
            and "reflexion sencilla" in texto
            and tiene_hashtag(texto),
            repr(texto[:120]),
        )

    # La info que viaja respeta registro/perfil/personalidad de ESA cuenta.
    with _env():
        capturado = {}

        def _fake_mantenimiento(cuentas_info, **kwargs):
            capturado["info"] = cuentas_info[0]
            return [["Texto de prueba para la cuenta"]]

        with mock.patch(
            "ia.generador_contenido.generar_textos_mantenimiento", _fake_mantenimiento
        ):
            calentamiento.generar_texto(cuenta)
        info = capturado.get("info") or {}
        check(
            "texto: el perfil de la cuenta viaja a la IA",
            (info.get("usuario"), info.get("registro"), info.get("perfil"),
             info.get("tipo_accion")) == ("u_estilo", "ciudadana", "popular", "post"),
            repr(info),
        )
        check(
            "texto: personalidad/nombre de la cuenta viajan a la IA",
            info.get("personalidad") == "le gusta el futbol"
            and info.get("nombre") == "Una Ciudadana",
            repr(info),
        )


# --------------------------------------------------------------------------- #
# (e) programar_publicacion
# --------------------------------------------------------------------------- #
def test_programar_publicacion(check):
    print("(e) programar_publicacion: Tarea correcta y sin duplicar pendientes")
    with _env(), _estado_limpio():
        db = _FakeDB(cuentas=[_cuenta(42, "u_tarea")])
        texto = "Buen dia, aqui seguimos con la comunidad. #Comunidad en el medio"
        with _sesion_de(db), mock.patch.object(
            calentamiento, "generar_texto", lambda cuenta: texto
        ):
            resultado = calentamiento.programar_publicacion()

        check(
            "programar: devuelve dict con id y usuario",
            isinstance(resultado, dict)
            and resultado.get("usuario") == "u_tarea"
            and resultado.get("id") is not None,
            repr(resultado),
        )
        check(
            "programar: agrega exactamente UNA Tarea",
            len(db.agregadas) == 1,
            f"tareas={len(db.agregadas)}",
        )
        tarea = db.agregadas[0]
        check(
            "programar: tipo=post y plataforma=twitter",
            tarea.tipo == "post" and tarea.plataforma == "twitter",
            f"{tarea.tipo}/{tarea.plataforma}",
        )
        check(
            "programar: contenido = texto generado",
            _texto_de_tarea(tarea) == texto,
            repr(_texto_de_tarea(tarea)[:80]),
        )
        check(
            "programar: cuentas_ids = json.dumps([42])",
            tarea.cuentas_ids == json.dumps([42]),
            repr(tarea.cuentas_ids),
        )
        check(
            "programar: estado=pendiente y creada_por=None",
            tarea.estado == "pendiente" and tarea.creada_por is None,
            f"{tarea.estado}/{tarea.creada_por}",
        )
        check(
            "programar: imagen_path vacio",
            not str(tarea.imagen_path or "").strip(),
            repr(tarea.imagen_path),
        )
        ahora = datetime.now()
        check(
            "programar: fecha_hora dentro de los siguientes 5 min",
            ahora - timedelta(seconds=5)
            <= tarea.fecha_hora
            <= ahora + timedelta(minutes=5, seconds=5),
            repr(tarea.fecha_hora),
        )

        # No duplica: ya existe una tarea de post pendiente para esa cuenta.
        db2 = _FakeDB(
            cuentas=[_cuenta(42, "u_tarea")],
            tareas=[
                Tarea(
                    tipo="post",
                    plataforma="twitter",
                    cuentas_ids=json.dumps([42]),
                    estado="pendiente",
                    fecha_hora=datetime.now(),
                )
            ],
        )
        with _sesion_de(db2), mock.patch.object(
            calentamiento, "generar_texto", lambda cuenta: "otro texto #X aqui"
        ):
            repetida = calentamiento.programar_publicacion()
        check(
            "programar: con esa cuenta ya pendiente -> None (no duplica)",
            repetida is None and not db2.agregadas,
            repr(repetida),
        )

        # Con otra cuenta libre, salta a ella.
        db3 = _FakeDB(
            cuentas=[_cuenta(42, "u_ocupada"), _cuenta(43, "u_libre")],
            tareas=[
                Tarea(
                    tipo="post",
                    plataforma="twitter",
                    cuentas_ids=json.dumps([42]),
                    estado="pendiente",
                    fecha_hora=datetime.now(),
                )
            ],
        )
        with _sesion_de(db3), mock.patch.object(
            calentamiento, "generar_texto", lambda cuenta: "texto libre #Comunidad ok"
        ):
            otra = calentamiento.programar_publicacion()
        check(
            "programar: salta a la otra cuenta si la primera esta ocupada",
            isinstance(otra, dict) and otra.get("usuario") == "u_libre",
            repr(otra),
        )

        # Registro reciente: la cuenta con accion reciente se omite.
        db4 = _FakeDB(
            cuentas=[_cuenta(44, "u_reciente"), _cuenta(45, "u_fresca")],
            registros=[("u_reciente",)],
        )
        with _sesion_de(db4), mock.patch.object(
            calentamiento, "generar_texto", lambda cuenta: "texto fresco #Mexico ok"
        ):
            fresca = calentamiento.programar_publicacion()
        check(
            "programar: omite cuentas con RegistroAccion reciente",
            isinstance(fresca, dict) and fresca.get("usuario") == "u_fresca",
            repr(fresca),
        )

        # Sin cuentas elegibles -> None sin lanzar.
        db5 = _FakeDB(cuentas=[_cuenta(46, "sin_sesion", cookies_path="")])
        with _sesion_de(db5):
            ninguna = calentamiento.programar_publicacion()
        check(
            "programar: sin cuentas elegibles -> None",
            ninguna is None and not db5.agregadas,
            repr(ninguna),
        )

        # Sesion que revienta -> None sin lanzar.
        with mock.patch.object(
            calentamiento,
            "get_db_session",
            side_effect=RuntimeError("db caida"),
        ):
            rota = calentamiento.programar_publicacion()
        check("programar: DB que lanza -> None sin lanzar", rota is None, repr(rota))


# --------------------------------------------------------------------------- #
# (f) ejecutar_tanda_si_toca
# --------------------------------------------------------------------------- #
def test_ejecutar_tanda(check):
    print("(f) ejecutar_tanda_si_toca: apagado, campana, tanda e intervalo")

    # Apagado: no hace nada.
    with _env(CALENTAMIENTO_ACTIVO="0"), _estado_limpio():
        calentamiento._proxima_tanda = None
        with mock.patch.object(
            calentamiento, "programar_publicacion", side_effect=AssertionError("no")
        ):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        check(
            "tanda: ACTIVO=0 -> motivo desactivado",
            resultado.get("motivo") == "desactivado" and not resultado.get("programadas"),
            repr(resultado),
        )
        check(
            "tanda: ACTIVO=0 no toca la proxima ventana",
            calentamiento._proxima_tanda is None,
            repr(calentamiento._proxima_tanda),
        )

    # Campana activa: pausado incluso si ya tocaba.
    with _env(CALENTAMIENTO_MIN_MIN="1", CALENTAMIENTO_MAX_MIN="1"), _estado_limpio():
        calentamiento._proxima_tanda = time.monotonic() - 1
        with mock.patch.object(calentamiento, "campana_activa", lambda: True), \
             mock.patch.object(
                 calentamiento, "programar_publicacion", side_effect=AssertionError("no")
             ):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        check(
            "tanda: campana activa -> motivo campana_activa y 0 publicaciones",
            resultado.get("motivo") == "campana_activa"
            and resultado.get("programadas") == [],
            repr(resultado),
        )
        check(
            "tanda: la campana NO reprograma la ventana (no publica al terminar)",
            calentamiento._proxima_tanda < time.monotonic(),
            repr(calentamiento._proxima_tanda),
        )

    # Primera pasada: solo agenda (no publica).
    with _env(CALENTAMIENTO_MIN_MIN="1", CALENTAMIENTO_MAX_MIN="1"), _estado_limpio():
        calentamiento._proxima_tanda = None
        t0 = time.monotonic()
        with mock.patch.object(calentamiento, "campana_activa", lambda: False), \
             mock.patch.object(
                 calentamiento, "programar_publicacion", side_effect=AssertionError("no")
             ):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        t1 = time.monotonic()
        check(
            "tanda: primera pasada -> primera_espera sin publicar",
            resultado.get("motivo") == "primera_espera"
            and resultado.get("programadas") == [],
            repr(resultado),
        )
        check(
            "tanda: primera pasada agenda +1 min (dentro de rango)",
            calentamiento._proxima_tanda is not None
            and t0 + 60 <= calentamiento._proxima_tanda <= t1 + 60,
            repr(calentamiento._proxima_tanda),
        )

    # Ya tocaba: programa 2 publicaciones con pausa entre ellas y reprograma 5 min.
    with _env(
        CALENTAMIENTO_MIN_MIN="5",
        CALENTAMIENTO_MAX_MIN="5",
        CALENTAMIENTO_POSTS="2",
    ), _estado_limpio(), _sin_sleep():
        calentamiento._proxima_tanda = time.monotonic() - 1
        creadas = []

        def _fake_programar():
            creadas.append(len(creadas) + 1)
            return {"id": len(creadas), "usuario": f"u{len(creadas)}"}

        with mock.patch.object(calentamiento, "campana_activa", lambda: False), \
             mock.patch.object(calentamiento, "programar_publicacion", _fake_programar):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        check(
            "tanda: POSTS=2 -> programa 2 publicaciones",
            resultado.get("motivo") == "ok" and len(resultado.get("programadas", [])) == 2,
            repr(resultado),
        )
        check(
            "tanda: devuelve id/usuario de cada publicacion",
            [p.get("usuario") for p in resultado["programadas"]] == ["u1", "u2"],
            repr(resultado["programadas"]),
        )
        restante = calentamiento._proxima_tanda - time.monotonic()
        check(
            "tanda: el intervalo queda dentro del rango (5 min)",
            295 <= restante <= 301,
            f"{restante:.1f}s",
        )
        check(
            "tanda: se programaron exactamente 2 llamadas",
            creadas == [1, 2],
            repr(creadas),
        )

    # Sin cuentas elegibles: motivo sin_cuentas y aun asi reprograma.
    with _env(CALENTAMIENTO_MIN_MIN="2", CALENTAMIENTO_MAX_MIN="2"), _estado_limpio():
        calentamiento._proxima_tanda = time.monotonic() - 1
        with mock.patch.object(calentamiento, "campana_activa", lambda: False), \
             mock.patch.object(calentamiento, "programar_publicacion", lambda: None):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        restante = calentamiento._proxima_tanda - time.monotonic()
        check(
            "tanda: sin cuentas -> motivo sin_cuentas",
            resultado.get("motivo") == "sin_cuentas"
            and resultado.get("programadas") == [],
            repr(resultado),
        )
        check(
            "tanda: sin cuentas igual reprograma (no reintenta cada 60s)",
            115 <= restante <= 121,
            f"{restante:.1f}s",
        )

    # programar_publicacion que revienta: no lanza y reprograma.
    with _env(CALENTAMIENTO_MIN_MIN="3", CALENTAMIENTO_MAX_MIN="3"), _estado_limpio():
        calentamiento._proxima_tanda = time.monotonic() - 1
        with mock.patch.object(calentamiento, "campana_activa", lambda: False), \
             mock.patch.object(
                 calentamiento, "programar_publicacion", side_effect=RuntimeError("boom")
             ):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        restante = calentamiento._proxima_tanda - time.monotonic()
        check(
            "tanda: excepcion al programar -> NO lanza",
            isinstance(resultado, dict) and resultado.get("programadas") == [],
            repr(resultado),
        )
        check(
            "tanda: tras la excepcion reprograma la ventana (3 min)",
            175 <= restante <= 181,
            f"{restante:.1f}s",
        )

    # config_calentamiento que revienta: tampoco lanza.
    with _estado_limpio():
        calentamiento._proxima_tanda = None
        with mock.patch.object(
            calentamiento, "config_calentamiento", side_effect=RuntimeError("boom")
        ):
            resultado = calentamiento.ejecutar_tanda_si_toca()
        check(
            "tanda: config que lanza -> dict vacio sin lanzar",
            isinstance(resultado, dict) and resultado.get("programadas") == [],
            repr(resultado),
        )


def test_manager_gate(check):
    print("(g) SchedulerManager: gate con_calentamiento (standalone vs dashboard)")

    # Web: SchedulerManager() sin argumentos (como lo llama el dashboard).
    with _manager() as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "manager: SchedulerManager() sin argumentos no falla y NO registra calentamiento",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    with _manager(con_calentamiento=False) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "manager: con_calentamiento=False NO registra calentamiento_continuo",
            ids == {"verificar_tareas"},
            repr(sorted(ids)),
        )

    with _manager(con_calentamiento=True) as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "manager: con_calentamiento=True registra calentamiento_continuo",
            ids == {"verificar_tareas", "calentamiento_continuo"},
            repr(sorted(ids)),
        )
        job = next(j for j in sched.jobs if j["id"] == "calentamiento_continuo")
        intervalo = getattr(job.get("trigger"), "interval", None)
        check(
            "manager: el job de calentamiento corre cada 60s",
            intervalo is not None and intervalo.total_seconds() == 60,
            repr(intervalo),
        )
        check(
            "manager: el job usa max_instances=1, coalesce y replace_existing",
            callable(job.get("func"))
            and job.get("max_instances") == 1
            and job.get("coalesce") is True
            and job.get("replace_existing") is True,
            repr({k: job.get(k) for k in ("max_instances", "coalesce", "replace_existing")}),
        )
        with mock.patch.object(
            calentamiento, "ejecutar_tanda_si_toca", side_effect=RuntimeError("boom")
        ):
            try:
                obj._calentamiento()
                wrapper_ok = True
            except Exception:  # noqa: BLE001
                wrapper_ok = False
        check(
            "manager: el wrapper del calentamiento no lanza si el motor falla",
            wrapper_ok,
        )

    with _manager(con_calentamiento=True, CALENTAMIENTO_ACTIVO="0") as (obj, sched):
        ids = {job["id"] for job in sched.jobs}
        check(
            "manager: ACTIVO=0 ni con con_calentamiento=True registra",
            "calentamiento_continuo" not in ids,
            repr(sorted(ids)),
        )

    # Contrato de los call sites del dashboard (no deben pasar argumentos).
    archivos = []
    for ruta in sorted((RAIZ / "web").rglob("*.py")):
        texto = ruta.read_text(encoding="utf-8")
        if "SchedulerManager(" in texto:
            archivos.append(ruta.name)
            check(
                f"web: {ruta.name} sigue llamando SchedulerManager() sin argumentos",
                "SchedulerManager()" in texto and "con_calentamiento" not in texto,
                "",
            )
    check(
        "web: siguen existiendo los call sites del dashboard (>= 3)",
        len(archivos) >= 3,
        f"archivos={sorted(archivos)}",
    )


def test_verificar_tareas(check):
    print("(h) _verificar_tareas: difiere las tareas durante campana")

    # Sin campana: ejecuta la tarea pendiente y la completa.
    tarea = Tarea(
        tipo="post",
        plataforma="twitter",
        contenido="texto programado",
        cuentas_ids=json.dumps([1]),
        estado="pendiente",
        fecha_hora=datetime.now() - timedelta(minutes=1),
    )
    db = _FakeDB(tareas=[tarea])
    with _manager() as (obj, sched):
        obj.ejecutor = mock.MagicMock()
        with _sesion_manager(db), mock.patch.object(
            calentamiento, "campana_activa", lambda: False
        ):
            obj._verificar_tareas()
        check(
            "verificar: sin campana SI ejecuta la tarea pendiente",
            obj.ejecutor.ejecutar_tarea.call_count == 1,
            f"calls={obj.ejecutor.ejecutar_tarea.call_count}",
        )
        check(
            "verificar: la tarea ejecutada queda 'completada'",
            tarea.estado == "completada",
            repr(tarea.estado),
        )

    # Con campana fresca: NO ejecuta, la deja pendiente y no marca fallo.
    tarea2 = Tarea(
        tipo="post",
        plataforma="twitter",
        contenido="otro texto programado",
        cuentas_ids=json.dumps([2]),
        estado="pendiente",
        fecha_hora=datetime.now() - timedelta(minutes=1),
    )
    db2 = _FakeDB(tareas=[tarea2])
    with _manager() as (obj, sched):
        obj.ejecutor = mock.MagicMock()
        with _sesion_manager(db2), mock.patch.object(
            calentamiento, "campana_activa", lambda: True
        ):
            obj._verificar_tareas()
        check(
            "verificar: con campana NO ejecuta la tarea",
            obj.ejecutor.ejecutar_tarea.call_count == 0,
            f"calls={obj.ejecutor.ejecutar_tarea.call_count}",
        )
        check(
            "verificar: la tarea diferida sigue 'pendiente' sin resultado",
            tarea2.estado == "pendiente" and not (tarea2.resultado or ""),
            f"estado={tarea2.estado!r} resultado={tarea2.resultado!r}",
        )
        check(
            "verificar: con campana ni siquiera consulta la base de datos",
            db2.consultas == 0,
            f"consultas={db2.consultas}",
        )

    # Si falla la lectura del marcador, se sigue el flujo normal (o al menos
    # `_verificar_tareas` nunca propaga la excepcion).
    with _manager() as (obj, sched):
        obj.ejecutor = mock.MagicMock()
        with mock.patch.object(
            calentamiento, "campana_activa", side_effect=RuntimeError("boom")
        ):
            try:
                obj._verificar_tareas()
                error = None
            except Exception as e:  # noqa: BLE001
                error = e
        check(
            "verificar: campana_activa que lanza -> NO propaga",
            error is None,
            repr(error),
        )

    # El camino de fallo del ejecutor sigue igual (no lo toca el defer).
    tarea3 = Tarea(
        tipo="post",
        plataforma="twitter",
        contenido="texto que fallara",
        cuentas_ids=json.dumps([3]),
        estado="pendiente",
        fecha_hora=datetime.now() - timedelta(minutes=1),
    )
    db3 = _FakeDB(tareas=[tarea3])
    with _manager() as (obj, sched):
        ejecutor = mock.MagicMock()
        ejecutor.ejecutar_tarea.side_effect = RuntimeError("boom ejecutor")
        obj.ejecutor = ejecutor
        with _sesion_manager(db3), mock.patch.object(
            calentamiento, "campana_activa", lambda: False
        ):
            obj._verificar_tareas()
        check(
            "verificar: si el ejecutor falla la tarea queda 'fallida' (sin regresion)",
            tarea3.estado == "fallida" and "boom ejecutor" in (tarea3.resultado or ""),
            f"estado={tarea3.estado!r} resultado={tarea3.resultado!r}",
        )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_config(check)
    test_campana_activa(check)
    test_elegir_cuenta(check)
    test_generar_texto(check)
    test_programar_publicacion(check)
    test_ejecutar_tanda(check)
    test_manager_gate(check)
    test_verificar_tareas(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_calentamiento.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
