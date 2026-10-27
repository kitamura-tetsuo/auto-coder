"""Join authenticated admission, original source evidence, and Supervisor effects.

Only the shared management dispatcher calls ``forward``. A possibly delivered
handoff is subsequently observed by immutable identity and is never resubmitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger
from mcp.server.auth.middleware.auth_context import get_access_token

from auto_coder.build_provenance import observe_controller_artifact
from auto_coder.operating_policy import MANAGEMENT_REPOSITORY
from auto_coder.tested_source_refs import TestedSourcePublisher
from auto_coder.util.gh_cache import GitHubGitDataClient
from auto_coder.worker_versions import PreparationFailure, WorkerVersionError

from .bootstrap_store import ChildPhase
from .bootstrap_supervisor import BootstrapConfig, ManagementBootstrapSupervisor
from .oauth_config import ManagementSettings
from .oauth_grants import AuthenticatedGrant, OAuthGrantService
from .operation_store import ManagementOperationStore
from .operations import AdmissionStatus, EffectReceipt, EffectState, ManagedTarget, MutationStatus, OperationIntent, OperationPhase, OperationRecord, ReadStatus
from .self_update_handoff import ApplicationProof, HandoffPhase, HandoffRecord, HandoffStore, SelfUpdateHandoff, observe_supervisor, runtime_for_receipt, valid_application_proof
from .self_update_policy import MANAGEMENT_JOURNAL_CHANNEL, ManagementUpdateAction, ManagementUpdateIntent, ManagementUpdateOutcome, authorize_management_update, journal_entry_for_update, recheck_management_update_before_effect
from .self_update_preflight import ManagementPreflightError, ManagementSelfUpdatePreflight
from .version_staging import ManagementReadyReceipt, ManagementRuntimeConfig, ManagementVersionStager

_ACTION = ManagementUpdateAction.SELF_UPDATE.value
_SOURCE = re.compile(r"[0-9a-f]{40}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_TERMINAL = {OperationPhase.SUCCEEDED, OperationPhase.FAILED, OperationPhase.ROLLED_BACK}


class ManagementSelfUpdateHandler:
    """A fixed action adapter, without a scheduler or process-control authority."""

    def __init__(self, settings: ManagementSettings, grants: OAuthGrantService, journal: ManagementOperationStore, source_authority: TestedSourcePublisher | None = None) -> None:
        self.settings = settings
        self.grants = grants
        self.journal = journal
        self.source_authority = source_authority
        self.startup_reconciled = False

    @staticmethod
    def _response(status: str, reason: str, **fields: object) -> dict[str, object]:
        return {"status": status, "reason": reason, **fields}

    def _configuration(self) -> BootstrapConfig:
        settings = self.settings
        if settings.bootstrap_config_path is None or settings.config_path is None:
            raise ManagementPreflightError("bootstrap_unconfigured")
        config = BootstrapConfig.read(settings.bootstrap_config_path)
        listener = urlsplit(config.listener_url)
        if (
            config.management_version_root is None
            or config.management_config != settings.config_path
            or config.oauth_root != settings.state_root
            or config.journal_root != settings.operation_state_root
            or listener.hostname not in {"127.0.0.1", "::1", "localhost"}
            or listener.port != settings.port
            or listener.path != "/mcp"
            or listener.query
            or listener.fragment
        ):
            raise ManagementPreflightError("bootstrap_configuration_mismatch")
        return config

    def _stager(self, config: BootstrapConfig) -> ManagementVersionStager:
        settings = self.settings
        if settings.self_update_source_repository is None or settings.self_update_python_executable is None or settings.self_update_uv_executable is None or not settings.self_update_uv_version or config.management_version_root is None:
            raise ManagementPreflightError("staging_unconfigured")
        authority = self.source_authority or TestedSourcePublisher(GitHubGitDataClient(os.environ.get("GITHUB_TOKEN", ""), MANAGEMENT_REPOSITORY), MANAGEMENT_REPOSITORY)
        return ManagementVersionStager(
            ManagementRuntimeConfig(
                config.instance_id,
                MANAGEMENT_REPOSITORY,
                settings.self_update_source_repository,
                config.management_version_root,
                settings.self_update_python_executable,
                settings.self_update_uv_executable,
                settings.self_update_uv_version,
                protected_roots=(config.state_root, config.oauth_root, config.journal_root),
                management_config=config.management_config,
                source_authority=authority,
            )
        )

    @staticmethod
    def _params(record: OperationRecord) -> dict[str, str | int | bool]:
        return {item.name: item.value for item in record.intent.parameters}

    @staticmethod
    def _target(config: BootstrapConfig) -> ManagedTarget:
        return ManagedTarget(config.instance_id, MANAGEMENT_JOURNAL_CHANNEL, MANAGEMENT_REPOSITORY)

    def admit(self, source_sha: str, idempotency_key: str, grant: AuthenticatedGrant) -> OperationRecord | dict[str, object]:
        if not isinstance(source_sha, str) or not _SOURCE.fullmatch(source_sha) or not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
            return self._response("INVALID_REQUEST", "exact_source_sha_and_bounded_idempotency_key_required")
        try:
            config = self._configuration()
        except ManagementPreflightError as exc:
            return self._response("UNSUPPORTED", exc.category, recovery="Manually provision the immutable management Supervisor and configured READY runtimes.")
        except (OSError, ValueError, TypeError, KeyError):
            return self._response("UNAVAILABLE", "bootstrap_configuration_unavailable")
        # An authenticated retry can observe its original operation even after a
        # policy revision changes. It must never replace the accepted authority.
        original = self.grants.accepted_authority(grant)
        from .operations import AcceptedAuthority

        actor = AcceptedAuthority(original.issuer, original.resource, original.project_id, original.uid, original.client_id, original.grant_id, original.accepted_scopes, original.auth_time, original.accepted_at)
        probe = OperationIntent(self._target(config), _ACTION, (), idempotency_key)
        old = self.journal.find_by_key(probe, actor)
        if old.status is ReadStatus.UNAVAILABLE:
            return self._response("UNAVAILABLE", "operation_journal_unavailable")
        if old.record is not None:
            if old.record.intent.action != _ACTION or self._params(old.record).get("source_sha") != source_sha:
                return self._response("CONFLICT", "idempotency_key_conflict", operation_id=old.record.operation_id)
            return old.record
        if self.settings.operating_policy_path is None:
            return self._response("UNAVAILABLE", "operating_policy_unconfigured")
        access = get_access_token()
        if access is None or access.client_id != grant.client_id:
            return self._response("DENIED", "authenticated_context_unavailable")
        assert self.settings.bootstrap_config_path is not None
        decision, accepted = authorize_management_update(self.settings.operating_policy_path, self.settings.bootstrap_config_path, self.grants, access.token, ManagementUpdateIntent(config.instance_id, MANAGEMENT_REPOSITORY))
        if accepted is None or decision.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED:
            return self._response(decision.outcome.value, decision.reason_code.value)
        if accepted.original_authority.grant_id != grant.grant_id:
            return self._response("DENIED", "original_grant_mismatch")
        intent, authority = journal_entry_for_update(accepted, source_sha, idempotency_key)
        admitted = self.journal.admit(intent, authority)
        if admitted.status not in {AdmissionStatus.CREATED, AdmissionStatus.REPLAY} or admitted.record is None:
            return self._response(admitted.status.value.upper(), "journal_" + admitted.status.value, operation_id=admitted.record.operation_id if admitted.record else None)
        logger.info("Management self-update admitted operation={} source={}", admitted.record.operation_id, source_sha)
        return admitted.record

    def owned_operation(self, operation_id: str, grant: AuthenticatedGrant) -> OperationRecord | dict[str, object] | None:
        if not isinstance(operation_id, str) or not _OPERATION.fullmatch(operation_id):
            return self._response("INVALID_REQUEST", "invalid_operation_id")
        lookup = self.journal.get(operation_id)
        if lookup.status is ReadStatus.UNAVAILABLE:
            return self._response("UNAVAILABLE", "operation_journal_unavailable")
        record = lookup.record
        if record is None:
            return self._response("NOT_FOUND", "operation_not_found")
        if record.intent.action != _ACTION:
            return None
        authority = record.authority
        if (authority.issuer, authority.resource, authority.project_id, authority.uid, authority.oauth_client_id) != (self.settings.oauth.canonical_issuer, grant.resource, grant.principal.project_id, grant.principal.uid, grant.client_id):
            return self._response("NOT_FOUND", "operation_not_found")
        try:
            if record.intent.target != self._target(self._configuration()):
                return self._response("NOT_FOUND", "operation_not_found")
        except (ManagementPreflightError, OSError, ValueError, TypeError, KeyError):
            return self._response("UNAVAILABLE", "bootstrap_configuration_unavailable")
        return record

    def project(self, record: OperationRecord) -> dict[str, object]:
        category = record.safe_error_category
        status = record.phase.value.upper()
        if record.phase is OperationPhase.ROLLED_BACK:
            status = "PROCESS_RESTORED"
        elif record.phase is OperationPhase.FAILED and ":" in category:
            status = category.split(":", 1)[0]
        elif record.phase is OperationPhase.NEEDS_ATTENTION and category.startswith("UNKNOWN:"):
            status = "UNKNOWN"
        safe_receipt = record.effect.receipt
        no_effect = record.effect.state is EffectState.NOT_SENT or (record.effect.state is EffectState.REJECTED and record.phase is OperationPhase.FAILED)
        return {
            "status": status,
            "operation_id": record.operation_id,
            "action": record.intent.action,
            "target": asdict(record.intent.target),
            "candidate_source_sha": self._params(record).get("source_sha"),
            "phase": record.phase.value,
            "effect_state": record.effect.state.value,
            "disposition": "NOT_STARTED" if no_effect else status,
            "revision": record.revision,
            "safe_reason": category or None,
            "reconnect_url": self.settings.oauth.resource,
            "operation_get": {"tool": "auto_coder_operation_get", "arguments": {"operation_id": record.operation_id}},
            "guidance": "Reconnect to the same MCP URL and query this operation ID. Do not submit a different key while the lane is retained; unknown evidence requires operator inspection of this exact journal and Supervisor receipt.",
            "receipt": None if safe_receipt is None else {"source": safe_receipt.observation_source, "result": safe_receipt.safe_result, "observed_at": safe_receipt.observed_at},
            "verification": self._verification(record),
        }

    def _verification(self, record: OperationRecord) -> dict[str, object]:
        """Expose distinct observed stages without promoting any one to success."""
        if record.effect.state in {EffectState.NOT_SENT, EffectState.REJECTED}:
            return {"handoff_phase": "not_started"}
        try:
            config = self._configuration()
            receipt = HandoffStore(config.state_root, config.instance_id).read(record.operation_id)
            if receipt is None or not self._joined(record, receipt):
                return {"handoff_phase": "unknown"}
            observed = observe_supervisor(config)
            child = observed[1] if observed is not None else None
            running = child is not None and child.phase is ChildPhase.RUNNING and ManagementBootstrapSupervisor._identity(child) == receipt.selected_identity and child.selected_runtime == receipt.handoff.candidate.management_runtime_id
            local_startup = running and child is not None and child.pid == os.getpid() and child.start_token == ManagementBootstrapSupervisor._start_token(os.getpid()) and self.startup_reconciled
            proof = valid_application_proof(receipt, uid=record.authority.uid, client_id=record.authority.oauth_client_id)
            return {
                "handoff_phase": receipt.phase.value,
                "selected_process_running": running,
                "local_startup_reconciled": local_startup,
                "protected_challenge_observed": receipt.challenge_observed,
                "authenticated_readback_observed": proof,
                "verification_deadline": receipt.deadline or None,
            }
        except (ManagementPreflightError, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return {"handoff_phase": "unknown"}

    def _refuse(self, record: OperationRecord, status: str, reason: str) -> OperationRecord:
        evidence = EffectReceipt("not-started:" + record.operation_id, "management-self-update-preflight", time.time(), json.dumps({"status": status, "reason": reason, "process_effect": "not_started"}, sort_keys=True))
        rejected = self.journal.reject_before_effect(record.operation_id, record.revision, category=f"{status}:{reason}", evidence=evidence)
        return rejected.record or record

    def _hold(self, record: OperationRecord, reason: str) -> OperationRecord:
        if record.phase is OperationPhase.NEEDS_ATTENTION:
            return record
        changed = self.journal.transition(record.operation_id, record.revision, OperationPhase.NEEDS_ATTENTION, safe_error_category=reason)
        return changed.record or record

    @staticmethod
    def _ready_for_source(stager: ManagementVersionStager, source_sha: str) -> ManagementReadyReceipt:
        root = stager.config.version_root
        if not root.is_dir():
            raise ManagementPreflightError("prepared_runtime_missing")
        paths = list(root.glob("revisions/*/*/MANAGEMENT_READY.json"))
        if len(paths) > 256:
            raise ManagementPreflightError("too_many_prepared_runtimes")
        matching = []
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("source_sha") == source_sha:
                    matching.append(path)
            except (OSError, ValueError):
                continue
        if len(matching) != 1:
            raise ManagementPreflightError("prepared_runtime_missing_or_ambiguous")
        return stager.lookup(matching[0])

    @staticmethod
    def _disk_preflight(config: BootstrapConfig) -> None:
        databases = (config.oauth_root / "oauth.sqlite3", config.journal_root / "management-operations.sqlite3")
        required = 16 * 1024 * 1024 + sum(path.stat().st_size for path in databases) * 3
        import tempfile

        for root in (config.state_root, config.journal_root, Path(tempfile.gettempdir())):
            if shutil.disk_usage(root).free < required:
                raise ManagementPreflightError("insufficient_disk")

    def forward(self, record: OperationRecord) -> OperationRecord:
        if record.intent.action != _ACTION or record.phase is not OperationPhase.ACCEPTED or record.effect.state is not EffectState.NOT_SENT:
            return record
        try:
            config = self._configuration()
            if record.intent.target != self._target(config):
                return self._refuse(record, "DENIED", "configured_target_mismatch")
            if self.settings.operating_policy_path is None or self.settings.bootstrap_config_path is None:
                return self._refuse(record, "UNAVAILABLE", "operating_policy_unconfigured")
            decision = recheck_management_update_before_effect(self.settings.operating_policy_path, self.settings.bootstrap_config_path, self.grants, self.journal, record.operation_id)
            if decision.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED:
                return self._refuse(record, decision.outcome.value, decision.reason_code.value)
            observed = observe_supervisor(config)
            if observed is None or observed[1].phase is not ChildPhase.RUNNING:
                return self._refuse(record, "UNSUPPORTED", "live_bootstrap_unavailable")
            incarnation, child = observed
            stager = self._stager(config)
            candidate = self._ready_for_source(stager, str(self._params(record).get("source_sha", "")))
            previous_path = HandoffStore(config.state_root, config.instance_id).runtime(child.selected_runtime)
            previous_spec = next((item for item in config.runtimes if item.reference == child.selected_runtime), None)
            receipt_path = Path(previous_path.environment_path).parent / "MANAGEMENT_READY.json" if previous_path is not None else previous_spec.ready_receipt if previous_spec is not None else None
            if receipt_path is None:
                return self._refuse(record, "UNSUPPORTED", "previous_verified_runtime_missing")
            previous = stager.lookup(receipt_path)
            if previous.management_runtime_id != child.selected_runtime or child.source_receipt != previous.receipt_identity or child.runtime_receipt != "sha256:" + previous.python_executable_sha256:
                return self._refuse(record, "NOT_READY", "running_runtime_identity_mismatch")
            if candidate.management_runtime_id == previous.management_runtime_id:
                return self._refuse(record, "NOT_READY", "candidate_already_running")
            self._disk_preflight(config)
            assert config.management_config is not None
            preflight = ManagementSelfUpdatePreflight(config.management_config).verify(candidate, previous)
            # Repeat receipt and process checks after executing target code; no
            # old policy snapshot or READY pathname alone permits the handoff.
            if stager.lookup(Path(candidate.environment_path).parent / "MANAGEMENT_READY.json") != candidate or stager.lookup(receipt_path) != previous:
                return self._refuse(record, "NOT_READY", "prepared_identity_changed")
            if observe_supervisor(config) != (incarnation, child):
                return self._refuse(record, "NOT_READY", "management_lifetime_changed")
            if not runtime_for_receipt(config, candidate).verify() or not runtime_for_receipt(config, previous).verify():
                return self._refuse(record, "NOT_READY", "installed_profile_not_verified")
            decision = recheck_management_update_before_effect(self.settings.operating_policy_path, self.settings.bootstrap_config_path, self.grants, self.journal, record.operation_id)
            if decision.outcome is not ManagementUpdateOutcome.MANAGER_UPDATE_ALLOWED:
                return self._refuse(record, decision.outcome.value, decision.reason_code.value)
            correlation = hashlib.sha256(json.dumps((record.operation_id, record.intent_fingerprint, incarnation, asdict(child), candidate.receipt_identity, previous.receipt_identity, asdict(preflight)), sort_keys=True).encode()).hexdigest()
            attempt = uuid.uuid4().hex
            begun = self.journal.begin_effect(record.operation_id, decision.journal_revision if decision.journal_revision is not None else -1, owner_attempt_id=attempt, attempt_id=attempt, correlation_identity=correlation)
            if begun.status is not MutationStatus.UPDATED or begun.record is None:
                if begun.status is MutationStatus.UNAVAILABLE:
                    logger.warning("Management self-update effect claim unavailable operation={}", record.operation_id)
                    return self._hold(record, "UNKNOWN:effect_claim_persistence_unavailable")
                if begun.status is MutationStatus.INVALID:
                    return self._hold(begun.record or record, "NEEDS_ATTENTION:effect_claim_rejected")
                return begun.record or record
            record = begun.record
            preflight_identity = hashlib.sha256(json.dumps(asdict(preflight), sort_keys=True).encode()).hexdigest()
            handoff = SelfUpdateHandoff(record.operation_id, attempt, correlation, record.effect.owner_generation, incarnation, child, candidate, previous, str(self._params(record)["policy_revision"]), time.time(), preflight_identity)
            HandoffStore(config.state_root, config.instance_id).submit(handoff)
            logger.info("Management self-update handoff committed operation={} candidate={}", record.operation_id, candidate.source_sha)
            return self.reconcile(record)
        except WorkerVersionError as exc:
            status = "UNAVAILABLE" if exc.failure in {PreparationFailure.UNAVAILABLE, PreparationFailure.NETWORK_UNAVAILABLE, PreparationFailure.AUTHENTICATION_UNAVAILABLE} else "NOT_READY"
            return self._refuse(record, status, "prepared_" + exc.failure.value.lower()) if record.effect.state is EffectState.NOT_SENT else self._hold(record, "UNKNOWN:prepared_evidence_unavailable")
        except ManagementPreflightError as exc:
            return self._refuse(record, "NOT_READY", exc.category) if record.effect.state is EffectState.NOT_SENT else self._hold(record, "UNKNOWN:preflight_evidence_unavailable")
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return self._refuse(record, "UNAVAILABLE", "local_evidence_unavailable") if record.effect.state is EffectState.NOT_SENT else self._hold(record, "UNKNOWN:handoff_persistence_unavailable")

    @staticmethod
    def _joined(record: OperationRecord, receipt: HandoffRecord) -> bool:
        handoff = receipt.handoff
        return (
            handoff.operation_id == record.operation_id
            and handoff.attempt_id == record.effect.attempt_id
            and handoff.correlation_identity == record.effect.correlation_identity
            and handoff.effect_generation == record.effect.owner_generation
            and handoff.candidate.instance_id == record.intent.target.instance_id
            and handoff.candidate.source_sha == ManagementSelfUpdateHandler._params(record).get("source_sha")
            and handoff.policy_revision == ManagementSelfUpdateHandler._params(record).get("policy_revision")
        )

    def reconcile(self, record: OperationRecord) -> OperationRecord:
        if record.phase in _TERMINAL or record.effect.state is EffectState.NOT_SENT:
            return record
        try:
            config = self._configuration()
            if record.intent.target != self._target(config):
                return self._hold(record, "UNKNOWN:configured_target_changed")
            receipt = HandoffStore(config.state_root, config.instance_id).read(record.operation_id)
            if receipt is None or not self._joined(record, receipt):
                return self._hold(record, "UNKNOWN:handoff_missing_or_conflicting")
            terminal = {
                HandoffPhase.SUCCEEDED: OperationPhase.SUCCEEDED,
                HandoffPhase.PROCESS_RESTORED: OperationPhase.ROLLED_BACK,
                HandoffPhase.DENIED: OperationPhase.FAILED,
                HandoffPhase.UNAVAILABLE: OperationPhase.FAILED,
            }.get(receipt.phase)
            if terminal is None:
                return self._hold(record, "NEEDS_ATTENTION:" + receipt.detail) if receipt.phase is HandoffPhase.NEEDS_ATTENTION else record
            if terminal in {OperationPhase.SUCCEEDED, OperationPhase.ROLLED_BACK}:
                observed = observe_supervisor(config)
                expected = receipt.selected_identity if terminal is OperationPhase.SUCCEEDED else receipt.restored_identity
                selected = receipt.handoff.candidate if terminal is OperationPhase.SUCCEEDED else receipt.handoff.previous
                if observed is None or selected is None or ManagementBootstrapSupervisor._identity(observed[1]) != expected or observed[1].phase is not ChildPhase.RUNNING or observed[1].selected_runtime != selected.management_runtime_id:
                    return self._hold(record, "UNKNOWN:terminal_process_evidence_unavailable")
                if observed[1].source_receipt != selected.receipt_identity or observed[1].runtime_receipt != "sha256:" + selected.python_executable_sha256 or not runtime_for_receipt(config, selected).verify():
                    return self._hold(record, "UNKNOWN:terminal_installed_identity_unavailable")
                if terminal is OperationPhase.SUCCEEDED and not valid_application_proof(receipt, uid=record.authority.uid, client_id=record.authority.oauth_client_id):
                    return self._hold(record, "UNKNOWN:authenticated_proof_unavailable")
            result = json.dumps({"status": receipt.phase.value, "source_sha": receipt.handoff.candidate.source_sha, "runtime_id": receipt.handoff.candidate.management_runtime_id, "generation": receipt.selected_generation}, sort_keys=True)
            evidence = EffectReceipt(record.effect.correlation_identity, "management-supervisor-authenticated-receipt", time.time(), result)
            state = EffectState.REJECTED if terminal is OperationPhase.FAILED else EffectState.CONFIRMED
            if record.effect.state is EffectState.SENT_UNKNOWN:
                mutated = self.journal.record_effect(record.operation_id, record.revision, attempt_id=record.effect.attempt_id, state=state, receipt=evidence)
                if mutated.record is None or mutated.status is not MutationStatus.UPDATED:
                    return mutated.record or record
                record = mutated.record
            category = f"{receipt.phase.value.upper()}:{receipt.detail}" if terminal is OperationPhase.FAILED else ""
            if record.phase is OperationPhase.NEEDS_ATTENTION:
                ended = self.journal.resolve_needs_attention(record.operation_id, record.revision, terminal, evidence=record.effect.receipt or evidence)
            else:
                ended = self.journal.transition(record.operation_id, record.revision, terminal, safe_error_category=category, evidence=record.effect.receipt or evidence)
            return ended.record or record
        except (ManagementPreflightError, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            return self._hold(record, "UNKNOWN:supervisor_evidence_unavailable")

    def observe_authenticated_identity(self, grant: AuthenticatedGrant) -> None:
        """Persist proof only from the real selected process's authenticated path."""
        operation_id = os.environ.get("AUTO_CODER_SELF_UPDATE_OPERATION_ID", "")
        if not self.startup_reconciled or not _OPERATION.fullmatch(operation_id):
            return
        try:
            lookup = self.journal.get(operation_id)
            record = lookup.record
            if record is None or record.intent.action != _ACTION or (record.authority.project_id, record.authority.uid, record.authority.oauth_client_id, record.authority.resource) != (grant.principal.project_id, grant.principal.uid, grant.client_id, grant.resource):
                return
            config = self._configuration()
            store = HandoffStore(config.state_root, config.instance_id)
            receipt = store.read(operation_id)
            observed = observe_supervisor(config)
            if receipt is None or not self._joined(record, receipt) or observed is None or receipt.phase is not HandoffPhase.AWAITING_PROOF:
                return
            child = observed[1]
            candidate = receipt.handoff.candidate
            identity = ManagementBootstrapSupervisor._identity(child)
            if child.pid != os.getpid() or child.start_token != ManagementBootstrapSupervisor._start_token(os.getpid()) or identity != receipt.selected_identity or child.child_generation != receipt.selected_generation or Path(sys.prefix).resolve() != Path(candidate.environment_path).resolve():
                return
            import auto_coder

            if auto_coder.__file__ is None or not Path(auto_coder.__file__).resolve().is_relative_to(Path(candidate.environment_path).resolve()):
                return
            artifact = observe_controller_artifact()
            if not artifact.source_revision.available or not artifact.distribution_version.available or artifact.source_revision.value != candidate.source_sha or artifact.distribution_version.value != candidate.distribution_version or not runtime_for_receipt(config, candidate).verify():
                return
            proof = ApplicationProof(identity, candidate.management_runtime_id, candidate.receipt_identity, candidate.source_sha, grant.principal.uid, grant.client_id, grant.grant_id, time.time())
            store.record_proof(operation_id, proof)
        except (ManagementPreflightError, OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # Existing identity responses remain available even if update proof
            # persistence fails. The operation itself remains unresolved.
            logger.warning("Management reconnect proof unavailable operation={}", operation_id)
