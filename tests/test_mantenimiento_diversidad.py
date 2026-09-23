"""Tests de DIVERSIDAD del mantenimiento organico (sin red, sin OpenAI real).

Cubre el requerimiento del dueño: que el contenido de MANTENIMIENTO no parezca
un enjambre de bots. Verifica:

  (a) ``TEMAS_MANTENIMIENTO`` con >=30 temas cotidianos (claves normalizadas,
      sin duplicados) y una instruccion PROPIA (no generica) para cada uno;
      alias/plurales/acentos de los temas legacy y nuevos normalizan bien.
  (b) ``_prompt_lote_mantenimiento`` con lote MIXTO (15 slots): trae el bloque
      "REGLA DE DIVERSIDAD (OBLIGATORIA)", menciona cada tema asignado, reparte
      angulos narrativos distintos por PERFIL consecutivo y los temas listados
      son TODOS distintos (lotes ya no homogeneos por tema).
  (c) Fallback TOTAL con IA caida (``GeneradorContenido._chat`` lanza): 30
      cuentas x 2 textos => no vacios, EXACTAMENTE 2 por cuenta, 60/60 unicos,
      posts <=100 con "#" en medio, comentarios sin "#"/"@"/"http" y CERO
      palabras clave compartidas entre los textos de una MISMA cuenta.
  (d) ``_fallback_estructura_mantenimiento`` (30 x 2) con las mismas garantias.
  (e) Determinismo (misma entrada => mismo resultado; entradas distintas =>
      resultados distintos) y diversidad de vocabulario (>=40 palabras
      tematicas distintas en los 60 textos).
  (f) Extras: pools por partes con los minimos (>=30 sujetos, >=20 acciones,
      >=30 comentarios por perfil), formato de ``_generar_plantilla_local``
      por perfil, respeto de ``evitar`` y los wrappers ``_plantillas_por_perfil``
      / ``_plantillas_comentario``.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_mantenimiento_diversidad.py
"""
from __future__ import annotations

import inspect
import re
import sys
import unicodedata
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los demas tests).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia.generador_contenido as gc  # noqa: E402
import ia.prompts as prompts  # noqa: E402

_PERFILES = ("formal", "ciudadano", "popular", "")
_REGISTROS = ("politica", "activista", "ciudadana", "")


def _sin_acentos(texto: str) -> str:
    """Minusculas sin diacriticos (mismas reglas que ``_normalizar_tema``)."""
    plano = unicodedata.normalize("NFKD", str(texto or "").lower())
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return plano.replace("ñ", "n")


def _cuentas_diversidad(n: int = 30) -> list[dict]:
    """Cuentas de prueba: perfiles/registros mezclados y algunas de comentario."""
    cuentas = []
    for i in range(n):
        cuentas.append({
            "usuario": f"cuenta_div_{i:02d}",
            "registro": _REGISTROS[i % len(_REGISTROS)],
            "perfil": _PERFILES[(i * 3) % len(_PERFILES)],
            "personalidad": f"le gusta el futbol y la musica {i}",
            "nombre": f"Cuenta Diversa {i:02d}",
            "tipo_accion": "comentario" if i % 5 == 0 else "post",
        })
    return cuentas


def cuentas_accion(cuenta: dict) -> str:
    """tipo_accion de la cuenta (normalizado por el propio modulo)."""
    return gc._normalizar_accion((cuenta or {}).get("tipo_accion"))


def _textos_planos(res: list) -> list[str]:
    return [t for fila in (res or []) for t in (fila or [])]


def _hashtag_en_medio(texto: str) -> bool:
    """True si el primer hashtag esta integrado y NO al final del texto."""
    t = str(texto or "")
    pos = t.find("#")
    if pos <= 0 or pos > int(len(t) * 0.92):
        return False
    return not gc._hashtag_al_final(t)


def _chat_caido(capturas=None):
    """``_chat`` falso que SIEMPRE lanza (IA caida total, sin red)."""

    def _fake(self, prompt, temperature=0.7):
        if capturas is not None:
            capturas.append(prompt)
        raise RuntimeError("OPENAI_API_KEY invalida (test de diversidad)")

    return _fake


def _solape_por_cuenta(res: list) -> int:
    """Numero de cuentas cuyos textos comparten alguna palabra clave."""
    solapadas = 0
    for fila in res or []:
        usadas: set = set()
        for t in fila or []:
            claves = gc._palabras_clave(t)
            if claves & usadas:
                solapadas += 1
                break
            usadas |= claves
    return solapadas


# --------------------------------------------------------------------------- #
# (a) Temas: >=30, normalizados, instruccion propia y alias
# --------------------------------------------------------------------------- #
def test_temas(check):
    print("(a) TEMAS_MANTENIMIENTO >=30 con instruccion propia y alias")
    temas = tuple(prompts.TEMAS_MANTENIMIENTO)
    check("temas: al menos 30", len(temas) >= 30, f"n={len(temas)}")
    check(
        "temas: sin duplicados",
        len(set(temas)) == len(temas),
        f"unicos={len(set(temas))}",
    )
    claves_ok = all(
        isinstance(t, str)
        and t == t.lower()
        and t == _sin_acentos(t)
        and re.fullmatch(r"[a-z0-9_]+", t) is not None
        for t in temas
    )
    check("temas: claves normalizadas (minusculas, sin acentos ni espacios)", claves_ok)

    legacy = ("azteca", "dia", "tendencias", "gustos")
    check(
        "temas: los 4 legacy siguen existiendo",
        all(t in temas for t in legacy),
        repr([t for t in legacy if t not in temas]),
    )
    obligatorios = (
        "trafico",
        "clima",
        "comida",
        "series",
        "lunes",
        "insomnio",
        "futbol",
        "memes",
        "motivacion",
        "mascotas",
    )
    check(
        "temas: incluye los 10 cotidianos obligatorios",
        all(t in temas for t in obligatorios),
        repr([t for t in obligatorios if t not in temas]),
    )

    generico = False
    con_tema = 0
    instrucciones = {}
    for t in temas:
        ins = prompts.instrucciones_tema(t)
        instrucciones[t] = ins
        if not ins or ins.startswith("TEMA A TRATAR"):
            generico = True
        else:
            con_tema += 1
    check(
        "instrucciones: TODOS los temas tienen instruccion propia (no generica)",
        not generico and con_tema == len(temas),
        f"con_instruccion={con_tema}/{len(temas)}",
    )
    check(
        "instrucciones: todas empiezan con 'TEMA:' y son especificas",
        all(ins.startswith("TEMA:") and len(ins) > 80 for ins in instrucciones.values()),
    )
    check(
        "instrucciones: legacy conserva su texto exacto",
        instrucciones.get("azteca", "").startswith("TEMA: AZTECA")
        and "TEMA: DIA / ACTUALIDAD" in instrucciones.get("dia", "")
        and "TEMA: TENDENCIAS" in instrucciones.get("tendencias", "")
        and "TEMA: GUSTOS" in instrucciones.get("gustos", ""),
    )
    check(
        "instrucciones: el generico queda SOLO para temas desconocidos",
        prompts.instrucciones_tema("tema-inventado-xyz").startswith("TEMA A TRATAR"),
    )

    alias_legacy = {
        "prehispanico": "azteca",
        "prehispanica": "azteca",
        "mexica": "azteca",
        "aztecas": "azteca",
        "actualidad": "dia",
        "diario": "dia",
        "efemerides": "dia",
        "tendencia": "tendencias",
        "trending": "tendencias",
        "gusto": "gustos",
        "cotidiano": "gustos",
        "intereses": "gustos",
    }
    malos = {
        k: (prompts._normalizar_tema(k), v)
        for k, v in alias_legacy.items()
        if prompts._normalizar_tema(k) != v
    }
    check("alias legacy: normalizan a su tema", not malos, repr(malos))

    alias_nuevos = {
        "tráfico": "trafico",
        "trafico vehicular": "trafico",
        "clima local": "clima",
        "antojo": "comida",
        "antojos": "comida",
        "comida de casa": "comida",
        "series de tv": "series",
        "series": "series",
        "tv": "series",
        "no puedo dormir": "insomnio",
        "desveladas": "insomnio",
        "fútbol": "futbol",
        "frases": "motivacion",
        "motivacionales": "motivacion",
        "mascota": "mascotas",
        "perros": "mascotas",
        "gatos": "mascotas",
        "home office": "home_office",
        "teletrabajo": "home_office",
        "súper": "super",
        "mercado": "super",
        "fin de semana": "finde",
        "música": "musica",
        "pan dulce": "postres",
    }
    malos_nuevos = {}
    for entrada, esperado in alias_nuevos.items():
        obtenido = prompts._normalizar_tema(entrada)
        if obtenido != esperado:
            malos_nuevos[entrada] = obtenido
    check(
        "alias nuevos: acentos/plurales/sinonimos normalizan al canonico",
        not malos_nuevos,
        repr(malos_nuevos),
    )
    check(
        "alias: instrucciones_tema('tráfico') == instrucciones_tema('trafico')",
        prompts.instrucciones_tema("tráfico") == prompts.instrucciones_tema("trafico")
        and prompts.instrucciones_tema("fútbol") == prompts.instrucciones_tema("futbol"),
    )

    # Una sola fuente de verdad con ia.generador_contenido.
    check(
        "temas: _TEMAS_MANTENIMIENTO deriva de prompts.TEMAS_MANTENIMIENTO",
        tuple(gc._TEMAS_MANTENIMIENTO) == temas,
        repr((len(gc._TEMAS_MANTENIMIENTO), len(temas))),
    )
    check(
        "temas: vecinos de familia DISTINTA (rotacion por cuenta sin repetir familia)",
        all(
            gc._familia_de_tema(temas[i]) != gc._familia_de_tema(temas[i + 1])
            for i in range(len(temas) - 1)
        ),
    )


# --------------------------------------------------------------------------- #
# (b) Prompt del lote: diversidad, temas mezclados y angulos rotativos
# --------------------------------------------------------------------------- #
def test_prompt_diversidad(check):
    print("(b) _prompt_lote_mantenimiento: diversidad + temas mezclados")
    temas = list(gc._TEMAS_MANTENIMIENTO)
    lote = [(i, 0, temas[i]) for i in range(15)]
    cuentas = _cuentas_diversidad(15)
    prompt = gc._prompt_lote_mantenimiento(lote, cuentas)

    check(
        "prompt: trae el bloque de diversidad obligatoria",
        "REGLA DE DIVERSIDAD (OBLIGATORIA)" in prompt,
    )
    check(
        "prompt: prohibe repetir palabras clave/estructuras",
        "PROHIBIDO repetir palabras clave" in prompt
        and "NINGUN texto puede parecerse a otro del lote" in prompt,
    )
    check(
        "prompt: cada PERFIL trata SOLO su tema",
        "trata SOLO" in prompt and "PROHIBIDO repetir el tema" in prompt,
    )

    temas_prompt = re.findall(r"tema: (\S+) \|", prompt)
    check(
        "prompt: lista los 15 temas del lote",
        len(temas_prompt) == 15,
        f"temas={len(temas_prompt)}",
    )
    check(
        "prompt: los temas del lote son TODOS distintos (lote mixto)",
        len(set(temas_prompt)) == 15,
        repr(sorted(set(temas_prompt))),
    )
    faltantes = [t for t in temas[:15] if f"tema: {t}" not in prompt]
    check("prompt: menciona cada tema asignado", not faltantes, repr(faltantes))
    faltan_instr = [
        t for t in temas[:15]
        if prompts.instrucciones_tema(t)[:40] not in prompt
    ]
    check(
        "prompt: incluye la instruccion especifica de cada tema",
        not faltan_instr,
        repr(faltan_instr),
    )

    angulos = re.findall(r"angulo: ([a-z]+(?:[- ][a-z]+)*)", prompt)
    check(
        "prompt: asigna un angulo a cada PERFIL",
        len(angulos) == 15 and all(a in gc._ANGULOS_MANTENIMIENTO for a in angulos),
        repr(angulos[:4]),
    )
    check(
        "prompt: los angulos consecutivos son distintos",
        len(set(angulos)) >= 6
        and all(angulos[i] != angulos[i + 1] for i in range(len(angulos) - 1)),
        repr(angulos),
    )
    check(
        "prompt: exige usar el angulo de cada texto",
        "ANGULO NARRATIVO OBLIGATORIO DE ESTE TEXTO" in prompt,
    )

    # Reglas existentes que NO deben romperse: hashtag en medio en posts,
    # comentarios spam-safe y limite 100.
    check(
        "prompt: conserva hashtag EN MEDIO para los posts",
        "hashtag INTEGRADO EN MEDIO" in prompt and "NUNCA lo pongas al final" in prompt,
    )
    check(
        "prompt: conserva comentarios sin hashtags/links/@menciones",
        "NO deben llevar hashtags" in prompt
        and "links, NI @menciones" in prompt,
    )
    check(
        "prompt: conserva el limite de 100 para posts",
        "Maximo 100 caracteres" in prompt,
    )

    # Lote mixto (posts + comentarios): ambas reglas conviven.
    cuentas_mixtas = _cuentas_diversidad(3)
    cuentas_mixtas[0]["tipo_accion"] = "post"
    cuentas_mixtas[1]["tipo_accion"] = "comentario"
    cuentas_mixtas[2]["tipo_accion"] = "post"
    prompt_mixto = gc._prompt_lote_mantenimiento(
        [(0, 0, "clima"), (1, 0, "comida"), (2, 0, "futbol")], cuentas_mixtas
    )
    check(
        "prompt mixto: los comentarios NO llevan hashtags y los posts SI",
        "Los textos de POST DEBEN incluir" in prompt_mixto
        and "COMENTARIO/RESPUESTA NO deben llevar hashtags" in prompt_mixto,
    )


# --------------------------------------------------------------------------- #
# (c) Fallback total con IA caida: 30 cuentas x 2
# --------------------------------------------------------------------------- #
def test_fallback_ia_caida(check):
    print("(c) fallback con IA caida: 30 cuentas x 2, 60/60 unicos y sin solape")
    cuentas = _cuentas_diversidad(30)
    capturas: list = []
    with mock.patch.object(gc.GeneradorContenido, "_chat", _chat_caido(capturas)):
        res = gc.generar_textos_mantenimiento(cuentas, n_por_cuenta=2)

    check("fallback IA: el _chat falso SI se intento (IA caida real)", len(capturas) > 0)
    # Integracion: los lotes REALES que manda el impl mezclan 15 temas
    # distintos (ya no se agrupan por tema en lotes homogeneos).
    lotes_temas = [
        re.findall(r"tema: (\S+) \|", prompt) for prompt in capturas
    ]
    check(
        "fallback IA: cada lote real lleva 15 temas DISTINTOS (mezcla forzada)",
        bool(lotes_temas)
        and all(len(temas) == 15 and len(set(temas)) == 15 for temas in lotes_temas),
        repr([len(set(t)) for t in lotes_temas]),
    )
    check("fallback IA: devuelve 30 filas", len(res) == 30, f"filas={len(res)}")
    check(
        "fallback IA: EXACTAMENTE 2 textos por cuenta",
        all(len(fila) == 2 for fila in res),
        repr([len(f) for f in res][:6]),
    )
    textos = _textos_planos(res)
    check("fallback IA: 60 textos no vacios", len(textos) == 60 and all(t.strip() for t in textos))
    check(
        "fallback IA: GLOBALMENTE unicos (60/60)",
        len(set(textos)) == len(textos),
        f"unicos={len(set(textos))}",
    )

    # Escenarios: cuentas con sobresaltos (perfiles/registros mezclados).
    cuentas_estilos = []
    base_cuentas = _cuentas_diversidad(30)
    for i in range(30):
        cu = dict(base_cuentas[i])
        cu["perfil"] = ("formal", "ciudadano", "popular", "")[i % 4]
        cu["registro"] = ("politica", "activista", "ciudadana", "")[i % 4]
        cu["tipo_accion"] = "comentario" if i % 3 == 0 else "post"
        cuentas_estilos.append(cu)
    with mock.patch.object(gc.GeneradorContenido, "_chat", _chat_caido()):
        res_estilos = gc.generar_textos_mantenimiento(cuentas_estilos, n_por_cuenta=2)
    textos_estilos = _textos_planos(res_estilos)
    check(
        "fallback IA (mezcla perfiles/registros): 60/60 unicos",
        len(textos_estilos) == 60 and len(set(textos_estilos)) == 60,
        f"unicos={len(set(textos_estilos))}",
    )
    check(
        "fallback IA (mezcla perfiles/registros): cero solape por cuenta",
        _solape_por_cuenta(res_estilos) == 0,
        f"cuentas_con_solape={_solape_por_cuenta(res_estilos)}",
    )

    posts = []
    comentarios = []
    for cuenta, fila in zip(cuentas, res):
        destino = comentarios if cuentas_accion(cuenta) == "comentario" else posts
        destino.extend(fila)
    check(
        "fallback IA: los POSTS jamas superan 100 caracteres",
        posts and all(len(t) <= 100 for t in posts),
        repr([len(t) for t in posts if len(t) > 100][:3]),
    )
    check(
        "fallback IA: TODO post lleva '#' integrado en medio (no al final)",
        posts and all(_hashtag_en_medio(t) for t in posts),
        repr([t for t in posts if not _hashtag_en_medio(t)][:2]),
    )
    check(
        "fallback IA: NINGUN comentario lleva #, @ ni http",
        comentarios
        and all(
            "#" not in t and "@" not in t and "http" not in t.lower()
            for t in comentarios
        ),
        repr([t for t in comentarios if "#" in t or "@" in t or "http" in t.lower()][:2]),
    )
    check(
        "fallback IA: los comentarios son conversacionales (>=10 chars)",
        comentarios and all(len(t.strip()) >= 10 for t in comentarios),
        repr([t for t in comentarios if len(t.strip()) < 10][:2]),
    )
    check(
        "fallback IA: cero palabras clave compartidas por cuenta",
        _solape_por_cuenta(res) == 0,
        f"cuentas_con_solape={_solape_por_cuenta(res)}",
    )


# --------------------------------------------------------------------------- #
# (d) Fallback estructural directo: 30 x 2
# --------------------------------------------------------------------------- #
def test_fallback_estructura(check):
    print("(d) _fallback_estructura_mantenimiento: 30 x 2 unico y sin solape")
    cuentas = _cuentas_diversidad(30)
    res = gc._fallback_estructura_mantenimiento(cuentas, 2)
    textos = _textos_planos(res)
    check("estructura: 30 filas de 2 textos", len(res) == 30 and all(len(f) == 2 for f in res))
    check("estructura: no vacios", all(t.strip() for t in textos))
    check(
        "estructura: GLOBALMENTE unicos (60/60)",
        len(set(textos)) == len(textos),
        f"unicos={len(set(textos))}",
    )
    check(
        "estructura: cero palabras clave compartidas por cuenta",
        _solape_por_cuenta(res) == 0,
        f"cuentas_con_solape={_solape_por_cuenta(res)}",
    )
    posts = []
    comentarios = []
    for cuenta, fila in zip(cuentas, res):
        destino = comentarios if cuentas_accion(cuenta) == "comentario" else posts
        destino.extend(fila)
    check(
        "estructura: posts <=100 con '#' en medio",
        posts and all(len(t) <= 100 and _hashtag_en_medio(t) for t in posts),
    )
    check(
        "estructura: comentarios sin #, @ ni http",
        comentarios
        and all(
            "#" not in t and "@" not in t and "http" not in t.lower()
            for t in comentarios
        ),
    )

    # n_por_cuenta=0 y entradas raras: nunca lanza.
    try:
        vacio = gc._fallback_estructura_mantenimiento(cuentas, 0)
        raro = gc._fallback_estructura_mantenimiento(
            [None, "basura", {"perfil": None}], 1
        )
        error = None
    except Exception as e:  # noqa: BLE001
        error = e
        vacio, raro = [], []
    check(
        "estructura: n=0 y entradas raras no lanzan",
        error is None and all(len(f) == 0 for f in vacio) and len(raro) == 3,
        repr(error),
    )


# --------------------------------------------------------------------------- #
# (e) Determinismo y diversidad de vocabulario
# --------------------------------------------------------------------------- #
def test_determinismo_y_vocabulario(check):
    print("(e) determinismo y vocabulario de los 60 textos")
    cuentas = _cuentas_diversidad(30)
    with mock.patch.object(gc.GeneradorContenido, "_chat", _chat_caido()):
        res_a = gc.generar_textos_mantenimiento(cuentas, n_por_cuenta=2)
        res_b = gc.generar_textos_mantenimiento(cuentas, n_por_cuenta=2)
    check(
        "determinismo: misma entrada => mismo resultado",
        _textos_planos(res_a) == _textos_planos(res_b),
    )

    cuentas_reves = list(reversed(cuentas))
    with mock.patch.object(gc.GeneradorContenido, "_chat", _chat_caido()):
        res_reves = gc.generar_textos_mantenimiento(cuentas_reves, n_por_cuenta=2)
    check(
        "determinismo: entradas distintas (orden/usuarios) => resultados distintos",
        _textos_planos(res_a) != _textos_planos(res_reves),
    )

    res_fb_a = gc._fallback_estructura_mantenimiento(cuentas, 2)
    res_fb_b = gc._fallback_estructura_mantenimiento(cuentas, 2)
    check(
        "determinismo: fallback estructural reproducible",
        _textos_planos(res_fb_a) == _textos_planos(res_fb_b),
    )

    # Vocabulario: palabras tematicas (sin hashtags) distintas en los 60.
    vocabulario: set = set()
    for t in _textos_planos(res_a):
        sin_tags = re.sub(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", " ", t)
        vocabulario |= gc._palabras_clave(sin_tags)
    check(
        "vocabulario: >=40 palabras tematicas distintas en 60 textos",
        len(vocabulario) >= 40,
        f"distintas={len(vocabulario)}",
    )

    # Los temas rotativos cubren un abanico amplio de hashtags distintos.
    tags = set()
    for t in _textos_planos(res_a):
        tags |= {
            tag.lower()
            for tag in re.findall(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+", t)
        }
    check(
        "vocabulario: >=20 hashtags de tema distintos entre los posts",
        len(tags) >= 20,
        f"hashtags={len(tags)}",
    )


# --------------------------------------------------------------------------- #
# (f) Extras: pools por partes, formato por perfil, evitar y wrappers
# --------------------------------------------------------------------------- #
def test_pools_y_generador(check):
    print("(f) pools por partes, formato por perfil y respeto de 'evitar'")
    perfiles = ("formal", "ciudadano", "popular", "generico")
    minimos_ok = True
    detalle = {}
    for p in perfiles:
        piezas = gc._PLANTILLAS_MANTENIMIENTO.get(p) or {}
        n_suj = len(piezas.get("sujetos") or ())
        n_acc = len(piezas.get("acciones") or ())
        n_com = len(piezas.get("comentarios") or ())
        detalle[p] = (n_suj, n_acc, n_com)
        if (n_suj, n_acc, n_com) < (30, 20, 30):
            minimos_ok = False
    check(
        "pools: >=30 sujetos x >=20 acciones x >=30 comentarios por perfil",
        minimos_ok,
        repr(detalle),
    )
    check(
        "pools: familias de vocabulario cubren los temas (>=12 familias)",
        len(set(gc._FAMILIA_POR_TEMA.values())) >= 12
        and all(
            gc._familia_de_tema(t) in gc._PIEZAS_FAMILIA
            for t in gc._TEMAS_MANTENIMIENTO
        ),
    )
    check(
        "pools: cada familia tiene sujetos/acciones/comentarios",
        all(
            all(
                len(fam.get(tipo) or ()) >= 3
                for tipo in ("sujetos", "acciones", "comentarios")
            )
            for fam in gc._PIEZAS_FAMILIA.values()
        ),
    )

    # _generar_plantilla_local: firma congelada y sin narrativa.
    firma = inspect.signature(gc._generar_plantilla_local)
    check(
        "generador: firma (perfil, tema, semilla, comentario, evitar)",
        list(firma.parameters) == ["perfil", "tema", "semilla", "comentario", "evitar"],
        repr(list(firma.parameters)),
    )
    check(
        "generador: NUNCA recibe/lee narrativa (anti-fuga)",
        "narrativa" not in firma.parameters,
    )

    t_formal = gc._generar_plantilla_local(perfil="formal", tema="clima", semilla=1)
    check(
        "generador formal: 3 bloques con doble enter",
        "\n\n" in t_formal and len(t_formal.split("\n\n")) >= 3,
        repr(t_formal[:80]),
    )
    t_popular = gc._generar_plantilla_local(perfil="popular", tema="clima", semilla=1)
    check(
        "generador popular: 1 renglon casual con faltas q/pa/xq/tons/k",
        "\n" not in t_popular
        and re.search(r"\b(q|pa|xq|tons|k)\b", t_popular) is not None,
        repr(t_popular[:80]),
    )
    t_ciudadano = gc._generar_plantilla_local(perfil="ciudadano", tema="clima", semilla=1)
    check(
        "generador ciudadano: 1-2 frases (sin doble enter)",
        "\n\n" not in t_ciudadano and len(t_ciudadano) > 10,
        repr(t_ciudadano[:80]),
    )

    t1 = gc._generar_plantilla_local(perfil="ciudadano", tema="trafico", semilla=7)
    t2 = gc._generar_plantilla_local(perfil="ciudadano", tema="trafico", semilla=7)
    t3 = gc._generar_plantilla_local(perfil="ciudadano", tema="trafico", semilla=8)
    check("generador: misma semilla => mismo texto", t1 == t2, repr(t1[:60]))
    check("generador: semilla distinta => texto distinto", t1 != t3)

    comentario = gc._generar_plantilla_local(
        perfil="popular", tema="clima", semilla=3, comentario=True
    )
    check(
        "generador comentario: sin hashtags y conversacional",
        "#" not in comentario and len(comentario) > 5,
        repr(comentario[:80]),
    )

    # Theme-aware: en varias semillas aparece vocabulario de la familia calle.
    vocab_calle: set = set()
    for piezas in gc._PIEZAS_FAMILIA["calle"].values():
        for pieza in piezas:
            vocab_calle |= gc._palabras_clave(pieza)
    con_familia = 0
    for semilla in range(8):
        t = gc._generar_plantilla_local(
            perfil="ciudadano", tema="trafico", semilla=semilla
        )
        if gc._palabras_clave(t) & vocab_calle:
            con_familia += 1
    check(
        "generador: tema-aware (usa vocabulario de la familia del tema)",
        con_familia >= 4,
        f"muestras_con_familia={con_familia}/8",
    )
    check(
        "generador: tema vacio/desconocido usa pools genericos",
        gc._familia_de_tema("") == "generico"
        and gc._familia_de_tema("tema-inventado") == "generico",
    )

    # 'evitar': reintenta hasta no compartir palabras clave.
    base = gc._generar_plantilla_local(
        perfil="formal", tema="comida", semilla=1, comentario=True
    )
    reto = gc._generar_plantilla_local(
        perfil="formal",
        tema="comida",
        semilla=2,
        comentario=True,
        evitar=gc._palabras_clave(base),
    )
    check(
        "generador: respeta 'evitar' (cero palabras clave compartidas)",
        bool(reto) and not (gc._palabras_clave(reto) & gc._palabras_clave(base)),
        repr((base[:40], reto[:40])),
    )

    # Wrappers compatibles y no vacios.
    muestras = gc._plantillas_por_perfil("clima", "formal")
    comentarios = gc._plantillas_comentario("popular")
    check(
        "_plantillas_por_perfil: devuelve muestras no vacias",
        isinstance(muestras, tuple) and len(muestras) >= 4,
        f"n={len(muestras)}",
    )
    check(
        "_plantillas_comentario: devuelve muestras sin hashtags",
        isinstance(comentarios, tuple)
        and len(comentarios) >= 4
        and all("#" not in t for t in comentarios),
        f"n={len(comentarios)}",
    )
    check(
        "wrappers: nunca lanzan con entradas raras",
        gc._plantillas_por_perfil("", "") is not None
        and gc._plantillas_comentario("") is not None,
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_temas(check)
    test_prompt_diversidad(check)
    test_fallback_ia_caida(check)
    test_fallback_estructura(check)
    test_determinismo_y_vocabulario(check)
    test_pools_y_generador(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_mantenimiento_diversidad.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
