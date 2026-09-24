# -*- coding: utf-8 -*-
"""Regresion del fix "los Tier 1 se detienen en la curva de aceleracion".

Sintoma reportado en produccion: con la Curva de Aceleracion activa, los Tier 1
dejaban de publicar y la campana quedaba detenida. Causas raiz cubiertas aqui:

    (1) GATE CIEGO A CUOTAS: la fase 1 elegia Tier 1 sin mirar si tenian cupo;
        las cuentas "descansando" por cuota horaria/diaria se tomaban ronda tras
        ronda devolviendo "cuota agotada" (rondas basura) sin ningun avance.
    (2) FASE 1 SECUESTRABA LA CAMPANA: si NINGUN Tier 1 era utilizable (todos
        sin cupo o con la sesion caida/omitida), el gate no dejaba elegibles y
        la campana quedaba congelada hasta agotar `curva_fase1_min` (default
        15 min) con cero acciones. Ahora se ANTICIPA la fase 2 con un WARNING y
        el motivo visible (`curva_fase2_anticipada` / `curva_fase2_motivo`).
    (3) CLAVES DE TIER DESALINEADAS: `_registrar_tiers` guarda los usuarios sin
        espacios, pero el gate y la captura de cascada buscaban la clave exacta;
        una cuenta con espacios/mayusculas distintas nunca era elegible en la
        fase 1 ni aportaba URLs a la cascada.

Sin red, sin Chrome y sin tocar la base real (fakes + reloj falso + parches,
mismo patron que `tests/test_curva_motor.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_curva_tier1_fix.py
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.cuotas as cuotas_mod  # noqa: E402
import activaciones.motor as motor_mod  # noqa: E402
from activaciones.motor import MotorActivacion  # noqa: E402
from core.config import settings  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers (mismo patron que tests/test_curva_motor.py)
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

    def __init__(self, usuario, rol_activacion="", tier_calidad="",
                 registro="politica"):
        self.usuario = usuario
        self.auth_token = "auth_token_fake"
        self.cookies_json = ""
        self.password = ""
        self.tipo_cuenta = registro
        self.perfil_personalidad = "formal"
        self.personalidad = ""
        self.seccion = ""
        self.nombre_mostrado = ""
        self.rol_activacion = rol_activacion
        self.tier_calidad = tier_calidad
        self.activa = True
        self.proxy = ""
        self.pais = ""


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


class _LoggerFalso:
    """Captura los mensajes de loguru sin escribirlos."""

    def __init__(self):
        self.mensajes: list = []

    def _apunta(self, nivel):
        def _registrar(mensaje, *args, **kwargs):
            self.mensajes.append((nivel, str(mensaje)))
        return _registrar

    def __getattr__(self, nombre):
        if nombre in ("info", "warning", "error", "debug", "success", "critical"):
            return self._apunta(nombre)
        raise AttributeError(nombre)


def _escenario(n_t1=5, n_otros=5):
    """5 Tier 1 (hashtags) + 5 volumen (rt/cita/comentario)."""
    cuentas = [
        _CuentaFake(f"t1_{i}", rol_activacion="hashtags", tier_calidad="tier1")
        for i in range(n_t1)
    ]
    roles = ("rt", "cita", "comentario")
    for i in range(n_otros):
        cuentas.append(_CuentaFake(
            f"o_{i}", rol_activacion=roles[i % len(roles)],
            tier_calidad=("tier2" if i % 2 == 0 else ""),
        ))
    return cuentas


def _base_exhausta(usuarios, rol="hashtags", cuota=5):
    """Respuesta de `contar_acciones_por_usuario` con el cupo de TODOS usado."""
    return {str(u): {rol: cuota} for u in (usuarios or [])}


def _hechos_por_usuario(inicio, acciones, limite_fase1):
    """(acciones T1 antes del limite, acciones no-T1 antes del limite).

    `limite_fase1` = cuando habria terminado la fase 1 SIN el fix.
    """
    t1, otros = [], []
    for usuario, rol, momento, _en2 in acciones:
        if momento >= limite_fase1:
            continue
        (t1 if str(usuario).startswith("t1_") else otros).append(
            (usuario, rol)
        )
    return t1, otros


# --------------------------------------------------------------------------- #
# (1) Gate: fase 1 sin cupo no es elegible + fase 2 anticipada
# --------------------------------------------------------------------------- #
def test_gate_cuotas(check):
    print("(1) gate fase 1: Tier 1 sin cupo no es elegible y anticipa fase 2")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1": "tier1", "t2": "tier2"}
    motor._curva_activa = True
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 3600.0
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible = motor._elegibilidad_curva(
            True, viable=lambda u: str(u) != "t1"
        )
        check(
            "fase 1: Tier 1 SIN cupo NO es elegible",
            elegible("t1") is False,
            "(el gate viejo seguia eligiendolo y quemaba rondas)",
        )
        check(
            "fase 1: Tier 2 tampoco es elegible (sigue el blindaje)",
            elegible("t2") is False,
        )
        check(
            "el gate expone el hook `avanzar_si_bloqueada`",
            callable(getattr(elegible, "avanzar_si_bloqueada", None)),
        )
        check(
            "sin ningun Tier 1 con cupo: la fase 2 se ANTICIPA",
            elegible.avanzar_si_bloqueada() is True
            and motor._en_fase2() is True
            and motor._curva_fase2_anticipada is True,
            f"(en_fase2={motor._en_fase2()}, "
            f"anticipada={motor._curva_fase2_anticipada})",
        )
        check(
            "el motivo visible menciona el cupo",
            "sin cupo" in motor._curva_fase2_motivo,
            f"({motor._curva_fase2_motivo!r})",
        )
        check(
            "progreso: fase_actual=2 tras anticipar",
            motor.snapshot_progreso().get("fase_actual") == 2,
            f"({motor.snapshot_progreso().get('fase_actual')})",
        )
        check(
            "el hook es idempotente (2a llamada False)",
            elegible.avanzar_si_bloqueada() is False,
        )
        check(
            "fase 2: todos elegibles",
            elegible("t1") is True and elegible("t2") is True,
        )

        # Con UN Tier 1 con cupo no se anticipa nada.
        motor2 = MotorActivacion(max_concurrente=1)
        motor2._tier_de = {"t1_a": "tier1", "t1_b": "tier1"}
        motor2._curva_activa = True
        motor2._curva_fase1_seg = 3600.0
        motor2._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible2 = motor2._elegibilidad_curva(
            True, viable=lambda u: str(u) == "t1_b"
        )
        check(
            "con un Tier 1 con cupo: solo ese es elegible",
            elegible2("t1_a") is False and elegible2("t1_b") is True,
        )
        check(
            "con un Tier 1 con cupo: NO se anticipa la fase 2",
            elegible2.avanzar_si_bloqueada() is False
            and motor2._curva_fase2_anticipada is False
            and motor2._en_fase2() is False,
        )
        check(
            "con un Tier 1 con cupo: el motivo queda vacio",
            motor2._curva_fase2_motivo == "",
            f"({motor2._curva_fase2_motivo!r})",
        )
    finally:
        motor_mod.time = original_time


def test_gate_sin_viable_retrocompatible(check):
    print("(1b) gate sin `viable`: comportamiento clasico intacto")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1": "tier1", "t2": "tier2"}
    motor._curva_activa = True
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 3600.0
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible = motor._elegibilidad_curva(True)
        check(
            "sin `viable`: Tier 1 elegible y Tier 2 no (como antes)",
            elegible("t1") is True and elegible("t2") is False,
        )
        check(
            "sin `viable` y con Tier 1 vivo: NO se anticipa",
            elegible.avanzar_si_bloqueada() is False
            and motor._en_fase2() is False,
        )
    finally:
        motor_mod.time = original_time


def test_gate_reserva_fuera_de_campana(check):
    print("(1c) gate: reserva Tier 1 FUERA de la campaña no evita la fase 2")
    motor = MotorActivacion(max_concurrente=1)
    motor._curva_activa = True
    principal = _CuentaFake("t1_main", tier_calidad="tier1")
    reserva = _CuentaFake("t1_res", tier_calidad="tier1")
    motor._registrar_tiers([principal, reserva])
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 3600.0
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        gate = motor._elegibilidad_curva(
            True,
            viable=lambda u: str(u) != "t1_main",
            procesables=[principal],
        )
        check(
            "la reserva registrada pero fuera de la campaña NO cuenta",
            gate.avanzar_si_bloqueada() is True
            and "1 Tier 1 sin cupo" in motor._curva_fase2_motivo,
            f"(motivo={motor._curva_fase2_motivo!r})",
        )

        motor2 = MotorActivacion(max_concurrente=1)
        motor2._curva_activa = True
        motor2._registrar_tiers([principal, reserva])
        motor2._curva_fase1_seg = 3600.0
        motor2._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        gate2 = motor2._elegibilidad_curva(
            True,
            viable=lambda u: str(u) != "t1_main",
            procesables=[principal, reserva],
        )
        check(
            "la reserva DENTRO de la campaña SI evita la anticipacion",
            gate2.avanzar_si_bloqueada() is False
            and motor2._en_fase2() is False,
            f"(en_fase2={motor2._en_fase2()})",
        )
    finally:
        motor_mod.time = original_time


# --------------------------------------------------------------------------- #
# (2) Gate: Tier 1 con sesion caida tambien anticipa la fase 2
# --------------------------------------------------------------------------- #
def test_gate_sesiones_caidas(check):
    print("(2) gate fase 1: todos los Tier 1 con sesion caida -> fase 2")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1_a": "tier1", "t1_b": "tier1"}
    motor._curva_activa = True
    motor._marcar_sesion_caida("t1_a")
    motor._marcar_sesion_caida("t1_b")
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 3600.0
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible = motor._elegibilidad_curva(True)
        check(
            "con los Tier 1 caidos: la fase 2 se anticipa",
            elegible.avanzar_si_bloqueada() is True
            and motor._en_fase2() is True
            and motor._curva_fase2_anticipada is True,
        )
        check(
            "motivo: menciona la sesion caida",
            "sesion" in motor._curva_fase2_motivo
            and "2 Tier 1" in motor._curva_fase2_motivo,
            f"({motor._curva_fase2_motivo!r})",
        )
    finally:
        motor_mod.time = original_time


# --------------------------------------------------------------------------- #
# (3) Claves de tier tolerantes a espacios/mayusculas
# --------------------------------------------------------------------------- #
def test_tier_registrado_tolerante(check):
    print("(3) `_tier_registrado`: espacios/mayusculas no desalinean el gate")
    motor = MotorActivacion(max_concurrente=1)
    motor._tier_de = {"t1_x": "tier1", "t2_y": "tier2"}
    check(
        "clave exacta",
        motor._tier_registrado("t1_x") == "tier1",
    )
    check(
        "clave con espacios alrededor",
        motor._tier_registrado("  t1_x  ") == "tier1",
        f"({motor._tier_registrado('  t1_x  ')!r})",
    )
    check(
        "clave con mayusculas distintas",
        motor._tier_registrado("T1_X") == "tier1",
        f"({motor._tier_registrado('T1_X')!r})",
    )
    check(
        "cuenta desconocida -> sin tier",
        motor._tier_registrado("otra") == "",
    )
    check(
        "cuenta con tier vacio -> devuelve ''",
        motor._tier_registrado("vacia") == ""
        and (motor._tier_de.setdefault("vacia", "") or "") == "",
    )

    motor._curva_activa = True
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        motor._curva_fase1_seg = 3600.0
        motor._curva_fase2_t0 = motor_mod.time.monotonic() + 3600.0
        elegible = motor._elegibilidad_curva(True)
        check(
            "fase 1: el Tier 1 con espacios SI es elegible",
            elegible("  t1_x  ") is True,
        )
        motor._capturar_url_fase1(
            "  t1_x  ", "hashtags", "https://x.com/t1_x/status/1"
        )
        check(
            "cascada: la URL del Tier 1 con espacios SI se captura",
            motor._urls_fase1 == ["https://x.com/t1_x/status/1"],
            f"({motor._urls_fase1})",
        )
    finally:
        motor_mod.time = original_time


# --------------------------------------------------------------------------- #
# (4) `_bucle_rondas`: hook `avanzar_si_bloqueada` recalcula la ronda
# --------------------------------------------------------------------------- #
def test_hook_bucle_rondas(check):
    print("(4) _bucle_rondas: si el gate se bloquea, el hook libera y sigue")
    motor = MotorActivacion(max_concurrente=1)
    motor._n_workers = lambda: 2
    procesables = [_CuentaFake("a"), _CuentaFake("b")]
    estado = {"fase": 1, "avances": 0}

    def _gate(usuario):
        return estado["fase"] == 2

    def _avanzar():
        if estado["fase"] == 2:
            return False
        estado["fase"] = 2
        estado["avances"] += 1
        return True

    _gate.avanzar_si_bloqueada = _avanzar
    ejecutadas: list = []
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        rondas = motor._bucle_rondas(
            procesables,
            1,
            lambda ronda, usuarios=None: {
                u: "texto" for u in (usuarios or [])
            },
            lambda costo, texto, rol="": (
                ejecutadas.append(costo.usuario),
                (costo.usuario, rol, True, "ok", ""),
            )[1],
            lambda resultado, ronda: None,
            elegibilidad=_gate,
        )
    finally:
        motor_mod.time = original_time
    check(
        "el hook libero la fase UNA vez",
        estado["avances"] == 1,
        f"({estado['avances']})",
    )
    check(
        "las 2 cuentas ejecutaron tras liberar el gate",
        sorted(set(ejecutadas)) == ["a", "b"],
        f"({ejecutadas})",
    )
    check("se arranco al menos una ronda", rondas >= 1, f"({rondas})")

    # Gate sin hook (comportamiento clasico): 0 rondas, 0 ejecuciones.
    ejecutadas2: list = []
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=2.0)
    try:
        rondas2 = motor._bucle_rondas(
            procesables,
            1,
            lambda ronda, usuarios=None: {},
            lambda costo, texto, rol="": ejecutadas2.append(costo.usuario),
            lambda resultado, ronda: None,
            elegibilidad=lambda usuario: False,
        )
    finally:
        motor_mod.time = original_time
    check(
        "gate cerrado sin hook: 0 rondas y 0 ejecuciones (intacto)",
        rondas2 == 0 and ejecutadas2 == [],
        f"(rondas={rondas2}, ejecuciones={ejecutadas2})",
    )


# --------------------------------------------------------------------------- #
# (5) E2E: Tier 1 sin cupo -> fase 2 anticipada y el resto trabaja YA
# --------------------------------------------------------------------------- #
def _preparar_e2e(motor, cuentas, acciones, intentar=None):
    motor._n_workers = lambda: 4
    motor._obtener_cuentas_por_rol = lambda *a, **k: list(cuentas)
    motor._obtener_anclas = lambda urls: {}
    motor._generar_textos_por_rol = (
        lambda grupos, *a, **k: {
            c.usuario: "texto #x"
            for lista in grupos.values() for c in lista
        }
    )
    motor._distribuir_cohortes = lambda cs, d, c: [list(cs)]
    motor._dormir_cancelable = lambda *a, **k: True
    if intentar is None:
        def intentar(cuenta, rol, urls, texto, dar_like):
            acciones.append((
                cuenta.usuario, rol, motor_mod.time.monotonic(),
                motor._en_fase2(),
            ))
            url = (f"https://x.com/{cuenta.usuario}/status/1"
                   if rol in ("hashtags", "cita", "comentario") else "")
            return (cuenta.usuario, rol, True, "ok", url)
    motor._intentar_accion_rol = intentar


def _lanzar_roles(motor, cuentas, duracion=9, fase1=3):
    registros: list = []
    with _parches(
        (motor_mod, "_partir_por_sesion", lambda cs: (list(cs), [])),
        (motor_mod, "registrar_accion", lambda *a, **k: registros.append(a)),
    ):
        resumen = motor.ejecutar_por_roles(
            urls=["https://x.com/ancla/status/9"],
            texto_base="texto base",
            hashtags="#x",
            usuarios=[c.usuario for c in cuentas],
            duracion_min=duracion,
            cohortes=1,
            repetir=True,
            roles_aleatorios=False,
            curva_aceleracion=True,
            curva_fase1_min=fase1,
        )
    return resumen, registros


def test_e2e_tier1_sin_cupo(check):
    print("(5) E2E: 5 Tier 1 sin cupo horario -> fase 2 ANTES del reloj")
    cuentas = _escenario()
    t1_usuarios = [c.usuario for c in cuentas if c.usuario.startswith("t1_")]
    motor = MotorActivacion(max_concurrente=1)
    acciones: list = []
    _preparar_e2e(motor, cuentas, acciones)
    logger_falso = _LoggerFalso()
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        t_inicio = motor_mod.time.monotonic()
        limite_fase1 = t_inicio + 3 * 60.0
        with _parches(
            (motor_mod, "logger", logger_falso),
            (cuotas_mod, "contar_acciones_por_usuario",
             lambda usuarios, minutos=None: _base_exhausta(
                 t1_usuarios, "hashtags", 5
             )),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
        ):
            resumen, _registros = _lanzar_roles(motor, cuentas)
    finally:
        motor_mod.time = original_time
    t1_fase1, otros_fase1 = _hechos_por_usuario(
        t_inicio, acciones, limite_fase1
    )
    check(
        "Tier 1 sin cupo: NO se despachan en la fase 1 (0 rondas basura)",
        t1_fase1 == [],
        f"({t1_fase1})",
    )
    check(
        "el resto trabaja ANTES de donde terminaria la fase 1 sin el fix",
        len(otros_fase1) > 0,
        f"(acciones={len(acciones)}, fase1={len(otros_fase1)})",
    )
    check(
        "resumen: curva_fase2_anticipada=True con motivo de cupo",
        resumen.get("curva_fase2_anticipada") is True
        and "sin cupo" in str(resumen.get("curva_fase2_motivo", "")),
        f"(anticipada={resumen.get('curva_fase2_anticipada')}, "
        f"motivo={resumen.get('curva_fase2_motivo')!r})",
    )
    check(
        "WARNING visible en el log con el motivo",
        any(
            nivel == "warning" and "se ANTICIPA la fase 2" in mensaje
            and "sin cupo" in mensaje
            for nivel, mensaje in logger_falso.mensajes
        ),
        f"({[m for _n, m in logger_falso.mensajes if 'ANTICIPA' in m][:1]})",
    )
    check(
        "exitosas > 0 (la campana siguio trabajando)",
        int(resumen.get("exitosas", 0)) > 0,
        f"(exitosas={resumen.get('exitosas')})",
    )


def test_e2e_tier1_sesion_caida(check):
    print("(5b) E2E: todos los Tier 1 con sesion caida -> fase 2 anticipada")
    cuentas = _escenario()
    motor = MotorActivacion(max_concurrente=1)
    acciones: list = []
    fallos_t1: list = []

    def _intentar(cuenta, rol, urls, texto, dar_like):
        if str(cuenta.usuario).startswith("t1_"):
            fallos_t1.append(cuenta.usuario)
            return (
                cuenta.usuario, rol, False,
                "sesión de X expirada: renueva cookies/login", "",
            )
        acciones.append((
            cuenta.usuario, rol, motor_mod.time.monotonic(),
            motor._en_fase2(),
        ))
        return (cuenta.usuario, rol, True, "ok", "")

    _preparar_e2e(motor, cuentas, acciones, intentar=_intentar)
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        t_inicio = motor_mod.time.monotonic()
        limite_fase1 = t_inicio + 3 * 60.0
        with _parches(
            (cuotas_mod, "contar_acciones_por_usuario",
             lambda *a, **k: {}),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
        ):
            resumen, _registros = _lanzar_roles(motor, cuentas)
    finally:
        motor_mod.time = original_time
    _t1_fase1, otros_fase1 = _hechos_por_usuario(
        t_inicio, acciones, limite_fase1
    )
    check(
        "los Tier 1 fallaron y quedaron omitidos",
        len(set(fallos_t1)) >= 1,
        f"({sorted(set(fallos_t1))})",
    )
    check(
        "fase 1 sin Tier 1 vivos: el resto trabaja ANTES del fin de fase 1",
        len(otros_fase1) > 0,
        f"(otros={len(otros_fase1)}, total={len(acciones)})",
    )
    check(
        "resumen: fase 2 anticipada con motivo de sesion",
        resumen.get("curva_fase2_anticipada") is True
        and "sesion" in str(resumen.get("curva_fase2_motivo", "")),
        f"(anticipada={resumen.get('curva_fase2_anticipada')}, "
        f"motivo={resumen.get('curva_fase2_motivo')!r})",
    )


def test_e2e_algun_tier1_vivo(check):
    print("(5c) E2E: con UN Tier 1 con cupo la fase 1 NO se anticipa")
    cuentas = _escenario()
    t1_usuarios = [c.usuario for c in cuentas if c.usuario.startswith("t1_")]
    vivo = t1_usuarios[0]
    bloqueados = set(t1_usuarios[1:])
    limite = 10000

    def _base(usuarios, minutos=None):
        return {
            str(u): {"hashtags": limite}
            for u in (usuarios or []) if str(u) in bloqueados
        }

    motor = MotorActivacion(max_concurrente=1)
    acciones: list = []
    _preparar_e2e(motor, cuentas, acciones)
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(
            # Limite alto para que el Tier 1 con cupo NO se agote en la
            # campana; los bloqueados arrancan con su cupo ya consumido.
            # El tope diario se desactiva para que el Tier 1 vivo no se
            # retire por "Agotada por hoy" durante la prueba.
            (settings, "limite_acciones_dia", 0),
            (cuotas_mod, "limite_por_rol", lambda rol: limite),
            (cuotas_mod, "contar_acciones_por_usuario", _base),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
        ):
            resumen, _registros = _lanzar_roles(motor, cuentas)
    finally:
        motor_mod.time = original_time
    t1_en_fase1 = [
        (u, r) for u, r, _t, en2 in acciones
        if u == vivo and en2 is False
    ]
    check(
        "el Tier 1 con cupo SI trabajo en fase 1",
        bool(t1_en_fase1),
        f"(fase1={t1_en_fase1[:3]})",
    )
    check(
        "los Tier 1 sin cupo NO se despacharon en fase 1",
        not any(
            u in bloqueados and en2 is False
            for u, _r, _t, en2 in acciones
        ),
        f"({[(u, en2) for u, _r, _t, en2 in acciones if u in bloqueados][:5]})",
    )
    check(
        "no se anticipo la fase 2 (habia Tier 1 con cupo)",
        resumen.get("curva_fase2_anticipada") is False,
        f"(anticipada={resumen.get('curva_fase2_anticipada')}, "
        f"motivo={resumen.get('curva_fase2_motivo')!r})",
    )


# --------------------------------------------------------------------------- #
# (6) Cita masiva: el mismo blindaje con el rol "cita"
# --------------------------------------------------------------------------- #
def test_e2e_cita_masiva_sin_cupo(check):
    print("(6) E2E cita masiva: Tier 1 sin cupo de cita -> fase 2 anticipada")
    cuentas = [
        _CuentaFake("t1_c1", tier_calidad="tier1"),
        _CuentaFake("t1_c2", tier_calidad="tier1"),
        _CuentaFake("o_c1", tier_calidad="tier2"),
        _CuentaFake("o_c2", tier_calidad=""),
    ]
    t1_usuarios = ["t1_c1", "t1_c2"]
    motor = MotorActivacion(max_concurrente=1)
    llamadas: list = []
    motor._obtener_cuentas = lambda *a, **k: list(cuentas)
    motor._asignar_variaciones_cita = (
        lambda cs, *a, **k: {c.usuario: "texto" for c in cs}
    )

    def _quote(cuenta, urls, texto, dar_like, retardo=0):
        llamadas.append((cuenta.usuario, motor._en_fase2()))
        return (cuenta.usuario, True, "ok", "https://x.com/c/status/1")

    motor._quote_rt_una_cuenta = _quote
    motor._distribuir_cohortes = lambda cs, d, c: [list(cs)]
    motor._dormir_cancelable = lambda *a, **k: True
    registros: list = []
    original_time = motor_mod.time
    motor_mod.time = _RelojFalso(paso=0.5)
    try:
        with _parches(
            (motor_mod, "registrar_accion",
             lambda *a, **k: registros.append(a)),
            (cuotas_mod, "contar_acciones_por_usuario",
             lambda usuarios, minutos=None: _base_exhausta(
                 t1_usuarios, "cita", 5
             )),
            (cuotas_mod, "contar_acciones_dia_por_usuario",
             lambda *a, **k: {}),
        ):
            resumen = motor.ejecutar(
                urls=["https://x.com/ancla/status/9"],
                texto_base="texto",
                duracion_min=9,
                cohortes=1,
                repetir=True,
                curva_aceleracion=True,
                curva_fase1_min=3,
            )
    finally:
        motor_mod.time = original_time
    check(
        "los Tier 1 sin cupo de cita NO se ejecutaron en fase 1",
        not any(u in t1_usuarios and not en2 for u, en2 in llamadas),
        f"(t1_en_fase1={[u for u, en2 in llamadas if u in t1_usuarios and not en2][:5]})",
    )
    check(
        "el resto si trabajo y en fase 2 anticipada",
        any(u not in t1_usuarios for u, _e in llamadas)
        and resumen.get("curva_fase2_anticipada") is True,
        f"(otros={sum(1 for u, _e in llamadas if u not in t1_usuarios)}, "
        f"anticipada={resumen.get('curva_fase2_anticipada')})",
    )
    check(
        "motivo de cupo visible en el resumen",
        "sin cupo" in str(resumen.get("curva_fase2_motivo", "")),
        f"({resumen.get('curva_fase2_motivo')!r})",
    )


# --------------------------------------------------------------------------- #
# (7) Claves del resumen SIEMPRE presentes
# --------------------------------------------------------------------------- #
def test_claves_siempre_presentes(check):
    print("(7) resumen: claves de fase anticipada siempre presentes")
    motor = MotorActivacion(max_concurrente=1)
    motor._obtener_cuentas = lambda *a, **k: []
    motor._obtener_cuentas_por_rol = lambda *a, **k: []
    resumen = motor.ejecutar_por_roles(
        urls=[], texto_base="", duracion_min=1, repetir=True
    )
    check(
        "campana vacia: `curva_fase2_anticipada=False` y motivo ''",
        resumen.get("curva_fase2_anticipada") is False
        and resumen.get("curva_fase2_motivo") == "",
        f"(anticipada={resumen.get('curva_fase2_anticipada')}, "
        f"motivo={resumen.get('curva_fase2_motivo')!r})",
    )


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_gate_cuotas(check)
    test_gate_sin_viable_retrocompatible(check)
    test_gate_reserva_fuera_de_campana(check)
    test_gate_sesiones_caidas(check)
    test_tier_registrado_tolerante(check)
    test_hook_bucle_rondas(check)
    test_e2e_tier1_sin_cupo(check)
    test_e2e_tier1_sesion_caida(check)
    test_e2e_algun_tier1_vivo(check)
    test_e2e_cita_masiva_sin_cupo(check)
    test_claves_siempre_presentes(check)


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
