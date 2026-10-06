"""Retain controller identities for review comments without managed finding markers."""

from __future__ import annotations

import hashlib

from .canonical_pr_blocker_ledger import (
    BlockerAdmissionPayload,
    BlockerAlias,
    CanonicalPRBlockerLedger,
    CorrectionScope,
    StaleLedgerRevisionError,
)
from .util.gh_cache import ReviewThread, ReviewThreadComment

_MANAGED_HEADERS = ("### Auto-Coder adversarial finding", "### Auto-Coder material test-oracle gap")


def retain_external_review_feedback(
    ledger: CanonicalPRBlockerLedger,
    repository: str,
    pr_number: int,
    thread: ReviewThread,
    comment: ReviewThreadComment,
    head_sha: str,
) -> tuple[str, ...]:
    """Allocate once by native comment identity; preserve original scope on replay.

    Tracking external prose does not authenticate it as an Auto-Coder finding,
    invent Issue requirements, or grant automatic thread-closure authority.
    """
    if comment.body.startswith(_MANAGED_HEADERS):
        return ()
    if thread.is_resolved or thread.comments_truncated or not thread.comments or not comment.body.strip():
        return ()
    index = thread.comments.index(comment)
    anchor = f"comment:{comment.database_id}" if comment.database_id is not None else f"thread:{thread.id}:comment:{index}"
    alias = BlockerAlias("external_review_comment", anchor)
    aliases = [alias]
    if index == 0:
        aliases.append(BlockerAlias("github_thread", thread.id))
        if comment.database_id is not None:
            aliases.append(BlockerAlias("github_root_comment", str(comment.database_id)))
    payload = BlockerAdmissionPayload(
        category="IMPLEMENTATION",
        authoritative_boundary=f"GitHub review thread {thread.id}",
        incorrect_behavior_or_missing_invariant=comment.body,
        required_correction_outcome=comment.body,
        accepted_scope=CorrectionScope(description=comment.body),
        aliases=tuple(aliases),
        evidence=comment.body,
        reviewed_head_sha=head_sha,
        observation_identity=anchor,
    )
    operation = "external-review:" + hashlib.sha256(f"{repository}#{pr_number}:{anchor}".encode()).hexdigest()
    for _attempt in range(5):
        snapshot = ledger.initialize_namespace("https://api.github.com", repository, pr_number)
        existing = snapshot.get_blockers_for_alias(alias.alias_type, alias.alias_value)
        if not existing and index == 0 and comment.database_id is not None:
            existing = snapshot.get_blockers_for_alias("github_root_comment", str(comment.database_id))
        if existing:
            return tuple(blocker.blocker_id for blocker in existing)
        try:
            _blocker_id, snapshot = ledger.admit_blocker("https://api.github.com", repository, pr_number, operation, snapshot.ledger_revision, payload)
            return tuple(blocker.blocker_id for blocker in snapshot.get_blockers_for_alias(alias.alias_type, alias.alias_value))
        except StaleLedgerRevisionError:
            continue
    raise RuntimeError("External review feedback admission could not acquire a current ledger revision")
