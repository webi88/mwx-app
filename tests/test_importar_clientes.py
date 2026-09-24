"""Tests rapidos de `importar_clientes.py` (sin red, sin BD real, sin Chrome).

`importar_clientes` importa `get_db_session` de forma perezosa dentro de la
funcion, asi que estos tests reemplazan `core.database.get_db_session` por una
sesion FALSA (mismo patron que `tests/test_restaurador.py`). TODOS los datos son
sinteticos.

Cubre:
  (1) `parsear_linea_cliente`: 4 campos, comentarios/vacias, campos faltantes,
      `email_password` vacio o con `:` y credenciales conservadas tal cual.
  (2) Dry-run: reporta sin escribir (nada en `db.add`).
  (3) `--apply`: crea con `plataforma="twitter"`, `activa=True`,
      `pausada_activacion=True` y sin cookies/totp.
  (4) Existentes: actualiza SOLO password/email/email_password y fuerza
      `pausada_activacion=True`; cookies/totp/seccion/tier/status/rol intactos.
  (5) Coincidencia case-insensitive del usuario.
  (6) Errores por linea/BD aislados, sin lanzar y con credenciales ocultas.
  (7) `main()` del CLI: dry-run por defecto, `--apply`, `--json` y salida 1 si
      la lista no se puede leer.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_importar_clientes.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import core.database  # noqa: E402
import importar_clientes  # noqa: E402
from importar_clientes import (  # noqa: E402
    CAMPOS_ACTUALIZABLES,
    _ocultar_credenciales,
    importar_clientes as importar,
    parsear_linea_cliente,
)

# --------------------------------------------------------------------------- #
# Fakes (nunca se conecta a una BD real)
# --------------------------------------------------------------------------- #
class _CuentaFake:
    """Cuenta existente con las columnas que el importador NO debe tocar."""

    def __init__(self, usuario, **extra):
        self.usuario = usuario
        self.password = extra.get("password", "pass_vieja")
        self.email = extra.get("email", "viejo@x.com")
        self.email_password = extra.get("email_password", "mailpass_vieja")
        self.pausada_activacion = extra.get("pausada_activacion", False)
        self.cookies_json = extra.get("cookies_json", [{"name": "auth_token"}])
        self.totp_secret = extra.get("totp_secret", "TOTPVIEJO")
        self.seccion = extra.get("seccion", "CI")
        self.tier_calidad = extra.get("tier_calidad", "tier1")
        self.rol_activacion = extra.get("rol_activacion", "cita")
        self.status = extra.get("status", "active")


class _FakeQuery:
    """Emula `db.query(Cuenta).filter(...).first()` y captura el filtro."""

    def __init__(self, db):
        self.db = db
        self.usuario_filtrado = None
        self.expresion = None

    def filter(self, *criterios, **kwargs):
        for criterio in criterios:
            self.expresion = criterio
            try:
                self.usuario_filtrado = str(criterio.right.value).lower()
            except Exception:
                self.usuario_filtrado = None
            break
        return self

    def first(self):
        if self.usuario_filtrado in self.db.error_usuarios:
            raise self.db.error_usuarios[self.usuario_filtrado]
        return self.db.existentes.get(self.usuario_filtrado)


class _FakeDB:
    """Sesion falsa: existentes por clave en minusculas + filas agregadas."""

    def __init__(self, existentes=None, error_usuarios=None):
        self.existentes = {str(u).lower(): c for u, c in (existentes or {}).items()}
        self.agregadas = []
        self.error_usuarios = dict(error_usuarios or {})
        self.commits = 0
        self.cerrada = False

    def query(self, modelo):
        return _FakeQuery(self)

    def add(self, objeto):
        self.agregadas.append(objeto)
        self.existentes[str(objeto.usuario).lower()] = objeto

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.cerrada = True


@contextlib.contextmanager
def _sesion_fake(db):
    try:
        yield db
        db.commit()
    finally:
        db.close()


@contextlib.contextmanager
def _con_db(db):
    """Inyecta la sesion falsa en el import perezoso de `core.database`."""
    with mock.patch.object(core.database, "get_db_session", lambda: _sesion_fake(db)):
        yield db


def _lista(tmp, contenido):
    """Escribe una lista temporal y devuelve su ruta."""
    ruta = Path(tmp) / "a_importar.txt"
    ruta.write_text(contenido, encoding="utf-8")
    return str(ruta)


LINEA_1 = "cliente_uno:Secreta-111:uno@yahoo.com:Secreta-111"
LINEA_2 = "cliente_dos:Secreta-222:dos@yahoo.com:Secreta-222"


# --------------------------------------------------------------------------- #
# (1) Parser puro
# --------------------------------------------------------------------------- #
def _test_parser(check):
    campos = parsear_linea_cliente(LINEA_1)
    check(
        "parser: 4 campos validos",
        campos == {
            "usuario": "cliente_uno",
            "password": "Secreta-111",
            "email": "uno@yahoo.com",
            "email_password": "Secreta-111",
        },
    )
    check(
        "parser: comentarios y lineas vacias -> None",
        parsear_linea_cliente("# comentario") is None
        and parsear_linea_cliente("") is None
        and parsear_linea_cliente("   ") is None
        and parsear_linea_cliente(None) is None,
    )
    check(
        "parser: 3 campos (sin email_password) -> None",
        parsear_linea_cliente("usuario:pass:correo@x.com") is None,
    )
    check(
        "parser: sin usuario/password/email -> None",
        parsear_linea_cliente(":pass:correo@x.com:mp") is None
        and parsear_linea_cliente("usuario::correo@x.com:mp") is None
        and parsear_linea_cliente("usuario:pass::mp") is None,
    )
    check(
        "parser: email_password vacio es valido",
        parsear_linea_cliente("usuario:pass:correo@x.com:")["email_password"] == "",
    )
    check(
        "parser: email_password con ':' se conserva entero",
        parsear_linea_cliente("usuario:pass:correo@x.com:mp:con:dos")["email_password"]
        == "mp:con:dos",
    )
    check(
        "parser: password con caracteres raros no se recorta",
        parsear_linea_cliente("usuario:p#a ss$1:correo@x.com:mp")["password"]
        == "p#a ss$1",
    )
    check(
        "parser: usuario/email se recortan de espacios",
        parsear_linea_cliente("  cliente  :pass:  correo@x.com  :mp")["usuario"] == "cliente"
        and parsear_linea_cliente("  cliente  :pass:  correo@x.com  :mp")["email"]
        == "correo@x.com",
    )
    check(
        "parser: CAMPOS_ACTUALIZABLES son los 3 campos permitidos",
        CAMPOS_ACTUALIZABLES == ("password", "email", "email_password"),
    )


# --------------------------------------------------------------------------- #
# (2) Dry-run y (3) creacion real
# --------------------------------------------------------------------------- #
def _test_dry_run_y_creacion(check):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n" + LINEA_2 + "\n")

        db = _FakeDB()
        with _con_db(db):
            resultado = importar(ruta, dry_run=True)
        check("dry-run: reporta ok y 2 creadas", resultado["ok"] and resultado["creadas"] == 2)
        check("dry-run: no escribe NADA en la BD", db.agregadas == [] and db.commits == 1)
        check(
            "dry-run: las cuentas reportadas no traen credenciales",
            "Secreta-111" not in json.dumps(resultado)
            and resultado["cuentas"][0]["usuario"] == "cliente_uno",
        )
        check("dry-run: total de lineas validas", resultado["total"] == 2)

        db = _FakeDB()
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)
        check("apply: creadas 2 y ok", resultado["ok"] and resultado["creadas"] == 2)
        check("apply: agrega las 2 cuentas a la sesion", len(db.agregadas) == 2)

        nueva = db.agregadas[0]
        check(
            "apply: nueva con plataforma twitter, activa y pausada",
            nueva.plataforma == "twitter"
            and nueva.activa is True
            and nueva.pausada_activacion is True,
        )
        check(
            "apply: nueva con credenciales del archivo y status imported",
            nueva.usuario == "cliente_uno"
            and nueva.password == "Secreta-111"
            and nueva.email == "uno@yahoo.com"
            and nueva.email_password == "Secreta-111"
            and nueva.status == "imported",
        )
        check(
            "apply: la cuenta nueva no hereda cookies ni totp",
            getattr(nueva, "cookies_json", None) in (None,)
            and str(getattr(nueva, "totp_secret", "") or "") == "",
        )


# --------------------------------------------------------------------------- #
# (4) Actualizacion de existentes y (5) case-insensitive
# --------------------------------------------------------------------------- #
def _test_actualizacion(check):
    existente = _CuentaFake("cliente_uno")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n")
        db = _FakeDB(existentes={"cliente_uno": existente})
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)

    check("actualizar: 1 actualizada y 0 creadas", resultado["actualizadas"] == 1 and resultado["creadas"] == 0)
    check(
        "actualizar: pisa SOLO password/email/email_password",
        existente.password == "Secreta-111"
        and existente.email == "uno@yahoo.com"
        and existente.email_password == "Secreta-111",
    )
    check("actualizar: fuerza pausada_activacion=True", existente.pausada_activacion is True)
    check(
        "actualizar: NO toca cookies/totp/seccion/tier/rol/status",
        existente.cookies_json == [{"name": "auth_token"}]
        and existente.totp_secret == "TOTPVIEJO"
        and existente.seccion == "CI"
        and existente.tier_calidad == "tier1"
        and existente.rol_activacion == "cita"
        and existente.status == "active",
    )
    check(
        "actualizar: reporta los campos tocados sin credenciales",
        resultado["cuentas"][0]["campos_actualizados"]
        == ["password", "email", "email_password", "pausada_activacion"]
        and "Secreta-111" not in json.dumps(resultado),
    )
    check("actualizar: la fila existente no se re-agrega", db.agregadas == [])

    # Coincidencia case-insensitive: la lista trae "CLIENTE_UNO" y la BD
    # "cliente_uno"; el filtro usa lower() y no debe crear una duplicada.
    existente = _CuentaFake("cliente_uno")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, "CLIENTE_UNO:Secreta-111:uno@yahoo.com:Secreta-111\n")
        db = _FakeDB(existentes={"CLIENTE_UNO": existente})
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)
    check(
        "actualizar: coincidencia case-insensitive (sin duplicar)",
        resultado["actualizadas"] == 1
        and resultado["creadas"] == 0
        and db.agregadas == []
        and existente.pausada_activacion is True,
    )

    # Mezcla: una existente + una nueva.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n" + LINEA_2 + "\n")
        db = _FakeDB(existentes={"cliente_dos": _CuentaFake("cliente_dos")})
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)
    check(
        "mezcla: 1 creada + 1 actualizada",
        resultado["creadas"] == 1 and resultado["actualizadas"] == 1 and resultado["total"] == 2,
    )


# --------------------------------------------------------------------------- #
# (6) Errores aislados
# --------------------------------------------------------------------------- #
def _test_errores(check):
    check(
        "seguridad: _ocultar_credenciales tapa los secretos",
        "***" in _ocultar_credenciales("fallo con Secreta-111 y mp-222", "Secreta-111", "mp-222")
        and "Secreta-111" not in _ocultar_credenciales("fallo con Secreta-111", "Secreta-111"),
    )

    # Linea malformada junto a validas: se procesan las buenas, ok=False.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, "linea_mala_sin_campos\n" + LINEA_1 + "\n")
        db = _FakeDB()
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)
    check(
        "errores: linea malformada se reporta sin romper el lote",
        resultado["errores"] == 1 and resultado["creadas"] == 1 and resultado["ok"] is False,
    )
    check(
        "errores: detalle identifica la linea (sin credenciales)",
        "Linea 1" in resultado["detalle_errores"][0],
    )

    # Solo lineas malformadas: no hay nada que importar.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, "basura\n# comentario\n")
        with _con_db(_FakeDB()):
            resultado = importar(ruta)
    check(
        "errores: sin lineas validas devuelve error claro",
        resultado["total"] == 0 and "no habia lineas validas" in resultado["error"],
    )

    # Archivo inexistente.
    with _con_db(_FakeDB()):
        resultado = importar(str(Path(tempfile.gettempdir()) / "no_existe_xyz.txt"))
    check(
        "errores: archivo inexistente no lanza y explica",
        resultado["ok"] is False and "no se pudo leer" in resultado["error"],
    )

    # Error de BD en una cuenta: las demas siguen y el mensaje se sanea.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n" + LINEA_2 + "\n")
        db = _FakeDB(error_usuarios={"cliente_uno": RuntimeError("fallo con Secreta-111")})
        with _con_db(db):
            resultado = importar(ruta, dry_run=False)
    check(
        "errores: fallo por cuenta se aisla (la otra se procesa)",
        resultado["errores"] == 1 and resultado["creadas"] == 1 and resultado["ok"] is False,
    )
    check(
        "errores: el error de la cuenta no expone la contrasena",
        "Secreta-111" not in json.dumps(resultado)
        and "***" in resultado["detalle_errores"][0],
    )

    # Fallo global de BD (get_db_session no se puede abrir).
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n")
        with mock.patch.object(
            core.database, "get_db_session", side_effect=RuntimeError("sin BD")
        ):
            resultado = importar(ruta, dry_run=False)
    check(
        "errores: fallo global de BD produce ok=False sin lanzar",
        resultado["ok"] is False and "no se pudo abrir/escribir" in resultado["error"],
    )


# --------------------------------------------------------------------------- #
# (7) CLI
# --------------------------------------------------------------------------- #
def _test_cli(check):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _lista(tmp, LINEA_1 + "\n")

        # Dry-run por defecto + --json.
        db = _FakeDB()
        salida = io.StringIO()
        with _con_db(db), contextlib.redirect_stdout(salida):
            codigo = importar_clientes.main([ruta, "--json"])
        datos = json.loads(salida.getvalue())
        check(
            "cli: --json sin --apply es dry-run y sale 0",
            codigo == 0 and datos["dry_run"] is True and datos["creadas"] == 1,
        )
        check("cli: dry-run no escribio en la BD", db.agregadas == [])

        # --apply.
        db = _FakeDB()
        salida = io.StringIO()
        with _con_db(db), contextlib.redirect_stdout(salida):
            codigo = importar_clientes.main([ruta, "--apply"])
        check(
            "cli: --apply crea de verdad y sale 0",
            codigo == 0 and len(db.agregadas) == 1 and db.agregadas[0].pausada_activacion is True,
        )
        check(
            "cli: la salida legible no imprime la contrasena",
            "Secreta-111" not in salida.getvalue(),
        )

        # Lista inexistente: codigo de salida 1.
        salida = io.StringIO()
        with _con_db(_FakeDB()), contextlib.redirect_stdout(salida):
            codigo = importar_clientes.main([str(Path(tmp) / "nada.txt")])
        check(
            "cli: lista inexistente sale 1 con error",
            codigo == 1 and "no se pudo leer" in salida.getvalue(),
        )


def run(check):
    """Ejecuta todos los checks del importador de clientes."""
    _test_parser(check)
    _test_dry_run_y_creacion(check)
    _test_actualizacion(check)
    _test_errores(check)
    _test_cli(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_importar_clientes.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
