"""Once-per-generation, scoped verification of completed local corrections."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .adversarial_validator import AdversarialValidationResult, UnverifiedLocalRepair, _extract_thread_dispositions
from .backend_manager import BackendManager, run_llm_prompt
from .durable_repair_allowance import GenerationLifecycleState, RepairAllowanceLedger, ValidationObservation
from .local_review_repair import LocalReviewRepairRequest, LocalReviewRepairStore, local_review_repair_db_path, settle_local_review_repair_validation
from .prompt_loader import render_prompt
from .review_thread_validation import ClaimedReviewThread, render_claimed_review_threads_section
from .security_utils import redact_string
from .utils import CommandExecutor, bind_command_execution_cwd, reset_command_execution_cwd


@dataclass(frozen=True)
class VerificationCheckpoint:
    phase: str = ""
    response: str = ""
    published: bool = False
    publication_started: bool = False
    diagnostic_published: bool = False


def _verification_response(result: AdversarialValidationResult) -> str:
    return json.dumps({"summary": result.summary, "dispositions": [asdict(item) for item in result.thread_dispositions], "unverified": [asdict(item) for item in result.unverified_local_repairs]}, sort_keys=True)


class LocalRepairVerificationStore:
    """Reserve model entry before invocation and retain the exact report for replay."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS verifications ("
                "generation_id TEXT NOT NULL, head_sha TEXT NOT NULL, phase TEXT NOT NULL, response TEXT NOT NULL DEFAULT '', "
                "published INTEGER NOT NULL DEFAULT 0, publication_started INTEGER NOT NULL DEFAULT 0, "
                "diagnostic_published INTEGER NOT NULL DEFAULT 0, diagnostic_started INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY (generation_id, head_sha))"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def claim(self, generation_id: str, head_sha: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("INSERT OR IGNORE INTO verifications (generation_id, head_sha, phase) VALUES (?, ?, 'executing')", (generation_id, head_sha))
            return cursor.rowcount == 1

    def get(self, generation_id: str, head_sha: str) -> VerificationCheckpoint:
        with self._connect() as connection:
            row = connection.execute("SELECT phase, response, published, publication_started, diagnostic_published FROM verifications WHERE generation_id=? AND head_sha=?", (generation_id, head_sha)).fetchone()
        return VerificationCheckpoint(str(row[0]), str(row[1]), bool(row[2]), bool(row[3]), bool(row[4])) if row else VerificationCheckpoint()

    def complete(self, generation_id: str, head_sha: str, result: AdversarialValidationResult) -> None:
        payload = _verification_response(result)
        with self._connect() as connection:
            connection.execute("UPDATE verifications SET phase='completed', response=? WHERE generation_id=? AND head_sha=?", (payload, generation_id, head_sha))

    def reclaim_missing_roots(self, generation_id: str, head_sha: str, checkpoint: VerificationCheckpoint) -> bool:
        """Atomically recover a target-acquisition failure before reviewer entry."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE verifications SET phase='executing', response='', published=0, publication_started=0, diagnostic_published=0, diagnostic_started=0 " "WHERE generation_id=? AND head_sha=? AND phase='completed' AND response=?",
                (generation_id, head_sha, checkpoint.response),
            )
            return cursor.rowcount == 1

    def mark_published(self, generation_id: str, head_sha: str, expected_response: str, *, diagnostic: bool = False) -> bool:
        column = "diagnostic_published" if diagnostic else "published"
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE verifications SET {column}=1 WHERE generation_id=? AND head_sha=? AND response=?", (generation_id, head_sha, expected_response))
            return cursor.rowcount == 1

    def reserve_publication(self, generation_id: str, head_sha: str, expected_response: str, *, diagnostic: bool = False) -> bool:
        column = "diagnostic_started" if diagnostic else "publication_started"
        with self._connect() as connection:
            cursor = connection.execute(f"UPDATE verifications SET {column}=1 WHERE generation_id=? AND head_sha=? AND response=? AND {column}=0", (generation_id, head_sha, expected_response))
            return cursor.rowcount == 1


def _target_identity(repository: str, pr_number: int, target: ClaimedReviewThread) -> str:
    from .pr_processor import _review_feedback_identity
    from .util.gh_cache import ReviewThread, ReviewThreadComment

    return _review_feedback_identity(f"{repository}#{pr_number}:local:", ReviewThread(id=target.thread_id, comments=[ReviewThreadComment(database_id=target.root_comment_database_id)]), 0)


def run_pending_local_repair_verification(
    repository: str,
    pr_number: int,
    head_sha: str,
    *,
    select_threads: Callable[[], Sequence[ClaimedReviewThread]],
    linked_issue_contract: Callable[[], str],
    worktree: Callable[[], object],
    publish: Callable[[AdversarialValidationResult, bool], bool],
    head_is_current: Optional[Callable[[], bool]] = None,
    ledger: Optional[RepairAllowanceLedger] = None,
    repair_store: Optional[LocalReviewRepairStore] = None,
    verification_store: Optional[LocalRepairVerificationStore] = None,
    backend_manager: Optional[BackendManager] = None,
) -> AdversarialValidationResult:
    """Check only unsettled roots, never rerunning the same generation/head."""
    from contextlib import AbstractContextManager

    ledger = ledger or RepairAllowanceLedger()
    snapshot = ledger.get_snapshot("https://api.github.com", repository, pr_number)
    generation = snapshot.get_outstanding_generation()
    if generation is None or generation.owning_identity != "local-review-repair" or generation.lifecycle_state != GenerationLifecycleState.PENDING_REVALIDATION:
        raise RuntimeError("no completed local repair generation awaits verification")
    settled = {item.blocker_id for item in generation.settlements}
    pending = tuple(identity for identity in generation.covered_blocker_ids if identity not in settled)
    repair_store = repair_store or LocalReviewRepairStore(local_review_repair_db_path(repository))
    verification_store = verification_store or LocalRepairVerificationStore(local_review_repair_db_path(repository).with_name("local_review_verifications.sqlite3"))
    request = LocalReviewRepairRequest(repository, pr_number, repository, "", head_sha, (), "")
    record = repair_store.get(request, generation.bundle_reference)
    result = AdversarialValidationResult(result="INCONCLUSIVE", local_repair_generation_id=generation.generation_id, summary="Independent verification of pending local corrections only.")
    checkpoint = verification_store.get(generation.generation_id, head_sha)
    retry_claimed = False
    if checkpoint.phase == "completed":
        saved = json.loads(checkpoint.response)
        # This exact failure occurs before worktree/backend entry. A newly
        # acquired root can safely resume it; uncertain or completed model
        # invocations retain the original once-per-generation/head checkpoint.
        missing_roots = saved.get("unverified", [])
        if not saved.get("dispositions") and missing_roots and all(item.get("reason") == "pending correction roots could not be acquired" and not item.get("thread_id") for item in missing_roots):
            if select_threads():
                retry_claimed = verification_store.reclaim_missing_roots(generation.generation_id, head_sha, checkpoint)
                checkpoint = VerificationCheckpoint() if retry_claimed else verification_store.get(generation.generation_id, head_sha)
    if checkpoint.phase == "completed":
        payload = json.loads(checkpoint.response)
        result.summary = payload["summary"]
        result.thread_dispositions = _extract_thread_dispositions(payload["dispositions"])
        result.unverified_local_repairs = [UnverifiedLocalRepair(**item) for item in payload["unverified"]]
    elif checkpoint.phase or (not retry_claimed and not verification_store.claim(generation.generation_id, head_sha)):
        result.local_repair_verification_pending = True
        result.unverified_local_repairs = [UnverifiedLocalRepair(identity, reason="Prior verification invocation is still executing or indeterminate; it will not be repeated at the same commit.") for identity in pending]
        if not checkpoint.diagnostic_published:
            may_send = verification_store.reserve_publication(generation.generation_id, head_sha, "", diagnostic=True)
            if not publish(result, may_send):
                raise RuntimeError("pending verification diagnostic publication is unconfirmed")
            verification_store.mark_published(generation.generation_id, head_sha, "", diagnostic=True)
        return result
    else:
        targets: Sequence[ClaimedReviewThread] = ()
        try:
            if record is None or not record.result_sha:
                raise RuntimeError("completed correction has no retained result commit")
            if record.result_sha == record.head_sha and head_sha == record.head_sha:
                raise RuntimeError("local correction produced no new commit; repeating verification of the unchanged implementation is suppressed")
            targets = select_threads()
            if not targets:
                raise RuntimeError("pending correction roots could not be acquired")
            if backend_manager is None:
                from .cli_helpers import resolve_adversarial_validation_availability

                backend_manager = resolve_adversarial_validation_availability(validation_kind="pr").backend_manager
            if backend_manager is None:
                raise RuntimeError("no read-only verification backend is available")
            prompt = render_prompt(
                "pr.local_repair_verification",
                repo_name=repository,
                pr_number=pr_number,
                head_sha=head_sha,
                generation_id=generation.generation_id,
                baseline_sha=record.head_sha,
                pending_threads=render_claimed_review_threads_section(targets, pending_local_correction=True),
                linked_issue_contract=linked_issue_contract(),
            )
            context = worktree()
            if not isinstance(context, AbstractContextManager):
                raise RuntimeError("exact-head verification worktree is unavailable")
            with context as directory:
                ancestry = CommandExecutor.run_command(["git", "merge-base", "--is-ancestor", record.result_sha, head_sha], cwd=str(directory))
                if not ancestry.success:
                    raise RuntimeError("current head does not contain the completed local correction")
                for boundary in ("before", "after"):
                    observed = CommandExecutor.run_command(["git", "rev-parse", "HEAD"], cwd=str(directory))
                    if not observed.success or observed.stdout.strip() != head_sha:
                        raise RuntimeError(f"verification worktree head mismatch {boundary} reviewer invocation")
                    if boundary == "before":
                        token = bind_command_execution_cwd(str(directory))
                        try:
                            response = run_llm_prompt(prompt, backend_manager=backend_manager, is_noedit=True)
                        finally:
                            reset_command_execution_cwd(token)
                payload = json.loads(response)
                if not isinstance(payload, dict) or set(payload) != {"thread_dispositions"}:
                    raise RuntimeError("verification response must contain only requested thread dispositions")
                dispositions = _extract_thread_dispositions(payload["thread_dispositions"])
                allowed = {thread.thread_id for thread in targets}
                if any(item.thread_id not in allowed for item in dispositions):
                    raise RuntimeError("verification response included unrelated threads")
                result.thread_dispositions = dispositions
        except Exception as exc:
            reason = redact_string(str(exc))[:2000]
            known_threads = {_target_identity(repository, pr_number, target): target.thread_id for target in targets}
            result.unverified_local_repairs = [UnverifiedLocalRepair(identity, known_threads.get(identity, ""), reason) for identity in pending]
        if not result.unverified_local_repairs:
            disposition_by_thread = {item.thread_id: item for item in result.thread_dispositions}
            target_by_identity = {_target_identity(repository, pr_number, target): target for target in targets}
            for identity in pending:
                target = target_by_identity.get(identity)
                if target is None:
                    result.unverified_local_repairs.append(UnverifiedLocalRepair(identity, reason="Original correction root is missing, truncated, or unauthenticated; it was not verified."))
                    continue
                disposition = disposition_by_thread.get(target.thread_id)
                if disposition is None or disposition.status == "INCONCLUSIVE":
                    reason = f"{disposition.rationale}; {disposition.evidence}" if disposition else "Reviewer omitted the required disposition; this target was not verified."
                    result.unverified_local_repairs.append(UnverifiedLocalRepair(identity, target.thread_id, reason))
        result.summary = f"Verification limited to {len(pending)} pending local correction target(s): {len(result.thread_dispositions)} disposition(s) returned, {len(result.unverified_local_repairs)} target(s) NOT verified. No full PR validation was performed."
        verification_store.complete(generation.generation_id, head_sha, result)
    if not checkpoint.published:
        expected_response = _verification_response(result)
        may_send = verification_store.reserve_publication(generation.generation_id, head_sha, expected_response)
        if not publish(result, may_send):
            raise RuntimeError("pending local verification report publication is unconfirmed; no new reviewer invocation will be started")
        if not verification_store.mark_published(generation.generation_id, head_sha, expected_response):
            raise RuntimeError("verification report changed before publication confirmation")
    if head_is_current is not None and not head_is_current():
        raise RuntimeError("PR head changed before verification settlement; the pinned report is retained without settling pending corrections")
    observations = []
    # Recover identities from the same authoritative roots, preserving covered scope.
    if result.thread_dispositions:
        for target in select_threads():
            identity = _target_identity(repository, pr_number, target)
            disposition = next((item for item in result.thread_dispositions if item.thread_id == target.thread_id), None)
            if disposition and disposition.status in {"ADDRESSED", "STILL_VALID"}:
                observations.append(ValidationObservation(identity, disposition.status == "STILL_VALID", validation_seq=time.time_ns(), evidence=f"scoped independent verification at {head_sha}: {disposition.evidence}"))
    settle_local_review_repair_validation(repository, pr_number, head_sha, tuple(observations), ledger=ledger, store=repair_store, expected_generation_id=generation.generation_id)
    return result
