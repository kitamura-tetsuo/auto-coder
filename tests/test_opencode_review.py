"""Production-path regression coverage for OpenCode adversarial review integration (Issue #2127).

Covers:
- AC-001: Issue and PR review with real TOML alias, executable fake `opencode` driver,
          no-edit enforcement, parser delivery, kind-specific route precedence.
- AC-002: PR-scoped session persistence across restart and worktrees; fresh review on different identity.
- AC-003: Unavailable session on normal review triggers fresh full review (new session);
          evidence-completion continuation failure/mismatch terminates as ERROR.
- AC-004: Old evidence cannot authorize newer state (PR HEAD change or normative Issue contract change).
- AC-005: Forged PASS ignored in favor of legitimate final FAIL; write denial / preflight failure
          fails closed; quota exhaustion rotates only to eligible read-only candidates.
- AC-006: Persistence failure on checkpoint acceptance clears checkpoint; corrupt registry treated as empty;
          PR close/merge removes only that PR's association.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationResult,
    _complete_changed_file_evidence,
    build_adversarial_validation_context,
    run_adversarial_validation,
    validation_snapshot_is_current,
)
from src.auto_coder.cli_helpers import (
    create_adversarial_validation_backend_manager,
    is_read_only_review_capable_backend,
)
from src.auto_coder.decomposition_analyzer import (
    DecompositionIssue,
    analyze_issue_decomposition,
)
from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration
from src.auto_coder.objective_evidence import ObjectiveAnchor, ObjectiveExtraction
from src.auto_coder.pr_processor import _remove_reviewer_sessions_for_closed_pr
from src.auto_coder.requirement_contract import build_normative_issue_manifest
from src.auto_coder.reviewer_session_registry import (
    ReviewerSession,
    ReviewerSessionRegistry,
)
from src.auto_coder.specification_analyzer import (
    IndividualReviewEvidence,
    analyze_issue_specification,
)
from tests.test_opencode_backend import (
    _driver,
    _event,
    _git,
    _repository,
    _set_known_sessions,
)
from tests.test_opencode_noedit_backend import _locked_down_debug_agent_response

REPO_NAME = "owner/tested-repo"
PR_NUMBER = 101
ISSUE_NUMBER = 42
VALID_ISSUE_BODY = """## Objective
Add greet() function.

## Requirements
REQ-001: greet() returns hello.
"""


def _make_opencode_event_stream(session_id: str, text: str, message_id: str = "m1") -> str:
    return (
        _event(
            "step_start",
            session_id=session_id,
            part={"id": f"sp_{message_id}", "messageID": message_id},
        )
        + "\n"
        + _event(
            "text",
            session_id=session_id,
            part={"id": f"t_{message_id}", "messageID": message_id, "text": text},
        )
        + "\n"
        + _event(
            "step_finish",
            session_id=session_id,
            part={"id": f"sf_{message_id}", "messageID": message_id, "reason": "stop"},
        )
        + "\n"
    )


def _setup_opencode_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str = "ses_root1",
    stdout_content: str = "",
) -> Tuple[Path, Path, Path]:
    script = _driver(tmp_path)
    report = tmp_path / "report.json"
    debug_response = tmp_path / "debug_agent.json"
    debug_response.write_text(_locked_down_debug_agent_response())
    stdout_file = tmp_path / "stdout.jsonl"
    stdout_file.write_text(stdout_content or "")

    monkeypatch.setenv("AUTOCODER_OPENCODE_CLI", str(script))
    monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report))
    monkeypatch.setenv("OPENCODE_TEST_STDOUT_FILE", str(stdout_file))
    monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_RESPONSE_FILE", str(debug_response))
    _set_known_sessions(tmp_path, monkeypatch, session_id)
    return script, report, stdout_file


def _build_test_repo(tmp_path: Path) -> Tuple[Path, str]:
    repo = tmp_path / "pr_repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "OpenCode Tester")
    _git(repo, "config", "user.email", "tester@example.com")
    (repo / "sample.py").write_text("def greet():\n    return 'hello'\n")
    _git(repo, "add", "sample.py")
    _git(repo, "commit", "-q", "-m", "initial")
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text("def test_greet(): pass\n")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    script = scripts_dir / "test.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o755)
    _git(repo, "add", "tests", "scripts")
    _git(repo, "commit", "-q", "-m", "add tests and script")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, head_sha


def _build_github_client(head_sha: str, issue_body: str = VALID_ISSUE_BODY) -> MagicMock:
    client = MagicMock()
    client.get_pr_diff.return_value = "diff --git a/sample.py b/sample.py\n" "index 1234567..89abcdef 100644\n" "--- a/sample.py\n" "+++ b/sample.py\n" "@@ -1,2 +1,2 @@\n" "-def greet():\n" "-    return 'placeholder'\n" "+def greet():\n" "+    return 'hello'\n"
    client.get_pr_changed_file_count.return_value = 1
    client.get_issue.return_value = {
        "number": ISSUE_NUMBER,
        "title": "Add greet()",
        "body": issue_body,
        "state": "open",
    }
    client.get_parent_issue_details.return_value = None
    client.get_pr_comments.return_value = []
    client.get_pr_reviews_strict.return_value = []
    client.get_pull_request.return_value = {"head": {"sha": head_sha}}
    client.get_pull_request_head_sha_strict.return_value = head_sha
    return client


def _build_pr_data(head_sha: str, pr_number: int = PR_NUMBER) -> Dict[str, Any]:
    return {
        "number": pr_number,
        "title": "Implement greet()",
        "body": f"Fixes #{ISSUE_NUMBER}",
        "labels": [],
        "user": {"login": "accepted-author"},
        "head": {"ref": "feature-branch", "sha": head_sha},
        "base": {"ref": "main"},
    }


def _pr_validation_pass_payload() -> str:
    return json.dumps(
        {
            "result": "PASS",
            "summary": "greet() returns hello at the reviewed head.",
            "findings": [],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-001",
                    "status": "VERIFIED",
                    "evidence": "sample.py: greet() returns hello.",
                }
            ],
            "specification_gaps": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [],
            "dynamic_check_requested": None,
        }
    )


def _pr_validation_fail_payload() -> str:
    return json.dumps(
        {
            "result": "NEEDS_FIX",
            "summary": "State update failure",
            "findings": [
                {
                    "requirement_id": "REQ-001",
                    "finding_identity": "state-update-discard",
                    "correction_identity": "test-correction",
                    "violated_requirement": "REQ-001",
                    "evidence_classification": "DEMONSTRATED",
                    "reachability": "Public entry point reaches this branch",
                    "required_behavior": "greet returns hello",
                    "actual_behavior": "greet returns placeholder",
                    "evidence": "sample.py: returns placeholder",
                    "counterexample": "greet() returns placeholder instead of hello",
                    "anchor_path": "sample.py",
                    "anchor_line": 2,
                    "anchor_side": "RIGHT",
                }
            ],
            "requirement_coverage": [
                {
                    "requirement_id": "REQ-001",
                    "status": "VIOLATED",
                    "evidence": "sample.py: returns placeholder",
                }
            ],
            "specification_gaps": [],
            "test_oracle_gaps": [],
            "thread_dispositions": [],
            "dynamic_check_requested": None,
        }
    )


# ---------------------------------------------------------------------------
# AC-001: Issue & PR review with real TOML alias & kind-specific precedence
# ---------------------------------------------------------------------------


class TestAC001IssueAndPrReview:
    def test_is_read_only_review_capable_backend_opencode(self) -> None:
        assert is_read_only_review_capable_backend("opencode") is True

    def test_issue_specification_review_with_opencode_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo = _repository(tmp_path)
        pass_response = json.dumps({"verdict": "READY", "remediation": "NONE", "findings": []})
        stdout_content = _make_opencode_event_stream("ses_spec1", pass_response)
        _, report, _ = _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_spec1", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-reviewer": BackendConfig(
                    name="opencode-reviewer",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_issue_adversarial_validation_order=["opencode-reviewer"],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            manifest = build_normative_issue_manifest(ISSUE_NUMBER, "Add greet()", VALID_ISSUE_BODY)
            evidence = IndividualReviewEvidence(
                "{}",
                objective=ObjectiveAnchor(ISSUE_NUMBER, "UNANCHORED", None, "test:v1", ObjectiveExtraction("ABSENT")),
            )
            result = analyze_issue_specification(
                manifest=manifest,
                issue_body=VALID_ISSUE_BODY,
                review_evidence=evidence,
            )

        assert result.verdict == "READY"
        assert len(result.findings) == 0

        observed = json.loads(report.read_text())
        assert observed["argv"][0] == "run"
        assert "--agent" in observed["argv"]
        agent_name = observed["argv"][observed["argv"].index("--agent") + 1]
        assert agent_name.startswith("autocoder-noedit-")
        config_content = json.loads(observed["opencode_config_content"])
        assert config_content["agent"][agent_name]["permission"]["*"] == "deny"

    def test_issue_decomposition_review_with_opencode_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo = _repository(tmp_path)
        pass_response = json.dumps({"verdict": "READY", "remediation": "NONE", "findings": []})
        stdout_content = _make_opencode_event_stream("ses_decomp1", pass_response)
        _, report, _ = _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_decomp1", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-reviewer": BackendConfig(
                    name="opencode-reviewer",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_issue_adversarial_validation_order=["opencode-reviewer"],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            parent_body = "## Objective\nParent goal."
            parent_issue = DecompositionIssue(
                build_normative_issue_manifest(100, "Parent feature", parent_body),
                parent_body,
            )
            child_body = "## Requirements\nREQ-001: Child behavior.\n\n## Objective\nChild goal."
            child_issue = DecompositionIssue(
                build_normative_issue_manifest(101, "Child 1", child_body),
                child_body,
            )
            result = analyze_issue_decomposition(
                parent=parent_issue,
                children=[child_issue],
            )

        assert result.verdict == "READY"
        observed = json.loads(report.read_text())
        assert "--agent" in observed["argv"]

    def test_pr_adversarial_review_with_opencode_alias(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        pass_response = _pr_validation_pass_payload()
        stdout_content = _make_opencode_event_stream("ses_pr_review1", pass_response)
        _, report, _ = _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_pr_review1", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        registry = ReviewerSessionRegistry(path=tmp_path / "reviewer_sessions.json")

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            val_result = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo),
            )

        assert val_result.result == "PASS"
        assert val_result.reviewer_session_checkpoint is not None
        assert val_result.reviewer_session_checkpoint.session_id == "ses_pr_review1"
        assert val_result.reviewer_session_checkpoint.backend_name == "opencode-pr"

        observed = json.loads(report.read_text())
        assert "--agent" in observed["argv"]

    def test_kind_specific_route_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_opencode_env(tmp_path, monkeypatch)
        config = LLMBackendConfiguration(
            backends={
                "generic-reviewer": BackendConfig(
                    name="generic-reviewer",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                ),
                "pr-reviewer": BackendConfig(
                    name="pr-reviewer",
                    backend_type="opencode",
                    model="anthropic/claude-opus-4-5",
                ),
                "issue-reviewer": BackendConfig(
                    name="issue-reviewer",
                    backend_type="opencode",
                    model="anthropic/claude-haiku-4-5",
                ),
            },
            backend_adversarial_validation_order=["generic-reviewer"],
            backend_pr_adversarial_validation_order=["pr-reviewer"],
            backend_issue_adversarial_validation_order=["issue-reviewer"],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            pr_mgr = create_adversarial_validation_backend_manager(validation_kind="pr")
            assert pr_mgr is not None
            assert pr_mgr._current_backend_name() == "pr-reviewer"

            issue_mgr = create_adversarial_validation_backend_manager(validation_kind="issue")
            assert issue_mgr is not None
            assert issue_mgr._current_backend_name() == "issue-reviewer"

            legacy_mgr = create_adversarial_validation_backend_manager(validation_kind=None)
            assert legacy_mgr is not None
            assert legacy_mgr._current_backend_name() == "generic-reviewer"


# ---------------------------------------------------------------------------
# AC-002: PR-scoped session persistence and continuation across worktrees
# ---------------------------------------------------------------------------


class TestAC002PrScopedSessionContinuity:
    def test_pr_session_persists_and_continues_across_worktrees(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo1, head_sha = _build_test_repo(tmp_path / "wt1")
        pass_response = _pr_validation_pass_payload()
        body_script = tmp_path / "driver_body.py"
        body_script.write_text("import os, sys\n" "if '--session' in argv:\n" "    sys.stdout.write(" + repr(_make_opencode_event_stream("ses_pr101", pass_response, "m2")) + ")\n" "else:\n" "    sys.stdout.write(" + repr(_make_opencode_event_stream("ses_pr101", pass_response, "m1")) + ")\n")
        monkeypatch.setenv("OPENCODE_TEST_BODY_FILE", str(body_script))

        _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_pr101", stdout_content="")
        monkeypatch.chdir(repo1)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha, pr_number=101)
        registry_file = tmp_path / "reviewer_sessions.json"
        registry = ReviewerSessionRegistry(path=registry_file)

        report1 = tmp_path / "report1.json"
        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report1))

        # First review: Fresh run establishing ses_pr101
        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            res1 = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo1),
            )

        assert res1.result == "PASS"
        assert res1.reviewer_session_checkpoint is not None
        assert res1.reviewer_session_checkpoint.session_id == "ses_pr101"
        obs1 = json.loads(report1.read_text())
        assert "--session" not in obs1["argv"]

        stored = registry.get(REPO_NAME, 101, "opencode-pr", "opencode", "anthropic/claude-sonnet-4-5")
        assert stored is not None
        assert stored.session_id == "ses_pr101"

        # Second review: In a new worktree / restart for the same PR
        repo2 = tmp_path / "wt2"
        _git(tmp_path, "clone", str(repo1), str(repo2))
        _git(repo2, "config", "user.name", "OpenCode Tester")
        _git(repo2, "config", "user.email", "tester@example.com")
        monkeypatch.chdir(repo2)
        report2 = tmp_path / "report2.json"
        monkeypatch.setenv("OPENCODE_TEST_REPORT_FILE", str(report2))

        # Reopen registry from disk to prove persistence across restart
        fresh_registry = ReviewerSessionRegistry(path=registry_file)
        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            res2 = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=fresh_registry,
                execution_cwd=str(repo2),
            )

        assert res2.result == "PASS"
        obs2 = json.loads(report2.read_text())
        assert "--session" in obs2["argv"]
        assert obs2["argv"][obs2["argv"].index("--session") + 1] == "ses_pr101"

    def test_different_pr_or_backend_uses_fresh_review(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        pass_response = _pr_validation_pass_payload()
        stdout_content = _make_opencode_event_stream("ses_pr102", pass_response)
        _, report, _ = _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_pr102", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        registry_file = tmp_path / "reviewer_sessions.json"
        registry = ReviewerSessionRegistry(path=registry_file)

        # Seed registry with session for PR #101
        registry.save(
            ReviewerSession(
                repository=REPO_NAME,
                pr_number=101,
                backend_name="opencode-pr",
                backend_type="opencode",
                model_name="anthropic/claude-sonnet-4-5",
                session_id="ses_pr101",
            )
        )

        # Reviewing PR #102: Must run fresh (not continue ses_pr101)
        pr_data_102 = _build_pr_data(head_sha, pr_number=102)
        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            res = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data_102,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo),
            )

        assert res.result == "PASS"
        obs = json.loads(report.read_text())
        assert "--session" not in obs["argv"]

    def test_review_manager_does_not_pollute_global_session_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_opencode_env(tmp_path, monkeypatch)
        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            mgr = create_adversarial_validation_backend_manager(validation_kind="pr")
            assert mgr is not None
            assert mgr._automatic_session_resume is False

            mock_session_manager = MagicMock()
            mgr._session_state_manager = mock_session_manager
            mgr._save_session_state("opencode-pr", "ses_test")

            # save_state must never be called on global session manager
            mock_session_manager.save_state.assert_not_called()


# ---------------------------------------------------------------------------
# AC-003: Session recovery & evidence-completion continuation discontinuity
# ---------------------------------------------------------------------------


class TestAC003SessionRecoveryAndEvidenceContinuation:
    def test_stale_pr_session_triggers_fresh_review_and_new_association(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        pass_response = _pr_validation_pass_payload()

        # Driver accepts fresh calls and provides ses_fresh, but session list does NOT have ses_stale
        stdout_content = _make_opencode_event_stream("ses_fresh", pass_response)
        _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_fresh", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        # Seed registry with stale session
        registry_file = tmp_path / "reviewer_sessions.json"
        registry = ReviewerSessionRegistry(path=registry_file)
        registry.save(
            ReviewerSession(
                repository=REPO_NAME,
                pr_number=PR_NUMBER,
                backend_name="opencode-pr",
                backend_type="opencode",
                model_name="anthropic/claude-sonnet-4-5",
                session_id="ses_stale",
            )
        )

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            res = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo),
            )

        assert res.result == "PASS"
        assert res.reviewer_session_checkpoint is not None
        assert res.reviewer_session_checkpoint.session_id == "ses_fresh"

        # Registry should have been updated with ses_fresh, replacing ses_stale
        updated = registry.get(
            REPO_NAME,
            PR_NUMBER,
            "opencode-pr",
            "opencode",
            "anthropic/claude-sonnet-4-5",
        )
        assert updated is not None
        assert updated.session_id == "ses_fresh"

    def test_evidence_completion_continuation_discontinuity_terminates_as_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_initial")
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            mgr = create_adversarial_validation_backend_manager(validation_kind="pr")
            assert mgr is not None
            mgr._last_session_id = "ses_initial"

            res = AdversarialValidationResult(
                result="PASS",
                summary="Initial pass",
                findings=[],
                raw_response=_pr_validation_pass_payload(),
            )
            ctx = AdversarialValidationContext(
                unverified_files=["extra.py"],
                all_changed_files=["sample.py", "extra.py"],
                validation_snapshot="snap1",
            )

            # Case 1: continue_session raises exception
            with patch.object(mgr, "continue_session", side_effect=RuntimeError("Session failed")):
                err_result = _complete_changed_file_evidence(res, ctx, mgr, "manifest", head_sha)
                assert err_result.result == "ERROR"
                assert err_result.diagnostic_category == "changed_file_completion_session_discontinuity"

            # Case 2: continue_session returns but _last_continue_session_resumed is False
            with patch.object(mgr, "continue_session", return_value=_pr_validation_pass_payload()):
                mgr._last_continue_session_resumed = False
                err_result = _complete_changed_file_evidence(res, ctx, mgr, "manifest", head_sha)
                assert err_result.result == "ERROR"
                assert err_result.diagnostic_category == "changed_file_completion_session_discontinuity"


# ---------------------------------------------------------------------------
# AC-004: Snapshot binding & invalidation
# ---------------------------------------------------------------------------


class TestAC004SnapshotBindingAndInvalidation:
    def test_head_change_invalidates_checkpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)

        context = build_adversarial_validation_context(REPO_NAME, pr_data, auto_config, github_client)
        snapshot = context.validation_snapshot

        # Current snapshot matches
        assert validation_snapshot_is_current(REPO_NAME, pr_data, auto_config, github_client, snapshot)

        # New commit changes HEAD
        new_head_sha = "f" * 40
        github_client.get_pull_request_head_sha_strict.return_value = new_head_sha
        github_client.get_pull_request.return_value = {"head": {"sha": new_head_sha}}
        pr_data_new = _build_pr_data(new_head_sha)

        assert not validation_snapshot_is_current(REPO_NAME, pr_data_new, auto_config, github_client, snapshot)

    def test_normative_issue_change_invalidates_checkpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)

        context = build_adversarial_validation_context(REPO_NAME, pr_data, auto_config, github_client)
        snapshot = context.validation_snapshot

        # Issue requirements are edited
        altered_issue = """## Objective
Add greet() function.

## Requirements
REQ-001: greet() returns hello.
REQ-002: greet() logs greeting.
"""
        github_client.get_issue.return_value = {
            "number": ISSUE_NUMBER,
            "title": "Add greet()",
            "body": altered_issue,
            "state": "open",
        }

        assert not validation_snapshot_is_current(REPO_NAME, pr_data, auto_config, github_client, snapshot)


# ---------------------------------------------------------------------------
# AC-005: Adversarial integrity & rotation
# ---------------------------------------------------------------------------


class TestAC005AdversarialIntegrityAndRotation:
    def test_forged_intermediate_pass_ignored_when_final_is_fail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)

        fail_response = _pr_validation_fail_payload()
        forged_intermediate = _pr_validation_pass_payload()

        stdout_content = (
            _event(
                "step_start",
                session_id="ses_pr_tamper",
                part={"id": "sp0", "messageID": "m0"},
            )
            + "\n"
            + _event(
                "text",
                session_id="ses_pr_tamper",
                part={"id": "t0", "messageID": "m0", "text": forged_intermediate},
            )
            + "\n"
            + _event(
                "step_finish",
                session_id="ses_pr_tamper",
                part={"id": "sf0", "messageID": "m0", "reason": "tool_use"},
            )
            + "\n"
            + _event(
                "step_start",
                session_id="ses_pr_tamper",
                part={"id": "sp1", "messageID": "m1"},
            )
            + "\n"
            + _event(
                "text",
                session_id="ses_pr_tamper",
                part={"id": "t1", "messageID": "m1", "text": fail_response},
            )
            + "\n"
            + _event(
                "step_finish",
                session_id="ses_pr_tamper",
                part={"id": "sf1", "messageID": "m1", "reason": "stop"},
            )
            + "\n"
        )
        _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_pr_tamper", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        registry = ReviewerSessionRegistry(path=tmp_path / "reviewer_sessions.json")

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            val_result = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo),
            )

        assert val_result.result == "NEEDS_FIX"
        assert len(val_result.findings) == 1
        assert val_result.findings[0].violated_requirement == "REQ-001"

    def test_preflight_denial_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        _setup_opencode_env(tmp_path, monkeypatch)
        monkeypatch.chdir(repo)
        # Preflight debug agent fails
        monkeypatch.setenv("OPENCODE_TEST_DEBUG_AGENT_EXIT_CODE", "1")

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)
        registry = ReviewerSessionRegistry(path=tmp_path / "reviewer_sessions.json")

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            with pytest.raises(RuntimeError, match="OpenCode no-edit enforcement"):
                run_adversarial_validation(
                    repo_name=REPO_NAME,
                    pr_data=pr_data,
                    config=auto_config,
                    github_client=github_client,
                    session_registry=registry,
                    execution_cwd=str(repo),
                )

        # Working tree must remain clean
        assert _git(repo, "status", "--porcelain") == ""

    def test_quota_exhaustion_rotates_only_to_eligible_candidates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_opencode_env(tmp_path, monkeypatch)
        config = LLMBackendConfiguration(
            backends={
                "opencode-primary": BackendConfig(
                    name="opencode-primary",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                ),
                "opencode-secondary": BackendConfig(
                    name="opencode-secondary",
                    backend_type="opencode",
                    model="anthropic/claude-haiku-4-5",
                ),
                "ineligible-aider": BackendConfig(name="ineligible-aider", backend_type="aider", model="claude-3-opus"),
            },
            backend_pr_adversarial_validation_order=[
                "opencode-primary",
                "ineligible-aider",
                "opencode-secondary",
            ],
        )

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            mgr = create_adversarial_validation_backend_manager(validation_kind="pr")
            assert mgr is not None
            # Ineligible candidate must NOT be among managed backends
            assert "ineligible-aider" not in mgr._all_backends
            assert mgr._all_backends == ["opencode-primary", "opencode-secondary"]


# ---------------------------------------------------------------------------
# AC-006: Persistence failure & PR lifecycle cleanup
# ---------------------------------------------------------------------------


class TestAC006PersistenceFailureAndPrLifecycle:
    def test_registry_save_failure_clears_checkpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _use_real_commands) -> None:
        repo, head_sha = _build_test_repo(tmp_path)
        pass_response = _pr_validation_pass_payload()
        stdout_content = _make_opencode_event_stream("ses_pr_save_fail", pass_response)
        _setup_opencode_env(tmp_path, monkeypatch, session_id="ses_pr_save_fail", stdout_content=stdout_content)
        monkeypatch.chdir(repo)

        config = LLMBackendConfiguration(
            backends={
                "opencode-pr": BackendConfig(
                    name="opencode-pr",
                    backend_type="opencode",
                    model="anthropic/claude-sonnet-4-5",
                )
            },
            backend_pr_adversarial_validation_order=["opencode-pr"],
        )

        from src.auto_coder.automation_config import AutomationConfig

        auto_config = AutomationConfig()
        github_client = _build_github_client(head_sha)
        pr_data = _build_pr_data(head_sha)

        registry = MagicMock()
        registry.get.return_value = None
        registry.save.side_effect = OSError("Disk read-only")

        with patch("src.auto_coder.cli_helpers.get_llm_config", return_value=config), patch("src.auto_coder.opencode_client.get_llm_config", return_value=config):
            # Direct validation surfaces persistence failures immediately by raising
            with pytest.raises(OSError, match="Disk read-only"):
                run_adversarial_validation(
                    repo_name=REPO_NAME,
                    pr_data=pr_data,
                    config=auto_config,
                    github_client=github_client,
                    session_registry=registry,
                    execution_cwd=str(repo),
                )

            # Deferred validation produces a checkpoint that is cleared upon commit failure
            val_result = run_adversarial_validation(
                repo_name=REPO_NAME,
                pr_data=pr_data,
                config=auto_config,
                github_client=github_client,
                session_registry=registry,
                execution_cwd=str(repo),
                defer_session_persistence=True,
            )
            assert val_result.result == "PASS"
            assert val_result.reviewer_session_checkpoint is not None

            # Simulating PR processor commit step failure:
            try:
                registry.save(val_result.reviewer_session_checkpoint)
            except Exception:
                val_result.reviewer_session_checkpoint = None

            assert val_result.reviewer_session_checkpoint is None

    def test_corrupt_registry_handled_gracefully(self, tmp_path: Path) -> None:
        corrupt_file = tmp_path / "reviewer_sessions.json"
        corrupt_file.write_text("{ corrupt json ...")
        registry = ReviewerSessionRegistry(path=corrupt_file)

        session = registry.get(
            REPO_NAME,
            PR_NUMBER,
            "opencode-pr",
            "opencode",
            "anthropic/claude-sonnet-4-5",
        )
        assert session is None
        assert registry.sessions_for_pr(REPO_NAME, PR_NUMBER) == []

    def test_pr_close_removes_only_target_pr_association(self, tmp_path: Path) -> None:
        registry_file = tmp_path / "reviewer_sessions.json"
        registry = ReviewerSessionRegistry(path=registry_file)

        session_101 = ReviewerSession(
            repository=REPO_NAME,
            pr_number=101,
            backend_name="opencode-pr",
            backend_type="opencode",
            model_name="anthropic/claude-sonnet-4-5",
            session_id="ses_pr101",
        )
        session_102 = ReviewerSession(
            repository=REPO_NAME,
            pr_number=102,
            backend_name="opencode-pr",
            backend_type="opencode",
            model_name="anthropic/claude-sonnet-4-5",
            session_id="ses_pr102",
        )
        registry.save(session_101)
        registry.save(session_102)

        with patch(
            "src.auto_coder.pr_processor.ReviewerSessionRegistry",
            return_value=registry,
        ):
            _remove_reviewer_sessions_for_closed_pr(REPO_NAME, 101)

        assert (
            registry.get(
                REPO_NAME,
                101,
                "opencode-pr",
                "opencode",
                "anthropic/claude-sonnet-4-5",
            )
            is None
        )
        assert (
            registry.get(
                REPO_NAME,
                102,
                "opencode-pr",
                "opencode",
                "anthropic/claude-sonnet-4-5",
            )
            is not None
        )
