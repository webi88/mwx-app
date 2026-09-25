"""Checks de la pagina de alertas (`web/operaciones/alertas.py`).

Cubre:
  - La persistencia del resultado de "Ejecutar Alertas" en `st.session_state`
    y su render DESPUES del rerun (el bug historico: `st.rerun()` borraba las
    cifras recien pintadas y el usuario nunca las veia).
  - Normalizacion tolerante del resultado del motor (mencionadas/enviadas/
    filtradas/duplicadas + errores de envio) y de `por_fuente`/`por_temas`
    viejos o ausentes.
  - Conteos de "hoy" con `_inicio_del_dia` (sin segundos/microsegundos) y
    conteo agrupado de clientes (sin N+1).
  - Caption de estado: informa que en Railway las alertas corren automaticas
    (job del scheduler) cada pocas horas segun configuracion y que el boton es
    manual.
  - AppTest de la pagina completa (0 excepciones) y una renderizacion con
    `alertas_ultimo_resultado` simulado en `session_state` que demuestra que
    las cifras y los errores se ven tras el rerun.
  - `envio_pausado` del motor (`ALERTAS_ENVIAR_TELEGRAM` desactivado):
    se normaliza (ausente => False) y la pagina avisa que las alertas se
    registran en el dashboard pero NO se envian a Telegram.

NUNCA pulsa `btn_ejecutar_alertas` (dispararia busquedas reales y Telegram);
solo pulsa el boton de limpiar resultado, que es 100% local.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _fuente_alertas() -> str:
    return (RAIZ / "web" / "operaciones" / "alertas.py").read_text(encoding="utf-8")


def _app_alertas_render():
    """Script de AppTest: render completo de la pagina (solo lectura).

    Solo ASCII: `AppTest.from_function` escribe el script temporal con la
    codificacion local de Windows y los acentos/emojis lo rompen en silencio."""
    from web.operaciones.alertas import render

    render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})


def _app_alertas_persistido():
    """Script de AppTest: simula un resultado ya persistido en session_state.

    El sembrado es condicional (bandera `sembrado`) para que al pulsar
    "Limpiar resultado" el rerun NO lo vuelva a inyectar y se pueda comprobar
    que desaparece de la vista. Solo ASCII."""
    import streamlit as st

    from web.operaciones.alertas import CLAVE_RESULTADO_ALERTAS, render

    if not st.session_state.get("sembrado"):
        st.session_state["sembrado"] = True
        st.session_state[CLAVE_RESULTADO_ALERTAS] = {
            "cliente": "Cliente Demo",
            "horas": 24,
            "fecha": "01/01/2026 10:00:00",
            "mencionadas": 17,
            "total": 17,
            "enviadas": 15,
            "filtradas": 12,
            "duplicadas": 11,
            "errores": ["chat 123: parse error"],
            "total_errores": 1,
            "error": "",
        }
    render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})


def _app_alertas_persistido_pausado():
    """Script de AppTest: resultado persistido con `envio_pausado=True`.

    Simula una ejecucion con `ALERTAS_ENVIAR_TELEGRAM` desactivado (el motor
    detecta y registra, pero no envia a Telegram). Solo ASCII."""
    import streamlit as st

    from web.operaciones.alertas import CLAVE_RESULTADO_ALERTAS, render

    st.session_state[CLAVE_RESULTADO_ALERTAS] = {
        "cliente": "Cliente Demo",
        "horas": 24,
        "fecha": "01/01/2026 10:00:00",
        "mencionadas": 17,
        "total": 17,
        "enviadas": 0,
        "filtradas": 12,
        "duplicadas": 11,
        "errores": [],
        "total_errores": 0,
        "error": "",
        "envio_pausado": True,
    }
    render({"rol": "admin", "nombre": "Auditor", "usuario": "auditor"})


def run(check):
    from web.operaciones import alertas as pagina

    # ------------------------------------------------------------------ #
    # Helpers puros (sin Streamlit runtime)
    # ------------------------------------------------------------------ #
    inicio = pagina._inicio_del_dia()
    check(
        "alertas: _inicio_del_dia deja hora/minuto/segundo/microsegundo en cero",
        (inicio.hour, inicio.minute, inicio.second, inicio.microsecond)
        == (0, 0, 0, 0),
        str(inicio),
    )
    check(
        "alertas: _inicio_del_dia conserva la fecha de hoy",
        inicio.date() == datetime.now().date(),
    )

    res = pagina._normalizar_resultado(
        {
            "total": 7,
            "enviadas": 5,
            "filtradas": 2,
            "duplicadas": 1,
            "errores_envio": ["chat 123: parse error"],
        },
        cliente="Cliente X",
        horas=24,
        iniciada="01/01/2026 10:00:00",
    )
    check(
        "alertas: normaliza mencionadas/enviadas/filtradas/duplicadas",
        res["mencionadas"] == 7
        and res["enviadas"] == 5
        and res["filtradas"] == 2
        and res["duplicadas"] == 1,
        str(res),
    )
    check(
        "alertas: conserva errores_envio como lista",
        res["errores"] == ["chat 123: parse error"] and res["total_errores"] == 1,
        str(res["errores"]),
    )
    vacio = pagina._normalizar_resultado(None)
    check(
        "alertas: resultado None/ausente no lanza y queda en ceros",
        vacio["mencionadas"] == 0 and vacio["errores"] == [] and vacio["error"] == "",
    )
    alias = pagina._normalizar_resultado(
        {"mencionadas": "9", "errores": {"cuenta1": "timeout"}}
    )
    check(
        "alertas: tolera alias mencionadas y errores tipo dict",
        alias["mencionadas"] == 9 and alias["errores"] == ["cuenta1: timeout"],
        str(alias["errores"]),
    )
    pausado = pagina._normalizar_resultado({"total": 3, "envio_pausado": True})
    check(
        "alertas: normaliza envio_pausado=True",
        pausado["envio_pausado"] is True,
        str(pausado.get("envio_pausado")),
    )
    activo = pagina._normalizar_resultado({"total": 3, "envio_pausado": False})
    check(
        "alertas: normaliza envio_pausado=False",
        activo["envio_pausado"] is False,
        str(activo.get("envio_pausado")),
    )
    sin_flag = pagina._normalizar_resultado({"total": 3})
    check(
        "alertas: envio_pausado ausente queda en False",
        sin_flag["envio_pausado"] is False,
        str(sin_flag.get("envio_pausado")),
    )
    check(
        "alertas: envio_pausado tolera strings ('1'/'0')",
        pagina._normalizar_resultado({"envio_pausado": "1"})["envio_pausado"]
        is True
        and pagina._normalizar_resultado({"envio_pausado": "0"})["envio_pausado"]
        is False,
    )
    check(
        "alertas: _pares_conteo acepta dict",
        pagina._pares_conteo({"Google News": 3}) == [("Google News", 3)],
    )
    check(
        "alertas: _pares_conteo tolera None/str/lista de dicts",
        pagina._pares_conteo(None) == []
        and pagina._pares_conteo("x") == []
        and pagina._pares_conteo([{"tema": "politica", "cantidad": 2}])
        == [("politica", 2)],
    )

    # ------------------------------------------------------------------ #
    # Fuente: persistencia del resultado, caption y fixes menores
    # ------------------------------------------------------------------ #
    fuente = _fuente_alertas()
    pos_guardado = fuente.find("st.session_state[CLAVE_RESULTADO_ALERTAS] = datos")
    pos_rerun = fuente.find("st.rerun()", pos_guardado)
    check(
        "alertas: el resultado se guarda en session_state ANTES del st.rerun()",
        pos_guardado != -1 and pos_rerun != -1,
        f"(guardado={pos_guardado}, rerun={pos_rerun})",
    )
    check(
        "alertas: _render_ultimo_resultado lee el resultado persistido",
        "st.session_state.get(CLAVE_RESULTADO_ALERTAS)" in fuente
        and "def _render_ultimo_resultado" in fuente,
    )
    caption_automatico = pagina.CAPTION_AUTOMATICO
    check(
        "alertas: caption avisa ejecucion automatica cada pocas horas segun "
        "configuracion (job del scheduler) y ejecucion manual",
        "automática cada pocas horas" in caption_automatico
        and "según configuración" in caption_automatico
        and "job del scheduler" in caption_automatico
        and "ejecución manual" in caption_automatico,
        caption_automatico,
    )
    check(
        "alertas: conteos de hoy usan _inicio_del_dia (sin segundos)",
        fuente.count("_inicio_del_dia()") >= 3
        and "replace(hour=0, minute=0, second=0, microsecond=0)" in fuente,
    )
    check(
        "alertas: clientes por conteo agrupado (sin N+1)",
        "group_by(AlertaHistorial.cliente_id)" in fuente,
    )
    check(
        "alertas: resumenes usan _pares_conteo para fuente y temas + fecha",
        "_pares_conteo(reporte.por_fuente)" in fuente
        and "_pares_conteo(reporte.por_temas)" in fuente
        and "Fecha de generación" in fuente,
    )
    check(
        "alertas: menciones del dia muestran fecha y toleran titulos vacios",
        "hoy.strftime" in fuente and "mencion.titulo or" in fuente,
    )
    texto_pausa = pagina.TEXTO_ENVIO_PAUSADO
    check(
        "alertas: el texto de envio pausado avisa que NO se envia a Telegram "
        "y nombra ALERTAS_ENVIAR_TELEGRAM",
        "Envío a Telegram en pausa" in texto_pausa
        and "ALERTAS_ENVIAR_TELEGRAM" in texto_pausa
        and "NO se envían" in texto_pausa,
        texto_pausa[:160],
    )
    check(
        "alertas: el panel de ultimo resultado muestra el aviso si esta pausado",
        "def _aviso_envio_pausado" in fuente
        and "_aviso_envio_pausado(datos)" in fuente,
    )

    # ------------------------------------------------------------------ #
    # AppTest: render completo de la pagina
    # ------------------------------------------------------------------ #
    from streamlit.testing.v1 import AppTest

    try:
        at = AppTest.from_function(_app_alertas_render, default_timeout=180)
        at.run()
        ok_render = not at.exception
        error_render = "" if ok_render else str(at.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_render, error_render = False, f"{type(e).__name__}: {e}"
    check(
        "alertas: la pagina renderiza sin excepciones (AppTest)",
        ok_render,
        error_render,
    )

    if ok_render:
        tabs = [t.label for t in at.tabs]
        check(
            "alertas: las 7 pestanas siguen registradas",
            len(tabs) == 7
            and tabs[0].endswith("Alertas Recientes")
            and tabs[-1].endswith("Ejecutar Alertas"),
            str(tabs),
        )
        textos = [str(m.value) for m in at.markdown] + [
            str(c.value) for c in at.caption
        ]
        check(
            "alertas: el boton manual y el caption de automatico estan presentes",
            any(b.key == "btn_ejecutar_alertas" for b in at.button)
            and any("pocas horas" in t for t in textos),
        )

    # ------------------------------------------------------------------ #
    # AppTest: el resultado persistido se ve tras el rerun
    # ------------------------------------------------------------------ #
    try:
        at2 = AppTest.from_function(_app_alertas_persistido, default_timeout=180)
        at2.run()
        ok_persist = not at2.exception
        error_persist = "" if ok_persist else str(at2.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_persist, error_persist = False, f"{type(e).__name__}: {e}"
    check(
        "alertas: el resultado persistido renderiza sin excepciones (AppTest)",
        ok_persist,
        error_persist,
    )

    if ok_persist:
        html = "\n".join(str(m.value) for m in at2.markdown)
        captions = "\n".join(str(c.value) for c in at2.caption)
        warnings = "\n".join(str(w.value) for w in at2.warning)
        check(
            "alertas: tras el rerun se ven las 4 cifras persistidas",
            all(f'class="valor">{n}</div>' in html for n in (17, 15, 12, 11))
            and all(
                etiqueta in html
                for etiqueta in ("Menciones", "Enviadas", "Filtradas", "Duplicadas")
            ),
        )
        check(
            "alertas: tras el rerun se ve 'Búsqueda completada' y el cliente",
            any("Búsqueda completada" in str(s.value) for s in at2.success)
            and "Cliente Demo" in captions,
        )
        check(
            "alertas: los errores de envio persistidos se muestran",
            "error(es)" in warnings and "chat 123: parse error" in captions,
            warnings[:120],
        )
        check(
            "alertas: sin envio_pausado NO aparece el aviso de pausa",
            "Envío a Telegram en pausa" not in warnings,
            warnings[:120],
        )
        check(
            "alertas: hay boton para limpiar el resultado",
            any(b.key == "btn_alertas_limpiar_resultado" for b in at2.button),
        )

        # El boton de limpiar es 100% local (no toca red/Telegram): se pulsa.
        try:
            at3 = at2.button(key="btn_alertas_limpiar_resultado").click().run()
            limpio = not at3.exception and not any(
                "Búsqueda completada" in str(s.value) for s in at3.success
            )
            detalle_limpieza = (
                "sin excepciones"
                if not at3.exception
                else str(at3.exception[0].value)[:120]
            )
        except Exception as e:  # noqa: BLE001
            limpio, detalle_limpieza = False, f"{type(e).__name__}: {e}"
        check(
            "alertas: limpiar resultado lo quita de la vista",
            limpio,
            detalle_limpieza,
        )

    # ------------------------------------------------------------------ #
    # AppTest: resultado persistido con el envio a Telegram en pausa
    # ------------------------------------------------------------------ #
    try:
        at4 = AppTest.from_function(
            _app_alertas_persistido_pausado, default_timeout=180
        )
        at4.run()
        ok_pausa = not at4.exception
        error_pausa = "" if ok_pausa else str(at4.exception[0].value)[:200]
    except Exception as e:  # noqa: BLE001
        ok_pausa, error_pausa = False, f"{type(e).__name__}: {e}"
    check(
        "alertas: resultado con envio_pausado renderiza sin excepciones (AppTest)",
        ok_pausa,
        error_pausa,
    )

    if ok_pausa:
        avisos = "\n".join(str(w.value) for w in at4.warning)
        html_pausa = "\n".join(str(m.value) for m in at4.markdown)
        check(
            "alertas: el aviso de envio pausado es visible con el texto completo",
            "Envío a Telegram en pausa" in avisos
            and "ALERTAS_ENVIAR_TELEGRAM" in avisos
            and "NO se envían" in avisos,
            avisos[:160],
        )
        check(
            "alertas: con envio pausado las cifras siguen visibles",
            all(f'class="valor">{n}</div>' in html_pausa for n in (17, 12, 11)),
        )
