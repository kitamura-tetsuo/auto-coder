"""The disposable smoke Git service must not inherit live source-selection refs."""

import subprocess
from pathlib import Path

import pytest

from auto_coder.beta_source_selection import BETA_REF, CURRENT_SELECTION_REF, BetaSourceSelector, MutationCertainty
from auto_coder.util.gh_cache import GitHubGitDataClient
from tests.test_beta_runtime_smoke_container import _source_http, _SourceService

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")


def test_smoke_git_clone_excludes_upstream_tags_and_retains_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.com")
    (source / "file").write_text("first")
    git("add", "file")
    git("commit", "-m", "first")
    old = git("rev-parse", "HEAD")
    (source / "file").write_text("second")
    git("commit", "-am", "second")
    selected = git("rev-parse", "HEAD")
    for ref in [BETA_REF, CURRENT_SELECTION_REF, "refs/tags/auto-coder-beta-selection-r77", "refs/tags/unrelated-upstream-tag"]:
        git("update-ref", ref, old)
    original = git("show-ref")
    service = _SourceService(source, tmp_path / "fixture.git")
    assert service._git("for-each-ref", "--format=%(refname)", "refs/tags") == ""
    assert service._git("rev-parse", "HEAD") == selected
    service.publish_proof({"source_sha": selected, "distribution_version": "1.0.0", "lock_sha256": "a" * 64}, 101)
    service.executions[(11, 1)] = selected, "2026-10-01T01:00:00Z"
    with _source_http(service) as api_url:
        selector = BetaSourceSelector(GitHubGitDataClient("fixture-only", "owner/repo", api_url=api_url), "owner/repo")
        receipt = selector.advance("r77", None)
        assert receipt.certainty is MutationCertainty.CONFIRMED and receipt.selected_sha == selected
        assert service._git("rev-parse", BETA_REF) == selected
    assert git("show-ref") == original
