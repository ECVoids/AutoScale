"""
scaling_policy.py — Políticas de escalamiento del ControllerASG.

Recibe el snapshot del MonitorS y retorna una decisión:
  "scale_up"   → crear una nueva instancia
  "scale_down" → eliminar una instancia
  "no_change"  → mantener el estado actual

Las políticas son deliberadamente conservadoras: se exige que la condición
se cumpla de forma sostenida (contadores de confirmación) para evitar
oscilaciones rápidas (flapping).
"""

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ── Umbrales (ajustables sin tocar lógica) ───────────────────────────────────

# Scale-up: CPU promedio sobre este umbral durante N evaluaciones seguidas
SCALE_UP_CPU_THRESHOLD   = 70.0   # %
SCALE_UP_CONFIRM_ROUNDS  = 2      # evaluaciones consecutivas requeridas

# Scale-down: CPU promedio bajo este umbral durante N evaluaciones seguidas
SCALE_DOWN_CPU_THRESHOLD = 30.0   # %
SCALE_DOWN_CONFIRM_ROUNDS = 3     # más conservador para no destruir prematuramente

# Umbral de instancias UNREACHABLE para forzar scale-up de reemplazo
UNREACHABLE_RATIO_THRESHOLD = 0.5  # si >50% de instancias son unreachable → scale_up


@dataclass
class ScalingPolicy:
    min_instances: int
    max_instances: int

    # Contadores internos de confirmación
    _up_streak:   int = field(default=0, init=False)
    _down_streak: int = field(default=0, init=False)

    # ── Punto de entrada ──────────────────────────────────────────────────────

    def evaluate(self, state: dict) -> str:
        """
        Evalúa el snapshot del MonitorS y retorna la decisión de escalamiento.

        state = {
            total: int,
            healthy: int,
            unreachable: int,
            average_cpu: float,
            instances: list[dict],
        }
        """
        total       = state.get("total", 0)
        healthy     = state.get("healthy", 0)
        unreachable = state.get("unreachable", 0)
        avg_cpu     = state.get("average_cpu", 0.0)

        # ── Regla 0: Siempre mantener el mínimo ──────────────────────────────
        if total < self.min_instances:
            log.info(
                "Política: total(%d) < min(%d) → scale_up (regla mínimo)",
                total, self.min_instances,
            )
            self._reset_streaks()
            return "scale_up"

        # ── Regla 1: Reemplazo por instancias inalcanzables ───────────────────
        if total > 0 and (unreachable / total) >= UNREACHABLE_RATIO_THRESHOLD:
            log.info(
                "Política: %.0f%% de instancias UNREACHABLE → scale_up (regla reemplazo)",
                (unreachable / total) * 100,
            )
            self._reset_streaks()
            return "scale_up"

        # ── Regla 2: Scale-up por CPU alta ───────────────────────────────────
        if avg_cpu >= SCALE_UP_CPU_THRESHOLD and total < self.max_instances:
            self._down_streak = 0
            self._up_streak  += 1
            log.info(
                "Política: avg_cpu=%.1f%% ≥ %.1f%% | up_streak=%d/%d",
                avg_cpu, SCALE_UP_CPU_THRESHOLD,
                self._up_streak, SCALE_UP_CONFIRM_ROUNDS,
            )
            if self._up_streak >= SCALE_UP_CONFIRM_ROUNDS:
                self._up_streak = 0
                return "scale_up"
            return "no_change"

        # ── Regla 3: Scale-down por CPU baja ─────────────────────────────────
        if avg_cpu <= SCALE_DOWN_CPU_THRESHOLD and total > self.min_instances:
            self._up_streak    = 0
            self._down_streak += 1
            log.info(
                "Política: avg_cpu=%.1f%% ≤ %.1f%% | down_streak=%d/%d",
                avg_cpu, SCALE_DOWN_CPU_THRESHOLD,
                self._down_streak, SCALE_DOWN_CONFIRM_ROUNDS,
            )
            if self._down_streak >= SCALE_DOWN_CONFIRM_ROUNDS:
                self._down_streak = 0
                return "scale_down"
            return "no_change"

        # ── Sin cambios ───────────────────────────────────────────────────────
        self._reset_streaks()
        log.info(
            "Política: avg_cpu=%.1f%% dentro del rango [%.1f%%, %.1f%%] → no_change",
            avg_cpu, SCALE_DOWN_CPU_THRESHOLD, SCALE_UP_CPU_THRESHOLD,
        )
        return "no_change"

    # ── Selección de candidato para scale-down ────────────────────────────────

    def pick_candidate_for_removal(self, instances: list[dict]) -> str | None:
        """
        Elige la instancia menos cargada (o la más antigua si hay empate).
        Retorna el instance_id o None si no hay candidatos válidos.

        Solo considera instancias en estado HEALTHY o DEGRADED; nunca
        elimina instancias UNREACHABLE (ya están fallando — dejarlas morir).
        """
        candidates = [
            i for i in instances
            if i.get("status") in ("HEALTHY", "DEGRADED")
        ]
        if not candidates:
            return None

        # Ordenar: menor cpu_percent primero; en caso de empate, mayor uptime
        # (eliminar las más antiguas si tienen poca carga)
        candidates.sort(
            key=lambda i: (
                i.get("last_metrics", {}).get("cpu_percent", 100),
                -i.get("last_metrics", {}).get("uptime_seconds", 0),
            )
        )
        chosen = candidates[0]["instance_id"]
        log.info(
            "Candidato para scale-down: %s (cpu=%.1f%%)",
            chosen,
            candidates[0].get("last_metrics", {}).get("cpu_percent", 0),
        )
        return chosen

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _reset_streaks(self):
        self._up_streak   = 0
        self._down_streak = 0
