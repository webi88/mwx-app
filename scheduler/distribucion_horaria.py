# -*- coding: utf-8 -*-
"""Distribucion horaria de acciones por cuenta (posts, comentarios y RTs).

Requerimiento operativo: por CADA cuenta y CADA HORA se ejecutan 7 acciones de
texto repartidas aleatoriamente entre tweets originales y comentarios/respuestas
(distribuciones validas: 4 posts + 3 comentarios, 5+2 o 6+1) y entre 15 y 20
retweets. Las acciones NO se disparan de golpe: se reparten a lo largo de una
ventana (por defecto 60 minutos) con pausas aleatorias (p. ej. 2 min, luego
5 min, etc.) para simular comportamiento humano.

Este modulo es deliberadamente PURO:
    - Solo stdlib + ``random`` (y, si esta disponible, ``core.perfiles`` para
      normalizar el perfil; con fallback local si no se puede importar).
    - Sin Streamlit, sin base de datos y sin ORM.
    - Todas las funciones publicas son tolerantes a errores: NUNCA lanzan.
      Ante entradas invalidas devuelven listas vacias o resumenes en cero.

Flujo tipico (lo usan el dashboard y el scheduler):

    n_com = n_comentarios_hora(7, 1, 3)              # 1..3
    orden = plan_hora_lote(cuentas, hora_inicio,     # fechas por cuenta
                           n_posts=7 - n_com, n_comentarios=n_com, n_rts=17)
    plan = construir_plan_completo(orden, posts_por_cuenta,
                                   comentarios_por_cuenta, urls_rt,
                                   urls_comentario)
    tareas = crear_tareas_desde_plan(plan, ids_por_usuario)  # scheduler.models
"""
import math
import random
from datetime import datetime, timedelta

try:  # core.perfiles es puro (stdlib); el fallback evita romper si no existe.
    from core.perfiles import normalizar_perfil as _normalizar_perfil_core
except Exception:  # pragma: no cover - entorno sin core
    _normalizar_perfil_core = None

# Tipos canonicos de accion que produce el planner.
TIPO_POST = "post"
TIPO_COMENTARIO = "comentario"
TIPO_RETWEET = "retweet"


# --------------------------------------------------------------------------- #
# Helpers internos (tolerantes, nunca lanzan)
# --------------------------------------------------------------------------- #
def _a_int(valor, default=0) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return default


def _a_float(valor, default=0.0) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return default


def _normalizar_perfil(valor) -> str:
    if _normalizar_perfil_core is not None:
        try:
            return _normalizar_perfil_core(valor)
        except Exception:
            pass
    return str(valor or "").strip()


def _como_lista(valor) -> list:
    """Convierte None/str/scalar/iterable en una lista de strings utiles."""
    if valor is None:
        return []
    if isinstance(valor, str):
        return [valor] if valor.strip() else []
    if isinstance(valor, (list, tuple, set)):
        return [str(v) for v in valor if str(v or "").strip()]
    try:
        return [str(v) for v in valor if str(v or "").strip()]
    except TypeError:
        texto = str(valor or "").strip()
        return [texto] if texto else []


def _recortar_suma(delays: list, limite: float) -> list:
    """Reduce los delays mas grandes hasta que la suma no supere ``limite``.

    Redondea siempre HACIA ABAJO (math.floor) para no reintroducir exceso por
    el redondeo. Nunca deja valores negativos.
    """
    intentos = 0
    while sum(delays) > limite and intentos <= len(delays) + 2:
        exceso = sum(delays) - limite
        indice = max(range(len(delays)), key=lambda i: delays[i])
        if delays[indice] <= 0:
            break
        delays[indice] = max(0.0, math.floor((delays[indice] - exceso) * 100) / 100.0)
        intentos += 1
    return [max(0.0, d) for d in delays]


def _calcular_delays(n_acciones, ventana_minutos, min_seg, max_seg, rng) -> list:
    """Implementacion con ``rng`` inyectable de :func:`calcular_delays`.

    Garantiza: ``len == n_acciones`` (si n > 0), cada delay >= 0 y
    ``sum(delays) <= ventana_minutos * 60``. Si el minimo no cabe en la ventana
    se escala proporcionalmente (nunca suma mas que la ventana).
    """
    try:
        n = _a_int(n_acciones, 0)
        if n <= 0:
            return []
        ventana = _a_float(ventana_minutos, 60.0) * 60.0
        minimo = max(0.0, _a_float(min_seg, 30.0))
        maximo = max(minimo, _a_float(max_seg, 240.0))
        if ventana <= 0:
            return [0.0] * n

        crudos = [rng.uniform(minimo, maximo) for _ in range(n)]
        total = sum(crudos)

        if total <= ventana:
            resultado = list(crudos)
        elif n * minimo <= ventana:
            # Hay presupuesto para respetar el minimo: reparte el margen
            # disponible en proporcion a lo sorteado por encima del minimo.
            margen = ventana - n * minimo
            extras = [max(0.0, d - minimo) for d in crudos]
            total_extras = sum(extras)
            if total_extras > 0:
                resultado = [minimo + (e / total_extras) * margen for e in extras]
            else:
                resultado = [minimo] * n
        else:
            # Ni el minimo cabe (demasiadas acciones): escala proporcional.
            factor = ventana / total
            resultado = [d * factor for d in crudos]

        resultado = [round(max(0.0, d), 2) for d in resultado]
        if sum(resultado) > ventana:
            resultado = _recortar_suma(resultado, ventana)
        return resultado
    except Exception:
        return []


def _tipos_mezclados(n_acciones, n_posts, n_comentarios, n_rts, rng) -> list:
    """Construye la lista de tipos BARAJADA (posts/comentarios/RTs mezclados).

    Si la suma de tipos no coincide con ``n_acciones`` se recorta (aleatorio) o
    se rellena con "retweet". Nunca lanza.
    """
    n = max(0, _a_int(n_acciones, 0))
    posts = max(0, _a_int(n_posts, 0))
    comentarios = max(0, _a_int(n_comentarios, 0))
    rts = max(0, _a_int(n_rts, 0))
    tipos = [TIPO_POST] * posts + [TIPO_COMENTARIO] * comentarios + [TIPO_RETWEET] * rts
    if len(tipos) > n:
        try:
            rng.shuffle(tipos)
        except Exception:
            pass
        tipos = tipos[:n]
    while len(tipos) < n:
        tipos.append(TIPO_RETWEET)
    try:
        rng.shuffle(tipos)
    except Exception:
        pass
    return tipos


def _fechas_desde_delays(hora_inicio: datetime, delays: list, ventana_minutos: float) -> list:
    """Aplica delays (segundos) desde ``hora_inicio`` y recorta a la ventana.

    La primera accion queda exactamente en ``hora_inicio`` y se usan los
    primeros ``len(delays) - 1`` delays como separaciones entre acciones; el
    ultimo delay es el margen de cierre. Todas las fechas quedan dentro de
    ``[hora_inicio, hora_inicio + ventana]``.
    """
    fechas = [hora_inicio]
    for delay in list(delays)[:-1]:
        fechas.append(fechas[-1] + timedelta(seconds=float(delay)))
    limite = hora_inicio + timedelta(minutes=float(ventana_minutos))
    return [min(fecha, limite) for fecha in fechas]


def _tipos_y_fechas(n_acciones, hora_inicio, n_posts, n_comentarios, n_rts,
                    ventana_minutos, rng):
    """Genera en una sola pasada la secuencia MEZCLADA de tipos y sus fechas.

    Compartida por :func:`generar_orden_horario` y :func:`plan_hora_cuenta`
    para que el barajado de tipos y los delays consuman el mismo ``rng``.
    """
    n = max(0, _a_int(n_acciones, 0))
    if n <= 0 or not isinstance(hora_inicio, datetime):
        return [], []
    ventana = _a_float(ventana_minutos, 60.0)
    if ventana <= 0:
        ventana = 60.0
    tipos = _tipos_mezclados(n, n_posts, n_comentarios, n_rts, rng)
    delays = _calcular_delays(n, ventana, 30, 240, rng)
    if len(delays) < n:
        delays = list(delays) + [0.0] * (n - len(delays))
    fechas = _fechas_desde_delays(hora_inicio, delays[:n], ventana)
    return tipos, fechas


def _normalizar_usuarios_perfiles(usuarios_perfiles) -> list:
    """Acepta lista de dicts {"usuario","perfil"}, tuplas (usuario, perfil),
    strings sueltos o un dict {usuario: perfil}. Devuelve lista de tuplas."""
    resultado = []
    if not usuarios_perfiles:
        return resultado
    if isinstance(usuarios_perfiles, dict):
        for usuario, perfil in usuarios_perfiles.items():
            if isinstance(perfil, dict):
                perfil = (
                    perfil.get("perfil")
                    or perfil.get("perfil_personalidad")
                    or perfil.get("personalidad")
                    or ""
                )
            resultado.append((str(usuario or "").strip(), _normalizar_perfil(perfil)))
        return [par for par in resultado if par[0]]
    for item in usuarios_perfiles:
        usuario = ""
        perfil = ""
        if isinstance(item, dict):
            usuario = item.get("usuario") or item.get("user") or item.get("username") or ""
            perfil = (
                item.get("perfil")
                or item.get("perfil_personalidad")
                or item.get("personalidad")
                or ""
            )
        elif isinstance(item, (tuple, list)):
            if len(item) >= 1:
                usuario = item[0]
            if len(item) >= 2:
                perfil = item[1]
        else:
            usuario = item
        usuario = str(usuario or "").strip()
        if usuario:
            resultado.append((usuario, _normalizar_perfil(perfil)))
    return resultado


def _normalizar_escalonar(escalonar_segundos) -> tuple:
    """Normaliza (min, max) de escalonado; cualquiera de los dos puede venir
    como numero suelto (se usa como maximo)."""
    if escalonar_segundos is None:
        return (0.0, 0.0)
    if isinstance(escalonar_segundos, (int, float)):
        return (0.0, max(0.0, float(escalonar_segundos)))
    try:
        minimo = max(0.0, _a_float(escalonar_segundos[0], 0.0))
        maximo = max(minimo, _a_float(escalonar_segundos[1], minimo))
        return (minimo, maximo)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


# --------------------------------------------------------------------------- #
# API publica
# --------------------------------------------------------------------------- #
def n_comentarios_hora(total_textos=7, min_comentarios=1, max_comentarios=3) -> int:
    """Cantidad aleatoria de comentarios para la hora (acotada al total).

    Con los valores por defecto devuelve 1, 2 o 3; el resto de ``total_textos``
    son posts originales (6+1, 5+2 o 4+3). Nunca lanza.
    """
    try:
        total = max(0, _a_int(total_textos, 7))
        minimo = max(0, _a_int(min_comentarios, 1))
        maximo = max(0, _a_int(max_comentarios, 3))
        if minimo > maximo:
            minimo, maximo = maximo, minimo
        maximo = min(maximo, total)
        minimo = min(minimo, maximo)
        if maximo <= 0:
            return 0
        return random.randint(minimo, maximo)
    except Exception:
        return 0


def calcular_delays(n_acciones, ventana_minutos=60, min_seg=30, max_seg=240) -> list[float]:
    """Genera ``n_acciones`` delays (en SEGUNDOS) para repartir una hora.

    - Suma total <= ``ventana_minutos * 60`` (nunca se pasa de la ventana).
    - Cada delay queda en ``[min_seg, max_seg]`` siempre que quepa; si no cabe
      el minimo se escala proporcionalmente y si el maximo cabe se reparte el
      margen respetando el minimo.
    - Entradas invalidas o ``n_acciones <= 0`` devuelven ``[]``. Nunca lanza.
    """
    return _calcular_delays(n_acciones, ventana_minutos, min_seg, max_seg, random)


def generar_orden_horario(n_acciones, hora_inicio, n_posts, n_comentarios, n_rts,
                          ventana_minutos=60, rng=random) -> list[datetime]:
    """Fechas de inicio de las acciones, mezclando tipos y sin agruparlos.

    La primera accion arranca exactamente en ``hora_inicio``; el resto se
    separa con delays aleatorios de :func:`calcular_delays`. Garantiza que
    TODAS las fechas queden en ``[hora_inicio, hora_inicio + ventana]``.
    Devuelve solo las fechas (el tipo correspondiente lo asigna el planner).
    """
    try:
        _, fechas = _tipos_y_fechas(
            n_acciones, hora_inicio, n_posts, n_comentarios, n_rts,
            ventana_minutos, rng,
        )
        return fechas
    except Exception:
        return []


def plan_hora_cuenta(usuario, perfil, hora_inicio, n_posts=5, n_comentarios=2,
                     n_rts=17, ventana_minutos=60, rng=random) -> list[dict]:
    """Plan de UNA cuenta para una hora.

    Devuelve exactamente ``n_posts + n_comentarios + n_rts`` acciones,
    ordenadas por fecha, con dicts::

        {"usuario", "perfil", "tipo", "fecha_hora", "indice"}

    donde ``tipo`` es "post", "comentario" o "retweet" y los tipos van
    mezclados aleatoriamente (nunca agrupados). Nunca lanza.
    """
    try:
        if not isinstance(hora_inicio, datetime):
            return []
        posts = max(0, _a_int(n_posts, 5))
        comentarios = max(0, _a_int(n_comentarios, 2))
        rts = max(0, _a_int(n_rts, 17))
        n = posts + comentarios + rts
        if n <= 0:
            return []
        usuario_txt = str(usuario or "").strip()
        perfil_txt = _normalizar_perfil(perfil)
        tipos, fechas = _tipos_y_fechas(
            n, hora_inicio, posts, comentarios, rts, ventana_minutos, rng,
        )
        if len(fechas) < n:
            fechas = list(fechas) + [hora_inicio] * (n - len(fechas))
        plan = [
            {
                "usuario": usuario_txt,
                "perfil": perfil_txt,
                "tipo": tipo,
                "fecha_hora": fecha,
                "indice": indice,
            }
            for indice, (tipo, fecha) in enumerate(zip(tipos, fechas))
        ]
        plan.sort(key=lambda accion: accion["fecha_hora"])
        return plan
    except Exception:
        return []


def plan_hora_lote(usuarios_perfiles, hora_inicio, n_posts=5, n_comentarios=2,
                   n_rts=17, ventana_minutos=60, escalonar_segundos=(0, 300),
                   rng=random) -> list[dict]:
    """Aplica :func:`plan_hora_cuenta` a varias cuentas.

    ``usuarios_perfiles`` acepta lista de dicts ``{"usuario","perfil"}``, lista
    de tuplas ``(usuario, perfil)``, strings sueltos o un dict
    ``{usuario: perfil}``. Cada cuenta recibe un retardo de arranque aleatorio
    distinto (``escalonar_segundos``) para no arrancar todas al mismo segundo.
    El resultado se devuelve ordenado cronologicamente. Nunca lanza.
    """
    try:
        if not isinstance(hora_inicio, datetime):
            return []
        cuentas = _normalizar_usuarios_perfiles(usuarios_perfiles)
        if not cuentas:
            return []
        minimo, maximo = _normalizar_escalonar(escalonar_segundos)
        plan_total = []
        for usuario, perfil in cuentas:
            if maximo > minimo:
                offset = rng.uniform(minimo, maximo)
            else:
                offset = (minimo + maximo) / 2.0
            inicio_cuenta = hora_inicio + timedelta(seconds=offset)
            plan_total.extend(
                plan_hora_cuenta(
                    usuario, perfil, inicio_cuenta,
                    n_posts=n_posts, n_comentarios=n_comentarios, n_rts=n_rts,
                    ventana_minutos=ventana_minutos, rng=rng,
                )
            )
        plan_total.sort(key=lambda accion: accion["fecha_hora"])
        return plan_total
    except Exception:
        return []


def resumen_plan(plan) -> dict:
    """Resumen agregado de un plan (total y desglose por tipo/cuenta)."""
    resumen = {
        "acciones": 0,
        "posts": 0,
        "comentarios": 0,
        "retweets": 0,
        "primera": None,
        "ultima": None,
        "por_cuenta": {},
    }
    try:
        for accion in plan or []:
            if not isinstance(accion, dict):
                continue
            tipo = str(accion.get("tipo") or "").strip().lower()
            usuario = str(accion.get("usuario") or "").strip()
            resumen["acciones"] += 1
            if tipo == TIPO_POST:
                resumen["posts"] += 1
            elif tipo == TIPO_COMENTARIO:
                resumen["comentarios"] += 1
            elif tipo == TIPO_RETWEET:
                resumen["retweets"] += 1
            cuenta = resumen["por_cuenta"].setdefault(
                usuario, {"posts": 0, "comentarios": 0, "retweets": 0}
            )
            if tipo == TIPO_POST:
                cuenta["posts"] += 1
            elif tipo == TIPO_COMENTARIO:
                cuenta["comentarios"] += 1
            elif tipo == TIPO_RETWEET:
                cuenta["retweets"] += 1
            fecha = accion.get("fecha_hora")
            if isinstance(fecha, datetime):
                if resumen["primera"] is None or fecha < resumen["primera"]:
                    resumen["primera"] = fecha
                if resumen["ultima"] is None or fecha > resumen["ultima"]:
                    resumen["ultima"] = fecha
    except Exception:
        pass
    return resumen


def construir_plan_completo(orden, posts_por_cuenta, comentarios_por_cuenta,
                            urls_rt, urls_comentario, rng=random) -> list[dict]:
    """Asigna contenido (texto/url) a cada accion del plan ``orden``.

    - "post"       -> ``texto`` del pool de esa cuenta (rota sin repetir
                       consecutivo); ``url`` vacio.
    - "comentario" -> ``texto`` del pool de comentarios de la cuenta + una URL
                       destino rotando/aleatoria de ``urls_comentario``.
    - "retweet"    -> ``url`` rotando/aleatoria de ``urls_rt``; ``texto`` vacio.

    Devuelve dicts ``{"usuario","perfil","tipo","fecha_hora","texto","url"}``.
    Si faltan textos o URLs, la accion se marca con "texto"/"url" vacios.
    Nunca lanza.
    """
    try:
        resultado = []
        if not orden:
            return resultado
        posts = posts_por_cuenta if isinstance(posts_por_cuenta, dict) else {}
        comentarios = (
            comentarios_por_cuenta if isinstance(comentarios_por_cuenta, dict) else {}
        )
        rt_urls = _como_lista(urls_rt)
        coment_urls = _como_lista(urls_comentario)

        punteros_posts = {}
        punteros_coment = {}
        ptr_rt = rng.randrange(len(rt_urls)) if rt_urls else 0
        ptr_coment_url = rng.randrange(len(coment_urls)) if coment_urls else 0

        for accion in orden:
            if not isinstance(accion, dict):
                continue
            usuario = str(accion.get("usuario") or "").strip()
            tipo = str(accion.get("tipo") or "").strip().lower()
            item = {
                "usuario": usuario,
                "perfil": accion.get("perfil") or "",
                "tipo": tipo,
                "fecha_hora": accion.get("fecha_hora"),
                "texto": "",
                "url": "",
            }

            if tipo == TIPO_POST:
                pool = _como_lista(posts.get(usuario))
                if pool:
                    indice = punteros_posts.get(usuario, rng.randrange(len(pool)))
                    item["texto"] = pool[indice % len(pool)]
                    punteros_posts[usuario] = indice + 1

            elif tipo == TIPO_COMENTARIO:
                pool = _como_lista(comentarios.get(usuario))
                if pool:
                    indice = punteros_coment.get(usuario, rng.randrange(len(pool)))
                    item["texto"] = pool[indice % len(pool)]
                    punteros_coment[usuario] = indice + 1
                if coment_urls:
                    item["url"] = coment_urls[ptr_coment_url % len(coment_urls)]
                    ptr_coment_url += 1

            elif tipo == TIPO_RETWEET:
                if rt_urls:
                    item["url"] = rt_urls[ptr_rt % len(rt_urls)]
                    ptr_rt += 1

            resultado.append(item)
        return resultado
    except Exception:
        return []


N_POSTS_CAMPANA = 3
N_COMENTARIOS_CAMPANA = 3
N_RTS_CAMPANA = 3


def plan_campana_3_3_3(cuentas, hora_inicio=None, urls_rt=None,
                       urls_comentarios=None, ventana_minutos=60,
                       textos_por_cuenta=None, comentarios_por_cuenta=None,
                       escalonar_segundos=(0, 300), rng=random) -> list[dict]:
    """Plan de campana 3+3+3: 9 acciones por cuenta en una ventana.

    Cada cuenta recibe exactamente 3 posts + 3 comentarios + 3 retweets,
    con tipos mezclados aleatoriamente y fechas repartidas en
    ``[hora_inicio, hora_inicio + ventana]`` mediante
    :func:`plan_hora_cuenta` (que usa ``calcular_delays`` y
    ``generar_orden_horario``) mas un arranque escalonado por cuenta
    (cohortes anti-spam via ``escalonar_segundos``).

    Acepta dos ordenes de llamada:

    - Estilo lote (canonico):
      ``plan_campana_3_3_3(cuentas, hora_inicio, urls_rt, urls_comentarios)``
    - Estilo campana:
      ``plan_campana_3_3_3(cuentas, urls_rt, urls_comentarios,
      ventana_minutos)`` (sin ``hora_inicio``; se usa la hora actual).

    ``textos_por_cuenta`` y ``comentarios_por_cuenta`` son dicts
    ``usuario -> lista de textos``. Cuando se proporcionan, cada slot de
    post/comentario recibe un texto distinto rotando sin repetir
    consecutivo (delega en :func:`construir_plan_completo`); cuando no,
    el campo ``texto`` queda vacio para que lo rellene
    ``ia.generar_pool_campana_por_cuenta`` y cada slot lleva su indice
    ``slot`` (0..2 por tipo y cuenta) para mapear los textos luego.

    Devuelve dicts ``{"usuario","perfil","tipo","fecha_hora","texto",
    "url","slot"}`` ordenados cronologicamente, listos para
    ``scheduler.models.crear_tareas_desde_plan``. Nunca lanza.
    """
    try:
        if hora_inicio is not None and not isinstance(hora_inicio, datetime):
            _rt = hora_inicio
            _com = urls_rt
            _vent = ventana_minutos
            if isinstance(urls_comentarios, (int, float)):
                _vent = urls_comentarios
            else:
                try:
                    if urls_comentarios is not None:
                        _vent = float(urls_comentarios)
                except (TypeError, ValueError):
                    pass
            urls_rt = _rt
            urls_comentarios = _com
            ventana_minutos = _vent
            hora_inicio = None
        if hora_inicio is None:
            hora_inicio = datetime.now()
        if not isinstance(hora_inicio, datetime):
            return []
        cuentas_norm = _normalizar_usuarios_perfiles(cuentas)
        if not cuentas_norm:
            return []
        ventana = _a_float(ventana_minutos, 60.0)
        if ventana <= 0:
            ventana = 60.0
        rt_urls = _como_lista(urls_rt)
        com_urls = _como_lista(urls_comentarios)
        pool_posts = textos_por_cuenta if isinstance(textos_por_cuenta, dict) else {}
        pool_coms = comentarios_por_cuenta if isinstance(comentarios_por_cuenta, dict) else {}
        minimo, maximo = _normalizar_escalonar(escalonar_segundos)
        if rng is None:
            rng = random
        orden = []
        for usuario, perfil in cuentas_norm:
            try:
                if maximo > minimo:
                    offset = rng.uniform(minimo, maximo)
                else:
                    offset = (minimo + maximo) / 2.0
            except Exception:
                offset = 0.0
            inicio_cuenta = hora_inicio + timedelta(seconds=offset)
            orden.extend(
                plan_hora_cuenta(
                    usuario, perfil, inicio_cuenta,
                    n_posts=N_POSTS_CAMPANA,
                    n_comentarios=N_COMENTARIOS_CAMPANA,
                    n_rts=N_RTS_CAMPANA,
                    ventana_minutos=ventana,
                    rng=rng,
                )
            )
        if not orden:
            return []
        plan = construir_plan_completo(
            orden, pool_posts, pool_coms, rt_urls, com_urls, rng,
        )
        contadores: dict = {}
        for item in plan:
            try:
                clave = (item.get("usuario"), item.get("tipo"))
                item["slot"] = contadores.get(clave, 0)
                contadores[clave] = contadores.get(clave, 0) + 1
            except Exception:
                try:
                    item["slot"] = 0
                except Exception:
                    pass
        try:
            plan.sort(key=lambda accion: accion.get("fecha_hora") or hora_inicio)
        except Exception:
            pass
        return plan
    except Exception:
        return []
