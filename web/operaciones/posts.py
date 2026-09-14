import streamlit as st
from loguru import logger
from core.database import get_db_session
from core.models import Cliente
from core.registro import registrar_accion
from core.perfiles import etiqueta_perfil, normalizar_perfil
from core.registros import etiqueta_tipo_cuenta, normalizar_tipo_cuenta
from ia.celulas import CelulasManager
from web.ui import cabecera, empty_state, emoji_plataforma
from web.operaciones._helpers import (
    cuentas_por_plataforma,
    ejecutar_en_cuentas,
    mostrar_resultados,
    guardar_imagen_subida,
    generar_pool_por_cuenta_seguro,
)


def render(usuario: dict):
    cabecera("📰 POSTS / MANTENIMIENTOS", "Publicación de contenido")
    
    tabs = st.tabs([
        "✍️ Publicar Texto",
        "🧵 Publicar Hilo",
        "✨ Generado con IA",
        "🗓️ Mantenimiento Programado",
    ])
    
    with tabs[0]:
        _publicar_texto()
    with tabs[1]:
        _publicar_hilo()
    with tabs[2]:
        _generar_ia()
    with tabs[3]:
        _mantenimiento_programado(usuario)


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
    
    variar_por_cuenta = st.checkbox(
        "🔀 Generar una variación distinta por cuenta (evita texto idéntico en masa)",
        key="pub_variar_por_cuenta",
    )
    if variar_por_cuenta and len(cuentas) > 1:
        st.caption(
            "Se publicará una versión distinta del texto en cada cuenta "
            "(IA con fallback local si no está disponible)."
        )
    
    if st.button("📤 Publicar", type="primary", key="btn_publicar"):
        if not cuentas:
            st.warning("Selecciona al menos una cuenta.")
            return
        if not contenido:
            st.warning("El contenido no puede estar vacío.")
            return
        
        progreso = st.progress(0)
        estado = st.empty()
        
        if variar_por_cuenta and len(cuentas) > 1:
            registros = [
                normalizar_tipo_cuenta(getattr(c, "tipo_cuenta", "")) for c in cuentas
            ]
            with st.spinner("🔀 Generando una variación por cuenta..."):
                pool, uso_fallback = generar_pool_por_cuenta_seguro(
                    contenido, len(cuentas), registros=registros
                )
            if uso_fallback:
                st.info("La IA no estaba disponible: se usaron variaciones locales del texto.")
            
            if plataforma == "twitter":
                acciones = [(lambda b, t=texto: b.publicar_tweet(t, imagen_path)) for texto in pool]
            else:
                acciones = [(lambda b, t=texto: b.publicar(t, imagen_path)) for texto in pool]
            resultados = ejecutar_en_cuentas(
                cuentas, acciones, plataforma, progreso, estado, tipo="mantenimiento"
            )
        else:
            if plataforma == "twitter":
                accion = lambda bot: bot.publicar_tweet(contenido, imagen_path)
            else:
                accion = lambda bot: bot.publicar(contenido, imagen_path)
            resultados = ejecutar_en_cuentas(
                cuentas, accion, plataforma, progreso, estado, tipo="mantenimiento"
            )
        
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
                logger.exception(f"Error publicando hilo en @{cuenta.usuario}: {e}")
                resultados["fallidos"] += 1
                resultados["detalles"].append(f"❌ @{cuenta.usuario}: {str(e)[:400]}")
                registrar_accion(cuenta.usuario, "hilo", "fallido", "", str(e)[:400])
        
        mostrar_resultados(resultados)


def _generar_ia():
    st.markdown("### ✨ Generado con IA")
    
    if "web_ia_error" in st.session_state:
        st.error(f"Error de generación: {st.session_state.pop('web_ia_error')}")
    
    st.markdown("#### 🎯 Cuentas destino")
    st.caption(
        "Se generará **un texto distinto por cuenta** seleccionada "
        "(misma narrativa, redacción diferente)."
    )
    cuentas_ia = _selector_cuentas("twitter", "ia_pub_cuentas")
    
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
    
    cantidad_efectiva = int(cantidad)
    if cuentas_ia:
        cantidad_efectiva = max(int(cantidad), len(cuentas_ia))
        st.caption(
            f"ℹ️ Se generarán **{cantidad_efectiva}** textos para cubrir "
            f"las {len(cuentas_ia)} cuentas seleccionadas."
        )
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("✨ Generar Contenido", type="primary", key="btn_ia_generar"):
            _ejecutar_generacion({
                "tipo": tipo_map[tipo],
                "celula_id": celula.id if celula else None,
                "cliente_id": cliente.id if cliente else None,
                "contexto": contexto,
                "cantidad": cantidad_efectiva,
            })
    
    _mostrar_posts_generados(cuentas_ia)


def _resolver_narrativa_entrenamiento(celula_id, cliente_id) -> tuple[str, str]:
    """Recupera narrativa (célula) y entrenamiento (cliente) para la IA."""
    from core.database import get_db_session
    from core.models import Celula, Cliente
    
    narrativa = ""
    entrenamiento = ""
    
    if celula_id:
        with get_db_session() as db:
            celula = db.query(Celula).filter(Celula.id == celula_id).first()
            if celula:
                narrativa = celula.narrativa or ""
    
    if cliente_id:
        with get_db_session() as db:
            cliente = db.query(Cliente).filter(Cliente.id == cliente_id).first()
            if cliente:
                entrenamiento = cliente.entrenamiento or ""
    
    return narrativa, entrenamiento


def _ejecutar_generacion(config):
    from ia.generador_contenido import GeneradorContenido
    
    narrativa, entrenamiento = _resolver_narrativa_entrenamiento(
        config.get("celula_id"), config.get("cliente_id")
    )
    
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
        causa = getattr(generador, "ultimo_error", "") or "sin respuesta del generador"
        st.session_state["web_ia_error"] = str(causa)
        st.rerun()
        return
    
    config["narrativa"] = narrativa
    config["entrenamiento"] = entrenamiento
    st.session_state["web_ia_posts"] = posts
    st.session_state["web_ia_config"] = config
    st.success(f"Se generaron {len(posts)} textos.")
    st.rerun()


def _editor_textos(posts: list[str]) -> list[str]:
    """Muestra los textos en un editor y devuelve la lista editada."""
    try:
        import pandas as pd
        
        df = pd.DataFrame({"N°": range(1, len(posts) + 1), "texto": posts})
        editado = st.data_editor(
            df,
            key=f"web_ia_editor_{abs(hash(tuple(posts))) % (10 ** 8)}",
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "N°": st.column_config.NumberColumn("N°", disabled=True, width="small"),
                "texto": st.column_config.TextColumn(
                    "Texto (editable)", width="large", required=False
                ),
            },
        )
        textos = [str(t).strip() for t in editado["texto"].tolist()]
        return [t for t in textos if t]
    except Exception as e:
        logger.warning(f"data_editor no disponible ({e}); usando text_area")
        textos = []
        for i, post in enumerate(posts):
            t = st.text_area(
                f"Texto {i + 1}", value=post, key=f"ia_post_edit_{i}", height=110
            )
            if t and t.strip():
                textos.append(t.strip())
        return textos


def _mostrar_posts_generados(cuentas=None):
    posts = st.session_state.get("web_ia_posts", [])
    if not posts:
        return
    
    st.markdown("### 📄 Textos generados (revisa y edita antes de publicar)")
    
    if cuentas is None:
        cuentas = _selector_cuentas("twitter", "ia_pub_cuentas")
    
    posts = _editor_textos(posts)
    if not posts:
        st.warning("No queda ningún texto después de la edición.")
        return
    st.session_state["web_ia_posts"] = posts
    
    with st.expander("🔍 Mapeo previsto cuenta → texto", expanded=False):
        if not cuentas:
            st.caption("Selecciona cuentas de Twitter para ver el mapeo.")
        else:
            for i, cuenta in enumerate(cuentas):
                registro = etiqueta_tipo_cuenta(getattr(cuenta, "tipo_cuenta", ""))
                perfil = etiqueta_perfil(getattr(cuenta, "perfil_personalidad", ""))
                if i < len(posts):
                    fragmento = " ".join(posts[i].split())[:160]
                    st.markdown(
                        f"**{i + 1}. @{cuenta.usuario}** "
                        f"[{registro} · {perfil}] → {fragmento}…"
                    )
                else:
                    st.markdown(
                        f"**{i + 1}. @{cuenta.usuario}** [{registro} · {perfil}] → "
                        "_(se completará con una variación al publicar)_"
                    )
    
    st.markdown("---")
    
    if len(posts) > 1:
        idx = st.selectbox(
            "Texto base para 'el mismo en todas'",
            options=list(range(len(posts))),
            format_func=lambda i: f"#{i + 1}: {' '.join(posts[i].split())[:90]}…",
            key="ia_texto_mismo",
        )
    else:
        idx = 0
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "📤 Publicar (un texto distinto por cuenta)",
            type="primary",
            key="btn_ia_publicar_distinto",
        ):
            _publicar_pool_por_cuenta(posts, cuentas)
    with col2:
        if st.button("📤 Publicar el mismo texto en todas", key="btn_ia_publicar_mismo"):
            _publicar_mismo_texto(posts[idx], cuentas)


def _publicar_pool_por_cuenta(posts: list[str], cuentas: list):
    """Publica un texto distinto por cuenta (empareja pool[i] → cuenta[i])."""
    if not cuentas:
        st.warning("Selecciona al menos una cuenta de Twitter.")
        return
    if not posts:
        st.warning("No hay textos para publicar.")
        return
    
    cfg = st.session_state.get("web_ia_config", {}) or {}
    narrativa = cfg.get("narrativa", "")
    entrenamiento = cfg.get("entrenamiento", "")
    if not narrativa and not entrenamiento:
        narrativa, entrenamiento = _resolver_narrativa_entrenamiento(
            cfg.get("celula_id"), cfg.get("cliente_id")
        )
    
    pool = [str(p).strip() for p in posts if str(p).strip()]
    if len(pool) < len(cuentas):
        # Registro y perfil alineados por indice con las cuentas seleccionadas.
        registros = [
            normalizar_tipo_cuenta(getattr(c, "tipo_cuenta", "")) for c in cuentas
        ]
        perfiles = [
            normalizar_perfil(getattr(c, "perfil_personalidad", "")) for c in cuentas
        ]
        with st.spinner("🔀 Generando una variación por cuenta..."):
            pool, uso_fallback = generar_pool_por_cuenta_seguro(
                pool[0] if pool else "",
                len(cuentas),
                narrativa,
                entrenamiento,
                registros=registros,
                perfiles=perfiles,
            )
        if uso_fallback:
            st.info(
                "La IA no estaba disponible: se usaron variaciones locales "
                "para completar los textos faltantes."
            )
    
    if len(pool) < len(cuentas):
        st.error("No se pudieron generar suficientes textos para todas las cuentas.")
        return
    
    resultados = {"exitos": 0, "fallidos": 0, "detalles": []}
    progreso = st.progress(0)
    estado = st.empty()
    total = len(cuentas)
    
    for i, cuenta in enumerate(cuentas):
        texto = pool[i]
        estado.write(f"⏳ Publicando en **@{cuenta.usuario}** ({i + 1}/{total})...")
        bot = None
        try:
            from plataformas.twitter.selenium_bot import TwitterBot
            bot = TwitterBot(cuenta.usuario)
            res = bot.publicar_tweet(texto)
            url = res if isinstance(res, str) else (getattr(bot, "ultima_url_publicada", "") or "")
            if res:
                resultados["exitos"] += 1
                if url:
                    resultados["detalles"].append(f"✅ @{cuenta.usuario} — [ver post]({url})")
                else:
                    resultados["detalles"].append(f"✅ @{cuenta.usuario}")
                registrar_accion(cuenta.usuario, "mantenimiento", "exito", url, "")
            else:
                resultados["fallidos"] += 1
                motivo = getattr(bot, "ultimo_error", "") or ""
                detalle = f"❌ @{cuenta.usuario}"
                if motivo:
                    detalle += f": {motivo[:400]}"
                resultados["detalles"].append(detalle)
                registrar_accion(cuenta.usuario, "mantenimiento", "fallido", "", detalle)
        except Exception as e:
            logger.exception(f"Error publicando en @{cuenta.usuario}: {e}")
            resultados["fallidos"] += 1
            resultados["detalles"].append(f"❌ @{cuenta.usuario}: {str(e)[:400]}")
            registrar_accion(cuenta.usuario, "mantenimiento", "fallido", "", str(e)[:400])
        finally:
            if bot is not None:
                try:
                    bot.cerrar()
                except Exception:
                    pass
        progreso.progress((i + 1) / total)
    
    estado.write("✅ Proceso terminado")
    mostrar_resultados(resultados)
    
    st.session_state.pop("web_ia_posts", None)
    st.session_state.pop("web_ia_config", None)


def _publicar_mismo_texto(texto: str, cuentas: list):
    """Publica el MISMO texto en todas las cuentas seleccionadas."""
    if not cuentas:
        st.warning("Selecciona al menos una cuenta de Twitter.")
        return
    if not texto or not str(texto).strip():
        st.warning("No hay texto para publicar.")
        return
    
    progreso = st.progress(0)
    estado = st.empty()
    resultados = ejecutar_en_cuentas(
        cuentas,
        lambda bot: bot.publicar_tweet(texto),
        "twitter",
        progreso,
        estado,
        tipo="mantenimiento",
    )
    mostrar_resultados(resultados)


# ================== MANTENIMIENTO PROGRAMADO ==================
# Reparte N tuits por cuenta a lo largo del dia (ventana 07:00 → 02:00
# configurable) generando cada texto segun la PERSONALIDAD de la cuenta y
# alternando los temas de mantenimiento (azteca/dia/tendencias/gustos).


def _generar_slots_ventana(
    fecha,
    hora_inicio,
    hora_fin,
    cruza_medianoche: bool = False,
    n: int = 1,
    min_sep_min: int = 45,
) -> list:
    """Devuelve `n` datetimes aleatorios ORDENADOS dentro de la ventana.

    - `fecha` puede ser `date` o `datetime` (se usa su parte de fecha).
    - Si `cruza_medianoche=True` el fin se corre un dia (p. ej. 07:00 → 02:00).
    - La separacion entre slots de la MISMA cuenta es >= `min_sep_min`; si no
      caben `n` slots con esa separacion, se reduce al maximo posible
      (`ventana / (n-1)`), por lo que nunca quedan fuera de rango.
    - `n <= 0` o ventana invalida devuelven []. Funcion pura, sin Streamlit
      ni BD (testeable offline).
    """
    import random
    from datetime import datetime, timedelta

    try:
        n = int(n)
        min_sep_min = float(min_sep_min)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    if min_sep_min < 0:
        min_sep_min = 0.0

    try:
        fecha_ref = fecha.date() if isinstance(fecha, datetime) else fecha
        inicio = datetime.combine(fecha_ref, hora_inicio)
        fin = datetime.combine(fecha_ref, hora_fin)
    except Exception:
        return []
    if cruza_medianoche:
        fin = fin + timedelta(days=1)
    if fin < inicio:
        return []

    total_min = (fin - inicio).total_seconds() / 60.0

    if n == 1:
        return [inicio + timedelta(minutes=random.uniform(0.0, total_min))]

    # Separacion efectiva: la pedida o la maxima posible si no cabe.
    sep = min(min_sep_min, total_min / (n - 1))
    span = (n - 1) * sep
    extra = random.uniform(0.0, max(0.0, total_min - span))

    # Reparte el "extra" aleatoriamente entre los n-1 huecos.
    if n > 2:
        cortes = sorted(random.uniform(0.0, extra) for _ in range(n - 2))
        partes = [cortes[0]]
        partes += [cortes[k] - cortes[k - 1] for k in range(1, len(cortes))]
        partes.append(extra - cortes[-1])
    else:
        partes = [extra]

    posiciones = [random.uniform(0.0, max(0.0, total_min - (span + extra)))]
    for parte in partes:
        posiciones.append(posiciones[-1] + sep + parte)

    return [inicio + timedelta(minutes=p) for p in posiciones]


def _construir_plan(
    cuentas_sel: list,
    textos_por_cuenta: list,
    slots_por_cuenta: list,
    temas: list | None = None,
    n_por_cuenta: int | None = None,
) -> list:
    """Construye las entradas del plan de mantenimiento (funcion pura).

    Devuelve una lista de dicts {"cuenta_id", "usuario", "fecha_hora", "tema",
    "texto"} ordenada por `fecha_hora`. La rotacion de temas es la MISMA que
    usa `ia.generador_contenido`: `temas[(i * n + j) % len(temas)]`, para que
    la etiqueta del plan coincida con el texto generado.
    """
    if temas is None:
        try:
            from ia.prompts import TEMAS_MANTENIMIENTO

            temas = list(TEMAS_MANTENIMIENTO)
        except Exception:
            temas = []
    temas = [str(t).strip() for t in (temas or []) if str(t).strip()] or [
        "azteca",
        "dia",
        "tendencias",
        "gustos",
    ]

    from datetime import datetime as _dt

    plan = []
    for i, fila in enumerate(cuentas_sel or []):
        fila = fila or {}
        textos = list(textos_por_cuenta[i]) if i < len(textos_por_cuenta or []) and textos_por_cuenta[i] else []
        slots = list(slots_por_cuenta[i]) if i < len(slots_por_cuenta or []) and slots_por_cuenta[i] else []
        n = int(n_por_cuenta) if n_por_cuenta is not None else len(textos)
        for j in range(max(0, n)):
            texto = str(textos[j]).strip() if j < len(textos) and textos[j] is not None else ""
            plan.append(
                {
                    "cuenta_id": fila.get("id"),
                    "usuario": str(fila.get("usuario") or ""),
                    "fecha_hora": slots[j] if j < len(slots) else None,
                    "tema": temas[(i * n + j) % len(temas)],
                    "texto": texto,
                }
            )

    plan.sort(key=lambda e: e.get("fecha_hora") or _dt.max)
    return plan


def _fila_selector_mantenimiento(cuenta) -> dict:
    """Convierte una Cuenta ORM en el dict que consume `_selector_masivo`."""
    from core.registros import normalizar_tipo_cuenta
    from core.secciones import normalizar_seccion

    return {
        "id": getattr(cuenta, "id", None),
        "usuario": getattr(cuenta, "usuario", "") or "",
        "status": getattr(cuenta, "status", "") or "",
        "seccion": normalizar_seccion(getattr(cuenta, "seccion", "")),
        "tipo_cuenta": normalizar_tipo_cuenta(getattr(cuenta, "tipo_cuenta", "")),
        "handle_actual": (getattr(cuenta, "handle_actual", "") or "").strip(),
        "nombre_mostrado": (getattr(cuenta, "nombre_mostrado", "") or "").strip(),
        "personalidad": (getattr(cuenta, "personalidad", "") or "").strip(),
        "perfil": normalizar_perfil(getattr(cuenta, "perfil_personalidad", "")),
        "grupo": getattr(cuenta, "grupo", "") or "",
        "email": getattr(cuenta, "email", "") or "",
    }


def _preparar_plan(
    seleccion: list,
    fecha_base,
    hora_inicio,
    hora_fin,
    cruza: bool,
    n: int,
    min_sep: int,
    celula,
    cliente,
):
    """Genera los textos con IA (fallback local) y guarda el plan preparado."""
    from datetime import datetime

    from ia.generador_contenido import generar_textos_mantenimiento
    from ia.prompts import TEMAS_MANTENIMIENTO

    narrativa, entrenamiento = _resolver_narrativa_entrenamiento(
        celula.id if celula else None, cliente.id if cliente else None
    )
    cuentas_info = [
        {
            "usuario": f.get("usuario") or "",
            "registro": f.get("tipo_cuenta") or "",
            "personalidad": f.get("personalidad") or "",
            "seccion": f.get("seccion") or "",
            "nombre": f.get("nombre_mostrado") or f.get("usuario") or "",
            "perfil": f.get("perfil") or "",
            "tipo_accion": "post",
        }
        for f in seleccion
    ]
    slots_por_cuenta = [
        _generar_slots_ventana(fecha_base, hora_inicio, hora_fin, cruza, n, min_sep)
        for _ in seleccion
    ]

    progreso = st.progress(0.0)
    estado = st.empty()
    total_objetivo = max(1, len(seleccion) * n)

    def _callback(hechas, total):
        try:
            total = int(total) or total_objetivo
            progreso.progress(min(1.0, float(hechas) / total))
            estado.caption(f"✍️ Generando textos de mantenimiento: {hechas}/{total}...")
        except Exception:
            pass

    errores = []
    try:
        textos = generar_textos_mantenimiento(
            cuentas_info,
            n_por_cuenta=n,
            narrativa=narrativa,
            entrenamiento=entrenamiento,
            callback=_callback,
        )
    except Exception as e:
        logger.exception(f"Error generando textos de mantenimiento: {e}")
        textos = []
        errores.append(str(e)[:300])

    try:
        progreso.progress(1.0)
        estado.caption("✅ Textos generados.")
    except Exception:
        pass

    plan = _construir_plan(
        seleccion,
        textos,
        slots_por_cuenta,
        temas=list(TEMAS_MANTENIMIENTO),
        n_por_cuenta=n,
    )
    sin_texto = sum(1 for p in plan if not (p.get("texto") or "").strip())
    fechas = [p["fecha_hora"] for p in plan if p.get("fecha_hora")]

    st.session_state["mant_plan"] = plan
    st.session_state["mant_plan_resumen"] = {
        "cuentas": len(seleccion),
        "tuits": len(plan),
        "n_por_cuenta": n,
        "sin_texto": sin_texto,
        "primer_slot": min(fechas) if fechas else None,
        "ultimo_slot": max(fechas) if fechas else None,
        "errores": errores,
        "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def _preview_plan(plan: list):
    """Previsualiza hasta 300 filas del plan (usuario, fecha, tema, texto)."""
    filas = []
    for p in plan[:300]:
        fh = p.get("fecha_hora")
        filas.append(
            {
                "usuario": f"@{p.get('usuario', '')}",
                "fecha_hora": fh.strftime("%d/%m/%Y %H:%M") if hasattr(fh, "strftime") else "",
                "tema": p.get("tema", ""),
                "texto": " ".join(str(p.get("texto") or "").split())[:100],
            }
        )
    try:
        import pandas as pd

        st.dataframe(
            pd.DataFrame(filas), hide_index=True, use_container_width=True
        )
    except Exception:
        for fila in filas:
            st.markdown(
                f"**{fila['usuario']}** · {fila['fecha_hora']} · "
                f"`{fila['tema']}` — {fila['texto']}"
            )
    if len(plan) > 300:
        st.caption(f"Mostrando las primeras 300 de {len(plan)} filas.")


def _programar_plan(plan: list, usuario: dict | None = None):
    """Crea una Tarea por entrada del plan y la guarda en el scheduler."""
    from core.models import Tarea
    from scheduler.manager import SchedulerManager

    try:
        manager = SchedulerManager()
    except Exception as e:
        logger.exception(f"No se pudo iniciar el scheduler: {e}")
        st.error(f"No se pudo iniciar el scheduler: {e}")
        return

    programadas = 0
    fallidas = 0
    omitidas = 0
    for p in plan:
        texto = str(p.get("texto") or "").strip()
        if not texto or p.get("fecha_hora") is None or p.get("cuenta_id") is None:
            omitidas += 1
            continue
        try:
            tarea = Tarea(
                tipo="post",
                plataforma="twitter",
                contenido=texto,
                cuentas_ids=str([p["cuenta_id"]]),
                fecha_hora=p["fecha_hora"],
                estado="pendiente",
                creada_por=(usuario or {}).get("username"),
            )
            if manager.programar_tarea(tarea):
                programadas += 1
            else:
                fallidas += 1
        except Exception as e:
            logger.exception(f"Error programando tuit del plan: {e}")
            fallidas += 1

    if programadas:
        st.success(f"✅ {programadas} tuit(es) programados en el scheduler.")
    if fallidas:
        st.error(f"❌ {fallidas} tuit(es) no se pudieron programar.")
    if omitidas:
        st.warning(f"⚠️ {omitidas} entrada(s) se omitieron (sin texto o sin slot).")
    st.caption(
        "El scheduler del servidor ejecutará cada tuit en su horario "
        "(requiere que el proceso scheduler esté corriendo)."
    )
    st.session_state.pop("mant_plan", None)
    st.session_state.pop("mant_plan_resumen", None)


def _mantenimiento_programado(usuario: dict | None = None):
    """Pestana de mantenimiento programado: reparte N tuits por cuenta a lo
    largo del dia (ventana inicio → fin, opcionalmente cruzando medianoche),
    con textos generados segun la personalidad y temas rotativos."""
    from datetime import date, datetime, time, timedelta

    usuario = usuario or {}
    st.markdown("### 🗓️ Mantenimiento Programado")
    st.caption(
        "Prepara el mantenimiento del día y prográmalo de forma orgánica entre "
        "la hora de inicio y la hora de fin (puede terminar al día siguiente). "
        "Cada cuenta publica textos acordes a su personalidad, alternando temas "
        "azteca/prehispánico, día/actualidad, tendencias y gustos."
    )

    # Resumen de la ultima generacion de personalidades (sobrevive al rerun).
    resumen_pers = st.session_state.pop("mant_pers_resumen", None)
    if resumen_pers:
        st.success(
            "🧠 Personalidades generadas: "
            f"{resumen_pers.get('asignadas', 0)} asignadas, "
            f"{resumen_pers.get('respetadas', 0)} ya existían."
        )
        errores_pers = list(resumen_pers.get("errores") or [])
        if errores_pers:
            with st.expander(f"⚠️ {len(errores_pers)} aviso(s) de personalidades"):
                for e in errores_pers[:20]:
                    st.write(f"- {e}")

    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        empty_state("No hay cuentas de Twitter activas.")
        return

    st.markdown("#### 🎯 Cuentas a programar")
    try:
        from web.operaciones.cuentas import _selector_masivo
    except Exception as e:
        st.error(f"No se pudo cargar el selector de cuentas: {e}")
        return

    filas = [_fila_selector_mantenimiento(c) for c in cuentas]
    seleccion = _selector_masivo(filas, key="mant_sel") or []

    sin_person = [f for f in seleccion if not (f.get("personalidad") or "").strip()]

    col_aviso, col_boton = st.columns([3, 1.4])
    with col_aviso:
        if seleccion and sin_person:
            st.warning(
                f"⚠️ {len(sin_person)} de {len(seleccion)} cuentas seleccionadas "
                "no tienen personalidad. Genera las faltantes para que cada tuit "
                "respete su tono e intereses."
            )
        elif seleccion:
            st.success(
                f"✅ Las {len(seleccion)} cuentas seleccionadas tienen personalidad."
            )
    with col_boton:
        if st.button(
            "🧠 Generar personalidades faltantes",
            key="mant_btn_person",
            disabled=not sin_person,
            use_container_width=True,
        ):
            from cuentas.generador_identidades import asignar_personalidades

            with st.spinner("Generando personalidades (IA con fallback local)..."):
                resumen = asignar_personalidades(
                    [f["usuario"] for f in sin_person], forzar=False
                )
            st.session_state["mant_pers_resumen"] = resumen
            st.rerun()

    if seleccion:
        # Reparto por perfil de redaccion (formal/ciudadano/popular).
        conteo_perfiles = {"formal": 0, "ciudadano": 0, "popular": 0, "sin_perfil": 0}
        for fila in seleccion:
            clave = normalizar_perfil((fila or {}).get("perfil"))
            conteo_perfiles[clave if clave in conteo_perfiles else "sin_perfil"] += 1
        st.caption(
            "🎭 Reparto por perfil: "
            f"{etiqueta_perfil('formal')}: {conteo_perfiles['formal']} · "
            f"{etiqueta_perfil('ciudadano')}: {conteo_perfiles['ciudadano']} · "
            f"{etiqueta_perfil('popular')}: {conteo_perfiles['popular']} · "
            f"Sin perfil: {conteo_perfiles['sin_perfil']}"
        )

        with st.expander(
            "👁️ Personalidades de las cuentas seleccionadas (hasta 10)",
            expanded=False,
        ):
            for fila in seleccion[:10]:
                pers = (fila.get("personalidad") or "").strip() or "_(sin personalidad)_"
                st.markdown(f"**@{fila.get('usuario', '')}** — {pers}")
            if len(seleccion) > 10:
                st.caption(f"... y {len(seleccion) - 10} cuenta(s) más.")

    st.markdown("#### ⏱️ Ventana y ritmo")
    col_fecha, col_ini, col_fin, col_cruza, col_n, col_sep = st.columns(
        [1.2, 1, 1, 1.2, 1, 1.3]
    )
    with col_fecha:
        fecha_base = st.date_input(
            "Fecha base",
            value=date.today() + timedelta(days=1),
            key="mant_fecha",
            help="Día en que empieza la ventana (por defecto, mañana).",
        )
    with col_ini:
        hora_inicio = st.time_input(
            "Hora inicio", value=time(7, 0), key="mant_hora_inicio"
        )
    with col_fin:
        hora_fin = st.time_input(
            "Hora fin", value=time(2, 0), key="mant_hora_fin"
        )
    with col_cruza:
        cruza = st.checkbox(
            "Termina al día siguiente",
            value=(hora_fin < hora_inicio),
            key="mant_cruza",
            help="Marca si la ventana cruza medianoche (p. ej. 07:00 → 02:00).",
        )
    with col_n:
        n_por_cuenta = st.number_input(
            "Tuits por cuenta",
            min_value=1,
            max_value=5,
            value=2,
            step=1,
            key="mant_n",
        )
    with col_sep:
        min_sep = st.number_input(
            "Separación mínima (min)",
            min_value=10,
            max_value=240,
            value=45,
            step=5,
            key="mant_sep",
        )

    inicio_dt = datetime.combine(fecha_base, hora_inicio)
    fin_dt = datetime.combine(fecha_base, hora_fin)
    if cruza:
        fin_dt += timedelta(days=1)
    ventana_valida = fin_dt >= inicio_dt
    if ventana_valida:
        horas = (fin_dt - inicio_dt).total_seconds() / 3600.0
        st.caption(
            f"🕒 Ventana: **{inicio_dt:%d/%m/%Y %H:%M} → "
            f"{fin_dt:%d/%m/%Y %H:%M}** ({horas:.1f} h) · "
            f"{int(n_por_cuenta)} tuit(es) por cuenta · separación mínima "
            f"{int(min_sep)} min."
        )
    else:
        st.error(
            "La hora de fin quedó antes que la de inicio. Marca "
            "'Termina al día siguiente' o corrige las horas."
        )

    st.markdown("#### 🧬 Narrativa y entrenamiento (opcional)")
    manager = CelulasManager()

    col_cel, col_cli = st.columns(2)
    with col_cel:
        celula_opts = {"(Sin célula)": None}
        for c in manager.obtener_celulas():
            celula_opts[c.nombre] = c
        sel_cel = st.selectbox("Célula", list(celula_opts), key="mant_celula")
        celula = celula_opts[sel_cel]
    with col_cli:
        cliente_opts = {"(Sin cliente)": None}
        for c in manager.obtener_clientes():
            cliente_opts[c.nombre] = c
        sel_cli = st.selectbox("Cliente", list(cliente_opts), key="mant_cliente")
        cliente = cliente_opts[sel_cli]

    if st.button(
        "🧠 Preparar plan (generar textos)",
        type="primary",
        key="mant_btn_preparar",
        disabled=not seleccion or not ventana_valida,
    ):
        _preparar_plan(
            seleccion=seleccion,
            fecha_base=fecha_base,
            hora_inicio=hora_inicio,
            hora_fin=hora_fin,
            cruza=cruza,
            n=int(n_por_cuenta),
            min_sep=int(min_sep),
            celula=celula,
            cliente=cliente,
        )

    plan = st.session_state.get("mant_plan")
    if not plan:
        return

    resumen = st.session_state.get("mant_plan_resumen", {}) or {}
    st.markdown("---")
    st.markdown("#### 📋 Plan preparado")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Cuentas", resumen.get("cuentas", 0))
    c2.metric("Tuits", len(plan))
    c3.metric("Sin texto", resumen.get("sin_texto", 0))
    c4.metric("Por cuenta", resumen.get("n_por_cuenta", 0))

    primer = resumen.get("primer_slot")
    ultimo = resumen.get("ultimo_slot")
    st.caption(
        "Primer slot: "
        + (primer.strftime("%d/%m/%Y %H:%M") if hasattr(primer, "strftime") else "—")
        + " · Último slot: "
        + (ultimo.strftime("%d/%m/%Y %H:%M") if hasattr(ultimo, "strftime") else "—")
        + (f" · Preparado: {resumen['generado']}" if resumen.get("generado") else "")
    )
    if resumen.get("errores"):
        with st.expander(
            f"⚠️ {len(resumen['errores'])} error(es) durante la generación"
        ):
            for e in resumen["errores"][:10]:
                st.write(f"- {e}")
    if resumen.get("sin_texto"):
        st.warning(
            f"⚠️ {resumen['sin_texto']} tuit(s) quedaron sin texto y no se "
            "programarán. Vuelve a preparar el plan o revisa la generación."
        )

    _preview_plan(plan)

    if st.button(
        "✅ Programar plan en el scheduler",
        type="primary",
        key="mant_btn_programar",
    ):
        _programar_plan(plan, usuario)