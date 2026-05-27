#!/usr/bin/env bash
# =============================================================================
# startup.sh
# Se ejecuta en cada instancia EC2 al arrancar, inyectado como user-data.
# Responsabilidades:
#   1. Instalar dependencias del sistema.
#   2. Clonar / actualizar el repositorio.
#   3. Detectar el Instance ID y la IP privada desde el metadata service de EC2.
#   4. Generar el .env de MonitorC y AppInstance con los valores reales de la instancia.
#   5. Levantar MonitorC y AppInstance.
# =============================================================================
set -euo pipefail

# ── Configuración — ajustar antes de crear la AMI ────────────────────────────
REPO_URL="https://github.com/ECVoids/AutoScale.git"
REPO_DIR="/opt/autoscale"
BRANCH="main"

MONITOR_S_HOST="<IP_PRIVADA_MONITOR_S>"   # reemplazar con IP/DNS del MonitorS
MONITOR_S_PORT="50052"
MONITOR_C_PORT="50051"
APP_PORT="8080"
AGENT_VERSION="1.0.0"
METRICS_INTERVAL="5"

LOG_FILE="/var/log/autoscale-startup.log"

# ── Logging ───────────────────────────────────────────────────────────────────
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=============================="
echo "startup.sh — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="

# ── 1. Dependencias del sistema ───────────────────────────────────────────────
echo "[1/5] Instalando dependencias del sistema..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# ── 2. Clonar o actualizar el repositorio ────────────────────────────────────
echo "[2/5] Sincronizando repositorio..."
if [ -d "$REPO_DIR/.git" ]; then
    git -C "$REPO_DIR" pull origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
fi

# ── 3. Obtener metadatos de EC2 ───────────────────────────────────────────────
echo "[3/5] Leyendo metadatos de la instancia EC2..."
# IMDSv2 — más seguro que v1
TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

INSTANCE_ID=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)

INSTANCE_IP=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" \
    http://169.254.169.254/latest/meta-data/local-ipv4)

echo "  instance_id : $INSTANCE_ID"
echo "  instance_ip : $INSTANCE_IP"

# ── 4. Generar .env de MonitorC y AppInstance ─────────────────────────────────
echo "[4/5] Generando archivos .env..."

cat > "$REPO_DIR/monitor-c/.env" <<EOF
INSTANCE_ID=$INSTANCE_ID
INSTANCE_IP=$INSTANCE_IP
MONITOR_C_PORT=$MONITOR_C_PORT
MONITOR_S_HOST=$MONITOR_S_HOST
MONITOR_S_PORT=$MONITOR_S_PORT
AGENT_VERSION=$AGENT_VERSION
METRICS_INTERVAL=$METRICS_INTERVAL
EOF

cat > "$REPO_DIR/app-instance/.env" <<EOF
INSTANCE_ID=$INSTANCE_ID
APP_PORT=$APP_PORT
LOAD_TICK_SECONDS=2.0
LOAD_MEAN=50.0
LOAD_REVERSION=0.05
LOAD_NOISE_STD=3.0
LOAD_SPIKE_PROB=0.03
LOAD_SPIKE_MIN=20.0
LOAD_SPIKE_MAX=40.0
LOAD_SPIKE_DECAY=0.8
EOF

echo "  .env generados correctamente."

# ── 5. Instalar dependencias Python y levantar procesos ───────────────────────
echo "[5/5] Levantando AppInstance y MonitorC..."

# Entorno virtual compartido
python3 -m venv "$REPO_DIR/.venv"
source "$REPO_DIR/.venv/bin/activate"

# Compilar protos (por si la AMI no los tiene pre-compilados)
pip install -q grpcio grpcio-tools python-dotenv
python -m grpc_tools.protoc \
    -I "$REPO_DIR/shared-protos" \
    --python_out="$REPO_DIR/shared-protos" \
    --grpc_python_out="$REPO_DIR/shared-protos" \
    "$REPO_DIR/shared-protos/monitor.proto"

pip install -q -r "$REPO_DIR/monitor-c/requirements.txt"
pip install -q -r "$REPO_DIR/app-instance/requirements.txt"

# Exportar PYTHONPATH para que los módulos encuentren los pb2
export PYTHONPATH="$REPO_DIR/shared-protos:$REPO_DIR/monitor-c:$REPO_DIR/app-instance"

# AppInstance en background
nohup python "$REPO_DIR/app-instance/app.py" \
    >> /var/log/app-instance.log 2>&1 &
echo "  AppInstance PID: $!"

# Pequeña espera para que AppInstance levante antes que MonitorC intente leerla
sleep 2

# MonitorC en background
nohup python "$REPO_DIR/monitor-c/monitor_c_server.py" \
    >> /var/log/monitor-c.log 2>&1 &
echo "  MonitorC PID: $!"

echo ""
echo "startup.sh completado — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
