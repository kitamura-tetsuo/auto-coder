"""Bounded, read-only requests to the Codex app-server stdio API."""

import json
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Mapping

from . import __version__


def read_account_data(method: str, timeout: float = 15.0) -> Mapping[str, object]:
    """Initialize a short-lived server and read account data without starting a turn.

    Codex owns credential storage and refresh. Never log server output: errors
    and notifications can contain account information or credentials.
    """
    if method not in {"account/read", "account/rateLimits/read"}:
        raise ValueError("Unsupported account read method")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    messages: Queue[str | Exception] = Queue()
    assert process.stdout is not None
    assert process.stdin is not None
    stdout = process.stdout
    stdin = process.stdin

    def read_stdout() -> None:
        try:
            while True:
                line = stdout.readline(1_048_577)
                if not line:
                    raise EOFError("Codex app-server closed stdout")
                if len(line) > 1_048_576:
                    raise ValueError("Codex app-server response is too large")
                messages.put(line)
        except Exception as error:
            messages.put(error)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()

    def send(message: Mapping[str, object]) -> None:
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def receive(request_id: int) -> Mapping[str, object]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server request timed out")
            try:
                line = messages.get(timeout=remaining)
            except Empty:
                raise TimeoutError("Codex app-server request timed out") from None
            if isinstance(line, Exception):
                raise line
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Invalid Codex app-server message")
            if message.get("id") != request_id:
                continue
            if "error" in message or not isinstance(message.get("result"), dict):
                raise ValueError("Codex app-server request failed")
            return message["result"]

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "auto_coder", "version": __version__}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": method})
        return receive(2)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=1.0)
        process.stdin.close()
        process.stdout.close()
