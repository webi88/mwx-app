import streamlit as st
from web.ui import cabecera
from activaciones.motor import MotorActivacion
from core.config import settings


def render(usuario: dict):
    cabecera("🎯 ACTIVACIÓN MASIVA", "RT con cita masivo y aleatorizado por cohortes")

    st.info(
        f"Concurrencia máxima de navegadores: **{settings.max_browsers}** "
        f"(configurable con la variable `MAX_BROWSERS`). Headless: **{settings.headless}**."
    )

    st.markdown("Pega una URL de tweet por línea (objetivos a citar):")
    urls_text = st.text_area("URLs objetivo", height=100, key="act_urls")

    texto_base = st.text_area(
        "Texto base de la cita (se generan variaciones automáticas)",
        height=100,
        key="act_texto",
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        cantidad = st.number_input(
            "Cantidad de cuentas (vacío = todas)",
            min_value=0, max_value=1000, value=0, step=10, key="act_cant",
        )
    with col2:
        duracion_min = st.number_input(
            "Duración (min)", min_value=1, max_value=360, value=60, step=5, key="act_dur",
        )
    with col3:
        cohortes = st.number_input(
            "Cohortes", min_value=1, max_value=24, value=4, step=1, key="act_coh",
        )

    col4, col5 = st.columns(2)
    with col4:
        dar_like = st.checkbox("Dar like también", value=False, key="act_like")
    with col5:
        grupo = st.text_input("Filtrar por grupo (A/B/C, opcional)", key="act_grupo")

    if st.button("🎯 Lanzar activación", type="primary", key="btn_act"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Pega al menos una URL objetivo.")
            return
        if not texto_base:
            st.warning("Escribe el texto base de la cita.")
            return

        motor = MotorActivacion()
        progreso = st.progress(0.0)
        estado = st.empty()

        def callback(hechas, total, usuario, ok):
            progreso.progress(hechas / total if total else 0)
            icono = "✅" if ok else "❌"
            estado.write(f"⏳ {icono} **@{usuario}** ({hechas}/{total})")

        resultados = motor.ejecutar(
            urls=urls,
            texto_base=texto_base,
            cantidad_cuentas=int(cantidad) if cantidad > 0 else None,
            grupo=grupo.strip() or None,
            dar_like=dar_like,
            duracion_min=int(duracion_min),
            cohortes=int(cohortes),
            callback=callback,
        )

        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        col1.metric("🎯 Total", resultados["total"])
        col2.metric("✅ Exitosas", resultados["exitosas"])
        col3.metric("❌ Fallidas", resultados["fallidas"])

        if resultados["detalles"]:
            with st.expander("🔍 Detalle por cuenta", expanded=False):
                for d in resultados["detalles"]:
                    icono = "✅" if d["ok"] else "❌"
                    linea = f"{icono} @{d['usuario']} — {d['detalle']}"
                    if d.get("url"):
                        linea += f" — [ver post]({d['url']})"
                    st.markdown(linea)
