from core.models import Tarea
from datetime import datetime
import json


def crear_tarea_post(
    plataforma: str,
    contenido: str,
    cuenta_ids: list[int],
    fecha_hora: datetime,
    imagen_path: str = "",
    creada_por: int = None
) -> Tarea:
    return Tarea(
        tipo="post",
        plataforma=plataforma,
        contenido=contenido,
        imagen_path=imagen_path,
        cuentas_ids=json.dumps(cuenta_ids),
        fecha_hora=fecha_hora,
        creada_por=creada_por
    )


def crear_tarea_retweet(
    urls: list[str],
    cuenta_ids: list[int],
    fecha_hora: datetime,
    creada_por: int = None
) -> Tarea:
    return Tarea(
        tipo="retweet",
        plataforma="twitter",
        contenido=json.dumps(urls),
        cuentas_ids=json.dumps(cuenta_ids),
        fecha_hora=fecha_hora,
        creada_por=creada_por
    )


def crear_tarea_like(
    urls: list[str],
    cuenta_ids: list[int],
    fecha_hora: datetime,
    creada_por: int = None
) -> Tarea:
    return Tarea(
        tipo="like",
        plataforma="twitter",
        contenido=json.dumps(urls),
        cuentas_ids=json.dumps(cuenta_ids),
        fecha_hora=fecha_hora,
        creada_por=creada_por
    )


def crear_tarea_visualizacion(
    plataforma: str,
    url: str,
    cuenta_ids: list[int],
    fecha_hora: datetime,
    creada_por: int = None
) -> Tarea:
    return Tarea(
        tipo="visualizacion",
        plataforma=plataforma,
        contenido=url,
        cuentas_ids=json.dumps(cuenta_ids),
        fecha_hora=fecha_hora,
        creada_por=creada_por
    )
