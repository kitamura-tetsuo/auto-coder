"""Deployment-fixed one-shot host issuer for management recovery ownership.

Original evidence and current containment are independently re-derived here.
Only this issuer combines those facts with exact target READY and physically
verified consumer custody, durably consumes the epoch, and sends one grant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import stat
import sys
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from auto_coder.logger_config import get_logger, setup_logger

from .bootstrap_supervisor import ManagementBootstrapSupervisor
from .containment_kernel_subobservation import _require_capture_host
from .installed_bootstrap_mount_profile import _inspect as inspect_container
from .original_attempt_evidence import OriginalLineageEvidence, derive_lineage, verify_lineage
from .original_attempts import canonical, source_identity
from .quarantine_history import InspectionReport, QuarantineHistory, QuarantineJournal
from .recovery_containment import RecoveryContainmentReport, load_enrollment
from .recovery_containment import observe as observe_containment
from .recovery_containment import verify as verify_containment
from .recovery_containment_model import RecoveryEnrollment, RecoveryEnrollmentObservation
from .recovery_diagnostics import refusal_causes as _refusal_causes
from .recovery_epoch_authorization import (
    PEER_ROOT,
    PEER_SOCKET,
    AuthorizationObservation,
    EpochAuthorization,
    TargetReadyObservation,
    check_authorization,
    load_authorization,
    observe_target_ready,
    read_bound_file,
    verify_target_ready,
)
from .recovery_epoch_consumer import GRANT_SCHEMA, parse_consumer_configuration
from .recovery_epoch_peer import ExpectedPeer, HeldFence, HeldResource, PeerCustody, PeerSession, encode_peer_message, inspect_held_resource
from .recovery_epoch_resources import EpochResources, hold_epoch_resources
from .recovery_epoch_state import EpochBinding, EpochInventory, EpochReceipt, EpochUnavailable, RecoveryEpochState, hold_recovery_epoch, issuer_lifetime

logger = get_logger(__name__)
_OVERRIDES = ("AUTO_CODER_RECOVERY_EPOCH_ROOT", "AUTO_CODER_RECOVERY_EPOCH_INVENTORY", "AUTO_CODER_RECOVERY_AUTHORIZATION_ROOT", "AUTO_CODER_RECOVERY_CONTAINMENT_ROOT")


@dataclass(frozen=True)
class VerifiedEpochEvidence:
    lineage: OriginalLineageEvidence
    containment: RecoveryContainmentReport
    target: TargetReadyObservation
    inspection: InspectionReport
    custody: PeerCustody


@dataclass(frozen=True)
class EpochDispatch:
    receipt: EpochReceipt
    evidence_sha256: str
    dispatch_attempted: bool
    # A successful send is not acknowledgment, child launch or current readiness.
    current_ready: bool = False


def join_lineage(authorization: EpochAuthorization, inventory: EpochInventory, enrollment: RecoveryEnrollmentObservation, lineage: OriginalLineageEvidence, containment: RecoveryContainmentReport) -> None:
    """Match actual complete original derivation to the entire observed scope."""
    selected = enrollment.enrollment
    if lineage.unavailable or lineage.genesis is None or not lineage.generations or lineage.binding != authorization.history:
        raise EpochUnavailable("complete original genesis and retained lineage are unavailable")
    old_attempts = {generation.request_id for generation in lineage.generations}
    if authorization.request_id in old_attempts or authorization.authorization_id == lineage.genesis.authorization_id:
        raise EpochUnavailable("recovery requires new authorization and a new request identity")
    if containment.status != "observed-current-recovery-containment" or containment.grant_issued or containment.history_asserted or containment.admission_created or containment.enrollment != enrollment:
        raise EpochUnavailable("current containment is not the exact non-authorizing prerequisite")
    if selected.deployment_id != lineage.binding.deployment or selected.source_identity != lineage.binding.source or selected.source_identity != source_identity() or selected.source_identity != "sha256:" + inventory.source_sha256:
        raise EpochUnavailable("original and current source/deployment identities differ")
    if selected.target.management_id != inventory.target_id or selected.target.bootstrap_config.sha256 != inventory.config_sha256:
        raise EpochUnavailable("exact target differs from deployment-fixed inventory")
    if selected.engine_id != lineage.genesis.engine_id:
        raise EpochUnavailable("current Docker engine differs from original genesis")
    original_by_id = {item.management_id: item for item in selected.originals}
    generations = {item.management_id: item for item in lineage.generations}
    facts = {item.container_id: item for item in containment.managers}
    if len(generations) != len(lineage.generations) or len(facts) != len(containment.managers) or set(original_by_id) - set(generations):
        raise EpochUnavailable("current recovery enrollment omits or aliases original lineage")
    if set(facts) != {*original_by_id, selected.target.management_id}:
        raise EpochUnavailable("current effective manager scope differs from the exact enrollment")
    for identifier, generation in generations.items():
        original = original_by_id.get(identifier)
        if original is None:
            if generation.outcome != "never-created" or identifier in containment.docker_ids or generation.owner_inode:
                raise EpochUnavailable("original generation is absent from current full-scope containment")
            continue
        fact = facts[identifier]
        if (original.instance_id, original.management_image_id, original.bootstrap_config.sha256, original.management_config.sha256) != (generation.instance_id, generation.image_id, generation.bootstrap_config_sha256, generation.management_config_sha256):
            raise EpochUnavailable("current original configuration differs from authoritative lineage")
        if generation.proxy_id and (original.proxy_id, original.proxy_image_id) != (generation.proxy_id, generation.proxy_image_id):
            raise EpochUnavailable("original proxy differs from genesis provenance")
        if (fact.state_root, fact.journal_root, fact.oauth_root) != (generation.state_root, generation.journal_root, generation.oauth_root):
            raise EpochUnavailable("original root mapping differs from current effective configuration")
        for path, device, inode in ((generation.state_root, generation.state_device, generation.state_inode), (generation.journal_root, generation.journal_device, generation.journal_inode), (generation.oauth_root, generation.oauth_device, generation.oauth_inode)):
            roots = [mount for mount in fact.mounts if mount.source == path and mount.writable]
            if len(roots) != 1 or (roots[0].device, roots[0].inode) != (device, inode):
                raise EpochUnavailable("original root inode differs from current Docker mapping")
        if generation.outcome == "never-created":
            if original.state != "never-started" or original.owner is not None or generation.owner_inode:
                raise EpochUnavailable("original never-created boundary conflicts with current facts")
        elif original.state != "started" or original.owner is None or (original.owner.path, original.owner.device, original.owner.inode) != (generation.owner_path, generation.owner_device, generation.owner_inode):
            raise EpochUnavailable("current original owner differs from native original provenance")
    target = facts[selected.target.management_id]
    oauth = lineage.genesis.root("oauth")
    roots = [item for item in target.mounts if item.writable and item.source == oauth.host_path]
    if target.oauth_root != oauth.host_path or len(roots) != 1 or (roots[0].device, roots[0].inode) != (oauth.device, oauth.inode):
        raise EpochUnavailable("recovery target does not preserve the exact original OAuth store")
    original_paths = [Path(value) for generation in lineage.generations for value in (generation.state_root, generation.journal_root, generation.oauth_root)]
    for target_root in (Path(target.state_root), Path(target.journal_root)):
        if any(target_root == original or target_root in original.parents or original in target_root.parents for original in original_paths):
            raise EpochUnavailable("recovery target reuses or overlaps an original root")


def _quarantine_entries(lineage: OriginalLineageEvidence) -> tuple[QuarantineJournal, ...]:
    originals = {item.path: item for item in lineage.originals}
    result = []
    for generation in lineage.generations:
        if generation.outcome == "never-created":
            continue
        stores = dict(generation.expected_stores)
        original = originals.get(stores.get("operations", ""))
        owner = originals.get(generation.owner_path)
        if original is None or owner is None:
            raise EpochUnavailable("original journal or owner fingerprint is missing")
        database, fence = Path(original.path), Path(owner.path)
        database_info, fence_info = database.lstat(), fence.lstat()
        result.append(QuarantineJournal(generation.instance_id, database, fence, original.device, original.inode, original.sha256, database_info.st_ctime_ns, owner.device, owner.inode, fence_info.st_ctime_ns))
    return tuple(result)


def _check_consumer_scope(authorization: EpochAuthorization, enrollment: RecoveryEnrollment, resources: EpochResources) -> None:
    selected = parse_consumer_configuration(read_bound_file(authorization.consumer_config))
    if (selected.request_id, selected.authorization_id, selected.target_id, selected.issuer_uid, selected.socket_directory_device, selected.socket_directory_inode) != (
        authorization.request_id,
        authorization.authorization_id,
        authorization.target_id,
        0,
        authorization.socket_directory_device,
        authorization.socket_directory_inode,
    ):
        raise EpochUnavailable("consumer's pinned authorization differs")
    peer_mounts = [item for item in enrollment.target.mounts if item.destination == "/management-recovery"]
    if len(peer_mounts) != 1 or peer_mounts[0].source != str(PEER_ROOT) or peer_mounts[0].writable or (peer_mounts[0].device, peer_mounts[0].inode) != (authorization.socket_directory_device, authorization.socket_directory_inode):
        raise EpochUnavailable("consumer lacks the exact read-only issuer endpoint mount")
    original_paths = {"history": ("/management-recovery-history/history.lock", str(Path(authorization.history.root) / "history.lock"))}
    original_paths.update({f"owner-{index}": (f"/management-recovery-originals/{index}/owner.lock", item.owner.path) for index, item in enumerate(enrollment.originals) if item.owner is not None})
    held: tuple[HeldFence | HeldResource, ...] = (*resources.fences, *resources.namespaces)
    observed = tuple(inspect_held_resource(item) for item in held)
    originals = {(item.name, item.device, item.inode) for item in observed if item.kind == "original-sh"}
    if {(item.name, item.device, item.inode) for item in selected.original_fences} != originals or len(selected.original_fences) != len(originals):
        raise EpochUnavailable("consumer omitted or changed an independently acquired original fence")
    for original in selected.original_fences:
        container_path, host_path = original_paths[original.name]
        mounts = [item for item in enrollment.target.mounts if item.destination == container_path]
        if original.path != container_path or len(mounts) != 1 or mounts[0].source != host_path or mounts[0].writable or (mounts[0].device, mounts[0].inode) != (original.device, original.inode):
            raise EpochUnavailable("consumer original fence is not the exact read-only mounted inode")
    target = next(item for item in observed if item.kind == "target-ex")
    if (selected.target_owner.name, selected.target_owner.device, selected.target_owner.inode) != (target.name, target.device, target.inode):
        raise EpochUnavailable("consumer's independent target owner differs")
    transfers = {(item.name, item.kind, item.device, item.inode) for item in observed if item.kind in ("transferred-ex", "network-namespace")}
    if {(item.name, item.kind, item.device, item.inode) for item in selected.transferred_resources} != transfers or len(selected.transferred_resources) != len(transfers):
        raise EpochUnavailable("consumer omitted or changed controller, deployment or namespace custody")


def _expected_peer(authorization: EpochAuthorization) -> ExpectedPeer:
    actual = inspect_container(authorization.target_id)
    state = actual.get("State")
    if actual.get("Id") != authorization.target_id or not isinstance(state, dict) or state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 1:
        raise EpochUnavailable("exact recovery target is not a live bootstrap consumer")
    pid = state["Pid"]
    token = ManagementBootstrapSupervisor._start_token(pid)
    if not token:
        raise EpochUnavailable("exact recovery target process lifetime is unavailable")
    return ExpectedPeer(pid, token, issuer_lifetime()[0], 0, authorization.request_id, authorization.authorization_id, authorization.target_id)


def _match_peer_report(session: PeerSession, report: RecoveryContainmentReport) -> None:
    if (report.target_pid, report.target_start_token, report.facts.target.boot_id) != (session.expected.pid, session.expected.start_token, session.expected.boot_id):
        raise EpochUnavailable("authenticated peer differs from currently observed Docker target")


def _binding(state: RecoveryEpochState, authorization: AuthorizationObservation, evidence: VerifiedEpochEvidence) -> EpochBinding:
    boot, pid, start = issuer_lifetime()
    # Retain all original file fingerprints and current scope/custody identities;
    # the independently verified controller/kernel report is bound in full by
    # its canonical evidence digest, not by an unverified success flag.
    original = {
        "schema": "verified-management-recovery-evidence-v1",
        "authorization": asdict(authorization),
        "lineage": asdict(evidence.lineage),
        "containment_sha256": evidence.containment.evidence_sha256,
        "containment_enrollment": asdict(evidence.containment.enrollment),
        "controller_fences": [asdict(item) for item in evidence.containment.facts.watchers],
        "current_holder": asdict(evidence.containment.facts.resources),
        "target": asdict(evidence.target),
        "inspection": asdict(evidence.inspection),
        "custody": asdict(evidence.custody),
    }
    encoded = canonical(original)
    inventory = state.inventory
    return EpochBinding(
        inventory.deployment_id,
        inventory.authorization_id,
        inventory.enrollment_ids,
        inventory.request_id,
        inventory.preparation_id,
        inventory.target_id,
        inventory.source_sha256,
        inventory.config_sha256,
        hashlib.sha256(encoded).hexdigest(),
        boot,
        pid,
        start,
        inventory.authorization_sha256,
        encoded.decode(),
    )


def issue_epoch_once(state: RecoveryEpochState, authorization: AuthorizationObservation, enrollment: RecoveryEnrollmentObservation, resources: EpochResources, session: PeerSession) -> tuple[EpochDispatch, PeerCustody]:
    """Verify, irreversibly consume and attempt exactly one current peer grant."""
    selected = enrollment.enrollment
    approved = authorization.authorization
    check_authorization(authorization, state.inventory)
    _check_consumer_scope(approved, selected, resources)
    target = observe_target_ready(approved, selected)
    lineage = derive_lineage(approved.history, resources.history_descriptor, resources.original_descriptors)
    lineage = verify_lineage(lineage, approved.history, resources.history_descriptor, resources.original_descriptors)
    with QuarantineHistory(_quarantine_entries(lineage)) as history:
        inspection = history.inspect()
        containment = observe_containment(approved.enrollment_id, resources.containment)
        join_lineage(approved, state.inventory, enrollment, lineage, containment)
        _match_peer_report(session, containment)
        custody = session.offer_and_verify_custody(resources.fences, resources.namespaces)
        evidence = VerifiedEpochEvidence(lineage, containment, target, inspection, custody)
        binding = _binding(state, authorization, evidence)
        state.reserve(binding)
        state.record_intent(binding)

        def final_verification() -> None:
            check_authorization(authorization, state.inventory)
            if load_enrollment(approved.enrollment_id) != enrollment:
                raise EpochUnavailable("protected recovery enrollment changed at issuance")
            verify_target_ready(approved, selected, target)
            verify_lineage(lineage, approved.history, resources.history_descriptor, resources.original_descriptors)
            if history.inspect() != inspection:
                raise EpochUnavailable("quarantined original exclusions changed before grant")
            current = verify_containment(approved.enrollment_id, containment, resources.containment)
            join_lineage(approved, state.inventory, enrollment, lineage, current)
            _match_peer_report(session, current)
            _check_consumer_scope(approved, selected, resources)
            resources.check()
            session.check(custody)
            state.check()

        final_verification()
        receipt = state.consume(binding)
        # Persistence can block or be interrupted. Its completion is never
        # substituted for a current containment/custody/READY observation.
        final_verification()
        packet = encode_peer_message(
            {
                "schema": GRANT_SCHEMA,
                "granted": "recovery-epoch-consumed",
                **{name: getattr(approved, name) for name in ("request_id", "authorization_id", "target_id")},
                "challenge": custody.challenge,
                "receipt_nonce": custody.receipt_nonce,
                **{name: getattr(receipt, name) for name in ("inventory_sha256", "binding_sha256", "consumed_sha256", "receipt_sha256")},
                "host_boot_id": binding.host_boot_id,
                "host_pid": binding.host_pid,
                "host_start_token": binding.host_start_token,
            }
        )
        session.check(custody)
        state.check()
        # A partial/failed send, lost reply or exception has no retry path.
        session.connection.settimeout(10)
        session.connection.sendall(packet)
        logger.bind(recovery_epoch_phase="grant-send-completed", request_id=approved.request_id, target_id=approved.target_id, current_ready=False).info("One consumed recovery epoch grant send completed; delivery and readiness are not inferred")
        return EpochDispatch(receipt, binding.evidence_sha256, True), custody


@contextmanager
def _listen(authorization: AuthorizationObservation) -> Iterator[socket.socket]:
    root = PEER_ROOT.stat()
    expected = authorization.authorization
    if (root.st_dev, root.st_ino) != (expected.socket_directory_device, expected.socket_directory_inode) or PEER_SOCKET.exists() or PEER_SOCKET.is_symlink():
        raise EpochUnavailable("fixed recovery socket directory or original endpoint is unavailable")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = None
    try:
        listener.bind(str(PEER_SOCKET))
        os.chmod(PEER_SOCKET, 0o600)
        bound = PEER_SOCKET.lstat()
        if not stat.S_ISSOCK(bound.st_mode) or bound.st_uid != 0:
            raise EpochUnavailable("recovery local endpoint is not protected")
        listener.listen(1)
        listener.settimeout(600)
        logger.bind(recovery_epoch_phase="listening", request_id=expected.request_id, target_id=expected.target_id).info("Fixed recovery issuer endpoint is listening for its exact target")
        yield listener
    finally:
        listener.close()
        if bound is not None:
            try:
                current = PEER_SOCKET.lstat()
                if (current.st_dev, current.st_ino) == (bound.st_dev, bound.st_ino):
                    PEER_SOCKET.unlink()
            except FileNotFoundError:
                pass


def run_issuer() -> EpochDispatch:
    """Own one current epoch until the target exits or its custody is lost."""
    if any(name in os.environ for name in _OVERRIDES):
        raise EpochUnavailable("request/environment recovery inventory overrides are unsupported")
    _require_capture_host()
    stopped = threading.Event()
    handlers: dict[int, object] = {}
    with ExitStack() as held:
        state = held.enter_context(hold_recovery_epoch())
        authorization = load_authorization(state.inventory)
        enrollment = load_enrollment(authorization.authorization.enrollment_id)
        resources = held.enter_context(hold_epoch_resources(authorization.authorization, enrollment.enrollment, state))
        listener = held.enter_context(_listen(authorization))
        connection, _ = listener.accept()
        try:
            session = held.enter_context(PeerSession(connection, _expected_peer(authorization.authorization), timeout=60))
        except BaseException:
            connection.close()
            raise
        listener.close()
        dispatch, custody = issue_epoch_once(state, authorization, enrollment, resources, session)
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, lambda *_: stopped.set())
        try:
            while not stopped.wait(0.2):
                check_authorization(authorization, state.inventory)
                resources.check()
                session.check(custody)
                state.check()
            return dispatch
        finally:
            for saved_signal, handler in handlers.items():
                signal.signal(saved_signal, handler)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue one approved management ownership epoch from fixed host authority")
    parser.parse_args(argv)
    setup_logger(stream=sys.stdout, log_file="/var/log/auto-coder/recovery-epoch.log")
    try:
        result = run_issuer()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        logger.bind(recovery_epoch_phase="unavailable", current_ready=False).error("Recovery epoch unavailable; preserved evidence is not repaired: {}", error)
        for index, cause in enumerate(_refusal_causes(error), start=1):
            logger.bind(recovery_epoch_phase="unavailable", current_ready=False).error("Recovery epoch refusal cause {}: {}", index, json.dumps(cause, sort_keys=True, separators=(",", ":")))
        return 2
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
