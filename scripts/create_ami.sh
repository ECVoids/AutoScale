#!/usr/bin/env bash
# =============================================================================
# create_ami.sh
# Crea una AMI personalizada a partir de una instancia EC2 base ya configurada
# (con AppInstance y MonitorC instalados y probados).
# El ControllerASG usará el AMI_ID resultante para lanzar nuevas instancias.
#
# Pre-requisitos:
#   - AWS CLI instalado y configurado (aws configure o variables de entorno).
#   - La instancia base debe estar RUNNING y tener startup.sh ya ejecutado.
#   - El AMI_NAME debe ser único; si ya existe, el script lo detecta y sale.
# =============================================================================
set -euo pipefail

# ── Configuración ─────────────────────────────────────────────────────────────
SOURCE_INSTANCE_ID="${1:-}"                        # pasar como argumento o definir aquí
AMI_NAME="autoscale-app-instance-$(date +%Y%m%d)"
AMI_DESCRIPTION="AutoScale AppInstance + MonitorC — base image"
WAIT_TIMEOUT=600                                   # segundos máximos esperando available
OUTPUT_FILE="$(dirname "$0")/../controller-asg/config_ami.txt"

# ── Validaciones ──────────────────────────────────────────────────────────────
if [ -z "$SOURCE_INSTANCE_ID" ]; then
    echo "[ERROR] Debes pasar el Instance ID de la instancia base como argumento."
    echo "  Uso: ./create_ami.sh i-0abc1234567890def"
    exit 1
fi

echo "=============================="
echo "create_ami.sh — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="
echo "  Instancia fuente : $SOURCE_INSTANCE_ID"
echo "  Nombre AMI       : $AMI_NAME"

# Verificar que la instancia existe y está running
STATE=$(aws ec2 describe-instances \
    --instance-ids "$SOURCE_INSTANCE_ID" \
    --query "Reservations[0].Instances[0].State.Name" \
    --output text)

if [ "$STATE" != "running" ]; then
    echo "[ERROR] La instancia $SOURCE_INSTANCE_ID no está en estado 'running' (actual: $STATE)."
    exit 1
fi

# Verificar que el nombre AMI no esté ya tomado
EXISTING=$(aws ec2 describe-images \
    --owners self \
    --filters "Name=name,Values=$AMI_NAME" \
    --query "Images[0].ImageId" \
    --output text)

if [ "$EXISTING" != "None" ] && [ -n "$EXISTING" ]; then
    echo "[WARN] Ya existe una AMI con ese nombre: $EXISTING"
    echo "  Usa ese AMI_ID o cambia AMI_NAME en el script."
    echo "$EXISTING" > "$OUTPUT_FILE"
    echo "  AMI_ID guardado en $OUTPUT_FILE"
    exit 0
fi

# ── Crear AMI ─────────────────────────────────────────────────────────────────
echo ""
echo "[1/3] Creando AMI (esto puede tardar varios minutos)..."
AMI_ID=$(aws ec2 create-image \
    --instance-id "$SOURCE_INSTANCE_ID" \
    --name "$AMI_NAME" \
    --description "$AMI_DESCRIPTION" \
    --no-reboot \
    --query "ImageId" \
    --output text)

echo "  AMI_ID: $AMI_ID"

# ── Etiquetar la AMI ──────────────────────────────────────────────────────────
echo "[2/3] Etiquetando AMI..."
aws ec2 create-tags \
    --resources "$AMI_ID" \
    --tags \
        Key=Name,Value="$AMI_NAME" \
        Key=Project,Value=AutoScale \
        Key=CreatedBy,Value=create_ami_sh \
        Key=SourceInstance,Value="$SOURCE_INSTANCE_ID"

# ── Esperar a que esté disponible ─────────────────────────────────────────────
echo "[3/3] Esperando que la AMI esté disponible (timeout: ${WAIT_TIMEOUT}s)..."
aws ec2 wait image-available \
    --image-ids "$AMI_ID" \
    --cli-read-timeout "$WAIT_TIMEOUT"

echo ""
echo "AMI lista: $AMI_ID"

# Guardar el AMI_ID para que el ControllerASG lo lea de config
mkdir -p "$(dirname "$OUTPUT_FILE")"
echo "$AMI_ID" > "$OUTPUT_FILE"
echo "  AMI_ID guardado en $OUTPUT_FILE"

echo ""
echo "create_ami.sh completado — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="
echo "  Próximo paso: actualizar AMI_ID en controller-asg/config.json"
echo "  AMI_ID = $AMI_ID"
echo "=============================="
