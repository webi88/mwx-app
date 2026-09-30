"""Tests de UI de las IMAGENES aleatorias de los posts (activaciones).

Peticion del dueño: poder subir VARIAS imagenes en las activaciones y que, de
vez en cuando y de manera aleatoria, los posts (rol "hashtags", tipicamente
Tier 1) se publiquen con el texto + UNA imagen al azar. El motor acepta los
kwargs `imagenes=[rutas...]` y `probabilidad_imagen=N` y la UI solo los pasa si
`_soporta_kwarg(func, "imagenes")` es True (si no, avisa y sigue sin imagenes).

Cubren, sin red/Chrome/BD real (fakes + monkeypatch, mismo patron que
`tests/test_ui_tiers_curva.py` y `tests/test_actividad_web.py`):

  - Helper `_parametros_imagenes` de `web/operaciones/activacion_masiva.py`:
    sin rutas -> `{}` (comportamiento clasico exacto); con rutas y motor que
    soporta `imagenes` (firma explicita o `**kwargs`) -> `imagenes` +
    `probabilidad_imagen` (clamp 0-100; basura -> 30); motor viejo -> `{}` y
    aviso (la campaña sigue sin imagenes, nunca se rompe).
  - Fuente: pestaña "Por roles" con uploader multiple `act_roles_imagenes` +
    `act_roles_prob_imagen`; operacion Actividad con `actividad_imagenes` +
    `actividad_prob_imagen`; Cita masiva/3+3+3 SIN imagenes (una sola llamada a
    `st.file_uploader` por archivo).
  - AppTest de "Por roles" (el harness de Streamlit 1.41.1 NO expone
    `file_uploader` en el arbol, asi que el uploader se monkeypatchea para
    simular 3 archivos y `guardar_imagen_subida` para no escribir a disco):
    render sin excepciones, number input y label del bloque presentes, y al
    lanzar un motor fake que soporta `imagenes` recibe las rutas guardadas con
    prefijo `act_img_N` y la probabilidad elegida; con un motor viejo NO las
    recibe y el aviso aparece (la campaña si se lanza).
  - AppTest de "Actividad": render sin excepciones, number input presente y al
    lanzar el motor fake recibe `imagenes`/`probabilidad_imagen`.
  - Lanzamiento DIRECTO de `actividad._lanzar` con
    `_lanzar_con_opciones_velocidad` monkeypatcheado (sin hilos ni guard): los
    fakes capturan los kwargs exactos, con y sin soporte del motor.

Los scripts de `AppTest.from_function` son SOLO ASCII (Streamlit escribe el
script temporal con la codificacion local de Windows; misma regla que
`test_ui_tiers_curva.py`).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


# ===================== FAKES (definidos FUERA del script) =====================

class _ArchivoFalso:
    """UploadedFile simulado (solo lo que usa el codigo: `name`/`getbuffer`)."""

    def __init__(self, nombre: str):
        self.name = nombre

    def getbuffer(self):
        return b"fake-image"


class _GuardarImagenEspia:
    """Sustituye `_helpers.guardar_imagen_subida` sin escribir en disco."""

    def __init__(self):
        self.llamadas: list = []

    def __call__(self, archivo, prefijo="subida"):
        nombre = str(getattr(archivo, "name", "") or "")
        self.llamadas.append((nombre, str(prefijo)))
        return f"C:/tmp/imagenes/{prefijo}_{nombre}"


class _MotorRolesFake:
    """Motor con `**kwargs`: soporta `imagenes` y captura los kwargs."""

    capturados: list = []

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar_por_roles(self, **kwargs):
        type(self).capturados.append(dict(kwargs))
        return {
            "total": 0,
            "exitosas": 0,
            "fallidas": 0,
            "rondas": 1,
            "por_rol": {},
            "detalles": [],
        }

    def snapshot_progreso(self):
        return {}


class _MotorRolesViejoFake:
    """Motor viejo: firma explicita completa SIN `imagenes` (tampoco kwargs).

    Incluye todos los parametros que la pagina pasa SIEMPRE (y los opcionales
    de curva/reserva/pausa/cancelar) para que el unico kwarg ausente sea el que
    se esta probando.
    """

    capturados: list = []

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar_por_roles(
        self,
        urls=None,
        texto_base="",
        hashtags="",
        menciones="",
        dar_like=False,
        duracion_min=60,
        cohortes=4,
        usuarios=None,
        solo_roles=None,
        narrativa="",
        entrenamiento="",
        callback=None,
        contexto="",
        solo_con_registro=False,
        repetir=False,
        roles_aleatorios=False,
        cooldown_min=0,
        secciones=None,
        porcentaje_min_ronda=40,
        porcentaje_max_ronda=90,
        pausa_comentario_url_seg=15.0,
        reserva_usuarios=None,
        curva_aceleracion=False,
        curva_fase1_min=None,
        cancelar=None,
    ):
        type(self).capturados.append(
            {
                "urls": urls,
                "callback_presente": callback is not None,
                "cancelar_presente": cancelar is not None,
                "reserva_usuarios": reserva_usuarios,
            }
        )
        return {
            "total": 0,
            "exitosas": 0,
            "fallidas": 0,
            "rondas": 1,
            "por_rol": {},
            "detalles": [],
        }

    def snapshot_progreso(self):
        return {}


class _MotorActividadFake:
    """Motor con `**kwargs`: soporta `imagenes` (captura ejecutar_actividad)."""

    capturados: list = []

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar_actividad(self, **kwargs):
        type(self).capturados.append(dict(kwargs))
        return {
            "exitosas": 0,
            "fallidas": 0,
            "omitidas": 0,
            "por_cuenta": {},
        }

    def snapshot_progreso(self):
        return {}

    def solicitar_paro(self):
        pass


class _MotorActividadViejoDirecto:
    """Motor viejo explicito para el lanzamiento directo de `actividad._lanzar`.

    Sin `imagenes` y sin `**kwargs`: `_parametros_imagenes` debe avisar y no
    pasar nada.
    """

    capturados: list = []

    def __init__(self, *args, **kwargs):
        self.inicial = dict(kwargs)

    def ejecutar_actividad(
        self,
        usuarios=None,
        hashtags=None,
        posts_min=3,
        posts_max=4,
        pausa_entre_posts_seg=(60, 240),
        texto_base="",
        contexto="",
        narrativa="",
        menciones=None,
        max_browsers=None,
        permitir_pausadas=False,
        duracion_max_min=120,
        callback=None,
    ):
        type(self).capturados.append(
            {
                "usuarios": usuarios,
                "hashtags": hashtags,
                "callback_presente": callback is not None,
            }
        )
        return {}


class _CuentasRolesFake:
    """Cuentas activas simuladas para la pestaña Por roles (los 4 roles)."""

    def __call__(self, *args, **kwargs):
        roles = ("cita", "hashtags", "comentario", "rt")
        return [
            {
                "usuario": f"cuenta_{i:03d}",
                "status": "active",
                "seccion": "LIB",
                "tipo_cuenta": "ciudadana",
                "handle_actual": "",
                "grupo": "A",
                "rol_activacion": roles[i % len(roles)],
                "tier_calidad": "",
                "pausada_activacion": False,
            }
            for i in range(8)
        ]


class _CuentasActividadFake:
    """Cuentas activas simuladas para la operacion Actividad."""

    def __call__(self, *args, **kwargs):
        return [
            {
                "usuario": f"activa_{i:02d}",
                "status": "active",
                "seccion": "LIB",
                "tipo_cuenta": "ciudadana",
                "handle_actual": "",
                "grupo": "A",
                "rol_activacion": "hashtags",
                "tier_calidad": "",
                "pausada_activacion": False,
            }
            for i in range(4)
        ]


def _reserva_fake(*args, **kwargs):
    """Respaldo simulado (evita consultar la BD real en los AppTest)."""
    return ["reserva_uno", "reserva_dos"]


def _funcion_con_imagenes(imagenes=None, probabilidad_imagen=30):
    """Funcion falsa con `imagenes` explicito (como el motor nuevo)."""
    return None


def _funcion_kwargs(**kwargs):
    """Funcion falsa que acepta `**kwargs` (como el motor nuevo)."""
    return None


def _funcion_sin_imagenes(urls=None, callback=None):
    """Funcion falsa SIN `imagenes` (como un motor viejo)."""
    return None


# ===================== LIMPIEZA / ESPERA =====================

def _limpiar_campana():
    """Limpia el guard/registro de campanas y el marcador de disco."""
    from web.operaciones import activacion_masiva as am

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


def _esperar_capturas(clase, minimo: int, timeout: float = 8.0) -> None:
    """Espera a que el motor falso capture (la campaña corre en un hilo)."""
    limite = time.time() + timeout
    while len(clase.capturados) < minimo and time.time() < limite:
        time.sleep(0.05)


# ===================== SCRIPTS DE APPTEST (SOLO ASCII) =====================

def _app_roles():
    from web.operaciones import activacion_masiva as am

    am._por_roles()


def _app_actividad():
    from web.operaciones import actividad

    actividad.render({})


# ===================== DRIVERS DE APPTEST =====================

def _app_test_roles_imagenes(motor_clase) -> tuple:
    """AppTest de "Por roles" con 3 archivos simulados en el uploader.

    Parchea `streamlit.file_uploader` (el harness no expone el widget), guarda
    las llamadas de `guardar_imagen_subida` en un espia (sin tocar disco) y
    captura advertencias con un spy de `st.warning` (el `st.rerun()` del
    lanzamiento reemplaza los elementos del arbol y podria perder el aviso).
    """
    from streamlit.testing.v1 import AppTest

    import streamlit as st_mod

    from activaciones import motor as motor_mod
    from web.operaciones import _helpers
    from web.operaciones import activacion_masiva as am

    datos = {
        "number_inputs": set(),
        "markdown": [],
        "captions": [],
        "advertencias": [],
        "guardados": [],
        "captura": {},
        "excepcion": "",
    }
    espia = _GuardarImagenEspia()
    original_cuentas = am._cargar_cuentas_con_roles
    original_reserva = am._cargar_reserva_usuarios
    original_motor = motor_mod.MotorActivacion
    original_uploader = st_mod.file_uploader
    original_warning = st_mod.warning
    original_guardar = _helpers.guardar_imagen_subida

    def _warning_spy(body, *args, **kwargs):
        datos["advertencias"].append(str(body))
        return original_warning(body, *args, **kwargs)

    st_mod.file_uploader = lambda *a, **k: [
        _ArchivoFalso("uno.png"),
        _ArchivoFalso("dos.JPG"),
        _ArchivoFalso("tres.webp"),
    ]
    st_mod.warning = _warning_spy
    _helpers.guardar_imagen_subida = espia
    am._cargar_cuentas_con_roles = _CuentasRolesFake()
    am._cargar_reserva_usuarios = _reserva_fake
    motor_mod.MotorActivacion = motor_clase
    motor_clase.capturados.clear()
    _limpiar_campana()
    try:
        at = AppTest.from_function(_app_roles, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["number_inputs"] = {n.key for n in at.number_input}
        datos["markdown"] = [str(m.value) for m in at.markdown]

        at.text_area(key="act_roles_urls").input(
            "https://x.com/a/status/1"
        ).run()
        at.number_input(key="act_roles_prob_imagen").set_value(55).run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["captions"] = [str(c.value) for c in at.caption]
        datos["guardados"] = list(espia.llamadas)

        at.button(key="btn_act_roles_launch").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        _esperar_capturas(motor_clase, 1)
        if motor_clase.capturados:
            datos["captura"] = dict(motor_clase.capturados[-1])
        datos["advertencias"].extend(str(w.value) for w in at.warning)
        return True, "", datos
    finally:
        st_mod.file_uploader = original_uploader
        st_mod.warning = original_warning
        _helpers.guardar_imagen_subida = original_guardar
        am._cargar_cuentas_con_roles = original_cuentas
        am._cargar_reserva_usuarios = original_reserva
        motor_mod.MotorActivacion = original_motor
        _limpiar_campana()


def _app_test_actividad_imagenes() -> tuple:
    """AppTest de "Actividad" con 1 archivo simulado en el uploader."""
    from streamlit.testing.v1 import AppTest

    import streamlit as st_mod

    from activaciones import motor as motor_mod
    from web.operaciones import _helpers
    from web.operaciones import actividad

    datos = {
        "number_inputs": set(),
        "markdown": [],
        "captions": [],
        "guardados": [],
        "captura": {},
        "excepcion": "",
    }
    espia = _GuardarImagenEspia()
    original_cuentas = actividad._cargar_cuentas_con_roles
    original_motor = motor_mod.MotorActivacion
    original_uploader = st_mod.file_uploader
    original_guardar = _helpers.guardar_imagen_subida

    st_mod.file_uploader = lambda *a, **k: [_ArchivoFalso("foto.png")]
    _helpers.guardar_imagen_subida = espia
    actividad._cargar_cuentas_con_roles = _CuentasActividadFake()
    motor_mod.MotorActivacion = _MotorActividadFake
    _MotorActividadFake.capturados.clear()
    _limpiar_campana()
    try:
        at = AppTest.from_function(_app_actividad, default_timeout=90)
        at.run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["number_inputs"] = {n.key for n in at.number_input}
        datos["markdown"] = [str(m.value) for m in at.markdown]

        at.text_area(key="actividad_hashtags").input("#mexico").run()
        at.number_input(key="actividad_prob_imagen").set_value(100).run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        datos["captions"] = [str(c.value) for c in at.caption]
        datos["guardados"] = list(espia.llamadas)

        at.button(key="btn_actividad_lanzar").click().run()
        if at.exception:
            datos["excepcion"] = str(at.exception[0].value)[:200]
            return False, datos["excepcion"], datos
        _esperar_capturas(_MotorActividadFake, 1)
        if _MotorActividadFake.capturados:
            datos["captura"] = dict(_MotorActividadFake.capturados[-1])
        return True, "", datos
    finally:
        st_mod.file_uploader = original_uploader
        _helpers.guardar_imagen_subida = original_guardar
        actividad._cargar_cuentas_con_roles = original_cuentas
        motor_mod.MotorActivacion = original_motor
        _limpiar_campana()


def _capturar_lanzamiento_directo(motor_clase, imagenes, probabilidad) -> list:
    """Llama `actividad._lanzar` con fakes y devuelve los kwargs capturados.

    `_lanzar_con_opciones_velocidad` se monkeypatchea para invocar el `lanzar`
    envuelto SIN hilo, sin guard y sin envs: el motor falso captura el dict
    exacto que la pagina le pasaria.
    """
    from activaciones import motor as motor_mod
    from web.operaciones import actividad

    motor_clase.capturados.clear()
    original_motor = motor_mod.MotorActivacion
    original_lanzar = actividad._lanzar_con_opciones_velocidad

    def _lanzar_falso(lanzar, motor, **kwargs):
        return lanzar(None)

    motor_mod.MotorActivacion = motor_clase
    actividad._lanzar_con_opciones_velocidad = _lanzar_falso
    try:
        actividad._lanzar(
            tags=["#mexico"],
            usuarios_sel=["activa_00"],
            posts_min=3,
            posts_max=4,
            pausa_min=10,
            pausa_max=20,
            navegadores=1,
            duracion=5,
            contexto="",
            narrativa="",
            menciones="",
            incluir_pausadas=False,
            limpiar_contexto=False,
            imagenes=imagenes,
            probabilidad_imagen=probabilidad,
        )
    finally:
        motor_mod.MotorActivacion = original_motor
        actividad._lanzar_con_opciones_velocidad = original_lanzar
    return list(motor_clase.capturados)


# ===================== SUITE =====================

def run(check):
    from web.operaciones import activacion_masiva as am
    from web.operaciones import actividad

    # ---------------- 1) Helper `_parametros_imagenes` ----------------
    check(
        "imagenes helper: sin rutas -> {} (comportamiento clasico exacto)",
        am._parametros_imagenes(_funcion_con_imagenes, [], 30) == {}
        and am._parametros_imagenes(_funcion_con_imagenes, None, 30) == {}
        and am._parametros_imagenes(_funcion_con_imagenes, ["", None], 30) == {},
    )
    resultado = am._parametros_imagenes(
        _funcion_con_imagenes, ["C:/tmp/a.png", "C:/tmp/b.jpg"], 42
    )
    check(
        "imagenes helper: rutas + motor que soporta -> imagenes/probabilidad",
        resultado
        == {
            "imagenes": ["C:/tmp/a.png", "C:/tmp/b.jpg"],
            "probabilidad_imagen": 42,
        },
        str(resultado),
    )
    check(
        "imagenes helper: acepta motores con **kwargs (VAR_KEYWORD)",
        am._parametros_imagenes(_funcion_kwargs, ["x.png"], 30)
        == {"imagenes": ["x.png"], "probabilidad_imagen": 30},
    )
    check(
        "imagenes helper: limpieza de rutas vacias y no string",
        am._parametros_imagenes(
            _funcion_con_imagenes, ["a.png", "", "  ", None, "b.png"], 30
        )
        == {"imagenes": ["a.png", "b.png"], "probabilidad_imagen": 30},
    )
    check(
        "imagenes helper: probabilidad clamp 0-100 y basura -> 30",
        am._parametros_imagenes(_funcion_con_imagenes, ["a"], 150)[
            "probabilidad_imagen"
        ]
        == 100
        and am._parametros_imagenes(_funcion_con_imagenes, ["a"], -3)[
            "probabilidad_imagen"
        ]
        == 0
        and am._parametros_imagenes(_funcion_con_imagenes, ["a"], "basura")[
            "probabilidad_imagen"
        ]
        == 30
        and am._parametros_imagenes(_funcion_con_imagenes, ["a"], None)[
            "probabilidad_imagen"
        ]
        == 30
        and am._parametros_imagenes(_funcion_con_imagenes, ["a"], 55.9)[
            "probabilidad_imagen"
        ]
        == 55,
    )
    try:
        resultados = (
            am._parametros_imagenes(_funcion_sin_imagenes, ["a.png"], 30),
            am._parametros_imagenes(lambda x: x, ["a.png"], 30),
        )
        error_helper = ""
    except Exception as e:  # noqa: BLE001
        resultados, error_helper = ("EXCEPCION",), str(e)
    check(
        "imagenes helper: motor sin el kwarg -> {} y nunca lanza",
        resultados == ({}, {}) and not error_helper,
        error_helper or str(resultados),
    )

    # ---------------- 2) Fuente: controles y guard ----------------
    fuente_am = (
        RAIZ / "web" / "operaciones" / "activacion_masiva.py"
    ).read_text(encoding="utf-8")
    fuente_act = (
        RAIZ / "web" / "operaciones" / "actividad.py"
    ).read_text(encoding="utf-8")
    check(
        "activacion fuente: uploader multiple y probabilidad en Por roles",
        '"act_roles_imagenes"' in fuente_am
        and '"act_roles_prob_imagen"' in fuente_am
        and "accept_multiple_files=True" in fuente_am
        and "Imágenes para los posts" in fuente_am,
    )
    check(
        "activacion fuente: el guard solo pasa imagenes si el motor soporta",
        "def _parametros_imagenes" in fuente_am
        and "motor.ejecutar_por_roles, rutas_imagenes, prob_imagen" in fuente_am
        and "**imagen_kwargs," in fuente_am
        and '"imagenes": candidatas' in fuente_am
        and "probabilidad_imagen" in fuente_am,
    )
    check(
        "activacion fuente: aviso exacto cuando el motor no soporta imagenes",
        "Este motor todavía no soporta imágenes en los posts; la campaña "
        in fuente_am
        and "irá sin imágenes." in fuente_am,
    )
    check(
        "actividad fuente: uploader multiple y probabilidad presentes",
        '"actividad_imagenes"' in fuente_act
        and '"actividad_prob_imagen"' in fuente_act
        and "accept_multiple_files=True" in fuente_act
        and "motor.ejecutar_actividad, imagenes, probabilidad_imagen"
        in fuente_act
        and "**imagen_kwargs," in fuente_act,
    )
    check(
        "imagenes: solo hay UN file_uploader por pagina (nada en Cita/3+3+3)",
        fuente_am.count("st.file_uploader(") == 1
        and fuente_act.count("st.file_uploader(") == 1
        and '"act_imagenes"' not in fuente_am
        and '"act_prob_imagen"' not in fuente_am
        and "_parametros_imagenes" in fuente_act,
    )
    check(
        "imagenes: guardar_imagen_subida con prefijo act_img_N en ambas paginas",
        'guardar_imagen_subida(archivo, prefijo=f"act_img_{i}")' in fuente_am
        and 'guardar_imagen_subida(archivo, prefijo=f"act_img_{i}")'
        in fuente_act,
    )

    # ---------------- 3) AppTest "Por roles" con motor que si soporta ----------------
    ok, detalle, datos_roles = _app_test_roles_imagenes(_MotorRolesFake)
    check(
        "roles AppTest: render y lanzamiento sin excepciones",
        ok,
        detalle,
    )
    check(
        "roles AppTest: number input de probabilidad y label del bloque",
        ok
        and "act_roles_prob_imagen" in datos_roles["number_inputs"]
        and any(
            "Imágenes para los posts" in m for m in datos_roles["markdown"]
        ),
        f"number_inputs={sorted(datos_roles['number_inputs'])}",
    )
    check(
        "roles AppTest: guarda cada archivo con prefijo act_img_N",
        ok
        and datos_roles["guardados"][:3]
        == [("uno.png", "act_img_0"), ("dos.JPG", "act_img_1"),
            ("tres.webp", "act_img_2")]
        and set(datos_roles["guardados"])
        == {
            ("uno.png", "act_img_0"),
            ("dos.JPG", "act_img_1"),
            ("tres.webp", "act_img_2"),
        },
        str(datos_roles["guardados"]),
    )
    check(
        "roles AppTest: caption de imagenes listas con la probabilidad",
        ok
        and any(
            "3 imagen(es) lista(s)" in c and "55%" in c
            for c in datos_roles["captions"]
        ),
        ascii(datos_roles["captions"])[:220],
    )
    captura_roles = datos_roles.get("captura") or {}
    check(
        "roles AppTest: el motor recibe imagenes (rutas) y probabilidad_imagen",
        ok
        and captura_roles.get("imagenes")
        == [
            "C:/tmp/imagenes/act_img_0_uno.png",
            "C:/tmp/imagenes/act_img_1_dos.JPG",
            "C:/tmp/imagenes/act_img_2_tres.webp",
        ]
        and captura_roles.get("probabilidad_imagen") == 55,
        ascii(
            {
                "imagenes": captura_roles.get("imagenes"),
                "probabilidad_imagen": captura_roles.get("probabilidad_imagen"),
            }
        )[:240],
    )

    # ---------------- 4) AppTest "Por roles" con motor viejo ----------------
    ok, detalle, datos_viejo = _app_test_roles_imagenes(_MotorRolesViejoFake)
    check(
        "roles AppTest motor viejo: render y lanzamiento sin excepciones",
        ok,
        detalle,
    )
    check(
        "roles AppTest motor viejo: avisa y NO pasa imagenes (campana si lanza)",
        ok
        and datos_viejo.get("captura")
        and any(
            "no soporta imágenes" in w
            for w in datos_viejo.get("advertencias") or []
        )
        and "imagenes" not in datos_viejo.get("captura", {}),
        ascii(datos_viejo.get("advertencias"))[:220],
    )

    # ---------------- 5) AppTest "Actividad" con imagenes ----------------
    ok, detalle, datos_act = _app_test_actividad_imagenes()
    check(
        "actividad AppTest: render y lanzamiento sin excepciones",
        ok,
        detalle,
    )
    check(
        "actividad AppTest: number input de probabilidad y label del bloque",
        ok
        and "actividad_prob_imagen" in datos_act["number_inputs"]
        and any(
            "Imágenes para los posts" in m for m in datos_act["markdown"]
        ),
        f"number_inputs={sorted(datos_act['number_inputs'])}",
    )
    check(
        "actividad AppTest: caption de imagen lista y guardado con prefijo",
        ok
        and datos_act["guardados"][:1] == [("foto.png", "act_img_0")]
        and set(datos_act["guardados"]) == {("foto.png", "act_img_0")}
        and any(
            "1 imagen(es) lista(s)" in c and "100%" in c
            for c in datos_act["captions"]
        ),
        f"guardados={datos_act['guardados']}",
    )
    captura_act = datos_act.get("captura") or {}
    check(
        "actividad AppTest: el motor recibe imagenes y probabilidad_imagen",
        ok
        and captura_act.get("imagenes")
        == ["C:/tmp/imagenes/act_img_0_foto.png"]
        and captura_act.get("probabilidad_imagen") == 100,
        ascii(
            {
                "imagenes": captura_act.get("imagenes"),
                "probabilidad_imagen": captura_act.get("probabilidad_imagen"),
            }
        )[:240],
    )

    # ---------------- 6) Lanzamiento directo de `actividad._lanzar` ----------------
    directo = _capturar_lanzamiento_directo(
        _MotorActividadFake, ["C:/tmp/a.png", "C:/tmp/b.png"], 30
    )
    check(
        "actividad directo: con rutas y soporte -> imagenes + probabilidad",
        directo
        and directo[0].get("imagenes") == ["C:/tmp/a.png", "C:/tmp/b.png"]
        and directo[0].get("probabilidad_imagen") == 30
        and directo[0].get("hashtags") == ["#mexico"],
        ascii({k: directo[0].get(k) for k in ("imagenes", "probabilidad_imagen")})
        if directo
        else "sin capturas",
    )
    directo_vacio = _capturar_lanzamiento_directo(_MotorActividadFake, [], 30)
    check(
        "actividad directo: sin rutas NO se pasan kwargs de imagenes",
        directo_vacio
        and "imagenes" not in directo_vacio[0]
        and "probabilidad_imagen" not in directo_vacio[0],
        ascii(sorted(directo_vacio[0])) if directo_vacio else "sin capturas",
    )
    directo_viejo = _capturar_lanzamiento_directo(
        _MotorActividadViejoDirecto, ["C:/tmp/a.png"], 30
    )
    check(
        "actividad directo: motor viejo -> sin kwargs de imagenes (no rompe)",
        directo_viejo
        and "imagenes" not in directo_viejo[0]
        and "probabilidad_imagen" not in directo_viejo[0]
        and directo_viejo[0].get("hashtags") == ["#mexico"],
        ascii(sorted(directo_viejo[0])) if directo_viejo else "sin capturas",
    )

    # ---------------- 7) Limpieza final ----------------
    check(
        "imagenes: el marcador data/.campana_activa quedo limpio",
        not (RAIZ / "data" / ".campana_activa").exists(),
    )


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
