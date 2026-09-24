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
El reparto de roles es POR PORCENTAJES: el usuario elige las cuentas (p. ej.
el selector en modo Filtro -> Seccion: Libertad), define el % de cada rol en
«🎚️ Reparto por porcentajes» y lo aplica. La campana usa SIEMPRE el rol
guardado de cada cuenta (`roles_aleatorios=False` en la llamada al motor: el
motor lo sigue soportando, pero la UI ya no ofrece el modo aleatorio). El
expander «🏷️ Asignar roles manualmente» queda para cuentas sueltas. Ver
conteos/ejemplos y lanzar `MotorActivacion.ejecutar_por_roles` limitado a una
seccion opcional.

Modo "📝 Campaña solo de posts (sin tweet ancla)": checkbox de la pestana B
para campanas donde NO hay tweet ancla. Deshabilita las URLs y limita las
acciones a posts con hashtag/contexto (`solo_roles=["hashtags"]` con rol
fijo); requiere material para que la IA genere los posts: hashtags, texto
base, el contexto manual (tema) o el trasfondo de noticias (que NUNCA se
menciona).

Las cuentas sin registro siguen saltandose. `cooldown_min` evita que una
misma cuenta repita accion antes de ese descanso.

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

Opciones de velocidad (ambas pestanas de activacion): en la vista principal
solo quedan los datos de la campaña (cuentas/seccion, URLs o modo solo-posts,
contexto/hashtags, duracion y el boton de lanzar). Todo lo demas vive en
«⚙️ Opciones avanzadas» con los valores recomendados ya fijos: 2 navegadores,
12 trabajadores (`MAX_WORKERS`), "Repetir hasta agotar el tiempo" ON con
40-90% de cuentas por ronda, "Sin proxy" OFF, "No cargar imagenes" ON, modo
pestaña persistente ON (reciclado a las 40 acciones), "Publicar por API" OFF
(X bloquea/limita la API con anti-bot 226 / limite diario 344) y pausa entre
comentarios al MISMO tweet de 15s. Los disyuntores de la API
(`API_BREAKER_FALLOS`, `API_BREAKER_SEG`) no se editan en la UI: se documentan
en un caption y se ajustan por env. En Railway NO conviene pasar de 3-4
"navegadores": si Chrome crashea, la campana se frena en cascada.

Proceso persistente, paro y respaldo de estado: las campanas se registran a
nivel MODULO (`_CAMPANAS`), asi que el panel "🚦 Proceso en vivo" (barra con
tiempo/avance, ronda, velocidad y feed de eventos) sigue visible si recargas la
pagina o cambias de operacion, y aparece en AMBAS pestanas de activacion.
Incluye "⛔ Paro total" (evento `cancelar` para el motor, con
`motor.solicitar_paro()` como fallback) que corta la campana en segundos y
libera el guard, y "⚠️ Forzar liberación" a los 90s si el hilo no muere. El
ultimo estado se guarda (escritura atomica, sin credenciales) en
`data/campanas/ultima_campana.json` y se muestra si ya no hay campana en
memoria. El guard usa un token de propiedad: el hilo viejo nunca libera el
guard de una campana nueva.

Caso de uso "trending con UN solo tweet ancla" (pestana B): usa el selector
de cuentas en modo Filtro (p. ej. Seccion: Libertad), define el porcentaje de
cada rol en «🎚️ Reparto por porcentajes» y aplica (el boton queda
deshabilitado si la suma no es 100%). Para una campana de ancla unica suele
funcionar mas RT simple (amplifica y es lo que menos castiga X) con citas y
hashtags como combustible; los comentarios van en minoria y espaciados
(ver `pausa_comentario_url_seg`).
"""
import json
import os
import threading
import time
import uuid

import streamlit as st

from web.ui import cabecera
from web.operaciones._helpers import (
    aviso_pausadas,
    es_pausada_activacion,
    separar_pausadas,
)
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

# Token de PROPIEDAD del guard: id de la campana que lo tomo. Permite que el
# "forzar liberacion" del panel libere el guard para relanzar de inmediato SIN
# que el hilo viejo (que sigue muriendo) libere por error el guard de una
# campana NUEVA al ejecutarse su `finally`.
_CAMPANA_ID = ""

# REGISTRO DE CAMPANAS a nivel MODULO: sobrevive a recargados de la pagina y a
# la navegacion entre operaciones (no vive en `st.session_state`). Cada entrada:
#   {"id", "motor", "hilo", "evento" (threading.Event de paro), "inicio",
#    "inicio_mono", "fin", "estado" ("en_curso"|"deteniendo"|"terminada"|
#    "cancelada"|"error"), "tipo" ("citas"|"roles"), "resumen", "error",
#    "parametros", "previos_env", "usa_cancelar", "paro_solicitado", "total"}
# El hilo del motor SOLO escribe en este registro (y en el JSON de disco); los
# `st.*` los pinta el hilo principal desde `snapshot_progreso()`.
_CAMPANAS: dict = {}
_CAMPANAS_LOCK = threading.RLock()

# Marcador de ARCHIVO de campana activa: lo leen otros procesos del sistema
# (p. ej. el calentamiento continuo del scheduler) para NO lanzar acciones
# mientras hay una campana de activacion en curso. Se escribe al adquirir el
# guard y se borra al liberarlo (ver `_adquirir_campana`/`_liberar_campana`).
RUTA_CAMPANA_ACTIVA = "data/.campana_activa"

# Persistencia del ultimo proceso: un JSON chico, atomico y SIN credenciales
# (id/estado/tiempos/tipo/parametros seguros/resumen saneado/progreso). Se
# escribe al registrar la campana, en cada tick del panel en vivo y al
# terminar; al abrir la pagina sin campana en memoria se muestra ese estado.
RUTA_ULTIMA_CAMPANA = "data/campanas/ultima_campana.json"

# Segundos que se espera, tras "⛔ Paro total", antes de ofrecer el boton
# "⚠️ Forzar liberación" (red de seguridad; nunca se fuerza solo).
PARO_FORZAR_SEG = 90

# Claves de `st.session_state` del panel persistente: id descartado con
# "🧹 Ocultar", id de la campana que lanzo ESTA sesion (para la limpieza
# diferida del contexto) y marca de limpieza ya programada.
CLAVE_CAMPANA_OCULTA = "act_proceso_oculto"
CLAVE_CAMPANA_LANZADA = "act_campana_lanzada_id"
CLAVE_CAMPANA_LIMPIEZA = "act_campana_limpieza_hecha"


def _marcar_campana_activa() -> None:
    """Escribe `data/.campana_activa` con el timestamp actual (epoch).

    El archivo es el aviso para otros procesos (calentamiento continuo del
    scheduler): mientras exista, no deben lanzar acciones. Es tolerante a
    errores: si no se puede escribir, el guard en memoria sigue funcionando
    igual (nunca rompe el lanzamiento de la campana).
    """
    try:
        import time

        from core.config import resolver_ruta

        ruta = resolver_ruta(RUTA_CAMPANA_ACTIVA)
        try:
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
        except Exception:
            pass
        with open(ruta, "w", encoding="utf-8") as archivo:
            archivo.write(str(time.time()))
    except Exception:
        pass


def _limpiar_campana_activa() -> None:
    """Borra `data/.campana_activa` si existe (tolerante a errores)."""
    try:
        from core.config import resolver_ruta

        ruta = resolver_ruta(RUTA_CAMPANA_ACTIVA)
        if os.path.exists(ruta):
            os.remove(ruta)
    except Exception:
        pass


def _adquirir_campana() -> bool:
    """Toma el guard de campana unica (evento en memoria + marcador de archivo).

    Devuelve False si ya habia una campana en curso en este proceso. El
    marcador se escribe SOLO si se logro tomar el evento, para no pisar el de
    una campana que ya esta corriendo.
    """
    if _CAMPANA_ACTIVA.is_set():
        return False
    _CAMPANA_ACTIVA.set()
    _marcar_campana_activa()
    return True


def _liberar_campana(id_campana=None) -> bool:
    """Libera el guard de campana unica (evento + marcador). Idempotente.

    Con `id_campana` solo libera si esa campana sigue siendo la DUEÑA del
    guard (token): asi el `finally` de un hilo viejo que termina tarde (tras un
    "forzar liberación" y el relanzamiento de OTRA campaña) no libera el guard
    de la campaña nueva. Sin `id_campana` libera incondicionalmente (limpieza y
    compatibilidad)."""
    global _CAMPANA_ID
    with _CAMPANAS_LOCK:
        if id_campana is not None and _CAMPANA_ID and _CAMPANA_ID != id_campana:
            return False
        _CAMPANA_ID = ""
    _CAMPANA_ACTIVA.clear()
    _limpiar_campana_activa()
    return True


def _es_dueno(id_campana) -> bool:
    """True si `id_campana` sigue siendo el dueño actual del guard."""
    with _CAMPANAS_LOCK:
        return bool(id_campana) and _CAMPANA_ID == id_campana


def _campana_actual():
    """Ultima entrada del registro (la campana mas reciente) o None.

    Es una referencia viva a la entrada (los paneles leen `snapshot_progreso`
    del motor); no la mutan fuera de los helpers con lock."""
    with _CAMPANAS_LOCK:
        if not _CAMPANAS:
            return None
        return list(_CAMPANAS.values())[-1]


def _campana_en_curso():
    """Entrada mas reciente con estado "en_curso" o "deteniendo" (o None)."""
    with _CAMPANAS_LOCK:
        for entrada in reversed(list(_CAMPANAS.values())):
            if entrada.get("estado") in ("en_curso", "deteniendo"):
                return entrada
    return None


def _limpiar_registro(id_campana=None) -> None:
    """Borra una entrada del registro (o TODAS si no se pasa id)."""
    with _CAMPANAS_LOCK:
        if id_campana is None:
            _CAMPANAS.clear()
        else:
            _CAMPANAS.pop(id_campana, None)


def _solicitar_paro() -> bool:
    """Pide el paro TOTAL de la campaña en curso (evento + estado).

    Setea el `threading.Event` de la campaña (el motor lo consulta en sus
    bucles y muere en segundos), marca el estado "deteniendo" y, si el motor
    NO recibio el evento (`_soporta_kwarg(..., "cancelar")` era False), llama a
    `motor.solicitar_paro()` como fallback. Devuelve False si no habia campaña.
    """
    entrada = _campana_en_curso()
    if entrada is None:
        return False
    entrada["estado"] = "deteniendo"
    entrada["paro_solicitado"] = time.time()
    evento = entrada.get("evento")
    if evento is not None:
        try:
            evento.set()
        except Exception:
            pass
    if not entrada.get("usa_cancelar"):
        func = getattr(entrada.get("motor"), "solicitar_paro", None)
        if callable(func):
            try:
                func()
            except Exception:
                pass
    _escribir_ultima_campana(_entrada_para_json(entrada))
    return True


def _snapshot_progreso(motor) -> dict:
    """`motor.snapshot_progreso()` tolerante ({} si no existe o falla)."""
    func = getattr(motor, "snapshot_progreso", None)
    if not callable(func):
        return {}
    try:
        datos = func()
        return datos if isinstance(datos, dict) else {}
    except Exception:
        return {}


def _entero(valor) -> int:
    """int() tolerante a None/str/basura."""
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


def _sanear_url(valor) -> str:
    """URL sin query/fragment (los links pueden traer tokens/credenciales)."""
    texto = str(valor or "")
    if not texto.lower().startswith(("http://", "https://")):
        return texto
    try:
        from urllib.parse import urlsplit, urlunsplit

        partes = urlsplit(texto)
        return urlunsplit((partes.scheme, partes.netloc, partes.path, "", ""))
    except Exception:
        return texto.split("?", 1)[0].split("#", 1)[0]


def _valor_saneado(valor, profundidad: int = 0):
    """Version JSON-segura de un valor del resumen (sin objetos ni tokens)."""
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, str):
        if valor.lower().startswith(("http://", "https://")):
            return _sanear_url(valor)[:500]
        return valor[:500]
    if profundidad >= 3:
        return str(valor)[:200]
    if isinstance(valor, dict):
        saneado = {}
        for clave, sub in list(valor.items())[:40]:
            saneado[str(clave)[:60]] = _valor_saneado(sub, profundidad + 1)
        return saneado
    if isinstance(valor, (list, tuple, set)):
        return [_valor_saneado(sub, profundidad + 1) for sub in list(valor)[:40]]
    return str(valor)[:200]


def _resumen_saneado(resumen) -> dict:
    """Resumen del motor listo para JSON: mismo contenido, sin URLs con tokens."""
    if not isinstance(resumen, dict):
        return {}
    return _valor_saneado(resumen, 0)


def _evento_saneado(evento) -> dict:
    """Evento del feed en formato JSON-seguro (para el archivo de disco)."""
    if not isinstance(evento, dict):
        return {}
    return {
        "usuario": str(evento.get("usuario") or "")[:80],
        "ok": bool(evento.get("ok")),
        "detalle": _recortar(evento.get("detalle"), 120),
        "ronda": evento.get("ronda"),
        "rol": str(evento.get("rol") or "")[:40],
        "url": _sanear_url(evento.get("url"))[:300],
    }


def _ruta_ultima_campana() -> str:
    """Ruta absoluta del JSON de persistencia (tolerante a core viejo)."""
    try:
        from core.config import resolver_ruta

        return resolver_ruta(RUTA_ULTIMA_CAMPANA)
    except Exception:
        return RUTA_ULTIMA_CAMPANA


def _escribir_ultima_campana(datos: dict) -> bool:
    """Escribe el JSON de disco de forma ATOMICA (tmp + os.replace).

    Nunca lanza: la persistencia es un extra del panel y no debe romper la
    campaña. El contenido ya viene saneado por `_entrada_para_json`."""
    try:
        ruta = _ruta_ultima_campana()
        carpeta = os.path.dirname(ruta)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        temporal = f"{ruta}.tmp"
        with open(temporal, "w", encoding="utf-8") as archivo:
            json.dump(datos, archivo, ensure_ascii=False, indent=2, default=str)
        os.replace(temporal, ruta)
        return True
    except Exception:
        return False


def _leer_ultima_campana() -> dict:
    """Lee el JSON de disco del ultimo proceso ({} si no existe o esta roto)."""
    try:
        with open(_ruta_ultima_campana(), "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
        return datos if isinstance(datos, dict) else {}
    except Exception:
        return {}


def _entrada_para_json(entrada: dict, snap: dict | None = None) -> dict:
    """Version serializable de una entrada del registro (SIN credenciales).

    Solo se copian claves conocidas: id/estado/tiempos, tipo, un resumen
    saneado, el error y el progreso (hechas/exitosas/fallidas/omitidas/ronda/
    fase + ultimos 8 eventos con URLs sin query). Nunca se serializan
    `motor`/`hilo`/`evento`/`previos_env` ni `parametros` completos."""
    if snap is None:
        snap = _snapshot_progreso(entrada.get("motor"))
    parametros = entrada.get("parametros") or {}
    eventos = list(snap.get("eventos") or [])[-8:]
    return {
        "id": str(entrada.get("id") or ""),
        "estado": str(entrada.get("estado") or ""),
        "tipo": str(entrada.get("tipo") or "citas"),
        "inicio": entrada.get("inicio"),
        "fin": entrada.get("fin"),
        "actualizado": time.time(),
        "duracion_min": parametros.get("duracion_min"),
        "repetir": bool(parametros.get("repetir")),
        "curva_aceleracion": bool(parametros.get("curva_aceleracion")),
        "curva_fase1_min": parametros.get("curva_fase1_min"),
        "resumen": _resumen_saneado(entrada.get("resumen") or {}),
        "error": _recortar(entrada.get("error"), 300),
        "progreso": {
            "hechas": _entero(snap.get("hechas")),
            "exitosas": _entero(snap.get("exitosas")),
            "fallidas": _entero(snap.get("fallidas")),
            "omitidas": _entero(snap.get("omitidas")),
            "ronda_actual": _entero(snap.get("ronda_actual")) or 1,
            "fase_actual": snap.get("fase_actual"),
            "eventos": [_evento_saneado(evento) for evento in eventos],
        },
    }


def _registrar_campana(motor=None, hilo=None, evento=None, tipo: str = "citas",
                       parametros: dict | None = None, previos_env=None,
                       usa_cancelar: bool = False,
                       id_campana: str | None = None) -> str:
    """Registra una campaña en `_CAMPANAS` y devuelve su id.

    La entrada queda disponible para el panel persistente (aunque la sesion
    que la lanzo se recargue o navegue a otra operacion) y se escribe el JSON
    de disco al registrar."""
    id_campana = id_campana or f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    entrada = {
        "id": id_campana,
        "motor": motor,
        "hilo": hilo,
        "evento": evento if evento is not None else threading.Event(),
        "inicio": time.time(),
        "inicio_mono": time.monotonic(),
        "fin": None,
        "estado": "en_curso",
        "tipo": str(tipo or "citas"),
        "resumen": None,
        "error": "",
        "parametros": dict(parametros or {}),
        "previos_env": previos_env,
        "usa_cancelar": bool(usa_cancelar),
        "paro_solicitado": None,
        "total": 0,
        "forzada": False,
        "ultimo_json": 0.0,
    }
    global _CAMPANA_ID
    with _CAMPANAS_LOCK:
        _CAMPANAS[id_campana] = entrada
        _CAMPANA_ID = id_campana
    _escribir_ultima_campana(_entrada_para_json(entrada))
    return id_campana


def _finalizar_campana(id_campana, resumen=None, error=None) -> bool:
    """Cierra una campaña del registro: estado, fin, resumen y guard.

    - Estado final: "error" si hubo excepcion; "cancelada" si se pidio el paro
      (evento set) o el resumen trae `cancelada=True`; "terminada" si no.
    - Restaura las env de velocidad guardadas SOLO si esta campaña seguia
      siendo la dueña del guard (si se forzo la liberacion y ya corre otra,
      la campaña nueva manda).
    - Escribe el JSON de disco (si es la ultima entrada) y libera el guard con
      token (`_liberar_campana(id_campana)`), para no pisar a una campaña nueva.
    Devuelve True si la entrada existia. Nunca lanza."""
    if not id_campana:
        return False
    previos = None
    entrada = None
    es_ultima = False
    try:
        with _CAMPANAS_LOCK:
            entrada = _CAMPANAS.get(id_campana)
            if entrada is not None:
                evento = entrada.get("evento")
                cancelada = bool(
                    (evento is not None and evento.is_set())
                    or (
                        isinstance(resumen, dict)
                        and resumen.get("cancelada")
                    )
                )
                if error is not None:
                    entrada["estado"] = "error"
                elif cancelada:
                    entrada["estado"] = "cancelada"
                else:
                    entrada["estado"] = "terminada"
                entrada["fin"] = time.time()
                if isinstance(resumen, dict):
                    entrada["resumen"] = resumen
                entrada["error"] = "" if error is None else _recortar(error, 300)
                previos = entrada.pop("previos_env", None)
                es_ultima = bool(
                    _CAMPANAS and list(_CAMPANAS.values())[-1].get("id") == id_campana
                )
        if previos is not None and _es_dueno(id_campana):
            _restaurar_opciones_velocidad(previos)
        if entrada is not None and es_ultima:
            _escribir_ultima_campana(_entrada_para_json(entrada))
    except Exception:
        pass
    _liberar_campana(id_campana)
    return entrada is not None


# Navegadores recomendados para una campaña normal (valor FIJO que trae el
# formulario; solo se cambia desde «⚙️ Opciones avanzadas»). Con modo pestaña
# persistente 2 navegadores ya rinden bien y en Railway no conviene pasar de
# 3-4 (si Chrome crashea, la campaña se frena en cascada).
NAVEGADORES_RECOMENDADOS = 2


def _navegadores_default() -> int:
    """Valor por defecto de "Navegadores simultáneos" (2, recomendado).

    El número es fijo para que lanzar una campaña normal no requiera tocar
    nada; se puede subir en el mismo campo de «⚙️ Opciones avanzadas».
    """
    return NAVEGADORES_RECOMENDADOS


# ============================ LOGICA PURA ============================

def _repartir_por_porcentajes(usuarios: list, pesos: dict | None = None) -> dict:
    """Reparte 'usuarios' (en orden) entre los 4 roles segun `pesos` (%).

    Pensado para que el usuario controle el reparto desde la pestana "Por
    roles": elige las cuentas (p. ej. el selector en modo Filtro -> Seccion:
    Libertad), define el % de cada rol en «🎚️ Reparto por porcentajes» y lo
    aplica a `Cuenta.rol_activacion`.

    Reglas:
      - `pesos` es {rol: numero >= 0}; default equitativo 25/25/25/25.
        Si todos los pesos son 0 (o `pesos` es None), reparte equitativo.
      - Se normaliza por la SUMA (matematicamente igual con suma 100):
        p. ej. {1,1,1,1} = {25,25,25,25}.
      - Metodo del RESTO MAYOR: cuota = n * peso / suma; la parte entera se
        asigna y los puestos sobrantes van a los mayores restos (empate ->
        mayor peso, luego orden de ORDEN_ROLES). La suma es EXACTA = n.
      - Los roles con peso 0 NUNCA reciben cuentas.
      - Mismas garantias que el resto de repartos: limpia None/vacios y
        duplicados (se ignora un '@' inicial, case-insensitive), cortes
        CONTIGUOS y ningun usuario queda en dos roles a la vez.

    Ejemplos: n=100 equitativo -> 25/25/25/25; n=141 equitativo ->
    cita 36 / hashtags 35 / comentario 35 / rt 35; n=100 con
    {cita:50, hashtags:30, comentario:10, rt:10} -> 50/30/10/10; un rol con
    peso 0 -> 0 cuentas.
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

    pesos_rol: dict = {}
    for rol in ORDEN_ROLES:
        try:
            valor = float((pesos or {}).get(rol, 0.0))
        except (TypeError, ValueError):
            valor = 0.0
        pesos_rol[rol] = valor if valor > 0 else 0.0
    if sum(pesos_rol.values()) <= 0:
        # Sin pesos utiles (None o todos 0): reparto equitativo.
        pesos_rol = {rol: 1.0 for rol in ORDEN_ROLES}
    total = sum(pesos_rol.values())

    cuotas = {rol: n * pesos_rol[rol] / total for rol in ORDEN_ROLES}
    asignados = {rol: int(cuotas[rol]) for rol in ORDEN_ROLES}
    restantes = n - sum(asignados.values())
    if restantes > 0:
        # Solo los roles con peso > 0 pueden recibir sobrantes.
        candidatos = [rol for rol in ORDEN_ROLES if pesos_rol[rol] > 0]
        candidatos.sort(
            key=lambda rol: (
                -(cuotas[rol] - int(cuotas[rol])),
                -pesos_rol[rol],
                ORDEN_ROLES.index(rol),
            )
        )
        for rol in candidatos[:restantes]:
            asignados[rol] += 1
        # Red de seguridad para redondeos de punto flotante: la particion
        # siempre queda EXACTA (= n).
        faltan = n - sum(asignados.values())
        for rol in candidatos:
            if faltan <= 0:
                break
            asignados[rol] += 1
            faltan -= 1

    inicio = 0
    for rol in ORDEN_ROLES:
        reparto[rol] = limpios[inicio:inicio + asignados[rol]]
        inicio += asignados[rol]
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
    `usuarios` devuelve todas. Sirve para validar que la seleccion de la
    campana tiene al menos una cuenta que procesar.
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


# ============================ TIERS, CURVA Y CUOTAS ============================

def _limite_diario_config() -> int:
    """Tope diario por cuenta (`LIMITE_DIARIO_POR_CUENTA`); 0 = sin tope.

    Lee la configuracion existente (`core.registro.limite_acciones_dia`), sin
    escribir envs nuevas. Tolerante si el core es viejo: nunca lanza."""
    try:
        from core.registro import limite_acciones_dia

        return int(limite_acciones_dia() or 0)
    except Exception:
        return 0


def _valor_metrica(valor):
    """Normaliza un valor del resumen para `st.metric` (listas -> conteo)."""
    if valor is None:
        return 0
    if isinstance(valor, (list, tuple, set, dict)):
        return len(valor)
    return valor


def _metricas_tier_curva(resultados: dict) -> list:
    """Metricas OPCIONALES de curva/tiers/cuotas que traiga el resumen.

    Solo agrega la metrica si la clave existe en el resumen del motor (asi la
    pagina sigue funcionando con motores viejos que no devuelven esas claves).
    """
    res = resultados or {}
    metricas = []
    if "curva_aceleracion" in res:
        metricas.append(
            ("🚀 Curva", "Sí" if res.get("curva_aceleracion") else "No")
        )
    if "curva_fase1_min" in res:
        metricas.append(
            ("🚀 Fase 1 (min)", _valor_metrica(res.get("curva_fase1_min")))
        )
    if "fase_actual" in res:
        metricas.append(("🚀 Fase final", f"{res.get('fase_actual')}/2"))
    if "cascada_urls" in res:
        metricas.append(
            ("🔗 Cascada URLs", _valor_metrica(res.get("cascada_urls")))
        )
    if "tier2_hashtags_omitidas" in res:
        metricas.append(
            (
                "🧱 Tier 2 sin hashtags",
                _valor_metrica(res.get("tier2_hashtags_omitidas")),
            )
        )
    if "tier3_omitidas" in res:
        metricas.append(
            ("📊 Tier 3 sin rol", _valor_metrica(res.get("tier3_omitidas")))
        )
    if "tier3_sin_rol" in res:
        metricas.append(
            ("📊 Tier 3 sin acción", _valor_metrica(res.get("tier3_sin_rol")))
        )
    if "tier3_liberadas" in res:
        metricas.append(
            (
                "🚀 Tier 3 liberadas fase 2",
                _valor_metrica(res.get("tier3_liberadas")),
            )
        )
    if "sustituciones_bloqueadas_tier" in res:
        metricas.append(
            (
                "📊 Reservas bloqueadas por tier",
                _valor_metrica(res.get("sustituciones_bloqueadas_tier")),
            )
        )
    if "rotadas_por_cuota_dia" in res:
        metricas.append(
            ("♻️ Rotadas a respaldo", _valor_metrica(res.get("rotadas_por_cuota_dia")))
        )
    if "agotadas_dia" in res:
        metricas.append(
            ("🛑 Agotadas por hoy", _valor_metrica(res.get("agotadas_dia")))
        )
    if "reserva_usada" in res:
        metricas.append(
            ("🛡️ Reserva usada", _valor_metrica(res.get("reserva_usada")))
        )
    if "reserva_disponible" in res:
        metricas.append(
            ("🛡️ Reserva disponible", _valor_metrica(res.get("reserva_disponible")))
        )
    return metricas


def _bloqueo_roles_tier(filas, usuarios=None, solo_roles=None,
                        aleatorio: bool = False) -> list:
    """Mensajes bloqueantes de `core.tiers` para cuentas con rol prohibido.

    Aplica los mismos filtros que la campana (`_cuentas_objetivo` + roles
    permitidos por `solo_roles`) y devuelve los mensajes de
    `core.tiers.error_rol_tier` de CUALQUIER fila cuyo tier prohiba su rol
    efectivo ALMACENADO (Tier 2 con "hashtags" -incluye post/mantenimiento/
    hilo- y Tier 3 con "hashtags"/"cita"/"comentario"; Tier 3 solo admite
    rt/like). Lista vacía = la campana puede lanzarse. Con `aleatorio=True`
    devuelve []: el motor sortea roles permitidos por tier. Nunca lanza (si el
    core es viejo devuelve [])."""
    if aleatorio:
        return []
    try:
        from core.registro import normalizar_rol_cuota
        from web.operaciones.cuentas import _errores_roles_tier
    except Exception:
        return []

    objetivo = _cuentas_objetivo(filas, usuarios)
    permitidos = None
    if solo_roles:
        permitidos = {
            normalizar_rol_cuota(rol)
            for rol in solo_roles
            if str(rol or "").strip()
        }

    candidatas = []
    for fila in objetivo:
        if not isinstance(fila, dict):
            continue
        rol = normalizar_rol_cuota(fila.get("rol_activacion"))
        if not rol:
            continue
        if permitidos is not None and rol not in permitidos:
            continue
        candidatas.append(fila)
    try:
        return _errores_roles_tier(candidatas)
    except Exception:
        return []


def _bloqueo_tier2_hashtags(filas, usuarios=None, solo_roles=None,
                            aleatorio: bool = False) -> list:
    """Alias retrocompatible de `_bloqueo_roles_tier` (Tier 2 y Tier 3).

    Se conserva el nombre viejo para no romper tests/llamadas existentes; la
    validacion ahora cubre ambos tiers (Tier 2 no puede hashtags; Tier 3 solo
    RT y likes)."""
    return _bloqueo_roles_tier(
        filas, usuarios=usuarios, solo_roles=solo_roles, aleatorio=aleatorio
    )


def _errores_rol_tier_destino(filas, rol) -> list:
    """Mensajes bloqueantes al querer ASIGNAR `rol` (rol DESTINO) a esas filas.

    A diferencia de `_bloqueo_roles_tier` (que valida el rol ya almacenado),
    aquí se valida el rol que el reparto por porcentajes o la asignación manual
    van a escribir: `core.tiers.error_rol_tier` por cada fila (Tier 2 + hashtags
    y Tier 3 + hashtags/cita/comentario bloquean). Devuelve solo los mensajes
    NO vacíos. Nunca lanza."""
    try:
        from core.tiers import error_rol_tier
        from web.operaciones.cuentas import _fila_como_cuenta
    except Exception:
        return []
    errores = []
    for fila in filas or []:
        if not isinstance(fila, dict):
            continue
        try:
            mensaje = error_rol_tier(_fila_como_cuenta(fila), rol)
        except Exception:
            mensaje = ""
        if mensaje:
            errores.append(mensaje)
    return errores


def _excluidos_cita(cuentas: list, cantidad, todas_cuentas: bool) -> set:
    """Cuentas que probablemente use la pestana Cita masiva (para la reserva).

    Aproximacion documentada: sin cantidad (o con "todas las cuentas") el flujo
    clasico usa todas las activas; con cantidad > 0, las primeras N (el motor
    las recorre en orden alfabetico). Las cuentas de respaldo son las que NO
    caen aqui."""
    usuarios = [
        str(f.get("usuario") or "") for f in (cuentas or []) if f.get("usuario")
    ]
    try:
        n = int(cantidad or 0)
    except (TypeError, ValueError):
        n = 0
    if todas_cuentas or n <= 0:
        return set(usuarios)
    return set(usuarios[:n])


def _tiene_sesion_cuenta(cuenta) -> bool:
    """True si la cuenta tiene una sesion reutilizable (auth_token/cookies).

    Mismo criterio del calentamiento: `auth_token`, `cookies_json` con
    contenido o `cookies_path` con archivo existente. Nunca lanza."""
    try:
        if str(getattr(cuenta, "auth_token", "") or "").strip():
            return True
        cookies_json = getattr(cuenta, "cookies_json", "")
        if cookies_json:
            if isinstance(cookies_json, str):
                return bool(cookies_json.strip())
            return bool(cookies_json)
        ruta = str(getattr(cuenta, "cookies_path", "") or "").strip()
        if not ruta:
            return False
        from core.config import resolver_ruta

        return os.path.isfile(resolver_ruta(ruta))
    except Exception:
        return False


def _seleccionar_reserva(candidatos, excluidos=None, maximo: int = 50,
                         rng=None) -> list:
    """Baraja y limita las cuentas de respaldo (helper PURO y testeable).

    Limpia '@'/vacios/duplicados (case-insensitive), descarta los `excluidos`
    (la seleccion principal de la campana) y baraja con `rng` (default:
    `random`). Devuelve hasta `maximo` usuarios (tope default 50 si el valor
    viene invalido/<=0)."""
    import random as _random

    generador = rng or _random
    vistos, limpios = set(), []
    for usuario in candidatos or []:
        nombre = str(usuario or "").strip().lstrip("@")
        if not nombre:
            continue
        clave = nombre.lower()
        if clave in vistos:
            continue
        vistos.add(clave)
        limpios.append(nombre)

    excluidos_norm = {
        str(u).strip().lstrip("@").lower()
        for u in (excluidos or [])
        if str(u).strip()
    }
    disponibles = [u for u in limpios if u.lower() not in excluidos_norm]
    try:
        generador.shuffle(disponibles)
    except Exception:
        pass
    try:
        tope = int(maximo)
    except (TypeError, ValueError):
        tope = 50
    if tope <= 0:
        tope = 50
    return disponibles[:tope]


def _cargar_reserva_usuarios(excluidos=None, seccion=None, maximo: int = 50) -> list:
    """Cuentas twitter activas CON sesion que NO estan en la seleccion principal.

    Mismo filtro de seccion si `seccion` viene ("" / None = sin filtro),
    barajadas y limitadas a `maximo`. Las cuentas PAUSADAS para activacion
    (`core.pausas`) quedan fuera tambien de la reserva. Se usan como respaldo
    cuando una cuenta agota su cuota diaria. Nunca lanza: ante cualquier error
    devuelve []."""
    try:
        from core.database import get_db_session
        from core.models import Cuenta
        from core.secciones import normalizar_seccion

        with get_db_session() as db:
            cuentas = (
                db.query(Cuenta)
                .filter(Cuenta.plataforma == "twitter", Cuenta.activa == True)
                .order_by(Cuenta.usuario)
                .all()
            )
        candidatos = []
        for c in cuentas:
            if es_pausada_activacion(c):
                continue
            if seccion and normalizar_seccion(getattr(c, "seccion", "")) != seccion:
                continue
            if not _tiene_sesion_cuenta(c):
                continue
            candidatos.append(str(getattr(c, "usuario", "") or ""))
        return _seleccionar_reserva(candidatos, excluidos=excluidos, maximo=maximo)
    except Exception:
        return []


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


def _invocar_lanzar(lanzar, callback, cancelar=None):
    """Llama `lanzar(callback, cancelar)` / `lanzar(callback)` / `lanzar()`.

    Decide por firma (sin capturar `TypeError` internos): con 2+ parametros
    posicionales pasa tambien el `threading.Event` de paro; con 1 pasa solo el
    callback; sin parametros no pasa nada. Asi los motores viejos que no
    soportan `cancelar` siguen funcionando igual.
    """
    import inspect

    try:
        parametros = inspect.signature(lanzar).parameters
    except (TypeError, ValueError):
        parametros = {}
    if not parametros:
        return lanzar()
    posicionales = [
        p for p in parametros.values()
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    if len(posicionales) >= 2:
        return lanzar(callback, cancelar)
    return lanzar(callback)


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


# ==================== PANEL PERSISTENTE DE PROCESO ====================

def _ss_get(clave, default=None):
    """`st.session_state.get` tolerante (fuera de un script devuelve default)."""
    try:
        return st.session_state.get(clave, default)
    except Exception:
        return default


def _ss_set(clave, valor) -> None:
    """Asigna `st.session_state[clave]` tolerante (no rompe en modo bare)."""
    try:
        st.session_state[clave] = valor
    except Exception:
        pass


def _en_contexto_streamlit() -> bool:
    """True solo dentro de un script de Streamlit (AppTest incluido).

    En modo bare (tests que llaman helpers sin runtime) es False, de modo que
    `_lanzar_con_progreso` no intenta `st.rerun()` ni pinta widgets."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def _fecha_corta(ts) -> str:
    """Timestamp -> "YYYY-MM-DD HH:MM:SS" local (tolerante)."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return str(ts or "")


def _estado_para_panel(entrada=None, snap=None, ahora=None) -> dict:
    """Estado PURO (sin `st.*`) de una campaña para el panel persistente.

    Devuelve {} sin entrada. Con entrada, un dict con el avance (0..1), el
    `tiempo_txt`, la linea de metricas/velocidad/fase, los eventos del feed,
    `puede_forzar` (paro pedido hace `PARO_FORZAR_SEG` y el hilo sigue vivo),
    `cancelada` y el resumen final. `_pintar_proceso` solo pinta este dict;
    asi la logica queda testeable sin Streamlit.
    """
    if not isinstance(entrada, dict):
        return {}
    if snap is None:
        snap = _snapshot_progreso(entrada.get("motor"))
    if ahora is None:
        ahora = time.monotonic()

    parametros = entrada.get("parametros") or {}
    estado = str(entrada.get("estado") or "")
    hechas = _entero(snap.get("hechas"))
    exitosas = _entero(snap.get("exitosas"))
    fallidas = _entero(snap.get("fallidas"))
    omitidas = _entero(snap.get("omitidas"))
    ronda_actual = _entero(snap.get("ronda_actual")) or 1
    fase_actual = snap.get("fase_actual")

    inicio_mono = entrada.get("inicio_mono")
    try:
        base = float(inicio_mono) if inicio_mono is not None else ahora
    except (TypeError, ValueError):
        base = ahora
    transcurrido = max(0.0, ahora - base)

    repetir = bool(parametros.get("repetir"))
    duracion_min = _entero(parametros.get("duracion_min"))
    if repetir:
        limite_segundos = max(1.0, duracion_min * 60.0)
        avance = min(1.0, transcurrido / limite_segundos)
        tiempo_txt = (
            f"⏱️ Faltan "
            f"{_formato_tiempo(max(0.0, limite_segundos - transcurrido))}"
        )
    else:
        total_estimado = _entero(entrada.get("total"))
        referencia = total_estimado if total_estimado > 0 else max(1, hechas)
        avance = min(1.0, hechas / max(1, referencia))
        tiempo_txt = f"⏳ Transcurrido {_formato_tiempo(transcurrido)}"

    linea_curva = ""
    if fase_actual not in (None, ""):
        linea_curva = f" · 🚀 Fase {fase_actual}/2 de la curva"
    ritmo = (hechas / transcurrido * 60.0) if transcurrido > 0 else 0.0
    linea = (
        f"**🔄 Ronda {ronda_actual}** · ✅ {exitosas} exitosas · "
        f"❌ {fallidas} fallidas · 🧮 {hechas} hechas · "
        f"⚡ {hechas} acciones · {ritmo:.1f}/min ≈ {ritmo * 60.0:.0f}/h · "
        f"{tiempo_txt}{linea_curva}"
    )

    hilo = entrada.get("hilo")
    try:
        vivo = bool(hilo is not None and hilo.is_alive())
    except Exception:
        vivo = False
    paro_solicitado = entrada.get("paro_solicitado")
    puede_forzar = False
    if estado == "deteniendo" and vivo and paro_solicitado is not None:
        try:
            puede_forzar = (time.time() - float(paro_solicitado)) >= PARO_FORZAR_SEG
        except (TypeError, ValueError):
            puede_forzar = False

    evento = entrada.get("evento")
    try:
        cancelada = bool(evento is not None and evento.is_set())
    except Exception:
        cancelada = False
    if snap.get("cancelada"):
        # El motor tambien reporta la cancelacion en su snapshot.
        cancelada = True

    resumen = entrada.get("resumen")
    return {
        "id": str(entrada.get("id") or ""),
        "estado": estado,
        "en_curso": estado in ("en_curso", "deteniendo"),
        "deteniendo": estado == "deteniendo",
        "terminada": estado in ("terminada", "cancelada", "error"),
        "tipo": str(entrada.get("tipo") or "citas"),
        "transcurrido": transcurrido,
        "avance": avance,
        "tiempo_txt": tiempo_txt,
        "hechas": hechas,
        "exitosas": exitosas,
        "fallidas": fallidas,
        "omitidas": omitidas,
        "ronda_actual": ronda_actual,
        "fase_actual": fase_actual,
        "linea": linea,
        "eventos": list(snap.get("eventos") or []),
        "vivo": vivo,
        "puede_forzar": puede_forzar,
        "cancelada": cancelada,
        "resumen": resumen if isinstance(resumen, dict) else {},
        "error": str(entrada.get("error") or ""),
    }


def _mostrar_resultados_citas(resultados: dict) -> None:
    """Metricas + detalle de una campana de Cita masiva (render de siempre)."""
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
    # Metricas opcionales de la curva/tiers/cuotas (solo si el motor las trae).
    metricas.extend(_metricas_tier_curva(resultados))
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


def _render_resultados(resumen, tipo: str = "citas") -> None:
    """Pinta el resumen final con el render que corresponde al tipo de campaña."""
    resumen = resumen if isinstance(resumen, dict) else {}
    if str(tipo or "") == "roles":
        _mostrar_resultados_roles(resumen)
    else:
        _mostrar_resultados_citas(resumen)


def _render_ultimo_desde_disco(prefix: str = "act") -> None:
    """Expander discreto con el ultimo estado guardado en disco (si existe).

    Se muestra cuando NO hay campaña en memoria (p. ej. tras reiniciar el
    proceso del dashboard): el proceso en vivo ya no se puede conectar, pero
    el JSON guardado deja ver como quedo el ultimo proceso."""
    datos = _leer_ultima_campana()
    if not datos:
        return
    progreso = datos.get("progreso") or {}
    resumen = datos.get("resumen") or {}
    with st.expander(
        "🗂️ Último proceso guardado (sin conexión en vivo)", expanded=False
    ):
        st.caption(
            "No hay campaña en memoria en esta sesión; este es el último estado "
            "guardado en disco. La tabla completa de acciones vive en Reportes."
        )
        columnas = st.columns(4)
        columnas[0].metric("Estado", str(datos.get("estado") or "—"))
        columnas[1].metric(
            "✅ Exitosas",
            _entero(resumen.get("exitosas", progreso.get("exitosas"))),
        )
        columnas[2].metric(
            "❌ Fallidas",
            _entero(resumen.get("fallidas", progreso.get("fallidas"))),
        )
        columnas[3].metric("🧮 Hechas", _entero(progreso.get("hechas")))
        actualizado = datos.get("fin") or datos.get("actualizado")
        if actualizado:
            st.caption(f"Última actualización: {_fecha_corta(actualizado)}")
        eventos = progreso.get("eventos") or []
        lineas = [_linea_evento(ev) for ev in eventos[-8:]]
        if lineas:
            st.markdown("  \n".join(lineas))


def _actualizar_json_proceso(entrada: dict, snap: dict | None = None) -> None:
    """Escribe el tick del panel en disco (throttle ~2s; nunca lanza)."""
    ahora = time.time()
    try:
        ultimo = float(entrada.get("ultimo_json") or 0.0)
    except (TypeError, ValueError):
        ultimo = 0.0
    if ahora - ultimo < 2.0:
        return
    entrada["ultimo_json"] = ahora
    _escribir_ultima_campana(_entrada_para_json(entrada, snap))


def _limpieza_contexto_si_termino(entrada: dict, estado: dict) -> None:
    """Programa la limpieza diferida del contexto si ESTA sesion lanzo la
    campaña y el checkbox la pidio. Tras un refresh la marca se pierde (la
    limpieza diferida es de la sesion que lanzo; limitacion aceptada)."""
    parametros = entrada.get("parametros") or {}
    if not parametros.get("limpiar_contexto"):
        return
    if _ss_get(CLAVE_CAMPANA_LANZADA) != entrada.get("id"):
        return
    if _ss_get(CLAVE_CAMPANA_LIMPIEZA) == entrada.get("id"):
        return
    _programar_limpieza_contexto(parametros.get("prefix") or "act")
    _ss_set(CLAVE_CAMPANA_LIMPIEZA, entrada.get("id"))


def _pintar_en_curso(prefix: str, entrada: dict, estado: dict) -> None:
    """Barra + metricas + feed + boton "⛔ Paro total" de una campaña viva."""
    st.markdown("#### 🚦 Proceso en vivo")
    st.progress(estado["avance"], text=estado["tiempo_txt"])
    st.markdown(estado["linea"])
    eventos = estado.get("eventos") or []
    if eventos:
        st.markdown("  \n".join(_linea_evento(ev) for ev in eventos[-8:]))
    else:
        st.markdown("⏳ Esperando las primeras cuentas…")
    if estado["deteniendo"]:
        st.warning("⛔ Paro solicitado: cerrando navegadores…")
    columnas = st.columns(2)
    with columnas[0]:
        if st.button(
            "⛔ Paro total",
            type="primary",
            key=f"{prefix}_btn_paro",
            disabled=estado["deteniendo"],
            help=(
                "Corta la campaña en segundos: el motor deja de abrir cuentas "
                "nuevas y cierra los navegadores; el guard se libera al morir "
                "el hilo."
            ),
        ):
            if _solicitar_paro():
                st.rerun()
    with columnas[1]:
        if estado["puede_forzar"]:
            st.warning(
                f"⚠️ La campaña sigue viva {PARO_FORZAR_SEG}s después del paro. "
                "Al forzar la liberación podrás lanzar otra de inmediato, pero "
                "la campaña vieja puede seguir cerrando navegadores."
            )
            if st.button("⚠️ Forzar liberación", key=f"{prefix}_btn_forzar"):
                _liberar_campana(entrada.get("id"))
                entrada["estado"] = "terminada"
                entrada["forzada"] = True
                if not entrada.get("error"):
                    entrada["error"] = "liberación forzada desde el panel"
                _escribir_ultima_campana(_entrada_para_json(entrada))
                st.rerun()


def _pintar_terminada(prefix: str, entrada: dict, estado: dict) -> None:
    """Panel "Último proceso" de una campaña terminada (resumen + Ocultar)."""
    if estado["estado"] == "error":
        st.error(
            "❌ Campaña interrumpida: "
            + (estado["error"] or "error inesperado del motor")
        )
    elif estado["cancelada"] or estado["estado"] == "cancelada":
        st.success("⛔ Campaña detenida")
        st.caption(
            "El guard quedó libre: ya puedes lanzar otra campaña de inmediato."
        )
    else:
        resumen = estado.get("resumen") or {}
        linea = (
            f"✅ Campaña finalizada: {_entero(resumen.get('exitosas'))} ok / "
            f"{_entero(resumen.get('fallidas'))} errores"
        )
        rondas = resumen.get("rondas")
        if rondas not in (None, ""):
            linea += f" · {rondas} rondas"
        st.success(linea)
    st.markdown("#### 🧾 Último proceso")
    _render_resultados(estado.get("resumen") or {}, estado.get("tipo") or "citas")
    if st.button("🧹 Ocultar", key=f"{prefix}_btn_ocultar"):
        _ss_set(CLAVE_CAMPANA_OCULTA, entrada.get("id"))
        st.rerun()


def _pintar_proceso(prefix: str = "act") -> None:
    """Pinta el panel persistente: proceso en vivo, ultimo proceso o disco.

    Lee SIEMPRE del registro de modulo (no de `st.session_state`), por lo que
    el panel reaparece aunque el usuario recargue o cambie de operacion."""
    entrada = _campana_en_curso() or _campana_actual()
    if entrada is None:
        _render_ultimo_desde_disco(prefix)
        return
    snap = _snapshot_progreso(entrada.get("motor"))
    estado = _estado_para_panel(entrada, snap)
    if not estado.get("en_curso"):
        if _ss_get(CLAVE_CAMPANA_OCULTA) == entrada.get("id"):
            return
        _limpieza_contexto_si_termino(entrada, estado)
        _pintar_terminada(prefix, entrada, estado)
        return
    _pintar_en_curso(prefix, entrada, estado)
    _actualizar_json_proceso(entrada, snap)


def _refrescar_con_fragmento(prefix: str) -> bool:
    """Registra un `st.fragment(run_every=1s)` que repinta el proceso.

    Devuelve False si esta version de Streamlit no soporta fragmentos (el
    llamador cae al bucle `time.sleep(1); st.rerun()`). Cuando la campaña
    termina, el fragmento dispara UN rerun completo y deja de refrescar."""
    fabrica = getattr(st, "fragment", None)
    if not callable(fabrica):
        return False

    def _pasada():
        if _campana_en_curso() is None:
            st.rerun(scope="app")
            return
        _pintar_proceso(prefix)

    try:
        decorada = fabrica(run_every=1.0)(_pasada)
    except Exception:
        return False
    decorada()
    return True


def _render_proceso_activo(prefix: str = "act", max_pasos=None) -> None:
    """Panel de proceso persistente de una pestana (auto-refresco en vivo).

    - Con campaña en curso: usa `st.fragment(run_every=1s)` si esta disponible
      y, si no, el bucle `time.sleep(1); st.rerun()` (SOLO mientras corre).
    - Sin campaña en curso: UNA pasada (ultimo proceso en memoria o el
      expander con el JSON de disco).

    `max_pasos` es un flag interno para tests/llamadas acotadas: con un valor
    (p. ej. 1) se pinta UNA sola pasada, sin fragmentos ni bucles."""
    if max_pasos is not None:
        _pintar_proceso(prefix)
        return
    if _campana_en_curso() is None:
        _pintar_proceso(prefix)
        return
    if _refrescar_con_fragmento(prefix):
        return
    _pintar_proceso(prefix)
    time.sleep(1)
    st.rerun()


def _motor_usa_cancelar(motor, tipo: str = "citas") -> bool:
    """True si el motor acepta el kwarg `cancelar` en el flujo del `tipo`.

    Se comprueba la firma del metodo que la pagina va a llamar
    (`ejecutar_por_roles` para "roles"; `ejecutar` para el resto). Con un motor
    viejo devuelve False y el paro usa `motor.solicitar_paro()` como fallback.
    """
    nombre = "ejecutar_por_roles" if str(tipo or "") == "roles" else "ejecutar"
    func = getattr(motor, nombre, None)
    return bool(func is not None and _soporta_kwarg(func, "cancelar"))


def _lanzar_motor(func, base_kwargs: dict):
    """Envuelve una llamada al motor en un callable `(callback, cancelar)`.

    `cancelar` (threading.Event) se pasa SOLO si el motor lo soporta
    (`_soporta_kwarg`): asi un motor viejo sigue funcionando igual y el paro
    cae a `motor.solicitar_paro()` como fallback. Los call sites construyen
    `base_kwargs` con el resto de argumentos (urls, textos, curva, reserva...).
    """
    soporta_cancelar = _soporta_kwarg(func, "cancelar")

    def _lanzar(cb, cancelar=None):
        extra = dict(base_kwargs)
        if cancelar is not None and soporta_cancelar:
            extra["cancelar"] = cancelar
        return func(callback=cb, **extra)

    return _lanzar


def _lanzar_con_progreso(lanzar, motor, duracion_min: int, repetir: bool,
                         tipo: str = "citas", parametros: dict | None = None,
                         previos_env=None, prefix: str = "act",
                         info_lanzamiento=None) -> dict:
    """Lanza el motor en un hilo y REGISTRA la campaña (panel persistente).

    Mantiene la firma publica `(lanzar, motor, duracion_min, repetir)`; el
    resto de parametros son opcionales:

    - `lanzar` es un callable que llama al motor (bloqueante) y devuelve el
      resumen. Si acepta un segundo argumento recibe el `threading.Event` de
      paro (`_invocar_lanzar`); si no, solo el callback del total.
    - La campaña se registra en `_CAMPANAS` ANTES de arrancar el hilo y el
      `finally` del runner la finaliza: estado/fin/resumen/error, restauracion
      de las env de velocidad (`previos_env`) y liberacion del guard CON TOKEN
      (el hilo viejo nunca libera el guard de una campaña nueva).
    - Ya NO pinta el bucle en vivo: delega en `_render_proceso_activo` (panel
      persistente a nivel modulo, sobrevive a refresh/navegacion). Fuera de un
      script de Streamlit (tests en modo bare) espera al hilo y devuelve el
      resumen.

    Guard de campaña unica: si otra campaña ya esta en curso, repinta el panel
    al recargar (con su boton "⛔ Paro total") y devuelve `{}`."""
    if not _adquirir_campana():
        if _en_contexto_streamlit():
            # El panel persistente (al inicio de la pestana) mostra la campaña
            # en curso con su boton de paro: no dejamos al usuario sin salida.
            st.rerun()
        else:
            st.error(
                "⚠️ Ya hay una campaña de activación en curso. Espera a que "
                "termine antes de lanzar otra: dos campañas simultáneas "
                "saturan Chrome (tab crashed) y hacen más lenta la que corre."
            )
        return {}

    resultado: dict = {}
    referencia: dict = {"entrada": None}
    id_campana = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
    evento = threading.Event()
    parametros_camp = dict(parametros or {})
    parametros_camp.setdefault("duracion_min", int(duracion_min or 0))
    parametros_camp.setdefault("repetir", bool(repetir))
    parametros_camp["prefix"] = prefix
    usa_cancelar = _motor_usa_cancelar(motor, tipo)

    def _cb_total(hechas, total, usuario, ok):
        """Callback del motor: SOLO guarda el total (corre en otro hilo)."""
        try:
            total_int = int(total or 0)
        except (TypeError, ValueError):
            total_int = 0
        entrada = referencia.get("entrada")
        if entrada is not None:
            entrada["total"] = total_int

    def _runner():
        error = None
        try:
            resultado["resumen"] = _invocar_lanzar(lanzar, _cb_total, evento)
        except BaseException as e:
            resultado["error"] = e
            error = e
        finally:
            # Estado final + restauracion de envs + liberacion del guard con
            # token (si otra campaña ya tomo el relevo, no se toca).
            _finalizar_campana(
                id_campana, resumen=resultado.get("resumen"), error=error
            )
            _liberar_campana(id_campana)

    hilo = threading.Thread(target=_runner, daemon=True)
    _registrar_campana(
        motor=motor, hilo=hilo, evento=evento, tipo=tipo,
        parametros=parametros_camp, previos_env=previos_env,
        usa_cancelar=usa_cancelar, id_campana=id_campana,
    )
    referencia["entrada"] = _campana_actual()
    if info_lanzamiento is not None:
        info_lanzamiento["registrada"] = True
    en_streamlit = _en_contexto_streamlit()
    if en_streamlit:
        _ss_set(CLAVE_CAMPANA_LANZADA, id_campana)
    try:
        hilo.start()
    except BaseException:
        # Si el hilo no llego a arrancar, nadie mas liberaria el guard.
        _finalizar_campana(id_campana, resumen=resultado.get("resumen"))
        _liberar_campana(id_campana)
        if previos_env is not None:
            _restaurar_opciones_velocidad(previos_env)
        raise

    if en_streamlit:
        # El panel persistente toma el relevo en el rerun (auto-refresco vivo).
        st.rerun()
        return {}
    hilo.join(timeout=10.0)
    return resultado.get("resumen") or {}


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


def _caption_cuota_diaria() -> None:
    """Caption del tope diario vigente (lee la config existente, sin envs)."""
    limite = _limite_diario_config()
    if limite > 0:
        st.caption(
            f"Tope diario por cuenta: **{limite} acciones** "
            "(`LIMITE_DIARIO_POR_CUENTA`). Al alcanzarlo, la cuenta pasa a "
            "'Agotada por hoy' y (si hay respaldo) rota a otra."
        )
    else:
        st.caption(
            "Tope diario por cuenta: **sin tope** "
            "(`LIMITE_DIARIO_POR_CUENTA=0`)."
        )


def _parametros_curva_reserva(func, curva: bool, curva_fase1, reserva_activa: bool,
                              reserva_usuarios) -> dict:
    """Kwargs opcionales de curva/reserva SOLO si `func` (el motor) los acepta.

    Devuelve el dict para llamar al motor sin romper versiones viejas:
      - `curva_aceleracion=True` + `curva_fase1_min=N` si el checkbox esta
        activo Y el motor soporta los kwargs; si no, `st.warning` sin bloquear.
      - `reserva_usuarios=[...]` si la rotacion esta activa Y hay candidatas Y
        el motor soporta el kwarg; sin candidatas muestra un caption (no
        bloquea).
    Nunca lanza."""
    parametros = {}
    try:
        if curva:
            if _soporta_kwarg(func, "curva_aceleracion"):
                parametros["curva_aceleracion"] = True
                if _soporta_kwarg(func, "curva_fase1_min"):
                    parametros["curva_fase1_min"] = int(curva_fase1)
            else:
                st.warning(
                    "🚀 Este motor todavía no soporta la Curva de Aceleración; "
                    "la campaña se lanzará sin ella."
                )

        lista_reserva = [
            str(u).strip()
            for u in (reserva_usuarios or [])
            if str(u).strip()
        ]
        if reserva_activa:
            if not lista_reserva:
                st.caption(
                    "🛡️ Sin cuentas de respaldo disponibles con los filtros "
                    "actuales; la campaña no se bloquea."
                )
            elif _soporta_kwarg(func, "reserva_usuarios"):
                parametros["reserva_usuarios"] = lista_reserva
            else:
                st.warning(
                    "🛡️ Este motor todavía no soporta cuentas de respaldo; se "
                    "ignora la reserva."
                )
    except Exception:
        pass
    return parametros


def _lanzar_con_progreso_o_limpiar(lanzar, motor, duracion_min: int, repetir: bool,
                                   prefix: str, limpiar: bool,
                                   tipo: str = "citas",
                                   parametros: dict | None = None,
                                   previos_env=None,
                                   info_lanzamiento=None) -> dict:
    """`_lanzar_con_progreso` + limpieza diferida del contexto al volver.

    Si `limpiar` y el lanzamiento devuelve un resumen (modo bare/tests), se
    programa la limpieza del contexto de `prefix`; en Streamlit el lanzamiento
    sale por `st.rerun()` y la limpieza la re-programa el panel persistente al
    terminar la campaña (`_limpieza_contexto_si_termino`). Si el guard de
    campaña unica rechaza el lanzamiento (`{}`), no se limpia nada.

    La limpieza SOLO vive en `st.session_state` de la sesion que lanzo: si el
    usuario recarga la pagina a mitad de campaña, se pierde (limitacion
    aceptable). El panel persistente re-programa la limpieza al terminar si la
    sesion sigue viva (ver `_limpieza_contexto_si_termino`). Los parametros
    extra viajan al registro de la campaña (tipo/JSON/panel)."""
    parametros_camp = dict(parametros or {})
    parametros_camp["limpiar_contexto"] = bool(limpiar)
    parametros_camp["prefix"] = prefix
    try:
        resultados = _lanzar_con_progreso(
            lanzar, motor, duracion_min, repetir, tipo=tipo,
            parametros=parametros_camp, previos_env=previos_env, prefix=prefix,
            info_lanzamiento=info_lanzamiento,
        )
    except BaseException:
        # OJO: un lanzamiento exitoso sale por `st.rerun()` (RerunException),
        # asi que NO se limpia aqui: la limpieza de una campaña registrada la
        # re-programa el panel persistente al terminar (sesion que lanzo).
        raise
    if limpiar and resultados:
        _programar_limpieza_contexto(prefix)
    return resultados


# ============================ OPCIONES DE VELOCIDAD ============================

# Variables de entorno que leen los modulos del bot/motor al construir cada
# navegador o peticion (nunca se editan esos modulos: solo se cambian aqui
# antes de lanzar la campana y se restauran al terminar).
#   - `RT_POR_API` se mantiene como alias/compat de `API_PRIMERO` (maestro).
#   - `MAX_WORKERS` es numerico: trabajadores en paralelo del motor.
#   - `ACTIVACION_PERMITIR_PASSWORD`: "1" permite el login lento con
#     password/TOTP cuando la cuenta no tiene cookies validas (default "0").
# Los disyuntores de API (`API_BREAKER_FALLOS`/`API_BREAKER_SEG`) NO se
# escriben desde aqui: solo se documentan en la UI y se ajustan por env.
_VARS_VELOCIDAD = (
    "TWITTER_SIN_PROXY",
    "CHROME_SIN_IMAGENES",
    "RT_POR_API",
    "API_PRIMERO",
    "MAX_WORKERS",
    "ACTIVACION_PERMITIR_PASSWORD",
    "MODO_PESTANA",
    "PESTANA_MAX_ACCIONES",
)


def _aplicar_opciones_velocidad(sin_proxy: bool, sin_imagenes: bool,
                                rt_api: bool, api_primero: bool = None,
                                max_workers: int = 8,
                                permitir_password: bool = False,
                                modo_pestana: bool = True,
                                pestana_max_acciones: int = 40) -> dict:
    """Escribe las env de velocidad y devuelve sus valores PREVIOS.

    Convencion "1"/"0" para los interruptores; `MAX_WORKERS` y
    `PESTANA_MAX_ACCIONES` se escriben como enteros. `RT_POR_API` y
    `API_PRIMERO` son el MISMO interruptor (alias/compatibilidad): si
    cualquiera de los dos esta activo, ambos van a "1". Si no se pasa
    `api_primero`, se usa el valor de `rt_api` (llamadas viejas).
    `permitir_password` (default False = OFF) controla
    `ACTIVACION_PERMITIR_PASSWORD`.

    Pestañas persistentes: `modo_pestana` (default True = ON) controla
    `MODO_PESTANA` ("1"/"0") y `pestana_max_acciones` (default 40; clamp
    5..200) el reciclado de la pestaña tras N acciones
    (`PESTANA_MAX_ACCIONES`). Los kwargs nuevos tienen defaults para no romper
    llamadas viejas.

    Los valores previos (o `None` si la variable no existia) se devuelven para
    poder restaurarlos con `_restaurar_opciones_velocidad` al terminar la
    campana, de modo que el resto del dashboard no herede las opciones de una
    campana.
    """
    if api_primero is None:
        api_primero = rt_api
    api_activa = "1" if (bool(rt_api) or bool(api_primero)) else "0"
    try:
        workers = str(int(max_workers))
    except (TypeError, ValueError):
        workers = "8"
    try:
        max_pestana = int(pestana_max_acciones)
    except (TypeError, ValueError):
        max_pestana = 40
    max_pestana = min(200, max(5, max_pestana))
    valores = {
        "TWITTER_SIN_PROXY": "1" if sin_proxy else "0",
        "CHROME_SIN_IMAGENES": "1" if sin_imagenes else "0",
        "RT_POR_API": api_activa,
        "API_PRIMERO": api_activa,
        "MAX_WORKERS": workers,
        "ACTIVACION_PERMITIR_PASSWORD": (
            "1" if permitir_password else "0"
        ),
        "MODO_PESTANA": "1" if modo_pestana else "0",
        "PESTANA_MAX_ACCIONES": str(max_pestana),
    }
    previos = {clave: os.environ.get(clave) for clave in _VARS_VELOCIDAD}
    for clave, valor in valores.items():
        os.environ[clave] = valor
    return previos


def _restaurar_opciones_velocidad(previos: dict) -> None:
    """Restaura las env a `previos`; si no existian, las ELIMINA.

    Cubre TODAS las variables de `_VARS_VELOCIDAD` (incluida
    `ACTIVACION_PERMITIR_PASSWORD`). Nunca lanza: corre en el `finally` de la
    campana y no debe tapar el error original si algo falla al restaurar.
    """
    for clave, valor in (previos or {}).items():
        try:
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = str(valor)
        except Exception:
            pass


def _caption_disyuntores_api() -> None:
    """Caption comun (ambas pestanas) sobre el disyuntor de la API.

    El motor de API usa un disyuntor: si X rechaza las peticiones (anti-bot o
    limite), deja de intentar la API por un tiempo y pasa a Selenium solo. No
    se expone en la UI para no complicarla; si el proceso ya trae overrides por
    env, se muestran al final del caption como estado.
    """
    overrides = []
    for var in ("API_BREAKER_FALLOS", "API_BREAKER_SEG"):
        valor = (os.environ.get(var) or "").strip()
        if valor:
            overrides.append(f"`{var}={valor}`")
    sufijo = (" Ahora por env: " + " · ".join(overrides) + ".") if overrides else ""
    st.caption(
        "🧯 Si X rechaza la API (anti-bot/límite), el sistema deja de "
        "intentarla y pasa a Selenium automáticamente; puedes ajustar "
        "`API_BREAKER_FALLOS`/`API_BREAKER_SEG` por env." + sufijo
    )


def _lanzar_con_opciones_velocidad(lanzar, motor, duracion_min: int,
                                   repetir: bool, prefix: str, limpiar: bool,
                                   sin_proxy: bool, sin_imagenes: bool,
                                   rt_api: bool, api_primero: bool = None,
                                   max_workers: int = 8,
                                   permitir_password: bool = False,
                                   modo_pestana: bool = True,
                                   pestana_max_acciones: int = 40,
                                   tipo: str = "citas",
                                   parametros: dict | None = None) -> dict:
    """`_lanzar_con_progreso_o_limpiar` aplicando y restaurando la velocidad.

    Las env (`TWITTER_SIN_PROXY`, `CHROME_SIN_IMAGENES`, `RT_POR_API`,
    `API_PRIMERO`, `MAX_WORKERS`, `ACTIVACION_PERMITIR_PASSWORD`,
    `MODO_PESTANA` y `PESTANA_MAX_ACCIONES`) se escriben ANTES de lanzar (el
    motor las lee al abrir cada navegador/hacer cada peticion) y se restauran a
    sus valores previos SIEMPRE: si la campaña se registra, el `finally` del
    hilo runner las restaura via `_finalizar_campana` (asi un refresh a mitad
    de campaña no deja las env colgadas); si el lanzamiento se rechaza o falla,
    se restauran aqui mismo (idempotente). `api_primero=None` usa `rt_api`
    (alias/compat). `tipo`/`parametros` viajan al registro (panel y JSON).

    Devuelve lo mismo que `_lanzar_con_progreso`: el resumen en modo bare o {}.
    """
    previos = _aplicar_opciones_velocidad(
        sin_proxy, sin_imagenes, rt_api,
        api_primero=api_primero, max_workers=max_workers,
        permitir_password=permitir_password,
        modo_pestana=modo_pestana,
        pestana_max_acciones=pestana_max_acciones,
    )
    info = {"registrada": False}
    try:
        return _lanzar_con_progreso_o_limpiar(
            lanzar, motor, duracion_min, repetir, prefix=prefix, limpiar=limpiar,
            tipo=tipo, parametros=parametros, previos_env=previos,
            info_lanzamiento=info,
        )
    finally:
        if not info.get("registrada"):
            _restaurar_opciones_velocidad(previos)


# ============================ ACCESO A DATOS ============================

def _cargar_cuentas_con_roles() -> list:
    """Cuentas twitter activas como dicts listos para `_selector_masivo`."""
    from core.database import get_db_session
    from core.models import Cuenta
    from core.registros import normalizar_tipo_cuenta
    from core.roles import normalizar_rol_activacion
    from core.secciones import normalizar_seccion

    try:
        from core.tiers import normalizar_tier
    except Exception:
        def normalizar_tier(valor):
            return ""

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
                    "tier_calidad": normalizar_tier(
                        getattr(c, "tier_calidad", "")
                    ),
                    # Pausa para activacion (solo clientes): las paginas la
                    # excluyen de campanas y reservas con `_helpers`.
                    "pausada_activacion": bool(
                        getattr(c, "pausada_activacion", False)
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
    # Metricas opcionales de la curva/tiers/cuotas (solo si el motor las trae).
    metricas.extend(_metricas_tier_curva(resultados))
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
    """Pestana A: quote-RTs masivos con variaciones (formulario original).

    La vista principal deja solo lo esencial (URLs, texto base, hashtags,
    seccion, cantidad, duracion y boton de lanzar); cohortes, navegadores,
    trabajadores, repetir/porcentajes, dar like, grupo, limpieza y las
    opciones de velocidad viven en «⚙️ Opciones avanzadas» con los valores
    recomendados ya fijos (no hace falta tocar nada para lanzar)."""
    # Limpieza diferida del contexto (fin de campana / "Limpiar contexto"):
    # SIEMPRE antes de crear cualquier widget de la pestana.
    _aplicar_limpieza_contexto_pendiente("act")

    # Panel de proceso persistente: si hay una campaña corriendo (aunque la
    # haya lanzado otra sesion o esta pagina se haya recargado), se muestra el
    # proceso en vivo con su boton "⛔ Paro total" y NO se repinta el formulario.
    _render_proceso_activo(prefix="act")
    if _campana_en_curso() is not None:
        st.caption(
            "🚦 Hay una campaña en curso: usa el panel de arriba para "
            "detenerla o espera a que termine para lanzar otra."
        )
        return

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
    # Pausadas para activacion (clientes): FUERA de la campana (siguen en
    # mantenimiento). El aviso muestra el conteo excluido.
    cuentas_activas, pausadas_activas = separar_pausadas(cuentas_activas)
    aviso_pausadas(pausadas_activas)
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

    col_cant, col_dur = st.columns(2)
    with col_cant:
        cantidad = st.number_input(
            "Cantidad de cuentas (vacío = todas)",
            min_value=0, max_value=1000, value=0, step=10, key="act_cant",
        )
    with col_dur:
        duracion_min = st.number_input(
            "Duración (min)", min_value=1, max_value=360, value=60, step=5,
            key="act_dur",
        )

    st.markdown("#### 🚀 Estrategia")
    curva = st.checkbox(
        "🚀 Estrategia: Curva de Aceleración",
        value=False,
        key="act_curva",
        help=(
            "Arranca con pocas cuentas (Fase 1) y acelera el volumen en la "
            "Fase 2: la campaña se ve más orgánica y evita ráfagas que X "
            "castiga. Se combina con la rotación a cuentas de respaldo. "
            "Fase 1: solo Tier 1 (Tier 2 y Tier 3 ignorados). Fase 2: se "
            "liberan Tier 2 y Tier 3; el Tier 3 solo dispara RT/likes en "
            "cascada a los posts de los Tier 1."
        ),
    )
    curva_fase1 = 15
    if curva:
        curva_fase1 = st.number_input(
            "Fase 1 (min)",
            min_value=1,
            max_value=60,
            value=15,
            step=1,
            key="act_curva_fase1",
            help="Duración de la primera fase de la curva (1-60 min).",
        )

    with st.expander("🛡️ Cuotas diarias y respaldo", expanded=False):
        reserva_rotar = st.checkbox(
            "♻️ Rotar a cuentas de respaldo al agotar la cuota diaria",
            value=True,
            key="act_reserva_rotar",
            help=(
                "Cuando una cuenta alcanza su tope diario ('Agotada por hoy'), "
                "el motor la sustituye por una cuenta de respaldo con sesión "
                "válida para no frenar la campaña."
            ),
        )
        reserva_max = st.number_input(
            "Máx. cuentas de respaldo",
            min_value=1,
            max_value=200,
            value=50,
            step=1,
            key="act_reserva_max",
            help=(
                "Tope de cuentas de respaldo que se pasan al motor: activas, "
                "con sesión, fuera de esta campaña y de la misma sección."
            ),
        )
        _caption_cuota_diaria()

    with st.expander(
        "📰 Contexto desde noticias (opcional, solo trasfondo)", expanded=False
    ):
        panel_noticias = _panel_contexto_noticias("act")

    with st.expander("⚙️ Opciones avanzadas (ya vienen configuradas)", expanded=False):
        st.caption(
            "Valores recomendados ya fijos: 2 navegadores, 12 trabajadores, "
            "pestaña persistente, repetir por rondas (40-90% de cuentas), sin "
            "proxy, sin API y pausa anti-spam. Cámbialos solo si sabes lo que "
            "haces."
        )
        col_coh, col_nav, col_work = st.columns(3)
        with col_coh:
            cohortes = st.number_input(
                "Cohortes", min_value=1, max_value=24, value=4, step=1, key="act_coh",
            )
        with col_nav:
            navegadores = st.number_input(
                "Navegadores simultáneos",
                min_value=1, max_value=30, value=_navegadores_default(), step=1,
                key="act_nav",
                help=(
                    "Cada navegador ejecuta una cuenta a la vez. En Railway NO "
                    "conviene pasar de 3-4: cada Chrome consume RAM/CPU/hilos y, "
                    "si uno crashea, la campaña se frena en cascada (variable "
                    "MAX_BROWSERS)."
                ),
            )
        with col_work:
            max_workers = st.number_input(
                "Trabajadores simultáneos (acciones en paralelo)",
                min_value=6, max_value=30, value=12, step=1,
                key="act_workers",
                help=(
                    "Acciones que el motor ejecuta a la vez cuando la API va "
                    "primero (variable MAX_WORKERS): con API primero este es el "
                    "paralelismo REAL; súbelo a 16-24 si la API responde. Los "
                    "navegadores solo se abren cuando la API falla."
                ),
            )

        col_like, col_grupo = st.columns(2)
        with col_like:
            dar_like = st.checkbox("Dar like también", value=False, key="act_like")
        with col_grupo:
            grupo = st.text_input("Filtrar por grupo (A/B/C, opcional)", key="act_grupo")

        repetir = st.checkbox(
            "🔁 Repetir hasta agotar el tiempo (textos nuevos en cada ronda)",
            value=True,
            key="act_repetir",
            help=(
                "Cada cuenta sigue trabajando en rondas hasta agotar la "
                "duración, con textos nuevos regenerados en cada ronda."
            ),
        )
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

        st.markdown("##### 🏎️ Opciones de velocidad (recomendadas)")
        sin_proxy = st.checkbox(
            "🌐 Sin proxy: usar la IP del servidor (Railway)",
            value=False,
            key="act_sin_proxy",
            help=(
                "Las peticiones salen directo desde Railway (más rápido y sin "
                "GB de proxy). Úsalo solo en pruebas: compartir una IP de "
                "datacenter entre muchas cuentas puede hacer que X las "
                "bloquee o limite."
            ),
        )
        sin_imagenes = st.checkbox(
            "🖼️ No cargar imágenes ni video (más rápido)",
            value=True,
            key="act_sin_imagenes",
            help=(
                "Chrome carga las páginas sin imágenes/video; las acciones de "
                "texto (post/RT/comentario) no las necesitan. Ahorra RAM y "
                "datos."
            ),
        )
        modo_pestana = st.checkbox(
            "🪟 Modo pestaña persistente (reutiliza Chrome y cambia la cuenta "
            "en la misma pestaña)",
            value=True,
            key="act_pestana",
            help=(
                "El motor abre un Chrome por worker y lo conserva toda la "
                "campaña; cada cuenta cambia su sesión (cookies + UA + proxy) "
                "en la misma pestaña, como la app de referencia (variable "
                "MODO_PESTANA)."
            ),
        )
        pestana_max_acciones = st.number_input(
            "♻️ Reciclar pestaña cada N acciones",
            min_value=5, max_value=200, value=40, step=5,
            key="act_pestana_max",
            help=(
                "Al llegar al límite se cierra y se abre otra; sirve para no "
                "acumular memoria/caché."
            ),
        )
        rt_api = st.checkbox(
            "⚡ Publicar por API (RT, likes, posts, comentarios y citas)",
            value=False,
            key="act_rt_api",
            help=(
                "X está bloqueando/limitando la API (anti-bot 226 / límite "
                "344) y puede marcar cuentas; con pestaña persistente Selenium "
                "es rápido. Actívala solo si tu API responde estable "
                "(variables API_PRIMERO/RT_POR_API). Si la API falla, se "
                "reintenta automáticamente con Chrome."
            ),
        )
        permitir_password = st.checkbox(
            "🔑 Permitir login con password/TOTP en campañas (lento)",
            value=False,
            key="act_permitir_password",
            help=(
                "Si una cuenta no tiene cookies válidas, permite el login con "
                "la contraseña guardada (+TOTP) durante la campaña. Es MUY "
                "lento (~20-40s por cuenta); déjalo apagado salvo lotes "
                "pequeños o cuentas recién creadas (variable "
                "ACTIVACION_PERMITIR_PASSWORD)."
            ),
        )
        _caption_disyuntores_api()

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

        # Cuentas de respaldo (misma sección, activas, con sesión y fuera de
        # la selección principal) SOLO si la rotación está activa.
        reserva_usuarios = []
        if reserva_rotar:
            reserva_usuarios = _cargar_reserva_usuarios(
                excluidos=_excluidos_cita(cuentas_activas, cantidad, todas_cuentas),
                seccion=(secciones_param[0] if secciones_param else None),
                maximo=int(reserva_max),
            )

        motor = MotorActivacion(max_concurrente=int(navegadores))
        parametros_extra = _parametros_curva_reserva(
            motor.ejecutar,
            curva,
            curva_fase1,
            reserva_rotar,
            reserva_usuarios,
        )
        lanzar = _lanzar_motor(
            motor.ejecutar,
            {
                "urls": urls,
                "texto_base": texto_base,
                "narrativa": panel_noticias["contexto"],
                "cantidad_cuentas": (
                    None if todas_cuentas else (int(cantidad) if cantidad > 0 else None)
                ),
                "grupo": grupo.strip() or None,
                "dar_like": dar_like,
                "duracion_min": int(duracion_min),
                "cohortes": int(cohortes),
                "hashtags": hashtags,
                "repetir": bool(repetir),
                "solo_con_registro": bool(todas_cuentas),
                "secciones": secciones_param,
                "porcentaje_min_ronda": int(pct_min),
                "porcentaje_max_ronda": int(pct_max),
                **parametros_extra,
            },
        )
        _lanzar_con_opciones_velocidad(
            lanzar,
            motor,
            duracion_min=int(duracion_min),
            repetir=bool(repetir),
            prefix="act",
            limpiar=bool(limpiar_contexto_al_terminar),
            sin_proxy=bool(sin_proxy),
            sin_imagenes=bool(sin_imagenes),
            rt_api=bool(rt_api),
            api_primero=bool(rt_api),
            max_workers=int(max_workers),
            permitir_password=bool(permitir_password),
            modo_pestana=bool(modo_pestana),
            pestana_max_acciones=int(pestana_max_acciones),
            tipo="citas",
            parametros={
                "duracion_min": int(duracion_min),
                "repetir": bool(repetir),
                "curva_aceleracion": bool(curva),
                "curva_fase1_min": int(curva_fase1) if curva else None,
            },
        )
        # El panel persistente (arriba de la pestana) pinta el proceso en vivo
        # y, al terminar, el resumen: aqui no se pinta nada mas.


def _por_roles():
    """Pestana B: subcuentas por rol (reparto por porcentajes + lanzar).

    La vista principal deja lo esencial: selector de cuentas, «🎚️ Reparto por
    porcentajes», seccion, modo sin tweet ancla + URLs,
    contexto/hashtags/menciones, duracion y el boton de lanzar. La campana usa
    SIEMPRE el rol guardado de cada cuenta (`roles_aleatorios=False`: el motor
    lo sigue soportando, pero la UI ya no ofrece el modo aleatorio). La
    asignacion manual de roles sueltos, los conteos y todo el resto (cohortes,
    navegadores, trabajadores, cooldown, pausa anti-spam, repetir/porcentajes
    y opciones de velocidad) viven en «⚙️ Opciones avanzadas» con los valores
    recomendados ya fijos."""
    from core.roles import ROLES_ACTIVACION, etiqueta_rol_activacion, normalizar_rol_activacion

    # Import perezoso: cuentas.py importa Streamlit y compania y solo se
    # necesita al abrir esta pestana (evita acoplar el arranque del dashboard).
    from web.operaciones.cuentas import _selector_masivo

    # Limpieza diferida del contexto (fin de campana / "Limpiar contexto"):
    # SIEMPRE antes de crear cualquier widget de la pestana (incluido
    # `act_roles_contexto`).
    _aplicar_limpieza_contexto_pendiente("act_roles")

    # Panel de proceso persistente (mismo comportamiento que la pestana A):
    # campaña en curso -> proceso en vivo con paro y sin formulario.
    _render_proceso_activo(prefix="act_roles")
    if _campana_en_curso() is not None:
        st.caption(
            "🚦 Hay una campaña en curso: usa el panel de arriba para "
            "detenerla o espera a que termine para lanzar otra."
        )
        return

    cuentas = _cargar_cuentas_con_roles()
    # Pausadas para activacion (clientes): FUERA de la campana (siguen en
    # mantenimiento). El aviso muestra el conteo excluido.
    cuentas, pausadas = separar_pausadas(cuentas)
    aviso_pausadas(pausadas)
    st.caption(f"Cuentas twitter activas: **{len(cuentas)}**")

    if not cuentas:
        st.info(
            "No hay cuentas twitter activas. Importa/activa cuentas en "
            "'🗂️ Cuentas: Perfiles, Secciones & Nombres'."
        )
        return

    # ---------------- Cuentas objetivo ----------------
    st.markdown("### 👥 Cuentas objetivo")
    seleccion = _selector_masivo(cuentas, "act_roles_selector")
    usuarios_sel = [f.get("usuario") for f in seleccion if f.get("usuario")]
    filas_por_usuario = {
        str(f.get("usuario") or ""): f for f in cuentas if f.get("usuario")
    }

    # ---------------- Reparto por porcentajes ----------------
    # El usuario elige las cuentas (selector de arriba, p. ej. modo Filtro ->
    # Sección: Libertad), define el % de cada rol y lo aplica. La campana usa
    # SIEMPRE el rol guardado (`Cuenta.rol_activacion`) que deja este reparto.
    st.markdown("#### 🎚️ Reparto por porcentajes")
    st.caption(
        "Elige la sección en el selector de arriba (modo **Filtro**, p. ej. "
        "**Libertad**), define cuánto hace cada rol y aplica el reparto: la "
        "campaña usará el rol guardado de cada cuenta."
    )
    st.caption(
        "🛡️ **Tiers**: las cuentas **Tier 2 (Volumen/Aged)** no pueden recibir "
        "el rol **Hashtags y menciones** (el reparto y el lanzamiento lo "
        "bloquean); solo hacen RT, Cita o Comentario. Las **Tier 3 (Métricas/"
        "Soporte)** SOLO hacen RT y likes: el reparto y el lanzamiento bloquean "
        "hashtags, cita y comentario (volumen ciego en cascada)."
    )
    claves_pct = {
        "cita": "act_roles_pct_cita",
        "hashtags": "act_roles_pct_hashtags",
        "comentario": "act_roles_pct_comentario",
        "rt": "act_roles_pct_rt",
    }
    pesos = {}
    for columna, rol in zip(st.columns(4), ORDEN_ROLES):
        with columna:
            pesos[rol] = st.number_input(
                f"{etiqueta_rol_activacion(rol)} (%)",
                min_value=0,
                max_value=100,
                value=25,
                step=5,
                key=claves_pct[rol],
            )
    try:
        suma_pct = sum(int(p or 0) for p in pesos.values())
    except (TypeError, ValueError):
        suma_pct = 0

    reparto_preview = _repartir_por_porcentajes(usuarios_sel, pesos)
    st.markdown(
        f"Con **{len(usuarios_sel)}** cuentas seleccionadas: "
        + " · ".join(
            f"{etiqueta_rol_activacion(rol)} **{len(reparto_preview[rol])}**"
            for rol in ORDEN_ROLES
        )
    )
    if suma_pct != 100:
        st.warning(
            f"Los porcentajes suman **{suma_pct}%**: ajústalos hasta 100% "
            "para poder aplicar el reparto."
        )

    aplicar_pct = st.button(
        "💾 Aplicar reparto por porcentajes a las cuentas seleccionadas",
        key="btn_act_roles_pct_apply",
        disabled=suma_pct != 100,
        help=(
            "Asigna a cada cuenta seleccionada el rol que le toca según los "
            "porcentajes (en bloques contiguos, como están ordenadas)."
        ),
    )
    if aplicar_pct:
        if not usuarios_sel:
            st.warning(
                "Selecciona al menos una cuenta para aplicar el reparto por "
                "porcentajes."
            )
        else:
            # VALIDACION BLOQUEANTE: ningun bloque del reparto puede dejar a
            # una cuenta con un rol prohibido por su tier (Tier 2 + hashtags;
            # Tier 3 + hashtags/cita/comentario, que solo admite RT y likes).
            # Si algo falla, NO se aplica NADA.
            errores_tier = []
            for rol in ORDEN_ROLES:
                filas_bloque = [
                    filas_por_usuario.get(u) or {"usuario": u}
                    for u in reparto_preview[rol]
                ]
                if not filas_bloque:
                    continue
                errores_tier.extend(
                    _errores_rol_tier_destino(filas_bloque, rol)
                )
            if errores_tier:
                st.error(
                    "🚫 **Reparto bloqueado**: hay cuentas cuyo tier no permite "
                    "el rol que les toca (Tier 2 no puede 'hashtags'; Tier 3 "
                    "solo RT y likes). **No se aplicó ningún cambio.**\n\n"
                    + "\n".join(f"- {mensaje}" for mensaje in errores_tier)
                    + "\n\nAjusta los porcentajes (deja Hashtags solo para "
                    "Tier 1) o cambia el tier de esas cuentas y vuelve a "
                    "aplicar."
                )
            else:
                resumen = []
                for rol in ORDEN_ROLES:
                    n_rol = _actualizar_roles(reparto_preview[rol], rol)
                    resumen.append(f"{etiqueta_rol_activacion(rol)}: {n_rol}")
                st.session_state["act_roles_pct_msg"] = (
                    "🎚️ Reparto por porcentajes → " + " · ".join(resumen)
                )
                st.rerun()
    mensaje_pct = st.session_state.pop("act_roles_pct_msg", "")
    if mensaje_pct:
        st.success(mensaje_pct)

    # ---------------- Asignar roles manualmente (avanzado) ----------------
    # El reparto masivo vive en «🎚️ Reparto por porcentajes»; este expander
    # queda para asignar rol a cuentas sueltas y ver los conteos actuales.
    with st.expander("🏷️ Asignar roles manualmente (opcional)", expanded=False):
        st.caption(
            "Para cuentas sueltas: si quieres repartir TODAS las "
            "seleccionadas usa «🎚️ Reparto por porcentajes»."
        )
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

        if asignar:
            if not usuarios_sel:
                st.warning("Selecciona al menos una cuenta para asignarle rol.")
            else:
                codigo = normalizar_rol_activacion(opcion_rol)
                # Validacion por TIER para CUALQUIER rol destino (Tier 2 no
                # puede hashtags; Tier 3 solo RT/likes): si algo falla no se
                # asigna nada.
                filas_sel = [
                    filas_por_usuario.get(u) or {"usuario": u}
                    for u in usuarios_sel
                ]
                errores_tier = _errores_rol_tier_destino(filas_sel, codigo)
                if errores_tier:
                    st.error(
                        "🚫 **Rol bloqueado**: hay cuentas cuyo tier no permite "
                        "el rol elegido (Tier 2 no puede 'hashtags'; Tier 3 "
                        "solo RT y likes). **No se asignó nada.**\n\n"
                        + "\n".join(f"- {mensaje}" for mensaje in errores_tier)
                    )
                else:
                    n = _actualizar_roles(usuarios_sel, codigo)
                    st.success(
                        f"✅ Rol «{etiqueta_rol_activacion(codigo)}» asignado a {n} cuenta(s)."
                    )
                    st.rerun()

        # ---------------- Conteos y subcuentas ----------------
        st.markdown("#### 📊 Subcuentas por rol (reparto actual)")
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
    urls_text = st.text_area(
        "URLs objetivo (una por línea; las usan 'cita', 'comentario' y 'rt')",
        height=100,
        key="act_roles_urls",
        disabled=sin_ancla,
    )
    if sin_ancla:
        st.caption(
            "🚫📌 Campaña solo de posts: no se usan URLs; todas las acciones "
            "serán posts con hashtag/contexto."
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

    with st.expander(
        "📰 Contexto desde noticias (opcional, solo trasfondo)", expanded=False
    ):
        panel_noticias = _panel_contexto_noticias("act_roles")

    duracion_min = st.number_input(
        "Duración (min)", min_value=1, max_value=360, value=60, step=5,
        key="act_roles_dur",
    )

    st.markdown("#### 🚀 Estrategia")
    curva = st.checkbox(
        "🚀 Estrategia: Curva de Aceleración",
        value=False,
        key="act_roles_curva",
        help=(
            "Arranca con pocas cuentas (Fase 1) y acelera el volumen en la "
            "Fase 2: la campaña se ve más orgánica y evita ráfagas que X "
            "castiga. Se combina con la rotación a cuentas de respaldo. "
            "Fase 1: solo Tier 1 (Tier 2 y Tier 3 ignorados). Fase 2: se "
            "liberan Tier 2 y Tier 3; el Tier 3 solo dispara RT/likes en "
            "cascada a los posts de los Tier 1."
        ),
    )
    curva_fase1 = 15
    if curva:
        curva_fase1 = st.number_input(
            "Fase 1 (min)",
            min_value=1,
            max_value=60,
            value=15,
            step=1,
            key="act_roles_curva_fase1",
            help="Duración de la primera fase de la curva (1-60 min).",
        )

    with st.expander("🛡️ Cuotas diarias y respaldo", expanded=False):
        reserva_rotar = st.checkbox(
            "♻️ Rotar a cuentas de respaldo al agotar la cuota diaria",
            value=True,
            key="act_roles_reserva_rotar",
            help=(
                "Cuando una cuenta alcanza su tope diario ('Agotada por hoy'), "
                "el motor la sustituye por una cuenta de respaldo con sesión "
                "válida para no frenar la campaña."
            ),
        )
        reserva_max = st.number_input(
            "Máx. cuentas de respaldo",
            min_value=1,
            max_value=200,
            value=50,
            step=1,
            key="act_roles_reserva_max",
            help=(
                "Tope de cuentas de respaldo que se pasan al motor: activas, "
                "con sesión, fuera de esta campaña y de la misma sección."
            ),
        )
        _caption_cuota_diaria()

    with st.expander("⚙️ Opciones avanzadas (ya vienen configuradas)", expanded=False):
        st.caption(
            "Valores recomendados ya fijos: 2 navegadores, 12 trabajadores, "
            "pestaña persistente, repetir por rondas (40-90% de cuentas), sin "
            "proxy, sin API y pausa anti-spam de 15s. Los roles son FIJOS: la "
            "campaña usa el rol que asigna «🎚️ Reparto por porcentajes». "
            "Cámbialos solo si sabes lo que haces."
        )
        col_coh, col_nav, col_work = st.columns(3)
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
                    "Cada navegador ejecuta una cuenta a la vez. En Railway NO "
                    "conviene pasar de 3-4: cada Chrome consume RAM/CPU/hilos y, "
                    "si uno crashea, la campaña se frena en cascada (variable "
                    "MAX_BROWSERS)."
                ),
            )
        with col_work:
            max_workers = st.number_input(
                "Trabajadores simultáneos (acciones en paralelo)",
                min_value=6, max_value=30, value=12, step=1,
                key="act_roles_workers",
                help=(
                    "Acciones que el motor ejecuta a la vez cuando la API va "
                    "primero (variable MAX_WORKERS): con API primero este es el "
                    "paralelismo REAL; súbelo a 16-24 si la API responde. Los "
                    "navegadores solo se abren cuando la API falla."
                ),
            )

        col_cool, col_pausa = st.columns(2)
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
        with col_pausa:
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
                    "1 sola URL los comentarios van en fila: usa 2-5 tweets ancla "
                    "para no frenar (con varias URLs casi no afecta la velocidad)."
                ),
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
                help=(
                    "Limita la campaña a las cuentas que ya tienen rol (así se "
                    "respeta el reparto por porcentajes)."
                ),
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
                "cuentas y se usan todas las activas. Solo entran las que "
                "tengan registro definido (político/activista/ciudadanía) y, "
                "con «Solo cuentas con rol», las que ya tengan rol asignado; "
                "las demás no hacen nada."
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

        st.markdown("##### 🏎️ Opciones de velocidad (recomendadas)")
        sin_proxy = st.checkbox(
            "🌐 Sin proxy: usar la IP del servidor (Railway)",
            value=False,
            key="act_roles_sin_proxy",
            help=(
                "Las peticiones salen directo desde Railway (más rápido y sin "
                "GB de proxy). Úsalo solo en pruebas: compartir una IP de "
                "datacenter entre muchas cuentas puede hacer que X las "
                "bloquee o limite."
            ),
        )
        sin_imagenes = st.checkbox(
            "🖼️ No cargar imágenes ni video (más rápido)",
            value=True,
            key="act_roles_sin_imagenes",
            help=(
                "Chrome carga las páginas sin imágenes/video; las acciones de "
                "texto (post/RT/comentario) no las necesitan. Ahorra RAM y "
                "datos."
            ),
        )
        modo_pestana = st.checkbox(
            "🪟 Modo pestaña persistente (reutiliza Chrome y cambia la cuenta "
            "en la misma pestaña)",
            value=True,
            key="act_roles_pestana",
            help=(
                "El motor abre un Chrome por worker y lo conserva toda la "
                "campaña; cada cuenta cambia su sesión (cookies + UA + proxy) "
                "en la misma pestaña, como la app de referencia (variable "
                "MODO_PESTANA)."
            ),
        )
        pestana_max_acciones = st.number_input(
            "♻️ Reciclar pestaña cada N acciones",
            min_value=5, max_value=200, value=40, step=5,
            key="act_roles_pestana_max",
            help=(
                "Al llegar al límite se cierra y se abre otra; sirve para no "
                "acumular memoria/caché."
            ),
        )
        rt_api = st.checkbox(
            "⚡ Publicar por API (RT, likes, posts, comentarios y citas)",
            value=False,
            key="act_roles_rt_api",
            help=(
                "X está bloqueando/limitando la API (anti-bot 226 / límite "
                "344) y puede marcar cuentas; con pestaña persistente Selenium "
                "es rápido. Actívala solo si tu API responde estable "
                "(variables API_PRIMERO/RT_POR_API). Si la API falla, se "
                "reintenta automáticamente con Chrome."
            ),
        )
        permitir_password = st.checkbox(
            "🔑 Permitir login con password/TOTP en campañas (lento)",
            value=False,
            key="act_roles_permitir_password",
            help=(
                "Si una cuenta no tiene cookies válidas, permite el login con "
                "la contraseña guardada (+TOTP) durante la campaña. Es MUY "
                "lento (~20-40s por cuenta); déjalo apagado salvo lotes "
                "pequeños o cuentas recién creadas (variable "
                "ACTIVACION_PERMITIR_PASSWORD)."
            ),
        )
        _caption_disyuntores_api()

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
        comentario_posible = "comentario" in _roles_objetivo(
            base_objetivo,
            usuarios_param,
            list(ORDEN_ROLES) if solo_con_rol else None,
        )
    if len(urls_previas) == 1 and comentario_posible:
        st.warning(
            "⚠️ Con una sola URL ancla todos los comentarios van al mismo "
            "tweet y X los agrupa como 'Probable spam'. Usa 2-5 tweets ancla "
            "para no frenar: pega 2-5 URLs o sube la pausa."
        )

    # "Solo cuentas con registro": para el RT simple no se necesita registro,
    # asi que este filtro puede dejar fuera cuentas utiles para el reparto por
    # porcentajes. (El checkbox no se toca; el aviso solo aparece si esta
    # encendido.)
    if todas_cuentas:
        st.warning(
            "⚠️ «Todas las cuentas publican» está encendido: solo entran las "
            "cuentas con registro (político/activista/ciudadanía) y el RT "
            "simple no necesita registro, así que podrías dejar fuera cuentas "
            "útiles para el reparto por porcentajes."
        )

    if st.button(
        "🗂️ Lanzar campaña por roles",
        type="primary",
        key="btn_act_roles_launch",
        help=(
            "La campaña usa el rol FIJO de cada subcuenta asignado con "
            "«🎚️ Reparto por porcentajes» (Retweet con cita, Hashtags y "
            "menciones, Comentario en el tweet ancla o Retweet simple)."
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
            # Campaña sin tweet ancla: SOLO posts con hashtag/contexto (rol
            # fijo; la UI ya no ofrece el modo aleatorio).
            solo_roles_param = ["hashtags"]
        else:
            solo_roles_param = list(ORDEN_ROLES) if solo_con_rol else None

        # VALIDACION BLOQUEANTE de tiers: con roles FIJOS, ninguna cuenta
        # Tier 2 puede quedarse con el rol "hashtags" y ninguna Tier 3 con
        # hashtags/cita/comentario (solo RT y likes). Se avisa con
        # `error_rol_tier` y NO se lanza la campaña (el usuario debe corregir).
        errores_tier = _bloqueo_tier2_hashtags(
            base_objetivo,
            usuarios_param,
            solo_roles_param,
            aleatorio=False,
        )
        if errores_tier:
            st.error(
                "🚫 **No se lanzó la campaña**: hay cuentas cuyo tier no permite "
                "su rol guardado (Tier 2 no puede hashtags; Tier 3 solo RT y "
                "likes).\n\n"
                + "\n".join(f"- {mensaje}" for mensaje in errores_tier)
                + "\n\nCorrige el rol con «🎚️ Reparto por porcentajes» "
                "(asígnale RT, o Cita/Comentario si es Tier 2), cambia su tier, "
                "y vuelve a lanzar."
            )
            return

        material_posts = (
            str(hashtags or "").strip()
            or contexto_manual
            or str(texto_base or "").strip()
            or narrativa_noticias
        )

        if sin_ancla:
            roles_objetivo = _roles_objetivo(
                base_objetivo, usuarios_param, solo_roles_param
            )
            if "hashtags" not in roles_objetivo:
                st.warning(
                    "No hay cuentas con rol 'Hashtags y menciones' que "
                    "cumplan la selección. Asigna ese rol con el reparto por "
                    "porcentajes («🎚️ Reparto por porcentajes»)."
                )
                return
            if not material_posts:
                st.warning(
                    "Sin tweet ancla necesitas al menos hashtags, texto base, "
                    "el contexto de los posts o links de noticias (trasfondo) "
                    "para que la IA genere los posts."
                )
                return
        else:
            roles_objetivo = _roles_objetivo(
                base_objetivo, usuarios_param, solo_roles_param
            )
            if not roles_objetivo:
                st.warning(
                    "No hay cuentas con rol que cumplan la selección. Asigna "
                    "roles con el reparto por porcentajes («🎚️ Reparto por "
                    "porcentajes») o revisa los filtros."
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

        # Cuentas de respaldo (misma sección, activas, con sesión y fuera de
        # la selección principal) SOLO si la rotación está activa.
        reserva_usuarios = []
        if reserva_rotar:
            excluidos_reserva = set(usuarios_sel)
            if todas_cuentas:
                excluidos_reserva = {
                    str(f.get("usuario") or "") for f in base_objetivo
                }
            reserva_usuarios = _cargar_reserva_usuarios(
                excluidos=excluidos_reserva,
                seccion=(secciones_param[0] if secciones_param else None),
                maximo=int(reserva_max),
            )

        motor = MotorActivacion(max_concurrente=int(navegadores))
        # El motor viejo no acepta `pausa_comentario_url_seg`: se comprueba la
        # firma para pasar el kwarg solo si existe (la pagina nunca falla).
        pausa_kwargs = {}
        if _soporta_kwarg(motor.ejecutar_por_roles, "pausa_comentario_url_seg"):
            pausa_kwargs["pausa_comentario_url_seg"] = int(pausa_comentario)
        parametros_extra = _parametros_curva_reserva(
            motor.ejecutar_por_roles,
            curva,
            curva_fase1,
            reserva_rotar,
            reserva_usuarios,
        )
        lanzar = _lanzar_motor(
            motor.ejecutar_por_roles,
            {
                "urls": urls,
                "texto_base": texto_base,
                "hashtags": hashtags,
                "menciones": menciones,
                "dar_like": dar_like,
                "duracion_min": int(duracion_min),
                "cohortes": int(cohortes),
                "usuarios": usuarios_param,
                "solo_roles": solo_roles_param,
                "contexto": contexto_manual,
                "narrativa": narrativa_noticias,
                "repetir": bool(repetir),
                "solo_con_registro": bool(todas_cuentas),
                # La UI ya no ofrece el modo aleatorio: la campaña usa SIEMPRE
                # el rol guardado que asigna «🎚️ Reparto por porcentajes». El
                # motor lo sigue soportando (por si se reactiva la UI).
                "roles_aleatorios": False,
                "cooldown_min": float(cooldown_min),
                "secciones": secciones_param,
                "porcentaje_min_ronda": int(pct_min),
                "porcentaje_max_ronda": int(pct_max),
                **pausa_kwargs,
                **parametros_extra,
            },
        )
        _lanzar_con_opciones_velocidad(
            lanzar,
            motor,
            duracion_min=int(duracion_min),
            repetir=bool(repetir),
            prefix="act_roles",
            limpiar=bool(limpiar_contexto_al_terminar),
            sin_proxy=bool(sin_proxy),
            sin_imagenes=bool(sin_imagenes),
            rt_api=bool(rt_api),
            api_primero=bool(rt_api),
            max_workers=int(max_workers),
            permitir_password=bool(permitir_password),
            modo_pestana=bool(modo_pestana),
            pestana_max_acciones=int(pestana_max_acciones),
            tipo="roles",
            parametros={
                "duracion_min": int(duracion_min),
                "repetir": bool(repetir),
                "curva_aceleracion": bool(curva),
                "curva_fase1_min": int(curva_fase1) if curva else None,
            },
        )
        # El panel persistente (arriba de la pestana) pinta el proceso en vivo
        # y, al terminar, el resumen; aqui no se pinta nada mas.


def render(usuario: dict):
    cabecera(
        "🎯 ACTIVACIÓN MASIVA",
        "RT con cita masivo, campañas por roles y campaña 3+3+3",
    )

    st.info(
        f"Configuración recomendada ya fija: **{NAVEGADORES_RECOMENDADOS} "
        f"navegadores**, 12 trabajadores, modo pestaña persistente y pausa "
        f"anti-spam. Solo cambia algo en «⚙️ Opciones avanzadas» si hace "
        f"falta (en Railway NO conviene pasar de 3-4 navegadores: si Chrome "
        f"crashea, la campaña se frena; `MAX_BROWSERS={settings.max_browsers}`). "
        f"Headless: **{settings.headless}**."
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
