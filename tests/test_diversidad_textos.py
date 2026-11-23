"""Tests de DIVERSIDAD REAL de textos (posts y retweets con cita).

Pedido del dueño: en las campanas, cada publicacion (y sobre todo cada RETWEET
CON CITA) debe tener SIEMPRE un texto distinto y MUY distinto entre cuentas.
Hoy la IA devuelve variaciones casi iguales y el fallback de sinonimos repite
el mismo texto base.

Cubre, sin red y sin Chrome:

  (a) Utilidades publicas de diversidad: normalizar_para_comparar,
      similitud_textos, son_textos_similares,
      texto_es_suficientemente_distinto y elegir_textos_diversos.
  (b) Prompts: ANGULOS_VARIACION (>=12), bloque_diversidad y su inyeccion en
      get_prompt_hashtags / get_prompt_comentario / lotes de mantenimiento /
      generar_variaciones_masivas (con "NO REPITAS" cuando hay ``evitar``).
  (c) Caso REAL del dueño (disculpa a un profesor, ~350 chars): con IA caida,
      12 textos, ninguno igual al base, mayoria de pares bajo el umbral de cita
      y <=100 (y tambien con limite=280), con y sin hashtags, registro/perfil
      variados.
  (d) IA que devuelve textos identicos / casi iguales / menos cantidad: la
      salida no tiene duplicados y se rellena con textos distintos.
  (e) ``evitar`` impide reutilizar textos entre dos llamadas.
  (f) POSTS: 8 cuentas x 3 textos con IA caida, exactamente 3 por cuenta, sin
      duplicados exactos, mayoria de pares bajo DIVERSIDAD_UMBRAL_POST,
      hashtags en medio, <=100 y sin copiar el contexto.
  (g) Firmas: ``limite`` sigue siendo el ultimo parametro con default 100 y
      ``evitar``/``angulos`` van inmediatamente antes.
  (h) Casos limite: base vacia/corta, hashtags=[], limite 20/280, cantidad 0/1,
      emojis/acentos.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_diversidad_textos.py   (solo este)
"""
from __future__ import annotations

import inspect
import itertools
import json
import re
import sys
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia.generador_contenido as gc  # noqa: E402
import ia.prompts as prompts  # noqa: E402
from activaciones import variaciones as var  # noqa: E402
from ia.generador_contenido import (  # noqa: E402
    DIVERSIDAD_UMBRAL_CITA,
    DIVERSIDAD_UMBRAL_POST,
    GeneradorContenido,
    elegir_textos_diversos,
    generar_textos_actividad_por_cuenta,
    generar_textos_hashtags_por_cuenta,
    generar_variaciones_locales_diversas,
    normalizar_para_comparar,
    similitud_textos,
    son_textos_similares,
    texto_es_suficientemente_distinto,
    verificar_cobertura_actividad,
)
from ia.prompts import REGLA_MAX_100  # noqa: E402

# Caso REAL reportado por el dueño (~350 chars, con ¿? y acentos).
BASE_DUENO = (
    "Profesor, le ofrezco una disculpa. Me retuvieron en una junta de trabajo "
    "en la oficina en Santa Fe y me va a ser imposible llegar a las 6 pm al "
    "auditorio de ESCOM. ¿Cree que habría alguna posibilidad de tener alguna "
    "facilidad por el día de hoy? Le adjunto una foto de mi oficina para "
    "justificar la situación. Agradezco mucho su comprensión."
)

_RE_TAG = re.compile(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+")
_PALABRAS_FUNCION = re.compile(
    r"\b(?:de|del|la|el|los|las|un|una|y|o|que|en|con|por|para|al|se|su|"
    r"es|son|como|más|mas|muy)\b",
    re.IGNORECASE,
)


def _chat_caido(self, prompt, temperature=0.7):
    """``_chat`` falso que SIEMPRE lanza (IA caida, sin red)."""
    raise RuntimeError("sin IA (test de diversidad)")


def _chat_identicos(cantidad: int = 10):
    """Devuelve SIEMPRE la misma cantidad de textos IDENTICOS."""
    def _fake(self, prompt, temperature=0.7):
        return json.dumps(["Mismo texto exacto para todos"] * cantidad,
                          ensure_ascii=False)
    return _fake


def _chat_json(textos, capturas=None):
    """Devuelve un JSON con ``textos`` en cada llamada (sin red)."""
    def _fake(self, prompt, temperature=0.7):
        if capturas is not None:
            capturas.append(prompt)
        return json.dumps(list(textos), ensure_ascii=False)
    return _fake


def _chat_vacio(self, prompt, temperature=0.7):
    """Respuesta vacia (la IA no devolvio nada)."""
    return ""


def _pares(textos):
    """Lista de similitudes de todos los pares (sin importar el orden)."""
    return [
        similitud_textos(a, b)
        for a, b in itertools.combinations(list(textos or []), 2)
    ]


def _pct_bajo(textos, umbral) -> float:
    """Fraccion de pares con similitud ESTRICTAMENTE menor al umbral."""
    pares = _pares(textos)
    if not pares:
        return 1.0
    return sum(1 for s in pares if s < umbral) / len(pares)


def _tags(texto) -> list:
    """Hashtags del texto en minusculas, unicos y en orden de aparicion."""
    vistos: list = []
    for m in _RE_TAG.finditer(str(texto or "")):
        clave = m.group(0).lower()
        if clave not in vistos:
            vistos.append(clave)
    return vistos


def _no_termina_en_hashtag(texto) -> bool:
    limpio = re.sub(r"[\s.,;:!?)…\"']+$", "", str(texto or "").rstrip())
    return not re.search(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+$", limpio)


def _cuentas(n: int = 8) -> list:
    registros = ("politica", "activista", "ciudadana")
    perfiles = ("formal", "ciudadano", "popular")
    return [
        {
            "usuario": f"u{i}",
            "registro": registros[i % 3],
            "perfil": perfiles[i % 3],
        }
        for i in range(n)
    ]


def _textos_posts(salida) -> list:
    return [t for fila in (salida or {}).values() for t in (fila or [])]


# --------------------------------------------------------------------------- #
# (a) Utilidades publicas de diversidad
# --------------------------------------------------------------------------- #
def test_utilidades(check):
    print("(a) utilidades de diversidad")
    normal = normalizar_para_comparar(
        "¡Hola, MÉXICO! #Futbol https://x.com/a @usuario"
    )
    check(
        "normalizar: minusculas, sin acentos, hashtags, URL ni @",
        normal == "hola mexico",
        repr(normal),
    )
    check(
        "normalizar: colapsa espacios y quita puntuacion",
        normalizar_para_comparar("  Hola,,   mundo!!  ") == "hola mundo",
    )
    check(
        "normalizar: entradas raras => ''",
        normalizar_para_comparar(None) == ""
        and normalizar_para_comparar("") == "",
    )

    check(
        "similitud: textos identicos = 1.0",
        similitud_textos("Hola mundo", "Hola mundo!") == 1.0,
    )
    check(
        "similitud: textos totalmente distintos < 0.2",
        similitud_textos("El gato duerme en el sofa",
                         "El avion despega muy temprano") < 0.2,
    )
    check(
        "similitud: textos apenas distintos > 0.9",
        similitud_textos(
            "Profesor le pido una disculpa por la demora",
            "Profesor le pido una disculpa por la demora.",
        ) > 0.9,
    )
    check(
        "similitud: cortos ('hola' vs 'adios') es baja",
        similitud_textos("hola", "adios") < 0.6,
    )
    check(
        "similitud: ambos vacios = 1.0 y uno vacio = 0.0",
        similitud_textos(None, "") == 1.0
        and similitud_textos("", "algo") == 0.0,
    )
    check(
        "similitud: nunca lanza con entradas raras",
        isinstance(similitud_textos(123, None), float),
    )

    check(
        "son_textos_similares: umbral default detecta casi iguales",
        son_textos_similares(
            "Texto completamente diferente uno",
            "Texto completamente diferente uno!",
        ),
    )
    check(
        "son_textos_similares: umbral 1.0 solo iguala identicos",
        son_textos_similares("hola mundo", "hola mundo", 1.0)
        and not son_textos_similares("hola mundo", "hola mundo distinto", 1.0)
    )

    check(
        "distinto: sin otros => True; igual => False",
        texto_es_suficientemente_distinto("texto uno", None)
        and not texto_es_suficientemente_distinto("texto uno", ["texto uno"]),
    )
    check(
        "distinto: similar => False; diferente => True",
        not texto_es_suficientemente_distinto(
            "Profesor le pido una disculpa por la demora",
            ["Profesor le pido una disculpa por la demora."],
        )
        and texto_es_suficientemente_distinto(
            "El gato duerme en el sofa", ["El avion despega temprano"]
        ),
    )
    check(
        "elegir_textos_diversos: filtra iguales y similares",
        elegir_textos_diversos(
            ["uno dos tres", "uno dos tres", "cuatro cinco seis"],
            2,
        )[:2] == ["uno dos tres", "cuatro cinco seis"],
    )
    check(
        "elegir_textos_diversos: cantidad 0 / None => []",
        elegir_textos_diversos(["a"], 0) == []
        and elegir_textos_diversos(None, 3) == [],
    )
    check(
        "umbrales por env: POST y CITA en (0, 1]",
        0 < DIVERSIDAD_UMBRAL_POST <= 1
        and 0 < DIVERSIDAD_UMBRAL_CITA <= 1,
        f"post={DIVERSIDAD_UMBRAL_POST} cita={DIVERSIDAD_UMBRAL_CITA}",
    )


# --------------------------------------------------------------------------- #
# (b) Prompts: angulos y bloque de diversidad
# --------------------------------------------------------------------------- #
def test_prompts(check):
    print("(b) prompts con angulos y prohibicion de repetir")
    check(
        "ANGULOS_VARIACION: >= 12 angulos",
        len(prompts.ANGULOS_VARIACION) >= 12,
        repr(len(prompts.ANGULOS_VARIACION)),
    )
    check(
        "ANGULOS_VARIACION: todos distintos",
        len(set(prompts.ANGULOS_VARIACION)) == len(prompts.ANGULOS_VARIACION),
    )

    bloque = prompts.bloque_diversidad(4)
    check(
        "bloque: pide EXACTAMENTE la cantidad",
        "EXACTAMENTE 4" in bloque,
    )
    check(
        "bloque: asigna un angulo numerado por texto",
        "Texto 1 | ángulo obligatorio" in bloque
        and "Texto 4 | ángulo obligatorio" in bloque,
    )
    check(
        "bloque: prohibe frases de 4+ palabras y parafrasis",
        "PROHIBIDO repetir frases de 4+ palabras" in bloque
        and "PROHIBIDO parafrasear" in bloque,
    )
    check(
        "bloque: exige apertura/desarrollo/cierre distintos",
        "APERTURA, DESARROLLO, RITMO y CIERRE DISTINTOS" in bloque,
    )
    check(
        "bloque: prohibe copiar base/contexto",
        "PROHIBIDO copiar el texto base" in bloque,
    )

    bloque_evitar = prompts.bloque_diversidad(
        3, evitar=["Texto ya usado que no debe repetirse"], tipo="post"
    )
    check(
        "bloque evitar: incluye el texto vetado",
        "Texto ya usado que no debe repetirse" in bloque_evitar,
    )
    check(
        "bloque evitar: marca la lista de ya usados",
        "TEXTOS YA USADOS" in bloque_evitar,
    )
    check(
        "bloque post: recuerda hashtag EN MEDIO",
        "EN MEDIO" in bloque_evitar,
    )
    check(
        "bloque comentario: recuerda que no lleva hashtags",
        "no llevan hashtags" in prompts.bloque_diversidad(2, tipo="comentario"),
    )

    p_hashtags = prompts.get_prompt_hashtags(
        hashtags="#mexico #futbol", contexto="el futbol", cantidad=3
    )
    check(
        "get_prompt_hashtags: incluye bloque y angulos",
        "REGLA DE DIVERSIDAD OBLIGATORIA" in p_hashtags
        and "Texto 1 | ángulo obligatorio" in p_hashtags,
    )
    check(
        "get_prompt_hashtags: 100 conserva REGLA_MAX_100",
        REGLA_MAX_100 in p_hashtags,
    )
    p_280 = prompts.get_prompt_hashtags(
        hashtags="#mexico", contexto="el futbol", cantidad=2, limite=280
    )
    check(
        "get_prompt_hashtags: 280 no usa la regla de 100",
        "280 caracteres" in p_280 and REGLA_MAX_100 not in p_280,
    )

    p_com = prompts.get_prompt_comentario("4T", cantidad=2, registro="politica")
    check(
        "get_prompt_comentario: incluye diversidad",
        "REGLA DE DIVERSIDAD OBLIGATORIA" in p_com,
    )
    check(
        "get_prompt_comentario: sigue sin REGLA_MAX_100",
        REGLA_MAX_100 not in p_com,
    )

    lote_post = gc._prompt_lote_mantenimiento(
        [(0, 0, "dia")],
        [{"usuario": "u", "registro": "politica", "perfil": "formal"}],
    )
    lote_com = gc._prompt_lote_mantenimiento(
        [(0, 0, "dia")],
        [{
            "usuario": "u",
            "registro": "politica",
            "perfil": "formal",
            "tipo_accion": "comentario",
        }],
    )
    check(
        "lote mantenimiento: incluye bloque de diversidad",
        "REGLA DE DIVERSIDAD OBLIGATORIA" in lote_post
        and "REGLA DE DIVERSIDAD OBLIGATORIA" in lote_com,
    )
    check(
        "lote comentario: no recibe la regla de 100",
        REGLA_MAX_100 not in lote_com,
    )

    capturas: list = []
    with mock.patch.object(
        GeneradorContenido,
        "_chat",
        _chat_json(["Texto uno distinto", "Texto dos distinto"], capturas),
    ):
        GeneradorContenido().generar_variaciones_masivas(
            "Cita base", 2, evitar=["NO REPITAS ESTE TEXTO VIEJO"]
        )
    unido = "\n".join(capturas)
    check(
        "prompt de cita: contiene angulos y bloque",
        "REGLA DE DIVERSIDAD OBLIGATORIA" in unido
        and "Texto 1 | ángulo obligatorio" in unido,
    )
    check(
        "prompt de cita: incluye 'NO REPITAS' cuando hay evitar",
        "NO REPITAS ESTE TEXTO VIEJO" in unido and "TEXTOS YA USADOS" in unido,
    )


# --------------------------------------------------------------------------- #
# (c) Caso REAL del dueño (IA caida)
# --------------------------------------------------------------------------- #
def test_caso_dueno(check):
    print("(c) caso real del dueño (disculpa al profesor)")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        t100 = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 12, registro="politica", limite=100
        )
    check("dueño 100: devuelve 12 textos", len(t100) == 12, repr(len(t100)))
    check(
        "dueño 100: ninguno es el base literal",
        all(t.strip() != BASE_DUENO for t in t100)
        and all(normalizar_para_comparar(t) != normalizar_para_comparar(BASE_DUENO)
                for t in t100),
    )
    check(
        "dueño 100: todos <= 100",
        all(len(t) <= 100 for t in t100),
        repr([len(t) for t in t100]),
    )
    check(
        "dueño 100: ninguno vacio y sin duplicados exactos",
        all(t.strip() for t in t100) and len(set(t100)) == 12,
    )
    pct = _pct_bajo(t100, DIVERSIDAD_UMBRAL_CITA)
    check(
        "dueño 100: >=90% de pares bajo el umbral de cita",
        pct >= 0.90,
        f"{pct:.3f}",
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        t280 = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 12, registro="", limite=280
        )
    check("dueño 280: devuelve 12 textos", len(t280) == 12)
    check(
        "dueño 280: todos <= 280 y ninguno es el base",
        all(len(t) <= 280 for t in t280)
        and all(t.strip() != BASE_DUENO for t in t280),
    )
    check(
        "dueño 280: >=90% de pares bajo el umbral de cita",
        _pct_bajo(t280, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
        f"{_pct_bajo(t280, DIVERSIDAD_UMBRAL_CITA):.3f}",
    )
    check(
        "dueño 280: al menos un texto supera 100 (material completo)",
        any(len(t) > 100 for t in t280),
        repr([len(t) for t in t280]),
    )

    # Generador LOCAL DIRECTO con hashtags pedidos.
    locales_tags = generar_variaciones_locales_diversas(
        BASE_DUENO,
        12,
        registro="activista",
        perfil="ciudadano",
        hashtags=["#mexico", "#futbol"],
        limite=100,
        semilla=2026,
    )
    check(
        "local con hashtags: 12 textos <=100",
        len(locales_tags) == 12 and all(len(t) <= 100 for t in locales_tags),
    )
    check(
        "local con hashtags: cada texto lleva al menos uno pedido",
        all(_tags(t) for t in locales_tags),
    )
    check(
        "local con hashtags: ninguno ajeno ni al final",
        all(set(_tags(t)) <= {"#mexico", "#futbol"} for t in locales_tags)
        and all(_no_termina_en_hashtag(t) for t in locales_tags),
    )
    check(
        "local con hashtags: >=90% de pares bajo el umbral de cita",
        _pct_bajo(locales_tags, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
        f"{_pct_bajo(locales_tags, DIVERSIDAD_UMBRAL_CITA):.3f}",
    )

    for registro in ("ciudadana", "activista"):
        locales = generar_variaciones_locales_diversas(
            BASE_DUENO, 6, registro=registro, perfil="popular",
            limite=100, semilla=77,
        )
        check(
            f"local registro {registro}: sin '¿' ni '¡'",
            all("¿" not in t and "¡" not in t for t in locales),
        )
        check(
            f"local registro {registro}: 6 textos no vacios",
            len(locales) == 6 and all(t.strip() for t in locales),
        )

    sin_tags = generar_variaciones_locales_diversas(
        BASE_DUENO, 5, hashtags=[], limite=100, semilla=9
    )
    check(
        "local hashtags=[]: no agrega ningun hashtag",
        all("#" not in t for t in sin_tags),
    )
    check(
        "local hashtags=None: no agrega hashtags",
        all(
            "#" not in t
            for t in generar_variaciones_locales_diversas(
                BASE_DUENO, 5, hashtags=None, limite=100, semilla=9
            )
        ),
    )


# --------------------------------------------------------------------------- #
# (d) IA que repite / devuelve menos / falla
# --------------------------------------------------------------------------- #
def test_ia_repeticiones(check):
    print("(d) IA identica / casi igual / cantidad incompleta")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_identicos(10)):
        identicos = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 5, registro="politica"
        )
    check(
        "IA identica: devuelve 5 textos",
        len(identicos) == 5,
        repr(len(identicos)),
    )
    check(
        "IA identica: sin duplicados exactos",
        len(set(identicos)) == 5,
        repr(identicos),
    )
    check(
        "IA identica: el texto repetido no se duplica (maximo una vez)",
        sum(t.strip() == "Mismo texto exacto para todos" for t in identicos) <= 1,
    )
    check(
        "IA identica: >=90% de pares bajo el umbral de cita",
        _pct_bajo(identicos, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
    )

    casi = [
        "Profesor le pido una disculpa por no poder llegar hoy",
        "Profesor le pido una disculpa por no poder llegar hoy.",
        "Profesor le pido una disculpa por no poder llegar hoy!",
        "Profesor le pido una disculpa por no poder llegar hoy...",
        "Profesor le pido una disculpa por no poder llegar hoy,",
    ]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_json(casi)):
        similares = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 5, registro="politica"
        )
    check(
        "IA casi igual: devuelve 5 y sin duplicados exactos",
        len(similares) == 5 and len(set(similares)) == 5,
    )
    check(
        "IA casi igual: >=90% de pares bajo el umbral de cita",
        _pct_bajo(similares, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
        f"{_pct_bajo(similares, DIVERSIDAD_UMBRAL_CITA):.3f}",
    )

    with mock.patch.object(
        GeneradorContenido, "_chat",
        _chat_json(["Solo un texto de la IA", "Otro texto de la IA"]),
    ):
        pocos = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 6, registro="politica"
        )
    check(
        "IA devuelve menos: el contrato se completa a 6",
        len(pocos) == 6 and len(set(pocos)) == 6,
        repr(pocos),
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_vacio):
        vacio = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 4, registro="politica"
        )
    check("IA vacia: 4 textos locales no vacios", len(vacio) == 4
          and all(t.strip() for t in vacio))
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        caido = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 4, registro="politica"
        )
    check("IA caida: 4 textos locales unicos", len(caido) == 4
          and len(set(caido)) == 4)

    # Chunking: 30 pedidos => al menos 2 llamadas (lotes <=25).
    llamadas = {"n": 0}

    def _chat_25(self, prompt, temperature=0.7):
        llamadas["n"] += 1
        base = llamadas["n"] * 100
        return json.dumps(
            [f"Texto distinto numero {base + i} sobre el tema" for i in range(25)]
        )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_25):
        treinta = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 30, registro="politica"
        )
    check(
        "chunking: cantidad 30 se pide por lotes (>=2 llamadas)",
        llamadas["n"] >= 2,
        repr(llamadas["n"]),
    )
    check(
        "chunking: devuelve exactamente 30 y sin duplicados",
        len(treinta) == 30 and len(set(treinta)) == 30,
    )


# --------------------------------------------------------------------------- #
# (e) ``evitar`` entre dos llamadas
# --------------------------------------------------------------------------- #
def test_evitar(check):
    print("(e) evitar impide reutilizar textos entre llamadas")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        primera = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 6, registro="politica", limite=100
        )
        segunda = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 6, registro="politica", evitar=primera, limite=100
        )
    check(
        "evitar: la segunda llamada devuelve 6 textos",
        len(segunda) == 6,
        repr(len(segunda)),
    )
    check(
        "evitar: cero textos exactos repetidos entre llamadas",
        not (set(primera) & set(segunda)),
    )
    check(
        "evitar: >=90% de pares cruzados bajo el umbral de cita",
        _pct_bajo(primera + segunda, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
        f"{_pct_bajo(primera + segunda, DIVERSIDAD_UMBRAL_CITA):.3f}",
    )

    with mock.patch.object(
        GeneradorContenido, "_chat",
        _chat_json(
            ["Texto vetado de la IA uno", "Texto vetado de la IA dos"],
        ),
    ):
        vetados = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO,
            4,
            registro="politica",
            evitar=["Texto vetado de la IA uno", "Texto vetado de la IA dos"],
        )
    check(
        "evitar: la IA no puede devolver lo vetado",
        all("texto vetado de la ia" not in t.lower() for t in vetados),
        repr(vetados),
    )


# --------------------------------------------------------------------------- #
# (f) POSTS: derivacion por cuenta
# --------------------------------------------------------------------------- #
_CONTEXTO_LARGO = (
    "La comunidad organizada salio a las calles para presentar sus propuestas "
    "sobre seguridad, agua potable, alumbrado y espacios publicos; despues de "
    "varias reuniones vecinales acordaron mantener el dialogo con las "
    "autoridades y dar seguimiento a cada compromiso hasta que se cumpla."
)


def test_posts(check):
    print("(f) posts: 8 cuentas x 3 textos con IA caida")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_hashtags_por_cuenta(
            _cuentas(8),
            hashtags="#mexico #futbol",
            contexto=_CONTEXTO_LARGO,
            n_por_cuenta=3,
            limite=100,
        )
    textos = _textos_posts(salida)
    check(
        "posts: 8 cuentas con exactamente 3 textos",
        len(salida) == 8
        and all(len(fila) == 3 for fila in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    check("posts: 24 textos en total", len(textos) == 24)
    check(
        "posts: sin duplicados exactos",
        len(set(textos)) == 24,
        f"unicos={len(set(textos))}",
    )
    check(
        "posts: todos <= 100",
        all(len(t) <= 100 for t in textos),
        repr([len(t) for t in textos]),
    )
    pct = _pct_bajo(textos, DIVERSIDAD_UMBRAL_POST)
    check(
        "posts: >=90% de pares bajo el umbral de POST",
        pct >= 0.90,
        f"{pct:.3f}",
    )
    check(
        "posts: cada texto lleva al menos un hashtag pedido",
        all(any(tag in _tags(t) for tag in ("#mexico", "#futbol"))
            for t in textos),
    )
    check(
        "posts: ningun hashtag ajeno",
        all(set(_tags(t)) <= {"#mexico", "#futbol"} for t in textos),
    )
    check(
        "posts: ningun texto termina en hashtag",
        all(_no_termina_en_hashtag(t) for t in textos),
    )
    check(
        "posts: ninguno copia el contexto completo",
        all(_CONTEXTO_LARGO.lower() not in t.lower() for t in textos)
        and all(not gc._es_copia_contexto(t, _CONTEXTO_LARGO) for t in textos),
    )
    check(
        "posts: aperturas variadas (>=8 primeras palabras distintas)",
        len({t[:25].lower() for t in textos}) >= 8,
        repr(len({t[:25].lower() for t in textos})),
    )
    check(
        "posts: validacion estricta con el subset presente",
        all(
            gc.validar_texto_hashtag(
                t, [tag for tag in ("#mexico", "#futbol")
                     if tag in _tags(t)], 100
            )["ok"]
            for t in textos
        ),
    )

    # ``evitar`` en posts: no reutiliza el texto vetado.
    vetado = "Texto vetado sobre la comunidad #mexico"
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_json([vetado] * 9)
    ):
        salida_vetada = generar_textos_hashtags_por_cuenta(
            _cuentas(3),
            hashtags="#mexico",
            contexto="la comunidad",
            n_por_cuenta=3,
            evitar=[vetado],
            limite=100,
        )
    check(
        "posts evitar: no repite el texto vetado",
        all(t.strip() != vetado for t in _textos_posts(salida_vetada)),
        repr(_textos_posts(salida_vetada)),
    )

    # Modo LOCAL 280: textos completos y hashtag en medio.
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida280 = generar_textos_hashtags_por_cuenta(
            _cuentas(2),
            hashtags="#mexico",
            contexto=_CONTEXTO_LARGO,
            n_por_cuenta=2,
            limite=280,
        )
    textos280 = _textos_posts(salida280)
    check(
        "posts 280: todos <=280 y al menos uno >100",
        textos280
        and all(len(t) <= 280 for t in textos280)
        and any(len(t) > 100 for t in textos280),
        repr([len(t) for t in textos280]),
    )
    check(
        "posts 280: conserva el hashtag pedido",
        all("#mexico" in t.lower() for t in textos280),
    )


# --------------------------------------------------------------------------- #
# (f2) Modo ACTIVIDAD: 3-4 posts por cuenta, todos distintos
# --------------------------------------------------------------------------- #
def test_actividad(check):
    print("(f2) ACTIVIDAD: diversidad entre 3-4 posts por cuenta")
    tags = ["#mexico", "#futbol"]
    cuentas = _cuentas(10)

    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida = generar_textos_actividad_por_cuenta(
            cuentas, tags, 3, contexto=_CONTEXTO_LARGO
        )
    textos = _textos_posts(salida)
    check(
        "actividad: 10 cuentas con exactamente 3 textos",
        len(salida) == 10
        and all(len(fila) == 3 for fila in salida.values()),
        repr({k: len(v) for k, v in salida.items()}),
    )
    check("actividad: 30 textos en total", len(textos) == 30)
    check(
        "actividad: sin duplicados exactos",
        len(set(textos)) == 30,
        f"unicos={len(set(textos))}",
    )
    check(
        "actividad: sin duplicados normalizados (hashtags/puntuacion aparte)",
        len({normalizar_para_comparar(t) for t in textos}) == 30,
        f"norm={len({normalizar_para_comparar(t) for t in textos})}",
    )
    pct = _pct_bajo(textos, DIVERSIDAD_UMBRAL_POST)
    check(
        "actividad: >=90% de pares bajo el umbral de POST",
        pct >= 0.90,
        f"{pct:.3f} max={max(_pares(textos)):.3f}",
    )
    check(
        "actividad: cobertura TOTAL de hashtags por cuenta",
        all(verificar_cobertura_actividad(fila, tags)
            for fila in salida.values()),
    )
    check(
        "actividad: todos <= 100",
        all(len(t) <= 100 for t in textos),
        repr([len(t) for t in textos]),
    )
    check(
        "actividad: ningun hashtag ajeno",
        all(set(_tags(t)) <= {"#mexico", "#futbol"} for t in textos),
    )
    check(
        "actividad: hashtags EN MEDIO y validos",
        all(
            gc.validar_texto_hashtag(
                t, [x for x in ("#mexico", "#futbol") if x in _tags(t)], 100
            )["ok"]
            for t in textos
        ),
    )
    check(
        "actividad: ningun texto termina en hashtag",
        all(_no_termina_en_hashtag(t) for t in textos),
    )
    check(
        "actividad: no copia el contexto",
        all(_CONTEXTO_LARGO.lower() not in t.lower() for t in textos)
        and all(not gc._es_copia_contexto(t, _CONTEXTO_LARGO) for t in textos),
    )

    # IA que devuelve SIEMPRE el mismo texto: se reemplaza y no se duplica.
    with mock.patch.object(GeneradorContenido, "_chat", _chat_identicos(30)):
        salida_id = generar_textos_actividad_por_cuenta(
            _cuentas(10), tags, 3, contexto=_CONTEXTO_LARGO
        )
    textos_id = _textos_posts(salida_id)
    check(
        "actividad IA identica: 30 textos, 3 por cuenta",
        len(textos_id) == 30
        and all(len(fila) == 3 for fila in salida_id.values()),
    )
    check(
        "actividad IA identica: sin duplicados exactos",
        len(set(textos_id)) == 30,
        f"unicos={len(set(textos_id))}",
    )
    check(
        "actividad IA identica: el texto repetido no se duplica",
        sum(t.strip() == "Mismo texto exacto para todos" for t in textos_id) <= 1,
    )
    check(
        "actividad IA identica: >=90% de pares bajo el umbral",
        _pct_bajo(textos_id, DIVERSIDAD_UMBRAL_POST) >= 0.90,
        f"{_pct_bajo(textos_id, DIVERSIDAD_UMBRAL_POST):.3f}",
    )
    check(
        "actividad IA identica: cobertura total de hashtags",
        all(verificar_cobertura_actividad(fila, tags)
            for fila in salida_id.values()),
    )

    # IA que devuelve textos CASI iguales: se filtran por similitud.
    casi = [
        "Profesor le pido una disculpa por no poder llegar hoy",
        "Profesor le pido una disculpa por no poder llegar hoy.",
        "Profesor le pido una disculpa por no poder llegar hoy!",
    ]
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_json(casi * 10)
    ):
        salida_casi = generar_textos_actividad_por_cuenta(
            _cuentas(10), tags, 3, contexto=_CONTEXTO_LARGO
        )
    textos_casi = _textos_posts(salida_casi)
    check(
        "actividad IA casi igual: 30 textos y 3 por cuenta",
        len(textos_casi) == 30
        and all(len(fila) == 3 for fila in salida_casi.values()),
    )
    check(
        "actividad IA casi igual: sin duplicados exactos",
        len(set(textos_casi)) == 30,
    )
    check(
        "actividad IA casi igual: >=90% de pares bajo el umbral",
        _pct_bajo(textos_casi, DIVERSIDAD_UMBRAL_POST) >= 0.90,
        f"{_pct_bajo(textos_casi, DIVERSIDAD_UMBRAL_POST):.3f}",
    )
    check(
        "actividad IA casi igual: cobertura total de hashtags",
        all(verificar_cobertura_actividad(fila, tags)
            for fila in salida_casi.values()),
    )

    # 1 sola cuenta x 4 textos (el caso mas exigente de plantillas).
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        salida_uno = generar_textos_actividad_por_cuenta(
            [{"usuario": "uno", "registro": "politica", "perfil": "formal"}],
            tags,
            4,
            contexto=_CONTEXTO_LARGO,
        )
    textos_uno = _textos_posts(salida_uno)
    check(
        "actividad 1x4: exactamente 4 textos",
        len(textos_uno) == 4,
        repr(textos_uno),
    )
    check(
        "actividad 1x4: sin duplicados exactos",
        len(set(textos_uno)) == 4,
    )
    check(
        "actividad 1x4: pares bajo el umbral de POST",
        all(s < DIVERSIDAD_UMBRAL_POST for s in _pares(textos_uno)),
        repr([round(s, 3) for s in _pares(textos_uno)]),
    )
    check(
        "actividad 1x4: cobertura total de hashtags",
        verificar_cobertura_actividad(textos_uno, tags),
    )

    # Firma CONGELADA del motor de Actividad.
    sig = inspect.signature(generar_textos_actividad_por_cuenta)
    check(
        "firma actividad: parametros exactos y en orden",
        list(sig.parameters) == [
            "cuentas_info", "hashtags", "n_por_cuenta",
            "contexto", "narrativa", "menciones",
        ],
        repr(sig),
    )
    check(
        "firma actividad: defaults contexto/narrativa '' y menciones None",
        sig.parameters["contexto"].default == ""
        and sig.parameters["narrativa"].default == ""
        and sig.parameters["menciones"].default is None,
    )


# --------------------------------------------------------------------------- #
# (g) Firmas congeladas: ``limite`` SIEMPRE al final con default 100
# --------------------------------------------------------------------------- #
def test_firmas(check):
    print("(g) firmas: limite al final, evitar/angulos antes")
    sig_var = inspect.signature(GeneradorContenido.generar_variaciones_masivas)
    params_var = list(sig_var.parameters)
    check(
        "firma variaciones: limite=100 es el ULTIMO parametro",
        params_var[-1] == "limite"
        and sig_var.parameters["limite"].default == 100,
        repr(sig_var),
    )
    check(
        "firma variaciones: evitar va antes de limite",
        "evitar" in params_var
        and params_var.index("evitar") == params_var.index("limite") - 1,
        repr(params_var),
    )

    sig_pool = inspect.signature(var.generar_pool_variaciones_openai)
    params_pool = list(sig_pool.parameters)
    check(
        "firma pool OpenAI: limite=100 es el ULTIMO parametro",
        params_pool[-1] == "limite"
        and sig_pool.parameters["limite"].default == 100,
        repr(sig_pool),
    )
    check(
        "firma pool OpenAI: evitar va antes de limite",
        "evitar" in params_pool
        and params_pool.index("evitar") == params_pool.index("limite") - 1,
    )

    sig_posts = inspect.signature(generar_textos_hashtags_por_cuenta)
    params_posts = list(sig_posts.parameters)
    check(
        "firma posts: limite=100 es el ULTIMO parametro",
        params_posts[-1] == "limite"
        and sig_posts.parameters["limite"].default == 100,
        repr(sig_posts),
    )
    check(
        "firma posts: angulos y evitar antes de limite",
        params_posts.index("angulos") < params_posts.index("limite")
        and params_posts.index("evitar") < params_posts.index("limite"),
    )
    sig_local = inspect.signature(gc._texto_hashtag_local)
    check(
        "firma _texto_hashtag_local: limite=100 al final",
        list(sig_local.parameters)[-1] == "limite"
        and sig_local.parameters["limite"].default == 100,
    )
    sig_prompt = inspect.signature(prompts.get_prompt_hashtags)
    check(
        "firma get_prompt_hashtags: limite=100 al final",
        list(sig_prompt.parameters)[-1] == "limite"
        and sig_prompt.parameters["limite"].default == 100,
    )
    check(
        "wrapper publico generar_pool_variaciones_diversas existe",
        callable(var.generar_pool_variaciones_diversas),
    )


# --------------------------------------------------------------------------- #
# (h) Casos limite
# --------------------------------------------------------------------------- #
def test_limites(check):
    print("(h) casos limite")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        vacia = GeneradorContenido().generar_variaciones_masivas("", 6)
    check(
        "base vacia: 6 textos no vacios",
        len(vacia) == 6 and all(t.strip() for t in vacia),
    )
    corta = generar_variaciones_locales_diversas(
        "Hola mundo", 5, limite=100, semilla=3
    )
    check(
        "base de 10 chars: 5 textos y ninguno igual al base",
        len(corta) == 5 and all(t.strip() != "Hola mundo" for t in corta),
        repr(corta),
    )
    check(
        "cantidad 0: generador local devuelve []",
        generar_variaciones_locales_diversas(BASE_DUENO, 0) == [],
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        uno = GeneradorContenido().generar_variaciones_masivas(
            BASE_DUENO, 1
        )
    check("cantidad 1: devuelve exactamente 1", len(uno) == 1 and bool(uno[0]))

    l20 = generar_variaciones_locales_diversas(
        BASE_DUENO, 6, limite=20, semilla=5
    )
    check(
        "limite 20: todos <= 20",
        len(l20) == 6 and all(len(t) <= 20 for t in l20),
        repr([len(t) for t in l20]),
    )
    l280 = generar_variaciones_locales_diversas(
        BASE_DUENO, 6, limite=280, semilla=5
    )
    check(
        "limite 280: todos <= 280 y al menos uno >100",
        len(l280) == 6
        and all(len(t) <= 280 for t in l280)
        and any(len(t) > 100 for t in l280),
    )
    emoji = generar_variaciones_locales_diversas(
        "Hoy celebramos con 🎉 y mucho ánimo; ¡qué alegría!",
        4,
        limite=100,
        semilla=1,
    )
    check(
        "emojis/acentos: 4 textos no vacios",
        len(emoji) == 4 and all(t.strip() for t in emoji),
    )
    check(
        "normalizar/similitud: entradas gigantes no lanzan",
        isinstance(
            similitud_textos("palabra " * 2000, "palabra " * 1999), float
        ),
    )
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        raro = GeneradorContenido().generar_variaciones_masivas(None, 3)
    check(
        "base None: 3 textos y no lanza",
        len(raro) == 3 and all(t.strip() for t in raro),
    )


# --------------------------------------------------------------------------- #
# (i) Wrapper de activaciones
# --------------------------------------------------------------------------- #
def test_wrapper_activaciones(check):
    print("(i) wrapper de citas de activaciones")
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        pool = var.generar_pool_variaciones_openai(
            BASE_DUENO, 6, registro="politica", limite=100
        )
    check(
        "pool: IA caida devuelve 6 textos <=100",
        len(pool) == 6 and all(len(t) <= 100 for t in pool),
        repr([len(t) for t in pool]),
    )
    check(
        "pool: ninguno es el base literal y sin duplicados",
        all(t.strip() != BASE_DUENO for t in pool) and len(set(pool)) == 6,
    )
    check(
        "pool: >=90% de pares bajo el umbral de cita",
        _pct_bajo(pool, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        pool_evitar = var.generar_pool_variaciones_openai(
            BASE_DUENO, 6, registro="politica", evitar=pool, limite=100
        )
    check(
        "pool evitar: cero exactos repetidos",
        not (set(pool) & set(pool_evitar)) and len(pool_evitar) == 6,
    )
    check(
        "pool evitar: >=90% de pares cruzados bajo el umbral",
        _pct_bajo(pool + pool_evitar, DIVERSIDAD_UMBRAL_CITA) >= 0.90,
        f"{_pct_bajo(pool + pool_evitar, DIVERSIDAD_UMBRAL_CITA):.3f}",
    )

    diversas = var.generar_pool_variaciones_diversas(
        BASE_DUENO, 5, hashtags=["#mexico"], limite=100, semilla=4
    )
    check(
        "wrapper diverso: 5 textos, hashtag pedido y <=100",
        len(diversas) == 5
        and all("#mexico" in t.lower() for t in diversas)
        and all(len(t) <= 100 for t in diversas),
    )
    check(
        "wrapper diverso: nunca devuelve el base literal",
        all(t.strip() != BASE_DUENO for t in diversas),
    )


def run(check):
    """Ejecuta los checks con el ``check`` del runner (o del marco local)."""
    test_utilidades(check)
    test_prompts(check)
    test_caso_dueno(check)
    test_ia_repeticiones(check)
    test_evitar(check)
    test_posts(check)
    test_actividad(check)
    test_firmas(check)
    test_limites(check)
    test_wrapper_activaciones(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
