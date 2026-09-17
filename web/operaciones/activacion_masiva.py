"""Operacion ACTIVACION MASIVA: cita masiva clasica + campana por roles + 3+3+3.

Pestana A ("🎯 Cita masiva"): quote-RTs aleatorizados sobre N cuentas con
`MotorActivacion.ejecutar` (mismo formulario de siempre).

Pestana B ("🗂️ Por roles (subcuentas)"): divide las cuentas twitter activas en
SUBCUENTAS por rol (`Cuenta.rol_activacion`, ver `core/roles.py`):
  - "cita"     -> Retweet con cita.
  - "hashtags" -> Hashtags y menciones.
  - "rt"       -> Retweet simple.
Permite asignar el rol a la seleccion (selector masivo de `web.operaciones.
cuentas`), repartir en 3 tercios automaticamente, ver conteos/ejemplos y
lanzar `MotorActivacion.ejecutar_por_roles`. Las cuentas sin rol se saltan.

Pestana C ("📋 Campaña 3+3+3"): por cuenta 3 posts + 3 comentarios + 3 RTs
del tweet principal (9 acciones). Genera los 9 textos con
`ia.generador_contenido.generar_pool_campana_por_cuenta` (atajo
`generar_textos_campana_3_3_3`), muestra el preview por cuenta y programa
las 9 acciones en el scheduler con `scheduler.distribucion_horaria`
(igual que "⏰ Reparto por Hora").
"""
import streamlit as st

from web.ui import cabecera
from core.config import settings

OPCION_SIN_ROL = "Sin rol"

# Orden canonico de los roles en la UI (core/roles.ROLES_ACTIVACION).
ORDEN_ROLES = ("cita", "hashtags", "rt")


def _navegadores_default() -> int:
    """Valor por defecto (1-30) de "Navegadores simultáneos" (`MAX_BROWSERS`).

    `settings.max_browsers` viene de la variable de entorno y podría quedar
    fuera del rango del `number_input`; se acota para no romper el widget.
    """
    try:
        valor = int(settings.max_browsers)
    except (TypeError, ValueError):
        valor = 1
    return min(30, max(1, valor))


# ============================ LOGICA PURA ============================

def _repartir_tercios(usuarios: list) -> dict:
    """Reparte 'usuarios' (en orden) en 3 tercios contiguos: cita/hashtags/rt.

    Criterio exacto:
      - Se limpian valores vacios y se quitan duplicados conservando el primer
        orden de aparicion (se ignora un '@' inicial).
      - Con n usuarios se calcula divmod(n, 3); el resto se reparte de a uno a
        los primeros roles en el orden cita -> hashtags -> rt.
      - Los cortes son contiguos, por lo que ningun usuario se pierde y ninguno
        queda en dos roles a la vez.

    Ejemplos: 10 -> cita 4 / hashtags 3 / rt 3; 2 -> 1/1/0; 0 -> 0/0/0.
    """
    limpios, vistos = [], set()
    for u in (usuarios or []):
        if u is None:
            continue
        nombre = str(u).strip().lstrip("@")
        if not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        limpios.append(nombre)

    reparto = {"cita": [], "hashtags": [], "rt": []}
    n = len(limpios)
    if n == 0:
        return reparto

    base, resto = divmod(n, 3)
    tamanos = [base + (1 if i < resto else 0) for i in range(3)]
    inicio = 0
    for rol, tam in zip(ORDEN_ROLES, tamanos):
        reparto[rol] = limpios[inicio:inicio + tam]
        inicio += tam
    return reparto


def _conteo_por_rol(cuentas: list) -> dict:
    """Cuenta cuentas por rol normalizado; incluye la clave "" (sin rol)."""
    from core.roles import normalizar_rol_activacion

    conteo = {"cita": 0, "hashtags": 0, "rt": 0, "": 0}
    for fila in (cuentas or []):
        rol = normalizar_rol_activacion(fila.get("rol_activacion"))
        conteo[rol] = conteo.get(rol, 0) + 1
    return conteo


def _tabla_roles(cuentas: list, ejemplos: int = 8) -> list:
    """Filas (Rol, Subcuentas, Ejemplos) por rol para `st.dataframe`."""
    from core.roles import etiqueta_rol_activacion, normalizar_rol_activacion

    grupos = {"cita": [], "hashtags": [], "rt": [], "": []}
    for fila in (cuentas or []):
        rol = normalizar_rol_activacion(fila.get("rol_activacion"))
        grupos.setdefault(rol, []).append(fila.get("usuario") or "")

    filas = []
    for clave in ORDEN_ROLES + ("",):
        usuarios = grupos.get(clave) or []
        muestra = ", ".join(f"@{u}" for u in usuarios[:max(0, int(ejemplos))])
        if len(usuarios) > ejemplos:
            muestra += f" … (+{len(usuarios) - ejemplos})"
        filas.append(
            {
                "Rol": etiqueta_rol_activacion(clave),
                "Subcuentas": len(usuarios),
                "Ejemplos": muestra,
            }
        )
    return filas


def _roles_objetivo(cuentas: list, usuarios: list | None,
                    solo_roles: list | None) -> set:
    """Roles (no vacios) de las cuentas que la campana realmente procesara.

    Aplica los mismos filtros que `MotorActivacion._obtener_cuentas_por_rol`:
    usuarios (sin '@' y case-insensitive) y roles permitidos. Sirve para
    validar en la UI si se necesitan URLs y si hay al menos una cuenta con rol.
    """
    from core.roles import normalizar_rol_activacion

    base = cuentas or []
    if usuarios:
        deseados = {
            str(u).strip().lstrip("@").lower()
            for u in usuarios
            if str(u).strip()
        }
        base = [
            f for f in base
            if str(f.get("usuario") or "").strip().lstrip("@").lower() in deseados
        ]

    if solo_roles:
        permitidos = {normalizar_rol_activacion(r) for r in solo_roles}
        permitidos.discard("")
        base = [
            f for f in base
            if normalizar_rol_activacion(f.get("rol_activacion")) in permitidos
        ]

    return {
        normalizar_rol_activacion(f.get("rol_activacion"))
        for f in base
        if normalizar_rol_activacion(f.get("rol_activacion"))
    }


# ============================ ACCESO A DATOS ============================

def _cargar_cuentas_con_roles() -> list:
    """Cuentas twitter activas como dicts listos para `_selector_masivo`."""
    from core.database import get_db_session
    from core.models import Cuenta
    from core.registros import normalizar_tipo_cuenta
    from core.roles import normalizar_rol_activacion
    from core.secciones import normalizar_seccion

    try:
        with get_db_session() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.activa == True)
                .order_by(Cuenta.usuario)
                .all()
            )
            return [
                {
                    "usuario": c.usuario,
                    "status": c.status or "",
                    "seccion": normalizar_seccion(getattr(c, "seccion", "")),
                    "tipo_cuenta": normalizar_tipo_cuenta(
                        getattr(c, "tipo_cuenta", "")
                    ),
                    "handle_actual": (getattr(c, "handle_actual", "") or "").strip(),
                    "grupo": c.grupo or "",
                    "rol_activacion": normalizar_rol_activacion(
                        getattr(c, "rol_activacion", "")
                    ),
                }
                for c in cuentas
            ]
    except Exception as e:
        st.error(f"No se pudieron cargar las cuentas: {e}")
        return []


def _actualizar_roles(usuarios: list, codigo: str) -> int:
    """UPDATE masivo de `Cuenta.rol_activacion`; devuelve cuantas filas cambio.

    'codigo' se normaliza con `core.roles.normalizar_rol_activacion` ("" =
    sin rol). Ignora usuarios vacios o con '@'. No lanza: ante error devuelve 0
    y muestra el detalle en la UI.
    """
    from core.database import get_db_session
    from core.models import Cuenta
    from core.roles import normalizar_rol_activacion

    limpios = [
        str(u).strip().lstrip("@")
        for u in (usuarios or [])
        if str(u).strip()
    ]
    if not limpios:
        return 0
    codigo = normalizar_rol_activacion(codigo)
    try:
        with get_db_session() as db:
            return (
                db.query(Cuenta)
                .filter(
                    Cuenta.plataforma == "twitter",
                    Cuenta.usuario.in_(limpios),
                )
                .update({Cuenta.rol_activacion: codigo}, synchronize_session=False)
            )
    except Exception as e:
        st.error(f"No se pudieron asignar los roles: {e}")
        return 0


# ============================ UI: PANEL ============================

def _mostrar_panel_roles(cuentas: list):
    """Metricas + tabla de subcuentas por rol (sin lanzar nada)."""
    conteo = _conteo_por_rol(cuentas)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("💬 Retweet con cita", conteo.get("cita", 0))
    col2.metric("🏷️ Hashtags y menciones", conteo.get("hashtags", 0))
    col3.metric("🔁 Retweet simple", conteo.get("rt", 0))
    col4.metric("➖ Sin rol", conteo.get("", 0))
    st.dataframe(
        _tabla_roles(cuentas),
        use_container_width=True,
        hide_index=True,
    )


def _mostrar_resultados_roles(resultados: dict):
    """Metricas generales + por rol + detalle de una campana por roles."""
    from core.roles import etiqueta_rol_activacion

    st.markdown("---")
    metricas = [
        ("🎯 Total", resultados.get("total", 0)),
        ("✅ Exitosas", resultados.get("exitosas", 0)),
        ("❌ Fallidas", resultados.get("fallidas", 0)),
        ("➖ Sin rol (saltadas)", resultados.get("sin_rol", 0)),
    ]
    if "rondas" in resultados:
        metricas.append(("🔄 Rondas", resultados.get("rondas", 0)))
    if "sin_registro" in resultados:
        metricas.append(
            ("🪪 Sin registro (saltadas)", resultados.get("sin_registro", 0))
        )
    for col, (etiqueta, valor) in zip(st.columns(len(metricas)), metricas):
        col.metric(etiqueta, valor)

    st.markdown("#### 🗂️ Subcuentas por rol")
    por_rol = resultados.get("por_rol") or {}
    for rol in ORDEN_ROLES:
        info = por_rol.get(rol) or {"total": 0, "exitosas": 0, "fallidas": 0}
        st.markdown(f"**{etiqueta_rol_activacion(rol)}**")
        sub1, sub2, sub3 = st.columns(3)
        sub1.metric("Subcuentas", info.get("total", 0))
        sub2.metric("✅ Exitosas", info.get("exitosas", 0))
        sub3.metric("❌ Fallidas", info.get("fallidas", 0))

    detalles = resultados.get("detalles") or []
    if detalles:
        with st.expander(f"🔍 Detalle por cuenta ({len(detalles)})", expanded=False):
            filas = []
            for d in detalles:
                fila = {
                    "Usuario": f"@{d.get('usuario', '')}",
                    "Rol": etiqueta_rol_activacion(d.get("rol", "")),
                    "OK": "✅" if d.get("ok") else "❌",
                    "Detalle": d.get("detalle", ""),
                    "URL": d.get("url", ""),
                }
                if "ronda" in d:
                    fila["Ronda"] = d.get("ronda", "")
                filas.append(fila)
            st.dataframe(filas, use_container_width=True, hide_index=True)

    sin_rol_usuarios = resultados.get("sin_rol_usuarios") or []
    if sin_rol_usuarios:
        with st.expander(f"➖ Cuentas sin rol saltadas ({len(sin_rol_usuarios)})"):
            st.caption(", ".join(f"@{u}" for u in sin_rol_usuarios))

    sin_registro_usuarios = resultados.get("sin_registro_usuarios") or []
    sugerencia_registro = (resultados.get("sugerencia_registro") or "").strip()
    if sin_registro_usuarios or sugerencia_registro:
        with st.expander(
            f"🪪 Cuentas sin registro saltadas ({len(sin_registro_usuarios)})",
            expanded=False,
        ):
            if sugerencia_registro:
                st.info(sugerencia_registro)
            if sin_registro_usuarios:
                st.caption(", ".join(f"@{u}" for u in sin_registro_usuarios))


# ============================ PESTANAS ============================

def _cita_masiva():
    """Pestana A: quote-RTs masivos con variaciones (formulario original)."""
    st.markdown("Pega una URL de tweet por línea (objetivos a citar):")
    urls_text = st.text_area("URLs objetivo", height=100, key="act_urls")

    texto_base = st.text_area(
        "Texto base de la cita (se generan variaciones automáticas)",
        height=100,
        key="act_texto",
    )

    hashtags = st.text_input(
        "Hashtags para las citas (opcional, ej. #Mexico #4T)",
        key="act_hashtags",
        help="Se agregan a los retweets con cita (con '#' garantizado).",
    )

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        cantidad = st.number_input(
            "Cantidad de cuentas (vacío = todas)",
            min_value=0, max_value=1000, value=0, step=10, key="act_cant",
        )
    with col2:
        duracion_min = st.number_input(
            "Duración (min)", min_value=1, max_value=360, value=60, step=5, key="act_dur",
        )
    with col3:
        cohortes = st.number_input(
            "Cohortes", min_value=1, max_value=24, value=4, step=1, key="act_coh",
        )
    with col4:
        navegadores = st.number_input(
            "Navegadores simultáneos",
            min_value=1, max_value=30, value=_navegadores_default(), step=1,
            key="act_nav",
            help=(
                "Cada navegador ejecuta una cuenta a la vez. En Railway "
                "conviene 3 (configurable con la variable MAX_BROWSERS)."
            ),
        )

    col4, col5 = st.columns(2)
    with col4:
        dar_like = st.checkbox("Dar like también", value=False, key="act_like")
    with col5:
        grupo = st.text_input("Filtrar por grupo (A/B/C, opcional)", key="act_grupo")

    col6, col7 = st.columns(2)
    with col6:
        repetir = st.checkbox(
            "🔁 Repetir hasta agotar el tiempo (textos nuevos en cada ronda)",
            value=True,
            key="act_repetir",
            help=(
                "Cada cuenta sigue trabajando en rondas hasta agotar la "
                "duración, con textos nuevos regenerados en cada ronda."
            ),
        )
    with col7:
        todas_cuentas = st.checkbox(
            "📢 Todas las cuentas publican (solo con registro definido)",
            value=False,
            key="act_todas",
            help=(
                "Ignora la cantidad y usa todas las cuentas activas; las "
                "cuentas sin registro (político/activista/ciudadanía) no hacen nada."
            ),
        )
    if todas_cuentas:
        st.caption(
            "📢 **Todas las cuentas publican**: se ignora la cantidad de "
            "cuentas y se usan todas las activas. Solo publican las que tengan "
            "registro definido (político/activista/ciudadanía); las cuentas sin "
            "registro no hacen nada."
        )

    if st.button("🎯 Lanzar activación", type="primary", key="btn_act"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Pega al menos una URL objetivo.")
            return
        if not texto_base:
            st.warning("Escribe el texto base de la cita.")
            return

        from activaciones.motor import MotorActivacion

        motor = MotorActivacion(max_concurrente=int(navegadores))
        progreso = st.progress(0.0)
        estado = st.empty()

        def callback(hechas, total, usuario, ok):
            progreso.progress(hechas / total if total else 0)
            icono = "✅" if ok else "❌"
            estado.write(f"⏳ {icono} **@{usuario}** ({hechas}/{total})")

        resultados = motor.ejecutar(
            urls=urls,
            texto_base=texto_base,
            cantidad_cuentas=(
                None if todas_cuentas else (int(cantidad) if cantidad > 0 else None)
            ),
            grupo=grupo.strip() or None,
            dar_like=dar_like,
            duracion_min=int(duracion_min),
            cohortes=int(cohortes),
            callback=callback,
            hashtags=hashtags,
            repetir=bool(repetir),
            solo_con_registro=bool(todas_cuentas),
        )

        st.markdown("---")
        metricas = [
            ("🎯 Total", resultados.get("total", 0)),
            ("✅ Exitosas", resultados.get("exitosas", 0)),
            ("❌ Fallidas", resultados.get("fallidas", 0)),
        ]
        if "rondas" in resultados:
            metricas.append(("🔄 Rondas", resultados.get("rondas", 0)))
        if "sin_registro" in resultados:
            metricas.append(
                ("🪪 Sin registro (saltadas)", resultados.get("sin_registro", 0))
            )
        for col, (etiqueta, valor) in zip(st.columns(len(metricas)), metricas):
            col.metric(etiqueta, valor)

        detalles = resultados.get("detalles") or []
        if detalles:
            with st.expander("🔍 Detalle por cuenta", expanded=False):
                for d in detalles:
                    icono = "✅" if d.get("ok") else "❌"
                    linea = f"{icono} @{d.get('usuario', '')} — {d.get('detalle', '')}"
                    if d.get("url"):
                        linea += f" — [ver post]({d['url']})"
                    if d.get("ronda"):
                        linea += f" — ronda {d['ronda']}"
                    st.markdown(linea)

        sin_registro_usuarios = resultados.get("sin_registro_usuarios") or []
        sugerencia_registro = (resultados.get("sugerencia_registro") or "").strip()
        if sin_registro_usuarios or sugerencia_registro:
            with st.expander(
                f"🪪 Cuentas sin registro (saltadas) ({len(sin_registro_usuarios)})",
                expanded=False,
            ):
                if sugerencia_registro:
                    st.info(sugerencia_registro)
                if sin_registro_usuarios:
                    st.caption(", ".join(f"@{u}" for u in sin_registro_usuarios))


def _por_roles():
    """Pestana B: subcuentas por rol (asignar + lanzar campana)."""
    from core.roles import ROLES_ACTIVACION, etiqueta_rol_activacion, normalizar_rol_activacion

    # Import perezoso: cuentas.py importa Streamlit y compania y solo se
    # necesita al abrir esta pestana (evita acoplar el arranque del dashboard).
    from web.operaciones.cuentas import _selector_masivo

    cuentas = _cargar_cuentas_con_roles()
    st.caption(f"Cuentas twitter activas: **{len(cuentas)}**")

    if not cuentas:
        st.info(
            "No hay cuentas twitter activas. Importa/activa cuentas en "
            "'🗂️ Cuentas: Perfiles, Secciones & Nombres'."
        )
        return

    # ---------------- Asignar roles ----------------
    st.markdown("### 🏷️ Asignar roles (subcuentas)")
    seleccion = _selector_masivo(cuentas, "act_roles_selector")
    usuarios_sel = [f.get("usuario") for f in seleccion if f.get("usuario")]

    opciones_rol = [OPCION_SIN_ROL] + list(ROLES_ACTIVACION.values())
    col_rol, col_btn = st.columns([2, 1])
    with col_rol:
        opcion_rol = st.selectbox(
            "Rol a asignar",
            opciones_rol,
            key="act_roles_rol",
            help="El rol vacio ('Sin rol') hace que la cuenta se salte en las campanas por rol.",
        )
    with col_btn:
        st.write("")
        asignar = st.button(
            "💾 Asignar rol a seleccionadas",
            key="btn_act_roles_assign",
        )
    repartir = st.button(
        "🎲 Repartir en 3 tercios automaticamente",
        key="btn_act_roles_tercios",
        help="En orden alfabetico: un tercio a cita, un tercio a hashtags y el resto a rt.",
    )

    if asignar:
        if not usuarios_sel:
            st.warning("Selecciona al menos una cuenta para asignarle rol.")
        else:
            codigo = normalizar_rol_activacion(opcion_rol)
            n = _actualizar_roles(usuarios_sel, codigo)
            st.success(
                f"✅ Rol «{etiqueta_rol_activacion(codigo)}» asignado a {n} cuenta(s)."
            )
            st.rerun()

    if repartir:
        if not usuarios_sel:
            st.warning("Selecciona al menos una cuenta para repartir en tercios.")
        else:
            reparto = _repartir_tercios(usuarios_sel)
            resumen = []
            for rol in ORDEN_ROLES:
                n = _actualizar_roles(reparto[rol], rol)
                resumen.append(f"{etiqueta_rol_activacion(rol)}: {n}")
            st.success("🎲 Reparto en tercios → " + " · ".join(resumen))
            st.rerun()

    # ---------------- Conteos y subcuentas ----------------
    st.markdown("### 📊 Subcuentas por rol (reparto actual)")
    _mostrar_panel_roles(cuentas)

    if st.button(
        "👁️ Previsualizar reparto actual",
        key="btn_act_roles_preview",
        help="Muestra el conteo por rol y ejemplos de usuarios sin lanzar nada.",
    ):
        st.session_state["act_roles_preview"] = True
    if st.session_state.get("act_roles_preview"):
        with st.expander(
            "👁️ Previsualización del reparto actual (sin lanzar nada)",
            expanded=True,
        ):
            for rol in ORDEN_ROLES + ("",):
                subcuentas = [
                    f.get("usuario")
                    for f in cuentas
                    if normalizar_rol_activacion(f.get("rol_activacion")) == rol
                ]
                st.markdown(
                    f"**{etiqueta_rol_activacion(rol)}** — {len(subcuentas)} subcuenta(s)"
                )
                if subcuentas:
                    st.caption(
                        "Ejemplos: " + ", ".join(f"@{u}" for u in subcuentas[:20])
                        + (" …" if len(subcuentas) > 20 else "")
                    )

    # ---------------- Lanzar campana ----------------
    st.markdown("### 🚀 Lanzar campaña por roles")
    urls_text = st.text_area(
        "URLs objetivo (una por línea; las usan 'cita' y 'rt')",
        height=100,
        key="act_roles_urls",
    )
    texto_base = st.text_area(
        "Texto base de la cita / contexto por defecto de los posts con hashtag",
        height=80,
        key="act_roles_texto",
    )
    contexto = st.text_area(
        "Contexto de los posts con hashtag (tema sobre el que debe opinar la IA, "
        "ej. 'gran deporte que tenemos como el futbol')",
        height=80,
        key="act_roles_contexto",
        help=(
            "Si lo dejas vacío, el motor usa el texto base de la cita como "
            "contexto por defecto."
        ),
    )

    col_hashtags, col_menciones = st.columns(2)
    with col_hashtags:
        hashtags = st.text_input(
            "Hashtags (ej. #Mexico #4T)",
            key="act_roles_hashtags",
            help=(
                "Se agregan también a los retweets con cita y la IA los usa en "
                "los posts con hashtag."
            ),
        )
    with col_menciones:
        menciones = st.text_input(
            "Menciones (ej. @cuenta1 @cuenta2)",
            key="act_roles_menciones",
        )

    col_dur, col_coh, col_nav = st.columns(3)
    with col_dur:
        duracion_min = st.number_input(
            "Duración (min)", min_value=1, max_value=360, value=60, step=5,
            key="act_roles_dur",
        )
    with col_coh:
        cohortes = st.number_input(
            "Cohortes", min_value=1, max_value=24, value=4, step=1,
            key="act_roles_coh",
        )
    with col_nav:
        navegadores = st.number_input(
            "Navegadores simultáneos",
            min_value=1, max_value=30, value=_navegadores_default(), step=1,
            key="act_roles_nav",
            help=(
                "Cada navegador ejecuta una cuenta a la vez. En Railway "
                "conviene 3 (configurable con la variable MAX_BROWSERS)."
            ),
        )

    col_like, col_solo, col_rep = st.columns(3)
    with col_like:
        dar_like = st.checkbox(
            "Dar like también", value=False, key="act_roles_like"
        )
    with col_solo:
        solo_con_rol = st.checkbox(
            "Solo cuentas con rol", value=True, key="act_roles_solo_rol"
        )
    with col_rep:
        repetir = st.checkbox(
            "🔁 Repetir hasta agotar el tiempo (textos nuevos en cada ronda)",
            value=True,
            key="act_roles_repetir",
            help=(
                "Cada cuenta sigue trabajando en rondas hasta agotar la "
                "duración, con textos nuevos regenerados en cada ronda."
            ),
        )

    todas_cuentas = st.checkbox(
        "📢 Todas las cuentas publican (solo con registro definido)",
        value=False,
        key="act_roles_todas",
        help=(
            "Ignora el selector y usa todas las cuentas activas; las cuentas "
            "sin registro (político/activista/ciudadanía) no hacen nada."
        ),
    )
    if todas_cuentas:
        st.caption(
            "📢 **Todas las cuentas publican**: se ignora el selector de "
            "cuentas y se usan todas las activas que cumplan el filtro de rol. "
            "Solo publican las que tengan registro definido "
            "(político/activista/ciudadanía); las cuentas sin registro no hacen nada."
        )

    if st.button(
        "🗂️ Lanzar campaña por roles",
        type="primary",
        key="btn_act_roles_launch",
    ):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        solo_roles_param = list(ORDEN_ROLES) if solo_con_rol else None
        usuarios_param = None if todas_cuentas else (usuarios_sel or None)
        base_objetivo = (
            [f for f in cuentas if f.get("tipo_cuenta")]
            if todas_cuentas
            else cuentas
        )
        roles_objetivo = _roles_objetivo(
            base_objetivo, usuarios_param, solo_roles_param
        )

        if not roles_objetivo:
            st.warning(
                "No hay cuentas con rol que cumplan la selección. Asigna roles "
                "en «🏷️ Asignar roles» o revisa el selector."
            )
            return
        if not urls and (roles_objetivo & {"cita", "rt"}):
            st.warning(
                "Pega al menos una URL objetivo: la selección incluye cuentas "
                "de 'Retweet con cita' y/o 'Retweet simple'."
            )
            return

        from activaciones.motor import MotorActivacion

        motor = MotorActivacion(max_concurrente=int(navegadores))
        progreso = st.progress(0.0)
        estado = st.empty()

        def callback(hechas, total, usuario, ok):
            progreso.progress(hechas / total if total else 0)
            icono = "✅" if ok else "❌"
            estado.write(f"⏳ {icono} **@{usuario}** ({hechas}/{total})")

        resultados = motor.ejecutar_por_roles(
            urls=urls,
            texto_base=texto_base,
            hashtags=hashtags,
            menciones=menciones,
            dar_like=dar_like,
            duracion_min=int(duracion_min),
            cohortes=int(cohortes),
            usuarios=usuarios_param,
            solo_roles=solo_roles_param,
            callback=callback,
            contexto=contexto,
            repetir=bool(repetir),
            solo_con_registro=bool(todas_cuentas),
        )
        _mostrar_resultados_roles(resultados)


def render(usuario: dict):
    cabecera(
        "🎯 ACTIVACIÓN MASIVA",
        "RT con cita masivo, campañas por roles y campaña 3+3+3",
    )

    st.info(
        f"Concurrencia máxima de navegadores: **{settings.max_browsers}** "
        f"(configurable con la variable `MAX_BROWSERS`). Headless: **{settings.headless}**."
    )

    tabs = st.tabs(["🎯 Cita masiva", "🗂️ Por roles (subcuentas)", "📋 Campaña 3+3+3"])
    with tabs[0]:
        _cita_masiva()
    with tabs[1]:
        _por_roles()
    with tabs[2]:
        _campana_3_3_3(usuario)


# ============================ PESTANA C: 3+3+3 ============================

def _campana_3_3_3(usuario: dict):
    """Pestana C: 3 posts + 3 comentarios + 3 RTs del principal por cuenta.

    Genera los 9 textos con `ia.generador_contenido.
    generar_pool_campana_por_cuenta` (fallback al atajo
    `generar_textos_campana_3_3_3`), muestra el preview por cuenta con los 9
    textos y programa las acciones en el scheduler con
    `scheduler.distribucion_horaria.plan_hora_cuenta` (n_posts=3,
    n_comentarios=3, n_rts=3). Solo llama a ia//scheduler: no edita esos
    modulos."""
    from datetime import datetime, timedelta

    from web.operaciones._helpers import cuentas_por_plataforma

    st.markdown("### 📋 Campaña 3+3+3 (9 acciones por cuenta)")
    st.caption(
        "Por cuenta: **3 posts + 3 comentarios + 3 RTs del tweet principal**. "
        "Los 9 textos se generan con la campaña 3+3+3 respetando registro y "
        "perfil de cada cuenta (hashtag en medio) y se programan en el "
        "scheduler a lo largo de la ventana."
    )

    cuentas = cuentas_por_plataforma("twitter")
    if not cuentas:
        st.info(
            "No hay cuentas twitter activas. Importa/activa cuentas en "
            "'🗂️ Cuentas: Perfiles, Secciones & Nombres'."
        )
        return

    perfiles = {}
    for c in cuentas:
        try:
            from core.perfiles import etiqueta_perfil

            perfiles[c.usuario] = etiqueta_perfil(
                getattr(c, "perfil_personalidad", "")
            )
        except Exception:
            perfiles[c.usuario] = ""
    opciones = {f"@{c.usuario} · {perfiles[c.usuario]}": c for c in cuentas}
    seleccion_nombres = st.multiselect(
        "Cuentas (por defecto, todas)",
        list(opciones),
        default=list(opciones),
        key="act333_cuentas",
    )
    seleccion = [opciones[n] for n in seleccion_nombres]
    if not seleccion:
        st.warning("Selecciona al menos una cuenta.")
        return

    url_principal = st.text_input(
        "Tweet principal (URL del tweet a retwittear)",
        key="act333_url",
        placeholder="https://x.com/…/status/…",
    )
    col_com, col_cita = st.columns(2)
    with col_com:
        urls_com = st.text_area(
            "URLs para comentar/responder (una por línea)",
            height=100,
            key="act333_urls_com",
            placeholder="https://x.com/…/status/…",
        )
    with col_cita:
        base_cita = st.text_area(
            "Texto base de la cita (opcional)",
            height=100,
            key="act333_base_cita",
            placeholder="Si lo das, las 3 citas son variaciones suyas.",
        )

    col_fecha, col_hora, col_vent = st.columns(3)
    with col_fecha:
        fecha_base = st.date_input(
            "Fecha de inicio",
            value=(datetime.now() + timedelta(hours=1)).date(),
            key="act333_fecha",
        )
    with col_hora:
        hora_base = st.time_input(
            "Hora de inicio",
            value=(datetime.now() + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            ).time(),
            key="act333_hora",
        )
    with col_vent:
        ventana = st.number_input(
            "Ventana (min)", 30, 240, 60, step=10, key="act333_ventana"
        )

    inicio_dt = datetime.combine(fecha_base, hora_base)
    if inicio_dt <= datetime.now():
        st.error("La hora de inicio debe ser futura.")
        return

    col_g, col_p, col_l = st.columns(3)
    with col_g:
        generar = st.button(
            "🧠 Generar 9 textos por cuenta",
            type="primary",
            key="btn_act333_generar",
        )
    with col_p:
        programar = st.button(
            "✅ Programar en el scheduler",
            key="btn_act333_programar",
            disabled="act333_plan" not in st.session_state,
        )
    with col_l:
        limpiar = st.button("🗑️ Descartar", key="btn_act333_limpiar")

    if limpiar:
        for k in ("act333_plan", "act333_pool", "act333_preview"):
            st.session_state.pop(k, None)
        st.rerun()

    if generar:
        urls_com_list = [
            l.strip() for l in str(urls_com or "").splitlines() if l.strip()
        ]
        if not (url_principal or "").strip():
            st.error("Pega la URL del tweet principal.")
        elif not urls_com_list:
            st.error("Pega al menos una URL para los comentarios.")
        else:
            _generar_333(
                seleccion,
                inicio_dt,
                ventana=int(ventana),
                url_principal=url_principal.strip(),
                urls_com=urls_com_list,
                base_cita=(base_cita or "").strip(),
            )

    preview = st.session_state.get("act333_preview") or []
    if preview:
        _preview_333(preview)
        if programar:
            _programar_333(st.session_state.get("act333_plan") or [], usuario)


def _generar_333(seleccion, inicio_dt, ventana, url_principal, urls_com, base_cita):
    """Genera el pool 3+3+3, arma el plan horario y lo guarda en session."""
    import random

    from loguru import logger

    from scheduler.distribucion_horaria import (
        construir_plan_completo,
        plan_hora_cuenta,
    )

    try:
        from core.perfiles import normalizar_perfil
    except Exception:
        def normalizar_perfil(v):
            return str(v or "").strip()

    perfil_por_usuario = {
        c.usuario: normalizar_perfil(getattr(c, "perfil_personalidad", ""))
        for c in seleccion
    }

    orden = []
    for c in seleccion:
        rng = random.Random(hash((c.usuario, inicio_dt.isoformat())) & 0xFFFFFFFF)
        orden.extend(
            plan_hora_cuenta(
                c.usuario,
                perfil_por_usuario.get(c.usuario, ""),
                inicio_dt,
                n_posts=3,
                n_comentarios=3,
                n_rts=3,
                ventana_minutos=int(ventana),
                rng=rng,
            )
        )

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

    def _cb(hechas, total):
        try:
            progreso.progress(min(1.0, float(hechas) / max(1, int(total or 1))))
            estado.caption(f"✍️ Generando campaña 3+3+3: {hechas}/{total}...")
        except Exception:
            pass

    try:
        from ia.generador_contenido import generar_pool_campana_por_cuenta

        pool = generar_pool_campana_por_cuenta(
            cuentas_info,
            n_posts=3,
            n_comentarios=3,
            n_citas=3,
            base_cita=base_cita or "",
            callback=_cb,
        )
    except TypeError:
        from ia.generador_contenido import generar_textos_campana_3_3_3

        pool = generar_textos_campana_3_3_3(
            cuentas_info, base_cita=base_cita or "", callback=_cb
        )
    except Exception as e:
        logger.exception(f"Error generando campaña 3+3+3: {e}")
        st.error(f"Error generando textos: {e}")
        return

    textos_posts, textos_com, textos_citas = {}, {}, {}
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
    estado.caption("✅ Textos listos (9 por cuenta).")

    plan = construir_plan_completo(
        orden,
        posts_por_cuenta=textos_posts,
        comentarios_por_cuenta=textos_com,
        urls_rt=[url_principal],
        urls_comentario=urls_com,
    )
    citas_restantes = {u: list(v) for u, v in textos_citas.items()}
    for p in plan:
        if p.get("tipo") == "retweet":
            bolsa = citas_restantes.get(p.get("usuario")) or []
            if bolsa:
                p["texto"] = bolsa.pop(0)

    st.session_state["act333_plan"] = plan
    st.session_state["act333_pool"] = {
        c.usuario: {
            "posts": textos_posts.get(c.usuario, []),
            "comentarios": textos_com.get(c.usuario, []),
            "citas": textos_citas.get(c.usuario, []),
        }
        for c in seleccion
    }
    por_usuario: dict[str, list] = {}
    for p in plan:
        por_usuario.setdefault(p["usuario"], []).append(p)
    st.session_state["act333_preview"] = sorted(por_usuario)
    st.success(
        f"✅ Plan 3+3+3 listo: {len(plan)} acciones "
        f"({sum(1 for p in plan if p['tipo'] == 'post')} posts · "
        f"{sum(1 for p in plan if p['tipo'] == 'comentario')} comentarios · "
        f"{sum(1 for p in plan if p['tipo'] == 'retweet')} RTs)."
    )
    st.rerun()


def _preview_333(usuarios: list):
    """Tabla resumen + expanders por cuenta con los 9 textos."""
    pool = st.session_state.get("act333_pool") or {}
    plan = st.session_state.get("act333_plan") or []
    por_usuario: dict[str, list] = {}
    for p in plan:
        por_usuario.setdefault(p.get("usuario"), []).append(p)

    filas = []
    for u in usuarios:
        accs = por_usuario.get(u, [])
        detalle = pool.get(u) or {}
        filas.append(
            {
                "cuenta": f"@{u}",
                "posts": len(detalle.get("posts") or []),
                "comentarios": len(detalle.get("comentarios") or []),
                "citas (RTs)": len(detalle.get("citas") or []),
                "acciones": len(accs),
            }
        )
    st.dataframe(filas, use_container_width=True, hide_index=True)

    st.markdown("#### 📝 Textos por cuenta (9 por cuenta)")
    for u in usuarios:
        detalle = pool.get(u) or {}
        posts = detalle.get("posts") or []
        comentarios = detalle.get("comentarios") or []
        citas = detalle.get("citas") or []
        with st.expander(f"@{u} — {len(posts) + len(comentarios) + len(citas)} textos"):
            st.markdown("**📝 Posts**")
            for i, t in enumerate(posts, start=1):
                st.markdown(f"{i}. {t}")
            st.markdown("**💬 Comentarios**")
            for i, t in enumerate(comentarios, start=1):
                st.markdown(f"{i}. {t}")
            st.markdown("**🔁 Citas (textos de los RTs del principal)**")
            for i, t in enumerate(citas, start=1):
                st.markdown(f"{i}. {t}")


def _programar_333(plan: list, usuario: dict):
    """Programa el plan 3+3+3 como Tareas (post/comentario/retweet)."""
    from loguru import logger

    from core.database import get_db_session
    from core.models import Cuenta, Tarea
    from scheduler.manager import SchedulerManager

    usuarios = sorted({p.get("usuario") for p in (plan or []) if p.get("usuario")})
    try:
        with get_db_session() as db:
            filas = db.query(Cuenta).filter(Cuenta.usuario.in_(usuarios)).all()
            id_por_usuario = {c.usuario: c.id for c in filas}
    except Exception as e:
        st.error(f"No se pudieron cargar las cuentas: {e}")
        return

    try:
        manager = SchedulerManager()
    except Exception as e:
        logger.exception(f"No se pudo iniciar el scheduler: {e}")
        st.error(f"No se pudo iniciar el scheduler: {e}")
        return

    programadas = omitidas = fallidas = 0
    for p in plan or []:
        cuenta_id = id_por_usuario.get(p.get("usuario"))
        if cuenta_id is None or p.get("fecha_hora") is None:
            omitidas += 1
            continue
        texto = (p.get("texto") or "").strip()
        url = (p.get("url") or "").strip()
        tipo = p.get("tipo")
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
            else:
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
            logger.exception(f"Error programando acción 3+3+3: {e}")
            fallidas += 1

    if programadas:
        st.success(f"✅ {programadas} acción(es) 3+3+3 programadas.")
    if fallidas:
        st.error(f"❌ {fallidas} acción(es) no se pudieron programar.")
    if omitidas:
        st.warning(f"⚠️ {omitidas} acción(es) omitidas (sin cuenta, texto o URL).")
    for k in ("act333_plan", "act333_pool", "act333_preview"):
        st.session_state.pop(k, None)
