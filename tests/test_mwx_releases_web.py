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

Los fakes se instalan DENTRO del script de AppTest (solo ASCII) y las caches
`@st.cache_data` de releases se limpian antes/despues de cada bloque.
"""
from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from web.operaciones import mwx_app  # noqa: E402


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
# Runner
# ---------------------------------------------------------------------------


def run(check) -> None:
    _probar_sin_releases(check)
    _probar_con_release(check)
    _probar_dist_local(check)
    _probar_publicar_vacio(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
