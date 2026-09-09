import streamlit as st
import json
import os
from datetime import datetime
from core.config import resolver_ruta
from core.database import get_db_session
from core.models import Cuenta
from web.ui import cabecera, stat, divider


def render(usuario: dict):
    cabecera("📈 MONITOR: ACTIVIDAD", "Conteo de tweets y retweets por cuenta")
    
    tabs = st.tabs([
        "🔍 Revisar Actividad",
        "📊 Estadísticas Guardadas",
    ])
    
    with tabs[0]:
        _revisar()
    with tabs[1]:
        _estadisticas()


def _revisar():
    st.markdown("### 🔍 Revisar Actividad de Cuentas")
    
    horas = st.slider("Ventana de tiempo (horas)", 6, 168, 24, key="mon_horas")
    
    with get_db_session() as db:
        cuentas_twitter = db.query(Cuenta).filter(
            Cuenta.plataforma == "twitter", Cuenta.activa == True
        ).all()
    
    if not cuentas_twitter:
        st.warning("No hay cuentas de Twitter activas.")
        return
    
    cuenta_opts = {f"@{c.usuario}": c for c in cuentas_twitter}
    seleccionadas = st.multiselect("Cuentas a monitorear", list(cuenta_opts), key="mon_cuentas")
    
    if st.button("📈 Iniciar Monitoreo", type="primary", key="btn_mon"):
        if not seleccionadas:
            st.warning("Selecciona cuentas.")
            return
        
        cuentas_filtro = [cuenta_opts[s].usuario for s in seleccionadas]
        
        from core.monitor_actividad import MonitorActividad
        monitor = MonitorActividad()
        
        with st.spinner("Revisando actividad de las cuentas..."):
            resultados = monitor.monitorizar(horas=horas, cuentas_filtro=cuentas_filtro)
        
        if "error" in resultados:
            st.error("No hay configuración de cuentas lectoras. Configúralas en "
                     "config/monitor_actividad_config.json")
            return
        
        st.success("Monitoreo completado")
        
        c1, c2, c3 = st.columns(3)
        with c1:
            stat(resultados.get("cuentas_monitor", 0), "Cuentas")
        with c2:
            stat(resultados.get("lectoras_usadas", 0), "Lectoras")
        with c3:
            stat(resultados.get("horas", 0), "Horas")
        
        divider()
        
        for cuenta in resultados.get("resumen", []):
            with st.container(border=True):
                st.markdown(f"**@{cuenta['usuario']}**")
                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("Tweets", cuenta.get("tweets", 0))
                with c2:
                    st.metric("Retweets", cuenta.get("retweets", 0))
                with c3:
                    st.metric("Total", cuenta.get("total", 0))


def _estadisticas():
    st.markdown("### 📊 Estadísticas Guardadas")
    
    resultados_path = resolver_ruta("reportes/monitor_actividad.json")
    if not os.path.exists(resultados_path):
        st.info("No hay resultados guardados todavía.")
        return
    
    with open(resultados_path, encoding="utf-8") as f:
        historial = json.load(f)
    
    if not historial:
        st.info("Sin historial.")
        return
    
    st.markdown(f"Total de ejecuciones guardadas: **{len(historial)}**")
    
    for registro in reversed(historial[-20:]):
        timestamp = registro.get("timestamp", "?")
        with st.expander(f"📈 {timestamp}"):
            st.markdown(f"Cuentas: {registro.get('cuentas_monitor', 0)} · "
                        f"Lectoras: {registro.get('lectoras_usadas', 0)} · "
                        f"Horas: {registro.get('horas', 0)}")
            for cuenta in registro.get("resumen", []):
                st.markdown(
                    f"@{cuenta['usuario']} — tweets: {cuenta.get('tweets', 0)}, "
                    f"rt: {cuenta.get('retweets', 0)}, total: {cuenta.get('total', 0)}"
                )