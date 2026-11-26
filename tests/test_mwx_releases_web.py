# -*- coding: utf-8 -*-
"""Tests de la pestana "⬇️ Ejecutable / Actualizaciones" de MWX APP.

Cubren SIN descargar ZIPs reales y SIN tocar `data/`:

  (1) Sin releases en BD y sin `dist/MWX APP`: la pestana renderiza, muestra
      las instrucciones de `build_mwx.bat` y NO ejecuta empaquetado.
  (2) Con un release fake en BD (`ultimo_release`/`leer_release`/`listar_releases`
      monkeypatcheados): muestra version/fecha/tamano/obligatoria, el boton de
      descarga `dl_mwx_release` con el contenido fake y la tabla de releases.
  (3) Con `dist/MWX APP` local (carpeta temporal): aparece el boton de
      empaquetado sin pulsarlo.
  (4) El boton "Publicar actualizacion" valida el formulario sin subir nada
      (sin ZIP avisa y NO llama `publicar_release`).
  (5) Multi-plataforma: selector Windows/mac-arm64/mac-intel con default
      Windows, caches con la plataforma, version por defecto con sufijo Mac,
      rechazo de un ZIP de otra plataforma y tabla con columna Plataforma.

Los fakes se instalan DENTRO del script de AppTest (solo ASCII) y las caches
`@st.cache_data` de releases se limpian antes/despues de cada bloque.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from web.operaciones import mwx_app  # noqa: E402

_INEXISTENTE = object()
_CONTENIDO_ZIP = b"PK\x03\x04contenido-de-prueba"


# ---------------------------------------------------------------------------
# Aislamiento de core.app_releases: fakes SIN BD ni red, restaurados siempre
# ---------------------------------------------------------------------------


@contextmanager
def _fakes_core(
    version="9.9.9",
    ultimo_fn=None,
    listar_fn=None,
    contenido=_CONTENIDO_ZIP,
    publicadas=None,
):
    """Reemplaza core.app_releases por fakes y restaura al salir."""
    import core.app_releases as releases

    nombres = (
        "ultimo_release",
        "listar_releases",
        "leer_release",
        "version_actual",
        "empaquetar_dist",
        "publicar_release",
    )
    originales = {nombre: getattr(releases, nombre, _INEXISTENTE) for nombre in nombres}

    def _publicar(
        datos,
        version_release,
        obligatorio=True,
        notas="",
        filename="",
    ):
        if publicadas is not None:
            publicadas.append(
                {
                    "version": version_release,
                    "obligatorio": obligatorio,
                    "notas": notas,
                    "filename": filename,
                    "tamano": len(datos),
                }
            )
        return {
            "ok": True,
            "version": version_release,
            "sha256": "b" * 64,
            "tamano": len(datos),
        }

    releases.ultimo_release = ultimo_fn or (
        lambda plataforma="": {"disponible": False}
    )
    releases.listar_releases = listar_fn or (
        lambda limite=5, plataforma="": []
    )
    releases.leer_release = lambda version="": {
        "disponible": bool(contenido),
        "version": version,
        "contenido": contenido,
    }
    releases.version_actual = lambda: version
    releases.empaquetar_dist = lambda *args, **kwargs: {
        "ok": False,
        "error": "no deberia llamarse",
    }
    releases.publicar_release = _publicar
    try:
        yield releases
    finally:
        for nombre, original in originales.items():
            if original is _INEXISTENTE:
                try:
                    delattr(releases, nombre)
                except AttributeError:
                    pass
            else:
                setattr(releases, nombre, original)
        mwx_app._limpiar_cache_releases()


@contextmanager
def _uploader_fake(nombre, contenido=_CONTENIDO_ZIP):
    """Sustituye `st.file_uploader` por un ZIP fake (AppTest no lo simula)."""
    import streamlit as st

    original = st.file_uploader

    class _Subido:
        name = nombre

        def getvalue(self):
            return contenido

    st.file_uploader = lambda *args, **kwargs: _Subido()
    try:
        yield
    finally:
        st.file_uploader = original


# ---------------------------------------------------------------------------
# Scripts de AppTest (SOLO ASCII: se escriben con la codificacion local)
# ---------------------------------------------------------------------------


def _app_ejecutable_sin_releases():
    import os
    import tempfile

    import core.app_releases as releases
    from web.operaciones import mwx_app as pagina

    releases.ultimo_release = lambda: {"disponible": False}
    releases.listar_releases = lambda limite=5: []
    releases.empaquetar_dist = lambda *args, **kwargs: {
        "ok": False,
        "error": "no deberia llamarse",
    }
    carpeta = tempfile.mkdtemp(prefix="mwx_no_dist_")
    pagina.resolver_ruta = lambda relativa: os.path.join(carpeta, "no_existe")
    pagina._limpiar_cache_releases()
    pagina._tab_ejecutable()


def _app_ejecutable_con_release():
    import streamlit as st

    import core.app_releases as releases
    from web.operaciones import mwx_app as pagina

    meta = {
        "disponible": True,
        "version": "9.9.9",
        "obligatorio": True,
        "notas": "prueba de liberacion",
        "sha256": "a" * 64,
        "tamano": 2048,
        "filename": "MWX-APP-9.9.9.zip",
        "fecha": "2026-11-20T10:00:00",
    }

    def _leer(version=""):
        st.session_state["release_leido"] = str(version)
        return dict(meta, contenido=b"PK\x03\x04contenido-de-prueba")

    releases.ultimo_release = lambda: dict(meta)
    releases.leer_release = _leer
    releases.listar_releases = lambda limite=5: [dict(meta)]
    pagina._limpiar_cache_releases()
    pagina._tab_ejecutable()


def _app_ejecutable_dist_local():
    import os
    import tempfile
    from pathlib import Path

    import core.app_releases as releases
    from web.operaciones import mwx_app as pagina

    releases.ultimo_release = lambda: {"disponible": False}
    releases.listar_releases = lambda limite=5: []
    releases.empaquetar_dist = lambda *args, **kwargs: {
        "ok": False,
        "error": "no deberia llamarse",
    }
    raiz = Path(tempfile.mkdtemp(prefix="mwx_dist_"))
    (raiz / "dist" / "MWX APP").mkdir(parents=True)
    (raiz / "dist" / "MWX APP" / "MWX APP.exe").write_bytes(b"exe-fake")
    pagina.resolver_ruta = lambda relativa: os.path.join(
        str(raiz), str(relativa).replace("/", os.sep)
    )
    pagina._limpiar_cache_releases()
    pagina._tab_ejecutable()


def _app_ejecutable_publicar_vacio():
    import os
    import tempfile

    import core.app_releases as releases
    from web.operaciones import mwx_app as pagina

    releases.ultimo_release = lambda: {"disponible": False}
    releases.listar_releases = lambda limite=5: []
    releases.publicar_release = lambda *args, **kwargs: {"ok": True}
    carpeta = tempfile.mkdtemp(prefix="mwx_no_dist_")
    pagina.resolver_ruta = lambda relativa: os.path.join(carpeta, "no_existe")
    pagina._limpiar_cache_releases()
    pagina._tab_ejecutable()


def _app_tab_ejecutable():
    """Render de la pestana con los fakes instalados FUERA del script."""
    import os
    import tempfile

    from web.operaciones import mwx_app as pagina

    carpeta = tempfile.mkdtemp(prefix="mwx_release_web_")
    pagina.resolver_ruta = lambda relativa: os.path.join(carpeta, "no_existe")
    pagina._limpiar_cache_releases()
    pagina._tab_ejecutable()


# ---------------------------------------------------------------------------
# Runners de AppTest
# ---------------------------------------------------------------------------


def _correr(script):
    from streamlit.testing.v1 import AppTest

    mwx_app._limpiar_cache_releases()
    at = None
    error = ""
    try:
        at = AppTest.from_function(script, default_timeout=60)
        at.run()
    except Exception as e:  # pragma: no cover - fallo de infraestructura
        error = f"{type(e).__name__}: {e}"
    finally:
        mwx_app._limpiar_cache_releases()
    if error:
        return None, error
    if at.exception:
        return None, str(at.exception[0].value)[:300]
    return at, ""


def _probar_sin_releases(check) -> None:
    at, error = _correr(_app_ejecutable_sin_releases)
    check(
        "mwx releases: sin releases la pestana renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    check(
        "mwx releases: sin releases ni dist muestra las instrucciones de "
        "build_mwx.bat",
        any(
            "build_mwx.bat" in str(i.value)
            for i in at.info
        ),
    )
    check(
        "mwx releases: sin releases NO se ejecuta el empaquetado",
        not any("ZIP generado" in str(s.value) for s in at.success)
        and not any("no deberia llamarse" in str(e.value) for e in at.error),
    )
    claves = {b.key for b in at.button}
    check(
        "mwx releases: el boton de publicar existe y no hay descarga",
        "mwx_release_publicar" in claves
        and len(at.get("download_button")) == 0,
        str(sorted(claves)),
    )


def _probar_con_release(check) -> None:
    at, error = _correr(_app_ejecutable_con_release)
    check(
        "mwx releases: con release la pestana renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    metricas = {str(m.label): str(m.value) for m in at.metric}
    check(
        "mwx releases: muestra version, tamano y obligatoriedad del release",
        metricas.get("Versión") == "9.9.9"
        and metricas.get("Tamaño") == "0.0 MB"
        and metricas.get("Obligatoria") == "Sí",
        str(metricas),
    )
    leido = (
        at.session_state["release_leido"]
        if "release_leido" in at.session_state
        else ""
    )
    check(
        "mwx releases: se lee el contenido del release para el boton de descarga",
        len(at.get("download_button")) == 1 and str(leido) == "9.9.9",
        f"(downloads={len(at.get('download_button'))}, leido={leido!r})",
    )
    check(
        "mwx releases: la pestana declara el boton de descarga con key estable",
        'key="dl_mwx_release"' in (
            RAIZ / "web" / "operaciones" / "mwx_app.py"
        ).read_text(encoding="utf-8"),
    )
    check(
        "mwx releases: la tabla lista la version publicada",
        any("9.9.9" in str(tabla.value) for tabla in at.dataframe),
    )


def _probar_dist_local(check) -> None:
    at, error = _correr(_app_ejecutable_dist_local)
    check(
        "mwx releases: con dist/MWX APP local renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    check(
        "mwx releases: con dist/MWX APP aparece el boton de empaquetar",
        any(b.key == "mwx_btn_empaquetar" for b in at.button)
        and any("Empaquetar" in str(b.label) for b in at.button),
    )
    check(
        "mwx releases: empaquetar NO se ejecuta al renderizar",
        not any("ZIP generado" in str(s.value) for s in at.success),
    )


def _probar_publicar_vacio(check) -> None:
    at, error = _correr(_app_ejecutable_publicar_vacio)
    check(
        "mwx releases: el formulario de publicacion renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    try:
        at2 = at.button(key="mwx_release_publicar").click().run()
        error2 = str(at2.exception[0].value)[:200] if at2.exception else ""
    except Exception as e:  # pragma: no cover
        at2 = at
        error2 = f"{type(e).__name__}: {e}"
    check(
        "mwx releases: publicar sin ZIP avisa y NO llama publicar_release",
        not error2
        and any("Adjunta un ZIP" in str(w.value) for w in at2.warning)
        and not any("Actualización publicada" in str(s.value) for s in at2.success),
        error2 or str([str(w.value) for w in at2.warning]),
    )


# ---------------------------------------------------------------------------
# Multi-plataforma (Windows / macOS arm64 / macOS Intel)
# ---------------------------------------------------------------------------


def _meta(
    version,
    obligatorio=True,
    filename="",
    tamano=2048,
    fecha="2026-11-20T10:00:00",
):
    return {
        "disponible": True,
        "version": version,
        "obligatorio": obligatorio,
        "notas": "",
        "sha256": "a" * 64,
        "tamano": tamano,
        "filename": filename or f"MWX-APP-{version}.zip",
        "fecha": fecha,
    }


def _ultimo_por_plataforma(datos):
    def _ultimo(plataforma=""):
        valor = datos.get(plataforma)
        return dict(valor) if valor else {"disponible": False}

    return _ultimo


def _error_de(at) -> str:
    try:
        return str(at.exception[0].value)[:300] if at.exception else ""
    except Exception as e:  # pragma: no cover
        return f"{type(e).__name__}: {e}"


def _probar_cache_plataforma(check) -> None:
    llamadas_ultimo = []
    llamadas_listar = []

    def _ultimo(plataforma=""):
        llamadas_ultimo.append(plataforma)
        return {"disponible": False}

    def _listar(limite=5, plataforma=""):
        llamadas_listar.append((limite, plataforma))
        return []

    with _fakes_core(ultimo_fn=_ultimo, listar_fn=_listar):
        mwx_app._limpiar_cache_releases()
        mwx_app._cache_ultimo_release("windows")
        mwx_app._cache_ultimo_release("mac-arm64")
        mwx_app._cache_listar_releases(10, "mac-intel")

    check(
        "mwx releases: _cache_ultimo_release propaga la plataforma al core",
        llamadas_ultimo == ["windows", "mac-arm64"],
        str(llamadas_ultimo),
    )
    check(
        "mwx releases: _cache_listar_releases propaga limite y plataforma",
        llamadas_listar == [(10, "mac-intel")],
        str(llamadas_listar),
    )


def _probar_cache_fallback_viejo(check) -> None:
    llamadas = []

    def _viejo_sin_plataforma():
        llamadas.append("sin_plataforma")
        return {"disponible": False}

    with _fakes_core(ultimo_fn=_viejo_sin_plataforma):
        mwx_app._limpiar_cache_releases()
        datos = mwx_app._cache_ultimo_release("windows")

    check(
        "mwx releases: core viejo sin plataforma cae a ultimo_release()",
        datos == {"disponible": False} and llamadas == ["sin_plataforma"],
        f"(datos={datos!r}, llamadas={llamadas!r})",
    )


def _probar_selector_y_version(check) -> None:
    check(
        "mwx releases: defaults de version por plataforma (1.1.4)",
        mwx_app._version_default_plataforma("windows", "1.1.4") == "1.1.4"
        and mwx_app._version_default_plataforma("mac-arm64", "1.1.4")
        == "1.1.4-mac-arm64"
        and mwx_app._version_default_plataforma("mac-intel", "1.1.4")
        == "1.1.4-mac-intel"
        and mwx_app._version_default_plataforma(
            "mac-arm64", "1.1.4-mac-arm64"
        )
        == "1.1.4-mac-arm64",
    )
    with _fakes_core(version="9.9.9"):
        at, error = _correr(_app_tab_ejecutable)
    check(
        "mwx releases: selector de plataforma renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    radio = at.radio(key="mwx_release_plataforma")
    check(
        "mwx releases: el selector existe con default windows",
        radio.value == "windows"
        and list(radio.options) == list(mwx_app.PLATAFORMAS_RELEASE.values()),
        f"(value={radio.value!r}, options={list(radio.options)})",
    )
    check(
        "mwx releases: version por defecto de Windows",
        at.text_input(key="mwx_release_version").value == "9.9.9",
        repr(at.text_input(key="mwx_release_version").value),
    )

    with _fakes_core(version="9.9.9"):
        at = at.radio(key="mwx_release_plataforma").set_value("mac-arm64").run()
        error_mac = _error_de(at)
        valor_mac = at.text_input(key="mwx_release_version").value
        at = at.radio(key="mwx_release_plataforma").set_value("mac-intel").run()
        error_intel = _error_de(at)
        valor_intel = at.text_input(key="mwx_release_version").value
        at = at.radio(key="mwx_release_plataforma").set_value("windows").run()
        error_win = _error_de(at)
        valor_win = at.text_input(key="mwx_release_version").value

    check(
        "mwx releases: la version cambia a -mac-arm64 al elegir Apple Silicon",
        not error_mac and valor_mac == "9.9.9-mac-arm64",
        error_mac or repr(valor_mac),
    )
    check(
        "mwx releases: la version cambia a -mac-intel al elegir Intel",
        not error_intel and valor_intel == "9.9.9-mac-intel",
        error_intel or repr(valor_intel),
    )
    check(
        "mwx releases: al volver a Windows la version vuelve a la base",
        not error_win and valor_win == "9.9.9",
        error_win or repr(valor_win),
    )


def _probar_publicar_nombre_windows_en_mac(check) -> None:
    publicadas = []
    with _fakes_core(version="9.9.9", publicadas=publicadas):
        with _uploader_fake("MWX-APP-9.9.9.zip"):
            at, error = _correr(_app_tab_ejecutable)
            if at is not None and not error:
                at = at.radio(key="mwx_release_plataforma").set_value(
                    "mac-arm64"
                ).run()
                error = _error_de(at)
            if at is not None and not error:
                at = at.button(key="mwx_release_publicar").click().run()
                error = _error_de(at)
    check(
        "mwx releases: ZIP con nombre de Windows en plataforma Mac avisa",
        not error
        and at is not None
        and any("no parece de macOS" in str(w.value) for w in at.warning),
        error or (str([str(w.value) for w in at.warning]) if at is not None else "sin app"),
    )
    check(
        "mwx releases: el ZIP de Windows NO se publica como Mac",
        publicadas == [],
        str(publicadas),
    )


def _probar_publicar_mac(check) -> None:
    publicadas = []
    nombre = "MWX-APP-9.9.9-mac-arm64.zip"
    with _fakes_core(version="9.9.9", publicadas=publicadas):
        with _uploader_fake(nombre):
            at, error = _correr(_app_tab_ejecutable)
            if at is not None and not error:
                at = at.radio(key="mwx_release_plataforma").set_value(
                    "mac-arm64"
                ).run()
                error = _error_de(at)
            if at is not None and not error:
                at = at.button(key="mwx_release_publicar").click().run()
                error = _error_de(at)
    check(
        "mwx releases: publicar un ZIP Mac renderiza sin excepciones",
        not error,
        error,
    )
    check(
        "mwx releases: publicar un ZIP Mac llama publicar_release con filename",
        len(publicadas) == 1
        and publicadas[0]["version"] == "9.9.9-mac-arm64"
        and publicadas[0]["filename"] == nombre,
        str(publicadas),
    )
    check(
        "mwx releases: la publicacion Mac confirma plataforma en el exito",
        at is not None
        and any("macOS" in str(s.value) for s in at.success),
        str([str(s.value) for s in at.success]) if at is not None else "sin app",
    )


def _probar_tabla_plataformas(check) -> None:
    meta_win = _meta("9.9.9", filename="MWX-APP-9.9.9.zip")
    meta_mac = _meta(
        "9.9.9-mac-arm64",
        filename="MWX-APP-9.9.9-mac-arm64.zip",
    )
    datos = {"windows": meta_win, "mac-arm64": meta_mac}
    with _fakes_core(
        version="9.9.9",
        ultimo_fn=_ultimo_por_plataforma(datos),
        listar_fn=lambda limite=5, plataforma="": [meta_win, meta_mac],
    ):
        at, error = _correr(_app_tab_ejecutable)
    check(
        "mwx releases: la tabla multi-plataforma renderiza sin excepciones",
        at is not None,
        error,
    )
    if at is None:
        return
    check(
        "mwx releases: la tabla tiene columna Plataforma",
        len(at.dataframe) == 1
        and "Plataforma" in list(at.dataframe[0].value.columns),
        str(list(at.dataframe[0].value.columns) if at.dataframe else []),
    )
    if not at.dataframe:
        return
    tabla = at.dataframe[0].value
    plataformas = [str(valor) for valor in tabla["Plataforma"].tolist()]
    check(
        "mwx releases: la tabla muestra filas Windows y macOS",
        any("Windows" in valor for valor in plataformas)
        and any("macOS" in valor for valor in plataformas),
        str(plataformas),
    )
    check(
        "mwx releases: la tabla conserva el filename de cada release",
        "MWX-APP-9.9.9.zip" in [str(v) for v in tabla["filename"].tolist()]
        and "MWX-APP-9.9.9-mac-arm64.zip"
        in [str(v) for v in tabla["filename"].tolist()],
        str([str(v) for v in tabla["filename"].tolist()]),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(check) -> None:
    _probar_sin_releases(check)
    _probar_con_release(check)
    _probar_dist_local(check)
    _probar_publicar_vacio(check)
    _probar_cache_plataforma(check)
    _probar_cache_fallback_viejo(check)
    _probar_selector_y_version(check)
    _probar_publicar_nombre_windows_en_mac(check)
    _probar_publicar_mac(check)
    _probar_tabla_plataformas(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
