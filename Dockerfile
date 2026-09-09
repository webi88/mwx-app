FROM python:3.11-slim

# Evita prompts interactivos de debconf/apt durante el build.
ENV DEBIAN_FRONTEND=noninteractive

# Railway NO tiene IPv6. Hosts como Supabase resuelven a IPv6 (AAAA) y eso
# provoca "Network is unreachable" al conectar por IPv6. Forzamos preferir IPv4.
RUN echo "precedence ::ffff:0:0/96  100" >> /etc/gai.conf

WORKDIR /app

# Dependencias del sistema: Chrome + Xvfb (pantalla virtual) + supervisord.
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg unzip ca-certificates \
    xvfb supervisor fonts-liberation \
    libnss3 libasound2 libgbm1 libx11-6 libx11-xcb1 libxcb1 \
    libxcomposite1 libxdamage1 libxrandr2 libxss1 libxtst6 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 libxshmfence1 \
    libxfixes3 libgtk-3-0 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome estable (para la automatización Selenium).
# 'apt-key' fue eliminado en Debian 13 (Trixie), por eso usamos un keyring
# firmado con signed-by= + gpg --dearmor en lugar de `apt-key add -`.
RUN wget -q -O /tmp/google-chrome.pub https://dl-ssl.google.com/linux/linux_signing_key.pub \
    && mkdir -p /usr/share/keyrings \
    && gpg --dearmor < /tmp/google-chrome.pub > /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la app.
COPY . .

# Datos "semilla" (proxies, usuarios web, config) que se copian al volumen
# persistente en el primer arranque (el volumen montado en /app/data oculta
# el contenido de la imagen, así que los guardamos aparte en /app/seed).
RUN mkdir -p /app/seed \
    && cp -r /app/data/proxies /app/seed/proxies \
    && cp /app/data/proxy_base.txt /app/seed/proxy_base.txt 2>/dev/null || true \
    && cp /app/data/web_users.json /app/seed/web_users.json 2>/dev/null || true \
    && cp -r /app/config /app/seed/config 2>/dev/null || true

# Script de arranque (siembra el volumen y lanza supervisord).
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Mexico_City \
    PORT=8501

EXPOSE 8501

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/supervisord.conf", "-n"]
