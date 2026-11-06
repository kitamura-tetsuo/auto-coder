"""REQ-006/008: connected controller origins, real quota policy and durable stores.

Only GitHub, account-usage and reviewer transports are scripted. Git commands,
configuration loading, admission, dispatch, resolution and persistence stay real.
"""

import asyncio
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.durable_repair_allowance import RepairAllowanceLedger
from auto_coder.entity_invalidation import EntityIdentity
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.llm_backend_config import active_repo_context
from auto_coder.pr_processor import _two_tier_gate_inputs
from auto_coder.pr_review_execution import ReviewExecutionResult
from auto_coder.util.gh_cache import GitHubClient
from tests.ci_observation_fakes import _Actions, _Checks
from tests.test_strong_audit_producer import _git, _repository


class OriginScenario:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.runner = asyncio.Runner()
        # Keep real manager/client construction without requiring a host CLI.
        # No model execution is allowed through this external probe transport.
        cli_dir = root / "bin"
        cli_dir.mkdir()
        codex = cli_dir / "codex"
        codex.write_text('#!/bin/sh\nif [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then\n  echo "codex fixture transport"\n  exit 0\nfi\necho "unexpected Codex execution" >&2\nexit 97\n')
        codex.chmod(0o755)
        monkeypatch.setenv("PATH", str(cli_dir) + os.pathsep + os.environ["PATH"])
        self.clock = 1800000000.0
        self.reset = self.clock + 4 * 86400 + 600
        self.used = 80.0
        self.observations = []
        self.reviews = []
        self.timeline = []
        self.repository, self.base, self.head = _repository(root)
        _git(self.repository, "remote", "add", "origin", str(self.repository))
        _git(self.repository, "update-ref", "refs/pull/100/head", self.head)
        monkeypatch.chdir(self.repository)
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("AUTO_CODER_INVALIDATION_DB", str(root / "invalidations.sqlite3"))
        for variable in ("AUTO_CODER_CONFIG_PATH", "AUTO_CODER_LLM_CONFIG_PATH"):
            monkeypatch.delenv(variable, raising=False)
        import auto_coder.codex_usage_checker as usage_module

        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(self.clock, tz or timezone.utc)

        monkeypatch.setattr(usage_module, "datetime", ClockDateTime)
        monkeypatch.setattr("auto_coder.quota_selector.datetime", ClockDateTime)
        monkeypatch.setattr("time.time", lambda: self.clock)
        self.config_file = root / ".auto-coder" / "owner" / "repo" / "llm_config.toml"
        self.config_file.parent.mkdir(parents=True)
        self.configure("surplus")
        self.pr = {
            "number": 100,
            "title": "Quota recovery",
            "body": "Fixes #99",
            "state": "open",
            "created_at": "2026-11-01T00:00:00Z",
            "updated_at": "2026-11-01T00:00:00Z",
            "labels": [],
            "user": {"login": "owner", "id": 1},
            "mergeable": True,
            "head": {"ref": "feature", "sha": self.head},
            "base": {"ref": "main", "sha": self.base},
            "changed_files": 1,
            "additions": 1,
        }
        self.issue = {"number": 99, "state": "open", "title": "Contract", "updated_at": "2026-11-01T00:00:00Z", "body": "## Requirements\nREQ-001: Preserve the gate.", "labels": []}
        self.github = GitHubClient("test-token")
        monkeypatch.setattr(GitHubClient, "_instance", self.github)
        monkeypatch.setattr(self.github, "_read_issue_resource", lambda _repo, _number, relation="", **kw: ([] if relation != "parent" else None) if relation else dict(self.issue))
        for method in ("get_pull_request", "get_pull_request_metadata_strict"):
            monkeypatch.setattr(self.github, method, lambda *a, **kw: dict(self.pr))
        monkeypatch.setattr(self.github, "get_issue_timeline_strict", lambda *a, **kw: list(self.timeline))
        monkeypatch.setattr(self.github, "get_pr_details", lambda pr: pr)
        for method in ("get_issue", "get_issue_strict"):
            monkeypatch.setattr(self.github, method, lambda *a, **kw: dict(self.issue))
        monkeypatch.setattr(self.github, "get_issue_details", lambda issue: issue)
        monkeypatch.setattr(self.github, "get_pr_diff", lambda *a, **kw: "diff --git a/contract.txt b/contract.txt\n-base\n+head\n")
        for method in ("get_pr_review_threads_strict", "get_pr_reviews_strict", "get_issue_comments_strict", "get_pr_comments_strict", "get_pr_comments"):
            monkeypatch.setattr(self.github, method, lambda *a, **kw: [])
        monkeypatch.setattr(self.github, "try_add_labels", lambda *a, **kw: True)
        monkeypatch.setattr(self.github, "remove_labels", lambda *a, **kw: None)
        monkeypatch.setattr(self.github, "get_pull_request_head_sha_strict", lambda *a, **kw: self.head)
        api = SimpleNamespace(checks=_Checks("completed", "success", 1, False), actions=_Actions())
        monkeypatch.setattr("auto_coder.util.github_action.get_ghapi_client", lambda *a, **kw: api)
        monkeypatch.setattr(usage_module, "read_account_data", self.account)
        monkeypatch.setattr("auto_coder.pr_processor.run_adversarial_validation", lambda *a, **kw: AdversarialValidationResult(result="PASS", summary="Current ordinary contract verified"))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", self.publish_ordinary)
        monkeypatch.setattr("auto_coder.pr_processor.execute_review", self.review)

    def configure(self, strategy, enabled=True):
        self.config_file.write_text(
            f"""[quota_selection]
strategy = "{strategy}"
[backend_strong_pr_adversarial_validation]
order = ["codex-reviewer"]
default = "codex-reviewer"
[backends.codex-reviewer]
backend_type = "codex"
model = "gpt-5"
enabled = {str(enabled).lower()}
"""
        )

    def account(self, method, **kwargs):
        self.observations.append((self.clock, self.used))
        assert method == "account/rateLimits/read"
        return {"rateLimits": {"primary": {"windowDurationMins": 10080, "usedPercent": self.used, "resetsAt": self.reset}}}

    def review(self, review_input, manager, cwd):
        assert self.observations[-1] == (self.clock, self.used)
        assert _git(Path(cwd), "rev-parse", "HEAD") == self.head
        assert Path(cwd) != self.repository
        self.reviews.append(review_input)
        return ReviewExecutionResult(
            mode=review_input.mode,
            round_id=review_input.round_id,
            attempt_id=review_input.attempt_id,
            head_sha=review_input.head_sha,
            base_sha=review_input.base_sha,
            contract_identity=review_input.contract.identity,
            policy_identity=review_input.policy.identity,
            finding_set_revision=0,
            reviewer_provenance="strong/codex/gpt-5",
            verdict="PASS",
        )

    def publish_ordinary(self, *args, **kwargs):
        self.pr["updated_at"] = datetime.fromtimestamp(self.clock, timezone.utc).isoformat()
        return ReviewPublicationResult(True, "APPROVE", "")

    def engine(self):
        config = AutomationConfig(repo_name="owner/repo")
        config.AUTO_MERGE = True
        config.ENABLE_ADVERSARIAL_VALIDATION = True
        return AutomationEngine(self.github, config)

    def explicit(self, engine=None, force=False):
        return (engine or self.engine()).process_single("owner/repo", "pr", 100, explicit_only=True, force=force)

    def dispatch(self, engine, *, invalidate=False):
        async def run():
            if invalidate:
                await engine.invalidate_entity("owner/repo", "pr", 100)
            else:
                await engine._enqueue_pending_invalidations("owner/repo")
            worker = asyncio.create_task(engine._worker_loop("owner/repo", 0))
            try:
                await asyncio.wait_for(engine.queue.join(), timeout=10)
            finally:
                worker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker

        self.runner.run(run())

    def history(self):
        path = self.root / ".auto-coder" / "owner/repo" / "pr_review_cycle.json"
        return json.loads(path.read_text())["prs"]["100"]["wait_history"]

    def snapshot(self):
        with active_repo_context("owner/repo"):
            return _two_tier_gate_inputs(self.github, "owner/repo", self.pr).gate.state.snapshot(100)


@pytest.fixture
def scenario(tmp_path, monkeypatch, _use_real_commands):
    instance = OriginScenario(tmp_path, monkeypatch)
    try:
        yield instance
    finally:
        instance.runner.close()


def test_explicit_origin_loaded_burst_recovers_surplus_wait(scenario):
    s = scenario
    result = s.explicit()
    assert not result["errors"], result
    previous = s.snapshot()
    assert previous.retry_not_before == math.nextafter(s.clock + 600, math.inf), result
    assert not s.reviews
    with active_repo_context("owner/repo"):
        inputs = _two_tier_gate_inputs(s.github, "owner/repo", s.pr)
    inputs.gate.ordinary_pass(101, s.head, s.base, inputs.contract)
    unrelated = inputs.gate.state.snapshot(101)
    allowances = RepairAllowanceLedger()
    ledger_before = allowances.initialize_namespace("https://api.github.com", "owner/repo", 100)
    s.configure("burst")
    result = s.explicit(force=True)
    assert len(s.reviews) == 1, result
    assert s.pr["updated_at"] != s.pr["created_at"]
    accepted = s.snapshot()
    assert accepted.accepted_strong_round.policy_identity == inputs.policy.identity
    assert accepted.open_epoch == previous.open_epoch
    assert accepted.ordinary_pass_head_sha == previous.ordinary_pass_head_sha
    assert accepted.completion is None
    assert accepted.pending_effect == "STRONG_PUBLICATION"
    assert s.history() == [
        {
            "reason": previous.waiting_reason,
            "retry_not_before": previous.retry_not_before,
            "superseded_at": s.clock,
            "superseded_by": "explicit-operator-recheck",
            "superseding_observation": "accepted PASS",
        }
    ]
    assert inputs.gate.state.snapshot(101) == unrelated
    assert allowances.get_snapshot("https://api.github.com", "owner/repo", 100) == ledger_before


def test_automatic_pending_dispatch_rechecks_unchanged_usage_before_reset(scenario):
    s = scenario
    engine = s.engine()
    s.dispatch(engine, invalidate=True)
    first = s.snapshot()
    assert first.retry_not_before == math.nextafter(s.clock + 600, math.inf)
    assert not s.reviews
    observed = len(s.observations)
    s.clock += 100
    s.dispatch(engine, invalidate=True)
    assert s.snapshot().retry_not_before == first.retry_not_before
    assert len(s.observations) == observed
    assert s.history() == []
    s.clock = first.retry_not_before + 0.000001
    s.dispatch(engine)
    assert len(s.reviews) == 1
    assert s.clock < s.reset
    assert s.observations[-1] == (s.clock, 80.0)
    assert s.history()[0]["retry_not_before"] == first.retry_not_before


def test_explicit_origin_restores_selected_wait_when_target_changes_during_usage(
    scenario,
    monkeypatch,
):
    s = scenario
    s.explicit()
    selected = s.snapshot()
    assert selected.retry_not_before > s.clock
    observations = len(s.observations)

    def account(method, **kwargs):
        result = s.account(method, **kwargs)
        if len(s.observations) > observations:
            s.timeline.append(
                {
                    "event": "reopened",
                    "id": 1234,
                    "created_at": "2027-01-15T08:01:00Z",
                }
            )
        return result

    monkeypatch.setattr("auto_coder.codex_usage_checker.read_account_data", account)
    result = s.explicit(force=True)

    assert result["target_outcome"] == "deferred", result
    assert not s.reviews
    restored = s.snapshot()
    assert restored.active_claim is None
    assert restored.attempt_error_reason == selected.attempt_error_reason
    assert restored.retry_not_before == selected.retry_not_before
    assert s.history() == []


@pytest.mark.parametrize("origin", ["explicit", "automatic"])
@pytest.mark.parametrize("refusal", ["zero", "unavailable"])
def test_origin_refuses_fresh_zero_or_disabled_route(scenario, origin, refusal):
    s = scenario
    engine = s.engine()
    if origin == "explicit":
        s.explicit(engine)
    else:
        s.dispatch(engine, invalidate=True)
    previous = s.snapshot()
    if refusal == "zero":
        s.used = 100.0
    else:
        s.configure("surplus", enabled=False)
    s.clock = previous.retry_not_before + 1
    if origin == "explicit":
        result = s.explicit(engine, force=True)
        assert result["target_outcome"] == "deferred", result
    else:
        s.dispatch(engine)
    current = s.snapshot()
    assert not s.reviews
    assert current.accepted_strong_round is None
    assert current.waiting_reason == f"strong reviewer route is {'EXHAUSTED' if refusal == 'zero' else 'UNAVAILABLE'}"
    if refusal == "zero":
        assert current.retry_not_before == s.reset
    elif origin == "automatic":
        assert engine.invalidations.get_deferred(EntityIdentity("owner/repo", "pr", 100)).retry_not_before == s.clock + 60
    assert s.history()[0]["retry_not_before"] == previous.retry_not_before


def test_automatic_successor_bound_restart_and_duplicate_suppression(scenario):
    s = scenario
    s.reset = s.clock + 4.5 * 86400
    engine = s.engine()
    s.dispatch(engine, invalidate=True)
    first = s.snapshot()
    assert first.retry_not_before == s.clock + 1800
    s.clock = first.retry_not_before
    engine = s.engine()  # New controller; all obligations are loaded from disk.
    engine.invalidations.recover("owner/repo")
    s.dispatch(engine)
    second = s.snapshot()
    assert second.retry_not_before == s.clock + 1800
    assert not s.reviews
    assert s.history() == [
        {
            "reason": first.waiting_reason,
            "retry_not_before": first.retry_not_before,
            "superseded_at": s.clock,
            "superseded_by": "automatic-due-recheck",
            "superseding_observation": "strong reviewer route is EXHAUSTED",
        }
    ]
    count = len(s.observations)
    s.clock += 10
    s.dispatch(engine, invalidate=True)
    assert s.snapshot().retry_not_before == second.retry_not_before
    assert len(s.observations) == count
    assert len(s.history()) == 1


def test_explicit_origin_preserves_accepted_unpublished_effect(scenario, monkeypatch):
    from auto_coder.github_app_reviewer import GitHubAppReviewer

    s = scenario
    (s.root / ".auto-coder" / "config.toml").write_text('[github-app-auto-coder-reviewer]\napp_id = "1"\n')
    sends = []
    reconciliations = []

    def send(_reviewer, repo, number, head, body, authorize, **kwargs):
        assert authorize()
        sends.append((repo, number, head, body))
        return ReviewPublicationResult(False, "", "controlled uncertain response")

    def reconcile(_reviewer, repo, number, head, body, **kwargs):
        reconciliations.append((repo, number, head, body))
        return ReviewPublicationResult(False, "", "controlled unavailable receipt lookup")

    monkeypatch.setattr(GitHubAppReviewer, "publish_exact_pr_review", send)
    monkeypatch.setattr(GitHubAppReviewer, "find_exact_pr_review", reconcile)
    s.explicit()
    s.configure("burst")
    s.explicit(force=True)
    accepted = s.snapshot()
    assert len(s.reviews) == 1
    assert accepted.pending_effect == "STRONG_PUBLICATION"
    s.explicit(force=True)
    recovered = s.snapshot()
    assert len(sends) == 1
    assert reconciliations and all(receipt == sends[0] for receipt in reconciliations)
    effects = json.loads((s.root / ".auto-coder" / "owner/repo" / "pr_review_effects.json").read_text())
    assert len(effects["operations"]) == 1
    assert len(s.reviews) == 1
    assert recovered.accepted_strong_round == accepted.accepted_strong_round
    assert recovered.pending_effect == accepted.pending_effect
    assert recovered.completion is None


@pytest.mark.parametrize("after_commit", [False, True])
def test_operator_admission_write_interruption_retains_discoverable_work(scenario, monkeypatch, after_commit):
    from auto_coder.pr_review_cycle import PrReviewCycleRepository

    s = scenario
    engine = s.engine()
    s.dispatch(engine, invalidate=True)
    previous = s.snapshot()
    original_write = PrReviewCycleRepository._write
    interrupted = []

    def write(repository, state):
        if state["prs"]["100"]["active_claim"] and not interrupted:
            interrupted.append(True)
            if after_commit:
                original_write(repository, state)
            raise OSError("controlled admission persistence interruption")
        return original_write(repository, state)

    s.configure("burst")
    with monkeypatch.context() as fault:
        fault.setattr(PrReviewCycleRepository, "_write", write)
        result = s.explicit(force=True)
    assert interrupted == [True]
    assert result["target_outcome"] == "deferred", result
    assert "controlled admission persistence interruption" in " ".join(result["target_actions"])
    assert not s.reviews
    persisted = s.snapshot()
    assert persisted.accepted_strong_round is None
    restarted = s.engine()
    restarted.invalidations.recover("owner/repo")
    assert restarted.invalidations.pending_count("owner/repo") == 1
    if after_commit:
        assert persisted.active_claim is not None
        assert s.history()[0]["retry_not_before"] == previous.retry_not_before
    else:
        assert persisted.retry_not_before == previous.retry_not_before
        assert persisted.waiting_reason == previous.waiting_reason
        assert s.history() == []
    s.clock = previous.retry_not_before + 1
    s.dispatch(restarted)
    if after_commit:
        assert not s.reviews  # A retained execution owner must never be reset.
        assert s.snapshot().active_claim == persisted.active_claim
        assert restarted.invalidations.pending_count("owner/repo") == 1
    else:
        assert len(s.reviews) == 1


def test_overlapping_explicit_origins_reject_stale_selected_wait(scenario, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from auto_coder.pr_review_cycle import PrReviewCycleRepository

    s = scenario
    s.explicit()
    s.configure("burst")
    captured = Event()
    release = Event()
    original_claim = PrReviewCycleRepository.claim_strong_audit
    first_thread = []

    def claim(repository, *args, **kwargs):
        if current_thread().name == first_thread[0]:
            captured.set()
            assert release.wait(10), "competing controller did not finish its refusal"
        return original_claim(repository, *args, **kwargs)

    def first():
        first_thread.append(current_thread().name)
        return s.explicit(force=True)

    monkeypatch.setattr(PrReviewCycleRepository, "claim_strong_audit", claim)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert captured.wait(10), "first controller never captured the selected wait"
            s.used = 100.0
            refused = s.explicit(force=True)
            assert refused["target_outcome"] == "deferred", refused
            newer = s.snapshot()
            assert newer.retry_not_before == s.reset
            count = len(s.observations)
            history = s.history()
            s.used = 80.0
        finally:
            release.set()
        stale = future.result(timeout=10)
    assert "wait changed before admission" in " ".join(stale["target_actions"]), stale
    assert not s.reviews
    assert len(s.observations) == count
    assert s.snapshot().retry_not_before == newer.retry_not_before
    assert s.history() == history


def test_overlapping_explicit_origins_preserve_live_strong_owner(scenario, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    s = scenario
    s.explicit()
    s.configure("burst")
    observing = Event()
    release = Event()
    original_account = s.account

    def account(*args, **kwargs):
        observing.set()
        assert release.wait(10), "second controller did not contend with the live owner"
        return original_account(*args, **kwargs)

    monkeypatch.setattr("auto_coder.codex_usage_checker.read_account_data", account)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(s.explicit, force=True)
        try:
            assert observing.wait(10), "first controller never acquired its strong claim"
            owner = s.snapshot().active_claim
            assert owner is not None
            competing = s.explicit(force=True)
            assert "owns the live strong-audit claim" in " ".join(competing["target_actions"]), competing
            assert s.snapshot().active_claim == owner
            assert not s.reviews
        finally:
            release.set()
        result = future.result(timeout=10)
    assert len(s.reviews) == 1, result
    assert s.snapshot().accepted_strong_round is not None


def test_automatic_exact_four_day_boundary_has_future_queue_wake(scenario):
    s = scenario
    s.reset = s.clock + 4 * 86400
    engine = s.engine()
    s.dispatch(engine, invalidate=True)
    selected = s.snapshot()
    assert selected.retry_not_before == math.nextafter(s.clock, math.inf)
    queued = engine.invalidations.get_deferred(EntityIdentity("owner/repo", "pr", 100))
    assert queued.retry_not_before == s.clock + 1
    count = len(s.observations)
    s.dispatch(engine, invalidate=True)
    assert not s.reviews
    assert len(s.observations) == count
    assert s.history() == []
    s.clock = queued.retry_not_before
    s.dispatch(engine)
    assert len(s.reviews) == 1
    assert s.history()[0]["retry_not_before"] == selected.retry_not_before
    assert s.history()[0]["retry_not_before"] != queued.retry_not_before


def test_overlapping_automatic_dispatches_cannot_duplicate_strong_execution(scenario, monkeypatch):
    from threading import Event

    s = scenario
    first, second = s.engine(), s.engine()
    s.dispatch(first, invalidate=True)
    s.clock = s.snapshot().retry_not_before + 1
    observing, release = Event(), Event()
    original_account = s.account

    def account(*args, **kwargs):
        observing.set()
        assert release.wait(10), "competing automatic intake did not finish"
        return original_account(*args, **kwargs)

    monkeypatch.setattr("auto_coder.codex_usage_checker.read_account_data", account)

    async def overlap():
        await first._enqueue_pending_invalidations("owner/repo")
        worker = asyncio.create_task(first._worker_loop("owner/repo", 0))
        try:
            assert await asyncio.to_thread(observing.wait, 10)
            assert s.snapshot().active_claim is not None
            await second.invalidate_entity("owner/repo", "pr", 100)
            await second._enqueue_pending_invalidations("owner/repo")
            assert second.queue.empty()
            assert not s.reviews
        finally:
            release.set()
            await asyncio.wait_for(first.queue.join(), timeout=10)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    s.runner.run(overlap())
    assert len(s.reviews) == 1
    s.clock += 60
    s.dispatch(second)
    assert len(s.reviews) == 1
    assert s.snapshot().accepted_strong_round is not None
