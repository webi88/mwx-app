"""Tests rapidos del bootstrap de proxies por pais (sin red y sin tocar data/).

En Railway el volumen NO trae los archivos gitignored (`data/proxy_base.txt`,
`data/proxies/` y `data/proxies.txt`), asi que el contenedor arrancaba SIN
proxies. `utils/proxies.py` ahora resuelve el proxy base con
`ProxyManager._base_configurada()`: (a) la primera linea valida de
`data/proxy_base.txt` y (b) si el archivo no existe o no tiene lineas validas,
la variable de entorno `PROXY_BASE`; `regenerar_si_base()` (llamado desde
`entrypoint.sh` en cada arranque) usa ese helper y regenera 15 paises x 50.

Estos tests usan SIEMPRE un directorio temporal: reemplazan `pm.base_path`,
`pm.dir_paises`, `pm.archivo` y `pm.quemados_path` para no tocar `data/` real.
No usan red ni Chrome.

Cubre:
  (1) sin archivo ni PROXY_BASE -> "";
  (2) archivo con solo comentarios/vacios -> cae a PROXY_BASE;
  (3) PROXY_BASE con espacios/comentarios/vacios -> primera linea util;
  (4) archivo valido tiene prioridad sobre PROXY_BASE;
  (5) archivo con lineas invalidas -> toma la primera valida;
  (6) `regenerar_si_base()` NO regenera si ya hay proxies por pais (directorio
      intacto) aunque haya base configurada;
  (7) sin base disponible -> False y no crea archivos de pais;
  (8) PROXY_BASE + carpeta vacia -> True, 15 archivos de pais y 50 sesiones;
  (9) archivo valido + PROXY_BASE distinto -> regenera desde el archivo.

Uso:
    .venv/Scripts/python.exe tests/run_tests.py
    .venv/Scripts/python.exe tests/test_proxies_bootstrap.py   (solo este archivo)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# La raiz del repo a sys.path (mismo patron que los scripts del proyecto).
RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from utils.proxies import PAIS_ISO, ProxyManager  # noqa: E402

# Datos SINTETICOS: ningun host/credencial real.
BASE_ARCHIVO = "archivo.proxy.net:8080:user_archivo:pass_archivo"
BASE_ARCHIVO_NORMAL = "http://user_archivo:pass_archivo@archivo.proxy.net:8080"
BASE_ENV = "env.proxy.net:9090:user_env:pass_env"
BASE_ENV_NORMAL = "http://user_env:pass_env@env.proxy.net:9090"
BASE_ENV_OTRO = "otro.proxy.net:7070:user_otro:pass_otro"
BASE_ENV_OTRO_NORMAL = "http://user_otro:pass_otro@otro.proxy.net:7070"
PROXY_PAIS_VALIDO = "http://u:p@mex.proxy.net:3120"


def _manager(tmp: Path) -> ProxyManager:
    """ProxyManager con TODAS sus rutas dentro del temp dir (nunca data/)."""
    pm = ProxyManager()
    pm.base_path = str(tmp / "proxy_base.txt")
    pm.dir_paises = str(tmp / "proxies")
    pm.archivo = str(tmp / "proxies.txt")
    pm.quemados_path = str(tmp / "proxies" / "quemados.txt")
    return pm


def _escribir(ruta: Path, contenido: str) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(contenido, encoding="utf-8")


def _archivos_pais(pm: ProxyManager) -> list:
    carpeta = Path(pm.dir_paises)
    if not carpeta.is_dir():
        return []
    return sorted(p.name for p in carpeta.glob("*.txt"))


def test_base_configurada(check):
    """Helper `_base_configurada`: archivo -> PROXY_BASE -> ""."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pm = _manager(tmp)

        # (1) Sin archivo ni PROXY_BASE.
        with mock.patch.dict(os.environ, {"PROXY_BASE": ""}):
            check(
                "sin archivo ni PROXY_BASE: devuelve cadena vacia",
                pm._base_configurada() == "",
            )

        # (2) Archivo sin lineas validas (solo comentarios/vacias) -> env.
        _escribir(tmp / "proxy_base.txt", "# comentario\n\n   \n")
        with mock.patch.dict(os.environ, {"PROXY_BASE": f"  {BASE_ENV}  "}):
            check(
                "archivo sin lineas validas: cae a PROXY_BASE normalizado",
                pm._base_configurada() == BASE_ENV_NORMAL,
            )

        # (3) PROXY_BASE con comentarios/vacios: primera linea util.
        with mock.patch.dict(
            os.environ, {"PROXY_BASE": f"\n# comentario\n  {BASE_ENV_OTRO}  "}
        ):
            check(
                "PROXY_BASE con comentarios/vacios: normaliza la primera linea util",
                pm._base_configurada() == BASE_ENV_OTRO_NORMAL,
            )

        # (4) Prioridad del archivo sobre PROXY_BASE.
        _escribir(tmp / "proxy_base.txt", f"# lote\n{BASE_ARCHIVO}\n")
        with mock.patch.dict(os.environ, {"PROXY_BASE": BASE_ENV}):
            check(
                "archivo valido tiene prioridad sobre PROXY_BASE",
                pm._base_configurada() == BASE_ARCHIVO_NORMAL,
            )

        # (5) Archivo con una linea invalida y una valida: salta la invalida.
        _escribir(
            tmp / "proxy_base.txt",
            "no-es-un-proxy\n# x\n" + BASE_ARCHIVO + "\n",
        )
        with mock.patch.dict(os.environ, {"PROXY_BASE": BASE_ENV}):
            check(
                "archivo: salta lineas invalidas y toma la primera valida",
                pm._base_configurada() == BASE_ARCHIVO_NORMAL,
            )


def test_regenerar_si_base(check):
    """`regenerar_si_base`: no pisa lo existente, regenera desde archivo o env."""
    # (6) Ya hay proxies por pais: NO regenera y el directorio queda intacto,
    # aunque exista proxy base (archivo Y env).
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pm = _manager(tmp)
        ruta_mexico = tmp / "proxies" / "mexico.txt"
        _escribir(ruta_mexico, PROXY_PAIS_VALIDO + "\n")
        contenido_antes = ruta_mexico.read_text(encoding="utf-8")
        archivos_antes = _archivos_pais(pm)
        _escribir(tmp / "proxy_base.txt", f"# lote\n{BASE_ARCHIVO}\n")
        with mock.patch.dict(os.environ, {"PROXY_BASE": BASE_ENV}):
            resultado = pm.regenerar_si_base()
        check(
            "con proxies por pais: regenerar_si_base() devuelve False",
            resultado is False,
        )
        check(
            "con proxies por pais: el directorio queda intacto",
            archivos_antes == _archivos_pais(pm)
            and archivos_antes == ["mexico.txt"]
            and ruta_mexico.read_text(encoding="utf-8") == contenido_antes,
        )

    # (7) Sin base disponible: False y no crea archivos de pais.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pm = _manager(tmp)
        with mock.patch.dict(os.environ, {"PROXY_BASE": ""}):
            resultado = pm.regenerar_si_base()
        check("sin base disponible: devuelve False", resultado is False)
        check(
            "sin base disponible: no crea archivos de pais",
            _archivos_pais(pm) == [],
        )

    # (8) PROXY_BASE + carpeta vacia: regenera los 15 paises x 50 sesiones.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pm = _manager(tmp)
        with mock.patch.dict(os.environ, {"PROXY_BASE": BASE_ENV}):
            resultado = pm.regenerar_si_base()
        archivos = _archivos_pais(pm)
        check(
            "con PROXY_BASE y carpeta vacia: devuelve True",
            resultado is True,
        )
        check(
            "con PROXY_BASE: se crean los 15 archivos de pais",
            len(archivos) == len(PAIS_ISO) == 15,
            f"({len(archivos)} archivos)",
        )
        ruta_mexico = tmp / "proxies" / "mexico.txt"
        lineas = [
            s
            for s in ruta_mexico.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.startswith("#")
        ]
        check(
            "mexico.txt: 50 sesiones sticky generadas",
            len(lineas) == 50,
            f"({len(lineas)})",
        )
        check(
            "mexico.txt: sale del PROXY_BASE con _area-MX",
            all("env.proxy.net:9090" in s and "_area-MX" in s for s in lineas),
        )

    # (9) Archivo valido + PROXY_BASE distinto: regenera desde el ARCHIVO.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pm = _manager(tmp)
        _escribir(tmp / "proxy_base.txt", f"# lote\n{BASE_ARCHIVO}\n")
        with mock.patch.dict(os.environ, {"PROXY_BASE": BASE_ENV}):
            resultado = pm.regenerar_si_base()
        mexico = (tmp / "proxies" / "mexico.txt").read_text(encoding="utf-8")
        check(
            "archivo valido: regenera (True) aunque PROXY_BASE sea otro",
            resultado is True,
        )
        check(
            "archivo valido: usa host/usuario del archivo y no del env",
            "archivo.proxy.net:8080" in mexico
            and "user_archivo" in mexico
            and "env.proxy.net" not in mexico,
        )


def run(check):
    """Ejecuta los checks con el `check` del runner (o del marco local)."""
    test_base_configurada(check)
    test_regenerar_si_base(check)


if __name__ == "__main__":
    # Ejecucion suelta: `python tests/test_proxies_bootstrap.py`.
    from run_tests import check, resumen  # noqa: E402

    run(check)
    sys.exit(resumen())
