# Provider Repair Delivery and Completion Correlation

The provider repair correlation adapter layer normalizes actual provider follow-up
delivery and completion into causally correlated corrective-generation evidence
(GitHub Issue #2141, Stage S7 of the convergent PR review tracking family #2134).
It connects the bounded correction contracts defined in [bounded-correction-contracts.md](bounded-correction-contracts.md)
(Issue #2139) and the durable state machine in [durable-repair-allowance-state-machine.md](durable-repair-allowance-state-machine.md)
(Issue #2140) to Auto-Coder's supported provider execution boundaries.

Exhaustion of a blocker's repair allowance or ambiguity in remote observation
never changes a correctness verdict, resolves review threads, deletes obligations,
or authorizes merge.

## Outbound Boundary and Durable Admission Tickets

Before crossing any outbound repair boundary (sending a repair request to an
AI coding provider or initiating local workspace repair), the coordinator
requires and durably persists a currently valid `AdmissionTicket` (REQ-002):
- **Exact Binding:** Durably binds the admitted generation ID, canonical bundle ID,
  target repository, PR number, reviewed head SHA, provider identifier, authoritative
  owner task or invocation ID, and namespace epoch.
- **Pre-Send Baseline Persistence:** Captures and persists the native baseline evidence
  (such as latest assistant turn, session update timestamp, message count, or Git HEAD)
  needed to distinguish prior activity from subsequent correction.
- **Fail-Closed Gate:** If generation admission, ticket binding, or durable persistence
  fails, the outbound send is strictly refused. Auto-Coder never dispatches a repair
  request first and invents authority afterward.
- **Owner and Epoch Fencing:** Senders attempting to deliver a queued repair request
  to an outdated owner or against a stale epoch are refused immediately (AS-006).

## Delivery Normalization

Delivery outcomes at the transport boundary are normalized into three explicit states
(REQ-003):
1. **CONFIRMED:** The provider or transport confirmed receipt of the corrective request.
   Advances the generation to `CONFIRMED_DELIVERED` (pending completion).
2. **DEFINITE_NON_DELIVERY:** Pre-send quota refusal or admission denial proven to occur
   before any corrective work reached the remote provider. Returns the generation to
   `RESERVED` so the same logical generation may retry delivery; no failure is charged,
   and no replacement generation is created.
3. **INDETERMINATE:** A timeout, lost network response, generic client `False`, or missing
   receipt is not proof of rejection. The generation remains in `INDETERMINATE` state,
   holding the single outstanding slot to prevent blind resubmissions or duplicate
   generations. The same operation must be reconciled or explicitly superseded.

## Provider-Native Completion Evidence

Terminal correction is recognized only from positively correlated provider-native work
occurring causally after the admitted request or from normal completion of the exact
local invocation (REQ-004, REQ-005):

### Codex Cloud (`codex-cloud`)
- **Native Evidence:** WHAM assistant turn records (`WhamTurn`).
- **Correlation:** Anchored to `pre_send_turn_id`. Positively correlated completion is
  established by a completed assistant turn (`resolve_completed_assistant_turn_after` /
  `completed_assistant_turn:<turn_id>`) appearing after the baseline.
- **Competing Requests:** Any intervening user turn in the task session breaks correlation
  and produces an explicitly `AMBIGUOUS` outcome (AS-003).
- **Inability / No-Change:** Explicit statements of inability or no code changes
  (`CANNOT_FIX`, "no changes needed") are recognized as completed corrections with
  `code_changed=False`, eligible for independent validation rather than discarded.

### Google Jules (`jules`)
- **Native Evidence:** REST session state (`outputs`, `updateTime`, `state`, messages).
- **Correlation:** Anchored to pre-send `updateTime` and message baseline. Completion
  requires the session state to reach `COMPLETED` with timestamp advancement past the
  baseline.
- **Paused / Intermediate States:** `PAUSED`, `AWAITING_USER_FEEDBACK`, `AWAITING_PLAN_APPROVAL`,
  or a changed `updateTime` alone is never a completed correction (REQ-005, AS-002).
- **Inability / No-Change:** Explicit session output messages indicating inability or
  unmodified files are recorded as completed corrections with `code_changed=False`.

### Claude Routine (`claude-routine`)
- **Native Evidence:** Routine session API data (`raw_data`, `updated_at`, `state`).
- **Correlation:** Requires session status to reach `COMPLETED` causally after the pre-send
  timestamp baseline.
- **Paused / Intermediate States:** `PAUSED` (or absence of a created pull request) and
  routine timestamp bumps alone do not constitute completed corrections.
- **Task Identity:** Activity from another task ID or session is rejected (AS-002).

### Local Execution (`local`)
- **Native Evidence:** Execution outcome, process exit code, and repository file modifications.
- **Correlation:** Anchored to local invocation ID and pre-execution Git HEAD commit.
- **Outcomes:** Clean execution with modified files records `code_changed=True`. An explicit
  refusal (`CANNOT_FIX` or no diff) records `code_changed=False`. Unhandled process crashes
  or abnormal interruptions remain `UNAVAILABLE` transport failures.

## Validation Evidence Capture and Freezing

Independent validation must be causally bound to completed corrective generations (REQ-007):
- **Validation Evidence Binding:** Before an independent adversarial reviewer or test
  oracle runs, `capture_validation_evidence` freezes the completed generation ID, bundle ID,
  target head commit, and provider native completion reference into a `ValidationEvidenceBinding`.
- **Premature Validation Guard:** A validation captured before a generation completes its
  corrective work cannot be combined with later provider activity to settle the generation
  (AS-005).
- **Settlement Fencing:** Result processing requires the exact matching binding. If the head
  commit, requirement manifest, or owner changes before validation finishes, revalidation or
  explicit supersession is required.

## Restart Reconstruction and Uncertainty Retention

On daemon restart or process recovery:
- **Durable Reconstruction:** Active admission tickets, delivery observations, and
  correlation bindings are reconstructed from the local SQLite store (REQ-008).
- **Uncertainty Retention:** When evidence cannot prove correlation, the system retains
  uncertainty (`INDETERMINATE` / `UNAVAILABLE`) and denies speculative re-execution rather
  than treating the task as newly idle or resetting history.
- **Ownership Preservation:** Quota limits and remote execution retain the existing owner
  task and PR; Auto-Coder never creates a replacement PR to evade uncertain delivery or
  exhaustion (REQ-009).

