"""Tests del limite configurable 100/280 para posts con hashtag (modo LOCAL).

Problema real: la campana LOCAL (Chrome visible, 5 cuentas) reutiliza
``generar_textos_hashtags_por_cuenta``, que tenia un limite duro de 100
caracteres. Los textos salian cortados con "..." y a veces sin el hashtag.

Regla del cambio:
  * Default ``limite=100`` = comportamiento historico BYTE A BYTE (campanas
    masivas: activaciones, actividad, mantenimiento). No se toca su contrato.
  * El modo LOCAL pasa ``limite=280`` (maximo de X): el texto se conserva
    COMPLETO, con el hashtag pedido integrado EN MEDIO (ventana 15-85%), sin
    inventar otros hashtags y sin agregar "..." cuando cabe.

Cubre (sin red, sin OpenAI real y sin Chrome):

  (a) ``get_prompt_hashtags``: con 100 usa ``REGLA_MAX_100`` y "100
      caracteres"; con 280 usa "280" y NO la regla de 100. El default y la
      basura se comportan como 100.
  (b) ``garantizar_texto_hashtag``: un texto de ~232 chars con ``#Muuki2026``
      pasa completo con ``limite=280`` (sin "..." del recorte) y sigue
      recortado a <=100 con ``limite=100``.
  (c) ``generar_textos_hashtags_por_cuenta`` con IA simulada (texto largo) y
      con IA caida (fallback local): ``limite=280`` devuelve <=280 y >100
      cuando el material lo permite; con el default devuelve <=100.
  (d) El hashtag NO se pierde al pasar de 100 a 280.
  (e) ``_texto_hashtag_local(..., limite=280)`` respeta el limite.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_limite_local_280.py   (solo este)
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
import ia.prompts as prompts  # noqa: E402
from ia.generador_contenido import GeneradorContenido  # noqa: E402
from ia.prompts import REGLA_MAX_100, get_prompt_hashtags  # noqa: E402

_TAG = "#Muuki2026"
_RE_TAG = re.compile(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+")

# Texto de 232 caracteres con #Muuki2026 en el 66% (dentro de 15-85%) y un
# token anterior seguro ("avances": ni articulo ni preposicion).
_TEXTO_232 = (
    "Hoy salimos a caminar por el barrio y escuchamos a la gente con calma. "
    "Recogimos sus ideas y las llevamos con respeto a cada colonia. "
    "Compartimos avances #Muuki2026 con las familias y seguimos "
    "trabajando por una comunidad mas unida."
)

# Contexto largo (>200 chars) para que el fallback local supere los 100 chars.
_CONTEXTO_LARGO = (
    "La comunidad organizada salio a las calles para presentar sus propuestas "
    "sobre seguridad, agua potable, alumbrado y espacios publicos; despues de "
    "varias reuniones vecinales acordaron mantener el dialogo con las "
    "autoridades y dar seguimiento a cada compromiso hasta que se cumpla."
)


def _chat_fijo(textos):
    """``_chat`` falso que responde un JSON con ``textos`` (sin red)."""

    def _fake(self, prompt, temperature=0.7):
        return json.dumps(list(textos), ensure_ascii=False)

    return _fake


def _chat_caido(self, prompt, temperature=0.7):
    """``_chat`` falso que SIEMPRE lanza (IA caida, sin red)."""
    raise RuntimeError("sin IA (test de limite local 280)")


def _tags(texto) -> list:
    """Hashtags del texto, en minusculas y sin duplicados, en orden."""
    vistos: list = []
    for m in _RE_TAG.finditer(str(texto or "")):
        clave = m.group(0).lower()
        if clave not in vistos:
            vistos.append(clave)
    return vistos


def _posicion_relativa(texto, tag) -> float:
    """Posicion del hashtag como fraccion del largo (0.0 - 1.0)."""
    t = str(texto or "")
    clave = str(tag or "").lower()
    pos = t.lower().find(clave)
    return (pos / len(t)) if t and pos >= 0 else -1.0


def _no_termina_en_hashtag(texto) -> bool:
    """True si el texto no cierra con un hashtag (regla del proyecto)."""
    limpio = re.sub(r"[\s.,;:!?)…\"']+$", "", str(texto or "").rstrip())
    return not re.search(r"#[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ_]+$", limpio)


def _cuentas_una() -> list:
    """Una cuenta neutral (sin registro/perfil) para aislar el limite."""
    return [{"usuario": "u1", "registro": "", "perfil": ""}]


def _primero(salida) -> str:
    """Primer texto de la salida ``{usuario: [textos]}``."""
    for fila in (salida or {}).values():
        for t in fila or []:
            return str(t or "")
    return ""


# --------------------------------------------------------------------------- #
# (a) Prompt con limite 100 (default, byte a byte) vs 280
# --------------------------------------------------------------------------- #
def test_prompt(check):
    print("(a) get_prompt_hashtags: 100 default intacto y 280 propio")

    p100 = get_prompt_hashtags(
        hashtags=_TAG, contexto="la comunidad", registro="", limite=100
    )
    check(
        "prompt 100: contiene REGLA_MAX_100 verbatim",
        REGLA_MAX_100 in p100,
        repr(REGLA_MAX_100[:40]),
    )
    check(
        "prompt 100: menciona '100 caracteres'",
        "100 caracteres" in p100,
    )

    p_default = get_prompt_hashtags(
        hashtags=_TAG, contexto="la comunidad", registro=""
    )
    check(
        "prompt default (sin kwarg) == prompt 100 byte a byte",
        p_default == p100,
        f"len default={len(p_default)} len 100={len(p100)}",
    )

    p280 = get_prompt_hashtags(
        hashtags=_TAG, contexto="la comunidad", registro="", limite=280
    )
    check("prompt 280: menciona 280", "280" in p280)
    check(
        "prompt 280: NO contiene la regla de 100",
        REGLA_MAX_100 not in p280,
    )
    check(
        "prompt 280: extension con '280 caracteres'",
        "280 caracteres" in p280,
    )
    check(
        "prompt 280: conserva la regla de hashtags en medio",
        "EN MEDIO" in p280 and "nunca al final" in p280,
    )

    p_basura = get_prompt_hashtags(
        hashtags=_TAG, contexto="la comunidad", registro="", limite="basura"
    )
    check(
        "prompt limite basura: cae al default de 100",
        REGLA_MAX_100 in p_basura and "100 caracteres" in p_basura,
    )

    p_alto = get_prompt_hashtags(
        hashtags=_TAG, contexto="la comunidad", registro="", limite=999
    )
    check(
        "prompt limite 999: se normaliza a 280",
        "280 caracteres" in p_alto and REGLA_MAX_100 not in p_alto,
    )

    sig = inspect.signature(get_prompt_hashtags)
    check(
        "firma: kwarg limite=100 AL FINAL",
        list(sig.parameters)[-1] == "limite"
        and sig.parameters["limite"].default == 100,
    )


# --------------------------------------------------------------------------- #
# (b) garantizar_texto_hashtag conserva 232 chars con limite 280
# --------------------------------------------------------------------------- #
def test_garantizar(check):
    print("(b) garantizar_texto_hashtag: 280 conserva, 100 recorta")
    check("texto de prueba ~232 chars", 150 < len(_TEXTO_232) < 280, repr(len(_TEXTO_232)))

    r280 = gc.garantizar_texto_hashtag(_TEXTO_232, [_TAG], limite=280)
    check(
        "280: conserva el texto completo (len > 150)",
        len(r280) > 150,
        repr(len(r280)),
    )
    check(
        "280: no agrega '...' del recorte",
        "…" not in r280,
    )
    frac = _posicion_relativa(r280, _TAG)
    check(
        "280: hashtag en la ventana 15-85%",
        0.15 <= frac <= 0.85,
        f"pos={frac:.2f}",
    )
    check("280: el texto no termina en hashtag", _no_termina_en_hashtag(r280))
    check(
        "280: validacion dura ok",
        gc.validar_texto_hashtag(r280, [_TAG], 280).get("ok") is True,
        repr(gc.validar_texto_hashtag(r280, [_TAG], 280)),
    )

    r100 = gc.garantizar_texto_hashtag(_TEXTO_232, [_TAG], limite=100)
    check("100: sigue <= 100", len(r100) <= 100, repr(len(r100)))
    check("100: conserva el hashtag pedido", _TAG.lower() in r100.lower())
    check("100: no termina en hashtag", _no_termina_en_hashtag(r100))
    check(
        "el hashtag no se pierde al pasar de 100 a 280",
        _TAG.lower() in r280.lower() and _TAG.lower() in r100.lower(),
    )


# --------------------------------------------------------------------------- #
# (c/d) generar_textos_hashtags_por_cuenta: IA larga y IA caida
# --------------------------------------------------------------------------- #
def test_generador(check):
    print("(c/d) generar_textos_hashtags_por_cuenta: 280 vs 100")

    cuentas = _cuentas_una()

    # IA simulada: devuelve el texto largo de 232 chars con el hashtag exacto.
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_fijo([_TEXTO_232])
    ):
        salida280 = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto="la comunidad", limite=280
        )
        salida100 = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto="la comunidad", limite=100
        )
        salida_default = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto="la comunidad"
        )

    t280 = _primero(salida280)
    t100 = _primero(salida100)
    tdef = _primero(salida_default)

    check("IA larga 280: devuelve un texto", bool(t280))
    check("IA larga 280: len <= 280", len(t280) <= 280, repr(len(t280)))
    check(
        "IA larga 280: len > 100 (material completo)",
        len(t280) > 100,
        repr(len(t280)),
    )
    check("IA larga 280: lleva el hashtag pedido", _TAG.lower() in t280.lower())
    check(
        "IA larga 280: SIN otros hashtags",
        _tags(t280) == [_TAG.lower()],
        repr(_tags(t280)),
    )
    frac = _posicion_relativa(t280, _TAG)
    check(
        "IA larga 280: hashtag en la ventana 15-85%",
        0.15 <= frac <= 0.85,
        f"pos={frac:.2f}",
    )
    check("IA larga 280: no termina en hashtag", _no_termina_en_hashtag(t280))

    check("IA larga 100: len <= 100", len(t100) <= 100, repr(len(t100)))
    check("IA larga 100: lleva el hashtag pedido", _TAG.lower() in t100.lower())
    check(
        "IA larga default: len <= 100 (no regresion)",
        len(tdef) <= 100,
        repr(len(tdef)),
    )
    check(
        "IA larga: el hashtag sobrevive de 100 a 280",
        _TAG.lower() in t100.lower() and _TAG.lower() in t280.lower(),
    )

    # IA caida: fallback local con contexto largo (>200 chars).
    with mock.patch.object(GeneradorContenido, "_chat", _chat_caido):
        fb280 = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto=_CONTEXTO_LARGO, limite=280
        )
        fb100 = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto=_CONTEXTO_LARGO, limite=100
        )
        fb_default = gc.generar_textos_hashtags_por_cuenta(
            cuentas, _TAG, contexto=_CONTEXTO_LARGO
        )

    f280 = _primero(fb280)
    f100 = _primero(fb100)
    fdef = _primero(fb_default)

    check("IA caida 280: devuelve un texto", bool(f280))
    check("IA caida 280: len <= 280", len(f280) <= 280, repr(len(f280)))
    check(
        "IA caida 280: len > 100 (contexto largo)",
        len(f280) > 100,
        repr(len(f280)),
    )
    check("IA caida 280: lleva el hashtag pedido", _TAG.lower() in f280.lower())
    check(
        "IA caida 280: SIN otros hashtags",
        _tags(f280) == [_TAG.lower()],
        repr(_tags(f280)),
    )
    check("IA caida 280: no termina en hashtag", _no_termina_en_hashtag(f280))
    check("IA caida 100: len <= 100", len(f100) <= 100, repr(len(f100)))
    check(
        "IA caida default: len <= 100 (no regresion)",
        len(fdef) <= 100,
        repr(len(fdef)),
    )

    sig = inspect.signature(gc.generar_textos_hashtags_por_cuenta)
    check(
        "firma generador: kwarg limite=100 AL FINAL",
        list(sig.parameters)[-1] == "limite"
        and sig.parameters["limite"].default == 100,
    )


# --------------------------------------------------------------------------- #
# (e) _texto_hashtag_local respeta el limite configurado
# --------------------------------------------------------------------------- #
def test_local(check):
    print("(e) _texto_hashtag_local con limite 280 y 100")
    ctx = _CONTEXTO_LARGO * 2  # >400 chars para forzar el recorte
    rng = random.Random(0)
    info = {"usuario": "u1", "registro": "", "perfil": ""}
    local280 = gc._texto_hashtag_local(
        info, ctx, [_TAG], 0, 0, rng, limite=280
    )
    check("local 280: len <= 280", len(local280) <= 280, repr(len(local280)))
    check("local 280: lleva el hashtag", _TAG.lower() in local280.lower())
    check("local 280: no termina en hashtag", _no_termina_en_hashtag(local280))

    local100 = gc._texto_hashtag_local(
        info, ctx, [_TAG], 0, 0, rng, limite=100
    )
    check("local 100: len <= 100", len(local100) <= 100, repr(len(local100)))
    check("local 100: lleva el hashtag", _TAG.lower() in local100.lower())

    sig = inspect.signature(gc._texto_hashtag_local)
    check(
        "firma _texto_hashtag_local: limite=100 AL FINAL",
        list(sig.parameters)[-1] == "limite"
        and sig.parameters["limite"].default == 100,
    )


def test_variaciones(check):
    print("(f) generar_variaciones_masivas / pool de citas con limite 280")
    from activaciones import variaciones as var

    prompt_capturado = {}

    def _chat_falso(self, prompt, temperature=0.9):
        prompt_capturado["texto"] = prompt
        return json.dumps([_TEXTO_232])

    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso
    ):
        v280 = GeneradorContenido().generar_variaciones_masivas(
            "Cita base sobre el festival", 1, limite=280
        )
    check(
        "variaciones 280: prompt pide 280 y no 100",
        "280" in prompt_capturado.get("texto", "")
        and REGLA_MAX_100 not in prompt_capturado.get("texto", ""),
    )
    check(
        "variaciones 280: conserva el texto largo (>100)",
        bool(v280) and len(v280[0]) > 100,
        repr(len(v280[0]) if v280 else 0),
    )
    check(
        "variaciones 280: no pierde el hashtag",
        bool(v280) and _TAG.lower() in v280[0].lower(),
    )

    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso
    ):
        v100 = GeneradorContenido().generar_variaciones_masivas(
            "Cita base sobre el festival", 1
        )
    check(
        "variaciones 100: prompt historico y texto <=100",
        REGLA_MAX_100 in prompt_capturado.get("texto", "")
        and bool(v100)
        and len(v100[0]) <= 100,
        repr(len(v100[0]) if v100 else 0),
    )

    sig = inspect.signature(
        GeneradorContenido.generar_variaciones_masivas
    )
    check(
        "firma variaciones: limite=100 AL FINAL",
        list(sig.parameters)[-1] == "limite"
        and sig.parameters["limite"].default == 100,
    )
    sig_pool = inspect.signature(var.generar_pool_variaciones_openai)
    check(
        "firma pool de citas: limite=100 AL FINAL",
        list(sig_pool.parameters)[-1] == "limite"
        and sig_pool.parameters["limite"].default == 100,
    )

    with mock.patch.object(
        GeneradorContenido,
        "generar_variaciones_masivas",
        return_value=[_TEXTO_232],
    ) as metodo:
        pool = var.generar_pool_variaciones_openai(
            "Cita base", 1, limite=280
        )
    check(
        "pool de citas: propaga limite=280 a la IA",
        metodo.called and metodo.call_args.kwargs.get("limite") == 280,
        str(metodo.call_args) if metodo.called else "no llamado",
    )
    check(
        "pool de citas: devuelve el texto largo del pool",
        bool(pool) and len(pool[0]) > 100,
    )


def run(check):
    """Ejecuta los checks con el ``check`` del runner (o del marco local)."""
    test_prompt(check)
    test_garantizar(check)
    test_generador(check)
    test_local(check)
    test_variaciones(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_limite_local_280.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
