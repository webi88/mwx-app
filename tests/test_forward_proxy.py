"""Tests rapidos del forward proxy local (sin red externa y sin Chrome).

Cubre el ahorro de datos del proxy residencial en el modo local de X:

  (1) bloqueo LOCAL de media pesada de X (`.pbs.twimg.com`,
      `.video.twimg.com`, `.ton.twimg.com`) en `_tunnel` (ruta con upstream):
      la respuesta 403 sale del proxy y `_upstream` NI SE TOCA;
  (2) `abs.twimg.com` (JS/CSS), `upload.*` y dominios "parecidos"
      (`notpbs.twimg.com`) NO se bloquean;
  (3) env `PROXY_BLOQUEAR_MEDIA=0/false/no/off` desactiva el bloqueo;
  (4) contadores `_bytes_subida`/`_bytes_bajada` de `_relay` y
      `estadisticas()` (copia thread-safe, tambien en modo directo);
  (5) `close()` deja UNA linea INFO con el total (sin host ni credenciales).

NO usa red externa: los sockets son `socket.socketpair()` (solo kernel local) y
el upstream de `_tunnel` se reemplaza por un fake que nunca conecta.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_forward_proxy.py   (solo este archivo)
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from loguru import logger  # noqa: E402

from utils.forward_proxy import (  # noqa: E402
    _DOMINIOS_MEDIA,
    LocalForwardProxy,
    _bloqueo_media_activo,
    _host_bloqueado,
    _host_bloqueado_media,
)

CONNECT_MEDIA = "CONNECT pbs.twimg.com:443 HTTP/1.1"
RESPUESTA_403 = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"


class _ErrorUpstream(Exception):
    """Marca que `_tunnel` intento conectar al upstream (no debe pasar)."""


class _ClienteFake:
    """Cliente minimo para `_tunnel`: solo captura lo que el proxy le envia."""

    def __init__(self):
        self.enviado = []

    def sendall(self, datos):
        self.enviado.append(datos)

    def close(self):
        pass


@contextmanager
def _sin_env(nombre):
    """Quita una variable de entorno y la restaura al salir (nunca falla)."""
    previo = os.environ.pop(nombre, None)
    try:
        yield
    finally:
        if previo is not None:
            os.environ[nombre] = previo


def _proxy_con_upstream_fake(llamadas):
    """Proxy en modo proxy cuyo `_upstream` registra y falla (sin red)."""
    proxy = LocalForwardProxy("upstream.proxy.test", 8080)

    def _upstream_falso(*args, **kwargs):
        llamadas.append(1)
        raise _ErrorUpstream("sin red en tests")

    proxy._upstream = _upstream_falso
    return proxy


def _leer_hasta(sock, cantidad, timeout=3.0):
    """Lee hasta `cantidad` bytes o hasta timeout/EOF."""
    sock.settimeout(timeout)
    datos = b""
    while len(datos) < cantidad:
        try:
            parte = sock.recv(cantidad - len(datos))
        except socket.timeout:
            break
        if not parte:
            break
        datos += parte
    return datos


def test_dominios_media(check):
    """Bloqueo de media por sufijo: exacto, sin puerto, case-insensitive."""
    check("_DOMINIOS_MEDIA tiene pbs/video/ton", _DOMINIOS_MEDIA == (
        ".pbs.twimg.com", ".video.twimg.com", ".ton.twimg.com"))
    for host in (
        "pbs.twimg.com",
        "pbs.twimg.com:443",
        "PBS.TWIMG.COM:443",
        "video.twimg.com",
        "video.twimg.com:443",
        "ton.twimg.com:443",
        "pbs.twimg.com.",
    ):
        check(f"media bloqueada: {host}", _host_bloqueado_media(host) is True)

    for host in (
        "abs.twimg.com",
        "abs.twimg.com:443",
        "upload.twitter.com:443",
        "upload.x.com:443",
        "notpbs.twimg.com",
        "notvideo.twimg.com:443",
        "pbs.twimg.com.evil.test",
        "evil-pbs.twimg.com",
        "x.com",
        "",
        None,
    ):
        check(f"media NO bloqueada: {host!r}", _host_bloqueado_media(host) is False)

    check("regresion: google sigue bloqueado", _host_bloqueado("google.com:443"))
    check("regresion: notgoogle NO bloqueado", not _host_bloqueado("notgoogle.com"))


def test_bloqueo_media_activo(check):
    """`PROXY_BLOQUEAR_MEDIA` default activo y 0/false/no/off lo desactivan."""
    with _sin_env("PROXY_BLOQUEAR_MEDIA"):
        check("media: activo por defecto (env ausente)", _bloqueo_media_activo() is True)
    for valor in ("1", "true", "si", "on"):
        os.environ["PROXY_BLOQUEAR_MEDIA"] = valor
        try:
            check(f"media: activo con env={valor}", _bloqueo_media_activo() is True)
        finally:
            os.environ.pop("PROXY_BLOQUEAR_MEDIA", None)
    for valor in ("0", "false", "no", "off", "OFF"):
        os.environ["PROXY_BLOQUEAR_MEDIA"] = valor
        try:
            check(f"media: desactivado con env={valor}", _bloqueo_media_activo() is False)
        finally:
            os.environ.pop("PROXY_BLOQUEAR_MEDIA", None)


def test_tunnel_bloquea_media(check):
    """Con upstream y bloqueo activo: 403 local y el upstream ni se toca."""
    llamadas = []
    proxy = _proxy_con_upstream_fake(llamadas)
    cliente = _ClienteFake()

    with _sin_env("PROXY_BLOQUEAR_MEDIA"):
        proxy._tunnel(cliente, CONNECT_MEDIA, CONNECT_MEDIA.encode() + b"\r\n\r\n")

    check("tunnel media: responde 403 exacto", cliente.enviado == [RESPUESTA_403])
    check("tunnel media: NO se conecto al upstream", llamadas == [])
    check("tunnel media: proxy sigue en modo upstream", proxy._directo is False)

    # `abs.twimg.com` (JS/CSS) NO se bloquea: el proxy SI intenta el upstream.
    llamadas.clear()
    cliente = _ClienteFake()
    with _sin_env("PROXY_BLOQUEAR_MEDIA"):
        try:
            proxy._tunnel(
                cliente,
                "CONNECT abs.twimg.com:443 HTTP/1.1",
                b"CONNECT abs.twimg.com:443 HTTP/1.1\r\n\r\n",
            )
        except _ErrorUpstream:
            pass
    check("tunnel abs.twimg.com: si intenta el upstream", len(llamadas) >= 1)
    check("tunnel abs.twimg.com: no responde 403", RESPUESTA_403 not in cliente.enviado)


def test_tunnel_env_desactiva(check):
    """`PROXY_BLOQUEAR_MEDIA=0`: la media pasa al upstream (sin bloquear)."""
    llamadas = []
    proxy = _proxy_con_upstream_fake(llamadas)
    cliente = _ClienteFake()
    os.environ["PROXY_BLOQUEAR_MEDIA"] = "0"
    try:
        try:
            proxy._tunnel(cliente, CONNECT_MEDIA, CONNECT_MEDIA.encode() + b"\r\n\r\n")
        except _ErrorUpstream:
            pass
    finally:
        os.environ.pop("PROXY_BLOQUEAR_MEDIA", None)
    check("media off: SI intenta el upstream", len(llamadas) >= 1)
    check("media off: no responde 403", RESPUESTA_403 not in cliente.enviado)


def test_estadisticas(check):
    """`_relay` suma subida/bajada y `estadisticas()` devuelve una copia."""
    proxy = LocalForwardProxy()  # modo directo
    vacias = proxy.estadisticas()
    check(
        "estadisticas: arranca en cero",
        vacias == {"subida": 0, "bajada": 0, "total": 0},
    )

    proxy._running = True
    a_cliente, a_extremo = socket.socketpair()
    b_upstream, b_extremo = socket.socketpair()
    hilo = threading.Thread(
        target=proxy._relay, args=(a_cliente, b_upstream), daemon=True
    )
    hilo.start()
    try:
        a_extremo.sendall(b"hola")     # cliente -> upstream: 4 bytes (subida)
        b_extremo.sendall(b"mundo!")   # upstream -> cliente: 6 bytes (bajada)
        hacia_upstream = _leer_hasta(b_extremo, 4)
        hacia_cliente = _leer_hasta(a_extremo, 6)
        check("relay: el upstream recibe los 4 bytes", hacia_upstream == b"hola")
        check("relay: el cliente recibe los 6 bytes", hacia_cliente == b"mundo!")
        datos = proxy.estadisticas()
        check("estadisticas: subida = 4", datos["subida"] == 4, f"({datos['subida']})")
        check("estadisticas: bajada = 6", datos["bajada"] == 6, f"({datos['bajada']})")
        check("estadisticas: total = 10", datos["total"] == 10, f"({datos['total']})")
        datos["subida"] = 999
        check(
            "estadisticas: la copia no altera el contador interno",
            proxy.estadisticas()["subida"] == 4,
        )
    finally:
        proxy._running = False
        a_extremo.close()
        b_extremo.close()
        hilo.join(3.0)
    check("relay: el hilo termina sin colgarse", not hilo.is_alive())


def test_close_loguea_una_linea(check):
    """`close()` loguea UNA vez el total; sin trafico no loguea."""
    registros = []
    sink = logger.add(lambda mensaje: registros.append(str(mensaje)), level="INFO")
    try:
        proxy = LocalForwardProxy()
        proxy._bytes_subida = 2048
        proxy._bytes_bajada = 1024
        proxy.close()
        proxy.close()  # segundo close NO duplica la linea
        lineas = [r for r in registros if "Forward proxy: trafico" in r]
        check("close: UNA linea INFO de trafico", len(lineas) == 1, f"({len(lineas)})")
        if lineas:
            linea = lineas[0]
            check("close: incluye subida/bajada/total", all(
                clave in linea for clave in ("subida=", "bajada=", "total=")))
            check("close: muestra KB y MB", "KB" in linea and "MB" in linea)
            check(
                "close: sin host ni credenciales",
                "upstream.proxy" not in linea and "127.0.0.1" not in linea,
            )

        registros.clear()
        sin_trafico = LocalForwardProxy()
        sin_trafico.close()
        check(
            "close: sin trafico no loguea",
            not [r for r in registros if "Forward proxy: trafico" in r],
        )
    finally:
        logger.remove(sink)


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_dominios_media(check)
    test_bloqueo_media_activo(check)
    test_tunnel_bloquea_media(check)
    test_tunnel_env_desactiva(check)
    test_estadisticas(check)
    test_close_loguea_una_linea(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_forward_proxy.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
