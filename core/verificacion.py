# -*- coding: utf-8 -*-
"""Cajon de verificacion humana de cuentas (soporte de datos en `core/`).

REGLA DE NEGOCIO
================
`Cuenta.verificacion_pendiente` marca las cuentas que necesitan que una
PERSONA las revise (login, captcha, verificacion de identidad, sesion caida...)
antes de volver a usarlas. Mientras esten pendientes se excluyen de las
campanas de activacion; al resolverlas se marcan como verificadas y vuelven a
estar disponibles.

API PUBLICA (nombres y firmas CONGELADOS):
=========================================
    esta_pendiente(cuenta) -> bool
    filtrar_para_activacion(cuentas, incluir_verificacion=False) -> list
    pendientes_usuarios(cuentas) -> list[str]
    marcar_pendiente(usuario, motivo="", origen="", plataforma="twitter") -> bool
    marcar_verificada(usuario, plataforma="twitter") -> bool
    listar_pendientes(plataforma="twitter", limite=1000) -> list[dict]
    contar_pendientes(plataforma="twitter") -> int

Estilo defensivo (igual que `core/pausas.py`): NINGUNA funcion lanza jamas.
Ante cualquier dato raro devuelve el valor conservador (False, [] o 0). No
importa `core.models` a nivel de modulo (solo dentro de las funciones que
tocan la BD) para evitar importaciones circulares con `core.database`.
"""
from __future__ import annotations

from datetime import datetime

__all__ = [
    "esta_pendiente",
    "filtrar_para_activacion",
    "pendientes_usuarios",
    "marcar_pendiente",
    "marcar_verificada",
    "listar_pendientes",
    "contar_pendientes",
]

# Cadenas que cuentan como "pendiente" (se comparan en minusculas y sin
# espacios, igual que core/pausas.py).
_VALORES_TRUE = frozenset(
    {"1", "true", "si", "sí", "yes", "on", "verdadero"}
)


# ---------------------------------------------------------------------------
# Utilidades internas tolerantes
# ---------------------------------------------------------------------------
def _texto(valor) -> str:
    """Convierte un valor a texto recortado; "" para None o errores."""
    try:
        if valor is None:
            return ""
        if isinstance(valor, (bytes, bytearray)):
            valor = valor.decode("utf-8", "ignore")
        return str(valor).strip()
    except Exception:
        return ""


def _leer_campo(cuenta, nombre: str, default=None):
    """Lee `nombre` de un ORM, dict o SimpleNamespace (nunca lanza).

    Primero intenta `getattr` (ORM/SimpleNamespace); si el objeto no expone el
    atributo y se comporta como dict (`dict` o `.get`), usa `.get`."""
    try:
        return getattr(cuenta, nombre)
    except Exception:
        pass
    try:
        if isinstance(cuenta, dict):
            return cuenta.get(nombre, default)
        obtener = getattr(cuenta, "get", None)
        if callable(obtener):
            return obtener(nombre, default)
    except Exception:
        pass
    return default


def _usuario(cuenta) -> str:
    """`.usuario` como texto recortado ("" si falta o falla)."""
    return _texto(_leer_campo(cuenta, "usuario", ""))


def _plataforma(valor) -> str:
    """Plataforma normalizada; default "twitter"."""
    return _texto(valor) or "twitter"


def _limite(valor, default: int = 1000) -> int:
    """Entero positivo para un LIMIT; default si viene vacio o invalido."""
    try:
        numero = int(valor)
    except (TypeError, ValueError, OverflowError):
        return default
    return numero if numero > 0 else default


def _fecha_iso(valor) -> str:
    """Fecha como ISO-8601 o "" (nunca lanza)."""
    try:
        if valor is None:
            return ""
        if isinstance(valor, datetime):
            return valor.isoformat()
        return str(valor)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Funciones puras (sin BD)
# ---------------------------------------------------------------------------
def esta_pendiente(cuenta) -> bool:
    """True si la cuenta esta en el cajon de verificacion humana.

    Acepta cualquier entrada (ORM, dict, SimpleNamespace, bool, numeros y
    cadenas "1"/"true"/"si"/"sí"). None/""/0/valor ausente -> False.
    Nunca lanza."""
    try:
        valor = _leer_campo(cuenta, "verificacion_pendiente", False)
        if valor is None:
            return False
        if isinstance(valor, bool):
            return valor
        if isinstance(valor, (int, float)):
            return valor != 0
        if isinstance(valor, (bytes, bytearray)):
            try:
                valor = valor.decode("utf-8", "ignore")
            except Exception:
                return False
        if isinstance(valor, str):
            return valor.strip().lower() in _VALORES_TRUE
        return bool(valor)
    except Exception:
        return False


def filtrar_para_activacion(cuentas, incluir_verificacion: bool = False) -> list:
    """Lista NUEVA de cuentas elegibles para activacion.

    Con `incluir_verificacion=False` (default) quita las pendientes de
    verificacion humana; con `incluir_verificacion=True` devuelve la lista
    completa. No muta la lista original y nunca lanza (entrada no iterable ->
    [])."""
    try:
        lista = list(cuentas)
    except Exception:
        return []
    if incluir_verificacion:
        return lista
    return [cuenta for cuenta in lista if not esta_pendiente(cuenta)]


def pendientes_usuarios(cuentas) -> list:
    """`.usuario` (texto no vacio) de las cuentas pendientes de la lista.

    Tolerante a objetos sin `.usuario`, con `.usuario = None` o valores que
    fallen al convertirse: esos se omiten. Nunca lanza."""
    try:
        lista = list(cuentas)
    except Exception:
        return []
    usuarios = []
    for cuenta in lista:
        try:
            if not esta_pendiente(cuenta):
                continue
            usuario = _usuario(cuenta)
            if usuario:
                usuarios.append(usuario)
        except Exception:
            continue
    return usuarios


# ---------------------------------------------------------------------------
# Funciones con BD (importes LOCALES para evitar ciclos)
# ---------------------------------------------------------------------------
def marcar_pendiente(
    usuario,
    motivo="",
    origen="",
    plataforma="twitter",
) -> bool:
    """Manda la cuenta al cajon de verificacion humana.

    Setea `verificacion_pendiente=True`, `verificacion_motivo` (recortado a
    300), `verificacion_origen` (recortado a 30) y `verificacion_fecha=ahora`
    (UTC), y confirma con commit. Devuelve True si la cuenta existe y el cambio
    se guardo; False si no existe, si el usuario/plataforma vienen vacios o si
    hubo cualquier error. Nunca lanza."""
    usuario_txt = _texto(usuario)
    if not usuario_txt:
        return False
    try:
        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import Cuenta

        plataforma_txt = _plataforma(plataforma)
        with get_db_session() as db:
            cuenta = (
                db.query(Cuenta)
                .filter(
                    Cuenta.usuario == usuario_txt,
                    Cuenta.plataforma == plataforma_txt,
                )
                .first()
            )
            if cuenta is None:
                return False
            cuenta.verificacion_pendiente = True
            cuenta.verificacion_motivo = _texto(motivo)[:300]
            cuenta.verificacion_origen = _texto(origen)[:30]
            cuenta.verificacion_fecha = datetime.utcnow()
            db.add(cuenta)
            return True
    except Exception:
        return False


def marcar_verificada(usuario, plataforma="twitter") -> bool:
    """Saca la cuenta del cajon (verificacion humana resuelta).

    Setea `verificacion_pendiente=False`, limpia motivo/origen y deja
    `verificacion_fecha=None`, y confirma con commit. Devuelve True solo si la
    cuenta existia y realmente habia algo que limpiar (estaba pendiente o
    tenia datos de verificacion); False si no existe, si ya estaba limpia o si
    hubo error. Nunca lanza."""
    usuario_txt = _texto(usuario)
    if not usuario_txt:
        return False
    try:
        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import Cuenta

        plataforma_txt = _plataforma(plataforma)
        with get_db_session() as db:
            cuenta = (
                db.query(Cuenta)
                .filter(
                    Cuenta.usuario == usuario_txt,
                    Cuenta.plataforma == plataforma_txt,
                )
                .first()
            )
            if cuenta is None:
                return False
            cambio = bool(
                esta_pendiente(cuenta)
                or _texto(getattr(cuenta, "verificacion_motivo", ""))
                or _texto(getattr(cuenta, "verificacion_origen", ""))
                or getattr(cuenta, "verificacion_fecha", None) is not None
            )
            cuenta.verificacion_pendiente = False
            cuenta.verificacion_motivo = ""
            cuenta.verificacion_origen = ""
            cuenta.verificacion_fecha = None
            db.add(cuenta)
            return cambio
    except Exception:
        return False


def listar_pendientes(plataforma="twitter", limite=1000) -> list:
    """Cuentas pendientes de verificacion, ordenadas por fecha descendente.

    Cada item es un dict con las claves `usuario, motivo, origen, fecha (ISO o
    ""), nombre_mostrado, handle_actual, tier_calidad, seccion, status,
    activa`. No incluye credenciales ni cookies. Ante cualquier error de BD
    devuelve [] (nunca lanza)."""
    try:
        from sqlalchemy import select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import Cuenta

        plataforma_txt = _plataforma(plataforma)
        tope = _limite(limite)
        columnas = (
            Cuenta.usuario,
            Cuenta.verificacion_motivo,
            Cuenta.verificacion_origen,
            Cuenta.verificacion_fecha,
            Cuenta.nombre_mostrado,
            Cuenta.handle_actual,
            Cuenta.tier_calidad,
            Cuenta.seccion,
            Cuenta.status,
            Cuenta.activa,
        )
        stmt = (
            select(*columnas)
            .where(
                Cuenta.plataforma == plataforma_txt,
                Cuenta.verificacion_pendiente.is_(True),
            )
            .order_by(Cuenta.verificacion_fecha.desc(), Cuenta.id.asc())
            .limit(tope)
        )
        with get_db_session() as db:
            filas = db.execute(stmt).all()

        pendientes = []
        for fila in filas:
            try:
                valores = list(fila)
                item = {
                    "usuario": _texto(valores[0]),
                    "motivo": _texto(valores[1]),
                    "origen": _texto(valores[2]),
                    "fecha": _fecha_iso(valores[3]),
                    "nombre_mostrado": _texto(valores[4]),
                    "handle_actual": _texto(valores[5]),
                    "tier_calidad": _texto(valores[6]),
                    "seccion": _texto(valores[7]),
                    "status": _texto(valores[8]),
                    "activa": bool(valores[9]),
                }
                pendientes.append(item)
            except Exception:
                continue
        return pendientes
    except Exception:
        return []


def contar_pendientes(plataforma="twitter") -> int:
    """Numero de cuentas pendientes de verificacion. 0 ante cualquier error."""
    try:
        from sqlalchemy import func, select

        from core.database import get_db_session  # import local (sin ciclos)
        from core.models import Cuenta

        plataforma_txt = _plataforma(plataforma)
        stmt = (
            select(func.count())
            .select_from(Cuenta)
            .where(
                Cuenta.plataforma == plataforma_txt,
                Cuenta.verificacion_pendiente.is_(True),
            )
        )
        with get_db_session() as db:
            total = db.execute(stmt).scalar()
        return int(total or 0)
    except Exception:
        return 0
