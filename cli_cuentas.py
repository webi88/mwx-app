# -*- coding: utf-8 -*-
"""CLI de gestion de cuentas (secciones CI/IP/LIB/JUS, tipo de voz, estado
activa/inactiva, propuestas de nombre/@, fotos de perfil/portada,
sincronizacion y perfil de X).

Atajo de linea de comandos para operaciones que ya existen en el dashboard web.
Consume los modulos verificados del proyecto (core.secciones, core.registros,
core.database, core.models, cuentas.generador_identidades, cuentas.fotos,
plataformas.twitter.perfil y plataformas.twitter.selenium_bot)
y no modifica ningun otro archivo.

Subcomandos:
    listar           Inventario de cuentas Twitter en texto plano (solo lectura).
    seccion          Asigna o limpia la seccion CI/IP/LIB/JUS de forma masiva.
    preclasificar    Asigna seccion desde el campo heredado 'sector'.
    tipo             Asigna o limpia el tipo de voz (politica/ciudadana).
    desactivar       Marca las cuentas como inactivas (Cuenta.activa=False).
    activar          Reactiva cuentas previamente desactivadas.
    generar-nombres  Genera propuestas de nombre/@ con IA/local y las guarda.
    aplicar-nombres  Aplica en X las propuestas pendientes (Chrome, en paralelo).
    generar-fotos    Genera avatar/portada con IA (OpenAI; no toca X).
    aplicar-fotos    Aplica en X las fotos ya generadas (Selenium + Chrome).
    sincronizar      Lee el nombre/@ reales desde X por httpx (sin Chrome).
    cambiar-perfil   Cambia el nombre/@ reales de UNA cuenta (Selenium + Chrome).
    renombrar        Renombra la clave interna (Cuenta.usuario) y migra todo.

Ejemplos:
    python cli_cuentas.py listar
    python cli_cuentas.py listar --status imported --seccion sin-asignar
    python cli_cuentas.py listar --tipo politica
    python cli_cuentas.py listar --estado inactiva
    python cli_cuentas.py seccion --seccion LIB --usuarios u1,u2
    python cli_cuentas.py seccion --seccion ninguna --todas --dry-run
    python cli_cuentas.py preclasificar --dry-run
    python cli_cuentas.py tipo --tipo ciudadana --seccion CI
    python cli_cuentas.py tipo --tipo ninguno --usuarios u1,u2 --dry-run
    python cli_cuentas.py desactivar --usuarios c1,c2,c3
    python cli_cuentas.py desactivar --seccion IP --dry-run
    python cli_cuentas.py activar --desde a --hasta m
    python cli_cuentas.py generar-nombres --todas --identidad auto --json propuestas.json
    python cli_cuentas.py generar-nombres --todas --identidad partido --limite 100
    python cli_cuentas.py generar-nombres --seccion CI --identidad mixto --dry-run
    python cli_cuentas.py generar-nombres --status imported --dry-run
    python cli_cuentas.py aplicar-nombres --usuarios u1,u2 --max-workers 2
    python cli_cuentas.py aplicar-nombres --todas --password clave --renombrar
    python cli_cuentas.py aplicar-nombres --seccion CI --limite 20 --dry-run
    python cli_cuentas.py aplicar-nombres --todas --renombrar --json resultado.json
    python cli_cuentas.py generar-fotos --todas --con-portada --limite 20
    python cli_cuentas.py generar-fotos --usuarios u1,u2 --forzar --dry-run
    python cli_cuentas.py aplicar-fotos --seccion CI
    python cli_cuentas.py aplicar-fotos --usuarios u1,u2 --con-portada
    python cli_cuentas.py sincronizar --todas --timeout 20
    python cli_cuentas.py sincronizar --usuarios u1,u2
    python cli_cuentas.py sincronizar --seccion sin-asignar
    python cli_cuentas.py cambiar-perfil --usuario u1 --nombre "Nuevo Nombre"
    python cli_cuentas.py cambiar-perfil --usuario u1 --handle nuevo_handle --password clave
    python cli_cuentas.py renombrar --usuario u1 --nuevo u1_nueva --dry-run
    python cli_cuentas.py renombrar --usuario u1 --actual

Seleccion por rango (seccion, tipo, desactivar, activar, generar-fotos,
aplicar-fotos, generar-nombres, aplicar-nombres y sincronizar):
    python cli_cuentas.py seccion --seccion CI --desde cuenta050 --hasta cuenta120
    python cli_cuentas.py tipo --tipo ciudadana --limite 100
    python cli_cuentas.py generar-nombres --seccion CI --limite 50 --json lote1.json
    python cli_cuentas.py sincronizar --desde a --hasta m --status active

--desde y --hasta son inclusivos y se aplican DESPUES de los filtros
existentes, sobre la lista ordenada por usuario (sin distinguir mayusculas);
--limite recorta el maximo de cuentas tras aplicar el rango. Se pueden usar
sin --usuarios/--todas (ej. --status active --desde a --limite 30).

Codigos de salida:
    0  Exito. En las operaciones masivas (seccion, tipo, desactivar, activar,
       generar-nombres, aplicar-nombres, generar-fotos, sincronizar), las
       cuentas fallidas individuales no cambian el codigo de salida.
    1  Error de uso, error de base de datos, error de escritura del --json,
       cambio de perfil fallido (cambiar-perfil), renombrado fallido
       (renombrar) o ninguna foto aplicada con fallas (aplicar-fotos).
"""
import argparse
import bisect
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

# Salida segura en consolas Windows (cp1252/cp850): evita UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import or_

from core.config import PROJECT_ROOT, resolver_ruta
from core.database import get_db_session, init_db
from core.models import Cuenta
from core.registros import (
    TIPOS_CUENTA,
    etiqueta_tipo_cuenta,
    normalizar_tipo_cuenta,
)
from core.secciones import (
    SECCIONES,
    etiqueta_seccion,
    normalizar_seccion,
    seccion_desde_sector,
)

PLATAFORMA = "twitter"

_TEXTO_SIN_ASIGNAR = (
    "sin-asignar",
    "sin_asignar",
    "sinasignar",
    "sin asignar",
    "ninguna",
    "ninguno",
    "none",
    "",
)

# Variantes aceptadas para limpiar Cuenta.tipo_cuenta.
_TEXTO_SIN_TIPO = _TEXTO_SIN_ASIGNAR + (
    "sin-definir",
    "sin_definir",
    "sindefinir",
    "sin definir",
    "sin-definido",
    "sin-definida",
    "indefinido",
    "indefinida",
)


# ---------------------------------------------------------------------------
# Infraestructura del parser y utilidades de consola
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """ArgumentParser que sale con codigo 1 (no 2) ante errores de uso."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"Error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _error(mensaje):
    """Imprime un error de uso/BD a stderr y termina con codigo 1."""
    print(f"ERROR: {mensaje}", file=sys.stderr)
    raise SystemExit(1)


def _ascii(texto):
    """Normaliza guiones largos para consolas Windows sin soporte unicode."""
    return (texto or "").replace("\u2014", "-").replace("\u2013", "-")


def _imprimir_tabla(encabezados, filas):
    """Imprime una tabla de texto plano con columnas alineadas."""
    anchos = [len(h) for h in encabezados]
    for fila in filas:
        for i, celda in enumerate(fila):
            anchos[i] = max(anchos[i], len(str(celda)))
    print("  ".join(h.ljust(anchos[i]) for i, h in enumerate(encabezados)))
    print("  ".join("-" * ancho for ancho in anchos))
    for fila in filas:
        print("  ".join(str(celda).ljust(anchos[i]) for i, celda in enumerate(fila)))


def _parsear_usuarios(valor):
    """Convierte 'a,b;@c' en ['a', 'b', 'c'] sin duplicados ni vacios."""
    if not valor:
        return []
    vistos = []
    for parte in str(valor).replace(";", ",").split(","):
        usuario = parte.strip().lstrip("@")
        if usuario and usuario not in vistos:
            vistos.append(usuario)
    return vistos


def _entero_positivo(texto):
    """Tipo argparse: entero >= 1."""
    try:
        valor = int(texto)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"valor no entero: {texto!r}")
    if valor < 1:
        raise argparse.ArgumentTypeError("debe ser un entero >= 1")
    return valor


def _resolver_seccion_destino(valor):
    """Destino de asignacion: 'CI'/'IP'/'LIB'/'JUS' o '' para limpiar la seccion."""
    texto = str(valor or "").strip().lower()
    if texto in _TEXTO_SIN_ASIGNAR:
        return ""
    codigo = normalizar_seccion(valor)
    if not codigo:
        validas = ", ".join(SECCIONES)
        _error(f"seccion invalida: {valor!r}. Usa {validas} o ninguna.")
    return codigo


def _resolver_filtro_seccion(valor):
    """Filtro de seccion: None = todas; '' = sin asignar; 'CI'/'IP'/'LIB'/'JUS'."""
    if valor is None:
        return None
    codigo = _resolver_seccion_destino(valor)
    return codigo


def _resolver_tipo_destino(valor):
    """Destino de asignacion: 'politica'/'ciudadana' o '' para limpiar el tipo."""
    texto = str(valor or "").strip().lower()
    if texto in _TEXTO_SIN_TIPO:
        return ""
    codigo = normalizar_tipo_cuenta(valor)
    if not codigo:
        validas = ", ".join(TIPOS_CUENTA)
        _error(f"tipo invalido: {valor!r}. Usa {validas} o ninguno.")
    return codigo


def _resolver_filtro_tipo(valor):
    """Filtro de tipo: None = todas; '' = sin definir; 'politica'/'ciudadana'."""
    if valor is None:
        return None
    codigo = _resolver_tipo_destino(valor)
    return codigo


def _resolver_filtro_estado(valor):
    """Filtro de estado: None = todas; True = activas; False = inactivas."""
    if valor is None:
        return None
    texto = str(valor).strip().lower()
    if texto in ("", "todas", "todos", "all"):
        return None
    if texto in (
        "activa", "activas", "activo", "activos", "active", "true", "1", "si",
    ):
        return True
    if texto in (
        "inactiva", "inactivas", "inactivo", "inactivos", "inactive", "false",
        "0", "no",
    ):
        return False
    _error(f"estado invalido: {valor!r}. Usa activa o inactiva.")


def _configurar_logging():
    """Reduce el ruido de loguru (DEBUG/INFO) y deja solo WARNING+ en stderr."""
    try:
        from loguru import logger

        logger.remove()
        logger.add(sys.stderr, level="WARNING", format="{level}: {message}")
    except Exception:
        pass


def _preparar_bd():
    """Crea tablas/migraciones ligeras si la BD aun no esta lista."""
    try:
        init_db()
        return True
    except Exception as e:
        print(
            f"ERROR: no se pudo preparar la base de datos: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return False


# ---------------------------------------------------------------------------
# Acceso a datos (copias desacopladas de la sesion ORM)
# ---------------------------------------------------------------------------


def _copiar_cuenta(cuenta):
    """Copia ligera de una Cuenta para no retener la sesion ORM abierta."""
    return SimpleNamespace(
        id=cuenta.id,
        usuario=cuenta.usuario or "",
        handle_actual=(getattr(cuenta, "handle_actual", "") or "").strip().lstrip("@"),
        nombre_mostrado=(getattr(cuenta, "nombre_mostrado", "") or "").strip(),
        seccion=normalizar_seccion(getattr(cuenta, "seccion", "")),
        tipo_cuenta=normalizar_tipo_cuenta(getattr(cuenta, "tipo_cuenta", "")),
        nombre_propuesto=(getattr(cuenta, "nombre_propuesto", "") or "").strip(),
        handle_propuesto=(
            (getattr(cuenta, "handle_propuesto", "") or "").strip().lstrip("@")
        ),
        status=(cuenta.status or "").strip(),
        sector=(getattr(cuenta, "sector", "") or "").strip(),
        auth_token=(getattr(cuenta, "auth_token", "") or "").strip(),
        cookies_json=getattr(cuenta, "cookies_json", None),
        proxy=(getattr(cuenta, "proxy", "") or "").strip(),
        password=(getattr(cuenta, "password", "") or ""),
        # None (fila antigua sin migrar) se trata como True, el default del modelo.
        activa=not (getattr(cuenta, "activa", True) is False),
        avatar_path=(getattr(cuenta, "avatar_path", "") or "").strip(),
        banner_path=(getattr(cuenta, "banner_path", "") or "").strip(),
    )


def _texto_estado(cuenta):
    """'activa'/'inactiva' legible para el inventario."""
    return "activa" if getattr(cuenta, "activa", True) else "inactiva"


def _texto_fotos(cuenta):
    """Indicador compacto de fotos: 'av+pt', 'av', 'pt' o '-'."""
    tiene_avatar = bool((getattr(cuenta, "avatar_path", "") or "").strip())
    tiene_portada = bool((getattr(cuenta, "banner_path", "") or "").strip())
    if tiene_avatar and tiene_portada:
        return "av+pt"
    if tiene_avatar:
        return "av"
    if tiene_portada:
        return "pt"
    return "-"


def _texto_propuesta(cuenta):
    """Resumen legible de la propuesta: 'Nombre (@handle)' o '' si no hay."""
    nombre = (getattr(cuenta, "nombre_propuesto", "") or "").strip()
    handle = (getattr(cuenta, "handle_propuesto", "") or "").strip().lstrip("@")
    if not nombre and not handle:
        return ""
    if nombre and handle:
        return f"{nombre} (@{handle})"
    if nombre:
        return nombre
    return f"(@{handle})"


def _cargar_cuentas(usuarios=None, status=None, seccion=None, tipo=None, estado=None):
    """Carga cuentas twitter segun los filtros dados (Nunca deja la sesion viva).

    - usuarios: lista de logins internos; None = sin filtro.
    - status: status exacto; None/'' = sin filtro.
    - seccion: None = sin filtro; '' = sin asignar; 'CI'/'IP'/'LIB'/'JUS'.
    - tipo: None = sin filtro; '' = sin definir; 'politica'/'ciudadana'.
    - estado: None = todas; True = activas; False = inactivas. Este filtro NO
      se usa en desactivar/activar: ahi se cargan activas e inactivas.
    """
    if usuarios is not None and not usuarios:
        return []
    try:
        with get_db_session() as db:
            q = db.query(Cuenta).filter(Cuenta.plataforma == PLATAFORMA)
            if usuarios:
                q = q.filter(Cuenta.usuario.in_(usuarios))
            if status:
                q = q.filter(Cuenta.status == status)
            if seccion is not None:
                if seccion == "":
                    q = q.filter(or_(Cuenta.seccion == "", Cuenta.seccion.is_(None)))
                else:
                    q = q.filter(Cuenta.seccion == seccion)
            cuentas = [_copiar_cuenta(c) for c in q.order_by(Cuenta.usuario).all()]
    except SystemExit:
        raise
    except Exception as e:
        _error(f"no se pudo consultar la base de datos: {type(e).__name__}: {e}")
    # El filtro por tipo se aplica sobre el valor normalizado (tolera filas con
    # acentos/mayusculas guardadas por versiones anteriores).
    if tipo is not None:
        cuentas = [c for c in cuentas if c.tipo_cuenta == tipo]
    # El filtro por estado usa el valor normalizado de _copiar_cuenta (None
    # cuenta como activa, el default del modelo).
    if estado is not None:
        buscado = bool(estado)
        cuentas = [c for c in cuentas if bool(c.activa) == buscado]
    return cuentas


# ---------------------------------------------------------------------------
# Seleccion por rango (--desde/--hasta/--limite)
# ---------------------------------------------------------------------------


def _hay_selector(args, incluir_seccion=False, incluir_tipo=False):
    """True si el usuario indico algun selector de cuentas en la linea de comandos."""
    if getattr(args, "usuarios", None) is not None:
        return True
    if getattr(args, "todas", False):
        return True
    if (getattr(args, "status", None) or "").strip():
        return True
    if incluir_seccion and (getattr(args, "seccion", None) or "").strip():
        return True
    if incluir_tipo and (getattr(args, "tipo", None) or "").strip():
        return True
    for nombre in ("desde", "hasta"):
        if (getattr(args, nombre, None) or "").strip():
            return True
    if getattr(args, "limite", None) is not None:
        return True
    return False


def _exigir_selector(args, incluir_seccion=False, incluir_tipo=False):
    """Error de uso si no se paso ningun selector de cuentas."""
    if _hay_selector(args, incluir_seccion=incluir_seccion, incluir_tipo=incluir_tipo):
        return
    opciones = ["--usuarios", "--todas", "--status"]
    if incluir_seccion:
        opciones.append("--seccion")
    if incluir_tipo:
        opciones.append("--tipo")
    opciones += ["--desde", "--hasta", "--limite"]
    _error("debes indicar un selector de cuentas: " + ", ".join(opciones))


def _descripcion_rango(desde=None, hasta=None, limite=None):
    """Texto legible de --desde/--hasta/--limite ('' si no se paso ninguno)."""
    partes = []
    if str(desde or "").strip():
        partes.append(f"--desde {str(desde).strip()}")
    if str(hasta or "").strip():
        partes.append(f"--hasta {str(hasta).strip()}")
    if limite is not None:
        partes.append(f"--limite {limite}")
    return ", ".join(partes)


def _aplicar_rango(cuentas, desde=None, hasta=None, limite=None):
    """Recorta la lista de cuentas segun --desde/--hasta/--limite.

    Ordena por usuario.lower() (alfabetico, sin distinguir mayusculas) y:
    - desde: incluye desde ese usuario (inclusive); si no existe exacto,
      empieza en el primero >=.
    - hasta: incluye hasta ese usuario (inclusive); si no existe exacto,
      termina en el ultimo <=.
    - limite: maximo de cuentas tras aplicar desde/hasta.
    Devuelve una lista nueva (no modifica la original).
    """
    seleccion = sorted(cuentas, key=lambda c: (c.usuario or "").lower())

    clave_desde = str(desde or "").strip().lstrip("@").lower()
    if clave_desde:
        claves = [(c.usuario or "").lower() for c in seleccion]
        seleccion = seleccion[bisect.bisect_left(claves, clave_desde):]

    clave_hasta = str(hasta or "").strip().lstrip("@").lower()
    if clave_hasta:
        claves = [(c.usuario or "").lower() for c in seleccion]
        seleccion = seleccion[:bisect.bisect_right(claves, clave_hasta)]

    if limite is not None:
        tope = int(limite)
        seleccion = seleccion[:tope] if tope > 0 else []

    return list(seleccion)


def _seleccionar_cuentas(args, usuarios=None, status=None, seccion=None, tipo=None):
    """Carga con los filtros base y luego aplica --desde/--hasta/--limite.

    Imprime un aviso claro y devuelve [] si la seleccion queda vacia.
    """
    cuentas = _cargar_cuentas(
        usuarios=usuarios, status=status, seccion=seccion, tipo=tipo
    )
    if not cuentas:
        print("No se encontraron cuentas twitter con esa seleccion.")
        return []

    rango = _descripcion_rango(
        getattr(args, "desde", None),
        getattr(args, "hasta", None),
        getattr(args, "limite", None),
    )
    cuentas = _aplicar_rango(
        cuentas,
        desde=getattr(args, "desde", None),
        hasta=getattr(args, "hasta", None),
        limite=getattr(args, "limite", None),
    )
    if not cuentas:
        print(f"El rango ({rango}) no selecciono ninguna cuenta.")
    return cuentas


def _agregar_rango(parser):
    """Agrega --desde/--hasta/--limite a un subparser (seccion/tipo/...)."""
    parser.add_argument(
        "--desde", default=None, metavar="USUARIO",
        help=(
            "Incluye desde este usuario (inclusive; orden alfabetico sin "
            "distinguir mayusculas). Si no existe exacto, empieza en el "
            "primero >=. Se aplica despues de los demas filtros"
        ),
    )
    parser.add_argument(
        "--hasta", default=None, metavar="USUARIO",
        help=(
            "Incluye hasta este usuario (inclusive). Si no existe exacto, "
            "termina en el ultimo <=. Se aplica despues de los demas filtros"
        ),
    )
    parser.add_argument(
        "--limite", type=_entero_positivo, default=None, metavar="N",
        help="Maximo N cuentas tras aplicar --desde/--hasta (entero >= 1)",
    )


def _agregar_selectores_masivos(parser):
    """Selectores comunes de los comandos masivos nuevos (desactivar, activar,
    generar-fotos y aplicar-fotos): usuarios/todas/status/seccion/tipo + rango."""
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--usuarios", metavar="a,b,c",
                       help="Lista de logins internos separados por comas")
    grupo.add_argument("--todas", action="store_true",
                       help="Todas las cuentas twitter (activas e inactivas)")
    grupo.add_argument("--status", metavar="STATUS",
                       help="Solo cuentas con ese status exacto")
    grupo.add_argument("--seccion", metavar="CI|IP|LIB|JUS|sin-asignar",
                       help="Solo cuentas de esa seccion; 'sin-asignar' = vacia")
    grupo.add_argument("--tipo", metavar="politica|ciudadana|sin-definir",
                       help="Solo cuentas con ese tipo de voz; 'sin-definir' = vacio")
    _agregar_rango(parser)


# ---------------------------------------------------------------------------
# Subcomando: listar
# ---------------------------------------------------------------------------


def cmd_listar(args):
    """Inventario de cuentas twitter en texto plano (solo lectura)."""
    seccion = _resolver_filtro_seccion(args.seccion)
    tipo = _resolver_filtro_tipo(args.tipo)
    estado = _resolver_filtro_estado(getattr(args, "estado", None))
    status = (args.status or "").strip() or None
    cuentas = _cargar_cuentas(
        status=status, seccion=seccion, tipo=tipo, estado=estado
    )

    if not cuentas:
        print("No se encontraron cuentas twitter con esos filtros.")
        return 0

    filas = []
    for c in cuentas:
        filas.append(
            [
                c.usuario,
                f"@{c.handle_actual}" if c.handle_actual else "-",
                c.nombre_mostrado or "-",
                _ascii(etiqueta_seccion(c.seccion)),
                _ascii(etiqueta_tipo_cuenta(c.tipo_cuenta)),
                _ascii(_texto_propuesta(c)) or "-",
                c.status or "-",
                _texto_estado(c),
                _texto_fotos(c),
            ]
        )
    _imprimir_tabla(
        [
            "usuario", "@ actual", "nombre", "seccion", "tipo", "propuesta",
            "status", "estado", "fotos",
        ],
        filas,
    )
    activas = sum(1 for c in cuentas if c.activa)
    print(
        f"\nTotal: {len(cuentas)} cuentas "
        f"(activas: {activas}, inactivas: {len(cuentas) - activas})."
    )
    print("Fotos: av=avatar, pt=portada, av+pt=ambas, -=sin fotos.")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: seccion
# ---------------------------------------------------------------------------


def cmd_seccion(args):
    """Asigna (o limpia) la seccion CI/IP/LIB/JUS de las cuentas seleccionadas."""
    destino = _resolver_seccion_destino(args.seccion)
    _exigir_selector(args)
    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None

    cuentas = _seleccionar_cuentas(args, usuarios=usuarios, status=status)
    if not cuentas:
        return 0

    cambios = [c for c in cuentas if c.seccion != destino]
    etiqueta_destino = _ascii(etiqueta_seccion(destino))

    if args.dry_run:
        print(
            f"DRY-RUN: {len(cuentas)} cuentas seleccionadas; "
            f"se cambiarian {len(cambios)} a {etiqueta_destino}."
        )
        for c in cambios:
            print(
                f"  @{c.usuario}: {_ascii(etiqueta_seccion(c.seccion))} "
                f"-> {etiqueta_destino}"
            )
        print("DRY-RUN: no se modifico la base de datos.")
        return 0

    if not cambios:
        print(
            f"Sin cambios: las {len(cuentas)} cuentas ya estaban en "
            f"{etiqueta_destino}."
        )
        return 0

    try:
        with get_db_session() as db:
            db.query(Cuenta).filter(
                Cuenta.plataforma == PLATAFORMA,
                Cuenta.usuario.in_([c.usuario for c in cambios]),
            ).update({Cuenta.seccion: destino}, synchronize_session=False)
    except Exception as e:
        _error(f"no se pudo actualizar la seccion: {type(e).__name__}: {e}")

    print(
        f"Se actualizaron {len(cambios)} de {len(cuentas)} cuentas "
        f"a {etiqueta_destino}:"
    )
    for c in cambios:
        handle = f" (@{c.handle_actual})" if c.handle_actual else ""
        print(
            f"  @{c.usuario}{handle}: {_ascii(etiqueta_seccion(c.seccion))} "
            f"-> {etiqueta_destino}"
        )
    return 0


# ---------------------------------------------------------------------------
# Subcomando: tipo
# ---------------------------------------------------------------------------


def cmd_tipo(args):
    """Asigna (o limpia) el tipo de voz politica/ciudadana de las seleccionadas."""
    destino = _resolver_tipo_destino(args.tipo)
    _exigir_selector(args, incluir_seccion=True)
    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None
    seccion = _resolver_filtro_seccion(getattr(args, "seccion", None))

    cuentas = _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion
    )
    if not cuentas:
        return 0

    cambios = [c for c in cuentas if c.tipo_cuenta != destino]
    etiqueta_destino = _ascii(etiqueta_tipo_cuenta(destino))

    if args.dry_run:
        print(
            f"DRY-RUN: {len(cuentas)} cuentas seleccionadas; "
            f"se cambiarian {len(cambios)} a {etiqueta_destino}."
        )
        for c in cambios:
            print(
                f"  @{c.usuario}: {_ascii(etiqueta_tipo_cuenta(c.tipo_cuenta))} "
                f"-> {etiqueta_destino}"
            )
        print("DRY-RUN: no se modifico la base de datos.")
        return 0

    if not cambios:
        print(
            f"Sin cambios: las {len(cuentas)} cuentas ya estaban en "
            f"{etiqueta_destino}."
        )
        return 0

    try:
        with get_db_session() as db:
            db.query(Cuenta).filter(
                Cuenta.plataforma == PLATAFORMA,
                Cuenta.usuario.in_([c.usuario for c in cambios]),
            ).update({Cuenta.tipo_cuenta: destino}, synchronize_session=False)
    except Exception as e:
        _error(f"no se pudo actualizar el tipo de cuenta: {type(e).__name__}: {e}")

    print(
        f"Se actualizaron {len(cambios)} de {len(cuentas)} cuentas "
        f"a {etiqueta_destino}:"
    )
    for c in cambios:
        handle = f" (@{c.handle_actual})" if c.handle_actual else ""
        print(
            f"  @{c.usuario}{handle}: {_ascii(etiqueta_tipo_cuenta(c.tipo_cuenta))} "
            f"-> {etiqueta_destino}"
        )
    return 0


# ---------------------------------------------------------------------------
# Subcomandos: desactivar / activar
# ---------------------------------------------------------------------------


def _cmd_cambiar_activa(args, activar):
    """Motor comun de desactivar/activar (Cuenta.activa) sobre la seleccion."""
    _exigir_selector(args, incluir_seccion=True, incluir_tipo=True)
    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None
    seccion = _resolver_filtro_seccion(getattr(args, "seccion", None))
    tipo = _resolver_filtro_tipo(getattr(args, "tipo", None))

    # OJO: a proposito NO se filtra por Cuenta.activa; --todas debe incluir
    # tanto activas como inactivas (ej. las 25 cuentas dadas a clientes).
    cuentas = _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion, tipo=tipo
    )
    if not cuentas:
        return 0

    objetivo = bool(activar)
    cambios = [c for c in cuentas if bool(c.activa) != objetivo]
    etiqueta = "activa" if objetivo else "inactiva"
    verbo_dry = "activarian" if objetivo else "desactivarian"

    if args.dry_run:
        print(
            f"DRY-RUN: {len(cuentas)} cuentas seleccionadas; "
            f"se {verbo_dry} {len(cambios)}."
        )
        for c in cuentas:
            actual = "activa" if c.activa else "inactiva"
            if bool(c.activa) == objetivo:
                print(f"  @{c.usuario}: {actual} (sin cambio)")
            else:
                print(f"  @{c.usuario}: {actual} -> {etiqueta}")
        print("DRY-RUN: no se modifico la base de datos.")
        return 0

    if not cambios:
        print(
            f"Sin cambios: las {len(cuentas)} cuentas seleccionadas ya estaban "
            f"{etiqueta}s."
        )
        return 0

    try:
        with get_db_session() as db:
            db.query(Cuenta).filter(
                Cuenta.plataforma == PLATAFORMA,
                Cuenta.usuario.in_([c.usuario for c in cambios]),
            ).update({Cuenta.activa: objetivo}, synchronize_session=False)
    except Exception as e:
        _error(f"no se pudo actualizar el estado activa: {type(e).__name__}: {e}")

    accion = "activaron" if objetivo else "desactivaron"
    print(
        f"Se {accion} {len(cambios)} de {len(cuentas)} cuentas "
        f"(ya estaban {etiqueta}s: {len(cuentas) - len(cambios)}):"
    )
    for c in cambios[:10]:
        handle = f" (@{c.handle_actual})" if c.handle_actual else ""
        print(f"  @{c.usuario}{handle}: -> {etiqueta}")
    if len(cambios) > 10:
        print(f"  ... y {len(cambios) - 10} mas.")
    print("")
    print(
        "AVISO: las cuentas inactivas quedan excluidas de publicaciones y campanas."
    )
    return 0


def cmd_desactivar(args):
    """Marca como inactivas (Cuenta.activa=False) las cuentas seleccionadas."""
    return _cmd_cambiar_activa(args, False)


def cmd_activar(args):
    """Reactiva (Cuenta.activa=True) las cuentas seleccionadas."""
    return _cmd_cambiar_activa(args, True)


# ---------------------------------------------------------------------------
# Subcomando: preclasificar
# ---------------------------------------------------------------------------


def cmd_preclasificar(args):
    """Preclasifica CI/IP/LIB/JUS las cuentas sin seccion segun su 'sector'."""
    cuentas = _cargar_cuentas()
    sin_seccion = [c for c in cuentas if not c.seccion]
    cambios = []
    for c in sin_seccion:
        codigo = seccion_desde_sector(c.sector)
        if codigo:
            cambios.append((c, codigo))

    print(
        f"Cuentas twitter: {len(cuentas)} "
        f"(con seccion: {len(cuentas) - len(sin_seccion)}, "
        f"sin asignar: {len(sin_seccion)})."
    )

    if args.dry_run:
        print(
            f"DRY-RUN: se preclasificarian {len(cambios)} cuentas "
            f"desde 'sector'."
        )
        for c, codigo in cambios:
            print(
                f"  @{c.usuario} (sector='{c.sector}') "
                f"-> {_ascii(etiqueta_seccion(codigo))}"
            )
        print("DRY-RUN: no se modifico la base de datos.")
        return 0

    if cambios:
        try:
            with get_db_session() as db:
                for c, codigo in cambios:
                    db.query(Cuenta).filter(
                        Cuenta.plataforma == PLATAFORMA,
                        Cuenta.usuario == c.usuario,
                    ).update({Cuenta.seccion: codigo}, synchronize_session=False)
        except Exception as e:
            _error(f"no se pudo preclasificar: {type(e).__name__}: {e}")
        for c, codigo in cambios:
            print(
                f"  @{c.usuario} (sector='{c.sector}') "
                f"-> {_ascii(etiqueta_seccion(codigo))}"
            )

    sin_sector = sum(1 for c in sin_seccion if not c.sector)
    desconocido = len(sin_seccion) - len(cambios) - sin_sector
    print(
        f"Preclasificadas: {len(cambios)}; sin sector: {sin_sector}; "
        f"sector no reconocido: {desconocido}."
    )
    return 0


# ---------------------------------------------------------------------------
# Subcomando: generar-nombres
# ---------------------------------------------------------------------------


def cmd_generar_nombres(args):
    """Genera propuestas de nombre/@ (IA en lotes o local) y las guarda.

    ``--identidad`` acepta auto/persona/movimiento/partido/mixto: "partido" son
    similitudes con partidos SIN nombrarlos (colores/simbolos: "Movimiento
    Naranja", "Los Bolillos", "Amarillo de Luz"...) y "mixto" reparte ~mitad
    persona / mitad partido. Con --dry-run no escribe en la BD.
    """
    try:
        from cuentas.generador_identidades import asignar_propuestas
    except Exception as e:
        _error(f"no se pudo importar el generador de identidades: {e}")

    _exigir_selector(args, incluir_seccion=True)
    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None
    seccion = _resolver_filtro_seccion(getattr(args, "seccion", None))

    cuentas = _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion
    )
    if not cuentas:
        return 0

    identidad = (args.identidad or "auto").strip() or "auto"
    # Si se filtro por seccion, se usa como contexto de generacion; si no,
    # asignar_propuestas() toma la seccion de cada cuenta.
    contexto_seccion = seccion or ""

    if args.dry_run:
        print(
            f"DRY-RUN: se generarian propuestas para {len(cuentas)} cuentas "
            "SIN escribir en la base de datos."
        )
    else:
        print(
            "AVISO: se guardaran las propuestas "
            "(nombre_propuesto/handle_propuesto) en la base de datos."
        )
    print(
        "AVISO: este comando NO cambia nada en X; para aplicarlas usa: "
        "python cli_cuentas.py aplicar-nombres ..."
    )

    resultado = asignar_propuestas(
        [c.usuario for c in cuentas],
        tipo=identidad,
        seccion=contexto_seccion,
        dry_run=args.dry_run,
    )
    propuestas = resultado.get("propuestas") or []
    errores = resultado.get("errores") or []
    origen_ia = bool(resultado.get("origen_ia"))

    if propuestas:
        filas = []
        for p in propuestas:
            handle = (p.get("handle") or "").strip()
            filas.append(
                [
                    p.get("usuario", ""),
                    p.get("nombre", ""),
                    f"@{handle}" if handle else "-",
                    p.get("tipo", ""),
                ]
            )
        _imprimir_tabla(
            ["usuario", "nombre propuesto", "@ propuesto", "identidad"], filas
        )
    else:
        print("No se genero ninguna propuesta.")

    for error in errores:
        print(f"ERROR: {error}", file=sys.stderr)

    print("")
    print(
        f"Resumen: total={resultado.get('total', len(cuentas))} "
        f"ok={resultado.get('ok', len(propuestas))} "
        f"persona={resultado.get('persona', 0)} "
        f"partido={resultado.get('partido', 0)} "
        f"movimiento={resultado.get('movimiento', 0)} "
        f"ia={'si' if origen_ia else 'no'} "
        f"errores={len(errores)}."
    )
    if not args.dry_run and propuestas:
        print(
            "Siguiente paso: python cli_cuentas.py aplicar-nombres "
            "--usuarios <usuarios> (abre Chrome y cambia X de verdad)."
        )

    if args.json:
        ruta = args.json
        if not os.path.isabs(ruta):
            ruta = resolver_ruta(ruta)
        payload = {
            "generado": datetime.now().isoformat(timespec="seconds"),
            "dry_run": bool(resultado.get("dry_run")),
            "identidad": identidad,
            "seccion": contexto_seccion,
            "total": resultado.get("total", len(cuentas)),
            "ok": resultado.get("ok", len(propuestas)),
            "origen_ia": origen_ia,
            "persona": resultado.get("persona", 0),
            "partido": resultado.get("partido", 0),
            "movimiento": resultado.get("movimiento", 0),
            "errores": errores,
            "propuestas": propuestas,
        }
        try:
            carpeta = os.path.dirname(ruta)
            if carpeta:
                os.makedirs(carpeta, exist_ok=True)
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as e:
            _error(f"no se pudo escribir el JSON {ruta!r}: {e}")
        print(f"JSON exportado: {ruta}")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: aplicar-nombres
# ---------------------------------------------------------------------------


def cmd_aplicar_nombres(args):
    """Aplica en X las propuestas pendientes (en paralelo, Chrome).

    Requiere Chrome. AVISO: ejecuta cambios REALES en X; el cambio de @ puede
    pedir la contrasena de la cuenta (si no se pasa --password se usa la BD).
    ``--max-workers`` controla cuantas cuentas se aplican a la vez (1-4; con
    SQLite conviene 2-3) y ``--renombrar`` migra la clave interna al @ real
    despues de cada cambio exitoso.
    """
    try:
        from cuentas.generador_identidades import aplicar_propuestas_en_lote
    except Exception as e:
        _error(f"no se pudo importar el aplicador de propuestas: {e}")

    _exigir_selector(args, incluir_seccion=True, incluir_tipo=True)
    usuarios = None
    if getattr(args, "usuarios", None) is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (getattr(args, "status", None) or "").strip() or None
    seccion = _resolver_filtro_seccion(getattr(args, "seccion", None))
    tipo = _resolver_filtro_tipo(getattr(args, "tipo", None))

    cuentas = _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion, tipo=tipo
    )
    if not cuentas:
        return 0

    con_propuesta = [c for c in cuentas if c.nombre_propuesto or c.handle_propuesto]
    sin_propuesta = len(cuentas) - len(con_propuesta)

    if getattr(args, "dry_run", False):
        print(
            f"DRY-RUN: se aplicarian propuestas en {len(con_propuesta)} de "
            f"{len(cuentas)} cuentas SIN abrir Chrome ni tocar X."
        )
        for c in con_propuesta:
            print(f"  @{c.usuario}: {_ascii(_texto_propuesta(c))}")
        if sin_propuesta:
            print(f"  ({sin_propuesta} cuentas sin propuesta pendiente)")
        print("DRY-RUN: no se abrio Chrome ni se modifico nada.")
        return 0

    print("AVISO: este comando abre Chrome (Selenium) y aplica cambios REALES en X.")
    print(
        "AVISO: el cambio de @ puede pedir la contrasena; si no pasas --password "
        "se usa la guardada en la base de datos."
    )

    if not con_propuesta:
        print(
            f"Sin propuestas pendientes: {sin_propuesta} cuentas seleccionadas "
            "no tienen nombre/handle propuesto."
        )
        return 0

    total = len(con_propuesta)
    max_workers = int(getattr(args, "max_workers", 2) or 2)
    renombrar = bool(getattr(args, "renombrar", False))
    print(
        f"Aplicando {total} propuestas con hasta {max_workers} cuenta(s) en "
        f"paralelo"
        + (" (y renombrando la clave interna al @ real)." if renombrar else ".")
    )

    def _progreso(progreso):
        """Callback del lote (hilo recolector): imprime una linea por cuenta."""
        hechas = int(progreso.get("hechas", 0) or 0)
        usuario = progreso.get("usuario", "")
        if progreso.get("ok"):
            print(f"[{hechas:>3}/{total}] @{usuario} -> OK", flush=True)
        else:
            error = (progreso.get("error") or "").strip() or "(sin detalle)"
            print(
                f"[{hechas:>3}/{total}] @{usuario} -> ERROR: {error}",
                file=sys.stderr,
                flush=True,
            )

    resultado = aplicar_propuestas_en_lote(
        [c.usuario for c in con_propuesta],
        max_workers=max_workers,
        password=(args.password or ""),
        renombrar=renombrar,
        callback=_progreso,
    )

    if renombrar:
        for entrada in resultado.get("resultados") or []:
            if entrada.get("renombrado"):
                print(
                    f"  @{entrada.get('usuario', '')}: clave interna renombrada "
                    "al @ real."
                )
            elif entrada.get("error_renombrado"):
                print(
                    f"AVISO @{entrada.get('usuario', '')}: no se pudo renombrar "
                    f"la clave interna: {entrada.get('error_renombrado')}",
                    file=sys.stderr,
                )

    for error in resultado.get("errores") or []:
        print(f"ERROR: {error}", file=sys.stderr)

    print("")
    print(
        f"Resumen: aplicadas={resultado.get('ok', 0)} "
        f"fallidas={resultado.get('fallidos', 0)} "
        f"renombrados={resultado.get('renombrados', 0)} "
        f"sin_propuesta={sin_propuesta}."
    )
    if resultado.get("cancelado"):
        print("AVISO: lote cancelado; quedaron cuentas sin procesar.")

    if getattr(args, "json", None):
        ruta = args.json
        if not os.path.isabs(ruta):
            ruta = resolver_ruta(ruta)
        payload = {
            "generado": datetime.now().isoformat(timespec="seconds"),
            "total": resultado.get("total", total),
            "ok": resultado.get("ok", 0),
            "fallidos": resultado.get("fallidos", 0),
            "renombrados": resultado.get("renombrados", 0),
            "cancelado": bool(resultado.get("cancelado")),
            "resultados": resultado.get("resultados") or [],
            "errores": resultado.get("errores") or [],
        }
        try:
            carpeta = os.path.dirname(ruta)
            if carpeta:
                os.makedirs(carpeta, exist_ok=True)
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
        except OSError as e:
            _error(f"no se pudo escribir el JSON {ruta!r}: {e}")
        print(f"JSON exportado: {ruta}")
    return 0


# ---------------------------------------------------------------------------
# Subcomandos: generar-fotos / aplicar-fotos
# ---------------------------------------------------------------------------


def _callback_fotos(actual, total, usuario, tipo, ok):
    """Imprime el progreso de cada foto (callback de cuentas.fotos)."""
    estado = "OK" if ok else "ERROR"
    print(f"[{actual:>3}/{total}] @{usuario} ({tipo}): {estado}", flush=True)


def _seleccion_fotos(args):
    """Valida selectores y devuelve las cuentas para los comandos de fotos."""
    _exigir_selector(args, incluir_seccion=True, incluir_tipo=True)
    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None
    seccion = _resolver_filtro_seccion(getattr(args, "seccion", None))
    tipo = _resolver_filtro_tipo(getattr(args, "tipo", None))
    return _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion, tipo=tipo
    )


def cmd_generar_fotos(args):
    """Genera avatar (y portada con --con-portada) con IA; no toca X."""
    try:
        from cuentas.fotos import generar_fotos
    except Exception as e:
        _error(f"no se pudo importar cuentas.fotos: {e}")

    cuentas = _seleccion_fotos(args)
    if not cuentas:
        return 0

    print(
        "AVISO: la generacion usa IA (OpenAI) y tarda ~1 imagen por cuenta; "
        "sin --con-portada solo se genera el avatar."
    )
    print(
        "AVISO: las imagenes se guardan en data/avatares/ (perfil) y "
        "data/portadas/ (portada); el comando NO cambia nada en X."
    )
    if args.dry_run:
        print(
            "DRY-RUN: no se genera ninguna imagen ni se escribe en la base de datos."
        )
    elif not args.forzar:
        print(
            "AVISO: las cuentas que ya tienen esa foto se omiten; usa --forzar "
            "para regenerarlas."
        )
    print(
        f"Procesando {len(cuentas)} cuentas"
        + (" (avatar + portada)..." if args.con_portada else " (avatar)...")
    )

    resultado = generar_fotos(
        [c.usuario for c in cuentas],
        con_portada=bool(args.con_portada),
        forzar=bool(args.forzar),
        dry_run=bool(args.dry_run),
        callback=_callback_fotos,
    )

    if args.dry_run:
        for entrada in resultado.get("detalle") or []:
            accion = entrada.get("accion") or "?"
            ruta = entrada.get("ruta") or ""
            print(
                f"  @{entrada.get('usuario', '')} ({entrada.get('tipo', '')}): "
                f"{accion} {ruta}".rstrip()
            )

    errores = resultado.get("errores") or []
    for error in errores:
        print(f"ERROR: {error}", file=sys.stderr)

    print("")
    print(
        f"Resumen: total={resultado.get('total', len(cuentas))} "
        f"avatares_ok={resultado.get('avatares_ok', 0)} "
        f"portadas_ok={resultado.get('portadas_ok', 0)} "
        f"omitidas={resultado.get('omitidas', 0)} "
        f"fallidas={resultado.get('fallidas', 0)} "
        f"errores={len(errores)}."
    )
    generadas = int(resultado.get("avatares_ok", 0)) + int(
        resultado.get("portadas_ok", 0)
    )
    if generadas and not args.dry_run:
        ejemplo = next((c.usuario for c in cuentas), "<usuarios>")
        aviso_portada = " --con-portada" if args.con_portada else ""
        print(
            "Siguiente paso: python cli_cuentas.py aplicar-fotos "
            f"--usuarios {ejemplo}{aviso_portada} (abre Chrome y sube las fotos)."
        )
    return 0


def cmd_aplicar_fotos(args):
    """Aplica en X las fotos ya generadas (Selenium + Chrome)."""
    try:
        from cuentas.fotos import aplicar_fotos
    except Exception as e:
        _error(f"no se pudo importar cuentas.fotos: {e}")

    cuentas = _seleccion_fotos(args)
    if not cuentas:
        return 0

    print(
        "AVISO: este comando abre Chrome (Selenium) y aplica cambios REALES en X "
        "(foto de perfil" + (" y portada" if args.con_portada else "") + ")."
    )
    print(
        "AVISO: cada cuenta usa sus cookies/proxy guardados; si no tiene "
        "avatar_path/banner_path en la BD se reporta como fallida."
    )
    print(f"Aplicando fotos en {len(cuentas)} cuentas...")

    resultado = aplicar_fotos(
        [c.usuario for c in cuentas],
        con_portada=bool(args.con_portada),
        callback=_callback_fotos,
    )

    for entrada in resultado.get("detalle") or []:
        if entrada.get("ok"):
            continue
        error = (entrada.get("error") or "").strip() or "(sin detalle)"
        print(
            f"ERROR @{entrada.get('usuario', '')} "
            f"({entrada.get('tipo', 'foto')}): {error}",
            file=sys.stderr,
        )

    avatares_ok = int(resultado.get("avatares_ok", 0))
    portadas_ok = int(resultado.get("portadas_ok", 0))
    fallidas = int(resultado.get("fallidas", 0))
    print("")
    print(
        f"Resumen: total={resultado.get('total', len(cuentas))} "
        f"avatares_ok={avatares_ok} portadas_ok={portadas_ok} "
        f"fallidas={fallidas}."
    )
    if (avatares_ok + portadas_ok) == 0 and fallidas > 0:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcomando: sincronizar
# ---------------------------------------------------------------------------


def cmd_sincronizar(args):
    """Lee nombre/@ reales desde X por httpx (sin Chrome) y los persiste."""
    try:
        from plataformas.twitter.perfil import sincronizar_cuenta
    except Exception as e:
        _error(f"no se pudo importar el sincronizador de perfiles: {e}")

    usuarios = None
    if args.usuarios is not None:
        usuarios = _parsear_usuarios(args.usuarios)
        if not usuarios:
            _error("--usuarios no contiene ningun usuario valido")
    status = (args.status or "").strip() or None
    seccion = _resolver_filtro_seccion(args.seccion)

    cuentas = _seleccionar_cuentas(
        args, usuarios=usuarios, status=status, seccion=seccion
    )
    if not cuentas:
        return 0

    total = len(cuentas)
    print(f"Sincronizando {total} cuentas contra X (httpx, sin Chrome)...")
    resumen = {
        "total": total,
        "ok": 0,
        "cambios_@": 0,
        "cambios_nombre": 0,
        "errores": 0,
        "sin_datos": 0,
    }

    for idx, c in enumerate(cuentas, start=1):
        antes_handle = c.handle_actual
        antes_nombre = c.nombre_mostrado
        resultado = sincronizar_cuenta(c, timeout=args.timeout)
        error = (resultado.get("error") or "").strip()
        handle = (resultado.get("handle") or "").strip().lstrip("@")
        nombre = (resultado.get("nombre") or "").strip()

        if error == "sin_datos":
            resumen["sin_datos"] += 1
            print(
                f"[{idx:>3}/{total}] @{c.usuario} -> SIN DATOS "
                f"(no se pudo leer el perfil)",
                flush=True,
            )
            continue
        if error:
            resumen["errores"] += 1
            print(
                f"[{idx:>3}/{total}] @{c.usuario} -> ERROR: {error}",
                flush=True,
            )
            continue

        resumen["ok"] += 1
        linea = (
            f"[{idx:>3}/{total}] @{c.usuario} -> @{handle or '-'} "
            f"({nombre or 'sin nombre'})"
        )
        marcas = []
        if resultado.get("cambio_handle"):
            resumen["cambios_@"] += 1
            marcas.append(f"CAMBIO @: {antes_handle or '(vacio)'} -> {handle}")
        if resultado.get("cambio_nombre"):
            resumen["cambios_nombre"] += 1
            marcas.append(
                f"CAMBIO nombre: {antes_nombre or '(vacio)'} -> {nombre}"
            )
        if marcas:
            linea += "  " + " | ".join(marcas)
        print(linea, flush=True)

    print("")
    print("Resumen:")
    print(
        f"  total={resumen['total']} ok={resumen['ok']} "
        f"cambios_@={resumen['cambios_@']} "
        f"cambios_nombre={resumen['cambios_nombre']} "
        f"errores={resumen['errores']} sin_datos={resumen['sin_datos']}"
    )
    return 0


# ---------------------------------------------------------------------------
# Subcomando: cambiar-perfil
# ---------------------------------------------------------------------------


def cmd_cambiar_perfil(args):
    """Cambia nombre/@ de UNA cuenta real (Selenium + Chrome)."""
    usuario = (args.usuario or "").strip().lstrip("@")
    nombre = (args.nombre or "").strip() or None
    handle = (args.handle or "").strip().lstrip("@") or None

    if not usuario:
        _error("--usuario es obligatorio")
    if not nombre and not handle:
        _error("debes indicar --nombre y/o --handle")

    if not _cargar_cuentas(usuarios=[usuario]):
        _error(
            f"la cuenta @{usuario} no existe en la base de datos "
            f"(plataforma {PLATAFORMA})"
        )

    print("AVISO: este comando abre Chrome (Selenium) y aplica un cambio REAL")
    print("       en la cuenta de X. Verifica la cuenta antes de continuar.")
    detalle = f"Cuenta: @{usuario}"
    if nombre:
        detalle += f"  |  nuevo nombre: {nombre!r}"
    if handle:
        detalle += f"  |  nuevo @: {handle}"
    print(detalle)

    bot = None
    try:
        from plataformas.twitter.selenium_bot import TwitterBot

        bot = TwitterBot(usuario)
        resultado = bot.cambiar_perfil(
            nombre=nombre, handle=handle, password=args.password
        )
        print(f"Resultado: {resultado}")
        if not resultado.get("ok"):
            print(
                f"ultimo_error: {bot.ultimo_error or '(sin detalle)'}",
                file=sys.stderr,
            )
            return 1
        return 0
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if bot is not None:
            try:
                bot.cerrar()
            except Exception as e:
                print(f"AVISO: no se pudo cerrar el navegador: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcomando: renombrar
# ---------------------------------------------------------------------------


def _ruta_corta(ruta):
    """Ruta relativa al proyecto para que la tabla sea legible ('' -> '-')."""
    texto = str(ruta or "").strip()
    if not texto:
        return "-"
    try:
        raiz = os.path.abspath(str(PROJECT_ROOT))
        absoluta = os.path.abspath(texto)
        if os.path.normcase(absoluta).startswith(os.path.normcase(raiz + os.sep)):
            return os.path.relpath(absoluta, raiz).replace("\\", "/")
    except Exception:
        pass
    return texto


def cmd_renombrar(args):
    """Renombra la clave interna (Cuenta.usuario) migrando archivos/rutas."""
    try:
        from core.renombrar import renombrar_al_handle_actual, renombrar_usuario
    except Exception as e:
        _error(f"no se pudo importar core.renombrar: {e}")

    usuario = (args.usuario or "").strip().lstrip("@")
    if not usuario:
        _error("--usuario es obligatorio")

    if args.actual:
        resultado = renombrar_al_handle_actual(usuario, dry_run=bool(args.dry_run))
    else:
        resultado = renombrar_usuario(
            usuario, args.nuevo, dry_run=bool(args.dry_run)
        )

    for advertencia in resultado.get("advertencias") or []:
        print(f"AVISO: {advertencia}", file=sys.stderr)

    anterior = resultado.get("usuario_anterior") or usuario
    nuevo = resultado.get("usuario_nuevo") or ""
    modo = "DRY-RUN" if resultado.get("dry_run") else "REAL"
    print(f"Renombrado de clave interna ({modo}): {anterior} -> {nuevo or '-'}")

    archivos = resultado.get("archivos") or []
    if archivos:
        filas = []
        for entrada in archivos:
            filas.append(
                [
                    entrada.get("tipo", "?"),
                    _ruta_corta(entrada.get("de")),
                    _ruta_corta(entrada.get("a")),
                    "OK" if entrada.get("ok") else "ERROR",
                    entrada.get("error", ""),
                ]
            )
        _imprimir_tabla(["tipo", "de", "a", "estado", "error"], filas)
    else:
        print("Archivos a migrar: ninguno (no existen cookies/perfil/avatar/portada).")

    referencias = resultado.get("referencias") or {}
    print("")
    print(
        "Referencias: "
        f"registros={referencias.get('registros', 0)} "
        f"cookies_path={'si' if referencias.get('cookies_path') else 'no'} "
        f"rutas_imagenes={'si' if referencias.get('rutas_imagenes') else 'no'}"
    )

    if not resultado.get("ok"):
        error = (resultado.get("error") or "").strip() or "(sin detalle)"
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if resultado.get("dry_run"):
        print("DRY-RUN: no se modifico la base de datos ni el disco.")
        return 0

    if resultado.get("renombrado"):
        print(f"OK: la cuenta se renombro a '{nuevo}' en la base de datos.")
    fallos = [entrada for entrada in archivos if not entrada.get("ok")]
    if fallos:
        print(
            f"AVISO: {len(fallos)} archivo(s) no se pudieron migrar; "
            "revisa los errores de la tabla.",
            file=sys.stderr,
        )
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def construir_parser():
    """Construye el parser argparse con todos los subcomandos del CLI."""
    parser = _Parser(
        prog="cli_cuentas.py",
        description=(
            "Gestion masiva de cuentas Twitter/X: secciones CI/IP/LIB/JUS, tipo de "
            "voz, estado activa/inactiva, propuestas de nombre/@, fotos de "
            "perfil/portada, preclasificacion por sector, sincronizacion de "
            "perfil (httpx), cambio real de nombre/@ (Selenium + Chrome) y "
            "renombrado de la clave interna (Cuenta.usuario)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  python cli_cuentas.py listar --seccion sin-asignar\n"
            "  python cli_cuentas.py listar --tipo politica\n"
            "  python cli_cuentas.py listar --estado inactiva\n"
            "  python cli_cuentas.py seccion --seccion LIB --usuarios u1,u2\n"
            "  python cli_cuentas.py seccion --seccion ninguna --todas --dry-run\n"
            "  python cli_cuentas.py seccion --seccion CI --desde cuenta050 --hasta cuenta120\n"
            "  python cli_cuentas.py preclasificar --dry-run\n"
            "  python cli_cuentas.py tipo --tipo ciudadana --seccion CI\n"
            "  python cli_cuentas.py tipo --tipo ninguno --usuarios u1,u2 --dry-run\n"
            "  python cli_cuentas.py tipo --tipo ciudadana --limite 100\n"
            "  python cli_cuentas.py desactivar --usuarios c1,c2,c3\n"
            "  python cli_cuentas.py desactivar --seccion IP --dry-run\n"
            "  python cli_cuentas.py activar --desde a --hasta m\n"
            "  python cli_cuentas.py generar-nombres --todas --json propuestas.json\n"
            "  python cli_cuentas.py generar-nombres --todas --identidad partido\n"
            "  python cli_cuentas.py generar-nombres --seccion CI --identidad mixto --limite 100\n"
            "  python cli_cuentas.py generar-nombres --status imported --dry-run\n"
            "  python cli_cuentas.py generar-nombres --seccion CI --limite 50 --json lote1.json\n"
            "  python cli_cuentas.py aplicar-nombres --usuarios u1,u2 --max-workers 2\n"
            "  python cli_cuentas.py aplicar-nombres --todas --password clave --renombrar\n"
            "  python cli_cuentas.py aplicar-nombres --seccion CI --limite 20 --dry-run\n"
            "  python cli_cuentas.py aplicar-nombres --todas --renombrar --json resultado.json\n"
            "  python cli_cuentas.py generar-fotos --todas --con-portada --limite 20\n"
            "  python cli_cuentas.py generar-fotos --usuarios u1,u2 --forzar --dry-run\n"
            "  python cli_cuentas.py aplicar-fotos --seccion CI\n"
            "  python cli_cuentas.py aplicar-fotos --usuarios u1,u2 --con-portada\n"
            "  python cli_cuentas.py sincronizar --todas --timeout 20\n"
            "  python cli_cuentas.py sincronizar --desde a --hasta m --status active\n"
            "  python cli_cuentas.py cambiar-perfil --usuario u1 --nombre \"Nuevo Nombre\"\n"
            "  python cli_cuentas.py renombrar --usuario u1 --nuevo u1_nueva --dry-run\n"
            "  python cli_cuentas.py renombrar --usuario u1 --actual\n"
            "\n"
            "Seleccion por rango (seccion, tipo, desactivar, activar,\n"
            "generar-fotos, aplicar-fotos, generar-nombres, sincronizar):\n"
            "  --desde USUARIO  incluye desde ese usuario (inclusive)\n"
            "  --hasta USUARIO  incluye hasta ese usuario (inclusive)\n"
            "  --limite N       maximo N cuentas tras aplicar desde/hasta\n"
            "Se aplican despues de los filtros, en orden alfabetico por usuario\n"
            "(sin distinguir mayusculas), y no requieren --usuarios/--todas.\n"
        ),
    )
    sub = parser.add_subparsers(
        dest="comando",
        metavar=(
            "{listar,seccion,preclasificar,tipo,desactivar,activar,"
            "generar-nombres,aplicar-nombres,generar-fotos,aplicar-fotos,"
            "sincronizar,cambiar-perfil,renombrar}"
        ),
    )

    # --- listar ------------------------------------------------------------
    p = sub.add_parser(
        "listar",
        help="Inventario de cuentas twitter (solo lectura)",
        description=(
            "Muestra usuario, @ actual, nombre, seccion, tipo, propuesta y status "
            "en texto plano. No modifica la base de datos."
        ),
    )
    p.add_argument("--status", default=None, metavar="STATUS",
                   help="Filtra por status exacto (ej. imported, active)")
    p.add_argument("--seccion", default=None, metavar="CI|IP|LIB|JUS|sin-asignar",
                   help="Filtra por seccion; 'sin-asignar' = seccion vacia")
    p.add_argument("--tipo", default=None, metavar="politica|ciudadana|sin-definir",
                   help="Filtra por tipo de voz; 'sin-definir' = tipo vacio")
    p.add_argument("--estado", default=None, metavar="activa|inactiva",
                   help="Filtra por estado activa/inactiva (default: todas)")
    p.set_defaults(func=cmd_listar)

    # --- seccion -----------------------------------------------------------
    p = sub.add_parser(
        "seccion",
        help="Asigna o limpia la seccion CI/IP/LIB/JUS de forma masiva",
        description=(
            "Asigna la seccion indicada a las cuentas seleccionadas. "
            "'--seccion ninguna' limpia la seccion (cadena vacia). "
            "La seleccion acepta --usuarios/--todas/--status y, ademas, "
            "--desde/--hasta/--limite."
        ),
    )
    p.add_argument("--seccion", required=True, metavar="CI|IP|LIB|JUS|ninguna",
                   help="Seccion destino; 'ninguna' limpia la asignacion")
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument("--usuarios", metavar="a,b,c",
                       help="Lista de logins internos separados por comas")
    grupo.add_argument("--todas", action="store_true",
                       help="Todas las cuentas twitter")
    grupo.add_argument("--status", metavar="STATUS",
                       help="Solo cuentas con ese status exacto")
    _agregar_rango(p)
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que haria sin modificar la base de datos")
    p.set_defaults(func=cmd_seccion)

    # --- preclasificar -----------------------------------------------------
    p = sub.add_parser(
        "preclasificar",
        help="Preclasifica CI/IP/LIB/JUS desde el campo heredado 'sector'",
        description=(
            "Para cuentas twitter sin seccion, aplica seccion_desde_sector(sector): "
            "privados->IP, centroizquierda->CI, libertad->LIB, justicia->JUS "
            "(centroderecha queda sin asignar)."
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que haria sin modificar la base de datos")
    p.set_defaults(func=cmd_preclasificar)

    # --- tipo --------------------------------------------------------------
    p = sub.add_parser(
        "tipo",
        help="Asigna o limpia el tipo de voz politica/ciudadana de forma masiva",
        description=(
            "Asigna el tipo de voz indicado a las cuentas seleccionadas. "
            "'--tipo ninguno' limpia el tipo (cadena vacia). La seleccion "
            "funciona como en 'seccion' (usuarios/todas/status/seccion) y "
            "ademas acepta --desde/--hasta/--limite."
        ),
    )
    p.add_argument("--tipo", required=True, metavar="politica|ciudadana|ninguno",
                   help="Tipo destino; 'ninguno' limpia la asignacion")
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument("--usuarios", metavar="a,b,c",
                       help="Lista de logins internos separados por comas")
    grupo.add_argument("--todas", action="store_true",
                       help="Todas las cuentas twitter")
    grupo.add_argument("--status", metavar="STATUS",
                       help="Solo cuentas con ese status exacto")
    grupo.add_argument("--seccion", metavar="CI|IP|LIB|JUS|sin-asignar",
                       help="Solo cuentas de esa seccion; 'sin-asignar' = vacia")
    _agregar_rango(p)
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que haria sin modificar la base de datos")
    p.set_defaults(func=cmd_tipo)

    # --- desactivar / activar ---------------------------------------------
    p = sub.add_parser(
        "desactivar",
        help="Desactiva cuentas (Cuenta.activa=False) sin borrarlas",
        description=(
            "Marca como inactivas las cuentas seleccionadas. Con --todas se "
            "incluyen tanto activas como inactivas. Las cuentas inactivas "
            "quedan excluidas de publicaciones y campanas, pero conservan sus "
            "cookies, proxies y tareas. La seleccion acepta "
            "usuarios/todas/status/seccion/tipo y --desde/--hasta/--limite."
        ),
    )
    _agregar_selectores_masivos(p)
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que cuentas desactivaria sin tocar la BD")
    p.set_defaults(func=cmd_desactivar)

    p = sub.add_parser(
        "activar",
        help="Reactiva cuentas (Cuenta.activa=True) previamente desactivadas",
        description=(
            "Vuelve a marcar como activas las cuentas seleccionadas. Con "
            "--todas se incluyen tanto activas como inactivas. La seleccion "
            "acepta usuarios/todas/status/seccion/tipo y "
            "--desde/--hasta/--limite."
        ),
    )
    _agregar_selectores_masivos(p)
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que cuentas activaria sin tocar la BD")
    p.set_defaults(func=cmd_activar)

    # --- generar-nombres ---------------------------------------------------
    p = sub.add_parser(
        "generar-nombres",
        help="Genera y guarda propuestas de nombre/@ (no toca X)",
        description=(
            "Genera identidades para las cuentas seleccionadas y guarda "
            "nombre_propuesto/handle_propuesto en la base de datos (salvo "
            "--dry-run). NO cambia nada en X: para aplicar usa 'aplicar-nombres'. "
            "Con --identidad auto se respeta Cuenta.tipo_cuenta "
            "(politica->movimiento, ciudadana->persona); 'partido' genera "
            "similitudes con partidos SIN nombrarlos (colores/simbolos: "
            "Movimiento Naranja, Los Bolillos, Amarillo de Luz...) y 'mixto' "
            "reparte mitad persona / mitad partido. Usa OpenAI en lotes si hay "
            "key real y completa con el generador local. La seleccion acepta "
            "usuarios/todas/status/seccion y --desde/--hasta/--limite."
        ),
    )
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument("--usuarios", metavar="a,b,c",
                       help="Lista de logins internos separados por comas")
    grupo.add_argument("--todas", action="store_true",
                       help="Todas las cuentas twitter")
    grupo.add_argument("--status", metavar="STATUS",
                       help="Solo cuentas con ese status exacto")
    grupo.add_argument("--seccion", metavar="CI|IP|LIB|JUS|sin-asignar",
                       help="Solo cuentas de esa seccion; 'sin-asignar' = vacia")
    _agregar_rango(p)
    p.add_argument("--identidad",
                   choices=["auto", "persona", "movimiento", "partido", "mixto"],
                   default="auto",
                   metavar="auto|persona|movimiento|partido|mixto",
                   help="Tipo de identidad (default: auto, segun tipo_cuenta; "
                        "partido = similitud de partido sin nombrarlo; "
                        "mixto = mitad persona / mitad partido)")
    p.add_argument("--dry-run", action="store_true",
                   help="Genera y muestra propuestas sin escribir en la BD")
    p.add_argument("--json", default=None, metavar="RUTA",
                   help="Exporta las propuestas a un JSON legible")
    p.set_defaults(func=cmd_generar_nombres)

    # --- aplicar-nombres ---------------------------------------------------
    p = sub.add_parser(
        "aplicar-nombres",
        help="Aplica en X las propuestas pendientes (Selenium + Chrome)",
        description=(
            "Abre Chrome con las cookies de cada cuenta y aplica en paralelo la "
            "propuesta pendiente (nombre_propuesto/handle_propuesto). Solo "
            "procesa cuentas con propuesta. AVISO: hace cambios REALES en X; el "
            "cambio de @ puede pedir la contrasena (si no se pasa --password, se "
            "usa la de la BD). Con --renombrar, ademas del cambio en X migra la "
            "clave interna (Cuenta.usuario) al @ real. Con SQLite conviene "
            "--max-workers 2-3. La seleccion acepta "
            "usuarios/todas/status/seccion/tipo y --desde/--hasta/--limite."
        ),
    )
    _agregar_selectores_masivos(p)
    p.add_argument("--password", default=None, metavar="PASS",
                   help="Contrasena de X (opcional; si falta se lee de la BD)")
    p.add_argument("--max-workers", type=_entero_positivo, default=2, metavar="N",
                   help="Cuentas aplicadas a la vez, acotado a 1-4 (default: 2)")
    p.add_argument("--renombrar", action="store_true",
                   help="Tras cambiar el @ en X, migra la clave interna al @ real")
    p.add_argument("--dry-run", action="store_true",
                   help="Solo lista las propuestas a aplicar (no abre Chrome)")
    p.add_argument("--json", default=None, metavar="RUTA",
                   help="Exporta el resultado del lote a un JSON legible")
    p.set_defaults(func=cmd_aplicar_nombres)

    # --- generar-fotos -----------------------------------------------------
    p = sub.add_parser(
        "generar-fotos",
        help="Genera avatar/portada con IA (OpenAI; no toca X)",
        description=(
            "Genera con IA la foto de perfil (y la portada con --con-portada) "
            "de las cuentas seleccionadas y guarda las rutas en la BD. NO "
            "cambia nada en X: para aplicarlas usa 'aplicar-fotos'. Requiere "
            "OPENAI_API_KEY. Las imagenes se guardan en data/avatares/ "
            "(1024x1024) y data/portadas/ (1536x1024). La seleccion acepta "
            "usuarios/todas/status/seccion/tipo y --desde/--hasta/--limite."
        ),
    )
    _agregar_selectores_masivos(p)
    p.add_argument("--con-portada", action="store_true",
                   help="Genera tambien la foto de portada (1 imagen extra por cuenta)")
    p.add_argument("--forzar", action="store_true",
                   help="Regenera aunque la cuenta ya tenga avatar/portada")
    p.add_argument("--dry-run", action="store_true",
                   help="Solo muestra que generaria (no llama a OpenAI ni escribe en la BD)")
    p.set_defaults(func=cmd_generar_fotos)

    # --- aplicar-fotos -----------------------------------------------------
    p = sub.add_parser(
        "aplicar-fotos",
        help="Aplica en X las fotos generadas (Selenium + Chrome)",
        description=(
            "Abre Chrome con las cookies de cada cuenta y sube la foto de "
            "perfil (y la portada con --con-portada) ya guardadas en la BD. "
            "AVISO: abre Chrome y hace cambios REALES en X; las cuentas sin "
            "avatar_path/banner_path se reportan como fallidas. La seleccion "
            "acepta usuarios/todas/status/seccion/tipo y --desde/--hasta/--limite."
        ),
    )
    _agregar_selectores_masivos(p)
    p.add_argument("--con-portada", action="store_true",
                   help="Aplica tambien la portada guardada en banner_path")
    p.set_defaults(func=cmd_aplicar_fotos)

    # --- sincronizar -------------------------------------------------------
    p = sub.add_parser(
        "sincronizar",
        help="Lee nombre/@ reales desde X por httpx (sin Chrome)",
        description=(
            "Consulta el perfil real de cada cuenta (httpx, respeta el proxy de "
            "la cuenta) y actualiza nombre_mostrado/handle_actual. "
            "Sin selector usa --todas. Acepta --desde/--hasta/--limite para "
            "acotar el lote a sincronizar."
        ),
    )
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument("--usuarios", metavar="a,b,c",
                       help="Lista de logins internos separados por comas")
    grupo.add_argument("--todas", action="store_true",
                       help="Todas las cuentas twitter (opcion por defecto)")
    grupo.add_argument("--status", metavar="STATUS",
                       help="Solo cuentas con ese status exacto")
    grupo.add_argument("--seccion", metavar="CI|IP|LIB|JUS|sin-asignar",
                       help="Solo cuentas de esa seccion; 'sin-asignar' = vacia")
    _agregar_rango(p)
    p.add_argument("--timeout", type=_entero_positivo, default=20, metavar="SEG",
                   help="Timeout por peticion HTTP en segundos (default: 20)")
    p.set_defaults(func=cmd_sincronizar)

    # --- cambiar-perfil ----------------------------------------------------
    p = sub.add_parser(
        "cambiar-perfil",
        help="Cambia nombre/@ de UNA cuenta (Selenium + Chrome)",
        description=(
            "Abre Chrome con las cookies de la cuenta y aplica el cambio REAL de "
            "nombre y/o @. Si no se pasa --password y hay cambio de @, se lee de "
            "la base de datos."
        ),
    )
    p.add_argument("--usuario", required=True, metavar="USER",
                   help="Login interno de la cuenta en la base de datos")
    p.add_argument("--nombre", default=None, metavar="TEXTO",
                   help="Nuevo nombre visible (max. 50 caracteres en X)")
    p.add_argument("--handle", default=None, metavar="HANDLE",
                   help="Nuevo @usuario (4-15 letras, numeros o guion bajo)")
    p.add_argument("--password", default=None, metavar="PASS",
                   help="Contrasena de X (opcional; si falta se lee de la BD)")
    p.set_defaults(func=cmd_cambiar_perfil)

    # --- renombrar ---------------------------------------------------------
    p = sub.add_parser(
        "renombrar",
        help="Renombra la clave interna (Cuenta.usuario) y migra archivos",
        description=(
            "Renombra la clave interna de UNA cuenta (Cuenta.usuario) migrando "
            "cookies, carpeta de perfil Chrome, avatar/portada, los campos "
            "cookies_path/avatar_path/banner_path y el historial de "
            "RegistroAccion. NO cambia nada en X: solo la base de datos y los "
            "archivos locales. Con --actual usa handle_actual (de la BD) como "
            "destino; si la cuenta cambio de @ en X, sincroniza primero."
        ),
    )
    p.add_argument("--usuario", required=True, metavar="USER",
                   help="Clave interna actual (Cuenta.usuario)")
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--nuevo", metavar="NUEVO",
                       help="Nuevo nombre de la clave interna (sin @; si no "
                            "parece un handle de X se advierte, no se bloquea)")
    grupo.add_argument("--actual", action="store_true",
                       help="Usa Cuenta.handle_actual de la BD como nuevo nombre")
    p.add_argument("--dry-run", action="store_true",
                   help="Muestra que haria sin tocar la base de datos ni el disco")
    p.set_defaults(func=cmd_renombrar)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    """Punto de entrada del CLI; devuelve el codigo de salida."""
    _configurar_logging()
    parser = construir_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "comando", None):
        parser.print_help()
        return 0

    if not _preparar_bd():
        return 1

    try:
        return args.func(args) or 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
