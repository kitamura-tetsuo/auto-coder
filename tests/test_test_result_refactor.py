import textwrap
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.fix_to_pass_tests_runner import extract_important_errors_from_local_tests
from src.auto_coder.pr_processor import _apply_github_actions_fix
from src.auto_coder.test_result import TestResult


def test_testresult_dataclass_structure():
    """Test that TestResult dataclass properly stores all required fields and defaults."""
    result = TestResult(
        success=False,
        output="test output",
        errors="test errors",
        return_code=1,
    )
    assert result.success is False
    assert result.output == "test output"
    assert result.errors == "test errors"
    assert result.return_code == 1
    # Defaults
    assert result.command == ""
    assert result.test_file is None
    assert result.stability_issue is False
    assert result.extraction_context == {}
    assert result.framework_type is None


def test_enhanced_error_extraction_playwright_block():
    """Test that error extraction maintains accuracy with TestResult for Playwright-like logs."""
    fake_playwright_output = textwrap.dedent(
        """
        1) [core] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link

          Error: expect(received).toContain(expected) // indexOf

          Expected substring: "<a href=\"https://example.com\"\n            target=\"_blank\"\n            class=\"internal\">Example</a>"
          Received string:    "<div>Example</div>"
        """
    )
    tr = TestResult(
        success=False,
        output=fake_playwright_output,
        errors="",
        return_code=1,
        framework_type="playwright",
    )
    errors = extract_important_errors_from_local_tests(tr)
    assert "Expected substring:" in errors
    assert "Received string:" in errors
    assert ".spec.ts" in errors


def test_enhanced_error_extraction_pytest_keywords():
    """Pytest-style traceback and error keywords should be captured from TestResult."""
    stdout = textwrap.dedent(
        """
        _________________________________ test_spam __________________________________
        tests/test_bar.py:12: in test_spam
            assert 1 == 2
        E   AssertionError: boom
        """
    )
    tr = TestResult(success=False, output=stdout, errors="", return_code=1, framework_type="pytest")
    errors = extract_important_errors_from_local_tests(tr)
    assert "AssertionError" in errors
    assert "tests/test_bar.py" in errors


def test_engine_extract_important_errors_accepts_testresult(mock_github_client, mock_gemini_client):
    """AutomationEngine._extract_important_errors should accept a TestResult instance."""
    engine = AutomationEngine(mock_github_client)
    tr = TestResult(
        success=False,
        output="Running tests...\nERROR: Test failed\nMore output\nFAILED: assertion error",
        errors="ImportError: module not found",
        return_code=1,
    )
    result = engine._extract_important_errors(tr)
    assert "ERROR: Test failed" in result
    assert "FAILED: assertion error" in result
    assert "ImportError: module not found" in result


def test_github_actions_enhanced_integration_passes_structured_context(monkeypatch):
    """_apply_github_actions_fix should include structured context when TestResult is provided."""
    config = AutomationConfig()
    monkeypatch.setattr(config, "JULES_MODE", False)
    pr_data = {"number": 123, "title": "Fix CI", "head": {"ref": "test-branch"}}
    github_logs = "Simulated GitHub Actions logs"

    # Provide structured context in TestResult
    tr = TestResult(
        success=False,
        output="",
        errors="",
        return_code=1,
        framework_type="playwright",
    )
    tr.extraction_context = {
        "failed_tests": ["e2e/core/foo.spec.ts"],
        "summary": "1 failed",
    }

    captured = {}

    def fake_render_prompt(key, **kwargs):
        captured["key"] = key
        captured.update(kwargs)
        return "PROMPT"

    with (
        patch("src.auto_coder.pr_processor.render_prompt", side_effect=fake_render_prompt) as mock_render,
        patch("src.auto_coder.pr_processor.run_llm_prompt", return_value="OK") as mock_llm,
        patch("src.auto_coder.pr_processor.get_commit_log", return_value="commit log") as _,
    ):
        actions = _apply_github_actions_fix("owner/repo", pr_data, config, github_logs, test_result=tr)

    # Verify prompt was rendered with structured data
    assert captured.get("key") == "pr.github_actions_fix"
    assert captured.get("structured_errors") == tr.extraction_context
    assert captured.get("framework_type") == "playwright"
    assert "Applied GitHub Actions fix" in actions[0]
