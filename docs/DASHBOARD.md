# Auto-Coder Dashboard

> **Issue-stage routing note:** durable Review and Implementation lane records
> are currently scheduling evidence only. Until their respective workers consume
> them, the dashboard intentionally does not present those records as active
> workers, provider handoffs, or completed outcomes.

For an explicit Issue restart (`--only <issue> --force --retry`),
`issue.manual-retry` records authorization after admission checks. A completed
authorization is not a successful provider handoff or implementation: inspect
the following provider dispatch result. The Issue slot retains prior remote
session membership and adds the accepted replacement; retry does not cancel
old sessions. The latest accepted session becomes the cloud tracking target.

The detail view's `issue.family-discovery` stage reports family-local live
confirmation using the cached open-Issue list for discovery. Its facts show the
discovery source, live scope and confirmed declared children. `completed` means
that discovery pass finished, with `authorizes_execution=false`; validation and
implementation admission still have to pass. Body-only relationships absent
from both the cached list and native membership become discoverable on list refresh.
At `--only` startup, `issue.explicit-relationship-discovery` instead reports
`discovery_source=live-open-issue-list` and `live_scope=all-open-issues` because
this entry point refreshes the complete list and every open Issue.

Specification-validation diagnostics include standalone, retained-owner, and
parent/direct-child scheduling. Each producer has its own execution identity,
while a waiting worker records consumption against the exact validation decision
identity rather than claiming the producer's work. READY, BLOCKED, ERROR,
cancellation, and disabled bypasses remain distinct. These events use bounded
process-local trace retention; they are not durable review history and cannot be
recovered after restart.

The Auto-Coder Dashboard provides a real-time visualization of the automation engine's activities, including the queue status, active workers, and detailed logs for processed items (Issues and Pull Requests).

Issue and PR processing use separate worker pools, with one worker per pool by
default. Worker IDs are unique across both pools. The queue lists PRs before
Issues for display, but each pool advances independently: a busy Issue worker
does not prevent a PR worker from starting, and vice versa. Shared implementation
admission limits still apply. Queue membership is waiting work, not evidence that
an execution has started; detail traces retain each item's own execution identity.

## Enabling the Dashboard

The dashboard is integrated into the Auto-Coder daemon, which can be started in two ways:

### 1. Using `process-issues` with Webhook

By default, the `process-issues` command enables the webhook server, which also hosts the dashboard.

```bash
auto-coder process-issues --repo owner/repo --enable-webhook
```

To disable the dashboard (and webhook server), use:

```bash
auto-coder process-issues --repo owner/repo --disable-webhook
```

### 2. Using `serve` Command

You can run the Auto-Coder daemon explicitly using the `serve` command:

```bash
auto-coder serve --repo owner/repo
```

This command starts the FastAPI server hosting both the webhook endpoints and the dashboard.

## Accessing the Dashboard

Once the daemon is running, the dashboard is accessible at:

**[http://localhost:8000/dashboard/](http://localhost:8000/dashboard/)**

(Default port is 8000, but can be configured via `--port` option)

## Dashboard Features

### Main View

The main dashboard view provides an overview of the current system state:

*   **Search Section**: Allows quick navigation to the detail view of a specific Issue or PR. Select the type (PR/Issue) and enter the number, then click "Go".
*   **Active Workers**: Displays the currently active worker tasks. Each card shows the worker ID, the item being processed (with a link to details), and the current task description.
*   **Implementation Slots**: A distinct, read-only view of durable
    implementation-slot occupancy -- not derived from Active Workers or the
    Queue below. A remote handoff or a retained PR can occupy a slot after
    every local worker for it has gone idle, so this section is populated
    from the controller's own slot-snapshot boundary
    (`AutomationEngine.get_implementation_slot_snapshot`), refreshed on its
    own one-second timer, independently of the rest of the page. See
    [Implementation Slots panel](#implementation-slots-panel) below.
*   **Queue**: Lists pending items in the processing queue. The table shows the item type, number, priority, and title.

#### Implementation Slots panel

Each row is one durably recorded owner (an Issue or a standalone PR), not a
worker or a queue entry -- an owner with no active local worker (a
finished execution, a remote provider handoff, or a process that exited
without releasing it) still occupies its slot and is still shown.

*   **Status line**: before any successful observation, an explicit
    "unavailable" reason is shown -- never a fabricated zero/free capacity
    or an empty-success message. After a successful observation, a later
    read/validation failure keeps showing that entire last-known
    snapshot (rows and counters together) with a prominent "STALE" banner
    and the unchanged last-successful observation time; the next
    successful observation replaces it and clears the banner.
*   **Counters**: repository/store identity, normal used/limit/available,
    and emergency usage (0 or 1), shown separately from normal usage.
    Several executions, PRs, or provider sessions recorded under one owner
    still count as one slot; normal usage above the configured limit is
    shown as-is, never clamped or hidden.
*   **Owner rows**: issue/pr identity (linking to the same
    `/detail/{item_type}/{item_number}` route as the rest of the
    dashboard), normal/emergency class, every recorded execution ID with
    its PID/`started_at` when recorded, every recorded implementation PR
    number (also linked), provider-session identifiers (plain text, never
    a link -- a session identifier is opaque recorded evidence, not a
    navigable trace ID), and `admission_pending`/`admission_established`
    with `false` shown distinctly from "not recorded". These are local
    recorded facts, not a live running/completed/free assertion, and never
    an inferred retention reason or provider identity.
*   **Refresh behavior**: at most one observation request is in flight per
    mounted page; observations read a complete atomically published state
    image without acquiring writer locks, including during slow admission
    checks. A slow filesystem read runs in a background thread and cannot block this page's event loop or pause the Active
    Workers/Queue/Open Items refresh above. An unchanged tick, or a
    membership-only change for one owner, never rebuilds unrelated owner
    rows or resets page scroll. Loading, linking to, or refreshing this
    panel never triggers GitHub/provider requests, liveness probes, CLI
    dispatch, or any slot reservation/release/reconciliation -- it is a
    read-only observation.

### Detail View

Clicking on an item in the Active Workers or Queue list (or using the Search) takes you to the Detail View for that specific Issue or PR, at `/dashboard/detail/{item_type}/{item_number}`.

The Detail View is a read-only projection of the automation process's own
locally retained diagnostic evidence (the schema-version-1 structured
trace collected by `execution_trace.TraceCollector`), scoped to this
dashboard's single configured repository. It never queries GitHub or any
other provider, and it never asserts that what it shows is current
GitHub/provider state -- everything displayed is an *observation*, with
its own observation timestamp, not a live status.

#### Executions, not a static workflow

An **execution** is one controller evaluation of the selected Issue/PR.
The page does not maintain a hand-drawn flowchart of "the" Issue/PR
workflow and does not infer which steps ran from log message text --
instead it renders exactly the structured events the producing code
actually recorded for the selected execution, in the order they were
published. Previously unseen stage identifiers and outcomes show up
automatically; repeated occurrences of the same stage remain individually
visible.

*   **Follow latest** (the default): the page follows whichever execution
    has the newest execution-start sequence currently retained, and
    switches automatically as new executions start.
*   **Pinned**: using the older/newer navigation pins one exact execution
    identity. A pinned selection does not move when new executions start,
    and a late event for an older execution never promotes it to "latest".
    Use **Follow latest** to return to automatic following.
*   If a pinned execution's evidence is later evicted by this process's
    bounded retention, the page reports that it is no longer retained --
    it never silently substitutes whatever execution now occupies that
    execution's former position in the list.

The local view refreshes every second while connected; polling and any
page-owned timers stop automatically when the page is closed or the
client disconnects. Empty or truncated local history is shown explicitly
and is not evidence that no earlier work happened, nor that any
in-progress remote work (e.g. a cloud handoff) has finished.

#### Sections

*   **Processing Path**: a Mermaid diagram of the selected execution's
    observed events in publication order. Arrows are labeled "observed
    order" -- they describe recording order only, not an inferred
    control-flow or causal edge. All event text is rendered as inert,
    escaped content, so unusual or adversarial characters in a label
    cannot alter the diagram. "Copy Mermaid Code" always copies the
    diagram currently shown for the selected execution.
*   **Observed Evidence**: one row per recorded stage-result/execution
    outcome for the selected execution, with its own explicit outcome
    (`unknown` when none was recorded -- never coerced to success or
    failure) and the facts that stage actually reported. Evidence from
    different executions or revisions is never merged into a synthetic
    combined state.
*   **Decision Log**: every recorded event for the selected execution,
    newest first.
*   **Unscoped / legacy diagnostic evidence**: structured events recorded
    without an execution identity, and any remaining pre-migration
    `TraceLogger` text entries for this item. These are shown as raw,
    inert diagnostic text and are never assigned a guessed execution or a
    successful outcome.

## Configuration

The dashboard uses NiceGUI and FastAPI. You can configure the host and port via CLI arguments:

*   `--host`: Host to bind the server to (default: 0.0.0.0).
*   `--port`: Port to bind the server to (default: 8000).

Example:

```bash
auto-coder serve --port 8080
```

Then access at `http://localhost:8080/dashboard/`.

PR detail traces include **implementation admission**, identifying the logical
owner whose slot is used. Claude Routine PRs with a session URL matching a
recorded provider session reuse that owner's slot, including when capacity is
full. Unknown sessions remain subject to ordinary capacity limits. A completed
admission stage does not mean review or merge has completed; a deferred stage
reports the admission reason.


An explicit `--only` lookup deferred before candidate creation reports `deferred`
with the governor reason and retry deadline in the CLI result. There is no
candidate execution trace yet: no processing origin or provider dispatch has
started. Governor transaction contention uses the existing structured diagnostic
fields (`decision=deferred`, `delay_reason=governor_transaction_contention`).
The dashboard execution schema and production processing emissions are unchanged.
Regression coverage: `tests/test_automation_engine.py::TestAutomationEngine::test_explicit_target_preserves_governor_deferral`
and `tests/test_github_request_governor.py::test_reservation_lock_contention_recovers_same_governor`.

Outcome-persistence lock contention also emits `governor_transaction_contention`.
The completed response is retained and blocks further sends until persisted;
this diagnostic does not mean that the preceding request was never sent.
No candidate execution or provider-success event is emitted by this retry.

Coverage: `tests/test_github_request_governor.py::test_outcome_lock_contention_retains_response_before_next_send` exercises successful and throttled responses.
