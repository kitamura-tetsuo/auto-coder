"""Tests for the adversarial validation module."""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest

from auto_coder.adversarial_validator import (
    ADVERSARIAL_RESPONSE_PREVIEW_LIMIT,
    AdversarialValidationContext,
    AdversarialValidationFinding,
    AdversarialValidationResult,
    build_adversarial_validation_context,
    build_file_aware_diff,
    extract_all_changed_files,
    extract_changed_test_files,
    is_test_file,
    parse_adversarial_validation_response,
    run_adversarial_validation,
)
from auto_coder.automation_config import AutomationConfig
from auto_coder.prompt_loader import render_prompt
from auto_coder.trace_logger import get_trace_logger


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

    def test_git_quoted_utf8_path_is_decoded_into_complete_manifest(self):
        quoted_diff = r'diff --git "a/src/\346\227\245\346\234\254.py" "b/src/\346\227\245\346\234\254.py"' "\n" r'--- "a/src/\346\227\245\346\234\254.py"' "\n" r'+++ "b/src/\346\227\245\346\234\254.py"' "\n" "+changed = True\n"

        assert extract_all_changed_files(quoted_diff) == ["src/日本.py"]


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

    def test_inconclusive_with_concrete_finding_is_normalized_to_needs_fix(self):
        response = """{
  "result": "INCONCLUSIVE",
  "summary": "One file was unavailable, but a required test category is absent",
  "findings": [
    {
      "violated_requirement": "The Issue requires an HTTP-level regression test",
      "counterexample": "Given the complete test manifest, when coverage is inspected, then the specification requires an HTTP test, but only a service test exists, and current tests pass because they never cross the HTTP boundary",
      "test_gap": "No changed HTTP or E2E test is present",
      "suggested_regression_scenario": "Exercise diagnosis and apply through the HTTP connector"
    }
  ]
}"""

        result = parse_adversarial_validation_response(response)

        assert result.result == "NEEDS_FIX"
        assert result.needs_fix
        assert len(result.findings) == 1

    def test_parse_bare_json(self):
        json_resp = '{"result": "PASS", "summary": "Looks good", "findings": []}'
        result = parse_adversarial_validation_response(json_resp)
        assert result.is_pass
        assert result.summary == "Looks good"

    def test_parse_concrete_findings_override_contradictory_pass_label(self):
        """A valid counterexample must survive a contradictory top-level PASS."""
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
        assert result.raw_response == json_resp

    def test_parse_malformed_findings_with_empty_dict_fails_closed_to_error(self):
        """Malformed findings containing empty dict must fail closed to ERROR."""
        json_resp = """{
  "result": "PASS",
  "findings": [{}]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_malformed_findings_with_numbers_fails_closed_to_error(self):
        """Malformed findings containing non-dict items must fail closed to ERROR."""
        json_resp = """{
  "result": "PASS",
  "findings": [123]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

    def test_parse_needs_fix_missing_counterexample_fails_closed_to_error(self):
        """NEEDS_FIX without required concrete counterexample must NOT synthesize fake counterexample, fails closed to ERROR."""
        json_resp = """{
  "result": "NEEDS_FIX",
  "findings": [
    {"violated_requirement": "Maybe caching is wrong"}
  ]
}"""
        result = parse_adversarial_validation_response(json_resp)
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"

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

    def test_parse_text_concrete_finding_overrides_pass(self):
        """Structured text counterexamples also receive finding-first precedence."""
        text_resp = """RESULT: PASS
VIOLATED_REQUIREMENT: User session must expire after 2 hours
COUNTEREXAMPLE: Given state S, produces invalid token
"""
        result = parse_adversarial_validation_response(text_resp)
        assert not result.is_pass
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"

    def test_parse_empty_response_fails_closed_to_error(self):
        """Empty response must fail closed to ERROR and block merge."""
        result = parse_adversarial_validation_response("")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"
        assert result.diagnostic_category == "empty_response"
        assert "no non-whitespace content" in (result.diagnostic_reason or "")
        assert result.raw_response == ""

    def test_parse_malformed_response_fails_closed_to_error(self):
        """Unparseable/corrupted response must fail closed to ERROR."""
        result = parse_adversarial_validation_response("Just random chatter with no valid JSON or RESULT header.")
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "ERROR"
        assert result.diagnostic_category == "unrecognized_format"
        assert result.raw_response == "Just random chatter with no valid JSON or RESULT header."

    def test_parse_invalid_json_reports_syntax_location_and_fails_closed(self):
        response = '{"result": "PASS", "findings": [}'
        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "json_parse_error"
        assert "line 1" in (result.diagnostic_reason or "")
        assert result.raw_response == response

    def test_parse_valid_json_with_invalid_schema_reports_schema_reason(self):
        response = '{"result": "PASS", "findings": "none"}'
        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "schema_error"
        assert result.diagnostic_reason == "findings must be a list, got str"
        assert result.raw_response == response

    def test_parse_codex_jsonl_uses_final_agent_message_and_preserves_event_stream(self):
        final_message = '{"result":"PASS","summary":"All requirements verified","findings":[]}'
        response = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"Reviewing the change."}}',
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_1", "type": "agent_message", "text": final_message},
                    }
                ),
                '{"type":"turn.completed"}',
            ]
        )

        result = parse_adversarial_validation_response(response)

        assert result.is_pass
        assert result.summary == "All requirements verified"
        assert result.raw_response == response

    def test_parse_codex_jsonl_with_non_json_contamination_fails_closed(self):
        response = "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_0",
                            "type": "agent_message",
                            "text": '{"result":"PASS","findings":[]}',
                        },
                    }
                ),
                "unexpected stderr warning",
            ]
        )

        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "cli_event_stream_error"
        assert "non-JSON content" in (result.diagnostic_reason or "")
        assert result.raw_response == response

    @pytest.mark.parametrize(
        ("event_line", "expected_reason"),
        [
            ('{"type":"turn.completed"}', "no completed agent message"),
            ('{"type":"turn.failed","error":{"message":"sandbox unavailable"}}', "turn.failed"),
        ],
    )
    def test_parse_incomplete_or_failed_codex_jsonl_fails_closed(self, event_line, expected_reason):
        response = "\n".join(['{"type":"thread.started","thread_id":"thread-1"}', event_line])

        result = parse_adversarial_validation_response(response)

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.diagnostic_category == "cli_event_stream_error"
        assert expected_reason in (result.diagnostic_reason or "")
        assert result.raw_response == response


class TestBuildAdversarialValidationContext:
    """Test gathering of context for adversarial validation."""

    def test_build_context(self):
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/tests/test_x.py b/tests/test_x.py\n+++ b/tests/test_x.py"
        mock_issue = MagicMock(spec=["title", "body"])
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

    def test_build_context_uses_file_aware_evidence_for_large_diff(self):
        mock_client = MagicMock()
        huge_diff = "diff --git a/file1.py b/file1.py\n+++ b/file1.py\n" + ("+" + "a" * 100 + "\n") * 500 + "diff --git a/file_late.py b/file_late.py\n+++ b/file_late.py\n"
        mock_client.get_pr_diff.return_value = huge_diff
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Big change"
        mock_issue.body = "Spec details"
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        config.MAX_PR_DIFF_SIZE = 100  # small threshold to trigger truncation
        pr_data = {"number": 99, "title": "Big PR", "body": "Fixes #10"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert context.is_diff_truncated
        assert "COVERAGE INCOMPLETE" in context.pr_diff
        assert "### Changed file: file_late.py" in context.pr_diff
        assert "file_late.py" in context.all_changed_files
        assert "file1.py" in context.unverified_files

    def test_rendered_prompt_preserves_late_file_patch_and_complete_manifests(self):
        """A large early patch must not hide a later material file from the prompt."""
        mock_client = MagicMock()
        diff_prefix = "diff --git a/src/early.py b/src/early.py\n+++ b/src/early.py\n" + ("+early_line\n" * 200)
        diff_suffix = "diff --git a/src/late_secret_feature.py b/src/late_secret_feature.py\n+++ b/src/late_secret_feature.py\n+late_line\n"
        huge_diff = diff_prefix + diff_suffix

        mock_client.get_pr_diff.return_value = huge_diff
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Complex feature"
        mock_issue.body = "Spec: Must handle late secret feature."
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        config.MAX_PR_DIFF_SIZE = 200
        pr_data = {"number": 100, "title": "Complex PR", "body": "Fixes #10"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)

        changed_tests_str = "\n".join(f"- {t}" for t in context.changed_tests) if context.changed_tests else "(No test files detected in diff)"
        rendered_prompt = render_prompt(
            "pr.adversarial_validation",
            repo_name="owner/repo",
            pr_number=100,
            pr_title=context.pr_title,
            pr_body=context.pr_body,
            pr_diff=context.pr_diff,
            linked_issues_context=context.issue_context,
            changed_tests=changed_tests_str,
            changed_files="\n".join(f"- {path}" for path in context.all_changed_files),
            coverage_status="INCOMPLETE",
        )

        assert "Complete Changed-File Manifest" in rendered_prompt
        assert "src/late_secret_feature.py" in rendered_prompt
        assert "+late_line" in rendered_prompt
        assert "Coverage: COMPLETE" in rendered_prompt

    def test_oversized_first_file_does_not_starve_later_security_and_test_files(self):
        huge_patch = "diff --git a/generated.txt b/generated.txt\n+++ b/generated.txt\n" + "+generated\n" * 1000
        security_patch = "diff --git a/src/security.py b/src/security.py\n+++ b/src/security.py\n+reject_unsafe_input()\n"
        test_patch = "diff --git a/tests/test_security.py b/tests/test_security.py\n+++ b/tests/test_security.py\n+assert_rejected()\n"

        evidence, unverified = build_file_aware_diff(huge_patch + security_patch + test_patch, 700)

        assert "generated.txt" in unverified
        assert "### Changed file: src/security.py" in evidence
        assert "+reject_unsafe_input()" in evidence
        assert "### Changed file: tests/test_security.py" in evidence
        assert "+assert_rejected()" in evidence

    def test_quoted_path_before_normal_file_is_never_omitted_or_reported_complete(self):
        quoted_patch = r'diff --git "a/src/\346\227\245\346\234\254.py" "b/src/\346\227\245\346\234\254.py"' "\n" r'--- "a/src/\346\227\245\346\234\254.py"' "\n" r'+++ "b/src/\346\227\245\346\234\254.py"' "\n" + "+quoted_change\n" * 100
        normal_patch = "diff --git a/src/normal.py b/src/normal.py\n+++ b/src/normal.py\n+normal_change\n"

        evidence, unverified = build_file_aware_diff(quoted_patch + normal_patch, 300)

        assert "### Changed file: src/normal.py" in evidence
        assert "src/日本.py" in unverified
        assert "COVERAGE INCOMPLETE" in evidence

    def test_many_file_evidence_obeys_hard_size_bound(self):
        patches = []
        for index in range(100):
            path = f"src/generated/component_{index:03d}_with_a_descriptive_name.py"
            patches.append(f"diff --git a/{path} b/{path}\n+++ b/{path}\n+value_{index} = True\n")

        evidence, unverified = build_file_aware_diff("".join(patches), 6000)

        assert len(evidence) <= 6000
        assert unverified
        assert "COVERAGE INCOMPLETE" in evidence
        assert "Unverified manifest SHA-256:" in evidence

    @pytest.mark.parametrize("late_file_first", [False, True])
    def test_violating_file_evidence_is_order_independent(self, late_file_first):
        huge_patch = "diff --git a/generated.txt b/generated.txt\n+++ b/generated.txt\n" + "+generated\n" * 1000
        violation_patch = "diff --git a/src/violation.py b/src/violation.py\n+++ b/src/violation.py\n+allow_forbidden_state()\n"
        raw_diff = violation_patch + huge_patch if late_file_first else huge_patch + violation_patch

        evidence, _ = build_file_aware_diff(raw_diff, 500)

        assert "+allow_forbidden_state()" in evidence

    def test_hierarchical_oracle_selection_prefers_explicit_linking_keyword(self):
        """When explicit linking keywords exist in body, other reference issues are NOT included."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"

        def get_issue_side_effect(repo, issue_num):
            m = MagicMock(spec=["title", "body"])
            if issue_num == 100:
                m.title = "Core Feature Specification"
                m.body = "Specification: Requirement A and B."
                return m
            elif issue_num == 200:
                m.title = "Background Discussion Only"
                m.body = "Discussion: Not the PR requirement."
                return m
            return None

        mock_client.get_issue.side_effect = get_issue_side_effect
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        # PR body explicitly links #100, while merely referencing #200
        pr_data = {
            "number": 50,
            "title": "feat: implementation",
            "body": "Fixes #100\n\nThis implementation follows the approach discussed in issue #200.",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #100" in context.issue_context
        assert "Linked Issue #200" not in context.issue_context

    def test_unaggregated_inconclusive_result_remains_blocked_until_precedence_is_applied(self):
        """Direct result construction remains fail-closed; parsed results apply precedence."""
        finding = AdversarialValidationFinding(
            violated_requirement="Spec invariant",
            counterexample="Given state S, action A produces X",
        )
        res = AdversarialValidationResult(
            result="INCONCLUSIVE",
            summary="Need dynamic verification",
            findings=[finding],
        )
        assert not res.needs_fix
        assert not res.is_pass
        assert res.is_blocked

    def test_parent_issue_scope_boundary_notice(self):
        """Parent issue context includes explicit SCOPE BOUNDARY NOTICE ensuring sub-issue PR scope is preserved."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Sub-issue A"
        mock_issue.body = "Specification: Implement feature A only."
        mock_client.get_issue.return_value = mock_issue

        mock_client.get_parent_issue_details.return_value = {"number": 100, "title": "Epic Feature A, B and C"}
        mock_client.get_parent_issue_body.return_value = "Parent Specification: Must implement A, B, and C."

        config = AutomationConfig()
        pr_data = {"number": 10, "title": "feat: feature A", "body": "Fixes #101"}

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #101: Sub-issue A" in context.issue_context
        assert "SCOPE BOUNDARY NOTICE" in context.issue_context
        assert "Do NOT require parent requirements outside the child issue scope" in context.issue_context
        assert "Parent Issue #100 (CONTEXT ONLY" in context.issue_context

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_enforces_permission_mode_plan_on_noedit(self, mock_sub_run, mock_get_config):
        """ClaudeClient must inject --permission-mode plan when is_noedit=True even if no options_for_noedit configured."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--max-thinking-tokens", "1000"]
        mock_backend.options_for_noedit = []
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--permission-mode" in called_cmd
            assert "plan" in called_cmd

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_overrides_configured_and_extra_args_write_modes_in_noedit(self, mock_sub_run, mock_get_config):
        """ClaudeClient must override configured bypassPermissions and strip extra args --dangerously-skip-permissions."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--permission-mode", "bypassPermissions"]
        mock_backend.options_for_noedit = ["--permission-mode", "bypassPermissions"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--permission-mode", "bypassPermissions"],
            "options_for_noedit": ["--permission-mode", "bypassPermissions"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._extra_args = ["--dangerously-skip-permissions", "--permission-mode", "default"]
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" not in called_cmd
            assert "bypassPermissions" not in called_cmd
            assert "default" not in called_cmd
            assert "--permission-mode" in called_cmd
            assert "plan" in called_cmd

    @patch("auto_coder.claude_client.get_llm_config")
    @patch("auto_coder.claude_client.subprocess.run")
    def test_claude_client_preserves_writable_mode_when_not_noedit(self, mock_sub_run, mock_get_config):
        """Normal implementation runs (is_noedit=False) retain writable configurations."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "claude-3-5-sonnet-20241022"
        mock_backend.options = ["--dangerously-skip-permissions"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["--dangerously-skip-permissions"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.claude_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.claude_client import ClaudeClient

            client = ClaudeClient(backend_name="claude")
            client._run_llm_cli("prompt", is_noedit=False)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-skip-permissions" in called_cmd
            assert "--permission-mode" not in called_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_enforces_sandbox_readonly_on_noedit(self, mock_sub_run, mock_get_config):
        """CodexClient must inject --sandbox read-only when is_noedit=True even if no options_for_noedit configured."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["exec", "--json"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["exec", "--json"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "--json",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_overrides_configured_and_extra_args_write_modes_in_noedit(self, mock_sub_run, mock_get_config):
        """CodexClient must override configured workspace-write and strip extra args danger-full-access/full-auto."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--sandbox", "workspace-write", "exec", "--full-auto"]
        mock_backend.options_for_noedit = ["--sandbox", "workspace-write", "exec"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--sandbox", "workspace-write", "exec", "--full-auto"],
            "options_for_noedit": ["--sandbox", "workspace-write", "exec"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._extra_args = ["--sandbox", "danger-full-access", "-y", "--ask-for-approval", "always"]
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "workspace-write" not in called_cmd
            assert "danger-full-access" not in called_cmd
            assert "--full-auto" not in called_cmd
            assert "-y" not in called_cmd
            assert "always" not in called_cmd
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_strips_yolo_and_dangerously_bypass_approvals_and_sandbox(self, mock_sub_run, mock_get_config):
        """CodexClient must strip real YOLO/bypass flags and -s alias and enforce -c approvals_reviewer="user"."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--dangerously-bypass-approvals-and-sandbox", "exec", "-s", "workspace-write"]
        mock_backend.options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox", "exec"]
        mock_backend.replace_placeholders.return_value = {
            "options": ["--dangerously-bypass-approvals-and-sandbox", "exec", "-s", "workspace-write"],
            "options_for_noedit": ["--dangerously-bypass-approvals-and-sandbox", "exec"],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._extra_args = ["--yolo", "--approve-for-me", "--not-so-yolo", "-c", 'approvals_reviewer="auto_review"']
            client._run_llm_cli("prompt", is_noedit=True)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--dangerously-bypass-approvals-and-sandbox" not in called_cmd
            assert "--yolo" not in called_cmd
            assert "--approve-for-me" not in called_cmd
            assert "--not-so-yolo" not in called_cmd
            assert "workspace-write" not in called_cmd
            assert 'approvals_reviewer="auto_review"' not in called_cmd
            expected_cmd = [
                "codex",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "-c",
                'approvals_reviewer="user"',
                "exec",
                "prompt",
            ]
            assert called_cmd == expected_cmd

    @patch("auto_coder.codex_client.get_llm_config")
    @patch("auto_coder.codex_client.subprocess.run")
    def test_codex_client_preserves_writable_mode_when_not_noedit(self, mock_sub_run, mock_get_config):
        """Normal implementation runs (is_noedit=False) retain writable configurations."""
        mock_sub_run.return_value.returncode = 0
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.model = "codex-model"
        mock_backend.options = ["--sandbox", "workspace-write", "--full-auto"]
        mock_backend.options_for_noedit = []
        mock_backend.replace_placeholders.return_value = {
            "options": ["--sandbox", "workspace-write", "--full-auto"],
            "options_for_noedit": [],
        }
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        with patch("auto_coder.codex_client.CommandExecutor.run_command") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="output", stderr="", returncode=0)
            from auto_coder.codex_client import CodexClient

            client = CodexClient(backend_name="codex")
            client._run_llm_cli("prompt", is_noedit=False)

            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--sandbox" in called_cmd
            assert "workspace-write" in called_cmd
            assert "--full-auto" in called_cmd

    def test_oracle_recovery_falls_back_to_title_when_no_body_link(self):
        """Issue specification is recovered from PR title when body omits linking phrase."""
        mock_client = MagicMock()
        mock_client.get_pr_diff.return_value = "diff --git a/src/main.py b/src/main.py\n+++ b/src/main.py"
        mock_issue = MagicMock(spec=["title", "body"])
        mock_issue.title = "Implement rate limiting"
        mock_issue.body = "Spec: Limit to 100 req/min"
        mock_client.get_issue.return_value = mock_issue
        mock_client.get_parent_issue_details.return_value = None

        config = AutomationConfig()
        # PR body has no linking keyword
        pr_data = {
            "number": 200,
            "title": "feat: rate limiting implementation for issue #1567",
            "body": "This PR updates the rate limiting algorithm.",
        }

        context = build_adversarial_validation_context("owner/repo", pr_data, config, github_client=mock_client)
        assert "Linked Issue #1567" in context.issue_context
        assert "Limit to 100 req/min" in context.issue_context


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
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Valid implementation", "requirement_coverage_complete": true, "unverified_requirements": [], "findings": []}'

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
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_parse_failure_trace_is_correlated_bounded_and_references_full_log(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=1582,
            pr_title="Diagnose malformed response",
            pr_body="Fixes #1582",
            pr_diff="diff content",
            issue_context="Malformed validation responses must remain blocked.",
        )
        response = "unexpected validator prose " + ("x" * (ADVERSARIAL_RESPONSE_PREVIEW_LIMIT + 500))
        mock_run_prompt.return_value = response
        backend_manager = MagicMock()
        backend_manager.get_last_backend_and_model.return_value = ("codex", "gpt-5.6")
        backend_manager.get_last_interaction_log_path.return_value = "/tmp/llm_output.jsonl"
        trace_logger = get_trace_logger()
        trace_logger.clear()

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 1582, "title": "Diagnose malformed response", "body": "Fixes #1582"},
            AutomationConfig(),
            backend_manager=backend_manager,
        )

        assert result.result == "ERROR"
        assert result.is_blocked
        assert result.raw_response == response
        parse_entries = [entry for entry in trace_logger.get_logs(item_type="pr", item_number=1582) if entry["category"] == "Adversarial Validation Parse Failure"]
        assert len(parse_entries) == 1
        details = parse_entries[0]["details"]
        assert details["attempt"] == "initial"
        assert details["backend"] == "codex"
        assert details["model"] == "gpt-5.6"
        assert details["response_state"] == "non-empty"
        assert details["response_length"] == len(response)
        assert details["diagnostic_category"] == "unrecognized_format"
        assert details["interaction_log"] == "/tmp/llm_output.jsonl"
        assert len(details["response_preview"]) <= ADVERSARIAL_RESPONSE_PREVIEW_LIMIT + 50
        assert "characters omitted" in details["response_preview"]

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
    def test_run_adversarial_validation_missing_diff_fails_closed_to_blocked(self, mock_build_ctx):
        """Diff retrieval failure must block merge (BLOCKED) rather than validating against empty diff."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="",  # Diff unavailable
            changed_tests=[],
            issue_context="Issue specification: Must do X.",
        )

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "Diff retrieval failed" in result.summary

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
    def test_concrete_finding_prevents_later_dynamic_uncertainty_from_erasing_it(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """A concrete finding wins immediately and is not downgraded by a later check."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.side_effect = [
            """{
  "result": "INCONCLUSIVE",
  "summary": "Suspected defect in state reload",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": [
    {
      "violated_requirement": "State reload invariant",
      "counterexample": "Given state S, when reload occurs, then persisted timestamp is lost",
      "test_gap": "Test does not check reload",
      "suggested_regression_scenario": "Test state reload"
    }
  ]
}""",
            '{"result": "PASS", "summary": "Reviewer confirmed reload output satisfies spec", "requirement_coverage_complete": true, "unverified_requirements": [], "findings": []}',
        ]
        # run_local_tests returns dict with output and errors
        mock_run_tests.return_value = {
            "success": True,
            "output": "PASSED tests/test_feature.py::test_reload_scenario",
            "errors": "DeprecationWarning: something deprecated",
        }

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert mock_run_prompt.call_count == 1
        mock_run_tests.assert_not_called()

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_is_rejected_when_material_file_evidence_is_incomplete(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Large change",
            pr_diff="bounded evidence",
            all_changed_files=["src/huge.py", "tests/test_feature.py"],
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification",
            is_diff_truncated=True,
            unverified_files=["src/huge.py"],
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Looks correct", "findings": []}'

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Large change"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert result.is_blocked
        assert result.diagnostic_category == "incomplete_evidence_coverage"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_without_structured_requirement_coverage_is_rejected(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
        )
        mock_run_prompt.return_value = '{"result": "PASS", "summary": "Reviewed R1", "findings": []}'

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert result.diagnostic_category == "incomplete_requirement_coverage"

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_pass_with_explicit_unverified_requirement_is_rejected(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Two requirements",
            pr_diff="complete bounded evidence",
            all_changed_files=["src/feature.py"],
            issue_context="R1: persist state. R2: emit an audit event.",
        )
        mock_run_prompt.return_value = """{
  "result": "PASS",
  "summary": "R1 was verified but R2 was not inspected",
  "requirement_coverage_complete": false,
  "unverified_requirements": ["R2: emit an audit event"],
  "findings": []
}"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Two requirements"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "INCONCLUSIVE"
        assert "R2: emit an audit event" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    def test_finding_wins_while_incomplete_coverage_remains_diagnostic(self, mock_run_prompt, mock_build_ctx):
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Large change",
            pr_diff="bounded evidence",
            all_changed_files=["src/huge.py", "tests/service_test.py"],
            changed_tests=["tests/service_test.py"],
            issue_context="Issue requires service and HTTP tests",
            unverified_files=["src/huge.py"],
        )
        mock_run_prompt.return_value = """{
  "result": "INCONCLUSIVE",
  "summary": "Implementation file is partial, but required HTTP coverage is absent",
  "findings": [{
    "violated_requirement": "HTTP-level tests are required",
    "counterexample": "Given the complete test manifest, when required categories are compared, then an HTTP test is required, but only a service test exists, and CI passes because no HTTP test runs",
    "test_gap": "The changed-test manifest has no HTTP test",
    "suggested_regression_scenario": "Add an HTTP connector regression test"
  }]
}"""

        result = run_adversarial_validation(
            "owner/repo",
            {"number": 100, "title": "Large change"},
            AutomationConfig(),
            backend_manager=MagicMock(),
        )

        assert result.result == "NEEDS_FIX"
        assert result.needs_fix
        assert result.diagnostic_category == "incomplete_evidence_coverage"
        assert "src/huge.py" in (result.diagnostic_reason or "")

    @patch("auto_coder.adversarial_validator.build_adversarial_validation_context")
    @patch("auto_coder.adversarial_validator.run_llm_prompt")
    @patch("auto_coder.fix_to_pass_tests_runner.run_local_tests")
    def test_run_adversarial_validation_dynamic_check_failure_routes_to_reviewer(self, mock_run_tests, mock_run_prompt, mock_build_ctx):
        """Failing dynamic check is sent to the reviewer for semantic determination against the counterexample."""
        mock_build_ctx.return_value = AdversarialValidationContext(
            repo_name="owner/repo",
            pr_number=100,
            pr_title="Add feature",
            pr_body="Fixes #1",
            pr_diff="diff content",
            changed_tests=["tests/test_feature.py"],
            issue_context="Issue specification: Must do X.",
        )
        mock_run_prompt.side_effect = [
            """{
  "result": "INCONCLUSIVE",
  "summary": "Need dynamic verification",
  "dynamic_check_requested": "tests/test_feature.py",
  "findings": []
}""",
            """{
  "result": "NEEDS_FIX",
  "summary": "Test failure confirmed the suspected specification violation",
  "findings": [
    {
      "violated_requirement": "State reload invariant",
      "counterexample": "Given state S, produces X",
      "test_gap": "Test failed on reload",
      "suggested_regression_scenario": "Fix reload logic"
    }
  ]
}""",
        ]
        mock_run_tests.return_value = {
            "success": False,
            "output": "FAILED tests/test_feature.py::test_reload",
            "errors": "AssertionError: 1 != 2",
        }

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert result.needs_fix
        assert result.result == "NEEDS_FIX"
        assert len(result.findings) == 1
        assert mock_run_prompt.call_count == 2

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
        mock_run_prompt.return_value = '{"result": "INCONCLUSIVE", "summary": "Need dynamic test", "dynamic_check_requested": "tests/test_feature.py", "findings": []}'
        mock_run_tests.side_effect = RuntimeError("Test runner crashed")

        config = AutomationConfig()
        pr_data = {"number": 100, "title": "Add feature", "body": "Fixes #1"}

        result = run_adversarial_validation("owner/repo", pr_data, config, backend_manager=MagicMock())
        assert not result.is_pass
        assert result.is_blocked
        assert result.result == "BLOCKED"
        assert "could not be completed" in result.summary
