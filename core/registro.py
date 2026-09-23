"""Registro de acciones (log de publicaciones, RTs, likes, etc.).

Permite persistir el resultado de cada accion ejecutada por el sistema para
que la seccion de reportes pueda mostrar, por ejemplo, la URL de cada
publicacion exitosa.

Tambien expone el conteo en tiempo real de acciones EXITOSAS por cuenta y rol
de activacion (Sistema de Cuotas Inteligente por Hora): el motor de
activaciones lo consulta antes de ejecutar una accion para no pasarse de los
limites de X en cuentas no premium.
"""
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import func

from loguru import logger

from core.config import settings
from core.database import get_db_session
from core.models import Cuenta, RegistroAccion

# Estados de RegistroAccion que cuentan como accion EXITOSA.
ESTADOS_EXITO = ("exito", "exitoso", "ok")

# rol de activacion -> tipos con los que se registra en RegistroAccion.tipo.
ACCIONES_POR_ROL = {
    "hashtags": ("post", "hashtags", "publicacion", "mantenimiento", "calentamiento", "hilo"),
    "cita": ("cita", "quote"),
    "rt": ("rt", "retweet", "repost"),
    "comentario": ("comentario", "respuesta", "reply"),
}

# Tipo canonico con el que se registra una accion de cada rol.
TIPOS_REGISTRO_ROL = {"hashtags": "post", "cita": "cita", "rt": "rt", "comentario": "comentario"}

# Tipos de RegistroAccion que cuentan para el TOPE DIARIO TOTAL por cuenta:
# union deduplicada de TODOS los tipos de ACCIONES_POR_ROL (se conserva el
# orden de aparicion). El tope diario NO distingue roles: suma por igual
# posts/hashtags/publicaciones/mantenimiento/calentamiento/hilos, citas/quotes,
# RTs/retweets/reposts y comentarios/respuestas/replies.
ACCIONES_OPERATIVAS = tuple(
    dict.fromkeys(tipo for tipos in ACCIONES_POR_ROL.values() for tipo in tipos)
)


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


# --------------------------------------------------------------------------- #
# Cuotas inteligentes por hora (conteo en tiempo real)
# --------------------------------------------------------------------------- #
def _tokens_rol(valor) -> list:
    """Parte un valor en palabras ya normalizadas (minusculas, sin acentos).

    "Retweet con cita" -> ["retweet", "con", "cita"]; None/vacio -> []. La
    normalizacion es local a proposito: esta capa no debe importar core.roles.
    """
    if valor is None:
        return []
    texto = str(valor).strip().lower()
    for separador in ("_", "-", ".", "/", ",", ";", "(", ")", "[", "]"):
        texto = texto.replace(separador, " ")
    texto = "".join(
        caracter
        for caracter in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(caracter)
    )
    return texto.split()


def normalizar_rol_cuota(valor) -> str:
    """Devuelve el rol canonico de una cuota: hashtags/cita/rt/comentario.

    Acepta variantes con/sin acentos, mayusculas/minusculas y sinonimos:
        - "post", "posts", "publicacion", "hashtag(s)" -> "hashtags".
        - "quote", "citar", "cita con comentario" -> "cita".
        - "retweet", "repost", "rt" -> "rt".
        - "respuesta(s)", "reply", "comentar" -> "comentario".
    Si no reconoce el valor devuelve el texto limpio en minusculas (sin
    acentos); vacio o None -> "".
    """
    tokens = _tokens_rol(valor)
    if not tokens:
        return ""
    # La cita gana sobre "retweet": "Retweet con cita" es una cita, no un RT.
    for token in tokens:
        if token.startswith("cita") or token.startswith("quote"):
            return "cita"
    for token in tokens:
        if token.startswith("hashtag") or token.startswith("mencion"):
            return "hashtags"
    for token in tokens:
        if token in (
            "post",
            "posts",
            "publicacion",
            "publicaciones",
            "mantenimiento",
            "calentamiento",
            "hilo",
            "hilos",
        ) or token.startswith("publicac"):
            return "hashtags"
    for token in tokens:
        if (
            token.startswith("coment")
            or token in ("reply", "responder", "respuesta", "respuestas")
        ):
            return "comentario"
    for token in tokens:
        if token == "rt" or token.startswith("retweet") or token.startswith("repost"):
            return "rt"
    return " ".join(tokens)


def tipo_registro_rol(rol) -> str:
    """Tipo canonico con el que registrar una accion de ese rol.

    Usa TIPOS_REGISTRO_ROL ("hashtags" -> "post", "cita" -> "cita",
    "rt" -> "rt", "comentario" -> "comentario"). Si el rol no esta en el mapa
    devuelve el rol limpio tal cual; si viene vacio/None -> "activacion"."""
    normalizado = normalizar_rol_cuota(rol)
    if not normalizado:
        return "activacion"
    return TIPOS_REGISTRO_ROL.get(normalizado, normalizado)


def limite_por_rol(rol) -> int:
    """Cuota de acciones EXITOSAS por cuenta para ese rol (0 = sin limite).

    - "hashtags"/"post" -> settings.limite_posts_hora
    - "cita"            -> settings.limite_citas_hora
    - "rt"              -> settings.limite_rts_hora
    - "comentario"      -> settings.limite_comentarios_hora
    Con settings.limite_cuotas_activo=False siempre devuelve 0 (ilimitado).
    Rol desconocido/vacio o valor de configuracion invalido -> 0.
    """
    try:
        if not getattr(settings, "limite_cuotas_activo", True):
            return 0
        normalizado = normalizar_rol_cuota(rol)
        if normalizado == "hashtags":
            valor = getattr(settings, "limite_posts_hora", 0)
        elif normalizado == "cita":
            valor = getattr(settings, "limite_citas_hora", 0)
        elif normalizado == "rt":
            valor = getattr(settings, "limite_rts_hora", 0)
        elif normalizado == "comentario":
            valor = getattr(settings, "limite_comentarios_hora", 0)
        else:
            return 0
        limite = int(valor)
        return limite if limite > 0 else 0
    except Exception as e:
        logger.warning(f"Error obteniendo limite por rol ({rol}): {e}")
        return 0


def limite_acciones_dia() -> int:
    """Tope DIARIO total de acciones EXITOSAS por cuenta (0 = sin tope).

    Lee `settings.limite_acciones_dia`; devuelve 0 si el interruptor
    `settings.limite_diario_activo` esta apagado o si el valor es invalido/<=0
    (mismos patrones tolerantes que `limite_por_rol`: nunca lanza). El tope no
    distingue roles: la suma es sobre ACCIONES_OPERATIVAS.
    """
    try:
        if not getattr(settings, "limite_diario_activo", True):
            return 0
        valor = getattr(settings, "limite_acciones_dia", 0)
        limite = int(valor)
        return limite if limite > 0 else 0
    except Exception as e:
        logger.warning(f"Error obteniendo limite diario de acciones: {e}")
        return 0


def _ventana_minutos(minutos) -> int:
    """Ventana efectiva en minutos: `minutos` > 0 o settings.limite_ventana_min.

    Valores None, <= 0 o no numericos caen al default de configuracion (60 si
    tampoco ese valor fuese valido)."""
    try:
        valor = int(minutos)
    except (TypeError, ValueError):
        valor = 0
    if valor > 0:
        return valor
    try:
        por_defecto = int(getattr(settings, "limite_ventana_min", 60))
    except (TypeError, ValueError):
        por_defecto = 60
    return por_defecto if por_defecto > 0 else 60


def _ventana_dia_minutos(minutos) -> int:
    """Ventana efectiva del tope diario: `minutos` > 0 o settings.limite_dia_ventana_min.

    Es un helper PROPIO (no reusa `_ventana_minutos`, cuyo default es 60):
    valores None, <= 0 o no numericos caen a settings.limite_dia_ventana_min
    (1440 si tampoco ese valor fuese valido)."""
    try:
        valor = int(minutos)
    except (TypeError, ValueError):
        valor = 0
    if valor > 0:
        return valor
    try:
        por_defecto = int(getattr(settings, "limite_dia_ventana_min", 1440))
    except (TypeError, ValueError):
        por_defecto = 1440
    return por_defecto if por_defecto > 0 else 1440


def contar_acciones_recientes(usuario: str, rol: str, minutos: int = 60) -> int:
    """Cuenta las acciones EXITOSAS de `usuario` para ese rol en la ventana.

    Una sola consulta agregada (COUNT) sobre RegistroAccion: estado exitoso,
    fecha dentro de la ventana y tipo en ACCIONES_POR_ROL[rol]. Si el rol no
    esta en el mapa se filtra por `tipo == rol` (limpio). `minutos` invalido
    usa settings.limite_ventana_min. Nunca lanza: ante cualquier error -> 0.
    """
    try:
        if not usuario:
            return 0
        desde = datetime.utcnow() - timedelta(minutes=_ventana_minutos(minutos))
        normalizado = normalizar_rol_cuota(rol)
        tipos = ACCIONES_POR_ROL.get(normalizado)
        with get_db_session() as db:
            query = db.query(func.count(RegistroAccion.id)).filter(
                RegistroAccion.usuario == str(usuario),
                RegistroAccion.estado.in_(ESTADOS_EXITO),
                RegistroAccion.fecha >= desde,
            )
            if tipos:
                query = query.filter(RegistroAccion.tipo.in_(tipos))
            else:
                query = query.filter(RegistroAccion.tipo == normalizado)
            resultado = query.scalar()
            return int(resultado or 0)
    except Exception as e:
        logger.warning(f"Error contando acciones recientes ({usuario}/{rol}): {e}")
        return 0


def _mapa_tipo_a_rol() -> dict:
    """Mapeo inverso tipo -> rol (si un tipo cayera en 2 roles, gana el 1o)."""
    mapa = {}
    for rol, tipos in ACCIONES_POR_ROL.items():
        for tipo in tipos:
            mapa.setdefault(tipo, rol)
    return mapa


def contar_acciones_por_usuario(usuarios, minutos: int = 60) -> dict:
    """Cuenta acciones EXITOSAS por usuario y rol con UNA consulta agrupada.

    Agrupa por (usuario, tipo) filtrando por la lista de usuarios, estado
    exitoso y la ventana de fecha; convierte los tipos a roles con el mapeo
    inverso y descarta los tipos que no pertenecen a ningun rol. Devuelve
    {usuario: {rol: conteo}} (solo entradas > 0). Lista vacia -> {} y ante
    cualquier error -> {} (nunca lanza).
    """
    try:
        if usuarios is None:
            return {}
        if isinstance(usuarios, str):
            usuarios = [usuarios]
        lista = [str(usuario) for usuario in usuarios if usuario]
        if not lista:
            return {}
        desde = datetime.utcnow() - timedelta(minutes=_ventana_minutos(minutos))
        tipo_a_rol = _mapa_tipo_a_rol()
        with get_db_session() as db:
            filas = (
                db.query(
                    RegistroAccion.usuario,
                    RegistroAccion.tipo,
                    func.count(RegistroAccion.id),
                )
                .filter(
                    RegistroAccion.usuario.in_(lista),
                    RegistroAccion.estado.in_(ESTADOS_EXITO),
                    RegistroAccion.fecha >= desde,
                )
                .group_by(RegistroAccion.usuario, RegistroAccion.tipo)
                .all()
            )
        resultado = {}
        for usuario, tipo, conteo in filas:
            rol = tipo_a_rol.get(str(tipo or "").strip().lower())
            if rol is None:
                continue
            total = int(conteo or 0)
            if total <= 0:
                continue
            por_rol = resultado.setdefault(str(usuario), {})
            por_rol[rol] = por_rol.get(rol, 0) + total
        return resultado
    except Exception as e:
        logger.warning(f"Error contando acciones por usuario: {e}")
        return {}


# --------------------------------------------------------------------------- #
# Tope diario total por cuenta (sin distinguir rol)
# --------------------------------------------------------------------------- #
def contar_acciones_dia(usuario: str, minutos: int = None) -> int:
    """Cuenta las acciones OPERATIVAS EXITOSAS de `usuario` en la ventana diaria.

    Una sola consulta agregada (COUNT) sobre RegistroAccion: estado exitoso,
    fecha dentro de la ventana y tipo en ACCIONES_OPERATIVAS. `minutos` None,
    <= 0 o no numerico usa settings.limite_dia_ventana_min (1440 = 24 h); un
    `minutos` explicito > 0 manda. Nunca lanza: ante cualquier error -> 0.
    """
    try:
        if not usuario:
            return 0
        desde = datetime.utcnow() - timedelta(minutes=_ventana_dia_minutos(minutos))
        with get_db_session() as db:
            resultado = (
                db.query(func.count(RegistroAccion.id))
                .filter(
                    RegistroAccion.usuario == str(usuario),
                    RegistroAccion.estado.in_(ESTADOS_EXITO),
                    RegistroAccion.fecha >= desde,
                    RegistroAccion.tipo.in_(ACCIONES_OPERATIVAS),
                )
                .scalar()
            )
            return int(resultado or 0)
    except Exception as e:
        logger.warning(f"Error contando acciones del dia ({usuario}): {e}")
        return 0


def contar_acciones_dia_por_usuario(usuarios, minutos: int = None) -> dict:
    """Cuenta acciones OPERATIVAS EXITOSAS por usuario con UNA consulta agrupada.

    Agrupa por usuario (`usuario, COUNT(id)`) filtrando por la lista de
    usuarios, estado exitoso, la ventana diaria (`_ventana_dia_minutos`) y
    tipo en ACCIONES_OPERATIVAS. Devuelve {usuario: total} solo con entradas
    > 0; lista vacia/None -> {} y ante cualquier error -> {} (nunca lanza).
    """
    try:
        if usuarios is None:
            return {}
        if isinstance(usuarios, str):
            usuarios = [usuarios]
        lista = [str(usuario) for usuario in usuarios if usuario]
        if not lista:
            return {}
        desde = datetime.utcnow() - timedelta(minutes=_ventana_dia_minutos(minutos))
        with get_db_session() as db:
            filas = (
                db.query(
                    RegistroAccion.usuario,
                    func.count(RegistroAccion.id),
                )
                .filter(
                    RegistroAccion.usuario.in_(lista),
                    RegistroAccion.estado.in_(ESTADOS_EXITO),
                    RegistroAccion.fecha >= desde,
                    RegistroAccion.tipo.in_(ACCIONES_OPERATIVAS),
                )
                .group_by(RegistroAccion.usuario)
                .all()
            )
        resultado = {}
        for usuario, conteo in filas:
            total = int(conteo or 0)
            if total > 0:
                resultado[str(usuario)] = total
        return resultado
    except Exception as e:
        logger.warning(f"Error contando acciones del dia por usuario: {e}")
        return {}


# --------------------------------------------------------------------------- #
# "Agotada por hoy": tope diario alcanzado (LIMITE_DIARIO_POR_CUENTA)
# --------------------------------------------------------------------------- #
def _tope_dia_efectivo(limite=None) -> int:
    """Tope diario efectivo en acciones: `limite` explicito > 0 manda.

    Si no, usa `limite_acciones_dia()` (que respeta
    `settings.limite_diario_activo` y `settings.limite_acciones_dia`,
    alimentado por `LIMITE_DIARIO_POR_CUENTA` / alias legado
    `LIMITE_ACCIONES_DIA`). 0 = sin tope. Nunca lanza."""
    try:
        valor = int(limite)
    except (TypeError, ValueError):
        valor = 0
    if valor > 0:
        return valor
    return limite_acciones_dia()


def esta_agotada_dia(usuario: str, limite: int = None) -> bool:
    """True si la cuenta esta "Agotada por hoy".

    "Agotada por hoy" = alcanzo `LIMITE_DIARIO_POR_CUENTA`
    (`settings.limite_acciones_dia`) acciones OPERATIVAS EXITOSAS en las
    ultimas 24 h (`LIMITE_DIA_VENTANA_MIN` = 1440 min). Un `limite` explicito
    > 0 manda sobre settings; si el tope efectivo es 0 (desactivado o
    ilimitado) devuelve False sin consultar. Ante cualquier error -> False
    (nunca lanza)."""
    try:
        tope = _tope_dia_efectivo(limite)
        if tope <= 0:
            return False
        return contar_acciones_dia(usuario) >= tope
    except Exception as e:
        logger.warning(f"Error verificando tope diario ({usuario}): {e}")
        return False


def usuarios_agotados_dia(usuarios, limite: int = None) -> set:
    """Subconjunto de `usuarios` que estan "Agotada por hoy".

    "Agotada por hoy" = alcanzo `LIMITE_DIARIO_POR_CUENTA`
    (`settings.limite_acciones_dia`) acciones OPERATIVAS EXITOSAS en las
    ultimas 24 h (`LIMITE_DIA_VENTANA_MIN` = 1440 min). Usa UNA sola consulta
    agrupada (`contar_acciones_dia_por_usuario`). Un `limite` explicito > 0
    manda sobre settings; si el tope efectivo es 0 (desactivado o ilimitado)
    devuelve un set vacio SIN consultar la BD. Lista vacia/None -> set();
    ante cualquier error -> set() (nunca lanza)."""
    try:
        tope = _tope_dia_efectivo(limite)
        if tope <= 0:
            return set()
        conteos = contar_acciones_dia_por_usuario(usuarios)
        return {
            str(usuario)
            for usuario, total in conteos.items()
            if int(total) >= tope
        }
    except Exception as e:
        logger.warning(f"Error listando cuentas agotadas del dia: {e}")
        return set()
