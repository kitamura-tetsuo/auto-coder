from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from auto_coder.release_catalog import ReleaseCatalog, new_record
from auto_coder.release_promotion import ReleasePromotion, ReleasePromotionError
from auto_coder.release_restore import ReleaseRestore, ReleaseRestoreError
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

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "actions/runs/42"):
            return GitDataResponse(200, {"created_at": "2026-09-07T08:49:12Z"})
        if method == "GET" and path.startswith("git/ref/tags/"):
            name = path.removeprefix("git/ref/tags/")
            sha = self.refs.get(name)
            return GitDataResponse(200, {"ref": f"refs/tags/{name}", "object": {"type": "tag", "sha": sha}}) if sha else GitDataResponse(404, {})
        if method == "GET" and path.startswith("git/tags/"):
            tag = self.tags.get(path.removeprefix("git/tags/"))
            return GitDataResponse(200, tag) if tag else GitDataResponse(404, {})
        if (method, path) == ("POST", "git/tags") and payload is not None:
            identity = f"{len(self.tags) + 1:040x}"
            self.tags[identity] = {**payload, "object": {"type": payload["type"], "sha": payload["object"]}}
            return GitDataResponse(201, {"sha": identity})
        if (method, path) == ("POST", "git/refs") and payload is not None:
            self.refs[str(payload["ref"]).removeprefix("refs/tags/")] = str(payload["sha"])
            return GitDataResponse(201, {})
        if method == "GET" and path.startswith("releases/tags/"):
            release = self.releases.get(path.removeprefix("releases/tags/"))
            return GitDataResponse(200, release) if release else GitDataResponse(404, {})
        if (method, path) == ("POST", "releases") and payload is not None:
            release = {
                **payload,
                "html_url": f"https://github.com/owner/repo/releases/tag/{payload['tag_name']}",
            }
            self.releases[str(payload["tag_name"])] = release
            return GitDataResponse(201, release)
        raise AssertionError((method, path, payload))


@dataclass
class RegistryState:
    tags: dict[str, str] = field(default_factory=dict)
    revisions: dict[str, str] = field(default_factory=dict)
    writes: list[str] = field(default_factory=list)
    readback: str | None = None

    def inspect_digest(self, reference: str) -> str:
        if reference == "ghcr.io/owner/repo:release" and self.readback is not None and self.writes:
            return self.readback
        if reference not in self.tags:
            raise RuntimeError("unavailable")
        return self.tags[reference]

    def inspect_revision(self, reference: str) -> str:
        if reference not in self.revisions:
            raise RuntimeError("unavailable")
        return self.revisions[reference]

    def set_release(self, image: str, digest: str) -> None:
        self.writes.append(digest)
        self.tags[f"{image}:release"] = digest

    def require_equal(self, expected: str, actual: str, evidence: str) -> None:
        if expected != actual:
            raise ReleasePromotionError(f"{evidence} digest mismatch")

    def ensure_history(self, image: str, source_sha: str, digest: str) -> None:
        self.require_equal(digest, self.tags[f"{image}:release-{source_sha}"], "history")


def published_catalog() -> GitHubState:
    state = GitHubState()
    record = new_record("owner/repo", 42, "2026-09-07T08:49:12Z", SHA, DIGEST, "safe $(false) memo")
    ReleaseCatalog(state, "owner/repo").prepare(record)
    state.releases[TAG] = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "target_commitish": "main",
        "name": f"editable lies: {OTHER}",
        "body": "$(touch /tmp/must-not-run)",
        "html_url": f"https://github.com/owner/repo/releases/tag/{TAG}",
    }
    state.calls.clear()
    return state


def catalog_registry() -> RegistryState:
    return RegistryState(
        tags={f"ghcr.io/owner/repo:release-{SHA}": DIGEST, "ghcr.io/owner/repo:release": OTHER},
        revisions={f"ghcr.io/owner/repo@{DIGEST}": SHA},
    )


def test_production_catalog_origin_restores_pinned_digest_and_is_repeatable() -> None:
    github = GitHubState()
    registry = RegistryState(
        tags={
            "ghcr.io/owner/repo:beta": DIGEST,
            f"ghcr.io/owner/repo:tested-beta-{SHA}": DIGEST,
            f"ghcr.io/owner/repo:release-{SHA}": DIGEST,
        },
        revisions={f"ghcr.io/owner/repo@{DIGEST}": SHA},
    )
    # Begin at the supported production origin rather than synthesizing an
    # internal validated record for the restore oracle.
    ReleasePromotion(github, registry, "owner/repo", 42, 1, "production memo").execute()
    registry.writes.clear()
    registry.tags["ghcr.io/owner/repo:release"] = OTHER
    github.calls.clear()

    outcome = ReleaseRestore(github, registry, "owner/repo").execute(TAG, "")
    repeated = ReleaseRestore(github, registry, "owner/repo").execute(TAG, "")

    assert outcome.release_tag == TAG and outcome.release_url.endswith(TAG)
    assert outcome.source_sha == SHA and outcome.digest == DIGEST and outcome.before_digest == OTHER
    assert registry.writes == [DIGEST]
    assert repeated.result == "verified no-op; distribution tag already selected"
    assert all(method == "GET" for method, _, _ in github.calls)


def test_legacy_history_restores_without_catalog_and_never_uses_tested_beta() -> None:
    github = GitHubState()
    registry = RegistryState(tags={f"ghcr.io/owner/repo:release-{SHA}": DIGEST, "ghcr.io/owner/repo:release": OTHER})
    outcome = ReleaseRestore(github, registry, "owner/repo").execute("", SHA)
    assert outcome.mode == "legacy release_sha" and registry.writes == [DIGEST]
    assert github.calls == []

    only_tested = RegistryState(tags={f"ghcr.io/owner/repo:tested-beta-{SHA}": DIGEST, "ghcr.io/owner/repo:release": OTHER})
    with pytest.raises(ReleaseRestoreError, match="absent or unavailable"):
        ReleaseRestore(github, only_tested, "owner/repo").execute("", SHA)
    assert only_tested.writes == []


@pytest.mark.parametrize(
    ("tag", "sha"),
    [("", ""), (TAG, SHA), ("missing", ""), ("https://github/release", ""), ("$(echo owned)", ""), ("", "abc"), ("", DIGEST)],
)
def test_invalid_or_ambiguous_selection_never_writes(tag: str, sha: str) -> None:
    registry = catalog_registry()
    with pytest.raises((ReleaseRestoreError, RuntimeError)):
        ReleaseRestore(published_catalog(), registry, "owner/repo").execute(tag, sha)
    assert registry.writes == []


@pytest.mark.parametrize("release_change", ["missing", "draft", "prerelease"])
def test_prepared_draft_and_prerelease_catalog_entries_are_ineligible(release_change: str) -> None:
    github = published_catalog()
    if release_change == "missing":
        github.releases.clear()
    else:
        github.releases[TAG][release_change] = True
    registry = catalog_registry()
    with pytest.raises(ReleaseRestoreError):
        ReleaseRestore(github, registry, "owner/repo").execute(TAG, "")
    assert registry.writes == []


@pytest.mark.parametrize("broken", ["history", "revision"])
def test_mismatched_registry_bindings_fail_before_write(broken: str) -> None:
    registry = catalog_registry()
    if broken == "history":
        registry.tags[f"ghcr.io/owner/repo:release-{SHA}"] = OTHER
    else:
        registry.revisions[f"ghcr.io/owner/repo@{DIGEST}"] = "d" * 40
    with pytest.raises(ReleaseRestoreError, match="does not match"):
        ReleaseRestore(published_catalog(), registry, "owner/repo").execute(TAG, "")
    assert registry.writes == []


def test_false_success_write_is_uncertain_and_never_compensated() -> None:
    registry = catalog_registry()
    registry.readback = OTHER
    with pytest.raises(ReleaseRestoreError, match="uncertain"):
        ReleaseRestore(published_catalog(), registry, "owner/repo").execute(TAG, "")
    assert registry.writes == [DIGEST]
