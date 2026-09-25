"""Checks de la operacion "Reportar Posts o Cuentas" (`web/operaciones/reportar.py`).

Cubre:
  - `_reportar_objetivo_con_bot`: usa `reportar_objetivo(url, motivo)` cuando el
    bot lo expone (Twitter nuevo) y cae a `reportar_post(url, motivo)` para bots
    viejos y plataformas que solo tienen reportar_post (FB/IG/TikTok); devuelve
    False si el reporte falla.
  - Motivos alineados al mapping del bot (`sensitive` en vez del viejo `nudity`)
    mas `impersonation`/`self_harm`/`false_info` con etiquetas en espanol.
  - Cabecera nueva, ayuda de formatos (post vs cuenta/perfil de X) y el aviso de
    alcance ("Se haran N reportes (C cuentas x U URLs)") antes de ejecutar.
  - AppTest de la pagina (0 excepciones), de la cabecera y de una seleccion
    simulada en `session_state` que muestra el aviso de alcance. NUNCA se pulsa
    `btn_reportar` (enviaria reportes reales).

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows).
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _fuente_reportar() -> str:
    return (RAIZ / "web" / "operaciones" / "reportar.py").read_text(encoding="utf-8")


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
    """Script de AppTest: seleccion simulada para ver el aviso de alcance."""
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
    st.session_state["rep_cuentas"] = ["@cuenta_uno (Grupo A)"]
    st.session_state["rep_urls"] = (
        "https://x.com/usuario/status/123\nhttps://x.com/usuario"
    )
    try:
        reportar.render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})
    finally:
        reportar.cuentas_por_plataforma = original


def run(check):
    from web.operaciones import reportar as pagina

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
    # Fuente: cabecera, formatos, alcance, motivos y wiring
    # ------------------------------------------------------------------ #
    fuente = _fuente_reportar()
    check(
        "reportar: la fuente usa getattr(reportar_objetivo) con fallback a "
        "reportar_post",
        'getattr(bot, "reportar_objetivo", None)' in fuente
        and "bot.reportar_post(url, motivo)" in fuente,
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
        claves_multi = {m.key for m in at.multiselect}
        claves_botones = {b.key for b in at.button}
        check(
            "reportar: controles presentes (plataforma, cuentas, motivo, URLs, boton)",
            {"rep_plataforma", "rep_motivo"} <= claves_select
            and "rep_cuentas" in claves_multi
            and "rep_urls" in claves_area
            and "btn_reportar" in claves_botones,
            str(sorted(claves_select | claves_area | claves_multi | claves_botones)),
        )
        html = "\n".join(str(m.value) for m in at.markdown)
        check(
            "reportar: la cabecera nueva se ve en la pagina (AppTest)",
            "REPORTAR POSTS O CUENTAS" in html
            and "Reporta publicaciones o perfiles en X con tus cuentas" in html,
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
