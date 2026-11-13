"""Authenticated, journal-fenced worker process controls and beta source activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import StrictInt

from auto_coder.operating_policy import ActionClass
from auto_coder.util.gh_cache import GitHubGitDataClient
from auto_coder.worker_startup_failure import observed_startup_failure, startup_failure_fact

from .action_scopes import DEPLOY_SCOPE, READ_SCOPE
from .action_scopes import AcceptedAuthority as GrantAuthority
from .action_scopes import PendingAuthorityStatus
from .operations import ActionParameter, AdmissionStatus, EffectReceipt, EffectState, MutationStatus, OperationIntent, OperationPhase, OperationRecord, QueryStatus, ReadStatus
from .release_worker_deployment import ReleaseWorkerEvidenceIdentity, ReleaseWorkerEvidenceReader
from .source_evidence import BetaEvidenceReader, SourceEvidenceError
from .source_release_dispatch import SourceReleaseError
from .worker_network_client import WorkerNetworkClient, WorkerNetworkResult

if TYPE_CHECKING:
    from .mcp_runtime_tools import ManagementRuntime

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{40}\Z")
LIFECYCLE_ACTIONS = frozenset({"start_worker", "stop_worker", "restart_worker", "deploy_beta_source", "deploy_release_source"})
SOURCE_ACTIONS = frozenset({"deploy_beta_source", "deploy_release_source"})
TERMINAL_STAGES = frozenset({"SUCCEEDED", "FAILED", "SUPERSEDED"})


@dataclass(frozen=True)
class WorkerFence:
    supervisor_generation: str
    process_run_id: str | None
    deployment_generation: str | None
    runtime_id: str | None


@dataclass(frozen=True)
class RetainedSupervisorEvidence:
    supervisor_generation: str
    receipt: dict[str, object]
    worker: dict[str, object] | None
    observation: dict[str, object] | None


class WorkerLifecycle:
    """A single-effect-at-a-time orchestrator; all effects have durable IDs."""

    def __init__(self, runtime: ManagementRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _answer(status: str, reason: str, **details: object) -> dict[str, object]:
        return {"status": status, "reason": reason, **details}

    def _source(self) -> BetaEvidenceReader:
        config = self.runtime.settings
        if config.source_repository is None or config.source_evidence_token_file is None:
            raise SourceEvidenceError("source evidence authority is not configured")
        path = config.source_evidence_token_file
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise SourceEvidenceError("source evidence credential is unavailable")
        secret = path.read_text(encoding="utf-8").strip()
        if not 20 <= len(secret) <= 512 or "\n" in secret:
            raise SourceEvidenceError("source evidence credential is invalid")
        return BetaEvidenceReader(GitHubGitDataClient(secret, config.source_repository), config.source_repository)

    def _release(self) -> ReleaseWorkerEvidenceReader:
        producer = self.runtime.source_release
        policy = producer.configuration()
        return ReleaseWorkerEvidenceReader(
            self.runtime.journal,
            producer.transport(policy),
            policy,
            promotion_target_id=self.runtime.settings.source_release_target_id,
            workflow_id=self.runtime.settings.source_release_workflow_id,
        )

    @staticmethod
    def _release_evidence(facts: object) -> str:
        from .release_worker_deployment import ReleaseWorkerPromotionFacts

        if not isinstance(facts, ReleaseWorkerPromotionFacts) or not facts.release_operation_selection_proven:
            raise SourceReleaseError("the original promotion is not the selected release")
        return json.dumps(
            {
                "source_sha": facts.identity.source_sha,
                "lock_sha256": facts.lock_sha256,
                "distribution_version": facts.distribution_version,
                "promotion_operation_id": facts.identity.promotion_operation_id,
                "catalog_oid": facts.identity.catalog_oid,
                "acceptance_oid": facts.identity.acceptance_oid,
                "policy_revision": facts.policy_revision,
                "promotion_intent_fingerprint": facts.promotion_intent_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _authority_refusal(self, record: OperationRecord) -> str | None:
        authority = record.authority
        settings = self.runtime.settings
        if (authority.issuer, authority.resource, authority.project_id) != (settings.oauth.canonical_issuer, settings.oauth.resource, settings.oauth.firebase_project_id) or not {READ_SCOPE, DEPLOY_SCOPE} <= set(authority.accepted_scopes):
            return "original_authority_denied"
        original = GrantAuthority(authority.trusted_grant_id, authority.project_id, authority.uid, authority.authentication_time, authority.oauth_client_id, authority.issuer, authority.resource, authority.accepted_scopes, authority.accepted_at)
        decision = self.runtime.grants.check_pending_authority(original, (READ_SCOPE, DEPLOY_SCOPE))
        if decision.status is not PendingAuthorityStatus.PERMITTED:
            return "original_authority_unavailable" if decision.status is PendingAuthorityStatus.UNAVAILABLE else "original_authority_denied"
        parameters = self.runtime._params(record)
        target_id = parameters.get("target_id")
        if not isinstance(target_id, str):
            return "recorded_target_invalid"
        from .mcp_runtime_tools import ResolvedWorker

        action = ActionClass.DEPLOYMENT if record.intent.action in SOURCE_ACTIONS else ActionClass.WORKER_CONTROL
        current = self.runtime._resolve(target_id, action)
        if not isinstance(current, ResolvedWorker) or current.target != record.intent.target or self.runtime._record_client(record) is None:
            return "current_policy_unavailable" if isinstance(current, dict) and current.get("status") == "UNAVAILABLE" else "current_policy_denied"
        return None

    @staticmethod
    def _inspection(client: WorkerNetworkClient) -> tuple[WorkerFence, dict[str, object]] | None:
        result = client.inspect_supervisor()
        data = result.data
        if result.result_code != "OK" or not isinstance(data, dict) or result.supervisor_generation != data.get("supervisor_incarnation"):
            return None
        worker = data.get("worker")
        if isinstance(worker, dict):
            if data.get("alive") is not True or data.get("status") != "RUNNING":
                return None
            run, generation, runtime_id = worker.get("process_run_id"), worker.get("deployment_generation"), worker.get("runtime_id")
            if not isinstance(run, str) or not isinstance(generation, str) or not isinstance(runtime_id, str):
                return None
            return WorkerFence(result.supervisor_generation, run, generation, runtime_id), data
        if data.get("status") == "STOPPED" and data.get("alive") is False:
            selected = data.get("selected_runtime_id")
            return WorkerFence(result.supervisor_generation, None, None, selected if isinstance(selected, str) else None), data
        return None

    async def admit(
        self,
        action: str,
        target_id: str,
        idempotency_key: str,
        expected_supervisor_generation: str,
        expected_process_run_id: str | None,
        expected_deployment_generation: str | None,
        expected_runtime_id: str | None,
        selected_git_sha: str | None = None,
        release_identity: ReleaseWorkerEvidenceIdentity | None = None,
    ) -> dict[str, object]:
        from .mcp_runtime_tools import ResolvedWorker

        grant = self.runtime._grant((READ_SCOPE, DEPLOY_SCOPE))
        values = (target_id, idempotency_key, expected_supervisor_generation)
        if (
            action not in LIFECYCLE_ACTIONS
            or any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in values)
            or (expected_process_run_id is None) != (expected_deployment_generation is None)
            or any(value is not None and (not isinstance(value, str) or _ID.fullmatch(value) is None) for value in (expected_process_run_id, expected_deployment_generation, expected_runtime_id))
            or (action in SOURCE_ACTIONS) != (isinstance(selected_git_sha, str) and _SHA.fullmatch(selected_git_sha) is not None)
            or (action == "deploy_release_source") != (release_identity is not None)
        ):
            return self._answer("INVALID_REQUEST", "invalid_worker_lifecycle_request")
        if release_identity is not None:
            try:
                release_identity.validate()
            except SourceReleaseError:
                return self._answer("INVALID_REQUEST", "invalid_release_provenance_identity")
            if release_identity.source_sha != selected_git_sha:
                return self._answer("INVALID_REQUEST", "release_source_sha_mismatch")
        resolved = self.runtime._resolve(target_id, ActionClass.INSPECT)
        if not isinstance(resolved, ResolvedWorker):
            return resolved
        if action == "deploy_beta_source" and resolved.target.channel != "beta":
            return self._answer("DENIED", "release_source_deployment_not_authorized")
        if action == "deploy_release_source" and resolved.target.channel != "release":
            return self._answer("DENIED", "configured_release_worker_required")
        if action in {"stop_worker", "restart_worker"} and expected_process_run_id is None:
            return self._answer("INVALID_REQUEST", "running_worker_identity_required")
        if action == "start_worker" and expected_process_run_id is not None:
            return self._answer("INVALID_REQUEST", "start_requires_stopped_worker")
        params = (
            ActionParameter("target_id", target_id),
            ActionParameter("worker_binding", resolved.client.config.binding_digest),
            ActionParameter("supervisor_generation", expected_supervisor_generation),
            ActionParameter("runtime_id", expected_runtime_id or "-"),
            ActionParameter("source_sha", selected_git_sha or "-"),
        ) + (tuple(ActionParameter(key, value) for key, value in asdict(release_identity).items() if key != "source_sha") if release_identity is not None else ())
        intent = OperationIntent(resolved.target, action, params, idempotency_key, expected_process_run_id or "-", expected_deployment_generation or "-")
        original = self.runtime._journal_authority(grant, self.runtime.grants.accepted_authority(grant))
        retained = await asyncio.to_thread(self.runtime.retained_admission, intent, original, target_id)
        if retained is not None:
            return retained
        old = await asyncio.to_thread(self.runtime.journal.find_by_key, intent, original)
        if old.status is ReadStatus.UNAVAILABLE:
            return self._answer("UNAVAILABLE", "journal_unavailable")
        if old.record is not None:
            if not self.runtime._same_original_intent(old.record, intent):
                return self._answer("CONFLICT", "idempotency_key_conflict", operation_id=old.record.operation_id)
            updated = await self.runtime.dispatcher.submit(old.record.operation_id)
            return self.project(updated or old.record)
        # A different key must see the existing lane owner even when its
        # requested release selection has since become stale or unavailable.
        outstanding = await asyncio.to_thread(self.runtime.journal.list_operations, outstanding_only=True, target=resolved.target)
        if outstanding.status is not QueryStatus.OK:
            return self._answer("UNAVAILABLE", "journal_unavailable")
        if outstanding.records:
            return self._answer("BUSY", "journal_busy", operation_id=outstanding.records[0].operation_id)
        mutation = self.runtime._resolve(target_id, ActionClass.DEPLOYMENT if action in SOURCE_ACTIONS else ActionClass.WORKER_CONTROL)
        if not isinstance(mutation, ResolvedWorker):
            return mutation

        if release_identity is not None:
            try:
                if self.runtime.source_release.configuration().source_repository != mutation.target.repository:
                    return self._answer("DENIED", "release_worker_repository_mismatch")
            except (SourceReleaseError, OSError, ValueError):
                return self._answer("UNAVAILABLE", "trusted_release_configuration_unavailable")

        def inspect_locked() -> tuple[WorkerFence, dict[str, object]] | None:
            with self.runtime._worker_locks[mutation.target_id]:
                return self._inspection(mutation.client)

        inspected = await asyncio.to_thread(inspect_locked)
        if inspected is None or inspected[0] != WorkerFence(expected_supervisor_generation, expected_process_run_id, expected_deployment_generation, expected_runtime_id):
            return self._answer("CONFLICT", "incumbent_or_supervisor_generation_changed")
        evidence = ""
        if action == "deploy_beta_source":
            try:
                evidence = await asyncio.to_thread(lambda: self._source().read(str(selected_git_sha)).canonical())
            except (SourceEvidenceError, RuntimeError, OSError, ValueError):
                return self._answer("UNAVAILABLE", "beta_source_evidence_unavailable_or_ineligible")
        elif release_identity is not None:
            try:
                evidence = await asyncio.to_thread(lambda: self._release_evidence(self._release().read(release_identity)))
            except (SourceReleaseError, RuntimeError, OSError, ValueError, TypeError):
                return self._answer("UNAVAILABLE", "release_source_evidence_unavailable_or_superseded")
        # The entire original authority and configured full/release policy must
        # remain current even if evidence retrieval or inspection was slow.
        if self.runtime.grants.check_pending_authority(self.runtime.grants.accepted_authority(grant), (READ_SCOPE, DEPLOY_SCOPE)).status is not PendingAuthorityStatus.PERMITTED or not isinstance(
            self.runtime._resolve(target_id, ActionClass.DEPLOYMENT if action in SOURCE_ACTIONS else ActionClass.WORKER_CONTROL), ResolvedWorker
        ):
            return self._answer("DENIED", "current_authority_or_policy_denied")
        now = time.time()
        incumbent_worker = inspected[1].get("worker")
        incumbent = incumbent_worker if isinstance(incumbent_worker, dict) else {}
        snapshot: dict[str, object] = {
            "schema_version": 1,
            "stage": "ACCEPTED",
            "action": action,
            "accepted_evidence": evidence,
            "release_identity": asdict(release_identity) if release_identity is not None else None,
            "revalidation_observation": None,
            "source_sha": selected_git_sha,
            "incumbent": {
                "process_run_id": expected_process_run_id,
                "deployment_generation": expected_deployment_generation,
                "runtime_id": expected_runtime_id,
                "supervisor_generation": expected_supervisor_generation,
            },
            "previous_runtime_verified": bool(isinstance(incumbent.get("runtime_binding"), dict) and incumbent.get("binding_status") == "VERIFIED"),
            "previous_binding": incumbent.get("runtime_binding"),
            "staged": None,
            "steps": {},
            "new_worker": None,
            "heartbeats": [],
            "recovery_attempts": 0,
            "accepted_at": now,
            "original_deadline": now + 900,
            "deadline": now + 900,
        }
        admission = await asyncio.to_thread(self.runtime.journal.admit, intent, original, lifecycle_snapshot=snapshot)
        if admission.status not in {AdmissionStatus.CREATED, AdmissionStatus.REPLAY} or admission.record is None:
            return self._answer(admission.status.value.upper(), "journal_" + admission.status.value, operation_id=admission.record.operation_id if admission.record else None)
        updated = await self.runtime.dispatcher.submit(admission.record.operation_id)
        return self.project(updated or admission.record)

    def project(self, record: OperationRecord) -> dict[str, object]:
        try:
            persisted = self.runtime.journal.lifecycle_snapshot(record.operation_id)
        except (OSError, ValueError):
            persisted = None
        if persisted is None:
            return self._answer("NEEDS_ATTENTION", "lifecycle_journal_unavailable", operation_id=record.operation_id)
        return self.project_snapshot(record, persisted[1])

    def project_snapshot(self, record: OperationRecord, snapshot: dict[str, object]) -> dict[str, object]:
        if not isinstance(snapshot.get("stage"), str):
            return self._answer("NEEDS_ATTENTION", "lifecycle_journal_unavailable", operation_id=record.operation_id)
        reason = record.safe_error_category or "worker_lifecycle"
        receipt = record.effect.receipt
        terminal_phase = OperationPhase.SUCCEEDED if snapshot["stage"] == "SUCCEEDED" else OperationPhase.FAILED
        if (
            snapshot["stage"] in TERMINAL_STAGES
            and record.phase is terminal_phase
            and record.effect.state is EffectState.CONFIRMED
            and receipt is not None
            and bool(record.effect.correlation_identity)
            and receipt.correlation_identity == record.effect.correlation_identity
            and receipt.observation_source == "supervisor-and-worker-lifecycle-journal"
            and receipt.safe_result == snapshot.get("reason")
        ):
            # Attention resolution retains the historical journal category.
            # Project the new outcome only after its correlated terminal proof
            # and lifecycle snapshot are both durable; never from snapshot alone.
            reason = receipt.safe_result
        return self._answer(
            str(snapshot["stage"]),
            reason,
            operation_id=record.operation_id,
            phase=record.phase.value,
            action=record.intent.action,
            target_id=self.runtime._params(record).get("target_id"),
            channel=record.intent.target.channel,
            instance_id=record.intent.target.instance_id,
            repository=record.intent.target.repository,
            source_sha=snapshot.get("source_sha"),
            revalidation_observation=snapshot.get("revalidation_observation"),
            incumbent=snapshot.get("incumbent"),
            staged=snapshot.get("staged"),
            new_worker=snapshot.get("new_worker"),
            steps=snapshot.get("steps"),
            heartbeats=snapshot.get("heartbeats"),
            startup_failure=snapshot.get("startup_failure"),
            recovery_attempts=snapshot.get("recovery_attempts"),
            recovery_state=snapshot.get("recovery_state", "NOT_ATTEMPTED"),
            recovery_method=snapshot.get("recovery_method"),
            restored_worker=snapshot.get("restored_worker"),
            release_identity=snapshot.get("release_identity"),
            original_deadline=snapshot.get("original_deadline"),
            deadline=snapshot.get("deadline"),
        )

    def _save(self, record: OperationRecord, version: int, snapshot: dict[str, object]) -> bool:
        return self.runtime.journal.lifecycle_save(record.operation_id, version, snapshot)

    def _finish(self, record: OperationRecord, version: int, snapshot: dict[str, object], stage: str, reason: str) -> OperationRecord:
        snapshot.update(stage=stage, reason=reason)
        if not self._save(record, version, snapshot):
            return record
        return self._settle(record, stage, reason)

    def _settle(self, record: OperationRecord, stage: str, reason: str) -> OperationRecord:
        """Finish an already-persisted outcome after a lost journal response."""
        phase = OperationPhase.SUCCEEDED if stage == "SUCCEEDED" else OperationPhase.FAILED if stage in TERMINAL_STAGES else OperationPhase.NEEDS_ATTENTION
        if record.phase is phase:
            return record
        receipt = EffectReceipt(record.effect.correlation_identity or record.operation_id, "supervisor-and-worker-lifecycle-journal", time.time(), reason)
        if record.phase is OperationPhase.NEEDS_ATTENTION and phase is not OperationPhase.NEEDS_ATTENTION:
            result = self.runtime.journal.resolve_needs_attention(record.operation_id, record.revision, phase, evidence=receipt)
        else:
            result = self.runtime.journal.transition(record.operation_id, record.revision, phase, safe_error_category=reason, evidence=receipt if phase is not OperationPhase.NEEDS_ATTENTION else None)
        return result.record or record

    @staticmethod
    def _original_supervisor(client: WorkerNetworkClient, snapshot: dict[str, object]) -> str:
        """Bootstrap a fresh signed client without rebinding a journaled effect."""
        incumbent = snapshot.get("incumbent")
        if not isinstance(incumbent, dict) or not isinstance(incumbent.get("supervisor_generation"), str):
            return "UNAVAILABLE"
        inspected = client.inspect_supervisor()
        data = inspected.data
        if inspected.result_code != "OK" or not isinstance(data, dict) or data.get("supervisor_incarnation") != inspected.supervisor_generation:
            return "UNAVAILABLE"
        expected = snapshot.get("current_supervisor_generation", incumbent["supervisor_generation"])
        if inspected.supervisor_generation != expected:
            return "CHANGED"
        return "SAME"

    def _rebind_retained_supervisor(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> bool | None:
        """Adopt a reconstructed owner only through an exact durable effect receipt."""
        evidence = self._retained_supervisor_evidence(record, snapshot, client)
        if not isinstance(evidence, RetainedSupervisorEvidence):
            return evidence
        snapshot["current_supervisor_generation"] = evidence.supervisor_generation
        return self._save(record, version, snapshot)

    def _retained_supervisor_evidence(self, record: OperationRecord, snapshot: dict[str, object], client: WorkerNetworkClient, *, attention_recovery: bool = False) -> RetainedSupervisorEvidence | bool | None:
        """Read original correlated facts; False conflicts and None is unavailable."""
        worker_observation: dict[str, object] | None = None
        incumbent = snapshot.get("incumbent")
        steps = snapshot.get("steps")
        source_action = record.intent.action == "deploy_release_source"
        if not isinstance(incumbent, dict) or not isinstance(steps, dict):
            return False
        if not source_action and (record.intent.action not in {"start_worker", "stop_worker", "restart_worker"} or incumbent.get("runtime_id") != "bootstrap"):
            return False
        # Recovery must use its own original receipt, never the candidate's
        # earlier START. The digest seals its failed-child fence and prior
        # runtime selection under the original dispatching Supervisor.
        recovery = "recovery" in steps
        if recovery and not source_action:
            return False
        name = "recovery" if recovery else "recovery_drain" if "recovery_drain" in steps else "start" if "start" in steps else "drain"
        staged = snapshot.get("staged")
        expected_runtime = incumbent.get("runtime_id")
        if source_action:
            if not isinstance(staged, dict) or not isinstance(staged.get("installed_runtime_id"), str):
                return False
            if name in {"start", "recovery_drain"}:
                expected_runtime = staged["installed_runtime_id"]
        if not isinstance(expected_runtime, str) or not expected_runtime:
            return False
        step = steps.get(name)
        if not isinstance(step, dict) or not isinstance(step.get("expected_digest"), str) or not isinstance(step.get("operation_id"), str):
            return False
        previous = snapshot.get("previous_binding")
        failed = snapshot.get("new_worker")
        if name == "recovery_drain":
            if (
                not source_action
                or snapshot.get("stage") not in ({"NEEDS_ATTENTION"} if attention_recovery else {"RECOVERY_DRAINING", "RESTORING"})
                or snapshot.get("previous_runtime_verified") is not True
                or snapshot.get("recovery_attempts") != 1
                or startup_failure_fact(snapshot.get("startup_failure")) is None
                or not isinstance(failed, dict)
                or failed.get("runtime_id") != expected_runtime
                or not isinstance(failed.get("process_run_id"), str)
                or not isinstance(failed.get("deployment_generation"), str)
                or step.get("operation_id") != "f-" + record.operation_id
                or step.get("state") not in {"SENT_UNKNOWN", "CONFIRMED"}
            ):
                return False
        if recovery:
            if (
                snapshot.get("previous_runtime_verified") is not True
                or snapshot.get("recovery_attempts") != 1
                or snapshot.get("stage") not in ({"NEEDS_ATTENTION"} if attention_recovery else {"RESTORING", "RECONCILING_PRIOR"})
                or step.get("state") not in {"SENT_UNKNOWN", "CONFIRMED"}
                or step.get("operation_id") != "r-" + record.operation_id
                or not isinstance(previous, dict)
                or previous.get("runtime_id") != expected_runtime
                or not isinstance(failed, dict)
                or not isinstance(staged, dict)
                or failed.get("runtime_id") != staged.get("installed_runtime_id")
                or not isinstance(failed.get("deployment_generation"), str)
                or not failed["deployment_generation"]
                or failed.get("deployment_generation") == incumbent.get("deployment_generation")
                or (failed.get("process_run_id") is not None and not isinstance(failed["process_run_id"], str))
            ):
                return False
        inspected = client.inspect_supervisor()
        data = inspected.data
        if source_action and inspected.result_code in {"UNAVAILABLE", "UNKNOWN", "IN_PROGRESS"}:
            return None
        if inspected.result_code != "OK" or not isinstance(data, dict) or data.get("supervisor_incarnation") != inspected.supervisor_generation:
            return False
        receipt = client.read_worker_operation(step["operation_id"])
        result = receipt.data
        if source_action and receipt.result_code in {"UNAVAILABLE", "UNKNOWN", "NOT_FOUND"}:
            return None
        if receipt.supervisor_generation != inspected.supervisor_generation or not isinstance(result, dict) or result.get("operation_id") != step["operation_id"] or result.get("intent_digest") != step["expected_digest"]:
            return False
        if receipt.result_code in {"IN_PROGRESS", "DRAINING", "RESTARTING"}:
            return None
        inspected = client.inspect_supervisor()
        data = inspected.data
        if source_action and inspected.result_code in {"UNAVAILABLE", "UNKNOWN", "IN_PROGRESS"}:
            return None
        if inspected.result_code != "OK" or not isinstance(data, dict) or data.get("supervisor_incarnation") != receipt.supervisor_generation or inspected.supervisor_generation != receipt.supervisor_generation:
            return False
        if name in {"drain", "recovery_drain"}:
            valid = receipt.result_code == "STOPPED" and data.get("status") == "STOPPED" and data.get("alive") is False and data.get("worker") is None and data.get("selected_runtime_id") == expected_runtime
        else:
            actual, accepted = data.get("worker"), result.get("worker")
            if not isinstance(actual, dict) or not isinstance(accepted, dict):
                return False
            valid = receipt.result_code == "RUNNING" and data.get("status") == "RUNNING" and data.get("alive") is True and actual.get("runtime_id") == accepted.get("runtime_id") == expected_runtime and actual.get("deployment_generation") == accepted.get("deployment_generation")
            if valid and actual.get("process_run_id") is None:
                return None
            valid = valid and isinstance(actual.get("process_run_id"), str) and accepted.get("process_run_id") in (None, actual.get("process_run_id"))
            if recovery:
                assert isinstance(failed, dict)
                valid = (
                    valid
                    and result.get("status") == "RUNNING"
                    and data.get("selected_runtime_id") == expected_runtime
                    and actual.get("deployment_generation") not in (failed["deployment_generation"], incumbent.get("deployment_generation"))
                    and actual.get("process_run_id") not in (failed.get("process_run_id"), incumbent.get("process_run_id"))
                )
        if not valid:
            return False
        if source_action and name in {"start", "recovery"}:
            # A retained launch receipt alone cannot attest to the installed
            # source. Reobserve the exact live child and its verified READY
            # binding before adopting the reconstructed Supervisor incarnation.
            assert isinstance(staged, dict) and isinstance(actual, dict)
            observed = client.observe_worker()
            body = observed.data
            if observed.result_code in {"UNAVAILABLE", "UNKNOWN", "IN_PROGRESS"}:
                return None
            if observed.result_code != "OK" or observed.supervisor_generation != inspected.supervisor_generation or not isinstance(body, dict):
                return False
            if body.get("binding_status") == "UNAVAILABLE":
                return None
            if body.get("binding_status") != "VERIFIED":
                return False
            binding, worker = body.get("runtime_binding"), body.get("observation")
            if not isinstance(binding, dict) or not isinstance(worker, dict):
                return False
            expected_binding = previous if recovery else staged
            assert isinstance(expected_binding, dict)
            fields = (("runtime_id", "runtime_id" if recovery else "installed_runtime_id"), ("content_identity", "content_identity"), ("source_sha", "source_sha"), ("distribution_version", "distribution_version"), ("lock_sha256", "lock_sha256"))
            if any(not isinstance(expected_binding.get(expected), str) or binding.get(field) != expected_binding[expected] for field, expected in fields):
                return False
            if any(binding.get(field) != actual.get(field) or worker.get(field) != actual.get(field) for field in ("process_run_id", "deployment_generation")):
                return False
            if recovery:
                artifact = worker.get("artifact")
                if not isinstance(artifact, dict):
                    return False
                for field, origin, expected in (("source_revision", "build_embedded", "source_sha"), ("distribution_version", "installed_distribution_metadata", "distribution_version")):
                    fact = artifact.get(field)
                    if not isinstance(fact, dict) or fact.get("available") is not True or fact.get("origin") != origin or fact.get("value") != expected_binding[expected]:
                        return False
            worker_observation = worker
        actual_worker = data.get("worker")
        return RetainedSupervisorEvidence(inspected.supervisor_generation, result, actual_worker if isinstance(actual_worker, dict) else None, worker_observation)

    def _reconcile_attention_recovery_drain(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> OperationRecord:
        """Settle an exited failed child without launching an expired recovery."""
        if record.phase is not OperationPhase.NEEDS_ATTENTION:
            return self._settle(record, "NEEDS_ATTENTION", str(snapshot.get("reason", "worker_lifecycle")))
        if self._authority_refusal(record) is not None:
            return record
        steps = snapshot.get("steps")
        if not isinstance(steps, dict) or "recovery" in steps:
            return record
        # No other possibly sent effect may be abandoned by this resolution.
        if any(name not in {"prepare", "drain", "start", "recovery_drain"} or not isinstance(step, dict) or (name != "recovery_drain" and step.get("state") != "CONFIRMED") for name, step in steps.items()):
            return record
        evidence = self._retained_supervisor_evidence(record, snapshot, client, attention_recovery=True)
        if not isinstance(evidence, RetainedSupervisorEvidence) or evidence.worker is not None:
            return record
        step = steps.get("recovery_drain")
        if not isinstance(step, dict) or self._authority_refusal(record) is not None:
            return record
        step.update(state="CONFIRMED", observation=evidence.receipt, observed_at=time.time())
        snapshot.update(current_supervisor_generation=evidence.supervisor_generation, recovery_state="NOT_LAUNCHED")
        # The original deadlines stay untouched. _finish first persists the
        # correlated terminal snapshot, then resolves the operation row with
        # the original effect identity. Failure of either write retains its lane.
        return self._finish(record, version, snapshot, "FAILED", "failed_candidate_stopped_recovery_not_launched")

    def _reconcile_attention_recovery(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> OperationRecord:
        """Resolve only a retained recovery using reads, even after its deadline."""
        if record.phase is not OperationPhase.NEEDS_ATTENTION:
            return self._settle(record, "NEEDS_ATTENTION", str(snapshot.get("reason", "worker_lifecycle")))
        if self._authority_refusal(record) is not None:
            return record
        evidence = self._retained_supervisor_evidence(record, snapshot, client, attention_recovery=True)
        if not isinstance(evidence, RetainedSupervisorEvidence) or evidence.worker is None or evidence.observation is None:
            return record
        worker = evidence.observation
        lifecycle, invocations, heartbeat = worker.get("lifecycle"), worker.get("invocations"), worker.get("heartbeat")
        if (
            not isinstance(lifecycle, dict)
            or not isinstance(invocations, dict)
            or not isinstance(heartbeat, dict)
            or lifecycle.get("state") != "running"
            or lifecycle.get("startup_reconciled") is not True
            or lifecycle.get("daemon_serving") is not True
            or invocations.get("state") != "running"
            or heartbeat.get("available") is not True
            or type(heartbeat.get("sequence")) is not int
            or not str(heartbeat.get("stage", "")).startswith("producer:")
        ):
            return record
        identity = {field: evidence.worker.get(field) for field in ("process_run_id", "deployment_generation", "runtime_id")}
        retained = snapshot.get("recovery_read_only_worker")
        if retained is not None and retained != identity:
            return record
        # Do not reuse candidate/pre-expiry heartbeat values or mix lifetimes.
        beats = snapshot.get("recovery_read_only_heartbeats", [])
        if not isinstance(beats, list) or any(type(value) is not int for value in beats) or (beats and heartbeat["sequence"] <= beats[-1]):
            return record
        if self._authority_refusal(record) is not None:
            return record
        beats.append(heartbeat["sequence"])
        snapshot.update(current_supervisor_generation=evidence.supervisor_generation, recovery_read_only_worker=identity, recovery_read_only_heartbeats=beats, restored_worker=evidence.worker, heartbeats=beats)
        steps = snapshot["steps"]
        assert isinstance(steps, dict) and isinstance(steps["recovery"], dict)
        steps["recovery"].update(state="CONFIRMED", observation=evidence.receipt, observed_at=time.time())
        if len(beats) >= 2:
            snapshot["recovery_state"] = "CONFIRMED"
            return self._finish(record, version, snapshot, "FAILED", "new_worker_failed_previous_runtime_restored")
        self._save(record, version, snapshot)
        return record

    def _release_reboot_evidence(self, record: OperationRecord, snapshot: dict[str, object], client: WorkerNetworkClient) -> dict[str, object] | None:
        """Require original incumbent drain and affirmative current unready evidence."""
        from auto_coder.worker_supervisor import WorkerSupervisor

        incumbent, failed, staged, steps = (snapshot.get(key) for key in ("incumbent", "new_worker", "staged", "steps"))
        if not all(isinstance(value, dict) for value in (incumbent, failed, staged, steps)):
            return None
        assert isinstance(incumbent, dict) and isinstance(failed, dict) and isinstance(staged, dict) and isinstance(steps, dict)
        drain = steps.get("drain")
        if not isinstance(drain, dict) or drain.get("state") != "CONFIRMED" or drain.get("operation_id") != "d-" + record.operation_id or not isinstance(incumbent.get("process_run_id"), str):
            return None
        receipt = drain.get("observation")
        digest = WorkerSupervisor._digest(
            {
                "operation": "REQUEST_GRACEFUL_STOP",
                "expected_supervisor_incarnation": incumbent.get("supervisor_generation"),
                "expected_process_run_id": incumbent["process_run_id"],
                "expected_generation": incumbent.get("deployment_generation"),
                "expected_runtime_id": incumbent.get("runtime_id"),
                "runtime_id": None,
            }
        )
        if not isinstance(receipt, dict) or receipt.get("status") != "STOPPED" or receipt.get("operation_id") != drain["operation_id"] or receipt.get("intent_digest") != digest or drain.get("expected_digest") != digest:
            return None
        inspected = client.inspect_supervisor()
        data = inspected.data
        current = data.get("worker") if isinstance(data, dict) else None
        generation = snapshot.get("current_supervisor_generation", incumbent.get("supervisor_generation"))
        if (
            inspected.result_code != "OK"
            or inspected.supervisor_generation != generation
            or not isinstance(data, dict)
            or data.get("supervisor_incarnation") != generation
            or data.get("alive") is not True
            or data.get("status") != "RUNNING"
            or not isinstance(current, dict)
            or any(current.get(key) != failed.get(key) for key in ("process_run_id", "deployment_generation", "runtime_id"))
        ):
            return None
        observed = client.observe_worker()
        body = observed.data
        if observed.result_code != "OK" or observed.supervisor_generation != generation or not isinstance(body, dict) or body.get("binding_status") != "VERIFIED":
            return None
        worker, binding = body.get("observation"), body.get("runtime_binding")
        if not isinstance(worker, dict) or not isinstance(binding, dict) or not isinstance(worker.get("process_run_id"), str) or any(worker.get(key) != current.get(key) or binding.get(key) != current.get(key) for key in ("process_run_id", "deployment_generation")):
            return None
        if any(binding.get(key) != staged.get(expected) for key, expected in (("runtime_id", "installed_runtime_id"), ("content_identity", "content_identity"), ("source_sha", "source_sha"), ("distribution_version", "distribution_version"), ("lock_sha256", "lock_sha256"))):
            return None
        lifecycle = worker.get("lifecycle")
        if not isinstance(lifecycle, dict) or lifecycle.get("state") != "running" or lifecycle.get("startup_reconciled") is not False or lifecycle.get("daemon_serving") is not False:
            return None
        return {
            "process_run_id": worker["process_run_id"],
            "deployment_generation": worker["deployment_generation"],
            "runtime_id": current["runtime_id"],
            "supervisor_generation": generation,
            "drain_operation_id": drain["operation_id"],
            "drain_digest": digest,
            "startup_reconciled": False,
            "daemon_serving": False,
            "observed_at": time.time(),
        }

    def _reserve_expired_release_recovery(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> OperationRecord:
        """Keep the original lane while recovering an unready release candidate."""
        failed = snapshot.get("new_worker")
        incumbent = snapshot.get("incumbent")
        previous = snapshot.get("previous_binding")
        staged = snapshot.get("staged")
        inspected = client.inspect_supervisor()
        data = inspected.data
        worker = data.get("worker") if isinstance(data, dict) else None
        if (
            record.intent.action != "deploy_release_source"
            or snapshot.get("previous_runtime_verified") is not True
            or snapshot.get("recovery_attempts") != 0
            or not isinstance(previous, dict)
            or not isinstance(incumbent, dict)
            or previous.get("runtime_id") != incumbent.get("runtime_id")
            or not isinstance(staged, dict)
            or not isinstance(failed, dict)
            or not isinstance(worker, dict)
            or not isinstance(data, dict)
            or inspected.result_code != "OK"
            or data.get("supervisor_incarnation") != inspected.supervisor_generation
            or inspected.supervisor_generation != snapshot.get("current_supervisor_generation", incumbent.get("supervisor_generation"))
            or worker.get("runtime_id") != failed.get("runtime_id")
            or failed.get("runtime_id") == incumbent.get("runtime_id")
            or worker.get("deployment_generation") != failed.get("deployment_generation")
            or (failed.get("process_run_id") is not None and worker.get("process_run_id") != failed.get("process_run_id"))
            or staged.get("installed_runtime_id") != failed.get("runtime_id")
        ):
            return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "failed_release_lifetime_unverified")
        alive = data.get("alive")
        if alive is True and data.get("status") == "RUNNING" and (worker.get("process_run_id") is None or isinstance(worker.get("process_run_id"), str)):
            evidence = self._release_reboot_evidence(record, snapshot, client)
            if evidence is None:
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "release_reboot_eligibility_unproven")
            snapshot["reboot_eligibility"] = evidence
            method = "REBOOT"
        elif alive is False:
            method = "EXITED"
        else:
            return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "failed_release_lifetime_unverified")
        if self._authority_refusal(record) is not None or not self.runtime.retained_effect_permitted(record):
            return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "recovery_authority_unavailable")
        if failed.get("process_run_id") is None and isinstance(worker.get("process_run_id"), str):
            failed["process_run_id"] = worker["process_run_id"]
        snapshot.update(stage="RESTORING", recovery_attempts=1, recovery_state="ATTEMPTED", recovery_method=method, deadline=time.time() + 300)
        self._save(record, version, snapshot)
        return record

    def _step(self, record: OperationRecord, version: int, snapshot: dict[str, object], name: str, client: WorkerNetworkClient, operation: str, runtime_id: str | None) -> OperationRecord:
        steps = snapshot["steps"]
        assert isinstance(steps, dict)
        identity = self._original_supervisor(client, snapshot)
        if identity != "SAME":
            # Reconstruction can occur after _process_locked's first check.
            # Its next iteration must apply the original retained-receipt gate.
            return record
        if name not in steps:
            if not self.runtime.retained_effect_permitted(record):
                return record
            fence = snapshot["incumbent"]
            assert isinstance(fence, dict)
            refusal = self._authority_refusal(record)
            if refusal is not None:
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", refusal)
            if operation == "RECOVER_PREVIOUS_REBOOT" and self._release_reboot_evidence(record, snapshot, client) is None:
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "release_reboot_eligibility_unproven")
            first_replacement = name == "drain" or (name == "start" and "drain" not in steps)
            if name == "start":
                observed = self._inspection(client)
                if observed is None or observed[0].process_run_id is not None or observed[1].get("worker") is not None or observed[1].get("status") != "STOPPED":
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "old_os_lifetime_exit_unproven")
                if observed[0].runtime_id != fence["runtime_id"]:
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "old_runtime_selection_changed")
            if first_replacement and record.intent.action in SOURCE_ACTIONS:
                try:
                    if record.intent.action == "deploy_beta_source":
                        original = json.loads(str(snapshot["accepted_evidence"]))
                        currently_selected = self._source().read(str(original["source_sha"])).canonical()
                        selection = json.loads(currently_selected)["selection_oid"]
                    else:
                        identity_data = snapshot.get("release_identity")
                        if not isinstance(identity_data, dict) or any(not isinstance(value, str) for value in identity_data.values()):
                            raise SourceReleaseError("original release identity unavailable")
                        release_identity = ReleaseWorkerEvidenceIdentity(**cast(dict[str, str], identity_data))
                        currently_selected = self._release_evidence(self._release().read(release_identity))
                        selection = json.loads(currently_selected)["catalog_oid"]
                except SourceReleaseError as exc:
                    if str(exc).startswith("SUPERSEDED"):
                        return self._finish(record, version, snapshot, "SUPERSEDED", "original_release_operation_superseded")
                    snapshot["revalidation_observation"] = {"checked_at": time.time(), "availability": "unavailable"}
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "source_selection_revalidation_unavailable")
                except (SourceEvidenceError, RuntimeError, OSError, KeyError, ValueError, TypeError):
                    snapshot["revalidation_observation"] = {"checked_at": time.time(), "availability": "unavailable"}
                    reason = "beta_selection_revalidation_unavailable" if record.intent.action == "deploy_beta_source" else "source_selection_revalidation_unavailable"
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", reason)
                snapshot["revalidation_observation"] = {"checked_at": time.time(), "availability": "confirmed", "selection_oid": selection, "matched": currently_selected == snapshot["accepted_evidence"]}
                if currently_selected != snapshot["accepted_evidence"]:
                    reason = "original_beta_selection_or_proof_changed" if record.intent.action == "deploy_beta_source" else "original_source_selection_or_proof_changed"
                    return self._finish(record, version, snapshot, "SUPERSEDED", reason)
            effect_id = {"prepare": "p", "drain": "d", "start": "s", "recovery": "r", "recovery_drain": "f"}[name] + "-" + record.operation_id
            steps[name] = {"operation_id": effect_id, "state": "SENT_UNKNOWN", "sent_at": time.time(), "observation": None, "owner_generation": record.effect.owner_generation, "attempt_id": record.effect.attempt_id, "correlation_identity": record.effect.correlation_identity}
            if name == "recovery":
                snapshot["recovery_state"] = "UNKNOWN"
            if name != "prepare":
                from auto_coder.worker_supervisor import WorkerSupervisor

                failed = snapshot.get("new_worker")
                expected_run = fence["process_run_id"] if name == "drain" else failed.get("process_run_id") if name in {"recovery", "recovery_drain"} and isinstance(failed, dict) else None
                expected_generation = fence["deployment_generation"] if name == "drain" else failed.get("deployment_generation") if name in {"recovery", "recovery_drain"} and isinstance(failed, dict) else None
                staged = snapshot.get("staged")
                selected_runtime = fence["runtime_id"] if name in {"drain", "start"} else staged.get("installed_runtime_id") if isinstance(staged, dict) else None
                request = {"operation": operation, "expected_supervisor_incarnation": client.supervisor_generation, "expected_process_run_id": expected_run, "expected_generation": expected_generation, "expected_runtime_id": selected_runtime, "runtime_id": runtime_id}
                steps[name]["expected_digest"] = WorkerSupervisor._digest(request)
            if not self._save(record, version, snapshot):
                return record
            # Once the possibly-sent step is durable, an authority loss must
            # leave its original lane owned, not treat it as retryable work.
            if not self.runtime.retained_effect_permitted(record) or self._authority_refusal(record) is not None:
                return record
            if first_replacement and record.intent.action == "deploy_release_source":
                # Close the journal-write window before the first worker effect.
                # A changed or unreadable selection leaves the persisted
                # possibly-sent step owned; it is never dispatched speculatively.
                try:
                    identity_data = snapshot.get("release_identity")
                    if not isinstance(identity_data, dict) or any(not isinstance(value, str) for value in identity_data.values()):
                        return record
                    selected = self._release_evidence(self._release().read(ReleaseWorkerEvidenceIdentity(**cast(dict[str, str], identity_data))))
                    if selected != snapshot["accepted_evidence"]:
                        return record
                except (SourceReleaseError, RuntimeError, OSError, ValueError, TypeError):
                    return record
            if operation == "RECOVER_PREVIOUS_REBOOT" and self._release_reboot_evidence(record, snapshot, client) is None:
                return record
            if name == "prepare":
                response = client.prepare_version(effect_id, str(snapshot["source_sha"]))
            else:
                failed = snapshot.get("new_worker")
                expected_run = fence["process_run_id"] if name == "drain" else failed.get("process_run_id") if name in {"recovery", "recovery_drain"} and isinstance(failed, dict) else None
                expected_generation = fence["deployment_generation"] if name == "drain" else failed.get("deployment_generation") if name in {"recovery", "recovery_drain"} and isinstance(failed, dict) else None
                staged = snapshot.get("staged")
                selected_runtime = fence["runtime_id"] if name in {"drain", "start"} else staged.get("installed_runtime_id") if isinstance(staged, dict) else None
                if (expected_run is not None and not isinstance(expected_run, str)) or (expected_generation is not None and not isinstance(expected_generation, str)) or (selected_runtime is not None and not isinstance(selected_runtime, str)):
                    return record
                response = client.dispatch_worker_operation(effect_id, operation, process_run_id=expected_run, deployment_generation=expected_generation, selected_runtime_id=selected_runtime, runtime_id=runtime_id)
        else:
            effect_id = steps[name]["operation_id"]
            response = client.read_preparation(effect_id) if name == "prepare" else client.read_worker_operation(effect_id)
        # Only signed, identity-bound Supervisor observations may advance a stage.
        if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED", "CONFLICT", "UNAVAILABLE", "UNKNOWN", "DRAINING", "RESTARTING", "IN_PROGRESS", "NEEDS_ATTENTION"} and isinstance(response.data, dict) and response.data.get("operation_id", effect_id) == effect_id:
            refreshed = self.runtime.journal.lifecycle_snapshot(record.operation_id)
            if refreshed is None:
                return record
            revision, snapshot = refreshed
            steps = snapshot["steps"]
            assert isinstance(steps, dict)
            step = steps[name]
            if name != "prepare" and response.data.get("intent_digest") != step.get("expected_digest"):
                # A POST acknowledgement has no durable Supervisor digest.
                # Only its original read_operation receipt can settle a step.
                return record
            if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED"}:
                step.update(state="CONFIRMED", observation=response.data, observed_at=time.time())
            elif response.result_code in {"CONFLICT", "UNAVAILABLE", "UNKNOWN", "NEEDS_ATTENTION"}:
                step.update(state="SENT_UNKNOWN", observation=None)
            self._save(record, revision, snapshot)
        return record

    def process(self, record: OperationRecord) -> OperationRecord | None:
        bound = self.runtime._record_client(record)
        if bound is None:
            return record
        # The network client caches the signed Supervisor incarnation. Keep an
        # inspect/read/dispatch sequence bound to that same client generation
        # while concurrent management reads and admissions use the client.
        with self.runtime._worker_locks[bound[0]]:
            return self._process_locked(record)

    def _process_locked(self, record: OperationRecord) -> OperationRecord | None:
        bound = self.runtime._record_client(record)
        if bound is None:
            return record
        _, client = bound
        try:
            persisted = self.runtime.journal.lifecycle_snapshot(record.operation_id)
            if persisted is None:
                return self.runtime._hold(record, "missing_original_lifecycle_evidence") or record
            version, snapshot = persisted
            stage = snapshot.get("stage")
            identity = self._original_supervisor(client, snapshot)
            if identity == "CHANGED" and stage not in TERMINAL_STAGES and stage != "NEEDS_ATTENTION":
                rebound = self._rebind_retained_supervisor(record, version, snapshot, client)
                if rebound:
                    return record
                deadline = snapshot.get("deadline")
                if rebound is None and isinstance(deadline, (int, float)) and time.time() <= deadline:
                    return record
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "original_supervisor_incarnation_changed")
            if stage in TERMINAL_STAGES or stage == "NEEDS_ATTENTION":
                # The lifecycle snapshot and the operation row use separate
                # transactions. Reconstruct an interrupted terminal write.
                if stage == "NEEDS_ATTENTION":
                    steps = snapshot.get("steps")
                    if record.intent.action == "deploy_release_source" and isinstance(steps, dict) and "recovery" in steps:
                        return self._reconcile_attention_recovery(record, version, snapshot, client)
                    if record.intent.action == "deploy_release_source" and isinstance(steps, dict) and "recovery_drain" in steps:
                        return self._reconcile_attention_recovery_drain(record, version, snapshot, client)
                    self._observe_uncertain_steps(record, version, snapshot, client)
                return self._settle(record, str(stage), str(snapshot.get("reason", "worker_lifecycle")))
            if record.effect.state is EffectState.NOT_SENT:
                refusal = self._authority_refusal(record)
                if refusal is not None:
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", refusal)
                correlation = hashlib.sha256((record.operation_id + record.intent.fingerprint()).encode()).hexdigest()
                begun = self.runtime.journal.begin_effect(record.operation_id, record.revision, owner_attempt_id=record.operation_id, attempt_id=record.operation_id, correlation_identity=correlation)
                if begun.status is not MutationStatus.UPDATED or begun.record is None:
                    return begun.record or record
                record = begun.record
            if stage == "ACCEPTED":
                snapshot["stage"] = "PREPARING" if record.intent.action in SOURCE_ACTIONS else "DRAINING" if record.intent.action in {"stop_worker", "restart_worker"} else "STARTING"
                self._save(record, version, snapshot)
                return record
            deadline = snapshot.get("deadline")
            if not isinstance(deadline, (int, float)) or time.time() > deadline:
                if stage == "RECONCILING" and record.intent.action == "deploy_release_source" and isinstance(deadline, (int, float)):
                    return self._reserve_expired_release_recovery(record, version, snapshot, client)
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "lifecycle_deadline_exceeded")
            if stage == "PREPARING":
                steps = snapshot["steps"]
                assert isinstance(steps, dict)
                step = steps.get("prepare")
                if not isinstance(step, dict) or step.get("state") != "CONFIRMED":
                    return self._step(record, version, snapshot, "prepare", client, "", None)
                observation = step["observation"]
                assert isinstance(observation, dict)
                original = json.loads(str(snapshot["accepted_evidence"]))
                if observation.get("status") != "READY":
                    return self._finish(record, version, snapshot, "FAILED", "worker_version_preparation_failed")
                if (
                    any(observation.get(key) != original[other] for key, other in (("source_sha", "source_sha"), ("lock_sha256", "lock_sha256"), ("distribution_version", "distribution_version")))
                    or observation.get("runtime_profile") != "python-3.12"
                    or not isinstance(observation.get("installed_runtime_id"), str)
                ):
                    return self._finish(record, version, snapshot, "FAILED", "installed_package_provenance_mismatch")
                incumbent = snapshot["incumbent"]
                assert isinstance(incumbent, dict)
                snapshot.update(staged={key: observation[key] for key in ("source_sha", "lock_sha256", "distribution_version", "content_identity", "installed_runtime_id", "runtime_profile")}, stage="DRAINING" if incumbent["process_run_id"] is not None else "STARTING", deadline=time.time() + 300)
                self._save(record, version, snapshot)
                return record
            if stage == "DRAINING":
                steps = snapshot["steps"]
                assert isinstance(steps, dict)
                step = steps.get("drain")
                if not isinstance(step, dict) or step.get("state") != "CONFIRMED":
                    return self._step(record, version, snapshot, "drain", client, "REQUEST_GRACEFUL_STOP", None)
                observation = step["observation"]
                if not isinstance(observation, dict) or observation.get("status") != "STOPPED":
                    return record
                inspected = client.inspect_supervisor()
                if not isinstance(inspected.data, dict) or inspected.data.get("alive") is not False or inspected.data.get("status") != "STOPPED":
                    return record
                if record.intent.action == "stop_worker":
                    return self._finish(record, version, snapshot, "SUCCEEDED", "incumbent_graceful_receipt_and_os_exit_verified")
                snapshot.update(stage="STARTING", deadline=time.time() + 180)
                self._save(record, version, snapshot)
                return record
            if stage == "STARTING":
                steps = snapshot["steps"]
                assert isinstance(steps, dict)
                step = steps.get("start")
                if not isinstance(step, dict) or step.get("state") != "CONFIRMED":
                    staged = snapshot.get("staged")
                    incumbent = snapshot["incumbent"]
                    assert isinstance(incumbent, dict)
                    runtime_id = staged.get("installed_runtime_id") if isinstance(staged, dict) else incumbent["runtime_id"]
                    if not isinstance(runtime_id, str):
                        return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "no_independently_selected_runtime")
                    return self._step(record, version, snapshot, "start", client, "START", runtime_id)
                observation = step["observation"]
                if not isinstance(observation, dict) or observation.get("status") != "RUNNING" or not isinstance(observation.get("worker"), dict):
                    return record
                snapshot.update(stage="RECONCILING", new_worker=observation["worker"], deadline=time.time() + 150)
                self._save(record, version, snapshot)
                return record
            if stage == "RECONCILING":
                inspected = client.inspect_supervisor()
                new_worker = snapshot.get("new_worker")
                if not isinstance(new_worker, dict) or not isinstance(inspected.data, dict):
                    return record
                observed_worker = inspected.data.get("worker")
                if (
                    isinstance(observed_worker, dict)
                    and observed_worker.get("deployment_generation") == new_worker.get("deployment_generation")
                    and observed_worker.get("runtime_id") == new_worker.get("runtime_id")
                    and new_worker.get("process_run_id") is None
                    and isinstance(observed_worker.get("process_run_id"), str)
                ):
                    # START may return before the child registers its real run
                    # ID. Persist the signed OS lifetime before crash recovery.
                    new_worker["process_run_id"] = observed_worker["process_run_id"]
                    self._save(record, version, snapshot)
                    return record
                if (
                    record.intent.action in SOURCE_ACTIONS
                    and snapshot.get("previous_runtime_verified") is True
                    and snapshot.get("recovery_attempts") == 0
                    and isinstance(observed_worker, dict)
                    and inspected.data.get("alive") is False
                    and observed_worker.get("deployment_generation") == new_worker.get("deployment_generation")
                ):
                    # Confirm the failed child lifetime through the actual
                    # Supervisor's OS observation before reserving recovery.
                    snapshot.update(stage="RESTORING", recovery_attempts=1, recovery_state="ATTEMPTED", recovery_method="EXITED", deadline=time.time() + 150)
                    self._save(record, version, snapshot)
                    return record
                if (
                    not isinstance(observed_worker, dict)
                    or any(observed_worker.get(key) != new_worker.get(key) for key in ("deployment_generation", "runtime_id"))
                    or (new_worker.get("process_run_id") is not None and observed_worker.get("process_run_id") != new_worker.get("process_run_id"))
                    or inspected.data.get("alive") is not True
                ):
                    return record
                observation = client.observe_worker()
                if not isinstance(observation.data, dict):
                    return record
                worker = observation.data.get("observation")
                if not isinstance(worker, dict) or worker.get("deployment_generation") != new_worker.get("deployment_generation") or worker.get("process_run_id") != observed_worker.get("process_run_id"):
                    return record
                staged = snapshot.get("staged")
                if isinstance(staged, dict):
                    binding = observation.data.get("runtime_binding")
                    if (
                        observation.data.get("binding_status") != "VERIFIED"
                        or not isinstance(binding, dict)
                        or any(binding.get(key) != staged.get(other) for key, other in (("runtime_id", "installed_runtime_id"), ("content_identity", "content_identity"), ("lock_sha256", "lock_sha256"), ("source_sha", "source_sha"), ("distribution_version", "distribution_version")))
                    ):
                        return record
                    if record.intent.action == "deploy_release_source":
                        artifact = worker.get("artifact")
                        source = artifact.get("source_revision") if isinstance(artifact, dict) else None
                        artifact_version = artifact.get("distribution_version") if isinstance(artifact, dict) else None
                        if (
                            not isinstance(source, dict)
                            or source.get("origin") != "build_embedded"
                            or source.get("available") is not True
                            or source.get("value") != staged["source_sha"]
                            or not isinstance(artifact_version, dict)
                            or artifact_version.get("origin") != "installed_distribution_metadata"
                            or artifact_version.get("available") is not True
                            or artifact_version.get("value") != staged["distribution_version"]
                        ):
                            return record
                failure = observed_startup_failure(worker)
                if (
                    record.intent.action == "deploy_release_source"
                    and snapshot.get("previous_runtime_verified") is True
                    and snapshot.get("recovery_attempts") == 0
                    and failure is not None
                    and observation.result_code == "OK"
                    and observation.supervisor_generation == inspected.supervisor_generation
                    and isinstance(observation.data.get("runtime_binding"), dict)
                    and all(observation.data["runtime_binding"].get(key) == worker.get(key) for key in ("process_run_id", "deployment_generation"))
                ):
                    snapshot.update(stage="RECOVERY_DRAINING", recovery_attempts=1, recovery_state="ATTEMPTED", startup_failure=asdict(failure), deadline=time.time() + 300)
                    self._save(record, version, snapshot)
                    return record
                lifecycle, invocation, heartbeat = worker.get("lifecycle"), worker.get("invocations"), worker.get("heartbeat")
                if (
                    not isinstance(lifecycle, dict)
                    or not isinstance(invocation, dict)
                    or not isinstance(heartbeat, dict)
                    or lifecycle.get("state") != "running"
                    or lifecycle.get("startup_reconciled") is not True
                    or lifecycle.get("daemon_serving") is not True
                    or invocation.get("state") != "running"
                    or heartbeat.get("available") is not True
                    or type(heartbeat.get("sequence")) is not int
                    or not str(heartbeat.get("stage", "")).startswith("producer:")
                ):
                    return record
                beats = snapshot["heartbeats"]
                assert isinstance(beats, list)
                if not beats or heartbeat["sequence"] > beats[-1]:
                    beats.append(heartbeat["sequence"])
                    if len(beats) >= 2:
                        return self._finish(record, version, snapshot, "SUCCEEDED", "real_installed_worker_and_advancing_producer_heartbeats_verified")
                    self._save(record, version, snapshot)
                return record
            if stage == "RECOVERY_DRAINING":
                steps = snapshot["steps"]
                assert isinstance(steps, dict)
                step = steps.get("recovery_drain")
                if not isinstance(step, dict) or step.get("state") != "CONFIRMED":
                    return self._step(record, version, snapshot, "recovery_drain", client, "REQUEST_GRACEFUL_STOP", None)
                observation = step.get("observation")
                inspected = client.inspect_supervisor()
                failed = snapshot.get("new_worker")
                if (
                    not isinstance(observation, dict)
                    or observation.get("status") != "STOPPED"
                    or not isinstance(inspected.data, dict)
                    or inspected.data.get("status") != "STOPPED"
                    or inspected.data.get("alive") is not False
                    or inspected.data.get("worker") is not None
                    or not isinstance(failed, dict)
                    or inspected.data.get("selected_runtime_id") != failed.get("runtime_id")
                ):
                    return record
                snapshot.update(stage="RESTORING", deadline=time.time() + 150)
                self._save(record, version, snapshot)
                return record
            if stage == "RESTORING":
                steps = snapshot["steps"]
                assert isinstance(steps, dict)
                step = steps.get("recovery")
                if not isinstance(step, dict) or step.get("state") != "CONFIRMED":
                    prior = snapshot["incumbent"]
                    assert isinstance(prior, dict)
                    runtime_id = prior.get("runtime_id")
                    if not isinstance(runtime_id, str) or runtime_id == "bootstrap":
                        return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "previous_prepared_runtime_unavailable")
                    operation = "RECOVER_PREVIOUS_REBOOT" if snapshot.get("recovery_method") == "REBOOT" else "RECOVER_PREVIOUS"
                    return self._step(record, version, snapshot, "recovery", client, operation, runtime_id)
                observation = step.get("observation")
                if not isinstance(observation, dict) or observation.get("status") != "RUNNING" or not isinstance(observation.get("worker"), dict):
                    return record
                snapshot.update(stage="RECONCILING_PRIOR", restored_worker=observation["worker"], heartbeats=[], deadline=time.time() + 150)
                self._save(record, version, snapshot)
                return record
            if stage == "RECONCILING_PRIOR":
                inspected = client.inspect_supervisor()
                recovered = snapshot.get("restored_worker")
                if not isinstance(recovered, dict) or not isinstance(inspected.data, dict) or inspected.data.get("alive") is not True:
                    return record
                worker = inspected.data.get("worker")
                if not isinstance(worker, dict) or worker.get("deployment_generation") != recovered.get("deployment_generation"):
                    return record
                observed = client.observe_worker()
                if not isinstance(observed.data, dict) or observed.data.get("binding_status") != "VERIFIED":
                    return record
                observation = observed.data.get("observation")
                binding = observed.data.get("runtime_binding")
                previous = snapshot.get("previous_binding")
                if not isinstance(observation, dict) or not isinstance(binding, dict) or not isinstance(previous, dict) or any(binding.get(key) != previous.get(key) for key in ("runtime_id", "content_identity", "source_sha", "distribution_version", "lock_sha256")):
                    return record
                lifecycle, invocations, heartbeat = observation.get("lifecycle"), observation.get("invocations"), observation.get("heartbeat")
                if (
                    not isinstance(lifecycle, dict)
                    or not isinstance(invocations, dict)
                    or not isinstance(heartbeat, dict)
                    or lifecycle.get("startup_reconciled") is not True
                    or lifecycle.get("daemon_serving") is not True
                    or invocations.get("state") != "running"
                    or heartbeat.get("available") is not True
                    or type(heartbeat.get("sequence")) is not int
                    or not str(heartbeat.get("stage", "")).startswith("producer:")
                ):
                    return record
                beats = snapshot["heartbeats"]
                assert isinstance(beats, list)
                if not beats or heartbeat["sequence"] > beats[-1]:
                    beats.append(heartbeat["sequence"])
                    if len(beats) >= 2:
                        snapshot["recovery_state"] = "CONFIRMED"
                        reason = "new_beta_worker_failed_previous_runtime_restored" if record.intent.action == "deploy_beta_source" else "new_worker_failed_previous_runtime_restored"
                        return self._finish(record, version, snapshot, "FAILED", reason)
                    self._save(record, version, snapshot)
                return record
            return record
        except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
            # Never erase the original pre-effect snapshot on a transient error.
            return record

    def _observe_uncertain_steps(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> None:
        """An expired/revoked lane remains read-reconcilable, never re-dispatched."""
        if self._original_supervisor(client, snapshot) != "SAME":
            return
        steps = snapshot.get("steps")
        if not isinstance(steps, dict):
            return
        for name, step in steps.items():
            if not isinstance(step, dict) or step.get("state") != "SENT_UNKNOWN" or name not in {"prepare", "drain", "start", "recovery", "recovery_drain"}:
                continue
            effect_id = step.get("operation_id")
            if not isinstance(effect_id, str):
                continue
            response = client.read_preparation(effect_id) if name == "prepare" else client.read_worker_operation(effect_id)
            if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED"} and isinstance(response.data, dict) and response.data.get("operation_id") == effect_id:
                step.update(state="CONFIRMED", observation=response.data, observed_at=time.time())
                self._save(record, version, snapshot)
                return
