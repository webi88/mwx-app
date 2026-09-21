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
    
    variar_por_cuenta = False
    if tipo == "post":
        variar_por_cuenta = st.checkbox(
            "🔀 Variar texto por cuenta (una versión distinta por cada cuenta)",
            value=True,
            key="cal_variar_por_cuenta",
        )
        if variar_por_cuenta:
            st.caption(
                "Se programará una tarea por cuenta, cada una con su propia "
                "versión del texto (IA con fallback local)."
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
        
        from scheduler.manager import SchedulerManager
        manager = SchedulerManager()
        
        if tipo == "post" and variar_por_cuenta and len(cuenta_ids) > 1:
            if not contenido.strip():
                st.warning("Escribe el contenido base para generar las variaciones.")
                return

            # Registro de lenguaje y perfil de redaccion por ID (el texto de
            # cada cuenta respeta ambos).
            from core.models import Cuenta
            from core.perfiles import normalizar_perfil
            from core.registros import normalizar_tipo_cuenta

            with get_db_session() as db:
                cuentas_pool = (
                    db.query(Cuenta).filter(Cuenta.id.in_(cuenta_ids)).all()
                )
                registro_por_id = {
                    c.id: normalizar_tipo_cuenta(getattr(c, "tipo_cuenta", ""))
                    for c in cuentas_pool
                }
                perfil_por_id = {
                    c.id: normalizar_perfil(getattr(c, "perfil_personalidad", ""))
                    for c in cuentas_pool
                }
            registros = [registro_por_id.get(cid, "") for cid in cuenta_ids]
            perfiles = [perfil_por_id.get(cid, "") for cid in cuenta_ids]

            from web.operaciones._helpers import generar_pool_por_cuenta_seguro
            with st.spinner("🔀 Generando una versión distinta por cuenta..."):
                try:
                    pool, uso_fallback = generar_pool_por_cuenta_seguro(
                        contenido,
                        len(cuenta_ids),
                        registros=registros,
                        perfiles=perfiles,
                    )
                except TypeError:
                    # Version vieja del helper sin el kwarg 'perfiles'.
                    pool, uso_fallback = generar_pool_por_cuenta_seguro(
                        contenido, len(cuenta_ids), registros=registros
                    )
            if uso_fallback:
                st.info(
                    "La IA no estaba disponible: se usaron variaciones locales del texto."
                )
            
            programadas = 0
            for i, cuenta_id in enumerate(cuenta_ids):
                texto = pool[i] if i < len(pool) else contenido
                tarea = Tarea(
                    tipo=tipo,
                    plataforma=plataforma,
                    contenido=texto,
                    cuentas_ids=str([cuenta_id]),
                    fecha_hora=fecha_hora,
                    estado="pendiente",
                    creada_por=usuario.get("username"),
                )
                if manager.programar_tarea(tarea):
                    programadas += 1
            
            if programadas == len(cuenta_ids):
                st.success(
                    f"✅ {programadas} tareas programadas (una por cuenta, con texto "
                    f"distinto) para {fecha_hora.strftime('%d/%m/%Y %H:%M')}"
                )
            elif programadas:
                st.warning(
                    f"⚠️ Solo se programaron {programadas}/{len(cuenta_ids)} tareas. "
                    "Revisa la pestaña 'Pendientes'."
                )
            else:
                st.error("Error al programar las tareas.")
            return
        
        tarea = Tarea(
            tipo=tipo,
            plataforma=plataforma,
            contenido=contenido,
            cuentas_ids=str(cuenta_ids),
            fecha_hora=fecha_hora,
            estado="pendiente",
            creada_por=usuario.get("username"),
        )
        
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