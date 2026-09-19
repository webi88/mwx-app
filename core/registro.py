"""Registro de acciones (log de publicaciones, RTs, likes, etc.).

Permite persistir el resultado de cada accion ejecutada por el sistema para
que la seccion de reportes pueda mostrar, por ejemplo, la URL de cada
publicacion exitosa.
"""
from datetime import datetime

from loguru import logger

from core.database import get_db_session
from core.models import Cuenta, RegistroAccion


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


def marcar_cuenta_suspendida(usuario: str) -> None:
    """Marca una cuenta como `status='suspended'` y la desactiva (`activa =
    False`) cuando el bot confirma (via `TwitterBot.cuenta_suspendida`) que X
    bloqueo la sesion. No borra nada: el borrado definitivo se hace a mano
    desde el dashboard (Cuentas > Estado > Cuentas suspendidas por X).

    En caso de error, lo registra con logger.warning y no propaga la excepcion.
    """
    try:
        with get_db_session() as db:
            reg = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if reg is not None:
                reg.status = "suspended"
                reg.activa = False
                reg.last_checked = datetime.utcnow()
    except Exception as e:
        logger.warning(f"Error marcando cuenta suspendida ({usuario}): {e}")


def obtener_acciones(limit: int = 100, solo_exitosas: bool = False) -> list:
    """Devuelve las ultimas 'limit' acciones ordenadas por fecha descendente.

    Args:
        limit: maximo de acciones a devolver (default 100).
        solo_exitosas: si es True, filtra solo las acciones con estado exitoso
            ("exito", "exitoso" u "ok"); si es False (default) devuelve todas.

    Ante cualquier error devuelve una lista vacia.
    """
    try:
        with get_db_session() as db:
            query = db.query(RegistroAccion)
            if solo_exitosas:
                query = query.filter(
                    RegistroAccion.estado.in_(("exito", "exitoso", "ok"))
                )
            acciones = (
                query.order_by(RegistroAccion.fecha.desc()).limit(limit).all()
            )
            return list(acciones)
    except Exception as e:
        logger.warning(f"Error obteniendo acciones: {e}")
        return []
