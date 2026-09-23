# -*- coding: utf-8 -*-
"""Tests rapidos del TOPE DIARIO total por cuenta (capa core).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: el bloque
de conteo usa una SQLite temporal en `tempfile.mkdtemp()` y reemplaza
`core.registro.get_db_session` por una sesion de esa base (se restaura en el
`finally` aunque algo falle).

Cubre:
    (1) defaults de `core.config.settings` (12 acciones, ventana 1440 min,
        interruptor ON) y `limite_acciones_dia()`: interruptor apagado -> 0,
        valores invalidos/<=0 -> 0 sin lanzar.
    (2) `ACCIONES_OPERATIVAS`: union deduplicada de TODOS los tipos de
        `ACCIONES_POR_ROL` (4 roles + alias) y sin tipos ajenos.
    (3) `contar_acciones_dia`: solo exitos, solo tipos operativos, ventana
        diaria por defecto 1440, `minutos` explicito manda y `minutos`
        invalido cae al default.
    (4) `contar_acciones_dia_por_usuario`: UNA consulta agrupada, solo
        entradas > 0, usuarios no pedidos fuera, lista vacia/None -> {}.
    (5) robustez: `get_db_session` que lanza -> 0 y {} sin propagar.
    (6) alias real `CUOTAS_DIARIAS_ACTIVO` en `Settings` (pydantic-settings v2
        ignora `env=` en Field): instanciacion con env 0/true,
        `LIMITE_ACCIONES_DIA=7` (alias legado) y `LIMITE_DIARIO_POR_CUENTA=9`
        (nombre principal, le gana al legado).
    (7) tiers (`core.tiers`): `normalizar_tier` con todas las variantes
        (acentos, mayusculas, etiquetas completas), `etiqueta_tier`,
        `tier_de_cuenta` con objetos fake y tolerancia a objetos explosivos.
    (8) `rol_permitido_tier` / `error_rol_tier`: Tier 2 tiene PROHIBIDO
        "hashtags" (y "post"/"publicacion"/... porque normalizan a "hashtags"),
        pero SI puede rt/cita/comentario; Tier 1 y tier vacio sin restriccion.
    (9) `esta_agotada_dia` / `usuarios_agotados_dia` con conteos y tope
        simulados: 12 -> agotada, 11 -> no; tope 0 o interruptor apagado ->
        nunca agotada; `limite` explicito > 0 manda.
    (10) migracion automatica de `tier_calidad` en una SQLite temporal (mismo
        `core.database._migrar_columnas`), columna en el modelo `Cuenta` y en
        la exportacion (`Tier_Calidad`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_cuotas_diarias_core.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from pydantic import AliasChoices  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.registro as registro  # noqa: E402
from core.config import settings  # noqa: E402
from core.exportar import COLUMNAS_CUENTAS  # noqa: E402
from core.models import Base, Cuenta, RegistroAccion  # noqa: E402
from core.registro import (  # noqa: E402
    ACCIONES_OPERATIVAS,
    contar_acciones_dia,
    contar_acciones_dia_por_usuario,
    esta_agotada_dia,
    limite_acciones_dia,
    usuarios_agotados_dia,
)
from core.tiers import (  # noqa: E402
    ROL_PROHIBIDO_TIER2,
    TIERS,
    error_rol_tier,
    es_tier2,
    etiqueta_tier,
    normalizar_tier,
    rol_permitido_tier,
    tier_de_cuenta,
)

# Tipos documentados del tope diario (4 roles + alias).
TIPOS_ESPERADOS = (
    "post",
    "hashtags",
    "publicacion",
    "mantenimiento",
    "calentamiento",
    "hilo",
    "cita",
    "quote",
    "rt",
    "retweet",
    "repost",
    "comentario",
    "respuesta",
    "reply",
)


# --------------------------------------------------------------------------- #
# Base SQLite temporal aislada (nunca la BD real)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _base_temporal():
    """Crea una SQLite temporal y apunta `core.registro.get_db_session` ahi.

    Devuelve el `sessionmaker` de esa base para inserts directos. Al salir
    restaura la funcion original de `core.registro` (aunque el test falle),
    cierra el engine y borra la carpeta temporal."""
    carpeta = tempfile.mkdtemp(prefix="cuotas_diarias_")
    ruta = Path(carpeta) / "cuotas_diarias_test.db"
    engine = create_engine(
        f"sqlite:///{ruta}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    original = registro.get_db_session

    @contextlib.contextmanager
    def _sesion_temporal():
        db = maker()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    registro.get_db_session = _sesion_temporal
    try:
        yield maker
    finally:
        registro.get_db_session = original
        engine.dispose()
        shutil.rmtree(carpeta, ignore_errors=True)


def _insertar_directo(maker, usuario, tipo, estado="exito", fecha=None):
    """Inserta una fila con `fecha` exacta (para probar la ventana)."""
    with maker() as db:
        db.add(
            RegistroAccion(
                usuario=usuario,
                tipo=tipo,
                estado=estado,
                fecha=fecha or datetime.utcnow(),
            )
        )
        db.commit()


def _filas(maker) -> int:
    """Numero de filas de la BD temporal (solo para verificar el setup)."""
    with maker() as db:
        return int(db.query(RegistroAccion).count())


# --------------------------------------------------------------------------- #
# (1) defaults de settings y limite_acciones_dia()
# --------------------------------------------------------------------------- #
def test_limite_diario(check):
    print("(1) defaults de settings y limite_acciones_dia()")
    check(
        "defaults: limite_acciones_dia=12",
        settings.limite_acciones_dia == 12,
        f"(={settings.limite_acciones_dia})",
    )
    check(
        "defaults: limite_dia_ventana_min=1440",
        settings.limite_dia_ventana_min == 1440,
        f"(={settings.limite_dia_ventana_min})",
    )
    check("defaults: limite_diario_activo=True", settings.limite_diario_activo is True)
    check(
        "limite_acciones_dia() lee el default (12)",
        limite_acciones_dia() == 12,
        f"(={limite_acciones_dia()})",
    )

    original_activo = settings.limite_diario_activo
    try:
        settings.limite_diario_activo = False
        check(
            "interruptor diario apagado -> 0 (ilimitado)",
            limite_acciones_dia() == 0,
            f"(={limite_acciones_dia()})",
        )
    finally:
        settings.limite_diario_activo = original_activo
    check(
        "interruptor restaurado y el limite vuelve a 12",
        settings.limite_diario_activo == original_activo
        and limite_acciones_dia() == 12,
    )

    original_limite = settings.limite_acciones_dia
    try:
        settings.limite_acciones_dia = "20"
        check("valor numerico en texto ('20') -> 20", limite_acciones_dia() == 20)
        settings.limite_acciones_dia = 0
        check("valor 0 -> 0 (ilimitado)", limite_acciones_dia() == 0)
        settings.limite_acciones_dia = -5
        check("valor negativo -> 0", limite_acciones_dia() == 0)
        settings.limite_acciones_dia = "abc"
        check("valor invalido ('abc') -> 0 y no lanza", limite_acciones_dia() == 0)
        settings.limite_acciones_dia = None
        check("valor None -> 0 y no lanza", limite_acciones_dia() == 0)

        settings.limite_acciones_dia = 7
        settings.limite_diario_activo = False
        check(
            "apagado gana aun con un valor valido -> 0",
            limite_acciones_dia() == 0,
        )
    finally:
        settings.limite_acciones_dia = original_limite
        settings.limite_diario_activo = original_activo
    check(
        "configuracion restaurada (limite_acciones_dia)",
        settings.limite_acciones_dia == original_limite
        and limite_acciones_dia() == 12,
    )


# --------------------------------------------------------------------------- #
# (2) ACCIONES_OPERATIVAS
# --------------------------------------------------------------------------- #
def test_acciones_operativas(check):
    print("(2) ACCIONES_OPERATIVAS: union completa de los 4 roles + alias")
    tipos_roles = {
        tipo for tipos in registro.ACCIONES_POR_ROL.values() for tipo in tipos
    }
    check(
        "es la union de TODOS los tipos de ACCIONES_POR_ROL",
        set(ACCIONES_OPERATIVAS) == tipos_roles,
        f"(faltan: {sorted(tipos_roles - set(ACCIONES_OPERATIVAS))})",
    )
    check(
        "sin duplicados",
        len(ACCIONES_OPERATIVAS) == len(set(ACCIONES_OPERATIVAS)),
        f"({len(ACCIONES_OPERATIVAS)} tipos)",
    )
    check(
        "son los 14 tipos documentados",
        set(ACCIONES_OPERATIVAS) == set(TIPOS_ESPERADOS),
        f"({len(ACCIONES_OPERATIVAS)})",
    )
    for tipo in TIPOS_ESPERADOS:
        check(f"contiene {tipo!r}", tipo in ACCIONES_OPERATIVAS)
    check(
        "alias de cita: cita y quote",
        {"cita", "quote"} <= set(ACCIONES_OPERATIVAS),
    )
    check(
        "alias de rt: rt, retweet y repost",
        {"rt", "retweet", "repost"} <= set(ACCIONES_OPERATIVAS),
    )
    check(
        "alias de comentario: comentario, respuesta y reply",
        {"comentario", "respuesta", "reply"} <= set(ACCIONES_OPERATIVAS),
    )
    check(
        "alias de hashtags: post, hashtags, publicacion, mantenimiento, calentamiento e hilo",
        {"post", "hashtags", "publicacion", "mantenimiento", "calentamiento", "hilo"}
        <= set(ACCIONES_OPERATIVAS),
    )
    check(
        "NO incluye tipos ajenos (like/activacion/megafono)",
        not ({"like", "activacion", "megafono"} & set(ACCIONES_OPERATIVAS)),
    )


# --------------------------------------------------------------------------- #
# (3) contar_acciones_dia
# --------------------------------------------------------------------------- #
def test_conteo_dia(check):
    print("(3) contar_acciones_dia: exitos, tipos operativos y ventana diaria")
    with _base_temporal() as maker:
        registrar = registro.registrar_accion
        registrar("dia_uno", "post", "exito")
        registrar("dia_uno", "hashtags", "ok")
        registrar("dia_uno", "cita", "exitoso")
        registrar("dia_uno", "retweet", "exito")
        registrar("dia_uno", "comentario", "exito")
        registrar("dia_uno", "respuesta", "ok")
        registrar("dia_uno", "quote", "exito")
        registrar("dia_uno", "post", "fallido")  # NO debe contar
        registrar("dia_uno", "like", "exito")  # tipo operativo? NO -> se ignora
        registrar("otro_dia", "post", "exito")  # otro usuario
        _insertar_directo(
            maker, "dia_uno", "post", "exito", datetime.utcnow() - timedelta(hours=2)
        )
        _insertar_directo(
            maker,
            "dia_uno",
            "rt",
            "exito",
            datetime.utcnow() - timedelta(hours=24, minutes=30),
        )

        check("BD temporal: 12 filas insertadas", _filas(maker) == 12)

        check(
            "default (1440 min): 8 exitos operativos (excluye el de 24.5 h)",
            contar_acciones_dia("dia_uno") == 8,
            f"(={contar_acciones_dia('dia_uno')})",
        )
        check(
            "las fallidas no cuentan y los tipos ajenos (like) tampoco",
            contar_acciones_dia("dia_uno", minutos=1440) == 8,
        )
        check(
            "minutos=60 excluye la fila de hace 2 h -> 7",
            contar_acciones_dia("dia_uno", minutos=60) == 7,
            f"(={contar_acciones_dia('dia_uno', minutos=60)})",
        )
        check(
            "minutos=1500 incluye la de 24.5 h -> 9",
            contar_acciones_dia("dia_uno", minutos=1500) == 9,
            f"(={contar_acciones_dia('dia_uno', minutos=1500)})",
        )
        check(
            "minutos invalido (None/0/'abc'/-3) usa la ventana diaria (8)",
            contar_acciones_dia("dia_uno", minutos=None) == 8
            and contar_acciones_dia("dia_uno", minutos=0) == 8
            and contar_acciones_dia("dia_uno", minutos="abc") == 8
            and contar_acciones_dia("dia_uno", minutos=-3) == 8,
        )
        check(
            "otro usuario no se mezcla (otro_dia=1)",
            contar_acciones_dia("otro_dia") == 1
            and contar_acciones_dia("dia_uno") == 8,
        )
        check(
            "usuario sin acciones -> 0",
            contar_acciones_dia("nadie") == 0,
        )
        check(
            "usuario vacio/None -> 0",
            contar_acciones_dia("") == 0 and contar_acciones_dia(None) == 0,
        )

        original_ventana = settings.limite_dia_ventana_min
        try:
            settings.limite_dia_ventana_min = 90
            check(
                "settings.limite_dia_ventana_min=90 manda con minutos invalido -> 7",
                contar_acciones_dia("dia_uno", minutos=None) == 7,
                f"(={contar_acciones_dia('dia_uno', minutos=None)})",
            )
            check(
                "un minutos explicito > 0 gana sobre settings (1500 -> 9)",
                contar_acciones_dia("dia_uno", minutos=1500) == 9,
            )
        finally:
            settings.limite_dia_ventana_min = original_ventana
        check(
            "ventana diaria de settings restaurada",
            settings.limite_dia_ventana_min == original_ventana
            and contar_acciones_dia("dia_uno") == 8,
        )


# --------------------------------------------------------------------------- #
# (4) contar_acciones_dia_por_usuario
# --------------------------------------------------------------------------- #
def test_conteo_dia_por_usuario(check):
    print("(4) contar_acciones_dia_por_usuario: UNA consulta agrupada")
    with _base_temporal() as maker:
        registrar = registro.registrar_accion
        registrar("u_uno", "post", "exito")
        registrar("u_uno", "post", "ok")
        registrar("u_uno", "cita", "exitoso")
        registrar("u_uno", "retweet", "exito")
        registrar("u_uno", "respuesta", "exito")
        registrar("u_uno", "like", "exito")  # tipo ajeno -> se ignora
        registrar("u_uno", "post", "fallido")  # fallida -> se ignora
        registrar("u_dos", "comentario", "exitoso")
        registrar("u_dos", "reply", "ok")
        registrar("u_tres", "post", "exito")  # no se pide -> no aparece
        _insertar_directo(
            maker, "u_uno", "quote", "exito", datetime.utcnow() - timedelta(hours=2)
        )
        _insertar_directo(
            maker,
            "u_uno",
            "rt",
            "exito",
            datetime.utcnow() - timedelta(hours=24, minutes=30),
        )

        res = contar_acciones_dia_por_usuario(["u_uno", "u_dos"])
        check(
            "default (1440 min): u_uno=6 y u_dos=2",
            res == {"u_uno": 6, "u_dos": 2},
            f"({res})",
        )
        check(
            "solo entradas > 0 y solo usuarios pedidos",
            set(res) == {"u_uno", "u_dos"} and all(v > 0 for v in res.values()),
        )
        check("u_tres (no pedido) no aparece", "u_tres" not in res)
        check(
            "las fallidas y los tipos ajenos (like) no suman",
            res.get("u_uno") == 6,
        )

        res_corta = contar_acciones_dia_por_usuario(["u_uno", "u_dos"], minutos=60)
        check(
            "minutos=60 excluye la de 2 h (u_uno=5) y no cambia u_dos",
            res_corta == {"u_uno": 5, "u_dos": 2},
            f"({res_corta})",
        )
        res_larga = contar_acciones_dia_por_usuario(["u_uno", "u_dos"], minutos=1500)
        check(
            "minutos=1500 incluye la de 24.5 h (u_uno=7)",
            res_larga == {"u_uno": 7, "u_dos": 2},
            f"({res_larga})",
        )
        check(
            "coherente con contar_acciones_dia (u_uno=6)",
            contar_acciones_dia_por_usuario(["u_uno"]).get("u_uno") == 6
            == contar_acciones_dia("u_uno"),
        )
        check("lista vacia -> {}", contar_acciones_dia_por_usuario([]) == {})
        check("None -> {}", contar_acciones_dia_por_usuario(None) == {})
        check(
            "usuario sin acciones -> no aparece",
            contar_acciones_dia_por_usuario(["fantasma"]) == {},
        )
        check(
            "un string suelto se acepta como 1 usuario",
            contar_acciones_dia_por_usuario("u_dos") == {"u_dos": 2},
        )
        check(
            "minutos invalido usa la ventana diaria (u_uno=6)",
            contar_acciones_dia_por_usuario(["u_uno"], minutos=None)
            == {"u_uno": 6}
            and contar_acciones_dia_por_usuario(["u_uno"], minutos="abc")
            == {"u_uno": 6},
        )


# --------------------------------------------------------------------------- #
# (5) robustez
# --------------------------------------------------------------------------- #
def test_robustez(check):
    print("(5) get_db_session que lanza -> 0 y {} sin propagar")
    original = registro.get_db_session

    def _explota():
        raise RuntimeError("BD caida a proposito")

    try:
        registro.get_db_session = _explota
        check(
            "contar_acciones_dia no lanza y devuelve 0",
            contar_acciones_dia("x") == 0,
        )
        check(
            "contar_acciones_dia_por_usuario no lanza y devuelve {}",
            contar_acciones_dia_por_usuario(["x"]) == {},
        )
        check(
            "limite_acciones_dia sigue funcionando sin BD",
            limite_acciones_dia() == settings.limite_acciones_dia,
        )
    finally:
        registro.get_db_session = original
    check(
        "get_db_session original restaurado",
        registro.get_db_session is original,
    )


# --------------------------------------------------------------------------- #
# (6) alias real de las variables diarias
# --------------------------------------------------------------------------- #
def test_alias_diario(check):
    """pydantic-settings v2 ignora `env=` en Field: el alias es el efectivo.

    Se comprueba el metadato de los campos diarios y, de verdad, la
    instanciacion aislada de `Settings()` leyendo CUOTAS_DIARIAS_ACTIVO,
    LIMITE_ACCIONES_DIA (alias legado) y LIMITE_DIARIO_POR_CUENTA (nombre
    principal pedido por el dueño; le gana al legado). El .env del proyecto se
    respeta; NUNCA se modifica. NO se recarga `core.config` (el objeto
    `settings` a nivel modulo lo comparten decenas de modulos: recargarlo los
    dejaria desincronizados); se instancian `Settings()` frescos con el entorno
    parcheado y se restauran SIEMPRE en el `finally`."""
    print("(6) alias real CUOTAS_DIARIAS_ACTIVO / LIMITE_ACCIONES_DIA / LIMITE_DIARIO_POR_CUENTA")
    from core.config import Settings

    campo = Settings.model_fields["limite_diario_activo"]
    check(
        "model_fields expone validation_alias=CUOTAS_DIARIAS_ACTIVO",
        str(campo.validation_alias) == "CUOTAS_DIARIAS_ACTIVO",
        f"({campo.validation_alias!r})",
    )
    check(
        "Settings expone los 3 campos del tope diario",
        all(
            nombre in Settings.model_fields
            for nombre in (
                "limite_acciones_dia",
                "limite_dia_ventana_min",
                "limite_diario_activo",
            )
        ),
    )
    campo_dia = Settings.model_fields["limite_acciones_dia"]
    check(
        "limite_acciones_dia usa AliasChoices",
        isinstance(campo_dia.validation_alias, AliasChoices),
        f"({campo_dia.validation_alias!r})",
    )
    opciones = list(getattr(campo_dia.validation_alias, "choices", []) or [])
    check(
        "AliasChoices = (LIMITE_DIARIO_POR_CUENTA, LIMITE_ACCIONES_DIA, limite_acciones_dia)",
        opciones
        == [
            "LIMITE_DIARIO_POR_CUENTA",
            "LIMITE_ACCIONES_DIA",
            "limite_acciones_dia",
        ],
        f"({opciones})",
    )

    previo_activo = os.environ.get("CUOTAS_DIARIAS_ACTIVO")
    previo_limite = os.environ.get("LIMITE_ACCIONES_DIA")
    previo_principal = os.environ.get("LIMITE_DIARIO_POR_CUENTA")
    settings_original = registro.settings
    try:
        # El nombre principal no debe interferir con los checks previos.
        os.environ.pop("LIMITE_DIARIO_POR_CUENTA", None)
        os.environ["CUOTAS_DIARIAS_ACTIVO"] = "0"
        nuevo_off = Settings()
        check(
            "Settings() con CUOTAS_DIARIAS_ACTIVO=0 -> limite_diario_activo False",
            nuevo_off.limite_diario_activo is False,
            f"(={nuevo_off.limite_diario_activo})",
        )
        registro.settings = nuevo_off
        check(
            "con ese settings parcheado, limite_acciones_dia() = 0",
            limite_acciones_dia() == 0,
        )

        os.environ["CUOTAS_DIARIAS_ACTIVO"] = "true"
        os.environ["LIMITE_ACCIONES_DIA"] = "7"
        nuevo_on = Settings()
        check(
            "Settings() con CUOTAS_DIARIAS_ACTIVO=true -> True",
            nuevo_on.limite_diario_activo is True,
        )
        check(
            "Settings() con LIMITE_ACCIONES_DIA=7 (alias legado) -> 7",
            nuevo_on.limite_acciones_dia == 7,
            f"(={nuevo_on.limite_acciones_dia})",
        )
        registro.settings = nuevo_on
        check(
            "con ese settings parcheado, limite_acciones_dia() = 7",
            limite_acciones_dia() == 7,
        )

        # Nombre PRINCIPAL pedido por el dueño: LIMITE_DIARIO_POR_CUENTA.
        os.environ["LIMITE_DIARIO_POR_CUENTA"] = "9"
        nuevo_principal = Settings()
        check(
            "Settings() con LIMITE_DIARIO_POR_CUENTA=9 y LIMITE_ACCIONES_DIA=7 -> gana el principal (9)",
            nuevo_principal.limite_acciones_dia == 9,
            f"(={nuevo_principal.limite_acciones_dia})",
        )
        os.environ.pop("LIMITE_ACCIONES_DIA", None)
        solo_principal = Settings()
        check(
            "solo LIMITE_DIARIO_POR_CUENTA=9 -> 9",
            solo_principal.limite_acciones_dia == 9,
            f"(={solo_principal.limite_acciones_dia})",
        )
        registro.settings = solo_principal
        check(
            "con ese settings parcheado, limite_acciones_dia() = 9",
            limite_acciones_dia() == 9,
        )
        os.environ.pop("LIMITE_DIARIO_POR_CUENTA", None)
        check(
            "sin ninguna variable diaria -> default 12",
            "LIMITE_DIARIO_POR_CUENTA" not in os.environ
            and "LIMITE_ACCIONES_DIA" not in os.environ
            and Settings().limite_acciones_dia == 12,
        )

        os.environ["LIMITE_DIARIO_POR_CUENTA"] = "0"
        os.environ["LIMITE_ACCIONES_DIA"] = "0"
        nuevo_cero = Settings()
        registro.settings = nuevo_cero
        check(
            "LIMITE_DIARIO_POR_CUENTA=0 / LIMITE_ACCIONES_DIA=0 -> 0 (ilimitado) y no lanza",
            nuevo_cero.limite_acciones_dia == 0 and limite_acciones_dia() == 0,
        )
    finally:
        registro.settings = settings_original
        if previo_activo is None:
            os.environ.pop("CUOTAS_DIARIAS_ACTIVO", None)
        else:
            os.environ["CUOTAS_DIARIAS_ACTIVO"] = previo_activo
        if previo_limite is None:
            os.environ.pop("LIMITE_ACCIONES_DIA", None)
        else:
            os.environ["LIMITE_ACCIONES_DIA"] = previo_limite
        if previo_principal is None:
            os.environ.pop("LIMITE_DIARIO_POR_CUENTA", None)
        else:
            os.environ["LIMITE_DIARIO_POR_CUENTA"] = previo_principal

    check(
        "settings de core.registro restaurado",
        registro.settings is settings_original,
    )
    check(
        "variables de entorno restauradas",
        os.environ.get("CUOTAS_DIARIAS_ACTIVO") == previo_activo
        and os.environ.get("LIMITE_ACCIONES_DIA") == previo_limite
        and os.environ.get("LIMITE_DIARIO_POR_CUENTA") == previo_principal,
    )


# --------------------------------------------------------------------------- #
# (7)(8) tiers: normalizacion, etiquetas y regla de roles
# --------------------------------------------------------------------------- #
class _CuentaFake:
    """Cuenta minima para probar core.tiers sin BD ni SQLAlchemy."""

    def __init__(self, usuario="cuenta_test", tier_calidad="", handle_actual=""):
        self.usuario = usuario
        self.tier_calidad = tier_calidad
        self.handle_actual = handle_actual


class _CuentaExplosiva:
    """Objeto cuyo acceso a atributos siempre falla (tolerancia de core.tiers)."""

    def __getattr__(self, nombre):
        raise RuntimeError(f"boom al leer {nombre}")


def test_tiers_normalizacion(check):
    print("(7) normalizar_tier / etiqueta_tier / tier_de_cuenta / es_tier2")
    check("TIERS tiene exactamente tier1 y tier2", set(TIERS) == {"tier1", "tier2"})
    check(
        "etiqueta tier1 exacta",
        TIERS["tier1"] == "Tier 1 (Líder/Boosted)",
        f"({TIERS['tier1']})",
    )
    check(
        "etiqueta tier2 exacta",
        TIERS["tier2"] == "Tier 2 (Volumen/Aged)",
        f"({TIERS['tier2']})",
    )
    check("ROL_PROHIBIDO_TIER2 = 'hashtags'", ROL_PROHIBIDO_TIER2 == "hashtags")

    variantes_tier1 = (
        "tier1",
        "Tier1",
        "TIER 1",
        "tier 1",
        "Tier 1",
        "1",
        "lider",
        "líder",
        "Líder",
        "LÍDER",
        "boosted",
        "Boosted",
        "principal",
        "Principal",
        " tier 1 ",
        "Tier 1 (Líder/Boosted)",
    )
    for valor in variantes_tier1:
        check(
            f"normalizar_tier({valor!r}) -> 'tier1'",
            normalizar_tier(valor) == "tier1",
            f"(={normalizar_tier(valor)!r})",
        )

    variantes_tier2 = (
        "tier2",
        "Tier2",
        "TIER 2",
        "tier 2",
        "Tier 2",
        "2",
        "volumen",
        "Volumen",
        "aged",
        "Aged",
        "masivo",
        "Masivo",
        "masiva",
        "masivos",
        "Tier 2 (Volumen/Aged)",
    )
    for valor in variantes_tier2:
        check(
            f"normalizar_tier({valor!r}) -> 'tier2'",
            normalizar_tier(valor) == "tier2",
            f"(={normalizar_tier(valor)!r})",
        )

    invalidos = ("", "   ", None, "tier3", "3", "gold", "premium", 0, "sin tier", "tier")
    for valor in invalidos:
        check(
            f"normalizar_tier({valor!r}) -> ''",
            normalizar_tier(valor) == "",
            f"(={normalizar_tier(valor)!r})",
        )

    check("etiqueta_tier('tier1')", etiqueta_tier("tier1") == "Tier 1 (Líder/Boosted)")
    check(
        "etiqueta_tier('Líder') -> etiqueta tier1",
        etiqueta_tier("Líder") == "Tier 1 (Líder/Boosted)",
    )
    check("etiqueta_tier('Tier 2')", etiqueta_tier("Tier 2") == "Tier 2 (Volumen/Aged)")
    check(
        "etiqueta_tier('aged') -> etiqueta tier2",
        etiqueta_tier("aged") == "Tier 2 (Volumen/Aged)",
    )
    check("etiqueta_tier('') -> ''", etiqueta_tier("") == "")
    check("etiqueta_tier(None) -> ''", etiqueta_tier(None) == "")
    check("etiqueta_tier('xxx') -> ''", etiqueta_tier("xxx") == "")

    check(
        "tier_de_cuenta('Tier 2') -> 'tier2'",
        tier_de_cuenta(_CuentaFake(tier_calidad="Tier 2")) == "tier2",
    )
    check(
        "tier_de_cuenta('líder') -> 'tier1'",
        tier_de_cuenta(_CuentaFake(tier_calidad="líder")) == "tier1",
    )
    check("tier_de_cuenta('') -> ''", tier_de_cuenta(_CuentaFake()) == "")
    check("tier_de_cuenta(None) -> ''", tier_de_cuenta(None) == "")
    check("tier_de_cuenta(objeto sin atributo) -> ''", tier_de_cuenta(object()) == "")
    check(
        "tier_de_cuenta(objeto explosivo) -> '' sin lanzar",
        tier_de_cuenta(_CuentaExplosiva()) == "",
    )
    check("es_tier2(Tier 2) True", es_tier2(_CuentaFake(tier_calidad="volumen")) is True)
    check("es_tier2(Tier 1) False", es_tier2(_CuentaFake(tier_calidad="boosted")) is False)
    check("es_tier2(tier vacio) False", es_tier2(_CuentaFake()) is False)
    check("es_tier2(None) False", es_tier2(None) is False)
    check("es_tier2(explosiva) False sin lanzar", es_tier2(_CuentaExplosiva()) is False)


def test_tiers_roles(check):
    print("(8) rol_permitido_tier / error_rol_tier: Tier 2 no publica hashtags")
    tier1 = _CuentaFake(usuario="lider_uno", tier_calidad="tier1")
    tier2 = _CuentaFake(usuario="volumen_dos", tier_calidad="tier2")
    sin_tier = _CuentaFake(usuario="nueva_tres")
    desconocido = _CuentaFake(usuario="rara_cuatro", tier_calidad="gold")

    # "post" (y sus primos) normalizan a "hashtags": tambien prohibidos.
    for rol in (
        "hashtags",
        "Hashtags",
        "post",
        "posts",
        "publicacion",
        "publicaciones",
        "mantenimiento",
        "calentamiento",
        "hilo",
        "hilos",
    ):
        check(
            f"Tier 2 PROHIBIDO el rol {rol!r}",
            rol_permitido_tier(tier2, rol) is False,
        )
    for rol in (
        "rt",
        "retweet",
        "repost",
        "RT",
        "cita",
        "quote",
        "Retweet con cita",
        "comentario",
        "respuesta",
        "reply",
        "",
        None,
        "desconocido",
    ):
        check(
            f"Tier 2 permitido el rol {rol!r}",
            rol_permitido_tier(tier2, rol) is True,
        )
    for rol in ("hashtags", "post", "mantenimiento"):
        check(f"Tier 1 permitido el rol {rol!r}", rol_permitido_tier(tier1, rol) is True)
    for rol in ("hashtags", "post"):
        check(
            f"tier vacio permitido el rol {rol!r}",
            rol_permitido_tier(sin_tier, rol) is True,
        )
        check(
            f"tier desconocido permitido el rol {rol!r}",
            rol_permitido_tier(desconocido, rol) is True,
        )
    check(
        "cuenta None -> permitido sin lanzar",
        rol_permitido_tier(None, "hashtags") is True,
    )
    check(
        "cuenta explosiva -> permitido sin lanzar",
        rol_permitido_tier(_CuentaExplosiva(), "hashtags") is True,
    )

    esperado = (
        "La cuenta @volumen_dos (Tier 2 - Volumen/Aged) tiene PROHIBIDO el rol "
        "'hashtags'. Usa Tier 1 para posts originales o asígnale RT/Cita/Comentario."
    )
    check(
        "error_rol_tier(Tier 2, hashtags) = mensaje bloqueante exacto",
        error_rol_tier(tier2, "hashtags") == esperado,
        f"({error_rol_tier(tier2, 'hashtags')!r})",
    )
    check(
        "error_rol_tier(Tier 2, post) tambien bloquea",
        error_rol_tier(tier2, "post") != "",
    )
    check("error_rol_tier(Tier 2, rt) = ''", error_rol_tier(tier2, "rt") == "")
    check(
        "error_rol_tier(Tier 2, cita) = ''",
        error_rol_tier(tier2, "Retweet con cita") == "",
    )
    check(
        "error_rol_tier(Tier 2, comentario) = ''",
        error_rol_tier(tier2, "comentario") == "",
    )
    check(
        "error_rol_tier(Tier 1, hashtags) = ''",
        error_rol_tier(tier1, "hashtags") == "",
    )
    check(
        "error_rol_tier(tier vacio, post) = ''",
        error_rol_tier(sin_tier, "post") == "",
    )
    check(
        "error_rol_tier(None, hashtags) = ''",
        error_rol_tier(None, "hashtags") == "",
    )


# --------------------------------------------------------------------------- #
# (9) "Agotada por hoy": esta_agotada_dia / usuarios_agotados_dia
# --------------------------------------------------------------------------- #
def test_agotada_dia(check):
    print("(9) esta_agotada_dia / usuarios_agotados_dia (conteos y tope simulados)")
    original_contar = registro.contar_acciones_dia
    original_grupo = registro.contar_acciones_dia_por_usuario
    original_limite = settings.limite_acciones_dia
    original_activo = settings.limite_diario_activo
    llamadas = {"grupo": 0}
    try:
        settings.limite_diario_activo = True
        settings.limite_acciones_dia = 12

        registro.contar_acciones_dia = lambda usuario, minutos=None: 12
        check("12 acciones de 12 -> agotada", esta_agotada_dia("cuenta_x") is True)
        registro.contar_acciones_dia = lambda usuario, minutos=None: 11
        check("11 acciones de 12 -> NO agotada", esta_agotada_dia("cuenta_x") is False)
        registro.contar_acciones_dia = lambda usuario, minutos=None: 13
        check("13 acciones de 12 -> agotada", esta_agotada_dia("cuenta_x") is True)
        check(
            "limite explicito 12 -> agotada",
            esta_agotada_dia("cuenta_x", limite=12) is True,
        )
        check(
            "limite explicito 20 -> NO agotada",
            esta_agotada_dia("cuenta_x", limite=20) is False,
        )
        check(
            "limite explicito invalido ('abc') cae a settings -> agotada",
            esta_agotada_dia("cuenta_x", limite="abc") is True,
        )

        settings.limite_acciones_dia = 0
        check(
            "tope 0 (ilimitado) -> nunca agotada",
            esta_agotada_dia("cuenta_x") is False,
        )
        settings.limite_acciones_dia = 12
        settings.limite_diario_activo = False
        check(
            "interruptor diario apagado -> nunca agotada",
            esta_agotada_dia("cuenta_x") is False,
        )
        check(
            "limite explicito > 0 manda sobre el interruptor -> agotada",
            esta_agotada_dia("cuenta_x", limite=12) is True,
        )
        settings.limite_diario_activo = True

        def _conteos(usuarios, minutos=None):
            llamadas["grupo"] += 1
            if not usuarios:
                return {}
            return {"a": 12, "b": 11, "c": 13}

        registro.contar_acciones_dia_por_usuario = _conteos
        llamadas["grupo"] = 0
        resultado = usuarios_agotados_dia(["a", "b", "c"])
        check(
            "usuarios_agotados_dia(['a','b','c']) = {'a','c'}",
            resultado == {"a", "c"},
            f"({resultado})",
        )
        check(
            "UNA sola consulta agrupada",
            llamadas["grupo"] == 1,
            f"({llamadas['grupo']} llamada(s))",
        )
        check(
            "limite explicito 20 -> nadie agotado",
            usuarios_agotados_dia(["a", "b", "c"], limite=20) == set(),
        )
        check(
            "limite explicito 11 -> todos agotados",
            usuarios_agotados_dia(["a", "b", "c"], limite=11) == {"a", "b", "c"},
        )
        check("lista vacia -> set()", usuarios_agotados_dia([]) == set())
        check("None -> set()", usuarios_agotados_dia(None) == set())

        settings.limite_acciones_dia = 0
        llamadas["grupo"] = 0
        check(
            "tope 0 -> set() sin consultar la BD",
            usuarios_agotados_dia(["a"]) == set() and llamadas["grupo"] == 0,
        )
        settings.limite_acciones_dia = 12
        settings.limite_diario_activo = False
        check(
            "interruptor apagado -> set() sin consultar",
            usuarios_agotados_dia(["a"]) == set() and llamadas["grupo"] == 0,
        )
        check(
            "limite explicito > 0 gana al interruptor (12 -> {'a','c'})",
            usuarios_agotados_dia(["a", "b", "c"], limite=12) == {"a", "c"},
        )
        settings.limite_diario_activo = True

        def _explota(usuarios=None, minutos=None):
            raise RuntimeError("BD caida a proposito")

        registro.contar_acciones_dia_por_usuario = _explota
        check(
            "conteo agrupado que lanza -> set() sin propagar",
            usuarios_agotados_dia(["a"]) == set(),
        )
        registro.contar_acciones_dia = lambda usuario, minutos=None: (_ for _ in ()).throw(
            RuntimeError("BD caida a proposito")
        )
        check(
            "conteo individual que lanza -> False sin propagar",
            esta_agotada_dia("a") is False,
        )
    finally:
        registro.contar_acciones_dia = original_contar
        registro.contar_acciones_dia_por_usuario = original_grupo
        settings.limite_acciones_dia = original_limite
        settings.limite_diario_activo = original_activo
    check(
        "conteos y settings restaurados",
        registro.contar_acciones_dia is original_contar
        and registro.contar_acciones_dia_por_usuario is original_grupo
        and settings.limite_acciones_dia == original_limite
        and settings.limite_diario_activo == original_activo,
    )


# --------------------------------------------------------------------------- #
# (10) migracion automatica de tier_calidad (SQLite temporal)
# --------------------------------------------------------------------------- #
def test_migracion_tier(check):
    print("(10) migracion automatica de 'tier_calidad' (SQLite temporal)")
    import core.database as database

    check(
        "NUEVAS_COLUMNAS_CUENTAS incluye tier_calidad",
        "tier_calidad" in database.NUEVAS_COLUMNAS_CUENTAS,
    )
    check(
        "modelo Cuenta tiene la columna tier_calidad",
        "tier_calidad" in Cuenta.__table__.columns,
    )
    check(
        "exportacion incluye ('tier_calidad', 'Tier_Calidad')",
        ("tier_calidad", "Tier_Calidad") in COLUMNAS_CUENTAS,
    )

    engine_original = database.engine
    es_sqlite_original = database._ES_SQLITE
    carpeta = tempfile.mkdtemp(prefix="tier_migracion_")
    ruta = Path(carpeta) / "migracion_tier.db"
    engine = create_engine(
        f"sqlite:///{ruta}",
        connect_args={"check_same_thread": False},
    )
    try:
        # Tabla 'cuentas' vieja (sin tier_calidad), como en produccion.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE cuentas ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "usuario VARCHAR(100), grupo VARCHAR(10), "
                    "seccion VARCHAR(20), sector VARCHAR(30))"
                )
            )
        database.engine = engine
        database._ES_SQLITE = True
        database._migrar_columnas()
        with engine.connect() as conn:
            info = {
                fila[1]: fila
                for fila in conn.execute(text("PRAGMA table_info(cuentas)"))
            }
        check("la migracion agrego 'tier_calidad'", "tier_calidad" in info)
        check(
            "columnas previas intactas",
            {"id", "usuario", "grupo", "seccion", "sector"} <= set(info),
        )
        fila_tier = info.get("tier_calidad")
        check(
            "tier_calidad es VARCHAR",
            fila_tier is not None and str(fila_tier[2]).upper().startswith("VARCHAR"),
            f"({fila_tier[2] if fila_tier else None})",
        )
        check(
            "tier_calidad default '' (sin clasificar)",
            fila_tier is not None and str(fila_tier[4]).strip("'\"") == "",
            f"({fila_tier[4] if fila_tier else None})",
        )

        total_antes = len(info)
        database._migrar_columnas()
        with engine.connect() as conn:
            info2 = {
                fila[1]: fila
                for fila in conn.execute(text("PRAGMA table_info(cuentas)"))
            }
        check(
            "segunda pasada idempotente (mismas columnas, tier_calidad una sola vez)",
            len(info2) == total_antes and "tier_calidad" in info2,
            f"({total_antes} -> {len(info2)})",
        )
    finally:
        database.engine = engine_original
        database._ES_SQLITE = es_sqlite_original
        engine.dispose()
        shutil.rmtree(carpeta, ignore_errors=True)
    check(
        "engine y _ES_SQLITE restaurados",
        database.engine is engine_original and database._ES_SQLITE == es_sqlite_original,
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_limite_diario(check)
    test_acciones_operativas(check)
    test_conteo_dia(check)
    test_conteo_dia_por_usuario(check)
    test_robustez(check)
    test_alias_diario(check)
    test_tiers_normalizacion(check)
    test_tiers_roles(check)
    test_agotada_dia(check)
    test_migracion_tier(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_cuotas_diarias_core.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
