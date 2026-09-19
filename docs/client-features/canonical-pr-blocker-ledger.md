# Canonical PR Blocker Ledger

The canonical PR blocker ledger provides a durable, provider-independent
semantic representation and public transition boundary for pull request review
blockers (GitHub Issue #2135, Stage S1 of the convergent PR review tracking
family #2134). It decouples review blocker identity and correction scope from
ephemeral review wording, changing git commit heads, GitHub thread/comment IDs,
and reviewer backend/model/session lifecycles.

## Namespace and Controller-Owned Identity

Blockers are stored in a namespace uniquely identified by:
- Canonical GitHub API origin (e.g., `https://api.github.com` or Enterprise origin, normalized without credentials or redundant default ports)
- Repository slug (`owner/repo`)
- Pull request number (`int`)

Upon initial admission, the controller allocates an opaque, persistent blocker
identifier (e.g. `blk_<hex>`) once. This ID is preserved across:
- Changing PR head or base commit SHAs
- File and line anchor shifts across commits
- Reworded or paraphrased reviewer comments
- GitHub comment and review thread IDs
- Switching reviewer backends, models, or session registries
- Controller process restarts and PR close/reopen lifecycles

## Actionable Correction Representation and Scope Invariance

Each canonical blocker models exactly one independently actionable correction:
- **Category:** The review finding category (e.g., `SPECIFICATION`, `REGRESSION`, `IMPLEMENTATION`, `TEST_ORACLE`).
- **Qualified Requirement References:** Explicit Issue requirement references, each combining the target Issue number and a stable requirement ID (e.g., `Issue #2135`, `REQ-001`).
- **Authoritative Production Boundary:** The specific production boundary or component where the defect exists or must be corrected.
- **Incorrect Observable Behavior or Missing Invariant:** The concrete failure mode or violated invariant.
- **Required Correction Outcome:** The explicit required outcome needed to satisfy the requirement.
- **Evidence Needed:** The verification method or test oracle required to establish correction.
- **Original Objective Anchor:** The verbatim Objective anchor supplied by the contributing Issue when present; for legacy Issues lacking an Objective, no anchor is invented.
- **Accepted Original Correction Scope:** The original correction description and constituent concern IDs accepted at admission. This scope is immutable and preserved across later review iterations rather than replaced with newer prose summaries. Scope modifications require an explicitly accepted contract-rebinding record.

## Semantic Dispositions and Evidence Availability

Semantic disposition is strictly decoupled from verification evidence availability:
- **Dispositions:** `OPEN`, `VERIFIED_CORRECTION`, `AUTHORIZED_INVALIDATION`, and `RECURRENCE`.
- **Evidence Availability:** `KNOWN`, `UNAVAILABLE`, `OMITTED`, or `INCONCLUSIVE`.
- An open blocker remains `OPEN` when verification evidence is unavailable, omitted from a subsequent report, or inconclusive; missing or omitted evidence never silently closes or deletes a blocker.
- Demonstrated recurrence of an earlier defect reuses the blocker's original ID and appends to its transition history.
- Materially different independently actionable defects receive distinct blocker IDs even when citing the same requirement.

## Explicit Reconciliation and Provenance Aliases

Review observations are reconciled against existing blockers via an explicit reconciliation operation:
- The operation records the existing blocker IDs considered, the accepted association or distinct-defect decision, and associated evidence.
- Cross-PR and unknown blocker references are rejected.
- Inconsistent scope associations (such as conflicting categories or production boundaries) are rejected.
- Unresolved association ambiguities are exposed as explicit errors rather than arbitrarily picking a match or discarding an obligation.
- Authenticated GitHub root/thread IDs, model identities, and test-oracle gap IDs are retained as provenance-bearing aliases rather than semantic authorities. A single historical root comment containing multiple distinct defects may own multiple blocker references, and resolving one blocker does not imply resolution of the others or the thread.

## Concurrency, Idempotency, and Durability Guarantees

State transitions are durable and use optimistic locking:
- **Compare-and-Set (CAS):** Admissions and transitions require the caller's expected ledger revision. Stale writers are rejected with a revision error.
- **Operation Idempotency:** Mutations record their operation ID and a canonical hash of the payload in an operation journal. Replaying an identical operation ID with an identical payload returns the committed snapshot idempotently; conflicting reuse of an operation ID with an altered payload is rejected.
- **Fail-Closed Retained State:** An explicitly initialized empty namespace is distinguished from missing, unreadable, corrupted, or unsupported-version retained state. Any corrupted or unreadable state raises an unavailable error and fails closed, never returning a false empty/zero-success replacement.
- **Reviewer Session Decoupling:** Reading, updating, or clearing reviewer session registries (such as `ReviewerSessionRegistry.remove_pr`) does not mutate or delete the canonical blocker ledger.

## Local Storage and Recovery Limits

The canonical blocker ledger is backed by a local SQLite database in Write-Ahead Logging (WAL) mode (`PRAGMA journal_mode=WAL`) with immediate transactions (`BEGIN IMMEDIATE`) and a busy timeout. Multiple Auto-Coder worker processes accessing the same configured local database share the same ACID and CAS concurrency guarantees.

**Limits and Non-Goals:**
- This standalone ledger module defines only the durable semantic model and public transition boundary. It does not perform production LLM review matching, thread publication, independent closure assessment, or repair budget enforcement (which are owned by downstream stages S2 through S8 in Issue #2134). Repair budget/allowance enforcement itself is modeled in the [durable repair allowance state machine](durable-repair-allowance-state-machine.md) (Issue #2140), which consumes this ledger's canonical `blocker_id`s.
- Durability guarantees apply to the configured local store. Distributed multi-host storage replication and persistent reconstruction after catastrophic local disk loss without backups are outside the scope of this local ledger.
