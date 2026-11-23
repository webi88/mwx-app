# -*- coding: utf-8 -*-
"""Tests de `AppImagen` + `core/app_imagenes.py` (imagenes del MWX APP en BD).

Todo corre en una SQLite TEMPORAL (`tempfile.mkdtemp`); NUNCA toca
`data/gestor_redes.db` ni escribe archivos de imagenes en disco.

Cubre:
  (1) `Base.metadata.create_all` crea `app_imagen` con FK a `app_usuario` y la
      restriccion unique(usuario_id, nombre).
  (2) `nombre_seguro` / `extension_de` / `content_type_de` y sus rechazos
      (rutas, traversal, no-str, >120 caracteres).
  (3) Limite `MWX_IMAGEN_MAX_MB` (monkeypatch): exacto acepta, +1 byte no.
  (4) Guardar/leer/upsert/listar/eliminar, sin commit implicito.
  (5) Upsert reemplaza (mismo id) y el mismo nombre convive entre usuarios.
  (6) Tolerancia: db/usuario/contenido/nombre invalidos devuelven
      None/False/[] sin lanzar.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mwx_imagenes.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que el resto de tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine, func, inspect, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import core.models  # noqa: F401,E402  (registra TODAS las tablas en Base)
from core import app_imagenes  # noqa: E402
from core.database import Base  # noqa: E402
from core.models import AppImagen, AppUsuario  # noqa: E402

_ENV_LIMITE = "MWX_IMAGEN_MAX_MB"


@contextmanager
def _limite_configurado(valor_mb=None):
    """Fija/quita `MWX_IMAGEN_MAX_MB` y restaura el entorno al salir."""
    anterior = os.environ.get(_ENV_LIMITE)
    try:
        if valor_mb is None:
            os.environ.pop(_ENV_LIMITE, None)
        else:
            os.environ[_ENV_LIMITE] = str(valor_mb)
        yield
    finally:
        if anterior is None:
            os.environ.pop(_ENV_LIMITE, None)
        else:
            os.environ[_ENV_LIMITE] = anterior


def _contar(db, usuario_id, nombre=None) -> int:
    consulta = select(func.count()).select_from(AppImagen).where(
        AppImagen.usuario_id == usuario_id
    )
    if nombre is not None:
        consulta = consulta.where(AppImagen.nombre == nombre)
    return int(db.execute(consulta).scalar() or 0)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _probar_nombres(check) -> None:
    check(
        "nombre_seguro: acepta nombres ASCII normales",
        app_imagenes.nombre_seguro("foto_1.PNG") == "foto_1.PNG"
        and app_imagenes.nombre_seguro("a-b_c.d.jpeg") == "a-b_c.d.jpeg",
    )
    check(
        "nombre_seguro: acepta exactamente 120 caracteres",
        app_imagenes.nombre_seguro("a" * 116 + ".png") == "a" * 116 + ".png",
    )
    check(
        "nombre_seguro: rechaza >120 caracteres",
        app_imagenes.nombre_seguro("a" * 117 + ".png") == "",
    )
    for etiqueta, valor in (
        ("vacio", ""),
        ("None", None),
        ("numero", 123),
        ("punto", "."),
        ("doble punto", ".."),
        ("traversal ../", "../foto.png"),
        ("traversal interno", "foto..png"),
        ("barra /", "sub/foto.png"),
        ("barra inversa \\", "sub\\foto.png"),
        ("espacio", "foto png.png"),
        ("acento", "ñandu.png"),
        ("salto de linea", "foto\n.png"),
        ("espacio al final", "foto.png "),
    ):
        check(
            f"nombre_seguro: rechaza {etiqueta}",
            app_imagenes.nombre_seguro(valor) == "",
        )
    check(
        "NOMBRE_SEGURO_RE: patron publico disponible",
        app_imagenes.NOMBRE_SEGURO_RE.fullmatch("ok-1_x.png") is not None
        and app_imagenes.NOMBRE_SEGURO_RE.fullmatch("../x") is None,
    )

    check(
        "extension_de: minusculas y sin punto",
        app_imagenes.extension_de("FOTO.JPG") == "jpg"
        and app_imagenes.extension_de("a.b.c.WebP") == "webp",
    )
    check(
        "extension_de: sin extension o nombre invalido -> ''",
        app_imagenes.extension_de("foto") == ""
        and app_imagenes.extension_de("../x.png") == ""
        and app_imagenes.extension_de(None) == "",
    )

    esperados = {
        "p.png": "image/png",
        "p.jpg": "image/jpeg",
        "p.JPEG": "image/jpeg",
        "p.webp": "image/webp",
        "p.GIF": "image/gif",
    }
    check(
        "content_type_de: png/jpg/jpeg/webp/gif",
        all(
            app_imagenes.content_type_de(nombre) == tipo
            for nombre, tipo in esperados.items()
        ),
    )
    check(
        "content_type_de: desconocida/invalida -> octet-stream",
        app_imagenes.content_type_de("programa.exe")
        == app_imagenes.CONTENT_TYPE_POR_DEFECTO
        and app_imagenes.content_type_de("../foto.png")
        == app_imagenes.CONTENT_TYPE_POR_DEFECTO
        and app_imagenes.CONTENT_TYPE_POR_DEFECTO
        == "application/octet-stream",
    )


def _probar_tabla(check, engine) -> None:
    check(
        "metadata: app_imagen registrada en Base.metadata",
        "app_imagen" in Base.metadata.tables,
    )
    inspector = inspect(engine)
    tablas = set(inspector.get_table_names())
    check("create_all: la tabla app_imagen existe", "app_imagen" in tablas)

    columnas = {c["name"]: c for c in inspector.get_columns("app_imagen")}
    check(
        "app_imagen: columnas requeridas",
        {
            "id",
            "usuario_id",
            "nombre",
            "contenido",
            "content_type",
            "tamano",
            "creado",
        }
        <= set(columnas),
    )
    check(
        "app_imagen: usuario_id y nombre/contenido NOT NULL",
        columnas["usuario_id"]["nullable"] is False
        and columnas["nombre"]["nullable"] is False
        and columnas["contenido"]["nullable"] is False,
    )
    check(
        "app_imagen: usuario_id indexado",
        any(
            indice.get("column_names") == ["usuario_id"]
            for indice in inspector.get_indexes("app_imagen")
        ),
    )
    claves_foraneas = inspector.get_foreign_keys("app_imagen")
    check(
        "app_imagen: FK usuario_id -> app_usuario.id",
        any(
            fk.get("referred_table") == "app_usuario"
            and fk.get("constrained_columns") == ["usuario_id"]
            and fk.get("referred_columns") == ["id"]
            for fk in claves_foraneas
        ),
    )
    uniques = {
        tuple(u.get("column_names") or [])
        for u in inspector.get_unique_constraints("app_imagen")
    }
    check(
        "app_imagen: unique(usuario_id, nombre)",
        ("usuario_id", "nombre") in uniques,
    )


def _probar_limite(check, db, usuario_id) -> None:
    with _limite_configurado(None):
        check(
            "limite: default de 8 MB sin variable de entorno",
            app_imagenes.limite_max_bytes() == 8 * 1024 * 1024,
        )

    with _limite_configurado(0.001):
        limite = app_imagenes.limite_max_bytes()
        check(
            "limite: MWX_IMAGEN_MAX_MB acepta decimales (0.001 MB)",
            limite == int(0.001 * 1024 * 1024) and limite > 0,
        )
        exacta = app_imagenes.guardar_imagen(
            db, usuario_id, "limite_ok.png", b"x" * limite
        )
        check(
            "limite: contenido EXACTO al maximo se acepta",
            exacta is not None
            and exacta.tamano == limite
            and _contar(db, usuario_id, "limite_ok.png") == 1,
        )
        excedida = app_imagenes.guardar_imagen(
            db, usuario_id, "limite_no.png", b"x" * (limite + 1)
        )
        check(
            "limite: contenido > maximo se rechaza",
            excedida is None and _contar(db, usuario_id, "limite_no.png") == 0,
        )
        db.rollback()

    with _limite_configurado("no-numerico"):
        check(
            "limite: valor invalido cae al default",
            app_imagenes.limite_max_bytes() == 8 * 1024 * 1024,
        )

    # Monkeypatch del default del modulo (sin variable de entorno).
    original = app_imagenes.MWX_IMAGEN_MAX_MB
    try:
        with _limite_configurado(None):
            app_imagenes.MWX_IMAGEN_MAX_MB = 0.002
            check(
                "limite: monkeypatch del default del modulo",
                app_imagenes.limite_max_bytes()
                == int(0.002 * 1024 * 1024),
            )
    finally:
        app_imagenes.MWX_IMAGEN_MAX_MB = original


def _probar_guardar_leer(check, db, ana_id, bea_id, Fabrica) -> None:
    contenido = b"\x89PNG\r\n\x1a\n-contenido-de-prueba"
    imagen = app_imagenes.guardar_imagen(
        db, ana_id, "foto.png", contenido
    )
    check(
        "guardar: devuelve AppImagen con id y metadatos",
        isinstance(imagen, AppImagen)
        and imagen.id is not None
        and imagen.usuario_id == ana_id
        and imagen.nombre == "foto.png"
        and imagen.contenido == contenido
        and imagen.content_type == "image/png"
        and imagen.tamano == len(contenido)
        and imagen.creado is not None,
    )
    check(
        "guardar: relacion con AppUsuario disponible",
        imagen.usuario is not None and imagen.usuario.id == ana_id,
    )

    # NO commit: otra sesion no ve nada hasta que el llamador confirme.
    db2 = Fabrica()
    try:
        visible = db2.execute(
            select(AppImagen).where(AppImagen.nombre == "foto.png")
        ).scalars().first()
        check("guardar: NO hace commit (otra sesion no ve la fila)", visible is None)
        db.commit()
        visible = db2.execute(
            select(AppImagen).where(AppImagen.nombre == "foto.png")
        ).scalars().first()
        check(
            "guardar: tras commit la fila persiste",
            visible is not None and visible.contenido == contenido,
        )
    finally:
        db2.close()

    leida = app_imagenes.leer_imagen(db, ana_id, "foto.png")
    check(
        "leer: devuelve exactamente nombre/contenido/content_type/tamano",
        isinstance(leida, dict)
        and set(leida) == {"nombre", "contenido", "content_type", "tamano"}
        and leida["nombre"] == "foto.png"
        and leida["contenido"] == contenido
        and leida["content_type"] == "image/png"
        and leida["tamano"] == len(contenido),
    )
    check(
        "leer: inexistente -> None",
        app_imagenes.leer_imagen(db, ana_id, "no_existe.png") is None,
    )
    check(
        "leer: nombre traversal -> None",
        app_imagenes.leer_imagen(db, ana_id, "../foto.png") is None,
    )

    # content_type explicito manda; vacio se deduce por extension.
    con_tipo = app_imagenes.guardar_imagen(
        db, ana_id, "raro.bin", b"datos", content_type="image/x-custom"
    )
    check(
        "guardar: content_type explicito se conserva",
        con_tipo is not None and con_tipo.content_type == "image/x-custom",
    )
    check(
        "guardar: content_type None se deduce por extension",
        app_imagenes.guardar_imagen(
            db, ana_id, "tipo.jpg", b"jpg", content_type=None
        ).content_type
        == "image/jpeg",
    )
    check(
        "guardar: content_type >80 se recorta",
        app_imagenes.guardar_imagen(
            db, ana_id, "largo.png", b"x", content_type="t" * 200
        ).content_type
        == "t" * 80,
    )
    check(
        "guardar: bytearray/memoryview se aceptan como bytes",
        app_imagenes.guardar_imagen(
            db, ana_id, "bytearray.gif", bytearray(b"GIF89a")
        ).contenido
        == b"GIF89a"
        and app_imagenes.guardar_imagen(
            db, ana_id, "memory.webp", memoryview(b"RIFF")
        ).contenido
        == b"RIFF",
    )

    # Upsert: mismo usuario+nombre reemplaza y conserva el id.
    id_antes = imagen.id
    creado_antes = imagen.creado
    time.sleep(0.01)
    nuevo = b"\x89PNG-nuevo-contenido"
    reemplazada = app_imagenes.guardar_imagen(
        db, ana_id, "foto.png", nuevo
    )
    check(
        "upsert: reemplaza contenido y tamano conservando el id",
        reemplazada is not None
        and reemplazada.id == id_antes
        and reemplazada.contenido == nuevo
        and reemplazada.tamano == len(nuevo)
        and reemplazada.creado >= creado_antes,
    )
    check(
        "upsert: solo queda UNA fila para (usuario, nombre)",
        _contar(db, ana_id, "foto.png") == 1,
    )
    db.commit()
    check(
        "upsert: leer devuelve el contenido nuevo",
        app_imagenes.leer_imagen(db, ana_id, "foto.png")["contenido"] == nuevo,
    )

    # Otro usuario con el MISMO nombre convive (la unique es por usuario).
    misma_bea = app_imagenes.guardar_imagen(
        db, bea_id, "foto.png", b"contenido-de-bea"
    )
    db.commit()
    check(
        "unique: otro usuario puede usar el mismo nombre",
        misma_bea is not None
        and misma_bea.id != reemplazada.id
        and _contar(db, ana_id, "foto.png") == 1
        and _contar(db, bea_id, "foto.png") == 1,
    )
    check(
        "unique: cada usuario lee su propio contenido",
        app_imagenes.leer_imagen(db, ana_id, "foto.png")["contenido"] == nuevo
        and app_imagenes.leer_imagen(db, bea_id, "foto.png")["contenido"]
        == b"contenido-de-bea",
    )


def _probar_listar_eliminar(check, db, ana_id, bea_id) -> None:
    app_imagenes.guardar_imagen(db, ana_id, "segunda.webp", b"RIFF-webp")
    db.commit()

    listado = app_imagenes.listar_imagenes(db, ana_id)
    nombres = [item["nombre"] for item in listado]
    check(
        "listar: devuelve los metadatos en orden de insercion",
        nombres
        == [
            "foto.png",
            "raro.bin",
            "tipo.jpg",
            "largo.png",
            "bytearray.gif",
            "memory.webp",
            "segunda.webp",
        ],
    )
    check(
        "listar: SIN contenido y con las 4 claves",
        all(
            set(item) == {"nombre", "content_type", "tamano", "creado"}
            and "contenido" not in item
            for item in listado
        ),
    )
    check(
        "listar: metadatos correctos",
        next(i for i in listado if i["nombre"] == "segunda.webp")["content_type"]
        == "image/webp"
        and next(i for i in listado if i["nombre"] == "segunda.webp")["tamano"]
        == len(b"RIFF-webp")
        and all(i["creado"] is not None for i in listado),
    )
    check(
        "listar: usuario invalido -> []",
        app_imagenes.listar_imagenes(db, None) == []
        and app_imagenes.listar_imagenes(db, 0) == []
        and app_imagenes.listar_imagenes(db, "abc") == []
        and app_imagenes.listar_imagenes(None, ana_id) == [],
    )
    check(
        "listar: cada usuario ve solo lo suyo",
        [i["nombre"] for i in app_imagenes.listar_imagenes(db, bea_id)]
        == ["foto.png"],
    )

    check(
        "eliminar: existente -> True",
        app_imagenes.eliminar_imagen(db, ana_id, "segunda.webp") is True,
    )
    check(
        "eliminar: ya no aparece en listar/leer",
        "segunda.webp"
        not in [
            i["nombre"] for i in app_imagenes.listar_imagenes(db, ana_id)
        ]
        and app_imagenes.leer_imagen(db, ana_id, "segunda.webp") is None,
    )
    check(
        "eliminar: segunda vez -> False",
        app_imagenes.eliminar_imagen(db, ana_id, "segunda.webp") is False,
    )
    check(
        "eliminar: nombre invalido/usuario invalido -> False",
        app_imagenes.eliminar_imagen(db, ana_id, "../x.png") is False
        and app_imagenes.eliminar_imagen(db, None, "foto.png") is False
        and app_imagenes.eliminar_imagen(None, ana_id, "foto.png") is False,
    )
    check(
        "eliminar: no afecta la imagen homonima de otro usuario",
        app_imagenes.eliminar_imagen(db, ana_id, "foto.png") is True
        and app_imagenes.leer_imagen(db, bea_id, "foto.png")["contenido"]
        == b"contenido-de-bea",
    )
    db.commit()


def _probar_tolerancia(check, db, ana_id) -> None:
    filas_antes = _contar(db, ana_id)
    check(
        "tolerancia: guardar con db/usuario/nombre/contenido invalidos -> None",
        app_imagenes.guardar_imagen(None, ana_id, "foto.png", b"x") is None
        and app_imagenes.guardar_imagen(db, None, "foto.png", b"x") is None
        and app_imagenes.guardar_imagen(db, -1, "foto.png", b"x") is None
        and app_imagenes.guardar_imagen(db, "abc", "foto.png", b"x") is None
        and app_imagenes.guardar_imagen(db, ana_id, "", b"x") is None
        and app_imagenes.guardar_imagen(db, ana_id, "../x.png", b"x") is None
        and app_imagenes.guardar_imagen(db, ana_id, "foto.png", "texto") is None
        and app_imagenes.guardar_imagen(db, ana_id, "foto.png", None) is None
        and app_imagenes.guardar_imagen(db, ana_id, "foto.png", 123) is None,
    )
    check(
        "tolerancia: ninguna fila nueva por datos invalidos",
        _contar(db, ana_id) == filas_antes,
    )
    check(
        "tolerancia: leer invalidos -> None",
        app_imagenes.leer_imagen(None, ana_id, "x.png") is None
        and app_imagenes.leer_imagen(db, None, "x.png") is None
        and app_imagenes.leer_imagen(db, ana_id, None) is None
        and app_imagenes.leer_imagen(db, ana_id, "") is None,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run(check) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mwx_imagenes_"))
    engine = None
    db = None
    try:
        ruta_db = tmp / "app_imagen_test.db"
        engine = create_engine(
            f"sqlite:///{ruta_db.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        Fabrica = sessionmaker(bind=engine, expire_on_commit=False)
        db = Fabrica()

        ana = AppUsuario(username="ana", password_hash="x", nombre="Ana")
        bea = AppUsuario(username="bea", password_hash="x", nombre="Bea")
        db.add_all([ana, bea])
        db.commit()
        db.refresh(ana)
        db.refresh(bea)

        _probar_tabla(check, engine)
        _probar_nombres(check)
        _probar_limite(check, db, ana.id)
        _probar_guardar_leer(check, db, ana.id, bea.id, Fabrica)
        _probar_listar_eliminar(check, db, ana.id, bea.id)
        _probar_tolerancia(check, db, ana.id)
    finally:
        if db is not None:
            db.close()
        if engine is not None:
            engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_mwx_imagenes.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
