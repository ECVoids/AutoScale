"""
monitor_s.py
------------
Proceso principal del MonitorS.

Dos responsabilidades en paralelo:

  1. Servidor gRPC (MonitorSService)
     Escucha registros y desregistros iniciados por los MonitorC.
     Expone también un endpoint de consulta de estado global
     (usado por el ControllerASG, que corre en el mismo proceso).

  2. Polling loop
     Cada POLL_INTERVAL segundos recorre todas las instancias registradas,
     hace Ping y GetMetrics, y actualiza el InstanceRegistry.

El InstanceRegistry es el objeto de memoria compartida que el ControllerASG
importa directamente (sin red) para leer el estado del sistema.

Variables de entorno (ver .env):
  MONITOR_S_ID       : identificador de este MonitorS          (default: monitor-s-1)
  MONITOR_S_PORT     : puerto gRPC donde escucha               (default: 50052)
  POLL_INTERVAL      : segundos entre rondas de polling        (default: 10)
  HEARTBEAT_TIMEOUT  : segundos sin respuesta → fallo          (default: 5)
  MAX_INSTANCES      : límite duro de instancias rastreadas    (default: 5)
"""

import os
import sys
import signal
import time
import threading
import logging
from concurrent import futures

import grpc

import monitor_pb2
import monitor_pb2_grpc

from instance_registry import InstanceRegistry
from grpc_client import MonitorCClient

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MonitorS] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuración
# ──────────────────────────────────────────────────────────────────────────────
MONITOR_S_ID      = os.getenv("MONITOR_S_ID",      "monitor-s-1")
MONITOR_S_PORT    = int(os.getenv("MONITOR_S_PORT", "50052"))
POLL_INTERVAL     = float(os.getenv("POLL_INTERVAL",     "10"))
HEARTBEAT_TIMEOUT = float(os.getenv("HEARTBEAT_TIMEOUT", "5"))
MAX_INSTANCES     = int(os.getenv("MAX_INSTANCES",        "5"))

# ──────────────────────────────────────────────────────────────────────────────
# Singleton del registro (importable por ControllerASG)
# ──────────────────────────────────────────────────────────────────────────────
registry = InstanceRegistry()


# ──────────────────────────────────────────────────────────────────────────────
# Servidor gRPC — MonitorSService
# (reutiliza los stubs de MonitorCService para los mensajes de registro,
#  ya que el .proto define Register/Deregister en MonitorCService)
# ──────────────────────────────────────────────────────────────────────────────
class MonitorSServicer(monitor_pb2_grpc.MonitorCServiceServicer):
    """
    MonitorS actúa como servidor gRPC para aceptar registros de los MonitorC.

    Implementa solo Register y Deregister del MonitorCService;
    el resto de métodos no aplican aquí y retornan UNIMPLEMENTED.
    """

    # ── Register ──────────────────────────────────────────────────────────────
    def Register(self, request, context):
        info = request.info
        if not info.instance_id or not info.ip_address or not info.port:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("InstanceInfo incompleta")
            return monitor_pb2.RegisterResponse(success=False, message="InstanceInfo incompleta")

        if registry.count() >= MAX_INSTANCES and not registry.get(info.instance_id):
            msg = f"Límite de instancias alcanzado ({MAX_INSTANCES})"
            log.warning(msg)
            return monitor_pb2.RegisterResponse(
                success=False,
                message=msg,
                monitor_s_id=MONITOR_S_ID,
            )

        registry.register(
            instance_id=info.instance_id,
            ip_address=info.ip_address,
            port=info.port,
            version=request.version,
        )
        return monitor_pb2.RegisterResponse(
            success=True,
            message=f"Instancia {info.instance_id} registrada",
            monitor_s_id=MONITOR_S_ID,
        )

    # ── Deregister ────────────────────────────────────────────────────────────
    def Deregister(self, request, context):
        ok = registry.deregister(request.instance_id, reason=request.reason)
        return monitor_pb2.DeregisterResponse(
            success=ok,
            message="OK" if ok else f"Instancia {request.instance_id} no encontrada",
        )

    # ── Métodos no relevantes para MonitorS ───────────────────────────────────
    def Ping(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return monitor_pb2.PongResponse()

    def GetMetrics(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return monitor_pb2.MetricsResponse()

    def GetStatus(self, request, context):
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        return monitor_pb2.StatusResponse()


# ──────────────────────────────────────────────────────────────────────────────
# Polling loop
# ──────────────────────────────────────────────────────────────────────────────

def _poll_instance(entry) -> None:
    """
    Sondea una sola instancia: Ping → si vive → GetMetrics.
    Actualiza el registry según el resultado.
    """
    client = MonitorCClient(
        instance_id=entry.instance_id,
        host=entry.ip_address,
        port=entry.port,
        timeout=HEARTBEAT_TIMEOUT,
    )

    alive, rtt_ms = client.ping()

    if not alive:
        registry.record_failure(entry.instance_id)
        log.warning("Instancia %s no responde al Ping", entry.instance_id)
        return

    registry.record_heartbeat_ok(entry.instance_id)

    snap = client.get_metrics()
    if snap:
        registry.update_metrics(entry.instance_id, snap)
        log.info(
            "%-20s  cpu=%5.1f%%  mem=%5.1f%%  disk=%5.1f%%  conns=%3d  rtt=%dms  status=%s",
            entry.instance_id,
            snap.cpu_load, snap.memory_usage, snap.disk_usage, snap.active_conns,
            rtt_ms,
            registry.get(entry.instance_id).status if registry.get(entry.instance_id) else "?",
        )
    else:
        log.warning("Instancia %s: Ping OK pero GetMetrics falló", entry.instance_id)


def _polling_loop(stop_event: threading.Event) -> None:
    """
    Bucle principal de polling. Cada POLL_INTERVAL segundos recorre
    todas las instancias registradas y las sondea en paralelo.
    """
    log.info("Polling loop iniciado (intervalo=%ss)", POLL_INTERVAL)
    while not stop_event.is_set():
        instances = registry.all_instances()

        if not instances:
            log.debug("Sin instancias registradas, esperando...")
        else:
            # Paralelizar el polling para no bloquear si alguna instancia tarda
            threads = [
                threading.Thread(target=_poll_instance, args=(entry,), daemon=True)
                for entry in instances
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=HEARTBEAT_TIMEOUT + 1)

            summary = registry.snapshot_for_controller()
            log.info(
                "Ronda completada — total=%d  healthy=%d  unreachable=%d  avg_cpu=%.1f%%",
                summary["total"], summary["healthy"],
                summary["unreachable"], summary["average_cpu"],
            )

        stop_event.wait(POLL_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def serve() -> None:
    stop_event = threading.Event()

    # ── Servidor gRPC ─────────────────────────────────────────────────────────
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    monitor_pb2_grpc.add_MonitorCServiceServicer_to_server(MonitorSServicer(), server)
    listen_addr = f"0.0.0.0:{MONITOR_S_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()
    log.info("MonitorS escuchando en %s (id=%s)", listen_addr, MONITOR_S_ID)

    # ── Polling loop ──────────────────────────────────────────────────────────
    poll_thread = threading.Thread(
        target=_polling_loop,
        args=(stop_event,),
        daemon=True,
        name="polling-loop",
    )
    poll_thread.start()

    # ── Apagado limpio ────────────────────────────────────────────────────────
    def _shutdown(signum, frame):
        log.info("Señal %d recibida — apagando MonitorS...", signum)
        stop_event.set()
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    log.info("MonitorS listo. Esperando instancias...")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
