import sys
import os

sys.stdout.reconfigure(encoding="utf-8")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

print("""
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
""")

try:
    from streamlit.web import cli as stcli
except ImportError:
    print("❌ Streamlit no esta instalado en este Python.")
    print("   Ejecuta: pip install streamlit")
    sys.exit(1)

sys.argv = ["streamlit", "run", "web/app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=false"]
stcli.main()