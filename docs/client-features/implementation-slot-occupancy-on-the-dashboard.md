# Implementation-slot occupancy on the dashboard

The mounted `/dashboard/` main page shows a distinct Implementation Slots
section after Active Workers, populated on initial load and refreshed on
its own one-second cadence from `ImplementationSlotRepository.snapshot()`
through `AutomationEngine.get_implementation_slot_snapshot`, independent
of local worker/queue state. Before any successful observation it shows an
explicit unavailable reason, never a fabricated zero/free display; after a
successful observation, a later read failure preserves the entire
last-known snapshot with a stale indicator and an unchanged
last-successful timestamp instead of mixing old rows with new counters.
Every recorded owner is shown -- including one with no active local
worker, empty membership lists, or admission flags absent from a legacy
reservation -- with its executions (PID/`started_at` when recorded),
implementation PR numbers, provider-session identifiers, and
`admission_pending`/`admission_established` (`false` distinguished from
not recorded), all described as recorded evidence rather than a
running/completed/free assertion. Normal usage counts each non-emergency
owner once regardless of its membership size, emergency usage is reported
separately, and usage above the configured limit is shown, not clamped.
Owner and PR entries link to the existing repository-scoped detail view.
Refreshing this panel never issues a GitHub/provider request, a liveness
probe, or a slot reservation/release/reconciliation call, and a slow
filesystem observation runs off the page's event loop so it cannot
block the rest of the dashboard.
