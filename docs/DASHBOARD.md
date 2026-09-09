# Auto-Coder Dashboard

The Auto-Coder Dashboard provides a real-time visualization of the automation engine's activities, including the queue status, active workers, and detailed logs for processed items (Issues and Pull Requests).

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
*   **Queue**: Lists pending items in the processing queue. The table shows the item type, number, priority, and title.

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

## Keeping observability accurate

The detail view only ever displays what production code actually recorded
into `execution_trace.TraceCollector`. There is no second, hand-maintained
model of "the" Issue/PR workflow to keep in sync -- stage labels, kinds,
outcomes, and facts all come from the producing boundary itself (see
`_record_pr_stage` in `pr_processor.py` and the equivalent Issue-side
helpers in `issue_processor.py` / `automation_engine.py` for where these are
emitted). That also means the page **cannot** verify that instrumentation is
complete: it can only render events that were actually published. A stage
that stops running, or a boundary that stops calling `record_event`/
`start_execution`, simply disappears from the page -- and touching
`dashboard.py`, editing the diagram, or getting an LLM to approve a
generated Mermaid snapshot proves none of that, because all three can
succeed with the underlying production emission removed or wrong. The only
way to catch that class of regression is a test that drives the real
production entrypoint and reads the result back from the real
`TraceCollector` (and, for page-level regressions, through the real mounted
`/dashboard/detail/{item_type}/{item_number}` page) -- see
`docs/DASHBOARD_OBSERVABILITY_COVERAGE.md`.

Run the deterministic observability regression suite locally with:

```bash
bash scripts/test.sh tests/test_issue_production_instrumentation.py \
  tests/test_pr_production_instrumentation.py \
  tests/test_dashboard_observability_joined.py \
  tests/test_execution_trace.py \
  tests/test_dashboard_detail_logic.py \
  tests/test_dashboard_detail.py
```

This suite requires no live GitHub/provider credentials and makes no LLM
calls; it is part of the normal `PR Tests` collection (plain `pytest` files
under `tests/`), so it runs on every PR without extra configuration.

**When to update instrumentation, coverage, or this document.** Whenever a
change touches a processing origin, an admission/validation gate, an
outcome a stage can report, provider/backend routing, a resumption path
(pending-work, validation-publication, merge-operation, adversarial
validation), or the diagnostic event schema itself, the same change must
either:

* assess whether the production boundary needs a new/updated
  `record_event`/`start_execution` call, add or update the corresponding
  entry in `docs/DASHBOARD_OBSERVABILITY_COVERAGE.md` and its test, and
  update this document if the observable behavior it describes changed; or
* record a concrete, specific reason the change is observability-neutral
  (e.g. "pure refactor of an internal helper, no change to what is
  recorded, verified by the existing suite still passing unmodified").

A display-label-only change to an existing stage, or a new stage identifier
the generic renderer has never seen before, does **not** require editing
`dashboard.py`'s Mermaid rendering: the page already renders any stage it is
given (see `dashboard_detail.build_observed_path_diagram`) -- co-modifying
`dashboard.py` for its own sake is not evidence of correct instrumentation,
and its absence is not evidence of a missed update.

**What counts as observed evidence.** Every displayed fact is an
observation, not current GitHub/provider state: it carries the execution
that produced it, an explicit outcome (`unknown` when none was recorded --
never coerced to a boolean), and, for CI evidence specifically, an
availability value (`known`, `known_empty`, `partial`, `unavailable`,
`throttled`, `superseded`) kept distinct from any pass/fail verdict. Stage
labels/facts are emitted at the boundary that owns the fact (e.g.
`_check_github_actions_status` in `util/github_action.py` for CI
observation, `_record_pr_stage` call sites in `pr_processor.py` for PR
stages, and the `_record_*_stage` helpers in `automation_engine.py` /
`issue_processor.py` for Issue stages) -- never reconstructed from a
worker's overall return value or an actions string.

## Configuration

The dashboard uses NiceGUI and FastAPI. You can configure the host and port via CLI arguments:

*   `--host`: Host to bind the server to (default: 0.0.0.0).
*   `--port`: Port to bind the server to (default: 8000).

Example:

```bash
auto-coder serve --port 8080
```

Then access at `http://localhost:8080/dashboard/`.
