"""Operacion ACTIVACION MASIVA: cita masiva clasica + campana por roles + 3+3+3.

Ambas pestanas de activacion incluyen el panel "📰 Contexto desde noticias
(solo trasfondo)": ahi se pegan links de prensa (uno por linea, con o sin
numeracion) y un texto de trasfondo adicional opcional; al pulsar "Extraer
contexto" el modulo `ia.contexto_noticias` scrapea las noticias, las resume
con IA (o fallback local) y muestra el preview y las fuentes usadas. Ese
resultado es TRASFONDO INVISIBLE: viaja al motor como `narrativa` (en ambas
pestanas) para orientar internamente los textos, y los prompts garantizan
que la IA NUNCA mencione la noticia, sus medios, cifras ni nombres; cada
texto se escribe con el registro y perfil de la cuenta. El TEMA que la IA SI
puede tratar (sin copiarlo) es el campo manual "Contexto de los posts con
hashtag", que en la pestana B viaja como `contexto` (la pestana A no lo usa).

Pestana A ("🎯 Cita masiva"): quote-RTs aleatorizados sobre N cuentas con
`MotorActivacion.ejecutar` (mismo formulario de siempre). Permite elegir la
SECCION (CI/IP/Libertad/Justicia o Todas: solo publican sus cuentas) y, con
"🔁 Repetir", el % minimo/maximo de cuentas que entra en cada ronda
(subconjunto aleatorio). Requiere URLs (tweet ancla).

Pestana B ("🗂️ Por roles (subcuentas)"): divide las cuentas twitter activas en
SUBCUENTAS por rol (`Cuenta.rol_activacion`, ver `core/roles.py`):
  - "cita"       -> Retweet con cita.
  - "hashtags"   -> Hashtags y menciones.
  - "comentario" -> Comentario en el tweet ancla (reply; requiere URLs).
  - "rt"         -> Retweet simple.
Permite asignar el rol a la seleccion (selector masivo de `web.operaciones.
cuentas`), repartir automaticamente entre los 4 roles, ver conteos/ejemplos y
lanzar `MotorActivacion.ejecutar_por_roles` limitado a una seccion opcional.

Modo "📝 Campaña solo de posts (sin tweet ancla)": checkbox de la pestana B
para campanas donde NO hay tweet ancla. Deshabilita las URLs y limita las
acciones a posts con hashtag/contexto (`solo_roles=["hashtags"]` tanto en
modo rol aleatorio como en rol fijo); requiere material para que la IA genere
los posts: hashtags, texto base, el contexto manual (tema) o el trasfondo de
noticias (que NUNCA se menciona).

Con "🎲 Rol aleatorio por cuenta en cada ronda" (default) el rol guardado NO
se usa como filtro: en cada ronda el motor sortea cita/hashtags/comentario/rt
por cuenta (`roles_aleatorios=True`) y cada cuenta CAMBIA de accion respecto
a su participacion anterior (si hizo RT, la siguiente puede ser cita, post con
hashtag o comentario; nunca repite mientras haya 2+ roles posibles).
`cooldown_min` evita que una misma cuenta repita accion antes de ese descanso.
Las cuentas sin registro siguen saltandose.

Con "🔁 Repetir hasta agotar el tiempo" (+ porcentajes) cada ronda usa un
SUBCONJUNTO ALEATORIO de cuentas: mas del minimo% y menos del maximo%, la
primera ronda tambien. Sin URLs no hay cita/rt/comentario; sin hashtags,
contexto manual, texto base ni trasfondo de noticias no hay posts con
hashtag.

Pestana C ("📋 Campaña 3+3+3"): por cuenta 3 posts + 3 comentarios + 3 RTs
del tweet principal (9 acciones). Genera los 9 textos con
`ia.generador_contenido.generar_pool_campana_por_cuenta` (atajo
`generar_textos_campana_3_3_3`), muestra el preview por cuenta y programa
las 9 acciones en el scheduler con `scheduler.distribucion_horaria`
(igual que "⏰ Reparto por Hora").

Anti-atasco del contexto: el panel de noticias guarda la foto de los links
(`links_crudos`) y, si los links cambian o se borran sin volver a extraer, el
contexto se IGNORA con aviso (no se borra solo). Al terminar una campana se
puede limpiar el contexto con el checkbox "🧹 Limpiar el contexto" (default
activo; aplica en el siguiente rerun y nunca toca URLs, hashtags, menciones,
cuentas ni resultados). La pestana B incluye ademas una "Pausa entre
comentarios al MISMO tweet" (anti-spam) y un aviso cuando solo hay 1 URL ancla.
"""
import threading

import streamlit as st

from web.ui import cabecera
from core.config import settings
from core.secciones import SECCIONES, etiqueta_seccion, normalizar_seccion

OPCION_SIN_ROL = "Sin rol"
OPCION_TODAS_SECCIONES = "Todas"

# Orden canonico de los roles en la UI (core/roles.ROLES_ACTIVACION).
ORDEN_ROLES = ("cita", "hashtags", "comentario", "rt")

# Icono de cada rol para las metricas (la etiqueta sale de etiqueta_rol_activacion).
ICONOS_ROL = {
    "cita": "💬",
    "hashtags": "🏷️",
    "comentario": "🗨️",
    "rt": "🔁",
}

# Guard de campana unica a nivel PROCESO: impide que el boton de otra pestana
# (u otra sesion del dashboard) lance una segunda campana mientras hay una
# corriendo; dos campanas simultaneas saturan Chrome/contenedor (`tab crashed`,
# `BlockingIOError`) y hacen mas lenta la que ya corre. Se libera SIEMPRE al
# terminar (o fallar) el lanzamiento.
_CAMPANA_ACTIVA = threading.Event()


def _campana_en_curso() -> bool:
    """True si hay una campana de activacion ejecutandose en este proceso."""
    return _CAMPANA_ACTIVA.is_set()


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
    """Reparte 'usuarios' (en orden) entre los 4 roles en bloques contiguos.

    Criterio exacto:
      - Se limpian valores vacios y se quitan duplicados conservando el primer
        orden de aparicion (se ignora un '@' inicial).
      - Con n usuarios se calcula divmod(n, 4); el resto se reparte de a uno a
        los primeros roles en el orden cita -> hashtags -> comentario -> rt.
      - Los cortes son contiguos, por lo que ningun usuario se pierde y ninguno
        queda en dos roles a la vez.

    Ejemplos: 10 -> cita 3 / hashtags 3 / comentario 2 / rt 2;
    2 -> 1/1/0/0; 0 -> 0/0/0/0.
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

    reparto = {rol: [] for rol in ORDEN_ROLES}
    n = len(limpios)
    if n == 0:
        return reparto

    base, resto = divmod(n, len(ORDEN_ROLES))
    tamanos = [
        base + (1 if i < resto else 0) for i in range(len(ORDEN_ROLES))
    ]
    inicio = 0
    for rol, tam in zip(ORDEN_ROLES, tamanos):
        reparto[rol] = limpios[inicio:inicio + tam]
        inicio += tam
    return reparto


def _conteo_por_rol(cuentas: list) -> dict:
    """Cuenta cuentas por rol normalizado; incluye la clave "" (sin rol)."""
    from core.roles import normalizar_rol_activacion

    conteo = {rol: 0 for rol in ORDEN_ROLES}
    conteo[""] = 0
    for fila in (cuentas or []):
        rol = normalizar_rol_activacion(fila.get("rol_activacion"))
        conteo[rol] = conteo.get(rol, 0) + 1
    return conteo


def _tabla_roles(cuentas: list, ejemplos: int = 8) -> list:
    """Filas (Rol, Subcuentas, Ejemplos) por rol para `st.dataframe`."""
    from core.roles import etiqueta_rol_activacion, normalizar_rol_activacion

    grupos = {rol: [] for rol in ORDEN_ROLES}
    grupos[""] = []
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


def _cuentas_objetivo(cuentas: list, usuarios: list | None) -> list:
    """Cuentas que cumplen el filtro `usuarios` (sin '@', case-insensitive).

    Mismo criterio que `MotorActivacion._obtener_cuentas_por_rol`: sin
    `usuarios` devuelve todas. Sirve para validar que el modo de rol aleatorio
    tiene al menos una cuenta que procesar aunque no tenga rol guardado.
    """
    base = list(cuentas or [])
    if not usuarios:
        return base
    deseados = {
        str(u).strip().lstrip("@").lower()
        for u in usuarios
        if str(u).strip()
    }
    return [
        f for f in base
        if str(f.get("usuario") or "").strip().lstrip("@").lower() in deseados
    ]


def _roles_objetivo(cuentas: list, usuarios: list | None,
                    solo_roles: list | None) -> set:
    """Roles (no vacios) de las cuentas que la campana realmente procesara.

    Aplica los mismos filtros que `MotorActivacion._obtener_cuentas_por_rol`:
    usuarios (sin '@' y case-insensitive) y roles permitidos. Sirve para
    validar en la UI si se necesitan URLs y si hay al menos una cuenta con rol.
    """
    from core.roles import normalizar_rol_activacion

    base = _cuentas_objetivo(cuentas, usuarios)

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


# ============================ PROGRESO EN VIVO ============================

def _formato_tiempo(segundos) -> str:
    """Formatea segundos como MM:SS (o H:MM:SS si pasa de una hora).

    Ejemplos: 0 -> "00:00", 65 -> "01:05", 3661 -> "1:01:01".
    """
    try:
        total = int(float(segundos))
    except (TypeError, ValueError, OverflowError):
        total = 0
    if total < 0:
        total = 0
    horas, resto = divmod(total, 3600)
    minutos, segs = divmod(resto, 60)
    if horas:
        return f"{horas}:{minutos:02d}:{segs:02d}"
    return f"{minutos:02d}:{segs:02d}"


def _invocar_lanzar(lanzar, callback):
    """Llama `lanzar(callback)` si acepta un parametro; si no, `lanzar()`.

    Permite que el lanzamiento reciba el callback que guarda el `total` sin
    romper a callables de cero argumentos (p. ej. en pruebas).
    """
    import inspect

    try:
        parametros = inspect.signature(lanzar).parameters
    except (TypeError, ValueError):
        parametros = {}
    if parametros:
        return lanzar(callback)
    return lanzar()


def _recortar(texto, limite: int = 80) -> str:
    """Detalle de una linea de feed: sin saltos y truncado a `limite` chars."""
    plano = " ".join(str(texto or "").split())
    if len(plano) <= limite:
        return plano
    return plano[: max(0, int(limite) - 1)].rstrip() + "…"


def _linea_evento(evento: dict) -> str:
    """Linea de feed de un evento: `✅ @usuario · Ronda N — detalle`."""
    icono = "✅" if evento.get("ok") else "❌"
    usuario = str(evento.get("usuario") or "?").strip().lstrip("@")
    linea = f"{icono} @{usuario}"
    ronda = evento.get("ronda")
    if ronda not in (None, ""):
        linea += f" · Ronda {ronda}"
    rol = str(evento.get("rol") or "").strip()
    if rol:
        linea += f" · {rol}"
    detalle = _recortar(evento.get("detalle"), 80)
    if detalle:
        linea += f" — {detalle}"
    return linea


def _lanzar_con_progreso(lanzar, motor, duracion_min: int, repetir: bool) -> dict:
    """Ejecuta `lanzar()` en un hilo y pinta contador, barra y feed en vivo.

    `lanzar` es un callable que llama al motor (bloqueante) y devuelve el
    resumen; si acepta un parametro, recibe un callback que SOLO guarda el
    total de cuentas en un dict (los callbacks del motor corren en hilos y
    nunca deben tocar `st.*`). El hilo principal refresca cada ~1s la barra,
    las metricas y el feed leyendo `motor.snapshot_progreso()`.

    Con `repetir=True` la barra avanza por tiempo (`duracion_min`); con
    `repetir=False` avanza por cuentas hechas sobre el total estimado.

    La linea de metricas incluye un indicador de VELOCIDAD calculado con
    `hechas` del snapshot y el tiempo transcurrido:
    `⚡ N acciones · r/min ≈ R/h`.

    Si `lanzar()` lanza, la excepcion se re-lanza aqui tras cerrar la barra.
    Devuelve el resumen del motor.

    Guard de campana unica: si otra campana ya esta en curso en este proceso,
    muestra `st.error` y devuelve `{}` sin lanzar hilo ni barra. El guard se
    libera SIEMPRE: en el `finally` del hilo runner (exito, error del motor o
    excepcion inesperada) y tambien si falla el arranque del hilo.
    """
    import time

    if _campana_en_curso():
        st.error(
            "⚠️ Ya hay una campaña de activación en curso. Espera a que "
            "termine antes de lanzar otra: dos campañas simultáneas saturan "
            "Chrome (tab crashed) y hacen más lenta la que ya corre."
        )
        return {}

    _CAMPANA_ACTIVA.set()

    resultado: dict = {}
    estado: dict = {"total": 0}

    def _cb_total(hechas, total, usuario, ok):
        try:
            estado["total"] = int(total or 0)
        except (TypeError, ValueError):
            pass

    def _runner():
        try:
            resultado["resumen"] = _invocar_lanzar(lanzar, _cb_total)
        except BaseException as e:  # re-lanzada en el hilo principal
            resultado["error"] = e
        finally:
            # El guard se libera SIEMPRE (exito, error del motor o excepcion
            # inesperada) antes de que el hilo principal re-lance el error.
            _CAMPANA_ACTIVA.clear()

    try:
        hilo = threading.Thread(target=_runner, daemon=True)
        inicio = time.monotonic()
        hilo.start()
    except BaseException:
        # Si el hilo no llego a arrancar, nadie mas liberaria el guard.
        _CAMPANA_ACTIVA.clear()
        raise

    barra = st.progress(0.0)
    metricas = st.empty()
    feed = st.empty()
    snapshot_fn = getattr(motor, "snapshot_progreso", None)
    limite_segundos = max(1.0, float(int(duracion_min or 0)) * 60.0)

    while hilo.is_alive():
        snap = {}
        if callable(snapshot_fn):
            try:
                snap = snapshot_fn() or {}
            except Exception:
                snap = {}
        hechas = int(snap.get("hechas") or 0)
        exitosas = int(snap.get("exitosas") or 0)
        fallidas = int(snap.get("fallidas") or 0)
        ronda_actual = int(snap.get("ronda_actual") or 1)
        transcurrido = max(0.0, time.monotonic() - inicio)
        ritmo = (hechas / transcurrido * 60.0) if transcurrido > 0 else 0.0

        if repetir:
            avance = min(1.0, transcurrido / limite_segundos)
            tiempo_txt = (
                f"⏱️ Faltan "
                f"{_formato_tiempo(max(0.0, limite_segundos - transcurrido))}"
            )
        else:
            total_estimado = int(estado.get("total") or 0)
            referencia = total_estimado if total_estimado > 0 else max(1, hechas)
            avance = min(1.0, hechas / max(1, referencia))
            tiempo_txt = f"⏳ Transcurrido {_formato_tiempo(transcurrido)}"

        barra.progress(avance, text=tiempo_txt)
        metricas.markdown(
            f"**🔄 Ronda {ronda_actual}** · ✅ {exitosas} exitosas · "
            f"❌ {fallidas} fallidas · 🧮 {hechas} hechas · "
            f"⚡ {hechas} acciones · {ritmo:.1f}/min ≈ {ritmo * 60.0:.0f}/h · "
            f"{tiempo_txt}"
        )

        eventos = snap.get("eventos") or []
        lineas = [_linea_evento(ev) for ev in eventos[-8:]]
        if lineas:
            feed.markdown("  \n".join(lineas))
        else:
            feed.markdown("⏳ Esperando las primeras cuentas…")
        time.sleep(1)

    hilo.join()
    barra.progress(1.0, text="✅ Campaña finalizada")

    if "error" in resultado:
        error = resultado["error"]
        feed.markdown(f"❌ Campaña interrumpida: {_recortar(error, 160)}")
        raise error

    resumen = resultado.get("resumen") or {}
    exitosas = int(resumen.get("exitosas") or 0)
    fallidas = int(resumen.get("fallidas") or 0)
    rondas = resumen.get("rondas")
    linea_final = f"✅ Campaña finalizada: {exitosas} ok / {fallidas} errores"
    if rondas not in (None, ""):
        linea_final += f" · {rondas} rondas"

    snap_final = {}
    if callable(snapshot_fn):
        try:
            snap_final = snapshot_fn() or {}
        except Exception:
            snap_final = {}
    eventos = snap_final.get("eventos") or []
    lineas = [_linea_evento(ev) for ev in eventos[-8:]]
    lineas.append(f"**{linea_final}**")
    feed.markdown("  \n".join(lineas))
    return resumen


def _soporta_kwarg(func, nombre: str) -> bool:
    """True si `func` acepta el kwarg `nombre` (backend viejo -> False).

    Se inspecciona la firma UNA vez antes de llamar: asi la pagina no falla si
    el motor todavia no tiene un parametro nuevo (p. ej.
    `pausa_comentario_url_seg`) sin riesgo de capturar un `TypeError` interno.
    Tambien acepta funciones que reciben `**kwargs`.
    """
    import inspect

    try:
        parametros = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if nombre in parametros:
        return True
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in parametros.values()
    )


def _lanzar_con_progreso_o_limpiar(lanzar, motor, duracion_min: int, repetir: bool,
                                   prefix: str, limpiar: bool) -> dict:
    """`_lanzar_con_progreso` + limpieza diferida del contexto al volver.

    Si `limpiar` y la campana llego a lanzarse (exito) o fallo, programa la
    limpieza del contexto de `prefix`; se aplicara en el siguiente rerun, sin
    borrar la pantalla de resultados actual. Si el guard de campana unica
    rechazo el lanzamiento (`_lanzar_con_progreso` devuelve `{}`), no limpia
    nada. La excepcion, si la hay, se re-lanza tal cual.
    """
    try:
        resultados = _lanzar_con_progreso(lanzar, motor, duracion_min, repetir)
    except BaseException:
        if limpiar:
            _programar_limpieza_contexto(prefix)
        raise
    if limpiar and resultados:
        _programar_limpieza_contexto(prefix)
    return resultados


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
    from core.roles import etiqueta_rol_activacion

    conteo = _conteo_por_rol(cuentas)
    # Una metrica por rol de ORDEN_ROLES (4) + "Sin rol".
    columnas = st.columns(len(ORDEN_ROLES) + 1)
    for col, rol in zip(columnas, ORDEN_ROLES):
        icono = ICONOS_ROL.get(rol, "")
        col.metric(f"{icono} {etiqueta_rol_activacion(rol)}".strip(), conteo.get(rol, 0))
    columnas[-1].metric("➖ Sin rol", conteo.get("", 0))
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

    if resultados.get("roles_aleatorios"):
        try:
            cooldown = float(resultados.get("cooldown_min") or 0)
        except (TypeError, ValueError):
            cooldown = 0.0
        st.caption(
            f"🎲 Roles sorteados por cuenta en cada ronda · "
            f"descanso por cuenta: {cooldown:g} min"
        )

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


# ============================ UI: CONTEXTO DE NOTICIAS ============================

# Claves de los campos de contexto MANUAL (tema/trasfondo escrito a mano) por
# pestana. La limpieza post-campana los vacia, pero NUNCA toca URLs, hashtags,
# menciones, cuentas ni resultados de la campana.
_CLAVES_CONTEXTO_MANUAL = {
    # Pestana A ("act"): hoy no tiene campo manual (solo el texto base de la
    # cita y el panel de noticias); `pop` defensivo si algun dia se agrega.
    "act": ("act_contexto",),
    # Pestana B: "Contexto de los posts con hashtag" (tema que la IA si opina).
    "act_roles": ("act_roles_contexto",),
}


def _programar_limpieza_contexto(prefix: str) -> None:
    """Programa (diferido) la limpieza del contexto de la pestana `prefix`.

    Se llama al terminar una campana (con el checkbox "🧹 Limpiar el contexto"
    marcado). NO borra nada en este run (los widgets ya estan instanciados): la
    limpieza se aplica en el siguiente rerun desde
    `_aplicar_limpieza_contexto_pendiente`.
    """
    st.session_state[f"{prefix}_noticias_limpiar_pendiente"] = True
    st.session_state[f"{prefix}_contexto_manual_limpiar_pendiente"] = True


def _aplicar_limpieza_contexto_pendiente(prefix: str) -> None:
    """Aplica la limpieza programada ANTES de crear los widgets de la pestana.

    Consume los flags (una sola vez por run) y hace `pop` de:
      - noticias: resultado extraido, links, texto extra y preview;
      - contexto manual: las keys de `_CLAVES_CONTEXTO_MANUAL[prefix]`.

    Nunca modifica el estado de un widget ya instanciado (se llama al inicio del
    tab/panel) y nunca toca URLs, hashtags, menciones, cuentas ni resultados.
    """
    limpiar_noticias = bool(
        st.session_state.pop(f"{prefix}_noticias_limpiar_pendiente", False)
    )
    limpiar_manual = bool(
        st.session_state.pop(f"{prefix}_contexto_manual_limpiar_pendiente", False)
    )
    if limpiar_noticias:
        for sufijo in (
            "noticias_resultado",
            "noticias_links",
            "noticias_texto",
            "noticias_contexto_ver",
        ):
            st.session_state.pop(f"{prefix}_{sufijo}", None)
    if limpiar_manual:
        for clave in _CLAVES_CONTEXTO_MANUAL.get(prefix, ()):
            st.session_state.pop(clave, None)


def _links_desactualizados(resultado: dict, links_raw: str,
                           links_actuales: list) -> bool:
    """True si el contexto guardado NO corresponde a los links actuales.

    Reglas (nunca lanza):
      - Campo de links vacio o sin URLs validas -> el contexto se ignora.
      - Con `links_crudos` (foto del text_area al extraer): se comparan las
        listas normalizadas (`normalizar_links`); si difieren, el contexto quedo
        atrasado respecto de lo que el usuario tiene ahora.
      - Resultados viejos sin `links_crudos`: se cae a `resultado["links"]` y,
        si tampoco existe, se considera desactualizado (no se puede verificar).
    """
    from ia.contexto_noticias import normalizar_links

    if not links_actuales:
        return True
    crudos_guardados = resultado.get("links_crudos")
    if crudos_guardados is None:
        guardados = resultado.get("links")
        if not guardados:
            return True
        return [str(x) for x in guardados] != [str(x) for x in links_actuales]
    return normalizar_links(crudos_guardados) != [str(x) for x in links_actuales]


def _panel_contexto_noticias(prefix: str) -> dict:
    """Panel "📰 Contexto desde noticias (solo trasfondo)" de una pestana.

    El resultado es TRASFONDO INVISIBLE: se pasa al motor como `narrativa`
    (y NO como `contexto`), de modo que la IA lo usa solo como referencia
    interna y NUNCA debe mencionar la noticia, sus medios, cifras ni nombres;
    cada texto se escribe con el registro y perfil de la cuenta. El widget de
    preview si muestra el contexto extraido, pero es para revision humana.

    Renderiza:
      - text_area de links (una por linea, con o sin numeracion) y text_area
        de texto/trasfondo adicional opcional.
      - boton "📰 Extraer contexto de las noticias": llama (bajo spinner y SOLO
        al pulsarlo) a `ia.contexto_noticias.generar_contexto_desde_links` con
        `max_caracteres=1800` y guarda el dict completo en
        `st.session_state[f"{prefix}_noticias_resultado"]`.
      - boton "🗑️ Limpiar contexto": programa la limpieza diferida (resultado,
        links, texto extra y preview) y hace `st.rerun()`.
      - si ya hay resultado, lo muestra en cada rerun (sin volver a raspar):
        links usados/duplicados, si fue "Resumen IA" o "Resumen local (sin IA)",
        los errores (max. 3) y un expander "Ver fuentes y contexto".
      - ANTI-ATASCO: el resultado guarda `links_crudos` (foto exacta del
        text_area al extraer); si los links actuales (normalizados) ya no
        coinciden —o el campo quedo vacio— el contexto se IGNORA con un aviso
        (sin borrarlo) hasta que se vuelva a extraer o limpiar.

    Devuelve `{"contexto", "links", "ok", "fuentes", "texto_extra"}` con
    valores vacios si todavia no hay resultado o si quedo desactualizado.
    Nunca raspa fuera del boton.
    """
    from ia.contexto_noticias import generar_contexto_desde_links, normalizar_links

    # Limpieza diferida (boton "Limpiar contexto" o fin de campana): SIEMPRE
    # antes de crear los widgets del panel.
    _aplicar_limpieza_contexto_pendiente(prefix)

    st.markdown("#### 📰 Contexto desde noticias (solo trasfondo)")
    st.caption(
        "Los links son solo trasfondo: la IA los usa como referencia interna "
        "y NO debe mencionar la noticia, medios, cifras ni nombres; cada texto "
        "se escribe con el registro y perfil de la cuenta."
    )
    links_raw = st.text_area(
        "Links de noticias (una por línea)",
        height=120,
        key=f"{prefix}_noticias_links",
        help=(
            "Los links son solo trasfondo: la IA los usa como referencia "
            "interna y NO debe mencionar la noticia, medios, cifras ni "
            "nombres; cada texto se escribe con el registro y perfil de la "
            "cuenta."
        ),
    )
    texto_extra = st.text_area(
        "Texto/contexto adicional (opcional)",
        height=80,
        key=f"{prefix}_noticias_texto",
        help=(
            "Se suma como trasfondo interno para orientar los textos; la IA "
            "no debe mencionarlo literalmente. El tema que la IA sí puede "
            "tratar es el campo 'Contexto de los posts con hashtag'."
        ),
    )

    col_extraer, col_limpiar = st.columns(2)
    with col_extraer:
        extraer = st.button(
            "📰 Extraer contexto de las noticias",
            key=f"{prefix}_noticias_btn",
        )
    with col_limpiar:
        limpiar = st.button(
            "🗑️ Limpiar contexto",
            key=f"{prefix}_noticias_clear",
        )

    clave_resultado = f"{prefix}_noticias_resultado"
    if limpiar:
        # Limpieza diferida: los text_area ya estan instanciados en este run,
        # asi que se programan para el siguiente (ver inicio del panel).
        st.session_state.pop(clave_resultado, None)
        st.session_state[f"{prefix}_noticias_limpiar_pendiente"] = True
        st.rerun()

    if extraer:
        with st.spinner("Leyendo las noticias y resumiendo el contexto…"):
            resultado = generar_contexto_desde_links(
                links_raw,
                texto_extra=texto_extra,
                max_caracteres=1800,
            )
        if not isinstance(resultado, dict):
            resultado = {"ok": False, "contexto": ""}
        # Foto EXACTA del text_area: permite detectar despues si el contexto
        # quedo atrasado (links cambiados o borrados sin volver a extraer).
        resultado["links_crudos"] = str(links_raw or "")
        st.session_state[clave_resultado] = resultado

    links_actuales = normalizar_links(links_raw)
    resultado = st.session_state.get(clave_resultado) or {}
    if not resultado:
        return {
            "contexto": "",
            "links": links_actuales,
            "ok": False,
            "fuentes": [],
            "texto_extra": "",
        }

    if _links_desactualizados(resultado, links_raw, links_actuales):
        # El contexto guardado pertenece a otros links (o ya no hay links): se
        # ignora SIN borrarlo, para que el usuario decida re-extraer o limpiar.
        st.warning(
            "⚠️ El contexto extraído ya no corresponde a los links actuales "
            "(o borraste los links). Se ignora hasta que vuelvas a pulsar "
            "'📰 Extraer contexto de las noticias' o '🗑️ Limpiar contexto'."
        )
        return {
            "contexto": "",
            "links": links_actuales,
            "ok": False,
            "fuentes": [],
            "texto_extra": "",
        }

    try:
        links_usados = int(resultado.get("links_usados") or 0)
    except (TypeError, ValueError):
        links_usados = 0
    try:
        links_duplicados = int(resultado.get("links_duplicados") or 0)
    except (TypeError, ValueError):
        links_duplicados = 0
    detalle = f"{links_usados} fuente(s)"
    if links_duplicados:
        detalle += f" · {links_duplicados} link(s) duplicado(s)"
    detalle += (
        " · Resumen IA" if resultado.get("resumen_ia")
        else " · Resumen local (sin IA)"
    )
    if resultado.get("ok"):
        st.success(f"📰 Contexto extraído: {detalle}.")
    else:
        motivo = str(resultado.get("ultimo_error") or "").strip()
        st.warning(
            f"📰 No se pudo construir el contexto: {detalle}."
            + (f" {motivo}" if motivo else "")
        )

    errores = [
        str(x).strip() for x in (resultado.get("errores") or []) if str(x).strip()
    ]
    if errores:
        st.warning("⚠️ Fuentes con error: " + " · ".join(errores[:3]))

    with st.expander("Ver fuentes y contexto", expanded=False):
        fuentes = resultado.get("fuentes") or []
        if fuentes:
            for fuente in fuentes:
                titulo = str(fuente.get("titulo") or "").strip() or "(sin título)"
                url = str(fuente.get("url") or "").strip()
                st.markdown(f"- [{titulo}]({url})" if url else f"- {titulo}")
        else:
            st.caption("No hay fuentes extraídas.")
        contexto_guardado = str(resultado.get("contexto") or "").strip()
        if contexto_guardado:
            clave_ver = f"{prefix}_noticias_contexto_ver"
            # Se fija el estado ANTES de crear el widget para que el preview
            # muestre siempre el ultimo contexto (widget de solo lectura).
            st.session_state[clave_ver] = contexto_guardado
            st.text_area(
                "Contexto",
                height=200,
                disabled=True,
                key=clave_ver,
            )
        else:
            st.caption("Sin contexto.")
        ultimo_error = str(resultado.get("ultimo_error") or "").strip()
        if ultimo_error:
            st.caption(f"Último error: {ultimo_error}")

    return {
        "contexto": str(resultado.get("contexto") or "").strip(),
        "links": links_actuales,
        "ok": bool(resultado.get("ok")),
        "fuentes": list(resultado.get("fuentes") or []),
        "texto_extra": str(texto_extra or "").strip(),
    }


# ============================ PESTANAS ============================

def _cita_masiva():
    """Pestana A: quote-RTs masivos con variaciones (formulario original)."""
    # Limpieza diferida del contexto (fin de campana / "Limpiar contexto"):
    # SIEMPRE antes de crear cualquier widget de la pestana.
    _aplicar_limpieza_contexto_pendiente("act")
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

    panel_noticias = _panel_contexto_noticias("act")

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
                "Cada navegador ejecuta una cuenta a la vez. Recomendado: "
                "4-6 en Railway (configurable con la variable MAX_BROWSERS)."
            ),
        )

    st.caption(
        "🖥️ Recomendado: 4-6 navegadores en Railway (cada Chrome ~300-500 MB). "
        "Más navegadores solo con RAM/GB de sobra; subirlo de más provoca "
        "`tab crashed` y la campaña va más lento. No lances dos campañas a la vez."
    )

    opciones_seccion = [OPCION_TODAS_SECCIONES] + [
        etiqueta_seccion(clave) for clave in SECCIONES
    ]
    seccion_opcion = st.selectbox(
        "Sección",
        opciones_seccion,
        key="act_seccion",
        help=(
            "Solo publican las cuentas de esa sección. «Todas» no filtra por "
            "sección; las cuentas sin asignar también entran con «Todas»."
        ),
    )
    secciones_param = (
        None
        if seccion_opcion == OPCION_TODAS_SECCIONES
        else [normalizar_seccion(seccion_opcion)]
    )
    cuentas_activas = _cargar_cuentas_con_roles()
    if secciones_param:
        n_seccion = sum(
            1 for f in cuentas_activas if f.get("seccion") == secciones_param[0]
        )
        st.caption(
            f"🚦 Publicarán las cuentas de **{etiqueta_seccion(secciones_param[0])}**: "
            f"{n_seccion} de {len(cuentas_activas)} activas."
        )
    else:
        st.caption(
            f"🚦 Publicarán todas las cuentas activas: {len(cuentas_activas)}."
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

    pct_min, pct_max = 40, 90
    if repetir:
        ayuda_pct = (
            "Cada ronda usa un subconjunto aleatorio de cuentas: más del mín% "
            "y menos del máx% (ej. 15 cuentas -> entre 7 y 13). La primera "
            "ronda también."
        )
        col_pmin, col_pmax = st.columns(2)
        with col_pmin:
            pct_min = st.number_input(
                "Mín % de cuentas por ronda",
                min_value=1, max_value=99, value=40, step=5,
                key="act_pct_min",
                help=ayuda_pct,
            )
        with col_pmax:
            pct_max = st.number_input(
                "Máx % de cuentas por ronda",
                min_value=1, max_value=99, value=90, step=5,
                key="act_pct_max",
                help=ayuda_pct,
            )

    limpiar_contexto_al_terminar = st.checkbox(
        "🧹 Limpiar el contexto (noticias/tema) al terminar",
        value=True,
        key="act_limpiar_contexto",
        help=(
            "Al terminar la campaña borra el resultado/links/texto de noticias "
            "y el tema manual; no toca URLs, hashtags, menciones, cuentas ni "
            "resultados de la campaña."
        ),
    )

    if st.button("🎯 Lanzar activación", type="primary", key="btn_act"):
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        if not urls:
            st.warning("Pega al menos una URL objetivo.")
            st.info(
                "Para campañas sin tweet ancla usa la pestaña «🗂️ Por roles "
                "(subcuentas)» con «Campaña solo de posts»."
            )
            return
        if not texto_base:
            st.warning("Escribe el texto base de la cita.")
            return
        if repetir and not (1 <= int(pct_min) < int(pct_max) <= 99):
            st.warning(
                "Revisa los porcentajes por ronda: el mínimo debe ser menor "
                "que el máximo y el máximo no puede pasar de 99%."
            )
            return

        from activaciones.motor import MotorActivacion

        motor = MotorActivacion(max_concurrente=int(navegadores))
        resultados = _lanzar_con_progreso_o_limpiar(
            lambda cb: motor.ejecutar(
                urls=urls,
                texto_base=texto_base,
                narrativa=panel_noticias["contexto"],
                cantidad_cuentas=(
                    None if todas_cuentas else (int(cantidad) if cantidad > 0 else None)
                ),
                grupo=grupo.strip() or None,
                dar_like=dar_like,
                duracion_min=int(duracion_min),
                cohortes=int(cohortes),
                callback=cb,
                hashtags=hashtags,
                repetir=bool(repetir),
                solo_con_registro=bool(todas_cuentas),
                secciones=secciones_param,
                porcentaje_min_ronda=int(pct_min),
                porcentaje_max_ronda=int(pct_max),
            ),
            motor,
            duracion_min=int(duracion_min),
            repetir=bool(repetir),
            prefix="act",
            limpiar=bool(limpiar_contexto_al_terminar),
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

    # Limpieza diferida del contexto (fin de campana / "Limpiar contexto"):
    # SIEMPRE antes de crear cualquier widget de la pestana (incluido
    # `act_roles_contexto`).
    _aplicar_limpieza_contexto_pendiente("act_roles")

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
        "🎲 Repartir automáticamente entre los 4 roles",
        key="btn_act_roles_tercios",
        help=(
            "En orden alfabético: reparte las cuentas seleccionadas entre "
            "Retweet con cita, Hashtags y menciones, Comentario en el tweet "
            "ancla y Retweet simple."
        ),
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
            st.warning(
                "Selecciona al menos una cuenta para repartir entre los 4 roles."
            )
        else:
            reparto = _repartir_tercios(usuarios_sel)
            resumen = []
            for rol in ORDEN_ROLES:
                n = _actualizar_roles(reparto[rol], rol)
                resumen.append(f"{etiqueta_rol_activacion(rol)}: {n}")
            st.success("🎲 Reparto entre los 4 roles → " + " · ".join(resumen))
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
    opciones_seccion = [OPCION_TODAS_SECCIONES] + [
        etiqueta_seccion(clave) for clave in SECCIONES
    ]
    seccion_opcion = st.selectbox(
        "Sección",
        opciones_seccion,
        key="act_roles_seccion",
        help=(
            "Solo se usan las cuentas de esa sección. «Todas» no filtra por "
            "sección; las cuentas sin asignar también entran con «Todas»."
        ),
    )
    secciones_param = (
        None
        if seccion_opcion == OPCION_TODAS_SECCIONES
        else [normalizar_seccion(seccion_opcion)]
    )
    if secciones_param:
        n_seccion = sum(
            1 for f in cuentas if f.get("seccion") == secciones_param[0]
        )
        st.caption(
            f"🚦 La campaña usará las cuentas de "
            f"**{etiqueta_seccion(secciones_param[0])}**: "
            f"{n_seccion} de {len(cuentas)} activas."
        )
    else:
        st.caption(
            f"🚦 La campaña usará todas las cuentas activas: {len(cuentas)}."
        )

    # El checkbox "📝 Campaña solo de posts" se renderiza mas abajo (junto a
    # "Rol aleatorio"); leemos su valor de session_state para poder deshabilitar
    # las URLs en el mismo rerun en que se marca.
    sin_ancla = bool(st.session_state.get("act_roles_sin_ancla", False))
    urls_text = st.text_area(
        "URLs objetivo (una por línea; las usan 'cita', 'comentario' y 'rt')",
        height=100,
        key="act_roles_urls",
        disabled=sin_ancla,
    )
    if sin_ancla:
        st.caption(
            "🚫 Sin tweet ancla: las URLs están deshabilitadas y no se usan; "
            "todas las acciones serán posts con hashtag/contexto."
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

    panel_noticias = _panel_contexto_noticias("act_roles")

    col_dur, col_coh, col_nav, col_cool = st.columns(4)
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
                "Cada navegador ejecuta una cuenta a la vez. Recomendado: "
                "4-6 en Railway (configurable con la variable MAX_BROWSERS)."
            ),
        )
    with col_cool:
        cooldown_min = st.number_input(
            "Descanso por cuenta (min)",
            min_value=0, max_value=60, value=4, step=1,
            key="act_roles_cooldown",
            help=(
                "Tiempo mínimo entre dos acciones de la MISMA cuenta (protege "
                "de spam). Con muchas cuentas casi no afecta la velocidad; "
                "0 = sin descanso."
            ),
        )

    pausa_comentario = st.number_input(
        "Pausa entre comentarios al MISMO tweet (s)",
        min_value=0,
        max_value=300,
        value=15,
        step=5,
        key="act_roles_pausa_comentario",
        help=(
            "X marca como probable spam los comentarios masivos al mismo "
            "tweet. Esta pausa espacia las respuestas a la MISMA URL; con "
            "varias URLs ancla casi no afecta la velocidad."
        ),
    )

    st.caption(
        "⚡ Rendimiento estimado: con ~45-60s por acción, 4-6 navegadores "
        "logran **~200-260 publicaciones/hora**; ajusta navegadores y "
        "descanso según tus proxies. 🖥️ Recomendado: 4-6 navegadores en "
        "Railway (cada Chrome ~300-500 MB). Más navegadores solo con RAM/GB "
        "de sobra; subirlo de más provoca `tab crashed` y la campaña va más "
        "lento. No lances dos campañas a la vez."
    )

    roles_aleatorios = st.checkbox(
        "🎲 Rol aleatorio por cuenta en cada ronda",
        value=True,
        key="act_roles_aleatorio",
        help=(
            "La IA sortea la acción de cada cuenta en cada ronda: RT con cita, "
            "post con hashtags, comentario en el tweet ancla o RT simple. Una "
            "cuenta que participa en rondas seguidas cambia de acción respecto "
            "a su participación anterior (si hizo RT, la siguiente puede ser "
            "cita, post con hashtag o comentario). Los inputs definen qué roles "
            "entran: sin URLs no hay cita/rt/comentario; sin hashtags ni "
            "contexto no hay posts con hashtag. Al marcarlo se ignora el rol "
            "guardado y se desactiva el filtro «Solo cuentas con rol»."
        ),
    )

    sin_ancla = st.checkbox(
        "📝 Campaña solo de posts (sin tweet ancla)",
        value=False,
        key="act_roles_sin_ancla",
        help=(
            "No hay tweet que retwittear/citar/comentar: todas las cuentas "
            "publican posts con el contexto manual (tema), hashtags, texto "
            "base o el trasfondo de noticias (que la IA no menciona)."
        ),
    )
    if sin_ancla:
        st.caption(
            "🚫📌 Campaña solo de posts: no se usan URLs; todas las acciones "
            "serán posts con hashtag/contexto."
        )

    col_like, col_solo, col_rep = st.columns(3)
    with col_like:
        dar_like = st.checkbox(
            "Dar like también", value=False, key="act_roles_like"
        )
    with col_solo:
        solo_con_rol = st.checkbox(
            "Solo cuentas con rol",
            value=True,
            key="act_roles_solo_rol",
            disabled=roles_aleatorios,
            help=(
                "Ignorado con «Rol aleatorio por cuenta»: el rol guardado no "
                "filtra; todas las cuentas con registro entran al sorteo."
                if roles_aleatorios
                else "Limita la campaña a las cuentas que ya tienen rol."
            ),
        )
        if roles_aleatorios:
            st.caption(
                "🎲 Deshabilitado: el rol se sortea por cuenta en cada ronda, "
                "sin usar el rol guardado."
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

    pct_min, pct_max = 40, 90
    if repetir:
        ayuda_pct = (
            "Cada ronda usa un subconjunto aleatorio de cuentas: más del mín% "
            "y menos del máx% (ej. 15 cuentas -> entre 7 y 13). La primera "
            "ronda también."
        )
        col_pmin, col_pmax = st.columns(2)
        with col_pmin:
            pct_min = st.number_input(
                "Mín % de cuentas por ronda",
                min_value=1, max_value=99, value=40, step=5,
                key="act_roles_pct_min",
                help=ayuda_pct,
            )
        with col_pmax:
            pct_max = st.number_input(
                "Máx % de cuentas por ronda",
                min_value=1, max_value=99, value=90, step=5,
                key="act_roles_pct_max",
                help=ayuda_pct,
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
            "cuentas y se usan todas las activas (con rol aleatorio, sin "
            "filtrar por rol guardado). Solo publican las que tengan registro "
            "definido (político/activista/ciudadanía); las cuentas sin "
            "registro no hacen nada."
        )

    # Filtros efectivos de la campana: los comparten el aviso anti-spam y el
    # boton de lanzamiento.
    usuarios_param = None if todas_cuentas else (usuarios_sel or None)
    base_objetivo = (
        [f for f in cuentas if f.get("tipo_cuenta")]
        if todas_cuentas
        else cuentas
    )
    if secciones_param:
        base_objetivo = [
            f for f in base_objetivo if f.get("seccion") == secciones_param[0]
        ]

    # Anti-spam: con UNA sola URL ancla y comentarios en juego, TODOS los
    # comentarios caen en el mismo tweet y X los agrupa como spam.
    urls_previas = (
        [] if sin_ancla else [u.strip() for u in urls_text.splitlines() if u.strip()]
    )
    comentario_posible = False
    if not sin_ancla:
        if roles_aleatorios:
            comentario_posible = True
        else:
            comentario_posible = "comentario" in _roles_objetivo(
                base_objetivo,
                usuarios_param,
                list(ORDEN_ROLES) if solo_con_rol else None,
            )
    if len(urls_previas) == 1 and comentario_posible:
        st.warning(
            "⚠️ Con una sola URL ancla todos los comentarios van al mismo "
            "tweet y X los agrupa como 'Probable spam'. Pega 2-5 URLs o sube "
            "la pausa."
        )

    limpiar_contexto_al_terminar = st.checkbox(
        "🧹 Limpiar el contexto (noticias/tema) al terminar",
        value=True,
        key="act_roles_limpiar_contexto",
        help=(
            "Al terminar la campaña borra el resultado/links/texto de noticias "
            "y el contexto manual de los posts; no toca URLs, hashtags, "
            "menciones, cuentas ni resultados de la campaña."
        ),
    )

    if st.button(
        "🗂️ Lanzar campaña por roles",
        type="primary",
        key="btn_act_roles_launch",
        help=(
            "Con «Rol aleatorio por cuenta» (recomendado) cada cuenta recibe "
            "una acción distinta sorteada en cada ronda (cita, hashtag, "
            "comentario o rt); desmárcalo para usar el rol guardado de cada "
            "subcuenta."
        ),
    ):
        urls = (
            []
            if sin_ancla
            else [u.strip() for u in urls_text.splitlines() if u.strip()]
        )
        # Separacion estricta: el campo manual es el TEMA (`contexto`, la IA
        # SI puede opinar de el) y las noticias raspadas son TRASFONDO
        # INVISIBLE (`narrativa`, la IA NUNCA debe mencionarlas ni copiarlas).
        contexto_manual = str(contexto or "").strip()
        narrativa_noticias = str(panel_noticias["contexto"] or "").strip()
        if repetir and not (1 <= int(pct_min) < int(pct_max) <= 99):
            st.warning(
                "Revisa los porcentajes por ronda: el mínimo debe ser menor "
                "que el máximo y el máximo no puede pasar de 99%."
            )
            return
        if sin_ancla:
            # Campaña sin tweet ancla: SOLO posts con hashtag/contexto, tanto
            # en modo rol aleatorio como en modo rol fijo.
            solo_roles_param = ["hashtags"]
        else:
            solo_roles_param = (
                None
                if roles_aleatorios
                else (list(ORDEN_ROLES) if solo_con_rol else None)
            )

        material_posts = (
            str(hashtags or "").strip()
            or contexto_manual
            or str(texto_base or "").strip()
            or narrativa_noticias
        )

        if sin_ancla:
            if roles_aleatorios:
                if not _cuentas_objetivo(base_objetivo, usuarios_param):
                    st.warning(
                        "No hay cuentas que cumplan la selección (sección, "
                        "selector o registro). Revisa los filtros."
                    )
                    return
            else:
                roles_objetivo = _roles_objetivo(
                    base_objetivo, usuarios_param, solo_roles_param
                )
                if "hashtags" not in roles_objetivo:
                    st.warning(
                        "No hay cuentas con rol 'Hashtags y menciones' que "
                        "cumplan la selección. Asigna ese rol o activa «🎲 Rol "
                        "aleatorio por cuenta en cada ronda»."
                    )
                    return
            if not material_posts:
                st.warning(
                    "Sin tweet ancla necesitas al menos hashtags, texto base, "
                    "el contexto de los posts o links de noticias (trasfondo) "
                    "para que la IA genere los posts."
                )
                return
        elif roles_aleatorios:
            if not _cuentas_objetivo(base_objetivo, usuarios_param):
                st.warning(
                    "No hay cuentas que cumplan la selección (sección, "
                    "selector o registro). Revisa los filtros."
                )
                return
            if not urls and not material_posts:
                st.warning(
                    "Pega al menos una URL objetivo, escribe hashtags, texto "
                    "base o el contexto de los posts, o extrae las noticias "
                    "de trasfondo: sin URLs el rol aleatorio no puede hacer "
                    "cita/rt/comentario y solo quedan posts con hashtag."
                )
                return
        else:
            roles_objetivo = _roles_objetivo(
                base_objetivo, usuarios_param, solo_roles_param
            )
            if not roles_objetivo:
                st.warning(
                    "No hay cuentas con rol que cumplan la selección. Asigna "
                    "roles en «🏷️ Asignar roles» o revisa los filtros."
                )
                return
            if not urls and (roles_objetivo & {"cita", "comentario", "rt"}):
                st.warning(
                    "Pega al menos una URL objetivo: la selección incluye "
                    "cuentas de 'Retweet con cita', 'Comentario en el tweet "
                    "ancla' y/o 'Retweet simple'."
                )
                return

        from activaciones.motor import MotorActivacion

        motor = MotorActivacion(max_concurrente=int(navegadores))
        # El motor viejo no acepta `pausa_comentario_url_seg`: se comprueba la
        # firma para pasar el kwarg solo si existe (la pagina nunca falla).
        pausa_kwargs = {}
        if _soporta_kwarg(motor.ejecutar_por_roles, "pausa_comentario_url_seg"):
            pausa_kwargs["pausa_comentario_url_seg"] = int(pausa_comentario)
        resultados = _lanzar_con_progreso_o_limpiar(
            lambda cb: motor.ejecutar_por_roles(
                urls=urls,
                texto_base=texto_base,
                hashtags=hashtags,
                menciones=menciones,
                dar_like=dar_like,
                duracion_min=int(duracion_min),
                cohortes=int(cohortes),
                usuarios=usuarios_param,
                solo_roles=solo_roles_param,
                callback=cb,
                contexto=contexto_manual,
                narrativa=narrativa_noticias,
                repetir=bool(repetir),
                solo_con_registro=bool(todas_cuentas),
                roles_aleatorios=bool(roles_aleatorios),
                cooldown_min=float(cooldown_min),
                secciones=secciones_param,
                porcentaje_min_ronda=int(pct_min),
                porcentaje_max_ronda=int(pct_max),
                **pausa_kwargs,
            ),
            motor,
            duracion_min=int(duracion_min),
            repetir=bool(repetir),
            prefix="act_roles",
            limpiar=bool(limpiar_contexto_al_terminar),
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
