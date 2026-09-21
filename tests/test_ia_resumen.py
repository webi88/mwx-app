"""Tests rapidos de `FiltrosAlertas.resumir` (sin red, sin IA real y sin Chrome).

Cubre el bug real de la pagina "Resumenes Ejecutivos"
(`web/operaciones/resumenes.py` llamaba `FiltrosAlertas().resumir(textos)` pero
el metodo no existia y la pagina siempre caia en `st.error`):

  (a) OpenAI monkeypatcheado para LANZAR excepcion -> `resumir` devuelve el
      fallback local (string no vacio) y NO lanza.
  (b) Lista vacia / `None` -> string claro sin excepcion.
  (c) OpenAI falso que devuelve texto (con fences ```) -> devuelve ese texto
      limpio.
  (d) Respuesta vacia de OpenAI -> cae al fallback local.
  (e) `_resumen_fallback` directo y `get_prompt_resumen_ejecutivo()` existen.
  (f) `web/operaciones/resumenes.py` sigue llamando `.resumir(`.

Compatible con openai 0.28 (instalado en el venv) y >= 1.0: el parche elige la
ruta segun `hasattr(openai, "OpenAI")`. Determinista y sin red: la llamada de
chat SIEMPRE se reemplaza por un fake.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_ia_resumen.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import openai  # noqa: E402

from ia.filtros_alertas import FiltrosAlertas  # noqa: E402
from ia.prompts import get_prompt_resumen_ejecutivo  # noqa: E402


def _respuesta(texto: str):
    """Respuesta minima con la forma `response.choices[0].message.content`."""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=texto))]
    )


@contextlib.contextmanager
def _openai_falso(contenido: str = "", error: Exception | None = None):
    """Parchea la llamada de chat de OpenAI (0.28 y >= 1.0) sin tocar la red.

    Con `error` lanza esa excepcion (simula API/key/red caida); si no, devuelve
    `contenido` como respuesta del modelo.
    """

    def _create(**kwargs):
        if error is not None:
            raise error
        return _respuesta(contenido)

    if hasattr(openai, "OpenAI"):  # openai >= 1.0
        cliente = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=_create)
            )
        )
        with mock.patch.object(openai, "OpenAI", lambda **kwargs: cliente):
            yield
    else:  # openai 0.28.x
        with mock.patch.object(
            openai, "ChatCompletion", types.SimpleNamespace(create=_create)
        ):
            yield


# --------------------------------------------------------------------------- #
# (a) OpenAI lanza -> fallback local, sin excepcion
# --------------------------------------------------------------------------- #
def test_openai_lanza_usa_fallback(check):
    print("(a) OpenAI cae -> fallback local sin lanzar")
    filtros = FiltrosAlertas()
    titulares = [
        "El congreso aprueba la reforma electoral",
        "Lluvias fuertes provocan inundaciones en el sureste",
        "La seleccion mexicana gana el partido amistoso",
    ]
    with _openai_falso(error=RuntimeError("OPENAI_API_KEY invalida (test)")):
        try:
            resultado = filtros.resumir(titulares)
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
            resultado = ""

    check("fallback: OpenAI falla y resumir NO lanza", error is None, repr(error))
    check(
        "fallback: devuelve un string no vacio",
        isinstance(resultado, str) and bool(resultado.strip()),
        repr(resultado[:120]),
    )
    check("fallback: reporta el total de titulares (3)", "3" in resultado, repr(resultado[:120]))
    check(
        "fallback: menciona el primer titular",
        titulares[0] in resultado,
        repr(resultado[:200]),
    )
    check(
        "fallback: usa los temas/palabras frecuentes locales",
        "Temas con" in resultado,
        repr(resultado[:200]),
    )


# --------------------------------------------------------------------------- #
# (b) Lista vacia / None -> string claro, sin excepcion
# --------------------------------------------------------------------------- #
def test_lista_vacia(check):
    print("(b) lista vacia -> mensaje claro sin excepcion")
    filtros = FiltrosAlertas()
    with _openai_falso(error=RuntimeError("no deberia llamarse (test)")):
        try:
            vacio = filtros.resumir([])
            error_vacio = None
        except Exception as e:  # noqa: BLE001
            error_vacio = e
            vacio = ""
        try:
            nulo = filtros.resumir(None)
            error_nulo = None
        except Exception as e:  # noqa: BLE001
            error_nulo = e
            nulo = ""

    check("vacio: resumir([]) NO lanza", error_vacio is None, repr(error_vacio))
    check(
        "vacio: resumir([]) devuelve mensaje string",
        isinstance(vacio, str) and bool(vacio.strip()),
        repr(vacio[:120]),
    )
    check("vacio: resumir(None) NO lanza", error_nulo is None, repr(error_nulo))
    check(
        "vacio: resumir(None) devuelve mensaje string",
        isinstance(nulo, str) and bool(nulo.strip()),
        repr(nulo[:120]),
    )


# --------------------------------------------------------------------------- #
# (c) OpenAI falso devuelve texto -> ese texto, limpio de fences
# --------------------------------------------------------------------------- #
def test_openai_devuelve_texto(check):
    print("(c) OpenAI falso devuelve texto -> texto limpio")
    filtros = FiltrosAlertas()
    esperado = "Los temas principales son la reforma electoral y las lluvias en el sureste."
    with _openai_falso(contenido=f"```markdown\n{esperado}\n```"):
        resultado = filtros.resumir(["Reforma electoral aprobada", "Lluvias en el sureste"])
    check(
        "IA: devuelve el texto del modelo sin fences",
        resultado == esperado,
        repr(resultado),
    )

    # (d) respuesta vacia -> fallback local
    with _openai_falso(contenido="   \n  "):
        vacio = filtros.resumir(["Un titular cualquiera"])
    check(
        "IA: respuesta vacia cae al fallback local no vacio",
        isinstance(vacio, str) and bool(vacio.strip()),
        repr(vacio[:120]),
    )
    check(
        "IA: el fallback por respuesta vacia conserva el titular",
        "Un titular cualquiera" in vacio,
        repr(vacio[:160]),
    )


# --------------------------------------------------------------------------- #
# (e/f) Contrato: prompt, fallback directo y llamada de la pagina
# --------------------------------------------------------------------------- #
def test_contrato(check):
    print("(e/f) contrato de resumir, prompt y llamada de resumenes.py")
    filtros = FiltrosAlertas()
    check(
        "contrato: FiltrosAlertas expone resumir(titulares)",
        callable(getattr(filtros, "resumir", None)),
    )
    check(
        "contrato: _resumen_fallback existe y devuelve texto",
        callable(getattr(filtros, "_resumen_fallback", None))
        and bool(filtros._resumen_fallback(["Titular uno", "Titular dos"]).strip()),
    )

    prompt = get_prompt_resumen_ejecutivo()
    check(
        "prompt: get_prompt_resumen_ejecutivo devuelve texto",
        isinstance(prompt, str) and len(prompt.strip()) > 50,
    )
    check(
        "prompt: pide 5 a 10 lineas y prohibe inventar",
        "5" in prompt and "10" in prompt and "inventes" in prompt.lower(),
    )

    fuente = (RAIZ / "web" / "operaciones" / "resumenes.py").read_text(encoding="utf-8")
    check(
        "pagina: resumenes.py sigue llamando a .resumir(",
        ".resumir(" in fuente,
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_openai_lanza_usa_fallback(check)
    test_lista_vacia(check)
    test_openai_devuelve_texto(check)
    test_contrato(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_ia_resumen.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
