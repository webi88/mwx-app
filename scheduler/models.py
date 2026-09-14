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


def crear_tarea_comentario(
    texto: str,
    url: str,
    cuenta_ids: list[int],
    fecha_hora: datetime,
    creada_por: int = None
) -> Tarea:
    """Crea una tarea de comentario/respuesta a un tweet.

    ``contenido`` se guarda como JSON ``{"url": ..., "texto": ...}`` para que
    el ejecutor pueda separar destino y redaccion; ``plataforma`` es "twitter"
    porque el comentario usa ``responder_tweet``.
    """
    return Tarea(
        tipo="comentario",
        plataforma="twitter",
        contenido=json.dumps({"url": url, "texto": texto}),
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


def crear_tareas_desde_plan(
    plan: list[dict],
    cuentas_id_por_usuario: dict,
    creada_por: int = None
) -> list[Tarea]:
    """Convierte un plan de ``scheduler.distribucion_horaria`` en ``Tarea``s.

    Mapeo por tipo de accion:
        - "post"       -> ``Tarea(tipo="post", contenido=texto)``
        - "comentario" -> :func:`crear_tarea_comentario` (url + texto en JSON)
        - "retweet"    -> ``Tarea(tipo="retweet", contenido=json.dumps([url]))``

    ``cuentas_id_por_usuario`` es un dict ``usuario -> id`` (tambien acepta
    objetos con atributo ``.id``). Las acciones sin cuenta mapeada, sin fecha
    o con tipo desconocido se omiten. Acepta tanto el plan horario clasico
    como el plan de campana 3+3+3 (mismos dicts con ``usuario``, ``tipo``,
    ``fecha_hora``, ``texto`` y ``url``; las claves extra se ignoran).
    Nunca lanza.
    """
    tareas: list[Tarea] = []
    if not plan or not isinstance(cuentas_id_por_usuario, dict):
        return tareas

    for accion in plan:
        try:
            if not isinstance(accion, dict):
                continue
            usuario = str(accion.get("usuario") or "").strip()
            cuenta_id = cuentas_id_por_usuario.get(usuario)
            if cuenta_id is None:
                continue
            if not isinstance(cuenta_id, int):
                cuenta_id = getattr(cuenta_id, "id", None)
            if cuenta_id is None:
                continue

            tipo = str(accion.get("tipo") or "").strip().lower()
            fecha_hora = accion.get("fecha_hora")
            if fecha_hora is None:
                continue
            texto = accion.get("texto") or ""
            url = accion.get("url") or ""

            if tipo == "post":
                tareas.append(Tarea(
                    tipo="post",
                    plataforma="twitter",
                    contenido=texto,
                    cuentas_ids=json.dumps([cuenta_id]),
                    fecha_hora=fecha_hora,
                    creada_por=creada_por
                ))
            elif tipo == "comentario":
                tareas.append(
                    crear_tarea_comentario(texto, url, [cuenta_id], fecha_hora, creada_por)
                )
            elif tipo == "retweet":
                tareas.append(Tarea(
                    tipo="retweet",
                    plataforma="twitter",
                    contenido=json.dumps([url]) if url else "[]",
                    cuentas_ids=json.dumps([cuenta_id]),
                    fecha_hora=fecha_hora,
                    creada_por=creada_por
                ))
        except Exception:
            continue

    return tareas
