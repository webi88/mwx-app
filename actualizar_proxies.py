"""Actualiza los proxies por pais desde UN solo proxy movil Smartproxy.

Smartproxy permite apuntar a distintos paises anadiendo _area-XX_ al USUARIO
(junto con _life-XX para la duracion sticky y _session-XXX para la sesion). El
password queda intacto. Por eso solo se mantiene UN proxy base y este script
regenera data/proxies/{pais}.txt para todos los paises.

Uso:
    python actualizar_proxies.py "proxy.smartproxy.net:3120:usuario:password"
    python actualizar_proxies.py "proxy.smartproxy.net:3120:usuario:password" --sesiones 50
    python actualizar_proxies.py          # lee data/proxy_base.txt o PROXY_BASE
"""
import sys

from utils.proxies import ProxyManager


def main():
    pm = ProxyManager()
    sesiones = 50
    args = [a for a in sys.argv[1:]]

    if "--sesiones" in args:
        i = args.index("--sesiones")
        try:
            sesiones = int(args[i + 1])
        except (IndexError, ValueError):
            pass
        del args[i:i + 2]

    proxy_base = args[0] if args else None
    if proxy_base:
        proxy_base = pm.normalizar(proxy_base) or proxy_base
    else:
        # Mismo criterio que `ProxyManager._base_configurada()`: primero las
        # lineas validas de data/proxy_base.txt y, si no hay, PROXY_BASE
        # (Railway no sube los archivos gitignored).
        proxy_base = pm._base_configurada() or None

    if not proxy_base:
        print("No se proporciono proxy base. Uso:")
        print('  python actualizar_proxies.py "host:port:user:pass"')
        print("  (o define PROXY_BASE / data/proxy_base.txt)")
        return

    resultado = pm.regenerar_desde_base(proxy_base, sesiones_por_pais=sesiones)
    print("Proxies actualizados:")
    for pais, n in resultado.items():
        print(f"  {pais}: {n}")
    print(f"Total: {sum(resultado.values())} proxies en data/proxies/")


if __name__ == "__main__":
    main()
