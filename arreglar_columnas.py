"""
Reparacion de columnas DESPLAZADAS en la tabla `cuentas`.

Caso tipico (lotes de proveedor con las columnas corridas): los datos
quedaron guardados en la columna equivocada de la base de datos:

    * `Cuenta.auth_token`     -> TOTP real (16 caracteres, mayusculas)
    * `Cuenta.totp_secret`    -> EMAIL real (contiene '@')
    * `Cuenta.email_password` -> AUTH TOKEN real (hex de 40)

Este script recorre las cuentas y SOLO cuando se cumple esa firma EXACTA
(`auth_token` de 16 caracteres en mayusculas, sin espacios, y `totp_secret`
con '@') reacomoda los 4 valores:

    real_totp       = cuenta.auth_token      -> cuenta.totp_secret
    real_auth_token = cuenta.email_password  -> cuenta.auth_token
    real_email      = cuenta.totp_secret     -> cuenta.email
    cuenta.email_password = ""               (se uso como puente)

Se hace UN commit por cada cuenta reparada (nunca un commit masivo al final) y
NO se tocan otras columnas: `tier_calidad`, `seccion`, `avatar_path`,
`banner_path`, `rol_activacion`, `status`, `cookies_json`, `password`, etc.
quedan intactas. Las cuentas con campos vacios/None o con otra forma no
cumplen la condicion y no se tocan (el script nunca lanza).

Por defecto APLICA los cambios (pedido del dueno). Para solo mirar:

    python arreglar_columnas.py --dry-run
    python arreglar_columnas.py --json

La operacion apunta a la base de datos de `DATABASE_URL` (por defecto la
local `data/gestor_redes.db`). Para reparar en Supabase, exporta la
DATABASE_URL de Railway antes de correr (los valores reales no van en el repo):

    DATABASE_URL="postgresql://usuario:password@host:5432/postgres" \
        python arreglar_columnas.py

AVISO: el script NUNCA imprime el TOTP ni los auth tokens; el email
restaurado si se puede mostrar.
"""

import argparse
import json
import sys
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

# Largo exacto del TOTP real que quedo guardado en `auth_token`.
LARGO_TOTP_MAYUS = 16


def es_patron_desplazado(auth_token, totp_secret) -> bool:
    """True si los dos campos tienen la forma del lote con columnas corridas.

    Condicion EXACTA pedida: `auth_token` con 16 caracteres en mayusculas y
    sin espacios (el TOTP real), y `totp_secret` con '@' (el email real).
    Campos vacios/None, de otro largo, en minusculas o con espacios no
    cumplen y esa cuenta se deja intacta. Nunca lanza."""
    try:
        token = auth_token if isinstance(auth_token, str) else ""
        correo = totp_secret if isinstance(totp_secret, str) else ""
        if len(token) != LARGO_TOTP_MAYUS or not token.isupper():
            return False
        if any(caracter.isspace() for caracter in token):
            return False
        return "@" in correo
    except Exception:
        return False


def _leer_campo(cuenta, nombre) -> str:
    """Lee un campo de la cuenta devolviendo "" si no existe o no se puede leer."""
    try:
        return str(getattr(cuenta, nombre, "") or "")
    except Exception:
        return ""


def _ocultar_credenciales(mensaje, *secretos) -> str:
    """Reemplaza cualquier credencial conocida por `***` en un mensaje."""
    texto = str(mensaje or "")
    for secreto in secretos:
        if secreto:
            texto = texto.replace(str(secreto), "***")
    return " ".join(texto.split())[:200]


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


def arreglar_columnas(dry_run: bool = False) -> dict:
    """Reacomoda las columnas desplazadas y devuelve el resumen.

    Devuelve `ok`, `dry_run`, `revisadas`, `reparadas`, `usuarios`, `detalle`
    (usuario/email restaurado; NUNCA TOTP ni tokens), `errores`,
    `detalle_errores` y `error`. Nunca lanza.

    Con `dry_run=True` no se escribe ni se hace commit: `reparadas` es el
    conteo de cuentas que SE repararian. Con `dry_run=False` se hace UN commit
    por cada cuenta reparada. El import de `core.database` es perezoso para
    que el script no conecte al cargarse.
    """
    resultado = {
        "ok": False,
        "dry_run": bool(dry_run),
        "revisadas": 0,
        "reparadas": 0,
        "usuarios": [],
        "detalle": [],
        "errores": 0,
        "detalle_errores": [],
        "error": "",
    }

    # Import perezoso: el script no importa core.database (ni conecta) al cargarse.
    from core.database import get_db_session
    from core.models import Cuenta

    try:
        with get_db_session() as db:
            cuentas = db.query(Cuenta).order_by(Cuenta.id).all()
            for cuenta in cuentas:
                resultado["revisadas"] += 1
                try:
                    if not es_patron_desplazado(
                        getattr(cuenta, "auth_token", ""),
                        getattr(cuenta, "totp_secret", ""),
                    ):
                        continue

                    usuario = str(getattr(cuenta, "usuario", "") or "")
                    real_totp = cuenta.auth_token
                    real_auth_token = cuenta.email_password
                    real_email = cuenta.totp_secret

                    if dry_run:
                        # Solo reporta: no escribe ni hace commit.
                        resultado["reparadas"] += 1
                        resultado["usuarios"].append(usuario)
                        resultado["detalle"].append(
                            {"usuario": usuario, "email": str(real_email or "")}
                        )
                        continue

                    # SOLO se tocan estos 4 campos (los demas quedan intactos).
                    cuenta.totp_secret = real_totp
                    cuenta.auth_token = real_auth_token
                    cuenta.email = real_email
                    cuenta.email_password = ""
                    # Un commit por cuenta reparada (no un commit masivo al final).
                    db.commit()

                    resultado["reparadas"] += 1
                    resultado["usuarios"].append(usuario)
                    resultado["detalle"].append(
                        {"usuario": usuario, "email": str(real_email or "")}
                    )
                except Exception as error:
                    resultado["errores"] += 1
                    resultado["detalle_errores"].append(
                        _ocultar_credenciales(
                            f"{type(error).__name__}: {error}",
                            _leer_campo(cuenta, "auth_token"),
                            _leer_campo(cuenta, "email_password"),
                            _leer_campo(cuenta, "totp_secret"),
                        )
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
            resultado["ok"] = resultado["errores"] == 0
    except Exception as error:
        resultado["error"] = (
            "no se pudo abrir/recorrer la base de datos "
            f"({_ocultar_credenciales(f'{type(error).__name__}: {error}')})."
        )
        resultado["ok"] = False
    return resultado


def _imprimir_resumen(resultado: dict) -> None:
    """Resumen legible para el operador (nunca imprime TOTP ni tokens)."""
    modo = (
        "SIMULACION (dry-run, no escribe)"
        if resultado.get("dry_run")
        else "REAL (escribe en la BD)"
    )
    print("=" * 62)
    print("Reparacion de columnas desplazadas (auth_token/totp/email)")
    print("=" * 62)
    print(f"BD destino: {_descripcion_destino()}")
    print(f"Modo      : {modo}")
    if resultado.get("error"):
        print("ESTADO    : con errores (revisa el detalle)")
    print("-" * 62)

    for fila in list(resultado.get("detalle") or []):
        usuario = fila.get("usuario") or "(sin usuario)"
        email = str(fila.get("email") or "")
        sufijo = f" (email: {email})" if email else ""
        if resultado.get("dry_run"):
            print(f"  @{usuario}: se reacomodarian los campos{sufijo}")
        else:
            print(f"  @{usuario}: campos reacomodados{sufijo}")

    errores = list(resultado.get("detalle_errores") or [])
    if errores:
        print("-" * 62)
        print("Primeros errores:")
        for mensaje in errores[:5]:
            print(f"  - {mensaje}")
        if len(errores) > 5:
            print(f"  ... y {len(errores) - 5} mas")

    if resultado.get("error"):
        print("-" * 62)
        print(f"ERROR: {resultado['error']}")

    print("-" * 62)
    reparadas = int(resultado.get("reparadas") or 0)
    revisadas = int(resultado.get("revisadas") or 0)
    if resultado.get("dry_run"):
        print(f"Cuentas a reparar: {reparadas} de {revisadas} revisadas (dry-run).")
        print("AVISO: simulacion sin cambios; para aplicar corre sin --dry-run.")
    else:
        print(f"Cuentas reparadas: {reparadas} de {revisadas} revisadas.")
        print("Cambios aplicados (un commit por cuenta reparada).")
    print(
        "AVISO: la operacion apunta a la BD de DATABASE_URL (por defecto la "
        "local data/gestor_redes.db). Para Supabase, exporta la DATABASE_URL "
        "de Railway antes de correr."
    )


def main(argv=None) -> int:
    """Punto de entrada CLI. Devuelve el codigo de salida (0/1)."""
    parser = argparse.ArgumentParser(
        description=(
            "Reacomoda en la BD las columnas desplazadas por un proveedor "
            "(auth_token=TOTP real, totp_secret=email real, "
            "email_password=auth token real). Por defecto APLICA los cambios; "
            "usa --dry-run para solo mirar. Solo toca esos 4 campos."
        ),
        epilog=(
            "SEGURIDAD: no imprime el TOTP ni los auth tokens (el email "
            "restaurado si se puede mostrar). Para Supabase, exporta la "
            "DATABASE_URL de Railway antes de correr: la reparacion escribe en "
            "ESA base de datos."
        ),
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Muestra que cuentas se repararian SIN escribir ni hacer commit.",
    )
    parser.add_argument(
        "--json",
        dest="como_json",
        action="store_true",
        help="Imprime solo el resumen en JSON (util para automatizar).",
    )
    args = parser.parse_args(argv)

    resultado = arreglar_columnas(dry_run=args.dry_run)

    if args.como_json:
        print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    else:
        _imprimir_resumen(resultado)

    return 0 if resultado.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
