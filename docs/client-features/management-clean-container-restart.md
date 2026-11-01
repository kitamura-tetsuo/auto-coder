# Management clean container restart

`restart_after_clean_shutdown: true` is an opt-in bootstrap configuration for a
selected management runtime with a configured private `drain_ack` path. On
SIGTERM the owning Supervisor asks its exact child to drain once and retains
its lock until the child has exited. A confirmed drain receipt and OS exit are
persisted as the original stop effect. A timeout, missing receipt, corruption,
unknown outcome or abrupt termination is not a confirmed stop and does not
authorize an automatic restart. Container stop timeouts must be long enough for
the workload; this feature never force-signals an unsettled child.

A new Supervisor incarnation, while holding its own owner lock, validates the
retained child and launch lifetime, exact stop operation and receipt, selected
runtime, operation and handoff stores, recovery mailbox, and actual listener
and prior lifetime absence before a single deterministic start claim. It locks
the existing stores across admission. Any unobservable or unresolved evidence
refuses; reconstruction and lost responses cannot authorize a second launch.
The fresh-initialization command remains a one-shot when unresolved; after an
authenticated successful first launch, reconstruction also reacquires and holds
the original instance's lock and checks retained root and input identities.
The first-launch claim and OAuth grants are preserved. Authentication, process
launch and exact-child readiness remain separate observations.

This feature does not recover an unknown or failed child, clear a management or
worker operation, carry forward a different operation journal, update a runtime,
change credentials or create a public process-control endpoint. Host operators
must retain the old owner-lock mount and state, arrange Docker and proxy startup
ordering, preserve both old and new journals through migration, and verify actual
endpoint readiness after restart. A failed or ambiguous launch requires explicit
investigation, not a new operation key or a state reset.
