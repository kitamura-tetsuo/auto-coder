"""Conservative, replay-safe promotion and GitHub Release publication."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .release_catalog import ReleaseCatalog, ReleaseCatalogError, ReleaseCatalogRecord, expected_release_tag, new_record
from .util.gh_cache import GitDataResponse, GitHubGitDataClient


class ReleasePromotionError(RuntimeError):
    """Promotion cannot be authoritatively completed."""


@dataclass(frozen=True)
class PromotionOutcome:
    record: ReleaseCatalogRecord
    result: str
    release_url: str
    channel_before: str = "unavailable"


class Registry:
    """The small GHCR command boundary used by promotion."""

    def inspect_digest(self, reference: str) -> str:
        command = ["python", "scripts/deployment_artifacts.py", "inspect-digest", reference]
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()

    def inspect_revision(self, reference: str) -> str:
        command = ["docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Image}}"]
        output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
        try:
            revision = json.loads(output)["config"]["Labels"]["org.opencontainers.image.revision"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ReleasePromotionError("immutable image has no trustworthy source revision") from exc
        if not isinstance(revision, str):
            raise ReleasePromotionError("immutable image has no trustworthy source revision")
        self._validate_sha(revision)
        return revision

    def require_equal(self, expected: str, actual: str, evidence: str) -> None:
        if expected != actual:
            raise ReleasePromotionError(f"{evidence} digest mismatch")

    def ensure_history(self, image: str, source_sha: str, digest: str) -> None:
        history = f"{image}:release-{source_sha}"
        try:
            found = self.inspect_digest(history)
        except subprocess.CalledProcessError as exc:
            # deployment_artifacts reserves status 3 for an authoritative absence.
            if exc.returncode != 3:
                raise ReleasePromotionError("release history unavailable") from exc
            subprocess.run(["python", "scripts/deployment_artifacts.py", "create-history-if-absent", history, digest], check=True)
            found = self.inspect_digest(history)
        self.require_equal(digest, found, "immutable release history")

    def set_release(self, image: str, digest: str) -> None:
        subprocess.run(
            ["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", f"{image}:release", f"{image}@{digest}"],
            check=True,
        )

    @staticmethod
    def _validate_sha(value: str) -> None:
        import re

        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ReleasePromotionError("source revision must be 40 lowercase hexadecimal characters")


class ReleasePromotion:
    """Resolve one Actions operation once, then reconcile all repeated attempts."""

    def __init__(self, client: GitHubGitDataClient, registry: Registry, repository: str, run_id: int, run_attempt: int, memo: str) -> None:
        self.client = client
        self.registry = registry
        self.repository = repository
        self.run_id = run_id
        self.run_attempt = run_attempt
        self.memo = memo
        self.image = f"ghcr.io/{repository}"
        self.catalog = ReleaseCatalog(client, repository)

    def execute(self) -> PromotionOutcome:
        requested_at = self._requested_at()
        tag = expected_release_tag(requested_at, self.run_id)
        response = self.client.request("GET", f"git/ref/tags/{tag}")
        if response.status == 404:
            if self.run_attempt != 1:
                raise ReleasePromotionError("re-run has no durable operation record; start a new workflow dispatch")
            return self._initial(requested_at)
        if not 200 <= response.status < 300:
            raise ReleasePromotionError("operation record availability could not be established")
        record = self.catalog.read(tag)
        return self._reconcile(record)

    def _initial(self, requested_at: str) -> PromotionOutcome:
        digest = self.registry.inspect_digest(f"{self.image}:beta")
        # Metadata is deliberately read through the selected immutable digest.
        source_sha = self.registry.inspect_revision(f"{self.image}@{digest}")
        tested = self.registry.inspect_digest(f"{self.image}:tested-beta-{source_sha}")
        self.registry.require_equal(digest, tested, "tested beta")
        self.registry.ensure_history(self.image, source_sha, digest)
        record = self.catalog.prepare(new_record(self.repository, self.run_id, requested_at, source_sha, digest, self.memo))
        before = self._channel_or_unavailable()
        try:
            self.registry.set_release(self.image, digest)
            self.registry.require_equal(digest, self.registry.inspect_digest(f"{self.image}:release"), "release postcondition")
        except Exception as exc:
            raise ReleasePromotionError("release channel changed or may have changed, but its postcondition is unverified") from exc
        # All three authorities are freshly re-read after the mutable write.
        self._validate_bindings(record, require_current=True)
        release = self._publish_or_confirm(record)
        return PromotionOutcome(record, "published", release, before)

    def _reconcile(self, record: ReleaseCatalogRecord) -> PromotionOutcome:
        release = self._read_release(record, absent_ok=True)
        if release is not None:
            return PromotionOutcome(record, "already catalogued", release)
        # Prepared recovery is publication-only; it can never move the channel.
        self._validate_bindings(record, require_current=True)
        release = self._publish_or_confirm(record)
        return PromotionOutcome(record, "publication recovered", release, record.digest)

    def _validate_bindings(self, record: ReleaseCatalogRecord, require_current: bool) -> None:
        if self.catalog.read(record.release_tag) != record:
            raise ReleasePromotionError("operation record changed")
        history = f"{self.image}:release-{record.source_sha}"
        self.registry.require_equal(record.digest, self.registry.inspect_digest(history), "release history")
        if self.registry.inspect_revision(f"{self.image}@{record.digest}") != record.source_sha:
            raise ReleasePromotionError("image revision no longer matches the prepared record")
        if require_current:
            try:
                current = self.registry.inspect_digest(f"{self.image}:release")
            except Exception as exc:
                raise ReleasePromotionError("prepared publication is incomplete; current release is unavailable") from exc
            if current != record.digest:
                raise ReleasePromotionError("prepared publication was superseded; start a new deliberate dispatch")

    def _publish_or_confirm(self, record: ReleaseCatalogRecord) -> str:
        payload = {"tag_name": record.release_tag, "name": self._title(record), "body": self._body(record), "draft": False, "prerelease": False}
        try:
            response = self.client.request("POST", "releases", payload)
            if 200 <= response.status < 300:
                return self._validate_release_response(response, record)
        except RuntimeError:
            pass
        found = self._read_release(record, absent_ok=True)
        if found is None:
            raise ReleasePromotionError("release channel changed, but GitHub Release publication is unconfirmed")
        return found

    def _read_release(self, record: ReleaseCatalogRecord, absent_ok: bool) -> str | None:
        response = self.client.request("GET", f"releases/tags/{record.release_tag}")
        if response.status == 404 and absent_ok:
            return None
        return self._validate_release_response(response, record)

    def _validate_release_response(self, response: GitDataResponse, record: ReleaseCatalogRecord) -> str:
        data = response.data
        if not 200 <= response.status < 300 or not isinstance(data, dict):
            raise ReleasePromotionError("exact GitHub Release is unavailable")
        if data.get("tag_name") != record.release_tag or data.get("draft") is not False or data.get("prerelease") is not False:
            raise ReleasePromotionError("conflicting GitHub Release exists for the operation tag")
        url = data.get("html_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ReleasePromotionError("GitHub Release response has no trustworthy URL")
        # The immutable tag, not editable prose or target_commitish, is authority.
        if self.catalog.read(record.release_tag) != record:
            raise ReleasePromotionError("GitHub Release has no matching immutable catalog binding")
        return url

    def _requested_at(self) -> str:
        response = self.client.request("GET", f"actions/runs/{self.run_id}")
        data = response.data
        value = data.get("created_at") if 200 <= response.status < 300 and isinstance(data, dict) else None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError as exc:
            raise ReleasePromotionError("original Actions run created_at is unavailable or malformed") from exc
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _channel_or_unavailable(self) -> str:
        try:
            return self.registry.inspect_digest(f"{self.image}:release")
        except Exception:
            return "unavailable"

    def _title(self, record: ReleaseCatalogRecord) -> str:
        return f"Release requested {record.release_tag.removeprefix('release-').split('-r', 1)[0]}"

    def _body(self, record: ReleaseCatalogRecord) -> str:
        run_url = f"https://github.com/{record.repository}/actions/runs/{record.run_id}"
        return "\n".join(
            [
                "## Promotion catalog entry",
                "This is historical catalog membership, not a current container-deployment monitor.",
                f"Promotion requested (JST): `{record.release_tag.split('-r', 1)[0].removeprefix('release-')}`",
                "GitHub shows publication time separately from the original request time.",
                f"Source SHA: `{record.source_sha}`",
                f"Copyable catalog tag: `{record.release_tag}`",
                f"Image digest: `{record.digest}`",
                f"Immutable image: `{record.image}:release-{record.source_sha}`",
                f"Original Actions run: {run_url}",
                "",
                "## Operator memo",
                record.memo,
            ]
        )


def write_summary(outcome: PromotionOutcome) -> None:
    """Write non-authoritative operator diagnostics to the Actions summary."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    record = outcome.record
    Path(path).write_text(
        f"## Promote Release: {outcome.result}\n\n- Operation tag: `{record.release_tag}`\n- Source SHA: `{record.source_sha}`\n- Digest: `{record.digest}`\n- Observed pre-change/current channel: `{outcome.channel_before}`\n- GitHub Release: {outcome.release_url}\n",
        encoding="utf-8",
    )
