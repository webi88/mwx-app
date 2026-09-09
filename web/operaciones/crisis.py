import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma


def render(usuario: dict):
    cabecera("💬 CRISIS: RESPUESTAS", "Respuestas a tweets en situaciones de crisis")
    
    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        st.warning("No hay cuentas de Twitter activas.")
        return
    
    st.markdown("### Configuración")
    
    col1, col2 = st.columns(2)
    with col1:
        cuenta_opts = {f"@{c.usuario}": c for c in cuentas}
        sel = st.selectbox("Cuenta", list(cuenta_opts), key="crisis_cuenta")
        cuenta = cuenta_opts[sel]
    
    url = st.text_input(
        "URL del tweet a responder",
        key="crisis_url",
        placeholder="https://x.com/usuario/status/123",
    )
    
    respuesta = st.text_area(
        "Texto de la respuesta",
        height=120,
        key="crisis_respuesta",
    )
    
    if st.button("💬 Enviar Respuesta", type="primary", key="btn_crisis"):
        if not url:
            st.warning("La URL es obligatoria.")
            return
        if not respuesta:
            st.warning("Escribe la respuesta.")
            return
        
        from plataformas.twitter.selenium_bot import TwitterBot
        bot = TwitterBot(cuenta.usuario)
        
        with st.spinner("Enviando respuesta..."):
            if bot.login_con_cookies():
                bot.driver.get(url)
                import time
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                
                time.sleep(3)
                try:
                    reply_btn = WebDriverWait(bot.driver, 10).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='reply']"))
                    )
                    reply_btn.click()
                    time.sleep(1)
                    
                    editor = WebDriverWait(bot.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='tweetTextarea_0']"))
                    )
                    bot._pegar_texto(editor, respuesta)
                    time.sleep(1)
                    
                    publish = bot.driver.find_element(By.CSS_SELECTOR, "[data-testid='tweetButtonInline']")
                    publish.click()
                    time.sleep(2)
                    
                    st.success("✅ Respuesta enviada")
                except Exception as e:
                    st.error(f"Error al responder: {e}")
                finally:
                    bot.cerrar()
            else:
                st.error("No se pudo iniciar sesión.")