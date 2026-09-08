from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.usefixtures("_use_real_commands")

CHECKER = Path(__file__).parents[1] / "scripts" / "check_repository_hygiene.py"
ALLOWLIST = json.loads((Path(__file__).parents[1] / "scripts" / "repository_hygiene_allowlist.json").read_text())
REPOSITORY = Path(__file__).parents[1]
WORKFLOW = REPOSITORY / ".github/workflows/pr-tests.yml"
PRE_COMMIT_CONFIG = REPOSITORY / ".pre-commit-config.yaml"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write(repo: Path, relative: str, content: str = "content\n") -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def initialize(repo: Path, *, commit: bool = True) -> None:
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.com")
    write(
        repo,
        "scripts/repository_hygiene_allowlist.json",
        json.dumps(ALLOWLIST),
    )
    write(repo, "README.md")
    write(repo, "scripts/maintained.py")
    write(repo, "tests/test_feature.py")
    git(repo, "add", ".")
    if commit:
        git(repo, "commit", "-qm", "initial")


def check(repo: Path, source: str = "index", cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, os.fspath(CHECKER), "--repo", os.fspath(cwd or repo), "--source", source],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def copy_checker(repo: Path) -> None:
    destination = write(repo, "scripts/check_repository_hygiene.py", CHECKER.read_text())
    destination.chmod(0o755)


def configured_hygiene_hook() -> dict[str, object]:
    config = yaml.safe_load(PRE_COMMIT_CONFIG.read_text())
    return next(hook for repository in config["repos"] for hook in repository["hooks"] if hook["id"] == "repository-hygiene")


def test_valid_states_ignore_untracked_artifacts_and_accept_subdirectory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    write(repo, ".agent-tmp/check.py")
    write(repo, ".agent-tmp/nested/result.txt")
    write(repo, "local output.log")

    assert check(repo).returncode == 0
    assert check(repo, "head", repo / "scripts").returncode == 0


def test_all_unknown_root_names_and_symlink_are_reported_without_splitting(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    names = [
        "debug_something.py",
        "result.txt",
        "foo.patch",
        "coverage.json",
        "=1.0.0",
        "<MagicMock name='stdout' id='42'>",
        ".unknown",
        "with spaces",
        "with\nnewline",
        "雪",
    ]
    for name in names:
        write(repo, name, "")
    os.symlink("README.md", repo / "unknown-link")
    git(repo, "add", ".")

    result = check(repo)

    assert result.returncode == 1
    assert result.stdout.count("VIOLATION:") == len(names) + 1
    for name in names + ["unknown-link"]:
        assert json.dumps(name, ensure_ascii=True) in result.stdout
    assert '"with\\nnewline"' in result.stdout
    assert "Disposable artifacts should remain untracked" in result.stdout


def test_index_and_head_remain_isolated_from_worktree_and_each_other(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    violation = write(repo, "debug_something.py")
    git(repo, "add", "debug_something.py")
    violation.unlink()
    assert check(repo).returncode == 1
    assert check(repo, "head").returncode == 0

    git(repo, "commit", "-qm", "track violation")
    git(repo, "add", "-u")
    assert check(repo).returncode == 0
    assert check(repo, "head").returncode == 1


def test_allowlist_is_read_only_from_selected_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    write(repo, "new-root.txt")
    git(repo, "add", "new-root.txt")
    allowlist_path = repo / "scripts/repository_hygiene_allowlist.json"
    allowlist_path.write_text(json.dumps([*ALLOWLIST, "new-root.txt"]))
    assert check(repo).returncode == 1

    git(repo, "add", os.fspath(allowlist_path.relative_to(repo)))
    assert check(repo).returncode == 0
    assert check(repo, "head").returncode == 0


def test_force_added_agent_tmp_content_and_agent_tmp_file_fail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    write(repo, ".gitignore", ".agent-tmp/\n")
    for name in [".agent-tmp/check.py", ".agent-tmp/nested/result.txt", ".agent-tmp/.gitkeep"]:
        write(repo, name)
    git(repo, "add", ".gitignore")
    git(repo, "add", "-f", ".agent-tmp")
    result = check(repo)
    assert result.returncode == 1
    assert result.stdout.count("tracked .agent-tmp content is prohibited") == 3

    other = tmp_path / "other"
    initialize(other)
    write(other, ".agent-tmp")
    git(other, "add", ".agent-tmp")
    assert check(other).returncode == 1


@pytest.mark.parametrize(
    "policy",
    [
        "not JSON",
        json.dumps({"README.md": True}),
        json.dumps(["README.md", "README.md"]),
        json.dumps(["*.md"]),
        json.dumps(["root.py"]),
        json.dumps([""]),
    ],
)
def test_invalid_selected_allowlist_fails_closed(tmp_path: Path, policy: str) -> None:
    repo = tmp_path / "repo"
    initialize(repo, commit=False)
    write(repo, "scripts/repository_hygiene_allowlist.json", policy)
    git(repo, "add", "scripts/repository_hygiene_allowlist.json")
    assert check(repo).returncode == 2


def test_missing_allowlist_and_unavailable_git_states_fail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo, commit=False)
    git(repo, "rm", "--cached", "scripts/repository_hygiene_allowlist.json")
    assert check(repo).returncode == 2
    assert check(repo, "head").returncode == 2
    assert check(repo, cwd=tmp_path).returncode == 2


def test_pre_first_commit_index_is_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo, commit=False)
    assert check(repo).returncode == 0


def test_index_conflict_fails_but_head_ignores_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    base = git(repo, "branch", "--show-current").stdout.decode().strip()
    write(repo, "README.md", "main\n")
    git(repo, "commit", "-qam", "main edit")
    git(repo, "checkout", "-qb", "other", "HEAD~1")
    write(repo, "README.md", "other\n")
    git(repo, "commit", "-qam", "other edit")
    merge = subprocess.run(
        ["git", "-C", os.fspath(repo), "merge", base],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert merge.returncode != 0

    assert check(repo).returncode == 2
    assert check(repo, "head").returncode == 0


def test_checker_does_not_modify_repository_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    write(repo, "old-artifact.bin")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "pre-existing violation")
    before = git(repo, "status", "--porcelain=v2", "-z").stdout
    head_before = git(repo, "rev-parse", "HEAD").stdout
    index_before = (repo / ".git/index").read_bytes()

    assert check(repo).returncode == 1
    assert git(repo, "status", "--porcelain=v2", "-z").stdout == before
    assert git(repo, "rev-parse", "HEAD").stdout == head_before
    assert (repo / ".git/index").read_bytes() == index_before
    assert (repo / "old-artifact.bin").exists()


def test_pr_workflow_runs_real_head_check_before_existing_linters(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    lint = workflow["jobs"]["lint"]
    assert lint["name"] == "Lint & Type Check"
    steps = lint["steps"]
    names = [step["name"] for step in steps]
    hygiene = steps[names.index("Check repository hygiene")]
    assert hygiene == {
        "name": "Check repository hygiene",
        "run": "scripts/check_repository_hygiene.py --source head --repo .",
    }
    assert names.index("Install dependencies") < names.index("Check repository hygiene")
    assert names.index("Check repository hygiene") < names.index("Black (check)")
    assert names.index("Black (check)") < names.index("isort (check)") < names.index("Flake8")

    repo = tmp_path / "repo"
    initialize(repo)
    copy_checker(repo)
    git(repo, "add", "scripts/check_repository_hygiene.py")
    git(repo, "commit", "-qm", "add checker")
    write(repo, "result.txt")
    git(repo, "add", "result.txt")
    git(repo, "commit", "-qm", "add violation")
    result = subprocess.run(shlex.split(str(hygiene["run"])), cwd=repo, check=False)
    assert result.returncode == 1
    git(repo, "rm", "-q", "result.txt")
    git(repo, "commit", "-qm", "remove violation")
    assert subprocess.run(shlex.split(str(hygiene["run"])), cwd=repo, check=False).returncode == 0


def test_pre_commit_hook_uses_complete_index_and_propagates_failure(tmp_path: Path) -> None:
    hook = configured_hygiene_hook()
    assert hook == {
        "id": "repository-hygiene",
        "name": "repository hygiene",
        "language": "system",
        "entry": "python scripts/check_repository_hygiene.py --source index --repo .",
        "always_run": True,
        "pass_filenames": False,
    }

    repo = tmp_path / "repo"
    initialize(repo)
    copy_checker(repo)
    write(repo, ".pre-commit-config.yaml", PRE_COMMIT_CONFIG.read_text())
    git(repo, "add", "scripts/check_repository_hygiene.py", ".pre-commit-config.yaml")
    git(repo, "commit", "-qm", "install policy")
    violation = write(repo, "result.txt")
    git(repo, "add", "result.txt")
    violation.unlink()

    environment = os.environ.copy()
    environment["PRE_COMMIT_HOME"] = os.fspath(tmp_path / "pre-commit-home")
    command = [sys.executable, "-m", "pre_commit", "run", "repository-hygiene"]
    assert subprocess.run(command, cwd=repo, env=environment, check=False).returncode == 1
    git(repo, "add", "-u")
    assert subprocess.run(command, cwd=repo, env=environment, check=False).returncode == 0


def test_root_disposable_ignores_do_not_hide_source_files_or_forced_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    initialize(repo)
    copy_checker(repo)
    write(repo, ".gitignore", (REPOSITORY / ".gitignore").read_text())
    git(repo, "add", ".gitignore", "scripts/check_repository_hygiene.py")
    git(repo, "commit", "-qm", "install policy")

    ignored = [
        ".agent-tmp/nested/result.txt",
        "coverage.json",
        "debug_log.json",
        "codex.patch",
        "setup.py.backup",
        "=1.0.0",
        "<MagicMock name='result'>",
    ]
    for relative in ignored:
        write(repo, relative)
    assert all(git(repo, "check-ignore", "-q", relative).returncode == 0 for relative in ignored)

    controls = ["src/auto_coder/example.py", "tests/test_example.py", "scripts/example.py"]
    for relative in controls:
        write(repo, relative)
        assert subprocess.run(["git", "-C", os.fspath(repo), "check-ignore", "-q", relative], check=False).returncode == 1
    git(repo, "add", *controls)
    assert all(relative in git(repo, "diff", "--cached", "--name-only").stdout.decode().splitlines() for relative in controls)

    git(repo, "add", "-f", ".agent-tmp/nested/result.txt")
    assert check(repo).returncode == 1
