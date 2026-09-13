# Dashboard observability verification

The detail view is a projection of **observed local evidence**. It does not query
GitHub or a provider and it does not turn absent, unavailable, partial, throttled,
or superseded evidence into a pass or failure. An accepted provider submission is
a handoff, not proof that a pull request was published or that implementation
completed. Each top-level evaluation or durable resumption has a distinct execution
identity; late evidence remains attached to the execution scope that produced it.

Inherited specification BLOCKED publication withdraws an explicitly submitted
child's readiness before the parent's readiness. The existing blocked outcome
and incomplete-publication reporting remain authoritative; no new origin,
provider route, or event schema is introduced. Readiness completion now includes
both targets. Dashboard consumers must not infer label mutation from a BLOCKED
verdict alone. Run
`bash scripts/test.sh tests/test_specification_validation_lifecycle.py tests/test_validation_publication_resumption.py`
for the production lifecycle regressions, including
`test_inherited_blocked_withdraws_child_and_parent_with_restart_retry` and
`test_inherited_blocked_edit_after_comment_preserves_both_labels`.

## Updating observable processing

Explicit Issue restart (`--only <issue> --force --retry`) keeps the
`explicit-single-target` origin and emits `issue.manual-retry` after admission,
with the Issue owner and explicit flag reason. `completed` means retry was
authorized, not that a provider accepted it. Existing provider dispatch stages
report the subsequent handoff or failure. No event schema change is needed.
`tests/test_specification_validation_lifecycle.py::test_manual_retry_retained_provider_admission_and_trace`
checks the real admission path and emitted authorization for every flag boundary;
`tests/test_dashboard_observability.py::test_manual_retry_authorization_reaches_mounted_detail`
checks its mounted detail projection. Run
`bash scripts/test.sh tests/test_manual_cloud_retry.py tests/test_specification_validation_lifecycle.py tests/test_dashboard_observability.py tests/test_process_issues_cloud_only.py`.

Family reconciliation emits `issue.family-discovery` after confirming related
declarations against live GitHub reads. Its facts identify
`discovery_source=cached-open-issue-list`,
`live_scope=related-declarations-and-native-children`,
`declared_issue_numbers`, and `authorizes_execution=false`. The one-hour list
cache discovers candidates; only family members bypass it for confirmation.
This completed stage does not claim complete live repository discovery or
implementation readiness. The production-path regression
`tests/test_parent_issue_reconciliation.py::test_family_discovery_refreshes_only_related_issues_and_records_scope`
checks both the bounded GitHub calls and the actual collector event; stale
declarations and uncached native-member conflicts are covered in the same file.
`tests/test_dashboard_observability.py::test_family_discovery_scope_reaches_mounted_detail`
joins that production event to the mounted detail view without issuing any
repository-wide strict discovery request.
The `--only` startup exception emits `issue.explicit-relationship-discovery`
with `discovery_source=live-open-issue-list`, `live_scope=all-open-issues`, and
`authorizes_execution=false`.
`tests/test_parent_issue_reconciliation.py::test_only_parent_reconciles_every_declared_child_before_unified_processing`
uses a stale cache omitting children and proves that the real explicit entry
point refreshes all Issues, including an unrelated Issue, before materializing
the target family.

Early dependency waiting emits `issue.cached-dependency-wait` as `deferred`
before authoritative refresh/family validation. The Issue execution includes
`waiting_on`, `retry_at`, `evidence_source=local-issue-observation`, and
`authorizes_execution=false`. No slot or provider execution is implied.
`test_cached_dependency_wait_reaches_mounted_detail` in
`tests/test_dashboard_observability.py` runs the production gate and mounted
detail projection with zero GitHub calls; `test_worker_skips_before_refresh_validation_and_slot`
in `tests/test_dependency_observation_cache.py` verifies the real worker releases
the candidate while retaining a durable retry. Run both with
`bash scripts/test.sh tests/test_dependency_observation_cache.py tests/test_dashboard_observability.py`.
The process-local per-Issue trust expires after 300 seconds and is discarded at
startup. Issue webhooks wake advisory waits; GitHub cooldowns remain in force.

Worker occupancy includes pre-dispatch GitHub refresh, submitted-parent validation,
and dependency expansion. `test_worker_status_owns_candidate_during_pre_dispatch`
in `tests/test_entity_invalidation.py` holds each production worker boundary open,
checks the dashboard's `get_status()` projection, and verifies release and durable
completion. This occupancy correction changes no processing origin, admission gate,
outcome, or structured event schema; existing execution traces remain unchanged.
Run it with `bash scripts/test.sh tests/test_entity_invalidation.py`.

Issue refusal caching emits `issue.cached-blocked-admission` with outcome
`blocked`, the refusal reason, `evidence_source=local-negative-cache`, and
`authorizes_execution=false`. A worker can reach this boundary before its first
GitHub refresh; it still opens an Issue execution and finishes it as blocked.
No validation, dispatch, or successful completion is inferred from a cache hit.
`test_cached_terminal_refusal_reaches_mounted_detail_without_github` in
`tests/test_dashboard_observability.py` joins the real admission path to the
mounted detail view and asserts zero GitHub calls and no slot creation.

The observation cache is separate from authoritative GitHub admission: relevant
webhooks invalidate negative results for the entire repository, including parent,
sibling, and reverse-dependency effects, and complete Issue payloads update the
advisory body/label observation. Results from before an invalidation cannot be
saved afterward. Ordinary cached refusals expire after 300 seconds and are lost
on restart; this is an expiry bound, not a processing delay. Durable
`reissue_required` markers remain terminal across body edits. Incomplete
publication and operational retries remain outside the negative-result cache.

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

Terminal PR CI-watch retirement is observability-neutral: it changes the durable
producer's eligibility for future periodic invalidations, not a processing
origin, trace outcome, provider route, or structured event schema. The worker's
existing closed/absent lifecycle result remains the Dashboard authority; the
regressions in `tests/test_entity_invalidation.py` instead verify the underlying
database, restart, and due-promotion behavior that prevents phantom queue work.

Post-adversarial CI refresh reuses the existing `pr.ci-eligibility` stage rather
than introducing a new event schema. A newly pending observation emits `deferred`
and a newly failing or unavailable observation emits `blocked`, both with
`phase=post-adversarial-validation`; merge is not attempted. The production-path
regression `test_new_nonpassing_ci_observation_after_validation_blocks_merge` in
`tests/test_adversarial_validation_pr_flow.py` proves that a validator-accepted
replacement observation is applied again at the outer merge gate.
If a durable CI delivery fences that green refresh during the final head check,
the controller emits the same stage with `phase=pre-merge-authority` and obtains
one newer authoritative observation before allowing the merge mutation.
CI delivery persistence/fencing and the final authority-check/merge mutation use
one short authority barrier, so a delivery cannot become accepted in the gap
between the proof and mutation. The barrier does not cover provider reads.

## Implementation Slots panel (Issue #1993)

The main dashboard's Implementation Slots section is a separate
observation from the detail view's diagnostic trace: it projects
`ImplementationSlotRepository.snapshot()` (Issue #1992's coherent
read-only boundary) through `AutomationEngine.get_implementation_slot_snapshot`,
not the worker/queue state `get_status()` already reports. A persisted
execution, PR, or provider-session reference is recorded evidence of
ownership, not a claim that the owning process is currently running or
that a PR/session is still live -- the panel labels it as such rather than
inferring a running/completed/free state.

*   **Known vs. unavailable** are kept explicitly distinct, never coerced
    into a zero/free display: before any successful read, an unavailable
    observation shows only a diagnostic reason; after one, a later failure
    preserves the entire last-known snapshot (rows and counters together)
    with a stale indicator and an unchanged last-successful timestamp,
    never a partial mix of old rows and new counters.
*   **Writer contention**: observations read one atomically published file image
    without acquiring writer locks. Slow hierarchy admission can leave the
    published image unchanged but cannot make the panel unavailable. Pending
    admissions remain recorded evidence, not successful dispatch claims.
    `tests/test_implementation_slots.py::test_snapshot_reads_published_image_while_local_writer_holds_lock`
    and `tests/test_implementation_slots.py::test_snapshot_does_not_wait_for_another_process_store_lock`
    verify observations during local and cross-process writes.
    `tests/test_dashboard_slots_observability.py::test_empty_capacity_refresh_stays_known_during_writer_lock`
    verifies that the mounted panel continues showing a known 0/2 observation.
    This changes no admission decisions or structured trace events.
*   **Capacity honesty**: normal usage counts each non-emergency owner
    once regardless of how many executions/PRs/sessions it has recorded;
    emergency usage is reported separately and excluded from normal usage;
    usage above the configured limit is displayed, not clamped.
*   **Non-interference**: loading, refreshing, or navigating from this
    panel never issues a GitHub/provider request, a liveness probe, or a
    slot reservation/release/reconciliation call -- it is display-only.

Pure projection logic (`format_optional_bool`, `owner_row`, `summarize`,
...) lives in `dashboard_slots.py` and is unit-tested directly in
`tests/test_dashboard_slots.py`, independent of NiceGUI, matching
`dashboard_detail.py`'s existing split for the detail view.

Production-to-mounted-main-page regressions live in
`tests/test_dashboard_slots_observability.py`: real `ImplementationSlotRepository`
writes (admission, execution start/finish, PR/session membership, capacity
override, emergency admission, legacy/absent-field records, storage
read/parse failures) reach the mounted main page's actual `ui.timer`
callback, distinguishing production ownership, capacity/emergency honesty,
startup against the correctly-bound store, unknown-vs-empty, and
legacy/falsy-field observation without repair. Real-browser scroll/DOM/
non-blocking coverage (an unchanged tick, a membership-only update for one
owner, and a delayed observation boundary) lives in
`tests/test_dashboard_slots_scroll_stability.py`, mirroring the detail
view's own `test_dashboard_detail_scroll_stability.py` pattern.

This section does not add a new lifecycle/admission policy: the underlying
recorded meaning of an owner, an execution, or an admission flag is
entirely owned by `implementation_slots.py` and Issue #1992's snapshot
contract; this panel only renders that already-defined observation.

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

Live durable candidates are served by dedicated Issue and PR worker pools,
each with MAX_CONCURRENT_TASKS workers, preserving arrival order within each
priority and type. The dashboard queue snapshot groups PRs first for display;
it does not imply a cross-pool execution order or that processing has started.
Worker IDs remain unique across pools. This scheduling change is trace-neutral: processing origins, execution
scope creation, admission gates, and terminal emissions are unchanged, and an
already running Issue is not interrupted. The real enqueue/worker/restart and
queue-status contract is covered by
`tests/test_candidate_queue.py::test_durable_prs_overtake_issue_backlog_without_losing_generations`.
Independent progress while either lane is busy is covered by
`tests/test_candidate_queue.py::test_dedicated_workers_progress_while_other_type_is_busy`;
cancellation without consuming another lane is covered by
`tests/test_candidate_queue.py::test_typed_queue_waiters_cancel_without_consuming_other_lane`.

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
The short first-use initialization lock and same-instance contention retry are likewise
trace-neutral: they occur inside the existing HTTP governor boundary and neither create
an entity execution nor alter the typed pending-work handoff. The runnable production
boundary checks are
`tests/test_github_request_governor.py::test_simultaneous_first_use_converges_and_preserves_live_request`
and `tests/test_github_request_governor.py::test_pre_schema_pragma_contention_recovers_same_governor_instance`.

These are collected pytest node IDs, not future test plans. Producer tests assert
business results/effect counts and the real structured snapshot. The joined tests
also mount and refresh the detail view from that snapshot.

| Production origin | Runnable checks |
| --- | --- |
| Cached negative Issue admission, before strict refresh or family enumeration | `tests/test_dashboard_observability.py::test_cached_terminal_refusal_reaches_mounted_detail_without_github`; `tests/test_issue_admission_cache.py::test_worker_acknowledges_terminal_refusal_without_strict_refresh_or_validation`; `tests/test_issue_admission_cache.py::test_completed_contract_refusal_is_reused_until_webhook_then_strictly_refreshed` |
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
| Material test-oracle gap reconciliation | `tests/test_adversarial_test_oracle_gaps.py::test_same_head_addressed_gap_thread_persists_before_unrelated_inconclusive_resolution` (covers omitted and explicit-OPEN response variants and reopens the durable reviewer store before the external thread-resolution boundary). This changes the reconciled validation outcome but introduces no processing origin or structured event field; the existing `pr.adversarial-validation` result emission and dashboard consumer remain authoritative. |
| Adversarial gap-state acceptance fence | `tests/test_adversarial_validation_pr_flow.py::TestAdversarialValidationPRFlow::test_forced_only_retry_runs_for_an_already_validated_head` (registers a newer attempt before the owning serialized application boundary and proves the stale reviewer checkpoint is not saved or published). Head-unavailable and persistence-failure outcomes retain the existing `pr.adversarial-validation` event schema and are distinguished by the recorded application phase and validation diagnostic. |

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

### Claude PR implementation admission

`tests/test_dashboard_observability.py::test_claude_pr_slot_admission_reaches_detail_view`
executes the unified PR admission boundary with a real, full slot store and
mounts the resulting PR detail page. A Claude session URL matching recorded
ownership reuses the Issue slot; an unknown session remains a standalone PR
and is deferred. The `pr.implementation-admission` result reports the resolved
`owner` and either `reused_owner` on admission or `reason` on deferral. Completion
of this stage means admission passed, not that the PR merged. Both cases assert
unchanged occupancy independently of the rendered outcome.

## Adversarial CI evidence reuse

Canonical-suite evidence reuse consumes the existing `pr.ci-observation`
decision without changing its trace schema or the production CI gate. Rejected
dynamic targets are validator protocol diagnostics, not implementation defects.


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

The final merge gate owns its read phase independently of candidate selection.
This scope-lifetime correction preserves production trace stages, facts, and
outcome mappings: successful merges use the existing merge result, and newly
non-passing CI still emits `pr.ci-eligibility`. No renderer or schema change is
needed. The runnable regressions
`test_webhook_fence_after_green_final_refresh_forces_new_ci_gate` and
`test_ci_delivery_cannot_be_accepted_between_authority_check_and_merge_mutation`
in `tests/test_adversarial_validation_pr_flow.py` enter production without an
externally supplied phase and cover refreshed green, pending, repeated
invalidation, and the delivery/merge barrier.
