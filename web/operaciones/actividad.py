"""Operacion ACTIVIDAD: modo LEVE de campana (3-4 tweets por cuenta).

A diferencia de "🎯 Activación Masiva" (campana INTENSIVA con rondas, RTs,
citas, comentarios y subconjuntos ALEATORIOS de hashtags en cada texto), la
ACTIVIDAD es una campana suave: cada cuenta publica entre 3 y 4 tweets con
pausas de 60-240s entre los tweets de la MISMA cuenta y CADA texto lleva
TODOS los hashtags pedidos (1-6), nunca un subconjunto. Es el modo ideal para
mantener la flota activa sin rafagas que X castigue.

La pagina reutiliza TODA la infraestructura de
`web/operaciones/activacion_masiva.py`:

  - `_aplicar_limpieza_contexto_pendiente` (al inicio, antes de crear widgets)
    y `_panel_contexto_noticias(prefix)`: panel de links de prensa como
    TRASFONDO INVISIBLE; viaja al motor como `narrativa` (la IA NUNCA debe
    mencionarlo) mientras el TEMA que si puede tratar va como `contexto`.
  - `_render_proceso_activo(prefix)` + `_lanzar_con_opciones_velocidad`: panel
    de proceso persistente (sobrevive a recargas y a la navegacion entre
    operaciones) con barra, feed de eventos, "⛔ Paro total" y guard de campana
    unica (`data/.campana_activa` + `data/campanas/ultima_campana.json`).
  - `_lanzar_motor`: pasa `callback` y el `threading.Event` de paro SOLO si el
    motor los soporta (`_soporta_kwarg`), tolerante a versiones viejas.
  - `_helpers.separar_pausadas`/`aviso_pausadas`: las cuentas pausadas para
    activacion (clientes) quedan fuera salvo que se marque el checkbox de
    "mantenimiento leve".

El motor (`MotorActivacion.ejecutar_actividad`, congelado) hace el resto:
resuelve el N aleatorio por cuenta, genera los textos con TODOS los hashtags,
omite pausadas/Tier sin abrir navegador, respeta cuotas horarias/diarias y el
tope de duracion. Los links de los posts publicados quedan en «📊 Reportes»
(tabla de acciones exitosas).
"""
import re

import streamlit as st

from web.ui import cabecera
from web.operaciones._helpers import aviso_pausadas, separar_pausadas
from web.operaciones.activacion_masiva import (
    _aplicar_limpieza_contexto_pendiente,
    _campana_en_curso,
    _cargar_cuentas_con_roles,
    _lanzar_con_opciones_velocidad,
    _lanzar_motor,
    _panel_contexto_noticias,
    _render_proceso_activo,
)

# Prefijo de la campana y de las claves de `st.session_state` de la operacion
# (no debe colisionar con "act" ni "act_roles" de Activacion Masiva).
PREFIX = "actividad"

# Tope de hashtags por actividad (el motor tambien recorta a 6).
MAX_HASHTAGS = 6

# Separadores del textarea de hashtags: saltos de linea, comas, punto y coma y
# espacios (un usuario puede escribir "#Mexico #Deportes" en la misma linea).
_SEPARADORES_HASHTAGS = re.compile(r"[\s,;]+")


def _normalizar_hashtags(texto) -> list:
    """Hashtags normalizados de un textarea/list: con '#' y SIN duplicados.

    Acepta texto (uno por linea o separados por coma/espacio) o una lista.
    Devuelve la lista en orden de aparicion, con '#' garantizado y sin
    duplicados case-insensitive ("#Mexico" y "#mexico" cuentan como uno).
    Ignora vacios, "#" suelos y tokens sin letras. Nunca lanza.
    """
    if texto is None:
        return []
    if isinstance(texto, (list, tuple, set)):
        piezas = []
        for elemento in texto:
            piezas.extend(_SEPARADORES_HASHTAGS.split(str(elemento or "")))
    else:
        piezas = _SEPARADORES_HASHTAGS.split(str(texto))
    tags: list = []
    vistos: set = set()
    for pieza in piezas:
        token = str(pieza or "").strip()
        if not token:
            continue
        token = "#" + token.lstrip("#").strip()
        if len(token) <= 1:
            continue
        clave = token.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        tags.append(token)
    return tags


def _entero(valor, default: int = 0) -> int:
    """int() tolerante a None/str/basura (nunca lanza)."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _cuentas_tier_bloqueadas(filas) -> list:
    """Usuarios que quedaran FUERA por su Tier en la actividad.

    El rol efectivo de la actividad es "hashtags" (posts originales), asi que
    el motor omite a las cuentas Tier 2 (Volumen/Aged) y Tier 3 (Métricas/
    Soporte) SIN abrir navegador (`tier_omitidas`). Este helper anticipa ese
    conteo en la seleccion para mostrarlo en la UI. Acepta las filas-dict del
    selector y nunca lanza: sin `core.tiers` devuelve [].
    """
    try:
        from core.tiers import es_tier2, es_tier3
        from web.operaciones.cuentas import _fila_como_cuenta
    except Exception:
        return []
    bloqueadas: list = []
    for fila in filas or []:
        if not isinstance(fila, dict):
            continue
        usuario = str(fila.get("usuario") or "").strip()
        if not usuario:
            continue
        try:
            cuenta = _fila_como_cuenta(fila)
            if es_tier2(cuenta) or es_tier3(cuenta):
                bloqueadas.append(usuario)
        except Exception:
            continue
    return bloqueadas


def mostrar_resultados_actividad(resumen: dict) -> None:
    """Metricas finales de una campana de Actividad (render de la pagina).

    Lo llama el panel persistente (`activacion_masiva._render_resultados`) al
    terminar la campana: metricas de esperados/exitosas/fallidas/omitidas +
    desglose de pausadas, Tier, tiempo y cuota + tabla por cuenta, con la nota
    de que los links quedan en Reportes. Nunca lanza.
    """
    if not isinstance(resumen, dict):
        resumen = {}
    st.markdown("---")

    error = str(resumen.get("error") or "").strip()
    if error:
        st.error(f"❌ La actividad terminó con error: {error}")
    if resumen.get("cancelada"):
        st.warning(
            "⛔ Actividad detenida con «Paro total»: los tweets no intentados "
            "quedaron como omitidos."
        )

    fila_1 = [
        ("🎯 Esperados", _entero(resumen.get("total_esperado"))),
        ("✅ Exitosas", _entero(resumen.get("exitosas"))),
        ("❌ Fallidas", _entero(resumen.get("fallidas"))),
        ("➖ Omitidas", _entero(resumen.get("omitidas"))),
    ]
    fila_2 = [
        ("⏸️ Pausadas fuera", _entero(resumen.get("pausadas_omitidas"))),
        ("🧱📊 Tier fuera", _entero(resumen.get("tier_omitidas"))),
        ("⏰ Por tiempo", _entero(resumen.get("omitidas_por_tiempo"))),
        ("🛑 Por cuota", _entero(resumen.get("omitidas_por_cuota"))),
    ]
    if "sin_sesion" in resumen:
        fila_2.append(("🔑 Sin sesión", _entero(resumen.get("sin_sesion"))))
    if "omitidas_por_cancelacion" in resumen:
        fila_2.append(
            ("⛔ Por paro", _entero(resumen.get("omitidas_por_cancelacion")))
        )
    for fila in (fila_1, fila_2):
        columnas = st.columns(len(fila))
        for columna, (etiqueta, valor) in zip(columnas, fila):
            columna.metric(etiqueta, valor)

    por_cuenta = resumen.get("por_cuenta") or {}
    if isinstance(por_cuenta, dict) and por_cuenta:
        filas = []
        for usuario, info in list(por_cuenta.items())[:300]:
            detalle = info if isinstance(info, dict) else {}
            filas.append(
                {
                    "Cuenta": f"@{usuario}",
                    "Objetivo": _entero(detalle.get("posts_objetivo")),
                    "✅ Exitosas": _entero(detalle.get("exitosas")),
                    "❌ Fallidas": _entero(detalle.get("fallidas")),
                    "🔗 Links": len(detalle.get("urls") or []),
                }
            )
        with st.expander(
            f"🔍 Detalle por cuenta ({len(por_cuenta)})", expanded=False
        ):
            st.dataframe(filas, use_container_width=True, hide_index=True)

    st.caption(
        "🔗 Los links de los tweets publicados están en «📊 Reportes» "
        "(tabla de acciones exitosas)."
    )


def _formulario() -> None:
    """Formulario completo de la campana de Actividad (sin campana en curso).

    La vista deja lo esencial: hashtags obligatorios, N de tweets por cuenta,
    pausas, navegadores, duracion, contexto/tema + noticias (trasfondo),
    menciones y la seleccion de cuentas con el patron de `_selector_masivo`
    (filtro por seccion/rango/cantidad/manual). Todo lo demas (tier, pausadas,
    previsualizacion y el boton de lanzar) se muestra en el mismo flujo.
    """
    st.markdown("### 📣 Hashtags (obligatorios)")
    st.caption(
        "Cada tweet lleva **TODOS** los hashtags de esta lista (1-6). A "
        "diferencia de la activación masiva, aquí no se reparten en "
        "subconjuntos aleatorios."
    )
    hashtags_text = st.text_area(
        "Hashtags (uno por línea o separados por coma)",
        height=80,
        key="actividad_hashtags",
        placeholder="#Mexico, #Deportes",
        help="De 1 a 6 hashtags; se garantiza el '#' y se eliminan duplicados.",
    )
    tags_preview = _normalizar_hashtags(hashtags_text)
    if tags_preview:
        st.caption("Se usarán en cada tweet: " + " ".join(tags_preview))
    elif str(hashtags_text or "").strip():
        st.caption("⚠️ No se detectó ningún hashtag válido todavía.")

    st.markdown("### ⚙️ Ritmo de la actividad")
    col_min, col_max = st.columns(2)
    with col_min:
        posts_min = st.number_input(
            "Tweets por cuenta (mín)",
            min_value=1,
            max_value=20,
            value=3,
            step=1,
            key="actividad_posts_min",
        )
    with col_max:
        posts_max = st.number_input(
            "Tweets por cuenta (máx)",
            min_value=1,
            max_value=20,
            value=4,
            step=1,
            key="actividad_posts_max",
            help="Cada cuenta publica un número aleatorio entre el mínimo y el máximo.",
        )
    col_pmin, col_pmax = st.columns(2)
    with col_pmin:
        pausa_min = st.number_input(
            "Pausa entre tweets de la misma cuenta (mín, s)",
            min_value=0,
            max_value=3600,
            value=60,
            step=5,
            key="actividad_pausa_min",
        )
    with col_pmax:
        pausa_max = st.number_input(
            "Pausa entre tweets de la misma cuenta (máx, s)",
            min_value=0,
            max_value=3600,
            value=240,
            step=5,
            key="actividad_pausa_max",
            help="Pausa aleatoria entre los tweets de la MISMA cuenta (anti-spam).",
        )
    col_nav, col_dur = st.columns(2)
    with col_nav:
        navegadores = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=3,
            value=2,
            step=1,
            key="actividad_navegadores",
            help=(
                "Cuentas publicando a la vez (1-3). En Railway no conviene "
                "pasar de 3: cada Chrome consume RAM/CPU/hilos."
            ),
        )
    with col_dur:
        duracion = st.number_input(
            "Duración máxima (min)",
            min_value=5,
            max_value=480,
            value=120,
            step=5,
            key="actividad_duracion",
            help=(
                "Tope de seguridad: los tweets que no alcancen el deadline se "
                "cuentan como omitidos por tiempo (sin abrir navegador)."
            ),
        )

    menciones = st.text_input(
        "Menciones (opcional, ej. @cuenta1 @cuenta2)",
        key="actividad_menciones",
        help="Se agregan a los textos generados cuando la IA las acepte.",
    )

    with st.expander("🎯 Contexto del tema (opcional)", expanded=False):
        contexto = st.text_area(
            "Tema sobre el que deben hablar los tweets",
            height=80,
            key="actividad_contexto",
            help=(
                "La IA lo usa como tema y NO lo copia literalmente. Si lo "
                "dejas vacío, los textos salen genéricos con los hashtags."
            ),
        )

    with st.expander(
        "📰 Contexto desde noticias (opcional, solo trasfondo)", expanded=False
    ):
        panel_noticias = _panel_contexto_noticias(PREFIX)

    st.markdown("### 👥 Cuentas objetivo")
    incluir_pausadas = st.checkbox(
        "⏸️ Incluir cuentas pausadas para activación (mantenimiento leve)",
        value=False,
        key="actividad_pausadas",
        help=(
            "Por defecto las cuentas pausadas para activación (clientes) "
            "quedan FUERA de las campañas. Márcalo para que participen en esta "
            "actividad leve (equivale a `permitir_pausadas=True` en el motor)."
        ),
    )

    from web.operaciones.cuentas import _selector_masivo

    cuentas = _cargar_cuentas_con_roles()
    cuentas, pausadas = separar_pausadas(
        cuentas, incluir_pausadas=bool(incluir_pausadas)
    )
    if incluir_pausadas:
        if pausadas:
            st.caption(
                f"⏸️ {len(pausadas)} cuentas pausadas incluidas en la "
                "actividad (mantenimiento leve)."
            )
    else:
        aviso_pausadas(pausadas)
    st.caption(f"Cuentas twitter activas disponibles: **{len(cuentas)}**")

    if not cuentas:
        st.info(
            "No hay cuentas twitter activas. Importa/activa cuentas en "
            "'🗂️ Cuentas: Perfiles, Secciones & Nombres'."
        )
        return

    seleccion = _selector_masivo(cuentas, "actividad_selector")
    usuarios_sel = [f.get("usuario") for f in seleccion if f.get("usuario")]

    tier_bloqueadas = _cuentas_tier_bloqueadas(seleccion)
    if tier_bloqueadas:
        st.caption(
            f"🧱📊 {len(tier_bloqueadas)} cuenta(s) de la selección quedarán "
            "FUERA por su Tier (Tier 2 y Tier 3 no publican posts con "
            "hashtag)."
        )
        with st.expander(
            f"Ver cuentas fuera por Tier ({len(tier_bloqueadas)})",
            expanded=False,
        ):
            st.caption(", ".join(f"@{u}" for u in tier_bloqueadas))

    bloqueadas_set = set(tier_bloqueadas)
    n_elegibles = len([u for u in usuarios_sel if u not in bloqueadas_set])
    if n_elegibles and int(posts_min) <= int(posts_max):
        minimo_pubs = n_elegibles * int(posts_min)
        maximo_pubs = n_elegibles * int(posts_max)
        st.markdown(
            f"**Preview:** {n_elegibles} cuentas × {int(posts_min)}-"
            f"{int(posts_max)} → **~{minimo_pubs} a {maximo_pubs} "
            "publicaciones**"
        )
    elif usuarios_sel:
        st.caption(
            "⚠️ Revisa el rango de tweets por cuenta para ver la "
            "previsualización."
        )

    limpiar_contexto = st.checkbox(
        "🧹 Limpiar el contexto (tema/noticias) al terminar",
        value=True,
        key="actividad_limpiar_contexto",
        help=(
            "Al terminar borra el resultado/links/texto de noticias y el tema "
            "manual; no toca hashtags, menciones, cuentas ni resultados."
        ),
    )

    st.caption(
        "🛡️ Cada cuenta respeta sus límites por hora y su tope diario "
        "(`LIMITE_*`/`CUOTAS_*`); si agota la cuota, sus tweets restantes se "
        "omiten sin abrir navegador."
    )

    if st.button(
        "🚀 Lanzar actividad",
        type="primary",
        key="btn_actividad_lanzar",
        help=(
            "Modo leve: 3-4 tweets por cuenta con TODOS los hashtags y pausas "
            "entre los tweets de la misma cuenta."
        ),
    ):
        _lanzar(
            tags=_normalizar_hashtags(hashtags_text),
            usuarios_sel=usuarios_sel,
            posts_min=posts_min,
            posts_max=posts_max,
            pausa_min=pausa_min,
            pausa_max=pausa_max,
            navegadores=navegadores,
            duracion=duracion,
            contexto=contexto,
            narrativa=panel_noticias.get("contexto"),
            menciones=menciones,
            incluir_pausadas=bool(incluir_pausadas),
            limpiar_contexto=bool(limpiar_contexto),
        )


def _lanzar(tags, usuarios_sel, posts_min, posts_max, pausa_min, pausa_max,
            navegadores, duracion, contexto, narrativa, menciones,
            incluir_pausadas, limpiar_contexto) -> None:
    """Valida y lanza `MotorActivacion.ejecutar_actividad` (nunca lanza).

    Validaciones bloqueantes: hashtags 1-6, rango de tweets, rango de pausas y
    al menos una cuenta. Si el motor es viejo (sin `ejecutar_actividad`) avisa
    claramente y NO lanza. El lanzamiento real se delega en
    `_lanzar_con_opciones_velocidad` (panel persistente + paro + guard unico).
    """
    if not tags:
        st.error(
            "Escribe al menos un hashtag (1-6). Sin hashtags no se lanza la "
            "actividad."
        )
        return
    if len(tags) > MAX_HASHTAGS:
        st.error(
            f"Como máximo {MAX_HASHTAGS} hashtags (hay {len(tags)}): quita "
            "algunos y vuelve a intentar."
        )
        return
    if _entero(posts_min, 3) > _entero(posts_max, 4):
        st.warning(
            "El mínimo de tweets por cuenta no puede ser mayor que el máximo."
        )
        return
    if _entero(pausa_min) > _entero(pausa_max):
        st.warning("La pausa mínima no puede ser mayor que la pausa máxima.")
        return
    if not usuarios_sel:
        st.warning("Selecciona al menos una cuenta para la actividad.")
        return

    try:
        from activaciones.motor import MotorActivacion
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo importar el motor de activaciones: {e}")
        return
    if not callable(getattr(MotorActivacion, "ejecutar_actividad", None)):
        st.error(
            "Este motor todavía no soporta el modo Actividad "
            "(`ejecutar_actividad`): actualiza el proyecto/Railway y vuelve a "
            "intentar."
        )
        return

    motor = MotorActivacion(max_concurrente=int(navegadores))
    lanzar = _lanzar_motor(
        motor.ejecutar_actividad,
        {
            "usuarios": list(usuarios_sel),
            "hashtags": list(tags),
            "posts_min": _entero(posts_min, 3),
            "posts_max": _entero(posts_max, 4),
            "pausa_entre_posts_seg": (_entero(pausa_min), _entero(pausa_max)),
            "texto_base": "",
            "contexto": str(contexto or "").strip(),
            "narrativa": str(narrativa or "").strip(),
            "menciones": (str(menciones or "").strip() or None),
            "max_browsers": int(navegadores),
            "permitir_pausadas": bool(incluir_pausadas),
            "duracion_max_min": _entero(duracion, 120),
        },
    )
    _lanzar_con_opciones_velocidad(
        lanzar,
        motor,
        duracion_min=_entero(duracion, 120),
        repetir=False,
        prefix=PREFIX,
        limpiar=bool(limpiar_contexto),
        # Valores recomendados fijos de velocidad (los mismos de Activacion
        # Masiva): sin proxy, sin imagenes, sin API, pestana persistente.
        sin_proxy=False,
        sin_imagenes=True,
        rt_api=False,
        api_primero=False,
        max_workers=12,
        permitir_password=False,
        modo_pestana=True,
        pestana_max_acciones=40,
        tipo="actividad",
        parametros={
            "duracion_min": _entero(duracion, 120),
            "repetir": False,
            "actividad": True,
            "posts_min": _entero(posts_min, 3),
            "posts_max": _entero(posts_max, 4),
            "hashtags": list(tags),
        },
    )
    # El panel persistente (arriba del formulario) pinta el proceso en vivo y,
    # al terminar, las metricas: aqui no se pinta nada mas.


def render(usuario: dict):
    """Pagina "✍️ Actividad": campana leve de 3-4 tweets por cuenta."""
    # Limpieza diferida del contexto (fin de campana): SIEMPRE antes de crear
    # cualquier widget de la pagina (misma regla que las pestanas de Activacion
    # Masiva: el pop de `actividad_contexto` no puede ocurrir despues).
    _aplicar_limpieza_contexto_pendiente(PREFIX)
    cabecera(
        "✍️ ACTIVIDAD",
        "Modo leve: 3-4 tweets por cuenta, con TODOS los hashtags",
    )
    st.info(
        "**Modo leve** de campaña. A diferencia de «🎯 Activación Masiva» "
        "(intensiva: rondas, RTs, citas y comentarios), aquí cada cuenta "
        "publica **3-4 tweets** con pausas de 60-240s entre sus propios "
        "tweets y **TODOS los hashtags** van en cada texto."
    )
    _render_proceso_activo(prefix=PREFIX)
    if _campana_en_curso() is not None:
        st.caption(
            "🚦 Hay una campaña en curso: usa el panel de arriba para "
            "detenerla o espera a que termine para lanzar otra."
        )
        return
    _formulario()
