"""Minimal HTTP process that intentionally starts listening after a delay."""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTUP_DELAY_SECONDS = int(os.getenv("STARTUP_DELAY_SECONDS", "40"))
PORT = int(os.getenv("PORT", "8080"))

_ready = False


def _log(message: str) -> None:
    """Emit an RFC 3339 timestamped line that the Evidence extractor parses."""

    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(f"{stamp} {message}", flush=True)


def _terminate(signum: int, frame: object) -> None:
    """Report a kill that arrives before startup completed, then exit at once.

    Exiting directly is required: re-raising the signal from inside the handler
    only queues a pending signal, which lets the process survive until the
    kubelet's grace period expires and reaches readiness meanwhile.
    """

    if not _ready:
        _log("process terminated before application ready")
    os._exit(128 + signum)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(404)
            return

        payload = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        _log(format % args)


def main() -> None:
    global _ready

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    _log(
        "application boot started; configured_startup_delay_seconds="
        f"{STARTUP_DELAY_SECONDS}"
    )
    time.sleep(STARTUP_DELAY_SECONDS)
    _ready = True
    _log(f"application ready; listening_on={PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
