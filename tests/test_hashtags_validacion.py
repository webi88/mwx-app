"""Validacion RIGUROSA de hashtags (pedido del dueño, 2026-09-22).

Reglas duras validadas aqui (sin red, sin OpenAI real y sin Chrome):

  * El hashtag pedido debe quedar NATURAL EN MEDIO de la oracion: no al
    inicio, no al final, dentro de la ventana 15%-85% y SIN quedar pegado a
    un articulo/determinante/preposicion ni partiendo la frase
    "articulo + sustantivo" (bug historico: ``"el orgullo #Mexico nacional"``).
  * El texto final NO debe superar 100 caracteres.
  * Solo pueden aparecer hashtags pedidos (cero ajenos).

Cubre:
  (a) firmas publicas estables de ``validar_texto_hashtag`` /
      ``garantizar_texto_hashtag``.
  (b) caso historico "el orgullo #Mexico nacional": detectado y arreglado.
  (c) fuzz determinista de 220 casos sinteticos (textos de 150-400 chars con
      hashtag al inicio/al final, contiguo a articulo, hashtags ajenos
      ``#otro``, varios pedidos, emojis, duplicados y texto de IA larga):
      0 textos > 100 chars, 100% con todos los pedidos, 0 ajenos, 0
      terminando en hashtag, 0 pegados a articulo y ``ok == True``.
  (d) ``generar_textos_hashtags_por_cuenta`` con IA caida (monkeypatch) y con
      IA simulada larga: N textos que pasan la validacion y respetan el
      contrato de cantidad.
  (e) tolerancia a fallos y retrocompatibilidad de las funciones existentes.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_hashtags_validacion.py  (solo este)
"""
from __future__ import annotations

import inspect
import json
import random
import re
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
    garantizar_texto_hashtag,
    validar_texto_hashtag,
)

# --------------------------------------------------------------------------- #
# Material deterministico del fuzz.
# --------------------------------------------------------------------------- #
_BASE = (
    "Hoy salimos a la calle con la frente en alto y la conviccion intacta. "
    "Caminamos juntos por el barrio escuchando a la gente y sumando voces. "
    "Sabemos que el camino es largo pero nadie suelta la causa ni un momento. "
    "Porque el orgullo de lo nuestro se defiende con hechos y no con discursos. "
    "Y el futbol nos recuerda que en equipo todo se puede lograr. "
    "Seguimos adelante con la conviccion de que las cosas buenas llegan. "
)
_TAGS = ("#mexico", "#futbol", "#seleccion", "#comunidad", "#barrio")
_EMOJIS = ("🎉", "🔥", "💪", "🇲🇽", "😉")
_PROHIBIDAS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "del", "al", "de",
}
_RE_TAG = re.compile(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+")


def _generar_caso_fuzz(i: int):
    """Caso adversarial deterministico: ``(texto, hashtags_pedidos)``."""
    rng = random.Random(20260922 + i)
    n = rng.randint(150, 400)
    texto = (_BASE * 3)[:n]
    if " " in texto:
        texto = texto.rsplit(" ", 1)[0]
    pedidos = rng.sample(_TAGS, rng.randint(1, 3))
    modo = rng.choice(
        ["inicio", "final", "articulo", "ajenos", "mixto", "emojis", "duplicado"]
    )
    if modo == "inicio":
        texto = f"{rng.choice(pedidos)} {texto}"
    elif modo == "final":
        texto = f"{texto} {rng.choice(pedidos)}"
        if rng.random() < 0.5:
            texto += "."
    elif modo == "articulo":
        corte = rng.randint(30, max(31, len(texto) - 30))
        texto = f"{texto[:corte]} el orgullo {pedidos[0]} nacional {texto[corte:]}"
    elif modo == "ajenos":
        texto = f"{texto} #otro #inventado {rng.choice(pedidos)}"
    elif modo == "mixto":
        corte = rng.randint(20, max(21, len(texto) - 20))
        texto = (
            f"{pedidos[0]} {texto[:corte]} el orgullo {pedidos[-1]} nacional "
            f"{texto[corte:]} #otro"
        )
    elif modo == "emojis":
        emoji = rng.choice(_EMOJIS)
        corte = rng.randint(20, max(21, len(texto) - 20))
        texto = (
            f"{texto[:corte]} {emoji} {pedidos[0]} {emoji} {texto[corte:]} "
            f"{rng.choice(_EMOJIS)}"
        )
    else:  # duplicado
        texto = f"{texto} {pedidos[0]} y de nuevo {pedidos[0]}"
    return texto, pedidos


def _pegado_a_articulo(texto: str) -> list:
    """Tags cuyo token INMEDIATAMENTE anterior es articulo/determinante."""
    pegados = []
    for m in _RE_TAG.finditer(str(texto or "")):
        antes = str(texto or "")[: m.start()].rstrip()
        if not antes or antes[-1] in ".,;:!?)»”’\"'":
            continue
        palabra = re.split(r"\s+", antes)[-1]
        palabra = palabra.strip("«»\"'()[]").lower()
        if palabra in _PROHIBIDAS:
            pegados.append(m.group(0))
    return pegados


def _chat_rotativo(textos_lista):
    """``_chat`` falso que entrega UN texto distinto por lote (ciclico)."""
    estado = {"i": 0}

    def _fake(self, prompt, temperature=0.7):
        texto = textos_lista[estado["i"] % len(textos_lista)]
        estado["i"] += 1
        return json.dumps([texto], ensure_ascii=False)

    return _fake


def _cuentas_prueba(n: int = 9) -> list:
    combos = [
        ("politica", "formal"),
        ("activista", "ciudadano"),
        ("ciudadana", "popular"),
        ("politica", "ciudadano"),
        ("activista", "formal"),
        ("ciudadana", "ciudadano"),
        ("", ""),
        ("politica", "popular"),
        ("activista", "popular"),
    ]
    cuentas = []
    for i in range(n):
        registro, perfil = combos[i % len(combos)]
        cuentas.append({
            "usuario": f"cuenta_{i}",
            "registro": registro,
            "perfil": perfil,
            "personalidad": "",
            "seccion": "",
            "nombre": f"Nombre {i}",
        })
    return cuentas


# --------------------------------------------------------------------------- #
# (a) Firmas publicas estables
# --------------------------------------------------------------------------- #
def test_firmas(check):
    print("(a) firmas publicas de validar/garantizar")
    sig_val = inspect.signature(validar_texto_hashtag)
    sig_gar = inspect.signature(garantizar_texto_hashtag)
    check(
        "validar: firma (texto, hashtags, limite=100)",
        list(sig_val.parameters) == ["texto", "hashtags", "limite"]
        and sig_val.parameters["limite"].default == 100,
        repr(sig_val),
    )
    check(
        "garantizar: firma (texto, hashtags, limite=100, semilla=0)",
        list(sig_gar.parameters) == ["texto", "hashtags", "limite", "semilla"]
        and sig_gar.parameters["limite"].default == 100
        and sig_gar.parameters["semilla"].default == 0,
        repr(sig_gar),
    )
    check(
        "ambas son publicas (sin guion bajo) y tolerantes",
        callable(validar_texto_hashtag)
        and callable(garantizar_texto_hashtag),
    )


# --------------------------------------------------------------------------- #
# (b) Caso historico: "el orgullo #Mexico nacional"
# --------------------------------------------------------------------------- #
def test_caso_historico(check):
    print("(b) caso historico 'el orgullo #Mexico nacional'")
    original = "el orgullo #Mexico nacional"
    v = validar_texto_hashtag(original, ["#Mexico"], 100)
    check("historico: el texto original NO valida", v["ok"] is False, repr(v["problemas"]))
    check(
        "historico: se detecta que parte la frase entre articulo y sustantivo",
        any(
            "parte la frase" in p.lower() or "pegado a un articulo" in p.lower()
            for p in v["problemas"]
        ),
        repr(v["problemas"]),
    )
    check(
        "historico: hashtag presente y sin faltantes/ajenos",
        v["hashtags_presentes"] == ["#Mexico"]
        and not v["hashtags_faltantes"]
        and not v["hashtags_ajenos"],
        repr(v),
    )

    out = garantizar_texto_hashtag(original, ["#Mexico"], 100, semilla=3)
    v_out = validar_texto_hashtag(out, ["#Mexico"], 100)
    check("historico: garantizar devuelve algo distinto", out != original, repr(out))
    check("historico: el arreglo valida", v_out["ok"] is True, repr(v_out["problemas"]))
    check(
        "historico: conserva el hashtag pedido",
        "#mexico" in out.lower(),
        repr(out),
    )
    check(
        "historico: <= 100, no termina en hashtag y no queda pegado a articulo",
        len(out) <= 100
        and not gc._hashtag_al_final(out)
        and not _pegado_a_articulo(out),
        repr(out),
    )
    check(
        "historico: el texto arreglado ya no contiene 'orgullo #Mexico nacional'",
        "orgullo #mexico nacional" not in out.lower(),
        repr(out),
    )

    # Variante con el articulo INMEDIATAMENTE antes del hashtag.
    original2 = "el #Mexico orgullo nacional"
    v2 = validar_texto_hashtag(original2, ["#Mexico"], 100)
    out2 = garantizar_texto_hashtag(original2, ["#Mexico"], 100, semilla=4)
    check(
        "historico: 'el #Mexico orgullo' se detecta y se arregla",
        v2["ok"] is False
        and validar_texto_hashtag(out2, ["#Mexico"], 100)["ok"] is True
        and not _pegado_a_articulo(out2),
        f"v2={v2['problemas']} out2={out2!r}",
    )


# --------------------------------------------------------------------------- #
# (c) Fuzz determinista: 220 casos sinteticos
# --------------------------------------------------------------------------- #
def test_fuzz_220(check):
    print("(c) fuzz determinista de 220 casos adversarios")
    casos = 0
    invalidos_entrada = 0
    largos = 0
    sin_todos = 0
    con_ajenos = 0
    en_final = 0
    pegados = 0
    no_ok = 0
    no_en_medio = 0
    for i in range(220):
        texto, pedidos = _generar_caso_fuzz(i)
        casos += 1
        if not validar_texto_hashtag(texto, pedidos, 100)["ok"]:
            invalidos_entrada += 1
        salida = garantizar_texto_hashtag(texto, pedidos, 100, semilla=i)
        v = validar_texto_hashtag(salida, pedidos, 100)
        if len(salida) > 100:
            largos += 1
        if v["hashtags_faltantes"]:
            sin_todos += 1
        if v["hashtags_ajenos"]:
            con_ajenos += 1
        if salida and gc._hashtag_al_final(salida):
            en_final += 1
        if _pegado_a_articulo(salida):
            pegados += 1
        if not v["ok"]:
            no_ok += 1
        if not v["en_medio"]:
            no_en_medio += 1
    check("fuzz: se generaron 220 casos", casos == 220, repr(casos))
    check(
        "fuzz: los casos de entrada son adversarios (invalidos)",
        invalidos_entrada >= 200,
        f"invalidos_entrada={invalidos_entrada}",
    )
    check("fuzz: 0 textos > 100 chars", largos == 0, f"largos={largos}")
    check(
        "fuzz: 100% con TODOS los hashtags pedidos",
        sin_todos == 0,
        f"sin_todos={sin_todos}",
    )
    check("fuzz: 0 hashtags ajenos", con_ajenos == 0, f"con_ajenos={con_ajenos}")
    check(
        "fuzz: 0 terminando en hashtag",
        en_final == 0,
        f"en_final={en_final}",
    )
    check(
        "fuzz: 0 con hashtag pegado a articulo",
        pegados == 0,
        f"pegados={pegados}",
    )
    check(
        "fuzz: validar_texto_hashtag ok en los 220",
        no_ok == 0,
        f"no_ok={no_ok}",
    )
    check(
        "fuzz: en_medio True en los 220",
        no_en_medio == 0,
        f"no_en_medio={no_en_medio}",
    )
    determinista = True
    for i in range(0, 220, 40):
        texto, pedidos = _generar_caso_fuzz(i)
        primera = garantizar_texto_hashtag(texto, pedidos, 100, semilla=i)
        segunda = garantizar_texto_hashtag(texto, pedidos, 100, semilla=i)
        if primera != segunda:
            determinista = False
            break
    check(
        "fuzz: garantizar es determinista (misma semilla, mismo texto)",
        determinista,
    )


# --------------------------------------------------------------------------- #
# (d) Integracion: generar_textos_hashtags_por_cuenta
# --------------------------------------------------------------------------- #
def test_integracion_ia_caida(check):
    print("(d1) generar_textos_hashtags_por_cuenta con IA caida")
    cuentas = _cuentas_prueba(9)
    with mock.patch.object(
        GeneradorContenido, "_chat", side_effect=RuntimeError("sin IA (test)")
    ):
        salida = gc.generar_textos_hashtags_por_cuenta(
            cuentas,
            hashtags="#mexico #futbol",
            contexto="el futbol y la comunidad",
            n_por_cuenta=1,
        )
    check(
        "integracion caida: devuelve las 9 cuentas con 1 texto",
        len(salida) == 9 and all(len(v) == 1 for v in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    textos = [t for v in salida.values() for t in v]
    pedidos = ("#mexico", "#futbol")
    mal_validados = 0
    ajenos = 0
    largos = 0
    for t in textos:
        presentes = [p for p in pedidos if p.lower() in t.lower()]
        v = validar_texto_hashtag(t, presentes, 100)
        if not v["ok"]:
            mal_validados += 1
        if v["hashtags_ajenos"]:
            ajenos += 1
        if len(t) > 100:
            largos += 1
    check(
        "integracion caida: TODOS validan (hashtag natural en medio)",
        mal_validados == 0,
        f"malos={mal_validados}",
    )
    check("integracion caida: 0 ajenos", ajenos == 0, f"ajenos={ajenos}")
    check("integracion caida: TODOS <= 100", largos == 0, f"largos={largos}")
    check(
        "integracion caida: TODOS llevan al menos un hashtag pedido",
        all(any(p.lower() in t.lower() for p in pedidos) for t in textos),
        repr(textos[:3]),
    )
    check(
        "integracion caida: ningun texto termina en hashtag",
        all(not gc._hashtag_al_final(t) for t in textos),
        repr(textos[:3]),
    )

    # Con UN solo hashtag el contrato es determinista: TODOS deben llevarlo.
    with mock.patch.object(
        GeneradorContenido, "_chat", side_effect=RuntimeError("sin IA (test)")
    ):
        salida_uno = gc.generar_textos_hashtags_por_cuenta(
            cuentas,
            hashtags="#mexico",
            contexto="la comunidad",
            n_por_cuenta=1,
        )
    textos_uno = [t for v in salida_uno.values() for t in v]
    check(
        "integracion caida (1 hashtag): todos llevan #mexico y validan",
        len(textos_uno) == 9
        and all("#mexico" in t for t in textos_uno)
        and all(validar_texto_hashtag(t, ["#mexico"], 100)["ok"] for t in textos_uno),
        repr(textos_uno[:3]),
    )


def test_integracion_ia_larga(check):
    print("(d2) generar_textos_hashtags_por_cuenta con IA larga y mal ubicada")
    cuentas = _cuentas_prueba(6)
    largos = [
        "#mexico " + ("Seguimos trabajando por la comunidad. " * 8),
        ("Seguimos trabajando por la comunidad. " * 7) + " #futbol",
        "Hoy salimos el orgullo #Mexico nacional " + ("y la comunidad sigue. " * 8),
        ("Con la gente, el futbol y la comunidad " + ("x" * 90)) + " #mexico #futbol",
        "#otro texto con hashtags ajenos y largos " + ("palabra " * 30) + "#mexico",
    ]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_rotativo(largos)):
        salida = gc.generar_textos_hashtags_por_cuenta(
            cuentas,
            hashtags="#mexico #futbol",
            contexto="el futbol",
            n_por_cuenta=1,
        )
    check(
        "integracion IA larga: devuelve las 6 cuentas con 1 texto",
        len(salida) == 6 and all(len(v) == 1 for v in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    textos = [t for v in salida.values() for t in v]
    malos = 0
    ajenos = 0
    for t in textos:
        presentes = [p for p in ("#mexico", "#futbol") if p.lower() in t.lower()]
        v = validar_texto_hashtag(t, presentes, 100)
        if not v["ok"] or not presentes or len(t) > 100:
            malos += 1
        if v["hashtags_ajenos"]:
            ajenos += 1
    check(
        "integracion IA larga: TODOS validan y <= 100",
        malos == 0,
        f"malos={malos}",
    )
    check(
        "integracion IA larga: 0 hashtags ajenos (#otro eliminado)",
        ajenos == 0 and all("#otro" not in t.lower() for t in textos),
        repr(textos),
    )
    check(
        "integracion IA larga: ningun texto termina en hashtag",
        all(not gc._hashtag_al_final(t) for t in textos),
        repr([t[-40:] for t in textos]),
    )
    check(
        "integracion IA larga: el caso 'orgullo #Mexico nacional' no sobrevive",
        all("orgullo #mexico nacional" not in t.lower() for t in textos),
        repr(textos),
    )


# --------------------------------------------------------------------------- #
# (e) Tolerancia y retrocompatibilidad
# --------------------------------------------------------------------------- #
def test_tolerancia_retrocompat(check):
    print("(e) tolerancia a fallos y retrocompatibilidad")
    check(
        "tolerancia: garantizar(None, None) no lanza y devuelve str",
        isinstance(garantizar_texto_hashtag(None, None), str)
        and garantizar_texto_hashtag(None, None) == "",
    )
    check(
        "tolerancia: garantizar con entradas raras no lanza",
        isinstance(garantizar_texto_hashtag(12345, 678, 100), str)
        and isinstance(garantizar_texto_hashtag("texto", ["###", ""]), str)
        and isinstance(garantizar_texto_hashtag("texto", "#mexico", "cien"), str),
    )
    check(
        "tolerancia: validar(None, None) devuelve dict coherente",
        validar_texto_hashtag(None, None)["ok"] is True
        and validar_texto_hashtag(None, None)["largo"] == 0,
    )
    check(
        "tolerancia: validar texto vacio con pedido => ok False",
        validar_texto_hashtag("", ["#mexico"])["ok"] is False,
    )
    check(
        "tolerancia: hashtags como string ('#mexico, #futbol') se normalizan",
        validar_texto_hashtag("Hola. #mexico sigue", "#mexico, #futbol")[
            "hashtags_faltantes"
        ]
        == ["#futbol"],
    )
    check(
        "tolerancia: sin hashtags pedidos solo limita largo (no inventa)",
        garantizar_texto_hashtag("texto corto sin tags", [], 100)
        == "texto corto sin tags"
        and len(garantizar_texto_hashtag("palabra " * 60, [], 100)) <= 100,
    )
    check(
        "retrocompat: _MAX_LARGO_HASHTAG == 100",
        gc._MAX_LARGO_HASHTAG == 100,
        repr(gc._MAX_LARGO_HASHTAG),
    )
    salida_vieja = gc.solo_hashtags_pedidos("Hola mundo #otro", ["#mexico"], 1)
    check(
        "retrocompat: solo_hashtags_pedidos sigue funcionando",
        isinstance(salida_vieja, str)
        and "#mexico" in salida_vieja.lower()
        and "#otro" not in salida_vieja.lower(),
        repr(salida_vieja),
    )
    recorte = gc._recortar_limite_hashtag("x " * 80 + "#mexico", ["#mexico"], 100)
    check(
        "retrocompat: _recortar_limite_hashtag sigue acotando a 100",
        isinstance(recorte, str) and len(recorte) <= 100,
        repr(len(recorte)),
    )
    check(
        "retrocompat: _acotar_limite conserva contrato",
        gc._acotar_limite("texto corto #mexico", 100) == "texto corto #mexico",
    )


def test_en_medio(check):
    print("(f) semantica de en_medio")
    v_ok = validar_texto_hashtag(
        "Hoy la comunidad celebra #mexico con alegria y respeto.", ["#mexico"], 100
    )
    check(
        "en_medio: hashtag centrado => True y ok",
        v_ok["en_medio"] is True and v_ok["ok"] is True,
        repr(v_ok),
    )
    v_inicio = validar_texto_hashtag(
        "#mexico hoy la comunidad celebra con alegria y respeto.", ["#mexico"], 100
    )
    check(
        "en_medio: hashtag al inicio => False y ok False",
        v_inicio["en_medio"] is False and v_inicio["ok"] is False,
        repr(v_inicio["problemas"]),
    )
    v_final = validar_texto_hashtag(
        "Hoy la comunidad celebra con alegria y respeto #mexico", ["#mexico"], 100
    )
    check(
        "en_medio: hashtag al final => False y ok False",
        v_final["en_medio"] is False and v_final["ok"] is False,
        repr(v_final["problemas"]),
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_firmas(check)
    test_caso_historico(check)
    test_fuzz_220(check)
    test_integracion_ia_caida(check)
    test_integracion_ia_larga(check)
    test_tolerancia_retrocompat(check)
    test_en_medio(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_hashtags_validacion.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
