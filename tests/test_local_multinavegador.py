# -*- coding: utf-8 -*-
"""Tests del modo LOCAL con 1-3 navegadores y del orden aleatorio por ciclo.

Sin Chrome, sin red y sin BD real: se parchean `TwitterBot`, la lectura de
cuentas, la cuota diaria, el registro, la generacion de textos y el reloj
(mismo patron que `tests/test_local_campana.py`).

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_local_multinavegador.py
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.local as local_mod  # noqa: E402
import activaciones.variaciones as variaciones_mod  # noqa: E402
import core.registro as registro_mod  # noqa: E402
import ia.generador_contenido as generador_mod  # noqa: E402
from core.config import settings  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _parches(*cambios):
    originales = []
    try:
        for objeto, nombre, valor in cambios:
            originales.append((objeto, nombre, getattr(objeto, nombre)))
            setattr(objeto, nombre, valor)
        yield
    finally:
        for objeto, nombre, valor in reversed(originales):
            setattr(objeto, nombre, valor)


@contextlib.contextmanager
def _settings(**cambios):
    originales = {}
    try:
        for nombre, valor in cambios.items():
            originales[nombre] = getattr(settings, nombre, None)
            setattr(settings, nombre, valor)
        yield
    finally:
        for nombre, valor in originales.items():
            setattr(settings, nombre, valor)


class _RelojHilos:
    """`time` falso thread-safe: monotonic avanza y sleep no duerme."""

    def __init__(self, inicio=1000.0, paso=0.5):
        self.ahora = float(inicio)
        self.paso = float(paso)
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            self.ahora += self.paso
            return self.ahora

    def sleep(self, segundos):
        with self._lock:
            try:
                self.ahora += max(0.0, float(segundos))
            except (TypeError, ValueError):
                self.ahora += self.paso


def _cuenta(usuario, pendiente=False):
    return {
        "usuario": usuario,
        "tier_calidad": "tier1",
        "tipo_cuenta": "ciudadana",
        "perfil_personalidad": "popular",
        "personalidad": "le gusta el futbol",
        "seccion": "CI",
        "nombre_mostrado": usuario,
        "handle_actual": "",
        "pausada_activacion": False,
        "verificacion_pendiente": pendiente,
        "verificacion_motivo": "",
        "auth_token": "token",
        "cookies_json": None,
        "cookies_path": "",
    }


class _BotMulti:
    """Fake de TwitterBot thread-safe (cada worker usa SU instancia)."""

    creados: list = []
    _lock_creados = threading.Lock()
    al_publicar = None  # hook opcional (recibe el bot; p.ej. cancelar)
    barrera = None  # barrera opcional para esperar a que existan N bots

    def __init__(self, usuario, modo_local=False):
        self.usuario = usuario
        self.modo_local = modo_local
        self.driver = True
        self.ultima_url_publicada = ""
        self.ultimo_error = ""
        self.ultimo_refresh = ""
        self.posts: list = []
        self.citas: list = []
        self.usuarios_usados: list = []
        self.cambios: list = []
        self.cerrado = False
        with type(self)._lock_creados:
            type(self).creados.append(self)

    def iniciar_driver(self, pantalla_externa=False, proxy_dinamico=False,
                       modo_local=False):
        return True

    def preparar_sesion_cdp(self):
        if type(self).barrera is not None:
            try:
                type(self).barrera.wait(timeout=5)
            except Exception:
                pass
        return True

    def login_con_cookies(self):
        return True

    def cambiar_cuenta(self, usuario, **kwargs):
        self.cambios.append(usuario)
        self.usuario = usuario
        self.ultima_url_publicada = ""
        return True

    def esta_vivo(self):
        return True

    def _anotar_usuario(self):
        if self.usuario not in self.usuarios_usados:
            self.usuarios_usados.append(self.usuario)

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True,
                       buscar_url_perfil=None):
        self._anotar_usuario()
        self.posts.append({"usuario": self.usuario, "texto": texto})
        url = f"https://x.com/{self.usuario}/status/{len(self.posts)}"
        self.ultima_url_publicada = url
        if type(self).al_publicar is not None:
            type(self).al_publicar(self)
        return url

    def solo_retwittear(self, targets, usuario, mensaje_cita=None,
                        dar_like=False, imagen_path=None, buscar_url=True,
                        buscar_url_perfil=None):
        self._anotar_usuario()
        self.citas.append({"usuario": usuario, "mensaje_cita": mensaje_cita})
        return {"exitos": 1, "fallidos": 0, "urls": []}

    def cerrar(self):
        self.cerrado = True


def _clase_bot(**atributos):
    clase = type("FakeBotMulti", (_BotMulti,), {})
    clase.creados = []
    for nombre, valor in atributos.items():
        setattr(clase, nombre, valor)
    return clase


@contextlib.contextmanager
def _campana(cuentas, bot_cls, reloj):
    registros: list = []

    def _leer(usuarios=None):
        return list(cuentas), [], ""

    def _contar(usuarios):
        return {}

    def _registrar(*args, **kwargs):
        registros.append(args)

    def _posts(info, **kwargs):
        n = int(kwargs.get("n_por_cuenta", 1) or 1)
        return {
            cuenta["usuario"]: [
                f"Post {indice} de {cuenta['usuario']}" for indice in range(n)
            ]
            for cuenta in info
        }

    def _variaciones(base, cantidad, **kwargs):
        return [f"{base} cita {indice}" for indice in range(int(cantidad))]

    cambios = [
        (local_mod, "_leer_cuentas", _leer),
        (local_mod, "time", reloj),
        (local_mod, "TwitterBot", bot_cls),
        (registro_mod, "contar_acciones_dia_por_usuario", _contar),
        (registro_mod, "registrar_accion", _registrar),
        (generador_mod, "generar_textos_hashtags_por_cuenta", _posts),
        (variaciones_mod, "generar_pool_variaciones_openai", _variaciones),
    ]
    with _parches(*cambios):
        yield registros


def _ejecutar(clase, cuentas, navegadores, **kwargs):
    reloj = _RelojHilos()
    with _settings(local_max_cuentas=0, limite_local_dia=49, headless=False):
        with _campana(cuentas, clase, reloj):
            res = local_mod.ejecutar_campana_local(
                usuarios=[cuenta["usuario"] for cuenta in cuentas],
                navegadores=navegadores,
                duracion_min=1,
                pausa_entre_acciones_seg=0,
                **kwargs,
            )
    return res


def _acciones_bot(bot) -> int:
    return len(bot.posts) + len(getattr(bot, "citas", []))


# --------------------------------------------------------------------------- #
# (1) Particion pura del plan
# --------------------------------------------------------------------------- #
def test_particion(check):
    print("(1) _partir_plan_por_navegador: cubetas equitativas y sin partir cuentas")

    def _plan(usuarios_acciones):
        plan = []
        for usuario, cantidad in usuarios_acciones:
            for indice in range(cantidad):
                plan.append(
                    {
                        "usuario": usuario,
                        "rol": "hashtags" if indice % 2 == 0 else "cita",
                        "orden": indice,
                    }
                )
        return plan

    plan = _plan([("a", 2), ("b", 2), ("c", 2), ("d", 2)])
    una = local_mod._partir_plan_por_navegador(plan, 1)
    check(
        "N=1 conserva el plan exacto",
        len(una) == 1 and una[0] == plan,
    )
    for n in (2, 3):
        cubetas = local_mod._partir_plan_por_navegador(plan, n)
        union = [accion for cubeta in cubetas for accion in cubeta]
        usuarios_por_cubeta = [
            {accion["usuario"] for accion in cubeta} for cubeta in cubetas
        ]
        check(
            f"N={n}: devuelve {n} cubetas y la union es exacta",
            len(cubetas) == n
            and len(union) == len(plan)
            and sorted(id(accion) for accion in union)
            == sorted(id(accion) for accion in plan),
            f"({[len(c) for c in cubetas]})",
        )
        check(
            f"N={n}: cada cuenta queda ENTERA en una sola cubeta",
            all(
                not (usuarios_por_cubeta[i] & usuarios_por_cubeta[j])
                for i in range(n) for j in range(i + 1, n)
            )
            and set().union(*usuarios_por_cubeta) == {"a", "b", "c", "d"},
        )
        check(
            f"N={n}: reparto equilibrado (grupos de 2 acciones)",
            max(len(c) for c in cubetas) - min(len(c) for c in cubetas) <= 2,
            f"({[len(c) for c in cubetas]})",
        )
        for cubeta in cubetas:
            por_usuario = {}
            for accion in cubeta:
                por_usuario.setdefault(accion["usuario"], []).append(
                    accion["orden"]
                )
            check(
                f"N={n}: el orden interno de cada cuenta se conserva",
                all(valores == sorted(valores) for valores in por_usuario.values()),
            )

    vacio = local_mod._partir_plan_por_navegador([], 2)
    check(
        "plan vacio: N listas vacias",
        vacio == [[], []],
        f"({vacio})",
    )
    check(
        "plan vacio con N=1: [ [] ]",
        local_mod._partir_plan_por_navegador([], 1) == [[]],
    )
    for invalido in (0, -4, "abc", None):
        resultado = local_mod._partir_plan_por_navegador(plan, invalido)
        check(
            f"N invalido {invalido!r}: se normaliza a 1 cubeta",
            resultado == [plan],
            f"({[len(c) for c in resultado]})",
        )
    exagerado = local_mod._partir_plan_por_navegador(plan, 99)
    check(
        "N exagerado: se normaliza al maximo de 3",
        len(exagerado) == 3,
        f"({len(exagerado)})",
    )

    # Varias corridas: el reparto puede cambiar de orden, pero nunca pierde ni
    # duplica acciones ni parte una cuenta.
    estable = True
    for _ in range(25):
        cubetas = local_mod._partir_plan_por_navegador(plan, 2)
        union = [accion for cubeta in cubetas for accion in cubeta]
        if len(union) != len(plan):
            estable = False
            break
        if sorted(id(a) for a in union) != sorted(id(a) for a in plan):
            estable = False
            break
        por_cuenta = {}
        for indice, cubeta in enumerate(cubetas):
            for accion in cubeta:
                por_cuenta.setdefault(accion["usuario"], set()).add(indice)
        if any(len(cubetas_usadas) != 1 for cubetas_usadas in por_cuenta.values()):
            estable = False
            break
    check("25 repartos aleatorios: invariantes intactos", estable)


# --------------------------------------------------------------------------- #
# (2) Campana con 2 navegadores
# --------------------------------------------------------------------------- #
def test_campana_dos_navegadores(check):
    print("(2) campana con navegadores=2: 2 bots, acciones repartidas")
    local_mod.cerrar_navegadores_locales()
    cuentas = [_cuenta(f"multi_{i}") for i in range(4)]
    clase = _clase_bot()
    res = _ejecutar(
        clase,
        cuentas,
        navegadores=2,
        urls=["https://x.com/ancla/status/1"],
        hashtags="#mexico",
        texto_base="texto base",
        acciones_por_cuenta=2,
    )
    bots = list(clase.creados)
    check(
        "navegadores=2: se crean exactamente 2 bots",
        len(bots) == 2,
        f"({len(bots)})",
    )
    check(
        "navegadores=2: ambos bots procesan acciones",
        all(_acciones_bot(bot) > 0 for bot in bots),
        f"({[_acciones_bot(bot) for bot in bots]})",
    )
    usuarios_por_bot = [set(bot.usuarios_usados) for bot in bots]
    check(
        "navegadores=2: ninguna cuenta aparece en dos bots",
        all(
            not (usuarios_por_bot[i] & usuarios_por_bot[j])
            for i in range(len(bots)) for j in range(i + 1, len(bots))
        ),
        f"({usuarios_por_bot})",
    )
    check(
        "navegadores=2: entre los 2 bots cubren las 4 cuentas",
        set().union(*usuarios_por_bot) == {c["usuario"] for c in cuentas},
        f"({usuarios_por_bot})",
    )
    check(
        "navegadores=2: resumen reporta N, usados y por_navegador",
        res.get("navegadores") == 2
        and res.get("navegadores_usados") == 2
        and isinstance(res.get("por_navegador"), list)
        and len(res["por_navegador"]) == 2,
        f"({res.get('navegadores')}, {res.get('navegadores_usados')}, "
        f"{res.get('por_navegador')})",
    )
    hechas_nav = sum(
        int(fila.get("hechas", 0) or 0) for fila in res["por_navegador"]
    )
    check(
        "navegadores=2: hechas total = suma de por_navegador",
        res.get("hechas") == 8
        and hechas_nav == res.get("hechas")
        and res.get("exitosas") == 8,
        f"(hechas={res.get('hechas')}, suma={hechas_nav}, "
        f"exitosas={res.get('exitosas')})",
    )
    check(
        "navegadores=2: motivo completada y cuentas usadas = 4",
        res.get("motivo") == "completada"
        and res.get("cancelada") is False
        and res.get("cuentas_usadas") == 4,
        f"(motivo={res.get('motivo')!r}, usadas={res.get('cuentas_usadas')})",
    )
    for fila in res["por_navegador"]:
        check(
            f"por_navegador {fila.get('navegador')}: acciones/hechas cuadran",
            fila.get("acciones") == 4
            and fila.get("hechas") == 4
            and fila.get("exitosas") == 4
            and fila.get("fallidas") == 0,
            f"({fila})",
        )


# --------------------------------------------------------------------------- #
# (3) Cancelacion con 2 navegadores
# --------------------------------------------------------------------------- #
def test_cancelacion_dos_navegadores(check):
    print("(3) cancelacion con navegadores=2: no cuelga y cierra ambos bots")
    local_mod.cerrar_navegadores_locales()
    evento = threading.Event()
    cuentas = [_cuenta(f"cancel_{i}") for i in range(4)]
    # La barrera garantiza que AMBOS workers creen su bot antes de que la
    # primera accion cancele la campana (asi se comprueba el cierre de los 2).
    clase = _clase_bot(
        al_publicar=lambda _bot: evento.set(),
        barrera=threading.Barrier(2),
    )
    reloj = _RelojHilos()
    with _settings(local_max_cuentas=0, limite_local_dia=49, headless=False):
        with _campana(cuentas, clase, reloj):
            res = local_mod.ejecutar_campana_local(
                usuarios=[cuenta["usuario"] for cuenta in cuentas],
                urls=["https://x.com/ancla/status/1"],
                hashtags="#mexico",
                texto_base="texto base",
                acciones_por_cuenta=2,
                duracion_min=1,
                pausa_entre_acciones_seg=0,
                navegadores=2,
                cancelar=evento,
            )
    bots = list(clase.creados)
    check(
        "cancelacion: la campana termina y marca cancelada",
        res.get("cancelada") is True and res.get("motivo") == "cancelada",
        f"(cancelada={res.get('cancelada')}, motivo={res.get('motivo')!r})",
    )
    check(
        "cancelacion: cierra los dos bots",
        len(bots) == 2 and all(bot.cerrado for bot in bots),
        f"({[(id(b), b.cerrado) for b in bots]})",
    )
    check(
        "cancelacion: lo hecho + omitido por tiempo = planificadas",
        int(res.get("hechas", 0))
        + int(res.get("omitidas_por_tiempo", 0))
        == int(res.get("planificadas", 0)),
        f"(hechas={res.get('hechas')}, "
        f"omitidas_tiempo={res.get('omitidas_por_tiempo')}, "
        f"planificadas={res.get('planificadas')})",
    )
    check(
        "cancelacion: no quedan bots vivos en estado/_NAVEGADORES_ABIERTOS",
        not local_mod._NAVEGADORES_ABIERTOS,
        f"({local_mod._NAVEGADORES_ABIERTOS})",
    )


# --------------------------------------------------------------------------- #
# (4) dejar_abierto=True con 2 bots
# --------------------------------------------------------------------------- #
def test_dejar_abierto_dos(check):
    print("(4) dejar_abierto=True con 2 bots: quedan abiertos y se cierran")
    local_mod.cerrar_navegadores_locales()
    cuentas = [_cuenta(f"abierto_{i}") for i in range(4)]
    clase = _clase_bot()
    res = _ejecutar(
        clase,
        cuentas,
        navegadores=2,
        urls=["https://x.com/ancla/status/1"],
        hashtags="#mexico",
        texto_base="texto base",
        acciones_por_cuenta=2,
        dejar_abierto=True,
    )
    bots = list(clase.creados)
    check(
        "dejar_abierto: 2 bots creados y ninguno cerrado por la campana",
        len(bots) == 2 and all(not bot.cerrado for bot in bots),
        f"({[(id(b), b.cerrado) for b in bots]})",
    )
    check(
        "dejar_abierto: resumen marca navegador_abierto",
        res.get("navegador_abierto") is True,
    )
    abiertos = list(local_mod._NAVEGADORES_ABIERTOS)
    check(
        "dejar_abierto: ambos bots quedan registrados",
        len(abiertos) == 2
        and all(bot in abiertos for bot in bots),
        f"({[(id(b)) for b in abiertos]})",
    )
    cerrados = local_mod.cerrar_navegadores_locales()
    check(
        "dejar_abierto: cerrar_navegadores_locales cierra los 2",
        cerrados == 2
        and all(bot.cerrado for bot in bots)
        and not local_mod._NAVEGADORES_ABIERTOS,
        f"(cerrados={cerrados}, "
        f"estados={[bot.cerrado for bot in bots]})",
    )


# --------------------------------------------------------------------------- #
# (5) navegadores=1 conserva el camino actual
# --------------------------------------------------------------------------- #
def test_un_navegador(check):
    print("(5) navegadores=1: un solo bot y sin claves multi")
    local_mod.cerrar_navegadores_locales()
    cuentas = [_cuenta("solo_a"), _cuenta("solo_b")]
    clase = _clase_bot()
    res = _ejecutar(
        clase,
        cuentas,
        navegadores=1,
        urls=["https://x.com/ancla/status/1"],
        hashtags="#mexico",
        texto_base="texto base",
        acciones_por_cuenta=2,
    )
    bots = list(clase.creados)
    check(
        "navegadores=1: se crea un solo bot",
        len(bots) == 1,
        f"({len(bots)})",
    )
    check(
        "navegadores=1: el bot unico procesa todas las acciones",
        _acciones_bot(bots[0]) == 4 if bots else False,
        f"({[_acciones_bot(b) for b in bots]})",
    )
    check(
        "navegadores=1: resumen conserva el camino clasico (sin por_navegador)",
        res.get("navegadores") == 1
        and "por_navegador" not in res
        and "navegadores_usados" not in res,
        f"({res.get('navegadores')}, "
        f"por_navegador={'por_navegador' in res})",
    )
    check(
        "navegadores=1: 4 acciones completadas",
        res.get("hechas") == 4 and res.get("exitosas") == 4,
        f"(hechas={res.get('hechas')}, exitosas={res.get('exitosas')})",
    )


# --------------------------------------------------------------------------- #
# (6) UI: control y caption (fuente/AST, sin campana real)
# --------------------------------------------------------------------------- #
def test_ui(check):
    print("(6) UI LOCAL: control de navegadores y caption de orden aleatorio")
    fuente = (RAIZ / "web" / "operaciones" / "local.py").read_text(
        encoding="utf-8"
    )
    check(
        "fuente: number_input de navegadores 1-3 con default 2",
        '"🪟 Navegadores simultáneos (Chrome visibles)"' in fuente
        and "min_value=1,\n        max_value=3,\n        value=2,"
        in fuente
        and 'f"{PREFIX}_navegadores"' in fuente,
    )
    check(
        "fuente: help honesto (RAM/CPU e IP compartida, sin prometer nada)",
        "más RAM/CPU" in fuente
        and "misma IP" in fuente
        and "No promete" in fuente,
    )
    check(
        "fuente: caption corto de orden aleatorio por ciclo",
        "Orden de cuentas aleatorio en cada ciclo" in fuente,
    )
    check(
        "fuente: pasa navegadores al backend solo si lo soporta",
        "_soporta_kwarg(\n        ejecutar_campana_local, \"navegadores\"\n    )"
        in fuente
        and 'kwargs_backend["navegadores"] = parametros["navegadores"]'
        in fuente
        and '"navegadores": navegadores_n' in fuente,
    )
    check(
        "resumen visual: captions/metricas de navegadores",
        "Navegadores simultáneos" in fuente
        and '"🪟 Navegadores"' in fuente,
    )


def run(check):
    local_mod.cerrar_navegadores_locales()
    test_particion(check)
    test_campana_dos_navegadores(check)
    test_cancelacion_dos_navegadores(check)
    test_dejar_abierto_dos(check)
    test_un_navegador(check)
    test_ui(check)


if __name__ == "__main__":
    sys.path.insert(0, str(RAIZ / "tests"))
    from run_tests import check as _check

    run(_check)
    import run_tests

    sys.exit(0 if not run_tests._FALLOS else 1)
