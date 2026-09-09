import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma


def render(usuario: dict):
    cabecera("👁️ VISUALIZACIONES", "Generar vistas en posts/videos")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "facebook", "instagram", "tiktok"],
        key="vis_plataforma",
        format_func=lambda p: {
            "twitter": "🐦 Twitter", "facebook": "📘 Facebook",
            "instagram": "📷 Instagram", "tiktok": "🎵 TikTok",
        }[p],
    )
    
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        st.warning(f"No hay cuentas activas de {plataforma}.")
        return
    
    cuenta_opts = {f"@{c.usuario}": c for c in cuentas}
    sel = st.selectbox("Cuenta", list(cuenta_opts), key="vis_cuenta")
    cuenta = cuenta_opts[sel]
    
    url = st.text_input(
        "URL del post/video",
        key="vis_url",
        placeholder=f"URL del post en {plataforma}",
    )
    
    vueltas = st.number_input("Número de visitas (vueltas)", 1, 50, 5, key="vis_vueltas")
    
    if st.button("👁️ Iniciar Visualizaciones", type="primary", key="btn_vis"):
        if not url:
            st.warning("La URL es obligatoria.")
            return
        
        from plataformas.base import PlataformaFactory
        
        progreso = st.progress(0)
        estado = st.empty()
        
        bot = PlataformaFactory.crear_bot(plataforma, cuenta.usuario)
        estado.write("⏳ Iniciando sesión...")
        
        if not bot.login_con_cookies():
            st.error("No se pudo iniciar sesión.")
            return
        
        exitos = 0
        for i in range(int(vueltas)):
            estado.write(f"👁️ Visita {i+1} de {int(vueltas)}")
            try:
                if bot.visualizar(url):
                    exitos += 1
            except Exception as e:
                st.error(f"Error en visita {i+1}: {e}")
            progreso.progress((i + 1) / int(vueltas))
        
        bot.cerrar()
        
        c1, c2 = st.columns(2)
        with c1:
            st.metric("✅ Visitas completadas", exitos)
        with c2:
            st.metric("Total programadas", int(vueltas))