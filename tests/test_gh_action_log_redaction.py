from unittest.mock import MagicMock, patch

import pytest

from auto_coder.test_log_utils import _clean_log_line as _clean_log_line_test_utils
from auto_coder.test_log_utils import generate_merged_playwright_report
from auto_coder.util.github_action import _clean_log_line, _extract_error_context, _filter_eslint_log


def test_clean_log_line_redaction():
    secret = "ghp_SECRETTOKEN123456789"
    line = f"Some log with {secret} inside."
    cleaned = _clean_log_line(line)

    assert secret not in cleaned, "Secret should be redacted in clean_log_line"
    assert "[REDACTED]" in cleaned


def test_extract_error_context_redaction():
    secret = "sk-SECRETKEY1234567890123456789012345678901234567890123456"
    content = f"""
    Error: Something failed
    Caused by: {secret}
    """
    extracted = _extract_error_context(content)
    assert secret not in extracted, "Secret should be redacted in error context"
    assert "[REDACTED]" in extracted


def test_filter_eslint_log_redaction():
    secret = "ghp_SECRETTOKEN123456789"
    content = f"""
    /path/to/file.ts:10:20: Error: {secret} is invalid
    """
    filtered = _filter_eslint_log(content)
    assert secret not in filtered, "Secret should be redacted in eslint log"
    assert "[REDACTED]" in filtered


def test_playwright_report_redaction():
    secret = "ghp_SECRETTOKEN123456789"
    report = {
        "suites": [
            {
                "file": "tests/e2e/test.spec.ts",
                "specs": [
                    {
                        "title": "Should fail securely",
                        "tests": [{"results": [{"status": "failed", "errors": [{"message": f"Error with {secret}", "stack": f"Error: {secret}\n    at /path/to/file.ts:10:20"}], "stdout": [{"text": f"Leaked {secret} in stdout"}], "stderr": [{"text": f"Leaked {secret} in stderr"}]}]}],
                    }
                ],
            }
        ]
    }

    summary = generate_merged_playwright_report([report])
    assert secret not in summary, "Secret should be redacted in playwright report summary"
    assert "[REDACTED]" in summary


def test_clean_log_line_test_utils_redaction():
    secret = "ghp_SECRETTOKEN123456789"
    line = f"Some log with {secret} inside."
    cleaned = _clean_log_line_test_utils(line)

    assert secret not in cleaned, "Secret should be redacted in test_log_utils._clean_log_line"
    assert "[REDACTED]" in cleaned
