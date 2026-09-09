# Dashboard observability verification

The detail view is a projection of **observed local evidence**. It does not query
GitHub or a provider and it does not turn absent, unavailable, partial, throttled,
or superseded evidence into a pass or failure. An accepted provider submission is
a handoff, not proof that a pull request was published or that implementation
completed. Each top-level evaluation or durable resumption has a distinct execution
identity; late evidence remains attached to the execution scope that produced it.

## Updating observable processing

When changing a processing origin, gate, outcome, provider route, resumption handler,
or event schema:

1. Identify every reached business outcome and external effect independently of its
   diagnostics. Add or update the structured event at the production boundary where
   that outcome becomes known. Stage labels and facts are emitted through
   `TraceCollector.record_event` in the processing modules; execution identity is
   opened with `TraceCollector.start_execution` at worker, scheduler, and resumption
   entry points.
2. Update the affected inventory row below and its runnable regression. The test must
   execute the production entry point, control services only at their external
   boundary, read the real collector snapshot, and take that snapshot through the
   mounted detail-page refresh. Assert business results and effect counts separately
   from displayed stages and outcomes.
3. Update this guide and `docs/client-features.yaml`, or state a concrete reason in
   the PR description why the change is observability-neutral.
4. Run `bash scripts/test.sh tests/test_dashboard_observability.py` locally. The same
   file is collected by the ordinary `PR Tests` shards; failures are mandatory test
   failures, not semantic prompt-evaluation advice.

Changing `dashboard.py`, approving a screenshot/generated snapshot, or editing a
Mermaid node map is not sufficient: none proves that a real producer still emits
the evidence. The renderer is generic, so a valid new production-emitted stage and
repeated stage occurrences require no static graph edit. Display-text-only changes
likewise do not require meaningless producer changes.

## Production-origin coverage inventory

These are collected pytest node IDs, not future test plans. Producer tests assert
business results/effect counts and the real structured snapshot. The joined tests
also mount and refresh the detail view from that snapshot.

| Production origin | Runnable checks |
| --- | --- |
| Normal/explicit Issue processing and pre-worker admission | `tests/test_dashboard_observability.py::test_issue_admission_reaches_mounted_detail_view`; `tests/test_issue_production_instrumentation.py::TestPreAdmissionGateVisible::test_author_disallowed_issue_records_skip_without_dispatch` |
| Normal/explicit PR processing | `tests/test_dashboard_observability.py::test_pr_admission_reaches_mounted_detail_view`; `tests/test_pr_production_instrumentation.py::TestPrAdmissionGateVisible::test_author_disallowed_pr_records_skip_without_dispatch` |
| Individual/decomposition validation jobs | `tests/test_issue_production_instrumentation.py::TestValidationJobsGetTheirOwnExecutionIdentity::test_traced_validation_job_does_not_borrow_ambient_worker_scope`; `tests/test_issue_production_instrumentation.py::TestValidationJobsGetTheirOwnExecutionIdentity::test_disabled_decomposition_validation_is_distinguishable_from_blocked` |
| Issue pending-work resumption | `tests/test_issue_production_instrumentation.py::TestDurableResumptionCreatesAnotherExecution::test_resumption_origin_and_identity_differ_from_a_fresh_evaluation` |
| Validation-publication resumption | `tests/test_validation_publication_resumption.py::test_validation_publication_stage_handler_resumes_after_restart_without_readiness_label` |
| PR pending-work resumption | `tests/test_pr_production_instrumentation.py::TestPrResumptionSupersededHead::test_pending_work_resumption_records_superseded_on_changed_head` |
| Merge-operation resumption | `tests/test_pr_production_instrumentation.py::TestMergeOperationResumeSupersededHead::test_merge_operation_resumption_records_superseded_on_changed_head` |
| Asynchronous PR adversarial validation | `tests/test_adversarial_validation_pr_flow.py::test_take_pr_actions_preserves_structured_adversarial_failure` |

The broader outcome matrix is kept by the production suites above plus
`TestDispatchRouteRecorded`, `TestDispatchOutcomesAreHonest`,
`TestCiObservationAvailabilityIsNotABoolean`, the durable resumption suites, and
`tests/test_dashboard_detail_logic.py`. Together they cover admission without
dispatch; disabled/queued/blocked validation; local, Jules, Claude Routine, and
Codex Cloud routing (ordinary and high-score); handoff without publication;
known, known-empty, partial, unavailable, throttled, and superseded CI evidence;
corrective acceptance without a repair claim; ambiguous merge delivery and
idempotent resumption; pinned retention; recorder failure; repeated and unknown
stages; and non-mutating dashboard refreshes.

## Negative controls

The joined suite suppresses a required producer emission and proves the
production-to-view assertion rejects it. Scope-isolation tests swap/concurrently
propagate execution contexts and reject reattribution. CI availability tests reject
boolean coercion, and handoff/publication tests reject invented completion. Generic
unknown-stage and display-format tests ensure those semantic controls do not become
a touched-file or static-diagram rule.
