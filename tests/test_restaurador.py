"""Tests rapidos de `cuentas/restaurador.py` y del CLI `restaurar_cuentas.py`.

Sin red, sin Chrome y sin base de datos real: `cuentas.restaurador.get_db_session`
se reemplaza por una sesion falsa (mismo patron que `tests/test_importador.py`).
TODOS los datos son sinteticos; jamas credenciales del respaldo real.

Cubre:
    (1) respaldo de 3 cuentas (1 existente + 2 nuevas): dry_run no escribe y
        `--apply` crea 2 y omite 1.
    (2) `sobrescribir=True` actualiza la existente y `keep` nunca se toca
        (exista o no en la BD), ni con sobrescribir.
    (3) `inactivas=True` fuerza `activa=False` en las cuentas nuevas.
    (4) `fecha_creacion`/`last_checked` ISO -> datetime, `cookies_json` tal cual,
        `id` NO se asigna y las claves desconocidas se ignoran.
    (5) usuario vacio -> error en detalle y sigue; archivo inexistente/JSON
        invalido -> ok=False sin lanzar; lista plana de dicts aceptada.
    (6) `main()` del CLI: dry-run por defecto, `--apply`, `--json` y banderas,
        con la funcion de restauracion monkeypatcheada (no toca la BD).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_restaurador.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import restaurar_cuentas  # noqa: E402
from cuentas import restaurador  # noqa: E402
from cuentas.restaurador import _parsear_fecha, restaurar_desde_backup  # noqa: E402

# --------------------------------------------------------------------------- #
# Fakes de base de datos (nunca se conecta a una BD real)
# --------------------------------------------------------------------------- #
class _FakeQuery:
    """Emula `db.query(Cuenta).filter(Cuenta.usuario == X).first()`."""

    def __init__(self, existentes):
        self._existentes = existentes
        self._usuario = None

    def filter(self, *criterios, **kwargs):
        for criterio in criterios:
            try:
                self._usuario = criterio.right.value
            except Exception:
                continue
            break
        return self

    def first(self):
        return self._existentes.get(self._usuario)


class _FakeDB:
    """Sesion falsa: registra `add` y emula commit/rollback/close."""

    def __init__(self, existentes=None):
        self.existentes = dict(existentes or {})
        self.agregadas = []
        self.commits = 0
        self.rollbacks = 0
        self.cerrada = False

    def query(self, modelo):
        return _FakeQuery(self.existentes)

    def add(self, objeto):
        self.agregadas.append(objeto)
        self.existentes[objeto.usuario] = objeto

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.cerrada = True


@contextlib.contextmanager
def _sesion_fake(db):
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextlib.contextmanager
def _con_sesion(db):
    with mock.patch.object(restaurador, "get_db_session", lambda: _sesion_fake(db)):
        yield db


# --------------------------------------------------------------------------- #
# Datos sinteticos
# --------------------------------------------------------------------------- #
def _cuenta(usuario, **extra):
    """Fila de respaldo sintetica con las columnas tipicas (nunca reales)."""
    datos = {
        "id": 999,
        "usuario": usuario,
        "password": "pass_sintetica",
        "plataforma": "twitter",
        "activa": True,
        "status": "imported",
        "cookies_json": None,
        "fecha_creacion": "2026-01-02T03:04:05",
        "last_checked": None,
        "clave_desconocida": "debe_ignorarse",
    }
    datos.update(extra)
    return datos


def _backup(cuentas, keep=None):
    return {
        "fecha": "2026-09-21T14:21:29.462094",
        "host": "host_sintetico",
        "keep": list(keep or []),
        "cuentas": cuentas,
        "tareas": [{"id": 1, "tipo": "post"}],
    }


def _escribir(directorio, data):
    ruta = Path(directorio) / "limpieza_cuentas_sintetica.json"
    ruta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(ruta)


def _existente(usuario, **extra):
    """Cuenta ya presente en la BD (objeto simple, no modelo SQLAlchemy)."""
    datos = types.SimpleNamespace(
        id=1,
        usuario=usuario,
        password="vieja",
        activa=True,
        status="imported",
        cookies_json=[{"name": "vieja", "value": "1"}],
        last_checked=None,
        fecha_creacion=datetime(2025, 1, 1),
    )
    for clave, valor in extra.items():
        setattr(datos, clave, valor)
    return datos


# --------------------------------------------------------------------------- #
# (1) dry_run vs apply con 1 existente y 2 nuevas
# --------------------------------------------------------------------------- #
def test_dry_run_y_apply(check):
    print("(1) respaldo de 3 cuentas: 1 existente y 2 nuevas")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _escribir(
            tmp,
            _backup([_cuenta("existente_uno"), _cuenta("nueva_uno"), _cuenta("nueva_dos")]),
        )

        db_dry = _FakeDB({"existente_uno": _existente("existente_uno")})
        with _con_sesion(db_dry):
            res_dry = restaurar_desde_backup(ruta, dry_run=True)

        check("dry-run: ok", res_dry["ok"] is True)
        check("dry-run: total 3", res_dry["total"] == 3)
        check("dry-run: 2 restauradas", res_dry["restauradas"] == 2)
        check("dry-run: 1 omitida (ya existente)", res_dry["omitidas"] == 1)
        check("dry-run: 0 errores", res_dry["errores"] == 0)
        check("dry-run: NO llama add", db_dry.agregadas == [])
        check(
            "dry-run: la existente no se modifica",
            db_dry.existentes["existente_uno"].password == "vieja",
        )
        check("dry-run: se marca como simulacion", res_dry["dry_run"] is True)

        db_apply = _FakeDB({"existente_uno": _existente("existente_uno")})
        with _con_sesion(db_apply):
            res_apply = restaurar_desde_backup(ruta, dry_run=False)

        check("apply: ok", res_apply["ok"] is True)
        check("apply: 2 restauradas", res_apply["restauradas"] == 2)
        check("apply: 1 omitida", res_apply["omitidas"] == 1)
        check("apply: se agregaron 2 cuentas", len(db_apply.agregadas) == 2)
        check(
            "apply: usuarios creados exactos",
            {c.usuario for c in db_apply.agregadas} == {"nueva_uno", "nueva_dos"},
        )
        check(
            "apply: la existente queda intacta sin --sobrescribir",
            db_apply.existentes["existente_uno"].password == "vieja",
        )
        check(
            "apply: la transaccion hizo commit y cerro la sesion",
            db_apply.commits == 1 and db_apply.cerrada,
        )


# --------------------------------------------------------------------------- #
# (2) sobrescribir + keep
# --------------------------------------------------------------------------- #
def test_sobrescribir_y_keep(check):
    print("(2) sobrescribir actualiza y 'keep' nunca se toca")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _escribir(
            tmp,
            _backup(
                [
                    _cuenta("keep_existe", password="nueva_keep"),
                    _cuenta("keep_nuevo", password="nueva_keep_nuevo"),
                    _cuenta("normal_existe", password="nueva_normal", activa=False),
                    _cuenta("normal_nueva", password="nueva_nueva"),
                ],
                keep=["keep_existe", "keep_nuevo"],
            ),
        )

        db = _FakeDB(
            {
                "keep_existe": _existente("keep_existe", password="keep_vieja"),
                "normal_existe": _existente("normal_existe", password="vieja_normal"),
            }
        )
        with _con_sesion(db):
            res = restaurar_desde_backup(ruta, dry_run=False, sobrescribir=True)

        check("sobrescribir: 1 actualizada", res["actualizadas"] == 1)
        check("sobrescribir: 2 omitidas (keep)", res["omitidas"] == 2)
        check("sobrescribir: 1 restaurada", res["restauradas"] == 1)
        check(
            "sobrescribir: la existente SI se pisa",
            db.existentes["normal_existe"].password == "nueva_normal",
        )
        check(
            "sobrescribir: tambien actualiza otras columnas (activa)",
            db.existentes["normal_existe"].activa is False,
        )
        check(
            "keep existente: intacta aunque sobrescribir=True",
            db.existentes["keep_existe"].password == "keep_vieja",
        )
        check(
            "keep nuevo: NO se crea",
            [c.usuario for c in db.agregadas] == ["normal_nueva"],
        )
        check("keep nuevo: no aparece en la sesion", "keep_nuevo" not in db.existentes)

        db2 = _FakeDB({"normal_existe": _existente("normal_existe", password="vieja_normal")})
        with _con_sesion(db2):
            res2 = restaurar_desde_backup(ruta, dry_run=False)  # sin sobrescribir
        check(
            "sin sobrescribir: la existente queda intacta",
            db2.existentes["normal_existe"].password == "vieja_normal",
        )
        check("sin sobrescribir: 3 omitidas (2 keep + 1 existente)", res2["omitidas"] == 3)
        check("sin sobrescribir: 0 actualizadas", res2["actualizadas"] == 0)


# --------------------------------------------------------------------------- #
# (3) inactivas
# --------------------------------------------------------------------------- #
def test_inactivas(check):
    print("(3) inactivas=True fuerza activa=False en las nuevas")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _escribir(tmp, _backup([_cuenta("nueva_activa", activa=True)]))

        db_on = _FakeDB()
        with _con_sesion(db_on):
            restaurar_desde_backup(ruta, dry_run=False, inactivas=True)
        check("inactivas=True: la nueva queda activa=False", db_on.agregadas[0].activa is False)

        db_off = _FakeDB()
        with _con_sesion(db_off):
            restaurar_desde_backup(ruta, dry_run=False)
        check(
            "inactivas=False: conserva activa=True del respaldo",
            db_off.agregadas[0].activa is True,
        )


# --------------------------------------------------------------------------- #
# (4) columnas: fechas, cookies_json, id y claves desconocidas
# --------------------------------------------------------------------------- #
def test_columnas(check):
    print("(4) fechas ISO, cookies_json tal cual, id no asignado y claves ajenas")
    cookies = [{"name": "auth_token", "value": "sintetica", "domain": ".x.com", "path": "/"}]
    fila = _cuenta(
        "fechas_user",
        cookies_json=cookies,
        fecha_creacion="2026-01-02T03:04:05.123456",
        last_checked="2026-01-03T04:05:06Z",
    )
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _escribir(tmp, _backup([fila]))
        db = _FakeDB()
        with _con_sesion(db):
            restaurar_desde_backup(ruta, dry_run=False)

    creada = db.agregadas[0]
    check(
        "fecha_creacion ISO -> datetime",
        creada.fecha_creacion == datetime(2026, 1, 2, 3, 4, 5, 123456),
    )
    check(
        "last_checked ISO con Z -> datetime UTC sin tzinfo",
        creada.last_checked == datetime(2026, 1, 3, 4, 5, 6)
        and creada.last_checked.tzinfo is None,
    )
    check(
        "cookies_json se pasa tal cual (lista con los mismos valores)",
        isinstance(creada.cookies_json, list) and creada.cookies_json == cookies,
    )
    check("id NO se asigna (lo asigna la BD)", creada.id is None)
    check("usuario sale limpio del respaldo", creada.usuario == "fechas_user")
    check("clave desconocida ignorada", not hasattr(creada, "clave_desconocida"))

    ahora = datetime(2026, 5, 6, 7, 8, 9)
    check("_parsear_fecha: datetime pasa igual", _parsear_fecha(ahora) is ahora)
    check("_parsear_fecha: None -> None", _parsear_fecha(None) is None)
    check("_parsear_fecha: cadena vacia -> None", _parsear_fecha("") is None)
    check("_parsear_fecha: texto invalido -> None", _parsear_fecha("no-es-fecha") is None)
    check("_parsear_fecha: numero -> None (no lanza)", _parsear_fecha(12345) is None)
    check(
        "_parsear_fecha: ISO sin zona",
        _parsear_fecha("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5),
    )
    check(
        "_parsear_fecha: ISO con Z",
        _parsear_fecha("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5),
    )


# --------------------------------------------------------------------------- #
# (5) errores tolerados: usuario vacio, archivo/JSON malos y lista plana
# --------------------------------------------------------------------------- #
def test_errores(check):
    print("(5) usuario vacio, archivo inexistente, JSON invalido y lista plana")
    with tempfile.TemporaryDirectory() as tmp:
        ruta = _escribir(
            tmp,
            _backup(
                [
                    _cuenta("valida_uno"),
                    {"usuario": ""},
                    {"usuario": "   "},
                    _cuenta("valida_dos"),
                ]
            ),
        )
        db = _FakeDB()
        with _con_sesion(db):
            res = restaurar_desde_backup(ruta, dry_run=False)

        check("usuario vacio: 2 errores", res["errores"] == 2)
        check("usuario vacio: 2 mensajes en detalle", len(res["detalle_errores"]) == 2)
        check("usuario vacio: sigue con las demas (2 restauradas)", res["restauradas"] == 2)
        check("usuario vacio: total contado (4)", res["total"] == 4)
        check(
            "usuario vacio: el detalle identifica las filas 2 y 3",
            any("fila 2" in m for m in res["detalle_errores"])
            and any("fila 3" in m for m in res["detalle_errores"]),
        )

        inexistente = str(Path(tmp) / "no_existe.json")
        res_falta = restaurar_desde_backup(inexistente, dry_run=True)
        check("archivo inexistente: ok=False sin lanzar", res_falta["ok"] is False)
        check(
            "archivo inexistente: error reportado",
            res_falta["errores"] == 1 and bool(res_falta["detalle_errores"]),
        )

        ruta_mala = Path(tmp) / "mal.json"
        ruta_mala.write_text("{esto no es json", encoding="utf-8")
        res_malo = restaurar_desde_backup(str(ruta_mala), dry_run=True)
        check("JSON invalido: ok=False sin lanzar", res_malo["ok"] is False)
        check("JSON invalido: error reportado", res_malo["errores"] == 1)

        ruta_lista = Path(tmp) / "lista.json"
        ruta_lista.write_text(json.dumps([_cuenta("plana_uno")]), encoding="utf-8")
        db_lista = _FakeDB()
        with _con_sesion(db_lista):
            res_lista = restaurar_desde_backup(str(ruta_lista), dry_run=False)
        check(
            "lista plana: 1 restaurada y ok=True",
            res_lista["ok"] is True and res_lista["restauradas"] == 1,
        )
        check(
            "lista plana: cuenta creada",
            [c.usuario for c in db_lista.agregadas] == ["plana_uno"],
        )


# --------------------------------------------------------------------------- #
# (6) CLI main(): dry-run por defecto, --apply, --json y banderas
# --------------------------------------------------------------------------- #
def test_main_cli(check):
    print("(6) main(): dry-run por defecto, --apply y --json")
    llamadas = []

    def _falsa(archivo, dry_run=True, sobrescribir=False, inactivas=False):
        llamadas.append(
            {
                "archivo": archivo,
                "dry_run": dry_run,
                "sobrescribir": sobrescribir,
                "inactivas": inactivas,
            }
        )
        return {
            "ok": True,
            "total": 3,
            "restauradas": 2,
            "actualizadas": 0,
            "omitidas": 1,
            "errores": 0,
            "detalle_errores": [],
            "dry_run": dry_run,
            "archivo": archivo,
        }

    with tempfile.TemporaryDirectory() as tmp:
        ruta_fake = str(Path(tmp) / "respaldo_fake.json")
        with mock.patch.object(restaurador, "restaurar_desde_backup", _falsa):
            salida = io.StringIO()
            with contextlib.redirect_stdout(salida):
                codigo = restaurar_cuentas.main(["--archivo", ruta_fake])
            check("main dry-run: devuelve 0", codigo == 0)
            check("main dry-run: usa la ruta indicada", llamadas[-1]["archivo"] == ruta_fake)
            check("main dry-run: dry_run=True sin --apply", llamadas[-1]["dry_run"] is True)
            check(
                "main dry-run: el texto avisa simulacion y --apply",
                "SIMULACION" in salida.getvalue() and "--apply" in salida.getvalue(),
            )

            with contextlib.redirect_stdout(io.StringIO()):
                codigo2 = restaurar_cuentas.main(
                    ["--archivo", ruta_fake, "--apply", "--sobrescribir", "--inactivas"]
                )
            check("main --apply: devuelve 0", codigo2 == 0)
            check(
                "main --apply: pasa las banderas",
                llamadas[-1]["dry_run"] is False
                and llamadas[-1]["sobrescribir"] is True
                and llamadas[-1]["inactivas"] is True,
            )

            salida_json = io.StringIO()
            with contextlib.redirect_stdout(salida_json):
                codigo3 = restaurar_cuentas.main(["--archivo", ruta_fake, "--json"])
            try:
                datos = json.loads(salida_json.getvalue())
            except Exception:
                datos = None
            check("main --json: devuelve 0", codigo3 == 0)
            check(
                "main --json: imprime JSON parseable con el resumen",
                isinstance(datos, dict) and datos.get("restauradas") == 2,
            )
            check("main: nunca llamo a la funcion real (solo al fake)", len(llamadas) == 3)


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_dry_run_y_apply(check)
    test_sobrescribir_y_keep(check)
    test_inactivas(check)
    test_columnas(check)
    test_errores(check)
    test_main_cli(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_restaurador.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
