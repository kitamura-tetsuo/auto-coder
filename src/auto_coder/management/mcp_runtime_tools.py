"""Authenticated, bounded runtime MCP tools over fixed Supervisor bridges."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from mcp.types import ToolAnnotations
from pydantic import StrictInt

from auto_coder.operating_policy import ActionClass, DecisionOutcome, PolicyIntent, TargetChannel, evaluate_snapshot, load_operating_policy
from auto_coder.tested_source_refs import TestedSourcePublisher

from .action_scopes import DEPLOY_SCOPE, OPERATE_SCOPE, READ_SCOPE
from .action_scopes import AcceptedAuthority as GrantAuthority
from .action_scopes import PendingAuthorityStatus
from .mcp_resource import ManagementTool, current_management_grant
from .oauth_config import ManagementSettings
from .oauth_grants import AuthenticatedGrant, OAuthGrantService
from .operation_executor import ManagementOperationDispatcher, OperationHandler
from .operation_store import ManagementOperationStore
from .operations import (
    AcceptedAuthority,
    ActionParameter,
    AdmissionStatus,
    EffectReceipt,
    EffectState,
    ManagedTarget,
    MutationStatus,
    OperationIntent,
    OperationPhase,
    OperationRecord,
    ReadStatus,
)
from .self_update_operation import ManagementSelfUpdateHandler
from .self_update_policy import ManagementUpdateAction
from .worker_lifecycle import LIFECYCLE_ACTIONS, WorkerLifecycle
from .worker_network_client import WorkerNetworkClient, WorkerNetworkResult

REEVALUATE_ACTION = "reevaluate_target"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_LOG_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CURSOR = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
_ATTEMPT_ID = re.compile(r"[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class ResolvedWorker:
    target_id: str
    client: WorkerNetworkClient
    policy_revision: str

    @property
    def target(self) -> ManagedTarget:
        config = self.client.config
        return ManagedTarget(config.instance_id, config.channel, config.repository)


class ManagementRuntime:
    """One service-lifetime adapter, with no Docker or worker scheduling powers."""

    def __init__(self, settings: ManagementSettings, grants: OAuthGrantService, clients: dict[str, WorkerNetworkClient], *, self_update_source_authority: TestedSourcePublisher | None = None) -> None:
        self.settings = settings
        self.grants = grants
        self.clients = clients
        self.journal = ManagementOperationStore(settings.operation_state_root or settings.state_root)
        self.dispatcher = ManagementOperationDispatcher(self.journal)
        self.dispatcher.register(REEVALUATE_ACTION, OperationHandler(self._forward_reevaluation, self._reconcile_reevaluation))
        self.self_update = ManagementSelfUpdateHandler(settings, grants, self.journal, self_update_source_authority)
        self.dispatcher.register(ManagementUpdateAction.SELF_UPDATE.value, OperationHandler(self.self_update.forward, self.self_update.reconcile))
        self._worker_locks = {channel: threading.RLock() for channel in clients}
        self.lifecycle = WorkerLifecycle(self)
        for action in sorted(LIFECYCLE_ACTIONS):
            self.dispatcher.register(action, OperationHandler(self.lifecycle.process, self.lifecycle.process))
        self._lifecycle_monitor: asyncio.Task[None] | None = None

    async def start_monitor(self) -> None:
        self._lifecycle_monitor = asyncio.create_task(self._monitor_lifecycle(), name="management-worker-lifecycle-monitor")

    async def stop_monitor(self) -> None:
        if self._lifecycle_monitor is not None:
            self._lifecycle_monitor.cancel()
            try:
                await self._lifecycle_monitor
            except asyncio.CancelledError:
                pass
            self._lifecycle_monitor = None

    async def _monitor_lifecycle(self) -> None:
        while True:
            await asyncio.sleep(2)
            await self._poll_lifecycle_once()

    async def _poll_lifecycle_once(self) -> None:
        """Run one real journal-backed lifecycle monitor sweep."""
        cursor = 0
        while True:
            page = await asyncio.to_thread(self.journal.list_operations, cursor=cursor, limit=100, outstanding_only=True)
            if page.status.value != "ok":
                break
            for record in page.records:
                if record.intent.action in LIFECYCLE_ACTIONS:
                    await self.dispatcher.submit(record.operation_id)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

    @staticmethod
    def _response(status: str, reason: str, **fields: object) -> dict[str, object]:
        return {"status": status, "reason": reason, **fields}

    @staticmethod
    def _grant(required: tuple[str, ...]) -> AuthenticatedGrant:
        grant = current_management_grant()
        if not set(required) <= set(grant.scopes):
            raise PermissionError("the current management grant lacks the required scopes")
        return grant

    def _resolve(self, target_id: str, action: ActionClass) -> ResolvedWorker | dict[str, object]:
        if not isinstance(target_id, str) or not _NAME.fullmatch(target_id):
            return self._response("INVALID_REQUEST", "invalid_target_id")
        if self.settings.operating_policy_path is None:
            return self._response("UNAVAILABLE", "operating_policy_unconfigured")
        loaded = load_operating_policy(self.settings.operating_policy_path)
        if loaded.outcome is DecisionOutcome.UNAVAILABLE or loaded.policy is None:
            return self._response("UNAVAILABLE", "operating_policy_unavailable")
        target = next((item for item in loaded.policy.targets if item.target_id == target_id), None)
        if target is None:
            return self._response("DENIED", "target_unknown")
        client = self.clients.get(target.channel.value)
        if client is None:
            return self._response("UNAVAILABLE", "supervisor_bridge_unconfigured")
        config = client.config
        # A policy alias may only address the one immutable operator-provisioned
        # instance and repository; multiple repository choices are not an MCP API.
        if len(target.repositories) != 1 or target.repositories[0] != config.repository or target.channel.value != config.channel:
            return self._response("DENIED", "target_configuration_mismatch")
        decision = evaluate_snapshot(loaded.policy, PolicyIntent(target_id, config.repository, action, TargetChannel(config.channel)))
        if decision.outcome is not DecisionOutcome.ALLOWED:
            return self._response(decision.outcome.value, decision.reason_code.value)
        return ResolvedWorker(target_id, client, loaded.revision)

    @staticmethod
    def _runtime_binding(data: dict[str, object], observation: dict[str, object], supervisor: dict[str, object]) -> tuple[str, dict[str, object] | None]:
        status = data.get("binding_status")
        binding = data.get("runtime_binding")
        if status != "VERIFIED":
            return ("CONFLICT" if status == "CONFLICT" or binding is not None else "UNAVAILABLE"), None
        if not isinstance(binding, dict):
            return "UNAVAILABLE", None
        worker = supervisor.get("worker")
        artifact = observation.get("artifact")
        if not isinstance(worker, dict) or not isinstance(artifact, dict):
            return "CONFLICT", None
        source, version = artifact.get("source_revision"), artifact.get("distribution_version")
        if not isinstance(source, dict) or not isinstance(version, dict):
            return "CONFLICT", None
        supervisor_binding = worker.get("runtime_binding")
        exact = isinstance(supervisor_binding, dict) and binding.get("runtime_id") == supervisor.get("selected_runtime_id") == worker.get("runtime_id") and binding.get("runtime_id") == supervisor_binding.get("runtime_id")
        if not exact or any(not isinstance(binding.get(key), str) or not binding[key] for key in ("runtime_id", "content_identity", "lock_sha256", "source_sha", "distribution_version")):
            return "CONFLICT", None
        if (
            not _SHA.fullmatch(str(binding["content_identity"]))
            or not _SHA.fullmatch(str(binding["lock_sha256"]))
            or binding.get("process_run_id") != observation.get("process_run_id")
            or binding.get("deployment_generation") != observation.get("deployment_generation")
            or source.get("available") is not True
            or version.get("available") is not True
            or source.get("value") != binding.get("source_sha")
            or version.get("value") != binding.get("distribution_version")
            or worker.get("binding_status") != "VERIFIED"
        ):
            return "CONFLICT", None
        # The signed Supervisor projection and nonce-bound worker observation
        # must both identify the same selected READY receipt and locked inputs.
        assert isinstance(supervisor_binding, dict)
        if any(supervisor_binding.get(field) != binding.get(field) for field in ("runtime_id", "content_identity", "lock_sha256", "source_sha", "distribution_version", "process_run_id", "deployment_generation")):
            return "CONFLICT", None
        return "VERIFIED", {key: binding[key] for key in ("runtime_id", "content_identity", "lock_sha256", "source_sha", "distribution_version", "process_run_id", "deployment_generation")}

    def _observe(self, resolved: ResolvedWorker) -> dict[str, object]:
        client = resolved.client
        with self._worker_locks[client.config.channel]:
            supervisor_result = client.inspect_supervisor()
            supervisor = supervisor_result.data
            if supervisor_result.result_code != "OK" or not isinstance(supervisor, dict) or supervisor.get("supervisor_incarnation") != supervisor_result.supervisor_generation:
                return self._response("UNAVAILABLE", "supervisor_observation_unavailable")
            if supervisor.get("alive") is not True or supervisor.get("status") != "RUNNING":
                return self._response("UNAVAILABLE", "worker_not_running", supervisor_status=supervisor.get("status"))
            observation_result = client.observe_worker(uuid.uuid4().hex)
            data = observation_result.data
            if observation_result.result_code != "OK" or not isinstance(data, dict) or observation_result.supervisor_generation != supervisor_result.supervisor_generation:
                return self._response("UNAVAILABLE", "worker_observation_unavailable")
            observation = data.get("observation")
            worker = supervisor.get("worker")
            if not isinstance(observation, dict) or not isinstance(worker, dict) or observation.get("process_run_id") != worker.get("process_run_id") or observation.get("deployment_generation") != worker.get("deployment_generation"):
                return self._response("CONFLICT", "worker_incarnation_mismatch")
            binding_status, binding = self._runtime_binding(data, observation, supervisor)
            return self._response(
                "OK" if binding_status == "VERIFIED" else binding_status,
                "verified_worker_observation" if binding_status == "VERIFIED" else "runtime_binding_" + binding_status.lower(),
                supervisor_generation=supervisor_result.supervisor_generation,
                instance_id=client.config.instance_id,
                repository=client.config.repository,
                channel=client.config.channel,
                observation=observation,
                runtime_binding=binding,
                binding_status=binding_status,
            )

    def _diagnostic(self, target_id: str, kind: str, params: dict[str, str]) -> dict[str, object]:
        self._grant((READ_SCOPE,))
        resolved = self._resolve(target_id, ActionClass.INSPECT)
        if not isinstance(resolved, ResolvedWorker):
            return resolved
        with self._worker_locks[resolved.client.config.channel]:
            observed = self._observe(resolved)
            observation = observed.get("observation")
            if not isinstance(observation, dict):
                return observed
            result = resolved.client.diagnostics(kind, params)
            if result.supervisor_generation != observed["supervisor_generation"]:
                return self._response("CONFLICT", "supervisor_changed_during_diagnostic")
            if result.result_code not in {"OK", "ACCEPTED"}:
                return self._response(result.result_code, "worker_diagnostic_" + result.result_code.lower(), projection=result.data)
            return self._response(
                str(observed["status"]),
                "worker_diagnostic" if observed["status"] == "OK" else str(observed["reason"]),
                instance_id=resolved.target.instance_id,
                channel=resolved.target.channel,
                repository=resolved.target.repository,
                process_run_id=observation["process_run_id"],
                supervisor_generation=observed["supervisor_generation"],
                worker_observation=observation if kind == "status" else None,
                runtime_binding=observed["runtime_binding"],
                binding_status=observed["binding_status"],
                projection=result.data,
            )

    async def status(self, target_id: str, limit: StrictInt = 50) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 500:
            return self._response("INVALID_REQUEST", "invalid_limit")
        return await asyncio.to_thread(self._diagnostic, target_id, "status", {"limit": str(limit)})

    async def logs(self, target_id: str, limit: StrictInt = 50, item_type: str | None = None, item_number: StrictInt | None = None, execution_id: str | None = None, process_run_id: str | None = None, after_sequence: StrictInt | None = None, cursor: str | None = None) -> dict[str, object]:
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or (item_type is None) != (item_number is None)
            or (item_type is not None and (item_type not in {"issue", "pr"} or type(item_number) is not int or not 0 < item_number <= 999999999))
            or (after_sequence is not None and (type(after_sequence) is not int or not 0 <= after_sequence < 10**15))
        ):
            return self._response("INVALID_REQUEST", "invalid_log_selection")
        optional: dict[str, object] = {"item_type": item_type, "item_number": item_number, "execution_id": execution_id, "process_run_id": process_run_id, "after_sequence": after_sequence, "cursor": cursor}
        if (
            any(value is not None and not _LOG_ID.fullmatch(value) for value in (execution_id, process_run_id))
            or (cursor is not None and not _CURSOR.fullmatch(cursor))
            or (cursor is not None and any(value is not None for key, value in optional.items() if key != "cursor"))
            or ((execution_id is not None or after_sequence is not None) and process_run_id is None)
        ):
            return self._response("INVALID_REQUEST", "invalid_log_selection")
        params = {key: str(value) for key, value in optional.items() if value is not None}
        params["limit"] = str(limit)
        return await asyncio.to_thread(self._diagnostic, target_id, "logs", params)

    async def validation_attempt(self, target_id: str, pr_number: StrictInt | None = None, attempt_id: str | None = None, attempt_sequence: StrictInt | None = None) -> dict[str, object]:
        if (
            type(pr_number) is not int
            or not 0 < pr_number <= 999999999
            or (attempt_id is None) == (attempt_sequence is None)
            or (attempt_sequence is not None and (type(attempt_sequence) is not int or not 0 < attempt_sequence < 10**15))
            or (attempt_id is not None and not _ATTEMPT_ID.fullmatch(attempt_id))
        ):
            return self._response("INVALID_REQUEST", "invalid_validation_selection")
        params = {key: str(value) for key, value in {"pr_number": pr_number, "attempt_id": attempt_id, "attempt_sequence": attempt_sequence}.items() if value is not None}
        return await asyncio.to_thread(self._diagnostic, target_id, "validation-attempts", params)

    @staticmethod
    def _journal_authority(grant: AuthenticatedGrant, original: GrantAuthority) -> AcceptedAuthority:
        if original.grant_id != grant.grant_id or original.client_id != grant.client_id:
            raise PermissionError("original grant changed during admission")
        return AcceptedAuthority(original.issuer, original.resource, original.project_id, original.uid, original.client_id, original.grant_id, original.accepted_scopes, original.auth_time, original.accepted_at)

    @staticmethod
    def _project_operation(record: OperationRecord) -> dict[str, object]:
        receipt = record.effect.receipt
        return {
            "status": "accepted_for_reevaluation" if record.intent.action == REEVALUATE_ACTION and record.phase is OperationPhase.SUCCEEDED and record.effect.state is EffectState.CONFIRMED else record.phase.value.upper(),
            "operation_id": record.operation_id,
            "target": asdict(record.intent.target),
            "action": record.intent.action,
            "phase": record.phase.value,
            "effect_state": record.effect.state.value,
            "revision": record.revision,
            "safe_reason": record.safe_error_category or None,
            "accepted_actor": {"project_id": record.authority.project_id, "uid": record.authority.uid, "client_id": record.authority.oauth_client_id, "scopes": record.authority.accepted_scopes},
            "receipt": None if receipt is None else {"source": receipt.observation_source, "result": receipt.safe_result, "observed_at": receipt.observed_at},
        }

    def _owned_operation(self, target_id: str, operation_id: str, grant: AuthenticatedGrant) -> OperationRecord | dict[str, object]:
        if not isinstance(operation_id, str) or not _IDENTITY.fullmatch(operation_id):
            return self._response("INVALID_REQUEST", "invalid_operation_id")
        lookup = self.journal.get(operation_id)
        if lookup.status is ReadStatus.UNAVAILABLE:
            return self._response("UNAVAILABLE", "journal_unavailable")
        record = lookup.record
        physical = self.clients.get(record.intent.target.channel) if record is not None else None
        if (
            record is None
            or physical is None
            or record.intent.target != ManagedTarget(physical.config.instance_id, physical.config.channel, physical.config.repository)
            or self._params(record).get("target_id") != target_id
            or (record.authority.project_id, record.authority.uid, record.authority.oauth_client_id, record.authority.issuer, record.authority.resource) != (grant.principal.project_id, grant.principal.uid, grant.client_id, self.settings.oauth.canonical_issuer, grant.resource)
        ):
            return self._response("NOT_FOUND", "operation_not_found")
        return record

    async def operation_get(self, operation_id: str, target_id: str | None = None) -> dict[str, object]:
        grant = self._grant((READ_SCOPE,))
        management = await asyncio.to_thread(self.self_update.owned_operation, operation_id, grant)
        if management is not None:
            if isinstance(management, dict):
                return management
            if target_id is not None and target_id != management.intent.target.instance_id:
                return self._response("NOT_FOUND", "operation_not_found")
            await self.observe_authenticated_identity(grant)
            queued = self.dispatcher.enqueue(operation_id)
            return {**await asyncio.to_thread(self.self_update.project, management), "dispatch_queued": queued}
        if target_id is None:
            return self._response("INVALID_REQUEST", "worker_target_id_required")
        owned = self._owned_operation(target_id, operation_id, grant)
        if not isinstance(owned, OperationRecord):
            return owned
        if owned.intent.action in LIFECYCLE_ACTIONS:
            # An inspection-only MCP call must not become the actuator for
            # the next worker-changing step. The service dispatcher owns it.
            return self.lifecycle.project(owned)
        if owned.effect.state in {EffectState.SENT_UNKNOWN, EffectState.CONFIRMED} and owned.phase not in {OperationPhase.SUCCEEDED, OperationPhase.FAILED, OperationPhase.ROLLED_BACK}:
            updated = await self.dispatcher.submit(operation_id)
            if updated is not None:
                owned = updated
        return self._project_operation(owned)

    async def update_management_service(self, source_sha: str, idempotency_key: str) -> dict[str, object]:
        grant = self._grant((READ_SCOPE, DEPLOY_SCOPE))
        admitted = await asyncio.to_thread(self.self_update.admit, source_sha, idempotency_key, grant)
        if isinstance(admitted, dict):
            return admitted
        queued = self.dispatcher.enqueue(admitted.operation_id)
        return {**await asyncio.to_thread(self.self_update.project, admitted), "dispatch_queued": queued}

    async def observe_authenticated_identity(self, grant: AuthenticatedGrant, observation_id: str = "") -> None:
        from .readiness_observation import publish_authenticated_readback

        await asyncio.to_thread(self.self_update.observe_authenticated_identity, grant)
        await asyncio.to_thread(publish_authenticated_readback, self.settings, grant, startup_reconciled=self.self_update.startup_reconciled, observation_id=observation_id)

    async def deployment_status(self, target_id: str) -> dict[str, object]:
        self._grant((READ_SCOPE,))
        resolved = self._resolve(target_id, ActionClass.INSPECT)
        if not isinstance(resolved, ResolvedWorker):
            return resolved
        with self._worker_locks[resolved.client.config.channel]:
            result = resolved.client.inspect_supervisor()
            if result.result_code != "OK" or not isinstance(result.data, dict):
                return self._response("UNAVAILABLE", "supervisor_status_unavailable")
            data = result.data
            output = self._response(
                "OK",
                "supervisor_observed",
                instance_id=resolved.target.instance_id,
                channel=resolved.target.channel,
                repository=resolved.target.repository,
                supervisor_generation=result.supervisor_generation,
                process_state=data.get("status"),
                alive=data.get("alive"),
                selected_runtime_id=data.get("selected_runtime_id"),
                worker=data.get("worker"),
            )
            if data.get("alive") is True and data.get("status") == "RUNNING":
                observed = self._observe(resolved)
                output["worker_observation"] = observed.get("observation")
                output["installed_binding_status"] = observed.get("binding_status")
                output["installed_runtime"] = observed.get("runtime_binding")
            return output

    async def start_worker(self, target_id: str, idempotency_key: str, expected_supervisor_generation: str, expected_runtime_id: str | None = None) -> dict[str, object]:
        return await self.lifecycle.admit("start_worker", target_id, idempotency_key, expected_supervisor_generation, None, None, expected_runtime_id)

    async def stop_worker(self, target_id: str, idempotency_key: str, expected_supervisor_generation: str, expected_process_run_id: str, expected_deployment_generation: str, expected_runtime_id: str) -> dict[str, object]:
        return await self.lifecycle.admit("stop_worker", target_id, idempotency_key, expected_supervisor_generation, expected_process_run_id, expected_deployment_generation, expected_runtime_id)

    async def restart_worker(self, target_id: str, idempotency_key: str, expected_supervisor_generation: str, expected_process_run_id: str, expected_deployment_generation: str, expected_runtime_id: str) -> dict[str, object]:
        return await self.lifecycle.admit("restart_worker", target_id, idempotency_key, expected_supervisor_generation, expected_process_run_id, expected_deployment_generation, expected_runtime_id)

    async def deploy_beta_source(self, target_id: str, selected_git_sha: str, idempotency_key: str, expected_supervisor_generation: str, expected_runtime_id: str | None = None, expected_process_run_id: str | None = None, expected_deployment_generation: str | None = None) -> dict[str, object]:
        return await self.lifecycle.admit("deploy_beta_source", target_id, idempotency_key, expected_supervisor_generation, expected_process_run_id, expected_deployment_generation, expected_runtime_id, selected_git_sha)

    @staticmethod
    def _ready_to_reevaluate(observed: dict[str, object], expected_run: str, expected_generation: str) -> bool:
        observation = observed.get("observation")
        if observed.get("status") != "OK" or not isinstance(observation, dict):
            return False
        lifecycle, invocations, heartbeat = observation.get("lifecycle"), observation.get("invocations"), observation.get("heartbeat")
        return (
            observation.get("process_run_id") == expected_run
            and observation.get("deployment_generation") == expected_generation
            and isinstance(lifecycle, dict)
            and lifecycle.get("state") == "running"
            and lifecycle.get("startup_reconciled") is True
            and lifecycle.get("daemon_serving") is True
            and isinstance(invocations, dict)
            and invocations.get("state") == "running"
            and isinstance(heartbeat, dict)
            and heartbeat.get("available") is True
        )

    async def reevaluate_target(self, target_id: str, target_type: str, number: StrictInt, idempotency_key: str, expected_process_run_id: str, expected_deployment_generation: str) -> dict[str, object]:
        grant = self._grant((READ_SCOPE, OPERATE_SCOPE))
        if (
            target_type not in {"issue", "pr"}
            or type(number) is not int
            or not 0 < number <= 2**31 - 1
            or not isinstance(idempotency_key, str)
            or not _KEY.fullmatch(idempotency_key)
            or not isinstance(expected_process_run_id, str)
            or not _IDENTITY.fullmatch(expected_process_run_id)
            or not isinstance(expected_deployment_generation, str)
            or not _IDENTITY.fullmatch(expected_deployment_generation)
        ):
            return self._response("INVALID_REQUEST", "invalid_reevaluation_request")
        resolved = self._resolve(target_id, ActionClass.INSPECT)
        if not isinstance(resolved, ResolvedWorker):
            return resolved
        intent = OperationIntent(
            resolved.target,
            REEVALUATE_ACTION,
            (ActionParameter("target_id", target_id), ActionParameter("target_type", target_type), ActionParameter("number", number)),
            idempotency_key,
            expected_process_run_id,
            expected_deployment_generation,
        )
        original = self._journal_authority(grant, self.grants.accepted_authority(grant))
        old = await asyncio.to_thread(self.journal.find_by_key, intent, original)
        if old.status is ReadStatus.UNAVAILABLE:
            return self._response("UNAVAILABLE", "journal_unavailable")
        if old.record is not None:
            if old.record.intent_fingerprint != intent.fingerprint():
                return self._response("CONFLICT", "idempotency_key_conflict", operation_id=old.record.operation_id)
            # Replays retain the original immutable authority. They may observe
            # an already-sent receipt, but never substitute a refreshed grant.
            updated = await self.dispatcher.submit(old.record.operation_id)
            return self._project_operation(updated or old.record)
        mutation = self._resolve(target_id, ActionClass.WORKER_CONTROL)
        if not isinstance(mutation, ResolvedWorker):
            return mutation
        observed = await asyncio.to_thread(self._observe, mutation)
        if not self._ready_to_reevaluate(observed, expected_process_run_id, expected_deployment_generation):
            return self._response("UNAVAILABLE" if observed.get("status") == "UNAVAILABLE" else "CONFLICT", "worker_not_ready_or_generation_mismatch")
        admitted = await asyncio.to_thread(self.journal.admit, intent, original)
        if admitted.status in {AdmissionStatus.UNAVAILABLE, AdmissionStatus.CONFLICT, AdmissionStatus.BUSY}:
            return self._response(admitted.status.value.upper(), "journal_" + admitted.status.value, operation_id=admitted.record.operation_id if admitted.record else None)
        assert admitted.record is not None
        updated = await self.dispatcher.submit(admitted.record.operation_id)
        return self._project_operation(updated or admitted.record)

    @staticmethod
    def _params(record: OperationRecord) -> dict[str, str | int | bool]:
        return {part.name: part.value for part in record.intent.parameters}

    def _recorded_target(self, record: OperationRecord) -> ResolvedWorker | None:
        if record.intent.action != REEVALUATE_ACTION or not _IDENTITY.fullmatch(record.intent.expected_incarnation) or not _IDENTITY.fullmatch(record.intent.expected_artifact):
            return None
        params = self._params(record)
        if set(params) != {"target_id", "target_type", "number"} or not isinstance(params["target_id"], str) or params["target_type"] not in {"issue", "pr"} or type(params["number"]) is not int or params["number"] <= 0:
            return None
        resolved = self._resolve(params["target_id"], ActionClass.WORKER_CONTROL)
        return resolved if isinstance(resolved, ResolvedWorker) and resolved.target == record.intent.target else None

    def _policy_refusal(self, record: OperationRecord) -> str | None:
        params = self._params(record)
        target_id = params.get("target_id")
        if not isinstance(target_id, str):
            return "recorded_target_invalid"
        outcome = self._resolve(target_id, ActionClass.WORKER_CONTROL)
        if isinstance(outcome, ResolvedWorker) and outcome.target == record.intent.target:
            return None
        if isinstance(outcome, dict) and outcome.get("status") == "UNAVAILABLE":
            return "current_policy_unavailable"
        return "current_policy_denied"

    def _hold(self, record: OperationRecord, reason: str) -> OperationRecord | None:
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            return record
        result = self.journal.transition(record.operation_id, record.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category=reason)
        return result.record

    def _forward_reevaluation(self, record: OperationRecord) -> OperationRecord | None:
        # No newer OAuth token can overwrite the original acceptance context.
        authority = record.authority
        if (authority.issuer, authority.resource, authority.project_id) != (self.settings.oauth.canonical_issuer, self.settings.oauth.resource, self.settings.oauth.firebase_project_id) or not set((READ_SCOPE, OPERATE_SCOPE)) <= set(authority.accepted_scopes):
            return self._hold(record, "original_authority_denied")
        original = GrantAuthority(authority.trusted_grant_id, authority.project_id, authority.uid, authority.authentication_time, authority.oauth_client_id, authority.issuer, authority.resource, authority.accepted_scopes, authority.accepted_at)
        decision = self.grants.check_pending_authority(original, (READ_SCOPE, OPERATE_SCOPE))
        if decision.status is not PendingAuthorityStatus.PERMITTED:
            return self._hold(record, "original_authority_unavailable" if decision.status is PendingAuthorityStatus.UNAVAILABLE else "original_authority_denied")
        refusal = self._policy_refusal(record)
        if refusal is not None:
            return self._hold(record, refusal)
        resolved = self._recorded_target(record)
        if resolved is None:
            return self._hold(record, "recorded_target_invalid")
        with self._worker_locks[resolved.client.config.channel]:
            observed = self._observe(resolved)
            if not self._ready_to_reevaluate(observed, record.intent.expected_incarnation, record.intent.expected_artifact):
                return self._hold(record, "worker_not_ready_or_generation_mismatch")
            # The original durable grant and the current phase-policy are
            # checked again at the actual first-send boundary, not just prior
            # to potentially slow Supervisor observation.
            check = self.grants.check_pending_authority(original, (READ_SCOPE, OPERATE_SCOPE))
            if check.status is not PendingAuthorityStatus.PERMITTED:
                return self._hold(record, "original_authority_unavailable" if check.status is PendingAuthorityStatus.UNAVAILABLE else "original_authority_denied")
            refusal = self._policy_refusal(record)
            if refusal is not None:
                return self._hold(record, refusal)
            # The journal CAS is the durable pre-send fence. Once installed,
            # even a lost HTTP response may only be reconciled by readback.
            correlation = hashlib.sha256(json.dumps((record.operation_id, asdict(record.intent.target), observed["supervisor_generation"], record.intent.expected_incarnation, record.intent.expected_artifact), sort_keys=True).encode()).hexdigest()
            attempt = uuid.uuid4().hex
            begun = self.journal.begin_effect(record.operation_id, record.revision, owner_attempt_id=attempt, attempt_id=attempt, correlation_identity=correlation)
            if begun.status is not MutationStatus.UPDATED or begun.record is None:
                return begun.record
            params = self._params(record)
            response = resolved.client.accept_reevaluation(record.operation_id, process_run_id=record.intent.expected_incarnation, deployment_generation=record.intent.expected_artifact, target_type=str(params["target_type"]), number=int(params["number"]))
            return self._consume_receipt(begun.record, response) or begun.record

    @staticmethod
    def _verified_receipt(record: OperationRecord, response: WorkerNetworkResult) -> EffectReceipt | None:
        value = response.data
        params = ManagementRuntime._params(record)
        if response.http_status not in {200, 202} or response.result_code not in {"OK", "ACCEPTED"} or not isinstance(value, dict):
            return None
        target = value.get("target")
        invalidation = value.get("invalidation")
        if not isinstance(target, dict) or not isinstance(invalidation, dict):
            return None
        if (
            value.get("schema_version") != 1
            or value.get("operation_id") != record.operation_id
            or value.get("repository") != record.intent.target.repository
            or value.get("channel") != record.intent.target.channel
            or target != {"type": params["target_type"], "number": params["number"]}
            or value.get("requested_process_run_id") != record.intent.expected_incarnation
            or value.get("requested_deployment_generation") != record.intent.expected_artifact
            or value.get("accepting_process_run_id") != record.intent.expected_incarnation
            or value.get("accepting_deployment_generation") != record.intent.expected_artifact
            or value.get("outcome") != "accepted_for_reevaluation"
            or type(invalidation.get("generation")) is not int
            or invalidation["generation"] <= 0
            or not isinstance(invalidation.get("disposition"), str)
            or not invalidation["disposition"]
        ):
            return None
        safe = json.dumps({"outcome": value["outcome"], "generation": invalidation["generation"], "disposition": invalidation["disposition"], "accepting_process_run_id": value["accepting_process_run_id"]}, sort_keys=True)
        if len(safe) > 512:
            return None
        return EffectReceipt(record.effect.correlation_identity, "worker-uds-invalidation-receipt", time.time(), safe)

    def _consume_receipt(self, record: OperationRecord, response: WorkerNetworkResult) -> OperationRecord | None:
        receipt = self._verified_receipt(record, response)
        if receipt is None:
            # UNAVAILABLE, NOT_FOUND, conflicting/stale identity and lost replies
            # all retain ownership. No missing receipt can prove no effect.
            return record
        if record.effect.state is EffectState.SENT_UNKNOWN:
            mutation = self.journal.record_effect(record.operation_id, record.revision, attempt_id=record.effect.attempt_id, state=EffectState.CONFIRMED, receipt=receipt)
            if mutation.status is not MutationStatus.UPDATED or mutation.record is None:
                return mutation.record
            record = mutation.record
        elif record.effect.state is not EffectState.CONFIRMED:
            return record
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            completed = self.journal.resolve_needs_attention(record.operation_id, record.revision, OperationPhase.SUCCEEDED, evidence=record.effect.receipt or receipt)
        else:
            completed = self.journal.transition(record.operation_id, record.revision, OperationPhase.SUCCEEDED, evidence=record.effect.receipt or receipt)
        return completed.record

    def _reconcile_reevaluation(self, record: OperationRecord) -> OperationRecord | None:
        if record.effect.state is EffectState.CONFIRMED and record.effect.receipt is not None:
            if record.phase is OperationPhase.NEEDS_ATTENTION:
                terminal = self.journal.resolve_needs_attention(record.operation_id, record.revision, OperationPhase.SUCCEEDED, evidence=record.effect.receipt)
            else:
                terminal = self.journal.transition(record.operation_id, record.revision, OperationPhase.SUCCEEDED, evidence=record.effect.receipt)
            return terminal.record
        if record.effect.state is not EffectState.SENT_UNKNOWN:
            return record
        # Reconciliation is tied to the immutable physical endpoint recorded
        # before send. It requires no fresh mutation or policy authority.
        client = self.clients.get(record.intent.target.channel)
        if client is None or record.intent.target != ManagedTarget(client.config.instance_id, client.config.channel, client.config.repository):
            return record
        with self._worker_locks[client.config.channel]:
            if client.inspect_supervisor().result_code != "OK":
                return record
            response = client.read_reevaluation(record.operation_id)
            return self._consume_receipt(record, response) or record

    def tool_registrations(self) -> tuple[ManagementTool, ...]:
        read = ToolAnnotations(readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False)
        mutation = ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=False, openWorldHint=False)
        return (
            ManagementTool("auto_coder_status", self.status, (READ_SCOPE,), read),
            ManagementTool("auto_coder_logs", self.logs, (READ_SCOPE,), read),
            ManagementTool("auto_coder_validation_attempt", self.validation_attempt, (READ_SCOPE,), read),
            ManagementTool("auto_coder_operation_get", self.operation_get, (READ_SCOPE,), read),
            ManagementTool("auto_coder_reevaluate_target", self.reevaluate_target, (READ_SCOPE, OPERATE_SCOPE), mutation),
            ManagementTool("auto_coder_update_management_service", self.update_management_service, (READ_SCOPE, DEPLOY_SCOPE), ToolAnnotations(readOnlyHint=False, idempotentHint=True, destructiveHint=True, openWorldHint=False), ("source_sha", "idempotency_key")),
            ManagementTool("auto_coder_deployment_status", self.deployment_status, (READ_SCOPE,), read),
            ManagementTool("auto_coder_start_worker", self.start_worker, (READ_SCOPE, DEPLOY_SCOPE), mutation),
            ManagementTool("auto_coder_stop_worker", self.stop_worker, (READ_SCOPE, DEPLOY_SCOPE), mutation),
            ManagementTool("auto_coder_restart_worker", self.restart_worker, (READ_SCOPE, DEPLOY_SCOPE), mutation),
            ManagementTool("auto_coder_deploy_beta_source", self.deploy_beta_source, (READ_SCOPE, DEPLOY_SCOPE), mutation),
        )
