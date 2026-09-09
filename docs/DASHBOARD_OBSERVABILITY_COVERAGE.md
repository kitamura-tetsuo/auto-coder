# Dashboard Observability Coverage Inventory

This is the coverage inventory required by Issue #1948 REQ-003: it maps each
minimum production origin to the concrete, currently-collected tests that
exercise it end to end -- a real production entrypoint emitting into the real
`execution_trace.TraceCollector`, read back either directly or through the
real mounted `/dashboard/detail/{item_type}/{item_number}` page.

See `docs/DASHBOARD.md` ("Keeping observability accurate" section) for how
to run these locally and what to update when processing changes.

Run everything in this inventory with:

```bash
bash scripts/test.sh tests/test_issue_production_instrumentation.py \
  tests/test_pr_production_instrumentation.py \
  tests/test_dashboard_observability_joined.py \
  tests/test_execution_trace.py \
  tests/test_dashboard_detail_logic.py \
  tests/test_dashboard_detail.py
```

No test in this inventory requires live GitHub/provider credentials or an
LLM call: GitHub/provider responses are controlled at the boundary with
small scripted fakes or `unittest.mock`, and the diagnostic evidence itself
is always read back from the real `TraceCollector` singleton (never a
recorder mocked to return canned events).

## Origins (REQ-003 of #1948)

| # | Production origin | Covered by |
|---|---|---|
| 1 | Normal Issue/PR processing (`origin="worker"`) | `tests/test_issue_production_instrumentation.py::TestPreAdmissionGateVisible`, `::TestDispatchRouteRecorded`; `tests/test_pr_production_instrumentation.py::TestPrAdmissionGateVisible`; joined: `tests/test_dashboard_observability_joined.py::TestJoinedProductionToView::test_issue_admission_denial_reaches_detail_view_without_dispatch` |
| 2 | Explicit single-target Issue/PR processing (`origin="explicit-single-target"`) | `tests/test_dashboard_observability_joined.py::TestNewOriginCoverage::test_explicit_single_target_origin_is_recorded` |
| 3 | Pre-worker Issue admission | `tests/test_issue_production_instrumentation.py::TestPreAdmissionGateVisible::test_author_disallowed_issue_records_skip_without_dispatch` |
| 4 | Individual/decomposition Issue validation jobs (`origin="validation-scheduler"`) | `tests/test_issue_production_instrumentation.py::TestValidationJobsGetTheirOwnExecutionIdentity`; joined: `TestJoinedProductionToView::test_validation_scheduler_job_is_a_distinct_execution_from_the_worker`, `TestOutcomeMatrixCoverage::test_queued_validation_is_distinguishable_from_disabled_and_blocked` |
| 5 | Issue pending-work resumption (`origin="issue-pending-work-resumption"`) | `tests/test_issue_production_instrumentation.py::TestDurableResumptionCreatesAnotherExecution` |
| 6 | Validation-publication resumption (`origin="validation-publication-resumption"`) | `tests/test_dashboard_observability_joined.py::TestNewOriginCoverage::test_validation_publication_resumption_origin_is_recorded` |
| 7 | PR pending-work resumption (`origin="pr-pending-work-resumption"`) | `tests/test_pr_production_instrumentation.py::TestPrResumptionSupersededHead`; joined: `TestJoinedProductionToView::test_pr_pending_work_resumption_reaches_detail_view_as_superseded` |
| 8 | Merge-operation resumption (`origin="merge-operation-resumption"`) | `tests/test_pr_production_instrumentation.py::TestMergeOperationResumeSupersededHead`; joined: `TestJoinedProductionToView::test_merge_operation_resumption_reaches_detail_view_as_superseded` |
| 9 | Asynchronous/concurrency-admitted PR adversarial validation (`pr.adversarial-validation`, admitted through `AdversarialValidationScheduler`) | `tests/test_dashboard_observability_joined.py::TestNewOriginCoverage::test_asynchronous_pr_adversarial_validation_origin_is_recorded` |

Each origin check verifies the correct repository/item/execution
attribution (`repository`, `item_type`, `item_number`, `execution_id`,
`origin`) and the displayed result (`outcome`, relevant `facts`) rather than
only that a call happened.

## Outcome matrix (REQ-004 of #1948)

| Case | Covered by |
|---|---|
| Observed admission denial without dispatch | `TestPreAdmissionGateVisible` (Issue, PR); joined `test_issue_admission_denial_reaches_detail_view_without_dispatch` |
| Disabled vs. queued vs. blocked validation | `TestValidationJobsGetTheirOwnExecutionIdentity::test_disabled_decomposition_validation_is_distinguishable_from_blocked`; joined `TestOutcomeMatrixCoverage::test_queued_validation_is_distinguishable_from_disabled_and_blocked` |
| Local execution dispatch | `test_pr_production_instrumentation` local-mode case; `test_issue_production_instrumentation.py::TestDispatchRouteRecorded::test_local_mode_records_local_route` |
| Jules handoff | `TestDispatchOutcomesAreHonest::test_jules_dispatch_records_accepted_handoff` |
| Claude Routine / Codex Cloud handoff, ordinary cloud selection | joined `TestOutcomeMatrixCoverage::test_ordinary_cloud_selects_claude_routine_and_codex_cloud` (parametrized) |
| High-score cloud selection | `TestDispatchRouteRecorded::test_difficult_label_routes_to_high_score_cloud` |
| Handoff without PR publication | joined `TestJoinedProductionToView::test_accepted_handoff_reaches_detail_view_without_pr_publication` |
| CI evidence: known / known-empty / partial / unavailable / throttled / superseded, without boolean collapse | `test_pr_production_instrumentation.py::TestCiObservationAvailabilityIsNotABoolean` (known, unavailable, known->unavailable->known sequence); joined `TestOutcomeMatrixCoverage::test_known_empty_ci_availability_is_deferred_not_a_pass_or_failure`, `::test_partial_ci_availability_is_unknown_not_a_pass_or_failure`, `::test_throttled_ci_availability_is_unknown_not_a_pass_or_failure`, `::test_superseded_ci_availability_is_reported_as_superseded` |
| Corrective work accepted without claiming a repair | joined `TestOutcomeMatrixCoverage::test_corrective_work_accepted_without_claiming_a_repair` |
| Ambiguous merge delivery followed by real scheduler resumption without duplicate mutation | `test_pr_production_instrumentation.py::TestMergeOperationResumeSupersededHead`; joined `TestJoinedProductionToView::test_merge_operation_resumption_reaches_detail_view_as_superseded` (fresh execution identity on resumption; no fabricated merge/cleanup) |

## Negative / mutation controls (REQ-005, REQ-007 of #1948)

| Control | Covered by |
|---|---|
| A suppressed production emission is detected, not silently accepted | `TestNegativeAndMutationControls::test_suppressed_production_emission_is_detected_not_silently_accepted` |
| Concurrent items never share or swap execution identity | `TestNegativeAndMutationControls::test_concurrent_items_never_share_or_swap_execution_identity` |
| Unavailable evidence is never rendered as success or failure by boolean coercion | `TestNegativeAndMutationControls::test_unavailable_ci_evidence_is_never_rendered_as_success_or_failure`; unit-level: `tests/test_dashboard_detail_logic.py::TestEvidenceRows::test_unknown_outcome_is_not_coerced_to_success_or_failure`, `tests/test_dashboard_detail_logic.py::TestFormatFactValue::test_booleans_are_literal_not_coerced` |
| Accepted handoff never becomes a fabricated completion | joined `TestJoinedProductionToView::test_accepted_handoff_reaches_detail_view_without_pr_publication` |
| A previously unseen valid stage/outcome renders without a dashboard node-map edit | `tests/test_execution_trace.py::TestExtensibilityAndLegacy::test_unseen_stage_identifier_preserved_unchanged`; `tests/test_dashboard_detail_logic.py::TestObservedPathDiagram::test_repeated_stage_and_unknown_stage_both_render_as_distinct_nodes` |
| A repaired/no-op producer emission is detected only by presence, not by which file was touched -- the suppression control above proves the converse of "a fixture supplying the expected stage without executing its production emission is not evidence against missing instrumentation" (REQ-005): the *absence* of the real emission call is what the check fails on | `TestNegativeAndMutationControls::test_suppressed_production_emission_is_detected_not_silently_accepted` |

## Adding a new checked origin

When you add a new production entry point that should emit diagnostic
evidence:

1. Add a boundary-level test in `tests/test_issue_production_instrumentation.py`
   or `tests/test_pr_production_instrumentation.py` that drives the real
   entrypoint (not a fabricated event) and reads the result back from the
   real `TraceCollector`.
2. If the new origin should also be exercised production-to-page, add a case
   to `tests/test_dashboard_observability_joined.py` that additionally mounts
   `/dashboard/detail/{item_type}/{item_number}` and asserts on the rendered
   output.
3. Add a row to the tables above.
4. If the change affects processing origins, gates, outcomes, provider
   routing, resumption paths, or the event schema, follow
   `docs/DASHBOARD.md`'s "Keeping observability accurate" checklist.
