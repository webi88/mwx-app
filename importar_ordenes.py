"""Importacion de ORDENES de compra de cuentas de X (Twitter).

Lee uno o varios archivos de orden (una cuenta por linea, campos separados por
`:`) y hace UPSERT por `Cuenta.usuario` (case-insensitive) usando la heuristica
por FORMA de `cuentas.importador.parsear_linea`: los campos NO detectados se
asignan respetando su posicion relativa a los detectados (segmentos del orden
canonico), asi que el `email_password` de una orden SIN TOTP jamas cae en
`totp_secret`.

Formatos soportados por el parser (ver `cuentas/importador.py`):

    5 campos sin TOTP : usuario:password:email:email_password:auth_token
    6 campos con TOTP : usuario:password:email:email_password:auth_token:totp
    7/8 campos        : ...[:cookies][:user_agent] (Aged / vendedor)

UPSERT:

* Si la cuenta NO existe: se crea con `plataforma="twitter"`, `activa=True`,
  `pausada_activacion=False` (True si su usuario viene en la lista de pausa) y
  las credenciales que traiga la orden.
* Si YA existe: SOLO se pisan los campos NO vacios del lote (`password`,
  `email`, `email_password`, `auth_token`, `totp_secret`). NUNCA se tocan
  `cookies_json`, `seccion`, `tier_calidad`, avatares/portadas,
  `rol_activacion` ni `status`: una cuenta existente solo recibe credenciales.

Pausa para activacion: los usuarios de `--pausar "u1,u2"` o de
`--pausar-archivo RUTA` (por defecto `data/clientes/a_importar.txt` si existe)
quedan con `pausada_activacion=True`; el resto NO cambia su pausa existente
(False solo al crear). El archivo de pausa acepta `usuario`, `@usuario` o la
linea completa `usuario:password:...`.

Por defecto corre en SIMULACION (dry-run): solo consulta y reporta que haria,
sin escribir nada. La importacion real requiere `--apply`.

La operacion apunta a la base de datos de `DATABASE_URL` (por defecto la local
`data/gestor_redes.db`). Para importar en Supabase, exporta la DATABASE_URL de
Railway antes de correr (los valores reales no van en el repo):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python importar_ordenes.py --apply

Uso:
    python importar_ordenes.py                                # dry-run de data/clientes/order*.txt
    python importar_ordenes.py order1.txt order2.txt --apply  # importacion real
    python importar_ordenes.py --apply --json                 # resumen JSON
    python importar_ordenes.py --apply --pausar "cuenta_uno,cuenta_dos"

AVISO: los archivos de orden son SENSIBLES (traen credenciales). Viven en
`data/clientes/` (ignorado por git) y este script NUNCA imprime contrasenas,
auth_tokens ni semillas 2FA completas: solo el usuario y que campos se tocaron.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    # Ruta absoluta desde la raiz del proyecto (core/config.py).
    from core.config import resolver_ruta
except Exception:  # pragma: no cover - el script debe funcionar aunque falle la config
    def resolver_ruta(relativa: str) -> str:
        """Fallback sin dependencias: resuelve relativo a este archivo."""
        return str((Path(__file__).resolve().parent / relativa).resolve())


PROJECT_ROOT = Path(resolver_ruta("."))
CARPETA_CLIENTES = PROJECT_ROOT / "data" / "clientes"
PATRON_ORDENES = "order*.txt"
ARCHIVO_PAUSA_POR_DEFECTO = "data/clientes/a_importar.txt"

# Campos del lote que SI se pueden pisar en una cuenta existente (y solo con
# valor NO vacio). NO incluye `cookies_json`, `seccion`, `tier_calidad`,
# avatares/portadas, `rol_activacion` ni `status`: esas columnas jamas se tocan.
CAMPOS_ACTUALIZABLES = (
    "password",
    "email",
    "email_password",
    "auth_token",
    "totp_secret",
)


def _ocultar_credenciales(mensaje, *secretos) -> str:
    """Reemplaza cualquier credencial conocida por `***` en un mensaje."""
    texto = str(mensaje or "")
    for secreto in secretos:
        if secreto:
            texto = texto.replace(str(secreto), "***")
    return " ".join(texto.split())[:200]


def _informado(valor) -> bool:
    """True si el lote trae un valor no vacio (tras `strip`)."""
    return isinstance(valor, str) and bool(valor.strip())


def parsear_usuarios_pausa(texto) -> list:
    """Usuarios de una lista de pausa (una por linea).

    Acepta `usuario`, `@usuario` o la linea completa `usuario:password:...`;
    ignora lineas vacias y comentarios (`#`). Dedupe case-insensitive
    conservando la primera forma escrita. Nunca lanza.
    """
    usuarios = []
    vistos = set()
    for linea in str(texto or "").splitlines():
        recortada = linea.strip()
        if not recortada or recortada.startswith("#"):
            continue
        usuario = recortada.split(":", 1)[0].strip().lstrip("@").strip()
        if not usuario or usuario.lower() in vistos:
            continue
        vistos.add(usuario.lower())
        usuarios.append(usuario)
    return usuarios


def _leer_archivo(ruta):
    """Lee un archivo de texto (None si no se pudo leer)."""
    try:
        with Path(ruta).open("r", encoding="utf-8", errors="replace") as manejador:
            return manejador.read()
    except Exception:
        return None


def _normalizar_archivos(archivos):
    """Resuelve la lista de archivos de orden (o el glob por defecto)."""
    if archivos:
        return [str(archivo) for archivo in archivos]
    if CARPETA_CLIENTES.is_dir():
        return [str(ruta) for ruta in sorted(CARPETA_CLIENTES.glob(PATRON_ORDENES))]
    return []


def importar_ordenes(archivos=None, dry_run: bool = True, usuarios_pausar=None) -> dict:
    """Importa/actualiza las cuentas de las ordenes de compra.

    `archivos`: lista de rutas (si es None/vacia usa `data/clientes/order*.txt`).
    `usuarios_pausar`: usuarios que quedaran con `pausada_activacion=True`; el
    resto conserva su pausa existente (False solo al crear).

    Devuelve un resumen con `ok`, `dry_run`, `archivos`, `total` (lineas no
    vacias), `creadas`, `actualizadas`, `errores`, `detalle_errores`, `cuentas`
    (usuario, accion, `existia`, `campos_actualizados`, `pausada`; NUNCA
    credenciales), `pausa` (usuarios pedidos) y `error` (fallo global). Nunca
    lanza: los errores por linea/cuenta se aislan y se reportan saneados.

    Con `dry_run=True` solo se consulta la BD y se reporta que pasaria; con
    `dry_run=False` los cambios se confirman en UNA transaccion. El import de
    `core.database` es perezoso para que el script no conecte al cargarse.
    """
    rutas = _normalizar_archivos(archivos)
    pausa = {
        str(usuario).lstrip("@").strip().lower()
        for usuario in (usuarios_pausar or [])
        if str(usuario or "").strip()
    }
    resultado = {
        "ok": False,
        "dry_run": bool(dry_run),
        "archivos": list(rutas),
        "total": 0,
        "creadas": 0,
        "actualizadas": 0,
        "errores": 0,
        "detalle_errores": [],
        "cuentas": [],
        "pausa": sorted(pausa),
        "error": "",
    }

    if not rutas:
        resultado["error"] = (
            "no se encontro ningun archivo de ordenes (pasa las rutas o deja "
            f"{PATRON_ORDENES} en data/clientes/)."
        )
        return resultado

    # 1) Leer y parsear TODOS los archivos (la heuristica por forma ya asigna
    #    email/totp/auth_token por segmentos del orden canonico). Import
    #    perezoso (mismo patron que importar_clientes.py): el script no importa
    #    core.database al cargarse.
    from cuentas.importador import parsear_linea

    registros = []
    for ruta in rutas:
        crudo = _leer_archivo(ruta)
        if crudo is None:
            resultado["errores"] += 1
            resultado["detalle_errores"].append(f"{ruta}: no se pudo leer.")
            continue
        for numero, linea in enumerate(crudo.splitlines(), start=1):
            texto = linea.strip()
            if not texto or texto.startswith("#"):
                continue
            resultado["total"] += 1
            try:
                fields = parsear_linea(texto)
            except Exception:  # defensivo: parsear_linea nunca deberia lanzar
                fields = None
            if fields is None:
                resultado["errores"] += 1
                resultado["detalle_errores"].append(
                    f"{Path(ruta).name}:{numero}: linea malformada."
                )
                continue
            registros.append(fields)

    if not registros:
        if resultado["errores"]:
            resultado["error"] = "no habia lineas validas que importar."
        resultado["ok"] = resultado["errores"] == 0
        return resultado

    # Import perezoso: el script no importa core.database (ni conecta) al cargarse.
    from sqlalchemy import func

    from core.database import get_db_session
    from core.models import Cuenta

    try:
        with get_db_session() as db:
            for fields in registros:
                usuario = fields["username"]
                secretos = (
                    fields.get("password"),
                    fields.get("email_password"),
                    fields.get("auth_token"),
                    fields.get("totp_secret"),
                )
                pausar = usuario.lower() in pausa
                try:
                    existente = (
                        db.query(Cuenta)
                        .filter(func.lower(Cuenta.usuario) == usuario.lower())
                        .first()
                    )
                    if existente is None:
                        if not dry_run:
                            db.add(
                                Cuenta(
                                    usuario=usuario,
                                    password=fields.get("password") or "",
                                    email=fields.get("email") or "",
                                    email_password=fields.get("email_password") or "",
                                    auth_token=fields.get("auth_token") or "",
                                    totp_secret=fields.get("totp_secret") or "",
                                    plataforma="twitter",
                                    activa=True,
                                    pausada_activacion=pausar,
                                    status="imported",
                                    fecha_creacion=datetime.utcnow(),
                                )
                            )
                        campos_actualizados = [
                            campo
                            for campo in CAMPOS_ACTUALIZABLES
                            if _informado(fields.get(campo))
                        ]
                        if pausar:
                            campos_actualizados.append("pausada_activacion")
                        resultado["creadas"] += 1
                        accion = "crear"
                    else:
                        campos_actualizados = [
                            campo
                            for campo in CAMPOS_ACTUALIZABLES
                            if _informado(fields.get(campo))
                        ]
                        pausa_cambia = pausar and not bool(
                            getattr(existente, "pausada_activacion", False)
                        )
                        if not dry_run:
                            for campo in campos_actualizados:
                                setattr(existente, campo, fields[campo])
                            if pausar:
                                existente.pausada_activacion = True
                        if pausa_cambia:
                            campos_actualizados.append("pausada_activacion")
                        resultado["actualizadas"] += 1
                        accion = "actualizar"

                    resultado["cuentas"].append(
                        {
                            "usuario": usuario,
                            "accion": accion,
                            "existia": existente is not None,
                            "campos_actualizados": campos_actualizados,
                            "pausada": bool(
                                pausar
                                or (
                                    existente is not None
                                    and getattr(existente, "pausada_activacion", False)
                                )
                            ),
                        }
                    )
                except Exception as error:
                    resultado["errores"] += 1
                    resultado["detalle_errores"].append(
                        f"{usuario}: {type(error).__name__}: "
                        f"{_ocultar_credenciales(error, *secretos)}"
                    )
            resultado["ok"] = resultado["errores"] == 0
    except Exception as error:
        resultado["error"] = (
            "no se pudo abrir/escribir la base de datos "
            f"({_ocultar_credenciales(f'{type(error).__name__}: {error}')})."
        )
        resultado["ok"] = False
    return resultado


def _descripcion_destino() -> str:
    """Descripcion de la BD destino sin exponer credenciales."""
    try:
        from restaurar_cuentas import _describir_destino_bd

        return _describir_destino_bd()
    except Exception:
        try:
            from core.config import settings

            url = settings.database_url or ""
        except Exception:
            url = ""
        if not url:
            return "DATABASE_URL no definida; se usara la local por defecto"
        if url.startswith("sqlite"):
            return f"SQLite: {resolver_ruta(url.split(':///', 1)[-1])}"
        return "remota (credenciales ocultas)"


def _imprimir_resumen(resultado: dict, origen_pausa: str = "") -> None:
    """Resumen legible para el operador (nunca imprime credenciales)."""
    modo = (
        "SIMULACION (dry-run, no escribe)"
        if resultado.get("dry_run")
        else "REAL (escribe en la BD)"
    )
    print("=" * 62)
    print("Importacion de ORDENES de compra (lotes de cuentas)")
    print("=" * 62)
    print(f"Archivos  : {', '.join(resultado.get('archivos') or []) or '-'}")
    print(f"BD destino: {_descripcion_destino()}")
    print(f"Modo      : {modo}")
    pausa = resultado.get("pausa") or []
    if pausa:
        origen = f" ({origen_pausa})" if origen_pausa else ""
        print(f"Pausa     : {len(pausa)} usuario(s){origen}")
    if resultado.get("error"):
        print("ESTADO    : con errores (revisa el detalle)")
    print("-" * 62)
    print(f"Lineas validas : {int(resultado.get('total') or 0)}")
    print(f"Creadas        : {int(resultado.get('creadas') or 0)}")
    print(f"Actualizadas   : {int(resultado.get('actualizadas') or 0)}")
    print(f"Errores        : {int(resultado.get('errores') or 0)}")

    cuentas = list(resultado.get("cuentas") or [])
    if cuentas:
        print("-" * 62)
        for fila in cuentas[:25]:
            etiqueta = "NUEVA" if fila.get("accion") == "crear" else "ACTUALIZADA"
            campos = ", ".join(fila.get("campos_actualizados") or []) or "-"
            pausada = " | pausada" if fila.get("pausada") else ""
            print(f"  [{etiqueta}] {fila.get('usuario')} ({campos}){pausada}")
        if len(cuentas) > 25:
            print(f"  ... y {len(cuentas) - 25} mas")

    detalle = resultado.get("detalle_errores") or []
    if detalle:
        print("-" * 62)
        print("Primeros errores:")
        for mensaje in detalle[:5]:
            print(f"  - {mensaje}")
        if len(detalle) > 5:
            print(f"  ... y {len(detalle) - 5} mas")

    if resultado.get("error"):
        print("-" * 62)
        print(f"ERROR: {resultado['error']}")

    print("-" * 62)
    if resultado.get("dry_run"):
        print("AVISO: simulacion sin cambios; la importacion real requiere --apply.")
    elif resultado.get("error"):
        print("La importacion se detuvo; revisa el error de arriba.")
    else:
        print("Importacion aplicada (transaccion confirmada).")
    print(
        "Las cuentas con pausa quedan fuera de campanas pero disponibles para "
        "mantenimiento; el resto no cambia su pausa (False solo al crear)."
    )
    print(
        "AVISO: la operacion apunta a la BD de DATABASE_URL (por defecto la "
        "local data/gestor_redes.db). Para Supabase, exporta la DATABASE_URL "
        "de Railway antes de correr."
    )


def _resolver_pausa(pausar: str, pausar_archivo) -> tuple:
    """Resuelve `(usuarios, origen, error)` de `--pausar`/`--pausar-archivo`.

    Sin `--pausar-archivo` usa `data/clientes/a_importar.txt` si existe. Si un
    archivo pedido/existente no se puede leer, devuelve un error (mejor abortar
    que importar cuentas que deberian quedar pausadas).
    """
    usuarios = []
    origenes = []
    if pausar:
        usuarios += parsear_usuarios_pausa(str(pausar).replace(",", "\n"))
        origenes.append("--pausar")
    ruta = pausar_archivo
    explicito = bool(pausar_archivo)
    if not ruta:
        candidata = Path(resolver_ruta(ARCHIVO_PAUSA_POR_DEFECTO))
        if candidata.is_file():
            ruta = str(candidata)
    if ruta:
        crudo = _leer_archivo(ruta)
        if crudo is None:
            if explicito:
                return [], "", f"no se pudo leer la lista de pausa '{ruta}'."
            return [], "", ""
        usuarios += parsear_usuarios_pausa(crudo)
        origenes.append(str(ruta))

    deduplicados = []
    vistos = set()
    for usuario in usuarios:
        clave = usuario.lower()
        if clave not in vistos:
            vistos.add(clave)
            deduplicados.append(usuario)
    return deduplicados, " + ".join(origenes), ""


def main(argv=None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = argparse.ArgumentParser(
        description=(
            "Importa ORDENES de compra de cuentas "
            "(usuario:...:auth_token[:totp]) y las agrega a la BD. Por defecto "
            "es un DRY-RUN: no escribe nada hasta que se pasa --apply."
        ),
        epilog=(
            "SEGURIDAD: no imprime contrasenas, auth_tokens ni semillas 2FA. "
            "Los archivos de orden viven en data/clientes/ (ignorado por git). "
            "Para Supabase, exporta la DATABASE_URL de Railway antes de correr: "
            "la importacion escribe en ESA base de datos."
        ),
    )
    parser.add_argument(
        "archivos",
        nargs="*",
        default=None,
        help=(
            "Archivos de orden (uno o varios). Si se omiten, usa todos los "
            f"data/clientes/{PATRON_ORDENES}."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Escribe de verdad en la BD. Sin esta bandera todo es dry-run.",
    )
    parser.add_argument(
        "--json",
        dest="como_json",
        action="store_true",
        help="Imprime solo el resumen en JSON (util para automatizar).",
    )
    parser.add_argument(
        "--pausar",
        default="",
        help=(
            "Usuarios separados por comas que quedaran con "
            'pausada_activacion=True (ej. "cuenta_uno,cuenta_dos").'
        ),
    )
    parser.add_argument(
        "--pausar-archivo",
        default=None,
        help=(
            "Archivo con los usuarios a pausar (usuario, @usuario o la linea "
            f"completa). Por defecto {ARCHIVO_PAUSA_POR_DEFECTO} si existe."
        ),
    )
    args = parser.parse_args(argv)

    usuarios_pausa, origen_pausa, error_pausa = _resolver_pausa(
        args.pausar, args.pausar_archivo
    )
    if error_pausa:
        if args.como_json:
            print(json.dumps({"ok": False, "error": error_pausa}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {error_pausa}")
        return 1

    resultado = importar_ordenes(
        args.archivos or None,
        dry_run=not args.apply,
        usuarios_pausar=usuarios_pausa,
    )
    resultado["pausa_origen"] = origen_pausa

    if args.como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resultado, origen_pausa)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
