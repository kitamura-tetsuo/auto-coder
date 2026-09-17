"""Apply authorized review adjudications to a PR's repair/review lifecycle.

This module owns the *effects* of an already-authoritative adjudication
decision (see :mod:`review_adjudication` and :mod:`review_adjudication_github`
for the model and the GitHub reading/publishing boundary). It never reads
GitHub itself and never authors a decision; it only decides, from an already
computed :class:`~auto_coder.review_adjudication.AdjudicationResult`, what
durable internal projection or outbound action a caller must still perform,
and durably records that the decision's current generation has been
consumed so an unchanged decision set is never re-applied (REQ-002).

Two effect kinds exist:

- ``UPHOLD``/``FIX``: the target finding stays unresolved; the caller must
  route the decision's exact rationale/directive into the PR's normal bounded
  repair path (REQ-003). This module never claims delivery; it only plans it.
- ``OVERRULE``/``NO_CHANGE``: the target finding is retired as
  adjudicated-invalid. This module identifies the finding's owned
  projections -- currently its GitHub review thread and, when the root
  finding is a persisted material test-oracle gap (identified by the
  ``Gap identity`` marker :func:`auto_coder.adversarial_validator.format_test_oracle_gap_comment`
  embeds in the posted finding), that gap -- and instructs the caller to
  retire only the contributions this exact context owns, leaving any other
  live contribution to the same shared projection untouched (REQ-004,
  REQ-013).

A later supersession, revocation, or conflict that removes an already
applied OVERRULE's authority is reported back as a ``reopen`` action so the
caller can reconcile its owned projections instead of leaving a stale
merge exemption behind (REQ-007).
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from .review_adjudication import AdjudicationStatus
from .review_adjudication_github import AdjudicationSnapshot

ADJUDICATION_EFFECTS_DB_ENV = "AUTO_CODER_REVIEW_ADJUDICATION_EFFECTS_DB"
DEFAULT_ADJUDICATION_EFFECTS_DB_PATH = "~/.auto-coder/review-adjudication-effects.sqlite3"

_GAP_IDENTITY_PATTERN = re.compile(r"Gap identity: `([^`\n]+)`")

_TERMINAL_STATUSES = {"delivered", "retired", "reconciled", "none"}

# Distinguishes a thread Auto-Coder resolved because an authorized human
# adjudication overruled the finding from one resolved because an
# independent validator run confirmed the implementation agent's ADDRESSED
# claim (``auto-coder-review-thread-resolved:v1`` in review_thread_validation.py).
OVERRULE_RESOLUTION_MARKER = "<!-- auto-coder-review-adjudication-overruled:v1 -->"


def format_overrule_explanation(decision_id: str, rationale: str) -> str:
    """Render the auditable explanation posted before an overrule resolve."""
    return "\n".join(
        [
            "An authorized reviewer overruled this finding; it is retired as adjudicated-invalid, not independently verified as fixed.",
            "",
            f"Decision `{decision_id}`:",
            "",
            rationale,
            "",
            OVERRULE_RESOLUTION_MARKER,
        ]
    )


def extract_test_oracle_gap_id(raw_finding: str) -> Optional[str]:
    """Return the material test-oracle gap identity embedded in a finding.

    Only :func:`auto_coder.adversarial_validator.format_test_oracle_gap_comment`
    embeds this exact marker, so a match reliably identifies the persisted
    :class:`~auto_coder.reviewer_session_registry.TestOracleGap` this root
    finding is about.
    """
    match = _GAP_IDENTITY_PATTERN.search(raw_finding)
    return match.group(1) if match else None


@dataclass(frozen=True)
class EffectRecord:
    context_id: str
    repository: str
    pr_number: int
    decision_id: str
    head_sha: str
    contract_digest: str
    verdict: str
    status: str
    gap_id: str


class AdjudicationEffectStore:
    """Durable idempotent journal of applied adjudication effects.

    Keyed by ``context_id`` (one live context per repository/PR/root thread,
    see :class:`auto_coder.review_adjudication_github.AdjudicationContextStore`),
    recording the exact decision generation an effect was last computed for
    and whether that effect is still pending, confirmed, or needs
    reconciliation (REQ-002, REQ-008, REQ-014).
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS adjudication_effects (
                context_id TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                decision_id TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                contract_digest TEXT NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL,
                gap_id TEXT NOT NULL DEFAULT ''
            )"""
        )
        self._db.commit()

    def get(self, context_id: str) -> Optional[EffectRecord]:
        with self._lock:
            row = self._db.execute(
                "SELECT context_id, repository, pr_number, decision_id, head_sha, contract_digest, verdict, status, gap_id FROM adjudication_effects WHERE context_id=?",
                (context_id,),
            ).fetchone()
        return EffectRecord(*row) if row else None

    def begin(self, context_id: str, repository: str, pr_number: int, decision_id: str, head_sha: str, contract_digest: str, verdict: str, gap_id: str = "") -> None:
        """Durably record a generation as ``pending`` before any outbound effect.

        Must be called, and must succeed, before any GitHub or provider call
        for that generation is attempted (REQ-008 fail-closed persistence).
        """
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO adjudication_effects(context_id, repository, pr_number, decision_id, head_sha, contract_digest, verdict, status, gap_id) VALUES(?,?,?,?,?,?,?, 'pending', ?)"
                " ON CONFLICT(context_id) DO UPDATE SET repository=excluded.repository, pr_number=excluded.pr_number, decision_id=excluded.decision_id, head_sha=excluded.head_sha,"
                " contract_digest=excluded.contract_digest, verdict=excluded.verdict, status='pending', gap_id=excluded.gap_id",
                (context_id, repository, pr_number, decision_id, head_sha, contract_digest, verdict, gap_id),
            )

    def finish(self, context_id: str, status: str, gap_id: Optional[str] = None) -> None:
        with self._lock, self._db:
            if gap_id is None:
                self._db.execute("UPDATE adjudication_effects SET status=? WHERE context_id=?", (status, context_id))
            else:
                self._db.execute("UPDATE adjudication_effects SET status=?, gap_id=? WHERE context_id=?", (status, gap_id, context_id))

    def rows_for_pr(self, repository: str, pr_number: int) -> tuple[EffectRecord, ...]:
        with self._lock:
            rows = self._db.execute(
                "SELECT context_id, repository, pr_number, decision_id, head_sha, contract_digest, verdict, status, gap_id FROM adjudication_effects WHERE repository=? AND pr_number=?",
                (repository, pr_number),
            ).fetchall()
        return tuple(EffectRecord(*row) for row in rows)

    def force_revalidation_needed(self, repository: str, pr_number: int) -> bool:
        """Whether any tracked context still needs its effect confirmed.

        Used to bypass ordinary same-head validation suppression only while
        real adjudication-driven work is outstanding (REQ-002); an
        unchanged, fully-applied decision set never forces revalidation.
        """
        return any(row.status not in _TERMINAL_STATUSES for row in self.rows_for_pr(repository, pr_number))


@dataclass(frozen=True)
class UpholdEffect:
    context_id: str
    thread_id: str
    root_comment_id: int
    decision_id: str
    head_sha: str
    contract_digest: str
    rationale: str
    raw_finding: str


@dataclass(frozen=True)
class OverruleEffect:
    context_id: str
    thread_id: str
    root_comment_id: int
    decision_id: str
    head_sha: str
    contract_digest: str
    rationale: str
    gap_id: Optional[str]
    gap_still_contributed_elsewhere: bool


@dataclass(frozen=True)
class ReopenEffect:
    """A previously applied OVERRULE lost its authority and must be reversed."""

    context_id: str
    thread_id: str
    gap_id: str
    decision_id: str
    head_sha: str
    contract_digest: str
    verdict: str


@dataclass(frozen=True)
class AdjudicationEffectPlan:
    upholds: tuple[UpholdEffect, ...] = ()
    overrules: tuple[OverruleEffect, ...] = ()
    reopens: tuple[ReopenEffect, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.upholds or self.overrules or self.reopens)


def plan_adjudication_effects(snapshots: Sequence[AdjudicationSnapshot], effect_store: AdjudicationEffectStore) -> AdjudicationEffectPlan:
    """Compute what effects, if any, must still be applied for this PR.

    Read-only with respect to ``effect_store``: callers must call
    :meth:`AdjudicationEffectStore.begin` for the exact generation planned
    here before performing any outbound effect, and
    :meth:`AdjudicationEffectStore.finish` after.
    """
    # A gap_id maps to every context that still contributes to it (i.e. has
    # not been overruled). Retiring one overruled context's contribution
    # must never clear a gap that another live context still contributes to
    # (REQ-004, REQ-013, AS-007).
    live_gap_contexts: dict[str, list[str]] = {}
    for snapshot in snapshots:
        context = snapshot.context
        if context is None or context.retired_reason is not None:
            continue
        is_applied_overrule = snapshot.result.status == AdjudicationStatus.APPLICABLE and snapshot.result.verdict == "OVERRULE"
        if is_applied_overrule:
            continue
        gap_id = extract_test_oracle_gap_id(snapshot.raw_finding)
        if gap_id:
            live_gap_contexts.setdefault(gap_id, []).append(context.context_id)

    upholds: list[UpholdEffect] = []
    overrules: list[OverruleEffect] = []
    reopens: list[ReopenEffect] = []

    for snapshot in snapshots:
        context = snapshot.context
        if context is None:
            continue
        result = snapshot.result
        prior = effect_store.get(context.context_id)
        applicable = result.status == AdjudicationStatus.APPLICABLE and result.verdict in {"UPHOLD", "OVERRULE"}
        decision_record = context.decisions.get(result.decision_id) if result.decision_id else None
        rationale = decision_record.decision.rationale if decision_record is not None else ""
        generation = (result.decision_id or "", context.head_sha, context.contract_digest, result.verdict or "")
        prior_generation = (prior.decision_id, prior.head_sha, prior.contract_digest, prior.verdict) if prior else None
        is_new = prior_generation != generation

        # REQ-007: a previously applied OVERRULE that lost its authority
        # (superseded by a different disposition, revoked, or now
        # conflicted/undecided) must be reversed regardless of what the new
        # current disposition is -- including a fresh applicable UPHOLD,
        # which is planned independently below.
        was_applied_overrule = prior is not None and prior.verdict == "OVERRULE" and prior.status in {"retired", "reconciliation-required"}
        still_applied_overrule = applicable and result.verdict == "OVERRULE"
        if prior is not None and was_applied_overrule and is_new and not still_applied_overrule:
            reopens.append(
                ReopenEffect(
                    context_id=context.context_id,
                    thread_id=context.thread_id,
                    gap_id=prior.gap_id,
                    decision_id=result.decision_id or "",
                    head_sha=context.head_sha,
                    contract_digest=context.contract_digest,
                    verdict=result.verdict or "",
                )
            )

        if applicable and result.verdict == "UPHOLD":
            if is_new or prior is None or prior.status != "delivered":
                upholds.append(
                    UpholdEffect(
                        context_id=context.context_id,
                        thread_id=context.thread_id,
                        root_comment_id=context.root_comment_id,
                        decision_id=result.decision_id or "",
                        head_sha=context.head_sha,
                        contract_digest=context.contract_digest,
                        rationale=rationale,
                        raw_finding=snapshot.raw_finding,
                    )
                )
        elif still_applied_overrule:
            if is_new or prior is None or prior.status != "retired":
                gap_id = extract_test_oracle_gap_id(snapshot.raw_finding)
                shared = bool(gap_id and live_gap_contexts.get(gap_id))
                overrules.append(
                    OverruleEffect(
                        context_id=context.context_id,
                        thread_id=context.thread_id,
                        root_comment_id=context.root_comment_id,
                        decision_id=result.decision_id or "",
                        head_sha=context.head_sha,
                        contract_digest=context.contract_digest,
                        rationale=rationale,
                        gap_id=gap_id,
                        gap_still_contributed_elsewhere=shared,
                    )
                )

    return AdjudicationEffectPlan(tuple(upholds), tuple(overrules), tuple(reopens))
