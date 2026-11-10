"""Version-2 Git-source release records, independent of historical OCI catalogs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .management.beta_runtime_smoke import PROFILE_ID
from .tested_source_refs import WORKFLOW_PATH
from .util.gh_cache import GitDataResponse, GitHubGitDataClient

SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
OPERATION = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
RELEASE_WORKFLOW = ".github/workflows/promote-accepted-source-release.yml"
RELEASE_SELECTION_REF = "refs/tags/auto-coder-release-source-selection-current"
MAX_RECORD_BYTES = 16 * 1024


class SourceReleaseError(RuntimeError):
    """A source release cannot be proven eligible or safely completed."""


class SourceReleaseConflict(SourceReleaseError):
    """An immutable identity is already bound to something else."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SourceReleaseError("duplicate JSON field")
        result[key] = value
    return result


def _json(content: str, limit: int) -> object:
    if len(content.encode("utf-8")) > limit:
        raise SourceReleaseError("evidence exceeds its size limit")
    try:
        return json.loads(content, object_pairs_hook=_unique_pairs)
    except (TypeError, ValueError) as exc:
        raise SourceReleaseError("evidence is not strict JSON") from exc


def _object(response: GitDataResponse, purpose: str, status: int = 200) -> dict[str, object]:
    if response.status != status or not isinstance(response.data, dict):
        raise SourceReleaseError(f"{purpose} unavailable (HTTP {response.status})")
    return response.data


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise SourceReleaseError(f"{field} must be a full lowercase Git SHA")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or DIGEST.fullmatch(value) is None:
        raise SourceReleaseError(f"{field} must be a lowercase SHA-256")
    return value


def release_tag(operation_id: str) -> str:
    if not isinstance(operation_id, str) or OPERATION.fullmatch(operation_id) is None:
        raise SourceReleaseError("operation ID is not a bounded Git-ref-safe identifier")
    return f"auto-coder-source-release-{operation_id}"


@dataclass(frozen=True)
class AllowedProfile:
    profile_id: str
    content_revision: str


@dataclass(frozen=True)
class SourceReleasePolicy:
    schema_version: int
    source_repository: str
    release_target: str
    release_ref: str
    accepted_beta_instance_ids: tuple[str, ...]
    accepted_profiles: tuple[AllowedProfile, ...]
    maximum_evidence_age_seconds: int
    source_test_workflow_path: str
    promotion_workflow_path: str
    release_branch: str

    @property
    def revision(self) -> str:
        return digest(asdict(self))

    @classmethod
    def load(cls, path: Path) -> SourceReleasePolicy:
        try:
            document = _json(path.read_text(encoding="utf-8"), 8192)
        except OSError as exc:
            raise SourceReleaseError("operator-owned promotion policy is unavailable") from exc
        return cls.from_document(document)

    @classmethod
    def from_document(cls, document: object) -> SourceReleasePolicy:
        """Validate a persisted original policy using the same strict parser."""
        if not isinstance(document, dict) or set(document) != set(cls.__dataclass_fields__):
            raise SourceReleaseError("operator-owned promotion policy has unsupported fields")
        raw_profiles = document.get("accepted_profiles")
        raw_instances = document.get("accepted_beta_instance_ids")
        if not isinstance(raw_profiles, list) or not isinstance(raw_instances, list):
            raise SourceReleaseError("operator-owned promotion allowlists are missing")
        profiles: list[AllowedProfile] = []
        for item in raw_profiles:
            if not isinstance(item, dict) or set(item) != {"profile_id", "content_revision"}:
                raise SourceReleaseError("promotion profile allowlist is malformed")
            profiles.append(AllowedProfile(item["profile_id"], item["content_revision"]))
        policy = cls(
            document["schema_version"],
            document["source_repository"],
            document["release_target"],
            document["release_ref"],
            tuple(raw_instances),
            tuple(profiles),
            document["maximum_evidence_age_seconds"],
            document["source_test_workflow_path"],
            document["promotion_workflow_path"],
            document["release_branch"],
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise SourceReleaseError("unsupported promotion policy version")
        if not isinstance(self.source_repository, str) or REPOSITORY.fullmatch(self.source_repository) is None:
            raise SourceReleaseError("trusted source repository is invalid")
        if self.release_target != "release" or self.release_ref != "refs/tags/auto-coder-release-source-current":
            raise SourceReleaseError("unrecognized source release target/ref namespace")
        if self.source_test_workflow_path != WORKFLOW_PATH or self.promotion_workflow_path != RELEASE_WORKFLOW or self.release_branch != "main":
            raise SourceReleaseError("unsupported trusted Actions identity or release branch")
        if type(self.maximum_evidence_age_seconds) is not int or not 1 <= self.maximum_evidence_age_seconds <= 604800:
            raise SourceReleaseError("promotion evidence maximum age must be finite and bounded")
        if not 1 <= len(self.accepted_beta_instance_ids) <= 16 or any(not isinstance(i, str) or INSTANCE.fullmatch(i) is None for i in self.accepted_beta_instance_ids) or len(set(self.accepted_beta_instance_ids)) != len(self.accepted_beta_instance_ids):
            raise SourceReleaseError("invalid accepted beta instance allowlist")
        if not 1 <= len(self.accepted_profiles) <= 16 or len(set(self.accepted_profiles)) != len(self.accepted_profiles):
            raise SourceReleaseError("invalid accepted profile allowlist")
        for profile in self.accepted_profiles:
            if profile.profile_id != PROFILE_ID:
                raise SourceReleaseError("unrecognized runtime acceptance profile")
            _digest(profile.content_revision, "accepted profile revision")


@dataclass(frozen=True)
class SourceReleaseCandidate:
    """Exact immutable candidate, before an Actions dispatch identity exists."""

    operation_id: str
    source_repository: str
    source_sha: str
    ci_run_id: int
    ci_run_attempt: int
    tested_source_ref: str
    tested_source_oid: str
    tested_source_record_sha256: str
    acceptance_ref: str
    acceptance_oid: str
    release_target: str

    def validate(self, policy: SourceReleasePolicy) -> None:
        release_tag(self.operation_id)
        _sha(self.source_sha, "candidate")
        _sha(self.tested_source_oid, "tested source object")
        _sha(self.acceptance_oid, "acceptance object")
        _digest(self.tested_source_record_sha256, "tested source record digest")
        if self.source_repository != policy.source_repository or self.release_target != policy.release_target:
            raise SourceReleaseError("requested repository or release target is not operator-authorized")
        if self.tested_source_ref != f"refs/tags/auto-coder-tested-{self.source_sha}":
            raise SourceReleaseError("tested source ref does not name the exact candidate")
        if not self.acceptance_ref.startswith("refs/tags/auto-coder-beta-acceptance-"):
            raise SourceReleaseError("acceptance ref is outside its immutable namespace")
        name = self.acceptance_ref.removeprefix("refs/tags/auto-coder-beta-acceptance-")
        if not name or len(name) > 128 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None:
            raise SourceReleaseError("acceptance operation identity is invalid")
        if any(type(value) is not int or value <= 0 for value in (self.ci_run_id, self.ci_run_attempt)):
            raise SourceReleaseError("original CI run ID and attempt must be positive integers")


@dataclass(frozen=True)
class SourceReleaseRequest(SourceReleaseCandidate):
    promotion_run_id: int
    promotion_run_attempt: int

    def validate(self, policy: SourceReleasePolicy) -> None:
        super().validate(policy)
        if any(type(value) is not int or value <= 0 for value in (self.promotion_run_id, self.promotion_run_attempt)):
            raise SourceReleaseError("original Actions run IDs and attempts must be positive integers")


@dataclass(frozen=True)
class SourceReleaseRecord:
    schema_version: int
    record_type: str
    operation_id: str
    source_repository: str
    source_sha: str
    ci_run_id: int
    ci_run_attempt: int
    tested_source_ref: str
    tested_source_oid: str
    tested_source_record_sha256: str
    acceptance_ref: str
    acceptance_oid: str
    local_evidence_id: str
    local_evidence_content_sha256: str
    beta_instance_id: str
    acceptance_profile_id: str
    acceptance_profile_revision: str
    policy_revision: str
    release_target: str
    release_ref: str
    expected_previous_sha: str | None
    promotion_run_id: int
    original_promotion_run_attempt: int
    promotion_workflow_path: str

    @property
    def tag(self) -> str:
        return release_tag(self.operation_id)

    def serialize(self) -> str:
        return canonical(asdict(self))

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2 or self.record_type != "git-source-release":
            raise SourceReleaseError("unsupported source-release catalog schema")
        if not isinstance(self.source_repository, str) or REPOSITORY.fullmatch(self.source_repository) is None:
            raise SourceReleaseError("catalog repository is invalid")
        release_tag(self.operation_id)
        for field in ("source_sha", "tested_source_oid", "acceptance_oid"):
            _sha(getattr(self, field), field)
        for field in ("tested_source_record_sha256", "local_evidence_content_sha256", "acceptance_profile_revision", "policy_revision"):
            _digest(getattr(self, field), field)
        for field in ("ci_run_id", "ci_run_attempt", "promotion_run_id", "original_promotion_run_attempt"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise SourceReleaseError(f"catalog {field} is invalid")
        if self.expected_previous_sha is not None:
            _sha(self.expected_previous_sha, "expected previous source")
        if self.tested_source_ref != f"refs/tags/auto-coder-tested-{self.source_sha}" or not self.acceptance_ref.startswith("refs/tags/auto-coder-beta-acceptance-"):
            raise SourceReleaseError("catalog evidence ref is invalid")
        if self.local_evidence_id != f"beta-runtime-smoke-v1:{self.acceptance_ref.removeprefix('refs/tags/auto-coder-beta-acceptance-')}":
            raise SourceReleaseError("catalog local evidence ID differs from acceptance")
        if not isinstance(self.beta_instance_id, str) or INSTANCE.fullmatch(self.beta_instance_id) is None or self.acceptance_profile_id != PROFILE_ID:
            raise SourceReleaseError("catalog acceptance profile or beta instance is invalid")
        if self.release_target != "release" or self.release_ref != "refs/tags/auto-coder-release-source-current" or self.promotion_workflow_path != RELEASE_WORKFLOW:
            raise SourceReleaseError("catalog release destination or Actions identity is invalid")
        if len(self.serialize().encode("utf-8")) > MAX_RECORD_BYTES:
            raise SourceReleaseError("source-release catalog exceeds size limit")


@dataclass(frozen=True)
class CatalogReadback:
    record: SourceReleaseRecord
    tag_object_oid: str


class SourceReleaseCatalog:
    """Read/create one immutable annotated source tag; no OCI catalog writes."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        self.client, self.repository = client, repository

    def read(self, operation_id: str) -> CatalogReadback | None:
        name = release_tag(operation_id)
        ref = self.client.request("GET", f"git/ref/tags/{name}")
        if ref.status == 404:
            return None
        data = _object(ref, "source release catalog ref")
        target = data.get("object")
        if data.get("ref") != f"refs/tags/{name}" or not isinstance(target, dict) or target.get("type") != "tag":
            raise SourceReleaseConflict("source catalog ref is not an exact annotated tag")
        oid = _sha(target.get("sha"), "catalog tag object")
        tag = _object(self.client.request("GET", f"git/tags/{oid}"), "source catalog annotation")
        direct, message = tag.get("object"), tag.get("message")
        if tag.get("tag") != name or not isinstance(direct, dict) or direct.get("type") != "commit" or not isinstance(message, str):
            raise SourceReleaseConflict("source catalog object is invalid")
        parsed = _json(message, MAX_RECORD_BYTES)
        if not isinstance(parsed, dict) or set(parsed) != set(SourceReleaseRecord.__dataclass_fields__):
            raise SourceReleaseConflict("source catalog has unknown or missing fields")
        record = SourceReleaseRecord(**parsed)
        record.validate()
        if record.operation_id != operation_id or record.source_repository != self.repository or record.source_sha != _sha(direct.get("sha"), "catalog direct commit") or record.serialize() != message:
            raise SourceReleaseConflict("source catalog identity or canonical annotation differs")
        return CatalogReadback(record, oid)

    def prepare(self, record: SourceReleaseRecord, *, require_new: bool = False) -> CatalogReadback:
        record.validate()
        if record.source_repository != self.repository:
            raise SourceReleaseError("catalog source repository differs")
        existing = self.read(record.operation_id)
        if existing is not None:
            if existing.record != record:
                raise SourceReleaseConflict("operation already has a different immutable catalog")
            if require_new:
                raise SourceReleaseConflict("immutable catalog appeared after first-attempt admission; pointer effect not owned")
            return existing
        created = _object(self.client.request("POST", "git/tags", {"tag": record.tag, "message": record.serialize(), "object": record.source_sha, "type": "commit"}), "catalog tag creation", 201)
        oid = _sha(created.get("sha"), "created catalog object")
        try:
            self.client.request("POST", "git/refs", {"ref": f"refs/tags/{record.tag}", "sha": oid})
        except RuntimeError:
            pass
        observed = self.read(record.operation_id)
        if observed is None:
            raise SourceReleaseError("catalog publication outcome is uncertain; ref is absent")
        if observed.record != record or observed.tag_object_oid != oid:
            raise SourceReleaseConflict("catalog ref was won by a conflicting object")
        return observed
