"""Tests rapidos de la navegacion agrupada del dashboard web.

Verifica (sin Chrome y sin Streamlit runtime) que:
  - La navegacion de `web/app.py` tiene 4 categorias que cubren TODAS las
    operaciones visibles, sin duplicados, y que la operacion por defecto
    (Alertas) sigue existiendo.
  - No quedan lineas ocultas ni branches de despacho huerfanos: cada modulo
    importado por el dispatch existe en disco y todo prefijo despachado tiene
    una operacion visible.
  - `web/operaciones/cuentas.py` agrupa sus 16 pestanas en 3 modos (sin
    perder ni repetir ninguna) y `TABS` sigue completo.
  - El bloque multimedia huerfano del sidebar se elimino: la operacion
    Multimedia y su boton generador viven en `web/operaciones/multimedia.py`.
  - El default recomendado de "Navegadores simultaneos" de Activacion Masiva
    es 2.
  - `iniciar_dashboard` se importa sin arrancar Streamlit (def main + guard) y
    `web/operaciones/change.py` NO borra `builtins.input` (lo restaura).
  - El reparto por porcentajes `_repartir_por_porcentajes` de Activacion
    Masiva (helper puro: resto mayor, roles con peso 0 nunca reciben cuentas)
    y que los widgets/botones viejos (rol aleatorio, preset trending, reparto
    en tercios) ya no existen en la fuente.
  - El marcador `data/.campana_activa` del guard de campana unica (se crea al
    adquirir, se borra al liberar incluso si la campana falla).
  - La pestana "Nombres" de Cuentas renderiza con `AppTest` y expone el flujo
    masivo con IA: tipos partido/mixto, contexto para la IA, "aplicar a
    TODAS", navegadores simultaneos y renombrar la clave interna.
  - La proteccion de identidad de la pestana "Nombres": checkbox
    `nom_proteger_brandeadas` (default True), el kwarg `proteger_brandeadas`
    que `_llamar_asignar_propuestas` pasa/omite segun la firma del backend
    (con fallback TypeError) y el banner persistente del paso 3️⃣ con las
    cuentas omitidas (`omitidas_protegidas`/`protegidas_usuarios`/
    `protegidas_detalle`, guardadas en `st.session_state["nom_protegidas"]`).
  - La pestana "Eliminar" de Cuentas (borrado definitivo por lista pegada) esta
    registrada en TABS/MODOS_TABS/render y renderiza con `AppTest` el
    text_area, el boton de busqueda, el multiselect autoseleccionado y el
    boton de borrado deshabilitado sin confirmacion. NUNCA se pulsa el boton
    de borrar (jamas se ejecuta una eliminacion real).

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

    Con ellos se comprueba que cada operacion visible conserva su branch
    `if/elif` de despacho y que ningun prefijo quedo sin operacion."""
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


def _modulos_operaciones_importados_app() -> set:
    """Modulos `web.operaciones.X` importados por el dispatch de app.py (AST).

    Sirve para verificar que ninguna rama de despacho apunta a un archivo que
    ya no existe en `web/operaciones/`."""
    fuente = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module:
            if nodo.module.startswith("web.operaciones."):
                modulos.add(nodo.module.rsplit(".", 1)[-1])
    return modulos


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


def _tipo_mapa_nombres() -> dict:
    """Extrae el `tipo_mapa` de `_tab_nombres` (cuentas.py) via AST."""
    fuente = (RAIZ / "web" / "operaciones" / "cuentas.py").read_text(encoding="utf-8")
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "_tab_nombres":
            for sub in ast.walk(nodo):
                if isinstance(sub, ast.Assign):
                    for objetivo in sub.targets:
                        if isinstance(objetivo, ast.Name) and objetivo.id == "tipo_mapa":
                            try:
                                return ast.literal_eval(sub.value)
                            except Exception:
                                return {}
    return {}


def _llamada_widget_por_key(fuente: str, key: str) -> dict:
    """Kwargs literales de la primera llamada `st.<widget>(..., key=<key>)`."""
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Call):
            continue
        coincide = False
        salida = {}
        for kw in nodo.keywords:
            if isinstance(kw.value, ast.Constant):
                salida[kw.arg] = kw.value.value
            if (
                kw.arg == "key"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == key
            ):
                coincide = True
        if coincide:
            return salida
    return {}


def _generar_pasa_proteger(fuente: str) -> bool:
    """True si alguna llamada a `_llamar_asignar_propuestas` recibe
    `proteger_brandeadas=bool(<variable>)` (retrocompatible si no)."""
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if not (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Name)
            and nodo.func.id == "_llamar_asignar_propuestas"
        ):
            continue
        for kw in nodo.keywords:
            if kw.arg != "proteger_brandeadas":
                continue
            valor = kw.value
            if (
                isinstance(valor, ast.Call)
                and isinstance(valor.func, ast.Name)
                and valor.func.id == "bool"
                and valor.args
                and isinstance(valor.args[0], ast.Name)
                and valor.args[0].id == "proteger_brandeadas"
            ):
                return True
    return False


def _banner_en_paso3(fuente: str) -> bool:
    """True si `_tab_nombres` llama `_banner_protegidas_pendientes()` despues
    del titulo del paso 3️⃣ (antes de la tabla editable)."""
    pasos = []
    for nodo in ast.walk(ast.parse(fuente)):
        if not (isinstance(nodo, ast.FunctionDef) and nodo.name == "_tab_nombres"):
            continue
        for stmt in nodo.body:
            if not (
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            ):
                continue
            llamada = stmt.value
            if isinstance(llamada.func, ast.Name) and (
                llamada.func.id == "_banner_protegidas_pendientes"
            ):
                pasos.append("banner")
            if (
                isinstance(llamada.func, ast.Attribute)
                and llamada.func.attr == "markdown"
                and llamada.args
                and isinstance(llamada.args[0], ast.Constant)
                and isinstance(llamada.args[0].value, str)
                and "Revisa y guarda las propuestas" in llamada.args[0].value
            ):
                pasos.append("paso3")
    return (
        "paso3" in pasos
        and "banner" in pasos
        and pasos.index("banner") > pasos.index("paso3")
    )


def _fallbacks_nombres() -> dict:
    """Fallbacks de la UI cuando el backend de identidades es viejo/falta.

    - `_llamar_asignar_propuestas` omite `contexto` con firma vieja, lo pasa
      con la nueva y reintenta sin el si el backend lanza `TypeError`.
    - `_aplicar_lote_con_progreso` devuelve None (bucle secuencial) si el
      backend no expone `aplicar_propuestas_en_lote` o su firma no acepta los
      kwargs nuevos."""
    from web.operaciones import cuentas
    import cuentas.generador_identidades as gid

    def _viejo(usuarios, tipo="auto", seccion="", dry_run=False):
        return {"firma": "vieja", "tipo": tipo, "contexto": None}

    def _nuevo(usuarios, tipo="auto", seccion="", dry_run=False, contexto=""):
        return {"firma": "nueva", "contexto": contexto}

    def _terco(usuarios, tipo="auto", seccion="", dry_run=False, contexto=""):
        if contexto:
            raise TypeError("contexto no soportado")
        return {"firma": "terca"}

    res_viejo = cuentas._llamar_asignar_propuestas(_viejo, ["u"], "partido", "CI", "ctx")
    res_nuevo = cuentas._llamar_asignar_propuestas(_nuevo, ["u"], "mixto", "", "ctx")
    res_terco = cuentas._llamar_asignar_propuestas(_terco, ["u"], "auto", "", "ctx")

    original = getattr(gid, "aplicar_propuestas_en_lote", None)
    try:
        if original is not None:
            delattr(gid, "aplicar_propuestas_en_lote")
        sin_backend = cuentas._aplicar_lote_con_progreso(["u"], 2, False)

        def _firma_vieja(usuarios, password="", callback=None):
            return {}

        gid.aplicar_propuestas_en_lote = _firma_vieja
        firma_vieja = cuentas._aplicar_lote_con_progreso(["u"], 2, False)
    finally:
        if original is None:
            if hasattr(gid, "aplicar_propuestas_en_lote"):
                delattr(gid, "aplicar_propuestas_en_lote")
        else:
            gid.aplicar_propuestas_en_lote = original

    return {
        "viejo_ok": res_viejo.get("firma") == "vieja"
        and res_viejo.get("tipo") == "partido",
        "nuevo_ok": res_nuevo.get("contexto") == "ctx",
        "terco_ok": res_terco.get("firma") == "terca",
        "sin_backend_none": sin_backend is None,
        "firma_vieja_none": firma_vieja is None,
    }


def _fallback_proteger_brandeadas() -> dict:
    """`_llamar_asignar_propuestas` pasa/omite `proteger_brandeadas` por firma.

    Invocacion directa con fakes (sin Streamlit): firma nueva con el kwarg al
    final, `None` = no enviar, firma vieja sin el parametro, backend con
    `**kwargs` y backend que acepta el parametro pero lanza `TypeError` (la UI
    reintenta sin el y no se rompe)."""
    from web.operaciones import cuentas

    llamadas: list = []
    sentinela = "AUSENTE"

    def _nuevo(
        usuarios,
        tipo="auto",
        seccion="",
        dry_run=False,
        contexto="",
        proteger_brandeadas=sentinela,
    ):
        llamadas.append(("nuevo", proteger_brandeadas))
        return {"firma": "nueva"}

    def _viejo(usuarios, tipo="auto", seccion="", dry_run=False, contexto=""):
        llamadas.append(("viejo", sentinela))
        return {"firma": "vieja"}

    def _terco(
        usuarios,
        tipo="auto",
        seccion="",
        dry_run=False,
        contexto="",
        proteger_brandeadas=sentinela,
    ):
        llamadas.append(("terco", proteger_brandeadas))
        if proteger_brandeadas is not sentinela and proteger_brandeadas:
            raise TypeError("proteger_brandeadas no soportado")
        if contexto:
            raise TypeError("contexto no soportado")
        return {"firma": "terca"}

    def _var_kwargs(usuarios, **kwargs):
        llamadas.append(("kwargs", kwargs.get("proteger_brandeadas", sentinela)))
        return {"firma": "kwargs"}

    def _valores(nombre):
        return [valor for etiqueta, valor in llamadas if etiqueta == nombre]

    res_nuevo = cuentas._llamar_asignar_propuestas(
        _nuevo, ["u"], "auto", "", "ctx", proteger_brandeadas=True
    )
    res_none = cuentas._llamar_asignar_propuestas(
        _nuevo, ["u"], "auto", "", "ctx", proteger_brandeadas=None
    )
    res_viejo = cuentas._llamar_asignar_propuestas(
        _viejo, ["u"], "auto", "", "ctx", proteger_brandeadas=True
    )
    res_terco = cuentas._llamar_asignar_propuestas(
        _terco, ["u"], "auto", "", "ctx", proteger_brandeadas=True
    )
    res_var = cuentas._llamar_asignar_propuestas(
        _var_kwargs, ["u"], "auto", "", "", proteger_brandeadas=False
    )

    return {
        "nuevo_true": res_nuevo.get("firma") == "nueva"
        and _valores("nuevo")[:1] == [True],
        "none_no_enviado": res_none.get("firma") == "nueva"
        and _valores("nuevo") == [True, sentinela],
        "viejo_ok": res_viejo.get("firma") == "vieja"
        and _valores("viejo") == [sentinela],
        "terco_fallback": res_terco.get("firma") == "terca"
        and _valores("terco")
        and _valores("terco")[0] is True
        and _valores("terco")[-1] is sentinela,
        "var_kwargs_false": res_var.get("firma") == "kwargs"
        and _valores("kwargs") == [False],
        "soporta_nuevo": cuentas._soporta_kwargs(
            _nuevo, ("contexto", "proteger_brandeadas")
        ),
        "soporta_viejo_false": not cuentas._soporta_kwargs(
            _viejo, ("proteger_brandeadas",)
        ),
        "soporta_var_kwargs": cuentas._soporta_kwargs(
            _var_kwargs, ("proteger_brandeadas",)
        ),
    }


def _app_nombres():
    """Script de AppTest: pestana Nombres de Cuentas con cuentas simuladas.

    Deja `nom_protegidas` en session_state para simular la recarga tras una
    generacion con cuentas omitidas por proteccion (banner persistente).
    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos lo rompen en silencio."""
    import streamlit as st

    from web.operaciones import cuentas

    st.session_state["nom_protegidas"] = {
        "count": 3,
        "usuarios": ["cuenta_tres", "cuenta_cuatro", "cuenta_cinco"],
        "detalle": [
            {
                "usuario": "cuenta_tres",
                "campo": "handle_actual",
                "valor": "ana_lopez",
            }
        ],
    }

    class _ListarFake:
        def __call__(self, *args, **kwargs):
            return [
                {
                    "usuario": "cuenta_uno",
                    "email": "",
                    "status": "active",
                    "last_checked": "",
                    "cookies": "si",
                    "seccion": "",
                    "seccion_etiqueta": "Sin asignar",
                    "tipo_cuenta": "ciudadana",
                    "tipo_etiqueta": "Ciudadana",
                    "handle_actual": "cuenta_uno",
                    "nombre_mostrado": "",
                    "nombre_propuesto": "Naranja Uno",
                    "handle_propuesto": "naranja_uno",
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
                },
                {
                    "usuario": "cuenta_dos",
                    "email": "",
                    "status": "active",
                    "last_checked": "",
                    "cookies": "si",
                    "seccion": "",
                    "seccion_etiqueta": "Sin asignar",
                    "tipo_cuenta": "politica",
                    "tipo_etiqueta": "Politica",
                    "handle_actual": "cuenta_dos",
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
                },
            ]

        def clear(self):
            pass

    original = cuentas._listar_cuentas
    cuentas._listar_cuentas = _ListarFake()
    try:
        cuentas._tab_nombres()
    finally:
        # Sin esto, el fake quedaria activo para el resto de la suite.
        cuentas._listar_cuentas = original


def _app_test_nombres() -> tuple[bool, str, dict]:
    """Corre la pestana Nombres con `AppTest` y reporta los widgets nuevos."""
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "tipo_partido": False,
        "tipo_mixto": False,
        "contexto": False,
        "todas": False,
        "workers": False,
        "renombrar": False,
        "proteger_brandeadas": False,
        "proteger_default": False,
        "banner_protegidas": False,
        "banner_usuarios": False,
    }
    try:
        at = AppTest.from_function(_app_nombres, default_timeout=60)
        at.run()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", resultado

    if at.exception:
        return False, str(at.exception[0].value)[:200], resultado
    resultado["sin_excepciones"] = True

    claves_checkbox = {c.key for c in at.checkbox}
    resultado["proteger_brandeadas"] = "nom_proteger_brandeadas" in claves_checkbox
    if resultado["proteger_brandeadas"]:
        resultado["proteger_default"] = bool(
            at.checkbox(key="nom_proteger_brandeadas").value
        )
    resultado["banner_protegidas"] = any(
        "Protecci" in str(w.value) for w in at.warning
    )
    resultado["banner_usuarios"] = any(
        "cuenta_tres" in str(m.value) for m in at.markdown
    )

    try:
        opciones = list(at.selectbox(key="nom_tipo_identidad").options)
    except KeyError:
        opciones = []
    resultado["tipo_partido"] = any("Similitudes de partido" in op for op in opciones)
    resultado["tipo_mixto"] = any("Mixto" in op for op in opciones)
    resultado["contexto"] = any(t.key == "nom_tipo_contexto" for t in at.text_area)
    resultado["todas"] = any(c.key == "nom_lote_todas" for c in at.checkbox)
    resultado["workers"] = any(n.key == "nom_lote_workers" for n in at.number_input)
    resultado["renombrar"] = any(c.key == "nom_lote_renombrar" for c in at.checkbox)
    return True, "", resultado


def _app_nombres_generar(modo="nuevo"):
    """Script de AppTest: pulsa "Generar propuestas" con un backend fake.

    `modo="nuevo"` simula el backend con `proteger_brandeadas` al final
    (captura el valor recibido) y devuelve `omitidas_protegidas=3` con sus
    listas; `modo="viejo"` simula la firma vieja (sin el kwarg). Las capturas
    quedan en `gid._captura_nombres`.
    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos lo rompen en silencio."""
    import cuentas.generador_identidades as gid
    from web.operaciones import cuentas

    class _ListarFake:
        def __call__(self, *args, **kwargs):
            return [
                {
                    "usuario": "cuenta_uno",
                    "email": "",
                    "status": "active",
                    "last_checked": "",
                    "cookies": "si",
                    "seccion": "",
                    "seccion_etiqueta": "Sin asignar",
                    "tipo_cuenta": "ciudadana",
                    "tipo_etiqueta": "Ciudadana",
                    "handle_actual": "cuenta_uno",
                    "nombre_mostrado": "",
                    "nombre_propuesto": "Naranja Uno",
                    "handle_propuesto": "naranja_uno",
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
                }
            ]

        def clear(self):
            pass

    original_listar = cuentas._listar_cuentas
    original_asignar = gid.asignar_propuestas
    if not hasattr(gid, "_captura_nombres"):
        gid._captura_nombres = []

    if modo == "viejo":

        def _asignar(usuarios, tipo="auto", seccion="", dry_run=False, contexto=""):
            gid._captura_nombres.append("firma-vieja")
            return {
                "total": len(usuarios),
                "ok": len(usuarios),
                "errores": [],
                "origen_ia": False,
            }

    else:

        def _asignar(
            usuarios,
            tipo="auto",
            seccion="",
            dry_run=False,
            contexto="",
            proteger_brandeadas="AUSENTE",
        ):
            gid._captura_nombres.append(proteger_brandeadas)
            return {
                "total": len(usuarios),
                "ok": len(usuarios),
                "errores": [],
                "origen_ia": False,
                "omitidas_protegidas": 3,
                "protegidas_usuarios": [
                    "cuenta_tres",
                    "cuenta_cuatro",
                    "cuenta_cinco",
                ],
                "protegidas_detalle": [
                    {
                        "usuario": "cuenta_tres",
                        "campo": "handle_actual",
                        "valor": "ana_lopez",
                    }
                ],
            }

    cuentas._listar_cuentas = _ListarFake()
    gid.asignar_propuestas = _asignar
    try:
        cuentas._tab_nombres()
    finally:
        cuentas._listar_cuentas = original_listar
        gid.asignar_propuestas = original_asignar


def _app_test_nombres_generar(modo="nuevo") -> tuple[bool, str, dict]:
    """Genera con AppTest y verifica kwarg + banner persistente tras el rerun."""
    from streamlit.testing.v1 import AppTest

    import cuentas.generador_identidades as gid

    if hasattr(gid, "_captura_nombres"):
        delattr(gid, "_captura_nombres")
    resultado = {
        "sin_excepciones": False,
        "checkbox_presente": False,
        "checkbox_default": False,
        "llamada": False,
        "kwarg_true": False,
        "session_state": False,
        "banner": False,
        "banner_usuarios": False,
    }

    def _capturas() -> list:
        capturas = list(getattr(gid, "_captura_nombres", []) or [])
        if hasattr(gid, "_captura_nombres"):
            delattr(gid, "_captura_nombres")
        return capturas

    try:
        at = AppTest.from_function(
            _app_nombres_generar, default_timeout=90, kwargs={"modo": modo}
        )
        at.run()
        if at.exception:
            capturas = _capturas()
            resultado["llamada"] = bool(capturas)
            return False, str(at.exception[0].value)[:200], resultado

        claves = {c.key for c in at.checkbox}
        resultado["checkbox_presente"] = "nom_proteger_brandeadas" in claves
        if resultado["checkbox_presente"]:
            resultado["checkbox_default"] = bool(
                at.checkbox(key="nom_proteger_brandeadas").value
            )

        at.button(key="btn_nom_generar").click().run()
    except Exception as e:
        _capturas()
        return False, f"{type(e).__name__}: {e}", resultado

    capturas = _capturas()
    if at.exception:
        return False, str(at.exception[0].value)[:200], resultado
    resultado["sin_excepciones"] = True
    resultado["llamada"] = bool(capturas)
    if modo != "viejo":
        resultado["kwarg_true"] = True in capturas
        try:
            resumen = at.session_state["nom_protegidas"]
        except Exception:
            resumen = None
        resultado["session_state"] = (
            isinstance(resumen, dict) and resumen.get("count") == 3
        )
        resultado["banner"] = any(
            "Protecci" in str(w.value) and "omitid" in str(w.value)
            for w in at.warning
        )
        resultado["banner_usuarios"] = any(
            "cuenta_tres" in str(m.value) and "ana_lopez" in str(m.value)
            for m in at.markdown
        )
    return True, "", resultado


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


def _fuente_activacion() -> str:
    """Fuente de `web/operaciones/activacion_masiva.py` (checks de widgets)."""
    return (RAIZ / "web" / "operaciones" / "activacion_masiva.py").read_text(
        encoding="utf-8"
    )


def _fuente_cuentas() -> str:
    """Fuente de `web/operaciones/cuentas.py` (checks de widgets/pestanas)."""
    return (RAIZ / "web" / "operaciones" / "cuentas.py").read_text(encoding="utf-8")


def _paginas_render_cuentas() -> dict:
    """Dict `paginas` de `render()` en `cuentas.py`: pestana -> nombre funcion."""
    fuente = _fuente_cuentas()
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == "render":
            for sub in ast.walk(nodo):
                if isinstance(sub, ast.Assign):
                    for objetivo in sub.targets:
                        if (
                            isinstance(objetivo, ast.Name)
                            and objetivo.id == "paginas"
                            and isinstance(sub.value, ast.Dict)
                        ):
                            mapa = {}
                            for clave, valor in zip(
                                sub.value.keys, sub.value.values
                            ):
                                try:
                                    nombre = ast.literal_eval(clave)
                                except Exception:
                                    continue
                                mapa[nombre] = (
                                    valor.id if isinstance(valor, ast.Name) else ""
                                )
                            return mapa
    return {}


def _app_eliminar():
    """Script de AppTest: ABRE la pestana Eliminar via `render()` con una
    busqueda simulada (nunca pulsa el boton de borrar).

    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos/emojis lo rompen en silencio;
    por eso los nombres de modo/pestana se derivan de `cuentas.MODOS_TABS`/
    `TABS` en runtime y nunca se escriben como literal."""
    import streamlit as st

    from web.operaciones import cuentas

    tab = [t for t in cuentas.TABS if "Eliminar" in t][0]
    st.session_state["cuentas_modo_selector"] = cuentas._modo_de_tab(tab)
    st.session_state["cuentas_pestana_selector"] = tab
    st.session_state["elim_resultado"] = {
        "encontradas": [
            {
                "usuario": "cuenta_uno",
                "activa": True,
                "status": "active",
                "nombre_mostrado": "Cuenta Uno",
                "handle_actual": "cuenta_uno",
            },
            {
                "usuario": "cuenta_dos",
                "activa": False,
                "status": "suspended",
                "nombre_mostrado": "",
                "handle_actual": "",
            },
        ],
        "no_encontradas": ["cuenta_fantasma"],
        "error": "",
    }
    cuentas.render({})


def _app_test_eliminar() -> tuple[bool, str, dict]:
    """Corre la pestana Eliminar con `AppTest` y reporta sus widgets.

    Solo lectura/render: marca la confirmacion para comprobar que el boton se
    habilita, pero JAMAS pulsa `btn_elim_ejecutar` (no se elimina nada)."""
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "text_area": False,
        "btn_buscar": False,
        "multiselect": False,
        "default_todas": False,
        "btn_eliminar": False,
        "deshabilitado": False,
        "opciones_default": False,
        "confirma_habilita": False,
    }
    try:
        at = AppTest.from_function(_app_eliminar, default_timeout=60)
        at.run()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", resultado

    if at.exception:
        return False, str(at.exception[0].value)[:200], resultado
    resultado["sin_excepciones"] = True

    resultado["text_area"] = any(t.key == "elim_texto" for t in at.text_area)
    resultado["btn_buscar"] = any(b.key == "btn_elim_buscar" for b in at.button)
    resultado["multiselect"] = any(
        m.key == "elim_seleccion" for m in at.multiselect
    )
    try:
        seleccion = list(at.multiselect(key="elim_seleccion").value)
    except KeyError:
        seleccion = []
    resultado["default_todas"] = seleccion == ["@cuenta_uno", "@cuenta_dos"]

    claves_botones = {b.key for b in at.button}
    resultado["btn_eliminar"] = "btn_elim_ejecutar" in claves_botones
    if resultado["btn_eliminar"]:
        resultado["deshabilitado"] = bool(
            at.button(key="btn_elim_ejecutar").disabled
        )

    claves_opciones = {"elim_respaldar", "elim_cookies", "elim_tareas"}
    valores = {c.key: c.value for c in at.checkbox}
    resultado["opciones_default"] = claves_opciones <= set(valores) and all(
        valores[clave] for clave in claves_opciones
    )

    # Con la confirmacion marcada el boton se habilita (sin pulsarlo).
    try:
        at2 = at.checkbox(key="elim_confirmar").check().run()
        resultado["confirma_habilita"] = not at2.button(
            key="btn_elim_ejecutar"
        ).disabled
    except Exception:
        resultado["confirma_habilita"] = False

    return True, "", resultado


def _app_por_roles():
    """Script de AppTest: pestana Por roles con cuentas simuladas.

    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos lo rompen en silencio."""
    from web.operaciones import activacion_masiva as am

    class _CuentasFake:
        def __call__(self, *args, **kwargs):
            roles = ("cita", "hashtags", "comentario", "rt")
            return [
                {
                    "usuario": f"cuenta_{i:03d}",
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": roles[i % len(roles)],
                }
                for i in range(12)
            ]

    original = am._cargar_cuentas_con_roles
    am._cargar_cuentas_con_roles = _CuentasFake()
    try:
        am._por_roles()
    finally:
        # Sin esto, el fake quedaria activo para el resto de la suite.
        am._cargar_cuentas_con_roles = original


def _app_test_por_roles() -> tuple[bool, str, dict]:
    """Corre la pestana Por roles con `AppTest` y reporta los widgets nuevos."""
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "pct_cita": False,
        "pct_hashtags": False,
        "pct_comentario": False,
        "pct_rt": False,
        "btn_apply": False,
        "sin_viejos": False,
    }
    try:
        at = AppTest.from_function(_app_por_roles, default_timeout=60)
        at.run()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", resultado

    if at.exception:
        return False, str(at.exception[0].value)[:200], resultado
    resultado["sin_excepciones"] = True

    claves_inputs = {n.key for n in at.number_input}
    resultado["pct_cita"] = "act_roles_pct_cita" in claves_inputs
    resultado["pct_hashtags"] = "act_roles_pct_hashtags" in claves_inputs
    resultado["pct_comentario"] = "act_roles_pct_comentario" in claves_inputs
    resultado["pct_rt"] = "act_roles_pct_rt" in claves_inputs
    resultado["btn_apply"] = any(
        b.key == "btn_act_roles_pct_apply" for b in at.button
    )
    claves_widgets = (
        claves_inputs
        | {b.key for b in at.button}
        | {c.key for c in at.checkbox}
        | {r.key for r in at.radio}
    )
    viejos = {
        "act_roles_aleatorio",
        "btn_act_roles_trending",
        "btn_preset_trending",
        "btn_act_roles_tercios",
    }
    resultado["sin_viejos"] = not (claves_widgets & viejos)
    return True, "", resultado


def run(check):
    from web.operaciones.activacion_masiva import (
        ORDEN_ROLES,
        _navegadores_default,
        _repartir_por_porcentajes,
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

    fuente_app = (RAIZ / "web" / "app.py").read_text(encoding="utf-8")
    check(
        "navegacion: quedan 16 operaciones visibles y sin lineas comentadas "
        "de ocultamiento",
        len(opciones) == 16
        and "OCULTO" not in fuente_app
        and not any(
            linea.strip().startswith('# "') for linea in fuente_app.splitlines()
        ),
        f"(real={len(opciones)})",
    )
    check(
        "navegacion: cada operacion visible conserva su branch de despacho",
        all(any(op.startswith(pref) for pref in prefijos) for op in opciones),
    )
    check(
        "navegacion: todo prefijo despachado tiene su operacion (sin ramas "
        "muertas)",
        all(any(op.startswith(pref) for op in conocidas) for pref in prefijos),
        f"({len(prefijos)} prefijos)",
    )
    modulos_ops = _modulos_operaciones_importados_app()
    faltantes_disco = sorted(
        modulo
        for modulo in modulos_ops
        if not (RAIZ / "web" / "operaciones" / f"{modulo}.py").exists()
    )
    check(
        "navegacion: cada modulo de operaciones importado por el dispatch "
        "existe en disco",
        bool(modulos_ops) and not faltantes_disco,
        str(faltantes_disco) or f"({len(modulos_ops)} modulos)",
    )

    tabs_modos = [tab for modos in MODOS_TABS.values() for tab in modos]
    check(
        "cuentas: los 3 modos cubren todas las pestanas sin perder ninguna",
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

    # ---------------- Pestana Eliminar: borrado definitivo por lista --------
    check(
        "cuentas eliminar: TABS la ubica despues de Estado",
        TABS.index("🗑️ Eliminar") == TABS.index("⏸️ Estado") + 1,
    )
    check(
        "cuentas eliminar: el modo Cuentas la ubica despues de Estado",
        MODOS_TABS["🗂️ Cuentas"].index("🗑️ Eliminar")
        == MODOS_TABS["🗂️ Cuentas"].index("⏸️ Estado") + 1,
    )
    paginas_render = _paginas_render_cuentas()
    check(
        "cuentas eliminar: render() la mapea a _tab_eliminar y cubre TABS",
        paginas_render.get("🗑️ Eliminar") == "_tab_eliminar"
        and set(paginas_render) == set(TABS),
        f"({len(paginas_render)} paginas)",
    )
    from web.operaciones import cuentas as cuentas_elim

    fuente_cuentas = _fuente_cuentas()
    check(
        "cuentas eliminar: usa cuentas/eliminador y el boton exige seleccion + "
        "confirmacion",
        callable(getattr(cuentas_elim, "_tab_eliminar", None))
        and "from cuentas.eliminador import buscar_cuentas, parsear_lista_usuarios"
        in fuente_cuentas
        and "from cuentas.eliminador import eliminar_usuarios" in fuente_cuentas
        and "disabled=not elegidas or not confirmar" in fuente_cuentas,
    )

    app_ok, app_detalle, app_widgets = _app_test_eliminar()
    check(
        "cuentas eliminar: la pestana Eliminar renderiza sin excepciones (AppTest)",
        app_ok,
        app_detalle,
    )
    check(
        "cuentas eliminar: text_area, busqueda, multiselect con todo "
        "autoseleccionado y boton de borrado deshabilitado (AppTest)",
        app_widgets["sin_excepciones"]
        and app_widgets["text_area"]
        and app_widgets["btn_buscar"]
        and app_widgets["multiselect"]
        and app_widgets["default_todas"]
        and app_widgets["btn_eliminar"]
        and app_widgets["deshabilitado"]
        and app_widgets["opciones_default"]
        and app_widgets["confirma_habilita"],
        str({k: v for k, v in app_widgets.items() if not v}) or "todos presentes",
    )

    llamadas_sidebar, definidas_sidebar = _llamadas_y_definiciones_sidebar()
    fuente_multimedia = (RAIZ / "web" / "operaciones" / "multimedia.py").read_text(
        encoding="utf-8"
    )
    check(
        "sidebar: sin bloque multimedia huerfano (ni _seccion_multimedia ni "
        "web_generar_imagen)",
        "_seccion_multimedia" not in llamadas_sidebar
        and "_seccion_multimedia" not in definidas_sidebar
        and "web_generar_imagen" not in fuente_multimedia,
    )
    check(
        "multimedia: el boton Generar Imagen y _texto_sobre_imagen siguen vivos",
        'st.button("🎨 Generar Imagen"' in fuente_multimedia
        and "def _texto_sobre_imagen(" in fuente_multimedia,
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

    # ---------------- Pestana Nombres: nombres masivos con IA ----------------
    mapa_tipos = _tipo_mapa_nombres()
    check(
        "cuentas nombres: tipo_mapa incluye partido y mixto",
        mapa_tipos.get("Similitudes de partido (naranja, bolillos, amarilloluz)")
        == "partido"
        and mapa_tipos.get(
            "Mixto: mitad personas + mitad similitudes de partido"
        )
        == "mixto",
        f"({len(mapa_tipos)} tipos)",
    )

    fallbacks = _fallbacks_nombres()
    check(
        "cuentas nombres: contexto se omite con firma vieja y se pasa con la nueva",
        fallbacks["viejo_ok"] and fallbacks["nuevo_ok"] and fallbacks["terco_ok"],
    )
    check(
        "cuentas nombres: sin aplicar_propuestas_en_lote cae al bucle secuencial",
        fallbacks["sin_backend_none"] and fallbacks["firma_vieja_none"],
    )

    proteccion = _fallback_proteger_brandeadas()
    check(
        "cuentas nombres: proteger_brandeadas se pasa con firma nueva (None "
        "no se envia)",
        proteccion["nuevo_true"]
        and proteccion["none_no_enviado"]
        and proteccion["soporta_nuevo"],
    )
    check(
        "cuentas nombres: firma vieja/terca no rompe (fallback TypeError)",
        proteccion["viejo_ok"]
        and proteccion["terco_fallback"]
        and proteccion["soporta_viejo_false"],
    )
    check(
        "cuentas nombres: backend con **kwargs recibe proteger_brandeadas",
        proteccion["var_kwargs_false"] and proteccion["soporta_var_kwargs"],
    )

    fuente_cuentas = _fuente_cuentas()
    checkbox_proteger = _llamada_widget_por_key(
        fuente_cuentas, "nom_proteger_brandeadas"
    )
    check(
        "cuentas nombres: checkbox de proteccion con key "
        "nom_proteger_brandeadas y default True",
        checkbox_proteger.get("value") is True,
        "" if checkbox_proteger.get("value") is True else str(checkbox_proteger),
    )
    check(
        "cuentas nombres: el help del checkbox explica handle_actual/"
        "nombre_mostrado/propuesta",
        isinstance(checkbox_proteger.get("help"), str)
        and "handle_actual" in checkbox_proteger["help"]
        and "nombre_mostrado" in checkbox_proteger["help"]
        and "propuesta" in checkbox_proteger["help"],
    )
    check(
        "cuentas nombres: la generacion pasa proteger_brandeadas=bool(...) al "
        "helper",
        _generar_pasa_proteger(fuente_cuentas),
    )
    check(
        "cuentas nombres: el resumen de omitidas vive en nom_protegidas "
        "(count/usuarios/detalle)",
        'st.session_state["nom_protegidas"]' in fuente_cuentas
        and '"count": omitidas' in fuente_cuentas
        and '"usuarios": list(dict.fromkeys(protegidas_usuarios))' in fuente_cuentas
        and '"detalle": protegidas_detalle' in fuente_cuentas,
    )
    check(
        "cuentas nombres: el paso 3 llama al banner persistente de protegidas",
        _banner_en_paso3(fuente_cuentas),
    )
    check(
        "cuentas nombres: textos de omitidas (aviso + banner + detalle "
        "campo/valor)",
        "omitida(s) para proteger su" in fuente_cuentas
        and "Protección de identidad:" in fuente_cuentas
        and "porque ya parecían brandeadas." in fuente_cuentas
        and "Cuentas omitidas para proteger su identidad" in fuente_cuentas
        and 'fila.get("campo")' in fuente_cuentas
        and 'fila.get("valor")' in fuente_cuentas,
    )

    app_ok, app_detalle, app_widgets = _app_test_nombres()
    check(
        "cuentas nombres: la pestana Nombres renderiza sin excepciones (AppTest)",
        app_ok,
        app_detalle,
    )
    check(
        "cuentas nombres: widgets del lote masivo presentes (tipo/contexto/"
        "todas/workers/renombrar)",
        app_widgets["sin_excepciones"]
        and app_widgets["tipo_partido"]
        and app_widgets["tipo_mixto"]
        and app_widgets["contexto"]
        and app_widgets["todas"]
        and app_widgets["workers"]
        and app_widgets["renombrar"],
        str({k: v for k, v in app_widgets.items() if not v}) or "todos presentes",
    )
    check(
        "cuentas nombres: checkbox de proteccion presente y default True "
        "(AppTest)",
        app_widgets["proteger_brandeadas"] and app_widgets["proteger_default"],
        str({k: v for k, v in app_widgets.items() if not v}) or "ok",
    )
    check(
        "cuentas nombres: banner persistente con usuarios/detalle en el paso 3 "
        "tras el rerun (AppTest)",
        app_widgets["banner_protegidas"] and app_widgets["banner_usuarios"],
        str({k: v for k, v in app_widgets.items() if not v}) or "ok",
    )

    gen_ok, gen_detalle, gen_widgets = _app_test_nombres_generar()
    check(
        "cuentas nombres: generar con backend nuevo pasa "
        "proteger_brandeadas=True (AppTest)",
        gen_ok
        and gen_widgets["sin_excepciones"]
        and gen_widgets["checkbox_presente"]
        and gen_widgets["checkbox_default"]
        and gen_widgets["llamada"]
        and gen_widgets["kwarg_true"],
        gen_detalle or str({k: v for k, v in gen_widgets.items() if not v}),
    )
    check(
        "cuentas nombres: con omitidas_protegidas=3 el banner y los usuarios "
        "omitidos se renderizan tras el rerun (AppTest)",
        gen_widgets["session_state"]
        and gen_widgets["banner"]
        and gen_widgets["banner_usuarios"],
        str({k: v for k, v in gen_widgets.items() if not v}) or "ok",
    )
    viejo_ok, viejo_detalle, viejo_widgets = _app_test_nombres_generar(
        modo="viejo"
    )
    check(
        "cuentas nombres: backend viejo sin proteger_brandeadas no rompe la "
        "generacion (AppTest)",
        viejo_ok
        and viejo_widgets["sin_excepciones"]
        and viejo_widgets["llamada"],
        viejo_detalle
        or ("" if viejo_ok else f"llamada={viejo_widgets['llamada']}"),
    )

    # ---------------- Reparto por porcentajes `_repartir_por_porcentajes` ----
    def _cuentas(n: int) -> list:
        return [f"cuenta_{i:03d}" for i in range(1, n + 1)]

    reparto_100 = _repartir_por_porcentajes(_cuentas(100))
    check(
        "reparto pct: default equitativo n=100 -> 25 por rol",
        {rol: len(v) for rol, v in reparto_100.items()}
        == {"cita": 25, "hashtags": 25, "comentario": 25, "rt": 25},
    )

    reparto_141 = _repartir_por_porcentajes(_cuentas(141))
    conteo_141 = {rol: len(v) for rol, v in reparto_141.items()}
    check(
        "reparto pct: default equitativo n=141 -> 36/35/35/35 (resto mayor)",
        conteo_141 == {"cita": 36, "hashtags": 35, "comentario": 35, "rt": 35},
        f"(real={conteo_141})",
    )

    pesos_custom = {"cita": 50, "hashtags": 30, "comentario": 10, "rt": 10}
    reparto_custom = _repartir_por_porcentajes(_cuentas(100), pesos_custom)
    check(
        "reparto pct: pesos 50/30/10/10 en n=100 -> exacto",
        {rol: len(v) for rol, v in reparto_custom.items()} == pesos_custom,
    )

    reparto_cero = _repartir_por_porcentajes(
        _cuentas(20), {"cita": 50, "hashtags": 50, "comentario": 0, "rt": 0}
    )
    check(
        "reparto pct: un rol con peso 0 nunca recibe cuentas",
        len(reparto_cero["comentario"]) == 0
        and len(reparto_cero["rt"]) == 0
        and sum(len(v) for v in reparto_cero.values()) == 20,
    )

    general_ok = True
    for n in range(1, 11):
        limpios = _cuentas(n)
        reparto = _repartir_por_porcentajes(limpios)
        # Suma exacta + particion + orden original preservado (bloques).
        concatenados = [u for rol in ORDEN_ROLES for u in reparto[rol]]
        if concatenados != limpios:
            general_ok = False
        if sum(len(v) for v in reparto.values()) != n:
            general_ok = False
        # Resto mayor: cada rol a menos de 1 de su cuota equitativa.
        for rol in ORDEN_ROLES:
            if abs(len(reparto[rol]) - n * 25 / 100.0) > 1:
                general_ok = False
    check(
        "reparto pct: n=1..10 suma exacta, sin perder cuentas y contiguo",
        general_ok,
    )

    reparto_limpio = _repartir_por_porcentajes(
        ["@Ana", "ana", "", None, "Beto"],
        {"cita": 50, "hashtags": 50, "comentario": 0, "rt": 0},
    )
    check(
        "reparto pct: limpia '@'/vacios/duplicados (2 unicas de 5 entradas)",
        sum(len(v) for v in reparto_limpio.values()) == 2
        and reparto_limpio["cita"] == ["Ana"]
        and reparto_limpio["hashtags"] == ["Beto"],
    )

    reparto_norm = _repartir_por_porcentajes(
        _cuentas(100), {"cita": 1, "hashtags": 1, "comentario": 1, "rt": 1}
    )
    check(
        "reparto pct: suma != 100 se normaliza (pesos 1/1/1/1 = equitativo)",
        {rol: len(v) for rol, v in reparto_norm.items()}
        == {"cita": 25, "hashtags": 25, "comentario": 25, "rt": 25},
    )

    # ---------------- Widgets viejos fuera y nuevos dentro ----------------
    fuente = _fuente_activacion()
    viejos = (
        "btn_act_roles_trending",
        "btn_preset_trending",
        "btn_act_roles_tercios",
        "act_roles_aleatorio",
        "PESOS_TRENDING",
        "_repartir_trending",
        "PRESET_TRENDING",
    )
    presentes_viejos = [nombre for nombre in viejos if nombre in fuente]
    check(
        "reparto pct: los widgets/constantes viejos ya no existen en la fuente",
        not presentes_viejos,
        str(presentes_viejos) or "ninguno",
    )
    nuevos = (
        "act_roles_pct_cita",
        "act_roles_pct_hashtags",
        "act_roles_pct_comentario",
        "act_roles_pct_rt",
        "btn_act_roles_pct_apply",
        "_repartir_por_porcentajes",
    )
    faltantes = [nombre for nombre in nuevos if nombre not in fuente]
    check(
        "reparto pct: los widgets nuevos existen en la fuente",
        not faltantes,
        str(faltantes) or "todos",
    )

    app_ok, app_detalle, app_widgets = _app_test_por_roles()
    check(
        "reparto pct: la pestana Por roles renderiza sin excepciones (AppTest)",
        app_ok,
        app_detalle,
    )
    check(
        "reparto pct: los 4 number inputs y el boton de aplicar estan presentes",
        app_widgets["sin_excepciones"]
        and app_widgets["pct_cita"]
        and app_widgets["pct_hashtags"]
        and app_widgets["pct_comentario"]
        and app_widgets["pct_rt"]
        and app_widgets["btn_apply"]
        and app_widgets["sin_viejos"],
        str({k: v for k, v in app_widgets.items() if not v}) or "todos presentes",
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
