import subprocess
import sys


def test_cli_get_actions_logs_strips_prelude_and_is_compact():
    """Test that CLI output starts with Job header (no logger prelude) and is compact.

    This test verifies that when get-actions-logs command is executed:
    1. Output starts with '=== Job:' (not loguru prelude with timestamps)
    2. Output is compact (less than 1000 lines)
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.auto_coder.cli",
            "get-actions-logs",
            "--url",
            "https://github.com/test/repo/actions/runs/123/job/456",
            "--github-token",
            "dummy",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )

    # The CLI should complete successfully even if API calls fail
    assert result.returncode == 0, f"CLI returned non-zero: {result.returncode}, stderr: {result.stderr}"

    # Check that output starts with Job header (no logger prelude)
    # Loguru prelude would have timestamps like "2026-01-18 09:38:04"
    head = (result.stdout.splitlines() + [""])[:3]
    assert head and head[0].startswith("=== Job:"), f"Expected output to start with '=== Job:', got: {head[0] if head else 'empty'}"

    # Body should be compact due to slicing
    assert len(result.stdout.splitlines()) < 1000, "Output should be compact (less than 1000 lines)"
