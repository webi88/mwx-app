"""
Importacion de cuentas de CLIENTE (pausadas para activacion masiva).

Lee un archivo con UNA cuenta por linea y el formato:

    usuario:password:email:email_password

y hace UPSERT por `Cuenta.usuario` (case-insensitive):

* Si la cuenta NO existe: la crea en la plataforma `twitter` con
  `activa=True` y `pausada_activacion=True`.
* Si YA existe: actualiza SOLO `password`, `email` y `email_password`, y
  fuerza `pausada_activacion=True`. NUNCA pisa cookies, totp, seccion, tier,
  status, rol ni ninguna otra columna (esas cuentas pueden venir de la flota
  interna y solo cambian de dueno).

En ambos casos la cuenta queda PAUSADA para activacion masiva (campanas, RTs,
likes, reparto por hora y tareas de activacion) pero DISPONIBLE para
mantenimiento (calentamiento, publicaciones manuales y el bot de clientes).
Ver `core/pausas.py`.

Por defecto corre en SIMULACION (dry-run): solo consulta y reporta que pasaria,
sin escribir nada. La importacion real requiere `--apply`.

La operacion apunta a la base de datos de `DATABASE_URL` (por defecto la local
`data/gestor_redes.db`). Para importar en Supabase, exporta la DATABASE_URL de
Railway antes de correr (los valores reales no van en el repo):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python importar_clientes.py --apply

Uso:
    python importar_clientes.py                       # dry-run del default
    python importar_clientes.py --apply               # importacion real
    python importar_clientes.py otra_lista.txt --apply
    python importar_clientes.py --json                # resumen en JSON

AVISO: el archivo de cuentas es SENSIBLE (trae credenciales). Vive en
`data/clientes/` (ignorado por git) y este script NUNCA imprime contrasenas:
solo usuario, correo y que campos se actualizaron.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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
ARCHIVO_POR_DEFECTO = "data/clientes/a_importar.txt"

# Campos que la importacion de clientes SI puede pisar en una cuenta existente.
CAMPOS_ACTUALIZABLES = ("password", "email", "email_password")


def _ocultar_credenciales(mensaje, *secretos) -> str:
    """Reemplaza cualquier credencial conocida por `***` en un mensaje."""
    texto = str(mensaje or "")
    for secreto in secretos:
        if secreto:
            texto = texto.replace(str(secreto), "***")
    return " ".join(texto.split())[:200]


def parsear_linea_cliente(linea: str) -> Optional[dict]:
    """Convierte `usuario:password:email:email_password` en un dict.

    Devuelve `None` si la linea esta vacia, es un comentario (`#`) o no tiene
    los 4 campos con usuario/password/email no vacios. El `email_password`
    puede venir vacio. El `password` y el `email_password` NO se recortan
    (podrian tener caracteres significativos); usuario y email si.
    """
    texto = (linea or "").strip()
    if not texto or texto.startswith("#"):
        return None
    partes = texto.split(":", 3)
    if len(partes) != 4:
        return None
    usuario = partes[0].strip()
    password = partes[1]
    email = partes[2].strip()
    email_password = partes[3]
    if not usuario or not password or not email:
        return None
    return {
        "usuario": usuario,
        "password": password,
        "email": email,
        "email_password": email_password,
    }


def leer_archivo(ruta) -> Optional[str]:
    """Lee la lista de cuentas (texto crudo). None si no se pudo leer."""
    try:
        with Path(ruta).open("r", encoding="utf-8", errors="replace") as manejador:
            return manejador.read()
    except Exception:
        return None


def importar_clientes(ruta=None, dry_run: bool = True) -> dict:
    """Importa/actualiza las cuentas de cliente y las deja pausadas.

    Devuelve un resumen con `ok`, `dry_run`, `archivo`, `total`, `creadas`,
    `actualizadas`, `errores`, `detalle_errores`, `cuentas` (usuario, email y
    accion; NUNCA credenciales) y `error` (fallo global). Nunca lanza.

    Con `dry_run=True` solo se consulta la BD y se reporta que pasaria; con
    `dry_run=False` los cambios se confirman en UNA transaccion (los errores
    por cuenta se aislan y se reportan). El import de `core.database` es
    perezoso para que el script no conecte al cargarse.
    """
    archivo = str(ruta or resolver_ruta(ARCHIVO_POR_DEFECTO))
    resultado = {
        "ok": False,
        "dry_run": bool(dry_run),
        "archivo": archivo,
        "total": 0,
        "creadas": 0,
        "actualizadas": 0,
        "errores": 0,
        "detalle_errores": [],
        "cuentas": [],
        "error": "",
    }

    crudo = leer_archivo(archivo)
    if crudo is None:
        resultado["error"] = f"no se pudo leer la lista '{archivo}'."
        return resultado

    registros = []
    for numero, linea in enumerate(crudo.splitlines(), start=1):
        fields = parsear_linea_cliente(linea)
        if fields is None:
            if linea.strip() and not linea.strip().startswith("#"):
                resultado["errores"] += 1
                resultado["detalle_errores"].append(
                    f"Linea {numero}: malformada (se esperan 4 campos "
                    "usuario:password:email:email_password)."
                )
            continue
        registros.append(fields)
    resultado["total"] = len(registros)

    if not registros:
        if not resultado["error"] and resultado["errores"]:
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
                usuario = fields["usuario"]
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
                                    password=fields["password"],
                                    email=fields["email"],
                                    email_password=fields["email_password"],
                                    plataforma="twitter",
                                    activa=True,
                                    pausada_activacion=True,
                                    status="imported",
                                    fecha_creacion=datetime.utcnow(),
                                )
                            )
                        resultado["creadas"] += 1
                        accion = "crear"
                    else:
                        if not dry_run:
                            existente.password = fields["password"]
                            existente.email = fields["email"]
                            existente.email_password = fields["email_password"]
                            existente.pausada_activacion = True
                        resultado["actualizadas"] += 1
                        accion = "actualizar"
                    campos_actualizados = []
                    if existente is not None:
                        campos_actualizados = list(CAMPOS_ACTUALIZABLES) + [
                            "pausada_activacion"
                        ]
                    resultado["cuentas"].append(
                        {
                            "usuario": usuario,
                            "email": fields["email"],
                            "accion": accion,
                            "existia": existente is not None,
                            "campos_actualizados": campos_actualizados,
                        }
                    )
                except Exception as error:
                    resultado["errores"] += 1
                    resultado["detalle_errores"].append(
                        f"{usuario}: {type(error).__name__}: "
                        f"{_ocultar_credenciales(error, fields['password'], fields['email_password'])}"
                    )
            resultado["ok"] = resultado["errores"] == 0
    except Exception as error:
        resultado["error"] = (
            f"no se pudo abrir/escribir la base de datos "
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


def _imprimir_resumen(resultado: dict, origen: str) -> None:
    """Resumen legible para el operador (nunca imprime credenciales)."""
    modo = "SIMULACION (dry-run, no escribe)" if resultado.get("dry_run") else "REAL (escribe en la BD)"
    print("=" * 62)
    print("Importacion de cuentas de CLIENTE (pausadas)")
    print("=" * 62)
    print(f"Lista     : {origen}")
    print(f"BD destino: {_descripcion_destino()}")
    print(f"Modo      : {modo}")
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
            print(f"  [{etiqueta}] {fila.get('usuario')} <{fila.get('email')}>")
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
        "Las cuentas quedaron con pausada_activacion=True: fuera de campanas, "
        "pero disponibles para mantenimiento y el bot de clientes."
    )
    print(
        "AVISO: la operacion apunta a la BD de DATABASE_URL (por defecto la "
        "local data/gestor_redes.db). Para Supabase, exporta la DATABASE_URL "
        "de Railway antes de correr."
    )


def main(argv=None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = argparse.ArgumentParser(
        description=(
            "Importa cuentas de CLIENTE (usuario:password:email:email_password) "
            "y las deja pausadas para activacion masiva. Por defecto es un "
            "DRY-RUN: no escribe nada hasta que se pasa --apply."
        ),
        epilog=(
            "SEGURIDAD: no imprime contrasenas. El archivo de cuentas vive en "
            "data/clientes/ (ignorado por git). Para Supabase, exporta la "
            "DATABASE_URL de Railway antes de correr: la importacion escribe en "
            "ESA base de datos."
        ),
    )
    parser.add_argument(
        "archivo",
        nargs="?",
        default=None,
        help=f"Lista de cuentas. Si se omite, usa {ARCHIVO_POR_DEFECTO}.",
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
    args = parser.parse_args(argv)

    ruta = args.archivo or resolver_ruta(ARCHIVO_POR_DEFECTO)
    resultado = importar_clientes(ruta, dry_run=not args.apply)

    if args.como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resultado, ruta)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
