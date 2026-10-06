import random

import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import (
    aviso_pausadas,
    cuentas_por_plataforma,
    ejecutar_en_cuentas,
    mostrar_resultados,
    separar_pausadas,
)


def render(usuario: dict):
    cabecera("❤️ INFLUENCIA: LIKES", "Dar likes masivos en varias plataformas")
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "instagram", "facebook", "tiktok"],
        key="likes_plataforma",
        format_func=lambda p: {
            "twitter": "🐦 Twitter", "instagram": "📷 Instagram",
            "facebook": "📘 Facebook", "tiktok": "🎵 TikTok",
        }[p],
    )
    
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        st.warning(f"No hay cuentas activas de {plataforma}.")
        return

    # Pausadas para activación: fuera de los likes masivos (siguen en
    # mantenimiento). Además `ejecutar_en_cuentas` las omite por defecto.
    cuentas, pausadas = separar_pausadas(cuentas)
    aviso_pausadas(pausadas)
    if not cuentas:
        st.warning(
            f"Todas las cuentas activas de {plataforma} están pausadas para "
            "activación (siguen en mantenimiento)."
        )
        return
    
    todas = st.checkbox(
        "✅ Dar like con TODAS las cuentas", value=True, key="likes_todas"
    )

    cuenta_opts = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    if todas:
        cuentas_sel = list(cuentas)
        st.caption(
            f"Se dará like con las {len(cuentas_sel)} cuentas activas de "
            f"{plataforma}."
        )
    else:
        seleccionadas = st.multiselect(
            "Cuentas", list(cuenta_opts), key="likes_cuentas"
        )
        cuentas_sel = [cuenta_opts[s] for s in seleccionadas]
    
    st.markdown("Pega una URL por línea:")
    urls_text = st.text_area("URLs", height=120, key="likes_urls")

    # El RT (retweet) solo existe en Twitter: en el resto de plataformas no hay
    # `bot.solo_retwittear` y forzamos "Solo Like" para no crashear.
    es_twitter = plataforma == "twitter"
    if es_twitter:
        modo = st.radio(
            "Modo de influencia",
            [
                "🔁 Like + RT (recomendado para tendencia)",
                "❤️ Solo Like",
            ],
            index=0,
            key="likes_modo",
            help=(
                "El RT con like es una señal más fuerte para «tendencia» que el "
                "like solo (los likes pesan menos en el algoritmo de X). El RT "
                "además no quita el like si la cuenta ya lo tenía."
            ),
        )
    else:
        modo = "❤️ Solo Like"
        st.caption(
            "🐦 El modo «🔁 Like + RT» solo aplica a Twitter; en esta "
            "plataforma se usa «❤️ Solo Like»."
        )

    if st.button("❤️ Dar Likes", type="primary", key="btn_likes"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        
        if not cuentas_sel:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return

        # Baraja las cuentas: que el orden no sea siempre el mismo reduce el
        # patrón detectable de coordinación (muchas cuentas en fila fija).
        random.shuffle(cuentas_sel)

        progreso = st.progress(0)
        estado = st.empty()

        if modo.startswith("🔁"):
            # Like + RT: señal más rica que el like solo. `solo_retwittear`
            # acepta `dar_like=True` y no hace unlike si ya estaba likeado.
            def accion(bot):
                todos_ok = True
                for url in urls:
                    res = bot.solo_retwittear([url], bot.usuario, dar_like=True)
                    if isinstance(res, dict):
                        if (res.get("exitos") or 0) <= 0:
                            todos_ok = False
                    else:
                        todos_ok = False
                return todos_ok

            resultados = ejecutar_en_cuentas(
                cuentas_sel, accion, plataforma, progreso, estado, tipo="rt",
                pausa_entre=(2.0, 6.0),
            )
        else:
            def accion(bot):
                todos_ok = True
                for url in urls:
                    if not bot.like(url):
                        todos_ok = False
                return todos_ok

            resultados = ejecutar_en_cuentas(
                cuentas_sel, accion, plataforma, progreso, estado, tipo="like",
                pausa_entre=(2.0, 6.0),
            )
        mostrar_resultados(resultados)