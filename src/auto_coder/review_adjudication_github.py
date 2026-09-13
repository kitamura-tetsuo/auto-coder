"""Authoritative GitHub boundary for review adjudication contexts.

GitHub remains the source of decisions.  This module only stores the immutable
evidence needed to safely resume reconciliation; webhook bodies are never read
as evidence.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import httpx

from .github_request_governor import GitHubRequestDeferred
from .requirement_contract import REQUIREMENT_CONTRACT_PARSER_VERSION, build_normative_issue_manifest
from .review_adjudication import (
    AdjudicationLedger,
    AdjudicationResult,
    AdjudicationStatus,
    Decision,
    IssueContract,
    Requirement,
    ReviewContext,
    SourceComment,
    contract_identity,
    render_decision,
)
from .util.gh_cache import ReviewThread

CONTEXT_MARKER = "<!-- auto-coder-review-adjudication-context:v1 -->"


@dataclass(frozen=True)
class PullRequestBinding:
    repository_id: int
    repository: str
    pr_number: int
    head_sha: str
    base_sha: str
    base_ref: str


@dataclass(frozen=True)
class IssueEvidence:
    issue_id: int
    issue_number: int
    title: str
    body: str


@dataclass(frozen=True)
class AdjudicationSnapshot:
    context: Optional[ReviewContext]
    raw_finding: str
    contributing_issues: tuple[int, ...]
    root_author_id: Optional[int]
    source_comment_id: Optional[int]
    result: AdjudicationResult
    observation_revision: str


def _objective_text(body: str) -> str:
    """Extract the exact Objective payload, without normalizing its bytes."""
    lines = body.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == "## Objective"]
    if len(starts) != 1:
        raise ValueError("contributing Issue must contain exactly one Objective")
    start = starts[0] + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    value = "".join(lines[start:end])
    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n\r\n"):
        value = value[:-2]
    elif value.endswith("\n\n"):
        value = value[:-1]
    if not value.strip():
        raise ValueError("contributing Issue Objective is empty")
    return value


def build_issue_contracts(issues: Sequence[IssueEvidence]) -> tuple[IssueContract, ...]:
    """Build the complete explicit manifest supplied by normal PR validation."""
    if not issues:
        raise ValueError("complete contributing Issue evidence is required")
    contracts = []
    for issue in issues:
        if issue.issue_id <= 0 or issue.issue_number <= 0:
            raise ValueError("contributing Issues require stable positive identities")
        manifest = build_normative_issue_manifest(issue.issue_number, issue.title, issue.body)
        if not manifest.explicit_contract_present or not manifest.explicit_contract_valid:
            raise ValueError(manifest.error or manifest.invalid_reason or "contributing Issue contract is unavailable")
        contracts.append(
            IssueContract(
                issue_id=issue.issue_id,
                issue_number=issue.issue_number,
                requirements=tuple(Requirement(item.requirement_id, item.text) for item in manifest.requirements),
                objective_text=_objective_text(issue.body),
            )
        )
    # contract_identity also rejects duplicate stable IDs and empty manifests.
    contract_identity(contracts, REQUIREMENT_CONTRACT_PARSER_VERSION)
    return tuple(contracts)


class AdjudicationContextStore:
    """Atomic repository-scoped context ledger with one live root binding."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS adjudication_contexts (
                repository TEXT NOT NULL, pr_number INTEGER NOT NULL,
                root_comment_id INTEGER NOT NULL, context_id TEXT NOT NULL UNIQUE,
                live INTEGER NOT NULL, ledger TEXT NOT NULL,
                publication_state TEXT NOT NULL DEFAULT 'pending',
                publication_body TEXT NOT NULL DEFAULT '',
                observation_revision TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(repository, pr_number, root_comment_id, context_id)
            )"""
        )
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_live_adjudication_context ON adjudication_contexts(repository, pr_number, root_comment_id) WHERE live = 1")
        self._db.execute("CREATE TABLE IF NOT EXISTS adjudication_issue_prs (repository TEXT NOT NULL, issue_number INTEGER NOT NULL, pr_number INTEGER NOT NULL, PRIMARY KEY(repository,issue_number,pr_number))")
        self._db.commit()

    def register(self, context: ReviewContext, observation_revision: str) -> AdjudicationLedger:
        ledger = AdjudicationLedger(context)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT ledger FROM adjudication_contexts WHERE repository=? AND pr_number=? AND root_comment_id=? AND live=1",
                    (context.repository, context.pr_number, context.root_comment_id),
                ).fetchone()
                if row:
                    current = AdjudicationLedger.loads(row[0])
                    if _binding(current.context) == _binding(context):
                        self._db.commit()
                        return current
                    current.retire("a bound context revision changed")
                    self._db.execute(
                        "UPDATE adjudication_contexts SET live=0, ledger=? WHERE context_id=?",
                        (current.dumps(), current.context.context_id),
                    )
                self._db.execute(
                    "INSERT INTO adjudication_contexts(repository,pr_number,root_comment_id,context_id,live,ledger,observation_revision) VALUES(?,?,?,?,1,?,?)",
                    (context.repository, context.pr_number, context.root_comment_id, context.context_id, ledger.dumps(), observation_revision),
                )
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise
        return ledger

    def save(self, ledger: AdjudicationLedger, observation_revision: str) -> None:
        with self._lock, self._db:
            changed = self._db.execute("UPDATE adjudication_contexts SET live=?, ledger=?, observation_revision=? WHERE context_id=?", (0 if ledger.context.retired_reason else 1, ledger.dumps(), observation_revision, ledger.context.context_id)).rowcount
            if changed != 1:
                raise RuntimeError("adjudication context was not durably registered")

    def publication(self, context_id: str) -> tuple[str, str]:
        row = self._db.execute("SELECT publication_state,publication_body FROM adjudication_contexts WHERE context_id=?", (context_id,)).fetchone()
        if not row:
            raise RuntimeError("adjudication context was not durably registered")
        return str(row[0]), str(row[1])

    def set_publication(self, context_id: str, state: str, body: str) -> None:
        if state not in {"pending", "confirmed", "unknown", "definitely-not-sent"}:
            raise ValueError("invalid publication state")
        with self._lock, self._db:
            if self._db.execute("UPDATE adjudication_contexts SET publication_state=?,publication_body=? WHERE context_id=?", (state, body, context_id)).rowcount != 1:
                raise RuntimeError("adjudication context was not durably registered")

    def associate_issues(self, repository: str, pr_number: int, issue_numbers: Sequence[int]) -> None:
        """Atomically replace the reverse association from contracts to a PR."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM adjudication_issue_prs WHERE repository=? AND pr_number=?", (repository, pr_number))
            self._db.executemany(
                "INSERT INTO adjudication_issue_prs(repository,issue_number,pr_number) VALUES(?,?,?)",
                ((repository, number, pr_number) for number in issue_numbers),
            )

    def affected_prs(self, repository: str, issue_number: int) -> tuple[int, ...]:
        rows = self._db.execute(
            "SELECT pr_number FROM adjudication_issue_prs WHERE repository=? AND issue_number=? ORDER BY pr_number",
            (repository, issue_number),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def tracked_prs(self, repository: str) -> tuple[int, ...]:
        rows = self._db.execute(
            "SELECT DISTINCT pr_number FROM adjudication_contexts WHERE repository=? AND (live=1 OR publication_state!='confirmed') ORDER BY pr_number",
            (repository,),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def ledgers_for_pr(self, repository: str, pr_number: int) -> tuple[AdjudicationLedger, ...]:
        rows = self._db.execute(
            "SELECT ledger FROM adjudication_contexts WHERE repository=? AND pr_number=? AND live=1 ORDER BY root_comment_id",
            (repository, pr_number),
        ).fetchall()
        return tuple(AdjudicationLedger.loads(str(row[0])) for row in rows)


def _binding(context: ReviewContext) -> tuple[object, ...]:
    return (
        context.repository_id,
        context.repository,
        context.pr_number,
        context.thread_id,
        context.root_comment_id,
        context.root_author_id,
        context.root_body_hash,
        context.root_update_revision,
        context.head_sha,
        context.base_sha,
        context.base_ref,
        context.contract_digest,
        context.objective_fingerprints,
    )


def new_context(binding: PullRequestBinding, thread: ReviewThread, contracts: Sequence[IssueContract]) -> ReviewContext:
    if not thread.comments or thread.comments_truncated:
        raise ValueError("complete review-thread evidence is required")
    root = thread.comments[0]
    if root.database_id is None or root.author_id is None or root.author_type != "Bot" or not root.updated_at:
        raise ValueError("review root lacks authoritative identity or revision metadata")
    _, digest, objectives = contract_identity(contracts, REQUIREMENT_CONTRACT_PARSER_VERSION)
    return ReviewContext(
        context_id=str(uuid.uuid4()),
        repository_id=binding.repository_id,
        repository=binding.repository,
        pr_number=binding.pr_number,
        thread_id=thread.id,
        root_comment_id=root.database_id,
        root_author_id=root.author_id,
        root_actor_type=root.author_type,
        root_body_hash=hashlib.sha256(root.body.encode("utf-8")).hexdigest(),
        root_update_revision=root.updated_at,
        head_sha=binding.head_sha,
        base_sha=binding.base_sha,
        base_ref=binding.base_ref,
        parser_version=REQUIREMENT_CONTRACT_PARSER_VERSION,
        contracts=tuple(contracts),
        contract_digest=digest,
        objective_fingerprints=objectives,
    )


def reconcile_thread(ledger: AdjudicationLedger, thread: ReviewThread, adjudicator_ids: Sequence[int], root_reviewer_ids: Sequence[int]) -> AdjudicationResult:
    """Ingest one complete authoritative thread in physical source order."""
    if thread.comments_truncated or not thread.comments:
        return ledger.reconcile(
            available=False,
            root_body_hash="",
            root_update_revision="",
            root_author_id=0,
            root_actor_type="",
            head_sha=ledger.context.head_sha,
            base_sha=ledger.context.base_sha,
            base_ref=ledger.context.base_ref,
            contract_digest=ledger.context.contract_digest,
            objective_fingerprints=ledger.context.objective_fingerprints,
            root_reviewer_ids=root_reviewer_ids,
            adjudicator_ids=adjudicator_ids,
        )
    root = thread.comments[0]
    result = ledger.reconcile(
        available=True,
        root_body_hash=hashlib.sha256(root.body.encode()).hexdigest(),
        root_update_revision=root.updated_at,
        root_author_id=root.author_id or 0,
        root_actor_type=root.author_type,
        head_sha=ledger.context.head_sha,
        base_sha=ledger.context.base_sha,
        base_ref=ledger.context.base_ref,
        contract_digest=ledger.context.contract_digest,
        objective_fingerprints=ledger.context.objective_fingerprints,
        root_reviewer_ids=root_reviewer_ids,
        adjudicator_ids=adjudicator_ids,
    )
    if result.status in {AdjudicationStatus.STALE, AdjudicationStatus.REVOKED, AdjudicationStatus.INVALID}:
        return result
    observed_ids = {item.database_id for item in thread.comments}
    for record in (*ledger.context.decisions.values(), *ledger.context.pending_decisions.values()):
        if record.source.comment_id not in observed_ids:
            ledger.observe_deletion(record.source.comment_id)
            return ledger.current(None, None, "accepted adjudication source was confirmed absent")
    for comment in sorted(thread.comments[1:], key=lambda item: (item.created_at, item.database_id or 0)):
        if comment.database_id is None or comment.author_id is None:
            ledger.context.source_unavailable = True
            return ledger.current(None, None, "review comment identity is unavailable")
        source = SourceComment(
            ledger.context.repository_id, ledger.context.repository, ledger.context.pr_number, thread.id, ledger.context.root_comment_id, comment.database_id, comment.author_id, comment.created_at, comment.updated_at, comment.body, comment.in_reply_to_id == ledger.context.root_comment_id
        )
        ledger.ingest(source, adjudicator_ids, root_reviewer_ids)
        if ledger.context.retired_reason:
            return ledger.current(source, None, "authoritative adjudication history was invalidated")
    return ledger.current(None, None, "authoritative thread reconciled")


def render_context_projection(context: ReviewContext, tips: Sequence[str]) -> str:
    """Render copy-ready evidence and a valid decision template."""
    publication_id = str(uuid.uuid5(uuid.UUID(context.context_id), "review-adjudication-context-v1:" + ",".join(tips)))
    example = Decision(publication_id, context.context_id, context.head_sha, context.contract_digest, "UNDECIDED", "NONE", tuple(tips), "Replace with the adjudicator rationale.", "chatgpt-assisted")
    issues = ", ".join(f"#{item.issue_number}" for item in context.contracts)
    return (
        f"{CONTEXT_MARKER}\nContext ID: `{context.context_id}`\n"
        f"Target: `{context.repository}` PR #{context.pr_number}, thread `{context.thread_id}`, root `{context.root_comment_id}`\n"
        f"Head: `{context.head_sha}`\nBase: `{context.base_ref}` at `{context.base_sha}`\n"
        f"Contract digest: `{context.contract_digest}`\nContributing Issues: {issues}\n"
        f"Current predecessor tips: {', '.join(tips) or '(none)'}\n"
        "Allowed pairs: `UPHOLD/FIX`, `OVERRULE/NO_CHANGE`, `UNDECIDED/NONE`\n\n"
        f"Copy, edit, and post this entire envelope as a direct reply:\n\n{render_decision(example)}"
    )


def publish_context(github_client: object, store: AdjudicationContextStore, ledger: AdjudicationLedger, thread: ReviewThread) -> str:
    """Publish once, verifying an ambiguous prior send before any retry."""
    body = render_context_projection(ledger.context, ledger.tips())
    state, saved_body = store.publication(ledger.context.context_id)
    if state == "confirmed" and saved_body == body:
        return state
    if state in {"pending", "unknown"} and saved_body:
        if any(item.body == saved_body for item in thread.comments):
            store.set_publication(ledger.context.context_id, "confirmed", saved_body)
            return "confirmed"
        # The caller supplies a complete authoritative thread. Its confirmed
        # absence proves that the prior body was not accepted remotely.
        store.set_publication(ledger.context.context_id, "definitely-not-sent", saved_body)
    store.set_publication(ledger.context.context_id, "pending", body)
    try:
        github_client.reply_to_review_thread(ledger.context.repository, ledger.context.pr_number, ledger.context.root_comment_id, body)  # type: ignore[attr-defined]
    except GitHubRequestDeferred:
        store.set_publication(ledger.context.context_id, "pending", body)
        return "pending"
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500 and exc.response.status_code not in {408, 429}:
            store.set_publication(ledger.context.context_id, "definitely-not-sent", body)
            return "definitely-not-sent"
        store.set_publication(ledger.context.context_id, "unknown", body)
        return "unknown"
    except Exception:
        store.set_publication(ledger.context.context_id, "unknown", body)
        return "unknown"
    store.set_publication(ledger.context.context_id, "confirmed", body)
    return "confirmed"


class ReviewAdjudicationService:
    """Production refresh/snapshot boundary used by every PR processing origin."""

    def __init__(self, github_client: object, store: AdjudicationContextStore) -> None:
        self.github = github_client
        self.store = store
        self._snapshots: dict[tuple[str, int, int], AdjudicationSnapshot] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _binding(repository: str, pr_number: int, pr_data: dict[str, object]) -> PullRequestBinding:
        head = pr_data.get("head")
        base = pr_data.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError("authoritative PR metadata lacks head/base bindings")
        base_repo = base.get("repo")
        repository_id = base_repo.get("id") if isinstance(base_repo, dict) else None
        head_sha, base_sha, base_ref = head.get("sha"), base.get("sha"), base.get("ref")
        if not isinstance(repository_id, int) or isinstance(repository_id, bool) or repository_id <= 0 or not all(isinstance(value, str) and value for value in (head_sha, base_sha, base_ref)):
            raise ValueError("authoritative PR metadata lacks stable repository or revision identity")
        return PullRequestBinding(repository_id, repository, pr_number, str(head_sha), str(base_sha), str(base_ref))

    def refresh(
        self,
        repository: str,
        pr_number: int,
        pr_data: dict[str, object],
        issue_numbers: Sequence[int],
        root_reviewer_ids: Sequence[int],
        adjudicator_ids: Sequence[int],
    ) -> tuple[AdjudicationSnapshot, ...]:
        """Strictly re-read all evidence, persist it, then expose applicability."""
        binding = self._binding(repository, pr_number, pr_data)
        invalidate_issue_reads = getattr(self.github, "invalidate_issue_reads_for_adjudication", None)
        if callable(invalidate_issue_reads):
            invalidate_issue_reads(repository)
        issue_evidence = []
        for number in issue_numbers:
            issue = self.github.get_issue_dispatch_snapshot_strict(repository, number)  # type: ignore[attr-defined]
            if not isinstance(issue, dict) or issue.get("number") != number or "pull_request" in issue:
                raise ValueError(f"contributing Issue #{number} is unavailable or ambiguous")
            issue_id = issue.get("id")
            title, body = issue.get("title"), issue.get("body")
            if not isinstance(issue_id, int) or isinstance(issue_id, bool) or issue_id <= 0 or not isinstance(title, str) or not isinstance(body, str):
                raise ValueError(f"contributing Issue #{number} lacks complete authoritative identity/body")
            issue_evidence.append(IssueEvidence(issue_id, number, title, body))
        contracts = build_issue_contracts(issue_evidence)
        _, current_contract_digest, current_objectives = contract_identity(contracts, REQUIREMENT_CONTRACT_PARSER_VERSION)
        self.store.associate_issues(repository, pr_number, issue_numbers)
        threads = self.github.get_pr_review_threads_strict(repository, pr_number)  # type: ignore[attr-defined]
        threads_by_root = {thread.comments[0].database_id: thread for thread in threads if thread.comments}
        snapshots = []
        existing_ids = set()
        for ledger in self.store.ledgers_for_pr(repository, pr_number):
            existing_ids.add(ledger.context.root_comment_id)
            thread = threads_by_root.get(ledger.context.root_comment_id)
            if thread is None:
                ledger.retire("registered review root was confirmed absent")
                self.store.save(ledger, "complete-root-inventory")
                continue
            root = thread.comments[0]
            result = ledger.reconcile(
                available=True,
                root_body_hash=hashlib.sha256(root.body.encode("utf-8")).hexdigest(),
                root_update_revision=root.updated_at,
                root_author_id=root.author_id or 0,
                root_actor_type=root.author_type,
                head_sha=binding.head_sha,
                base_sha=binding.base_sha,
                base_ref=binding.base_ref,
                contract_digest=current_contract_digest,
                objective_fingerprints=current_objectives,
                root_reviewer_ids=root_reviewer_ids,
                adjudicator_ids=adjudicator_ids,
            )
            if result.status not in {AdjudicationStatus.STALE, AdjudicationStatus.REVOKED, AdjudicationStatus.INVALID}:
                result = reconcile_thread(ledger, thread, adjudicator_ids, root_reviewer_ids)
            observation = hashlib.sha256("\0".join(f"{item.database_id}:{item.updated_at}" for item in thread.comments).encode("utf-8")).hexdigest()
            self.store.save(ledger, observation)
            snapshot = AdjudicationSnapshot(ledger.context, root.body, tuple(issue_numbers), root.author_id, result.source_comment_id, result, observation)
            snapshots.append(snapshot)
            with self._lock:
                self._snapshots[(repository, pr_number, ledger.context.root_comment_id)] = snapshot
        for thread in threads:
            if thread.comments_truncated or not thread.comments:
                raise RuntimeError(f"review thread {thread.id} was not read completely")
            root = thread.comments[0]
            if root.database_id in existing_ids:
                continue
            if root.author_type != "Bot" or root.author_id not in root_reviewer_ids:
                continue
            candidate = new_context(binding, thread, contracts)
            observation = hashlib.sha256("\0".join(f"{item.database_id}:{item.updated_at}" for item in thread.comments).encode("utf-8")).hexdigest()
            ledger = self.store.register(candidate, observation)
            result = reconcile_thread(ledger, thread, adjudicator_ids, root_reviewer_ids)
            self.store.save(ledger, observation)
            if ledger.context.retired_reason is None:
                publish_context(self.github, self.store, ledger, thread)
            snapshot = AdjudicationSnapshot(
                context=ledger.context,
                raw_finding=root.body,
                contributing_issues=tuple(issue_numbers),
                root_author_id=root.author_id,
                source_comment_id=result.source_comment_id,
                result=result,
                observation_revision=observation,
            )
            snapshots.append(snapshot)
            with self._lock:
                self._snapshots[(repository, pr_number, ledger.context.root_comment_id)] = snapshot
        return tuple(snapshots)

    def mark_unavailable(self, repository: str, pr_number: int, reason: str) -> None:
        """Suspend every retained positive snapshot before a new observation."""
        for ledger in self.store.ledgers_for_pr(repository, pr_number):
            ledger.context.source_unavailable = True
            result = ledger.current(None, None, reason)
            self.store.save(ledger, reason)
            key = (repository, pr_number, ledger.context.root_comment_id)
            with self._lock:
                previous = self._snapshots.get(key)
                self._snapshots[key] = AdjudicationSnapshot(
                    ledger.context,
                    previous.raw_finding if previous else "",
                    previous.contributing_issues if previous else tuple(item.issue_number for item in ledger.context.contracts),
                    ledger.context.root_author_id,
                    None,
                    result,
                    reason,
                )

    def snapshots(self, repository: str, pr_number: int) -> tuple[AdjudicationSnapshot, ...]:
        """Return the last fully persisted read-only observations for consumers."""
        with self._lock:
            return tuple(value for (repo, pr, _), value in self._snapshots.items() if repo == repository and pr == pr_number)
