"""
monitor_c_server.py
-------------------
Servidor gRPC que implementa MonitorCService.
Corre dentro de cada AppInstance.

Responsabilidades:
  1. Responder Ping/Pong al MonitorS (heartbeat).
  2. Entregar métricas actualizadas (GetMetrics).
  3. Auto-registrarse con el MonitorS al arrancar.
  4. Desregistrarse limpiamente al apagarse (SIGTERM / SIGINT).
  5. Responder GetStatus con estado resumido.

Variables de entorno (ver .env):
  INSTANCE_ID      : identificador único de esta instancia
  INSTANCE_IP      : IP o hostname de esta instancia (reportada al MonitorS)
  MONITOR_C_PORT   : puerto donde escucha este servidor gRPC   (default 50051)
  MONITOR_S_HOST   : host del MonitorS para auto-registro
  MONITOR_S_PORT   : puerto gRPC del MonitorS                  (default 50052)
  AGENT_VERSION    : versión del agente MonitorC               (default 1.0.0)
  METRICS_INTERVAL : segundos entre actualizaciones de métricas (default 5)
"""

import os
import signal
import sys
import time
import threading
import logging
from concurrent import futures

import grpc

# Los pb2 están en shared-protos; ajustar PYTHONPATH en Dockerfile / docker-compose
import monitor_pb2
import monitor_pb2_grpc

from monitor_c.metrics_simulator import MetricsSimulator

# ──────────────────────────────────────────────────────────────────────────────
# Configuración de logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MonitorC] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Lectura de variables de entorno
# ──────────────────────────────────────────────────────────────────────────────
INSTANCE_ID      = os.getenv("INSTANCE_ID", "instance-local")
INSTANCE_IP      = os.getenv("INSTANCE_IP", "127.0.0.1")
MONITOR_C_PORT   = int(os.getenv("MONITOR_C_PORT", "50051"))
MONITOR_S_HOST   = os.getenv("MONITOR_S_HOST", "monitor-s")
MONITOR_S_PORT   = int(os.getenv("MONITOR_S_PORT", "50052"))
AGENT_VERSION    = os.getenv("AGENT_VERSION", "1.0.0")
METRICS_INTERVAL = float(os.getenv("METRICS_INTERVAL", "5"))


# ──────────────────────────────────────────────────────────────────────────────
# Implementación del servicio gRPC
# ──────────────────────────────────────────────────────────────────────────────
class MonitorCServicer(monitor_pb2_grpc.MonitorCServiceServicer):
    """Implementa todos los RPCs definidos en monitor.proto."""

    def __init__(self, simulator: MetricsSimulator):
        self._sim = simulator

    # ── Ping / Pong ────────────────────────────────────────────────────────────
    def Ping(self, request, context):
        log.debug("Ping recibido de %s", request.instance_id)
        return monitor_pb2.PongResponse(
            instance_id=INSTANCE_ID,
            timestamp=request.timestamp,   # echo para medir RTT en MonitorS
            alive=True,
        )

    # ── GetMetrics ─────────────────────────────────────────────────────────────
    def GetMetrics(self, request, context):
        snap = self._sim.snapshot()
        log.debug("GetMetrics → %s", snap)
        return monitor_pb2.MetricsResponse(
            instance_id  =INSTANCE_ID,
            cpu_load     =snap["cpu_load"],
            memory_usage =snap["memory_usage"],
            disk_usage   =snap["disk_usage"],
            active_conns =snap["active_conns"],
            timestamp    =int(time.time() * 1000),
        )

    # ── Register ───────────────────────────────────────────────────────────────
    def Register(self, request, context):
        """
        El MonitorS puede llamar Register para forzar un re-registro
        (raro, pero posible si el MonitorS reinicia y pierde su estado).
        La lógica principal de registro es iniciada por MonitorC al arrancar.
        """
        log.info("Register llamado por MonitorS (re-registro forzado)")
        return monitor_pb2.RegisterResponse(
            success     =True,
            message     ="Re-registro aceptado",
            monitor_s_id=f"{MONITOR_S_HOST}:{MONITOR_S_PORT}",
        )

    # ── Deregister ─────────────────────────────────────────────────────────────
    def Deregister(self, request, context):
        log.info("Deregister llamado por MonitorS: %s", request.reason)
        return monitor_pb2.DeregisterResponse(
            success=True,
            message=f"Desregistro de {INSTANCE_ID} aceptado",
        )

    # ── GetStatus ──────────────────────────────────────────────────────────────
    def GetStatus(self, request, context):
        snap = self._sim.snapshot()
        return monitor_pb2.StatusResponse(
            instance_id =INSTANCE_ID,
            state       =snap["state"],
            app_version =AGENT_VERSION,
            uptime_secs =snap["uptime_secs"],
        )


# ──────────────────────────────────────────────────────────────────────────────
# Auto-registro con MonitorS
# ──────────────────────────────────────────────────────────────────────────────
def _register_with_monitor_s(max_retries: int = 10, retry_delay: float = 5.0) -> bool:
    """
    Intenta registrarse con el MonitorS al arrancar.
    Reintenta hasta max_retries veces antes de rendirse.
    """
    target = f"{MONITOR_S_HOST}:{MONITOR_S_PORT}"
    request = monitor_pb2.RegisterRequest(
        info=monitor_pb2.InstanceInfo(
            instance_id=INSTANCE_ID,
            ip_address =INSTANCE_IP,
            port       =MONITOR_C_PORT,
        ),
        version=AGENT_VERSION,
    )

    for attempt in range(1, max_retries + 1):
        try:
            with grpc.insecure_channel(target) as channel:
                stub    = monitor_pb2_grpc.MonitorCServiceStub(channel)
                # Llamamos al Register del MonitorS (su propio servicio gRPC)
                # Nota: MonitorS debe exponer un endpoint de registro compatible.
                # Por simplicidad usamos el mismo stub; en producción MonitorS
                # tendría su propio servicio gRPC separado.
                response = stub.Register(request, timeout=5.0)
            if response.success:
                log.info("Registrado con MonitorS (%s): %s", target, response.message)
                return True
            else:
                log.warning("Registro rechazado: %s", response.message)
        except grpc.RpcError as e:
            log.warning("Intento %d/%d — MonitorS no disponible: %s",
                        attempt, max_retries, e.details())
        time.sleep(retry_delay)

    log.error("No se pudo registrar con MonitorS tras %d intentos.", max_retries)
    return False


def _deregister_from_monitor_s(reason: str = "shutdown") -> None:
    """Envía Deregister al MonitorS antes de apagarse."""
    target  = f"{MONITOR_S_HOST}:{MONITOR_S_PORT}"
    request = monitor_pb2.DeregisterRequest(
        instance_id=INSTANCE_ID,
        reason     =reason,
    )
    try:
        with grpc.insecure_channel(target) as channel:
            stub = monitor_pb2_grpc.MonitorCServiceStub(channel)
            stub.Deregister(request, timeout=3.0)
        log.info("Desregistrado del MonitorS correctamente.")
    except grpc.RpcError as e:
        log.warning("No se pudo desregistrar: %s", e.details())


# ──────────────────────────────────────────────────────────────────────────────
# Hilo de actualización periódica de métricas
# ──────────────────────────────────────────────────────────────────────────────
def _metrics_update_loop(simulator: MetricsSimulator, stop_event: threading.Event) -> None:
    """Actualiza las métricas cada METRICS_INTERVAL segundos."""
    while not stop_event.is_set():
        simulator.update()
        log.debug("Métricas actualizadas: %s", simulator.snapshot())
        stop_event.wait(METRICS_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def serve() -> None:
    simulator  = MetricsSimulator()
    stop_event = threading.Event()

    # Hilo de métricas
    metrics_thread = threading.Thread(
        target=_metrics_update_loop,
        args=(simulator, stop_event),
        daemon=True,
        name="metrics-updater",
    )
    metrics_thread.start()

    # Servidor gRPC
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    monitor_pb2_grpc.add_MonitorCServiceServicer_to_server(
        MonitorCServicer(simulator), server
    )
    listen_addr = f"0.0.0.0:{MONITOR_C_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log.info("MonitorC escuchando en %s (instancia: %s)", listen_addr, INSTANCE_ID)

    # Auto-registro (en hilo para no bloquear el servidor)
    threading.Thread(
        target=_register_with_monitor_s,
        daemon=True,
        name="auto-register",
    ).start()

    # Manejo de señales para apagado limpio
    def _shutdown(signum, frame):
        log.info("Señal %d recibida — apagando MonitorC...", signum)
        stop_event.set()
        _deregister_from_monitor_s(reason="shutdown")
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # Bloquear hilo principal
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
