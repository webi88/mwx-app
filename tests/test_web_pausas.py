"""Tests de la PAUSA para activacion masiva (cuentas de clientes).

Regla de negocio (`core/pausas.py`): una cuenta con `pausada_activacion=True`
queda EXCLUIDA de campanas, RT/likes masivos y reparto por hora, pero SIGUE
funcionando en mantenimiento (publicar texto/IA) y calentamiento.

Cubren, sin Chrome y sin BD real (fakes + monkeypatch):

  - `_helpers.es_pausada_activacion` / `separar_pausadas` / `texto_pausadas`
    (objetos Cuenta, filas-dict y `core.pausas` ausente).
  - `_helpers.ejecutar_en_cuentas`: la pausada NO crea bot, suma `omitidas`
    con el detalle exacto "⏸️ @usuario — pausada para activación (solo
    mantenimiento)" y no registra nada; con `incluir_pausadas=True` SI pasa.
  - Call sites: `posts.py` pasa `incluir_pausadas=True` en sus 3 llamadas de
    mantenimiento; `rts.py`/`likes.py` filtran con `separar_pausadas` + aviso;
    `activacion_masiva.py` filtra campana y RESERVA; `reparto_hora.py` filtra
    el plan 3+3+3 y guarda `pausadas_excluidas`; `cuentas.py` trae
    `_cambiar_pausa`, los botones de pausa y la columna/filtro del inventario.
  - AppTest: pestana Estado (confirmacion + espia sin escrituras reales),
    Activacion Masiva (caption y usuarios del motor sin pausadas) y Reparto
    por Hora (render con fakes).

Los scripts de `AppTest.from_function` son SOLO ASCII (misma regla que
`test_ui_tiers_curva.py`).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES (definidos FUERA del script) =====================

class _BotFake:
    """Bot minimo para `ejecutar_en_cuentas` (nunca toca Chrome)."""

    cuenta_suspendida = False
    ultima_url_publicada = ""

    def __init__(self, usuario: str):
        self.usuario = usuario

    def accion_ok(self, *args, **kwargs):
        return True

    def cerrar(self):
        pass


class _FactoryFake:
    """Sustituye a PlataformaFactory: registra que bots se crearon."""

    llamadas: list = []

    @classmethod
    def crear_bot(cls, plataforma, usuario):
        cls.llamadas.append((plataforma, usuario))
        return _BotFake(usuario)


class _ListarFake:
    """Sustituye `cuentas._listar_cuentas` devolviendo filas fijas."""

    def __init__(self, filas):
        self.filas = list(filas or [])

    def __call__(self, *args, **kwargs):
        return list(self.filas)

    def clear(self):
        pass


class _EspiaPausa:
    """Espia de `cuentas._cambiar_pausa` (no toca la BD)."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, usuarios, pausada):
        self.llamadas.append((list(usuarios or []), bool(pausada)))
        return len(usuarios or [])


class _MotorFake:
    """Motor falso que captura los kwargs de ejecutar_por_roles."""

    capturados: list = []

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar(self, **kwargs):
        type(self).capturados.append(dict(kwargs))
        return {"total": 0, "exitosas": 0, "fallidas": 0, "rondas": 1}

    def ejecutar_por_roles(self, **kwargs):
        type(self).capturados.append(dict(kwargs))
        return {
            "total": 0,
            "exitosas": 0,
            "fallidas": 0,
            "rondas": 1,
            "por_rol": {},
            "detalles": [],
        }

    def snapshot_progreso(self):
        return {}


class _CuentasConPausasFake:
    """Cuentas activas simuladas: 6 normales + 2 pausadas."""

    def __call__(self, *args, **kwargs):
        roles = ("cita", "hashtags", "comentario", "rt")
        filas = []
        for i in range(6):
            filas.append(
                {
                    "usuario": f"activa_{i:02d}",
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": roles[i % len(roles)],
                    "tier_calidad": "",
                    "pausada_activacion": False,
                }
            )
        for usuario, rol in (("pausada_uno", "rt"), ("pausada_dos", "hashtags")):
            filas.append(
                {
                    "usuario": usuario,
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": rol,
                    "tier_calidad": "",
                    "pausada_activacion": True,
                }
            )
        return filas


def _fila_estado(usuario: str, pausada: bool = False) -> dict:
    """Fila del inventario para la pestana Estado."""
    return {
        "usuario": usuario,
        "email": "",
        "status": "active",
        "last_checked": "",
        "cookies": "si",
        "seccion": "",
        "seccion_etiqueta": "Sin asignar",
        "tipo_cuenta": "politica",
        "tipo_etiqueta": "Politica",
        "handle_actual": usuario,
        "nombre_mostrado": "",
        "nombre_propuesto": "",
        "handle_propuesto": "",
        "password": "",
        "user_agent": "",
        "sector": "",
        "grupo": "",
        "grupo_etiqueta": "sin grupo",
        "proxy": "",
        "activa": True,
        "avatar": False,
        "banner": False,
        "avatar_path": "",
        "banner_path": "",
        "perfil_personalidad": "",
        "personalidad": "",
        "tier_calidad": "",
        "tier_etiqueta": "",
        "rol_activacion": "rt",
        "pausada_activacion": bool(pausada),
    }


def _cuenta_estado(usuario: str, pausada: bool,
                   perfil: str = "formal") -> SimpleNamespace:
    """Cuenta minima para el AppTest de Reparto por Hora."""
    return SimpleNamespace(
        usuario=usuario,
        perfil_personalidad=perfil,
        tier_calidad="",
        pausada_activacion=bool(pausada),
    )


def _limpiar_marcador_campana():
    """Borra `data/.campana_activa` y libera el guard (limpieza de tests)."""
    from web.operaciones import activacion_masiva as am

    try:
        am._liberar_campana()
    except Exception:
        pass
    try:
        (RAIZ / "data" / ".campana_activa").unlink()
    except Exception:
        pass


def _esperar_capturas(minimo: int, timeout: float = 8.0) -> None:
    """Espera a que el motor falso capture (corre en un hilo)."""
    limite = time.time() + timeout
    while len(_MotorFake.capturados) < minimo and time.time() < limite:
        time.sleep(0.05)


# ===================== SCRIPTS DE APPTEST (SOLO ASCII) =====================

def _app_estado():
    from web.operaciones import cuentas

    cuentas._tab_estado()


def _app_roles():
    from web.operaciones import activacion_masiva as am

    am._por_roles()


def _app_reparto():
    from web.operaciones import reparto_hora as rh

    rh.render({})


# ===================== DRIVERS DE APPTEST =====================

def _app_test_estado() -> tuple:
    """AppTest de la pestana Estado: botones de pausa + confirmacion."""
    from streamlit.testing.v1 import AppTest

    from web.operaciones import cuentas

    espia = _EspiaPausa()
    datos = {
        "botones": set(),
        "warnings": [],
        "success_pausar": [],
        "success_quitar": [],
        "llamadas_sin_confirmar": [],
        "llamadas": [],
        "excepcion": "",
    }
    original_listar = cuentas._listar_cuentas
    original_pausa = cuentas._cambiar_pausa
    original_estado = cuentas._cambiar_estado
    original_eliminar = cuentas._eliminar_cuentas
    cuentas._listar_cuentas = _ListarFake(
        [
            _fila_estado("pausada_uno", pausada=True),
            _fila_estado("activa_dos", pausada=False),
        ]
    )
    cuentas._cambiar_pausa = espia
    cuentas._cambiar_estado = lambda usuarios, activa: 0
    cuentas._eliminar_cuentas = lambda usuarios: 0
    try:
        at = AppTest.from_function(_app_estado, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["botones"] = {b.key for b in at.button}

        # Sin confirmacion: aviso y NINGUNA llamada.
        at.button(key="btn_est_pausar").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["warnings"] = [str(w.value) for w in at.warning]
        datos["llamadas_sin_confirmar"] = list(espia.llamadas)
        if espia.llamadas:
            return False, "escribio sin confirmacion", datos

        # Con confirmacion: pausar y quitar pausa llaman al espia.
        at.checkbox(key="est_pausa_confirmar").check().run()
        at.button(key="btn_est_pausar").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["success_pausar"] = [str(s.value) for s in at.success]
        at.button(key="btn_est_quitar_pausa").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["llamadas"] = list(espia.llamadas)
        datos["success_quitar"] = [str(s.value) for s in at.success]
        return True, "", datos
    finally:
        cuentas._listar_cuentas = original_listar
        cuentas._cambiar_pausa = original_pausa
        cuentas._cambiar_estado = original_estado
        cuentas._eliminar_cuentas = original_eliminar


def _app_test_roles() -> tuple:
    """AppTest de Activacion Masiva: caption de pausadas + motor sin ellas."""
    from streamlit.testing.v1 import AppTest

    from activaciones import motor as motor_mod
    from web.operaciones import activacion_masiva as am

    datos = {"captions": [], "captura": {}, "excepcion": ""}
    original_cuentas = am._cargar_cuentas_con_roles
    original_reserva = am._cargar_reserva_usuarios
    original_motor = motor_mod.MotorActivacion
    am._cargar_cuentas_con_roles = _CuentasConPausasFake()
    am._cargar_reserva_usuarios = lambda *a, **k: []
    motor_mod.MotorActivacion = _MotorFake
    _MotorFake.capturados.clear()
    _limpiar_marcador_campana()
    try:
        at = AppTest.from_function(_app_roles, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["captions"] = [str(c.value) for c in at.caption]

        at.text_area(key="act_roles_urls").input(
            "https://x.com/a/status/1"
        ).run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        at.button(key="btn_act_roles_launch").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        _esperar_capturas(1)
        if _MotorFake.capturados:
            datos["captura"] = dict(_MotorFake.capturados[-1])
        return True, "", datos
    finally:
        am._cargar_cuentas_con_roles = original_cuentas
        am._cargar_reserva_usuarios = original_reserva
        motor_mod.MotorActivacion = original_motor
        _limpiar_marcador_campana()


def _app_test_reparto() -> tuple:
    """AppTest de Reparto por Hora con cuentas fakes (2 pausadas)."""
    from streamlit.testing.v1 import AppTest

    from web.operaciones import reparto_hora as rh

    datos = {"captions": [], "excepcion": ""}
    original_plataforma = rh.cuentas_por_plataforma
    rh.cuentas_por_plataforma = lambda *a, **k: [
        _cuenta_estado("activa_a", False),
        _cuenta_estado("activa_b", False, perfil="popular"),
        _cuenta_estado("pausada_x", True),
        _cuenta_estado("pausada_y", True),
    ]
    try:
        at = AppTest.from_function(_app_reparto, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["captions"] = [str(c.value) for c in at.caption]
        return True, "", datos
    finally:
        rh.cuentas_por_plataforma = original_plataforma


# ===================== SUITE =====================

def run(check):
    from web.operaciones import _helpers as helpers

    # ---------------- 1) Helpers puros (objetos y filas-dict) ----------------
    pausada_obj = SimpleNamespace(usuario="cliente", pausada_activacion=True)
    activa_obj = SimpleNamespace(usuario="propia", pausada_activacion=False)
    sin_campo = SimpleNamespace(usuario="nueva")
    check(
        "pausa helper: objeto Cuenta True/False/sin campo",
        helpers.es_pausada_activacion(pausada_obj) is True
        and helpers.es_pausada_activacion(activa_obj) is False
        and helpers.es_pausada_activacion(sin_campo) is False,
    )
    check(
        "pausa helper: cadenas '1'/'true' cuentan como pausa (core.pausas)",
        helpers.es_pausada_activacion(
            SimpleNamespace(pausada_activacion="1")
        )
        is True
        and helpers.es_pausada_activacion(
            SimpleNamespace(pausada_activacion="true")
        )
        is True,
    )
    check(
        "pausa helper: filas-dict con clave pausada_activacion",
        helpers.es_pausada_activacion({"pausada_activacion": True}) is True
        and helpers.es_pausada_activacion({"pausada_activacion": "1"}) is True
        and helpers.es_pausada_activacion({"pausada_activacion": False}) is False
        and helpers.es_pausada_activacion({"usuario": "x"}) is False,
    )
    elegibles, pausadas = helpers.separar_pausadas(
        [
            {"usuario": "a", "pausada_activacion": True},
            {"usuario": "b", "pausada_activacion": False},
            {"usuario": "c"},
        ]
    )
    check(
        "pausa helper: separar_pausadas default excluye y reporta",
        [f["usuario"] for f in elegibles] == ["b", "c"]
        and [f["usuario"] for f in pausadas] == ["a"],
    )
    completas, pausadas_inc = helpers.separar_pausadas(
        [
            {"usuario": "a", "pausada_activacion": True},
            {"usuario": "b", "pausada_activacion": False},
        ],
        incluir_pausadas=True,
    )
    check(
        "pausa helper: incluir_pausadas=True deja pasar todo (solo reporta)",
        len(completas) == 2
        and [f["usuario"] for f in completas] == ["a", "b"]
        and len(pausadas_inc) == 1,
    )
    check(
        "pausa helper: texto_pausadas vacio con 0/None y exacto con N",
        helpers.texto_pausadas(0) == ""
        and helpers.texto_pausadas(None) == ""
        and helpers.texto_pausadas(3)
        == (
            "⏸️ 3 cuentas pausadas para activación quedaron fuera "
            "(siguen en mantenimiento)"
        ),
    )
    # core.pausas ausente (version vieja): nada se filtra y nunca lanza.
    guardado = sys.modules.get("core.pausas")
    sys.modules["core.pausas"] = None
    try:
        viejo = helpers.es_pausada_activacion(pausada_obj)
        elegibles_v, pausadas_v = helpers.separar_pausadas([pausada_obj, activa_obj])
    finally:
        if guardado is None:
            sys.modules.pop("core.pausas", None)
        else:
            sys.modules["core.pausas"] = guardado
    check(
        "pausa helper: core.pausas ausente -> no filtra y no lanza",
        viejo is False and len(elegibles_v) == 2 and pausadas_v == [],
    )

    # ---------------- 2) ejecutar_en_cuentas: guard + detalle exacto ----------------
    import plataformas.base as base_mod

    original_factory = base_mod.PlataformaFactory
    original_registrar = helpers.registrar_accion
    registros: list = []

    def _registrar(usuario, tipo, estado, url="", detalle=""):
        registros.append((usuario, tipo, estado))

    base_mod.PlataformaFactory = _FactoryFake
    helpers.registrar_accion = _registrar
    try:
        cuentas = [
            SimpleNamespace(usuario="cliente", pausada_activacion=True),
            SimpleNamespace(usuario="propia", pausada_activacion=False),
        ]
        _FactoryFake.llamadas = []
        registros.clear()
        res = helpers.ejecutar_en_cuentas(
            cuentas, lambda bot: bot.accion_ok(), "twitter", None, None,
            tipo="rt",
        )
        check(
            "ejecutar: la pausada NO crea bot y la activa si",
            [u for _, u in _FactoryFake.llamadas] == ["propia"],
            str(_FactoryFake.llamadas),
        )
        check(
            "ejecutar: omitidas=1 con el detalle exacto de pausa",
            res["omitidas"] == 1
            and res["exitos"] == 1
            and res["fallidos"] == 0
            and res["detalles"][0]
            == "⏸️ @cliente — pausada para activación (solo mantenimiento)",
            ascii(res),
        )
        check(
            "ejecutar: la pausada no registra RegistroAccion",
            [u for u, _, _ in registros] == ["propia"],
            str(registros),
        )

        _FactoryFake.llamadas = []
        registros.clear()
        res_inc = helpers.ejecutar_en_cuentas(
            cuentas,
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="mantenimiento",
            incluir_pausadas=True,
        )
        check(
            "ejecutar: incluir_pausadas=True las deja pasar",
            [u for _, u in _FactoryFake.llamadas] == ["cliente", "propia"]
            and res_inc["exitos"] == 2
            and res_inc["omitidas"] == 0
            and [u for u, _, _ in registros] == ["cliente", "propia"],
            f"llamadas={_FactoryFake.llamadas}",
        )
    finally:
        base_mod.PlataformaFactory = original_factory
        helpers.registrar_accion = original_registrar

    # ---------------- 3) Fuente: call sites auditados ----------------
    fuente_helpers = (
        RAIZ / "web" / "operaciones" / "_helpers.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente helpers: parametro, filtro core.pausas y detalle exacto",
        "incluir_pausadas: bool = False" in fuente_helpers
        and "from core.pausas import esta_pausada" in fuente_helpers
        and "pausada para activación (solo mantenimiento)" in fuente_helpers
        and "def separar_pausadas" in fuente_helpers
        and "def es_pausada_activacion" in fuente_helpers
        and "def aviso_pausadas" in fuente_helpers,
    )
    fuente_posts = (
        RAIZ / "web" / "operaciones" / "posts.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente posts: las 3 llamadas de mantenimiento pasan incluir_pausadas=True",
        fuente_posts.count("incluir_pausadas=True") == 3
        and "Mantenimiento: las pausadas para activación SÍ publican"
        in fuente_posts,
        f"(veces={fuente_posts.count('incluir_pausadas=True')})",
    )
    fuente_rts = (
        RAIZ / "web" / "operaciones" / "rts.py"
    ).read_text(encoding="utf-8")
    fuente_likes = (
        RAIZ / "web" / "operaciones" / "likes.py"
    ).read_text(encoding="utf-8")
    fuente_reportar = (
        RAIZ / "web" / "operaciones" / "reportar.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente rts/likes: filtran con separar_pausadas + aviso (default del helper)",
        "separar_pausadas" in fuente_rts
        and "aviso_pausadas" in fuente_rts
        and "separar_pausadas" in fuente_likes
        and "aviso_pausadas" in fuente_likes
        and "incluir_pausadas" not in fuente_rts
        and "incluir_pausadas" not in fuente_likes,
    )
    check(
        "fuente reportar: usa el default (sin incluir_pausadas)",
        "ejecutar_en_cuentas" in fuente_reportar
        and "incluir_pausadas" not in fuente_reportar,
    )
    fuente_am = (
        RAIZ / "web" / "operaciones" / "activacion_masiva.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente activacion: campana y reserva excluyen pausadas con aviso",
        fuente_am.count("separar_pausadas(") >= 2
        and fuente_am.count("aviso_pausadas(") >= 2
        and "es_pausada_activacion(c)" in fuente_am
        and '"pausada_activacion"' in fuente_am,
    )
    fuente_rh = (
        RAIZ / "web" / "operaciones" / "reparto_hora.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente reparto: filtro en render + defensa en _preparar + resumen",
        "separar_pausadas" in fuente_rh
        and "aviso_pausadas" in fuente_rh
        and "texto_pausadas" in fuente_rh
        and '"pausadas_excluidas"' in fuente_rh,
    )
    fuente_cuentas = (
        RAIZ / "web" / "operaciones" / "cuentas.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente cuentas: update de pausa, botones, badge y filtro del inventario",
        "def _cambiar_pausa" in fuente_cuentas
        and "Cuenta.pausada_activacion" in fuente_cuentas
        and "btn_est_pausar" in fuente_cuentas
        and "btn_est_quitar_pausa" in fuente_cuentas
        and "⏸️ Pausar para activación" in fuente_cuentas
        and "def _etiqueta_pausa" in fuente_cuentas
        and '"pausa"' in fuente_cuentas
        and 'key="inv_pausa"' in fuente_cuentas,
    )

    # ---------------- 4) AppTest: pestana Estado ----------------
    ok, detalle, datos_estado = _app_test_estado()
    check(
        "estado AppTest: renderiza sin excepciones y con botones de pausa",
        ok and {"btn_est_pausar", "btn_est_quitar_pausa"} <= datos_estado["botones"],
        detalle or str(sorted(datos_estado["botones"])),
    )
    check(
        "estado AppTest: sin confirmacion avisa y NO escribe",
        ok
        and any("Marca la confirmación" in w for w in datos_estado["warnings"])
        and datos_estado["llamadas_sin_confirmar"] == [],
        ascii(datos_estado["llamadas_sin_confirmar"]),
    )
    check(
        "estado AppTest: pausar y quitar pausa escriben con el conteo",
        ok
        and [(len(u), p) for u, p in datos_estado["llamadas"]]
        == [(2, True), (2, False)]
        and any(
            "2 cuenta(s)" in s for s in datos_estado["success_pausar"]
        )
        and any(
            "2 cuenta(s)" in s for s in datos_estado["success_quitar"]
        ),
        ascii(datos_estado["llamadas"])
        + " | "
        + ascii(datos_estado["success_pausar"])
        + " | "
        + ascii(datos_estado["success_quitar"]),
    )

    # ---------------- 5) AppTest: Activacion Masiva ----------------
    ok, detalle, datos_roles = _app_test_roles()
    check(
        "activacion AppTest: render y lanzamiento sin excepciones",
        ok,
        detalle,
    )
    check(
        "activacion AppTest: caption con el conteo de pausadas",
        ok
        and any(
            "2 cuentas pausadas para activación quedaron fuera" in c
            for c in datos_roles["captions"]
        ),
        ascii(datos_roles["captions"])[:220],
    )
    captura = datos_roles.get("captura") or {}
    usuarios_motor = captura.get("usuarios") or []
    check(
        "activacion AppTest: el motor NO recibe cuentas pausadas",
        ok
        and "pausada_uno" not in usuarios_motor
        and "pausada_dos" not in usuarios_motor
        and "activa_00" in usuarios_motor
        and len(usuarios_motor) == 6,
        ascii(usuarios_motor),
    )

    # ---------------- 6) AppTest: Reparto por Hora ----------------
    ok, detalle, datos_rh = _app_test_reparto()
    check(
        "reparto AppTest: render sin excepciones",
        ok,
        detalle,
    )
    check(
        "reparto AppTest: caption con el conteo de pausadas excluidas",
        ok
        and any(
            "2 cuentas pausadas para activación quedaron fuera" in c
            for c in datos_rh["captions"]
        ),
        ascii(datos_rh["captions"])[:220],
    )
