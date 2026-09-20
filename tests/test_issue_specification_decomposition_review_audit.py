"""Production-boundary regression tests for Issue #1984 (Parent #1980).

Drives the real ``AutomationEngine._traced_validation_job`` / ``_schedule_parent_validations``
orchestration, the real ``SpecificationValidationLifecycle``/``DecompositionValidationLifecycle``
lifecycles (with an injected analyzer function standing in for the real LLM
parsing step, exactly like ``tests/test_issue_specification_validation_kill_switch.py``
and ``tests/test_issue_decomposition_validation_kill_switch.py`` already do),
and the real ``BackendManager``/``review_capture`` interaction-capture path
(a minimal client double at the same boundary
``tests/test_backend_manager_review_audit.py`` uses). Only the GitHub client
and the reviewer backend's raw response text are test doubles; the durable
``ReviewAuditStore`` audit persistence is a real temporary SQLite store, never
mocked.

Covers AS-001 through AS-004 and the REQ-007/AS-007 "audit store unwritable
never changes review lifecycle" spirit from the Issue body.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import auto_coder.review_capture.recorder as review_recorder
from auto_coder.automation_config import AutomationConfig
from auto_coder.automation_engine import AutomationEngine
from auto_coder.backend_manager import BackendManager
from auto_coder.dashboard_reviews import list_row, selection_error
from auto_coder.decomposition_analyzer import DecompositionAnalysisResult
from auto_coder.decomposition_validation_lifecycle import (
    DecompositionIssue,
    DecompositionValidationLifecycle,
)
from auto_coder.requirement_contract import build_normative_issue_manifest
from auto_coder.review_audit import EvaluationLifecycle, ExecutionMode, ReviewAuditStore
from auto_coder.specification_analyzer import SpecificationAnalysisResult, SpecificationFinding
from auto_coder.specification_validation_lifecycle import SpecificationValidationLifecycle
from auto_coder.validation_scheduler import ValidationScheduler

REPO = "owner/repo"

OBJECTIVE_A_BODY = "## Objective\nAdd feature X.\n\n## Requirements\n- REQ-001: Return the current value.\n"
OBJECTIVE_B_BODY = "## Objective\nAdd feature Y instead.\n\n## Requirements\n- REQ-001: Return the current value.\n"


# ---------------------------------------------------------------------------
# Real temporary audit store (never mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_audit_db(tmp_path: Path):
    db_root = tmp_path / "audit_db"
    db_root.mkdir(exist_ok=True)
    store = ReviewAuditStore(audit_root=db_root)
    store._ensure_db(REPO)
    review_recorder._global_audit_store = store
    yield store
    review_recorder._global_audit_store = None


def fresh_store_view(tmp_path: Path) -> ReviewAuditStore:
    """A brand-new ``ReviewAuditStore`` instance over the same on-disk files.

    Models "readable after a fresh-collector restart" (AS-001): no process
    state or singleton is reused, only the durable SQLite files on disk.
    """
    return ReviewAuditStore(audit_root=tmp_path / "audit_db")


# ---------------------------------------------------------------------------
# Minimal backend double (same boundary as test_backend_manager_review_audit.py)
# ---------------------------------------------------------------------------


class MockClient:
    def __init__(self, name: str, response: str = "ok", model: str = "test-model"):
        self.name = name
        self.response = response
        self.model_name = model
        self.session_id = None
        # A cloud backend type skips isolated_local_llm_worktree's real git
        # worktree machinery (backend_manager._execute_backend_with_providers),
        # which this test has no need to exercise.
        self.config_backend = MagicMock()
        self.config_backend.backend_type = "codex-cloud"

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        return self.response

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        return self.response

    def get_last_session_id(self):
        return self.session_id


@pytest.fixture
def mock_llm_config():
    with patch("auto_coder.backend_manager.get_llm_config") as mock_get_config:
        config = MagicMock()
        mock_get_config.return_value = config

        def _get_backend_config(name):
            bc = MagicMock()
            bc.backend_type = f"{name}-type"
            bc.usage_limit_retry_count = 0
            bc.always_switch_after_execution = False
            return bc

        config.get_backend_config.side_effect = _get_backend_config
        yield config


def make_backend_manager(mock_llm_config, response: str = "ok") -> BackendManager:
    client = MockClient("backend-A", response=response)
    manager = BackendManager(default_backend="backend-A", default_client=client, factories={"backend-A": lambda: client}, automatic_session_resume=False)
    manager._all_backends = ["backend-A"]
    manager._clients = {"backend-A": client}
    manager._get_or_create_client = lambda _name: client
    manager._initialization_lock = MagicMock()
    manager._instance_lock = MagicMock()
    return manager


# ---------------------------------------------------------------------------
# AS-001: a real EXECUTED READY round-trip readable after a fresh-collector
# restart.
# ---------------------------------------------------------------------------


class TestAS001ExecutedReadyRoundTrip:
    def test_executed_individual_ready_is_durable_after_restart(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config, response='{"verdict":"READY"}')

        def analyzer(manifest, body):
            # A real backend call is made while the review context is bound
            # (see run_traced_review), so this produces a genuine
            # ReviewInteractionRecord and the wrapper must classify EXECUTED.
            backend.run_prompt("review this issue")
            return SpecificationAnalysisResult("READY")

        lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", analyzer)
        manifest = build_normative_issue_manifest(101, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(101, "Title", OBJECTIVE_A_BODY)

        decision = AutomationEngine._traced_validation_job(
            REPO,
            101,
            "issue.individual-validation-job",
            "issue#101 individual validation job",
            {"issue_number": 101, "review_kind": "individual", "validation_identity": identity.key},
            lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert decision.verdict == "READY"
        assert decision.evaluation_source == "model"

        # Simulate a fresh collector/process restart: a brand-new store
        # instance over the same on-disk SQLite files.
        restarted = fresh_store_view(tmp_path)
        related = restarted.get_related_evaluations(REPO, "issue", "101", review_kind="issue_specification")
        assert related.health.value == "AVAILABLE"
        rows = [r for r in related.records if r.reviewed_generation == identity.key]
        assert len(rows) == 1
        row = rows[0]
        assert row.lifecycle == EvaluationLifecycle.FINISHED
        assert row.execution_mode == ExecutionMode.EXECUTED
        assert row.native_verdict == "READY"
        assert row.native_report is not None
        assert row.native_report["verdict"] == "READY"
        assert row.native_report["identity"]["issue_number"] == 101
        assert row.native_report["identity"]["policy_identity"] == identity.policy_identity
        assert row.native_report["identity"]["specification_digest"] == identity.specification_digest

        full = restarted.get_evaluation(REPO, row.review_id)
        assert full.record is not None
        assert len(full.record.interactions) == 1
        assert full.record.interactions[0].completion_status == "RETURNED"
        # Joined production-to-view oracle: the dashboard projection consumes
        # the row emitted by the real lifecycle/backend capture above, never a
        # hand-built expected audit row.
        dashboard_row = list_row(full.record)
        assert dashboard_row.mode == "EXECUTED"
        assert dashboard_row.verdict == "READY"
        assert dashboard_row.detail_path == f"/detail/issue/101?review_id={row.review_id}"
        assert selection_error(full.record, "issue", 101) is None


# ---------------------------------------------------------------------------
# AS-002: a REUSED round-trip with unchanged original timestamps, plus a
# legacy-decision-with-no-audit-association fallback.
# ---------------------------------------------------------------------------


class TestAS002ReusedRoundTrip:
    def test_reused_decision_links_to_original_executed_review_unchanged(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)
        calls = {"count": 0}

        def analyzer(manifest, body):
            calls["count"] += 1
            assert calls["count"] == 1, "analyzer must not be called a second time on a cache hit"
            backend.run_prompt("review this issue")
            return SpecificationAnalysisResult("READY")

        lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", analyzer)
        manifest = build_normative_issue_manifest(202, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(202, "Title", OBJECTIVE_A_BODY)
        facts = {"issue_number": 202, "review_kind": "individual", "validation_identity": identity.key}

        first = AutomationEngine._traced_validation_job(
            REPO,
            202,
            "issue.individual-validation-job",
            "issue#202 individual validation job",
            facts,
            lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert first.verdict == "READY"

        store = temp_audit_db
        first_related = store.get_related_evaluations(REPO, "issue", "202", review_kind="issue_specification")
        first_rows = [r for r in first_related.records if r.reviewed_generation == identity.key]
        assert len(first_rows) == 1
        first_row_before = first_rows[0]
        assert first_row_before.execution_mode == ExecutionMode.EXECUTED

        second = AutomationEngine._traced_validation_job(
            REPO,
            202,
            "issue.individual-validation-job",
            "issue#202 individual validation job",
            facts,
            lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert second.verdict == "READY"
        assert second.evaluation_source == "stored-decision-reuse"

        related = store.get_related_evaluations(REPO, "issue", "202", review_kind="issue_specification")
        rows = sorted((r for r in related.records if r.reviewed_generation == identity.key), key=lambda r: r.creation_sequence)
        assert len(rows) == 2
        first_row_after, second_row = rows
        # The original EXECUTED row is completely unchanged by the reuse.
        assert first_row_after.creation_time == first_row_before.creation_time
        assert first_row_after.execution_mode == ExecutionMode.EXECUTED
        assert first_row_after.native_report == first_row_before.native_report

        assert second_row.execution_mode == ExecutionMode.REUSED
        assert second_row.native_verdict == "READY"
        assert second_row.native_report is not None
        assert second_row.native_report["reuse_source_review_id"] == first_row_after.review_id
        assert second_row.source_review_id == first_row_after.review_id
        assert set(second_row.native_report["observation_times"]) == {"queued", "started", "terminal"}
        # No new interaction was captured for the reuse.
        second_full = store.get_evaluation(REPO, second_row.review_id)
        assert second_full.record is not None
        assert second_full.record.interactions == []

    def test_legacy_decision_with_no_audit_association_falls_back_to_none(self, temp_audit_db, tmp_path, mock_llm_config):
        """A decision persisted before this audit adapter existed has no
        instrumented producer: reuse must never fabricate an association."""
        analyzer = MagicMock(return_value=SpecificationAnalysisResult("READY"))
        legacy_lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", analyzer)
        manifest = build_normative_issue_manifest(303, "Title", OBJECTIVE_A_BODY)
        # Decide directly (bypassing _traced_validation_job entirely), exactly
        # as a pre-#1984 production build would have: the decision is
        # durably persisted, but no audit row is ever created for it.
        legacy_decision = legacy_lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY)
        assert legacy_decision.verdict == "READY"
        identity = legacy_lifecycle.identity(303, "Title", OBJECTIVE_A_BODY)

        assert temp_audit_db.get_related_evaluations(REPO, "issue", "303", review_kind="issue_specification").records == []

        # Now the audited wrapper observes this pre-existing durable decision
        # (a fresh SpecificationValidationLifecycle pointed at the same JSON
        # store, matching production's per-run lifecycle construction).
        observing_lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", MagicMock(side_effect=AssertionError("must not re-invoke the analyzer for a cache hit")))
        decision = AutomationEngine._traced_validation_job(
            REPO,
            303,
            "issue.individual-validation-job",
            "issue#303 individual validation job",
            {"issue_number": 303, "review_kind": "individual", "validation_identity": identity.key},
            lambda: observing_lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert decision.verdict == "READY"
        assert decision.evaluation_source == "stored-decision-reuse"

        related = temp_audit_db.get_related_evaluations(REPO, "issue", "303", review_kind="issue_specification")
        rows = [r for r in related.records if r.reviewed_generation == identity.key]
        assert len(rows) == 1
        assert rows[0].execution_mode == ExecutionMode.REUSED
        assert rows[0].source_review_id is None
        assert rows[0].native_report is not None
        assert rows[0].native_report["reuse_source_review_id"] is None


# ---------------------------------------------------------------------------
# AS-003: malformed backend output / local Objective-integrity refusal /
# disabled-review BYPASSED, each distinct.
# ---------------------------------------------------------------------------


class TestAS003DistinctNonReadyCases:
    def test_malformed_backend_output_is_executed_error(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config, response="not-json-at-all")

        def analyzer(manifest, body):
            # Mirrors analyze_issue_specification's real "backend responded,
            # but parsing the response failed" outcome.
            backend.run_prompt("review this issue")
            return SpecificationAnalysisResult("ERROR", error="response was not valid JSON")

        lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", analyzer)
        manifest = build_normative_issue_manifest(404, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(404, "Title", OBJECTIVE_A_BODY)

        decision = AutomationEngine._traced_validation_job(
            REPO,
            404,
            "issue.individual-validation-job",
            "issue#404 individual validation job",
            {"issue_number": 404, "review_kind": "individual", "validation_identity": identity.key},
            lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert decision.verdict == "ERROR"

        related = temp_audit_db.get_related_evaluations(REPO, "issue", "404", review_kind="issue_specification")
        rows = [r for r in related.records if r.reviewed_generation == identity.key]
        assert len(rows) == 1
        assert rows[0].execution_mode == ExecutionMode.EXECUTED
        assert rows[0].native_verdict == "ERROR"
        # ERROR is audit evidence only: it is never persisted to the durable
        # decision cache, so a subsequent decide() call re-runs the analyzer.
        assert not (tmp_path / "spec.json").exists() or "ERROR" not in (tmp_path / "spec.json").read_text()

    def test_local_objective_integrity_refusal_is_local_only_without_backend_call(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)
        analyzer = MagicMock(return_value=SpecificationAnalysisResult("READY"))

        def wrapped_analyzer(manifest, body):
            backend.run_prompt("review this issue")
            return analyzer(manifest, body)

        lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", wrapped_analyzer)
        manifest_a = build_normative_issue_manifest(505, "Title", OBJECTIVE_A_BODY)

        first = AutomationEngine._traced_validation_job(
            REPO,
            505,
            "issue.individual-validation-job",
            "issue#505 individual validation job",
            {"issue_number": 505, "review_kind": "individual"},
            lambda: lifecycle.decide(manifest_a, "Title", OBJECTIVE_A_BODY),
        )
        assert first.verdict == "READY"
        assert wrapped_analyzer is not analyzer  # sanity: distinct closures

        # A second, DIFFERENT identity (different body: the Objective text
        # changed) never reaches the analyzer at all: the real local
        # Objective-integrity check refuses it first.
        never_called = MagicMock(side_effect=AssertionError("must not invoke the analyzer for a local-only refusal"))
        refusing_lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", never_called)
        manifest_b = build_normative_issue_manifest(505, "Title", OBJECTIVE_B_BODY)
        changed_identity = refusing_lifecycle.identity(505, "Title", OBJECTIVE_B_BODY)

        second = AutomationEngine._traced_validation_job(
            REPO,
            505,
            "issue.individual-validation-job",
            "issue#505 individual validation job",
            {"issue_number": 505, "review_kind": "individual", "validation_identity": changed_identity.key},
            lambda: refusing_lifecycle.decide(manifest_b, "Title", OBJECTIVE_B_BODY),
        )
        assert second.verdict == "BLOCKED"
        assert second.evaluation_source == "local-only"
        never_called.assert_not_called()

        related = temp_audit_db.get_related_evaluations(REPO, "issue", "505", review_kind="issue_specification")
        rows = [r for r in related.records if r.reviewed_generation == changed_identity.key]
        assert len(rows) == 1
        assert rows[0].execution_mode == ExecutionMode.LOCAL_ONLY
        assert rows[0].native_verdict == "BLOCKED"
        second_full = temp_audit_db.get_evaluation(REPO, rows[0].review_id)
        assert second_full.record is not None
        assert second_full.record.interactions == []

    def test_disabled_decomposition_and_specification_are_recorded_bypassed(self, temp_audit_db, tmp_path):
        parent = {"number": 606, "title": "Parent", "body": "## Objective\nTrack work.\n", "state": "open"}
        children = [{"number": 607, "title": "Child", "body": OBJECTIVE_A_BODY, "state": "open"}]
        config = AutomationConfig(repo_name=REPO)
        config.issue_decomposition_validation = False
        config.issue_specification_validation = False
        engine = AutomationEngine(MagicMock(), config=config)

        set_job, child_jobs = engine._schedule_parent_validations(REPO, (parent, children), config)
        assert set_job is None
        assert child_jobs == {}

        decomposition_rows = temp_audit_db.get_related_evaluations(REPO, "issue", "606", review_kind="issue_decomposition").records
        assert len(decomposition_rows) == 1
        assert decomposition_rows[0].execution_mode == ExecutionMode.BYPASSED
        assert decomposition_rows[0].lifecycle == EvaluationLifecycle.FINISHED
        assert decomposition_rows[0].native_verdict is None

        individual_rows = temp_audit_db.get_related_evaluations(REPO, "issue", "607", review_kind="issue_specification").records
        assert len(individual_rows) == 1
        assert individual_rows[0].execution_mode == ExecutionMode.BYPASSED


# ---------------------------------------------------------------------------
# AS-004: a parent+open-child+closed-child decomposition record retaining
# both identities, and two waiters on one coalesced scheduler job producing
# exactly one review.
# ---------------------------------------------------------------------------


class TestAS004DecompositionIdentityAndCoalescing:
    def test_decomposition_record_retains_parent_and_both_children(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)

        def analyzer(parent, children):
            backend.run_prompt("review this decomposition")
            return DecompositionAnalysisResult("READY")

        lifecycle = DecompositionValidationLifecycle(REPO, "policy", tmp_path / "decomp.json", analyzer)
        parent = {"number": 700, "title": "Parent", "body": "## Objective\nTrack work.\n"}
        open_child = {"number": 701, "title": "Open child", "body": OBJECTIVE_A_BODY}
        closed_child = {"number": 702, "title": "Closed child", "body": OBJECTIVE_A_BODY, "state": "closed"}
        children = [open_child, closed_child]
        identity = lifecycle.identity(parent, children)

        parent_manifest = build_normative_issue_manifest(700, "Parent", parent["body"])
        child_inputs = [DecompositionIssue(build_normative_issue_manifest(c["number"], c["title"], c["body"]), c["body"]) for c in children]

        decision = AutomationEngine._traced_validation_job(
            REPO,
            700,
            "issue.decomposition-validation-job",
            "issue#700 decomposition validation job",
            {"parent_number": 700, "member_issue_numbers": [701, 702], "validation_identity": identity.key},
            lambda: lifecycle.decide(identity, DecompositionIssue(parent_manifest, parent["body"]), child_inputs),
        )
        assert decision.verdict == "READY"

        related = temp_audit_db.get_related_evaluations(REPO, "issue", "700", review_kind="issue_decomposition")
        rows = [r for r in related.records if r.reviewed_generation == identity.key]
        assert len(rows) == 1
        report = rows[0].native_report
        assert report is not None
        assert report["identity"]["parent"]["issue_number"] == 700
        reported_children = {c["issue_number"] for c in report["identity"]["children"]}
        assert reported_children == {701, 702}

    def test_two_waiters_on_one_coalesced_job_produce_exactly_one_review(self, temp_audit_db, tmp_path, mock_llm_config):
        release_event = threading.Event()
        backend = make_backend_manager(mock_llm_config)

        def slow_analyzer(parent, children):
            assert release_event.wait(timeout=10), "test setup deadlock"
            backend.run_prompt("review this decomposition")
            return DecompositionAnalysisResult("READY")

        lifecycle = DecompositionValidationLifecycle(REPO, "policy", tmp_path / "decomp.json", slow_analyzer)
        parent = {"number": 800, "title": "Parent", "body": "## Objective\nTrack work.\n"}
        child = {"number": 801, "title": "Child", "body": OBJECTIVE_A_BODY}
        children = [child]
        identity = lifecycle.identity(parent, children)
        parent_manifest = build_normative_issue_manifest(800, "Parent", parent["body"])
        child_inputs = [DecompositionIssue(build_normative_issue_manifest(child["number"], child["title"], child["body"]), child["body"])]

        scheduler = ValidationScheduler(concurrency=2)

        def submit() -> Any:
            return scheduler.submit(
                f"decomposition:{identity.key}",
                lambda: AutomationEngine._traced_validation_job(
                    REPO,
                    800,
                    "issue.decomposition-validation-job",
                    "issue#800 decomposition validation job",
                    {"parent_number": 800, "member_issue_numbers": [801], "validation_identity": identity.key},
                    lambda: lifecycle.decide(identity, DecompositionIssue(parent_manifest, parent["body"]), child_inputs),
                ),
            )

        job1 = submit()
        job2 = submit()
        assert job1.future is job2.future, "two overlapping submissions for the same identity must coalesce onto one future"

        release_event.set()
        decision1 = job1.result()
        decision2 = job2.result()
        assert decision1 is decision2
        assert decision1.verdict == "READY"

        related = temp_audit_db.get_related_evaluations(REPO, "issue", "800", review_kind="issue_decomposition")
        rows = [r for r in related.records if r.reviewed_generation == identity.key]
        assert len(rows) == 1, "exactly one producing review_id must exist for one coalesced scheduler job"
        assert rows[0].execution_mode == ExecutionMode.EXECUTED
        scheduler.shutdown(wait=True)


# ---------------------------------------------------------------------------
# REQ-007 / AS-007 spirit: audit store unwritable never changes review
# lifecycle, and a disabled-then-enabled sequence does not corrupt earlier
# history.
# ---------------------------------------------------------------------------


class TestAuditFailuresAreNonAuthorizing:
    def test_unwritable_audit_store_never_changes_the_returned_decision(self, tmp_path, mock_llm_config):
        class _ExplodingStore:
            def __getattr__(self, _name):
                raise RuntimeError("audit store is unavailable")

        with patch.object(review_recorder, "_global_audit_store", _ExplodingStore()):
            backend = make_backend_manager(mock_llm_config)

            def analyzer(manifest, body):
                backend.run_prompt("review this issue")
                return SpecificationAnalysisResult("READY")

            lifecycle = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "spec.json", analyzer)
            manifest = build_normative_issue_manifest(900, "Title", OBJECTIVE_A_BODY)

            decision = AutomationEngine._traced_validation_job(
                REPO,
                900,
                "issue.individual-validation-job",
                "issue#900 individual validation job",
                {"issue_number": 900, "review_kind": "individual"},
                lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
            )
            assert decision.verdict == "READY"

    def test_disabled_then_enabled_does_not_corrupt_earlier_bypassed_history(self, temp_audit_db, tmp_path, mock_llm_config):
        parent = {"number": 1000, "title": "Parent", "body": "## Objective\nTrack work.\n", "state": "open"}
        children: list = []
        config = AutomationConfig(repo_name=REPO)
        config.issue_decomposition_validation = False
        engine = AutomationEngine(MagicMock(), config=config)
        engine._schedule_parent_validations(REPO, (parent, children), config)

        bypassed_before = temp_audit_db.get_related_evaluations(REPO, "issue", "1000", review_kind="issue_decomposition").records
        assert len(bypassed_before) == 1
        assert bypassed_before[0].execution_mode == ExecutionMode.BYPASSED

        backend = make_backend_manager(mock_llm_config)

        def analyzer(parent_issue, child_issues):
            backend.run_prompt("review this decomposition")
            return DecompositionAnalysisResult("READY")

        config.issue_decomposition_validation = True
        engine._decomposition_validators[REPO] = DecompositionValidationLifecycle(REPO, "policy", tmp_path / "decomp.json", analyzer)
        job, _ = engine._schedule_parent_validations(REPO, (parent, children), config)
        assert job is not None
        decision = job.result()
        assert decision.verdict == "READY"

        rows = temp_audit_db.get_related_evaluations(REPO, "issue", "1000", review_kind="issue_decomposition").records
        assert len(rows) == 2
        # The original BYPASSED row is untouched.
        original = next(r for r in rows if r.review_id == bypassed_before[0].review_id)
        assert original.execution_mode == ExecutionMode.BYPASSED
        assert original.creation_time == bypassed_before[0].creation_time
        executed = next(r for r in rows if r.review_id != bypassed_before[0].review_id)
        assert executed.execution_mode == ExecutionMode.EXECUTED


class TestRequiredFailureAndOrderingRegressions:
    """Production-boundary regressions required by REQ-009/AS-005..AS-007."""

    def test_authorization_store_failure_retains_ready_and_original_exception(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)
        lifecycle = SpecificationValidationLifecycle(
            REPO,
            "policy",
            tmp_path / "authorization.json",
            lambda *_args: (backend.run_prompt("review"), SpecificationAnalysisResult("READY"))[1],
        )
        manifest = build_normative_issue_manifest(1100, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(1100, "Title", OBJECTIVE_A_BODY)
        failure = OSError("authoritative store unavailable")

        with patch.object(lifecycle.store, "save", side_effect=failure):
            with pytest.raises(OSError, match="authoritative store unavailable") as raised:
                AutomationEngine._traced_validation_job(
                    REPO,
                    1100,
                    "issue.individual-validation-job",
                    "issue#1100 individual validation job",
                    {"validation_identity": identity.key, "audit_identity": identity},
                    lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
                )

        assert raised.value is failure
        row = temp_audit_db.find_finished_evaluation(REPO, "issue", "1100", "issue_specification", identity.key, (ExecutionMode.EXECUTED,))
        assert row is not None
        assert row.native_verdict == "READY"
        assert row.native_report is not None
        assert row.native_report["authorization_persistence"] == {
            "disposition": "failed",
            "error": "authoritative store unavailable",
        }
        assert lifecycle.store.get(identity) is None

    def test_late_g1_completion_never_rebinds_g2_audit_generation(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)
        g1_release = threading.Event()
        g1_started = threading.Event()

        def g1_analyzer(*_args):
            g1_started.set()
            assert g1_release.wait(timeout=10)
            backend.run_prompt("review G1")
            return SpecificationAnalysisResult("READY")

        g1 = SpecificationValidationLifecycle(REPO, "policy", tmp_path / "g1.json", g1_analyzer)
        g2 = SpecificationValidationLifecycle(
            REPO,
            "policy",
            tmp_path / "g2.json",
            lambda *_args: (backend.run_prompt("review G2"), SpecificationAnalysisResult("READY"))[1],
        )
        m1 = build_normative_issue_manifest(1101, "Title", OBJECTIVE_A_BODY)
        m2 = build_normative_issue_manifest(1101, "Title", OBJECTIVE_B_BODY)
        i1 = g1.identity(1101, "Title", OBJECTIVE_A_BODY)
        i2 = g2.identity(1101, "Title", OBJECTIVE_B_BODY)
        scheduler = ValidationScheduler(2)
        try:
            job1 = scheduler.submit(
                i1.key,
                lambda: AutomationEngine._traced_validation_job(
                    REPO,
                    1101,
                    "issue.individual-validation-job",
                    "G1",
                    {"validation_identity": i1.key, "audit_identity": i1},
                    lambda: g1.decide(m1, "Title", OBJECTIVE_A_BODY),
                ),
            )
            assert g1_started.wait(timeout=10)
            job2 = scheduler.submit(
                i2.key,
                lambda: AutomationEngine._traced_validation_job(
                    REPO,
                    1101,
                    "issue.individual-validation-job",
                    "G2",
                    {"validation_identity": i2.key, "audit_identity": i2},
                    lambda: g2.decide(m2, "Title", OBJECTIVE_B_BODY),
                ),
            )
            assert job2.result().identity == i2
            g1_release.set()
            assert job1.result().identity == i1
        finally:
            g1_release.set()
            scheduler.shutdown()

        rows = temp_audit_db.get_related_evaluations(REPO, "issue", "1101", review_kind="issue_specification").records
        assert {row.reviewed_generation for row in rows} == {i1.key, i2.key}
        assert all(row.native_report["identity"]["specification_digest"] in {i1.specification_digest, i2.specification_digest} for row in rows)

    def test_unavailable_middle_read_does_not_replace_known_producer(self, temp_audit_db, tmp_path, mock_llm_config):
        backend = make_backend_manager(mock_llm_config)
        lifecycle = SpecificationValidationLifecycle(
            REPO,
            "policy",
            tmp_path / "known.json",
            lambda *_args: (backend.run_prompt("review known"), SpecificationAnalysisResult("READY"))[1],
        )
        manifest = build_normative_issue_manifest(1102, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(1102, "Title", OBJECTIVE_A_BODY)
        facts = {"validation_identity": identity.key, "audit_identity": identity}
        first = AutomationEngine._traced_validation_job(REPO, 1102, "issue.individual-validation-job", "known", facts, lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY))
        assert first.verdict == "READY"
        producer = temp_audit_db.find_finished_evaluation(REPO, "issue", "1102", "issue_specification", identity.key, (ExecutionMode.EXECUTED,))
        assert producer is not None

        original_get = lifecycle.store.get
        with patch.object(lifecycle.store, "get", side_effect=OSError("authoritative evidence unavailable")):
            with pytest.raises(OSError, match="authoritative evidence unavailable"):
                AutomationEngine._traced_validation_job(REPO, 1102, "issue.individual-validation-job", "unavailable", facts, lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY))
        assert original_get(identity) is not None
        restored = AutomationEngine._traced_validation_job(REPO, 1102, "issue.individual-validation-job", "restored", facts, lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY))
        assert restored.evaluation_source == "stored-decision-reuse"
        rows = temp_audit_db.get_related_evaluations(REPO, "issue", "1102", review_kind="issue_specification").records
        reused = max((row for row in rows if row.execution_mode == ExecutionMode.REUSED), key=lambda row: row.creation_sequence)
        assert reused.source_review_id == producer.review_id
        assert len([row for row in rows if row.execution_mode == ExecutionMode.EXECUTED]) == 1

    def test_pending_publication_handler_appends_effect_to_original_review(self, temp_audit_db, tmp_path, mock_llm_config, monkeypatch):
        from auto_coder.automation_engine import _ValidationPublicationStageHandler
        from auto_coder.github_pending_work import PendingObligation, PendingReason, WorkIdentity

        backend = make_backend_manager(mock_llm_config)
        finding = SpecificationFinding("material_ambiguity", ("REQ-001",), "Undefined value", "Define it", "", "")
        lifecycle = SpecificationValidationLifecycle(
            REPO,
            "policy",
            tmp_path / "blocked.json",
            lambda *_args: (backend.run_prompt("review blocked"), SpecificationAnalysisResult("BLOCKED", (finding,)))[1],
        )
        manifest = build_normative_issue_manifest(1103, "Title", OBJECTIVE_A_BODY)
        identity = lifecycle.identity(1103, "Title", OBJECTIVE_A_BODY)
        decision = AutomationEngine._traced_validation_job(
            REPO,
            1103,
            "issue.individual-validation-job",
            "blocked",
            {"validation_identity": identity.key, "audit_identity": identity},
            lambda: lifecycle.decide(manifest, "Title", OBJECTIVE_A_BODY),
        )
        assert decision.verdict == "BLOCKED"
        producer = temp_audit_db.find_finished_evaluation(REPO, "issue", "1103", "issue_specification", identity.key, (ExecutionMode.EXECUTED,))
        assert producer is not None

        engine = MagicMock()
        engine.github.get_issue_dispatch_snapshot_strict.return_value = {
            "number": 1103,
            "title": "Title",
            "body": OBJECTIVE_A_BODY,
        }
        engine._get_specification_validator.return_value = lifecycle
        engine._get_authoritative_parent_number.return_value = None
        lifecycle.apply_blocked = MagicMock(return_value=None)
        monkeypatch.setattr("auto_coder.automation_engine.get_pending_work_store", lambda: MagicMock(get=lambda _identity: None))
        obligation = PendingObligation(
            WorkIdentity(REPO, "issue:1103", "validation-publication", identity.key),
            PendingReason.THROTTLED,
            0,
            ("diagnostic",),
        )
        outcome = _ValidationPublicationStageHandler(engine, REPO)._run_impl(obligation, 1103)
        assert outcome.completed_effects == ("diagnostic",)

        full = temp_audit_db.get_evaluation(REPO, producer.review_id).record
        assert full is not None
        assert [(effect.disposition, effect.details) for effect in full.effects] == [("unknown", {"recovery": True, "error": None})]

    def test_recovery_effect_uses_source_unavailable_observation_for_legacy_decision(self, temp_audit_db):
        from auto_coder.review_capture.issue_review_audit import record_effect

        record_effect(
            repository=REPO,
            target_number=1103,
            review_kind="issue_specification",
            generation_key="legacy-generation",
            policy_identity="policy",
            disposition="unknown",
            details={"recovery": True},
        )
        rows = temp_audit_db.get_related_evaluations(REPO, "issue", "1103", review_kind="issue_specification").records
        assert len(rows) == 1
        assert rows[0].execution_mode == ExecutionMode.REUSED
        assert rows[0].native_report == {"source_unavailable": True}
        full = temp_audit_db.get_evaluation(REPO, rows[0].review_id).record
        assert full is not None
        assert [(effect.disposition, effect.details) for effect in full.effects] == [("unknown", {"recovery": True})]
