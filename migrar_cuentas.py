"""
Script de migracion de cuentas desde el proyecto original (GestorTwitter).
Importa las cuentas del config.json y copia las cookies .pkl al nuevo proyecto.

Uso:
    python migrar_cuentas.py [ruta_al_config_original]
    
Ejemplo:
    python migrar_cuentas.py "C:/Users/23boy/Downloads/GestorTwitter/GestorTwitter/config.json"
"""

import sys
import json
import os
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from core.database import init_db, get_db_session
from core.models import Cuenta


ORIGINAL_DIR = "C:/Users/23boy/Downloads/GestorTwitter/GestorTwitter"
ORIGINAL_COOKIES = os.path.join(ORIGINAL_DIR, "cookies")
NUEVO_COOKIES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cookies", "twitter")


def migrar_cuentas(config_path: str):
    init_db()
    
    if not os.path.exists(config_path):
        print(f"ERROR: No existe el config: {config_path}")
        return
    
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    
    cuentas_originales = config.get("cuentas", [])
    print(f"\nEncontradas {len(cuentas_originales)} cuentas en config.json\n")
    
    os.makedirs(NUEVO_COOKIES, exist_ok=True)
    
    importadas = 0
    con_cookies = 0
    sin_cookies = 0
    duplicadas = 0
    
    with get_db_session() as db:
        for c in cuentas_originales:
            usuario = c.get("user", "").strip()
            if not usuario:
                continue
            
            tags = ", ".join(c.get("tags", []))
            grupo = c.get("grupo", "") or "A"
            plataforma = "twitter"
            
            existe = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
            if existe:
                duplicadas += 1
                print(f"  [SKIP] @{usuario} ya existe en la base de datos")
                continue
            
            cuenta = Cuenta(
                usuario=usuario,
                plataforma=plataforma,
                tags=tags,
                grupo=grupo,
                activa=True,
                cookies_path=f"data/cookies/twitter/{usuario}.pkl"
            )
            db.add(cuenta)
            importadas += 1
            
            # Copiar cookies del proyecto original si existen
            cookie_origen = os.path.join(ORIGINAL_COOKIES, f"{usuario}.pkl")
            cookie_destino = os.path.join(NUEVO_COOKIES, f"{usuario}.pkl")
            
            if os.path.exists(cookie_origen):
                shutil.copy2(cookie_origen, cookie_destino)
                con_cookies += 1
                estado = "✅ cookies copiadas"
            else:
                sin_cookies += 1
                estado = "⚠️ sin cookies (requiere login)"
            
            print(f"  [OK] @{usuario} | grupo {grupo} | tags: {tags} | {estado}")
        
        db.commit()
    
    # Cookies que existen pero no estan en config.json
    cookies_extra = []
    usuarios_config = [c.get("user") for c in cuentas_originales]
    if os.path.exists(ORIGINAL_COOKIES):
        for archivo in os.listdir(ORIGINAL_COOKIES):
            if archivo.endswith(".pkl"):
                usuario = archivo.replace(".pkl", "")
                if usuario not in usuarios_config:
                    cookies_extra.append(usuario)
    
    if cookies_extra:
        print(f"\n{'='*50}")
        print(f"IMPORTANDO COOKIES EXTRA ({len(cookies_extra)})")
        print(f"{'='*50}")
        with get_db_session() as db:
            for usuario in cookies_extra:
                existe = db.query(Cuenta).filter(Cuenta.usuario == usuario).first()
                if existe:
                    print(f"  [SKIP] @{usuario} ya existe")
                    continue
                cookie_origen = os.path.join(ORIGINAL_COOKIES, f"{usuario}.pkl")
                cookie_destino = os.path.join(NUEVO_COOKIES, f"{usuario}.pkl")
                shutil.copy2(cookie_origen, cookie_destino)
                cuenta = Cuenta(
                    usuario=usuario,
                    plataforma="twitter",
                    tags="",
                    grupo="A",
                    activa=True,
                    cookies_path=f"data/cookies/twitter/{usuario}.pkl"
                )
                db.add(cuenta)
                importadas += 1
                con_cookies += 1
                print(f"  [OK] @{usuario} | ✅ cookies copiadas (no estaba en config)")
            db.commit()
    
    print(f"\n{'='*50}")
    print("RESUMEN DE MIGRACION")
    print(f"{'='*50}")
    print(f"  Importadas:      {importadas}")
    print(f"  Duplicadas:      {duplicadas}")
    print(f"  Con cookies:     {con_cookies} (pueden iniciar sesion)")
    print(f"  Sin cookies:     {sin_cookies} (requieren login manual)")
    
    if cookies_extra:
        print(f"\n  Cookies sin cuenta en config.json:")
        for u in cookies_extra:
            print(f"    - @{u}")
    
    print(f"\nCuentas listas. Usa /cuentas para verlas o /cuentas_verificar para validar estado.")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ORIGINAL_DIR, "config.json")
    migrar_cuentas(config_path)