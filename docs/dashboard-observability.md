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

## Codex quota transport

Codex quota reads use the app-server account API. This transport change does not
introduce a new processing origin, provider dispatch, durable resumption path, or
trace schema: quota retrieval still feeds the existing eligible, insufficient, and
retrieval-failed branches. API-key fallback now requires a confirmed account type;
unknown authentication takes the existing retrieval-failed branch. Cloud admission
still emits `issue.dispatch.codex-cloud` with a deferred usage-limit outcome when
quota cannot authorize submission. Dashboard rendering and production emissions
therefore need no new stage or field for the transport itself.

`tests/test_codex_app_server.py`, `tests/test_codex_usage_checker.py`, and
`tests/test_quota_selector.py` exercise the new read boundary and its decisions;
`tests/test_dashboard_observability.py::test_codex_app_server_failure_remains_deferred_in_detail_view` drives the real Cloud admission path from a failed account read to the mounted detail view and asserts that no task or comment is sent.

## Production-origin coverage inventory

CI correlation supersession is covered by
`tests/test_ci_webhook_intake.py::test_ci_drain_survives_delivery_during_lookup_and_promotes_other_prs`
and `test_superseded_correlation_preserves_delivery_and_retry_across_restart`.
A new delivery during SHA lookup retains the pending correlation, discards the
stale lookup result, and lets independent PR batches proceed. This is trace-neutral:
correlation occurs before an entity execution is opened, and its existing durable
invalidation path still creates the worker execution and emits PR processing
stages. The controller logs supersession instead of false correlation completion;
no CI verdict, completed implementation, or synthetic dashboard execution is emitted
from a webhook or lookup. Run these regressions with
`bash scripts/test.sh tests/test_ci_webhook_intake.py tests/test_dashboard_observability.py`.
The related supervisor-cancellation regression is
`tests/test_graceful_shutdown.py::test_supervisor_cancellation_waits_for_owned_work_then_stops_caller`.
Owned local work still reaches its real boundary before cancellation propagates;
the existing worker cancellation and durable-claim cleanup paths remain the
authority. This creates no new execution origin or trace schema, and an explicit
graceful drain still finalizes its result through the existing checkpoints.

Cloud submission rejection is covered by
`tests/test_dashboard_observability.py::test_cloud_submission_slot_cleanup_reaches_detail_view`.
It drives ordinary and high-score Cloud routing through the real submission
journal and slot cleanup, checks capacity availability independently, and mounts
the resulting detail view. The `issue.cloud-submission-slot-release` stage reports
`slot_released=true` with `completed` only after atomic removal; retained work
reports `slot_released=false` with `deferred`. Accepted and indeterminate
submissions do not enter this cleanup path. A completed cleanup stage describes
capacity recovery, not successful implementation; dispatch remains deferred.

Live durable candidates are scheduled with PRs ahead of waiting Issues and
dependency work, preserving arrival order within each priority. The dashboard's
queue snapshot reports that order and priority; it does not imply processing has
started. This scheduling change is trace-neutral: processing origins, execution
scope creation, admission gates, and terminal emissions are unchanged, and an
already running Issue is not interrupted. The real enqueue/worker/restart and
queue-status contract is covered by
`tests/test_candidate_queue.py::test_durable_prs_overtake_issue_backlog_without_losing_generations`.

Authoritative-refresh admission deferrals remain before candidate processing and
therefore do not open or finish an implementation execution. This is deliberately
trace-neutral: the existing durable-invalidation-worker origin begins only after a
current authoritative candidate exists, so representing a definitely-not-sent read
as a successful, failed, or completed execution would invent an outcome. The durable
reason, API origin, generation, and deadline are operational queue diagnostics, and
`tests/test_entity_invalidation.py::test_worker_persists_real_strict_refresh_deferral_without_candidate_error`
drives the production worker and strict-read adapter and verifies that processing is
not dispatched.

Shared-governor incarnation ownership changes the existing HTTP admission gate but is
dashboard-observability neutral. Governor admission still occurs before an Issue or PR
processing execution outcome becomes known, and the durable pending-work paths still
record the same typed deferral; emitting a dashboard completion or failure for live-owner
contention or orphan recovery would invent business progress. Secret-safe operational
diagnostics and the SQLite state are the appropriate evidence. The production-boundary
regressions are `tests/test_github_request_governor.py`, while
`tests/test_entity_invalidation.py::test_worker_persists_real_strict_refresh_deferral_without_candidate_error`
continues to cover the unchanged trace handoff.

These are collected pytest node IDs, not future test plans. Producer tests assert
business results/effect counts and the real structured snapshot. The joined tests
also mount and refresh the detail view from that snapshot.

| Production origin | Runnable checks |
| --- | --- |
| Codex Cloud quota acquisition and admission | `tests/test_dashboard_observability.py::test_codex_app_server_failure_remains_deferred_in_detail_view` |
| Standalone sibling-dependency admission (empty vs nonempty declaration) | `tests/test_dashboard_observability.py::test_standalone_dependency_gate_reaches_mounted_detail_view` |
| Normal/explicit Issue processing and pre-worker admission | `tests/test_dashboard_observability.py::test_issue_admission_reaches_mounted_detail_view`; `tests/test_dashboard_observability.py::TestNewOriginCoverage::test_explicit_single_target_origin_is_recorded`; `tests/test_issue_production_instrumentation.py::TestPreAdmissionGateVisible::test_author_disallowed_issue_records_skip_without_dispatch` |
| Normal/explicit PR processing | `tests/test_dashboard_observability.py::test_pr_admission_reaches_mounted_detail_view`; `tests/test_pr_production_instrumentation.py::TestPrAdmissionGateVisible::test_author_disallowed_pr_records_skip_without_dispatch`; `tests/test_pr_production_instrumentation.py::TestPrAdmissionGateVisible::test_dependency_bot_pr_admission_records_skip_without_dispatch` (Issue #1995: the common `pr.dependency-bot-admission` gate, reached by every PR-processing origin, not only the `_get_candidates` prefilter) |
| Individual/decomposition validation jobs | `tests/test_dashboard_observability.py::test_standalone_dependency_gate_reaches_mounted_detail_view` drives the real standalone worker and lifecycle for fresh model evaluation, stored-decision reuse, and a local-only Objective-integrity result; `tests/test_specification_validation_lifecycle.py::test_async_logical_owner_allows_changed_generation_validation` drives retained provider ownership through production reevaluation; `tests/test_issue_production_instrumentation.py::TestValidationJobsGetTheirOwnExecutionIdentity::test_traced_validation_job_does_not_borrow_ambient_worker_scope`; `tests/test_issue_production_instrumentation.py::TestValidationJobsGetTheirOwnExecutionIdentity::test_disabled_decomposition_validation_is_distinguishable_from_blocked`; `tests/test_dashboard_observability.py::TestOutcomeMatrixCoverage::test_queued_validation_is_distinguishable_from_disabled_and_blocked`; `tests/test_dashboard_observability.py::TestJoinedProductionToView::test_validation_scheduler_job_is_a_distinct_execution_from_the_worker` covers the parent/direct-child scheduler and mounted detail callback. Producers have separate execution identities; caller observations reference the exact decision identity. Reused and local-only lifecycle decisions are labeled by evaluation source, and disabled bypasses remain observations rather than fresh model executions. Evidence is process-local and is not restart-readable review history. |
| Issue pending-work resumption | `tests/test_issue_production_instrumentation.py::TestDurableResumptionCreatesAnotherExecution::test_resumption_origin_and_identity_differ_from_a_fresh_evaluation` |
| Validation-publication resumption | `tests/test_dashboard_observability.py::TestNewOriginCoverage::test_validation_publication_resumption_origin_is_recorded` (drives `_ValidationPublicationStageHandler` and reads the real `TraceCollector` snapshot back); `tests/test_validation_publication_resumption.py::test_validation_publication_stage_handler_resumes_after_restart_without_readiness_label` (same production handler, asserts the durable-effect/no-duplicate-delivery contract rather than the diagnostic trace) |
| PR pending-work resumption | `tests/test_pr_production_instrumentation.py::TestPrResumptionSupersededHead::test_pending_work_resumption_records_superseded_on_changed_head`; `tests/test_dashboard_observability.py::TestJoinedProductionToView::test_pr_pending_work_resumption_reaches_detail_view_as_superseded` |
| Merge-operation resumption | `tests/test_pr_production_instrumentation.py::TestMergeOperationResumeSupersededHead::test_merge_operation_resumption_records_superseded_on_changed_head`; `tests/test_dashboard_observability.py::TestJoinedProductionToView::test_merge_operation_resumption_reaches_detail_view_as_superseded` |
| Asynchronous PR adversarial validation | `tests/test_dashboard_observability.py::TestNewOriginCoverage::test_asynchronous_pr_adversarial_validation_origin_is_recorded` (drives `_handle_pr_merge` through the real `AdversarialValidationScheduler` admission and reads the real `pr.adversarial-validation` event back; `test_take_pr_actions_preserves_structured_adversarial_failure` mocks `_handle_pr_merge` itself, so it does not exercise this emission) |

The broader outcome matrix is kept by the production suites above plus
`TestDispatchRouteRecorded`, `TestDispatchOutcomesAreHonest`,
`TestCiObservationAvailabilityIsNotABoolean` (`known`, `unavailable`, and a
`known` -> `unavailable` -> `known` sequence),
`tests/test_dashboard_observability.py::TestOutcomeMatrixCoverage` (`known_empty`,
`partial`, `throttled`, and `superseded` CI availability; ordinary-cloud routing to
Claude Routine and Codex Cloud backend types; corrective work accepted without a
repair claim), the durable resumption suites, and `tests/test_dashboard_detail_logic.py`.
Together they cover admission without dispatch; disabled/queued/blocked validation;
local, Jules, Claude Routine, and Codex Cloud routing (ordinary and high-score);
handoff without publication; known, known-empty, partial, unavailable, throttled,
and superseded CI evidence; corrective acceptance without a repair claim; ambiguous
merge delivery and idempotent resumption; pinned retention; recorder failure;
repeated and unknown stages; and non-mutating dashboard refreshes.

## Negative controls

`tests/test_dashboard_observability.py::test_missing_producer_emission_is_rejected_by_joined_oracle`
suppresses a required producer emission and proves the production-to-view assertion
rejects it. `TestAdditionalNegativeAndMutationControls::test_concurrent_items_never_share_or_swap_execution_identity`
drives two different Issues through the real worker entrypoint concurrently and
asserts their execution identities and event scopes stay fully disjoint -- proving
scope reattribution across concurrently propagated execution contexts is rejected,
not merely asserted at the unit level. CI availability tests
(`TestOutcomeMatrixCoverage` and `TestAdditionalNegativeAndMutationControls::test_unavailable_ci_evidence_is_never_rendered_as_success_or_failure`,
the latter through the mounted detail view) reject boolean coercion, and
handoff/publication tests (`TestJoinedProductionToView::test_accepted_handoff_reaches_detail_view_without_pr_publication`)
reject invented completion. Generic unknown-stage and display-format tests in
`tests/test_dashboard_detail_logic.py` and `tests/test_execution_trace.py` ensure
those semantic controls do not become a touched-file or static-diagram rule.

## Standalone dependency admission

The final Issue dispatch boundary emits `issue.sibling-dependency-gate`. A
completed gate means the dependency check passed, not that implementation
finished. Empty `Blocked-By:` on an authoritatively parentless Issue can pass;
nonempty or malformed parentless declarations and unavailable relationship
evidence remain deferred. Native child validation remains unchanged. The joined
regression above uses the production reconciliation path, checks whether dispatch
is reached independently, and renders the actual collector event in the detail
view. No static diagram mapping or new dashboard request is needed.
