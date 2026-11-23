"""Tests rapidos del saneado de logs de proxies (sin red y sin tocar data/).

`ProxyManager._colapsar_repeticiones()` logueaba el proxy COMPLETO (usuario y
password) cuando colapsaba segmentos duplicados (`_area-MX_life-15_area-MX_life-15`).
Estos checks garantizan que:

  (1) el log solo muestra la version saneada (`***:***`, host:port) y NUNCA el
      usuario, el password ni `_session-XXX` crudos;
  (2) el colapso funcional sigue igual y `_session-XXX` se conserva;
  (3) `normalizar_guardado()` devuelve la version limpia de un proxy ya
      guardado (para que el pre-vuelo pueda persistirla en `Cuenta.proxy`);
  (4) los logs de proxy invalido de `normalizar()`, `regenerar_desde_base()` y
      `aplicar_a_options()` tambien quedan saneados.

Datos 100% SINTETICOS: ningun proxy ni credencial real. No usa red, Chrome ni
toca `data/`.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_proxies_logs.py   (solo este archivo)
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from loguru import logger  # noqa: E402

from utils.proxies import ProxyManager, _proxy_para_log  # noqa: E402

# Datos SINTETICOS (nunca reales).
USUARIO = "usuario_SINTETICO"
PASSWORD = "clave_SINTETICA"
SESION = "AbC12345"
HOST = "gateway.test:8080"
DUPLICADO = "_area-MX_life-15_area-MX_life-15"
URL_DUPLICADA = f"http://{USUARIO}{DUPLICADO}_session-{SESION}:{PASSWORD}@{HOST}"
URL_LIMPIA = f"http://{USUARIO}_area-MX_life-15_session-{SESION}:{PASSWORD}@{HOST}"
PLANO_DUPLICADO = f"{HOST}:{USUARIO}_area-MX_area-MX:{PASSWORD}"
PLANO_LIMPIO = f"{HOST}:{USUARIO}_area-MX:{PASSWORD}"


@contextmanager
def _capturar_logs(nivel="DEBUG"):
    """Captura los logs de loguru en una lista para inspeccionarlos."""
    registros = []
    sink = logger.add(lambda mensaje: registros.append(str(mensaje)), level=nivel)
    try:
        yield registros
    finally:
        logger.remove(sink)


def _texto(registros) -> str:
    return "\n".join(registros)


def _sin_secretos(texto: str) -> bool:
    return all(
        secreto not in texto
        for secreto in (USUARIO, PASSWORD, SESION, "clave_SINTETICA", "usuario_SINTETICO")
    )


def test_colapsar_log_saneado(check):
    """El colapso sigue funcionando y el warning no filtra credenciales."""
    pm = ProxyManager()
    with _capturar_logs() as registros:
        limpio = pm._colapsar_repeticiones(URL_DUPLICADA)

    check("colapsar: elimina el segmento duplicado", DUPLICADO not in limpio)
    check("colapsar: deja un solo _area-MX_life-15", limpio.count("_area-MX_life-15") == 1)
    check("colapsar: conserva _session-XXX", f"_session-{SESION}" in limpio)
    check(
        "colapsar: conserva la password para uso interno",
        PASSWORD in limpio,
        "(la cadena devuelta no es el log)",
    )
    check("colapsar: devuelve exactamente la version limpia", limpio == URL_LIMPIA)

    texto = _texto(registros)
    check("log: hubo el warning de colapso", "credenciales repetidas colapsado" in texto)
    check("log: NO expone el usuario", USUARIO not in texto)
    check("log: NO expone el password", PASSWORD not in texto)
    check("log: NO expone _session-XXX", SESION not in texto)
    check("log: conserva host:port para diagnostico", HOST in texto)
    check("log: enmascara las credenciales", "***:***" in texto)

    # Formato plano `host:port:user:pass` (tambien con duplicados).
    with _capturar_logs() as registros:
        plano = pm._colapsar_repeticiones(PLANO_DUPLICADO)
    check("colapsar plano: version limpia", plano == PLANO_LIMPIO)
    texto = _texto(registros)
    check("log plano: NO expone usuario/password", _sin_secretos(texto))
    check("log plano: conserva host:port", HOST in texto)


def test_proxy_para_log(check):
    """El helper de saneado enmascara credenciales y conserva host/puerto."""
    casos = (
        (f"https://{USUARIO}:{PASSWORD}@{HOST}", f"https://***:***@{HOST}"),
        (f"{USUARIO}:{PASSWORD}@{HOST}", f"***:***@{HOST}"),
        (f"{HOST}:{USUARIO}:{PASSWORD}", f"{HOST}:***:***"),
        (f"http://{HOST}", f"http://{HOST}"),
        (f"http://{USUARIO}_session-{SESION}:{PASSWORD}@{HOST}",
         f"http://***:***@{HOST}"),
    )
    for crudo, esperado in casos:
        saneado = _proxy_para_log(crudo)
        check(f"saneado: {crudo.split('@')[-1]}", saneado == esperado, f"({saneado})")
        check(
            f"saneado sin secretos: {crudo.split('@')[-1]}",
            _sin_secretos(saneado),
        )
    check("saneado: vacio", _proxy_para_log("") == "(vacio)")
    check("saneado: None no lanza", _proxy_para_log(None) == "(vacio)")
    generico = _proxy_para_log(f"{USUARIO}:{PASSWORD}")
    check("saneado: formato desconocido no filtra", _sin_secretos(generico))


def test_normalizar_guardado(check):
    """API publica para limpiar y persistir `Cuenta.proxy` sin tocar la sesion."""
    pm = ProxyManager()
    check(
        "guardado: colapsa URL duplicada",
        pm.normalizar_guardado(URL_DUPLICADA) == URL_LIMPIA,
    )
    check(
        "guardado: colapsa formato plano",
        pm.normalizar_guardado(PLANO_DUPLICADO) == PLANO_LIMPIO,
    )
    check(
        "guardado: sin duplicados devuelve lo mismo",
        pm.normalizar_guardado(URL_LIMPIA) == URL_LIMPIA,
    )
    limpio = pm.normalizar_guardado(URL_DUPLICADA)
    check("guardado: conserva _session-XXX", f"_session-{SESION}" in limpio)
    check("guardado: vacio -> ''", pm.normalizar_guardado("") == "")
    check("guardado: None -> ''", pm.normalizar_guardado(None) == "")
    check(
        "guardado: tolera texto con espacios",
        pm.normalizar_guardado(f"  {URL_DUPLICADA}  ") == URL_LIMPIA,
    )


def test_logs_proxy_invalido(check):
    """`normalizar`, `regenerar_desde_base` y `aplicar_a_options` no filtran."""
    pm = ProxyManager()
    invalido = f"http://{USUARIO}:{PASSWORD}@host_sin_puerto"

    with _capturar_logs() as registros:
        resultado = pm.normalizar(invalido)
    texto = _texto(registros)
    check("normalizar: proxy invalido devuelve None", resultado is None)
    check("normalizar: log sin usuario/password", _sin_secretos(texto))
    check("normalizar: log conserva el host", "host_sin_puerto" in texto)

    with tempfile.TemporaryDirectory() as tmp:
        pm.dir_paises = os.path.join(tmp, "proxies")
        with _capturar_logs() as registros:
            resultado = pm.regenerar_desde_base(
                f"http://{USUARIO}:{PASSWORD}@:8080"
            )
    texto = _texto(registros)
    check("regenerar: base invalida -> {}", resultado == {})
    check("regenerar: log sin usuario/password", _sin_secretos(texto))
    check("regenerar: log dice 'Proxy base invalido'", "Proxy base invalido" in texto)

    with _capturar_logs() as registros:
        resultado = pm.aplicar_a_options(object(), invalido, tag="test")
    texto = _texto(registros)
    check("aplicar: proxy invalido -> None (no dinamico)", resultado is None)
    check("aplicar: log sin usuario/password", _sin_secretos(texto))


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_colapsar_log_saneado(check)
    test_proxy_para_log(check)
    test_normalizar_guardado(check)
    test_logs_proxy_invalido(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_proxies_logs.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
