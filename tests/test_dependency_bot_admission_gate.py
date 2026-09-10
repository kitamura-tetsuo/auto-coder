"""Tests for the common dependency-bot processing-policy gate (Issue #1995).

`evaluate_dependency_bot_admission` (and the pure decision helpers it is
built from) is the single shared decision used both by the mandatory common
admission boundary in `AutomationEngine._process_single_candidate_unified_impl`
and by the optional prefilter retained in `AutomationEngine._get_candidates`.
The bug this closes: a dependency-bot PR reaching processing through any
other origin (startup reconciliation, webhook-invalidated worker dispatch,
explicit single-target processing, pending-work/merge-operation resumption)
never consulted the dependency-bot policy at all, so it could acquire a
standalone implementation reservation even though `_get_candidates()` would
have rejected it.

Client fakes below use a plain class with a real
``get_pull_request_metadata_strict`` method (mirroring how
``test_codex_cloud_unsafe_branch.py`` exercises the same strict-retrieval
capability check elsewhere in this suite) rather than ``unittest.mock.Mock``:
the capability check in ``pr_processor._fetch_authoritative_dependency_bot_pr``
looks at ``type(client)``, which a bare ``Mock()`` never exposes the method
on regardless of what is stubbed on the instance.
"""

from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig, Candidate, ExplicitTargetOutcome
from auto_coder.automation_engine import AutomationEngine
from auto_coder.pr_processor import (
    DependencyBotAdmissionDecision,
    _dependency_bot_flag_decision,
    _dependency_bot_readiness_decision,
    evaluate_dependency_bot_admission,
)
from auto_coder.util.github_action import GitHubActionsStatusResult


def _mergeable_open_pr(number: int = 5318, author: str = "dependabot[bot]", sha: str = "deadbeef") -> dict:
    return {
        "number": number,
        "title": "Bump some-package",
        "body": "",
        "state": "open",
        "mergeable": True,
        "head": {"ref": "dependabot/npm_and_yarn/some-package-1.0.0", "sha": sha},
        "labels": [],
        "author": author,
        "user": {"login": author},
    }


def _config(*, ignore: bool = False, auto_merge: bool = True) -> AutomationConfig:
    config = AutomationConfig()
    config.IGNORE_DEPENDABOT_PRS = ignore
    config.AUTO_MERGE_DEPENDABOT_PRS = auto_merge
    return config


class _FakeStrictClient:
    """A minimal client exposing a real ``get_pull_request_metadata_strict``.

    Successive calls return successive entries of ``responses`` (the last
    entry repeats once exhausted), or raise ``fetch_error`` on every call
    when provided.
    """

    def __init__(self, responses=None, fetch_error: Exception | None = None, token: str = "test-token"):
        self.token = token
        self._responses = list(responses or [])
        self._fetch_error = fetch_error
        self.calls = 0

    def get_pull_request_metadata_strict(self, repo_name: str, pr_number: int) -> dict:
        self.calls += 1
        if self._fetch_error is not None:
            raise self._fetch_error
        if not self._responses:
            raise AssertionError("no PR response configured for this fetch")
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


class _NoStrictRetrievalClient:
    """A lightweight client with no ``get_pull_request_metadata_strict`` at all."""

    def __init__(self, token: str = "test-token"):
        self.token = token


class TestDependencyBotFlagDecision:
    """REQ-001, REQ-002, REQ-008: classification and flag precedence."""

    def test_non_bot_pr_is_always_allowed(self):
        decision = _dependency_bot_flag_decision(False, ignore_dependabot_prs=True, auto_merge_dependabot_prs=True)
        assert decision == DependencyBotAdmissionDecision(allowed=True)

    def test_ignore_flag_skips_regardless_of_auto_merge(self):
        decision = _dependency_bot_flag_decision(True, ignore_dependabot_prs=True, auto_merge_dependabot_prs=False)
        assert decision is not None
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED

    def test_both_flags_false_permits_ordinary_processing(self):
        decision = _dependency_bot_flag_decision(True, ignore_dependabot_prs=False, auto_merge_dependabot_prs=False)
        assert decision == DependencyBotAdmissionDecision(allowed=True)

    def test_auto_merge_true_requires_readiness_confirmation(self):
        decision = _dependency_bot_flag_decision(True, ignore_dependabot_prs=False, auto_merge_dependabot_prs=True)
        assert decision is None


class TestDependencyBotReadinessDecision:
    """REQ-002, REQ-003, REQ-006: readiness semantics for AUTO_MERGE_DEPENDABOT_PRS."""

    def _decide(self, **overrides):
        base = dict(is_open=True, mergeable=True, ci_error=None, ci_pending=False, ci_success=True)
        base.update(overrides)
        return _dependency_bot_readiness_decision(**base)

    def test_passing_and_mergeable_is_allowed(self):
        assert self._decide().allowed is True

    def test_closed_pr_is_skipped(self):
        result = self._decide(is_open=False)
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.SKIPPED

    def test_unknown_open_state_is_deferred(self):
        result = self._decide(is_open=None)
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.DEFERRED

    def test_non_boolean_mergeable_is_deferred(self):
        for value in (None, "true", 1):
            result = self._decide(mergeable=value)
            assert result.allowed is False and result.outcome is ExplicitTargetOutcome.DEFERRED, value

    def test_mergeable_false_is_skipped(self):
        result = self._decide(mergeable=False)
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.SKIPPED

    def test_ci_observation_unavailable_is_deferred(self):
        result = self._decide(ci_error="throttled")
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.DEFERRED

    def test_pending_ci_is_skipped_not_deferred(self):
        result = self._decide(ci_pending=True)
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.SKIPPED

    def test_failing_ci_is_skipped(self):
        result = self._decide(ci_success=False)
        assert result.allowed is False and result.outcome is ExplicitTargetOutcome.SKIPPED


class TestEvaluateDependencyBotAdmission:
    """The mandatory common gate: fresh, same-HEAD confirmation (REQ-004, REQ-005)."""

    def test_non_bot_pr_allowed_without_any_fetch(self):
        client = _FakeStrictClient()
        config = _config()
        pr_data = {"number": 1, "author": "a-human", "user": {"login": "a-human"}, "head": {"ref": "x"}, "mergeable": True}
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is True
        assert client.calls == 0

    def test_jules_bot_is_not_classified_as_dependency_bot(self):
        client = _FakeStrictClient()
        config = _config()
        pr_data = {"number": 1, "author": "google-labs-jules[bot]", "head": {"ref": "x"}, "mergeable": True}
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is True
        assert client.calls == 0

    def test_ignore_dependabot_prs_skips_without_ci_evidence(self):
        client = _FakeStrictClient()
        config = _config(ignore=True, auto_merge=True)
        pr_data = _mergeable_open_pr()
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED
        assert client.calls == 0

    def test_both_flags_false_allows_failing_pr_like_normal_processing(self):
        client = _FakeStrictClient()
        config = _config(ignore=False, auto_merge=False)
        pr_data = dict(_mergeable_open_pr(), mergeable=False)
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is True
        assert client.calls == 0

    def test_auto_merge_true_reported_bug_scenario_is_deferred_when_ci_unavailable(self):
        """Reproduces the reported PR #5318 shape: no recorded evidence beyond the PR itself."""
        pr_data = _mergeable_open_pr()
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, error="No current CI observations", in_progress=True)):
            decision = evaluate_dependency_bot_admission(client, "kitamura-tetsuo/outliner", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED

    def test_auto_merge_true_admits_open_mergeable_passing_pr(self):
        pr_data = _mergeable_open_pr()
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, in_progress=False)):
            decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is True
        assert client.calls == 2  # initial + post-CI reconfirmation

    def test_auto_merge_true_rejects_failing_ci(self):
        pr_data = _mergeable_open_pr()
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, in_progress=False)):
            decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED

    def test_auto_merge_true_rejects_pending_ci(self):
        pr_data = _mergeable_open_pr()
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, in_progress=True)):
            decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED

    def test_auto_merge_true_defers_when_mergeable_is_null(self):
        pr_data = dict(_mergeable_open_pr(), mergeable=None)
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.DEFERRED

    def test_auto_merge_true_defers_when_metadata_fetch_raises(self):
        client = _FakeStrictClient(fetch_error=RuntimeError("rate limited"))
        config = _config(ignore=False, auto_merge=True)
        decision = evaluate_dependency_bot_admission(client, "o/r", _mergeable_open_pr(), config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.DEFERRED

    def test_auto_merge_true_skips_closed_pr(self):
        pr_data = dict(_mergeable_open_pr(), state="closed")
        client = _FakeStrictClient(responses=[pr_data])
        config = _config(ignore=False, auto_merge=True)
        decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.SKIPPED

    def test_auto_merge_true_defers_when_head_changes_during_readiness_evaluation(self):
        """REQ-005/AS-005: an accepted newer HEAD must fence stale green evidence."""
        initial = _mergeable_open_pr(sha="old-sha")
        moved = _mergeable_open_pr(sha="new-sha")
        client = _FakeStrictClient(responses=[initial, moved])
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, in_progress=False)):
            decision = evaluate_dependency_bot_admission(client, "o/r", initial, config)
        assert decision.allowed is False
        assert decision.outcome is ExplicitTargetOutcome.DEFERRED

    def test_lightweight_client_without_strict_retrieval_uses_given_data(self):
        """A client lacking `get_pull_request_metadata_strict` falls back to caller data."""
        client = _NoStrictRetrievalClient()
        pr_data = _mergeable_open_pr()
        config = _config(ignore=False, auto_merge=True)
        with patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, in_progress=False)):
            decision = evaluate_dependency_bot_admission(client, "o/r", pr_data, config)
        assert decision.allowed is True


class TestCommonAdmissionBoundaryRefusesDependencyBotPR:
    """AS-001/AS-002: every PR-processing origin funnels through the same gate.

    These exercise `_process_single_candidate_unified` directly with a
    candidate that never passed through `_get_candidates()` -- the same
    shape `_create_candidate_from_single` produces for startup
    reconciliation, webhook-invalidated worker dispatch, explicit
    single-target processing, and pending-work/merge-operation resumption.
    Before this fix, none of those origins consulted dependency-bot policy
    at all, so a non-ready dependency-bot PR reaching them could still
    acquire a standalone implementation reservation.
    """

    def test_refuses_before_touching_label_manager_or_pr_processing(self, mock_github_client):
        config = AutomationConfig()  # defaults: IGNORE=False, AUTO_MERGE=True
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = dict(_mergeable_open_pr(), mergeable=False)
        candidate = Candidate(type="pr", data=pr_data, priority=0)

        with patch("auto_coder.automation_engine.LabelManager") as mock_lm, patch("auto_coder.automation_engine.process_pull_request") as mock_pr_proc:
            result = engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.SKIPPED
        assert "not mergeable" in (result.target_reason or "")
        mock_lm.assert_not_called()
        mock_pr_proc.assert_not_called()

    def test_defers_without_admission_when_ci_evidence_unavailable(self, mock_github_client):
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = _mergeable_open_pr()
        candidate = Candidate(type="pr", data=pr_data, priority=0)

        with (
            patch("auto_coder.automation_engine.LabelManager") as mock_lm,
            patch("auto_coder.automation_engine.process_pull_request") as mock_pr_proc,
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, error="GitHub CI request failed (unavailable)")),
        ):
            result = engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)

        assert result.target_outcome is ExplicitTargetOutcome.DEFERRED
        assert result.refill_retry_required is True
        mock_lm.assert_not_called()
        mock_pr_proc.assert_not_called()

    def test_repeated_refusal_does_not_release_or_touch_existing_capacity(self, mock_github_client):
        """REQ-007: a refusal must not claim to free, or otherwise touch, unrelated capacity."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = dict(_mergeable_open_pr(), state="closed")
        candidate = Candidate(type="pr", data=pr_data, priority=0)

        slots = engine._get_implementation_slots("kitamura-tetsuo/outliner")
        with patch.object(slots, "start_execution", wraps=slots.start_execution) as spy_start:
            with patch("auto_coder.automation_engine.LabelManager"), patch("auto_coder.automation_engine.process_pull_request"):
                first = engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)
                second = engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)
            spy_start.assert_not_called()

        assert first.target_outcome is ExplicitTargetOutcome.SKIPPED
        assert second.target_outcome is ExplicitTargetOutcome.SKIPPED

    def test_permitted_dependency_bot_pr_still_reaches_ordinary_processing(self, mock_github_client):
        """Positive control: a ready dependency-bot PR is not blocked by this gate."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = _mergeable_open_pr()
        candidate = Candidate(type="pr", data=pr_data, priority=0)

        with (
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=True, in_progress=False)),
            patch("auto_coder.automation_engine.process_pull_request") as mock_pr_proc,
        ):
            mock_pr_proc.return_value = MagicMock(outcome=MagicMock(value="success"), error=None, actions_taken=[], pr_data=pr_data)
            engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)

        # Reaching ordinary PR processing (not refused at the dependency-bot
        # gate) is what matters here; deeper PR-processing behavior is
        # covered by the dedicated pr_processor test suite.
        assert mock_pr_proc.called

    def test_non_bot_pr_is_unaffected(self, mock_github_client):
        """A positively identified non-bot PR never touches this gate's fetch path."""
        config = AutomationConfig()
        engine = AutomationEngine(mock_github_client, config=config)
        pr_data = dict(_mergeable_open_pr(author="a-human-contributor"), mergeable=False)
        pr_data["user"] = {"login": "a-human-contributor"}
        candidate = Candidate(type="pr", data=pr_data, priority=0)

        with (
            patch("auto_coder.pr_processor._check_github_actions_status", return_value=GitHubActionsStatusResult(success=False, in_progress=False)),
            patch("auto_coder.automation_engine.process_pull_request") as mock_pr_proc,
        ):
            mock_pr_proc.return_value = MagicMock(outcome=MagicMock(value="success"), error=None, actions_taken=[], pr_data=pr_data)
            result = engine._process_single_candidate_unified("kitamura-tetsuo/outliner", candidate, config)

        assert result.target_outcome is not ExplicitTargetOutcome.SKIPPED
        assert mock_pr_proc.called
