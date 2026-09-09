import streamlit as st
from core.database import get_db_session
from core.models import Cliente
from core.registro import registrar_accion
from ia.celulas import CelulasManager
from web.ui import cabecera, empty_state, emoji_plataforma
from web.operaciones._helpers import cuentas_por_plataforma, ejecutar_en_cuentas, mostrar_resultados, guardar_imagen_subida


def render(usuario: dict):
    cabecera("📰 POSTS / MANTENIMIENTOS", "Publicación de contenido")
    
    tabs = st.tabs([
        "✍️ Publicar Texto",
        "🧵 Publicar Hilo",
        "✨ Generado con IA",
    ])
    
    with tabs[0]:
        _publicar_texto()
    with tabs[1]:
        _publicar_hilo()
    with tabs[2]:
        _generar_ia()


def _selector_cuentas(plataforma: str, key: str):
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        empty_state(f"No hay cuentas activas de {plataforma}.")
        return []
    
    opciones = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    seleccionadas = st.multiselect(
        f"Cuentas {emoji_plataforma(plataforma)} {plataforma}",
        list(opciones),
        key=key,
    )
    return [opciones[s] for s in seleccionadas]


def _publicar_texto():
    st.markdown("### ✍️ Publicar Texto")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "facebook", "instagram", "tiktok"],
        key="pub_plataforma",
        format_func=lambda p: f"{emoji_plataforma(p)} {p}",
    )
    
    cuentas = _selector_cuentas(plataforma, "pub_cuentas")
    
    contenido = st.text_area(
        "Contenido",
        height=150,
        key="pub_contenido",
        placeholder="Escribe el post a publicar...",
    )
    
    col1, col2 = st.columns([1, 3])
    with col1:
        usar_imagen = st.checkbox("Con imagen", key="pub_con_imagen")
    if usar_imagen:
        st.caption("📎 Pega o arrastra la imagen (o selecciónala con el botón).")
        archivo = st.file_uploader(
            "Imagen del post",
            type=["png", "jpg", "jpeg", "gif", "webp"],
            key="pub_imagen_upload",
        )
        imagen_path = guardar_imagen_subida(archivo, prefijo="post")
        if archivo is not None and imagen_path:
            st.image(imagen_path, width=220)
    else:
        imagen_path = None
    
    if st.button("📤 Publicar", type="primary", key="btn_publicar"):
        if not cuentas:
            st.warning("Selecciona al menos una cuenta.")
            return
        if not contenido:
            st.warning("El contenido no puede estar vacío.")
            return
        
        if plataforma == "twitter":
            accion = lambda bot: bot.publicar_tweet(contenido, imagen_path)
        else:
            accion = lambda bot: bot.publicar(contenido, imagen_path)
        
        progreso = st.progress(0)
        estado = st.empty()
        resultados = ejecutar_en_cuentas(cuentas, accion, plataforma, progreso, estado, tipo="mantenimiento")
        
        st.markdown("---")
        mostrar_resultados(resultados)


def _publicar_hilo():
    st.markdown("### 🧵 Publicar Hilo")
    
    cuentas = _selector_cuentas("twitter", "hilo_cuentas")
    
    st.caption("Escribe cada tweet del hilo separado por una línea en blanco.")
    hilo_texto = st.text_area(
        "Hilo (un tweet por párrafo)",
        height=220,
        key="hilo_texto",
    )
    
    if st.button("🧵 Publicar Hilo", type="primary", key="btn_hilo"):
        tweets = [t.strip() for t in hilo_texto.split("\n") if t.strip()]
        
        if not cuentas:
            st.warning("Selecciona al menos una cuenta.")
            return
        if len(tweets) < 2:
            st.warning("El hilo necesita al menos 2 tweets.")
            return
        
        resultados = {"exitos": 0, "fallidos": 0, "detalles": []}
        
        for cuenta in cuentas:
            try:
                from plataformas.twitter.selenium_bot import TwitterBot
                bot = TwitterBot(cuenta.usuario)
                ok = bot.publicar_hilo(tweets, cuenta.usuario)
                url = ok if isinstance(ok, str) else getattr(bot, "ultima_url_publicada", "")
                bot.cerrar()
                if ok:
                    resultados["exitos"] += 1
                    if url:
                        resultados["detalles"].append(f"✅ @{cuenta.usuario} — [ver post]({url})")
                    else:
                        resultados["detalles"].append(f"✅ @{cuenta.usuario}")
                    registrar_accion(cuenta.usuario, "hilo", "exito", url, "")
                else:
                    resultados["fallidos"] += 1
                    resultados["detalles"].append(f"❌ @{cuenta.usuario}")
                    registrar_accion(cuenta.usuario, "hilo", "fallido", "", "publicar_hilo")
            except Exception as e:
                resultados["fallidos"] += 1
                resultados["detalles"].append(f"❌ @{cuenta.usuario}: {str(e)[:50]}")
                registrar_accion(cuenta.usuario, "hilo", "fallido", "", str(e)[:120])
        
        mostrar_resultados(resultados)


def _generar_ia():
    st.markdown("### ✨ Generado con IA")
    
    if "web_generar_contenido" in st.session_state:
        pendiente = st.session_state["web_generar_contenido"]
        st.info(
            f"Generación programada: {pendiente['tipo']} "
            f"({pendiente['cantidad']} posts)."
        )
        if st.button("⚡ Generar ahora", key="btn_generar_pendiente"):
            _ejecutar_generacion(pendiente)
    
    st.markdown("---")
    st.markdown("#### Configurar nueva generación")
    
    tipos = [
        "Mantenimiento Diario", "Activación Digital", "Narrativa",
        "Reposteo con Cita", "Blog", "Verificado Ambiental", "Harfuch"
    ]
    tipo_map = {
        "Mantenimiento Diario": "mantenimiento",
        "Activación Digital": "activacion",
        "Narrativa": "narrativa",
        "Reposteo con Cita": "reposteo",
        "Blog": "blog",
        "Verificado Ambiental": "verificado",
        "Harfuch": "harfuch",
    }
    
    tipo = st.selectbox("Formato", tipos, key="pg_tipo")
    
    manager = CelulasManager()
    celulas = manager.obtener_celulas()
    clientes = manager.obtener_clientes()
    
    celula_opts = {c.nombre: c for c in celulas}
    if celula_opts:
        sel_cel = st.selectbox("Célula", list(celula_opts), key="pg_celula")
        celula = celula_opts[sel_cel]
    else:
        celula = None
        st.caption("No hay células. Créalas en el sidebar.")
    
    cliente_opts = {"(Sin cliente)": None}
    for c in clientes:
        cliente_opts[c.nombre] = c
    sel_cli = st.selectbox("Cliente", list(cliente_opts), key="pg_cliente")
    cliente = cliente_opts[sel_cli]
    
    contexto = st.text_area("Contexto / copy", height=90, key="pg_contexto")
    cantidad = st.number_input("Cantidad", 1, 20, 10, key="pg_cantidad")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✨ Generar Contenido", type="primary", key="btn_ia_generar"):
            _ejecutar_generacion({
                "tipo": tipo_map[tipo],
                "celula_id": celula.id if celula else None,
                "cliente_id": cliente.id if cliente else None,
                "contexto": contexto,
                "cantidad": int(cantidad),
            })


def _ejecutar_generacion(config):
    from ia.generador_contenido import GeneradorContenido
    from core.database import get_db_session
    from core.models import Celula, Cliente
    
    narrativa = ""
    entrenamiento = ""
    
    if config.get("celula_id"):
        with get_db_session() as db:
            celula = db.query(Celula).filter(Celula.id == config["celula_id"]).first()
            if celula:
                narrativa = celula.narrativa
    
    if config.get("cliente_id"):
        with get_db_session() as db:
            cliente = db.query(Cliente).filter(Cliente.id == config["cliente_id"]).first()
            if cliente:
                entrenamiento = cliente.entrenamiento
    
    with st.spinner("✨ Generando con OpenAI..."):
        generador = GeneradorContenido()
        posts = generador.generar_contenido(
            tipo=config["tipo"],
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            contexto=config.get("contexto", ""),
            cantidad=config.get("cantidad", 10),
        )
    
    if "web_generar_contenido" in st.session_state:
        del st.session_state["web_generar_contenido"]
    
    if not posts:
        st.error("No se generó contenido. Revisa la API key de OpenAI.")
        return
    
    st.session_state["web_ia_posts"] = posts
    st.success(f"Se generaron {len(posts)} posts.")
    st.rerun()


def _mostrar_posts_generados():
    posts = st.session_state.get("web_ia_posts", [])
    if not posts:
        return
    
    st.markdown("### 📄 Posts generados")
    
    cuentas = _selector_cuentas("twitter", "ia_pub_cuentas")
    
    for i, post in enumerate(posts):
        with st.container(border=True):
            st.markdown(post)
    
    if st.button("📤 Publicar todos en las cuentas seleccionadas", type="primary",
                 key="btn_ia_publicar"):
        if not cuentas:
            st.warning("Selecciona cuentas de Twitter.")
            return
        
        resultados = {"exitos": 0, "fallidos": 0, "detalles": []}
        
        for cuenta in cuentas:
            try:
                from plataformas.twitter.selenium_bot import TwitterBot
                bot = TwitterBot(cuenta.usuario)
                ok_todos = True
                url = ""
                for post in posts:
                    res = bot.publicar_tweet(post)
                    if not res:
                        ok_todos = False
                        break
                    url = res if isinstance(res, str) else getattr(bot, "ultima_url_publicada", "")
                bot.cerrar()
                if ok_todos:
                    resultados["exitos"] += 1
                    if url:
                        resultados["detalles"].append(f"✅ @{cuenta.usuario} — [ver post]({url})")
                    else:
                        resultados["detalles"].append(f"✅ @{cuenta.usuario}")
                    registrar_accion(cuenta.usuario, "mantenimiento", "exito", url, "")
                else:
                    resultados["fallidos"] += 1
                    resultados["detalles"].append(f"❌ @{cuenta.usuario}")
                    registrar_accion(cuenta.usuario, "mantenimiento", "fallido", "", "publicar_tweet")
            except Exception as e:
                resultados["fallidos"] += 1
                resultados["detalles"].append(f"❌ @{cuenta.usuario}: {str(e)[:50]}")
                registrar_accion(cuenta.usuario, "mantenimiento", "fallido", "", str(e)[:120])
        
        mostrar_resultados(resultados)


# Llamar al final de la pestaña IA para mostrar posts ya generados
if "web_ia_posts" in st.session_state:
    _mostrar_posts_generados()