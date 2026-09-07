"""Real-worktree regression coverage for concurrent adversarial validation."""

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from auto_coder.pr_processor import isolated_pr_head_worktree
from auto_coder.utils import CommandExecutor


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_concurrent_real_validation_worktrees_are_context_isolated(tmp_path: Path, _use_real_commands: None) -> None:
    """Overlapping production worktree contexts never redirect process CWD."""
    remote = tmp_path / "remote.git"
    service_repo = tmp_path / "service"
    ambient_repo = tmp_path / "ambient"
    remote.mkdir()
    _git(remote, "init", "--bare")
    service_repo.mkdir()
    _git(service_repo, "init")
    _git(service_repo, "config", "user.email", "test@example.com")
    _git(service_repo, "config", "user.name", "Test")
    (service_repo / "identity.txt").write_text("base", encoding="utf-8")
    _git(service_repo, "add", ".")
    _git(service_repo, "commit", "-m", "base")
    _git(service_repo, "remote", "add", "origin", str(remote))

    heads: dict[int, str] = {}
    for number, identity in ((101, "alpha"), (202, "beta")):
        (service_repo / "identity.txt").write_text(identity, encoding="utf-8")
        _git(service_repo, "commit", "-am", identity)
        heads[number] = _git(service_repo, "rev-parse", "HEAD")
        _git(service_repo, "push", "origin", f"HEAD:refs/pull/{number}/head")

    ambient_repo.mkdir()
    _git(ambient_repo, "init")
    _git(ambient_repo, "config", "user.email", "test@example.com")
    _git(ambient_repo, "config", "user.name", "Test")
    (ambient_repo / "identity.txt").write_text("ambient", encoding="utf-8")
    _git(ambient_repo, "add", ".")
    _git(ambient_repo, "commit", "-m", "ambient")

    entered = threading.Barrier(3)
    release_first = threading.Event()
    first_cleaned = threading.Event()
    observations: dict[int, tuple[str, str, str]] = {}
    original_cwd = Path.cwd()
    os.chdir(service_repo)
    try:

        def validate(number: int) -> None:
            with isolated_pr_head_worktree("owner/repo", number, heads[number]) as worktree:
                entered.wait(timeout=10)
                first = CommandExecutor.run_command(["cat", "identity.txt"])
                sha = CommandExecutor.run_command(["git", "rev-parse", "HEAD"])
                if number == 101:
                    release_first.wait(timeout=10)
                else:
                    release_first.set()
                    first_cleaned.wait(timeout=10)
                second = CommandExecutor.run_command(["cat", "identity.txt"])
                observations[number] = (first.stdout.strip(), sha.stdout.strip(), second.stdout.strip())
            if number == 101:
                first_cleaned.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(validate, number) for number in heads]
            entered.wait(timeout=10)
            # Ordinary work explicitly bound to its checkout is unaffected while
            # both validations are active, and ambient process CWD never moved.
            ordinary = CommandExecutor.run_command(["cat", "identity.txt"], cwd=str(ambient_repo))
            assert ordinary.stdout.strip() == "ambient"
            assert Path.cwd() == service_repo
            for future in futures:
                future.result(timeout=20)
    finally:
        os.chdir(original_cwd)

    assert observations[101] == ("alpha", heads[101], "alpha")
    assert observations[202] == ("beta", heads[202], "beta")
    assert not any(Path("/tmp").glob("auto_coder_val_pr101_*"))
    assert not any(Path("/tmp").glob("auto_coder_val_pr202_*"))
