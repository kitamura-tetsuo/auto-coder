"""Fast checks of installed acceptance oracles; no simulated host acceptance."""

from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_release_worker_acceptance_container as acceptance


def _facts() -> dict[str, object]:
    return {
        "Id": "container-original",
        "State": {"Pid": 123, "StartedAt": "2026-11-11T06:00:00Z"},
        "Mounts": [
            {"Type": "bind", "Source": "/host/source", "Destination": "/source", "Mode": "ro", "RW": False, "Propagation": "rprivate"},
            {"Type": "bind", "Source": "/host/runtime", "Destination": "/runtime", "Mode": "", "RW": True, "Propagation": "rprivate"},
            {"Type": "bind", "Source": "/host/routing", "Destination": "/routing", "Mode": "", "RW": True, "Propagation": "rprivate"},
        ],
    }


def _identity(monkeypatch, facts):
    def inspect(*arguments):
        assert arguments == ("inspect", "disposable-container")
        return SimpleNamespace(stdout=json.dumps([facts]))

    monkeypatch.setattr(acceptance, "_docker", inspect)
    return acceptance._container_identity("disposable-container")


def test_container_identity_ignores_only_mount_order(monkeypatch) -> None:
    facts = _facts()
    original = copy.deepcopy(facts)
    expected = _identity(monkeypatch, facts)
    for permutation in itertools.permutations(facts["Mounts"]):
        changed = {**facts, "Mounts": list(permutation)}
        assert _identity(monkeypatch, changed) == expected
    assert facts == original
    assert expected["mounts"] == sorted(facts["Mounts"], key=lambda item: json.dumps(item, sort_keys=True))


@pytest.mark.parametrize("field,value", [("Mode", "rw"), ("RW", True), ("Source", "/host/other"), ("Destination", "/other"), ("Propagation", "rshared"), ("Type", "volume"), ("FutureField", "different")])
def test_container_identity_retains_every_mount_field(monkeypatch, field: str, value: object) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    changed["Mounts"][0][field] = value
    assert _identity(monkeypatch, changed) != expected


@pytest.mark.parametrize("change", ["missing", "duplicate", "replace-with-duplicate"])
def test_container_identity_retains_mount_multiplicity(monkeypatch, change: str) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    if change == "missing":
        changed["Mounts"].pop()
    elif change == "duplicate":
        changed["Mounts"].append(copy.deepcopy(changed["Mounts"][0]))
    else:
        changed["Mounts"][1] = copy.deepcopy(changed["Mounts"][0])
    assert _identity(monkeypatch, changed) != expected


@pytest.mark.parametrize("field,value", [("Id", "container-other"), ("Pid", 456), ("StartedAt", "2026-11-11T07:00:00Z")])
def test_container_identity_retains_process_identity(monkeypatch, field: str, value: object) -> None:
    facts = _facts()
    expected = _identity(monkeypatch, facts)
    changed = copy.deepcopy(facts)
    if field == "Id":
        changed[field] = value
    else:
        changed["State"][field] = value
    assert _identity(monkeypatch, changed) != expected


def _lifetime_observation(monkeypatch, observed: str, token: str = "12345", pid: int = 223) -> None:
    def read_stat(*arguments):
        assert arguments[:3] == ("exec", "disposable-container", "python")
        assert arguments[-1] == str(pid)
        return SimpleNamespace(stdout=observed)

    monkeypatch.setattr(acceptance, "_docker", read_stat)
    acceptance._assert_lifetime_exited("disposable-container", {"pid": pid, "start_token": token})


def _stat(state: str, token: str = "12345") -> str:
    fields = [state, *(["0"] * 18), token, *(["0"] * 32)]
    return "223 (worker (original)) " + " ".join(fields)


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "W", "K", "P", "I"])
def test_original_live_or_stopped_lifetime_is_never_exited(monkeypatch, state: str) -> None:
    with pytest.raises(AssertionError):
        _lifetime_observation(monkeypatch, _stat(state))


@pytest.mark.parametrize("observed", ["ABSENT", _stat("Z"), _stat("X"), _stat("S", "67890")])
def test_lifetime_exit_oracle_accepts_only_exit_or_replaced_lifetime(monkeypatch, observed: str) -> None:
    _lifetime_observation(monkeypatch, observed)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel process-exit observation")
def test_actual_exited_child_is_proven_before_parent_reaps_it(monkeypatch, _use_real_commands: None) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"], stdin=subprocess.PIPE)
    try:
        path = Path(f"/proc/{child.pid}/stat")
        before = path.read_text()
        token = before.rsplit(") ", 1)[1].split()[19]
        with pytest.raises(AssertionError):
            _lifetime_observation(monkeypatch, before, token, child.pid)
        assert child.stdin is not None
        child.stdin.close()
        exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert exited.si_pid == child.pid and exited.si_code == os.CLD_EXITED and exited.si_status == 0
        retained = path.read_text()
        fields = retained.rsplit(") ", 1)[1].split()
        assert fields[0] == "Z" and fields[19] == token
        _lifetime_observation(monkeypatch, retained, token, child.pid)
    finally:
        if child.stdin is not None:
            child.stdin.close()
        assert child.wait(timeout=10) == 0
    assert not path.exists()


def test_container_identity_ignores_mount_order_but_detects_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    mounts = [{"Destination": "/workspace", "RW": True}, {"Destination": "/fixtures", "RW": False}]
    baseline = {"Id": "fixed", "State": {"Pid": 123, "StartedAt": "fixed-time"}, "Mounts": mounts}
    reordered = {**baseline, "Mounts": list(reversed(mounts))}
    altered = {**baseline, "Mounts": [{**mounts[0], "RW": False}, mounts[1]]}
    inspections = iter((baseline, reordered, altered))

    def inspect_fixture(*args: str) -> subprocess.CompletedProcess[str]:
        assert args == ("inspect", "fixture")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([next(inspections)]), stderr="")

    monkeypatch.setattr(acceptance, "_docker", inspect_fixture)
    identity = acceptance._container_identity("fixture")
    assert acceptance._container_identity("fixture") == identity
    assert acceptance._container_identity("fixture") != identity


def _publication_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, responses: list[dict[str, object]], *, sleep_advance: float = 0.1):
    """Control fixture replies only; this is not authenticated native evidence."""
    from tests import test_supervisor_container_integration as supervisor_fixture

    clock = SimpleNamespace(now=0.0, sleeps=[])
    requests: list[dict[str, object]] = []
    reads: list[str] = []
    replies = iter(responses)
    observation = SimpleNamespace(state=SimpleNamespace(value="IN_PROGRESS"), reason="original_window_in_progress", evidence=None)

    def read(operation_id: str):
        reads.append(operation_id)
        return observation

    producer = SimpleNamespace(read=read)
    system = SimpleNamespace(http=object(), bearer="fixture-bearer-secret", app=SimpleNamespace(state=SimpleNamespace(management_runtime=SimpleNamespace(beta_acceptance=SimpleNamespace(producer=producer)))))

    def call(http, bearer, tool, arguments):
        assert http is system.http and bearer == system.bearer
        assert tool == "auto_coder_beta_acceptance_get"
        assert arguments == {"target_id": "beta-worker", "operation_id": "original-acceptance"}
        requests.append(dict(arguments))
        return copy.deepcopy(next(replies))

    def sleep(delay: float) -> None:
        assert delay == 0.1
        clock.sleeps.append(delay)
        clock.now += sleep_advance

    monkeypatch.setattr(acceptance, "_call", call)
    monkeypatch.setattr(supervisor_fixture, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "retained"))
    accepted = {"operation_id": "original-acceptance", "status": "ACCEPTED", "phase": "accepted", "effect_state": "not_sent", "revision": 0, "safe_reason": None, "receipt": None, "local_state": "IN_PROGRESS", "local_result": None}
    artifact = tmp_path / "retained" / "supervisor-integration-logs" / "publication-case" / "beta-acceptance-failure.json"
    return system, accepted, artifact, requests, reads, clock


def _negative_acceptance(local_result: str) -> dict[str, object]:
    receipt = {"source": "beta-runtime-smoke-local-result", "result": json.dumps({"local_evidence_id": "beta-runtime-smoke-v1:original-acceptance", "local_result": local_result, "reason": "worker_observation_unavailable"}), "observed_at": 100.0}
    return {"operation_id": "original-acceptance", "status": "FAILED", "phase": "failed", "effect_state": "rejected", "revision": 2, "safe_reason": "local_" + local_result.lower(), "receipt": receipt, "local_state": "COMPLETE", "local_result": local_result}


@pytest.mark.parametrize("local_result", ["FAIL", "BLOCKED", "SUPERSEDED"])
@pytest.mark.parametrize("at_admission", [True, False], ids=["admission", "poll"])
def test_original_negative_acceptance_fails_immediately_and_retains_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_result: str, at_admission: bool) -> None:
    negative = _negative_acceptance(local_result)
    negative.update(accepted_actor={"uid": "private-actor"}, bearer="fixture-bearer-secret", configuration={"secret": "private-configuration"})
    negative["receipt"]["credential"] = "private-receipt-extra"
    original = copy.deepcopy(negative)
    system, accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [] if at_admission else [negative])
    with pytest.raises(AssertionError, match="Beta acceptance did not reach PASS_PUBLISHED") as failure:
        acceptance._wait_for_published_acceptance(system, negative if at_admission else accepted, "publication-case")
    assert requests == ([] if at_admission else [{"target_id": "beta-worker", "operation_id": "original-acceptance"}])
    assert clock.now == 0 and clock.sleeps == []
    assert reads == ["original-acceptance"]
    retained = json.loads(artifact.read_text())
    projection = retained["last_projection"]
    assert projection["local_result"] == local_result and projection["safe_reason"] == "local_" + local_result.lower()
    assert projection["receipt"] == {key: negative["receipt"][key] for key in ("source", "result", "observed_at")}
    assert json.loads(projection["receipt"]["result"])["reason"] == "worker_observation_unavailable"
    for text in (artifact.read_text(), str(failure.value)):
        assert "worker_observation_unavailable" in text
        assert all(secret not in text for secret in ("private-actor", "fixture-bearer-secret", "private-configuration", "private-receipt-extra"))
    assert isinstance(failure.value.__cause__, AssertionError)
    assert "immutable non-passing local result" in str(failure.value.__cause__)
    assert negative == original


def test_local_pass_with_uncertain_publication_keeps_reading_original_operation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    uncertain = {"operation_id": "original-acceptance", "status": "NEEDS_ATTENTION", "phase": "needs_attention", "effect_state": "sent_unknown", "safe_reason": "UNKNOWN:source_acceptance_publication", "local_state": "COMPLETE", "local_result": "PASS", "receipt": None}
    published = {**uncertain, "status": "PASS_PUBLISHED", "phase": "succeeded", "effect_state": "confirmed", "safe_reason": None}
    system, accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [uncertain, published])
    acceptance._wait_for_published_acceptance(system, uncertain, "publication-case")
    assert len(requests) == 2 and requests[0] == requests[1]
    assert clock.sleeps == [0.1] and clock.now == 0.1
    assert reads == [] and not artifact.exists()


@pytest.mark.parametrize("local_result", [None, "PASS"], ids=["sampling-pending", "publication-uncertain"])
def test_publication_timeout_retains_latest_projection_at_original_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, local_result: str | None) -> None:
    first = {
        "operation_id": "original-acceptance",
        "status": "NEEDS_ATTENTION",
        "phase": "needs_attention",
        "effect_state": "sent_unknown",
        "safe_reason": "UNKNOWN:source_acceptance_publication",
        "local_state": "COMPLETE" if local_result else "IN_PROGRESS",
        "local_result": local_result,
        "revision": 3,
        "receipt": None,
    }
    latest = {**first, "revision": 4}
    system, accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [first, latest], sleep_advance=100)
    with pytest.raises(AssertionError, match="Beta acceptance did not reach PASS_PUBLISHED") as failure:
        acceptance._wait_for_published_acceptance(system, accepted, "publication-case")
    assert len(requests) == 2 and clock.now == 200 and clock.sleeps == [0.1, 0.1]
    assert reads == ["original-acceptance"]
    assert json.loads(artifact.read_text())["last_projection"] == latest
    assert '"revision": 4' in str(failure.value)
    assert isinstance(failure.value.__cause__, AssertionError)
    assert "Supervisor fixture did not become ready" in str(failure.value.__cause__)


def test_failure_before_operation_receipt_retains_existing_durable_local_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from auto_coder.management.mcp_runtime_tools import ManagementRuntime
    from tests.test_beta_runtime_smoke import _fixture

    smoke = _fixture(tmp_path / "original")
    smoke.worker.unavailable = True
    producer = smoke.producer()
    terminal = producer.advance(smoke.operation.operation_id)
    assert terminal.evidence is not None and terminal.evidence.local_result.value == "BLOCKED"
    record = smoke.journal.get(smoke.operation.operation_id).record
    assert record is not None and record.effect.receipt is None and not record.safe_error_category
    runtime = ManagementRuntime.__new__(ManagementRuntime)
    runtime.beta_acceptance = SimpleNamespace(producer=producer)
    projected = runtime._project_beta_acceptance(record)
    assert projected["local_result"] == "BLOCKED" and projected["receipt"] is None and projected["safe_reason"] is None
    system, _accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [])
    system.app.state.management_runtime = runtime
    before = smoke.snapshot()
    source_calls, worker_calls = list(smoke.remote.calls), list(smoke.worker.calls)
    actual_reads: list[str] = []
    original_read = producer.read

    def read(operation_id: str):
        actual_reads.append(operation_id)
        return original_read(operation_id)

    monkeypatch.setattr(producer, "read", read)
    with pytest.raises(AssertionError, match="supervisor_observation_unavailable"):
        acceptance._wait_for_published_acceptance(system, projected, "publication-case")
    retained = json.loads(artifact.read_text())
    assert retained["last_projection"]["receipt"] is None
    assert retained["local_observation"]["reason"] == "supervisor_observation_unavailable"
    assert retained["local_observation"]["last_sample"] == {"supervisor_request_id": "request-1", "checks": [{"check": "supervisor_observation_unavailable", "disposition": "BLOCKED"}], "checks_truncated": False}
    assert actual_reads == [smoke.operation.operation_id] and requests == [] and clock.sleeps == []
    assert smoke.snapshot() == before and smoke.remote.calls == source_calls and smoke.worker.calls == worker_calls
    assert smoke.journal.get(smoke.operation.operation_id).record == record
    assert "original-grant" not in artifact.read_text() and "accepted_authority" not in artifact.read_text()
    # The case-owned state is disposable; its failure projection must survive.
    import shutil

    shutil.rmtree(tmp_path / "original")
    assert not (tmp_path / "original").exists() and json.loads(artifact.read_text()) == retained


def test_unavailable_local_diagnostic_preserves_original_negative_projection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system, accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [])

    def unavailable(operation_id: str):
        assert operation_id == "original-acceptance"
        raise OSError("private authority material")

    system.app.state.management_runtime.beta_acceptance.producer.read = unavailable
    with pytest.raises(AssertionError, match='"local_result": "BLOCKED"') as failure:
        acceptance._wait_for_published_acceptance(system, _negative_acceptance("BLOCKED"), "publication-case")
    assert json.loads(artifact.read_text())["local_observation"] == {"state": "UNAVAILABLE", "error_type": "OSError"}
    assert "private authority material" not in artifact.read_text() and "private authority material" not in str(failure.value)
    assert requests == [] and clock.sleeps == []


def test_diagnostic_write_failure_does_not_replace_original_acceptance_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    system, accepted, artifact, requests, reads, clock = _publication_case(monkeypatch, tmp_path, [])
    workspace = tmp_path / "retained"
    workspace.write_text("not a directory", encoding="utf-8")
    with pytest.raises(AssertionError, match='"local_result": "BLOCKED"') as failure:
        acceptance._wait_for_published_acceptance(system, _negative_acceptance("BLOCKED"), "publication-case")
    assert failure.value.__notes__ == ["Could not retain beta acceptance projection: NotADirectoryError"]
    assert isinstance(failure.value.__cause__, AssertionError)
    assert workspace.read_text() == "not a directory" and requests == [] and clock.sleeps == []


def test_local_failure_diagnostics_are_bounded_and_exclude_evidence_authority() -> None:
    last = {"supervisor_request_id": "s" * 200, "worker_request_id": "w" * 200, "accepted_authority": "private-sample-authority", "checks": [{"check": "c" * 600, "disposition": "BLOCKED", "reason": "r" * 600, "credential": "private-check-credential"} for _ in range(17)]}
    document = {"accepted_authority": "private-evidence-authority", "samples": [{"supervisor_request_id": "earlier-request"}, last]}
    evidence = SimpleNamespace(local_evidence_id="e" * 600, content_sha256="a" * 64, local_result=SimpleNamespace(value="BLOCKED"), document=lambda: document)
    observation = SimpleNamespace(state=SimpleNamespace(value="COMPLETE"), reason="r" * 600, evidence=evidence)
    calls: list[str] = []

    def read(operation_id: str):
        calls.append(operation_id)
        return observation

    system = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(management_runtime=SimpleNamespace(beta_acceptance=SimpleNamespace(producer=SimpleNamespace(read=read))))))
    result = acceptance._acceptance_local_failure(system, "original-acceptance")
    assert calls == ["original-acceptance"]
    assert result["state"] == "COMPLETE" and result["reason"] == "r" * 512 and result["local_evidence_id"] == "e" * 512
    assert result["content_sha256"] == "a" * 64 and result["sample_count"] == 2
    assert result["last_sample"]["supervisor_request_id"] == "s" * 128 and result["last_sample"]["worker_request_id"] == "w" * 128
    assert result["last_sample"]["checks"] == [{"check": "c" * 512, "disposition": "BLOCKED", "reason": "r" * 512}] * 16
    assert result["last_sample"]["checks_truncated"] is True
    assert "private-" not in json.dumps(result) and "earlier-request" not in json.dumps(result)
