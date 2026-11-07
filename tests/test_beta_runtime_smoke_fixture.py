"""Real Git regression for disposable beta smoke evidence isolation."""

import subprocess
from pathlib import Path

import pytest

from auto_coder.beta_source_selection import BetaSourceSelector, MutationCertainty
from tests.test_beta_runtime_smoke_container import _SourceService


@pytest.mark.parametrize("detached", [False, True])
def test_source_service_does_not_inherit_checkout_acceptance_tags(tmp_path: Path, _use_real_commands: None, detached: bool) -> None:
    source = tmp_path / "source.git"
    subprocess.run(["git", "init", "--bare", str(source)], check=True, capture_output=True)

    def git(*args: str, input: str | None = None) -> str:
        return subprocess.run(["git", f"--git-dir={source}", *args], input=input, check=True, capture_output=True, text=True).stdout.strip()

    tree = git("mktree", input="")
    prior = git("hash-object", "-w", "-t", "commit", "--stdin", input=f"tree {tree}\nauthor Fixture <fixture@example.com> 0 +0000\ncommitter Fixture <fixture@example.com> 0 +0000\n\nprior\n")
    candidate = git("hash-object", "-w", "-t", "commit", "--stdin", input=f"tree {tree}\nparent {prior}\nauthor Fixture <fixture@example.com> 1 +0000\ncommitter Fixture <fixture@example.com> 1 +0000\n\ncandidate\n")
    git("update-ref", "refs/heads/main", candidate)
    git("symbolic-ref", "HEAD", "refs/heads/main")
    if detached:
        git("update-ref", "refs/heads/main", prior)
        git("update-ref", "--no-deref", "HEAD", candidate)
    inherited = ("auto-coder-beta-current", "auto-coder-beta-selection-current", "auto-coder-beta-selection-r77", f"auto-coder-tested-{candidate}", "auto-coder-beta-acceptance-existing")
    for name in inherited:
        git("update-ref", f"refs/tags/{name}", prior)
    original_refs = git("show-ref")

    remote = _SourceService(source, tmp_path / "isolated.git")
    assert remote._git("rev-parse", "HEAD") == candidate
    assert remote._git("cat-file", "-t", prior) == "commit"
    assert remote._git("for-each-ref", "--format=%(refname)", "refs/tags") == ""
    for name in inherited:
        assert remote.request("GET", f"git/ref/tags/{name}").status == 404

    remote.publish_proof({"source_sha": candidate, "distribution_version": f"1.0+g{candidate}", "lock_sha256": "e" * 64}, 101)
    remote.executions[(11, 1)] = (candidate, "2026-10-01T01:00:00Z")
    selected = BetaSourceSelector(remote, "owner/repo").advance("r77", None)
    assert selected.certainty is MutationCertainty.CONFIRMED
    assert selected.selected_sha == candidate
    assert selected.observed_sha == candidate
    assert git("show-ref") == original_refs
