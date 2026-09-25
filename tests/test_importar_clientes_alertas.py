# -*- coding: utf-8 -*-
"""Tests rapidos de `importar_clientes_alertas.py` (sin red, con BD temporal).

Cubre:
  (a) `normalizar_chats`: singular/plural, listas, dedupe, vacios.
  (b) `normalizar_cliente(s)`: keywords/exclude JSON, num_principales,
      `telegram_chat_id` -> `telegram_chat_ids`.
  (c) `cargar_clientes_desde_config`: acentos UTF-8, archivo inexistente,
      config sin `alertas_keywords`, JSON invalido.
  (d) Dry-run: reporta creados y NO escribe NADA (0 clientes / 0 celulas).
  (e) `--apply` (via `importar_clientes`): crea celula + clientes con los
      campos correctos; segunda pasada = sin cambios (idempotente).
  (f) Nunca pisa con vacios: keywords/chats/localidad/exclude vacios en el
      origen conservan lo existente; `activo=False` se reactiva; match de
      nombre case-insensitive; celula existente se reutiliza.
  (g) `main`: archivo inexistente -> codigo 1; dry-run cablea `--celula` y
      `--json` sin tocar la BD real (funciones parcheadas).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_importar_clientes_alertas.py
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.models  # noqa: E402,F401  (registra las tablas en Base.metadata)
import importar_clientes_alertas as imp  # noqa: E402
from core.database import Base  # noqa: E402
from core.models import Celula, Cliente  # noqa: E402

_CONFIG_EJEMPLO = {
    "alertas_keywords": {
        "ORA": {
            "keywords": ["Óscar Rébora", "@OscarRebora", "SEMA Quintana Roo"],
            "localidad": "Cancún",
            "telegram_chat_id": "-5148472299",
            "exclude_terms": ["hotel", "resort"],
            "num_principales": 10,
        },
        "Harfuch": {
            "keywords": ["Omar García Harfuch", "Harfuch"],
            "num_principales": 6,
            "telegram_chat_id": "-5256145609, -5291009791",
        },
    },
    "gemini_api_key": "NO_DEBE_SALIR_NUNCA",
    "telegram_bot_token": "NO_DEBE_SALIR_NUNCA",
}

_CLIENTES = imp.normalizar_clientes(_CONFIG_EJEMPLO["alertas_keywords"])


@contextlib.contextmanager
def _bd_temporal():
    """Crea una BD SQLite temporal y devuelve una `session_factory` inyectable."""
    carpeta = tempfile.mkdtemp(prefix="alertas_clientes_")
    engine = None
    try:
        engine = create_engine(f"sqlite:///{carpeta}/test.db")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        @contextlib.contextmanager
        def sesion():
            db = Session()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        yield sesion
    finally:
        if engine is not None:
            engine.dispose()
        shutil.rmtree(carpeta, ignore_errors=True)


def _escribir_config(carpeta: str, datos) -> str:
    ruta = Path(carpeta) / "config.json"
    ruta.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    return str(ruta)


def _clientes_en(db_factory):
    with db_factory() as db:
        return db.query(Cliente).order_by(Cliente.nombre).all()


def _celulas_en(db_factory):
    with db_factory() as db:
        return db.query(Celula).all()


# --------------------------------------------------------------------------- #
# (a) normalizar_chats
# --------------------------------------------------------------------------- #
def test_normalizar_chats(check):
    print("(a) normalizar_chats")
    check(
        "chats: string con 2 ids se conserva",
        imp.normalizar_chats("-5256145609, -5291009791") == "-5256145609, -5291009791",
        repr(imp.normalizar_chats("-5256145609, -5291009791")),
    )
    check(
        "chats: lista con espacios/vacios -> 'id1, id2'",
        imp.normalizar_chats(["-1", " -2 ", ""]) == "-1, -2",
        repr(imp.normalizar_chats(["-1", " -2 ", ""])),
    )
    check("chats: None -> ''", imp.normalizar_chats(None) == "", repr(imp.normalizar_chats(None)))
    check(
        "chats: duplicados se eliminan",
        imp.normalizar_chats("-1,-1,-2") == "-1, -2",
        repr(imp.normalizar_chats("-1,-1,-2")),
    )
    check(
        "chats: un solo id sigue siendo valido",
        imp.normalizar_chats("-5148472299") == "-5148472299",
        repr(imp.normalizar_chats("-5148472299")),
    )
    check(
        "chats: numero int -> str",
        imp.normalizar_chats(12345) == "12345",
        repr(imp.normalizar_chats(12345)),
    )


# --------------------------------------------------------------------------- #
# (b) normalizar_cliente(s)
# --------------------------------------------------------------------------- #
def test_normalizar_clientes(check):
    print("(b) normalizar_cliente(s)")
    ora = next(c for c in _CLIENTES if c["nombre"] == "ORA")
    harfuch = next(c for c in _CLIENTES if c["nombre"] == "Harfuch")

    check(
        "cliente: telegram_chat_id singular -> telegram_chat_ids plural",
        ora["telegram_chat_ids"] == "-5148472299",
        repr(ora["telegram_chat_ids"]),
    )
    check(
        "cliente: keywords completas y con acentos",
        ora["keywords"] == ["Óscar Rébora", "@OscarRebora", "SEMA Quintana Roo"],
        repr(ora["keywords"]),
    )
    check(
        "cliente: exclude_terms lista",
        ora["exclude_terms"] == ["hotel", "resort"],
        repr(ora["exclude_terms"]),
    )
    check(
        "cliente: localidad y num_principales",
        ora["localidad"] == "Cancún" and ora["num_principales"] == 10,
        repr((ora["localidad"], ora["num_principales"])),
    )
    check(
        "cliente: sin localidad/num -> '' y 5",
        harfuch["localidad"] == "" and harfuch["num_principales"] == 6,
        repr((harfuch["localidad"], harfuch["num_principales"])),
    )

    minimo = imp.normalizar_cliente("X", {})
    check(
        "cliente: datos vacios -> defaults",
        minimo["keywords"] == []
        and minimo["exclude_terms"] == []
        and minimo["telegram_chat_ids"] == ""
        and minimo["num_principales"] == 5,
        repr(minimo),
    )
    raro = imp.normalizar_cliente("X", {"keywords": '["a", "b"]', "num_principales": "abc"})
    check(
        "cliente: keywords JSON string y num invalido",
        raro["keywords"] == ["a", "b"] and raro["num_principales"] == 5,
        repr(raro),
    )
    check(
        "normalizar_clientes: 2 clientes del ejemplo",
        [c["nombre"] for c in _CLIENTES] == ["ORA", "Harfuch"],
        repr([c["nombre"] for c in _CLIENTES]),
    )
    check(
        "normalizar_clientes: dict no-valido -> []",
        imp.normalizar_clientes(None) == [] and imp.normalizar_clientes("x") == [],
        "",
    )


# --------------------------------------------------------------------------- #
# (c) cargar_clientes_desde_config
# --------------------------------------------------------------------------- #
def test_cargar_config(check):
    print("(c) cargar_clientes_desde_config")
    carpeta = tempfile.mkdtemp(prefix="alertas_cfg_")
    try:
        ruta = _escribir_config(carpeta, _CONFIG_EJEMPLO)
        clientes = imp.cargar_clientes_desde_config(ruta)
        check(
            "config: carga 2 clientes normalizados",
            len(clientes) == 2 and {c["nombre"] for c in clientes} == {"ORA", "Harfuch"},
            repr([c["nombre"] for c in clientes]),
        )
        check(
            "config: acentos UTF-8 conservados",
            any("Óscar Rébora" in c["keywords"] for c in clientes),
            "",
        )

        ruta2 = Path(carpeta) / "sin_alertas.json"
        ruta2.write_text('{"otra": 1}', encoding="utf-8")
        try:
            imp.cargar_clientes_desde_config(str(ruta2))
            check("config: sin alertas_keywords -> ValueError", False)
        except ValueError:
            check("config: sin alertas_keywords -> ValueError", True)

        ruta3 = Path(carpeta) / "roto.json"
        ruta3.write_text("{no-json", encoding="utf-8")
        try:
            imp.cargar_clientes_desde_config(str(ruta3))
            check("config: JSON invalido -> ValueError", False)
        except ValueError:
            check("config: JSON invalido -> ValueError", True)

        try:
            imp.cargar_clientes_desde_config(str(Path(carpeta) / "nada.json"))
            check("config: archivo inexistente -> ValueError", False)
        except ValueError:
            check("config: archivo inexistente -> ValueError", True)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


# --------------------------------------------------------------------------- #
# (d) dry-run / (e) apply / (f) reglas de upsert
# --------------------------------------------------------------------------- #
def test_dry_run(check):
    print("(d) Dry-run: no escribe")
    with _bd_temporal() as sesion:
        resumen = imp.importar_clientes(_CLIENTES, dry_run=True, session_factory=sesion)
        check(
            "dry-run: reporta 2 creados y celula nueva",
            resumen["dry_run"] is True
            and resumen["creados"] == 2
            and resumen["celula_creada"] is True
            and resumen["errores"] == 0,
            repr({k: resumen[k] for k in ("dry_run", "creados", "celula_creada", "errores")}),
        )
        check(
            "dry-run: la BD queda con 0 clientes y 0 celulas",
            len(_clientes_en(sesion)) == 0 and len(_celulas_en(sesion)) == 0,
            f"{len(_clientes_en(sesion))}/{len(_celulas_en(sesion))}",
        )
        check(
            "dry-run: detalle por cliente",
            [d["accion"] for d in resumen["detalle"]] == ["creado", "creado"],
            repr(resumen["detalle"]),
        )


def test_apply(check):
    print("(e) Apply: crea celula + clientes")
    with _bd_temporal() as sesion:
        resumen = imp.importar_clientes(_CLIENTES, dry_run=False, session_factory=sesion)
        check(
            "apply: 2 creados, 0 errores, celula creada",
            resumen["creados"] == 2
            and resumen["errores"] == 0
            and resumen["celula_creada"] is True
            and resumen["celula_id"] is not None,
            repr({k: resumen[k] for k in ("creados", "errores", "celula_creada", "celula_id")}),
        )
        celulas = _celulas_en(sesion)
        check(
            "apply: celula 'Clientes' en la BD",
            len(celulas) == 1 and celulas[0].nombre == "Clientes" and celulas[0].activa is True,
            repr([(c.nombre, c.activa) for c in celulas]),
        )
        clientes = _clientes_en(sesion)
        check(
            "apply: 2 clientes en la BD",
            len(clientes) == 2,
            repr([c.nombre for c in clientes]),
        )
        ora = next(c for c in clientes if c.nombre == "ORA")
        check(
            "apply: ORA keywords JSON roundtrip",
            json.loads(ora.keywords) == ora_keywords(),
            repr(ora.keywords),
        )
        check(
            "apply: ORA localidad/chats/exclude/num/activo",
            ora.localidad == "Cancún"
            and ora.telegram_chat_ids == "-5148472299"
            and json.loads(ora.exclude_terms) == ["hotel", "resort"]
            and ora.num_principales == 10
            and ora.activo is True
            and ora.celula_id == celulas[0].id,
            repr(
                (
                    ora.localidad,
                    ora.telegram_chat_ids,
                    ora.exclude_terms,
                    ora.num_principales,
                    ora.activo,
                )
            ),
        )
        harfuch = next(c for c in clientes if c.nombre == "Harfuch")
        check(
            "apply: Harfuch usa plural con 2 chats",
            harfuch.telegram_chat_ids == "-5256145609, -5291009791",
            repr(harfuch.telegram_chat_ids),
        )

        # Idempotencia: misma entrada -> sin cambios ni duplicados.
        resumen2 = imp.importar_clientes(_CLIENTES, dry_run=False, session_factory=sesion)
        check(
            "apply: segunda pasada = sin_cambios (idempotente)",
            resumen2["sin_cambios"] == 2
            and resumen2["creados"] == 0
            and resumen2["actualizados"] == 0
            and resumen2["celula_creada"] is False,
            repr({k: resumen2[k] for k in ("sin_cambios", "creados", "actualizados", "celula_creada")}),
        )
        check(
            "apply: sigue habiendo 2 clientes (sin duplicados)",
            len(_clientes_en(sesion)) == 2,
            repr([c.nombre for c in _clientes_en(sesion)]),
        )


def ora_keywords():
    return next(c["keywords"] for c in _CLIENTES if c["nombre"] == "ORA")


def test_reglas_upsert(check):
    print("(f) Reglas: vacios no pisan, reactivar, case-insensitive, celula existente")
    with _bd_temporal() as sesion:
        # Celula preexistente reutilizada.
        with sesion() as db:
            db.add(Celula(nombre="Clientes", narrativa="x", activa=True))
        with sesion() as db:
            db.add(
                Cliente(
                    nombre="ora",  # minusculas: el match debe ser case-insensitive
                    celula_id=1,
                    keywords='["Vieja"]',
                    localidad="Vieja",
                    telegram_chat_ids="-999",
                    exclude_terms='["viejo"]',
                    num_principales=9,
                    activo=False,
                )
            )
        resumen = imp.importar_clientes(
            [_CLIENTES[0]], dry_run=False, session_factory=sesion
        )
        check(
            "reglas: celula existente NO se recrea",
            resumen["celula_creada"] is False and resumen["celula_id"] == 1,
            repr((resumen["celula_creada"], resumen["celula_id"])),
        )
        check(
            "reglas: match de nombre case-insensitive (ora vs ORA)",
            resumen["actualizados"] == 1
            and len(_clientes_en(sesion)) == 1
            and resumen["detalle"][0]["accion"] == "actualizado",
            repr(resumen["detalle"]),
        )
        cambios = resumen["detalle"][0]["cambios"]
        check(
            "reglas: actualiza keywords/localidad/chats/exclude/num",
            {"keywords", "localidad", "telegram_chat_ids", "exclude_terms", "num_principales"}
            <= set(cambios),
            repr(sorted(cambios)),
        )
        check(
            "reglas: activo=False se reactiva",
            cambios.get("activo") is True,
            repr(cambios.get("activo")),
        )

        # Origen vacio: NO pisa keywords/chats/localidad/exclude existentes.
        vacio = imp.normalizar_cliente("ORA", {"num_principales": 10})
        resumen_vacio = imp.importar_clientes(
            [vacio], dry_run=False, session_factory=sesion
        )
        check(
            "reglas: origen vacio -> sin cambios (no pisa)",
            resumen_vacio["sin_cambios"] == 1
            and resumen_vacio["actualizados"] == 0,
            repr({k: resumen_vacio[k] for k in ("sin_cambios", "actualizados")}),
        )
        ora = _clientes_en(sesion)[0]
        check(
            "reglas: keywords/localidad/chats/exclude intactos tras vacio",
            json.loads(ora.keywords) == ora_keywords()
            and ora.localidad == "Cancún"
            and ora.telegram_chat_ids == "-5148472299"
            and json.loads(ora.exclude_terms) == ["hotel", "resort"],
            repr((ora.keywords, ora.localidad, ora.telegram_chat_ids, ora.exclude_terms)),
        )


# --------------------------------------------------------------------------- #
# (g) CLI
# --------------------------------------------------------------------------- #
def test_main(check):
    print("(g) CLI main")
    check(
        "main: archivo inexistente -> codigo 1",
        imp.main(["--desde", "no_existe_12345.json"]) == 1,
        "",
    )

    capturado = {}

    def _falso_importar(clientes, celula_nombre=None, dry_run=True, session_factory=None):
        capturado.update(
            {"clientes": clientes, "celula": celula_nombre, "dry_run": dry_run}
        )
        return {
            "dry_run": dry_run,
            "celula": celula_nombre,
            "celula_creada": False,
            "total": len(clientes),
            "creados": 0,
            "actualizados": 0,
            "sin_cambios": len(clientes),
            "errores": 0,
            "detalle": [],
            "error": "",
        }

    with mock.patch.object(imp, "cargar_clientes_desde_config", lambda ruta: _CLIENTES), \
         mock.patch.object(imp, "importar_clientes", _falso_importar):
        codigo = imp.main(["--desde", "x.json", "--celula", "MiCelula", "--json"])
    check(
        "main: reporta y sale con 0 (dry-run)",
        codigo == 0 and capturado.get("dry_run") is True,
        repr((codigo, capturado)),
    )
    check(
        "main: --celula se pasa al importador",
        capturado.get("celula") == "MiCelula",
        repr(capturado.get("celula")),
    )


def run(check):
    test_normalizar_chats(check)
    test_normalizar_clientes(check)
    test_cargar_config(check)
    test_dry_run(check)
    test_apply(check)
    test_reglas_upsert(check)
    test_main(check)


if __name__ == "__main__":
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "run_tests", str(RAIZ / "tests" / "run_tests.py")
    )
    _runner = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_runner)
    try:
        run(_runner.check)
    finally:
        sys.exit(_runner.resumen())
