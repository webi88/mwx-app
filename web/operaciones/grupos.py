import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma


def render(usuario: dict):
    cabecera("👥 GRUPOS: RECIPROCIDAD", "Envío a grupos y reciprocidad")
    
    tabs = st.tabs([
        "📨 Enviar a Grupos",
        "🔄 Reciprocidad Completa",
        "📥 Recopilar Links",
    ])
    
    with tabs[0]:
        _enviar_grupos()
    with tabs[1]:
        _reciprocidad()
    with tabs[2]:
        _recopilar()


def _enviar_grupos():
    st.markdown("### 📨 Enviar enlaces a grupos")
    
    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        st.warning("No hay cuentas de Twitter.")
        return
    
    cuenta_opts = {f"@{c.usuario}": c for c in cuentas}
    sel = st.selectbox("Cuenta", list(cuenta_opts), key="grupos_cuenta")
    cuenta = cuenta_opts[sel]
    
    link = st.text_input("Link a compartir", key="grupos_link")
    mensaje = st.text_area("Mensaje", height=60, key="grupos_mensaje")
    grupos_text = st.text_area(
        "Grupos (uno por línea o separados por coma)",
        height=80, key="grupos_lista",
        placeholder="Grupo1\nGrupo2",
    )
    
    if st.button("📨 Enviar a Grupos", type="primary", key="btn_grupos"):
        grupos = [g.strip() for g in grupos_text.replace(",", "\n").splitlines() if g.strip()]
        if not link:
            st.warning("El link es obligatorio.")
            return
        if not grupos:
            st.warning("Escribe al menos un grupo.")
            return
        
        from plataformas.twitter.selenium_bot import TwitterBot
        bot = TwitterBot(cuenta.usuario)
        
        with st.spinner("Enviando..."):
            if bot.login_con_cookies():
                resultados = bot.enviar_link_a_grupos(link, mensaje, cuenta.usuario, grupos)
                bot.cerrar()
                c1, c2 = st.columns(2)
                with c1:
                    st.metric("✅ Enviados", resultados.get("enviados", 0))
                with c2:
                    st.metric("❌ Fallidos", resultados.get("fallidos", 0))
            else:
                st.error("No se pudo iniciar sesión con esa cuenta.")


def _reciprocidad():
    st.markdown("### 🔄 Reciprocidad de Grupos")
    
    st.info(
        "Recopila los links compartidos en grupos y luego da RT + like "
        "a quienes compartieron."
    )
    
    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        st.warning("No hay cuentas de Twitter.")
        return
    
    cuenta_opts = {f"@{c.usuario}": c for c in cuentas}
    sel = st.selectbox("Cuenta", list(cuenta_opts), key="recip_cuenta")
    cuenta = cuenta_opts[sel]
    
    hacer_reciprocidad = st.checkbox(
        "Hacer reciprocidad (RT + like a quienes comparten)", value=True,
        key="recip_hacer",
    )
    
    if st.button("🔄 Iniciar Reciprocidad", type="primary", key="btn_recip"):
        from plataformas.twitter.selenium_bot import TwitterBot
        bot = TwitterBot(cuenta.usuario)
        
        with st.spinner("Fase 1: Recopilando links de grupos..."):
            if bot.login_con_cookies():
                links = bot.recopilar_links_de_grupos(cuenta.usuario)
                
                st.markdown(f"### 📋 Links encontrados: {len(links)}")
                for l in links[:20]:
                    st.caption(l)
                
                if hacer_reciprocidad and links:
                    st.markdown("### 🔄 Dando RT + Like automáticamente...")
                    resultados = bot.solo_retwittear(links, cuenta.usuario, dar_like=True)
                    c1, c2 = st.columns(2)
                    with c1:
                        st.metric("✅ RT exitosos", resultados.get("exitos", 0))
                    with c2:
                        st.metric("❌ Fallidos", resultados.get("fallidos", 0))
                
                bot.cerrar()
            else:
                st.error("No se pudo iniciar sesión.")


def _recopilar():
    st.markdown("### 📥 Recopilar Links de Grupos")
    
    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        st.warning("No hay cuentas de Twitter.")
        return
    
    cuenta_opts = {f"@{c.usuario}": c for c in cuentas}
    sel = st.selectbox("Cuenta", list(cuenta_opts), key="recop_cuenta")
    cuenta = cuenta_opts[sel]
    
    if st.button("📥 Recopilar Links", type="primary", key="btn_recopilar"):
        from plataformas.twitter.selenium_bot import TwitterBot
        bot = TwitterBot(cuenta.usuario)
        
        with st.spinner("Recopilando links de grupos..."):
            if bot.login_con_cookies():
                links = bot.recopilar_links_de_grupos(cuenta.usuario)
                bot.cerrar()
                
                st.session_state["web_links_recopilados"] = links
                st.success(f"Se recopilaron {len(links)} links.")
            else:
                st.error("No se pudo iniciar sesión.")
    
    links = st.session_state.get("web_links_recopilados", [])
    if links:
        st.markdown(f"### 📋 Links ({len(links)})")
        for i, l in enumerate(links, 1):
            st.markdown(f"{i}. {l}")