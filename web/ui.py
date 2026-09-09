import streamlit as st


CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    .mw-header {
        background: linear-gradient(135deg, #0a1a2f 0%, #0d2847 100%);
        padding: 22px 30px;
        border-radius: 14px;
        border-left: 5px solid #0095A6;
        margin-bottom: 20px;
        box-shadow: 0 4px 20px rgba(0,0,0,0.35);
    }
    .mw-header h1 {
        color: #ffffff;
        font-size: 1.6rem;
        letter-spacing: 1.5px;
        font-weight: 600;
        margin: 0;
    }
    .mw-header p {
        color: #0095A6;
        letter-spacing: 2px;
        font-size: 0.75rem;
        text-transform: uppercase;
        margin: 4px 0 0 0;
    }
    .mw-card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px;
        padding: 18px 20px;
        margin-bottom: 14px;
    }
    .mw-card h3 {
        color: #ffffff;
        font-size: 1rem;
        margin: 0 0 10px 0;
        letter-spacing: 0.5px;
    }
    .mw-badge {
        display: inline-block;
        background: #0095A6;
        color: white;
        padding: 2px 10px;
        border-radius: 20px;
        font-size: 0.7rem;
        font-weight: 600;
        letter-spacing: 1px;
    }
    .mw-stat {
        background: rgba(0,149,166,0.12);
        border: 1px solid rgba(0,149,166,0.25);
        border-radius: 10px;
        padding: 14px 16px;
        text-align: center;
    }
    .mw-stat .valor {
        font-size: 1.8rem;
        font-weight: 700;
        color: #0095A6;
    }
    .mw-stat .etiqueta {
        font-size: 0.7rem;
        color: rgba(255,255,255,0.6);
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    .stButton > button {
        border-radius: 8px;
        font-weight: 500;
    }
    .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div {
        border-radius: 8px;
    }
</style>
"""


def inyectar_css():
    st.markdown(CSS, unsafe_allow_html=True)


def cabecera(titulo: str, subtitulo: str = "Sistema de Comunicación Estratégica"):
    st.markdown(
        f"""
        <div class="mw-header">
            <h1>{titulo}</h1>
            <p>{subtitulo} | By MW Group</p>
        </div>
        """,
        unsafe_allow_html=True
    )


def tarjeta(titulo: str, contenido: str):
    st.markdown(
        f"""
        <div class="mw-card">
            <h3>{titulo}</h3>
            <div style="color:rgba(255,255,255,0.85); font-size:0.9rem;">{contenido}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def stat(valor, etiqueta):
    st.markdown(
        f"""
        <div class="mw-stat">
            <div class="valor">{valor}</div>
            <div class="etiqueta">{etiqueta}</div>
        </div>
        """,
        unsafe_allow_html=True
    )


def empty_state(mensaje: str):
    st.markdown(
        f"<div style='text-align:center; color:rgba(255,255,255,0.4); padding:40px 0;'>"
        f"{mensaje}</div>",
        unsafe_allow_html=True
    )


def divider():
    st.markdown("---")


EMOJIS_PLATAFORMA = {
    "twitter": "🐦",
    "facebook": "📘",
    "instagram": "📷",
    "tiktok": "🎵",
    "X (Twitter)": "🐦",
}


def emoji_plataforma(plataforma: str) -> str:
    return EMOJIS_PLATAFORMA.get(plataforma, "📱")