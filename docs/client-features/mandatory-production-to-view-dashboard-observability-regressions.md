# Mandatory production-to-view dashboard observability regressions

Local PR conflict repair uses the existing `pr.mergeability-remediation` stage:
confirmed repair publication is `COMPLETED` with `result=success`, and failed
repair is `FAILED` with `result=failed`. A staged index or leftover conflict
markers never justify completion or a degrading-merge verdict. The generic
dashboard detail projection and structured schema remain unchanged.
`tests/test_cloud_conflict_delegation.py::test_local_conflict_repair_emits_confirmed_remediation_outcome`
checks both production outcomes; the real Git/private-workspace checks and run
command are recorded in `docs/dashboard-observability.md`.

Local Issue PR recovery uses the existing commit/push and PR-publication stages.
Successful publication of existing commits is `COMPLETED`; an empty base diff
is `SKIPPED` for commit/push and `BLOCKED` for PR publication, with a reason in
the stage facts. Fetch, inspection, and push errors remain `FAILED`.
`TestKeepLabelOnPRCreation` in `tests/test_issue_processor.py` exercises these
production boundaries; see the Local Issue publication admission inventory in
`docs/dashboard-observability.md` and
`local-issue-pr-publication-recovery.md`.

Dashboard observability is protected by deterministic tests in the ordinary PR
shards and by a documented `scripts/test.sh` invocation. The maintained coverage
inventory maps processing, validation, resumption, CI, merge, and asynchronous
origins to concrete collected tests. Joined regressions execute production entry
points, consume the real structured collector snapshot through the mounted detail
view, and independently assert business outcomes. Negative controls reject missing
emissions, scope reattribution, evidence coercion, and false completion while the
generic renderer continues to accept new or repeated stages without a node map.

Runtime owner-lock timeouts reach the mounted detail view as a deferred
implementation-admission stage and a deferred execution outcome, including the
operational reason. They do not imply implementation success or specification
rejection. `test_owner_lock_timeout_defers_and_retries_with_mounted_evidence`
holds the real owner lock, exercises the shared processing boundary, checks the
mounted view and item identity, and verifies admission after the lock is released.

Shared GitHub admission tickets remain operational capacity evidence. Queue waits
and ticket release never create a dashboard execution or imply provider progress.
The `admission_queue` refusal uses the existing typed `Deferred` result and durable
pending-work handoff. Governor process-order regressions and the production wrapped
reconciliation regression are recorded in `docs/dashboard-observability.md`; the
processing trace schema and rendered outcome meanings remain unchanged.

Initial implementation-clone tests emit `local.workspace-tests` before the
provider runs. Unexpected Muse approval or user-input requests emit
`llm.muse-interactive-request` with a blocked outcome instead of remaining hidden
until the invocation timeout. The mounted detail view presents these observed
stages under the caller's execution identity. Initial-test completion never means
implementation completion; a failed test baseline and a blocked provider request
remain distinct. Raw command and approval-subject text is excluded from these
facts. `test_muse_initial_tests_and_interactive_refusal_reach_mounted_detail`
drives both real producers through an executable MSP host and checks the mounted
view, outcomes, exact identity, exit code, and absence of sensitive subject text.

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

Local review correction exposes the pending allowance generation through the
existing repair-delegation stage's `local_phase=awaiting_validation` fact and
continues to scoped verification of only unsettled covered targets. Its
`effect=local-validation-scoped` facts include the generation, exact head,
unverified count, and each unverified target's identity and reason. Incomplete
verification is `BLOCKED`; publication or settlement failure is `FAILED`, with
the local route, retained phase and reason visible in the generic detail table.
The local repair generation revalidation inventory in
`docs/dashboard-observability.md` records the regression commands; the trace
schema and renderer remain unchanged.
That inventory also covers already-resolved original repair roots while excluding
unrelated findings. A production-to-mounted-view regression asserts that an
omitted verification target remains pending and is visible with the same identity
and reason in both the dashboard and the GitHub native review. Completed scoped
verification remains a deferred processing result and does not imply merge approval.
