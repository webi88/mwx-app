"""Restauracion de cuentas desde un respaldo JSON a la base de datos.

El respaldo creado al limpiar la flota (`data/backups/limpieza_cuentas_*.json`)
tiene esta forma:

    {
      "fecha": "2026-09-21T14:21:29.462094",
      "host": "...",
      "keep": ["usuario_que_no_se_toca", ...],
      "cuentas": [ {..fila de la tabla `cuentas`..}, ... ],
      "tareas": [ ... ]              # historico: NO se restaura
    }

Tambien se acepta una lista plana de dicts (sin `keep`).

Uso local (BD SQLite por defecto `data/gestor_redes.db`, simulacion):

    from cuentas.restaurador import restaurar_desde_backup
    resumen = restaurar_desde_backup(
        "data/backups/limpieza_cuentas_20260921_142129.json", dry_run=True
    )
    print(resumen["restauradas"], resumen["omitidas"])

Restauracion real en Supabase (exportar la DATABASE_URL de Railway antes):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python restaurar_cuentas.py --apply --inactivas

Reglas:
    - Nunca lanza: los fallos se devuelven en `detalle_errores` con ok=False.
    - Toda la restauracion va en UNA transaccion (`get_db_session`): si algo
      revienta, se hace rollback y no queda nada a medias.
    - Los usuarios de `keep` JAMAS se crean ni se actualizan, ni con
      `sobrescribir=True`.
    - `id` nunca se copia: la BD asigna el nuevo. `fecha_creacion` solo se
      conserva al crear.
    - `activa=False` con `inactivas=True` se aplica a las cuentas NUEVAS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import DateTime

from core.database import get_db_session
from core.models import Cuenta

# Columnas que no se copian/actualizan nunca.
_EXCLUIDAS_AL_ACTUALIZAR = ("id", "usuario", "fecha_creacion")


def _parsear_fecha(valor: Any) -> Optional[datetime]:
    """Convierte un valor de fecha del respaldo a `datetime` (o None).

    Acepta:
        - `datetime` (se devuelve tal cual).
        - Texto ISO: "2026-09-21T14:21:29", con microsegundos y/o sufijo `Z`
          (UTC), p. ej. "2026-09-21T14:21:29.462094Z".
        - None / cadena vacia.

    Cualquier otro valor (int, dict, texto invalido) devuelve None. NUNCA
    lanza. Las fechas con zona horaria se normalizan a UTC sin tzinfo para que
    encajen con las columnas DateTime (timestamp sin zona) de SQLite/PostgreSQL.
    """
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if not texto:
        return None

    candidatos = [texto]
    if texto.endswith(("Z", "z")):  # tolerante a Python < 3.11
        candidatos.append(f"{texto[:-1]}+00:00")
    for candidato in candidatos:
        try:
            fecha = datetime.fromisoformat(candidato)
        except (ValueError, TypeError):
            continue
        if fecha.tzinfo is not None:
            fecha = fecha.astimezone(timezone.utc).replace(tzinfo=None)
        return fecha
    return None


def _convertir_valor(columna, valor: Any) -> Any:
    """Aplica `_parsear_fecha` a columnas DateTime; deja el resto tal cual."""
    if isinstance(columna.type, DateTime):
        return _parsear_fecha(valor)
    return valor


def _columnas_cuenta() -> list:
    """Columnas reales del modelo `Cuenta` (fuente unica de verdad)."""
    return list(Cuenta.__table__.columns)


def _construir_cuenta(fila: dict, usuario: str, columnas: list, inactivas: bool = False) -> Cuenta:
    """Crea un `Cuenta` con las columnas validas presentes en la fila.

    - `id` se salta para que la BD asigne el nuevo.
    - Se ignoran las claves desconocidas (no existen en el modelo).
    - `fecha_creacion`/`last_checked` se convierten con `_parsear_fecha`.
    - Con `inactivas=True` la cuenta nace con `activa=False`.
    """
    datos: Dict[str, Any] = {}
    for columna in columnas:
        if columna.name in ("id", "usuario"):
            continue
        if columna.name not in fila:
            continue
        valor = _convertir_valor(columna, fila[columna.name])
        if valor is None and not columna.nullable:
            # No pisar columnas NOT NULL con None (el JSON puede traer null).
            continue
        datos[columna.name] = valor
    datos["usuario"] = usuario
    if inactivas:
        datos["activa"] = False
    return Cuenta(**datos)


def _actualizar_cuenta(cuenta, fila: dict, columnas: list) -> None:
    """Pisa en `cuenta` las columnas presentes en la fila del respaldo.

    Nunca toca `id`, `usuario` ni `fecha_creacion`. Las claves desconocidas se
    ignoran y los DateTime se convierten con `_parsear_fecha`.
    """
    for columna in columnas:
        if columna.name in _EXCLUIDAS_AL_ACTUALIZAR:
            continue
        if columna.name not in fila:
            continue
        valor = _convertir_valor(columna, fila[columna.name])
        if valor is None and not columna.nullable:
            continue
        setattr(cuenta, columna.name, valor)


def restaurar_desde_backup(
    ruta,
    dry_run: bool = False,
    sobrescribir: bool = False,
    inactivas: bool = False,
) -> Dict[str, Any]:
    """Restaura cuentas de un respaldo JSON a la BD de `DATABASE_URL`.

    Parametros:
        ruta: archivo JSON del respaldo (dict con `cuentas`/`keep` o lista
              plana de dicts de cuenta).
        dry_run: si es True NO escribe nada; solo cuenta lo que haria.
        sobrescribir: si una cuenta ya existe (mismo `usuario` exacto), en vez
              de omitirla actualiza sus columnas (excepto `id`, `usuario` y
              `fecha_creacion`).
        inactivas: las cuentas NUEVAS se crean con `activa=False` (no entran
              en campanas hasta reactivarlas).

    Devuelve siempre un dict:
        {
          "ok": bool,                # True si el respaldo se proceso y la
                                     # transaccion termino; False si el archivo/
                                     # JSON fallo o la BD reviento (rollback).
          "total": int,              # filas en `cuentas`
          "restauradas": int,        # creadas
          "actualizadas": int,       # existentes pisadas (requiere sobrescribir)
          "omitidas": int,           # ya existentes sin sobrescribir + keep
          "errores": int,            # filas invalidas + fallos fatales
          "detalle_errores": [str],  # mensajes (primeros errores)
          "dry_run": bool,
          "archivo": str,
        }

    Los usuarios listados en `keep` se cuentan en `omitidas` y no se tocan
    aunque existan y `sobrescribir=True`. La operacion es atomica: usa
    `get_db_session` y si algo revienta el context manager hace rollback y la
    funcion devuelve ok=False con el error. NUNCA lanza.
    """
    resultado: Dict[str, Any] = {
        "ok": False,
        "total": 0,
        "restauradas": 0,
        "actualizadas": 0,
        "omitidas": 0,
        "errores": 0,
        "detalle_errores": [],
        "dry_run": bool(dry_run),
        "archivo": str(ruta) if ruta else "",
    }

    def _error(mensaje: str) -> None:
        resultado["detalle_errores"].append(mensaje)
        resultado["errores"] += 1

    if not ruta:
        _error("No se indico la ruta del respaldo")
        return resultado

    # ------------------------------------------------------------------ #
    # 1) Leer el respaldo (nunca lanza)
    # ------------------------------------------------------------------ #
    try:
        with Path(ruta).open("r", encoding="utf-8") as manejador:
            data = json.load(manejador)
    except FileNotFoundError:
        _error(f"Archivo no encontrado: {ruta}")
        return resultado
    except Exception as exc:
        _error(f"No se pudo leer el respaldo ({type(exc).__name__}: {exc})")
        return resultado

    keep = set()
    if isinstance(data, dict):
        crudo_keep = data.get("keep")
        if isinstance(crudo_keep, (list, tuple, set)):
            keep = {str(u).strip() for u in crudo_keep if str(u or "").strip()}
        cuentas = data.get("cuentas")
    elif isinstance(data, list):
        cuentas = data
    else:
        cuentas = None

    if not isinstance(cuentas, list):
        _error("El respaldo no contiene la lista 'cuentas'")
        return resultado

    resultado["total"] = len(cuentas)
    columnas = _columnas_cuenta()

    # ------------------------------------------------------------------ #
    # 2) Recorrer y restaurar en UNA transaccion
    # ------------------------------------------------------------------ #
    try:
        with get_db_session() as db:
            for indice, fila in enumerate(cuentas, start=1):
                if not isinstance(fila, dict):
                    _error(f"fila {indice}: no es un objeto de cuenta")
                    continue

                usuario = str(fila.get("usuario") or "").strip()
                if not usuario:
                    _error(f"fila {indice}: usuario vacio, se omite")
                    continue

                # `keep` manda sobre todo: nunca se crea ni se actualiza.
                if usuario in keep:
                    resultado["omitidas"] += 1
                    continue

                existente = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()

                if existente is None:
                    if not dry_run:
                        db.add(_construir_cuenta(fila, usuario, columnas, inactivas=inactivas))
                    resultado["restauradas"] += 1
                    continue

                if not sobrescribir:
                    resultado["omitidas"] += 1
                    continue

                if not dry_run:
                    _actualizar_cuenta(existente, fila, columnas)
                resultado["actualizadas"] += 1
    except Exception as exc:
        # El context manager ya hizo rollback: nada quedo a medias.
        _error(f"Error de base de datos: {type(exc).__name__}: {exc}")
        return resultado

    resultado["ok"] = True
    return resultado
