"""Tests del dashboard web de la operacion CHANGE.ORG: REPORTES.

La pagina `web/operaciones/change.py` dejo de ser el formulario de FIRMAS
(monkeypatch de `builtins.input`, `firmas_por_ip`, `reconexiones`,
`ChangeOrgBot`) y ahora lanza un ATAQUE DE REPORTES masivo con identidades
generadas por IA a traves del backend congelado
`cuentas.change_org.ejecutar_campana_reportes`.

Cubren, SIN Chrome, SIN red y SIN campanas reales (fakes + monkeypatch):

  (1) Contrato del modulo: `render` conservado, registro a nivel modulo
      (`_CAMPANAS` + `_CAMPANAS_LOCK`), `LOG_MAX` y firma de
      `_render_proceso_activo(max_pasos=None)`.
  (2) Registro puro: `_registrar_campana` (campos del contrato),
      `_anotar_evento` (inicio/reporte ok/fallo, log exacto y tope de 200),
      `_finalizar_campana` (terminada/cancelada/error), `_solicitar_paro`
      (setea el Event y pasa a deteniendo) y `_limpiar_registro`.
  (3) Fuente de `web/app.py`: "✍️ Change.org: Reportes" en OPCIONES y en
      CATEGORIAS (categoria de Monitoreo), sin "Peticiones", y el dispatch de
      "✍️ Actividad" ANTES del prefijo generico "✍️".
  (4) Fuente de `change.py`: sin la logica vieja de firmas y con el backend
      perezoso + visor de la granja.
  (5) AppTest: render con usuario fake admin sin excepciones, widgets/labels/
      keys/defaults exactos, expander de opciones avanzadas, selector de pais
      y visor de granja (helper monkeado, con y sin error de BD).
  (6) Validaciones: URL vacia, URL sin change.org y contexto vacio -> warning
      y backend NO llamado.
  (7) Campana fake end-to-end: kwargs congelados, progreso en vivo con feed
      "✅ Reporte enviado por", boton "⛔ Detener" que setea el Event, aviso de
      "ya hay un ataque en curso" y resumen final (success + dataframe +
      caption de identidades_guardadas).

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows; misma regla que
`test_actividad_web.py`/`test_ui_paro_progreso.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_web.py   (solo este archivo)
"""
from __future__ import annotations

import ast
import inspect
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


# ===================== FAKES (definidos FUERA de los scripts) =====================

class _BackendReportesFake:
    """Fake de `ejecutar_campana_reportes` (sin Chrome, sin red, sin BD)."""

    def __init__(self):
        self.llamadas: list = []
        self.modo = "instantaneo"  # "instantaneo" | "bloqueado"
        self.bloquear = threading.Event()
        self.eventos = [
            {"tipo": "inicio", "total": 2},
            {
                "tipo": "reporte",
                "hechas": 1,
                "total": 2,
                "ok": True,
                "email": "ana@example.com",
                "identidad": {"nombre": "Ana", "email": "ana@example.com"},
                "detalle": "",
            },
            {
                "tipo": "reporte",
                "hechas": 2,
                "total": 2,
                "ok": False,
                "email": "bob@example.com",
                "identidad": {"nombre": "Bob", "email": "bob@example.com"},
                "detalle": "captcha",
            },
        ]
        self.resumen = {
            "total": 2,
            "enviados": 1,
            "fallidos": 1,
            "cancelada": False,
            "identidades_guardadas": 2,
            "resultados": [
                {"email": "ana@example.com", "ok": True, "detalle": ""},
                {"email": "bob@example.com", "ok": False, "detalle": "captcha"},
            ],
            "proxies_total": 1,
            "sin_proxy": False,
            "error": "",
        }

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        cb = kwargs.get("callback")
        cancelar = kwargs.get("cancelar")
        eventos = self.eventos[:2] if self.modo == "bloqueado" else self.eventos
        for evento in eventos:
            if callable(cb):
                try:
                    cb(evento)
                except Exception:
                    pass
        if self.modo == "bloqueado":
            # Espera a que el test pida el paro (o suelta la campana).
            self.bloquear.wait(timeout=15.0)
            resumen = dict(self.resumen)
            resumen["cancelada"] = bool(
                cancelar is not None and cancelar.is_set()
            )
            return resumen
        return dict(self.resumen)


class _BackendParcheado:
    """Monkeypatch temporal de `cuentas.change_org.ejecutar_campana_reportes`."""

    def __init__(self, fake):
        self.fake = fake
        self.modulo = None
        self.original = None
        self.existia = False

    def __enter__(self):
        import cuentas.change_org as modulo

        self.modulo = modulo
        self.existia = hasattr(modulo, "ejecutar_campana_reportes")
        self.original = getattr(modulo, "ejecutar_campana_reportes", None)
        modulo.ejecutar_campana_reportes = self.fake
        return self.fake

    def __exit__(self, *exc):
        if self.existia:
            self.modulo.ejecutar_campana_reportes = self.original
        else:
            try:
                delattr(self.modulo, "ejecutar_campana_reportes")
            except Exception:
                pass
        return False


class _GranjaParcheada:
    """Monkeypatch temporal del helper `change._cargar_granja`."""

    def __init__(self, change_mod, resultado=None, error=None):
        self.change = change_mod
        self.resultado = (0, []) if resultado is None else resultado
        self.error = error
        self.original = None

    def __enter__(self):
        self.original = self.change._cargar_granja

        def _fake(limite=20):
            if self.error is not None:
                raise self.error
            return self.resultado

        self.change._cargar_granja = _fake
        return self

    def __exit__(self, *exc):
        self.change._cargar_granja = self.original
        return False


# ===================== SCRIPTS DE APPTEST (SOLO ASCII) =====================

def _app_change():
    from web.operaciones import change

    change.render({"rol": "admin", "username": "tester"})


def _app_change_una_pasada():
    from web.operaciones import change

    original = change._render_proceso_activo

    def _una_pasada(max_pasos=None):
        return original(max_pasos=1)

    change._render_proceso_activo = _una_pasada
    try:
        change.render({"rol": "admin", "username": "tester"})
    finally:
        change._render_proceso_activo = original


# ===================== HELPERS =====================

def _widget_por_key(at, coleccion, key):
    """Elemento de una coleccion del AppTest por `key` (None si no existe)."""
    for elemento in getattr(at, coleccion, []) or []:
        if getattr(elemento, "key", None) == key:
            return elemento
    return None


def _textos(at) -> str:
    """Junta el texto de todos los elementos pintados por un AppTest."""
    partes = []
    for atributo in (
        "markdown", "success", "warning", "error", "caption", "info",
    ):
        for elemento in getattr(at, atributo, []) or []:
            try:
                partes.append(str(elemento.value))
            except Exception:
                pass
    return "\n".join(partes)


def _esperar_campana_libre(change, timeout: float = 15.0) -> bool:
    """Espera a que el hilo de la campana termine (estado no activo)."""
    limite = time.time() + timeout
    while time.time() < limite:
        if change._campana_en_curso() is None:
            return True
        time.sleep(0.05)
    return False


def _esperar_eventos(change, minimo: int = 1, timeout: float = 8.0) -> bool:
    """Espera a que el callback haya anotado al menos `minimo` reportes."""
    limite = time.time() + timeout
    while time.time() < limite:
        snap = change._snapshot() or {}
        if int(snap.get("hechas") or 0) >= minimo:
            return True
        time.sleep(0.05)
    return False


def _leer_constantes_app() -> dict:
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


def _fuente_change() -> str:
    return (RAIZ / "web" / "operaciones" / "change.py").read_text(
        encoding="utf-8"
    )


def _identificadores_codigo(fuente: str) -> set:
    """Nombres/atributos/args usados en el CODIGO (ignora docstrings/prosa)."""
    arbol = ast.parse(fuente)
    nombres: set = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Name):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.Attribute):
            nombres.add(nodo.attr)
        elif isinstance(nodo, ast.arg):
            nombres.add(nodo.arg)
        elif isinstance(nodo, (ast.FunctionDef, ast.ClassDef)):
            nombres.add(nodo.name)
        elif isinstance(nodo, ast.keyword) and nodo.arg:
            nombres.add(nodo.arg)
    return nombres


# ===================== SUITE =====================

def run(check):
    from streamlit.testing.v1 import AppTest

    from web.operaciones import change

    # ---------------- 1) Contrato del modulo ----------------
    check(
        "change: conserva render(usuario) y el registro a nivel modulo",
        callable(getattr(change, "render", None))
        and isinstance(getattr(change, "_CAMPANAS", None), dict)
        and hasattr(getattr(change, "_CAMPANAS_LOCK", None), "acquire")
        and change.LOG_MAX == 200,
    )
    firma_proceso = inspect.signature(change._render_proceso_activo).parameters
    check(
        "change: _render_proceso_activo acepta el parametro max_pasos (tests)",
        "max_pasos" in firma_proceso,
        str(list(firma_proceso)),
    )
    check(
        "change: helpers puros del registro/callback disponibles",
        all(
            callable(getattr(change, nombre, None))
            for nombre in (
                "_registrar_campana",
                "_anotar_evento",
                "_finalizar_campana",
                "_solicitar_paro",
                "_limpiar_registro",
                "_snapshot",
                "_pintar_proceso",
                "_visor_granja",
            )
        ),
    )

    # ---------------- 2) Fuente: fuera la logica vieja de firmas ----------------
    fuente = _fuente_change()
    identificadores = _identificadores_codigo(fuente)
    check(
        "change fuente: sin monkeypatch de input, firmas ni ChangeOrgBot "
        "(solo codigo, no la prosa del docstring)",
        not (
            identificadores
            & {
                "builtins",
                "ChangeOrgBot",
                "firmas_por_ip",
                "reconexiones",
                "input_auto",
                "_INPUT_ORIGINAL",
            }
        ),
        ascii(sorted(identificadores & {"builtins", "ChangeOrgBot"}))[:120],
    )
    check(
        "change fuente: backend perezoso, granja y panel persistente",
        "from cuentas.change_org import ejecutar_campana_reportes" in fuente
        and "CuentaChange" in fuente
        and "obtener_sesion" in fuente
        and "_render_proceso_activo" in fuente
        and "btn_change_reportes" in fuente,
    )

    # ---------------- 3) Registro puro (sin Streamlit) ----------------
    change._limpiar_registro()
    id_campana = change._registrar_campana()
    entrada = change._CAMPANAS.get(id_campana) or {}
    check(
        "registro: la entrada nace en_curso con los campos del contrato",
        entrada.get("estado") == "en_curso"
        and isinstance(entrada.get("evento"), threading.Event)
        and isinstance(entrada.get("log"), list)
        and all(
            campo in entrada
            for campo in (
                "id",
                "hilo",
                "evento",
                "estado",
                "hechas",
                "total",
                "enviados",
                "fallidos",
                "log",
                "resumen",
                "error",
            )
        ),
        str(sorted(entrada)),
    )
    change._anotar_evento(id_campana, {"tipo": "inicio", "total": 2})
    check(
        "registro: el evento 'inicio' fija el total",
        change._CAMPANAS[id_campana]["total"] == 2,
    )
    change._anotar_evento(
        id_campana,
        {
            "tipo": "reporte",
            "hechas": 1,
            "total": 2,
            "ok": True,
            "email": "ana@example.com",
            "identidad": {"nombre": "Ana"},
            "detalle": "",
        },
    )
    entrada = change._CAMPANAS[id_campana]
    check(
        "registro: reporte ok incrementa hechas/enviados/identidades y loggea",
        entrada["hechas"] == 1
        and entrada["enviados"] == 1
        and entrada["fallidos"] == 0
        and entrada["identidades"] == 1
        and entrada["log"][-1] == "✅ Reporte enviado por ana@example.com",
        ascii(str(entrada["log"])),
    )
    change._anotar_evento(
        id_campana,
        {
            "tipo": "reporte",
            "hechas": 2,
            "total": 2,
            "ok": False,
            "email": "bob@example.com",
            "identidad": {"nombre": "Bob"},
            "detalle": "captcha",
        },
    )
    entrada = change._CAMPANAS[id_campana]
    check(
        "registro: reporte fallido incrementa fallidos y loggea el detalle",
        entrada["hechas"] == 2
        and entrada["fallidos"] == 1
        and entrada["log"][-1] == "❌ bob@example.com — captcha",
        ascii(str(entrada["log"][-1])),
    )
    for indice in range(250):
        change._anotar_evento(
            id_campana,
            {
                "tipo": "reporte",
                "hechas": 3 + indice,
                "total": 300,
                "ok": bool(indice % 2 == 0),
                "email": f"cuenta{indice}@example.com",
                "identidad": {"nombre": "X"},
                "detalle": "test",
            },
        )
    check(
        "registro: el log se recorta a LOG_MAX (200) lineas",
        len(change._CAMPANAS[id_campana]["log"]) == change.LOG_MAX,
        str(len(change._CAMPANAS[id_campana]["log"])),
    )
    change._finalizar_campana(
        id_campana,
        resumen={
            "total": 2,
            "enviados": 9,
            "fallidos": 3,
            "cancelada": False,
            "resultados": [],
        },
    )
    entrada = change._CAMPANAS[id_campana]
    check(
        "registro: _finalizar_campana marca terminada, guarda resumen y "
        "sincroniza contadores",
        entrada["estado"] == "terminada"
        and entrada["resumen"]["enviados"] == 9
        and entrada["enviados"] == 9
        and entrada["fallidos"] == 3
        and entry_fin(entrada),
        str(entrada["estado"]),
    )
    check(
        "registro: sin campana activa _campana_en_curso() es None",
        change._campana_en_curso() is None,
    )

    id_cancelada = change._registrar_campana()
    change._finalizar_campana(id_cancelada, resumen={"cancelada": True})
    check(
        "registro: resumen con cancelada=True -> estado 'cancelada'",
        change._CAMPANAS[id_cancelada]["estado"] == "cancelada",
    )
    id_error = change._registrar_campana()
    change._finalizar_campana(id_error, error=RuntimeError("boom de prueba"))
    check(
        "registro: error -> estado 'error' con el detalle",
        change._CAMPANAS[id_error]["estado"] == "error"
        and "boom de prueba" in change._CAMPANAS[id_error]["error"],
    )

    id_paro = change._registrar_campana()
    pedido = change._solicitar_paro()
    evento_paro = change._CAMPANAS[id_paro]["evento"]
    check(
        "registro: _solicitar_paro setea el Event y pasa a deteniendo",
        pedido is True
        and evento_paro.is_set()
        and change._CAMPANAS[id_paro]["estado"] == "deteniendo"
        and change._campana_en_curso() is not None,
    )
    check(
        "registro: el snapshot expone contadores/log y en_curso/deteniendo",
        isinstance(change._snapshot(), dict)
        and change._snapshot()["deteniendo"] is True
        and isinstance(change._snapshot()["log"], list),
    )
    change._limpiar_registro()
    check(
        "registro: _limpiar_registro deja el registro vacio",
        change._CAMPANAS == {},
    )

    # ---------------- 4) Fuente de web/app.py ----------------
    constantes = _leer_constantes_app()
    opciones = constantes.get("OPCIONES") or []
    categorias = constantes.get("CATEGORIAS") or []
    check(
        "app: 'Change.org: Reportes' esta en OPCIONES (con su emoji)",
        "✍️ Change.org: Reportes" in opciones,
        f"(opciones={len(opciones)})",
    )
    check(
        "app: 'Change.org: Reportes' sigue en Monitoreo y respuesta",
        any(
            "✍️ Change.org: Reportes" in ops and "Monitoreo" in nombre
            for nombre, ops in categorias
        ),
    )
    check(
        "app: la etiqueta vieja 'Peticiones' desaparecio",
        "✍️ Change.org: Peticiones" not in opciones
        and all(
            "✍️ Change.org: Peticiones" not in ops for _, ops in categorias
        ),
    )
    fuente_app = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    check(
        "app: el dispatch de 'Actividad' sigue ANTES del prefijo generico "
        "de Change.org",
        'startswith("✍️ Actividad")' in fuente_app
        and fuente_app.index('startswith("✍️ Actividad")')
        < fuente_app.index('startswith("✍️")'),
    )

    # ---------------- 5) AppTest: render y widgets ----------------
    fake = _BackendReportesFake()
    with _BackendParcheado(fake), _GranjaParcheada(change):
        at = AppTest.from_function(_app_change, default_timeout=90)
        at.run()
        excepcion = str(at.exception[0].value)[:200] if at.exception else ""
        entrada_url = _widget_por_key(at, "text_input", "change_rep_url")
        entrada_contexto = _widget_por_key(at, "text_area", "change_rep_contexto")
        cantidad = _widget_por_key(at, "number_input", "change_rep_cantidad")
        workers = _widget_por_key(at, "number_input", "change_rep_workers")
        boton = _widget_por_key(at, "button", "btn_change_reportes")
        check(
            "change AppTest: render con usuario admin sin excepciones",
            not at.exception,
            excepcion,
        )
        check(
            "change AppTest: URL y motivo con labels/keys exactos",
            entrada_url is not None
            and entrada_url.label == "URL de la petición objetivo"
            and "https://www.change.org/p/" in str(entrada_url.placeholder)
            and entrada_contexto is not None
            and "Motivo" in entrada_contexto.label
            and "contexto" in entrada_contexto.label.lower(),
            f"(url={getattr(entrada_url, 'label', None)!r} "
            f"ctx={getattr(entrada_contexto, 'label', None)!r})",
        )
        check(
            "change AppTest: cantidad 1-100 default 5 y workers 1-4 default 2",
            cantidad is not None
            and int(cantidad.value) == 5
            and int(cantidad.min) == 1
            and int(cantidad.max) == 100
            and workers is not None
            and int(workers.value) == 2
            and int(workers.min) == 1
            and int(workers.max) == 4,
            f"(cant={getattr(cantidad, 'value', None)} "
            f"workers={getattr(workers, 'value', None)})",
        )
        check(
            "change AppTest: boton 'Lanzar ataque de reportes' primario",
            boton is not None and "Lanzar ataque de reportes" in boton.label,
            ascii(str(getattr(boton, "label", ""))),
        )
        expanders = [str(e.label) for e in at.expander]
        check(
            "change AppTest: expander 'Opciones avanzadas' presente",
            any("Opciones avanzadas" in etiqueta for etiqueta in expanders),
            ascii(expanders)[:200],
        )
        checkbox_proxies = _widget_por_key(at, "checkbox", "change_rep_proxies")
        checkbox_guardar = _widget_por_key(at, "checkbox", "change_rep_guardar")
        checkbox_visible = _widget_por_key(at, "checkbox", "change_rep_visible")
        pais = _widget_por_key(at, "selectbox", "change_rep_pais")
        check(
            "change AppTest: opciones avanzadas con defaults (proxies ON, "
            "guardar ON, Chrome visible OFF)",
            checkbox_proxies is not None
            and bool(checkbox_proxies.value) is True
            and checkbox_guardar is not None
            and bool(checkbox_guardar.value) is True
            and checkbox_visible is not None
            and bool(checkbox_visible.value) is False,
        )
        check(
            "change AppTest: selector de pais del proxy arranca en 'Todas' y "
            "nunca lanza sin carpeta",
            pais is not None
            and str(pais.value) == change.PAIS_TODAS
            and change.PAIS_TODAS in list(pais.options),
            ascii(str(getattr(pais, "options", [])))[:200],
        )
        check(
            "change AppTest: helper de proxies devuelve int y paises sin "
            "'quemados'",
            isinstance(change._proxies_disponibles(), int)
            and change._proxies_disponibles() >= 0
            and isinstance(change._paises_proxy(), list)
            and "quemados" not in change._paises_proxy(),
        )

    # ---------------- 6) Visor de la granja ----------------
    filas_granja = [
        {
            "Nombre": f"Persona {i}",
            "Email": f"persona{i}@example.com",
            "CP": str(1000 + i),
            "Petición": "https://www.change.org/p/demo",
            "Fecha": "2026-09-28 10:00",
        }
        for i in range(3)
    ]
    with _BackendParcheado(_BackendReportesFake()), _GranjaParcheada(
        change, resultado=(3, filas_granja)
    ):
        at_granja = AppTest.from_function(_app_change, default_timeout=90)
        at_granja.run()
        expanders_granja = [str(e.label) for e in at_granja.expander]
        check(
            "change AppTest: visor de granja con (N) y dataframe sin excepciones",
            not at_granja.exception
            and any(
                "Identidades creadas para firmar (3)" in etiqueta
                for etiqueta in expanders_granja
            )
            and any(
                {"Nombre", "Email", "CP"} <= set(df.value.columns)
                for df in at_granja.dataframe
            ),
            ascii(expanders_granja)[:220] + excepcion_app(at_granja),
        )
    with _BackendParcheado(_BackendReportesFake()), _GranjaParcheada(
        change, error=RuntimeError("sin tabla")
    ):
        at_granja_error = AppTest.from_function(_app_change, default_timeout=90)
        at_granja_error.run()
        check(
            "change AppTest: sin BD el visor muestra caption informativo "
            "(pagina viva)",
            not at_granja_error.exception
            and "no está disponible" in _textos(at_granja_error),
            excepcion_app(at_granja_error),
        )

    # ---------------- 7) Validaciones (backend NO llamado) ----------------
    fake_vacio = _BackendReportesFake()
    with _BackendParcheado(fake_vacio), _GranjaParcheada(change):
        change._limpiar_registro()
        at = AppTest.from_function(_app_change, default_timeout=90)
        at.run()
        at.button(key="btn_change_reportes").click().run()
        check(
            "validacion: URL vacia -> warning y backend no llamado",
            not at.exception
            and any(
                "Change.org" in str(w.value) for w in at.warning
            )
            and fake_vacio.llamadas == [],
            ascii([str(w.value) for w in at.warning])[:220],
        )
        at.text_input(key="change_rep_url").input(
            "https://example.com/p/no-es-change"
        ).run()
        at.button(key="btn_change_reportes").click().run()
        check(
            "validacion: URL sin change.org -> warning y backend no llamado",
            not at.exception
            and any(
                "Change.org" in str(w.value) for w in at.warning
            )
            and fake_vacio.llamadas == [],
            ascii([str(w.value) for w in at.warning])[:220],
        )
        at.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at.button(key="btn_change_reportes").click().run()
        check(
            "validacion: contexto vacio -> warning y backend no llamado",
            not at.exception
            and any(
                "motivo general" in str(w.value).lower() for w in at.warning
            )
            and fake_vacio.llamadas == [],
            ascii([str(w.value) for w in at.warning])[:220],
        )

    # ---------------- 8) Campana fake end-to-end ----------------
    fake_bloqueado = _BackendReportesFake()
    fake_bloqueado.modo = "bloqueado"
    with _BackendParcheado(fake_bloqueado), _GranjaParcheada(change):
        change._limpiar_registro()
        at = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at.run()
        at.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at.text_area(key="change_rep_contexto").input(
            "Difunde informacion falsa y acosa a personas"
        ).run()
        at.button(key="btn_change_reportes").click().run()
        excepcion_camp = excepcion_app(at)
        # El callback corre en el hilo del backend: espera al primer reporte y
        # repinta UNA pasada para comprobar el feed en vivo.
        _esperar_eventos(change, minimo=1)
        at.run()
        llamada = dict(fake_bloqueado.llamadas[-1]) if fake_bloqueado.llamadas else {}
        check(
            "campana AppTest: el backend recibe los kwargs congelados",
            bool(llamada)
            and llamada.get("url_peticion") == "https://www.change.org/p/demo"
            and llamada.get("contexto") == "Difunde informacion falsa y acosa a personas"
            and llamada.get("cantidad") == 5
            and llamada.get("max_workers") == 2
            and llamada.get("usar_proxies") is True
            and llamada.get("pais_proxy") == ""
            and llamada.get("guardar_identidades") is True
            and llamada.get("headless") is True
            and isinstance(llamada.get("cancelar"), threading.Event)
            and callable(llamada.get("callback")),
            ascii(
                {
                    clave: llamada.get(clave)
                    for clave in (
                        "url_peticion",
                        "cantidad",
                        "max_workers",
                        "usar_proxies",
                        "guardar_identidades",
                        "headless",
                    )
                }
            ),
        )
        textos_vivo = _textos(at)
        check(
            "campana AppTest: progreso en vivo con contador y feed de exito",
            not at.exception
            and "enviados" in textos_vivo
            and "✅ Reporte enviado por ana@example.com" in textos_vivo
            and "identidades" in textos_vivo,
            ascii(textos_vivo)[:240] + excepcion_camp,
        )
        boton_detener = _widget_por_key(at, "button", "change_btn_detener")
        activa = change._campana_en_curso()
        check(
            "campana AppTest: boton 'Detener' presente mientras corre",
            boton_detener is not None
            and "Detener" in str(boton_detener.label)
            and activa is not None,
            ascii(str(getattr(boton_detener, "label", ""))),
        )
        # Segundo lanzamiento con el ataque en curso: avisa y NO llama de nuevo.
        at.button(key="btn_change_reportes").click().run()
        check(
            "campana AppTest: ataque en curso -> warning y sin doble backend",
            not at.exception
            and any(
                "ya hay un ataque de reportes en curso" in str(w.value).lower()
                for w in at.warning
            )
            and len(fake_bloqueado.llamadas) == 1,
            ascii([str(w.value) for w in at.warning])[:220],
        )
        at.button(key="change_btn_detener").click().run()
        evento_campana = activa["evento"] if activa is not None else None
        check(
            "campana AppTest: 'Detener' setea el Event del registro",
            not at.exception
            and evento_campana is not None
            and evento_campana.is_set()
            and str(
                change._CAMPANAS.get(activa["id"], {}).get("estado")
            ) == "deteniendo",
            f"(estado={change._CAMPANAS.get(activa['id'], {}).get('estado')})",
        )
        fake_bloqueado.bloquear.set()
        check(
            "campana AppTest: al liberar, el hilo termina y el guard queda libre",
            _esperar_campana_libre(change),
        )
        at.run()
        textos_detenido = _textos(at)
        check(
            "campana AppTest: resumen final de detenido en el panel",
            not at.exception and "detenido" in textos_detenido.lower(),
            ascii(textos_detenido)[:240],
        )

    # ---------------- 9) Resumen final exitoso + dataframe ----------------
    fake_ok = _BackendReportesFake()
    with _BackendParcheado(fake_ok), _GranjaParcheada(change):
        change._limpiar_registro()
        at_final = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at_final.run()
        at_final.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_final.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at_final.button(key="btn_change_reportes").click().run()
        _esperar_campana_libre(change)
        at_final.run()
        textos_final = _textos(at_final)
        check(
            "campana AppTest: al terminar pinta st.success con el resumen",
            not at_final.exception
            and any(
                "terminado" in str(s.value) and "1 enviados" in str(s.value)
                for s in at_final.success
            ),
            ascii([str(s.value) for s in at_final.success])[:240]
            + excepcion_app(at_final),
        )
        tablas = [
            df.value for df in at_final.dataframe
        ]
        check(
            "campana AppTest: dataframe de resultados (Email/OK/Detalle)",
            any(
                hasattr(tabla, "columns")
                and {"Email", "OK", "Detalle"} <= set(tabla.columns)
                and len(tabla) == 2
                for tabla in tablas
            ),
            ascii(str([list(t.columns) for t in tablas]))[:220],
        )
        check(
            "campana AppTest: caption con identidades guardadas",
            any(
                "Identidades guardadas: 2" in str(c.value)
                for c in at_final.caption
            ),
            ascii([str(c.value) for c in at_final.caption])[:220],
        )
        check(
            "campana AppTest: el registro quedo terminado y con resumen",
            (change._snapshot() or {}).get("estado") == "terminada"
            and (change._snapshot() or {}).get("resumen", {}).get("total") == 2,
        )
        change._limpiar_registro()


def entry_fin(entrada: dict) -> bool:
    """True si la entrada quedo con timestamp de fin (campana cerrada)."""
    return bool(entrada.get("fin"))


def excepcion_app(at) -> str:
    """Detalle de la primera excepcion de un AppTest ('' si no hay)."""
    if not at.exception:
        return ""
    return ascii(str(at.exception[0].value))[:200]


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_change_web.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
