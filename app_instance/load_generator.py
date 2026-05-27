"""
load_generator.py - Simulador de carga para AppInstance

Simula métricas de carga de manera gradual y realista.
La carga fluctúa con:
  - Deriva lenta (random walk con reversión a la media)
  - Ruido gaussiano pequeño en cada tick
  - Picos ocasionales para simular ráfagas de tráfico

MonitorC importa la instancia singleton `shared_load` para reportar
la métrica actual sin duplicar el hilo de simulación.
"""

import threading
import random
import time
import logging
import os

logger = logging.getLogger(__name__)

# ── Parámetros de simulación (ajustables via .env) ───────────────────────────
TICK_SECONDS   = float(os.getenv("LOAD_TICK_SECONDS",  "2.0"))   # intervalo entre actualizaciones
MEAN_LOAD      = float(os.getenv("LOAD_MEAN",          "50.0"))   # media hacia la que deriva (0-100)
REVERSION_RATE = float(os.getenv("LOAD_REVERSION",     "0.05"))   # qué tan fuerte es la reversión
NOISE_STD      = float(os.getenv("LOAD_NOISE_STD",     "3.0"))    # desviación estándar del ruido
SPIKE_PROB     = float(os.getenv("LOAD_SPIKE_PROB",    "0.03"))   # probabilidad de pico por tick
SPIKE_MIN      = float(os.getenv("LOAD_SPIKE_MIN",     "20.0"))   # incremento mínimo de un pico
SPIKE_MAX      = float(os.getenv("LOAD_SPIKE_MAX",     "40.0"))   # incremento máximo de un pico
SPIKE_DECAY    = float(os.getenv("LOAD_SPIKE_DECAY",   "0.8"))    # factor de decaimiento del pico


class LoadGenerator:
    """
    Genera una señal de carga continua y gradual entre 0 y 100.

    Uso:
        gen = LoadGenerator()
        gen.start()
        print(gen.current_load)   # lectura thread-safe
        gen.stop()
    """

    def __init__(self, initial_load: float | None = None):
        self._lock = threading.Lock()
        self._load: float = initial_load if initial_load is not None else MEAN_LOAD
        self._spike_extra: float = 0.0          # carga adicional temporal por pico
        self._running = False
        self._thread: threading.Thread | None = None

    # ── Propiedades públicas ─────────────────────────────────────────────────

    @property
    def current_load(self) -> float:
        with self._lock:
            return round(self._load, 2)

    def set_load(self, value: float):
        """Fuerza un valor de carga (útil para pruebas manuales vía HTTP)."""
        with self._lock:
            self._load = max(0.0, min(100.0, value))
        logger.info("Carga fijada manualmente a %.1f%%", self._load)

    # ── Control del hilo ─────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="LoadGenerator")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ── Lógica de simulación ─────────────────────────────────────────────────

    def _run(self):
        logger.info(
            "Simulación de carga iniciada (mean=%.0f%%, tick=%.1fs, spike_prob=%.0f%%)",
            MEAN_LOAD, TICK_SECONDS, SPIKE_PROB * 100,
        )
        while self._running:
            with self._lock:
                self._step()
            time.sleep(TICK_SECONDS)

    def _step(self):
        """Un tick de simulación (debe llamarse con _lock adquirido)."""
        # 1. Ruido gaussiano
        noise = random.gauss(0, NOISE_STD)

        # 2. Reversión a la media (Ornstein-Uhlenbeck discreto)
        reversion = REVERSION_RATE * (MEAN_LOAD - self._load)

        # 3. Decaer picos previos
        self._spike_extra *= SPIKE_DECAY

        # 4. Disparo de nuevo pico
        if random.random() < SPIKE_PROB:
            spike = random.uniform(SPIKE_MIN, SPIKE_MAX)
            self._spike_extra += spike
            logger.debug("Pico de carga: +%.1f%%", spike)

        # 5. Aplicar cambio y acotar a [0, 100]
        delta = reversion + noise + self._spike_extra
        self._load = max(0.0, min(100.0, self._load + delta))


# ── Singleton compartido ──────────────────────────────────────────────────────
# MonitorC puede importar esto directamente:
#   from load_generator import shared_load
shared_load = LoadGenerator()
