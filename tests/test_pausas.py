# -*- coding: utf-8 -*-
"""Tests rapidos de `core/pausas.py` (pausa de cuentas para activacion masiva).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: solo se
usan objetos falsos en memoria. La parte estructural se comprueba contra la
metadata de SQLAlchemy (no se abre conexion).

Cubre:
    (1) `esta_pausada`: True/False/1/0/None/"" y cadenas ("1", "true", "si",
        "SI", con espacios), objeto sin el atributo y atributo que lanza.
    (2) `filtrar_para_activacion`: excluye pausadas por defecto, con
        `incluir_pausadas=True` devuelve la lista completa y NUNCA muta la
        original; acepta tuplas/generadores y entradas no iterables.
    (3) `pausadas_usuarios`: objetos sin `.usuario`, con None, vacio o
        espacios; simulacion tipica de exclusion + reporte.
    (4) Tolerancia total: las 3 funciones nunca lanzan con datos raros.
    (5) Estructura: `Cuenta.pausada_activacion` es Boolean default False, la
        migracion `NUEVAS_COLUMNAS_CUENTAS` usa "BOOLEAN DEFAULT FALSE"
        (compatible SQLite+PostgreSQL) y el export incluye
        ("pausada_activacion", "Pausada_Activacion") al final con "Si"/"No".

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_pausas.py   (solo este archivo)
"""
from __future__ import annotations

import sys
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.pausas import (  # noqa: E402
    esta_pausada,
    filtrar_para_activacion,
    pausadas_usuarios,
)


# --------------------------------------------------------------------------- #
# Fakes (nunca se conecta a la BD real)
# --------------------------------------------------------------------------- #
class CuentaFake:
    """Cuenta minima con `.usuario` y `.pausada_activacion`."""

    def __init__(self, usuario="cuenta", pausada=False):
        self.usuario = usuario
        self.pausada_activacion = pausada


class SinAtributo:
    """Objeto que NO tiene el atributo de pausa (cuenta "vieja")."""

    def __init__(self, usuario="sin_attr"):
        self.usuario = usuario


class AtributoQueLanza:
    """`pausada_activacion` que explota al leerse (nunca debe propagarse)."""

    usuario = "explosiva"

    @property
    def pausada_activacion(self):
        raise RuntimeError("boom")


class ExplosivoTotal:
    """Cualquier atributo que se le pida lanza."""

    def __getattr__(self, nombre):
        raise RuntimeError(f"boom:{nombre}")


class UsuarioQueLanza:
    """Pausada, pero leer `.usuario` explota (debe omitirse)."""

    pausada_activacion = True
    usuario = None

    @property
    def usuario(self):  # noqa: F811 - propiedad que lanza
        raise RuntimeError("boom usuario")


class ObjetoRaro:
    """Sin atributos utiles y no iterable."""

    def __iter__(self):
        raise TypeError("no iterable")


# --------------------------------------------------------------------------- #
# (1) esta_pausada
# --------------------------------------------------------------------------- #
def test_esta_pausada(check):
    check("esta_pausada: True -> True", esta_pausada(CuentaFake("a", True)) is True)
    check("esta_pausada: False -> False", esta_pausada(CuentaFake("a", False)) is False)
    check("esta_pausada: 1 -> True", esta_pausada(CuentaFake("a", 1)) is True)
    check("esta_pausada: 0 -> False", esta_pausada(CuentaFake("a", 0)) is False)

    check("esta_pausada: '1' -> True", esta_pausada(CuentaFake("a", "1")) is True)
    check("esta_pausada: 'true' -> True", esta_pausada(CuentaFake("a", "true")) is True)
    check("esta_pausada: 'TRUE' -> True", esta_pausada(CuentaFake("a", "TRUE")) is True)
    check("esta_pausada: 'si' -> True", esta_pausada(CuentaFake("a", "si")) is True)
    check("esta_pausada: 'SI' -> True", esta_pausada(CuentaFake("a", "SI")) is True)
    check("esta_pausada: 'Sí' -> True", esta_pausada(CuentaFake("a", "Sí")) is True)
    check("esta_pausada: ' true ' -> True", esta_pausada(CuentaFake("a", " true ")) is True)

    check("esta_pausada: '0' -> False", esta_pausada(CuentaFake("a", "0")) is False)
    check("esta_pausada: 'false' -> False", esta_pausada(CuentaFake("a", "false")) is False)
    check("esta_pausada: 'no' -> False", esta_pausada(CuentaFake("a", "no")) is False)
    check("esta_pausada: '' -> False", esta_pausada(CuentaFake("a", "")) is False)
    check("esta_pausada: None -> False", esta_pausada(CuentaFake("a", None)) is False)

    check("esta_pausada: objeto sin atributo -> False", esta_pausada(SinAtributo()) is False)
    check("esta_pausada: atributo que lanza -> False", esta_pausada(AtributoQueLanza()) is False)
    check("esta_pausada: __getattr__ que lanza -> False", esta_pausada(ExplosivoTotal()) is False)
    check("esta_pausada: objeto plano -> False", esta_pausada(object()) is False)
    check("esta_pausada: None -> False", esta_pausada(None) is False)


# --------------------------------------------------------------------------- #
# (2) filtrar_para_activacion
# --------------------------------------------------------------------------- #
def test_filtrar_para_activacion(check):
    pausada = CuentaFake("pausada", True)
    activa = CuentaFake("activa", False)
    vieja = SinAtributo("vieja")
    original = [pausada, activa, vieja]

    elegibles = filtrar_para_activacion(original)
    check(
        "filtrar: default quita la pausada y conserva orden",
        [c.usuario for c in elegibles] == ["activa", "vieja"],
    )
    check("filtrar: default devuelve list", isinstance(elegibles, list))
    check("filtrar: default devuelve lista NUEVA", elegibles is not original)
    check(
        "filtrar: NO muta la lista original",
        original == [pausada, activa, vieja] and len(original) == 3,
    )

    completas = filtrar_para_activacion(original, incluir_pausadas=True)
    check(
        "filtrar: incluir_pausadas=True devuelve la lista completa",
        completas == original,
    )
    check("filtrar: incluir_pausadas=True es lista nueva", completas is not original)
    check("filtrar: incluir=False explicito excluye", filtrar_para_activacion(original, False) == [activa, vieja])

    check(
        "filtrar: acepta tupla y devuelve list",
        filtrar_para_activacion((pausada, activa)) == [activa],
    )
    generador = (c for c in (pausada, activa, vieja))
    check(
        "filtrar: acepta generador",
        [c.usuario for c in filtrar_para_activacion(generador)] == ["activa", "vieja"],
    )
    check("filtrar: None -> []", filtrar_para_activacion(None) == [])
    check("filtrar: no iterable -> []", filtrar_para_activacion(3.14) == [])


# --------------------------------------------------------------------------- #
# (3) pausadas_usuarios
# --------------------------------------------------------------------------- #
def test_pausadas_usuarios(check):
    cuentas = [
        CuentaFake("uno", True),
        CuentaFake("dos", False),
        SinAtributo("tres"),
        CuentaFake(" cuatro ", True),
        CuentaFake("", True),
        CuentaFake(None, True),
        UsuarioQueLanza(),
        AtributoQueLanza(),
        ExplosivoTotal(),
    ]
    usuarios = pausadas_usuarios(cuentas)
    check("pausadas_usuarios: solo pausadas con usuario valido", usuarios == ["uno", "cuatro"], str(usuarios))
    check("pausadas_usuarios: devuelve list[str]", isinstance(usuarios, list) and all(isinstance(u, str) for u in usuarios))
    check("pausadas_usuarios: sin pausadas -> []", pausadas_usuarios([CuentaFake("a", False)]) == [])
    check("pausadas_usuarios: None -> []", pausadas_usuarios(None) == [])
    check("pausadas_usuarios: no iterable -> []", pausadas_usuarios(ObjetoRaro()) == [])
    check(
        "pausadas_usuarios: no muta la lista original",
        len(cuentas) == 9 and cuentas[0].usuario == "uno",
    )


# --------------------------------------------------------------------------- #
# (4) Tolerancia total (nunca lanzan)
# --------------------------------------------------------------------------- #
def test_tolerancia(check):
    raros = [None, "", 0, 1, "texto", 3.14, object(), ExplosivoTotal(), AtributoQueLanza(), {"a": 1}, [1, 2]]
    ok = True
    for raro in raros:
        try:
            esta_pausada(raro)
            filtrar_para_activacion(raro)
            pausadas_usuarios(raro)
            filtrar_para_activacion(raro, incluir_pausadas=True)
        except Exception as e:  # nunca debe pasar
            ok = False
            print(f"    lazo: {raro!r} -> {e!r}")
    check("tolerancia: las 3 funciones nunca lanzan con datos raros", ok)
    check("tolerancia: esta_pausada siempre devuelve bool", all(isinstance(esta_pausada(r), bool) for r in raros))
    check(
        "tolerancia: filtrar siempre devuelve list",
        all(isinstance(filtrar_para_activacion(r), list) for r in raros),
    )
    check(
        "tolerancia: pausadas_usuarios siempre devuelve list",
        all(isinstance(pausadas_usuarios(r), list) for r in raros),
    )


# --------------------------------------------------------------------------- #
# (5) Estructura: modelo, migracion y export
# --------------------------------------------------------------------------- #
def test_estructura(check):
    from sqlalchemy import Boolean

    import core.database as database
    from core.exportar import COLUMNAS_CUENTAS, _formatear_valor_cuenta
    from core.models import Cuenta

    columna = Cuenta.__table__.columns.get("pausada_activacion")
    check("modelo: Cuenta.pausada_activacion existe", columna is not None)
    check(
        "modelo: el tipo es Boolean",
        columna is not None and isinstance(columna.type, Boolean),
    )
    check(
        "modelo: default False",
        columna is not None
        and columna.default is not None
        and columna.default.arg is False,
    )

    tipo = database.NUEVAS_COLUMNAS_CUENTAS.get("pausada_activacion", "")
    check("migracion: registra pausada_activacion", bool(tipo), tipo)
    check(
        "migracion: tipo compatible SQLite+PostgreSQL",
        tipo.upper() == "BOOLEAN DEFAULT FALSE",
        tipo,
    )

    par = ("pausada_activacion", "Pausada_Activacion")
    check("export: incluye Pausada_Activacion", par in COLUMNAS_CUENTAS)
    check("export: es la ultima columna", COLUMNAS_CUENTAS[-1] == par)
    check(
        "export: True -> 'Sí'",
        _formatear_valor_cuenta("pausada_activacion", True) == "Sí",
    )
    check(
        "export: 1 -> 'Sí'",
        _formatear_valor_cuenta("pausada_activacion", 1) == "Sí",
    )
    check(
        "export: 0 -> 'No'",
        _formatear_valor_cuenta("pausada_activacion", 0) == "No",
    )
    check(
        "export: None -> 'No'",
        _formatear_valor_cuenta("pausada_activacion", None) == "No",
    )
    check(
        "export: otras columnas conservan su valor",
        _formatear_valor_cuenta("usuario", "abc") == "abc"
        and _formatear_valor_cuenta("password", None) == "",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_esta_pausada(check)
    test_filtrar_para_activacion(check)
    test_pausadas_usuarios(check)
    test_tolerancia(check)
    test_estructura(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_pausas.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
