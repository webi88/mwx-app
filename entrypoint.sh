#!/bin/sh
# Entrypoint del contenedor (Railway).
# 1) Siembra /app/data (volumen persistente) con los datos iniciales la primera vez.
# 2) Lanza supervisord (bot + dashboard + scheduler + xvfb).
set -e

SEED_MARKER=/app/data/.seeded

if [ ! -f "$SEED_MARKER" ]; then
    echo "[entrypoint] Primera ejecución: sembrando datos iniciales en /app/data ..."
    mkdir -p /app/data/proxies /app/data/cookies /app/data/perfiles_chrome \
             /app/data/reportes /app/data/temp /app/data/logs /app/data/avatars /app/data/bin

    if [ -d /app/seed/proxies ]; then
        cp -r /app/seed/proxies/. /app/data/proxies/ 2>/dev/null || true
    fi
    [ -f /app/seed/proxy_base.txt ] && cp /app/seed/proxy_base.txt /app/data/proxy_base.txt || true
    [ -f /app/seed/web_users.json ] && cp /app/seed/web_users.json /app/data/web_users.json || true
    if [ -d /app/seed/config ]; then
        cp -r /app/seed/config/. /app/config/ 2>/dev/null || true
    fi

    touch "$SEED_MARKER"
    echo "[entrypoint] Siembra completada."
fi

echo "[entrypoint] Arrancando supervisord ..."
exec "$@"
