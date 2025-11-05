_extract_error_context function test

import pytest

from src.auto_coder.util.github_action import _extract_error_context


def test_extract_error_context_with_playwright_error():
    """Verify that sufficient context can be extracted from Playwright error logs"""
    # Sample similar to actual Playwright error logs
    log_content = """
2025-10-27T03:25:50.0000000Z Running tests...
2025-10-27T03:25:51.0000000Z 
2025-10-27T03:25:52.0000000Z   ✓ [basic] › e2e/basic/test1.spec.ts:10:5 › Test 1 (100ms)
2025-10-27T03:25:53.0000000Z   ✓ [basic] › e2e/basic/test2.spec.ts:15:5 › Test 2 (150ms)
2025-10-27T03:25:54.0000000Z 
2025-10-27T03:25:55.0000000Z   1) [core] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:25:56.0000000Z 
2025-10-27T03:25:57.0000000Z     Error: expect(received).toContain(expected) // indexOf
2025-10-27T03:25:58.0000000Z 
2025-10-27T03:25:59.0000000Z     Expected substring: "<a href=\\"https://example.com\\""
2025-10-27T03:26:00.0000000Z     Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"
2025-10-27T03:26:01.0000000Z 
2025-10-27T03:26:02.0000000Z       47 |
2025-10-27T03:26:03.0000000Z       48 |         const firstItemHtml = await page.locator(".outliner-item").first().locator(".item-text").innerHTML();
2025-10-27T03:26:04.0000000Z     > 49 |         expect(firstItemHtml).toContain('<a href="https://example.com"');
2025-10-27T03:26:05.0000000Z          |                               ^
2025-10-27T03:26:06.0000000Z       50 |         expect(firstItemHtml).toContain(">https://example.com</a>");
2025-10-27T03:26:07.0000000Z       51 |     });
2025-10-27T03:26:08.0000000Z       52 |
2025-10-27T03:26:09.0000000Z   1 failed
2025-10-27T03:26:10.0000000Z   147 passed
2025-10-27T03:26:11.0000000Z   1 skipped
2025-10-27T03:26:12.0000000Z   151 did not run
2025-10-27T03:26:13.0000000Z 
2025-10-27T03:26:14.0000000Z Tests completed
"""

    result = _extract_error_context(log_content)

    # Verify that error message is included
    assert "Error: expect(received).toContain(expected)" in result
    assert "Expected substring:" in result
    assert "Received string:" in result
    assert "fmt-url-label-links-a391b6c2.spec.ts" in result

    # Verify that context before and after error is included
    assert "URL label links" in result
    assert "expect(firstItemHtml).toContain" in result

    # Verify that line count is appropriate (includes 10 lines before and after error line)
    result_lines = result.split("\n")
    assert len(result_lines) >= 20  # At least 10 lines before and after error line
    assert len(result_lines) <= 500  # Maximum 500 lines


def test_extract_error_context_with_multiple_errors():
    """Verify that all error contexts can be extracted when there are multiple errors"""
    log_content = (
        """
2025-10-27T03:25:50.0000000Z Running tests...
2025-10-27T03:25:51.0000000Z 
2025-10-27T03:25:52.0000000Z   1) [core] › e2e/core/test1.spec.ts:10:5 › Test 1
2025-10-27T03:25:53.0000000Z 
2025-10-27T03:25:54.0000000Z     Error: Test 1 failed
2025-10-27T03:25:55.0000000Z     Expected: true
2025-10-27T03:25:56.0000000Z     Received: false
2025-10-27T03:25:57.0000000Z 

"""
        + "\n".join([f"2025-10-27T03:26:{i:02d}.0000000Z   Some log line {i}" for i in range(100)])
        + """
2025-10-27T03:28:00.0000000Z 
2025-10-27T03:28:01.0000000Z   2) [core] › e2e/core/test2.spec.ts:20:5 › Test 2
2025-10-27T03:28:02.0000000Z 
2025-10-27T03:28:03.0000000Z     Error: Test 2 failed
2025-10-27T03:28:04.0000000Z     Expected substring: "hello"
2025-10-27T03:28:05.0000000Z     Received string: "world"
2025-10-27T03:28:06.0000000Z 
2025-10-27T03:28:07.0000000Z   2 failed
2025-10-27T03:28:08.0000000Z   148 passed
"""
    )

    result = _extract_error_context(log_content)

    # Verify that both errors are included
    assert "Test 1 failed" in result
    assert "Test 2 failed" in result
    assert "test1.spec.ts" in result
    assert "test2.spec.ts" in result

    # Verify that details of both errors are included
    assert "Expected: true" in result
    assert "Received: false" in result
    assert 'Expected substring: "hello"' in result
    assert 'Received string: "world"' in result


def test_extract_error_context_with_long_log():
    """Verify that only important parts are extracted from long logs"""
    # Generate 1000 lines log (first 100 lines, error part, last 100 lines)
    log_lines = []

    # First 100 lines (no error)
    for i in range(100):
        log_lines.append(f"2025-10-27T03:25:{i:02d}.0000000Z   Setup line {i}")

    # Error part
    log_lines.extend(
        [
            "2025-10-27T03:26:00.0000000Z   1) [core] › e2e/core/critical-test.spec.ts:50:5 › Critical Test",
            "2025-10-27T03:26:01.0000000Z ",
            "2025-10-27T03:26:02.0000000Z     Error: Critical failure",
            '2025-10-27T03:26:03.0000000Z     Expected substring: "important data"',
            '2025-10-27T03:26:04.0000000Z     Received string: "wrong data"',
            "2025-10-27T03:26:05.0000000Z ",
            "2025-10-27T03:26:06.0000000Z     at critical-test.spec.ts:50:31",
            "2025-10-27T03:26:07.0000000Z ",
        ]
    )

    # Middle 700 lines (no error)
    for i in range(700):
        log_lines.append(f"2025-10-27T03:27:{i%60:02d}.0000000Z   Middle line {i}")

    # Last 100 lines (no error)
    for i in range(100):
        log_lines.append(f"2025-10-27T03:28:{i:02d}.0000000Z   Cleanup line {i}")

    log_content = "\n".join(log_lines)

    result = _extract_error_context(log_content, max_lines=500)

    # Verify that error part is included
    assert "Critical failure" in result
    assert "critical-test.spec.ts" in result
    assert 'Expected substring: "important data"' in result
    assert 'Received string: "wrong data"' in result

    # Verify that result is within max line count
    result_lines = result.split("\n")
    assert len(result_lines) <= 500

    # Verify that context before and after error is included
    assert "Critical Test" in result


def test_extract_error_context_no_errors():
    """Verify that first part is returned when there is no error"""
    log_content = "\n".join([f"2025-10-27T03:25:{i:02d}.0000000Z   Test line {i}" for i in range(600)])

    result = _extract_error_context(log_content, max_lines=500)

    # Verify it is within max line count
    result_lines = result.split("\n")
    assert len(result_lines) <= 500

    # Verify that first part is included
    assert "Test line 0" in result
    assert "Test line 1" in result


def test_extract_error_context_empty_log():
    """Verify that empty log can be processed"""
    result = _extract_error_context("")
    assert result == ""

    result = _extract_error_context("")
    assert result == ""


def test_extract_error_context_preserves_important_context():
    """Verify that important context of error is preserved"""
    log_content = """
2025-10-27T03:25:50.0000000Z Test setup started
2025-10-27T03:25:51.0000000Z Navigating to page
2025-10-27T03:25:52.0000000Z Page loaded
2025-10-27T03:25:53.0000000Z Clicking button
2025-10-27T03:25:54.0000000Z Waiting for response
2025-10-27T03:25:55.0000000Z 
2025-10-27T03:25:56.0000000Z   1) [core] › e2e/core/button-click.spec.ts:30:5 › Button click test
2025-10-27T03:25:57.0000000Z 
2025-10-27T03:25:58.0000000Z     Error: expect(received).toContain(expected)
2025-10-27T03:25:59.0000000Z 
2025-10-27T03:26:00.0000000Z     Expected substring: "Success message"
2025-10-27T03:26:01.0000000Z     Received string: "Error: Network timeout"
2025-10-27T03:26:02.0000000Z 
2025-10-27T03:26:03.0000000Z     at button-click.spec.ts:30:31
2025-10-27T03:26:04.0000000Z 
2025-10-27T03:26:05.0000000Z Test teardown completed
"""

    result = _extract_error_context(log_content)

    # Verify that error message and context before/after error are included
    assert "Button click test" in result
    assert "Error: expect(received).toContain(expected)" in result
    assert 'Expected substring: "Success message"' in result
    assert 'Received string: "Error: Network timeout"' in result

    # Verify that setup information before error is included (first 10 lines)
    assert "Waiting for response" in result or "Clicking button" in result

    # Verify that information after error is included (last 10 lines)
    assert "button-click.spec.ts:30:31" in result
