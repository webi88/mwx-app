"""Tests rapidos de `importar_ordenes.py` (sin red, sin BD real y sin Chrome).

`importar_ordenes` importa `get_db_session` de forma perezosa dentro de la
funcion, asi que estos tests reemplazan `core.database.get_db_session` por una
sesion FALSA (mismo patron que `tests/test_importar_clientes.py`). TODOS los
datos son sinteticos.

Cubre:
  (1) `CAMPOS_ACTUALIZABLES` y `_ocultar_credenciales`.
  (2) `parsear_usuarios_pausa`: usuario, `@usuario`, linea completa, comentarios,
      vacias y dedupe case-insensitive.
  (3) Creacion via ordenes 5 campos SIN TOTP (caso A: `totp_secret` vacio y el
      `email_password` en su campo) y 6 campos con TOTP al final (caso B).
  (4) Dry-run: reporta sin escribir; el resumen nunca trae credenciales.
  (5) `--apply`/`usuarios_pausar`: altas con plataforma/activa/status y pausa
      solo para los usuarios pedidos.
  (6) Actualizacion de existentes: pisa SOLO campos NO vacios del lote y NUNCA
      cookies/seccion/tier/rol/status/pausa; coincidencia case-insensitive.
  (7) Glob por defecto `data/clientes/order*.txt` (con carpeta parcheada),
      archivo ilegible, linea malformada, error por cuenta saneado y fallo
      global de BD.
  (8) `_resolver_pausa`: union de `--pausar` + archivo, dedupe y error si el
      archivo explicito no se puede leer.
  (9) `main()` del CLI: dry-run por defecto, `--apply`, `--json`, `--pausar`,
      `--pausar-archivo` inexistente (salida 1) y salida sin credenciales.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_importar_ordenes.py   (solo este archivo)
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
import importar_ordenes  # noqa: E402
from importar_ordenes import (  # noqa: E402
    ARCHIVO_PAUSA_POR_DEFECTO,
    CAMPOS_ACTUALIZABLES,
    _ocultar_credenciales,
    _resolver_pausa,
    importar_ordenes as importar,
    parsear_usuarios_pausa,
)

# --------------------------------------------------------------------------- #
# Datos SINTETICOS (nunca credenciales reales)
# --------------------------------------------------------------------------- #
TOKEN = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"  # 40 hex minuscula
TOTP = "KRUGS4ZANFZSAYJA"  # 16 base32 mayuscula
LINEA_5 = f"cli_uno:Clave-1:uno@x.com:MailPass-1:{TOKEN}"
LINEA_6 = f"cli_dos:Clave-2:dos@x.com:MailPass-2:{TOKEN}:{TOTP}"
LINEA_5_MAYUS = f"CLI_UNO:Clave-1:uno@x.com:MailPass-1:{TOKEN}"


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
        self.auth_token = extra.get("auth_token", "tok_viejo")
        self.totp_secret = extra.get("totp_secret", "TOTP-VIEJO")
        self.pausada_activacion = extra.get("pausada_activacion", False)
        self.cookies_json = extra.get("cookies_json", [{"name": "auth_token"}])
        self.seccion = extra.get("seccion", "CI")
        self.tier_calidad = extra.get("tier_calidad", "tier1")
        self.rol_activacion = extra.get("rol_activacion", "cita")
        self.status = extra.get("status", "active")
        self.activa = extra.get("activa", True)
        self.avatar_path = extra.get("avatar_path", "data/avatares/viejo.png")
        self.banner_path = extra.get("banner_path", "data/portadas/vieja.png")


class _FakeQuery:
    """Emula `db.query(Cuenta).filter(...).first()` y captura el filtro."""

    def __init__(self, db):
        self.db = db
        self.usuario_filtrado = None

    def filter(self, *criterios, **kwargs):
        for criterio in criterios:
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


def _archivo(tmp, contenido, nombre="orden.txt"):
    """Escribe un archivo temporal de orden y devuelve su ruta."""
    ruta = Path(tmp) / nombre
    ruta.write_text(contenido, encoding="utf-8")
    return str(ruta)


def _sin_credenciales(datos) -> bool:
    """True si el resumen serializado no expone ningun secreto de prueba."""
    texto = json.dumps(datos, ensure_ascii=False)
    return all(secreto not in texto for secreto in (TOKEN, TOTP, "Clave-1", "Clave-2", "MailPass-1", "MailPass-2"))


# --------------------------------------------------------------------------- #
# (1) Constantes y saneado
# --------------------------------------------------------------------------- #
def _test_constantes(check):
    check(
        "constantes: CAMPOS_ACTUALIZABLES exactos",
        CAMPOS_ACTUALIZABLES
        == ("password", "email", "email_password", "auth_token", "totp_secret"),
    )
    check(
        "constantes: archivo de pausa por defecto",
        ARCHIVO_PAUSA_POR_DEFECTO == "data/clientes/a_importar.txt",
    )
    check(
        "seguridad: _ocultar_credenciales tapa los secretos",
        "***" in _ocultar_credenciales(f"fallo con {TOKEN} y Clave-1", TOKEN, "Clave-1")
        and TOKEN not in _ocultar_credenciales(f"fallo con {TOKEN}", TOKEN),
    )


# --------------------------------------------------------------------------- #
# (2) Parser de la lista de pausa
# --------------------------------------------------------------------------- #
def _test_parser_pausa(check):
    lista = parsear_usuarios_pausa(
        "cuenta_uno\n"
        "@Cuenta_Dos\n"
        "cuenta_tres:pass:correo@x.com:mp\n"
        "CUENTA_UNO\n"          # duplicado case-insensitive
        "# comentario\n"
        "\n"
        "   \n"
    )
    check(
        "pausa: usuario/@usuario/linea completa y dedupe case-insensitive",
        lista == ["cuenta_uno", "Cuenta_Dos", "cuenta_tres"],
    )
    check("pausa: vacio/Ninguno -> lista vacia", parsear_usuarios_pausa("") == [] and parsear_usuarios_pausa(None) == [])


# --------------------------------------------------------------------------- #
# (3-4) Creacion (caso A/B) y dry-run
# --------------------------------------------------------------------------- #
def _test_creacion(check):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n" + LINEA_6 + "\n")

        db = _FakeDB()
        with _con_db(db):
            resultado = importar([ruta], dry_run=True)
        check(
            "dry-run: ok, 2 creadas y nada escrito",
            resultado["ok"] and resultado["creadas"] == 2 and db.agregadas == [],
        )
        check("dry-run: total 2 y 0 errores", resultado["total"] == 2 and resultado["errores"] == 0)
        check(
            "dry-run: el resumen no trae credenciales",
            _sin_credenciales(resultado)
            and resultado["cuentas"][0]["usuario"] == "cli_uno"
            and resultado["cuentas"][0]["accion"] == "crear",
        )

        db = _FakeDB()
        with _con_db(db):
            resultado = importar([ruta], dry_run=False)
        check("apply: 2 creadas y ok", resultado["ok"] and resultado["creadas"] == 2)
        check("apply: agrega las 2 cuentas", len(db.agregadas) == 2)
        nueva5, nueva6 = db.agregadas
        check(
            "apply caso A (5 campos SIN TOTP): email_password correcto y totp VACIO",
            nueva5.email_password == "MailPass-1"
            and nueva5.totp_secret == ""
            and nueva5.auth_token == TOKEN,
        )
        check(
            "apply caso B (6 campos TOTP final): todos los campos",
            nueva6.email_password == "MailPass-2"
            and nueva6.totp_secret == TOTP
            and nueva6.auth_token == TOKEN
            and nueva6.email == "dos@x.com",
        )
        check(
            "apply: nuevas con plataforma twitter/activa/status y SIN pausa",
            nueva5.plataforma == "twitter"
            and nueva5.activa is True
            and nueva5.status == "imported"
            and nueva5.pausada_activacion is False
            and nueva6.pausada_activacion is False,
        )
        check(
            "apply: reporte por cuenta sin credenciales y campos listados",
            _sin_credenciales(resultado)
            and resultado["cuentas"][0]["campos_actualizados"]
            == ["password", "email", "email_password", "auth_token"],
        )


# --------------------------------------------------------------------------- #
# (5) Pausa en altas
# --------------------------------------------------------------------------- #
def _test_pausa_altas(check):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n" + LINEA_6 + "\n")
        db = _FakeDB()
        with _con_db(db):
            resultado = importar(
                [ruta], dry_run=False, usuarios_pausar=["CLI_UNO", "@cli_uno"]
            )
        check(
            "pausa altas: solo el usuario pedido queda pausado",
            db.agregadas[0].pausada_activacion is True
            and db.agregadas[1].pausada_activacion is False,
        )
        check(
            "pausa altas: el resumen lista la pausa y marca la cuenta",
            resultado["pausa"] == ["cli_uno"]
            and resultado["cuentas"][0]["pausada"] is True
            and resultado["cuentas"][1]["pausada"] is False,
        )
        check("pausa altas: sin credenciales en el resumen", _sin_credenciales(resultado))


# --------------------------------------------------------------------------- #
# (6) Actualizacion de existentes y no-pisado
# --------------------------------------------------------------------------- #
def _test_actualizacion(check):
    existente = _CuentaFake("cli_uno")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n")
        db = _FakeDB(existentes={"cli_uno": existente})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False)

    check(
        "actualizar: 1 actualizada, 0 creadas y no re-agrega",
        resultado["actualizadas"] == 1 and resultado["creadas"] == 0 and db.agregadas == [],
    )
    check(
        "actualizar: pisa password/email/email_password/auth_token",
        existente.password == "Clave-1"
        and existente.email == "uno@x.com"
        and existente.email_password == "MailPass-1"
        and existente.auth_token == TOKEN,
    )
    check(
        "actualizar: NO pisa totp_secret (el lote sin TOTP no lo trae)",
        existente.totp_secret == "TOTP-VIEJO",
    )
    check(
        "actualizar: NO toca cookies/seccion/tier/rol/status/pausa",
        existente.cookies_json == [{"name": "auth_token"}]
        and existente.seccion == "CI"
        and existente.tier_calidad == "tier1"
        and existente.rol_activacion == "cita"
        and existente.status == "active"
        and existente.pausada_activacion is False,
    )
    check(
        "actualizar: campos reportados (sin pausa si no cambia)",
        resultado["cuentas"][0]["campos_actualizados"]
        == ["password", "email", "email_password", "auth_token"],
    )

    # Campos vacios del lote NO pisan lo existente (solo el email no vacio).
    existente2 = _CuentaFake("cli_dos")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, "cli_dos::nuevo@x.com::\n")
        db = _FakeDB(existentes={"cli_dos": existente2})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False)
    check(
        "actualizar: vacios no pisan (password/totp/mailpass/auth intactos)",
        existente2.password == "pass_vieja"
        and existente2.totp_secret == "TOTP-VIEJO"
        and existente2.email_password == "mailpass_vieja"
        and existente2.auth_token == "tok_viejo",
    )
    check(
        "actualizar: el email no vacio si se guarda y se reporta",
        existente2.email == "nuevo@x.com"
        and resultado["cuentas"][0]["campos_actualizados"] == ["email"],
    )

    # Existente YA pausada + en la lista: no repite el campo en el reporte.
    existente3 = _CuentaFake("cli_uno", pausada_activacion=True)
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n")
        db = _FakeDB(existentes={"cli_uno": existente3})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False, usuarios_pausar=["cli_uno"])
    check(
        "actualizar: pausa ya activa se conserva y no se reporta de nuevo",
        existente3.pausada_activacion is True
        and "pausada_activacion" not in resultado["cuentas"][0]["campos_actualizados"],
    )

    # Existente NO pausada + en la lista: pasa a pausada y se reporta.
    existente4 = _CuentaFake("cli_uno", pausada_activacion=False)
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n")
        db = _FakeDB(existentes={"cli_uno": existente4})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False, usuarios_pausar=["cli_uno"])
    check(
        "actualizar: pausa pedida se aplica y se reporta",
        existente4.pausada_activacion is True
        and resultado["cuentas"][0]["campos_actualizados"][-1] == "pausada_activacion",
    )

    # Coincidencia case-insensitive (la lista trae CLI_UNO y la BD cli_uno).
    existente5 = _CuentaFake("cli_uno")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5_MAYUS + "\n")
        db = _FakeDB(existentes={"CLI_UNO": existente5})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False)
    check(
        "actualizar: case-insensitive (sin duplicar)",
        resultado["actualizadas"] == 1
        and resultado["creadas"] == 0
        and db.agregadas == [],
    )


# --------------------------------------------------------------------------- #
# (7) Glob, ilegibles, malformadas y errores
# --------------------------------------------------------------------------- #
def _test_archivos_y_errores(check):
    # Glob por defecto order*.txt dentro de una carpeta parcheada.
    with tempfile.TemporaryDirectory() as tmp:
        _archivo(tmp, LINEA_5 + "\n", nombre="order_a.txt")
        _archivo(tmp, LINEA_6 + "\n", nombre="order_b.txt")
        _archivo(tmp, "ignorado:no:debe:entrar:en:el:glob\n", nombre="otro.txt")
        with mock.patch.object(importar_ordenes, "CARPETA_CLIENTES", Path(tmp)):
            db = _FakeDB()
            with _con_db(db):
                resultado = importar(None, dry_run=True)
        check(
            "glob: toma solo order*.txt y cuenta sus lineas",
            len(resultado["archivos"]) == 2
            and resultado["total"] == 2
            and resultado["creadas"] == 2,
        )

        vacia = Path(tmp) / "vacia"
        vacia.mkdir()
        with mock.patch.object(importar_ordenes, "CARPETA_CLIENTES", vacia):
            with _con_db(_FakeDB()):
                resultado = importar(None, dry_run=True)
        check(
            "glob: sin archivos devuelve error claro sin lanzar",
            resultado["ok"] is False and "no se encontro ningun archivo" in resultado["error"],
        )

    # Archivo ilegible junto a uno bueno.
    with tempfile.TemporaryDirectory() as tmp:
        bueno = _archivo(tmp, LINEA_5 + "\n")
        with _con_db(_FakeDB()):
            resultado = importar([bueno, str(Path(tmp) / "no_existe.txt")], dry_run=True)
        check(
            "archivos: ilegible se reporta y el bueno se procesa",
            resultado["errores"] == 1 and resultado["creadas"] == 1 and resultado["ok"] is False,
        )

    # Linea malformada (9 campos) + valida.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, "a:b:c:d:e:f:g:h:i\n" + LINEA_5 + "\n")
        with _con_db(_FakeDB()):
            resultado = importar([ruta], dry_run=True)
        check(
            "malformada: se cuenta como error sin romper el lote",
            resultado["errores"] == 1
            and resultado["creadas"] == 1
            and "linea malformada" in resultado["detalle_errores"][0],
        )

    # Solo malformadas: error global claro.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, "a:b:c:d:e:f:g:h:i\n")
        with _con_db(_FakeDB()):
            resultado = importar([ruta], dry_run=True)
        check(
            "malformada: sin lineas validas explica el fallo",
            resultado["total"] == 1
            and "no habia lineas validas" in resultado["error"]
            and resultado["ok"] is False,
        )

    # Error por cuenta (aislado y saneado).
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n" + LINEA_6 + "\n")
        db = _FakeDB(error_usuarios={"cli_uno": RuntimeError(f"fallo con {TOKEN}")})
        with _con_db(db):
            resultado = importar([ruta], dry_run=False)
        check(
            "errores: fallo por cuenta se aisla y no expone el token",
            resultado["errores"] == 1
            and resultado["creadas"] == 1
            and resultado["ok"] is False
            and TOKEN not in json.dumps(resultado)
            and "***" in resultado["detalle_errores"][0],
        )

    # Fallo global de BD.
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n")
        with mock.patch.object(
            core.database, "get_db_session", side_effect=RuntimeError("sin BD")
        ):
            resultado = importar([ruta], dry_run=False)
        check(
            "errores: fallo global de BD produce ok=False sin lanzar",
            resultado["ok"] is False and "no se pudo abrir/escribir" in resultado["error"],
        )


# --------------------------------------------------------------------------- #
# (8) _resolver_pausa
# --------------------------------------------------------------------------- #
def _test_resolver_pausa(check):
    with tempfile.TemporaryDirectory() as tmp:
        lista = _archivo(tmp, "cuenta_uno\n@cuenta_dos\n# comentario\n", nombre="pausa.txt")
        usuarios, origen, error = _resolver_pausa("cuenta_tres,CUENTA_UNO", lista)
        check(
            "resolver: une --pausar + archivo con dedupe (conserva 1ª forma)",
            usuarios == ["cuenta_tres", "CUENTA_UNO", "cuenta_dos"]
            and error == ""
            and str(lista) in origen,
        )

    usuarios, origen, error = _resolver_pausa("", None)
    check(
        "resolver: sin argumentos no falla (usa el default si existe)",
        isinstance(usuarios, list) and error == "",
    )

    usuarios, origen, error = _resolver_pausa("", str(Path(tempfile.gettempdir()) / "no_existe_pausa.txt"))
    check(
        "resolver: archivo explicito ilegible -> error (no importa sin pausa)",
        usuarios == [] and "no se pudo leer la lista de pausa" in error,
    )


# --------------------------------------------------------------------------- #
# (9) CLI
# --------------------------------------------------------------------------- #
def _test_cli(check):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _archivo(tmp, LINEA_5 + "\n")

        db = _FakeDB()
        salida = io.StringIO()
        with _con_db(db), contextlib.redirect_stdout(salida):
            codigo = importar_ordenes.main([ruta, "--json"])
        datos = json.loads(salida.getvalue())
        check(
            "cli: --json sin --apply es dry-run y sale 0",
            codigo == 0 and datos["dry_run"] is True and datos["creadas"] == 1,
        )
        check("cli: dry-run no escribio", db.agregadas == [])
        check("cli: el JSON no expone credenciales", _sin_credenciales(datos))

        db = _FakeDB()
        salida = io.StringIO()
        with _con_db(db), contextlib.redirect_stdout(salida):
            codigo = importar_ordenes.main([ruta, "--apply", "--pausar", "cli_uno"])
        check(
            "cli: --apply crea y --pausar deja pausada la cuenta",
            codigo == 0
            and len(db.agregadas) == 1
            and db.agregadas[0].pausada_activacion is True,
        )
        check(
            "cli: la salida legible no imprime la contrasena",
            "Clave-1" not in salida.getvalue() and "MailPass-1" not in salida.getvalue(),
        )

        salida = io.StringIO()
        with _con_db(_FakeDB()), contextlib.redirect_stdout(salida):
            codigo = importar_ordenes.main(
                [ruta, "--pausar-archivo", str(Path(tmp) / "nada.txt")]
            )
        check(
            "cli: pausar-archivo inexistente sale 1 con error",
            codigo == 1 and "no se pudo leer la lista de pausa" in salida.getvalue(),
        )

        # Sin archivos y con la carpeta parcheada vacia: error claro y salida 1.
        vacia = Path(tmp) / "vacia"
        vacia.mkdir()
        salida = io.StringIO()
        with mock.patch.object(importar_ordenes, "CARPETA_CLIENTES", vacia):
            with _con_db(_FakeDB()), contextlib.redirect_stdout(salida):
                codigo = importar_ordenes.main(["--json"])
        check(
            "cli: sin archivos de orden sale 1 con error",
            codigo == 1 and "no se encontro ningun archivo" in salida.getvalue(),
        )


def run(check):
    """Ejecuta todos los checks del importador de ordenes."""
    _test_constantes(check)
    _test_parser_pausa(check)
    _test_creacion(check)
    _test_pausa_altas(check)
    _test_actualizacion(check)
    _test_archivos_y_errores(check)
    _test_resolver_pausa(check)
    _test_cli(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_importar_ordenes.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
