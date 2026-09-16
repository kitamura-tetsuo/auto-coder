# Mandatory production-to-view dashboard observability regressions

Dashboard observability is protected by deterministic tests in the ordinary PR
shards and by a documented `scripts/test.sh` invocation. The maintained coverage
inventory maps processing, validation, resumption, CI, merge, and asynchronous
origins to concrete collected tests. Joined regressions execute production entry
points, consume the real structured collector snapshot through the mounted detail
view, and independently assert business outcomes. Negative controls reject missing
emissions, scope reattribution, evidence coercion, and false completion while the
generic renderer continues to accept new or repeated stages without a node map.

Two inventory rows previously cited pre-existing tests that never actually
exercised the diagnostic trace they were listed against: validation-publication
resumption pointed at a durable-effect test that does not touch `TraceCollector`,
and asynchronous PR adversarial validation pointed at a test that mocks out
`_handle_pr_merge` (the function that emits `pr.adversarial-validation`) entirely.
`tests/test_dashboard_observability.py::TestNewOriginCoverage` now drives both
production handlers for real and reads their diagnostic trace back, and the
inventory keeps the original citations alongside the new ones since they still
cover the durable-effect/business-behavior contract. The same file's
`TestOutcomeMatrixCoverage` also adds the previously-unexercised `known_empty`,
`partial`, `throttled`, and `superseded` CI-availability values, ordinary-cloud
routing to the Claude Routine and Codex Cloud backend types, corrective work
accepted without a repair claim, and queued-vs-disabled-vs-blocked validation;
`TestJoinedProductionToView` and `TestAdditionalNegativeAndMutationControls` add
production-to-mounted-view coverage for PR/merge-operation resumption, an
accepted handoff without PR publication, a validation job as its own execution,
two concurrently processed Issues never sharing or swapping execution identity,
and unavailable CI evidence reaching the mounted view as an explicit `unknown`
outcome rather than a coerced boolean.
