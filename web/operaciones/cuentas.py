"""Página de CUENTAS: carga masiva de credenciales, validación de sesión
(sin navegador, vía httpx) e inventario de cuentas de Twitter/X.

Reemplaza el flujo obsoleto de auto-registro con Grizzly SMS por la nueva
arquitectura: importación en lote (7 campos) + validador de sesión + inventario.
"""
import streamlit as st

from core.database import get_db_session
from core.models import Cuenta
from web.ui import cabecera


def _tiene_cookies(cookies_json) -> bool:
    """Devuelve True si la cuenta tiene cookies guardadas (no vacías)."""
    if not cookies_json:
        return False
    if isinstance(cookies_json, str):
        return bool(cookies_json.strip())
    if isinstance(cookies_json, (list, tuple)):
        return len(cookies_json) > 0
    return bool(cookies_json)


def _fmt_fecha(dt) -> str:
    """Formatea una fecha de last_checked de forma tolerante."""
    if not dt:
        return ""
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _inventario(status_filtro: str) -> list:
    """Devuelve el inventario de cuentas twitter como lista de dicts."""
    filas = []
    with get_db_session() as db:
        q = db.query(Cuenta).filter(Cuenta.plataforma == "twitter")
        if status_filtro and status_filtro != "todas":
            q = q.filter(Cuenta.status == status_filtro)
        cuentas = q.order_by(Cuenta.usuario).all()
        for c in cuentas:
            filas.append(
                {
                    "usuario": c.usuario,
                    "email": c.email or "",
                    "status": c.status or "",
                    "last_checked": _fmt_fecha(c.last_checked),
                    "cookies": "sí" if _tiene_cookies(c.cookies_json) else "no",
                }
            )
    return filas


def render(usuario):
    cabecera(
        "🗂️ CUENTAS",
        "Carga masiva de credenciales, validación de sesión e inventario",
    )

    # ===================== 1. IMPORTAR LOTE =====================
    st.markdown("### 📥 Importar lote de cuentas")
    st.caption(
        "Una cuenta por línea, en formato de 7 campos separados por `:`:\n"
        "`username:password:totp_secret:email:email_password:auth_token:cookies_base64` "
        "(el último campo es el JSON de cookies en base64)."
    )

    texto_lote = st.text_area("Pega aquí el lote (una cuenta por línea)", height=220)
    archivo = st.file_uploader("o sube un archivo .txt", type=["txt"])

    if st.button("🚀 Importar", type="primary", use_container_width=True):
        if archivo is not None:
            contenido = archivo.getvalue().decode("utf-8", errors="replace")
        else:
            contenido = texto_lote

        if not (contenido or "").strip():
            st.warning("No hay contenido para importar. Pega el lote o sube un archivo .txt.")
        else:
            from cuentas.importador import importar_lote

            res = importar_lote(contenido)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Importadas", res.get("importadas", 0))
            c2.metric("Actualizadas", res.get("actualizadas", 0))
            c3.metric("Errores", res.get("errores", 0))
            c4.metric("Total", res.get("total", 0))

            if res.get("detalle_errores"):
                with st.expander("⚠️ Errores de parseo"):
                    for err in res["detalle_errores"]:
                        st.markdown(f"- {err}")

    st.markdown("---")

    # ===================== 2. VALIDAR SESIONES =====================
    st.markdown("### 🔎 Validar sesiones (httpx, sin abrir navegador)")
    st.caption(
        "Hace peticiones reales a X vía proxy para detectar cuentas activas, "
        "expiradas, suspendidas o limitadas, y persiste `status` + `last_checked`."
    )

    alcance = st.selectbox(
        "Alcance",
        ["imported", "todas", "active", "expired", "suspended", "limited", "error"],
        help="'todas' valida todas las cuentas; el resto filtra por su estado actual.",
    )

    if st.button("🔎 Validar ahora", type="primary", use_container_width=True):
        try:
            from plataformas.twitter.session_validator import validar_todas

            solo = None if alcance == "todas" else alcance
            barra = st.progress(0.0)
            estado = st.empty()

            def _cb(actual, total, usuario, est):
                barra.progress(min(1.0, (actual / total) if total else 1.0))
                estado.write(f"🔎 @{usuario} → {est}")

            res = validar_todas(solo_status=solo, callback=_cb)
            estado.write("✅ Validación terminada")

            c1, c2, c3, c4, c5, c6 = st.columns(6)
            c1.metric("Activas", res.get("active", 0))
            c2.metric("Expiradas", res.get("expired", 0))
            c3.metric("Suspendidas", res.get("suspended", 0))
            c4.metric("Limitadas", res.get("limited", 0))
            c5.metric("Error", res.get("error", 0))
            c6.metric("Total", res.get("total", 0))
        except Exception as e:
            st.error(f"Falló la validación de sesiones: {e}")

    st.markdown("---")

    # ===================== 3. INVENTARIO =====================
    st.markdown("### 📋 Inventario de cuentas (Twitter)")

    filtro = st.selectbox(
        "Filtrar por status",
        ["todas", "imported", "active", "expired", "suspended", "limited", "error"],
    )

    filas = _inventario(filtro)
    if filas:
        st.dataframe(filas, use_container_width=True)
    else:
        st.info("No hay cuentas para mostrar con ese filtro.")

    st.markdown("---")

    # ===================== 4. EXPORTAR =====================
    st.markdown("### 📤 Exportar cuentas (login manual)")
    solo_activas = st.checkbox("Exportar solo cuentas activas", key="exp_solo_activas")

    import os
    from core.exportar import (
        exportar_cuentas_excel,
        exportar_cuentas_csv,
        INSTRUCCIONES_LOGIN,
    )

    col_xlsx, col_csv = st.columns(2)
    with col_xlsx:
        if st.button("⬇️ Descargar Excel (.xlsx)", key="btn_exp_xlsx",
                     use_container_width=True):
            res = exportar_cuentas_excel(solo_activas=solo_activas)
            if res.get("error"):
                st.error(res["error"])
            elif res.get("ruta"):
                bytes_ = open(res["ruta"], "rb").read()
                st.download_button(
                    "⬇️ Descargar Excel (.xlsx)",
                    data=bytes_,
                    file_name=os.path.basename(res["ruta"]),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="btn_dl_xlsx",
                )
    with col_csv:
        if st.button("⬇️ Descargar CSV", key="btn_exp_csv",
                     use_container_width=True):
            res = exportar_cuentas_csv(solo_activas=solo_activas)
            if res.get("error"):
                st.error(res["error"])
            elif res.get("ruta"):
                bytes_ = open(res["ruta"], "rb").read()
                st.download_button(
                    "⬇️ Descargar CSV",
                    data=bytes_,
                    file_name=os.path.basename(res["ruta"]),
                    mime="text/csv",
                    key="btn_dl_csv",
                )

    with st.expander("📖 Guía de acceso manual a X"):
        for linea in INSTRUCCIONES_LOGIN:
            st.markdown(linea)
