import json
from dataclasses import dataclass, field

import pytest
from click.testing import CliRunner

from auto_coder.cli_commands_deployment import deployment_group
from auto_coder.release_promotion import ReleasePromotion, ReleasePromotionError
from auto_coder.util.gh_cache import GitDataResponse

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
TAG = "release-20260907T174912JST-r42"


@dataclass
class GitHubState:
    tags: dict[str, dict[str, object]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    releases: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, object] | None]] = field(default_factory=list)
    fail_release_write: bool = False

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "actions/runs/42"):
            return GitDataResponse(200, {"created_at": "2026-09-07T08:49:12Z"})
        if method == "GET" and path.startswith("git/ref/tags/"):
            name = path.removeprefix("git/ref/tags/")
            sha = self.refs.get(name)
            data = {"ref": f"refs/tags/{name}", "object": {"type": "tag", "sha": sha}}
            return GitDataResponse(200, data) if sha else GitDataResponse(404, {})
        if method == "GET" and path.startswith("git/tags/"):
            return GitDataResponse(200, self.tags[path.removeprefix("git/tags/")])
        if (method, path) == ("POST", "git/tags") and payload is not None:
            sha = f"{len(self.tags) + 1:040x}"
            self.tags[sha] = {**payload, "object": {"type": payload["type"], "sha": payload["object"]}}
            return GitDataResponse(201, {"sha": sha})
        if (method, path) == ("POST", "git/refs") and payload is not None:
            self.refs[str(payload["ref"]).removeprefix("refs/tags/")] = str(payload["sha"])
            return GitDataResponse(201, {})
        if method == "GET" and path.startswith("releases/tags/"):
            release = self.releases.get(path.removeprefix("releases/tags/"))
            return GitDataResponse(200, release) if release else GitDataResponse(404, {})
        if (method, path) == ("POST", "releases") and payload is not None:
            if self.fail_release_write:
                return GitDataResponse(500, {})
            release = {**payload, "html_url": f"https://github.com/owner/repo/releases/tag/{payload['tag_name']}"}
            self.releases[str(payload["tag_name"])] = release
            return GitDataResponse(201, release)
        raise AssertionError((method, path, payload))


@dataclass
class RegistryState:
    tags: dict[str, str] = field(
        default_factory=lambda: {
            "ghcr.io/owner/repo:beta": DIGEST,
            f"ghcr.io/owner/repo:tested-beta-{SHA}": DIGEST,
            f"ghcr.io/owner/repo:release-{SHA}": DIGEST,
        }
    )
    writes: list[str] = field(default_factory=list)
    revisions: dict[str, str] = field(default_factory=lambda: {f"ghcr.io/owner/repo@{DIGEST}": SHA})

    def inspect_digest(self, reference: str) -> str:
        if reference not in self.tags:
            raise RuntimeError("unavailable")
        return self.tags[reference]

    def inspect_revision(self, reference: str) -> str:
        return self.revisions[reference]

    def require_equal(self, expected: str, actual: str, evidence: str) -> None:
        if expected != actual:
            raise ReleasePromotionError(f"{evidence} digest mismatch")

    def ensure_history(self, image: str, source_sha: str, digest: str) -> None:
        self.require_equal(digest, self.tags[f"{image}:release-{source_sha}"], "history")

    def set_release(self, image: str, digest: str) -> None:
        self.writes.append(digest)
        self.tags[f"{image}:release"] = digest


def test_initial_operation_pins_digest_metadata_then_publishes_complete_release() -> None:
    github = GitHubState()
    registry = RegistryState()
    memo = '日本語\n"quoted" `code` $(touch /tmp/nope) ::set-env name=X::bad'

    outcome = ReleasePromotion(github, registry, "owner/repo", 42, 1, memo).execute()

    assert outcome.result == "published"
    assert registry.writes == [DIGEST]
    assert outcome.record.source_sha == SHA
    assert outcome.record.memo == memo
    assert github.refs[TAG]
    release = github.releases[TAG]
    assert release["draft"] is False and release["prerelease"] is False
    assert memo in str(release["body"])
    assert all(value in str(release["body"]) for value in (SHA, DIGEST, TAG, f"release-{SHA}", "/actions/runs/42"))
    paths = [path for _, path, _ in github.calls]
    assert paths.index(f"git/ref/tags/{TAG}") < paths.index("releases")


def test_rerun_published_is_read_only_even_after_channel_and_beta_advance() -> None:
    github = GitHubState()
    registry = RegistryState()
    ReleasePromotion(github, registry, "owner/repo", 42, 1, "first").execute()
    registry.writes.clear()
    registry.tags["ghcr.io/owner/repo:beta"] = OTHER
    registry.tags["ghcr.io/owner/repo:release"] = OTHER

    outcome = ReleasePromotion(github, registry, "owner/repo", 42, 2, "changed").execute()

    assert outcome.result == "already catalogued"
    assert outcome.record.memo == "first"
    assert registry.writes == []


def test_prepared_recovery_only_publishes_while_channel_still_matches() -> None:
    github = GitHubState(fail_release_write=True)
    registry = RegistryState()
    with pytest.raises(ReleasePromotionError, match="channel changed"):
        ReleasePromotion(github, registry, "owner/repo", 42, 1, "memo").execute()
    assert registry.tags["ghcr.io/owner/repo:release"] == DIGEST
    github.fail_release_write = False
    registry.writes.clear()

    recovered = ReleasePromotion(github, registry, "owner/repo", 42, 2, "ignored").execute()
    assert recovered.result == "publication recovered"
    assert registry.writes == []

    del github.releases[TAG]
    registry.tags["ghcr.io/owner/repo:release"] = OTHER
    with pytest.raises(ReleasePromotionError, match="superseded"):
        ReleasePromotion(github, registry, "owner/repo", 42, 3, "ignored").execute()
    assert registry.writes == []


def test_rerun_without_record_and_conflicting_release_fail_closed() -> None:
    with pytest.raises(ReleasePromotionError, match="no durable operation"):
        ReleasePromotion(GitHubState(), RegistryState(), "owner/repo", 42, 2, "memo").execute()

    github = GitHubState()
    registry = RegistryState()
    ReleasePromotion(github, registry, "owner/repo", 42, 1, "memo").execute()
    github.releases[TAG]["draft"] = True
    with pytest.raises(ReleasePromotionError, match="conflicting"):
        ReleasePromotion(github, registry, "owner/repo", 42, 2, "memo").execute()


def test_production_cli_preserves_memo_from_workflow_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    github = GitHubState()
    registry = RegistryState()
    memo = '日本語\n"quoted" $(false) `false` ::set-output name=x::bad'
    monkeypatch.setenv("PROMOTION_MEMO", memo)
    monkeypatch.setattr("auto_coder.cli_commands_deployment.GitHubGitDataClient", lambda *_args: github)
    monkeypatch.setattr("auto_coder.cli_commands_deployment.Registry", lambda: registry)

    result = CliRunner().invoke(
        deployment_group,
        [
            "promote-release",
            "--repository",
            "owner/repo",
            "--run-id",
            "42",
            "--run-attempt",
            "1",
            "--github-token",
            "catalog-token",
        ],
    )

    assert result.exit_code == 0, result.output
    annotation = github.tags[github.refs[TAG]]["message"]
    assert isinstance(annotation, str)
    assert memo == json.loads(annotation)["memo"]
