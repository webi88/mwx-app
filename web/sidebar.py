import streamlit as st
from core.database import get_db_session
from core.models import Cuenta, Cliente
from ia.celulas import CelulasManager
from web.ui import emoji_plataforma, divider


def render_sidebar(usuario: dict):
    rol_emoji = "👑" if usuario["rol"] == "admin" else "👤"
    
    col_user, col_logout = st.columns([3, 1])
    with col_user:
        st.markdown(f"{rol_emoji} **{usuario['nombre']}** `({usuario['rol']})`")
    with col_logout:
        if st.button("🚪 Salir", key="btn_logout_web"):
            for key in ["web_autenticado", "web_usuario"]:
                if key in st.session_state:
                    del st.session_state[key]
            st.rerun()
    
    divider()
    st.markdown("### ≡ PANEL DE CONTROL")
    
    _seccion_gestion_clientes(usuario)
    _seccion_asistente_ia(usuario)
    _seccion_multimedia(usuario)
    _seccion_inventario(usuario)


def _seccion_gestion_clientes(usuario: dict):
    with st.expander("👥 GESTIÓN DE CLIENTES", expanded=False):
        manager = CelulasManager()
        celulas = manager.obtener_celulas()
        
        if not celulas:
            st.caption("No hay células. Crea la primera:")
            nombre = st.text_input("Nombre célula", key="sb_cel_nombre")
            narrativa = st.text_area("Narrativa", key="sb_cel_narrativa", height=70)
            if st.button("➕ Crear Célula", key="sb_btn_crear_celula"):
                if nombre:
                    if manager.crear_celula(nombre, narrativa):
                        st.success(f"Célula '{nombre}' creada")
                        st.rerun()
                    else:
                        st.error("Ya existe una célula con ese nombre")
                else:
                    st.warning("El nombre es obligatorio")
        
        else:
            st.markdown("**🗂️ Ver Células**")
            for celula in celulas:
                with get_db_session() as db:
                    n_clientes = db.query(Cliente).filter(
                        Cliente.celula_id == celula.id
                    ).count()
                st.markdown(
                    f"🟢 **{celula.nombre}** (ID: {celula.id}) · {n_clientes} clientes"
                )
                if st.checkbox("Ver narrativa", key=f"sb_ver_narrativa_{celula.id}"):
                    st.caption(celula.narrativa)
                if usuario["rol"] == "admin":
                    if st.button(f"🗑️ Eliminar {celula.nombre}", key=f"del_cel_{celula.id}"):
                        manager.eliminar_celula(celula.id)
                        st.rerun()

            st.divider()
            st.markdown("**➕ Crear Cliente**")
            celula_opts = {c.nombre: c.id for c in celulas}
            sel_celula = st.selectbox("Célula", list(celula_opts), key="sb_cli_celula")
            nombre_cli = st.text_input("Nombre cliente", key="sb_cli_nombre")
            if st.button("➕ Crear Cliente", key="sb_btn_crear_cliente"):
                if nombre_cli:
                    if manager.crear_cliente(nombre_cli, celula_opts[sel_celula]):
                        st.success(f"Cliente '{nombre_cli}' creado")
                        st.rerun()

            st.divider()
            st.markdown("**🤖 Entrenar Cliente**")
            clientes = manager.obtener_clientes()
            if clientes:
                cli_opts = {c.nombre: c.id for c in clientes}
                sel_cli = st.selectbox("Cliente", list(cli_opts), key="sb_ent_cliente")
                entrenamiento = st.text_area(
                    "Instrucciones", height=90, key="sb_ent_texto"
                )
                if st.button("💾 Guardar Entrenamiento", key="sb_btn_entrenar"):
                    if manager.entrenar_cliente(cli_opts[sel_cli], entrenamiento):
                        st.success("Entrenamiento guardado")
                        st.rerun()
            else:
                st.caption("No hay clientes aún")


def _seccion_asistente_ia(usuario: dict):
    with st.expander("◈ ASISTENTE IA", expanded=False):
        st.caption("Generar contenido con OpenAI")
        
        tipos = [
            "Mantenimiento Diario", "Activación Digital", "Narrativa",
            "Reposteo con Cita", "Blog", "Verificado Ambiental", "Harfuch"
        ]
        tipo_map = {
            "Mantenimiento Diario": "mantenimiento",
            "Activación Digital": "activacion",
            "Narrativa": "narrativa",
            "Reposteo con Cita": "reposteo",
            "Blog": "blog",
            "Verificado Ambiental": "verificado",
            "Harfuch": "harfuch",
        }
        st.selectbox("Formato:", tipos, key="sb_ia_formato")
        
        celulas = CelulasManager().obtener_celulas()
        if celulas:
            cel_opts = {c.nombre: c.id for c in celulas}
            sel_cel_nombre = st.selectbox("Célula", list(cel_opts), key="sb_ia_celula")
            sel_cel_id = cel_opts[sel_cel_nombre]
        else:
            sel_cel_id = None
        
        clientes = CelulasManager().obtener_clientes()
        if clientes:
            cli_opts = {"(Sin cliente)": None, **{c.nombre: c.id for c in clientes}}
            sel_cli_nombre = st.selectbox("Cliente", list(cli_opts), key="sb_ia_cliente")
            sel_cli_id = cli_opts[sel_cli_nombre]
        else:
            sel_cli_id = None
        
        contexto = st.text_area("Contexto / copy adicional", height=80, key="sb_ia_contexto")
        cantidad = st.number_input("Cantidad", 1, 20, 5, key="sb_ia_cant")
        
        if st.button("✨ Generar Contenido", key="sb_btn_ia"):
            st.session_state["web_generar_contenido"] = {
                "tipo": tipo_map[st.session_state["sb_ia_formato"]],
                "celula_id": sel_cel_id,
                "cliente_id": sel_cli_id,
                "contexto": contexto,
                "cantidad": int(cantidad),
            }
            st.success("Generación programada. Ve a la operación 'Posts / Mantenimientos'.")


def _seccion_multimedia(usuario: dict):
    with st.expander("◧ MULTIMEDIA", expanded=False):
        st.caption("🎨 Generar Imagen con IA")
        prompt_img = st.text_area(
            "Prompt de la imagen", height=80,
            key="sb_img_prompt",
            placeholder="Describe la imagen a generar..."
        )
        if st.button("🎨 Generar Imagen", key="sb_btn_imagen"):
            st.session_state["web_generar_imagen"] = prompt_img
            st.success("Imagen programada. Revisa la sección Multimedia de la página principal.")


def _seccion_inventario(usuario: dict):
    with st.expander("▤ INVENTARIO", expanded=False):
        with get_db_session() as db:
            cuentas = db.query(Cuenta).all()
        
        if not cuentas:
            st.caption("No hay cuentas registradas. Usa la operación 'Cuentas: Importar & Validar'.")
            return
        
        plataformas = {}
        for c in cuentas:
            plataformas.setdefault(c.plataforma, []).append(c)
        
        for plat, lista in plataformas.items():
            activas = sum(1 for c in lista if c.activa)
            st.markdown(f"{emoji_plataforma(plat)} **{plat.upper()}** ({activas}/{len(lista)} activas)")
            for c in lista:
                estado = "🟢" if c.activa else "🔴"
                st.caption(f"{estado} @{c.usuario} · Grupo {c.grupo}")