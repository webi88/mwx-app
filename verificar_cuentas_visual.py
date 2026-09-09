"""Verificacion VISUAL de cuentas Twitter.

Abre cada cuenta en una ventana de Chrome visible (no headless), carga sus
cookies, toma una captura de pantalla en data/reportes/verificacion/ y
determina el estado real (activa / suspendida / limitada / sesion expirada).

Uso:
    python verificar_cuentas_visual.py
    python verificar_cuentas_visual.py --cuentas Red4THarfuch,verif_ambiental
    python verificar_cuentas_visual.py --pausa 3   # segundos viendo cada cuenta
"""

import argparse
import os
import pickle
import socket
import sys
import time

socket.setdefaulttimeout(20.0)

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")
sys.stdout.reconfigure(line_buffering=True)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger
from core.config import settings, resolver_ruta, detectar_chrome_version, obtener_chromedriver
from core.database import get_db_session
from core.models import Cuenta
import undetected_chromedriver as uc

settings.headless = False


def cargar_x(driver, espera_max: float = 45.0) -> bool:
    """Navega a X.com tolerante a timeouts de red (X suele tardar).
    Usa page_load_strategy=eager (configurado en las opciones de Chrome)
    para no colgarse con los WebSockets y la SPA de x.com."""
    inicio = time.time()
    while time.time() - inicio < espera_max:
        try:
            driver.get("https://x.com")
            time.sleep(6)
            return True
        except Exception:
            time.sleep(3)
    return False


def esperar_carga(driver, segundos: float = 15.0) -> bool:
    """Espera a que el DOM se estabilice sin usar timeouts estrictos."""
    from selenium.webdriver.common.by import By
    fin = time.time() + segundos
    intentos = 0
    while time.time() < fin:
        try:
            driver.execute_script("return document.readyState")
            intentos += 1
            if intentos > 3:
                return True
            time.sleep(1)
        except Exception:
            time.sleep(2)
    return True

SUSPENDIDA = "🚫"
LIMITADA = "⚠️"
EXPERIADA = "🔑"
OK = "✅"

CUENTAS_EXCLUIDAS = {
    "4T_puntodos", "verif_ambiental", "Red4THarfuch",
    "Voces4TMx", "redchaira", "Vocesdelsureste",
}

SENALES_SUSPENSION = [
    "account is suspended", "cuenta está suspendida", "cuenta suspendida",
    "account suspended", "your account has been suspended",
    "ha sido suspendida", "has been suspended",
]

SENALES_LIMITE = [
    "temporarily limited", "temporalmente limitad", "suspicious activity",
    "actividad sospechosa", "account has been locked", "cuenta ha sido bloqueada",
    "verify your identity", "verifica tu identidad", "confirm your identity",
    "confirma tu identidad", "are you a robot", "eres un robot",
    "we couldn't confirm you", "no pudimos confirmar", "verification required",
]


def clasificar_estado(driver) -> tuple:
    url = driver.current_url.lower()
    time.sleep(1)
    src = driver.page_source[:20000].lower()

    if "/account/suspended" in url or "/i/account_suspended" in url:
        return SUSPENDIDA, "URL de suspension (account suspended)"
    if "/i/flow/login" in url or "login" in url and "home" not in url:
        if any(s in src for s in SENALES_SUSPENSION):
            return SUSPENDIDA, "Sesion enviada a login + señal de suspension"
        return EXPERIADA, "Sesion expirada (redirige a login)"
    if "complete/account" in url or "challenge" in url or "/i/flow/security" in url:
        return LIMITADA, "Challenge de seguridad / verificacion requerida"

    for s in SENALES_SUSPENSION:
        if s in src:
            return SUSPENDIDA, f"Señal: {s}"
    for s in SENALES_LIMITE:
        if s in src:
            return LIMITADA, f"Señal: {s}"

    if any(m in src for m in ["home", "timeline", "data-testid='primaryColumn'"]):
        return OK, "Timeline cargado (sesion valida)"
    if "href=\"/login\"" in src or "sign in to x" in src:
        return EXPERIADA, "Pide iniciar sesion"
    return OK, "Sin señales de problema (ver captura)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cuentas", help="Lista separada por comas (opcional)")
    parser.add_argument("--pausa", type=float, default=4.0, help="Segundos viendo cada cuenta")
    parser.add_argument("--no-capturas", action="store_true", help="No guardar screenshots")
    args = parser.parse_args()

    capturas_dir = resolver_ruta("data/reportes/verificacion")
    os.makedirs(capturas_dir, exist_ok=True)

    with get_db_session() as db:
        if args.cuentas:
            nombres = [u.strip() for u in args.cuentas.split(",") if u.strip()]
            cuentas = db.query(Cuenta).filter(
                Cuenta.plataforma == "twitter", Cuenta.usuario.in_(nombres)
            ).all()
        else:
            cuentas = db.query(Cuenta).filter(
                Cuenta.plataforma == "twitter", Cuenta.activa == True
            ).all()

    if not cuentas:
        print("No hay cuentas de twitter para verificar.")
        return

    print(f"\n{'='*70}")
    print(f"  VERIFICACION VISUAL - {len(cuentas)} cuentas")
    print(f"  Chrome visible, capturas en: {capturas_dir}")
    print(f"{'='*70}\n")

    resultados = {SUSPENDIDA: [], LIMITADA: [], EXPERIADA: [], OK: []}
    version = detectar_chrome_version()

    for i, cuenta in enumerate(cuentas, 1):
        print(f"\n[{i}/{len(cuentas)}] @{cuenta.usuario} - abriendo Chrome...")
        driver = None
        try:
            options = uc.ChromeOptions()
            options.page_load_strategy = "eager"
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument(f"--user-data-dir={settings.profiles_dir / cuenta.usuario}")
            options.add_argument("--window-size=1366,768")

            print("   Iniciando descarga/arranque de ChromeDriver...")
            driver_path = obtener_chromedriver(version)
            print(f"   Chromedriver listo: {driver_path}")
            driver = uc.Chrome(
                driver_executable_path=driver_path,
                options=options,
                version_main=version,
                headless=False,
                use_subprocess=False,
            )
            driver.set_page_load_timeout(25)
            print("   Instancia de Chrome creada con éxito.")

            print("   Cargando x.com...")
            if not cargar_x(driver):
                print(f"   ⚠️ @{cuenta.usuario} - no se pudo cargar X.com (red)")
                resultados.setdefault("🌐", []).append(cuenta.usuario)
                continue
            esperar_carga(driver, 3)
            print(f"   Página cargada, URL actual: {driver.current_url}")

            cookies_path = resolver_ruta(f"data/cookies/twitter/{cuenta.usuario}.pkl")
            if os.path.exists(cookies_path):
                with open(cookies_path, "rb") as f:
                    cookies = pickle.load(f)
                for cookie in cookies:
                    try:
                        driver.add_cookie(cookie)
                    except Exception:
                        continue
            else:
                print(f"   ⚠️ Sin archivo de cookies para @{cuenta.usuario}")

            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(args.pausa)

            estado, motivo = clasificar_estado(driver)

            if not args.no_capturas:
                archivo = os.path.join(capturas_dir, f"{cuenta.usuario}.png")
                try:
                    driver.save_screenshot(archivo)
                except Exception as e:
                    print(f"   (no se pudo guardar captura: {e})")

            print(f"   {estado} @{cuenta.usuario} - {motivo}")
            resultados.setdefault(estado, []).append(cuenta.usuario)

            with get_db_session() as db2:
                c = db2.query(Cuenta).filter(Cuenta.id == cuenta.id).first()
                if c:
                    if cuenta.usuario in CUENTAS_EXCLUIDAS:
                        c.activa = False
                        print(f"   🔒 @{cuenta.usuario} - cuenta de riesgo: se mantiene INACTIVA")
                    else:
                        c.activa = (estado == OK)
                    db2.commit()

        except Exception as e:
            logger.error(f"Error con @{cuenta.usuario}: {e}")
            print(f"   ❌ @{cuenta.usuario} - error: {str(e)[:80]}")
            resultados.setdefault("❌", []).append(cuenta.usuario)
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    print(f"\n{'='*70}")
    print("  RESUMEN")
    print(f"{'='*70}")
    print(f"  {OK} Activas:  {len(resultados.get(OK, []))}   {', '.join(resultados.get(OK, []))}")
    print(f"  {SUSPENDIDA} Suspendidas: {len(resultados.get(SUSPENDIDA, []))}   {', '.join(resultados.get(SUSPENDIDA, []))}")
    print(f"  {LIMITADA} Limitadas: {len(resultados.get(LIMITADA, []))}   {', '.join(resultados.get(LIMITADA, []))}")
    print(f"  {EXPERIADA} Expiradas: {len(resultados.get(EXPERIADA, []))}   {', '.join(resultados.get(EXPERIADA, []))}")
    if "❌" in resultados:
        print(f"  ❌ Errores:  {len(resultados.get('❌'))}   {', '.join(resultados.get('❌'))}")
    if "🌐" in resultados:
        print(f"  🌐 Sin conexion:  {len(resultados.get('🌐'))}   {', '.join(resultados.get('🌐'))}")
    print(f"\n  Capturas guardadas en: {capturas_dir}")
    print()


if __name__ == "__main__":
    main()