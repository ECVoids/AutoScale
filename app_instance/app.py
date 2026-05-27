"""
app.py - AppInstance

Aplicación principal que corre dentro de cada instancia EC2.
Expone endpoints HTTP para health check y estado de carga.
Trabaja en conjunto con load_generator.py para simular carga,
y con MonitorC que reporta esas métricas via gRPC al MonitorS.
"""

import os
import time
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

from load_generator import LoadGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [APP] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── Configuración ────────────────────────────────────────────────────────────
APP_PORT = int(os.getenv("APP_PORT", 8080))
INSTANCE_ID = os.getenv("INSTANCE_ID", "local-dev")

# Singleton compartido de carga (MonitorC también lo leerá via import)
load_gen = LoadGenerator()


# ── Handler HTTP ─────────────────────────────────────────────────────────────
class AppHandler(BaseHTTPRequestHandler):
    """Endpoints básicos de la AppInstance."""

    def log_message(self, format, *args):  # silencia logs por defecto de HTTPServer
        pass

    def _send_json(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "instance_id": INSTANCE_ID})

        elif self.path == "/status":
            self._send_json(200, {
                "instance_id": INSTANCE_ID,
                "load_percent": load_gen.current_load,
                "uptime_seconds": int(time.time() - START_TIME),
            })

        elif self.path == "/metrics":
            self._send_json(200, {
                "load_percent": load_gen.current_load,
                "instance_id": INSTANCE_ID,
            })

        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        """Permite ajustar la carga manualmente para pruebas."""
        if self.path == "/load/set":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            value = float(body.get("load_percent", load_gen.current_load))
            load_gen.set_load(value)
            self._send_json(200, {"load_percent": load_gen.current_load})
        else:
            self._send_json(404, {"error": "not found"})


# ── Entry point ───────────────────────────────────────────────────────────────
START_TIME = time.time()


def main():
    # Inicia la simulación de carga en background
    load_gen.start()
    logger.info("LoadGenerator iniciado")

    server = HTTPServer(("0.0.0.0", APP_PORT), AppHandler)
    logger.info("AppInstance escuchando en puerto %d (instance_id=%s)", APP_PORT, INSTANCE_ID)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("AppInstance detenida")
    finally:
        load_gen.stop()
        server.server_close()


if __name__ == "__main__":
    main()
