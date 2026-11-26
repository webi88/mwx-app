#!/usr/bin/env bash
# Compila MWX APP para macOS (Apple Silicon o Intel) como .app + ZIP.
# Uso (en un Mac):  bash build_mwx_mac.sh
# NO incluye cuentas ni .env: solo el codigo, los iconos y config.json.
set -euo pipefail

cd "$(dirname "$0")"

VERSION="1.1.4"
ENTORNO=".venv-mwx-mac"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: este script empaqueta MWX APP para macOS (requiere macOS y ditto)."
    exit 1
fi

PY=""
if command -v python3.11 >/dev/null 2>&1; then
    PY="python3.11"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "ERROR: no se encontro python3.11 ni python3. Instala Python 3.11 desde https://www.python.org/downloads/"
    exit 1
fi

echo "============================================================"
echo " MWX APP para macOS (Python: $PY)"
echo "============================================================"
echo ""

if [ ! -x "$ENTORNO/bin/python" ]; then
    echo "[1/6] Creando entorno virtual limpio $ENTORNO ..."
    "$PY" -m venv "$ENTORNO"
else
    echo "[1/6] Reutilizando el entorno virtual $ENTORNO ..."
fi
VPY="$ENTORNO/bin/python"

echo "[2/6] Actualizando pip ..."
"$VPY" -m pip install --upgrade pip

echo "[3/6] Instalando dependencias de MWX APP y del build ..."
"$VPY" -m pip install -r requirements-mwx.txt "pyinstaller==6.11.1" "pillow==10.4.0"

echo "[4/6] Generando iconos ..."
if [ ! -f assets/mwx_logo.png ] || [ ! -f assets/mwx_app.ico ]; then
    "$VPY" assets/generar_icono.py
fi
if [ ! -f assets/mwx_app.icns ]; then
    "$VPY" - <<'PY'
from pathlib import Path

from PIL import Image

raiz = Path("assets")
imagen = Image.open(raiz / "mwx_logo.png").convert("RGBA")
if imagen.size != (1024, 1024):
    imagen = imagen.resize((1024, 1024), Image.LANCZOS)
imagen.save(raiz / "mwx_app.icns", format="ICNS")
print("assets/mwx_app.icns generado")
PY
fi

echo "[5/6] Empaquetando con PyInstaller (MWX APP.app) ..."
"$VPY" -m PyInstaller --noconfirm --clean mwx_app.spec

ARQ="$(uname -m)"
SALIDA="dist/MWX-APP-mac-${ARQ}-${VERSION}.zip"
CARPETA="dist/MWX-APP-mac-${ARQ}-${VERSION}"

if [ ! -d "dist/MWX APP.app" ]; then
    echo "ERROR: PyInstaller no genero dist/MWX APP.app"
    exit 1
fi

echo "[6/6] Generando $SALIDA ..."
rm -rf "$CARPETA"
mkdir -p "$CARPETA"
cp -R "dist/MWX APP.app" "$CARPETA/"
if [ -f config.json ]; then
    cp config.json "$CARPETA/"
else
    echo "AVISO: no hay config.json en la raiz; la app pedira la direccion del servidor al abrir."
fi
cp LEEME_MAC.txt "$CARPETA/"

ditto -c -k --sequesterRsrc --keepParent "$CARPETA" "$SALIDA"

echo ""
echo "LISTO: $SALIDA"
echo "Contenido: MWX APP.app + config.json + LEEME_MAC.txt"
echo "La app NO esta firmada: el primer arranque requiere clic derecho > Abrir."
