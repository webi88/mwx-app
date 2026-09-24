"""Tests rapidos de `arreglar_columnas.py` (sin red, sin BD real, sin Chrome).

`arreglar_columnas` importa `get_db_session` de forma perezosa dentro de la
funcion, asi que estos tests reemplazan `core.database.get_db_session` por una
sesion FALSA (mismo patron que `tests/test_importar_clientes.py`). TODOS los
datos son sinteticos.

Cubre:
  (1) Detector: firma desplazada valida; rechaza corto/largo/minusculas/espacios/
      solo digitos/sin '@'/None/vacios y valores raros.
  (2) Reparacion real: los 4 cambios exactos + `email_password=""`.
  (3) No toca cuentas que no cumplen la firma.
  (4) No modifica tier/seccion/avatar/banner/rol/status/cookies/password.
  (5) Un commit por cuenta reparada (contador de la sesion fake).
  (6) Dry-run: reporta sin escribir ni commitear.
  (7) Tolerante a valores raros y a fallos de BD (nunca lanza).
  (8) CLI: --dry-run, --json, aplicacion por defecto y sin filtrar secretos.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_arreglar_columnas.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import core.database  # noqa: E402
import arreglar_columnas  # noqa: E402
from arreglar_columnas import (  # noqa: E402
    LARGO_TOTP_MAYUS,
    _ocultar_credenciales,
    arreglar_columnas as arreglar,
    es_patron_desplazado,
)

# --------------------------------------------------------------------------- #
# Datos sinteticos (ninguna credencial real)
# --------------------------------------------------------------------------- #
TOTP_REAL = "JBSWY3DPEHPK3PXP"  # 16 mayusculas, forma base32
TOKEN_HEX = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40 hex
CORREO_REAL = "correo.restaurado@yahoo.com"


# --------------------------------------------------------------------------- #
# Fakes (nunca se conecta a una BD real)
# --------------------------------------------------------------------------- #
class _CuentaFake:
    """Cuenta con las columnas que el arreglo SI puede tocar y las que NO."""

    def __init__(self, usuario, **extra):
        self.usuario = usuario
        self.password = extra.get("password", "pass_original")
        self.auth_token = extra.get("auth_token", "")
        self.totp_secret = extra.get("totp_secret", "")
        self.email = extra.get("email", "")
        self.email_password = extra.get("email_password", "")
        # Columnas que NUNCA deben cambiar.
        self.tier_calidad = extra.get("tier_calidad", "tier1")
        self.seccion = extra.get("seccion", "CI")
        self.avatar_path = extra.get("avatar_path", "data/avatares/foto.png")
        self.banner_path = extra.get("banner_path", "data/portadas/portada.png")
        self.rol_activacion = extra.get("rol_activacion", "cita")
        self.status = extra.get("status", "active")
        self.cookies_json = extra.get("cookies_json", [{"name": "auth_token", "value": "x"}])
        self.cookies_path = extra.get("cookies_path", "data/cookies/twitter/x.pkl")


def _cuenta_desplazada(usuario="cuenta_desplazada", **extra):
    """Cuenta con el patron exacto que el proveedor dejo desplazado."""
    datos = {
        "auth_token": TOTP_REAL,
        "totp_secret": CORREO_REAL,
        "email": "viejo@desplazado.com",
        "email_password": TOKEN_HEX,
        "password": "Pass-Original-1",
    }
    datos.update(extra)
    return _CuentaFake(usuario, **datos)


def _estado(cuenta):
    """Tupla con los 4 campos que el arreglo toca (para snapshots)."""
    return (cuenta.auth_token, cuenta.totp_secret, cuenta.email, cuenta.email_password)


class _FakeQuery:
    """Emula `db.query(Cuenta).order_by(...).all()` sobre la lista fake."""

    def __init__(self, db):
        self.db = db

    def order_by(self, *criterios, **kwargs):
        return self

    def all(self):
        return list(self.db.cuentas)


class _FakeDB:
    """Sesion falsa: lista de cuentas + contadores de commit/rollback."""

    def __init__(self, cuentas=None, error_consulta=None, error_commit=None):
        self.cuentas = list(cuentas or [])
        self.error_consulta = error_consulta
        self.error_commit = error_commit
        self.commits = 0
        self.rollbacks = 0
        self.cerrada = False

    def query(self, modelo):
        if self.error_consulta is not None:
            raise self.error_consulta
        return _FakeQuery(self)

    def commit(self):
        self.commits += 1
        if self.error_commit is not None:
            error = self.error_commit
            self.error_commit = None  # fallo one-shot (el commit del contexto ya no falla)
            raise error

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.cerrada = True


@contextlib.contextmanager
def _sesion_fake(db):
    """Mimetiza `core.database.get_db_session` (commit al salir, rollback con error)."""
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextlib.contextmanager
def _con_db(db):
    """Inyecta la sesion falsa en el import perezoso de `core.database`."""
    with mock.patch.object(core.database, "get_db_session", lambda: _sesion_fake(db)):
        yield db


# --------------------------------------------------------------------------- #
# (1) Detector
# --------------------------------------------------------------------------- #
def _test_detector(check):
    check(
        "detector: firma desplazada valida (16 mayusculas + '@' en totp)",
        es_patron_desplazado(TOTP_REAL, CORREO_REAL) is True,
    )
    check(
        "detector: exige EXACTAMENTE 16 caracteres",
        es_patron_desplazado(TOTP_REAL[:15], CORREO_REAL) is False
        and es_patron_desplazado(TOTP_REAL + "X", CORREO_REAL) is False
        and LARGO_TOTP_MAYUS == 16,
    )
    check(
        "detector: minusculas no cumplen",
        es_patron_desplazado(TOTP_REAL.lower(), CORREO_REAL) is False,
    )
    check(
        "detector: espacios no cumplen",
        es_patron_desplazado("ABCDEFGHIJK LMNO", CORREO_REAL) is False,
    )
    check(
        "detector: solo digitos no cumple",
        es_patron_desplazado("1234567890123456", CORREO_REAL) is False,
    )
    check(
        "detector: totp_secret sin '@' no cumple",
        es_patron_desplazado(TOTP_REAL, "TOTPVALIDO123456") is False,
    )
    check(
        "detector: None/vacios no cumplen",
        es_patron_desplazado(None, None) is False
        and es_patron_desplazado("", "") is False
        and es_patron_desplazado(TOTP_REAL, None) is False,
    )
    check(
        "detector: valores raros no lanzan",
        es_patron_desplazado(1234567890123456, ["x"]) is False
        and es_patron_desplazado(b"ABCDEFGHIJKLMNOP", {"a": 1}) is False
        and es_patron_desplazado(TOTP_REAL, ["correo@x.com"]) is False,
    )
    check(
        "seguridad: _ocultar_credenciales tapa los secretos",
        "***" in _ocultar_credenciales(f"fallo con {TOKEN_HEX}", TOKEN_HEX)
        and TOKEN_HEX not in _ocultar_credenciales(f"fallo con {TOKEN_HEX}", TOKEN_HEX),
    )


# --------------------------------------------------------------------------- #
# (2) Reparacion real
# --------------------------------------------------------------------------- #
def _test_reparacion(check):
    db = _FakeDB([_cuenta_desplazada()])
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    cuenta = db.cuentas[0]

    check(
        "reparar: ok, 1 revisada y 1 reparada",
        resultado["ok"] is True
        and resultado["revisadas"] == 1
        and resultado["reparadas"] == 1
        and resultado["dry_run"] is False,
    )
    check("reparar: totp_secret recibe el TOTP real", cuenta.totp_secret == TOTP_REAL)
    check("reparar: auth_token recibe el hex de email_password", cuenta.auth_token == TOKEN_HEX)
    check("reparar: email recibe el correo que estaba en totp_secret", cuenta.email == CORREO_REAL)
    check("reparar: email_password queda vacio (se uso de puente)", cuenta.email_password == "")
    check("reparar: password original intacto", cuenta.password == "Pass-Original-1")
    check(
        "reparar: lista de usuarios reparados",
        resultado["usuarios"] == ["cuenta_desplazada"]
        and resultado["detalle"] == [{"usuario": "cuenta_desplazada", "email": CORREO_REAL}],
    )
    check(
        "reparar: el reporte no expone TOTP ni token",
        TOTP_REAL not in json.dumps(resultado) and TOKEN_HEX not in json.dumps(resultado),
    )
    check("reparar: cierra la sesion", db.cerrada is True)


# --------------------------------------------------------------------------- #
# (3) No toca las que no cumplen
# --------------------------------------------------------------------------- #
def _test_no_cumplen(check):
    normales = [
        _CuentaFake(
            "normal",
            auth_token=TOKEN_HEX,
            totp_secret=TOTP_REAL,
            email=CORREO_REAL,
            email_password="mailpass",
        ),
        _CuentaFake("token_corto", auth_token=TOTP_REAL[:10], totp_secret=CORREO_REAL, email="a@b.com", email_password="mp"),
        _CuentaFake("token_largo", auth_token=TOTP_REAL + "ZZ", totp_secret=CORREO_REAL, email="a@b.com", email_password="mp"),
        _CuentaFake("token_minusculas", auth_token=TOTP_REAL.lower(), totp_secret=CORREO_REAL, email="a@b.com", email_password="mp"),
        _CuentaFake("totp_sin_arroba", auth_token=TOTP_REAL, totp_secret="TOTPVALIDO123456", email="a@b.com", email_password="mp"),
        _CuentaFake("vacia", auth_token="", totp_secret="", email="", email_password=""),
        _CuentaFake("nula", auth_token=None, totp_secret=None, email=None, email_password=None),
    ]
    antes = [_estado(cuenta) for cuenta in normales]

    db = _FakeDB(normales)
    with _con_db(db):
        resultado = arreglar(dry_run=False)

    check(
        "no cumple: 7 revisadas y 0 reparadas",
        resultado["ok"] is True and resultado["revisadas"] == 7 and resultado["reparadas"] == 0,
    )
    check(
        "no cumple: ningun campo cambio",
        [_estado(cuenta) for cuenta in normales] == antes and resultado["usuarios"] == [],
    )
    check(
        "no cumple: solo el commit del contexto (nada que escribir)",
        db.commits == 1 and db.rollbacks == 0,
    )


# --------------------------------------------------------------------------- #
# (4) No modifica otras columnas
# --------------------------------------------------------------------------- #
def _test_no_modifica_otras_columnas(check):
    cookies = [{"name": "auth_token", "value": "secreta"}]
    cuenta = _cuenta_desplazada(
        "intacta",
        tier_calidad="tier2",
        seccion="LIB",
        avatar_path="data/avatares/avatar.png",
        banner_path="data/portadas/banner.png",
        rol_activacion="rt",
        status="suspended",
        cookies_json=cookies,
        cookies_path="data/cookies/twitter/intacta.pkl",
        password="Pass-Intacta",
    )
    db = _FakeDB([cuenta])
    with _con_db(db):
        arreglar(dry_run=False)

    check(
        "otras columnas: tier/seccion/avatar/banner/rol/status intactos",
        cuenta.tier_calidad == "tier2"
        and cuenta.seccion == "LIB"
        and cuenta.avatar_path == "data/avatares/avatar.png"
        and cuenta.banner_path == "data/portadas/banner.png"
        and cuenta.rol_activacion == "rt"
        and cuenta.status == "suspended",
    )
    check(
        "otras columnas: cookies/password/usuario intactos",
        cuenta.cookies_json == cookies
        and cuenta.cookies_path == "data/cookies/twitter/intacta.pkl"
        and cuenta.password == "Pass-Intacta"
        and cuenta.usuario == "intacta",
    )
    check(
        "otras columnas: el arreglo si ocurrio con las 4 permitidas",
        cuenta.totp_secret == TOTP_REAL
        and cuenta.auth_token == TOKEN_HEX
        and cuenta.email == CORREO_REAL
        and cuenta.email_password == "",
    )


# --------------------------------------------------------------------------- #
# (5) Un commit por cuenta reparada
# --------------------------------------------------------------------------- #
def _test_commit_por_cuenta(check):
    db = _FakeDB([_cuenta_desplazada("uno"), _cuenta_desplazada("dos"), _cuenta_desplazada("tres")])
    with _con_db(db):
        resultado = arreglar(dry_run=False)

    check("commits: 3 reparadas de 3 revisadas", resultado["reparadas"] == 3 and resultado["revisadas"] == 3)
    check(
        "commits: uno por cuenta reparada (+ el del contexto)",
        db.commits == resultado["reparadas"] + 1,
        f"(commits={db.commits})",
    )
    check(
        "commits: usuarios en orden estable",
        resultado["usuarios"] == ["uno", "dos", "tres"],
    )


# --------------------------------------------------------------------------- #
# (6) Dry-run
# --------------------------------------------------------------------------- #
def _test_dry_run(check):
    desplazadas = [_cuenta_desplazada("uno"), _cuenta_desplazada("dos")]
    antes = [_estado(cuenta) for cuenta in desplazadas]

    db = _FakeDB(desplazadas)
    with _con_db(db):
        resultado = arreglar(dry_run=True)

    check(
        "dry-run: reporta 2 a reparar de 2 revisadas",
        resultado["ok"] is True
        and resultado["dry_run"] is True
        and resultado["revisadas"] == 2
        and resultado["reparadas"] == 2,
    )
    check(
        "dry-run: lista de usuarios y emails restaurados",
        resultado["usuarios"] == ["uno", "dos"]
        and [fila["email"] for fila in resultado["detalle"]] == [CORREO_REAL, CORREO_REAL],
    )
    check("dry-run: NO escribio ningun campo", [_estado(cuenta) for cuenta in desplazadas] == antes)
    check("dry-run: no hizo commit propio (solo el del contexto)", db.commits == 1)


# --------------------------------------------------------------------------- #
# (7) Tolerancia a valores raros y errores
# --------------------------------------------------------------------------- #
class _CuentaQueExplota(_CuentaFake):
    """Cuenta cuyo `auth_token` no se puede leer (campo corrupto)."""

    @property
    def auth_token(self):
        raise RuntimeError("campo ilegible")

    @auth_token.setter
    def auth_token(self, valor):
        self._auth_token = valor


def _test_tolerante(check):
    # Valores raros en los campos de la condicion: no cumplen y no lanzan.
    raras = [
        _CuentaFake("int", auth_token=1234567890123456, totp_secret=CORREO_REAL),
        _CuentaFake("bytes", auth_token=b"ABCDEFGHIJKLMNOP", totp_secret=CORREO_REAL),
        _CuentaFake("lista", auth_token=[TOTP_REAL], totp_secret=[CORREO_REAL]),
        _CuentaFake("dict", auth_token={"a": 1}, totp_secret={"b": 2}),
    ]
    antes = [_estado(cuenta) for cuenta in raras]
    db = _FakeDB(raras)
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    check(
        "raro: valores no-string se ignoran sin lanzar",
        resultado["ok"] is True and resultado["reparadas"] == 0 and [_estado(c) for c in raras] == antes,
    )

    # email_password None en una cuenta desplazada: copia literal (sin excepcion).
    cuenta = _CuentaFake(
        "sin_emailpass",
        auth_token=TOTP_REAL,
        totp_secret=CORREO_REAL,
        email="viejo@x.com",
        email_password=None,
    )
    db = _FakeDB([cuenta])
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    check(
        "raro: email_password None se copia tal cual (sin lanzar)",
        resultado["reparadas"] == 1
        and cuenta.totp_secret == TOTP_REAL
        and cuenta.auth_token is None
        and cuenta.email == CORREO_REAL
        and cuenta.email_password == "",
    )

    # Una cuenta que lanza se aisla: la siguiente se repara igual.
    db = _FakeDB([_CuentaQueExplota("explota"), _cuenta_desplazada("buena")])
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    check(
        "raro: una cuenta que lanza se aisla y se reporta",
        resultado["errores"] == 1
        and resultado["reparadas"] == 1
        and resultado["ok"] is False
        and resultado["usuarios"] == ["buena"],
    )
    check(
        "raro: el fallo por cuenta se reporta en detalle_errores",
        len(resultado["detalle_errores"]) == 1
        and "campo ilegible" in resultado["detalle_errores"][0],
    )

    # Fallo de commit con un secreto en el mensaje: se sanea con ***.
    db = _FakeDB(
        [_cuenta_desplazada("commit_falla")],
        error_commit=RuntimeError(f"commit fallo con {TOKEN_HEX}"),
    )
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    check(
        "raro: el error de commit no expone el token",
        resultado["errores"] == 1
        and TOKEN_HEX not in json.dumps(resultado)
        and "***" in resultado["detalle_errores"][0]
        and db.rollbacks >= 1,
    )

    # Fallo global de consulta: ok=False claro, sin lanzar.
    db = _FakeDB([_cuenta_desplazada("nunca")], error_consulta=RuntimeError("sin BD"))
    with _con_db(db):
        resultado = arreglar(dry_run=False)
    check(
        "raro: fallo global de BD produce ok=False sin lanzar",
        resultado["ok"] is False
        and "no se pudo abrir/recorrer" in resultado["error"]
        and resultado["reparadas"] == 0,
    )


# --------------------------------------------------------------------------- #
# (8) CLI
# --------------------------------------------------------------------------- #
def _test_cli(check):
    # --dry-run --json: no escribe y sale 0.
    cuenta = _cuenta_desplazada("uno")
    db = _FakeDB([cuenta])
    salida = io.StringIO()
    with _con_db(db), contextlib.redirect_stdout(salida):
        codigo = arreglar_columnas.main(["--dry-run", "--json"])
    datos = json.loads(salida.getvalue())
    check(
        "cli: --dry-run --json sale 0 y reporta dry_run",
        codigo == 0 and datos["dry_run"] is True and datos["reparadas"] == 1 and datos["revisadas"] == 1,
    )
    check(
        "cli: --dry-run no escribio ni expuso el TOTP/token",
        cuenta.auth_token == TOTP_REAL
        and cuenta.totp_secret == CORREO_REAL
        and cuenta.email_password == TOKEN_HEX
        and db.commits == 1
        and TOTP_REAL not in json.dumps(datos)
        and TOKEN_HEX not in json.dumps(datos),
    )

    # Sin flags: aplica los cambios y muestra la linea discreta + conteo exacto.
    cuenta = _cuenta_desplazada("uno")
    db = _FakeDB([cuenta])
    salida = io.StringIO()
    with _con_db(db), contextlib.redirect_stdout(salida):
        codigo = arreglar_columnas.main([])
    texto = salida.getvalue()
    check(
        "cli: sin flags aplica los cambios y sale 0",
        codigo == 0
        and cuenta.totp_secret == TOTP_REAL
        and cuenta.auth_token == TOKEN_HEX
        and cuenta.email == CORREO_REAL
        and cuenta.email_password == "",
    )
    check(
        "cli: imprime la linea discreta por cuenta",
        "@uno: campos reacomodados" in texto,
        "",
    )
    check(
        "cli: imprime el conteo EXACTO (reparadas y revisadas)",
        "Cuentas reparadas: 1 de 1 revisadas." in texto,
    )
    check(
        "cli: NUNCA imprime el TOTP ni el auth token",
        TOTP_REAL not in texto and TOKEN_HEX not in texto,
    )
    check(
        "cli: el email restaurado si se puede mostrar",
        CORREO_REAL in texto,
    )

    # Fallo global: sale 1 con ERROR.
    db = _FakeDB([], error_consulta=RuntimeError("sin BD"))
    salida = io.StringIO()
    with _con_db(db), contextlib.redirect_stdout(salida):
        codigo = arreglar_columnas.main([])
    check(
        "cli: fallo de BD sale 1 sin lanzar",
        codigo == 1 and "ERROR" in salida.getvalue(),
    )


def run(check):
    """Ejecuta todos los checks de la reparacion de columnas."""
    _test_detector(check)
    _test_reparacion(check)
    _test_no_cumplen(check)
    _test_no_modifica_otras_columnas(check)
    _test_commit_por_cuenta(check)
    _test_dry_run(check)
    _test_tolerante(check)
    _test_cli(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_arreglar_columnas.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
