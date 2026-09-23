"""Tests del bloqueo de Tier 3 en los flujos DIRECTOS del dashboard.

Cubren, sin Chrome y sin BD real (fakes + monkeypatch), el hueco de EJECUCION:
una cuenta Tier 3 (Metricas/Soporte) SOLO puede RT/likes, jamas posts, citas ni
comentarios.

  - `web/operaciones/_helpers.{_rol_efectivo_ejecucion, bloqueo_tier_ejecucion}`
    (Tier 3 y Tier 2, tolerante a `core.tiers` viejo).
  - `_helpers.ejecutar_en_cuentas`: con rol prohibido NO se crea el bot (la
    factory registra/falla si se llama), suma `resultados["omitidas"]` y deja
    el detalle "⛔ ... PROHIBIDO (no se ejecutó)"; tampoco registra
    `RegistroAccion`. tier3 con rt/like si ejecuta; tier1/sin tier sin cambios.
  - Fuente: `rts._rt_cita` usa `tipo="cita"` (antes "activacion") y `likes.py`
    usa `tipo="like"` (antes el default "post").
  - `posts.py` (hilo y pool IA): guard minimo antes de crear `TwitterBot`.
  - `reparto_hora`: `_ritmo_por_cuenta` deja a las Tier 3 con 0 posts y 0
    comentarios conservando sus RTs; `_pool_por_usuario` alinea pool↔cuentas
    sin que las Tier 3 consuman textos; AppTest de `_preparar` con un pool falso.

Los scripts de `AppTest.from_function` son SOLO ASCII (misma regla que
`test_ui_tiers_curva.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES (definidos FUERA del script) =====================

class _CuentaFake(SimpleNamespace):
    """Cuenta minima para los helpers (atributos que leen los guards)."""


def _cuenta(usuario: str, tier: str = "", rol: str = "", **extra) -> _CuentaFake:
    datos = {
        "usuario": usuario,
        "tier_calidad": tier,
        "rol_activacion": rol,
        "activa": True,
    }
    datos.update(extra)
    return _CuentaFake(**datos)


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


# ===================== APPTEST DE `_preparar` (SOLO ASCII) =====================

def _app_preparar():
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import streamlit as st

    from web.operaciones import reparto_hora as rh

    original_rerun = st.rerun
    st.rerun = lambda *a, **k: None

    from ia import generador_contenido as gen

    original_pool = gen.generar_pool_campana_por_cuenta

    def _fake_pool(cuentas_info, n_posts=0, n_comentarios=0, n_citas=0,
                   base_cita="", callback=None, **kwargs):
        filas = []
        for info in cuentas_info:
            filas.append(
                {
                    "usuario": info["usuario"],
                    "posts": [f"post {info['usuario']} {i}" for i in range(3)],
                    "comentarios": [
                        f"com {info['usuario']} {i}" for i in range(3)
                    ],
                    "citas": [f"cita {info['usuario']} {i}" for i in range(3)],
                }
            )
        if callback:
            callback(len(filas), max(1, len(filas)))
        return filas

    gen.generar_pool_campana_por_cuenta = _fake_pool
    try:
        cuentas = [
            SimpleNamespace(
                usuario="granja", tier_calidad="tier3",
                perfil_personalidad="formal", tipo_cuenta="politica",
                personalidad="", seccion="LIB", nombre_mostrado="",
            ),
            SimpleNamespace(
                usuario="lider", tier_calidad="tier1",
                perfil_personalidad="formal", tipo_cuenta="politica",
                personalidad="", seccion="LIB", nombre_mostrado="",
            ),
        ]
        rh._preparar(
            cuentas,
            datetime.now() + timedelta(hours=1),
            3, 3, 3, 60,
            ["https://x.com/a/status/1"],
            ["https://x.com/a/status/2"],
            "",
        )
    finally:
        gen.generar_pool_campana_por_cuenta = original_pool
        st.rerun = original_rerun


def _app_test_preparar() -> tuple:
    """AppTest de `_preparar` con el generador de textos falso."""
    from streamlit.testing.v1 import AppTest

    datos = {"plan": [], "resumen": {}, "pool": {}, "excepcion": ""}
    at = AppTest.from_function(_app_preparar, default_timeout=90)
    at.run()
    if at.exception:
        datos["excepcion"] = str(at.exception[0].value)[:200]
        return False, datos["excepcion"], datos
    try:
        datos["plan"] = list(at.session_state["rh_plan"] or [])
        datos["resumen"] = dict(at.session_state["rh_resumen"] or {})
        datos["pool"] = dict(at.session_state["rh_pool"] or {})
    except Exception as e:  # pragma: no cover - diagnostico
        datos["excepcion"] = f"{type(e).__name__}: {e}"
        return False, datos["excepcion"], datos
    return True, "", datos


# ===================== SUITE =====================

def run(check):
    from web.operaciones import _helpers as helpers
    from web.operaciones import reparto_hora as rh

    # ---------------- 1) Rol efectivo del `tipo` ----------------
    check(
        "rol efectivo: posts/mantenimiento/hilo -> hashtags; cita/comentario/rt",
        helpers._rol_efectivo_ejecucion("post") == "hashtags"
        and helpers._rol_efectivo_ejecucion("mantenimiento") == "hashtags"
        and helpers._rol_efectivo_ejecucion("hilo") == "hashtags"
        and helpers._rol_efectivo_ejecucion("cita") == "cita"
        and helpers._rol_efectivo_ejecucion("comentario") == "comentario"
        and helpers._rol_efectivo_ejecucion("rt") == "rt"
        and helpers._rol_efectivo_ejecucion("like") == "like",
    )
    check(
        "rol efectivo: calentamiento se trata como rt (RT+likes) y vacio ''",
        helpers._rol_efectivo_ejecucion("calentamiento") == "rt"
        and helpers._rol_efectivo_ejecucion("Calentamiento") == "rt"
        and helpers._rol_efectivo_ejecucion("") == ""
        and helpers._rol_efectivo_ejecucion(None) == ""
        and helpers._rol_efectivo_ejecucion("visualizacion") == "visualizacion",
    )

    # ---------------- 2) Guard por tier ----------------
    t3 = _cuenta("granja", "tier3")
    for tipo in ("post", "mantenimiento", "hilo", "cita", "comentario"):
        mensaje = helpers.bloqueo_tier_ejecucion(t3, tipo)
        check(
            f"guard tier3: tipo '{tipo}' bloqueado con mensaje claro",
            mensaje.startswith("⛔ @granja")
            and "Tier 3" in mensaje
            and "PROHIBIDO" in mensaje
            and "no se ejecutó" in mensaje,
            ascii(mensaje),
        )
    for tipo in ("rt", "like", "calentamiento", "visualizacion", "", None,
                 "basura"):
        check(
            f"guard tier3: tipo '{tipo}' permitido (sin bloqueo)",
            helpers.bloqueo_tier_ejecucion(t3, tipo) == "",
        )
    t2 = _cuenta("volumen", "Tier 2")
    check(
        "guard tier2: hashtags/post bloqueado; rt/cita/comentario/calent. pasan",
        "Tier 2" in helpers.bloqueo_tier_ejecucion(t2, "post")
        and "PROHIBIDO" in helpers.bloqueo_tier_ejecucion(t2, "hashtags")
        and helpers.bloqueo_tier_ejecucion(t2, "rt") == ""
        and helpers.bloqueo_tier_ejecucion(t2, "cita") == ""
        and helpers.bloqueo_tier_ejecucion(t2, "comentario") == ""
        and helpers.bloqueo_tier_ejecucion(t2, "calentamiento") == "",
    )
    check(
        "guard tier1/sin tier/basura: nunca bloquea",
        helpers.bloqueo_tier_ejecucion(_cuenta("lider", "tier1"), "post") == ""
        and helpers.bloqueo_tier_ejecucion(_cuenta("nueva", ""), "post") == ""
        and helpers.bloqueo_tier_ejecucion(_cuenta("rara", "otro"), "cita") == "",
    )

    # Core viejo: `import core.tiers` roto -> SIN bloqueo (nunca lanza).
    guardado = sys.modules.get("core.tiers")
    sys.modules["core.tiers"] = None
    try:
        core_viejo = helpers.bloqueo_tier_ejecucion(t3, "post")
    finally:
        if guardado is None:
            sys.modules.pop("core.tiers", None)
        else:
            sys.modules["core.tiers"] = guardado
    check(
        "guard core viejo: devuelve '' (sin bloqueo) y no propaga excepcion",
        core_viejo == "",
    )

    # ---------------- 3) ejecutar_en_cuentas: sin bot y con omitidas ----------------
    import plataformas.base as base_mod

    original_factory = base_mod.PlataformaFactory
    original_registrar = helpers.registrar_accion
    registros: list = []

    def _registrar(usuario, tipo, estado, url="", detalle=""):
        registros.append((usuario, tipo, estado))

    base_mod.PlataformaFactory = _FactoryFake
    helpers.registrar_accion = _registrar
    _FactoryFake.llamadas = []
    try:
        cuentas = [
            _cuenta("t3_post", "tier3"),
            _cuenta("t1_ok", "tier1"),
            _cuenta("t2_post", "tier2"),
        ]
        res = helpers.ejecutar_en_cuentas(
            cuentas,
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="mantenimiento",
        )
        check(
            "ejecutar: tier3/tier2 con post NO crean bot (factory no llamada)",
            [u for _, u in _FactoryFake.llamadas] == ["t1_ok"],
            str(_FactoryFake.llamadas),
        )
        check(
            "ejecutar: omitidas=2, exitos=1, fallidos=0 y detalles bloqueados",
            res["omitidas"] == 2
            and res["exitos"] == 1
            and res["fallidos"] == 0
            and sum(1 for d in res["detalles"] if "PROHIBIDO" in d) == 2,
            ascii(res),
        )
        check(
            "ejecutar: cuentas bloqueadas NO registran RegistroAccion",
            [u for u, _, _ in registros] == ["t1_ok"],
            str(registros),
        )

        _FactoryFake.llamadas = []
        res_rt = helpers.ejecutar_en_cuentas(
            [_cuenta("t3_rt", "tier3")],
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="rt",
        )
        res_like = helpers.ejecutar_en_cuentas(
            [_cuenta("t3_like", "tier3")],
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="like",
        )
        check(
            "ejecutar: tier3 con rt/like SI ejecuta (sin omitidas)",
            [u for _, u in _FactoryFake.llamadas] == ["t3_rt", "t3_like"]
            and res_rt["exitos"] == 1
            and res_rt["omitidas"] == 0
            and res_like["exitos"] == 1
            and res_like["omitidas"] == 0,
            f"llamadas={_FactoryFake.llamadas}",
        )
    finally:
        base_mod.PlataformaFactory = original_factory
        helpers.registrar_accion = original_registrar

    # Con `core.tiers` viejo el flujo sigue igual (sin bloqueo).
    guardado = sys.modules.get("core.tiers")
    sys.modules["core.tiers"] = None
    base_mod.PlataformaFactory = _FactoryFake
    helpers.registrar_accion = _registrar
    _FactoryFake.llamadas = []
    try:
        res_viejo = helpers.ejecutar_en_cuentas(
            [_cuenta("t3_post", "tier3")],
            lambda bot: bot.accion_ok(),
            "twitter",
            None,
            None,
            tipo="post",
        )
    finally:
        base_mod.PlataformaFactory = original_factory
        helpers.registrar_accion = original_registrar
        if guardado is None:
            sys.modules.pop("core.tiers", None)
        else:
            sys.modules["core.tiers"] = guardado
    check(
        "ejecutar core viejo: tier3 con post se ejecuta (sin bloqueo)",
        [u for _, u in _FactoryFake.llamadas] == ["t3_post"]
        and res_viejo["omitidas"] == 0,
        str(_FactoryFake.llamadas),
    )

    # ---------------- 4) Fuente: tipos correctos y guards ----------------
    fuente_rts = (RAIZ / "web" / "operaciones" / "rts.py").read_text(
        encoding="utf-8"
    )
    fuente_likes = (RAIZ / "web" / "operaciones" / "likes.py").read_text(
        encoding="utf-8"
    )
    fuente_posts = (RAIZ / "web" / "operaciones" / "posts.py").read_text(
        encoding="utf-8"
    )
    fuente_helpers = (RAIZ / "web" / "operaciones" / "_helpers.py").read_text(
        encoding="utf-8"
    )
    fuente_reparto = (
        RAIZ / "web" / "operaciones" / "reparto_hora.py"
    ).read_text(encoding="utf-8")
    check(
        "fuente rts: _rt_cita pasa tipo='cita' (ya no 'activacion')",
        'estado, tipo="cita")' in fuente_rts
        and 'tipo="activacion"' not in fuente_rts,
    )
    check(
        "fuente likes: pasa tipo='like' (ya no el default 'post')",
        'estado, tipo="like"' in fuente_likes,
    )
    check(
        "fuente helpers: helpers nuevos, omitidas y mensaje de bloqueo",
        "def _rol_efectivo_ejecucion" in fuente_helpers
        and "def bloqueo_tier_ejecucion" in fuente_helpers
        and '"omitidas": 0' in fuente_helpers
        and "no se ejecutó" in fuente_helpers,
    )
    check(
        "fuente posts: guard en hilo y pool IA (2 llamadas) + omitidas",
        "bloqueo_tier_ejecucion" in fuente_posts
        and fuente_posts.count("bloqueo_tier_ejecucion(cuenta,") == 2
        and '"omitidas": 0' in fuente_posts,
    )
    check(
        "fuente reparto: helper de ritmo/pool y resumen tier3_limitadas",
        "def _ritmo_por_cuenta" in fuente_reparto
        and "def _pool_por_usuario" in fuente_reparto
        and "tier3_limitadas" in fuente_reparto
        and "Tier 3" in fuente_reparto,
    )

    # ---------------- 5) Reparto: ritmo y mapeo del pool ----------------
    t3 = _cuenta("granja", "tier3")
    t1 = _cuenta("lider", "tier1")
    t2 = _cuenta("volumen", "tier2")
    ritmo = rh._ritmo_por_cuenta([t3, t1, t2], 3, 3, 3)
    check(
        "ritmo: tier3 -> 0 posts/0 comentarios y sus RTs intactos",
        ritmo.get("granja") == (0, 0, 3),
        str(ritmo),
    )
    check(
        "ritmo: tier1/tier2 no cambian (3,3,3)",
        ritmo.get("lider") == (3, 3, 3) and ritmo.get("volumen") == (3, 3, 3),
    )
    from datetime import datetime

    from scheduler.distribucion_horaria import plan_hora_cuenta

    posts_c, com_c, rts_c = ritmo["granja"]
    plan_t3 = plan_hora_cuenta(
        "granja", "formal", datetime.now(), n_posts=posts_c,
        n_comentarios=com_c, n_rts=rts_c,
    )
    tipos_t3 = [p["tipo"] for p in plan_t3]
    check(
        "ritmo: el plan real de una tier3 solo tiene retweets (3/3)",
        len(plan_t3) == 3
        and tipos_t3 == ["retweet", "retweet", "retweet"],
        str(tipos_t3),
    )

    pool = [
        {"posts": ["a1"], "comentarios": ["a2"], "citas": ["a3"]},
        {"posts": ["b1"], "comentarios": ["b2"], "citas": ["b3"]},
    ]
    info = [{"usuario": "lider"}, {"usuario": "ciudadana"}]
    mapeado = rh._pool_por_usuario(pool, info)
    check(
        "pool: mapea pool[i] con cuentas_info[i] por usuario",
        mapeado.get("lider") == pool[0] and mapeado.get("ciudadana") == pool[1],
        str(mapeado),
    )
    mapeado_t3 = rh._pool_por_usuario(
        [
            {"posts": ["a1"]},
            {"posts": ["b1"]},
            {"posts": ["c1"]},
        ],
        [{"usuario": "lider"}, {"usuario": "ciudadana"}],
    )
    check(
        "pool: la tier3 (fuera de cuentas_info) no consume ni recibe textos",
        "granja" not in mapeado_t3
        and mapeado_t3.get("lider") == {"posts": ["a1"]}
        and mapeado_t3.get("ciudadana") == {"posts": ["b1"]},
        str(mapeado_t3),
    )
    check(
        "pool: tolerante a pool raro (None/no-lista) -> {}",
        rh._pool_por_usuario(None, info) == {}
        and rh._pool_por_usuario({"x": 1}, info) == {},
    )

    # Core viejo: el ritmo de la tier3 no se recorta (sin bloqueo).
    guardado = sys.modules.get("core.tiers")
    sys.modules["core.tiers"] = None
    try:
        ritmo_viejo = rh._ritmo_por_cuenta([t3], 3, 3, 3)
    finally:
        if guardado is None:
            sys.modules.pop("core.tiers", None)
        else:
            sys.modules["core.tiers"] = guardado
    check(
        "ritmo core viejo: sin bloqueo (3,3,3)",
        ritmo_viejo.get("granja") == (3, 3, 3),
    )

    # ---------------- 6) AppTest de `_preparar` ----------------
    ok, detalle, datos = _app_test_preparar()
    check(
        "reparto AppTest: _preparar corre sin excepciones (pool falso)",
        ok,
        detalle,
    )
    resumen = datos.get("resumen") or {}
    plan = datos.get("plan") or []
    acciones_granja = [p for p in plan if p.get("usuario") == "granja"]
    acciones_lider = [p for p in plan if p.get("usuario") == "lider"]
    check(
        "reparto AppTest: tier3 solo retweets (0 posts/0 comentarios) y resumen",
        ok
        and all(p.get("tipo") == "retweet" for p in acciones_granja)
        and len(acciones_granja) == 3
        and resumen.get("tier3_limitadas") == 1
        and resumen.get("posts") == 3
        and resumen.get("comentarios") == 3
        and resumen.get("retweets") == 6,
        ascii(resumen),
    )
    check(
        "reparto AppTest: la tier1 conserva 3 posts + 3 comentarios + 3 RTs",
        ok
        and len(acciones_lider) == 9
        and sum(1 for p in acciones_lider if p["tipo"] == "post") == 3
        and sum(1 for p in acciones_lider if p["tipo"] == "comentario") == 3
        and sum(1 for p in acciones_lider if p["tipo"] == "retweet") == 3,
        str(len(acciones_lider)),
    )
    pool_sesion = datos.get("pool") or {}
    check(
        "reparto AppTest: la tier3 queda sin textos (pool vacio)",
        ok
        and (pool_sesion.get("granja") or {}).get("posts") == []
        and (pool_sesion.get("granja") or {}).get("comentarios") == []
        and (pool_sesion.get("granja") or {}).get("citas") == []
        and len(((pool_sesion.get("lider") or {}).get("posts") or [])) == 3,
        ascii(pool_sesion),
    )
