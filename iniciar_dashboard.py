"""Lanzador del dashboard web (Streamlit).

Importar este modulo NO arranca nada: todo el flujo vive en `main()` y solo se
ejecuta con `python iniciar_dashboard.py` (guard `if __name__ == "__main__"`).
Antes, cualquier `import iniciar_dashboard` lanzaba un servidor Streamlit."""
import os
import sys

MENSAJE_BIENVENIDA = """
╔══════════════════════════════════════╗
║        DASHBOARD WEB MWX.app         ║
║      Sistema de Comunicacion         ║
║      Estrategica | By MW Group       ║
╚══════════════════════════════════════╝

  Iniciando Streamlit...
  URL local:  http://localhost:8501

  Usuario por defecto: admin / admin
  (Cambiala en data/web_users.json)

  Para detener: Ctrl + C
"""


def main() -> int:
    """Arranca Streamlit sirviendo `web/app.py`.

    Devuelve el codigo de salida (1 si falta Streamlit). Conserva el mensaje
    amigable original."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, ".")

    print(MENSAJE_BIENVENIDA)

    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print("❌ Streamlit no esta instalado en este Python.")
        print("   Ejecuta: pip install streamlit")
        return 1

    sys.argv = [
        "streamlit",
        "run",
        "web/app.py",
        "--server.port=8501",
        "--server.address=0.0.0.0",
        "--server.headless=false",
    ]
    stcli.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
