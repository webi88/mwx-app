# Servicio API del ejecutable MWX APP (independiente del bot/dashboard).
# Build desde la RAIZ del repo:
#   docker build -f api/Dockerfile -t mwx-api .
# Railway: crear un servicio nuevo con Dockerfile path `api/Dockerfile`
# (contexto = raiz del repo) y Target Port = $PORT (normalmente 8000).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements-api.txt /app/requirements-api.txt
RUN pip install --no-cache-dir -r /app/requirements-api.txt

# Solo lo que la API necesita (core es de SOLO LECTURA para esta API).
COPY core /app/core
COPY api /app/api

EXPOSE 8000

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
