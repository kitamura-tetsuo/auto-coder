"""Read-only TLS GitHub boundary for isolated production-worker acceptance.

The real installed worker performs its normal HTTP requests and startup
reconciliation. The fixture repository is intentionally empty: all unknown
paths and all write methods are refused, never interpreted as success.
"""

from __future__ import annotations

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class FixtureGitHub(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        target = urlsplit(self.path)
        if target.path not in {"/repos/fixture/beta/pulls", "/repos/fixture/beta/issues"}:
            self._send(404, {"message": "fixture endpoint not available"})
            return
        self._send(200, [])

    def _send(self, status: int, value: object) -> None:
        encoded = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        print("fixture GitHub: " + (format % args), flush=True)


def main() -> None:
    trust = Path("/ci-trust")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(trust / "github.crt"), str(trust / "github.key"))
    server = ThreadingHTTPServer(("0.0.0.0", 443), FixtureGitHub)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    print("fixture GitHub HTTPS ready", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
