import streamlit as st
from datetime import datetime, timedelta
from core.database import get_db_session
from core.models import Cliente, AlertaHistorial, MencionDia, ReporteDiario
from ia.celulas import CelulasManager
from web.ui import cabecera, stat, divider, empty_state


def render(usuario: dict):
    cabecera("🚨 SISTEMA DE ALERTAS", "Monitoreo y vigilancia por cliente")
    
    st.markdown("## Resumen General")
    
    with get_db_session() as db:
        total_alertas = db.query(AlertaHistorial).count()
        alertas_hoy = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= datetime.now().replace(hour=0, minute=0)
        ).count()
        alertas_7d = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= datetime.now() - timedelta(days=7)
        ).count()
        clientes_total = db.query(Cliente).filter(Cliente.activo == True).count()
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat(alertas_hoy, "Alertas hoy")
    with c2:
        stat(alertas_7d, "Últimos 7 días")
    with c3:
        stat(total_alertas, "Historial total")
    with c4:
        stat(clientes_total, "Clientes")
    
    divider()
    
    tabs = st.tabs([
        "🔔 Alertas Recientes",
        "👥 Por Cliente",
        "📰 Por Fuente",
        "📝 Menciones del Día",
        "📊 Resúmenes",
        "📋 Keywords por Cliente",
        "⚙️ Ejecutar Alertas",
    ])
    
    with tabs[0]:
        _alertas_recientes()
    with tabs[1]:
        _alertas_por_cliente()
    with tabs[2]:
        _alertas_por_fuente()
    with tabs[3]:
        _menciones_dia()
    with tabs[4]:
        _resumenes()
    with tabs[5]:
        _keywords_clientes()
    with tabs[6]:
        _ejecutar_alertas()


def _alertas_recientes():
    st.markdown("### 🔔 Alertas Recientes")
    
    with get_db_session() as db:
        alertas = db.query(AlertaHistorial).order_by(
            AlertaHistorial.fecha_envio.desc()
        ).limit(50).all()
        
        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}
    
    if not alertas:
        empty_state("No hay alertas registradas todavía.")
        return
    
    for alerta in alertas:
        cliente_nombre = clientes_map.get(alerta.cliente_id, f"Cliente {alerta.cliente_id}")
        fecha = alerta.fecha_envio.strftime("%d/%m/%Y %H:%M")
        with st.container(border=True):
            st.markdown(
                f"**{cliente_nombre}** · {alerta.fuente} · 🕐 {fecha}"
            )
            st.markdown(f"🔗 [Abrir fuente]({alerta.url})")
            st.caption(alerta.url[:120])


def _alertas_por_cliente():
    st.markdown("### 👥 Alertas por Cliente")
    
    with get_db_session() as db:
        clientes = db.query(Cliente).filter(Cliente.activo == True).all()
        if not clientes:
            empty_state("No hay clientes configurados. Créalos en el sidebar.")
            return
        
        alertas_map = {}
        for cliente in clientes:
            total = db.query(AlertaHistorial).filter(
                AlertaHistorial.cliente_id == cliente.id
            ).count()
            hoy = db.query(AlertaHistorial).filter(
                AlertaHistorial.cliente_id == cliente.id,
                AlertaHistorial.fecha_envio >= datetime.now().replace(hour=0, minute=0)
            ).count()
            alertas_map[cliente.id] = (total, hoy)
    
    for cliente in clientes:
        total, hoy = alertas_map.get(cliente.id, (0, 0))
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(f"**{cliente.nombre}**")
                st.caption(cliente.localidad or "Sin localidad")
            with c2:
                st.metric("Hoy", hoy)
            with c3:
                st.metric("Total", total)


def _alertas_por_fuente():
    st.markdown("### 📰 Alertas por Fuente")
    
    from sqlalchemy import func
    
    with get_db_session() as db:
        fuentes = db.query(AlertaHistorial.fuente, func.count(
            AlertaHistorial.id
        )).group_by(AlertaHistorial.fuente).all()
    
    if not fuentes:
        empty_state("Sin datos de fuentes.")
        return
    
    datos = {"fuentes": [], "cantidades": []}
    for fuente, cantidad in fuentes:
        datos["fuentes"].append(fuente or "desconocida")
        datos["cantidades"].append(cantidad)
    
    st.bar_chart({"Fuente": datos["fuentes"], "Cantidad": datos["cantidades"]},
                 x="Fuente", y="Cantidad")
    
    st.markdown("### Detalle por fuente")
    for fuente, cantidad in fuentes:
        st.markdown(f"**{fuente or 'desconocida'}**: {cantidad} alertas")


def _menciones_dia():
    st.markdown("### 📝 Menciones del Día")
    
    hoy = datetime.now().date()
    
    with get_db_session() as db:
        menciones = db.query(MencionDia).filter(
            MencionDia.fecha == hoy
        ).order_by(MencionDia.es_principal.desc()).all()
        
        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}
    
    if not menciones:
        empty_state("Sin menciones registradas hoy.")
        return
    
    for mencion in menciones:
        cliente_nombre = clientes_map.get(mencion.cliente_id, f"Cliente {mencion.cliente_id}")
        principal = "⭐" if mencion.es_principal else ""
        with st.container(border=True):
            st.markdown(f"{principal} **{mencion.titulo}** — {cliente_nombre} ({mencion.fuente})")
            if mencion.resumen:
                st.caption(mencion.resumen)
            if mencion.enlace:
                st.markdown(f"[Abrir enlace]({mencion.enlace})")


def _resumenes():
    st.markdown("### 📊 Resúmenes por Cliente")
    
    with get_db_session() as db:
        clientes = db.query(Cliente).filter(Cliente.activo == True).all()
        reportes_map = {}
        for cliente in clientes:
            reporte = db.query(ReporteDiario).filter(
                ReporteDiario.cliente_id == cliente.id
            ).order_by(ReporteDiario.fecha.desc()).first()
            if reporte:
                reportes_map[cliente.id] = reporte
    
    if not reportes_map:
        empty_state("No hay resúmenes generados todavía.")
        return
    
    for cliente in clientes:
        reporte = reportes_map.get(cliente.id)
        if not reporte:
            continue
        with st.expander(f"📊 {cliente.nombre} — {reporte.fecha}"):
            st.markdown(f"**Total alertas:** {reporte.total_alertas}")
            if reporte.por_fuente:
                st.markdown("**Por fuente:**")
                for fuente, cant in reporte.por_fuente.items():
                    st.markdown(f"  - {fuente}: {cant}")
            if reporte.por_temas:
                st.markdown("**Por temas:**")
                for tema, cant in reporte.por_temas.items():
                    st.markdown(f"  - {tema}: {cant}")


def _keywords_clientes():
    st.markdown("### 🔑 Keywords por Cliente")
    
    manager = CelulasManager()
    clientes = manager.obtener_clientes()
    
    if not clientes:
        empty_state("No hay clientes.")
        return
    
    for cliente in clientes:
        try:
            import json
            keywords = json.loads(cliente.keywords) if cliente.keywords else []
        except Exception:
            keywords = []
        
        with st.expander(f"🔍 **{cliente.nombre}** — {len(keywords)} keywords"):
            if keywords:
                st.markdown(", ".join(f"`{k}`" for k in keywords))
            else:
                st.caption("Sin keywords configuradas.")
            st.caption(f"Localidad: {cliente.localidad or '—'}")
            if cliente.exclude_terms:
                try:
                    excl = json.loads(cliente.exclude_terms)
                    if excl:
                        st.caption(f"Excluir: {', '.join(excl)}")
                except Exception:
                    pass


def _ejecutar_alertas():
    st.markdown("### ⚙️ Ejecutar Alertas")
    
    manager = CelulasManager()
    clientes = manager.obtener_clientes()
    
    if not clientes:
        empty_state("No hay clientes configurados.")
        return
    
    horas = st.slider("Ventana de búsqueda (horas)", 6, 168, 24, key="alertas_horas")
    
    cliente_opts = {"(Todos los clientes)": None}
    for c in clientes:
        cliente_opts[c.nombre] = c.id
    sel = st.selectbox("Cliente", list(cliente_opts), key="alertas_sel_cliente")
    
    if st.button("🚀 Ejecutar Alertas", type="primary", key="btn_ejecutar_alertas"):
        with st.spinner("Buscando en Google News y Twitter..."):
            from alertas.motor import MotorAlertas
            motor = MotorAlertas()
            resultados = motor.ejecutar_alertas(cliente_id=cliente_opts[sel], horas=horas)
        
        st.success("Búsqueda completada")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            stat(resultados["total"], "Menciones")
        with c2:
            stat(resultados["enviadas"], "Enviadas")
        with c3:
            stat(resultados["filtradas"], "Filtradas")
        with c4:
            stat(resultados["duplicadas"], "Duplicadas")
        st.rerun()