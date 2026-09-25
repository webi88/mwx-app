import random
import threading
import time
from datetime import datetime

import streamlit as st

from web.ui import cabecera
from web.operaciones._helpers import (
    cuentas_por_plataforma,
    ejecutar_en_cuentas,
    mostrar_resultados,
)


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

# Nota operativa para campanas masivas (sin intervalos fijos).
NOTA_MASIVO = (
    "💡 Recomendación: empieza con un lote de 20-30 cuentas para validar y "
    "luego lanza todas. Las cuentas con la sesión vencida fallarán: renueva "
    "cookies/valida la sesión antes de una campaña masiva."
)

# Claves de `st.session_state` con el ultimo resultado (sobrevive al rerun).
CLAVE_RESULTADO_REPORTES = "reportes_ultimo_resultado"
CLAVE_RUN_REPORTES = "reportes_ultimo_run"

# Modos del selector masivo de cuentas.
MODO_TODAS = "Todas las activas"
MODO_PRIMERAS = "Primeras N"
MODO_RANGO = "Por rango"
MODO_MANUAL = "Manual"


# ---------------------------------------------------------------------------
# Estado compartido de la ejecucion en segundo plano
# ---------------------------------------------------------------------------
# Los hilos SOLO tocan este dict (bajo `_LOCK`). Ningun hilo llama a `st.*` ni
# a `st.session_state`: el hilo principal es el unico que renderiza y el unico
# que mueve el resultado final a `st.session_state`.
_LOCK = threading.Lock()
_EJECUCION = {
    "activa": False,
    "run_id": 0,
    "paro": None,          # threading.Event
    "total": 0,
    "hechas": 0,
    "exitos": 0,
    "fallidos": 0,
    "omitidas": 0,
    "ultima": "",
    "urls": 0,
    "paralelo": 1,
    "iniciada": "",
    "finalizada": "",
    "eventos": [],         # ultimos 5: {"usuario", "estado", "detalle"}
    "detalles": [],
    "resultado_final": None,
}


def _esta_corriendo() -> bool:
    with _LOCK:
        return bool(_EJECUCION["activa"])


def _snapshot() -> dict:
    """Copia thread-safe del estado para que el hilo principal la pinte."""
    with _LOCK:
        return {
            "total": int(_EJECUCION["total"]),
            "hechas": int(_EJECUCION["hechas"]),
            "exitos": int(_EJECUCION["exitos"]),
            "fallidos": int(_EJECUCION["fallidos"]),
            "omitidas": int(_EJECUCION["omitidas"]),
            "ultima": _EJECUCION["ultima"],
            "urls": int(_EJECUCION["urls"]),
            "paralelo": int(_EJECUCION["paralelo"]),
            "paro_solicitado": bool(
                _EJECUCION["paro"] and _EJECUCION["paro"].is_set()
            ),
            "eventos": list(_EJECUCION["eventos"]),
        }


def _solicitar_paro() -> None:
    """Marca el Evento de paro (lo comprueban los workers antes de cada cuenta)."""
    with _LOCK:
        if _EJECUCION["paro"] is not None:
            _EJECUCION["paro"].set()


def _marcar_ultima(usuario: str) -> None:
    with _LOCK:
        _EJECUCION["ultima"] = usuario


# ---------------------------------------------------------------------------
# Helpers puros (testeables sin Streamlit)
# ---------------------------------------------------------------------------
def _ordenar_cuentas(cuentas) -> list:
    """Orden alfabetico case-insensitive por usuario (mismo criterio que Cuentas)."""
    return sorted(
        list(cuentas or []),
        key=lambda c: (getattr(c, "usuario", "") or "").lower(),
    )


def _seleccionar_por_rango(cuentas, desde="", hasta="") -> list:
    """Rango INCLUSIVO por usuario sobre la lista ya ordenada (case-insensitive).

    Acepta `@usuario` o `usuario`; si un extremo no existe o viene vacio se usa
    el primero/ultimo. Si el orden esta invertido se intercambia. Nunca lanza."""
    ordenada = list(cuentas or [])
    if not ordenada:
        return []
    usuarios = [(getattr(c, "usuario", "") or "").lower() for c in ordenada]

    def _indice(valor, defecto):
        limpio = str(valor or "").strip().lower().lstrip("@")
        if limpio and limpio in usuarios:
            return usuarios.index(limpio)
        return defecto

    idx_desde = _indice(desde, 0)
    idx_hasta = _indice(hasta, len(ordenada) - 1)
    if idx_desde > idx_hasta:
        idx_desde, idx_hasta = idx_hasta, idx_desde
    return ordenada[idx_desde:idx_hasta + 1]


def _aplicar_modo(cuentas, modo, primeras_n=0, desde="", hasta="",
                  manual=None, cuenta_opts=None) -> list:
    """Cuentas a reportar segun el modo elegido (helper puro, sin Streamlit).

    - `Todas las activas`: todas las recibidas (orden alfabetico).
    - `Primeras N`: las primeras N del orden alfabetico.
    - `Por rango`: desde/hasta por usuario, ambos inclusive.
    - `Manual`: las etiquetas elegidas en el multiselect (via `cuenta_opts`)."""
    ordenada = _ordenar_cuentas(cuentas)
    if modo == MODO_PRIMERAS:
        try:
            n = int(primeras_n or 0)
        except (TypeError, ValueError):
            n = 0
        return ordenada[:max(0, n)]
    if modo == MODO_RANGO:
        return _seleccionar_por_rango(ordenada, desde, hasta)
    if modo == MODO_MANUAL:
        opciones = cuenta_opts or {}
        return [opciones[etiqueta] for etiqueta in (manual or []) if etiqueta in opciones]
    return ordenada


def _dividir_en_bloques(cuentas, n) -> list:
    """Reparte las cuentas en `n` bloques intercalados `cuentas[i::n]`.

    Hilos distintos trabajan cuentas distintas (sin solapes) y cada worker
    respeta su orden. `n` se acota a [1, len(cuentas)]."""
    lista = list(cuentas or [])
    try:
        n = int(n or 1)
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(n, len(lista) or 1))
    return [lista[i::n] for i in range(n)]


def _normalizar_resultado(datos) -> dict:
    """Resultado homogéneo del panel persistido, tolerante a datos viejos."""
    if not isinstance(datos, dict):
        datos = {}

    def _entero(*claves) -> int:
        for clave in claves:
            if clave in datos:
                try:
                    return int(datos.get(clave) or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    detalles = datos.get("detalles")
    if isinstance(detalles, str):
        detalles = [detalles]
    if not isinstance(detalles, (list, tuple)):
        detalles = []

    return {
        "exitos": _entero("exitos"),
        "fallidos": _entero("fallidos"),
        "omitidas": _entero("omitidas"),
        "detalles": [str(d) for d in detalles],
        "cuentas_totales": _entero("cuentas_totales", "total_cuentas"),
        "cuentas_hechas": _entero("cuentas_hechas", "hechas"),
        "paradas": _entero("paradas"),
        "urls": _entero("urls"),
        "paralelo": _entero("paralelo", "hilos") or 1,
        "fecha": str(datos.get("fecha") or ""),
    }


# ---------------------------------------------------------------------------
# Reporte por bot (mismo flujo de siempre)
# ---------------------------------------------------------------------------
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


def _accion_para(cuenta, urls, motivo, pausa_seg=None):
    """Callable `accion(bot)` para UNA cuenta (corre en el hilo worker).

    Incluye la pausa aleatoria de 1-3s entre cuentas y reporta todas las URLs.
    `pausa_seg` permite fijar la pausa en tests (0 = sin pausa)."""
    def accion(bot):
        _marcar_ultima(getattr(cuenta, "usuario", ""))
        if pausa_seg is None:
            time.sleep(random.uniform(1, 3))
        elif pausa_seg > 0:
            time.sleep(pausa_seg)
        todos_ok = True
        for url in urls:
            if not _reportar_objetivo_con_bot(bot, url, motivo):
                todos_ok = False
        return todos_ok

    return accion


def _acumular(run_id: int, usuario: str, resultado) -> None:
    """Suma el resultado de UNA cuenta al estado compartido (bajo lock)."""
    res = resultado if isinstance(resultado, dict) else {}
    exitos = int(res.get("exitos") or 0)
    fallidos = int(res.get("fallidos") or 0)
    omitidas = int(res.get("omitidas") or 0)
    detalles = [str(d) for d in (res.get("detalles") or [])]
    if exitos:
        estado = "ok"
    elif omitidas:
        estado = "omitida"
    else:
        estado = "fallo"
    detalle = detalles[0] if detalles else ("sin detalle" if estado != "ok" else "")
    with _LOCK:
        if _EJECUCION["run_id"] != run_id or not _EJECUCION["activa"]:
            return
        _EJECUCION["hechas"] += 1
        _EJECUCION["exitos"] += exitos
        _EJECUCION["fallidos"] += fallidos
        _EJECUCION["omitidas"] += omitidas
        _EJECUCION["detalles"].extend(detalles)
        _EJECUCION["eventos"].insert(
            0, {"usuario": usuario, "estado": estado, "detalle": detalle[:180]}
        )
        del _EJECUCION["eventos"][5:]


def _worker(run_id: int, bloque: list, urls: list, motivo: str,
            plataforma: str, paro: threading.Event) -> None:
    """Reporta su bloque de cuentas; comprueba el paro ANTES de cada cuenta.

    Corre en un hilo: NO toca ningun widget de Streamlit ni `st.session_state`
    (solo el dict compartido). Se llama `ejecutar_en_cuentas` por cuenta para
    poder cortar ordenadamente entre cuentas y registrar cada una."""
    for cuenta in list(bloque or []):
        if paro.is_set():
            break
        accion = _accion_para(cuenta, urls, motivo)
        try:
            resultado = ejecutar_en_cuentas(
                [cuenta], accion, plataforma, tipo="reportar"
            )
        except Exception as e:  # nunca debe tumbar el worker
            resultado = {
                "exitos": 0,
                "fallidos": 1,
                "omitidas": 0,
                "detalles": [f"❌ @{getattr(cuenta, 'usuario', '?')}: {e}"],
            }
        _acumular(run_id, getattr(cuenta, "usuario", "?"), resultado)


def _runner(run_id: int, cuentas: list, urls: list, motivo: str,
            plataforma: str, n_hilos: int, paro: threading.Event) -> None:
    """Coordina los N hilos de workers y publica el resultado final."""
    try:
        bloques = _dividir_en_bloques(cuentas, n_hilos)
        hilos = [
            threading.Thread(
                target=_worker,
                args=(run_id, bloque, urls, motivo, plataforma, paro),
                name=f"reportar-{run_id}-{i}",
                daemon=True,
            )
            for i, bloque in enumerate(bloques)
            if bloque
        ]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()
    finally:
        with _LOCK:
            if _EJECUCION["run_id"] != run_id:
                return
            hechas = int(_EJECUCION["hechas"])
            _EJECUCION["activa"] = False
            _EJECUCION["finalizada"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            _EJECUCION["resultado_final"] = {
                "exitos": int(_EJECUCION["exitos"]),
                "fallidos": int(_EJECUCION["fallidos"]),
                "omitidas": int(_EJECUCION["omitidas"]),
                "detalles": list(_EJECUCION["detalles"]),
                "cuentas_totales": int(_EJECUCION["total"]),
                "cuentas_hechas": hechas,
                "paradas": max(0, int(_EJECUCION["total"]) - hechas),
                "urls": int(_EJECUCION["urls"]),
                "paralelo": max(1, int(n_hilos or 1)),
                "fecha": _EJECUCION["finalizada"],
            }


def _iniciar_ejecucion(cuentas_sel, urls, motivo: str, plataforma: str,
                       n_hilos: int) -> bool:
    """Arranca la campana en segundo plano. False si ya hay una en curso."""
    with _LOCK:
        if _EJECUCION["activa"]:
            return False
        _EJECUCION["run_id"] += 1
        run_id = _EJECUCION["run_id"]
        paro = threading.Event()
        _EJECUCION.update({
            "activa": True,
            "paro": paro,
            "total": len(cuentas_sel),
            "hechas": 0,
            "exitos": 0,
            "fallidos": 0,
            "omitidas": 0,
            "ultima": "",
            "urls": len(urls),
            "paralelo": max(1, int(n_hilos or 1)),
            "iniciada": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "finalizada": "",
            "eventos": [],
            "detalles": [],
            "resultado_final": None,
        })
    hilo = threading.Thread(
        target=_runner,
        args=(run_id, list(cuentas_sel), list(urls), motivo, plataforma,
              n_hilos, paro),
        name=f"reportar-run-{run_id}",
        daemon=True,
    )
    hilo.start()
    return True


def _recoger_resultado_final() -> None:
    """Hilo principal: mueve el resultado final del worker a session_state."""
    with _LOCK:
        if _EJECUCION["activa"]:
            return
        run_id = _EJECUCION["run_id"]
        final = _EJECUCION["resultado_final"]
    if not run_id or final is None:
        return
    if st.session_state.get(CLAVE_RUN_REPORTES) == run_id:
        return
    st.session_state[CLAVE_RESULTADO_REPORTES] = _normalizar_resultado(final)
    st.session_state[CLAVE_RUN_REPORTES] = run_id


# ---------------------------------------------------------------------------
# Render (SOLO hilo principal)
# ---------------------------------------------------------------------------
def _render_ultimo_resultado() -> None:
    """Panel del ultimo resultado persistido (sobrevive a reruns/recargas)."""
    datos = st.session_state.get(CLAVE_RESULTADO_REPORTES)
    if not isinstance(datos, dict) or not datos:
        return

    fecha = str(datos.get("fecha") or "")
    st.success("Reportes terminados" + (f" — {fecha}" if fecha else ""))
    st.caption(
        f"Cuentas: **{datos.get('cuentas_totales', 0)}** · "
        f"URLs por cuenta: **{datos.get('urls', 0)}** · "
        f"En paralelo: **{datos.get('paralelo', 1)}**"
    )

    # Mismo flujo de resultados de siempre (exitos/fallidos/omitidas/detalle).
    mostrar_resultados(datos)

    paradas = int(datos.get("paradas") or 0)
    if paradas:
        st.warning(
            f"⛔ {paradas} cuenta(s) quedaron sin ejecutarse por el paro."
        )

    if st.button("🧹 Limpiar resultado", key="btn_rep_limpiar_resultado"):
        st.session_state.pop(CLAVE_RESULTADO_REPORTES, None)
        st.rerun()


def _render_proceso() -> None:
    """Panel en vivo de la campana (barra, contadores y ultimos resultados)."""
    snap = _snapshot()
    total = max(1, int(snap["total"]))
    hechas = min(int(snap["hechas"]), total)

    st.markdown(f"#### 🚩 Reportando... {hechas}/{snap['total']}")
    st.progress(min(1.0, hechas / total))

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("✅ Éxitos", snap["exitos"])
    with c2:
        st.metric("❌ Fallos", snap["fallidos"])
    with c3:
        st.metric(
            "👤 Última cuenta",
            f"@{snap['ultima']}" if snap["ultima"] else "—",
        )

    st.caption(
        f"Hechas: **{hechas}/{snap['total']}** · {snap['paralelo']} en "
        f"paralelo · URLs por cuenta: {snap['urls']}"
    )

    if snap["paro_solicitado"]:
        st.warning("⛔ Paro solicitado: terminando la cuenta en curso...")

    if st.button("⛔ Paro", key="btn_rep_paro"):
        _solicitar_paro()
        st.rerun(scope="app")

    if snap["eventos"]:
        st.markdown("**Últimos resultados**")
        for evento in snap["eventos"]:
            estado = evento.get("estado")
            icono = "✅" if estado == "ok" else ("⏸️" if estado == "omitida" else "❌")
            linea = f"{icono} @{evento.get('usuario', '?')}"
            detalle = evento.get("detalle") or ""
            if detalle:
                linea += f" — {detalle}"
            st.caption(linea)


def _refrescar_con_fragmento() -> bool:
    """`st.fragment(run_every=1s)` que repinta el proceso en vivo.

    Devuelve False si esta version de Streamlit no soporta fragmentos (el
    llamador cae al bucle `time.sleep(1); st.rerun()`)."""
    fabrica = getattr(st, "fragment", None)
    if not callable(fabrica):
        return False

    def _pasada():
        if not _esta_corriendo():
            st.rerun(scope="app")
            return
        _render_proceso()

    try:
        decorada = fabrica(run_every=1.0)(_pasada)
    except Exception:
        return False
    decorada()
    return True


def _render_proceso_activo() -> None:
    """Mientras corre, pinta el panel con auto-refresco (sin congelar la UI)."""
    if not _esta_corriendo():
        return
    if _refrescar_con_fragmento():
        return
    _render_proceso()
    time.sleep(1)
    st.rerun()


def render(usuario: dict):
    cabecera(
        "🚩 REPORTAR POSTS O CUENTAS",
        "Reporta publicaciones o perfiles en X con tus cuentas",
    )

    # Resultado final de una campana en segundo plano (hilo principal).
    _recoger_resultado_final()
    if not _esta_corriendo():
        _render_ultimo_resultado()

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

    ordenada = _ordenar_cuentas(cuentas)
    total = len(ordenada)

    # ---------------- Selector masivo de cuentas ----------------
    opciones_modo = [
        f"{MODO_TODAS} ({total})",
        MODO_PRIMERAS,
        MODO_RANGO,
        MODO_MANUAL,
    ]
    modo_label = st.radio(
        "Selección de cuentas",
        opciones_modo,
        key="rep_modo_cuentas",
        horizontal=True,
        help="Todas las activas, las primeras N, por rango de usuario o manual.",
    )
    modo = MODO_TODAS if str(modo_label).startswith(MODO_TODAS) else modo_label

    cuenta_opts = {f"@{c.usuario} (Grupo {c.grupo})": c for c in ordenada}
    primeras_n = 0
    desde = ""
    hasta = ""
    manual: list = []

    if modo == MODO_PRIMERAS:
        primeras_n = st.number_input(
            "¿Cuántas cuentas? (desde el inicio del orden alfabético)",
            min_value=1,
            max_value=max(1, total),
            value=min(20, total),
            step=1,
            key="rep_primeras_n",
            help="Se toman las primeras N del listado ordenado por usuario.",
        )
    elif modo == MODO_RANGO:
        col_desde, col_hasta = st.columns(2)
        with col_desde:
            desde = st.text_input(
                "Desde usuario (inclusive)",
                key="rep_rango_desde",
                placeholder="@cuenta",
            )
        with col_hasta:
            hasta = st.text_input(
                "Hasta usuario (inclusive)",
                key="rep_rango_hasta",
                placeholder="@cuenta",
            )
        rango_preview = _seleccionar_por_rango(ordenada, desde, hasta)
        if rango_preview:
            st.caption(
                f"Rango: **{rango_preview[0].usuario}** → "
                f"**{rango_preview[-1].usuario}** "
                f"({len(rango_preview)} de {total})"
            )
    elif modo == MODO_MANUAL:
        manual = st.multiselect("Cuentas", list(cuenta_opts), key="rep_cuentas")

    cuentas_sel = _aplicar_modo(
        ordenada,
        modo,
        primeras_n=primeras_n,
        desde=desde,
        hasta=hasta,
        manual=manual,
        cuenta_opts=cuenta_opts,
    )

    # ---------------- Motivo y URLs ----------------
    motivo_labels = list(MOTIVOS.values())
    motivo_sel = st.selectbox("Motivo del reporte", motivo_labels, key="rep_motivo")
    motivo = [k for k, v in MOTIVOS.items() if v == motivo_sel][0]

    st.caption(AYUDA_FORMATOS)
    urls_text = st.text_area("URLs (una por línea)", height=120, key="rep_urls")
    urls = [u.strip() for u in urls_text.splitlines() if u.strip()]

    # Seleccion y alcance: SIEMPRE visibles.
    st.caption(
        f"Seleccionadas: **{len(cuentas_sel)} cuentas** · URLs: "
        f"**{len(urls)}** · Total de reportes: "
        f"**{len(cuentas_sel) * len(urls)}**"
    )
    if cuentas_sel and urls:
        st.info(
            f"Se harán {len(cuentas_sel) * len(urls)} reportes "
            f"({len(cuentas_sel)} cuentas × {len(urls)} URLs)."
        )
    st.caption(NOTA_MASIVO)

    # ---------------- Proceso en curso ----------------
    if _esta_corriendo():
        _render_proceso_activo()
        return

    paralelo = st.number_input(
        "Cuentas en paralelo",
        min_value=1,
        max_value=3,
        value=2,
        step=1,
        key="rep_paralelo",
        help="Navegadores simultaneos (2-3 es lo estable en Railway).",
    )

    if st.button("🚩 Reportar", type="primary", key="btn_reportar"):
        if not cuentas_sel:
            st.warning("Selecciona cuentas.")
            return
        if not urls:
            st.warning("Pega al menos una URL.")
            return
        if _iniciar_ejecucion(cuentas_sel, urls, motivo, plataforma, paralelo):
            st.rerun()
        else:
            st.warning("Ya hay una ejecución de reportes en curso.")
