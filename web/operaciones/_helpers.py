import streamlit as st
from core.database import get_db_session
from core.models import Cuenta
from core.registro import registrar_accion
from loguru import logger


def guardar_imagen_subida(archivo, prefijo: str = "subida") -> str | None:
    """Guarda una imagen subida/pegada en data/temp y devuelve su ruta absoluta.
    Acepta un objeto UploadedFile de Streamlit o bytes."""
    import os
    from core.config import resolver_ruta

    if archivo is None:
        return None

    try:
        temp_dir = resolver_ruta("data/temp")
        os.makedirs(temp_dir, exist_ok=True)

        if hasattr(archivo, "name") and hasattr(archivo, "getbuffer"):
            nombre = archivo.name or "imagen.png"
            data = archivo.getbuffer()
            ext = os.path.splitext(nombre)[1].lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                ext = ".png"
        else:
            ext = ".png"
            data = archivo

        salida = os.path.join(temp_dir, f"{prefijo}_{os.getpid()}{ext}")
        with open(salida, "wb") as f:
            if isinstance(data, bytes):
                f.write(data)
            else:
                f.write(bytes(data))
        return salida
    except Exception as e:
        logger.error(f"Error guardando imagen subida: {e}")
        return None


def cuentas_por_plataforma(plataforma: str, solo_activas: bool = True) -> list[Cuenta]:
    with get_db_session() as db:
        query = db.query(Cuenta).filter(Cuenta.plataforma == plataforma)
        if solo_activas:
            query = query.filter(Cuenta.activa == True)
        return query.all()


def cuentas_por_tags(tags_requeridos: list[str] | None = None) -> list[Cuenta]:
    with get_db_session() as db:
        cuentas = db.query(Cuenta).filter(Cuenta.plataforma == "twitter", Cuenta.activa == True).all()
    
    if not tags_requeridos:
        return cuentas
    
    resultado = []
    for c in cuentas:
        c_tags = [t.strip().upper() for t in (c.tags or "").split(",") if t.strip()]
        if all(tag.upper() in c_tags for tag in tags_requeridos):
            resultado.append(c)
    return resultado


def generar_pool_por_cuenta_seguro(
    base: str,
    n_cuentas: int,
    narrativa: str = "",
    entrenamiento: str = "",
    registros: list | None = None,
    perfiles: list | None = None,
) -> tuple[list[str], bool]:
    """Genera exactamente 'n_cuentas' textos unicos (uno por cuenta).

    Intenta primero la IA (`ia.generador_contenido.generar_pool_por_cuenta`,
    import perezoso para no romper el arranque si aun no existe) y completa
    con el generador local de variaciones (`activaciones.variaciones`).

    `registros` es una lista opcional alineada con las cuentas (mismo indice):
    "politica", "ciudadana" o "". Se reenvia tal cual a la IA para que cada
    texto respete el registro de su cuenta. Si la IA falla o devuelve menos
    textos, el relleno local se agrega respetando el orden de los indices
    (los textos ya generados conservan su posicion).

    `perfiles` es otra lista opcional alineada con las cuentas (mismo indice):
    "formal", "ciudadano", "popular" o "". Se reenvia a la IA (firma nueva
    `generar_pool_por_cuenta(..., perfiles=...)`); si la version instalada
    todavia no acepta el kwarg, se reintenta SIN perfiles para no romper.

    Ademas, TODO texto del pool sale con un hashtag integrado EN MEDIO
    (`core.perfiles.colocar_hashtag_en_medio`), nunca al final.

    Devuelve (pool, uso_fallback_local); el pool siempre tiene 'n_cuentas'
    elementos cuando n_cuentas > 0.
    """
    if n_cuentas <= 0:
        return [], False

    base = (base or "").strip()
    pool: list[str] = []
    uso_fallback = False

    try:
        from ia.generador_contenido import generar_pool_por_cuenta

        try:
            nuevos = generar_pool_por_cuenta(
                base,
                n_cuentas,
                narrativa,
                entrenamiento,
                registros=registros,
                perfiles=perfiles,
            )
        except TypeError as e:
            # Version vieja sin el kwarg 'perfiles': reintento sin el.
            logger.info(
                f"generar_pool_por_cuenta aun no acepta 'perfiles' ({e}); "
                "reintentando sin perfiles"
            )
            nuevos = generar_pool_por_cuenta(
                base, n_cuentas, narrativa, entrenamiento, registros=registros
            )
        for t in (nuevos or []):
            t = str(t).strip()
            if t and t not in pool:
                pool.append(t)
    except Exception as e:
        logger.warning(f"generar_pool_por_cuenta no disponible: {e}")
        uso_fallback = True

    if len(pool) < n_cuentas:
        uso_fallback = True
        try:
            from activaciones.variaciones import generar_pool_variaciones

            for t in generar_pool_variaciones(base, n_cuentas - len(pool)):
                t = str(t).strip()
                if t and t not in pool:
                    pool.append(t)
                if len(pool) >= n_cuentas:
                    break
        except Exception as e:
            logger.warning(f"generar_pool_variaciones fallo: {e}")

    sufijo = len(pool) + 1
    while len(pool) < n_cuentas:
        relleno = ""
        try:
            from activaciones.variaciones import variar_texto

            relleno = (variar_texto(base) or "").strip()
        except Exception:
            pass
        if not relleno or relleno in pool:
            while True:
                relleno = f"{base} ({sufijo})".strip()
                sufijo += 1
                if relleno not in pool:
                    break
        pool.append(relleno)

    # Regla global: todo texto del pool lleva UN hashtag integrado en MEDIO
    # (sin romper los que ya lo traen; la funcion lo reubica si esta al final).
    try:
        from core.perfiles import colocar_hashtag_en_medio

        pool = [colocar_hashtag_en_medio(t) for t in pool]
    except Exception as e:
        logger.warning(f"No se pudo garantizar el hashtag en medio: {e}")

    return pool[:n_cuentas], uso_fallback


def _rol_efectivo_ejecucion(tipo) -> str:
    """Rol efectivo del `tipo` de un flujo DIRECTO del dashboard.

    Igual que `core.registro.normalizar_rol_cuota` con UNA salvedad: en los
    flujos del dashboard "calentamiento" hace RT+likes (`rts._calentamiento`),
    no publica posts, así que se trata como "rt" (no como "hashtags"). Nunca
    lanza."""
    texto = str(tipo or "").strip().lower()
    try:
        from core.registro import normalizar_rol_cuota

        rol = normalizar_rol_cuota(tipo)
    except Exception:
        rol = texto
    if rol == "hashtags" and "calentamiento" in texto:
        return "rt"
    return rol


def bloqueo_tier_ejecucion(cuenta, tipo) -> str:
    """Mensaje "⛔ ..." (o "") si el tier de `cuenta` prohíbe ejecutar `tipo`.

    Regla de negocio (centralizada en `core.tiers`):
      - Tier 3 (Métricas/Soporte): SOLO RT y likes. Bloquea el rol efectivo
        "hashtags" (post/mantenimiento/hilo), "cita" y "comentario"; pasan
        rt/like, calentamiento (RT+likes), visualizacion, vacío y desconocidos.
      - Tier 2 (Volumen/Aged): bloquea el rol efectivo "hashtags".
      - Tier 1 o sin tier: nunca bloquea.
    Se usa ANTES de crear el bot: una cuenta bloqueada no abre Chrome ni
    registra nada. Nunca lanza: con `core.tiers` viejo devuelve "" (sin
    bloqueo)."""
    try:
        from core.tiers import es_tier2, es_tier3
    except Exception:
        return ""
    try:
        usuario = str(getattr(cuenta, "usuario", "") or "")
        if es_tier3(cuenta):
            rol = _rol_efectivo_ejecucion(tipo)
            if rol in ("hashtags", "cita", "comentario"):
                return (
                    f"⛔ @{usuario} — Tier 3: rol '{rol}' PROHIBIDO "
                    "(no se ejecutó; solo RT y likes)"
                )
        if es_tier2(cuenta):
            if _rol_efectivo_ejecucion(tipo) == "hashtags":
                return (
                    f"⛔ @{usuario} — Tier 2: rol 'hashtags' PROHIBIDO "
                    "(no se ejecutó; solo RT, Cita o Comentario)"
                )
    except Exception:
        return ""
    return ""


def es_pausada_activacion(cuenta) -> bool:
    """True si `cuenta` esta pausada para la activacion masiva (`core.pausas`).

    Acepta objetos `Cuenta` (atributo `pausada_activacion`) y las filas-dict de
    las paginas (clave `pausada_activacion`): a los dicts se les pasa un
    adaptador porque `core.pausas.esta_pausada` solo lee atributos. Nunca
    lanza: sin `core.pausas` (version vieja) devuelve False (nada se filtra)."""
    try:
        from core.pausas import esta_pausada
    except Exception:
        return False
    try:
        if isinstance(cuenta, dict):
            from types import SimpleNamespace

            return bool(
                esta_pausada(
                    SimpleNamespace(
                        pausada_activacion=cuenta.get("pausada_activacion")
                    )
                )
            )
        return bool(esta_pausada(cuenta))
    except Exception:
        return False


def separar_pausadas(cuentas, incluir_pausadas: bool = False) -> tuple[list, list]:
    """Separa (elegibles, pausadas) para la activacion masiva (`core.pausas`).

    Con `incluir_pausadas=True` la primera lista conserva TODO (las pausadas
    solo se reportan): lo usan los flujos de mantenimiento. Nunca lanza."""
    try:
        lista = list(cuentas or [])
    except Exception:
        return [], []
    elegibles: list = []
    pausadas: list = []
    for cuenta in lista:
        if es_pausada_activacion(cuenta):
            pausadas.append(cuenta)
        else:
            elegibles.append(cuenta)
    if incluir_pausadas:
        return lista, pausadas
    return elegibles, pausadas


# Texto UNICO del aviso de cuentas pausadas excluidas de la activacion
# (mismo caption en rts/likes/activacion masiva/reparto por hora).
MENSAJE_PAUSADAS = (
    "⏸️ {n} cuentas pausadas para activación quedaron fuera "
    "(siguen en mantenimiento)"
)


def texto_pausadas(total) -> str:
    """Texto unico del aviso de pausadas ('' si no hay ninguna). Nunca lanza."""
    try:
        n = int(total or 0)
    except Exception:
        n = 0
    if n <= 0:
        return ""
    return MENSAJE_PAUSADAS.format(n=n)


def aviso_pausadas(pausadas) -> None:
    """Caption unico: cuentas pausadas excluidas de la activacion masiva."""
    try:
        total = len(list(pausadas or []))
    except Exception:
        total = 0
    texto = texto_pausadas(total)
    if texto:
        st.caption(texto)


def ejecutar_en_cuentas(cuentas: list[Cuenta], accion, plataforma: str = "twitter",
                        progreso: st.progress = None, estado: st.empty = None,
                        tipo: str = "post", incluir_pausadas: bool = False) -> dict:
    """Ejecuta 'accion(bot)' sobre cada cuenta y acumula resultados.

    'accion' puede ser un callable o una lista/tupla de callables (uno por
    cuenta, en el mismo orden). Esto permite publicar un texto distinto en
    cada cuenta: `[lambda bot, t=t: bot.publicar_tweet(t) for t in pool]`.

    ANTES de crear el bot de cada cuenta se valida su Tier: Tier 3 solo RT y
    likes y Tier 2 no puede hashtags (`bloqueo_tier_ejecucion`). Las cuentas
    bloqueadas NO abren navegador ni registran `RegistroAccion`: suman a
    `resultados["omitidas"]` con un detalle "⛔ ...".

    Las cuentas pausadas para la activacion masiva (`Cuenta.pausada_activacion`,
    ver `core.pausas`) tampoco se ejecutan: suman a `resultados["omitidas"]` con
    el detalle "⏸️ ...", sin abrir navegador ni registrar nada. SOLO los flujos
    de mantenimiento pasan `incluir_pausadas=True` (mantenimiento programado,
    publicar texto e IA de `posts.py`): ahi las pausadas siguen publicando."""
    from plataformas.base import PlataformaFactory
    
    resultados = {"exitos": 0, "fallidos": 0, "detalles": [], "omitidas": 0}
    total = len(cuentas)
    if isinstance(accion, (list, tuple)):
        acciones = list(accion)
    else:
        acciones = [accion] * total
    
    for i, cuenta in enumerate(cuentas):
        if not incluir_pausadas and es_pausada_activacion(cuenta):
            usuario = getattr(cuenta, "usuario", "?")
            resultados["omitidas"] += 1
            resultados["detalles"].append(
                f"⏸️ @{usuario} — pausada para activación (solo mantenimiento)"
            )
            if progreso and total > 0:
                progreso.progress((i + 1) / total)
            continue
        motivo_tier = bloqueo_tier_ejecucion(cuenta, tipo)
        if motivo_tier:
            resultados["omitidas"] += 1
            resultados["detalles"].append(motivo_tier)
            if progreso and total > 0:
                progreso.progress((i + 1) / total)
            continue
        try:
            if estado:
                estado.write(f"⏳ Trabajando con **@{cuenta.usuario}**...")
            
            bot = PlataformaFactory.crear_bot(plataforma, cuenta.usuario)
            accion_cuenta = acciones[i] if i < len(acciones) else acciones[-1]
            res = accion_cuenta(bot)
            if getattr(bot, "cuenta_suspendida", False):
                from core.registro import marcar_cuenta_suspendida
                marcar_cuenta_suspendida(cuenta.usuario)
                logger.warning(f"@{cuenta.usuario} marcada como suspendida (desactivada)")
            bot.cerrar()

            ok = res
            url = res if isinstance(res, str) else (getattr(bot, "ultima_url_publicada", "") or "")
            
            if ok:
                resultados["exitos"] += 1
                if url:
                    # RT simple (y calentamiento, que tambien retwittea sin
                    # cita) enlazan el perfil de quien retwittea, no un post.
                    etiqueta = "ver perfil" if tipo in ("rt", "calentamiento") else "ver post"
                    resultados["detalles"].append(f"✅ @{cuenta.usuario} — [{etiqueta}]({url})")
                else:
                    resultados["detalles"].append(f"✅ @{cuenta.usuario}")
                registrar_accion(cuenta.usuario, tipo, "exito", url, "")
            else:
                resultados["fallidos"] += 1
                motivo = getattr(bot, "ultimo_error", "") or ""
                detalle = f"❌ @{cuenta.usuario}"
                if motivo:
                    detalle += f": {motivo[:400]}"
                resultados["detalles"].append(detalle)
                registrar_accion(cuenta.usuario, tipo, "fallido", "", detalle)
        except Exception as e:
            logger.exception(f"Error en @{cuenta.usuario}: {e}")
            resultados["fallidos"] += 1
            resultados["detalles"].append(f"❌ @{cuenta.usuario}: {str(e)[:400]}")
            registrar_accion(cuenta.usuario, tipo, "fallido", "", str(e)[:400])
        
        if progreso and total > 0:
            progreso.progress((i + 1) / total)
    
    if estado:
        estado.write("✅ Proceso terminado")
    
    return resultados


def mostrar_resultados(resultados: dict):
    col1, col2 = st.columns(2)
    with col1:
        st.metric("✅ Exitosos", resultados["exitos"])
    with col2:
        st.metric("❌ Fallidos", resultados["fallidos"])

    omitidas = int(resultados.get("omitidas") or 0)
    if omitidas:
        st.caption(
            f"⛔ {omitidas} cuenta(s) omitida(s) (por su Tier o por estar "
            "pausadas para activación): no se abrió navegador ni se registró nada."
        )

    if resultados.get("detalles"):
        with st.expander("🔍 Detalle por cuenta"):
            for detalle in resultados["detalles"]:
                st.markdown(detalle)