import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma, ejecutar_en_cuentas, mostrar_resultados


MOTIVOS = {
    "spam": "Spam",
    "hate": "Hate / Acoso",
    "abuse": "Abuso",
    "violence": "Violencia",
    "nudity": "Contenido sexual",
    "false_info": "Información falsa",
}


def render(usuario: dict):
    cabecera("🚩 REPORTAR POSTS", "Reportar contenido en las plataformas")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "instagram", "facebook", "tiktok"],
        key="rep_plataforma",
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
    seleccionadas = st.multiselect("Cuentas", list(cuenta_opts), key="rep_cuentas")
    
    motivo_labels = list(MOTIVOS.values())
    motivo_sel = st.selectbox("Motivo del reporte", motivo_labels, key="rep_motivo")
    motivo = [k for k, v in MOTIVOS.items() if v == motivo_sel][0]
    
    st.markdown("Pega una URL por línea:")
    urls_text = st.text_area("URLs", height=120, key="rep_urls")
    
    if st.button("🚩 Reportar", type="primary", key="btn_reportar"):
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
                if not bot.reportar_post(url, motivo):
                    todos_ok = False
            return todos_ok
        
        resultados = ejecutar_en_cuentas(cuentas_sel, accion, plataforma, progreso, estado)
        mostrar_resultados(resultados)