"""Tests rapidos de la navegacion agrupada del dashboard web.

Verifica (sin Chrome y sin Streamlit runtime) que:
  - La navegacion de `web/app.py` tiene 4 categorias que cubren TODAS las
    operaciones visibles, sin duplicados, y que la operacion por defecto
    (Alertas) sigue existiendo.
  - Las 3 operaciones ocultas (Blogs Web, Visualizaciones, Follows) NO estan
    en el menu, pero conservan su branch de despacho (reactivables en 1 linea).
  - `web/operaciones/cuentas.py` agrupa sus 15 pestanas en 3 modos (sin
    perder ni repetir ninguna) y `TABS` sigue completo.
  - El "🎨 Generar Imagen" del sidebar esta oculto (su funcion sigue viva).
  - El default recomendado de "Navegadores simultaneos" de Activacion Masiva
    es 2.
  - `iniciar_dashboard` se importa sin arrancar Streamlit (def main + guard) y
    `web/operaciones/change.py` NO borra `builtins.input` (lo restaura).

El recorrido completo de operaciones visibles con `AppTest` se corre aparte
(script temporal del agente) porque es lento para la suite rapida.
"""
from __future__ import annotations

import ast
import builtins
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Operaciones retiradas de la navegacion (los archivos siguen existiendo).
OPS_OCULTAS = ["🚀 Impulso: Follows", "👁️ Visualizaciones", "🌐 Blogs Web"]


def _leer_constantes_app() -> dict:
    """Extrae las constantes de nivel de modulo de `web/app.py` con AST.

    No importa `web/app.py` (ejecutaria `main()` y pediria Streamlit runtime);
    solo lee las asignaciones literales."""
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    constantes = {}
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for objetivo in nodo.targets:
                if isinstance(objetivo, ast.Name):
                    try:
                        constantes[objetivo.id] = ast.literal_eval(nodo.value)
                    except Exception:
                        pass
    return constantes


def _prefijos_despacho_app() -> set:
    """Prefijos string usados en llamadas `...startswith("...")` de app.py.

    Con ellos se comprueba que cada operacion (visible u oculta) conserva su
    branch `if/elif` de despacho."""
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    prefijos = set()
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Attribute)
            and nodo.func.attr == "startswith"
            and nodo.args
            and isinstance(nodo.args[0], ast.Constant)
            and isinstance(nodo.args[0].value, str)
        ):
            prefijos.add(nodo.args[0].value)
    return prefijos


def _llamadas_y_definiciones_sidebar() -> tuple[set, set]:
    """Nombres llamados dentro de `render_sidebar` y funciones definidas."""
    fuente = (RAIZ / "web" / "sidebar.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    llamadas: set = set()
    definidas: set = set()
    for nodo in arbol.body:
        if isinstance(nodo, ast.FunctionDef):
            definidas.add(nodo.name)
            if nodo.name == "render_sidebar":
                for sub in ast.walk(nodo):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        llamadas.add(sub.func.id)
    return llamadas, definidas


def _importar_modulo_en_subproceso(modulo: str, timeout: int = 30) -> tuple[bool, str]:
    """Importa `modulo` en un subproceso; False si tarda mas que `timeout`.

    Detecta modulos que arrancan un servidor al importarse: antes
    `import iniciar_dashboard` lanzaba Streamlit y no terminaba nunca."""
    codigo = (
        "import sys; sys.path.insert(0, r'{raiz}'); "
        "import {modulo}; print('IMPORT_OK')"
    ).format(raiz=str(RAIZ), modulo=modulo)
    try:
        proceso = subprocess.run(
            [sys.executable, "-c", codigo],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(RAIZ),
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout de {timeout}s (¿arranco un servidor?)"
    salida = (proceso.stdout or "") + (proceso.stderr or "")
    return proceso.returncode == 0 and "IMPORT_OK" in salida, salida[-200:].strip()


def _tiene_guard_main(ruta: Path) -> tuple[bool, bool]:
    """(tiene `if __name__ == "__main__"`, tiene `def main`) via AST."""
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    tiene_guard = False
    tiene_main = False
    for nodo in arbol.body:
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "main":
            tiene_main = True
        if isinstance(nodo, ast.If) and isinstance(nodo.test, ast.Compare):
            izquierda = nodo.test.left
            if (
                isinstance(izquierda, ast.Name)
                and izquierda.id == "__name__"
                and any(
                    isinstance(c, ast.Constant) and c.value == "__main__"
                    for c in nodo.test.comparators
                )
            ):
                tiene_guard = True
    return tiene_guard, tiene_main


def _change_no_borra_input() -> tuple[bool, bool, bool]:
    """(no borra builtins.input, restaura `_INPUT_ORIGINAL`, lo define) via AST."""
    fuente = (RAIZ / "web" / "operaciones" / "change.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    borra = False
    restaura = False
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Delete):
            for objetivo in nodo.targets:
                if (
                    isinstance(objetivo, ast.Attribute)
                    and objetivo.attr == "input"
                    and isinstance(objetivo.value, ast.Name)
                    and objetivo.value.id == "builtins"
                ):
                    borra = True
        if isinstance(nodo, ast.Assign) and len(nodo.targets) == 1:
            objetivo = nodo.targets[0]
            if (
                isinstance(objetivo, ast.Attribute)
                and objetivo.attr == "input"
                and isinstance(objetivo.value, ast.Name)
                and objetivo.value.id == "builtins"
                and isinstance(nodo.value, ast.Name)
                and nodo.value.id == "_INPUT_ORIGINAL"
            ):
                restaura = True
    define_original = (
        '_INPUT_ORIGINAL = getattr(builtins, "input", None)' in fuente
    )
    # Chequeo literal pedido: el archivo NO debe contener `del builtins.input`.
    sin_del = "del builtins.input" not in fuente
    return (not borra) and sin_del, restaura, define_original


def run(check):
    from web.operaciones.activacion_masiva import _navegadores_default
    from web.operaciones.cuentas import MODOS_TABS, TABS, _modo_de_tab

    constantes = _leer_constantes_app()
    opciones = constantes.get("OPCIONES") or []
    admin = constantes.get("OPCIONES_ADMIN") or []
    crear = constantes.get("OPCIONES_CREAR") or []
    categorias = constantes.get("CATEGORIAS") or []
    default = constantes.get("OPERACION_POR_DEFECTO") or ""

    conocidas = set(opciones) | set(admin) | set(crear)
    en_categorias = [op for _, ops in categorias for op in ops]
    prefijos = _prefijos_despacho_app()

    check(
        "navegacion: las 4 categorias cubren todas las operaciones visibles",
        set(en_categorias) == conocidas and len(categorias) == 4,
        f"({len(en_categorias)} ops en {len(categorias)} categorias)",
    )
    check(
        "navegacion: ninguna operacion repetida entre categorias",
        len(en_categorias) == len(set(en_categorias)),
    )
    check(
        "navegacion: la operacion por defecto (Alertas) existe",
        default in opciones and default == "🚨 Alertas",
    )
    check(
        "navegacion: Cuentas y Admin quedan en la categoria de datos",
        any(
            "🗂️ Cuentas: Perfiles, Secciones & Nombres" in ops
            and "👑 Admin: Usuarios" in ops
            for _, ops in categorias
        ),
    )

    check(
        "navegacion: quedan 15 operaciones visibles (18 - 3 ocultas)",
        len(opciones) == 15,
        f"(real={len(opciones)})",
    )
    check(
        "navegacion: las 3 operaciones ocultas NO estan en el menu",
        not (set(OPS_OCULTAS) & conocidas)
        and not (set(OPS_OCULTAS) & set(en_categorias)),
    )
    check(
        "navegacion: cada operacion visible conserva su branch de despacho",
        all(any(op.startswith(pref) for pref in prefijos) for op in opciones),
    )
    check(
        "navegacion: las operaciones ocultas siguen despachables (1 linea)",
        all(any(op.startswith(pref) for pref in prefijos) for op in OPS_OCULTAS),
    )

    tabs_modos = [tab for modos in MODOS_TABS.values() for tab in modos]
    check(
        "cuentas: los 3 modos cubren las 15 pestanas sin perder ninguna",
        len(MODOS_TABS) == 3 and sorted(tabs_modos) == sorted(TABS),
        f"({len(MODOS_TABS)} modos, {len(tabs_modos)} pestanas)",
    )
    check(
        "cuentas: ninguna pestana repetida entre modos",
        len(tabs_modos) == len(set(tabs_modos)),
    )
    check(
        "cuentas: una pestana externa (Renombrar usuario) resuelve su modo",
        _modo_de_tab("🏷️ Renombrar usuario") == "🛡️ Avanzado"
        and _modo_de_tab("🎨 Perfiles") == "🎭 Identidad"
        and _modo_de_tab("pestana-inexistente") in MODOS_TABS,
    )

    llamadas_sidebar, definidas_sidebar = _llamadas_y_definiciones_sidebar()
    check(
        "sidebar: Generar Imagen (multimedia) esta oculto pero su funcion vive",
        "_seccion_multimedia" not in llamadas_sidebar
        and "_seccion_multimedia" in definidas_sidebar,
    )
    check(
        "activacion: navegadores por defecto = 2 (recomendado)",
        _navegadores_default() == 2,
        f"(real={_navegadores_default()!r})",
    )

    guard, tiene_main = _tiene_guard_main(RAIZ / "iniciar_dashboard.py")
    check(
        "lanzador: iniciar_dashboard tiene def main() y guard __main__",
        guard and tiene_main,
    )
    importable, salida = _importar_modulo_en_subproceso("iniciar_dashboard")
    check(
        "lanzador: import iniciar_dashboard termina sin arrancar Streamlit",
        importable,
        salida,
    )

    from web.operaciones import change as change_page

    check(
        "change: guarda el input() original del proceso",
        change_page._INPUT_ORIGINAL is builtins.input,
    )
    no_borra, restaura, define_original = _change_no_borra_input()
    check(
        "change: sin 'del builtins.input' y con restauracion del original",
        no_borra and restaura and define_original,
    )
