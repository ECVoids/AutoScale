# AutoScale

Sistema distribuido de autoescalamiento sobre AWS EC2 basado en monitoreo activo, métricas simuladas y políticas dinámicas de escalamiento.

Proyecto desarrollado para la materia **ST0263 - Tópicos Especiales en Telemática**.

Repositorio oficial:
https://github.com/ECVoids/AutoScale

---

# Objetivo del Proyecto

El propósito de este proyecto es diseñar e implementar un sistema de autoescalamiento similar a un Auto Scaling Group (ASG), utilizando instancias EC2 de AWS y mecanismos distribuidos desarrollados en Python.

El sistema monitorea continuamente múltiples instancias de aplicación mediante agentes distribuidos y toma decisiones automáticas de creación o destrucción de instancias dependiendo de métricas de carga.

---

# Contexto Académico

El proyecto está basado en los lineamientos definidos por la Universidad EAFIT para el Proyecto 2 de ST0263.

Los principales requerimientos incluyen:

- Monitoreo distribuido mediante gRPC.
- Simulación de métricas de carga.
- Heartbeat y detección de fallos.
- Autoescalamiento de instancias EC2.
- Uso de AMIs personalizadas.
- Infraestructura como código mediante AWS SDK.

---

# Arquitectura General

El sistema está dividido en tres componentes principales:

## 1. MonitorC

Proceso ejecutado dentro de cada instancia EC2 de aplicación.

Responsabilidades:
- Exponer servicios gRPC.
- Reportar métricas.
- Responder heartbeats.
- Registrar/desregistrar instancias.
- Simular carga.

---

## 2. MonitorS

Servicio centralizado de monitoreo.

Responsabilidades:
- Consultar periódicamente métricas.
- Detectar fallos.
- Mantener inventario de instancias.
- Compartir estado global con el ControllerASG.

---

## 3. ControllerASG

Servicio responsable del autoescalamiento.

Responsabilidades:
- Evaluar políticas de escalamiento.
- Crear instancias EC2.
- Eliminar instancias EC2.
- Gestionar límites mínimos y máximos.
- Administrar AMIs personalizadas.

---

# Arquitectura Objetivo del Proyecto

```text
project-root/
│
├── monitor-c/
│   ├── monitor_c_server.py
│   ├── metrics_simulator.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env
│
├── monitor-s/
│   ├── monitor_s.py
│   ├── instance_registry.py
│   ├── grpc_client.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env
│
├── controller-asg/
│   ├── controller.py
│   ├── aws_manager.py
│   ├── scaling_policy.py
│   ├── config.json
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env
│
├── app-instance/
│   ├── app.py
│   ├── load_generator.py
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env
│
├── shared-protos/
│   ├── monitor.proto
│   ├── monitor_pb2.py
│   └── monitor_pb2_grpc.py
│
├── scripts/
│   ├── create_ami.sh
│   ├── deploy.sh
│   ├── startup.sh
│   └── cleanup.sh
│
├── docs/
│   ├── arquitectura.png
│   ├── secuencia.png
│   ├── politicas-escalamiento.md
│   └── README.md
│
├── docker-compose.yml
├── .gitignore
└── requirements-global.txt