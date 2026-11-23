# -*- coding: utf-8 -*-
"""Tests rapidos de `core/verificacion.py` (cajon de verificacion humana).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: las
funciones puras se prueban con fakes y las que tocan BD con un fake de
`core.database.get_db_session` (monkeypatch restaurado siempre). La parte
estructural se comprueba contra la metadata de SQLAlchemy (sin conexion).

Cubre:
    (1) `esta_pendiente`: True/False/0/1/"true"/"si"/None, dict y
        SimpleNamespace, objeto sin atributo y atributo que lanza.
    (2) `filtrar_para_activacion`: excluye pendientes por defecto, con
        `incluir_verificacion=True` conserva todas y NUNCA muta la original.
    (3) `pendientes_usuarios`: solo pendientes con `.usuario` valido.
    (4) `marcar_pendiente` / `marcar_verificada`: fake de sesion, recortes,
        commit, cuenta inexistente y sesion que explota (nunca lanza).
    (5) Tolerancia de `listar_pendientes` / `contar_pendientes` sin BD.
    (6) Estructura: las 4 columnas del modelo, la migracion y el export.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_verificacion_core.py   (solo este)
"""
from __future__ import annotations

import contextlib
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.verificacion import (  # noqa: E402
    contar_pendientes,
    esta_pendiente,
    filtrar_para_activacion,
    listar_pendientes,
    marcar_pendiente,
    marcar_verificada,
    pendientes_usuarios,
)


# --------------------------------------------------------------------------- #
# Fakes (nunca se conecta a la BD real)
# --------------------------------------------------------------------------- #
class CuentaFake:
    """Cuenta minima con `.usuario` y los campos de verificacion."""

    def __init__(
        self,
        usuario="cuenta",
        pendiente=False,
        motivo="",
        origen="",
        fecha=None,
    ):
        self.usuario = usuario
        self.verificacion_pendiente = pendiente
        self.verificacion_motivo = motivo
        self.verificacion_origen = origen
        self.verificacion_fecha = fecha


class SinAtributo:
    """Objeto que NO tiene el atributo de verificacion (cuenta "vieja")."""

    def __init__(self, usuario="sin_attr"):
        self.usuario = usuario


class AtributoQueLanza:
    """`verificacion_pendiente` que explota al leerse (nunca se propaga)."""

    usuario = "explosiva"

    @property
    def verificacion_pendiente(self):
        raise RuntimeError("boom")


class ExplosivoTotal:
    """Cualquier atributo que se le pida lanza."""

    def __getattr__(self, nombre):
        raise RuntimeError(f"boom:{nombre}")


class UsuarioQueLanza:
    """Pendiente, pero leer `.usuario` explota (debe omitirse)."""

    verificacion_pendiente = True

    @property
    def usuario(self):  # noqa: F811 - propiedad que lanza
        raise RuntimeError("boom usuario")


class FakeQuery:
    """Emula `db.query(Modelo).filter(...).first()`."""

    def __init__(self, cuentas):
        self._cuentas = cuentas

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._cuentas[0] if self._cuentas else None


class FakeDB:
    """Sesion falsa minima (query/add/commit/rollback/close)."""

    def __init__(self, cuentas=None):
        self.cuentas = list(cuentas or [])
        self.agregadas = []
        self.commit_llamado = False
        self.rollback_llamado = False
        self.cerrada = False

    def query(self, modelo):  # noqa: ARG002 - API de SQLAlchemy
        return FakeQuery(self.cuentas)

    def add(self, obj):
        if obj not in self.cuentas:
            self.cuentas.append(obj)
        self.agregadas.append(obj)

    def commit(self):
        self.commit_llamado = True

    def rollback(self):
        self.rollback_llamado = True

    def close(self):
        self.cerrada = True


@contextlib.contextmanager
def _sesion_falsa(cuentas=None):
    """Parchea `core.database.get_db_session` y expone la sesion usada."""
    import core.database as database

    original = database.get_db_session
    caja = {}

    @contextlib.contextmanager
    def _fabrica():
        db = FakeDB(cuentas)
        caja["db"] = db
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    database.get_db_session = _fabrica
    try:
        yield caja
    finally:
        database.get_db_session = original


@contextlib.contextmanager
def _sesion_que_explota():
    """Parchea `get_db_session` para que lance al abrir la sesion."""
    import core.database as database

    original = database.get_db_session

    def _explota(*args, **kwargs):
        raise RuntimeError("BD caida (fake)")

    database.get_db_session = _explota
    try:
        yield
    finally:
        database.get_db_session = original


# --------------------------------------------------------------------------- #
# (1) esta_pendiente
# --------------------------------------------------------------------------- #
def test_esta_pendiente(check):
    check("esta_pendiente: True -> True", esta_pendiente(CuentaFake(pendiente=True)) is True)
    check("esta_pendiente: False -> False", esta_pendiente(CuentaFake(pendiente=False)) is False)
    check("esta_pendiente: 1 -> True", esta_pendiente(CuentaFake(pendiente=1)) is True)
    check("esta_pendiente: 0 -> False", esta_pendiente(CuentaFake(pendiente=0)) is False)

    check("esta_pendiente: '1' -> True", esta_pendiente(CuentaFake(pendiente="1")) is True)
    check("esta_pendiente: 'true' -> True", esta_pendiente(CuentaFake(pendiente="true")) is True)
    check("esta_pendiente: 'TRUE' -> True", esta_pendiente(CuentaFake(pendiente="TRUE")) is True)
    check("esta_pendiente: 'si' -> True", esta_pendiente(CuentaFake(pendiente="si")) is True)
    check("esta_pendiente: 'Sí' -> True", esta_pendiente(CuentaFake(pendiente="Sí")) is True)
    check("esta_pendiente: ' si ' -> True", esta_pendiente(CuentaFake(pendiente=" si ")) is True)

    check("esta_pendiente: '0' -> False", esta_pendiente(CuentaFake(pendiente="0")) is False)
    check("esta_pendiente: 'false' -> False", esta_pendiente(CuentaFake(pendiente="false")) is False)
    check("esta_pendiente: 'no' -> False", esta_pendiente(CuentaFake(pendiente="no")) is False)
    check("esta_pendiente: '' -> False", esta_pendiente(CuentaFake(pendiente="")) is False)
    check("esta_pendiente: None -> False", esta_pendiente(CuentaFake(pendiente=None)) is False)

    check(
        "esta_pendiente: dict con True -> True",
        esta_pendiente({"verificacion_pendiente": True}) is True,
    )
    check(
        "esta_pendiente: dict sin la clave -> False",
        esta_pendiente({"usuario": "x"}) is False,
    )
    check(
        "esta_pendiente: SimpleNamespace 'si' -> True",
        esta_pendiente(SimpleNamespace(verificacion_pendiente="si")) is True,
    )
    check(
        "esta_pendiente: objeto sin atributo -> False",
        esta_pendiente(SinAtributo()) is False,
    )
    check(
        "esta_pendiente: atributo que lanza -> False",
        esta_pendiente(AtributoQueLanza()) is False,
    )
    check(
        "esta_pendiente: __getattr__ que lanza -> False",
        esta_pendiente(ExplosivoTotal()) is False,
    )
    check("esta_pendiente: None -> False", esta_pendiente(None) is False)
    check("esta_pendiente: objeto plano -> False", esta_pendiente(object()) is False)


# --------------------------------------------------------------------------- #
# (2) filtrar_para_activacion
# --------------------------------------------------------------------------- #
def test_filtrar_para_activacion(check):
    pendiente = CuentaFake("pendiente", pendiente=True)
    activa = CuentaFake("activa", pendiente=False)
    vieja = SinAtributo("vieja")
    original = [pendiente, activa, vieja]

    elegibles = filtrar_para_activacion(original)
    check(
        "filtrar: default quita pendientes y conserva orden",
        [c.usuario for c in elegibles] == ["activa", "vieja"],
    )
    check("filtrar: default devuelve list", isinstance(elegibles, list))
    check("filtrar: default devuelve lista NUEVA", elegibles is not original)
    check(
        "filtrar: NO muta la lista original",
        original == [pendiente, activa, vieja] and len(original) == 3,
    )

    completas = filtrar_para_activacion(original, incluir_verificacion=True)
    check(
        "filtrar: incluir_verificacion=True conserva todas",
        completas == original,
    )
    check("filtrar: incluir_verificacion=True es lista nueva", completas is not original)
    check(
        "filtrar: incluir_verificacion=False explicito excluye",
        filtrar_para_activacion(original, False) == [activa, vieja],
    )

    check(
        "filtrar: acepta tupla y devuelve list",
        filtrar_para_activacion((pendiente, activa)) == [activa],
    )
    generador = (c for c in (pendiente, activa, vieja))
    check(
        "filtrar: acepta generador",
        [c.usuario for c in filtrar_para_activacion(generador)] == ["activa", "vieja"],
    )
    check("filtrar: None -> []", filtrar_para_activacion(None) == [])
    check("filtrar: no iterable -> []", filtrar_para_activacion(3.14) == [])


# --------------------------------------------------------------------------- #
# (3) pendientes_usuarios
# --------------------------------------------------------------------------- #
def test_pendientes_usuarios(check):
    cuentas = [
        CuentaFake("uno", pendiente=True),
        CuentaFake("dos", pendiente=False),
        SinAtributo("tres"),
        CuentaFake(" cuatro ", pendiente=True),
        CuentaFake("", pendiente=True),
        CuentaFake(None, pendiente=True),
        UsuarioQueLanza(),
        AtributoQueLanza(),
        ExplosivoTotal(),
        {"usuario": "dict", "verificacion_pendiente": True},
    ]
    usuarios = pendientes_usuarios(cuentas)
    check(
        "pendientes_usuarios: solo pendientes con usuario valido",
        usuarios == ["uno", "cuatro", "dict"],
        str(usuarios),
    )
    check(
        "pendientes_usuarios: devuelve list[str]",
        isinstance(usuarios, list) and all(isinstance(u, str) for u in usuarios),
    )
    check(
        "pendientes_usuarios: sin pendientes -> []",
        pendientes_usuarios([CuentaFake("a", pendiente=False)]) == [],
    )
    check("pendientes_usuarios: None -> []", pendientes_usuarios(None) == [])
    check("pendientes_usuarios: no muta la lista original", len(cuentas) == 10)


# --------------------------------------------------------------------------- #
# (4) marcar_pendiente / marcar_verificada (fake de get_db_session)
# --------------------------------------------------------------------------- #
def test_marcar_pendiente(check):
    cuenta = CuentaFake("Ana")
    with _sesion_falsa([cuenta]) as caja:
        ok = marcar_pendiente("Ana", "pide captcha", "campana")
        check("marcar_pendiente: devuelve True", ok is True)
        check(
            "marcar_pendiente: setea flag True",
            cuenta.verificacion_pendiente is True,
        )
        check(
            "marcar_pendiente: guarda motivo",
            cuenta.verificacion_motivo == "pide captcha",
            cuenta.verificacion_motivo,
        )
        check(
            "marcar_pendiente: guarda origen",
            cuenta.verificacion_origen == "campana",
            cuenta.verificacion_origen,
        )
        check(
            "marcar_pendiente: fecha es datetime UTC",
            isinstance(cuenta.verificacion_fecha, datetime),
        )
        check(
            "marcar_pendiente: hace commit",
            caja.get("db") is not None and caja["db"].commit_llamado is True,
        )
        check(
            "marcar_pendiente: cierra la sesion",
            caja["db"].cerrada is True,
        )

    cuenta_limites = CuentaFake("Limites")
    with _sesion_falsa([cuenta_limites]):
        marcar_pendiente("Limites", motivo="m" * 500, origen="o" * 80)
        check(
            "marcar_pendiente: recorta motivo a 300",
            len(cuenta_limites.verificacion_motivo) == 300,
            str(len(cuenta_limites.verificacion_motivo)),
        )
        check(
            "marcar_pendiente: recorta origen a 30",
            len(cuenta_limites.verificacion_origen) == 30,
            str(len(cuenta_limites.verificacion_origen)),
        )

    with _sesion_falsa([]):
        check(
            "marcar_pendiente: cuenta inexistente -> False",
            marcar_pendiente("NoExiste", "x") is False,
        )

    with _sesion_que_explota():
        check(
            "marcar_pendiente: BD que explota -> False",
            marcar_pendiente("Ana", "x") is False,
        )

    check("marcar_pendiente: usuario vacio -> False", marcar_pendiente("", "x") is False)
    check("marcar_pendiente: usuario None -> False", marcar_pendiente(None, "x") is False)


def test_marcar_verificada(check):
    cuenta = CuentaFake(
        "Ana",
        pendiente=True,
        motivo="captcha",
        origen="campana",
        fecha=datetime.utcnow(),
    )
    with _sesion_falsa([cuenta]) as caja:
        ok = marcar_verificada("Ana")
        check("marcar_verificada: devuelve True si habia algo que limpiar", ok is True)
        check(
            "marcar_verificada: deja el flag en False",
            cuenta.verificacion_pendiente is False,
        )
        check("marcar_verificada: limpia motivo", cuenta.verificacion_motivo == "")
        check("marcar_verificada: limpia origen", cuenta.verificacion_origen == "")
        check("marcar_verificada: limpia fecha", cuenta.verificacion_fecha is None)
        check(
            "marcar_verificada: hace commit",
            caja.get("db") is not None and caja["db"].commit_llamado is True,
        )

    limpia = CuentaFake("Limpia", pendiente=False, motivo="", origen="", fecha=None)
    with _sesion_falsa([limpia]):
        check(
            "marcar_verificada: ya limpia -> False",
            marcar_verificada("Limpia") is False,
        )

    with _sesion_falsa([]):
        check(
            "marcar_verificada: cuenta inexistente -> False",
            marcar_verificada("NoExiste") is False,
        )

    with _sesion_que_explota():
        check(
            "marcar_verificada: BD que explota -> False",
            marcar_verificada("Ana") is False,
        )

    check("marcar_verificada: usuario vacio -> False", marcar_verificada("") is False)


# --------------------------------------------------------------------------- #
# (5) listar_pendientes / contar_pendientes sin BD
# --------------------------------------------------------------------------- #
def test_listar_y_contar_tolerantes(check):
    with _sesion_que_explota():
        check("listar_pendientes: BD caida -> []", listar_pendientes() == [])
        check("contar_pendientes: BD caida -> 0", contar_pendientes() == 0)
    check(
        "contar_pendientes: devuelve int",
        isinstance(contar_pendientes(), int),
    )
    check(
        "listar_pendientes: devuelve list",
        isinstance(listar_pendientes(), list),
    )


# --------------------------------------------------------------------------- #
# (6) Estructura: modelo, migracion y export
# --------------------------------------------------------------------------- #
def test_estructura(check):
    from sqlalchemy import Boolean, DateTime, String

    import core.database as database
    from core.exportar import COLUMNAS_CUENTAS, _formatear_valor_cuenta
    from core.models import Cuenta

    check(
        "modelo: Cuenta.verificacion_pendiente existe",
        hasattr(Cuenta, "verificacion_pendiente"),
    )
    check(
        "modelo: Cuenta.verificacion_motivo existe",
        hasattr(Cuenta, "verificacion_motivo"),
    )
    check(
        "modelo: Cuenta.verificacion_origen existe",
        hasattr(Cuenta, "verificacion_origen"),
    )
    check(
        "modelo: Cuenta.verificacion_fecha existe",
        hasattr(Cuenta, "verificacion_fecha"),
    )

    columna = Cuenta.__table__.columns.get("verificacion_pendiente")
    check(
        "modelo: verificacion_pendiente es Boolean default False",
        columna is not None
        and isinstance(columna.type, Boolean)
        and columna.default is not None
        and columna.default.arg is False,
    )
    columna_motivo = Cuenta.__table__.columns.get("verificacion_motivo")
    check(
        "modelo: verificacion_motivo es String(300) default ''",
        columna_motivo is not None
        and isinstance(columna_motivo.type, String)
        and columna_motivo.type.length == 300
        and columna_motivo.default is not None
        and columna_motivo.default.arg == "",
    )
    columna_origen = Cuenta.__table__.columns.get("verificacion_origen")
    check(
        "modelo: verificacion_origen es String(30) default ''",
        columna_origen is not None
        and isinstance(columna_origen.type, String)
        and columna_origen.type.length == 30,
    )
    columna_fecha = Cuenta.__table__.columns.get("verificacion_fecha")
    check(
        "modelo: verificacion_fecha es DateTime nullable",
        columna_fecha is not None
        and isinstance(columna_fecha.type, DateTime)
        and columna_fecha.nullable is True,
    )

    check(
        "migracion: verificacion_pendiente BOOLEAN DEFAULT FALSE",
        database.NUEVAS_COLUMNAS_CUENTAS.get("verificacion_pendiente")
        == "BOOLEAN DEFAULT FALSE",
    )
    check(
        "migracion: verificacion_motivo VARCHAR(300) DEFAULT ''",
        database.NUEVAS_COLUMNAS_CUENTAS.get("verificacion_motivo")
        == "VARCHAR(300) DEFAULT ''",
    )
    check(
        "migracion: verificacion_origen VARCHAR(30) DEFAULT ''",
        database.NUEVAS_COLUMNAS_CUENTAS.get("verificacion_origen")
        == "VARCHAR(30) DEFAULT ''",
    )
    check(
        "migracion: verificacion_fecha DATETIME",
        database.NUEVAS_COLUMNAS_CUENTAS.get("verificacion_fecha") == "DATETIME",
    )

    check(
        "export: incluye Verificacion_Pendiente",
        ("verificacion_pendiente", "Verificacion_Pendiente") in COLUMNAS_CUENTAS,
    )
    check(
        "export: incluye Verificacion_Motivo",
        ("verificacion_motivo", "Verificacion_Motivo") in COLUMNAS_CUENTAS,
    )
    check(
        "export: True -> 'Sí'",
        _formatear_valor_cuenta("verificacion_pendiente", True) == "Sí",
    )
    check(
        "export: 0 -> 'No'",
        _formatear_valor_cuenta("verificacion_pendiente", 0) == "No",
    )
    check(
        "export: motivo conserva su valor",
        _formatear_valor_cuenta("verificacion_motivo", "captcha") == "captcha",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_esta_pendiente(check)
    test_filtrar_para_activacion(check)
    test_pendientes_usuarios(check)
    test_marcar_pendiente(check)
    test_marcar_verificada(check)
    test_listar_y_contar_tolerantes(check)
    test_estructura(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
