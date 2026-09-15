# -*- coding: utf-8 -*-
"""Renombrado seguro de la clave interna ``Cuenta.usuario``.

La clave interna (``Cuenta.usuario``) identifica la cuenta para login/cookies y
para las rutas locales (cookies, perfil Chrome, avatar/portada). Cuando la
cuenta cambia de @ en X el inventario queda desactualizado: este modulo renombra
esa clave migrando TODO lo que depende de ella:

* Archivos de cookies ``data/cookies/<plataforma>/<usuario>.pkl`` (twitter es el
  caso principal; el resto de plataformas se soportan igual).
* Carpeta de perfil Chrome ``data/perfiles_chrome/<usuario>/`` (si existe).
* Imagenes ``data/avatares/<archivo_seguro>.png`` y
  ``data/portadas/<archivo_seguro>.png``, con el mismo criterio de nombre que
  ``cuentas/fotos.py`` (``_archivo_seguro`` replicado para no crear un import
  circular ``core -> cuentas``). Solo se migran los nombres esperados: si el
  archivo canonico no existe, no se adivina ningun otro.
* Campos de la BD ``Cuenta.cookies_path``, ``Cuenta.avatar_path`` y
  ``Cuenta.banner_path`` cuando contienen el nombre viejo (se respeta la forma
  guardada: relativa o absoluta).
* Historial ``RegistroAccion.usuario`` (para no perder el historial).

Las ``Tarea`` referencian cuentas por ``id`` (JSON de ids), NO por usuario, por
lo que no se tocan.

Interfaz congelada::

    renombrar_usuario(usuario_actual, nuevo_usuario, dry_run=False) -> dict
    renombrar_al_handle_actual(usuario_actual, dry_run=False) -> dict

Reglas de diseno:

* ``renombrar_usuario`` NUNCA lanza excepcion: todos los fallos se reportan en
  el dict devuelto (``ok``/``error``/``advertencias``/``archivos``).
* Orden: (1) validar y calcular, (2) actualizar la BD, (3) mover archivos. Si la
  BD falla no se mueve ningun archivo (``ok=False``). Si un archivo falla, se
  registra en ``archivos[].error`` + ``advertencias`` pero no revienta.
* ``dry_run=True`` calcula y reporta exactamente lo mismo sin tocar la BD ni el
  disco (``renombrado=False``, ``ok=True`` si la validacion pasa).
* Los archivos originales nunca se borran antes de que el destino este movido.
"""
import os
import re
import shutil

from loguru import logger

from core.config import resolver_ruta

__all__ = ["renombrar_usuario", "renombrar_al_handle_actual"]

# Mismo patron de X que usan cuentas/generador_identidades.py y la web. Si el
# nombre nuevo no lo cumple se advierte, pero NO se bloquea: algunas claves
# internas no son handles (ej. nombres importados con puntos o guiones).
_PATRON_HANDLE = re.compile(r"^[A-Za-z0-9_]{4,15}$")

# Espacios y barras no pueden formar parte de la clave interna ni de las rutas.
_PROHIBIDOS = re.compile(r"[\s/\\]+")

# Orden y carpetas de los planes de archivos.
_CARPETAS = {
    "avatar": "avatares",
    "portada": "portadas",
}


# --------------------------------------------------------------------------- #
# Utilidades puras
# --------------------------------------------------------------------------- #
def _archivo_seguro(nombre) -> str:
    """Nombre de archivo sin caracteres problematicos.

    Replica ``cuentas.fotos._archivo_seguro`` (no se importa desde ahi para no
    crear una dependencia circular ``core -> cuentas``):
    ``[A-Za-z0-9_.-]`` se conserva; todo lo demas se reemplaza por ``_``.
    """
    limpio = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(nombre or "").strip())
    return limpio.strip("._") or "cuenta"


def _limpiar_usuario(valor, advertencias=None) -> str:
    """Normaliza un nombre de usuario.

    Quita espacios externos y ``@`` iniciales; elimina espacios internos y
    barras ``/`` ``\\`` (romperian la clave y las rutas) avisando cuando se
    limpiaron caracteres.
    """
    texto = str(valor or "").strip()
    while texto.startswith("@"):
        texto = texto[1:].strip()
    if _PROHIBIDOS.search(texto):
        limpio = _PROHIBIDOS.sub("", texto)
        if advertencias is not None:
            advertencias.append(
                f"se quitaron espacios o barras de {texto!r} (no pueden formar "
                f"parte de la clave interna ni de las rutas): queda {limpio!r}"
            )
        texto = limpio
    return texto


def _resultado(anterior, nuevo, dry_run, advertencias) -> dict:
    """Dict con la interfaz congelada (estado inicial de error)."""
    return {
        "ok": False,
        "usuario_anterior": anterior,
        "usuario_nuevo": nuevo,
        "dry_run": bool(dry_run),
        "renombrado": False,
        "archivos": [],
        "referencias": {
            "registros": 0,
            "cookies_path": False,
            "rutas_imagenes": False,
        },
        # Se conserva la MISMA lista para que los avisos agregados despues de
        # validar (handle invalido, fallos de archivo) aparezcan en el dict.
        "advertencias": advertencias,
        "error": "",
    }


def _reemplazar_segmento(ruta, viejo, nuevo):
    """Reemplaza el ultimo segmento de ``ruta`` igual a ``viejo``.

    Conserva separadores y extension: ``.../viejo.pkl`` -> ``.../nuevo.pkl``,
    ``.../viejo/Cookies`` -> ``.../nuevo/Cookies``. Solo cambia el segmento mas
    a la derecha para no tocar nombres de carpetas globales (ej. ``data``).
    """
    if not ruta or not viejo or viejo == nuevo:
        return ruta, False
    partes = re.split(r"([/\\])", ruta)
    for indice in range(len(partes) - 1, -1, -1):
        if indice % 2 == 1:  # separador
            continue
        parte = partes[indice]
        if not parte:
            continue
        base, extension = os.path.splitext(parte)
        if parte == viejo or base == viejo:
            partes[indice] = nuevo + extension
            return "".join(partes), True
    return ruta, False


def _migrar_campo_ruta(valor, anterior, nuevo):
    """Actualiza un campo de ruta de la BD si contiene el usuario viejo.

    Devuelve ``(nuevo_valor, cambio)``. Prueba primero con el nombre crudo
    (cookies/perfil) y despues con el nombre seguro (imagenes). Si no contiene
    el nombre viejo, el valor se deja intacto.
    """
    texto = str(valor or "").strip()
    if not texto or anterior == nuevo:
        return texto, False
    intentos = [(anterior, nuevo)]
    seguro_viejo = _archivo_seguro(anterior)
    seguro_nuevo = _archivo_seguro(nuevo)
    if seguro_viejo != anterior or seguro_nuevo != nuevo:
        intentos.append((seguro_viejo, seguro_nuevo))
    for viejo, reemplazo in intentos:
        if not viejo or viejo == reemplazo:
            continue
        actualizado, cambio = _reemplazar_segmento(texto, viejo, reemplazo)
        if cambio:
            return actualizado, True
    return texto, False


def _planes_archivos(plataforma, anterior, nuevo):
    """Tuplas ``(tipo, origen, destino)`` de archivos/directorios a migrar.

    Las rutas son absolutas (``resolver_ruta``) y corresponden a los nombres
    canonicos esperados; el llamador solo incluye las que existen.
    """
    plataforma = (plataforma or "twitter").strip().lower() or "twitter"
    avatar_carpeta = _CARPETAS["avatar"]
    portada_carpeta = _CARPETAS["portada"]
    return [
        (
            "cookies",
            resolver_ruta(f"data/cookies/{plataforma}/{anterior}.pkl"),
            resolver_ruta(f"data/cookies/{plataforma}/{nuevo}.pkl"),
        ),
        (
            "perfil_chrome",
            resolver_ruta(f"data/perfiles_chrome/{anterior}"),
            resolver_ruta(f"data/perfiles_chrome/{nuevo}"),
        ),
        (
            "avatar",
            resolver_ruta(f"data/{avatar_carpeta}/{_archivo_seguro(anterior)}.png"),
            resolver_ruta(f"data/{avatar_carpeta}/{_archivo_seguro(nuevo)}.png"),
        ),
        (
            "portada",
            resolver_ruta(f"data/{portada_carpeta}/{_archivo_seguro(anterior)}.png"),
            resolver_ruta(f"data/{portada_carpeta}/{_archivo_seguro(nuevo)}.png"),
        ),
    ]


def _existe(ruta) -> bool:
    try:
        return os.path.exists(ruta)
    except OSError:
        return False


def _mismo_archivo(a, b) -> bool:
    """True si ``a`` y ``b`` apuntan al MISMO archivo/directorio.

    En Windows (case-insensitive) permite distinguir "solo cambia el caso" de
    una colision real; en Linux devuelve False para nombres distintos.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _mover(origen, destino) -> None:
    """Mueve ``origen`` a ``destino`` sin borrar el original si el destino falla.

    Si el destino ya existe y NO es el mismo archivo, lanza ``OSError`` (no se
    sobrescribe nada). El cambio de mayusculas/minusculas usa ``os.rename``.
    """
    if _existe(destino) and not _mismo_archivo(origen, destino):
        raise OSError(f"el destino ya existe: {destino}")
    if _mismo_archivo(origen, destino):
        os.rename(origen, destino)  # solo cambia mayusculas/minusculas
        return
    carpeta = os.path.dirname(destino)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    shutil.move(origen, destino)


# --------------------------------------------------------------------------- #
# Acceso a la BD
# --------------------------------------------------------------------------- #
def _leer_cuenta(usuario):
    """Datos de la cuenta + numero de registros historicos; None si no existe."""
    from core.database import get_db_session
    from core.models import Cuenta, RegistroAccion

    with get_db_session() as db:
        cuenta = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
        if cuenta is None:
            return None
        datos = {
            "id": cuenta.id,
            "usuario": cuenta.usuario or "",
            "plataforma": (cuenta.plataforma or "").strip() or "twitter",
            "cookies_path": cuenta.cookies_path or "",
            "avatar_path": getattr(cuenta, "avatar_path", "") or "",
            "banner_path": getattr(cuenta, "banner_path", "") or "",
        }
        datos["registros"] = (
            db.query(RegistroAccion)
            .filter(RegistroAccion.usuario == usuario)
            .count()
        )
        return datos


def _buscar_colision(nuevo, cuenta_id):
    """Usuario de otra cuenta que colisiona (ignorando mayusculas) o None."""
    from sqlalchemy import func

    from core.database import get_db_session
    from core.models import Cuenta

    with get_db_session() as db:
        existente = (
            db.query(Cuenta)
            .filter(func.lower(Cuenta.usuario) == nuevo.lower())
            .first()
        )
        if existente is None or existente.id == cuenta_id:
            return None
        return existente.usuario or ""


def _actualizar_bd(cuenta_id, anterior, nuevo, cookies_path, avatar_path, banner_path) -> int:
    """Renombra la cuenta y sus referencias en UNA transaccion.

    Devuelve el numero de filas de ``RegistroAccion`` actualizadas. Si algo
    falla, el context manager hace rollback (incluido ``Cuenta.usuario``) y la
    excepcion sube al llamador, que reporta el error sin mover archivos.
    """
    from core.database import get_db_session
    from core.models import Cuenta, RegistroAccion

    with get_db_session() as db:
        cuenta = db.query(Cuenta).filter(Cuenta.id == cuenta_id).first()
        if cuenta is None:
            raise RuntimeError(
                f"la cuenta id={cuenta_id} desaparecio de la base de datos"
            )
        cuenta.usuario = nuevo
        if (cuenta.cookies_path or "") != cookies_path:
            cuenta.cookies_path = cookies_path
        if (getattr(cuenta, "avatar_path", "") or "") != avatar_path:
            cuenta.avatar_path = avatar_path
        if (getattr(cuenta, "banner_path", "") or "") != banner_path:
            cuenta.banner_path = banner_path
        registros = (
            db.query(RegistroAccion)
            .filter(RegistroAccion.usuario == anterior)
            .update({RegistroAccion.usuario: nuevo}, synchronize_session=False)
        )
    return int(registros or 0)


# --------------------------------------------------------------------------- #
# Interfaz publica
# --------------------------------------------------------------------------- #
def renombrar_usuario(usuario_actual: str, nuevo_usuario: str, dry_run: bool = False) -> dict:
    """Renombra Cuenta.usuario y migra archivos/referencias. NUNCA lanza excepcion.

    Devuelve:
    {
      "ok": bool,                    # True solo si la BD quedo renombrada (o dry_run valido)
      "usuario_anterior": str,
      "usuario_nuevo": str,
      "dry_run": bool,
      "renombrado": bool,            # la fila Cuenta.usuario se actualizo
      "archivos": [ {"tipo": "cookies|perfil_chrome|avatar|portada",
                     "de": ruta, "a": ruta, "ok": bool, "error": str}, ... ],
      "referencias": {"registros": int, "cookies_path": bool, "rutas_imagenes": bool},
      "advertencias": [str, ...],
      "error": str,                  # "" si todo ok; motivo legible si no
    }
    """
    advertencias = []
    anterior = _limpiar_usuario(usuario_actual, advertencias)
    nuevo = _limpiar_usuario(nuevo_usuario, advertencias)
    dry_run = bool(dry_run)
    resultado = _resultado(anterior, nuevo, dry_run, advertencias)

    try:
        # (1) Validacion.
        if not anterior:
            resultado["error"] = "el usuario actual esta vacio"
            return resultado
        if not nuevo:
            resultado["error"] = "el nuevo usuario esta vacio"
            return resultado
        if anterior == nuevo:
            resultado["error"] = (
                "el nuevo usuario es igual al actual; no hay nada que renombrar"
            )
            return resultado
        if not _PATRON_HANDLE.match(nuevo):
            advertencias.append(
                f"{nuevo!r} no cumple el patron de handle de X "
                "(^[A-Za-z0-9_]{4,15}$); se usara igual como clave interna"
            )

        datos = _leer_cuenta(anterior)
        if datos is None:
            resultado["error"] = (
                f"la cuenta {anterior!r} no existe en la base de datos"
            )
            return resultado

        colision = _buscar_colision(nuevo, datos["id"])
        if colision:
            resultado["error"] = (
                f"ya existe otra cuenta con el usuario {colision!r} "
                "(la comparacion ignora mayusculas/minusculas); no se cambio nada"
            )
            return resultado

        # (2) Plan: referencias de la BD y archivos en disco.
        cookies_nuevo, cambio_cookies = _migrar_campo_ruta(
            datos["cookies_path"], anterior, nuevo
        )
        avatar_nuevo, cambio_avatar = _migrar_campo_ruta(
            datos["avatar_path"], anterior, nuevo
        )
        banner_nuevo, cambio_banner = _migrar_campo_ruta(
            datos["banner_path"], anterior, nuevo
        )
        resultado["referencias"] = {
            "registros": int(datos["registros"]),
            "cookies_path": bool(cambio_cookies),
            "rutas_imagenes": bool(cambio_avatar or cambio_banner),
        }

        archivos = []
        for tipo, origen, destino in _planes_archivos(
            datos["plataforma"], anterior, nuevo
        ):
            if not _existe(origen):
                continue
            entrada = {
                "tipo": tipo,
                "de": origen,
                "a": destino,
                "ok": True,
                "error": "",
            }
            if _existe(destino) and not _mismo_archivo(origen, destino):
                entrada["ok"] = False
                entrada["error"] = f"el destino ya existe: {destino}"
            archivos.append(entrada)
        resultado["archivos"] = archivos

        if dry_run:
            resultado["ok"] = True
            return resultado

        # (3) BD primero: si falla, no se mueve nada.
        try:
            registros = _actualizar_bd(
                datos["id"], anterior, nuevo,
                cookies_nuevo, avatar_nuevo, banner_nuevo,
            )
        except Exception as e:
            resultado["error"] = (
                f"no se pudo renombrar en la base de datos "
                f"({type(e).__name__}: {e}); no se movio ningun archivo"
            )
            return resultado

        resultado["renombrado"] = True
        # La BD ya quedo renombrada: ok=True aunque un archivo falle despues.
        resultado["ok"] = True
        resultado["referencias"]["registros"] = int(registros)

        # (4) Archivos: un fallo individual no revienta el renombrado.
        for entrada in archivos:
            if not entrada["ok"]:
                advertencias.append(
                    f"{entrada['tipo']}: no se migro {entrada['de']!r}: "
                    f"{entrada['error']}"
                )
                continue
            try:
                _mover(entrada["de"], entrada["a"])
                logger.info(
                    f"Renombrado {entrada['tipo']} de '{anterior}' a "
                    f"'{nuevo}': {entrada['a']}"
                )
            except Exception as e:
                entrada["ok"] = False
                entrada["error"] = f"{type(e).__name__}: {e}"
                advertencias.append(
                    f"{entrada['tipo']}: no se migro {entrada['de']!r}: "
                    f"{entrada['error']}"
                )

        return resultado
    except Exception as e:  # blindaje global: nunca lanzar
        logger.exception(f"Error inesperado renombrando '{usuario_actual}': {e}")
        resultado["error"] = f"{type(e).__name__}: {e}"
        return resultado


def renombrar_al_handle_actual(usuario_actual: str, dry_run: bool = False) -> dict:
    """Renombra la clave interna usando ``Cuenta.handle_actual`` como destino.

    Lee el @ real guardado en la BD (el que dejo ``sincronizar``) y delega en
    ``renombrar_usuario``. Devuelve el mismo dict; si la cuenta no existe o no
    tiene ``handle_actual`` devuelve error legible (``ok=False``). Nunca lanza.
    """
    advertencias = []
    anterior = _limpiar_usuario(usuario_actual, advertencias)
    resultado = _resultado(anterior, "", bool(dry_run), advertencias)

    try:
        if not anterior:
            resultado["error"] = "el usuario actual esta vacio"
            return resultado

        from core.database import get_db_session
        from core.models import Cuenta

        with get_db_session() as db:
            cuenta = db.query(Cuenta).filter(Cuenta.usuario == anterior).first()
            if cuenta is None:
                resultado["error"] = (
                    f"la cuenta {anterior!r} no existe en la base de datos"
                )
                return resultado
            handle = (getattr(cuenta, "handle_actual", "") or "").strip().lstrip("@")

        if not handle:
            resultado["error"] = (
                f"la cuenta {anterior!r} no tiene handle_actual; "
                "sincroniza primero (comando 'sincronizar')"
            )
            return resultado

        resultado = renombrar_usuario(anterior, handle, dry_run=bool(dry_run))
        # Si ya habia avisos de la limpieza, se conservan antes de los nuevos.
        resultado["advertencias"] = advertencias + list(
            resultado.get("advertencias") or []
        )
        return resultado
    except Exception as e:
        logger.exception(f"Error inesperado renombrando '{usuario_actual}' al handle actual: {e}")
        resultado["error"] = f"{type(e).__name__}: {e}"
        return resultado
