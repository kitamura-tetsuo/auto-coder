"""Create-only publication of an exact local beta smoke result."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient

from .beta_runtime_smoke import LocalSmokeEvidence

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_OID = re.compile(r"[0-9a-f]{40}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MAX_ANNOTATION_BYTES = 64 * 1024


class SourceAcceptanceError(RuntimeError):
    """Acceptance evidence is invalid, conflicting, or unavailable."""


@dataclass(frozen=True)
class PublishedSourceAcceptance:
    tag_ref: str
    tag_object_oid: str
    direct_commit_sha: str
    annotation: str


def tag_name(operation_id: str) -> str:
    if not isinstance(operation_id, str) or _OPERATION.fullmatch(operation_id) is None:
        raise SourceAcceptanceError("operation ID is invalid")
    return f"auto-coder-beta-acceptance-{operation_id}"


def annotation_from_local(evidence: LocalSmokeEvidence, repository: str) -> str:
    """Promote the exact producer content without accepting caller verdicts."""
    try:
        local = evidence.document()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SourceAcceptanceError("local evidence is invalid") from exc
    capture = local.get("capture")
    source = capture.get("source") if isinstance(capture, dict) else None
    required = {
        "schema_version": 1,
        "record_type": "beta-runtime-smoke",
        "repository": repository,
        "local_result": "PASS",
        "local_evidence_id": evidence.local_evidence_id,
        "content_sha256": evidence.content_sha256,
    }
    if _REPOSITORY.fullmatch(repository) is None or any(local.get(key) != value for key, value in required.items() if key not in {"content_sha256"}):
        raise SourceAcceptanceError("local evidence is not an exact PASS for the configured repository")
    if not isinstance(source, dict) or source.get("repository") != repository or source.get("source_sha") != local.get("source_sha"):
        raise SourceAcceptanceError("local source-selection binding is unavailable")
    if not isinstance(local.get("operation_id"), str) or tag_name(local["operation_id"]) == "":
        raise SourceAcceptanceError("local operation binding is invalid")
    record = {
        "schema_version": 1,
        "record_type": "beta-source-acceptance",
        "operation_id": local["operation_id"],
        "repository": repository,
        "source_sha": local["source_sha"],
        "local_result": "PASS",
        "local_evidence_id": evidence.local_evidence_id,
        "local_evidence_content_sha256": evidence.content_sha256,
        "local_evidence": local,
    }
    value = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(value.encode()) > MAX_ANNOTATION_BYTES:
        raise SourceAcceptanceError("acceptance annotation is too large")
    return value


class SourceAcceptancePublisher:
    """Publish and independently read back one immutable annotated Git tag."""

    def __init__(self, client: GitHubGitDataClient, repository: str) -> None:
        if _REPOSITORY.fullmatch(repository) is None:
            raise SourceAcceptanceError("repository is invalid")
        self.client, self.repository = client, repository

    @staticmethod
    def _object(response: GitDataResponse, purpose: str) -> dict[str, object]:
        if response.status != 200 or not isinstance(response.data, dict):
            raise SourceAcceptanceError(f"{purpose} unavailable")
        return response.data

    def read(self, operation_id: str) -> PublishedSourceAcceptance | None:
        name = tag_name(operation_id)
        response = self.client.request("GET", f"git/ref/tags/{name}")
        if response.status == 404:
            return None
        ref = self._object(response, "acceptance ref readback")
        target = ref.get("object")
        if ref.get("ref") != f"refs/tags/{name}" or not isinstance(target, dict) or target.get("type") != "tag" or not isinstance(target.get("sha"), str) or _OID.fullmatch(target["sha"]) is None:
            raise SourceAcceptanceError("acceptance ref is not an exact annotated tag")
        oid = target["sha"]
        tag = self._object(self.client.request("GET", f"git/tags/{oid}"), "acceptance tag-object readback")
        direct, annotation = tag.get("object"), tag.get("message")
        if tag.get("tag") != name or not isinstance(direct, dict) or direct.get("type") != "commit" or not isinstance(direct.get("sha"), str) or _SHA.fullmatch(direct["sha"]) is None or not isinstance(annotation, str):
            raise SourceAcceptanceError("acceptance tag object is malformed")
        try:
            record = json.loads(annotation)
        except json.JSONDecodeError as exc:
            raise SourceAcceptanceError("acceptance annotation is invalid") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("record_type") != "beta-source-acceptance"
            or record.get("operation_id") != operation_id
            or record.get("repository") != self.repository
            or record.get("source_sha") != direct["sha"]
            or record.get("local_result") != "PASS"
            or "PASS_PUBLISHED" in record
            or json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False) != annotation
        ):
            raise SourceAcceptanceError("acceptance annotation conflicts with its ref or target")
        return PublishedSourceAcceptance(f"refs/tags/{name}", oid, direct["sha"], annotation)

    def publish(self, evidence: LocalSmokeEvidence, tagger_date: str) -> PublishedSourceAcceptance:
        annotation = annotation_from_local(evidence, self.repository)
        record = json.loads(annotation)
        operation_id, source_sha = record["operation_id"], record["source_sha"]
        existing = self.read(operation_id)
        if existing is not None:
            if existing.annotation != annotation or existing.direct_commit_sha != source_sha:
                raise SourceAcceptanceError("existing acceptance record conflicts")
            return existing
        name = tag_name(operation_id)
        created = self.client.request("POST", "git/tags", {"tag": name, "message": annotation, "object": source_sha, "type": "commit", "tagger": {"name": "auto-coder", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": tagger_date}})
        if created.status != 201 or not isinstance(created.data, dict) or not isinstance(created.data.get("sha"), str) or _OID.fullmatch(created.data["sha"]) is None:
            raise SourceAcceptanceError("tag-object creation is unavailable; publication is uncertain")
        oid = created.data["sha"]
        try:
            response = self.client.request("POST", "git/refs", {"ref": f"refs/tags/{name}", "sha": oid})
            if response.status not in {201, 422}:
                raise SourceAcceptanceError("ref creation is unavailable; publication is uncertain")
        except RuntimeError:
            pass
        observed = self.read(operation_id)
        if observed is None:
            raise SourceAcceptanceError("acceptance ref is not observable; publication is uncertain")
        if observed.tag_object_oid != oid or observed.annotation != annotation or observed.direct_commit_sha != source_sha:
            raise SourceAcceptanceError("acceptance readback conflicts with the created object")
        return observed
