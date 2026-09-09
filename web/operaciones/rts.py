import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_tags, ejecutar_en_cuentas, mostrar_resultados, guardar_imagen_subida


def render(usuario: dict):
    cabecera("⚡ RTS / ACTIVACIONES", "Retweets masivos y activación de cuentas")
    
    tabs = st.tabs([
        "🔁 Retweet Masivo",
        "💬 Retweet con Cita",
        "❤️ Like Masivo",
        "🔥 Calentamiento",
    ])
    
    with tabs[0]:
        _rt_masivo()
    with tabs[1]:
        _rt_cita()
    with tabs[2]:
        _like_masivo()
    with tabs[3]:
        _calentamiento()


def _seleccionar_cuentas(key: str):
    col1, col2 = st.columns(2)
    with col1:
        tags = st.multiselect(
            "Filtrar por tags",
            ["MD", "RT", "LK", "GRP", "ACT", "LIB", "NAC", "CAR"],
            key=f"{key}_tags",
        )
    with col2:
        cuentas = cuentas_por_tags(tags if tags else None)
        st.caption(f"Cuentas disponibles: **{len(cuentas)}**")
    
    opciones = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    seleccionadas = st.multiselect(
        "Selecciona cuentas",
        list(opciones),
        key=f"{key}_sel",
    )
    return [opciones[s] for s in seleccionadas]


def _parsear_urls(texto: str) -> list[str]:
    urls = []
    for linea in texto.splitlines():
        linea = linea.strip()
        if linea:
            urls.append(linea)
    return urls


def _rt_masivo():
    st.markdown("### 🔁 Retweet Masivo")
    
    cuentas = _seleccionar_cuentas("rt")
    
    st.markdown("Pega una URL de tweet por línea:")
    urls_text = st.text_area("URLs de tweets", height=120, key="rt_urls")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        dar_like = st.checkbox("Dar like también", key="rt_like", value=True)
    
    if st.button("🔁 Retwittear", type="primary", key="btn_rt"):
        urls = _parsear_urls(urls_text)
        if not cuentas:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            resultados = bot.solo_retwittear(
                urls, bot.usuario, dar_like=dar_like
            )
            return resultados.get("exitos", 0) > 0
        
        resultados = ejecutar_en_cuentas(cuentas, accion, "twitter", progreso, estado, tipo="rt")
        mostrar_resultados(resultados)


def _rt_cita():
    st.markdown("### 💬 Retweet con Cita")
    
    cuentas = _seleccionar_cuentas("rtc")
    
    st.markdown("Pega una URL de tweet por línea:")
    urls_text = st.text_area("URLs de tweets", height=100, key="rtc_urls")
    cita = st.text_area("Mensaje de cita (se usa en todos)", height=80, key="rtc_cita")
    
    archivo = st.file_uploader(
        "Imagen para la cita (opcional)",
        type=["png", "jpg", "jpeg", "gif", "webp"],
        key="rtc_imagen",
    )
    imagen_path = guardar_imagen_subida(archivo, prefijo="cita")
    if archivo is not None and imagen_path:
        st.image(imagen_path, width=180)
    
    if st.button("💬 RT con cita", type="primary", key="btn_rtc"):
        urls = _parsear_urls(urls_text)
        if not cuentas:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            resultados = bot.solo_retwittear(
                urls, bot.usuario, mensaje_cita=cita, imagen_path=imagen_path
            )
            return resultados.get("exitos", 0) > 0
        
        resultados = ejecutar_en_cuentas(cuentas, accion, "twitter", progreso, estado, tipo="activacion")
        mostrar_resultados(resultados)


def _like_masivo():
    st.markdown("### ❤️ Like Masivo")
    
    cuentas = _seleccionar_cuentas("lk")
    
    st.markdown("Pega una URL de tweet por línea:")
    urls_text = st.text_area("URLs de tweets", height=120, key="lk_urls")
    
    if st.button("❤️ Dar likes", type="primary", key="btn_lk"):
        urls = _parsear_urls(urls_text)
        if not cuentas:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            todos_ok = True
            for url in urls:
                if not bot.like(url):
                    todos_ok = False
            return todos_ok
        
        resultados = ejecutar_en_cuentas(cuentas, accion, "twitter", progreso, estado, tipo="like")
        mostrar_resultados(resultados)


def _calentamiento():
    st.markdown("### 🔥 Calentamiento")
    
    st.info(
        "El calentamiento realiza RT + likes gradualmente en cuentas nuevas "
        "para que no sean detectadas como bots."
    )
    
    cuentas = _seleccionar_cuentas("cal")
    
    st.markdown("Pega una URL de tweet por línea:")
    urls_text = st.text_area("URLs para calentamiento", height=100, key="cal_urls")
    
    if st.button("🔥 Iniciar calentamiento", type="primary", key="btn_cal"):
        urls = _parsear_urls(urls_text)
        if not cuentas:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        def accion(bot):
            resultados = bot.solo_retwittear(
                urls, bot.usuario, es_calentamiento=True, dar_like=True
            )
            return resultados.get("exitos", 0) > 0
        
        resultados = ejecutar_en_cuentas(cuentas, accion, "twitter", progreso, estado, tipo="calentamiento")
        mostrar_resultados(resultados)