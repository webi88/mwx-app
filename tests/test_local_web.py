"""Tests de la operacion dashboard "🖥️ Local (flota)".

Cubren, SIN Chrome y SIN BD real (fakes + monkeypatch):

  - Helpers PUROS de `web/operaciones/local.py`:
      * `_formatear_evento` (✅/❌/➖, usuario, rol, detalle y link).
      * `_validar_formulario` (sin cuentas, sin material, sin tope por
        defecto con 6 cuentas, tope explicito `maximo=5` por compatibilidad,
        válido, aviso de "solo texto" cuando no hay imágenes).
      * `_defaults_cuentas` (sin límite = todas las candidatas; `maximo=5`
        recorta a 5).
      * `_resumen_metricas` (8 métricas siempre presentes, 0 por defecto).
      * Extras: sesión real de la cuenta (mismos criterios que el scheduler),
        URLs por línea y hashtags normalizados.
  - AppTest de la pagina con cuentas y backend FALSOS (nunca se pulsa
    "▶️ Ejecutar en local": no se lanzan hilos reales): 0 excepciones,
    widgets/valores por defecto y panel final con la tabla/eventos y el botón
    "🪟 Cerrar navegador local" (que llama al fake del backend).
  - Registro en `web/app.py`: operación en OPCIONES y en la categoría de
    automatización, branch de despacho y backend importado PEREZOSAMENTE
    (nunca a nivel de módulo) en `local.py`.

El backend real (`activaciones/local.py`) se crea en paralelo: aqui se inyecta
un modulo falso en `sys.modules` y se retira al terminar para no contaminar el
resto de la suite.
"""
from __future__ import annotations

import ast
import sys
import threading
import time
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# ===================== FAKES (definidos FUERA del script AppTest) =====================

# Capturas y cierres de los fakes del backend (no se pulsa Ejecutar, pero el
# boton de cerrar SI se prueba en el panel final).
_CAPTURAS: list = []
_CIERRES: list = []


def _cuentas_fake() -> list:
    """7 cuentas Tier 1: 5 con sesión, 1 pausada con sesión y 1 sin sesión."""
    filas = []
    for indice in range(5):
        usuario = f"lider_{indice:02d}"
        filas.append(
            {
                "usuario": usuario,
                "nombre": f"Lider {indice}",
                "etiqueta": f"@{usuario} — Lider {indice}",
                "tiene_sesion": True,
                "pausada": False,
            }
        )
    filas.append(
        {
            "usuario": "pausada_00",
            "nombre": "Pausada Cero",
            "etiqueta": "@pausada_00 — Pausada Cero",
            "tiene_sesion": True,
            "pausada": True,
        }
    )
    filas.append(
        {
            "usuario": "sin_sesion_00",
            "nombre": "",
            "etiqueta": "@sin_sesion_00",
            "tiene_sesion": False,
            "pausada": False,
        }
    )
    return filas


def _resumen_fake(**extra) -> dict:
    """Resumen mínimo válido del backend (los tests pueden ampliarlo)."""
    resumen = {
        "ok": True,
        "planificadas": 6,
        "hechas": 1,
        "exitosas": 1,
        "fallidas": 0,
        "omitidas": 0,
        "por_cuenta": {},
        "eventos": [],
    }
    resumen.update(extra)
    return resumen


def _ejecutar_campana_local_falso(**kwargs) -> dict:
    """Backend falso: captura kwargs y devuelve un resumen válido."""
    _CAPTURAS.append(dict(kwargs))
    return _resumen_fake()


def _ejecutar_campana_local_nuevo(
    usuarios=None,
    urls=None,
    hashtags="",
    texto_base="",
    contexto="",
    narrativa="",
    menciones="",
    imagenes=None,
    probabilidad_imagen=100,
    acciones_por_cuenta=6,
    duracion_min=60,
    limite_dia=None,
    dar_like=False,
    permitir_pausadas=False,
    dejar_abierto=False,
    cancelar=None,
    callback=None,
    pausa_entre_acciones_seg=20,
    capturar_url=False,
    usar_proxy=True,
    sin_imagenes=True,
    esperar_captcha_seg=60,
) -> dict:
    """Backend NUEVO (firma coordinada): captura ritmo/captura/red y datos."""
    _CAPTURAS.append(
        {
            "usuarios": list(usuarios or []),
            "pausa_entre_acciones_seg": pausa_entre_acciones_seg,
            "capturar_url": capturar_url,
            "usar_proxy": usar_proxy,
            "sin_imagenes": sin_imagenes,
            "esperar_captcha_seg": esperar_captcha_seg,
        }
    )
    return _resumen_fake(
        hashtags=hashtags,
        hashtags_auto=["#Muuki2026"],
        capturar_url=capturar_url,
        pausa_entre_acciones_seg=pausa_entre_acciones_seg,
    )


def _ejecutar_campana_local_viejo(
    usuarios=None,
    urls=None,
    hashtags="",
    texto_base="",
    contexto="",
    narrativa="",
    menciones="",
    imagenes=None,
    probabilidad_imagen=100,
    acciones_por_cuenta=6,
    duracion_min=60,
    limite_dia=None,
    dar_like=False,
    permitir_pausadas=False,
    dejar_abierto=False,
    cancelar=None,
    callback=None,
) -> dict:
    """Backend VIEJO (sin pausa/capturar_url): debe seguir funcionando."""
    _CAPTURAS.append(
        {
            "usuarios": list(usuarios or []),
            "pausa_entre_acciones_seg": "NO_PASADO",
            "capturar_url": "NO_PASADO",
        }
    )
    return _resumen_fake()


def _cerrar_navegadores_locales_falso() -> int:
    """Backend falso de cierre: registra la llamada y devuelve 1."""
    _CIERRES.append(True)
    return 1


def _instalar_backend_falso():
    """Inyecta `activaciones.local` falso y devuelve el modulo anterior."""
    modulo = types.ModuleType("activaciones.local")
    modulo.ejecutar_campana_local = _ejecutar_campana_local_falso
    modulo.cerrar_navegadores_locales = _cerrar_navegadores_locales_falso
    anterior = sys.modules.get("activaciones.local")
    sys.modules["activaciones.local"] = modulo
    return anterior


def _restaurar_backend(anterior) -> None:
    """Restaura `sys.modules["activaciones.local"]` (lo quita si no existia)."""
    if anterior is None:
        sys.modules.pop("activaciones.local", None)
    else:
        sys.modules["activaciones.local"] = anterior


def _lanzar_con_backend(
    backend,
    usar_proxy: bool = True,
    sin_imagenes: bool = True,
    esperar_captcha_seg: int = 60,
) -> tuple:
    """Llama `_lanzar` con un backend fake y ESPERA a que el hilo termine.

    Devuelve `(snapshot, capturas)`. Restaura `sys.modules` y limpia el registro
    pase lo que pase (no deja campanas ni modulos falsos para el resto de la
    suite)."""
    from web.operaciones import local

    modulo = types.ModuleType("activaciones.local")
    modulo.ejecutar_campana_local = backend
    modulo.cerrar_navegadores_locales = _cerrar_navegadores_locales_falso
    anterior = sys.modules.get("activaciones.local")
    sys.modules["activaciones.local"] = modulo
    _CAPTURAS.clear()
    local._limpiar_registro()
    try:
        local._lanzar(
            usuarios=["lider_00"],
            urls_texto="",
            contexto="tema de prueba",
            hashtags="#Muuki2026",
            narrativa="",
            texto_base="",
            imagenes=[],
            probabilidad_imagen=100,
            acciones=2,
            duracion=5,
            limite_dia=49,
            dar_like=False,
            permitir_pausadas=False,
            dejar_abierto=False,
            pausa_entre_acciones_seg=20,
            capturar_url=False,
            usar_proxy=usar_proxy,
            sin_imagenes=sin_imagenes,
            esperar_captcha_seg=esperar_captcha_seg,
        )
        limite = time.time() + 5.0
        while time.time() < limite and local._campana_en_curso() is not None:
            time.sleep(0.02)
        return local._snapshot(), list(_CAPTURAS)
    finally:
        local._limpiar_registro()
        _restaurar_backend(anterior)


# ===================== SCRIPT DE APPTEST (SOLO ASCII) =====================

def _app_local():
    from web.operaciones import local

    local.render({})


# ===================== DRIVER DE APPTEST =====================

def _sembrar_campana_terminada(local) -> None:
    """Deja una campana terminada en el registro (sin hilo) para el panel final."""
    with local._CAMPANAS_LOCK:
        local._CAMPANAS.clear()
        local._CAMPANAS["test-terminada"] = {
            "id": "test-terminada",
            "hilo": None,
            "evento": threading.Event(),
            "estado": "terminada",
            "hechas": 3,
            "total": 6,
            "exitosas": 2,
            "fallidas": 1,
            "omitidas": 3,
            "ronda": 0,
            "eventos": [
                {
                    "usuario": "lider_00",
                    "rol": "hashtags",
                    "ok": True,
                    "detalle": "post publicado con imagen",
                    "url": "https://x.com/lider_00/status/1",
                },
                {
                    "usuario": "lider_01",
                    "rol": "cita",
                    "ok": False,
                    "detalle": "no se encontró el botón Post",
                    "url": "",
                },
            ],
            "resumen": {
                "ok": True,
                "cancelada": False,
                "planificadas": 6,
                "hechas": 3,
                "exitosas": 2,
                "fallidas": 1,
                "omitidas": 3,
                "omitidas_tier": 1,
                "omitidas_sin_sesion": 1,
                "omitidas_pausadas": 1,
                "omitidas_por_cuota": 0,
                "omitidas_por_tiempo": 0,
                "navegador_abierto": True,
                "hashtags": "#Mexico #Comunidad",
                "hashtags_auto": ["#Muuki2026"],
                "capturar_url": False,
                "pausa_entre_acciones_seg": 20,
                "datos_proxy_total": 12 * 1024 * 1024,
                "datos_proxy_subida": 2 * 1024 * 1024,
                "datos_proxy_bajada": 10 * 1024 * 1024,
                "captchas_resueltos": 2,
                "captchas_agotados": 1,
                "por_cuenta": {
                    "lider_00": {
                        "exitosas": 2,
                        "fallidas": 0,
                        "omitidas": 0,
                        "urls": ["https://x.com/lider_00/status/1"],
                    },
                    "lider_01": {
                        "exitosas": 0,
                        "fallidas": 1,
                        "omitidas": 0,
                        "urls": [],
                    },
                },
            },
            "error": "",
            "motivo": "",
            "inicio": time.time() - 60,
            "fin": time.time(),
            "parametros": {
                "dejar_abierto": True,
                "capturar_url": False,
                "pausa_entre_acciones_seg": 20,
            },
        }


def _app_test_local(terminada: bool = False) -> tuple:
    """AppTest de la pagina Local con cuentas y backend falsos.

    Con `terminada=True` siembra una campana finalizada y prueba el resumen
    (métricas/tabla/eventos) + el botón de cerrar navegador. NO pulsa el botón
    de ejecución."""
    from streamlit.testing.v1 import AppTest

    import streamlit as st_mod

    from web.operaciones import local

    datos = {
        "text_areas": set(),
        "number_inputs": set(),
        "number_values": {},
        "checkboxes": set(),
        "checkbox_values": {},
        "sliders": set(),
        "multiselects": set(),
        "buttons": set(),
        "metricas": {},
        "infos": [],
        "warnings": [],
        "captions": [],
        "markdown": [],
        "tablas": [],
        "uploader_llamadas": [],
        "seleccion_default": [],
        "capturas_antes": 0,
        "cierres": [],
        "excepcion": "",
    }
    original_cuentas = local._cargar_cuentas_tier1
    original_uploader = getattr(st_mod, "file_uploader", None)
    anterior_backend = _instalar_backend_falso()
    _CAPTURAS.clear()
    _CIERRES.clear()
    local._limpiar_registro()
    if terminada:
        _sembrar_campana_terminada(local)

    def _uploader_spy(*args, **kwargs):
        datos["uploader_llamadas"].append(dict(kwargs))
        return []

    local._cargar_cuentas_tier1 = _cuentas_fake
    st_mod.file_uploader = _uploader_spy
    try:
        at = AppTest.from_function(_app_local, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:300]
            return False, datos["excepcion"], datos
        datos["text_areas"] = {t.key for t in at.text_area}
        datos["number_inputs"] = {n.key for n in at.number_input}
        datos["number_values"] = {n.key: n.value for n in at.number_input}
        datos["checkboxes"] = {c.key for c in at.checkbox}
        datos["checkbox_values"] = {c.key: c.value for c in at.checkbox}
        datos["sliders"] = {s.key for s in at.slider}
        datos["multiselects"] = {m.key for m in at.multiselect}
        datos["buttons"] = {b.key for b in at.button}
        datos["metricas"] = {str(m.label): str(m.value) for m in at.metric}
        datos["infos"] = [str(i.value) for i in at.info]
        datos["warnings"] = [str(w.value) for w in at.warning]
        datos["captions"] = [str(c.value) for c in at.caption]
        datos["markdown"] = [str(m.value) for m in at.markdown]
        datos["tablas"] = [
            [str(columna) for columna in df.value.columns]
            for df in at.dataframe
        ]
        try:
            datos["seleccion_default"] = list(
                at.multiselect(key="local_cuentas").value
            )
        except Exception:
            datos["seleccion_default"] = []
        datos["capturas_antes"] = len(_CAPTURAS)

        if terminada:
            at.button(key="local_btn_cerrar").click().run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:300]
                return False, datos["excepcion"], datos
            datos["cierres"] = list(_CIERRES)
        return True, "", datos
    finally:
        local._cargar_cuentas_tier1 = original_cuentas
        local._limpiar_registro()
        if original_uploader is not None:
            st_mod.file_uploader = original_uploader
        _restaurar_backend(anterior_backend)


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


def _imports_de_modulo(fuente: str, modulo: str) -> list:
    """Nombres importados de `modulo` a NIVEL de modulo (AST)."""
    arbol = ast.parse(fuente)
    encontrados = []
    for nodo in arbol.body:
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "") == modulo:
            encontrados.extend(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.Import):
            for alias in nodo.names:
                if alias.name == modulo or alias.name.startswith(f"{modulo}."):
                    encontrados.append(alias.name)
    return encontrados


# ===================== SUITE =====================

def run(check):
    from web.operaciones import local

    # ---------------- 1) _formatear_evento (helper puro) ----------------
    linea_ok = local._formatear_evento(
        {
            "usuario": "@lider_00",
            "rol": "hashtags",
            "ok": True,
            "detalle": "publicado  con\nimagen",
            "url": "https://x.com/lider_00/status/1",
        }
    )
    check(
        "local evento: éxito con usuario/rol/detalle/link",
        linea_ok.startswith("✅ @lider_00")
        and "hashtags" in linea_ok
        and "publicado con imagen" in linea_ok
        and "https://x.com/lider_00/status/1" in linea_ok,
        ascii(linea_ok)[:160],
    )
    check(
        "local evento: fallo -> ❌ y omitida (ok=None) -> ➖",
        local._formatear_evento(
            {"usuario": "lider_01", "ok": False, "detalle": "error"}
        ).startswith("❌ @lider_01")
        and local._formatear_evento({"usuario": "lider_02", "ok": None}).startswith(
            "➖ @lider_02"
        ),
    )
    check(
        "local evento: no-dict -> '' y sin usuario -> solo icono",
        local._formatear_evento(None) == ""
        and local._formatear_evento("x") == ""
        and local._formatear_evento({"ok": True, "rol": "rt"})
        == "✅ · rt",
    )
    check(
        "local evento: no inventa link si la url no es http(s)",
        "link" not in local._formatear_evento(
            {"usuario": "u", "ok": True, "url": "file:///tmp/x"}
        ),
    )
    evento_captcha = local._formatear_evento(
        {"tipo": "espera_captcha", "usuario": "lider_00", "segundos": 45}
    )
    check(
        "local evento: espera_captcha (sin ok) -> 🧠 con usuario y segundos",
        evento_captcha
        == "🧠 @lider_00 — resuelve la verificación en Chrome (45s)"
        and local._formatear_evento(
            {"tipo": "espera_captcha", "segundos": 10}
        )
        == "🧠 Resuelve la verificación en Chrome (10s)"
        and local._formatear_evento(
            {"tipo": "espera_captcha", "usuario": "u"}
        )
        == "🧠 @u — resuelve la verificación en Chrome (0s)",
        ascii(evento_captcha)[:160],
    )

    # ---------------- 2) _validar_formulario (helper puro) ----------------
    ok, error, aviso = local._validar_formulario(
        usuarios=[], urls="https://x.com/a/status/1"
    )
    check(
        "local validar: sin cuentas bloquea con mensaje",
        ok is False and "cuenta" in error.lower() and aviso == "",
        ascii(error)[:140],
    )
    ok, error, aviso = local._validar_formulario(
        usuarios=["lider_00"], urls="", contexto="", hashtags="", narrativa=""
    )
    check(
        "local validar: sin material (URLs/contexto/hashtags/trasfondo) bloquea",
        ok is False and ("contexto" in error.lower() or "url" in error.lower()),
        ascii(error)[:140],
    )
    ok, error, aviso = local._validar_formulario(
        usuarios=[f"lider_{i:02d}" for i in range(6)],
        urls="https://x.com/a/status/1",
    )
    check(
        "local validar: 6 cuentas con tope por defecto 0 (sin límite) es válido",
        ok is True and error == "",
        f"ok={ok} error={ascii(error)[:120]}",
    )
    ok, error, aviso = local._validar_formulario(
        usuarios=[f"lider_{i:02d}" for i in range(6)],
        urls="https://x.com/a/status/1",
        maximo=5,
    )
    check(
        "local validar: maximo=5 sigue bloqueando más de 5 (compatibilidad)",
        ok is False and "5" in error and "máximo" in error.lower(),
        ascii(error)[:140],
    )
    ok, error, aviso = local._validar_formulario(
        usuarios=["lider_00"], urls="https://x.com/a/status/1", imagenes=[]
    )
    check(
        "local validar: válido con URL y sin imágenes -> ok + aviso de solo texto",
        ok is True and error == "" and "solo con texto" in aviso,
        f"ok={ok} aviso={ascii(aviso)[:100]}",
    )
    ok, error, aviso = local._validar_formulario(
        usuarios=["lider_00"], contexto="tema", imagenes=["/tmp/a.png"]
    )
    check(
        "local validar: contexto válido y con imágenes -> sin aviso",
        ok is True and error == "" and aviso == "",
    )
    for material in (
        {"hashtags": "#Mexico"},
        {"narrativa": "nota de contexto"},
    ):
        ok, error, _ = local._validar_formulario(usuarios=["lider_00"], **material)
        check(
            f"local validar: material alternativo válido {list(material)[0]}",
            ok is True and error == "",
        )

    # ---------------- 2b) _defaults_cuentas (sin límite vs tope) ----------------
    filas_defaults = [
        {"etiqueta": f"@cand_{i}", "tiene_sesion": True, "pausada": False}
        for i in range(7)
    ] + [
        {"etiqueta": "@pausada_x", "tiene_sesion": True, "pausada": True},
        {"etiqueta": "@sin_sesion_x", "tiene_sesion": False, "pausada": False},
    ]
    todas = local._defaults_cuentas(filas_defaults, maximo=0)
    check(
        "local defaults: maximo=0 devuelve TODAS las candidatas con sesión "
        "(sin pausadas)",
        len(todas) == 7
        and todas == [f"@cand_{i}" for i in range(7)]
        and "@pausada_x" not in todas
        and "@sin_sesion_x" not in todas,
        ascii(todas)[:160],
    )
    recortadas = local._defaults_cuentas(filas_defaults, maximo=5)
    check(
        "local defaults: maximo=5 recorta a 5 (compatibilidad)",
        recortadas == todas[:5],
        ascii(recortadas)[:160],
    )
    check(
        "local defaults: sin maximo usa el default sin límite (MAX_CUENTAS=0)",
        local._defaults_cuentas(filas_defaults) == todas
        and local.MAX_CUENTAS == 0,
        f"MAX_CUENTAS={local.MAX_CUENTAS}",
    )

    # ---------------- 3) _resumen_metricas (helper puro) ----------------
    metricas = local._resumen_metricas(
        {
            "exitosas": 4,
            "fallidas": 2,
            "omitidas": 5,
            "omitidas_tier": 1,
            "omitidas_sin_sesion": 2,
            "omitidas_pausadas": 1,
            "omitidas_por_cuota": 0,
            "omitidas_por_tiempo": 1,
        }
    )
    check(
        "local métricas: 8 claves con sus valores",
        set(metricas) == {
            "✅ Exitosas",
            "❌ Fallidas",
            "➖ Omitidas",
            "🧱 Tier fuera",
            "🔑 Sin sesión",
            "⏸️ Pausadas",
            "🛑 Por cuota",
            "⏰ Por tiempo",
        }
        and metricas["✅ Exitosas"] == 4
        and metricas["❌ Fallidas"] == 2
        and metricas["➖ Omitidas"] == 5
        and metricas["🧱 Tier fuera"] == 1
        and metricas["🔑 Sin sesión"] == 2
        and metricas["⏸️ Pausadas"] == 1
        and metricas["🛑 Por cuota"] == 0
        and metricas["⏰ Por tiempo"] == 1,
        ascii(metricas)[:220],
    )
    check(
        "local métricas: resumen vacío/None -> todas 0",
        set(local._resumen_metricas(None).values()) == {0}
        and set(local._resumen_metricas({}).values()) == {0}
        and set(local._resumen_metricas("basura").values()) == {0},
    )

    # ---------------- 4) Extras puros (sesión, URLs, hashtags, tabla) ----------------
    sesion = types.SimpleNamespace
    check(
        "local sesión: auth_token/cookies_json con contenido/archivo real",
        local._cuenta_tiene_sesion(sesion(auth_token="abc", cookies_json=None, cookies_path=""))
        and local._cuenta_tiene_sesion(
            sesion(auth_token="", cookies_json='[{"name":"auth_token"}]', cookies_path="")
        )
        and not local._cuenta_tiene_sesion(
            sesion(auth_token="", cookies_json="[]", cookies_path="")
        )
        and local._cuenta_tiene_sesion(
            sesion(auth_token="", cookies_json=None, cookies_path=str(Path(__file__)))
        )
        and not local._cuenta_tiene_sesion(
            sesion(auth_token="", cookies_json=None, cookies_path="no_existe.pkl")
        ),
    )
    check(
        "local URLs: una por línea, sin vacíos/comentarios ni líneas no http(s)",
        local._lista_urls(
            "https://x.com/a/status/1\n\n# comentario\nno es una url\n "
            "https://x.com/b/status/2 \n"
        )
        == ["https://x.com/a/status/1", "https://x.com/b/status/2"],
    )
    check(
        "local hashtags: normaliza '#', separadores y duplicados",
        local._normalizar_hashtags("#Mexico, #futbol\n#MEXICO; #deportes")
        == ["#Mexico", "#futbol", "#deportes"]
        and local._hashtags_texto(["#Uno", "#Dos"]) == "#Uno #Dos",
    )
    check(
        "local hashtags: detecta # en contexto/texto base para el preview",
        local._hashtags_detectados(
            "Cada año, el Día de Muertos nos reúne. #Muuki2026", ""
        )
        == ["#Muuki2026"]
        and local._hashtags_detectados("sin tag", "texto #Otro") == ["#Otro"]
        and local._hashtags_detectados("", "") == [],
    )
    filas = local._filas_por_cuenta(
        {
            "por_cuenta": {
                "lider_00": {
                    "exitosas": 2,
                    "fallidas": 1,
                    "omitidas": 0,
                    "urls": ["https://x.com/a/status/1"],
                },
                "lider_01": "basura",
            }
        }
    )
    check(
        "local tabla por cuenta: filas con contadores y links",
        len(filas) == 2
        and filas[0]["Cuenta"] == "@lider_00"
        and filas[0]["✅ Exitosas"] == 2
        and filas[0]["❌ Fallidas"] == 1
        and filas[0]["🔗 Links"] == 1
        and filas[1]["Cuenta"] == "@lider_01",
        ascii(filas)[:220],
    )

    # ---------------- 4b) Ritmo, captura de enlaces y backend viejo ----------------
    check(
        "local _soporta_kwarg: detecta kwargs nuevos, **kwargs y backend viejo",
        local._soporta_kwarg(
            _ejecutar_campana_local_nuevo, "pausa_entre_acciones_seg"
        )
        and local._soporta_kwarg(_ejecutar_campana_local_nuevo, "capturar_url")
        and not local._soporta_kwarg(
            _ejecutar_campana_local_viejo, "pausa_entre_acciones_seg"
        )
        and not local._soporta_kwarg(
            _ejecutar_campana_local_viejo, "capturar_url"
        )
        and local._soporta_kwarg(
            _ejecutar_campana_local_falso, "cualquier_kwarg"
        ),
    )
    captions = local._captions_resumen(
        {
            "hashtags": "#Mexico #Comunidad",
            "hashtags_auto": ["#Muuki2026"],
            "capturar_url": False,
            "pausa_entre_acciones_seg": 20,
        }
    )
    check(
        "local captions resumen: hashtags auto/usados, links no capturados y pausa",
        any(
            "Hashtags detectados del contexto" in linea
            and "#Muuki2026" in linea
            for linea in captions
        )
        and any("Hashtags usados" in linea and "#Mexico" in linea for linea in captions)
        and any("no capturados" in linea for linea in captions)
        and any("20 s" in linea for linea in captions),
        ascii(captions)[:260],
    )
    check(
        "local captions resumen: sin datos -> sin captions (no inventa)",
        local._captions_resumen({}, {}) == []
        and local._captions_resumen(None, None) == []
        and local._captions_resumen(
            {"hashtags_auto": [], "hashtags": ""}, {"capturar_url": True}
        )
        == ["🔗 Enlaces de cada publicación: capturados para Reportes."],
    )
    con_datos = {
        "datos_proxy_total": 12 * 1024 * 1024,
        "datos_proxy_subida": 2 * 1024 * 1024,
        "datos_proxy_bajada": 10 * 1024 * 1024,
        "captchas_resueltos": 2,
        "captchas_agotados": 1,
    }
    datos_extra = local._resumen_datos_extra(con_datos)
    check(
        "local datos proxy: métrica total en MB y captchas resueltos/agotados",
        datos_extra.get("📡 Datos del proxy") == "12.0 MB"
        and datos_extra.get("🧠 Captchas resueltos") == "2"
        and datos_extra.get("⏳ Captchas agotados") == "1",
        ascii(datos_extra)[:220],
    )
    check(
        "local datos proxy: caption con subida y bajada en MB",
        local._caption_datos_proxy(con_datos)
        == "📡 Tráfico del proxy — subida 2.0 MB · bajada 10.0 MB.",
        ascii(local._caption_datos_proxy(con_datos))[:160],
    )
    check(
        "local datos proxy: sin campos -> {} y sin caption (no inventa)",
        local._resumen_datos_extra({}) == {}
        and local._resumen_datos_extra(None) == {}
        and local._resumen_datos_extra(
            {"datos_proxy_total": "basura", "captchas_resueltos": None}
        )
        == {}
        and local._caption_datos_proxy({}) == ""
        and local._caption_datos_proxy(
            {"datos_proxy_total": 5 * 1024 * 1024}
        )
        == "",
    )

    snap, capturas = _lanzar_con_backend(_ejecutar_campana_local_nuevo)
    check(
        "local _lanzar: backend nuevo recibe pausa=20 y capturar_url=False",
        len(capturas) == 1
        and capturas[0].get("pausa_entre_acciones_seg") == 20
        and capturas[0].get("capturar_url") is False
        and capturas[0].get("usuarios") == ["lider_00"],
        ascii(capturas)[:220],
    )
    check(
        "local _lanzar: registro guarda ritmo/captura y termina la campaña",
        snap is not None
        and snap["estado"] == "terminada"
        and snap["parametros"].get("pausa_entre_acciones_seg") == 20
        and snap["parametros"].get("capturar_url") is False,
        ascii(snap.get("parametros") if snap else None)[:220],
    )
    snap_viejo, capturas_viejas = _lanzar_con_backend(
        _ejecutar_campana_local_viejo
    )
    check(
        "local _lanzar: backend viejo (sin kwargs) NO falla y sigue la campaña",
        len(capturas_viejas) == 1
        and capturas_viejas[0].get("pausa_entre_acciones_seg") == "NO_PASADO"
        and capturas_viejas[0].get("capturar_url") == "NO_PASADO"
        and snap_viejo is not None
        and snap_viejo["estado"] == "terminada"
        and snap_viejo["error"] == "",
        ascii(capturas_viejas)[:220],
    )
    snap_red, capturas_red = _lanzar_con_backend(
        _ejecutar_campana_local_nuevo,
        usar_proxy=False,
        sin_imagenes=False,
        esperar_captcha_seg=0,
    )
    check(
        "local _lanzar: backend nuevo recibe usar_proxy/sin_imagenes/"
        "esperar_captcha_seg",
        len(capturas_red) == 1
        and capturas_red[0].get("usar_proxy") is False
        and capturas_red[0].get("sin_imagenes") is False
        and capturas_red[0].get("esperar_captcha_seg") == 0
        and snap_red is not None
        and snap_red["parametros"].get("usar_proxy") is False
        and snap_red["parametros"].get("sin_imagenes") is False
        and snap_red["parametros"].get("esperar_captcha_seg") == 0,
        ascii(capturas_red)[:260],
    )
    snap_red_viejo, capturas_red_viejas = _lanzar_con_backend(
        _ejecutar_campana_local_viejo,
        usar_proxy=True,
        sin_imagenes=True,
        esperar_captcha_seg=60,
    )
    check(
        "local _lanzar: backend viejo NO recibe kwargs de red y no rompe",
        len(capturas_red_viejas) == 1
        and "usar_proxy" not in capturas_red_viejas[0]
        and "sin_imagenes" not in capturas_red_viejas[0]
        and "esperar_captcha_seg" not in capturas_red_viejas[0]
        and snap_red_viejo is not None
        and snap_red_viejo["estado"] == "terminada"
        and snap_red_viejo["error"] == "",
        ascii(capturas_red_viejas)[:260],
    )
    snap_clamp, capturas_clamp = _lanzar_con_backend(
        _ejecutar_campana_local_nuevo, esperar_captcha_seg=999
    )
    check(
        "local _lanzar: esperar_captcha_seg se acota a 0..300",
        len(capturas_clamp) == 1
        and capturas_clamp[0].get("esperar_captcha_seg") == 300
        and snap_clamp["parametros"].get("esperar_captcha_seg") == 300,
        ascii(capturas_clamp)[:160],
    )

    # ---------------- 5) Fuente de web/app.py ----------------
    fuente_app = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    constantes = _constantes_app()
    opciones = constantes.get("OPCIONES") or []
    categorias = constantes.get("CATEGORIAS") or []
    check(
        "app: '🖥️ Local (flota)' está en OPCIONES y en una categoría",
        "🖥️ Local (flota)" in opciones
        and any("🖥️ Local (flota)" in ops for _, ops in categorias),
        f"(opciones={len(opciones)})",
    )
    check(
        "app: Local vive en '⚡ Automatización Twitter' después de Actividad",
        any(
            "🖥️ Local (flota)" in ops
            and "Automatización" in nombre
            and ops.index("🖥️ Local (flota)")
            == ops.index("✍️ Actividad") + 1
            for nombre, ops in categorias
        ),
    )
    check(
        "app: dispatch de Local importa la página y usa su prefijo",
        "from web.operaciones.local import render" in fuente_app
        and 'seleccion.startswith("🖥️")' in fuente_app,
    )

    # ---------------- 6) Backend importado PEREZOSAMENTE ----------------
    fuente_local = (RAIZ / "web" / "operaciones" / "local.py").read_text(
        encoding="utf-8"
    )
    top_imports = _imports_de_modulo(fuente_local, "activaciones.local")
    check(
        "local fuente: el backend NO se importa a nivel de módulo",
        top_imports == [],
        str(top_imports),
    )
    check(
        "local fuente: importa ejecutar_campana_local y "
        "cerrar_navegadores_locales dentro de funciones",
        "from activaciones.local import ejecutar_campana_local" in fuente_local
        and "from activaciones.local import cerrar_navegadores_locales"
        in fuente_local,
    )
    arbol_local = ast.parse(fuente_local)
    nombres_funciones = {
        nodo.name for nodo in ast.walk(arbol_local) if isinstance(nodo, ast.FunctionDef)
    }
    check(
        "local fuente: render + helpers/panel esperados",
        {
            "render",
            "_formatear_evento",
            "_validar_formulario",
            "_resumen_metricas",
            "_render_proceso_activo",
            "_lanzar",
            "_pintar_en_vivo",
            "_pintar_terminada",
        }
        <= nombres_funciones,
        str(sorted(nombres_funciones))[:220],
    )
    check(
        "local fuente: botones de ejecutar/detener/cerrar y uploader de imágenes",
        'key=f"{PREFIX}_btn_ejecutar"' in fuente_local
        and 'key=f"{PREFIX}_btn_detener"' in fuente_local
        and 'key=f"{PREFIX}_btn_cerrar"' in fuente_local
        and "accept_multiple_files=True" in fuente_local
        and         'prefijo=f"local_img_{indice}"' in fuente_local,
    )
    check(
        "local fuente: textos completos hasta 280 (ya no 100) con hashtag en medio",
        "280 caracteres" in fuente_local
        and "100 caracteres" not in fuente_local,
    )
    check(
        "local fuente: controles de pausa y captura de enlaces",
        '"⏱️ Pausa entre acciones (segundos)"' in fuente_local
        and '"🔗 Capturar enlace de cada publicación (instantáneo)"'
        in fuente_local
        and 'f"{PREFIX}_pausa_acciones"' in fuente_local
        and 'f"{PREFIX}_capturar_url"' in fuente_local
        and "pausa_entre_acciones_seg" in fuente_local
        and "capturar_url" in fuente_local
        and "hashtags_auto" in fuente_local
        and "min_value=1,\n            max_value=120,\n            value=2,"
        in fuente_local,
    )
    check(
        "local fuente: controles de red/datos con prefijo y caption de ahorro",
        '"🌐 Usar proxies residenciales (MX)"' in fuente_local
        and '"🖼️ No cargar imágenes de X (ahorra datos)"' in fuente_local
        and '"🧠 Esperar a que resuelvas el reto Cloudflare (seg)"'
        in fuente_local
        and 'f"{PREFIX}_usar_proxy"' in fuente_local
        and 'f"{PREFIX}_sin_imagenes"' in fuente_local
        and 'f"{PREFIX}_esperar_captcha"' in fuente_local
        and "CAPTION_AHORRO" in fuente_local
        and "se comprimen automáticamente" in fuente_local
        and "Backend sin soporte de proxies; actualiza" in fuente_local,
    )
    check(
        "local fuente: selector sin límite, caption sin '/5' y encabezado FLOTA",
        '"Cuentas Tier 1 (sin límite)"' in fuente_local
        and "Seleccionadas: **{len(usuarios_sel)}**" in fuente_local
        and "🖥️ LOCAL (FLOTA)" in fuente_local
        and "/{MAX_CUENTAS}" not in fuente_local,
    )
    check(
        "local fuente: captura instantánea (sin 'más lento' ni '5-10s más')",
        "más lento" not in fuente_local
        and "5-10s más" not in fuente_local
        and "pausa de 2s" in fuente_local,
        "textos de velocidad nuevos",
    )

    # ---------------- 7) AppTest: render del formulario ----------------
    ok, detalle, datos = _app_test_local(terminada=False)
    check("local AppTest: render sin excepciones", ok, detalle)
    check(
        "local AppTest: campos de contexto/hashtags/trasfondo/URLs/texto base",
        ok
        and {
            "local_contexto",
            "local_hashtags",
            "local_narrativa",
            "local_urls",
            "local_texto_base",
        }
        <= datos["text_areas"],
        f"text_areas={sorted(datos['text_areas'])}",
    )
    check(
        "local AppTest: acciones/ventana/tope diario como number_input",
        ok
        and {"local_acciones", "local_duracion", "local_limite_dia"}
        <= datos["number_inputs"],
        f"number_inputs={sorted(datos['number_inputs'])}",
    )
    check(
        "local AppTest: limite por hora (10) y pausa 2 por defecto; captura ON",
        ok
        and "local_pausa_acciones" in datos["number_inputs"]
        and datos["number_values"].get("local_pausa_acciones") == 2
        and datos["number_values"].get("local_acciones") == 10
        and "local_capturar_url" in datos["checkboxes"]
        and datos["checkbox_values"].get("local_capturar_url") is True,
        f"number_values={ascii(datos['number_values'])[:120]} "
        f"checkbox_values={ascii(datos['checkbox_values'])[:160]}",
    )
    check(
        "local AppTest: caption del plan con pausa de 2s y captura instantánea",
        ok
        and any("pausa de 2s" in caption for caption in datos["captions"])
        and any(
            "se captura al instante" in caption for caption in datos["captions"]
        ),
        ascii(datos["captions"])[:260],
    )
    check(
        "local AppTest: navegadores simultáneos default 2 y orden aleatorio "
        "por ciclo",
        ok
        and "local_navegadores" in datos["number_inputs"]
        and datos["number_values"].get("local_navegadores") == 2
        and any(
            "Orden de cuentas aleatorio en cada ciclo" in caption
            for caption in datos["captions"]
        ),
        f"number_values={ascii(datos['number_values'])[:160]} "
        f"captions={ascii(datos['captions'])[:200]}",
    )
    check(
        "local AppTest: aviso visible cuando no hay hashtags escritos",
        ok
        and any("Sin hashtags escritos" in w for w in datos["warnings"]),
        ascii(datos["warnings"])[:220],
    )
    check(
        "local AppTest: probabilidad de imagen (slider 100) y checkboxes",
        ok
        and "local_prob_imagen" in datos["sliders"]
        and {"local_dar_like", "local_pausadas", "local_dejar_abierto"}
        <= datos["checkboxes"],
        f"sliders={sorted(datos['sliders'])} checkboxes={sorted(datos['checkboxes'])}",
    )
    check(
        "local AppTest: red/datos con defaults (proxy ON, no imágenes ON, "
        "Cloudflare 0s = saltar y guardar en el cajón) y caption de ahorro",
        ok
        and {"local_usar_proxy", "local_sin_imagenes"} <= datos["checkboxes"]
        and datos["checkbox_values"].get("local_usar_proxy") is True
        and datos["checkbox_values"].get("local_sin_imagenes") is True
        and datos["number_values"].get("local_esperar_captcha") == 0
        and any(
            "se comprimen automáticamente" in caption
            for caption in datos["captions"]
        ),
        f"checkbox_values={ascii(datos['checkbox_values'])[:200]} "
        f"number_values={ascii(datos['number_values'])[:200]}",
    )
    check(
        "local AppTest: multiselect de cuentas, botón Ejecutar y uploader",
        ok
        and "local_cuentas" in datos["multiselects"]
        and "local_btn_ejecutar" in datos["buttons"]
        and any(
            llamada.get("key") == "local_imagenes"
            and llamada.get("accept_multiple_files") is True
            for llamada in datos["uploader_llamadas"]
        ),
        f"botones={sorted(datos['buttons'])}",
    )
    check(
        "local AppTest: métricas Tier 1 total/con sesión/pausadas",
        ok
        and datos["metricas"].get("Tier 1 total") == "7"
        and datos["metricas"].get("Tier 1 con sesión") == "6"
        and datos["metricas"].get("Pausadas") == "1",
        ascii(datos["metricas"])[:220],
    )
    seleccion = list(datos.get("seleccion_default") or [])
    check(
        "local AppTest: selección por defecto = todas las candidatas con "
        "sesión (sin pausadas)",
        ok
        and len(seleccion) == 5
        and all("lider_" in etiqueta for etiqueta in seleccion)
        and not any("pausada_00" in etiqueta for etiqueta in seleccion)
        and not any("sin_sesion_00" in etiqueta for etiqueta in seleccion),
        ascii(seleccion)[:220],
    )
    check(
        "local AppTest: sin pulsar Ejecutar no se lanza el backend",
        ok and datos.get("capturas_antes") == 0,
        f"capturas={datos.get('capturas_antes')}",
    )
    check(
        "local AppTest: panel informativo fijo (Chrome visible/proxies "
        "opcionales/Tier 1)",
        ok
        and any(
            "Chrome visible" in info
            and "proxies opcionales" in info
            and "Tier 1" in info
            for info in datos["infos"]
        ),
        ascii(datos["infos"])[:220],
    )

    # ---------------- 8) AppTest: panel final + cerrar navegador ----------------
    ok, detalle, datos_final = _app_test_local(terminada=True)
    check(
        "local AppTest: panel final sin excepciones con una campaña terminada",
        ok,
        detalle,
    )
    check(
        "local AppTest: métricas finales del resumen",
        ok
        and datos_final["metricas"].get("✅ Exitosas") == "2"
        and datos_final["metricas"].get("❌ Fallidas") == "1"
        and datos_final["metricas"].get("➖ Omitidas") == "3"
        and datos_final["metricas"].get("🧱 Tier fuera") == "1",
        ascii(datos_final["metricas"])[:260],
    )
    check(
        "local AppTest: resumen muestra datos del proxy (MB) y captchas",
        ok
        and datos_final["metricas"].get("📡 Datos del proxy") == "12.0 MB"
        and datos_final["metricas"].get("🧠 Captchas resueltos") == "2"
        and datos_final["metricas"].get("⏳ Captchas agotados") == "1"
        and any(
            "Tráfico del proxy" in caption
            and "subida 2.0 MB" in caption
            and "bajada 10.0 MB" in caption
            for caption in datos_final["captions"]
        ),
        ascii(datos_final["metricas"])[:260],
    )
    check(
        "local AppTest: resumen muestra hashtags_auto, links no capturados y pausa",
        ok
        and any(
            "Hashtags detectados del contexto" in linea
            and "#Muuki2026" in linea
            for linea in datos_final["captions"]
        )
        and any(
            "Hashtags usados" in linea and "#Mexico" in linea
            for linea in datos_final["captions"]
        )
        and any("no capturados" in linea for linea in datos_final["captions"])
        and any("20 s" in linea for linea in datos_final["captions"]),
        ascii(datos_final["captions"])[:300],
    )
    check(
        "local AppTest: tabla por cuenta en el resumen",
        ok
        and any(
            {"Cuenta", "✅ Exitosas", "❌ Fallidas", "🔗 Links"} <= set(columnas)
            for columnas in datos_final["tablas"]
        ),
        ascii(str(datos_final["tablas"]))[:200],
    )
    check(
        "local AppTest: eventos en el panel final (markdown)",
        ok
        and any("@lider_00" in linea for linea in datos_final["markdown"])
        and any("post publicado con imagen" in linea for linea in datos_final["markdown"]),
        ascii(datos_final["markdown"])[:220],
    )
    check(
        "local AppTest: '🪟 Cerrar navegador local' llama al backend fake",
        ok and datos_final["cierres"] == [True],
        f"cierres={datos_final['cierres']}",
    )

    # ---------------- 9) Registro limpio para el resto de la suite ----------------
    check(
        "local AppTest: registro de campañas limpio al terminar",
        local._campana_en_curso() is None
        and local._campana_actual() is None,
    )
