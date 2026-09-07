"""Fail-closed restoration of the mutable release distribution tag."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .release_catalog import DIGEST_PATTERN, SHA_PATTERN, ReleaseCatalog, ReleaseCatalogRecord
from .release_promotion import Registry
from .util.gh_cache import GitDataResponse, GitHubGitDataClient


class ReleaseRestoreError(RuntimeError):
    """A restore target or its postcondition could not be established."""


@dataclass(frozen=True)
class RestoreOutcome:
    mode: str
    source_sha: str
    digest: str
    before_digest: str
    result: str
    release_tag: str = ""
    release_url: str = ""


class ReleaseRestore:
    """Validate one explicit target, pin its digest, and move only ``:release``."""

    def __init__(self, client: GitHubGitDataClient, registry: Registry, repository: str) -> None:
        self.client = client
        self.registry = registry
        self.repository = repository
        self.image = f"ghcr.io/{repository}"
        self.catalog = ReleaseCatalog(client, repository)

    def execute(self, release_tag: str, release_sha: str) -> RestoreOutcome:
        if bool(release_tag) == bool(release_sha):
            raise ReleaseRestoreError("exactly one of release_tag and release_sha must be nonempty")
        if release_tag:
            record, release_url = self._catalog_target(release_tag)
            mode = "catalog release_tag"
        else:
            if SHA_PATTERN.fullmatch(release_sha) is None:
                raise ReleaseRestoreError("release_sha must be exactly 40 lowercase hexadecimal characters")
            digest = self._inspect_digest(f"{self.image}:release-{release_sha}", "legacy release history")
            record = ReleaseCatalogRecord(1, self.repository, 1, "1970-01-01T00:00:00Z", "", release_sha, self.image, digest, "")
            release_url = ""
            mode = "legacy release_sha"

        before = self._optional_current()
        if before == record.digest:
            result = "verified no-op; distribution tag already selected"
        else:
            try:
                self.registry.set_release(self.image, record.digest)
            except Exception as exc:
                raise ReleaseRestoreError("release distribution tag may have changed; write outcome is uncertain") from exc
            result = "restored distribution tag"
        try:
            observed = self.registry.inspect_digest(f"{self.image}:release")
        except Exception as exc:
            raise ReleaseRestoreError("release distribution tag post-write observation is unavailable; outcome is uncertain") from exc
        if observed != record.digest:
            raise ReleaseRestoreError(f"release distribution tag postcondition differs from pinned digest; outcome is uncertain ({observed!r})")
        return RestoreOutcome(mode, record.source_sha, record.digest, before, result, record.release_tag, release_url)

    def _catalog_target(self, release_tag: str) -> tuple[ReleaseCatalogRecord, str]:
        record = self.catalog.read(release_tag)
        release = self._eligible_release(release_tag)
        # Re-read the immutable binding after the independently editable Release.
        if self.catalog.read(release_tag) != record:
            raise ReleaseRestoreError("catalog binding changed while it was being validated")
        history = self._inspect_digest(f"{self.image}:release-{record.source_sha}", "catalog release history")
        if history != record.digest:
            raise ReleaseRestoreError("catalog release history digest does not match its annotation")
        try:
            revision = self.registry.inspect_revision(f"{self.image}@{record.digest}")
        except Exception as exc:
            raise ReleaseRestoreError("immutable image source revision is unavailable") from exc
        if revision != record.source_sha:
            raise ReleaseRestoreError("immutable image source revision does not match its annotation")
        url = release.data.get("html_url") if isinstance(release.data, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ReleaseRestoreError("eligible GitHub Release has no trustworthy URL")
        return record, url

    def _eligible_release(self, release_tag: str) -> GitDataResponse:
        response = self.client.request("GET", f"releases/tags/{release_tag}")
        data = response.data
        if not 200 <= response.status < 300 or not isinstance(data, dict):
            raise ReleaseRestoreError("exact published GitHub Release is unavailable")
        if data.get("tag_name") != release_tag or data.get("draft") is not False or data.get("prerelease") is not False:
            raise ReleaseRestoreError("GitHub Release is draft, prerelease, or belongs to a different tag")
        return response

    def _inspect_digest(self, reference: str, evidence: str) -> str:
        try:
            digest = self.registry.inspect_digest(reference)
        except Exception as exc:
            raise ReleaseRestoreError(f"{evidence} is absent or unavailable") from exc
        if DIGEST_PATTERN.fullmatch(digest) is None:
            raise ReleaseRestoreError(f"{evidence} returned a malformed digest")
        return digest

    def _optional_current(self) -> str:
        try:
            value = self.registry.inspect_digest(f"{self.image}:release")
            return value if DIGEST_PATTERN.fullmatch(value) else "unavailable"
        except Exception:
            return "unavailable"


def write_restore_summary(outcome: RestoreOutcome) -> None:
    """Write diagnostics without claiming that any running container updated."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    catalog = ""
    if outcome.release_tag:
        catalog = f"- Selected tag: `{outcome.release_tag}`\n- GitHub Release: {outcome.release_url}\n"
    Path(path).write_text(
        f"## Restore Release: confirmed\n\n- Input mode: `{outcome.mode}`\n{catalog}"
        f"- Source SHA: `{outcome.source_sha}`\n- Pinned target digest: `{outcome.digest}`\n"
        f"- Observed current/before digest: `{outcome.before_digest}`\n- Outcome: {outcome.result}\n\n"
        "This confirms only the GHCR `:release` distribution tag; it does not confirm a container update or restart.\n",
        encoding="utf-8",
    )


def write_restore_failure_summary(message: str) -> None:
    """Make an unverified/uncertain result explicit without claiming rollback."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        Path(path).write_text(
            "## Restore Release: not confirmed\n\n" f"- Outcome: `{message}`\n\nNo successful distribution-tag restoration or container update is confirmed.\n",
            encoding="utf-8",
        )
