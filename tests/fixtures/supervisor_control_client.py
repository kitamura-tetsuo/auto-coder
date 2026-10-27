"""Send one typed Supervisor request from inside its isolated container."""

from __future__ import annotations

import json
import socket
import sys


def main() -> None:
    request = json.loads(sys.argv[1])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect("/runtime/supervisor/control.sock")
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        if "--drop-response" in sys.argv[2:]:
            return
        with client.makefile("rb") as response:
            value = json.loads(response.readline())
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
