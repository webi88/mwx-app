import streamlit as st


# --------------------------------------------------------------------------- #
# Tema visual (CSS). Solo se define aquí y se inyecta una vez desde app.py.
# Las animaciones de entrada y los hovers son CSS puro (seguro: Streamlit no
# elimina <style>, solo los <script>).
# --------------------------------------------------------------------------- #
CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&family=Poppins:wght@300;400;500;600&display=swap');

    /* Paleta inspirada en mwgroup.agency: azul petroleo profundo, turquesa
       y verde menta, botones tipo pildora y tipografia redonda. */
    :root {
        --mw-bg: #0b2433;
        --mw-bg-2: #0a1f2c;
        --mw-surface: rgba(16, 48, 66, 0.72);
        --mw-surface-2: rgba(20, 58, 78, 0.85);
        --mw-border: rgba(94, 230, 214, 0.12);
        --mw-border-strong: rgba(94, 230, 214, 0.24);
        --mw-text: #eef6f8;
        --mw-muted: rgba(220, 235, 240, 0.64);
        --mw-accent: #23b5bf;
        --mw-accent-deep: #1d7f86;
        --mw-accent-2: #5ee6c9;
        --mw-navy: #0a2232;
        --mw-pill: #dfe5e9;
        --mw-success: #5ee6c9;
        --mw-glow: 0 10px 34px rgba(35, 181, 191, 0.18);
        --mw-gradient: linear-gradient(90deg, #1d7f86 0%, #23b5bf 45%, #5ee6c9 100%);
    }

    html, body, [class*="css"], [data-testid="stAppViewContainer"],
    .stMarkdown, .stTextInput, .stSelectbox, button, input, textarea,
    [data-testid="stAppViewContainer"] :is(p, li, label, input, textarea, button, td, th, summary),
    [data-testid="stSidebar"] :is(p, li, label, input, button, summary) {
        font-family: 'Poppins', -apple-system, sans-serif;
    }
    h1, h2, h3, h4, h5, h6,
    [data-testid="stHeading"], .mw-header h1, .mw-stat .valor {
        font-family: 'Plus Jakarta Sans', 'Poppins', sans-serif !important;
        letter-spacing: -0.3px;
    }

    /* Fondo: azul petroleo con brillo suave y cuadricula tenue */
    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(60% 50% at 50% 0%, rgba(35,181,191,0.10) 0%, transparent 70%),
            linear-gradient(rgba(94,230,214,0.035) 1px, transparent 1px) 0 0 / 56px 56px,
            linear-gradient(90deg, rgba(94,230,214,0.035) 1px, transparent 1px) 0 0 / 56px 56px,
            linear-gradient(180deg, var(--mw-bg) 0%, var(--mw-bg-2) 100%);
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #0c2a3b 0%, #0a1f2c 100%);
        border-right: 1px solid var(--mw-border);
    }

    .block-container { padding-top: 1.6rem; padding-bottom: 3rem; }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1200px;
    }

    /* ------------------------------- Cabecera ---------------------------- */
    .mw-header {
        position: relative;
        overflow: hidden;
        text-align: center;
        background:
            radial-gradient(70% 120% at 50% 0%, rgba(35,181,191,0.16) 0%, transparent 65%),
            var(--mw-surface);
        padding: 30px 28px 26px;
        border-radius: 28px;
        border: 1px solid var(--mw-border-strong);
        margin-bottom: 22px;
        box-shadow: 0 14px 44px rgba(0,0,0,0.35);
        backdrop-filter: blur(6px);
        animation: mwFadeUp .45s cubic-bezier(.22,.8,.36,1) both;
    }
    /* Puntos de luz decorativos (como en la pagina de referencia) */
    .mw-header::before, .mw-header::after {
        content: "";
        position: absolute;
        width: 8px; height: 8px;
        border-radius: 50%;
    }
    .mw-header::before {
        top: 18px; left: 9%;
        background: #e2504f;
        box-shadow: 0 0 14px 4px rgba(226,80,79,0.45);
    }
    .mw-header::after {
        bottom: 22px; right: 10%;
        background: var(--mw-accent);
        box-shadow: 0 0 14px 4px rgba(35,181,191,0.45);
    }
    .mw-header__tag {
        display: inline-block;
        color: var(--mw-text);
        letter-spacing: 2.5px;
        font-size: 0.68rem;
        font-weight: 600;
        padding: 6px 16px;
        border: 1px solid var(--mw-border-strong);
        border-radius: 999px;
        background: rgba(10,34,50,0.55);
        margin-bottom: 12px;
    }
    .mw-header h1 {
        font-size: 2rem;
        font-weight: 800;
        margin: 0;
        line-height: 1.15;
        background: linear-gradient(90deg, #ffffff 0%, #ffffff 35%, #5ee6c9 100%);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    .mw-header p {
        color: var(--mw-muted);
        font-size: 0.92rem;
        margin: 10px 0 0 0;
    }

    /* ------------------------------- Tarjetas ---------------------------- */
    .mw-card {
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 22px;
        padding: 20px 22px;
        margin-bottom: 14px;
        box-shadow: 0 6px 22px rgba(0,0,0,0.22);
        transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
        animation: mwFadeUp .4s cubic-bezier(.22,.8,.36,1) both;
    }
    .mw-card:hover {
        transform: translateY(-2px);
        border-color: var(--mw-border-strong);
        box-shadow: 0 12px 32px rgba(0,0,0,0.3), var(--mw-glow);
    }
    .mw-card h3 {
        color: #ffffff;
        font-size: 1.02rem;
        margin: 0 0 10px 0;
        font-weight: 700;
    }

    .mw-badge {
        display: inline-block;
        background: var(--mw-gradient);
        color: #06202c;
        padding: 4px 14px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.5px;
    }

    /* ------------------------------ Estadisticas ------------------------- */
    .mw-stat {
        position: relative;
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 22px;
        padding: 20px 16px 16px;
        text-align: center;
        transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease;
        animation: mwFadeUp .45s cubic-bezier(.22,.8,.36,1) both;
        overflow: hidden;
    }
    .mw-stat:hover {
        transform: translateY(-2px);
        border-color: var(--mw-border-strong);
        box-shadow: var(--mw-glow);
    }
    .mw-stat .valor {
        font-size: 2rem;
        font-weight: 800;
        line-height: 1.1;
        background: var(--mw-gradient);
        -webkit-background-clip: text;
        background-clip: text;
        color: transparent;
    }
    .mw-stat .etiqueta {
        font-size: 0.7rem;
        color: var(--mw-muted);
        text-transform: uppercase;
        letter-spacing: 2px;
        margin-top: 6px;
    }

    /* ---------------------------- Botones (pildora) ---------------------- */
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
        border-radius: 999px !important;
        font-weight: 600;
        padding: 0.45rem 1rem;
        white-space: nowrap;
        border: 1.5px solid var(--mw-border-strong) !important;
        background: transparent;
        color: var(--mw-text);
        transition: transform .12s ease, box-shadow .12s ease, background .15s ease;
    }
    .stButton > button:hover, .stDownloadButton > button:hover,
    .stFormSubmitButton > button:hover {
        transform: translateY(-1px);
        border-color: var(--mw-accent) !important;
        color: #ffffff;
        box-shadow: var(--mw-glow);
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"],
    .stFormSubmitButton > button {
        background: var(--mw-accent-deep) !important;
        border-color: var(--mw-accent-deep) !important;
        color: #ffffff !important;
    }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button:hover {
        background: var(--mw-accent) !important;
        border-color: var(--mw-accent) !important;
    }

    /* ---------------------------- Campos ---------------------------------- */
    .stTextInput input, .stTextArea textarea, .stNumberInput input,
    .stDateInput input, .stTimeInput input {
        border-radius: 14px !important;
        background-color: rgba(10,34,50,0.6) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
        border-color: var(--mw-accent) !important;
        box-shadow: 0 0 0 1px var(--mw-accent) !important;
    }
    [data-baseweb="select"] > div {
        border-radius: 14px !important;
        border-color: var(--mw-border-strong) !important;
        background-color: rgba(10,34,50,0.6) !important;
    }
    [data-baseweb="tag"] {
        border-radius: 999px !important;
        background-color: var(--mw-accent-deep) !important;
    }

    /* Expanders */
    [data-testid="stExpander"] {
        border: 1px solid var(--mw-border);
        border-radius: 18px;
        overflow: hidden;
        background: var(--mw-surface);
    }

    /* Pestanas como pildoras */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 6px;
        padding: 5px;
        background: rgba(10,34,50,0.55);
        border: 1px solid var(--mw-border);
        border-radius: 999px;
        width: fit-content;
        max-width: 100%;
        flex-wrap: wrap;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        border-radius: 999px;
        padding: 6px 16px;
        height: auto;
    }
    [data-testid="stTabs"] [aria-selected="true"] {
        background: var(--mw-accent-deep) !important;
        color: #ffffff !important;
    }
    [data-testid="stTabs"] [data-baseweb="tab-highlight"],
    [data-testid="stTabs"] [data-baseweb="tab-border"] {
        display: none;
    }

    /* Selector de categorias (pildoras) */
    [data-testid="stSegmentedControl"] {
        gap: 6px;
        padding: 5px;
        background: rgba(10,34,50,0.55);
        border: 1px solid var(--mw-border);
        border-radius: 999px;
    }
    [data-testid="stSegmentedControl"] button {
        border-radius: 999px !important;
        transition: all .16s ease;
    }

    /* Metricas nativas de Streamlit */
    [data-testid="stMetric"] {
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 20px;
        padding: 14px 18px;
    }
    [data-testid="stMetricValue"] { color: var(--mw-accent-2); }

    /* Tablas, alertas y barra de progreso */
    [data-testid="stDataFrame"] {
        border-radius: 18px;
        overflow: hidden;
        border: 1px solid var(--mw-border);
    }
    [data-testid="stAlert"] { border-radius: 16px; }
    [data-testid="stProgress"] > div > div > div > div {
        background: var(--mw-gradient) !important;
    }

    [data-testid="stSidebar"] .stButton > button { padding: 0.4rem 0.75rem; }

    /* Enlaces */
    a { color: var(--mw-accent-2); }

    /* ---------------------------- Animaciones ---------------------------- */
    @keyframes mwFadeUp {
        from { opacity: 0; transform: translateY(10px); }
        to   { opacity: 1; transform: translateY(0); }
    }

    /* Scrollbar sutil */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-thumb {
        background: rgba(94,230,214,0.18);
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
            <span class="mw-header__tag">📡 MWX · Plataforma de gestión</span>
            <h1>{titulo}</h1>
            <p>{subtitulo}</p>
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
        f"<div style='text-align:center; color:var(--mw-muted); padding:40px 0;'>"
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
