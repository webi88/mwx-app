"""Tests del generador del modo ACTIVIDAD (producto leve: 3-4 posts/cuenta).

Cubre el pedido del dueño: ademas de las activaciones masivas, el modo
ACTIVIDAD usa la MISMA logica de posts con hashtags pero **TODOS los
hashtags pedidos** (hoy el rol hashtags elige un subconjunto aleatorio por
texto con `solo_hashtags_pedidos`).

Reglas verificadas aqui (sin red, sin OpenAI real y sin Chrome):

  * Con una lista de hashtags que CABE en un tuit (100 chars), TODOS los
    textos de TODAS las cuentas llevan TODOS los hashtags.
  * Con una lista que NO cabe (p. ej. 6 hashtags largos), los hashtags se
    reparten entre los N textos de ESA cuenta con cobertura total (cada
    hashtag aparece al menos una vez) y ningun texto supera 100 chars ni
    termina en hashtag ni inventa hashtags ajenos.
  * ``n_por_cuenta`` int y dict ``{usuario: n}``: cantidad EXACTA por cuenta.
  * Textos unicos por cuenta, con registro (politica/activista/ciudadana) y
    perfil (formal/ciudadano/popular) respetados.
  * IA simulada (monkeypatch de ``_chat``) y fallback local con IA caida.
  * ``verificar_cobertura_actividad`` en casos ok/fallo/input raro.
  * Fuzz de 50 corridas con IA caida, 3-4 hashtags y n=4: 0 violaciones de
    limite, cobertura, hashtag final, ajenos, cantidad y unicidad.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_actividad_textos.py  (solo este)
"""
from __future__ import annotations

import inspect
import json
import random
import re
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia.generador_contenido as gc  # noqa: E402
from ia.generador_contenido import (  # noqa: E402
    GeneradorContenido,
    generar_textos_actividad_por_cuenta,
    verificar_cobertura_actividad,
)

_RE_TAG = re.compile(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+")
_VOCALES_ACENTO = "áéíóúÁÉÍÓÚ"

# Combos de (registro, perfil) que rotan las cuentas de prueba.
_COMBOS = (
    ("politica", "formal"),
    ("activista", "ciudadano"),
    ("ciudadana", "popular"),
    ("politica", "ciudadano"),
    ("activista", "formal"),
    ("ciudadana", "ciudadano"),
    ("", ""),
    ("politica", "popular"),
    ("activista", "popular"),
)
_TAGS_CORTOS = ["#mexico", "#futbol", "#comunidad", "#barrio"]
# 6 hashtags LARGOS: el grupo completo (~127 chars) no cabe en un tuit.
_TAGS_LARGOS = [
    "#seleccionnacionalmx",
    "#comunidadorganizada",
    "#orgullobienentendido",
    "#trabajocomunitario",
    "#barriounidoyfuerte",
    "#mexicosiempreunido",
]
_POOL_FUZZ = [
    "#mexico", "#futbol", "#comunidad", "#barrio", "#seleccion",
    "#orgullo", "#familia", "#cultura",
]
_NARRATIVA = (
    "NOTICIA: el aviario de Durango colapso y Toño Ochoa no atendio el "
    "zoologico municipal tras el reporte ciudadano."
)


def _cuentas(n: int = 9) -> list[dict]:
    """Cuentas de prueba con combos de registro/perfil rotados."""
    cuentas = []
    for i in range(n):
        registro, perfil = _COMBOS[i % len(_COMBOS)]
        cuentas.append({
            "usuario": f"u{i}",
            "registro": registro,
            "perfil": perfil,
            "personalidad": f"le gusta el futbol y la musica {i}",
            "nombre": f"Cuenta Actividad {i}",
        })
    return cuentas


def _planos(salida) -> list:
    """Todos los textos de la salida ``{usuario: [textos]}``."""
    return [t for fila in (salida or {}).values() for t in (fila or [])]


def _tags_de(texto) -> set:
    """Conjunto de hashtags (minusculas) presentes en el texto."""
    return {m.group(0).lower() for m in _RE_TAG.finditer(str(texto or ""))}


def _sin_tags(texto) -> str:
    """Texto sin hashtags (para revisar acentos del cuerpo)."""
    return _RE_TAG.sub(" ", str(texto or ""))


def _chat_caido(self, prompt, temperature=0.7):
    """``_chat`` falso que SIEMPRE lanza (IA caida, sin red)."""
    raise RuntimeError("sin IA (test de Actividad)")


def _chat_json(textos):
    """``_chat`` falso que devuelve una lista JSON de textos una vez."""
    def _fake(self, prompt, temperature=0.7):
        return json.dumps(list(textos), ensure_ascii=False)

    return _fake


def _chat_basura(self, prompt, temperature=0.7):
    """``_chat`` falso que devuelve contenido no parseable."""
    return "esto no es JSON ni tiene separadores utiles"


# --------------------------------------------------------------------------- #
# (a) Firmas congeladas (contrato para el motor)
# --------------------------------------------------------------------------- #
def test_firmas(check):
    print("(a) firmas congeladas")
    sig = inspect.signature(generar_textos_actividad_por_cuenta)
    params = list(sig.parameters)
    check(
        "firma: parametros exactos y en orden",
        params == [
            "cuentas_info", "hashtags", "n_por_cuenta",
            "contexto", "narrativa", "menciones",
        ],
        repr(sig),
    )
    check(
        "firma: defaults contexto='' / narrativa='' / menciones=None",
        sig.parameters["contexto"].default == ""
        and sig.parameters["narrativa"].default == ""
        and sig.parameters["menciones"].default is None,
        repr(sig),
    )
    sig_v = inspect.signature(verificar_cobertura_actividad)
    check(
        "firma: verificar_cobertura_actividad(textos, hashtags)",
        list(sig_v.parameters) == ["textos", "hashtags"],
        repr(sig_v),
    )
    check(
        "publicas: ambas funciones existen a nivel de modulo",
        callable(gc.generar_textos_actividad_por_cuenta)
        and callable(gc.verificar_cobertura_actividad),
    )
    check(
        "compat: generar_textos_hashtags_por_cuenta sigue existiendo",
        callable(gc.generar_textos_hashtags_por_cuenta),
    )
    check(
        "limite: _MAX_LARGO_HASHTAG == 100",
        gc._MAX_LARGO_HASHTAG == 100,
        repr(gc._MAX_LARGO_HASHTAG),
    )


# --------------------------------------------------------------------------- #
# (b) TODOS los hashtags en CADA texto cuando caben
# --------------------------------------------------------------------------- #
def test_todos_caben(check):
    print("(b) todos los hashtags en CADA texto (lista que cabe)")
    cuentas = _cuentas(6)
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico"], 4, contexto="el futbol"
        )
    check(
        "1 tag: 6 cuentas x 4 textos exactos",
        len(salida) == 6 and all(len(v) == 4 for v in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    textos = _planos(salida)
    check(
        "1 tag: TODOS los textos llevan #mexico",
        len(textos) == 24 and all("#mexico" in t for t in textos),
    )
    check(
        "1 tag: TODOS validan (en medio, <=100, sin final)",
        all(
            gc.validar_texto_hashtag(t, ["#mexico"], 100)["ok"] for t in textos
        ),
    )
    check(
        "1 tag: ninguno termina en hashtag",
        all(not gc._hashtag_al_final(t) for t in textos),
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_CORTOS[:3], 3, contexto="la comunidad"
        )
    textos = _planos(salida)
    pedidos = {t.lower() for t in _TAGS_CORTOS[:3]}
    check(
        "3 tags que caben: 6 cuentas x 3 textos exactos",
        len(salida) == 6 and all(len(v) == 3 for v in salida.values()),
    )
    check(
        "3 tags que caben: TODOS los textos llevan TODOS los tags",
        all(_tags_de(t) == pedidos for t in textos),
        repr(textos[:2]),
    )
    check(
        "3 tags que caben: cobertura por cuenta True",
        all(
            verificar_cobertura_actividad(v, _TAGS_CORTOS[:3])
            for v in salida.values()
        ),
    )
    check(
        "3 tags que caben: TODOS validan y <=100",
        all(
            gc.validar_texto_hashtag(t, list(pedidos), 100)["ok"]
            and len(t) <= 100
            for t in textos
        ),
    )
    check(
        "criterio: _puede_llevar_grupo_hashtags True con 3 cortos",
        gc._puede_llevar_grupo_hashtags(_TAGS_CORTOS[:3]) is True,
    )
    check(
        "criterio: _puede_llevar_grupo_hashtags False con 6 largos",
        gc._puede_llevar_grupo_hashtags(_TAGS_LARGOS) is False,
    )


# --------------------------------------------------------------------------- #
# (c) Lista larga que NO cabe: cobertura total por cuenta
# --------------------------------------------------------------------------- #
def test_lista_larga(check):
    print("(c) lista larga que no cabe: reparto con cobertura total")
    cuentas = _cuentas(4)
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_LARGOS, 4, contexto="lo nuestro"
        )
    pedidos = {t.lower() for t in _TAGS_LARGOS}
    check(
        "largos: 4 cuentas x 4 textos exactos",
        len(salida) == 4 and all(len(v) == 4 for v in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    ok_cobertura = 0
    ok_ajenos = 0
    ok_largo = 0
    ok_final = 0
    ok_al_menos_uno = 0
    for textos in salida.values():
        union: set = set()
        for t in textos:
            union |= _tags_de(t)
            if len(t) <= 100:
                ok_largo += 1
            if gc._hashtag_al_final(t):
                ok_final += 1
            if _tags_de(t):
                ok_al_menos_uno += 1
        if union == pedidos:
            ok_cobertura += 1
        if union <= pedidos:
            ok_ajenos += 1
    check("largos: cobertura total en las 4 cuentas", ok_cobertura == 4, repr(ok_cobertura))
    check("largos: 0 hashtags ajenos en las 4 cuentas", ok_ajenos == 4, repr(ok_ajenos))
    check("largos: TODOS los textos <=100", ok_largo == 16, repr(ok_largo))
    check("largos: 0 textos terminando en hashtag", ok_final == 0, repr(ok_final))
    check(
        "largos: cada texto lleva al menos un hashtag",
        ok_al_menos_uno == 16,
        repr(ok_al_menos_uno),
    )
    check(
        "largos: verificar_cobertura_actividad True por cuenta",
        all(
            verificar_cobertura_actividad(v, _TAGS_LARGOS)
            for v in salida.values()
        ),
    )
    check(
        "largos: el reparto usa subconjuntos (no todos en todos)",
        any(
            len(_tags_de(t)) < 6 for textos in salida.values() for t in textos
        ),
    )


# --------------------------------------------------------------------------- #
# (d) n_por_cuenta: int y dict; cantidad exacta
# --------------------------------------------------------------------------- #
def test_cantidades(check):
    print("(d) n_por_cuenta int y dict")
    cuentas = _cuentas(3)
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida_int = generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_CORTOS[:2], 3, contexto="la comunidad"
        )
    check(
        "int: 3 textos por cuenta y claves exactas",
        set(salida_int) == {"u0", "u1", "u2"}
        and all(len(v) == 3 for v in salida_int.values()),
        repr({k: len(v) for k, v in salida_int.items()}),
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida_dict = generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_CORTOS[:2], {"u0": 4, "u2": 3},
            contexto="la comunidad",
        )
    check(
        "dict: u0=4, u1=3 (default), u2=3",
        len(salida_dict["u0"]) == 4
        and len(salida_dict["u1"]) == 3
        and len(salida_dict["u2"]) == 3,
        repr({k: len(v) for k, v in salida_dict.items()}),
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida_cero = generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_CORTOS[:2], {"u0": 0, "u1": 1, "u2": 3},
            contexto="la comunidad",
        )
    check(
        "dict: acepta 0 y cantidades distintas",
        len(salida_cero["u0"]) == 0
        and len(salida_cero["u1"]) == 1
        and len(salida_cero["u2"]) == 3,
        repr({k: len(v) for k, v in salida_cero.items()}),
    )
    check(
        "dict: 0 textos => lista vacia y texto unico",
        len(salida_cero["u1"]) == 1 and salida_cero["u1"][0].strip(),
    )
    check(
        "tolerancia: n_por_cuenta invalido no lanza (usa default)",
        len(
            generar_textos_actividad_por_cuenta(
                cuentas[:1], _TAGS_CORTOS[:1], "dos"
            )["u0"]
        ) == 3,
    )


# --------------------------------------------------------------------------- #
# (e) IA simulada, basura y fallback local
# --------------------------------------------------------------------------- #
def test_ia_simulada(check):
    print("(e) IA simulada + fallback local")
    cuentas = _cuentas(1)
    ia_textos = [
        "Hoy celebramos la comunidad #mexico al final",
        "Texto con #otro ajeno y #mexico",
        "Sin hashtag alguno, opinion propia de la calle.",
    ]
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_json(ia_textos)
    ):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico", "#futbol"], 3, contexto="la comunidad"
        )
    textos = salida["u0"]
    check(
        "IA: devuelve EXACTAMENTE 3 textos",
        len(textos) == 3,
        repr(len(textos)),
    )
    check(
        "IA: TODOS llevan los 2 hashtags (aunque la IA omita alguno)",
        all(_tags_de(t) == {"#mexico", "#futbol"} for t in textos),
        repr(textos),
    )
    check(
        "IA: el hashtag ajeno #otro se elimina",
        all("#otro" not in t.lower() for t in textos),
        repr(textos),
    )
    check(
        "IA: hashtag al final reubicado y <=100",
        all(
            len(t) <= 100 and not gc._hashtag_al_final(t)
            for t in textos
        ),
        repr(textos),
    )

    # La IA devuelve MENOS textos de los pedidos => relleno local.
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_json(["Solo un texto de IA #mexico"])
    ):
        salida_pocos = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico", "#futbol"], 4, contexto="la comunidad"
        )
    textos_pocos = salida_pocos["u0"]
    check(
        "IA corta: rellena hasta 4 textos",
        len(textos_pocos) == 4,
        repr(len(textos_pocos)),
    )
    check(
        "IA corta: cobertura total y sin ajenos",
        verificar_cobertura_actividad(textos_pocos, ["#mexico", "#futbol"])
        and all(_tags_de(t) <= {"#mexico", "#futbol"} for t in textos_pocos),
    )

    # La IA devuelve basura no parseable => fallback local total.
    with mock.patch.object(GeneradorContenido, "_chat", _chat_basura):
        salida_basura = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico", "#futbol"], 4, contexto="la comunidad"
        )
    textos_basura = salida_basura["u0"]
    check(
        "IA basura: fallback local completo con 4 textos",
        len(textos_basura) == 4
        and all(t.strip() for t in textos_basura),
        repr(len(textos_basura)),
    )
    check(
        "IA basura: cobertura total y validacion",
        verificar_cobertura_actividad(textos_basura, ["#mexico", "#futbol"])
        and all(
            gc.validar_texto_hashtag(
                t, ["#mexico", "#futbol"], 100
            )["ok"]
            for t in textos_basura
        ),
    )


def test_prompt_actividad(check):
    print("(e2) prompt: la IA recibe la instruccion de usar TODOS")
    capturas: list = []

    def _capturar(self, prompt, temperature=0.7):
        capturas.append(prompt)
        raise RuntimeError("sin IA (test de prompt)")

    cuentas = _cuentas(1)
    with mock.patch.object(GeneradorContenido, "_chat", _capturar):
        generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_CORTOS[:3], 3, contexto="la comunidad"
        )
    unido = "\n".join(capturas)
    check(
        "prompt (todos caben): pide TODOS los hashtags",
        "TODOS los hashtags de la lista" in unido,
        repr(unido[-400:]),
    )
    capturas.clear()
    with mock.patch.object(GeneradorContenido, "_chat", _capturar):
        generar_textos_actividad_por_cuenta(
            cuentas, _TAGS_LARGOS, 3, contexto="lo nuestro"
        )
    unido = "\n".join(capturas)
    check(
        "prompt (no caben): NO pide todos en cada texto",
        "TODOS los hashtags de la lista" not in unido,
    )
    check(
        "prompt: siempre lista los hashtags obligatorios",
        "#seleccionnacionalmx" in unido and "HASHTAGS OBLIGATORIOS" in unido,
    )


# --------------------------------------------------------------------------- #
# (f) Unicidad, registro y perfil
# --------------------------------------------------------------------------- #
def test_unicos_y_estilo(check):
    print("(f) unicidad por cuenta + registro/perfil respetados")
    cuentas = _cuentas(9)
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico", "#futbol"], 4, contexto="la comunidad"
        )
    repetidas = [
        u for u, textos in salida.items() if len(set(textos)) != len(textos)
    ]
    check(
        "unicidad: 0 cuentas con textos repetidos (n=4 > plantillas=3)",
        repetidas == [],
        repr(repetidas),
    )
    todos = _planos(salida)
    check(
        "unicidad global: 36 textos y sin colisiones entre cuentas",
        len(todos) == 36 and len(set(todos)) == 36,
        f"{len(todos)}/{len(set(todos))}",
    )
    check(
        "fallback: TODOS validan y llevan los 2 hashtags",
        all(
            _tags_de(t) == {"#mexico", "#futbol"}
            and gc.validar_texto_hashtag(
                t, ["#mexico", "#futbol"], 100
            )["ok"]
            for t in todos
        ),
    )

    problemas_signos = []
    problemas_acentos = []
    for i, cuenta in enumerate(cuentas):
        textos = salida[cuenta["usuario"]]
        registro = cuenta["registro"]
        if registro in ("activista", "ciudadana"):
            for t in textos:
                if "¿" in t or "¡" in t:
                    problemas_signos.append((cuenta["usuario"], t))
        if registro == "ciudadana":
            for t in textos:
                if any(c in _VOCALES_ACENTO for c in _sin_tags(t)):
                    problemas_acentos.append((cuenta["usuario"], t))
    check(
        "registro: activista/ciudadana SIN '¿' ni '¡'",
        problemas_signos == [],
        repr(problemas_signos[:2]),
    )
    check(
        "registro: ciudadana sin tildes fuera de los hashtags",
        problemas_acentos == [],
        repr(problemas_acentos[:2]),
    )
    check(
        "estilo: _humanizar_por_registro es determinista por semilla",
        gc._humanizar_por_registro(
            "Hablando de esto, la verdad es que hay que hacer algo, gracias.",
            "activista", 7,
        )
        == gc._humanizar_por_registro(
            "Hablando de esto, la verdad es que hay que hacer algo, gracias.",
            "activista", 7,
        ),
    )

    # El perfil de la cuenta llega hasta `_plantillas_hashtags`.
    with mock.patch.object(
        gc, "_plantillas_hashtags", wraps=gc._plantillas_hashtags
    ) as espia:
        with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
            generar_textos_actividad_por_cuenta(
                cuentas, ["#mexico"], 2, contexto="la comunidad"
            )
    llamadas = {tuple(args[:2]) for args, _kw in espia.call_args_list}
    check(
        "perfil: se consultan las plantillas del (registro, perfil) real",
        ("politica", "formal") in llamadas
        and ("activista", "ciudadano") in llamadas
        and ("ciudadana", "popular") in llamadas,
        repr(sorted(llamadas)),
    )


# --------------------------------------------------------------------------- #
# (g) Menciones
# --------------------------------------------------------------------------- #
def test_menciones(check):
    print("(g) @menciones al final solo si caben")
    cuentas = [
        {"usuario": "u0", "registro": "ciudadana", "perfil": "popular"},
    ]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico"], 3, contexto="el barrio",
            menciones=["@amigo", "amigo2", "@@raro", ""],
        )
    textos = salida["u0"]
    check(
        "menciones: normaliza (sin arrobas dobles) y agrega al final",
        all(t.endswith("@amigo @amigo2 @raro") for t in textos),
        repr(textos),
    )
    check(
        "menciones: el texto sigue <=100 y sin hashtag al final",
        all(len(t) <= 100 and not gc._hashtag_al_final(t) for t in textos),
    )
    check(
        "menciones: la cobertura de hashtags no se rompe",
        all("#mexico" in t for t in textos),
    )

    grandes = [f"@usuario{i:02d}" for i in range(10)]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida_grandes = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico"], 2, contexto="el barrio",
            menciones=grandes,
        )
    check(
        "menciones: si NO caben, no se agrega ninguna",
        all(
            not any(m in t for m in grandes)
            for t in salida_grandes["u0"]
        ),
        repr(salida_grandes["u0"]),
    )
    check(
        "menciones: texto intacto (<=100 y con su hashtag)",
        all(
            len(t) <= 100 and "#mexico" in t
            for t in salida_grandes["u0"]
        ),
    )
    normalizadas = gc._normalizar_menciones(["@a", "a", "@@b", "no vale!!", 12])
    check(
        "_normalizar_menciones: dedupe, limpia arrobas y descarta basura",
        normalizadas == ["@a", "@b", "@no"],
        repr(normalizadas),
    )
    check(
        "_normalizar_menciones: entradas raras sin lanzar",
        gc._normalizar_menciones(None) == []
        and gc._normalizar_menciones(123) == []
        and gc._normalizar_menciones("") == []
        and gc._normalizar_menciones(["@usuario_con_mas_de_15_chars"]) == [],
    )


# --------------------------------------------------------------------------- #
# (h) Narrativa solo como trasfondo invisible
# --------------------------------------------------------------------------- #
def test_narrativa_invisible(check):
    print("(h) narrativa como trasfondo invisible")
    cuentas = _cuentas(2)

    def _chat_narrativa(self, prompt, temperature=0.7):
        return json.dumps([
            "El aviario de Durango colapso y Toño Ochoa no hizo nada #mexico",
            "Otra noticia del zoologico de Durango y el aviario #futbol",
            "Toño Ochoa y el aviario de Durango, que colapso #mexico",
        ], ensure_ascii=False)

    with mock.patch.object(GeneradorContenido, "_chat", _chat_narrativa):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, ["#mexico", "#futbol"], 3, contexto="lo nuestro",
            narrativa=_NARRATIVA,
        )
    textos = [t.lower() for t in _planos(salida)]
    fugas = [
        t for t in textos
        if any(
            palabra in t
            for palabra in ("aviario", "durango", "toño", "ochoa", "zoologico")
        )
    ]
    check(
        "narrativa: 0 textos publicados con material reconocible",
        fugas == [],
        repr(fugas[:2]),
    )
    check(
        "narrativa: cobertura total y validacion se mantienen",
        all(
            _tags_de(t) == {"#mexico", "#futbol"}
            and gc.validar_texto_hashtag(
                t, ["#mexico", "#futbol"], 100
            )["ok"]
            for t in _planos(salida)
        ),
    )


# --------------------------------------------------------------------------- #
# (i) verificar_cobertura_actividad
# --------------------------------------------------------------------------- #
def test_verificador(check):
    print("(i) verificar_cobertura_actividad")
    check(
        "ok: usa todos los pedidos y ninguno ajeno",
        verificar_cobertura_actividad(
            ["Hoy #mexico con #futbol en medio."], ["#mexico", "#futbol"]
        )
        is True,
    )
    check(
        "ok: acepta string, dict y entradas raras",
        verificar_cobertura_actividad("texto #mexico", ["#mexico"]) is True
        and verificar_cobertura_actividad(
            {"u0": ["#mexico #futbol"], "u1": []}, ["#futbol", "#mexico"]
        )
        is True
        and verificar_cobertura_actividad(
            [None, 42, {}, "#mexico"], "#mexico"
        )
        is True,
    )
    check(
        "fallo: falta un hashtag pedido",
        verificar_cobertura_actividad(["#mexico"], ["#mexico", "#futbol"])
        is False,
    )
    check(
        "fallo: hay un hashtag ajeno",
        verificar_cobertura_actividad(
            ["#mexico #otro"], ["#mexico"]
        )
        is False,
    )
    check(
        "sin pedidos: True solo si no hay ningun hashtag",
        verificar_cobertura_actividad("", []) is True
        and verificar_cobertura_actividad(None, None) is True
        and verificar_cobertura_actividad("Hola #x", []) is False,
    )
    check(
        "tolerante: entradas invalidas devuelven bool sin lanzar",
        isinstance(verificar_cobertura_actividad(None, ["#a"]), bool)
        and verificar_cobertura_actividad(None, ["#a"]) is False
        and isinstance(verificar_cobertura_actividad(123, None), bool),
    )


# --------------------------------------------------------------------------- #
# (j) Fuzz: 50 corridas con IA caida, 3-4 hashtags y n=4
# --------------------------------------------------------------------------- #
def test_fuzz_50(check):
    print("(j) fuzz de 50 corridas (IA caida, 3-4 tags, n=4)")
    viol_largo = 0
    viol_cobertura = 0
    viol_final = 0
    viol_ajeno = 0
    viol_cantidad = 0
    viol_unicidad = 0
    total_textos = 0
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        for i in range(50):
            rng = random.Random(20260924 + i)
            tags = rng.sample(_POOL_FUZZ, rng.randint(3, 4))
            cuentas = [
                {
                    "usuario": f"f{i}_{k}",
                    "registro": rng.choice(
                        ["politica", "activista", "ciudadana", ""]
                    ),
                    "perfil": rng.choice(
                        ["formal", "ciudadano", "popular", ""]
                    ),
                }
                for k in range(3)
            ]
            salida = generar_textos_actividad_por_cuenta(
                cuentas, tags, 4, contexto="la vida diaria"
            )
            for textos in salida.values():
                total_textos += len(textos)
                if len(textos) != 4:
                    viol_cantidad += 1
                if len(set(textos)) != len(textos):
                    viol_unicidad += 1
                if not verificar_cobertura_actividad(textos, tags):
                    viol_cobertura += 1
                for t in textos:
                    if len(t) > 100:
                        viol_largo += 1
                    if gc._hashtag_al_final(t):
                        viol_final += 1
                    if _tags_de(t) - {x.lower() for x in tags}:
                        viol_ajeno += 1
    check("fuzz: se generaron 600 textos", total_textos == 600, repr(total_textos))
    check("fuzz: 0 textos >100", viol_largo == 0, repr(viol_largo))
    check(
        "fuzz: 0 cuentas sin cobertura total",
        viol_cobertura == 0,
        repr(viol_cobertura),
    )
    check(
        "fuzz: 0 textos terminando en hashtag",
        viol_final == 0,
        repr(viol_final),
    )
    check("fuzz: 0 hashtags ajenos", viol_ajeno == 0, repr(viol_ajeno))
    check(
        "fuzz: 0 cantidades incorrectas",
        viol_cantidad == 0,
        repr(viol_cantidad),
    )
    check(
        "fuzz: 0 cuentas con textos repetidos",
        viol_unicidad == 0,
        repr(viol_unicidad),
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_firmas(check)
    test_todos_caben(check)
    test_lista_larga(check)
    test_cantidades(check)
    test_ia_simulada(check)
    test_prompt_actividad(check)
    test_unicos_y_estilo(check)
    test_menciones(check)
    test_narrativa_invisible(check)
    test_verificador(check)
    test_fuzz_50(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_actividad_textos.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
