"""Tests de UI de Tiers, cuota diaria y Curva de Aceleracion (dashboard web).

Cubren, sin Chrome y sin BD real (fakes + monkeypatch), lo agregado en
`web/operaciones/cuentas.py` y `web/operaciones/activacion_masiva.py`:

  - Badges de Tier (Tier 1 / Tier 2 / sin asignar) y columna "Cuota hoy"
    (`usado/limite`, "Agotada por hoy" y "sin tope").
  - Validacion BLOQUEANTE Tier 2 + rol efectivo "hashtags" a nivel helper y a
    nivel vista: la pestana Tiers NO llama al guardado (espia que falla si se
    escribe) y muestra el error de `core.tiers.error_rol_tier`.
  - Pestana "🏅 Tiers" (AppTest con fakes): metricas, tabla, botones, bloqueo y
    asignaciones permitidas despues del bloqueo.
  - Inventario con las columnas nuevas "tier" y "cuota_hoy" (AppTest con fakes
    y conteos fake de `contar_acciones_dia_por_usuario`).
  - Activacion Masiva: controles de Curva de Aceleracion y respaldo presentes
    en AMBAS pestanas; al lanzar, un motor falso captura `curva_aceleracion`,
    `curva_fase1_min` y `reserva_usuarios`; sin el checkbox no se pasan; con
    Tier 2 + hashtags fijos la campana NO se lanza (motor no llamado) y se
    muestra el error bloqueante.

Los scripts de `AppTest.from_function` son SOLO ASCII: Streamlit escribe el
script temporal con la codificacion local de Windows y los acentos/emojis lo
rompen en silencio (misma regla que `test_dashboard_navegacion.py`).
"""
from __future__ import annotations

import random
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES/ESPÍAS (definidos FUERA del script) =====================

class _ListarFake:
    """Sustituye `cuentas._listar_cuentas` devolviendo filas fijas."""

    def __init__(self, filas):
        self.filas = list(filas or [])

    def __call__(self, *args, **kwargs):
        return list(self.filas)

    def clear(self):
        pass


class _EspiaTier:
    """Espia de `cuentas._asignar_tier`.

    Registra las llamadas y FALLA si alguien intenta escribir Tier 2 (asi una
    regresion del bloqueo rompe el test en vez de tocar la BD)."""

    def __init__(self):
        self.llamadas = []

    def __call__(self, usuarios, codigo):
        self.llamadas.append((list(usuarios or []), codigo))
        if codigo == "tier2":
            raise AssertionError("se intento escribir Tier 2 con hashtags")
        return len(usuarios or [])


class _MotorFake:
    """Motor falso que captura los kwargs de ejecutar/ejecutar_por_roles."""

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


class _CuentasRolesFake:
    """Cuentas activas simuladas para la pestana Por roles.

    Con `incluir_tier2=True` la cuenta del rol "hashtags" queda marcada como
    Tier 2 (escenario de bloqueo)."""

    def __init__(self, incluir_tier2: bool = False):
        self.incluir_tier2 = bool(incluir_tier2)

    def __call__(self, *args, **kwargs):
        roles = ("cita", "hashtags", "comentario", "rt")
        filas = []
        for i in range(12):
            filas.append(
                {
                    "usuario": f"cuenta_{i:03d}",
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": roles[i % len(roles)],
                    "tier_calidad": "",
                }
            )
        if self.incluir_tier2:
            for fila in filas:
                if fila["rol_activacion"] == "hashtags":
                    fila["tier_calidad"] = "Tier 2"
        return filas


def _reserva_fake(*args, **kwargs):
    """Respaldo simulado (evita consultar la BD real en los AppTest)."""
    return ["reserva_uno", "reserva_dos"]


def _fila_ui(usuario, tier, rol, tipo="politica"):
    """Fila del inventario con las columnas nuevas (`tier_calidad`/rol)."""
    return {
        "usuario": usuario,
        "email": "",
        "status": "active",
        "last_checked": "",
        "cookies": "si",
        "seccion": "",
        "seccion_etiqueta": "Sin asignar",
        "tipo_cuenta": tipo,
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
        "tier_calidad": tier,
        "tier_etiqueta": "",
        "rol_activacion": rol,
    }


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


# ===================== SCRIPTS DE APPTEST (SOLO ASCII) =====================

def _app_inventario():
    from web.operaciones import cuentas

    cuentas._tab_inventario()


def _app_tiers():
    import streamlit as st

    from web.operaciones import cuentas

    tab = [t for t in cuentas.TABS if "Tiers" in t][0]
    st.session_state["cuentas_modo_selector"] = cuentas._modo_de_tab(tab)
    st.session_state["cuentas_pestana_selector"] = tab
    cuentas.render({})


def _app_roles_launch():
    from web.operaciones import activacion_masiva as am

    am._por_roles()


def _app_cita_launch():
    from web.operaciones import activacion_masiva as am

    am._cita_masiva()


# ===================== DRIVERS DE APPTEST =====================

def _app_test_inventario() -> tuple:
    """AppTest del inventario: columnas tier/cuota hoy renderizadas."""
    from streamlit.testing.v1 import AppTest

    from web.operaciones import cuentas

    datos = {"columnas": [], "tier": [], "cuota": [], "excepcion": ""}
    original_listar = cuentas._listar_cuentas
    original_cuotas = cuentas._cuotas_dia_por_usuarios
    cuentas._listar_cuentas = _ListarFake(
        [
            _fila_ui("cuenta_t1", "tier1", "rt"),
            _fila_ui("cuenta_t2", "tier2", "rt"),
        ]
    )
    cuentas._cuotas_dia_por_usuarios = lambda usuarios: {
        "limite": 12,
        "usados": {"cuenta_t1": 3, "cuenta_t2": 12},
    }
    try:
        at = AppTest.from_function(_app_inventario, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        if at.dataframe:
            df = at.dataframe[0].value
            datos["columnas"] = [str(c) for c in df.columns]
            if "tier" in df.columns:
                datos["tier"] = [str(v) for v in df["tier"].tolist()]
            if "cuota_hoy" in df.columns:
                datos["cuota"] = [str(v) for v in df["cuota_hoy"].tolist()]
        return True, "", datos
    finally:
        cuentas._listar_cuentas = original_listar
        cuentas._cuotas_dia_por_usuarios = original_cuotas


def _app_test_tiers() -> tuple:
    """AppTest de la pestana Tiers: bloqueo + asignaciones permitidas."""
    from streamlit.testing.v1 import AppTest

    from web.operaciones import cuentas

    espia = _EspiaTier()
    datos = {
        "botones": set(),
        "tabla_columnas": [],
        "errores_bloqueo": [],
        "llamadas_tras_bloqueo": -1,
        "llamadas": [],
        "excepcion": "",
    }
    original_listar = cuentas._listar_cuentas
    original_asignar = cuentas._asignar_tier
    cuentas._listar_cuentas = _ListarFake(
        [
            _fila_ui("bloqueada_t2", "Tier 2", "hashtags"),
            _fila_ui("lider_t1", "tier1", "rt"),
            _fila_ui("sin_tier", "", "rt"),
        ]
    )
    cuentas._asignar_tier = espia
    try:
        at = AppTest.from_function(_app_tiers, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["botones"] = {b.key for b in at.button}
        if at.dataframe:
            datos["tabla_columnas"] = [str(c) for c in at.dataframe[0].value.columns]

        # Click bloqueante: Tier 2 con una cuenta hashtags NO debe escribir.
        at.button(key="btn_tier2").click().run()
        datos["errores_bloqueo"] = [str(e.value) for e in at.error]
        datos["llamadas_tras_bloqueo"] = len(espia.llamadas)

        # Despues del bloqueo se pueden hacer asignaciones validas.
        at.button(key="btn_tier_quitar").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        at.button(key="btn_tier1").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["llamadas"] = list(espia.llamadas)
        return True, "", datos
    finally:
        cuentas._listar_cuentas = original_listar
        cuentas._asignar_tier = original_asignar


def _app_test_roles_launch(con_curva: bool, incluir_tier2: bool = False) -> tuple:
    """AppTest de la pestana Por roles lanzando con un motor falso."""
    from streamlit.testing.v1 import AppTest

    from activaciones import motor as motor_mod
    from web.operaciones import activacion_masiva as am

    datos = {
        "checkboxes": set(),
        "number_inputs": set(),
        "captura": {},
        "errores": [],
        "advertencias": [],
        "excepcion": "",
    }
    original_cuentas = am._cargar_cuentas_con_roles
    original_reserva = am._cargar_reserva_usuarios
    original_motor = motor_mod.MotorActivacion
    am._cargar_cuentas_con_roles = _CuentasRolesFake(incluir_tier2)
    am._cargar_reserva_usuarios = _reserva_fake
    motor_mod.MotorActivacion = _MotorFake
    _MotorFake.capturados.clear()
    _limpiar_marcador_campana()
    try:
        at = AppTest.from_function(_app_roles_launch, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["checkboxes"] = {c.key for c in at.checkbox}
        datos["number_inputs"] = {n.key for n in at.number_input}

        at.text_area(key="act_roles_urls").input("https://x.com/a/status/1").run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        if con_curva:
            at.checkbox(key="act_roles_curva").check().run()
            at.number_input(key="act_roles_curva_fase1").set_value(20).run()
            datos["number_inputs"] = {n.key for n in at.number_input}
        at.button(key="btn_act_roles_launch").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["errores"] = [str(e.value) for e in at.error]
        datos["advertencias"] = [str(w.value) for w in at.warning]
        if _MotorFake.capturados:
            datos["captura"] = dict(_MotorFake.capturados[-1])
        return True, "", datos
    finally:
        am._cargar_cuentas_con_roles = original_cuentas
        am._cargar_reserva_usuarios = original_reserva
        motor_mod.MotorActivacion = original_motor
        _limpiar_marcador_campana()


def _app_test_cita_launch(con_curva: bool) -> tuple:
    """AppTest de la pestana Cita masiva lanzando con un motor falso."""
    from streamlit.testing.v1 import AppTest

    from activaciones import motor as motor_mod
    from web.operaciones import activacion_masiva as am

    datos = {"captura": {}, "excepcion": ""}
    original_cuentas = am._cargar_cuentas_con_roles
    original_reserva = am._cargar_reserva_usuarios
    original_motor = motor_mod.MotorActivacion
    am._cargar_cuentas_con_roles = _CuentasRolesFake(False)
    am._cargar_reserva_usuarios = _reserva_fake
    motor_mod.MotorActivacion = _MotorFake
    _MotorFake.capturados.clear()
    _limpiar_marcador_campana()
    try:
        at = AppTest.from_function(_app_cita_launch, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        at.text_area(key="act_urls").input("https://x.com/a/status/1").run()
        at.text_area(key="act_texto").input("Texto base").run()
        if con_curva:
            at.checkbox(key="act_curva").check().run()
            at.number_input(key="act_curva_fase1").set_value(25).run()
        at.button(key="btn_act").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        if _MotorFake.capturados:
            datos["captura"] = dict(_MotorFake.capturados[-1])
        return True, "", datos
    finally:
        am._cargar_cuentas_con_roles = original_cuentas
        am._cargar_reserva_usuarios = original_reserva
        motor_mod.MotorActivacion = original_motor
        _limpiar_marcador_campana()


# ===================== SUITE =====================

def run(check):
    from web.operaciones import activacion_masiva as am
    from web.operaciones import cuentas

    # ---------------- 1) Badge de Tier ----------------
    badge_t1 = cuentas._etiqueta_tier_badge("tier1")
    badge_t1b = cuentas._etiqueta_tier_badge("Líder")
    badge_t2 = cuentas._etiqueta_tier_badge("Tier 2")
    badge_t2b = cuentas._etiqueta_tier_badge("aged")
    check(
        "tiers badge: tier1 -> Tier 1 (Lider/Boosted)",
        badge_t1 == "🏅 Tier 1 (Líder/Boosted)"
        and badge_t1 == badge_t1b,
        ascii(badge_t1),
    )
    check(
        "tiers badge: tier2 -> Tier 2 (Volumen/Aged)",
        badge_t2 == "🧱 Tier 2 (Volumen/Aged)"
        and badge_t2 == badge_t2b,
        ascii(badge_t2),
    )
    check(
        "tiers badge: vacio/desconocido -> guion",
        cuentas._etiqueta_tier_badge("") == "—"
        and cuentas._etiqueta_tier_badge(None) == "—"
        and cuentas._etiqueta_tier_badge("basura") == "—",
    )
    check(
        "tiers: normalizar/normalizar_rol tolerantes a core viejo",
        cuentas._normalizar_tier_seguro("Tier 2") == "tier2"
        and cuentas._normalizar_rol_cuota_seguro("post") == "hashtags"
        and cuentas._normalizar_rol_cuota_seguro("Retweet con cita") == "cita",
    )

    # ---------------- 2) Cuota hoy ----------------
    check(
        "cuota: con tope -> 'usado/limite'",
        cuentas._etiqueta_cuota_dia(3, 12) == "3/12"
        and cuentas._etiqueta_cuota_dia("4", "12") == "4/12",
    )
    check(
        "cuota: usado >= limite -> Agotada por hoy",
        "🔴 Agotada por hoy" in cuentas._etiqueta_cuota_dia(12, 12)
        and "🔴 Agotada por hoy" in cuentas._etiqueta_cuota_dia(15, 12)
        and cuentas._etiqueta_cuota_dia(12, 12) == "12/12 · 🔴 Agotada por hoy",
    )
    check(
        "cuota: tope 0 -> 'sin tope' y NUNCA agotada",
        cuentas._etiqueta_cuota_dia(0, 0) == "sin tope"
        and cuentas._etiqueta_cuota_dia(99, 0) == "sin tope",
    )
    check(
        "cuota: _cuota_dia_usuario usa el mapa de conteos fake",
        cuentas._cuota_dia_usuario(
            "u", {"limite": 10, "usados": {"u": 10}}
        )
        == "10/10 · 🔴 Agotada por hoy"
        and cuentas._cuota_dia_usuario("u", {}) == "sin tope",
    )

    # ---------------- 3) Validacion Tier 2 + hashtags ----------------
    f_t2_hashtags = {"usuario": "t2h", "tier_calidad": "tier2", "rol_activacion": "hashtags"}
    f_t2_post = {"usuario": "t2p", "tier_calidad": "Tier 2", "rol_activacion": "post"}
    f_t2_rt = {"usuario": "t2r", "tier_calidad": "tier2", "rol_activacion": "rt"}
    f_t1_hashtags = {"usuario": "t1h", "tier_calidad": "tier1", "rol_activacion": "hashtags"}
    f_vacia_hashtags = {"usuario": "vh", "tier_calidad": "", "rol_activacion": "hashtags"}
    errores = cuentas._errores_tier2_hashtags(
        [f_t2_hashtags, f_t2_post, f_t2_rt, f_t1_hashtags, f_vacia_hashtags]
    )
    check(
        "bloqueo helper: solo Tier 2 con hashtags/post incumple",
        len(errores) == 2
        and all("PROHIBIDO el rol 'hashtags'" in m for m in errores),
        f"({len(errores)} errores)",
    )
    check(
        "bloqueo helper: mensaje de core.tiers.error_rol_tier exacto",
        errores
        and errores[0]
        == (
            "La cuenta @t2h (Tier 2 - Volumen/Aged) tiene PROHIBIDO el rol "
            "'hashtags'. Usa Tier 1 para posts originales o asígnale "
            "RT/Cita/Comentario."
        ),
    )
    check(
        "bloqueo helper: tolerante a filas raras (None/sin claves) -> []",
        cuentas._errores_tier2_hashtags([None, {}, {"usuario": "x"}]) == [],
    )
    check(
        "bloqueo activacion: filtra por usuarios/solo_roles y respeta aleatorio",
        len(
            am._bloqueo_tier2_hashtags(
                [f_t2_hashtags, f_t2_post, f_t2_rt, f_t1_hashtags]
            )
        )
        == 2
        and am._bloqueo_tier2_hashtags(
            [f_t2_hashtags], usuarios=["T1H"]
        )
        == []
        and am._bloqueo_tier2_hashtags(
            [f_t2_hashtags], solo_roles=["rt", "cita"]
        )
        == []
        and am._bloqueo_tier2_hashtags(
            [f_t2_hashtags], aleatorio=True
        )
        == [],
    )

    # ---------------- 4) Reserva de respaldo (helper puro) ----------------
    rng = random.Random(1234)
    reserva = am._seleccionar_reserva(
        ["@Uno", "dos", "UNO", "", None, "tres"],
        excluidos=["DOS"],
        maximo=2,
        rng=rng,
    )
    check(
        "reserva: limpia '@'/vacios/duplicados y excluye case-insensitive",
        len(reserva) == 2 and sorted(reserva) == ["Uno", "tres"],
        str(reserva),
    )
    check(
        "reserva: tope maximo (<=0 usa default 50) y limitacion real",
        len(am._seleccionar_reserva(["a", "b", "c"], maximo=1)) == 1
        and len(am._seleccionar_reserva(["a", "b", "c"], maximo=0)) == 3,
    )
    cuentas_fake = [{"usuario": f"u{i}"} for i in range(5)]
    check(
        "reserva: _excluidos_cita respeta cantidad y 'todas las cuentas'",
        set(am._excluidos_cita(cuentas_fake, 2, False)) == {"u0", "u1"}
        and set(am._excluidos_cita(cuentas_fake, 0, False))
        == {"u0", "u1", "u2", "u3", "u4"}
        and set(am._excluidos_cita(cuentas_fake, 2, True))
        == {"u0", "u1", "u2", "u3", "u4"},
    )
    check(
        "cuota: limite_acciones_dia configurado es un entero >= 0",
        isinstance(am._limite_diario_config(), int)
        and am._limite_diario_config() >= 0,
        f"(limite={am._limite_diario_config()})",
    )

    # ---------------- 5) Metricas opcionales del resumen ----------------
    check(
        "metricas: sin claves nuevas -> lista vacia",
        am._metricas_tier_curva({}) == []
        and am._metricas_tier_curva({"total": 3}) == [],
    )
    resumen = {
        "curva_aceleracion": True,
        "curva_fase1_min": 15,
        "fase_actual": 2,
        "cascada_urls": ["u1", "u2"],
        "tier2_hashtags_omitidas": 4,
        "rotadas_por_cuota_dia": 3,
        "agotadas_dia": 2,
        "reserva_usada": 2,
        "reserva_disponible": 5,
    }
    metricas = dict(am._metricas_tier_curva(resumen))
    check(
        "metricas: las 9 claves nuevas se convierten en metricas",
        len(metricas) == 9
        and metricas.get("🚀 Curva") == "Sí"
        and metricas.get("🚀 Fase 1 (min)") == 15
        and metricas.get("🚀 Fase final") == "2/2"
        and metricas.get("🔗 Cascada URLs") == 2
        and metricas.get("🧱 Tier 2 sin hashtags") == 4
        and metricas.get("♻️ Rotadas a respaldo") == 3
        and metricas.get("🛑 Agotadas por hoy") == 2
        and metricas.get("🛡️ Reserva usada") == 2
        and metricas.get("🛡️ Reserva disponible") == 5,
        ascii(metricas),
    )

    # ---------------- 6) Fuente: controles y pestana registrada ----------------
    fuente_am = (RAIZ / "web" / "operaciones" / "activacion_masiva.py").read_text(
        encoding="utf-8"
    )
    claves_widgets = (
        "act_curva",
        "act_curva_fase1",
        "act_reserva_rotar",
        "act_reserva_max",
        "act_roles_curva",
        "act_roles_curva_fase1",
        "act_roles_reserva_rotar",
        "act_roles_reserva_max",
    )
    faltantes = [c for c in claves_widgets if c not in fuente_am]
    check(
        "activacion fuente: los 8 widgets de curva/respaldo existen",
        not faltantes,
        str(faltantes) or "todos",
    )
    check(
        "activacion fuente: kwargs opcionales y guard _soporta_kwarg",
        "curva_aceleracion" in fuente_am
        and "curva_fase1_min" in fuente_am
        and "reserva_usuarios" in fuente_am
        and '_soporta_kwarg(func, "curva_aceleracion")' in fuente_am
        and '_soporta_kwarg(func, "reserva_usuarios")' in fuente_am,
    )
    fuente_cuentas = (RAIZ / "web" / "operaciones" / "cuentas.py").read_text(
        encoding="utf-8"
    )
    check(
        "cuentas fuente: pestana Tiers, helpers y columnas nuevas",
        "def _tab_tiers" in fuente_cuentas
        and '"🏅 Tiers"' in fuente_cuentas
        and "btn_tier2" in fuente_cuentas
        and "_errores_tier2_hashtags" in fuente_cuentas
        and "tier_calidad" in fuente_cuentas
        and '"cuota_hoy"' in fuente_cuentas,
    )
    tab_tiers = [t for t in cuentas.TABS if "Tiers" in t]
    check(
        "cuentas: la pestana Tiers esta en TABS, en un modo y mapeada en render()",
        len(tab_tiers) == 1
        and any(
            tab_tiers[0] in tabs for tabs in cuentas.MODOS_TABS.values()
        )
        and cuentas._modo_de_tab(tab_tiers[0])
        in cuentas.MODOS_TABS,
        ascii(tab_tiers),
    )

    # ---------------- 7) Inventario con columnas nuevas (AppTest) ----------------
    ok, detalle, datos_inv = _app_test_inventario()
    check(
        "inventario: renderiza sin excepciones (AppTest)",
        ok,
        detalle,
    )
    check(
        "inventario: columnas 'tier' y 'cuota_hoy' presentes",
        ok
        and "tier" in datos_inv["columnas"]
        and "cuota_hoy" in datos_inv["columnas"],
        str(datos_inv["columnas"]),
    )
    check(
        "inventario: badge de tier y cuota/agotada se pintan",
        ok
        and datos_inv["tier"]
        == ["🏅 Tier 1 (Líder/Boosted)", "🧱 Tier 2 (Volumen/Aged)"]
        and datos_inv["cuota"]
        == ["3/12", "12/12 · 🔴 Agotada por hoy"],
        ascii(f"tier={datos_inv['tier']} cuota={datos_inv['cuota']}"),
    )

    # ---------------- 8) Pestana Tiers: bloqueo y asignaciones (AppTest) ----------------
    ok, detalle, datos_tiers = _app_test_tiers()
    check(
        "tiers tab: renderiza sin excepciones (AppTest)",
        ok,
        detalle,
    )
    check(
        "tiers tab: botones de asignar/quitar y tabla con columnas",
        ok
        and {"btn_tier1", "btn_tier2", "btn_tier_quitar"} <= datos_tiers["botones"]
        and "tier" in datos_tiers["tabla_columnas"]
        and "rol efectivo" in datos_tiers["tabla_columnas"],
        f"botones={sorted(datos_tiers['botones'])}",
    )
    check(
        "tiers tab: Tier 2 + hashtags muestra error bloqueante de core.tiers",
        ok
        and any(
            "PROHIBIDO el rol 'hashtags'" in e and "Sistema no modificado" in e
            for e in datos_tiers["errores_bloqueo"]
        ),
        ascii(datos_tiers["errores_bloqueo"])[:200],
    )
    check(
        "tiers tab: el bloqueo NO llama a _asignar_tier (zero escrituras)",
        ok and datos_tiers["llamadas_tras_bloqueo"] == 0,
        f"(llamadas={datos_tiers['llamadas_tras_bloqueo']})",
    )
    check(
        "tiers tab: tras el bloqueo, quitar tier y asignar Tier 1 si escriben",
        ok
        and [codigo for _, codigo in datos_tiers["llamadas"]] == ["", "tier1"]
        and len(datos_tiers["llamadas"][0][0]) == 3,
        str(datos_tiers["llamadas"]),
    )

    # ---------------- 9) Activacion Masiva: curva + reserva (AppTest) ----------------
    ok, detalle, datos_roles = _app_test_roles_launch(con_curva=True)
    check(
        "activacion roles: render y lanzamiento sin excepciones (AppTest)",
        ok,
        detalle,
    )
    check(
        "activacion roles: checkbox de curva/fase1/respaldo presentes",
        ok
        and {"act_roles_curva", "act_roles_reserva_rotar"} <= datos_roles["checkboxes"]
        and {"act_roles_reserva_max", "act_roles_curva_fase1"}
        <= datos_roles["number_inputs"],
        f"checkboxes={sorted(datos_roles['checkboxes'])}",
    )
    captura = datos_roles.get("captura") or {}
    check(
        "activacion roles: el motor recibe curva_aceleracion/curva_fase1_min",
        captura.get("curva_aceleracion") is True
        and captura.get("curva_fase1_min") == 20,
        f"curva={captura.get('curva_aceleracion')} fase1={captura.get('curva_fase1_min')}",
    )
    check(
        "activacion roles: el motor recibe reserva_usuarios (respaldo)",
        captura.get("reserva_usuarios") == ["reserva_uno", "reserva_dos"],
        str(captura.get("reserva_usuarios")),
    )

    ok, detalle, datos_sin = _app_test_roles_launch(con_curva=False)
    captura_sin = datos_sin.get("captura") or {}
    check(
        "activacion roles: sin checkbox NO se pasan curva/fase1",
        ok
        and "curva_aceleracion" not in captura_sin
        and "curva_fase1_min" not in captura_sin
        and "act_roles_curva_fase1" not in datos_sin["number_inputs"],
        f"kwargs={sorted(captura_sin)}",
    )
    check(
        "activacion roles: sin checkbox la reserva sigue pasandose",
        captura_sin.get("reserva_usuarios") == ["reserva_uno", "reserva_dos"],
        str(captura_sin.get("reserva_usuarios")),
    )

    ok, detalle, datos_bloqueo = _app_test_roles_launch(
        con_curva=False, incluir_tier2=True
    )
    check(
        "activacion roles: Tier 2 + hashtags fijos NO lanza (motor no llamado)",
        ok and not datos_bloqueo.get("captura"),
        str(datos_bloqueo.get("captura")),
    )
    check(
        "activacion roles: muestra el error bloqueante y no pide URLs",
        ok
        and any(
            "PROHIBIDO el rol 'hashtags'" in e and "No se lanzó" in e
            for e in datos_bloqueo["errores"]
        )
        and not any(
            "Pega al menos una URL" in w for w in datos_bloqueo["advertencias"]
        ),
        ascii(datos_bloqueo["errores"])[:200],
    )

    ok, detalle, datos_cita = _app_test_cita_launch(con_curva=True)
    captura_cita = datos_cita.get("captura") or {}
    check(
        "activacion cita: render y lanzamiento sin excepciones (AppTest)",
        ok,
        detalle,
    )
    check(
        "activacion cita: el motor recibe curva/fase1/reserva",
        captura_cita.get("curva_aceleracion") is True
        and captura_cita.get("curva_fase1_min") == 25
        and captura_cita.get("reserva_usuarios")
        == ["reserva_uno", "reserva_dos"],
        str({k: captura_cita.get(k) for k in ("curva_aceleracion", "curva_fase1_min", "reserva_usuarios")}),
    )

    ok, detalle, datos_cita_sin = _app_test_cita_launch(con_curva=False)
    captura_cita_sin = datos_cita_sin.get("captura") or {}
    check(
        "activacion cita: sin checkbox no se pasan curva/fase1",
        ok
        and "curva_aceleracion" not in captura_cita_sin
        and "curva_fase1_min" not in captura_cita_sin,
        f"kwargs={sorted(captura_cita_sin)}",
    )
    check(
        "activacion: el marcador data/.campana_activa quedo limpio",
        not (RAIZ / "data" / ".campana_activa").exists(),
    )
