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

## AppInstance

La AppInstance es la unidad de trabajo escalable del sistema. Corre dentro de cada instancia EC2 y está compuesta por dos módulos que operan en paralelo: un servidor HTTP liviano y un simulador de carga.

### app.py

Servidor HTTP construido sobre la stdlib de Python (sin dependencias externas). Expone tres endpoints:

| Endpoint | Método | Descripción |
|---|---|---|
| `/health` | GET | Liveness check. Retorna `instance_id` y estado `ok`. Usado por el Dockerfile HEALTHCHECK. |
| `/status` | GET | Estado completo: `instance_id`, `load_percent` y `uptime_seconds`. |
| `/metrics` | GET | Solo la carga actual. Disponible para consumo directo si se requiere. |
| `/load/set` | POST | Fuerza un valor de carga manualmente. Útil para pruebas de las políticas de escalamiento sin esperar a la simulación. |

### load_generator.py

Simula la carga de la instancia de forma gradual y realista usando un modelo **Ornstein-Uhlenbeck discreto**: la señal deriva lentamente hacia una media configurable, con ruido gaussiano pequeño en cada tick y picos aleatorios ocasionales que decaen con el tiempo. Esto evita saltos bruscos de carga que distorsionarían las decisiones del ControllerASG.

Exporta un singleton `shared_load` que MonitorC puede importar directamente para leer la carga actual sin levantar un segundo hilo de simulación:

```python
from load_generator import shared_load
load = shared_load.current_load  # float thread-safe, 0.0 – 100.0
```

### Variables de entorno — AppInstance

| Variable | Default | Descripción |
|---|---|---|
| `INSTANCE_ID` | `local-dev` | ID único de la instancia. En EC2 se reemplaza por el Instance ID real vía `user-data`. |
| `APP_PORT` | `8080` | Puerto donde escucha el servidor HTTP. |
| `LOAD_TICK_SECONDS` | `2.0` | Intervalo entre actualizaciones de la simulación (segundos). |
| `LOAD_MEAN` | `50.0` | Media de carga hacia la que deriva el sistema (0–100). Ajustar para simular instancias más o menos cargadas. |
| `LOAD_REVERSION` | `0.05` | Fuerza de reversión a la media. Valores más altos hacen la señal más estable. |
| `LOAD_NOISE_STD` | `3.0` | Desviación estándar del ruido gaussiano por tick. |
| `LOAD_SPIKE_PROB` | `0.03` | Probabilidad de pico por tick (3% por defecto). |
| `LOAD_SPIKE_MIN` | `20.0` | Incremento mínimo de un pico (puntos porcentuales). |
| `LOAD_SPIKE_MAX` | `40.0` | Incremento máximo de un pico. |
| `LOAD_SPIKE_DECAY` | `0.8` | Factor de decaimiento del pico por tick. `0.8` significa que cada tick el pico vale el 80% del anterior. |

---

## MonitorS

El MonitorS es el servicio centralizado de observabilidad. Corre en su propia instancia EC2 junto al ControllerASG y tiene dos responsabilidades ejecutadas en paralelo: aceptar registros de los MonitorC vía gRPC, y sondear activamente todas las instancias registradas en un loop periódico.

### monitor_s.py

Punto de entrada del proceso. Levanta dos componentes en paralelo:

**Servidor gRPC (`MonitorSServicer`)** — Escucha en `MONITOR_S_PORT` y atiende las llamadas de registro que los MonitorC hacen al arrancar. Implementa `Register` y `Deregister` del proto. Rechaza registros si el número de instancias ya alcanzó `MAX_INSTANCES`. El resto de métodos del proto (`Ping`, `GetMetrics`, `GetStatus`) retornan `UNIMPLEMENTED`, ya que MonitorS no los necesita como servidor.

**Polling loop** — Cada `POLL_INTERVAL` segundos recorre todas las instancias del registro y las sondea en paralelo, cada una en su propio hilo. El flujo por instancia es: `Ping` → si responde → `GetMetrics` → actualizar registro. Si una instancia no responde al Ping, se registra el fallo. Tras `FAILURE_THRESHOLD` (3) fallos consecutivos la instancia es marcada `UNREACHABLE`. Al final de cada ronda se imprime un resumen con totales y CPU promedio del cluster.

### instance_registry.py

El registro es el **objeto de memoria compartida** entre MonitorS y ControllerASG. Ambos corren en el mismo proceso Python y el ControllerASG importa el singleton `registry` directamente, sin ninguna llamada de red.

```python
from monitor_s import registry
state = registry.snapshot_for_controller()
# Retorna: { total, healthy, unreachable, average_cpu, instances[], snapshot_time }
```

Cada entrada del registro (`InstanceEntry`) almacena: `instance_id`, `ip_address`, `port`, `version`, `registered_at`, `last_seen`, `last_metrics` (snapshot completo de métricas), `status` y `consecutive_failures`. Todo acceso es thread-safe mediante `RLock` reentrante.

La transición de estados de cada instancia sigue esta lógica:

```
cpu < 60%   →  HEALTHY
cpu < 85%   →  DEGRADED
cpu >= 85%  →  CRITICAL
3+ fallos   →  UNREACHABLE
```

### grpc_client.py

Encapsula todas las llamadas gRPC salientes del MonitorS hacia cada MonitorC. El polling loop nunca escribe gRPC directamente, solo usa este cliente. Métodos disponibles:

| Método | Retorna | Uso |
|---|---|---|
| `ping()` | `(alive: bool, rtt_ms: float)` | Heartbeat. El RTT queda disponible para diagnóstico. |
| `get_metrics()` | `MetricsSnapshot \| None` | Solicita métricas completas al MonitorC. |
| `get_status()` | `dict \| None` | Estado resumido: `state`, `app_version`, `uptime_secs`. |
| `force_deregister(reason)` | `bool` | Llamado por el ControllerASG antes de terminar una instancia EC2, para que MonitorC se limpie antes de desaparecer. |

### Variables de entorno — MonitorS

| Variable | Default | Descripción |
|---|---|---|
| `MONITOR_S_ID` | `monitor-s-1` | Identificador del MonitorS. Se incluye en las respuestas de registro para que los MonitorC sepan con quién están hablando. |
| `MONITOR_S_PORT` | `50052` | Puerto gRPC donde MonitorS escucha registros de los MonitorC. Debe coincidir con `MONITOR_S_PORT` en el `.env` de MonitorC. |
| `POLL_INTERVAL` | `10` | Segundos entre rondas completas de Ping + GetMetrics. Valores más bajos aumentan la sensibilidad a fallos pero incrementan el tráfico de red. |
| `HEARTBEAT_TIMEOUT` | `5` | Timeout por llamada gRPC hacia cada MonitorC (segundos). Si una instancia tarda más que esto en responder, cuenta como fallo. |
| `MAX_INSTANCES` | `5` | Máximo de instancias que MonitorS acepta registrar. Debe coincidir con `maxInstances` del ControllerASG. Con cuentas AWS Academy el límite práctico es 5. |

---

### Scripts

1. deploy.sh — se ejecuta una sola vez desde tu máquina local para desplegar el MonitorS en su instancia EC2 dedicada. Lo levanta como servicio systemd para que sobreviva reinicios. Recibe el Instance ID como argumento o lo busca automáticamente por tag Name=monitor-s.
2. startup.sh — se inyecta como user-data en la instancia EC2 base (la que luego se convierte en AMI). Usa IMDSv2 para leer el INSTANCE_ID e INSTANCE_IP reales de EC2, genera los .env de MonitorC y AppInstance con esos valores, compila los protos y levanta ambos procesos. El único valor que debes editar antes de crear la AMI es MONITOR_S_HOST con la IP privada del MonitorS.
3. create_ami.sh — toma el Instance ID de esa instancia base ya configurada, crea la AMI, la etiqueta con Project=AutoScale y guarda el AMI_ID resultante en controller-asg/config_ami.txt para que el ControllerASG lo lea al arrancar. Se usa con: ./create_ami.sh i-0abc123...
4. cleanup.sh — para el final de cada sesión de pruebas y no quemar créditos de AWS Academy. Flags importantes:

Sin flags → termina solo instancias AppInstance (respeta el MonitorS)
--all → termina todo incluyendo MonitorS
--amis → también elimina AMIs y sus snapshots
--dry-run → muestra qué haría sin ejecutar nada, ideal para verificar antes de destruir

---

### El directorio controller-asg 

está completo con 7 archivos. Aquí un resumen de lo que hace cada uno:
controller.py — Bucle principal. Cada EVAL_INTERVAL segundos lee el snapshot del registry (memoria compartida con MonitorS), consulta la política, y ejecuta scale-up o scale-down respetando cooldowns. También llama force_deregister en el MonitorC antes de terminar una instancia.
aws_manager.py — Toda la interacción con boto3. Maneja launch_instance(), terminate_instance(), describe_instances(), y create_ami(). Lee el AMI_ID desde config_ami.txt (generado por create_ami.sh) o desde el .env. El user-data que inyecta en cada instancia nueva configura el INSTANCE_ID real vía IMDSv2 y reinicia los servicios.
scaling_policy.py — Lógica de decisión desacoplada del resto. Implementa tres reglas:

Regla 0: si total < minInstances → scale-up inmediato
Regla 1: si ≥50% de instancias son UNREACHABLE → scale-up de reemplazo
Regla 2/3: CPU alta/baja sostenida durante N rondas consecutivas (anti-flapping)

También incluye pick_candidate_for_removal() que elige la instancia menos cargada para el scale-down.
config.json — Todos los parámetros de configuración documentados en un solo lugar.
.env.example — Plantilla con todas las variables de entorno; hay que copiarla como .env y completar SECURITY_GROUP_ID, SUBNET_ID, MONITOR_S_HOST con los valores reales de AWS Academy.

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
---

# Cómo correr el proyecto localmente

> Esta sección asume que **Docker y Docker Compose están instalados** en tu máquina. No requiere ninguna cuenta de AWS ni credenciales configuradas. Todo corre en contenedores locales.

---

## Requisitos previos

| Herramienta | Versión mínima | Verificar con |
|---|---|---|
| Docker | 24.x | `docker --version` |
| Docker Compose | 2.x (plugin) | `docker compose version` |
| Python | 3.11 | `python3 --version` (solo si quieres correr sin Docker) |
| Git | cualquiera | `git --version` |

---

## Opción A — Correr con Docker Compose (recomendado)

### 1. Clonar el repositorio

```bash
git clone https://github.com/ECVoids/AutoScale.git
cd AutoScale
```

### 2. Regenerar los archivos proto (solo si modificas el .proto)

```bash
pip install grpcio-tools==1.64.1
python -m grpc_tools.protoc \
  -I shared-protos \
  --python_out=shared-protos \
  --grpc_python_out=shared-protos \
  shared-protos/monitor.proto
```

> Los archivos `monitor_pb2.py` y `monitor_pb2_grpc.py` ya están generados en el repo. Solo necesitas este paso si editas `monitor.proto`.

### 3. Configurar variables de entorno

Cada componente tiene su propio `.env`. Para desarrollo local los valores por defecto funcionan sin cambios. Verifica que existan:

```bash
ls monitor-c/.env
ls monitor-s/.env
ls app-instance/.env
ls controller-asg/.env   # este necesita ajuste manual (ver nota abajo)
```

> **controller-asg/.env** contiene `SECURITY_GROUP_ID`, `SUBNET_ID` y `MONITOR_S_HOST` que apuntan a AWS. Para correr localmente sin AWS, el ControllerASG simplemente no podrá crear instancias EC2, pero MonitorS, MonitorC y AppInstance funcionan completos.

### 4. Levantar el stack completo

```bash
docker compose up --build
```

Esto construye las imágenes y levanta todos los servicios en orden. Los logs de todos los contenedores aparecen en la misma terminal con prefijo por servicio.

Para correr en background:

```bash
docker compose up --build -d
```

Ver logs de un servicio específico:

```bash
docker compose logs -f monitor-s
docker compose logs -f monitor-c
docker compose logs -f app-instance
```

### 5. Verificar que todo está corriendo

```bash
docker compose ps
```

Deberías ver algo así:

```
NAME              STATUS          PORTS
monitor-s         running         0.0.0.0:50052->50052/tcp
monitor-c         running         0.0.0.0:50051->50051/tcp
app-instance      running         0.0.0.0:8080->8080/tcp
controller-asg    running
```

### 6. Probar los endpoints de AppInstance

```bash
# Liveness check
curl http://localhost:8080/health

# Estado completo con carga actual
curl http://localhost:8080/status

# Solo la métrica de carga
curl http://localhost:8080/metrics

# Forzar una carga manual para probar las políticas de escalamiento
curl -X POST http://localhost:8080/load/set \
  -H "Content-Type: application/json" \
  -d '{"load": 90}'
```

### 7. Bajar el stack

```bash
# Detener sin borrar volúmenes ni imágenes
docker compose down

# Detener y borrar todo (imágenes, volúmenes, redes)
docker compose down --rmi all --volumes
```

---

## Opción B — Correr cada componente directamente con Python

Útil para depurar un componente en particular sin levantar todo el stack.

### 1. Crear un entorno virtual (una sola vez)

```bash
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows
```

### 2. Instalar dependencias de todos los componentes

```bash
pip install -r monitor-c/requirements.txt
pip install -r monitor-s/requirements.txt
pip install -r app-instance/requirements.txt
pip install -r controller-asg/requirements.txt
```

O instalar todo de golpe con el archivo global:

```bash
pip install -r requirements-global.txt
```

### 3. Exportar el PYTHONPATH para que los módulos encuentren los protos

```bash
export PYTHONPATH=$(pwd)/shared-protos:$PYTHONPATH
```

> En Windows (PowerShell): `$env:PYTHONPATH = "$(Get-Location)\shared-protos;$env:PYTHONPATH"`

### 4. Levantar cada componente en terminales separadas

**Terminal 1 — AppInstance:**
```bash
source .venv/bin/activate
cd app-instance
cp .env ../.env_app && export $(cat .env | xargs)
python app.py
```

**Terminal 2 — MonitorC:**
```bash
source .venv/bin/activate
cd monitor-c
export $(cat .env | xargs)
python monitor_c_server.py
```

**Terminal 3 — MonitorS:**
```bash
source .venv/bin/activate
cd monitor-s
export $(cat .env | xargs)
python monitor_s.py
```

**Terminal 4 — ControllerASG (opcional sin AWS):**
```bash
source .venv/bin/activate
cd controller-asg
export $(cat .env | xargs)
python controller.py
```

> El ControllerASG fallará al intentar conectarse a AWS pero el resto del sistema opera normalmente.

---

## Orden de arranque esperado

El sistema tiene dependencias de arranque. El orden correcto es:

```
AppInstance  →  MonitorC  →  MonitorS  →  ControllerASG
```

Docker Compose maneja esto automáticamente con `depends_on`. Si corres con Python directamente, respeta este orden o verás errores de conexión en los logs (son recuperables, MonitorC reintenta el registro hasta 10 veces).

---

## Flujo observable en los logs

Una vez todo corriendo, en los logs de MonitorS verás rondas de polling cada `POLL_INTERVAL` segundos:

```
[MonitorS] INFO  Ronda de polling iniciada — 1 instancias registradas
[MonitorS] INFO  instance-local-01 → HEALTHY  cpu=34.2%  rtt=1.3ms
[MonitorS] INFO  Resumen: total=1  healthy=1  unreachable=0  cpu_avg=34.2%
```

Y en los logs de MonitorC verás las consultas entrantes:

```
[MonitorC] DEBUG Ping recibido de monitor-s-1
[MonitorC] DEBUG GetMetrics → {'cpu_load': 34.2, 'memory_usage': 41.7, ...}
```

Para ver cómo reacciona el sistema ante carga alta, usa el endpoint de forzado:

```bash
curl -X POST http://localhost:8080/load/set \
  -H "Content-Type: application/json" \
  -d '{"load": 92}'
```

Después de 2–3 rondas de polling verás la instancia pasar a `CRITICAL` en los logs del MonitorS.

---

## Solución de problemas comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `ModuleNotFoundError: monitor_pb2` | PYTHONPATH no incluye `shared-protos/` | `export PYTHONPATH=$(pwd)/shared-protos:$PYTHONPATH` |
| MonitorC no se registra | MonitorS aún no está listo | Normal — MonitorC reintenta 10 veces. Espera ~50 s o levanta MonitorS primero |
| `Connection refused` en puerto 50051/50052 | El servicio no arrancó | Revisa logs con `docker compose logs <servicio>` |
| ControllerASG falla con `NoCredentialsError` | No hay credenciales AWS configuradas | Esperado en local. El resto del sistema no se ve afectado |
| Puerto 8080 ocupado | Otro proceso usa el puerto | Cambia `APP_PORT` en `app-instance/.env` y el mapping en `docker-compose.yml` |
