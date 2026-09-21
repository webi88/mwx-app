# -*- coding: utf-8 -*-
"""Tests rapidos del Sistema de Cuotas Inteligente por Hora (capa core).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: el bloque
de conteo usa una SQLite temporal en `tempfile.mkdtemp()` y reemplaza
`core.registro.get_db_session` por una sesion de esa base (se restaura en el
`finally` aunque algo falle).

Cubre:
    (1) defaults de `core.config.settings` (5/5/7/3, ventana 60) y mapeo de
        `limite_por_rol` para los 4 roles.
    (2) `CUOTAS_HORARIAS_ACTIVO=False` -> `limite_por_rol` devuelve 0 y la
        configuracion se restaura.
    (3) `normalizar_rol_cuota`: variantes con/sin acentos, mayusculas y
        sinonimos; desconocido -> texto limpio; vacio/None -> "".
    (4) `tipo_registro_rol`: mapa canonico, rol desconocido y vacio.
    (5) roundtrip real: `registrar_accion` (exito/fallido, post/retweet/
        respuesta/quote) + una fila vieja insertada directo, contada con
        `contar_acciones_recientes` dentro y fuera de la ventana.
    (6) `contar_acciones_por_usuario` agrupado por usuario y rol con 2 usuarios
        y tipos mezclados (los tipos sin rol se ignoran).
    (7) robustez: `get_db_session` que lanza -> 0 y {} sin propagar.
    (8) alias real `CUOTAS_HORARIAS_ACTIVO` en `Settings` (pydantic-settings v2
        ignora `env=` en Field): instanciacion con env 0/true/1 y
        `limite_por_rol` con ese settings parcheado.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_cuotas_core.py   (solo este archivo)
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

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.registro as registro  # noqa: E402
from core.config import settings  # noqa: E402
from core.models import Base, RegistroAccion  # noqa: E402
from core.registro import (  # noqa: E402
    contar_acciones_por_usuario,
    contar_acciones_recientes,
    limite_por_rol,
    normalizar_rol_cuota,
    registrar_accion,
    tipo_registro_rol,
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
    carpeta = tempfile.mkdtemp(prefix="cuotas_core_")
    ruta = Path(carpeta) / "cuotas_test.db"
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
# (1) defaults de settings y limite_por_rol
# --------------------------------------------------------------------------- #
def test_settings_defaults(check):
    print("(1) defaults de settings y limite_por_rol por rol")
    check(
        "defaults: limite_posts_hora=5",
        settings.limite_posts_hora == 5,
        f"(={settings.limite_posts_hora})",
    )
    check(
        "defaults: limite_citas_hora=5",
        settings.limite_citas_hora == 5,
        f"(={settings.limite_citas_hora})",
    )
    check(
        "defaults: limite_rts_hora=7",
        settings.limite_rts_hora == 7,
        f"(={settings.limite_rts_hora})",
    )
    check(
        "defaults: limite_comentarios_hora=3",
        settings.limite_comentarios_hora == 3,
        f"(={settings.limite_comentarios_hora})",
    )
    check(
        "defaults: limite_ventana_min=60",
        settings.limite_ventana_min == 60,
        f"(={settings.limite_ventana_min})",
    )
    check("defaults: limite_cuotas_activo=True", settings.limite_cuotas_activo is True)

    check(
        "limite_por_rol('hashtags') = posts",
        limite_por_rol("hashtags") == settings.limite_posts_hora,
    )
    check(
        "limite_por_rol('cita') = citas",
        limite_por_rol("cita") == settings.limite_citas_hora,
    )
    check(
        "limite_por_rol('rt') = rts",
        limite_por_rol("rt") == settings.limite_rts_hora,
    )
    check(
        "limite_por_rol('comentario') = comentarios",
        limite_por_rol("comentario") == settings.limite_comentarios_hora,
    )
    check("limite_por_rol desconocido -> 0", limite_por_rol("baile") == 0)
    check(
        "limite_por_rol vacio/None -> 0",
        limite_por_rol("") == 0 and limite_por_rol(None) == 0,
    )


# --------------------------------------------------------------------------- #
# (2) cuotas desactivadas
# --------------------------------------------------------------------------- #
def test_cuotas_desactivadas(check):
    print("(2) CUOTAS_HORARIAS_ACTIVO=False -> limite 0 y restauracion")
    original = settings.limite_cuotas_activo
    try:
        settings.limite_cuotas_activo = False
        ok = all(
            limite_por_rol(rol) == 0
            for rol in ("hashtags", "post", "cita", "quote", "rt", "comentario", "respuesta")
        )
        check("con cuotas apagadas los 4 roles (y alias) devuelven 0", ok)
    finally:
        settings.limite_cuotas_activo = original
    check(
        "configuracion restaurada (limite_cuotas_activo)",
        settings.limite_cuotas_activo == original,
    )
    check(
        "tras restaurar, hashtags vuelve a su limite",
        limite_por_rol("hashtags") == settings.limite_posts_hora,
    )


# --------------------------------------------------------------------------- #
# (3) normalizar_rol_cuota
# --------------------------------------------------------------------------- #
def test_normalizar(check):
    print("(3) normalizar_rol_cuota: variantes y sinonimos")
    casos = [
        ("post", "hashtags"),
        ("POST", "hashtags"),
        ("Posts", "hashtags"),
        ("publicacion", "hashtags"),
        ("publicación", "hashtags"),
        ("hashtag", "hashtags"),
        ("hashtags", "hashtags"),
        ("quote", "cita"),
        ("citar", "cita"),
        ("CITA", "cita"),
        ("cita con comentario", "cita"),
        ("Retweet con cita", "cita"),
        ("retweet", "rt"),
        ("Retweet", "rt"),
        ("repost", "rt"),
        ("RT", "rt"),
        ("respuesta", "comentario"),
        ("RESPUESTAS", "comentario"),
        ("reply", "comentario"),
        ("comentar", "comentario"),
    ]
    for entrada, esperado in casos:
        check(
            f"normalizar({entrada!r}) -> {esperado}",
            normalizar_rol_cuota(entrada) == esperado,
        )
    check(
        "desconocido -> texto limpio en minusculas",
        normalizar_rol_cuota("  Baile  ") == "baile",
    )
    check("None -> ''", normalizar_rol_cuota(None) == "")
    check("solo espacios -> ''", normalizar_rol_cuota("   ") == "")


# --------------------------------------------------------------------------- #
# (4) tipo_registro_rol
# --------------------------------------------------------------------------- #
def test_tipo_registro(check):
    print("(4) tipo_registro_rol: tipo canonico por rol")
    check(
        "hashtags/post -> post",
        tipo_registro_rol("hashtags") == "post" and tipo_registro_rol("post") == "post",
    )
    check(
        "cita/quote -> cita",
        tipo_registro_rol("cita") == "cita" and tipo_registro_rol("quote") == "cita",
    )
    check(
        "rt/retweet -> rt",
        tipo_registro_rol("rt") == "rt" and tipo_registro_rol("retweet") == "rt",
    )
    check(
        "comentario/respuesta -> comentario",
        tipo_registro_rol("comentario") == "comentario"
        and tipo_registro_rol("respuesta") == "comentario",
    )
    check(
        "vacio/None -> activacion",
        tipo_registro_rol("") == "activacion" and tipo_registro_rol(None) == "activacion",
    )
    check(
        "desconocido -> rol limpio tal cual",
        tipo_registro_rol("Baile") == "baile",
    )


# --------------------------------------------------------------------------- #
# (5) roundtrip con registrar_accion
# --------------------------------------------------------------------------- #
def test_conteo_reciente(check):
    print("(5) registrar_accion + contar_acciones_recientes (ventana y exitos)")
    with _base_temporal() as maker:
        registrar_accion("cuota_uno", "post", "exito")
        registrar_accion("cuota_uno", "post", "exitoso")
        registrar_accion("cuota_uno", "post", "ok")
        registrar_accion("cuota_uno", "post", "fallido")  # NO debe contar
        registrar_accion("cuota_uno", "retweet", "exito")
        registrar_accion("cuota_uno", "respuesta", "ok")
        registrar_accion("cuota_uno", "quote", "exito")
        registrar_accion("cuota_uno", "megafono", "exito")  # tipo sin rol
        registrar_accion("otro_user", "post", "exito")
        _insertar_directo(
            maker,
            "cuota_uno",
            "post",
            "exito",
            datetime.utcnow() - timedelta(hours=2),
        )

        check("BD temporal: 10 filas insertadas", _filas(maker) == 10)

        check(
            "'post' exitoso (3) suma al rol hashtags y el fallido no cuenta",
            contar_acciones_recientes("cuota_uno", "hashtags") == 3,
        )
        check(
            "la fila de hace 2h NO entra en la ventana de 60 min",
            contar_acciones_recientes("cuota_uno", "hashtags", minutos=60) == 3,
        )
        check(
            "con minutos=180 SI entra la fila vieja (4)",
            contar_acciones_recientes("cuota_uno", "hashtags", minutos=180) == 4,
        )
        check(
            "'retweet' suma al rol rt",
            contar_acciones_recientes("cuota_uno", "rt") == 1,
        )
        check(
            "'respuesta' suma al rol comentario",
            contar_acciones_recientes("cuota_uno", "comentario") == 1,
        )
        check(
            "'quote' suma al rol cita",
            contar_acciones_recientes("cuota_uno", "cita") == 1,
        )
        check(
            "alias 'post' cuenta igual que 'hashtags'",
            contar_acciones_recientes("cuota_uno", "post") == 3,
        )
        check(
            "rol desconocido filtra tipo == rol (megafono=1)",
            contar_acciones_recientes("cuota_uno", "megafono") == 1,
        )
        check(
            "otro usuario no se mezcla",
            contar_acciones_recientes("otro_user", "hashtags") == 1
            and contar_acciones_recientes("cuota_uno", "rt") == 1,
        )
        check(
            "usuario sin acciones -> 0",
            contar_acciones_recientes("nadie", "hashtags") == 0,
        )
        check(
            "usuario vacio -> 0",
            contar_acciones_recientes("", "hashtags") == 0,
        )
        check(
            "minutos invalido (None/0/'abc') usa la ventana default de 60",
            contar_acciones_recientes("cuota_uno", "hashtags", minutos=None) == 3
            and contar_acciones_recientes("cuota_uno", "hashtags", minutos=0) == 3
            and contar_acciones_recientes("cuota_uno", "hashtags", minutos="abc") == 3,
        )

        original_ventana = settings.limite_ventana_min
        try:
            settings.limite_ventana_min = 180
            check(
                "minutos invalido respeta settings.limite_ventana_min",
                contar_acciones_recientes("cuota_uno", "hashtags", minutos=None) == 4,
            )
        finally:
            settings.limite_ventana_min = original_ventana
        check(
            "ventana de settings restaurada",
            settings.limite_ventana_min == original_ventana,
        )


# --------------------------------------------------------------------------- #
# (6) contar_acciones_por_usuario
# --------------------------------------------------------------------------- #
def test_conteo_por_usuario(check):
    print("(6) contar_acciones_por_usuario agrupado por usuario y rol")
    with _base_temporal() as maker:
        registrar_accion("u_uno", "post", "exito")
        registrar_accion("u_uno", "post", "fallido")
        registrar_accion("u_uno", "retweet", "ok")
        registrar_accion("u_uno", "respuesta", "exito")
        registrar_accion("u_uno", "like", "exito")  # tipo sin rol -> se ignora
        registrar_accion("u_dos", "quote", "exitoso")
        registrar_accion("u_dos", "retweet", "exito")
        registrar_accion("u_tres", "post", "exito")  # no se pide -> no aparece
        _insertar_directo(
            maker,
            "u_uno",
            "post",
            "exito",
            datetime.utcnow() - timedelta(hours=3),
        )

        res = contar_acciones_por_usuario(["u_uno", "u_dos"])
        check(
            "u_uno: hashtags=1, rt=1, comentario=1",
            res.get("u_uno") == {"hashtags": 1, "rt": 1, "comentario": 1},
            f"({res.get('u_uno')})",
        )
        check(
            "u_dos: cita=1, rt=1",
            res.get("u_dos") == {"cita": 1, "rt": 1},
            f"({res.get('u_dos')})",
        )
        check("solo usuarios pedidos con conteo > 0", set(res) == {"u_uno", "u_dos"})
        check("u_tres (no pedido) no aparece", "u_tres" not in res)
        check(
            "los tipos sin rol (like) se ignoran",
            all("like" not in roles for roles in res.values()),
        )
        check(
            "la fila de hace 3h no entra en 60 min",
            res.get("u_uno", {}).get("hashtags") == 1,
        )

        res_largo = contar_acciones_por_usuario(["u_uno", "u_dos"], minutos=240)
        check(
            "con 240 min la fila vieja suma (hashtags=2)",
            res_largo.get("u_uno", {}).get("hashtags") == 2,
        )
        check(
            "la ventana larga no cambia a u_dos",
            res_largo.get("u_dos") == {"cita": 1, "rt": 1},
        )
        check("lista vacia -> {}", contar_acciones_por_usuario([]) == {})
        check("None -> {}", contar_acciones_por_usuario(None) == {})
        check(
            "usuario sin acciones -> no aparece",
            contar_acciones_por_usuario(["fantasma"]) == {},
        )
        check(
            "un string suelto se acepta como 1 usuario",
            contar_acciones_por_usuario("u_dos") == {"u_dos": {"cita": 1, "rt": 1}},
        )


# --------------------------------------------------------------------------- #
# (7) robustez
# --------------------------------------------------------------------------- #
def test_robustez(check):
    print("(7) get_db_session que lanza -> 0 y {} sin propagar")
    original = registro.get_db_session

    def _explota():
        raise RuntimeError("BD caida a proposito")

    try:
        registro.get_db_session = _explota
        check(
            "contar_acciones_recientes no lanza y devuelve 0",
            contar_acciones_recientes("x", "hashtags") == 0,
        )
        check(
            "contar_acciones_por_usuario no lanza y devuelve {}",
            contar_acciones_por_usuario(["x"]) == {},
        )
        check(
            "limite_por_rol sigue funcionando sin BD",
            limite_por_rol("hashtags") == settings.limite_posts_hora,
        )
    finally:
        registro.get_db_session = original
    check(
        "get_db_session original restaurado",
        registro.get_db_session is original,
    )


# --------------------------------------------------------------------------- #
# (8) alias real de la variable CUOTAS_HORARIAS_ACTIVO
# --------------------------------------------------------------------------- #
def test_alias_cuotas_activo(check):
    """pydantic-settings v2 ignora `env=` en Field: el alias es el efectivo.

    Se comprueba el metadato del campo y, de verdad, la instanciacion de
    `Settings()` leyendo la variable de entorno 0/true/1 (el .env del proyecto
    se respeta; NUNCA se desactiva)."""
    print("(8) alias real CUOTAS_HORARIAS_ACTIVO (pydantic-settings v2)")
    from core.config import Settings

    campo = Settings.model_fields["limite_cuotas_activo"]
    check(
        "model_fields expone validation_alias=CUOTAS_HORARIAS_ACTIVO",
        str(campo.validation_alias) == "CUOTAS_HORARIAS_ACTIVO",
        f"({campo.validation_alias!r})",
    )

    previo = os.environ.get("CUOTAS_HORARIAS_ACTIVO")
    settings_original = registro.settings
    try:
        os.environ["CUOTAS_HORARIAS_ACTIVO"] = "0"
        nuevo_off = Settings()
        check(
            "Settings() con CUOTAS_HORARIAS_ACTIVO=0 -> limite_cuotas_activo False",
            nuevo_off.limite_cuotas_activo is False,
            f"(={nuevo_off.limite_cuotas_activo})",
        )

        registro.settings = nuevo_off
        check(
            "con ese settings parcheado, limite_por_rol('rt') = 0",
            limite_por_rol("rt") == 0,
        )
        check(
            "con ese settings parcheado, limite_por_rol('hashtags') = 0",
            limite_por_rol("hashtags") == 0,
        )

        os.environ["CUOTAS_HORARIAS_ACTIVO"] = "true"
        nuevo_on = Settings()
        check(
            "Settings() con CUOTAS_HORARIAS_ACTIVO=true -> limite_cuotas_activo True",
            nuevo_on.limite_cuotas_activo is True,
            f"(={nuevo_on.limite_cuotas_activo})",
        )
        registro.settings = nuevo_on
        check(
            "con el settings encendido, limite_por_rol('rt') = limite_rts_hora",
            limite_por_rol("rt") == nuevo_on.limite_rts_hora,
            f"(={nuevo_on.limite_rts_hora})",
        )

        os.environ["CUOTAS_HORARIAS_ACTIVO"] = "1"
        nuevo_uno = Settings()
        check(
            "Settings() con CUOTAS_HORARIAS_ACTIVO=1 -> limite_cuotas_activo True",
            nuevo_uno.limite_cuotas_activo is True,
        )
    finally:
        registro.settings = settings_original
        if previo is None:
            os.environ.pop("CUOTAS_HORARIAS_ACTIVO", None)
        else:
            os.environ["CUOTAS_HORARIAS_ACTIVO"] = previo

    check(
        "settings de core.registro restaurado",
        registro.settings is settings_original,
    )
    check(
        "variable de entorno restaurada",
        os.environ.get("CUOTAS_HORARIAS_ACTIVO") == previo,
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_settings_defaults(check)
    test_cuotas_desactivadas(check)
    test_normalizar(check)
    test_tipo_registro(check)
    test_conteo_reciente(check)
    test_conteo_por_usuario(check)
    test_robustez(check)
    test_alias_cuotas_activo(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_cuotas_core.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
