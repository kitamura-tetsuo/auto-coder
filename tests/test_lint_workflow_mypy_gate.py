"""Regression coverage for the PR Tests "Lint & Type Check" static-analysis gate.

These tests execute the *actual* production commands parsed out of
``.github/workflows/pr-tests.yml`` and ``.pre-commit-config.yaml`` against real,
disposable source fixtures using the real ``black``/``isort``/``flake8``/``mypy``
binaries already provisioned in this repository's own virtual environment. They
intentionally avoid mocks: a fake process that merely returns a chosen exit
status would not prove that the checked-out ``auto_coder`` package is actually
analyzed with the intended Python-version semantics and configuration.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.test_repository_hygiene_cli import ALLOWLIST, git, write

pytestmark = pytest.mark.usefixtures("_use_real_commands")

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/pr-tests.yml"
PRE_COMMIT_CONFIG = ROOT / ".pre-commit-config.yaml"
PYPROJECT = ROOT / "pyproject.toml"
FLAKE8_CONFIG = ROOT / ".flake8"
VENV_BIN = ROOT / ".venv" / "bin"

# uv resolves/links packages out of this cache; pointing every subprocess at the
# real, already-warm cache (rather than each test's throwaway $HOME) keeps a
# from-scratch "uv sync" fast without weakening what is actually being proven --
# that the declared extra provisions the tools, not that a network fetch occurs.
REAL_UV_CACHE_DIR = os.environ.get("UV_CACHE_DIR") or str(Path(os.environ["HOME"]) / ".cache" / "uv")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _lint_job() -> dict:
    return _workflow()["jobs"]["lint"]


def _lint_step(name: str) -> dict:
    return next(step for step in _lint_job()["steps"] if step["name"] == name)


def _pre_commit_hook(hook_id: str) -> dict:
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    return next(hook for repository in config["repos"] for hook in repository["hooks"] if hook["id"] == hook_id)


def _uv_env() -> dict:
    env = os.environ.copy()
    env["UV_CACHE_DIR"] = REAL_UV_CACHE_DIR
    return env


def _venv_argv(run: str) -> list[str]:
    """Translate a workflow/hook ``uv run <tool> ...`` command into a direct call.

    Uses the real binaries this repository's own ``.venv`` (provisioned via the
    same ``[project.optional-dependencies].dev`` extra the workflow installs)
    so fixture-driven tests do not pay for a full "uv run" resolution per call.
    The argument list -- the thing the requirements actually constrain -- is
    left untouched.
    """
    parts = shlex.split(run.strip())
    assert parts[:2] == ["uv", "run"], f"expected a 'uv run' command, got: {run!r}"
    tool = parts[2]
    binary = VENV_BIN / tool
    assert binary.exists(), f"expected provisioned tool at {binary}"
    return [str(binary), *parts[3:]]


# ---------------------------------------------------------------------------
# Static contract: REQ-001, REQ-002, REQ-005, REQ-008
# ---------------------------------------------------------------------------


def test_workflow_static_contract_matches_requirements() -> None:
    workflow = _workflow()
    assert workflow["name"] == "PR Tests"
    assert workflow["on"]["pull_request"]["types"] == ["opened", "synchronize", "reopened"]
    assert "workflow_dispatch" in workflow["on"]

    lint = _lint_job()
    assert lint["name"] == "Lint & Type Check"
    assert "if" not in lint, "the lint job must not be gated by author/provider/label/path conditions"

    steps = lint["steps"]
    names = [step["name"] for step in steps]
    assert names == [
        "Checkout",
        "Setup Python",
        "Install uv",
        "Install dependencies",
        "Check repository hygiene",
        "Black (check)",
        "isort (check)",
        "Flake8",
        "Mypy",
    ]
    assert steps[0]["uses"].startswith("actions/checkout@")

    for step in steps:
        assert "if" not in step, f"step {step['name']!r} must not be conditionally skipped"
        assert step.get("continue-on-error") is not True, f"step {step['name']!r} must not swallow failures"
        run = step.get("run", "")
        assert "|| true" not in run and "continue-on-error" not in run and "set +e" not in run

    install = _lint_step("Install dependencies")
    assert "uv sync --extra dev" in install["run"]

    hygiene = _lint_step("Check repository hygiene")
    assert hygiene["run"].strip() == "scripts/check_repository_hygiene.py --source head --repo ."

    black_step = _lint_step("Black (check)")
    assert black_step["run"].strip() == "uv run black --check src/ tests/"

    isort_step = _lint_step("isort (check)")
    assert isort_step["run"].strip() == "uv run isort --check-only src/ tests/"

    flake8_step = _lint_step("Flake8")
    assert flake8_step["run"].strip() == "uv run flake8 src/ tests/"

    mypy_step = _lint_step("Mypy")
    assert mypy_step["run"].strip() == "uv run mypy --config-file pyproject.toml -p auto_coder"

    # REQ-002/REQ-003: the same canonical invocation, not a diverging local copy.
    mypy_hook = _pre_commit_hook("mypy")
    assert mypy_hook["args"] == ["--config-file", "pyproject.toml", "-p", "auto_coder"]


def test_no_competing_root_mypy_ini_and_pyproject_is_authoritative() -> None:
    assert not (ROOT / "mypy.ini").exists()
    assert not (ROOT / "setup.cfg").exists()
    assert "mypy.ini" not in ALLOWLIST

    with PYPROJECT.open("rb") as handle:
        config = tomllib.load(handle)["tool"]["mypy"]

    assert config["python_version"] == "3.12"
    assert config["mypy_path"] == "src"
    assert config["namespace_packages"] is True
    assert config["explicit_package_bases"] is True
    assert config["strict"] is False
    assert config["check_untyped_defs"] is False
    assert config["ignore_missing_imports"] is True
    assert config["no_implicit_optional"] is False
    assert config["packages"] == ["auto_coder"]


# ---------------------------------------------------------------------------
# Fixture repository shared by the dynamic scenarios below
# ---------------------------------------------------------------------------


def _seed_lint_fixture_repo(repo: Path) -> None:
    """Build a disposable, committed checkout with a real auto_coder package.

    Copies the real ``pyproject.toml``/``.flake8`` (so black/isort/flake8/mypy
    settings are the production ones) plus the *entire* real ``src/auto_coder``
    tree (so REQ-002's "recursively check the package" and REQ-003's
    "resolution from the checked-out src/auto_coder" are exercised against
    real production source, not a synthetic stand-in package).
    """
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")

    shutil.copy2(PYPROJECT, repo / "pyproject.toml")
    shutil.copy2(FLAKE8_CONFIG, repo / ".flake8")
    shutil.copy2(ROOT / "uv.lock", repo / "uv.lock")
    shutil.copy2(ROOT / ".python-version", repo / ".python-version")
    write(repo, "scripts/repository_hygiene_allowlist.json", (ROOT / "scripts/repository_hygiene_allowlist.json").read_text())
    checker = write(repo, "scripts/check_repository_hygiene.py", (ROOT / "scripts/check_repository_hygiene.py").read_text())
    checker.chmod(0o755)
    shutil.copytree(ROOT / "src/auto_coder", repo / "src/auto_coder")
    write(repo, "tests/test_placeholder.py", "def test_placeholder() -> None:\n    assert True\n")

    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed disposable checkout")


@pytest.fixture()
def lint_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _seed_lint_fixture_repo(repo)
    return repo


@pytest.fixture(scope="session")
def mypy_cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A cache directory shared by the mypy-driven scenarios in this file.

    mypy's own incremental cache invalidates per-file by content hash, so
    sharing it across these disposable fixture checkouts only saves the
    repeated cost of a full from-scratch package scan; it cannot mask a real
    type error introduced by any individual test.
    """
    return tmp_path_factory.mktemp("mypy-cache")


def _run(argv: list[str], cwd: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = _uv_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _mypy_argv(repo: Path, *, cache_dir: Path) -> list[str]:
    argv = _venv_argv(_lint_step("Mypy")["run"])
    return [*argv, "--cache-dir", str(cache_dir)]


# ---------------------------------------------------------------------------
# AS-001: clean setup, fresh environment, all five checks succeed
# ---------------------------------------------------------------------------


def test_fresh_environment_setup_provisions_tools_and_all_checks_pass(tmp_path: Path) -> None:
    """Reproduce the lint job's actual setup + check commands from scratch.

    The checkout has no preinstalled Black/isort/Flake8/mypy and no installed
    git hooks -- ``uv sync --extra dev`` is the only thing that makes those
    tools available, exactly as on a fresh GitHub Actions runner.
    """
    repo = tmp_path / "repo"
    shutil.copytree(
        ROOT,
        repo,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".mypy_cache",
            "__pycache__",
            ".pytest_cache",
            "htmlcov",
            "pr-test-logs",
            "node_modules",
            ".agent-tmp",
        ),
    )
    assert not (repo / ".venv").exists()

    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fresh checkout")
    head_before = git(repo, "rev-parse", "HEAD").stdout

    install = _run(["uv", "sync", "--extra", "dev"], cwd=repo)
    assert install.returncode == 0, install.stderr
    for tool in ("black", "isort", "flake8", "mypy"):
        assert (repo / ".venv/bin" / tool).exists(), f"{tool} was not provisioned by the declared dev extra"

    hygiene = _run(shlex.split(_lint_step("Check repository hygiene")["run"]), cwd=repo)
    assert hygiene.returncode == 0, hygiene.stdout + hygiene.stderr

    black = _run(["uv", "run", "black", "--check", "src/", "tests/"], cwd=repo)
    assert black.returncode == 0, black.stdout + black.stderr

    isort = _run(["uv", "run", "isort", "--check-only", "src/", "tests/"], cwd=repo)
    assert isort.returncode == 0, isort.stdout + isort.stderr

    flake8 = _run(["uv", "run", "flake8", "src/", "tests/"], cwd=repo)
    assert flake8.returncode == 0, flake8.stdout + flake8.stderr

    mypy = _run(["uv", "run", "mypy", "--config-file", "pyproject.toml", "-p", "auto_coder"], cwd=repo)
    assert mypy.returncode == 0, mypy.stdout + mypy.stderr
    assert "Success" in mypy.stdout

    # None of the checks may rewrite tracked source.
    assert git(repo, "status", "--porcelain").stdout == b""
    assert git(repo, "rev-parse", "HEAD").stdout == head_before


def test_workflow_dispatch_wiring_allows_selecting_an_arbitrary_ref() -> None:
    """AS-001's manually-selected-ref coverage: workflow_dispatch has no inputs

    that would force checkout onto something other than the dispatched ref, and
    the checkout step takes no explicit, hardcoded ``ref:``. That leaves
    ``actions/checkout``'s default (``github.ref``, i.e. the manually selected
    ref for a workflow_dispatch run, or the PR merge ref otherwise) in effect.
    """
    workflow = _workflow()
    assert workflow["on"]["workflow_dispatch"] in (None, {})
    checkout = _lint_job()["steps"][0]
    assert checkout["name"] == "Checkout"
    assert "with" not in checkout or "ref" not in checkout.get("with", {})


# ---------------------------------------------------------------------------
# AS-002: bypassed hooks + an unimported module with real type errors
# ---------------------------------------------------------------------------


def test_mypy_detects_seeded_errors_in_unimported_and_nested_modules(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    assert (repo / "src/auto_coder/__init__.py").exists()

    # A module nobody imports from auto_coder/__init__.py.
    write(
        repo,
        "src/auto_coder/_lint_gate_regression_leaf.py",
        "value: int = 'wrong'\n",
    )
    # A nested package module with a return-type mismatch.
    write(repo, "src/auto_coder/_lint_gate_regression_pkg/__init__.py", "")
    write(
        repo,
        "src/auto_coder/_lint_gate_regression_pkg/leaf.py",
        "def broken() -> int:\n    return 'not an int'\n",
    )

    # Committed without running any hooks (none are installed in this fixture).
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed real type errors")

    result = _run(_mypy_argv(repo, cache_dir=mypy_cache_dir), cwd=repo)

    assert result.returncode != 0
    assert "_lint_gate_regression_leaf.py" in result.stdout
    assert "_lint_gate_regression_pkg/leaf.py" in result.stdout
    assert "error" in result.stdout


# ---------------------------------------------------------------------------
# AS-003: --config-file wins; Python-3.12-only source is accepted; hook parity
# ---------------------------------------------------------------------------


def test_explicit_config_file_wins_over_a_stray_mypy_ini(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    write(
        repo,
        "src/auto_coder/_lint_gate_regression_config_probe.py",
        "value: int = 'wrong'\n",
    )
    # A conflicting mypy.ini that would suppress the error if it were consulted.
    write(repo, "mypy.ini", "[mypy]\nignore_errors = True\npython_version = 3.11\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed conflicting mypy.ini beside the error")

    result = _run(_mypy_argv(repo, cache_dir=mypy_cache_dir), cwd=repo)
    assert result.returncode != 0
    assert "_lint_gate_regression_config_probe.py" in result.stdout


def test_python_312_only_syntax_is_not_rejected_as_a_syntax_error(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    # PEP 695 generic syntax, valid only from Python 3.12 onward. If mypy ever
    # fell back to a 3.11 configuration (as the old root mypy.ini declared),
    # this would be rejected as a syntax error rather than type-checked.
    write(
        repo,
        "src/auto_coder/_lint_gate_regression_py312_syntax.py",
        "type IntAlias = int\n\n\nclass Box[T]:\n    def __init__(self, value: T) -> None:\n        self.value = value\n",
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed 3.12-only syntax")

    result = _run(_mypy_argv(repo, cache_dir=mypy_cache_dir), cwd=repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "syntax" not in result.stdout.lower()


def test_pre_commit_mypy_hook_uses_the_same_configuration_and_target(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    write(repo, "src/auto_coder/_lint_gate_regression_hook_probe.py", "value: int = 'wrong'\n")
    write(repo, ".pre-commit-config.yaml", PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed pre-commit hook parity fixture")

    hook = _pre_commit_hook("mypy")
    argv = [str(VENV_BIN / "mypy"), *hook["args"], "--cache-dir", str(mypy_cache_dir)]

    result = _run(argv, cwd=repo)
    assert result.returncode != 0
    assert "_lint_gate_regression_hook_probe.py" in result.stdout


# ---------------------------------------------------------------------------
# AS-004: tool/config failure must not be converted into a pass
# ---------------------------------------------------------------------------


def test_unreadable_explicit_config_file_fails_loudly(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    argv = [str(VENV_BIN / "mypy"), "--config-file", "does-not-exist.toml", "-p", "auto_coder", "--cache-dir", str(mypy_cache_dir)]
    result = _run(argv, cwd=repo)
    assert result.returncode != 0
    assert result.stdout or result.stderr


def test_checker_that_cannot_launch_fails_the_step(lint_fixture_repo: Path) -> None:
    """Reproduce the literal "Mypy" step's ``run:`` command with ``uv`` unavailable.

    On a real runner this is what "the checker cannot launch" looks like: the
    step's shell command fails (command not found) rather than the job
    silently reporting success.
    """
    repo = lint_fixture_repo
    bash = shutil.which("bash")
    uv_path = shutil.which("uv")
    assert bash is not None and uv_path is not None
    uv_dir = os.fspath(Path(uv_path).parent)
    stripped_path = os.pathsep.join(part for part in os.environ.get("PATH", "").split(os.pathsep) if part and part != uv_dir)

    result = subprocess.run(
        [bash, "-c", _lint_step("Mypy")["run"]],
        cwd=repo,
        env={**os.environ, "PATH": stripped_path},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "success" not in result.stdout.lower()


def test_dependency_installation_failure_fails_loudly(lint_fixture_repo: Path) -> None:
    result = _run(["uv", "sync", "--extra", "does-not-exist-extra"], cwd=lint_fixture_repo)
    assert result.returncode != 0
    assert "does-not-exist-extra" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# AS-005: the other static checks remain mandatory and non-mutating
# ---------------------------------------------------------------------------


def test_hygiene_violation_fails_the_gate(lint_fixture_repo: Path) -> None:
    repo = lint_fixture_repo
    write(repo, "stray_root_artifact.bin", "junk")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed hygiene violation")

    result = _run(shlex.split(_lint_step("Check repository hygiene")["run"]), cwd=repo)
    assert result.returncode == 1
    assert "stray_root_artifact.bin" in result.stdout


def test_black_violation_fails_without_rewriting_the_file(lint_fixture_repo: Path) -> None:
    repo = lint_fixture_repo
    target = write(repo, "src/auto_coder/_lint_gate_regression_black.py", "def f( ):\n    return   1\n")
    before = target.read_text()

    result = _run(_venv_argv(_lint_step("Black (check)")["run"]), cwd=repo)
    assert result.returncode != 0
    assert target.read_text() == before


def test_isort_violation_fails_without_rewriting_the_file(lint_fixture_repo: Path) -> None:
    repo = lint_fixture_repo
    target = write(repo, "src/auto_coder/_lint_gate_regression_isort.py", "import sys\nimport os\n")
    before = target.read_text()

    result = _run(_venv_argv(_lint_step("isort (check)")["run"]), cwd=repo)
    assert result.returncode != 0
    assert target.read_text() == before


def test_flake8_violation_fails(lint_fixture_repo: Path) -> None:
    repo = lint_fixture_repo
    write(repo, "src/auto_coder/_lint_gate_regression_flake8.py", "def f():\n    return undefined_name_used_for_this_regression\n")

    result = _run(_venv_argv(_lint_step("Flake8")["run"]), cwd=repo)
    assert result.returncode != 0
    assert "undefined_name_used_for_this_regression" in result.stdout or "F821" in result.stdout


# ---------------------------------------------------------------------------
# AS-006: a configuration-only PR must not filter out a pre-existing error
# ---------------------------------------------------------------------------


def test_config_only_change_does_not_hide_a_preexisting_package_error(lint_fixture_repo: Path, mypy_cache_dir: Path) -> None:
    repo = lint_fixture_repo
    write(repo, "src/auto_coder/_lint_gate_regression_preexisting.py", "value: int = 'wrong'\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base commit with a package type error")
    base_head = git(repo, "rev-parse", "HEAD").stdout.decode().strip()

    # A PR that changes only a non-Python documentation file -- the seeded
    # type error above is untouched by this diff.
    write(repo, "docs/NOTES.md", "docs only\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "docs-only change")

    changed_files = git(repo, "diff", "--name-only", base_head, "HEAD").stdout.decode().splitlines()
    assert changed_files == ["docs/NOTES.md"]
    assert "src/auto_coder/_lint_gate_regression_preexisting.py" not in changed_files

    result = _run(_mypy_argv(repo, cache_dir=mypy_cache_dir), cwd=repo)
    assert result.returncode != 0
    assert "_lint_gate_regression_preexisting.py" in result.stdout
