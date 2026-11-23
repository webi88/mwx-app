# -*- coding: utf-8 -*-
"""Tests de la pestana "🚦 Verificación" de Cuentas (dashboard web).

Cubren SIN tocar `data/` real:

  (1) `_listar_cuentas` expone los campos del cajon de verificacion
      (`verificacion_pendiente/motivo/origen/fecha`) y es tolerante con una
      cuenta/BD vieja sin esas columnas (getattr -> False/"").
  (2) Helpers del cajon: `_es_motivo_verificacion_humana` detecta
      login/captcha/onboarding (sin tildes ni mayusculas) y no confunde otros
      motivos.
  (3) `_validar_y_reactivar`: SOLO llama `marcar_verificada` cuando el
      validador devuelve `active`; un estado distinto (expired/suspended/...)
      deja la cuenta en el cajon. Una excepcion por cuenta no rompe el bucle.
  (4) AppTest de `render()` con la pestana seleccionada: 0 pendientes muestra
      el `st.info`; con pendientes renderiza metricas, tabla, multiselect con
      TODO preseleccionado, la lista para copiar y el boton de reactivar
      (pulsado con fakes: `marcar_verificada` recibe cada usuario).

Los fakes se instalan DENTRO del script de AppTest (solo ASCII) y se restauran
al terminar cada bloque, para no contaminar el resto de la suite.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from web.operaciones import cuentas  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes de BD (nunca se escribe en la BD real)
# ---------------------------------------------------------------------------


class _QueryFalsa:
    def __init__(self, filas):
        self._filas = list(filas)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._filas)

    def first(self):
        return self._filas[0] if self._filas else None


class _DBFalsa:
    def __init__(self, filas):
        self._filas = list(filas)

    def query(self, *args, **kwargs):
        return _QueryFalsa(self._filas)


@contextmanager
def _sesion_falsa(filas):
    yield _DBFalsa(filas)


def _cuenta_fake(usuario, **extra):
    """Objeto tipo Cuenta (SimpleNamespace) con los atributos que lee la UI."""
    base = {
        "usuario": usuario,
        "plataforma": "twitter",
        "email": f"{usuario}@example.com",
        "status": "active",
        "last_checked": None,
        "cookies_json": None,
    }
    base.update(extra)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# (1) _listar_cuentas con los campos del cajon
# ---------------------------------------------------------------------------


def _probar_listar_cuentas(check) -> None:
    original = cuentas.get_db_session
    try:
        cuentas._listar_cuentas.clear()
        cuentas.get_db_session = lambda: _sesion_falsa(
            [
                _cuenta_fake(
                    "cuenta_pendiente",
                    verificacion_pendiente=True,
                    verificacion_motivo="login requerido (anti-bot)",
                    verificacion_origen="campana",
                    verificacion_fecha=datetime(2026, 11, 1, 10, 0),
                ),
                _cuenta_fake("cuenta_vieja"),
            ]
        )
        filas = cuentas._listar_cuentas(cuentas.OPCION_TODAS, None)
    finally:
        cuentas.get_db_session = original
        cuentas._listar_cuentas.clear()

    por_usuario = {fila["usuario"]: fila for fila in filas}
    pendiente = por_usuario.get("cuenta_pendiente") or {}
    vieja = por_usuario.get("cuenta_vieja") or {}

    check(
        "verificacion web: _listar_cuentas incluye los 4 campos del cajon",
        {"verificacion_pendiente", "verificacion_motivo",
         "verificacion_origen", "verificacion_fecha"} <= set(pendiente),
        str(sorted(pendiente)),
    )
    check(
        "verificacion web: la cuenta pendiente se marca como tal con su motivo",
        pendiente.get("verificacion_pendiente") is True
        and pendiente.get("verificacion_motivo") == "login requerido (anti-bot)"
        and pendiente.get("verificacion_origen") == "campana",
    )
    check(
        "verificacion web: verificacion_fecha se formatea con _fmt_fecha",
        pendiente.get("verificacion_fecha") == "2026-11-01 10:00",
        str(pendiente.get("verificacion_fecha")),
    )
    check(
        "verificacion web: una cuenta/BD vieja sin columnas -> False y ''",
        vieja.get("verificacion_pendiente") is False
        and vieja.get("verificacion_motivo") == ""
        and vieja.get("verificacion_origen") == ""
        and vieja.get("verificacion_fecha") == "",
        str(vieja.get("verificacion_pendiente")),
    )

    # Motivos humanos vs otros (sin tildes, sin mayusculas).
    check(
        "verificacion web: detecta motivos de login/captcha/onboarding",
        cuentas._es_motivo_verificacion_humana("Login requerido")
        and cuentas._es_motivo_verificacion_humana(
            "Cloudflare challenge (anti-bot)"
        )
        and cuentas._es_motivo_verificacion_humana(
            "Onboarding de teléfono"
        ),
    )
    check(
        "verificacion web: NO confunde cuotas/errores normales con verificacion",
        not cuentas._es_motivo_verificacion_humana("cuota agotada")
        and not cuentas._es_motivo_verificacion_humana("")
        and not cuentas._es_motivo_verificacion_humana(None),
    )


# ---------------------------------------------------------------------------
# (3) _validar_y_reactivar solo borra la verificacion si el estado es active
# ---------------------------------------------------------------------------


def _probar_validar_y_reactivar(check) -> None:
    from core import verificacion
    from plataformas.twitter import session_validator

    original_sesion = cuentas.get_db_session
    original_validar = session_validator.validar_cuenta
    original_marcar = verificacion.marcar_verificada

    cuentas_validas = {
        "cuenta_uno": _cuenta_fake("cuenta_uno", auth_token="tok", status="active"),
        "cuenta_dos": _cuenta_fake(
            "cuenta_dos", auth_token="tok", status="expired"
        ),
        "cuenta_tres": _cuenta_fake(
            "cuenta_tres", auth_token="tok", status="active"
        ),
    }
    marcadas = []

    def _validar_fake(cuenta):
        if cuenta.usuario == "cuenta_tres":
            raise RuntimeError("fallo de red simulado")
        return "active" if cuenta.usuario == "cuenta_uno" else "expired"

    def _marcar_fake(usuario, plataforma="twitter"):
        marcadas.append(usuario)
        return True

    # `_validar_y_reactivar` abre UNA sesion por usuario, en el orden de la
    # lista de entrada: se entrega la cuenta correspondiente (y None para la
    # fantasma).
    cola = iter(
        [
            cuentas_validas["cuenta_uno"],
            cuentas_validas["cuenta_dos"],
            cuentas_validas["cuenta_tres"],
            None,
        ]
    )

    try:
        cuentas.get_db_session = lambda: _sesion_falsa([next(cola, None)])
        session_validator.validar_cuenta = _validar_fake
        verificacion.marcar_verificada = _marcar_fake
        resumen = cuentas._validar_y_reactivar(
            ["cuenta_uno", "cuenta_dos", "cuenta_tres", "cuenta_fantasma"]
        )
    finally:
        cuentas.get_db_session = original_sesion
        session_validator.validar_cuenta = original_validar
        verificacion.marcar_verificada = original_marcar

    check(
        "verificacion web: validar solo reactiva las cuentas en estado active",
        resumen.get("reactivadas") == ["cuenta_uno"]
        and marcadas == ["cuenta_uno"],
        f"(reactivadas={resumen.get('reactivadas')}, marcadas={marcadas})",
    )
    check(
        "verificacion web: un estado distinto de active NO borra la verificacion",
        resumen.get("no_activas") == [("cuenta_dos", "expired")]
        and "cuenta_tres" not in [u for u, _ in resumen.get("no_activas")],
        str(resumen.get("no_activas")),
    )
    check(
        "verificacion web: una excepcion por cuenta no rompe el bucle",
        any("cuenta_tres" in e for e in resumen.get("errores") or [])
        and any("cuenta_fantasma" in e for e in resumen.get("errores") or []),
        str(resumen.get("errores")),
    )
    check(
        "verificacion web: revisadas cuenta todas las entradas",
        resumen.get("revisadas") == 4,
        str(resumen.get("revisadas")),
    )
    vacio = cuentas._validar_y_reactivar([])
    check(
        "verificacion web: validar sin seleccion devuelve un resumen vacio",
        vacio.get("revisadas") == 0
        and vacio.get("reactivadas") == []
        and vacio.get("no_activas") == []
        and vacio.get("errores") == [],
    )


# ---------------------------------------------------------------------------
# (4) AppTest de la pestana "Verificacion"
# ---------------------------------------------------------------------------


def _app_verificacion_vacia():
    """AppTest (solo ASCII): render de la pestana con 0 pendientes."""
    import streamlit as st

    import core.verificacion as verificacion
    from web.operaciones import cuentas as pagina

    verificacion.contar_pendientes = lambda plataforma="twitter": 0
    verificacion.listar_pendientes = lambda plataforma="twitter", limite=1000: []

    def _cuentas_falsas(*args, **kwargs):
        return []

    _cuentas_falsas.clear = lambda: None
    pagina._listar_cuentas = _cuentas_falsas

    tab = [t for t in pagina.TABS if "Verificaci" in t][0]
    st.session_state["cuentas_modo_selector"] = pagina._modo_de_tab(tab)
    st.session_state["cuentas_pestana_selector"] = tab
    pagina.render({"rol": "admin", "username": "admin"})


def _app_verificacion_pendientes():
    """AppTest (solo ASCII): render de la pestana con 2 pendientes."""
    import streamlit as st

    import core.verificacion as verificacion
    from web.operaciones import cuentas as pagina

    pendientes = [
        {
            "usuario": "cuenta_uno",
            "motivo": "login requerido (anti-bot)",
            "origen": "campana",
            "fecha": "2026-11-01T10:00:00",
            "nombre_mostrado": "",
            "handle_actual": "",
            "tier_calidad": "tier1",
            "seccion": "LIB",
            "status": "expired",
            "activa": True,
        },
        {
            "usuario": "cuenta_dos",
            "motivo": "captcha al publicar",
            "origen": "scheduler",
            "fecha": "2026-11-02T08:30:00",
            "nombre_mostrado": "",
            "handle_actual": "",
            "tier_calidad": "",
            "seccion": "",
            "status": "error",
            "activa": True,
        },
    ]

    def _contar(plataforma="twitter"):
        return len(pendientes)

    def _listar(plataforma="twitter", limite=1000):
        return list(pendientes)

    def _marcar(usuario, plataforma="twitter"):
        llamadas = list(st.session_state.get("verif_llamadas", []))
        llamadas.append(usuario)
        st.session_state["verif_llamadas"] = llamadas
        return True

    verificacion.contar_pendientes = _contar
    verificacion.listar_pendientes = _listar
    verificacion.marcar_verificada = _marcar
    verificacion.marcar_pendiente = (
        lambda usuario, motivo="", origen="", plataforma="twitter": True
    )

    def _cuentas_falsas(*args, **kwargs):
        return []

    _cuentas_falsas.clear = lambda: None
    pagina._listar_cuentas = _cuentas_falsas

    tab = [t for t in pagina.TABS if "Verificaci" in t][0]
    st.session_state["cuentas_modo_selector"] = pagina._modo_de_tab(tab)
    st.session_state["cuentas_pestana_selector"] = tab
    pagina.render({"rol": "admin", "username": "admin"})


def _app_test_verificacion_vacia() -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    resultado = {"sin_excepciones": False, "info": False, "metrica": False}
    try:
        at = AppTest.from_function(_app_verificacion_vacia, default_timeout=60)
        at.run()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", resultado
    if at.exception:
        return False, str(at.exception[0].value)[:300], resultado
    resultado["sin_excepciones"] = True
    resultado["info"] = any(
        "No hay cuentas pendientes" in str(i.value) for i in at.info
    )
    resultado["metrica"] = any(
        "Pendientes" in str(m.label) and str(m.value) == "0" for m in at.metric
    )
    return True, "", resultado


def _app_test_verificacion_pendientes() -> tuple[bool, str, dict]:
    from streamlit.testing.v1 import AppTest

    resultado = {
        "sin_excepciones": False,
        "metricas": False,
        "tabla": False,
        "multiselect": False,
        "default_todas": False,
        "boton_marcar": False,
        "boton_validar": False,
        "codigo": False,
        "llamadas": False,
        "flash": False,
    }
    try:
        at = AppTest.from_function(
            _app_verificacion_pendientes, default_timeout=60
        )
        at.run()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", resultado
    if at.exception:
        return False, str(at.exception[0].value)[:300], resultado
    resultado["sin_excepciones"] = True

    datos_metricas = {str(m.label): str(m.value) for m in at.metric}
    resultado["metricas"] = datos_metricas.get("🚦 Pendientes") == "2"
    resultado["tabla"] = len(at.dataframe) >= 1

    claves_multi = {m.key for m in at.multiselect}
    resultado["multiselect"] = "verif_selector" in claves_multi
    if resultado["multiselect"]:
        widget = at.multiselect(key="verif_selector")
        resultado["default_todas"] = list(widget.value) == [
            "cuenta_uno",
            "cuenta_dos",
        ]

    claves_botones = {b.key for b in at.button}
    resultado["boton_marcar"] = "btn_verif_marcar" in claves_botones
    resultado["boton_validar"] = "btn_verif_validar" in claves_botones
    resultado["codigo"] = any(
        "cuenta_uno" in str(c.value) and "cuenta_dos" in str(c.value)
        for c in at.code
    )

    if resultado["boton_marcar"]:
        try:
            at2 = at.button(key="btn_verif_marcar").click().run()
            error = str(at2.exception[0].value)[:200] if at2.exception else ""
        except Exception as e:  # pragma: no cover
            at2 = at
            error = f"{type(e).__name__}: {e}"
        resultado["llamadas"] = list(
            at2.session_state["verif_llamadas"]
            if "verif_llamadas" in at2.session_state
            else []
        ) == ["cuenta_uno", "cuenta_dos"]
        resultado["flash"] = any(
            "reactivadas" in str(s.value).lower() for s in at2.success
        )
        if error:
            return False, error, resultado
    return True, "", resultado


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(check) -> None:
    _probar_listar_cuentas(check)
    _probar_validar_y_reactivar(check)

    ok, detalle, datos = _app_test_verificacion_vacia()
    check(
        "verificacion web: con 0 pendientes la pagina renderiza sin excepciones "
        "(AppTest)",
        ok,
        detalle,
    )
    check(
        "verificacion web: con 0 pendientes muestra el info y la metrica en 0",
        datos["sin_excepciones"] and datos["info"] and datos["metrica"],
        str({k: v for k, v in datos.items() if not v}) or "ok",
    )

    ok, detalle, datos = _app_test_verificacion_pendientes()
    check(
        "verificacion web: con pendientes la pagina renderiza sin excepciones "
        "(AppTest)",
        ok,
        detalle,
    )
    check(
        "verificacion web: la tabla/multiselect preseleccionan TODAS las "
        "pendientes",
        datos["metricas"]
        and datos["tabla"]
        and datos["multiselect"]
        and datos["default_todas"],
        str({k: v for k, v in datos.items() if not v}) or "ok",
    )
    check(
        "verificacion web: muestra la lista para copiar y los botones "
        "reactivar/validar",
        datos["codigo"] and datos["boton_marcar"] and datos["boton_validar"],
        str({k: v for k, v in datos.items() if not v}) or "ok",
    )
    check(
        "verificacion web: pulsar reactivar llama marcar_verificada por cada "
        "seleccionada y muestra el success",
        datos["llamadas"] and datos["flash"],
        str({k: v for k, v in datos.items() if not v}) or "ok",
    )


if __name__ == "__main__":
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
