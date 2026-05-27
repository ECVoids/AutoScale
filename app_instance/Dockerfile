# app-instance/Dockerfile
FROM python:3.11-slim

LABEL maintainer="ECVoids"
LABEL description="AppInstance - carga simulada con endpoint HTTP"

WORKDIR /app

# Sin dependencias externas, copiamos directamente el código
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY load_generator.py .

# Variables de entorno por defecto (sobreescribibles en docker-compose o EC2 user-data)
ENV APP_PORT=8080 \
    INSTANCE_ID=local-dev \
    LOAD_TICK_SECONDS=2.0 \
    LOAD_MEAN=50.0 \
    LOAD_REVERSION=0.05 \
    LOAD_NOISE_STD=3.0 \
    LOAD_SPIKE_PROB=0.03 \
    LOAD_SPIKE_MIN=20.0 \
    LOAD_SPIKE_MAX=40.0 \
    LOAD_SPIKE_DECAY=0.8

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${APP_PORT}/health')"

CMD ["python", "app.py"]
