"""Authenticated, journal-fenced worker process controls and beta source activation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import StrictInt

from auto_coder.operating_policy import ActionClass
from auto_coder.util.gh_cache import GitHubGitDataClient

from .action_scopes import DEPLOY_SCOPE, READ_SCOPE
from .action_scopes import AcceptedAuthority as GrantAuthority
from .action_scopes import PendingAuthorityStatus
from .operations import ActionParameter, AdmissionStatus, EffectReceipt, EffectState, MutationStatus, OperationIntent, OperationPhase, OperationRecord, ReadStatus
from .source_evidence import BetaEvidenceReader, SourceEvidenceError
from .worker_network_client import WorkerNetworkClient, WorkerNetworkResult

if TYPE_CHECKING:
    from .mcp_runtime_tools import ManagementRuntime

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{40}\Z")
LIFECYCLE_ACTIONS = frozenset({"start_worker", "stop_worker", "restart_worker", "deploy_beta_source"})
TERMINAL_STAGES = frozenset({"SUCCEEDED", "FAILED", "SUPERSEDED"})


@dataclass(frozen=True)
class WorkerFence:
    supervisor_generation: str
    process_run_id: str | None
    deployment_generation: str | None
    runtime_id: str | None


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

        action = ActionClass.DEPLOYMENT if record.intent.action == "deploy_beta_source" else ActionClass.WORKER_CONTROL
        current = self.runtime._resolve(target_id, action)
        if not isinstance(current, ResolvedWorker) or current.target != record.intent.target:
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
    ) -> dict[str, object]:
        from .mcp_runtime_tools import ResolvedWorker

        grant = self.runtime._grant((READ_SCOPE, DEPLOY_SCOPE))
        values = (target_id, idempotency_key, expected_supervisor_generation)
        if (
            action not in LIFECYCLE_ACTIONS
            or any(not isinstance(value, str) or _ID.fullmatch(value) is None for value in values)
            or (expected_process_run_id is None) != (expected_deployment_generation is None)
            or any(value is not None and (not isinstance(value, str) or _ID.fullmatch(value) is None) for value in (expected_process_run_id, expected_deployment_generation, expected_runtime_id))
            or (action == "deploy_beta_source") != (isinstance(selected_git_sha, str) and _SHA.fullmatch(selected_git_sha) is not None)
        ):
            return self._answer("INVALID_REQUEST", "invalid_worker_lifecycle_request")
        resolved = self.runtime._resolve(target_id, ActionClass.INSPECT)
        if not isinstance(resolved, ResolvedWorker):
            return resolved
        if action == "deploy_beta_source" and resolved.target.channel != "beta":
            return self._answer("DENIED", "release_source_deployment_not_authorized")
        if action in {"stop_worker", "restart_worker"} and expected_process_run_id is None:
            return self._answer("INVALID_REQUEST", "running_worker_identity_required")
        if action == "start_worker" and expected_process_run_id is not None:
            return self._answer("INVALID_REQUEST", "start_requires_stopped_worker")
        params = (
            ActionParameter("target_id", target_id),
            ActionParameter("supervisor_generation", expected_supervisor_generation),
            ActionParameter("runtime_id", expected_runtime_id or "-"),
            ActionParameter("source_sha", selected_git_sha or "-"),
        )
        intent = OperationIntent(resolved.target, action, params, idempotency_key, expected_process_run_id or "-", expected_deployment_generation or "-")
        original = self.runtime._journal_authority(grant, self.runtime.grants.accepted_authority(grant))
        old = await asyncio.to_thread(self.runtime.journal.find_by_key, intent, original)
        if old.status is ReadStatus.UNAVAILABLE:
            return self._answer("UNAVAILABLE", "journal_unavailable")
        if old.record is not None:
            if old.record.intent_fingerprint != intent.fingerprint():
                return self._answer("CONFLICT", "idempotency_key_conflict", operation_id=old.record.operation_id)
            updated = await self.runtime.dispatcher.submit(old.record.operation_id)
            return self.project(updated or old.record)
        mutation = self.runtime._resolve(target_id, ActionClass.DEPLOYMENT if action == "deploy_beta_source" else ActionClass.WORKER_CONTROL)
        if not isinstance(mutation, ResolvedWorker):
            return mutation

        def inspect_locked() -> tuple[WorkerFence, dict[str, object]] | None:
            with self.runtime._worker_locks[mutation.client.config.channel]:
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
        now = time.time()
        incumbent_worker = inspected[1].get("worker")
        incumbent = incumbent_worker if isinstance(incumbent_worker, dict) else {}
        snapshot: dict[str, object] = {
            "schema_version": 1,
            "stage": "ACCEPTED",
            "action": action,
            "accepted_evidence": evidence,
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
        snapshot = persisted[1]
        return self._answer(
            str(snapshot["stage"]),
            record.safe_error_category or "worker_lifecycle",
            operation_id=record.operation_id,
            phase=record.phase.value,
            action=record.intent.action,
            channel=record.intent.target.channel,
            instance_id=record.intent.target.instance_id,
            source_sha=snapshot.get("source_sha"),
            revalidation_observation=snapshot.get("revalidation_observation"),
            incumbent=snapshot.get("incumbent"),
            staged=snapshot.get("staged"),
            new_worker=snapshot.get("new_worker"),
            steps=snapshot.get("steps"),
            heartbeats=snapshot.get("heartbeats"),
            recovery_attempts=snapshot.get("recovery_attempts"),
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

    def _rebind_bootstrap_supervisor(self, record: OperationRecord, version: int, snapshot: dict[str, object], client: WorkerNetworkClient) -> bool | None:
        """Adopt a reconstructed owner only through an exact durable effect receipt."""
        incumbent = snapshot.get("incumbent")
        steps = snapshot.get("steps")
        if record.intent.action not in {"start_worker", "stop_worker", "restart_worker"} or not isinstance(incumbent, dict) or incumbent.get("runtime_id") != "bootstrap" or not isinstance(steps, dict):
            return False
        name = "start" if "start" in steps else "drain"
        step = steps.get(name)
        if not isinstance(step, dict) or not isinstance(step.get("expected_digest"), str) or not isinstance(step.get("operation_id"), str):
            return False
        inspected = client.inspect_supervisor()
        data = inspected.data
        if inspected.result_code != "OK" or not isinstance(data, dict) or data.get("supervisor_incarnation") != inspected.supervisor_generation:
            return False
        receipt = client.read_worker_operation(step["operation_id"])
        result = receipt.data
        if receipt.supervisor_generation != inspected.supervisor_generation or not isinstance(result, dict) or result.get("operation_id") != step["operation_id"] or result.get("intent_digest") != step["expected_digest"]:
            return False
        if receipt.result_code in {"IN_PROGRESS", "DRAINING"}:
            return None
        inspected = client.inspect_supervisor()
        data = inspected.data
        if inspected.result_code != "OK" or not isinstance(data, dict) or data.get("supervisor_incarnation") != receipt.supervisor_generation or inspected.supervisor_generation != receipt.supervisor_generation:
            return False
        if name == "drain":
            valid = receipt.result_code == "STOPPED" and data.get("status") == "STOPPED" and data.get("alive") is False and data.get("selected_runtime_id") == "bootstrap"
        else:
            actual, accepted = data.get("worker"), result.get("worker")
            if not isinstance(actual, dict) or not isinstance(accepted, dict):
                return False
            valid = receipt.result_code == "RUNNING" and data.get("status") == "RUNNING" and data.get("alive") is True and actual.get("runtime_id") == accepted.get("runtime_id") == "bootstrap" and actual.get("deployment_generation") == accepted.get("deployment_generation")
            if valid and actual.get("process_run_id") is None:
                return None
            valid = valid and isinstance(actual.get("process_run_id"), str) and accepted.get("process_run_id") in (None, actual.get("process_run_id"))
        if not valid:
            return False
        snapshot["current_supervisor_generation"] = inspected.supervisor_generation
        return self._save(record, version, snapshot)

    def _step(self, record: OperationRecord, version: int, snapshot: dict[str, object], name: str, client: WorkerNetworkClient, operation: str, runtime_id: str | None) -> OperationRecord:
        steps = snapshot["steps"]
        assert isinstance(steps, dict)
        identity = self._original_supervisor(client, snapshot)
        if identity == "CHANGED":
            return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "original_supervisor_incarnation_changed")
        if identity != "SAME":
            return record
        if name not in steps:
            fence = snapshot["incumbent"]
            assert isinstance(fence, dict)
            refusal = self._authority_refusal(record)
            if refusal is not None:
                return self._finish(record, version, snapshot, "NEEDS_ATTENTION", refusal)
            first_replacement = name == "drain" or (name == "start" and "drain" not in steps)
            if first_replacement and record.intent.action == "deploy_beta_source":
                try:
                    original = json.loads(str(snapshot["accepted_evidence"]))
                    currently_selected = self._source().read(str(original["source_sha"])).canonical()
                except (SourceEvidenceError, RuntimeError, OSError, KeyError, ValueError, TypeError):
                    snapshot["revalidation_observation"] = {"checked_at": time.time(), "availability": "unavailable"}
                    return self._finish(record, version, snapshot, "NEEDS_ATTENTION", "beta_selection_revalidation_unavailable")
                current_record = json.loads(currently_selected)
                snapshot["revalidation_observation"] = {"checked_at": time.time(), "availability": "confirmed", "selection_oid": current_record["selection_oid"], "matched": currently_selected == snapshot["accepted_evidence"]}
                if currently_selected != snapshot["accepted_evidence"]:
                    return self._finish(record, version, snapshot, "SUPERSEDED", "original_beta_selection_or_proof_changed")
            effect_id = {"prepare": "p", "drain": "d", "start": "s", "recovery": "r"}[name] + "-" + record.operation_id
            steps[name] = {"operation_id": effect_id, "state": "SENT_UNKNOWN", "sent_at": time.time(), "observation": None}
            if name != "prepare":
                from auto_coder.worker_supervisor import WorkerSupervisor

                failed = snapshot.get("new_worker")
                expected_run = fence["process_run_id"] if name == "drain" else failed.get("process_run_id") if name == "recovery" and isinstance(failed, dict) else None
                expected_generation = fence["deployment_generation"] if name == "drain" else failed.get("deployment_generation") if name == "recovery" and isinstance(failed, dict) else None
                staged = snapshot.get("staged")
                selected_runtime = fence["runtime_id"] if name in {"drain", "start"} else staged.get("installed_runtime_id") if isinstance(staged, dict) else None
                request = {"operation": operation, "expected_supervisor_incarnation": client.supervisor_generation, "expected_process_run_id": expected_run, "expected_generation": expected_generation, "expected_runtime_id": selected_runtime, "runtime_id": runtime_id}
                steps[name]["expected_digest"] = WorkerSupervisor._digest(request)
            if not self._save(record, version, snapshot):
                return record
            if name == "prepare":
                response = client.prepare_version(effect_id, str(snapshot["source_sha"]))
            else:
                failed = snapshot.get("new_worker")
                expected_run = fence["process_run_id"] if name == "drain" else failed.get("process_run_id") if name == "recovery" and isinstance(failed, dict) else None
                expected_generation = fence["deployment_generation"] if name == "drain" else failed.get("deployment_generation") if name == "recovery" and isinstance(failed, dict) else None
                staged = snapshot.get("staged")
                selected_runtime = fence["runtime_id"] if name in {"drain", "start"} else staged.get("installed_runtime_id") if isinstance(staged, dict) else None
                if (expected_run is not None and not isinstance(expected_run, str)) or (expected_generation is not None and not isinstance(expected_generation, str)) or (selected_runtime is not None and not isinstance(selected_runtime, str)):
                    return record
                response = client.dispatch_worker_operation(effect_id, operation, process_run_id=expected_run, deployment_generation=expected_generation, selected_runtime_id=selected_runtime, runtime_id=runtime_id)
        else:
            effect_id = steps[name]["operation_id"]
            response = client.read_preparation(effect_id) if name == "prepare" else client.read_worker_operation(effect_id)
        # Only signed, identity-bound Supervisor observations may advance a stage.
        if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED", "CONFLICT", "UNAVAILABLE", "UNKNOWN", "DRAINING", "IN_PROGRESS", "NEEDS_ATTENTION"} and isinstance(response.data, dict) and response.data.get("operation_id", effect_id) == effect_id:
            refreshed = self.runtime.journal.lifecycle_snapshot(record.operation_id)
            if refreshed is None:
                return record
            revision, snapshot = refreshed
            steps = snapshot["steps"]
            assert isinstance(steps, dict)
            step = steps[name]
            if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED"}:
                step.update(state="CONFIRMED", observation=response.data, observed_at=time.time())
            elif response.result_code in {"CONFLICT", "UNAVAILABLE", "UNKNOWN", "NEEDS_ATTENTION"}:
                step.update(state="SENT_UNKNOWN", observation=None)
            self._save(record, revision, snapshot)
        return record

    def process(self, record: OperationRecord) -> OperationRecord | None:
        lock = self.runtime._worker_locks.get(record.intent.target.channel)
        if lock is None:
            return record
        # The network client caches the signed Supervisor incarnation. Keep an
        # inspect/read/dispatch sequence bound to that same client generation
        # while concurrent management reads and admissions use the client.
        with lock:
            return self._process_locked(record)

    def _process_locked(self, record: OperationRecord) -> OperationRecord | None:
        client = self.runtime.clients.get(record.intent.target.channel)
        if client is None or (client.config.instance_id, client.config.channel, client.config.repository) != (record.intent.target.instance_id, record.intent.target.channel, record.intent.target.repository):
            return record
        try:
            persisted = self.runtime.journal.lifecycle_snapshot(record.operation_id)
            if persisted is None:
                return self.runtime._hold(record, "missing_original_lifecycle_evidence") or record
            version, snapshot = persisted
            stage = snapshot.get("stage")
            identity = self._original_supervisor(client, snapshot)
            if identity == "CHANGED" and stage not in TERMINAL_STAGES and stage != "NEEDS_ATTENTION":
                rebound = self._rebind_bootstrap_supervisor(record, version, snapshot, client)
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
                snapshot["stage"] = "PREPARING" if record.intent.action == "deploy_beta_source" else "DRAINING" if record.intent.action in {"stop_worker", "restart_worker"} else "STARTING"
                self._save(record, version, snapshot)
                return record
            deadline = snapshot.get("deadline")
            if not isinstance(deadline, (int, float)) or time.time() > deadline:
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
                    record.intent.action == "deploy_beta_source"
                    and snapshot.get("previous_runtime_verified") is True
                    and snapshot.get("recovery_attempts") == 0
                    and isinstance(observed_worker, dict)
                    and inspected.data.get("alive") is False
                    and observed_worker.get("deployment_generation") == new_worker.get("deployment_generation")
                ):
                    # Confirm the failed child lifetime through the actual
                    # Supervisor's OS observation before reserving recovery.
                    snapshot.update(stage="RESTORING", recovery_attempts=1, deadline=time.time() + 150)
                    self._save(record, version, snapshot)
                    return record
                if not isinstance(observed_worker, dict) or any(observed_worker.get(key) != new_worker.get(key) for key in ("deployment_generation", "runtime_id")) or inspected.data.get("alive") is not True:
                    return record
                observation = client.observe_worker()
                if not isinstance(observation.data, dict):
                    return record
                worker = observation.data.get("observation")
                if not isinstance(worker, dict) or worker.get("deployment_generation") != new_worker.get("deployment_generation") or worker.get("process_run_id") != observed_worker.get("process_run_id"):
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
                staged = snapshot.get("staged")
                if isinstance(staged, dict):
                    binding = observation.data.get("runtime_binding")
                    if (
                        observation.data.get("binding_status") != "VERIFIED"
                        or not isinstance(binding, dict)
                        or any(binding.get(key) != staged.get(other) for key, other in (("runtime_id", "installed_runtime_id"), ("content_identity", "content_identity"), ("lock_sha256", "lock_sha256"), ("source_sha", "source_sha"), ("distribution_version", "distribution_version")))
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
                    return self._step(record, version, snapshot, "recovery", client, "RECOVER_PREVIOUS", runtime_id)
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
                        return self._finish(record, version, snapshot, "FAILED", "new_beta_worker_failed_previous_runtime_restored")
                    self._save(record, version, snapshot)
                return record
            return record
        except (RuntimeError, OSError, ValueError, KeyError, TypeError):
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
            if not isinstance(step, dict) or step.get("state") != "SENT_UNKNOWN" or name not in {"prepare", "drain", "start", "recovery"}:
                continue
            effect_id = step.get("operation_id")
            if not isinstance(effect_id, str):
                continue
            response = client.read_preparation(effect_id) if name == "prepare" else client.read_worker_operation(effect_id)
            if response.result_code in {"READY", "STOPPED", "RUNNING", "FAILED"} and isinstance(response.data, dict) and response.data.get("operation_id") == effect_id:
                step.update(state="CONFIRMED", observation=response.data, observed_at=time.time())
                self._save(record, version, snapshot)
                return
