import importlib
import os
import subprocess
import sys


def test_cli_get_actions_logs_strips_prelude_and_is_compact(_use_real_home, _use_real_commands):
    # Skip if no valid GitHub token is available
    github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not github_token:
        import pytest

        pytest.skip("GITHUB_TOKEN or GH_TOKEN not set")

    importlib.reload(subprocess)
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/17006383413/job/48216559181?pr=502"
    # Run CLI and capture output; pass dummy token to avoid auth prompt
    # Use a wrapper script that checks PYTHONPATH before running the CLI
    check_script = """
import os
print("PYTHONPATH_CHECK:", os.environ.get('PYTHONPATH', 'NOT_SET'))
import sys
print("Executing:", ' '.join(sys.argv[1:]))
sys.exit(0)
"""
    # First, verify PYTHONPATH is set correctly by running a simple check
    check_result = subprocess.run(
        [sys.executable, "-c", check_script],
        capture_output=True,
        text=True,
    )
    print(f"Check script output: {check_result.stdout}", file=sys.stderr)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.auto_coder.cli",
            "get-actions-logs",
            "--url",
            url,
            "--github-token",
            github_token,
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(f"CLI stderr: {result.stderr}", file=sys.stderr)
    assert result.returncode == 0
    # Standard output should start with Job header (no logger prelude)
    head = (result.stdout.splitlines() + [""])[:3]
    assert head and head[0].startswith("=== Job ")
    # Body should be compact due to slicing
    assert len(result.stdout.splitlines()) < 1000
    # If failure summary lines are present, they should be placed under a Summary block
    if any(
        x in result.stdout.lower()
        for x in [
            " failed",
            "did not run",
            "error was not a part of any test",
            "command failed with exit code",
            "process completed with exit code",
        ]
    ):
        assert "--- Summary ---" in result.stdout
