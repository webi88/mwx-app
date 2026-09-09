import streamlit as st
from datetime import datetime
from core.database import get_db_session
from core.models import Cuenta, Celula, Cliente, Tarea, AlertaHistorial
from core.registro import obtener_acciones
from web.ui import cabecera, stat, divider


def render(usuario: dict):
    cabecera("📊 REPORTES", "Estadísticas generales del sistema")
    
    st.markdown("## Resumen General")
    
    with get_db_session() as db:
        total_cuentas = db.query(Cuenta).count()
        activas = db.query(Cuenta).filter(Cuenta.activa == True).count()
        celulas = db.query(Celula).filter(Celula.activa == True).count()
        clientes = db.query(Cliente).filter(Cliente.activo == True).count()
        tareas_total = db.query(Tarea).count()
        tareas_pend = db.query(Tarea).filter(Tarea.estado == "pendiente").count()
        alertas = db.query(AlertaHistorial).count()
        alertas_hoy = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= datetime.now().replace(hour=0, minute=0)
        ).count()
        posts_hoy = db.query(Tarea).filter(
            Tarea.tipo == "post",
            Tarea.estado == "completada",
            Tarea.fecha_hora >= datetime.now().replace(hour=0, minute=0)
        ).count()
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat(f"{activas}/{total_cuentas}", "Cuentas activas")
    with c2:
        stat(celulas, "Células")
    with c3:
        stat(clientes, "Clientes")
    with c4:
        stat(tareas_pend, "Tareas pendientes")
    
    divider()
    
    c1, c2, c3 = st.columns(3)
    with c1:
        stat(posts_hoy, "Posts hoy")
    with c2:
        stat(alertas_hoy, "Alertas hoy")
    with c3:
        stat(alertas, "Alertas totales")
    
    divider()
    
    st.markdown("## Cuentas por Plataforma")
    
    with get_db_session() as db:
        por_plataforma = {}
        for c in db.query(Cuenta).all():
            por_plataforma.setdefault(c.plataforma, {"total": 0, "activas": 0})
            por_plataforma[c.plataforma]["total"] += 1
            if c.activa:
                por_plataforma[c.plataforma]["activas"] += 1
    
    if por_plataforma:
        for plat, datos in por_plataforma.items():
            st.markdown(f"**{plat}**: {datos['activas']}/{datos['total']} activas")
            st.progress(datos["activas"] / datos["total"] if datos["total"] else 0)

    divider()

    st.markdown("## Historial de acciones (mantenimientos / activaciones)")
    acciones = obtener_acciones(limit=50)
    if acciones:
        filas = []
        for a in acciones:
            filas.append({
                "Fecha": a.fecha.strftime("%Y-%m-%d %H:%M") if a.fecha else "",
                "Usuario": a.usuario,
                "Tipo": a.tipo,
                "Estado": a.estado,
                "URL publicación": a.url_publicacion or "",
            })
        st.dataframe(filas, use_container_width=True)
        # además muestra los enlaces clicables de las acciones exitosas con URL
        for a in acciones:
            if a.url_publicacion:
                st.markdown(f"🔗 @{a.usuario} — [{a.tipo}] [ver post]({a.url_publicacion})")
    else:
        st.info("Aún no hay acciones registradas.")