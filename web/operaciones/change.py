import builtins

import streamlit as st
from web.ui import cabecera, stat

# `input()` original del proceso: se guarda UNA vez al importar y se restaura
# SIEMPRE tras el monkeypatch de la campana. Antes el `finally` borraba el
# atributo y dejaba al proceso de Streamlit sin `input()` (rompia cualquier
# libreria/pagina que lo usara hasta reiniciar el servidor).
_INPUT_ORIGINAL = getattr(builtins, "input", None)


def render(usuario: dict):
    cabecera("✍️ CHANGE.ORG: PETICIONES", "Campañas de firmas automatizadas")
    
    st.markdown("### Configuración de la Campaña")
    
    url = st.text_input(
        "URL de la petición",
        key="change_url",
        placeholder="https://www.change.org/p/...",
    )
    
    col1, col2 = st.columns(2)
    with col1:
        firmas_por_ip = st.number_input("Firmas por IP", 1, 50, 5, key="change_firmas")
    with col2:
        reconexiones = st.number_input("Reconexiones (cambios de IP)", 1, 10, 1, key="change_recon")
    
    st.warning(
        "⚠️ En el VPS, entre cada reconexión el sistema pausa para que cambies "
        "la IP manualmente. Con 'Reconexiones = 1' no hay pausas."
    )
    
    if st.button("✍️ Iniciar Campaña", type="primary", key="btn_change"):
        if not url:
            st.warning("La URL es obligatoria.")
            return
        
        from cuentas.change_org import ChangeOrgBot
        
        with st.spinner("⏳ Iniciando campaña de firmas..."):
            # Monkeypatch input() para que las pausas de IP avancen; el
            # original se restaura SIEMPRE en el `finally` (ver
            # `_INPUT_ORIGINAL`): nunca se elimina el builtin del proceso.
            contador = {"n": 0}

            def input_auto(prompt=""):
                contador["n"] += 1
                st.session_state.setdefault("web_change_pausas", []).append(prompt)
                return ""

            try:
                builtins.input = input_auto

                bot = ChangeOrgBot()
                resultados = bot.ejecutar_sesion(
                    url_peticion=url,
                    firmas_por_ip=int(firmas_por_ip),
                    reconexiones=int(reconexiones),
                )
            finally:
                try:
                    if _INPUT_ORIGINAL is not None:
                        builtins.input = _INPUT_ORIGINAL
                    else:
                        builtins.__dict__.pop("input", None)
                except Exception:
                    pass
        
        st.success("Campaña terminada")
        c1, c2, c3 = st.columns(3)
        with c1:
            stat(resultados["total"], "Total intentos")
        with c2:
            stat(resultados["exitosas"], "Exitosas")
        with c3:
            stat(resultados["fallidas"], "Fallidas")
        
        if resultados["firmas"]:
            with st.expander("🔍 Detalle de firmas"):
                for firma in resultados["firmas"]:
                    nombre = firma.get("nombre", "?")
                    estado = "✅" if firma.get("exito") else "❌"
                    st.markdown(f"{estado} {nombre} — {firma.get('error', '')}")