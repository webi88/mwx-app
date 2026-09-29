"""Tests del dashboard web de la operacion CHANGE.ORG: REPORTES.

La pagina `web/operaciones/change.py` dejo de ser el formulario de FIRMAS
(monkeypatch de `builtins.input`, `firmas_por_ip`, `reconexiones`,
`ChangeOrgBot`) y ahora lanza un ATAQUE DE REPORTES masivo con identidades
generadas por IA a traves del backend congelado
`cuentas.change_org.ejecutar_campana_reportes`.

Desde 2026-09-29 la pagina tiene DOS pestanas:

  1. "🚩 Ataque de reportes": modo "🧾 Con cuentas registradas (recomendado)"
     (default) que pasa `cuentas=[{usuario,email,email_password,
     nombre_mostrado}]` al backend, o modo "🎭 Anónimo (identidades IA)" con
     `cuentas=None` (comportamiento clasico).
  2. "🧾 Cuentas Change.org": registro/login en Change.org con
     `cuentas.change_org.ejecutar_campana_registros` (import perezoso).

Cubren, SIN Chrome, SIN red y SIN campanas reales (fakes + monkeypatch):

  (1) Contrato del modulo: `render` conservado, registro a nivel modulo
      (`_CAMPANAS` + `_CAMPANAS_LOCK`), `LOG_MAX` y firma de
      `_render_proceso_activo(max_pasos=None)`.
  (2) Registro puro: `_registrar_campana` (campos del contrato + tipo),
      `_anotar_evento` (inicio/reporte/registro, log exacto y tope de 200),
      `_finalizar_campana` (terminada/cancelada/error y sincronizacion de
      exitosos/nuevas/existentes), `_solicitar_paro` y `_limpiar_registro`.
  (3) Fuente de `web/app.py`: "✍️ Change.org: Reportes" en OPCIONES y en
      CATEGORIAS, sin "Peticiones", y el dispatch de "✍️ Actividad" ANTES del
      prefijo generico "✍️".
  (4) Fuente de `change.py`: sin la logica vieja de firmas, con el backend
      perezoso (reportes y registros), `st.tabs`, selector por pestana y visor
      de la granja.
  (5) AppTest: render con usuario fake admin sin excepciones, 2 pestanas,
      radio de modo con default "Con cuentas", widgets/labels/keys/defaults
      exactos, expander de opciones avanzadas, selector de pais y visor de
      granja (helper monkeado, con y sin error de BD).
  (6) Validaciones: URL vacia, URL sin change.org, contexto vacio y modo con
      cuentas sin seleccion -> warning y backend NO llamado.
  (7) Campana fake de reportes end-to-end: kwargs congelados (anonimo con
      `cuentas=None`; con cuentas con la lista completa), progreso en vivo con
      feed "✅ Reporte enviado por", boton "⛔ Detener", aviso de "ya hay un
      ataque en curso" y resumen final.
  (8) Campana fake de registros end-to-end: kwargs de
      `ejecutar_campana_registros`, panel "🧾 Registro de cuentas en vivo",
      contador nuevas/existentes, "⛔ Detener" y resumen final con el
      dataframe Usuario/Email/Estado/Detalle.
  (9) Modo asistido anti-bot (Cloudflare): checkboxes `change_rep_captcha` y
      `change_reg_captcha` (default OFF) y number_input de segundos
      (`change_rep_captcha_seg` / `change_reg_captcha_seg`, 30-900 default
      180); con el modo marcado Chrome visible queda forzado
      (`headless=False`) aunque el checkbox de visible siga apagado y el
      backend moderno recibe `esperar_captcha_seg=180`; con un backend viejo
      se lanza SIN el kwarg y se avisa con `st.warning`; el evento
      `espera_captcha` agrega la linea 🧠 al log del panel sin contar como
      exito/fallo (y respeta el recorte LOG_MAX).

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

# Cuentas fake con correo y contrasena (el formato que carga la pagina desde
# la BD): son el material de los selectores y del backend.
FILAS_CUENTAS = [
    {
        "usuario": "ana",
        "status": "active",
        "seccion": "CI",
        "tipo_cuenta": "ciudadana",
        "handle_actual": "ana_x",
        "grupo": "A",
        "rol_activacion": "hashtags",
        "tier_calidad": "tier1",
        "pausada_activacion": False,
        "email": "ana@example.com",
        "email_password": "pass-ana",
        "nombre_mostrado": "Ana López",
    },
    {
        "usuario": "bob",
        "status": "active",
        "seccion": "LIB",
        "tipo_cuenta": "activista",
        "handle_actual": "",
        "grupo": "B",
        "rol_activacion": "rt",
        "tier_calidad": "tier2",
        "pausada_activacion": False,
        "email": "bob@example.com",
        "email_password": "pass-bob",
        "nombre_mostrado": "Bob",
    },
]


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


class _BackendRegistrosFake:
    """Fake de `ejecutar_campana_registros` (sin Chrome, sin red, sin BD)."""

    def __init__(self):
        self.llamadas: list = []
        self.modo = "instantaneo"  # "instantaneo" | "bloqueado"
        self.bloquear = threading.Event()
        self.eventos = [
            {"tipo": "inicio", "total": 3},
            {
                "tipo": "registro",
                "hechas": 1,
                "total": 3,
                "ok": True,
                "usuario": "ana",
                "email": "ana@example.com",
                "estado": "nueva",
                "detalle": "",
            },
            {
                "tipo": "registro",
                "hechas": 2,
                "total": 3,
                "ok": True,
                "usuario": "bob",
                "email": "bob@example.com",
                "estado": "existente",
                "detalle": "",
            },
            {
                "tipo": "registro",
                "hechas": 3,
                "total": 3,
                "ok": False,
                "usuario": "carla",
                "email": "carla@example.com",
                "estado": "fallo",
                "detalle": "captcha",
            },
        ]
        self.resumen = {
            "total": 3,
            "exitosos": 2,
            "fallidos": 1,
            "nuevas": 1,
            "existentes": 1,
            "omitidas": 0,
            "cancelada": False,
            "resultados": [
                {
                    "ok": True,
                    "usuario": "ana",
                    "email": "ana@example.com",
                    "estado": "nueva",
                    "detalle": "",
                },
                {
                    "ok": True,
                    "usuario": "bob",
                    "email": "bob@example.com",
                    "estado": "existente",
                    "detalle": "",
                },
                {
                    "ok": False,
                    "usuario": "carla",
                    "email": "carla@example.com",
                    "estado": "fallo",
                    "detalle": "captcha",
                },
            ],
            "proxies_total": 2,
            "sin_proxy": False,
            "error": "",
        }

    def __call__(self, **kwargs):
        self.llamadas.append(dict(kwargs))
        cb = kwargs.get("callback")
        cancelar = kwargs.get("cancelar")
        # Bloqueado: emite inicio + 2 registros (nueva y existente) y espera.
        eventos = self.eventos[:3] if self.modo == "bloqueado" else self.eventos
        for evento in eventos:
            if callable(cb):
                try:
                    cb(evento)
                except Exception:
                    pass
        if self.modo == "bloqueado":
            self.bloquear.wait(timeout=15.0)
            resumen = dict(self.resumen)
            resumen["cancelada"] = bool(
                cancelar is not None and cancelar.is_set()
            )
            return resumen
        return dict(self.resumen)


class _BackendReportesViejo:
    """Fake con la firma VIEJA de `ejecutar_campana_reportes` (sin `cuentas`).

    Comprueba la tolerancia a un backend anterior al parametro nuevo: en modo
    anonimo la pagina lo llama igual (SIN pasar `cuentas`) y en modo con
    cuentas avisa con `st.error` sin lanzar."""

    def __init__(self):
        self.llamadas: list = []
        self.resumen = {
            "total": 0,
            "enviados": 0,
            "fallidos": 0,
            "cancelada": False,
            "identidades_guardadas": 0,
            "resultados": [],
            "proxies_total": 0,
            "sin_proxy": 0,
            "error": "",
        }

    def __call__(
        self,
        url_peticion,
        contexto="",
        cantidad=5,
        max_workers=2,
        usar_proxies=True,
        pais_proxy="",
        guardar_identidades=True,
        headless=None,
        cancelar=None,
        callback=None,
    ):
        self.llamadas.append(
            {
                "url_peticion": url_peticion,
                "contexto": contexto,
                "cantidad": cantidad,
                "max_workers": max_workers,
                "usar_proxies": usar_proxies,
                "pais_proxy": pais_proxy,
                "guardar_identidades": guardar_identidades,
                "headless": headless,
                "cancelar": cancelar,
                "callback": callback,
            }
        )
        if callable(callback):
            try:
                callback({"tipo": "inicio", "total": 0})
            except Exception:
                pass
        return dict(self.resumen)


class _BackendRegistrosViejo:
    """Fake con la firma VIEJA de `ejecutar_campana_registros` (sin captcha).

    Comprueba la retrocompatibilidad del modo asistido: la pagina lanza SIN
    `esperar_captcha_seg` y muestra un `st.warning` de que el modo asistido no
    esta disponible en ese backend."""

    def __init__(self):
        self.llamadas: list = []
        self.resumen = {
            "total": 0,
            "exitosos": 0,
            "fallidos": 0,
            "nuevas": 0,
            "existentes": 0,
            "omitidas": 0,
            "cancelada": False,
            "resultados": [],
            "proxies_total": 0,
            "sin_proxy": 0,
            "error": "",
        }

    def __call__(
        self,
        cuentas,
        max_workers=2,
        usar_proxies=True,
        pais_proxy="",
        headless=None,
        cancelar=None,
        callback=None,
    ):
        self.llamadas.append(
            {
                "cuentas": list(cuentas or []),
                "max_workers": max_workers,
                "usar_proxies": usar_proxies,
                "pais_proxy": pais_proxy,
                "headless": headless,
                "cancelar": cancelar,
                "callback": callback,
            }
        )
        if callable(callback):
            try:
                callback({"tipo": "inicio", "total": 0})
            except Exception:
                pass
        return dict(self.resumen)


class _BackendParcheado:
    """Monkeypatch temporal de una funcion de `cuentas.change_org`.

    Inyecta el atributo aunque no exista todavia (el agente del backend puede
    estar implementando `ejecutar_campana_registros` en paralelo) y lo borra
    al salir si no existia antes."""

    def __init__(self, fake, nombre: str = "ejecutar_campana_reportes"):
        self.fake = fake
        self.nombre = nombre
        self.modulo = None
        self.original = None
        self.existia = False

    def __enter__(self):
        import cuentas.change_org as modulo

        self.modulo = modulo
        self.existia = hasattr(modulo, self.nombre)
        self.original = getattr(modulo, self.nombre, None)
        setattr(modulo, self.nombre, self.fake)
        return self.fake

    def __exit__(self, *exc):
        if self.existia:
            setattr(self.modulo, self.nombre, self.original)
        else:
            try:
                delattr(self.modulo, self.nombre)
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


class _CuentasChangeParcheadas:
    """Monkeypatch temporal de `change._cargar_cuentas_change` (sin BD)."""

    def __init__(self, change_mod, filas=None):
        self.change = change_mod
        self.filas = [dict(f) for f in (FILAS_CUENTAS if filas is None else filas)]
        self.original = None

    def __enter__(self):
        self.original = self.change._cargar_cuentas_change
        filas = [dict(f) for f in self.filas]

        def _fake():
            return filas

        self.change._cargar_cuentas_change = _fake
        return self

    def __exit__(self, *exc):
        self.change._cargar_cuentas_change = self.original
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
    """Espera a que el callback haya anotado al menos `minimo` acciones."""
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


def _llamada(fake) -> dict:
    """Ultima llamada registrada por un fake ({} si aun no llamaron)."""
    return dict(fake.llamadas[-1]) if fake.llamadas else {}


def _click_lanzar(at, key_boton):
    """Click en un boton de lanzamiento devolviendo el AppTest re-ejecutado."""
    at.button(key=key_boton).click().run()
    return at


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
                "_cargar_cuentas_change",
                "_cuentas_para_backend",
                "_filas_resultados_registros",
                "_lanzar_registros",
                "_acepta_kwarg",
            )
        ),
    )
    check(
        "change: constantes de tipos y modos con los textos exactos",
        change.TIPO_REPORTES == "reportes"
        and change.TIPO_REGISTROS == "registros"
        and change.MODO_CON_CUENTAS
        == "🧾 Con cuentas registradas (recomendado)"
        and change.MODO_ANONIMO == "🎭 Anónimo (identidades IA)",
        ascii(
            f"({change.MODO_CON_CUENTAS!r} / {change.MODO_ANONIMO!r})"
        ),
    )
    check(
        "change: _acepta_kwarg detecta kwargs explicitos y **kwargs",
        change._acepta_kwarg(lambda **kw: None, "cuentas") is True
        and change._acepta_kwarg(lambda x=1: None, "cuentas") is False
        and change._acepta_kwarg(
            lambda **kw: None, "no_existe"
        ) is True,
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
    check(
        "change fuente: 2 pestanas, registro perezoso y selectores por pestana",
        "st.tabs(" in fuente
        and "ejecutar_campana_registros" in fuente
        and 'getattr(backend, "ejecutar_campana_registros", None)' in fuente
        and "change_rep_selector" in fuente
        and "change_reg_selector" in fuente
        and "change_rep_modo" in fuente
        and "btn_change_registros" in fuente,
    )
    check(
        "change fuente: el modo con cuentas se pasa como kwarg `cuentas` al "
        "backend de reportes",
        'kwargs["cuentas"] = cuentas_envio or None' in fuente
        and "_acepta_kwarg(ejecutar_campana_reportes" in fuente,
    )
    check(
        "change fuente: docstring, keys y constantes del modo asistido "
        "(captcha) anti-bot",
        "modo asistido" in fuente.lower()
        and "esperar_captcha_seg" in fuente
        and "espera_captcha" in fuente
        and "CAPTCHA_LABEL" in fuente
        and "CAPTCHA_CAPTION" in fuente
        and "change_rep_captcha" in fuente
        and "change_rep_captcha_seg" in fuente
        and "change_reg_captcha" in fuente
        and "change_reg_captcha_seg" in fuente,
    )
    check(
        "change fuente: el modo asistido fuerza Chrome visible y comprueba el "
        "kwarg con _acepta_kwarg antes de pasarlo",
        "chrome_visible = True" in fuente
        and '_acepta_kwarg(\n        ejecutar_campana_reportes, '
        '"esperar_captcha_seg"\n    )' in fuente
        and '_acepta_kwarg(funcion, "esperar_captcha_seg")' in fuente,
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
        and entrada.get("tipo") == "reportes"
        and all(
            campo in entrada
            for campo in (
                "id",
                "tipo",
                "hilo",
                "evento",
                "estado",
                "hechas",
                "total",
                "enviados",
                "fallidos",
                "nuevas",
                "existentes",
                "omitidas",
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

    # ---------------- 3b) Eventos de REGISTRO de cuentas ----------------
    id_reg = change._registrar_campana(tipo=change.TIPO_REGISTROS)
    entrada_reg = change._CAMPANAS[id_reg]
    check(
        "registro cuentas: la entrada nace con tipo 'registros' y contadores "
        "nuevas/existentes en 0",
        entrada_reg.get("tipo") == "registros"
        and entrada_reg.get("nuevas") == 0
        and entrada_reg.get("existentes") == 0,
    )
    change._anotar_evento(id_reg, {"tipo": "inicio", "total": 3})
    change._anotar_evento(
        id_reg,
        {
            "tipo": "registro",
            "hechas": 1,
            "total": 3,
            "ok": True,
            "usuario": "ana",
            "email": "ana@example.com",
            "estado": "nueva",
            "detalle": "",
        },
    )
    entrada_reg = change._CAMPANAS[id_reg]
    check(
        "registro cuentas: ok 'nueva' suma ok/nuevas y loggea 'cuenta nueva'",
        entrada_reg["hechas"] == 1
        and entrada_reg["enviados"] == 1
        and entrada_reg["nuevas"] == 1
        and entrada_reg["existentes"] == 0
        and entrada_reg["log"][-1] == "✅ @ana — cuenta nueva",
        ascii(str(entrada_reg["log"])),
    )
    change._anotar_evento(
        id_reg,
        {
            "tipo": "registro",
            "hechas": 2,
            "total": 3,
            "ok": True,
            "usuario": "bob",
            "email": "bob@example.com",
            "estado": "existente",
            "detalle": "",
        },
    )
    entrada_reg = change._CAMPANAS[id_reg]
    check(
        "registro cuentas: ok 'existente' suma existentes y loggea el inicio "
        "de sesión",
        entrada_reg["enviados"] == 2
        and entrada_reg["existentes"] == 1
        and entrada_reg["log"][-1]
        == "✅ @bob — ya tenía cuenta (solo inició sesión)",
        ascii(str(entrada_reg["log"][-1])),
    )
    change._anotar_evento(
        id_reg,
        {
            "tipo": "registro",
            "hechas": 3,
            "total": 3,
            "ok": False,
            "usuario": "carla",
            "email": "carla@example.com",
            "estado": "fallo",
            "detalle": "captcha",
        },
    )
    entrada_reg = change._CAMPANAS[id_reg]
    check(
        "registro cuentas: fallo suma fallidos y loggea el detalle",
        entrada_reg["hechas"] == 3
        and entrada_reg["fallidos"] == 1
        and entrada_reg["log"][-1] == "❌ @carla — captcha",
        ascii(str(entrada_reg["log"][-1])),
    )
    change._anotar_evento(
        id_reg,
        {
            "tipo": "registro",
            "hechas": 4,
            "total": 4,
            "ok": False,
            "usuario": "dave",
            "email": "dave@example.com",
            "estado": "omitida",
            "detalle": "sin correo",
        },
    )
    entrada_reg = change._CAMPANAS[id_reg]
    check(
        "registro cuentas: 'omitida' NO suma fallidos, suma omitidas y loggea "
        "el guion",
        entrada_reg["hechas"] == 4
        and entrada_reg["fallidos"] == 1
        and entrada_reg["omitidas"] == 1
        and entrada_reg["log"][-1] == "➖ @dave — sin correo",
        ascii(str(entrada_reg["log"][-1])),
    )
    snap_reg = change._snapshot(id_reg)
    check(
        "registro cuentas: el snapshot expone tipo/nuevas/existentes",
        snap_reg is not None
        and snap_reg["tipo"] == "registros"
        and snap_reg["nuevas"] == 1
        and snap_reg["existentes"] == 1
        and snap_reg["omitidas"] == 1,
    )
    resumen_reg = {
        "total": 4,
        "exitosos": 2,
        "fallidos": 1,
        "nuevas": 1,
        "existentes": 1,
        "omitidas": 1,
        "cancelada": False,
        "resultados": [
            {
                "ok": True,
                "usuario": "ana",
                "email": "ana@example.com",
                "estado": "nueva",
                "detalle": "",
            },
            {
                "ok": True,
                "usuario": "bob",
                "email": "bob@example.com",
                "estado": "existente",
                "detalle": "",
            },
            {
                "ok": False,
                "usuario": "carla",
                "email": "carla@example.com",
                "estado": "fallo",
                "detalle": "captcha",
            },
            {
                "ok": False,
                "usuario": "dave",
                "email": "dave@example.com",
                "estado": "omitida",
                "detalle": "sin correo",
            },
        ],
    }
    change._finalizar_campana(id_reg, resumen=resumen_reg)
    snap_reg = change._snapshot(id_reg)
    check(
        "registro cuentas: _finalizar_campana sincroniza exitosos -> enviados "
        "y guarda nuevas/existentes/omitidas",
        snap_reg["estado"] == "terminada"
        and snap_reg["enviados"] == 2
        and snap_reg["fallidos"] == 1
        and snap_reg["nuevas"] == 1
        and snap_reg["existentes"] == 1
        and snap_reg["omitidas"] == 1,
        str({clave: snap_reg[clave] for clave in ("estado", "enviados")}),
    )
    filas_reg = change._filas_resultados_registros(resumen_reg)
    check(
        "registro cuentas: _filas_resultados_registros mapea "
        "Usuario/Email/Estado/Detalle",
        filas_reg
        == [
            {
                "Usuario": "@ana",
                "Email": "ana@example.com",
                "Estado": "🆕 Nueva",
                "Detalle": "",
            },
            {
                "Usuario": "@bob",
                "Email": "bob@example.com",
                "Estado": "👤 Existente",
                "Detalle": "",
            },
            {
                "Usuario": "@carla",
                "Email": "carla@example.com",
                "Estado": "❌ Fallo",
                "Detalle": "captcha",
            },
            {
                "Usuario": "@dave",
                "Email": "dave@example.com",
                "Estado": "➖ Omitida",
                "Detalle": "sin correo",
            },
        ],
        ascii(str(filas_reg))[:220],
    )
    change._limpiar_registro()

    id_rep_cuenta = change._registrar_campana()
    change._anotar_evento(
        id_rep_cuenta,
        {
            "tipo": "reporte",
            "hechas": 1,
            "total": 1,
            "ok": True,
            "usuario": "ana",
            "con_cuenta": True,
            "detalle": "",
        },
    )
    check(
        "registro: reporte con cuenta (sin email) loggea @usuario y NO cuenta "
        "identidades",
        change._CAMPANAS[id_rep_cuenta]["log"][-1]
        == "✅ Reporte enviado por @ana"
        and change._CAMPANAS[id_rep_cuenta]["identidades"] == 0,
        ascii(str(change._CAMPANAS[id_rep_cuenta]["log"][-1])),
    )

    # ---------------- 3c) Cuentas para el backend ----------------
    cuentas_backend = change._cuentas_para_backend(FILAS_CUENTAS)
    check(
        "cuentas: _cuentas_para_backend arma los dicts exactos del contrato",
        cuentas_backend
        == [
            {
                "usuario": "ana",
                "email": "ana@example.com",
                "email_password": "pass-ana",
                "nombre_mostrado": "Ana López",
            },
            {
                "usuario": "bob",
                "email": "bob@example.com",
                "email_password": "pass-bob",
                "nombre_mostrado": "Bob",
            },
        ],
        ascii(str(cuentas_backend))[:220],
    )
    check(
        "cuentas: _cuentas_para_backend descarta filas sin correo/contrasena",
        change._cuentas_para_backend(
            [
                {"usuario": "sin-mail", "email": "", "email_password": "x"},
                {"usuario": "sin-pass", "email": "a@b.com", "email_password": ""},
                {"usuario": "", "email": "a@b.com", "email_password": "x"},
                "no-dict",
            ]
        )
        == [],
    )

    # ---------------- 3d) Evento `espera_captcha` del modo asistido ----------------
    id_cap = change._registrar_campana()
    change._anotar_evento(id_cap, {"tipo": "inicio", "total": 2})
    change._anotar_evento(
        id_cap,
        {
            "tipo": "espera_captcha",
            "hechas": 1,
            "total": 2,
            "usuario": "ana",
            "email": "ana@example.com",
            "detalle": "esperando captcha (180s)",
        },
    )
    entrada_cap = change._CAMPANAS[id_cap]
    check(
        "captcha registro: el evento agrega la linea con email y segundos y "
        "NO toca contadores (no es exito ni fallo)",
        entrada_cap["log"][-1]
        == "🧠 ana@example.com — esperando a que resuelvas el captcha "
        "en Chrome (180s)…"
        and entrada_cap["hechas"] == 0
        and entrada_cap["enviados"] == 0
        and entrada_cap["fallidos"] == 0
        and entrada_cap["identidades"] == 0,
        ascii(str(entrada_cap["log"][-1])),
    )
    change._anotar_evento(
        id_cap,
        {
            "tipo": "espera_captcha",
            "hechas": 1,
            "total": 2,
            "usuario": "bob",
            "detalle": "esperando captcha (30s)",
        },
    )
    check(
        "captcha registro: sin email usa @usuario en la linea del panel",
        change._CAMPANAS[id_cap]["log"][-1].startswith("🧠 @bob — ")
        and "(30s)" in change._CAMPANAS[id_cap]["log"][-1],
        ascii(str(change._CAMPANAS[id_cap]["log"][-1])),
    )
    for indice in range(250):
        change._anotar_evento(
            id_cap,
            {
                "tipo": "espera_captcha",
                "hechas": 2,
                "total": 2,
                "usuario": "ana",
                "detalle": f"esperando captcha ({indice}s)",
            },
        )
    check(
        "captcha registro: estos eventos tambien respetan el recorte LOG_MAX",
        len(change._CAMPANAS[id_cap]["log"]) == change.LOG_MAX
        and "(249s)" in change._CAMPANAS[id_cap]["log"][-1],
        str(len(change._CAMPANAS[id_cap]["log"])),
    )
    snap_cap = change._snapshot(id_cap)
    check(
        "captcha registro: el snapshot expone la linea del modo asistido",
        snap_cap is not None
        and any(
            "esperando a que resuelvas el captcha" in linea
            for linea in snap_cap["log"]
        ),
        ascii(str(snap_cap["log"][-1])) if snap_cap else "sin snapshot",
    )
    change._limpiar_registro()

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

    # ---------------- 5) AppTest: render, pestanas y widgets ----------------
    fake = _BackendReportesFake()
    with _BackendParcheado(fake), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        at = AppTest.from_function(_app_change, default_timeout=90)
        at.run()
        excepcion = str(at.exception[0].value)[:200] if at.exception else ""
        check(
            "change AppTest: render con usuario admin sin excepciones",
            not at.exception,
            excepcion,
        )
        etiquetas_tabs = [str(t.label) for t in at.tabs]
        check(
            "change AppTest: 2 pestanas (ataque de reportes + cuentas Change)",
            any("Ataque de reportes" in etiqueta for etiqueta in etiquetas_tabs)
            and any("Cuentas Change.org" in etiqueta for etiqueta in etiquetas_tabs),
            ascii(str(etiquetas_tabs))[:200],
        )
        radio_modo = _widget_por_key(at, "radio", "change_rep_modo")
        check(
            "change AppTest: radio 'Modo de reporte' con default "
            "'Con cuentas registradas (recomendado)'",
            radio_modo is not None
            and str(radio_modo.value) == change.MODO_CON_CUENTAS
            and change.MODO_ANONIMO in list(radio_modo.options),
            ascii(str(getattr(radio_modo, "value", None)))[:160],
        )
        entrada_url = _widget_por_key(at, "text_input", "change_rep_url")
        entrada_contexto = _widget_por_key(at, "text_area", "change_rep_contexto")
        cantidad = _widget_por_key(at, "number_input", "change_rep_cantidad")
        workers = _widget_por_key(at, "number_input", "change_rep_workers")
        boton = _widget_por_key(at, "button", "btn_change_reportes")
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
            "change AppTest: cantidad 1-100 default 5 y workers 1-5 default 2",
            cantidad is not None
            and int(cantidad.value) == 5
            and int(cantidad.min) == 1
            and int(cantidad.max) == 100
            and workers is not None
            and int(workers.value) == 2
            and int(workers.min) == 1
            and int(workers.max) == 5,
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
        selector_rep = _widget_por_key(at, "radio", "change_rep_selector_modo")
        selector_reg = _widget_por_key(at, "radio", "change_reg_selector_modo")
        check(
            "change AppTest: selector de cuentas por pestana (keys distintas)",
            selector_rep is not None and selector_reg is not None,
            f"(rep={selector_rep is not None} reg={selector_reg is not None})",
        )
        checkbox_proxies = _widget_por_key(at, "checkbox", "change_rep_proxies")
        checkbox_visible = _widget_por_key(at, "checkbox", "change_rep_visible")
        checkbox_guardar = _widget_por_key(at, "checkbox", "change_rep_guardar")
        pais = _widget_por_key(at, "selectbox", "change_rep_pais")
        check(
            "change AppTest: opciones avanzadas con defaults (proxies ON, "
            "Chrome visible OFF) y SIN checkbox de identidades en modo cuentas",
            checkbox_proxies is not None
            and bool(checkbox_proxies.value) is True
            and checkbox_visible is not None
            and bool(checkbox_visible.value) is False
            and checkbox_guardar is None,
        )
        captcha_rep = _widget_por_key(at, "checkbox", "change_rep_captcha")
        captcha_rep_seg = _widget_por_key(
            at, "number_input", "change_rep_captcha_seg"
        )
        captcha_reg = _widget_por_key(at, "checkbox", "change_reg_captcha")
        captcha_reg_seg = _widget_por_key(
            at, "number_input", "change_reg_captcha_seg"
        )
        check(
            "change AppTest: modo asistido presente en AMBAS pestanas con "
            "default OFF (sin number_input de segundos al inicio)",
            captcha_rep is not None
            and bool(captcha_rep.value) is False
            and captcha_rep_seg is None
            and captcha_reg is not None
            and bool(captcha_reg.value) is False
            and captcha_reg_seg is None,
            f"(rep={captcha_rep is not None} reg={captcha_reg is not None})",
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
        # ---- Pestana 2: registro de cuentas ----
        workers_reg = _widget_por_key(at, "number_input", "change_reg_workers")
        boton_reg = _widget_por_key(at, "button", "btn_change_registros")
        caption_reg = any(
            "Se registrarán" in str(c.value)
            and "2" in str(c.value)
            for c in at.caption
        )
        check(
            "change AppTest: pestana de registros con navegadores 1-5 default "
            "2 y caption 'Se registrarán N cuentas'",
            workers_reg is not None
            and int(workers_reg.value) == 2
            and int(workers_reg.min) == 1
            and int(workers_reg.max) == 5
            and caption_reg,
            f"(workers={getattr(workers_reg, 'value', None)} caption={caption_reg})",
        )
        check(
            "change AppTest: boton 'Registrar en Change.org' presente",
            boton_reg is not None
            and "Registrar en Change.org" in boton_reg.label,
            ascii(str(getattr(boton_reg, "label", ""))),
        )

        # ---- Modo anonimo: aparece el checkbox de identidades ----
        at.radio(key="change_rep_modo").set_value(change.MODO_ANONIMO).run()
        checkbox_guardar_anon = _widget_por_key(
            at, "checkbox", "change_rep_guardar"
        )
        selector_rep_anon = _widget_por_key(
            at, "radio", "change_rep_selector_modo"
        )
        check(
            "change AppTest: en modo anónimo aparece 'Guardar identidades' "
            "(default ON) y el selector de cuentas desaparece",
            not at.exception
            and checkbox_guardar_anon is not None
            and bool(checkbox_guardar_anon.value) is True
            and selector_rep_anon is None,
            excepcion_app(at) or ascii(str(checkbox_guardar_anon is not None)),
        )

    # ---------------- 5b) Modo asistido (captcha) anti-bot ----------------
    fake_captcha = _BackendReportesFake()
    with _BackendParcheado(fake_captcha), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_cap = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at_cap.run()
        at_cap.radio(key="change_rep_modo").set_value(change.MODO_ANONIMO).run()
        at_cap.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_cap.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at_cap.checkbox(key="change_rep_captcha").set_value(True).run()
        seg_rep = _widget_por_key(
            at_cap, "number_input", "change_rep_captcha_seg"
        )
        visible_rep = _widget_por_key(at_cap, "checkbox", "change_rep_visible")
        textos_cap = _textos(at_cap)
        check(
            "captcha AppTest: al marcar el modo asistido aparece el "
            "number_input 30-900 default 180 paso 30 y la recomendacion de "
            "usar 1 navegador",
            not at_cap.exception
            and seg_rep is not None
            and int(seg_rep.value) == 180
            and int(seg_rep.min) == 30
            and int(seg_rep.max) == 900
            and int(seg_rep.step) == 30
            and "resuelve el reto de Cloudflare" in textos_cap
            and "1 navegador" in textos_cap,
            ascii(textos_cap)[:240] + excepcion_app(at_cap),
        )
        check(
            "captcha AppTest: el checkbox de Chrome visible queda "
            "deshabilitado y forzado a True sin que nadie lo marque a mano",
            visible_rep is not None
            and bool(visible_rep.value) is True
            and bool(getattr(visible_rep.proto, "disabled", False)) is True,
            ascii(
                f"(value={getattr(visible_rep, 'value', None)} "
                f"disabled={getattr(getattr(visible_rep, 'proto', None), 'disabled', None)})"
            ),
        )
        at_cap.button(key="btn_change_reportes").click().run()
        _esperar_campana_libre(change)
        at_cap.run()
        llamada_cap = _llamada(fake_captcha)
        check(
            "captcha AppTest: backend moderno recibe esperar_captcha_seg=180 "
            "y headless=False pese a tener Chrome visible apagado",
            not at_cap.exception
            and llamada_cap.get("esperar_captcha_seg") == 180
            and llamada_cap.get("headless") is False,
            ascii(
                str(
                    {
                        clave: llamada_cap.get(clave)
                        for clave in ("esperar_captcha_seg", "headless")
                    }
                )
            ),
        )
        change._limpiar_registro()

    # ---------------- 5c) Modo asistido en el registro de cuentas ----------------
    fake_cap_reg = _BackendRegistrosFake()
    with _BackendParcheado(
        fake_cap_reg, nombre="ejecutar_campana_registros"
    ), _GranjaParcheada(change), _CuentasChangeParcheadas(change):
        change._limpiar_registro()
        at_cap_reg = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_cap_reg.run()
        at_cap_reg.checkbox(key="change_reg_captcha").set_value(True).run()
        seg_reg = _widget_por_key(
            at_cap_reg, "number_input", "change_reg_captcha_seg"
        )
        visible_reg = _widget_por_key(
            at_cap_reg, "checkbox", "change_reg_visible"
        )
        check(
            "captcha AppTest registros: number_input 180/30-900, caption del "
            "modo asistido y Chrome visible forzado/deshabilitado",
            not at_cap_reg.exception
            and seg_reg is not None
            and int(seg_reg.value) == 180
            and int(seg_reg.min) == 30
            and int(seg_reg.max) == 900
            and int(seg_reg.step) == 30
            and visible_reg is not None
            and bool(visible_reg.value) is True
            and bool(getattr(visible_reg.proto, "disabled", False)) is True
            and "ventana de Chrome" in _textos(at_cap_reg),
            ascii(_textos(at_cap_reg))[:240] + excepcion_app(at_cap_reg),
        )
        at_cap_reg.button(key="btn_change_registros").click().run()
        _esperar_campana_libre(change)
        at_cap_reg.run()
        llamada_cap_reg = _llamada(fake_cap_reg)
        check(
            "captcha AppTest registros: el backend recibe "
            "esperar_captcha_seg=180 y headless=False",
            not at_cap_reg.exception
            and llamada_cap_reg.get("esperar_captcha_seg") == 180
            and llamada_cap_reg.get("headless") is False,
            ascii(
                str(
                    {
                        clave: llamada_cap_reg.get(clave)
                        for clave in ("esperar_captcha_seg", "headless")
                    }
                )
            ),
        )
        change._limpiar_registro()

    # ---------------- 5d) Backend VIEJO sin modo asistido ----------------
    viejo_cap = _BackendReportesViejo()
    with _BackendParcheado(viejo_cap), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_viejo_cap = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_viejo_cap.run()
        at_viejo_cap.radio(key="change_rep_modo").set_value(
            change.MODO_ANONIMO
        ).run()
        at_viejo_cap.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_viejo_cap.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at_viejo_cap.checkbox(key="change_rep_captcha").set_value(True).run()
        at_viejo_cap.button(key="btn_change_reportes").click().run()
        _esperar_campana_libre(change)
        check(
            "captcha compat: backend viejo -> lanza SIN el kwarg y avisa que "
            "el modo asistido no esta disponible",
            not at_viejo_cap.exception
            and len(viejo_cap.llamadas) == 1
            and "esperar_captcha_seg" not in viejo_cap.llamadas[0]
            and any(
                "asistido" in str(w.value).lower()
                for w in at_viejo_cap.warning
            ),
            ascii([str(w.value) for w in at_viejo_cap.warning])[:240]
            + excepcion_app(at_viejo_cap),
        )
        change._limpiar_registro()

    viejo_cap_reg = _BackendRegistrosViejo()
    with _BackendParcheado(
        viejo_cap_reg, nombre="ejecutar_campana_registros"
    ), _GranjaParcheada(change), _CuentasChangeParcheadas(change):
        change._limpiar_registro()
        at_viejo_cap_reg = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_viejo_cap_reg.run()
        at_viejo_cap_reg.checkbox(key="change_reg_captcha").set_value(
            True
        ).run()
        at_viejo_cap_reg.button(key="btn_change_registros").click().run()
        _esperar_campana_libre(change)
        check(
            "captcha compat registros: backend viejo -> lanza sin el kwarg y "
            "warning del modo asistido",
            not at_viejo_cap_reg.exception
            and len(viejo_cap_reg.llamadas) == 1
            and "esperar_captcha_seg" not in viejo_cap_reg.llamadas[0]
            and any(
                "asistido" in str(w.value).lower()
                for w in at_viejo_cap_reg.warning
            ),
            ascii([str(w.value) for w in at_viejo_cap_reg.warning])[:240]
            + excepcion_app(at_viejo_cap_reg),
        )
        change._limpiar_registro()

    # ---------------- 5e) Linea del captcha en el panel en vivo ----------------
    id_cap_panel = change._registrar_campana()
    change._anotar_evento(id_cap_panel, {"tipo": "inicio", "total": 2})
    change._anotar_evento(
        id_cap_panel,
        {
            "tipo": "espera_captcha",
            "hechas": 1,
            "total": 2,
            "usuario": "ana",
            "detalle": "esperando captcha (180s)",
        },
    )
    with _BackendParcheado(_BackendReportesFake()), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        at_panel = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_panel.run()
        textos_panel = _textos(at_panel)
        check(
            "captcha AppTest: la linea 🧠 del modo asistido se ve en el panel "
            "en vivo",
            not at_panel.exception
            and "🧠 @ana — esperando a que resuelvas el captcha" in textos_panel
            and "en Chrome (180s)" in textos_panel,
            ascii(textos_panel)[:240] + excepcion_app(at_panel),
        )
    change._limpiar_registro()

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
    ), _CuentasChangeParcheadas(change):
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
    ), _CuentasChangeParcheadas(change):
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
    with _BackendParcheado(fake_vacio), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
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
        # Modo con cuentas pero SIN seleccion: avisa y no llama al backend.
        at.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at.radio(key="change_rep_selector_modo").set_value(
            "Manual (cuenta por cuenta)"
        ).run()
        at.button(key="btn_change_reportes").click().run()
        check(
            "validacion: modo con cuentas sin selección -> warning y backend "
            "no llamado",
            not at.exception
            and any(
                "selecciona al menos una cuenta" in str(w.value).lower()
                for w in at.warning
            )
            and fake_vacio.llamadas == [],
            ascii([str(w.value) for w in at.warning])[:240],
        )

    # ---------------- 8) Campana fake de reportes end-to-end ----------------
    fake_bloqueado = _BackendReportesFake()
    fake_bloqueado.modo = "bloqueado"
    with _BackendParcheado(fake_bloqueado), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at.run()
        at.radio(key="change_rep_modo").set_value(change.MODO_ANONIMO).run()
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
        llamada = _llamada(fake_bloqueado)
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
        check(
            "campana AppTest: modo anónimo pasa cuentas=None al backend",
            llamada.get("cuentas") is None,
            ascii(str(llamada.get("cuentas"))),
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
    with _BackendParcheado(fake_ok), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_final = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at_final.run()
        at_final.radio(key="change_rep_modo").set_value(change.MODO_ANONIMO).run()
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

    # ---------------- 10) Reportes CON cuentas (round-robin) ----------------
    fake_cuentas = _BackendReportesFake()
    with _BackendParcheado(fake_cuentas), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_c = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at_c.run()
        at_c.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_c.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        # El selector arranca en modo Filtro (todas), asi que selecciona las 2.
        at_c.button(key="btn_change_reportes").click().run()
        check(
            "campana AppTest: modo con cuentas -> el registro queda tipo "
            "'reportes' con cuentas_total=2",
            not at_c.exception
            and len(fake_cuentas.llamadas) == 1
            and (change._snapshot() or {}).get("tipo") == "reportes",
            excepcion_app(at_c),
        )
        _esperar_campana_libre(change)
        at_c.run()
        llamada_c = _llamada(fake_cuentas)
        check(
            "campana AppTest: reportes con cuentas pasa la lista completa y "
            "guardar_identidades=False",
            llamada_c.get("cuentas")
            == [
                {
                    "usuario": "ana",
                    "email": "ana@example.com",
                    "email_password": "pass-ana",
                    "nombre_mostrado": "Ana López",
                },
                {
                    "usuario": "bob",
                    "email": "bob@example.com",
                    "email_password": "pass-bob",
                    "nombre_mostrado": "Bob",
                },
            ]
            and llamada_c.get("guardar_identidades") is False,
            ascii(str(llamada_c.get("cuentas")))[:240],
        )
        change._limpiar_registro()

    # ---------------- 10b) Compatibilidad con el backend VIEJO ----------------
    viejo_anon = _BackendReportesViejo()
    with _BackendParcheado(viejo_anon), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_viejo = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_viejo.run()
        at_viejo.radio(key="change_rep_modo").set_value(
            change.MODO_ANONIMO
        ).run()
        at_viejo.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_viejo.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at_viejo.button(key="btn_change_reportes").click().run()
        _esperar_campana_libre(change)
        check(
            "compat: backend viejo sin `cuentas` funciona en anónimo (SIN "
            "pasar el kwarg)",
            not at_viejo.exception
            and len(viejo_anon.llamadas) == 1
            and "cuentas" not in viejo_anon.llamadas[0]
            and viejo_anon.llamadas[0]["url_peticion"]
            == "https://www.change.org/p/demo",
            excepcion_app(at_viejo),
        )
        change._limpiar_registro()

    viejo_cuentas = _BackendReportesViejo()
    with _BackendParcheado(viejo_cuentas), _GranjaParcheada(change), (
        _CuentasChangeParcheadas(change)
    ):
        change._limpiar_registro()
        at_viejo2 = AppTest.from_function(_app_change, default_timeout=90)
        at_viejo2.run()
        at_viejo2.text_input(key="change_rep_url").input(
            "https://www.change.org/p/demo"
        ).run()
        at_viejo2.text_area(key="change_rep_contexto").input(
            "Incumple las normas de la comunidad"
        ).run()
        at_viejo2.button(key="btn_change_reportes").click().run()
        check(
            "compat: backend viejo en modo con cuentas -> st.error legible y "
            "sin llamada",
            not at_viejo2.exception
            and any(
                "no soporta" in str(e.value).lower() for e in at_viejo2.error
            )
            and viejo_cuentas.llamadas == [],
            ascii([str(e.value) for e in at_viejo2.error])[:220]
            + excepcion_app(at_viejo2),
        )
        change._limpiar_registro()

    # ---------------- 11) Campana fake de REGISTROS end-to-end ----------------
    fake_reg_bloqueado = _BackendRegistrosFake()
    fake_reg_bloqueado.modo = "bloqueado"
    with _BackendParcheado(
        fake_reg_bloqueado, nombre="ejecutar_campana_registros"
    ), _GranjaParcheada(change), _CuentasChangeParcheadas(change):
        change._limpiar_registro()
        at_reg = AppTest.from_function(_app_change_una_pasada, default_timeout=90)
        at_reg.run()
        at_reg.button(key="btn_change_registros").click().run()
        excepcion_reg = excepcion_app(at_reg)
        # El fake bloqueado emite inicio + 2 registros: espera a ambos para
        # revisar el contador y el feed con las dos lineas.
        _esperar_eventos(change, minimo=2)
        at_reg.run()
        llamada_reg = _llamada(fake_reg_bloqueado)
        check(
            "registros AppTest: el backend recibe cuentas completas y los "
            "kwargs congelados",
            bool(llamada_reg)
            and llamada_reg.get("cuentas")
            == [
                {
                    "usuario": "ana",
                    "email": "ana@example.com",
                    "email_password": "pass-ana",
                    "nombre_mostrado": "Ana López",
                },
                {
                    "usuario": "bob",
                    "email": "bob@example.com",
                    "email_password": "pass-bob",
                    "nombre_mostrado": "Bob",
                },
            ]
            and llamada_reg.get("max_workers") == 2
            and llamada_reg.get("usar_proxies") is True
            and llamada_reg.get("pais_proxy") == ""
            and llamada_reg.get("headless") is True
            and isinstance(llamada_reg.get("cancelar"), threading.Event)
            and callable(llamada_reg.get("callback")),
            ascii(
                {
                    clave: llamada_reg.get(clave)
                    for clave in (
                        "max_workers",
                        "usar_proxies",
                        "pais_proxy",
                        "headless",
                    )
                }
            ),
        )
        textos_reg = _textos(at_reg)
        check(
            "registros AppTest: panel en vivo con contador nuevas/existentes "
            "y feed exacto",
            not at_reg.exception
            and "Registro de cuentas en vivo" in textos_reg
            and "nuevas" in textos_reg
            and "✅ @ana — cuenta nueva" in textos_reg
            and "✅ @bob — ya tenía cuenta (solo inició sesión)" in textos_reg,
            ascii(textos_reg)[:240] + excepcion_reg,
        )
        boton_detener_reg = _widget_por_key(at_reg, "button", "change_btn_detener")
        activa_reg = change._campana_en_curso()
        check(
            "registros AppTest: boton 'Detener' presente y setea el Event",
            boton_detener_reg is not None
            and "Detener" in str(boton_detener_reg.label)
            and activa_reg is not None,
            ascii(str(getattr(boton_detener_reg, "label", ""))),
        )
        at_reg.button(key="change_btn_detener").click().run()
        evento_reg = activa_reg["evento"] if activa_reg is not None else None
        check(
            "registros AppTest: 'Detener' setea el Event del registro",
            not at_reg.exception
            and evento_reg is not None
            and evento_reg.is_set()
            and str(
                change._CAMPANAS.get(activa_reg["id"], {}).get("estado")
            ) == "deteniendo",
        )
        fake_reg_bloqueado.bloquear.set()
        _esperar_campana_libre(change)
        at_reg.run()
        textos_reg_paro = _textos(at_reg)
        check(
            "registros AppTest: resumen final de registro detenido",
            not at_reg.exception and "Registro detenido" in textos_reg_paro,
            ascii(textos_reg_paro)[:240],
        )
        change._limpiar_registro()

    fake_reg_ok = _BackendRegistrosFake()
    with _BackendParcheado(
        fake_reg_ok, nombre="ejecutar_campana_registros"
    ), _GranjaParcheada(change), _CuentasChangeParcheadas(change):
        change._limpiar_registro()
        at_reg_ok = AppTest.from_function(
            _app_change_una_pasada, default_timeout=90
        )
        at_reg_ok.run()
        at_reg_ok.button(key="btn_change_registros").click().run()
        _esperar_campana_libre(change)
        at_reg_ok.run()
        textos_reg_ok = _textos(at_reg_ok)
        check(
            "registros AppTest: resumen exitoso con conteos de nuevas/"
            "existentes/fallidos",
            not at_reg_ok.exception
            and any(
                "Registro terminado" in str(s.value)
                and "2 ok" in str(s.value)
                and "1 nuevas" in str(s.value)
                and "1 existentes" in str(s.value)
                and "1 fallidos" in str(s.value)
                for s in at_reg_ok.success
            ),
            ascii([str(s.value) for s in at_reg_ok.success])[:240]
            + excepcion_app(at_reg_ok),
        )
        tabla_reg = [
            df.value
            for df in at_reg_ok.dataframe
            if hasattr(df.value, "columns")
            and {"Usuario", "Email", "Estado", "Detalle"} <= set(df.value.columns)
        ]
        check(
            "registros AppTest: dataframe Usuario/Email/Estado/Detalle con "
            "Nueva/Existente/Fallo",
            bool(tabla_reg)
            and len(tabla_reg[0]) == 3
            and "@ana" in str(tabla_reg[0].iloc[0].to_dict())
            and "🆕 Nueva" in str(tabla_reg[0].iloc[0].to_dict())
            and "👤 Existente" in str(tabla_reg[0].iloc[1].to_dict())
            and "❌ Fallo" in str(tabla_reg[0].iloc[2].to_dict()),
            ascii(str([dict(fila) for fila in tabla_reg[0].to_dict("records")]))[
                :240
            ] if tabla_reg else "sin dataframe",
        )
        check(
            "registros AppTest: el registro quedo terminado tipo 'registros'",
            (change._snapshot() or {}).get("estado") == "terminada"
            and (change._snapshot() or {}).get("tipo") == "registros"
            and (change._snapshot() or {}).get("enviados") == 2,
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
