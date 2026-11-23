# -*- coding: utf-8 -*-
"""Tests de la campana LOCAL secuencial (`activaciones/local.py`).

Solo stdlib + fakes: sin Chrome, sin red, sin BD real. Se parchean la lectura
de cuentas, la cuota diaria, el registro de acciones, la generacion de textos,
el reloj y `TwitterBot`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_local_campana.py
"""
from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import activaciones.local as local_mod  # noqa: E402
import activaciones.variaciones as variaciones_mod  # noqa: E402
import core.database as database_mod  # noqa: E402
import core.registro as registro_mod  # noqa: E402
import ia.generador_contenido as generador_mod  # noqa: E402
import utils.proxies as proxies_mod  # noqa: E402
from core.config import settings  # noqa: E402
from ia.generador_contenido import validar_texto_hashtag  # noqa: E402


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


class RelojFalso:
    def __init__(self, inicio=1000.0):
        self.ahora = float(inicio)
        self.suenos = []

    def monotonic(self):
        return self.ahora

    def time(self):
        return self.ahora

    def sleep(self, segundos):
        valor = float(segundos)
        self.suenos.append(valor)
        self.ahora += valor


def _cuenta(
    usuario,
    tier="tier1",
    auth="token",
    cookies_json=None,
    cookies_path="",
    pausada=False,
    tipo="ciudadana",
    perfil="popular",
    nombre="",
):
    return {
        "usuario": usuario,
        "tier_calidad": tier,
        "tipo_cuenta": tipo,
        "perfil_personalidad": perfil,
        "personalidad": "le gusta el futbol",
        "seccion": "CI",
        "nombre_mostrado": nombre or usuario,
        "handle_actual": "",
        "pausada_activacion": pausada,
        "auth_token": auth,
        "cookies_json": cookies_json,
        "cookies_path": cookies_path,
    }


class _BotBase:
    creados = []
    eventos = []

    def __init__(self, usuario, modo_local=False):
        self.usuario = usuario
        self.modo_local = modo_local
        self.driver = True
        self.ultima_url_publicada = ""
        self.ultimo_error = ""
        self.cerrado = False
        self.cambio_llamadas = []
        self.refrescar_llamadas = 0
        self.refrescos_solicitados = []
        self.posts = []
        self.citas = []
        self.hora_inicio = None
        self.headless_en_creacion = getattr(settings, "headless", None)
        type(self).creados.append(self)

    def iniciar_driver(self, pantalla_externa=False, proxy_dinamico=False, modo_local=False):
        self.driver_args = {
            "pantalla_externa": pantalla_externa,
            "proxy_dinamico": proxy_dinamico,
            "modo_local": modo_local,
        }
        self.env_en_driver = {
            "TWITTER_SIN_PROXY": os.environ.get("TWITTER_SIN_PROXY"),
            "CHROME_SIN_IMAGENES": os.environ.get("CHROME_SIN_IMAGENES"),
        }
        return True

    def preparar_sesion_cdp(self):
        return self.usuario not in getattr(type(self), "fallan_sesion", set())

    def login_con_cookies(self):
        return self.usuario not in getattr(type(self), "fallan_sesion", set())

    def refrescar_pagina_rapido(self, timeout=3.0):
        self.refrescar_llamadas += 1
        return "ok"

    def cambiar_cuenta(self, usuario, refrescar=True):
        self.refrescos_solicitados.append(refrescar)
        self.cambio_llamadas.append(usuario)
        self.usuario = usuario
        self.ultima_url_publicada = ""
        if usuario in getattr(type(self), "fallan_cambio", set()):
            self.ultimo_error = "cambio de sesion fallido"
            return False
        return True

    def esta_vivo(self):
        return not getattr(type(self), "muerto", False)

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True):
        self.posts.append(
            {
                "texto": texto,
                "imagen_path": imagen_path,
                "buscar_url": buscar_url,
                "usuario": self.usuario,
            }
        )
        if getattr(type(self), "post_devuelve_none", False):
            self.ultimo_error = "fallo simulado de publicacion"
            return None
        numero = len(self.posts) + 1000
        url = f"https://x.com/{self.usuario}/status/{numero}"
        self.ultima_url_publicada = url
        return url

    def solo_retwittear(self, targets, usuario, mensaje_cita=None, dar_like=False, imagen_path=None, buscar_url=True):
        self.citas.append(
            {
                "targets": list(targets),
                "usuario": usuario,
                "mensaje_cita": mensaje_cita,
                "dar_like": dar_like,
                "buscar_url": buscar_url,
            }
        )
        if not buscar_url:
            # El bot real NO busca el link de la cita cuando buscar_url=False.
            self.ultima_url_publicada = ""
            return {"exitos": 1, "fallidos": 0, "urls": []}
        urls = []
        if getattr(type(self), "devolver_ancla", False):
            urls = [targets[0]] if targets else []
        else:
            urls = [f"https://x.com/{usuario}/status/777"]
        self.ultima_url_publicada = urls[0] if urls else ""
        return {"exitos": 1, "fallidos": 0, "urls": urls}

    def cerrar(self):
        self.cerrado = True


class _BotSinImagen(_BotBase):
    def publicar_tweet(self, texto, buscar_url=True):
        self.posts.append(
            {
                "texto": texto,
                "imagen_path": "SIN_KWARG",
                "buscar_url": buscar_url,
                "usuario": self.usuario,
            }
        )
        url = f"https://x.com/{self.usuario}/status/4242"
        self.ultima_url_publicada = url
        return url


class _BotConPerfil(_BotBase):
    """Bot fake NUEVO: acepta `buscar_url_perfil` y registra si se paso.

    El default `None` permite distinguir "no se paso el kwarg" (None) de
    "se paso False" (no visitar el perfil) o "se paso True".
    """

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True,
                       buscar_url_perfil=None):
        respuesta = super().publicar_tweet(
            texto, imagen_path=imagen_path, buscar_url=buscar_url
        )
        self.posts[-1]["buscar_url_perfil"] = buscar_url_perfil
        return respuesta

    def solo_retwittear(self, targets, usuario, mensaje_cita=None,
                        dar_like=False, imagen_path=None, buscar_url=True,
                        buscar_url_perfil=None):
        respuesta = super().solo_retwittear(
            targets,
            usuario,
            mensaje_cita=mensaje_cita,
            dar_like=dar_like,
            imagen_path=imagen_path,
            buscar_url=buscar_url,
        )
        self.citas[-1]["buscar_url_perfil"] = buscar_url_perfil
        return respuesta


def _clase_bot(base=_BotBase, **atributos):
    clase = type(f"FakeBot_{len(base.creados)}", (base,), {})
    clase.creados = []
    for nombre, valor in atributos.items():
        setattr(clase, nombre, valor)
    return clase


class _FwdProxyFalso:
    """Forwarder fake: devuelve `estadisticas()` con bytes fijos."""

    def __init__(self, subida=0, bajada=0):
        self.subida = int(subida)
        self.bajada = int(bajada)

    def estadisticas(self):
        return {
            "subida": self.subida,
            "bajada": self.bajada,
            "total": self.subida + self.bajada,
        }


class _BotConProxy(_BotBase):
    """Fake NUEVO: proxy dinamico, cambio con `proxy=`, sesion guardable y anti-bot."""

    guardados = 0
    anti_bot_llamadas = 0
    anti_bot_restante = 0
    bytes_subida = 0
    bytes_bajada = 0

    def __init__(self, usuario, modo_local=False):
        super().__init__(usuario, modo_local=modo_local)
        self.proxy_llamadas = []
        self.guardados_bot = 0
        self.anti_bot_restante = int(type(self).anti_bot_restante)
        self._fwd_proxy = _FwdProxyFalso(
            subida=type(self).bytes_subida,
            bajada=type(self).bytes_bajada,
        )

    def cambiar_cuenta(self, usuario, proxy="", refrescar=True):
        self.proxy_llamadas.append(proxy)
        self.refrescos_solicitados.append(refrescar)
        self.cambio_llamadas.append(usuario)
        self.usuario = usuario
        self.ultima_url_publicada = ""
        return True

    def guardar_sesion_actual(self):
        type(self).guardados += 1
        self.guardados_bot += 1
        return True

    def es_pagina_anti_bot(self):
        type(self).anti_bot_llamadas += 1
        if self.anti_bot_restante > 0:
            self.anti_bot_restante -= 1
            return True
        return False


class _BotAntiBot(_BotConProxy):
    """Fake: las primeras N publicaciones fallan con reto anti-bot."""

    fallos_antibot = 0

    def publicar_tweet(self, texto, imagen_path=None, buscar_url=True):
        if type(self).fallos_antibot > 0:
            type(self).fallos_antibot -= 1
            self.ultimo_error = (
                "X pidió verificación anti-bot (Cloudflare); sesión no confirmada"
            )
            return None
        return super().publicar_tweet(
            texto, imagen_path=imagen_path, buscar_url=buscar_url
        )


@contextlib.contextmanager
def _campana(
    cuentas,
    no_encontradas=None,
    usados=None,
    bot_cls=None,
    posts_fn=None,
    variaciones_fn=None,
    leer_fn=None,
):
    registro_llamadas = []
    reloj = RelojFalso()

    def _leer(usuarios=None):
        if leer_fn is not None:
            return leer_fn(usuarios)
        return list(cuentas), list(no_encontradas or []), ""

    def _contar(usuarios):
        return dict(usados or {})

    def _registrar(usuario, tipo, estado, url="", detalle=""):
        registro_llamadas.append(
            {
                "usuario": usuario,
                "tipo": tipo,
                "estado": estado,
                "url": url,
                "detalle": detalle,
            }
        )

    def _posts(info, **kwargs):
        if posts_fn is not None:
            return posts_fn(info, **kwargs)
        n = int(kwargs.get("n_por_cuenta", 1) or 1)
        return {
            cuenta["usuario"]: [
                f"Texto de prueba {indice} de {cuenta['usuario']}"
                for indice in range(n)
            ]
            for cuenta in info
        }

    def _variaciones(base, cantidad, **kwargs):
        if variaciones_fn is not None:
            return variaciones_fn(base, cantidad, **kwargs)
        return [f"{base} variante {indice}" for indice in range(int(cantidad))]

    cambios = [
        (local_mod, "_leer_cuentas", _leer),
        (local_mod, "time", reloj),
        (local_mod, "TwitterBot", bot_cls or _clase_bot()),
        (registro_mod, "contar_acciones_dia_por_usuario", _contar),
        (registro_mod, "registrar_accion", _registrar),
        (generador_mod, "generar_textos_hashtags_por_cuenta", _posts),
        (variaciones_mod, "generar_pool_variaciones_openai", _variaciones),
    ]
    with _parches(*cambios):
        yield {"registros": registro_llamadas, "reloj": reloj}


def _limpiar_abiertos():
    local_mod.cerrar_navegadores_locales()


def run(check):
    _limpiar_abiertos()

    cuentas_base = [
        _cuenta("tier1_a"),
        _cuenta("tier2_b", tier="tier2"),
        _cuenta("tier3_c", tier="tier3"),
        _cuenta("sin_tier_d", tier=""),
        _cuenta("sin_sesion_e", auth="", cookies_json=None, cookies_path=""),
        _cuenta("cookies_f", auth="", cookies_json=[{"name": "auth_token"}]),
    ]

    with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as archivo:
        archivo.write(b"cookies")
        ruta_cookies = archivo.name
    try:
        cuentas_base.append(
            _cuenta("path_g", auth="", cookies_json=None, cookies_path=ruta_cookies)
        )
        cuentas_base.append(_cuenta("pausada_h", pausada=True))
        cuentas_base.append(_cuenta("tier1_i"))

        seleccion = local_mod._seleccionar_cuentas(
            cuentas_base, None, False, 10
        )
        nombres = [cuenta["usuario"] for cuenta in seleccion["selected"]]
        check(
            "seleccion: solo entra Tier 1",
            "tier1_a" in nombres and "tier1_i" in nombres,
        )
        check(
            "seleccion: omite Tier 2, Tier 3 y sin tier",
            seleccion["omitidas_tier"] == ["tier2_b", "tier3_c", "sin_tier_d"],
        )
        check(
            "seleccion: omite cuentas sin sesion real",
            seleccion["omitidas_sin_sesion"] == ["sin_sesion_e"],
        )
        check(
            "seleccion: acepta auth_token, cookies_json y cookies_path existente",
            {"tier1_a", "cookies_f", "path_g"}.issubset(set(nombres)),
        )
        check(
            "seleccion: omite pausadas por defecto",
            seleccion["omitidas_pausadas"] == ["pausada_h"]
            and "pausada_h" not in nombres,
        )

        con_pausadas = local_mod._seleccionar_cuentas(
            cuentas_base, None, True, 10
        )
        check(
            "seleccion: permitir_pausadas incluye la cuenta pausada",
            "pausada_h"
            in [cuenta["usuario"] for cuenta in con_pausadas["selected"]],
        )

        ordenadas = local_mod._seleccionar_cuentas(
            cuentas_base, ["tier1_i", "tier1_a", "no_existe"], False, 10
        )
        check(
            "seleccion: respeta el orden de usuarios y filtra inexistentes",
            [cuenta["usuario"] for cuenta in ordenadas["selected"]]
            == ["tier1_i", "tier1_a"],
        )
        check(
            "seleccion: lista de usuarios vacia no selecciona a nadie",
            local_mod._seleccionar_cuentas(
                cuentas_base, [], False, 10
            )["selected"]
            == [],
        )

        recortadas = local_mod._seleccionar_cuentas(
            [
                _cuenta("r1"),
                _cuenta("r2"),
                _cuenta("r3"),
                _cuenta("r4"),
                _cuenta("r5"),
                _cuenta("r6"),
            ],
            None,
            False,
            5,
        )
        check(
            "seleccion: tope de 5 cuentas y recorte reportado",
            len(recortadas["selected"]) == 5 and recortadas["recortadas"] == 1,
        )

        sin_tope = local_mod._seleccionar_cuentas(
            [_cuenta(f"st{i}") for i in range(6)],
            None,
            False,
            0,
        )
        check(
            "seleccion: max_cuentas=0 (sin tope) no recorta",
            len(sin_tope["selected"]) == 6
            and sin_tope["recortadas"] == 0
            and sin_tope["recortadas_usuarios"] == [],
            f"({len(sin_tope['selected'])}, {sin_tope['recortadas']})",
        )
        negativo = local_mod._seleccionar_cuentas(
            [_cuenta(f"neg{i}") for i in range(6)],
            None,
            False,
            -5,
        )
        check(
            "seleccion: max_cuentas negativo tampoco recorta",
            len(negativo["selected"]) == 6 and negativo["recortadas"] == 0,
        )

        urls = ["https://x.com/ancla/status/1"]
        plan_cuota = local_mod._construir_plan(
            [
                _cuenta("q1"),
                _cuenta("q2"),
                _cuenta("q3"),
            ],
            ["hashtags", "cita"],
            6,
            {"q1": 47},
            49,
            urls,
        )
        check(
            "cuota: con 47 usadas planifica solo 2 acciones",
            len(plan_cuota["por_cuenta"]["q1"]) == 2,
        )
        check(
            "cuota: la cuenta con 2 cupos alterna hashtags y cita",
            [a["rol"] for a in plan_cuota["por_cuenta"]["q1"]]
            == ["hashtags", "cita"],
        )
        plan_agotada = local_mod._construir_plan(
            [_cuenta("q1"), _cuenta("q2")],
            ["hashtags"],
            6,
            {"q1": 49},
            49,
            [],
        )
        check(
            "cuota: con 49 usadas omite la cuenta por cuota",
            plan_agotada["omitidas_cuota"] == ["q1"]
            and plan_agotada["por_cuenta"]["q1"] == [],
        )
        plan_ilimitado = local_mod._construir_plan(
            [_cuenta("q1")], ["hashtags"], 6, {"q1": 49}, 0, []
        )
        check(
            "cuota: limite_dia=0 es ilimitado",
            len(plan_ilimitado["por_cuenta"]["q1"]) == 6,
        )
        plan_recorte_cupo = local_mod._construir_plan(
            [_cuenta("q1")], ["hashtags"], 6, {"q1": 45}, 49, []
        )
        check(
            "cuota: acciones_por_cuenta se recorta al cupo restante",
            len(plan_recorte_cupo["por_cuenta"]["q1"]) == 4,
        )

        plan_alterno = local_mod._construir_plan(
            [_cuenta("p1"), _cuenta("p2")],
            ["hashtags", "cita"],
            4,
            {},
            49,
            urls,
        )
        roles_p1 = [a["rol"] for a in plan_alterno["por_cuenta"]["p1"]]
        check(
            "plan: alterna hashtags/cita por cuenta",
            roles_p1 == ["hashtags", "cita", "hashtags", "cita"],
        )
        orden_plan = [a["usuario"] for a in plan_alterno["plan"]]
        rondas_plan = [
            orden_plan[inicio:inicio + 2]
            for inicio in range(0, len(orden_plan), 2)
        ]
        check(
            "plan: cada ronda tiene las 2 cuentas (orden aleatorio por ciclo)",
            len(orden_plan) == 8
            and all(
                len(set(ronda)) == 2 and set(ronda) == set(orden_plan[:2])
                for ronda in rondas_plan
            )
            and len(rondas_plan) == 4,
            f"({rondas_plan})",
        )
        check(
            "plan: planificadas coincide con len(plan)",
            len(plan_alterno["plan"]) == 8,
        )
    finally:
        try:
            os.unlink(ruta_cookies)
        except OSError:
            pass

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("u1"), _cuenta("u2")]) as entorno:
        res = local_mod.ejecutar_campana_local(
            usuarios=["u1", "u2"],
            hashtags="#mexico",
            urls=["https://x.com/ancla/status/1"],
            acciones_por_cuenta=2,
            duracion_min=2,
        )
        roles = [evento["rol"] for evento in res["eventos"]]
        check(
            "campana: con material y urls alterna hashtags y cita",
            "hashtags" in roles and "cita" in roles,
        )
        check(
            "campana: sin RT simple ni likes (solo hashtags/cita)",
            set(roles).issubset({"hashtags", "cita"}),
        )
        check(
            "campana: resumen con claves obligatorias",
            {
                "ok",
                "error",
                "motivo",
                "cancelada",
                "inicio",
                "fin",
                "segundos",
                "modo_local",
                "navegador_visible",
                "sin_proxy",
                "cuentas",
                "cuentas_total",
                "cuentas_seleccionadas",
                "cuentas_usadas",
                "limite_dia",
                "acciones_por_cuenta",
                "duracion_min",
                "planificadas",
                "hechas",
                "exitosas",
                "fallidas",
                "omitidas",
                "omitidas_tier",
                "omitidas_sin_sesion",
                "omitidas_pausadas",
                "omitidas_por_cuota",
                "omitidas_por_tiempo",
                "omitidas_usuarios",
                "recortadas",
                "por_cuenta",
                "eventos",
                "imagenes_disponibles",
                "probabilidad_imagen",
                "sin_imagenes",
                "navegadores_creados",
                "navegador_abierto",
            }.issubset(res.keys()),
        )
        check("campana: modo local visible y sin proxy", res["modo_local"] is True and res["navegador_visible"] is True and res["sin_proxy"] is True)
        check(
            "campana: planificadas y hechas cuadran",
            res["planificadas"] == 4 and res["hechas"] == 4,
        )
        check(
            "campana: cuentas usadas y por_cuenta completos",
            res["cuentas_usadas"] == 2
            and all(
                fila["hechas"] > 0 for fila in res["por_cuenta"].values()
            ),
        )
        try:
            json.dumps(res, ensure_ascii=False)
            serializable = True
        except Exception:
            serializable = False
        check("campana: resumen serializable a JSON", serializable)
        check(
            "campana: eventos con las claves del callback",
            all(
                {"usuario", "rol", "ok", "detalle", "url"}.issubset(evento)
                for evento in res["eventos"]
            ),
        )
        check(
            "campana: primer evento es inmediato (sin sleep previo)",
            entorno["reloj"].suenos
            and max(entorno["reloj"].suenos) <= 0.5
            and res["eventos"],
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("solo_url")]) as entorno:
        res = local_mod.ejecutar_campana_local(
            usuarios=["solo_url"],
            urls=["https://x.com/ancla/status/9"],
            texto_base="Texto base de la cita",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        check(
            "plan: solo urls (sin material) hace solo citas",
            res["hechas"] == 2
            and all(evento["rol"] == "cita" for evento in res["eventos"]),
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("solo_post")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["solo_post"],
            hashtags="#mexico #futbol",
            contexto="el partido de ayer",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        check(
            "plan: solo material (sin urls) hace solo posts",
            res["hechas"] == 2
            and all(evento["rol"] == "hashtags" for evento in res["eventos"]),
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("narrativa")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["narrativa"],
            narrativa="una noticia de trasfondo que no se debe copiar",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "plan: narrativa sola habilita posts (trasfondo invisible)",
            res["ok"] is True and res["hechas"] == 1,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("nada")]) as entorno:
        res = local_mod.ejecutar_campana_local(usuarios=["nada"])
        check(
            "plan: sin urls ni material devuelve error claro",
            res["ok"] is False and "urls" in res["error"].lower(),
        )
        check(
            "plan: error sin material no crea navegador",
            res["navegadores_creados"] == 0
            and entorno["reloj"].suenos == [],
        )

    evento_pre = threading.Event()
    evento_pre.set()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cancel_pre")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cancel_pre"],
            hashtags="#mexico",
            acciones_por_cuenta=3,
            duracion_min=5,
            cancelar=evento_pre,
        )
        check(
            "cancelacion: evento pre-seteado no ejecuta nada",
            res["cancelada"] is True and res["hechas"] == 0,
        )
        check(
            "cancelacion: pre-seteado no crea navegador",
            res["navegadores_creados"] == 0,
        )
        check(
            "cancelacion: pre-seteado marca pendientes por tiempo",
            res["omitidas_por_tiempo"] == res["planificadas"] == 3,
        )

    evento_medio = threading.Event()

    def _callback_corta(evento):
        if evento["hechas"] >= 2:
            evento_medio.set()

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cancel_medio")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cancel_medio"],
            hashtags="#mexico",
            acciones_por_cuenta=6,
            duracion_min=5,
            cancelar=evento_medio,
            callback=_callback_corta,
        )
        check(
            "cancelacion: paro a mitad corta la campana",
            res["cancelada"] is True and res["hechas"] == 2,
        )
        check(
            "cancelacion: pendientes se reportan por tiempo",
            res["omitidas_por_tiempo"] == 4,
        )

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as imagen:
        imagen.write(b"\x89PNG\r\n\x1a\n")
        ruta_imagen = imagen.name
    try:
        clase = _clase_bot()
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ), _campana([_cuenta("img1")], bot_cls=clase):
            res = local_mod.ejecutar_campana_local(
                usuarios=["img1"],
                hashtags="#mexico #futbol",
                imagenes=[ruta_imagen],
                probabilidad_imagen=100,
                acciones_por_cuenta=1,
                duracion_min=1,
            )
            bot = clase.creados[0]
            check(
                "imagen: probabilidad 100 envia la ruta al post",
                bot.posts and bot.posts[0]["imagen_path"] == ruta_imagen,
            )
            check(
                "imagen: resumen reporta imagen disponible",
                res["imagenes_disponibles"] == 1 and res["sin_imagenes"] is False,
            )
            texto = bot.posts[0]["texto"]
            check(
                "post: texto <=280 con los hashtags en medio",
                len(texto) <= 280
                and validar_texto_hashtag(texto, "#mexico #futbol", 280)["ok"],
            )
    finally:
        try:
            os.unlink(ruta_imagen)
        except OSError:
            pass

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as imagen:
        imagen.write(b"\x89PNG\r\n\x1a\n")
        ruta_imagen = imagen.name
    try:
        clase = _clase_bot()
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ), _campana([_cuenta("img0")], bot_cls=clase):
            res = local_mod.ejecutar_campana_local(
                usuarios=["img0"],
                hashtags="#mexico",
                imagenes=[ruta_imagen],
                probabilidad_imagen=0,
                acciones_por_cuenta=1,
                duracion_min=1,
            )
            bot = clase.creados[0]
            check(
                "imagen: probabilidad 0 publica sin imagen",
                bot.posts and bot.posts[0]["imagen_path"] is None,
            )
            check(
                "imagen: probabilidad 0 conserva la imagen disponible",
                res["imagenes_disponibles"] == 1,
            )
    finally:
        try:
            os.unlink(ruta_imagen)
        except OSError:
            pass

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as inexistente:
        ruta_borrada = inexistente.name
    os.unlink(ruta_borrada)
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("img_inexistente")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["img_inexistente"],
            hashtags="#mexico",
            imagenes=[ruta_borrada],
            probabilidad_imagen=100,
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "imagen: archivo inexistente no se usa y se reporta",
            res["imagenes_disponibles"] == 0 and res["sin_imagenes"] is True,
        )

    clase = _clase_bot()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cita1")], bot_cls=clase):
        ancla = "https://x.com/ancla/status/55"
        res = local_mod.ejecutar_campana_local(
            usuarios=["cita1"],
            urls=[ancla],
            texto_base="Texto base de la cita",
            dar_like=True,
            acciones_por_cuenta=1,
            duracion_min=1,
            capturar_url=True,
        )
        bot = clase.creados[0]
        cita = bot.citas[0]
        check(
            "cita: recibe una sola URL ancla",
            cita["targets"] == [ancla],
        )
        check(
            "cita: recibe mensaje_cita no vacio",
            bool(cita["mensaje_cita"] and cita["mensaje_cita"].strip()),
        )
        check("cita: recibe dar_like=True", cita["dar_like"] is True)
        check(
            "cita: URL registrada es la del post de la cita y no el ancla",
            res["eventos"]
            and res["eventos"][0]["url"].endswith("/status/777")
            and res["eventos"][0]["url"] != ancla,
        )
        check(
            "cita: mensaje de cita <=280",
            len(cita["mensaje_cita"]) <= 280,
        )

    clase = _clase_bot(devolver_ancla=True)
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cita_ancla")], bot_cls=clase):
        ancla = "https://x.com/ancla/status/66"
        res = local_mod.ejecutar_campana_local(
            usuarios=["cita_ancla"],
            urls=[ancla],
            texto_base="Otra cita",
            acciones_por_cuenta=1,
            duracion_min=1,
            capturar_url=True,
        )
        check(
            "cita: nunca usa el ancla como URL publicada",
            res["eventos"][0]["url"] == "",
        )

    clase = _clase_bot()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta("cam_a"), _cuenta("cam_b")], bot_cls=clase
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cam_a", "cam_b"],
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        bot = clase.creados[0]
        check(
            "cuentas: un solo navegador persistente",
            len(clase.creados) == 1,
        )
        check(
            "cuentas: cambia de cuenta en caliente entre acciones",
            len(bot.cambio_llamadas) >= 1
            and set(bot.cambio_llamadas).issubset({"cam_a", "cam_b"}),
        )

    clase = _clase_bot(fallan_sesion={"falla"}, fallan_cambio={"falla"})
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("falla"), _cuenta("sigue")], bot_cls=clase):
        res = local_mod.ejecutar_campana_local(
            usuarios=["falla", "sigue"],
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        check(
            "sesion: una cuenta sin sesion no aborta a las demas",
            res["hechas"] >= 1
            and any(evento["usuario"] == "sigue" for evento in res["eventos"]),
        )
        check(
            "sesion: la cuenta caida se reporta en omitidas_sin_sesion",
            "falla" in res["omitidas_usuarios"]["sin_sesion"],
        )

    clase = _clase_bot(_BotSinImagen)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as imagen:
        imagen.write(b"\x89PNG\r\n\x1a\n")
        ruta_imagen = imagen.name
    try:
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ), _campana([_cuenta("fallback_kw")], bot_cls=clase):
            res = local_mod.ejecutar_campana_local(
                usuarios=["fallback_kw"],
                hashtags="#mexico",
                imagenes=[ruta_imagen],
                probabilidad_imagen=100,
                acciones_por_cuenta=1,
                duracion_min=1,
            )
            check(
                "post: TypeError por imagen cae al fallback sin imagen",
                res["exitosas"] == 1
                and clase.creados[0].posts[0]["imagen_path"] == "SIN_KWARG",
            )
    finally:
        try:
            os.unlink(ruta_imagen)
        except OSError:
            pass

    clase = _clase_bot(post_devuelve_none=True)
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("falla_post")], bot_cls=clase) as entorno:
        res = local_mod.ejecutar_campana_local(
            usuarios=["falla_post"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "post: publicacion fallida se cuenta como fallida",
            res["fallidas"] == 1 and res["exitosas"] == 0,
        )
        check(
            "post: fallo no registra una URL ajena",
            res["eventos"][0]["url"] == "",
        )
        check(
            "registro: intento fallido con estado fallido",
            entorno["registros"]
            and entorno["registros"][0]["estado"] == "fallido",
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("reg1")]) as entorno:
        res = local_mod.ejecutar_campana_local(
            usuarios=["reg1"],
            hashtags="#mexico",
            urls=["https://x.com/ancla/status/2"],
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        tipos = {fila["tipo"] for fila in entorno["registros"]}
        check(
            "registro: usa tipos post y cita",
            tipos == {"post", "cita"},
        )
        check(
            "registro: todos los intentos exitosos se registran como exito",
            all(fila["estado"] == "exito" for fila in entorno["registros"]),
        )
        check(
            "registro: un registro por accion ejecutada",
            len(entorno["registros"]) == res["hechas"],
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cb1")]):
        recibidos = []

        def _cb(evento):
            recibidos.append(evento)
            if evento["hechas"] == 1:
                raise RuntimeError("callback que falla")

        res = local_mod.ejecutar_campana_local(
            usuarios=["cb1"],
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
            callback=_cb,
        )
        check(
            "callback: se llama una vez por accion",
            len(recibidos) == res["hechas"] == 2,
        )
        check(
            "callback: evento con hechas/total/usuario/rol/ok/detalle/url",
            all(
                {
                    "hechas",
                    "total",
                    "usuario",
                    "rol",
                    "ok",
                    "detalle",
                    "url",
                }.issubset(evento)
                for evento in recibidos
            ),
        )
        check(
            "callback: una excepcion no tumba la campana",
            res["ok"] is True and res["hechas"] == 2,
        )

    previos = {
        clave: os.environ.get(clave)
        for clave in (
            "TWITTER_SIN_PROXY",
            "API_PRIMERO",
            "RT_POR_API",
            "CHROME_SIN_IMAGENES",
            "MODO_PESTANA",
        )
    }
    for clave in previos:
        os.environ[clave] = "previo"
    clase_settings = _clase_bot()
    try:
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=True
        ), _campana([_cuenta("env1")], bot_cls=clase_settings):
            res = local_mod.ejecutar_campana_local(
                usuarios=["env1"],
                hashtags="#mexico",
                acciones_por_cuenta=1,
                duracion_min=1,
            )
            check(
                "settings: headless se restaura al terminar",
                settings.headless is True,
            )
            check(
                "settings: durante la campana headless se fuerza a False",
                clase_settings.creados
                and clase_settings.creados[0].headless_en_creacion is False,
            )
        check(
            "env: TWITTER_SIN_PROXY se restaura",
            os.environ.get("TWITTER_SIN_PROXY") == "previo",
        )
        check(
            "env: API_PRIMERO se restaura",
            os.environ.get("API_PRIMERO") == "previo",
        )
        check(
            "env: RT_POR_API se restaura",
            os.environ.get("RT_POR_API") == "previo",
        )
        check(
            "env: CHROME_SIN_IMAGENES se restaura",
            os.environ.get("CHROME_SIN_IMAGENES") == "previo",
        )
        check(
            "env: MODO_PESTANA se restaura",
            os.environ.get("MODO_PESTANA") == "previo",
        )
    finally:
        for clave, valor in previos.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor

    def _leer_roto(usuarios=None):
        raise RuntimeError("BD rota")

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([], leer_fn=_leer_roto):
        res = local_mod.ejecutar_campana_local(
            usuarios=["x"], hashtags="#mexico"
        )
        check(
            "errores: una excepcion inesperada devuelve ok=False sin lanzar",
            res["ok"] is False and "BD rota" in res["error"],
        )
        check(
            "errores: los env se restauran incluso con excepcion",
            os.environ.get("TWITTER_SIN_PROXY") in (None, "previo")
            and res["navegadores_creados"] == 0,
        )

    clase = _clase_bot()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("abierto")], bot_cls=clase):
        res = local_mod.ejecutar_campana_local(
            usuarios=["abierto"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
            dejar_abierto=True,
        )
        bot = clase.creados[0]
        check(
            "dejar_abierto: navegador abierto y registrado",
            res["navegador_abierto"] is True and bot.cerrado is False,
        )
        cerrados = local_mod.cerrar_navegadores_locales()
        check(
            "dejar_abierto: cerrar_navegadores_locales cierra y cuenta",
            cerrados == 1 and bot.cerrado is True,
        )
        check(
            "dejar_abierto: segunda llamada devuelve 0",
            local_mod.cerrar_navegadores_locales() == 0,
        )

    def _posts_roto(info, **kwargs):
        raise RuntimeError("IA caida")

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("ia_caida")], posts_fn=_posts_roto):
        res = local_mod.ejecutar_campana_local(
            usuarios=["ia_caida"],
            hashtags="#mexico #futbol",
            contexto="tema manual",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        clase_bot = local_mod.TwitterBot
        textos = [post["texto"] for post in clase_bot.creados[0].posts]
        check(
            "IA caida: los posts siguen teniendo texto",
            res["exitosas"] == 2 and all(textos),
        )
        check(
            "IA caida: textos <=280 con los hashtags pedidos",
            all(
                len(texto) <= 280
                and validar_texto_hashtag(texto, "#mexico #futbol", 280)["ok"]
                for texto in textos
            ),
        )

    def _variaciones_roto(base, cantidad, **kwargs):
        raise RuntimeError("variaciones caidas")

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("var_caida")], variaciones_fn=_variaciones_roto):
        res = local_mod.ejecutar_campana_local(
            usuarios=["var_caida"],
            urls=["https://x.com/ancla/status/3"],
            texto_base="Texto base firme",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        clase_bot = local_mod.TwitterBot
        mensajes = [cita["mensaje_cita"] for cita in clase_bot.creados[0].citas]
        check(
            "IA caida: las citas nunca quedan sin texto",
            res["exitosas"] == 2 and all(mensajes),
        )
        check(
            "IA caida: citas <=280 con los hashtags pedidos",
            all(
                len(mensaje) <= 280
                and validar_texto_hashtag(mensaje, "#mexico", 280)["ok"]
                for mensaje in mensajes
            ),
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta(f"cap{i}") for i in range(6)]
    ):
        res = local_mod.ejecutar_campana_local(
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "tope: la campana usa como maximo local_max_cuentas",
            res["cuentas_seleccionadas"] == 5 and res["recortadas"] == 1,
        )
        check(
            "tope: cuentas_total refleja todas las leidas",
            res["cuentas_total"] == 6,
        )

    with _settings(
        local_max_cuentas=0, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta(f"sin_tope{i}") for i in range(6)]
    ):
        res = local_mod.ejecutar_campana_local(
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        esperadas = [f"sin_tope{i}" for i in range(6)]
        check(
            "tope: local_max_cuentas=0 usa las 6 cuentas (sin tope)",
            res["cuentas_seleccionadas"] == 6
            and res["recortadas"] == 0
            and res["cuentas"] == esperadas,
            f"({res['cuentas_seleccionadas']}, {res['recortadas']}, {res['cuentas']})",
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta("cuota_llena")], usados={"cuota_llena": 49}
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cuota_llena"],
            hashtags="#mexico",
            acciones_por_cuenta=6,
            duracion_min=1,
        )
        check(
            "cuota diaria: cuenta con 49 usadas se omite en la campana",
            res["omitidas_por_cuota"] == 1
            and res["planificadas"] == 0
            and res["navegadores_creados"] == 0,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cuota_2")], usados={"cuota_2": 47}):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cuota_2"],
            hashtags="#mexico",
            acciones_por_cuenta=6,
            duracion_min=1,
        )
        check(
            "cuota diaria: con 47 usadas solo planifica 2",
            res["planificadas"] == 2 and res["hechas"] == 2,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=0, headless=False
    ), _campana([_cuenta("cuota_libre")], usados={"cuota_libre": 49}):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cuota_libre"],
            hashtags="#mexico",
            acciones_por_cuenta=4,
            limite_dia=0,
            duracion_min=1,
        )
        check(
            "cuota diaria: limite_dia=0 es ilimitado en la campana",
            res["planificadas"] == 4 and res["hechas"] == 4,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("recorte_cupo")], usados={"recorte_cupo": 45}):
        res = local_mod.ejecutar_campana_local(
            usuarios=["recorte_cupo"],
            hashtags="#mexico",
            acciones_por_cuenta=6,
            duracion_min=1,
        )
        check(
            "cuota diaria: acciones_por_cuenta se recorta al cupo",
            res["planificadas"] == 4,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("clamp")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["clamp"],
            hashtags="#mexico",
            acciones_por_cuenta=999,
            duracion_min=999,
            probabilidad_imagen=999,
        )
        check(
            "limites: acciones_por_cuenta se limita a 49",
            res["planificadas"] == 49 and res["acciones_por_cuenta"] == 49,
        )
        check(
            "limites: duracion se limita a 180 minutos",
            res["duracion_min"] == 180,
        )
        check(
            "limites: probabilidad se limita a 100",
            res["probabilidad_imagen"] == 100,
        )
        check(
            "eventos: se guardan como maximo 200",
            len(res["eventos"]) <= 200,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta("orden_b"), _cuenta("orden_a")], no_encontradas=["fantasma"]
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["orden_a", "orden_b", "fantasma"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "seleccion: respeta el orden de usuarios en el resumen",
            res["cuentas"] == ["orden_a", "orden_b"],
        )
        check(
            "seleccion: usuario inexistente se reporta",
            res["omitidas_usuarios"]["no_encontradas"] == ["fantasma"],
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("rep1"), _cuenta("rep2")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["rep1", "rep2"],
            hashtags="#mexico",
            acciones_por_cuenta=3,
            duracion_min=1,
        )
        fila1 = res["por_cuenta"]["rep1"]
        check(
            "resumen: por_cuenta cuadra planificadas/hechas/exitosas/fallidas",
            fila1["planificadas"] == 3
            and fila1["hechas"] == 3
            and fila1["exitosas"] == 3
            and fila1["fallidas"] == 0,
        )
        check(
            "resumen: omitidas = planificadas - hechas",
            res["omitidas"] == res["planificadas"] - res["hechas"],
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("tipo_vacio", tipo="", perfil="")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["tipo_vacio"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "textos: cuenta sin registro ni perfil publica igual",
            res["exitosas"] == 1,
        )

    # ------------------------------------------------------------------ #
    # Fixes 2026-10-21: textos de 280, hashtag autodetectado y velocidad.
    # ------------------------------------------------------------------ #
    def _posts_largos(info, **kwargs):
        return {
            cuenta["usuario"]: [
                ("A" * 60) + " #mexico " + ("B" * 120)
            ]
            for cuenta in info
        }

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("texto_280")], posts_fn=_posts_largos):
        res = local_mod.ejecutar_campana_local(
            usuarios=["texto_280"],
            hashtags="#mexico",
            contexto="tema",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        texto_largo = local_mod.TwitterBot.creados[0].posts[0]["texto"]
        check(
            "texto: el limite local es 280 (no 100)",
            res["limite_texto"] == 280,
        )
        check(
            "texto: un texto de ~190 chars NO se corta a 100",
            len(texto_largo) > 150
            and validar_texto_hashtag(texto_largo, "#mexico", 280)["ok"],
            f"(len={len(texto_largo)})",
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("auto_hash")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["auto_hash"],
            contexto="Cada año, el Día de Muertos nos reúne. #Muuki2026",
            hashtags="",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        texto_auto = local_mod.TwitterBot.creados[0].posts[0]["texto"]
        check(
            "hashtag: se detecta #Muuki2026 del contexto si el campo esta vacio",
            res["hashtags_auto"] == ["#Muuki2026"]
            and "#Muuki2026" in res["hashtags"],
        )
        check(
            "hashtag: el post publicado lleva el # detectado",
            "#Muuki2026" in texto_auto,
            texto_auto,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("auto_base")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["auto_base"],
            urls=["https://x.com/ancla/status/9"],
            texto_base="Apoyemos esto #Muuki2026",
            hashtags="",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "hashtag: tambien se detecta del texto base",
            res["hashtags_auto"] == ["#Muuki2026"],
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("sin_link")]):
        ancla = "https://x.com/ancla/status/10"
        res = local_mod.ejecutar_campana_local(
            usuarios=["sin_link"],
            urls=[ancla],
            texto_base="Texto base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        bot = local_mod.TwitterBot.creados[0]
        check(
            "velocidad: capturar_url=False por defecto en posts",
            bot.posts and bot.posts[0]["buscar_url"] is False,
        )
        check(
            "velocidad: capturar_url=False por defecto en citas",
            bot.citas and bot.citas[0]["buscar_url"] is False,
        )
        check(
            "velocidad: sin captura la cita no registra link",
            res["eventos"] and res["eventos"][-1]["url"] == ""
            and res["capturar_url"] is False,
        )

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("con_link")]):
        ancla = "https://x.com/ancla/status/11"
        res = local_mod.ejecutar_campana_local(
            usuarios=["con_link"],
            urls=[ancla],
            texto_base="Texto base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
            capturar_url=True,
        )
        bot = local_mod.TwitterBot.creados[0]
        check(
            "velocidad: capturar_url=True se propaga a posts y citas",
            bot.posts and bot.posts[0]["buscar_url"] is True
            and bot.citas and bot.citas[0]["buscar_url"] is True,
        )
        check(
            "velocidad: con captura la cita registra link",
            res["eventos"] and res["eventos"][-1]["url"].endswith("/status/777"),
        )

    # --- NUEVO: capturar_url=True NO visita el perfil (kwarg nuevo) ---
    clase = _clase_bot(_BotConPerfil)
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("rapido")], bot_cls=clase):
        res = local_mod.ejecutar_campana_local(
            usuarios=["rapido"],
            urls=["https://x.com/ancla/status/20"],
            texto_base="Texto base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
            capturar_url=True,
        )
        bot = clase.creados[0]
        check(
            "perfil: capturar_url=True pasa buscar_url_perfil=False al post",
            res["ok"] is True
            and bot.posts
            and bot.posts[0].get("buscar_url_perfil") is False,
            f"({bot.posts[:1]})",
        )
        check(
            "perfil: capturar_url=True pasa buscar_url_perfil=False a la cita",
            bot.citas
            and bot.citas[0].get("buscar_url_perfil") is False,
            f"({bot.citas[:1]})",
        )
        check(
            "perfil: con capturar_url=True el bot sigue pidiendo buscar_url=True",
            bot.posts
            and bot.posts[0].get("buscar_url") is True
            and bot.citas
            and bot.citas[0].get("buscar_url") is True,
        )

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as imagen_tmp:
        ruta_perfil = imagen_tmp.name
    try:
        clase = _clase_bot(_BotConPerfil)
        with _settings(
            local_max_cuentas=5, limite_local_dia=49, headless=False
        ), _campana([_cuenta("rapido_img")], bot_cls=clase):
            res = local_mod.ejecutar_campana_local(
                usuarios=["rapido_img"],
                hashtags="#mexico",
                imagenes=[ruta_perfil],
                probabilidad_imagen=100,
                acciones_por_cuenta=1,
                duracion_min=1,
                capturar_url=True,
            )
            post = clase.creados[0].posts[0]
            check(
                "perfil: rama con imagen_path tambien evita el perfil",
                res["ok"] is True
                and post.get("imagen_path") == ruta_perfil
                and post.get("buscar_url") is True
                and post.get("buscar_url_perfil") is False,
                f"({post})",
            )
    finally:
        try:
            os.unlink(ruta_perfil)
        except OSError:
            pass

    clase = _clase_bot(_BotConPerfil)
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("clasico_perfil")], bot_cls=clase):
        res = local_mod.ejecutar_campana_local(
            usuarios=["clasico_perfil"],
            urls=["https://x.com/ancla/status/21"],
            texto_base="Texto base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        bot = clase.creados[0]
        check(
            "perfil: capturar_url=False NO agrega el kwarg nuevo (byte a byte)",
            res["ok"] is True
            and bot.posts
            and bot.posts[0].get("buscar_url") is False
            and bot.posts[0].get("buscar_url_perfil") is None
            and bot.citas
            and bot.citas[0].get("buscar_url") is False
            and bot.citas[0].get("buscar_url_perfil") is None,
            f"({bot.posts[:1]}, {bot.citas[:1]})",
        )

    clase = _clase_bot()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("viejo_perfil")], bot_cls=clase):
        res = local_mod.ejecutar_campana_local(
            usuarios=["viejo_perfil"],
            urls=["https://x.com/ancla/status/22"],
            texto_base="Texto base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
            capturar_url=True,
        )
        bot = clase.creados[0]
        check(
            "perfil: bot viejo sin el kwarg no lanza y conserva su llamada",
            res["ok"] is True
            and res["exitosas"] == 2
            and bot.posts
            and "buscar_url_perfil" not in bot.posts[0]
            and bot.citas
            and "buscar_url_perfil" not in bot.citas[0],
            f"(exitosas={res['exitosas']}, {bot.posts[:1]}, {bot.citas[:1]})",
        )

    firma_local = inspect.signature(local_mod.ejecutar_campana_local)
    check(
        "velocidad: pausa entre acciones default = 2s",
        firma_local.parameters["pausa_entre_acciones_seg"].default == 2,
        f"({firma_local.parameters['pausa_entre_acciones_seg'].default})",
    )
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("pausa_default")]):
        res_default = local_mod.ejecutar_campana_local(
            usuarios=["pausa_default"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        check(
            "velocidad: el resumen reporta la pausa default 2s",
            res_default["pausa_entre_acciones_seg"] == 2,
            f"({res_default['pausa_entre_acciones_seg']})",
        )

    clase = _clase_bot()
    with _settings(
        local_max_cuentas=0, limite_local_dia=49, headless=False
    ), _campana(
        [_cuenta("sin_refresh_a"), _cuenta("sin_refresh_b")],
        bot_cls=clase,
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["sin_refresh_a", "sin_refresh_b"],
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        refrescos = sum(bot.refrescar_llamadas for bot in clase.creados)
        solicitudes = [
            valor
            for bot in clase.creados
            for valor in bot.refrescos_solicitados
        ]
        check(
            "velocidad: la campana local NO llama refrescar_pagina_rapido",
            res["ok"] is True and refrescos == 0,
            f"(refrescos={refrescos})",
        )
        check(
            "velocidad: cambiar_cuenta recibe refrescar=False",
            bool(solicitudes) and all(valor is False for valor in solicitudes),
            f"({solicitudes})",
        )
        check(
            "velocidad: _ENV_LOCAL fuerza REFRESH_AL_ENTRAR y REFRESH_ENTRADA_MAX a 0",
            local_mod._ENV_LOCAL.get("REFRESH_AL_ENTRAR") == "0"
            and local_mod._ENV_LOCAL.get("REFRESH_ENTRADA_MAX") == "0",
        )

    objetivos_rapidos = local_mod._objetivos(30, 3600.0, 1000.0, 20)
    check(
        "velocidad: pausa 20s comprime el plan (no usa los 60 min)",
        objetivos_rapidos[1] - objetivos_rapidos[0] <= 25.0
        and objetivos_rapidos[-1] - objetivos_rapidos[0] < 900.0,
        f"(ultimo={objetivos_rapidos[-1] - objetivos_rapidos[0]:.0f}s)",
    )
    objetivos_clasicos = local_mod._objetivos(30, 3600.0, 1000.0, 0)
    check(
        "velocidad: pausa 0 conserva el reparto clasico en la ventana",
        objetivos_clasicos[-1] - objetivos_clasicos[0] > 3000.0,
    )
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("pausa")]):
        res_pausa = local_mod.ejecutar_campana_local(
            usuarios=["pausa"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
            pausa_entre_acciones_seg=120,
        )
        check(
            "velocidad: la pausa configurada se reporta en el resumen",
            res_pausa["pausa_entre_acciones_seg"] == 120,
        )

    def _variaciones_largas(base, cantidad, **kwargs):
        return [
            ("A" * 60) + " #mexico " + ("B" * 120)
        ] * int(cantidad)

    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("cita_280")], variaciones_fn=_variaciones_largas):
        res = local_mod.ejecutar_campana_local(
            usuarios=["cita_280"],
            urls=["https://x.com/ancla/status/12"],
            texto_base="Cita base",
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
        )
        cita_larga = local_mod.TwitterBot.creados[0].citas[0]["mensaje_cita"]
        check(
            "cita local: un texto de ~190 chars NO se corta a 100",
            len(cita_larga) > 100
            and len(cita_larga) <= 280
            and validar_texto_hashtag(cita_larga, "#mexico", 280)["ok"],
            f"(len={len(cita_larga)})",
        )

    # ------------------------------------------------------------------
    # Proxy sticky por cuenta + modo asistido de Cloudflare (2026-11-19)
    # ------------------------------------------------------------------
    firma_proxy = inspect.signature(local_mod.ejecutar_campana_local).parameters
    check(
        "firma: kwargs de proxy/captcha/navegadores AL FINAL y con defaults "
        "historicos",
        list(firma_proxy)[-5:]
        == [
            "usar_proxy", "proxies_pais", "sin_imagenes",
            "esperar_captcha_seg", "navegadores",
        ]
        and firma_proxy["usar_proxy"].default is False
        and firma_proxy["proxies_pais"].default == "mexico"
        and firma_proxy["sin_imagenes"].default is None
        and firma_proxy["esperar_captcha_seg"].default == 0
        and firma_proxy["navegadores"].default == 1,
        str(list(firma_proxy)[-5:]),
    )

    def _asignar_dos_proxies(usuarios, pais="mexico"):
        return (
            {
                usuario: f"http://proxy-{indice}:8080"
                for indice, usuario in enumerate(usuarios)
            },
            2,
            "",
        )

    clase_proxy = _clase_bot(
        _BotConProxy, bytes_subida=100, bytes_bajada=200
    )
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _parches(
        (local_mod, "_asignar_proxies_cuentas", _asignar_dos_proxies)
    ), _campana(
        [_cuenta("proxy_a"), _cuenta("proxy_b")], bot_cls=clase_proxy
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["proxy_a", "proxy_b"],
            hashtags="#mexico",
            acciones_por_cuenta=2,
            duracion_min=1,
            usar_proxy=True,
        )
        bot = clase_proxy.creados[0]
        proxys_usados = set(bot.proxy_llamadas)
        check(
            "proxy: iniciar_driver recibe proxy_dinamico=True y modo_local=True",
            bot.driver_args["proxy_dinamico"] is True
            and bot.driver_args["modo_local"] is True,
            str(bot.driver_args),
        )
        check(
            "proxy: durante la campana TWITTER_SIN_PROXY=0 y CHROME_SIN_IMAGENES=1",
            bot.env_en_driver.get("TWITTER_SIN_PROXY") == "0"
            and bot.env_en_driver.get("CHROME_SIN_IMAGENES") == "1",
            str(bot.env_en_driver),
        )
        check(
            "proxy: cambiar_cuenta recibe el proxy sticky de cada cuenta",
            proxys_usados
            == {"http://proxy-0:8080", "http://proxy-1:8080"},
            str(sorted(proxys_usados)),
        )
        check(
            "proxy: cambiar_cuenta conserva refrescar=False",
            bool(bot.refrescos_solicitados)
            and all(valor is False for valor in bot.refrescos_solicitados),
            str(bot.refrescos_solicitados),
        )
        check(
            "proxy: guardar_sesion_actual se llama tras CADA accion exitosa",
            res["exitosas"] == 4 and bot.guardados_bot == 4,
            f"({bot.guardados_bot})",
        )
        check(
            "proxy: resumen reporta usar_proxy/proxies asignados e invalidos",
            res["usar_proxy"] is True
            and res["sin_proxy"] is False
            and res["proxies_asignados"] == 2
            and res["proxies_invalidos"] == 2,
            f"({res['proxies_asignados']}/{res['proxies_invalidos']})",
        )
        check(
            "proxy: bytes del forwarder sumados al resumen",
            res["datos_proxy_subida"] == 100
            and res["datos_proxy_bajada"] == 200
            and res["datos_proxy_total"] == 300,
            f"({res['datos_proxy_subida']}/{res['datos_proxy_bajada']}/{res['datos_proxy_total']})",
        )

    def _sin_proxies_validos(usuarios, pais="mexico"):
        return (
            {},
            3,
            "No hay proxies válidos: x.com no respondio por ningun candidato",
        )

    clase_sin_proxy = _clase_bot()
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _parches(
        (local_mod, "_asignar_proxies_cuentas", _sin_proxies_validos)
    ), _campana(
        [_cuenta("sin_proxy_a")], bot_cls=clase_sin_proxy
    ) as entorno_sin_proxy:
        res = local_mod.ejecutar_campana_local(
            usuarios=["sin_proxy_a"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
            usar_proxy=True,
        )
        check(
            "proxy: sin proxies validos aborta SIN abrir Chrome",
            res["ok"] is False
            and res["motivo"] == "sin proxies"
            and "No hay proxies válidos" in res["error"]
            and clase_sin_proxy.creados == [],
            f"({res['motivo']}, bots={len(clase_sin_proxy.creados)})",
        )
        check(
            "proxy: sin proxies no registra acciones y reporta invalidos",
            res["hechas"] == 0
            and entorno_sin_proxy["registros"] == []
            and res["proxies_asignados"] == 0
            and res["proxies_invalidos"] == 3,
        )

    # `_asignar_proxies_cuentas`: stickiness + reemplazo de proxies vencidos.
    class _CuentaFalsaProxy:
        def __init__(self, usuario, proxy=""):
            self.usuario = usuario
            self.plataforma = "twitter"
            self.proxy = proxy

    class _QueryFalsa:
        def __init__(self, cuentas):
            self.cuentas = cuentas
            self.usuario = None

        def filter(self, *expresiones):
            for expresion in expresiones:
                try:
                    valor = expresion.right.value
                except Exception:
                    continue
                if isinstance(valor, str) and valor in self.cuentas:
                    self.usuario = valor
            return self

        def first(self):
            return self.cuentas.get(self.usuario)

    class _SesionFalsa:
        def __init__(self, cuentas):
            self.cuentas = cuentas
            self.commits = 0

        def query(self, modelo):
            return _QueryFalsa(self.cuentas)

        def commit(self):
            self.commits += 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _ProxyManagerFalso:
        def __init__(self):
            self.validos = {
                "http://bueno:8080",
                "http://nuevo-1:8080",
                "http://nuevo-2:8080",
            }
            self.llamadas = []

        def cargar_por_pais(self, pais):
            return [
                "http://viejo:8080",
                "http://nuevo-1:8080",
                "http://nuevo-2:8080",
            ]

        def cargar_proxies(self):
            return ["http://fallback:8080"]

        def x_accesible(self, proxy, timeout=12):
            self.llamadas.append(proxy)
            return proxy in self.validos

    cuentas_proxy = {
        "u_valida": _CuentaFalsaProxy("u_valida", "http://bueno:8080"),
        "u_vencida": _CuentaFalsaProxy("u_vencida", "http://viejo:8080"),
        "u_nueva": _CuentaFalsaProxy("u_nueva", ""),
    }
    pm_falso = _ProxyManagerFalso()
    sesion_falsa = _SesionFalsa(cuentas_proxy)
    with _parches(
        (database_mod, "get_db_session", lambda: sesion_falsa),
        (proxies_mod, "ProxyManager", lambda: pm_falso),
    ):
        asignados, invalidos, error = local_mod._asignar_proxies_cuentas(
            ["u_valida", "u_vencida", "u_nueva"], "mexico"
        )
    check(
        "proxies: reutiliza el proxy vigente de la BD (stickiness)",
        asignados.get("u_valida") == "http://bueno:8080",
        str(asignados),
    )
    check(
        "proxies: reemplaza el proxy vencido por el siguiente valido",
        asignados.get("u_vencida") == "http://nuevo-1:8080",
        str(asignados),
    )
    check(
        "proxies: la siguiente cuenta usa un candidato DISTINTO",
        asignados.get("u_nueva") == "http://nuevo-2:8080",
        str(asignados),
    )
    check(
        "proxies: persiste los asignados (commit por cuenta), cuenta invalidos y no da error",
        cuentas_proxy["u_vencida"].proxy == "http://nuevo-1:8080"
        and cuentas_proxy["u_nueva"].proxy == "http://nuevo-2:8080"
        and sesion_falsa.commits == 2
        and invalidos == 1
        and error == "",
        f"(commits={sesion_falsa.commits}, invalidos={invalidos})",
    )

    class _ProxyManagerVacio(_ProxyManagerFalso):
        def cargar_por_pais(self, pais):
            return []

        def cargar_proxies(self):
            return []

    with _parches(
        (database_mod, "get_db_session", lambda: _SesionFalsa({})),
        (proxies_mod, "ProxyManager", lambda: _ProxyManagerVacio()),
    ):
        asignados_vacio, invalidos_vacio, error_vacio = (
            local_mod._asignar_proxies_cuentas(["solo"], "mexico")
        )
    check(
        "proxies: lista vacia devuelve error claro (sin lanzar)",
        asignados_vacio == {}
        and invalidos_vacio == 0
        and "No hay proxies válidos" in error_vacio,
        error_vacio,
    )

    # Modo asistido: el humano resuelve el reto y se reintenta UNA vez.
    clase_captcha = _clase_bot(
        _BotAntiBot, fallos_antibot=1, anti_bot_restante=1
    )
    eventos_captcha = []
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _parches(
        (local_mod, "_asignar_proxies_cuentas", _asignar_dos_proxies)
    ), _campana(
        [_cuenta("captcha_a")], bot_cls=clase_captcha
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["captcha_a"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
            usar_proxy=True,
            esperar_captcha_seg=30,
            callback=eventos_captcha.append,
        )
        bot_captcha = clase_captcha.creados[0]
        check(
            "captcha: emite callback espera_captcha con segundos",
            any(
                evento.get("tipo") == "espera_captcha"
                and evento.get("usuario") == "captcha_a"
                and evento.get("segundos") == 30
                for evento in eventos_captcha
            ),
            str(eventos_captcha[:2]),
        )
        check(
            "captcha: resuelto => reintenta UNA vez y cuenta como exito",
            res["exitosas"] == 1
            and res["fallidas"] == 0
            and res["captchas_resueltos"] == 1
            and res["captchas_agotados"] == 0,
            f"(exitosas={res['exitosas']}, resueltos={res['captchas_resueltos']})",
        )
        check(
            "captcha: guarda la sesion al resolver el reto",
            bot_captcha.guardados_bot >= 1,
            f"({bot_captcha.guardados_bot})",
        )

    # Modo asistido: si el humano NO resuelve, falla y omite la cuenta.
    clase_captcha_agotado = _clase_bot(
        _BotAntiBot, fallos_antibot=99, anti_bot_restante=99
    )
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _parches(
        (local_mod, "_asignar_proxies_cuentas", _asignar_dos_proxies)
    ), _campana(
        [_cuenta("captcha_b")], bot_cls=clase_captcha_agotado
    ):
        res = local_mod.ejecutar_campana_local(
            usuarios=["captcha_b"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
            usar_proxy=True,
            esperar_captcha_seg=4,
        )
        check(
            "captcha: sin resolver se agota, falla y marca sin_sesion",
            res["fallidas"] == 1
            and res["captchas_agotados"] == 1
            and res["omitidas_sin_sesion"] == 1
            and res["captchas_resueltos"] == 0,
            f"(fallidas={res['fallidas']}, agotados={res['captchas_agotados']})",
        )

    # Sin proxy/captcha el comportamiento es el historico (0 captchas).
    with _settings(
        local_max_cuentas=5, limite_local_dia=49, headless=False
    ), _campana([_cuenta("sin_proxy_hist")]):
        res = local_mod.ejecutar_campana_local(
            usuarios=["sin_proxy_hist"],
            hashtags="#mexico",
            acciones_por_cuenta=1,
            duracion_min=1,
        )
        bot_hist = local_mod.TwitterBot.creados[0]
        check(
            "historico: sin usar_proxy no hay proxy dinamico ni captchas",
            res["usar_proxy"] is False
            and res["captchas_resueltos"] == 0
            and res["captchas_agotados"] == 0
            and bot_hist.driver_args["proxy_dinamico"] is False
            and bot_hist.env_en_driver.get("TWITTER_SIN_PROXY") == "1",
            str(bot_hist.driver_args),
        )
