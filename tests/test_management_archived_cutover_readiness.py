"""Component checks for the native fixture's readiness observation order.

Controlled replies exercise the fixture oracle only; they are not evidence of
Docker, authenticated application readiness, or a successful native cutover.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import test_management_archived_cutover_container as cutover


def _ready_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, initial_status: str = "launched", terminal_status: str = "succeeded", operator: bool = True, changed_field: str | None = None, challenge: int = 401):
    generation = cutover.Generation("manager-c", tmp_path, "exact-manager-c", [], proxy="exact-proxy-c")
    chain = cutover.Chain.__new__(cutover.Chain)
    chain.worker_baseline = None
    launch = {"request_id": "request-c", "owner_incarnation": "owner-c", "child_identity": "child-c", "observation_id": "a" * 32, "started": 100.0, "deadline": 160.0}
    current = [{**launch, "status": initial_status}]
    reads: list[dict[str, object]] = []
    authenticated_calls: list[bool] = []

    def docker(*arguments, **kwargs):
        if arguments[0] == "exec":
            assert arguments[1] == generation.container
            reads.append(dict(current[0]))
            return json.dumps(current[0])
        assert arguments[0] == "inspect"
        return "running"

    def call(selected, tool, arguments, *, authenticated=True):
        assert selected is generation and tool == "auto_coder_whoami"
        authenticated_calls.append(authenticated)
        if not authenticated:
            return {"http_status": challenge}
        assert arguments == {"observation_id": launch["observation_id"]}
        current[0] = {**launch, "status": terminal_status}
        if changed_field is not None:
            current[0][changed_field] = "different-generation"
        return {"uid": "operator" if operator else "different-principal"}

    # A single poll starts within the existing budget. A second poll would be
    # too late, reproducing the completed transition during authenticated I/O.
    ticks = iter((0.0, 1.0, 61.0))
    monkeypatch.setattr(cutover, "time", SimpleNamespace(monotonic=lambda: next(ticks, 61.0), sleep=lambda _: None))
    monkeypatch.setattr(chain, "ensure_proxy", lambda selected: None)
    monkeypatch.setattr(chain, "docker", docker)
    monkeypatch.setattr(chain, "call", call)
    return chain, generation, reads, authenticated_calls


@pytest.mark.parametrize("initial_status", ["launched", "succeeded"])
def test_terminal_read_follows_authenticated_whoami(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_status: str) -> None:
    chain, generation, reads, calls = _ready_case(monkeypatch, tmp_path, initial_status=initial_status)
    chain.ready(generation, fresh=True)
    assert [value["status"] for value in reads] == [initial_status, "succeeded"]
    assert calls == [False, True]


@pytest.mark.parametrize("changed_field", ["observation_id", "child_identity"])
def test_terminal_read_must_bind_the_same_authenticated_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed_field: str) -> None:
    chain, generation, reads, calls = _ready_case(monkeypatch, tmp_path, changed_field=changed_field)
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.ready(generation, fresh=True)
    assert len(reads) == 2 and calls == [False, True]


def test_authenticated_whoami_does_not_replace_terminal_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chain, generation, reads, calls = _ready_case(monkeypatch, tmp_path, terminal_status="launched")
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.ready(generation, fresh=True)
    assert [value["status"] for value in reads] == ["launched", "launched"]
    assert calls == [False, True]


def test_terminal_success_does_not_replace_operator_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chain, generation, reads, calls = _ready_case(monkeypatch, tmp_path, operator=False)
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.ready(generation, fresh=True)
    assert len(reads) == 1 and calls == [False, True]


def test_unauthenticated_challenge_must_still_refuse_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chain, generation, reads, calls = _ready_case(monkeypatch, tmp_path, challenge=200)
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.ready(generation, fresh=True)
    assert len(reads) == 1 and calls == [False]


def _cutover_case(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, ready_at: float = 80.0, replaced: str | None = None):
    chain, generation, _, _ = _ready_case(monkeypatch, tmp_path)
    clock = SimpleNamespace(now=0.0)
    launch = {"request_id": "request-c", "owner_incarnation": "owner-c", "child_identity": "child-c", "observation_id": "a" * 32, "started": 100.0, "deadline": 220.0, "status": "launched"}
    samples = []
    authenticated = []
    proved = []

    def status(selected, *, require_live=False):
        assert selected is generation
        if not samples:
            clock.now += 45.0
        samples.append((require_live, clock.now))
        result = {**launch, "status": "succeeded" if authenticated else "launched"}
        if not require_live and (replaced == "before-auth" or (replaced == "after-auth" and authenticated)):
            result["observation_id"] = "b" * 32
        return result

    def call(selected, tool, arguments, *, authenticated=True):
        assert selected is generation and tool == "auto_coder_whoami"
        if not authenticated:
            return {"http_status": 401}
        if clock.now < ready_at:
            raise ValueError("application startup is still in progress")
        assert arguments == {"observation_id": launch["observation_id"]}
        accepted.append(clock.now)
        return {"uid": "operator"}

    accepted = authenticated
    monkeypatch.setattr(cutover, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: setattr(clock, "now", clock.now + 1.0)))
    monkeypatch.setattr(chain, "fresh_status", status)
    monkeypatch.setattr(chain, "call", call)
    monkeypatch.setattr(chain, "host", lambda selected: SimpleNamespace(communicate=lambda **kwargs: ("", ""), returncode=0))
    monkeypatch.setattr(chain, "status", lambda *args: None)
    monkeypatch.setattr(chain, "stop", lambda *args: None)
    monkeypatch.setattr(chain, "docker", lambda *args, **kwargs: "running")
    monkeypatch.setattr(chain, "prove_child", lambda selected: proved.append(selected))
    return chain, generation, clock, samples, proved


def test_prelaunch_source_work_has_a_separate_finite_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chain, generation, clock, samples, proved = _cutover_case(monkeypatch, tmp_path)
    chain.start_cutover(generation, generation)
    assert samples[0] == (True, 45.0)
    assert clock.now == 80.0
    assert proved == [generation]


@pytest.mark.parametrize("replaced", ["before-auth", "after-auth"])
def test_launch_cannot_change_between_phase_gate_and_readiness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replaced: str) -> None:
    chain, generation, clock, _, proved = _cutover_case(monkeypatch, tmp_path, replaced=replaced)
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.start_cutover(generation, generation)
    assert clock.now == 105.0
    assert proved == []


def test_readiness_retries_do_not_renew_the_sixty_second_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    chain, generation, clock, _, proved = _cutover_case(monkeypatch, tmp_path, ready_at=106.0)
    with pytest.raises(AssertionError, match="exact synthetic child readiness was not proven"):
        chain.start_cutover(generation, generation)
    assert clock.now == 105.0
    assert proved == []


@pytest.mark.parametrize("state", ["pending", "invalid", "unknown", "late-launch"])
def test_launch_gate_refuses_without_renewing_its_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str) -> None:
    chain, generation, _, _ = _ready_case(monkeypatch, tmp_path)
    clock = SimpleNamespace(now=0.0)
    samples = []

    def status(selected, *, require_live=False):
        assert selected is generation and require_live
        samples.append(clock.now)
        if state == "invalid":
            raise ValueError("production fresh record or live lifetime is invalid")
        if state == "late-launch":
            clock.now = 61.0
            return {"status": "launched"}
        return {"status": state}

    monkeypatch.setattr(cutover, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda _: setattr(clock, "now", clock.now + 1.0)))
    monkeypatch.setattr(chain, "fresh_status", status)
    with pytest.raises(AssertionError, match="exact synthetic fresh launch was not proven"):
        chain.wait_for_fresh_launch(generation)
    assert 60.0 <= clock.now <= 62.0
    assert 1 <= len(samples) <= 60


@pytest.mark.parametrize("mismatch", [None, "missing", "owner", "child", "phase", "expired", "window", "terminal-failed"])
def test_live_launch_reader_joins_actual_production_observations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mismatch: str | None) -> None:
    import io
    import time
    from contextlib import redirect_stdout

    from auto_coder.management import bootstrap_supervisor, fresh_initialization, self_update_handoff

    chain, generation, _, _ = _ready_case(monkeypatch, tmp_path)
    child = SimpleNamespace(pid=42, start_token="start-c", child_generation=1, selected_runtime="runtime-c", supervisor_incarnation="owner-c", phase=SimpleNamespace(value="running"))
    now = time.time()
    launch = {
        "request_id": "request-c",
        "owner_incarnation": "owner-c",
        "child_identity": bootstrap_supervisor.ManagementBootstrapSupervisor._identity(child),
        "observation_id": "a" * 32,
        "started": now - 5,
        "deadline": now + 115,
        "status": "launched",
    }
    owner = "different-owner" if mismatch == "owner" else "owner-c"
    if mismatch == "child":
        child.pid = 43
    if mismatch == "phase":
        child.phase.value = "stopped"
    if mismatch == "expired":
        launch.update(started=now - 121, deadline=now - 1)
    if mismatch == "window":
        launch["deadline"] = now + 500
    if mismatch == "terminal-failed":
        launch["status"] = "unknown"
    monkeypatch.setattr(fresh_initialization, "read_fresh_status", lambda path: launch)
    monkeypatch.setattr(bootstrap_supervisor.BootstrapConfig, "read", classmethod(lambda cls, path: SimpleNamespace(verification_timeout=120)))
    monkeypatch.setattr(self_update_handoff, "observe_supervisor", lambda config: None if mismatch == "missing" else (owner, child))

    def docker(*arguments, **kwargs):
        assert arguments[:4] == ("exec", generation.container, "python", "-c")
        captured = io.StringIO()
        # Execute the actual fixture reader program with controlled production
        # observations; these component tests do not claim kernel evidence.
        with redirect_stdout(captured):
            exec(arguments[4], {})
        return captured.getvalue()

    monkeypatch.setattr(chain, "docker", docker)
    if mismatch is None:
        assert chain.fresh_status(generation, require_live=True) == launch
    else:
        with pytest.raises(ValueError, match="fresh launch"):
            chain.fresh_status(generation, require_live=True)
