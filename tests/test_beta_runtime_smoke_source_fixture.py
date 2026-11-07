"""The container smoke source transport must not inherit live repository tags."""

import subprocess
from pathlib import Path

from auto_coder.beta_source_selection import BetaSourceSelector, MutationCertainty
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.util.gh_cache import GitHubGitDataClient
from tests.test_beta_runtime_smoke_container import _source_http, _SourceService


def test_source_fixture_excludes_checkout_tags_without_losing_commits(tmp_path: Path, _use_real_commands: None) -> None:
    source = tmp_path / "checkout"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True, text=True).stdout.strip()

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.com")
    git("commit", "--allow-empty", "-m", "Prior source")
    previous = git("rev-parse", "HEAD")
    git("tag", "auto-coder-beta-current")
    git("tag", "-a", "auto-coder-beta-selection-current", "-m", "Live selection")
    git("tag", "-a", "auto-coder-beta-selection-r77", "-m", "Existing operation")
    git("tag", "-a", f"auto-coder-tested-{previous}", "-m", "Live proof")
    git("commit", "--allow-empty", "-m", "Selected source")
    selected = git("rev-parse", "HEAD")
    original_refs = git("show-ref", "--tags")

    remote = _SourceService(source, tmp_path / "source.git")
    assert remote._git("for-each-ref", "refs/tags") == ""
    assert remote._git("rev-parse", "HEAD") == selected
    assert remote._git("rev-parse", "HEAD^") == previous
    assert remote.request("GET", "git/ref/tags/auto-coder-beta-current").status == 404
    remote.publish_proof({"source_sha": selected, "distribution_version": "1.0.0", "lock_sha256": "e" * 64}, 101)
    remote.executions[(11, 1)] = selected, "2026-10-01T01:00:00Z"
    with _source_http(remote) as api_url:
        selector = BetaSourceSelector(GitHubGitDataClient("external-selector", "owner/repo", api_url=api_url), "owner/repo")
        receipt = selector.advance("r77", None)
        assert receipt.certainty is MutationCertainty.CONFIRMED
        assert receipt.selected_sha == selected
        reader = BetaEvidenceReader(GitHubGitDataClient("read-only-fixture", "owner/repo", api_url=api_url), "owner/repo")
        evidence = reader.read(selected)
        assert evidence.proof_run_id == 101
        assert evidence.selected_run_id == 11
    assert git("show-ref", "--tags") == original_refs
    assert remote.read_only_mutations == []
