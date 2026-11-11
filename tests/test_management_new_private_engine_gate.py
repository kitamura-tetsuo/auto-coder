"""Real Unix socket custody and one-shot Docker API gate regressions."""

from __future__ import annotations

import http.client
import http.server
import os
import socket
import socketserver
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from auto_coder.management.fresh_private_engine_gate import hold_one_start_api_gate
from auto_coder.management.fresh_new_host_start import _LocalDockerConnection

SELECTED = "c" * 64


class _OriginalEngine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self._answer()

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def _answer(self) -> None:
        server = self.server
        server.effects.append((self.command, self.path))  # type: ignore[attr-defined]
        status = 204 if self.command == "POST" else 200
        data = b"" if status == 204 else b'{"ID":"new-engine"}'
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class _UnixEngine(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def _api(socket_path: Path, method: str, path: str) -> tuple[int, bytes]:
    connection = _LocalDockerConnection(socket_path)
    try:
        connection.request(method, path)
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()


def test_original_socket_sealed_and_only_original_process_can_start(tmp_path: Path) -> None:
    root = tmp_path / "private-source"
    socket_dir = root / "socket"
    socket_dir.mkdir(parents=True, mode=0o700)
    raw = socket_dir / "docker.sock"
    with _UnixEngine(str(raw), _OriginalEngine) as engine:
        engine.effects = []
        original_thread = threading.Thread(target=engine.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        original_thread.start()
        os.chmod(raw, 0o600)
        try:
            with hold_one_start_api_gate(root, SELECTED) as gate:
                assert stat.S_IMODE(raw.stat().st_mode) == 0
                assert stat.S_IMODE(gate.stat().st_mode) == 0o600
                assert _api(gate, "GET", "/v1.41/info")[0] == 200
                assert _api(gate, "HEAD", "/_ping")[0] == 200
                assert _api(gate, "POST", "/v1.41/containers/" + "d" * 64 + "/start")[0] == 403
                assert _api(gate, "POST", "/v1.41/containers/create")[0] == 403
                assert _api(gate, "DELETE", "/v1.41/containers/" + SELECTED)[0] == 403
                assert _api(gate, "PUT", "/v1.41/containers/" + SELECTED + "/update")[0] == 403
                outsider = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import http.client,socket,sys;"
                            "s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
                            "s.sendall(('POST /v1.41/containers/'+sys.argv[2]+'/start HTTP/1.1\\r\\n'"
                            "+'Host: localhost\\r\\nContent-Length: 0\\r\\n\\r\\n').encode());"
                            "print(s.recv(256).split(b'\\r\\n')[0].decode())"
                        ),
                        str(gate),
                        SELECTED,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert outsider.returncode == 0, outsider.stderr
                assert "403" in outsider.stdout
                denied_raw = subprocess.run(
                    [sys.executable, "-c", "import socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1])", str(raw)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                assert denied_raw.returncode != 0 and "PermissionError" in denied_raw.stderr
                assert _api(gate, "POST", "/v1.41/containers/" + SELECTED + "/start")[0] == 204
                assert _api(gate, "POST", "/v1.41/containers/" + SELECTED + "/start")[0] == 409
                assert engine.effects == [
                    ("GET", "/v1.41/info"),
                    ("HEAD", "/_ping"),
                    ("POST", "/v1.41/containers/" + SELECTED + "/start"),
                ]
            assert not gate.exists()
            assert stat.S_IMODE(raw.stat().st_mode) == 0o600
            assert _api(raw, "GET", "/v1.41/info")[0] == 200
        finally:
            engine.shutdown()
            original_thread.join(timeout=5)


def test_unattempted_start_unseals_and_refuses_success(tmp_path: Path) -> None:
    socket_dir = tmp_path / "socket"
    socket_dir.mkdir(mode=0o700)
    raw = socket_dir / "docker.sock"
    with _UnixEngine(str(raw), _OriginalEngine) as engine:
        engine.effects = []
        original_thread = threading.Thread(target=engine.serve_forever, daemon=True)
        original_thread.start()
        os.chmod(raw, 0o600)
        try:
            with pytest.raises(Exception, match="first-start effect was never attempted"):
                with hold_one_start_api_gate(tmp_path, SELECTED) as gate:
                    assert _api(gate, "GET", "/v1.41/info")[0] == 200
            assert stat.S_IMODE(raw.stat().st_mode) == 0o600
            assert not (socket_dir / "first-start-gate.sock").exists()
            assert engine.effects == [("GET", "/v1.41/info")]
        finally:
            engine.shutdown()
            original_thread.join(timeout=5)
