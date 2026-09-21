"""Production-boundary regression tests for Issue #1985.

These tests drive the real PR-processing orchestration (`_handle_pr_merge`),
the real `run_adversarial_validation` context assembly/parsing, and the real
`BackendManager` invocation path. Only true I/O boundaries are controlled:
the GitHub API (a `MagicMock` client), the worktree provisioning helper
(replaced with a real local git checkout), the merge/CI gates that are
irrelevant to this feature, and the reviewer LLM backend's raw response text
(a minimal client double at the same boundary `test_backend_manager_review_audit.py`
uses). Interaction recording, review-context binding, and durable audit
persistence are never mocked: they run for real against a temporary SQLite
audit store.

REQ-010 through REQ-013 (see Issue #1985) define these fixtures literally;
each test below is named after the fixture/scenario it drives.
"""

from __future__ import annotations

import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from auto_coder.adversarial_validator import AdversarialValidationResult
from auto_coder.automation_config import AutomationConfig
from auto_coder.backend_manager import BackendManager
from auto_coder.cli_helpers import AdversarialValidationAvailability
from auto_coder.dashboard_reviews import list_row
from auto_coder.exceptions import AutoCoderUsageLimitError
from auto_coder.github_app_reviewer import ReviewPublicationResult
from auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from auto_coder.pr_processor import TwoTierGateInputs, _handle_pr_merge
from auto_coder.pr_review_cycle import (
    VERDICT_FINDINGS,
    ContractSnapshot,
    Finding,
    FindingDisposition,
    NotApplicableError,
    PrReviewCycleRepository,
    RoundProvenance,
    StrongPolicyIdentity,
)
from auto_coder.review_audit import EvaluationLifecycle, ExecutionMode, ReviewAuditStore
from auto_coder.review_capture import recorder as review_recorder
from auto_coder.two_tier_pr_gate import TwoTierPrGate
from auto_coder.util.github_action import GitHubActionsStatusResult

REPO_NAME = "owner/repo"
PR_NUMBER = 4242
ISSUE_NUMBER = 99

VALID_ISSUE_BODY = '## Objective\nAdd a greet() function that returns the string "hello".\n\n## Requirements\nREQ-001: greet() returns the string hello.\n'

DUPLICATE_REQUIREMENT_ISSUE_BODY = '## Objective\nAdd a greet() function that returns the string "hello".\n\n## Requirements\nREQ-001: greet() returns the string hello.\nREQ-001: greet() must not raise.\n'

PASS_PAYLOAD = (
    '{"result":"PASS","summary":"greet() returns hello at the reviewed head.",'
    '"findings":[],"requirement_coverage":[{"requirement_id":"REQ-001","status":"VERIFIED",'
    '"evidence":"sample.py: greet() returns hello."}],"specification_gaps":[],'
    '"test_oracle_gaps":[],"thread_dispositions":[],"dynamic_check_requested":null}'
)

NON_JSON_PAYLOAD = "not-json"


@pytest.fixture(autouse=True)
def _real_commands(_use_real_commands):
    """This module drives real git/bash boundaries; never stub them (see conftest)."""


# ---------------------------------------------------------------------------
# Real local-git fixture (the "isolated worktree verifiably at H" from REQ-010)
# ---------------------------------------------------------------------------


def _git(args: List[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _build_pr_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real git repository whose tip (H) implements greet() -> "hello"."""
    repo = tmp_path / "pr-repo"
    repo.mkdir(parents=True)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "PR Author"], repo)
    (repo / "sample.py").write_text("def greet():\n    return 'placeholder'\n")
    _git(["add", "sample.py"], repo)
    _git(["commit", "-q", "-m", "base"], repo)

    (repo / "sample.py").write_text('def greet():\n    return "hello"\n')
    _git(["add", "sample.py"], repo)
    _git(["commit", "-q", "-m", "implement greet"], repo)
    head_sha = _git(["rev-parse", "HEAD"], repo)
    return repo, head_sha


def _add_dynamic_check_script(repo: Path, head_sha: str) -> str:
    """A real, tiny, always-succeeding test-command boundary (REQ-012).

    Amends the fixture's final commit so the returned head SHA still matches
    the exact reviewed revision used everywhere else in the fixture.
    """
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text("def test_greet():\n    assert True\n")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    script = scripts_dir / "test.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o755)
    _git(["add", "tests", "scripts"], repo)
    _git(["commit", "-q", "-m", "add focused test target", "--amend", "--no-edit"], repo)
    return _git(["rev-parse", "HEAD"], repo)


PR_DIFF = "diff --git a/sample.py b/sample.py\nindex e69de29..1234567 100644\n--- a/sample.py\n+++ b/sample.py\n@@ -1,2 +1,2 @@\n-def greet():\n-    return 'placeholder'\n+def greet():\n+    return \"hello\"\n"


def _build_github_client(head_sha: str, issue_body: str = VALID_ISSUE_BODY) -> MagicMock:
    client = MagicMock()
    client.get_pr_diff.return_value = PR_DIFF
    client.get_pr_changed_file_count.return_value = 1
    client.get_issue.return_value = {"number": ISSUE_NUMBER, "title": "Add greet()", "body": issue_body, "state": "open"}
    client.get_parent_issue_details.return_value = None
    client.get_pr_comments.return_value = []
    client.get_pr_reviews_strict.return_value = []
    client.get_pull_request.return_value = {"head": {"sha": head_sha}}
    client.get_pull_request_head_sha_strict.return_value = head_sha
    return client


def _build_pr_data(head_sha: str) -> Dict[str, object]:
    return {
        "number": PR_NUMBER,
        "title": "Implement greet()",
        "body": f"Fixes #{ISSUE_NUMBER}",
        "labels": [],
        "user": {"login": "accepted-author"},
        "head": {"ref": "feature-branch", "sha": head_sha},
        "base": {"ref": "main"},
    }


def _build_config() -> AutomationConfig:
    config = AutomationConfig()
    config.AUTO_MERGE = True
    config.ENABLE_ADVERSARIAL_VALIDATION = True
    config.pr_adversarial_validation = True
    config.MAX_ADVERSARIAL_VALIDATIONS = 100
    return config


@contextmanager
def _static_worktree(path: Path):
    yield str(path)


class MockReviewerClient:
    """The one true I/O boundary: raw reviewer-backend response text."""

    def __init__(self, name: str, responses: Optional[List[str]] = None, session_id: Optional[str] = None, raise_once: Optional[Exception] = None):
        self.name = name
        self.model_name = f"{name}-model"
        self._responses = list(responses or [])
        self._session_id = session_id
        self._raise_once = raise_once
        self.config_backend = MagicMock()
        self.config_backend.backend_type = "codex-cloud"  # a cloud type: skip local-worktree isolation
        self.calls: List[str] = []

    def _next(self) -> str:
        if not self._responses:
            raise AssertionError(f"{self.name}: no scripted reviewer response left")
        return self._responses.pop(0)

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        self.calls.append("fresh")
        if self._raise_once is not None:
            exc, self._raise_once = self._raise_once, None
            raise exc
        return self._next()

    def continue_session(self, session_id: str, prompt: str, is_noedit: bool = False) -> str:
        self.calls.append("continue")
        return self._next()

    def get_last_session_id(self) -> Optional[str]:
        return self._session_id


def _build_backend_manager(monkeypatch: pytest.MonkeyPatch, clients: Dict[str, MockReviewerClient], default_name: str) -> BackendManager:
    def _get_backend_config(name: str):
        bc = MagicMock()
        bc.backend_type = "codex-cloud"
        bc.usage_limit_retry_count = 0
        bc.always_switch_after_execution = False
        return bc

    llm_config = MagicMock()
    llm_config.get_backend_config.side_effect = _get_backend_config
    monkeypatch.setattr("auto_coder.backend_manager.get_llm_config", lambda: llm_config)

    names = list(clients.keys())
    manager = BackendManager(default_backend=default_name, default_client=clients[default_name], factories={name: (lambda name=name: clients[name]) for name in names}, automatic_session_resume=False)
    manager._all_backends = names
    manager._clients = dict(clients)
    manager._get_or_create_client = lambda name: manager._clients[name]
    manager._initialization_lock = MagicMock()
    manager._instance_lock = MagicMock()
    return manager


@pytest.fixture
def audit_store(tmp_path, monkeypatch):
    """A fresh, isolated durable audit store for one test."""
    store = ReviewAuditStore(audit_root=tmp_path / "review_audit")
    monkeypatch.setattr(review_recorder, "_global_audit_store", store)
    yield store
    monkeypatch.setattr(review_recorder, "_global_audit_store", None)


def _apply_standard_merge_gates(monkeypatch: pytest.MonkeyPatch, *, mergeable: bool = True, merge_result: bool = False) -> None:
    monkeypatch.setattr("auto_coder.pr_processor.check_github_actions_and_exit_if_in_progress", lambda **kwargs: True)
    monkeypatch.setattr("auto_coder.pr_processor._get_mergeable_state", lambda *a, **k: {"mergeable": mergeable, "merge_state_status": "clean" if mergeable else "dirty"})
    monkeypatch.setattr("auto_coder.pr_processor._check_github_actions_status", lambda *a, **k: GitHubActionsStatusResult(success=True, ids=[1]))
    monkeypatch.setattr("auto_coder.pr_processor.has_unresolved_review_threads", lambda *a, **k: False)
    monkeypatch.setattr("auto_coder.pr_processor._merge_pr", lambda *a, **k: merge_result)


def _wire_backend(monkeypatch: pytest.MonkeyPatch, manager: BackendManager) -> None:
    monkeypatch.setattr(
        "auto_coder.cli_helpers.resolve_adversarial_validation_availability",
        lambda validation_kind=None: AdversarialValidationAvailability(backend_manager=manager),
    )


def _get_only_evaluation(store: ReviewAuditStore, repository: str):
    history = store.get_recent_history(repository, limit=50)
    assert history.records, "expected at least one durable evaluation record"
    return history.records


# ---------------------------------------------------------------------------
# REQ-010 / AS-001 / AS-002: one-call execution (PASS and malformed ERROR)
# ---------------------------------------------------------------------------


class TestReq010OneCallExecution:
    def test_muse_peer_commit_reaches_publication_with_original_head_and_attempt(self, tmp_path, monkeypatch, audit_store):
        """Issue #2192/REQ-008: peer refs cannot replace a real PR verdict."""
        repo, head_sha = _build_pr_repo(tmp_path)
        validation_worktree = tmp_path / "validation-worktree"
        _git(["worktree", "add", "--detach", str(validation_worktree), head_sha], repo)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        ready = tmp_path / "muse-ready"
        release = tmp_path / "muse-release"
        invocation_count = tmp_path / "muse-invocations"
        executable = tmp_path / "muse-reviewer"
        executable.write_text(
            """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Muse Code test")
    raise SystemExit(0)
count_path = Path(os.environ["MUSE_INVOCATION_COUNT"])
count_path.write_text(count_path.read_text() + "1\\n" if count_path.exists() else "1\\n")
Path(os.environ["MUSE_READY"]).write_text("ready")
while not Path(os.environ["MUSE_RELEASE"]).exists():
    time.sleep(0.01)
print(os.environ["MUSE_VERDICT"])
"""
        )
        executable.chmod(0o700)
        backend_config = LLMBackendConfiguration(
            backends={
                "muse-reviewer": BackendConfig(
                    name="muse-reviewer",
                    backend_type="muse",
                    model="muse-spark-1.3",
                    options_for_noedit=["--no-edit"],
                )
            }
        )
        monkeypatch.setenv("AUTOCODER_MUSE_CLI", str(executable))
        monkeypatch.setenv("MUSE_READY", str(ready))
        monkeypatch.setenv("MUSE_RELEASE", str(release))
        monkeypatch.setenv("MUSE_INVOCATION_COUNT", str(invocation_count))
        monkeypatch.setenv("MUSE_VERDICT", PASS_PAYLOAD)

        from auto_coder.cli_helpers import build_backend_manager

        with (
            patch("auto_coder.cli_helpers.get_llm_config", return_value=backend_config),
            patch("auto_coder.muse_client.get_llm_config", return_value=backend_config),
        ):
            manager = build_backend_manager(
                ["muse-reviewer"],
                "muse-reviewer",
                {"muse-reviewer": "muse-spark-1.3"},
                use_noedit_options=True,
            )

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        merge_gate = MagicMock(return_value=False)
        monkeypatch.setattr("auto_coder.pr_processor._merge_pr", merge_gate)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr(
            "auto_coder.pr_processor.isolated_pr_head_worktree",
            lambda *a, **k: _static_worktree(validation_worktree),
        )
        publications: list[tuple[str, int, str, AdversarialValidationResult]] = []

        def publish(repo_name, pr_number, published_head, result, **_kwargs):
            publications.append((repo_name, pr_number, published_head, result))
            return ReviewPublicationResult(True, "APPROVE", "")

        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", publish)
        outcomes: list[object] = []

        def process_pr() -> None:
            try:
                outcomes.append(_handle_pr_merge(client, REPO_NAME, pr_data, config, {}))
            except BaseException as exc:
                outcomes.append(exc)

        worker = threading.Thread(target=process_pr)
        worker.start()
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), f"Muse did not pause after its invocation snapshot: {outcomes!r}"

        (repo / "peer.txt").write_text("peer progress\n")
        _git(["add", "peer.txt"], repo)
        _git(["commit", "-q", "-m", "peer commit"], repo)
        peer_head = _git(["rev-parse", "HEAD"], repo)
        release.write_text("continue")
        worker.join(timeout=10)

        assert not worker.is_alive()
        assert len(outcomes) == 1 and not isinstance(outcomes[0], BaseException)
        actions = outcomes[0]
        assert invocation_count.read_text().splitlines() == ["1"]
        assert len(publications) == 1
        published_repo, published_pr, published_head, published_result = publications[0]
        assert (published_repo, published_pr, published_head) == (REPO_NAME, PR_NUMBER, head_sha)
        assert published_result.result == "PASS"
        assert published_result.summary == "greet() returns hello at the reviewed head."
        assert published_result.attempt_id
        assert published_result.attempt_sequence == 1
        assert _git(["rev-parse", "HEAD"], validation_worktree) == head_sha
        assert _git(["rev-parse", "HEAD"], repo) == peer_head
        assert any("Published APPROVE adversarial review" in action for action in actions)
        merge_gate.assert_called_once()
        assert not any("Successfully merged" in action for action in actions)

    def test_pass_payload_is_one_executed_review_with_one_invocation(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        # Independent business assertions, made without touching audit data.
        assert reviewer.calls == ["fresh"]
        assert any("Published APPROVE adversarial review" in action for action in actions)

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.review_kind == "pr_adversarial"
        assert record.target_type == "pr"
        assert record.target_number == str(PR_NUMBER)
        assert record.reviewed_generation == head_sha
        assert record.lifecycle == EvaluationLifecycle.FINISHED
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert record.native_verdict == "PASS"
        assert record.native_report is not None
        assert record.native_report["result"] == "PASS"
        assert len(record.interactions) == 1
        assert record.interactions[0].completion_status == "RETURNED"
        assert record.interactions[0].backend_alias == "reviewer"

        # An independent merge refusal does not remove the retained review.
        assert not any("Successfully merged" in action for action in actions)
        reread = audit_store.get_evaluation(REPO_NAME, record.review_id)
        assert reread.record is not None
        assert reread.record.native_verdict == "PASS"
        dashboard_row = list_row(reread.record)
        assert dashboard_row.verdict == "PASS"
        assert dashboard_row.mode == "EXECUTED"
        assert dashboard_row.detail_path == f"/detail/pr/{PR_NUMBER}?review_id={record.review_id}"

    def test_non_json_response_is_one_executed_review_with_native_error(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[NON_JSON_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert record.native_verdict != "PASS"
        assert len(record.interactions) == 1
        assert list_row(record).verdict == record.native_verdict

    def test_pass_with_specification_gaps_retains_both(self, tmp_path, monkeypatch, audit_store):
        """AS-002: a PASS report with additional blocking specification gaps is
        retained in full; the audit never presents unconditional merge
        readiness from the bare PASS verdict alone."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        payload = (
            '{"result":"PASS","summary":"greet() returns hello, but scope is unclear.",'
            '"findings":[],"requirement_coverage":[{"requirement_id":"REQ-001","status":"VERIFIED",'
            '"evidence":"sample.py: greet() returns hello."}],'
            '"specification_gaps":[{"question":"Should greet() accept a name argument?",'
            '"why_existing_issue_is_insufficient":"The Issue never mentions parameters.",'
            '"observed_case":"greet() takes no arguments in this diff.",'
            '"affected_scope":"Public greet() signature",'
            '"candidate_options":["Keep greet() parameterless","Add an optional name parameter"]}],'
            '"test_oracle_gaps":[],"thread_dispositions":[],"dynamic_check_requested":null}'
        )
        reviewer = MockReviewerClient("reviewer", responses=[payload])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.native_verdict == "PASS"
        assert record.native_report is not None
        assert len(record.native_report["specification_gaps"]) == 1
        assert record.native_report["specification_gaps"][0]["question"] == "Should greet() accept a name argument?"
        # An unresolved specification gap must not become an unconditional
        # automatic merge (production policy, not this adapter's decision).
        assert not any("Successfully merged" in action for action in actions)

    def test_needs_fix_report_is_retained_faithfully(self, tmp_path, monkeypatch, audit_store):
        """AS-002/REQ-003: a valid NEEDS_FIX report (a demonstrated finding)
        is retained in full through real parsing/normalization; merge
        approval is never synthesized for a non-PASS verdict."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        payload = (
            '{"result":"NEEDS_FIX","summary":"Found 1 specification violation in greet() output.",'
            '"findings":[{'
            '"finding_identity":"greet-return-value",'
            '"correction_identity":"greet-return-value-fix",'
            '"violated_requirement":"greet() returns the string hello",'
            '"requirement_id":"REQ-001",'
            '"evidence_classification":"DEMONSTRATED",'
            '"reachability":"The public greet() function is called directly and its return value is observable",'
            '"required_behavior":"greet() must return the exact string \'hello\'",'
            '"actual_behavior":"greet() returns a different string on this head",'
            '"evidence":"sample.py shows greet() returning a value other than \'hello\'",'
            "\"counterexample\":\"Calling greet() returns 'hell0' instead of 'hello'\","
            '"test_gap":"No existing test asserts the exact return value of greet()",'
            '"suggested_regression_scenario":"Assert greet() == \'hello\' exactly",'
            '"anchor_path":"sample.py"'
            "}],"
            '"specification_gaps":[],"test_oracle_gaps":[],"thread_dispositions":[],"dynamic_check_requested":null}'
        )
        reviewer = MockReviewerClient("reviewer", responses=[payload])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]
        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert record.native_verdict == "NEEDS_FIX"
        assert record.native_report is not None
        assert len(record.native_report["findings"]) == 1
        finding = record.native_report["findings"][0]
        assert finding["violated_requirement"] == "greet() returns the string hello"
        assert finding["counterexample"] == "Calling greet() returns 'hell0' instead of 'hello'"
        assert finding["evidence_classification"] == "DEMONSTRATED"
        assert not any("Successfully merged" in action for action in actions)

    def test_inconclusive_report_is_retained_faithfully(self, tmp_path, monkeypatch, audit_store):
        """AS-002/REQ-003: a valid INCONCLUSIVE report (bounded evidence
        recovery plus a scoped decision-critical evidence gap) is retained in
        full; INCONCLUSIVE never becomes an approved/merged PR."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        payload = (
            '{"result":"INCONCLUSIVE","summary":"greet() behavior could not be fully confirmed on the reviewed head.",'
            '"findings":[],'
            '"requirement_coverage":[{"requirement_id":"REQ-001","status":"UNVERIFIED","evidence":"sample.py could not be fully inspected."}],'
            '"specification_gaps":[],"test_oracle_gaps":[],"thread_dispositions":[],"dynamic_check_requested":null,'
            '"evidence_recovery":[{"path":"sample.py","source":"repository inspection","status":"UNAVAILABLE",'
            '"evidence":"Attempted inspection did not resolve REQ-001.","requirement_ids":["REQ-001"]}],'
            '"decision_critical_evidence_gaps":[{"requirement_id":"REQ-001",'
            '"evidence_needed":"Confirmed runtime behavior of greet()",'
            '"recovery_attempts":["Inspected sample.py source"]}]}'
        )
        reviewer = MockReviewerClient("reviewer", responses=[payload])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "COMMENT", ""))

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]
        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert record.native_verdict == "INCONCLUSIVE"
        assert record.native_report is not None
        assert record.native_report["result"] == "INCONCLUSIVE"
        assert len(record.native_report["decision_critical_evidence_gaps"]) == 1
        assert record.native_report["decision_critical_evidence_gaps"][0]["requirement_id"] == "REQ-001"
        assert len(record.native_report["evidence_recovery"]) == 1
        assert not any("Successfully merged" in action for action in actions)


# ---------------------------------------------------------------------------
# REQ-011 / AS-003: reuse, legacy reuse, LOCAL_ONLY, BYPASSED
# ---------------------------------------------------------------------------


class TestReq011ReuseBypassLocalOnly:
    def test_post_feature_reuse_links_producing_review_with_zero_new_invocations(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})
        producer_records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(producer_records) == 1
        producer_review_id = producer_records[0].review_id
        assert reviewer.calls == ["fresh"]

        # Second run: the PR is unchanged and now has an authoritative published
        # PASS comment at the same head. No new reviewer invocation may occur.
        from auto_coder.adversarial_validator import format_adversarial_validation_comment

        client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS", summary="Previously verified"), head_sha)}]

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"], "reuse must not invoke the reviewer-backend again"

        records = audit_store.get_recent_history(REPO_NAME, limit=50).records
        assert len(records) == 2
        reused = next(r for r in records if r.review_id != producer_review_id)
        assert reused.execution_mode == ExecutionMode.REUSED
        assert reused.source_review_id == producer_review_id
        assert reused.reviewed_generation == head_sha

    def test_legacy_reuse_has_unavailable_source_provenance(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()

        from auto_coder.adversarial_validator import format_adversarial_validation_comment

        # An authoritative PASS exists, but no producing review was ever
        # instrumented (pre-existing/legacy result).
        client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS", summary="Legacy verified"), head_sha)}]

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("run_adversarial_validation must not be invoked on a reuse path")

        monkeypatch.setattr("auto_coder.pr_processor.run_adversarial_validation", _fail_if_called)

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        records = audit_store.get_recent_history(REPO_NAME, limit=50).records
        assert len(records) == 1
        assert records[0].execution_mode == ExecutionMode.REUSED
        assert records[0].source_review_id is None

    def test_duplicate_requirement_id_is_local_only_blocked_before_invocation(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha, issue_body=DUPLICATE_REQUIREMENT_ISSUE_BODY)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        # No reviewer client registered at all: any invocation attempt fails loudly.
        manager = MagicMock()
        manager.get_current_backend_identity.side_effect = AssertionError("backend must not be selected before the manifest gate")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.execution_mode == ExecutionMode.LOCAL_ONLY
        assert record.native_verdict == "BLOCKED"
        assert record.native_report is not None
        assert record.native_report["diagnostic_category"] == "invalid_requirement_contract"
        assert record.interactions == []

    def test_disabled_validation_is_bypassed_with_no_verdict(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        config.ENABLE_ADVERSARIAL_VALIDATION = False
        config.pr_adversarial_validation = False

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("run_adversarial_validation must not run while validation is disabled")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        monkeypatch.setattr("auto_coder.pr_processor.run_adversarial_validation", _fail_if_called)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.execution_mode == ExecutionMode.BYPASSED
        assert record.native_verdict is None
        assert record.interactions == []

    def test_forced_rereview_of_same_head_gets_a_distinct_review_id(self, tmp_path, monkeypatch, audit_store):
        """AS-001: a later explicit review of the same head, when production
        policy actually invokes the analyzer again (here: --force), has a
        distinct review_id despite reusing the same provider session."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD, PASS_PAYLOAD], session_id="reused-session")
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})
        first_records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(first_records) == 1
        first_review_id = first_records[0].review_id

        from auto_coder.adversarial_validator import format_adversarial_validation_comment

        client.get_pr_comments.return_value = [{"body": format_adversarial_validation_comment(AdversarialValidationResult(result="PASS", summary="Previously verified"), head_sha)}]

        # --force reaches a fresh current-head validation attempt even though
        # a same-head PASS is already durable (REQ-001/REQ-009/REQ-010).
        _handle_pr_merge(client, REPO_NAME, pr_data, config, {}, force_adversarial_validation=True)

        # The second run resumes the persisted provider session (a genuine
        # continuation), not a fresh call; the point of this fixture is that
        # session reuse alone still gets a brand-new review_id.
        assert reviewer.calls == ["fresh", "continue"]

        records = audit_store.get_recent_history(REPO_NAME, limit=10).records
        assert len(records) == 2
        second_review_id = next(r.review_id for r in records if r.review_id != first_review_id)
        assert second_review_id != first_review_id
        for record in records:
            assert record.execution_mode == ExecutionMode.EXECUTED
            assert record.native_verdict == "PASS"
            assert record.reviewed_generation == head_sha
        # The continuation call is explicitly bound to the reused session
        # identity, yet still belongs to the distinct second review_id.
        second_record = next(r for r in records if r.review_id == second_review_id)
        assert second_record.interactions[0].invocation_mode == "continuation"
        assert second_record.interactions[0].session_identity == "reused-session"


# ---------------------------------------------------------------------------
# REQ-012 / AS-004: multi-call fixtures (fallback, dynamic follow-up)
# ---------------------------------------------------------------------------


class TestReq012MultiCallFixtures:
    def test_backend_fallback_is_one_review_with_two_ordered_invocations(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()

        reviewer_a = MockReviewerClient("reviewer-a", raise_once=AutoCoderUsageLimitError("quota exceeded"))
        reviewer_b = MockReviewerClient("reviewer-b", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer-a": reviewer_a, "reviewer-b": reviewer_b}, "reviewer-a")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer_a.calls == ["fresh"]
        assert reviewer_b.calls == ["fresh"]

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.native_verdict == "PASS"
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert len(record.interactions) == 2
        assert record.interactions[0].backend_alias == "reviewer-a"
        assert record.interactions[0].completion_status == "RAISED"
        assert record.interactions[1].backend_alias == "reviewer-b"
        assert record.interactions[1].completion_status == "RETURNED"
        assert record.interactions[0].interaction_id != record.interactions[1].interaction_id
        dashboard_row = list_row(record)
        assert "reviewer-a" in dashboard_row.backend
        assert "reviewer-b" in dashboard_row.backend

    def test_dynamic_followup_is_one_review_with_two_ordered_invocations(self, tmp_path, monkeypatch, audit_store):
        repo, head_sha = _build_pr_repo(tmp_path)
        head_sha = _add_dynamic_check_script(repo, head_sha)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        config.TEST_SCRIPT_PATH = str(repo / "scripts" / "test.sh")

        initial_payload = (
            '{"result":"PASS","summary":"Needs a focused dynamic check.",'
            '"findings":[],"requirement_coverage":[{"requirement_id":"REQ-001","status":"VERIFIED",'
            '"evidence":"sample.py: greet() returns hello."}],"specification_gaps":[],'
            '"test_oracle_gaps":[],"thread_dispositions":[],'
            '"dynamic_check_requested":"tests/test_sample.py::test_greet"}'
        )
        followup_payload = PASS_PAYLOAD
        reviewer = MockReviewerClient("reviewer", responses=[initial_payload, followup_payload], session_id="session-S")
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh", "continue"]

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.native_verdict == "PASS"
        assert record.execution_mode == ExecutionMode.EXECUTED
        assert len(record.interactions) == 2
        assert record.interactions[0].invocation_mode == "fresh"
        assert record.interactions[1].invocation_mode == "continuation"
        assert record.interactions[1].session_identity == "session-S"
        # The explicit provider session and native attempt id are not review
        # identities themselves.
        assert record.review_id != "session-S"
        assert record.interactions[0].interaction_id != "session-S"


# ---------------------------------------------------------------------------
# REQ-013: paired baseline (audit instrumentation disabled) vs instrumented
# ---------------------------------------------------------------------------


def _disable_audit_instrumentation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove only this family's audit instrumentation; production stays real."""

    @contextmanager
    def _inert_begin(*args, **kwargs):
        yield None

    monkeypatch.setattr("auto_coder.pr_processor.begin_executed_review", _inert_begin)
    monkeypatch.setattr("auto_coder.pr_processor.finish_executed_review", lambda *a, **k: None)
    monkeypatch.setattr("auto_coder.pr_processor.record_bypassed", lambda *a, **k: None)
    monkeypatch.setattr("auto_coder.pr_processor.record_reused", lambda *a, **k: None)
    monkeypatch.setattr("auto_coder.pr_processor.record_effect", lambda *a, **k: None)
    monkeypatch.setattr("auto_coder.pr_processor.find_reusable_source_review_id", lambda *a, **k: None)


class TestReq013PairedComparison:
    def _run(self, tmp_path, monkeypatch, audit_store, *, instrumented: bool):
        # Each call gets its own scoped MonkeyPatch so the baseline's removed
        # instrumentation never leaks into the paired instrumented call.
        with pytest.MonkeyPatch.context() as mp:
            repo, head_sha = _build_pr_repo(tmp_path / ("instrumented" if instrumented else "baseline"))
            client = _build_github_client(head_sha)
            pr_data = _build_pr_data(head_sha)
            config = _build_config()
            reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD])
            manager = _build_backend_manager(mp, {"reviewer": reviewer}, "reviewer")

            _apply_standard_merge_gates(mp, mergeable=True, merge_result=True)
            _wire_backend(mp, manager)
            mp.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
            mp.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

            if not instrumented:
                _disable_audit_instrumentation(mp)

            actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})
            return actions, reviewer

    def test_business_outcome_matches_with_and_without_instrumentation(self, tmp_path, monkeypatch, audit_store):
        baseline_actions, baseline_reviewer = self._run(tmp_path, monkeypatch, audit_store, instrumented=False)
        assert audit_store.get_recent_history(REPO_NAME, limit=10).records == []

        instrumented_actions, instrumented_reviewer = self._run(tmp_path, monkeypatch, audit_store, instrumented=True)

        assert baseline_reviewer.calls == instrumented_reviewer.calls == ["fresh"]

        assert any("Successfully merged" in a for a in baseline_actions) == any("Successfully merged" in a for a in instrumented_actions)
        assert any("Published APPROVE adversarial review" in a for a in baseline_actions) == any("Published APPROVE adversarial review" in a for a in instrumented_actions)

        records = audit_store.get_recent_history(REPO_NAME, limit=10).records
        assert len(records) == 1
        assert records[0].native_verdict == "PASS"

    def test_audit_write_failure_does_not_change_business_outcome(self, tmp_path, monkeypatch, audit_store):
        def _raise(*args, **kwargs):
            raise RuntimeError("simulated disk failure")

        monkeypatch.setattr(audit_store, "record_evaluation", _raise)
        monkeypatch.setattr(audit_store, "update_evaluation", _raise)

        actions, reviewer = self._run(tmp_path, monkeypatch, audit_store, instrumented=True)

        assert reviewer.calls == ["fresh"]
        assert any("Successfully merged" in a for a in actions)
        assert any("Published APPROVE adversarial review" in a for a in actions)


def _two_tier_reprocessing_setup(monkeypatch, tmp_path, gate, inputs, head_sha):
    client = _build_github_client(head_sha)
    pr_data = _build_pr_data(head_sha)
    config = _build_config()
    config.MAX_ADVERSARIAL_VALIDATIONS = 0
    _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
    monkeypatch.setattr("auto_coder.pr_processor._get_adversarial_validation_eligibility", lambda *a, **k: MagicMock(is_applicable=True, lookup_error=""))
    monkeypatch.setattr("auto_coder.pr_processor._get_published_adversarial_validation_status", lambda *a, **k: ("PASS", None))
    monkeypatch.setattr("auto_coder.pr_processor._adversarial_validation_exhaustion_retry_due", lambda *a, **k: (False, None))
    monkeypatch.setattr("auto_coder.pr_processor._two_tier_gate_inputs", lambda *a, **k: inputs)
    monkeypatch.setattr("auto_coder.pr_processor._consume_pending_two_tier_publication", lambda *a, **k: (False, "no pending effect"))
    return client, pr_data, config


def _closure_completion(gate, contract, policy, *, pr_number, base, audited, repaired):
    gate.ordinary_pass(pr_number, audited, base, contract)
    claim = gate.state.claim_strong_audit(pr_number, RoundProvenance(audited, base), contract, policy)
    finding = Finding(
        finding_id="finding-a",
        origin_round_id=claim.claim_id,
        requirement_ids=("#0/REQ-002",),
        requirement_texts=("Reuse bounded closure completion.",),
        counterexample="A duplicate strong audit runs at the repair head.",
        expected_behavior="The accepted closure is reused.",
        actual_behavior="A new claim replaces it.",
        evidence="src/auto_coder/pr_processor.py",
        affected_boundary="two-tier completion reuse",
        focused_regression_scenario="Reprocess the certified repair head.",
    )
    strong_round = gate.state.record_strong_result(pr_number, claim.claim_id, VERDICT_FINDINGS, "strong/model", [finding])
    gate.state.acknowledge_publication(pr_number, strong_round.round_id)
    gate.ordinary_pass(pr_number, repaired, base, contract)
    closure = gate.state.certify_closure(
        pr_number,
        RoundProvenance(repaired, base),
        contract,
        policy,
        strong_round.round_id,
        strong_round.finding_set_revision,
        [FindingDisposition("finding-a", "FIXED", "Regression proves the repair.", repaired)],
        True,
        "The cumulative diff contains only the tracked repair and regression.",
    )
    return strong_round, gate.state.acknowledge_closure_publication(pr_number, closure.accepted_closure.closure_id)


def test_production_reentry_reuses_bounded_closure_after_reconstruction(tmp_path, monkeypatch, audit_store):
    monkeypatch.setenv("HOME", str(tmp_path))
    contract = ContractSnapshot(("#0",), "Issue #0 REQ-002: Reuse bounded closure completion.")
    policy = StrongPolicyIdentity("strong", "model=strong", "v1")
    base, audited, repaired = "b" * 40, "a" * 40, "c" * 40
    gate = TwoTierPrGate(REPO_NAME)
    strong_round, completed = _closure_completion(gate, contract, policy, pr_number=PR_NUMBER, base=base, audited=audited, repaired=repaired)
    reconstructed = TwoTierPrGate(REPO_NAME)
    inputs = TwoTierGateInputs(reconstructed, contract, policy, repaired, base)
    client, pr_data, config = _two_tier_reprocessing_setup(monkeypatch, tmp_path, reconstructed, inputs, repaired)
    strong_execution = MagicMock(side_effect=AssertionError("reused closure must not execute a strong audit"))
    monkeypatch.setattr("auto_coder.pr_processor._execute_pending_strong_audit", strong_execution)

    actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

    strong_execution.assert_not_called()
    after = reconstructed.state.snapshot(PR_NUMBER)
    assert after.completion == completed.completion
    assert after.completion.basis == "ORDINARY_CLOSURE"
    assert after.accepted_strong_round.round_id == strong_round.round_id
    assert after.accepted_strong_round.head_sha == audited
    assert after.accepted_closure.head_sha == repaired
    assert [(item.finding_id, item.status, item.disposition_head_sha) for item in after.findings] == [("finding-a", "FIXED", repaired)]
    assert any("reused existing ORDINARY_CLOSURE completion" in action for action in actions)


def test_production_recheck_reuses_completion_won_after_preliminary_read(tmp_path, monkeypatch, audit_store):
    monkeypatch.setenv("HOME", str(tmp_path))
    contract = ContractSnapshot(("#0",), "Issue #0 REQ-004: Fence delayed claims.")
    policy = StrongPolicyIdentity("strong", "model=strong", "v1")
    base, head = "b" * 40, "a" * 40
    gate_b = TwoTierPrGate(REPO_NAME)
    gate_b.ordinary_pass(PR_NUMBER, head, base, contract)
    inputs = TwoTierGateInputs(gate_b, contract, policy, head, base)
    client, pr_data, config = _two_tier_reprocessing_setup(monkeypatch, tmp_path, gate_b, inputs, head)
    preliminary_read = threading.Event()
    allow_b = threading.Event()
    original_reusable = gate_b.reusable_completion
    calls = {"count": 0}

    def paused_reusable(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            assert original_reusable(*args, **kwargs) is None
            preliminary_read.set()
            assert allow_b.wait(timeout=5)
            return None
        return original_reusable(*args, **kwargs)

    monkeypatch.setattr(gate_b, "reusable_completion", paused_reusable)
    model = MagicMock(side_effect=AssertionError("the delayed claimant must not invoke the model"))
    monkeypatch.setattr("auto_coder.pr_processor.execute_review", model)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(_handle_pr_merge(client, REPO_NAME, pr_data, config, {})))
    worker.start()
    assert preliminary_read.wait(timeout=5)

    gate_a = TwoTierPrGate(REPO_NAME)
    claim = gate_a.state.claim_strong_audit(PR_NUMBER, RoundProvenance(head, base), contract, policy)
    accepted = gate_a.state.record_strong_result(PR_NUMBER, claim.claim_id, "PASS", "strong/model")
    gate_a.state.acknowledge_publication(PR_NUMBER, accepted.round_id)
    completed = gate_a.state.accept_strong_pass_completion(PR_NUMBER, accepted.round_id)
    allow_b.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    model.assert_not_called()
    assert len(outcomes) == 1
    assert any("reused existing STRONG_PASS completion" in action for action in outcomes[0])
    after = gate_b.state.snapshot(PR_NUMBER)
    assert after.completion == completed.completion
    assert after.active_claim is None
    with pytest.raises(NotApplicableError, match="completion already exists"):
        gate_b.state.claim_strong_audit(PR_NUMBER, RoundProvenance(head, base), contract, policy)


# ---------------------------------------------------------------------------
# AS-005 / AS-006: stale-head ordering, ambiguous publication, restart
# ---------------------------------------------------------------------------


class TestStaleHeadPublicationAndRestart:
    def test_restart_and_closure_preserve_readable_history(self, tmp_path, monkeypatch, audit_store):
        """AS-006: a completed review remains readable after PR closure/session
        cleanup and after the audit root is reopened as a fresh store (restart)."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(True, "APPROVE", ""))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})
        records_before = _get_only_evaluation(audit_store, REPO_NAME)
        review_id = records_before[0].review_id

        # Real PR closure/session cleanup: unrelated reviewer-session state is
        # removed, but the durable audit record is a separate store.
        from auto_coder.pr_processor import _remove_reviewer_sessions_for_closed_pr

        _remove_reviewer_sessions_for_closed_pr(REPO_NAME, PR_NUMBER)

        # "Restart": open the same audit root as a brand-new store instance.
        restarted_store = ReviewAuditStore(audit_root=tmp_path / "review_audit")
        reread = restarted_store.get_evaluation(REPO_NAME, review_id)
        assert reread.record is not None
        assert reread.record.native_verdict == "PASS"
        assert reread.record.execution_mode == ExecutionMode.EXECUTED
        assert len(reread.record.interactions) == 1

        history = restarted_store.get_recent_history(REPO_NAME, limit=10)
        assert [r.review_id for r in history.records] == [review_id]

    def test_ambiguous_publication_is_pending_then_confirmed_by_reconciliation(self, tmp_path, monkeypatch, audit_store):
        """AS-005: an accepted write whose response was lost is not immediately
        confirmed; the existing reconciliation path's later confirmation is a
        separate appended effect, and the semantic review report is untouched
        throughout."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=False)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        # The write was actually accepted, but this process never saw the
        # confirmation; the existing owner reconciliation path later confirms it.
        monkeypatch.setattr("auto_coder.pr_processor.publish_adversarial_review", lambda *a, **k: ReviewPublicationResult(False, "", "response lost"))
        monkeypatch.setattr("auto_coder.pr_processor._reconcile_failed_adversarial_publication", lambda *a, **k: (True, None))

        _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        # The semantic report is retained regardless of the publication drama.
        assert record.native_verdict == "PASS"
        assert record.execution_mode == ExecutionMode.EXECUTED

        dispositions = [effect.disposition for effect in record.effects]
        assert dispositions == ["pending", "confirmed"]
        assert record.effects[0].details is not None and record.effects[0].details.get("phase") == "publication"
        assert record.effects[1].details is not None and record.effects[1].details.get("phase") == "reconciliation"

    def test_stale_head_late_completion_is_superseded_not_overwriting(self, tmp_path, monkeypatch, audit_store):
        """AS-005: H1's review is paused (a slower validation), H2 is validated
        and completes first, then H1's late completion is observed as
        superseded rather than approving/overwriting the newer head."""
        repo, h1_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(h1_sha)
        pr_data = _build_pr_data(h1_sha)
        config = _build_config()

        # A second, independent attempt for the SAME head races ahead and
        # completes while H1's own reviewer call is still "in flight" (the
        # LLM-boundary side effect below simulates that race deterministically,
        # without disturbing the real attempt-repository/publication code).
        from auto_coder.adversarial_validation_attempts import AdversarialValidationAttemptRepository

        attempt_repo_for_race = AdversarialValidationAttemptRepository(REPO_NAME)

        class RacingReviewerClient(MockReviewerClient):
            def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
                # A newer attempt for this exact head is registered by another
                # (independent) run before this one's own result is accepted.
                attempt_repo_for_race.start(PR_NUMBER, h1_sha)
                return super()._run_llm_cli(prompt, is_noedit=is_noedit)

        reviewer = RacingReviewerClient("reviewer", responses=[PASS_PAYLOAD])
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        publish_calls: List[str] = []
        monkeypatch.setattr(
            "auto_coder.pr_processor.publish_adversarial_review",
            lambda *a, **k: (publish_calls.append("called"), ReviewPublicationResult(True, "APPROVE", ""))[1],
        )

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]
        assert not publish_calls, "a superseded attempt must not publish"
        assert any("newer attempt is already applicable" in action for action in actions)

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        # H1's own retained result is the real, non-overwritten native verdict.
        assert record.native_verdict == "PASS"
        assert record.reviewed_generation == h1_sha
        effect_dispositions = [effect.disposition for effect in record.effects]
        assert effect_dispositions == ["superseded"]
        assert record.effects[0].details is not None and record.effects[0].details.get("phase") == "pre-publication"
        assert list_row(record).generation == h1_sha

    def test_head_changed_before_durable_acceptance_is_superseded(self, tmp_path, monkeypatch, audit_store):
        """REQ-006: the current PR head moved before the gap-state checkpoint
        could be durably accepted; the review's own retained result stays
        intact, publication is never attempted, and the observation is a
        separate 'superseded' effect rather than an overwrite."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        # An explicit provider session makes run_adversarial_validation build
        # a reviewer_session_checkpoint, which is what reaches the
        # gap-state-acceptance boundary below.
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD], session_id="fresh-session")
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        # The authoritative current head has moved on by the time the result
        # is checked, even though this validation examined head_sha.
        client.get_pull_request_head_sha_strict.return_value = "0" * 40

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        publish_calls: List[str] = []
        monkeypatch.setattr(
            "auto_coder.pr_processor.publish_adversarial_review",
            lambda *a, **k: (publish_calls.append("called"), ReviewPublicationResult(True, "APPROVE", ""))[1],
        )

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]
        assert not publish_calls, "a result rejected for a moved head must not publish"
        assert any("current head changed before durable acceptance" in action for action in actions)

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        # The review's own retained result is untouched by the rejection.
        assert record.native_verdict == "PASS"
        assert record.reviewed_generation == head_sha
        effect_dispositions = [effect.disposition for effect in record.effects]
        assert effect_dispositions == ["superseded"]
        assert record.effects[0].details is not None
        assert record.effects[0].details.get("phase") == "gap-state-acceptance"
        assert record.effects[0].details.get("observed_head") == "0" * 40

    def test_head_observation_failure_before_durable_acceptance_appends_failed_effect(self, tmp_path, monkeypatch, audit_store):
        """REQ-006: the authoritative head check itself raises at the
        gap-state-acceptance boundary (a transient GitHub failure). The
        review's own retained PASS result for the original head stays
        intact, publication is never attempted, and the raised check is its
        own appended 'failed' effect rather than leaving zero observations."""
        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        # An explicit provider session makes run_adversarial_validation build
        # a reviewer_session_checkpoint, which is what reaches the
        # gap-state-acceptance boundary below.
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD], session_id="fresh-session")
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        # The authoritative head check itself fails transiently.
        client.get_pull_request_head_sha_strict.side_effect = RuntimeError("GitHub API unavailable")

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        publish_calls: List[str] = []
        monkeypatch.setattr(
            "auto_coder.pr_processor.publish_adversarial_review",
            lambda *a, **k: (publish_calls.append("called"), ReviewPublicationResult(True, "APPROVE", ""))[1],
        )

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        assert reviewer.calls == ["fresh"]
        assert not publish_calls, "a result rejected for an unconfirmable head must not publish"
        assert any("authoritative head could not be confirmed" in action for action in actions)

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        # The review's own retained result for the original head is untouched.
        assert record.native_verdict == "PASS"
        assert record.reviewed_generation == head_sha
        effect_dispositions = [effect.disposition for effect in record.effects]
        assert effect_dispositions == ["failed"]
        assert record.effects[0].details is not None
        assert record.effects[0].details.get("phase") == "gap-state-acceptance"
        assert record.effects[0].details.get("reason") == "head observation unavailable"

    def test_changed_validation_snapshot_before_acceptance_is_superseded(self, tmp_path, monkeypatch, audit_store):
        """REQ-006: recovered file evidence exists from a prior session, but
        the validation snapshot can no longer be confirmed current at the
        gap-state-acceptance boundary; the retained review result stays
        intact and this is recorded as a separate 'superseded' effect."""
        from auto_coder.reviewer_session_registry import RecoveredFileEvidence, ReviewerSession, ReviewerSessionRegistry

        repo, head_sha = _build_pr_repo(tmp_path)
        client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        config = _build_config()
        reviewer = MockReviewerClient("reviewer", responses=[PASS_PAYLOAD], session_id="fresh-session")
        manager = _build_backend_manager(monkeypatch, {"reviewer": reviewer}, "reviewer")

        # A prior session recorded recovered evidence for a file this run's
        # response does not re-confirm; real recovery-ledger construction
        # invalidates it, giving the checkpoint non-empty recovered_file_evidence.
        registry = ReviewerSessionRegistry()
        registry.save(
            ReviewerSession(
                repository=REPO_NAME,
                pr_number=PR_NUMBER,
                backend_name="reviewer",
                backend_type="codex-cloud",
                model_name="reviewer-model",
                session_id="stale-session",
                last_head_sha=head_sha,
                recovered_file_evidence=[
                    RecoveredFileEvidence(path="docs/prior_evidence.md", source="past run", status="RECOVERED", evidence="Previously inspected."),
                ],
                evidence_validation_snapshot="stale-snapshot",
            )
        )

        _apply_standard_merge_gates(monkeypatch, mergeable=True, merge_result=True)
        _wire_backend(monkeypatch, manager)
        monkeypatch.setattr("auto_coder.pr_processor.isolated_pr_head_worktree", lambda *a, **k: _static_worktree(repo))
        # validation_snapshot_is_current is the real GitHub-freshness boundary
        # check itself (not the validator/parser), called at two real
        # boundaries: once inside run_adversarial_validation's own recovery-
        # ledger check (which must see the snapshot as still current so the
        # review actually executes and a checkpoint is built), and again at
        # pr_processor's later gap-state-acceptance boundary (simulating a
        # real TOCTOU: the PR state changed in between the two checks).
        snapshot_check_calls = {"count": 0}

        def _snapshot_current_then_stale(*args, **kwargs):
            snapshot_check_calls["count"] += 1
            return snapshot_check_calls["count"] == 1

        monkeypatch.setattr("auto_coder.adversarial_validator.validation_snapshot_is_current", _snapshot_current_then_stale)
        monkeypatch.setattr("auto_coder.pr_processor.validation_snapshot_is_current", _snapshot_current_then_stale)
        publish_calls: List[str] = []
        monkeypatch.setattr(
            "auto_coder.pr_processor.publish_adversarial_review",
            lambda *a, **k: (publish_calls.append("called"), ReviewPublicationResult(True, "APPROVE", ""))[1],
        )

        actions = _handle_pr_merge(client, REPO_NAME, pr_data, config, {})

        # A non-empty stored session_id makes this a continuation, not a
        # fresh call; the checkpoint's invalidated recovered evidence is what
        # this fixture is really about.
        assert reviewer.calls == ["continue"]
        assert not publish_calls, "a result rejected for a stale snapshot must not publish"
        assert any("validation snapshot changed before durable acceptance" in action for action in actions)

        records = _get_only_evaluation(audit_store, REPO_NAME)
        assert len(records) == 1
        record = records[0]
        assert record.native_verdict == "PASS"
        assert record.reviewed_generation == head_sha
        effect_dispositions = [effect.disposition for effect in record.effects]
        assert effect_dispositions == ["superseded"]
        assert record.effects[0].details is not None
        assert record.effects[0].details.get("phase") == "gap-state-acceptance"
        assert record.effects[0].details.get("reason") == "validation snapshot changed or could not be confirmed"
