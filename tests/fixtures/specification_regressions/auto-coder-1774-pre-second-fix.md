Parent-Issue: #1730

## Context

Specification validation currently occurs too late in the implementation path. An `implementation-ready` submission should be validated soon after its submitted specification becomes stable, independently of implementation-slot availability or candidate-selection timing.

Parent/child normalization is a prerequisite for correct eager validation. Coding agents cannot always create GitHub native parent/sub-issue relationships, so Auto-Coder also recognizes supported `Parent-Issue` metadata and materializes the corresponding native relationship. If validation snapshots an Issue before that reconciliation occurs, a child can be misclassified as standalone or a parent decomposition can omit a real child and receive a false `READY` result.

This Issue owns the pre-validation pipeline: initial stabilization of newly created Issues, readiness-target classification, `Parent-Issue` reconciliation, authoritative relationship re-fetch, generation snapshotting, and eager scheduling eligibility for the required individual/decomposition validation identities. It does not own semantic validation criteria or bounded-concurrency policy.

## Requirements

REQ-001: After authoritative parent/direct-child reconciliation, Auto-Coder must classify readiness submissions as follows: an Issue with no native parent and no direct children is a submitted standalone Issue only when it carries `implementation-ready` and then requires individual validation; an Issue with one or more authoritative direct children is a submitted parent set only when that parent carries `implementation-ready` and then requires one decomposition validation for the complete direct-child set plus individual validation for every authoritative direct child regardless of each child's own readiness label or open/closed/implementation state; a leaf Issue with a native parent is not a standalone submission and its own `implementation-ready` label must not substitute for readiness on its authoritative parent, although it participates in individual validation when that parent is a submitted parent set.
REQ-002: A newly created readiness submission must not be specification-validated from an intermediate creation state during the first one-minute stabilization window after creation; mutations during that window may change title, body, labels, or parent/direct-child relationships, and validation after the window must use the latest authoritative state rather than validating each intermediate generation.
REQ-003: After the initial one-minute stabilization window has elapsed, a later explicit addition of `implementation-ready`, any later change to an authoritative Issue title or body value, or a later parent/direct-child membership change while the relevant readiness submission remains live must make the resulting current required validation identity or identities eligible without imposing another fixed one-minute creation delay.
REQ-004: A `Parent-Issue` declaration candidate is a body line whose trimmed text uses the case-insensitive key `parent-issue`, `parent_issue`, or `parent issue` followed by `:`; a supported declaration has, after that colon, exactly an optional `#` followed by one positive decimal Issue number with only surrounding whitespace, all declaration candidates must be examined, repeated supported declarations naming the same number are equivalent, and two or more distinct declared numbers are ambiguous.
REQ-005: When an Issue has no native parent and has exactly one unambiguous supported `Parent-Issue` declaration, Auto-Coder must resolve that number authoritatively in the same repository as the child and, if it identifies a different GitHub Issue that can validly be the native parent, materialize that parent/sub-issue relationship before readiness classification or validation generation snapshotting.
REQ-006: `Parent-Issue` reconciliation must complete and authoritative parent/direct-child state must then be re-fetched before any standalone, child-individual, or parent-decomposition validation identity is snapshotted or scheduled; semantic validation and implementation eligibility must consume the re-fetched native graph rather than treating the body marker as a second independent parenthood oracle.
REQ-007: If a supported `Parent-Issue` declaration names #A while GitHub already records a different native parent #C, Auto-Coder must not silently choose either declaration, must not reparent the Issue, and must not validate or implement from that contradictory state; the conflict must remain observably blocked until the declaration or native relationship is corrected.
REQ-008: A malformed `Parent-Issue` declaration candidate, a self-parent declaration, multiple distinct declared parents, a declaration whose target is authoritatively absent or is not a GitHub Issue in the same repository, or a relationship that GitHub authoritatively rejects as structurally invalid such as a cycle or unsupported hierarchy must not create or guess a relationship and must prevent validation and implementation until corrected; parent open/closed state alone must not make an otherwise valid target invalid.
REQ-009: Network failures, timeouts, rate limiting, authentication/authorization failures, service failures, or another response that does not establish whether the declaration or relationship is structurally valid must be treated as operational reconciliation failure rather than specification defect; such failure must not remove `implementation-ready`, publish semantic defect findings, or authorize validation/implementation from the unresolved graph, and the current state must remain eligible for retry.
REQ-010: The authoritative parent decomposition generation must include the parent's current specification generation and the complete native direct-child membership regardless of child open/closed, implementation, PR, readiness-label, or ownership state; therefore any parent title/body change or direct-child addition/removal/reparenting must create a new decomposition validation identity.
REQ-011: Before a completed validation mutates readiness state, publishes findings as current, suppresses required validation, or authorizes implementation, Auto-Coder must verify that the authoritative parent/direct-child relationship state still matches the state used for that validation; otherwise the completion is stale and must not perform those current-state effects.
REQ-012: Eager validation scheduling must be identity-aware and idempotent: repeated observations of the same current standalone, decomposition, or child-individual validation identity must not create unbounded duplicate analyzer work, while a new identity belonging to a still-live readiness submission must remain eligible for its required validation.
REQ-013: Process restart must not permanently lose a live readiness submission that requires validation; Auto-Coder may reconstruct pending validation from authoritative GitHub state plus persisted validation evidence and is not required to persist a separate durable validation-work queue.
REQ-014: The pre-validation path defined by this Issue must not acquire implementation ownership, consume an implementation slot, create an implementation branch or PR, start a coding backend, or otherwise perform implementation dispatch as a consequence of scheduling or completing eager specification validation.
## Acceptance Scenarios

### AC-001 — New standalone Issue stabilizes before validation
Covers: REQ-001, REQ-002, REQ-006

Given a new Issue has no native parent or direct children and is created with `implementation-ready`,
and its body is modified during the first minute after creation,
when the stabilization window ends,
then Auto-Coder uses the latest authoritative state and schedules only the current individual-validation identity,
and no implementation slot or ownership is required for validation to begin.

### AC-002 — Later readiness does not incur another creation delay
Covers: REQ-003

Given an Issue is older than one minute and does not carry the readiness submission required by REQ-001,
when the relevant `implementation-ready` label is later added,
then its current required validation identity or identities become eligible promptly rather than waiting another fixed one-minute stabilization period.

### AC-003 — Parent marker is materialized before classification
Covers: REQ-004, REQ-005, REQ-006

Given child #B contains `Parent-Issue: #A`, has no native parent yet, and #A authoritatively resolves to a valid Issue in the same repository,
when eager pre-validation processing observes #B,
then Auto-Coder first materializes #A as #B's native parent,
re-fetches authoritative relationship state,
and only then classifies readiness or snapshots validation identities so #B cannot be incorrectly treated as standalone.

### AC-004 — Parent readiness defines the validation set
Covers: REQ-001, REQ-010

Given parent #A carries `implementation-ready` and its authoritative direct children are #B, #C, and closed already-implemented child #D,
when the stabilized submission is scheduled,
then the required work consists of decomposition validation over #A with complete membership {#B,#C,#D} plus individual validation identities for #B, #C, and #D,
and the children do not need their own `implementation-ready` labels for that validation participation.

Given instead leaf child #B carries `implementation-ready` but its authoritative parent #A does not,
when eager scheduling observes #B,
then #B is not treated as a standalone submission and that child label alone does not submit #A's parent set for validation.

### AC-005 — Native/marker conflict cannot false-success
Covers: REQ-007

Given #B declares `Parent-Issue: #A` but GitHub already records a different native parent #C,
when reconciliation runs,
then Auto-Coder does not silently choose #A or #C, does not reparent #B, and does not start specification validation or implementation from that contradictory state.

### AC-006 — Malformed or structurally invalid marker is not guessed
Covers: REQ-004, REQ-008

Given an Issue contains `Parent-Issue: #abc`, declares itself as its own parent, declares two distinct parent numbers, resolves to a pull request rather than an Issue, or requests a relationship GitHub definitively rejects because it would create an invalid hierarchy,
when reconciliation runs,
then no relationship is created by guessing and validation/implementation remain blocked until the metadata or relationship is corrected.

Given instead the declared parent is a closed Issue but the relationship is otherwise valid,
then closed state alone does not invalidate the declaration.

### AC-007 — Relationship API outage is operational, not semantic
Covers: REQ-009

Given a syntactically valid `Parent-Issue` declaration requires authoritative lookup or native materialization,
when the request fails because of timeout, rate limiting, permission failure, service failure, or another outcome that does not establish structural invalidity,
then the relevant `implementation-ready` submission remains intact,
no specification-defect finding is published,
validation/implementation do not proceed from the unresolved graph,
and the same current state remains retryable.

### AC-008 — Superseded validation cannot act as current
Covers: REQ-003, REQ-010, REQ-011, REQ-012

Given parent #A has decomposition identity G1 for current parent text and children #B and #C,
and decomposition validation of G1 is in flight or already `READY`,
when #D is subsequently reconciled as another direct child of #A,
then the authoritative set requires a new decomposition identity G2,
G1 cannot authorize G2,
and any late G1 completion cannot remove readiness, publish findings as current, suppress required G2 validation, or authorize implementation from G2.

### AC-009 — Duplicate observations do not create duplicate work
Covers: REQ-012

Given the same unchanged validation identity is observed repeatedly by polling, webhook, explicit processing, or restart recovery,
when validation is already in flight or reusable completed evidence exists,
then Auto-Coder does not launch unbounded duplicate analyzer executions for that identity.

### AC-010 — Pending validation is rediscovered after restart
Covers: REQ-013

Given a live readiness submission requires validation and Auto-Coder stops before obtaining every required completed result,
when Auto-Coder restarts,
then authoritative GitHub state and persisted evidence are sufficient to rediscover the missing current validation identities without requiring a separately persisted queue entry.

### AC-011 — Early validation stays separate from implementation dispatch
Covers: REQ-014

Given all implementation slots are occupied and stabilized readiness submissions need specification validation,
when eager validation scheduling and completion run,
then validation may produce and persist its evidence without acquiring ownership, consuming implementation slots, creating implementation branches/PRs, or starting coding backends.

## Non-goals

- Define semantic `READY` / `BLOCKED` / `ERROR` criteria.
- Define bounded parallelism, fairness, or execution order among distinct validation jobs.
- Change sibling implementation ordering or any implementation-dispatch policy beyond the explicit prohibition on implementation side effects in REQ-014.
- Infer parenthood from prose that is not a `Parent-Issue` declaration candidate defined by REQ-004.
- Silently repair contradictory native parent relationships.
- Require persistence of a dedicated validation-job queue when pending work can be reconstructed from authoritative state.

## Implementation Notes

Treat the one-minute rule as an initial creation stabilization window rather than a recurring debounce penalty after every later edit. Relationship reconciliation is a prerequisite stage, not part of semantic validation. Existing `Parent-Issue` parsing/linking code is evidence about current behavior, but the supported syntax and failure classification in this Issue are the contract for this feature. After reconciliation, the re-fetched native graph is the single parenthood source consumed downstream.
