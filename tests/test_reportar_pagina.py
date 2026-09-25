"""Checks de la operacion "Reportar Posts o Cuentas" (`web/operaciones/reportar.py`).

Cubre:
  - `_reportar_objetivo_con_bot`: usa `reportar_objetivo(url, motivo)` cuando el
    bot lo expone (Twitter nuevo) y cae a `reportar_post(url, motivo)` para bots
    viejos y plataformas que solo tienen reportar_post (FB/IG/TikTok).
  - Selector masivo (`_aplicar_modo`/`_seleccionar_por_rango`): Todas, Primeras
    N, Por rango (inclusivo, case-insensitive) y Manual; `_dividir_en_bloques`
    reparte `cuentas[i::n]` sin solapes.
  - Ejecucion en segundo plano: hilos + Event de paro + `st.fragment(run_every)`;
    los hilos NO tocan `st.*` (AST) y `ejecutar_en_cuentas` se llama por cuenta
    sin widgets (`progreso`/`estado` = None) con `tipo="reportar"`.
  - Worker real con fakes: acumula contadores/feed y respeta el paro; el wrapper
    de accion reporta cada URL con su motivo.
  - Aviso de alcance, nota de lote 20-30, cabecera, motivos y persistencia del
    resultado final en `session_state` (`_normalizar_resultado`).
  - AppTest: pagina 0 excepciones, los 4 modos renderizan, aviso de alcance con
    seleccion simulada y panel persistido con `paradas`. NUNCA se pulsa
    `btn_reportar` (enviaria reportes reales a X).

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows).
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _fuente_reportar() -> str:
    return (RAIZ / "web" / "operaciones" / "reportar.py").read_text(encoding="utf-8")


def _sin_streamlit_en_funcion(fuente: str, nombre: str) -> bool:
    """True si la funcion (y sus anidadas) NO toca `st.*` (apta para hilos)."""
    arbol = ast.parse(fuente)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            for sub in ast.walk(nodo):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "st"
                ):
                    return False
            return True
    return False


def _app_reportar():
    """Script de AppTest: render de la pagina con cuentas simuladas."""
    from web.operaciones import reportar

    class _CuentaFake:
        def __init__(self, usuario, grupo="A"):
            self.usuario = usuario
            self.grupo = grupo

    class _CuentasFake:
        def __call__(self, plataforma, solo_activas=True):
            return [_CuentaFake("cuenta_uno"), _CuentaFake("cuenta_dos")]

    original = reportar.cuentas_por_plataforma
    reportar.cuentas_por_plataforma = _CuentasFake()
    try:
        reportar.render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})
    finally:
        reportar.cuentas_por_plataforma = original


def _app_reportar_con_seleccion():
    """Script de AppTest: modo Manual con 1 cuenta y 2 URLs (aviso de alcance)."""
    import streamlit as st

    from web.operaciones import reportar

    class _CuentaFake:
        def __init__(self, usuario, grupo="A"):
            self.usuario = usuario
            self.grupo = grupo

    class _CuentasFake:
        def __call__(self, plataforma, solo_activas=True):
            return [_CuentaFake("cuenta_uno"), _CuentaFake("cuenta_dos")]

    original = reportar.cuentas_por_plataforma
    reportar.cuentas_por_plataforma = _CuentasFake()
    st.session_state["rep_modo_cuentas"] = reportar.MODO_MANUAL
    st.session_state["rep_cuentas"] = ["@cuenta_uno (Grupo A)"]
    st.session_state["rep_urls"] = (
        "https://x.com/usuario/status/123\nhttps://x.com/usuario"
    )
    try:
        reportar.render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})
    finally:
        reportar.cuentas_por_plataforma = original


def _app_reportar_modos():
    """Script de AppTest: selector masivo con 5 cuentas simuladas."""
    from web.operaciones import reportar

    class _CuentaFake:
        def __init__(self, usuario, grupo="A"):
            self.usuario = usuario
            self.grupo = grupo

    class _CuentasFake:
        def __call__(self, plataforma, solo_activas=True):
            return [_CuentaFake(f"cuenta_{i:02d}") for i in range(1, 6)]

    original = reportar.cuentas_por_plataforma
    reportar.cuentas_por_plataforma = _CuentasFake()
    try:
        reportar.render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})
    finally:
        reportar.cuentas_por_plataforma = original


def _app_reportar_persistido():
    """Script de AppTest: resultado persistido con cuentas paradas por el paro."""
    import streamlit as st

    from web.operaciones import reportar

    class _CuentaFake:
        def __init__(self, usuario, grupo="A"):
            self.usuario = usuario
            self.grupo = grupo

    class _CuentasFake:
        def __call__(self, plataforma, solo_activas=True):
            return [_CuentaFake("cuenta_uno"), _CuentaFake("cuenta_dos")]

    original = reportar.cuentas_por_plataforma
    reportar.cuentas_por_plataforma = _CuentasFake()
    if not st.session_state.get("sembrado"):
        st.session_state["sembrado"] = True
        st.session_state[reportar.CLAVE_RESULTADO_REPORTES] = {
            "exitos": 7,
            "fallidos": 2,
            "omitidas": 1,
            "detalles": ["OK @cuenta_uno", "ERROR @cuenta_dos: sin sesion"],
            "cuentas_totales": 12,
            "cuentas_hechas": 9,
            "paradas": 3,
            "urls": 2,
            "paralelo": 2,
            "fecha": "01/01/2026 10:00:00",
        }
    try:
        reportar.render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})
    finally:
        reportar.cuentas_por_plataforma = original


def run(check):
    from web.operaciones import reportar as pagina

    fuente = _fuente_reportar()

    # ------------------------------------------------------------------ #
    # Helper de ejecucion por bot (fallback reportar_objetivo/reportar_post)
    # ------------------------------------------------------------------ #
    class _BotObjetivo:
        def __init__(self):
            self.llamadas = []

        def reportar_objetivo(self, url, motivo):
            self.llamadas.append(("objetivo", url, motivo))
            return True

        def reportar_post(self, url, motivo):
            self.llamadas.append(("post", url, motivo))
            return True

    bot = _BotObjetivo()
    ok = pagina._reportar_objetivo_con_bot(
        bot, "https://x.com/usuario/status/123", "spam"
    )
    check(
        "reportar: usa reportar_objetivo(url, motivo) cuando el bot lo expone",
        ok is True
        and bot.llamadas == [("objetivo", "https://x.com/usuario/status/123", "spam")],
        str(bot.llamadas),
    )

    class _BotViejo:
        def __init__(self):
            self.llamadas = []

        def reportar_post(self, url, motivo):
            self.llamadas.append(("post", url, motivo))
            return True

    bot_viejo = _BotViejo()
    ok = pagina._reportar_objetivo_con_bot(
        bot_viejo, "https://www.instagram.com/p/abc", "hate"
    )
    check(
        "reportar: cae a reportar_post sin reportar_objetivo (FB/IG/TikTok)",
        ok is True
        and bot_viejo.llamadas
        == [("post", "https://www.instagram.com/p/abc", "hate")],
        str(bot_viejo.llamadas),
    )

    class _BotNone:
        reportar_objetivo = None

        def __init__(self):
            self.llamadas = []

        def reportar_post(self, url, motivo):
            self.llamadas.append(url)
            return True

    bot_none = _BotNone()
    check(
        "reportar: reportar_objetivo=None tambien cae al fallback",
        pagina._reportar_objetivo_con_bot(bot_none, "https://x.com/usuario", "spam")
        is True
        and bot_none.llamadas == ["https://x.com/usuario"],
        str(bot_none.llamadas),
    )

    class _BotFalla:
        def reportar_objetivo(self, url, motivo):
            return False

    check(
        "reportar: devuelve False si el bot falla (el flujo continua)",
        pagina._reportar_objetivo_con_bot(_BotFalla(), "https://x.com/usuario", "spam")
        is False,
    )

    # ------------------------------------------------------------------ #
    # Selector masivo (helpers puros)
    # ------------------------------------------------------------------ #
    class _C:
        def __init__(self, usuario, grupo="A"):
            self.usuario = usuario
            self.grupo = grupo

    cuentas = [_C("Zeta"), _C("alfa"), _C("Beta"), _C("gama")]

    todas = pagina._aplicar_modo(cuentas, pagina.MODO_TODAS)
    check(
        "reportar: modo 'Todas las activas' ordena alfabetico case-insensitive",
        [c.usuario for c in todas] == ["alfa", "Beta", "gama", "Zeta"],
        str([c.usuario for c in todas]),
    )

    primeras = pagina._aplicar_modo(cuentas, pagina.MODO_PRIMERAS, primeras_n=2)
    check(
        "reportar: modo 'Primeras N' toma las N primeras del orden",
        [c.usuario for c in primeras] == ["alfa", "Beta"],
        str([c.usuario for c in primeras]),
    )
    check(
        "reportar: 'Primeras N' con valor invalido devuelve vacio",
        pagina._aplicar_modo(cuentas, pagina.MODO_PRIMERAS, primeras_n="x") == [],
    )

    rango = pagina._aplicar_modo(
        cuentas, pagina.MODO_RANGO, desde="@beta", hasta="ZETA"
    )
    check(
        "reportar: rango por usuario inclusivo, case-insensitive y con '@'",
        [c.usuario for c in rango] == ["Beta", "gama", "Zeta"],
        str([c.usuario for c in rango]),
    )
    rango_inv = pagina._aplicar_modo(
        cuentas, pagina.MODO_RANGO, desde="zeta", hasta="alfa"
    )
    check(
        "reportar: rango invertido se intercambia (no queda vacio)",
        [c.usuario for c in rango_inv]
        == ["alfa", "Beta", "gama", "Zeta"],
        str([c.usuario for c in rango_inv]),
    )
    check(
        "reportar: rango sin extremos = todas las cuentas",
        len(pagina._aplicar_modo(cuentas, pagina.MODO_RANGO)) == 4,
    )

    manual = pagina._aplicar_modo(
        cuentas,
        pagina.MODO_MANUAL,
        manual=["@Beta (Grupo A)", "@no_existe"],
        cuenta_opts={"@Beta (Grupo A)": cuentas[2]},
    )
    check(
        "reportar: modo Manual mapea etiquetas e ignora desconocidas",
        [c.usuario for c in manual] == ["Beta"],
        str([c.usuario for c in manual]),
    )

    # ------------------------------------------------------------------ #
    # Bloques de concurrencia
    # ------------------------------------------------------------------ #
    bloques = pagina._dividir_en_bloques(list(range(7)), 3)
    aplanado = sorted(x for bloque in bloques for x in bloque)
    check(
        "reportar: bloques intercalados cuentas[i::n] sin solapes ni perdidas",
        [len(b) for b in bloques] == [3, 2, 2] and aplanado == list(range(7)),
        str(bloques),
    )
    check(
        "reportar: n fuera de rango se acota (0 -> 1 bloque, n>len -> 1 c/u)",
        pagina._dividir_en_bloques([1, 2], 0) == [[1, 2]]
        and pagina._dividir_en_bloques([1, 2], 9) == [[1], [2]],
    )

    # ------------------------------------------------------------------ #
    # Fuente: modos, hilos, paro, aviso y wiring
    # ------------------------------------------------------------------ #
    check(
        "reportar: la fuente usa getattr(reportar_objetivo) con fallback a "
        "reportar_post",
        'getattr(bot, "reportar_objetivo", None)' in fuente
        and "bot.reportar_post(url, motivo)" in fuente,
    )
    check(
        "reportar: selector masivo con 4 modos y key rep_modo_cuentas",
        'key="rep_modo_cuentas"' in fuente
        and "MODO_TODAS = " in fuente
        and "MODO_PRIMERAS = " in fuente
        and "MODO_RANGO = " in fuente
        and "MODO_MANUAL = " in fuente,
    )
    check(
        "reportar: resumen siempre visible (Seleccionadas/URLs/Total de reportes)",
        "Seleccionadas: **" in fuente and "Total de reportes:" in fuente,
    )
    check(
        "reportar: hilo + Event de paro + fragment(run_every=1s) para el progreso",
        "threading.Thread(" in fuente
        and "threading.Event()" in fuente
        and "run_every=1.0" in fuente
        and 'getattr(st, "fragment", None)' in fuente,
    )
    check(
        "reportar: controles de paro/paralelo y pausa aleatoria en el wrapper",
        'key="btn_rep_paro"' in fuente
        and 'key="rep_paralelo"' in fuente
        and "time.sleep(random.uniform(1, 3))" in fuente,
    )
    check(
        "reportar: los hilos no tocan Streamlit (worker/runner/accion/acumular)",
        all(
            _sin_streamlit_en_funcion(fuente, nombre)
            for nombre in (
                "_worker",
                "_runner",
                "_accion_para",
                "_acumular",
                "_dividir_en_bloques",
                "_snapshot",
            )
        ),
    )
    check(
        "reportar: cabecera nueva 'REPORTAR POSTS O CUENTAS' + subtitulo",
        "REPORTAR POSTS O CUENTAS" in fuente
        and "Reporta publicaciones o perfiles en X con tus cuentas" in fuente,
    )
    check(
        "reportar: campo etiquetado 'URLs (una por línea)' y ayuda de formatos",
        "URLs (una por línea)" in fuente
        and "x.com/usuario/status" in fuente
        and "(https://x.com/usuario)" in fuente,
    )
    check(
        "reportar: aviso de alcance antes de ejecutar (cuentas x URLs)",
        "st.info(" in fuente
        and "Se harán " in fuente
        and "reportes " in fuente
        and "cuentas × " in fuente
        and "URLs)." in fuente,
    )
    check(
        "reportar: motivos alineados al bot + impersonation/self_harm/false_info",
        all(
            clave in pagina.MOTIVOS
            for clave in (
                "spam",
                "hate",
                "abuse",
                "violence",
                "sensitive",
                "false_info",
                "impersonation",
                "self_harm",
            )
        )
        and "Suplantación" in pagina.MOTIVOS["impersonation"]
        and "Autolesión" in pagina.MOTIVOS["self_harm"],
        str(list(pagina.MOTIVOS)),
    )
    check(
        "reportar: conserva ejecutar_en_cuentas(tipo='reportar') sin incluir_pausadas",
        'tipo="reportar"' in fuente and "incluir_pausadas" not in fuente,
    )
    check(
        "reportar: nota de lote 20-30 cuentas y aviso de sesion vencida",
        "lote de 20-30 cuentas" in fuente and "sesión vencida" in fuente,
    )

    # ------------------------------------------------------------------ #
    # Worker real con fakes: acumula resultados y respeta el paro
    # ------------------------------------------------------------------ #
    llamadas = []

    def _fake_ejecutar(cuentas_bloque, accion, plataforma, progreso=None,
                       estado=None, tipo="post", incluir_pausadas=False):
        llamadas.append({
            "n": len(cuentas_bloque),
            "progreso": progreso,
            "estado": estado,
            "tipo": tipo,
        })
        usuario = getattr(cuentas_bloque[0], "usuario", "?")
        return {
            "exitos": 1,
            "fallidos": 0,
            "omitidas": 0,
            "detalles": [f"✅ @{usuario}"],
        }

    original_ejecutar = pagina.ejecutar_en_cuentas
    pagina.ejecutar_en_cuentas = _fake_ejecutar

    def _reset(run_id, total, paro):
        with pagina._LOCK:
            pagina._EJECUCION.update({
                "activa": True,
                "run_id": run_id,
                "paro": paro,
                "total": total,
                "hechas": 0,
                "exitos": 0,
                "fallidos": 0,
                "omitidas": 0,
                "ultima": "",
                "urls": 1,
                "paralelo": 1,
                "iniciada": "",
                "finalizada": "",
                "eventos": [],
                "detalles": [],
                "resultado_final": None,
            })

    try:
        paro = threading.Event()
        _reset(101, 3, paro)
        pagina._worker(101, cuentas[:3], ["https://x.com/u"], "spam", "twitter", paro)
        with pagina._LOCK:
            snap = dict(pagina._EJECUCION)
        check(
            "reportar: worker procesa su bloque por cuenta, sin widgets y con "
            "tipo='reportar'",
            snap["hechas"] == 3
            and snap["exitos"] == 3
            and len(llamadas) == 3
            and all(
                c["n"] == 1
                and c["progreso"] is None
                and c["estado"] is None
                and c["tipo"] == "reportar"
                for c in llamadas
            ),
            f"hechas={snap['hechas']} llamadas={llamadas[:1]}",
        )
        check(
            "reportar: feed con los ultimos resultados (usuario + estado)",
            len(snap["eventos"]) == 3
            and snap["eventos"][0]["usuario"] == "Beta"
            and snap["eventos"][0]["estado"] == "ok",
            str(snap["eventos"][:1]),
        )

        llamadas.clear()
        paro2 = threading.Event()
        paro2.set()
        _reset(102, 3, paro2)
        pagina._worker(
            102, cuentas[:3], ["https://x.com/u"], "spam", "twitter", paro2
        )
        with pagina._LOCK:
            snap2 = dict(pagina._EJECUCION)
        check(
            "reportar: con paro solicitado el worker NO ejecuta cuentas",
            snap2["hechas"] == 0 and len(llamadas) == 0,
            f"hechas={snap2['hechas']} llamadas={len(llamadas)}",
        )

        # Runner completo (hilos reales) con y sin paro.
        llamadas.clear()
        paro3 = threading.Event()
        _reset(103, 4, paro3)
        pagina._runner(103, cuentas, ["https://x.com/u"], "spam", "twitter", 2, paro3)
        with pagina._LOCK:
            final = dict(pagina._EJECUCION["resultado_final"] or {})
            activa = bool(pagina._EJECUCION["activa"])
        check(
            "reportar: el runner termina, apaga 'activa' y deja el resultado "
            "final con cuentas_totales/paradas",
            activa is False
            and final.get("cuentas_totales") == 4
            and final.get("cuentas_hechas") == 4
            and final.get("paradas") == 0
            and final.get("exitos") == 4
            and final.get("paralelo") == 2,
            str(final),
        )

        llamadas.clear()
        paro4 = threading.Event()
        paro4.set()
        _reset(104, 4, paro4)
        pagina._runner(104, cuentas, ["https://x.com/u"], "spam", "twitter", 2, paro4)
        with pagina._LOCK:
            final_stop = dict(pagina._EJECUCION["resultado_final"] or {})
        check(
            "reportar: runner con paro deja paradas=cuentas_totales (omitidas)",
            final_stop.get("paradas") == 4
            and final_stop.get("cuentas_hechas") == 0
            and final_stop.get("exitos") == 0,
            str(final_stop),
        )
    finally:
        pagina.ejecutar_en_cuentas = original_ejecutar
        with pagina._LOCK:
            pagina._EJECUCION.update({
                "activa": False,
                "run_id": 0,
                "paro": None,
                "total": 0,
                "hechas": 0,
                "exitos": 0,
                "fallidos": 0,
                "omitidas": 0,
                "ultima": "",
                "urls": 0,
                "paralelo": 1,
                "iniciada": "",
                "finalizada": "",
                "eventos": [],
                "detalles": [],
                "resultado_final": None,
            })

    # ------------------------------------------------------------------ #
    # Wrapper de accion (pausa inyectable) + normalizacion del resultado
    # ------------------------------------------------------------------ #
    class _CuentaX:
        usuario = "cuenta_x"

    class _BotUrls:
        def __init__(self):
            self.calls = []

        def reportar_objetivo(self, url, motivo):
            self.calls.append((url, motivo))
            return url.endswith("/ok")

    bot_urls = _BotUrls()
    accion = pagina._accion_para(
        _CuentaX(), ["https://x.com/a", "https://x.com/ok"], "spam", pausa_seg=0
    )
    ok = accion(bot_urls)
    check(
        "reportar: el wrapper de accion reporta cada URL con el motivo (bool)",
        ok is False
        and bot_urls.calls
        == [("https://x.com/a", "spam"), ("https://x.com/ok", "spam")],
        str(bot_urls.calls),
    )

    norm = pagina._normalizar_resultado({
        "exitos": "7",
        "fallidos": 2,
        "omitidas": None,
        "detalles": ["✅ @a"],
        "cuentas_totales": "10",
        "cuentas_hechas": 9,
        "paradas": "1",
        "urls": 3,
        "paralelo": 2,
        "fecha": "hoy",
    })
    check(
        "reportar: _normalizar_resultado tolera strings/None "
        "(cuentas_totales/paradas)",
        norm["exitos"] == 7
        and norm["fallidos"] == 2
        and norm["omitidas"] == 0
        and norm["cuentas_totales"] == 10
        and norm["paradas"] == 1
        and norm["detalles"] == ["✅ @a"]
        and norm["paralelo"] == 2,
        str(norm),
    )
    vacio = pagina._normalizar_resultado(None)
    check(
        "reportar: _normalizar_resultado(None) queda en ceros",
        vacio["exitos"] == 0
        and vacio["paradas"] == 0
        and vacio["cuentas_totales"] == 0
        and vacio["detalles"] == [],
    )

    # ------------------------------------------------------------------ #
    # AppTest: render completo de la pagina
    # ------------------------------------------------------------------ #
    from streamlit.testing.v1 import AppTest

    try:
        at = AppTest.from_function(_app_reportar, default_timeout=120)
        at.run()
        ok_render = not at.exception
        error_render = "" if ok_render else str(at.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_render, error_render = False, f"{type(e).__name__}: {e}"
    check(
        "reportar: la pagina renderiza sin excepciones (AppTest)",
        ok_render,
        error_render,
    )

    if ok_render:
        claves_select = {s.key for s in at.selectbox}
        claves_area = {t.key for t in at.text_area}
        claves_radio = {r.key for r in at.radio}
        claves_num = {n.key for n in at.number_input}
        claves_botones = {b.key for b in at.button}
        check(
            "reportar: controles presentes (plataforma, modo, motivo, URLs, "
            "paralelo, boton)",
            {"rep_plataforma", "rep_motivo"} <= claves_select
            and "rep_modo_cuentas" in claves_radio
            and "rep_urls" in claves_area
            and "rep_paralelo" in claves_num
            and "btn_reportar" in claves_botones,
            str(sorted(
                claves_select | claves_area | claves_radio | claves_num | claves_botones
            )),
        )
        textos = "\n".join(str(m.value) for m in at.markdown) + "\n".join(
            str(c.value) for c in at.caption
        )
        check(
            "reportar: cabecera nueva y resumen de seleccion visibles (AppTest)",
            "REPORTAR POSTS O CUENTAS" in textos
            and "Seleccionadas:" in textos
            and "Total de reportes:" in textos,
        )

    # ------------------------------------------------------------------ #
    # AppTest: los 4 modos de seleccion renderizan
    # ------------------------------------------------------------------ #
    try:
        at_modos = AppTest.from_function(_app_reportar_modos, default_timeout=120)
        at_modos.run()
        ok_modos = not at_modos.exception
        error_modos = "" if ok_modos else str(at_modos.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_modos, error_modos = False, f"{type(e).__name__}: {e}"
    check(
        "reportar: render del selector masivo sin excepciones (AppTest)",
        ok_modos,
        error_modos,
    )

    if ok_modos:
        opciones = list(at_modos.radio(key="rep_modo_cuentas").options)
        check(
            "reportar: el radio expone los 4 modos (Todas/Primeras/Rango/Manual)",
            len(opciones) == 4
            and opciones[0].startswith(pagina.MODO_TODAS)
            and pagina.MODO_PRIMERAS in opciones
            and pagina.MODO_RANGO in opciones
            and pagina.MODO_MANUAL in opciones,
            str(opciones),
        )
        fallos_modos = []
        at_actual = at_modos
        for opcion in opciones:
            at_actual = at_actual.radio(key="rep_modo_cuentas").set_value(opcion).run()
            if at_actual.exception:
                fallos_modos.append(
                    f"{opcion}: {str(at_actual.exception[0].value)[:120]}"
                )
        check(
            "reportar: los 4 modos renderizan sin excepciones (AppTest)",
            not fallos_modos,
            "; ".join(fallos_modos),
        )

    # ------------------------------------------------------------------ #
    # AppTest: aviso de alcance con seleccion simulada (sin pulsar el boton)
    # ------------------------------------------------------------------ #
    try:
        at2 = AppTest.from_function(
            _app_reportar_con_seleccion, default_timeout=120
        )
        at2.run()
        ok_sel = not at2.exception
        error_sel = "" if ok_sel else str(at2.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_sel, error_sel = False, f"{type(e).__name__}: {e}"
    check(
        "reportar: render con seleccion simulada sin excepciones (AppTest)",
        ok_sel,
        error_sel,
    )

    if ok_sel:
        infos = "\n".join(str(i.value) for i in at2.info)
        check(
            "reportar: aviso de alcance visible (1 cuenta x 2 URLs = 2 reportes)",
            "Se harán 2 reportes" in infos
            and "1 cuentas" in infos
            and "2 URLs" in infos,
            infos[:160],
        )

    # ------------------------------------------------------------------ #
    # AppTest: resultado persistido (con paradas por el paro)
    # ------------------------------------------------------------------ #
    try:
        at_p = AppTest.from_function(_app_reportar_persistido, default_timeout=120)
        at_p.run()
        ok_persist = not at_p.exception
        error_persist = "" if ok_persist else str(at_p.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_persist, error_persist = False, f"{type(e).__name__}: {e}"
    check(
        "reportar: el resultado persistido renderiza sin excepciones (AppTest)",
        ok_persist,
        error_persist,
    )

    if ok_persist:
        metricas = {str(m.label): str(m.value) for m in at_p.metric}
        captions_p = "\n".join(str(c.value) for c in at_p.caption)
        warnings_p = "\n".join(str(w.value) for w in at_p.warning)
        botones_p = {b.key for b in at_p.button}
        check(
            "reportar: el resultado persistido pinta Exitosos/Fallidos (AppTest)",
            metricas.get("✅ Exitosos") == "7"
            and metricas.get("❌ Fallidos") == "2",
            str(metricas),
        )
        check(
            "reportar: cuentas_totales/paradas visibles con su aviso",
            "Cuentas: **12**" in captions_p
            and "⛔ 3 cuenta(s) quedaron sin ejecutarse por el paro." in warnings_p,
            (captions_p + warnings_p)[:180],
        )
        check(
            "reportar: hay boton para limpiar el resultado persistido",
            "btn_rep_limpiar_resultado" in botones_p,
        )
