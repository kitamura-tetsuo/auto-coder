"""Owning-Supervisor consumer for the fixed local recovery mailbox."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import secrets
import sqlite3
import subprocess
import time
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from .bootstrap_store import ChildPhase, ChildRecord
from .bootstrap_supervisor import ManagementBootstrapSupervisor, ResultStatus, RuntimeSpec
from .oauth_config import load_management_settings
from .readiness_observation import read_authenticated_readback
from .recovery_evidence import EvidenceUnavailable, assert_current_owner, assert_journal_clear, assert_kernel_absence, is_completed_safety_restore, read_child_evidence
from .recovery_mailbox import RecoveryIntent, RecoveryMailbox, RecoveryRecord
from .self_update_handoff import HandoffStore, runtime_for_receipt


class RecoveryConsumer:
    def __init__(self, supervisor: ManagementBootstrapSupervisor, bootstrap_config_path: Path) -> None:
        self.supervisor = supervisor
        self.config = supervisor.config
        self.bootstrap_config_path = bootstrap_config_path
        assert_current_owner(self.config, self.supervisor.incarnation)
        self.mailbox = RecoveryMailbox(self.config.state_root, self.config.instance_id)
        self.mailbox.initialize()
        connection = self.mailbox.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM recovery_requests WHERE status='claimed'").fetchall()
            for row in rows:
                self.mailbox._record(row)
            connection.execute("UPDATE recovery_requests SET status='unknown',detail='owner interrupted after durable claim',updated_at=? WHERE status='claimed'", (time.time(),))
            connection.commit()
            for row in rows:
                logger.info("Management local recovery outcome request={} status=unknown", row["request_id"])
        finally:
            connection.close()

    @staticmethod
    def _locked(path: Path) -> sqlite3.Connection:
        if path.is_symlink() or not path.is_file():
            raise EvidenceUnavailable("required store unavailable")
        connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
        except Exception:
            connection.close()
            raise
        return connection

    def _runtime(self, reference: str) -> RuntimeSpec | None:
        spec = next((item for item in self.config.runtimes if item.reference == reference), None)
        if spec is None and self.config.management_version_root is not None:
            receipt = HandoffStore(self.config.state_root, self.config.instance_id).runtime(reference, readonly=True)
            if receipt is not None:
                spec = runtime_for_receipt(self.config, receipt)
        return spec

    def _settings(self, intent: RecoveryIntent, runtime: RuntimeSpec):
        path = Path(intent.management_config)
        settings = load_management_settings(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != intent.management_config_sha256:
            raise EvidenceUnavailable("management configuration changed")
        if settings.config_path != path or settings.bootstrap_config_path != self.bootstrap_config_path or settings.state_root != self.config.oauth_root or settings.operation_state_root != self.config.journal_root:
            raise EvidenceUnavailable("management configuration differs from bootstrap")
        if self.config.management_config is not None:
            if settings.config_path != self.config.management_config:
                raise EvidenceUnavailable("management configuration is not fixed by bootstrap")
        else:
            positions = [index for index, argument in enumerate(runtime.arguments) if argument == "--config"]
            if len(positions) != 1 or positions[0] + 1 >= len(runtime.arguments) or runtime.arguments[positions[0] + 1] != str(path):
                raise EvidenceUnavailable("management configuration is not fixed by the selected invocation")
        endpoint = urlsplit(self.config.listener_url)
        if endpoint.hostname != settings.bind_host or endpoint.port != settings.port or endpoint.path != "/mcp":
            raise EvidenceUnavailable("management listener differs from bootstrap")
        return settings

    def blocks_self_update(self) -> bool:
        """Hold physical self-update effects while a recovery may have launched."""
        try:
            connection = self.mailbox.connect(readonly=True)
            try:
                records = (self.mailbox._record(row) for row in connection.execute("SELECT * FROM recovery_requests"))
                return any(record.status in {"claimed", "launched", "unknown"} for record in records)
            finally:
                connection.close()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return True

    def tick(self) -> None:
        try:
            connection = self.mailbox.connect()
            try:
                rows = connection.execute("SELECT * FROM recovery_requests ORDER BY created_at,request_id").fetchall()
                records = tuple(self.mailbox._record(row) for row in rows)
            finally:
                connection.close()
            pending = next((record for record in records if record.status == "pending"), None)
            launched = next((record for record in records if record.status == "launched"), None)
            if pending is not None:
                self._admit(pending, records)
            elif launched is not None:
                self._observe(launched)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            logger.warning("Management recovery observation unavailable")

    def _set(self, request_id: str, expected: str, status: str, detail: str, *, observation_id: str | None = None, child_identity: str | None = None, started: float | None = None, deadline: float | None = None) -> bool:
        connection = self.mailbox.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE recovery_requests SET status=?,detail=?,observation_id=COALESCE(?,observation_id),child_identity=COALESCE(?,child_identity),observation_started=COALESCE(?,observation_started),observation_deadline=COALESCE(?,observation_deadline),updated_at=? WHERE request_id=? AND status=?",
                (status, detail, observation_id, child_identity, started, deadline, time.time(), request_id, expected),
            )
            connection.commit()
            if cursor.rowcount == 1:
                logger.info("Management local recovery outcome request={} status={}", request_id, status)
                return True
            return False
        finally:
            connection.close()

    def _admit(self, record: RecoveryRecord, records: tuple[RecoveryRecord, ...]) -> None:
        try:
            assert_current_owner(self.config, self.supervisor.incarnation)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            return
        intent = record.intent
        with self.supervisor._lock:
            try:
                if any(
                    other.intent.request_id != intent.request_id and (other.status in {"claimed", "launched", "unknown"} or (other.intent.child_generation == intent.child_generation and other.intent.supervisor_incarnation == intent.supervisor_incarnation and other.status == "succeeded"))
                    for other in records
                ):
                    raise EvidenceUnavailable("another recovery retains this lane")
                with ExitStack() as stack:
                    journal_lock = self._locked(self.config.journal_root / "management-operations.sqlite3")
                    stack.callback(journal_lock.close)
                    handoff_lock = self._locked(self.config.state_root / "management-self-update.sqlite3")
                    stack.callback(handoff_lock.close)
                    assert_journal_clear(self.config)
                    child, previous = read_child_evidence(self.config)
                    if (intent.instance_id, intent.supervisor_incarnation, intent.child_generation, intent.runtime, intent.runtime_receipt, intent.source_receipt) != (
                        self.config.instance_id,
                        self.supervisor.incarnation,
                        child.child_generation,
                        child.selected_runtime,
                        child.runtime_receipt,
                        child.source_receipt,
                    ):
                        raise EvidenceUnavailable("expected child or owner differs")
                    completed_child = any(
                        other.status == "succeeded"
                        and child.effect_operation_id == "recovery-" + other.intent.request_id
                        and other.intent.child_generation + 1 == child.child_generation
                        and other.intent.supervisor_incarnation == child.supervisor_incarnation
                        and other.child_identity == f"{previous.pid}:{previous.start_token}:{previous.generation}:{previous.runtime}:{previous.incarnation}"
                        for other in records
                    )
                    if child.supervisor_incarnation != self.supervisor.incarnation or (child.recovery_used and not completed_child and not is_completed_safety_restore(self.config, child, previous)):
                        raise EvidenceUnavailable("child ownership or earlier recovery is unresolved")
                    runtime = self._runtime(child.selected_runtime)
                    if runtime is None or (runtime.receipt, runtime.source_receipt) != (child.runtime_receipt, child.source_receipt) or not runtime.verify(initial=runtime.reference == self.config.initial_runtime):
                        raise EvidenceUnavailable("selected runtime is unavailable or changed")
                    self._settings(intent, runtime)
                    assert_kernel_absence(self.config, previous)
                    assert_current_owner(self.config, self.supervisor.incarnation)
                    if not self._set(intent.request_id, "pending", "claimed", "durable effect claimed"):
                        return
                    self._launch(intent, child, runtime)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                self._set(intent.request_id, "pending", "refused", "recovery evidence unavailable or conflicting")

    def _launch(self, intent: RecoveryIntent, child: ChildRecord, runtime: RuntimeSpec) -> None:
        operation = "recovery-" + intent.request_id
        attempt = str(uuid.uuid4())
        try:
            effect = self.supervisor.store.begin_effect(operation, attempt, "RECOVER_SELECTED", runtime.reference, self.supervisor._identity(child))
            if effect.phase != "prepared":
                self._set(intent.request_id, "claimed", "unknown", "effect outcome unavailable")
                return
            prepared = replace(
                child,
                phase=ChildPhase.STARTING,
                child_generation=child.child_generation + 1,
                supervisor_incarnation=self.supervisor.incarnation,
                intended_stop=False,
                readiness="not_observed",
                recovery_used=True,
                exit_cause="",
                effect_operation_id=operation,
                effect_attempt_id=attempt,
                drain_nonce=str(uuid.uuid4()),
            )
            if not self.supervisor.store.reserve(child, prepared):
                self._set(intent.request_id, "claimed", "unknown", "child transition conflicted after claim")
                return
            if not runtime.verify(initial=runtime.reference == self.config.initial_runtime):
                self._set(intent.request_id, "claimed", "unknown", "runtime verification failed after claim")
                return
            self._settings(intent, runtime)
            environment = dict(runtime.environment)
            environment.update(self.supervisor._worker_bridge_environment)
            environment.update({"AUTO_CODER_CHILD_GENERATION": str(prepared.child_generation), "AUTO_CODER_DRAIN_NONCE": prepared.drain_nonce})
            if runtime.execution_kind == "management-installed":
                environment.update({"AUTO_CODER_MANAGEMENT_RUNTIME_ID": runtime.reference, "AUTO_CODER_MANAGEMENT_RECEIPT_ID": runtime.source_receipt})
            if runtime.drain_ack is not None:
                environment["AUTO_CODER_DRAIN_RECEIPT"] = str(runtime.drain_ack)
            process = subprocess.Popen([str(runtime.executable), *runtime.arguments], env=environment, start_new_session=False)
            start_token = self.supervisor._start_token(process.pid)
            if not start_token:
                self._set(intent.request_id, "claimed", "unknown", "launched process lifetime unavailable")
                return
            self.supervisor._children[process.pid] = process
            running = replace(prepared, phase=ChildPhase.RUNNING, pid=process.pid, start_token=start_token)
            if not self.supervisor.store.reserve(prepared, running):
                self._set(intent.request_id, "claimed", "unknown", "launched process observation conflicted")
                return
            self.supervisor._finish(operation, ResultStatus.OBSERVED_RUNNING, running)
            now = time.time()
            self._set(intent.request_id, "claimed", "launched", "process launched; authenticated readback pending", observation_id=secrets.token_hex(16), child_identity=self.supervisor._identity(running), started=now, deadline=now + self.config.verification_timeout)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            # An uncertain effect is never retried, even if recording this
            # outcome itself fails and the original claim remains.
            try:
                self._set(intent.request_id, "claimed", "unknown", "launch outcome unavailable")
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                pass

    def _observe(self, record: RecoveryRecord) -> None:
        try:
            assert_current_owner(self.config, self.supervisor.incarnation)
            if time.time() > record.observation_deadline:
                self._set(record.intent.request_id, "launched", "unknown", "authenticated readiness unavailable")
                return
            runtime = self._runtime(record.intent.runtime)
            if runtime is None:
                return
            settings = self._settings(record.intent, runtime)
            current = self.supervisor.inspect()
            if not current.process_ready or self.supervisor._identity(current.record) != record.child_identity:
                self._set(record.intent.request_id, "launched", "unknown", "selected child lifetime unavailable")
                return
            endpoint = urlsplit(self.config.listener_url)
            if endpoint.hostname is None or endpoint.port is None:
                return
            client = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=1)
            try:
                client.request("GET", endpoint.path, headers={"Host": urlsplit(settings.oauth.canonical_issuer).netloc})
                response = client.getresponse()
                challenge = response.status == 401 and "Bearer" in response.getheader("WWW-Authenticate", "")
                response.read()
            finally:
                client.close()
            if not challenge:
                return
            proof = read_authenticated_readback(self.config, observation_id=record.observation_id, observation_started_at=record.observation_started, observation_ends_at=record.observation_deadline)
            if proof is not None and proof.child_identity == record.child_identity and proof.management_config_sha256 == record.intent.management_config_sha256:
                assert_current_owner(self.config, self.supervisor.incarnation)
                self._set(record.intent.request_id, "launched", "succeeded", "fresh protected challenge and authenticated child readback observed", observation_id=record.observation_id, child_identity=record.child_identity, started=record.observation_started, deadline=record.observation_deadline)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, http.client.HTTPException):
            return
