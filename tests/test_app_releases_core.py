# -*- coding: utf-8 -*-
"""Tests rapidos de `core/app_releases.py` (releases del ejecutable MWX APP).

Sin red, sin Chrome y SIN tocar la base real `data/gestor_redes.db`: la parte
de BD usa una **SQLite temporal** (fake de `core.database.get_db_session`
restaurado siempre) y el empaquetado trabaja en un **directorio temporal**
(nunca `dist/` ni `data/` reales).

Cubre:
    (1) `version_actual`: version del paquete `mwx_app`.
    (2) `empaquetar_dist`: ZIP valido (contenido relativo, config.json, LEEME
        generado, exe recursivo), sha256/tamano, release.json, origen
        inexistente y dist sin exe (no publica).
    (3) `publicar_release`: UPSERT por version, recorte de notas, filename por
        defecto, validaciones (vacio, no-zip, <1KB) y retencion de 2.
    (4) `publicar_release_desde_archivo`: version deducida del nombre.
    (5) `ultimo_release` / `leer_release` / `listar_releases` / `borrar_release`
        con fake de sesion; metadatos SIN contenido salvo `leer_release`.
    (6) Tolerancia: BD caida -> dicts/[] conservadores.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_app_releases_core.py   (solo este)
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.app_releases import (  # noqa: E402
    borrar_release,
    empaquetar_dist,
    leer_release,
    listar_releases,
    publicar_release,
    publicar_release_desde_archivo,
    ultimo_release,
    version_actual,
)
from core.database import Base  # noqa: E402
from core.models import AppRelease  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers (siempre en directorios temporales, nunca data/)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _base_temporal():
    """SQLite temporal aislada + parche de `core.database.get_db_session`."""
    import core.database as database

    carpeta = tempfile.mkdtemp(prefix="app_releases_")
    engine = create_engine(
        f"sqlite:///{Path(carpeta) / 'releases_test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    original = database.get_db_session

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

    database.get_db_session = _sesion_temporal
    try:
        yield maker
    finally:
        database.get_db_session = original
        engine.dispose()
        shutil.rmtree(carpeta, ignore_errors=True)


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


def _zip_bytes(relleno=4096, nombre="MWX APP.exe"):
    """ZIP en memoria con un exe fake y datos incompresibles (> 1 KB)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(nombre, b"exe fake")
        zf.writestr("datos.bin", os.urandom(relleno))
    return buffer.getvalue()


def _conteo(maker):
    with maker() as db:
        return db.query(AppRelease).count()


def _fila(maker, version):
    with maker() as db:
        return (
            db.query(AppRelease)
            .filter(AppRelease.version == version)
            .first()
        )


# --------------------------------------------------------------------------- #
# (1) version_actual
# --------------------------------------------------------------------------- #
def test_version_actual(check):
    from mwx_app import __version__

    actual = version_actual()
    check("version_actual: devuelve str", isinstance(actual, str))
    check("version_actual: coincide con mwx_app.__version__", actual == __version__)
    check("version_actual: no esta vacia", bool(actual), actual)


# --------------------------------------------------------------------------- #
# (2) empaquetar_dist
# --------------------------------------------------------------------------- #
def test_empaquetar_dist(check):
    raiz = tempfile.mkdtemp(prefix="mwx_dist_")
    try:
        origen = Path(raiz) / "MWX APP"
        origen.mkdir()
        (origen / "MWX APP.exe").write_bytes(b"exe fake")
        (origen / "config.json").write_text(
            '{"api_url": "https://test.invalid"}', encoding="utf-8"
        )
        (origen / "sub").mkdir()
        (origen / "sub" / "dll.txt").write_text("binario", encoding="utf-8")
        destino = Path(raiz) / "releases"

        resultado = empaquetar_dist(str(origen), str(destino), version="9.9.9")
        check("empaquetar: ok True", resultado.get("ok") is True, str(resultado.get("error")))
        check(
            "empaquetar: filename MWX-APP-<version>.zip",
            resultado.get("filename") == "MWX-APP-9.9.9.zip",
            str(resultado.get("filename")),
        )
        check("empaquetar: version devuelta", resultado.get("version") == "9.9.9")
        zip_path = Path(resultado.get("zip") or "")
        check("empaquetar: el zip existe", zip_path.is_file(), str(zip_path))

        nombres = []
        if zip_path.is_file():
            with zipfile.ZipFile(zip_path) as zf:
                check("empaquetar: zip valido (testzip)", zf.testzip() is None)
                nombres = zf.namelist()
        check("empaquetar: incluye MWX APP.exe", "MWX APP.exe" in nombres, str(nombres))
        check("empaquetar: incluye config.json", "config.json" in nombres)
        check("empaquetar: conserva subcarpetas", "sub/dll.txt" in nombres)
        check("empaquetar: genera LEEME.txt", "LEEME.txt" in nombres)

        datos = zip_path.read_bytes() if zip_path.is_file() else b""
        check(
            "empaquetar: sha256 real",
            resultado.get("sha256") == hashlib.sha256(datos).hexdigest(),
        )
        check("empaquetar: tamano real", resultado.get("tamano") == len(datos))
        check("empaquetar: tamano > 0", (resultado.get("tamano") or 0) > 0)

        meta_path = destino / "release.json"
        check("empaquetar: escribe release.json", meta_path.is_file())
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        check(
            "empaquetar: release.json sin contenido",
            "contenido" not in meta,
        )
        check(
            "empaquetar: release.json con sha256/version",
            meta.get("sha256") == resultado.get("sha256")
            and meta.get("version") == "9.9.9"
            and meta.get("filename") == "MWX-APP-9.9.9.zip",
        )

        # Version vacia: usa version_actual() o una marca de tiempo.
        resultado_auto = empaquetar_dist(str(origen), str(destino), version="")
        check("empaquetar: version vacia usa un default", bool(resultado_auto.get("version")))
        check(
            "empaquetar: version vacia mantiene el nombre MWX-APP-*",
            str(resultado_auto.get("filename") or "").startswith("MWX-APP-"),
        )

        # Origen inexistente: no publica.
        inexistente = empaquetar_dist(
            str(Path(raiz) / "no_existe"), str(destino), version="1.0.0"
        )
        check("empaquetar: origen inexistente -> ok False", inexistente.get("ok") is False)
        check(
            "empaquetar: error claro de origen",
            "origen" in str(inexistente.get("error") or "").lower(),
            str(inexistente.get("error")),
        )
        check("empaquetar: origen inexistente no deja zip", inexistente.get("zip") == "")

        # Dist sin el exe: borra el zip y no publica.
        origen_sin_exe = Path(raiz) / "sin_exe"
        origen_sin_exe.mkdir()
        (origen_sin_exe / "otro.txt").write_text("x", encoding="utf-8")
        sin_exe = empaquetar_dist(
            str(origen_sin_exe), str(destino), version="1.0.1"
        )
        check("empaquetar: dist sin exe -> ok False", sin_exe.get("ok") is False)
        check(
            "empaquetar: error menciona MWX APP.exe",
            "MWX APP.exe" in str(sin_exe.get("error") or ""),
            str(sin_exe.get("error")),
        )
        check(
            "empaquetar: dist sin exe no deja zip",
            not (destino / "MWX-APP-1.0.1.zip").exists(),
        )
    finally:
        shutil.rmtree(raiz, ignore_errors=True)


# --------------------------------------------------------------------------- #
# (3) publicar_release
# --------------------------------------------------------------------------- #
def test_publicar_release(check):
    with _base_temporal() as maker:
        datos = _zip_bytes()
        sha = hashlib.sha256(datos).hexdigest()

        resultado = publicar_release(datos, "1.0.0", obligatorio=True, notas="hola")
        check("publicar: ok True", resultado.get("ok") is True, str(resultado.get("error")))
        check("publicar: sha256", resultado.get("sha256") == sha)
        check("publicar: tamano", resultado.get("tamano") == len(datos))
        check("publicar: version", resultado.get("version") == "1.0.0")
        check("publicar: una sola fila", _conteo(maker) == 1)

        fila = _fila(maker, "1.0.0")
        check("publicar: guarda contenido bytes", fila is not None and fila.contenido == datos)
        check(
            "publicar: filename por defecto",
            fila is not None and fila.filename == "MWX-APP-1.0.0.zip",
        )
        check("publicar: obligatorio respetado", fila is not None and fila.obligatorio is True)
        check("publicar: notas guardadas", fila is not None and fila.notas == "hola")
        id_original = fila.id if fila is not None else None

        # UPSERT: misma version -> reemplaza (misma fila, nuevos datos).
        datos2 = _zip_bytes(relleno=8192)
        resultado2 = publicar_release(
            datos2,
            "1.0.0",
            obligatorio=False,
            notas="nueva",
            filename="custom.zip",
        )
        check("publicar: UPSERT ok", resultado2.get("ok") is True)
        check("publicar: UPSERT sin duplicar", _conteo(maker) == 1)
        fila2 = _fila(maker, "1.0.0")
        check("publicar: UPSERT conserva el id", fila2 is not None and fila2.id == id_original)
        check(
            "publicar: UPSERT actualiza sha256",
            resultado2.get("sha256") == hashlib.sha256(datos2).hexdigest(),
        )
        check("publicar: UPSERT actualiza contenido", fila2 is not None and fila2.contenido == datos2)
        check("publicar: UPSERT actualiza obligatorio", fila2 is not None and fila2.obligatorio is False)
        check("publicar: UPSERT actualiza notas", fila2 is not None and fila2.notas == "nueva")
        check("publicar: UPSERT actualiza filename", fila2 is not None and fila2.filename == "custom.zip")

        # Recorte de notas a 4000.
        publicar_release(datos, "1.0.1", notas="x" * 5000)
        fila3 = _fila(maker, "1.0.1")
        check(
            "publicar: recorta notas a 4000",
            fila3 is not None and len(fila3.notas) == 4000,
            str(len(fila3.notas) if fila3 is not None else -1),
        )

        # Validaciones.
        check(
            "publicar: version vacia -> ok False",
            publicar_release(datos, "").get("ok") is False,
        )
        check(
            "publicar: contenido no bytes -> ok False",
            publicar_release("texto", "1.0.2").get("ok") is False,
        )
        check(
            "publicar: contenido no-zip -> ok False",
            publicar_release(b"no soy un zip real", "1.0.3").get("ok") is False,
        )
        check(
            "publicar: zip menor a 1 KB -> ok False",
            publicar_release(_zip_bytes(relleno=1), "1.0.4").get("ok") is False,
        )

        # Retencion: solo los 2 mas recientes por id.
        publicar_release(datos, "2.0.0")
        publicar_release(datos, "2.0.1")
        versiones = set()
        with maker() as db:
            versiones = {f.version for f in db.query(AppRelease).all()}
        check("publicar: retencion de 2 filas", _conteo(maker) == 2, str(versiones))
        check(
            "publicar: conserva las 2 mas recientes",
            versiones == {"2.0.0", "2.0.1"},
            str(versiones),
        )


# --------------------------------------------------------------------------- #
# (4) publicar_release_desde_archivo
# --------------------------------------------------------------------------- #
def test_publicar_desde_archivo(check):
    carpeta = tempfile.mkdtemp(prefix="mwx_release_file_")
    try:
        with _base_temporal() as maker:
            ruta = Path(carpeta) / "MWX-APP-7.7.7.zip"
            ruta.write_bytes(_zip_bytes())
            resultado = publicar_release_desde_archivo(str(ruta), version="")
            check(
                "desde_archivo: ok True",
                resultado.get("ok") is True,
                str(resultado.get("error")),
            )
            check(
                "desde_archivo: deduce la version del nombre",
                resultado.get("version") == "7.7.7",
                str(resultado.get("version")),
            )
            fila = _fila(maker, "7.7.7")
            check(
                "desde_archivo: guarda el filename del archivo",
                fila is not None and fila.filename == "MWX-APP-7.7.7.zip",
            )

            faltante = publicar_release_desde_archivo(
                str(Path(carpeta) / "no_existe.zip")
            )
            check("desde_archivo: ruta inexistente -> ok False", faltante.get("ok") is False)
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


# --------------------------------------------------------------------------- #
# (5) ultimo_release / leer_release / listar_releases / borrar_release
# --------------------------------------------------------------------------- #
def test_consultas(check):
    with _base_temporal() as maker:
        check(
            "ultimo_release: sin releases -> disponible False",
            ultimo_release().get("disponible") is False,
        )
        check("listar_releases: sin releases -> []", listar_releases() == [])
        vacio = leer_release()
        check(
            "leer_release: sin releases -> contenido b''",
            vacio.get("disponible") is False and vacio.get("contenido") == b"",
        )

        datos_v1 = _zip_bytes(relleno=2048)
        datos_v2 = _zip_bytes(relleno=3072)
        publicar_release(datos_v1, "1.0.0", notas="primera")
        publicar_release(datos_v2, "1.1.0", obligatorio=False, notas="segunda")

        ultimo = ultimo_release()
        check("ultimo_release: disponible True", ultimo.get("disponible") is True)
        check("ultimo_release: version mas reciente", ultimo.get("version") == "1.1.0")
        check("ultimo_release: obligatorio", ultimo.get("obligatorio") is False)
        check("ultimo_release: notas", ultimo.get("notas") == "segunda")
        check("ultimo_release: sin contenido", "contenido" not in ultimo)
        check(
            "ultimo_release: fecha ISO str",
            isinstance(ultimo.get("fecha"), str) and bool(ultimo.get("fecha")),
            str(ultimo.get("fecha")),
        )
        check(
            "ultimo_release: sha256/tamano",
            ultimo.get("sha256") == hashlib.sha256(datos_v2).hexdigest()
            and ultimo.get("tamano") == len(datos_v2),
        )

        contenido_ultimo = leer_release()
        check(
            "leer_release: devuelve el contenido del mas reciente",
            contenido_ultimo.get("disponible") is True
            and contenido_ultimo.get("contenido") == datos_v2,
        )
        contenido_v1 = leer_release("1.0.0")
        check(
            "leer_release: devuelve la version pedida",
            contenido_v1.get("disponible") is True
            and contenido_v1.get("contenido") == datos_v1
            and contenido_v1.get("version") == "1.0.0",
        )
        inexistente = leer_release("999.0.0")
        check(
            "leer_release: version inexistente -> disponible False",
            inexistente.get("disponible") is False
            and inexistente.get("contenido") == b"",
        )

        lista = listar_releases()
        check("listar_releases: devuelve list", isinstance(lista, list) and len(lista) == 2)
        check(
            "listar_releases: mas nuevo primero",
            [r.get("version") for r in lista] == ["1.1.0", "1.0.0"],
        )
        check(
            "listar_releases: sin contenido",
            all("contenido" not in r for r in lista),
        )
        check("listar_releases: limite", len(listar_releases(limite=1)) == 1)

        borrado = borrar_release("1.0.0")
        check("borrar_release: ok True", borrado.get("ok") is True, str(borrado.get("error")))
        check("borrar_release: queda 1 fila", _conteo(maker) == 1)
        check(
            "borrar_release: segunda vez -> ok False",
            borrar_release("1.0.0").get("ok") is False,
        )
        check(
            "borrar_release: version vacia -> ok False",
            borrar_release("").get("ok") is False,
        )


# --------------------------------------------------------------------------- #
# (6) Tolerancia: BD caida
# --------------------------------------------------------------------------- #
def test_tolerancia(check):
    with _sesion_que_explota():
        check(
            "tolerancia: ultimo_release con BD caida -> disponible False",
            ultimo_release() == {"disponible": False},
        )
        check(
            "tolerancia: leer_release con BD caida -> contenido b''",
            leer_release().get("disponible") is False,
        )
        check("tolerancia: listar_releases con BD caida -> []", listar_releases() == [])
        check(
            "tolerancia: publicar_release con BD caida -> ok False",
            publicar_release(_zip_bytes(), "3.0.0").get("ok") is False,
        )
        check(
            "tolerancia: borrar_release con BD caida -> ok False",
            borrar_release("3.0.0").get("ok") is False,
        )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_version_actual(check)
    test_empaquetar_dist(check)
    test_publicar_release(check)
    test_publicar_desde_archivo(check)
    test_consultas(check)
    test_tolerancia(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
