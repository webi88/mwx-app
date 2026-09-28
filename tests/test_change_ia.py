"""Tests rapidos de la IA de quejas para reportes en Change.org (sin red).

Cubre el contrato congelado
``ia.generador_contenido.generar_queja_change(contexto, evitar=None,
variante=None) -> {"ok", "queja", "usada_ia", "error"}`` y el prompt
``ia.prompts.get_prompt_queja_change(contexto, variante)``:

  (a) Exito de la IA: se usa el texto del modelo tal cual (usada_ia=True).
  (b) Fallo de la IA (excepcion): fallback local no vacio, sin lanzar.
  (c) Respuesta vacia: fallback local no vacio.
  (d) Unicidad: si la IA repite un texto de ``evitar``, reintenta UNA vez con
      "NO repitas este texto ya usado" y cae al fallback si sigue repetido.
  (e) ``evitar`` tolera None, strings sueltos y listas con None/espacios.
  (f) Prompt: menciona Change.org, el contexto, exige 60-140 palabras en
      primera persona y rota el angulo entre variantes.
  (g) Fallback local: >=5 plantillas por angulo, varia entre semillas, no
      copia el contexto largo y tolera None/"".
  (h) Normalizacion: sin ``---``, sin saltos dobles ni fences; recorte a 1200
      en frase completa.

Determinista y sin red: ``GeneradorContenido._chat`` SIEMPRE se reemplaza por
un fake (no se usa OPENAI_API_KEY real).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_change_ia.py   (solo este archivo)
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia.generador_contenido as gc  # noqa: E402
from ia.generador_contenido import (  # noqa: E402
    GeneradorContenido,
    _queja_change_local,
    generar_queja_change,
)
from ia.prompts import ANGULOS_QUEJA_CHANGE, get_prompt_queja_change  # noqa: E402

_CLAVES = {"ok", "queja", "usada_ia", "error"}


def _chat_fijo(respuesta="", capturas=None):
    """Fake de ``_chat`` que devuelve SIEMPRE ``respuesta`` (sin red)."""

    def _fake(self, prompt, temperature=0.7):
        if capturas is not None:
            capturas.append(prompt)
        return respuesta

    return _fake


def _chat_error(error: Exception, capturas=None):
    """Fake de ``_chat`` que lanza ``error`` (simula API/key/red caida)."""

    def _fake(self, prompt, temperature=0.7):
        if capturas is not None:
            capturas.append(prompt)
        raise error

    return _fake


# --------------------------------------------------------------------------- #
# (a) Exito IA
# --------------------------------------------------------------------------- #
def test_exito_ia(check):
    print("(a) exito IA: usa el texto del modelo tal cual")
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_fijo("Texto fake de queja")
    ):
        resultado = generar_queja_change("Corrupción inmobiliaria")

    check("exito: ok True", resultado.get("ok") is True, repr(resultado.get("ok")))
    check(
        "exito: usada_ia True",
        resultado.get("usada_ia") is True,
        repr(resultado.get("usada_ia")),
    )
    check(
        "exito: queja == texto del modelo",
        resultado.get("queja") == "Texto fake de queja",
        repr(resultado.get("queja")),
    )
    check("exito: error vacio", resultado.get("error") == "", repr(resultado.get("error")))
    check(
        "contrato: claves exactas y tipos",
        set(resultado.keys()) == _CLAVES
        and isinstance(resultado["queja"], str)
        and isinstance(resultado["error"], str)
        and isinstance(resultado["ok"], bool)
        and isinstance(resultado["usada_ia"], bool),
        repr(sorted(resultado.keys())),
    )


# --------------------------------------------------------------------------- #
# (b/c) Fallo y respuesta vacia -> fallback local
# --------------------------------------------------------------------------- #
def test_fallo_ia(check):
    print("(b) fallo IA: excepcion -> fallback local sin lanzar")
    with mock.patch.object(
        GeneradorContenido,
        "_chat",
        _chat_error(RuntimeError("OPENAI_API_KEY invalida (test)")),
    ):
        try:
            resultado = generar_queja_change("Corrupción inmobiliaria")
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
            resultado = {}

    check("fallo: NO lanza", error is None, repr(error))
    check(
        "fallo: ok True y usada_ia False",
        resultado.get("ok") is True and resultado.get("usada_ia") is False,
        repr((resultado.get("ok"), resultado.get("usada_ia"))),
    )
    check(
        "fallo: queja no vacia",
        bool(str(resultado.get("queja") or "").strip()),
        repr(str(resultado.get("queja"))[:80]),
    )
    check(
        "fallo: error no vacio y <=300 chars",
        bool(resultado.get("error")) and len(resultado["error"]) <= 300,
        repr(resultado.get("error")),
    )

    print("(c) respuesta vacia -> fallback local")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_fijo("   \n  ")):
        vacio = generar_queja_change("Corrupción inmobiliaria")
    check(
        "vacia: ok True, usada_ia False y queja no vacia",
        vacio.get("ok") is True
        and vacio.get("usada_ia") is False
        and bool(str(vacio.get("queja") or "").strip()),
        repr(str(vacio.get("queja"))[:80]),
    )
    check(
        "vacia: error describe la respuesta vacia",
        "vacia" in str(vacio.get("error") or "").lower(),
        repr(vacio.get("error")),
    )


# --------------------------------------------------------------------------- #
# (d/e) Anti-repeticion y tolerancia de `evitar`
# --------------------------------------------------------------------------- #
def test_unicidad(check):
    print("(d) anti-repeticion: reintento unico y fallback distinto")
    capturas = []
    with mock.patch.object(
        GeneradorContenido,
        "_chat",
        _chat_fijo("Texto fake de queja", capturas),
    ):
        primera = generar_queja_change("Corrupción inmobiliaria")
        segunda = generar_queja_change(
            "Corrupción inmobiliaria", evitar=[primera["queja"]]
        )

    check(
        "unicidad: la 1a llamada usa el texto de la IA",
        primera.get("queja") == "Texto fake de queja"
        and primera.get("usada_ia") is True,
        repr(primera.get("queja")),
    )
    check(
        "unicidad: la 2a devuelve algo DISTINTO y no vacio",
        bool(segunda.get("queja")) and segunda["queja"] != primera["queja"],
        repr(str(segunda.get("queja"))[:90]),
    )
    check(
        "unicidad: la 2a cae al fallback local (usada_ia False)",
        segunda.get("usada_ia") is False,
        repr(segunda.get("usada_ia")),
    )
    check(
        "unicidad: el reintento avisa 'NO repitas este texto ya usado'",
        any("NO repitas este texto ya usado" in p for p in capturas),
        f"prompts capturados={len(capturas)}",
    )
    check(
        "unicidad: sin --- ni saltos dobles en la queja final",
        "---" not in segunda["queja"] and "\n\n" not in segunda["queja"],
        repr(segunda["queja"][-60:]),
    )


def test_evitar_tolerante(check):
    print("(e) `evitar` tolera None, strings sueltos y listas raras")
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_fijo("Texto fake de queja")
    ):
        con_none = generar_queja_change("tema", evitar=None)
        con_string = generar_queja_change(
            "tema", evitar="Texto fake de queja"
        )
        con_lista = generar_queja_change(
            "tema", evitar=[None, "   ", "Texto fake de queja"]
        )
        raro = generar_queja_change(123, evitar=123)

    check(
        "evitar None: la IA se usa normal",
        con_none.get("queja") == "Texto fake de queja"
        and con_none.get("usada_ia") is True,
        repr(con_none.get("queja")),
    )
    check(
        "evitar string suelto: evita el texto y usa fallback",
        con_string.get("usada_ia") is False
        and con_string["queja"] != "Texto fake de queja"
        and bool(con_string["queja"].strip()),
        repr(con_string["queja"][:70]),
    )
    check(
        "evitar lista con None/espacios: no lanza y evita el texto",
        con_lista.get("ok") is True
        and con_lista.get("usada_ia") is False
        and bool(con_lista["queja"].strip()),
        repr(con_lista["queja"][:70]),
    )
    check(
        "entradas raras (contexto/evitar no texto): no lanzan",
        raro.get("ok") is True and bool(str(raro.get("queja") or "").strip()),
        repr(str(raro.get("queja"))[:70]),
    )


# --------------------------------------------------------------------------- #
# (f) Prompt
# --------------------------------------------------------------------------- #
def test_prompt(check):
    print("(f) prompt de la queja de Change.org")
    p0 = get_prompt_queja_change("Corrupción inmobiliaria", 0)
    p1 = get_prompt_queja_change("Corrupción inmobiliaria", 1)
    p2 = get_prompt_queja_change("Corrupción inmobiliaria", 2)

    check("prompt: contiene Change.org", "Change.org" in p0)
    check("prompt: contiene el contexto", "Corrupción inmobiliaria" in p0)
    check(
        "prompt: exige 60-140 palabras en primera persona",
        "60 a 140" in p0 and "primera persona" in p0,
    )
    check(
        "prompt: prohibe copiar el contexto e inventar hechos",
        "PROHIBIDO copiar" in p0 and "PROHIBIDO inventar" in p0,
    )
    check(
        "prompt: bloque 'TEMA DE REFERENCIA' etiquetado",
        "TEMA DE REFERENCIA" in p0,
    )
    check(
        "prompt: variantes 0/1/2 distintas con angulos distintos",
        len({p0, p1, p2}) == 3
        and "DESINFORMACIÓN" in p0
        and "DISCURSO DE ODIO" in p1
        and "INCITACIÓN A LA VIOLENCIA" in p2,
    )
    check(
        "prompt: lista de angulos amplia (>=5)",
        len(ANGULOS_QUEJA_CHANGE) >= 5,
        repr(len(ANGULOS_QUEJA_CHANGE)),
    )
    check(
        "prompt: variante fuera de rango y contexto None/'' no lanzan",
        isinstance(get_prompt_queja_change("x", 99), str)
        and isinstance(get_prompt_queja_change("x", -1), str)
        and isinstance(get_prompt_queja_change(None), str)
        and isinstance(get_prompt_queja_change(""), str),
    )


# --------------------------------------------------------------------------- #
# (g) Fallback local
# --------------------------------------------------------------------------- #
def test_fallback_local(check):
    print("(g) fallback local `_queja_change_local`")
    textos = [_queja_change_local("Corrupción inmobiliaria", s) for s in range(6)]
    check(
        "fallback: 6 llamadas producen >1 texto distinto",
        len(set(textos)) > 1,
        f"unicos={len(set(textos))}",
    )
    check("fallback: ninguno vacio", all(t.strip() for t in textos))
    check(
        "fallback: todos mencionan Change.org y las normas de la comunidad",
        all(
            "Change.org" in t and "normas de la comunidad" in t for t in textos
        ),
    )
    check(
        "fallback: >=5 plantillas por cada angulo",
        len(gc._PLANTILLAS_QUEJA_CHANGE) == len(ANGULOS_QUEJA_CHANGE)
        and all(len(v) >= 5 for v in gc._PLANTILLAS_QUEJA_CHANGE.values()),
        repr({k: len(v) for k, v in gc._PLANTILLAS_QUEJA_CHANGE.items()}),
    )
    contexto_largo = (
        "Corrupción inmobiliaria en los manglares de la zona costera con "
        "detalles internos que no deben copiarse en el reporte"
    )
    check(
        "fallback: contexto largo NO se copia literal",
        "manglares de la zona" not in _queja_change_local(contexto_largo, 0),
    )
    check(
        "fallback: contexto None/'' no lanza",
        bool(_queja_change_local(None).strip())
        and bool(_queja_change_local("").strip()),
    )


# --------------------------------------------------------------------------- #
# (h) Normalizacion y recorte
# --------------------------------------------------------------------------- #
def test_normalizacion(check):
    print("(h) normalizacion a un parrafo y recorte a 1200")
    respuesta = (
        "```\nPrimer parrafo de la queja.\n\nSegundo parrafo del reporte.\n"
        "---\nOtra opcion que sobra.\n```"
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_fijo(respuesta)):
        normalizada = generar_queja_change("tema")
    check(
        "normaliza: un solo parrafo (sin ---, saltos dobles ni fences)",
        "---" not in normalizada["queja"]
        and "\n\n" not in normalizada["queja"]
        and "\n" not in normalizada["queja"]
        and "```" not in normalizada["queja"],
        repr(normalizada["queja"][:90]),
    )
    check(
        "normaliza: toma el primer fragmento y descarta lo demas",
        "Primer parrafo de la queja." in normalizada["queja"]
        and "Otra opcion" not in normalizada["queja"],
        repr(normalizada["queja"][:90]),
    )

    largo = "Considero que esta peticion debe revisarse. " * 200
    with mock.patch.object(GeneradorContenido, "_chat", _chat_fijo(largo)):
        recortada = generar_queja_change("tema")
    check(
        "recorte: <=1200 caracteres",
        len(recortada["queja"]) <= 1200,
        f"len={len(recortada['queja'])}",
    )
    check(
        "recorte: termina en frase completa",
        recortada["queja"].rstrip().endswith((".", "!", "?")),
        repr(recortada["queja"][-60:]),
    )


# --------------------------------------------------------------------------- #
# Contrato congelado
# --------------------------------------------------------------------------- #
def test_contrato(check):
    print("(i) firma congelada de generar_queja_change")
    firma = inspect.signature(generar_queja_change)
    parametros = list(firma.parameters)
    check(
        "contrato: firma (contexto, evitar=None, variante=None)",
        parametros == ["contexto", "evitar", "variante"]
        and firma.parameters["evitar"].default is None
        and firma.parameters["variante"].default is None,
        repr(str(firma)),
    )
    check(
        "contrato: generar_queja_change es funcion de modulo",
        inspect.isfunction(generar_queja_change),
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_exito_ia(check)
    test_fallo_ia(check)
    test_unicidad(check)
    test_evitar_tolerante(check)
    test_prompt(check)
    test_fallback_local(check)
    test_normalizacion(check)
    test_contrato(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_change_ia.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
