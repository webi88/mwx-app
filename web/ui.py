import streamlit as st


# --------------------------------------------------------------------------- #
# Tema visual (CSS). Solo se define aquí y se inyecta una vez desde app.py.
# Las animaciones de entrada y los hovers son CSS puro (seguro: Streamlit no
# elimina <style>, solo los <script>).
# --------------------------------------------------------------------------- #
CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

    :root {
        --mw-bg: #0a0e13;
        --mw-surface: #11161d;
        --mw-surface-2: #171e27;
        --mw-border: rgba(255,255,255,0.08);
        --mw-border-strong: rgba(255,255,255,0.14);
        --mw-text: #e6edf3;
        --mw-muted: rgba(230,237,243,0.55);
        --mw-accent: #00b4c4;
        --mw-accent-2: #7c5cff;
        --mw-success: #2dd4a7;
        --mw-glow: 0 8px 32px rgba(0, 180, 196, 0.16);
    }

    html, body, [class*="css"], [data-testid="stAppViewContainer"] {
        font-family: 'Inter', -apple-system, sans-serif;
    }

    /* Quita el "padding" muerto superior que Streamlit agrega por defecto */
    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1200px;
    }

    /* ------------------------------- Cabecera ---------------------------- */
    .mw-header {
        position: relative;
        overflow: hidden;
        background:
            radial-gradient(120% 160% at 0% 0%, rgba(0,180,196,0.18) 0%, transparent 55%),
            radial-gradient(120% 160% at 100% 0%, rgba(124,92,255,0.16) 0%, transparent 55%),
            linear-gradient(135deg, #0c1420 0%, #0f1b2c 100%);
        padding: 24px 28px;
        border-radius: 18px;
        border: 1px solid var(--mw-border-strong);
        margin-bottom: 20px;
        box-shadow: 0 12px 40px rgba(0,0,0,0.45), var(--mw-glow);
        animation: mwFadeUp .45s cubic-bezier(.22,.8,.36,1) both;
    }
    .mw-header::before {
        content: "";
        position: absolute;
        inset: 0 0 auto 0;
        height: 3px;
        background: linear-gradient(90deg, var(--mw-accent), var(--mw-accent-2), var(--mw-accent));
        background-size: 200% 100%;
        animation: mwShimmer 6s linear infinite;
    }
    .mw-header__tag {
        display: inline-block;
        color: var(--mw-accent);
        letter-spacing: 2.5px;
        font-size: 0.66rem;
        font-weight: 700;
        text-transform: uppercase;
        margin-bottom: 8px;
    }
    .mw-header h1 {
        color: #ffffff;
        font-size: 1.7rem;
        letter-spacing: 1px;
        font-weight: 700;
        margin: 0;
        line-height: 1.2;
    }
    .mw-header p {
        color: var(--mw-muted);
        letter-spacing: 1px;
        font-size: 0.8rem;
        text-transform: uppercase;
        margin: 8px 0 0 0;
    }

    /* ------------------------------- Tarjetas ---------------------------- */
    .mw-card {
        background: linear-gradient(180deg, var(--mw-surface-2) 0%, var(--mw-surface) 100%);
        border: 1px solid var(--mw-border);
        border-radius: 14px;
        padding: 18px 20px;
        margin-bottom: 14px;
        box-shadow: 0 4px 18px rgba(0,0,0,0.28);
        transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
        animation: mwFadeUp .4s cubic-bezier(.22,.8,.36,1) both;
    }
    .mw-card:hover {
        transform: translateY(-2px);
        border-color: rgba(0,180,196,0.4);
        box-shadow: 0 10px 30px rgba(0,0,0,0.4), var(--mw-glow);
    }
    .mw-card h3 {
        color: #ffffff;
        font-size: 1rem;
        margin: 0 0 10px 0;
        letter-spacing: 0.5px;
        font-weight: 600;
    }

    .mw-badge {
        display: inline-block;
        background: linear-gradient(90deg, var(--mw-accent), var(--mw-accent-2));
        color: white;
        padding: 3px 12px;
        border-radius: 20px;
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 1px;
        box-shadow: 0 4px 12px rgba(0,180,196,0.35);
    }

    /* ------------------------------ Estadísticas ------------------------- */
    .mw-stat {
        position: relative;
        background: linear-gradient(180deg, rgba(0,180,196,0.14) 0%, rgba(0,180,196,0.04) 100%);
        border: 1px solid rgba(0,180,196,0.28);
        border-radius: 14px;
        padding: 18px 16px 14px;
        text-align: center;
        transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        animation: mwFadeUp .45s cubic-bezier(.22,.8,.36,1) both;
        overflow: hidden;
    }
    .mw-stat::before {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 2px;
        background: linear-gradient(90deg, var(--mw-accent), var(--mw-accent-2));
        opacity: .7;
    }
    .mw-stat:hover {
        transform: translateY(-2px);
        border-color: rgba(0,180,196,0.55);
        box-shadow: var(--mw-glow);
    }
    .mw-stat .valor {
        font-size: 1.9rem;
        font-weight: 800;
        color: #ffffff;
        line-height: 1.1;
    }
    .mw-stat .etiqueta {
        font-size: 0.68rem;
        color: var(--mw-muted);
        text-transform: uppercase;
        letter-spacing: 1.2px;
        margin-top: 6px;
    }

    /* ---------------------------- Widgets base --------------------------- */
    .stButton > button {
        border-radius: 10px;
        font-weight: 600;
        letter-spacing: 0.2px;
        transition: transform .12s ease, box-shadow .12s ease;
    }
    .stButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 6px 18px rgba(0,0,0,0.3);
    }
    .stTextInput input, .stTextArea textarea, .stNumberInput input {
        border-radius: 10px !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
        border-color: var(--mw-accent) !important;
        box-shadow: 0 0 0 1px var(--mw-accent) !important;
    }

    /* Expanders */
    [data-testid="stExpander"] {
        border: 1px solid var(--mw-border);
        border-radius: 12px;
        overflow: hidden;
        background: var(--mw-surface);
    }

    /* Tabs */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 4px;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        border-radius: 10px 10px 0 0;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        color: var(--mw-accent) !important;
    }
    [data-testid="stTabs"] [data-baseweb="tab-highlight"] {
        background-color: var(--mw-accent);
    }

    /* Selectbox / multiselect */
    [data-baseweb="select"] > div {
        border-radius: 10px !important;
        border-color: var(--mw-border-strong) !important;
        background-color: var(--mw-surface-2) !important;
    }

    /* Segmented control (píldoras de categoría) */
    [data-testid="stSegmentedControl"] {
        gap: 6px;
        padding: 4px;
        background: rgba(255,255,255,0.03);
        border: 1px solid var(--mw-border);
        border-radius: 14px;
    }
    [data-testid="stSegmentedControl"] button {
        border-radius: 10px !important;
        transition: all .16s ease;
    }

    /* Dataframes */
    [data-testid="stDataFrame"] {
        border-radius: 12px;
        overflow: hidden;
        border: 1px solid var(--mw-border);
    }

    /* ---------------------------- Animaciones ---------------------------- */
    @keyframes mwFadeUp {
        from { opacity: 0; transform: translateY(10px); }
        to   { opacity: 1; transform: translateY(0); }
    }
    @keyframes mwShimmer {
        0%   { background-position: 0% 50%; }
        100% { background-position: 200% 50%; }
    }

    /* Barra lateral: scrollbar más sutil */
    [data-testid="stSidebar"] ::-webkit-scrollbar { width: 8px; }
    [data-testid="stSidebar"] ::-webkit-scrollbar-thumb {
        background: rgba(255,255,255,0.12);
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
            <span class="mw-header__tag">📡 MW Group · Plataforma de gestión</span>
            <h1>{titulo}</h1>
            <p>{subtitulo} · By MW Group</p>
        </div>
        """,
        unsafe_allow_html=True
    )


def tarjeta(titulo: str, contenido: str):
    st.markdown(
        f"""
        <div class="mw-card">
            <h3>{titulo}</h3>
            <div style="color:var(--mw-muted); font-size:0.9rem;">{contenido}</div>
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
