import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma, ejecutar_en_cuentas, mostrar_resultados


def render(usuario: dict):
    cabecera("🚀 IMPULSO: FOLLOWS", "Seguir cuentas/perfiles masivamente")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "instagram", "facebook", "tiktok"],
        key="follows_plataforma",
        format_func=lambda p: {
            "twitter": "🐦 Twitter", "instagram": "📷 Instagram",
            "facebook": "📘 Facebook", "tiktok": "🎵 TikTok",
        }[p],
    )
    
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        st.warning(f"No hay cuentas activas de {plataforma}.")
        return
    
    cuenta_opts = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    seleccionadas = st.multiselect("Cuentas", list(cuenta_opts), key="follows_cuentas")
    
    st.markdown("Pega un perfil URL por línea:")
    urls_text = st.text_area(
        "URLs de perfiles",
        height=120,
        key="follows_urls",
        placeholder="https://x.com/usuario_a_seguir",
    )
    
    if st.button("🚀 Seguir", type="primary", key="btn_follows"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        cuentas_sel = [cuenta_opts[s] for s in seleccionadas]
        
        if not cuentas_sel:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL de perfil.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            todos_ok = True
            for url in urls:
                if not bot.seguir_usuario(url):
                    todos_ok = False
            return todos_ok
        
        resultados = ejecutar_en_cuentas(cuentas_sel, accion, plataforma, progreso, estado)
        mostrar_resultados(resultados)