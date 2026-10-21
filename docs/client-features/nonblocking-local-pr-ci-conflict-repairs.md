# Non-blocking local PR CI and conflict repairs

Eligible locally owned CI-failure and merge-conflict repairs are admitted as durable local jobs at the existing PR routing boundary. Admission returns a typed handoff containing the exact job, head/base, repair kind, and implementation execution; it does not claim that editing, tests, publication, review, or merge completed.

The ordinary PR worker releases its transient processing and checkout scopes after admission while the durable job retains the implementation execution. The repository-scoped local runner later performs model work in its private workspace and the existing repair finalizer publishes exact-head artifacts and wakes a fresh PR evaluation. Cloud-owned work, dependency bots, unsupported Jules routes, and green-CI review/merge deferrals retain their existing routing.

A missing or contradictory durable ownership read is fail-closed: the execution is retained rather than being retired while a committed handoff may exist. Repeated observations use the stable current-head operation identity and cannot infer success from an offer.
