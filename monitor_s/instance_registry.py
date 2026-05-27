"""
instance_registry.py
--------------------
Registro centralizado de todas las AppInstances conocidas por el MonitorS.

Es el único estado compartido entre MonitorS y ControllerASG.
Todo acceso es thread-safe mediante RLock (reentrante para permitir
lecturas anidadas desde el mismo hilo).

Estructura de cada entrada:
  {
    "info":        InstanceInfo (dict con instance_id, ip_address, port),
    "version":     str,
    "registered_at": float (unix timestamp),
    "last_seen":   float (unix timestamp del último heartbeat exitoso),
    "last_metrics": MetricsSnapshot | None,
    "status":      "HEALTHY" | "DEGRADED" | "CRITICAL" | "UNREACHABLE",
    "consecutive_failures": int,
  }
"""

import threading
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger(__name__)

# ── Umbrales de estado ────────────────────────────────────────────────────────
FAILURE_THRESHOLD = int(3)   # fallos consecutivos para marcar UNREACHABLE


@dataclass
class MetricsSnapshot:
    cpu_load:     float
    memory_usage: float
    disk_usage:   float
    active_conns: int
    timestamp:    int   # unix ms


@dataclass
class InstanceEntry:
    instance_id:          str
    ip_address:           str
    port:                 int
    version:              str
    registered_at:        float = field(default_factory=time.time)
    last_seen:            float = field(default_factory=time.time)
    last_metrics:         Optional[MetricsSnapshot] = None
    status:               str = "HEALTHY"
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.last_metrics:
            d["last_metrics"] = asdict(self.last_metrics)
        return d


class InstanceRegistry:
    """
    Registro thread-safe de instancias. Compartido por referencia entre
    MonitorS y ControllerASG (memoria compartida dentro del mismo proceso).
    """

    def __init__(self):
        self._lock     = threading.RLock()
        self._entries: dict[str, InstanceEntry] = {}

    # ── Registro / desregistro ────────────────────────────────────────────────

    def register(self, instance_id: str, ip_address: str, port: int, version: str) -> bool:
        with self._lock:
            existed = instance_id in self._entries
            self._entries[instance_id] = InstanceEntry(
                instance_id=instance_id,
                ip_address=ip_address,
                port=port,
                version=version,
            )
            action = "actualizada" if existed else "registrada"
            log.info("Instancia %s — %s (ip=%s port=%d)", instance_id, action, ip_address, port)
            return True

    def deregister(self, instance_id: str, reason: str = "unknown") -> bool:
        with self._lock:
            if instance_id not in self._entries:
                log.warning("Deregister de instancia desconocida: %s", instance_id)
                return False
            del self._entries[instance_id]
            log.info("Instancia %s desregistrada (razón: %s)", instance_id, reason)
            return True

    # ── Actualización de heartbeat y métricas ─────────────────────────────────

    def record_heartbeat_ok(self, instance_id: str) -> None:
        with self._lock:
            entry = self._entries.get(instance_id)
            if not entry:
                return
            entry.last_seen            = time.time()
            entry.consecutive_failures = 0
            # No tocamos el status aquí; lo recalcula update_status()

    def record_failure(self, instance_id: str) -> None:
        with self._lock:
            entry = self._entries.get(instance_id)
            if not entry:
                return
            entry.consecutive_failures += 1
            if entry.consecutive_failures >= FAILURE_THRESHOLD:
                if entry.status != "UNREACHABLE":
                    log.warning(
                        "Instancia %s marcada UNREACHABLE (%d fallos consecutivos)",
                        instance_id, entry.consecutive_failures,
                    )
                entry.status = "UNREACHABLE"

    def update_metrics(self, instance_id: str, snapshot: MetricsSnapshot) -> None:
        with self._lock:
            entry = self._entries.get(instance_id)
            if not entry:
                return
            entry.last_metrics         = snapshot
            entry.last_seen            = time.time()
            entry.consecutive_failures = 0
            entry.status               = _status_from_cpu(snapshot.cpu_load)

    # ── Lecturas ──────────────────────────────────────────────────────────────

    def get(self, instance_id: str) -> Optional[InstanceEntry]:
        with self._lock:
            return self._entries.get(instance_id)

    def all_instances(self) -> list[InstanceEntry]:
        with self._lock:
            return list(self._entries.values())

    def healthy_instances(self) -> list[InstanceEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.status != "UNREACHABLE"]

    def unreachable_instances(self) -> list[InstanceEntry]:
        with self._lock:
            return [e for e in self._entries.values() if e.status == "UNREACHABLE"]

    def count(self) -> int:
        with self._lock:
            return len(self._entries)

    def average_cpu(self) -> float:
        """CPU promedio de instancias con métricas disponibles (excluye UNREACHABLE)."""
        with self._lock:
            loads = [
                e.last_metrics.cpu_load
                for e in self._entries.values()
                if e.last_metrics and e.status != "UNREACHABLE"
            ]
        return round(sum(loads) / len(loads), 2) if loads else 0.0

    def snapshot_for_controller(self) -> dict:
        """
        Resumen compacto consumido por ControllerASG para tomar decisiones.
        Llamar sin necesidad de importar InstanceEntry externamente.
        """
        with self._lock:
            instances = [e.to_dict() for e in self._entries.values()]
        return {
            "total":         len(instances),
            "healthy":       sum(1 for e in instances if e["status"] != "UNREACHABLE"),
            "unreachable":   sum(1 for e in instances if e["status"] == "UNREACHABLE"),
            "average_cpu":   self.average_cpu(),
            "instances":     instances,
            "snapshot_time": time.time(),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _status_from_cpu(cpu: float) -> str:
    if cpu < 60:
        return "HEALTHY"
    elif cpu < 85:
        return "DEGRADED"
    return "CRITICAL"
