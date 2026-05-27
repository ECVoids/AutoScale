#!/usr/bin/env bash
# =============================================================================
# cleanup.sh
# Limpia todos los recursos EC2 creados por el ControllerASG.
# Úsalo al finalizar pruebas para evitar cargos en AWS Academy.
#
# Qué hace:
#   1. Lista todas las instancias EC2 con tag Project=AutoScale.
#   2. Excluye la instancia del MonitorS (tag Role=monitor-s) para no tirarla.
#   3. Envía Deregister a cada MonitorC antes de terminar su instancia.
#   4. Termina las instancias de AppInstance.
#   5. (Opcional) Elimina las AMIs creadas por create_ami.sh.
#   6. (Opcional) Termina también la instancia del MonitorS.
#
# Flags:
#   --all          Termina TODO, incluyendo el MonitorS.
#   --amis         También deregistra las AMIs del proyecto.
#   --dry-run      Muestra qué haría sin ejecutar nada.
# =============================================================================
set -euo pipefail

# ── Configuración ─────────────────────────────────────────────────────────────
PROJECT_TAG="AutoScale"
MONITOR_C_PORT="50051"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/vockey.pem}"
SSH_USER="ubuntu"

# ── Flags ─────────────────────────────────────────────────────────────────────
DELETE_ALL=false
DELETE_AMIS=false
DRY_RUN=false

for arg in "$@"; do
    case $arg in
        --all)     DELETE_ALL=true ;;
        --amis)    DELETE_AMIS=true ;;
        --dry-run) DRY_RUN=true ;;
    esac
done

# ── Helper ────────────────────────────────────────────────────────────────────
run() {
    if $DRY_RUN; then
        echo "  [DRY-RUN] $*"
    else
        eval "$@"
    fi
}

echo "=============================="
echo "cleanup.sh — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
$DRY_RUN && echo "  MODO DRY-RUN — no se ejecutará nada"
echo "=============================="

# ── 1. Listar instancias AppInstance (excluir MonitorS a menos que --all) ─────
echo ""
echo "[1/4] Buscando instancias AppInstance del proyecto..."

FILTER_BASE="Name=tag:Project,Values=$PROJECT_TAG Name=instance-state-name,Values=running,stopped"

if $DELETE_ALL; then
    INSTANCE_IDS=$(aws ec2 describe-instances \
        --filters $FILTER_BASE \
        --query "Reservations[].Instances[].InstanceId" \
        --output text)
else
    # Excluir la instancia con tag Role=monitor-s
    INSTANCE_IDS=$(aws ec2 describe-instances \
        --filters $FILTER_BASE \
        --query "Reservations[].Instances[?!contains(Tags[?Key=='Role'].Value, 'monitor-s')].InstanceId" \
        --output text)
fi

if [ -z "$INSTANCE_IDS" ]; then
    echo "  No se encontraron instancias AppInstance con tag Project=$PROJECT_TAG."
else
    echo "  Instancias encontradas: $INSTANCE_IDS"
fi

# ── 2. Deregister limpio en cada MonitorC ────────────────────────────────────
echo ""
echo "[2/4] Enviando Deregister a cada MonitorC..."

for INSTANCE_ID in $INSTANCE_IDS; do
    PRIVATE_IP=$(aws ec2 describe-instances \
        --instance-ids "$INSTANCE_ID" \
        --query "Reservations[0].Instances[0].PrivateIpAddress" \
        --output text)

    if [ -z "$PRIVATE_IP" ] || [ "$PRIVATE_IP" = "None" ]; then
        echo "  [$INSTANCE_ID] Sin IP privada, saltando deregister."
        continue
    fi

    echo "  [$INSTANCE_ID] Deregister → $PRIVATE_IP:$MONITOR_C_PORT"
    run "python3 -c \"
import grpc, sys
sys.path.insert(0, 'shared-protos')
try:
    import monitor_pb2, monitor_pb2_grpc
    with grpc.insecure_channel('$PRIVATE_IP:$MONITOR_C_PORT') as ch:
        stub = monitor_pb2_grpc.MonitorCServiceStub(ch)
        r = stub.Deregister(
            monitor_pb2.DeregisterRequest(instance_id='$INSTANCE_ID', reason='cleanup'),
            timeout=3
        )
        print('  Deregister OK:', r.message)
except Exception as e:
    print('  Deregister falló (ignorando):', e)
\" 2>/dev/null || true"
done

# ── 3. Terminar instancias EC2 ────────────────────────────────────────────────
echo ""
echo "[3/4] Terminando instancias EC2..."

if [ -z "$INSTANCE_IDS" ]; then
    echo "  Nada que terminar."
else
    run "aws ec2 terminate-instances --instance-ids $INSTANCE_IDS --output table"
    echo "  Instancias enviadas a terminate: $INSTANCE_IDS"

    if ! $DRY_RUN; then
        echo "  Esperando confirmación de terminación..."
        aws ec2 wait instance-terminated --instance-ids $INSTANCE_IDS
        echo "  Todas las instancias terminadas."
    fi
fi

# ── 4. (Opcional) Eliminar AMIs del proyecto ──────────────────────────────────
echo ""
echo "[4/4] AMIs del proyecto..."

if $DELETE_AMIS; then
    AMI_IDS=$(aws ec2 describe-images \
        --owners self \
        --filters "Name=tag:Project,Values=$PROJECT_TAG" \
        --query "Images[].ImageId" \
        --output text)

    if [ -z "$AMI_IDS" ]; then
        echo "  No se encontraron AMIs con tag Project=$PROJECT_TAG."
    else
        for AMI_ID in $AMI_IDS; do
            echo "  Deregistrando AMI: $AMI_ID"
            run "aws ec2 deregister-image --image-id $AMI_ID"

            # Eliminar también los snapshots asociados
            SNAPSHOT_IDS=$(aws ec2 describe-images \
                --image-ids "$AMI_ID" \
                --query "Images[0].BlockDeviceMappings[].Ebs.SnapshotId" \
                --output text 2>/dev/null || true)

            for SNAP_ID in $SNAPSHOT_IDS; do
                echo "  Eliminando snapshot: $SNAP_ID"
                run "aws ec2 delete-snapshot --snapshot-id $SNAP_ID"
            done
        done
    fi
else
    echo "  (Omitido — usa --amis para eliminar AMIs)"
fi

echo ""
echo "cleanup.sh completado — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "=============================="
if $DELETE_ALL; then
    echo "  ADVERTENCIA: Se incluyó la instancia MonitorS."
fi
$DRY_RUN && echo "  DRY-RUN: ningún recurso fue modificado."
echo "=============================="
