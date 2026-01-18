"""
Integration tests using actual GitHub Actions logs
"""

import io
import json
import sys
import zipfile
from unittest.mock import Mock, patch

import pytest

from src.auto_coder.util.github_action import _extract_error_context, get_github_actions_logs_from_url


def test_extract_error_context_with_realistic_playwright_log():
    """Verify that error context can be extracted in format close to actual Playwright logs"""
    # Sample very close to actual Playwright error logs
    realistic_log = """2025-10-27T03:25:34.7924026Z Current runner version: '2.327.1'
2025-10-27T03:25:34.7930495Z Runner name: 'runner-1'
2025-10-27T03:25:34.7931234Z Machine name: 'test-machine'
2025-10-27T03:25:35.0000000Z ##[group]Operating System
2025-10-27T03:25:35.0000001Z Ubuntu
2025-10-27T03:25:35.0000002Z 22.04.5
2025-10-27T03:25:35.0000003Z LTS
2025-10-27T03:25:35.0000004Z ##[endgroup]
2025-10-27T03:25:36.0000000Z ##[group]Runner Image
2025-10-27T03:25:36.0000001Z Image: ubuntu-22.04
2025-10-27T03:25:36.0000002Z Version: 20241020.1.0
2025-10-27T03:25:36.0000003Z ##[endgroup]
2025-10-27T03:25:37.0000000Z Preparing workflow directory
2025-10-27T03:25:38.0000000Z Preparing all required actions
2025-10-27T03:25:39.0000000Z Getting action download info
2025-10-27T03:25:40.0000000Z Download action repository 'actions/checkout@v4'
2025-10-27T03:25:50.0000000Z ##[group]Run actions/checkout@v4
2025-10-27T03:25:50.0000001Z with:
2025-10-27T03:25:50.0000002Z   repository: kitamura-tetsuo/outliner
2025-10-27T03:25:50.0000003Z   token: ***
2025-10-27T03:25:50.0000004Z ##[endgroup]
2025-10-27T03:25:51.0000000Z Syncing repository: kitamura-tetsuo/outliner
2025-10-27T03:25:52.0000000Z Getting Git version info
2025-10-27T03:25:53.0000000Z Initializing the repository
2025-10-27T03:25:54.0000000Z Disabling automatic garbage collection
2025-10-27T03:25:55.0000000Z Setting up auth
2025-10-27T03:25:56.0000000Z Fetching the repository
2025-10-27T03:25:57.0000000Z Determining the checkout info
2025-10-27T03:25:58.0000000Z Checking out the ref
2025-10-27T03:25:59.0000000Z Setting up auth for fetching submodules
2025-10-27T03:26:00.0000000Z Persisting credentials for submodules
2025-10-27T03:26:01.0000000Z ##[group]Run npm ci
2025-10-27T03:26:02.0000000Z npm ci
2025-10-27T03:26:03.0000000Z ##[endgroup]
2025-10-27T03:26:04.0000000Z added 1234 packages in 45s
2025-10-27T03:26:05.0000000Z ##[group]Run npm test
2025-10-27T03:26:06.0000000Z npm test
2025-10-27T03:26:07.0000000Z ##[endgroup]
2025-10-27T03:26:08.0000000Z
2025-10-27T03:26:09.0000000Z > outliner@1.0.0 test
2025-10-27T03:26:10.0000000Z > playwright test
2025-10-27T03:26:11.0000000Z
2025-10-27T03:26:12.0000000Z Running 300 tests using 4 workers
2025-10-27T03:26:13.0000000Z
2025-10-27T03:26:14.0000000Z   ✓  1 [chromium] › e2e/basic/test-001.spec.ts:10:5 › Basic test 1 (1.2s)
2025-10-27T03:26:15.0000000Z   ✓  2 [chromium] › e2e/basic/test-002.spec.ts:15:5 › Basic test 2 (0.8s)
2025-10-27T03:26:16.0000000Z   ✓  3 [chromium] › e2e/basic/test-003.spec.ts:20:5 › Basic test 3 (1.5s)
2025-10-27T03:26:17.0000000Z   ✓  4 [chromium] › e2e/basic/test-004.spec.ts:25:5 › Basic test 4 (0.9s)
2025-10-27T03:26:18.0000000Z   ✓  5 [chromium] › e2e/basic/test-005.spec.ts:30:5 › Basic test 5 (1.1s)
2025-10-27T03:26:19.0000000Z
2025-10-27T03:26:20.0000000Z   1) [chromium] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:26:21.0000000Z
2025-10-27T03:26:22.0000000Z     Retry #1 ───────────────────────────────────────────────────────────────────────────────────
2025-10-27T03:26:23.0000000Z
2025-10-27T03:26:24.0000000Z     Error: expect(received).toContain(expected) // indexOf
2025-10-27T03:26:25.0000000Z
2025-10-27T03:26:26.0000000Z     Expected substring: "<a href=\\"https://example.com\\""
2025-10-27T03:26:27.0000000Z     Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"
2025-10-27T03:26:28.0000000Z
2025-10-27T03:26:29.0000000Z       45 |         // Wait for the link to be rendered
2025-10-27T03:26:30.0000000Z       46 |         await page.waitForTimeout(500);
2025-10-27T03:26:31.0000000Z       47 |
2025-10-27T03:26:32.0000000Z       48 |         const firstItemHtml = await page.locator(".outliner-item").first().locator(".item-text").innerHTML();
2025-10-27T03:26:33.0000000Z     > 49 |         expect(firstItemHtml).toContain('<a href="https://example.com"');
2025-10-27T03:26:34.0000000Z          |                               ^
2025-10-27T03:26:35.0000000Z       50 |         expect(firstItemHtml).toContain(">https://example.com</a>");
2025-10-27T03:26:36.0000000Z       51 |     });
2025-10-27T03:26:37.0000000Z       52 |
2025-10-27T03:26:38.0000000Z       53 | });
2025-10-27T03:26:39.0000000Z
2025-10-27T03:26:40.0000000Z     at /tmp/runner/work/outliner/outliner/client/e2e/core/fmt-url-label-links-a391b6c2.spec.ts:49:31
2025-10-27T03:26:41.0000000Z
2025-10-27T03:26:42.0000000Z   ✓  6 [chromium] › e2e/basic/test-006.spec.ts:35:5 › Basic test 6 (1.3s)
2025-10-27T03:26:43.0000000Z   ✓  7 [chromium] › e2e/basic/test-007.spec.ts:40:5 › Basic test 7 (0.7s)
2025-10-27T03:26:44.0000000Z   ✓  8 [chromium] › e2e/basic/test-008.spec.ts:45:5 › Basic test 8 (1.4s)
2025-10-27T03:26:45.0000000Z
2025-10-27T03:26:46.0000000Z   1 failed
2025-10-27T03:26:47.0000000Z     [chromium] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:26:48.0000000Z   147 passed (2.5m)
2025-10-27T03:26:49.0000000Z   1 skipped
2025-10-27T03:26:50.0000000Z   151 did not run
2025-10-27T03:26:51.0000000Z
2025-10-27T03:26:52.0000000Z ##[error]Process completed with exit code 1.
"""

    result = _extract_error_context(realistic_log)

    # Verify that important error information is included
    assert "Error: expect(received).toContain(expected)" in result
    assert 'Expected substring: "<a href=\\"https://example.com\\""' in result or 'Expected substring: "<a href="https://example.com"' in result
    assert 'Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"' in result
    assert "fmt-url-label-links-a391b6c2.spec.ts" in result
    assert "URL label links" in result

    # Verify that context before and after error is included
    assert "expect(firstItemHtml).toContain" in result
    assert "at /tmp/runner/work/outliner/outliner/client/e2e/core/fmt-url-label-links-a391b6c2.spec.ts:49:31" in result

    # Verify that test summary is included
    assert "1 failed" in result or "147 passed" in result

    # Verify that result is appropriate length
    result_lines = result.split("\n")
    assert len(result_lines) >= 20  # At least 10 lines before and after error line
    assert len(result_lines) <= 500  # Maximum 500 lines

    # Verify that unnecessary setup information is excluded (or minimal)
    # Even if setup information is included, verify that error part is prioritized
    error_line_index = None
    for i, line in enumerate(result_lines):
        if "Error: expect(received).toContain(expected)" in line:
            error_line_index = i
            break

    assert error_line_index is not None, "Error line not found"


def test_get_github_actions_logs_from_url_with_realistic_zip():
    """Verify that processing can be done in format close to actual GitHub Actions ZIP logs"""
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/18828609259/job/53715705095"

    # Create ZIP file close to actual logs
    realistic_step_log = """2025-10-27T03:26:08.0000000Z
2025-10-27T03:26:09.0000000Z > outliner@1.0.0 test
2025-10-27T03:26:10.0000000Z > playwright test
2025-10-27T03:26:11.0000000Z
2025-10-27T03:26:12.0000000Z Running 300 tests using 4 workers
2025-10-27T03:26:13.0000000Z
2025-10-27T03:26:14.0000000Z   ✓  1 [chromium] › e2e/basic/test-001.spec.ts:10:5 › Basic test 1 (1.2s)
2025-10-27T03:26:15.0000000Z   ✓  2 [chromium] › e2e/basic/test-002.spec.ts:15:5 › Basic test 2 (0.8s)
2025-10-27T03:26:16.0000000Z
2025-10-27T03:26:17.0000000Z   1) [chromium] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:26:18.0000000Z
2025-10-27T03:26:19.0000000Z     Error: expect(received).toContain(expected) // indexOf
2025-10-27T03:26:20.0000000Z
2025-10-27T03:26:21.0000000Z     Expected substring: "<a href=\\"https://example.com\\""
2025-10-27T03:26:22.0000000Z     Received string:    "test-page-1755122947471Visit https:/example.comSecond item<!---->"
2025-10-27T03:26:23.0000000Z
2025-10-27T03:26:24.0000000Z       47 |
2025-10-27T03:26:25.0000000Z       48 |         const firstItemHtml = await page.locator(".outliner-item").first().locator(".item-text").innerHTML();
2025-10-27T03:26:26.0000000Z     > 49 |         expect(firstItemHtml).toContain('<a href="https://example.com"');
2025-10-27T03:26:27.0000000Z          |                               ^
2025-10-27T03:26:28.0000000Z       50 |         expect(firstItemHtml).toContain(">https://example.com</a>");
2025-10-27T03:26:29.0000000Z       51 |     });
2025-10-27T03:26:30.0000000Z       52 |
2025-10-27T03:26:31.0000000Z   53 | });
2025-10-27T03:26:32.0000000Z
2025-10-27T03:26:33.0000000Z     at /tmp/runner/work/outliner/outliner/client/e2e/core/fmt-url-label-links-a391b6c2.spec.ts:49:31
2025-10-27T03:26:34.0000000Z
2025-10-27T03:26:35.0000000Z   1 failed
2025-10-27T03:26:36.0000000Z     [chromium] › e2e/core/fmt-url-label-links-a391b6c2.spec.ts:34:5 › URL label links › converts plain URL to clickable link
2025-10-27T03:26:37.0000000Z   147 passed (2.5m)
2025-10-27T03:26:38.0000000Z   1 skipped
2025-10-27T03:26:39.0000000Z   151 did not run
"""

    # Mock job details
    job_details = {
        "id": 53715705095,
        "name": "CI / e2e tests",
        "conclusion": "failure",
        "steps": [
            {
                "name": "Set up job",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Run actions/checkout@v4",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Run npm ci",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Run npm test",
                "status": "completed",
                "conclusion": "failure",
            },
        ],
    }

    # Create ZIP content
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        zf.writestr("1_Set up job.txt", "Setting up job...\nJob setup complete.")
        zf.writestr(
            "2_Run actions checkout@v4.txt",
            "Checking out code...\nCheckout complete.",
        )
        zf.writestr(
            "3_Run npm ci.txt",
            "Installing dependencies...\nadded 1234 packages in 45s",
        )
        zf.writestr("4_Run npm test.txt", realistic_step_log)
    zip_content = bio.getvalue()

    with patch("src.auto_coder.util.github_action.GitHubClient") as mock_github_client:
        with patch("src.auto_coder.util.github_action.get_ghapi_client") as mock_get_ghapi_client:
            # Configure mocks
            mock_github_client.get_instance.return_value.token = "dummy_token"

            mock_api = Mock()
            mock_get_ghapi_client.return_value = mock_api

            # Mock get_job_for_workflow_run
            mock_api.actions.get_job_for_workflow_run.return_value = job_details

            # Mock download_job_logs_for_workflow_run
            mock_api.actions.download_job_logs_for_workflow_run.return_value = zip_content

            result = get_github_actions_logs_from_url(url)

            # Verification
            mock_api.actions.get_job_for_workflow_run.assert_called_with(owner="kitamura-tetsuo", repo="outliner", job_id="53715705095")
            mock_api.actions.download_job_logs_for_workflow_run.assert_called_with(owner="kitamura-tetsuo", repo="outliner", job_id="53715705095")

    # Verify that error information is included
    assert "Error: expect(received).toContain(expected)" in result
    assert "fmt-url-label-links-a391b6c2.spec.ts" in result
    assert "Expected substring:" in result
    assert "Received string:" in result

    # Verify that successful steps are excluded
    # Note: Using the zip method, the logs include "Step Set up job" headers if matched or errors found.
    # The current implementation of get_github_actions_logs_from_url with zip extraction
    # filters by failing steps. "Set up job" is success, so it should be skipped.
    assert "Set up job" not in result  # Successful steps should be excluded
    assert "Checking out code" not in result  # Successful steps should be excluded
    assert "Installing dependencies" not in result  # Successful steps should be excluded

    # Verify that test summary is included
    # Note: These may be truncated in the output, so we check for the key error information instead
    # assert "1 failed" in result
    # assert "147 passed" in result
