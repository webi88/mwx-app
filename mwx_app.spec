# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller (onedir) del ejecutable MWX APP.

Uso:
    pyinstaller --noconfirm --clean mwx_app.spec

En Windows/Linux genera ``dist/MWX APP/MWX APP.exe``; en macOS envuelve el
resultado en ``dist/MWX APP.app`` (icono ``.icns``, sin firma). La app NO
incluye cuentas ni .env: solo el codigo, el icono y el logo. La configuracion
del servidor se entrega como ``config.json`` junto a la app.
"""
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

RAIZ = os.path.abspath(globals().get("SPECPATH") or os.getcwd())

if sys.platform == "darwin":
    ICONO = os.path.join(RAIZ, "assets", "mwx_app.icns")
else:
    ICONO = os.path.join(RAIZ, "assets", "mwx_app.ico")

EXCLUIR = [
    "streamlit",
    "pandas",
    "pyarrow",
    "numpy",
    "altair",
    "pydeck",
    "faker",
    "apscheduler",
    "telegram",
    "bot",
    "bot_clientes",
    "alertas",
    "scheduler",
    "cuentas",
    "web",
    "psycopg2",
    "openpyxl",
    "feedparser",
    "curl_cffi",
    "ia.generador_imagenes",
    "ia.filtros_alertas",
    "ia.celulas",
    "ia.contexto_noticias",
    "activaciones.motor",
    "activaciones.cuotas",
    "plataformas.twitter.api_http",
    "plataformas.twitter.perfil",
    "plataformas.twitter.session_validator",
    "plataformas.facebook",
    "plataformas.instagram",
    "plataformas.tiktok",
    "utils.captcha_solver",
    "utils.humaner",
    "utils.humanizer",
    "utils.lector_correo",
    "utils.url_utils",
]

OCULTOS = [
    "mwx_app",
    "mwx_app.config",
    "mwx_app.api_client",
    "mwx_app.store",
    "mwx_app.runner",
    "mwx_app.theme",
    "mwx_app.updater",
    "mwx_app.ui",
    "mwx_app.main",
    "core",
    "core.config",
    "core.database",
    "core.models",
    "core.app_auth",
    "core.registro",
    "core.perfiles",
    "core.roles",
    "core.registros",
    "core.secciones",
    "core.tiers",
    "core.pausas",
    "core.verificacion",
    "ia",
    "ia.prompts",
    "ia.generador_contenido",
    "activaciones",
    "activaciones.local",
    "activaciones.variaciones",
    "plataformas",
    "plataformas.chrome_driver",
    "plataformas.twitter",
    "plataformas.twitter.selenium_bot",
    "utils",
    "utils.proxies",
    "utils.forward_proxy",
    "utils.anti_detection",
    "undetected_chromedriver",
    "websockets",
    "pydantic_core",
    "sqlalchemy.dialects.sqlite",
    "pyotp",
]

OCULTOS += collect_submodules("selenium.webdriver.chrome")
OCULTOS += collect_submodules("selenium.webdriver.common")
OCULTOS += collect_submodules("undetected_chromedriver")

a = Analysis(
    [os.path.join(RAIZ, "mwx_app", "main.py")],
    pathex=[RAIZ],
    binaries=[],
    datas=[
        (ICONO, "assets"),
        (os.path.join(RAIZ, "assets", "mwx_logo.png"), "assets"),
    ],
    hiddenimports=OCULTOS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUIR,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MWX APP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICONO,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MWX APP",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MWX APP.app",
        icon=ICONO,
        bundle_identifier="com.mwgroupp.mwxapp",
        info_plist={
            "CFBundleName": "MWX APP",
            "CFBundleDisplayName": "MWX APP",
            "CFBundleShortVersionString": "1.1.4",
            "CFBundleVersion": "1.1.4",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
