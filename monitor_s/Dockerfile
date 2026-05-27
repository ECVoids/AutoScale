# monitor-s/Dockerfile
FROM python:3.11-slim

LABEL maintainer="ECVoids"
LABEL description="MonitorS — servicio centralizado de monitoreo y registro"

WORKDIR /app

# Dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código del MonitorS
COPY monitor_s.py .
COPY instance_registry.py .
COPY grpc_client.py .

# Proto compilados (generados en shared-protos y copiados aquí en build)
# En docker-compose se monta shared-protos como volumen para desarrollo.
# En producción correr: python -m grpc_tools.protoc ... antes de hacer build.
COPY ../shared-protos/monitor_pb2.py      .
COPY ../shared-protos/monitor_pb2_grpc.py .

ENV MONITOR_S_ID=monitor-s-1 \
    MONITOR_S_PORT=50052      \
    POLL_INTERVAL=10          \
    HEARTBEAT_TIMEOUT=5       \
    MAX_INSTANCES=5

EXPOSE 50052

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "\
import grpc, monitor_pb2, monitor_pb2_grpc; \
ch = grpc.insecure_channel('localhost:50052'); \
grpc.channel_ready_future(ch).result(timeout=4)"

CMD ["python", "monitor_s.py"]
