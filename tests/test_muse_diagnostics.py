"""Production-path regressions for bounded Muse MSP invocation diagnostics."""

from __future__ import annotations

import io
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

from src.auto_coder import muse_diagnostics
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.logger_config import setup_logger
from src.auto_coder.muse_diagnostics import MARKER, MuseInvocationObserver, TimedFrame, display
from tests.test_muse_msp import _host, _manager, _repository

_GATED_PEER = r"""#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["--version"]:
    print("Muse Code 9.9.9")
    raise SystemExit
gate = Path(os.environ["GATE"])
mode = os.environ.get("GATE_MODE", "silent")
def emit(value):
    print(json.dumps(value), flush=True)
def wait_gate():
    while not gate.exists():
        time.sleep(0.02)
for line in sys.stdin:
    frame = json.loads(line)
    method = frame.get("method")
    if method == "initialize":
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"serverInfo":{"name":"fixture","version":"7.7"},"schema":{"version":1,"fingerprint":"sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f"}}})
    elif method == "session/start":
        session = {"sessionId":"gated/session","workspaceRoot":os.getcwd(),"modelId":"muse-spark-1.3","approvalMode":{"mode":frame["params"]["approvalMode"]}}
        emit({"jsonrpc":"2.0","id":frame["id"],"result":{"session":session,"pendingRequests":[]}})
        if mode == "stopread":
            wait_gate()
    elif method == "turn/start":
        Path(os.environ["SEEN"]).write_text("read")
        turn = "turn-1"
        ack = {"commandId":frame["params"]["commandId"],"status":"accepted","turnId":turn,"startedNewTurn":True,"disposition":"started"}
        encoded = json.dumps({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        if mode == "stderr":
            while not gate.exists():
                sys.stderr.write("SECRET-STDERR-SENTINEL\n"); sys.stderr.flush(); time.sleep(0.05)
        elif mode == "partial":
            sys.stdout.write(encoded[:20]); sys.stdout.flush()
            wait_gate()
            sys.stdout.write(encoded[20:] + "\n"); sys.stdout.flush()
            ack = None
        elif mode == "busy":
            while not gate.exists():
                emit({"jsonrpc":"2.0","method":"host/noise","params":{"n":time.time()}}); time.sleep(0.02)
        elif mode == "silent":
            wait_gate()
        if ack is not None:
            emit({"jsonrpc":"2.0","id":frame["id"],"result":ack})
        emit({"jsonrpc":"2.0","method":"item/completed","params":{"sessionId":"gated/session","item":{"itemId":"a","kind":"agentMessage","status":"completed","turnId":turn,"text":"gated-answer"}}})
        emit({"jsonrpc":"2.0","method":"turn/completed","params":{"sessionId":"gated/session","turnId":turn,"terminal":"completed"}})
        if mode == "hold-exit":
            wait_gate()
"""


@pytest.fixture
def sinks(tmp_path):
    console = io.StringIO()
    log_file = tmp_path / "app.log"
    setup_logger(log_level="INFO", log_file=str(log_file), stream=console)
    yield console, log_file
    setup_logger(log_level="INFO")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _records(text: str) -> list[dict]:
    complete = text if text.endswith("\n") else text[: text.rfind("\n") + 1]  # a concurrent writer may be mid-line
    return [json.loads(_ANSI.sub("", line).split(MARKER, 1)[1]) for line in complete.splitlines() if MARKER in line]


def _gated(tmp_path: Path, monkeypatch, mode: str, *, timeout: Optional[int] = None, interval: float = 0.2, backend: str = "muse"):
    repo = _repository(tmp_path)
    peer = tmp_path / "gated-muse"
    peer.write_text(_GATED_PEER)
    peer.chmod(0o700)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(peer))
    monkeypatch.setenv("GATE", str(tmp_path / "gate"))
    monkeypatch.setenv("SEEN", str(tmp_path / "seen"))
    monkeypatch.setenv("GATE_MODE", mode)
    monkeypatch.setattr(muse_diagnostics, "HEARTBEAT_INTERVAL_SECONDS", interval)
    config = LLMBackendConfiguration(backends={backend: BackendConfig(name=backend, backend_type="muse", model="muse-spark-1.3", timeout=timeout)})
    return _manager(config, backend), tmp_path / "gate", tmp_path / "seen"


class _Call:
    def __init__(self, manager, prompt: str = "first", **kwargs) -> None:
        self.result: Optional[str] = None
        self.error: Optional[BaseException] = None

        def run() -> None:
            try:
                self.result = manager._run_llm_cli(prompt, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def join(self, timeout: float = 20) -> None:
        self.thread.join(timeout)
        assert not self.thread.is_alive()


def _wait_for(condition: Callable[[], object], timeout: float = 15) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _heartbeats(log_file: Path) -> list[dict]:
    return [record for record in _records(log_file.read_text()) if record["kind"] == "heartbeat"]


@pytest.mark.parametrize("backend", ["muse", "named-muse"])
@pytest.mark.parametrize("is_noedit", [False, True])
def test_both_sinks_receive_one_ordered_timeline_for_returned_call(tmp_path, monkeypatch, _use_real_commands, sinks, backend, is_noedit):
    console, log_file = sinks
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    config = LLMBackendConfiguration(backends={backend: BackendConfig(name=backend, backend_type="muse", model="muse-spark-1.3")})
    manager = _manager(config, backend)

    assert manager._run_llm_cli("first", is_noedit=is_noedit) == "answer:first"

    for text in (console.getvalue(), log_file.read_text()):
        records = _records(text)
        assert len({record["diagnostic_id"] for record in records}) == 1
        assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
        assert [record["kind"] for record in records].count("start") == 1
        assert [record["kind"] for record in records].count("end") == 1
        assert records[-1]["kind"] == "end" and records[-1]["summary"]["outcome"] == "returned"
        phases = [record["phase"] for record in records if record["kind"] == "phase"]
        assert phases[:4] == ["preparation", "host_startup", "initialization", "session_start_resume"]
        assert "turn_terminal_wait" in phases and phases[-1] in {"writer_settlement", "result_validation"}
        assert records[-1]["context"]["backend_alias"] == backend
        assert records[-1]["context"]["effective_mode"] == ("no-edit" if is_noedit else "edit")
        assert records[-1]["context"]["cli_version"] == "Muse Code 1.3.0"
        assert records[-1]["identity"]["confirmed_session"] == "opaque/provider/session"
        assert records[-1]["host"]["writers"] == "confirmed"
        assert records[-1]["boundary"]["provider_transport"] == "unobserved"


def test_pending_snapshot_reaches_both_sinks_before_completion_and_names_wait(tmp_path, monkeypatch, _use_real_commands, sinks):
    console, log_file = sinks
    manager, gate, seen = _gated(tmp_path, monkeypatch, "silent")
    call = _Call(manager)
    _wait_for(lambda: seen.exists() and _heartbeats(log_file))

    snapshot = _heartbeats(log_file)[-1]
    assert _heartbeats_in(console.getvalue())
    assert snapshot["phase"] == "turn_submission"
    assert snapshot["wait"]["reason"] == "response_wait"
    assert snapshot["wait"]["request"]["method"] == "turn/start"
    assert snapshot["wait"]["request"]["write"] == "complete"
    assert snapshot["wait"]["request"]["response_received"] is False
    assert snapshot["identity"]["turn"] == "unobserved"
    assert snapshot["milestones"]["assistant_text"]["state"] == "unobserved"
    assert snapshot["budgets"]["execution_remaining_s"] > 0
    assert call.thread.is_alive()

    gate.write_text("go")
    call.join()
    assert call.result == "gated-answer"
    assert [record["kind"] for record in _records(log_file.read_text())].count("end") == 1


def _heartbeats_in(text: str) -> list[dict]:
    return [record for record in _records(text) if record["kind"] == "heartbeat"]


def test_stderr_only_does_not_refresh_stdout_or_frame_activity(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, gate, seen = _gated(tmp_path, monkeypatch, "stderr")
    call = _Call(manager)
    _wait_for(lambda: seen.exists() and len(_heartbeats(log_file)) >= 2)
    first, second = _heartbeats(log_file)[:2]

    assert second["activity"]["stderr"]["bytes"] > first["activity"]["stderr"]["bytes"] > 0
    assert second["activity"]["stdout"]["bytes"] == first["activity"]["stdout"]["bytes"]
    assert second["activity"]["frames"]["count"] == first["activity"]["frames"]["count"]
    assert second["activity"]["frames"]["age_s"] > first["activity"]["frames"]["age_s"]
    gate.write_text("go")
    call.join()
    assert call.result == "gated-answer"
    assert "SECRET-STDERR-SENTINEL" not in log_file.read_text()


def test_partial_stdout_counts_bytes_but_not_a_decoded_response_and_same_call_finishes(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, gate, seen = _gated(tmp_path, monkeypatch, "partial")
    call = _Call(manager)
    _wait_for(lambda: seen.exists() and _heartbeats(log_file))
    snapshot = _heartbeats(log_file)[-1]
    frames_before = snapshot["activity"]["frames"]["count"]

    assert snapshot["activity"]["stdout_buffered_bytes"] == 20
    assert snapshot["wait"]["request"]["response_received"] is False
    gate.write_text("go")
    call.join()
    assert call.result == "gated-answer"
    end = _records(log_file.read_text())[-1]
    assert end["activity"]["frames"]["count"] > frames_before
    assert end["activity"]["stdout_buffered_bytes"] == 0


def test_blocked_write_is_partial_and_not_an_accepted_turn(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, gate, _ = _gated(tmp_path, monkeypatch, "stopread")
    call = _Call(manager, "x" * (2 * 1024 * 1024))
    snapshot = _wait_for(lambda: next((h for h in _heartbeats(log_file) if h["wait"]["write"]["write"] == "partial"), None))

    assert snapshot["phase"] == "turn_submission" and snapshot["wait"]["reason"] == "pipe_write"
    assert 0 < snapshot["wait"]["write"]["bytes_written"] < snapshot["wait"]["write"]["total_bytes"]
    assert snapshot["wait"]["request"]["response_received"] is False
    assert snapshot["identity"]["turn"] == "unobserved"
    gate.write_text("go")
    call.join()
    assert call.result == "gated-answer"


def test_blocked_write_keeps_original_timeout(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, _, _ = _gated(tmp_path, monkeypatch, "stopread", timeout=2, interval=0.3)
    started = time.monotonic()
    with pytest.raises(AutoCoderTimeoutError):
        manager._run_llm_cli("x" * (2 * 1024 * 1024))
    assert time.monotonic() - started < 6
    end = _records(log_file.read_text())[-1]
    assert end["summary"]["outcome"] == "raised" and end["summary"]["exception_class"] == "AutoCoderTimeoutError"
    assert end["summary"]["primary_failure"]["phase"] == "turn_submission"
    assert end["summary"]["primary_failure"]["category"] == "timeout"
    assert len(_heartbeats(log_file)) >= 2


def test_busy_pipes_still_report_on_schedule_without_per_event_logging(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, gate, _ = _gated(tmp_path, monkeypatch, "busy", interval=0.3)
    call = _Call(manager)
    _wait_for(lambda: len(_heartbeats(log_file)) >= 2)
    beats = _heartbeats(log_file)
    assert beats[-1]["activity"]["frames"]["count"] > 10
    assert beats[-1]["activity"]["last_method"] == "other"
    assert beats[-1]["milestones"]["turn_terminal"]["state"] == "unobserved"
    gate.write_text("go")
    call.join()
    assert len(_records(log_file.read_text())) < 40


def test_post_terminal_host_exit_wait_is_reported_with_retained_milestones(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    manager, gate, _ = _gated(tmp_path, monkeypatch, "hold-exit", timeout=20)
    call = _Call(manager)
    snapshot = _wait_for(lambda: next((h for h in _heartbeats(log_file) if h["phase"] == "post_terminal_host_exit_wait"), None))

    assert snapshot["wait"]["reason"] == "host_exit_wait"
    assert snapshot["host"]["state"] == "running" and snapshot["host"]["stdin_closed"] is True
    assert snapshot["host"]["writers"] == "unobserved"
    assert snapshot["milestones"]["assistant_text"]["state"] == "observed"
    assert snapshot["milestones"]["turn_terminal"]["terminal"] == "completed"
    gate.write_text("go")
    call.join()
    assert call.result == "gated-answer"
    assert [record["kind"] for record in _records(log_file.read_text())].count("error") == 0


def test_events_before_ack_keep_original_decode_times(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_EVENTS_BEFORE_ACK", "1")
    manager = _manager(LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")}))

    assert manager._run_llm_cli("first") == "answer:first"
    milestones = _records(log_file.read_text())[-1]["milestones"]
    assert milestones["assistant_text"]["before_ack"] is True and milestones["assistant_text"]["confirmed"] is True
    assert milestones["turn_terminal"]["terminal"] == "completed"
    assert milestones["assistant_text"]["first_decoded"] <= milestones["turn_terminal"]["first_decoded"]


def test_quota_failure_keeps_primary_stage_and_rpc_code_and_ignores_contents(tmp_path, monkeypatch, _use_real_commands, sinks):
    console, log_file = sinks
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    monkeypatch.setenv("MSP_QUOTA_RESPONSE", "1")
    manager = _manager(LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", api_key="API-KEY-SENTINEL")}))

    with pytest.raises(AutoCoderUsageLimitError):
        manager._run_llm_cli("PROMPT-SENTINEL\nsecond line")

    end = _records(log_file.read_text())[-1]
    assert end["summary"]["outcome"] == "raised"
    assert end["summary"]["exception_class"] == "AutoCoderUsageLimitError"
    assert end["summary"]["primary_failure"] == {"phase": "turn_submission", "class": "AutoCoderUsageLimitError", "category": "usage_limit"}
    assert end["summary"]["rpc_error_code"] == 429
    for text in (console.getvalue(), log_file.read_text()):
        marker_lines = [line for line in text.splitlines() if MARKER in line]
        assert all(len(line.split(MARKER, 1)[1].encode()) <= 8192 for line in marker_lines)
        assert not any(secret in "\n".join(marker_lines) for secret in ("PROMPT-SENTINEL", "API-KEY-SENTINEL", "quota exceeded"))


def test_reused_client_does_not_inherit_previous_call_identity(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    monkeypatch.setenv("MSP_LOG", str(tmp_path / "msp.jsonl"))
    manager = _manager(LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")}))
    assert manager._run_llm_cli("first") == "answer:first"
    monkeypatch.setenv("MSP_REJECT_INITIALIZE", "1")
    with pytest.raises(RuntimeError):
        manager._run_llm_cli("second")

    records = _records(log_file.read_text())
    ends = [record for record in records if record["kind"] == "end"]
    assert len(ends) == 2 and ends[0]["diagnostic_id"] != ends[1]["diagnostic_id"]
    assert ends[1]["identity"]["confirmed_session"] == "unobserved" and ends[1]["identity"]["turn"] == "unobserved"
    assert ends[1]["milestones"]["assistant_text"]["state"] == "unobserved"
    assert ends[1]["summary"]["primary_failure"]["phase"] == "initialization"
    assert ends[1]["summary"]["primary_failure"]["category"] == "rpc_error"


def test_pre_host_refusal_still_gets_start_and_end(tmp_path, monkeypatch, _use_real_commands, sinks):
    _, log_file = sinks
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3", options=["--prompt-file=x"])})
    manager = _manager(config)
    with pytest.raises(RuntimeError):
        manager._run_llm_cli("first")
    records = _records(log_file.read_text())
    assert [record["kind"] for record in records] == ["start", "phase", "end"]
    assert records[-1]["host"]["state"] == "not_started" and records[-1]["host"]["pid"] is None
    assert records[-1]["summary"]["primary_failure"]["phase"] == "preparation"


@pytest.mark.parametrize("failure", ["emitter", "suppressed"])
def test_diagnostic_trouble_does_not_change_result_or_wire_traffic(tmp_path, monkeypatch, _use_real_commands, failure):
    repo = _repository(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(_host(tmp_path)))
    config = LLMBackendConfiguration(backends={"muse": BackendConfig(name="muse", backend_type="muse", model="muse-spark-1.3")})

    def run(log_name: str) -> list[str]:
        monkeypatch.setenv("MSP_LOG", str(tmp_path / log_name))
        assert _manager(config)._run_llm_cli("first") == "answer:first"
        return [json.loads(line)["frame"].get("method") for line in (tmp_path / log_name).read_text().splitlines()]

    baseline = run("baseline.jsonl")

    def reject(level: str, line: str) -> None:
        raise OSError("sink rejected")

    monkeypatch.setattr(muse_diagnostics, "emit_record", reject if failure == "emitter" else lambda level, line: None)
    assert run("variant.jsonl") == baseline


def test_default_cadence_is_thirty_seconds():
    assert muse_diagnostics.HEARTBEAT_INTERVAL_SECONDS == 30.0
    assert MuseInvocationObserver().interval == 30.0


def test_display_sanitizes_and_bounds_metadata():
    shown = display("token=abc123456789012345678901234567890\nline\x1b[31m" + "x" * 500)
    assert len(shown) <= 128 and shown.endswith("[truncated]") and "\n" not in shown and "\x1b" not in shown
    assert display("short") == "short"


def test_before_ack_timing_annotation_is_released_after_fold():
    observer = MuseInvocationObserver()
    observer.session_confirmed("s")
    item = TimedFrame({"method": "item/completed", "params": {"sessionId": "s", "item": {"kind": "agentMessage", "status": "completed", "turnId": "t", "text": "SECRET"}}}, 1.0)
    old = TimedFrame({"method": "item/completed", "params": {"sessionId": "s", "item": {"kind": "agentMessage", "status": "completed", "turnId": "old", "text": "x"}}}, 0.5)
    observer.notification(old)
    observer.notification(item)
    assert observer.text.first_at is None and observer.candidate_text_at == 0.5
    observer.turn_acknowledged("t", [old, item])
    assert observer.text.first_at == 1.0 and observer.text.before_ack
    assert item.decoded_at is None and old.decoded_at is None
    assert "SECRET" not in json.dumps(observer._payload("heartbeat", time.monotonic()))
