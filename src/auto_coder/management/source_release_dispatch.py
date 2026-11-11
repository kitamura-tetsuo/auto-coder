"""One authenticated accepted-source workflow send, then exact read-only recovery.

SQLite owns intent and possible-send state. A process-scoped file lock prevents
another manager from reconciling/freeing the lane while a sender is still alive.
Neither a timeout nor a negative Actions listing permits a second POST.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Mapping

from auto_coder.logger_config import get_logger
from auto_coder.operating_policy import ActionClass, DecisionOutcome, PolicyIntent, evaluate_operating_policy
from auto_coder.source_release_catalog import RELEASE_WORKFLOW, SourceReleaseCandidate, SourceReleaseError, SourceReleasePolicy, SourceReleaseRequest, canonical
from auto_coder.source_release_promotion import AcceptedSourceValidator, SourceReleasePromotion
from auto_coder.util.gh_cache import GitHubGitDataClient

from .action_scopes import DEPLOY_SCOPE, READ_SCOPE
from .action_scopes import AcceptedAuthority as GrantAuthority
from .action_scopes import PendingAuthorityStatus
from .operations import ActionParameter, AdmissionStatus, EffectReceipt, EffectState, ManagedTarget, MutationStatus, OperationIntent, OperationPhase, OperationRecord

if TYPE_CHECKING:
    from .mcp_runtime_tools import ManagementRuntime

logger = get_logger(__name__)
ACTION = "promote_accepted_source_release"
SCOPES = (READ_SCOPE, DEPLOY_SCOPE)
MAX_PAGES = 5
PAGE_SIZE = 100
INPUT_FIELDS = (
    "source_sha",
    "ci_run_id",
    "ci_run_attempt",
    "tested_source_ref",
    "tested_source_oid",
    "tested_source_record_sha256",
    "acceptance_ref",
    "acceptance_oid",
)


class SourceReleaseDispatch:
    def __init__(self, runtime: ManagementRuntime) -> None:
        self.runtime = runtime
        self.settings = runtime.settings
        self.journal = runtime.journal

    def configuration(self) -> SourceReleasePolicy:
        path = self.settings.source_release_policy_path
        if path is None or self.settings.source_release_token_file is None:
            raise SourceReleaseError("release dispatch is not configured")
        return SourceReleasePolicy.load(path)

    def project(self, record: OperationRecord) -> dict[str, object]:
        result = self.runtime._project_operation(record)
        result["workflow_run"] = self.journal.source_release_run(record.operation_id)
        result["selected_source_sha"] = record.intent.expected_artifact
        result["worker_deployed"] = False
        if record.phase is OperationPhase.SUCCEEDED:
            result["safe_reason"] = None
        result["source_promotion"] = json.loads(record.effect.receipt.safe_result)["outcome"] if record.phase is OperationPhase.SUCCEEDED and record.effect.receipt is not None else "UNRESOLVED" if record.effect.state is EffectState.SENT_UNKNOWN else "NOT_DISPATCHED"
        return result

    def transport(self, policy: SourceReleasePolicy) -> GitHubGitDataClient:
        assert self.settings.source_release_token_file is not None
        token = self.settings.source_release_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise SourceReleaseError("release dispatch credential is unavailable")
        return GitHubGitDataClient(token, policy.source_repository)

    def permitted(self, target_id: str, policy: SourceReleasePolicy) -> bool:
        return (
            target_id == self.settings.source_release_target_id
            and self.settings.operating_policy_path is not None
            and evaluate_operating_policy(self.settings.operating_policy_path, PolicyIntent(target_id, policy.source_repository, ActionClass.RELEASE_PROMOTION, "release")).outcome is DecisionOutcome.ALLOWED
        )

    @staticmethod
    def request(policy: SourceReleasePolicy, values: Mapping[str, object], operation_id: str = "admission") -> SourceReleaseCandidate:
        return SourceReleaseCandidate(operation_id=operation_id, source_repository=policy.source_repository, release_target=policy.release_target, **{key: values[key] for key in INPUT_FIELDS})  # type: ignore[arg-type]

    async def admit(self, target_id: str, idempotency_key: str, values: dict[str, object]) -> dict[str, object]:
        grant = self.runtime._grant(SCOPES)
        if not isinstance(idempotency_key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", idempotency_key) is None:
            return self.runtime._response("INVALID_REQUEST", "invalid_idempotency_key")
        try:
            policy = self.configuration()
            request = self.request(policy, values)
            request.validate(policy)
            # One canonical source/ref lane, independent of target aliases,
            # actors, OAuth clients and idempotency keys.
            target = ManagedTarget("accepted-source-release", "release", policy.source_repository)
            params = {**values, "target_id": target_id, "release_policy_revision": policy.revision, "workflow_id": self.settings.source_release_workflow_id}
            intent = OperationIntent(target, ACTION, tuple(ActionParameter(key, value) for key, value in params.items()), idempotency_key, policy.release_ref, request.source_sha)  # type: ignore[arg-type]
            original = self.runtime.grants.accepted_authority(grant)
            authority = self.runtime._journal_authority(grant, original)
            old = await asyncio.to_thread(self.journal.find_by_key, intent, authority)
            if old.record is not None:
                if old.record.intent.fingerprint() != intent.fingerprint():
                    return self.runtime._response("CONFLICT", "idempotency_key_conflict", operation_id=old.record.operation_id)
                updated = await self.runtime.dispatcher.submit(old.record.operation_id)
                return self.project(updated or old.record)
            if not self.permitted(target_id, policy):
                return self.runtime._response("DENIED", "release_policy_denied")
            validated_at = time.time()
            accepted = await asyncio.to_thread(AcceptedSourceValidator(self.transport(policy), policy, clock=lambda: validated_at).validate, request)
            if self.runtime.grants.check_pending_authority(original, SCOPES).status is not PendingAuthorityStatus.PERMITTED or not self.permitted(target_id, policy):
                return self.runtime._response("DENIED", "current_authority_or_policy_denied")
            snapshot = {"schema_version": 1, "kind": ACTION, "policy": asdict(policy), "accepted": asdict(accepted), "validated_at": validated_at, "workflow_id": self.settings.source_release_workflow_id}
            admitted = await asyncio.to_thread(self.journal.admit, intent, authority, lifecycle_snapshot=snapshot)
            if admitted.status not in {AdmissionStatus.CREATED, AdmissionStatus.REPLAY} or admitted.record is None:
                return self.runtime._response(admitted.status.value.upper(), "journal_" + admitted.status.value, operation_id=admitted.record.operation_id if admitted.record else None)
            updated = await self.runtime.dispatcher.submit(admitted.record.operation_id)
            return self.project(updated or admitted.record)
        except (RuntimeError, OSError, ValueError, TypeError, KeyError):
            return self.runtime._response("UNAVAILABLE", "exact_source_evidence_unavailable")

    def _authority(self, record: OperationRecord) -> bool:
        auth = record.authority
        oauth = self.settings.oauth
        if (auth.issuer, auth.resource, auth.project_id) != (oauth.canonical_issuer, oauth.resource, oauth.firebase_project_id):
            return False
        original = GrantAuthority(auth.trusted_grant_id, auth.project_id, auth.uid, auth.authentication_time, auth.oauth_client_id, auth.issuer, auth.resource, auth.accepted_scopes, auth.accepted_at)
        return self.runtime.grants.check_pending_authority(original, SCOPES).status is PendingAuthorityStatus.PERMITTED

    def _hold(self, record: OperationRecord, reason: str) -> OperationRecord:
        logger.info("Source release observation operation={} outcome={}", record.operation_id, reason)
        if record.phase is OperationPhase.NEEDS_ATTENTION and record.safe_error_category == reason:
            return record
        return self.journal.transition(record.operation_id, record.revision, OperationPhase.NEEDS_ATTENTION, owner_attempt_id=record.owner_attempt_id, safe_error_category=reason).record or record

    def _refuse(self, record: OperationRecord, reason: str) -> OperationRecord:
        receipt = EffectReceipt(record.operation_id, "source-release-preflight", time.time(), reason)
        return self.journal.reject_before_effect(record.operation_id, record.revision, category=reason, evidence=receipt).record or record

    def _workflow(self, client: GitHubGitDataClient, policy: SourceReleasePolicy, workflow_id: int) -> None:
        response = client.request("GET", f"actions/workflows/{workflow_id}")
        data = response.data
        if (
            response.status != 200
            or not isinstance(data, dict)
            or type(data.get("id")) is not int
            or data.get("id") != workflow_id
            or data.get("path") != RELEASE_WORKFLOW
            or data.get("state") != "active"
            or data.get("url") != f"https://api.github.com/repos/{policy.source_repository}/actions/workflows/{workflow_id}"
        ):
            raise SourceReleaseError("fixed workflow identity unavailable")

    @staticmethod
    def _run(data: object, policy: SourceReleasePolicy, workflow_id: int, operation_id: str) -> dict[str, object]:
        if not isinstance(data, dict):
            raise SourceReleaseError("run unavailable")
        run_id, attempt = data.get("id"), data.get("run_attempt")
        repository = data.get("repository")
        if (
            type(run_id) is not int
            or run_id <= 0
            or type(attempt) is not int
            or attempt != 1
            or type(data.get("workflow_id")) is not int
            or data.get("workflow_id") != workflow_id
            or data.get("path") != RELEASE_WORKFLOW
            or not isinstance(repository, dict)
            or repository.get("full_name") != policy.source_repository
            or data.get("event") != "workflow_dispatch"
            or data.get("head_branch") != policy.release_branch
            or data.get("display_title") != f"accepted-release/{operation_id}"
            or not isinstance(data.get("head_sha"), str)
            or re.fullmatch(r"[a-f0-9]{40}", str(data["head_sha"])) is None
            or data.get("html_url") != f"https://github.com/{policy.source_repository}/actions/runs/{run_id}"
            or data.get("url") != f"https://api.github.com/repos/{policy.source_repository}/actions/runs/{run_id}"
            or data.get("status") not in {"queued", "in_progress", "completed"}
        ):
            raise SourceReleaseError("run identity mismatch")
        return {key: data[key] for key in ("id", "run_attempt", "head_sha", "workflow_id", "html_url", "url")}

    def _read_run(self, client: GitHubGitDataClient, policy: SourceReleasePolicy, workflow_id: int, operation_id: str, run_id: int, *, attempt: bool = False) -> tuple[dict[str, object], dict[str, object]]:
        response = client.request("GET", f"actions/runs/{run_id}" + ("/attempts/1" if attempt else ""))
        if response.status != 200 or not isinstance(response.data, dict):
            raise SourceReleaseError("exact Actions run unavailable")
        identity = self._run(response.data, policy, workflow_id, operation_id)
        if identity["id"] != run_id:
            raise SourceReleaseError("run ID readback differs")
        return identity, response.data

    def _discover(self, client: GitHubGitDataClient, policy: SourceReleasePolicy, workflow_id: int, operation_id: str) -> dict[str, object]:
        seen: set[int] = set()
        matches: list[dict[str, object]] = []
        total: int | None = None
        for page in range(1, MAX_PAGES + 1):
            response = client.request("GET", f"actions/workflows/{workflow_id}/runs?event=workflow_dispatch&branch=main&per_page={PAGE_SIZE}&page={page}")
            data = response.data
            if response.status != 200 or not isinstance(data, dict) or type(data.get("total_count")) is not int or not isinstance(data.get("workflow_runs"), list):
                raise SourceReleaseError("incomplete Actions listing")
            if total is None:
                total = data["total_count"]
            if data["total_count"] != total or total < 0 or total > MAX_PAGES * PAGE_SIZE:
                raise SourceReleaseError("Actions listing changed or exceeds bound")
            runs = data["workflow_runs"]
            if len(runs) > PAGE_SIZE:
                raise SourceReleaseError("unbounded Actions page")
            for run in runs:
                if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0 or run["id"] in seen:
                    raise SourceReleaseError("Actions listing gap/duplicate")
                seen.add(run["id"])
                if run.get("display_title") == f"accepted-release/{operation_id}":
                    matches.append(self._run(run, policy, workflow_id, operation_id))
            if len(runs) < PAGE_SIZE:
                if len(seen) != total or len(matches) != 1:
                    raise SourceReleaseError("zero, multiple or incomplete exact run matches")
                return matches[0]
        raise SourceReleaseError("Actions listing exhausted finite bound")

    def process(self, original: OperationRecord) -> OperationRecord:
        root = self.settings.operation_state_root or self.settings.state_root
        root.mkdir(parents=True, exist_ok=True)
        lane = hashlib.sha256(original.intent.target.key().encode()).hexdigest()
        with (root / f"source-release-{lane}.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return original
            record = self.journal.get(original.operation_id).record
            if record is None or record.phase in {OperationPhase.SUCCEEDED, OperationPhase.FAILED, OperationPhase.ROLLED_BACK}:
                return record or original
            try:
                saved = self.journal.lifecycle_snapshot(record.operation_id)
                if saved is None or saved[1].get("kind") != ACTION:
                    raise SourceReleaseError("original dispatch snapshot unavailable")
                snapshot = saved[1]
                policy = SourceReleasePolicy.from_document(snapshot["policy"])
                workflow_id = snapshot["workflow_id"]
                if type(workflow_id) is not int or workflow_id <= 0:
                    raise SourceReleaseError("original workflow invalid")
                params = self.runtime._params(record)
                request = self.request(policy, params, record.operation_id)
                request.validate(policy)
                if (
                    record.intent.target != ManagedTarget("accepted-source-release", "release", policy.source_repository)
                    or record.intent.expected_incarnation != policy.release_ref
                    or record.intent.expected_artifact != request.source_sha
                    or params.get("release_policy_revision") != policy.revision
                    or params.get("workflow_id") != workflow_id
                ):
                    raise SourceReleaseError("original intent binding invalid")
                client = self.transport(policy)
                if record.effect.state is EffectState.NOT_SENT:
                    if not self._authority(record) or not self.permitted(str(params["target_id"]), policy):
                        return self._refuse(record, "current_authority_or_policy_denied")
                    if self.configuration() != policy or self.settings.source_release_workflow_id != workflow_id:
                        return self._refuse(record, "trusted_dispatch_configuration_changed")
                    accepted = AcceptedSourceValidator(client, policy, clock=time.time).validate(request)
                    if asdict(accepted) != snapshot["accepted"]:
                        return self._refuse(record, "immutable_acceptance_changed")
                    self._workflow(client, policy, workflow_id)
                    if not self._authority(record) or not self.permitted(str(params["target_id"]), policy):
                        return self._refuse(record, "current_authority_or_policy_denied")
                    if self.configuration() != policy:
                        return self._refuse(record, "trusted_dispatch_configuration_changed")
                    attempt_id = uuid.uuid4().hex
                    begun = self.journal.begin_effect(record.operation_id, record.revision, owner_attempt_id=attempt_id, attempt_id=attempt_id, correlation_identity=record.operation_id)
                    if begun.status is not MutationStatus.UPDATED or begun.record is None:
                        return begun.record or record
                    record = begun.record
                    if not self.runtime.retained_effect_permitted(record):
                        return self._hold(record, "retained_effect_fence_denied")
                    inputs = {key: getattr(request, key) for key in ("operation_id", "source_repository", *INPUT_FIELDS, "release_target")}
                    # Persist SENT_UNKNOWN before entering the shared HTTP transport.
                    response = client.request("POST", f"actions/workflows/{workflow_id}/dispatches", {"ref": policy.release_branch, "inputs": inputs, "return_run_details": True})
                    data = response.data
                    if response.status == 200:
                        if not isinstance(data, dict) or type(data.get("workflow_run_id")) is not int or data["workflow_run_id"] <= 0:
                            return self._hold(record, "dispatch_response_identity_invalid")
                        run_id = data["workflow_run_id"]
                        if any(data.get(key) not in (None, value) for key, value in (("run_url", f"https://api.github.com/repos/{policy.source_repository}/actions/runs/{run_id}"), ("html_url", f"https://github.com/{policy.source_repository}/actions/runs/{run_id}"))):
                            return self._hold(record, "dispatch_response_url_mismatch")
                        if not self.journal.bind_source_release_run(record, {"id": run_id}, kind="response"):
                            return self._hold(record, "response_binding_unavailable")
                        record = self.journal.get(record.operation_id).record or record
                    elif response.status != 204:
                        return self._hold(record, "dispatch_response_unconfirmed")
                return self._observe(record, policy, workflow_id, snapshot, client)
            except (RuntimeError, OSError, ValueError, TypeError, KeyError):
                if record.effect.state is EffectState.NOT_SENT:
                    return self._refuse(record, "pre_dispatch_evidence_unavailable")
                return self._hold(record, "source_promotion_unknown")

    def _observe(self, record: OperationRecord, policy: SourceReleasePolicy, workflow_id: int, snapshot: dict[str, object], client: GitHubGitDataClient) -> OperationRecord:
        bound = self.journal.source_release_run(record.operation_id)
        if bound is None:
            response = self.journal.source_release_run(record.operation_id, kind="response")
            if response is not None:
                bound, _ = self._read_run(client, policy, workflow_id, record.operation_id, int(str(response["id"])))
            else:
                bound = self._discover(client, policy, workflow_id, record.operation_id)
            if not self.journal.bind_source_release_run(record, bound):
                return self._hold(record, "run_binding_unavailable")
            record = self.journal.get(record.operation_id).record or record
        identity, run = self._read_run(client, policy, workflow_id, record.operation_id, int(str(bound["id"])), attempt=True)
        if identity != bound:
            return self._hold(record, "bound_run_identity_changed")
        if run.get("status") != "completed":
            return self._hold(record, "workflow_pending")
        if run.get("conclusion") != "success":
            # Even failure/cancellation can follow a partial publication.
            return self._hold(record, "workflow_failed_effect_unresolved")
        request = SourceReleaseRequest(**asdict(self.request(policy, self.runtime._params(record), record.operation_id)), promotion_run_id=int(str(bound["id"])), promotion_run_attempt=int(str(bound["run_attempt"])))
        # Recheck immutable original evidence at its original validation time;
        # expiry now forbids new dispatch, not exact historical readback.
        accepted = AcceptedSourceValidator(client, policy, clock=lambda: float(str(snapshot["validated_at"]))).validate(request)
        if asdict(accepted) != snapshot["accepted"]:
            return self._hold(record, "original_acceptance_changed")
        promotion = SourceReleasePromotion(client, policy)
        catalog = promotion.catalog.read(record.operation_id)
        if catalog is None:
            return self._hold(record, "successful_workflow_catalog_missing")
        promotion._verify_original(catalog.record, request, accepted)
        release = promotion._read_release(catalog.record)
        pointer = promotion._pointer()
        if release is None or pointer is None:
            return self._hold(record, "source_publication_incomplete")
        selection = promotion._selection_oid()
        outcome = "SOURCE_PROMOTION_CONFIRMED" if pointer == request.source_sha and selection == catalog.tag_object_oid else "COMPLETED_BUT_SUPERSEDED"
        if promotion.catalog.read(record.operation_id) != catalog or promotion._read_release(catalog.record) != release or promotion._pointer() != pointer or promotion._selection_oid() != selection:
            return self._hold(record, "source_publication_readback_changed")
        receipt = EffectReceipt(
            record.effect.correlation_identity, "github-source-release-readback", time.time(), canonical({"outcome": outcome, "run_id": bound["id"], "run_attempt": bound["run_attempt"], "catalog_oid": catalog.tag_object_oid, "source_sha": request.source_sha, "worker_deployed": False})
        )
        logger.info("Source release observation operation={} outcome={} run={}", record.operation_id, outcome, bound["id"])
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            return self.journal.resolve_needs_attention(record.operation_id, record.revision, OperationPhase.SUCCEEDED, evidence=receipt).record or record
        return self.journal.transition(record.operation_id, record.revision, OperationPhase.SUCCEEDED, owner_attempt_id=record.owner_attempt_id, evidence=receipt).record or record
