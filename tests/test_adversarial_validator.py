"""Tests for the adversarial validation module."""

from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    AdversarialValidationContext,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    apply_adversarial_fix,
    build_adversarial_validation_context,
    extract_all_changed_files,
    extract_changed_test_files,
    is_test_file,
    parse_adversarial_validation_response,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig


class TestExtractChangedTestFiles:
    """Test extraction of test files from unified git diffs."""

    def test_extract_python_test_files(self):
        diff = """diff --git a/src/auto_coder/main.py b/src/auto_coder/main.py
--- a/src/auto_coder/main.py
+++ b/src/auto_coder/main.py
@@ -1,3 +1,4 @@
+print('hello')
diff --git a/tests/test_main.py b/tests/test_main.py
new file mode 100644
--- /dev/null
+++ b/tests/test_main.py
@@ -0,0 +1,5 @@
+def test_something():
+    assert True
diff --git a/src/service_test.py b/src/service_test.py
--- a/src/service_test.py
+++ b/src/service_test.py
@@ -10,3 +10,4 @@
"""
        test_files = extract_changed_test_files(diff)
        assert "tests/test_main.py" in test_files
        assert "src/service_test.py" in test_files
        assert "src/auto_coder/main.py" not in test_files

    def test_empty_diff(self):
        assert extract_changed_test_files("") == []
        assert extract_all_changed_files("") == []


class TestParseAdversarialValidationResponse:
    """Test parsing of strong-model validation output."""

    def test_parse_json_pass(self):
        json_resp = """```json
{
  "result": "PASS",
  "summary": "All acceptance criteria verified against implementation.",
  "dynamic_check_requested": null,
  "findings": []
}
```"""
        result = parse_adversarial_validation_response(json_resp)
        assert result.is_pass
        assert not result.needs_fix
        assert result.result == "PASS"
        assert "All acceptance criteria" in result.summary
        assert len(result.findings) == 0

    def test_parse_json_needs_fix(self):
        json_resp = """```json
{
  "result": "NEEDS_FIX",
  "summary": "Found 1 subtle specification violation in edge case handling.",
  "findings": [
    {
      "violated_requirement": "State must be persisted before event dispatch",
      "counterexample": "Given state S, when action A occurs, then specification requires R, but implementation produces X, and tests pass because mock ignores order",
      "test_gap": "Current unit tests assert both calls happen but not the order",
      "suggested_regression_scenario": "Test state persistence timestamp is strictly before dispatch timestamp"
    }
  ]
}
```"""
        result = parse_adversarial_validation_response(json_resp)
        assert result.needs_fix
        assert not result.is_pass
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.violated_requirement == "State must be persisted before event dispatch"
        assert "Given state S" in finding.counterexample
        assert "assert both calls happen" in finding.test_gap
        assert "strictly before" in finding.suggested_regression_scenario

    def test_parse_bare_json(self):
        json_resp = '{"result": "PASS", "summary": "Looks good", "findings": []}'
        result = parse_adversarial_validation_response(json_resp)
        assert result.is_pass
        assert result.summary == "Looks good"

    def test_parse_contradictory_pass_with_findings_converts_to_needs_fix(self):
        """Contradictory output (result: PASS but findings present) must fail closed to NEEDS_FIX."""
        json_resp = """{
  "result": "PASS",
  "summary": "Says pass but listed a bug",
  "findings": [
    {
      "violated_requirement": "Spec requirement R",
      "counterexample": "Given state S, produces X",
      "test_gap": "Gap G",
      "suggested_regression_scenario": "Scenario T"
    }
  ]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1

    def test_parse_text_fallback_needs_fix(self):
        text_resp = """RESULT: NEEDS_FIX
VIOLATED_REQUIREMENT: User session must expire after 2 hours
COUNTEREXAMPLE: Given state S, when token is 3 hours old, then specification requires logout, but current implementation accepts it because timestamp is checked against local time instead of UTC
TEST_GAP: Tests mock datetime.now() with UTC timezone directly
SUGGESTED_REGRESSION_SCENARIO: Assert token expiration with timezone offset differences
"""
        result = parse_adversarial_validation_response(text_resp)
        assert result.needs_fix
        assert len(result.findings) == 1
        assert "User session must expire" in result.findings[0].violated_requirement
        assert "Given state S" in result.findings[0].counterexample

    def test_parse_empty_response_fails_closed_to_error(self):
        """Empty response must fail closed to ERROR and block merge."""
        result = parse_adversarial_validation_response("")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_malformed_response_fails_closed_to_error(self):
        """Unparseable/corrupted response must fail closed to ERROR."""
        result = parse_adversarial_validation_response("Just random chatter with no valid JSON or RESULT header.")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"


class TestBuildAdversarialValidationContext:
    """Test gathering of context for adversarial validation."""

    def test_build_context(self):
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/tests/test_x.py b/tests/test_x.py\n+++ b/tests/test_x.py"
        mock_issue = MagicMock()
        mock_issue.title = "Add rate limiting"
        mock_issue.body = "Specification: Limit to 100 req/min. Acceptance Criteria: Return 429 when exceeded."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        pr_data = {
            "number": 42,
            "title": "Implement rate limiting",
            "body": "Fixes #10",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert context.pr_number == 42
        assert context.pr_title == "Implement rate limiting"
        assert "tests/test_x.py" in context.changed_tests
        assert "Linked Issue #10" in context.issue_context
        assert not context.is_diff_truncated

    def test_build_context_with_truncation_warning(self):
        mock_client = MagicMock()
        huge_diff = "diff --git a/file1.py b/file1.py\n+++ b/file1.py\n" + ("+" + "a" * 100 + "\n") * 500
        mock_client.get_pr_diff.return_value = huge_diff
        mock_issue = MagicMock()
        mock_issue.title = "Big change"
        mock_issue.body = "Spec details"
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        config.MAX_PR_DIFF_SIZE = 100  # small threshold to trigger truncation
        pr_data = {"number": 99, "title": "Big PR", "body": "Fixes #10"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert context.is_diff_truncated
        assert "WARNING: PR Diff was truncated" in context.pr_diff
        assert "file1.py" in context.all_changed_files


class TestRunAdversarialValidation:
    """Test executing adversarial validation."""

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_pass(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Valid implementation", "findings": []}'

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.is_pass
        assert result.result == "PASS"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_run_adversarial_validation_needs_fix(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = """{
  "result": "NEEDS_FIX",
  "summary": "Found violation",
  "findings": [
    {
      "violated_requirement": "Spec X",
      "counterexample": "Given state S, when A occurs, then R, but produces X, tests pass because Y",
      "test_gap": "Gap",
      "suggested_regression_scenario": "Scenario"
    }
  ]
}"""

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert len(result.findings) == 1

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    def test_run_adversarial_validation_no_issue_context_fails_closed_to_blocked(self, mock_build_ctx):
        """Oracle acquisition failure must block merge (BLOCKED), not pass."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="No linked issue",
            pr_diff="diff content",
            changed_tests=[],
            issue_context="",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": ""}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "Oracle acquisition failed" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.cli_helpers.create_adversarial_validation_backend_manager", return_value=None)
    def test_run_adversarial_validation_no_backend_available_fails_closed(self, mock_mgr, mock_build_ctx):
        """No strong backend configured or available must fail closed to BLOCKED."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=[],
            issue_context="Spec content",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=None)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "No strong adversarial validation backend configured" in result.summary

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_run_adversarial_validation_dynamic_check_failure(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Looks ok statically", "dynamic_check_requested": "tests/test_feature.py", "findings": []}'
        mock_run_tests.return_value = {"success": False, "stderr": "AssertionError"}

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert len(result.findings) == 1
        assert "Dynamic check failed" in result.findings[0].violated_requirement

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_run_adversarial_validation_dynamic_check_exception_fails_closed(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Looks ok statically", "dynamic_check_requested": "tests/test_feature.py", "findings": []}'
        mock_run_tests.side_effect = RuntimeError("Test runner crashed")

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "could not be completed" in result.summary


class TestApplyAdversarialFix:
    """Test applying regression test and fix on PR branch."""

    @patch("auto_coder.adversarial_validator.cmd")
    @patch("auto_coder.adversarial_validator.git_commit_with_retry")
    @patch("auto_coder.adversarial_validator.git_push")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.adversarial_validator.get_commit_log")
    def test_apply_adversarial_fix_with_regression_test(self, mock_commit_log, mock_run_prompt, mock_git_push, mock_git_commit, mock_cmd):
        mock_commit_log.return_value = "commit log"
        mock_run_prompt.return_value = "ACTION_SUMMARY: Added regression test and fixed validation error"
        mock_cmd.run_command.return_value = MagicMock(success=True, stdout=" M src/code.py\n A tests/test_regression.py")
        mock_git_commit.return_value = MagicMock(success=True)
        mock_git_push.return_value = MagicMock(success=True)

        config = AutomationConfig()
        pr_data = {"number": 50, "title": "Feature PR", "body": "Fixes #5"}
        val_result = AdversarialValidationResult(
            result="NEEDS_FIX",
            summary="Found violation",
            findings=[
                AdversarialValidationFinding(
                    violated_requirement="Spec requires Y",
                    counterexample="Given state S, when action A occurs, then R, but produces X, tests pass because Z",
                    test_gap="Test did not verify boundary condition",
                    suggested_regression_scenario="Add boundary test",
                )
            ],
        )

        actions = apply_adversarial_fix("owner/repo", pr_data, config, val_result)
        assert any("Committed regression test (tests/test_regression.py)" in a for a in actions)
        assert any("Pushed adversarial fixes" in a for a in actions)
        mock_git_commit.assert_called_once()
        mock_git_push.assert_called_once()
