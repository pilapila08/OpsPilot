"""Minimal HTTP process that intentionally starts listening after a delay."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTUP_DELAY_SECONDS = int(os.getenv("STARTUP_DELAY_SECONDS", "40"))
PORT = int(os.getenv("PORT", "8080"))


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
        print(format % args, flush=True)


def main() -> None:
    print(
        f"application boot started; configured_startup_delay_seconds="
        f"{STARTUP_DELAY_SECONDS}",
        flush=True,
    )
    time.sleep(STARTUP_DELAY_SECONDS)
    print(f"application ready; listening_on={PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    main()
