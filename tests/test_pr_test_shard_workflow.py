import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _prepare_step(tmp_path, group, test_script, timeout="0.25", grace="0.1"):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/run_pr_test_shard.py", scripts)
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    (scripts / "test.sh").write_text(test_script, encoding="utf-8")
    run = next(step["run"] for step in _workflow()["jobs"]["tests-shard"]["steps"] if step["name"].startswith("Run tests with coverage"))
    run = re.sub(r"--attempt-timeout\s+\S+", f"--attempt-timeout {timeout}", run)
    run = re.sub(r"--termination-grace\s+\S+", f"--termination-grace {grace}", run)
    return run.replace("${{ matrix.group }}", str(group)).replace("uv run python", f"{shutil.which('python')} ")


def _run_step(tmp_path, group, test_script, timeout="0.25", grace="0.1"):
    run = _prepare_step(tmp_path, group, test_script, timeout, grace)
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
    assert (tmp_path / "invocations").read_text().splitlines() == [
        f"--splits 4 --group {group} --splitting-algorithm duration_based_chunks --durations-path pr-test-logs/shard-{group}/normalized-durations.json -m not browser and not opencode_live and not muse_live and not supervisor_integration -vv -o faulthandler_timeout=30"
    ]
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


def test_changed_duration_input_cannot_be_accepted_or_retried(tmp_path):
    result = _run_step(tmp_path, 1, '#!/bin/bash\nprintf "{}" > "$8"\nexit 0\n')
    assert result.returncode != 0
    assert "Duration input changed during attempt 1" in result.stderr
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


def test_cancellation_kills_owned_term_resistant_group_without_retry(tmp_path):
    run = _prepare_step(
        tmp_path,
        1,
        """#!/bin/bash
        echo invoked >> invocations
        echo $$ > child-pid
        echo child-started > child-started
        trap '' TERM
        while :; do echo retained-diagnostic; sleep .03; done
        """,
        timeout="10",
        grace="0.1",
    )
    process = subprocess.Popen(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run],
        cwd=tmp_path,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 3
    while not (tmp_path / "child-started").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (tmp_path / "child-started").exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode != 0
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    logs = tmp_path / "pr-test-logs/shard-1"
    assert "retained-diagnostic" in (logs / "attempt-1.stdout.log").read_text()
    supervisor = (logs / "attempt-1.supervisor.log").read_text()
    assert "cancelled by signal" in supervisor
    assert "Escalating managed process group" in supervisor
    assert "starting retry" not in stderr
    child = int((tmp_path / "child-pid").read_text())
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
    assert "retained-diagnostic" in stdout


def test_unconfirmed_cancellation_cleanup_is_non_success_without_retry(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "test.sh").write_text("#!/bin/bash\necho invoked >> invocations\nsleep .1\n", encoding="utf-8")
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("cancel_cleanup_runner", ROOT / "scripts/run_pr_test_shard.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_stop_group", lambda *_args: False)

    def request_cancel_after_start():
        deadline = time.monotonic() + 3
        while not (tmp_path / "invocations").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        runner.cancel_signal = signal.SIGTERM

    cancellation = threading.Thread(target=request_cancel_after_start)
    cancellation.start()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pr_test_shard.py",
            "--group",
            "1",
            "--attempt-timeout",
            "10",
            "--termination-grace",
            ".1",
            "--max-attempts",
            "2",
            "--log-dir",
            "pr-test-logs/shard-1",
        ],
    )

    assert runner.main() != 0
    cancellation.join(timeout=1)
    assert not cancellation.is_alive()
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    supervisor = (tmp_path / "pr-test-logs/shard-1/attempt-1.supervisor.log").read_text()
    assert "cancelled by signal" in supervisor
    assert not (tmp_path / "pr-test-logs/shard-1/attempt-2.stdout.log").exists()


@pytest.mark.parametrize("completion_signal", [None, signal.SIGINT, signal.SIGTERM])
def test_cancellation_after_successful_child_exit_is_non_success(tmp_path, monkeypatch, completion_signal):
    """Exercise the real successful attempt before delivering the boundary signal."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "test.sh").write_text("#!/bin/bash\necho invoked >> invocations\necho retained-success-output\n", encoding="utf-8")
    data = tmp_path / "tests/data"
    data.mkdir(parents=True)
    (data / "pr-test-durations.json").write_text('{"fixture": 1.0}', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("completion_boundary_runner", ROOT / "scripts/run_pr_test_shard.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    original_attempt = runner._run_attempt
    outcomes = []

    def deliver_after_success(*args):
        outcome = original_attempt(*args)
        assert outcome == "success"
        outcomes.append(outcome)
        if completion_signal is not None:
            signal.raise_signal(completion_signal)
            assert runner.cancel_signal == completion_signal
        return outcome

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "_run_attempt", deliver_after_success)
    monkeypatch.setattr(sys, "argv", ["run_pr_test_shard.py", "--group", "1", "--attempt-timeout", "10", "--max-attempts", "2", "--log-dir", "pr-test-logs/shard-1"])
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = runner.main()
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        runner.logger.remove()

    assert outcomes == ["success"]
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    logs = tmp_path / "pr-test-logs/shard-1"
    assert "retained-success-output" in (logs / "attempt-1.stdout.log").read_text()
    assert not (logs / "attempt-2.stdout.log").exists()
    diagnostic = (logs / "supervisor.log").read_text()
    assert "starting retry" not in diagnostic
    if completion_signal is None:
        assert result == 0
    else:
        assert result != 0
        assert "Cancellation observed after attempt 1" in diagnostic


def test_two_timeouts_fail_and_keep_both_logs(tmp_path):
    result = _run_step(tmp_path, 4, "#!/bin/bash\necho invoked >> invocations\nsleep 2\n")

    assert result.returncode != 0
    assert len((tmp_path / "invocations").read_text().splitlines()) == 2
    logs = tmp_path / "pr-test-logs/shard-4"
    assert (logs / "attempt-1.supervisor.log").exists()
    assert (logs / "attempt-2.supervisor.log").exists()


def test_completed_pytest_progress_permits_one_bounded_wall_extension(tmp_path):
    result = _run_step(
        tmp_path,
        3,
        "#!/bin/bash\necho invoked >> invocations\nsleep .10\necho 'tests/example.py::test_one PASSED [ 10%]'\nsleep .20\n",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "invocations").read_text().splitlines() == ["invoked"]
    assert "extending bounded deadline" in result.stderr


def test_unrelated_output_cannot_extend_stalled_test_progress(tmp_path):
    result = _run_step(
        tmp_path,
        3,
        """#!/bin/bash
count=$(cat count 2>/dev/null || echo 0)
count=$((count + 1)); echo "$count" > count
if [ "$count" = 1 ]; then
  echo 'tests/example.py::test_one PASSED [ 10%]'
  while :; do echo 'unrelated process output'; sleep .02; done
fi
echo retry-succeeded
""",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "count").read_text().strip() == "2"
    assert "without a completed pytest test" in result.stderr
    assert "retry-succeeded" in result.stdout


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
    assert test_step["timeout-minutes"] == 24
    assert "--attempt-timeout 480" in test_step["run"]
    assert "--termination-grace 10" in test_step["run"]
    assert "--max-attempts 2" in test_step["run"]
    assert "scripts/run_pr_test_shard.py" in test_step["run"]
    aggregate = workflow["jobs"]["tests"]
    assert aggregate["needs"] == "tests-shard"
    assert aggregate["if"] == "always()"
    assert '!= "success"' in aggregate["steps"][0]["run"]
