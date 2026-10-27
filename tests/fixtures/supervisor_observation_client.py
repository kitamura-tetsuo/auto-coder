"""Fetch a fresh, nonce-bound observation from the real worker Unix socket."""

from __future__ import annotations

import http.client
import socket
import sys


class WorkerConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/runtime/management.sock")


def main() -> None:
    connection = WorkerConnection("localhost", timeout=4)
    try:
        connection.request("GET", "/v1/observation?nonce=" + sys.argv[1])
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"worker observation returned HTTP {response.status}")
        print(response.read().decode())
    finally:
        connection.close()


if __name__ == "__main__":
    main()
