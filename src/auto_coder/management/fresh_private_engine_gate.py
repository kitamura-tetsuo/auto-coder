"""Transient trusted-host Docker API gate for an original NEW child launch.

The private Engine is created independently before this gate is entered. Its
original socket is sealed at the OS permission boundary, while a separately
preconnected local HTTP path permits observations and precisely one start from
the verified host guardian PID. No management child receives Docker custody.
"""

from __future__ import annotations

import http.client
import http.server
import os
import re
import socket
import socketserver
import stat
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .fresh_host_observation import HostObservationUnavailable

_SELECTED_START = re.compile(r"/v[0-9]+(?:\.[0-9]+)?/containers/[0-9a-f]{64}/start\Z")
_MAX_RESPONSE = 4 * 1024 * 1024


def _assert_only_original_engine_connection(socket_path: Path, daemon_net_pid: int) -> None:
    """Fail closed on preconnected clients in the original daemon netns.

    Sealing the pathname prevents ordinary *new* connects, but does not
    revoke a client that connected before chmod. Linux exposes both accepted
    and not-yet-accepted AF_UNIX stream connections under the daemon's network
    namespace. The guardian's warmed connection must be the only one left.
    """
    if type(daemon_net_pid) is not int or daemon_net_pid <= 0:
        raise HostObservationUnavailable("NEW private Engine daemon namespace PID is unavailable")
    try:
        entries = (Path("/proc") / str(daemon_net_pid) / "net/unix").read_text().splitlines()
    except OSError as error:
        raise HostObservationUnavailable("NEW private Engine Unix connection evidence is unavailable") from error
    matching: list[tuple[str, str, str]] = []
    for entry in entries[1:]:
        columns = entry.split(maxsplit=7)
        if len(columns) == 8 and columns[7] == str(socket_path):
            matching.append((columns[3], columns[4], columns[5]))
    # The listening socket is state 01 with the accept flag; the one warmed
    # guardian channel has state 03. A pending connect is state 02 and must
    # also poison admission, even if dockerd has not accepted it yet.
    if sorted(matching) != [("00000000", "0001", "03"), ("00010000", "0001", "01")]:
        raise HostObservationUnavailable("NEW private Engine has another preconnected or unverified API client")


class _EngineConnection(http.client.HTTPConnection):
    def __init__(self, address: Path) -> None:
        super().__init__("localhost", timeout=20)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(str(self.address))
        except OSError:
            self.close()
            raise


class _SourceGate(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address: Path, engine: _EngineConnection, child: str, host_pid: int) -> None:
        self.engine = engine
        self.child = child
        self.host_pid = host_pid
        self.effect_claimed = False
        self.engine_lock = threading.Lock()
        super().__init__(str(address), _GateRequestHandler)


class _GateRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        # The trusted host logs refusals without exposing Docker API payloads.
        return

    def _reply(self, status: int, body: bytes, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        for name, value in headers:
            if name.lower() not in {"content-length", "transfer-encoding", "connection", "server", "date"}:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._reply(403, b"Docker mutation excluded by first-launch gate")

    def do_DELETE(self) -> None:
        self._reply(403, b"Docker mutation excluded by first-launch gate")

    def do_PATCH(self) -> None:
        self._reply(403, b"Docker mutation excluded by first-launch gate")

    def _forward(self) -> None:
        server: _SourceGate = self.server  # type: ignore[assignment]
        path = self.path
        if not path.startswith("/") or "//" in path or ".." in path or len(path) > 2048:
            self._reply(403, b"Docker API route excluded by first-launch gate")
            return
        start = self.command == "POST" and path == f"/v1.41/containers/{server.child}/start" and _SELECTED_START.fullmatch(path) is not None
        if self.command not in {"GET", "HEAD"} and not start:
            self._reply(403, b"Docker mutation excluded by first-launch gate")
            return
        if start:
            pid, uid, _gid = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if (pid, uid) != (server.host_pid, os.geteuid()):
                self._reply(403, b"Docker start requires the original guardian process")
                return
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Content-Length") not in (None, "0"):
            self._reply(403, b"Docker API body excluded by first-launch gate")
            return
        with server.engine_lock:
            if start:
                if server.effect_claimed:
                    self._reply(409, b"Docker first-start effect already claimed")
                    return
                # Consume before forwarding: a lost reply is never retried.
                server.effect_claimed = True
            try:
                server.engine.request(self.command, path)
                response = server.engine.getresponse()
                body = response.read(_MAX_RESPONSE + 1)
                if len(body) > _MAX_RESPONSE:
                    raise HostObservationUnavailable("NEW Docker read-only gate response exceeds its bound")
                self._reply(response.status, body, tuple(response.getheaders()))
            except (OSError, ValueError, http.client.HTTPException, HostObservationUnavailable):
                self._reply(503, b"Docker original Engine connection unavailable")


@contextmanager
def hold_one_start_api_gate(private_root: Path, container_id: str, *, daemon_net_pid: int) -> Iterator[Path]:
    """Exclude independent Docker starts until the original child is observed.

    Connections to the actual Engine are opened before its socket is sealed.
    Only the host process is allowed to send the selected start through the
    read-only gate. The caller must already hold the verified private source
    and original admission fences. A process death leaves a poisoned claim.
    """
    original = private_root / "socket" / "docker.sock"
    gate_path = private_root / "socket" / "first-start-gate.sock"
    info = os.stat(original, follow_symlinks=False)
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid() or info.st_gid != os.getegid() or mode != 0o600:
        raise HostObservationUnavailable("NEW private Engine original API socket is not exclusively host-owned")
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("NEW original start requires an immutable container ID")
    if os.path.lexists(gate_path):
        raise HostObservationUnavailable("NEW Docker first-start API gate already exists")

    # Keep an already-authorized original Engine channel across chmod(000).
    # The Docker Engine must honor ordinary persistent local HTTP connections.
    engine = _EngineConnection(original)
    try:
        engine.connect()
        # Force dockerd to accept the guardian's channel before enumerating
        # accepted and pending clients. HTTP/1.1 keep-alive is mandatory here.
        try:
            engine.request("GET", "/v1.41/info")
            warm = engine.getresponse()
            body = warm.read(_MAX_RESPONSE + 1)
            if warm.status != 200 or len(body) > _MAX_RESPONSE or engine.sock is None:
                raise HostObservationUnavailable("NEW private Engine cannot retain its original API channel")
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise HostObservationUnavailable("NEW private Engine original API channel is unavailable") from error
        with _SourceGate(gate_path, engine, container_id, os.getpid()) as server:
            os.chmod(gate_path, 0o600, follow_symlinks=False)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            sealed = False
            try:
                os.chmod(original, 0o000, follow_symlinks=False)
                sealed = True
                current = os.stat(original, follow_symlinks=False)
                if (current.st_dev, current.st_ino, stat.S_IMODE(current.st_mode)) != (info.st_dev, info.st_ino, 0):
                    raise HostObservationUnavailable("NEW Docker original API socket was not continuously sealed")
                _assert_only_original_engine_connection(original, daemon_net_pid)
                yield gate_path
                current = os.stat(original, follow_symlinks=False)
                if (current.st_dev, current.st_ino, stat.S_IMODE(current.st_mode)) != (info.st_dev, info.st_ino, 0):
                    raise HostObservationUnavailable("NEW Docker original API source changed during guarded launch")
                if not server.effect_claimed:
                    raise HostObservationUnavailable("NEW Docker guarded first-start effect was never attempted")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                if sealed:
                    current = os.stat(original, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                        os.chmod(original, mode, follow_symlinks=False)
                gate_info = os.stat(gate_path, follow_symlinks=False)
                if stat.S_ISSOCK(gate_info.st_mode):
                    gate_path.unlink()
    finally:
        engine.close()
