"""Tests del panel persistente de proceso y del PARO TOTAL de Activacion Masiva.

Cubren, sin Chrome y sin campanas reales (fakes + AppTest), lo agregado en
`web/operaciones/activacion_masiva.py`:

  - REGISTRO de campanas a nivel modulo (`_CAMPANAS`): `_registrar_campana`,
    `_campana_actual`, `_campana_en_curso`, `_finalizar_campana` (estados
    en_curso/terminada/cancelada/error), `_limpiar_registro` y el TOKEN del
    guard: el hilo viejo NO libera el guard de una campana nueva.
  - PARO: `_solicitar_paro()` setea el `threading.Event`, marca "deteniendo" y
    (con un lanzar falso que respeta `cancelar`) el hilo termina y el guard
    queda libre para relanzar.
  - Lanzamiento: `_lanzar_motor`/`_invocar_lanzar` pasan el Event SOLO si el
    motor soporta el kwarg `cancelar`; un motor viejo (sin kwarg) no falla y
    usa `motor.solicitar_paro()` en el paro.
  - PERSISTENCIA: escritura atomica y lectura del JSON en una ruta temporal
    (monkeypatch de `RUTA_ULTIMA_CAMPANA`), resumen saneado (sin tokens en las
    URLs) y sin credenciales/parametros no whitelisted.
  - PANEL: `_estado_para_panel` puro (avance, tiempo, velocidad, fase y
    `puede_forzar` a los 90s) y AppTest del render persistente en UNA pasada
    (`max_pasos=1`): proceso en vivo con boton "⛔ Paro total" sin formulario,
    ultimo proceso guardado desde disco y render normal sin registro.

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows y los acentos/emojis lo
rompen en silencio), misma regla que `test_ui_tiers_curva.py`.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES (fuera de los scripts) =====================

class _MotorProgresoFake:
    """Motor falso con `snapshot_progreso` (datos + eventos + cancelada)."""

    def __init__(self, cancelada: bool = False):
        self.cancelada = bool(cancelada)
        self.paro_pedido = 0

    def snapshot_progreso(self):
        return {
            "hechas": 7,
            "exitosas": 5,
            "fallidas": 2,
            "omitidas": 1,
            "ronda_actual": 3,
            "fase_actual": 2,
            "cancelada": self.cancelada,
            "eventos": [
                {
                    "usuario": "u1",
                    "ok": True,
                    "detalle": "rt ok",
                    "ronda": 3,
                    "rol": "rt",
                    "url": "https://x.com/u1/status/1?token=SECRETO",
                },
                {
                    "usuario": "u2",
                    "ok": False,
                    "detalle": "falló",
                    "ronda": 3,
                    "rol": "cita",
                    "url": "",
                },
            ],
        }

    def solicitar_paro(self):
        self.paro_pedido += 1


class _HiloVivo:
    """Sustituto de `threading.Thread` que siempre se reporta vivo."""

    def is_alive(self):
        return True


class _MotorViejo:
    """Motor sin el kwarg `cancelar` (firma explicita, sin **kwargs)."""

    def __init__(self):
        self.paro_pedido = 0
        self.kwargs = None

    def ejecutar(self, urls=None, callback=None, duracion_min=1):
        self.kwargs = {
            "urls": urls,
            "duracion_min": duracion_min,
            "callback_presente": callback is not None,
        }
        return {"exitosas": 0, "fallidas": 0}

    def solicitar_paro(self):
        self.paro_pedido += 1

    def snapshot_progreso(self):
        return {}


class _MotorNuevo:
    """Motor con el kwarg `cancelar` (firma explicita)."""

    def __init__(self):
        self.cancelar = "sin-llamar"

    def ejecutar(self, urls=None, callback=None, cancelar=None):
        self.cancelar = cancelar
        return {"exitosas": 1, "fallidas": 0, "cancelada": False}


class _RutaTemporal:
    """Monkeypatch de `am.RUTA_ULTIMA_CAMPANA` a una ruta temporal."""

    def __init__(self, am, ruta: str):
        self.am = am
        self.ruta = ruta
        self.original = None

    def __enter__(self):
        self.original = self.am.RUTA_ULTIMA_CAMPANA
        self.am.RUTA_ULTIMA_CAMPANA = self.ruta
        return self

    def __exit__(self, *exc):
        self.am.RUTA_ULTIMA_CAMPANA = self.original
        return False


# ===================== HELPERS =====================

def _limpiar_estado(am) -> None:
    """Limpia registro, guard y marcador de archivo (para tests)."""
    try:
        am._limpiar_registro()
    except Exception:
        pass
    try:
        am._liberar_campana()
    except Exception:
        pass
    try:
        (RAIZ / "data" / ".campana_activa").unlink()
    except Exception:
        pass


def _textos(at) -> str:
    """Junta el texto de todos los elementos pintados por un AppTest."""
    partes = []
    for atributo in ("markdown", "success", "warning", "error", "caption", "info"):
        for elemento in getattr(at, atributo, []) or []:
            try:
                partes.append(str(elemento.value))
            except Exception:
                pass
    return "\n".join(partes)


def _boton(at, key):
    """Elemento de boton por key (None si no existe)."""
    for boton in getattr(at, "button", []) or []:
        if getattr(boton, "key", None) == key:
            return boton
    return None


# ===================== SCRIPTS DE APPTEST (SOLO ASCII) =====================

def _app_panel_una_pasada():
    from web.operaciones import activacion_masiva as am

    am._render_proceso_activo(prefix="act", max_pasos=1)


def _app_roles_panel_una_pasada():
    from web.operaciones import activacion_masiva as am

    original = am._render_proceso_activo

    def _una_pasada(prefix="act", max_pasos=None):
        return original(prefix=prefix, max_pasos=1)

    am._render_proceso_activo = _una_pasada
    try:
        am._por_roles()
    finally:
        am._render_proceso_activo = original


def _app_roles_sin_campana():
    from web.operaciones import activacion_masiva as am

    class _CuentasFake:
        def __call__(self, *args, **kwargs):
            return [
                {
                    "usuario": f"cuenta_{i:03d}",
                    "status": "active",
                    "seccion": "LIB",
                    "tipo_cuenta": "ciudadana",
                    "handle_actual": "",
                    "grupo": "A",
                    "rol_activacion": ("cita", "hashtags", "comentario", "rt")[i % 4],
                }
                for i in range(8)
            ]

    original_cuentas = am._cargar_cuentas_con_roles
    original_panel = am._render_proceso_activo

    def _una_pasada(prefix="act", max_pasos=None):
        return original_panel(prefix=prefix, max_pasos=1)

    am._cargar_cuentas_con_roles = _CuentasFake()
    am._render_proceso_activo = _una_pasada
    try:
        am._por_roles()
    finally:
        am._cargar_cuentas_con_roles = original_cuentas
        am._render_proceso_activo = original_panel


# ===================== PRUEBAS =====================

def _probar_registro(check, am) -> None:
    _limpiar_estado(am)
    check(
        "registro: el guard arranca libre",
        am._CAMPANA_ACTIVA.is_set() is False and am._campana_en_curso() is None,
    )
    check("registro: _adquirir_campana toma el guard", am._adquirir_campana() is True)

    motor = _MotorProgresoFake()
    evento = threading.Event()
    id_campana = am._registrar_campana(
        motor=motor,
        hilo=None,
        evento=evento,
        tipo="roles",
        parametros={"duracion_min": 5, "repetir": True},
        usa_cancelar=True,
    )
    entrada = am._campana_en_curso()
    check(
        "registro: la entrada queda en_curso con id/tipo y es la actual",
        entrada is not None
        and entrada["id"] == id_campana
        and entrada["estado"] == "en_curso"
        and entrada["tipo"] == "roles"
        and am._campana_actual()["id"] == id_campana,
        ascii(str(entrada and entrada.get("estado"))),
    )
    check(
        "registro: _campana_en_curso incluye el estado deteniendo",
        am._CAMPANAS[id_campana].__setitem__("estado", "deteniendo") is None
        and am._campana_en_curso() is not None,
    )
    am._CAMPANAS[id_campana]["estado"] = "en_curso"

    am._finalizar_campana(
        id_campana, resumen={"exitosas": 3, "fallidas": 1, "cancelada": False}
    )
    entrada = am._CAMPANAS[id_campana]
    check(
        "registro: _finalizar_campana marca terminada/fin/resumen y libera el guard",
        entrada["estado"] == "terminada"
        and entrada["fin"] is not None
        and entrada["resumen"]["exitosas"] == 3
        and am._CAMPANA_ACTIVA.is_set() is False
        and am._campana_en_curso() is None,
        ascii(str(entrada.get("estado"))),
    )
    check(
        "registro: _limpiar_registro borra la entrada",
        (am._limpiar_registro(id_campana), id_campana not in am._CAMPANAS)[1],
    )

    # ---- estado de error ----
    am._adquirir_campana()
    id_error = am._registrar_campana(motor=_MotorProgresoFake(), evento=threading.Event())
    am._finalizar_campana(id_error, error=RuntimeError("boom de prueba"))
    check(
        "registro: error -> estado 'error' con detalle",
        am._CAMPANAS[id_error]["estado"] == "error"
        and "boom de prueba" in am._CAMPANAS[id_error]["error"],
    )
    am._limpiar_registro(id_error)

    # ---- cancelada por resumen ----
    am._adquirir_campana()
    id_canc = am._registrar_campana(
        motor=_MotorProgresoFake(cancelada=True), evento=threading.Event()
    )
    am._finalizar_campana(
        id_canc, resumen={"exitosas": 1, "fallidas": 0, "cancelada": True}
    )
    check(
        "registro: resumen con cancelada=True -> estado 'cancelada'",
        am._CAMPANAS[id_canc]["estado"] == "cancelada",
    )

    # ---- TOKEN del guard: el hilo viejo no libera la campana nueva ----
    am._liberar_campana(id_canc)  # simula "forzar liberacion"
    check(
        "token: la liberacion forzada deja el guard libre",
        am._CAMPANA_ACTIVA.is_set() is False,
    )
    check("token: se puede relanzar (adquirir otra vez)", am._adquirir_campana() is True)
    id_nueva = am._registrar_campana(
        motor=_MotorProgresoFake(), evento=threading.Event()
    )
    am._finalizar_campana(id_canc, resumen={"cancelada": True})  # hilo viejo tarde
    check(
        "token: el hilo viejo NO libera el guard de la campana nueva",
        am._CAMPANA_ACTIVA.is_set() is True
        and (am._campana_en_curso() or {}).get("id") == id_nueva,
    )
    check(
        "token: la entrada vieja si quedo cancelada",
        am._CAMPANAS[id_canc]["estado"] == "cancelada",
    )
    _limpiar_estado(am)


def _probar_paro(check, am) -> None:
    """Paro con un `lanzar` falso que respeta el Event (sin Chrome)."""
    _limpiar_estado(am)
    visto: dict = {"cancelar": None, "arrancado": False}
    resultado: dict = {}
    liberar = threading.Event()

    def _lanzar(cb, cancelar=None):
        visto["cancelar"] = cancelar
        visto["arrancado"] = True
        for _ in range(1000):
            if callable(getattr(cancelar, "is_set", None)) and cancelar.is_set():
                break
            time.sleep(0.01)
        # Se queda "muriendo" hasta que el test comprueba el estado deteniendo:
        # asi el check no depende de la carrera con `_finalizar_campana`.
        liberar.wait(timeout=5.0)
        return {"cancelada": True, "exitosas": 0, "fallidas": 0}

    def _runner():
        resultado["resumen"] = am._lanzar_con_progreso(
            _lanzar, _MotorProgresoFake(), 1, False, tipo="citas",
            parametros={"duracion_min": 1, "repetir": False},
        )

    hilo = threading.Thread(target=_runner, daemon=True)
    hilo.start()
    limite = time.monotonic() + 5.0
    while time.monotonic() < limite:
        if visto["arrancado"] and am._campana_en_curso() is not None:
            break
        time.sleep(0.01)

    entrada = am._campana_en_curso()
    check(
        "paro: la campana queda en curso y el lanzar recibio el Event",
        entrada is not None and isinstance(visto["cancelar"], threading.Event),
        f"(cancelar={type(visto['cancelar']).__name__})",
    )
    pedido = am._solicitar_paro()
    evento = entrada["evento"] if entrada is not None else None
    check(
        "paro: _solicitar_paro setea el Event y marca deteniendo",
        pedido is True
        and evento is not None
        and evento.is_set()
        and am._CAMPANAS[entrada["id"]]["estado"] == "deteniendo",
        ascii(str(entrada and entrada.get("estado"))),
    )

    liberar.set()
    hilo.join(timeout=5.0)
    check(
        "paro: el hilo termina con resumen cancelado",
        not hilo.is_alive() and resultado.get("resumen", {}).get("cancelada") is True,
        ascii(str(resultado.get("resumen"))),
    )
    check(
        "paro: el guard queda libre y se puede relanzar",
        am._CAMPANA_ACTIVA.is_set() is False and am._adquirir_campana() is True,
    )
    _limpiar_estado(am)


def _probar_lanzamiento(check, am) -> None:
    """El Event viaja al motor nuevo; el viejo no falla y usa solicitar_paro."""
    check(
        "lanzamiento: _motor_usa_cancelar detecta la firma nueva y la vieja",
        am._motor_usa_cancelar(_MotorNuevo(), "citas") is True
        and am._motor_usa_cancelar(_MotorViejo(), "citas") is False,
    )
    check(
        "lanzamiento: _invocar_lanzar elige 2/1/0 argumentos por firma",
        am._invocar_lanzar(lambda cb, cancelar=None: (cb, cancelar), "CB", "EV")
        == ("CB", "EV")
        and am._invocar_lanzar(lambda cb: (cb, None), "CB", "EV") == ("CB", None)
        and am._invocar_lanzar(lambda: "sin-args", "CB", "EV") == "sin-args",
    )

    # ---- motor VIEJO (sin kwarg): no se le pasa cancelar ----
    motor_viejo = _MotorViejo()
    lanzar_viejo = am._lanzar_motor(motor_viejo.ejecutar, {"urls": ["u"]})
    resumen_viejo = lanzar_viejo("cb", threading.Event())
    check(
        "lanzamiento: motor viejo no recibe 'cancelar' y no falla",
        motor_viejo.kwargs is not None
        and "cancelar" not in motor_viejo.kwargs
        and resumen_viejo.get("fallidas") == 0,
        ascii(str(motor_viejo.kwargs)),
    )

    _limpiar_estado(am)
    am._adquirir_campana()
    am._registrar_campana(
        motor=motor_viejo, evento=threading.Event(), usa_cancelar=False
    )
    check(
        "lanzamiento: el paro usa motor.solicitar_paro() con un motor viejo",
        am._solicitar_paro() is True and motor_viejo.paro_pedido == 1,
    )
    _limpiar_estado(am)

    # ---- motor NUEVO: el Event llega al kwarg ----
    motor_nuevo = _MotorNuevo()
    lanzar_nuevo = am._lanzar_motor(motor_nuevo.ejecutar, {"urls": ["u"]})
    resumen_nuevo = am._lanzar_con_progreso(
        lanzar_nuevo, motor_nuevo, 1, False, tipo="citas",
        parametros={"duracion_min": 1, "repetir": False},
    )
    check(
        "lanzamiento: el motor nuevo recibe el Event por el kwarg cancelar",
        isinstance(motor_nuevo.cancelar, threading.Event)
        and motor_nuevo.cancelar.is_set() is False,
        f"(cancelar={type(motor_nuevo.cancelar).__name__})",
    )
    check(
        "lanzamiento: en modo bare devuelve el resumen y libera el guard",
        resumen_nuevo.get("exitosas") == 1
        and am._CAMPANA_ACTIVA.is_set() is False,
        ascii(str(resumen_nuevo)),
    )
    _limpiar_estado(am)


def _probar_persistencia(check, am, carpeta_temporal: str) -> None:
    ruta = os.path.join(carpeta_temporal, "campanas", "ultima_campana.json")
    with _RutaTemporal(am, ruta):
        _limpiar_estado(am)
        am._adquirir_campana()
        motor = _MotorProgresoFake()
        id_campana = am._registrar_campana(
            motor=motor,
            hilo=None,
            evento=threading.Event(),
            tipo="roles",
            parametros={
                "duracion_min": 7,
                "repetir": True,
                "curva_aceleracion": True,
                "curva_fase1_min": 15,
                "password": "SECRETO123",
            },
            usa_cancelar=True,
        )
        am._finalizar_campana(
            id_campana,
            resumen={
                "exitosas": 2,
                "fallidas": 1,
                "ronda": 4,
                "url": "https://x.com/a/status/1?token=ABC",
                "detalles": [
                    {
                        "usuario": "u1",
                        "ok": True,
                        "url": "https://x.com/u1/status/2?t=XYZ",
                    }
                ],
            },
        )
        datos = am._leer_ultima_campana()
        texto = Path(ruta).read_text(encoding="utf-8")
        entrada = am._CAMPANAS.get(id_campana) or {}

        check(
            "persistencia: el JSON existe y la escritura es atomica (sin .tmp)",
            Path(ruta).exists() and not Path(f"{ruta}.tmp").exists(),
        )
        check(
            "persistencia: guarda id/estado/fin/tipo/duracion/repetir/curva",
            datos.get("id") == id_campana
            and datos.get("estado") == "terminada"
            and datos.get("fin") is not None
            and datos.get("tipo") == "roles"
            and datos.get("duracion_min") == 7
            and datos.get("repetir") is True
            and datos.get("curva_aceleracion") is True
            and datos.get("curva_fase1_min") == 15,
            ascii({k: datos.get(k) for k in ("id", "estado", "tipo", "duracion_min")}),
        )
        check(
            "persistencia: el resumen sanea los tokens de las URLs",
            "token=ABC" not in texto
            and "?t=XYZ" not in texto
            and "https://x.com/a/status/1" in texto
            and datos.get("resumen", {}).get("exitosas") == 2,
        )
        check(
            "persistencia: parametros no whitelisted (credenciales) no se escriben",
            "SECRETO123" not in texto and "password" not in texto,
        )
        check(
            "persistencia: el JSON no incluye objetos internos",
            all(clave not in datos for clave in ("motor", "hilo", "evento", "previos_env")),
        )
        progreso = datos.get("progreso") or {}
        eventos = progreso.get("eventos") or []
        check(
            "persistencia: progreso con hechas/ronda/fase y eventos saneados",
            progreso.get("hechas") == 7
            and progreso.get("ronda_actual") == 3
            and progreso.get("fase_actual") == 2
            and len(eventos) == 2
            and eventos[0].get("url") == "https://x.com/u1/status/1",
            ascii(str(progreso)),
        )
        check(
            "persistencia: _entrada_para_json es tolerante sin snapshot",
            isinstance(am._entrada_para_json(entrada), dict),
        )
        _limpiar_estado(am)


def _probar_estado_panel_puro(check, am) -> None:
    motor = _MotorProgresoFake()
    entrada = {
        "id": "abc",
        "estado": "en_curso",
        "tipo": "citas",
        "evento": threading.Event(),
        "hilo": _HiloVivo(),
        "inicio_mono": time.monotonic() - 30.0,
        "parametros": {"duracion_min": 1, "repetir": True},
        "paro_solicitado": None,
        "total": 0,
        "motor": motor,
    }
    estado = am._estado_para_panel(entrada, am._snapshot_progreso(motor))
    check(
        "panel puro: avance por tiempo, tiempo restante y metricas",
        abs(estado.get("avance", 0) - 0.5) < 0.05
        and estado.get("tiempo_txt") == "⏱️ Faltan 00:30"
        and estado.get("ronda_actual") == 3
        and estado.get("hechas") == 7
        and "7/1.0/min" not in estado.get("linea", "")
        and "🚀 Fase 2/2 de la curva" in estado.get("linea", ""),
        ascii(str({k: estado.get(k) for k in ("avance", "tiempo_txt", "linea")})),
    )
    check(
        "panel puro: sin cancelacion no ofrece forzar",
        estado.get("puede_forzar") is False and estado.get("cancelada") is False,
    )

    entrada_parada = dict(entrada)
    entrada_parada["estado"] = "deteniendo"
    entrada_parada["paro_solicitado"] = time.time() - am.PARO_FORZAR_SEG - 1.0
    entrada_parada["evento"] = threading.Event()
    entrada_parada["evento"].set()
    estado_parada = am._estado_para_panel(
        entrada_parada, am._snapshot_progreso(motor)
    )
    check(
        "panel puro: paro con hilo vivo y 90s -> puede_forzar y cancelada",
        estado_parada.get("puede_forzar") is True
        and estado_parada.get("deteniendo") is True
        and estado_parada.get("cancelada") is True,
    )

    entrada_fresca = dict(entrada_parada)
    entrada_fresca["paro_solicitado"] = time.time()
    check(
        "panel puro: paro reciente NO ofrece forzar",
        am._estado_para_panel(entrada_fresca, {}).get("puede_forzar") is False,
    )
    check(
        "panel puro: sin entrada devuelve {}",
        am._estado_para_panel(None) == {},
    )


def _app_test_panel_vivo(am) -> dict:
    """AppTest: proceso en vivo en una pasada + boton de paro."""
    from streamlit.testing.v1 import AppTest

    datos = {"ok": False, "excepcion": "", "paro": False, "textos": "", "estado": ""}
    motor = _MotorProgresoFake()
    evento = threading.Event()
    am._adquirir_campana()
    id_campana = am._registrar_campana(
        motor=motor,
        hilo=_HiloVivo(),
        evento=evento,
        tipo="citas",
        parametros={"duracion_min": 10, "repetir": True, "limpiar_contexto": False},
        usa_cancelar=True,
    )
    try:
        at = AppTest.from_function(_app_panel_una_pasada, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return datos
        datos["textos"] = _textos(at)
        datos["paro"] = _boton(at, "act_btn_paro") is not None

        if datos["paro"]:
            at = at.button(key="act_btn_paro").click().run()
            if at.exception:
                datos["excepcion"] = str(at.exception[0].value)[:200]
                return datos
            datos["textos"] += "\n" + _textos(at)
        datos["estado"] = str(am._CAMPANAS.get(id_campana, {}).get("estado") or "")
        datos["ok"] = True
        return datos
    finally:
        _limpiar_estado(am)


def _app_test_roles_con_panel(am) -> dict:
    """AppTest: la pestana Por roles delega en el panel y oculta el formulario."""
    from streamlit.testing.v1 import AppTest

    datos = {
        "ok": False,
        "excepcion": "",
        "paro": False,
        "formulario": False,
        "textos": "",
    }
    am._adquirir_campana()
    id_campana = am._registrar_campana(
        motor=_MotorProgresoFake(),
        hilo=_HiloVivo(),
        evento=threading.Event(),
        tipo="roles",
        parametros={"duracion_min": 10, "repetir": True, "limpiar_contexto": False},
        usa_cancelar=True,
    )
    try:
        at = AppTest.from_function(_app_roles_panel_una_pasada, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return datos
        datos["textos"] = _textos(at)
        datos["paro"] = _boton(at, "act_roles_btn_paro") is not None
        datos["formulario"] = any(
            getattr(t, "key", None) == "act_roles_urls" for t in at.text_area
        ) or _boton(at, "btn_act_roles_launch") is not None
        datos["ok"] = True
        return datos
    finally:
        am._limpiar_registro(id_campana)
        _limpiar_estado(am)


def _app_test_roles_sin_campana(am) -> dict:
    """AppTest: sin campana se renderiza el formulario normal (sin panel vivo)."""
    from streamlit.testing.v1 import AppTest

    datos = {"ok": False, "excepcion": "", "paro": False, "formulario": False}
    try:
        at = AppTest.from_function(_app_roles_sin_campana, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return datos
        datos["paro"] = _boton(at, "act_roles_btn_paro") is not None
        datos["formulario"] = any(
            getattr(t, "key", None) == "act_roles_urls" for t in at.text_area
        )
        datos["ok"] = True
        return datos
    finally:
        _limpiar_estado(am)


def _app_test_panel_desde_disco(am, carpeta_temporal: str) -> dict:
    """AppTest: sin registro en memoria, expander con el JSON de disco."""
    from streamlit.testing.v1 import AppTest

    datos = {"ok": False, "excepcion": "", "texto_disco": "", "sin_disco": ""}
    ruta = os.path.join(carpeta_temporal, "ultima_campana.json")
    with _RutaTemporal(am, ruta):
        _limpiar_estado(am)
        am._escribir_ultima_campana(
            {
                "id": "disco-1",
                "estado": "terminada",
                "tipo": "citas",
                "actualizado": time.time(),
                "progreso": {
                    "hechas": 5,
                    "exitosas": 4,
                    "fallidas": 1,
                    "ronda_actual": 2,
                    "eventos": [
                        {"usuario": "u9", "ok": True, "detalle": "ok", "ronda": 2}
                    ],
                },
                "resumen": {"exitosas": 4, "fallidas": 1},
            }
        )
        at = AppTest.from_function(_app_panel_una_pasada, default_timeout=60)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return datos
        datos["texto_disco"] = _textos(at)

        try:
            Path(ruta).unlink()
        except Exception:
            pass
        at_sin = AppTest.from_function(_app_panel_una_pasada, default_timeout=60)
        at_sin.run()
        if at_sin.exception:
            datos["excepcion"] = str(at_sin.exception[0].value)[:200]
            return datos
        datos["sin_disco"] = _textos(at_sin)
        datos["ok"] = True
        return datos


def run(check):
    from web.operaciones import activacion_masiva as am

    with tempfile.TemporaryDirectory() as carpeta:
        ruta_temporal = os.path.join(carpeta, "ultima_campana.json")
        with _RutaTemporal(am, ruta_temporal):
            _probar_registro(check, am)
            _probar_paro(check, am)
            _probar_lanzamiento(check, am)
            _probar_estado_panel_puro(check, am)
            _probar_persistencia(check, am, carpeta)

            # ------------- Panel persistente (AppTest) -------------
            datos_vivo = _app_test_panel_vivo(am)
            check(
                "panel vivo: renderiza sin excepciones en una pasada",
                datos_vivo.get("ok"),
                datos_vivo.get("excepcion", ""),
            )
            check(
                "panel vivo: barra/metricas/feed y boton 'Paro total' presentes",
                datos_vivo.get("ok")
                and datos_vivo.get("paro")
                and "Ronda 3" in datos_vivo.get("textos", "")
                and "Fase 2/2" in datos_vivo.get("textos", "")
                and "@u1" in datos_vivo.get("textos", ""),
                ascii(datos_vivo.get("textos", ""))[:200],
            )
            check(
                "panel vivo: el clic en 'Paro total' deja la campana deteniendo",
                datos_vivo.get("ok")
                and datos_vivo.get("estado") == "deteniendo"
                and "Paro solicitado" in datos_vivo.get("textos", ""),
                ascii(datos_vivo.get("estado", "")),
            )

            datos_roles = _app_test_roles_con_panel(am)
            check(
                "panel vivo: la pestana Por roles delega en el panel sin excepciones",
                datos_roles.get("ok"),
                datos_roles.get("excepcion", ""),
            )
            check(
                "panel vivo: con campana en curso NO pinta el formulario de lanzar",
                datos_roles.get("ok")
                and datos_roles.get("paro") is True
                and datos_roles.get("formulario") is False,
                ascii(str({k: datos_roles.get(k) for k in ("paro", "formulario")})),
            )

            datos_sin = _app_test_roles_sin_campana(am)
            check(
                "panel vivo: sin campana la pestana Por roles renderiza normal",
                datos_sin.get("ok")
                and datos_sin.get("formulario") is True
                and datos_sin.get("paro") is False,
                datos_sin.get("excepcion", "")
                or ascii(str({k: datos_sin.get(k) for k in ("paro", "formulario")})),
            )

            datos_disco = _app_test_panel_desde_disco(am, carpeta)
            check(
                "panel desde disco: sin registro muestra el ultimo estado guardado",
                datos_disco.get("ok")
                and "último estado guardado en disco" in datos_disco.get("texto_disco", "")
                and "Última actualización" in datos_disco.get("texto_disco", ""),
                ascii(datos_disco.get("texto_disco", ""))[:200],
            )
            check(
                "panel desde disco: sin registro ni JSON no pinta nada",
                datos_disco.get("ok")
                and "último estado guardado en disco" not in datos_disco.get("sin_disco", ""),
                ascii(datos_disco.get("sin_disco", ""))[:200],
            )

    check(
        "panel: el marcador data/.campana_activa quedo limpio al final",
        not (RAIZ / "data" / ".campana_activa").exists(),
    )
