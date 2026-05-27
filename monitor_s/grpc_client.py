"""
grpc_client.py
--------------
Cliente gRPC que el MonitorS usa para hablar con cada MonitorC.

Encapsula:
  - Ping  (heartbeat)
  - GetMetrics
  - GetStatus
  - Deregister forzado (cuando ControllerASG elimina una instancia)

Cada método crea un canal efímero (insecure) y lo cierra al terminar.
Para un sistema de producción real se podría usar un pool de canales,
pero para este proyecto la sobrecarga es despreciable.
"""

import time
import logging
import grpc

import monitor_pb2
import monitor_pb2_grpc
from instance_registry import MetricsSnapshot

log = logging.getLogger(__name__)

# Timeout por defecto para cada llamada gRPC (segundos)
DEFAULT_TIMEOUT = 5.0


class MonitorCClient:
    """
    Cliente de un MonitorC específico.

    Uso:
        client = MonitorCClient(instance_id="i-abc123", host="10.0.1.5", port=50051)
        ok, rtt = client.ping()
        snapshot = client.get_metrics()
    """

    def __init__(self, instance_id: str, host: str, port: int, timeout: float = DEFAULT_TIMEOUT):
        self.instance_id = instance_id
        self.host        = host
        self.port        = port
        self.timeout     = timeout
        self._target     = f"{host}:{port}"

    def _channel(self):
        return grpc.insecure_channel(self._target)

    # ── Ping / Pong ───────────────────────────────────────────────────────────

    def ping(self) -> tuple[bool, float]:
        """
        Envía un Ping y retorna (alive, rtt_ms).
        Si falla retorna (False, -1).
        """
        sent_ts = int(time.time() * 1000)
        try:
            with self._channel() as ch:
                stub     = monitor_pb2_grpc.MonitorCServiceStub(ch)
                response = stub.Ping(
                    monitor_pb2.PingRequest(
                        instance_id=self.instance_id,
                        timestamp=sent_ts,
                    ),
                    timeout=self.timeout,
                )
            rtt_ms = int(time.time() * 1000) - sent_ts
            log.debug("Ping OK  %s  rtt=%dms  alive=%s", self._target, rtt_ms, response.alive)
            return response.alive, rtt_ms
        except grpc.RpcError as e:
            log.debug("Ping FAIL %s: %s", self._target, e.details())
            return False, -1

    # ── GetMetrics ────────────────────────────────────────────────────────────

    def get_metrics(self) -> MetricsSnapshot | None:
        """
        Pide métricas al MonitorC.
        Retorna MetricsSnapshot o None si falla.
        """
        try:
            with self._channel() as ch:
                stub     = monitor_pb2_grpc.MonitorCServiceStub(ch)
                response = stub.GetMetrics(
                    monitor_pb2.MetricsRequest(instance_id=self.instance_id),
                    timeout=self.timeout,
                )
            snap = MetricsSnapshot(
                cpu_load     = response.cpu_load,
                memory_usage = response.memory_usage,
                disk_usage   = response.disk_usage,
                active_conns = response.active_conns,
                timestamp    = response.timestamp,
            )
            log.debug(
                "GetMetrics %s  cpu=%.1f%%  mem=%.1f%%  conns=%d",
                self._target, snap.cpu_load, snap.memory_usage, snap.active_conns,
            )
            return snap
        except grpc.RpcError as e:
            log.debug("GetMetrics FAIL %s: %s", self._target, e.details())
            return None

    # ── GetStatus ─────────────────────────────────────────────────────────────

    def get_status(self) -> dict | None:
        """
        Obtiene estado resumido (state, app_version, uptime_secs).
        Retorna dict o None si falla.
        """
        try:
            with self._channel() as ch:
                stub     = monitor_pb2_grpc.MonitorCServiceStub(ch)
                response = stub.GetStatus(
                    monitor_pb2.StatusRequest(instance_id=self.instance_id),
                    timeout=self.timeout,
                )
            return {
                "instance_id": response.instance_id,
                "state":       response.state,
                "app_version": response.app_version,
                "uptime_secs": response.uptime_secs,
            }
        except grpc.RpcError as e:
            log.debug("GetStatus FAIL %s: %s", self._target, e.details())
            return None

    # ── Deregister forzado ────────────────────────────────────────────────────

    def force_deregister(self, reason: str = "scale_in") -> bool:
        """
        El ControllerASG llama esto antes de terminar una instancia EC2,
        para que MonitorC se limpie antes de que la máquina desaparezca.
        """
        try:
            with self._channel() as ch:
                stub     = monitor_pb2_grpc.MonitorCServiceStub(ch)
                response = stub.Deregister(
                    monitor_pb2.DeregisterRequest(
                        instance_id=self.instance_id,
                        reason=reason,
                    ),
                    timeout=self.timeout,
                )
            log.info("Deregister forzado %s: success=%s", self._target, response.success)
            return response.success
        except grpc.RpcError as e:
            log.warning("Force deregister FAIL %s: %s", self._target, e.details())
            return False
