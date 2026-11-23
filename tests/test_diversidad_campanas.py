# -*- coding: utf-8 -*-
"""Integracion de la diversidad de textos en las campanas (sin Chrome/red).

Verifica el pedido del dueno: cada publicacion (post o RT con cita) debe salir
con un texto DINAMICO y MUY DISTINTO, incluso cuando la IA falla. Cubre:

  (a) `MotorActivacion._asignar_variaciones_cita`: 12 cuentas con IA caida ->
      sin texto igual al base, <=100 chars, hashtags pedidos en medio y pares
      por debajo del umbral de similitud de citas.
  (b) Memoria anti-repeticion entre llamadas (`evitar`).
  (c) `activaciones.local._generar_citas`: 10 cuentas, limite 280, sin
      repeticiones ni textos casi iguales.
  (d) `activaciones.local._generar_posts`: 5 cuentas x 3 textos con IA caida.
  (e) Helpers de memoria/refresh y cableado del refresh de primera entrada.

Sin dependencias nuevas. No escribe en `data/` ni abre navegador.
"""
from __future__ import annotations

import inspect
import re
from unittest import mock

from activaciones import local as L
from activaciones.motor import (
    MotorActivacion,
    _acotar_cita_100,
    _es_texto_diverso,
    _recordar_textos,
)
from ia.generador_contenido import (
    DIVERSIDAD_UMBRAL_CITA,
    DIVERSIDAD_UMBRAL_POST,
    normalizar_para_comparar,
    similitud_textos,
)

BASE = (
    "Profesor, le ofrezco una disculpa. Me retuvieron en una junta de trabajo "
    "en la oficina en Santa Fe y me va a ser imposible llegar a las 6 pm al "
    "auditorio de ESCOM. ¿Cree que habría alguna posibilidad de tener alguna "
    "facilidad por el día de hoy? Le adjunto una foto de mi oficina para "
    "justificar la situación. Agradezco mucho su comprensión."
)

TAGS = ["#mexico", "#futbol"]


class _CuentaFake:
    """Cuenta minima para el motor (atributos que usa _asignar_variaciones)."""

    def __init__(self, usuario, registro="", perfil=""):
        self.usuario = usuario
        self.tipo_cuenta = registro
        self.perfil_personalidad = perfil
        self.personalidad = ""
        self.seccion = ""
        self.nombre_mostrado = usuario


def _cuentas(n=12):
    registros = ["politica", "activista", "ciudadana"]
    perfiles = ["formal", "ciudadano", "popular"]
    return [
        _CuentaFake(
            f"cuenta_{i}",
            registros[i % len(registros)],
            perfiles[i % len(perfiles)],
        )
        for i in range(n)
    ]


def _pares(textos):
    valores = []
    for i in range(len(textos)):
        for j in range(i + 1, len(textos)):
            valores.append(similitud_textos(textos[i], textos[j]))
    return valores


def _sin_ia():
    return mock.patch(
        "ia.generador_contenido.GeneradorContenido._chat",
        side_effect=RuntimeError("sin IA (test)"),
    )


def _no_termina_en_tag(texto) -> bool:
    limpio = str(texto or "").rstrip()
    return not any(limpio.lower().endswith(t.lower()) for t in TAGS)


# --------------------------------------------------------------------------- #
# (a) Motor: citas sin IA
# --------------------------------------------------------------------------- #
def test_motor_citas(check):
    print("(a) Motor _asignar_variaciones_cita con IA caida")
    motor = MotorActivacion()
    with _sin_ia():
        asignadas = motor._asignar_variaciones_cita(
            _cuentas(12), BASE, tags=TAGS
        )
    textos = list(asignadas.values())
    check("citas: 12 textos asignados", len(textos) == 12, repr(len(textos)))
    check(
        "citas: todos unicos",
        len(set(textos)) == len(textos),
        repr(len(set(textos))),
    )
    check(
        "citas: ninguno es el base literal",
        all(
            normalizar_para_comparar(t) != normalizar_para_comparar(BASE)
            for t in textos
        ),
    )
    check(
        "citas: todos <= 100 chars",
        all(len(t) <= 100 for t in textos),
        repr([len(t) for t in textos]),
    )
    pares = _pares(textos)
    bajo = sum(v < DIVERSIDAD_UMBRAL_CITA for v in pares)
    check(
        "citas: TODOS los pares bajo el umbral de citas",
        bajo == len(pares),
        f"{bajo}/{len(pares)} max={max(pares):.3f}",
    )
    check(
        "citas: todos llevan al menos un hashtag pedido",
        all(
            any(t.lower().find(tag.lower()) >= 0 for tag in TAGS)
            for t in textos
        ),
    )
    check(
        "citas: ninguno termina en hashtag",
        all(_no_termina_en_tag(t) for t in textos),
    )
    pedidos = {tag.lower() for tag in TAGS}
    check(
        "citas: no aparecen hashtags ajenos",
        all(
            all(
                tag.lower() in pedidos
                for tag in re.findall(r"#[A-Za-z0-9_]+", t)
            )
            for t in textos
        ),
        repr([
            re.findall(r"#[A-Za-z0-9_]+", t) for t in textos
        ]),
    )


# --------------------------------------------------------------------------- #
# (b) Memoria anti-repeticion entre llamadas
# --------------------------------------------------------------------------- #
def test_memoria_evitar(check):
    print("(b) evitar: no reutiliza textos de llamadas previas")
    motor = MotorActivacion()
    with _sin_ia():
        primera = motor._asignar_variaciones_cita(
            _cuentas(6), BASE, tags=TAGS
        )
        segunda = motor._asignar_variaciones_cita(
            _cuentas(6), BASE, tags=TAGS, evitar=list(primera.values())
        )
    previos = list(primera.values())
    nuevos = list(segunda.values())
    check("evitar: segunda llamada devuelve 6", len(nuevos) == 6)
    check(
        "evitar: ningun texto nuevo es igual a uno previo",
        all(t not in previos for t in nuevos),
    )
    max_sim = max(
        (similitud_textos(a, b) for a in nuevos for b in previos),
        default=0.0,
    )
    check(
        "evitar: ningun texto nuevo es similar a los previos",
        max_sim < DIVERSIDAD_UMBRAL_CITA,
        f"max={max_sim:.3f}",
    )
    check(
        "evitar: los nuevos tambien son distintos entre si",
        all(v < DIVERSIDAD_UMBRAL_CITA for v in _pares(nuevos)),
        repr([round(v, 3) for v in _pares(nuevos)]),
    )


# --------------------------------------------------------------------------- #
# (c) Campana LOCAL: citas (limite 280)
# --------------------------------------------------------------------------- #
def test_local_citas(check):
    print("(c) Local _generar_citas con IA caida (limite 280)")
    plan = []
    registros = ["politica", "activista", "ciudadana"]
    perfiles = ["formal", "ciudadano", "popular"]
    for i in range(10):
        plan.append(
            {
                "rol": "cita",
                "usuario": f"local_{i}",
                "cuenta": {
                    "usuario": f"local_{i}",
                    "tipo_cuenta": registros[i % 3],
                    "perfil_personalidad": perfiles[i % 3],
                },
            }
        )
    with _sin_ia():
        L._generar_citas(plan, BASE, "", " ".join(TAGS))
    textos = [p.get("texto") or "" for p in plan]
    check("local citas: 10 textos", len(textos) == 10, repr(len(textos)))
    check(
        "local citas: todos unicos",
        len(set(textos)) == len(textos),
        repr(len(set(textos))),
    )
    check(
        "local citas: ninguno vacio",
        all(t.strip() for t in textos),
    )
    check(
        "local citas: ninguno es el base literal",
        all(
            normalizar_para_comparar(t) != normalizar_para_comparar(BASE)
            for t in textos
        ),
    )
    check(
        "local citas: todos <= 280 chars",
        all(len(t) <= 280 for t in textos),
        repr([len(t) for t in textos]),
    )
    pares = _pares(textos)
    check(
        "local citas: TODOS los pares bajo el umbral de citas",
        all(v < DIVERSIDAD_UMBRAL_CITA for v in pares),
        f"max={max(pares):.3f}",
    )
    check(
        "local citas: llevan hashtag pedido",
        all(any(tag.lower() in t.lower() for tag in TAGS) for t in textos),
    )
    check(
        "local citas: ninguno termina en hashtag",
        all(_no_termina_en_tag(t) for t in textos),
    )


# --------------------------------------------------------------------------- #
# (d) Campana LOCAL: posts (3 por cuenta)
# --------------------------------------------------------------------------- #
def test_local_posts(check):
    print("(d) Local _generar_posts con IA caida (5 cuentas x 3)")
    selected = []
    registros = ["politica", "activista", "ciudadana"]
    perfiles = ["formal", "ciudadano", "popular"]
    for i in range(5):
        selected.append(
            {
                "usuario": f"post_{i}",
                "tipo_cuenta": registros[i % 3],
                "perfil_personalidad": perfiles[i % 3],
                "personalidad": "",
                "seccion": "",
                "nombre_mostrado": f"post_{i}",
            }
        )
    with _sin_ia():
        posts = L._generar_posts(
            selected, " ".join(TAGS), BASE, "", 3
        )
    check("local posts: 5 cuentas", len(posts) == 5, repr(list(posts)))
    check(
        "local posts: exactamente 3 por cuenta",
        all(len(v) == 3 for v in posts.values()),
        repr([len(v) for v in posts.values()]),
    )
    textos = [t for lista in posts.values() for t in lista]
    check("local posts: 15 textos en total", len(textos) == 15)
    check(
        "local posts: todos unicos",
        len(set(textos)) == len(textos),
        repr(len(set(textos))),
    )
    check(
        "local posts: todos <= 280 chars",
        all(len(t) <= 280 for t in textos),
        repr([len(t) for t in textos]),
    )
    check(
        "local posts: ninguno es el base literal",
        all(
            normalizar_para_comparar(t) != normalizar_para_comparar(BASE)
            for t in textos
        ),
    )
    pares = _pares(textos)
    bajo = sum(v < DIVERSIDAD_UMBRAL_POST for v in pares)
    check(
        "local posts: al menos el 90% de pares bajo el umbral de posts",
        bajo >= int(len(pares) * 0.90),
        f"{bajo}/{len(pares)} max={max(pares):.3f}",
    )
    check(
        "local posts: llevan hashtag pedido",
        all(any(tag.lower() in t.lower() for tag in TAGS) for t in textos),
    )
    check(
        "local posts: ninguno termina en hashtag",
        all(_no_termina_en_tag(t) for t in textos),
    )
    check(
        "local posts: no copian el contexto completo",
        all(
            normalizar_para_comparar(t) != normalizar_para_comparar(BASE)
            for t in textos
        ),
    )


# --------------------------------------------------------------------------- #
# (e) Helpers y cableado del refresh de primera entrada
# --------------------------------------------------------------------------- #
def test_helpers(check):
    print("(e) helpers de diversidad y refresh")
    check(
        "helper: texto distinto True con textos claramente distintos",
        _es_texto_diverso(
            "El clima de hoy está fresco y agradable.",
            ["Ayer llovió toda la tarde en la ciudad."],
        ),
    )
    check(
        "helper: texto distinto False con casi el mismo texto",
        not _es_texto_diverso(
            "Profesor, le ofrezco una disculpa por no llegar.",
            ["Profesor, le ofrezco una disculpa por no llegar hoy."],
        ),
    )
    memoria = ["uno", "dos"]
    _recordar_textos(memoria, ["dos", "tres", "", None, "tres"])
    check(
        "memoria: deduplica y agrega solo nuevos",
        memoria == ["uno", "dos", "tres"],
        repr(memoria),
    )
    _recordar_textos(memoria, [f"t{i}" for i in range(10)], maximo=5)
    check(
        "memoria: respeta el tope maximo",
        len(memoria) == 5 and memoria[-1] == "t9",
        repr(memoria),
    )
    acotado = _acotar_cita_100(BASE, TAGS)
    check(
        "acotar cita: <=100 con hashtags y sin terminar en tag",
        len(acotado) <= 100 and _no_termina_en_tag(acotado),
        repr(acotado),
    )

    llamadas = []

    class _BotFake:
        def refrescar_pagina_rapido(self):
            llamadas.append("refresh")
            return "ok"

    L._refrescar_entrada(_BotFake())
    check("refresh entrada: llama al metodo UNA vez", llamadas == ["refresh"])
    L._refrescar_entrada(object())  # sin metodo: no lanza
    check("refresh entrada: tolera bot viejo sin el metodo", llamadas == ["refresh"])

    fuente = inspect.getsource(L)
    sin_definicion = fuente.replace("def _refrescar_entrada(bot) -> None:", "")
    check(
        "local: el flujo NO llama _refrescar_entrada (solo queda el helper)",
        "_refrescar_entrada(bot)" not in sin_definicion,
    )
    check(
        "local: _ENV_LOCAL fuerza REFRESH_AL_ENTRAR=0 y REFRESH_ENTRADA_MAX=0",
        L._ENV_LOCAL.get("REFRESH_AL_ENTRAR") == "0"
        and L._ENV_LOCAL.get("REFRESH_ENTRADA_MAX") == "0",
        f"({L._ENV_LOCAL.get('REFRESH_AL_ENTRAR')}, "
        f"{L._ENV_LOCAL.get('REFRESH_ENTRADA_MAX')})",
    )
    check(
        "local: _generar_citas pasa evitar a la IA si lo soporta",
        "_acepta_kwarg(funcion, \"evitar\")" in inspect.getsource(L._generar_citas),
    )


def run(check):
    test_motor_citas(check)
    test_memoria_evitar(check)
    test_local_citas(check)
    test_local_posts(check)
    test_helpers(check)
