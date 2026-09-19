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

The candidate set is the union of: durable implementation PR membership from the slot store, native GitHub Development/closing associations to the source Issue, every PR output of each positively bound Jules session, and current open PR discovery with restricted attribution. Attribution requires a closing directive for the exact local Issue, an Issue-bearing head-branch marker, a native association, or an established durable provider association — ordinary mentions and "relates to" prose are not counted. Contradictory or foreign-repository attribution, or a failed/incomplete native-association or open-PR enumeration, marks discovery incomplete rather than attributing ambiguously.

Incomplete discovery blocks retirement **unconditionally**: even when one or more already-known PR candidates are all terminal, a failed or incomplete enumeration means an unobserved implementation PR (for example, one published just after a crash before it was recorded in the slot store) could still exist. This is never gated on "no other known PRs are terminal" — that would let a stale/partial crash-recovery discovery authorize release merely because the already-known PR happened to be closed.

### Fresh, Cache-Bypassing GitHub Reads

Each candidate PR is read individually with the strict, cache-bypassing PR metadata read (`get_pull_request_metadata_strict`), never the cached `get_pull_request` read. Native Issue→PR association discovery uses `get_connected_prs(..., strict=True)`, and open-PR discovery uses the strict, complete enumeration API (`get_open_pull_requests_strict`). A 404, access denial, throttle response, malformed payload, wrong-identity response, or absence of the strict API itself is recorded as `UNKNOWN`/incomplete discovery, never as an absent or terminal PR. Listing omissions, cached responses, and Issue state are never used as substitutes — a stale cached `closed` snapshot can never authorize release when a fresh strict read says the PR is open.

### CloudRunRepository Provider Evidence

Jules ownership resolution also queries the repository's real `CloudRunRepository` API — `list_for_issue(issue_number)` (preferred) or `list_all()` — never invented methods. This surfaces accepted/unresolved Jules work recorded in `CloudRunRepository` even when it has not yet been mirrored into the slot's own `provider_sessions` list, so such work cannot be silently excluded from retirement evidence.

### Jules Session Observation

Each bound Jules session is read individually with a direct session GET (not from the cached full-session list). COMPLETED and FAILED states are terminal candidates only when established PR publication exists in the session's outputs. COMPLETED without established publication is retained as ACTIVE (waiting-for-publication). Every `pullRequest` or `pull_request` output in both mapping and list payloads is preserved without flattening to avoid silently discarding additional output entries, and a PR is identified either by a `number` field or by parsing the canonical GitHub PR URL, with repository identity always validated so a foreign repository's PR number is never reinterpreted as local.

Explicitly supported states (`QUEUED`, `PLANNING`, `IN_PROGRESS`, `PAUSED`, `AWAITING_PLAN_APPROVAL`, `AWAITING_USER_FEEDBACK`, `AWAITING_COMMENT`, `AWAITING_COMMENTS`) retain capacity as ACTIVE. Any other state — including an `AWAITING_*` variant that is *not* in this supported list — resolves to `UNKNOWN`, never implicitly ACTIVE merely because it shares the `AWAITING_` prefix.

### Activity Causality Binding

A COMPLETED or FAILED terminal state captured before a later resume, repair, or plan-approval must not settle the newer activity. The observer queries the Jules activities API (following pagination completely — a partial first page is never treated as complete history) for each terminal session and confirms that no user-message or plan-approval event post-dates the terminal completion event. Event kind is recognized from the real oneof-shaped Jules Activity payload (e.g. a `sessionCompleted` field holding the event's own object) as well as a legacy `type`-string shape. When activities are unavailable, unreadable, malformed, incomplete, or contradictory, causality is conservatively unconfirmed and the session is retained as `UNKNOWN` — absence of contrary evidence is never treated as proof that the latest admitted activity ended, even when the activities API does not exist at all for a given client.

### Retired Session and PR Guards, and Durable Outbound Admission

Before automatically resuming, continuing, or reusing a Jules session, callers check `guard_retired_session_reuse` and `guard_retired_pr_reuse`. A session or PR that belongs to a durably retired implementation slot must not be revived by stale maintenance or rediscovery scans.

`register_outbound_jules_activity` (backed by `ImplementationSlotRepository.admit_outbound_provider_activity`) must be called, and must succeed, immediately before sending resume, feedback, plan-approval, replacement-session, or publication work. Unlike `record_provider_session` — whose membership recording is idempotent and does not by itself advance the activity revision for an already-known session id — this admission unconditionally advances `activity_revision` even for a same-session continuation, so any retirement observation captured before the admission is durably staled (`STALE_OBSERVATION`) rather than able to release the slot afterward. If the owner has already retired, admission fails and the outbound mutation must not be sent. This guard/admission pair is wired into every production caller that sends implementation-mutating work to an existing Jules session: the periodic Jules maintenance loop (`check_and_resume_or_archive_sessions`, invoked with the repository's real `ImplementationSlotRepository`), its failed-session replacement-session path, the PR CI-failure repair path (`_send_jules_error_feedback`), and both PR-side merge-conflict-resolution delegation paths (`_update_with_base_branch` and `_handle_definitive_merge_rejection` in `pr_processor.py`, both via the shared `_guard_outbound_jules_send` wrapper) — a guard or admission failure blocks the send rather than being silently treated as permission to proceed.

The one outbound Jules send that is deliberately excluded from the admission half of this guard is the stale-session "stop" request (`_stop_jules_session_for_issue` in `issue_processor.py`). Ending a session creates no new implementation-mutating responsibility, and this call site is reached specifically for orphaned/legacy stale sessions that may predate any provider-session membership ever being durably recorded for their owner; requiring an active store record here (as full admission does) would incorrectly block stopping and replacing such a session. It still applies the independent, membership-free `guard_retired_session_reuse` check, so a session already committed to a durably retired incarnation is never resumed or reused through this path either.

## Runtime Reclamation Scheduling (Issue #2148)

The retirement predicate (above) and the evidence collector are pure, on-demand functions: nothing decides *when* to invoke them. `implementation_reclamation_scheduler.py` supplies that missing runtime layer, so terminal PR-backed slots are actually reclaimed during normal daemon operation and after a restart, not merely reclaimable if something happens to call the right function.

### Durable, Level-Triggered Obligations

A `ReclamationObligationStore` persists one JSON object (`implementation_slots_reclamation.json`, a sibling file next to `implementation_slots.json`, written with the same atomic-replace pattern `ImplementationSlotRepository` itself uses) keyed by owner (`issue:<number>`). Each entry records the owner's current `incarnation` and the next UTC timestamp at which it is due for a reconciliation check.

`schedule_reevaluation(owner, slots, ...)` is the entry point every trigger site calls to record "this owner needs a check". It is a pure bookkeeping call: it never starts an execution, never blocks on capacity, and is a no-op for anything that is not a currently active Issue-owned reservation. Scheduling twice for the same incarnation coalesces to the earlier of the two requested due times rather than creating a duplicate entry or pushing the check later.

### Trigger Sites (REQ-001)

`schedule_reevaluation` (via the `schedule_reevaluation_for_pr_owner` convenience wrapper, which resolves a PR payload to its owning Issue through the same `ImplementationSlotRepository.resolve_owner` used elsewhere) is called from:

* the PR-closed GitHub webhook handler in `webhook_server.py` — an authoritative closure observation, not merely a wake signal;
* the worker loop's terminal early-return for a closed PR discovered via invalidation/refresh (`automation_engine.py`, next to the existing `retire_ci_watches` call) — this path does not itself start a new coding execution;
* the explicit single-target (`--only`/`--force`) candidate-loading path (`_create_candidate_from_single`), for both an authoritative 404 (PR no longer exists) and an ordinary closed-state observation.

### Startup Recovery (REQ-002)

`recover_obligations_at_startup(slots)` runs once during `_attempt_startup_reconciliation`, right after the existing `ImplementationSlotRepository.reconcile(..., discover_open_prs=True)` call. It seeds an immediately-due obligation for **every** currently active Issue-owned reservation — not only ones a previous run already flagged — so an owner whose PR closed entirely while the daemon was offline, and is therefore absent from the open-PR enumeration that startup reconciliation performs, is still revisited. This only schedules a check: `run_due_reclamation_checks` performs the actual fresh observation and re-validates against the live store before doing anything, so a live or uncertain owner is simply retained again rather than force-released.

### Due-Check Consumer and 60-Second Cadence (REQ-003)

`run_due_reclamation_checks` services every currently due obligation once: for each, it calls `collect_retirement_observation` and hands the result to `retire_implementation_slot` (this module never reimplements terminality logic). On `RELEASED`, the obligation is cleared. On `RETAINED_ACTIVE`/`RETAINED_UNKNOWN`/`STALE_OBSERVATION`, or an evidence-collection failure, the obligation is rescheduled 60 seconds out (`RECLAMATION_RECHECK_SECONDS`).

Each owner's check runs inside `ImplementationSlotRepository.serialize(owner)` — the same per-owner cross-process lock ordinary admission/mutation paths already use — so two overlapping checks for the same incarnation never run. Before doing anything, a due check re-reads the live store's current incarnation for the owner and compares it against the obligation's recorded incarnation; a mismatch (already retired, or retired-and-recreated under a new incarnation) safely discards only the stale entry via `ReclamationObligationStore.clear`, which itself re-checks the incarnation immediately before removing the record — so an older incarnation's completion can never consume or clear a newer incarnation's obligation.

This consumer is piggybacked onto the daemon's existing per-repository capacity-refill loop (`_capacity_refill_loop`, already ticking once per second) rather than adding a new global poller or asyncio task. No obligation store scan happens when nothing is pending in it, so idle repositories with no reclamation obligation incur no extra GitHub/Jules reads on this loop's account.

### Capacity Refill Integration (REQ-006)

`run_due_reclamation_checks` accepts an `on_capacity_freed` callback, invoked synchronously after each `RELEASED` commit. `automation_engine.py` wires this to flag the same-tick capacity-refill pass as pending, so once reclaimed capacity is committed to the active store, the existing `_capacity_refill_loop` admission path picks it up in the same daemon run — it does not need a new unrelated webhook or wait for its own independent one-second polling identity check to happen to land on a later tick. The refill path itself is unmodified: reclamation only changes when capacity becomes available, never any readiness/authorization/hierarchy/quota/generation-deduplication admission decision.

### Logging (REQ-008)

All scheduling and reclamation events log through `loguru` (`get_logger(__name__)`), reaching both stdout and the configured rotating log file per this project's standard logger configuration. An expected "still active" or "still unknown" outcome logs at `INFO`/`DEBUG`; only a genuinely unexpected failure (for example, a required persistence write failing) logs at `ERROR`, so a repeated unchanged pending check does not produce an unbounded `ERROR` log stream.

### Scope Boundary (REQ-007, REQ-009)

This scheduling layer never closes/reopens/merges Issues or PRs, changes labels, cancels remote work, starts a replacement implementation, or resets a generation tombstone. It reuses the existing per-owner `collect_retirement_observation` read set for each due check rather than performing a full Jules-session enumeration or a full-repository candidate scan, and it never runs anything when no reclamation obligation is pending.
