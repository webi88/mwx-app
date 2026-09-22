import io

import streamlit as st
from datetime import datetime
from core.database import get_db_session
from core.models import Cuenta, Celula, Cliente, Tarea, AlertaHistorial
from core.registro import obtener_acciones
from web.ui import cabecera, stat, divider


# Columnas de la "tabla de exitos" (mismo orden en pantalla y en el Excel).
COLUMNAS_EXITOS = ("Fecha", "Usuario", "Tipo", "URL publicación")


def _es_fila_vacia(fila: dict) -> bool:
    """True si TODOS los valores de la fila estan vacios (fila separadora)."""
    if not isinstance(fila, dict):
        return True
    return not any(str(valor or "").strip() for valor in fila.values())


def _filas_con_separacion(filas: list[dict]) -> list[dict]:
    """Intercala UNA fila vacia entre cada exito y el siguiente.

    Orden resultante: exito1, vacia, exito2, vacia, ..., exitoN. Sin fila
    vacia antes del primero ni despues del ultimo; con 0 o 1 exitos no hay
    ninguna vacia. Las filas separadoras tienen TODAS sus claves en "" (las
    mismas claves que usan los exitos). Es idempotente: si la lista ya viene
    separada, vuelve a dejar una sola fila vacia entre exitos.

    Args:
        filas: lista de dicts con los exitos ("Fecha", "Usuario", "Tipo",
            "URL publicación"). Nunca lanza.

    Returns:
        Nueva lista (no modifica la original) con los exitos en orden y las
        filas vacias intercaladas.
    """
    limpias = [
        fila
        for fila in (filas or [])
        if isinstance(fila, dict) and not _es_fila_vacia(fila)
    ]
    if not limpias:
        return []

    claves: list = []
    for fila in limpias:
        for clave in fila:
            if clave not in claves:
                claves.append(clave)

    vacia = {clave: "" for clave in claves}
    separadas: list = []
    for indice, fila in enumerate(limpias):
        if indice:
            separadas.append(dict(vacia))
        separadas.append(fila)
    return separadas


def _excel_exitos_bytes(filas: list[dict]) -> bytes:
    """Genera EN MEMORIA el .xlsx de exitos con una fila vacia en medio.

    Fila 1: cabeceras con estilo (negrita, fondo 1DA1F2 y letra blanca) y
    ancho 22 por columna. Luego las filas tal cual: un exito, una fila
    completamente vacia, el siguiente exito, etc. Las filas vacias se escriben
    sin valores (todas las celdas vacias).

    Args:
        filas: exitos crudos o ya separados (idempotente); se aplica
            `_filas_con_separacion` antes de escribir.

    Returns:
        Los bytes del .xlsx, o b"" si openpyxl falla (nunca lanza).
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        separadas = _filas_con_separacion(filas)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Exitos"
        ws.append(list(COLUMNAS_EXITOS))

        for columna in range(1, len(COLUMNAS_EXITOS) + 1):
            celda = ws.cell(row=1, column=columna)
            celda.font = Font(bold=True, color="FFFFFF")
            celda.fill = PatternFill("solid", fgColor="1DA1F2")
            celda.alignment = Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(columna)].width = 22

        for fila in separadas:
            ws.append([
                "" if fila.get(columna) is None else fila.get(columna, "")
                for columna in COLUMNAS_EXITOS
            ])

        buffer = io.BytesIO()
        wb.save(buffer)
        return buffer.getvalue()
    except Exception:
        return b""


def render(usuario: dict):
    cabecera("📊 REPORTES", "Estadísticas generales del sistema")
    
    st.markdown("## Resumen General")
    
    with get_db_session() as db:
        total_cuentas = db.query(Cuenta).count()
        activas = db.query(Cuenta).filter(Cuenta.activa == True).count()
        celulas = db.query(Celula).filter(Celula.activa == True).count()
        clientes = db.query(Cliente).filter(Cliente.activo == True).count()
        tareas_total = db.query(Tarea).count()
        tareas_pend = db.query(Tarea).filter(Tarea.estado == "pendiente").count()
        alertas = db.query(AlertaHistorial).count()
        alertas_hoy = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= datetime.now().replace(hour=0, minute=0)
        ).count()
        posts_hoy = db.query(Tarea).filter(
            Tarea.tipo == "post",
            Tarea.estado == "completada",
            Tarea.fecha_hora >= datetime.now().replace(hour=0, minute=0)
        ).count()
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat(f"{activas}/{total_cuentas}", "Cuentas activas")
    with c2:
        stat(celulas, "Células")
    with c3:
        stat(clientes, "Clientes")
    with c4:
        stat(tareas_pend, "Tareas pendientes")
    
    divider()
    
    c1, c2, c3 = st.columns(3)
    with c1:
        stat(posts_hoy, "Posts hoy")
    with c2:
        stat(alertas_hoy, "Alertas hoy")
    with c3:
        stat(alertas, "Alertas totales")
    
    divider()
    
    st.markdown("## Cuentas por Plataforma")
    
    with get_db_session() as db:
        por_plataforma = {}
        for c in db.query(Cuenta).all():
            por_plataforma.setdefault(c.plataforma, {"total": 0, "activas": 0})
            por_plataforma[c.plataforma]["total"] += 1
            if c.activa:
                por_plataforma[c.plataforma]["activas"] += 1
    
    if por_plataforma:
        for plat, datos in por_plataforma.items():
            st.markdown(f"**{plat}**: {datos['activas']}/{datos['total']} activas")
            st.progress(datos["activas"] / datos["total"] if datos["total"] else 0)

    divider()

    st.markdown("## Historial de acciones (mantenimientos / activaciones)")
    try:
        acciones = obtener_acciones(limit=50, solo_exitosas=True)
    except TypeError:
        # Compatibilidad mientras core/registro.py no tenga el parámetro:
        estados_ok = {"exito", "exitoso", "ok"}
        acciones = [
            a for a in obtener_acciones(limit=200)
            if (a.estado or "").strip().lower() in estados_ok
        ][:50]
    if acciones:
        filas = []
        for a in acciones:
            filas.append({
                "Fecha": a.fecha.strftime("%Y-%m-%d %H:%M") if a.fecha else "",
                "Usuario": a.usuario,
                "Tipo": a.tipo,
                "URL publicación": a.url_publicacion or "",
            })
        filas_mostradas = _filas_con_separacion(filas)
        st.caption("✅ Solo se muestran las acciones exitosas; las fallidas no se incluyen.")
        st.dataframe(filas_mostradas, use_container_width=True)
        datos_excel = _excel_exitos_bytes(filas_mostradas)
        if datos_excel:
            st.download_button(
                "⬇️ Descargar Excel (.xlsx)",
                data=datos_excel,
                file_name=f"exitos_{datetime.now():%Y%m%d_%H%M%S}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_reportes_exitos",
            )
        else:
            st.warning(
                "No se pudo generar el Excel (openpyxl no disponible). "
                "Puedes descargar el CSV desde el menú del dataframe."
            )
        # además muestra los enlaces clicables de las acciones exitosas con URL
        for a in acciones:
            if a.url_publicacion:
                st.markdown(f"🔗 @{a.usuario} — [{a.tipo}] [ver post]({a.url_publicacion})")
    else:
        st.info("Aún no hay acciones registradas.")