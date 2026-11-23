@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MWX APP - Compilacion

echo ============================================================
echo  MWX APP - Compilacion del ejecutable (Windows 10/11 64-bit)
echo ============================================================
echo.

set "PY_CMD=python"
set "PY_VER_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py"
        set "PY_VER_ARGS=-3.11"
    )
)

%PY_CMD% %PY_VER_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo ERROR: No se encontro Python 3.10 o superior.
    echo Instala Python 3.11 de 64 bits desde https://www.python.org/downloads/
    goto :error
)

if not exist ".venv-mwx\Scripts\python.exe" (
    echo [1/5] Creando entorno virtual limpio .venv-mwx ...
    %PY_CMD% %PY_VER_ARGS% -m venv .venv-mwx
    if errorlevel 1 goto :error
) else (
    echo [1/5] Reutilizando el entorno virtual .venv-mwx ...
)

set "VPY=%CD%\.venv-mwx\Scripts\python.exe"

echo [2/5] Actualizando pip ...
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto :error

echo [3/5] Instalando dependencias de MWX APP ...
"%VPY%" -m pip install -r requirements-mwx.txt "pyinstaller==6.11.1" "pillow==10.4.0"
if errorlevel 1 goto :error

echo [4/5] Generando icono y logo ...
"%VPY%" assets\generar_icono.py
if errorlevel 1 goto :error

echo [5/5] Empaquetando con PyInstaller ...
"%VPY%" -m PyInstaller --noconfirm --clean mwx_app.spec
if errorlevel 1 goto :error

if exist "config.json" (
    copy /y "config.json" "dist\MWX APP\config.json" >nul
    echo config.json copiado junto al ejecutable.
)

echo [6/6] Generando ZIP de actualizacion ...
"%VPY%" empaquetar_mwx.py
if errorlevel 1 (
    echo ADVERTENCIA: no se pudo generar el ZIP de actualizacion.
)

echo.
echo ============================================================
echo  LISTO: dist\MWX APP\MWX APP.exe
echo ============================================================
echo  Entrega la carpeta COMPLETA dist\MWX APP\ y un config.json
echo  con {"api_url": "https://tu-servidor"} al lado del .exe.
echo  El equipo debe tener Chrome instalado e internet.
echo.
pause
exit /b 0

:error
echo.
echo La compilacion fallo. Revisa el mensaje anterior.
echo.
pause
exit /b 1
