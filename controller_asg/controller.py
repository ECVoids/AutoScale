"""
ControllerASG — Servicio de autoescalamiento.

Corre en el mismo proceso que MonitorS e importa el singleton `registry`
directamente (sin red). Evalúa las políticas de escalamiento cada
EVAL_INTERVAL segundos y delega la creación/destrucción de instancias
EC2 a aws_manager.py.
"""

import os
import time
import logging
import threading
from dotenv import load_dotenv

from instance_registry import registry          # shared memory con MonitorS
from scaling_policy import ScalingPolicy
from aws_manager import AWSManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ControllerASG] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuración ────────────────────────────────────────────────────────────
EVAL_INTERVAL   = int(os.getenv("EVAL_INTERVAL", "30"))   # segundos entre evaluaciones
MIN_INSTANCES   = int(os.getenv("MIN_INSTANCES", "2"))
MAX_INSTANCES   = int(os.getenv("MAX_INSTANCES", "5"))
COOLDOWN_UP     = int(os.getenv("COOLDOWN_UP",   "120"))  # segundos entre scale-ups
COOLDOWN_DOWN   = int(os.getenv("COOLDOWN_DOWN", "180"))  # segundos entre scale-downs


class ControllerASG:
    """Bucle principal de autoescalamiento."""

    def __init__(self):
        self.policy      = ScalingPolicy(MIN_INSTANCES, MAX_INSTANCES)
        self.aws         = AWSManager()
        self._lock       = threading.Lock()
        self._last_scale_up   = 0.0
        self._last_scale_down = 0.0
        self._running    = False

    # ── Arranque / parada ────────────────────────────────────────────────────

    def start(self):
        self._running = True
        log.info(
            "ControllerASG iniciado | min=%d max=%d eval_interval=%ds",
            MIN_INSTANCES, MAX_INSTANCES, EVAL_INTERVAL,
        )
        self._loop()

    def stop(self):
        self._running = False

    # ── Bucle principal ──────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self._evaluate()
            except Exception as exc:
                log.error("Error en ciclo de evaluación: %s", exc, exc_info=True)
            time.sleep(EVAL_INTERVAL)

    def _evaluate(self):
        state = registry.snapshot_for_controller()

        log.info(
            "Snapshot | total=%d healthy=%d unreachable=%d avg_cpu=%.1f%%",
            state["total"],
            state["healthy"],
            state["unreachable"],
            state["average_cpu"],
        )

        decision = self.policy.evaluate(state)
        now = time.time()

        if decision == "scale_up":
            if now - self._last_scale_up < COOLDOWN_UP:
                remaining = int(COOLDOWN_UP - (now - self._last_scale_up))
                log.info("Scale-up bloqueado por cooldown (%ds restantes)", remaining)
                return
            self._scale_up(state)
            self._last_scale_up = now

        elif decision == "scale_down":
            if now - self._last_scale_down < COOLDOWN_DOWN:
                remaining = int(COOLDOWN_DOWN - (now - self._last_scale_down))
                log.info("Scale-down bloqueado por cooldown (%ds restantes)", remaining)
                return
            self._scale_down(state)
            self._last_scale_down = now

        else:
            log.info("Decisión: sin cambios.")

    # ── Acciones de escalamiento ─────────────────────────────────────────────

    def _scale_up(self, state: dict):
        current = state["total"]
        if current >= MAX_INSTANCES:
            log.warning("Scale-up ignorado: ya estamos en el máximo (%d).", MAX_INSTANCES)
            return

        log.info("⬆  Scale-up: creando nueva instancia EC2 (actual=%d)…", current)
        instance_id = self.aws.launch_instance()
        if instance_id:
            log.info("⬆  Instancia creada: %s", instance_id)
        else:
            log.error("Scale-up falló: aws_manager no devolvió instance_id.")

    def _scale_down(self, state: dict):
        current = state["total"]
        if current <= MIN_INSTANCES:
            log.warning("Scale-down ignorado: ya estamos en el mínimo (%d).", MIN_INSTANCES)
            return

        # Elegir la instancia menos cargada para terminar
        candidate = self.policy.pick_candidate_for_removal(state["instances"])
        if not candidate:
            log.warning("Scale-down: no se encontró candidato apto.")
            return

        log.info("⬇  Scale-down: terminando instancia %s (actual=%d)…", candidate, current)
        # Notificar al MonitorC antes de terminar
        self._deregister_gracefully(candidate, state["instances"])
        ok = self.aws.terminate_instance(candidate)
        if ok:
            log.info("⬇  Instancia terminada: %s", candidate)
        else:
            log.error("Scale-down falló: no se pudo terminar %s.", candidate)

    def _deregister_gracefully(self, instance_id: str, instances: list):
        """Intenta llamar force_deregister en el MonitorC antes de terminar la EC2."""
        try:
            from grpc_client import GRPCClient  # importado aquí para evitar ciclos
            entry = next((i for i in instances if i["instance_id"] == instance_id), None)
            if entry:
                client = GRPCClient(entry["ip_address"], entry["port"])
                client.force_deregister("ControllerASG scale-down")
                log.info("force_deregister enviado a %s", instance_id)
        except Exception as exc:
            log.warning("force_deregister falló para %s: %s", instance_id, exc)


# ── Punto de entrada (standalone) ────────────────────────────────────────────

if __name__ == "__main__":
    controller = ControllerASG()
    try:
        controller.start()
    except KeyboardInterrupt:
        log.info("ControllerASG detenido por el usuario.")
        controller.stop()
