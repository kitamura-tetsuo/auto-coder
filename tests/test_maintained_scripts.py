"""Regression tests for maintained scripts relocated out of the repository root."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROXY_SCRIPT = REPOSITORY_ROOT / "scripts" / "tcp_proxy.py"


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_tcp_proxy_forwards_bytes_and_requires_three_arguments():
    """The relocated command preserves its CLI and bidirectional forwarding."""
    invalid = subprocess.run(
        [sys.executable, str(PROXY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert invalid.returncode == 1
    assert invalid.stdout == "Usage: tcp_proxy.py <listen_port> <target_host> <target_port>\n"

    target_port = _unused_port()
    listen_port = _unused_port()
    expected = b"relocated proxy round trip\x00\xff"
    server_ready = threading.Event()

    def echo_once() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", target_port))
            server.listen(1)
            server_ready.set()
            connection, _ = server.accept()
            with connection:
                connection.sendall(connection.recv(4096))

    server_thread = threading.Thread(target=echo_once, daemon=True)
    server_thread.start()
    assert server_ready.wait(timeout=2)

    proxy = subprocess.Popen(
        [sys.executable, str(PROXY_SCRIPT), str(listen_port), "127.0.0.1", str(target_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", listen_port), timeout=1)
                break
            except ConnectionRefusedError:
                assert proxy.poll() is None
                if time.monotonic() >= deadline:
                    raise AssertionError("TCP proxy did not begin listening")
                time.sleep(0.05)

        with client:
            client.sendall(expected)
            assert client.recv(4096) == expected
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()
    finally:
        proxy.terminate()
        proxy.wait(timeout=5)


def test_maintained_script_callers_use_relocated_paths():
    """All maintained invocation surfaces point at scripts, not root copies."""
    expected_references = {
        "Makefile": "python scripts/comprehensive_type_checker.py",
        ".github/workflows/type-check.yml.disabled": "python scripts/comprehensive_type_checker.py",
        "start_proxy.sh": "/home/node/src/auto-coder/scripts/tcp_proxy.py",
        "monitor_proxy.sh": "/home/node/src/auto-coder/scripts/tcp_proxy.py",
        "ACCESS_GUIDE.md": "/home/node/src/auto-coder/scripts/tcp_proxy.py",
    }

    for relative_path, invocation in expected_references.items():
        assert invocation in (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")

    assert not list(REPOSITORY_ROOT.glob("*.py"))
    assert PROXY_SCRIPT.is_file()
    assert (REPOSITORY_ROOT / "scripts" / "comprehensive_type_checker.py").is_file()


def test_make_type_check_launches_relocated_checker(tmp_path):
    """The public make target reaches the relocated checker through its real recipe."""
    invocation_file = tmp_path / "python-invocation"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {invocation_file}\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{tmp_path}:{environment['PATH']}"

    result = subprocess.run(
        ["make", "type-check"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert invocation_file.read_text(encoding="utf-8") == "scripts/comprehensive_type_checker.py\n"
