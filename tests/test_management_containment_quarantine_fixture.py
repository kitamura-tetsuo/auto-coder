"""Real Unix guard transport regressions; these do not claim host acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from scripts import containment_quarantine_fixture as quarantine_helper

from auto_coder.management.containment_kernel_subobservation import _holder_start_token

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def _request_process(socket_path: Path, granted: Path, request: dict[str, str]) -> subprocess.Popen:
    code = """import json,sys
from pathlib import Path
from auto_coder.management.fresh_initialization import request_fresh_host_grant
request_fresh_host_grant(Path(sys.argv[1]),**json.loads(sys.argv[2]))
Path(sys.argv[3]).write_text('unexpected grant')
"""
    return subprocess.Popen((sys.executable, "-c", code, str(socket_path), json.dumps(request), str(granted)), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _request() -> dict[str, str]:
    return {"request_id": str(uuid.uuid4()), "intent_sha256": "a" * 64, "owner_incarnation": str(uuid.uuid4()), "new_container_id": "b" * 64}


def test_real_guard_holds_request_and_never_grants(tmp_path):
    path, granted = tmp_path / "fresh-guard.sock", tmp_path / "unexpected-grant"
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(path, request_id=expected["request_id"], intent_sha256=expected["intent_sha256"], container_id=expected["new_container_id"])
    process = _request_process(path, granted, expected)
    try:
        request = guard.receive_and_pause(process.pid, expected_start_token=_holder_start_token(process.pid), timeout=10)
        assert request.owner_incarnation == expected["owner_incarnation"]
        assert request.intent_sha256 == expected["intent_sha256"]
        assert len(request.challenge) == 64
        assert Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T"
        assert not granted.exists()
        guard.close()
        process.send_signal(signal.SIGCONT)
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0
        assert "fresh host grant is incomplete" in error
        assert not granted.exists()
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        guard.close()


def test_real_guard_rejects_another_intent_before_suspension(tmp_path):
    path, granted = tmp_path / "fresh-guard.sock", tmp_path / "unexpected-grant"
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(path, request_id=expected["request_id"], intent_sha256="c" * 64, container_id=expected["new_container_id"])
    process = _request_process(path, granted, expected)
    try:
        with pytest.raises(ValueError, match="sealed intent"):
            guard.receive_and_pause(process.pid, expected_start_token=_holder_start_token(process.pid), timeout=10)
        assert Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T"
        guard.close()
        _, error = process.communicate(timeout=5)
        assert process.returncode != 0
        assert "fresh host grant is incomplete" in error
        assert not granted.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        guard.close()


def test_real_guard_refuses_changed_peer_lifetime(tmp_path):
    expected = _request()
    guard = quarantine_helper.HeldFreshGuard(tmp_path / "fresh-guard.sock", request_id=expected["request_id"], intent_sha256=expected["intent_sha256"], container_id=expected["new_container_id"])
    try:
        with pytest.raises(ValueError, match="lifetime changed"):
            guard.receive_and_pause(os.getpid(), expected_start_token="0", timeout=0.1)
        assert guard.request is None
    finally:
        guard.close()


def _original_intent_fixture(tmp_path: Path) -> tuple[dict[str, object], Path, Path, Path]:
    state, journal, oauth, guard = (tmp_path / name for name in ("state", "journal", "oauth", "guard"))
    for path in (state, journal, oauth, guard):
        path.mkdir(mode=0o700)
    values: dict[str, object] = {
        "request_id": str(uuid.uuid4()),
        "old_instance": "original-fixture",
        "old_container_id": "a" * 64,
        "new_instance": "quarantined-fixture",
        "new_container_id": "b" * 64,
        "prior_evidence_sha256": "c" * 64,
        "owner_lock_device": 1,
        "owner_lock_inode": 2,
        "host_listener_port": 18080,
        "bootstrap_config_sha256": "d" * 64,
        "management_config_sha256": "e" * 64,
        "image_digest": quarantine_helper.QUARANTINE_IMAGE,
        "executable_sha256": "f" * 64,
        "application_sha256": "1" * 64,
        "runtime_reference": "installed-fixture-runtime",
        "state_root": "/management-child-state",
        "journal_root": "/management-operations",
        "oauth_root": "/management-oauth",
        "host_state_root": str(state),
        "host_journal_root": str(journal),
        "host_oauth_root": str(oauth),
    }
    for name, path in (("state", state), ("journal", journal), ("oauth", oauth), ("guard", guard)):
        info = path.stat()
        values[name + "_device"], values[name + "_inode"] = info.st_dev, info.st_ino
    return values, state, journal, oauth


def _controlled_original_claim_reply(intent: dict[str, object], state: Path, *, extra_field: bool = False) -> dict[str, str]:
    """A transport-boundary fixture, not original-source or native acceptance."""
    record = {**intent, "host_prior_journal_root": "", "prior_journal_root": "", "prior_journal_device": 0, "prior_journal_inode": 0}
    if extra_field:
        record.update(original_history="", original_history_id="")
    content = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    quarantine_helper._write_private(state / "fresh-initialization.json", content)
    return {
        "intent_sha256": hashlib.sha256(content).hexdigest(),
        "request_id": str(intent["request_id"]),
        "new_container_id": str(intent["new_container_id"]),
        "intent_source_sha256": quarantine_helper._ORIGINAL_INTENT_SOURCE_SHA256,
        "bootstrap_source_sha256": quarantine_helper._REVIEWED_QUARANTINED_SOURCE_SHA256,
    }


def test_original_claim_adapter_binds_image_source_mounts_and_host_readback(tmp_path: Path) -> None:
    intent, state, journal, oauth = _original_intent_fixture(tmp_path)
    calls = []

    def docker(*arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert arguments[:8] == ("run", "--rm", "--read-only", "--network", "none", "--env", "PYTHONDONTWRITEBYTECODE=1", "--mount")
        mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
        assert mounts == [f"type=bind,src={state},dst={state}", f"type=bind,src={journal},dst={journal},readonly", f"type=bind,src={oauth},dst={oauth},readonly"]
        assert arguments[-7:-4] == ("python", quarantine_helper.QUARANTINE_IMAGE, "-c")
        assert arguments[-4] == quarantine_helper._CLAIM_ORIGINAL_INTENT
        assert arguments[-3:-1] == (quarantine_helper._ORIGINAL_INTENT_SOURCE_SHA256, quarantine_helper._REVIEWED_QUARANTINED_SOURCE_SHA256)
        assert json.loads(arguments[-1]) == intent
        return json.dumps(_controlled_original_claim_reply(intent, state))

    observed = quarantine_helper._claim_original_intent(docker=docker, image_id=quarantine_helper.QUARANTINE_IMAGE, state=state, journal=journal, oauth=oauth, intent=intent)
    content = (state / "fresh-initialization.json").read_bytes()
    assert len(calls) == 1
    assert observed["intent_sha256"] == hashlib.sha256(content).hexdigest()
    assert observed["wire_json"].encode("utf-8") == content
    assert hashlib.sha256(observed["wire_json"].encode("utf-8")).hexdigest() == observed["intent_sha256"]
    assert len(json.loads(content)) == 33
    assert "original_history" not in json.loads(content)
    assert "original_history_id" not in json.loads(content)
    assert list(journal.iterdir()) == list(oauth.iterdir()) == []
    assert sorted(path.name for path in state.iterdir()) == ["fresh-initialization.json"]


@pytest.mark.parametrize("field", ("original_history", "original_history_id", "unknown_extension"))
def test_original_claim_never_silently_drops_new_input_fields(tmp_path: Path, field: str) -> None:
    intent, state, journal, oauth = _original_intent_fixture(tmp_path)
    intent[field] = ""
    with pytest.raises(ValueError, match="exact original input fields"):
        quarantine_helper._claim_original_intent(docker=lambda *_args, **_kwargs: pytest.fail("Unrecognized input must not launch a claim producer"), image_id=quarantine_helper.QUARANTINE_IMAGE, state=state, journal=journal, oauth=oauth, intent=intent)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("changed", ("image", "host-root"))
def test_original_claim_refuses_unbound_image_or_root_before_launch(tmp_path: Path, changed: str) -> None:
    intent, state, journal, oauth = _original_intent_fixture(tmp_path)
    intent["image_digest" if changed == "image" else "host_state_root"] = "substituted"
    with pytest.raises(ValueError, match="immutable manager image|host roots differ"):
        quarantine_helper._claim_original_intent(docker=lambda *_args, **_kwargs: pytest.fail("Unbound fixture inputs must not launch"), image_id=quarantine_helper.QUARANTINE_IMAGE, state=state, journal=journal, oauth=oauth, intent=intent)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("changed", ("request_id", "intent_sha256", "intent_source_sha256", "record-extension"))
def test_original_claim_refuses_receipt_or_actual_wire_substitution(tmp_path: Path, changed: str) -> None:
    intent, state, journal, oauth = _original_intent_fixture(tmp_path)

    def docker(*_arguments, **_kwargs):
        reply = _controlled_original_claim_reply(intent, state, extra_field=changed == "record-extension")
        if changed != "record-extension":
            reply[changed] = "substituted"
        return json.dumps(reply)

    with pytest.raises(ValueError, match="exact host readback"):
        quarantine_helper._claim_original_intent(docker=docker, image_id=quarantine_helper.QUARANTINE_IMAGE, state=state, journal=journal, oauth=oauth, intent=intent)
    assert (state / "fresh-initialization.json").is_file()
    assert not (state / "fresh-launch-claimed.json").exists()


def test_original_claim_program_rejects_current_producer_bytes_before_writing(tmp_path: Path) -> None:
    intent, state, _, _ = _original_intent_fixture(tmp_path)
    result = subprocess.run(
        (sys.executable, "-B", "-c", quarantine_helper._CLAIM_ORIGINAL_INTENT, quarantine_helper._ORIGINAL_INTENT_SOURCE_SHA256, quarantine_helper._REVIEWED_QUARANTINED_SOURCE_SHA256, json.dumps(intent)),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "original initialization source differs" in result.stderr
    assert list(state.iterdir()) == []


def _diagnostic_prepared(tmp_path: Path, docker) -> quarantine_helper.PreparedQuarantine:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    return quarantine_helper.PreparedQuarantine(tmp_path, "a" * 64, quarantine_helper.QUARANTINE_IMAGE, tmp_path / "config.json", tmp_path / "management.yml", state, tmp_path / "journal", tmp_path / "oauth", (), tmp_path / "support.json", None, docker)


def test_failed_capture_exports_bounded_actual_log_and_allowlisted_state_before_reraise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def docker(*arguments, **kwargs):
        calls.append(arguments)
        return json.dumps({"Id": "a" * 64, "Image": quarantine_helper.QUARANTINE_IMAGE, "State": {"Status": "exited", "Running": False, "ExitCode": 1, "Pid": 0, "Health": {"Log": "PRIVATE_HEALTH_CONTENT"}}, "Config": {"Env": ["PRIVATE_ENV_CONTENT"]}})

    prepared = _diagnostic_prepared(tmp_path, docker)
    log = prepared.state_root / "logs" / "management-bootstrap.log"
    log.parent.mkdir()
    content = b"DISCARDED_OLD_PREFIX\n" + b"x" * 65536 + b"\nactual bootstrap refusal\n"
    log.write_bytes(content)
    original = TimeoutError("original guard deadline")

    def fail(_self):
        raise original

    monkeypatch.setattr(quarantine_helper.PreparedQuarantine, "_start_and_capture", fail)
    with pytest.raises(TimeoutError) as raised:
        prepared.start_and_capture()
    assert raised.value is original
    assert len(calls) == 1
    assert calls[0][:4] == ("inspect", "--type", "container", "--format")
    assert ".Config" not in calls[0][4]
    assert prepared.artifacts == [tmp_path / "start-failure.json"]
    report = json.loads(prepared.artifacts[0].read_text())
    assert report["exception_category"] == "TimeoutError"
    assert report["docker"]["State"] == {"Status": "exited", "Running": False, "ExitCode": 1, "Pid": 0}
    assert report["bootstrap_log"]["tail_bytes"] == 65536
    assert report["bootstrap_log"]["text"].encode() == content[-65536:]
    assert report["bootstrap_log"]["tail_sha256"] == hashlib.sha256(content[-65536:]).hexdigest()
    assert report["bootstrap_log"]["truncated"] is True
    encoded = json.dumps(report)
    assert all(value not in encoded for value in ("PRIVATE_ENV_CONTENT", "PRIVATE_HEALTH_CONTENT", "DISCARDED_OLD_PREFIX"))
    assert prepared.artifacts[0].stat().st_mode & 0o777 == 0o600


def test_failed_capture_preserves_original_exception_when_sources_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def docker(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["docker"], stderr="PRIVATE_DIAGNOSTIC")

    prepared = _diagnostic_prepared(tmp_path, docker)
    original = ValueError("actual capture predicate")

    def fail(_self):
        raise original

    monkeypatch.setattr(quarantine_helper.PreparedQuarantine, "_start_and_capture", fail)
    with pytest.raises(ValueError) as raised:
        prepared.start_and_capture()
    assert raised.value is original
    report = json.loads(prepared.artifacts[0].read_text())
    assert report["docker_readback_error"] == "CalledProcessError"
    assert report["bootstrap_log_error"] == "FileNotFoundError"
    assert "PRIVATE_DIAGNOSTIC" not in json.dumps(report)


def test_failed_diagnostic_write_cannot_replace_capture_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _diagnostic_prepared(tmp_path, lambda *_args, **_kwargs: "{}")
    retained = tmp_path / "start-failure.json"
    retained.write_bytes(b"original diagnostic evidence")
    original = TimeoutError("original guard deadline")

    def fail(_self):
        raise original

    monkeypatch.setattr(quarantine_helper.PreparedQuarantine, "_start_and_capture", fail)
    with pytest.raises(TimeoutError) as raised:
        prepared.start_and_capture()
    assert raised.value is original
    assert retained.read_bytes() == b"original diagnostic evidence"
    assert prepared.artifacts == []


def test_successful_capture_does_not_run_failure_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _diagnostic_prepared(tmp_path, lambda *_args, **_kwargs: pytest.fail("Success must not inspect for failure diagnostics"))
    actual = object()
    monkeypatch.setattr(quarantine_helper.PreparedQuarantine, "_start_and_capture", lambda _self: actual)
    assert prepared.start_and_capture() is actual
    assert prepared.artifacts == []


def test_nonregular_bootstrap_log_cannot_block_failure_capture(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    os.mkfifo(state / "logs" / "management-bootstrap.log")
    code = """import json,sys
from pathlib import Path
from scripts.containment_quarantine_fixture import PreparedQuarantine,QUARANTINE_IMAGE
root=Path(sys.argv[1])
def docker(*args,**kwargs):
    value={'Id':'a'*64,'Image':QUARANTINE_IMAGE,'State':{'Running':False}}
    return json.dumps(value if '--format' in args else [value])
prepared=PreparedQuarantine(root,'a'*64,QUARANTINE_IMAGE,root/'config',root/'management',root/'state',root/'journal',root/'oauth',(),root/'support',None,docker)
try:
    prepared.start_and_capture()
except RuntimeError as error:
    assert str(error)=='the exact quarantined manager did not start'
else:
    raise AssertionError('missing actual start failure')
print(json.dumps({'artifacts':[str(path) for path in prepared.artifacts]}))
"""
    result = subprocess.run((sys.executable, "-B", "-c", code, str(tmp_path)), capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "start-failure.json").read_text())
    assert report["exception_category"] == "RuntimeError"
    assert report["bootstrap_log_error"] == "ValueError"
    assert report["docker"]["State"] == {"Running": False}
