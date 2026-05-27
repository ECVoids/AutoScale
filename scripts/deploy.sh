#!/usr/bin/env bash
# =============================================================================
# deploy.sh
# Despliega el MonitorS (y opcionalmente el ControllerASG) en la instancia
# EC2 designada para ello. Se ejecuta manualmente desde la máquina del
# desarrollador o desde un pipeline CI/CD.
#
# Flujo:
#   1. Conectarse a la instancia MonitorS vía SSH.
#   2. Clonar / actualizar el repositorio.
#   3. Compilar los protobuf.
#   4. Instalar dependencias.
#   5. Generar el .env de MonitorS.
#   6. Levantar MonitorS como servicio systemd (o proceso en background).
#
# Pre-requisitos:
#   - La instancia EC2 del MonitorS debe estar RUNNING.
#   - vockey.pem debe estar en la ruta indicada en KEY_PATH.
#   - AWS CLI configurado para obtener la IP si no se pasa como argumento.
# =============================================================================
set -euo pipefail

# ── Configuración — ajustar según tu entorno ──────────────────────────────────
MONITOR_S_INSTANCE_ID="${1:-}"                  # argumento opcional
KEY_PATH="${KEY_PATH:-$HOME/.ssh/vockey.pem}"
SSH_USER="ubuntu"                               # usuario por defecto en AMIs Ubuntu

REPO_URL="https://github.com/ECVoids/AutoScale.git"
REPO_DIR="/opt/autoscale"
BRANCH="main"

MONITOR_S_PORT="50052"
POLL_INTERVAL="10"
HEARTBEAT_TIMEOUT="5"
MAX_INSTANCES="5"
MONITOR_S_ID="monitor-s-1"

LOG_FILE="/var/log/autoscale-deploy.log"

# ── Obtener IP del MonitorS ───────────────────────────────────────────────────
if [ -z "$MONITOR_S_INSTANCE_ID" ]; then
    # Buscar por tag Name=monitor-s si no se pasó argumento
    MONITOR_S_INSTANCE_ID=$(aws ec2 describe-instances \
        --filters "Name=tag:Name,Values=monitor-s" "Name=instance-state-name,Values=running" \
        --query "Reservations[0].Instances[0].InstanceId" \
        --output text)
fi

if [ -z "$MONITOR_S_INSTANCE_ID" ] || [ "$MONITOR_S_INSTANCE_ID" = "None" ]; then
    echo "[ERROR] No se encontró instancia MonitorS. Pasa el Instance ID como argumento."
    echo "  Uso: ./deploy.sh i-0abc1234567890def"
    exit 1
fi

MONITOR_S_IP=$(aws ec2 describe-instances \
    --instance-ids "$MONITOR_S_INSTANCE_ID" \
    --query "Reservations[0].Instances[0].PublicIpAddress" \
    --output text)

echo "=============================="
echo "deploy.sh — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="
echo "  MonitorS instance : $MONITOR_S_INSTANCE_ID"
echo "  MonitorS IP       : $MONITOR_S_IP"
echo "  Key               : $KEY_PATH"

SSH="ssh -i $KEY_PATH -o StrictHostKeyChecking=no $SSH_USER@$MONITOR_S_IP"

# ── Función auxiliar para ejecutar comandos remotos ──────────────────────────
remote() {
    $SSH "bash -lc '$*'"
}

# ── 1. Clonar / actualizar repositorio ───────────────────────────────────────
echo ""
echo "[1/5] Sincronizando repositorio en MonitorS..."
remote "
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv git

    if [ -d '$REPO_DIR/.git' ]; then
        sudo git -C '$REPO_DIR' pull origin '$BRANCH'
    else
        sudo git clone --branch '$BRANCH' '$REPO_URL' '$REPO_DIR'
    fi
    sudo chown -R \$USER:\$USER '$REPO_DIR'
"

# ── 2. Compilar protobuf ──────────────────────────────────────────────────────
echo "[2/5] Compilando protobuf..."
remote "
    python3 -m venv '$REPO_DIR/.venv'
    source '$REPO_DIR/.venv/bin/activate'
    pip install -q grpcio grpcio-tools python-dotenv
    python -m grpc_tools.protoc \
        -I '$REPO_DIR/shared-protos' \
        --python_out='$REPO_DIR/shared-protos' \
        --grpc_python_out='$REPO_DIR/shared-protos' \
        '$REPO_DIR/shared-protos/monitor.proto'
"

# ── 3. Instalar dependencias de MonitorS ──────────────────────────────────────
echo "[3/5] Instalando dependencias Python..."
remote "
    source '$REPO_DIR/.venv/bin/activate'
    pip install -q -r '$REPO_DIR/monitor-s/requirements.txt'
"

# ── 4. Generar .env de MonitorS ───────────────────────────────────────────────
echo "[4/5] Generando .env de MonitorS..."
remote "
cat > '$REPO_DIR/monitor-s/.env' <<EOF
MONITOR_S_ID=$MONITOR_S_ID
MONITOR_S_PORT=$MONITOR_S_PORT
POLL_INTERVAL=$POLL_INTERVAL
HEARTBEAT_TIMEOUT=$HEARTBEAT_TIMEOUT
MAX_INSTANCES=$MAX_INSTANCES
EOF
echo '  .env generado.'
"

# ── 5. Levantar MonitorS ──────────────────────────────────────────────────────
echo "[5/5] Levantando MonitorS..."

# Crear unit de systemd para que sobreviva reinicios
$SSH "sudo bash -c \"cat > /etc/systemd/system/monitor-s.service <<'UNIT'
[Unit]
Description=AutoScale MonitorS
After=network.target

[Service]
User=$SSH_USER
WorkingDirectory=$REPO_DIR/monitor-s
Environment=PYTHONPATH=$REPO_DIR/shared-protos:$REPO_DIR/monitor-s
EnvironmentFile=$REPO_DIR/monitor-s/.env
ExecStart=$REPO_DIR/.venv/bin/python $REPO_DIR/monitor-s/monitor_s.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=multi-user.target
UNIT
\""

remote "
    sudo systemctl daemon-reload
    sudo systemctl enable monitor-s
    sudo systemctl restart monitor-s
    sleep 2
    sudo systemctl status monitor-s --no-pager
"

echo ""
echo "deploy.sh completado — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="
echo "  MonitorS corriendo en $MONITOR_S_IP:$MONITOR_S_PORT"
echo "  Logs : ssh -i $KEY_PATH $SSH_USER@$MONITOR_S_IP 'journalctl -u monitor-s -f'"
echo "=============================="
