import json
from datetime import datetime, timedelta

import streamlit as st
from sqlalchemy import func

from core.database import get_db_session
from core.models import Cliente, AlertaHistorial, MencionDia, ReporteDiario
from ia.celulas import CelulasManager
from web.ui import cabecera, stat, divider, empty_state


# Clave de `st.session_state` con el ultimo resultado de una ejecucion manual.
# Se escribe ANTES del `st.rerun()`: antes el rerun borraba las cifras recien
# pintadas y el usuario nunca veia mencionadas/enviadas/filtradas/duplicadas
# ni los errores de envio que devuelve el motor.
CLAVE_RESULTADO_ALERTAS = "alertas_ultimo_resultado"

# Caption de estado (sin intervalos fijos: el job 24/7 vive en el scheduler).
CAPTION_AUTOMATICO = (
    "🔁 En Railway las alertas también se ejecutan de forma automática cada "
    "pocas horas mediante el job del scheduler (según configuración). Este "
    "botón es para forzar una ejecución manual inmediata."
)

# Aviso cuando el motor detecta alertas pero NO las envía a Telegram
# (`ALERTAS_ENVIAR_TELEGRAM` desactivado): solo se registran en el dashboard.
# El motor lo reporta con `envio_pausado=True` en el resultado.
TEXTO_ENVIO_PAUSADO = (
    "⏸️ Envío a Telegram en pausa (ALERTAS_ENVIAR_TELEGRAM=0): las alertas se "
    "están detectando y registrando en este dashboard, pero NO se envían a los "
    "grupos. Pide a quien administra activar el envío cuando quieras que lleguen."
)


def _inicio_del_dia() -> datetime:
    """Medianoche de hoy SIN segundos ni microsegundos.

    Los conteos de "hoy" usaban `replace(hour=0, minute=0)` y conservaban los
    segundos/microsegundos actuales, por lo que perdían las alertas del primer
    instante del día. Se centraliza aquí para el resumen y por cliente."""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _flag_tolerante(valor) -> bool:
    """Convierte un flag del resultado del motor a `bool` sin lanzar.

    Acepta `True`/`False`, `1`/`0` y strings (`"1"`, `"true"`, `"si"`, `"on"`,
    etc.); ausente/None => False. El motor puede devolver `envio_pausado` como
    booleano o como 0/1 según cómo lo serialice."""
    if isinstance(valor, str):
        return valor.strip().lower() in ("1", "true", "si", "sí", "yes", "on")
    return bool(valor)


def _normalizar_resultado(resultados, cliente: str = "", horas=None,
                          iniciada: str = "") -> dict:
    """Resultado homogéneo para la UI, tolerante a motores viejos/nuevos.

    Acepta `None`, diccionarios parciales o claves alternativas
    (`mencionadas`/`total`/`menciones`) y errores como str, lista o dict
    (`errores`, `errores_envio`, `fallos`). Conserva `envio_pausado` (si el
    motor no lo trae, queda en False). Nunca lanza."""
    datos = resultados if isinstance(resultados, dict) else {}

    def _entero(*claves) -> int:
        for clave in claves:
            if clave in datos:
                try:
                    return int(datos.get(clave) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    errores: list = []
    for clave in ("errores", "errores_envio", "fallos"):
        valor = datos.get(clave)
        if not valor:
            continue
        if isinstance(valor, str):
            errores = [valor]
        elif isinstance(valor, dict):
            errores = [f"{k}: {v}" for k, v in valor.items()]
        elif isinstance(valor, (list, tuple, set)):
            errores = [str(e) for e in valor if str(e).strip()]
        if errores:
            break

    return {
        "cliente": cliente,
        "horas": horas,
        "fecha": iniciada,
        "mencionadas": _entero("mencionadas", "total", "menciones"),
        "total": _entero("total", "mencionadas", "menciones"),
        "enviadas": _entero("enviadas"),
        "filtradas": _entero("filtradas"),
        "duplicadas": _entero("duplicadas"),
        "errores": errores[:20],
        "total_errores": len(errores),
        "error": str(datos.get("error") or ""),
        # Envio a Telegram en pausa (`ALERTAS_ENVIAR_TELEGRAM` desactivado):
        # el motor lo reporta y la UI lo avisa. Ausente/False = comportamiento
        # actual (sin aviso).
        "envio_pausado": _flag_tolerante(datos.get("envio_pausado")),
    }


def _pares_conteo(datos) -> list:
    """Convierte `por_fuente`/`por_temas` en pares (etiqueta, cantidad).

    Tolera JSON viejo o ausente: `None`, dict, lista de pares o lista de dicts
    con `fuente`/`tema`/`titulo` + `cantidad`/`total`. Nunca lanza."""
    if isinstance(datos, dict):
        return [(str(k), v) for k, v in datos.items()]
    if isinstance(datos, (list, tuple)):
        pares = []
        for item in datos:
            try:
                if isinstance(item, dict):
                    etiqueta = (
                        item.get("fuente") or item.get("tema") or item.get("titulo")
                    )
                    cantidad = item.get("cantidad", item.get("total", 0))
                    if etiqueta:
                        pares.append((str(etiqueta), cantidad))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    pares.append((str(item[0]), item[1]))
            except Exception:
                continue
        return pares
    return []


def render(usuario: dict):
    cabecera("🚨 SISTEMA DE ALERTAS", "Monitoreo y vigilancia por cliente")

    st.markdown("## Resumen General")

    with get_db_session() as db:
        total_alertas = db.query(AlertaHistorial).count()
        alertas_hoy = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= _inicio_del_dia()
        ).count()
        alertas_7d = db.query(AlertaHistorial).filter(
            AlertaHistorial.fecha_envio >= datetime.now() - timedelta(days=7)
        ).count()
        clientes_total = db.query(Cliente).filter(Cliente.activo == True).count()

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat(alertas_hoy, "Alertas hoy")
    with c2:
        stat(alertas_7d, "Últimos 7 días")
    with c3:
        stat(total_alertas, "Historial total")
    with c4:
        stat(clientes_total, "Clientes")

    st.caption(CAPTION_AUTOMATICO)

    divider()

    tabs = st.tabs([
        "🔔 Alertas Recientes",
        "👥 Por Cliente",
        "📰 Por Fuente",
        "📝 Menciones del Día",
        "📊 Resúmenes",
        "📋 Keywords por Cliente",
        "⚙️ Ejecutar Alertas",
    ])

    with tabs[0]:
        _alertas_recientes()
    with tabs[1]:
        _alertas_por_cliente()
    with tabs[2]:
        _alertas_por_fuente()
    with tabs[3]:
        _menciones_dia()
    with tabs[4]:
        _resumenes()
    with tabs[5]:
        _keywords_clientes()
    with tabs[6]:
        _ejecutar_alertas()


def _alertas_recientes():
    st.markdown("### 🔔 Alertas Recientes")

    with get_db_session() as db:
        alertas = db.query(AlertaHistorial).order_by(
            AlertaHistorial.fecha_envio.desc()
        ).limit(50).all()

        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}

    if not alertas:
        empty_state("No hay alertas registradas todavía.")
        return

    for alerta in alertas:
        # Datos viejos/incompletos no deben tumbar la página.
        try:
            cliente_nombre = clientes_map.get(
                alerta.cliente_id, f"Cliente {alerta.cliente_id}"
            )
            fecha = (
                alerta.fecha_envio.strftime("%d/%m/%Y %H:%M")
                if alerta.fecha_envio else "sin fecha"
            )
            fuente = alerta.fuente or "sin fuente"
            url = alerta.url or ""
            with st.container(border=True):
                st.markdown(f"**{cliente_nombre}** · {fuente} · 🕐 {fecha}")
                if url:
                    st.markdown(f"🔗 [Abrir fuente]({url})")
                    st.caption(url[:120])
                else:
                    st.caption("Sin URL registrada.")
        except Exception:
            continue


def _alertas_por_cliente():
    st.markdown("### 👥 Alertas por Cliente")

    with get_db_session() as db:
        clientes = db.query(Cliente).filter(Cliente.activo == True).all()
        if not clientes:
            empty_state("No hay clientes configurados. Créalos en el sidebar.")
            return

        # Conteo agrupado (1 consulta para totales y 1 para hoy) en vez de
        # 2 consultas por cliente (N+1).
        ids = [c.id for c in clientes]
        totales = dict(
            db.query(AlertaHistorial.cliente_id, func.count(AlertaHistorial.id))
            .filter(AlertaHistorial.cliente_id.in_(ids))
            .group_by(AlertaHistorial.cliente_id)
            .all()
        )
        hoy_por_cliente = dict(
            db.query(AlertaHistorial.cliente_id, func.count(AlertaHistorial.id))
            .filter(
                AlertaHistorial.cliente_id.in_(ids),
                AlertaHistorial.fecha_envio >= _inicio_del_dia(),
            )
            .group_by(AlertaHistorial.cliente_id)
            .all()
        )

    for cliente in clientes:
        total = int(totales.get(cliente.id, 0) or 0)
        hoy = int(hoy_por_cliente.get(cliente.id, 0) or 0)
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(f"**{cliente.nombre}**")
                st.caption(cliente.localidad or "Sin localidad")
            with c2:
                st.metric("Hoy", hoy)
            with c3:
                st.metric("Total", total)


def _alertas_por_fuente():
    st.markdown("### 📰 Alertas por Fuente")

    with get_db_session() as db:
        fuentes = db.query(AlertaHistorial.fuente, func.count(
            AlertaHistorial.id
        )).group_by(AlertaHistorial.fuente).all()

    if not fuentes:
        empty_state("Sin datos de fuentes.")
        return

    datos = {"fuentes": [], "cantidades": []}
    for fuente, cantidad in fuentes:
        datos["fuentes"].append(fuente or "desconocida")
        datos["cantidades"].append(int(cantidad or 0))

    st.bar_chart({"Fuente": datos["fuentes"], "Cantidad": datos["cantidades"]},
                 x="Fuente", y="Cantidad")

    st.markdown("### Detalle por fuente")
    for fuente, cantidad in fuentes:
        st.markdown(f"**{fuente or 'desconocida'}**: {cantidad} alertas")


def _menciones_dia():
    st.markdown("### 📝 Menciones del Día")

    hoy = datetime.now().date()

    with get_db_session() as db:
        menciones = db.query(MencionDia).filter(
            MencionDia.fecha == hoy
        ).order_by(MencionDia.es_principal.desc()).all()

        clientes_map = {c.id: c.nombre for c in db.query(Cliente).all()}

    if not menciones:
        empty_state("Sin menciones registradas hoy.")
        return

    st.caption(
        f"{len(menciones)} mención(es) registradas el {hoy.strftime('%d/%m/%Y')}"
    )

    for mencion in menciones:
        # Tolerante a datos viejos/parciales: una fila rota no tumba la página.
        try:
            cliente_nombre = clientes_map.get(
                mencion.cliente_id, f"Cliente {mencion.cliente_id}"
            )
            principal = "⭐ " if mencion.es_principal else ""
            titulo = mencion.titulo or "(sin título)"
            fuente = mencion.fuente or "sin fuente"
            with st.container(border=True):
                st.markdown(f"{principal}**{titulo}** — {cliente_nombre} ({fuente})")
                if mencion.kw_principal:
                    st.caption(f"Keyword principal: `{mencion.kw_principal}`")
                if mencion.resumen:
                    st.markdown(mencion.resumen)
                if mencion.enlace:
                    st.markdown(f"[Abrir enlace]({mencion.enlace})")
                else:
                    st.caption("Sin enlace registrado.")
        except Exception:
            continue


def _resumenes():
    st.markdown("### 📊 Resúmenes por Cliente")

    with get_db_session() as db:
        clientes = db.query(Cliente).filter(Cliente.activo == True).all()
        if not clientes:
            empty_state("No hay clientes activos.")
            return

        # Último reporte por cliente con UNA sola consulta (antes: N+1).
        ids = [c.id for c in clientes]
        reportes = (
            db.query(ReporteDiario)
            .filter(ReporteDiario.cliente_id.in_(ids))
            .order_by(ReporteDiario.fecha.desc())
            .all()
        )

    reportes_map = {}
    for reporte in reportes:
        reportes_map.setdefault(reporte.cliente_id, reporte)

    if not reportes_map:
        empty_state("No hay resúmenes generados todavía.")
        return

    for cliente in clientes:
        reporte = reportes_map.get(cliente.id)
        if not reporte:
            continue
        fecha = reporte.fecha.strftime("%d/%m/%Y") if reporte.fecha else "sin fecha"
        enviado = " · ✅ Enviado a Telegram" if reporte.enviado else ""
        with st.expander(f"📊 {cliente.nombre} — {fecha}"):
            st.caption(f"Fecha de generación: {fecha}{enviado}")
            st.markdown(f"**Total alertas:** {reporte.total_alertas or 0}")

            fuentes = _pares_conteo(reporte.por_fuente)
            if fuentes:
                st.markdown("**Por fuente:**")
                for fuente, cant in fuentes:
                    st.markdown(f"  - {fuente}: {cant}")
            else:
                st.caption("Sin desglose por fuente en este reporte.")

            temas = _pares_conteo(reporte.por_temas)
            if temas:
                st.markdown("**Por temas:**")
                for tema, cant in temas:
                    st.markdown(f"  - {tema}: {cant}")
            else:
                st.caption("Sin desglose por temas en este reporte.")


def _keywords_clientes():
    st.markdown("### 🔑 Keywords por Cliente")

    manager = CelulasManager()
    clientes = manager.obtener_clientes()

    if not clientes:
        empty_state("No hay clientes.")
        return

    for cliente in clientes:
        try:
            keywords = json.loads(cliente.keywords) if cliente.keywords else []
        except Exception:
            keywords = []

        with st.expander(f"🔍 **{cliente.nombre}** — {len(keywords)} keywords"):
            if keywords:
                st.markdown(", ".join(f"`{k}`" for k in keywords))
            else:
                st.caption("Sin keywords configuradas.")
            st.caption(f"Localidad: {cliente.localidad or '—'}")
            if cliente.exclude_terms:
                try:
                    excl = json.loads(cliente.exclude_terms)
                    if excl:
                        st.caption(f"Excluir: {', '.join(excl)}")
                except Exception:
                    pass


def _aviso_envio_pausado(datos) -> bool:
    """Muestra el aviso de envío a Telegram en pausa si el resultado lo trae.

    Con `envio_pausado` True (el motor detectó alertas pero no las envió porque
    `ALERTAS_ENVIAR_TELEGRAM` está desactivado) pinta un `st.warning` visible.
    Con False/ausente no hace nada (comportamiento actual intacto). Devuelve
    True si mostró el aviso."""
    if not isinstance(datos, dict) or not datos.get("envio_pausado"):
        return False
    st.warning(TEXTO_ENVIO_PAUSADO)
    return True


def _render_ultimo_resultado():
    """Pinta el resultado persistido tras el rerun (clave principal del fix).

    Tras pulsar "Ejecutar Alertas" el script hace `st.rerun()`; este bloque lee
    `st.session_state[CLAVE_RESULTADO_ALERTAS]` (guardado ANTES del rerun) y
    muestra las cifras y los errores de envío para que el usuario SÍ los vea.
    Si el motor reportó `envio_pausado=True`, además avisa que las alertas se
    registran en el dashboard pero NO se envían a Telegram. Se pinta dentro de
    la pestaña "⚙️ Ejecutar Alertas" (llamada en `_ejecutar_alertas`)."""
    datos = st.session_state.get(CLAVE_RESULTADO_ALERTAS)
    if not isinstance(datos, dict) or not datos:
        return

    # Aviso de envío pausado ANTES de las cifras, para que sea lo primero
    # que vea el operador. Con envio_pausado False/ausente no pinta nada.
    _aviso_envio_pausado(datos)

    fecha = str(datos.get("fecha") or "")
    st.success("Búsqueda completada" + (f" — {fecha}" if fecha else ""))

    cliente = datos.get("cliente") or "(todos los clientes)"
    horas = datos.get("horas")
    detalle = f"Cliente: {cliente}" + (f" · Ventana: {horas} h" if horas else "")
    st.caption(detalle)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        stat(datos.get("mencionadas", 0), "Menciones")
    with c2:
        stat(datos.get("enviadas", 0), "Enviadas")
    with c3:
        stat(datos.get("filtradas", 0), "Filtradas")
    with c4:
        stat(datos.get("duplicadas", 0), "Duplicadas")

    if datos.get("error"):
        st.error(f"❌ Error en la ejecución: {datos['error']}")

    errores = datos.get("errores") or []
    if errores:
        total = int(datos.get("total_errores", len(errores)) or len(errores))
        st.warning(f"⚠️ {total} error(es) de envío/consulta durante la ejecución:")
        for linea in errores[:10]:
            st.caption(f"• {linea}")
        if total > 10:
            st.caption(f"… y {total - 10} más.")

    if st.button("🧹 Limpiar resultado", key="btn_alertas_limpiar_resultado"):
        st.session_state.pop(CLAVE_RESULTADO_ALERTAS, None)
        st.rerun()


def _ejecutar_alertas():
    st.markdown("### ⚙️ Ejecutar Alertas")
    st.caption(CAPTION_AUTOMATICO)

    # Resultado de la ejecución anterior (si la hubo): sobrevive al rerun.
    _render_ultimo_resultado()

    manager = CelulasManager()
    clientes = manager.obtener_clientes()

    if not clientes:
        empty_state("No hay clientes configurados.")
        return

    horas = st.slider("Ventana de búsqueda (horas)", 6, 168, 24, key="alertas_horas")

    cliente_opts = {"(Todos los clientes)": None}
    for c in clientes:
        cliente_opts[c.nombre] = c.id
    sel = st.selectbox("Cliente", list(cliente_opts), key="alertas_sel_cliente")

    if st.button("🚀 Ejecutar Alertas", type="primary", key="btn_ejecutar_alertas"):
        etiqueta = "(Todos los clientes)" if cliente_opts[sel] is None else sel
        _ejecutar_ahora(cliente_opts[sel], horas, etiqueta)


def _ejecutar_ahora(cliente_id, horas: int, etiqueta: str):
    """Corre el motor (bloqueante, con spinner) y persiste el resultado.

    El resultado se guarda en `st.session_state` ANTES del `st.rerun()`: así
    las cifras y los errores se muestran en la siguiente pasada
    (`_render_ultimo_resultado`), en vez de perderse con el rerun."""
    resultado, error = None, ""
    with st.spinner(
        "Buscando en Google News y Twitter... esto puede tardar unos minutos."
    ):
        try:
            from alertas.motor import MotorAlertas

            motor = MotorAlertas()
            resultado = motor.ejecutar_alertas(cliente_id=cliente_id, horas=horas)
        except Exception as e:
            # Nunca tumbar la página: el error se muestra tras el rerun.
            error = f"{type(e).__name__}: {e}"

    datos = _normalizar_resultado(
        resultado,
        cliente=etiqueta,
        horas=horas,
        iniciada=datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
    )
    if error:
        datos["error"] = error
    st.session_state[CLAVE_RESULTADO_ALERTAS] = datos
    st.rerun()
