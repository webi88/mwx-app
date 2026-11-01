import streamlit as st


# --------------------------------------------------------------------------- #
# Tema visual (CSS). Solo se define aquí y se inyecta una vez desde app.py.
# Las animaciones de entrada y los hovers son CSS puro (seguro: Streamlit no
# elimina <style>, solo los <script>).
# --------------------------------------------------------------------------- #
CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600&display=swap');

    /* Estilo minimalista: area principal blanca con acentos turquesa y la
       barra lateral en azul petroleo (branding). Titulos en serif y texto
       en sans pequeno, al estilo de Claude. */
    :root {
        --mw-bg: #ffffff;
        --mw-surface: #ffffff;
        --mw-surface-2: #f6f9fa;
        --mw-border: #e6ecee;
        --mw-border-strong: #d4dee1;
        --mw-text: #1d2a30;
        --mw-muted: #6b7a80;
        --mw-accent: #1d8f97;
        --mw-accent-deep: #17707a;
        --mw-accent-soft: rgba(29,143,151,0.08);
        --mw-accent-2: #3cc8b4;
        --mw-navy: #0b2433;
        --mw-navy-2: #0f2e40;
        --mw-side-text: #e3eef1;
        --mw-side-muted: rgba(227,238,241,0.6);
        --mw-side-border: rgba(94,230,214,0.14);
    }

    /* ----------------------------- Tipografia ---------------------------- */
    html, body, [class*="css"], [data-testid="stAppViewContainer"],
    [data-testid="stAppViewContainer"] :is(p, li, label, input, textarea, button, td, th, summary, div),
    [data-testid="stSidebar"] :is(p, li, label, input, button, summary) {
        font-family: 'Poppins', -apple-system, 'Segoe UI', sans-serif;
    }
    [data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }
    html, body { font-size: 14px; }
    [data-testid="stAppViewContainer"] p,
    [data-testid="stAppViewContainer"] li,
    [data-testid="stMarkdownContainer"] { font-size: 0.93rem; line-height: 1.6; }
    label, [data-testid="stWidgetLabel"] p {
        font-size: 0.82rem !important;
        color: var(--mw-muted) !important;
        font-weight: 500 !important;
    }
    h1, h2, h3, h4, h5, h6, [data-testid="stHeading"] * {
        font-family: 'Poppins', -apple-system, sans-serif !important;
        font-weight: 500 !important;
        color: var(--mw-text);
        letter-spacing: -0.2px;
    }
    h1 { font-size: 1.7rem !important; }
    h2 { font-size: 1.35rem !important; }
    h3 { font-size: 1.12rem !important; }
    h4, h5, h6 { font-size: 0.98rem !important; }

    /* ------------------------------ Fondo -------------------------------- */
    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(40% 30% at 100% 0%, rgba(60,200,180,0.07) 0%, transparent 70%),
            radial-gradient(35% 25% at 0% 100%, rgba(29,143,151,0.05) 0%, transparent 70%),
            var(--mw-bg);
    }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stDecoration"], [data-testid="stDeployButton"],
    .stDeployButton, [data-testid="stAppDeployButton"] { display: none !important; }

    /* Login centrado */
    .mw-login { text-align: center; margin: 9vh 0 22px 0; }
    .mw-login h1 {
        font-family: 'Poppins', -apple-system, sans-serif !important;
        font-weight: 500 !important;
        font-size: 1.9rem !important;
        margin: 6px 0 4px 0; padding: 0;
    }
    .mw-login p { color: var(--mw-muted); font-size: 0.88rem; margin: 0; }
    [data-testid="stForm"] {
        border: 1px solid var(--mw-border);
        border-radius: 12px;
        padding: 18px 18px 14px;
        background: #ffffff;
        box-shadow: 0 6px 24px rgba(11,36,51,0.05);
    }
    .block-container { padding-top: 1.4rem; padding-bottom: 3rem; }
    [data-testid="stAppViewContainer"] > .main .block-container { max-width: 1120px; }
    hr { border-color: var(--mw-border) !important; }

    /* ------------------------ Barra lateral (oscura) --------------------- */
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, var(--mw-navy-2) 0%, var(--mw-navy) 100%);
        border-right: 1px solid var(--mw-side-border);
    }
    [data-testid="stSidebar"] :is(p, span, label, li, h1, h2, h3, h4, h5, h6, summary, small) {
        color: var(--mw-side-text) !important;
    }
    [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
    [data-testid="stSidebar"] .stCaption, [data-testid="stSidebar"] small {
        color: var(--mw-side-muted) !important;
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] {
        background: rgba(255,255,255,0.03);
        border: 1px solid var(--mw-side-border);
    }
    [data-testid="stSidebar"] [data-testid="stExpander"] summary:hover * { color: #ffffff !important; }
    [data-testid="stSidebar"] :is(input, textarea),
    [data-testid="stSidebar"] [data-baseweb="select"] > div {
        background-color: rgba(255,255,255,0.06) !important;
        color: var(--mw-side-text) !important;
        border-color: var(--mw-side-border) !important;
    }
    [data-testid="stSidebar"] .stButton > button {
        background: transparent;
        color: var(--mw-side-text);
        border: 1px solid var(--mw-side-border) !important;
        padding: 0.35rem 0.75rem;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        border-color: var(--mw-accent-2) !important;
        color: #ffffff;
        background: rgba(60,200,180,0.08);
    }
    [data-testid="stSidebar"] hr { border-color: var(--mw-side-border) !important; }

    /* ------------------------------- Cabecera ---------------------------- */
    .mw-header {
        position: relative;
        padding: 6px 0 18px 0;
        margin-bottom: 22px;
        border-bottom: 1px solid var(--mw-border);
        animation: mwFadeUp .35s ease both;
    }
    .mw-header__tag {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--mw-accent);
        font-size: 0.72rem;
        font-weight: 500;
        letter-spacing: 0.4px;
        padding: 3px 10px;
        border-radius: 999px;
        background: var(--mw-accent-soft);
        margin-bottom: 10px;
    }
    .mw-header h1 {
        font-family: 'Poppins', -apple-system, sans-serif !important;
        color: var(--mw-text);
        font-size: 1.75rem !important;
        font-weight: 500 !important;
        margin: 0;
        padding: 0;
        line-height: 1.2;
    }
    .mw-header p {
        color: var(--mw-muted);
        font-size: 0.88rem;
        margin: 6px 0 0 0;
    }

    /* ------------------------------- Tarjetas ---------------------------- */
    .mw-card {
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 12px;
        padding: 16px 18px;
        margin-bottom: 12px;
        transition: border-color .15s ease, box-shadow .15s ease;
    }
    .mw-card:hover {
        border-color: var(--mw-border-strong);
        box-shadow: 0 4px 16px rgba(11,36,51,0.05);
    }
    .mw-card h3 {
        color: var(--mw-text);
        font-size: 0.98rem !important;
        margin: 0 0 8px 0;
    }

    .mw-badge {
        display: inline-block;
        background: var(--mw-accent-soft);
        color: var(--mw-accent-deep);
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.72rem;
        font-weight: 500;
    }

    /* ------------------------------ Estadisticas ------------------------- */
    .mw-stat {
        position: relative;
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 12px;
        padding: 14px 16px 12px;
        text-align: left;
        overflow: hidden;
        transition: border-color .15s ease;
    }
    .mw-stat::before {
        content: "";
        position: absolute;
        left: 0; top: 14px; bottom: 14px;
        width: 2px;
        border-radius: 2px;
        background: linear-gradient(180deg, var(--mw-accent), var(--mw-accent-2));
    }
    .mw-stat:hover { border-color: var(--mw-border-strong); }
    .mw-stat .valor {
        font-family: 'Poppins', -apple-system, sans-serif;
        font-size: 1.6rem;
        font-weight: 500;
        color: var(--mw-text);
        line-height: 1.1;
    }
    .mw-stat .etiqueta {
        font-size: 0.74rem;
        color: var(--mw-muted);
        margin-top: 4px;
    }

    /* -------------------------------- Botones ---------------------------- */
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
        border-radius: 8px !important;
        font-size: 0.85rem !important;
        font-weight: 500;
        padding: 0.35rem 0.9rem;
        min-height: 2.2rem;
        white-space: nowrap;
        border: 1px solid var(--mw-border-strong) !important;
        background: var(--mw-surface);
        color: var(--mw-text);
        box-shadow: none;
        transition: border-color .12s ease, background .12s ease, color .12s ease;
    }
    .stButton > button p, .stDownloadButton > button p, .stFormSubmitButton > button p {
        font-size: 0.85rem !important;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        border-color: var(--mw-accent) !important;
        color: var(--mw-accent-deep);
        background: var(--mw-accent-soft);
    }
    .stButton > button[kind="primary"], .stFormSubmitButton > button {
        background: var(--mw-accent-deep) !important;
        border-color: var(--mw-accent-deep) !important;
        color: #ffffff !important;
    }
    .stButton > button[kind="primary"] p, .stFormSubmitButton > button p { color: #ffffff !important; }
    .stButton > button[kind="primary"]:hover, .stFormSubmitButton > button:hover {
        background: var(--mw-accent) !important;
        border-color: var(--mw-accent) !important;
    }

    /* --------------------------------- Campos ---------------------------- */
    .stTextInput input, .stTextArea textarea, .stNumberInput input,
    .stDateInput input, .stTimeInput input {
        border-radius: 8px !important;
        font-size: 0.88rem !important;
        background-color: #ffffff !important;
    }
    [data-testid="stAppViewContainer"] .stTextInput [data-baseweb="input"],
    [data-testid="stAppViewContainer"] .stTextArea [data-baseweb="textarea"],
    [data-testid="stAppViewContainer"] .stNumberInput [data-baseweb="input"] {
        border-radius: 8px !important;
        border: 1px solid var(--mw-border-strong) !important;
        background-color: #ffffff !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {
        box-shadow: none !important;
    }
    [data-testid="stAppViewContainer"] [data-baseweb="input"]:focus-within,
    [data-testid="stAppViewContainer"] [data-baseweb="textarea"]:focus-within {
        border-color: var(--mw-accent) !important;
        box-shadow: 0 0 0 3px var(--mw-accent-soft) !important;
    }
    [data-testid="stAppViewContainer"] [data-baseweb="select"] > div {
        border-radius: 8px !important;
        border-color: var(--mw-border-strong) !important;
        background-color: #ffffff !important;
        font-size: 0.88rem;
    }
    [data-baseweb="tag"] {
        border-radius: 999px !important;
        background-color: var(--mw-accent-soft) !important;
    }
    [data-baseweb="tag"] span { color: var(--mw-accent-deep) !important; }

    /* Expanders */
    [data-testid="stExpander"] {
        border: 1px solid var(--mw-border);
        border-radius: 10px;
        overflow: hidden;
        background: var(--mw-surface);
    }
    [data-testid="stExpander"] summary { font-size: 0.88rem; }

    /* Pestanas: subrayado fino */
    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 18px;
        border-bottom: 1px solid var(--mw-border);
    }
    [data-testid="stTabs"] [data-baseweb="tab"] {
        padding: 6px 2px;
        height: auto;
        background: transparent;
    }
    [data-testid="stTabs"] [data-baseweb="tab"] p { font-size: 0.86rem; color: var(--mw-muted); }
    [data-testid="stTabs"] [aria-selected="true"] p { color: var(--mw-text) !important; font-weight: 500; }
    [data-testid="stTabs"] [data-baseweb="tab-highlight"] {
        background-color: var(--mw-accent);
        height: 2px;
    }
    [data-testid="stTabs"] [data-baseweb="tab-border"] { display: none; }

    /* Selector de categorias */
    [data-testid="stSegmentedControl"] button,
    [data-testid="stButtonGroup"] button {
        border-radius: 999px !important;
        font-size: 0.82rem !important;
        border-color: var(--mw-border) !important;
        background: #ffffff;
    }
    [data-testid="stSegmentedControl"] button p,
    [data-testid="stButtonGroup"] button p { font-size: 0.82rem !important; }
    [data-testid="stButtonGroup"] { gap: 6px; }
    [data-testid="stButtonGroup"] button[kind$="Active"],
    [data-testid="stSegmentedControl"] button[aria-checked="true"] {
        background: var(--mw-accent-soft) !important;
        border-color: var(--mw-accent) !important;
    }
    [data-testid="stButtonGroup"] button[kind$="Active"] p { color: var(--mw-accent-deep) !important; }

    /* Metricas nativas */
    [data-testid="stMetric"] {
        background: var(--mw-surface);
        border: 1px solid var(--mw-border);
        border-radius: 12px;
        padding: 12px 16px;
    }
    [data-testid="stMetricLabel"] p { font-size: 0.78rem !important; color: var(--mw-muted) !important; }
    [data-testid="stMetricValue"] {
        font-family: 'Poppins', -apple-system, sans-serif;
        font-size: 1.5rem !important;
        color: var(--mw-text);
    }

    /* Tablas, avisos y progreso */
    [data-testid="stDataFrame"] {
        border-radius: 10px;
        overflow: hidden;
        border: 1px solid var(--mw-border);
    }
    [data-testid="stAlert"] { border-radius: 10px; font-size: 0.88rem; }
    [data-testid="stProgress"] > div > div > div > div {
        background: linear-gradient(90deg, var(--mw-accent), var(--mw-accent-2)) !important;
    }
    [data-testid="stCaptionContainer"], .stCaption { color: var(--mw-muted) !important; font-size: 0.8rem; }

    a { color: var(--mw-accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code { color: var(--mw-accent-deep); background: var(--mw-surface-2); }

    @keyframes mwFadeUp {
        from { opacity: 0; transform: translateY(6px); }
        to   { opacity: 1; transform: translateY(0); }
    }

    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-thumb { background: #d4dee1; border-radius: 8px; }
    [data-testid="stSidebar"] ::-webkit-scrollbar-thumb { background: rgba(94,230,214,0.18); }
</style>
"""


def inyectar_css():
    st.markdown(CSS, unsafe_allow_html=True)


def cabecera(titulo: str, subtitulo: str = "Sistema de Comunicación Estratégica"):
    st.markdown(
        f"""
        <div class="mw-header">
            <span class="mw-header__tag">MWX · Plataforma de gestión</span>
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
