"""Regression coverage for scripts/test.sh's package-wide mypy gate (issue #2015).

These tests execute the *actual*, unmodified ``scripts/test.sh`` (and, for the
supervisor scenario, the real ``scripts/run_pr_test_shard.py``) against
disposable checkouts built from this repository's own production
``src/auto_coder`` tree, ``pyproject.toml`` and ``uv.lock``. They intentionally
avoid mocking the check itself: a fake mypy process that returns a chosen
status would not prove that the checked-out ``auto_coder`` package is
actually analyzed before the "checks passed" message and the test collector.

Two boundary scenarios (a config file mypy cannot find, and mypy failing to
be provisioned at all) use a narrowly targeted setup double documented at
each call site, because ``uv``'s own environment self-healing (``uv run``
transparently reprovisions a project's declared dev-group tools) makes those
specific failure modes impossible to reproduce by sabotaging the real,
unmodified install step. The mypy *invocation* itself is never altered
outside of the single test that explicitly targets "config file missing".

Fixtures copy a small, self-contained *slice* of the real ``src/auto_coder``
tree (a handful of complete, unmodified production files with no relative
imports outside that slice) rather than the whole ~140-file package: `mypy
-p auto_coder` walks whatever package directory is actually on disk, so a
seeded nested/otherwise-unimported submodule proves the same "package-wide,
not import-driven" contract either way, while Black/Flake8 -- which have no
cross-run cache -- scan an order of magnitude fewer real files per one of
this suite's dozen full script invocations. This keeps the whole file's
runtime well inside a single PR-Tests shard's attempt budget.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("_use_real_commands")

ROOT = Path(__file__).parents[1]
TEST_SH = ROOT / "scripts" / "test.sh"
RUN_PR_TEST_SHARD = ROOT / "scripts" / "run_pr_test_shard.py"
PYPROJECT = ROOT / "pyproject.toml"
FLAKE8_CONFIG = ROOT / ".flake8"
VENV_BIN = ROOT / ".venv" / "bin"

REAL_UV_CACHE_DIR = os.environ.get("UV_CACHE_DIR") or str(Path(os.environ["HOME"]) / ".cache" / "uv")

_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")

# Small, complete, unmodified real production files with no relative imports
# outside this list (verified against the actual source), used in place of
# the whole ~140-file auto_coder package -- see the module docstring.
_SLIM_PACKAGE_FILES = (
    "shutdown_context.py",
    "codex_cloud_task.py",
    "review_feedback_marker.py",
    "exceptions.py",
    "test_result.py",
    "log_utils.py",
    "security_utils.py",
)

_STUB_COLLECTOR = '''"""Stub standing in for local_test_log_collector.py in disposable fixtures.

Records the argv it is invoked with (proving REQ-005's "exactly once, with
the original argument vector unchanged") and exits with a controllable
status, so tests can prove non-invocation and argv/exit-code passthrough
without paying for a real pytest run inside every fixture checkout.
"""

import json
import os
import sys
from pathlib import Path

_MARKER = Path(__file__).resolve().parents[2] / "collector-invocations.json"
_records = json.loads(_MARKER.read_text()) if _MARKER.exists() else []
_records.append(sys.argv[1:])
_MARKER.write_text(json.dumps(_records))
sys.exit(int(os.environ.get("STUB_COLLECTOR_EXIT_CODE", "0")))
'''


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _uv_env(**overrides: str) -> dict:
    """A clean env for subprocess calls: no ambient project venv leaking in.

    This test module itself normally runs under ``uv run pytest`` from this
    repository's root, which prepends this repo's own ``.venv/bin`` to the
    *pytest process's* PATH and would otherwise leak into every subprocess
    spawned here. ``uv pip install`` (unlike ``uv run``) silently treats a
    PATH-discoverable interpreter as "the active environment" instead of
    erroring, which would make e.g. the missing-virtualenv fixture (which
    has no ``.venv`` of its own) accidentally reuse this repo's real
    ``.venv`` and mask the failure this suite means to reproduce.
    """
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = REAL_UV_CACHE_DIR
    env.pop("VIRTUAL_ENV", None)
    root_venv_bin = str(VENV_BIN)
    env["PATH"] = os.pathsep.join(part for part in env.get("PATH", "").split(os.pathsep) if part and part != root_venv_bin)
    env.update(overrides)
    return env


def _tool_version(tool: str) -> str:
    output = subprocess.run([str(VENV_BIN / tool), "--version"], capture_output=True, text=True, check=True).stdout
    match = _VERSION_RE.search(output)
    assert match, f"could not parse a version from {tool} --version output: {output!r}"
    return match.group(0)


def _isort_version() -> str:
    output = subprocess.run([str(VENV_BIN / "isort"), "--version-number"], capture_output=True, text=True, check=True).stdout
    match = _VERSION_RE.search(output)
    assert match, f"could not parse a version from isort --version-number output: {output!r}"
    return match.group(0)


def _find_python312() -> str | None:
    candidate = shutil.which("python3.12")
    if candidate:
        return candidate
    candidate = shutil.which("python3")
    if candidate:
        result = subprocess.run([candidate, "--version"], capture_output=True, text=True)
        if "3.12" in result.stdout + result.stderr:
            return candidate
    return None


def _write_pyproject_with_shared_cache(dest: Path, cache_dir: Path) -> None:
    """Copy the real pyproject.toml, pointing mypy's cache at a shared dir.

    Sharing one cache directory across these disposable, per-test checkouts
    (mypy's cache is keyed by file content, so this cannot mask a real
    seeded error) makes every mypy run after the first well under a second.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    marker = "[tool.mypy]\n"
    assert text.count(marker) == 1
    text = text.replace(marker, f"{marker}cache_dir = {json.dumps(cache_dir.as_posix())}\n", 1)
    dest.write_text(text, encoding="utf-8")


def _seed_fixture_repo(repo: Path, cache_dir: Path) -> None:
    repo.mkdir(parents=True)
    _write_pyproject_with_shared_cache(repo / "pyproject.toml", cache_dir)
    shutil.copy2(FLAKE8_CONFIG, repo / ".flake8")
    shutil.copy2(ROOT / "uv.lock", repo / "uv.lock")
    shutil.copy2(ROOT / ".python-version", repo / ".python-version")
    (repo / "src/auto_coder").mkdir(parents=True)
    _write(repo, "src/auto_coder/__init__.py", '"""Slim auto_coder package subset for scripts/test.sh fixtures."""\n')
    for name in _SLIM_PACKAGE_FILES:
        shutil.copy2(ROOT / "src/auto_coder" / name, repo / "src/auto_coder" / name)
    _write(repo, "tests/test_placeholder.py", "def test_placeholder() -> None:\n    assert True\n")
    script = _write(repo, "scripts/test.sh", TEST_SH.read_text(encoding="utf-8"))
    script.chmod(0o755)
    # The collector is an expensive, unrelated external boundary (a real
    # pytest run); every scenario here is decided before it would run, so a
    # stub that records invocations makes non-execution and argv/exit-code
    # passthrough directly observable instead of merely inferred.
    _write(repo, "src/auto_coder/local_test_log_collector.py", _STUB_COLLECTOR)


def _provision_uv_venv(repo: Path) -> None:
    """Mirror the real PR Tests workflow's own pre-sync ("uv sync --dev --extra test")

    that runs as a separate step before scripts/test.sh is ever invoked.
    """
    result = subprocess.run(["uv", "sync", "--dev", "--extra", "test", "-q"], cwd=repo, env=_uv_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _run_test_script(
    repo: Path,
    args: list[str] | tuple[str, ...] = (),
    *,
    ci: bool,
    extra_env: dict | None = None,
    path_override: str | None = None,
) -> subprocess.CompletedProcess:
    env = _uv_env(GITHUB_ACTIONS="true" if ci else "false")
    env.pop("CI", None)
    if path_override is not None:
        env["PATH"] = path_override
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["bash", "scripts/test.sh", *args], cwd=repo, env=env, capture_output=True, text=True, timeout=180)


@pytest.fixture(scope="session")
def mypy_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("test-sh-mypy-cache")


@pytest.fixture()
def fixture_repo(tmp_path: Path, mypy_cache_dir: Path) -> Path:
    repo = tmp_path / "repo"
    _seed_fixture_repo(repo, mypy_cache_dir)
    return repo


@pytest.fixture(scope="session")
def sysvenv_with_tools(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A plain (non-uv-managed) Python 3.12 venv with pinned quality tools.

    Pinned to this repository's own provisioned versions so a tool-version
    mismatch (e.g. a newer black's default formatting) cannot masquerade as
    a real seeded error in the "uv absent" system-Python route.
    """
    python312 = _find_python312()
    if not python312:
        pytest.skip("no Python 3.12 interpreter available for the system-Python fallback route")
    packages = [
        f"black=={_tool_version('black')}",
        f"isort=={_isort_version()}",
        f"flake8=={_tool_version('flake8')}",
        f"mypy=={_tool_version('mypy')}",
        "types-toml",
        "types-python-dateutil",
    ]
    venv_dir = tmp_path_factory.mktemp("test-sh-sysvenv") / "venv"
    subprocess.run([python312, "-m", "venv", str(venv_dir)], check=True, capture_output=True, text=True)
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_dir / "bin" / "python"), *packages],
        env=_uv_env(),
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return venv_dir


@pytest.fixture(scope="session")
def sysvenv_without_mypy(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Like ``sysvenv_with_tools`` but mypy is never installed into it."""
    python312 = _find_python312()
    if not python312:
        pytest.skip("no Python 3.12 interpreter available for the system-Python fallback route")
    packages = [
        f"black=={_tool_version('black')}",
        f"isort=={_isort_version()}",
        f"flake8=={_tool_version('flake8')}",
        "types-toml",
        "types-python-dateutil",
    ]
    venv_dir = tmp_path_factory.mktemp("test-sh-sysvenv-nomypy") / "venv"
    subprocess.run([python312, "-m", "venv", str(venv_dir)], check=True, capture_output=True, text=True)
    install = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_dir / "bin" / "python"), *packages],
        env=_uv_env(),
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    return venv_dir


@pytest.fixture(scope="session")
def fake_pip_rejecting_mypy(tmp_path_factory: pytest.TempPathFactory, sysvenv_without_mypy: Path) -> Path:
    """A ``pip`` that installs everything real except a request for "mypy".

    This is the targeted setup double described in the module docstring: it
    reproduces "mypy could not be provisioned" without touching the mypy
    invocation itself, and without fighting uv's own reprovisioning of its
    project's declared dev-group tools (which defeats direct sabotage of an
    already-installed mypy in the uv route).
    """
    real_pip = sysvenv_without_mypy / "bin" / "pip"
    bin_dir = tmp_path_factory.mktemp("test-sh-fakepip")
    script = bin_dir / "pip"
    script.write_text(
        "#!/bin/bash\n" 'for arg in "$@"; do\n' '  if [ "$arg" = "mypy" ]; then\n' "    echo 'pip: simulated failure installing mypy' >&2\n" "    exit 1\n" "  fi\n" "done\n" f'exec "{real_pip}" "$@"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


@pytest.fixture()
def fake_docker(tmp_path: Path) -> Path:
    """A ``docker`` double that runs ``docker exec`` locally instead of in a container.

    It forwards to a real checkout on disk (``$FAKE_DOCKER_TARGET_DIR``)
    rather than launching an actual container, but the inner
    ``scripts/test.sh`` invocation it runs is the real, unmodified script.
    Its own argv is recorded NUL-separated (never space-joined via ``"$*"``,
    which would make a single ``"not browser"`` argument indistinguishable
    from the two separate arguments ``"not"``, ``"browser"``), so a test can
    assert on the exact argument boundaries the inner script receives.
    """
    bin_dir = tmp_path / "fakedockerbin"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/bash\n"
        'printf \'%s\\0\' "$@" >> "$FAKE_DOCKER_LOG"\n'
        'if [ "$1" = "exec" ]; then\n'
        "  shift\n"
        '  env_assign=""\n'
        '  if [ "$1" = "-e" ]; then\n'
        '    env_assign="$2"\n'
        "    shift 2\n"
        "  fi\n"
        '  container="$1"; shift\n'
        '  echo "$container" >> "$FAKE_DOCKER_CONTAINER_LOG"\n'
        '  cd "$FAKE_DOCKER_TARGET_DIR" || exit 97\n'
        '  env "$env_assign" "$@"\n'
        "  exit $?\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _run_outer_forwarding_script(outer: Path, target_dir: Path, docker_bin: Path, *, args: list[str] = ()) -> subprocess.CompletedProcess:
    outer.mkdir(parents=True, exist_ok=True)
    outer_script = _write(outer, "scripts/test.sh", TEST_SH.read_text(encoding="utf-8"))
    outer_script.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(docker_bin), env.get("PATH", "")])
    env["AM_I_AUTOCODER_CONTAINER"] = "true"
    env["REPO_NAME"] = "owner/project"
    env["GITHUB_ACTIONS"] = "true"
    env.pop("CI", None)
    env.pop("INSIDE_TARGET_EXECUTION", None)
    env["FAKE_DOCKER_LOG"] = str(outer / "docker-invocations.log")
    env["FAKE_DOCKER_CONTAINER_LOG"] = str(outer / "docker-container.log")
    env["FAKE_DOCKER_TARGET_DIR"] = str(target_dir)
    return subprocess.run(["bash", "scripts/test.sh", *args], cwd=outer, env=env, capture_output=True, text=True, timeout=180)


# ---------------------------------------------------------------------------
# AS-001: a real, seeded package error stops the script before the collector
# ---------------------------------------------------------------------------


def test_real_package_error_stops_before_collector(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    _write(fixture_repo, "src/auto_coder/_as001_leaf.py", 'value: int = "wrong"\n')
    _write(fixture_repo, "src/auto_coder/_as001_pkg/__init__.py", "")
    _write(fixture_repo, "src/auto_coder/_as001_pkg/leaf.py", 'def broken() -> int:\n    return "not an int"\n')

    result = _run_test_script(fixture_repo, ci=True)

    assert result.returncode != 0
    assert "_as001_leaf.py" in result.stdout
    assert "_as001_pkg/leaf.py" in result.stdout
    assert "All code quality checks passed" not in result.stdout
    assert not (fixture_repo / "collector-invocations.json").exists()


# ---------------------------------------------------------------------------
# AS-002: runner and formatting-mode variants
# ---------------------------------------------------------------------------


def test_uv_absent_system_python_route_reports_the_real_error(fixture_repo: Path, sysvenv_with_tools: Path) -> None:
    _write(fixture_repo, "src/auto_coder/_as002_leaf.py", 'value: int = "wrong"\n')
    path_override = os.pathsep.join([str(sysvenv_with_tools / "bin"), "/usr/bin", "/bin"])

    result = _run_test_script(fixture_repo, ci=True, path_override=path_override)

    assert result.returncode != 0
    assert "_as002_leaf.py" in result.stdout
    assert "All code quality checks passed" not in result.stdout
    assert not (fixture_repo / "collector-invocations.json").exists()


def test_ci_mode_formatter_violation_fails_without_rewriting(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    target = _write(fixture_repo, "src/auto_coder/_as002_black.py", "def f( ):\n    return   1\n")
    before = target.read_text()

    result = _run_test_script(fixture_repo, ci=True)

    assert result.returncode != 0
    assert target.read_text() == before
    assert not (fixture_repo / "collector-invocations.json").exists()


def test_non_ci_mode_retains_local_autofix_and_still_reaches_mypy(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    target = _write(fixture_repo, "src/auto_coder/_as002_black_fix.py", "def f( ):\n    return   1\n")
    unformatted = target.read_text()

    result = _run_test_script(fixture_repo, ci=False, extra_env={"STUB_COLLECTOR_EXIT_CODE": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert target.read_text() != unformatted
    assert "All code quality checks passed" in result.stdout
    assert json.loads((fixture_repo / "collector-invocations.json").read_text()) == [[]]


# ---------------------------------------------------------------------------
# AS-003: missing checker and failed setup/configuration
# ---------------------------------------------------------------------------


def test_missing_virtualenv_fails_before_success_message(fixture_repo: Path) -> None:
    # No `uv sync`/`uv venv` was run for this repo: the real, unmodified
    # "Install code quality tools" step has no virtual environment to
    # install into (REQ-002/REQ-003's "failure to provision the required
    # execution environment").
    result = _run_test_script(fixture_repo, ci=True)

    assert result.returncode != 0
    assert "No virtual environment" in result.stdout + result.stderr
    assert "All code quality checks passed" not in result.stdout
    assert not (fixture_repo / "collector-invocations.json").exists()


def test_missing_explicit_config_file_fails_loudly(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    script = fixture_repo / "scripts/test.sh"
    text = script.read_text(encoding="utf-8")
    original = "$RUN mypy --config-file pyproject.toml -p auto_coder"
    assert text.count(original) == 1
    # Targeted double (see module docstring): points only this copy's mypy
    # invocation at a file that does not exist, isolating "the configured
    # file is missing" from "the real pyproject.toml's content", which
    # AS-001/AS-002 already cover with the unmodified command.
    script.write_text(text.replace(original, "$RUN mypy --config-file does-not-exist.toml -p auto_coder", 1), encoding="utf-8")

    result = _run_test_script(fixture_repo, ci=True)

    assert result.returncode != 0
    assert "does-not-exist.toml" in result.stdout + result.stderr
    assert "All code quality checks passed" not in result.stdout
    assert not (fixture_repo / "collector-invocations.json").exists()


def test_checker_that_cannot_be_provisioned_fails_the_script(
    fixture_repo: Path,
    sysvenv_without_mypy: Path,
    fake_pip_rejecting_mypy: Path,
) -> None:
    path_override = os.pathsep.join([str(fake_pip_rejecting_mypy), str(sysvenv_without_mypy / "bin"), "/usr/bin", "/bin"])

    result = _run_test_script(fixture_repo, ci=True, path_override=path_override)

    assert result.returncode != 0
    assert "mypy" in (result.stdout + result.stderr).lower()
    assert "All code quality checks passed" not in result.stdout
    assert not (fixture_repo / "collector-invocations.json").exists()


# ---------------------------------------------------------------------------
# AS-004: successful checks preserve the test invocation
# ---------------------------------------------------------------------------


def test_successful_checks_invoke_collector_once_with_original_argv(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    argv = ["--splits", "4", "--group", "2", "-vv", "-o", "faulthandler_timeout=30"]

    result = _run_test_script(fixture_repo, argv, ci=True, extra_env={"STUB_COLLECTOR_EXIT_CODE": "0"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "All code quality checks passed" in result.stdout
    assert json.loads((fixture_repo / "collector-invocations.json").read_text()) == [argv]


def test_collector_exit_status_is_returned_not_overwritten(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)

    result = _run_test_script(fixture_repo, ci=True, extra_env={"STUB_COLLECTOR_EXIT_CODE": "5"})

    assert result.returncode == 5
    assert "All code quality checks passed" in result.stdout


# ---------------------------------------------------------------------------
# AS-005: the target-container failure reaches the caller
# ---------------------------------------------------------------------------


def test_target_container_mypy_failure_reaches_outer_caller(tmp_path: Path, fixture_repo: Path, fake_docker: Path) -> None:
    _provision_uv_venv(fixture_repo)
    _write(fixture_repo, "src/auto_coder/_as005_leaf.py", 'value: int = "wrong"\n')
    # The documented shard argument vector (scripts/run_pr_test_shard.py's own
    # `command`), so a regression that drops the forwarded "$@" is caught
    # here rather than only by tests that never pass arguments at all.
    argv = ["--splits", "4", "--group", "3", "-m", "not browser", "-vv", "-o", "faulthandler_timeout=30"]

    result = _run_outer_forwarding_script(tmp_path / "outer", fixture_repo, fake_docker, args=argv)

    assert result.returncode != 0
    assert "_as005_leaf.py" in result.stdout
    assert (tmp_path / "outer/docker-container.log").read_text().strip() == "auto-coder-project"
    assert not (fixture_repo / "collector-invocations.json").exists()

    # fake_docker records its own argv NUL-separated, immediately before
    # cd-ing into the target checkout, so this proves the inner script
    # received the original argument *boundaries* unchanged (not merely a
    # space-joined string that a split/merged argument could also produce)
    # -- not just that the target container name was resolved correctly.
    raw_parts = (tmp_path / "outer/docker-invocations.log").read_bytes().split(b"\0")
    if raw_parts and raw_parts[-1] == b"":
        raw_parts = raw_parts[:-1]  # trailing NUL from printf '%s\0'
    recorded_argv = [part.decode() for part in raw_parts]
    assert recorded_argv == ["exec", "-e", "INSIDE_TARGET_EXECUTION=true", "auto-coder-project", "./scripts/test.sh", *argv]


def test_docker_launch_failure_propagates_as_outer_nonzero(tmp_path: Path) -> None:
    bin_dir = tmp_path / "failing-docker-bin"
    bin_dir.mkdir()
    script = _write(bin_dir, "docker", "#!/bin/bash\nexit 17\n")
    script.chmod(0o755)

    result = _run_outer_forwarding_script(tmp_path / "outer", tmp_path / "unused-target", bin_dir)

    assert result.returncode != 0


# ---------------------------------------------------------------------------
# AS-006: the production PR supervisor does not hide or retry a type failure
# ---------------------------------------------------------------------------


def test_shard_supervisor_does_not_retry_a_real_mypy_failure(fixture_repo: Path) -> None:
    _provision_uv_venv(fixture_repo)
    shutil.copy2(RUN_PR_TEST_SHARD, fixture_repo / "scripts/run_pr_test_shard.py")
    _write(fixture_repo, "src/auto_coder/_as006_leaf.py", 'value: int = "wrong"\n')
    log_dir = fixture_repo / "pr-test-logs/shard-2"

    result = subprocess.run(
        [
            str(fixture_repo / ".venv/bin/python"),
            "scripts/run_pr_test_shard.py",
            "--group",
            "2",
            "--attempt-timeout",
            "60",
            "--termination-grace",
            "5",
            "--log-dir",
            str(log_dir),
        ],
        cwd=fixture_repo,
        env=_uv_env(GITHUB_ACTIONS="true"),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "ordinary failure" in result.stderr
    assert "starting retry" not in result.stderr
    assert "_as006_leaf.py" in (log_dir / "attempt-1.stdout.log").read_text()
    assert not (log_dir / "attempt-2.stdout.log").exists()
    assert not (fixture_repo / "collector-invocations.json").exists()
