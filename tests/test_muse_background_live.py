"""Real released-host conformance for Muse background observations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

import pytest

from auto_coder.muse_background_observation import (
    MuseContinuationReadiness,
    MuseObservationBinding,
    MuseObservationFold,
    MuseRuntimeConformanceEvidence,
    MuseTaskOutcome,
    decode_muse_msp_frame,
    detect_muse_background_support,
)

pytestmark = [pytest.mark.muse_live, pytest.mark.timeout(120)]

_PROFILE = "Muse Code 1.4.6 (1.4.6-R6000.1)"


class _ProviderState:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.changed = threading.Condition()


def _sse(value: object) -> bytes:
    return f"data: {json.dumps(value)}\n\n".encode()


def _provider_handler(state: _ProviderState, command: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_GET(self) -> None:
            body = {
                "object": "list",
                "data": [
                    {
                        "id": "auto-coder-fixture",
                        "object": "model",
                        "metadata": {
                            "muse-code": {
                                "name": "Auto-Coder conformance fixture",
                                "release_date": "2026-01-01",
                                "is_hidden": False,
                                "tool_call": True,
                                "reasoning": True,
                                "limit": {"context": 1_000_000, "output": 4096},
                            }
                        },
                    }
                ],
            }
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length))
                assert isinstance(body, dict)
                with state.changed:
                    state.requests.append(body)
                    state.changed.notify_all()
                inputs = body.get("input")
                encoded_inputs = json.dumps(inputs)
                delivered = "function_call_output" in encoded_inputs and "call_background" in encoded_inputs
                already_called = "call_background" in encoded_inputs
                if not already_called:
                    content = {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": 2,
                        "output_index": 0,
                        "item_id": "fc_call_background",
                        "name": "bash",
                        "call_id": "call_background",
                        "arguments": json.dumps({"command": command, "description": "Wait for the conformance gate"}),
                    }
                    response_id = "response-tool"
                else:
                    text = "result incorporated" if delivered else "foreground complete"
                    content = {
                        "type": "response.output_text.delta",
                        "sequence_number": 2,
                        "output_index": 0,
                        "item_id": f"message-{len(state.requests)}",
                        "content_index": 0,
                        "delta": text,
                    }
                    response_id = f"response-{len(state.requests)}"
                frames = (
                    {
                        "type": "response.created",
                        "sequence_number": 1,
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "model": "auto-coder-fixture",
                            "status": "in_progress",
                            "output": [],
                        },
                    },
                    content,
                    {
                        "type": "response.completed",
                        "sequence_number": 3,
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "model": "auto-coder-fixture",
                            "status": "completed",
                            "output": [],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        },
                    },
                )
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"".join(_sse(frame) for frame in frames))
            except BaseException as exc:
                with state.changed:
                    state.errors.append(str(exc))
                    state.changed.notify_all()
                self.send_response(500)
                self.end_headers()

    return Handler


class _MspHost:
    def __init__(self, binary: str, workspace: Path, env: dict[str, str]) -> None:
        self.frames: list[dict[str, object]] = []
        self.changed = threading.Condition()
        self.process = subprocess.Popen(
            [binary, "serve", "--provider", "meta", "--model", "auto-coder-fixture", "--disable-sandbox"],
            cwd=workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            frame = decode_muse_msp_frame(raw.rstrip(b"\n"))
            with self.changed:
                self.frames.append(frame)
                self.changed.notify_all()

    def send(self, request_id: Optional[int], method: str, params: dict[str, object]) -> None:
        frame: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            frame["id"] = request_id
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(frame).encode() + b"\n")
        self.process.stdin.flush()

    def wait(self, predicate: Callable[[dict[str, object]], bool]) -> dict[str, object]:
        deadline = time.monotonic() + 60
        with self.changed:
            while True:
                match = next((frame for frame in self.frames if predicate(frame)), None)
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stderr = self.process.stderr.read().decode(errors="replace") if self.process.poll() is not None and self.process.stderr else ""
                    raise AssertionError(f"real Muse host observation timed out: {stderr}")
                self.changed.wait(remaining)

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        self.process.wait(timeout=30)
        assert self.process.returncode == 0


def _result(frame: dict[str, object]) -> dict[str, object]:
    result = frame.get("result")
    assert isinstance(result, dict)
    return result


def _params(frame: dict[str, object]) -> dict[str, object]:
    params = frame.get("params")
    assert isinstance(params, dict)
    return params


def test_released_muse_host_background_result_and_native_continuation(tmp_path: Path) -> None:
    binary = os.environ.get("AUTOCODER_MUSE_CONFORMANCE_BIN") or shutil.which("muse")
    if binary is None:
        pytest.skip("real Muse host is installed by the dedicated Muse live workflow")
    version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True).stdout.strip()
    assert version == _PROFILE

    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    gate = workspace / "release-gate"
    workspace.mkdir()
    config = home / "config" / "muse"
    config.mkdir(parents=True)
    state = _ProviderState()
    command = f"while [ ! -f {gate} ]; do sleep 0.05; done; echo MUSE_BACKGROUND_RESULT"
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _provider_handler(state, command))
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    base_url = f"http://127.0.0.1:{provider.server_address[1]}"
    (config / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "endpoint_transport": {"base_url": base_url, "auth": "bearer"},
                "run": {"reminder_roster": {"agents": []}},
            }
        )
    )
    (config / "auth.json").write_text(json.dumps({"schema_version": 1, "providers": {"meta": {"api_key": "fixture-only"}}}))
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "META_API_KEY": "fixture-only",
        "MUSE_EXPERIMENTAL_SDK_ENABLED": "on",
        "MUSE_NO_AUTO_UPDATE": "1",
        "TBH_CREDENTIAL_BACKEND": "file",
        "TBH_DISABLE_FEATURE_CONFIG": "1",
        "TBH_DISABLE_TELEMETRY": "1",
        "TBH_IS_E2E_TEST": "1",
    }
    host = _MspHost(binary, workspace, env)
    try:
        host.send(1, "initialize", {"clientInfo": {"name": "auto_coder_conformance", "version": "1"}})
        initialized = _result(host.wait(lambda frame: frame.get("id") == 1))
        assert initialized["serverInfo"] == {"name": "muse", "version": "1.4.6"}
        host.send(None, "initialized", {})
        host.send(
            2,
            "session/start",
            {
                "commandId": "0198f0aa-1111-7000-8000-000000000001",
                "workspaceRoot": str(workspace),
                "providerId": "meta",
                "modelId": "auto-coder-fixture",
                "approvalMode": "allowAll",
            },
        )
        opened = _result(host.wait(lambda frame: frame.get("id") == 2))
        session = opened["session"]
        assert isinstance(session, dict)
        session_id = session["sessionId"]
        assert isinstance(session_id, str)
        host.send(
            3,
            "turn/start",
            {
                "commandId": "0198f0aa-1111-7000-8000-000000000002",
                "sessionId": session_id,
                "input": [{"type": "text", "text": "Run the conformance command."}],
            },
        )
        admitted = _result(host.wait(lambda frame: frame.get("id") == 3))
        foreground_turn = admitted["turnId"]
        assert isinstance(foreground_turn, str)
        started = host.wait(lambda frame: frame.get("method") == "item/started" and isinstance(_params(frame).get("item"), dict) and _params(frame)["item"].get("kind") == "toolCall")  # type: ignore[union-attr]
        item = _params(started)["item"]
        assert isinstance(item, dict)
        task_id = item["itemId"]
        assert isinstance(task_id, str)
        host.send(
            4,
            "task/background",
            {
                "commandId": "0198f0aa-1111-7000-8000-000000000003",
                "sessionId": session_id,
                "taskId": task_id,
            },
        )
        background_ack = _result(host.wait(lambda frame: frame.get("id") == 4))
        assert background_ack == {
            "commandId": "0198f0aa-1111-7000-8000-000000000003",
            "status": "accepted",
            "taskId": task_id,
        }
        host.send(5, "task/list", {"sessionId": session_id})
        live_tasks = _result(host.wait(lambda frame: frame.get("id") == 5))["tasks"]
        assert live_tasks == [
            {
                "backgroundInitiator": "user",
                "kind": "tool",
                "status": "inProgress",
                "taskId": task_id,
                "tool": "bash",
            }
        ]
        foreground_terminal = host.wait(lambda frame: frame.get("method") == "turn/completed" and _params(frame).get("turnId") == foreground_turn)
        assert _params(foreground_terminal)["terminal"] == "completed"
        gate.write_text("release")
        terminal = host.wait(lambda frame: frame.get("method") == "item/completed" and isinstance(_params(frame).get("item"), dict) and _params(frame)["item"].get("itemId") == task_id)  # type: ignore[union-attr]
        terminal_item = _params(terminal)["item"]
        assert isinstance(terminal_item, dict)
        assert terminal_item["status"] == "completed"
        assert "MUSE_BACKGROUND_RESULT" in str(terminal_item["visibleOutput"])
        native_started = host.wait(lambda frame: frame.get("method") == "turn/started" and _params(frame).get("turnId") != foreground_turn)
        native_turn = _params(native_started)["turnId"]
        assert isinstance(native_turn, str)
        native_terminal = host.wait(lambda frame: frame.get("method") == "turn/completed" and _params(frame).get("turnId") == native_turn)
        assert _params(native_terminal)["terminal"] == "completed"
        with state.changed:
            assert state.changed.wait_for(
                lambda: any("function_call_output" in json.dumps(request.get("input")) and "MUSE_BACKGROUND_RESULT" in json.dumps(request.get("input")) for request in state.requests),
                timeout=30,
            )
        host.send(6, "session/read", {"sessionId": session_id, "excludeItems": False})
        replay = _result(host.wait(lambda frame: frame.get("id") == 6))
        assert replay["viewCursor"]
        history = replay["history"]
        assert isinstance(history, dict) and history["mode"] == "inline"

        evidence = MuseRuntimeConformanceEvidence(True, True, True, "native")
        support = detect_muse_background_support(initialized, evidence)
        assert support.supported and support.native_wake is True
        fold = MuseObservationFold(
            MuseObservationBinding("muse", session_id, "live-execution", "live-attempt", workspace),
            foreground_turn,
            support,
        )
        for frame in host.frames:
            if "method" in frame:
                fold.consume(frame)
        fold.consume_task_inventory(live_tasks, "task/list:5")
        result_reference = f"{task_id}@{terminal_item['revision']}"
        fold.consume_result_delivery(native_turn, [result_reference], str(replay["viewCursor"]))
        observation = fold.observation()
        # This released profile exposes transport completion and output, but no
        # authoritative command exit discriminator in the MSP item.
        assert observation.tasks[0].outcome is MuseTaskOutcome.UNKNOWN
        assert observation.tasks[0].result_delivered
        assert observation.incorporated_results == (result_reference,)
        assert observation.readiness is MuseContinuationReadiness.NOT_READY
        assert observation.reconciliation_reason == "native runtime owns continuation"
        assert not state.errors
    finally:
        host.close()
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
