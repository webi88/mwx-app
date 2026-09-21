import streamlit as st
import sys
import os

# Asegurar que la raiz del proyecto este en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import init_db
from web.auth import (
    asegurar_admin_por_defecto,
    crear_token_sesion,
    verificar_login_web,
    verificar_token_sesion,
)
from web.ui import inyectar_css, cabecera
from web.sidebar import render_sidebar

# Configuración de la página
st.set_page_config(
    page_title="MWX.app - Dashboard",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource(show_spinner=False)
def _init_app():
    """Inicializa la base de datos y el admin por defecto UNA sola vez por
    proceso (cache_resource). Antes corria en cada rerun/interaccion y con
    PostgreSQL/Supabase agregaba latencia a todas las paginas."""
    init_db()
    asegurar_admin_por_defecto()


_init_app()
inyectar_css()


OPCIONES = [
    "📰 Posts / Mantenimientos",
    "⏰ Reparto por Hora",
    "📅 Calendario: Programación",
    "🖼️ Multimedia",
    "⚡ RTS / Activaciones",
    "🎯 Activación Masiva",
    "❤️ Influencia: Likes",
    # OCULTO (sin soporte Twitter): reactivable borrando el '#'.
    # "🚀 Impulso: Follows",
    "🚩 Reportar Posts",
    # OCULTO (sin soporte Twitter): reactivable borrando el '#'.
    # "👁️ Visualizaciones",
    "🚨 Alertas",
    "💬 Crisis: Respuestas",
    "👥 Grupos: Reciprocidad",
    "📋 Resúmenes Ejecutivos",
    # OCULTO (sin backend real de blogs): reactivable borrando el '#'.
    # "🌐 Blogs Web",
    "✍️ Change.org: Peticiones",
    "📊 Reportes",
    "📈 Monitor: Actividad",
]

OPCIONES_ADMIN = ["👑 Admin: Usuarios"]
OPCIONES_CREAR = ["🗂️ Cuentas: Perfiles, Secciones & Nombres"]

# Navegación en 2 niveles (categoría -> operación): evita mostrar ~20 botones
# a la vez. Cada operación aparece en UNA sola categoría y las categorías sin
# operaciones visibles para el rol (p.ej. un operador no ve Cuentas/Admin) se
# ocultan. `?op=` sigue funcionando: si la URL trae una operación se
# preselecciona su categoría.
CATEGORIAS = [
    (
        "✍️ Publicar y programar",
        [
            "📰 Posts / Mantenimientos",
            "⏰ Reparto por Hora",
            "📅 Calendario: Programación",
            "🖼️ Multimedia",
        ],
    ),
    (
        "⚡ Automatización Twitter",
        [
            "⚡ RTS / Activaciones",
            "🎯 Activación Masiva",
            "❤️ Influencia: Likes",
            # OCULTO (sin soporte Twitter en TwitterBot): reactivable borrando
            # el '#' de la línea correspondiente en OPCIONES y aquí.
            # "🚀 Impulso: Follows",
            "🚩 Reportar Posts",
            # OCULTO (sin soporte Twitter en TwitterBot): reactivable borrando
            # el '#' de la línea correspondiente en OPCIONES y aquí.
            # "👁️ Visualizaciones",
        ],
    ),
    (
        "📡 Monitoreo y respuesta",
        [
            "🚨 Alertas",
            "💬 Crisis: Respuestas",
            "👥 Grupos: Reciprocidad",
            "📋 Resúmenes Ejecutivos",
            # OCULTO (blogs.py no tiene backend real): reactivable borrando el
            # '#' de la línea correspondiente en OPCIONES y aquí.
            # "🌐 Blogs Web",
            "✍️ Change.org: Peticiones",
        ],
    ),
    (
        "📊 Datos y cuentas",
        [
            "📊 Reportes",
            "📈 Monitor: Actividad",
            "🗂️ Cuentas: Perfiles, Secciones & Nombres",
            "👑 Admin: Usuarios",
        ],
    ),
]

# Operación que se abre al entrar sin `?op=` (landing histórica del dashboard).
OPERACION_POR_DEFECTO = "🚨 Alertas"


def _leer_query_param(nombre: str):
    """Devuelve el valor del query param `nombre` (o None)."""
    try:
        valor = st.query_params.get(nombre)
    except Exception:
        return None
    if isinstance(valor, (list, tuple)):
        valor = valor[0] if valor else None
    return valor or None


def _leer_token_query_params():
    """Devuelve el token de sesion del query param `s` (o None)."""
    return _leer_query_param("s")


def _op_desde_query_param(opciones: list) -> str | None:
    """Restaura la operación seleccionada desde `?op=`.

    Acepta coincidencia exacta o por prefijo (tolerante a URLs viejas).
    Devuelve None si no hay `?op=` o no es válido para este rol (ej. un
    operador abriendo un link de una operación de admin)."""
    crudo = _leer_query_param("op")
    if not crudo:
        return None
    crudo = str(crudo)
    if crudo in opciones:
        return crudo
    for op in opciones:
        if op.startswith(crudo) or crudo.startswith(op):
            return op
    return None


def _categorias_para(rol: str) -> list:
    """[ (categoria, [operaciones]) ] visibles para el rol.

    Un operador no ve las operaciones de admin (Cuentas/Admin): la categoría
    "📊 Datos y cuentas" sigue visible con Reportes y Monitor. Las categorías
    sin ninguna operación visible se omiten.
    """
    visibles = set(OPCIONES)
    if rol == "admin":
        visibles.update(OPCIONES_ADMIN + OPCIONES_CREAR)
    categorias = []
    for nombre, operaciones in CATEGORIAS:
        filtradas = [op for op in operaciones if op in visibles]
        if filtradas:
            categorias.append((nombre, filtradas))
    return categorias


def _categoria_de(operacion: str, categorias: list) -> str:
    """Categoría que contiene `operacion` (la primera si no está en ninguna)."""
    for nombre, operaciones in categorias:
        if operacion in operaciones:
            return nombre
    return categorias[0][0]


def _sincronizar_op_en_url(seleccion: str):
    """Escribe `?op=` en la URL sin disparar rerun (actualizar query params
    no re-ejecuta el script). Así una recarga del navegador vuelve a la
    misma operación en vez de caer a Alertas por defecto."""
    try:
        if _leer_query_param("op") != seleccion:
            st.query_params["op"] = seleccion
    except Exception:
        pass


def _on_cambio_operacion():
    """Callback del selectbox: persiste la operación en URL + session_state
    y limpia `?tab=` al salir de Cuentas (la pestaña solo aplica ahí)."""
    try:
        seleccion = st.session_state.get("web_nav_selector")
        if not seleccion:
            return
        st.session_state["operacion_actual"] = seleccion
        st.query_params["op"] = seleccion
        if not str(seleccion).startswith("🗂️") and "tab" in st.query_params:
            del st.query_params["tab"]
    except Exception:
        pass


def _guardar_token_sesion(usuario: dict):
    """Escribe el token firmado en el query param `s` para que una recarga
    del navegador o una reconexion del WebSocket no boten al login."""
    try:
        token = crear_token_sesion(usuario)
        if token:
            st.query_params["s"] = token
    except Exception:
        pass


def _recuperar_sesion() -> bool:
    """Reautentica desde el token del query param. True si lo logro."""
    token = _leer_token_query_params()
    if not token:
        return False
    usuario = verificar_token_sesion(token)
    if not usuario:
        return False
    st.session_state["web_autenticado"] = True
    st.session_state["web_usuario"] = usuario
    return True


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
            _guardar_token_sesion(usuario)
            st.rerun()
        else:
            st.error("Credenciales inválidas")


def main():
    if not st.session_state.get("web_autenticado"):
        if _recuperar_sesion():
            st.rerun()
        _login()
        return
    
    usuario = st.session_state["web_usuario"]

    # Si ya esta autenticado y no hay token en la URL, generarlo para que
    # una recarga del navegador no bote la sesion.
    if not _leer_token_query_params():
        _guardar_token_sesion(usuario)
    
    with st.sidebar:
        render_sidebar(usuario)
    
    categorias = _categorias_para(usuario["rol"])
    mapa_categorias = dict(categorias)
    opciones = [op for _, ops in categorias for op in ops]
    default_op = (
        OPERACION_POR_DEFECTO if OPERACION_POR_DEFECTO in opciones else opciones[0]
    )

    def _categoria_de_op(op: str) -> str:
        return _categoria_de(op, categorias)

    # --- Navegación persistente (?op= + session_state) ---
    # Recarga del navegador -> session_state vacío -> se restaura desde ?op=.
    # st.rerun() normal -> los selectboxes conservan su valor y ?op= solo se
    # re-sincroniza. Nunca se vuelve a Alertas salvo que no haya ni estado
    # ni query param válido.
    if "web_nav_selector" not in st.session_state:
        st.session_state["web_nav_selector"] = (
            _op_desde_query_param(opciones) or default_op
        )
    # Sesiones viejas o cambio de rol: si la operación guardada ya no es
    # visible (p.ej. un operador con la operación Cuentas de un admin) se cae
    # a la operación por defecto.
    if st.session_state["web_nav_selector"] not in opciones:
        st.session_state["web_nav_selector"] = default_op
    # La categoría se deriva de la operación activa; el selectbox de categoría
    # la actualiza al cambiarla.
    if st.session_state.get("web_nav_categoria") not in mapa_categorias:
        st.session_state["web_nav_categoria"] = _categoria_de_op(
            st.session_state["web_nav_selector"]
        )

    col_categoria, col_operacion = st.columns([2, 3])
    with col_categoria:
        categoria = st.selectbox(
            "📂 CATEGORÍA:",
            list(mapa_categorias),
            key="web_nav_categoria",
        )
    opciones_categoria = mapa_categorias.get(categoria) or opciones
    if st.session_state.get("web_nav_selector") not in opciones_categoria:
        # Cambió la categoría: abrir la primera operación de la nueva. Se
        # escribe ANTES de instanciar el selectbox de operación, así el widget
        # la toma como valor válido (sin "value not in options").
        st.session_state["web_nav_selector"] = opciones_categoria[0]
    with col_operacion:
        seleccion = st.selectbox(
            "📍 SELECCIONA LA OPERACIÓN:",
            opciones_categoria,
            key="web_nav_selector",
            on_change=_on_cambio_operacion,
        )
    st.session_state["operacion_actual"] = seleccion
    _sincronizar_op_en_url(seleccion)

    if not seleccion.startswith("🗂️"):
        # La pestaña ?tab= solo aplica a Cuentas: limpiar restos viejos.
        try:
            if "tab" in st.query_params:
                del st.query_params["tab"]
        except Exception:
            pass
    
    st.markdown("---")
    
    if seleccion.startswith("📰"):
        from web.operaciones.posts import render
        render(usuario)
    elif seleccion.startswith("⏰"):
        from web.operaciones.reparto_hora import render
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
        # Pre-carga la pestaña interna desde ?tab= ANTES del render para
        # que una recarga no caiga a "Importar" (ver cuentas.py).
        try:
            from web.operaciones.cuentas import TABS as CUENTAS_TABS
            tab_qp = _leer_query_param("tab")
            if (
                tab_qp
                and "cuentas_pestana_selector" not in st.session_state
                and str(tab_qp) in CUENTAS_TABS
            ):
                st.session_state["cuentas_pestana_selector"] = str(tab_qp)
        except Exception:
            pass
        from web.operaciones.cuentas import render
        render(usuario)


main()