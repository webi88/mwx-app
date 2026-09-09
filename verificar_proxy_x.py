"""Verificación previa de proxies contra x.com.

Comprueba que cada proxy del pool pueda cargar x.com ANTES de gastar números
Grizzly. Muestra un resumen: cuántos OK, cuántos bloqueados (proveedor o X) y
cuántos con fallo transitorio.

Uso:
    .venv/Scripts/python.exe verificar_proxy_x.py                 # todo el pool
    .venv/Scripts/python.exe verificar_proxy_x.py --pais brasil   # un país
"""
import sys

from utils.proxies import ProxyManager


def main() -> None:
    pm = ProxyManager()

    pais = None
    if "--pais" in sys.argv:
        try:
            pais = sys.argv[sys.argv.index("--pais") + 1]
        except IndexError:
            print("Falta el país tras --pais")
            return

    proxies = pm.cargar_por_pais(pais) if pais else pm.cargar_proxies()
    if not proxies:
        print("No hay proxies para verificar.")
        return

    print(f"Verificando {len(proxies)} proxies contra x.com ...\n")

    ok = 0
    bloqueado_proveedor = 0
    bloqueado_x = 0
    transitorio = 0
    invalido = 0

    for p in proxies:
        res = pm.verificar_acceso_x(p)
        clave = pm._clave(p)
        if res.get("ok"):
            ok += 1
            print(f"  [OK]        {clave}  (HTTP {res.get('status')})")
        elif res.get("definitivo"):
            if "proveedor" in res.get("error", ""):
                bloqueado_proveedor += 1
            else:
                bloqueado_x += 1
            print(f"  [BLOQUEADO] {clave}  -> {res.get('error')}")
        else:
            transitorio += 1
            print(f"  [TIMEOUT]   {clave}  -> {res.get('error')}")

    print("\n--- RESUMEN ---")
    print(
        f"OK: {ok} | Bloqueado proveedor: {bloqueado_proveedor} | "
        f"Bloqueado X: {bloqueado_x} | Transitorio: {transitorio} | Inválido: {invalido}"
    )


if __name__ == "__main__":
    main()
