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


def ejecutar_en_cuentas(cuentas: list[Cuenta], accion, plataforma: str = "twitter",
                        progreso: st.progress = None, estado: st.empty = None,
                        tipo: str = "post") -> dict:
    """Ejecuta 'accion(bot)' sobre cada cuenta y acumula resultados.

    'accion' puede ser un callable o una lista/tupla de callables (uno por
    cuenta, en el mismo orden). Esto permite publicar un texto distinto en
    cada cuenta: `[lambda bot, t=t: bot.publicar_tweet(t) for t in pool]`.
    """
    from plataformas.base import PlataformaFactory
    
    resultados = {"exitos": 0, "fallidos": 0, "detalles": []}
    total = len(cuentas)
    if isinstance(accion, (list, tuple)):
        acciones = list(accion)
    else:
        acciones = [accion] * total
    
    for i, cuenta in enumerate(cuentas):
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
    
    if resultados.get("detalles"):
        with st.expander("🔍 Detalle por cuenta"):
            for detalle in resultados["detalles"]:
                st.markdown(detalle)