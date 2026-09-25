"""Smoke REAL con Chrome del fix de inyeccion de cookies por CDP.

Manual (NO lo ejecuta `run_tests.py`). Reproduce el caso de produccion de
Railway ("invalid cookie domain" con Chrome 154): toma 1-2 cuentas de la BD
LOCAL que NO tengan `.pkl` y cuyo `cookies_json` este vacio (solo `auth_token`),
y llama `bot.login_con_cookies()` (la misma via del dashboard/Reportar).

Comprueba que:
  1. `Network.setCookie` (CDP) inyecta las cookies SIN depender del documento.
  2. Desaparece `invalid cookie domain` (bug de `driver.add_cookie`).
  3. Reporta el resultado real: sesion confirmada con ct0, o
     "X no emitio ct0"/"sesion expirada" (auth_token vencido => NO es bug).

PROHIBIDO publicar/reportar/enviar: este script SOLO hace login y cierra Chrome
(`bot.cerrar()` en `finally`).

Uso:
    .venv/Scripts/python.exe tests/smoke_login_cdp.py
    SMOKE_USUARIOS="cuentaA,cuentaB" .venv/Scripts/python.exe tests/smoke_login_cdp.py
    SMOKE_CUANTAS=2 .venv/Scripts/python.exe tests/smoke_login_cdp.py

Requiere Chrome instalado. Corre sin proxy (`TWITTER_SIN_PROXY=1`) por defecto.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

os.environ.setdefault("HEADLESS", "true")
os.environ.setdefault("TWITTER_SIN_PROXY", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///data/gestor_redes.db")

from loguru import logger  # noqa: E402

from core.database import get_db_session  # noqa: E402
from core.models import Cuenta  # noqa: E402
from plataformas.twitter.selenium_bot import TwitterBot  # noqa: E402


def _candidatas(cantidad: int = 2) -> list:
    """Cuentas locales: auth_token presente, SIN `.pkl` y cookies_json vacio.

    Es el path exacto del bug (el flujo usa `_cookies_auth_token` para que X
    emita ct0). Nunca lanza: ante error devuelve [].
    """
    try:
        con_pkl = {
            p.stem for p in (RAIZ / "data" / "cookies" / "twitter").glob("*.pkl")
        }
        elegidas = []
        with get_db_session() as db:
            for cuenta in db.query(Cuenta).filter(Cuenta.plataforma == "twitter").all():
                if not (cuenta.auth_token or "").strip():
                    continue
                if cuenta.usuario in con_pkl:
                    continue
                if cuenta.cookies_json:
                    continue
                elegidas.append(cuenta.usuario)
        return elegidas[:cantidad]
    except Exception as e:
        print(f"  (aviso) no se pudieron listar candidatas: {type(e).__name__}: {e}")
        return []


def main() -> int:
    usuarios = [
        u.strip()
        for u in (os.environ.get("SMOKE_USUARIOS") or "").split(",")
        if u.strip()
    ]
    if not usuarios:
        usuarios = _candidatas(int(os.environ.get("SMOKE_CUANTAS", "2") or "2"))
    if not usuarios:
        print("No hay cuentas locales sin .pkl y con auth_token para el smoke.")
        print("\nRESULTADO SMOKE: 0/0")
        return 1

    print(
        f"SMOKE login CDP con {usuarios} "
        f"(headless={os.environ.get('HEADLESS')}, "
        f"sin_proxy={os.environ.get('TWITTER_SIN_PROXY')})"
    )

    total = 0
    pasados = 0
    viven = 0
    vencidas = 0
    inyeccion_ok = 0

    for usuario in usuarios:
        capturas: list = []
        # DEBUG: el bug original logueaba "invalid cookie domain" en debug.
        sink = logger.add(lambda m: capturas.append(m), level="DEBUG")
        bot = TwitterBot(usuario)
        try:
            ok = bool(bot.login_con_cookies())
            lineas = [m.record["message"] for m in capturas]
            cdp = [l for l in lineas if "cookies inyectadas via CDP" in l]
            invalid = [
                l for l in lineas
                if "invalid cookie domain" in str(l).lower()
            ]
            sin_ct0 = [
                l for l in lineas
                if ("no emitio ct0" in l) or ("no emitió ct0" in l)
            ]
            error = (bot.ultimo_error or "").strip()

            total += 1
            if cdp and not invalid:
                inyeccion_ok += 1
                pasados += 1
            elif ok and not invalid:
                # CDP pudo no registrar su linea si el .pkl aparecio entre
                # medio, pero tampoco hubo "invalid cookie domain".
                inyeccion_ok += 1
                pasados += 1
            if ok:
                viven += 1
            elif sin_ct0 or "expirad" in error.lower() or "invalid" in error.lower():
                vencidas += 1

            print(f"  @{usuario}: login_con_cookies() -> {ok}", flush=True)
            print(f"    CDP: {cdp[0] if cdp else 'sin linea de inyeccion CDP'}", flush=True)
            print(f"    'invalid cookie domain': {len(invalid)}", flush=True)
            print(
                "    ct0: "
                + ("NO emitido (auth_token vencido/bloqueado)"
                   if sin_ct0 else "emitido o sesion no evaluada"),
                flush=True,
            )
            print(f"    ultimo_error: {error or '(vacio)'}", flush=True)
        finally:
            try:
                bot.cerrar()
            except Exception:
                pass
            logger.remove(sink)

    print("\nRESUMEN:")
    print(f"  inyeccion CDP sin 'invalid cookie domain': {inyeccion_ok}/{total}")
    print(f"  sesiones vivas (ct0 emitido): {viven}/{total}")
    print(f"  auth_token vencido/sesion bloqueada: {vencidas}/{total}")
    print("\nRESULTADO SMOKE: " + f"{pasados}/{total}")
    return 0 if pasados == total else 1


if __name__ == "__main__":
    sys.exit(main())
