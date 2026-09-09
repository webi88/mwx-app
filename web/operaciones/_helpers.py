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


def ejecutar_en_cuentas(cuentas: list[Cuenta], accion, plataforma: str = "twitter",
                        progreso: st.progress = None, estado: st.empty = None,
                        tipo: str = "post") -> dict:
    """Ejecuta 'accion(bot)' sobre cada cuenta y acumula resultados."""
    from plataformas.base import PlataformaFactory
    
    resultados = {"exitos": 0, "fallidos": 0, "detalles": []}
    total = len(cuentas)
    
    for i, cuenta in enumerate(cuentas):
        try:
            if estado:
                estado.write(f"⏳ Trabajando con **@{cuenta.usuario}**...")
            
            bot = PlataformaFactory.crear_bot(plataforma, cuenta.usuario)
            res = accion(bot)
            bot.cerrar()
            
            ok = res
            url = res if isinstance(res, str) else (getattr(bot, "ultima_url_publicada", "") or "")
            
            if ok:
                resultados["exitos"] += 1
                if url:
                    resultados["detalles"].append(f"✅ @{cuenta.usuario} — [ver post]({url})")
                else:
                    resultados["detalles"].append(f"✅ @{cuenta.usuario}")
                registrar_accion(cuenta.usuario, tipo, "exito", url, "")
            else:
                resultados["fallidos"] += 1
                detalle = f"❌ @{cuenta.usuario}"
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