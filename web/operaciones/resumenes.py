import streamlit as st
from datetime import datetime, timedelta
from core.database import get_db_session
from core.models import MencionDia, Cliente, ReporteDiario
from ia.celulas import CelulasManager
from web.ui import cabecera, stat, divider, empty_state


def render(usuario: dict):
    cabecera("📋 RESÚMENES EJECUTIVOS", "Resúmenes y mañaneras por cliente")
    
    tabs = st.tabs([
        "🕐 Hoy",
        "📅 Por Fecha",
        "📊 Reportes Guardados",
        "⚙️ Generar Resumen",
    ])
    
    with tabs[0]:
        _resumen_hoy()
    with tabs[1]:
        _por_fecha()
    with tabs[2]:
        _reportes_guardados()
    with tabs[3]:
        _generar_resumen()


def _seleccionar_cliente(key: str):
    manager = CelulasManager()
    clientes = manager.obtener_clientes()
    if not clientes:
        empty_state("No hay clientes.")
        return None
    opts = {c.nombre: c for c in clientes}
    sel = st.selectbox("Cliente", list(opts), key=key)
    return opts[sel]


def _resumen_hoy():
    st.markdown("### 🕐 Resumen de Hoy")
    
    hoy = datetime.now().date()
    
    with get_db_session() as db:
        total = db.query(MencionDia).filter(MencionDia.fecha == hoy).count()
        principales = db.query(MencionDia).filter(
            MencionDia.fecha == hoy, MencionDia.es_principal == True
        ).count()
        menciones = db.query(MencionDia).filter(MencionDia.fecha == hoy).all()
        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}
    
    c1, c2 = st.columns(2)
    with c1:
        stat(total, "Menciones hoy")
    with c2:
        stat(principales, "Principales")
    
    divider()
    
    if not menciones:
        empty_state("Sin menciones registradas hoy.")
        return
    
    for m in menciones:
        nombre = clientes_map.get(m.cliente_id, f"Cliente {m.cliente_id}")
        with st.container(border=True):
            titulo = f"⭐ **{m.titulo}**" if m.es_principal else f"**{m.titulo}**"
            st.markdown(titulo)
            st.caption(f"{nombre} · {m.fuente}")
            if m.resumen:
                st.markdown(m.resumen)
            if m.enlace:
                st.markdown(f"[Abrir enlace]({m.enlace})")


def _por_fecha():
    st.markdown("### 📅 Menciones por Fecha")
    
    fecha = st.date_input("Selecciona fecha", value=datetime.now().date(), key="men_fecha")
    
    with get_db_session() as db:
        menciones = db.query(MencionDia).filter(MencionDia.fecha == fecha).all()
        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}
    
    if not menciones:
        empty_state(f"Sin menciones para {fecha}.")
        return
    
    for m in menciones:
        nombre = clientes_map.get(m.cliente_id, f"Cliente {m.cliente_id}")
        with st.container(border=True):
            titulo = f"⭐ **{m.titulo}**" if m.es_principal else f"**{m.titulo}**"
            st.markdown(titulo)
            st.caption(f"{nombre} · {m.fuente}")
            if m.resumen:
                st.markdown(m.resumen)
            if m.enlace:
                st.markdown(f"[Abrir enlace]({m.enlace})")


def _reportes_guardados():
    st.markdown("### 📊 Reportes Guardados")
    
    with get_db_session() as db:
        reportes = db.query(ReporteDiario).order_by(ReporteDiario.fecha.desc()).limit(50).all()
        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}
    
    if not reportes:
        empty_state("No hay reportes guardados.")
        return
    
    for reporte in reportes:
        nombre = clientes_map.get(reporte.cliente_id, f"Cliente {reporte.cliente_id}")
        with st.expander(f"📊 {nombre} — {reporte.fecha} ({reporte.total_alertas} alertas)"):
            if reporte.por_fuente:
                st.markdown("**Por fuente:**")
                for fuente, cant in reporte.por_fuente.items():
                    st.markdown(f"- {fuente}: {cant}")
            if reporte.por_temas:
                st.markdown("**Por temas:**")
                for tema, cant in reporte.por_temas.items():
                    st.markdown(f"- {tema}: {cant}")


def _generar_resumen():
    st.markdown("### ⚙️ Generar Resumen")
    
    cliente = _seleccionar_cliente("res_cliente")
    if not cliente:
        return
    
    dias = st.slider("Días a cubrir", 1, 30, 7, key="res_dias")
    
    if st.button("📊 Generar Resumen", type="primary", key="btn_generar_resumen"):
        with st.spinner("Recopilando menciones y generando resumen con IA..."):
            try:
                from ia.filtros_alertas import FiltrosAlertas
                
                with get_db_session() as db:
                    menciones = db.query(MencionDia).filter(
                        MencionDia.cliente_id == cliente.id,
                        MencionDia.fecha >= datetime.now().date() - timedelta(days=dias)
                    ).all()
                
                if not menciones:
                    st.warning("No hay menciones en ese periodo.")
                    return
                
                textos = [m.titulo for m in menciones]
                resumen = FiltrosAlertas().resumir(textos)
                
                st.session_state["web_resumen_generado"] = {
                    "cliente": cliente.nombre,
                    "dias": dias,
                    "texto": resumen,
                }
            except Exception as e:
                st.error(f"Error: {e}")
        
        st.rerun()
    
    if "web_resumen_generado" in st.session_state:
        resumen = st.session_state["web_resumen_generado"]
        st.markdown(f"### Resumen de {resumen['cliente']} ({resumen['dias']} días)")
        st.markdown(resumen["texto"])