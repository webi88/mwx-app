# -*- coding: utf-8 -*-
"""Carga de los clientes de alertas desde el config.json del sistema viejo.

Los 5 clientes del patron (ORA, ISLA, Harfuch, Tobogan, pinfra) viven en
``alertas_keywords`` del ``config.json`` de GestorTwitter. Este script los
normaliza al esquema actual de ``core.models.Cliente`` y hace UPSERT por
nombre (case-insensitive) dentro de una celula (default "Clientes"):

    keywords          -> JSON string (columna Text)
    localidad         -> texto
    telegram_chat_id  -> telegram_chat_ids (singular -> plural "id1, id2")
    exclude_terms     -> JSON string
    num_principales   -> entero
    activo            -> True

Reglas:
  - DRY-RUN por defecto (no escribe NADA); `--apply` escribe en la BD.
  - Nunca pisa con vacios: si el origen trae keywords/chats/localidad/
    exclude_terms vacios, se conserva lo que ya hubiera en la BD.
  - Funciona igual contra SQLite local o PostgreSQL/Supabase: todo pasa por
    `core.database` (`DATABASE_URL`).
  - Nunca imprime claves/secretos (solo lee `alertas_keywords` del config).

Uso:
    python importar_clientes_alertas.py                    # dry-run
    python importar_clientes_alertas.py --apply            # escribe
    python importar_clientes_alertas.py --desde RUTA --celula Clientes --apply
    python importar_clientes_alertas.py --json             # salida JSON
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import func

from core.database import get_db_session
from core.models import Celula, Cliente
from loguru import logger

#: Config del proyecto original (GestorTwitter) con `alertas_keywords`.
RUTA_POR_DEFECTO = (
    r"C:\Users\23boy\Downloads\GestorTwitter\GestorTwitter\config.json"
)
CELULA_POR_DEFECTO = "Clientes"
NARRATIVA_POR_DEFECTO = (
    "Clientes de alertas (cargados con importar_clientes_alertas.py)"
)


# --------------------------------------------------------------------------- #
# Lectura y normalizacion del origen
# --------------------------------------------------------------------------- #
def _leer_texto(ruta: str) -> str:
    """Lee el archivo probando varias codificaciones (configs viejos)."""
    with open(ruta, "rb") as f:
        crudo = f.read()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return crudo.decode(encoding)
        except UnicodeDecodeError:
            continue
    return crudo.decode("utf-8", errors="replace")


def normalizar_chats(valor) -> str:
    """Convierte `telegram_chat_id(s)` en ``"id1, id2"`` (sin vacios ni repetidos).

    Acepta el string del config viejo (``"-1, -2"``), una lista/tupla o None.
    Nunca lanza.
    """
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple, set)):
        partes = list(valor)
    else:
        partes = str(valor).split(",")
    limpios = []
    for parte in partes:
        texto = str(parte or "").strip()
        if texto and texto not in limpios:
            limpios.append(texto)
    return ", ".join(limpios)


def _lista_json(valor) -> list:
    """Normaliza a lista: acepta listas, JSON string, valor simple o None."""
    if valor is None or valor == "":
        return []
    if isinstance(valor, list):
        return valor
    if isinstance(valor, str):
        try:
            dato = json.loads(valor)
        except (TypeError, ValueError):
            return [valor]
        return dato if isinstance(dato, list) else [dato]
    return [valor]


def normalizar_cliente(nombre: str, datos) -> dict:
    """Normaliza UN cliente del config viejo al contrato de `Cliente`."""
    datos = datos if isinstance(datos, dict) else {}
    keywords = [
        str(k).strip() for k in _lista_json(datos.get("keywords")) if str(k or "").strip()
    ]
    exclude_terms = [
        str(t).strip()
        for t in _lista_json(datos.get("exclude_terms"))
        if str(t or "").strip()
    ]
    chats = normalizar_chats(
        datos.get("telegram_chat_ids", datos.get("telegram_chat_id"))
    )
    localidad = str(datos.get("localidad") or "").strip()
    try:
        num_principales = int(datos.get("num_principales") or 5)
    except (TypeError, ValueError):
        num_principales = 5
    return {
        "nombre": str(nombre or "").strip(),
        "keywords": keywords,
        "localidad": localidad,
        "telegram_chat_ids": chats,
        "exclude_terms": exclude_terms,
        "num_principales": max(0, num_principales),
    }


def normalizar_clientes(crudos: dict) -> list[dict]:
    """Normaliza el dict `alertas_keywords` completo (nunca lanza)."""
    clientes = []
    if not isinstance(crudos, dict):
        return clientes
    for nombre, datos in crudos.items():
        cliente = normalizar_cliente(nombre, datos)
        if cliente["nombre"]:
            clientes.append(cliente)
    return clientes


def cargar_clientes_desde_config(ruta: str) -> list[dict]:
    """Devuelve los clientes normalizados de `alertas_keywords` del JSON.

    Lanza ``ValueError`` con mensaje claro si el archivo no existe, no es JSON
    o no trae `alertas_keywords`. Nunca imprime secretos: del config solo se
    lee esa clave.
    """
    try:
        contenido = _leer_texto(ruta)
        datos = json.loads(contenido)
    except FileNotFoundError:
        raise ValueError(f"No existe el archivo de origen: {ruta}")
    except (OSError, ValueError) as e:
        raise ValueError(f"No se pudo leer el config de origen {ruta}: {e}")
    if not isinstance(datos, dict):
        raise ValueError("El config de origen no es un objeto JSON")
    crudos = datos.get("alertas_keywords")
    if not isinstance(crudos, dict) or not crudos:
        raise ValueError("El config no trae 'alertas_keywords' (o esta vacio)")
    clientes = normalizar_clientes(crudos)
    if not clientes:
        raise ValueError("El config no trae ningun cliente con nombre valido")
    return clientes


# --------------------------------------------------------------------------- #
# UPSERT en la BD (SQLite o Supabase; sesion inyectable para tests)
# --------------------------------------------------------------------------- #
def _obtener_celula(db, nombre: str, dry_run: bool) -> tuple:
    """Devuelve ``(celula, creada)``; en dry-run no persiste la celula nueva."""
    objetivo = str(nombre or "").strip() or CELULA_POR_DEFECTO
    celula = (
        db.query(Celula)
        .filter(func.lower(Celula.nombre) == objetivo.lower())
        .first()
    )
    if celula is not None:
        return celula, False
    celula = Celula(
        nombre=objetivo, narrativa=NARRATIVA_POR_DEFECTO, activa=True
    )
    if not dry_run:
        db.add(celula)
        db.flush()
    return celula, True


def _mismo_json_lista(actual, nueva: list) -> bool:
    """True si `actual` (columna Text JSON) equivale a la lista `nueva`."""
    if actual in (None, ""):
        actual_lista = []
    elif isinstance(actual, str):
        try:
            actual_lista = json.loads(actual)
        except (TypeError, ValueError):
            return False
    else:
        actual_lista = actual
    if not isinstance(actual_lista, list):
        return False
    return actual_lista == nueva


def _aplicar_cliente(
    db, datos: dict, celula_id, dry_run: bool
) -> dict:
    """Crea o actualiza UN cliente. Devuelve el reporte de la accion.

    En updates NUNCA pisa con vacios (keywords/localidad/chats/exclude_terms):
    si el origen viene vacio se conserva el valor existente. `activo=True`
    siempre. En dry-run no se escribe nada.
    """
    nombre = datos["nombre"]
    keywords_json = json.dumps(datos["keywords"], ensure_ascii=False)
    exclude_json = json.dumps(datos["exclude_terms"], ensure_ascii=False)
    existente = (
        db.query(Cliente)
        .filter(func.lower(Cliente.nombre) == nombre.lower())
        .first()
    )

    if existente is None:
        if not dry_run:
            db.add(
                Cliente(
                    nombre=nombre,
                    celula_id=celula_id,
                    entrenamiento="",
                    keywords=keywords_json,
                    localidad=datos["localidad"],
                    telegram_chat_ids=datos["telegram_chat_ids"],
                    exclude_terms=exclude_json,
                    num_principales=datos["num_principales"],
                    activo=True,
                )
            )
            db.flush()
        return {
            "nombre": nombre,
            "accion": "creado",
            "keywords": len(datos["keywords"]),
            "localidad": datos["localidad"],
            "chats": len([c for c in datos["telegram_chat_ids"].split(",") if c.strip()]),
            "telegram_chat_ids": datos["telegram_chat_ids"],
            "exclude_terms": len(datos["exclude_terms"]),
            "num_principales": datos["num_principales"],
        }

    cambios = {}
    if datos["keywords"] and not _mismo_json_lista(existente.keywords, datos["keywords"]):
        cambios["keywords"] = keywords_json
    if datos["localidad"] and datos["localidad"] != (existente.localidad or ""):
        cambios["localidad"] = datos["localidad"]
    if datos["telegram_chat_ids"] and datos["telegram_chat_ids"] != (
        existente.telegram_chat_ids or ""
    ):
        cambios["telegram_chat_ids"] = datos["telegram_chat_ids"]
    if datos["exclude_terms"] and not _mismo_json_lista(
        existente.exclude_terms, datos["exclude_terms"]
    ):
        cambios["exclude_terms"] = exclude_json
    if datos["num_principales"] and int(existente.num_principales or 0) != datos[
        "num_principales"
    ]:
        cambios["num_principales"] = datos["num_principales"]
    if not bool(existente.activo):
        cambios["activo"] = True

    if not cambios:
        return {"nombre": nombre, "accion": "sin_cambios", "cambios": {}}
    if not dry_run:
        for campo, valor in cambios.items():
            setattr(existente, campo, valor)
        db.flush()
    return {"nombre": nombre, "accion": "actualizado", "cambios": cambios}


def importar_clientes(
    clientes: list[dict],
    celula_nombre: str = CELULA_POR_DEFECTO,
    dry_run: bool = True,
    session_factory=None,
) -> dict:
    """UPSERT de `clientes` en la BD. Devuelve el resumen con conteos.

    `session_factory` permite inyectar una sesion (tests); por defecto usa
    `core.database.get_db_session`. Nunca lanza: los errores se reportan en el
    resumen (clave ``error``) y por cliente (accion "error").
    """
    if session_factory is None:
        session_factory = get_db_session
    # accion del detalle -> contador del resumen (creado -> creados, ...)
    contador_por_accion = {
        "creado": "creados",
        "actualizado": "actualizados",
        "sin_cambios": "sin_cambios",
    }
    resumen = {
        "dry_run": bool(dry_run),
        "celula": str(celula_nombre or "").strip() or CELULA_POR_DEFECTO,
        "celula_creada": False,
        "celula_id": None,
        "total": 0,
        "creados": 0,
        "actualizados": 0,
        "sin_cambios": 0,
        "errores": 0,
        "detalle": [],
        "error": "",
    }
    try:
        with session_factory() as db:
            celula, creada = _obtener_celula(db, resumen["celula"], dry_run)
            resumen["celula_creada"] = bool(creada)
            resumen["celula_id"] = getattr(celula, "id", None)
    except Exception as e:  # noqa: BLE001
        resumen["error"] = f"No se pudo preparar la celula: {e}"
        return resumen

    for datos in clientes or []:
        resumen["total"] += 1
        try:
            with session_factory() as db:
                reporte = _aplicar_cliente(
                    db, datos, resumen["celula_id"], dry_run
                )
            accion = reporte.get("accion", "")
            contador = contador_por_accion.get(accion)
            if contador in resumen:
                resumen[contador] += 1
            resumen["detalle"].append(reporte)
        except Exception as e:  # noqa: BLE001
            resumen["errores"] += 1
            resumen["detalle"].append(
                {"nombre": datos.get("nombre", ""), "accion": "error", "detalle": str(e)}
            )
            logger.error(f"No se pudo importar el cliente {datos.get('nombre', '')}: {e}")
    return resumen


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _destino_bd() -> str:
    """Descripcion de la BD destino SIN credenciales (para el reporte)."""
    try:
        from core.database import engine

        return engine.url.render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "desconocida"


def _imprimir_resumen(resumen: dict) -> None:
    modo = "DRY-RUN (no escribe nada)" if resumen.get("dry_run") else "APLICANDO CAMBIOS"
    creada = " (se creara)" if resumen.get("dry_run") and resumen.get("celula_creada") else (
        " (creada)" if resumen.get("celula_creada") else " (existente)"
    )
    print(f"Modo: {modo}")
    print(f"BD destino: {_destino_bd()}")
    print(f"Celula: '{resumen.get('celula', '')}'{creada}")
    if resumen.get("error"):
        print(f"ERROR: {resumen['error']}")
    print(
        f"Total: {resumen.get('total', 0)} | "
        f"creados: {resumen.get('creados', 0)} | "
        f"actualizados: {resumen.get('actualizados', 0)} | "
        f"sin cambios: {resumen.get('sin_cambios', 0)} | "
        f"errores: {resumen.get('errores', 0)}"
    )
    for item in resumen.get("detalle", []):
        accion = item.get("accion", "")
        if accion == "error":
            print(f"  - {item.get('nombre', '')}: ERROR {item.get('detalle', '')}")
        elif accion == "creado":
            print(
                f"  - {item['nombre']}: creado "
                f"({item.get('keywords', 0)} keywords, "
                f"localidad '{item.get('localidad', '')}', "
                f"{item.get('chats', 0)} chat(s), "
                f"{item.get('exclude_terms', 0)} excluidos, "
                f"num_principales={item.get('num_principales', 0)})"
            )
        elif accion == "actualizado":
            print(f"  - {item['nombre']}: actualizado ({', '.join(item.get('cambios', {}))})")
        else:
            print(f"  - {item['nombre']}: sin cambios")
    if resumen.get("dry_run"):
        print("(dry-run: vuelve a correr con --apply para escribir en la BD)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Importa los clientes de alertas del config.json antiguo a la BD "
            "(UPSERT por nombre; dry-run por defecto)."
        )
    )
    parser.add_argument(
        "--desde",
        default=RUTA_POR_DEFECTO,
        help=f"config.json de origen (default: {RUTA_POR_DEFECTO})",
    )
    parser.add_argument(
        "--celula",
        default=CELULA_POR_DEFECTO,
        help=f"nombre de la celula destino (default: {CELULA_POR_DEFECTO})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="escribe en la BD (sin esta bandera solo simula)",
    )
    parser.add_argument(
        "--json", action="store_true", help="imprime el resumen en JSON"
    )
    args = parser.parse_args(argv)

    try:
        clientes = cargar_clientes_desde_config(args.desde)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    if args.apply:
        try:
            from core.database import init_db

            init_db()
        except Exception as e:  # noqa: BLE001
            print(f"ADVERTENCIA: no se pudo inicializar/migrar la BD: {e}")

    resumen = importar_clientes(
        clientes, celula_nombre=args.celula, dry_run=not args.apply
    )
    if args.json:
        print(json.dumps(resumen, ensure_ascii=False, indent=2))
    else:
        _imprimir_resumen(resumen)
    if resumen.get("error"):
        return 2
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
