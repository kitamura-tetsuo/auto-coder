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
