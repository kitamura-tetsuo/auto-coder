import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_step(tmp_path, group, test_script, timeout="0.25", grace="0.1"):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/run_pr_test_shard.py", scripts)
    (scripts / "test.sh").write_text(test_script, encoding="utf-8")
    run = next(step["run"] for step in _workflow()["jobs"]["tests-shard"]["steps"] if step["name"].startswith("Run tests with coverage"))
    run = run.replace("${{ matrix.group }}", str(group)).replace("--attempt-timeout 180", f"--attempt-timeout {timeout}").replace("--termination-grace 10", f"--termination-grace {grace}").replace("uv run python", f"{shutil.which('python')} ")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize("group", [1, 2, 3, 4])
def test_production_step_passes_correct_shard_once(tmp_path, group):
    result = _run_step(
        tmp_path,
        group,
        '#!/bin/bash\nprintf "%s\\n" "$*" >> invocations\necho shard-output\n',
    )

    assert result.returncode == 0
    assert (tmp_path / "invocations").read_text().splitlines() == [f"--splits 4 --group {group} -vv -o faulthandler_timeout=30"]
    assert "shard-output" in result.stdout
    assert (tmp_path / f"pr-test-logs/shard-{group}/attempt-1.stdout.log").exists()
    assert not (tmp_path / f"pr-test-logs/shard-{group}/attempt-2.stdout.log").exists()


@pytest.mark.parametrize("status", [1, 2, 5, 124, 137])
def test_completed_nonzero_status_is_not_retried(tmp_path, status):
    result = _run_step(
        tmp_path,
        2,
        f"#!/bin/bash\necho invoked >> invocations\nexit {status}\n",
    )

    assert result.returncode != 0
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    assert "ordinary failure" in result.stderr
    assert "starting retry" not in result.stderr


@pytest.mark.parametrize("first_body", ["sleep 2", "while :; do echo active; done"])
def test_deadline_retries_one_complete_invocation(tmp_path, first_body):
    result = _run_step(
        tmp_path,
        3,
        f"""#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
echo "attempt-$count-out"
echo "attempt-$count-err" >&2
if [ "$count" = 1 ]; then {first_body}; fi
exit 0
""",
    )

    assert result.returncode == 0
    assert (tmp_path / "count").read_text().strip() == "2"
    logs = tmp_path / "pr-test-logs/shard-3"
    assert "attempt-1-out" in (logs / "attempt-1.stdout.log").read_text()
    assert "attempt-1-err" in (logs / "attempt-1.stderr.log").read_text()
    assert "attempt-2-out" in (logs / "attempt-2.stdout.log").read_text()
    assert "starting retry" in result.stderr


def test_timeout_kills_term_resistant_child_before_retry(tmp_path):
    result = _run_step(
        tmp_path,
        1,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
if [ "$count" = 1 ]; then
  bash -c 'trap "" TERM; while :; do echo child-alive >> child-events; sleep .03; done' &
  child=$!; echo "$child" > child-pid
  trap 'exit 0' TERM
  wait
fi
state=$(awk '{print $3}' "/proc/$(cat child-pid)/stat" 2>/dev/null || true)
[ -n "$state" ] && [ "$state" != Z ] && exit 22
echo retry-started >> child-events
""",
    )

    assert result.returncode == 0
    assert "retry-started" in (tmp_path / "child-events").read_text()
    child = int((tmp_path / "child-pid").read_text())
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_two_timeouts_fail_and_keep_both_logs(tmp_path):
    result = _run_step(tmp_path, 4, "#!/bin/bash\necho invoked >> invocations\nsleep 2\n")

    assert result.returncode != 0
    assert len((tmp_path / "invocations").read_text().splitlines()) == 2
    logs = tmp_path / "pr-test-logs/shard-4"
    assert (logs / "attempt-1.supervisor.log").exists()
    assert (logs / "attempt-2.supervisor.log").exists()


def test_retry_removes_partial_coverage(tmp_path):
    result = _run_step(
        tmp_path,
        1,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0); count=$((count + 1)); echo "$count" > count
mkdir -p htmlcov
if [ "$count" = 1 ]; then echo stale > htmlcov/index.html; sleep 2; fi
echo fresh > htmlcov/index.html
""",
    )

    assert result.returncode == 0
    assert (tmp_path / "htmlcov/index.html").read_text().strip() == "fresh"
    partial = tmp_path / "pr-test-logs/shard-1/attempt-1-partial-reports/htmlcov/index.html"
    assert partial.read_text().strip() == "stale"


def test_workflow_contract_and_aggregate_shell():
    workflow = _workflow()
    assert workflow["name"] == "PR Tests"
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) >= {"lint", "tests-shard", "tests"}
    shard = workflow["jobs"]["tests-shard"]
    assert shard["strategy"] == {"fail-fast": False, "matrix": {"group": [1, 2, 3, 4]}}
    test_step = next(step for step in shard["steps"] if step["name"].startswith("Run tests"))
    assert test_step["timeout-minutes"] == 8
    assert "--attempt-timeout 180" in test_step["run"]
    assert "--termination-grace 10" in test_step["run"]
    assert "--max-attempts 2" in test_step["run"]
    assert "scripts/run_pr_test_shard.py" in test_step["run"]
    aggregate = workflow["jobs"]["tests"]
    assert aggregate["if"] == "always()"
    assert '!= "success"' in aggregate["steps"][0]["run"]
