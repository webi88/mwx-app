import streamlit as st
from web.auth import (
    listar_usuarios_web, crear_usuario_web, actualizar_usuario_web,
    eliminar_usuario_web, es_admin_web,
)
from web.ui import cabecera


def render(usuario: dict):
    if not es_admin_web(usuario["rol"]):
        st.error("Acceso restringido. Solo administradores.")
        return
    
    cabecera("👑 ADMIN", "Gestión del sistema")
    
    tabs = st.tabs([
        "👥 Usuarios",
        "➕ Crear Usuario",
        "👤 Editar Usuario",
        "📊 Exportar Cuentas",
    ])
    
    with tabs[0]:
        _listar()
    with tabs[1]:
        _crear()
    with tabs[2]:
        _editar()
    with tabs[3]:
        _exportar_cuentas()


def _listar():
    st.markdown("### 👥 Usuarios del Sistema")
    
    usuarios = listar_usuarios_web()
    if not usuarios:
        st.info("No hay usuarios.")
        return
    
    for u in usuarios:
        rol_emoji = "👑" if u["role"] == "admin" else "👤"
        status_emoji = "🟢" if u.get("activo", True) else "🔴"
        with st.expander(
            f"{status_emoji} {rol_emoji} **{u['username']}** — {u.get('nombre', u['username'])} "
            f"({u['role']})"
        ):
            st.caption(f"Creado: {u.get('creado', '?')}")
            if st.button(
                "🗑️ Eliminar", key=f"admin_del_{u['username']}"
            ):
                if eliminar_usuario_web(u["username"]):
                    st.success("Usuario eliminado")
                    st.rerun()


def _crear():
    st.markdown("### ➕ Crear Usuario")
    
    with st.form("form_crear_usuario_web"):
        username = st.text_input("Nombre de usuario")
        nombre = st.text_input("Nombre completo")
        password = st.text_input("Contraseña", type="password")
        rol = st.selectbox("Rol", ["operador", "admin"])
        submitted = st.form_submit_button("Crear Usuario", type="primary")
    
    if submitted:
        if not username or not password:
            st.warning("Usuario y contraseña son obligatorios.")
        elif crear_usuario_web(username, password, rol, nombre):
            st.success(f"Usuario '{username}' creado")
            st.rerun()
        else:
            st.error("Ese usuario ya existe.")


def _editar():
    st.markdown("### 👤 Editar Usuario")
    
    usuarios = listar_usuarios_web()
    if not usuarios:
        st.info("No hay usuarios.")
        return
    
    opts = {f"{u['username']} ({u['role']})": u["username"] for u in usuarios}
    sel = st.selectbox("Selecciona usuario", list(opts), key="admin_edit_sel")
    username = opts[sel]
    
    usuario = next(u for u in usuarios if u["username"] == username)
    
    col1, col2 = st.columns(2)
    with col1:
        nuevo_nombre = st.text_input("Nombre", value=usuario.get("nombre", username), key="admin_edit_nombre")
    with col2:
        nuevo_rol = st.selectbox(
            "Rol", ["operador", "admin"],
            index=0 if usuario["role"] == "operador" else 1,
            key="admin_edit_rol",
        )
    
    nueva_password = st.text_input("Nueva contraseña (dejar vacío para no cambiar)",
                                   type="password", key="admin_edit_pass")
    
    activo = st.checkbox("Activo", value=usuario.get("activo", True), key="admin_edit_activo")
    
    if st.button("💾 Guardar Cambios", type="primary", key="btn_admin_edit"):
        campos = {
            "nombre": nuevo_nombre,
            "rol": nuevo_rol,
            "activo": activo,
        }
        if nueva_password:
            campos["password"] = nueva_password
        
        if actualizar_usuario_web(username, **campos):
            st.success("Cambios guardados")
            st.rerun()


def _exportar_cuentas():
    st.markdown("### 📊 Exportar Cuentas a Excel")
    st.caption(
        "Muestra la base de datos de cuentas y genera un archivo .xlsx con solo "
        "el usuario y la contraseña para poder iniciar sesión después."
    )

    # Tabla con la base de datos de cuentas (vista previa antes de exportar).
    from core.database import get_db_session
    from core.models import Cuenta

    with get_db_session() as db:
        cuentas = db.query(Cuenta).order_by(Cuenta.fecha_creacion.desc()).all()

    if not cuentas:
        st.info("No hay cuentas registradas en la base de datos.")
    else:
        filas = []
        for c in cuentas:
            filas.append(
                {
                    "Usuario": c.usuario or "",
                    "Contraseña": c.password or "",
                    "Plataforma": c.plataforma or "",
                    "Email": c.email or "",
                    "Teléfono": c.phone or "",
                    "Grupo": c.grupo or "",
                    "Sector": c.sector or "",
                    "País": c.pais or "",
                    "Activa": "Sí" if c.activa else "No",
                }
            )
        st.dataframe(filas, use_container_width=True)
        st.caption(f"Total: {len(cuentas)} cuentas.")

    st.markdown("---")
    solo_activas = st.checkbox("Solo cuentas activas", key="exportar_solo_activas")

    if st.button("Generar Excel", type="primary", key="btn_exportar_cuentas"):
        from core.exportar import exportar_cuentas_excel

        res = exportar_cuentas_excel(solo_activas=solo_activas)
        if res.get("error"):
            st.error(res["error"])
        else:
            st.success(f"Se exportaron {res['total']} cuentas (usuario y contraseña).")
            import os
            with open(res["ruta"], "rb") as f:
                data = f.read()
            st.download_button(
                "⬇️ Descargar Excel",
                data=data,
                file_name=os.path.basename(res["ruta"]),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_exportar_cuentas",
            )