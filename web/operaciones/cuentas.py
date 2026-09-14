"""Página de CUENTAS: carga masiva de credenciales, validación de sesión
(sin navegador, vía httpx), secciones CI/CD/IP, sincronización del perfil real
desde X (httpx, sin Chrome), cambio de nombre/@ (Selenium + Chrome) e
inventario/exportación.

Reemplaza el flujo obsoleto de auto-registro con Grizzly SMS por la nueva
arquitectura: importación en lote (7 campos) + validador de sesión + inventario.
"""
import bisect
import os
import re

import streamlit as st

from core.database import get_db_session
from core.models import Cuenta
from core.registros import (
    TIPOS_CUENTA,
    etiqueta_tipo_cuenta,
    normalizar_tipo_cuenta,
)
from core.secciones import (
    SECCIONES,
    etiqueta_seccion,
    normalizar_seccion,
    seccion_desde_sector,
)
from web.ui import cabecera

# Estados posibles de `Cuenta.status` (los mismos del validador de sesiones).
ESTADOS = ["imported", "active", "expired", "suspended", "limited", "error"]

OPCION_TODAS = "Todas"
OPCION_SIN_ASIGNAR = "Sin asignar"
OPCION_SIN_DEFINIR = "Sin definir"

# Modos del selector masivo de cuentas (`_selector_masivo`).
MODO_FILTRO = "Filtro (todas las que cumplan)"
MODO_RANGO = "Rango (de cuenta a cuenta)"
MODO_CANTIDAD = "Cantidad (primeras/últimas N)"
MODO_MANUAL = "Manual (cuenta por cuenta)"
MODOS_SELECCION = [MODO_FILTRO, MODO_RANGO, MODO_CANTIDAD, MODO_MANUAL]

# Mismas reglas de X que usa `cuentas/generador_identidades.py`.
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{4,15}$")

TABS = [
    "📥 Importar",
    "🔎 Validar",
    "🗂️ Secciones CI/CD/IP",
    "🎭 Registro",
    "🎨 Perfiles",
    "🏷️ Nombres",
    "⏸️ Estado",
    "📷 Fotos",
    "🔄 Sincronizar desde X",
    "✏️ Cambiar nombre/@",
    "📋 Inventario",
    "📤 Exportar",
]


# ============================ HELPERS ============================

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


def _opciones_filtro_seccion(incluir_todas: bool = True) -> list:
    """Opciones legibles para los filtros por sección."""
    opciones = [etiqueta_seccion(clave) for clave in SECCIONES]
    opciones.append(OPCION_SIN_ASIGNAR)
    if incluir_todas:
        opciones.insert(0, OPCION_TODAS)
    return opciones


def _seccion_desde_filtro(opcion: str):
    """Traduce una opción de filtro a clave de sección.

    Devuelve None para "Todas" (sin filtro), "" para "Sin asignar" y
    "CI"/"CD"/"IP" para una sección concreta."""
    if opcion == OPCION_TODAS:
        return None
    if opcion == OPCION_SIN_ASIGNAR:
        return ""
    return normalizar_seccion(opcion)


def _normalizar_tipo_opcion(opcion: str) -> str:
    """Traduce una etiqueta o código de registro a la clave canónica.

    `core.registros.normalizar_tipo_cuenta` no reconoce la etiqueta completa
    "Activista / Técnico-coloquial" (la aplana a "activistatecnicocoloquial"),
    así que primero se traduce por etiqueta exacta de `TIPOS_CUENTA` y solo
    después se delega al normalizador. Devuelve "" para "Sin definir"."""
    if opcion == OPCION_SIN_DEFINIR:
        return ""
    for clave, etiqueta in TIPOS_CUENTA.items():
        if opcion == etiqueta:
            return clave
    return normalizar_tipo_cuenta(opcion)


def _tipo_desde_filtro(opcion: str):
    """Traduce una opción de filtro a clave de tipo de cuenta.

    Devuelve None para "Todas" (sin filtro), "" para "Sin definir" y
    "politica"/"activista"/"ciudadana" para un registro concreto."""
    if opcion == OPCION_TODAS:
        return None
    return _normalizar_tipo_opcion(opcion)


def _coincide_tipo(fila: dict, opcion: str) -> bool:
    """True si la fila del inventario pasa el filtro de registro elegido."""
    objetivo = _tipo_desde_filtro(opcion)
    if objetivo is None:
        return True
    return (fila.get("tipo_cuenta") or "") == objetivo


def _etiqueta_grupo(valor) -> str:
    """Etiqueta del grupo operativo: 'sin grupo' si es '' o NULL.

    El antiguo default 'A' ya se limpio en BD (core/database convierte los
    grupo='A' historicos a ''); aqui NUNCA se fuerza 'A': un grupo vacio se
    muestra tal cual como 'sin grupo'."""
    texto = (valor or "").strip() if valor is not None else ""
    return texto or "sin grupo"


# ------------------- Lógica pura del selector masivo -------------------
# Estas funciones no dependen de Streamlit (salvo por las etiquetas de
# `core.secciones`/`core.registros`) y se pueden probar offline con dicts
# simulados de cuentas: {"usuario", "status", "seccion", "tipo_cuenta", ...}.

def _ordenar_cuentas(cuentas: list) -> list:
    """Ordena por `usuario.lower()` (case-insensitive), de forma estable."""
    return sorted(
        cuentas or [], key=lambda f: str((f or {}).get("usuario") or "").lower()
    )


def _aplicar_filtro(
    lista: list,
    status: str = OPCION_TODAS,
    seccion: str = OPCION_TODAS,
    tipo: str = OPCION_TODAS,
    texto: str = "",
) -> list:
    """Modo Filtro: devuelve las cuentas que cumplen TODOS los criterios.

    - `status`: etiqueta "Todas" (sin filtro) o un status concreto.
    - `seccion`: "Todas"/"Sin asignar"/etiqueta de sección o código ("CI"...).
    - `tipo`: "Todas"/"Sin definir"/etiqueta de registro o código ("politica").
    - `texto`: substring de `usuario` (case-insensitive); "" = sin filtro.
    Devuelve la lista ordenada por usuario (case-insensitive).
    """
    ordenada = _ordenar_cuentas(lista)
    status_obj = (status or "").strip()
    filtrar_status = bool(status_obj) and status_obj.lower() != OPCION_TODAS.lower()
    seccion_obj = _seccion_desde_filtro(seccion) if isinstance(seccion, str) else seccion
    tipo_obj = _tipo_desde_filtro(tipo) if isinstance(tipo, str) else tipo
    texto_norm = (texto or "").strip().lower()

    salida = []
    for fila in ordenada:
        if filtrar_status and (fila.get("status") or "").lower() != status_obj.lower():
            continue
        if seccion_obj is not None and (fila.get("seccion") or "") != (seccion_obj or ""):
            continue
        if tipo_obj is not None and (fila.get("tipo_cuenta") or "") != (tipo_obj or ""):
            continue
        if texto_norm and texto_norm not in str(fila.get("usuario") or "").lower():
            continue
        salida.append(fila)
    return salida


def _aplicar_rango(
    lista: list,
    desde=None,
    hasta=None,
    por_numero: bool = False,
    ini: int = 1,
    fin: int = None,
) -> list:
    """Modo Rango: devuelve el subconjunto entre `desde` y `hasta` (inclusive).

    - Por usuario (`por_numero=False`): `desde`/`hasta` son nombres de usuario.
      Se comparan sin distinguir mayúsculas; si no existen se toma la posición
      alfabética más cercana (bisect). `None` = primero/último.
    - Por número (`por_numero=True`): `ini`/`fin` son posiciones 1-based sobre
      la lista ordenada, acotadas al total.
    Si el rango viene invertido, se intercambian los extremos.
    La lista devuelta conserva el orden por usuario (case-insensitive).
    """
    ordenada = _ordenar_cuentas(lista)
    total = len(ordenada)
    if total == 0:
        return []

    if por_numero:
        a = int(ini or 1)
        b = int(fin if fin is not None else total)
        a = max(1, min(a, total))
        b = max(1, min(b, total))
        if a > b:
            a, b = b, a
        return ordenada[a - 1:b]

    claves = [str(f.get("usuario") or "").lower() for f in ordenada]
    if desde:
        i = bisect.bisect_left(claves, str(desde).strip().lower())
    else:
        i = 0
    if hasta:
        j = bisect.bisect_right(claves, str(hasta).strip().lower()) - 1
    else:
        j = total - 1
    if i > j:
        i, j = j, i
    if i < 0 or j >= total:
        return []
    return ordenada[i:j + 1]


def _aplicar_cantidad(lista: list, n, primeras: bool = True) -> list:
    """Modo Cantidad: devuelve las primeras (o últimas) `n` cuentas.

    `n` se acota a [1, total]; con n<=0 devuelve []. Orden case-insensitive.
    """
    ordenada = _ordenar_cuentas(lista)
    total = len(ordenada)
    if total == 0:
        return []
    try:
        n = int(n)
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    if n >= total:
        return list(ordenada)
    return ordenada[:n] if primeras else ordenada[-n:]


@st.cache_data(ttl=15, show_spinner=False)
def _listar_cuentas(status_filtro: str = OPCION_TODAS, seccion_filtro=None) -> list:
    """Inventario de cuentas twitter como lista de dicts (datos desacoplados).

    `status_filtro`: "todas" o un status concreto.
    `seccion_filtro`: None = todas; "" = sin asignar; "CI"/"CD"/"IP" = concreta.
    Nunca lanza: ante error de BD muestra st.error y devuelve [].

    Cacheada 15s (`st.cache_data`) para que cambiar de pestaña no repita la
    consulta a la BD en cada rerun. Tras cualquier escritura de esta pagina se
    llama `_listar_cuentas.clear()` para no mostrar datos viejos."""
    filas = []
    try:
        with get_db_session() as db:
            q = db.query(Cuenta).filter(Cuenta.plataforma == "twitter")
            if status_filtro and str(status_filtro).lower() != OPCION_TODAS.lower():
                q = q.filter(Cuenta.status == status_filtro)
            cuentas = q.order_by(Cuenta.usuario).all()
            for c in cuentas:
                seccion = normalizar_seccion(getattr(c, "seccion", ""))
                if seccion_filtro is not None and seccion != (seccion_filtro or ""):
                    continue
                tipo = normalizar_tipo_cuenta(getattr(c, "tipo_cuenta", ""))
                filas.append(
                    {
                        "usuario": c.usuario,
                        "email": c.email or "",
                        "status": c.status or "",
                        "last_checked": _fmt_fecha(c.last_checked),
                        "cookies": "sí" if _tiene_cookies(c.cookies_json) else "no",
                        "seccion": seccion,
                        "seccion_etiqueta": etiqueta_seccion(seccion),
                        "tipo_cuenta": tipo,
                        "tipo_etiqueta": etiqueta_tipo_cuenta(tipo),
                        "handle_actual": (getattr(c, "handle_actual", "") or "").strip(),
                        "nombre_mostrado": (getattr(c, "nombre_mostrado", "") or "").strip(),
                        "nombre_propuesto": (
                            getattr(c, "nombre_propuesto", "") or ""
                        ).strip(),
                        "handle_propuesto": (
                            getattr(c, "handle_propuesto", "") or ""
                        ).strip(),
                        "password": getattr(c, "password", "") or "",
                        "sector": getattr(c, "sector", "") or "",
                        "grupo": (getattr(c, "grupo", "") or "").strip(),
                        "grupo_etiqueta": _etiqueta_grupo(
                            getattr(c, "grupo", "")
                        ),
                        "proxy": getattr(c, "proxy", "") or "",
                        "activa": bool(getattr(c, "activa", True)),
                        "avatar": bool(
                            str(getattr(c, "avatar_path", "") or "").strip()
                        ),
                        "banner": bool(
                            str(getattr(c, "banner_path", "") or "").strip()
                        ),
                        "avatar_path": getattr(c, "avatar_path", "") or "",
                        "banner_path": getattr(c, "banner_path", "") or "",
                        "perfil_personalidad": (
                            getattr(c, "perfil_personalidad", "") or ""
                        ).strip(),
                        "personalidad": (
                            getattr(c, "personalidad", "") or ""
                        ).strip(),
                    }
                )
    except Exception as e:
        st.error(f"No se pudo consultar la base de datos de cuentas: {e}")
    return filas


def _etiqueta_cuenta(fila: dict) -> str:
    """Etiqueta de cuenta para selectores: @usuario — handle (sección)."""
    usuario = fila.get("usuario", "")
    handle = fila.get("handle_actual") or usuario
    seccion = fila.get("seccion") or OPCION_SIN_ASIGNAR
    return f"@{usuario} — {handle} ({seccion})"


def _etiqueta_cuenta_perfil(fila: dict) -> str:
    """Etiqueta de cuenta para el cambio de perfil, marcando el handle real."""
    usuario = fila.get("usuario", "")
    handle = (fila.get("handle_actual") or "").strip()
    etiqueta = f"@{usuario}"
    if handle and handle.lower() != usuario.lower():
        etiqueta += f" (@{handle})"
    etiqueta += f" [{fila.get('seccion') or OPCION_SIN_ASIGNAR}]"
    return etiqueta


def _descripcion_seleccion(modo: str, total: int, cuentas: list, extra: str = "") -> str:
    """Caption con el modo/rango usado (Task 3): "Modo: ... — N cuentas"."""
    base = f"Modo: {modo}"
    if extra:
        base += f" ({extra})"
    return f"{base} — {len(cuentas)} cuentas"


def _selector_masivo(cuentas: list, key: str) -> list:
    """UI de selección de cuentas con 4 modos. Devuelve las cuentas elegidas.

    Modos (`st.radio`):
      1. "Filtro (todas las que cumplan)": status, sección, registro y texto de
         usuario; selecciona TODAS las que cumplan.
      2. "Rango (de cuenta a cuenta)": por usuario (Desde/Hasta sobre la lista
         ordenada, ambos inclusive, muestra la posición) o por número
         (number_input 1-based acotado al total).
      3. "Cantidad (primeras/últimas N)": radio Primeras/Últimas + N (1..total).
      4. "Manual (cuenta por cuenta)": el multiselect clásico.

    Siempre muestra "Seleccionadas: N de M", un expander con hasta 30 usuarios
    y aviso si N=0. Guarda un caption del modo/rango en
    `st.session_state[f"{key}_descripcion"]` y detalles en
    `st.session_state[f"{key}_detalle"]` (incluye `seccion_codigo` para los
    generadores de contenido). Todas las claves de widget usan `key` como
    prefijo para evitar colisiones entre pestañas.
    """
    ordenada = _ordenar_cuentas(cuentas)
    total = len(ordenada)

    if total == 0:
        st.info("No hay cuentas disponibles para seleccionar.")
        st.session_state[f"{key}_descripcion"] = ""
        st.session_state[f"{key}_detalle"] = {}
        return []

    modo = st.radio(
        "Modo de selección",
        MODOS_SELECCION,
        key=f"{key}_modo",
        horizontal=True,
        help="Elige cuentas por filtro, por rango, por cantidad o manualmente.",
    )

    detalle = {"modo": modo, "seccion_codigo": ""}
    seleccion = []
    extra = ""

    if modo == MODO_FILTRO:
        col_status, col_seccion, col_tipo, col_texto = st.columns(4)
        with col_status:
            status_f = st.selectbox(
                "Status",
                [OPCION_TODAS] + ESTADOS,
                key=f"{key}_filtro_status",
            )
        with col_seccion:
            seccion_f = st.selectbox(
                "Sección",
                _opciones_filtro_seccion(),
                key=f"{key}_filtro_seccion",
            )
        with col_tipo:
            tipo_f = st.selectbox(
                "Registro",
                [OPCION_TODAS] + list(TIPOS_CUENTA.values()) + [OPCION_SIN_DEFINIR],
                key=f"{key}_filtro_tipo",
            )
        with col_texto:
            texto_f = st.text_input(
                "Usuario contiene",
                key=f"{key}_filtro_texto",
                placeholder="ej. juan",
            )
        seleccion = _aplicar_filtro(ordenada, status_f, seccion_f, tipo_f, texto_f)
        detalle["status"] = status_f
        detalle["seccion"] = seccion_f
        detalle["tipo"] = tipo_f
        detalle["texto"] = texto_f
        detalle["seccion_codigo"] = _seccion_desde_filtro(seccion_f) or ""
        extra = (
            f"status={status_f}, sección={seccion_f}, registro={tipo_f}, "
            f"texto='{texto_f}'"
        )

    elif modo == MODO_RANGO:
        tipo_rango = st.radio(
            "Tipo de rango",
            ["Por usuario", "Por número"],
            key=f"{key}_rango_tipo",
            horizontal=True,
        )
        if tipo_rango == "Por usuario":
            usuarios_ord = [str(f.get("usuario") or "") for f in ordenada]
            col_desde, col_hasta = st.columns(2)
            with col_desde:
                desde = st.selectbox(
                    "Desde (usuario)",
                    usuarios_ord,
                    key=f"{key}_rango_desde",
                    format_func=lambda u: f"@{u}",
                )
            with col_hasta:
                hasta = st.selectbox(
                    "Hasta (usuario)",
                    usuarios_ord,
                    index=total - 1,
                    key=f"{key}_rango_hasta",
                    format_func=lambda u: f"@{u}",
                )
            pos_desde = usuarios_ord.index(desde) + 1
            pos_hasta = usuarios_ord.index(hasta) + 1
            aviso = ""
            if pos_desde > pos_hasta:
                aviso = " · se invertirá el orden (Desde va después de Hasta)"
            st.caption(
                f"Desde: **{pos_desde} de {total}** · "
                f"Hasta: **{pos_hasta} de {total}**{aviso}"
            )
            seleccion = _aplicar_rango(
                ordenada, desde, hasta, por_numero=False, ini=1, fin=total
            )
            extra = f"por usuario {desde} → {hasta}"
            detalle["rango_tipo"] = "Por usuario"
            detalle["desde"] = desde
            detalle["hasta"] = hasta
        else:
            col_ini, col_fin = st.columns(2)
            with col_ini:
                ini = st.number_input(
                    "desde la #",
                    min_value=1,
                    max_value=total,
                    value=1,
                    step=1,
                    key=f"{key}_rango_ini",
                )
            with col_fin:
                fin = st.number_input(
                    "hasta la #",
                    min_value=1,
                    max_value=total,
                    value=total,
                    step=1,
                    key=f"{key}_rango_fin",
                )
            seleccion = _aplicar_rango(
                ordenada, por_numero=True, ini=int(ini), fin=int(fin)
            )
            extra = f"por número {int(ini)}-{int(fin)}"
            detalle["rango_tipo"] = "Por número"
            detalle["ini"] = int(ini)
            detalle["fin"] = int(fin)

    elif modo == MODO_CANTIDAD:
        col_sentido, col_n = st.columns(2)
        with col_sentido:
            sentido = st.radio(
                "Tomar las",
                ["Primeras", "Últimas"],
                key=f"{key}_cant_sentido",
                horizontal=True,
            )
        default_n = min(20, total)
        with col_n:
            n = st.number_input(
                "Número de cuentas (N)",
                min_value=1,
                max_value=total,
                value=default_n,
                step=1,
                key=f"{key}_cant_n",
            )
        primeras = sentido == "Primeras"
        seleccion = _aplicar_cantidad(ordenada, int(n), primeras=primeras)
        extra = f"{sentido.lower()} {int(n)} de {total}"
        detalle["sentido"] = sentido
        detalle["n"] = int(n)

    else:  # MODO_MANUAL
        opciones = {_etiqueta_cuenta(f): f for f in ordenada}
        etiquetas = st.multiselect(
            "Cuentas (selección manual)",
            list(opciones.keys()),
            key=f"{key}_manual",
        )
        seleccion = [opciones[label] for label in etiquetas]
        extra = "selección una por una"

    descripcion = _descripcion_seleccion(modo.split(" ")[0], total, seleccion, extra)
    detalle["descripcion"] = descripcion
    detalle["modo"] = modo
    st.session_state[f"{key}_descripcion"] = descripcion
    st.session_state[f"{key}_detalle"] = detalle

    st.markdown(f"**Seleccionadas: {len(seleccion)} de {total}**")
    st.caption(descripcion)

    if not seleccion:
        st.warning("No hay cuentas seleccionadas con este modo/filtros.")
    else:
        with st.expander(
            f"👁️ Ver cuentas seleccionadas ({len(seleccion)})", expanded=False
        ):
            for fila in seleccion[:30]:
                usuario = fila.get("usuario") or ""
                handle = (fila.get("handle_actual") or "").strip()
                sufijo = f" (@{handle})" if handle and handle.lower() != usuario.lower() else ""
                st.markdown(f"- @{usuario}{sufijo}")
            if len(seleccion) > 30:
                st.caption(f"... y {len(seleccion) - 30} cuenta(s) más.")

    return seleccion


def _asignar_seccion(usuarios, codigo: str) -> int:
    """UPDATE masivo de `Cuenta.seccion`; devuelve cuántas filas cambió."""
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return 0
    with get_db_session() as db:
        cambiadas = (
            db.query(Cuenta)
            .filter(Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios))
            .update({Cuenta.seccion: codigo}, synchronize_session=False)
        )
    _listar_cuentas.clear()
    return cambiadas


def _asignar_tipo_cuenta(usuarios, codigo: str) -> int:
    """UPDATE masivo de `Cuenta.tipo_cuenta`; devuelve cuántas filas cambió."""
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return 0
    with get_db_session() as db:
        cambiadas = (
            db.query(Cuenta)
            .filter(Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios))
            .update({Cuenta.tipo_cuenta: codigo}, synchronize_session=False)
        )
    _listar_cuentas.clear()
    return cambiadas


def _asignar_perfil_personalidad(
    usuarios, codigo: str, regenerar_personalidad: bool = False
) -> int:
    """Asigna `Cuenta.perfil_personalidad` a las cuentas dadas.

    - `codigo` con perfil ("formal"/"ciudadano"/"popular") lo aplica directo;
      `codigo` vacio deja que `asignar_perfiles_personalidad` reparta los 3
      perfiles de forma EQUITATIVA entre las cuentas dadas.
    - `regenerar_personalidad=True` genera ademas `Cuenta.personalidad` con el
      tono del perfil (OpenAI + fallback local de cuentas/generador_identidades).
    - Devuelve cuantas cuentas quedaron con perfil asignado. Nunca lanza.
    """
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return 0
    try:
        from core.perfiles import normalizar_perfil

        codigo_norm = normalizar_perfil(codigo)

        if not regenerar_personalidad:
            with get_db_session() as db:
                query = db.query(Cuenta).filter(
                    Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios)
                )
                if codigo_norm:
                    cambiadas = query.update(
                        {Cuenta.perfil_personalidad: codigo_norm},
                        synchronize_session=False,
                    )
                    _listar_cuentas.clear()
                    return cambiadas
            # Reparto equitativo (solo cuentas sin perfil).
            from cuentas.generador_identidades import asignar_perfiles_personalidad

            resumen = asignar_perfiles_personalidad(usuarios)
            _listar_cuentas.clear()
            return int(resumen.get("repartidos", 0) or 0)

        # Con regeneracion de personalidad (OpenAI + fallback local).
        if codigo_norm:
            with get_db_session() as db:
                db.query(Cuenta).filter(
                    Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios)
                ).update(
                    {Cuenta.perfil_personalidad: codigo_norm},
                    synchronize_session=False,
                )
            _listar_cuentas.clear()
        else:
            from cuentas.generador_identidades import asignar_perfiles_personalidad

            asignar_perfiles_personalidad(usuarios)
            _listar_cuentas.clear()

        from cuentas.generador_identidades import asignar_personalidades

        resumen = asignar_personalidades(usuarios, forzar=True)
        _listar_cuentas.clear()
        return int(resumen.get("asignadas", 0) or 0)
    except Exception as e:
        st.error(f"No se pudo asignar el perfil: {e}")
        return 0


def _cambiar_estado(usuarios, activa: bool) -> int:
    """UPDATE masivo de `Cuenta.activa`; devuelve cuántas filas cambió.

    `activa=False` desactiva (las cuentas quedan excluidas de publicaciones,
    campañas y tareas, pero NO se borran: cookies y datos siguen intactos) y
    `activa=True` las reactiva. Limpia la caché de `_listar_cuentas` para que
    los cambios se vean de inmediato."""
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return 0
    with get_db_session() as db:
        cambiadas = (
            db.query(Cuenta)
            .filter(Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios))
            .update({Cuenta.activa: bool(activa)}, synchronize_session=False)
        )
    _listar_cuentas.clear()
    return cambiadas


def _guardar_propuestas_editadas(df) -> None:
    """Valida y persiste las propuestas editadas en el data_editor.

    Si CUALQUIER fila es inválida no se guarda nada (st.error + detalle)."""
    registros = []
    errores = []
    for _, row in df.iterrows():
        usuario = str(row.get("usuario") or "").strip()
        if not usuario:
            continue
        nombre = re.sub(r"\s+", " ", str(row.get("nombre_propuesto") or "").strip())
        handle = str(row.get("handle_propuesto") or "").strip().lstrip("@").strip()
        if not nombre or len(nombre) > 120:
            errores.append(
                f"@{usuario}: el nombre no puede estar vacío ni superar 120 caracteres."
            )
            continue
        if not HANDLE_RE.match(handle):
            errores.append(
                f"@{usuario}: handle inválido '{handle}' "
                "(4-15 caracteres, solo letras, números y _)."
            )
            continue
        registros.append((usuario, nombre, handle))

    if errores:
        st.error(
            "No se guardó nada. Corrige estos errores:\n\n"
            + "\n".join(f"- {e}" for e in errores)
        )
        return
    if not registros:
        st.warning("No hay filas válidas para guardar.")
        return

    try:
        with get_db_session() as db:
            filas = (
                db.query(Cuenta)
                .filter(Cuenta.usuario.in_([u for u, _, _ in registros]))
                .all()
            )
            por_usuario = {f.usuario: f for f in filas}
            guardadas = 0
            for usuario, nombre, handle in registros:
                cuenta = por_usuario.get(usuario)
                if cuenta is None:
                    continue
                cuenta.nombre_propuesto = nombre
                cuenta.handle_propuesto = handle
                guardadas += 1
        _listar_cuentas.clear()
        _flash(f"Propuestas guardadas: {guardadas} cuenta(s).")
        st.rerun()
    except Exception as e:
        st.error(f"No se pudieron guardar las propuestas: {e}")


def _flash(mensaje: str):
    """Guarda un mensaje para mostrarlo tras el siguiente st.rerun()."""
    st.session_state["cuentas_flash"] = mensaje


def _sincronizar_usuarios(usuarios, callback=None) -> dict:
    """Sincroniza una lista concreta de cuentas (misma forma que sincronizar_todas).

    Se usa para los alcances que `plataformas.twitter.perfil.sincronizar_todas`
    no cubre: cuentas seleccionadas y cuentas sin sección asignada."""
    from plataformas.twitter.perfil import sincronizar_cuenta

    resumen = {
        "total": 0,
        "ok": 0,
        "errores": 0,
        "sin_datos": 0,
        "cambios_nombre": 0,
        "cambios_handle": 0,
        "detalle": [],
    }
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return resumen

    try:
        with get_db_session() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios))
                .order_by(Cuenta.usuario)
                .all()
            )
    except Exception as e:
        st.error(f"No se pudieron cargar las cuentas a sincronizar: {e}")
        return resumen

    resumen["total"] = len(cuentas)
    for idx, cuenta in enumerate(cuentas, start=1):
        resultado = sincronizar_cuenta(cuenta)
        resumen["detalle"].append(resultado)

        error = resultado.get("error", "")
        if error == "":
            resumen["ok"] += 1
        elif error == "sin_datos":
            resumen["sin_datos"] += 1
        else:
            resumen["errores"] += 1

        if resultado.get("cambio_nombre"):
            resumen["cambios_nombre"] += 1
        if resultado.get("cambio_handle"):
            resumen["cambios_handle"] += 1

        if callback:
            try:
                callback(idx, resumen["total"], resultado.get("usuario", ""), resultado)
            except Exception:
                pass

    return resumen


def _mostrar_resumen_sincronizacion(res: dict):
    """Métricas + tabla de cambios/errores de una sincronización de perfiles."""
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total", res.get("total", 0))
    c2.metric("Leídas", res.get("ok", 0))
    c3.metric("Cambios de @", res.get("cambios_handle", 0))
    c4.metric("Cambios de nombre", res.get("cambios_nombre", 0))
    c5.metric("Errores", res.get("errores", 0))
    c6.metric("Sin datos", res.get("sin_datos", 0))

    filas = []
    for r in res.get("detalle") or []:
        if r.get("cambio_handle") or r.get("cambio_nombre") or r.get("error"):
            filas.append(
                {
                    "usuario interno": r.get("usuario", ""),
                    "@ en X": r.get("handle", ""),
                    "nombre en X": r.get("nombre", ""),
                    "cambió @": "sí" if r.get("cambio_handle") else "no",
                    "cambió nombre": "sí" if r.get("cambio_nombre") else "no",
                    "error": r.get("error", ""),
                }
            )

    if filas:
        st.dataframe(filas, use_container_width=True)
    else:
        st.info("No hubo cambios ni errores: los perfiles ya estaban al día.")


# ============================ TABS ============================

def _tab_importar():
    st.markdown("### 📥 Importar lote de cuentas")
    st.caption(
        "Una cuenta por línea, campos separados por `:`:\n"
        "`username:password:totp_secret:email:email_password:auth_token:cookies_base64`\n"
        "El 7º campo (`cookies_base64`) es **opcional**: si no lo traes, la cuenta "
        "igual se importa y el validador obtiene el `ct0` con el `auth_token`."
    )

    texto_lote = st.text_area(
        "Pega aquí el lote (una cuenta por línea)", height=220, key="imp_texto"
    )
    archivo = st.file_uploader("o sube un archivo .txt", type=["txt"], key="imp_archivo")

    st.markdown("#### 🏷️ Sección y registro del lote")
    st.caption(
        "Se aplican a TODAS las líneas importadas. En cuentas ya existentes "
        "solo se pisan si eliges un valor distinto de 'Sin asignar' / 'Sin definir'."
    )
    col_sec, col_tipo = st.columns(2)
    with col_sec:
        seccion_lote = st.selectbox(
            "Sección para este lote",
            [OPCION_SIN_ASIGNAR] + [etiqueta_seccion(clave) for clave in SECCIONES],
            key="imp_seccion_lote",
        )
    with col_tipo:
        tipo_lote = st.selectbox(
            "Tipo de cuenta del lote",
            [OPCION_SIN_DEFINIR] + list(TIPOS_CUENTA.values()),
            key="imp_tipo_lote",
            help=(
                "Política = lenguaje institucional; "
                "Activista = técnico-coloquial; "
                "Ciudadana = persona real/coloquial."
            ),
        )

    if st.button("🚀 Importar", type="primary", use_container_width=True, key="btn_importar"):
        if archivo is not None:
            contenido = archivo.getvalue().decode("utf-8", errors="replace")
        else:
            contenido = texto_lote

        if not (contenido or "").strip():
            st.warning("No hay contenido para importar. Pega el lote o sube un archivo .txt.")
        else:
            try:
                from cuentas.importador import importar_lote

                res = importar_lote(
                    contenido,
                    seccion=normalizar_seccion(seccion_lote),
                    tipo_cuenta=_normalizar_tipo_opcion(tipo_lote),
                )
                _listar_cuentas.clear()
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Importadas", res.get("importadas", 0))
                c2.metric("Actualizadas", res.get("actualizadas", 0))
                c3.metric("Errores", res.get("errores", 0))
                c4.metric("Total", res.get("total", 0))

                if res.get("detalle_errores"):
                    with st.expander("⚠️ Errores de parseo"):
                        for err in res["detalle_errores"]:
                            st.markdown(f"- {err}")
            except Exception as e:
                st.error(f"Falló la importación: {e}")


def _tab_validar():
    st.markdown("### 🔎 Validar sesiones (httpx, sin abrir navegador)")
    st.caption(
        "Hace peticiones reales a X vía proxy para detectar cuentas activas, "
        "expiradas, suspendidas o limitadas, y persiste `status` + `last_checked`."
    )

    alcance = st.selectbox(
        "Alcance",
        ["imported", "todas", "active", "expired", "suspended", "limited", "error"],
        help="'todas' valida todas las cuentas; el resto filtra por su estado actual.",
        key="val_alcance",
    )

    if st.button("🔎 Validar ahora", type="primary", use_container_width=True, key="btn_validar"):
        try:
            from plataformas.twitter.session_validator import validar_todas

            solo = None if alcance == "todas" else alcance
            barra = st.progress(0.0)
            estado = st.empty()

            def _cb(actual, total, usuario, est):
                barra.progress(min(1.0, (actual / total) if total else 1.0))
                estado.write(f"🔎 @{usuario} → {est}")

            res = validar_todas(solo_status=solo, callback=_cb)
            _listar_cuentas.clear()
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


def _tab_secciones():
    st.markdown("### 🗂️ Secciones CI / CD / IP")
    st.caption(
        "Clasifica cada cuenta de Twitter en **CI** (Centro-Izquierda), "
        "**CD** (Centro-Derecha) o **IP** (Institución Privada)."
    )

    todas = _listar_cuentas(OPCION_TODAS)

    conteos = {clave: 0 for clave in SECCIONES}
    conteos[""] = 0
    for fila in todas:
        conteos[fila["seccion"] or ""] = conteos.get(fila["seccion"] or "", 0) + 1

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("CI — Centro-Izquierda", conteos.get("CI", 0))
    c2.metric("CD — Centro-Derecha", conteos.get("CD", 0))
    c3.metric("IP — Institución Privada", conteos.get("IP", 0))
    c4.metric("Sin asignar", conteos.get("", 0))

    st.markdown("---")

    filtro = st.selectbox(
        "Filtrar por sección", _opciones_filtro_seccion(), key="sec_filtro"
    )
    filas = _listar_cuentas(OPCION_TODAS, _seccion_desde_filtro(filtro))
    tabla = [
        {
            "usuario": f["usuario"],
            "handle_actual": f["handle_actual"],
            "nombre_mostrado": f["nombre_mostrado"],
            "seccion": f["seccion_etiqueta"],
            "status": f["status"],
        }
        for f in filas
    ]
    if tabla:
        st.dataframe(tabla, use_container_width=True)
    else:
        st.info("No hay cuentas con ese filtro de sección.")

    st.markdown("---")
    st.markdown("#### 💾 Asignación masiva")

    opciones = {_etiqueta_cuenta(f): f for f in todas}
    if not opciones:
        st.info("No hay cuentas de Twitter para asignar.")
        return

    etiquetas_seccion = [etiqueta_seccion(clave) for clave in SECCIONES]
    etiquetas_seccion.append(etiqueta_seccion(""))

    seleccion = _selector_masivo(todas, "sec_selector")
    descripcion_sel = st.session_state.get("sec_selector_descripcion", "")
    destino = st.selectbox("Sección destino", etiquetas_seccion, key="sec_destino")
    if descripcion_sel:
        st.caption(f"Se aplicarán los cambios con: {descripcion_sel}")

    if st.button(
        "💾 Asignar sección",
        type="primary",
        use_container_width=True,
        key="btn_sec_asignar",
    ):
        if not seleccion:
            st.warning("Selecciona al menos una cuenta.")
        else:
            usuarios = [f["usuario"] for f in seleccion]
            codigo = normalizar_seccion(destino)
            try:
                actualizadas = _asignar_seccion(usuarios, codigo)
                _flash(
                    f"Sección {etiqueta_seccion(codigo)} asignada a "
                    f"{actualizadas} cuenta(s). {descripcion_sel}"
                )
                st.rerun()
            except Exception as e:
                st.error(f"No se pudo asignar la sección: {e}")

    st.markdown("---")
    st.markdown("#### ⚡ Cambio rápido de una cuenta")

    etiqueta_rapida = st.selectbox("Cuenta", list(opciones.keys()), key="sec_rapida_cuenta")
    seccion_rapida = st.selectbox(
        "Nueva sección", etiquetas_seccion, key="sec_rapida_destino"
    )
    if st.button(
        "💾 Cambiar sección de esta cuenta",
        use_container_width=True,
        key="btn_sec_rapida",
    ):
        usuario_rapido = opciones[etiqueta_rapida]["usuario"]
        codigo = normalizar_seccion(seccion_rapida)
        try:
            actualizadas = _asignar_seccion([usuario_rapido], codigo)
            _flash(
                f"Sección {etiqueta_seccion(codigo)} asignada a "
                f"@{usuario_rapido} ({actualizadas} cuenta(s))."
            )
            st.rerun()
        except Exception as e:
            st.error(f"No se pudo asignar la sección: {e}")

    st.markdown("---")
    if st.button("🪄 Preclasificar por sector", key="btn_sec_preclasificar"):
        try:
            with get_db_session() as db:
                pendientes = (
                    db.query(Cuenta).filter(Cuenta.plataforma == "twitter").all()
                )
                cambios = 0
                for c in pendientes:
                    if normalizar_seccion(getattr(c, "seccion", "")):
                        continue
                    codigo = seccion_desde_sector(getattr(c, "sector", ""))
                    if codigo:
                        c.seccion = codigo
                        cambios += 1
            _listar_cuentas.clear()
            if cambios:
                _flash(
                    f"Se preclasificaron {cambios} cuenta(s) según su sector."
                )
                st.rerun()
            else:
                st.info("No había cuentas sin sección con sector reconocible.")
        except Exception as e:
            st.error(f"No se pudo preclasificar: {e}")


def _tab_registro():
    st.markdown("### 🎭 Registro de lenguaje")
    st.caption(
        "Clasifica cada cuenta según su voz: **Política / Institucional** "
        "(lenguaje cuidado, sin coloquialismos), **Activista / Técnico-coloquial** "
        "(términos políticos con lenguaje popular) o **Ciudadana / Persona real** "
        "(español mexicano coloquial). Al publicar, cada cuenta recibirá textos "
        "acordes a su registro."
    )

    todas = _listar_cuentas(OPCION_TODAS)

    conteos = {clave: 0 for clave in TIPOS_CUENTA}
    conteos[""] = 0
    for fila in todas:
        clave = fila.get("tipo_cuenta") or ""
        conteos[clave] = conteos.get(clave, 0) + 1

    # Una métrica por tipo definido en core.registros + "Sin definir".
    columnas = st.columns(len(TIPOS_CUENTA) + 1)
    for col, clave in zip(columnas, TIPOS_CUENTA):
        col.metric(TIPOS_CUENTA[clave], conteos.get(clave, 0))
    columnas[-1].metric(OPCION_SIN_DEFINIR, conteos.get("", 0))

    st.markdown("---")

    tabla = [
        {
            "usuario": f["usuario"],
            "@ actual": f["handle_actual"] or f["usuario"],
            "nombre": f["nombre_mostrado"],
            "sección": f["seccion_etiqueta"],
            "tipo": f["tipo_etiqueta"],
            "status": f["status"],
        }
        for f in todas
    ]
    if tabla:
        st.dataframe(tabla, use_container_width=True)
    else:
        st.info("No hay cuentas de Twitter en la base de datos.")

    st.markdown("---")
    st.markdown("#### 💾 Asignación masiva de registro")

    opciones = {_etiqueta_cuenta(f): f for f in todas}
    if not opciones:
        return

    etiquetas_tipo = list(TIPOS_CUENTA.values()) + [OPCION_SIN_DEFINIR]

    seleccion = _selector_masivo(todas, "reg_selector")
    descripcion_sel = st.session_state.get("reg_selector_descripcion", "")
    destino = st.selectbox("Registro destino", etiquetas_tipo, key="reg_destino")
    if descripcion_sel:
        st.caption(f"Se aplicarán los cambios con: {descripcion_sel}")

    if st.button(
        "💾 Asignar registro",
        type="primary",
        use_container_width=True,
        key="btn_reg_asignar",
    ):
        if not seleccion:
            st.warning("Selecciona al menos una cuenta.")
        else:
            usuarios = [f["usuario"] for f in seleccion]
            codigo = _normalizar_tipo_opcion(destino)
            try:
                actualizadas = _asignar_tipo_cuenta(usuarios, codigo)
                _flash(
                    f"Registro '{etiqueta_tipo_cuenta(codigo)}' asignado a "
                    f"{actualizadas} cuenta(s). {descripcion_sel}"
                )
                st.rerun()
            except Exception as e:
                st.error(f"No se pudo asignar el registro: {e}")

    st.markdown("---")
    st.markdown("#### ⚡ Cambio rápido de una cuenta")

    etiqueta_rapida = st.selectbox(
        "Cuenta", list(opciones.keys()), key="reg_rapida_cuenta"
    )
    registro_rapido = st.selectbox(
        "Nuevo registro", etiquetas_tipo, key="reg_rapida_destino"
    )
    if st.button(
        "💾 Cambiar registro de esta cuenta",
        use_container_width=True,
        key="btn_reg_rapida",
    ):
        usuario_rapido = opciones[etiqueta_rapida]["usuario"]
        codigo = _normalizar_tipo_opcion(registro_rapido)
        try:
            actualizadas = _asignar_tipo_cuenta([usuario_rapido], codigo)
            _flash(
                f"Registro '{etiqueta_tipo_cuenta(codigo)}' asignado a "
                f"@{usuario_rapido} ({actualizadas} cuenta(s))."
            )
            st.rerun()
        except Exception as e:
            st.error(f"No se pudo asignar el registro: {e}")


def _tab_perfiles():
    """Perfiles de redaccion (Formal / Ciudadano / Popular) por cuenta.

    Los 3 perfiles definen el formato EXACTO del contenido que genera la IA:
    - Formal/Estructurado: analitico en 3 partes (Titulo, Descripcion, Conclusion).
    - Ciudadano Promedio: par de renglones con analisis intermedio.
    - Popular/Organico: casual, de un renglon, con faltas intencionales.
    Se reparten EQUITATIVAMENTE entre las cuentas (165 -> 55 por perfil).
    """
    from core.perfiles import (
        PERFILES_PERSONALIDAD,
        etiqueta_perfil,
        normalizar_perfil,
    )

    st.markdown("### 🎨 Perfiles de redacción")
    st.caption(
        "Divide el contenido de las cuentas en **3 perfiles** para que no "
        "escriban igual: **Formal/Estructurado** (Título, Descripción y "
        "Conclusión), **Ciudadano Promedio** (un par de renglones, análisis "
        "intermedio) y **Popular/Orgánico** (un renglón casual, con faltas de "
        "ortografía intencionales). Se reparten de forma equitativa."
    )

    todas = _listar_cuentas(OPCION_TODAS)
    if not todas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    conteos = {clave: 0 for clave in PERFILES_PERSONALIDAD}
    conteos[""] = 0
    extranos = 0
    for fila in todas:
        clave = normalizar_perfil(fila.get("perfil_personalidad") or "")
        if clave:
            conteos[clave] = conteos.get(clave, 0) + 1
        elif (fila.get("perfil_personalidad") or "").strip():
            extranos += 1
        else:
            conteos[""] += 1

    columnas = st.columns(len(PERFILES_PERSONALIDAD) + 1)
    for col, clave in zip(columnas, PERFILES_PERSONALIDAD):
        col.metric(PERFILES_PERSONALIDAD[clave], conteos.get(clave, 0))
    columnas[-1].metric("Sin perfil", conteos.get("", 0) + extranos)

    tabla = [
        {
            "usuario": f["usuario"],
            "@ actual": f["handle_actual"] or f["usuario"],
            "perfil": etiqueta_perfil(f.get("perfil_personalidad")),
            "personalidad": " ".join((f.get("personalidad") or "").split())[:90],
            "registro": f.get("tipo_etiqueta") or OPCION_SIN_DEFINIR,
            "status": f.get("status") or "",
        }
        for f in todas
    ]
    st.dataframe(tabla, use_container_width=True)

    st.markdown("---")
    st.markdown("#### ⚖️ Reparto equitativo (55/55/55 con 165 cuentas)")

    seleccion = _selector_masivo(todas, "perf_selector")
    descripcion_sel = st.session_state.get("perf_selector_descripcion", "")

    col_opt1, col_opt2 = st.columns(2)
    with col_opt1:
        forzar = st.checkbox(
            "Forzar regeneración de personalidad",
            value=False,
            key="perf_forzar",
            help=(
                "Vuelve a generar el texto de personalidad de cada cuenta con "
                "el tono de su perfil (OpenAI + fallback local)."
            ),
        )
    with col_opt2:
        solo_sin_perfil = st.checkbox(
            "Respetar cuentas que ya tienen perfil",
            value=not forzar,
            key="perf_solo_sin",
            disabled=forzar,
            help="Si está activo, solo completa las cuentas sin perfil.",
        )

    if descripcion_sel:
        st.caption(f"Se aplicará el reparto con: {descripcion_sel}")

    if st.button(
        "⚖️ Generar/Repartir perfiles",
        type="primary",
        use_container_width=True,
        key="btn_perf_repartir",
    ):
        with st.spinner("Repartiendo perfiles y generando personalidades..."):
            try:
                usuarios = [f["usuario"] for f in seleccion]
                if forzar or not solo_sin_perfil:
                    # Rebalanceo global: deja 55/55/55 y regenera personalidades
                    # de las cuentas que cambian de perfil.
                    from cuentas.generador_identidades import rebalancear_perfiles

                    resumen = rebalancear_perfiles(
                        usuarios or None,
                        regenerar_personalidad=bool(forzar),
                    )
                    repartidos = int(resumen.get("asignados", 0) or 0)
                    respetadas = int(resumen.get("conservados", 0) or 0)
                    hist = resumen.get("por_perfil", {}) or {}
                else:
                    from cuentas.generador_identidades import (
                        asignar_perfiles_personalidad,
                    )

                    resumen = asignar_perfiles_personalidad(
                        usuarios or None, forzar=False
                    )
                    repartidos = int(resumen.get("repartidos", 0) or 0)
                    respetadas = int(resumen.get("respetadas", 0) or 0)
                    hist = resumen.get("por_perfil", {}) or {}

                _flash(
                    f"Perfiles repartidos a {repartidos} cuenta(s) "
                    f"({respetadas} respetadas). Histograma: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(hist.items()))
                )
                if resumen.get("errores"):
                    st.warning("Avisos: " + "; ".join(map(str, resumen["errores"][:5])))
                st.rerun()
            except Exception as e:
                st.error(f"No se pudo repartir los perfiles: {e}")

    st.markdown("---")
    st.markdown("#### ⚡ Cambio rápido de una cuenta")

    etiquetas = {_etiqueta_cuenta(f): f for f in todas}
    opciones_perfil = {
        etiqueta: clave for clave, etiqueta in PERFILES_PERSONALIDAD.items()
    }

    col_a, col_b, col_c = st.columns([2, 1.4, 1.4])
    with col_a:
        etiqueta_cuenta = st.selectbox(
            "Cuenta", list(etiquetas.keys()), key="perf_rapida_cuenta"
        )
    with col_b:
        perfil_destino = st.selectbox(
            "Nuevo perfil", list(opciones_perfil.keys()), key="perf_rapida_destino"
        )
    with col_c:
        regenerar_rapido = st.checkbox(
            "Regenerar personalidad", value=True, key="perf_rapida_regen"
        )

    if st.button(
        "💾 Cambiar perfil de esta cuenta",
        use_container_width=True,
        key="btn_perf_rapida",
    ):
        usuario = etiquetas[etiqueta_cuenta]["usuario"]
        codigo = opciones_perfil[perfil_destino]
        with st.spinner("Aplicando perfil..."):
            cambiadas = _asignar_perfil_personalidad(
                [usuario], codigo, regenerar_personalidad=bool(regenerar_rapido)
            )
        _flash(
            f"Perfil '{etiqueta_perfil(codigo)}' asignado a @{usuario} "
            f"({cambiadas} cuenta(s))."
        )
        st.rerun()


def _etiqueta_propuesta(fila: dict) -> str:
    """Etiqueta para los selectores de propuestas pendientes."""
    usuario = fila.get("usuario", "")
    handle = fila.get("handle_propuesto") or fila.get("handle_actual") or usuario
    return f"@{usuario} → @{handle} ({fila.get('tipo_etiqueta') or OPCION_SIN_DEFINIR})"


def _tab_nombres():
    st.markdown("### 🏷️ Nombres propuestos (display name + @)")
    st.caption(
        "Genera propuestas de identidad para las cuentas: **cuentas de "
        "movimiento** (ej. Ciudadanía Feliz, Ciudad Unida) para perfiles "
        "políticos y **nombres mexicanos de persona real** para las ciudadanas. "
        "Las propuestas se guardan en la BD y puedes editarlas antes de "
        "aplicarlas en X."
    )

    errores_pend = st.session_state.pop("nom_errores", None)
    if errores_pend:
        with st.expander(f"⚠️ Errores de la última generación ({len(errores_pend)})"):
            for err in errores_pend:
                st.markdown(f"- {err}")

    # ---------------- Selección de cuentas ----------------
    st.markdown("#### 1️⃣ Selecciona las cuentas")
    st.caption(
        "Elige por filtro, rango (de cuenta a cuenta o por número), cantidad "
        "(primeras/últimas N) o manualmente."
    )
    todas = _listar_cuentas(OPCION_TODAS)
    seleccion_masiva = _selector_masivo(todas, "nom_selector")
    usuarios = [f["usuario"] for f in seleccion_masiva]
    descripcion_sel = st.session_state.get("nom_selector_descripcion", "")
    if descripcion_sel:
        st.caption(f"Cuentas objetivo: **{len(usuarios)}** — {descripcion_sel}")
    else:
        st.caption(f"Cuentas objetivo: **{len(usuarios)}**")

    # ---------------- Generación ----------------
    st.markdown("#### 2️⃣ Genera las propuestas")
    tipo_etiqueta = st.selectbox(
        "Tipo de identidad",
        [
            "Auto (según tipo de cuenta)",
            "Solo personas reales",
            "Solo cuentas de movimiento",
        ],
        key="nom_tipo_identidad",
    )
    tipo_mapa = {
        "Auto (según tipo de cuenta)": "auto",
        "Solo personas reales": "persona",
        "Solo cuentas de movimiento": "movimiento",
    }
    if tipo_etiqueta.startswith("Auto"):
        st.caption(
            "Auto: política → cuenta de movimiento; ciudadana → persona real; "
            "sin definir → aleatorio 50/50."
        )

    if st.button(
        "✨ Generar propuestas",
        type="primary",
        use_container_width=True,
        key="btn_nom_generar",
    ):
        if not usuarios:
            st.warning("No hay cuentas que cumplan los filtros.")
        else:
            seccion_ctx = (
                st.session_state.get("nom_selector_detalle") or {}
            ).get("seccion_codigo") or ""
            try:
                from cuentas.generador_identidades import asignar_propuestas

                with st.spinner(
                    f"✨ Generando propuestas para {len(usuarios)} cuenta(s)..."
                ):
                    res = asignar_propuestas(
                        usuarios,
                        tipo_mapa[tipo_etiqueta],
                        seccion=seccion_ctx,
                        dry_run=False,
                    )
                _listar_cuentas.clear()
                c1, c2, c3 = st.columns(3)
                c1.metric("Total", res.get("total", 0))
                c2.metric("Propuestas", res.get("ok", 0))
                c3.metric("Errores", len(res.get("errores") or []))
                if descripcion_sel:
                    st.caption(f"Generado con: {descripcion_sel}")
                _flash(
                    f"Propuestas generadas: {res.get('ok', 0)}/"
                    f"{res.get('total', 0)} cuenta(s)."
                )
                if res.get("errores"):
                    st.session_state["nom_errores"] = list(res["errores"])[:50]
                st.rerun()
            except Exception as e:
                st.error(f"Falló la generación de propuestas: {e}")

    # ---------------- Tabla editable ----------------
    st.markdown("---")
    st.markdown("#### 3️⃣ Revisa y guarda las propuestas")
    con_propuesta = [
        f
        for f in _listar_cuentas(OPCION_TODAS)
        if (f.get("nombre_propuesto") or f.get("handle_propuesto"))
    ]

    if not con_propuesta:
        st.info(
            "No hay propuestas pendientes. Genera propuestas arriba para verlas aquí."
        )
        return

    try:
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "usuario": f["usuario"],
                    "@ actual": f["handle_actual"] or f["usuario"],
                    "tipo": f["tipo_etiqueta"],
                    "nombre_propuesto": f["nombre_propuesto"],
                    "handle_propuesto": f["handle_propuesto"],
                }
                for f in con_propuesta
            ]
        )
        editado = st.data_editor(
            df,
            key="nom_editor_propuestas",
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            column_config={
                "usuario": st.column_config.TextColumn("usuario", disabled=True),
                "@ actual": st.column_config.TextColumn("@ actual", disabled=True),
                "tipo": st.column_config.TextColumn("tipo", disabled=True),
                "nombre_propuesto": st.column_config.TextColumn(
                    "Nombre propuesto", width="large", required=True
                ),
                "handle_propuesto": st.column_config.TextColumn(
                    "Handle propuesto (sin @)", width="medium", required=True
                ),
            },
        )
    except Exception as e:
        st.error(f"No se pudo mostrar el editor de propuestas: {e}")
        return

    if st.button(
        "💾 Guardar cambios",
        type="primary",
        use_container_width=True,
        key="btn_nom_guardar",
    ):
        _guardar_propuestas_editadas(editado)

    opciones_aplicar = {_etiqueta_propuesta(f): f for f in con_propuesta}

    # ---------------- Aplicación individual ----------------
    st.markdown("---")
    st.markdown("#### 4️⃣ Aplicar una propuesta en X (Selenium)")
    st.warning(
        "Requiere Chrome instalado. El cambio de nombre/@ es **real e "
        "irreversible** en X; X puede pedir verificación adicional."
    )

    etiqueta_ap = st.selectbox(
        "Cuenta con propuesta",
        list(opciones_aplicar.keys()),
        key="nom_aplicar_cuenta",
    )
    fila_ap = opciones_aplicar[etiqueta_ap]
    usuario_ap = fila_ap["usuario"]
    st.markdown(
        f"- **Nombre propuesto:** {fila_ap['nombre_propuesto'] or '_(sin cambio)_'}"
    )
    st.markdown(
        f"- **Handle propuesto:** @{fila_ap['handle_propuesto'] or '_(sin cambio)_'}"
    )
    password = st.text_input(
        "Contraseña de la cuenta",
        value=fila_ap.get("password") or "",
        type="password",
        key=f"nom_aplicar_pass_{usuario_ap}",
    )
    confirmado = st.checkbox(
        "Confirmo que quiero modificar el perfil REAL de esta cuenta",
        key=f"nom_aplicar_confirm_{usuario_ap}",
    )
    if st.button(
        "✏️ Aplicar en X (Selenium)",
        type="primary",
        use_container_width=True,
        key=f"nom_aplicar_btn_{usuario_ap}",
    ):
        if not confirmado:
            st.warning("Marca la casilla de confirmación para continuar.")
        else:
            with st.spinner(
                f"Abriendo Chrome y aplicando la propuesta de @{usuario_ap}..."
            ):
                try:
                    from cuentas.generador_identidades import aplicar_propuesta

                    res = aplicar_propuesta(usuario_ap, password)
                except Exception as e:
                    res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            _mostrar_resultado_aplicacion(res, usuario_ap)

    # ---------------- Aplicación en lote ----------------
    st.markdown("---")
    st.markdown("#### 5️⃣ Aplicar en lote (Chrome, lento)")
    seleccion_lote = st.multiselect(
        "Cuentas con propuesta a aplicar",
        list(opciones_aplicar.keys()),
        key="nom_lote_cuentas",
    )
    confirmado_lote = st.checkbox(
        "Confirmo que quiero aplicar las propuestas seleccionadas en X, "
        "una por una (puede tardar horas)",
        key="nom_lote_confirm",
    )
    if st.button(
        "✏️ Aplicar en lote (Chrome, lento)",
        type="primary",
        use_container_width=True,
        key="btn_nom_lote",
    ):
        if not confirmado_lote:
            st.warning("Marca la casilla de confirmación para continuar.")
        elif not seleccion_lote:
            st.warning("Selecciona al menos una cuenta.")
        else:
            usuarios_lote = [
                opciones_aplicar[label]["usuario"]
                for label in seleccion_lote
                if label in opciones_aplicar
            ]
            _aplicar_propuestas_en_lote(usuarios_lote)

    # ---------------- Descartar ----------------
    if st.button(
        "🗑️ Descartar propuestas seleccionadas",
        use_container_width=True,
        key="btn_nom_descartar",
    ):
        if not seleccion_lote:
            st.warning(
                "Selecciona al menos una cuenta en la lista del punto 5️⃣ para "
                "descartar sus propuestas."
            )
        else:
            try:
                from cuentas.generador_identidades import descartar_propuesta

                descartadas = 0
                for label in seleccion_lote:
                    fila_lote = opciones_aplicar.get(label)
                    if fila_lote and descartar_propuesta(fila_lote["usuario"]):
                        descartadas += 1
                _listar_cuentas.clear()
                _flash(f"Propuestas descartadas: {descartadas} cuenta(s).")
                st.rerun()
            except Exception as e:
                st.error(f"No se pudieron descartar las propuestas: {e}")


def _mostrar_resultado_aplicacion(res: dict, usuario: str):
    """Muestra el resultado de `aplicar_propuesta` de forma legible."""
    if res.get("ok"):
        st.success(f"Propuesta aplicada en @{usuario}.")
    else:
        st.error(
            f"No se pudo aplicar la propuesta de @{usuario}: "
            f"{res.get('error') or 'sin detalle'}"
        )
    if res.get("nombre"):
        st.markdown("- ✅ Nombre actualizado en X.")
    if res.get("handle"):
        st.markdown("- ✅ @ actualizado en X.")
    if res.get("error"):
        with st.expander("🔍 Detalle del error"):
            st.markdown(str(res.get("error")))


def _aplicar_propuestas_en_lote(usuarios: list):
    """Aplica propuestas secuencialmente con barra de progreso y resumen."""
    from cuentas.generador_identidades import aplicar_propuesta

    progreso = st.progress(0.0)
    estado = st.empty()
    ok = 0
    fallidas = []
    total = len(usuarios)

    for i, usuario in enumerate(usuarios, start=1):
        estado.write(f"⏳ Aplicando en @{usuario} ({i}/{total})...")
        try:
            res = aplicar_propuesta(usuario)
        except Exception as e:
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            ok += 1
        else:
            fallidas.append((usuario, res.get("error") or "sin detalle"))
        progreso.progress(i / total)

    _listar_cuentas.clear()
    estado.write("✅ Lote terminado")
    if ok:
        st.success(f"Propuestas aplicadas: {ok}/{total} cuenta(s).")
    if fallidas:
        st.error(f"Fallidas: {len(fallidas)}/{total} cuenta(s).")
        with st.expander("🔍 Detalle de fallidas", expanded=True):
            for usuario, error in fallidas:
                st.markdown(f"- `@{usuario}`: {error}")


def _nombre_archivo_seguro(nombre) -> str:
    """Nombre de archivo sin caracteres problemáticos (para la subida manual)."""
    limpio = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(nombre or "").strip())
    return limpio.strip("._") or "cuenta"


def _tab_estado():
    st.markdown("### ⏸️ Estado de las cuentas (desactivar / reactivar)")
    st.caption(
        "Las cuentas inactivas quedan excluidas de publicaciones, campañas y "
        "tareas. Desactivar NO borra cookies ni datos: la cuenta se puede "
        "reactivar en cualquier momento."
    )

    todas = _listar_cuentas(OPCION_TODAS)
    if not todas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    activas = sum(1 for f in todas if f.get("activa"))
    inactivas = len(todas) - activas
    c1, c2, c3 = st.columns(3)
    c1.metric("Total", len(todas))
    c2.metric("Activas", activas)
    c3.metric("Inactivas", inactivas)

    st.markdown("---")
    seleccion = _selector_masivo(todas, "est_selector")
    descripcion_sel = st.session_state.get("est_selector_descripcion", "")
    if descripcion_sel:
        st.caption(f"Se aplicará el cambio a: {descripcion_sel}")

    def _aplicar_estado(nuevo_estado: bool, etiqueta: str, accion: str):
        if not seleccion:
            st.warning("Selecciona al menos una cuenta.")
            return
        usuarios = [f["usuario"] for f in seleccion]
        try:
            cambiadas = _cambiar_estado(usuarios, nuevo_estado)
            _flash(f"Cuentas {etiqueta}: {cambiadas}. {descripcion_sel}")
            st.rerun()
        except Exception as e:
            st.error(f"No se pudieron {accion} las cuentas: {e}")

    col_des, col_act = st.columns(2)
    with col_des:
        if st.button(
            "⏸️ Desactivar seleccionadas",
            type="primary",
            use_container_width=True,
            key="btn_est_desactivar",
        ):
            _aplicar_estado(False, "desactivadas", "desactivar")
    with col_act:
        if st.button(
            "▶️ Activar seleccionadas",
            use_container_width=True,
            key="btn_est_activar",
        ):
            _aplicar_estado(True, "activadas", "activar")

    st.markdown("---")
    st.markdown("#### 📋 Estado actual")
    tabla = [
        {
            "usuario": f["usuario"],
            "activa": "Sí" if f.get("activa") else "No",
            "status": f["status"],
            "sección": f["seccion_etiqueta"],
            "tipo": f["tipo_etiqueta"],
            "avatar": "Sí" if f.get("avatar") else "No",
            "portada": "Sí" if f.get("banner") else "No",
        }
        for f in todas
    ]
    st.dataframe(tabla, use_container_width=True)


def _tab_fotos():
    st.markdown("### 📷 Fotos de perfil y portada (IA)")
    st.caption(
        "Genera avatares (1024x1024) y portadas (1536x1024) con la API de "
        "OpenAI a partir del nombre, el registro y la personalidad de cada "
        "cuenta. Las imágenes se guardan en `data/avatares/` y `data/portadas/`, "
        "y su ruta queda registrada en la BD. Aplicarlas en X requiere Chrome."
    )

    todas = _listar_cuentas(OPCION_TODAS)
    if not todas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    con_avatar = sum(1 for f in todas if f.get("avatar"))
    con_portada = sum(1 for f in todas if f.get("banner"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Con avatar", con_avatar)
    c2.metric("Sin avatar", len(todas) - con_avatar)
    c3.metric("Con portada", con_portada)
    c4.metric("Sin portada", len(todas) - con_portada)

    # ---------------- Selección de cuentas ----------------
    st.markdown("---")
    st.markdown("#### 1️⃣ Selecciona las cuentas")
    seleccion = _selector_masivo(todas, "fot_selector")
    usuarios = [f["usuario"] for f in seleccion]
    descripcion_sel = st.session_state.get("fot_selector_descripcion", "")
    if descripcion_sel:
        st.caption(f"Cuentas objetivo: **{len(usuarios)}** — {descripcion_sel}")
    else:
        st.caption(f"Cuentas objetivo: **{len(usuarios)}**")

    col_portada, col_forzar = st.columns(2)
    with col_portada:
        con_portada_chk = st.checkbox(
            "Incluir foto de portada (opcional)",
            value=False,
            key="fot_con_portada",
            help="Genera además un banner horizontal (1536x1024) por cuenta.",
        )
    with col_forzar:
        forzar = st.checkbox(
            "Regenerar aunque ya exista",
            value=False,
            key="fot_forzar",
            help="Vuelve a generar y reemplaza la foto registrada.",
        )

    st.info(
        "La generación usa la API de OpenAI y tarda ~1 imagen por cuenta "
        f"(seleccionadas: {len(usuarios)}"
        + ("; con portada, ~2 por cuenta)." if con_portada_chk else ").")
    )

    # ---------------- Generación con IA ----------------
    st.markdown("#### 2️⃣ Generar con IA")
    if st.button(
        "🎨 Generar fotos con IA",
        type="primary",
        use_container_width=True,
        key="btn_fot_generar",
    ):
        if not usuarios:
            st.warning("Selecciona al menos una cuenta.")
        else:
            barra = st.progress(0.0)
            estado = st.empty()

            def _cb_generar(actual, total, usuario, tipo, ok):
                pasos = 2 if con_portada_chk else 1
                hechos = (actual - 1) * pasos + (2 if tipo == "portada" else 1)
                barra.progress(min(1.0, (hechos / (total * pasos)) if total else 1.0))
                icono = "✅" if ok else "❌"
                estado.write(f"{icono} {tipo} de @{usuario} ({actual}/{total})")

            try:
                from cuentas.fotos import generar_fotos

                with st.spinner("Generando imágenes con IA..."):
                    res = generar_fotos(
                        usuarios,
                        con_portada=con_portada_chk,
                        forzar=forzar,
                        dry_run=False,
                        callback=_cb_generar,
                    )
                _listar_cuentas.clear()
                estado.write("✅ Generación terminada")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Avatares generados", res.get("avatares_ok", 0))
                m2.metric("Portadas generadas", res.get("portadas_ok", 0))
                m3.metric("Omitidas", res.get("omitidas", 0))
                m4.metric("Fallidas", res.get("fallidas", 0))
                if res.get("errores"):
                    with st.expander(
                        f"⚠️ Errores ({len(res['errores'])})", expanded=True
                    ):
                        for err in list(res["errores"])[:50]:
                            st.markdown(f"- {err}")
            except Exception as e:
                st.error(f"Falló la generación de fotos: {e}")

    if st.button(
        "🧪 Simular (dry-run)",
        use_container_width=True,
        key="btn_fot_dry",
        help=(
            "Muestra qué fotos generaría/omitiría sin llamar a la IA ni "
            "escribir en la BD."
        ),
    ):
        if not usuarios:
            st.warning("Selecciona al menos una cuenta.")
        else:
            try:
                from cuentas.fotos import generar_fotos

                res = generar_fotos(
                    usuarios,
                    con_portada=con_portada_chk,
                    forzar=forzar,
                    dry_run=True,
                )
                detalle = res.get("detalle") or []
                generaria = [d for d in detalle if d.get("accion") == "generaria"]
                omitiria = [d for d in detalle if d.get("accion") == "omitiria"]
                errores = [d for d in detalle if d.get("accion") == "error"]
                m1, m2, m3 = st.columns(3)
                m1.metric("Se generarían", len(generaria))
                m2.metric("Se omitirían (ya existen)", len(omitiria))
                m3.metric("Errores", len(errores))
                if detalle:
                    with st.expander("👁️ Ver plan simulado", expanded=True):
                        st.dataframe(
                            [
                                {
                                    "usuario": d.get("usuario", ""),
                                    "tipo": d.get("tipo", ""),
                                    "acción": d.get("accion", ""),
                                    "ruta": d.get("ruta", ""),
                                    "error": d.get("error", ""),
                                }
                                for d in detalle[:100]
                            ],
                            use_container_width=True,
                        )
            except Exception as e:
                st.error(f"Falló la simulación: {e}")

    # ---------------- Previsualización ----------------
    st.markdown("---")
    st.markdown("#### 3️⃣ Previsualización de las seleccionadas")

    def _mostrar_preview(filas_preview, campo_ruta, titulo):
        filas_con = [f for f in filas_preview if f.get(campo_ruta)][:5]
        if not filas_con:
            return
        st.markdown(f"**{titulo}**")
        columnas = st.columns(min(5, len(filas_con)))
        for col, fila in zip(columnas, filas_con):
            with col:
                st.caption(f"@{fila['usuario']}")
                ruta = str(fila.get(campo_ruta) or "")
                if ruta and os.path.exists(ruta):
                    st.image(ruta, use_container_width=True)
                else:
                    st.warning("(falta archivo)")

    if not seleccion:
        st.caption("No hay cuentas seleccionadas para previsualizar.")
    else:
        _mostrar_preview(seleccion, "avatar_path", "Avatares (máx. 5)")
        _mostrar_preview(seleccion, "banner_path", "Portadas (máx. 5)")
        if not any(f.get("avatar_path") for f in seleccion) and not any(
            f.get("banner_path") for f in seleccion
        ):
            st.caption(
                "Ninguna de las cuentas seleccionadas tiene foto registrada "
                "todavía. Genérala arriba."
            )

    # ---------------- Aplicación en X ----------------
    st.markdown("---")
    st.markdown("#### 4️⃣ Aplicar en X (Selenium)")
    st.warning(
        "Requiere Chrome instalado y la sesión de cada cuenta. El cambio de "
        "foto es REAL en X y puede tardar bastante por cuenta."
    )
    confirmado = st.checkbox(
        "Confirmo que quiero aplicar las fotos seleccionadas en X",
        key="fot_confirm_aplicar",
    )
    if st.button(
        "📤 Aplicar fotos en X (Chrome, lento)",
        type="primary",
        use_container_width=True,
        key="btn_fot_aplicar",
    ):
        if not usuarios:
            st.warning("Selecciona al menos una cuenta.")
        elif not confirmado:
            st.warning("Marca la casilla de confirmación para continuar.")
        else:
            barra = st.progress(0.0)
            estado = st.empty()

            def _cb_aplicar(actual, total, usuario, tipo, ok):
                pasos = 2 if con_portada_chk else 1
                hechos = (actual - 1) * pasos + (2 if tipo == "portada" else 1)
                barra.progress(min(1.0, (hechos / (total * pasos)) if total else 1.0))
                icono = "✅" if ok else "❌"
                estado.write(f"{icono} {tipo} de @{usuario} ({actual}/{total})")

            try:
                from cuentas.fotos import aplicar_fotos

                res = aplicar_fotos(
                    usuarios, con_portada=con_portada_chk, callback=_cb_aplicar
                )
                _listar_cuentas.clear()
                estado.write("✅ Aplicación terminada")
                m1, m2, m3 = st.columns(3)
                m1.metric("Avatares aplicados", res.get("avatares_ok", 0))
                m2.metric("Portadas aplicadas", res.get("portadas_ok", 0))
                m3.metric("Fallidas", res.get("fallidas", 0))
                fallidas_det = [
                    d for d in (res.get("detalle") or []) if not d.get("ok")
                ]
                if fallidas_det:
                    with st.expander(
                        f"⚠️ Detalle de fallidas ({len(fallidas_det)})",
                        expanded=True,
                    ):
                        for d in fallidas_det[:50]:
                            st.markdown(
                                f"- `@{d.get('usuario')}` ({d.get('tipo')}): "
                                f"{d.get('error') or 'sin detalle'}"
                            )
            except Exception as e:
                st.error(f"Falló la aplicación de fotos: {e}")

    # ---------------- Subida manual ----------------
    st.markdown("---")
    with st.expander("📁 Subida manual (tu propio archivo, opcional)"):
        st.caption(
            "Reemplaza el avatar o la portada de UNA cuenta con tu archivo. Se "
            "guarda como `data/avatares/{usuario}.png` / "
            "`data/portadas/{usuario}.png` y queda registrado en la BD."
        )
        etiquetas_manual = {_etiqueta_cuenta(f): f for f in todas}
        etiqueta_manual = st.selectbox(
            "Cuenta", list(etiquetas_manual.keys()), key="fot_manual_cuenta"
        )
        fila_manual = etiquetas_manual[etiqueta_manual]
        usuario_manual = fila_manual["usuario"]
        col_av, col_po = st.columns(2)
        with col_av:
            avatar_file = st.file_uploader(
                "Avatar (se guarda como .png)",
                type=["png", "jpg", "jpeg", "webp"],
                key="fot_manual_avatar",
            )
        with col_po:
            portada_file = st.file_uploader(
                "Portada (se guarda como .png)",
                type=["png", "jpg", "jpeg", "webp"],
                key="fot_manual_portada",
            )
        if st.button(
            "💾 Guardar archivos en la BD",
            use_container_width=True,
            key="btn_fot_manual_guardar",
        ):
            if avatar_file is None and portada_file is None:
                st.warning("Sube al menos un archivo (avatar o portada).")
            else:
                from core.config import resolver_ruta

                try:
                    guardados = []
                    if avatar_file is not None:
                        destino = resolver_ruta(
                            f"data/avatares/{_nombre_archivo_seguro(usuario_manual)}.png"
                        )
                        os.makedirs(os.path.dirname(destino), exist_ok=True)
                        with open(destino, "wb") as fh:
                            fh.write(avatar_file.getvalue())
                        guardados.append(("avatar_path", destino))
                    if portada_file is not None:
                        destino = resolver_ruta(
                            f"data/portadas/{_nombre_archivo_seguro(usuario_manual)}.png"
                        )
                        os.makedirs(os.path.dirname(destino), exist_ok=True)
                        with open(destino, "wb") as fh:
                            fh.write(portada_file.getvalue())
                        guardados.append(("banner_path", destino))

                    existe = False
                    with get_db_session() as db:
                        cuenta = (
                            db.query(Cuenta)
                            .filter(Cuenta.usuario == usuario_manual)
                            .first()
                        )
                        if cuenta is not None:
                            existe = True
                            for campo, ruta in guardados:
                                setattr(cuenta, campo, ruta)

                    if not existe:
                        st.error(f"No se encontró @{usuario_manual} en la BD.")
                    else:
                        # Fuera del `with`: st.rerun lanza una excepción que
                        # haría rollback si ocurriera dentro de la sesión.
                        _listar_cuentas.clear()
                        _flash(
                            f"Archivos guardados para @{usuario_manual}: "
                            f"{len(guardados)}."
                        )
                        st.rerun()
                except Exception as e:
                    st.error(f"No se pudieron guardar los archivos: {e}")


def _tab_sincronizar():
    st.markdown("### 🔄 Sincronizar nombre y @ reales desde X")
    st.caption(
        "Sincronización rápida vía httpx con las cookies guardadas; no requiere "
        "Chrome. Usa el proxy de cada cuenta. No modifica la clave interna `usuario`."
    )

    alcance = st.selectbox(
        "Alcance",
        [
            "Todas",
            "Por status",
            "Por sección",
            "Seleccionadas",
            "Selección personalizada (rango/cantidad)",
        ],
        key="sync_alcance",
    )

    solo_status = None
    seccion_sel = None
    seleccion_labels = []
    opciones = {}
    usuarios_personalizada = []
    descripcion_personalizada = ""

    if alcance == "Por status":
        solo_status = st.selectbox("Status a sincronizar", ESTADOS, key="sync_status")
    elif alcance == "Por sección":
        seccion_sel = st.selectbox(
            "Sección a sincronizar",
            _opciones_filtro_seccion(incluir_todas=False),
            key="sync_seccion",
        )
    elif alcance == "Seleccionadas":
        cuentas = _listar_cuentas(OPCION_TODAS)
        opciones = {_etiqueta_cuenta(f): f["usuario"] for f in cuentas}
        seleccion_labels = st.multiselect(
            "Cuentas a sincronizar", list(opciones.keys()), key="sync_cuentas"
        )
    elif alcance == "Selección personalizada (rango/cantidad)":
        cuentas = _listar_cuentas(OPCION_TODAS)
        usuarios_personalizada = [
            f["usuario"] for f in _selector_masivo(cuentas, "sync_selector")
        ]
        descripcion_personalizada = st.session_state.get(
            "sync_selector_descripcion", ""
        )
        if descripcion_personalizada:
            st.caption(f"Se sincronizará con: {descripcion_personalizada}")

    if st.button(
        "🔄 Sincronizar ahora",
        type="primary",
        use_container_width=True,
        key="btn_sync",
    ):
        if alcance == "Seleccionadas" and not seleccion_labels:
            st.warning("Selecciona al menos una cuenta para sincronizar.")
        elif (
            alcance == "Selección personalizada (rango/cantidad)"
            and not usuarios_personalizada
        ):
            st.warning("No hay cuentas seleccionadas en el rango/cantidad.")
        else:
            barra = st.progress(0.0)
            estado = st.empty()

            def _cb(actual, total, usuario, resultado):
                barra.progress(min(1.0, (actual / total) if total else 1.0))
                handle = (resultado.get("handle") or "").strip()
                nombre = (resultado.get("nombre") or "").strip()
                detalle = f"@{handle}" if handle else "(sin handle)"
                if nombre:
                    detalle += f" ({nombre})"
                estado.write(f"🔄 @{usuario} → {detalle}")

            try:
                from plataformas.twitter.perfil import sincronizar_todas

                if alcance == "Todas":
                    res = sincronizar_todas(callback=_cb)
                elif alcance == "Por status":
                    res = sincronizar_todas(solo_status=solo_status, callback=_cb)
                elif alcance == "Por sección":
                    if seccion_sel == OPCION_SIN_ASIGNAR:
                        pendientes = [
                            f["usuario"] for f in _listar_cuentas(OPCION_TODAS, "")
                        ]
                        res = _sincronizar_usuarios(pendientes, _cb)
                    else:
                        res = sincronizar_todas(
                            seccion=normalizar_seccion(seccion_sel), callback=_cb
                        )
                elif alcance == "Selección personalizada (rango/cantidad)":
                    res = _sincronizar_usuarios(usuarios_personalizada, _cb)
                else:
                    usuarios = [
                        opciones[label]
                        for label in seleccion_labels
                        if label in opciones
                    ]
                    res = _sincronizar_usuarios(usuarios, _cb)

                _listar_cuentas.clear()
                estado.write("✅ Sincronización terminada")
                if (
                    alcance == "Selección personalizada (rango/cantidad)"
                    and descripcion_personalizada
                ):
                    st.caption(f"Resumen de: {descripcion_personalizada}")
                elif alcance == "Seleccionadas":
                    st.caption(
                        f"Resumen de: Modo: Manual — {len(seleccion_labels)} cuentas"
                    )
                _mostrar_resumen_sincronizacion(res)
            except Exception as e:
                estado.write("")
                st.error(f"Falló la sincronización: {e}")


def _tab_cambiar_perfil():
    st.markdown("### ✏️ Cambiar nombre / @ (Selenium)")
    st.warning(
        "Requiere Chrome instalado; el cambio es real e irreversible en X "
        "(X puede pedir verificación adicional)."
    )

    cuentas = _listar_cuentas(OPCION_TODAS)
    if not cuentas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    opciones = {_etiqueta_cuenta_perfil(f): f for f in cuentas}
    etiqueta = st.selectbox("Cuenta", list(opciones.keys()), key="perfil_cuenta")
    fila = opciones[etiqueta]
    usuario = fila["usuario"]

    nombre_default = fila["nombre_mostrado"] or ""
    handle_default = (fila["handle_actual"] or usuario or "").lstrip("@")
    password_default = fila.get("password") or ""

    col_nombre, col_handle = st.columns(2)
    with col_nombre:
        nuevo_nombre = st.text_input(
            "Nuevo nombre",
            value=nombre_default,
            key=f"perfil_nombre_{usuario}",
        )
    with col_handle:
        nuevo_handle = st.text_input(
            "Nuevo @ (sin @)",
            value=handle_default,
            key=f"perfil_handle_{usuario}",
        )

    password = st.text_input(
        "Contraseña de la cuenta",
        value=password_default,
        type="password",
        key=f"perfil_pass_{usuario}",
    )
    st.caption(
        "Deja un campo vacío para no cambiarlo. X exige la contraseña para "
        "cambiar el @."
    )

    handle_guardado = (fila["handle_actual"] or "").lstrip("@")
    if handle_guardado and (nuevo_handle or "").strip().lstrip("@").lower() == handle_guardado.lower():
        st.caption(
            "ℹ️ El @ ingresado es igual al actual guardado; si no quieres "
            "cambiarlo, deja el campo vacío."
        )

    confirmado = st.checkbox(
        "Confirmo que quiero modificar el perfil REAL de esta cuenta",
        key=f"perfil_confirm_{usuario}",
    )

    if st.button(
        "✏️ Aplicar cambios en X",
        type="primary",
        use_container_width=True,
        key=f"perfil_btn_{usuario}",
    ):
        if not confirmado:
            st.warning("Marca la casilla de confirmación para continuar.")
            return

        nombre_env = (nuevo_nombre or "").strip()
        handle_env = (nuevo_handle or "").strip().lstrip("@").strip()
        pass_env = password or ""

        if not nombre_env and not handle_env:
            st.warning("No hay cambios que aplicar: nombre y @ están vacíos.")
            return

        bot = None
        res = None
        try:
            from plataformas.twitter.selenium_bot import TwitterBot

            with st.spinner(f"Abriendo Chrome y aplicando cambios en @{usuario}..."):
                bot = TwitterBot(usuario)
                res = bot.cambiar_perfil(
                    nombre=nombre_env or None,
                    handle=handle_env or None,
                    password=pass_env,
                )
        except Exception as e:
            st.error(f"Falló la ejecución de Selenium: {e}")
        finally:
            if bot is not None:
                try:
                    bot.cerrar()
                except Exception:
                    pass

        if res is None:
            return

        _listar_cuentas.clear()

        if res.get("ok"):
            st.success("Proceso terminado: se aplicaron los cambios que tuvieron éxito.")
        else:
            st.error("No se pudo aplicar ningún cambio en X.")

        if nombre_env:
            if res.get("nombre"):
                st.markdown("- ✅ **Nombre** actualizado en X.")
            else:
                st.markdown("- ❌ No se pudo cambiar el **nombre**.")
        if handle_env:
            if res.get("handle"):
                st.markdown("- ✅ **@** actualizado en X.")
                st.info(
                    f"El @ interno (`usuario`) NO cambió por diseño: la clave "
                    f"interna de login sigue siendo `{usuario}`."
                )
            else:
                st.markdown("- ❌ No se pudo cambiar el **@**.")

        if res.get("error"):
            st.warning(f"Detalle del error: {res['error']}")

        with st.expander("🔍 Detalle técnico"):
            st.json(res)
            ultimo_error = getattr(bot, "ultimo_error", "") if bot is not None else ""
            if ultimo_error:
                st.markdown(f"`bot.ultimo_error`: {ultimo_error}")


def _tab_inventario():
    st.markdown("### 📋 Inventario de cuentas (Twitter)")
    st.caption(
        "El grupo operativo es libre: las cuentas sin grupo muestran "
        "'sin grupo' (ya no se fuerza 'A'). El inventario se agrupa por "
        "registro de lenguaje con sus conteos."
    )

    col_status, col_seccion, col_tipo, col_estado = st.columns(4)
    with col_status:
        filtro_status = st.selectbox(
            "Filtrar por status",
            [OPCION_TODAS] + ESTADOS,
            key="inv_status",
        )
    with col_seccion:
        filtro_seccion = st.selectbox(
            "Filtrar por sección",
            _opciones_filtro_seccion(),
            key="inv_seccion",
        )
    with col_tipo:
        filtro_tipo = st.selectbox(
            "Filtrar por registro",
            [OPCION_TODAS] + list(TIPOS_CUENTA.values()) + [OPCION_SIN_DEFINIR],
            key="inv_tipo",
        )
    with col_estado:
        filtro_estado = st.selectbox(
            "Filtrar por estado",
            [OPCION_TODAS, "Activas", "Inactivas"],
            key="inv_estado",
        )

    filas_base = _listar_cuentas(filtro_status, _seccion_desde_filtro(filtro_seccion))
    if filtro_estado == "Activas":
        filas_base = [f for f in filas_base if f.get("activa")]
    elif filtro_estado == "Inactivas":
        filas_base = [f for f in filas_base if not f.get("activa")]

    # Conteos por registro sobre el conjunto ya filtrado por status/sección/
    # estado (sin aplicar aún el filtro de registro, para ver el reparto).
    conteos = {clave: 0 for clave in TIPOS_CUENTA}
    conteos[""] = 0
    for f in filas_base:
        clave = normalizar_tipo_cuenta(f.get("tipo_cuenta") or "")
        conteos[clave] = conteos.get(clave, 0) + 1

    columnas = st.columns(len(TIPOS_CUENTA) + 1)
    for col, clave in zip(columnas, TIPOS_CUENTA):
        col.metric(TIPOS_CUENTA[clave], conteos.get(clave, 0))
    columnas[-1].metric(OPCION_SIN_DEFINIR, conteos.get("", 0))
    st.caption(f"Total (sin filtro de registro): {len(filas_base)} cuenta(s).")

    filas = [f for f in filas_base if _coincide_tipo(f, filtro_tipo)]

    if not filas:
        st.info("No hay cuentas para mostrar con esos filtros.")
        return

    st.caption(
        f"Mostrando {len(filas)} cuenta(s) con el filtro de registro actual."
    )

    # Separadores visuales por registro: politica / activista / ciudadana +
    # sin definir, cada uno con su conteo y su subtabla (incluye el grupo
    # operativo como 'sin grupo' cuando viene vacio, sin forzar 'A').
    for clave in list(TIPOS_CUENTA.keys()) + [""]:
        grupo_filas = [
            f for f in filas if (f.get("tipo_cuenta") or "") == clave
        ]
        if not grupo_filas:
            continue
        etiqueta = TIPOS_CUENTA.get(clave) if clave else OPCION_SIN_DEFINIR
        st.markdown(f"#### {etiqueta} ({len(grupo_filas)})")
        tabla = [
            {
                "usuario": f["usuario"],
                "handle_actual": f["handle_actual"],
                "nombre_mostrado": f["nombre_mostrado"],
                "seccion": f["seccion_etiqueta"],
                "tipo": f["tipo_etiqueta"],
                "grupo": f.get("grupo_etiqueta") or "sin grupo",
                "nombre_propuesto": f["nombre_propuesto"],
                "handle_propuesto": f["handle_propuesto"],
                "activa": "Sí" if f.get("activa") else "No",
                "avatar": "Sí" if f.get("avatar") else "No",
                "portada": "Sí" if f.get("banner") else "No",
                "status": f["status"],
                "email": f["email"],
                "last_checked": f["last_checked"],
                "cookies": f["cookies"],
            }
            for f in grupo_filas
        ]
        st.dataframe(tabla, use_container_width=True)


def _tab_exportar():
    st.markdown("### 📤 Exportar cuentas (login manual)")
    solo_activas = st.checkbox("Exportar solo cuentas activas", key="exp_solo_activas")

    from core.exportar import (
        exportar_cuentas_excel,
        exportar_cuentas_csv,
        INSTRUCCIONES_LOGIN,
    )

    col_xlsx, col_csv = st.columns(2)
    with col_xlsx:
        if st.button(
            "⬇️ Descargar Excel (.xlsx)",
            key="btn_exp_xlsx",
            use_container_width=True,
        ):
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
        if st.button(
            "⬇️ Descargar CSV",
            key="btn_exp_csv",
            use_container_width=True,
        ):
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


def _restaurar_pestana_desde_url():
    """Restaura la pestaña interna desde `?tab=` en recargas del navegador.

    Solo actúa cuando `cuentas_pestana_selector` aún no existe en
    session_state (sesión nueva): en un st.rerun() normal el radio conserva
    su valor solo y no se toca nada. Los valores inválidos se ignoran."""
    if "cuentas_pestana_selector" in st.session_state:
        return
    try:
        tab = st.query_params.get("tab")
    except Exception:
        return
    if isinstance(tab, (list, tuple)):
        tab = tab[0] if tab else None
    if tab and str(tab) in TABS:
        st.session_state["cuentas_pestana_selector"] = str(tab)


def _on_cambio_pestana():
    """Persiste la pestaña interna en `?tab=` al cambiar de sección."""
    try:
        st.query_params["tab"] = st.session_state.get(
            "cuentas_pestana_selector", TABS[0]
        )
    except Exception:
        pass


def render(usuario):
    cabecera(
        "🗂️ CUENTAS",
        "Perfiles, secciones CI/CD/IP, registro de lenguaje, nombres y validación",
    )

    flash = st.session_state.pop("cuentas_flash", "")
    if flash:
        st.success(flash)

    paginas = {
        "📥 Importar": _tab_importar,
        "🔎 Validar": _tab_validar,
        "🗂️ Secciones CI/CD/IP": _tab_secciones,
        "🎭 Registro": _tab_registro,
        "🎨 Perfiles": _tab_perfiles,
        "🏷️ Nombres": _tab_nombres,
        "⏸️ Estado": _tab_estado,
        "📷 Fotos": _tab_fotos,
        "🔄 Sincronizar desde X": _tab_sincronizar,
        "✏️ Cambiar nombre/@": _tab_cambiar_perfil,
        "📋 Inventario": _tab_inventario,
        "📤 Exportar": _tab_exportar,
    }

    # Radio horizontal en vez de st.tabs: Streamlit ejecuta TODAS las pestanas
    # de st.tabs en cada rerun (11 consultas a BD por interaccion). Con radio
    # solo se renderiza la seleccionada (~1/N consultas).
    # La pestaña persiste en ?tab= (ver _restaurar_pestana_desde_url): un
    # st.rerun() tras cambiar registro/tipo/sección conserva el radio vía
    # session_state, y una recarga del navegador lo restaura desde la URL.
    _restaurar_pestana_desde_url()
    seleccion = st.radio(
        "📍 SECCIÓN:",
        TABS,
        key="cuentas_pestana_selector",
        horizontal=True,
        on_change=_on_cambio_pestana,
    )
    # Re-sincroniza ?tab= en cada run (cubre cambios que no pasaron por el
    # callback). Escribir query params no dispara rerun: no hay bucle.
    try:
        tab_actual = st.query_params.get("tab")
        if isinstance(tab_actual, (list, tuple)):
            tab_actual = tab_actual[0] if tab_actual else None
        if tab_actual != seleccion:
            st.query_params["tab"] = seleccion
    except Exception:
        pass

    funcion = paginas.get(seleccion)
    if funcion:
        funcion()
