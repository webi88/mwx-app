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
  - El preset "Trending 1 hora" y el reparto ponderado `_repartir_trending`
    (RT 45 / Citas 25 / Hashtags 20 / Comentarios 10) de Activacion Masiva.
  - El marcador `data/.campana_activa` del guard de campana unica (se crea al
    adquirir, se borra al liberar incluso si la campana falla).

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


def _marcador_campana_prueba() -> dict:
    """Prueba funcional del marcador `data/.campana_activa`.

    Crea el archivo via `_adquirir_campana`, valida contenido numerico y guard
    unico, simula el fallo de campana (try/finally con `_liberar_campana`) y
    comprueba el borrado. SIEMPRE limpia el archivo/evento al terminar."""
    from web.operaciones import activacion_masiva as am

    ruta = RAIZ / "data" / ".campana_activa"
    resultados = {
        "creado": False,
        "numerico": False,
        "unico": False,
        "borrado": False,
        "sin_archivo_ok": False,
    }
    try:
        try:
            ruta.unlink()
        except Exception:
            pass

        resultados["creado"] = am._adquirir_campana() and ruta.exists()
        if ruta.exists():
            try:
                resultados["numerico"] = (
                    float(ruta.read_text(encoding="utf-8").strip()) > 0
                )
            except Exception:
                resultados["numerico"] = False
        resultados["unico"] = am._adquirir_campana() is False

        # Simula una campana que lanza: el finally del runner la libera.
        try:
            raise RuntimeError("campana de prueba")
        except RuntimeError:
            pass  # el error de la campana no debe romper el test
        finally:
            am._liberar_campana()
        resultados["borrado"] = not ruta.exists()

        # Sin archivo tambien es tolerante.
        am._limpiar_campana_activa()
        resultados["sin_archivo_ok"] = not ruta.exists()
    finally:
        am._liberar_campana()
    return resultados


def _guard_centralizado_en_lanzar() -> tuple[bool, int, int, bool]:
    """(adquiere, finales con liberar, liberaciones, sin set/clear crudos).

    Revisa por AST `_lanzar_con_progreso`: debe adquirir con
    `_adquirir_campana`, liberar con `_liberar_campana` dentro de un `finally`
    y no tocar `_CAMPANA_ACTIVA` directamente (centralizacion)."""
    fuente = (RAIZ / "web" / "operaciones" / "activacion_masiva.py").read_text(
        encoding="utf-8"
    )
    arbol = ast.parse(fuente)
    funcion = None
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_lanzar_con_progreso":
            funcion = nodo
            break
    if funcion is None:
        return False, 0, 0, False

    llamadas = [
        n.func.id
        for n in ast.walk(funcion)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    adquiere = "_adquirir_campana" in llamadas
    liberaciones = llamadas.count("_liberar_campana")

    finales = 0
    for nodo in ast.walk(funcion):
        if isinstance(nodo, ast.Try) and nodo.finalbody:
            for sub in ast.walk(ast.Module(body=nodo.finalbody, type_ignores=[])):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_liberar_campana"
                ):
                    finales += 1

    crudo = any(
        isinstance(n, ast.Attribute)
        and n.attr in ("set", "clear")
        and isinstance(n.value, ast.Name)
        and n.value.id == "_CAMPANA_ACTIVA"
        for n in ast.walk(funcion)
    )
    return adquiere, finales, liberaciones, not crudo


def run(check):
    from web.operaciones.activacion_masiva import (
        PRESET_TRENDING,
        _navegadores_default,
        _repartir_trending,
    )
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

    # ---------------- Preset "Trending 1 hora" ----------------
    esperado_preset = {
        "act_roles_dur": 60,
        "act_roles_repetir": True,
        "act_roles_cooldown": 8,
        "act_roles_pausa_comentario": 30,
        "act_roles_pct_min": 40,
        "act_roles_pct_max": 90,
        "act_roles_nav": 2,
        "act_roles_workers": 12,
        "act_roles_aleatorio": True,
        "act_roles_seccion": "Todas",
    }
    check(
        "trending: PRESET_TRENDING fija duracion/rondas/cooldown/pausa/equipo",
        PRESET_TRENDING == esperado_preset,
        f"(real={PRESET_TRENDING!r})",
    )

    # ---------------- Reparto ponderado `_repartir_trending` ----------------
    def _cuentas(n: int) -> list:
        return [f"cuenta_{i:03d}" for i in range(1, n + 1)]

    esperados_exactos = {
        100: {"cita": 25, "hashtags": 20, "comentario": 10, "rt": 45},
        141: {"cita": 35, "hashtags": 28, "comentario": 14, "rt": 64},
    }
    exactos_ok = True
    for n, esperado in esperados_exactos.items():
        reparto = _repartir_trending(_cuentas(n))
        exactos_ok = exactos_ok and {
            rol: len(v) for rol, v in reparto.items()
        } == esperado
    check(
        "trending: reparto ponderado exacto en n=100 (25/20/10/45) y n=141",
        exactos_ok,
    )

    general_ok = True
    for n in range(1, 11):
        limpios = _cuentas(n)
        reparto = _repartir_trending(limpios)
        # Suma exacta + particion + orden original preservado (bloques).
        concatenados = [u for rol in ("cita", "hashtags", "comentario", "rt")
                        for u in reparto[rol]]
        if concatenados != limpios:
            general_ok = False
        # Resto mayor: cada rol a menos de 1 de su cuota.
        for rol, peso in (("cita", 25), ("hashtags", 20), ("comentario", 10), ("rt", 45)):
            if abs(len(reparto[rol]) - n * peso / 100.0) > 1:
                general_ok = False
        # Pocas cuentas: 0-1 por rol (nadie se pierde).
        if n <= 4 and any(len(v) > 1 for v in reparto.values()):
            general_ok = False
    check(
        "trending: n=1..10 suma exacta, sin perder cuentas y proporcional",
        general_ok,
    )

    reparto_1 = _repartir_trending(["solo_una"])
    check(
        "trending: con 1 cuenta va al RT (rol mas confiable)",
        reparto_1["rt"] == ["solo_una"]
        and sum(len(v) for v in reparto_1.values()) == 1,
    )

    reparto_limpio = _repartir_trending(["@Ana", "ana", "", None, "Beto"])
    check(
        "trending: limpia '@'/vacios/duplicados (2 unicas de 5 entradas)",
        sum(len(v) for v in reparto_limpio.values()) == 2
        and len(reparto_limpio["rt"]) == 1
        and len(reparto_limpio["cita"]) == 1,
    )

    # ---------------- Marcador de campana activa (data/.campana_activa) -------
    marcador = _marcador_campana_prueba()
    check(
        "campana: _adquirir/_marcar crea data/.campana_activa",
        marcador["creado"],
    )
    check(
        "campana: el marcador contiene un timestamp numerico",
        marcador["numerico"],
    )
    check(
        "campana: guard unico (2do acquire False) y _liberar borra el archivo",
        marcador["unico"] and marcador["borrado"],
    )
    check(
        "campana: _limpiar_campana_activa es tolerante si no existe",
        marcador["sin_archivo_ok"],
    )
    adquiere, finales, liberaciones, sin_raw = _guard_centralizado_en_lanzar()
    check(
        "campana: _lanzar_con_progreso centraliza adquirir/liberar en finally",
        adquiere and finales >= 1 and liberaciones >= 2 and sin_raw,
    )
