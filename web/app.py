import streamlit as st
import sys
import os

# Asegurar que la raiz del proyecto este en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import init_db
from web.auth import verificar_login_web, asegurar_admin_por_defecto
from web.ui import inyectar_css, cabecera
from web.sidebar import render_sidebar

# Configuración de la página
st.set_page_config(
    page_title="MWX.app - Dashboard",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

init_db()
asegurar_admin_por_defecto()
inyectar_css()


OPCIONES = [
    "📰 Posts / Mantenimientos",
    "⚡ RTS / Activaciones",
    "🎯 Activación Masiva",
    "🚨 Alertas",
    "👥 Grupos: Reciprocidad",
    "💬 Crisis: Respuestas",
    "👁️ Visualizaciones",
    "📋 Resúmenes Ejecutivos",
    "✍️ Change.org: Peticiones",
    "🌐 Blogs Web",
    "❤️ Influencia: Likes",
    "🚀 Impulso: Follows",
    "🚩 Reportar Posts",
    "📅 Calendario: Programación",
    "📊 Reportes",
    "📈 Monitor: Actividad",
    "🖼️ Multimedia",
]

OPCIONES_ADMIN = ["👑 Admin: Usuarios"]
OPCIONES_CREAR = ["🗂️ Cuentas: Importar & Validar"]


def _login():
    cabecera("MWX.app", "Acceso al Sistema de Comunicación Estratégica")
    st.markdown("#### Iniciar sesión")
    
    with st.form("login_form_web"):
        username = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        submitted = st.form_submit_button("Entrar", type="primary", use_container_width=True)
    
    if submitted:
        if not username or not password:
            st.error("Usuario y contraseña obligatorios")
            return
        usuario = verificar_login_web(username, password)
        if usuario:
            st.session_state["web_autenticado"] = True
            st.session_state["web_usuario"] = usuario
            st.rerun()
        else:
            st.error("Credenciales inválidas")


def main():
    if not st.session_state.get("web_autenticado"):
        _login()
        return
    
    usuario = st.session_state["web_usuario"]
    
    with st.sidebar:
        render_sidebar(usuario)
    
    opciones = OPCIONES.copy()
    if usuario["rol"] == "admin":
        opciones += OPCIONES_ADMIN + OPCIONES_CREAR
    
    index_default = opciones.index("🚨 Alertas")
    seleccion = st.selectbox(
        "📍 SELECCIONA LA OPERACIÓN:",
        opciones,
        index=index_default,
        key="web_nav_selector",
    )
    
    st.markdown("---")
    
    if seleccion.startswith("📰"):
        from web.operaciones.posts import render
        render(usuario)
    elif seleccion.startswith("⚡"):
        from web.operaciones.rts import render
        render(usuario)
    elif seleccion.startswith("🎯"):
        from web.operaciones.activacion_masiva import render
        render(usuario)
    elif seleccion.startswith("🚨"):
        from web.operaciones.alertas import render
        render(usuario)
    elif seleccion.startswith("👥"):
        from web.operaciones.grupos import render
        render(usuario)
    elif seleccion.startswith("💬"):
        from web.operaciones.crisis import render
        render(usuario)
    elif seleccion.startswith("👁️"):
        from web.operaciones.visualizaciones import render
        render(usuario)
    elif seleccion.startswith("📋"):
        from web.operaciones.resumenes import render
        render(usuario)
    elif seleccion.startswith("✍️"):
        from web.operaciones.change import render
        render(usuario)
    elif seleccion.startswith("🌐"):
        from web.operaciones.blogs import render
        render(usuario)
    elif seleccion.startswith("❤️"):
        from web.operaciones.likes import render
        render(usuario)
    elif seleccion.startswith("🚀"):
        from web.operaciones.follows import render
        render(usuario)
    elif seleccion.startswith("🚩"):
        from web.operaciones.reportar import render
        render(usuario)
    elif seleccion.startswith("📅"):
        from web.operaciones.calendario import render
        render(usuario)
    elif seleccion.startswith("📊"):
        from web.operaciones.reportes import render
        render(usuario)
    elif seleccion.startswith("📈"):
        from web.operaciones.monitor import render
        render(usuario)
    elif seleccion.startswith("🖼️"):
        from web.operaciones.multimedia import render
        render(usuario)
    elif seleccion.startswith("👑"):
        from web.operaciones.admin import render
        render(usuario)
    elif seleccion.startswith("🗂️"):
        from web.operaciones.cuentas import render
        render(usuario)


main()