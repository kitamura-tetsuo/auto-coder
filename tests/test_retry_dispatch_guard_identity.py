"""Regression coverage for #2292: an explicit retry must reach the unified
ordinary Issue dispatcher under its own durable attempt identity, rather than
colliding with a predecessor's indeterminate handoff at the ordinary numeric
attempt counter.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from auto_coder.automation_config import AutomationConfig
from auto_coder.issue_dispatch import (
    AdapterOutcome,
    CandidateHandoff,
    DispatchOutcome,
    IssueAttemptIdentity,
    IssueDispatchGuard,
)
from auto_coder.issue_stage_routing import IssueStageRoutingStore
from auto_coder.llm_backend_config import LLMBackendConfiguration

REPO = "owner/repo"
ISSUE_NUMBER = 2284


def _local_backend_config() -> LLMBackendConfiguration:
    return LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["local-open"]},
            "backends": {"local-open": {"backend_type": "opencode"}},
        }
    )


def _record_predecessor_indeterminate_handoff(numeric_attempt: str = "0") -> None:
    """Reproduce the reported ``length``-truncated OpenCode handoff: a durable
    guard row for the ordinary numeric attempt, left indeterminate."""
    owner, name = REPO.split("/", 1)
    identity = IssueAttemptIdentity(owner, name, ISSUE_NUMBER, numeric_attempt)
    guard = IssueDispatchGuard()
    claim = guard.reserve(identity, CandidateHandoff("local-open", "opencode"))
    assert claim.admitted
    finalized = guard.finalize(claim, AdapterOutcome(DispatchOutcome.INDETERMINATE, diagnostic="finish reason: 'length'"))
    assert finalized.outcome is DispatchOutcome.INDETERMINATE


def _owned_retry_authority(request_id: str = "implementation-retry-0766bfcfcdbb", generation: str = "generation-2292"):
    routing = IssueStageRoutingStore(Path.home() / ".auto-coder" / "issue-stage-routing.sqlite3")
    routing.accept_retry_request(request_id, REPO, ISSUE_NUMBER, generation)
    routing.capture_retry_predecessor(request_id, None, None, None)
    return routing.mark_retry_owned(request_id, f"invocation-{request_id}")


def test_explicit_retry_reaches_dispatch_under_its_own_durable_attempt(tmp_path, monkeypatch):
    """AS-001/REQ-002: the retry's durable attempt A must not collide with the
    predecessor's suppressing row at the stale numeric attempt "0", and the
    predecessor row must be retained untouched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from auto_coder.issue_processor import _dispatch_issue_candidates

    _record_predecessor_indeterminate_handoff()
    authority = _owned_retry_authority()

    config = _local_backend_config()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    invocations = []
    monkeypatch.setattr(
        "auto_coder.issue_processor._take_issue_actions",
        lambda *_args, **kwargs: invocations.append(kwargs) or ["implemented"],
    )
    monkeypatch.setattr(
        "auto_coder.cli_helpers.build_backend_manager",
        lambda **_kwargs: MagicMock(),
    )

    execution = _dispatch_issue_candidates(
        REPO,
        {"number": ISSUE_NUMBER},
        AutomationConfig(),
        MagicMock(),
        ["local-open"],
        retry_authority=authority,
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.identity.implementation_attempt_id == authority.attempt_id
    assert execution.result.identity.implementation_attempt_id != "0"
    assert len(invocations) == 1
    assert invocations[0]["retry_authority"].request_id == authority.request_id

    owner, name = REPO.split("/", 1)
    predecessor = IssueDispatchGuard().inspect(IssueAttemptIdentity(owner, name, ISSUE_NUMBER, "0"))
    assert predecessor is not None
    assert predecessor.outcome is DispatchOutcome.INDETERMINATE

    new_claim = IssueDispatchGuard().inspect(IssueAttemptIdentity(owner, name, ISSUE_NUMBER, authority.attempt_id))
    assert new_claim is not None
    assert new_claim.outcome is DispatchOutcome.LOCAL_COMPLETED


def test_replay_of_same_retry_recovers_receipt_without_second_invocation(tmp_path, monkeypatch):
    """REQ-005/REQ-006: replaying the identical retry request must not start a
    second local invocation once the first has durably completed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from auto_coder.issue_processor import _dispatch_issue_candidates

    _record_predecessor_indeterminate_handoff()
    authority = _owned_retry_authority()

    config = _local_backend_config()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    call_count = {"n": 0}

    def fake_take_actions(*_args, **_kwargs):
        call_count["n"] += 1
        return ["implemented"]

    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", fake_take_actions)
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: MagicMock())

    for _ in range(2):
        execution = _dispatch_issue_candidates(
            REPO,
            {"number": ISSUE_NUMBER},
            AutomationConfig(),
            MagicMock(),
            ["local-open"],
            retry_authority=authority,
        )
        assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED

    assert call_count["n"] == 1


def test_ordinary_reevaluation_without_retry_authority_stays_suppressed(tmp_path, monkeypatch):
    """REQ-007: outside a verified explicit retry, ordinary same-attempt
    suppression against the predecessor's indeterminate row is unchanged."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from auto_coder.issue_processor import _dispatch_issue_candidates

    _record_predecessor_indeterminate_handoff()

    config = _local_backend_config()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    invoked = []
    monkeypatch.setattr(
        "auto_coder.issue_processor._take_issue_actions",
        lambda *_args, **_kwargs: invoked.append(True) or ["implemented"],
    )
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: MagicMock())

    execution = _dispatch_issue_candidates(
        REPO,
        {"number": ISSUE_NUMBER},
        AutomationConfig(),
        MagicMock(),
        ["local-open"],
    )

    assert not invoked
    assert execution.result.outcome is DispatchOutcome.INDETERMINATE
    assert "suppressing ownership" in execution.result.diagnostic


def test_stale_generation_retry_authority_defers_without_new_claim(tmp_path, monkeypatch):
    """AS-003/REQ-001: authority that no longer matches the durable request
    record must produce a non-starting result, never a new guard claim."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from auto_coder.issue_processor import _dispatch_issue_candidates

    authority = _owned_retry_authority()
    stale = authority.__class__(**{**authority.__dict__, "generation": "generation-superseded"})

    config = _local_backend_config()
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)
    invoked = []
    monkeypatch.setattr(
        "auto_coder.issue_processor._take_issue_actions",
        lambda *_args, **_kwargs: invoked.append(True) or ["implemented"],
    )
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", lambda **_kwargs: MagicMock())

    execution = _dispatch_issue_candidates(
        REPO,
        {"number": ISSUE_NUMBER},
        AutomationConfig(),
        MagicMock(),
        ["local-open"],
        retry_authority=stale,
    )

    assert not invoked
    assert execution.result.outcome is DispatchOutcome.DEFERRED
    assert execution.result.claim_incarnation == ""

    owner, name = REPO.split("/", 1)
    assert IssueDispatchGuard().inspect(IssueAttemptIdentity(owner, name, ISSUE_NUMBER, authority.attempt_id)) is None
