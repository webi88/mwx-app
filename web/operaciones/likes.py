import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import (
    aviso_pausadas,
    cuentas_por_plataforma,
    ejecutar_en_cuentas,
    mostrar_resultados,
    separar_pausadas,
)


def render(usuario: dict):
    cabecera("❤️ INFLUENCIA: LIKES", "Dar likes masivos en varias plataformas")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "instagram", "facebook", "tiktok"],
        key="likes_plataforma",
        format_func=lambda p: {
            "twitter": "🐦 Twitter", "instagram": "📷 Instagram",
            "facebook": "📘 Facebook", "tiktok": "🎵 TikTok",
        }[p],
    )
    
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        st.warning(f"No hay cuentas activas de {plataforma}.")
        return

    # Pausadas para activación: fuera de los likes masivos (siguen en
    # mantenimiento). Además `ejecutar_en_cuentas` las omite por defecto.
    cuentas, pausadas = separar_pausadas(cuentas)
    aviso_pausadas(pausadas)
    if not cuentas:
        st.warning(
            f"Todas las cuentas activas de {plataforma} están pausadas para "
            "activación (siguen en mantenimiento)."
        )
        return
    
    cuenta_opts = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    seleccionadas = st.multiselect("Cuentas", list(cuenta_opts), key="likes_cuentas")
    
    st.markdown("Pega una URL por línea:")
    urls_text = st.text_area("URLs", height=120, key="likes_urls")
    
    if st.button("❤️ Dar Likes", type="primary", key="btn_likes"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        cuentas_sel = [cuenta_opts[s] for s in seleccionadas]
        
        if not cuentas_sel:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            todos_ok = True
            for url in urls:
                if not bot.like(url):
                    todos_ok = False
            return todos_ok
        
        resultados = ejecutar_en_cuentas(
            cuentas_sel, accion, plataforma, progreso, estado, tipo="like"
        )
        mostrar_resultados(resultados)