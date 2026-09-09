import streamlit as st
from datetime import datetime, timedelta
from core.database import get_db_session
from core.models import Tarea
from web.ui import cabecera


def render(usuario: dict):
    cabecera("📅 CALENDARIO: PROGRAMACIÓN", "Programación de tareas automáticas")
    
    tabs = st.tabs([
        "➕ Programar Tarea",
        "⏳ Pendientes",
        "📋 Historial",
    ])
    
    with tabs[0]:
        _programar()
    with tabs[1]:
        _pendientes()
    with tabs[2]:
        _historial()


def _programar():
    st.markdown("### ➕ Programar Tarea")
    
    tipos = ["post", "retweet", "like", "follow", "visualizacion", "respuesta"]
    tipo = st.selectbox("Tipo de tarea", tipos, key="cal_tipo")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "facebook", "instagram", "tiktok"],
        key="cal_plataforma",
    )
    
    fecha = st.date_input("Fecha", value=datetime.now().date() + timedelta(days=1), key="cal_fecha")
    hora = st.time_input("Hora", value=datetime.now().time().replace(hour=9, minute=0), key="cal_hora")
    
    contenido = st.text_area(
        "Contenido (si aplica)",
        height=80,
        key="cal_contenido",
    )
    
    cuenta_ids_text = st.text_input(
        "IDs de cuentas (separados por coma)",
        key="cal_cuentas_ids",
        placeholder="1,2,3",
    )
    
    if st.button("📅 Programar", type="primary", key="btn_cal_programar"):
        if not cuenta_ids_text:
            st.warning("Indica al menos un ID de cuenta.")
            return
        
        try:
            cuenta_ids = [int(x.strip()) for x in cuenta_ids_text.split(",")]
        except ValueError:
            st.error("Los IDs deben ser números.")
            return
        
        fecha_hora = datetime.combine(fecha, hora)
        
        tarea = Tarea(
            tipo=tipo,
            plataforma=plataforma,
            contenido=contenido,
            cuentas_ids=str(cuenta_ids),
            fecha_hora=fecha_hora,
            estado="pendiente",
            creada_por=usuario.get("username"),
        )
        
        from scheduler.manager import SchedulerManager
        manager = SchedulerManager()
        if manager.programar_tarea(tarea):
            st.success(f"✅ Tarea programada para {fecha_hora.strftime('%d/%m/%Y %H:%M')}")
        else:
            st.error("Error al programar la tarea.")


def _pendientes():
    st.markdown("### ⏳ Tareas Pendientes")
    
    with get_db_session() as db:
        tareas = db.query(Tarea).filter(
            Tarea.estado.in_(["pendiente", "ejecutando"])
        ).order_by(Tarea.fecha_hora).all()
    
    if not tareas:
        st.info("No hay tareas pendientes.")
        return
    
    for tarea in tareas:
        estado_emoji = "🔄" if tarea.estado == "ejecutando" else "⏳"
        with st.container(border=True):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.markdown(
                    f"{estado_emoji} **{tarea.tipo}** ({tarea.plataforma}) — "
                    f"ID {tarea.id}"
                )
            with col2:
                st.caption(f"⏰ {tarea.fecha_hora.strftime('%d/%m/%Y %H:%M')}")
            with col3:
                if tarea.estado == "pendiente" and st.button(
                    "❌ Cancelar", key=f"cal_cancel_{tarea.id}"
                ):
                    from scheduler.manager import SchedulerManager
                    if SchedulerManager().cancelar_tarea(tarea.id):
                        st.success("Tarea cancelada")
                        st.rerun()


def _historial():
    st.markdown("### 📋 Historial de Tareas")
    
    with get_db_session() as db:
        tareas = db.query(Tarea).filter(
            Tarea.estado.in_(["completada", "fallida", "cancelada"])
        ).order_by(Tarea.fecha_creacion.desc()).limit(50).all()
    
    if not tareas:
        st.info("Sin historial.")
        return
    
    for tarea in tareas:
        estado_emoji = {
            "completada": "✅", "fallida": "❌", "cancelada": "🚫"
        }.get(tarea.estado, "❓")
        with st.container(border=True):
            st.markdown(
                f"{estado_emoji} **{tarea.tipo}** ({tarea.plataforma}) — "
                f"{tarea.fecha_hora.strftime('%d/%m/%Y %H:%M')}"
            )
            if tarea.resultado:
                st.caption(tarea.resultado[:150])