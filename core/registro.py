"""Registro de acciones (log de publicaciones, RTs, likes, etc.).

Permite persistir el resultado de cada accion ejecutada por el sistema para
que la seccion de reportes pueda mostrar, por ejemplo, la URL de cada
publicacion exitosa.
"""
from loguru import logger

from core.database import get_db_session
from core.models import RegistroAccion


def registrar_accion(
    usuario: str,
    tipo: str,
    estado: str,
    url_publicacion: str = "",
    detalle: str = "",
) -> None:
    """Inserta una fila en la tabla 'registro_acciones'.

    En caso de error, lo registra con logger.warning y no propaga la excepcion.
    """
    try:
        with get_db_session() as db:
            db.add(
                RegistroAccion(
                    usuario=usuario,
                    tipo=tipo,
                    estado=estado,
                    url_publicacion=url_publicacion,
                    detalle=detalle,
                )
            )
    except Exception as e:
        logger.warning(f"Error registrando accion ({tipo}/{estado} de {usuario}): {e}")


def obtener_acciones(limit: int = 100) -> list:
    """Devuelve las ultimas 'limit' acciones ordenadas por fecha descendente.

    Ante cualquier error devuelve una lista vacia.
    """
    try:
        with get_db_session() as db:
            acciones = (
                db.query(RegistroAccion)
                .order_by(RegistroAccion.fecha.desc())
                .limit(limit)
                .all()
            )
            return list(acciones)
    except Exception as e:
        logger.warning(f"Error obteniendo acciones: {e}")
        return []
