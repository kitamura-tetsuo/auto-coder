import subprocess
import sys


def test_cli_get_actions_logs_strips_prelude_and_is_compact():
    url = "https://github.com/kitamura-tetsuo/outliner/actions/runs/17006383413/job/48216559181?pr=502"
    # Run CLI and capture output; pass dummy token to avoid auth prompt
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.auto_coder.cli",
            "get-actions-logs",
            "--url",
            url,
            "--github-token",
            "dummy",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0
    # Standard output should start with Job header (no logger prelude)
    head = (result.stdout.splitlines() + [""])[:3]
    assert head and head[0].startswith("=== Job ")
    # Body should be compact due to slicing
    assert len(result.stdout.splitlines()) < 1000
    # If failure summary lines are present, they should be placed under a Summary block
    if any(x in result.stdout.lower() for x in [" failed", "did not run", "error was not a part of any test", "command failed with exit code", "process completed with exit code"]):
        assert "--- Summary ---" in result.stdout

