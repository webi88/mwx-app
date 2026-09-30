#!/bin/sh
# VNC OPCIONAL (opt-in) para el MODO ASISTIDO de Change.org en Railway.
# Expone la pantalla virtual :99 (donde corre el Chrome visible) para que un
# humano resuelva el captcha de Change.org desde su navegador.
# Solo arranca si VNC_ACTIVO=1 y VNC_PASSWORD no esta vacio; en cualquier otro
# caso sale con 0 sin arrancar nada (supervisord, con autorestart=unexpected,
# no lo reinicia).
# Uso: vnc.sh x11vnc   (servidor VNC sobre :99)
#      vnc.sh novnc    (websockify + noVNC para verlo desde el navegador)

if [ "${VNC_ACTIVO:-0}" != "1" ]; then
    echo "[vnc] VNC_ACTIVO!=1: VNC no arrancado"
    exit 0
fi

if [ -z "${VNC_PASSWORD:-}" ]; then
    echo "[vnc] Aviso: VNC_ACTIVO=1 pero VNC_PASSWORD esta vacio; define VNC_PASSWORD y reinicia el servicio"
    exit 0
fi

case "$1" in
    x11vnc)
        exec x11vnc -display :99 -forever -shared -noxdamage -repeat -rfbport "${VNC_PORT:-5900}" -passwd "$VNC_PASSWORD" -no6
        ;;
    novnc)
        exec websockify --web=/usr/share/novnc "0.0.0.0:${NOVNC_PORT:-6080}" "localhost:${VNC_PORT:-5900}"
        ;;
    *)
        echo "[vnc] Modo desconocido: '$1' (usa x11vnc o novnc)"
        exit 1
        ;;
esac
