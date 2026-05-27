FROM python:3.12-slim

LABEL project="AutoScale" \
      component="controller-asg" \
      description="ControllerASG — servicio de autoescalamiento sobre EC2"

WORKDIR /app

# Dependencias del sistema mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código fuente del controller
COPY . .

# Protos compartidos (montados en docker-compose desde shared-protos/)
# Se espera que estén en /app/protos o en el PYTHONPATH
ENV PYTHONPATH="/app:/app/protos"

# El controller no expone ningún puerto: se comunica vía shared memory
# con MonitorS (mismo proceso/contenedor) y via AWS SDK hacia afuera.

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import controller; print('ok')" || exit 1

CMD ["python", "-u", "controller.py"]
