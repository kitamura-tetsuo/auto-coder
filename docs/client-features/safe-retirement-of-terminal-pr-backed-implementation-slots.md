# Safe Retirement of Terminal PR-Backed Implementation Slots

Implementation ownership slots are bounded capacity resources that prevent conflicting concurrent modifications to code, tests, and pull requests. Previously, an open source Issue remained an unconditional retention condition for its implementation slot, causing slots to stay occupied even after all associated pull requests had been authoritatively closed or merged and all associated remote provider tasks had ended.

Safe retirement of terminal PR-backed implementation slots introduces a coordinated retirement model and durable transaction boundary that releases an implementation reservation's active capacity when all of its published work is conclusively terminal, without waiting for or causing the source Issue to close.

## Scope and Invariants

Retirement applies exclusively to ordinary Issue-owned implementation reservations that have established at least one implementation PR and whose retained work is identified as local execution or Jules provider tasks. Speculative competition owners, recurrent tasks, standalone PR owners, never-published reservations with no PR, unsupported providers, and ambiguous provider attribution are out of scope and cannot acquire an early-release entitlement.

Retirement requires proving a conclusive terminal predicate across complete, coherent lifecycle evidence:
* Every associated implementation pull request must be authoritatively closed or merged.
* Every recorded local execution must be conclusively ended.
* Every associated remote work unit must be conclusively ended for its latest admitted activity.
* No unresolved submissions, assigned repairs, replacement-publication obligations, or already-admitted retry handoffs may still exist.

If any associated pull request remains open, any local execution is live, any remote work is active, or any continuing obligation is pending, ownership is retained as `RETAINED_ACTIVE`. If any lifecycle evidence is unavailable, malformed, ambiguous, or unreadable (such as indeterminate process liveness or ambiguous provider attribution), ownership is retained as `RETAINED_UNKNOWN`.

## Incarnation and Activity Revision Protection

Each reservation is assigned a durable `incarnation` identity and monotonic `activity_revision` counter upon admission. Every subsequent mutation—admitting or finishing a local execution, adding an implementation PR, recording or completing a provider session, or binding an authorized validation identity—monotonically advances the activity revision.

Retirement validates the exact reservation incarnation and activity revision against the live store within a serialized, locked transaction. If any newer execution, membership addition, provider follow-up, or reservation recreation has occurred since the observation was captured, the observation is rejected as `STALE_OBSERVATION`, ensuring that concurrently admitted work is never deleted or orphaned.

## Active versus Retired Separation

A successful retirement transaction removes the incarnation from the active slot store (`implementation_slots.json`), immediately freeing active capacity for subsequent admissions. The historical associations (repository, Issue number, incarnation, PRs, sessions, generation, and retirement timestamp) are durably preserved in a companion store (`implementation_slots_retired.json`).

Active slot counting, snapshot reporting, and capacity admission evaluate only active reservations. Retired records do not count toward slot usage, do not consume capacity, and do not qualify as active implementation evidence. Conversely, retired history does not permanently blacklist the Issue or PR: newly authorized work may reserve capacity through ordinary admission when slots are available.

## Generation-Start Preservation and Crash Safety

Before removing an active reservation, retirement ensures that any captured generation that acquired implementation responsibility has its acquired-start fact durably preserved in the routing store's tombstones. This guarantees that capacity reclamation never turns a completed generation into an unauthorized fresh duplicate start. Legacy reservations that lack a generation binding are retired with their legacy status preserved, without synthesizing or guessing a semantic generation.

The retirement transaction follows a crash-safe commit sequence: acquired-start preservation and retired history are committed before the active reservation is removed. Any interruption or write failure prior to active removal recovers the reservation as still-occupied, ensuring capacity is never released optimistically while fencing evidence could be lost. Repeating retirement after a committed result is fully idempotent.

## Evidence Collection for Retirement Decisions

The `implementation_retirement_observer` module derives authoritative `ImplementationRetirementObservation` instances from live API data. It is strictly read-only — it does not close issues, merge or reopen PRs, send Jules messages, or mutate any store.

### PR Candidate Set Construction

The candidate set is the union of: durable implementation PR membership from the slot store, native GitHub Development/closing associations to the source Issue, every PR output of each positively bound Jules session, and current open PR discovery with restricted attribution. Attribution requires a closing directive for the exact local Issue, an Issue-bearing head-branch marker, a native association, or an established durable provider association — ordinary mentions and "relates to" prose are not counted. Contradictory or foreign-repository attribution marks discovery incomplete rather than attributing ambiguously.

### Fresh Per-PR GitHub Reads

Each candidate PR is read individually with a fresh GitHub REST API call. A 404, access denial, throttle response, malformed payload, or wrong-identity response is recorded as `UNKNOWN`, not as an absent or terminal PR. Listing omissions, cached responses, and Issue state are never used as substitutes.

### Jules Session Observation

Each bound Jules session is read individually with a direct session GET (not from the cached full-session list). COMPLETED and FAILED states are terminal candidates only when established PR publication exists in the session's outputs. COMPLETED without established publication is retained as ACTIVE (waiting-for-publication). Every `pullRequest` or `pull_request` output in both mapping and list payloads is preserved without flattening to avoid silently discarding additional output entries.

### Activity Causality Binding

A COMPLETED or FAILED terminal state captured before a later resume, repair, or plan-approval must not settle the newer activity. The observer queries the Jules activities API for each terminal session and confirms that no user-message or plan-approval event post-dates the terminal completion event. When activities are unavailable or contradictory, causality is conservatively unconfirmed and the session is retained as UNKNOWN.

### Retired Session and PR Guards

Before automatically resuming, continuing, or reusing a Jules session, callers check `guard_retired_session_reuse` and `guard_retired_pr_reuse`. A session or PR that belongs to a durably retired implementation slot must not be revived by stale maintenance or rediscovery scans. The `register_outbound_jules_activity` function must be called before sending resume, feedback, plan-approval, or replacement work to advance the activity revision durably before the outbound call.
