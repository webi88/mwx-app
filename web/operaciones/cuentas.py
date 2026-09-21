"""Página de CUENTAS: carga masiva de credenciales, validación de sesión
(sin navegador, vía httpx), secciones (IP/CI/Libertad/Justicia), sincronización del perfil real
desde X (httpx, sin Chrome), cambio de nombre/@ (Selenium + Chrome) e
inventario/exportación.

Reemplaza el flujo obsoleto de auto-registro con Grizzly SMS por la nueva
arquitectura: importación en lote (8 campos, con `user_agent` opcional) +
validador de sesión + inventario, incluyendo anti-detección (User-Agent por
cuenta y cookies completas).
"""
import bisect
import json
import os
import re
import threading

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
    "🗂️ Secciones (IP/CI/Libertad/Justicia)",
    "🎭 Registro",
    "🎨 Perfiles",
    "🏷️ Nombres",
    "⏸️ Estado",
    "📷 Fotos",
    "🛡️ Anti-detección",
    "🔄 Sincronizar desde X",
    "🏷️ Renombrar usuario",
    "✏️ Cambiar nombre/@",
    "🧾 Perfil completo",
    "📋 Inventario",
    "📤 Exportar",
]

# Agrupación de las 15 pestañas en 3 MODOS (radio superior). Reduce el ruido
# visual sin quitar ninguna pestaña: cada una aparece en UN solo modo y `TABS`
# (exportado, lo usa web/app.py para validar ?tab=) se conserva intacto.
MODOS_TABS = {
    "🗂️ Cuentas": [
        "📥 Importar",
        "🔎 Validar",
        "⏸️ Estado",
        "📋 Inventario",
        "📤 Exportar",
    ],
    "🎭 Identidad": [
        "🗂️ Secciones (IP/CI/Libertad/Justicia)",
        "🎭 Registro",
        "🎨 Perfiles",
        "🏷️ Nombres",
        "📷 Fotos",
    ],
    "🛡️ Avanzado": [
        "🛡️ Anti-detección",
        "🔄 Sincronizar desde X",
        "🏷️ Renombrar usuario",
        "✏️ Cambiar nombre/@",
        "🧾 Perfil completo",
    ],
}


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


def _abreviar(texto, maximo: int = 40) -> str:
    """Trunca visualmente un texto largo; el dato completo se conserva aparte."""
    texto = str(texto or "").strip()
    if len(texto) <= maximo:
        return texto
    return texto[: maximo - 1] + "…"


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
    "CI"/"IP"/"LIB"/"JUS" para una sección concreta."""
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
    `seccion_filtro`: None = todas; "" = sin asignar; "CI"/"IP"/"LIB"/"JUS" = concreta.
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
                        "user_agent": getattr(c, "user_agent", "") or "",
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


def _eliminar_cuentas(usuarios) -> int:
    """DELETE definitivo de cuentas (cookies, historial y configuración se
    pierden). A diferencia de `_cambiar_estado`, esto NO es reversible;
    pensado para cuentas ya confirmadas como baneadas/suspendidas por X."""
    usuarios = [u for u in (usuarios or []) if u]
    if not usuarios:
        return 0
    with get_db_session() as db:
        eliminadas = (
            db.query(Cuenta)
            .filter(Cuenta.plataforma == "twitter", Cuenta.usuario.in_(usuarios))
            .delete(synchronize_session=False)
        )
    _listar_cuentas.clear()
    return eliminadas


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
        "Una cuenta por línea, campos separados por `:`; los dos últimos son "
        "**opcionales**:\n"
        "`username:password:totp_secret:email:email_password:auth_token:cookies:user_agent`\n"
        "El 7º campo (`cookies`) acepta **JSON crudo** (`[...]` o "
        "`{\"cookies\":[...]}`) o **base64**, y el 8º (`user_agent`) es el UA "
        "original del lote. También se acepta el campo `ua=`/`user_agent=` en "
        "cualquier posición de la línea.\n"
        "Las cookies se guardan **COMPLETAS** (`auth_token`, `ct0`, `twid`…), "
        "no solo el `auth_token`: son las que se inyectan al abrir Chrome. Sin "
        "cookies la cuenta igual se importa y el validador obtiene el `ct0` con "
        "el `auth_token`."
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
    st.markdown("### 🗂️ Secciones (IP/CI/Libertad/Justicia)")
    st.caption(
        "Clasifica cada cuenta de Twitter en "
        + ", ".join(f"**{etiqueta_seccion(clave)}**" for clave in SECCIONES)
        + "."
    )

    todas = _listar_cuentas(OPCION_TODAS)

    conteos = {clave: 0 for clave in SECCIONES}
    conteos[""] = 0
    for fila in todas:
        conteos[fila["seccion"] or ""] = conteos.get(fila["seccion"] or "", 0) + 1

    # Una métrica por cada sección de `core.secciones.SECCIONES` + "Sin asignar"
    # (generado dinámicamente: si mañana cambian las secciones, la UI se adapta).
    columnas = st.columns(len(SECCIONES) + 1)
    for col, clave in zip(columnas, SECCIONES):
        col.metric(etiqueta_seccion(clave), conteos.get(clave, 0))
    columnas[-1].metric(OPCION_SIN_ASIGNAR, conteos.get("", 0))

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


def _ia_nombres_disponible():
    """True/False si el backend expone `ia_disponible`; None si aún no existe.

    Import perezoso y tolerante: `cuentas/generador_identidades.py` se
    desarrolla en paralelo, así que durante la transición la función puede no
    existir o cambiar de firma. En ese caso se devuelve None (caption neutro)."""
    try:
        from cuentas.generador_identidades import ia_disponible
    except ImportError:
        return None
    except Exception:
        return None
    try:
        return bool(ia_disponible())
    except TypeError:
        return None
    except Exception:
        return None


def _soporta_kwargs(func, nombres) -> bool:
    """True si `func` acepta todos los kwargs de `nombres` (o tiene **kwargs)."""
    import inspect

    try:
        parametros = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    var_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in parametros.values()
    )
    return all(nombre in parametros or var_kwargs for nombre in nombres)


def _a_int(valor) -> int:
    """Conteo entero tolerante: acepta número/str o lista (devuelve su largo)."""
    if isinstance(valor, (list, tuple, set)):
        return len(valor)
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def _llamar_asignar_propuestas(func, usuarios, tipo, seccion, contexto=""):
    """Llama `asignar_propuestas` pasando `contexto` solo si el backend lo acepta.

    Fallback para firmas viejas: si el backend aún no tiene el parámetro
    `contexto` se omite, y si lanza `TypeError` al recibirlo se reintenta sin
    él (la UI no se rompe mientras el backend se actualiza en paralelo)."""
    kwargs = {"tipo": tipo, "seccion": seccion, "dry_run": False}
    if contexto and _soporta_kwargs(func, ("contexto",)):
        kwargs["contexto"] = contexto
    try:
        return func(usuarios, **kwargs)
    except TypeError:
        if "contexto" in kwargs:
            kwargs.pop("contexto", None)
            return func(usuarios, **kwargs)
        raise


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
            "Similitudes de partido (naranja, bolillos, amarilloluz)",
            "Mixto: mitad personas + mitad similitudes de partido",
        ],
        key="nom_tipo_identidad",
    )
    tipo_mapa = {
        "Auto (según tipo de cuenta)": "auto",
        "Solo personas reales": "persona",
        "Solo cuentas de movimiento": "movimiento",
        "Similitudes de partido (naranja, bolillos, amarilloluz)": "partido",
        "Mixto: mitad personas + mitad similitudes de partido": "mixto",
    }
    tipo_codigo = tipo_mapa.get(tipo_etiqueta, "auto")
    if tipo_etiqueta.startswith("Auto"):
        st.caption(
            "Auto: política → cuenta de movimiento; ciudadana → persona real; "
            "sin definir → aleatorio 50/50 (no genera similitudes de partido)."
        )
    elif tipo_codigo == "partido":
        st.caption(
            "Similitudes de partido: nombres/handles con **guiños** (colores, "
            "símbolos o apodos como naranja, bolillos, amarilloluz) que NO "
            "nombran a ningún partido."
        )
    elif tipo_codigo == "mixto":
        st.caption(
            "Mixto: ~mitad **personas reales** y ~mitad **similitudes de "
            "partido** (guiños sin nombrar partidos)."
        )

    ia_estado = _ia_nombres_disponible()
    if ia_estado is True:
        st.caption("🧠 IA de nombres: activada (OpenAI)")
    elif ia_estado is False:
        st.caption("🧠 IA de nombres: no configurada; se usará el generador local")
    else:
        st.caption(
            "🧠 IA de nombres: estado desconocido; se usará el generador local "
            "si la IA no está disponible"
        )

    contexto_ia = st.text_area(
        "Contexto para la IA (opcional)",
        key="nom_tipo_contexto",
        height=80,
        placeholder=(
            "Ej. 'oposición al gobierno actual, tono ciudadano' o "
            "'similitudes naranjas del partido en el poder'"
        ),
        help=(
            "Se pasa tal cual al generador de identidades. Solo aplica cuando "
            "la IA de nombres está activa."
        ),
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
            contexto_generar = (contexto_ia or "").strip()
            try:
                from cuentas.generador_identidades import asignar_propuestas

                with st.spinner(
                    f"✨ Generando propuestas para {len(usuarios)} cuenta(s)..."
                ):
                    res = _llamar_asignar_propuestas(
                        asignar_propuestas,
                        usuarios,
                        tipo_codigo,
                        seccion_ctx,
                        contexto_generar,
                    )
                _listar_cuentas.clear()
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total", res.get("total", 0))
                c2.metric("Propuestas", res.get("ok", 0))
                c3.metric("Errores", len(res.get("errores") or []))
                origen_ia = res.get("origen_ia")
                c4.metric(
                    "Origen",
                    "🧠 IA" if origen_ia else ("Local" if origen_ia is not None else "—"),
                )
                personas = _a_int(res.get("persona"))
                partidos = _a_int(res.get("partido"))
                movimientos = _a_int(res.get("movimiento"))
                if personas or partidos or movimientos or origen_ia is not None:
                    st.caption(
                        "Origen: "
                        + ("IA (OpenAI)" if origen_ia else "generador local")
                        + f" · 👤 personas: {personas} · 🎩 partido: {partidos} · "
                        f"🏳️ movimiento: {movimientos}"
                    )
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
    st.markdown("#### 5️⃣ Aplicar en lote (Chrome, en paralelo)")
    st.caption(
        "Aplica las propuestas con varios navegadores a la vez, muestra el "
        "progreso en vivo y se puede cancelar. El cambio de nombre/@ es real "
        "en X."
    )

    if _lote_pendiente():
        st.markdown("**⏳ Aplicación en lote en curso**")
        _monitorear_lote()

    aplicar_todas = st.checkbox(
        "Aplicar a TODAS las cuentas con propuesta pendiente",
        key="nom_lote_todas",
        help=(
            "Ignora el multiselect de abajo y usa las cuentas listadas en el "
            "punto 3️⃣ (las que tienen propuesta pendiente)."
        ),
    )
    if aplicar_todas:
        usuarios_lote = [f["usuario"] for f in con_propuesta]
        st.caption(
            f"✅ Se aplicará a **{len(usuarios_lote)}** cuenta(s) con propuesta "
            "pendiente (el multiselect de abajo se ignora al aplicar)."
        )
    else:
        usuarios_lote = []

    seleccion_lote = st.multiselect(
        "Cuentas con propuesta a aplicar",
        list(opciones_aplicar.keys()),
        key="nom_lote_cuentas",
        help="Para elegir cuentas puntuales al aplicar o para 🗑️ Descartar.",
    )
    if not aplicar_todas:
        usuarios_lote = [
            opciones_aplicar[label]["usuario"]
            for label in seleccion_lote
            if label in opciones_aplicar
        ]

    col_workers, col_renombrar = st.columns([1, 2])
    with col_workers:
        max_workers = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=4,
            value=2,
            step=1,
            key="nom_lote_workers",
            help="2-3 es lo estable con Chrome; 4 solo si hay RAM de sobra.",
        )
    with col_renombrar:
        renombrar_lote = st.checkbox(
            "Renombrar también la clave interna al nuevo @ (recomendado para "
            "no desalinear cookies/tareas)",
            value=True,
            key="nom_lote_renombrar",
            help=(
                "Migra cookies, avatar/portada y registros internos al nuevo "
                "usuario; evita que las tareas queden apuntando a la clave vieja."
            ),
        )

    confirmado_lote = st.checkbox(
        "Confirmo que quiero aplicar las propuestas en X (cambios REALES de "
        "nombre/@; puede tardar)",
        key="nom_lote_confirm",
    )
    if st.button(
        "✏️ Aplicar en lote",
        type="primary",
        use_container_width=True,
        key="btn_nom_lote",
    ):
        if not confirmado_lote:
            st.warning("Marca la casilla de confirmación para continuar.")
        elif not usuarios_lote:
            st.warning(
                "Selecciona al menos una cuenta o marca 'Aplicar a TODAS las "
                "cuentas con propuesta pendiente'."
            )
        else:
            resumen = _aplicar_lote_con_progreso(
                usuarios_lote, int(max_workers or 2), bool(renombrar_lote)
            )
            if resumen is None:
                st.info(
                    "El backend de identidades aún no expone la aplicación en "
                    "paralelo; se aplica una por una con el flujo anterior."
                )
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


def _lote_pendiente() -> bool:
    """True si hay un lote de aplicación en curso o su resumen sin mostrar."""
    return st.session_state.get("nom_lote_hilo") is not None


def _formato_duracion(segundos) -> str:
    """MM:SS (o H:MM:SS si pasa de una hora) para el contador de progreso."""
    try:
        total = max(0, int(segundos))
    except (TypeError, ValueError):
        total = 0
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{segs:02d}"
    return f"{minutos:02d}:{segs:02d}"


def _linea_evento_lote(evento: dict) -> str:
    """Línea del feed para una cuenta terminada: `✅ @usuario — detalle`."""
    if not isinstance(evento, dict):
        return ""
    icono = "✅" if evento.get("ok") else "❌"
    usuario = str(evento.get("usuario") or "?").strip().lstrip("@") or "?"
    linea = f"{icono} @{usuario}"
    if not evento.get("ok"):
        error = _abreviar(evento.get("error") or "", 70)
        if error:
            linea += f" — {error}"
    return linea


def _contar_fallidos(resumen: dict) -> int:
    """Número de cuentas fallidas del resumen (acepta int o lista)."""
    fallidos = resumen.get("fallidos")
    if isinstance(fallidos, (list, tuple, set)):
        return len(fallidos)
    try:
        return int(fallidos or 0)
    except (TypeError, ValueError):
        return 0


def _mostrar_resumen_lote(resumen: dict, cancelado: bool = False):
    """Métricas y fallidas del resumen final de `aplicar_propuestas_en_lote`."""
    total = _a_int(resumen.get("total"))
    ok = _a_int(resumen.get("ok"))
    fallidas = _contar_fallidos(resumen)
    renombrados = _a_int(resumen.get("renombrados"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", total)
    c2.metric("Aplicadas", ok)
    c3.metric("Fallidas", fallidas)
    c4.metric("Renombradas", renombrados)

    if cancelado or resumen.get("cancelado"):
        st.warning(
            f"⏹️ Lote cancelado por el usuario: se aplicaron {ok}/{total} "
            "cuenta(s)."
        )
    elif ok:
        st.success(f"Propuestas aplicadas: {ok}/{total} cuenta(s).")

    detalles = []
    for err in resumen.get("errores") or []:
        texto = err if isinstance(err, str) else str(err)
        if texto:
            detalles.append(texto)
    if not detalles:
        # Backend sin lista de errores: se derivan de `resultados`.
        for fila in resumen.get("resultados") or []:
            if isinstance(fila, dict) and not fila.get("ok"):
                usuario = str(fila.get("usuario") or "?")
                detalles.append(f"`@{usuario}`: {fila.get('error') or 'sin detalle'}")
    for fila in resumen.get("resultados") or []:
        if not isinstance(fila, dict):
            continue
        error_renombrado = str(fila.get("error_renombrado") or "").strip()
        if error_renombrado:
            usuario = str(fila.get("usuario") or "?")
            detalles.append(
                f"`@{usuario}`: no se renombró la clave interna "
                f"({error_renombrado})"
            )
    if detalles:
        with st.expander(f"🔍 Detalle de fallidas ({len(detalles)})", expanded=True):
            for detalle in detalles[:100]:
                st.markdown(f"- {detalle}")


def _monitorear_lote() -> dict:
    """Pinta el progreso del lote activo y devuelve el resumen final.

    Se llama justo tras lanzar el hilo y también en reruns posteriores: el
    clic en "⏹️ Cancelar" interrumpe el script actual y el nuevo run vuelve a
    entrar aquí con el botón en True (el evento vive en `st.session_state`).
    El callback del backend corre en hilos de trabajo y solo escribe en el
    dict compartido `nom_lote_estado` bajo lock; todos los `st.*` se pintan
    aquí, en el hilo principal, cada ~0.5s. Al terminar limpia la caché de
    cuentas (`_listar_cuentas.clear()`)."""
    import time

    hilo = st.session_state.get("nom_lote_hilo")
    if hilo is None:
        return {}
    cancelar = st.session_state.get("nom_lote_cancelar")
    lock = st.session_state.get("nom_lote_lock")
    estado = st.session_state.get("nom_lote_estado") or {}
    resultado = st.session_state.get("nom_lote_resultado") or {}
    inicio = st.session_state.get("nom_lote_inicio") or time.monotonic()

    def _snapshot() -> dict:
        copia = {
            "total": estado.get("total", 0),
            "hechas": estado.get("hechas", 0),
            "ok": estado.get("ok", 0),
            "fallidas": estado.get("fallidas", 0),
            "eventos": list(estado.get("eventos") or []),
        }
        if lock is None:
            return copia
        try:
            with lock:
                return {
                    "total": estado.get("total", 0),
                    "hechas": estado.get("hechas", 0),
                    "ok": estado.get("ok", 0),
                    "fallidas": estado.get("fallidas", 0),
                    "eventos": list(estado.get("eventos") or []),
                }
        except Exception:
            return copia

    barra = st.progress(0.0, text="⏳ Iniciando el lote...")
    metricas = st.empty()
    feed = st.empty()
    col_cancelar, _ = st.columns([1, 3])
    with col_cancelar:
        cancelado_por_boton = st.button("⏹️ Cancelar", key="nom_lote_cancelar_btn")
    if cancelado_por_boton and cancelar is not None:
        cancelar.set()

    while callable(getattr(hilo, "is_alive", None)) and hilo.is_alive():
        snap = _snapshot()
        hechas = _a_int(snap.get("hechas"))
        total = max(1, _a_int(snap.get("total")) or 1)
        transcurrido = _formato_duracion(time.monotonic() - inicio)
        barra.progress(
            min(1.0, hechas / total),
            text=f"⏳ {hechas}/{total} · {transcurrido}",
        )
        metricas.markdown(
            f"**✅ {_a_int(snap.get('ok'))} aplicadas · "
            f"❌ {_a_int(snap.get('fallidas'))} fallidas · "
            f"🧮 {hechas}/{total} · ⏱️ {transcurrido}**"
        )
        lineas = [
            _linea_evento_lote(ev)
            for ev in (snap.get("eventos") or [])[-8:]
            if isinstance(ev, dict)
        ]
        feed.markdown(
            "  \n".join(lineas) if lineas else "⏳ Esperando las primeras cuentas…"
        )
        time.sleep(0.5)

    try:
        hilo.join(timeout=5)
    except Exception:
        pass
    barra.progress(1.0, text="✅ Lote finalizado")

    resumen = dict(resultado.get("resumen") or {})
    error = resultado.get("error")
    snap_final = _snapshot()
    lineas = [
        _linea_evento_lote(ev)
        for ev in (snap_final.get("eventos") or [])[-8:]
        if isinstance(ev, dict)
    ]
    if error is not None:
        lineas.append(f"**❌ Lote interrumpido: {_abreviar(error, 120)}**")
    else:
        lineas.append(
            f"**✅ Lote finalizado: {_a_int(resumen.get('ok'))} aplicadas · "
            f"{_contar_fallidos(resumen)} fallidas**"
        )
    feed.markdown("  \n".join(lineas) if lineas else "✅ Lote finalizado")

    if error is not None:
        st.error(f"El lote se interrumpió: {error}")
    else:
        _mostrar_resumen_lote(resumen, cancelado=bool(cancelado_por_boton))

    _listar_cuentas.clear()
    for clave in (
        "nom_lote_hilo",
        "nom_lote_cancelar",
        "nom_lote_lock",
        "nom_lote_estado",
        "nom_lote_resultado",
        "nom_lote_inicio",
    ):
        st.session_state.pop(clave, None)
    return resumen


def _aplicar_lote_con_progreso(usuarios: list, max_workers: int, renombrar: bool):
    """Aplica propuestas en paralelo con progreso en vivo y cancelación.

    Devuelve el resumen final, o `None` si el backend aún no expone
    `aplicar_propuestas_en_lote` o su firma no acepta los kwargs nuevos; en
    ese caso el llamador cae al bucle secuencial `_aplicar_propuestas_en_lote`.

    El hilo, el evento de cancelación y el estado del progreso viven en
    `st.session_state`: si un clic interrumpe el script, el siguiente run
    retoma el monitoreo sin relanzar el lote."""
    import time

    try:
        from cuentas.generador_identidades import aplicar_propuestas_en_lote
    except ImportError:
        return None
    except Exception:
        return None

    if not callable(aplicar_propuestas_en_lote):
        return None
    if not _soporta_kwargs(
        aplicar_propuestas_en_lote,
        ("max_workers", "renombrar", "callback", "cancelar"),
    ):
        return None

    lock = threading.Lock()
    cancelar = threading.Event()
    estado = {
        "total": len(usuarios),
        "hechas": 0,
        "ok": 0,
        "fallidas": 0,
        "eventos": [],
    }
    resultado: dict = {}

    def _callback(progreso):
        """Callback del backend (corre en hilos): SOLO escribe en `estado`."""
        try:
            datos = progreso if isinstance(progreso, dict) else {}
            with lock:
                total = _a_int(datos.get("total"))
                if total:
                    estado["total"] = total
                if datos.get("hechas") is not None:
                    estado["hechas"] = _a_int(datos.get("hechas"))
                ok = bool(datos.get("ok"))
                if ok:
                    estado["ok"] += 1
                else:
                    estado["fallidas"] += 1
                estado["eventos"].append(
                    {
                        "usuario": str(datos.get("usuario") or ""),
                        "ok": ok,
                        "error": str(datos.get("error") or ""),
                    }
                )
                del estado["eventos"][:-8]
        except Exception:
            pass

    def _runner():
        try:
            resultado["resumen"] = aplicar_propuestas_en_lote(
                list(usuarios),
                max_workers=max(1, _a_int(max_workers)),
                renombrar=bool(renombrar),
                callback=_callback,
                cancelar=cancelar,
            )
        except BaseException as e:
            resultado["error"] = e

    hilo = threading.Thread(target=_runner, daemon=True)
    st.session_state["nom_lote_hilo"] = hilo
    st.session_state["nom_lote_cancelar"] = cancelar
    st.session_state["nom_lote_lock"] = lock
    st.session_state["nom_lote_estado"] = estado
    st.session_state["nom_lote_resultado"] = resultado
    st.session_state["nom_lote_inicio"] = time.monotonic()
    hilo.start()
    return _monitorear_lote()


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

    st.markdown("---")
    st.markdown("#### 🚫 Cuentas suspendidas por X")
    st.caption(
        "Se marcan automáticamente como `suspended` cuando el bot confirma "
        "(al iniciar sesión o al intentar publicar/retwittear) que X bloqueó "
        "la cuenta — no por errores sueltos de carga o selectores. Borrar "
        "aquí es DEFINITIVO: se pierden cookies, historial y configuración; "
        "no se puede deshacer."
    )
    suspendidas = [f for f in todas if f.get("status") == "suspended"]
    if not suspendidas:
        st.caption("No hay cuentas marcadas como suspendidas.")
    else:
        st.dataframe(
            [
                {"usuario": f["usuario"], "última revisión": f.get("last_checked", "")}
                for f in suspendidas
            ],
            use_container_width=True,
        )
        opciones_sus = {f"@{f['usuario']}": f["usuario"] for f in suspendidas}
        elegidas = st.multiselect(
            "Selecciona cuáles borrar definitivamente",
            list(opciones_sus),
            default=list(opciones_sus),
            key="est_sus_borrar",
        )
        confirmar = st.checkbox(
            "Confirmo que quiero borrar estas cuentas de forma permanente",
            key="est_sus_confirmar",
        )
        if st.button(
            "🗑️ Eliminar definitivamente",
            type="primary",
            disabled=not elegidas or not confirmar,
            key="btn_est_eliminar",
        ):
            usuarios_borrar = [opciones_sus[e] for e in elegidas]
            try:
                eliminadas = _eliminar_cuentas(usuarios_borrar)
                _flash(f"Cuentas eliminadas definitivamente: {eliminadas}.")
                st.rerun()
            except Exception as e:
                st.error(f"No se pudieron eliminar las cuentas: {e}")


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
                cambios_handle = int(res.get("cambios_handle", 0) or 0)
                if cambios_handle > 0:
                    st.info(
                        f"{cambios_handle} cuenta(s) cambiaron de @ real: su "
                        "usuario interno quedó desalineado. Puedes renombrarlas "
                        "para dejar el inventario bien."
                    )
                    # on_click: cambiar cuentas_pestana_selector dentro del
                    # handler lanzaría StreamlitAPIException (el radio ya existe).
                    st.button(
                        "🏷️ Ir a Renombrar usuario",
                        use_container_width=True,
                        key="btn_sync_ir_renombrar",
                        on_click=_ir_a_tab_renombrar,
                    )
            except Exception as e:
                estado.write("")
                st.error(f"Falló la sincronización: {e}")


def _render_resultado_sync_cuenta(res: dict):
    """Muestra el resultado de `sincronizar_cuenta` para UNA cuenta."""
    usuario = res.get("usuario") or ""
    error = res.get("error") or ""
    handle = (res.get("handle") or "").strip()
    nombre = (res.get("nombre") or "").strip()
    if error == "":
        detalle = f"@{handle}" if handle else "(sin @)"
        if nombre:
            detalle += f" — {nombre}"
        st.success(f"Perfil de @{usuario} sincronizado desde X: {detalle}")
    elif error == "sin_datos":
        st.warning(
            f"@{usuario}: X no devolvió datos del perfil (cookies vencidas, "
            "proxy bloqueado o página sin parsear)."
        )
    else:
        st.error(f"No se pudo sincronizar @{usuario}: {error}")


def _render_resultado_renombrado(res: dict, titulo: str = ""):
    """Muestra el dict de `core.renombrar` (archivos, referencias y avisos).

    Sirve para el dry-run y para el renombrado real. Nunca lanza: tolera un
    dict incompleto con `.get`.
    """
    if titulo:
        st.markdown(f"#### {titulo}")
    anterior = res.get("usuario_anterior") or ""
    nuevo = res.get("usuario_nuevo") or ""
    if res.get("ok"):
        if res.get("dry_run"):
            st.success(
                f"Simulación correcta: @{anterior} → @{nuevo}. No se tocó nada."
            )
        else:
            st.success(f"Usuario interno renombrado: @{anterior} → @{nuevo}.")
    else:
        st.error(
            f"No se pudo renombrar @{anterior}: {res.get('error') or 'sin detalle'}"
        )

    archivos = res.get("archivos") or []
    if archivos:
        st.dataframe(
            [
                {
                    "tipo": a.get("tipo", ""),
                    "de": a.get("de", ""),
                    "a": a.get("a", ""),
                    "ok": "✅" if a.get("ok") else "❌",
                    "error": a.get("error", ""),
                }
                for a in archivos
            ],
            use_container_width=True,
        )
    else:
        st.caption(
            "No se encontraron archivos asociados para migrar (cookies `.pkl`, "
            "perfil Chrome, avatar o portada)."
        )

    refs = res.get("referencias") or {}
    st.caption(
        f"Historial migrado: {refs.get('registros', 0)} registro(s) · "
        f"`cookies_path`: {'sí' if refs.get('cookies_path') else 'no'} · "
        f"rutas de imágenes: {'sí' if refs.get('rutas_imagenes') else 'no'}"
    )
    if res.get("advertencias"):
        with st.expander(f"⚠️ Advertencias ({len(res['advertencias'])})"):
            for aviso in res["advertencias"]:
                st.markdown(f"- {aviso}")


def _ir_a_tab_renombrar():
    """Callback: cambia el radio interno a '🏷️ Renombrar usuario'.

    Se usa `on_click` porque `cuentas_pestana_selector` pertenece al radio de
    `render()`, que ya está instanciado cuando corre esta pestaña: asignarlo
    dentro del handler lanzaría `StreamlitAPIException`. Los callbacks corren
    antes del rerun, cuando todavía no hay widget instanciado.
    """
    st.session_state["cuentas_pestana_selector"] = "🏷️ Renombrar usuario"


def _tab_renombrar():
    """Renombra la clave interna `usuario` para alinearla con el @ real."""
    st.markdown("### 🏷️ Renombrar usuario interno")
    st.caption(
        "Renombra la clave interna `usuario` para que el inventario coincida con "
        "el @ real. La migración mueve las cookies `.pkl`, el perfil de Chrome, "
        "el avatar/portada y el historial (`RegistroAccion`); las **tareas "
        "programadas no se ven afectadas** porque referencian cuentas por `id`. "
        "Recomendado: sincroniza primero el perfil desde X y luego renombra aquí."
    )

    # Resultados pendientes de una acción anterior que hizo st.rerun() (el
    # rerun descarta lo dibujado: se guardan y se muestran en esta pasada).
    resultado_previo = st.session_state.pop("cuentas_renombrar_resultado", None)
    if resultado_previo:
        _render_resultado_renombrado(
            resultado_previo, "📄 Resultado del último renombrado"
        )

    sync_previo = st.session_state.pop("cuentas_renombrar_sync", None)
    if sync_previo:
        _render_resultado_sync_cuenta(sync_previo)

    cuentas = _listar_cuentas(OPCION_TODAS)
    if not cuentas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    opciones = {_etiqueta_cuenta_perfil(f): f for f in cuentas}
    etiqueta = st.selectbox(
        "Cuenta a renombrar", list(opciones.keys()), key="ren_cuenta"
    )
    fila = opciones[etiqueta]
    usuario = fila["usuario"]
    handle_real = (fila.get("handle_actual") or "").strip().lstrip("@")
    nombre_real = (fila.get("nombre_mostrado") or "").strip()
    difiere = bool(handle_real) and handle_real.lower() != usuario.lower()

    # ---------------- Estado actual ----------------
    st.markdown("#### 📋 Estado actual")
    c1, c2, c3 = st.columns(3)
    c1.metric("Usuario interno", f"@{usuario}")
    c2.metric("@ real en X", f"@{handle_real}" if handle_real else "—")
    c3.metric("Nombre en X", nombre_real or "—")

    if difiere:
        st.info(
            f"El @ real guardado (@{handle_real}) difiere del usuario interno "
            f"(@{usuario}). Sincroniza para confirmar que sigue vigente."
        )
    elif not handle_real:
        st.warning(
            "Esta cuenta no tiene `handle_actual` guardado; sincronízala para "
            "leer su @ real desde X."
        )
    else:
        st.caption("El usuario interno ya coincide con el @ real guardado.")

    if st.button(
        "🔄 Sincronizar este perfil desde X",
        use_container_width=True,
        key=f"ren_sync_{usuario}",
        help="Relee nombre/@ reales con las cookies guardadas (httpx, sin Chrome).",
    ):
        resultado = None
        try:
            with get_db_session() as db:
                cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is None:
                st.error(f"No se encontró @{usuario} en la base de datos.")
            else:
                from plataformas.twitter.perfil import sincronizar_cuenta

                resultado = sincronizar_cuenta(cuenta)
        except Exception as e:
            st.error(f"Falló la sincronización de @{usuario}: {e}")
        if resultado is not None:
            _listar_cuentas.clear()
            st.session_state["cuentas_renombrar_sync"] = resultado
            _flash(f"Perfil de @{usuario} sincronizado desde X.")
            st.rerun()

    # ---------------- Nuevo usuario ----------------
    st.markdown("---")
    st.markdown("#### ✍️ Nuevo usuario interno")
    default_nuevo = handle_real if difiere else usuario
    nuevo = (
        st.text_input(
            "Nuevo usuario interno",
            value=default_nuevo,
            key=f"ren_nuevo_{usuario}",
            help=(
                "Clave interna usada por cookies, perfil Chrome y rutas de "
                "imágenes. Por defecto se propone el @ real."
            ),
        )
        .strip()
        .lstrip("@")
        .strip()
    )

    if nuevo and not HANDLE_RE.match(nuevo):
        st.warning(
            f"'{nuevo}' no cumple el formato de handle de X "
            "(4-15 caracteres: letras, números y _). Puedes continuar, pero "
            "verifica que sea un nombre válido."
        )

    if st.button(
        "🔍 Previsualizar (dry-run)",
        use_container_width=True,
        key="btn_ren_dry",
        help="Calcula la migración sin tocar la BD ni mover archivos.",
    ):
        from core.renombrar import renombrar_usuario

        res = renombrar_usuario(usuario, nuevo, dry_run=True)
        _render_resultado_renombrado(res, "👁️ Simulación (no se tocó nada)")

    # ---------------- Renombrado real ----------------
    st.markdown("---")
    st.markdown("#### 🏷️ Aplicar renombrado")
    confirmado = st.checkbox(
        "Confirmo renombrar la clave interna (migra los archivos y el historial)",
        key=f"ren_confirm_{usuario}",
    )
    if st.button(
        "🏷️ Renombrar usuario interno",
        type="primary",
        use_container_width=True,
        key=f"ren_btn_{usuario}",
    ):
        if not confirmado:
            st.warning("Marca la casilla de confirmación para continuar.")
        elif not nuevo:
            st.warning("Escribe el nuevo usuario interno.")
        elif nuevo.lower() == usuario.lower():
            st.warning("El nuevo usuario es igual al actual: no hay nada que renombrar.")
        else:
            from core.renombrar import renombrar_usuario

            with st.spinner(f"Renombrando @{usuario} → @{nuevo}..."):
                res = renombrar_usuario(usuario, nuevo)
            _listar_cuentas.clear()
            st.session_state["cuentas_renombrar_resultado"] = res
            if res.get("ok"):
                _flash(f"Usuario interno renombrado: @{usuario} → @{nuevo}.")
            st.rerun()

    # ---------------- Lote al @ real ----------------
    st.markdown("---")
    st.markdown("#### 🧹 Renombrar al @ real (lote)")
    st.caption(
        "Detecta las cuentas cuyo `handle_actual` difiere del usuario interno y "
        "las renombra a ese @. Es la forma práctica de dejar el inventario "
        "alineado tras una sincronización masiva."
    )

    pendientes = []
    for f in cuentas:
        handle_f = (f.get("handle_actual") or "").strip().lstrip("@")
        if handle_f and handle_f.lower() != (f.get("usuario") or "").lower():
            pendientes.append(f)

    if not pendientes:
        st.info(
            "No hay cuentas pendientes: el usuario interno ya coincide con el "
            "@ real guardado."
        )
        return

    st.caption(f"Cuentas pendientes: **{len(pendientes)}**.")
    opciones_lote = {_etiqueta_cuenta_perfil(f): f for f in pendientes}
    seleccion_lote = st.multiselect(
        "Cuentas a renombrar al @ real",
        list(opciones_lote.keys()),
        key="ren_lote_cuentas",
    )
    confirmado_lote = st.checkbox(
        "Confirmo renombrar las cuentas seleccionadas al @ real guardado",
        key="ren_lote_confirm",
    )
    if st.button(
        "🏷️ Renombrar seleccionadas al @ real",
        type="primary",
        use_container_width=True,
        key="btn_ren_lote",
    ):
        if not confirmado_lote:
            st.warning("Marca la casilla de confirmación para continuar.")
        elif not seleccion_lote:
            st.warning("Selecciona al menos una cuenta.")
        else:
            from core.renombrar import renombrar_al_handle_actual

            usuarios_lote = [
                opciones_lote[label]["usuario"]
                for label in seleccion_lote
                if label in opciones_lote
            ]
            total = len(usuarios_lote)
            barra = st.progress(0.0)
            estado = st.empty()
            filas_resumen = []
            ok = 0
            for i, usuario_lote in enumerate(usuarios_lote, start=1):
                estado.write(f"⏳ Renombrando @{usuario_lote} ({i}/{total})...")
                try:
                    res_lote = renombrar_al_handle_actual(usuario_lote)
                except Exception as e:  # blindaje extra: nunca debería lanzar
                    res_lote = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                movidos = sum(
                    1 for a in (res_lote.get("archivos") or []) if a.get("ok")
                )
                if res_lote.get("ok"):
                    ok += 1
                filas_resumen.append(
                    {
                        "usuario anterior": res_lote.get("usuario_anterior")
                        or usuario_lote,
                        "usuario nuevo": res_lote.get("usuario_nuevo") or "",
                        "ok": "✅" if res_lote.get("ok") else "❌",
                        "archivos movidos": movidos,
                        "error": res_lote.get("error") or "",
                        "advertencias": " · ".join(
                            res_lote.get("advertencias") or []
                        ),
                    }
                )
                barra.progress(i / total)

            _listar_cuentas.clear()
            estado.write("✅ Lote terminado")
            if ok:
                st.success(f"Renombradas: {ok}/{total} cuenta(s).")
            if ok < total:
                st.error(f"Fallidas: {total - ok}/{total} cuenta(s).")
            st.dataframe(filas_resumen, use_container_width=True)


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
    renombrar_interno = st.checkbox(
        "Renombrar también el usuario interno al nuevo @ "
        "(migra cookies/perfiles/historial)",
        key=f"perfil_renombrar_{usuario}",
        help=(
            "Si el cambio de @ en X tiene éxito, la clave interna `usuario` pasa "
            "a ser el nuevo @. Las tareas programadas no se ven afectadas "
            "(referencian cuentas por id)."
        ),
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
                if renombrar_interno and handle_env.lower() != usuario.lower():
                    # El bot ya persistió `handle_actual`; aquí solo se migra la
                    # clave interna y sus archivos al nuevo @.
                    from core.renombrar import renombrar_usuario

                    with st.spinner(
                        f"Migrando la clave interna @{usuario} → @{handle_env}..."
                    ):
                        res_ren = renombrar_usuario(usuario, handle_env)
                    _listar_cuentas.clear()
                    if res_ren.get("ok"):
                        st.success(
                            "Usuario interno renombrado: "
                            f"@{res_ren.get('usuario_anterior') or usuario} → "
                            f"@{res_ren.get('usuario_nuevo') or handle_env}."
                        )
                    else:
                        st.error(
                            "No se pudo renombrar el usuario interno: "
                            f"{res_ren.get('error') or 'sin detalle'}"
                        )
                    for aviso in res_ren.get("advertencias") or []:
                        st.warning(aviso)
                elif renombrar_interno:
                    st.caption(
                        "El usuario interno ya coincide con el nuevo @: no hace "
                        "falta renombrar la clave interna."
                    )
                else:
                    st.info(
                        "El @ interno (`usuario`) NO cambió por diseño: la clave "
                        f"interna de login sigue siendo `{usuario}`. Marca la "
                        "casilla de renombrado si también quieres migrarla."
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


# ------------------- Perfil completo (flujo guiado en X) -------------------

# Campos booleanos que devuelve `actualizar_perfil_completo`, en el orden del
# flujo del backend (login → foto de perfil → portada → nombre → bio →
# ubicación → @). Se usan para el resumen persistente.
PERFIL_PASOS_RESUMEN = [
    ("login", "Inicio de sesión"),
    ("foto_perfil", "Foto de perfil"),
    ("foto_portada", "Foto de portada"),
    ("nombre", "Nombre mostrado"),
    ("bio", "Biografía"),
    ("ubicacion", "Ubicación"),
    ("handle", "Usuario (@)"),
]


def _progreso_perfil_callback(barra, estado, pasos: list, hilo_ui: int, lock):
    """Callback de progreso para `TwitterBot.actualizar_perfil_completo`.

    El backend Selenium es síncrono, así que en la práctica el callback corre en
    el mismo hilo que el script de Streamlit y puede pintar la barra/el estado en
    vivo. Si el backend lo invocara desde un hilo auxiliar, solo se registra el
    paso en `pasos` (protegido por `lock`) y NO se tocan widgets de Streamlit
    fuera de su hilo: eso es lo que provocaría excepciones y condiciones de
    carrera con la sesión.
    """

    def _callback(actual, total, paso, ok, detalle):
        registro = {
            "paso": str(paso or ""),
            "ok": bool(ok),
            "detalle": str(detalle or ""),
        }
        with lock:
            pasos.append(registro)

        if threading.get_ident() != hilo_ui:
            return
        try:
            if total:
                valor = max(0.0, min(float(actual) / float(total), 1.0))
                barra.progress(
                    valor, text=f"Paso {actual}/{total}: {registro['paso']}"
                )
            icono = "✅" if registro["ok"] else "❌"
            mensaje = f"{icono} [{actual}/{total}] {registro['paso']}"
            if registro["detalle"]:
                mensaje += f" — {registro['detalle']}"
            estado.write(mensaje)
        except Exception:
            pass

    return _callback


def _render_resultado_perfil_completo(resultado: dict):
    """Resumen (persistente en session_state) del flujo completo, real o dry-run."""
    usuario = resultado.get("usuario") or ""
    res = resultado.get("res") or {}
    dry = bool(resultado.get("dry_run"))
    pasos = res.get("pasos") or resultado.get("pasos_callback") or []

    if dry:
        st.info(
            f"Simulación (dry-run) para @{usuario}: se recorrieron los pasos "
            "sin guardar cambios en X."
        )
    elif res.get("ok"):
        st.success(f"Flujo completo terminado en @{usuario}.")
    else:
        st.error(f"El flujo en @{usuario} terminó con pasos fallidos.")

    filas = [
        {"paso": etiqueta, "resultado": "✅" if res.get(clave) else "—"}
        for clave, etiqueta in PERFIL_PASOS_RESUMEN
        if clave in res
    ]
    if filas:
        st.dataframe(filas, use_container_width=True)

    if pasos:
        st.markdown("#### 🧭 Pasos ejecutados")
        st.dataframe(
            [
                {
                    "paso": p.get("paso", ""),
                    "ok": "✅" if p.get("ok") else "❌",
                    "detalle": p.get("detalle", ""),
                }
                for p in pasos
            ],
            use_container_width=True,
        )
    else:
        st.caption("El backend no reportó pasos individuales.")

    if res.get("error"):
        st.warning(f"Detalle del error: {res['error']}")

    res_ren = resultado.get("renombrado")
    if res_ren:
        _render_resultado_renombrado(res_ren, "🏷️ Renombrado de la clave interna")


def _tab_perfil_completo():
    """Flujo guiado completo del perfil en X, en el orden que pidió el usuario.

    El backend (`TwitterBot.actualizar_perfil_completo`) es el único que toca X;
    esta pestaña solo arma el formulario, muestra el progreso paso a paso y
    persiste el resumen para que el `st.rerun()` no lo borre."""
    st.markdown("### 🧾 Perfil completo (flujo en orden)")
    st.caption(
        "Ejecuta el flujo completo en el orden del backend: **login con cookie → "
        "foto de perfil → foto de portada → nombre → biografía → ubicación → @ "
        "(con contraseña)**. El dry-run verifica selectores sin guardar cambios; "
        "la ejecución real **requiere Chrome instalado**."
    )
    st.warning(
        "Los cambios son **REALES e irreversibles** en X. Revisa la cuenta y "
        "cada dato antes de ejecutar."
    )

    # El rerun posterior a una ejecución exitosa descarta lo dibujado: el
    # resumen se guarda en session_state y se muestra en esta pasada.
    resultado_previo = st.session_state.pop(
        "cuentas_perfil_completo_resultado", None
    )
    if resultado_previo:
        _render_resultado_perfil_completo(resultado_previo)

    cuentas = _listar_cuentas(OPCION_TODAS)
    if not cuentas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    opciones = {_etiqueta_cuenta_perfil(f): f for f in cuentas}
    etiqueta = st.selectbox(
        "Cuenta a configurar", list(opciones.keys()), key="pc_cuenta"
    )
    fila = opciones[etiqueta]
    usuario = fila["usuario"]
    nombre_default = (fila.get("nombre_mostrado") or "").strip()
    handle_default = (
        (fila.get("handle_actual") or "").strip() or usuario
    ).lstrip("@")
    password_default = fila.get("password") or ""

    st.caption(
        "Deja un campo vacío para omitir ese paso. Las fotos son opcionales: si "
        "no subes ninguna, esos pasos se saltan."
    )

    # 1) Foto de perfil
    st.markdown("**1. 📷 Foto de perfil**")
    archivo_perfil = st.file_uploader(
        "Sube o pega la foto de perfil (png/jpg/jpeg/webp)",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"pc_foto_perfil_{usuario}",
    )
    if archivo_perfil is not None:
        st.image(
            archivo_perfil,
            caption="Vista previa de la foto de perfil",
            width=220,
        )

    # 2) Foto de portada
    st.markdown("**2. 🖼️ Foto de portada**")
    archivo_portada = st.file_uploader(
        "Sube o pega la foto de portada (png/jpg/jpeg/webp)",
        type=["png", "jpg", "jpeg", "webp"],
        key=f"pc_foto_portada_{usuario}",
    )
    if archivo_portada is not None:
        st.image(
            archivo_portada,
            caption="Vista previa de la foto de portada",
            width=220,
        )

    # 3) Nombre mostrado
    st.markdown("**3. 👤 Nombre mostrado**")
    nombre_txt = st.text_input(
        "Nombre mostrado (vacío = no cambiar)",
        value=nombre_default,
        key=f"pc_nombre_{usuario}",
    )
    nombre_env = (nombre_txt or "").strip()
    if nombre_env and nombre_env == nombre_default:
        st.caption(
            "ℹ️ Es el mismo nombre guardado; si no quieres cambiarlo, deja el "
            "campo vacío."
        )

    # 4) Biografía (máx. 160)
    st.markdown("**4. 📝 Biografía**")
    bio_txt = st.text_area(
        "Biografía (máx. 160 caracteres; vacío = no cambiar)",
        value="",
        max_chars=160,
        key=f"pc_bio_{usuario}",
    )
    bio_env = (bio_txt or "").strip()
    if len(bio_env) > 160:
        bio_env = bio_env[:160]
        st.warning(
            "La biografía supera los 160 caracteres: se enviarán solo los "
            "primeros 160."
        )
    if bio_env:
        st.caption(f"Caracteres: {len(bio_env)}/160")

    # 5) Ubicación
    st.markdown("**5. 📍 Ubicación**")
    ubicacion_txt = st.text_input(
        "Ubicación (vacío = no cambiar)",
        value="",
        key=f"pc_ubicacion_{usuario}",
    )
    ubicacion_env = (ubicacion_txt or "").strip()

    # 6) Nuevo @
    st.markdown("**6. 🏷️ Nuevo @**")
    handle_txt = st.text_input(
        "Nuevo @ (sin @; vacío = no cambiar)",
        value=handle_default,
        key=f"pc_handle_{usuario}",
    )
    handle_env = (handle_txt or "").strip().lstrip("@").strip()
    if handle_env and not HANDLE_RE.match(handle_env):
        st.warning(
            f"'{handle_env}' no cumple el formato de handle de X "
            "(4-15 caracteres: letras, números y _)."
        )
    if handle_env and handle_env.lower() == handle_default.lower():
        st.caption(
            "ℹ️ Es el @ actual guardado; si no quieres cambiarlo, deja el campo "
            "vacío."
        )

    # 7) Contraseña (Account information)
    st.markdown("**7. 🔑 Contraseña**")
    password_txt = st.text_input(
        "Contraseña de la cuenta (para Account information)",
        value=password_default,
        type="password",
        key=f"pc_pass_{usuario}",
    )
    password_env = password_txt or ""

    st.markdown("---")
    confirmado = st.checkbox(
        "Confirmo que quiero modificar el perfil REAL de esta cuenta en X",
        key=f"pc_confirm_{usuario}",
    )
    renombrar_interno = st.checkbox(
        "Renombrar también la clave interna al nuevo @ "
        "(migra cookies/perfiles/historial)",
        key=f"pc_renombrar_{usuario}",
        help=(
            "Si el paso del @ tiene éxito, la clave interna `usuario` pasa a ser "
            "el nuevo @. Las tareas programadas no se ven afectadas "
            "(referencian cuentas por id)."
        ),
    )

    col_dry, col_ejecutar = st.columns(2)
    with col_dry:
        btn_dry = st.button(
            "🔍 Verificar selectores (dry-run)",
            use_container_width=True,
            key=f"pc_dry_{usuario}",
            help="Recorre los pasos sin guardar cambios en X (no toca el perfil).",
        )
    with col_ejecutar:
        btn_ejecutar = st.button(
            "▶️ Ejecutar flujo completo en X",
            type="primary",
            use_container_width=True,
            key=f"pc_btn_{usuario}",
        )

    if not (btn_dry or btn_ejecutar):
        return

    if btn_ejecutar and not confirmado:
        st.warning("Marca la casilla de confirmación para ejecutar el flujo real.")
        return

    if handle_env and not HANDLE_RE.match(handle_env):
        st.error(
            "Corrige el nuevo @ antes de ejecutar "
            "(4-15 caracteres: letras, números y _)."
        )
        return

    # Las imágenes subidas se guardan en data/temp solo al ejecutar; se
    # reutiliza el mismo helper que Publicar Texto / RT con cita.
    foto_perfil_path = None
    foto_portada_path = None
    if archivo_perfil is not None or archivo_portada is not None:
        from web.operaciones._helpers import guardar_imagen_subida

        if archivo_perfil is not None:
            foto_perfil_path = guardar_imagen_subida(
                archivo_perfil, prefijo=f"perfil_{usuario}"
            )
        if archivo_portada is not None:
            foto_portada_path = guardar_imagen_subida(
                archivo_portada, prefijo=f"portada_{usuario}"
            )

    hay_algo = bool(
        foto_perfil_path
        or foto_portada_path
        or nombre_env
        or bio_env
        or ubicacion_env
        or handle_env
    )
    if not hay_algo:
        st.warning(
            "No hay nada que hacer: sube una foto o llena al menos un campo "
            "(nombre, biografía, ubicación o @)."
        )
        return

    dry_run = bool(btn_dry and not btn_ejecutar)

    hilo_ui = threading.get_ident()
    pasos_callbacks = []
    lock = threading.Lock()
    barra = st.progress(0.0, text="Iniciando flujo de perfil…")
    estado = st.empty()
    callback = _progreso_perfil_callback(
        barra, estado, pasos_callbacks, hilo_ui, lock
    )

    bot = None
    res = None
    try:
        from plataformas.twitter.selenium_bot import TwitterBot

        accion = (
            "Verificando selectores" if dry_run else "Ejecutando flujo completo"
        )
        with st.spinner(f"{accion} en @{usuario} (Chrome)..."):
            bot = TwitterBot(usuario)
            res = bot.actualizar_perfil_completo(
                foto_perfil_path=foto_perfil_path,
                foto_portada_path=foto_portada_path,
                nombre=nombre_env or None,
                bio=bio_env or None,
                ubicacion=ubicacion_env or None,
                handle=handle_env or None,
                password=password_env,
                dry_run=dry_run,
                callback=callback,
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
    if not isinstance(res, dict):
        res = {"ok": bool(res)}

    pasos_finales = list(res.get("pasos") or pasos_callbacks or [])

    # Renombrado opcional de la clave interna SOLO si el paso del @ tuvo éxito.
    res_ren = None
    if (
        not dry_run
        and res.get("handle")
        and renombrar_interno
        and handle_env
        and handle_env.lower() != usuario.lower()
    ):
        from core.renombrar import renombrar_usuario

        with st.spinner(
            f"Migrando la clave interna @{usuario} → @{handle_env}..."
        ):
            try:
                res_ren = renombrar_usuario(usuario, handle_env)
            except Exception as e:
                res_ren = {
                    "ok": False,
                    "usuario_anterior": usuario,
                    "usuario_nuevo": handle_env,
                    "error": f"{type(e).__name__}: {e}",
                }

    exitos = [
        clave
        for clave, _ in PERFIL_PASOS_RESUMEN
        if res.get(clave)
    ]
    resultado = {
        "usuario": usuario,
        "res": {**res, "pasos": pasos_finales},
        "dry_run": dry_run,
        "renombrado": res_ren,
        "pasos_callback": pasos_callbacks,
    }

    if not dry_run and exitos:
        _listar_cuentas.clear()
        st.session_state["cuentas_perfil_completo_resultado"] = resultado
        _flash(
            f"Flujo de perfil en @{usuario}: "
            f"{len(exitos)} de {len(PERFIL_PASOS_RESUMEN)} paso(s) aplicado(s)."
        )
        st.rerun()

    _render_resultado_perfil_completo(resultado)


# ------------------- Anti-detección (User-Agent + cookies) -------------------

def _datos_anti_deteccion(usuario: str) -> dict:
    """Lee `cookies_json`, `auth_token` y `user_agent` de una cuenta, ya planos.

    Se consulta en vivo (no desde `_listar_cuentas`) para mostrar las cookies
    completas sin engordar la cache del inventario. Nunca lanza: ante error
    devuelve `error` con el detalle."""
    datos = {"cookies": [], "auth_token": "", "user_agent": "", "error": ""}
    try:
        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if cuenta is None:
                datos["error"] = f"No se encontró @{usuario} en la base de datos."
                return datos
            cookies = getattr(cuenta, "cookies_json", None)
            if isinstance(cookies, str):
                try:
                    cookies = json.loads(cookies)
                except Exception:
                    cookies = []
            if isinstance(cookies, dict):
                cookies = cookies.get("cookies") or []
            datos["cookies"] = (
                list(cookies) if isinstance(cookies, (list, tuple)) else []
            )
            datos["auth_token"] = getattr(cuenta, "auth_token", "") or ""
            datos["user_agent"] = getattr(cuenta, "user_agent", "") or ""
    except Exception as e:
        datos["error"] = f"No se pudo consultar la cuenta: {e}"
    return datos


def _nombres_cookies(cookies) -> list:
    """Nombres de las cookies guardadas (acepta `name`/`Name`), sin duplicar."""
    nombres = []
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        nombre = cookie.get("name", cookie.get("Name"))
        if nombre and str(nombre) not in nombres:
            nombres.append(str(nombre))
    return nombres


def _valor_cookie(cookies, objetivo: str) -> str:
    """Valor de la primera cookie con ese nombre (comparacion sin mayusculas)."""
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        nombre = cookie.get("name", cookie.get("Name"))
        if nombre and str(nombre).strip().lower() == objetivo.lower():
            valor = cookie.get("value", cookie.get("Value"))
            if valor not in (None, ""):
                return str(valor)
    return ""


def _guardar_user_agent(usuario: str, user_agent: str) -> int:
    """Guarda `Cuenta.user_agent` ('' = UA natural). Devuelve filas cambiadas."""
    with get_db_session() as db:
        cambiadas = (
            db.query(Cuenta)
            .filter(Cuenta.plataforma == "twitter", Cuenta.usuario == usuario)
            .update({Cuenta.user_agent: user_agent}, synchronize_session=False)
        )
    _listar_cuentas.clear()
    return cambiadas


def _guardar_cookies_completas(usuario: str, cookies: list) -> dict:
    """Reemplaza `Cuenta.cookies_json` con la lista decodificada COMPLETA.

    Si el lote trae una cookie `auth_token` con valor, tambien actualiza
    `Cuenta.auth_token` (lo usa el validador de sesiones). Devuelve
    `{"ok": bool, "auth_token": bool, "error": str}`; nunca lanza."""
    try:
        auth = _valor_cookie(cookies, "auth_token")
        with get_db_session() as db:
            cuenta = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.usuario == usuario)
                .first()
            )
            if cuenta is None:
                return {
                    "ok": False,
                    "auth_token": False,
                    "error": f"No se encontró @{usuario} en la base de datos.",
                }
            cuenta.cookies_json = cookies
            if auth:
                cuenta.auth_token = auth.strip()[:200]
        _listar_cuentas.clear()
        return {"ok": True, "auth_token": bool(auth), "error": ""}
    except Exception as e:
        return {"ok": False, "auth_token": False, "error": str(e)}


def _tab_anti_deteccion():
    """User-Agent por cuenta y cookies completas (anti-deteccion)."""
    st.markdown("### 🛡️ Anti-detección (User-Agent y cookies)")
    st.caption(
        "`undetected-chromedriver` ya oculta `navigator.webdriver` al abrir "
        "Chrome. Esta pestaña controla lo que depende del lote: el "
        "**User-Agent de cada cuenta** (debe coincidir con el navegador que "
        "emitió las cookies) y las **cookies completas** (`auth_token`, `ct0`, "
        "`twid`…) que se inyectan al abrir el navegador."
    )

    cuentas = _listar_cuentas(OPCION_TODAS)
    if not cuentas:
        st.info("No hay cuentas de Twitter en la base de datos.")
        return

    preview = st.session_state.pop("cuentas_ad_cookies_preview", None)
    if preview:
        st.success(
            f"Cookies guardadas para @{preview['usuario']}: "
            f"{preview['total']} cookie(s), {preview['inyectables']} inyectable(s)."
        )
        if preview.get("nombres"):
            st.caption("Nombres: " + ", ".join(preview["nombres"]))

    opciones = {_etiqueta_cuenta_perfil(f): f for f in cuentas}
    etiqueta = st.selectbox("Cuenta", list(opciones.keys()), key="ad_cuenta")
    fila = opciones[etiqueta]
    usuario = fila["usuario"]

    datos = _datos_anti_deteccion(usuario)
    if datos.get("error"):
        st.error(datos["error"])
        return

    from utils.anti_detection import normalizar_cookies, resolver_ua_cuenta

    cookies = datos["cookies"]
    nombres = _nombres_cookies(cookies)
    ua_bd = (datos["user_agent"] or "").strip()
    ua_efectivo = resolver_ua_cuenta({"user_agent": ua_bd})
    ua_global = resolver_ua_cuenta({"user_agent": ""})

    st.markdown("#### 📋 Estado actual")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Cookies guardadas", len(cookies))
        if nombres:
            resumen_nombres = ", ".join(nombres[:10])
            if len(nombres) > 10:
                resumen_nombres += f" … (+{len(nombres) - 10})"
            st.caption(resumen_nombres)
        else:
            st.caption("Sin cookies guardadas.")
    with c2:
        st.metric("User-Agent en BD", "Sí" if ua_bd else "No")
        st.caption(ua_bd or "—")
    with c3:
        if ua_bd:
            origen_ua = "Cuenta"
        elif ua_global:
            origen_ua = "Global"
        else:
            origen_ua = "Natural"
        st.metric("UA efectivo", origen_ua)
        st.caption(ua_efectivo or "UA natural de Chrome")

    if ua_bd and "Mozilla/" not in ua_bd:
        st.warning(
            "El User-Agent guardado no contiene `Mozilla/`; los navegadores "
            "reales sí lo incluyen, así que probablemente sea inválido."
        )

    # ---------------- Editor de User-Agent ----------------
    st.markdown("---")
    st.markdown("#### ✍️ User-Agent de esta cuenta")
    version_ua = int(st.session_state.get("ad_ua_version", 0) or 0)
    ua_editado = st.text_area(
        "User-Agent",
        value=ua_bd,
        height=100,
        key=f"ad_ua_texto_{usuario}_{version_ua}",
        placeholder="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
        help="Se usa al abrir Chrome para esta cuenta. Vacío = UA natural del navegador.",
    )
    ua_limpio = (ua_editado or "").strip()
    no_mozilla = bool(ua_limpio) and "Mozilla/" not in ua_limpio

    confirmar_ua = False
    if no_mozilla:
        st.warning(
            "Este User-Agent no contiene `Mozilla/`: los navegadores reales sí. "
            "Guardarlo así puede delatar la automatización."
        )
        confirmar_ua = st.checkbox(
            "Confirmo guardar un User-Agent sin `Mozilla/`",
            key=f"ad_ua_confirmar_{usuario}_{version_ua}",
        )

    col_guardar, col_limpiar = st.columns(2)
    with col_guardar:
        if st.button(
            "💾 Guardar User-Agent",
            type="primary",
            use_container_width=True,
            key=f"ad_ua_guardar_{usuario}_{version_ua}",
        ):
            if not ua_limpio:
                st.warning(
                    "El campo está vacío: usa 'Limpiar (usar UA natural)' para "
                    "dejar la cuenta con el UA del navegador."
                )
            elif no_mozilla and not confirmar_ua:
                st.warning(
                    "Marca la casilla de confirmación para guardar un "
                    "User-Agent sin `Mozilla/`."
                )
            else:
                try:
                    if _guardar_user_agent(usuario, ua_limpio):
                        st.session_state["ad_ua_version"] = version_ua + 1
                        _flash(
                            f"User-Agent de @{usuario} guardado"
                            + (" (sin `Mozilla/`)." if no_mozilla else ".")
                        )
                        st.rerun()
                    else:
                        st.error(f"No se encontró @{usuario} en la base de datos.")
                except Exception as e:
                    st.error(f"No se pudo guardar el User-Agent: {e}")
    with col_limpiar:
        if st.button(
            "🧹 Limpiar (usar UA natural)",
            use_container_width=True,
            key=f"ad_ua_limpiar_{usuario}_{version_ua}",
        ):
            try:
                if _guardar_user_agent(usuario, ""):
                    st.session_state["ad_ua_version"] = version_ua + 1
                    _flash(
                        f"User-Agent de @{usuario} limpiado: usará el UA "
                        "natural de Chrome."
                    )
                    st.rerun()
                else:
                    st.error(f"No se encontró @{usuario} en la base de datos.")
            except Exception as e:
                st.error(f"No se pudo limpiar el User-Agent: {e}")

    # ---------------- Inyector de cookies ----------------
    st.markdown("---")
    st.markdown("#### 🍪 Cookies completas (pegar del vendedor)")
    st.caption(
        "Acepta JSON crudo (`[...]` o `{\"cookies\":[...]}`) o base64. Se "
        "guardan COMPLETAS (`auth_token`, `ct0`, `twid`…), no solo el "
        "`auth_token`; si el lote trae esa cookie, también se actualiza "
        "`Cuenta.auth_token`."
    )
    version_ck = int(st.session_state.get("ad_cookies_version", 0) or 0)
    texto_cookies = st.text_area(
        "Cookies (JSON crudo o base64)",
        height=160,
        key=f"ad_cookies_texto_{usuario}_{version_ck}",
        placeholder='[{"name":"auth_token","value":"..."}, ...]',
    )
    confirmar_cookies = st.checkbox(
        "Confirmo reemplazar las cookies guardadas de esta cuenta",
        key=f"ad_cookies_confirmar_{usuario}_{version_ck}",
    )
    if st.button(
        "🍪 Guardar cookies completas",
        type="primary",
        use_container_width=True,
        key=f"ad_cookies_guardar_{usuario}_{version_ck}",
    ):
        if not (texto_cookies or "").strip():
            st.warning("Pega las cookies primero (JSON crudo o base64).")
        elif not confirmar_cookies:
            st.warning("Marca la casilla de confirmación para reemplazar las cookies.")
        else:
            from cuentas.importador import decodificar_cookies

            lista = decodificar_cookies(texto_cookies)
            if not lista:
                st.error(
                    "No se pudieron decodificar: revisa que sea JSON válido "
                    "(`[...]` o `{\"cookies\":[...]}`) o base64 correcto."
                )
            else:
                normalizadas = normalizar_cookies(lista)
                if not normalizadas:
                    st.error(
                        "Las cookies decodificaron, pero ninguna es inyectable "
                        "en Selenium (revisa `name`/`value`): no se guardó nada."
                    )
                else:
                    if len(normalizadas) < len(lista):
                        st.warning(
                            f"{len(lista) - len(normalizadas)} cookie(s) no son "
                            "inyectables y Selenium las ignorará."
                        )
                    res = _guardar_cookies_completas(usuario, lista)
                    if res.get("ok"):
                        st.session_state["cuentas_ad_cookies_preview"] = {
                            "usuario": usuario,
                            "total": len(lista),
                            "inyectables": len(normalizadas),
                            "nombres": _nombres_cookies(lista)[:10],
                        }
                        st.session_state["ad_cookies_version"] = version_ck + 1
                        extra = (
                            " y `auth_token` actualizado"
                            if res.get("auth_token")
                            else " (sin cookie `auth_token`: no se tocó la columna)"
                        )
                        _flash(
                            f"Cookies de @{usuario} guardadas: {len(lista)} en "
                            f"total, {len(normalizadas)} inyectables{extra}."
                        )
                        st.rerun()
                    else:
                        st.error(
                            res.get("error")
                            or "No se pudieron guardar las cookies."
                        )

    # ---------------- Verificación manual ----------------
    st.markdown("---")
    with st.expander("✅ Verificación manual"):
        st.markdown(
            "1. Abre el navegador controlado y pulsa `F12` para abrir DevTools.\n"
            "2. En la consola ejecuta `navigator.webdriver`: debe devolver "
            "`undefined` (o `false`).\n"
            "3. En la pestaña Network revisa el header `User-Agent` de cualquier "
            "petición: debe ser idéntico al del lote.\n"
            "4. Entra a `https://x.com/home`: debe abrir el feed directo, sin "
            "pasar por `https://x.com/account/access`."
        )


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
                "user_agent": _abreviar(f.get("user_agent"), 40),
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


def _modo_de_tab(tab: str) -> str:
    """Modo (clave de `MODOS_TABS`) que contiene la pestaña `tab`.

    Si la pestaña no está en ningún modo devuelve el primero, para no dejar la
    UI sin un modo válido."""
    for modo, tabs in MODOS_TABS.items():
        if tab in tabs:
            return modo
    return next(iter(MODOS_TABS))


def _on_cambio_modo():
    """Al cambiar de modo, abre la primera pestaña de ese modo.

    Es un callback: corre antes de instanciar el radio de pestañas, así que
    puede reescribir `cuentas_pestana_selector` sin tocar un widget ya creado."""
    try:
        tabs_modo = MODOS_TABS.get(st.session_state.get("cuentas_modo_selector")) or []
        if tabs_modo:
            st.session_state["cuentas_pestana_selector"] = tabs_modo[0]
    except Exception:
        pass


def _restaurar_pestana_desde_url():
    """Restaura la pestaña interna desde `?tab=` en recargas del navegador.

    Solo actúa cuando `cuentas_pestana_selector` aún no existe en
    session_state (sesión nueva): en un st.rerun() normal el radio conserva
    su valor solo y no se toca nada. Los valores inválidos se ignoran.
    También preselecciona el MODO que contiene esa pestaña."""
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
        st.session_state["cuentas_modo_selector"] = _modo_de_tab(str(tab))


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
        "Perfiles, secciones (IP/CI/Libertad/Justicia), registro de lenguaje, nombres y validación",
    )

    flash = st.session_state.pop("cuentas_flash", "")
    if flash:
        st.success(flash)

    paginas = {
        "📥 Importar": _tab_importar,
        "🔎 Validar": _tab_validar,
        "🗂️ Secciones (IP/CI/Libertad/Justicia)": _tab_secciones,
        "🎭 Registro": _tab_registro,
        "🎨 Perfiles": _tab_perfiles,
        "🏷️ Nombres": _tab_nombres,
        "⏸️ Estado": _tab_estado,
        "📷 Fotos": _tab_fotos,
        "🛡️ Anti-detección": _tab_anti_deteccion,
        "🔄 Sincronizar desde X": _tab_sincronizar,
        "🏷️ Renombrar usuario": _tab_renombrar,
        "✏️ Cambiar nombre/@": _tab_cambiar_perfil,
        "🧾 Perfil completo": _tab_perfil_completo,
        "📋 Inventario": _tab_inventario,
        "📤 Exportar": _tab_exportar,
    }

    # Radio horizontal en vez de st.tabs: Streamlit ejecuta TODAS las pestanas
    # de st.tabs en cada rerun (11 consultas a BD por interaccion). Con radio
    # solo se renderiza la seleccionada (~1/N consultas).
    # Primero se elige el MODO (3 grupos de pestañas) y luego la pestaña del
    # modo; con 15 pestañas era demasiado ruido verlas todas juntas.
    # La pestaña persiste en ?tab= (ver _restaurar_pestana_desde_url): un
    # st.rerun() tras cambiar registro/tipo/sección conserva el radio vía
    # session_state, y una recarga del navegador lo restaura desde la URL.
    _restaurar_pestana_desde_url()

    # La pestaña activa puede pertenecer a otro modo (p.ej. el botón
    # "Ir a Renombrar usuario" de la pestaña Sincronizar): se cambia el modo
    # para mostrarla en vez de caer a la primera pestaña del modo actual.
    seleccion_guardada = st.session_state.get("cuentas_pestana_selector", TABS[0])
    if "cuentas_modo_selector" not in st.session_state:
        st.session_state["cuentas_modo_selector"] = _modo_de_tab(seleccion_guardada)
    elif seleccion_guardada in TABS and seleccion_guardada not in (
        MODOS_TABS.get(st.session_state["cuentas_modo_selector"]) or []
    ):
        st.session_state["cuentas_modo_selector"] = _modo_de_tab(seleccion_guardada)

    modo = st.radio(
        "📍 MODO:",
        list(MODOS_TABS),
        key="cuentas_modo_selector",
        horizontal=True,
        on_change=_on_cambio_modo,
        help=(
            "Cuentas: importar/validar/inventario · Identidad: secciones, "
            "registro, perfiles, nombres y fotos · Avanzado: anti-detección, "
            "sincronización y cambios de perfil en X."
        ),
    )
    tabs_modo = MODOS_TABS.get(modo) or TABS
    if st.session_state.get("cuentas_pestana_selector") not in tabs_modo:
        st.session_state["cuentas_pestana_selector"] = tabs_modo[0]
    seleccion = st.radio(
        "📍 SECCIÓN:",
        tabs_modo,
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
