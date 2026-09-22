"""Tests rapidos del limite ESTRICTO de 100 caracteres para POSTS y RETWEETS
CON CITA generados por IA (sin red, sin OpenAI real y sin Chrome).

Cubre el requerimiento del dueño: TODO post/cita generado por IA debe tener un
maximo estricto de 100 caracteres, tanto en el prompt (``REGLA_MAX_100`` de
``ia/prompts.py``) como con un corte duro de ultima barrera en
``ia/generador_contenido.py`` (``_acotar_limite``, por si la IA se pasa).

  (a) ``REGLA_MAX_100`` aparece VERBATIM en los prompts de posts/citas
      (``_reglas_registro`` con y sin registro, ``get_prompt_hashtags`` y el
      prompt de la cita capturado al monkeypatchear ``GeneradorContenido._chat``).
  (b) ``_MAX_LARGO_HASHTAG == 100`` y ``get_prompt_hashtags`` ya no pide 240.
  (c) ``_recortar_limite_hashtag(..., limite=100)``: <=100, preserva hashtags,
      no termina en hashtag, no parte palabras e idempotente.
  (d) ``generar_variaciones_masivas`` (citas): IA larga y fallback local <=100.
  (e) ``generar_pool_por_cuenta``: rama clasica y con registros/perfiles <=100.
  (f) ``generar_textos_mantenimiento``: posts <=100 y comentarios SIN forzar
      (los comentarios quedan fuera del alcance).
  (g) Extras: comentarios/blog sin ``REGLA_MAX_100`` y lote de mantenimiento
      (post con regla, comentario sin regla).
  (h) ``generar_contenido`` (pestana "Contenido IA"): posts estandar <=100 y
      formatos especiales (blog/verificado) con su limite propio intacto.
  (i) Contratos de conteo con colisiones forzadas (``_variar_hasta_unico``
      devuelve el mismo texto): ``generar_variaciones_masivas`` y
      ``generar_pool_por_cuenta`` devuelven EXACTAMENTE lo pedido y <=100.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_limite_100.py   (solo este archivo)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import ia.generador_contenido as gc  # noqa: E402
import ia.prompts as prompts  # noqa: E402
from ia.generador_contenido import GeneradorContenido  # noqa: E402
from ia.prompts import REGLA_MAX_100  # noqa: E402

# Texto EXACTO pedido (verbatim, con acentos y "SÉ").
_ESPERADA = (
    "REGLA DE ORO: EL TEXTO FINAL DEBE TENER UN MÁXIMO ESTRICTO DE 100 "
    "CARACTERES EN TOTAL, INCLUYENDO HASHTAGS Y ESPACIOS. SÉ MUY BREVE Y "
    "DIRECTO."
)

# Texto largo (>400 chars) con hashtags para probar el recorte a 100.
_TEXTO_400 = (
    "Hoy salimos a la calle con la frente en alto y la conviccion intacta. "
    "Caminamos juntos por el barrio escuchando a la gente y sumando voces. "
    "Sabemos que el camino es largo pero nadie suelta la causa ni un momento. "
    "Porque el orgullo #mexico se defiende con hechos y no con discursos. "
    "Y el futbol #futbol nos recuerda que en equipo todo se puede lograr. "
    "Seguimos adelante con la conviccion de que las cosas buenas llegan."
)


def _chat_falso(textos, capturas=None):
    """Devuelve un ``_chat`` falso que responde un JSON con ``textos``.

    ``capturas`` (lista opcional) guarda cada prompt recibido para poder
    verificar su contenido sin red.
    """

    def _fake(self, prompt, temperature=0.7):
        if capturas is not None:
            capturas.append(prompt)
        return json.dumps(list(textos), ensure_ascii=False)

    return _fake


def _texto_largo(n: int = 300, cola: str = "#mexico") -> str:
    """Texto de ~``n`` caracteres terminado en un hashtag (para simular IA)."""
    relleno = (
        "Seguimos trabajando por la comunidad con informacion clara. "
        * (n // 58 + 2)
    )
    corte = relleno[: max(1, n - len(cola) - 1)]
    if " " in corte:
        corte = corte.rsplit(" ", 1)[0]
    return f"{corte} {cola}".strip()


def _ultima_palabra(texto: str) -> str:
    """Ultima palabra del texto, sin puntuacion ni puntos suspensivos."""
    limpio = texto.strip().rstrip("…").strip()
    limpio = re.sub(r"[\s.,;:!?\"')\]]+$", "", limpio)
    return limpio.split()[-1] if limpio else ""


def _palabra_en_original(palabra: str, original: str) -> bool:
    """True si ``palabra`` aparece completa (frontera de palabra) en original."""
    if not palabra:
        return False
    patron = (
        r"(?<![\wÁÉÍÓÚÜÑáéíóúüñ])"
        + re.escape(palabra)
        + r"(?![\wÁÉÍÓÚÜÑáéíóúüñ])"
    )
    return re.search(patron, original) is not None


# --------------------------------------------------------------------------- #
# (a/b) La regla de oro en los prompts (posts/citas) y fuera de comentarios
# --------------------------------------------------------------------------- #
def test_regla_en_prompts(check):
    print("(a/b) REGLA_MAX_100 verbatim en prompts de posts/citas")
    check("regla: constante exportada con el texto exacto", REGLA_MAX_100 == _ESPERADA, repr(REGLA_MAX_100[:60]))

    check(
        "regla: _reglas_registro('politica') la incluye",
        REGLA_MAX_100 in prompts._reglas_registro("politica"),
    )
    check(
        "regla: _reglas_registro('') la incluye (registro vacio)",
        REGLA_MAX_100 in prompts._reglas_registro(""),
    )
    check(
        "regla: _reglas_registro('ciudadana') la incluye",
        REGLA_MAX_100 in prompts._reglas_registro("ciudadana"),
    )
    check(
        "regla: _reglas_registro('desconocido') la incluye",
        REGLA_MAX_100 in prompts._reglas_registro("registro-invalido"),
    )

    prompt_hashtags = prompts.get_prompt_hashtags(
        hashtags="#mexico #futbol", contexto="el futbol", cantidad=2
    )
    check(
        "regla: get_prompt_hashtags la incluye",
        REGLA_MAX_100 in prompt_hashtags,
    )
    check(
        "regla: get_prompt_hashtags ya NO pide 240",
        "240" not in prompt_hashtags,
    )
    check(
        "regla: get_prompt_hashtags exige maximo 100",
        "100" in prompt_hashtags and "MÁXIMO ESTRICTO" in prompt_hashtags,
    )

    # Prompt de la CITA capturado al monkeypatchear `_chat`.
    capturas = []
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso(["Texto corto uno", "Texto corto dos"], capturas)
    ):
        GeneradorContenido().generar_variaciones_masivas(
            "Cita base", 2, perfil="formal", registro="politica"
        )
    unido = "\n".join(capturas)
    check("regla: prompt de cita la incluye (capturado)", REGLA_MAX_100 in unido)
    check("regla: prompt de cita pide maximo 100", "100" in unido and "240" not in unido)

    # Contrato de formatos: el default de `_reglas_formato` es 100.
    formato = prompts._reglas_formato()
    check(
        "formato: _reglas_formato default = 100 (ya no 240)",
        "100 caracteres" in formato and "240" not in formato,
    )

    # Fuera del alcance: comentarios y blog NO reciben la regla de 100.
    prompt_comentario = prompts.get_prompt_comentario(
        "4T", registro="politica", cantidad=2
    )
    check(
        "excluido: get_prompt_comentario NO recibe la regla de 100",
        REGLA_MAX_100 not in prompt_comentario,
    )
    prompt_blog = prompts._prompt_blog("4T", registro="politica", cantidad=1)
    check(
        "excluido: _prompt_blog NO recibe la regla de 100",
        REGLA_MAX_100 not in prompt_blog,
    )
    check(
        "excluido: _prompt_blog conserva su maximo de 600",
        "600" in prompt_blog,
    )

    # Lote de mantenimiento (A.7): post con regla, comentario sin regla.
    cuentas_post = [
        {"usuario": "u_post", "registro": "politica", "perfil": "formal", "tipo_accion": "post"}
    ]
    cuentas_com = [
        {"usuario": "u_com", "registro": "politica", "perfil": "formal", "tipo_accion": "comentario"}
    ]
    prompt_lote_post = gc._prompt_lote_mantenimiento([(0, 0, "dia")], cuentas_post)
    prompt_lote_com = gc._prompt_lote_mantenimiento([(0, 0, "dia")], cuentas_com)
    check(
        "lote: el POST recibe la regla de 100",
        REGLA_MAX_100 in prompt_lote_post and "100 caracteres" in prompt_lote_post,
    )
    check(
        "lote: el COMENTARIO no recibe la regla de 100",
        REGLA_MAX_100 not in prompt_lote_com,
    )


# --------------------------------------------------------------------------- #
# (c) Corte duro directo: `_recortar_limite_hashtag` y `_acotar_limite`
# --------------------------------------------------------------------------- #
def test_recorte_100(check):
    print("(c) recorte duro a 100 sin partir palabras y con hashtags")
    check(
        "recorte: el texto de prueba supera 400 chars",
        len(_TEXTO_400) > 400,
        f"len={len(_TEXTO_400)}",
    )
    check(
        "recorte: _MAX_LARGO_HASHTAG == 100",
        gc._MAX_LARGO_HASHTAG == 100,
        repr(gc._MAX_LARGO_HASHTAG),
    )

    resultado = gc._recortar_limite_hashtag(_TEXTO_400, ["#mexico", "#futbol"], 100)
    check("recorte: <= 100", len(resultado) <= 100, f"len={len(resultado)}")
    check(
        "recorte: conserva #mexico",
        "#mexico" in resultado.lower(),
        repr(resultado),
    )
    check(
        "recorte: conserva #futbol",
        "#futbol" in resultado.lower(),
        repr(resultado),
    )
    check(
        "recorte: no termina en hashtag",
        not gc._hashtag_al_final(resultado),
        repr(resultado),
    )
    ultima = _ultima_palabra(resultado)
    check(
        "recorte: la ultima palabra existe completa en el original",
        _palabra_en_original(ultima, _TEXTO_400),
        f"ultima={ultima!r}",
    )
    segunda = gc._recortar_limite_hashtag(resultado, ["#mexico", "#futbol"], 100)
    check("recorte: idempotente (2a pasada no lo cambia)", segunda == resultado, repr(segunda))

    acotado = gc._acotar_limite(_TEXTO_400, 100)
    check("acotar: <= 100", len(acotado) <= 100, f"len={len(acotado)}")
    check(
        "acotar: detecta y conserva los hashtags del texto",
        "#mexico" in acotado.lower() and "#futbol" in acotado.lower(),
        repr(acotado),
    )
    check(
        "acotar: no termina en hashtag",
        not gc._hashtag_al_final(acotado),
        repr(acotado),
    )
    check(
        "acotar: texto corto queda intacto",
        gc._acotar_limite("Texto corto #mexico", 100) == "Texto corto #mexico",
    )
    check(
        "acotar: nunca lanza con entradas raras",
        gc._acotar_limite(None) == "" and gc._acotar_limite("") == "",
    )


# --------------------------------------------------------------------------- #
# (d) Variaciones de cita: IA larga y fallback local
# --------------------------------------------------------------------------- #
def test_variaciones_100(check):
    print("(d) generar_variaciones_masivas (citas) <= 100")
    largos = [
        _texto_largo(320, "#mexico"),
        _texto_largo(310, "#futbol"),
        _texto_largo(300, "#Mexico"),
    ]
    capturas = []
    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso(largos, capturas)
    ):
        textos = GeneradorContenido().generar_variaciones_masivas(
            "Apoyemos el deporte", 3, registro="politica", perfil="formal"
        )
    check("variaciones IA: devuelve 3 textos", len(textos) == 3, repr(len(textos)))
    check(
        "variaciones IA: TODOS <= 100",
        all(len(t) <= 100 for t in textos),
        repr([len(t) for t in textos]),
    )
    check(
        "variaciones IA: unicos",
        len(set(textos)) == len(textos),
        repr(textos),
    )
    check(
        "variaciones IA: con hashtag presente",
        all(gc.tiene_hashtag(t) for t in textos),
        repr(textos),
    )
    check(
        "variaciones IA: sin hashtag al final",
        all(not gc._hashtag_al_final(t) for t in textos),
        repr(textos),
    )

    # La IA "se pasa" y falla: el relleno local tambien debe salir <=100.
    try:
        with mock.patch.object(
            GeneradorContenido, "_chat", side_effect=RuntimeError("sin IA (test)")
        ):
            fallback = GeneradorContenido().generar_variaciones_masivas(
                "Apoyemos el deporte", 3, registro="politica", perfil="formal"
            )
        error = None
    except Exception as e:  # noqa: BLE001
        error = e
        fallback = []
    check("variaciones fallback: no lanza", error is None, repr(error))
    check("variaciones fallback: devuelve 3 textos", len(fallback) == 3, repr(len(fallback)))
    check(
        "variaciones fallback: TODOS <= 100",
        all(len(t) <= 100 for t in fallback),
        repr([len(t) for t in fallback]),
    )
    check(
        "variaciones fallback: con hashtag y sin hashtag al final",
        all(gc.tiene_hashtag(t) for t in fallback)
        and all(not gc._hashtag_al_final(t) for t in fallback),
        repr(fallback),
    )


# --------------------------------------------------------------------------- #
# (e) Pool por cuenta: rama clasica y con registros/perfiles
# --------------------------------------------------------------------------- #
def test_pool_100(check):
    print("(e) generar_pool_por_cuenta <= 100 en ambas ramas")
    largos = [
        _texto_largo(330, "#mexico"),
        _texto_largo(320, "#futbol"),
        _texto_largo(315, "#comunidad"),
        _texto_largo(305, "#Mexico"),
    ]

    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso(largos)):
        pool = gc.generar_pool_por_cuenta("Apoyemos el deporte", 4)
    check("pool clasico: 4 textos", len(pool) == 4, repr(len(pool)))
    check(
        "pool clasico: TODOS <= 100",
        all(len(t) <= 100 for t in pool),
        repr([len(t) for t in pool]),
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso(largos)):
        pool_estilos = gc.generar_pool_por_cuenta(
            "Apoyemos el deporte",
            4,
            registros=["politica", "activista", "ciudadana", ""],
            perfiles=["formal", "ciudadano", "popular", "formal"],
        )
    check("pool estilos: 4 textos", len(pool_estilos) == 4, repr(len(pool_estilos)))
    check(
        "pool estilos: TODOS <= 100",
        all(len(t) <= 100 for t in pool_estilos),
        repr([len(t) for t in pool_estilos]),
    )


# --------------------------------------------------------------------------- #
# (f) Mantenimiento: posts <=100; comentarios intactos
# --------------------------------------------------------------------------- #
def test_mantenimiento_100(check):
    print("(f) generar_textos_mantenimiento: posts <= 100, comentarios intactos")
    largo_post = _texto_largo(300, "#mexico")
    cuentas_post = [
        {
            "usuario": "u_post",
            "registro": "politica",
            "perfil": "formal",
            "tipo_accion": "post",
        }
    ]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso([largo_post])):
        resultado_post = gc.generar_textos_mantenimiento(cuentas_post, 1, temas=["dia"])
    textos_post = resultado_post[0] if resultado_post else []
    check("mantenimiento post: 1 texto", len(textos_post) == 1, repr(len(textos_post)))
    check(
        "mantenimiento post: TODOS <= 100",
        bool(textos_post) and all(len(t) <= 100 for t in textos_post),
        repr([len(t) for t in textos_post]),
    )
    check(
        "mantenimiento post: la IA larga fue cortada por el corte duro",
        bool(textos_post) and len(textos_post[0]) <= 100,
        repr(textos_post[0][:120] if textos_post else ""),
    )

    largo_com = (
        "Este es un comentario muy largo que debe quedar intacto sin recortar "
        "porque los comentarios estan fuera del alcance del limite de cien "
        "caracteres y no deben modificarse. "
    ) * 3
    cuentas_com = [
        {
            "usuario": "u_com",
            "registro": "politica",
            "perfil": "formal",
            "tipo_accion": "comentario",
        }
    ]
    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso([largo_com.strip()])):
        resultado_com = gc.generar_textos_mantenimiento(cuentas_com, 1, temas=["dia"])
    textos_com = resultado_com[0] if resultado_com else []
    check(
        "mantenimiento comentario: 1 texto",
        len(textos_com) == 1,
        repr(len(textos_com)),
    )
    check(
        "mantenimiento comentario: NO se fuerza a 100 (sigue >100)",
        bool(textos_com) and any(len(t) > 100 for t in textos_com),
        repr([len(t) for t in textos_com]),
    )
    check(
        "mantenimiento comentario: sin hashtags/links/@ (spam-safe)",
        bool(textos_com)
        and all("#" not in t and "http" not in t.lower() and "@" not in t for t in textos_com),
        repr(textos_com),
    )


def test_generar_contenido_100(check):
    print("(h) generar_contenido: posts estandar <= 100 y especiales intactos")
    largos = [_texto_largo(320, "#mexico"), _texto_largo(310, "#futbol")]

    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso(largos)):
        textos = GeneradorContenido().generar_contenido(
            "mantenimiento", "4T", cantidad=2
        )
    check(
        "generar_contenido mantenimiento: 2 textos",
        len(textos) == 2,
        repr(len(textos)),
    )
    check(
        "generar_contenido mantenimiento: TODOS <= 100 (corte duro)",
        len(textos) == 2 and all(len(t) <= 100 for t in textos),
        repr([len(t) for t in textos]),
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso(largos)):
        blog = GeneradorContenido().generar_contenido("blog", "4T", cantidad=2)
    check(
        "generar_contenido blog: NO se acota a 100 (limite propio 600)",
        len(blog) == 2 and any(len(t) > 100 for t in blog),
        repr([len(t) for t in blog]),
    )

    with mock.patch.object(GeneradorContenido, "_chat", _chat_falso(largos)):
        verificado = GeneradorContenido().generar_contenido(
            "verificado", "4T", contexto="http://ejemplo.test", cantidad=2
        )
    check(
        "generar_contenido verificado: NO se acota a 100 (limite propio 240)",
        len(verificado) == 2 and any(len(t) > 100 for t in verificado),
        repr([len(t) for t in verificado]),
    )


def test_contratos_conteo_100(check):
    print("(i) contratos de conteo con colisiones forzadas")
    # Textos DISTINTOS que despues del corte a 100 colapsan al MISMO texto
    # (la primera frase termina antes del limite y lo demas se recorta).
    comun = "Seguimos trabajando por la comunidad con informacion clara. " * 3
    colapsables = [
        comun + "version uno distinta",
        comun + "version dos distinta",
        comun + "version tres distinta",
    ]

    def _varia_igual(texto, vistos, intentos=8):
        """Forzado: la variacion SIEMPRE devuelve el mismo texto (colision)."""
        return texto

    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso(colapsables)
    ), mock.patch.object(gc, "_variar_hasta_unico", _varia_igual):
        variaciones = GeneradorContenido().generar_variaciones_masivas(
            "Apoyemos el deporte", 3
        )
    check(
        "variaciones colisionadas: EXACTAMENTE 3 (no descarta ninguna)",
        len(variaciones) == 3,
        repr(len(variaciones)),
    )
    check(
        "variaciones colisionadas: TODOS <= 100",
        all(len(t) <= 100 for t in variaciones),
        repr([len(t) for t in variaciones]),
    )

    with mock.patch.object(
        GeneradorContenido, "_chat", _chat_falso(colapsables)
    ), mock.patch.object(gc, "_variar_hasta_unico", _varia_igual):
        pool = gc.generar_pool_por_cuenta("Apoyemos el deporte", 3)
    check(
        "pool clasico colisionado: EXACTAMENTE 3",
        len(pool) == 3,
        repr(len(pool)),
    )
    check(
        "pool clasico colisionado: TODOS <= 100",
        all(len(t) <= 100 for t in pool),
        repr([len(t) for t in pool]),
    )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_regla_en_prompts(check)
    test_recorte_100(check)
    test_variaciones_100(check)
    test_pool_100(check)
    test_mantenimiento_100(check)
    test_generar_contenido_100(check)
    test_contratos_conteo_100(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_limite_100.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
