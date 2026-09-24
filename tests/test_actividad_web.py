"""Tests del dashboard web de la operacion ACTIVIDAD ("✍️ Actividad").

La operacion es el MODO LEVE de campana: 3-4 tweets por cuenta con TODOS los
hashtags (a diferencia de la activacion masiva, intensiva y con subconjuntos
aleatorios). El motor (`MotorActivacion.ejecutar_actividad`) esta congelado;
estos tests cubren SOLO la pagina `web/operaciones/actividad.py` y su
integracion con la infraestructura de `activacion_masiva.py`.

Cubren, sin Chrome y sin BD real (fakes + monkeypatch):

  - `_normalizar_hashtags` (linea/coma/espacio, '#', unicos case-insensitive).
  - `_cuentas_tier_bloqueadas` (Tier 2 y Tier 3 quedan fuera de la actividad).
  - Fuente/regex: "✍️ Actividad" en OPCIONES/CATEGORIAS y en el dispatch de
    `web/app.py` (ANTES del prefijo generico "✍️" de Change.org); el cableado
    de `activacion_masiva.py` (limpieza de contexto + motor `ejecutar_actividad`
    + render de resultados).
  - AppTest de la pagina: 0 excepciones y controles presentes (hashtags,
    min/max de tweets, pausas, navegadores, pausadas).
  - Lanzamiento con motor falso: `ejecutar_actividad` recibe usuarios (sin
    pausadas por defecto), hashtags normalizados, posts_min/max, la tupla de
    pausas, max_browsers, permitir_pausadas, duracion_max_min, `callback` y un
    `threading.Event` en `cancelar`; el registro de campana queda `tipo=
    "actividad"` con `usa_cancelar=True`.
  - Con "incluir pausadas" el motor recibe `permitir_pausadas=True` y las
    cuentas pausadas en `usuarios`.
  - Sin hashtags NO se lanza: error visible y motor no llamado.
  - Al terminar, el panel persistente pinta las metricas del resumen
    (esperados/exitosas/omitidas/pausadas/tier) y la tabla por cuenta.

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows; misma regla que
`test_ui_tiers_curva.py`/`test_web_pausas.py`).
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES (definidos FUERA del script) =====================

class _MotorActividadFake:
    """Motor falso que captura los kwargs de `ejecutar_actividad`."""

    capturados: list = []
    resumen_final: dict = {
        "exitosas": 2,
        "fallidas": 1,
        "omitidas": 3,
        "pausadas_omitidas": 1,
        "pausadas_usuarios": ["pausada_uno"],
        "tier_omitidas": 2,
        "tier_omitidas_usuarios": ["tier2_uno", "tier3_uno"],
        "omitidas_por_tiempo": 1,
        "omitidas_por_cuota": 0,
        "sin_sesion": 0,
        "cancelada": False,
        "total_esperado": 6,
        "urls": ["https://x.com/activa_00/status/1"],
        "por_cuenta": {
            "activa_00": {
                "posts_objetivo": 3,
                "exitosas": 2,
                "fallidas": 1,
                "urls": ["https://x.com/activa_00/status/1"],
            }
        },
    }

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar_actividad(self, **kwargs):
        type(self).capturados.append(dict(kwargs))
        cb = kwargs.get("callback")
        if callable(cb):
            try:
                cb(1, 6, "activa_00", True)
            except Exception:
                pass
        return dict(type(self).resumen_final)

    def snapshot_progreso(self):
        return {}

    def solicitar_paro(self):
        pass


class _MotorViejoFake:
    """Motor sin `cancelar` ni `**kwargs` (simula backend viejo)."""

    def ejecutar_actividad(self, usuarios=None, hashtags=None, posts_min=3):
        return {}


class _CuentasActividadFake:
    """Cuentas simuladas: 6 normales + 2 pausadas + 1 Tier 2 + 1 Tier 3."""

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
        for usuario, tier in (("tier2_uno", "Tier 2"), ("tier3_uno", "tier3")):
            filas.append(
                {
                    "usuario": usuario,
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": "rt",
                    "tier_calidad": tier,
                    "pausada_activacion": False,
                }
            )
        return filas


def _limpiar_campana():
    """Limpia el guard/registro de campanas y el marcador de disco."""
    from web.operaciones import activacion_masiva as am

    try:
        am._limpiar_registro()
    except Exception:
        pass
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
    while len(_MotorActividadFake.capturados) < minimo and time.time() < limite:
        time.sleep(0.05)


def _esperar_campana_libre(timeout: float = 10.0) -> None:
    """Espera a que el hilo de la campana termine (guard liberado)."""
    from web.operaciones import activacion_masiva as am

    limite = time.time() + timeout
    while am._campana_en_curso() is not None and time.time() < limite:
        time.sleep(0.05)


# ===================== SCRIPT DE APPTEST (SOLO ASCII) =====================

def _app_actividad():
    from web.operaciones import actividad

    actividad.render({})


# ===================== DRIVER DE APPTEST =====================

def _app_test_actividad(hashtags="#mexico\n#futbol", incluir_pausadas=False,
                        lanzar=True, resumen_final=None,
                        desmarcar_limpieza=False) -> tuple:
    """AppTest de la operacion Actividad con cuentas y motor fakes.

    `resumen_final` activa el rerun de despues de la campana para verificar el
    panel final; en ese caso conviene `desmarcar_limpieza=True`: la limpieza
    diferida del contexto hace `pop` de la key del widget `actividad_contexto`
    entre runs y el AppTest (que lee el estado de los widgets del arbol
    anterior) lanza KeyError, una limitacion conocida del harness.
    """
    from streamlit.testing.v1 import AppTest

    from activaciones import motor as motor_mod
    from web.operaciones import activacion_masiva as am
    from web.operaciones import actividad

    datos = {
        "text_areas": set(),
        "text_inputs": set(),
        "number_inputs": set(),
        "checkboxes": set(),
        "errores": [],
        "advertencias": [],
        "captions": [],
        "markdown": [],
        "metricas": {},
        "tablas": [],
        "captura": {},
        "tipo_campana": "",
        "usa_cancelar": None,
        "excepcion": "",
    }
    original_cuentas = actividad._cargar_cuentas_con_roles
    original_motor = motor_mod.MotorActivacion
    original_resumen = dict(_MotorActividadFake.resumen_final)
    actividad._cargar_cuentas_con_roles = _CuentasActividadFake()
    motor_mod.MotorActivacion = _MotorActividadFake
    _MotorActividadFake.capturados.clear()
    if resumen_final is not None:
        _MotorActividadFake.resumen_final = dict(resumen_final)
    _limpiar_campana()
    try:
        at = AppTest.from_function(_app_actividad, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["text_areas"] = {t.key for t in at.text_area}
        datos["text_inputs"] = {t.key for t in at.text_input}
        datos["number_inputs"] = {n.key for n in at.number_input}
        datos["checkboxes"] = {c.key for c in at.checkbox}

        if hashtags is not None:
            at.text_area(key="actividad_hashtags").input(hashtags).run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return False, datos["excepcion"], datos
        if incluir_pausadas:
            at.checkbox(key="actividad_pausadas").check().run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return False, datos["excepcion"], datos
        if desmarcar_limpieza:
            at.checkbox(key="actividad_limpiar_contexto").uncheck().run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return False, datos["excepcion"], datos
        # Captions/markdown se recapturan DESPUES de interactuar (los textos
        # cambian al marcar "incluir pausadas" o al escribir los hashtags).
        datos["captions"] = [str(c.value) for c in at.caption]
        datos["markdown"] = [str(m.value) for m in at.markdown]
        if lanzar:
            at.button(key="btn_actividad_lanzar").click().run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return False, datos["excepcion"], datos
            _esperar_capturas(1)
            if _MotorActividadFake.capturados:
                datos["captura"] = dict(_MotorActividadFake.capturados[-1])
            entradas = list(am._CAMPANAS.values())
            if entradas:
                datos["tipo_campana"] = str(entradas[-1].get("tipo") or "")
                datos["usa_cancelar"] = entradas[-1].get("usa_cancelar")
        datos["errores"] = [str(e.value) for e in at.error]
        datos["advertencias"] = [str(w.value) for w in at.warning]

        # Panel final: tras terminar la campana, un rerun pinta el resumen con
        # las metricas de la actividad (via `_render_resultados`).
        if lanzar and resumen_final is not None:
            _esperar_campana_libre()
            at.run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return False, datos["excepcion"], datos
            datos["metricas"] = {
                str(m.label): str(m.value) for m in at.metric
            }
            datos["tablas"] = [
                [str(columna) for columna in df.value.columns]
                for df in at.dataframe
            ]
            datos["captions"] = [str(c.value) for c in at.caption]
        return True, "", datos
    finally:
        actividad._cargar_cuentas_con_roles = original_cuentas
        motor_mod.MotorActivacion = original_motor
        _MotorActividadFake.resumen_final = dict(original_resumen)
        _limpiar_campana()


# ===================== HELPERS DE FUENTE =====================

def _constantes_app() -> dict:
    """Constantes literales de `web/app.py` via AST (sin importar Streamlit)."""
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    constantes: dict = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                if isinstance(objetivo, ast.Name):
                    try:
                        constantes[objetivo.id] = ast.literal_eval(nodo.value)
                    except Exception:
                        pass
    return constantes


# ===================== SUITE =====================

def run(check):
    from activaciones.motor import MotorActivacion
    from web.operaciones import activacion_masiva as am
    from web.operaciones import actividad

    # ---------------- 1) Normalizacion de hashtags (helper puro) ----------------
    check(
        "actividad hashtags: linea/coma/espacio -> con '#' y unicos",
        actividad._normalizar_hashtags("#mexico, #futbol\n#MEXICO; #deportes")
        == ["#mexico", "#futbol", "#deportes"]
        and actividad._normalizar_hashtags("mexico futbol")
        == ["#mexico", "#futbol"],
    )
    check(
        "actividad hashtags: vacios/None/'#' sueltos -> []",
        actividad._normalizar_hashtags("") == []
        and actividad._normalizar_hashtags(None) == []
        and actividad._normalizar_hashtags("  # , ;; \n") == [],
    )
    check(
        "actividad hashtags: acepta listas y deduplica case-insensitive",
        actividad._normalizar_hashtags(["#Uno", "uno", "#DOS", ""])
        == ["#Uno", "#DOS"],
    )

    # ---------------- 2) Tier bloqueado (rol efectivo hashtags) ----------------
    filas_tier = [
        {"usuario": "t1", "tier_calidad": "tier1", "rol_activacion": "rt"},
        {"usuario": "t2", "tier_calidad": "Tier 2", "rol_activacion": "rt"},
        {"usuario": "t3", "tier_calidad": "Métricas", "rol_activacion": "rt"},
        {"usuario": "sin", "tier_calidad": "", "rol_activacion": "hashtags"},
        {
            "usuario": "t2h",
            "tier_calidad": "aged",
            "rol_activacion": "hashtags",
        },
    ]
    check(
        "actividad tier: Tier 2/Tier 3 bloqueadas aunque su rol guardado sea rt",
        actividad._cuentas_tier_bloqueadas(filas_tier) == ["t2", "t3", "t2h"],
        str(actividad._cuentas_tier_bloqueadas(filas_tier)),
    )
    check(
        "actividad tier: filas raras (None/sin usuario) no rompen",
        actividad._cuentas_tier_bloqueadas([None, "x", {}, {"usuario": ""}])
        == [],
    )

    # ---------------- 3) Cancelar soportado/antiguo ----------------
    check(
        "actividad motor: _motor_usa_cancelar mapea ejecutar_actividad",
        am._motor_usa_cancelar(_MotorActividadFake(), "actividad") is True
        and am._motor_usa_cancelar(_MotorViejoFake(), "actividad") is False,
    )

    # ---------------- 4) Registro en web/app.py (regex de fuente) ----------------
    fuente_app = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    constantes = _constantes_app()
    opciones = constantes.get("OPCIONES") or []
    categorias = constantes.get("CATEGORIAS") or []
    check(
        "app: la operacion Actividad esta en OPCIONES y en una categoria",
        "✍️ Actividad" in opciones
        and any("✍️ Actividad" in ops for _, ops in categorias),
        f"(opciones={len(opciones)})",
    )
    check(
        "app: la operacion vive en la categoria de automatizacion Twitter",
        any(
            "✍️ Actividad" in ops and "Automatización" in nombre
            for nombre, ops in categorias
        ),
    )
    check(
        "app: dispatch de Actividad importa la pagina y va ANTES del prefijo "
        "generico de Change.org",
        "from web.operaciones.actividad import render" in fuente_app
        and 'startswith("✍️ Actividad")' in fuente_app
        and fuente_app.index('startswith("✍️ Actividad")')
        < fuente_app.index('startswith("✍️")'),
    )

    # ---------------- 5) Cableado en activacion_masiva.py ----------------
    fuente_am = (
        RAIZ / "web" / "operaciones" / "activacion_masiva.py"
    ).read_text(encoding="utf-8")
    check(
        "activacion: limpieza de contexto, cancelar y render de actividad",
        '"actividad": ("actividad_contexto",)' in fuente_am
        and 'tipo_txt == "actividad"' in fuente_am
        and "mostrar_resultados_actividad" in fuente_am,
    )
    fuente_act = (
        RAIZ / "web" / "operaciones" / "actividad.py"
    ).read_text(encoding="utf-8")
    check(
        "actividad fuente: reutiliza panel/paro y llama al motor congelado",
        "def render(" in fuente_act
        and "ejecutar_actividad" in fuente_act
        and "_render_proceso_activo" in fuente_act
        and "_lanzar_con_opciones_velocidad" in fuente_act
        and "_panel_contexto_noticias" in fuente_act
        and "_motor_usa_cancelar" not in fuente_act
        and 'tipo="actividad"' in fuente_act
        and "pausa_entre_posts_seg" in fuente_act
        and "permitir_pausadas" in fuente_act
        and "btn_actividad_lanzar" in fuente_act,
    )
    check(
        "actividad motor real: sigue existiendo `ejecutar_actividad` "
        "(firma congelada)",
        callable(getattr(MotorActivacion, "ejecutar_actividad", None)),
    )

    # ---------------- 6) AppTest: render + lanzamiento ----------------
    ok, detalle, datos = _app_test_actividad(
        hashtags="#mexico\n#futbol", lanzar=True
    )
    check(
        "actividad AppTest: render sin excepciones",
        ok,
        detalle,
    )
    check(
        "actividad AppTest: controles presentes (hashtags, tweets min/max, "
        "pausas, navegadores, pausadas)",
        ok
        and {
            "actividad_hashtags",
            "actividad_contexto",
        }
        <= datos["text_areas"]
        and "actividad_menciones" in datos["text_inputs"]
        and {
            "actividad_posts_min",
            "actividad_posts_max",
            "actividad_pausa_min",
            "actividad_pausa_max",
            "actividad_navegadores",
            "actividad_duracion",
        }
        <= datos["number_inputs"]
        and {"actividad_pausadas", "actividad_limpiar_contexto"}
        <= datos["checkboxes"],
        f"text_areas={sorted(datos['text_areas'])}",
    )
    texto_markdown = " ".join(datos["markdown"])
    check(
        "actividad AppTest: aviso de pausadas y preview de publicaciones",
        ok
        and any(
            "2 cuentas pausadas para activación quedaron fuera" in c
            for c in datos["captions"]
        )
        and "Preview:" in texto_markdown
        and "6 cuentas" in texto_markdown
        and "~18 a 24" in texto_markdown,
        ascii(texto_markdown)[:240],
    )
    captura = datos.get("captura") or {}
    usuarios = captura.get("usuarios") or []
    check(
        "actividad AppTest: usuarios sin pausadas por defecto (8 de 10)",
        ok
        and len(usuarios) == 8
        and "activa_00" in usuarios
        and "tier2_uno" in usuarios
        and "pausada_uno" not in usuarios
        and "pausada_dos" not in usuarios,
        ascii(usuarios),
    )
    check(
        "actividad AppTest: hashtags normalizados (con '#', sin duplicados)",
        ok and captura.get("hashtags") == ["#mexico", "#futbol"],
        str(captura.get("hashtags")),
    )
    check(
        "actividad AppTest: posts min/max, pausa tupla, navegadores y duracion",
        ok
        and captura.get("posts_min") == 3
        and captura.get("posts_max") == 4
        and captura.get("pausa_entre_posts_seg") == (60, 240)
        and isinstance(captura.get("pausa_entre_posts_seg"), tuple)
        and captura.get("max_browsers") == 2
        and captura.get("duracion_max_min") == 120
        and captura.get("permitir_pausadas") is False,
        ascii(
            {
                clave: captura.get(clave)
                for clave in (
                    "posts_min",
                    "posts_max",
                    "pausa_entre_posts_seg",
                    "max_browsers",
                    "duracion_max_min",
                    "permitir_pausadas",
                )
            }
        ),
    )
    check(
        "actividad AppTest: cancelar=Event y callback presentes",
        ok
        and isinstance(captura.get("cancelar"), threading.Event)
        and callable(captura.get("callback")),
        f"cancelar={type(captura.get('cancelar')).__name__}",
    )
    check(
        "actividad AppTest: el registro de campana es tipo 'actividad' y "
        "usa cancelar",
        ok
        and datos.get("tipo_campana") == "actividad"
        and datos.get("usa_cancelar") is True,
        f"tipo={datos.get('tipo_campana')} usa_cancelar={datos.get('usa_cancelar')}",
    )

    # ---------------- 7) AppTest: marcador de campana limpio ----------------
    check(
        "actividad AppTest: el marcador data/.campana_activa quedo limpio",
        not (RAIZ / "data" / ".campana_activa").exists(),
    )

    # ---------------- 8) AppTest: incluir pausadas ----------------
    ok, detalle, datos_pausadas = _app_test_actividad(
        hashtags="#mexico", incluir_pausadas=True, lanzar=True
    )
    captura_pausadas = datos_pausadas.get("captura") or {}
    usuarios_pausadas = captura_pausadas.get("usuarios") or []
    check(
        "actividad AppTest: 'incluir pausadas' -> permitir_pausadas=True y "
        "las pausadas participan",
        ok
        and captura_pausadas.get("permitir_pausadas") is True
        and "pausada_uno" in usuarios_pausadas
        and "pausada_dos" in usuarios_pausadas
        and len(usuarios_pausadas) == 10,
        ascii(usuarios_pausadas),
    )
    check(
        "actividad AppTest: con pausadas incluidas el caption cambia",
        ok
        and any(
            "cuentas pausadas incluidas" in c
            for c in datos_pausadas["captions"]
        ),
        ascii(datos_pausadas["captions"])[:220],
    )

    # ---------------- 9) AppTest: sin hashtags NO lanza ----------------
    ok, detalle, datos_vacio = _app_test_actividad(
        hashtags="", lanzar=True
    )
    check(
        "actividad AppTest: sin hashtags no lanza (motor no llamado)",
        ok and not datos_vacio.get("captura"),
        str(datos_vacio.get("captura"))[:120],
    )
    check(
        "actividad AppTest: sin hashtags muestra error claro",
        ok
        and any(
            "hashtag" in e.lower() and "no se lanza" in e.lower()
            for e in datos_vacio["errores"]
        ),
        ascii(datos_vacio["errores"])[:220],
    )

    # ---------------- 10) AppTest: metricas finales del resumen ----------------
    ok, detalle, datos_final = _app_test_actividad(
        hashtags="#mexico",
        lanzar=True,
        desmarcar_limpieza=True,
        resumen_final=dict(_MotorActividadFake.resumen_final),
    )
    metricas_final = datos_final.get("metricas") or {}
    check(
        "actividad AppTest: al terminar se pintan las metricas del resumen",
        ok
        and {
            "🎯 Esperados",
            "✅ Exitosas",
            "❌ Fallidas",
            "➖ Omitidas",
            "⏸️ Pausadas fuera",
            "🧱📊 Tier fuera",
        }
        <= set(metricas_final)
        and metricas_final.get("🎯 Esperados") == "6"
        and metricas_final.get("✅ Exitosas") == "2",
        ascii(metricas_final)[:240],
    )
    check(
        "actividad AppTest: tabla por cuenta con objetivo/exitosas/fallidas",
        ok
        and any(
            {"Cuenta", "Objetivo", "✅ Exitosas", "❌ Fallidas"} <= set(columnas)
            for columnas in datos_final.get("tablas") or []
        ),
        ascii(str(datos_final.get("tablas")))[:200],
    )
    check(
        "actividad AppTest: nota de que los links estan en Reportes",
        ok
        and any(
            "Reportes" in c and "links" in c.lower()
            for c in datos_final["captions"]
        ),
        ascii(datos_final["captions"])[:220],
    )
