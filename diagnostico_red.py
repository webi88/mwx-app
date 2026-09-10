"""Autotest de red para diagnosticar el arranque de Selenium en Railway.

Prueba, desde DENTRO del contenedor:
  1) DNS + TCP al proxy de Smartproxy.
  2) Peticion HTTPS directa (sin proxy).
  3) Peticion HTTPS a x.com a traves del proxy local (mismo camino que Chrome).

La salida va a stdout/stderr para verla en `railway logs`.
"""
import socket
import sys
import time

from core.config import settings
from utils.proxies import ProxyManager


def _log(msg: str):
    print(f"[diag-red] {msg}", flush=True)


def main():
    pm = ProxyManager()
    proxy = settings.proxy_sticky_mx(session_id="diagnostico")
    info = pm.analizar(proxy)
    _log(f"proxy host={info['host']} port={info['port']}")

    # 1) DNS + TCP.
    try:
        addrs = socket.getaddrinfo(info["host"], info["port"], socket.AF_INET)
        _log(f"DNS IPv4 OK: {[a[4][0] for a in addrs][:4]}")
    except Exception as e:
        _log(f"DNS FAIL: {type(e).__name__}: {e}")
    try:
        t0 = time.time()
        s = socket.create_connection((info["host"], info["port"]), timeout=15)
        s.close()
        _log(f"TCP al proxy OK en {int((time.time()-t0)*1000)}ms")
    except Exception as e:
        _log(f"TCP al proxy FAIL: {type(e).__name__}: {e}")

    # 2) HTTPS directo (sin proxy).
    try:
        import requests
        r = requests.get("https://api.ipify.org?format=json", timeout=20)
        _log(f"HTTPS directo OK: {r.status_code} {r.text[:80]}")
    except Exception as e:
        _log(f"HTTPS directo FAIL: {type(e).__name__}: {e}")

    # 3) Logica real del bot: validar x.com y rotar la sesion si esta bloqueada.
    _log("validando x.com con rotacion de sesion (igual que el bot)...")
    for i in range(6):
        if pm.x_accesible(proxy, timeout=12):
            sesion = proxy.split("_session-")[1].split(":")[0] if "_session-" in proxy else "?"
            _log(f"x.com ACCESIBLE con sesion {sesion} tras {i} rotacion(es)")
            break
        _log(f"  sesion bloqueada por X; rotando ({i + 1}/6)")
        proxy = pm.refrescar_sesion(proxy)
    else:
        _log("x.com NO accesible tras 6 rotaciones")

    # 4) Mismo camino que Chrome (proxy local) con la sesion ya validada.
    try:
        from utils.forward_proxy import LocalForwardProxy
        import requests
        info = pm.analizar(proxy)
        fwd = LocalForwardProxy(info["host"], info["port"], info["user"], info["password"])
        port = fwd.start()
        url = f"http://127.0.0.1:{port}"
        r = requests.get(
            "https://x.com/",
            proxies={"http": url, "https": url},
            timeout=30,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Safari/537.36"},
        )
        _log(f"x.com via proxy local OK: {r.status_code} ({len(r.text)} bytes)")
        fwd.close()
    except Exception as e:
        _log(f"x.com via proxy local FAIL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _log(f"FALLO GLOBAL: {type(e).__name__}: {e}")
    sys.stdout.flush()
