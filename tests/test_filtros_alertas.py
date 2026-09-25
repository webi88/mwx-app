"""Tests rapidos de `FiltrosAlertas` (sin red, sin IA real y sin Chrome).

Cubre el contrato congelado del clasificador que usa `alertas/motor.py`:

  (a) Clasifica en lotes de <= 20 y devuelve SOLO las relevantes.
  (b) Marca `es_principal`, `kw_principal` y `resumen_ai` cuando la IA puede.
  (c) FAIL-OPEN: lote con error o respuesta imparseable -> conserva ese lote
      sin marcar; sin `OPENAI_API_KEY` -> conserva TODAS sin marcar; nunca lanza.
  (d) Sin keywords principales -> filtra igual, sin marcar principales.
  (e) Tolera menciones sin `titulo`/`resumen` (incluso valores None).
  (f) `resumir` y `agrupar_temas` conservan firma y tipo de retorno.
  (g) Compatibilidad openai 0.28/>=1.0 y cero claves hardcodeadas.

SIN coste: `FiltrosAlertas._chat` y `FiltrosAlertas._api_key` se monkeypatchean
en TODOS los casos; jamas se llama a la API real.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_filtros_alertas.py   (solo este archivo)
"""
from __future__ import annotations

import contextlib
import inspect
import os
import re
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from core.config import settings  # noqa: E402
from ia.filtros_alertas import MAX_MENCIONES_POR_LOTE, FiltrosAlertas  # noqa: E402


@contextlib.contextmanager
def _chat_falso(respuestas, key="test-key"):
    """Parchea `_chat` y `_api_key` para no tocar la red en ningun caso.

    `respuestas` puede ser:
      - un str: misma respuesta para todos los lotes,
      - una Exception: todos los lotes fallan,
      - una lista/tupla: una respuesta por llamada (la ultima se repite),
      - un callable(prompt, idx) -> str/Exception.

    Devuelve la lista de prompts usados (`llamadas`).
    """
    llamadas = []
    contador = {"n": 0}

    def _fake_chat(self, prompt, temperature=0.3, timeout=None):
        idx = contador["n"]
        contador["n"] += 1
        llamadas.append(prompt)
        if callable(respuestas):
            respuesta = respuestas(prompt, idx)
        elif isinstance(respuestas, (list, tuple)):
            respuesta = respuestas[min(idx, len(respuestas) - 1)]
        else:
            respuesta = respuestas
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    with mock.patch.object(FiltrosAlertas, "_chat", _fake_chat), mock.patch.object(
        FiltrosAlertas, "_api_key", lambda self: key
    ):
        yield llamadas


def _items_del_prompt(prompt: str) -> list[str]:
    """Lineas numeradas (las menciones) del prompt del clasificador."""
    return re.findall(r"(?m)^\s*\d+\.\s.*$", prompt)


def _respuesta_todos_relevantes(prompt, idx):
    """Respuesta valida para cada item del prompt (resumen por mencion)."""
    total = len(_items_del_prompt(prompt))
    return "\n".join(f"{i}. Resumen de la nota {i}" for i in range(1, total + 1))


# --------------------------------------------------------------------------- #
# (g) Clave: respeta OPENAI_API_KEY del entorno y cae a settings
# --------------------------------------------------------------------------- #
def test_api_key_entorno(check):
    print("(g) _api_key: entorno primero, settings como respaldo")
    filtros = FiltrosAlertas()
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "  sk-test-entorno  "}):
        check(
            "api_key: prioriza OPENAI_API_KEY del entorno",
            filtros._api_key() == "sk-test-entorno",
            repr(filtros._api_key()),
        )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        esperada = str(getattr(settings, "openai_api_key", "") or "").strip()
        check(
            "api_key: sin env cae a settings (nunca hardcodea)",
            filtros._api_key() == esperada,
            repr(filtros._api_key()[:8]),
        )


# --------------------------------------------------------------------------- #
# (a/b) Lote OK: filtra y marca
# --------------------------------------------------------------------------- #
def test_clasifica_y_marca(check):
    print("(a/b) lote OK: filtra y marca es_principal/kw_principal/resumen_ai")
    menciones = [
        {
            "titulo": "El gobernador de Durango anuncia obra pública",
            "resumen": "Inversión estatal",
            "fuente": "El Siglo",
            "enlace": "https://n/1",
        },
        {
            "titulo": "Famoso actor termina relación amorosa",
            "resumen": "Farándula",
            "fuente": "TVNotas",
            "enlace": "https://n/2",
        },
        {
            "titulo": "Mia Nicole presume nuevo look",
            "resumen": "Espectáculos",
            "fuente": "Quién",
            "enlace": "https://n/3",
        },
        {
            "titulo": "Lluvias provocan inundaciones en Durango",
            "resumen": "Protección Civil atiende",
            "fuente": "Milenio",
            "enlace": "https://n/4",
        },
    ]
    respuesta = (
        "1. El gobernador anunció obra pública en Durango.\n"
        "2. NO\n"
        "3. NO\n"
        "4. Protección Civil atiende las inundaciones."
    )
    filtros = FiltrosAlertas()
    with _chat_falso(respuesta) as llamadas:
        resultado = filtros.clasificar_menciones(
            menciones, ["Gobernador", "Durango"], "Durango"
        )

    check("clasificar: devuelve SOLO las relevantes (2 de 4)", len(resultado) == 2, f"{len(resultado)}")
    check(
        "clasificar: la 1a relevante trae resumen_ai de una frase",
        resultado[0].get("resumen_ai") == "El gobernador anunció obra pública en Durango.",
        repr(resultado[0].get("resumen_ai")),
    )
    check("clasificar: marca es_principal=True con keyword en el titulo",
          resultado[0].get("es_principal") is True)
    check("clasificar: kw_principal es una de las keywords",
          resultado[0].get("kw_principal") in ("Gobernador", "Durango"),
          repr(resultado[0].get("kw_principal")))
    check(
        "clasificar: la 2a relevante tambien queda principal por 'Durango'",
        resultado[1].get("es_principal") is True and resultado[1].get("kw_principal") == "Durango",
        repr(resultado[1].get("kw_principal")),
    )
    check(
        "clasificar: NO muta las menciones originales",
        "resumen_ai" not in menciones[0] and "es_principal" not in menciones[0],
    )
    check("clasificar: usa UN solo lote para 4 menciones", len(llamadas) == 1, f"{len(llamadas)}")

    prompt = llamadas[0]
    check("prompt: incluye la regla de farandula", "farándula" in prompt)
    check(
        "prompt: menciona Mexico y publicidad",
        "México" in prompt and "publicidad" in prompt.lower(),
    )
    check(
        "prompt: incluye keywords y localidad",
        "Gobernador" in prompt and "Durango" in prompt,
    )
    check("prompt: lista las 4 menciones numeradas", len(_items_del_prompt(prompt)) == 4)


def test_lotes_maximo_20(check):
    print("(a) clasifica en lotes de <= 20")
    menciones = [{"titulo": f"Nota {i}", "enlace": f"https://n/{i}"} for i in range(1, 46)]
    filtros = FiltrosAlertas()
    with _chat_falso(_respuesta_todos_relevantes) as llamadas:
        resultado = filtros.clasificar_menciones(menciones, ["Nota"])
    tamanos = [len(_items_del_prompt(p)) for p in llamadas]
    check("lotes: 45 menciones -> 3 llamadas", len(llamadas) == 3, f"{len(llamadas)}")
    check("lotes: ningun lote pasa de 20", max(tamanos) <= 20, str(tamanos))
    check("lotes: el ultimo lote lleva el resto (5)", tamanos[-1] == 5, str(tamanos))
    check("lotes: devuelve las 45 relevantes", len(resultado) == 45, f"{len(resultado)}")
    check("lotes: todas marcadas con resumen_ai", all(m.get("resumen_ai") for m in resultado))
    check("lotes: todas marcadas principales", all(m.get("es_principal") is True for m in resultado))
    check("lotes: la constante del contrato es 20", MAX_MENCIONES_POR_LOTE == 20)


# --------------------------------------------------------------------------- #
# (c) Fail-open
# --------------------------------------------------------------------------- #
def test_fail_open_por_lote(check):
    print("(c) lote que falla -> fail-open de ese lote, sin lanzar")
    menciones = [{"titulo": f"Nota {i}", "enlace": f"https://n/{i}"} for i in range(1, 26)]
    respuestas = ["1. Resumen primera\n2. NO", RuntimeError("red caida (test)")]
    filtros = FiltrosAlertas()
    with _chat_falso(respuestas) as llamadas:
        try:
            resultado = filtros.clasificar_menciones(menciones, ["Nota"])
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
            resultado = []
    check("fail-open lote: NO lanza", error is None, repr(error))
    check("fail-open lote: 2 llamadas (20 + 5)", len(llamadas) == 2, f"{len(llamadas)}")
    check(
        "fail-open lote: conserva 24 (19 del 1er lote + 5 del lote caido)",
        len(resultado) == 24,
        f"{len(resultado)}",
    )
    check(
        "fail-open lote: el 1er lote quedo marcado",
        resultado[0].get("resumen_ai") == "Resumen primera",
        repr(resultado[0].get("resumen_ai")),
    )
    check(
        "fail-open lote: las del lote caido van SIN marcar",
        all("resumen_ai" not in m and "es_principal" not in m for m in resultado[19:]),
    )


def test_fail_open_total(check):
    print("(c) todos los lotes fallan -> se conservan todas sin marcar")
    menciones = [{"titulo": f"Nota {i}", "enlace": f"https://n/{i}"} for i in range(1, 8)]
    filtros = FiltrosAlertas()
    with _chat_falso(RuntimeError("sin red (test)")):
        try:
            resultado = filtros.clasificar_menciones(menciones, ["Nota"], "Durango")
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
            resultado = []
    check("fail-open total: NO lanza", error is None, repr(error))
    check("fail-open total: conserva las 7", len(resultado) == 7, f"{len(resultado)}")
    check(
        "fail-open total: nada marcado",
        all("resumen_ai" not in m and "es_principal" not in m for m in resultado),
    )
    check("fail-open total: no muta las originales", all("resumen_ai" not in m for m in menciones))


def test_sin_api_key_fail_open(check):
    print("(c) sin OPENAI_API_KEY -> fail-open total sin coste")
    menciones = [
        {"titulo": "Nota uno", "enlace": "https://n/1"},
        {"titulo": "Nota dos", "enlace": "https://n/2"},
    ]
    filtros = FiltrosAlertas()
    with _chat_falso("1. NO\n2. NO", key="") as llamadas:
        resultado = filtros.clasificar_menciones(menciones, ["Nota"], "Durango")
    check("sin key: no llama a la IA", llamadas == [], str(llamadas))
    check("sin key: conserva TODAS", len(resultado) == 2, f"{len(resultado)}")
    check("sin key: nada marcado", all("es_principal" not in m for m in resultado))
    check("sin key: tampoco agrega resumen_ai", all("resumen_ai" not in m for m in resultado))


# --------------------------------------------------------------------------- #
# (d) Sin keywords principales
# --------------------------------------------------------------------------- #
def test_keywords_vacio(check):
    print("(d) sin keywords principales: filtra igual, sin marcar principales")
    menciones = [
        {"titulo": "Publicidad de una marca", "enlace": "https://n/1"},
        {"titulo": "El congreso aprueba la reforma", "enlace": "https://n/2"},
    ]
    with _chat_falso("1. NO\n2. El congreso aprobó la reforma."):
        resultado = FiltrosAlertas().clasificar_menciones(menciones, [], "")
    check("keywords vacio: filtra las no relevantes", len(resultado) == 1, f"{len(resultado)}")
    check(
        "keywords vacio: conserva resumen_ai",
        resultado[0].get("resumen_ai") == "El congreso aprobó la reforma.",
    )
    check("keywords vacio: NO agrega es_principal", "es_principal" not in resultado[0])
    check("keywords vacio: NO agrega kw_principal", "kw_principal" not in resultado[0])

    with _chat_falso("1. NO"):
        sin_kw = FiltrosAlertas().clasificar_menciones(menciones[:1], None, "")
    check("keywords None: tambien filtra", sin_kw == [], repr(sin_kw))


# --------------------------------------------------------------------------- #
# (e) Tolerancia a campos ausentes
# --------------------------------------------------------------------------- #
def test_tolerante_sin_titulo(check):
    print("(e) tolera menciones sin titulo/resumen (y valores None)")
    menciones = [
        {"enlace": "https://x/1"},
        {"titulo": None, "resumen": None, "fuente": "X"},
        {},
        None,
    ]
    with _chat_falso("1. Resumen uno\n2. OK\n3. Resumen tres\n4. NO") as llamadas:
        try:
            resultado = FiltrosAlertas().clasificar_menciones(menciones, ["clave"])
            error = None
        except Exception as e:  # noqa: BLE001
            error = e
            resultado = []
    check("tolerante: NO lanza con campos ausentes", error is None, repr(error))
    check("tolerante: procesa los 3 dict (el None cae por 'NO')", len(resultado) == 3, f"{len(resultado)}")
    check("tolerante: resumen de la 1a", resultado[0].get("resumen_ai") == "Resumen uno")
    check("tolerante: 'OK' no inventa resumen", "resumen_ai" not in resultado[1])
    check("tolerante: usa placeholder 'Sin titulo' en el prompt", "Sin titulo" in llamadas[0])
    check(
        "tolerante: respeta el limite de un item por mencion",
        len(_items_del_prompt(llamadas[0])) == 4,
    )


def test_entrada_vacia(check):
    print("(e) entradas vacias/invalidas -> [] sin IA")
    filtros = FiltrosAlertas()
    with _chat_falso(RuntimeError("no deberia llamarse"), key="") as llamadas:
        check("vacio: None -> []", filtros.clasificar_menciones(None) == [])
        check("vacio: [] -> []", filtros.clasificar_menciones([]) == [])
        check("vacio: no llama a la IA", llamadas == [])
    with _chat_falso("1. Resumen\n", key="test"):
        unica = filtros.clasificar_menciones({"titulo": "Un dict suelto", "enlace": "https://n/1"}, ["dict"])
    check("vacio: un dict suelto se acepta como 1 mencion", len(unica) == 1, f"{len(unica)}")


# --------------------------------------------------------------------------- #
# (b) Match de keywords sin falsos positivos
# --------------------------------------------------------------------------- #
def test_keyword_limites(check):
    print("(b) match de keywords: limites de palabra y @")
    menciones = [
        {"titulo": "Mia Nicole de gira", "enlace": "https://n/1"},
        {"titulo": "Feria en Miami", "enlace": "https://n/2"},
    ]
    with _chat_falso("1. Relevante uno\n2. Relevante dos"):
        resultado = FiltrosAlertas().clasificar_menciones(menciones, ["Mia"], "")
    check("keyword: 'Mia' matchea la mencion correcta", resultado[0].get("es_principal") is True)
    check("keyword: 'Mia' NO matchea 'Miami'", resultado[1].get("es_principal") is False)
    check("keyword: kw_principal solo en la que matchea", "kw_principal" not in resultado[1])

    menciones_arroba = [{"titulo": "respaldo de @CuentaOficial hoy", "enlace": "https://n/3"}]
    with _chat_falso("1. Relevante"):
        con_arroba = FiltrosAlertas().clasificar_menciones(menciones_arroba, ["@cuentaoficial"], "")
    check("keyword: @keyword matchea con/sin arroba", con_arroba[0].get("es_principal") is True)


# --------------------------------------------------------------------------- #
# (f) Contratos de resumir y agrupar_temas
# --------------------------------------------------------------------------- #
def test_agrupar_temas_contrato(check):
    print("(f) agrupar_temas conserva firma, JSON y fallback")
    filtros = FiltrosAlertas()
    with _chat_falso('{"temas": [{"titulo": "reforma", "cantidad": 3}]}'):
        temas = filtros.agrupar_temas(["Reforma electoral", "Reforma judicial"])
    check(
        "agrupar: parsea el JSON de la IA",
        temas == [{"titulo": "reforma", "cantidad": 3}],
        repr(temas),
    )

    with _chat_falso('```json\n{"temas": [{"titulo": "agua", "cantidad": 2}]}\n```'):
        temas_fence = filtros.agrupar_temas(["Falta de agua", "Agua en el sur"])
    check(
        "agrupar: limpia fences ```json",
        temas_fence == [{"titulo": "agua", "cantidad": 2}],
        repr(temas_fence),
    )

    with _chat_falso(RuntimeError("sin red (test)")):
        fallback = filtros.agrupar_temas(
            ["Reforma electoral aprueba congreso", "Reforma electoral avanza senado"]
        )
    check(
        "agrupar: ante error usa el fallback local",
        isinstance(fallback, list)
        and bool(fallback)
        and all(isinstance(t, dict) and t.get("titulo") for t in fallback),
        repr(fallback),
    )

    with _chat_falso(RuntimeError("no deberia llamarse")) as llamadas:
        vacio = filtros.agrupar_temas([])
    check("agrupar: [] devuelve [] sin llamar a la IA", vacio == [], repr(vacio))
    check("agrupar: [] no gasto llamada", llamadas == [])

    firma = inspect.signature(FiltrosAlertas.agrupar_temas)
    check(
        "agrupar: firma (self, titulares)",
        list(firma.parameters) == ["self", "titulares"],
        str(list(firma.parameters)),
    )


def test_resumir_contrato(check):
    print("(f) resumir conserva firma y fallback")
    filtros = FiltrosAlertas()
    with _chat_falso("Resumen ejecutivo de prueba."):
        check(
            "resumir: devuelve el texto de la IA",
            filtros.resumir(["Titular"]) == "Resumen ejecutivo de prueba.",
        )
    with _chat_falso(RuntimeError("caida (test)")):
        r = filtros.resumir(["Titular uno", "Titular dos"])
    check("resumir: ante error devuelve fallback no vacio", isinstance(r, str) and bool(r.strip()))
    check("resumir: el fallback conserva el titular", "Titular uno" in r)
    with _chat_falso(RuntimeError("no deberia llamarse")):
        vacio = filtros.resumir([])
    check("resumir: [] devuelve mensaje sin IA", isinstance(vacio, str) and bool(vacio.strip()))

    firma = inspect.signature(FiltrosAlertas.resumir)
    check(
        "resumir: firma (self, titulares)",
        list(firma.parameters) == ["self", "titulares"],
        str(list(firma.parameters)),
    )


# --------------------------------------------------------------------------- #
# (g) Contrato del clasificador y fuente sin hardcodeo
# --------------------------------------------------------------------------- #
def test_contrato_y_fuente(check):
    print("(g) contrato de clasificar_menciones y fuente sin claves")
    firma = inspect.signature(FiltrosAlertas.clasificar_menciones)
    parametros = list(firma.parameters)
    check(
        "contrato: firma (self, menciones, keywords_principales, localidad)",
        parametros == ["self", "menciones", "keywords_principales", "localidad"],
        str(parametros),
    )
    check(
        "contrato: keywords_principales default None",
        firma.parameters["keywords_principales"].default is None,
    )
    check("contrato: localidad default ''", firma.parameters["localidad"].default == "")

    fuente = (RAIZ / "ia" / "filtros_alertas.py").read_text(encoding="utf-8")
    check(
        "fuente: _chat soporta openai >= 1.0 (hasattr OpenAI)",
        'hasattr(openai, "OpenAI")' in fuente,
    )
    check(
        "fuente: la unica llamada 0.28 esta en _chat",
        fuente.count("openai.ChatCompletion.create(") == 1,
        str(fuente.count("openai.ChatCompletion.create(")),
    )
    check("fuente: no hay API key hardcodeada", "sk-" not in fuente)
    check("fuente: define MAX_MENCIONES_POR_LOTE=20", "MAX_MENCIONES_POR_LOTE = 20" in fuente)
    check(
        "fuente: define clasificar_menciones y _clasificar_lote",
        "def clasificar_menciones" in fuente and "def _clasificar_lote" in fuente,
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_api_key_entorno(check)
    test_clasifica_y_marca(check)
    test_lotes_maximo_20(check)
    test_fail_open_por_lote(check)
    test_fail_open_total(check)
    test_sin_api_key_fail_open(check)
    test_keywords_vacio(check)
    test_tolerante_sin_titulo(check)
    test_entrada_vacia(check)
    test_keyword_limites(check)
    test_agrupar_temas_contrato(check)
    test_resumir_contrato(check)
    test_contrato_y_fuente(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_filtros_alertas.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
