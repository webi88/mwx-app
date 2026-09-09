import streamlit as st
from web.ui import cabecera


def render(usuario: dict):
    cabecera("🌐 BLOGS WEB", "Publicación de artículos en blogs")
    
    st.markdown("### Publicar Artículo")
    
    st.info(
        "Publica artículos generados por IA en los blogs WordPress configurados. "
        "El sistema original usaba WordPress REST API."
    )
    
    st.markdown("#### Datos del artículo")
    
    titulo = st.text_input("Título", key="blog_titulo")
    contenido = st.text_area(
        "Contenido (HTML o texto)",
        height=250,
        key="blog_contenido",
        placeholder="<p>Contenido del artículo...</p>",
    )
    
    col1, col2 = st.columns(2)
    with col1:
        estado_blog = st.selectbox(
            "Estado de publicación",
            ["publish", "draft", "pending"],
            key="blog_estado",
        )
    with col2:
        categoria = st.text_input("Categoría (slug)", key="blog_categoria")
    
    if st.button("📤 Publicar en Blog", type="primary", key="btn_blog"):
        if not titulo or not contenido:
            st.warning("Título y contenido son obligatorios.")
            return
        
        st.warning(
            "ℹ️ Para publicar necesitas configurar las credenciales de WordPress "
            "en el archivo de configuración (site URL + usuario + app password)."
        )