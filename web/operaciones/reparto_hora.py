"""Reparto por Hora: campaña 3+3+3 por cuenta.

Por cada cuenta y por cada hora de la ventana (campaña estándar):
    - 3 posts originales.
    - 3 comentarios/respuestas a los tweets objetivo.
    - 3 RTs del tweet principal (con su texto de cita como referencia).
    - 9 acciones en total, repartidas a lo largo de la ventana con pausas
      aleatorias (nunca en ráfaga).

Los 9 textos se generan con `ia.generador_contenido.
generar_pool_campana_por_cuenta` (atajo `generar_textos_campana_3_3_3`),
respetando el PERFIL de cada cuenta (Formal/Estructurado, Ciudadano
Promedio, Popular/Orgánico) y con hashtag obligatorio integrado en MEDIO
del texto. El plan se materializa como Tareas del scheduler (tipos
"post", "comentario" y "retweet") que el ejecutor corre a su hora.

Requiere URLs objetivo:
    - Retweets: el tweet principal a retwittear.
    - Comentarios: tweets a los que responder (el texto lo genera la IA).
"""
from datetime import datetime, timedelta

import streamlit as st
from loguru import logger

from web.ui import cabecera, empty_state
from web.operaciones._helpers import cuentas_por_plataforma


def render(usuario: dict):
    cabecera("⏰ Reparto por Hora", "Acciones orgánicas por cuenta, distribuidas en una hora")

    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        empty_state("No hay cuentas de Twitter activas.")
        return

    st.caption(
        "Cada cuenta objetivo recibe, **por cada hora** de la ventana: "
        "**3 posts + 3 comentarios + 3 RTs del tweet principal = 9 acciones**, "
        "repartidas a lo largo de la ventana con pausas aleatorias. "
        "Los 9 textos usan el perfil de cada cuenta y llevan hashtag en medio."
    )

    # ------------------ Paso 1: cuentas ------------------
    st.markdown("#### 1️⃣ Cuentas objetivo")
    perfiles = {}
    for c in cuentas:
        from core.perfiles import etiqueta_perfil

        perfiles[c.usuario] = etiqueta_perfil(getattr(c, "perfil_personalidad", ""))

    opciones = {
        f"@{c.usuario} · {perfiles[c.usuario]}": c for c in cuentas
    }
    seleccion_nombres = st.multiselect(
        "Cuentas (por defecto, todas las de la operación)",
        list(opciones),
        default=list(opciones),
        key="rh_cuentas",
    )
    seleccion = [opciones[n] for n in seleccion_nombres]

    sin_perfil = [c.usuario for c in seleccion if not (getattr(c, "perfil_personalidad", "") or "").strip()]
    if sin_perfil:
        st.warning(
            f"⚠️ {len(sin_perfil)} cuenta(s) no tienen perfil de redacción asignado. "
            "Corrige en **🗂️ Cuentas → 🎭 Perfiles** para que el contenido respete "
            "el formato (Formal / Ciudadano / Popular)."
        )

    # ------------------ Paso 2: ritmo ------------------
    st.markdown("#### 2️⃣ Ritmo por hora (3+3+3)")
    st.caption(
        "Campaña estándar: **3 posts + 3 comentarios + 3 RTs del tweet "
        "principal = 9 acciones** por cuenta y por hora, repartidas en la "
        "ventana con pausas aleatorias. Puedes ajustar los números, pero el "
        "valor pedido es 3+3+3."
    )
    col_p, col_c, col_r, col_vent = st.columns(4)
    with col_p:
        n_posts = st.number_input(
            "Posts/hora", 0, 10, 3, key="rh_posts",
            help="Tweets originales por cuenta y hora.",
        )
    with col_c:
        n_comentarios = st.number_input(
            "Comentarios/hora", 0, 10, 3, key="rh_comentarios",
            help="Respuestas a los tweets de 'URLs para comentar'.",
        )
    with col_r:
        n_rts = st.number_input(
            "RTs del principal/hora", 0, 10, 3, key="rh_rts",
            help="Retweets del tweet principal por cuenta y hora.",
        )
    with col_vent:
        ventana = st.number_input(
            "Ventana (min)", 30, 240, 60, step=10, key="rh_ventana"
        )

    n_posts, n_comentarios, n_rts = int(n_posts), int(n_comentarios), int(n_rts)
    st.caption(
        f"Por hora y cuenta: **{n_posts} posts + {n_comentarios} "
        f"comentarios + {n_rts} RTs** = "
        f"{n_posts + n_comentarios + n_rts} acciones."
    )

    if n_posts + n_comentarios + n_rts <= 0:
        st.error(
            "Configura al menos 1 acción por hora (posts, comentarios o RTs)."
        )
        return

    st.markdown("#### 3️⃣ URLs objetivo + texto de la campaña")
    st.caption(
        "Los RT van al **tweet principal** (pégalo en 'URLs para retweets') y "
        "los comentarios responden a 'URLs para comentar'. Se rotan "
        "aleatoriamente entre todas las cuentas. El texto base de la cita es "
        "opcional: si lo das, las 3 citas son variaciones suyas; si no, son "
        "textos alternos del perfil de cada cuenta."
    )
    col_urls_rt, col_urls_com = st.columns(2)
    with col_urls_rt:
        urls_rt = st.text_area(
            "URLs para retweets — tweet principal (una por línea)",
            height=140,
            key="rh_urls_rt",
            placeholder="https://x.com/…/status/…",
        )
    with col_urls_com:
        urls_com = st.text_area(
            "URLs para comentar/responder (una por línea)",
            height=140,
            key="rh_urls_com",
            placeholder="https://x.com/…/status/…",
        )
    base_cita = st.text_area(
        "Texto base de la cita / campaña (opcional)",
        height=80,
        key="rh_base_cita",
        placeholder="Texto del tweet principal o idea de la cita...",
    )

    st.markdown("#### 4️⃣ Horario")
    col_fecha, col_hora = st.columns(2)
    with col_fecha:
        fecha_base = st.date_input(
            "Fecha de inicio",
            value=(datetime.now() + timedelta(hours=1)).date(),
            key="rh_fecha",
        )
    with col_hora:
        hora_base = st.time_input(
            "Hora de inicio",
            value=(datetime.now() + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            ).time(),
            key="rh_hora",
        )

    inicio_dt = datetime.combine(fecha_base, hora_base)
    if inicio_dt <= datetime.now():
        st.error("La hora de inicio debe ser futura (el scheduler solo ejecuta tareas vencidas).")
        return

    st.caption(
        f"🕒 Primer arranque: **{inicio_dt:%d/%m/%Y %H:%M}** · cada cuenta "
        f"arranca con un desfase aleatorio de hasta 5 min · ventana individual "
        f"de {int(ventana)} min."
    )

    # ------------------ Paso 5: generar plan ------------------
    st.markdown("#### 5️⃣ Preparar y programar")
    resumen_previo = st.session_state.pop("rh_resumen", None)
    if resumen_previo:
        st.success(
            f"✅ Plan generado: {resumen_previo.get('acciones', 0)} acciones · "
            f"{resumen_previo.get('posts', 0)} posts · "
            f"{resumen_previo.get('comentarios', 0)} comentarios · "
            f"{resumen_previo.get('retweets', 0)} retweets."
        )

    col_b1, col_b2, col_b3 = st.columns(3)
    with col_b1:
        preparar = st.button(
            "🧠 Preparar plan (generar textos)",
            type="primary",
            key="rh_preparar",
            disabled=not seleccion,
        )
    with col_b2:
        programar = st.button(
            "✅ Programar en el scheduler",
            key="rh_programar",
            disabled="rh_plan" not in st.session_state,
        )
    with col_b3:
        limpiar = st.button("🗑️ Descartar plan", key="rh_limpiar")

    if limpiar:
        st.session_state.pop("rh_plan", None)
        st.session_state.pop("rh_pool", None)
        st.session_state.pop("rh_preview", None)
        st.session_state.pop("rh_resumen", None)
        st.rerun()

    if preparar:
        urls_rt_list = _parsear_urls(urls_rt)
        urls_com_list = _parsear_urls(urls_com)
        if int(n_rts) > 0 and not urls_rt_list:
            st.error("Pega al menos una URL para los retweets (el tweet principal).")
        elif int(n_comentarios) > 0 and not urls_com_list:
            st.error(
                "Pega al menos una URL para los comentarios "
                "(o pon Comentarios/hora en 0)."
            )
        else:
            _preparar(
                seleccion,
                inicio_dt,
                n_posts=int(n_posts),
                n_comentarios=int(n_comentarios),
                n_rts=int(n_rts),
                ventana=int(ventana),
                urls_rt=urls_rt_list,
                urls_com=urls_com_list,
                base_cita=(base_cita or "").strip(),
            )

    plan = st.session_state.get("rh_plan")
    if plan:
        _preview(st.session_state.get("rh_preview") or [])
        if programar:
            _programar(plan, usuario)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _parsear_urls(texto: str) -> list[str]:
    return [linea.strip() for linea in str(texto or "").splitlines() if linea.strip()]


def _perfiles_normalizados(seleccion: list) -> dict:
    """Devuelve {usuario: perfil canonico} para las cuentas seleccionadas."""
    from core.perfiles import normalizar_perfil

    return {
        c.usuario: normalizar_perfil(getattr(c, "perfil_personalidad", ""))
        for c in seleccion
    }


def _preparar(
    seleccion: list,
    inicio_dt: datetime,
    n_posts: int,
    n_comentarios: int,
    n_rts: int,
    ventana: int,
    urls_rt: list[str],
    urls_com: list[str],
    base_cita: str = "",
):
    import random

    from scheduler.distribucion_horaria import (
        construir_plan_completo,
        plan_hora_cuenta,
    )

    usuarios = [c.usuario for c in seleccion]
    perfil_por_usuario = _perfiles_normalizados(seleccion)

    n_posts = max(0, int(n_posts))
    n_comentarios = max(0, int(n_comentarios))
    n_rts = max(0, int(n_rts))

    # 1) Distribucion de acciones y horarios (3+3+3 por defecto).
    orden = []
    for usuario in usuarios:
        rng = random.Random(hash((usuario, inicio_dt.isoformat())) & 0xFFFFFFFF)
        orden.extend(
            plan_hora_cuenta(
                usuario,
                perfil_por_usuario.get(usuario, ""),
                inicio_dt,
                n_posts=n_posts,
                n_comentarios=n_comentarios,
                n_rts=n_rts,
                ventana_minutos=int(ventana),
                rng=rng,
            )
        )

    # 2) 9 textos por cuenta con la campaña 3+3+3 (SOLO llamada a ia/, el
    #    modulo ia/ no se edita desde el dashboard).
    cuentas_info = [
        {
            "usuario": c.usuario,
            "registro": getattr(c, "tipo_cuenta", "") or "",
            "personalidad": getattr(c, "personalidad", "") or "",
            "seccion": getattr(c, "seccion", "") or "",
            "nombre": getattr(c, "nombre_mostrado", "") or c.usuario,
            "perfil": perfil_por_usuario.get(c.usuario, ""),
        }
        for c in seleccion
    ]

    progreso = st.progress(0.0)
    estado = st.empty()
    total_estimado = max(1, len(seleccion) * (n_posts + n_comentarios + n_rts))

    def _cb(hechas, total):
        try:
            progreso.progress(min(1.0, float(hechas) / max(1, int(total or total_estimado))))
            estado.caption(f"✍️ Generando campaña 3+3+3: {hechas}/{total}...")
        except Exception:
            pass

    try:
        from ia.generador_contenido import generar_pool_campana_por_cuenta

        pool = generar_pool_campana_por_cuenta(
            cuentas_info,
            n_posts=n_posts,
            n_comentarios=n_comentarios,
            n_citas=n_rts,
            base_cita=base_cita or "",
            callback=_cb,
        )
    except TypeError:
        # Firma no disponible: atajo fijo 3+3+3 del mismo modulo.
        from ia.generador_contenido import generar_textos_campana_3_3_3

        pool = generar_textos_campana_3_3_3(
            cuentas_info,
            base_cita=base_cita or "",
            callback=_cb,
        )
    except Exception as e:
        logger.exception(f"Error generando campaña 3+3+3: {e}")
        st.error(f"Error generando textos: {e}")
        return

    textos_posts: dict[str, list] = {}
    textos_com: dict[str, list] = {}
    textos_citas: dict[str, list] = {}
    for i, c in enumerate(seleccion):
        fila = (
            pool[i]
            if isinstance(pool, list) and i < len(pool) and isinstance(pool[i], dict)
            else {}
        )
        textos_posts[c.usuario] = [
            str(t).strip() for t in (fila.get("posts") or []) if str(t).strip()
        ]
        textos_com[c.usuario] = [
            str(t).strip()
            for t in (fila.get("comentarios") or [])
            if str(t).strip()
        ]
        textos_citas[c.usuario] = [
            str(t).strip() for t in (fila.get("citas") or []) if str(t).strip()
        ]

    progreso.progress(1.0)
    estado.caption("✅ Textos listos (3 posts + 3 comentarios + 3 citas por cuenta).")

    # 3) Asignar contenido y fecha a cada accion.
    plan = construir_plan_completo(
        orden,
        posts_por_cuenta=textos_posts,
        comentarios_por_cuenta=textos_com,
        urls_rt=urls_rt,
        urls_comentario=urls_com,
    )

    # Las citas son los textos de los RTs del principal: se guardan en el
    # 'texto' de cada accion retweet para el preview (el scheduler programa
    # el RT simple con la URL; el texto queda como referencia de la cita).
    citas_restantes = {u: list(v) for u, v in textos_citas.items()}
    for p in plan:
        if p.get("tipo") == "retweet":
            bolsa = citas_restantes.get(p.get("usuario")) or []
            if bolsa:
                p["texto"] = bolsa.pop(0)

    faltantes = {
        "sin_post": sum(
            1 for p in plan if p["tipo"] == "post" and not (p.get("texto") or "").strip()
        ),
        "sin_comentario": sum(
            1
            for p in plan
            if p["tipo"] == "comentario"
            and not (p.get("texto") or "").strip()
        ),
        "sin_cita": sum(
            1
            for p in plan
            if p["tipo"] == "retweet"
            and not (p.get("texto") or "").strip()
        ),
    }

    st.session_state["rh_plan"] = plan
    st.session_state["rh_pool"] = {
        c.usuario: {
            "posts": textos_posts.get(c.usuario, []),
            "comentarios": textos_com.get(c.usuario, []),
            "citas": textos_citas.get(c.usuario, []),
        }
        for c in seleccion
    }
    st.session_state["rh_preview"] = _resumen_preview(plan, seleccion)
    st.session_state["rh_resumen"] = {
        "acciones": len(plan),
        "posts": sum(1 for p in plan if p["tipo"] == "post"),
        "comentarios": sum(1 for p in plan if p["tipo"] == "comentario"),
        "retweets": sum(1 for p in plan if p["tipo"] == "retweet"),
        "faltantes": faltantes,
    }
    st.rerun()


def _resumen_preview(plan: list, seleccion: list) -> list[dict]:
    """Una fila por cuenta con el desglose 3+3+3 y el rango horario."""
    por_usuario: dict[str, list] = {}
    for p in plan:
        por_usuario.setdefault(p["usuario"], []).append(p)

    from core.perfiles import etiqueta_perfil

    perfil_por_usuario = {
        c.usuario: etiqueta_perfil(getattr(c, "perfil_personalidad", ""))
        for c in seleccion
    }

    filas = []
    for usuario, acciones in por_usuario.items():
        fechas = [a["fecha_hora"] for a in acciones if a.get("fecha_hora")]
        if not fechas:
            continue
        acciones_ordenadas = sorted(acciones, key=lambda a: a["fecha_hora"])
        primera, ultima = fechas[0], fechas[-1]
        citas_con_texto = sum(
            1
            for a in acciones
            if a["tipo"] == "retweet" and (a.get("texto") or "").strip()
        )
        filas.append(
            {
                "cuenta": f"@{usuario}",
                "perfil": perfil_por_usuario.get(usuario, ""),
                "posts": sum(1 for a in acciones if a["tipo"] == "post"),
                "comentarios": sum(1 for a in acciones if a["tipo"] == "comentario"),
                "retweets": sum(1 for a in acciones if a["tipo"] == "retweet"),
                "citas c/texto": citas_con_texto,
                "inicio": primera.strftime("%H:%M:%S"),
                "fin": ultima.strftime("%H:%M:%S"),
                "texto_ejemplo": " · ".join(
                    " ".join(str(a.get("texto") or "").split())[:70]
                    for a in acciones_ordenadas
                    if a["tipo"] == "post"
                )[:140],
            }
        )
    return filas


def _preview(filas: list[dict]):
    st.markdown("#### 📋 Plan preparado (3+3+3 por cuenta)")
    if not filas:
        st.info("El plan no tiene acciones previsualizables.")
        return
    try:
        import pandas as pd

        st.dataframe(
            pd.DataFrame(filas),
            hide_index=True,
            use_container_width=True,
        )
    except Exception:
        for f in filas[:50]:
            st.markdown(
                f"**{f['cuenta']}** · {f['perfil']} · posts {f['posts']} · "
                f"comentarios {f['comentarios']} · RTs {f['retweets']} · "
                f"{f['inicio']}–{f['fin']}"
            )
    st.caption(
        "Cada fila es una cuenta; el scheduler ejecuta cada acción en su minuto "
        "(requiere el proceso del scheduler corriendo)."
    )

    # Detalle por cuenta con los 9 textos (3 posts + 3 comentarios + 3 citas
    # de los RTs del principal) generados por `generar_pool_campana_por_cuenta`.
    pool = st.session_state.get("rh_pool") or {}
    if not pool:
        return
    st.markdown("#### 📝 Textos por cuenta (9 por cuenta)")
    for usuario in sorted(pool):
        detalle = pool.get(usuario) or {}
        posts = detalle.get("posts") or []
        comentarios = detalle.get("comentarios") or []
        citas = detalle.get("citas") or []
        total = len(posts) + len(comentarios) + len(citas)
        with st.expander(f"@{usuario} — {total} textos", expanded=False):
            st.markdown("**📝 Posts**")
            for i, t in enumerate(posts, start=1):
                st.markdown(f"{i}. {t}")
            if not posts:
                st.caption("(sin posts)")
            st.markdown("**💬 Comentarios**")
            for i, t in enumerate(comentarios, start=1):
                st.markdown(f"{i}. {t}")
            if not comentarios:
                st.caption("(sin comentarios)")
            st.markdown("**🔁 Citas (textos de los RTs del principal)**")
            for i, t in enumerate(citas, start=1):
                st.markdown(f"{i}. {t}")
            if not citas:
                st.caption("(sin citas)")


def _programar(plan: list, usuario: dict):
    from core.database import get_db_session
    from core.models import Cuenta
    from scheduler.manager import SchedulerManager

    usuarios = sorted({p["usuario"] for p in plan})
    try:
        with get_db_session() as db:
            filas = db.query(Cuenta).filter(Cuenta.usuario.in_(usuarios)).all()
            id_por_usuario = {c.usuario: c.id for c in filas}
    except Exception as e:
        st.error(f"No se pudieron cargar las cuentas: {e}")
        return

    # Materializa el plan como Tareas del scheduler (tolerante a caidas).
    try:
        manager = SchedulerManager()
    except Exception as e:
        logger.exception(f"No se pudo iniciar el scheduler: {e}")
        st.error(f"No se pudo iniciar el scheduler: {e}")
        return

    from core.models import Tarea

    programadas = omitidas = fallidas = 0
    for p in plan:
        usuario_cuenta = p["usuario"]
        cuenta_id = id_por_usuario.get(usuario_cuenta)
        if cuenta_id is None or p.get("fecha_hora") is None:
            omitidas += 1
            continue
        texto = (p.get("texto") or "").strip()
        url = (p.get("url") or "").strip()
        tipo = p["tipo"]
        if tipo in ("post", "comentario") and not texto:
            omitidas += 1
            continue
        if tipo in ("retweet", "comentario") and not url:
            omitidas += 1
            continue
        try:
            if tipo == "post":
                tarea = Tarea(
                    tipo="post",
                    plataforma="twitter",
                    contenido=texto,
                    cuentas_ids=str([cuenta_id]),
                    fecha_hora=p["fecha_hora"],
                    estado="pendiente",
                    creada_por=(usuario or {}).get("username"),
                )
            elif tipo == "comentario":
                import json as _json

                tarea = Tarea(
                    tipo="comentario",
                    plataforma="twitter",
                    contenido=_json.dumps({"url": url, "texto": texto}),
                    cuentas_ids=str([cuenta_id]),
                    fecha_hora=p["fecha_hora"],
                    estado="pendiente",
                    creada_por=(usuario or {}).get("username"),
                )
            else:  # retweet
                import json as _json

                tarea = Tarea(
                    tipo="retweet",
                    plataforma="twitter",
                    contenido=_json.dumps([url]),
                    cuentas_ids=str([cuenta_id]),
                    fecha_hora=p["fecha_hora"],
                    estado="pendiente",
                    creada_por=(usuario or {}).get("username"),
                )
            if manager.programar_tarea(tarea):
                programadas += 1
            else:
                fallidas += 1
        except Exception as e:
            logger.exception(f"Error programando accion del reparto horario: {e}")
            fallidas += 1

    if programadas:
        st.success(f"✅ {programadas} acción(es) programadas en el scheduler.")
    if fallidas:
        st.error(f"❌ {fallidas} acción(es) no se pudieron programar.")
    if omitidas:
        st.warning(f"⚠️ {omitidas} acción(es) se omitieron (sin cuenta, texto o URL).")
    st.caption(
        "Las acciones se ejecutan solas a su hora siempre que el scheduler esté "
        "corriendo (servicio standalone o proceso del dashboard)."
    )
    st.session_state.pop("rh_plan", None)
    st.session_state.pop("rh_pool", None)
    st.session_state.pop("rh_preview", None)
    st.session_state.pop("rh_resumen", None)
