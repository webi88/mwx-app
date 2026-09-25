import streamlit as st
from web.ui import cabecera
from web.operaciones._helpers import cuentas_por_plataforma, ejecutar_en_cuentas, mostrar_resultados


# Motivos alineados al mapping de `TwitterBot.reportar_post`/`reportar_objetivo`
# (spam, hate, abuse, violence, sensitive, impersonation, self_harm).
# `sensitive` reemplaza al antiguo `nudity`, que el bot no reconocia y caia al
# default "Spam". `false_info` se conserva: el bot lo mapeara cuando exista.
MOTIVOS = {
    "spam": "Spam",
    "hate": "Odio o acoso",
    "abuse": "Abuso",
    "violence": "Violencia",
    "sensitive": "Contenido sensible / sexual",
    "false_info": "Información falsa",
    "impersonation": "Suplantación de identidad",
    "self_harm": "Autolesión",
}

# Ayuda de formatos aceptados (una URL por línea).
AYUDA_FORMATOS = (
    "Una URL por línea. En X acepta publicaciones "
    "(https://x.com/usuario/status/123...) y cuentas/perfiles "
    "(https://x.com/usuario)."
)


def _reportar_objetivo_con_bot(bot, url: str, motivo: str) -> bool:
    """Reporta `url` (publicación o cuenta) con un bot ya creado.

    Prefiere `reportar_objetivo(url, motivo)` cuando el bot lo expone
    (TwitterBot nuevo: distingue publicaciones de perfiles con la misma
    llamada) y cae a `reportar_post(url, motivo)` para bots viejos y para las
    plataformas que solo tienen `reportar_post` (Facebook, Instagram, TikTok).
    Devuelve False si el reporte falla, para que el flujo continúe con las
    demás cuentas/URLs."""
    reportar_objetivo = getattr(bot, "reportar_objetivo", None)
    if callable(reportar_objetivo):
        return bool(reportar_objetivo(url, motivo))
    return bool(bot.reportar_post(url, motivo))


def render(usuario: dict):
    cabecera(
        "🚩 REPORTAR POSTS O CUENTAS",
        "Reporta publicaciones o perfiles en X con tus cuentas",
    )
    
    plataforma = st.selectbox(
        "Plataforma",
        ["twitter", "instagram", "facebook", "tiktok"],
        key="rep_plataforma",
        format_func=lambda p: {
            "twitter": "🐦 Twitter", "instagram": "📷 Instagram",
            "facebook": "📘 Facebook", "tiktok": "🎵 TikTok",
        }[p],
    )
    
    cuentas = cuentas_por_plataforma(plataforma)
    if not cuentas:
        st.warning(f"No hay cuentas activas de {plataforma}.")
        return
    
    cuenta_opts = {f"@{c.usuario} (Grupo {c.grupo})": c for c in cuentas}
    seleccionadas = st.multiselect("Cuentas", list(cuenta_opts), key="rep_cuentas")
    
    motivo_labels = list(MOTIVOS.values())
    motivo_sel = st.selectbox("Motivo del reporte", motivo_labels, key="rep_motivo")
    motivo = [k for k, v in MOTIVOS.items() if v == motivo_sel][0]
    
    st.caption(AYUDA_FORMATOS)
    urls_text = st.text_area("URLs (una por línea)", height=120, key="rep_urls")

    urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
    cuentas_sel = [cuenta_opts[s] for s in seleccionadas]

    # Alcance ANTES de ejecutar: una cuenta reporta TODAS las URLs.
    if cuentas_sel and urls:
        st.info(
            f"Se harán {len(cuentas_sel) * len(urls)} reportes "
            f"({len(cuentas_sel)} cuentas × {len(urls)} URLs)."
        )

    if st.button("🚩 Reportar", type="primary", key="btn_reportar"):

        if not cuentas_sel:
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
                if not _reportar_objetivo_con_bot(bot, url, motivo):
                    todos_ok = False
            return todos_ok

        # `reportar` no es un rol de publicacion: no cae en el blindaje por
        # Tier (Tier 2/3 pueden reportar; el tipo se etiqueta correctamente).
        resultados = ejecutar_en_cuentas(
            cuentas_sel, accion, plataforma, progreso, estado, tipo="reportar"
        )
        mostrar_resultados(resultados)
