"""
aws_manager.py — Infraestructura como Código sobre AWS EC2.

Encapsula todas las llamadas al SDK de AWS (boto3).  El resto del sistema
nunca importa boto3 directamente; solo usa esta clase.
"""

import os
import json
import time
import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ── Variables de entorno ──────────────────────────────────────────────────────
AWS_REGION        = os.getenv("AWS_REGION",        "us-east-1")
INSTANCE_TYPE     = os.getenv("INSTANCE_TYPE",     "t2.micro")
KEY_NAME          = os.getenv("KEY_NAME",           "vockey")
SECURITY_GROUP_ID = os.getenv("SECURITY_GROUP_ID", "")          # sg-xxxxxxxx
SUBNET_ID         = os.getenv("SUBNET_ID",         "")          # subnet-xxxxxxxx
IAM_INSTANCE_PROFILE = os.getenv("IAM_INSTANCE_PROFILE", "")   # opcional

# AMI_ID se lee de config_ami.txt (generado por create_ami.sh) o del entorno
_CONFIG_AMI_FILE  = Path(__file__).parent / "config_ami.txt"

MONITOR_S_HOST    = os.getenv("MONITOR_S_HOST", "")   # IP privada del MonitorS
MONITOR_S_PORT    = os.getenv("MONITOR_S_PORT", "50052")
APP_PORT          = os.getenv("APP_PORT",        "8080")


def _read_ami_id() -> str:
    """Lee el AMI_ID desde variable de entorno o desde config_ami.txt."""
    ami_env = os.getenv("AMI_ID", "")
    if ami_env:
        return ami_env
    if _CONFIG_AMI_FILE.exists():
        ami = _CONFIG_AMI_FILE.read_text().strip()
        if ami:
            return ami
    raise RuntimeError(
        "AMI_ID no definido. Ejecuta create_ami.sh o define la variable AMI_ID en .env"
    )


def _build_user_data(instance_tag: str) -> str:
    """
    Script de user-data que se inyecta en cada nueva instancia.
    Sobreescribe las variables que dependen de la IP real de EC2.
    """
    return f"""#!/bin/bash
# Generado automáticamente por ControllerASG
export MONITOR_S_HOST={MONITOR_S_HOST}
export MONITOR_S_PORT={MONITOR_S_PORT}
export APP_PORT={APP_PORT}

# Leer metadatos IMDSv2
TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \\
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  http://169.254.169.254/latest/meta-data/instance-id)
INSTANCE_IP=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \\
  http://169.254.169.254/latest/meta-data/local-ipv4)

export INSTANCE_ID
export INSTANCE_IP

# Actualizar .env de MonitorC y AppInstance con valores reales
sed -i "s/^INSTANCE_ID=.*/INSTANCE_ID=$INSTANCE_ID/" /opt/autoscale/monitor-c/.env
sed -i "s/^INSTANCE_ID=.*/INSTANCE_ID=$INSTANCE_ID/" /opt/autoscale/app-instance/.env
sed -i "s/^INSTANCE_IP=.*/INSTANCE_IP=$INSTANCE_IP/" /opt/autoscale/monitor-c/.env
sed -i "s/^MONITOR_S_HOST=.*/MONITOR_S_HOST={MONITOR_S_HOST}/" /opt/autoscale/monitor-c/.env

# Reiniciar servicios
systemctl restart monitor-c
systemctl restart app-instance
"""


class AWSManager:
    """Wrapper sobre boto3 para operaciones EC2 del ControllerASG."""

    def __init__(self):
        self._ec2 = boto3.client("ec2", region_name=AWS_REGION)
        self._ami_id: str | None = None
        log.info("AWSManager inicializado | region=%s", AWS_REGION)

    # ── AMI ──────────────────────────────────────────────────────────────────

    def get_ami_id(self) -> str:
        if not self._ami_id:
            self._ami_id = _read_ami_id()
            log.info("AMI_ID cargado: %s", self._ami_id)
        return self._ami_id

    def create_ami(self, source_instance_id: str, name: str = "autoscale-base") -> str:
        """
        Crea una AMI a partir de una instancia EC2 existente.
        Equivale a lo que hace create_ami.sh, pero desde Python.
        """
        log.info("Creando AMI desde instancia %s…", source_instance_id)
        tag = f"{name}-{int(time.time())}"
        resp = self._ec2.create_image(
            InstanceId=source_instance_id,
            Name=tag,
            NoReboot=True,
            TagSpecifications=[{
                "ResourceType": "image",
                "Tags": [
                    {"Key": "Name",    "Value": tag},
                    {"Key": "Project", "Value": "AutoScale"},
                ],
            }],
        )
        ami_id = resp["ImageId"]
        # Esperar a que la AMI esté disponible
        log.info("Esperando a que AMI %s esté disponible…", ami_id)
        waiter = self._ec2.get_waiter("image_available")
        waiter.wait(ImageIds=[ami_id])
        # Persistir el AMI_ID
        _CONFIG_AMI_FILE.write_text(ami_id)
        self._ami_id = ami_id
        log.info("AMI creada y guardada: %s", ami_id)
        return ami_id

    # ── Instancias EC2 ────────────────────────────────────────────────────────

    def launch_instance(self) -> str | None:
        """
        Lanza una nueva instancia EC2 a partir de la AMI personalizada.
        Retorna el instance_id o None si falló.
        """
        try:
            ami_id = self.get_ami_id()
            ts     = int(time.time())
            tag    = f"autoscale-app-{ts}"

            launch_params: dict = {
                "ImageId":      ami_id,
                "InstanceType": INSTANCE_TYPE,
                "MinCount": 1,
                "MaxCount": 1,
                "KeyName":  KEY_NAME,
                "UserData": _build_user_data(tag),
                "TagSpecifications": [{
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name",    "Value": tag},
                        {"Key": "Project", "Value": "AutoScale"},
                        {"Key": "Role",    "Value": "app-instance"},
                    ],
                }],
                "MetadataOptions": {
                    "HttpTokens":              "required",   # IMDSv2
                    "HttpPutResponseHopLimit": 1,
                    "HttpEndpoint":            "enabled",
                },
            }

            # Parámetros opcionales según entorno
            if SECURITY_GROUP_ID:
                launch_params["SecurityGroupIds"] = [SECURITY_GROUP_ID]
            if SUBNET_ID:
                launch_params["SubnetId"] = SUBNET_ID
            if IAM_INSTANCE_PROFILE:
                launch_params["IamInstanceProfile"] = {"Name": IAM_INSTANCE_PROFILE}

            resp        = self._ec2.run_instances(**launch_params)
            instance_id = resp["Instances"][0]["InstanceId"]

            log.info("Instancia lanzada: %s | tag=%s", instance_id, tag)
            return instance_id

        except ClientError as exc:
            log.error("Error al lanzar instancia: %s", exc)
            return None

    def terminate_instance(self, instance_id: str) -> bool:
        """
        Termina una instancia EC2.
        Retorna True si la llamada tuvo éxito.
        """
        try:
            self._ec2.terminate_instances(InstanceIds=[instance_id])
            log.info("Instancia terminada: %s", instance_id)
            return True
        except ClientError as exc:
            log.error("Error al terminar instancia %s: %s", instance_id, exc)
            return False

    def describe_instances(self, filters: list | None = None) -> list[dict]:
        """
        Devuelve una lista de instancias EC2 del proyecto con estado running.
        Útil para reconciliar el estado real de AWS con el registro interno.
        """
        default_filters = [
            {"Name": "tag:Project", "Values": ["AutoScale"]},
            {"Name": "tag:Role",    "Values": ["app-instance"]},
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]
        try:
            resp      = self._ec2.describe_instances(Filters=filters or default_filters)
            instances = []
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    instances.append({
                        "instance_id":      inst["InstanceId"],
                        "state":            inst["State"]["Name"],
                        "private_ip":       inst.get("PrivateIpAddress", ""),
                        "launch_time":      str(inst.get("LaunchTime", "")),
                        "instance_type":    inst.get("InstanceType", ""),
                    })
            return instances
        except ClientError as exc:
            log.error("Error al describir instancias: %s", exc)
            return []

    def get_instance_status(self, instance_id: str) -> str:
        """Retorna el estado EC2 de una instancia concreta (running, stopped, etc.)."""
        try:
            resp = self._ec2.describe_instances(InstanceIds=[instance_id])
            inst = resp["Reservations"][0]["Instances"][0]
            return inst["State"]["Name"]
        except (ClientError, IndexError, KeyError):
            return "unknown"
