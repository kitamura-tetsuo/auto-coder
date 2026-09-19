# PR Repair Exhaustion and Operator Resumption

PR repair exhaustion and operator resumption stop infinite or thrashing repair
loops before dispatch, preserve merge and semantic gates, and provide an explicit,
audited resumption workflow for human operators (GitHub Issue #2142, completing the
convergent PR review tracking family #2134).

It consumes:
* The [canonical PR blocker ledger](canonical-pr-blocker-ledger.md) (Issue #2135),
* [Bounded correction bundles](bounded-correction-bundles.md) (Issue #2139),
* The [durable repair allowance state machine](durable-repair-allowance-state-machine.md) (Issue #2140), and
* [Provider repair correlation](provider-repair-correlation.md) (Issue #2141).

## Problem and Invariant

When an automated repair loop fails to converge across repeated attempts, continuing
unbounded repair dispatches wastes provider quota, churns repository history, and creates
unhelpful noise. Conversely, silently abandoning a PR or guessing mergeability without
satisfying semantic requirements compromises repository safety.

**Core Invariant:**
When a pull request's repair allowance is exhausted for any currently open canonical
blocker, all automatic repair dispatch origins must halt immediately and fail closed.
Automatic merge must never occur while blockers remain open. Only an explicit operator
command or a human commit that resolves all exhausted blockers can clear the hold.

## Repair Exhaustion Gate

Before outbound ticket creation, generation admission, or repair dispatch, the controller
checks whether any open canonical blocker has exhausted its failure allowance via
`check_pr_repair_exhaustion()`.

### Stopped Production Origins

Exhaustion stops every production repair origin:
1. **Local and Cloud Provider Follow-ups:** Outbound admission tickets are refused
   immediately with machine-readable reason `AUTO_REPAIR_EXHAUSTED`.
2. **Local PR Repair Tasks:** Halts before allocating or executing corrective tasks.
3. **Ordinary Review Thread Remediation:** Bypasses repair delegation and logs
   exhaustion.
4. **Cached Non-pass Reports:** Replays cached non-pass results without triggering
   repair attempts.
5. **CI and Merge Conflict Remediation:** Skips remediation dispatch and leaves the
   PR in its current state.
6. **Maintenance and Recovery Continuation:** Refuses automated replacement or re-issue
   of failed turns.
7. **Adversarial Validation Review Limits:** Reaching review limits with exhausted
   allowance blocks automatic merge and records `Outcome.BLOCKED` rather than proceeding
   to merge.

### Fail-Closed Merge Preservation

Repair exhaustion never:
* Fabricates a PASS verdict,
* Bypasses CI, branch protection, or adversarial validation gates,
* Auto-merges an unverified PR, or
* Closes an active PR prematurely.

### Single De-duplicated PR Notification

When repair exhaustion is first detected, a structured comment is posted to the PR
explaining:
* The exhausted canonical blocker IDs and associated qualified requirements,
* The failed correction count and limit,
* Explicit instruction that automatic repairs have been halted, and
* The exact copy-pasteable operator resume CLI command including the current ledger epoch.

The comment is de-duplicated using a machine-readable marker
(`<!-- auto-coder: pr-repair-exhausted -->`) so repeated runs do not spam the PR.

## Operator Resumption Workflow

Human operators investigate exhausted PRs and resume automation using dedicated CLI
commands.

### Read-Only Inspection: `auto-coder pr-repair status`

The status command inspects the durable state of canonical blockers and repair allowances:
```bash
auto-coder pr-repair status --repo <owner/repo> --pr <pr_number> [--json]
```
* **Read-only:** Performs zero database writes or state mutations.
* Displays current ledger epoch, overall repair state (`ALLOWABLE`, `EXHAUSTED`, or
  `RECONCILIATION_REQUIRED`), open blockers, exhausted blockers, per-blocker failure
  counts/limits, and any outstanding generation lifecycles.

### Explicit Grant: `auto-coder pr-repair resume`

When an operator determines repairs should continue (e.g. after fixing upstream
dependencies or updating requirements), they grant fresh repair allowance:
```bash
auto-coder pr-repair resume --repo <owner/repo> --pr <pr_number> --expected-epoch <epoch> --request-id <token> [--new-limit <n>] [--target-blocker-id <id>] [--json]
```

#### Preconditions and Safety Checks
* **Compare-and-Set (CAS):** Requires `--expected-epoch` matching the current
  `RepairAllowanceLedger` epoch. If concurrent activity advanced the epoch, the command
  fails with contention.
* **No Outstanding Generations:** Refuses grants if a generation is currently in-flight
  (`RESERVED`, `CONFIRMED_DELIVERED`, `OBSERVED_TERMINAL_UNAVAILABLE`).
* **Idempotency:** The mandatory `--request-id` prevents duplicate grants on retried CLI
  invocations.

#### Re-evaluation and Crash Recovery
* Atomically commits an `operator_grant` record and transitions target blocker
  allowance status back to `ALLOWABLE`.
* Immediately schedules an asynchronous re-evaluation obligation in the durable
  `PendingWorkStore` for the PR (`WorkIdentity(repo, "pr:<pr>", "pr_processing", "")`).
* The grant row tracks `reevaluation_delivered`. If the process crashes after committing
  the grant but before scheduling pending work, startup reconciliation
  (`reconcile_unfulfilled_grant_reevaluations`) automatically schedules the re-evaluation
  on the next run.

## Human Commit Revalidation

If a human contributor or operator pushes new commits directly to the PR branch that
resolve the underlying issues, subsequent verification records `VERIFIED_CORRECTION`
in the canonical blocker ledger.

Once all exhausted blockers transition out of `OPEN`, `check_pr_repair_exhaustion()`
evaluates to non-exhausted, naturally unblocking the PR without requiring an explicit
CLI resume command, while retaining full historical failure records.

## Configuration

The default repair allowance limit is configured in `config.toml`:

```toml
[pr_repair]
max_failed_corrections = 3

# Optional per-repository override
[repository."owner/repo".pr_repair]
max_failed_corrections = 5
```

* Defaults to 3 if omitted.
* Must be a positive integer (`> 0`). Invalid values are rejected at startup or
  config load time with a clear error.

