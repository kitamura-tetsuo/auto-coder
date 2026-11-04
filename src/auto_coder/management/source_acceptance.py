"""Create-only publication of an exact local beta smoke result."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass

from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient

from .action_scopes import DEPLOY_SCOPE, READ_SCOPE
from .action_scopes import AcceptedAuthority as GrantAuthority
from .action_scopes import PendingAuthorityStatus
from .beta_runtime_smoke import BetaRuntimeSmokeProducer, LocalResult, LocalSmokeEvidence, SmokeState
from .operations import EffectReceipt, EffectState, MutationStatus, OperationPhase, OperationRecord

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
    if hashlib.sha256(evidence.content.encode("utf-8")).hexdigest() != evidence.content_sha256:
        raise SourceAcceptanceError("local evidence content digest differs")
    if _REPOSITORY.fullmatch(repository) is None or any(local.get(key) != value for key, value in required.items() if key != "content_sha256"):
        raise SourceAcceptanceError("local evidence is not an exact PASS for the configured repository")
    if not isinstance(source, dict) or source.get("repository") != repository or source.get("source_sha") != local.get("source_sha"):
        raise SourceAcceptanceError("local source-selection binding is unavailable")
    operation_id = local.get("operation_id")
    if not isinstance(operation_id, str):
        raise SourceAcceptanceError("local operation binding is invalid")
    tag_name(operation_id)
    record = {
        "schema_version": 1,
        "record_type": "beta-source-acceptance",
        "operation_id": operation_id,
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
        local = record.get("local_evidence") if isinstance(record, dict) else None
        local_id = record.get("local_evidence_id") if isinstance(record, dict) else None
        local_digest = record.get("local_evidence_content_sha256") if isinstance(record, dict) else None
        if not isinstance(local, dict) or not isinstance(local_id, str) or not isinstance(local_digest, str):
            raise SourceAcceptanceError("acceptance local evidence is missing")
        local_content = json.dumps(local, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        expected_annotation = annotation_from_local(LocalSmokeEvidence(local_id, local_digest, local_content), self.repository)
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("record_type") != "beta-source-acceptance"
            or record.get("operation_id") != operation_id
            or record.get("repository") != self.repository
            or record.get("source_sha") != direct["sha"]
            or record.get("local_result") != "PASS"
            or "PASS_PUBLISHED" in record
            or expected_annotation != annotation
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


class BetaAcceptanceHandler:
    """Drive the single durable producer and publish only its exact PASS result."""

    def __init__(self, runtime: object, producer: BetaRuntimeSmokeProducer, publisher: SourceAcceptancePublisher) -> None:
        self.runtime = runtime
        self.producer = producer
        self.publisher = publisher

    def _hold(self, record: OperationRecord, reason: str) -> OperationRecord:
        journal = self.producer.journal
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            return record
        changed = journal.transition(record.operation_id, record.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category=reason)
        return changed.record or record

    def _authority_refusal(self, record: OperationRecord) -> str | None:
        authority = record.authority
        settings = self.runtime.settings  # type: ignore[attr-defined]
        if (authority.issuer, authority.resource, authority.project_id) != (settings.oauth.canonical_issuer, settings.oauth.resource, settings.oauth.firebase_project_id) or not {READ_SCOPE, DEPLOY_SCOPE} <= set(authority.accepted_scopes):
            return "original_authority_denied"
        original = GrantAuthority(authority.trusted_grant_id, authority.project_id, authority.uid, authority.authentication_time, authority.oauth_client_id, authority.issuer, authority.resource, authority.accepted_scopes, authority.accepted_at)
        decision = self.runtime.grants.check_pending_authority(original, (READ_SCOPE, DEPLOY_SCOPE))  # type: ignore[attr-defined]
        if decision.status is PendingAuthorityStatus.UNAVAILABLE:
            return "original_authority_unavailable"
        if decision.status is not PendingAuthorityStatus.PERMITTED:
            return "original_authority_denied"
        target_id = next((part.value for part in record.intent.parameters if part.name == "target_id"), None)
        resolved = self.runtime._resolve(target_id, self.runtime.acceptance_action_class)  # type: ignore[attr-defined]
        if not self.runtime.is_resolved_worker(resolved) or resolved.target != record.intent.target:  # type: ignore[attr-defined]
            return "current_policy_unavailable" if isinstance(resolved, dict) and resolved.get("status") == "UNAVAILABLE" else "current_policy_denied"
        return None

    def _complete(self, record: OperationRecord, published: PublishedSourceAcceptance) -> OperationRecord:
        journal = self.producer.journal
        receipt = EffectReceipt(
            record.effect.correlation_identity,
            "github-source-acceptance-readback",
            time.time(),
            json.dumps({"status": "PASS_PUBLISHED", "tag_ref": published.tag_ref, "tag_object_oid": published.tag_object_oid, "direct_commit_sha": published.direct_commit_sha}, sort_keys=True, separators=(",", ":")),
        )
        current = record
        if current.effect.state is EffectState.SENT_UNKNOWN:
            mutation = journal.record_effect(current.operation_id, current.revision, attempt_id=current.effect.attempt_id, state=EffectState.CONFIRMED, receipt=receipt)
            current = mutation.record or current
        if current.effect.state is not EffectState.CONFIRMED:
            return current
        mutation = (
            journal.resolve_needs_attention(current.operation_id, current.revision, OperationPhase.SUCCEEDED, evidence=current.effect.receipt or receipt)
            if current.phase is OperationPhase.NEEDS_ATTENTION
            else journal.transition(current.operation_id, current.revision, OperationPhase.SUCCEEDED, evidence=current.effect.receipt or receipt)
        )
        return mutation.record or current

    def forward(self, record: OperationRecord) -> OperationRecord:
        observation = self.producer.advance(record.operation_id)
        if observation.state is not SmokeState.COMPLETE or observation.evidence is None:
            return record
        if observation.evidence.local_result is not LocalResult.PASS:
            receipt = EffectReceipt(
                observation.evidence.content_sha256,
                "beta-runtime-smoke-local-result",
                time.time(),
                json.dumps(
                    {
                        "local_evidence_id": observation.evidence.local_evidence_id,
                        "local_result": observation.evidence.local_result.value,
                        "reason": observation.reason,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            changed = self.producer.journal.reject_before_effect(
                record.operation_id,
                record.revision,
                category=f"local_{observation.evidence.local_result.value.lower()}",
                evidence=receipt,
            )
            return changed.record or record
        refusal = self._authority_refusal(record)
        if refusal is not None:
            return self._hold(record, refusal)
        attempt = f"publish-{record.operation_id}"
        begun = self.producer.journal.begin_effect(record.operation_id, record.revision, owner_attempt_id=attempt, attempt_id=attempt, correlation_identity=observation.evidence.content_sha256)
        if begun.status is not MutationStatus.UPDATED or begun.record is None:
            return begun.record or record
        try:
            published = self.publisher.publish(observation.evidence, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        except (SourceAcceptanceError, RuntimeError, OSError):
            return self._hold(begun.record, "UNKNOWN:source_acceptance_publication")
        return self._complete(begun.record, published)

    def reconcile(self, record: OperationRecord) -> OperationRecord:
        if record.effect.state is not EffectState.SENT_UNKNOWN:
            return self.forward(record)
        try:
            published = self.publisher.read(record.operation_id)
        except (SourceAcceptanceError, RuntimeError, OSError):
            return record
        return self._complete(record, published) if published is not None else record
