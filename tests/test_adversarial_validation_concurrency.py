import os
import subprocess
import threading
from pathlib import Path

from auto_coder.pr_processor import isolated_pr_head_worktree
from auto_coder.utils import CommandExecutor


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_real_validation_worktrees_are_thread_isolated(tmp_path, monkeypatch, _use_real_commands):
    """Overlapping production worktree contexts never mutate ambient CWD."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    service = tmp_path / "service"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "identity").write_text("base", encoding="utf-8")
    _git(seed, "add", "identity")
    _git(seed, "commit", "-m", "base")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")

    heads = {}
    for number, identity in ((101, "alpha"), (102, "beta")):
        _git(seed, "checkout", "-B", f"pr-{number}", "main")
        (seed / "identity").write_text(identity, encoding="utf-8")
        _git(seed, "commit", "-am", identity)
        heads[number] = _git(seed, "rev-parse", "HEAD")
        _git(seed, "push", "origin", f"HEAD:refs/pull/{number}/head")

    _git(tmp_path, "clone", "-b", "main", str(origin), str(service))
    monkeypatch.chdir(service)
    ambient = os.getcwd()
    both_entered = threading.Barrier(3)
    release_alpha = threading.Event()
    alpha_done = threading.Event()
    observations = {}

    def validate(number: int, expected_identity: str) -> None:
        with isolated_pr_head_worktree("owner/repo", number, heads[number]) as worktree:
            executor = CommandExecutor()
            observations[number] = (
                executor.run_command(["git", "rev-parse", "HEAD"]).stdout.strip(),
                executor.run_command(["cat", "identity"]).stdout.strip(),
                worktree,
            )
            both_entered.wait()
            if number == 101:
                release_alpha.wait(timeout=5)
            else:
                release_alpha.set()
                alpha_done.wait(timeout=5)
                observations["sibling_after_cleanup"] = (
                    Path(worktree).exists(),
                    executor.run_command(["cat", "identity"]).stdout.strip(),
                )
        if number == 101:
            alpha_done.set()

    threads = [
        threading.Thread(target=validate, args=(101, "alpha")),
        threading.Thread(target=validate, args=(102, "beta")),
    ]
    for thread in threads:
        thread.start()
    both_entered.wait(timeout=10)
    assert os.getcwd() == ambient
    assert CommandExecutor().run_command(["git", "rev-parse", "--show-toplevel"]).stdout.strip() == ambient
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert observations[101][:2] == (heads[101], "alpha")
    assert observations[102][:2] == (heads[102], "beta")
    assert observations["sibling_after_cleanup"] == (True, "beta")
    assert not Path(observations[101][2]).exists()
    assert not Path(observations[102][2]).exists()
    assert os.getcwd() == ambient
