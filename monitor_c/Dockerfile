# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── Metadatos ─────────────────────────────────────────────────────────────────
LABEL maintainer="ST0263-equipo"
LABEL description="Agente MonitorC — servidor gRPC que corre en cada AppInstance"

# ── Variables de entorno por defecto (sobreescribibles en docker-compose / EC2)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MONITOR_C_PORT=50051

WORKDIR /app

# ── Dependencias Python ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Código fuente del agente ──────────────────────────────────────────────────
COPY monitor_c_server.py  .
COPY metrics_simulator.py .

# ── Protos compilados (generados desde shared-protos/) ────────────────────────
# Se copian desde el contexto de build (raíz del proyecto).
# En docker-compose el contexto es project-root/.
COPY shared-protos/monitor_pb2.py      .
COPY shared-protos/monitor_pb2_grpc.py .

# ── Puerto expuesto ───────────────────────────────────────────────────────────
EXPOSE ${MONITOR_C_PORT}

# ── Arranque ──────────────────────────────────────────────────────────────────
CMD ["python", "monitor_c_server.py"]
