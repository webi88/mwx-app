"""Operacion CHANGE.ORG: REPORTES (ataque masivo de reportes de politicas).

Antes esta pagina era un formulario de FIRMAS con `ChangeOrgBot` y un
monkeypatch de `builtins.input()` para las pausas de reconexion de IP. Eso se
elimino por completo: ahora lanza un ATAQUE DE REPORTES masivo en Change.org
con identidades generadas por IA (nombre, apellido, email, codigo postal y la
queja redactada por la IA a partir del motivo del operador).

Backend congelado (`cuentas/change_org.py`), importado PEREZOSAMENTE dentro
del handler del boton para que la pagina cargue sin Chrome:

    ejecutar_campana_reportes(
        url_peticion=..., contexto=..., cantidad=..., max_workers=...,
        usar_proxies=..., pais_proxy=..., guardar_identidades=...,
        headless=..., cancelar=<threading.Event>, callback=cb,
    )

El callback llega en los hilos worker del backend ("inicio" + un evento por
reporte) y SOLO muta el registro de campanas a nivel modulo (`_CAMPANAS` +
`_CAMPANAS_LOCK`): jamas toca `st.*`. El panel `_render_proceso_activo()` se
pinta SIEMPRE al inicio de `render()` (no en `st.session_state`), asi el
avance/log sobrevive a un rerun o a navegar a otra operacion, con
`st.fragment(run_every=1s)` cuando existe y fallback `time.sleep(1)+st.rerun()`
solo mientras el ataque corre.

La granja de identidades (`CuentaChange`) se muestra en un expander con el
total de filas y las ultimas 20, SIEMPRE envuelta en try/except: si la tabla o
la BD no existen, la pagina sigue funcionando con un caption informativo.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import streamlit as st

from web.ui import cabecera

# ============================ REGISTRO DE CAMPANAS ============================
# Las campanas de reportes de ESTE proceso (en curso y terminadas). La UI lee
# el estado desde aqui y no desde `st.session_state`, por lo que el panel
# reaparece aunque el usuario recargue o cambie de operacion. Los callbacks del
# backend corren en hilos worker: SIEMPRE mutan el registro bajo lock y NUNCA
# llaman a `st.*` (Streamlit no es thread-safe).
_CAMPANAS: dict = {}
_CAMPANAS_LOCK = threading.RLock()

# Estados que cuentan como "ataque en curso" (bloquean lanzar otro).
_ESTADOS_ACTIVOS = ("en_curso", "deteniendo")

# Maximo de lineas del log en memoria (anti-fuga en campanas de 100 reportes).
LOG_MAX = 200

# Etiqueta del pais "sin filtro" del selector de proxies.
PAIS_TODAS = "Todas"


def _entero(valor, default: int = 0) -> int:
    """int() tolerante a None/str/basura (nunca lanza)."""
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _en_contexto_streamlit() -> bool:
    """True solo dentro de un script de Streamlit (AppTest incluido).

    En modo bare (tests que llaman helpers sin runtime) es False, de modo que
    el lanzamiento no intenta `st.rerun()`."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def _campana_actual():
    """Entrada de la campana mas reciente (en curso o terminada); None si no hay."""
    with _CAMPANAS_LOCK:
        if not _CAMPANAS:
            return None
        return next(reversed(_CAMPANAS.values()))


def _campana_en_curso():
    """Entrada activa (en_curso/deteniendo) o None. Guard de campana unica."""
    with _CAMPANAS_LOCK:
        for entrada in reversed(list(_CAMPANAS.values())):
            if str(entrada.get("estado") or "") in _ESTADOS_ACTIVOS:
                return entrada
    return None


def _registrar_campana(hilo=None, evento=None, parametros=None) -> str:
    """Registra una campana nueva en estado `en_curso` y devuelve su id.

    Contrato de la entrada: id, hilo, evento (threading.Event de paro),
    estado, hechas, total, enviados, fallidos, identidades, log (lista),
    resumen y error (mas inicio/fin/parametros internos)."""
    id_campana = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    with _CAMPANAS_LOCK:
        _CAMPANAS[id_campana] = {
            "id": id_campana,
            "hilo": hilo,
            "evento": evento if evento is not None else threading.Event(),
            "estado": "en_curso",
            "hechas": 0,
            "total": 0,
            "enviados": 0,
            "fallidos": 0,
            "identidades": 0,
            "log": [],
            "resumen": None,
            "error": "",
            "inicio": time.time(),
            "fin": None,
            "parametros": dict(parametros or {}),
        }
    return id_campana


def _anotar_evento(id_campana, evento) -> None:
    """Procesa UN evento del callback del backend (puro: jamas toca `st.*`).

    - `{"tipo":"inicio","total":N}`: fija el total planificado.
    - `{"tipo":"reporte",...}`: incrementa hechas/enviados/fallidos, agrega la
      linea al log (`✅ Reporte enviado por {email}` si ok; `❌ {email} —
      {detalle}` si falla) y cuenta las identidades recibidas. El log se
      recorta a `LOG_MAX` lineas.
    """
    if not isinstance(evento, dict) or not id_campana:
        return
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        tipo = str(evento.get("tipo") or "")
        if tipo == "inicio":
            entrada["total"] = _entero(
                evento.get("total"), _entero(entrada.get("total"))
            )
            return
        if tipo != "reporte":
            return

        email = str(evento.get("email") or "").strip() or "?"
        ok = bool(evento.get("ok"))
        detalle = str(evento.get("detalle") or "").strip()
        entrada["hechas"] = _entero(
            evento.get("hechas"), _entero(entrada.get("hechas")) + 1
        )
        total = _entero(evento.get("total"))
        if total:
            entrada["total"] = total
        if ok:
            entrada["enviados"] = _entero(entrada.get("enviados")) + 1
            linea = f"✅ Reporte enviado por {email}"
        else:
            entrada["fallidos"] = _entero(entrada.get("fallidos")) + 1
            linea = f"❌ {email} — {detalle}"
        identidad = evento.get("identidad")
        if isinstance(identidad, dict) and identidad:
            entrada["identidades"] = _entero(entrada.get("identidades")) + 1
        log = entrada.setdefault("log", [])
        log.append(linea)
        if len(log) > LOG_MAX:
            del log[:-LOG_MAX]


def _finalizar_campana(id_campana, resumen=None, error=None) -> None:
    """Cierra la campana: estado final, resumen/error y fin (nunca lanza)."""
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is None:
            return
        entrada["fin"] = time.time()
        resumen_dict = resumen if isinstance(resumen, dict) else {}
        # Sin excepcion, el backend puede reportar un error interno en su
        # resumen ("error"): se guarda para que el panel lo muestre como error.
        entrada["error"] = (
            str(error)
            if error is not None
            else str(resumen_dict.get("error") or "")
        )
        if error is not None:
            entrada["estado"] = "error"
        elif resumen_dict.get("cancelada"):
            entrada["estado"] = "cancelada"
        else:
            entrada["estado"] = "terminada"
        if resumen_dict:
            entrada["resumen"] = resumen_dict
            entrada["enviados"] = _entero(
                resumen_dict.get("enviados"), _entero(entrada.get("enviados"))
            )
            entrada["fallidos"] = _entero(
                resumen_dict.get("fallidos"), _entero(entrada.get("fallidos"))
            )
            entrada["total"] = _entero(
                resumen_dict.get("total"), _entero(entrada.get("total"))
            )


def _solicitar_paro() -> bool:
    """Setea el `threading.Event` de la campana activa. True si habia una.

    Pasa el estado a `deteniendo` (el ataque sigue vivo hasta que el backend
    termina el reporte en curso)."""
    with _CAMPANAS_LOCK:
        entrada = None
        for candidata in reversed(list(_CAMPANAS.values())):
            if str(candidata.get("estado") or "") in _ESTADOS_ACTIVOS:
                entrada = candidata
                break
        if entrada is None:
            return False
        if entrada.get("estado") == "en_curso":
            entrada["estado"] = "deteniendo"
        try:
            evento = entrada.get("evento")
            if evento is not None:
                evento.set()
        except Exception:
            pass
    return True


def _limpiar_registro(id_campana=None) -> None:
    """Borra la entrada indicada (o TODAS) del registro. Limpieza/tests."""
    with _CAMPANAS_LOCK:
        if id_campana is None:
            _CAMPANAS.clear()
        else:
            _CAMPANAS.pop(id_campana, None)


def _snapshot(id_campana=None) -> dict | None:
    """Copia inmutable de la entrada para pintar el panel sin retener el lock."""
    with _CAMPANAS_LOCK:
        if id_campana:
            entrada = _CAMPANAS.get(id_campana)
        elif _CAMPANAS:
            entrada = next(reversed(_CAMPANAS.values()))
        else:
            entrada = None
        if entrada is None:
            return None
        estado = str(entrada.get("estado") or "")
        return {
            "id": str(entrada.get("id") or ""),
            "estado": estado,
            "en_curso": estado in _ESTADOS_ACTIVOS,
            "deteniendo": estado == "deteniendo",
            "hechas": _entero(entrada.get("hechas")),
            "total": _entero(entrada.get("total")),
            "enviados": _entero(entrada.get("enviados")),
            "fallidos": _entero(entrada.get("fallidos")),
            "identidades": _entero(entrada.get("identidades")),
            "log": list(entrada.get("log") or []),
            "resumen": (
                entrada.get("resumen")
                if isinstance(entrada.get("resumen"), dict)
                else {}
            ),
            "error": str(entrada.get("error") or ""),
        }


def _filas_resultados(resumen: dict) -> list:
    """Filas (Email/OK/Detalle) del `resumen["resultados"]` para `st.dataframe`."""
    filas = []
    for resultado in (resumen or {}).get("resultados") or []:
        if not isinstance(resultado, dict):
            continue
        filas.append(
            {
                "Email": str(resultado.get("email") or ""),
                "OK": "✅" if resultado.get("ok") else "❌",
                "Detalle": str(resultado.get("detalle") or ""),
            }
        )
    return filas


def _pintar_proceso(snap=None) -> None:
    """Pinta UNA pasada del panel: en vivo o el resumen final (`st.*` aqui)."""
    if snap is None:
        snap = _snapshot()
    if not snap:
        return

    if snap["en_curso"]:
        st.markdown("#### 🚩 Ataque de reportes en vivo")
        total = max(1, snap["total"])
        hechas = min(snap["hechas"], total) if snap["total"] else snap["hechas"]
        st.progress(min(1.0, hechas / total))
        st.markdown(
            f"✅ **{snap['enviados']} enviados** · ❌ {snap['fallidos']} "
            f"fallidos · 🧾 {snap['identidades']} identidades"
        )
        lineas = snap["log"][-12:]
        if lineas:
            st.markdown("  \n".join(lineas))
        else:
            st.markdown("⏳ Esperando los primeros reportes…")
        if snap["deteniendo"]:
            st.warning("⛔ Detención solicitada: terminando el reporte en curso…")
        if st.button(
            "⛔ Detener",
            type="primary",
            key="change_btn_detener",
            disabled=snap["deteniendo"],
            help=(
                "Pide al backend que pare: no lanza reportes nuevos y cierra "
                "los navegadores al terminar el reporte en curso."
            ),
        ):
            _solicitar_paro()
            st.rerun()
        return

    # ---- Terminada (terminada/cancelada/error): resumen final ----
    st.markdown("#### 🧾 Último ataque de reportes")
    resumen = snap["resumen"] or {}
    if snap["estado"] == "error" or snap["error"]:
        st.error(
            "❌ Ataque de reportes interrumpido: "
            + (snap["error"] or "error inesperado del backend")
        )
    elif snap["estado"] == "cancelada" or resumen.get("cancelada"):
        st.warning(
            f"⛔ Ataque de reportes detenido: {snap['enviados']} enviados / "
            f"{snap['fallidos']} fallidos"
        )
    else:
        st.success(
            f"✅ Ataque de reportes terminado: {snap['enviados']} enviados / "
            f"{snap['fallidos']} fallidos"
        )
    filas = _filas_resultados(resumen)
    if filas:
        st.dataframe(filas, use_container_width=True, hide_index=True)
    st.caption(
        f"🧾 Identidades guardadas: "
        f"{_entero(resumen.get('identidades_guardadas'))}"
    )


def _refrescar_con_fragmento() -> bool:
    """`st.fragment(run_every=1s)` que repinta el proceso en vivo.

    Mismo patron que `activacion_masiva._refrescar_con_fragmento`: devuelve
    False si esta version de Streamlit no soporta fragmentos (el llamador cae
    al bucle `time.sleep(1); st.rerun()`). Cuando el ataque termina, el
    fragmento dispara UN rerun completo y deja de refrescar."""
    fabrica = getattr(st, "fragment", None)
    if not callable(fabrica):
        return False

    def _pasada():
        if _campana_en_curso() is None:
            st.rerun(scope="app")
            return
        _pintar_proceso()

    try:
        decorada = fabrica(run_every=1.0)(_pasada)
    except Exception:
        return False
    decorada()
    return True


def _render_proceso_activo(max_pasos=None) -> None:
    """Panel de proceso persistente (auto-refresco SOLO mientras corre).

    - Con ataque en curso: `st.fragment(run_every=1s)` si esta disponible; si
      no, bucle `time.sleep(1); st.rerun()`.
    - Sin ataque en curso: UNA pasada (resumen final en memoria o nada).

    `max_pasos` es un flag interno para tests/llamadas acotadas: con un valor
    (p. ej. 1) se pinta UNA sola pasada, sin fragmentos ni bucles."""
    if max_pasos is not None:
        _pintar_proceso()
        return
    if _campana_en_curso() is None:
        _pintar_proceso()
        return
    if _refrescar_con_fragmento():
        return
    _pintar_proceso()
    time.sleep(1)
    st.rerun()


# ============================ PROXIES ============================

def _paises_proxy() -> list:
    """Paises detectados en `data/proxies/*.txt` (sin `quemados.txt`).

    Nunca lanza: sin carpeta o sin archivos devuelve []."""
    try:
        from core.config import resolver_ruta

        carpeta = resolver_ruta("data/proxies")
        if not os.path.isdir(carpeta):
            return []
        paises = set()
        for nombre in os.listdir(carpeta):
            if not str(nombre).lower().endswith(".txt"):
                continue
            base = os.path.splitext(str(nombre))[0].strip()
            if not base or base.lower() == "quemados":
                continue
            paises.add(base.lower())
        return sorted(paises)
    except Exception:
        return []


def _proxies_disponibles(pais: str = "") -> int:
    """Cuantos proxies usaria el ataque (tolerante a fallos: 0)."""
    try:
        from utils.proxies import ProxyManager

        gestor = ProxyManager()
        if pais:
            return len(gestor.cargar_por_pais(pais) or [])
        return len(gestor.cargar_proxies() or [])
    except Exception:
        return 0


# ============================ GRANJA DE IDENTIDADES ============================

def _cargar_granja(limite: int = 20):
    """(total, filas) de la granja `CuentaChange` (ultimas `limite`).

    Puede lanzar si la tabla/BD no existe: `_visor_granja` lo envuelve."""
    from core.database import obtener_sesion
    from core.models import CuentaChange

    with obtener_sesion() as db:
        total = int(db.query(CuentaChange).count() or 0)
        registros = (
            db.query(CuentaChange)
            .order_by(CuentaChange.fecha_creacion.desc())
            .limit(max(0, int(limite)))
            .all()
        )
    filas = []
    for registro in registros:
        nombre = (
            f"{registro.nombre or ''} {registro.apellido or ''}".strip()
        )
        fecha = getattr(registro, "fecha_creacion", None)
        filas.append(
            {
                "Nombre": nombre,
                "Email": str(registro.email or ""),
                "CP": str(registro.codigo_postal or ""),
                "Petición": str(registro.url_peticion or ""),
                "Fecha": (
                    fecha.strftime("%Y-%m-%d %H:%M") if fecha else ""
                ),
            }
        )
    return total, filas


def _visor_granja() -> None:
    """Expander con el total y las ultimas 20 identidades de la granja.

    Envuelto en try/except: si la tabla/BD no existe solo pinta un caption
    informativo y la pagina sigue funcionando."""
    try:
        total, filas = _cargar_granja(limite=20)
    except Exception:
        st.caption(
            "ℹ️ La granja de identidades todavía no está disponible (sin "
            "tabla o sin conexión a la BD); el ataque de reportes sigue "
            "funcionando."
        )
        return
    with st.expander(
        f"🧾 Identidades creadas para firmar ({total})", expanded=False
    ):
        if filas:
            st.dataframe(filas, use_container_width=True, hide_index=True)
        else:
            st.caption("Aún no hay identidades guardadas en la granja.")


# ============================ LANZAMIENTO ============================

def _lanzar_ataque(url, contexto, cantidad, workers, usar_proxies, pais_proxy,
                   guardar_identidades, headless) -> None:
    """Valida y lanza el ataque de reportes en un hilo daemon (nunca lanza).

    El backend se importa AQUI (perezoso): la pagina carga sin Chrome. El
    callback del backend solo muta `_CAMPANAS` bajo lock (`_anotar_evento`).
    """
    direccion = str(url or "").strip()
    if not direccion or "change.org" not in direccion.lower():
        st.warning(
            "La URL debe ser una petición de Change.org "
            "(https://www.change.org/p/...)."
        )
        return
    motivo = str(contexto or "").strip()
    if not motivo:
        st.warning(
            "Escribe el motivo general de la queja: la IA lo necesita para "
            "redactar los reportes."
        )
        return
    if _campana_en_curso() is not None:
        st.warning(
            "⛔ Ya hay un ataque de reportes en curso: espera a que termine o "
            "pulsa «⛔ Detener» en el panel de arriba."
        )
        return

    try:
        from cuentas.change_org import ejecutar_campana_reportes
    except Exception as e:  # noqa: BLE001
        st.error(f"No se pudo importar el backend de reportes: {e}")
        return

    evento = threading.Event()
    id_campana = _registrar_campana(
        evento=evento,
        parametros={
            "url": direccion,
            "cantidad": int(cantidad),
            "workers": int(workers),
            "usar_proxies": bool(usar_proxies),
            "pais_proxy": str(pais_proxy or ""),
            "guardar_identidades": bool(guardar_identidades),
            "headless": bool(headless),
        },
    )

    def _cb(evento_cb):
        # Corre en los hilos worker del backend: SOLO registro, jamas `st.*`.
        _anotar_evento(id_campana, evento_cb)

    def _runner():
        resumen = None
        error = None
        try:
            resumen = ejecutar_campana_reportes(
                url_peticion=direccion,
                contexto=motivo,
                cantidad=int(cantidad),
                max_workers=int(workers),
                usar_proxies=bool(usar_proxies),
                pais_proxy=str(pais_proxy or ""),
                guardar_identidades=bool(guardar_identidades),
                headless=bool(headless),
                cancelar=evento,
                callback=_cb,
            )
        except BaseException as e:  # noqa: BLE001
            error = e
        finally:
            _finalizar_campana(id_campana, resumen=resumen, error=error)

    hilo = threading.Thread(target=_runner, daemon=True, name="change-reportes")
    with _CAMPANAS_LOCK:
        entrada = _CAMPANAS.get(id_campana)
        if entrada is not None:
            entrada["hilo"] = hilo
    try:
        hilo.start()
    except BaseException as e:  # noqa: BLE001
        _finalizar_campana(id_campana, error=e)
        st.error(f"No se pudo iniciar el ataque de reportes: {e}")
        return
    if _en_contexto_streamlit():
        # El panel persistente (arriba) toma el relevo con el progreso en vivo.
        st.rerun()


def render(usuario: dict):
    """Pagina "✍️ Change.org: Reportes": ataque masivo con identidades IA."""
    cabecera(
        "✍️ CHANGE.ORG: REPORTES DE POLÍTICAS",
        "Ataque masivo de reportes con identidades generadas por IA",
    )

    # Panel persistente: SIEMPRE al inicio, para que el avance/log siga visible
    # aunque el usuario recargue o navegue a otra operacion durante el ataque.
    _render_proceso_activo()

    st.markdown("### 🎯 Objetivo del ataque")
    url = st.text_input(
        "URL de la petición objetivo",
        key="change_rep_url",
        placeholder="https://www.change.org/p/...",
        help="Petición de Change.org que se va a reportar por violar sus normas.",
    )
    contexto = st.text_area(
        "Motivo general de la queja (contexto para la IA)",
        key="change_rep_contexto",
        height=120,
        placeholder=(
            "Ej. La petición difunde información falsa y acosa a personas "
            "concretas para incumplir las normas de la comunidad."
        ),
        help=(
            "La IA redacta cada queja a partir de este motivo; no se copia "
            "literalmente en los reportes."
        ),
    )

    col_cantidad, col_workers = st.columns(2)
    with col_cantidad:
        cantidad = st.number_input(
            "Cantidad de reportes a enviar",
            min_value=1,
            max_value=100,
            value=5,
            step=1,
            key="change_rep_cantidad",
            help="Un reporte por cada identidad generada (1-100).",
        )
    with col_workers:
        workers = st.number_input(
            "Navegadores simultáneos",
            min_value=1,
            max_value=4,
            value=2,
            step=1,
            key="change_rep_workers",
            help=(
                "Reportes en paralelo (1-4). En Railway no conviene pasar de "
                "2-3: cada Chrome consume RAM/CPU/hilos."
            ),
        )

    with st.expander("⚙️ Opciones avanzadas", expanded=False):
        rotar_proxies = st.checkbox(
            "🌐 Rotar proxy residencial por reporte",
            value=True,
            key="change_rep_proxies",
            help=(
                "Cada reporte sale con una IP distinta (menos bloqueos de "
                "Change.org). Si no hay proxies, el ataque continúa sin proxy."
            ),
        )
        paises = _paises_proxy()
        pais_sel = st.selectbox(
            "País del proxy",
            [PAIS_TODAS] + paises,
            index=0,
            key="change_rep_pais",
            help=(
                "Filtra los proxies por país (archivos de `data/proxies/`). "
                "«Todas» usa cualquier proxy disponible."
            ),
        )
        pais_proxy = "" if pais_sel == PAIS_TODAS else str(pais_sel)
        if rotar_proxies:
            disponibles = _proxies_disponibles(pais_proxy)
            if disponibles:
                etiqueta_pais = (
                    f" para {pais_sel}" if pais_proxy else ""
                )
                st.caption(
                    f"🌐 {disponibles} proxies disponibles{etiqueta_pais}."
                )
            else:
                st.warning(
                    "No hay proxies disponibles con ese filtro: el ataque "
                    "continuará sin proxy."
                )
        guardar_identidades = st.checkbox(
            "🧾 Guardar identidades en la base de datos",
            value=True,
            key="change_rep_guardar",
            help=(
                "Las identidades usadas quedan en la granja (`CuentaChange`) "
                "para reutilizarlas más adelante."
            ),
        )
        chrome_visible = st.checkbox(
            "🖥️ Chrome visible (debug)",
            value=False,
            key="change_rep_visible",
            help="Solo para depurar: en producción el ataque corre headless.",
        )
        headless = not bool(chrome_visible)

    if st.button(
        "🚩 Lanzar ataque de reportes",
        type="primary",
        key="btn_change_reportes",
        help=(
            "Genera identidades con IA y reporta la petición con cada una. "
            "El progreso se muestra arriba y sobrevive a las recargas."
        ),
    ):
        _lanzar_ataque(
            url=url,
            contexto=contexto,
            cantidad=cantidad,
            workers=workers,
            usar_proxies=bool(rotar_proxies),
            pais_proxy=pais_proxy,
            guardar_identidades=bool(guardar_identidades),
            headless=headless,
        )

    _visor_granja()
