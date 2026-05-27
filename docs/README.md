# Documentación técnica — AutoScale

Esta carpeta contiene la documentación técnica del sistema AutoScale, desarrollado para el Proyecto 2 de ST0263 - Tópicos Especiales en Telemática (EAFIT, 2026-1).

Repositorio: [ECVoids/AutoScale](https://github.com/ECVoids/AutoScale)

---

## Contenido

| Archivo | Descripción |
|---|---|
| `arquitectura.png` | Diagrama estructural: componentes, instancias EC2 y flujos de comunicación |
| `secuencia.png` | Diagrama de secuencia: arranque, polling, evaluación de política y scale-up |
| `politicas-escalamiento.md` | Referencia completa de las políticas de creación y destrucción de instancias |
| `README.md` | Este índice |

---

## Visión general del sistema

AutoScale implementa un Auto Scaling Group (ASG) personalizado sobre EC2. El sistema está compuesto por tres servicios principales:

**MonitorC** corre dentro de cada instancia de aplicación. Expone una API gRPC para reportar métricas, responder heartbeats y gestionar su ciclo de registro/desregistro con el MonitorS.

**MonitorS** es el servicio centralizado de observabilidad. Sondea todas las instancias registradas en paralelo cada `POLL_INTERVAL` segundos: primero un Ping para verificar vivacidad, luego GetMetrics para obtener la carga CPU simulada. Mantiene un registro thread-safe con el estado de cada instancia.

**ControllerASG** corre en la misma instancia que el MonitorS y accede al registro por memoria compartida (sin llamadas de red). Evalúa las políticas de escalamiento cada `EVAL_INTERVAL` segundos y usa boto3 para crear o terminar instancias EC2 según la carga del cluster.

---

## Flujo de comunicación

```
MonitorC  ──gRPC──▶  MonitorS  ──shared memory──▶  ControllerASG  ──boto3──▶  AWS EC2 API
   ▲                    │                                                           │
   │                    │ Ping + GetMetrics (polling activo)                        │
   └────────────────────┘                                                           │
                                                                    run_instances / terminate
```

Las comunicaciones externas del sistema son dos:
- **gRPC** entre MonitorS y cada MonitorC (polling activo + registro/desregistro).
- **AWS SDK (boto3)** entre ControllerASG y la EC2 API (infraestructura como código).

El acceso al estado global entre MonitorS y ControllerASG es por **importación directa del singleton `registry`** en el mismo proceso Python, sin ningún protocolo de red.

---

## Estructura de directorios

```
project-root/
├── monitor-c/          # Agente dentro de cada AppInstance
├── monitor-s/          # Servicio centralizado de monitoreo
├── controller-asg/     # Servicio de autoescalamiento
├── app-instance/       # Servidor HTTP + simulador de carga
├── shared-protos/      # Contratos gRPC (monitor.proto)
├── scripts/            # deploy.sh · startup.sh · create_ami.sh · cleanup.sh
└── docs/               # Esta carpeta
```

---

## Políticas de escalamiento

Ver [politicas-escalamiento.md](./politicas-escalamiento.md) para la referencia completa.

En resumen:

- **Scale-up** se dispara cuando `avg_cpu ≥ 70%` durante 2 evaluaciones consecutivas, o cuando `total < MIN_INSTANCES`, o cuando ≥50% de instancias son `UNREACHABLE`.
- **Scale-down** se dispara cuando `avg_cpu ≤ 30%` durante 3 evaluaciones consecutivas y `total > MIN_INSTANCES`.
- Ambas acciones respetan períodos de cooldown (120s y 180s respectivamente) para evitar oscilaciones.

---

## Requisitos de infraestructura AWS Academy

| Recurso | Valor |
|---|---|
| Tipo de instancia | `t2.micro` |
| Almacenamiento | EBS (default) |
| Par de claves | `vockey.pem` |
| Mínimo de instancias | 2 |
| Máximo de instancias | 5 (límite AWS Academy) |
| AMI base | Personalizada vía `create_ami.sh` |
| Metadata | IMDSv2 obligatorio |

---

## Scripts de operación

| Script | Uso |
|---|---|
| `scripts/deploy.sh <instance-id>` | Despliega MonitorS en su EC2 como servicio systemd |
| `scripts/startup.sh` | User-data de la instancia base: configura y arranca MonitorC + AppInstance |
| `scripts/create_ami.sh <instance-id>` | Crea la AMI personalizada y guarda el ID en `controller-asg/config_ami.txt` |
| `scripts/cleanup.sh [--all] [--amis] [--dry-run]` | Termina instancias AppInstance (y opcionalmente MonitorS y AMIs) |

---

## Protocolo gRPC — resumen

Definido en `shared-protos/monitor.proto`. Los métodos relevantes son:

| Método | Dirección | Descripción |
|---|---|---|
| `Register` | MonitorC → MonitorS | Alta de instancia al arrancar |
| `Deregister` | MonitorC → MonitorS | Baja limpia al apagarse |
| `Ping` | MonitorS → MonitorC | Heartbeat; retorna `alive` y RTT |
| `GetMetrics` | MonitorS → MonitorC | CPU actual (0–100), uptime, versión |
| `GetStatus` | MonitorS → MonitorC | Estado resumido de la instancia |
| `ForceDeregister` | ControllerASG → MonitorC | Desregistro forzado antes de terminar EC2 |

---

## Simulación de carga

`app-instance/load_generator.py` implementa un modelo **Ornstein-Uhlenbeck discreto**: la señal de CPU deriva gradualmente hacia una media configurable (`LOAD_MEAN`, default 50%), con ruido gaussiano por tick y picos aleatorios ocasionales que decaen exponencialmente. Esto produce una señal realista y continua que permite probar las políticas de escalamiento sin comportamientos erráticos.
