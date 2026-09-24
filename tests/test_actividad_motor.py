# -*- coding: utf-8 -*-
"""Tests del modo ACTIVIDAD (leve) del motor: 3-4 posts por cuenta, TODOS los
hashtags, pausas entre posts de la MISMA cuenta y tope de duracion.

Sin red, sin Chrome y sin tocar la base real (fakes + reloj falso + parches,
mismo patron que `tests/test_pausas_motor.py`/`test_cuotas_diarias_motor.py`).

Cubre:
    (1) E2E 3 cuentas con N fijo 3 -> 9 posts exactos, pausas entre posts de la
        misma cuenta (reloj falso) y URLs capturadas por post.
    (2) N aleatorio en [3, 4] por cuenta (200 iteraciones, nunca fuera de rango).
    (3) Pausada de 4 -> omitida con contadores; `permitir_pausadas=True` la deja
        participar.
    (4) Tier 2/Tier 3 -> omitidas sin navegador; Tier 1/sin tier ejecutan.
    (5) Cuota horaria agotada a mitad -> solo los posts sin cupo se omiten.
    (6) Cancelacion (Event) a mitad -> cancelada=True y resumen consistente.
    (7) Una cuenta que falla no tumba a las demas; sesion caida omite el resto.
    (8) `duracion_max_min` chico -> `omitidas_por_tiempo`.
    (9) Sin hashtags -> `error` claro sin abrir navegador.
    (10) Generacion: contrato nuevo -> fallback a hashtags -> pool local (TODOS
        los hashtags garantizados).
    (11) Firma CONGELADA de `ejecutar_actividad` + helpers de rango/browsers.
    (12) `usuarios=None` -> todas las activas; sin sesion -> `sin_sesion`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_actividad_motor.py
"""
from __future__ import annotations

import contextlib
import inspect
import sys
import threading
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))
TESTS = RAIZ / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
import ia.generador_contenido as ia_mod  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _parches(*cambios):
    """Aplica `(objeto, nombre, valor)` y restaura SIEMPRE al salir."""
    originales = []
    try:
        for objeto, nombre, valor in cambios:
            originales.append((objeto, nombre, getattr(objeto, nombre)))
            setattr(objeto, nombre, valor)
        yield
    finally:
        for objeto, nombre, valor in reversed(originales):
            setattr(objeto, nombre, valor)


class _CuentaFake:
    """Cuenta minima con los atributos que lee el motor (sin BD)."""

    def __init__(self, usuario, tier_calidad="", pausada=False, sesion=True,
                 **extra):
        self.usuario = usuario
        self.auth_token = "auth_token_fake" if sesion else ""
        self.cookies_json = ""
        self.password = ""
        self.tipo_cuenta = "politica"
        self.perfil_personalidad = "formal"
        self.personalidad = ""
        self.seccion = ""
        self.nombre_mostrado = ""
        self.rol_activacion = "hashtags"
        self.tier_calidad = tier_calidad
        self.pausada_activacion = pausada
        self.plataforma = "twitter"
        self.activa = True
        for clave, valor in extra.items():
            setattr(self, clave, valor)


class _RelojFalso:
    """`time` minimo de motor: `monotonic` avanza y `sleep` no duerme."""

    def __init__(self, paso=0.5):
        self._t = 1000.0
        self._paso = float(paso)
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            self._t += self._paso
            return self._t

    def sleep(self, segundos):
        with self._lock:
            try:
                self._t += max(0.0, float(segundos))
            except (TypeError, ValueError):
                self._t += self._paso


def _textos_fake(cs, objetivo, tags, *a, **k):
    """Generacion fake: N textos por cuenta con TODOS los tags."""
    return {
        c.usuario: [
            f"texto {i + 1} {' '.join(tags)}"
            for i in range(objetivo.get(c.usuario, 0))
        ]
        for c in cs
    }


def _preparar_motor(cuentas, intentar=None, textos=None, max_concurrente=2):
    """Motor listo para un E2E de actividad (sin red ni Chrome)."""
    motor = MotorActivacion(max_concurrente=max_concurrente)
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._generar_textos_actividad = textos or _textos_fake
    if intentar is None:
        llamadas: list = []
        lock = threading.Lock()

        def intentar(cuenta, rol, urls, texto, dar_like):
            with lock:
                llamadas.append((cuenta.usuario, texto))
                numero = len(llamadas)
            return (
                cuenta.usuario, rol, True, "ok",
                f"https://x.com/{cuenta.usuario}/status/{numero}",
            )

        motor._llamadas = llamadas
    motor._intentar_accion_rol = intentar
    return motor


def _correr(motor, **kwargs):
    """Ejecuta `ejecutar_actividad` con parches (registro, cuotas, reloj)."""
    registros: list = []
    pausas: list = []
    motor._dormir_cancelable = (
        lambda segundos, tramo=0.5: (pausas.append(float(segundos)), True)[1]
    )
    base_horaria = kwargs.pop("_base_horaria", None)
    orig_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    contar = (
        (lambda usuarios, minutos=None: dict(base_horaria))
        if base_horaria is not None
        else (lambda *a, **k: {})
    )
    extras = (
        ((cuotas_mod, "limite_por_rol", lambda rol: 5),)
        if base_horaria is not None
        else ()
    )
    try:
        with _parches(
            (motor_mod, "registrar_accion",
             lambda *a, **k: registros.append(a)),
            (cuotas_mod, "contar_acciones_por_usuario", contar),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
            *extras,
        ):
            resumen = motor.ejecutar_actividad(**kwargs)
    finally:
        motor_mod.time = orig_time
    return resumen, registros, pausas


def _pausas_same_cuenta(pausas, minimo, maximo):
    return [p for p in pausas if minimo <= p <= maximo]


# --------------------------------------------------------------------------- #
# (1) E2E: 3 cuentas, N fijo 3 -> 9 posts con pausas y URLs
# --------------------------------------------------------------------------- #
def test_e2e_9_posts(check):
    print("(1) E2E 3 cuentas x 3 posts = 9 posts con pausas y URLs")
    cuentas = [_CuentaFake("act_a"), _CuentaFake("act_b"), _CuentaFake("act_c")]
    motor = _preparar_motor(cuentas)
    callback_calls: list = []
    resumen, registros, pausas = _correr(
        motor,
        usuarios=["act_a", "act_b", "act_c"],
        hashtags=["#mexico", "#futbol"],
        posts_min=3,
        posts_max=3,
        pausa_entre_posts_seg=(60, 240),
        max_browsers=2,
        callback=lambda h, t, u, ok: callback_calls.append((h, t, u, ok)),
    )
    llamadas = motor._llamadas
    por_usuario = {}
    for usuario, _texto in llamadas:
        por_usuario[usuario] = por_usuario.get(usuario, 0) + 1
    check(
        "exactamente 9 posts (3 por cuenta)",
        len(llamadas) == 9
        and sorted(por_usuario.values()) == [3, 3, 3],
        f"({por_usuario})",
    )
    check(
        "todos los textos llevan AMBOS hashtags (no subconjuntos)",
        all("#mexico" in t and "#futbol" in t for _u, t in llamadas),
        f"({[t for _u, t in llamadas][:2]})",
    )
    check(
        "resumen: exitosas=9, fallidas=0, omitidas=0, total_esperado=9",
        resumen.get("exitosas") == 9
        and resumen.get("fallidas") == 0
        and resumen.get("omitidas") == 0
        and resumen.get("total_esperado") == 9,
        f"({resumen.get('exitosas')}/{resumen.get('fallidas')}/"
        f"{resumen.get('omitidas')}/{resumen.get('total_esperado')})",
    )
    check(
        "por_cuenta: posts_objetivo=3, exitosas=3 y 3 urls por cuenta",
        all(
            resumen["por_cuenta"][u]["posts_objetivo"] == 3
            and resumen["por_cuenta"][u]["exitosas"] == 3
            and len(resumen["por_cuenta"][u]["urls"]) == 3
            for u in ("act_a", "act_b", "act_c")
        ),
        f"({resumen.get('por_cuenta')})",
    )
    urls = resumen.get("urls") or []
    check(
        "urls: 9 unicas y ninguna vacia",
        len(urls) == 9 and len(set(urls)) == 9 and all("/status/" in u for u in urls),
        f"({len(urls)})",
    )
    pausas_post = _pausas_same_cuenta(pausas, 60.0, 240.0)
    pausas_cuenta = _pausas_same_cuenta(pausas, 0.5, 2.0)
    check(
        "6 pausas entre posts de la MISMA cuenta (2 por cuenta) en [60,240]",
        len(pausas_post) == 6,
        f"({pausas_post})",
    )
    check(
        "3 pausas cortas entre cuentas distintas en [0.5,2]",
        len(pausas_cuenta) == 3,
        f"({pausas_cuenta})",
    )
    check(
        "registro: 9 acciones tipo 'post' con estado 'exito'",
        len(registros) == 9
        and all(r[1] == "post" and r[2] == "exito" for r in registros),
        f"({registros[:1]})",
    )
    progreso = motor.snapshot_progreso()
    check(
        "progreso: hechas=9 y ronda_actual=1 (sin rondas)",
        progreso.get("hechas") == 9 and progreso.get("ronda_actual") == 1,
        f"(hechas={progreso.get('hechas')}, ronda={progreso.get('ronda_actual')})",
    )
    check(
        "callback: 9 llamadas con total=9 y ultima hechas=9",
        len(callback_calls) == 9
        and all(t == 9 for _h, t, _u, _ok in callback_calls)
        and callback_calls[-1][0] == 9,
        f"(n={len(callback_calls)})",
    )


# --------------------------------------------------------------------------- #
# (2) N aleatorio en [3,4]
# --------------------------------------------------------------------------- #
def test_n_aleatorio(check):
    print("(2) N aleatorio por cuenta dentro de [min,max] (200 iteraciones)")
    cuentas = [_CuentaFake("n1"), _CuentaFake("n2"), _CuentaFake("n3")]
    valores = set()
    en_rango = True
    for _ in range(200):
        objetivo = MotorActivacion._objetivos_actividad(cuentas, 3, 4)
        if len(objetivo) != 3:
            en_rango = False
            break
        for valor in objetivo.values():
            valores.add(valor)
            if valor < 3 or valor > 4:
                en_rango = False
    check(
        "N nunca sale de [3,4] en 200 iteraciones",
        en_rango,
        f"(valores={sorted(valores)})",
    )
    check(
        "ambos valores (3 y 4) aparecen",
        valores == {3, 4},
        f"({sorted(valores)})",
    )
    check(
        "rango saneado: min>max se intercambia y valores raros se acotan",
        MotorActivacion._rango_n_actividad(4, 3) == (3, 4)
        and MotorActivacion._rango_n_actividad(0, 0) == (1, 1)
        and MotorActivacion._rango_n_actividad(None, None) == (3, 4)
        and MotorActivacion._rango_n_actividad(1, 999) == (1, 50),
        f"({MotorActivacion._rango_n_actividad(4, 3)}, "
        f"{MotorActivacion._rango_n_actividad(None, None)})",
    )


# --------------------------------------------------------------------------- #
# (3) Pausadas
# --------------------------------------------------------------------------- #
def test_pausadas(check):
    print("(3) pausada de 4: omitida con contadores; permitir_pausadas la deja")
    cuentas = [
        _CuentaFake("libre1"), _CuentaFake("libre2"), _CuentaFake("libre3"),
        _CuentaFake("clienta", pausada=True),
    ]
    motor = _preparar_motor(cuentas)
    resumen, _registros, _pausas = _correr(
        motor,
        usuarios=[c.usuario for c in cuentas],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
    )
    ejecutadas = {u for u, _t in motor._llamadas}
    check(
        "la pausada NO ejecuta con el default",
        "clienta" not in ejecutadas and len(motor._llamadas) == 9,
        f"({sorted(ejecutadas)})",
    )
    check(
        "pausadas_omitidas=1 y usuario listado",
        resumen.get("pausadas_omitidas") == 1
        and resumen.get("pausadas_usuarios") == ["clienta"],
        f"({resumen.get('pausadas_usuarios')})",
    )
    check(
        "la pausada no cuenta como fallo ni en total_esperado",
        resumen.get("fallidas") == 0 and resumen.get("total_esperado") == 9
        and "clienta" not in resumen.get("por_cuenta", {}),
        f"(total={resumen.get('total_esperado')})",
    )

    motor2 = _preparar_motor(cuentas)
    resumen2, _r2, _p2 = _correr(
        motor2,
        usuarios=[c.usuario for c in cuentas],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
        permitir_pausadas=True,
    )
    check(
        "permitir_pausadas=True: la pausada tambien publica (12 posts)",
        len(motor2._llamadas) == 12
        and len({u for u, _t in motor2._llamadas}) == 4,
        f"({len(motor2._llamadas)})",
    )
    check(
        "permitir_pausadas=True: contadores de pausa en 0",
        resumen2.get("pausadas_omitidas") == 0
        and resumen2.get("pausadas_usuarios") == []
        and resumen2.get("total_esperado") == 12,
        f"({resumen2.get('pausadas_omitidas')}, "
        f"{resumen2.get('total_esperado')})",
    )

    # Todas pausadas: salida temprana, sin ejecutar nada y con contadores.
    motor3 = _preparar_motor([_CuentaFake("solo_pausada", pausada=True)])
    resumen3, _r3, _p3 = _correr(
        motor3,
        usuarios=["solo_pausada"],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
    )
    check(
        "todas pausadas: 0 ejecuciones y contadores consistentes",
        motor3._llamadas == []
        and resumen3.get("pausadas_omitidas") == 1
        and resumen3.get("total_esperado") == 0
        and resumen3.get("por_cuenta") == {}
        and resumen3.get("exitosas") == 0,
        f"(omitidas={resumen3.get('pausadas_omitidas')}, "
        f"total={resumen3.get('total_esperado')})",
    )


# --------------------------------------------------------------------------- #
# (4) Tier 2 / Tier 3
# --------------------------------------------------------------------------- #
def test_tier_omitidas(check):
    print("(4) Tier 2/Tier 3 omitidas sin navegador; Tier 1/sin tier ejecutan")
    cuentas = [
        _CuentaFake("t2_post", tier_calidad="tier2"),
        _CuentaFake("t3_post", tier_calidad="tier3"),
        _CuentaFake("t1_ok", tier_calidad="tier1"),
        _CuentaFake("sin_tier_ok"),
    ]
    motor = _preparar_motor(cuentas)
    resumen, _registros, _pausas = _correr(
        motor,
        usuarios=[c.usuario for c in cuentas],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
    )
    ejecutadas = {u for u, _t in motor._llamadas}
    check(
        "solo Tier 1 y sin tier ejecutan",
        ejecutadas == {"t1_ok", "sin_tier_ok"},
        f"({sorted(ejecutadas)})",
    )
    check(
        "tier_omitidas=2 con la lista completa",
        resumen.get("tier_omitidas") == 2
        and sorted(resumen.get("tier_omitidas_usuarios") or [])
        == ["t2_post", "t3_post"],
        f"({resumen.get('tier_omitidas_usuarios')})",
    )
    check(
        "contadores por tier: tier2=1 y tier3=1",
        resumen.get("tier2_hashtags_omitidas") == 1
        and resumen.get("tier3_omitidas") == 1
        and resumen.get("tier2_hashtags_usuarios") == ["t2_post"]
        and resumen.get("tier3_omitidas_usuarios") == ["t3_post"],
        f"(t2={resumen.get('tier2_hashtags_omitidas')}, "
        f"t3={resumen.get('tier3_omitidas')})",
    )
    check(
        "las omitidas no son fallos ni entran a total_esperado",
        resumen.get("fallidas") == 0
        and resumen.get("total_esperado") == 6
        and "t2_post" not in resumen.get("por_cuenta", {})
        and "t3_post" not in resumen.get("por_cuenta", {}),
        f"(total={resumen.get('total_esperado')})",
    )


# --------------------------------------------------------------------------- #
# (5) Cuota agotada a mitad
# --------------------------------------------------------------------------- #
def test_cuota_a_mitad(check):
    print("(5) cuota horaria agotada a mitad: solo se omiten los posts sin cupo")
    cuentas = [_CuentaFake("corta"), _CuentaFake("larga")]
    motor = _preparar_motor(cuentas)
    # "corta" arranca con 3 de sus 5 posts ya usados (LIMITE_POSTS_HORA=5).
    resumen, registros, _pausas = _correr(
        motor,
        usuarios=["corta", "larga"],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
        max_browsers=1,
        _base_horaria={"corta": {"hashtags": 3}},
    )
    por_usuario = {}
    for usuario, _texto in motor._llamadas:
        por_usuario[usuario] = por_usuario.get(usuario, 0) + 1
    check(
        "corta publica 2 (cupo restante) y larga 3",
        por_usuario == {"corta": 2, "larga": 3},
        f"({por_usuario})",
    )
    check(
        "omitidas_por_cuota=1 y omitidas=1",
        resumen.get("omitidas_por_cuota") == 1
        and resumen.get("omitidas") == 1,
        f"(cuota={resumen.get('omitidas_por_cuota')}, "
        f"omitidas={resumen.get('omitidas')})",
    )
    check(
        "exitosas=5 y el invariante cuadra (5+0+1=6)",
        resumen.get("exitosas") == 5
        and resumen.get("fallidas") == 0
        and resumen.get("exitosas") + resumen.get("fallidas")
        + resumen.get("omitidas") == resumen.get("total_esperado"),
        f"({resumen.get('exitosas')}/{resumen.get('fallidas')}/"
        f"{resumen.get('omitidas')}/{resumen.get('total_esperado')})",
    )
    check(
        "la cuenta con cupo no se ve afectada",
        resumen["por_cuenta"]["larga"]["exitosas"] == 3
        and len(registros) == 5,
        f"(larga={resumen['por_cuenta']['larga']['exitosas']})",
    )


# --------------------------------------------------------------------------- #
# (6) Cancelacion a mitad
# --------------------------------------------------------------------------- #
def test_cancelacion(check):
    print("(6) cancelacion a mitad: cancelada=True y resumen consistente")
    cuentas = [_CuentaFake("c1"), _CuentaFake("c2")]
    cancelar = threading.Event()
    llamadas: list = []
    lock = threading.Lock()
    despues: list = []

    def intentar(cuenta, rol, urls, texto, dar_like):
        with lock:
            if cancelar.is_set():
                despues.append(cuenta.usuario)
            llamadas.append((cuenta.usuario, texto))
            if len(llamadas) == 3:
                cancelar.set()
            numero = len(llamadas)
        return (
            cuenta.usuario, rol, True, "ok",
            f"https://x.com/{cuenta.usuario}/status/{numero}",
        )

    motor = _preparar_motor(cuentas, intentar=intentar)
    resumen, _registros, _pausas = _correr(
        motor,
        usuarios=["c1", "c2"],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
        max_browsers=1,
        cancelar=cancelar,
    )
    check(
        "cancelada=True",
        resumen.get("cancelada") is True,
        f"({resumen.get('cancelada')})",
    )
    check(
        "sin publicaciones despues del paro",
        despues == [],
        f"({despues})",
    )
    check(
        "invariante: exitosas+fallidas+omitidas = total_esperado",
        resumen.get("exitosas", 0) + resumen.get("fallidas", 0)
        + resumen.get("omitidas", 0) == resumen.get("total_esperado"),
        f"({resumen.get('exitosas')}+{resumen.get('fallidas')}+"
        f"{resumen.get('omitidas')}={resumen.get('total_esperado')})",
    )
    check(
        "la cuenta cortada por el paro cuenta sus posts restantes",
        resumen.get("omitidas_por_cancelacion", 0) >= 1
        and resumen.get("exitosas") == 3,
        f"(cancelacion={resumen.get('omitidas_por_cancelacion')}, "
        f"exitosas={resumen.get('exitosas')})",
    )


# --------------------------------------------------------------------------- #
# (7) Una cuenta que falla no tumba a las demas / sesion caida
# --------------------------------------------------------------------------- #
def test_fallo_aislado(check):
    print("(7) una cuenta que falla no tumba a las demas")
    cuentas = [_CuentaFake("buena1"), _CuentaFake("mala"), _CuentaFake("buena2")]

    def intentar(cuenta, rol, urls, texto, dar_like):
        if cuenta.usuario == "mala":
            return (cuenta.usuario, rol, False, "sin detalle", "")
        return (cuenta.usuario, rol, True, "ok",
                f"https://x.com/{cuenta.usuario}/status/1")

    motor = _preparar_motor(cuentas, intentar=intentar)
    resumen, _registros, _pausas = _correr(
        motor,
        usuarios=[c.usuario for c in cuentas],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
    )
    check(
        "las buenas completan 3 posts cada una",
        resumen["por_cuenta"]["buena1"]["exitosas"] == 3
        and resumen["por_cuenta"]["buena2"]["exitosas"] == 3,
        f"({resumen.get('por_cuenta')})",
    )
    check(
        "la mala falla sus 3 posts pero no afecta a las demas",
        resumen["por_cuenta"]["mala"]["fallidas"] == 3
        and resumen.get("exitosas") == 6
        and resumen.get("fallidas") == 3,
        f"(exitosas={resumen.get('exitosas')}, "
        f"fallidas={resumen.get('fallidas')})",
    )
    check(
        "invariante cuadra (6+3+0=9)",
        resumen.get("omitidas") == 0
        and resumen.get("total_esperado") == 9,
        f"(omitidas={resumen.get('omitidas')})",
    )

    # Sesion caida a mitad: el resto de SUS posts se omite, las demas siguen.
    cuentas2 = [_CuentaFake("viva"), _CuentaFake("caida")]

    def intentar2(cuenta, rol, urls, texto, dar_like):
        if cuenta.usuario == "caida":
            return (
                cuenta.usuario, rol, False,
                "sesión de X expirada: renueva cookies/login", "",
            )
        return (cuenta.usuario, rol, True, "ok",
                f"https://x.com/{cuenta.usuario}/status/1")

    motor2 = _preparar_motor(cuentas2, intentar=intentar2)
    resumen2, _r2, _p2 = _correr(
        motor2,
        usuarios=["viva", "caida"],
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
        max_browsers=1,
    )
    check(
        "sesion caida: 1 fallo + 2 omitidos por sesion",
        resumen2["por_cuenta"]["caida"]["fallidas"] == 1
        and resumen2.get("omitidas_por_sesion") == 2,
        f"(fallidas={resumen2['por_cuenta']['caida']['fallidas']}, "
        f"sesion={resumen2.get('omitidas_por_sesion')})",
    )
    check(
        "la otra cuenta termina intacta y el invariante cuadra",
        resumen2["por_cuenta"]["viva"]["exitosas"] == 3
        and resumen2.get("exitosas", 0) + resumen2.get("fallidas", 0)
        + resumen2.get("omitidas", 0) == resumen2.get("total_esperado"),
        f"(viva={resumen2['por_cuenta']['viva']['exitosas']})",
    )


# --------------------------------------------------------------------------- #
# (8) Tope de duracion
# --------------------------------------------------------------------------- #
def test_tope_tiempo(check):
    print("(8) duracion_max_min chico: los posts restantes se omiten por tiempo")
    cuentas = [_CuentaFake("rapida")]
    motor = _preparar_motor(cuentas)
    contador = {"pausas": 0}
    pausas: list = []

    def dormir(segundos, tramo=0.5):
        contador["pausas"] += 1
        pausas.append(float(segundos))
        if contador["pausas"] >= 2:
            # Pasada la primera pausa entre posts: el deadline ya no alcanza.
            motor_mod.time.sleep(99999)
        return True

    motor._dormir_cancelable = dormir
    registros: list = []
    orig_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(
            (motor_mod, "registrar_accion",
             lambda *a, **k: registros.append(a)),
            (cuotas_mod, "contar_acciones_por_usuario", lambda *a, **k: {}),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
        ):
            resumen = motor.ejecutar_actividad(
                usuarios=["rapida"],
                hashtags=["#x"],
                posts_min=3,
                posts_max=3,
                duracion_max_min=1,
                max_browsers=1,
            )
    finally:
        motor_mod.time = orig_time
    check(
        "publica 1 y omite 2 por tiempo",
        resumen.get("exitosas") == 1
        and resumen.get("omitidas_por_tiempo") == 2,
        f"(exitosas={resumen.get('exitosas')}, "
        f"tiempo={resumen.get('omitidas_por_tiempo')})",
    )
    check(
        "invariante cuadra y no hay fallos",
        1 + resumen.get("fallidas", 0) + resumen.get("omitidas", 0) == 3,
        f"(total={resumen.get('total_esperado')})",
    )
    check(
        "solo se registro 1 accion (las omitidas no abren nada)",
        len(registros) == 1,
        f"({len(registros)})",
    )


# --------------------------------------------------------------------------- #
# (9) Sin hashtags -> error claro
# --------------------------------------------------------------------------- #
def test_sin_hashtags(check):
    print("(9) sin hashtags: error claro sin abrir navegador")
    cuentas = [_CuentaFake("h1")]
    motor = _preparar_motor(cuentas)
    resumen, registros, _pausas = _correr(
        motor, usuarios=["h1"], hashtags=None, posts_min=3, posts_max=3
    )
    check(
        "resumen con error claro",
        isinstance(resumen.get("error"), str)
        and "hashtags" in resumen["error"]
        and resumen.get("exitosas") == 0
        and resumen.get("total_esperado") == 0,
        f"({resumen.get('error')!r})",
    )
    check(
        "no se publico nada",
        motor._llamadas == [] and registros == [],
        f"({motor._llamadas})",
    )
    resumen2, _r2, _p2 = _correr(
        motor, usuarios=["h1"], hashtags=[], posts_min=3, posts_max=3
    )
    check(
        "lista vacia tambien da error",
        isinstance(resumen2.get("error"), str),
        f"({resumen2.get('error')!r})",
    )
    check(
        "las claves estandar siguen presentes",
        "cancelada" in resumen and "omitidas_por_cuota" in resumen
        and "pausadas_omitidas" in resumen and "urls" in resumen,
        f"({sorted(resumen)[:6]})",
    )

    # Mas de 6 hashtags: se usan los primeros 6 (tolerante).
    motor2 = _preparar_motor([_CuentaFake("h2")])
    resumen3, _r3, _p3 = _correr(
        motor2, usuarios=["h2"],
        hashtags=["#a", "#b", "#c", "#d", "#e", "#f", "#g"],
        posts_min=3, posts_max=3,
    )
    check(
        "7 hashtags: se recortan a 6 y la actividad corre",
        resumen3.get("exitosas") == 3
        and all("#g" not in t for _u, t in motor2._llamadas),
        f"(exitosas={resumen3.get('exitosas')})",
    )


# --------------------------------------------------------------------------- #
# (10) Generacion: contrato nuevo -> fallback -> pool local
# --------------------------------------------------------------------------- #
def test_generacion_contrato_nuevo(check):
    print("(10) generacion: usa generar_textos_actividad_por_cuenta (nuevo)")
    cuentas = [_CuentaFake("g1"), _CuentaFake("g2")]
    motor = _preparar_motor(cuentas, textos=None)
    motor._generar_textos_actividad = MotorActivacion._generar_textos_actividad.__get__(
        motor, MotorActivacion
    )
    capturado: dict = {}

    def fake_actividad(cuentas_info, tags, n, **kwargs):
        capturado["info"] = list(cuentas_info)
        capturado["tags"] = list(tags)
        capturado["n"] = n
        capturado["kwargs"] = dict(kwargs)
        return {
            str(info.get("usuario")): [
                f"actividad {i + 1} {' '.join(tags)}"
                for i in range(n)
            ]
            for info in cuentas_info
        }

    with mock.patch.object(
        ia_mod, "generar_textos_actividad_por_cuenta", fake_actividad,
        create=True,
    ):
        resumen, _registros, _pausas = _correr(
            motor,
            usuarios=["g1", "g2"],
            hashtags=["#uno", "#dos"],
            posts_min=3,
            posts_max=3,
            contexto="mi contexto",
            narrativa="mi narrativa",
            menciones=["@amigo"],
        )
    check(
        "la IA nueva recibe (infos, tags, N) y los kwargs congelados",
        capturado.get("tags") == ["#uno", "#dos"]
        and capturado.get("n") == 3
        and capturado.get("kwargs", {}).get("contexto") == "mi contexto"
        and capturado.get("kwargs", {}).get("narrativa") == "mi narrativa"
        and capturado.get("kwargs", {}).get("menciones") == ["@amigo"],
        f"({capturado.get('kwargs')})",
    )
    check(
        "los textos publicados vienen de la IA nueva",
        len(motor._llamadas) == 6
        and all("actividad" in t for _u, t in motor._llamadas)
        and resumen.get("exitosas") == 6,
        f"({motor._llamadas[:1]})",
    )
    check(
        "una llamada por grupo de N (una sola aqui)",
        capturado.get("n") == 3,
    )


def test_generacion_fallback(check):
    print("(10b) generacion: fallback a generar_textos_hashtags_por_cuenta")
    cuentas = [_CuentaFake("f1")]
    motor = _preparar_motor(cuentas, textos=None)
    motor._generar_textos_actividad = MotorActivacion._generar_textos_actividad.__get__(
        motor, MotorActivacion
    )
    usado = {"hash": 0}

    def fake_hash(cuentas_info, **kwargs):
        usado["hash"] += 1
        return {
            str(info.get("usuario")): [f"hashtags {i + 1}" for i in range(3)]
            for info in cuentas_info
        }

    with mock.patch.object(
        ia_mod, "generar_textos_actividad_por_cuenta",
        side_effect=TypeError("no existe la funcion nueva"),
        create=True,
    ), mock.patch.object(
        ia_mod, "generar_textos_hashtags_por_cuenta", fake_hash,
    ):
        resumen, _registros, _pausas = _correr(
            motor,
            usuarios=["f1"],
            hashtags=["#uno", "#dos"],
            posts_min=3,
            posts_max=3,
        )
    check(
        "el fallback de hashtags se uso",
        usado["hash"] == 1 and resumen.get("exitosas") == 3,
        f"(hash={usado['hash']})",
    )
    check(
        "TODOS los hashtags quedan garantizados en cada texto",
        all(
            "#uno" in texto and "#dos" in texto
            for _u, texto in motor._llamadas
        ),
        f"({[t for _u, t in motor._llamadas][:1]})",
    )


def test_generacion_pool_local(check):
    print("(10c) generacion: si todo falla, pool local con TODOS los hashtags")
    cuentas = [_CuentaFake("p1")]
    motor = _preparar_motor(cuentas, textos=None)
    motor._generar_textos_actividad = MotorActivacion._generar_textos_actividad.__get__(
        motor, MotorActivacion
    )
    with mock.patch.object(
        ia_mod, "generar_textos_actividad_por_cuenta",
        side_effect=RuntimeError("boom"),
        create=True,
    ), mock.patch.object(
        ia_mod, "generar_textos_hashtags_por_cuenta",
        side_effect=RuntimeError("boom"),
    ):
        resumen, _registros, _pausas = _correr(
            motor,
            usuarios=["p1"],
            hashtags=["#uno", "#dos"],
            texto_base="texto base de la actividad",
            posts_min=3,
            posts_max=3,
        )
    check(
        "publica 3 textos igual (pool local)",
        resumen.get("exitosas") == 3
        and len(motor._llamadas) == 3,
        f"(exitosas={resumen.get('exitosas')})",
    )
    check(
        "cada texto del pool lleva TODOS los hashtags",
        all(
            "#uno" in texto and "#dos" in texto
            for _u, texto in motor._llamadas
        ),
        f"({[t for _u, t in motor._llamadas][:1]})",
    )


# --------------------------------------------------------------------------- #
# (11) Firma congelada + helpers
# --------------------------------------------------------------------------- #
def test_firma_y_helpers(check):
    print("(11) firma congelada de `ejecutar_actividad` y helpers")
    params = inspect.signature(MotorActivacion.ejecutar_actividad).parameters
    nombres = list(params)
    esperados = [
        "self", "usuarios", "hashtags", "posts_min", "posts_max",
        "pausa_entre_posts_seg", "texto_base", "contexto", "narrativa",
        "menciones", "max_browsers", "permitir_pausadas", "duracion_max_min",
        "cancelar", "callback",
    ]
    check(
        "orden y nombres de parametros congelados",
        nombres == esperados,
        f"({nombres})",
    )
    check(
        "defaults congelados (3..4, (60,240), 120, False)",
        params["posts_min"].default == 3
        and params["posts_max"].default == 4
        and params["pausa_entre_posts_seg"].default == (60, 240)
        and params["permitir_pausadas"].default is False
        and params["duracion_max_min"].default == 120
        and params["max_browsers"].default is None,
    )
    check(
        "kwargs al FINAL (duracion_max_min, cancelar, callback)",
        nombres[-3:] == ["duracion_max_min", "cancelar", "callback"],
    )
    check(
        "browsers: default <=3, tope 3 y minimo 1",
        1 <= MotorActivacion._browsers_actividad(None) <= 3
        and MotorActivacion._browsers_actividad(10) == 3
        and MotorActivacion._browsers_actividad(0) == 1
        and MotorActivacion._browsers_actividad("2") == 2,
        f"(None={MotorActivacion._browsers_actividad(None)}, "
        f"10={MotorActivacion._browsers_actividad(10)})",
    )
    check(
        "hashtags normalizados desde lista y texto",
        MotorActivacion._normalizar_hashtags_actividad(["mexico", "#fut"])
        == ["#mexico", "#fut"]
        and MotorActivacion._normalizar_hashtags_actividad("#a #b") == ["#a", "#b"]
        and MotorActivacion._normalizar_hashtags_actividad(None) == [],
        f"({MotorActivacion._normalizar_hashtags_actividad(['mexico', '#fut'])})",
    )
    check(
        "pausas saneadas (tupla, numero, invertida)",
        MotorActivacion._rango_pausas_actividad((60, 240)) == (60.0, 240.0)
        and MotorActivacion._rango_pausas_actividad((240, 60)) == (60.0, 240.0)
        and MotorActivacion._rango_pausas_actividad(30) == (30.0, 30.0)
        and MotorActivacion._rango_pausas_actividad("raro") == (60.0, 240.0),
        f"({MotorActivacion._rango_pausas_actividad('raro')})",
    )


# --------------------------------------------------------------------------- #
# (12) usuarios=None / sin sesion
# --------------------------------------------------------------------------- #
def test_usuarios_none_y_sin_sesion(check):
    print("(12) usuarios=None usa todas las activas; sin sesion se omite")
    cuentas = [_CuentaFake("todas1"), _CuentaFake("todas2"), _CuentaFake("sin_sesion", sesion=False)]
    motor = _preparar_motor(cuentas)
    capturado = []
    motor._obtener_cuentas_por_rol = (
        lambda *a, **k: (capturado.append((a, k)), list(cuentas))[1]
    )
    resumen, _registros, _pausas = _correr(
        motor,
        usuarios=None,
        hashtags=["#x"],
        posts_min=3,
        posts_max=3,
    )
    check(
        "sin `usuarios` carga TODAS las activas (sin filtros)",
        capturado == [((), {})],
        f"({capturado})",
    )
    check(
        "la cuenta sin credenciales queda en sin_sesion y no publica",
        resumen.get("sin_sesion") == 1
        and resumen.get("sin_sesion_usuarios") == ["sin_sesion"]
        and len(motor._llamadas) == 6,
        f"(sin_sesion={resumen.get('sin_sesion')})",
    )
    check(
        "sin_sesion no afecta a las demas ni al invariante",
        resumen.get("exitosas") == 6
        and resumen.get("total_esperado") == 6,
        f"(exitosas={resumen.get('exitosas')})",
    )
    check(
        "la cuenta sin sesion no aparece en por_cuenta",
        "sin_sesion" not in resumen.get("por_cuenta", {}),
    )

    # `usuarios=[]` equivale a None (todas) y acepta Cuentas directas.
    motor2 = _preparar_motor(cuentas)
    capturado2 = []
    motor2._obtener_cuentas_por_rol = (
        lambda *a, **k: (capturado2.append((a, k)), list(cuentas))[1]
    )
    _r_vacio, _reg_vacio, _p_vacio = _correr(
        motor2, usuarios=[], hashtags=["#x"], posts_min=3, posts_max=3
    )
    check(
        "usuarios=[] carga todas (loader sin filtros)",
        capturado2 == [((), {})],
        f"({capturado2})",
    )
    motor3 = _preparar_motor([_CuentaFake("d1"), _CuentaFake("d2")])
    directas = [_CuentaFake("d1"), _CuentaFake("d2")]
    resumen3, _r3, _p3 = _correr(
        motor3, usuarios=directas, hashtags=["#x"], posts_min=3, posts_max=3
    )
    check(
        "usuarios con Cuentas directas funciona (6 posts)",
        len(motor3._llamadas) == 6
        and {u for u, _t in motor3._llamadas} == {"d1", "d2"}
        and resumen3.get("total_esperado") == 6,
        f"({len(motor3._llamadas)})",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_e2e_9_posts(check)
    test_n_aleatorio(check)
    test_pausadas(check)
    test_tier_omitidas(check)
    test_cuota_a_mitad(check)
    test_cancelacion(check)
    test_fallo_aislado(check)
    test_tope_tiempo(check)
    test_sin_hashtags(check)
    test_generacion_contrato_nuevo(check)
    test_generacion_fallback(check)
    test_generacion_pool_local(check)
    test_firma_y_helpers(check)
    test_usuarios_none_y_sin_sesion(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
