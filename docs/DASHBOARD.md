# Auto-Coder Dashboard

> **Issue-stage routing note:** durable Review and Implementation lane records
> are currently scheduling evidence only. Until their respective workers consume
> them, the dashboard intentionally does not present those records as active
> workers, provider handoffs, or completed outcomes.
> A validation provider/model route change can supersede these scheduling
> identities without producing a dashboard event; execution remains observable
> only when an existing validation or implementation boundary actually runs.

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
`discovery_source=cache-aware-open-issue-list` and
`live_scope=target-and-related-family`. This entry point reuses unexpired discovery
caches and strictly confirms the target and related family. The source describes
the cache policy, not a guarantee of a cache hit; unrelated Issues are not
individually refreshed. Both discovery stages report `discovery_payload=issue-bodies`;
subsequent family/decomposition checks also avoid repository-wide detail enrichment,
including on a cold cache. Discovery completion does not authorize implementation.
GraphQL request streams are buffered for API admission classification without
logging their contents; transport success alone does not mark Issue work complete.

A child specification BLOCKED outcome preserves the parent `implementation-ready`
label and still prevents child dispatch. Readiness-withdrawal completion concerns
only the child's explicit label, so a blocked trace does not imply parent withdrawal.

Specification-validation diagnostics include standalone, retained-owner, and
parent/direct-child scheduling. Each producer has its own execution identity,
while a waiting worker records consumption against the exact validation decision
identity rather than claiming the producer's work. READY, BLOCKED, ERROR,
cancellation, and disabled bypasses remain distinct. These events use bounded
process-local trace retention; they are not durable review history and cannot be
recovered after restart.

Codex Cloud adversarial repair generations are recovered per finding from durable
follow-up baselines and completed assistant turns observed before new validation.
Accepted validations retain that observed-generation association across restart,
including when their first delivery-routing lookup fails, without re-observing
provider activity when an older validation is replayed. Recovering an earlier
provider receipt does not display or imply completion of a later generation. The dashboard continues
to show `Cloud Task Adversarial Feedback` only when the corresponding follow-up is
successfully accepted; a repeated turn, an unfinished turn, or an unrelated PR head
change produces no new successful-delivery event.

Adversarial rereviews resolve material test-oracle-gap updates against the
persisted PR-scoped gap registry. Compact ID/status/evidence responses and legacy
descriptive echoes therefore project the same canonical scope, while unknown or
conflicting references remain non-authorizing parse diagnostics. This changes no
dashboard event schema: the existing `pr.adversarial-validation` result and parse
diagnostic remain the displayed production signals.

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
*   **LLM Reviews**: Opens repository-wide retained review history. Exact
    Issue/PR and review-kind filters are evaluated across the durable audit,
    and a record can be reopened by its stable detail-page `review_id` link
    after a controller restart. The display is historical evidence, not a
    current approval, provider-activity, publication, or merge assertion.
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
*   **Dependency Rescan**: Opens the repository-scoped, read-only job view at
    `/dashboard/jobs/dependency-rescan`. The overview keeps this entry available
    when only retained history, a failure, or pending evidence exists. The
    durable queue's historical `dependency:1` token is displayed as an internal
    job and never as GitHub Dependency #1; the legacy
    `/dashboard/detail/dependency/1` URL redirects to the job view.

### Dependency-rescan job view

The view refreshes the local `RepoJobTraceCollector` snapshot every second and
never polls GitHub, executes a scan, dispatches an Issue, retries work, or changes
queue ownership. Intake, queued, and recovered-pending observations remain
separate from actual scan attempts. Attempt history is ordered by original start
sequence; following selects the newest attempt, while selecting an attempt pins
its opaque execution identity. An evicted pin is reported as unavailable rather
than silently replaced.

Displayed discovered and handoff totals are the producer's recorded aggregates,
not counts reconstructed from visible rows. `new_pending`, `coalesced`, and
`followup_required` are durable-invalidation dispositions. A completed rescan
means that discovery, durable handoffs, and the job's own claim acknowledgement
were confirmed; it does not say that a target Issue ran, became eligible, passed
review, or completed implementation. Target references link to ordinary Issue
details only. Missing values remain unavailable, clipped references get an
explicit partial-list marker, and scheduled wakes are not presented as
authoritative eligibility deadlines.

Repository-job history is bounded and process-local. Restart can expose newly
recovered pending work but cannot reconstruct the previous process's attempts or
triggers. Snapshot-read failure preserves the last display as stale rather than
showing a fresh empty or successful state.

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

Each Issue and PR detail page also has a separate **Review History** section.
It reads the durable review audit rather than the process-local Execution
Trace. A child Issue can show a captured parent-set decomposition review as a
related review without presenting it as the child's individual approval.
Deep links use `?review_id=<id>`; an unknown or wrong-target ID is reported as
unavailable and never silently replaced with another review.

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

For two-tier PR review, `pr.two-tier-review-effect` confirms publication of the
authenticated review and its separate finding threads, not implementation or
merge completion. Strong findings remain open until the review cycle accepts
their closure. The `github-reviewer-app:threads-v1` destination distinguishes
thread publication receipts from historical summary-only receipts.

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

The CLI preserves a diagnostic-bearing `deferred` result before explicit target
resolution, without changing production traces or asserting a resolved target type.
This presentation correction is observability-neutral: origins, admission, provider
routing, and event schemas are unchanged.
`tests/test_process_issues_cloud_only.py::test_process_issues_only_completion_status_uses_target_outcome`
verifies preserved deferrals, missing diagnostics, and target-number mismatches.

## Authenticated adjudication publishing boundary (Issue #2022)

The read-only Dashboard described above never authenticates a GitHub
operator: viewing it grants no write authority. A separate, opt-in boundary
lets exactly one authenticated Dashboard operator publish review
adjudications (see `review_adjudication.py`, `review_adjudication_github.py`)
under a configured GitHub account, mounted alongside the Dashboard by
`dashboard_adjudication.init_dashboard_adjudication` at
`/dashboard-adjudication/*`. This is a server API boundary only; the
interactive adjudication page that calls it is Issue #2023.

### Opt-in configuration

Add a repository-scoped `[dashboard_adjudication]` section to `config.toml`:

```toml
[dashboard_adjudication]
enabled = true
operator_secret_file = "~/.auto-coder/dashboard-operator.secret"
github_token_file = "~/.auto-coder/dashboard-operator.token"
allowed_origin = "https://dashboard.example.internal"
```

Authoring stays disabled — with a logged configuration diagnostic — unless
`enabled = true` **and** all three of `operator_secret_file`,
`github_token_file`, and `allowed_origin` are valid: both files must exist
and be readable by the daemon process, the operator secret must contain at
least 32 bytes, and `allowed_origin` must be exactly one HTTPS origin or a
direct loopback HTTP origin (`http://127.0.0.1`, `http://localhost`, or
`http://[::1]`), with no path, query, or fragment. There is no fallback to
anonymous access, a generated or default secret, the webhook secret, the
controller's own `GH_TOKEN`, or a reviewer-App credential — a configuration
defect simply disables this write capability while leaving the existing
read-only Dashboard unaffected.

### Operator session

Logging in (`POST /dashboard-adjudication/login`) with the exact
`operator_secret_file` contents (compared in constant time) issues a single
server-held session, scoped to this daemon's configured repository, valid
for **at most 30 minutes**. It is invalidated early by logout, by disabling
authoring, by rotating `operator_secret_file`, or by a daemon restart (the
session store is in-memory and intentionally not durable). Every context
read, draft preparation, submission, and publication-status lookup —
including any future NiceGUI/WebSocket callback the adjudication page adds —
is re-authorized at this same server boundary on every request; nothing
about a request from a browser (a hidden field, a client-side boolean, or a
copied cookie) can grant write access on its own.

State-changing requests must originate from the exact configured
`allowed_origin` (never inferred from `X-Forwarded-*` headers) and must
carry the per-session CSRF token issued at login in an `X-CSRF-Token`
header. The session cookie is `HttpOnly`, `SameSite=Strict`, and `Secure`
whenever the request arrived over HTTPS. Operator secrets and GitHub
credentials are never placed in a URL, browser storage, rendered page
state, a trace, or a log line, and the GitHub token itself is never
returned to the browser.

### Publishing identity

The publishing GitHub account is resolved from `github_token_file` — a
credential dedicated to this boundary, independent of the controller's own
`GH_TOKEN` — by asking GitHub for that credential's own stable numeric ID
immediately before every publish. That ID must appear in the effective
`[github].review_adjudicator_allowlist`, and the target thread's root must
be an automated (`Bot`) comment whose author is in the effective
`[github].pr_review_allowlist`, on an open pull request of this daemon's
own configured repository. **The authenticated local operator session and
the actual GitHub author are recorded separately**: a valid Dashboard login
proves who may use this boundary, not who GitHub will show as having
posted the decision, and neither the browser session nor the resulting
`"source": "dashboard"` field is an attestation that a human personally
typed the adjudication.

### Publication safety

Draft preparation (`POST /dashboard-adjudication/draft`) never writes to
GitHub; it returns a server-rendered proposed reply for the operator to
review. Submission (`POST /dashboard-adjudication/submit`) durably records
the decision before sending, so a lost network response never causes a
silent double-post: the outcome is recorded as unknown and is reconciled
only by reading the exact target thread for the same decision ID, payload,
and author — never by guessing or retrying blindly. Resubmitting the same
decision ID with the same payload returns the original result; resubmitting
it with a different payload is rejected. `GET
/dashboard-adjudication/status/{decision_id}` exposes this same
reconciliation as a read-only, authenticated lookup so a reconnecting
client can recover the true state without triggering another action.
Publication success itself only ever reports a confirmed GitHub comment
reference and a "published, awaiting processing" state — never that the
finding was applied, fixed, or regression-proven.
### Explicit retry handoff recovery

The durable explicit-retry handoff journal is operational recovery state and
is not currently rendered as a dashboard page. Existing issue dispatch traces
remain the dashboard-visible account of provider selection and handoff. In
particular, a journal claim or accepted receipt must not be presented as
implementation completion; incomplete or indeterminate creation remains an
operator diagnostic until its production projection is reconciled.

### GitHub Review Adjudication

A PR detail page links to a separate **GitHub Review Adjudication** page at
`/dashboard/adjudication/pr/<pr-number>`. Unlike the local diagnostic timeline,
this opt-in page reads authoritative GitHub-backed review contexts and can publish
an append-only adjudication after operator authentication, preview, and explicit
confirmation. It never treats publication or downstream delivery as proof that
code was fixed, tests passed, or the PR was approved. Issue detail pages remain
read-only and have no adjudication link.

Enable the page's authoring service with a valid `[dashboard_adjudication]`
configuration and repository-effective `[github].review_adjudicator_allowlist`.
The browser receives neither the GitHub token nor its path. Authentication proves
knowledge of the configured account-scoped operator secret; it is not proof of a
particular human's physical presence. Sessions are repository-scoped, expire
within 30 minutes, and are invalidated by logout, restart, secret changes, or
disablement.
