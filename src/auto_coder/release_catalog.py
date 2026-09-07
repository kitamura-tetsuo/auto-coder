"""Immutable release catalog records stored in annotated Git tags."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from .util.gh_cache import GitDataResponse, GitHubGitDataClient

SCHEMA_FIELDS = frozenset({"schema_version", "repository", "run_id", "requested_at", "release_tag", "source_sha", "image", "digest", "memo"})
REPOSITORY_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?/[a-z0-9](?:[a-z0-9_.-]{0,98}[a-z0-9])?")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TAG_PATTERN = re.compile(r"release-[0-9]{8}T[0-9]{6}JST-r[1-9][0-9]*")


class ReleaseCatalogError(RuntimeError):
    """The catalog could not be authoritatively read or prepared."""


class ReleaseCatalogConflict(ReleaseCatalogError):
    """An immutable tag is already bound to different or invalid data."""


@dataclass(frozen=True)
class ReleaseCatalogRecord:
    schema_version: int
    repository: str
    run_id: int
    requested_at: str
    release_tag: str
    source_sha: str
    image: str
    digest: str
    memo: str = ""

    def to_json(self) -> str:
        """Return the complete annotation in a deterministic UTF-8 JSON form."""
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseCatalogError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def expected_release_tag(requested_at: str, run_id: int) -> str:
    """Derive the stable JST tag from the original Actions run identity."""
    try:
        parsed = datetime.strptime(requested_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ReleaseCatalogError("requested_at must be a real UTC second in YYYY-MM-DDTHH:MM:SSZ form") from exc
    return f"release-{(parsed + timedelta(hours=9)).strftime('%Y%m%dT%H%M%S')}JST-r{run_id}"


def validate_record(record: ReleaseCatalogRecord, expected_repository: str | None = None) -> ReleaseCatalogRecord:
    """Validate every v1 field without normalization or inference."""
    if type(record.schema_version) is not int or record.schema_version != 1:
        raise ReleaseCatalogError("unsupported schema_version")
    if type(record.repository) is not str or REPOSITORY_PATTERN.fullmatch(record.repository) is None or record.repository != record.repository.lower():
        raise ReleaseCatalogError("repository must be a lowercase owner/repo identifier")
    if expected_repository is not None and record.repository != expected_repository:
        raise ReleaseCatalogError("catalog record belongs to a different repository")
    if type(record.run_id) is not int or isinstance(record.run_id, bool) or record.run_id <= 0:
        raise ReleaseCatalogError("run_id must be a positive integer")
    string_fields = (record.requested_at, record.release_tag, record.source_sha, record.image, record.digest, record.memo)
    if any(type(value) is not str for value in string_fields):
        raise ReleaseCatalogError("catalog string fields must be strings")
    if type(record.source_sha) is not str or SHA_PATTERN.fullmatch(record.source_sha) is None:
        raise ReleaseCatalogError("source_sha must be 40 lowercase hexadecimal characters")
    if type(record.digest) is not str or DIGEST_PATTERN.fullmatch(record.digest) is None:
        raise ReleaseCatalogError("digest must be sha256 followed by 64 lowercase hexadecimal characters")
    if record.image != f"ghcr.io/{record.repository}":
        raise ReleaseCatalogError("image does not match repository")
    if TAG_PATTERN.fullmatch(record.release_tag) is None or record.release_tag != expected_release_tag(record.requested_at, record.run_id):
        raise ReleaseCatalogError("release_tag does not match requested_at and run_id")
    return record


def parse_record(message: str, expected_repository: str) -> ReleaseCatalogRecord:
    """Parse a strict JSON annotation, rejecting duplicates and schema drift."""
    try:
        data = json.loads(message, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReleaseCatalogError("tag annotation is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != SCHEMA_FIELDS:
        raise ReleaseCatalogError("tag annotation must contain exactly the v1 fields")
    return validate_record(ReleaseCatalogRecord(**data), expected_repository)


def new_record(repository: str, run_id: int, requested_at: str, source_sha: str, digest: str, memo: str) -> ReleaseCatalogRecord:
    """Build and validate a proposed catalog record before any remote mutation."""
    record = ReleaseCatalogRecord(1, repository, run_id, requested_at, expected_release_tag(requested_at, run_id), source_sha, f"ghcr.io/{repository}", digest, memo)
    return validate_record(record, repository)


class ReleaseCatalog:
    """Fail-closed GitHub Git-data orchestration for immutable catalog refs."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        if REPOSITORY_PATTERN.fullmatch(repository) is None or repository != repository.lower():
            raise ReleaseCatalogError("repository must be a lowercase owner/repo identifier")
        self.client = client
        self.repository = repository

    def read(self, release_tag: str) -> ReleaseCatalogRecord:
        if TAG_PATTERN.fullmatch(release_tag) is None:
            raise ReleaseCatalogError("invalid release tag")
        ref = self.client.request("GET", f"git/ref/tags/{release_tag}")
        if ref.status == 404:
            raise ReleaseCatalogError("catalog ref is confirmed absent")
        data = self._require_object(ref, "exact ref")
        if data.get("ref") != f"refs/tags/{release_tag}":
            raise ReleaseCatalogError("GitHub returned a different ref")
        target = data.get("object")
        if not isinstance(target, dict) or target.get("type") != "tag" or SHA_PATTERN.fullmatch(target.get("sha", "")) is None:
            raise ReleaseCatalogError("catalog ref is not an annotated tag")
        annotation = self._require_object(self.client.request("GET", f"git/tags/{target['sha']}"), "annotated tag")
        obj = annotation.get("object")
        message = annotation.get("message")
        if annotation.get("tag") != release_tag or not isinstance(obj, dict) or obj.get("type") != "commit" or SHA_PATTERN.fullmatch(obj.get("sha", "")) is None or not isinstance(message, str):
            raise ReleaseCatalogError("annotated tag identity or direct commit target is invalid")
        record = parse_record(message, self.repository)
        if record.release_tag != release_tag or record.source_sha != obj["sha"]:
            raise ReleaseCatalogError("annotation does not match its tag name or target commit")
        return record

    def prepare(self, proposed: ReleaseCatalogRecord) -> ReleaseCatalogRecord:
        validate_record(proposed, self.repository)  # Must precede every mutation.
        observed = self.client.request("GET", f"git/ref/tags/{proposed.release_tag}")
        if observed.status != 404:
            return self._reuse_or_conflict(proposed, observed)
        tag_response = self.client.request("POST", "git/tags", {"tag": proposed.release_tag, "message": proposed.to_json(), "object": proposed.source_sha, "type": "commit"})
        tag_data = self._require_object(tag_response, "tag creation")
        tag_sha = tag_data.get("sha")
        if SHA_PATTERN.fullmatch(tag_sha if isinstance(tag_sha, str) else "") is None:
            raise ReleaseCatalogError("tag creation returned no trustworthy object identity")
        # POST /git/refs is create-only. Never update or delete a conflicting ref.
        try:
            self.client.request("POST", "git/refs", {"ref": f"refs/tags/{proposed.release_tag}", "sha": tag_sha})
        except RuntimeError:
            # The server may have accepted the create before the connection was
            # lost. Only the same authoritative read-back can establish success.
            pass
        return self._confirm_after_write(proposed)

    def _reuse_or_conflict(self, proposed: ReleaseCatalogRecord, response: GitDataResponse) -> ReleaseCatalogRecord:
        if response.status < 200 or response.status >= 300:
            raise ReleaseCatalogError(f"exact ref read unavailable (HTTP {response.status})")
        try:
            found = self.read(proposed.release_tag)
        except ReleaseCatalogError as exc:
            raise ReleaseCatalogConflict("existing catalog ref is invalid or different") from exc
        if found != proposed:
            raise ReleaseCatalogConflict("existing catalog record conflicts with proposal")
        return found

    def _confirm_after_write(self, proposed: ReleaseCatalogRecord) -> ReleaseCatalogRecord:
        try:
            found = self.read(proposed.release_tag)
        except ReleaseCatalogError as exc:
            raise ReleaseCatalogError("catalog write could not be confirmed by a fresh exact-ref read") from exc
        if found != proposed:
            raise ReleaseCatalogConflict("winning catalog binding conflicts with proposal")
        return found

    @staticmethod
    def _require_object(response: GitDataResponse, operation: str) -> dict[str, object]:
        if response.status < 200 or response.status >= 300 or not isinstance(response.data, dict):
            raise ReleaseCatalogError(f"{operation} unavailable or malformed (HTTP {response.status})")
        return response.data
