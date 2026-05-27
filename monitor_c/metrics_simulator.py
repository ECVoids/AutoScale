"""
metrics_simulator.py
--------------------
Simula métricas de una AppInstance de forma gradual y realista.

Estrategia:
  - cpu_load    : random walk acotado + onda senoidal lenta (imita picos de carga reales)
  - memory_usage: sube lentamente y se resetea ocasionalmente (imita leaks o GC)
  - disk_usage  : sube muy lento, casi estable
  - active_conns: correlacionado con cpu_load (más carga → más conexiones)
"""

import math
import random
import time


class MetricsSimulator:
    def __init__(self):
        self._start_time = time.time()

        # Estado interno — valores iniciales centrados en rangos medios
        self._cpu        = random.uniform(20.0, 40.0)
        self._memory     = random.uniform(30.0, 50.0)
        self._disk       = random.uniform(10.0, 30.0)
        self._conns      = random.randint(5, 20)

        # Parámetros de la onda senoidal para cpu
        self._cpu_wave_amplitude = random.uniform(10.0, 20.0)
        self._cpu_wave_period    = random.uniform(60.0, 120.0)  # segundos por ciclo

    # ──────────────────────────────────────────────────────────
    # Helpers internos
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _random_walk(self, current: float, step: float, lo: float, hi: float) -> float:
        """Desplaza `current` un paso aleatorio acotado entre lo y hi."""
        delta = random.uniform(-step, step)
        return self._clamp(current + delta, lo, hi)

    # ──────────────────────────────────────────────────────────
    # Actualización de métricas (llamar cada tick del servidor)
    # ──────────────────────────────────────────────────────────

    def update(self) -> None:
        """Avanza la simulación un tick. Llamar antes de leer las métricas."""
        elapsed = time.time() - self._start_time

        # cpu_load: random walk suave + componente senoidal
        wave      = self._cpu_wave_amplitude * math.sin(2 * math.pi * elapsed / self._cpu_wave_period)
        self._cpu = self._random_walk(self._cpu, step=2.0, lo=5.0, hi=95.0)
        self._cpu = self._clamp(self._cpu + wave * 0.1, 5.0, 95.0)

        # memory_usage: sube lentamente, reset ocasional (simula GC)
        if random.random() < 0.02:                          # 2 % de probabilidad de GC
            self._memory = random.uniform(20.0, 35.0)
        else:
            self._memory = self._random_walk(self._memory, step=0.8, lo=10.0, hi=90.0)

        # disk_usage: casi estático, sube muy despacio
        self._disk = self._random_walk(self._disk, step=0.1, lo=5.0, hi=85.0)

        # active_conns: correlacionado con cpu (más carga → más conexiones)
        target_conns = int(self._cpu / 100 * 80) + random.randint(-3, 3)
        self._conns  = max(0, target_conns)

    # ──────────────────────────────────────────────────────────
    # Lecturas
    # ──────────────────────────────────────────────────────────

    @property
    def cpu_load(self) -> float:
        return round(self._cpu, 2)

    @property
    def memory_usage(self) -> float:
        return round(self._memory, 2)

    @property
    def disk_usage(self) -> float:
        return round(self._disk, 2)

    @property
    def active_conns(self) -> int:
        return self._conns

    @property
    def uptime_secs(self) -> int:
        return int(time.time() - self._start_time)

    def state_label(self) -> str:
        """Devuelve HEALTHY / DEGRADED / CRITICAL según cpu_load."""
        if self._cpu < 60:
            return "HEALTHY"
        elif self._cpu < 85:
            return "DEGRADED"
        return "CRITICAL"

    def snapshot(self) -> dict:
        """Retorna todas las métricas actuales como diccionario."""
        return {
            "cpu_load":     self.cpu_load,
            "memory_usage": self.memory_usage,
            "disk_usage":   self.disk_usage,
            "active_conns": self.active_conns,
            "uptime_secs":  self.uptime_secs,
            "state":        self.state_label(),
        }
